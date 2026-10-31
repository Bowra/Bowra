"""Chat API（M1 Day 3 — asyncio fire-and-forget 版）.

- POST /send          → 创建 turn，**并发**启动 Orchestrator（不阻塞响应）
- GET  /stream/{id}   → SSE 订阅 turn 事件

历史修复：原版用 FastAPI `BackgroundTasks` 跑 Orchestrator，但
`BackgroundTask` 仅在 "response 发完后" 执行。在 ASGITransport + TestClient
的流式场景下，这会导致 SSE stream 的 `bus.subscribe()` 永远等不到事件而挂死。
换成 `asyncio.create_task` 让 Orchestrator 与 stream 并发执行。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import UUID, uuid4

from bowhead_core.agent_draft_send_target import AgentDraftSendTarget, agent_draft_target_from_client_info
from bowhead_core.chart.selected_context import ChartSelectedContext
from bowhead_core.chat_references import InputDocument
from bowhead_core.document_revision_target import DOCUMENT_REVISION_ADMISSION_ERRORS, DocumentRevisionTarget
from bowhead_core.i18n_flag import (
    is_i18n_enabled_for_workspace,
    is_paradigm_v2_enabled_for_workspace,
)
from bowhead_core.language_context import resolve_turn_language
from bowhead_core.model_clock import viewer_clock_scope
from bowhead_core.rate_card import credits_for_tokens
from bowhead_core.subagent_authoring_rollout import resolve_subagent_authoring_capabilities
from bowhead_core.team_language_policy import (
    KEY_TEAM_LANGUAGE_POLICY,
    parse_team_language_policy,
)
from bowhead_core.team_llm_config import (
    build_catalog,
    is_allowed_preferred_model,
    migrate_legacy_config_to_v2,
)
from bowhead_core.types import (
    LEGACY_TURN_WORKER_SETTLED_STATUSES,
    normalize_legacy_turn_status,
)
from bowhead_router.usage import usage_context
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import UUID4, AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sse_starlette.sse import EventSourceResponse

from bowhead_core import LanguageContext, Orchestrator, TurnRequest, get_event_registry
from bowhead_gateway.chat_action_identity import (
    ChatActionRequestIdentity,
    parse_chat_action_client_info,
    parse_chat_action_id,
)
from bowhead_gateway.chat_reference_request import REFERENCE_MEDIA_TYPE, ChatReferenceRoute, is_reference_media_type
from bowhead_gateway.chat_send_errors import (
    chat_send_http_error,
    chat_submission_receipt_http_error,
)
from bowhead_gateway.credit_admission import check_credit_admission
from bowhead_gateway.credit_admission import (
    record_credit_exhaustion_best_effort as _record_credit_exhaustion_best_effort,
)
from bowhead_gateway.datarc_context import (
    DatarcResolutionError,
    DatarcTurnContext,
    resolve_datarc_turn_context,
)
from bowhead_gateway.datarc_context import (
    resolve_team_datarc_client as _resolve_team_datarc_client,
)
from bowhead_gateway.datarc_context import (
    resolve_team_embed_client as _resolve_team_embed_client,
)
from bowhead_gateway.routes.chat_reference_catalog import router as reference_catalog_router
from bowhead_gateway.source_access_projection import (
    LiveEventProjectionCache,
    ViewerSourceProjectionError,
    project_event_payloads_for_viewer,
    project_live_event_payload_for_viewer,
)
from bowhead_gateway.terminal_protocol_rollout import (
    ChatExecutionWriteProtocol,
    PersistedChatExecutionProtocol,
    chat_execution_writer_readiness,
    durable_chat_execution_config,
    resolve_chat_execution_admission,
    resolve_persisted_chat_execution_protocol,
    terminal_v1_web_client_capable,
)
from bowhead_gateway.viewer_time_zone import viewer_time_zone_name
from bowhead_gateway.ws_authz import (
    authorize_ws_access,
    ws_membership_enforce_enabled,
)

if TYPE_CHECKING:
    from bowhead_core.terminal_protocol_store import WebChatActionLookup

router = APIRouter(route_class=ChatReferenceRoute)
router.include_router(reference_catalog_router, prefix="/references")
__all__ = [
    "DatarcResolutionError",
    "_resolve_team_datarc_client",
    "_resolve_team_embed_client",
    "router",
]

# Fire-and-forget task set (Python's recommended pattern: strong references prevent GC).
# WeakSet is insufficient: asyncio.Task needs a strong reference to stay alive.
_inflight_tasks: set[asyncio.Task[None]] = set()
_terminal_cleanup_tasks: set[asyncio.Task[None]] = set()
_MAX_CHAT_MESSAGE_LENGTH = 10_000
_MODULE_LOG = logging.getLogger("bowhead_gateway.chat")


class _PostFlightBillingState(StrEnum):
    VERIFIED_LEGACY = "verified_legacy"
    ABSENT_CONTRACT_V1 = "absent_contract_v1"
    CONTRACT_V2 = "contract_v2"
    MALFORMED = "malformed"
    FUTURE = "future"
    UNAVAILABLE = "unavailable"


_LEGACY_POST_FLIGHT_BILLING_STATES = frozenset(
    {
        _PostFlightBillingState.VERIFIED_LEGACY,
        _PostFlightBillingState.ABSENT_CONTRACT_V1,
    }
)


async def _persist_turn_row_for_stream(
    orchestrator: Orchestrator,
    req: TurnRequest,
    *,
    attachment_refs: list[dict[str, Any]] | None = None,
    source_parts: list[dict[str, Any]] | None = None,
) -> None:
    """Persist the Turn and canonical user source before exposing its stream."""
    session_store = getattr(orchestrator, "session_store", None)
    if session_store is None or not req.session_id:
        return
    if getattr(req, "terminal_runtime", None) is not None:
        return
    from bowhead_core.chat_turn_source import ChatTurnUserSource

    source = ChatTurnUserSource(
        turn_id=req.turn_id,
        session_id=req.session_id,
        user_id=req.user_id,
        workspace_id=req.workspace_id,
        user_text=req.text or "",
        attachment_ids=tuple(req.attachments),
        attachment_refs=tuple(attachment_refs or ()),
        parts=tuple(source_parts) if source_parts is not None else None,
        document_revision_target=req.document_revision_target,
        context_maintenance=req.context_maintenance,
    )
    await session_store.ensure_session(
        session_id=req.session_id,
        workspace_id=req.workspace_id or "",
        user_id=req.user_id or "",
    )
    await session_store.record_turn(
        turn_id=req.turn_id,
        session_id=req.session_id,
        user_text=req.text or "",
        user_source=source,
    )


_TERMINAL_ADMISSION_PUBLIC_CODES = frozenset(
    {
        "session_transitioning",
        "session_turn_active",
        "terminal_dependency_recovering",
        "terminal_protocol_admission_rejected",
        "terminal_protocol_future_version",
        "terminal_protocol_malformed",
        "terminal_protocol_writer_not_ready",
    }
)


def _terminal_admission_http_error(exc: Exception) -> HTTPException:
    """Project internal v1 admission failures onto a finite public contract."""

    from bowhead_core.context_action import ContextActionRejected

    if isinstance(exc, ContextActionRejected):
        return HTTPException(status_code=409, detail={"error": {"code": exc.code}})

    from bowhead_core.terminal_protocol import (
        TerminalProtocolFutureVersionError,
        TerminalProtocolMalformedError,
    )
    from bowhead_core.terminal_runtime_factory import (
        TerminalRuntimeAdmissionError,
    )

    if isinstance(exc, TerminalProtocolFutureVersionError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": {"code": "terminal_protocol_future_version"}},
        )
    if isinstance(exc, TerminalProtocolMalformedError):
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": {"code": "terminal_protocol_malformed"}},
        )
    if not isinstance(exc, TerminalRuntimeAdmissionError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "terminal_protocol_writer_not_ready"}},
        )

    code = exc.code
    if code in {
        "session_turn_active",
        "terminal_generation_already_claimed",
        "terminal_session_claim_changed",
    }:
        public_code = "session_turn_active"
    elif code == "session_transitioning":
        public_code = "session_transitioning"
    elif code == "terminal_steer_target_changed":
        # Exact target mismatch is proven before the steer transaction writes.
        # It is therefore safe for the web Composer to release this action.
        public_code = "session_turn_active"
    elif code in {
        "session_recovery_locked",
        "session_prior_effect_unknown",
        "session_deferred_pending",
        "terminal_admission_replay_pending",
        "terminal_admission_recovery_pending",
        "terminal_generation_attach_required",
        "terminal_generation_not_opened",
    }:
        public_code = "terminal_dependency_recovering"
    elif code in {
        "terminal_auth_fence_changed",
        "session_not_found",
        "session_access_denied",
        "session_deleted",
    }:
        public_code = "terminal_protocol_admission_rejected"
    elif code in {
        "terminal_protocol_future_version",
        "terminal_protocol_malformed",
    }:
        return HTTPException(
            status_code=(status.HTTP_409_CONFLICT if code == "terminal_protocol_future_version" else status.HTTP_500_INTERNAL_SERVER_ERROR),
            detail={"error": {"code": code}},
        )
    elif code.startswith(("session_", "terminal_admission_", "terminal_generation_")):
        public_code = "terminal_protocol_admission_rejected"
    else:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "terminal_protocol_writer_not_ready"}},
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": {"code": public_code}},
    )


def _reject_unconfigured_team_turn(team_router_applied: bool) -> bool:
    """团队没有可用 Router 时拒绝请求，绝不回退到进程级 Provider。"""
    return not team_router_applied


async def _caller_verified_email(request: Request) -> str | None:
    """ws-datarc-decouple ④: the caller's email IFF it is verified — else None.

    Uses a per-user identity so each member maps to their OWN datarc member. Returns
    None for unauthenticated / dev-stub-without-real-email / **unverified** (never resolve a
    datarc member from an unverified email — that would let an unverified address impersonate a
    real member). Best-effort: never raises into the turn."""
    from bowhead_gateway.routes.auth import _current_user

    try:
        user = await _current_user(request)
    except Exception:  # unauthenticated / dev-no-fallback → no per-user identity
        return None
    email = getattr(user, "email", None)
    if not email or not getattr(user, "email_verified", False):
        return None
    return email


async def _resolve_member_uid_first_hit(
    resolver: Any,
    identities: list[str],
    datarc_workspace_id: str,
    *,
    log: Any,
) -> str | None:
    """ws-datarc-decouple ④: resolve member_uid by trying ``identities`` in order (the caller's
    verified email first, then the global identity), returning the first hit.

    A miss / transient error on a non-last candidate falls through to the next. If the LAST
    candidate is **not found**, raise HTTP 400 (today's strict behavior for the configured
    identity); a transient error on the last → None so the caller drops to the env default.
    With ④ off, ``identities == [global]`` → exactly today's single-identity behavior."""
    from bowhead_gateway.member_resolver import MemberNotFoundError

    last = len(identities) - 1
    for i, ident in enumerate(identities):
        try:
            return await resolver.resolve(ident, datarc_workspace_id)
        except MemberNotFoundError as e:
            if i < last:
                log.info("member_resolve.identity_miss ws=%s → trying next identity", datarc_workspace_id)
                continue
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": {"code": "member_not_in_workspace"}},
            ) from e
        except Exception as e:
            log.warning("member_resolve_failed ws=%s err=%s", datarc_workspace_id, e)
            if i < last:
                continue
            return None
    return None


async def _resolve_datarc_member_uid(
    request: Request,
    *,
    resolver: Any,
    datarc_client: Any,
    datarc_ws: str,
    body_member_uid: str | None,
    ws_enforce: bool,
    log: Any,
) -> str:
    """Resolve the current user's memberUid through the current team connection.

    A client-supplied UID remains available only to unconstrained test/dev requests.
    Production data access always derives from the verified Bowhead identity; there is
    no deployment-wide identity or development UID fallback.
    """
    from bowhead_gateway.member_resolver import MemberNotFoundError

    # explicit client override — honored ONLY when ws access is not enforced (tests/dev)
    if not ws_enforce and body_member_uid:
        return body_member_uid

    if not datarc_ws:
        log.info("datarc.no_workspace — BI absent for this turn")
        return ""
    email = await _caller_verified_email(request)
    if not email:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"error": {"code": "datarc_identity_required"}},
        )
    if resolver is None or datarc_client is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "datarc_unavailable"}},
        )
    try:
        return await resolver.resolve(email, datarc_ws, datarc=datarc_client)
    except MemberNotFoundError as e:
        log.info("datarc.member_not_found email=%s ws=%s", email, datarc_ws)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"error": {"code": "datarc_member_not_found"}},
        ) from e
    except Exception as e:
        log.warning("datarc.member_resolve_failed ws=%s err=%s", datarc_ws, e)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "datarc_member_resolve_failed"}},
        ) from e


async def _resolve_optional_datarc_member(
    request: Request,
    datarc_context: DatarcTurnContext,
    *,
    body_member_uid: str | None = None,
    ws_enforce: bool,
    log: Any,
) -> tuple[DatarcTurnContext, str]:
    """Isolate Datarc identity failures from general task admission and resume."""
    from bowhead_bi_bridge import BIPermissionError

    try:
        member_uid = await _resolve_datarc_member_uid(
            request,
            resolver=getattr(request.app.state, "member_resolver", None),
            datarc_client=datarc_context.datarc_client,
            datarc_ws=datarc_context.datarc_project_for_calls,
            body_member_uid=body_member_uid,
            ws_enforce=ws_enforce,
            log=log,
        )
        return datarc_context, member_uid
    except HTTPException as exc:
        code = exc.detail.get("error", {}).get("code") if isinstance(exc.detail, dict) else None
        reasons = {
            "datarc_identity_required": "no_identity",
            "datarc_member_not_found": "member_unavailable",
            "datarc_unavailable": "transient",
            "datarc_member_resolve_failed": "transient",
        }
        if code not in reasons:
            raise
        reason = reasons[code]
        if isinstance(exc.__cause__, BIPermissionError) and exc.__cause__.source == "http":
            reason = "connection_rejected"
        log.info("datarc.task_continues_without_access code=%s reason=%s", code, reason)
        # Clear all query authority; a failed lookup must never reuse a saved member or data scope.
        return replace(
            datarc_context,
            connection_binding=None,
            active_datarc_project=None,
            datarc_project_for_calls="",
            active_datarc_workspace_name="",
            accessible_datarc_workspaces=(),
            datarc_client=None,
            vector_client=None,
            datarc_unavailable=True,
            datarc_absence_reason=reason,
        ), ""


def _make_datarc_context_switcher(
    request: Request,
    user_id: str,
    session_id: str,
    *,
    workspace_id: str,
    binding: Any,
    ws_enforce: bool,
    log: Any,
):
    """Bind the switch service to the executing Space's authorized Team connection."""
    from bowhead_gateway.datarc_switch import DatarcContextSwitcher

    return DatarcContextSwitcher(
        request.app,
        user_id=user_id,
        workspace_id=workspace_id,
        session_id=session_id,
        binding=binding,
        log=log,
    )


class UserProfileHint(BaseModel):
    """前端 localStorage 里的用户偏好，每次请求透传到 Lead prompt（#109）."""

    display_name: str | None = None
    language: str | None = None  # "auto" | "zh-CN" | "en-US" | "de-DE" | "fr-FR" (same UI/answer domain).
    answer_style: str | None = None  # "concise" | "detailed" | "technical" | "business"


