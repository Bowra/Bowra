"""Bounded, authenticated metadata discovery for Composer references."""

from __future__ import annotations

import logging
import os
import sqlite3

from bowhead_core.chat_reference_catalog import ReferenceCatalogError, ReferenceCatalogRequest, ReferenceCatalogResponse, read_reference_catalog
from bowhead_core.chat_references import AgentResourceRef, decode_reference_json
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.user_store import User, UserStore
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from bowhead_gateway.access_context import build_access_context_from_request
from bowhead_gateway.chat_reference_start import (
    current_reference_lead_peers,
    reference_initial_runtime_ready,
    reference_mcp_writer_ready,
    reference_plan_writer_ready,
)
from bowhead_gateway.routes.auth import _current_user, _store
from bowhead_gateway.terminal_protocol_rollout import (
    ChatExecutionWriteProtocol,
    chat_execution_writer_readiness,
    durable_chat_execution_config,
    resolve_chat_execution_admission,
    terminal_v1_web_client_capable,
)

MAX_CATALOG_REQUEST_BYTES = 32 * 1024
logger = logging.getLogger(__name__)


def _error(status: int, code: str) -> HTTPException:
    return HTTPException(status, detail={"error": {"code": code}})


class ReferenceCatalogRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
                raise _error(415, "reference_input_media_type_required")
            buffer = bytearray()
            async for chunk in request.stream():
                if len(buffer) + len(chunk) > MAX_CATALOG_REQUEST_BYTES:
                    raise _error(413, "reference_request_too_large")
                buffer.extend(chunk)
            raw = bytes(buffer)
            try:
                decoded = decode_reference_json(raw, max_bytes=MAX_CATALOG_REQUEST_BYTES)
                if not isinstance(decoded, dict):
                    raise ValueError("object required")
            except (ValueError, RecursionError) as exc:
                raise _error(422, "reference_input_invalid") from exc
            request._body, request._json = raw, decoded
            try:
                return await original(request)
            except RequestValidationError as exc:
                raise _error(422, "reference_input_invalid") from exc

        return handler


router = APIRouter(route_class=ReferenceCatalogRoute)


async def _initial_protocol_available(request: Request, *, user: User, body: ReferenceCatalogRequest) -> bool:
    """Use the POST writer's canonical protocol decision without opening a Turn."""
    try:
        facts = None
        if body.session_id is not None:
            facts = await request.app.state.terminal_protocol_store.read_session_protocol_facts(
                team_id=user.team_id or "", actor=user.id, workspace_id=body.workspace_id, session_id=body.session_id
            )
            if facts is None:
                return False
        config = durable_chat_execution_config()
        readiness = chat_execution_writer_readiness(getattr(request.app.state, "terminal_writer_service", None), config=config)
        decision = resolve_chat_execution_admission(
            config=config,
            team_id=user.team_id,
            session_facts=facts,
            client_capable=terminal_v1_web_client_capable(request.headers),
            action_identity_complete=False,
            require_action_identity=True,
            existing_writer_ready=readiness.existing_writer_ready,
            fresh_writer_ready=readiness.fresh_writer_ready,
        )
        return decision.write_protocol is ChatExecutionWriteProtocol.LEGACY_V0
    except Exception:
        return False


@router.post("/catalog", response_model=ReferenceCatalogResponse)
async def reference_catalog(
    body: ReferenceCatalogRequest,
    response: Response,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> ReferenceCatalogResponse:
    async def skill_access():
        access_store = getattr(request.app.state, "access_control_store", None)
        if access_store is None:
            raise ValueError("catalog_metadata_unavailable")
        context = await build_access_context_from_request(request, user=user, workspace_id=body.workspace_id, operation=SpaceOperation.READ_HISTORY)
        return access_store, context

    try:
        writer_available = reference_plan_writer_ready(request) if body.entry_mode == "plan" else reference_initial_runtime_ready(request)
        automatic_receiver_identities = None
        if writer_available:
            try:
                await build_access_context_from_request(request, user=user, workspace_id=body.workspace_id, operation=SpaceOperation.EXECUTE)
            except HTTPException:
                writer_available = False
        if writer_available:
            writer_available = await _initial_protocol_available(request, user=user, body=body)
        if writer_available and body.trigger == "/" and not any(isinstance(ref, AgentResourceRef) for ref in body.selected_refs):
            try:
                peers = await current_reference_lead_peers(
                    request,
                    team_id=user.team_id or "",
                    user_id=user.id,
                    workspace_id=body.workspace_id,
                )
                automatic_receiver_identities = frozenset(
                    (peer.source, peer.agent_id, peer.name) for peer in peers if peer.source in {"team", "system"} and peer.agent_id and peer.name
                )
            except Exception as exc:
                logger.warning("reference_catalog.lead_roster_unavailable error=%s", type(exc).__name__)
                automatic_receiver_identities = frozenset()
        mcp_index = mcp_cache.current() if (mcp_cache := getattr(request.app.state, "reference_mcp_metadata", None)) is not None else None
        mcp_access = getattr(request.app.state, "access_control_store", None)
        result = await read_reference_catalog(
            store.db_path,
            team_id=user.team_id or "",
            user_id=user.id,
            query=body,
            locale=user.locale,
            builtin_skill_index=getattr(request.app.state, "builtin_skill_reference_metadata", None),
            builtin_skill_access=skill_access,
            uploaded_skill_index=cache.current()
            if (cache := getattr(request.app.state, "uploaded_skill_reference_metadata", None)) is not None
            else None,
            system_agent_index=system_cache.current()
            if (system_cache := getattr(request.app.state, "system_agent_reference_metadata", None)) is not None
            else None,
            initial_writer_available=writer_available,
            capability_writer_available=writer_available and os.environ.get("FEATURE_CHAT_REFERENCE_CAPABILITY", "0") == "1",
            mcp_writer_available=writer_available
            and reference_mcp_writer_ready(
                request,
                mcp_index=mcp_index,
                mcp_access=mcp_access,
            ),
            mcp_prompt_writer_available=writer_available and os.environ.get("FEATURE_CHAT_REFERENCE_MCP_PROMPT", "0") == "1",
            receiver_index=receiver_cache.current()
            if (receiver_cache := getattr(request.app.state, "reference_receiver_metadata", None)) is not None
            else None,
            mcp_index=mcp_index,
            automatic_receiver_identities=automatic_receiver_identities,
            tool_registry=getattr(getattr(request.app.state, "orchestrator", None), "tool_registry", None),
        )
    except ReferenceCatalogError as exc:
        raise _error(
            404 if exc.code == "reference_scope_unavailable" else 503 if exc.code == "reference_catalog_unavailable" else 409, exc.code
        ) from exc
    except sqlite3.Error as exc:
        raise _error(503, "reference_catalog_unavailable") from exc
    response.headers["Cache-Control"] = "private, no-store"
    return result
