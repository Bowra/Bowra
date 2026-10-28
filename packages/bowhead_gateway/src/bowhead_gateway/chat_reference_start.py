"""Initial HTTP wiring consumes the same released cells as discovery.

Released code support remains separate from per-deployment readiness. The
worker flag, key, stores, source resolver and current authorization must all be
ready before this writer accepts an Agent/Skill/Capability/MCP submission.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from bowhead_core.chat_reference_admission import ReferenceStartAdmission
from bowhead_core.chat_reference_catalog import EntryMode, initial_reference_authoring_supported, initial_reference_entry_supported
from bowhead_core.chat_reference_preparation import prepare_reference_system_start_admission, prepare_reference_team_start_admission
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_reference_storage import ReferenceCipher, supports_lead_reference_receiver
from bowhead_core.chat_references import (
    AgentResourceRef,
    CapabilityResourceRef,
    InputDocument,
    McpPromptResourceRef,
    McpSourceResourceRef,
    McpToolResourceRef,
    ReferenceNode,
)
from bowhead_core.skill_read_resolution import SkillReadSourceResolver
from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)
# System Agents an @-mention can start only under the lead-delegation.v1 contract.
LEAD_DELEGATION_ONLY_SYSTEM_AGENTS = frozenset({"agent-coach"})

_REASON_CODE_RE = re.compile(r"^[a-z0-9_.:-]{1,96}$")


def _rejection_chain(exc: BaseException) -> str:
    """Finite reason chain of a rejected start, for the server log only.

    The caller still gets one opaque ``resource_unavailable``. Preparation reports finite reason
    codes and re-raises with ``from None``, which hides the cause from a traceback but leaves it on
    ``__context__``. Anything that is not a plain reason code is reduced to its type, so request
    text and resource content never reach the log.
    """
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(parts) < 6:
        text = str(current)
        parts.append(f"{type(current).__name__}:{text}" if _REASON_CODE_RE.fullmatch(text) else type(current).__name__)
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def reference_initial_runtime_ready(request: Request) -> bool:
    """Local prerequisites only; released entry cells remain independently required."""
    if os.environ.get("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1") != "1":
        return False
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if not callable(getattr(orchestrator, "reference_skill_sources_factory", None)):
        return False
    store = getattr(orchestrator, "session_store", None)
    if store is None or store is not getattr(request.app.state, "session_store", None):
        return False
    try:
        ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", ""))
    except ValueError:
        return False
    return True


def reference_composer_authoring_ready(request: Request) -> bool:
    return reference_initial_runtime_ready(request) and initial_reference_authoring_supported()


def reference_mcp_writer_ready(
    request: Request,
    *,
    mcp_index: Any | None = None,
    mcp_access: Any | None = None,
) -> bool:
    """Require the same current MCP authority used by catalog and POST."""
    if os.environ.get("FEATURE_CHAT_REFERENCE_MCP", "0") != "1":
        return False
    from bowhead_core.access_control import AccessControlStore
    from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataIndex

    try:
        if mcp_index is None:
            cache = getattr(request.app.state, "reference_mcp_metadata", None)
            mcp_index = cache.current() if cache is not None else None
        if mcp_access is None:
            mcp_access = getattr(request.app.state, "access_control_store", None)
        store = getattr(getattr(request.app.state, "orchestrator", None), "session_store", None)
        return bool(
            type(mcp_index) is ReferenceMcpMetadataIndex
            and mcp_index.ready
            and type(mcp_access) is AccessControlStore
            and store is not None
            and Path(mcp_index.db_path).resolve() == Path(store.db_path).resolve()
            and Path(mcp_access.db_path).resolve() == Path(store.db_path).resolve()
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def reference_plan_writer_ready(request: Request) -> bool:
    return (
        os.environ.get("FEATURE_CHAT_REFERENCE_PLAN", "1") == "1"
        and os.environ.get("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "1") == "1"
        and os.environ.get("BOWHEAD_PLANNING_MODE", "1") == "1"
        and getattr(getattr(request.app.state, "orchestrator", None), "plan_store", None) is not None
        and getattr(getattr(request.app.state, "orchestrator", None), "multi_agent_registry", None) is not None
        and reference_initial_runtime_ready(request)
    )


def reference_initial_writer_ready(
    request: Request,
    document: InputDocument,
    *,
    entry_mode: EntryMode = "ordinary",
    required_skill_modes: tuple[str, ...] | None = None,
) -> bool:
    if entry_mode == "plan" and not reference_plan_writer_ready(request):
        return False
    if (
        any(isinstance(node, ReferenceNode) and isinstance(node.resource, CapabilityResourceRef) for node in document.nodes)
        and os.environ.get("FEATURE_CHAT_REFERENCE_CAPABILITY", "0") != "1"
    ):
        return False
    if any(
        isinstance(node, ReferenceNode) and isinstance(node.resource, McpSourceResourceRef | McpToolResourceRef | McpPromptResourceRef)
        for node in document.nodes
    ):
        if not reference_mcp_writer_ready(request):
            return False
    if any(isinstance(node, ReferenceNode) and isinstance(node.resource, McpPromptResourceRef) for node in document.nodes):
        cache = getattr(request.app.state, "reference_mcp_metadata", None)
        index = cache.current() if cache is not None else None
        if os.environ.get("FEATURE_CHAT_REFERENCE_MCP_PROMPT", "0") != "1" or not getattr(index, "prompt_cache_ready", False):
            return False
    return reference_initial_runtime_ready(request) and initial_reference_entry_supported(
        document,
        entry_mode=entry_mode,
        required_skill_modes=required_skill_modes,
    )


def _lead_delegation_writer_selected(
    document: InputDocument,
    *,
    entry_mode: EntryMode,
    has_attachments: bool = False,
    attachment_refs: list[dict] | None = None,
    automatic_receiver: AgentResourceRef | None = None,
) -> bool:
    """Keep the first new-contract writer cell narrower than the legacy adapter."""
    if entry_mode not in {"ordinary", "steer", "plan"} or os.environ.get("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "1") != "1":
        return False
    if has_attachments:
        from bowhead_core.chat_reference_attachment_snapshot import supports_lead_attachment_refs

        if not attachment_refs or not supports_lead_attachment_refs(attachment_refs, [ref.get("fileId") for ref in attachment_refs]):
            return False
    resources = [node.resource for node in document.nodes if isinstance(node, ReferenceNode)]
    agents = [resource for resource in resources if isinstance(resource, AgentResourceRef)]
    if agents:
        receiver = agents[0] if len(agents) == 1 and automatic_receiver is None else None
    else:
        receiver = automatic_receiver
    return supports_lead_reference_receiver(receiver, resources)


async def current_reference_lead_peers(
    request: Request,
    *,
    team_id: str,
    user_id: str,
    workspace_id: str,
) -> list[Any]:
    """Build the same current team/system roster used by a normal Lead turn."""
    return await current_reference_lead_peers_for_runtime(
        request.app.state.orchestrator,
        access_store=getattr(request.app.state, "access_control_store", None),
        user_store=getattr(request.app.state, "user_store", None),
        override_store=getattr(
            request.app.state,
            "builtin_subagent_override_store",
            None,
        ),
        team_id=team_id,
        user_id=user_id,
        workspace_id=workspace_id,
    )


async def current_reference_lead_peers_for_runtime(
    orchestrator: Any,
    *,
    access_store: Any,
    user_store: Any,
    override_store: Any,
    team_id: str,
    user_id: str,
    workspace_id: str,
) -> list[Any]:
    """Build the actor-scoped Lead roster without requiring an HTTP request."""
    from bowhead_core.agents.lead_orchestrator import _gather_lead_peers
    from bowhead_core.capability_resolver import resolve_unconfigured_capability_tools
    from bowhead_core.share_service import grants_for_viewer

    if access_store is None or user_store is None:
        raise ValueError("reference_receiver_access_unavailable")
    shared_grants = await grants_for_viewer(
        access_store,
        team_id=team_id,
        user_id=user_id,
        workspace_id=workspace_id,
        kind="team_subagent",
    )
    overrides = await override_store.list_for_team(team_id) if override_store is not None else None
    unconfigured_tools = await resolve_unconfigured_capability_tools(orchestrator, team_id)
    runner = getattr(orchestrator, "subagent_runner", None)
    return await _gather_lead_peers(
        registry=getattr(orchestrator, "multi_agent_registry", None),
        workspace_id=workspace_id,
        custom_store=None,
        user_id=user_id,
        team_id=team_id,
        team_subagent_store=getattr(runner, "team_subagent_store", None) or user_store,
        shared_grant_ids=shared_grants,
        unconfigured_capability_tools=unconfigured_tools,
        builtin_overrides=overrides,
    )


def require_supported_orchestration_contract(prepared: ReferenceStartAdmission, *, team_id: str) -> None:
    """Reject an @-mention whose Agent runs only through Lead's spawn handler under another contract.

    Coach is admitted, bound to its draft and given its Tools only by that
    handler; the legacy direct contract would skip all three. The code says
    what the member can change: ``coach_mention_unavailable`` when this
    deployment has the contract switched off (ask Coach on the Agent page), and
    ``coach_mention_unsupported`` when the message itself selected another
    contract (attached files, other references or a second Agent).
    """
    if prepared.profile_name in LEAD_DELEGATION_ONLY_SYSTEM_AGENTS and prepared.orchestration_contract != "lead-delegation.v1":
        deployment_disabled = os.environ.get("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "1") != "1"
        logger.warning(
            "reference_start.rejected code=%s reason=lead_delegation_required profile=%s team_id=%s",
            "coach_mention_unavailable" if deployment_disabled else "coach_mention_unsupported",
            prepared.profile_name,
            team_id,
        )
        if deployment_disabled:
            raise HTTPException(409, detail={"error": {"code": "coach_mention_unavailable"}})
        raise HTTPException(409, detail={"error": {"code": "coach_mention_unsupported"}})


async def prepare_reference_http_start(
    request: Request,
    document: InputDocument,
    *,
    team_id: str,
    user_id: str,
    workspace_id: str,
    requested_session_id: str | None,
    entry_mode: EntryMode = "ordinary",
    router: Any | None = None,
    trace_id: str = "",
    has_attachments: bool = False,
    attachment_refs: list[dict] | None = None,
    preferred_provider: str | None,
    preferred_model: str | None,
) -> ReferenceStartAdmission:
    if not reference_initial_writer_ready(request, document, entry_mode=entry_mode):
        raise HTTPException(409, detail={"error": {"code": "unsupported_entry"}})
    orchestrator = request.app.state.orchestrator
    store = orchestrator.session_store
    scope = ReferenceRequestScope(team_id=team_id, user_id=user_id, workspace_id=workspace_id, session_id=requested_session_id)
    try:
        sources = orchestrator.reference_skill_sources_factory(scope)
        if inspect.isawaitable(sources):
            sources = await sources
        if type(sources) is not SkillReadSourceResolver or Path(sources.db_path).resolve() != Path(store.db_path).resolve():
            raise ValueError("reference_skill_source_scope_changed")
        from bowhead_core.documents.package_io import OFFICE_TEXT_MIMES
        from bowhead_core.documents.upload_confirmation import confirmed_content_hash

        for ref in attachment_refs or []:
            if ref.get("mime") not in OFFICE_TEXT_MIMES:
                continue
            uploads = getattr(request.app.state, "artifact_store", None)
            if uploads is None:
                raise ValueError("reference_attachment_material_unavailable")
            artifact = await uploads.get(ref.get("fileId", ref.get("file_id")), check_created_by=user_id, reject_null_owner=True)
            if artifact is None:
                raise ValueError("reference_attachment_material_unavailable")
            await confirmed_content_hash(uploads, artifact, user_id=user_id, workspace_id=workspace_id)
        kwargs = {
            "input_document": document,
            "scope": scope,
            "sources": sources,
            "has_attachments": has_attachments,
        }
        mcp = [
            node
            for node in document.nodes
            if isinstance(node, ReferenceNode) and isinstance(node.resource, McpSourceResourceRef | McpToolResourceRef | McpPromptResourceRef)
        ]
        if mcp:
            from bowhead_core.access_control import AccessControlStore
            from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataIndex

            cache = getattr(request.app.state, "reference_mcp_metadata", None)
            mcp_index = cache.current() if cache is not None else None
            mcp_access = getattr(request.app.state, "access_control_store", None)
            if (
                type(mcp_index) is not ReferenceMcpMetadataIndex
                or type(mcp_access) is not AccessControlStore
                or Path(mcp_index.db_path).resolve() != Path(store.db_path).resolve()
                or Path(mcp_access.db_path).resolve() != Path(store.db_path).resolve()
            ):
                raise ValueError("reference_mcp_source_scope_changed")
            kwargs.update(mcp_index=mcp_index, mcp_access=mcp_access)
        agents = [node.resource for node in document.nodes if isinstance(node, ReferenceNode) and isinstance(node.resource, AgentResourceRef)]
        if agents:
            agent = agents[0]
            prepared = (
                await prepare_reference_system_start_admission(store.db_path, runner=orchestrator.subagent_runner, **kwargs)
                if agent.source_kind == "system"
                else await prepare_reference_team_start_admission(store.db_path, **kwargs)
            )
        else:
            from bowhead_core.reference_receiver_candidates import (
                prepare_reference_receiver_choice,
                read_reference_receiver_candidates,
                select_reference_receiver,
            )
            from bowhead_core.space_governance import SpaceOperation

            from bowhead_gateway.access_context import build_access_context_for_user_from_request

            access_store = getattr(request.app.state, "access_control_store", None)
            user_store = getattr(request.app.state, "user_store", None)
            if access_store is None or user_store is None:
                raise ValueError("reference_receiver_access_unavailable")
            access_context = await build_access_context_for_user_from_request(
                request,
                user_id=user_id,
                workspace_id=workspace_id,
                operation=SpaceOperation.EXECUTE,
                expected_team_id=team_id,
            )

            async def skill_access():
                return access_store, access_context

            peers = await current_reference_lead_peers(
                request,
                team_id=team_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )

            def current(name: str):
                cache = getattr(request.app.state, name, None)
                return cache.current() if cache is not None else None

            user = await user_store.get_user(user_id)
            candidates = await read_reference_receiver_candidates(
                store.db_path,
                input_document=document,
                scope=scope,
                lead_peers=peers,
                builtin_skill_index=getattr(request.app.state, "builtin_skill_reference_metadata", None),
                builtin_skill_access=skill_access,
                uploaded_skill_index=current("uploaded_skill_reference_metadata"),
                system_agent_index=current("system_agent_reference_metadata"),
                receiver_index=current("reference_receiver_metadata"),
                locale=getattr(user, "locale", None) or "en-US",
                has_attachments=has_attachments,
            )
            selected = await select_reference_receiver(
                candidates,
                task_text=document.text_content(),
                router=router,
                trace_id=trace_id,
                team_id=team_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            prepared = await prepare_reference_receiver_choice(
                store.db_path,
                candidates=candidates,
                resource=selected,
                input_document=document,
                scope=scope,
                sources=sources,
                runner=orchestrator.subagent_runner,
                has_attachments=has_attachments,
            )
        if entry_mode == "plan":
            import json

            from bowhead_core._db import open_shared_ctx
            from bowhead_core.chat_reference_plan import require_readonly_definition

            async with open_shared_ctx(store.db_path) as db:
                async with db.execute(
                    "SELECT definition_json FROM team_subagent_revision WHERE team_id=? AND subagent_id=? AND id=?",
                    (team_id, prepared.expected.agent_id, prepared.expected.revision_ref),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise ValueError("reference_plan_receiver_unavailable")
                from bowhead_core.team_plugin_store import expand_plugins_conn

                # Same as run admission: a disabled team Plugin no longer blocks the Agent while the switch is on.
                definition = await expand_plugins_conn(db, team_id=team_id, definition=json.loads(row[0]), ignore_deleted=True, allow_inactive=True)
                from bowhead_gateway.builtin_plugin_fields import expand_selected_builtin_plugins

                definition = expand_selected_builtin_plugins(definition, strict=False)
                require_readonly_definition(definition, orchestrator.tool_registry)
                async with db.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='chat_reference_plan_step_scope'") as cursor:
                    if await cursor.fetchone() is None:
                        raise ValueError("reference_plan_migration_required")
    except Exception as exc:
        # One opaque code for the caller, but never a silent one for the operator: every distinct
        # preparation failure used to look identical here and left no trace server-side.
        logger.warning(
            "reference_start.rejected code=resource_unavailable entry_mode=%s team_id=%s chain=%s",
            entry_mode,
            team_id,
            _rejection_chain(exc),
        )
        raise HTTPException(409, detail={"error": {"code": "resource_unavailable"}}) from None
    if not reference_initial_writer_ready(
        request,
        document,
        entry_mode=entry_mode,
        required_skill_modes=prepared.required_skill_modes,
    ):
        raise HTTPException(409, detail={"error": {"code": "unsupported_entry"}})
    if _lead_delegation_writer_selected(
        document,
        entry_mode=entry_mode,
        has_attachments=has_attachments,
        attachment_refs=attachment_refs,
        automatic_receiver=prepared.automatic_receiver,
    ):
        prepared = replace(prepared, orchestration_contract="lead-delegation.v1").snapshot()
    require_supported_orchestration_contract(prepared, team_id=team_id)
    return replace(prepared, require_released_entry=True)


async def resolve_reference_prompt_endpoint(*, db_path: str | Path, team_id: str, resource: McpPromptResourceRef, source_revision: str) -> str:
    """Resolve the registered anonymous endpoint at the worker's fetch boundary.

    The caller revalidates the exact worker, receiver and current access before
    each expansion. Rebuild this one connection's identity so stale metadata
    can never send arguments to a replacement URL or connection subject.
    """
    import aiosqlite
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.chat_references import decode_reference_json
    from bowhead_core.reference_mcp_metadata import _entries, _prompt, _source_resource

    # Writer flags stop new admissions. Accepted work still resolves current
    # registry authority and drains through the existing worker fence.
    try:
        exact = McpPromptResourceRef.model_validate(resource.model_dump(mode="python", warnings="error"))
        async with open_shared_ctx(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            async with db.execute(
                "SELECT id,kind,base_url,auth_type,updated_at,prompts_schema_json,selected_op_ids_json,tools_schema_json "
                "FROM team_tool_source WHERE team_id=? AND id=? AND status='enabled'",
                (team_id, exact.source_id),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None or row["kind"] != "mcp" or row["auth_type"] != "none" or row["updated_at"] != source_revision:
            raise ValueError("reference_mcp_prompt_source_changed")
        source, revision, _subject = _source_resource(
            team_id=team_id, source_id=exact.source_id, kind=row["kind"], base_url=row["base_url"], auth_type=row["auth_type"], entries=_entries(row)
        )
        raw = decode_reference_json(str(row["prompts_schema_json"]).encode(), max_bytes=2 * 1024 * 1024, max_depth=12)
        if not isinstance(raw, list) or len(raw) > 2000:
            raise ValueError("reference_mcp_prompt_source_changed")
        matches = [
            _prompt(value, source_id=exact.source_id, source_resource=source, local_revision=revision, source_revision=source_revision)
            for value in raw
            if isinstance(value, dict) and value.get("name") == exact.prompt_name
        ]
        if len(matches) != 1 or matches[0].resource != exact:
            raise ValueError("reference_mcp_prompt_source_changed")
        base_url = str(row["base_url"])
    except Exception:
        raise ValueError("reference_mcp_prompt_source_changed") from None
    return base_url