class ChatAttachmentRef(BaseModel):
    """Lossless Composer attachment wire; runtime still consumes artifact IDs."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    file_id: str = Field(alias="fileId", min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=512)
    size: int = Field(ge=0)
    mime: str = Field(min_length=1, max_length=200)
    reference_kind: Literal["upload", "result"] | None = Field(
        default=None,
        alias="referenceKind",
    )
    selected_context: ChartSelectedContext | None = Field(default=None, alias="selectedContext")
    bundle_artifact_ids: list[str] | None = Field(
        default=None,
        alias="bundleArtifactIds",
        max_length=100,
    )


def _runtime_attachment_ids(refs: list[ChatAttachmentRef]) -> list[str]:
    """Flatten bundle children and stable-dedupe for runtime injection."""
    ids: list[str] = []
    for ref in refs:
        ids.extend(ref.bundle_artifact_ids or [ref.file_id])
    return list(dict.fromkeys(ids))


CONTINUE_CLIENT_KEY = "continue_checkpoints"
_CHECKPOINT_ID_RE = re.compile(r"^rsc_[0-9a-f]{20}$")


def continue_checkpoint_ids(client_info: dict[str, str]) -> list[str]:
    """Checkpoint ids a Continue click asked to resume (client_info JSON list, at most four)."""
    raw = client_info.get(CONTINUE_CLIENT_KEY)
    if raw is None:
        return []
    try:
        value = json.loads(raw) if len(raw) <= 1024 else None
    except ValueError:
        value = None
    if not isinstance(value, list) or not 1 <= len(value) <= 4 or not all(isinstance(item, str) and _CHECKPOINT_ID_RE.match(item) for item in value):
        raise ValueError("continue_checkpoints must list one to four checkpoint ids")
    return list(dict.fromkeys(value))


from bowhead_core.context_action import ContextMaintenanceAction


class ChatSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    workspace_id: str
    member_uid: str | None = None
    # Sprint chat-redesign-2026-05-21 Phase 1.1 (P0-7): min_length relaxed from
    # 1 → 0 so the L3-picker path (empty message + attachments) is reachable.
    # Handler still rejects (empty AND no attachments) with HTTP 400.
    message: str = ""
    input_document: InputDocument | None = None
    document_revision_target: DocumentRevisionTarget | None = None
    context_maintenance: ContextMaintenanceAction | None = None
    attachments: list[str] = Field(default_factory=list, max_length=100)
    attachment_refs: list[ChatAttachmentRef] | None = Field(default=None, max_length=32)
    client_info: dict[str, str] = Field(default_factory=dict)
    user_profile: UserProfileHint | None = None
    # M2.2: user's Composer model-picker (provider, model) passes through TurnRequest to
    # routing_resolver's user_pref priority tier (M2.3). See docs/M157-routing-user-pref-design.md.
    preferred_provider: str | None = None
    preferred_model: str | None = None
    # Planning Mode (BOWHEAD_PLANNING_MODE) — composer mode selector. "planning"
    # forces the plan card via hook1 (should_propose_plan); None/"off" = Auto
    # (keyword-triggered) or disabled. Ignored when the backend flag is OFF.
    planning_mode: Literal["auto", "planning", "off"] | None = None
    # Planning Mode track D — when set (+ flag on), this send is an *approve*:
    # route through chat/send's vetted identity/authz/credit/bus/spawn, then run
    # the plan via hook2 (TurnRequest.plan_execution_id) instead of a user turn.
    plan_execution_id: str | None = Field(default=None, max_length=128)
    expected_plan_revision: int | None = Field(default=None, strict=True, ge=1)
    # Composer batch A additive exactly-once contract. A completely absent
    # contract remains the byte-compatible legacy path.
    client_submission_id: UUID4 | None = None
    submission_kind: Literal["start", "replay", "steer"] | None = None
    submission_contract_version: Literal["chat-submit.v1", "chat-model.v2", "chat-model-effort.v3", "chat-references.v1"] | None = None
    model_control_batch: Literal["a", "b", "c", "references"] | None = None
    expected_parent_turn_id: str | None = None
    expected_execution_generation: int | None = Field(default=None, ge=1)
    replay_source_turn_id: str | None = None

    @model_validator(mode="after")
    def validate_submission_contract(self) -> ChatSendRequest:
        if self.expected_plan_revision is not None and not (self.plan_execution_id or "").strip():
            raise ValueError("expected_plan_revision requires plan_execution_id")
        if "input_document" in self.model_fields_set and self.model_control_batch != "references":
            raise ValueError("input_document requires the reference submission contract")
        action_identity = parse_chat_action_client_info(
            self.client_info,
            session_id=self.session_id,
        )
        draft_id = self.client_info.get("agent_draft_id")
        from bowhead_core.configuration_settings_target import SETTINGS_TARGET_CLIENT_KEY, WorkspaceSettingsSendTarget

        settings_target = self.client_info.get(SETTINGS_TARGET_CLIENT_KEY)
        if settings_target is not None:
            if len(settings_target) > 4096:
                raise ValueError("settings target exceeds its size limit")
            WorkspaceSettingsSendTarget.model_validate_json(settings_target)
            if (
                draft_id
                or self.plan_execution_id
                or self.planning_mode == "planning"
                or self.replay_source_turn_id
                or self.submission_kind == "steer"
                or self.model_control_batch == "references"
                or action_identity is not None
            ):
                raise ValueError("settings target requires an ordinary Web start or exact replay")
        from bowhead_core.coach_canvas_target import COACH_CANVAS_TARGET_KEY, CoachCanvasSendTarget

        resource_target = self.client_info.get(COACH_CANVAS_TARGET_KEY)
        if self.context_maintenance is not None:
            if (
                not self.session_id
                or self.message
                or self.attachments
                or self.attachment_refs
                or self.input_document
                or self.document_revision_target
                or draft_id
                or settings_target
                or resource_target
                or self.plan_execution_id
                or self.planning_mode not in (None, "off")
                or self.replay_source_turn_id
                or CONTINUE_CLIENT_KEY in self.client_info
                or self.submission_kind in ("steer", "replay")
                or self.model_control_batch == "references"
                or (action_identity is None and self.client_submission_id is None)
                or (action_identity is not None and action_identity.action.kind != "start")
            ):
                raise ValueError("context maintenance requires an idle scoped start without business input")
        if resource_target is not None:
            if len(resource_target) > 4096:
                raise ValueError("resource target exceeds its size limit")
            CoachCanvasSendTarget.model_validate_json(resource_target)
            if (
                draft_id
                or settings_target
                or self.plan_execution_id
                or self.planning_mode == "planning"
                or self.replay_source_turn_id
                or self.submission_kind in {"steer", "replay"}
                or self.model_control_batch == "references"
                or action_identity is not None
            ):
                raise ValueError("resource target requires an ordinary Web start or exact submission recovery")
        if self.document_revision_target is not None:
            if (
                self.session_id != self.document_revision_target.session_id
                or self.client_submission_id is None
                or self.submission_kind != "start"
                or self.model_control_batch == "references"
                or "input_document" in self.model_fields_set
                or self.attachments
                or self.attachment_refs
                or draft_id
                or settings_target
                or resource_target
                or action_identity is not None
                or self.plan_execution_id
                or self.planning_mode == "planning"
                or self.replay_source_turn_id
            ):
                raise ValueError("document revision requires an ordinary scoped start without competing inputs")
        if CONTINUE_CLIENT_KEY in self.client_info:
            continue_checkpoint_ids(self.client_info)
            if (
                draft_id
                or settings_target
                or resource_target
                or self.plan_execution_id
                or self.planning_mode == "planning"
                or self.replay_source_turn_id
                or self.submission_kind in {"steer", "replay"}
                or self.model_control_batch == "references"
                or self.document_revision_target is not None
                or (action_identity is not None and action_identity.action.kind != "start")
            ):
                raise ValueError("continue requires an ordinary Web start")
        draft_target = agent_draft_target_from_client_info(self.client_info)
        if draft_target is not None and (
            self.plan_execution_id
            or self.planning_mode == "planning"
            or self.replay_source_turn_id
            or self.submission_kind in {"steer", "replay"}
            or self.model_control_batch == "references"
        ):
            raise ValueError("draft targets require an ordinary start or same-action recovery")
        if action_identity is not None and self.client_submission_id is not None:
            raise ValueError("chat action identity cannot be mixed with a submission id")
        if action_identity is not None and self.attachments:
            if action_identity.action.kind == "steer":
                raise ValueError("Terminal steer attachments are not supported")
            if self.attachment_refs is None:
                raise ValueError("Terminal start attachments require typed refs")
        contract_values = (
            self.submission_kind,
            self.submission_contract_version,
            self.model_control_batch,
            self.expected_parent_turn_id,
            self.expected_execution_generation,
            self.replay_source_turn_id,
        )
        if self.attachment_refs is not None and self.attachments != _runtime_attachment_ids(self.attachment_refs):
            raise ValueError("ambiguous attachments")
        if self.client_submission_id is None:
            if any(value is not None for value in contract_values):
                raise ValueError("submission contract requires client_submission_id")
            return self
        if self.submission_kind is None or self.submission_contract_version is None or self.model_control_batch is None:
            raise ValueError("client_submission_id requires a complete submission contract")
        expected_pair = {
            "a": "chat-submit.v1",
            "b": "chat-model.v2",
            "c": "chat-model-effort.v3",
            "references": "chat-references.v1",
        }[self.model_control_batch]
        if self.submission_contract_version != expected_pair:
            raise ValueError("submission contract version/batch mismatch")
        if self.model_control_batch == "references":
            if self.submission_kind == "replay":
                if self.model_fields_set & {"message", "attachments", "attachment_refs", "input_document"}:
                    raise ValueError("reference replay accepts only its stored input source")
            else:
                if self.input_document is None or self.message != self.input_document.display_text():
                    raise ValueError("reference input requires a matching display message")
                self.input_document.validate_task(has_attachments=bool(self.attachments))
        if self.submission_kind == "start":
            if any(
                value is not None
                for value in (
                    self.expected_parent_turn_id,
                    self.expected_execution_generation,
                    self.replay_source_turn_id,
                )
            ):
                raise ValueError("start submission contains kind-exclusive fields")
        elif self.submission_kind == "replay":
            if (
                not self.replay_source_turn_id
                or self.expected_parent_turn_id is not None
                or self.expected_execution_generation is not None
                or self.message
                or self.attachments
                or self.attachment_refs is not None
            ):
                raise ValueError("invalid replay submission contract")
        elif (
            not self.session_id
            or not self.expected_parent_turn_id
            or self.expected_execution_generation is None
            or self.replay_source_turn_id is not None
            or self.preferred_provider is not None
            or self.preferred_model is not None
            or self.planning_mode is not None
            or self.plan_execution_id is not None
        ):
            raise ValueError("invalid steer submission contract")
        return self

    # NOTE 2026-05-19: `prior_turn_id` field removed — the Lead context never
    # read TurnRequest.prior_turn_id (audit grep confirmed zero consumers). FE
    # continued sending it for "continuity" but the value was discarded.
    # Real HITL continuity flows through /clarifications/{id}/resume_turn
    # (action="turn_resume") shipped Sprint chat-v4-streaming Phase 4C (0.26.0).
    # If a future "refinement of prior turn" semantic is needed, restore field
    # AND add a Lead prompt reader before re-shipping.


class ChatSendResponse(BaseModel):
    context_maintenance: ContextMaintenanceAction | None = Field(default=None, exclude_if=lambda value: value is None)
    document_revision_target: DocumentRevisionTarget | None = Field(default=None, exclude_if=lambda value: value is None)
    agent_draft_target: AgentDraftSendTarget | None = Field(default=None, exclude_if=lambda value: value is None)
    session_id: str
    turn_id: str
    stream_url: str
    # Sprint chat-redesign-2026-05-21 Phase 1.1 (P0-6): "started" (fresh turn),
    # "buffered" (steered into active turn's buffer) or "completed" (target turn
    # is no longer active). Historical submission receipt replay may still carry
    # "cancelled"; new /chat/send writes never create that outcome.
    status: str = "started"
    execution_generation: int = 1
    # Persisted Turn discriminator. Legacy responses omit the field so old
    # clients retain their exact response shape; v1 clients select the strict
    # event-id reducer from this value rather than a mutable feature flag.
    terminal_protocol_version: int | None = None


class ChatActionLookupResponse(BaseModel):
    context_maintenance: ContextMaintenanceAction | None = Field(default=None, exclude_if=lambda value: value is None)
    agent_draft_target: AgentDraftSendTarget | None = Field(default=None, exclude_if=lambda value: value is None)
    action_id: str
    action_kind: Literal["start", "steer"]
    outcome: Literal["admitted", "accepted"]
    session_id: str
    turn_id: str
    execution_generation: int
    terminal_protocol_version: Literal[1] = 1
    stream_url: str


def _record_composer_send_result(
    *,
    body: ChatSendRequest,
    attachment_count: int,
    result: Literal["success", "rejected", "server_error"],
    request: Request,
) -> None:
    """Record one server-owned terminal result; receipt replays bypass this helper."""
    if getattr(request.state, "composer_send_result_recorded", False):
        return
    request.state.composer_send_result_recorded = True
    from bowhead_core import metrics

    surface = body.client_info.get("composer_surface")
    if surface not in {"home", "workbench"}:
        surface = "workbench" if body.session_id else "home"
    labels = {
        "result": result,
        "surface": surface,
    }
    try:
        metrics.inc(
            "composer_send_result_total",
            labels=labels,
            help="Composer send outcomes by result and surface",
        )
        if attachment_count:
            metrics.inc(
                "composer_attachment_action_total",
                labels={
                    "result": result,
                },
                help="Composer attachment-bearing send outcomes",
            )
    except Exception:
        _MODULE_LOG.exception("composer_send_metric_failed result=%s", result)


class SubmissionAbandonRequest(BaseModel):
    workspace_id: str
    requested_session_id: str | None = None


class QueuedReferenceCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    requested_session_id: str


class QueuedReferenceResourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["agent", "skill", "capability", "mcp_source", "mcp_tool", "mcp_prompt"]
    label: Annotated[str, Field(min_length=1, max_length=256)]


class QueuedReferenceSubmissionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    client_submission_id: UUID4
    display_text: Annotated[str, Field(max_length=262_144)]
    resources: Annotated[list[QueuedReferenceResourceResponse], Field(max_length=8)]
    attachment_count: Annotated[int, Field(ge=0, le=100)]
    accepted_at: AwareDatetime


class QueuedReferenceQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[QueuedReferenceSubmissionResponse]


def _submission_fingerprint(
    body: ChatSendRequest,
    *,
    user_id: str,
    team_id: str,
    auth_session_generation: str,
) -> tuple[str, str, tuple[str, ...]]:
    """HMAC the normalized semantic wire; never persist raw prompt data."""
    from bowhead_core.composer_rollout import submission_fingerprint_keyring

    keyring = submission_fingerprint_keyring()
    if not keyring:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "submission_fingerprint_unavailable"}},
        )
    projection = {
        "scope": {
            "team_id": team_id,
            "user_id": user_id,
            "auth_session_generation": auth_session_generation,
            "workspace_id": body.workspace_id,
            "requested_session_id": body.session_id,
        },
        "contract": {
            "kind": body.submission_kind,
            "version": body.submission_contract_version,
            "batch": body.model_control_batch,
            "expected_parent_turn_id": body.expected_parent_turn_id,
            "expected_execution_generation": body.expected_execution_generation,
            "replay_source_turn_id": body.replay_source_turn_id,
        },
        "content": {
            "message": body.message,
            "attachments": body.attachments,
            "attachment_refs": (
                [ref.model_dump(by_alias=True, exclude_none=True) for ref in body.attachment_refs] if body.attachment_refs is not None else None
            ),
            "preferred_provider": body.preferred_provider,
            "preferred_model": body.preferred_model,
            "planning_mode": body.planning_mode,
            "plan_execution_id": body.plan_execution_id,
            "client_info": body.client_info,
            "user_profile": body.user_profile.model_dump() if body.user_profile else None,
        },
    }
    if body.document_revision_target is not None:
        projection["content"]["document_revision_target"] = body.document_revision_target.model_dump(mode="json")
    if body.context_maintenance is not None:
        projection["content"]["context_maintenance"] = body.context_maintenance.model_dump()
    if body.expected_plan_revision is not None:
        projection["content"]["expected_plan_revision"] = body.expected_plan_revision
    domain = b"bowhead/chat-submission-fingerprint/semantic-wire-v1\0"
    if body.model_control_batch == "references":
        # Display labels and editor node IDs are not semantic execution input.
        projection["content"]["message"] = body.input_document.text_content() if body.input_document else ""
        projection["content"]["input_document"] = body.input_document.semantic_projection() if body.input_document else None
        domain = b"bowhead/chat-submission-fingerprint/semantic-wire-references-v1\0"
    canonical = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    fingerprints = tuple(
        hmac.new(
            key,
            domain + canonical,
            hashlib.sha256,
        ).hexdigest()
        for _key_id, key in keyring
    )
    return keyring[0][0], fingerprints[0], fingerprints


async def _require_current_submission_auth_generation(
    request: Request,
    *,
    store: Any,
    user_id: str,
    allow_dev_fallback: bool = True,
) -> str:
    """Reject stale login chains before receipt replay or claim mutation."""
    from bowhead_gateway.routes.auth import (
        _auth_session_generation,
        _auth_session_generation_hash,
        _dev_user_fallback_enabled,
    )

    generation_hash = _auth_session_generation_hash(_auth_session_generation(request))
    if allow_dev_fallback and _dev_user_fallback_enabled(request):
        register = getattr(store, "register_chat_auth_session_generation", None)
        if callable(register):
            await register(
                user_id=user_id,
                auth_session_generation_hash=generation_hash,
            )
    is_active = getattr(store, "is_chat_auth_session_generation_active", None)
    if not callable(is_active):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "submission_store_unavailable"}},
        )
    if not await is_active(
        user_id=user_id,
        auth_session_generation_hash=generation_hash,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "auth_session_generation_inactive"}},
        )
    return generation_hash


async def _validate_chat_attachment_refs(
    request: Request,
    *,
    refs: list[ChatAttachmentRef],
    runtime_ids: list[str],
    user_id: str,
    workspace_id: str,
    session_id: str | None,
) -> list[dict[str, Any]]:
    """Revalidate uploads and stable session results through separate fences."""
    from bowhead_gateway.chat_attachment_access import (
        ChatAttachmentAccessError,
        validate_chat_attachment_access,
    )

    artifact_store = getattr(request.app.state, "artifact_store", None)
    wire_refs = [ref.model_dump(by_alias=True, exclude_none=True) for ref in refs]
    try:
        await validate_chat_attachment_access(
            artifact_store=artifact_store,
            refs=wire_refs,
            runtime_ids=runtime_ids,
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
        )
    except ChatAttachmentAccessError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": {"code": exc.code}},
        ) from exc
    return wire_refs


async def _resolve_web_datarc_identity(
    request: Request,
    *,
    team_id: str | None,
    workspace_id: str,
    user_id: str,
    session_id: str,
    body_member_uid: str | None,
    ws_enforce: bool,
    read_only: bool = False,
    log: Any,
) -> tuple[Any, str]:
    """Resolve the Datarc query target and member snapshot once per Web start."""

    datarc_context = await resolve_datarc_turn_context(
        request,
        team_id=team_id,
        workspace_id=workspace_id,
        user_id=user_id,
        session_id=session_id,
        read_only=read_only,
        log=log,
    )
    if read_only and (datarc_context.datarc_unavailable or datarc_context.datarc_absence_reason in {"transient", "connection_rejected"}):
        raise DatarcResolutionError("Datarc snapshot could not be verified")
    if not read_only:
        return await _resolve_optional_datarc_member(
            request,
            datarc_context,
            body_member_uid=body_member_uid,
            ws_enforce=ws_enforce,
            log=log,
        )
    member_uid = await _resolve_datarc_member_uid(
        request,
        resolver=getattr(request.app.state, "member_resolver", None),
        datarc_client=datarc_context.datarc_client,
        datarc_ws=datarc_context.datarc_project_for_calls,
        body_member_uid=body_member_uid,
        ws_enforce=ws_enforce,
        log=log,
    )
    return datarc_context, member_uid


async def _resolve_web_language_inputs(
    request: Request,
    body: ChatSendRequest,
    *,
    team_id: str | None = None,
    require_snapshot_reads: bool = False,
    log: Any,
) -> tuple[
    dict[str, str] | None,
    dict[str, str],
    LanguageContext | None,
    bool,
]:
    """Resolve the shared profile/settings language projection for Web starts."""

    profile_dict: dict[str, str] | None = None
    if body.user_profile is not None:
        profile_dict = {key: value for key, value in body.user_profile.model_dump().items() if value and value != "auto"} or None

    user_settings_kv: dict[str, str] = {}
    workspace_settings_kv: dict[str, str] = {}
    user_settings_store = getattr(request.app.state, "user_settings_store", None)
    workspace_settings_store = getattr(
        request.app.state,
        "workspace_settings_store",
        None,
    )
    if user_settings_store is not None:
        try:
            from bowhead_gateway.routes.auth import resolve_user_id_sync as _ruid

            user_settings_kv = await user_settings_store.list_for_user(
                _ruid(request),
            )
        except Exception as error:
            log.warning("user_settings_store.list_failed err=%s", error)
            if require_snapshot_reads:
                raise
    if workspace_settings_store is not None:
        try:
            workspace_settings_kv = await workspace_settings_store.list_for_workspace(
                body.workspace_id,
            )
        except Exception as error:
            log.warning(
                "workspace_settings_store.list_failed ws=%s err=%s",
                body.workspace_id,
                error,
            )
            if require_snapshot_reads:
                raise

    # Answer style uses the same precedence as language: request first, then account settings (ui_language has
    # this two-tier shape; see language_context.py's opening priority table). Settings writes server-side
    # user_settings.answer_style, but frontend turn submission reads localStorage bowhead.user_profile.
    # **No repository writer stores answer_style in that blob**
    # (its sole writer locale-switcher.tsx writes only language). Without account fallback here,
    # successfully saved preferences never take effect. Do not validate values here: consumer UserProfileHint.answer_style
    # is also a free string (no Literal); tightening would lock out existing account values.
    _stored_answer_style = (user_settings_kv.get("answer_style") or "").strip()
    if _stored_answer_style and not (profile_dict or {}).get("answer_style"):
        profile_dict = {**(profile_dict or {}), "answer_style": _stored_answer_style}

    # 2026-08-18: is_i18n_enabled_for_workspace became **default-on** (opt-out), so
    # the old unconfigured-flag + explicit-language-choice patch condition dissolves; it existed
    # to bypass default-off. Consolidate the criterion instead of maintaining two halves.
    # ⚠ Rollback unchanged: global BOWHEAD_I18N_V1_KILL=1 or workspace i18n_v1=false.
    language_context: LanguageContext | None = None
    # 2026-08-18: is_i18n_enabled_for_workspace became **default-on** (opt-out), so
    # the old unconfigured-flag + explicit-language-choice patch condition dissolves; it existed
    # to bypass default-off. Consolidate the criterion instead of maintaining two halves.
    # ★ Its final version also expanded once (spawn-language review P2-3): output_language alone
    #   must count as an explicit choice, or output-only users lacked language_context and the entire peer
    #   language-fallback injection failed. **Default-on strictly covers that requirement**: these users now have context.
    # ⚠ Rollback unchanged: global BOWHEAD_I18N_V1_KILL=1 or workspace i18n_v1=false.
    if is_i18n_enabled_for_workspace(workspace_settings_kv):
        # Unset answer preferences and explicit `follow_input` follow this question's
        # language (docs/design/language-policy.md). The preference decision, whether
        # to detect the question language, and the final resolution stay together in
        # `resolve_turn_language`, shared by web and channels; this route only gathers
        # inputs. The team policy must be read here too, or a team follow_input
        # policy would resolve with no carrier language.
        _team_lang_policy = await _load_team_language_policy(request, team_id, log=log)
        from bowhead_core.effective_language import channel_message_language

        # A question without reliable language evidence ("1", "ok", "SKU TOP10")
        # falls back to the language this session last answered in, so the
        # language does not jump mid-session. Use the resolver's own detector,
        # and read the session only then (a pure function, cheaper than a query).
        _session_last: str | None = None
        if not channel_message_language(body.message or ""):
            _session_last = await _resolve_session_last_output_language(request, body.session_id, log=log)
        language_context = resolve_turn_language(
            user_settings=user_settings_kv,
            workspace_settings=workspace_settings_kv,
            team_language_policy=_team_lang_policy,
            accept_language=request.headers.get("accept-language"),
            user_profile_language=(
                body.user_profile.language if body.user_profile is not None and body.user_profile.language else request.cookies.get("NEXT_LOCALE")
            ),
            message_text=body.message or "",
            session_last_language=_session_last,
        )
        # The viewer's browser zone sets the model's "today" for this turn and every
        # resume/continuation of it (bowhead_core.model_clock); missing/unknown = deploy clock.
        language_context = language_context.with_time_zone(viewer_time_zone_name(request))
    return (
        profile_dict,
        workspace_settings_kv,
        language_context,
        is_paradigm_v2_enabled_for_workspace(workspace_settings_kv),
    )


async def _resolve_session_last_output_language(
    request: Request,
    session_id: str | None,
    *,
    log: Any,
) -> str | None:
    """本会话上一轮**实际交付**的语言；取不到 → None（回落 ui，= 批7 之前的行为）。

    判据取的是上一轮 `final_answer` 的载体语言，而不是"上一轮解析出的设置值"——
    用户看到的是前者，跟着它走才不会出现"上一句英文答、这一句中文答"。
    往回找最多 3 轮：报错轮 / 纯画布轮的 `final_answer` 可能为空。

    读路径不因这一层兜底打断对话：任何异常都只记日志。
    """
    if not session_id:
        return None
    orchestrator = getattr(request.app.state, "orchestrator", None)
    store = getattr(orchestrator, "session_store", None)
    if store is None:
        return None
    try:
        turns = await store.list_turns(session_id=session_id, limit=3)
    except Exception as error:  # noqa: BLE001 — Fallback must not prevent turn startup.
        log.warning("session_last_language.load_failed session=%s err=%s", session_id, type(error).__name__)
        return None
    from bowhead_core.effective_language import session_follow_language_of_markdown

    for turn in reversed(turns):
        answer = (getattr(turn, "final_answer", "") or "").strip()
        if not answer:
            continue
        # Classifying **model output** needs overwhelming evidence: old criteria misclassified entity-dense English as Chinese
        # (two real counterexamples in observed_language's docstring), reversing the next turn's language.
        # German / French answers are told apart from English so a bare "1" keeps following them.
        language = session_follow_language_of_markdown(answer)
        if language:
            return language
    return None


async def _load_team_language_policy(
    request: Request,
    team_id: str | None,
    *,
    log: Any,
) -> dict[str, Any]:
    """读本团队的输出语言策略；没配 / 无 team / 读失败 → {}（= 沿用用户自己的设置）。

    读路径不因一行团队配置打断对话：策略缺失时的行为正是"没有策略"的行为。
    """
    if not team_id:
        return {}
    store = getattr(request.app.state, "team_settings_store", None)
    if store is None:
        return {}
    try:
        raw = await store.get(team_id, KEY_TEAM_LANGUAGE_POLICY)
    except Exception as error:
        log.warning("team_language_policy.load_failed team=%s err=%s", team_id, error)
        return {}
    return parse_team_language_policy(raw)


async def _resolve_effective_model_preference(
    request: Request,
    *,
    team_id: str | None,
    preferred_provider: str | None,
    preferred_model: str | None,
    require_snapshot_reads: bool = False,
    log: Any,
) -> tuple[str | None, str | None]:
    """Apply the existing team catalog validation to one requested model pair."""

    if not preferred_provider and not preferred_model:
        return preferred_provider, preferred_model
    team_llm_cfg: dict[str, Any] = {}
    team_llm_version = 0
    if team_id:
        user_store = getattr(request.app.state, "user_store", None)
        if user_store is not None:
            try:
                team_llm_cfg, team_llm_version = await user_store.get_team_llm_config_with_version(team_id)
                if team_llm_cfg:
                    team_llm_cfg = migrate_legacy_config_to_v2(team_llm_cfg)
            except Exception as error:
                log.warning(
                    "llm_pref.config_load_failed team=%s err=%s",
                    team_id,
                    error,
                )
                if require_snapshot_reads:
                    raise
                team_llm_cfg, team_llm_version = {}, 0
    if preferred_provider or preferred_model:
        try:
            catalog = build_catalog(
                config=team_llm_cfg,
                version=team_llm_version,
            )
            if not is_allowed_preferred_model(
                catalog,
                preferred_provider,
                preferred_model,
            ):
                log.info(
                    "llm_pref.invalid_ignored team=%s provider=%s model=%s",
                    team_id,
                    preferred_provider,
                    preferred_model,
                )
                return None, None
        except Exception as error:
            log.warning("llm_pref.validation_failed err=%s", error)
            if require_snapshot_reads:
                raise
            return None, None
    return preferred_provider, preferred_model


def _chat_action_request_conflict() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": {
                "code": "terminal_protocol_admission_rejected",
            }
        },
    )


def _optional_snapshot_text(value: object) -> str | None:
    """Read one admitted text value; anything else cannot prove parity."""

    if value is not None and not isinstance(value, str):
        raise TypeError("admitted resolution snapshot value is not text")
    return value


async def _replay_model_preference(
    request: Request,
    body: ChatSendRequest,
    *,
    admitted_resolution: Mapping[str, object],
    team_id: str | None,
    log: Any,
) -> tuple[str | None, str | None]:
    """Return the model pair a duplicate start compares against its admission.

    Admission persists either the requested pair (accepted by the team catalog)
    or ``(None, None)`` (catalog rejected it). A retry carrying exactly the
    persisted pair is the same request, so it uses the snapshot and never reads
    the team catalog: a team admin removing or changing models after admission
    cannot turn the identical retry into a conflict. Any other requested pair is
    either a changed request or one admission ignored as invalid; only the
    catalog can tell, so it falls back to the read-only validation used before
    snapshots (read failures still fail closed).
    """

    admitted_provider = _optional_snapshot_text(admitted_resolution["preferred_provider"])
    admitted_model = _optional_snapshot_text(admitted_resolution["preferred_model"])
    if (body.preferred_provider, body.preferred_model) == (admitted_provider, admitted_model):
        return admitted_provider, admitted_model
    return await _resolve_effective_model_preference(
        request,
        team_id=team_id,
        preferred_provider=body.preferred_provider,
        preferred_model=body.preferred_model,
        require_snapshot_reads=True,
        log=log,
    )


async def _replay_datarc_identity(
    request: Request,
    body: ChatSendRequest,
    *,
    admitted_resolution: Mapping[str, object],
    team_id: str | None,
    user_id: str,
    session_id: str,
    log: Any,
) -> tuple[str | None, str]:
    """Return the Datarc project and member uid a duplicate start compares against.

    Both are resolved by the server at admission from the team connection, the
    user's workspace ladder and Datarc membership, so replay uses the persisted
    snapshot and performs no Datarc call. The only client input is an explicit
    ``member_uid`` override; when the body carries one that differs from the
    snapshot it is either a changed request or an override admission ignored
    under workspace enforcement, so the member uid falls back to the read-only
    resolution used before snapshots.
    """

    admitted_project = _optional_snapshot_text(admitted_resolution["active_datarc_project"])
    admitted_member_uid = admitted_resolution["member_uid"]
    if not isinstance(admitted_member_uid, str):
        raise TypeError("admitted member uid snapshot is not text")
    if not body.member_uid or body.member_uid == admitted_member_uid:
        return admitted_project, admitted_member_uid
    _datarc_context, member_uid = await _resolve_web_datarc_identity(
        request,
        team_id=team_id,
        workspace_id=body.workspace_id,
        user_id=user_id,
        session_id=session_id,
        body_member_uid=body.member_uid,
        ws_enforce=ws_membership_enforce_enabled(),
        read_only=True,
        log=log,
    )
    return admitted_project, member_uid


async def _replay_existing_web_action(
    body: ChatSendRequest,
    request: Request,
    *,
    identity: ChatActionRequestIdentity,
    lookup: WebChatActionLookup,
    user_id: str,
    team_id: str | None,
    text: str,
    session_id: str,
    effective_attachment_ids: list[str],
    log: Any,
) -> ChatSendResponse:
    """Verify the complete same-action digest before replaying its canonical fact."""

    if team_id != lookup.scope.team_id or session_id != lookup.scope.session_id or body.workspace_id != lookup.workspace_id:
        raise _chat_action_request_conflict()
    if lookup.action_kind == "steer":
        # The durable ACK proves acceptance but intentionally does not expose the
        # request digest.  A repeated POST therefore cannot prove payload parity
        # after the buffer is drained; canonical recovery uses the GET lookup.
        raise _chat_action_request_conflict()
    if lookup.paradigm_v2 is None:
        # Rows written before the reader-first R0.4a rollout cannot prove
        # whether paradigm_v2 was true or false at admission. GET remains the
        # canonical recovery path; repeated POST must not claim semantic parity.
        raise _chat_action_request_conflict()

    from bowhead_core.terminal_protocol_store import (
        canonical_admission_request_digest,
    )
    from bowhead_core.terminal_runtime_factory import (
        web_start_execution_intent,
    )

    if lookup.action_kind == "start":
        if body.client_info.get("agent_draft_dirty") == "1":
            raise _chat_action_request_conflict()
        if ((body.plan_execution_id or "").strip() or None) != lookup.plan_execution_id:
            raise _chat_action_request_conflict()
        if body.expected_plan_revision != lookup.expected_plan_revision:
            raise _chat_action_request_conflict()
        replay_planning_mode = "planning" if lookup.planning_mode is True else "off" if lookup.planning_mode is False else lookup.planning_mode
        # Whether this turn may auto-propose a plan is part of its persisted
        # intent: fresh admission records "off" while the planning flag is off.
        # A flag-off client cannot send a mode, so its exact retry (None) matches
        # that "off" only while the flag is still off; once planning is back on
        # the same body would newly allow auto planning and must conflict.
        server_forced_off_still_off = replay_planning_mode == "off" and body.planning_mode is None and not _planning_enabled()
        if body.planning_mode != replay_planning_mode and not server_forced_off_still_off:
            raise _chat_action_request_conflict()
        effective_attachment_refs = (
            [ref.model_dump(by_alias=True, exclude_none=True) for ref in body.attachment_refs] if body.attachment_refs is not None else []
        )
        admitted_resolution = lookup.admitted_resolution
        if admitted_resolution is None:
            # Without the persisted model/Datarc snapshot the retry cannot be
            # compared against what admission resolved; like absent paradigm_v2
            # evidence, POST stays non-definitive and GET recovers the action.
            raise _chat_action_request_conflict()
        try:
            # Language, paradigm_v2, model selection and Datarc identity are
            # not re-resolved from current server state: replay compares them
            # against the snapshot persisted at first admission (see the
            # intent overrides below and the two helpers). Only differing
            # client-supplied values fall back to a read-only resolution.
            preferred_provider, preferred_model = await _replay_model_preference(
                request,
                body,
                admitted_resolution=admitted_resolution,
                team_id=team_id,
                log=log,
            )
            active_datarc_project, member_uid = await _replay_datarc_identity(
                request,
                body,
                admitted_resolution=admitted_resolution,
                team_id=team_id,
                user_id=user_id,
                session_id=session_id,
                log=log,
            )
        except Exception as error:
            raise _chat_action_request_conflict() from error
        from bowhead_core.chat_turn_source import ChatTurnUserSource

        source = ChatTurnUserSource(
            turn_id=lookup.scope.turn_id,
            session_id=lookup.scope.session_id,
            user_id=user_id,
            workspace_id=body.workspace_id,
            user_text=text,
            attachment_ids=tuple(effective_attachment_ids),
            attachment_refs=tuple(effective_attachment_refs),
            parts=None,
            context_maintenance=body.context_maintenance,
        )
        # team-router-rule: rebuild only to recompute admission digest; never execute
        # (web_start_execution_intent does not read team_router; adding it cannot affect the digest).
        replay_request = TurnRequest(
            context_maintenance=body.context_maintenance,
            session_id=lookup.scope.session_id,
            workspace_id=body.workspace_id,
            user_id=user_id,
            member_uid=member_uid,
            text=text,
            turn_id=lookup.scope.turn_id,
            attachments=effective_attachment_ids,
            language_context=None,
            preferred_provider=preferred_provider,
            preferred_model=preferred_model,
            active_datarc_project=active_datarc_project,
            planning_mode=None,
            paradigm_v2=lookup.paradigm_v2,
            agent_draft_target=agent_draft_target_from_client_info(body.client_info),
        )
        candidate_intent = web_start_execution_intent(
            replay_request,
            source,
        )
        # Feature flags constrain fresh admission only. A persisted action must
        # replay from its immutable effective intent across rollback/config
        # changes; otherwise the same action id becomes a false conflict.
        candidate_intent["planning_mode"] = lookup.planning_mode
        # Language and paradigm_v2 are derived server state (account/team/space
        # settings, flags, resolution rules), not client payload. Compare the
        # retry against the snapshot persisted at first admission so a deploy
        # or flag flip in between cannot turn the same request into a conflict.
        # paradigm_v2 comes from lookup.paradigm_v2 (never None here: legacy
        # envelopes without it were already rejected above).
        candidate_intent["language_context"] = dict(lookup.language_context) if lookup.language_context is not None else None
        # The zone carried beside a missing language snapshot is first-admission state too.
        candidate_intent.pop("viewer_time_zone", None)
        if lookup.viewer_time_zone:
            candidate_intent["viewer_time_zone"] = lookup.viewer_time_zone
        if lookup.plan_execution_id is not None:
            candidate_intent["plan_execution_id"] = lookup.plan_execution_id
        if lookup.expected_plan_revision is not None:
            candidate_intent["expected_plan_revision"] = lookup.expected_plan_revision
        candidate_digest = canonical_admission_request_digest(
            execution_intent=candidate_intent,
            request_id=identity.action.action_id,
            session_id=lookup.scope.session_id,
            team_id=lookup.scope.team_id,
            turn_id=lookup.scope.turn_id,
            user_text=text,
        )
        if candidate_digest != lookup.request_digest:
            raise _chat_action_request_conflict()

    return ChatSendResponse(
        agent_draft_target=AgentDraftSendTarget.model_validate(lookup.agent_draft_target) if lookup.agent_draft_target else None,
        context_maintenance=ContextMaintenanceAction.model_validate(lookup.context_maintenance) if lookup.context_maintenance is not None else None,
        session_id=lookup.scope.session_id,
        turn_id=lookup.scope.turn_id,
        stream_url=f"/api/v1/chat/stream/{lookup.scope.turn_id}",
        status=("started" if lookup.action_kind == "start" else "buffered"),
        execution_generation=lookup.scope.generation,
        terminal_protocol_version=1,
    )


async def _record_credit_deduct_failed_metric_best_effort(
    metrics_log: Any | None,
    *,
    team_id: str,
    user_id: str,
    workspace_id: str | None,
    turn_id: str | None,
    deduct_reason: str,
    cost_credits: int,
    tokens_input: int,
    tokens_output: int,
    credit_exhaustion_event: dict[str, Any] | None = None,
) -> None:
    """Record post-flight deduct failures without affecting turn cleanup."""

    if metrics_log is None:
        return
    try:
        await metrics_log.track(
            "credit.deduct_failed",
            user_id=user_id,
            attrs={
                "team_id": team_id,
                "workspace_id": workspace_id,
                "turn_id": turn_id,
                "deduct_reason": deduct_reason,
                "cost_credits": cost_credits,
                "tokens_input": tokens_input,
                "tokens_output": tokens_output,
                "credit_exhaustion_event_id": (credit_exhaustion_event or {}).get("id"),
            },
        )
    except Exception as e:
        import logging as _logging

        _logging.getLogger("bowhead_gateway.chat").warning(
            "credit.deduct_failed_metric_record_failed turn=%s reason=%s err=%s",
            turn_id,
            deduct_reason,
            str(e)[:200],
        )


async def _resolve_postflight_billing_state(
    terminal_runtime: Any,
    req: Any,
    *,
    team_id: str,
) -> _PostFlightBillingState:
    """Route legacy billing from persisted generation truth, never by fallback."""

    if terminal_runtime is None:
        return _PostFlightBillingState.VERIFIED_LEGACY

    from bowhead_core.terminal_effect_contract import (
        EffectExecutionContract,
        EffectIntentDisposition,
    )
    from bowhead_core.terminal_protocol import (
        TerminalProtocolFutureVersionError,
        TerminalProtocolMalformedError,
        TerminalScope,
    )

    scope = getattr(terminal_runtime, "scope", None)
    if not isinstance(scope, TerminalScope):
        return _PostFlightBillingState.MALFORMED
    if (
        scope.team_id != team_id
        or scope.session_id != getattr(req, "session_id", None)
        or scope.turn_id != getattr(req, "turn_id", None)
        or scope.generation != getattr(req, "execution_generation", None)
    ):
        return _PostFlightBillingState.MALFORMED

    store = getattr(terminal_runtime, "store", None)
    read_contract = getattr(store, "read_effect_execution_contract", None)
    if not callable(read_contract):
        return _PostFlightBillingState.UNAVAILABLE
    try:
        contract = await read_contract(scope)
    except TerminalProtocolFutureVersionError:
        return _PostFlightBillingState.FUTURE
    except TerminalProtocolMalformedError:
        return _PostFlightBillingState.MALFORMED
    except Exception:
        return _PostFlightBillingState.UNAVAILABLE

    if not isinstance(contract, EffectExecutionContract) or contract != getattr(
        terminal_runtime,
        "effect_contract",
        None,
    ):
        return _PostFlightBillingState.MALFORMED
    if contract.disposition is EffectIntentDisposition.LEGACY:
        return _PostFlightBillingState.ABSENT_CONTRACT_V1
    if contract.disposition is EffectIntentDisposition.RECOVERY_ONLY:
        return _PostFlightBillingState.CONTRACT_V2
    return _PostFlightBillingState.MALFORMED


async def _billable_turn_usage(
    orchestrator: Any,
    terminal_runtime: Any,
    result: Any,
    *,
    turn_id: str,
) -> tuple[int, int]:
    """Use the frozen cancelled projection, never post-Stop provider tail."""

    tokens_input = int(result.tokens_input or 0)
    tokens_output = int(result.tokens_output or 0)
    if terminal_runtime is None:
        return tokens_input, tokens_output
    from bowhead_core.terminal_protocol import TerminalStatus

    receipt = terminal_runtime.bus.receipt
    if receipt is None or receipt.status is not TerminalStatus.CANCELLED:
        return tokens_input, tokens_output
    try:
        persisted_turn = await orchestrator.session_store.get_turn(turn_id)
    except Exception as billing_read_error:
        _MODULE_LOG.warning(
            "credit.cancelled_usage_read_failed turn=%s error=%s",
            turn_id,
            type(billing_read_error).__name__,
        )
        return 0, 0
    if persisted_turn is None or persisted_turn.status != "cancelled":
        return 0, 0
    try:
        return (
            max(0, int(persisted_turn.tokens_input or 0)),
            max(0, int(persisted_turn.tokens_output or 0)),
        )
    except (AttributeError, TypeError, ValueError) as billing_projection_error:
        _MODULE_LOG.warning(
            "credit.cancelled_usage_projection_invalid turn=%s error=%s",
            turn_id,
            type(billing_projection_error).__name__,
        )
        return 0, 0


async def _subagent_draft_turn_overrides(
    request: Request,
    *,
    team_id: str,
    team_router: Any | None,
) -> dict[str, Any]:
    """Per-turn draft runtime for the chat creator trigger.

    Conversational drafting is on whenever authoring is; it needs the team
    model to generate. Without one, or when the runtime is unavailable, the
    trigger falls back to the manual card with a purpose hint.
    """
    if team_router is None or not resolve_subagent_authoring_capabilities(team_id).creator:
        return {}
    try:
        from bowhead_gateway._entitlement_gate import is_feature_licensed_request
        from bowhead_gateway.subagent_draft_runtime import build_subagent_draft_service

        return {
            "subagent_draft_service": build_subagent_draft_service(
                app_state=request.app.state,
                router=team_router,
            ),
            "subagent_draft_mcp_entitled": await is_feature_licensed_request(request, "mcp"),
        }
    except Exception as exc:
        # Drafting is an optional authoring surface. A missing runtime must
        # not break unrelated chat turns.
        _MODULE_LOG.warning(
            "subagent_draft.runtime_unavailable team=%s err=%s",
            team_id,
            type(exc).__name__,
        )
        return {}


def _get_orchestrator(request: Request) -> Orchestrator:
    orc: Orchestrator | None = getattr(request.app.state, "orchestrator", None)
    if orc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "orchestrator_not_ready"}},
        )
    return orc


# C1: move _planning_enabled into shared planning_flags (previously duplicated in plans.py+chat.py).
from bowhead_gateway.routes.planning_flags import (  # noqa: E402
    planning_enabled as _planning_enabled,
)


def _reference_rejected_before_admission(exc: HTTPException) -> HTTPException:
    """Mark a reference preparation refusal as definitive: no Turn, dispatch or receipt was written.

    The same 409 codes are also raised after a failed Turn persist, where the Turn may have committed;
    the client may release its local submission journal only on this marker, never on the code alone.
    """
    detail = exc.detail
    error = detail.get("error") if isinstance(detail, dict) else None
    if exc.status_code != 409 or not isinstance(error, dict) or set(error) != {"code"}:
        return exc
    code = error["code"]
    return HTTPException(409, detail={"error": {"code": code, "admission": "not_started"}}, headers=exc.headers)


async def _read_reference_submission(body: ChatSendRequest, request: Request) -> ChatSendResponse | None:
    """Recover existing receipts before checking the initial adapter declaration.

    Returning None permits exact preparation in the common send pipeline;
    it supplies no resource authority and never permits plaintext fallback.
    """
    from bowhead_core.space_governance import SpaceOperation

    from bowhead_gateway.routes.auth import _auth_session_generation, _current_user

    if not is_reference_media_type(request):
        raise HTTPException(status_code=415, detail={"error": {"code": "reference_input_media_type_required"}})
    user = await _current_user(request)
    await _require_submission_space_access(
        request,
        user_id=user.id,
        workspace_id=body.workspace_id,
        operation=SpaceOperation.READ_HISTORY,
        expected_team_id=user.team_id or "",
        allow_dev_fallback=False,
    )
    store = getattr(request.app.state, "session_store", None)
    if store is None or not callable(getattr(store, "get_chat_submission_receipt", None)):
        raise HTTPException(status_code=503, detail={"error": {"code": "submission_store_unavailable"}})
    await _require_current_submission_auth_generation(request, store=store, user_id=user.id, allow_dev_fallback=False)
    _, _, fingerprints = _submission_fingerprint(
        body, user_id=user.id, team_id=user.team_id or "", auth_session_generation=_auth_session_generation(request)
    )
    receipt = await store.get_chat_submission_receipt(
        team_id=user.team_id or "",
        user_id=user.id,
        client_submission_id=str(body.client_submission_id),
        workspace_id=body.workspace_id,
        requested_session_id=body.session_id,
    )
    if receipt is not None:
        request.state.composer_send_result_recorded = True
        if receipt.request_fingerprint not in fingerprints:
            raise HTTPException(status_code=409, detail={"error": {"code": "submission_id_conflict"}})
        if receipt.outcome == "rejected":
            raise chat_submission_receipt_http_error(receipt.response, fallback_code="submission_rejected")
        return ChatSendResponse.model_validate(receipt.response)
    from bowhead_gateway.chat_reference_start import reference_initial_writer_ready

    entry_mode = "steer" if body.submission_kind == "steer" else "plan" if body.planning_mode == "planning" else "ordinary"
    attachments_supported = not body.attachments or body.attachment_refs is not None
    if (
        body.submission_kind in {"start", "steer"}
        and body.input_document is not None
        and attachments_supported
        and body.plan_execution_id is None
        and (body.planning_mode in (None, "off") or (body.planning_mode == "planning" and body.submission_kind == "start" and not body.attachments))
        and reference_initial_writer_ready(
            request,
            body.input_document,
            entry_mode=entry_mode,
        )
    ):
        return None
    # No receipt exists (checked above) and nothing has been written for this submission.
    raise _reference_rejected_before_admission(HTTPException(status_code=409, detail={"error": {"code": "unsupported_entry"}}))


async def _send_impl(
    body: ChatSendRequest,
    request: Request,
    response: Response,
) -> ChatSendResponse:
    import logging as _lg

    reference_mode = body.model_control_batch == "references"
    reference_start = None
    if reference_mode:
        recovered = await _read_reference_submission(body, request)
        if recovered is not None:
            return recovered

    _log = _lg.getLogger("bowhead_gateway.chat")
    action_identity: ChatActionRequestIdentity | None = parse_chat_action_client_info(
        body.client_info,
        session_id=body.session_id,
    )
    dev_teamless_fallback = False
    if action_identity is not None:
        from bowhead_gateway.routes.auth import _current_user

        current_user = await _current_user(request)
        resolved_user_id = current_user.id
        resolved_team_id = current_user.team_id
    else:
        from bowhead_gateway.routes.auth import (
            _dev_user_fallback_enabled,
            resolve_user_or_stub,
        )

        resolved_user_id, resolved_team_id = await resolve_user_or_stub(request)
        dev_teamless_fallback = bool(resolved_team_id is None and _dev_user_fallback_enabled(request))
    client_declared_terminal_reader = terminal_v1_web_client_capable(
        request.headers,
    )
    if action_identity is not None and not client_declared_terminal_reader:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": {"code": "client_refresh_required"}},
        )
    existing_action: WebChatActionLookup | None = None
    if action_identity is not None:
        existing_action = await _read_web_chat_action(
            request,
            user_id=resolved_user_id,
            team_id=resolved_team_id,
            identity=action_identity,
        )
        if existing_action is not None:
            # This resolves an already-persisted action, not a new send result.
            # Wake the canonical start independently of whether this POST body
            # can prove semantic parity; waking never adopts the new body.
            request.state.composer_send_result_recorded = True
            if existing_action.action_kind == "start":
                terminal_factory = getattr(
                    request.app.state,
                    "terminal_runtime_factory",
                    None,
                )
                wake = getattr(
                    terminal_factory,
                    "wake_admitted_web_turn",
                    None,
                )
                if callable(wake):
                    try:
                        await wake(existing_action.scope)
                    except Exception as wake_error:
                        _log.warning(
                            "terminal.action_replay_wake_failed turn=%s err=%s",
                            existing_action.scope.turn_id,
                            type(wake_error).__name__,
                        )
        if existing_action is None and action_identity.action.kind == "steer" and (body.plan_execution_id or "").strip():
            # A steer never executes a plan (plan approval is a start action).
            # Since 23fd993f1 the field slipped through here: erased when planning
            # is off, or carried into plan admission when it is on. Reject it
            # whatever the flag, like the submission-contract steer validator.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error": {"code": "request_validation_failed"}},
            )
        if existing_action is None and action_identity.action.kind == "start" and body.session_id is None:
            _home_config = durable_chat_execution_config()
            _home_readiness = chat_execution_writer_readiness(
                getattr(
                    request.app.state,
                    "terminal_writer_service",
                    None,
                ),
                config=_home_config,
            )
            _home_decision = resolve_chat_execution_admission(
                config=_home_config,
                team_id=resolved_team_id,
                session_facts=None,
                client_capable=client_declared_terminal_reader,
                action_identity_complete=True,
                require_action_identity=True,
                existing_writer_ready=(_home_readiness.existing_writer_ready),
                fresh_writer_ready=_home_readiness.fresh_writer_ready,
            )
            if _home_decision.write_protocol is ChatExecutionWriteProtocol.REFRESH_REQUIRED:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"error": {"code": "client_refresh_required"}},
                )
            if _home_decision.write_protocol is not ChatExecutionWriteProtocol.TERMINAL_V1:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "error": {
                            "code": "terminal_protocol_writer_not_ready",
                        }
                    },
                )
    orchestrator = None if existing_action is not None else _get_orchestrator(request)
    # Fresh start and persisted-v1 steer share request gates but diverge before
    # TurnRequest construction: steer must never create a second Turn.
    _terminal_v1_admission_enabled = bool(action_identity is not None and action_identity.action.kind == "start")
    _terminal_v1_action_steer_enabled = bool(action_identity is not None and action_identity.action.kind == "steer")
    if len(body.message) > _MAX_CHAT_MESSAGE_LENGTH:
        if existing_action is not None:
            raise _chat_action_request_conflict()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": {"code": "message_too_long"}},
        )
    text = body.message if reference_mode else body.message.strip()
    # Planning Mode track D — an approve routes through chat/send with
    # plan_execution_id set (+ flag on): run the already-approved plan via hook2,
    # not a fresh user turn. Flag-gated; `_plan_exec_id` is "" when the flag is OFF
    # or the field is unset → every approve branch below is inert (normal sends
    # are byte-identical to before).
    _supplied_plan_exec_id = (body.plan_execution_id or "").strip()
    _planning_is_enabled = _planning_enabled()
    _plan_exec_id = _supplied_plan_exec_id if _planning_is_enabled else ""
    if (
        existing_action is None
        and (body.expected_plan_revision is not None or (action_identity is not None and action_identity.action.kind == "start"))
        and not _planning_is_enabled
        and (_supplied_plan_exec_id or body.planning_mode is not None)
    ):
        # A stale client must not create an action whose planning fields are
        # silently erased; future exact replay could not prove equivalence.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": {"code": "client_refresh_required"}},
        )
    _effective_text_plan_id = existing_action.plan_execution_id if existing_action is not None else _plan_exec_id
    if _effective_text_plan_id and not text:
        text = "(执行已批准的计划)"  # synthetic user text; hook2 ignores it

    session_id = body.session_id or (
        action_identity.action.home_session_id if action_identity is not None and action_identity.action.kind == "start" else f"s_{uuid4().hex[:12]}"
    )
    effective_attachment_ids = list(body.attachments)
    effective_attachment_refs = (
        [ref.model_dump(by_alias=True, exclude_none=True) for ref in body.attachment_refs] if body.attachment_refs is not None else []
    )
    effective_source_parts: list[dict[str, Any]] | None = None

    if existing_action is not None:
        assert action_identity is not None
        return await _replay_existing_web_action(
            body,
            request,
            identity=action_identity,
            lookup=existing_action,
            user_id=resolved_user_id,
            team_id=resolved_team_id,
            text=text,
            session_id=session_id,
            effective_attachment_ids=effective_attachment_ids,
            log=_log,
        )

    if body.client_info.get("agent_draft_id") and body.client_info.get("agent_draft_dirty") == "1":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": {"code": "subagent_draft_unsaved_changes", "admission": "not_started"}},
        )

    draft_target = agent_draft_target_from_client_info(body.client_info)
    draft_editor = None
    if draft_target is not None:
        from bowhead_gateway.agent_draft_send_target import admit_agent_draft_target

        draft_editor = await admit_agent_draft_target(
            request, draft_target, user_id=resolved_user_id, team_id=resolved_team_id or "", workspace_id=body.workspace_id
        )

    from bowhead_core.configuration_settings_target import SETTINGS_TARGET_CLIENT_KEY, WorkspaceSettingsSendTarget

    _configuration_settings_target = None
    if SETTINGS_TARGET_CLIENT_KEY in body.client_info:
        from bowhead_gateway.configuration_settings_target import admit_workspace_settings_target

        _configuration_settings_target = await admit_workspace_settings_target(
            request,
            WorkspaceSettingsSendTarget.model_validate_json(body.client_info[SETTINGS_TARGET_CLIENT_KEY]),
            user_id=resolved_user_id,
            team_id=resolved_team_id or "",
            workspace_id=body.workspace_id,
        )

    from bowhead_core.coach_canvas_target import COACH_CANVAS_TARGET_KEY, CoachCanvasSendTarget

    _coach_canvas_target = None
    if COACH_CANVAS_TARGET_KEY in body.client_info:
        from bowhead_gateway.coach_canvas_target import admit_coach_canvas_target

        _coach_canvas_target = await admit_coach_canvas_target(
            request,
            CoachCanvasSendTarget.model_validate_json(body.client_info[COACH_CANVAS_TARGET_KEY]),
            user_id=resolved_user_id,
            team_id=resolved_team_id or "",
            workspace_id=body.workspace_id,
            session_id=body.session_id or "",
        )

    assert orchestrator is not None
    # Sprint chat-redesign-2026-05-21 Phase 1.1 (P0-7): empty text is fine IF
    # attachments present (drives L3 picker path). Reject only when BOTH empty.
    if not text and not body.attachments and body.submission_kind != "replay" and body.context_maintenance is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "empty_message"}},
        )

    # feedback-0713 F2: soft-deleted sessions (R1 report-deletion linkage) reject new turns with 410 Gone.
    # Before steering interception: steering/approval must not revive deleted sessions. Decided boundary:
    # reject without revival; see turn_service.is_session_deleted docstring.
    if body.session_id:
        from bowhead_gateway.turn_service import is_session_deleted

        if await is_session_deleted(orchestrator, body.session_id):
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={"error": {"code": "session_deleted"}},
            )

    # P4.5.10 + R7.2 [review P1-a]: resolve the caller (JWT → user/team) and AUTHORIZE workspace
    # access BEFORE any datarc touch (project mapping / member resolver). Else, with
    # BOWHEAD_WS_MEMBERSHIP_ENFORCE=1, an unauthorized request would probe cross-workspace datarc
    # state and could leak the datarc project id via the member_not_in_workspace error. No JWT →
    # dev-stub fallback. (P4.5.11: first send also auto-enrolls when enforce is OFF.)
    _ws_enforce = ws_membership_enforce_enabled() or reference_mode or body.document_revision_target is not None
    _user_store_authz = getattr(request.app.state, "user_store", None)

    # A supplied, already-existing Session is an authority boundary, not a
    # convenient identifier.  Resolve it before the legacy first-send
    # auto-enrolment path so a removed member cannot resurrect their access by
    # steering/cancelling an active v1 Turn while the writer switch is off.
    # The terminal store repeats actor/team/active-turn checks transactionally;
    # this route check also protects every legacy shortcut and avoids any
    # mutation before an ownership mismatch is rejected.
    _existing_session_for_send = None
    _session_protocol_facts = None
    _managed_protocol_at_authority_read = False
    _authority_active_turn_id: str | None = None
    _authority_active_protocol_version: int | None = None
    _authority_active_generation: int | None = None
    if body.session_id:
        _session_authority_reader = getattr(
            orchestrator.session_store,
            "get_session",
            None,
        )
        try:
            if callable(_session_authority_reader):
                _existing_session_for_send = await _session_authority_reader(session_id)
        except Exception as _session_lookup_error:
            _log.warning(
                "chat.session_authority_lookup_failed sid=%s err=%s",
                session_id,
                type(_session_lookup_error).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": {"code": "authz_lookup_failed"}},
            ) from _session_lookup_error
        if _existing_session_for_send is not None and (
            _existing_session_for_send.user_id != resolved_user_id or _existing_session_for_send.workspace_id != body.workspace_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": {"code": "access_context_denied"}},
            )
        if _existing_session_for_send is not None:
            if not resolved_team_id:
                if not dev_teamless_fallback:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={"error": {"code": "authz_lookup_failed"}},
                    )
            else:
                if _user_store_authz is None:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={"error": {"code": "authz_lookup_failed"}},
                    )
                try:
                    authority_team_id = await _user_store_authz.get_workspace_team_id(
                        body.workspace_id,
                    )
                    authority_member_role = await _user_store_authz.get_member_role(
                        body.workspace_id,
                        resolved_user_id,
                    )
                except Exception as authority_error:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={"error": {"code": "authz_lookup_failed"}},
                    ) from authority_error
                if authority_team_id != resolved_team_id or not authority_member_role:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={"error": {"code": "access_context_denied"}},
                    )
                _protocol_facts_reader = getattr(
                    getattr(
                        request.app.state,
                        "terminal_protocol_store",
                        None,
                    ),
                    "read_session_protocol_facts",
                    None,
                )
                if callable(_protocol_facts_reader):
                    try:
                        _session_protocol_facts = await _protocol_facts_reader(
                            team_id=resolved_team_id,
                            actor=resolved_user_id,
                            workspace_id=body.workspace_id,
                            session_id=session_id,
                        )
                    except Exception as _protocol_facts_error:
                        from bowhead_gateway.terminal_protocol_http import (
                            terminal_protocol_http_error,
                        )

                        raise terminal_protocol_http_error(
                            _protocol_facts_error,
                        ) from _protocol_facts_error
                    if _session_protocol_facts is None:
                        raise HTTPException(
                            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"error": {"code": "authz_lookup_failed"}},
                        )
                    if _session_protocol_facts.v1_active_claim is not None:
                        _authority_active_turn_id = _session_protocol_facts.v1_active_claim.turn_id
                        _authority_active_protocol_version = 1
                        _authority_active_generation = _session_protocol_facts.v1_active_claim.generation
                    elif _session_protocol_facts.legacy_active_turn_id is not None:
                        _authority_active_turn_id = _session_protocol_facts.legacy_active_turn_id
                    _managed_protocol_at_authority_read = bool(
                        _session_protocol_facts.has_v1_history,
                    )
                elif (
                    getattr(
                        request.app.state,
                        "legacy_session_lifecycle_allowed",
                        None,
                    )
                    is not True
                ):
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={"error": {"code": "authz_lookup_failed"}},
                    )
    if _terminal_v1_action_steer_enabled:
        assert action_identity is not None
        try:
            authority_protocol = resolve_persisted_chat_execution_protocol(
                _authority_active_protocol_version,
            )
        except Exception as protocol_error:
            raise _terminal_admission_http_error(
                protocol_error,
            ) from protocol_error
        if (
            _existing_session_for_send is None
            or _authority_active_turn_id != action_identity.expected_turn_id
            or authority_protocol is not PersistedChatExecutionProtocol.TERMINAL_V1
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": {"code": "session_turn_active"}},
            )
        if _authority_active_generation != action_identity.expected_execution_generation:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": {"code": "client_refresh_required"}},
            )
    if _terminal_v1_admission_enabled and _authority_active_turn_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": {"code": "session_turn_active"}},
        )

    _durable_chat_config = durable_chat_execution_config()
    _terminal_writer_service = getattr(
        request.app.state,
        "terminal_writer_service",
        None,
    )
    if (
        _session_protocol_facts is not None
        and _session_protocol_facts.has_v1_history
        and client_declared_terminal_reader
        and action_identity is not None
    ):
        _ensure_existing_writer = getattr(
            _terminal_writer_service,
            "ensure_writer_ready",
            None,
        )
        if callable(_ensure_existing_writer):
            try:
                await _ensure_existing_writer()
            except Exception as _writer_error:
                _log.warning(
                    "terminal.sticky_writer_unavailable session=%s err=%s",
                    session_id,
                    type(_writer_error).__name__,
                )
    _durable_chat_readiness = chat_execution_writer_readiness(
        _terminal_writer_service,
        config=_durable_chat_config,
    )
    _chat_execution_decision = resolve_chat_execution_admission(
        config=_durable_chat_config,
        team_id=resolved_team_id,
        session_facts=_session_protocol_facts,
        client_capable=client_declared_terminal_reader,
        action_identity_complete=action_identity is not None,
        require_action_identity=True,
        existing_writer_ready=(_durable_chat_readiness.existing_writer_ready),
        fresh_writer_ready=_durable_chat_readiness.fresh_writer_ready,
    )
    from bowhead_core import metrics as _chat_execution_metrics

    _chat_execution_metrics.inc(
        "bowhead_chat_execution_admission_choice_total",
        labels={
            "mode": _durable_chat_config.mode.value,
            "protocol": _chat_execution_decision.write_protocol.value,
            "reason": _chat_execution_decision.reason,
            "cohort_eligible": str(
                _chat_execution_decision.cohort_eligible,
            ).lower(),
        },
        help="Durable chat execution policy outcomes.",
    )

    def _enforce_chat_execution_decision() -> None:
        if _chat_execution_decision.write_protocol is ChatExecutionWriteProtocol.REFRESH_REQUIRED:
            error = HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": {"code": "client_refresh_required"}},
            )
            # Reference claims have not started at either call site. Preserve
            # the browser's ability to edit this rejected draft without
            # sending it through the incompatible Terminal action protocol.
            raise _reference_rejected_before_admission(error) if reference_mode else error
        if _chat_execution_decision.write_protocol is ChatExecutionWriteProtocol.UNAVAILABLE:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": {
                        "code": "terminal_protocol_writer_not_ready",
                    }
                },
            )

    selected_terminal_v1 = _chat_execution_decision.write_protocol is ChatExecutionWriteProtocol.TERMINAL_V1
    _terminal_v1_admission_enabled = bool(selected_terminal_v1 and action_identity is not None and action_identity.action.kind == "start")
    _terminal_v1_action_steer_enabled = bool(selected_terminal_v1 and action_identity is not None and action_identity.action.kind == "steer")
    _strict_existing_session_access = _managed_protocol_at_authority_read
    if _ws_enforce:
        # R7.2 FAIL-CLOSED [review P1-a]: under enforce EVERY path ends in allow-or-deny — an
        # team-less caller (no JWT → stub, resolved_team_id None), a missing store, or a failed
        # membership lookup must DENY, never slip through to the datarc work below.
        if _user_store_authz is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": {"code": "authz_unavailable"}},
            )
        # [review P1-c] resolve the workspace's REAL owning team so the cross_team leg is live —
        # get_member_role keys only on (ws, user) and would otherwise honor a stale/cross-team
        # member row. ws_team_id None (no workspaces row: legacy datarc id) → cross_team leg skipped,
        # membership still required (authorize_ws_access docstring). Team-less caller is denied by
        # "no_team" before either lookup matters, so skip the reads entirely.
        existing_role: str | None = None
        ws_team_id: str | None = None
        if resolved_team_id:
            try:
                ws_team_id = await _user_store_authz.get_workspace_team_id(body.workspace_id)
                existing_role = await _user_store_authz.get_member_role(
                    body.workspace_id,
                    resolved_user_id,
                )
            except Exception as _e:  # cannot verify membership/ownership → fail closed
                _log.warning("member.lookup_failed user=%s ws=%s err=%s", resolved_user_id, body.workspace_id, _e)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"error": {"code": "authz_lookup_failed"}},
                ) from _e
        # authorize_ws_access denies an team-less caller ("no_team"), a caller whose team != the
        # workspace's team ("cross_team"), and a non-member ("not_a_member").
        _deny = authorize_ws_access(
            enforce=True,
            existing_role=existing_role,
            ws_team_id=ws_team_id,
            caller_team_id=resolved_team_id,
        )
        if _deny:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": {"code": "ws_access_denied", "reason": _deny}},
            )
    elif not _strict_existing_session_access and not body.session_id and resolved_team_id and _user_store_authz is not None:
        # flag OFF → best-effort auto-enrolment is a first-send compatibility
        # only.  An existing Session is an authority boundary; never mutate
        # membership while deciding whether its active tuple is legacy, v1 or
        # a future protocol version.
        try:
            existing_role = await _user_store_authz.get_member_role(
                body.workspace_id,
                resolved_user_id,
            )
            # Auto-enroll only never-members. get_member_role recognizes active only, so removed members also lack a role;
            # add_member (INSERT OR REPLACE) on that basis revives removed rows with one message, defeating removal and isolation.
            # Check removal history so first-send enrollment cannot restore a removed member.
            if not existing_role and not await _user_store_authz.has_member_record(body.workspace_id, resolved_user_id):
                is_admin = await _user_store_authz.is_team_admin(resolved_user_id, resolved_team_id)
                await _user_store_authz.add_member(
                    workspace_id=body.workspace_id,
                    user_id=resolved_user_id,
                    role="admin" if is_admin else "member",
                )
        except Exception as _e:
            _log.warning("member.auto_sync_failed user=%s ws=%s err=%s", resolved_user_id, body.workspace_id, _e)

    # Terminal v1 closes the authorization TOCTOU in this order:
    # 1) capture/ensure the fixed epoch vector; 2) perform the complete ACL
    # read below; 3) admission re-reads the vector under BEGIN IMMEDIATE.  A
    # revoke at any interleaving is therefore observed either by (2) or (3).
    _terminal_auth_fence_vector_digest: str | None = None
    if _terminal_v1_admission_enabled or _terminal_v1_action_steer_enabled or _managed_protocol_at_authority_read:
        _terminal_factory_for_auth = getattr(
            request.app.state,
            "terminal_runtime_factory",
            None,
        )
        _terminal_fence_team_id = resolved_team_id
        if _strict_existing_session_access and _user_store_authz is not None:
            try:
                _terminal_fence_team_id = await _user_store_authz.get_workspace_team_id(_existing_session_for_send.workspace_id)
            except Exception as _terminal_team_error:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"error": {"code": "authz_lookup_failed"}},
                ) from _terminal_team_error
        if _terminal_factory_for_auth is None or not _terminal_fence_team_id:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": {"code": "terminal_protocol_writer_not_ready"}},
            )
        try:
            _terminal_auth_fence_vector_digest = await _terminal_factory_for_auth.prepare_web_auth_fence(
                team_id=_terminal_fence_team_id,
                session_id=session_id,
                user_id=resolved_user_id,
            )
        except Exception as _terminal_auth_e:
            _log.warning(
                "terminal.auth_fence_prepare_failed team=%s err=%s",
                resolved_team_id,
                type(_terminal_auth_e).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": {"code": "terminal_protocol_writer_not_ready"}},
            ) from _terminal_auth_e

    _access_context: dict[str, Any] | None = None
    if resolved_team_id and _user_store_authz is not None:
        try:
            from bowhead_core.space_governance import SpaceOperation

            from bowhead_gateway.access_context import (
                AccessContextError,
                access_context_to_dict,
                build_access_context,
            )

            _expected_access_team_id = None
            if _strict_existing_session_access:
                _expected_access_team_id = await _user_store_authz.get_workspace_team_id(_existing_session_for_send.workspace_id)
                if not _expected_access_team_id:
                    raise AccessContextError("workspace_unavailable", 409)
            _access_ctx = await build_access_context(
                user_store=_user_store_authz,
                access_store=getattr(request.app.state, "access_control_store", None),
                user_id=resolved_user_id,
                workspace_id=body.workspace_id,
                operation=SpaceOperation.EXECUTE,
                expected_team_id=_expected_access_team_id,
            )
            _access_context = access_context_to_dict(_access_ctx)
            if _strict_existing_session_access:
                # Persisted Session ownership is exact-Team scoped.  Downstream
                # v1 store/factory calls must use that authoritative Team, not
                # a user's legacy primary-team projection.
                resolved_team_id = _access_ctx.team_id
        except AccessContextError as _e:
            if _e.code == "workspace_archived":
                raise HTTPException(
                    status_code=_e.status_code,
                    detail={"error": {"code": "workspace_archived"}},
                ) from _e
            if _e.code == "workspace_unavailable":
                raise HTTPException(
                    status_code=_e.status_code,
                    detail={"error": {"code": "workspace_unavailable"}},
                ) from _e
            if _ws_enforce or _strict_existing_session_access:
                _log.warning("access_context.build_failed user=%s ws=%s err=%s", resolved_user_id, body.workspace_id, _e)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error": {"code": "access_context_denied"}},
                ) from _e
            _log.info(
                "access_context.shadow_unavailable user=%s ws=%s err=%s",
                resolved_user_id,
                body.workspace_id,
                type(_e).__name__,
            )
        except Exception as _e:
            if _ws_enforce or _strict_existing_session_access:
                _log.warning("access_context.build_failed user=%s ws=%s err=%s", resolved_user_id, body.workspace_id, _e)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error": {"code": "access_context_denied"}},
                ) from _e
            _log.info("access_context.shadow_unavailable user=%s ws=%s err=%s", resolved_user_id, body.workspace_id, type(_e).__name__)

    # A response receipt is checked immediately after current-scope authorization,
    # before mutable availability/credit/model gates. A same-ID retry must replay
    # the already committed outcome even if those gates changed after the first
    # request; otherwise a lost 200 could become a misleading 402/409/503.
    _submission_store = None
    _submission_claim = None
    _submission_fingerprint_value: str | None = None
    _submission_fingerprints: tuple[str, ...] = ()
    _submission_fingerprint_key_id: str | None = None
    _submission_auth_generation_hash: str | None = None
    if body.client_submission_id is not None:
        _submission_store = getattr(orchestrator, "session_store", None)
        required_methods = (
            "claim_chat_submission",
            "finalize_started_chat_submission",
            "finalize_rejected_chat_submission",
            "release_chat_submission_claim",
            "get_chat_submission_receipt",
            "finalize_steer_chat_submission",
            "is_chat_auth_session_generation_active",
        )
        if _submission_store is None or any(not hasattr(_submission_store, method) for method in required_methods):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": {"code": "submission_store_unavailable"}},
            )
        from bowhead_gateway.routes.auth import _auth_session_generation

        _auth_generation = _auth_session_generation(request)
        (
            _submission_fingerprint_key_id,
            _submission_fingerprint_value,
            _submission_fingerprints,
        ) = _submission_fingerprint(
            body,
            user_id=resolved_user_id,
            team_id=resolved_team_id or "",
            auth_session_generation=_auth_generation,
        )
        _submission_auth_generation_hash = await _require_current_submission_auth_generation(
            request,
            store=_submission_store,
            user_id=resolved_user_id,
        )
        assert _submission_fingerprint_key_id is not None
        assert _submission_fingerprint_value is not None
        assert _submission_auth_generation_hash is not None
        existing_receipt = await _submission_store.get_chat_submission_receipt(
            team_id=resolved_team_id or "",
            user_id=resolved_user_id,
            client_submission_id=str(body.client_submission_id),
            workspace_id=body.workspace_id,
            requested_session_id=body.session_id,
        )
        if existing_receipt is not None:
            request.state.composer_send_result_recorded = True
            if existing_receipt.request_fingerprint not in _submission_fingerprints:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"error": {"code": "submission_id_conflict"}},
                )
            if existing_receipt.outcome == "rejected":
                raise chat_submission_receipt_http_error(
                    existing_receipt.response,
                    fallback_code="submission_rejected",
                )
            return ChatSendResponse.model_validate(existing_receipt.response)

        if body.document_revision_target is not None:
            from bowhead_core.document_revision_target import document_reuse_enabled

            if not document_reuse_enabled():
                raise HTTPException(409, detail={"error": {"code": "document_revision_unavailable"}})

        if body.attachment_refs is not None:
            try:
                effective_attachment_refs = await _validate_chat_attachment_refs(
                    request,
                    refs=body.attachment_refs,
                    runtime_ids=effective_attachment_ids,
                    user_id=resolved_user_id,
                    workspace_id=body.workspace_id,
                    session_id=body.session_id,
                )
            except HTTPException as exc:
                # Receipt replay was checked above; no new claim exists yet.
                raise _reference_rejected_before_admission(exc) if reference_mode else exc

        if body.submission_kind == "steer" and not reference_mode:
            submission_id = str(body.client_submission_id)
            submission_team_id = resolved_team_id or ""
            _submission_claim = await _submission_store.claim_chat_submission(
                team_id=submission_team_id,
                user_id=resolved_user_id,
                client_submission_id=submission_id,
                workspace_id=body.workspace_id,
                requested_session_id=body.session_id,
                submission_kind="steer",
                submission_contract_version=body.submission_contract_version,
                model_control_batch=body.model_control_batch,
                request_fingerprint=_submission_fingerprint_value,
                accepted_request_fingerprints=_submission_fingerprints,
                fingerprint_key_id=_submission_fingerprint_key_id,
                auth_session_generation_hash=_submission_auth_generation_hash,
            )
            if _submission_claim.kind == "receipt":
                request.state.composer_send_result_recorded = True
                receipt = _submission_claim.receipt
                assert receipt is not None
                if receipt.outcome == "rejected":
                    raise chat_submission_receipt_http_error(
                        receipt.response,
                        fallback_code="submission_rejected",
                    )
                return ChatSendResponse.model_validate(receipt.response)
            claim_errors = {
                "pending": "submission_pending",
                "conflict": "submission_id_conflict",
                "abandoned": "submission_abandoned",
                "not_found": "submission_not_found",
                "auth_generation_inactive": "auth_session_generation_inactive",
            }
            if _submission_claim.kind in claim_errors:
                raise chat_send_http_error(claim_errors[_submission_claim.kind])
            from bowhead_core.composer_rollout import (
                composer_submission_protocol_ready,
                resolve_composer_capabilities,
            )

            composer_caps = resolve_composer_capabilities(resolved_team_id)
            contract_enabled = {
                "a": composer_submission_protocol_ready(),
                "b": composer_caps.model_panel_v2,
                "c": composer_caps.thinking_effort,
            }[body.model_control_batch]
            if not contract_enabled:
                rejection = {
                    "http_status": status.HTTP_409_CONFLICT,
                    "detail": {"error": {"code": "submission_contract_inactive"}},
                }
                await _submission_store.finalize_rejected_chat_submission(
                    team_id=submission_team_id,
                    user_id=resolved_user_id,
                    client_submission_id=submission_id,
                    workspace_id=body.workspace_id,
                    requested_session_id=body.session_id,
                    submission_kind="steer",
                    submission_contract_version=body.submission_contract_version,
                    model_control_batch=body.model_control_batch,
                    request_fingerprint=_submission_fingerprint_value,
                    lease_owner=_submission_claim.lease_owner,
                    lease_version=_submission_claim.lease_version,
                    response=rejection,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"error": {"code": "submission_contract_inactive"}},
                )

            steer_receipt = await _submission_store.finalize_steer_chat_submission(
                team_id=submission_team_id,
                user_id=resolved_user_id,
                client_submission_id=submission_id,
                workspace_id=body.workspace_id,
                requested_session_id=body.session_id,
                expected_parent_turn_id=body.expected_parent_turn_id,
                expected_execution_generation=body.expected_execution_generation,
                text=text,
                attachment_ids=effective_attachment_ids,
                attachment_refs=effective_attachment_refs,
                submission_contract_version=body.submission_contract_version,
                model_control_batch=body.model_control_batch,
                request_fingerprint=_submission_fingerprint_value,
                lease_owner=_submission_claim.lease_owner,
                lease_version=_submission_claim.lease_version,
            )
            if steer_receipt.outcome == "rejected":
                raise chat_submission_receipt_http_error(
                    steer_receipt.response,
                    fallback_code="steer_parent_not_active",
                )
            if steer_receipt.outcome == "buffered":
                try:
                    from bowhead_core.events import OrchestratorEvent

                    await get_event_registry().emit_if_local_producer(
                        steer_receipt.turn_id,
                        OrchestratorEvent(
                            event_type="emoji_ack",
                            turn_id=steer_receipt.turn_id,
                            payload={
                                "target_msg_id": body.client_info.get("user_msg_id") or f"u_{uuid4().hex[:12]}",
                                "emoji": "🤝",
                                "text": text,
                            },
                        ),
                    )
                except Exception as exc:
                    _log.warning(
                        "steer.emoji_ack_emit_failed turn=%s err=%s",
                        steer_receipt.turn_id,
                        exc,
                    )
            return ChatSendResponse.model_validate(steer_receipt.response)

        if body.submission_kind == "replay":
            if not hasattr(_submission_store, "get_chat_turn_user_source"):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"error": {"code": "submission_store_unavailable"}},
                )
            source = await _submission_store.get_chat_turn_user_source(
                turn_id=body.replay_source_turn_id,
                user_id=resolved_user_id,
                workspace_id=body.workspace_id,
            )
            if source is None or (body.session_id and source.session_id != body.session_id):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": {"code": "replay_source_not_found"}},
                )
            if source.input_document is not None or source.document_revision_target is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"error": {"code": "unsupported_entry"}},
                )
            text = source.user_text
            effective_attachment_ids = list(source.attachment_ids)
            effective_attachment_refs = [dict(ref) for ref in source.attachment_refs]
            effective_source_parts = [dict(part) for part in source.parts] if source.parts is not None else None
            if source.attachment_refs:
                replay_refs = [ChatAttachmentRef.model_validate(ref) for ref in source.attachment_refs]
                effective_attachment_refs = await _validate_chat_attachment_refs(
                    request,
                    refs=replay_refs,
                    runtime_ids=effective_attachment_ids,
                    user_id=resolved_user_id,
                    workspace_id=body.workspace_id,
                    session_id=source.session_id,
                )
    if body.client_submission_id is None and body.attachment_refs is not None:
        effective_attachment_refs = await _validate_chat_attachment_refs(
            request,
            refs=body.attachment_refs,
            runtime_ids=effective_attachment_ids,
            user_id=resolved_user_id,
            workspace_id=body.workspace_id,
            session_id=body.session_id,
        )
    if (_terminal_v1_admission_enabled or _terminal_v1_action_steer_enabled or _strict_existing_session_access) and _access_context is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": {"code": "access_context_denied"}},
        )

    if _strict_existing_session_access:
        # Every /chat/send input creates work. Cancellation is owned exclusively
        # by the explicit Stop endpoint, so text never bypasses entitlement.
        from bowhead_gateway._entitlement_gate import gate_turn_http

        await gate_turn_http(request, resolved_team_id, response)

    if body.session_id:
        _enforce_chat_execution_decision()

    # Steer is an EXECUTE operation too. It runs only after the same
    # Team, membership and lifecycle admission as a fresh turn, so an archived
    # Space cannot resume or mutate an in-flight session through this shortcut.
    if (
        body.session_id
        and action_identity is None
        and body.context_maintenance is None
        and not reference_mode
        and not _plan_exec_id
        and (body.client_submission_id is None or body.submission_kind == "steer")
    ):
        from bowhead_gateway.turn_service import steer_or_proceed

        expected_turn_id = body.client_info.get("expected_turn_id") or None
        user_msg_id = body.client_info.get("user_msg_id") or f"u_{uuid4().hex[:12]}"
        _steer = await steer_or_proceed(
            orchestrator,
            session_id=session_id,
            text=text,
            user_msg_id=user_msg_id,
            expected_turn_id=expected_turn_id,
            has_attachments=bool(body.attachments),
        )
        if _steer.kind == "attachments_unsupported":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error": {"code": "request_validation_failed"}},
            )
        if _steer.kind == "protocol_active":
            if not _managed_protocol_at_authority_read:
                # The active tuple changed from legacy/idle to a managed
                # protocol after the authority read.  Do not expose its
                # version or mutate either the legacy buffer or membership;
                # a retry will restart with the strict ACL path.
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"error": {"code": "terminal_dependency_recovering"}},
                )
            try:
                steer_protocol = resolve_persisted_chat_execution_protocol(
                    _steer.active_protocol_version,
                )
            except Exception as protocol_error:
                raise _terminal_admission_http_error(
                    protocol_error,
                ) from protocol_error
            if steer_protocol is PersistedChatExecutionProtocol.TERMINAL_V1:
                terminal_factory = getattr(
                    request.app.state,
                    "terminal_runtime_factory",
                    None,
                )
                if terminal_factory is None or not resolved_team_id:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={"error": {"code": "terminal_dependency_recovering"}},
                    )
                auth_digest = _terminal_auth_fence_vector_digest
                if auth_digest is None:
                    # The active tuple changed from legacy/idle to v1 after
                    # the pre-ACL discriminator read.  Capturing a new vector
                    # here would invert the required fence→ACL→CAS order, so
                    # force a clean retry instead.
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={"error": {"code": "terminal_dependency_recovering"}},
                    )
                try:
                    active_scope = await request.app.state.terminal_protocol_store.read_current_scope(
                        team_id=resolved_team_id,
                        turn_id=_steer.active_turn_id,
                    )
                except Exception as active_scope_error:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={"error": {"code": "terminal_dependency_recovering"}},
                    ) from active_scope_error
                if active_scope.session_id != session_id or active_scope.turn_id != _steer.active_turn_id:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail={"error": {"code": "terminal_protocol_malformed"}},
                    )
                if expected_turn_id and _steer.active_turn_id != expected_turn_id:
                    return ChatSendResponse(
                        session_id=session_id,
                        turn_id=_steer.active_turn_id,
                        stream_url="",
                        status="completed",
                        execution_generation=active_scope.generation,
                        terminal_protocol_version=1,
                    )
                request_id = body.client_info.get("request_id") or body.client_info.get("message_id") or user_msg_id
                try:
                    accepted = await terminal_factory.steer_web_turn(
                        team_id=resolved_team_id,
                        session_id=session_id,
                        user_id=resolved_user_id,
                        expected_turn_id=active_scope.turn_id,
                        expected_execution_generation=active_scope.generation,
                        request_id=request_id,
                        user_msg_id=user_msg_id,
                        user_text=text,
                        workspace_id=body.workspace_id,
                        auth_fence_vector_digest=auth_digest,
                        presentation_locale=(request.headers.get("accept-language", "en-US").split(",", 1)[0] or "en-US"),
                        attachments=tuple(body.attachments),
                        planning_mode=body.planning_mode,
                    )
                except Exception as steer_error:
                    raise _terminal_admission_http_error(steer_error) from steer_error
                return ChatSendResponse(
                    session_id=session_id,
                    turn_id=accepted.active_turn_id or _steer.active_turn_id,
                    stream_url=f"/api/v1/chat/stream/{accepted.active_turn_id or _steer.active_turn_id}",
                    status="buffered",
                    execution_generation=accepted.active_generation or 1,
                    terminal_protocol_version=1,
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": {"code": "terminal_protocol_malformed"}},
            )
        if _steer.kind == "lookup_failed":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": {"code": "terminal_dependency_recovering"}},
            )
        if _steer.kind == "buffered":
            return ChatSendResponse(
                session_id=session_id,
                turn_id=_steer.active_turn_id,
                stream_url=f"/api/v1/chat/stream/{_steer.active_turn_id}",
                status=_steer.kind,
            )
        if _steer.kind == "completed":
            return ChatSendResponse(
                session_id=session_id,
                turn_id=_steer.active_turn_id or expected_turn_id or "",
                stream_url="",
                status="completed",
            )

    # Planning Mode track D — approve ownership + claim guard. Runs AFTER identity +
    # ws authz (member verified) and BEFORE the datarc/turn work, so an unauthorized
    # or non-owner approve never touches datarc. Verifies the plan belongs to this
    # caller+workspace (tenant in the WHERE), is approvable, and atomically claims
    # RUNNING (anti double-run). Inert when _plan_exec_id == "" (flag off / normal send).
    if _plan_exec_id:

        async def reject_plan(http_status: int, code: str) -> None:
            if body.client_submission_id is not None and _submission_claim is not None and _submission_claim.kind == "claimed":
                assert _submission_store is not None
                assert _submission_fingerprint_value is not None
                await _submission_store.finalize_rejected_chat_submission(
                    team_id=resolved_team_id or "",
                    user_id=resolved_user_id,
                    client_submission_id=str(body.client_submission_id),
                    workspace_id=body.workspace_id,
                    requested_session_id=body.session_id,
                    submission_kind=body.submission_kind,
                    submission_contract_version=body.submission_contract_version,
                    model_control_batch=body.model_control_batch,
                    request_fingerprint=_submission_fingerprint_value,
                    lease_owner=_submission_claim.lease_owner,
                    lease_version=_submission_claim.lease_version,
                    response={
                        "http_status": http_status,
                        "detail": {"error": {"code": code}},
                    },
                )
            raise chat_send_http_error(code)

        async def fail_plan_transient(code: str) -> None:
            """Release this request's lease for retryable plan-service faults."""
            if body.client_submission_id is not None and _submission_claim is not None and _submission_claim.kind == "claimed":
                assert _submission_store is not None
                assert _submission_claim.lease_owner is not None
                assert _submission_claim.lease_version is not None
                await _submission_store.release_chat_submission_claim(
                    team_id=resolved_team_id or "",
                    user_id=resolved_user_id,
                    client_submission_id=str(body.client_submission_id),
                    lease_owner=_submission_claim.lease_owner,
                    lease_version=_submission_claim.lease_version,
                )
            raise chat_send_http_error(code)

        _plan_store = getattr(request.app.state, "plan_store", None)
        if _plan_store is None:
            await fail_plan_transient("planning_unavailable")
        from bowhead_core.plan_store import PlanStatus

        try:
            _plan = await _plan_store.get_plan(
                _plan_exec_id,
                user_id=resolved_user_id,
                workspace_id=body.workspace_id,
            )
        except Exception:
            _log.exception("chat.plan_lookup_failed plan=%s", _plan_exec_id)
            await fail_plan_transient("planning_unavailable")
        _plan_rejection: tuple[int, str] | None = None
        if _plan is None:
            _plan_rejection = (status.HTTP_404_NOT_FOUND, "plan_not_found")
        elif action_identity is not None and action_identity.action.kind == "start" and body.session_id != _plan.session_id:
            # A Terminal action, including an exact in-flight replay, remains
            # scoped to the Plan Session supplied by the first request.
            raise _chat_action_request_conflict()
        elif body.expected_plan_revision is not None and body.expected_plan_revision != _plan.current_rev:
            _plan_rejection = (status.HTTP_409_CONFLICT, "plan_not_approvable")
        elif _plan.status not in (PlanStatus.PROPOSED, PlanStatus.BLOCKED):
            same_terminal_action_claim = bool(
                _plan.status == PlanStatus.RUNNING
                and _terminal_v1_admission_enabled
                and action_identity is not None
                and action_identity.action.kind == "start"
                and getattr(_plan, "execution_turn_id", None) == action_identity.action.start_turn_id
            )
            if not same_terminal_action_claim:
                _plan_rejection = (
                    status.HTTP_409_CONFLICT,
                    "plan_already_running" if _plan.status == PlanStatus.RUNNING else "plan_not_approvable",
                )
        # NB: the atomic try_claim_running happens LATER (just before spawn) so a
        # post-here failure (credit 402 / datarc 403) can't leave the plan stuck
        # RUNNING with no turn. Here we only verify ownership + approvable status.
        # run in the plan's own session so the report lands in the user's canvas
        if _plan is not None and _plan.session_id:
            session_id = _plan.session_id

    # workspace/ws-datarc-decouple: resolve the Datarc project + team-scoped clients once at
    # turn-start, then thread the same runtime envelope into member resolution and TurnRequest.
    _datarc_ctx, member_uid = await _resolve_web_datarc_identity(
        request,
        team_id=resolved_team_id,
        workspace_id=body.workspace_id,
        user_id=resolved_user_id,
        session_id=session_id,
        body_member_uid=body.member_uid,
        ws_enforce=_ws_enforce,
        log=_log,
    )
    _log.info(
        "chat_send_recv ws=%s member=%s text_len=%d attachment_count=%d body.session_id=%r resolved=%s new_session=%s",
        body.workspace_id,
        member_uid[:8] if member_uid else None,
        len(text),
        len(effective_attachment_ids),
        body.session_id,
        session_id,
        body.session_id is None,
    )

    # Confirmation is a control action. Resolve its new Turn's language from
    # the owned task, while keeping the submitted action and text unchanged.
    language_body = body
    if _plan_exec_id:
        language_body = body.model_copy(
            update={
                "message": getattr(_plan, "objective", "") or getattr(_plan, "title", "") or body.message,
                "session_id": session_id,
            }
        )
    (
        profile_dict,
        workspace_settings_kv,
        language_context,
        paradigm_v2,
    ) = await _resolve_web_language_inputs(request, language_body, team_id=resolved_team_id, log=_log)

    # (caller resolution + workspace authz + first-send auto-enroll moved earlier — before the
    # datarc touch — see the R7.2 [review P1-a] block above. resolved_user_id/resolved_team_id
    # are in scope here.)

    # P4.5.10: pre-flight credit check (team_id = real user; quota check, no stub check).
    _credit_denial = await check_credit_admission(
        getattr(request.app.state, "user_store", None),
        team_id=resolved_team_id,
        user_id=resolved_user_id,
        workspace_id=body.workspace_id,
        metrics_log=getattr(request.app.state, "metrics_log", None),
    )
    # Keep route error codes literal (chat_send_errors closed set: only its listed codes may use runtime expressions).
    if _credit_denial == "user_credit_exhausted":
        raise HTTPException(
            status_code=402,
            detail={"error": {"code": "user_credit_exhausted"}},
        )
    if _credit_denial == "team_pool_exhausted":
        raise HTTPException(
            status_code=402,
            detail={"error": {"code": "team_pool_exhausted"}},
        )

    # Licensing: pre-flight entitlement gate (§3.3①) — ABOVE create_task so a denied turn costs zero LLM.
    # Raises 402/403 (expired/suspended/over-grace/needs-license/tamper); stamps X-Entitlement-Health on the 200.
    from bowhead_gateway._entitlement_gate import gate_turn_http

    await gate_turn_http(request, resolved_team_id, response)

    if not body.session_id:
        _enforce_chat_execution_decision()

    # V7 kb-agentic-fix §2.6: resolve kb_role from JWT email (BOWHEAD_AUTO_ADMIN_DOMAINS env).
    from bowhead_gateway.routes.auth import resolve_kb_role

    _kb_role = resolve_kb_role(request)

    preferred_provider, preferred_model = await _resolve_effective_model_preference(
        request,
        team_id=resolved_team_id,
        preferred_provider=body.preferred_provider,
        preferred_model=body.preferred_model,
        log=_log,
    )

    # Batch 0b: every authenticated team turn resolves through the typed team
    # runtime boundary before any bus/turn row is created.  Missing, invalid, or
    # unreadable team config must never become permission to use orch.router.
    _team_model_runtime = None
    if resolved_team_id:
        from bowhead_gateway.turn_service import resolve_team_model_runtime

        _team_model_runtime = await resolve_team_model_runtime(
            request.app.state,
            resolved_team_id,
            entrypoint="plan" if _plan_exec_id else "chat",
        )
        if not _team_model_runtime.available:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": {"code": "team_model_unconfigured"}},
            )

    if reference_mode:
        from bowhead_gateway.chat_reference_start import prepare_reference_http_start

        try:
            # Receiver selection may call the model before any Turn exists; date it on the
            # viewer's calendar like the Turn it admits (bowhead_core.model_clock).
            with viewer_clock_scope(viewer_time_zone_name(request)):
                reference_start = await prepare_reference_http_start(
                    request,
                    body.input_document,
                    team_id=resolved_team_id or "",
                    user_id=resolved_user_id,
                    workspace_id=body.workspace_id,
                    requested_session_id=body.session_id,
                    entry_mode=("steer" if body.submission_kind == "steer" else "plan" if body.planning_mode == "planning" else "ordinary"),
                    router=_team_model_runtime.router if _team_model_runtime is not None else None,
                    trace_id=f"reference-receiver:{body.client_submission_id}",
                    preferred_provider=preferred_provider,
                    preferred_model=preferred_model,
                    has_attachments=bool(effective_attachment_ids),
                    attachment_refs=effective_attachment_refs,
                )
        except HTTPException as exc:
            # Another request may have committed while local resources were
            # being resolved. Recover that outcome before reporting mutable
            # availability for this same submission.
            recovered = await _read_reference_submission(body, request)
            if recovered is not None:
                return recovered
            raise _reference_rejected_before_admission(exc) from None

        if body.submission_kind == "steer":
            import os

            from bowhead_core.chat_reference_storage import (
                ChatReferenceStore,
                QueuedReferenceInput,
                ReferenceCipher,
                ReferenceScope,
            )

            assert _submission_store is not None
            assert _submission_fingerprint_value is not None
            assert _submission_fingerprint_key_id is not None
            assert _submission_auth_generation_hash is not None
            assert body.session_id is not None
            assert body.expected_parent_turn_id is not None
            assert body.expected_execution_generation is not None
            assert body.input_document is not None
            submission_id = str(body.client_submission_id)
            submission_team_id = resolved_team_id or ""
            _submission_claim = await _submission_store.claim_chat_submission(
                team_id=submission_team_id,
                user_id=resolved_user_id,
                client_submission_id=submission_id,
                workspace_id=body.workspace_id,
                requested_session_id=body.session_id,
                submission_kind="steer",
                submission_contract_version=body.submission_contract_version,
                model_control_batch=body.model_control_batch,
                request_fingerprint=_submission_fingerprint_value,
                accepted_request_fingerprints=_submission_fingerprints,
                fingerprint_key_id=_submission_fingerprint_key_id,
                auth_session_generation_hash=_submission_auth_generation_hash,
            )
            if _submission_claim.kind == "receipt":
                request.state.composer_send_result_recorded = True
                receipt = _submission_claim.receipt
                assert receipt is not None
                if receipt.outcome == "rejected":
                    raise chat_submission_receipt_http_error(
                        receipt.response,
                        fallback_code="submission_rejected",
                    )
                return ChatSendResponse.model_validate(receipt.response)
            claim_errors = {
                "pending": "submission_pending",
                "conflict": "submission_id_conflict",
                "abandoned": "submission_abandoned",
                "not_found": "submission_not_found",
                "auth_generation_inactive": "auth_session_generation_inactive",
            }
            if _submission_claim.kind in claim_errors:
                raise chat_send_http_error(claim_errors[_submission_claim.kind])
            assert _submission_claim.lease_owner is not None
            assert _submission_claim.lease_version is not None
            reference_scope = ReferenceScope(
                team_id=submission_team_id,
                user_id=resolved_user_id,
                workspace_id=body.workspace_id,
                session_id=body.session_id,
                policy_epoch=reference_start.policy_epoch,
            )
            reference_input = QueuedReferenceInput.model_validate(
                {
                    "schema_version": 1,
                    "orchestration_contract": reference_start.orchestration_contract,
                    "input_document": body.input_document.model_dump(
                        mode="json",
                        exclude_none=True,
                    ),
                    "automatic_receiver": (
                        reference_start.automatic_receiver.model_dump(
                            mode="json",
                            exclude_none=True,
                        )
                        if reference_start.automatic_receiver is not None
                        else None
                    ),
                    "attachment_ids": effective_attachment_ids,
                    "attachment_refs": effective_attachment_refs,
                }
            )
            steer_receipt = await _submission_store.finalize_steer_chat_submission(
                team_id=submission_team_id,
                user_id=resolved_user_id,
                client_submission_id=submission_id,
                workspace_id=body.workspace_id,
                requested_session_id=body.session_id,
                expected_parent_turn_id=body.expected_parent_turn_id,
                expected_execution_generation=body.expected_execution_generation,
                text=text,
                attachment_ids=effective_attachment_ids,
                attachment_refs=effective_attachment_refs,
                submission_contract_version=body.submission_contract_version,
                model_control_batch=body.model_control_batch,
                request_fingerprint=_submission_fingerprint_value,
                lease_owner=_submission_claim.lease_owner,
                lease_version=_submission_claim.lease_version,
                reference_store=ChatReferenceStore(
                    ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", "")),
                ),
                reference_scope=reference_scope,
                reference_input=reference_input,
            )
            if steer_receipt.outcome == "rejected":
                raise chat_submission_receipt_http_error(
                    steer_receipt.response,
                    fallback_code="steer_parent_not_active",
                )
            try:
                from bowhead_core.events import OrchestratorEvent

                await get_event_registry().emit_if_local_producer(
                    steer_receipt.turn_id,
                    OrchestratorEvent(
                        event_type="emoji_ack",
                        turn_id=steer_receipt.turn_id,
                        payload={
                            "target_msg_id": body.client_info.get("user_msg_id") or f"u_{uuid4().hex[:12]}",
                            "emoji": "🤝",
                            "text": text,
                        },
                    ),
                )
            except Exception as exc:
                _log.warning(
                    "steer.emoji_ack_emit_failed turn=%s err=%s",
                    steer_receipt.turn_id,
                    exc,
                )
            return ChatSendResponse.model_validate(steer_receipt.response)

    if _terminal_v1_action_steer_enabled:
        assert action_identity is not None
        assert action_identity.expected_turn_id is not None
        assert action_identity.expected_execution_generation is not None
        terminal_factory = getattr(
            request.app.state,
            "terminal_runtime_factory",
            None,
        )
        if terminal_factory is None or not resolved_team_id or _terminal_auth_fence_vector_digest is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": {"code": "terminal_protocol_writer_not_ready"}},
            )
        try:
            accepted = await terminal_factory.steer_web_turn(
                team_id=resolved_team_id,
                session_id=session_id,
                user_id=resolved_user_id,
                expected_turn_id=action_identity.expected_turn_id,
                expected_execution_generation=(action_identity.expected_execution_generation),
                request_id=action_identity.action.action_id,
                user_msg_id=action_identity.action.user_message_id,
                user_text=text,
                workspace_id=body.workspace_id,
                auth_fence_vector_digest=(_terminal_auth_fence_vector_digest),
                presentation_locale=(language_context.ui_language if language_context is not None else "en-US"),
                attachments=tuple(effective_attachment_ids),
                planning_mode=(body.planning_mode if _planning_enabled() else None),
            )
        except Exception as steer_error:
            raise _terminal_admission_http_error(steer_error) from steer_error
        assert accepted.scope is not None
        return ChatSendResponse(
            session_id=accepted.scope.session_id,
            turn_id=accepted.scope.turn_id,
            stream_url=f"/api/v1/chat/stream/{accepted.scope.turn_id}",
            status="buffered",
            execution_generation=accepted.scope.generation,
            terminal_protocol_version=1,
        )

    # Sprint chat-redesign-2026-05-21 Phase 1.1 (P0-7) — L3 picker hint.
    # When the user sends empty text + attachments, the gateway looks up mime-
    # derived candidate labels and passes them to Lead so the first action is
    # `clarification_ask` with proper choices (instead of guessing).
    _l3_hint: list[str] | None = None
    if not text and effective_attachment_ids:
        try:
            from bowhead_gateway.l3_picker.candidates import (
                candidates_from_attachments,
            )

            _artifact_store = getattr(
                request.app.state,
                "artifact_store",
                None,
            )
            if _artifact_store is not None:
                _l3_hint = await candidates_from_attachments(
                    effective_attachment_ids,
                    artifact_store=_artifact_store,
                )
        except Exception as _l3_e:
            _log.warning("l3_picker.candidates_failed err=%s", _l3_e)

    async def _chart_authoring_projection(section: Any) -> dict[str, Any] | None:
        from bowhead_gateway.chart_authoring_projection import project_chart_for_authoring

        return await project_chart_for_authoring(request, session_id, section)

    turn_req = TurnRequest(
        context_maintenance=body.context_maintenance,
        chart_authoring_projection=_chart_authoring_projection,
        document_revision_target=body.document_revision_target,
        session_id=session_id,
        workspace_id=body.workspace_id,
        # workspace-datarc-decouple P1 (R8): seeded datarc project (None pre-migration →
        # ctx._ws falls back to workspace_id == today's datarc id).
        active_datarc_project=_datarc_ctx.active_datarc_project,
        datarc_binding=_datarc_ctx.connection_binding,
        # Batch 2 (PRD §3.3b): carry per-user snapshots in the envelope (legacy always defaults).
        active_datarc_workspace_name=getattr(_datarc_ctx, "active_datarc_workspace_name", "") or "",
        accessible_datarc_workspaces=getattr(_datarc_ctx, "accessible_datarc_workspaces", ()) or (),
        datarc_absence_reason=getattr(_datarc_ctx, "datarc_absence_reason", "") or "",
        # Batch 2/c6 (PRD §3.4): switch commit callback. After tool_switch_context's unique match, this
        # closure re-resolves member_uid + writes session preference (never overwrites explicit default).
        datarc_context_switcher=(
            _make_datarc_context_switcher(
                request,
                resolved_user_id,
                session_id,
                workspace_id=body.workspace_id,
                binding=_datarc_ctx.connection_binding,
                ws_enforce=_ws_enforce,
                log=_log,
            )
        ),
        # ws-datarc-decouple P2a (③): per-team datarc client resolved above (R7.1 team-scoped).
        datarc_client=_datarc_ctx.datarc_client,
        # [review P1-b] resolution failed → fail closed (turn_runner won't use the global client).
        datarc_unavailable=_datarc_ctx.datarc_unavailable,
        # embed-via-openapi: team embed-proxy client for the BI semantic dictionary (None →
        # turn_runner falls back to the global direct embed client).
        vector_client=_datarc_ctx.vector_client,
        team_router=(_team_model_runtime.router if _team_model_runtime is not None else None),
        team_kb_retriever=(_team_model_runtime.kb_retriever if _team_model_runtime is not None else None),
        user_id=resolved_user_id,
        member_uid=member_uid,
        text=text,
        turn_id=(
            action_identity.action.start_turn_id
            if action_identity is not None and action_identity.action.kind == "start"
            else f"t_{uuid4().hex[:12]}"
        ),
        attachments=effective_attachment_ids,
        user_profile=profile_dict,
        language_context=language_context,
        # i18n disabled ⇒ no language snapshot to carry the viewer zone; keep it on the Turn instead.
        viewer_time_zone=(viewer_time_zone_name(request) if language_context is None else ""),
        preferred_provider=preferred_provider,
        preferred_model=preferred_model,
        paradigm_v2=paradigm_v2,
        # M158-12: pass workspace_settings_kv to turn_runner / build_lead_system_prompt.
        workspace_settings=workspace_settings_kv or None,
        # V4 PRD-D-4-4: team_id scopes memory_pack_snapshots admin access (NULL = orphan).
        team_id=resolved_team_id,
        access_context=_access_context,
        terminal_auth_fence_vector_digest=_terminal_auth_fence_vector_digest,
        # V7 kb-agentic-fix: caller user RBAC role for KB ScopeContext
        kb_role=_kb_role,
        # Sprint chat-redesign-2026-05-21 Phase 1.1 (P0-7)
        l3_picker_hint=_l3_hint,
        # Planning Mode — composer mode selector ("planning" forces hook1).
        # Batch A: the runtime kill switch closes both UI and wire behavior.
        # Explicit planning input is ignored when the server gate is off, and the
        # effective mode is recorded as "off" (not None = Auto): the durable
        # intent then carries "no auto plan", so a later wake or exact replay
        # after the flag is back on cannot start proposing plans for this turn.
        planning_mode=body.planning_mode if _planning_enabled() else "off",
        # track D — approve: run the already-approved plan via hook2 (not Lead loop).
        plan_execution_id=_plan_exec_id or None,
        expected_plan_revision=body.expected_plan_revision,
        # Config copilot batch 0.5 (tech-design-batch05-chat.md §5.2): only Web + flag on supports inline
        # light confirmation; Feishu/scheduled constructors default False -> config_propose structured rejection.
        config_lite_supported=True,
        configuration_settings_target=_configuration_settings_target,
        coach_canvas_target=_coach_canvas_target,
        # 2026-08-11: Web main path is an interactive user session, the sole entry for first-turn AI sidebar titles.
        # Scheduled jobs / dry-run do not declare it (default False), avoiding wasted calls for each new session.
        knowledge_user_request=True,
        interactive_session=True,
    )

    from bowhead_gateway.datarc_context import recovered_datarc_source

    if resolved_team_id:
        turn_req.datarc_source_evidence = recovered_datarc_source(_datarc_ctx, resolved_user_id, resolved_team_id or "", member_uid)
        if _datarc_ctx.datarc_client is None:
            turn_req.member_uid = ""
            turn_req.active_datarc_project = None

    if reference_start is not None:
        from dataclasses import replace as _reference_replace

        from bowhead_core.chat_reference_storage import ReferenceScope

        turn_req = _reference_replace(
            turn_req,
            planning_mode="planning" if body.planning_mode == "planning" else "off",
            reference_scope=ReferenceScope(
                team_id=resolved_team_id or "",
                user_id=resolved_user_id,
                workspace_id=body.workspace_id,
                session_id=session_id,
                policy_epoch=reference_start.policy_epoch,
            ),
        )

    if body.client_submission_id is not None:
        assert _submission_store is not None
        assert _submission_fingerprint_value is not None
        assert _submission_fingerprint_key_id is not None
        assert _submission_auth_generation_hash is not None
        submission_id = str(body.client_submission_id)
        submission_team_id = resolved_team_id or ""
        if _submission_claim is None:
            _submission_claim = await _submission_store.claim_chat_submission(
                team_id=submission_team_id,
                user_id=resolved_user_id,
                client_submission_id=submission_id,
                workspace_id=body.workspace_id,
                requested_session_id=body.session_id,
                submission_kind=body.submission_kind,
                submission_contract_version=body.submission_contract_version,
                model_control_batch=body.model_control_batch,
                request_fingerprint=_submission_fingerprint_value,
                accepted_request_fingerprints=_submission_fingerprints,
                fingerprint_key_id=_submission_fingerprint_key_id,
                auth_session_generation_hash=_submission_auth_generation_hash,
            )
        if _submission_claim.kind == "receipt":
            request.state.composer_send_result_recorded = True
            receipt = _submission_claim.receipt
            assert receipt is not None
            if receipt.outcome == "rejected":
                raise chat_submission_receipt_http_error(
                    receipt.response,
                    fallback_code="submission_rejected",
                )
            return ChatSendResponse.model_validate(receipt.response)
        if _submission_claim.kind == "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": {"code": "submission_pending"}},
            )
        if _submission_claim.kind == "conflict":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": {"code": "submission_id_conflict"}},
            )
        if _submission_claim.kind == "abandoned":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": {"code": "submission_abandoned"}},
            )
        if _submission_claim.kind == "not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": {"code": "submission_not_found"}},
            )
        if _submission_claim.kind == "auth_generation_inactive":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": {"code": "auth_session_generation_inactive"}},
            )
        from bowhead_core.composer_rollout import (
            composer_submission_protocol_ready,
            resolve_composer_capabilities,
        )

        composer_caps = resolve_composer_capabilities(resolved_team_id)
        contract_enabled = {
            # Batch A's receipt substrate remains active through a visual flag
            # rollback.  Existing and new legacy-view submissions therefore
            # keep the same crash-safe protocol instead of failing 409 merely
            # because the DOM experiment was disabled.
            "a": composer_submission_protocol_ready(),
            "b": composer_caps.model_panel_v2,
            "c": composer_caps.thinking_effort,
            "references": reference_start is not None,
        }[body.model_control_batch]
        rejection_code: str | None = None
        if not contract_enabled:
            rejection_code = "submission_contract_inactive"
        elif body.submission_kind not in {"start", "replay"}:
            # Steer has its own atomic buffer/cancel path above. Never
            # downgrade an unknown discriminated kind into a fresh start Turn.
            rejection_code = "submission_kind_unavailable"
        if rejection_code is not None:
            rejection = {
                "http_status": status.HTTP_409_CONFLICT,
                "detail": {"error": {"code": rejection_code}},
            }
            await _submission_store.finalize_rejected_chat_submission(
                team_id=submission_team_id,
                user_id=resolved_user_id,
                client_submission_id=submission_id,
                workspace_id=body.workspace_id,
                requested_session_id=body.session_id,
                submission_kind=body.submission_kind,
                submission_contract_version=body.submission_contract_version,
                model_control_batch=body.model_control_batch,
                request_fingerprint=_submission_fingerprint_value,
                lease_owner=_submission_claim.lease_owner,
                lease_version=_submission_claim.lease_version,
                response=rejection,
            )
            if rejection_code == "submission_contract_inactive":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"error": {"code": "submission_contract_inactive"}},
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": {"code": "submission_kind_unavailable"}},
            )

    if _plan_exec_id and _plan_rejection is not None:
        await reject_plan(*_plan_rejection)

    # P4.5.25 -> byo-no-clone: make BYO LLM/KB effective. Configured team llm_config attaches the team router / KB
    # retriever as **per-turn overrides on turn_req** (team_router / team_kb_retriever). turn_runner creates
    # a fresh subagent_runner and overrides ctx.router/kb_retriever. **Never dataclasses.replace the shared
    # Orchestrator singleton**: that loses its dynamic sidecars (subagent_runner / plan_store /
    # scheduled_store / multi_agent_registry / artifact_fetch_secret / product_terms_catalog），
    # crashing every BYO team's subagent_spawn with "subagent_runner/bus not configured on context".
    if resolved_team_id:
        _byo_overrides: dict[str, Any] = {}
        team_router = _team_model_runtime.router if _team_model_runtime is not None else None
        _byo_overrides.update(
            await _subagent_draft_turn_overrides(
                request,
                team_id=resolved_team_id,
                team_router=team_router,
            ),
        )
        if _byo_overrides:
            from dataclasses import replace as _dc_replace

            # override on the REQUEST (not the shared orchestrator). Steer-drain continuations
            # inherit these via dataclasses.replace(req) (~L1500); clarification-resume re-resolves
            # them (clarifications.py + feishu build_resume_turn_request → typed team runtime
            # / _resolve_team_router). Background scheduled / plan entry points now resolve the
            # same typed runtime before constructing their TurnRequest.
            turn_req = _dc_replace(turn_req, **_byo_overrides)

    if resolved_team_id and resolved_user_id and body.workspace_id:
        from dataclasses import replace as _coach_replace

        from bowhead_gateway.agent_coach_resources import AgentCoachResources
        from bowhead_gateway.routes.auth import _current_user

        coach_user = await _current_user(request)
        if coach_user.id == resolved_user_id and coach_user.team_id == resolved_team_id:
            turn_req = _coach_replace(
                turn_req,
                agent_coach_resources=AgentCoachResources(
                    request, coach_user, body.workspace_id, turn_req.session_id, canvas_target=_coach_canvas_target
                ),
            )

    if draft_editor is not None:
        from dataclasses import replace as _draft_replace

        turn_req = _draft_replace(turn_req, subagent_draft_editor=draft_editor, agent_draft_target=draft_target)

    if body.context_maintenance is not None:
        from bowhead_core._db import open_shared_ctx
        from bowhead_core.chat_turn_source import ChatTurnUserSource
        from bowhead_core.context_action import ContextActionRejected, validate_context_action_in_tx
        from bowhead_router.router import UserModelPref

        maintenance_router = turn_req.team_router or orchestrator.router
        if (
            maintenance_router.supports_context_maintenance(
                "lead", "lead", UserModelPref.from_request_fields(turn_req.preferred_provider, turn_req.preferred_model)
            )
            is not True
        ):
            raise HTTPException(status_code=409, detail={"error": {"code": "context_maintenance_unsupported"}})
        try:
            async with open_shared_ctx(orchestrator.session_store.db_path) as db:
                await db.execute("BEGIN")
                await validate_context_action_in_tx(
                    db,
                    source=ChatTurnUserSource(
                        turn_id=turn_req.turn_id,
                        session_id=session_id,
                        user_id=resolved_user_id,
                        workspace_id=body.workspace_id,
                        user_text="",
                        attachment_ids=(),
                        attachment_refs=(),
                        parts=None,
                        context_maintenance=body.context_maintenance,
                    ),
                )
        except ContextActionRejected as error:
            raise HTTPException(status_code=409, detail={"error": {"code": error.code}}) from error

    # Claim only after all BYO/model setup that may reject the request, but
    # before terminal admission so concurrent approvals cannot both create a
    # Turn. Every later pre-spawn failure must release this claim.
    _plan_claimed = False
    if _plan_exec_id:
        _ps = getattr(request.app.state, "plan_store", None)
        if _ps is not None:
            _plan_claimed = await _ps.try_claim_running(
                _plan_exec_id,
                turn_req.turn_id,
                expected_rev=_plan.current_rev,
            )
            if not _plan_claimed and _terminal_v1_admission_enabled:
                assert action_identity is not None
                exact_claim = await _ps.get_execution_claim_status(
                    _plan_exec_id,
                    turn_req.turn_id,
                )
                if exact_claim is not None:
                    same_action = await _read_web_chat_action(
                        request,
                        user_id=resolved_user_id,
                        team_id=resolved_team_id,
                        identity=action_identity,
                    )
                    if same_action is not None:
                        request.state.composer_send_result_recorded = True
                        terminal_factory = getattr(
                            request.app.state,
                            "terminal_runtime_factory",
                            None,
                        )
                        wake = getattr(
                            terminal_factory,
                            "wake_admitted_web_turn",
                            None,
                        )
                        if callable(wake):
                            try:
                                await wake(same_action.scope)
                            except Exception as wake_error:
                                _log.warning(
                                    "terminal.plan_action_replay_wake_failed turn=%s err=%s",
                                    same_action.scope.turn_id,
                                    type(wake_error).__name__,
                                )
                        return await _replay_existing_web_action(
                            body,
                            request,
                            identity=action_identity,
                            lookup=same_action,
                            user_id=resolved_user_id,
                            team_id=resolved_team_id,
                            text=text,
                            session_id=session_id,
                            effective_attachment_ids=(effective_attachment_ids),
                            log=_log,
                        )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={
                            "error": {
                                "code": "terminal_dependency_recovering",
                            }
                        },
                    )
            if not _plan_claimed:
                latest_plan = await _ps.get_plan(
                    _plan_exec_id,
                    user_id=resolved_user_id,
                    workspace_id=body.workspace_id,
                )
                plan_claim_error = (
                    "plan_not_approvable" if latest_plan is None or latest_plan.current_rev != _plan.current_rev else "plan_already_running"
                )
                if _submission_claim is not None:
                    assert _submission_store is not None
                    rejection = {
                        "http_status": status.HTTP_409_CONFLICT,
                        "detail": {"error": {"code": plan_claim_error}},
                    }
                    await _submission_store.finalize_rejected_chat_submission(
                        team_id=resolved_team_id or "",
                        user_id=resolved_user_id,
                        client_submission_id=str(body.client_submission_id),
                        workspace_id=body.workspace_id,
                        requested_session_id=body.session_id,
                        submission_kind=body.submission_kind,
                        submission_contract_version=body.submission_contract_version,
                        model_control_batch=body.model_control_batch,
                        request_fingerprint=_submission_fingerprint_value,
                        lease_owner=_submission_claim.lease_owner,
                        lease_version=_submission_claim.lease_version,
                        response=rejection,
                    )
                raise chat_send_http_error(plan_claim_error)

    async def _release_plan_claim() -> None:
        nonlocal _plan_claimed
        if not _plan_claimed or not _plan_exec_id:
            return

        async def _release_exact_claim() -> None:
            nonlocal _plan_claimed
            try:
                plan_store = getattr(request.app.state, "plan_store", None)
                if plan_store is not None and hasattr(
                    plan_store,
                    "release_execution_claim",
                ):
                    await plan_store.release_execution_claim(
                        _plan_exec_id,
                        turn_req.turn_id,
                    )
            except Exception:  # recover_orphans remains the crash backstop  # noqa: S110
                return
            _plan_claimed = False

        release_task = asyncio.create_task(
            _release_exact_claim(),
            name=f"chat_plan_claim_release:{turn_req.turn_id}",
        )
        cancellation: asyncio.CancelledError | None = None
        while not release_task.done():
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
        release_task.result()
        if cancellation is not None:
            raise cancellation

    # Continue button (agent run limits, phase 2 §6): record which saved helper runs this turn
    # resumes, with the user message and before any worker starts, so Lead's first step and a
    # recovered run read the same request. Ownership is checked when each run is claimed.
    _resume_ids = continue_checkpoint_ids(body.client_info)
    if _resume_ids and orchestrator.session_store is not None:
        from bowhead_core.subagent_resume import record_requests

        try:
            await record_requests(orchestrator.session_store.db_path, turn_id=turn_req.turn_id, checkpoint_ids=_resume_ids)
        except Exception as exc:  # noqa: BLE001 — Lead can still choose the supported resume path.
            _log.warning("subagent_resume.request_record_failed turn=%s err=%s", turn_req.turn_id, exc)

    # Terminal Protocol v1 admission happens after all request gates and before
    # any legacy Turn row/bus write.  Writer ON with missing runtime is a hard
    # readiness failure; it must never fall back to the legacy writer.
    terminal_runtime = None
    legacy_bus = None
    if _terminal_v1_admission_enabled:
        terminal_factory = getattr(
            request.app.state,
            "terminal_runtime_factory",
            None,
        )
        if terminal_factory is None:
            await _release_plan_claim()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": {"code": "terminal_protocol_writer_not_ready"}},
            )
        try:
            from bowhead_core.chat_turn_source import ChatTurnUserSource

            request_id = body.client_info.get("request_id") or body.client_info.get("message_id") or turn_req.turn_id
            locale = language_context.ui_language if language_context is not None else "en-US"
            terminal_runtime = await terminal_factory.admit_web_turn(
                turn_req,
                request_id=request_id,
                presentation_locale=str(locale),
                user_source=ChatTurnUserSource(
                    context_maintenance=turn_req.context_maintenance,
                    turn_id=turn_req.turn_id,
                    session_id=turn_req.session_id,
                    user_id=turn_req.user_id,
                    workspace_id=turn_req.workspace_id,
                    user_text=turn_req.text,
                    attachment_ids=tuple(effective_attachment_ids),
                    attachment_refs=tuple(effective_attachment_refs),
                    parts=(tuple(effective_source_parts) if effective_source_parts is not None else None),
                ),
                create_session_if_missing=(body.session_id is None),
                require_existing_v1_history=bool(_session_protocol_facts is not None and _session_protocol_facts.has_v1_history),
            )
        except HTTPException:
            raise
        except asyncio.CancelledError as exc:
            # Readiness cancellation is explicitly pre-admission. A Store
            # admission cancellation has unknown commit outcome and therefore
            # keeps the exact Plan fence for Terminal recovery.
            if not getattr(exc, "admission_may_have_committed", True):
                await _release_plan_claim()
            raise
        except Exception as exc:
            from bowhead_core.terminal_runtime_factory import (
                TerminalRuntimeAdmissionError,
            )

            # Once Terminal admission begins, a raised exception may be a
            # commit-ACK ambiguity. Keep the exact Plan fence; protocol
            # recovery either owns the persisted generation or Plan orphan
            # maintenance releases a genuinely pre-commit claim after grace.
            if isinstance(exc, TerminalRuntimeAdmissionError) and not exc.admission_may_have_committed:
                await _release_plan_claim()
            raise _terminal_admission_http_error(exc) from exc
        from dataclasses import replace as _dc_replace

        turn_req = _dc_replace(turn_req, terminal_runtime=terminal_runtime)
    else:
        # Legacy producer and SSE subscriber must see the same pre-created bus.
        try:
            from bowhead_gateway.turn_service import (
                admit_legacy_chat_execution,
            )

            legacy_bus = await admit_legacy_chat_execution(
                orchestrator,
                turn_req,
            )
            if turn_req.team_id and orchestrator.session_store is not None:
                from os import environ

                from bowhead_core.agent_collaboration import CollaborationStore
                from bowhead_core.chat_reference_storage import ReferenceCipher

                turn_req.collaboration_store = CollaborationStore(
                    orchestrator.session_store.db_path,
                    cipher=ReferenceCipher(environ.get("BOWHEAD_FERNET_KEY", "")),
                )
        except BaseException:
            await _release_plan_claim()
            raise

    # The claim precedes admission to prevent concurrent approvals from both
    # creating a Turn. Admission can outlive the 30-second publication grace,
    # so renew and revalidate the exact token after either writer admits but
    # before persistence/spawn. A stale request that lost its claim must retire
    # its admitted producer and can never reach provider side effects.
    if _plan_claimed and _plan_exec_id:
        _plan_store = getattr(request.app.state, "plan_store", None)
        assert _plan_store is not None
        try:
            _plan_claim_current = await _plan_store.bind_execution_turn(
                _plan_exec_id,
                turn_req.turn_id,
            )
        except BaseException as _plan_fence_error:
            from bowhead_gateway.turn_service import (
                _retire_admitted_legacy_bus,
                _run_cleanup_through_repeated_cancellation,
            )

            async def _cleanup_uncertain_plan_fence() -> None:
                if terminal_runtime is not None:
                    await terminal_runtime.handoff_to_recovery(
                        reason_code="worker_start_failed",
                    )
                elif legacy_bus is not None:
                    await _retire_admitted_legacy_bus(
                        orchestrator.event_registry,
                        turn_id=turn_req.turn_id,
                        producer_bus=legacy_bus,
                    )
                    await _release_plan_claim()

            try:
                await _run_cleanup_through_repeated_cancellation(
                    _cleanup_uncertain_plan_fence,
                    task_name=f"chat_plan_fence_cleanup:{turn_req.turn_id}",
                )
            except BaseException as cleanup_error:
                _plan_fence_error.add_note(
                    f"Plan claim fence cleanup also failed: {cleanup_error!r}",
                )
            raise
        if not _plan_claim_current:
            from bowhead_gateway.turn_service import (
                _retire_admitted_legacy_bus,
                _run_cleanup_through_repeated_cancellation,
            )

            async def _retire_stale_plan_admission() -> None:
                if terminal_runtime is not None:
                    await terminal_runtime.handoff_to_recovery(
                        reason_code="worker_start_failed",
                    )
                elif legacy_bus is not None:
                    await _retire_admitted_legacy_bus(
                        orchestrator.event_registry,
                        turn_id=turn_req.turn_id,
                        producer_bus=legacy_bus,
                    )

            await _run_cleanup_through_repeated_cancellation(
                _retire_stale_plan_admission,
                task_name=f"chat_plan_stale_admission:{turn_req.turn_id}",
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": {"code": "plan_execution_claim_lost"}},
            )

    # Sprint chat-redesign-2026-05-21 Phase 1.1 (P0-6): persist the turn row BEFORE
    # spawning the worker so the stream endpoint sees a live turn. (The active-turn
    # marker — which routes a concurrent /send into the steer path — is set inside
    # spawn_turn below, just before the worker is created.)
    _send_response = ChatSendResponse(
        document_revision_target=turn_req.document_revision_target,
        agent_draft_target=turn_req.agent_draft_target if action_identity is not None else None,
        context_maintenance=turn_req.context_maintenance,
        session_id=session_id,
        turn_id=turn_req.turn_id,
        stream_url=f"/api/v1/chat/stream/{turn_req.turn_id}",
        terminal_protocol_version=(1 if terminal_runtime is not None else None),
    )
    try:
        if _submission_claim is not None:
            assert _submission_store is not None
            assert _submission_fingerprint_value is not None
            assert _submission_claim.lease_owner is not None
            assert _submission_claim.lease_version is not None
            await _submission_store.finalize_started_chat_submission(
                team_id=resolved_team_id or "",
                user_id=resolved_user_id,
                client_submission_id=str(body.client_submission_id),
                workspace_id=body.workspace_id,
                requested_session_id=body.session_id,
                actual_session_id=session_id,
                turn_id=turn_req.turn_id,
                user_text=turn_req.text or "",
                attachment_ids=effective_attachment_ids,
                submission_kind=body.submission_kind,
                submission_contract_version=body.submission_contract_version,
                model_control_batch=body.model_control_batch,
                request_fingerprint=_submission_fingerprint_value,
                lease_owner=_submission_claim.lease_owner,
                lease_version=_submission_claim.lease_version,
                response=_send_response.model_dump(exclude_none=True),
                attachment_refs=effective_attachment_refs,
                parts=effective_source_parts,
                reference_start=reference_start,
                document_revision_target=body.document_revision_target,
                context_maintenance=body.context_maintenance,
            )
        else:
            await _persist_turn_row_for_stream(
                orchestrator,
                turn_req,
                attachment_refs=effective_attachment_refs,
                source_parts=effective_source_parts,
            )
    except BaseException as _pte:
        persist_error = _pte
        _active_conflict = str(_pte) == "submission_session_active"
        from bowhead_core.context_action import ContextActionRejected
        from bowhead_core.documents.request import DocumentRequestError

        _maintenance_rejection = _pte.code if isinstance(_pte, ContextActionRejected) else None

        _revision_rejection = (
            _pte.code
            if body.document_revision_target is not None
            and isinstance(_pte, DocumentRequestError)
            and _pte.code in DOCUMENT_REVISION_ADMISSION_ERRORS
            else None
        )
        from bowhead_gateway.turn_service import (
            _retire_admitted_legacy_bus,
            _run_cleanup_through_repeated_cancellation,
            handoff_legacy_spawn_failure_to_recovery,
        )

        async def _cleanup_failed_persist() -> None:
            # The Turn insert/finalization may have committed even when the
            # caller observed an exception.
            # Publish Store recovery ownership before retiring the exact SSE
            # producer so refresh never finds an untracked running Turn.
            if terminal_runtime is not None:
                try:
                    await terminal_runtime.handoff_to_recovery(
                        reason_code="worker_start_failed",
                    )
                except BaseException as handoff_error:
                    persist_error.add_note(
                        f"terminal persist recovery handoff also failed: {handoff_error!r}",
                    )
                    _log.exception(
                        "chat.terminal_persist_recovery_handoff_failed turn=%s",
                        turn_req.turn_id,
                    )
            elif legacy_bus is not None:
                try:
                    await handoff_legacy_spawn_failure_to_recovery(
                        orchestrator.session_store,
                        turn_id=turn_req.turn_id,
                        session_id=turn_req.session_id,
                        owner_id=orchestrator.dispatch_owner_id,
                        error="turn persistence failed before worker start",
                        error_kind="dispatch_start_failed",
                    )
                except BaseException as handoff_error:
                    persist_error.add_note(
                        f"legacy persist recovery handoff also failed: {handoff_error!r}",
                    )
                    _log.exception(
                        "chat.turn_persist_recovery_handoff_failed turn=%s",
                        turn_req.turn_id,
                    )
                finally:
                    await _retire_admitted_legacy_bus(
                        orchestrator.event_registry,
                        turn_id=turn_req.turn_id,
                        producer_bus=legacy_bus,
                    )

            if (
                (_active_conflict or _revision_rejection is not None or _maintenance_rejection is not None)
                and _submission_claim is not None
                and _submission_store is not None
                and _submission_claim.lease_owner is not None
                and _submission_claim.lease_version is not None
            ):
                try:
                    await _submission_store.finalize_rejected_chat_submission(
                        team_id=resolved_team_id or "",
                        user_id=resolved_user_id,
                        client_submission_id=str(body.client_submission_id),
                        workspace_id=body.workspace_id,
                        requested_session_id=body.session_id,
                        submission_kind=body.submission_kind,
                        submission_contract_version=body.submission_contract_version,
                        model_control_batch=body.model_control_batch,
                        request_fingerprint=_submission_fingerprint_value,
                        lease_owner=_submission_claim.lease_owner,
                        lease_version=_submission_claim.lease_version,
                        response={
                            "http_status": status.HTTP_409_CONFLICT,
                            "detail": {"error": {"code": _maintenance_rejection or _revision_rejection or "turn_became_active"}},
                        },
                    )
                except Exception:
                    _log.exception(
                        "chat.submission_active_rejection_failed submission=%s",
                        body.client_submission_id,
                    )
            elif (
                terminal_runtime is None
                and _submission_claim is not None
                and _submission_store is not None
                and _submission_claim.lease_owner is not None
                and _submission_claim.lease_version is not None
            ):
                try:
                    await _submission_store.release_chat_submission_claim(
                        team_id=resolved_team_id or "",
                        user_id=resolved_user_id,
                        client_submission_id=str(body.client_submission_id),
                        lease_owner=_submission_claim.lease_owner,
                        lease_version=_submission_claim.lease_version,
                    )
                except Exception:  # receipt reconciliation is the backstop
                    _log.exception(
                        "chat.submission_claim_release_failed submission=%s",
                        body.client_submission_id,
                    )
            # Once Terminal admission committed, its durable generation owns
            # the Plan fence until Store recovery terminalizes the Turn. An
            # immediate release here would permit a second Plan execution.
            if terminal_runtime is None:
                await _release_plan_claim()

        try:
            await _run_cleanup_through_repeated_cancellation(
                _cleanup_failed_persist,
                task_name=f"chat_persist_cleanup:{turn_req.turn_id}",
            )
        except BaseException as cleanup_error:
            _pte.add_note(
                f"turn persist cleanup also failed: {cleanup_error!r}",
            )
            _log.exception(
                "chat.turn_persist_cleanup_failed turn=%s",
                turn_req.turn_id,
            )
        if not isinstance(_pte, Exception):
            raise
        if _maintenance_rejection is not None:
            raise HTTPException(409, detail={"error": {"code": _maintenance_rejection}}) from None
        if _revision_rejection is not None:
            raise HTTPException(409, detail={"error": {"code": _revision_rejection}}) from None
        if reference_start is not None:
            from bowhead_core.agent_execution_binding import AgentExecutionBindingError
            from bowhead_core.skill_read_resolution import SkillReadResolutionError

            if str(_pte) == "reference_entry_unavailable":
                raise HTTPException(409, detail={"error": {"code": "unsupported_entry"}}) from None
            if str(_pte) == "reference_attachment_unavailable":
                raise HTTPException(409, detail={"error": {"code": "attachment_unavailable"}}) from None
            if isinstance(_pte, (AgentExecutionBindingError, SkillReadResolutionError)) or (
                isinstance(_pte, ValueError) and str(_pte) in {"reference_scope_unavailable", "reference_policy_changed"}
            ):
                raise HTTPException(409, detail={"error": {"code": "resource_unavailable"}}) from None
        _log.exception("chat.turn_persist_before_stream_failed turn=%s err=%s", turn_req.turn_id, _pte)
        if _active_conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": {"code": "turn_became_active"}},
            ) from _pte
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "turn_store_unavailable"}},
        ) from _pte

    # Start Orchestrator concurrently: mark active_turn + spawn the worker through the channel-neutral
    # tail shared with Feishu. Before the response returns, the task is already pending in
    # the event loop. P4.5.10: pass user_store + team_id to cleanup task for post-run credit deduction.
    from bowhead_gateway.turn_service import (
        TurnDispatchAlreadyOwnedError,
        _retire_admitted_legacy_bus,
        _run_cleanup_through_repeated_cancellation,
        handoff_legacy_spawn_failure_to_recovery,
        legacy_spawn_failure_handoff_attempted,
        spawn_turn,
    )

    user_store_for_deduct = getattr(request.app.state, "user_store", None) if resolved_team_id else None

    async def _revalidate_plan_before_schedule() -> None:
        """Fence a stale Plan owner at the shared final scheduling boundary."""
        if not _plan_claimed or not _plan_exec_id:
            return
        plan_store = getattr(request.app.state, "plan_store", None)
        assert plan_store is not None
        try:
            claim_current = await plan_store.bind_execution_turn(
                _plan_exec_id,
                turn_req.turn_id,
            )
        except asyncio.CancelledError:
            # The route's existing Terminal cancellation owner performs the
            # single recovery handoff after spawn_turn unwinds.
            raise
        except BaseException as fence_error:
            if terminal_runtime is not None:

                async def _handoff_uncertain_terminal_fence() -> None:
                    await terminal_runtime.handoff_to_recovery(
                        reason_code="worker_start_failed",
                    )

                try:
                    await _run_cleanup_through_repeated_cancellation(
                        _handoff_uncertain_terminal_fence,
                        task_name=f"chat_plan_preworker_fence:{turn_req.turn_id}",
                    )
                except BaseException as handoff_error:
                    fence_error.add_note(
                        f"Terminal Plan fence handoff also failed: {handoff_error!r}",
                    )
            raise
        if claim_current:
            return
        claim_lost_error = RuntimeError("plan_execution_claim_lost")
        if terminal_runtime is not None:

            async def _handoff_stale_terminal_owner() -> None:
                await terminal_runtime.handoff_to_recovery(
                    reason_code="worker_start_failed",
                )

            try:
                await _run_cleanup_through_repeated_cancellation(
                    _handoff_stale_terminal_owner,
                    task_name=f"chat_plan_stale_preworker:{turn_req.turn_id}",
                )
            except BaseException as handoff_error:
                claim_lost_error.add_note(
                    f"Terminal stale-owner handoff also failed: {handoff_error!r}",
                )
        raise claim_lost_error

    try:
        await spawn_turn(
            orchestrator,
            turn_req,
            user_store=user_store_for_deduct,
            team_id=resolved_team_id,
            metrics_log=getattr(request.app.state, "metrics_log", None),
            entitlements=getattr(request.app.state, "entitlements", None),  # §3.4 steer re-check
            admitted_legacy_bus=legacy_bus,
            before_schedule=_revalidate_plan_before_schedule,
            reference_skill_sources=reference_start.skill_sources if reference_start is not None else None,
            reference_mcp_index=reference_start.mcp_index if reference_start is not None else None,
            reference_mcp_access=reference_start.mcp_access if reference_start is not None else None,
            reference_mcp_prompt=reference_start is not None and reference_start.mcp_prompt_selection is not None,
        )
    except BaseException as spawn_error:
        caught_spawn_error = spawn_error
        if getattr(turn_req, "terminal_runtime", None) is not None:
            # turn_service owns synchronous task-create failures and seals the
            # generation exactly once. Request cancellation can interrupt
            # before that owner starts, so only that path needs route handoff.
            if not isinstance(spawn_error, asyncio.CancelledError):
                if terminal_runtime is not None and terminal_runtime.bus.receipt is not None:
                    try:
                        await _run_cleanup_through_repeated_cancellation(
                            _release_plan_claim,
                            task_name=(f"chat_terminal_spawn_plan_release:{turn_req.turn_id}"),
                        )
                    except BaseException as cleanup_error:
                        spawn_error.add_note(
                            f"terminal spawn Plan release also failed: {cleanup_error!r}",
                        )
                if not isinstance(spawn_error, Exception):
                    raise
                if str(spawn_error) == "plan_execution_claim_lost":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": {
                                "code": "plan_execution_claim_lost",
                            }
                        },
                    ) from spawn_error
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "error": {
                            "code": "turn_dispatch_unavailable",
                        }
                    },
                ) from spawn_error

            async def _handoff_terminal_spawn_failure() -> None:
                assert terminal_runtime is not None
                await terminal_runtime.handoff_to_recovery(
                    reason_code="worker_start_failed",
                )

            try:
                await _run_cleanup_through_repeated_cancellation(
                    _handoff_terminal_spawn_failure,
                    task_name=f"chat_terminal_spawn_cleanup:{turn_req.turn_id}",
                )
            except BaseException as cleanup_error:
                spawn_error.add_note(
                    f"terminal spawn recovery handoff also failed: {cleanup_error!r}",
                )
                _log.exception(
                    "chat.terminal_spawn_recovery_handoff_failed turn=%s",
                    turn_req.turn_id,
                )
            raise
        if isinstance(spawn_error, TurnDispatchAlreadyOwnedError):
            # A same-receipt retry can lose the durable owner race after both
            # callers observed the accepted Turn. spawn_turn already retired
            # only this caller's bus; return the existing receipt and leave the
            # winner's Turn/dispatch untouched.
            async def _cleanup_duplicate_spawn() -> None:
                await _retire_admitted_legacy_bus(
                    orchestrator.event_registry,
                    turn_id=turn_req.turn_id,
                    producer_bus=legacy_bus,
                )
                await _release_plan_claim()

            await _run_cleanup_through_repeated_cancellation(
                _cleanup_duplicate_spawn,
                task_name=f"chat_duplicate_spawn_cleanup:{turn_req.turn_id}",
            )
            return _send_response
        handoff: str | None = None

        async def _cleanup_failed_spawn() -> None:
            nonlocal handoff
            # `spawn_turn` marks the exception only after its protected Store
            # child exists. If ownership was never established here, the route
            # must still resolve commit-outcome uncertainty before closing SSE.
            if not legacy_spawn_failure_handoff_attempted(caught_spawn_error):
                try:
                    handoff = await handoff_legacy_spawn_failure_to_recovery(
                        orchestrator.session_store,
                        turn_id=turn_req.turn_id,
                        session_id=turn_req.session_id,
                        owner_id=orchestrator.dispatch_owner_id,
                        error="turn dispatch failed before worker start",
                        error_kind="dispatch_start_failed",
                    )
                except BaseException as handoff_error:
                    caught_spawn_error.add_note(
                        f"route recovery handoff also failed: {handoff_error!r}",
                    )
                    # The exact owner lease or drain marker remains the
                    # maintenance backstop; never restore a route-local
                    # terminal writer here.
                    _log.exception(
                        "chat.dispatch_recovery_handoff_failed turn=%s",
                        turn_req.turn_id,
                    )
            # No worker exists to run the task-done fallback. Retire only this
            # admitted generation after the durable handoff attempt, otherwise
            # SSE can subscribe to an empty producer instead of Store truth.
            await _retire_admitted_legacy_bus(
                orchestrator.event_registry,
                turn_id=turn_req.turn_id,
                producer_bus=legacy_bus,
            )
            await _release_plan_claim()

        try:
            await _run_cleanup_through_repeated_cancellation(
                _cleanup_failed_spawn,
                task_name=f"chat_spawn_cleanup:{turn_req.turn_id}",
            )
        except BaseException as cleanup_error:
            spawn_error.add_note(
                f"route spawn cleanup also failed: {cleanup_error!r}",
            )
            _log.exception(
                "chat.dispatch_cleanup_failed turn=%s",
                turn_req.turn_id,
            )
        if not isinstance(spawn_error, Exception):
            raise
        if handoff == "foreign_active":
            return _send_response
        if str(spawn_error) == "plan_execution_claim_lost":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": {"code": "plan_execution_claim_lost"}},
            ) from spawn_error
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "turn_dispatch_unavailable"}},
        ) from spawn_error

    return _send_response


@router.post(
    "/send",
    response_model=ChatSendResponse,
    response_model_exclude_none=True,
    openapi_extra={"requestBody": {"content": {REFERENCE_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/ChatSendRequest"}}}}},
)
async def send(
    body: ChatSendRequest,
    request: Request,
    response: Response,
) -> ChatSendResponse:
    """Record exactly one server-owned result across the complete send route."""
    attachment_count = len(body.attachments)
    try:
        result = await _send_impl(body, request, response)
    except HTTPException as exc:
        _record_composer_send_result(
            request=request,
            body=body,
            attachment_count=attachment_count,
            result="server_error" if exc.status_code >= 500 else "rejected",
        )
        raise
    except Exception:
        _record_composer_send_result(
            request=request,
            body=body,
            attachment_count=attachment_count,
            result="server_error",
        )
        raise
    _record_composer_send_result(
        request=request,
        body=body,
        attachment_count=attachment_count,
        result="success",
    )
    return result


async def _require_submission_space_access(
    request: Request,
    *,
    user_id: str,
    workspace_id: str,
    operation: Any,
    expected_team_id: str | None = None,
    allow_dev_fallback: bool = True,
) -> None:
    """Apply current Space admission before exposing or abandoning a receipt."""
    from bowhead_gateway.access_context import AccessContextError, build_access_context
    from bowhead_gateway.routes.auth import _dev_user_fallback_enabled

    if allow_dev_fallback and _dev_user_fallback_enabled(request):
        return
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "authz_unavailable"}},
        )
    try:
        await build_access_context(
            user_store=user_store,
            access_store=getattr(request.app.state, "access_control_store", None),
            user_id=user_id,
            workspace_id=workspace_id,
            operation=operation,
            expected_team_id=expected_team_id,
        )
    except AccessContextError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": {"code": exc.code}},
        ) from exc


async def _require_reference_recovery_access(request: Request, *, receipt: Any, store: Any) -> None:
    """Reference receipt readers never inherit the legacy dev authorization bypass."""
    from bowhead_core.space_governance import SpaceOperation

    from bowhead_gateway.routes.auth import _current_user

    if receipt.model_control_batch != "references":
        return
    user = await _current_user(request)
    if user.id != receipt.user_id or user.team_id != receipt.team_id:
        raise HTTPException(status_code=403, detail={"error": {"code": "access_context_denied"}})
    await _require_submission_space_access(
        request,
        user_id=user.id,
        workspace_id=receipt.workspace_id,
        operation=SpaceOperation.READ_HISTORY,
        expected_team_id=user.team_id,
        allow_dev_fallback=False,
    )
    await _require_current_submission_auth_generation(request, store=store, user_id=user.id, allow_dev_fallback=False)


@router.get("/submissions/{client_submission_id}")
async def get_submission_receipt(
    client_submission_id: UUID4,
    request: Request,
    workspace_id: str = Query(min_length=1),
    requested_session_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Return only the caller's exact scope-bound public receipt."""
    from bowhead_core.space_governance import SpaceOperation

    from bowhead_gateway.routes.auth import resolve_user_or_stub

    user_id, team_id = await resolve_user_or_stub(request)
    await _require_submission_space_access(
        request,
        user_id=user_id,
        workspace_id=workspace_id,
        operation=SpaceOperation.READ_HISTORY,
    )
    store = getattr(request.app.state, "session_store", None)
    if store is None or not hasattr(store, "get_chat_submission_receipt"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "submission_store_unavailable"}},
        )
    await _require_current_submission_auth_generation(
        request,
        store=store,
        user_id=user_id,
    )
    receipt = await store.get_chat_submission_receipt(
        team_id=team_id or "",
        user_id=user_id,
        client_submission_id=str(client_submission_id),
        workspace_id=workspace_id,
        requested_session_id=requested_session_id,
    )
    if receipt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "submission_not_found"}},
        )
    await _require_reference_recovery_access(request, receipt=receipt, store=store)
    return {
        "client_submission_id": receipt.client_submission_id,
        "outcome": receipt.outcome,
        "response": receipt.response,
    }


@router.post("/submissions/{client_submission_id}/abandon")
async def abandon_submission(
    client_submission_id: UUID4,
    body: SubmissionAbandonRequest,
    request: Request,
) -> dict[str, Any]:
    """Durably fence an unresolved ID before the client unlocks its draft."""
    from bowhead_core.space_governance import SpaceOperation

    from bowhead_gateway.routes.auth import resolve_user_or_stub

    user_id, team_id = await resolve_user_or_stub(request)
    await _require_submission_space_access(
        request,
        user_id=user_id,
        workspace_id=body.workspace_id,
        operation=SpaceOperation.EXECUTE,
    )
    store = getattr(request.app.state, "session_store", None)
    if store is None or not hasattr(store, "abandon_chat_submission"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "submission_store_unavailable"}},
        )
    await _require_current_submission_auth_generation(
        request,
        store=store,
        user_id=user_id,
    )
    outcome = await store.abandon_chat_submission(
        team_id=team_id or "",
        user_id=user_id,
        client_submission_id=str(client_submission_id),
        workspace_id=body.workspace_id,
        requested_session_id=body.requested_session_id,
    )
    if outcome == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "submission_not_found"}},
        )
    if outcome == "abandoned":
        return {
            "client_submission_id": str(client_submission_id),
            "outcome": "abandoned",
        }
    await _require_reference_recovery_access(request, receipt=outcome, store=store)
    return {
        "client_submission_id": outcome.client_submission_id,
        "outcome": outcome.outcome,
        "response": outcome.response,
    }


@router.post("/submissions/{client_submission_id}/cancel")
async def cancel_queued_reference_submission(
    client_submission_id: UUID4,
    body: QueuedReferenceCancelRequest,
    request: Request,
) -> dict[str, Any]:
    """Cancel exactly one accepted reference follow-up that is still queued."""
    from bowhead_core.space_governance import SpaceOperation

    from bowhead_gateway.routes.auth import _current_user

    user = await _current_user(request)
    await _require_submission_space_access(
        request,
        user_id=user.id,
        workspace_id=body.workspace_id,
        operation=SpaceOperation.EXECUTE,
        expected_team_id=user.team_id or "",
        allow_dev_fallback=False,
    )
    store = getattr(request.app.state, "session_store", None)
    if store is None or not hasattr(store, "cancel_queued_reference_submission"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "submission_store_unavailable"}},
        )
    auth_session_generation_hash = await _require_current_submission_auth_generation(
        request,
        store=store,
        user_id=user.id,
        allow_dev_fallback=False,
    )
    try:
        outcome = await store.cancel_queued_reference_submission(
            team_id=user.team_id or "",
            user_id=user.id,
            client_submission_id=str(client_submission_id),
            workspace_id=body.workspace_id,
            session_id=body.requested_session_id,
            auth_session_generation_hash=auth_session_generation_hash,
        )
    except ValueError as exc:
        if str(exc) == "submission_auth_generation_inactive":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": {"code": "auth_session_generation_inactive"}},
            ) from exc
        if str(exc).startswith("reference_queue_cancel_"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": {"code": "reference_queue_cancel_unavailable"}},
            ) from exc
        raise
    if outcome == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "submission_not_found"}},
        )
    if outcome == "materialized":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": {"code": "reference_submission_already_materialized"}},
        )
    return {
        "client_submission_id": str(client_submission_id),
        "outcome": "cancelled",
    }


@router.get("/sessions/{session_id}/reference-queue/{client_submission_id}/evidence")
async def get_queued_reference_evidence(
    session_id: str,
    client_submission_id: UUID4,
    request: Request,
    response: Response,
    workspace_id: Annotated[str, Query(min_length=1)],
    include_content: bool = Query(default=False),
) -> dict[str, Any]:
    """Locate the original accepted parent, then reuse its private source reader."""
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.space_governance import SpaceOperation

    from bowhead_gateway.routes.auth import _current_user
    from bowhead_gateway.routes.turns import _reference_projection

    response.headers.update(_CHAT_ACTION_CACHE_HEADERS)
    try:
        user = await _current_user(request)
        await _require_submission_space_access(
            request,
            user_id=user.id,
            workspace_id=workspace_id,
            operation=SpaceOperation.READ_HISTORY,
            expected_team_id=user.team_id or "",
            allow_dev_fallback=False,
        )
        store = request.app.state.session_store
        # Queue lineage is immutable and outlives the short submission receipt.
        # The session's current active turn may already be a different child.
        async with (
            open_shared_ctx(request.app.state.user_store.db_path) as db,
            db.execute(
                "SELECT accepted_parent_turn_id FROM chat_reference_queued_inputs "
                "WHERE team_id=? AND user_id=? AND workspace_id=? AND session_id=? AND client_submission_id=?",
                (user.team_id, user.id, workspace_id, session_id, str(client_submission_id)),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": {"code": "submission_not_found"}})
        await _require_current_submission_auth_generation(request, store=store, user_id=user.id, allow_dev_fallback=False)
        projection = await _reference_projection(row[0], request, include_content=include_content, submission_id=str(client_submission_id))
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["ETag"] = f'"{projection["projection_etag"]}"'
        return projection
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **_CHAT_ACTION_CACHE_HEADERS}
        raise
    except Exception as exc:
        _MODULE_LOG.exception("reference_queue_evidence_failed")
        raise HTTPException(
            status_code=503,
            detail={"error": {"code": "reference_projection_unavailable"}},
            headers=_CHAT_ACTION_CACHE_HEADERS,
        ) from exc


@router.get(
    "/sessions/{session_id}/reference-queue",
    response_model=QueuedReferenceQueueResponse,
)
async def list_queued_reference_submissions(
    session_id: str,
    request: Request,
    response: Response,
    workspace_id: Annotated[str, Query(min_length=1)],
) -> QueuedReferenceQueueResponse:
    """Read the current user's ordered, accepted reference follow-ups."""
    import os

    from bowhead_core.chat_reference_storage import ChatReferenceStore, ReferenceCipher
    from bowhead_core.space_governance import SpaceOperation

    from bowhead_gateway.routes.auth import _current_user

    response.headers.update(_CHAT_ACTION_CACHE_HEADERS)
    try:
        user = await _current_user(request)
        await _require_submission_space_access(
            request,
            user_id=user.id,
            workspace_id=workspace_id,
            operation=SpaceOperation.READ_HISTORY,
            expected_team_id=user.team_id or "",
            allow_dev_fallback=False,
        )
        store = getattr(request.app.state, "session_store", None)
        if store is None or not hasattr(store, "list_queued_reference_submissions"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": {"code": "submission_store_unavailable"}},
            )
        auth_session_generation_hash = await _require_current_submission_auth_generation(
            request,
            store=store,
            user_id=user.id,
            allow_dev_fallback=False,
        )
        try:
            reference_store = ChatReferenceStore(
                ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", "")),
            )
        except (TypeError, ValueError):
            reference_store = None
        try:
            items = await store.list_queued_reference_submissions(
                team_id=user.team_id or "",
                user_id=user.id,
                workspace_id=workspace_id,
                session_id=session_id,
                auth_session_generation_hash=auth_session_generation_hash,
                reference_store=reference_store,
            )
        except ValueError as exc:
            if str(exc) == "submission_auth_generation_inactive":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"error": {"code": "auth_session_generation_inactive"}},
                ) from exc
            if str(exc).startswith("reference_"):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"error": {"code": "reference_queue_unavailable"}},
                ) from exc
            raise
        return QueuedReferenceQueueResponse(
            items=[
                QueuedReferenceSubmissionResponse(
                    client_submission_id=UUID(item.client_submission_id),
                    display_text=item.display_text,
                    resources=[
                        QueuedReferenceResourceResponse(
                            kind=resource.kind,
                            label=resource.label,
                        )
                        for resource in item.resources
                    ],
                    attachment_count=item.attachment_count,
                    accepted_at=datetime.fromisoformat(item.accepted_at),
                )
                for item in items
            ]
        )
    except HTTPException as exc:
        exc.headers = {
            **(exc.headers or {}),
            **_CHAT_ACTION_CACHE_HEADERS,
        }
        raise
    except Exception as exc:
        _MODULE_LOG.exception("reference_queue_list_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "reference_queue_unavailable"}},
            headers=_CHAT_ACTION_CACHE_HEADERS,
        ) from exc


_CHAT_ACTION_CACHE_HEADERS = {"Cache-Control": "no-store"}


def _chat_action_http_error(http_status: int, code: str) -> HTTPException:
    return HTTPException(
        status_code=http_status,
        detail={"error": {"code": code}},
        headers=_CHAT_ACTION_CACHE_HEADERS,
    )


async def _read_web_chat_action(
    request: Request,
    *,
    user_id: str,
    team_id: str | None,
    identity: ChatActionRequestIdentity,
) -> WebChatActionLookup | None:
    """Point-read one actor-bound action and recheck its current Space visibility."""

    if not team_id:
        return None
    store = getattr(request.app.state, "terminal_protocol_store", None)
    reader = getattr(store, "read_web_chat_action", None)
    if not callable(reader):
        raise _chat_action_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "terminal_dependency_recovering",
        )
    from bowhead_core.terminal_protocol import TerminalProtocolFutureVersionError
    from bowhead_core.terminal_protocol_store import (
        WebChatActionFutureVersionError,
        WebChatActionMalformedError,
    )

    protocol_error: Exception | None = None
    error_access_scope = None
    try:
        lookup = await reader(
            action_id=identity.action.action_id,
            action_kind=identity.action.kind,
            actor=user_id,
            team_id=team_id,
            turn_id=(identity.action.start_turn_id if identity.action.kind == "start" else identity.expected_turn_id),
            user_message_id=(identity.action.user_message_id if identity.action.kind == "steer" else None),
            session_id=(identity.session_id if identity.action.kind == "steer" else None),
            execution_generation=(identity.expected_execution_generation if identity.action.kind == "steer" else None),
        )
    except (
        WebChatActionFutureVersionError,
        WebChatActionMalformedError,
    ) as exc:
        lookup = None
        error_access_scope = exc.access_scope
        protocol_error = exc
    except HTTPException:
        raise
    except Exception as exc:
        raise _chat_action_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "terminal_dependency_recovering",
        ) from exc
    if lookup is None and protocol_error is None:
        return None
    if lookup is None and error_access_scope is None:
        raise _chat_action_http_error(
            status.HTTP_404_NOT_FOUND,
            "chat_action_not_found",
        )

    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        raise _chat_action_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "authz_lookup_failed",
        )
    from bowhead_core.space_governance import SpaceOperation

    from bowhead_gateway.access_context import (
        AccessContextError,
        build_access_context,
    )

    try:
        workspace_id = lookup.workspace_id if lookup is not None else error_access_scope.workspace_id
        expected_team_id = lookup.scope.team_id if lookup is not None else error_access_scope.team_id
        await build_access_context(
            user_store=user_store,
            access_store=getattr(request.app.state, "access_control_store", None),
            user_id=user_id,
            workspace_id=workspace_id,
            operation=SpaceOperation.READ_HISTORY,
            expected_team_id=expected_team_id,
        )
    except AccessContextError as exc:
        raise _chat_action_http_error(
            status.HTTP_404_NOT_FOUND,
            "chat_action_not_found",
        ) from exc
    except Exception as exc:
        raise _chat_action_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "authz_lookup_failed",
        ) from exc
    if protocol_error is not None:
        if isinstance(protocol_error, TerminalProtocolFutureVersionError):
            raise _chat_action_http_error(
                status.HTTP_409_CONFLICT,
                "terminal_protocol_future_version",
            ) from protocol_error
        raise _chat_action_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "terminal_dependency_recovering",
        ) from protocol_error
    if lookup is None:
        raise _chat_action_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "terminal_dependency_recovering",
        )
    return lookup


@router.get(
    "/actions/{action_id}",
    response_model=ChatActionLookupResponse,
)
async def get_chat_action(
    action_id: str,
    request: Request,
    response: Response,
    session_id: str | None = Query(default=None),
    expected_turn_id: str | None = Query(default=None),
    expected_execution_generation: str | None = Query(default=None),
) -> ChatActionLookupResponse:
    """Read one canonical Terminal action outcome without creating protocol state."""

    try:
        query_items = list(request.query_params.multi_items())
        query_keys = [key for key, _value in query_items]
        allowed_query_keys = {
            "session_id",
            "expected_turn_id",
            "expected_execution_generation",
        }
        if any(key not in allowed_query_keys for key in query_keys) or len(query_keys) != len(set(query_keys)):
            raise ValueError("chat action lookup query is not canonical")
        action = parse_chat_action_id(action_id)
        if action.kind == "start" and any(
            value is not None
            for value in (
                session_id,
                expected_turn_id,
                expected_execution_generation,
            )
        ):
            raise ValueError("start action lookup contains steer-only scope")
        client_info = {
            "request_id": action.action_id,
            "action_kind": action.kind,
        }
        if expected_turn_id is not None:
            client_info["expected_turn_id"] = expected_turn_id
        if expected_execution_generation is not None:
            client_info["expected_execution_generation"] = expected_execution_generation
        identity = parse_chat_action_client_info(
            client_info,
            session_id=session_id,
        )
        if identity is None:
            raise ValueError("chat action identity is missing")
    except ValueError as exc:
        raise _chat_action_http_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "request_validation_failed",
        ) from exc

    from bowhead_gateway.routes.auth import _current_user

    try:
        user = await _current_user(request)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **_CHAT_ACTION_CACHE_HEADERS}
        raise
    lookup = await _read_web_chat_action(
        request,
        user_id=user.id,
        team_id=user.team_id,
        identity=identity,
    )
    if lookup is None:
        raise _chat_action_http_error(
            status.HTTP_404_NOT_FOUND,
            "chat_action_not_found",
        )
    response.headers["Cache-Control"] = "no-store"
    return ChatActionLookupResponse(
        agent_draft_target=AgentDraftSendTarget.model_validate(lookup.agent_draft_target) if lookup.agent_draft_target else None,
        context_maintenance=ContextMaintenanceAction.model_validate(lookup.context_maintenance) if lookup.context_maintenance is not None else None,
        action_id=lookup.action_id,
        action_kind=lookup.action_kind,
        outcome=("admitted" if lookup.action_kind == "start" else "accepted"),
        session_id=lookup.scope.session_id,
        turn_id=lookup.scope.turn_id,
        execution_generation=lookup.scope.generation,
        terminal_protocol_version=1,
        stream_url=f"/api/v1/chat/stream/{lookup.scope.turn_id}",
    )


# ─── clarification/approval resume — replay filter ───────────────────────────
# A turn that PAUSES for HITL persists a `done` event carrying a non-terminal
# status (clarification_pending / approval_pending). The FE (use-turn-runner)
# treats ANY `done` as a terminal stream event → markTerminal() disarms its
# reconnect + stall-watchdog. On the ORIGINAL live pass that is correct (the turn
# stopped; show the HITL card). But on a RESUME reconnect, /chat/stream replays
# history INCLUDING that paused `done`; markTerminal there strands the (often
# minutes-long) resumed pass — one transport blip during a quiet gap and the
# final answer never lands ("点击 clarification 没返回"; prod turn
# t_f2f6a88ad3cc — 210s resume, 51s/71s silent gaps, full report generated +
# persisted but never delivered to the browser).
_PAUSED_DONE_STATUSES = frozenset({"clarification_pending", "approval_pending"})
# #9 (dingsiyan 2026-07-07): every TRULY-terminal turn status (turn is done and will
# not resume). The stream-reconnect synthetic-terminal short-circuit must cover ALL
# of these — a reconnect for a cancelled/aborted turn whose `done` row was lost (emit
# persistence is best-effort log_sink) otherwise falls through to a closed bus and
# sticks "reconnecting" until the 10-min give-up. Paused statuses (clarification /
# approval) are deliberately EXCLUDED: the turn may resume, so the stream must keep
# subscribing rather than short-circuit. Mirror of session_store's known-terminal set
# minus the paused ones.
_TERMINAL_TURN_STATUSES = frozenset({"completed", "failed", "cancelled", "aborted"})
_ROOT_SEMANTIC_TERMINAL_STATUS = {
    "turn_failed": "failed",
    "turn_cancelled": "cancelled",
    "turn_aborted": "aborted",
}


def _is_transport_terminal_event(event_type: str, payload: dict[str, Any]) -> bool:
    """Classify root transport terminals, excluding legacy peer errors."""
    return event_type == "done" or (event_type == "error" and not payload.get("parent_agent_id"))


def _canonical_turn_status_for_wire_event(
    event_type: str,
    payload: dict[str, Any],
) -> str | None:
    if event_type == "error":
        return None if payload.get("parent_agent_id") else "failed"
    if event_type != "done":
        return None
    wire_status = payload.get("status")
    if not isinstance(wire_status, str):
        return None
    return normalize_legacy_turn_status(wire_status)


def _replay_rows_for_stream(
    past: list[Any],
    *,
    canonical_turn_status: str | None = None,
    validate_terminal_status: bool = False,
) -> list[Any]:
    """Filter persisted turn events for SSE replay on (re)connect.

    Drops a paused `done` marker ONLY when later events exist after it — i.e. the
    turn 已续跑过这个 pause, so the marker is stale and must not re-terminate the FE
    stream. A paused `done` that is the LAST persisted event is kept: the turn is
    genuinely paused awaiting the user, and the FE should settle the HITL card.
    When a canonical Turn status was read for the production stream, only the
    last root terminal that agrees with that status is replayed. This prevents
    an interrupted writer's stale terminal row from contradicting the Store.
    If the Store read is unavailable, root terminals fail closed; non-terminal
    progress can still be replayed or joined to a live producer.
    """
    last_idx = len(past) - 1
    canonical_terminal_idx: int | None = None
    canonical_semantic_idx: int | None = None
    if validate_terminal_status and canonical_turn_status is not None:
        normalized_canonical_status = normalize_legacy_turn_status(
            canonical_turn_status,
        )
        for i, row in enumerate(past):
            payload = getattr(row, "payload", None) or {}
            event_type = str(getattr(row, "event_type", ""))
            if (
                _canonical_turn_status_for_wire_event(
                    event_type,
                    payload,
                )
                == normalized_canonical_status
            ):
                canonical_terminal_idx = i
            if _ROOT_SEMANTIC_TERMINAL_STATUS.get(event_type) == normalized_canonical_status:
                canonical_semantic_idx = i
    out: list[Any] = []
    for i, row in enumerate(past):
        payload = getattr(row, "payload", None) or {}
        # Older SubagentRunner versions persisted peer-scoped ``error`` rows.
        # The frontend treats that event name as a root failure, so replaying it
        # would overwrite a later durable completed status. Runtime now emits a
        # non-terminal peer failure shape; filter only the legacy scoped row.
        if getattr(row, "event_type", None) == "error" and payload.get("parent_agent_id"):
            continue
        if (
            validate_terminal_status
            and _is_transport_terminal_event(
                str(getattr(row, "event_type", "")),
                payload,
            )
            and i != canonical_terminal_idx
        ):
            continue
        if validate_terminal_status and str(getattr(row, "event_type", "")) in _ROOT_SEMANTIC_TERMINAL_STATUS and i != canonical_semantic_idx:
            continue
        if (
            i < last_idx
            and getattr(row, "event_type", None) == "done"
            and normalize_legacy_turn_status(payload.get("status")) in _PAUSED_DONE_STATUSES
            and not (validate_terminal_status and i == canonical_terminal_idx)
        ):
            continue
        out.append(row)
    return out


def _terminal_v1_wire_payload(
    *,
    payload: dict[str, Any],
    event_id: str,
    event_seq: int,
    generation: int,
    terminal_receipt_id: str | None,
    terminal_current: bool,
) -> dict[str, Any]:
    """Add transport identity without overwriting business payload cursors."""

    from bowhead_core.terminal_protocol import TerminalProtocolMalformedError

    projected = dict(payload)
    identities: dict[str, object] = {
        "terminal_protocol_version": 1,
        "event_id": event_id,
        "event_seq": event_seq,
        "evt_seq": event_seq,
        "generation": generation,
        "execution_generation": generation,
        "terminal_current": terminal_current,
        # ``None`` is also authoritative: an ordinary payload may not smuggle
        # a receipt binding into its transport envelope.
        "terminal_receipt_id": terminal_receipt_id,
    }
    for key, expected in identities.items():
        if key in projected and projected[key] != expected:
            raise TerminalProtocolMalformedError(
                f"terminal event transport identity changed: {key}",
            )
        projected[key] = expected
    return projected


async def _terminal_v1_stream_response(
    *,
    request: Request,
    turn_id: str,
    team_id: str,
    session_id: str,
    viewer: str,
    client_cursor: int,
) -> EventSourceResponse:
    """Serve protocol-v1 from one authorized checkpoint/replay/live handoff."""

    from bowhead_core.space_governance import SpaceOperation
    from bowhead_core.terminal_protocol import (
        ConsumerProfile,
        TerminalClassification,
        TerminalEvent,
        TerminalProtocolMalformedError,
        classify_terminal,
    )
    from bowhead_core.terminal_protocol_store import TerminalReadContext
    from bowhead_core.terminal_runtime_factory import (
        terminal_auth_resolve_context_values,
    )
    from bowhead_core.terminal_stream import AuthorizedFence, TerminalReplayHandoff

    from bowhead_gateway.routes.auth import assert_turn_access
    from bowhead_gateway.routes.turns import _require_turn_access

    protocol_store = request.app.state.terminal_protocol_store
    buses = getattr(request.app.state, "terminal_generation_bus_registry", None)
    if buses is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "terminal_dependency_recovering"}},
        )

    resolve_context = terminal_auth_resolve_context_values(
        team_id=team_id,
        session_id=session_id,
        user_id=viewer,
        resource_ref=turn_id,
    )

    async def authorize() -> AuthorizedFence:
        # Re-run both the Team/Space lifecycle gate and the owner-bound SSE
        # check. The immutable vector then binds the same authorization state
        # to checkpoint promotion and live frame enqueue.
        current_user = await _require_turn_access(
            turn_id,
            request,
            operation=SpaceOperation.READ_HISTORY,
        )
        current_viewer = await assert_turn_access(
            request,
            turn_id,
            getattr(request.app.state, "session_store", None),
        )
        if current_user.id != viewer or current_viewer != viewer:
            raise TerminalProtocolMalformedError(
                "terminal stream viewer changed during authorization",
            )
        vector = await protocol_store.read_auth_fence_vector(resolve_context)
        return AuthorizedFence(resolve_context=resolve_context, vector=vector)

    handoff = TerminalReplayHandoff(protocol_store, buses)

    async def event_gen() -> Any:
        subscription = None
        next_ticket_task: asyncio.Task[Any] | None = None
        # Batch 2 (tech-design §3.4-3): turn-scoped projection cache fast-paths pre-done report citation
        # bursts, avoiding complete canvas-projection reconstruction for every event.
        _live_proj_cache = LiveEventProjectionCache()
        try:
            result = await handoff.connect(
                team_id=team_id,
                session_id=session_id,
                turn_id=turn_id,
                client_cursor=client_cursor,
                authorize=authorize,
            )
            subscription = result.subscription
            checkpoint = result.checkpoint
            current_receipt_id = checkpoint.current_receipt_summary.receipt_id if checkpoint.current_receipt_summary is not None else None
            read_context = TerminalReadContext(
                team_id=team_id,
                session_id=session_id,
                turn_id=turn_id,
                viewer=viewer,
                auth_fence_vector_digest=checkpoint.auth_fence_vector_digest,
            )

            async def gate_frame(frame: dict[str, str]) -> bool:
                authorized = await authorize()
                if authorized.vector.auth_fence_vector_digest != checkpoint.auth_fence_vector_digest:
                    return False
                queued: list[dict[str, str]] = []

                def enqueue() -> bool:
                    queued.append(frame)
                    return True

                allowed = await protocol_store.linearize_frame_enqueue(
                    read_context,
                    resolve_context=authorized.resolve_context,
                    expected_pointer=checkpoint.generation_pointer,
                    expected_auth_fence_vector=authorized.vector,
                    enqueue=enqueue,
                )
                return bool(allowed and queued)

            checkpoint_frame = {
                "event": "stream.checkpoint",
                "data": json.dumps(
                    {
                        "terminal_protocol_version": 1,
                        "turn_id": turn_id,
                        "current_generation": checkpoint.current_generation,
                        "current_generation_state": checkpoint.current_generation_state.value,
                        "last_terminal_generation": checkpoint.last_terminal_generation,
                        "persistent_event_highwater": checkpoint.persistent_event_highwater,
                        "current_receipt_id": current_receipt_id,
                        "checkpoint_token": checkpoint.checkpoint_token,
                    },
                    ensure_ascii=False,
                ),
            }
            if not await gate_frame(checkpoint_frame):
                return
            yield checkpoint_frame

            replay_rows = result.replay_events
            replay_payloads = await project_event_payloads_for_viewer(
                request,
                turn_id,
                [(row.event_type, row.payload) for row in replay_rows],
                event_ordinals=[row.event_seq for row in replay_rows],
                _live_cache=_live_proj_cache,
            )

            current_terminal_seen = False
            for row, safe_payload in zip(
                replay_rows,
                replay_payloads,
                strict=True,
            ):
                classification = classify_terminal(
                    1,
                    ConsumerProfile.WEB,
                    row.as_terminal_event(),
                )
                if classification is TerminalClassification.MALFORMED:
                    raise TerminalProtocolMalformedError(
                        "terminal replay event classification is malformed",
                    )
                terminal_current = row.generation == checkpoint.current_generation and (
                    (classification is TerminalClassification.ROOT_COMMITTED and row.terminal_receipt_id == current_receipt_id)
                    or classification
                    in {
                        TerminalClassification.ROOT_NON_DURABLE,
                        TerminalClassification.ROOT_UNKNOWN,
                    }
                )
                current_terminal_seen = current_terminal_seen or terminal_current
                frame = {
                    "id": str(row.event_seq),
                    "event": row.event_type,
                    "data": json.dumps(
                        _terminal_v1_wire_payload(
                            payload=safe_payload,
                            event_id=row.event_id,
                            event_seq=row.event_seq,
                            generation=row.generation,
                            terminal_receipt_id=row.terminal_receipt_id,
                            terminal_current=terminal_current,
                        ),
                        ensure_ascii=False,
                    ),
                }
                if not await gate_frame(frame):
                    return
                yield frame

            for ticket in result.overlap_events:
                raw_payload = ticket.payload()
                safe_payload = await project_live_event_payload_for_viewer(
                    request,
                    turn_id,
                    ticket.event_type,
                    raw_payload,
                    event_ordinal=ticket.event_seq,
                    cache=_live_proj_cache,
                )
                terminal_receipt_id = ticket.terminal_receipt_id
                event = TerminalEvent(
                    event_type=ticket.event_type,
                    event_id=ticket.event_id,
                    generation=ticket.scope.generation,
                    actor_scope=ticket.actor_scope.value,
                    phase_scope=ticket.phase_scope.value,
                    terminal_receipt_id=terminal_receipt_id,
                    payload=raw_payload,
                )
                classification = classify_terminal(1, ConsumerProfile.WEB, event)
                if classification is TerminalClassification.MALFORMED:
                    raise TerminalProtocolMalformedError(
                        "terminal overlap event classification is malformed",
                    )
                if classification is TerminalClassification.ROOT_COMMITTED and (
                    ticket.scope.generation != checkpoint.current_generation or terminal_receipt_id != current_receipt_id
                ):
                    raise TerminalProtocolMalformedError(
                        "terminal overlap event does not match the current receipt",
                    )
                terminal_current = ticket.scope.generation == checkpoint.current_generation and (
                    (classification is TerminalClassification.ROOT_COMMITTED and terminal_receipt_id == current_receipt_id)
                    or classification
                    in {
                        TerminalClassification.ROOT_NON_DURABLE,
                        TerminalClassification.ROOT_UNKNOWN,
                    }
                )
                current_terminal_seen = current_terminal_seen or terminal_current
                frame = {
                    "id": str(ticket.event_seq),
                    "event": ticket.event_type,
                    "data": json.dumps(
                        _terminal_v1_wire_payload(
                            payload=safe_payload,
                            event_id=ticket.event_id,
                            event_seq=ticket.event_seq,
                            generation=ticket.scope.generation,
                            terminal_receipt_id=terminal_receipt_id,
                            terminal_current=terminal_current,
                        ),
                        ensure_ascii=False,
                    ),
                }
                if not await gate_frame(frame):
                    return
                yield frame

            if current_terminal_seen or subscription is None:
                return

            live_iterator = subscription.__aiter__()
            while True:
                if next_ticket_task is None:
                    next_ticket_task = asyncio.create_task(anext(live_iterator))
                ready, _pending = await asyncio.wait(
                    {next_ticket_task},
                    timeout=15,
                )
                if not ready:
                    keepalive = {
                        "event": "keepalive",
                        "data": json.dumps(
                            {"ts": asyncio.get_running_loop().time()},
                        ),
                    }
                    if not await gate_frame(keepalive):
                        return
                    yield keepalive
                    progress_frame = _turn_progress_frame(turn_id)
                    if progress_frame is not None:
                        if not await gate_frame(progress_frame):
                            return
                        yield progress_frame
                    continue
                completed = next_ticket_task
                next_ticket_task = None
                try:
                    ticket = completed.result()
                except StopAsyncIteration:
                    return
                raw_payload = ticket.payload()
                safe_payload = await project_live_event_payload_for_viewer(
                    request,
                    turn_id,
                    ticket.event_type,
                    raw_payload,
                    event_ordinal=ticket.event_seq,
                    cache=_live_proj_cache,
                )
                terminal_receipt_id = ticket.terminal_receipt_id
                event = TerminalEvent(
                    event_type=ticket.event_type,
                    event_id=ticket.event_id,
                    generation=ticket.scope.generation,
                    actor_scope=ticket.actor_scope.value,
                    phase_scope=ticket.phase_scope.value,
                    terminal_receipt_id=terminal_receipt_id,
                    payload=raw_payload,
                )
                classification = classify_terminal(1, ConsumerProfile.WEB, event)
                if classification is TerminalClassification.MALFORMED:
                    raise TerminalProtocolMalformedError(
                        "terminal live event classification is malformed",
                    )
                terminal_current = False
                if classification in {
                    TerminalClassification.ROOT_COMMITTED,
                    TerminalClassification.ROOT_NON_DURABLE,
                    TerminalClassification.ROOT_UNKNOWN,
                }:
                    # A live terminal ticket is published only after its
                    # aggregate transaction, so verify it against a fresh
                    # coherent checkpoint before exposing terminal_current.
                    live_checkpoint = await protocol_store.read_checkpoint(
                        read_context,
                    )
                    live_receipt_id = (
                        live_checkpoint.current_receipt_summary.receipt_id if live_checkpoint.current_receipt_summary is not None else None
                    )
                    if live_checkpoint.generation_pointer != checkpoint.generation_pointer:
                        return
                    if classification is TerminalClassification.ROOT_COMMITTED and (
                        ticket.scope.generation != live_checkpoint.current_generation or terminal_receipt_id != live_receipt_id
                    ):
                        raise TerminalProtocolMalformedError(
                            "terminal live event does not match the committed receipt",
                        )
                    terminal_current = ticket.scope.generation == live_checkpoint.current_generation and (
                        (classification is TerminalClassification.ROOT_COMMITTED and terminal_receipt_id == live_receipt_id)
                        or classification
                        in {
                            TerminalClassification.ROOT_NON_DURABLE,
                            TerminalClassification.ROOT_UNKNOWN,
                        }
                    )
                frame = {
                    "id": str(ticket.event_seq),
                    "event": ticket.event_type,
                    "data": json.dumps(
                        _terminal_v1_wire_payload(
                            payload=safe_payload,
                            event_id=ticket.event_id,
                            event_seq=ticket.event_seq,
                            generation=ticket.scope.generation,
                            terminal_receipt_id=terminal_receipt_id,
                            terminal_current=terminal_current,
                        ),
                        ensure_ascii=False,
                    ),
                }
                if not await gate_frame(frame):
                    return
                yield frame
                if terminal_current:
                    return
        except ViewerSourceProjectionError:
            yield {
                "event": "terminal.protocol_error",
                "data": json.dumps({"code": "source_projection_unavailable"}),
            }
        except Exception as exc:
            _MODULE_LOG.warning(
                "terminal.v1_stream_failed turn=%s error=%s",
                turn_id,
                type(exc).__name__,
            )
            code = "terminal_protocol_malformed" if isinstance(exc, TerminalProtocolMalformedError) else "terminal_dependency_recovering"
            yield {
                "event": "terminal.protocol_error",
                "data": json.dumps({"code": code}),
            }
        finally:
            if next_ticket_task is not None and not next_ticket_task.done():
                next_ticket_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await next_ticket_task
            if subscription is not None:
                await subscription.aclose()

    return EventSourceResponse(event_gen())


def _turn_progress_frame(turn_id: str) -> dict[str, str] | None:
    """Transient progress of the agents running this turn (agent run limits, phase 3).

    Sent next to a keepalive; like a keepalive it is not on the bus, not persisted
    and not replayed. None when this process is not running the turn.
    """
    from bowhead_core import turn_progress

    agents = turn_progress.snapshot(turn_id)
    if not agents:
        return None
    return {"event": "turn.progress", "data": json.dumps({"turn_id": turn_id, "agents": agents})}


@router.get("/stream/{turn_id}")
async def stream(
    turn_id: str,
    request: Request,
    cursor: Annotated[int, Query(ge=0)] = 0,
) -> EventSourceResponse:
    """SSE stream for a turn.

    用户切到别的 session / 关页面再回来时，浏览器新开一个 EventSource 订阅 —— 但
    in-memory bus queue 的过往事件已经给第一次订阅的 stream 消费掉了. 这里做**断线
    重连**: 先从 turn_log_store replay 所有已持久化事件 → dedupe 后 subscribe 实时
    bus. 若 turn 已终结 (status=completed/failed/error) 只 replay 不订阅，立刻发
    done 让前端 UI 切"已完成"态.
    """
    from bowhead_core.space_governance import SpaceOperation

    from bowhead_gateway.routes.turns import _require_turn_access

    access_user = await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    registry = get_event_registry()
    log_store = getattr(request.app.state, "turn_log_store", None)
    session_store = getattr(request.app.state, "session_store", None)
    terminal_runtime_registry = getattr(
        request.app.state,
        "terminal_runtime_registry",
        None,
    )
    # authz: caller must be authenticated AND own the turn (turn→session→user_id). Raises 401/403
    # BEFORE the stream starts — was open to any caller with a turn_id (IDOR over turn events).
    from bowhead_gateway.routes.auth import assert_turn_access

    viewer = await assert_turn_access(request, turn_id, session_store)

    protocol_store = getattr(request.app.state, "terminal_protocol_store", None)
    if protocol_store is not None and session_store is not None:
        visible_turn = await session_store.get_turn(turn_id)
        visible_session = await session_store.get_session(visible_turn.session_id) if visible_turn is not None else None
        if visible_turn is None or visible_session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
        try:
            team_id, persisted_session_id, protocol_version = await protocol_store.read_persisted_turn_scope(turn_id=turn_id)
            persisted_protocol = resolve_persisted_chat_execution_protocol(
                protocol_version,
            )
        except Exception as exc:
            from bowhead_core.terminal_protocol import (
                TerminalProtocolFutureVersionError,
                TerminalProtocolMalformedError,
            )

            if isinstance(exc, TerminalProtocolFutureVersionError):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    {"error": {"code": "terminal_protocol_future_version"}},
                ) from exc
            if isinstance(exc, TerminalProtocolMalformedError):
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    {"error": {"code": "terminal_protocol_malformed"}},
                ) from exc
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                {"error": {"code": "terminal_dependency_recovering"}},
            ) from exc
        if persisted_session_id != visible_session.id:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                {"error": {"code": "terminal_protocol_malformed"}},
            )
        if persisted_protocol is PersistedChatExecutionProtocol.TERMINAL_V1:
            last_event_id = request.headers.get("last-event-id", "").strip()
            if last_event_id:
                try:
                    cursor = max(cursor, int(last_event_id))
                except ValueError as exc:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        {"error": {"code": "terminal_cursor_invalid"}},
                    ) from exc
            if access_user.id != viewer or visible_session.user_id != viewer:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
            return await _terminal_v1_stream_response(
                request=request,
                turn_id=turn_id,
                team_id=team_id,
                session_id=persisted_session_id,
                viewer=viewer,
                client_cursor=cursor,
            )

    async def event_gen() -> Any:
        # Batch 2: same per-stream, turn-scoped projection cache as above.
        _live_proj_cache = LiveEventProjectionCache()
        import json as _json

        seen: set[tuple[str, str]] = set()  # (event_type, occurred_at) dedupe key
        transport_terminal_seen = False

        replay_available = True
        replay_event_seq_floor = 0

        async def _read_dispatch_projection_state() -> str:
            if session_store is None or not hasattr(
                session_store,
                "get_chat_spawn_dispatch_projection_state",
            ):
                return "unavailable"
            try:
                return await session_store.get_chat_spawn_dispatch_projection_state(
                    turn_id=turn_id,
                )
            except Exception:
                return "unavailable"

        # This state must precede replay. A remote owner that is active here
        # can settle immediately afterward, but the replay snapshot may then be
        # stale; this connection must EOF and let the next one replay again.
        dispatch_projection_state_before_replay = await _read_dispatch_projection_state()

        async def _projection_snapshot_allows_synthetic_terminal() -> bool:
            """Require an unchanged known fence around replay/relation reads."""
            if dispatch_projection_state_before_replay not in {
                "settled",
                "untracked",
            }:
                return False
            current = await _read_dispatch_projection_state()
            return current == dispatch_projection_state_before_replay

        async def _durable_follow_up_projection() -> tuple[
            str,
            dict[str, str] | None,
            Any | None,
        ]:
            """Project the durable parent→child relation onto this SSE stream.

            The relation can become visible either before a reconnect starts or
            while an already-connected subscriber is being woken by bus close.
            Both paths must use the same payload so the frontend can attach the
            child before accepting a synthetic parent ``done``.

            The tri-state result is deliberate: a storage failure is not proof
            that no child exists.  Callers must end the transport without done
            on ``unavailable`` so EventSource retries discovery.
            """
            producer_bus, stable_registry_floor = await registry.projection_checkpoint(
                turn_id,
            )
            if not replay_available:
                return "unavailable", None, producer_bus
            if session_store is None or not hasattr(session_store, "get_chat_follow_up"):
                return "absent", None, producer_bus

            try:
                follow_up = await session_store.get_chat_follow_up(
                    parent_turn_id=turn_id,
                )
                if follow_up is None:
                    return "absent", None, producer_bus
                follow_up_turn_id, buffered_count = follow_up
                follow_up_turn = await session_store.get_turn(follow_up_turn_id)
                follow_up_source = None
                if follow_up_turn is not None and hasattr(session_store, "get_chat_turn_user_source"):
                    follow_up_session = await session_store.get_session(
                        follow_up_turn.session_id,
                    )
                    if follow_up_session is not None:
                        follow_up_source = await session_store.get_chat_turn_user_source(
                            turn_id=follow_up_turn_id,
                            user_id=follow_up_session.user_id,
                            workspace_id=follow_up_session.workspace_id,
                        )
            except Exception:
                return "unavailable", None, producer_bus

            # Relation materialisation precedes the ordered persistence barrier.
            # If that producer generation is still registered, replay can be
            # stale and its live counter can be ahead of seq_floor.  Drain the
            # exact bus instead of inventing a colliding projection sequence.
            # The producer may have been registered while the durable relation
            # query was in flight.  Re-check after that query so the captured
            # generation, rather than stale replay, supplies the ordered tail.
            producer_bus, stable_registry_floor = await registry.projection_checkpoint(
                turn_id,
            )
            if producer_bus is not None:
                return "pending", None, producer_bus

            # No local producer is not proof that no producer exists: another
            # Gateway may own the worker and still be draining its ordered log
            # barrier. Only a dispatch already settled before the relation
            # reads makes this request's earlier replay snapshot stable.
            if dispatch_projection_state_before_replay == "active":
                return "pending", None, None
            if dispatch_projection_state_before_replay != "settled":
                return "unavailable", None, None

            # Once the generation has retired, close() has captured its counter.
            # After restart there is no in-flight writer, so the projection can
            # advance strictly beyond both durable replay and every live-only
            # event observed in the retired generation.
            projection_seq = (
                max(
                    stable_registry_floor,
                    replay_event_seq_floor,
                )
                + 1
            )
            payload = {
                "follow_up_turn_id": follow_up_turn_id,
                "follow_up_stream_url": f"/api/v1/chat/stream/{follow_up_turn_id}",
                "reason": (
                    "steer_recovery" if follow_up_turn is not None and follow_up_turn.error_kind == "steer_recovery_required" else "steer_drain"
                ),
                "buffered_count": buffered_count,
                "attachments": (list(follow_up_source.attachment_refs) if follow_up_source is not None else []),
                "execution_generation": max(
                    1,
                    int(
                        getattr(
                            follow_up_turn,
                            "execution_generation",
                            1,
                        )
                        or 1
                    ),
                ),
                "seq": projection_seq,
                "evt_seq": projection_seq,
            }
            return (
                "present",
                {
                    "event": "turn.follow_up",
                    "data": _json.dumps(payload, ensure_ascii=False),
                },
                None,
            )

        # 1. Snapshot replay, but do not publish it yet. Recovery can expose a
        # durable parent→child relation without having persisted the parent's
        # turn.follow_up frame. In that case the discovery frame must precede
        # the parent's failure event; EventSource reserves `error` for transport
        # failure and clients may close immediately on a terminal frame.
        replay_rows: list[Any] = []
        replay_payloads: list[dict[str, Any]] = []
        past: list[Any] = []
        if log_store is not None:
            try:
                past = await log_store.list_events(turn_id)
            except Exception:
                replay_available = False

        # The Store row is the canonical terminal fact. Read it after the log
        # snapshot and before projecting any replay frame so an interrupted
        # writer's stale done/error can never contradict the current Turn.
        # An unavailable lookup is not permission to trust a root terminal.
        turn_state = "unavailable"
        canonical_turn_status: str | None = None
        terminal_turn_status: str | None = None
        if session_store is not None:
            try:
                t = await session_store.get_turn(turn_id)
                if t is not None:
                    canonical_turn_status = str(t.status)
                    normalized_turn_status = normalize_legacy_turn_status(
                        canonical_turn_status,
                    )
                    if normalized_turn_status in _TERMINAL_TURN_STATUSES:
                        turn_state = "terminal"
                        terminal_turn_status = normalized_turn_status
                    else:
                        turn_state = "running"
            except Exception:
                turn_state = "unavailable"

        if log_store is not None:
            # Drop stale pause markers and any root terminal that cannot be
            # proved against the canonical Turn status.
            replay_rows = _replay_rows_for_stream(
                past,
                canonical_turn_status=canonical_turn_status,
                validate_terminal_status=True,
            )
            for row in replay_rows:
                with contextlib.suppress(TypeError, ValueError):
                    replay_event_seq_floor = max(
                        replay_event_seq_floor,
                        int(row.payload.get("evt_seq", 0)),
                    )
            try:
                replay_payloads = await project_event_payloads_for_viewer(
                    request,
                    turn_id,
                    [(row.event_type, row.payload) for row in replay_rows],
                    _live_cache=_live_proj_cache,
                )
            except ViewerSourceProjectionError:
                yield {
                    "event": "error",
                    "data": _json.dumps(
                        {"code": "source_projection_unavailable"},
                    ),
                }
                return
        follow_up_projected = any(row.event_type == "turn.follow_up" for row in replay_rows)
        producer_bus, _stable_registry_floor = await registry.projection_checkpoint(
            turn_id,
        )
        # A worker can die after steer acceptance but before its in-memory
        # `turn.follow_up` emit.  The dispatch transaction retains the durable
        # parent→child relation, so reconnect must project the same discovery
        # event that the live path would have sent.
        projection_status = "absent"
        if not follow_up_projected:
            (
                projection_status,
                projected_event,
                producer_bus,
            ) = await _durable_follow_up_projection()
            if projected_event is not None:
                follow_up_projected = True
                yield projected_event

        # Publish the replay snapshot only after a recovery-only child relation
        # has been projected. Persisted normal-path turn.follow_up rows retain
        # their original row order here.
        for row, safe_payload in zip(replay_rows, replay_payloads, strict=True):
            key = (row.event_type, row.occurred_at)
            seen.add(key)
            if _is_transport_terminal_event(row.event_type, row.payload):
                transport_terminal_seen = True
            yield {
                "event": row.event_type,
                "data": _json.dumps(safe_payload, ensure_ascii=False),
            }
        # 2. Durable discovery is temporarily unavailable. An existing producer
        # bus is sufficient evidence to continue live; without one, EOF makes
        # EventSource retry the storage reads instead of waiting on an empty bus.
        if (projection_status == "unavailable" or turn_state == "unavailable") and producer_bus is None:
            return

        # Relation-visible-before-barrier: bypass the terminal shortcut and
        # drain the exact generation captured under the registry lock.
        if turn_state == "terminal" and producer_bus is None:
            if projection_status in {"unavailable", "pending"}:
                # EOF without done makes EventSource retry.  A transient replay /
                # relation read failure must not become a permanent terminal lie.
                return
            # If replay lacks done (failed turns may have only error), supply one so frontend stops waiting.
            if not transport_terminal_seen:
                # A remote owner can persist the Turn terminal row before its
                # ordered event tail and dispatch lease settle. Even with no
                # follow-up relation, synthetic done from an earlier replay
                # snapshot would permanently suppress those tail events.
                # A connection may start while the parent is untracked, then
                # accept steer and become drain_pending/orphan-active during
                # replay. Even untracked→settled is unsafe: the relation may
                # have appeared after this request's earlier absent query.
                if not await _projection_snapshot_allows_synthetic_terminal():
                    return
                yield {
                    "event": "done",
                    "data": _json.dumps(
                        {
                            "turn_id": turn_id,
                            "status": terminal_turn_status,
                        }
                    ),
                }
            return
        # 3. Turn still running: subscribe to live bus for subsequent events (dedupe).
        # fix/sse-fanout-bus: move get_or_create after terminal short-circuit, so closed-turn reconnects
        # no longer permanently reinsert an empty fresh bus into the registry (nobody closes it).
        # Producers do not depend on bus creation here (/send creates before spawning worker, ~:840).
        # A stream consumer never creates a bus.  On a non-owner Gateway that
        # would leave a subscriber-only generation nobody can close and later
        # storage outages would mistake it for a producer.  EOF lets the client
        # retry replay until a local producer or durable terminal state exists.
        if producer_bus is None and terminal_runtime_registry is not None:
            terminal_runtime = await terminal_runtime_registry.get(turn_id)
            producer_bus = terminal_runtime.bus if terminal_runtime is not None else None
        if producer_bus is None:
            return
        bus = producer_bus
        # W11-8: add stall detection + heartbeat keepalive.
        # ⚠ Implementation note (R4 smoke found and fixed): **never wait_for(sub_iter.__anext__())**.
        # CancelledError finalizes the async generator; subsequent __anext__ raises StopAsyncIteration,
        # killing the stream at its first stall timeout. Use the task pattern:
        # wrap __anext__() in Task, await asyncio.wait with timeout, **do not cancel task**, so
        # the generator keeps waiting in the next wait cycle. It stays alive during stalls, and later
        # events follow the normal path. Verify: tests/test_chat_sse_waitfor_smoke.py.
        # - Every 5s without a chunk: emit streaming.state paused for frontend W3-3 yellow background.
        # - Force keepalive every 15s to prevent frontend timeout false positives.
        import asyncio as _asyncio

        last_event_ts = _asyncio.get_event_loop().time()
        last_keepalive_ts = last_event_ts
        # B5 (W13 user feedback): 5s is too sensitive; BI-tool gaps (~5-15s) often falsely trigger paused.
        # Real stalls usually need user notice only at ≥30s; use 30 to avoid disturbing BI-report turns.
        STALL_S = 30.0
        KEEPALIVE_S = 15.0
        paused_emitted = False
        sub_iter = bus.subscribe().__aiter__()
        pending_task: _asyncio.Task[Any] | None = None
        try:
            while True:
                if pending_task is None:
                    pending_task = _asyncio.create_task(sub_iter.__anext__())
                done, _pending = await _asyncio.wait({pending_task}, timeout=1.0)
                now = _asyncio.get_event_loop().time()
                if pending_task in done:
                    try:
                        event = pending_task.result()
                    except StopAsyncIteration:
                        # Synthetic-done guard (defense-in-depth). After fix/sse-fanout-bus, event theft is impossible
                        # in fanout (private subscriber cursors read the full ring). Guard two remaining paths:
                        # ① flag-OFF legacy single-consumer queue overlap race (another subscriber steals done,
                        # leaving only SENTINEL for this connection);
                        # ② crash without any done/error emit, awakened by registry.close
                        # (_run_turn_and_cleanup best-effort mark_failed -> terminal status ->
                        # synthetic done here finalizes FE).
                        # The bus may be closed specifically because the
                        # persist-before-delivery follow-up sink exhausted its
                        # retries.  Re-project the durable relation *on this
                        # existing connection* before synthetic done; otherwise
                        # EventSource treats done as terminal and never reconnects
                        # to discover the already-running child.
                        if not follow_up_projected:
                            (
                                projection_status,
                                projected_event,
                                _projection_bus,
                            ) = await _durable_follow_up_projection()
                            if projected_event is not None:
                                follow_up_projected = True
                                yield projected_event
                            elif projection_status in {"unavailable", "pending"}:
                                # End this transport without done. EventSource's
                                # normal reconnect path will retry the durable read.
                                # The same pre-replay dispatch fence as the
                                # terminal shortcut is required here: a local bus
                                # can close after preterminal materialize failed,
                                # before recovery creates the child relation.
                                break
                            elif projection_status == "absent":
                                if not await _projection_snapshot_allows_synthetic_terminal():
                                    break
                        if session_store is not None and not transport_terminal_seen:
                            try:
                                t_term = await session_store.get_turn(turn_id)
                                normalized_terminal_status = (
                                    normalize_legacy_turn_status(
                                        getattr(t_term, "status", None),
                                    )
                                    if t_term
                                    else None
                                )
                                if normalized_terminal_status in _TERMINAL_TURN_STATUSES:
                                    yield {
                                        "event": "done",
                                        "data": _json.dumps(
                                            {
                                                "turn_id": turn_id,
                                                "status": normalized_terminal_status,
                                            }
                                        ),
                                    }
                            except Exception:  # noqa: S110
                                pass
                        break
                    pending_task = None
                    # Real event resets stall + paused state.
                    last_event_ts = now
                    if paused_emitted:
                        yield {
                            "event": "streaming.state",
                            "data": _json.dumps({"state": "active"}),
                        }
                        paused_emitted = False
                    key = (event.event_type, event.occurred_at)
                    if key not in seen:
                        seen.add(key)
                        if _is_transport_terminal_event(
                            event.event_type,
                            event.payload,
                        ):
                            transport_terminal_seen = True
                        if event.event_type == "turn.follow_up":
                            if follow_up_projected:
                                # Durable recovery may have already projected
                                # the same child before this live row arrived.
                                continue
                            follow_up_projected = True
                        try:
                            safe_payload = await project_live_event_payload_for_viewer(
                                request,
                                turn_id,
                                event.event_type,
                                event.payload,
                                cache=_live_proj_cache,
                            )
                        except ViewerSourceProjectionError:
                            yield {
                                "event": "error",
                                "data": _json.dumps({"code": "source_projection_unavailable"}),
                            }
                            break
                        safe_payload["seq"] = event.seq if event.seq >= 0 else 0
                        if event.parent_agent_id is not None:
                            safe_payload["parent_agent_id"] = event.parent_agent_id
                        yield {
                            "event": event.event_type,
                            "data": _json.dumps(safe_payload, ensure_ascii=False),
                        }
                # Timeout: pending_task still awaits queue.get; do not cancel, let it keep awaiting.
                elif not paused_emitted and now - last_event_ts > STALL_S:
                    yield {
                        "event": "streaming.state",
                        "data": _json.dumps({"state": "paused", "reason": f"no_chunk_for_{STALL_S}s"}),
                    }
                    paused_emitted = True
                # chat-polish-r3 Phase 3 (B10) — keepalive emit moved OUTSIDE the
                # done/timeout branch. Pre-r3 it only fired in the timeout branch,
                # meaning a turn that streams events faster than 1/sec (LLM token
                # narrative.delta storm or canvas.section_chunk batch) would NEVER
                # hit the timeout branch → no keepalive emit for 30+ seconds →
                # FE watchdog (use-turn-runner.ts:1287) false-positive "连接断了".
                # Now keepalive emits every KEEPALIVE_S (15s) regardless of event
                # flow. FE handle() also resets lastKeepalive on any event as a
                # second line of defense.
                if now - last_keepalive_ts > KEEPALIVE_S:
                    yield {
                        "event": "keepalive",
                        "data": _json.dumps({"ts": now}),
                    }
                    progress_frame = _turn_progress_frame(turn_id)
                    if progress_frame is not None:
                        yield progress_frame
                    last_keepalive_ts = now
        finally:
            # fix(sse-reconnect)/fix(sse-fanout-bus): client disconnect makes EventSourceResponse cancel
            # this generator, but does not automatically cancel pending_task.
            # - Fanout: abandoned task cannot steal events (private cursor). Cancellation is task hygiene
            #   only; otherwise it waits on cond.wait until turn end, wasting a task and retaining
            #   generator）。
            # - flag-OFF legacy: cancellation is required for correctness; a leftover getter steals subsequent
            #   events (including done) from the single-consumer queue, depriving reconnect of terminal state (dogfood t_be1bcc08).
            #   See test_chat_sse_waitfor_smoke.py::test_bus_cancelled_getter_hands_off_to_reconnect.
            if pending_task is not None and not pending_task.done():
                pending_task.cancel()

    return EventSourceResponse(event_gen())


# ─────────────────────────────────────────────────────────────
# Background worker
# ─────────────────────────────────────────────────────────────
async def _run_turn_and_cleanup(
    orchestrator: Orchestrator,
    req: TurnRequest,
    user_store=None,
    team_id: str | None = None,
    metrics_log=None,
    entitlements=None,
    admitted_legacy_bus=None,
    cleanup_grace_s: float = 5.0,
) -> Any:
    """跑 turn，完成/失败后延迟清理 event bus（等订阅端读完）.
    P4.5.10: 跑完扣 credit (user/team 双层 atomic) — 仅在 team_id 存在时 (真 user, 跳过 stub).

    On a legacy turn's ordered tail, atomically materialize an accepted steer
    buffer as a durable child. If work was drained, spawn that child with
    ``resume_turn_id`` so Lead treats it as a continuation.
    """

    import structlog

    log = structlog.get_logger()
    # fix/sse-fanout-bus r3 (impl review P1): capture this worker generation's bus identity.
    # Clarification resume closes the same turn_id, then get_or_create creates a new-generation bus.
    # This worker's delayed close needs expected guard or it closes the new generation, black-holing events.
    terminal_runtime = getattr(req, "terminal_runtime", None)
    if terminal_runtime is not None:
        try:
            # S5 spawn contract: task creation happens after claim, and the new
            # coroutine's first durable action is the started marker. No model,
            # tool, event, usage, or billing work may precede this line.
            await terminal_runtime.start_execution()
        except BaseException:
            await terminal_runtime.handoff_to_recovery(
                reason_code="worker_start_failed",
            )
            raise
    registry = getattr(orchestrator, "event_registry", None)
    if terminal_runtime is not None:
        _worker_bus = terminal_runtime.bus
    else:
        if admitted_legacy_bus is None:
            raise RuntimeError("legacy_chat_execution_not_admitted")
        checkpoint = getattr(registry, "projection_checkpoint", None)
        if not callable(checkpoint):
            raise RuntimeError("legacy_chat_event_registry_unavailable")
        _current_bus, _stable_floor = await checkpoint(req.turn_id)
        if _current_bus is not admitted_legacy_bus:
            raise RuntimeError("legacy_chat_execution_generation_changed")
        _worker_bus = admitted_legacy_bus
    if terminal_runtime is None and not bool(
        getattr(_worker_bus, "_persist_before_live", False),
    ):
        raise RuntimeError("legacy_chat_execution_not_durable")
    from uuid import uuid4 as _uuid

    _new_turn_id = f"t_{_uuid().hex[:12]}"
    _preterminal_buffered_texts: list[str] = []
    _preterminal_materialized = False
    _preterminal_attempted = False
    _preterminal_deferred = False
    _follow_up_announced = False
    _preterminal_reference_scope = None
    _preterminal_reference_sources = None
    _preterminal_reference_mcp_index = None
    _preterminal_reference_mcp_access = None
    _preterminal_reference_mcp_prompt = False

    async def _release_plan_execution_tail() -> None:
        """Release a Plan retry fence only after this worker's full tail ends."""
        plan_id = getattr(req, "plan_execution_id", None)
        plan_store = getattr(orchestrator, "plan_store", None)
        if plan_store is None:
            return
        retry_delay_s = 0.05
        for attempt in range(3):
            try:
                if plan_id and hasattr(plan_store, "release_execution_turn"):
                    await plan_store.release_execution_turn(
                        plan_id,
                        req.turn_id,
                    )
                elif hasattr(plan_store, "release_execution_turn_for_turn"):
                    await plan_store.release_execution_turn_for_turn(
                        req.turn_id,
                    )
                return
            except Exception:
                if attempt == 2:
                    log.exception(
                        "plan.execution_tail_release_failed",
                        plan_id=plan_id,
                        turn_id=req.turn_id,
                    )
                    return
                await asyncio.sleep(retry_delay_s)
                retry_delay_s *= 2

    async def _materialize_steer_before_terminal() -> bool:
        """Make the continuation discoverable before parent ``done`` closes SSE."""
        nonlocal _follow_up_announced
        nonlocal _preterminal_attempted
        nonlocal _preterminal_buffered_texts
        nonlocal _preterminal_deferred
        nonlocal _preterminal_materialized
        nonlocal _preterminal_reference_scope
        nonlocal _preterminal_reference_sources
        nonlocal _preterminal_reference_mcp_index
        nonlocal _preterminal_reference_mcp_access
        nonlocal _preterminal_reference_mcp_prompt
        if not req.session_id:
            return True
        try:
            from bowhead_gateway.turn_service import (
                materialize_reference_steer_head,
            )

            reference_materialization = (
                await materialize_reference_steer_head(
                    orchestrator,
                    session_id=req.session_id,
                    current_turn_id=req.turn_id,
                    next_turn_id=_new_turn_id,
                    team_id=team_id or "",
                    user_id=req.user_id,
                    workspace_id=req.workspace_id,
                )
                if team_id
                else None
            )
            if reference_materialization is None:
                _preterminal_buffered_texts = await orchestrator.session_store.materialize_steer_follow_up(
                    session_id=req.session_id,
                    current_turn_id=req.turn_id,
                    next_turn_id=_new_turn_id,
                )
            else:
                _preterminal_buffered_texts = list(
                    reference_materialization.texts,
                )
                _preterminal_reference_scope = reference_materialization.scope
                _preterminal_reference_sources = reference_materialization.skill_sources
                _preterminal_reference_mcp_index = reference_materialization.mcp_index
                _preterminal_reference_mcp_access = reference_materialization.mcp_access
                _preterminal_reference_mcp_prompt = reference_materialization.mcp_prompt
            _preterminal_materialized = bool(_preterminal_buffered_texts)
            _preterminal_attempted = True
        except asyncio.CancelledError:
            # Stop / lease cancellation can land while the SQLite transaction
            # is awaiting.  The transaction rolls back; publish a durable retry
            # marker before propagating cancellation.  If even that marker
            # fails, leave ``attempted`` false so this task's finalizer retries.
            try:
                _preterminal_deferred = await orchestrator.session_store.mark_chat_spawn_dispatch_drain_pending(
                    turn_id=req.turn_id,
                    session_id=req.session_id,
                )
            except BaseException:
                _preterminal_deferred = False
                log.warning(
                    "steer.preterminal_cancel_marker_failed",
                    parent_turn=req.turn_id,
                    exc_info=True,
                )
            raise
        except Exception:
            log.warning(
                "steer.preterminal_materialize_failed",
                parent_turn=req.turn_id,
                exc_info=True,
            )
            # Do not let the lease monitor terminalize this dispatch. Durable
            # maintenance will retry and project a recoverable child Turn.
            try:
                _preterminal_deferred = await orchestrator.session_store.mark_chat_spawn_dispatch_drain_pending(
                    turn_id=req.turn_id,
                    session_id=req.session_id,
                )
            except Exception:
                _preterminal_deferred = False
                log.warning(
                    "steer.preterminal_failure_marker_failed",
                    parent_turn=req.turn_id,
                    exc_info=True,
                )
            # Never deliver parent terminal after accepted steer work became
            # uncertain. The finalizer retries materialization/marker and then
            # closes this non-terminal bus; SSE reconnect discovers the durable
            # child only after maintenance has made the relation visible.
            return False
        if not _preterminal_buffered_texts:
            return True

        _preterminal_source = await orchestrator.session_store.get_chat_turn_user_source(
            turn_id=_new_turn_id,
            user_id=req.user_id,
            workspace_id=req.workspace_id,
        )
        if _preterminal_source is None:
            raise RuntimeError("steer_follow_up_source_missing")

        from bowhead_core.events import OrchestratorEvent

        follow_up_event = OrchestratorEvent(
            event_type="turn.follow_up",  # type: ignore[arg-type]
            turn_id=req.turn_id,
            payload={
                "follow_up_turn_id": _new_turn_id,
                "follow_up_stream_url": (f"/api/v1/chat/stream/{_new_turn_id}"),
                "reason": "steer_drain",
                "buffered_count": len(_preterminal_buffered_texts),
                "attachments": list(_preterminal_source.attachment_refs),
                "execution_generation": 1,
            },
            persist_before_delivery=True,
        )
        # A durable sink failure raises ``TerminalDurabilityAbort``. ``False``
        # only means this exact bus generation is already closed, so retrying
        # the same object cannot restore the ordering barrier.
        _follow_up_announced = await _worker_bus.emit(follow_up_event)
        if _follow_up_announced:
            return True
        # Do not emit parent done out of order and do not retain a healthy
        # dispatch lease forever.  The child/source relation is already
        # durable; finalization spawns it and closes this non-terminal bus.
        # EventSource reconnect then projects follow_up before synthetic done.
        log.error(
            "steer.follow_up_ordering_barrier_deferred",
            parent_turn=req.turn_id,
            attempts=1,
        )
        return False

    # TurnEventBus invokes this exactly once immediately before its first
    # terminal event.  Normal live order is therefore follow_up → done.
    if terminal_runtime is None:
        _worker_bus._before_terminal_emit = _materialize_steer_before_terminal
    _dispatch_owner_lost = False
    _unconfirmed_worker_error: BaseException | None = None
    result = None
    try:
        # fix/sse-fanout-bus r3 (impl review P1): crash recovery wraps **only** run_turn;
        # never include later credit deduction. A deduction error (DB lock/full disk) would otherwise
        # incorrectly mark a **completed** turn failed and zero its token ledger.
        try:
            # credit-billing gap 7: attribute **all** LLM calls in this turn (contextvar inherited automatically
            # by derived peer / subagent tasks). Billing attribution only; charging still happens once below
            # as the atomic post-flight dual deduction.
            def _accumulate_partial_usage(record: Any) -> None:
                """Keep exact completed-call usage available to Stop finalization."""
                # turn-summary llm_count: every successful LLM call in this turn (including contextvar-inheriting
                # derived peer/subagent tasks) passes here once. Store separately on bus, never in _pi_turn_state,
                # which Lead replaces entirely at startup, losing earlier calls.
                _worker_bus._turn_llm_call_count = (  # type: ignore[attr-defined]
                    int(getattr(_worker_bus, "_turn_llm_call_count", 0) or 0) + 1
                )
                state = getattr(_worker_bus, "_pi_turn_state", None)
                if not isinstance(state, dict):
                    state = {
                        "tokens_input": 0,
                        "tokens_output": 0,
                        "invocations": [],
                    }
                    _worker_bus._pi_turn_state = state  # type: ignore[attr-defined]
                state["tokens_input"] = int(state.get("tokens_input") or 0) + int(
                    getattr(record, "tokens_input", 0) or 0,
                )
                state["tokens_output"] = int(state.get("tokens_output") or 0) + int(
                    getattr(record, "tokens_output", 0) or 0,
                )

            from os import environ

            from bowhead_core.chat_reference_storage import ReferenceCipher
            from bowhead_core.physical_usage import PhysicalUsageObserver
            from bowhead_router.invocation import physical_observation_scope

            observer = PhysicalUsageObserver(
                orchestrator.session_store.db_path,
                cipher=ReferenceCipher(environ.get("BOWHEAD_FERNET_KEY", "")),
                turn_id=req.turn_id,
                generation=req.execution_generation,
                plan_id=req.plan_execution_id,
                model_credit_rates=getattr(getattr(req, "team_router", None), "model_credit_rates", None),
            )
            with (
                physical_observation_scope(observer),
                usage_context(
                    team_id=team_id,
                    user_id=req.user_id,
                    workspace_id=req.workspace_id,
                    turn_id=req.turn_id,
                    reason="turn",
                    turn_usage_sink=_accumulate_partial_usage,
                ),
            ):
                from bowhead_core.agent_run_limits import (
                    TurnCreditGate,
                    reset_credit_gate,
                    set_credit_gate,
                )

                async def _read_credit_balances() -> tuple[int | None, int | None]:
                    _u = await user_store.get_user(req.user_id)
                    _t = await user_store.get_team(team_id)
                    return (
                        getattr(_u, "credit_balance", None) if _u is not None else None,
                        getattr(_t, "team_credit_balance", None) if _t is not None else None,
                    )

                def _accrued_turn_credits() -> int:
                    _state = getattr(_worker_bus, "_pi_turn_state", None) or {}
                    return credits_for_tokens(int(_state.get("tokens_input") or 0), int(_state.get("tokens_output") or 0))

                # Agent run limits, phase 3: subagents stop with partial work once this
                # turn has used the remaining credit; settlement below is unchanged.
                _credit_gate_token = set_credit_gate(
                    TurnCreditGate(_read_credit_balances, _accrued_turn_credits) if user_store is not None and team_id else None
                )
                try:
                    result = await orchestrator.run_turn(req)
                finally:
                    reset_credit_gate(_credit_gate_token)
                    from bowhead_core import turn_progress

                    turn_progress.clear_turn(req.turn_id)
        except asyncio.CancelledError as cancel_exc:
            cancel_reason = str(cancel_exc.args[0]) if cancel_exc.args else ""
            if cancel_reason != "dispatch_owner_lost":
                raise
            _dispatch_owner_lost = True
            # Losing the exact lease also loses permission to mutate the Turn
            # or its ordered event tail. Foreign recovery/new ownership may
            # already exist; only the Store recovery transaction can rebuild
            # and settle this generation's terminal projection.
            raise
        except Exception as e:
            log.exception("orchestrator.unhandled_exception", turn_id=req.turn_id, error=str(e))
            # Previously logging alone left status running; stream's synthetic-done guard saw no terminal,
            # stranding FE running until its 10min give-up. Best effort: persist terminal status before emitting error,
            # matching Orchestrator's emit-done-AFTER-persist P0-1 ordering. Before mark_failed, recheck that
            # status is nonterminal (defense-in-depth: run_turn may already have persisted a terminal then raised;
            # never overwrite it). If Orchestrator already emitted a terminal, bus._closed makes emit return early,
            # preventing duplicate error events.
            # No duplicate error.
            if terminal_runtime is not None:
                try:
                    from bowhead_core.events import OrchestratorEvent

                    if terminal_runtime.bus.receipt is None:
                        await terminal_runtime.bus.emit(
                            OrchestratorEvent(
                                event_type="turn_failed",
                                turn_id=req.turn_id,
                                payload={
                                    "message_key": "turn.failed",
                                    "reason_code": "terminal_protocol_malformed",
                                },
                            )
                        )
                        await terminal_runtime.bus.emit(
                            OrchestratorEvent(
                                event_type="done",
                                turn_id=req.turn_id,
                                payload={
                                    "turn_id": req.turn_id,
                                    "status": "failed",
                                    "error": "terminal_protocol_malformed",
                                    "error_kind": "internal",
                                },
                            )
                        )
                except Exception as _te:
                    log.warning(
                        "worker.terminal_failure_deferred_to_recovery",
                        turn_id=req.turn_id,
                        error=type(_te).__name__,
                    )
            else:
                terminal_fact_confirmed = False
                failure_marked = False
                try:
                    _t_cur = await orchestrator.session_store.get_turn(req.turn_id)
                    terminal_fact_confirmed = bool(_t_cur is not None and _t_cur.status in LEGACY_TURN_WORKER_SETTLED_STATUSES)
                    if not terminal_fact_confirmed:
                        failure_marked = bool(
                            await orchestrator.session_store.mark_failed(
                                turn_id=req.turn_id,
                                error=str(e) or "unknown_error",
                                error_kind="lead_internal_error",
                            )
                        )
                        terminal_fact_confirmed = failure_marked
                        if not terminal_fact_confirmed:
                            # A concurrent canonical terminal CAS may have won
                            # between the first read and mark_failed. Re-read
                            # before classifying the outcome as unknown.
                            _t_after = await orchestrator.session_store.get_turn(
                                req.turn_id,
                            )
                            terminal_fact_confirmed = bool(_t_after is not None and _t_after.status in LEGACY_TURN_WORKER_SETTLED_STATUSES)
                except Exception as _me:
                    log.warning("worker.mark_failed_failed", turn_id=req.turn_id, error=str(_me))
                if failure_marked:
                    try:
                        from bowhead_core.events import OrchestratorEvent
                        from bowhead_core.published_diagnostics import build_chat_turn_failure

                        failure = build_chat_turn_failure(
                            reason_code="lead_internal_error",
                            turn_id=req.turn_id,
                        )

                        await _worker_bus.emit(
                            OrchestratorEvent(
                                event_type="turn_failed",
                                turn_id=req.turn_id,
                                payload={
                                    "turn_id": req.turn_id,
                                    "message": "internal_error",
                                    "reason_code": failure.reason_code,
                                    "diagnostics": failure.diagnostics,
                                },
                                persist_before_delivery=True,
                            )
                        )
                        await _worker_bus.emit(
                            OrchestratorEvent(
                                event_type="done",
                                turn_id=req.turn_id,
                                payload={
                                    "turn_id": req.turn_id,
                                    "status": "failed",
                                    "error": "internal_error",
                                    "error_kind": failure.reason_code,
                                    "reason_code": failure.reason_code,
                                    "diagnostics": failure.diagnostics,
                                },
                                persist_before_delivery=True,
                            )
                        )
                    except Exception as _ee:
                        log.warning("worker.error_emit_failed", turn_id=req.turn_id, error=str(_ee))
                if not terminal_fact_confirmed:
                    # The worker may run its local cleanup, but its Task must
                    # remain failed so the dispatch monitor cannot confuse a
                    # swallowed exception with a durable success. The spawned
                    # lease stays active for expiry recovery.
                    _unconfirmed_worker_error = e
                result = None
        if terminal_runtime is None:
            try:
                _result_turn = await orchestrator.session_store.get_turn(
                    req.turn_id,
                )
            except Exception as fact_error:
                _unconfirmed_worker_error = RuntimeError(
                    "turn terminal fact is unavailable after worker return",
                )
                _unconfirmed_worker_error.add_note(repr(fact_error))
                result = None
            else:
                _durable_result_status = str(
                    getattr(_result_turn, "status", "") or "",
                )
                if _durable_result_status not in LEGACY_TURN_WORKER_SETTLED_STATUSES:
                    _unconfirmed_worker_error = RuntimeError(
                        "turn terminal fact is not confirmed after worker return",
                    )
                    result = None
                elif result is not None:
                    _reported_status = getattr(
                        getattr(result, "status", None),
                        "value",
                        getattr(result, "status", None),
                    )
                    if str(_reported_status or "") != _durable_result_status:
                        log.error(
                            "worker.result_terminal_fact_mismatch",
                            turn_id=req.turn_id,
                            reported_status=str(_reported_status or ""),
                            durable_status=_durable_result_status,
                        )
                        result = None
        # P4.5.10: post-flight credit deduction errors remain log-only, matching prior behavior;
        # never touch a completed turn's status/token ledger.
        if user_store is not None and team_id and result is not None:
            billing_state = await _resolve_postflight_billing_state(
                terminal_runtime,
                req,
                team_id=team_id,
            )
            if billing_state in _LEGACY_POST_FLIGHT_BILLING_STATES:
                billing_tokens_input, billing_tokens_output = await _billable_turn_usage(
                    orchestrator,
                    terminal_runtime,
                    result,
                    turn_id=req.turn_id,
                )
                from bowhead_core.physical_usage import read_turn_credit_usage
                from bowhead_core.rate_card import credits_for_model_lines

                rate_version, model_usage = await read_turn_credit_usage(
                    orchestrator.session_store.db_path,
                    turn_id=req.turn_id,
                    generation=req.execution_generation,
                    projected_input=billing_tokens_input,
                    projected_output=billing_tokens_output,
                )
                if billing_tokens_input + billing_tokens_output > 0 or model_usage:
                    cost = (
                        credits_for_model_lines(model_usage)
                        if rate_version == "v2"
                        else credits_for_tokens(billing_tokens_input, billing_tokens_output)
                    )
                    ok, reason = await user_store.try_deduct_credit(
                        user_id=req.user_id,
                        team_id=team_id,
                        cost_credits=cost,
                        turn_id=req.turn_id,
                        workspace_id=req.workspace_id,
                        tokens_input=billing_tokens_input,
                        tokens_output=billing_tokens_output,
                        model_usage=model_usage,
                        rate_card_version=rate_version,
                    )
                    if not ok:
                        event = await _record_credit_exhaustion_best_effort(
                            user_store,
                            metrics_log=metrics_log,
                            team_id=team_id,
                            user_id=req.user_id,
                            workspace_id=req.workspace_id,
                            code="credit_deduct_failed",
                            turn_id=req.turn_id,
                        )
                        await _record_credit_deduct_failed_metric_best_effort(
                            metrics_log,
                            team_id=team_id,
                            user_id=req.user_id,
                            workspace_id=req.workspace_id,
                            turn_id=req.turn_id,
                            deduct_reason=reason,
                            cost_credits=cost,
                            tokens_input=billing_tokens_input,
                            tokens_output=billing_tokens_output,
                            credit_exhaustion_event=event,
                        )
                        log.warning(
                            "credit.deduct_failed",
                            turn_id=req.turn_id,
                            reason=reason,
                            cost=cost,
                        )
            elif billing_state is not _PostFlightBillingState.CONTRACT_V2:
                log.error(
                    "credit.postflight_contract_blocked",
                    turn_id=req.turn_id,
                    state=billing_state.value,
                )
                if metrics_log is not None:
                    try:
                        await metrics_log.track(
                            "credit.postflight_contract_blocked",
                            attrs={
                                "state": billing_state.value,
                                "turn_id": req.turn_id,
                            },
                        )
                    except Exception:
                        log.warning(
                            "credit.postflight_contract_metric_failed",
                            turn_id=req.turn_id,
                            state=billing_state.value,
                        )
    except Exception as e:
        # fix/sse-fanout-bus r3: only the credit-deduction segment reaches here (inner handler consumed
        # run_turn exceptions). Retain log-only behavior: turn completed successfully, leave status untouched.
        log.exception("credit.deduct_unhandled_exception", turn_id=req.turn_id, error=str(e))
    finally:
        if terminal_runtime is not None:
            # Never let the terminal-only branch's early return swallow worker
            # cancellation/SystemExit.  The return only skips the legacy
            # active-pointer/buffer finalizer after normal v1 completion.
            import sys

            active_exception = sys.exc_info()[0] is not None
            if terminal_runtime.bus.receipt is None:
                handoff = asyncio.create_task(
                    terminal_runtime.handoff_to_recovery(reason_code=("worker_cancelled" if active_exception else "worker_lost")),
                    name=f"terminal-worker-handoff:{req.turn_id}",
                )
                _terminal_cleanup_tasks.add(handoff)
                handoff.add_done_callback(_terminal_cleanup_tasks.discard)
                await asyncio.shield(handoff)
            else:
                try:
                    await asyncio.sleep(5)
                finally:
                    await terminal_runtime.retire()
                await _release_plan_execution_tail()
            if active_exception:
                raise
            return None  # noqa: B012
        import sys as _sys

        _active_legacy_error = _sys.exc_info()[1]
        if not _dispatch_owner_lost and (_unconfirmed_worker_error is not None or _active_legacy_error is not None):
            # A local task failure/cancellation without a canonical Store fact
            # is a recovery handoff, not a normal chat finalization. Preserve
            # active_turn_id, the accepted steer buffer and the spawned lease;
            # otherwise a new Turn could start while recovery still owns this
            # running parent. The spawn task-done fallback waits for the lease
            # monitor's recovery decision before retiring this producer bus.
            _handoff_error = _active_legacy_error or _unconfirmed_worker_error
            assert _handoff_error is not None
            if _active_legacy_error is not None:
                raise
            assert _unconfirmed_worker_error is not None
            raise _unconfirmed_worker_error
        if _dispatch_owner_lost:
            # The durable lease recovery transaction owns any accepted steer
            # materialization.  This cancelled worker must not independently
            # drain/spawn the same buffer after losing ownership. The task-done
            # fallback retires the bus after the monitor records that decision.
            await _release_plan_execution_tail()
            return None  # noqa: B012
        # ``materialize_steer_follow_up`` drains the buffer and swaps the active
        # pointer in one transaction. A concurrent /send therefore sees either
        # the parent or the fully materialized child, never an orphan window.
        _buffered_texts: list[str] = list(_preterminal_buffered_texts)
        _follow_up_materialized = _preterminal_materialized
        # Re-check admission before executing the steer follow-up.  Admission
        # failure may stop execution, but it must not erase work whose buffered
        # receipt was already acknowledged: that work is materialized below as
        # a durable, retryable failed child with its canonical source intact.
        _drain_ok = True
        _drain_halt_reason = "entitlement"
        if entitlements is not None and team_id:
            try:
                _drain_ok = (await entitlements.get(team_id)).health in ("active", "grace")
            except Exception as _ee:
                log.warning("steer.entitlement_recheck_failed", parent_turn=req.turn_id, error=str(_ee))
        if _drain_ok and team_id:
            from bowhead_gateway.turn_service import record_team_model_resolution

            if getattr(req, "team_router", None) is None:
                _drain_ok = False
                _drain_halt_reason = "team_model_runtime"
                record_team_model_resolution(
                    entrypoint="steer",
                    result="team_unavailable",
                    reason="router_unavailable",
                )
            else:
                record_team_model_resolution(
                    entrypoint="steer",
                    result="team_safe_success",
                    reason="resolved",
                )
        # feedback-0713 F2: session soft-deleted during this turn (R1 report-deletion linkage) ->
        # no steering follow-up spawn (same boundary as /send's 410 gate: reject, never revive).
        # Reuse _drain_ok: with empty buffer, materializer only clears active, creates no child.
        if _drain_ok and req.session_id:
            from bowhead_gateway.turn_service import is_session_deleted as _is_sess_deleted

            if await _is_sess_deleted(orchestrator, req.session_id):
                _drain_ok = False
                _drain_halt_reason = "session_deleted"
        if req.session_id and not _preterminal_deferred:
            try:
                if _preterminal_attempted:
                    # Normal path was atomically drained by the terminal hook.
                    pass
                else:
                    _buffered_texts = await orchestrator.session_store.materialize_steer_follow_up(
                        session_id=req.session_id,
                        current_turn_id=req.turn_id,
                        next_turn_id=_new_turn_id,
                    )
                    _follow_up_materialized = bool(_buffered_texts)
            except Exception as _pe:
                log.warning("steer.materialize_follow_up_failed", sid=req.session_id, error=str(_pe))
                try:
                    _preterminal_deferred = await orchestrator.session_store.mark_chat_spawn_dispatch_drain_pending(
                        turn_id=req.turn_id,
                        session_id=req.session_id,
                    )
                except Exception:
                    _preterminal_deferred = False
                    log.warning(
                        "steer.finalize_retry_marker_failed",
                        parent_turn=req.turn_id,
                        exc_info=True,
                    )
        if _buffered_texts:
            try:
                from dataclasses import replace as _dc_replace

                if not _follow_up_materialized:
                    raise RuntimeError("steer_follow_up_not_materialized")
                _follow_up_source = await orchestrator.session_store.get_chat_turn_user_source(
                    turn_id=_new_turn_id,
                    user_id=req.user_id,
                    workspace_id=req.workspace_id,
                )
                if _follow_up_source is None:
                    raise RuntimeError("steer_follow_up_source_missing")
                _merged_text = _follow_up_source.user_text
                _new_req = _dc_replace(
                    req,
                    turn_id=_new_turn_id,
                    # A steer continuation is a new durable Turn, not another
                    # generation of its parent. DB, SSE discovery and worker
                    # request must all start at generation 1.
                    execution_generation=1,
                    text=_merged_text,
                    attachments=list(_follow_up_source.attachment_ids),
                    # Tie the spawned turn to the original via resume_turn_id so
                    # Lead skips heuristic gate (mirrors clarification-resume).
                    resume_turn_id=req.turn_id,
                    # No L3 hint on the steer-resume turn — user already provided text.
                    l3_picker_hint=None,
                    reference_scope=_preterminal_reference_scope,
                    reference_runtime=None,
                    reference_clarification_id=None,
                    document_clarification_id=None,
                    document_checkpoint=None,
                    document_revision_target=_follow_up_source.document_revision_target,
                )
                if not _follow_up_announced:
                    try:
                        from bowhead_core.events import OrchestratorEvent

                        await registry.emit_out_of_band(
                            req.turn_id,
                            OrchestratorEvent(
                                event_type="turn.follow_up",  # type: ignore[arg-type]
                                turn_id=req.turn_id,
                                payload={
                                    "follow_up_turn_id": _new_turn_id,
                                    "follow_up_stream_url": (f"/api/v1/chat/stream/{_new_turn_id}"),
                                    "reason": "steer_drain",
                                    "buffered_count": len(_buffered_texts),
                                    "attachments": list(_follow_up_source.attachment_refs),
                                    "execution_generation": 1,
                                },
                                persist_before_delivery=True,
                            ),
                        )
                    except Exception as _fue:
                        log.warning(
                            "steer.follow_up_emit_failed",
                            parent_turn=req.turn_id,
                            error=str(_fue),
                        )
                # Follow-up child turns are new model work. Parent settled above; balance ≤0 forbids startup, or steering
                # could continue indefinitely after credit exhaustion. Check only when content truly needs continuation (record every check).
                _drain_halt_code: str | None = None
                if _drain_ok and team_id:
                    try:
                        _drain_halt_code = await check_credit_admission(
                            user_store,
                            team_id=team_id,
                            user_id=req.user_id,
                            workspace_id=req.workspace_id,
                            metrics_log=metrics_log,
                            turn_id=_new_turn_id,
                        )
                    except Exception as _ce:
                        log.warning("steer.credit_recheck_failed", parent_turn=req.turn_id, error=str(_ce))
                    if _drain_halt_code is not None:
                        _drain_ok = False
                        _drain_halt_reason = "credit"
                if _drain_ok:
                    from bowhead_gateway.turn_service import (
                        admit_legacy_chat_execution as _admit_legacy_chat_execution,
                    )
                    from bowhead_gateway.turn_service import (
                        spawn_turn as _spawn_turn,
                    )

                    child_bus = await _admit_legacy_chat_execution(
                        orchestrator,
                        _new_req,
                    )

                    await _spawn_turn(
                        orchestrator,
                        _new_req,
                        user_store=user_store,
                        team_id=team_id,
                        metrics_log=metrics_log,
                        entitlements=entitlements,
                        name_prefix="turn_run_steer_resume",
                        admitted_legacy_bus=child_bus,
                        reference_skill_sources=(_preterminal_reference_sources),
                        reference_mcp_index=_preterminal_reference_mcp_index,
                        reference_mcp_access=_preterminal_reference_mcp_access,
                        reference_mcp_prompt=_preterminal_reference_mcp_prompt,
                    )
                    log.info("steer.spawned_followup_turn", parent_turn=req.turn_id, new_turn=_new_turn_id, buffered_count=len(_buffered_texts))
                else:
                    # Credit rejection stores which user/team layer exhausted in turns.error_kind for diagnosis. ⚠ Frontend still gets
                    # generic failure: recovery turn_failed projects reason_code=lead_internal_error for every failure;
                    # user retry goes through /chat/send's 402 and gets precise credit messaging.
                    error_kind = _drain_halt_code or f"steer_{_drain_halt_reason}_unavailable"
                    from bowhead_gateway.turn_service import (
                        handoff_legacy_spawn_failure_to_recovery,
                    )

                    handoff = await handoff_legacy_spawn_failure_to_recovery(
                        orchestrator.session_store,
                        turn_id=_new_turn_id,
                        session_id=req.session_id,
                        owner_id=orchestrator.dispatch_owner_id,
                        error="accepted steer could not be executed",
                        error_kind=error_kind,
                    )
                    if handoff == "foreign_active":
                        log.warning(
                            "steer.drain_halted_foreign_owner",
                            parent_turn=req.turn_id,
                            new_turn=_new_turn_id,
                        )
                    log.info(
                        "steer.drain_halted",
                        reason=_drain_halt_reason,
                        parent_turn=req.turn_id,
                        new_turn=_new_turn_id,
                    )
            except Exception as _se:
                log.exception("steer.spawn_followup_failed", parent_turn=req.turn_id, error=str(_se))
        _tail_error: BaseException | None = _unconfirmed_worker_error

        async def _run_parent_tail_step(
            operation: str,
            step: Callable[[], Awaitable[Any]],
        ) -> None:
            nonlocal _tail_error
            try:
                await step()
            except Exception:
                log.warning(
                    "steer.parent_tail_cleanup_failed",
                    operation=operation,
                    parent_turn=req.turn_id,
                    exc_info=True,
                )
            except BaseException as cleanup_error:
                log.warning(
                    "steer.parent_tail_cleanup_aborted",
                    operation=operation,
                    parent_turn=req.turn_id,
                    exc_info=True,
                )
                if _tail_error is None:
                    _tail_error = cleanup_error
                else:
                    _tail_error.add_note(
                        f"steer parent tail failed to {operation}: {cleanup_error!r}",
                    )

        # BLOCKED is retryable, but the previous Turn may still be composing its
        # report / terminal events. Keep the exact Plan token through every side
        # effect, and complete every cleanup stage before surfacing cancellation.
        await _run_parent_tail_step(
            "wait for SSE drain grace",
            lambda: asyncio.sleep(max(0.0, cleanup_grace_s)),
        )
        await _run_parent_tail_step(
            "close producer bus",
            lambda: registry.close(req.turn_id, expected=_worker_bus),
        )
        await _run_parent_tail_step(
            "release plan execution",
            _release_plan_execution_tail,
        )
        if _tail_error is not None:
            raise _tail_error
    return result
