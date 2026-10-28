"""Turn event log + replay API.

GET  /api/v1/turns                       → 当前团队范围内最近的 turn_id
GET  /api/v1/turns/{turn_id}/events      → 全部已持久化事件
GET  /api/v1/turns/{turn_id}/replay      → SSE，按序推送事件 (客户端断线重连用)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid5

from bowhead_core.events import OrchestratorEvent, get_event_registry
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.terminal_protocol import is_explicit_user_cancel_receipt
from bowhead_core.user_store import User, UserStore
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import UUID4, UUID5, BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from bowhead_gateway.access_context import (
    AccessContextError,
    build_access_context_for_user_from_request,
    is_space_authorization_denial,
    require_space_lifecycle_without_authorization,
    space_denial_code,
)
from bowhead_gateway.dtos import TurnEventDTO, TurnEventsListDTO, TurnIdsDTO
from bowhead_gateway.routes.auth import _current_user, _dev_user_fallback_enabled
from bowhead_gateway.source_access_projection import (
    ViewerSourceProjectionError,
    project_event_payloads_for_viewer,
)
from bowhead_gateway.terminal_protocol_rollout import (
    PersistedChatExecutionProtocol,
    resolve_persisted_chat_execution_protocol,
)

if TYPE_CHECKING:
    from bowhead_core.session_store import SessionStore
    from bowhead_core.terminal_protocol import TerminalScope
    from bowhead_core.terminal_protocol_store import TerminalProtocolStore
    from bowhead_core.turn_log_store import TurnLogStore

logger = logging.getLogger(__name__)
router = APIRouter()


class CancelTurnRequest(BaseModel):
    cancel_request_id: UUID5
    source: Literal["explicit_stop"]
    expected_execution_generation: int = Field(ge=1)


class ManualEffectSettlementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(min_length=1, max_length=128)
    expected_attempt_row_version: int = Field(ge=1)
    outcome: Literal[
        "confirmed_applied",
        "confirmed_not_applied",
        "compensated",
    ]
    settlement_code: str = Field(min_length=1, max_length=128)
    evidence_digest: str = Field(min_length=1, max_length=256)
    object_version_digest: str = Field(min_length=1, max_length=256)


class AgentExecutionAttributionDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_execution_id: str
    parent_execution_id: str | None = None
    agent_id: str
    agent_source_kind: Literal["team", "system"]
    agent_revision_id: str
    agent_release_id: str | None = None
    dispatch_source: Literal["explicit", "lead", "plan", "system"]
    dispatch_key: str
    runtime_profile_name: str
    display_name_snapshot: str
    capability_fingerprint: str
    status: Literal["admitted", "running", "completed", "failed", "blocked", "cancelled"]
    created_at: str
    started_at: str | None = None
    ended_at: str | None = None
    failure_code: str | None = None


class RootAgentBindingDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    agent_source_kind: Literal["team", "system"]
    agent_revision_id: str
    agent_release_id: str | None = None
    binding_source: Literal["marketplace", "detail", "hr", "mention", "schedule", "plan", "lead"]
    capability_fingerprint: str
    created_at: str


class TurnAgentBindingDetailDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str
    root_binding: RootAgentBindingDTO | None = None
    executions: list[AgentExecutionAttributionDTO]


_EXPLICIT_STOP_NAMESPACE = UUID("0de3db36-d292-5fb8-80e2-eac393019ff4")


def explicit_stop_request_id(
    *,
    team_id: str,
    user_id: str,
    turn_id: str,
    execution_generation: int,
) -> UUID:
    fields = (
        team_id,
        user_id,
        turn_id,
        str(execution_generation),
        "explicit_stop",
    )
    encoded = bytearray()
    for field in fields:
        value = field.encode("utf-8")
        if len(value) > 0xFFFF:
            raise ValueError("cancel identity field is too long")
        encoded.extend(len(value).to_bytes(2, "big"))
        encoded.extend(value)
    name = base64.urlsafe_b64encode(bytes(encoded)).decode("ascii").rstrip("=")
    return uuid5(_EXPLICIT_STOP_NAMESPACE, name)


def _require_explicit_stop_request_id(
    *,
    actual: UUID,
    team_id: str,
    user_id: str,
    turn_id: str,
    execution_generation: int,
) -> None:
    expected = explicit_stop_request_id(
        team_id=team_id,
        user_id=user_id,
        turn_id=turn_id,
        execution_generation=execution_generation,
    )
    if actual != expected:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": {"code": "cancel_request_identity_mismatch"}},
        )


async def _record_terminal_cancel_receipt(
    session_store: SessionStore,
    *,
    team_id: str,
    user_id: str,
    cancel_request_id: str,
    workspace_id: str,
    turn_id: str,
    expected_execution_generation: int,
    outcome: Literal["cancelled", "terminal", "generation_conflict"],
) -> Literal["cancelled", "terminal", "generation_conflict"]:
    recorder = getattr(session_store, "record_terminal_cancel_receipt", None)
    if not callable(recorder):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "terminal_protocol_writer_not_ready",
        )
    try:
        return await recorder(
            team_id=team_id,
            user_id=user_id,
            cancel_request_id=cancel_request_id,
            workspace_id=workspace_id,
            turn_id=turn_id,
            expected_execution_generation=expected_execution_generation,
            outcome=outcome,
        )
    except ValueError as exc:
        code = str(exc)
        if code == "cancel_request_conflict":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"error": {"code": "cancel_request_conflict"}},
            ) from exc
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": {"code": code}},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "turn cancellation receipt could not be persisted",
        ) from exc


async def _cancel_plan_for_turn(request: Request, *, turn_id: str) -> bool:
    """Durably repair the Plan cascade before an accepted Stop ACK."""

    plan_store = getattr(request.app.state, "plan_store", None)
    if plan_store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "plan cancellation writer not ready",
        )
    cancel_execution = getattr(plan_store, "cancel_execution_for_turn", None)
    if not callable(cancel_execution):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "plan cancellation writer not ready",
        )
    try:
        return await cancel_execution(turn_id)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "plan cancellation could not be persisted",
        ) from exc


def _log(request: Request) -> TurnLogStore:
    s: TurnLogStore | None = getattr(request.app.state, "turn_log_store", None)
    if s is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "turn log store not configured")
    return s


def _optional_log(request: Request) -> TurnLogStore | None:
    """Reader-first v1 routes do not require the legacy log dependency."""

    return getattr(request.app.state, "turn_log_store", None)


def _session_store(request: Request) -> SessionStore:
    s: SessionStore | None = getattr(request.app.state, "session_store", None)
    if s is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "session store not configured")
    return s


def _user_store(request: Request) -> UserStore:
    s: UserStore | None = getattr(request.app.state, "user_store", None)
    if s is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "user_store not configured")
    return s


async def _audit_admin_turn_view(
    request: Request,
    *,
    admin_user_id: str,
    team_id: str,
    turn_id: str,
    target_user_id: str,
    workspace_id: str,
    operation: SpaceOperation,
) -> None:
    """管理员跨用户读 turn 的留痕（空间隔离批4 ③）—— auditing the auditor。

    缺口：`is_same_team_admin` 那条豁免让同团队 owner / team_admin 读本团队任意空间的
    turn，**今天完全不留痕**。为了回答「这条豁免有没有人用」，2026-09-17 那次量花了一个
    40 分钟的 agent 跑，靠 nginx 日志 + 登录事件按 IP ±10s 配对**猜**，8.9% 的请求根本
    归因不了；而 nginx 日志跟着容器走，当天发版重建容器后那段历史就没了。

    事件名对齐既有 ``admin.replay.viewed`` 家族（那条只覆盖 memory_replay 路由，
    **不覆盖本文件**）。

    ★ **best-effort**：审计是旁路副作用，**不改任何授权判定**。`user_audit_log` 抖一下
    不该让管理员读不了 turn；老夹具 / 精简 store 没有 `log_audit` 方法也不得 AttributeError。
    """
    user_store = getattr(request.app.state, "user_store", None)
    log_audit = getattr(user_store, "log_audit", None)
    if not callable(log_audit):
        return
    try:
        await log_audit(
            user_id=admin_user_id,
            team_id=team_id,
            event_type="admin.turn.viewed",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            payload={
                "turn_id": turn_id,
                "target_user_id": target_user_id,
                "workspace_id": workspace_id,
                "operation": str(operation),
            },
        )
    except Exception as exc:  # noqa: BLE001 - Audit write failures must not affect reads.
        logger.warning("turn.admin_view_audit_failed turn=%s error=%s", turn_id, type(exc).__name__)


async def _require_turn_access(
    turn_id: str,
    request: Request,
    user: User | None = None,
    *,
    operation: SpaceOperation = SpaceOperation.READ_HISTORY,
    allow_archived_team_admin: bool = False,
    allow_owner_without_space_membership: bool = False,
) -> User:
    """Allow the turn owner or an team admin in the same team.

    ``allow_owner_without_space_membership``：只给「停止」一族用（``POST /cancel`` 与查询停止结果）。
    turn 的属主即使已被移出该空间，也仍能停掉**自己**的 turn——否则那一轮会继续以他的身份跑完、
    写内容、扣额度，界面还一直提示「请重试」（2026-09-24 评审）。仍要求属主在团队里，空间生命周期照常判定。

    Turn event logs do not carry tenant metadata, so access must be derived
    through the canonical turns -> sessions -> users relationship.
    """
    if user is None and _dev_user_fallback_enabled(request):
        settings = request.app.state.settings
        hdr = request.headers.get("x-bowhead-user-id")
        uid = hdr.strip() if hdr and hdr.strip() else settings.dev_default_user
        return User(
            id=uid,
            team_id="",
            email=f"{uid}@dev.local",
            email_verified=True,
            tier="ultra",
            status="active",
            credit_balance=999_999,
            created_at="2026-01-01T00:00:00Z",
        )
    user = user or await _current_user(request)
    if _dev_user_fallback_enabled(request):
        return user
    session_store = _session_store(request)
    turn = await session_store.get_turn(turn_id)
    if turn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    session = await session_store.get_session(turn.session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    user_store = _user_store(request)
    workspace = await user_store.get_workspace(session.workspace_id)
    if workspace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    target_team_id = workspace.get("team_id")
    if not isinstance(target_team_id, str) or not target_team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    caller_role = await user_store.get_team_role(user.id, target_team_id)
    is_owner = session.user_id == user.id and caller_role is not None
    is_same_team_admin = caller_role in {"owner", "team_admin"}
    if not is_owner and not is_same_team_admin:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    if not await user_store.workspace_in_team(
        target_team_id,
        session.workspace_id,
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    if (
        workspace["status"] == "archived"
        and operation is SpaceOperation.READ_HISTORY
        and not (allow_archived_team_admin and is_same_team_admin)
        and await user_store.get_member_role(session.workspace_id, user.id) is None
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    if not (allow_owner_without_space_membership and is_owner):
        # Batch 2 (tech-design §4.2): active spaces also require membership checks. Lifecycle-only checks let
        # removed session owners pass throughout (PRD §4.3 observed 200 + original text disclosure).
        #
        # ★ allow_team_admin_override=True **preserves an existing path**, without adding privileges:
        # is_same_team_admin above at :304 lets same-team owners/team_admins read turns in any team space without space
        # membership. PRD §5.5 explicitly retained **option A** because nobody had decided to narrow it.
        # Pass this on **every** turn route, not merely the three using
        # allow_archived_team_admin=True, which only narrow the archived case.
        # ★ Archived rejection above at :312-318 **still runs**, narrowing archived access.
        # Removing it and unconditionally permitting here **relaxes** access (archived + admin + nonmember changes from 404
        # to 200), rather than narrowing it.
        # ★ expected_team_id: this function resolves caller_role using the **workspace's owning team**;
        # users.team_id can differ for multi-team users, so omission falsely classifies them as cross_team.
        try:
            await build_access_context_for_user_from_request(
                request,
                user_id=user.id,
                workspace_id=session.workspace_id,
                operation=operation,
                allow_team_admin_override=True,
                expected_team_id=target_team_id,
            )
        except HTTPException as exc:
            if is_space_authorization_denial(space_denial_code(exc)):
                # D3: the entire turns family rejects with anti-enumeration 404; authorization failures must not escape as 403.
                # Lifecycle failures (409 workspace_archived) still expose their original status code.
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    "turn not found",
                ) from exc
            raise
    else:
        try:
            await require_space_lifecycle_without_authorization(
                user_store=user_store,
                workspace_id=session.workspace_id,
                operation=operation,
            )
        except AccessContextError as exc:
            raise HTTPException(
                exc.status_code,
                {"error": {"code": exc.code}},
            ) from exc
    # Space isolation batch 4 ③: audit admins reading **other users'** turns. After both gates, recording only actual successful reads.
    if is_same_team_admin and not is_owner:
        await _audit_admin_turn_view(
            request,
            admin_user_id=user.id,
            team_id=target_team_id,
            turn_id=turn_id,
            target_user_id=session.user_id,
            workspace_id=session.workspace_id,
            operation=operation,
        )
    return user


async def _effect_scope_for_turn(
    turn_id: str,
    request: Request,
) -> tuple[TerminalProtocolStore, TerminalScope]:
    from bowhead_core.terminal_protocol import TerminalScope
    from bowhead_core.terminal_protocol_store import TerminalReadContext
    from bowhead_core.terminal_runtime_factory import (
        terminal_auth_resolve_context_values,
    )

    protocol_store = getattr(request.app.state, "terminal_protocol_store", None)
    if protocol_store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"error": {"code": "terminal_dependency_recovering"}},
        )
    session_store = _session_store(request)
    turn = await session_store.get_turn(turn_id)
    session = await session_store.get_session(turn.session_id) if turn is not None else None
    if turn is None or session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    try:
        team_id, persisted_session_id, protocol_version = await protocol_store.read_persisted_turn_scope(turn_id=turn_id)
        if (
            resolve_persisted_chat_execution_protocol(protocol_version) is not PersistedChatExecutionProtocol.TERMINAL_V1
            or persisted_session_id != session.id
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
        owner_auth = terminal_auth_resolve_context_values(
            team_id=team_id,
            session_id=session.id,
            user_id=session.user_id,
            resource_ref=turn_id,
        )
        vector = await protocol_store.read_auth_fence_vector(owner_auth)
        summary = await protocol_store.read_protocol_summary(
            TerminalReadContext(
                team_id=team_id,
                session_id=session.id,
                turn_id=turn_id,
                viewer=session.user_id,
                auth_fence_vector_digest=vector.auth_fence_vector_digest,
            )
        )
        if summary.current_generation is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
        return (
            protocol_store,
            TerminalScope(
                team_id=team_id,
                session_id=session.id,
                turn_id=turn_id,
                generation=summary.current_generation,
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _effect_reader_http_error(exc) from exc


def _effect_reader_http_error(error: BaseException) -> HTTPException:
    from bowhead_core.terminal_protocol import (
        TerminalProtocolFutureVersionError,
        TerminalProtocolMalformedError,
    )
    from bowhead_core.terminal_protocol_store import DecisionAccessRevokedError

    if isinstance(error, DecisionAccessRevokedError):
        return HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    if isinstance(error, TerminalProtocolFutureVersionError):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": {"code": "terminal_protocol_future_version"}},
        )
    if isinstance(error, TerminalProtocolMalformedError):
        return HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            {"error": {"code": "terminal_protocol_malformed"}},
        )
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        {"error": {"code": "terminal_dependency_recovering"}},
    )


@router.get(
    "/{turn_id}/agent-binding",
    response_model=TurnAgentBindingDetailDTO,
    response_model_exclude_none=True,
)
async def get_agent_binding_detail(
    turn_id: str,
    request: Request,
) -> TurnAgentBindingDetailDTO:
    """Recover the exact Agent ledger without replaying browser event state."""

    await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    session_store = _session_store(request)
    turn = await session_store.get_turn(turn_id)
    session = await session_store.get_session(turn.session_id) if turn is not None else None
    workspace = await _user_store(request).get_workspace(session.workspace_id) if session is not None else None
    team_id = workspace.get("team_id") if workspace is not None else None
    if not isinstance(team_id, str) or not team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore

    try:
        detail = await AgentExecutionBindingStore(
            _user_store(request).db_path,
        ).read_turn_binding_detail(team_id=team_id, turn_id=turn_id)
    except Exception as exc:
        logger.exception("turn.agent_binding_read_failed turn=%s", turn_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"error": {"code": "agent_binding_reader_unavailable"}},
        ) from exc
    return TurnAgentBindingDetailDTO.model_validate(detail)


async def _reference_visible_resource_identities(
    request: Request,
    *,
    user: User,
    team_id: str,
    workspace_id: str,
    session_id: str,
) -> tuple[frozenset[str], bool]:
    """Resolve current exact resource visibility without using the runtime flag."""

    from bowhead_core.chat_reference_catalog import ReferenceCatalogRequest, read_reference_metadata
    from bowhead_core.chat_reference_projection import reference_resource_identity

    access_store = getattr(request.app.state, "access_control_store", None)
    if access_store is None:
        return frozenset(), False
    try:
        context = await build_access_context_for_user_from_request(
            request,
            user_id=user.id,
            workspace_id=workspace_id,
            operation=SpaceOperation.READ_HISTORY,
            expected_team_id=team_id,
        )

        async def skill_access():
            return access_store, context

        def current(name: str):
            cache = getattr(request.app.state, name, None)
            return cache.current() if cache is not None else None

        common = {
            "db_path": _user_store(request).db_path,
            "team_id": team_id,
            "user_id": user.id,
            "locale": user.locale or "en-US",
            "builtin_skill_index": getattr(request.app.state, "builtin_skill_reference_metadata", None),
            "builtin_skill_access": skill_access,
            "uploaded_skill_index": current("uploaded_skill_reference_metadata"),
            "system_agent_index": current("system_agent_reference_metadata"),
            "receiver_index": current("reference_receiver_metadata"),
            "mcp_index": current("reference_mcp_metadata"),
        }
        groups = []
        for trigger in ("@", "/"):
            _, current_groups, _ = await read_reference_metadata(
                query=ReferenceCatalogRequest(
                    workspace_id=workspace_id,
                    session_id=session_id,
                    trigger=trigger,
                ),
                **common,
            )
            groups.extend(current_groups)
        identities = {
            reference_resource_identity(item.resource)
            for group in groups
            for item in group.items
            if item.resource is not None and item.reason_code == "unsupported_entry"
        }
        complete = not any(group.error is not None for group in groups if group.id in {"team_agents", "system_agents", "skills", "mcp"})
        return frozenset(identities), complete
    except Exception as exc:
        logger.warning("turn.reference_visibility_unavailable error=%s", type(exc).__name__)
        return frozenset(), False


async def _reference_projection(
    turn_id: str,
    request: Request,
    *,
    include_content: bool,
    submission_id: str | None = None,
) -> dict[str, Any]:
    from bowhead_core.chat_reference_projection import ReferenceProjectionError, read_reference_projection
    from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope

    user = await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    turn = await _session_store(request).get_turn(turn_id)
    session = await _session_store(request).get_session(turn.session_id) if turn is not None else None
    workspace = await _user_store(request).get_workspace(session.workspace_id) if session is not None else None
    team_id = workspace.get("team_id") if workspace is not None else None
    # The evidence API is intentionally actor-scoped. Team governance rights do
    # not grant another member's task text or private reference identities.
    if session is None or session.user_id != user.id or not isinstance(team_id, str) or not team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    context = await build_access_context_for_user_from_request(
        request,
        user_id=user.id,
        workspace_id=session.workspace_id,
        operation=SpaceOperation.READ_HISTORY,
        expected_team_id=team_id,
    )
    visible, visibility_complete = await _reference_visible_resource_identities(
        request,
        user=user,
        team_id=team_id,
        workspace_id=session.workspace_id,
        session_id=session.id,
    )
    cipher = None
    if include_content or submission_id is not None:
        try:
            cipher = ReferenceCipher(request.app.state.settings.fernet_key)
        except (AttributeError, ValueError, TypeError):
            import os

            try:
                cipher = ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", ""))
            except ValueError:
                cipher = None
    try:
        projection = await read_reference_projection(
            _user_store(request).db_path,
            scope=ReferenceScope(
                team_id=team_id,
                user_id=user.id,
                workspace_id=session.workspace_id,
                session_id=session.id,
                policy_epoch=context.policy_epoch,
            ),
            turn_id=turn_id,
            visible_resource_identities=visible,
            visibility_complete=visibility_complete,
            include_content=include_content,
            cipher=cipher,
            submission_id=submission_id,
        )
    except ReferenceProjectionError as exc:
        if str(exc) == "reference_projection_not_found":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found") from exc
        logger.warning("turn.reference_projection_unavailable turn=%s code=%s", turn_id, exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"error": {"code": "reference_projection_unavailable"}},
        ) from exc
    if include_content:
        try:
            await _user_store(request).log_audit(
                user_id=user.id,
                team_id=team_id,
                event_type="reference_evidence_content_read",
                ip=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                payload={
                    "turn_id": turn_id,
                    "facts_version": projection["facts_version"],
                    "projection_etag": projection["projection_etag"],
                    **({"submission_id": submission_id} if submission_id is not None else {}),
                },
            )
        except Exception as exc:
            logger.error("turn.reference_content_audit_failed turn=%s", turn_id)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                {"error": {"code": "reference_content_audit_unavailable"}},
            ) from exc
    return projection


@router.get("/{turn_id}/references")
async def get_reference_state(
    turn_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Return the user-facing state derived from canonical reference facts."""

    projection = await _reference_projection(turn_id, request, include_content=False)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["ETag"] = f'"{projection["projection_etag"]}"'
    return {
        key: projection[key]
        for key in (
            "facts_version",
            "turn_id",
            "source_locator",
            "composition",
            "references",
            "evidence_completeness",
            "projection_etag",
        )
    }


@router.get("/{turn_id}/collaboration")
async def get_collaboration_trace(turn_id: str, request: Request, response: Response) -> dict[str, Any]:
    """Read current collaboration evidence for the original task actor."""
    import os

    from bowhead_core.agent_collaboration import CollaborationStore, authorize_result_exchange
    from bowhead_core.chat_reference_storage import ReferenceCipher
    from bowhead_core.document_chat_continuation import DocumentTurnOwner

    user = await _require_turn_access(turn_id, request, operation=SpaceOperation.READ_HISTORY)
    sessions = _session_store(request)
    turn = await sessions.get_turn(turn_id)
    session = await sessions.get_session(turn.session_id) if turn is not None else None
    workspace = await _user_store(request).get_workspace(session.workspace_id) if session is not None else None
    team_id = workspace.get("team_id") if workspace is not None else None
    # Governance access to event logs does not disclose another actor's private
    # collaboration evidence. The same caller/resource reader owns live detail.
    if session is None or session.user_id != user.id or not isinstance(team_id, str) or not team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    try:
        cipher = ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", ""))
    except ValueError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "collaboration unavailable") from exc
    # This identity is only used for a read transaction. It cannot pass the
    # execution fence or admit, wake, acknowledge, or otherwise mutate a task.
    reader = DocumentTurnOwner(
        team_id=team_id,
        user_id=user.id,
        workspace_id=session.workspace_id,
        session_id=session.id,
        turn_id=turn_id,
        generation=1,
        owner_id="history-reader",
    )
    try:
        result = await CollaborationStore(_user_store(request).db_path, cipher=cipher).projection(reader, authorize=authorize_result_exchange)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "collaboration unavailable") from exc
    response.headers["Cache-Control"] = "private, no-store"
    return result


@router.get("/{turn_id}/reference-evidence")
async def get_reference_evidence(
    turn_id: str,
    request: Request,
    response: Response,
    include_content: bool = Query(default=False),
    submission_id: UUID4 | None = Query(default=None),
) -> dict[str, Any]:
    """Return the actor-scoped A-to-B evidence projection for one Turn."""

    projection = await _reference_projection(
        turn_id,
        request,
        include_content=include_content,
        submission_id=str(submission_id) if submission_id is not None else None,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["ETag"] = f'"{projection["projection_etag"]}"'
    return projection


@router.get("", response_model=TurnIdsDTO)
async def list_turns(
    request: Request,
    store: TurnLogStore = Depends(_log),
    limit: int = Query(default=100, ge=1, le=500),
) -> TurnIdsDTO:
    if _dev_user_fallback_enabled(request):
        return TurnIdsDTO(turn_ids=await store.list_turn_ids(limit=limit))
    user = await _current_user(request)
    user_store = _user_store(request)
    role = await user_store.get_team_role(user.id, user.team_id)
    workspace_ids = await user_store.list_active_workspace_ids(user.team_id)
    ids = await _session_store(request).list_recent_turn_ids(
        workspace_ids=workspace_ids,
        user_id=None if role in {"owner", "team_admin"} else user.id,
        limit=limit,
    )
    visible: list[str] = []
    for turn_id in ids:
        try:
            await _require_turn_access(
                turn_id,
                request,
                user,
                operation=SpaceOperation.READ_HISTORY,
            )
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                continue
            raise
        visible.append(turn_id)
    return TurnIdsDTO(turn_ids=visible)


@router.get("/{turn_id}/effect-disclosure")
async def get_effect_disclosure(
    turn_id: str,
    request: Request,
) -> dict[str, Any]:
    """Content-free current effect overlay for owner/admin hydrate and poll."""

    await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    protocol_store, scope = await _effect_scope_for_turn(turn_id, request)
    try:
        overlay = await protocol_store.read_current_effect_disclosure(scope)
    except Exception as exc:
        raise _effect_reader_http_error(exc) from exc
    _observe_effect_disclosure(overlay)
    return {
        "current_generation": scope.generation,
        "current_effect_disclosure": (overlay.payload if overlay is not None else None),
    }


def _observe_effect_disclosure(overlay: Any | None) -> None:
    """Emit bounded reader metrics without Turn, provider, or tenant labels."""

    if overlay is None:
        return
    try:
        from bowhead_core import metrics

        metrics.observe(
            "bowhead_terminal_effect_unknown_count",
            overlay.unknown_count,
            help="UNKNOWN effect count observed by the exact disclosure reader",
        )
        if overlay.oldest_unknown_age_upper_bound_seconds is not None:
            metrics.observe(
                "bowhead_terminal_effect_unknown_oldest_age_seconds",
                overlay.oldest_unknown_age_upper_bound_seconds,
                help="Upper-bound age of the oldest UNKNOWN effect",
            )
    except Exception:  # noqa: S110
        pass


@router.get("/{turn_id}/effect-attempts")
async def get_effect_attempt_details(
    turn_id: str,
    request: Request,
) -> dict[str, Any]:
    user = await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.READ_HISTORY,
        allow_archived_team_admin=True,
    )
    protocol_store, scope = await _effect_scope_for_turn(turn_id, request)
    await _require_effect_admin(
        request,
        user_id=user.id,
        team_id=scope.team_id,
    )
    details = await _read_admin_effect_attempts(
        protocol_store,
        scope,
        actor_user_id=user.id,
    )
    return {"attempts": [item.payload for item in details]}


async def _read_admin_effect_attempts(
    protocol_store: Any,
    scope: Any,
    *,
    actor_user_id: str,
) -> tuple[Any, ...]:
    try:
        return await protocol_store.read_effect_attempt_details(
            scope,
            actor_user_id=actor_user_id,
        )
    except Exception as exc:
        raise _effect_reader_http_error(exc) from exc


async def _require_effect_admin(
    request: Request,
    *,
    user_id: str,
    team_id: str,
) -> str:
    try:
        role = await _user_store(request).get_team_role(user_id, team_id)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"error": {"code": "terminal_dependency_recovering"}},
        ) from exc
    if role not in {"owner", "team_admin"}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    return role


@router.post("/{turn_id}/effect-settlements/manual")
async def settle_effect_manually(
    turn_id: str,
    body: ManualEffectSettlementRequest,
    request: Request,
) -> dict[str, Any]:
    from bowhead_core.terminal_effect_contract import (
        effect_control_identity_digest,
    )
    from bowhead_core.terminal_protocol import (
        EffectSettlementContext,
        EffectSettlementOutcome,
        EffectSettlementSource,
        TerminalProtocolMalformedError,
    )
    from bowhead_core.terminal_protocol_store import DecisionAccessRevokedError
    from bowhead_core.terminal_runtime_factory import (
        terminal_auth_resolve_context_values,
    )

    user = await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.READ_HISTORY,
        allow_archived_team_admin=True,
    )
    protocol_store, scope = await _effect_scope_for_turn(turn_id, request)
    role = await _require_effect_admin(
        request,
        user_id=user.id,
        team_id=scope.team_id,
    )
    resolve_context = terminal_auth_resolve_context_values(
        team_id=scope.team_id,
        session_id=scope.session_id,
        user_id=user.id,
        resource_ref=scope.turn_id,
    )
    try:
        vector = await protocol_store.read_auth_fence_vector(resolve_context)
    except Exception as exc:
        raise _effect_reader_http_error(exc) from exc
    details = await _read_admin_effect_attempts(
        protocol_store,
        scope,
        actor_user_id=user.id,
    )
    matches = [item for item in details if item.attempt.attempt_id == body.attempt_id]
    if len(matches) != 1:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "effect attempt not found")
    detail = matches[0]
    identity_fields = {
        "attempt_id": body.attempt_id,
        "provider_contract_key": detail.attempt.provider,
        "source": "manual",
        "outcome": body.outcome,
        "settlement_code": body.settlement_code,
        "evidence_digest": body.evidence_digest,
        "object_version_digest": body.object_version_digest,
        "actor_audit_ref": f"admin:{user.id}",
        "provider_external_ref_digest": None,
        "provider_external_ref_key_version": None,
    }
    audit_identity = effect_control_identity_digest(
        **identity_fields,
        audit_object_ref=None,
    )
    audit_object_ref = f"admin-attestation:{audit_identity}"
    identity = effect_control_identity_digest(
        **identity_fields,
        audit_object_ref=audit_object_ref,
    )
    settlement_id = f"set_{identity[:32]}"
    replay = bool(
        detail.attempt.state.value == "settled"
        and detail.settlement_id == settlement_id
        and detail.settlement_source is EffectSettlementSource.MANUAL
        and detail.settlement_code == body.settlement_code
        and detail.outcome is EffectSettlementOutcome(body.outcome)
        and detail.actor_audit_ref == f"admin:{user.id}"
        and detail.evidence_digest == body.evidence_digest
        and detail.object_version_digest == body.object_version_digest
        and isinstance(detail.authz_snapshot_digest, str)
    )
    context = EffectSettlementContext(
        scope=scope,
        attempt_id=body.attempt_id,
        expected_attempt_row_version=(detail.attempt.row_version - 1 if replay else body.expected_attempt_row_version),
        settlement_id=settlement_id,
        source=EffectSettlementSource.MANUAL,
        outcome=EffectSettlementOutcome(body.outcome),
        settlement_code=body.settlement_code,
        principal_ref=f"admin:{user.id}",
        principal_role="owner" if role == "owner" else "admin",
        authz_snapshot_digest=(detail.authz_snapshot_digest if replay else vector.auth_fence_vector_digest),
        evidence_link_id=f"evidence_{identity[:32]}",
        evidence_digest=body.evidence_digest,
        object_version_digest=body.object_version_digest,
        purge_after=None,
        audit_object_ref=audit_object_ref,
    )
    try:
        settled = await protocol_store.record_manual_effect_settlement(
            context,
            actor_user_id=user.id,
            resolve_context=resolve_context,
            expected_auth_fence_vector=vector,
        )
    except DecisionAccessRevokedError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found") from exc
    except TerminalProtocolMalformedError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": {"code": "effect_settlement_conflict"}},
        ) from exc
    return {
        "attempt_id": settled.attempt_id,
        "row_version": settled.row_version,
        "state": settled.state.value,
    }


@router.post("/{turn_id}/effect-attempts/{attempt_id}/probe")
async def probe_effect_attempt(
    turn_id: str,
    attempt_id: str,
    request: Request,
) -> dict[str, Any]:
    """Run a registered read-only provider probe; never re-execute the action."""

    from bowhead_core.terminal_effect_contract import (
        EffectProbeEvidence,
        effect_control_identity_digest,
        get_effect_provider_contract,
    )
    from bowhead_core.terminal_protocol import (
        EffectSettlementContext,
        EffectSettlementSource,
        TerminalProtocolMalformedError,
    )
    from bowhead_core.terminal_protocol_store import DecisionAccessRevokedError
    from bowhead_core.terminal_runtime_factory import (
        terminal_auth_resolve_context_values,
    )

    user = await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.READ_HISTORY,
        allow_archived_team_admin=True,
    )
    protocol_store, scope = await _effect_scope_for_turn(turn_id, request)
    await _require_effect_admin(
        request,
        user_id=user.id,
        team_id=scope.team_id,
    )
    resolve_context = terminal_auth_resolve_context_values(
        team_id=scope.team_id,
        session_id=scope.session_id,
        user_id=user.id,
        resource_ref=scope.turn_id,
    )
    try:
        vector = await protocol_store.read_auth_fence_vector(resolve_context)
    except Exception as exc:
        raise _effect_reader_http_error(exc) from exc
    details = await _read_admin_effect_attempts(
        protocol_store,
        scope,
        actor_user_id=user.id,
    )
    matches = [item for item in details if item.attempt.attempt_id == attempt_id]
    if len(matches) != 1:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "effect attempt not found")
    detail = matches[0]
    if detail.attempt.state.value not in {"unknown", "settled"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": {"code": "effect_probe_not_required"}},
        )
    if detail.attempt.state.value == "settled":
        return {
            "attempt_id": detail.attempt.attempt_id,
            "row_version": detail.attempt.row_version,
            "state": detail.attempt.state.value,
        }
    try:
        provider_contract = get_effect_provider_contract(detail.attempt.provider)
    except Exception as exc:
        raise _effect_reader_http_error(exc) from exc
    if not provider_contract.allows_control_probe:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": {"code": "effect_probe_not_supported"}},
        )
    adapters = getattr(request.app.state, "terminal_effect_probe_adapters", {})
    adapter = adapters.get(provider_contract.probe_adapter_key) if isinstance(adapters, dict) else None
    if not callable(adapter):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": {"code": "effect_probe_unavailable"}},
        )
    try:
        evidence = await adapter(detail.attempt)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"error": {"code": "effect_probe_unavailable"}},
        ) from exc
    if not isinstance(evidence, EffectProbeEvidence):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"error": {"code": "effect_probe_invalid"}},
        )
    identity = effect_control_identity_digest(
        attempt_id=detail.attempt.attempt_id,
        provider_contract_key=provider_contract.key,
        source="provider",
        outcome=evidence.outcome.value,
        settlement_code=evidence.settlement_code,
        evidence_digest=evidence.evidence_digest,
        object_version_digest=evidence.object_version_digest,
        actor_audit_ref=f"adapter:{provider_contract.probe_adapter_key}",
        audit_object_ref=evidence.audit_object_ref,
        provider_external_ref_digest=evidence.provider_external_ref_digest,
        provider_external_ref_key_version=(evidence.provider_external_ref_key_version),
    )
    context = EffectSettlementContext(
        scope=scope,
        attempt_id=detail.attempt.attempt_id,
        expected_attempt_row_version=(detail.attempt.row_version - 1 if detail.attempt.state.value == "settled" else detail.attempt.row_version),
        settlement_id=f"set_{identity[:32]}",
        source=EffectSettlementSource.PROVIDER,
        outcome=evidence.outcome,
        settlement_code=evidence.settlement_code,
        principal_ref=f"adapter:{provider_contract.probe_adapter_key}",
        principal_role="provider_adapter",
        authz_snapshot_digest=f"probe:{provider_contract.probe_adapter_key}",
        evidence_link_id=f"evidence_{identity[:32]}",
        evidence_digest=evidence.evidence_digest,
        object_version_digest=evidence.object_version_digest,
        purge_after=None,
        audit_object_ref=evidence.audit_object_ref,
        provider_external_ref_digest=evidence.provider_external_ref_digest,
        provider_external_ref_key_version=(evidence.provider_external_ref_key_version),
    )
    try:
        settled = await protocol_store.record_admin_probed_effect_settlement(
            context,
            actor_user_id=user.id,
            resolve_context=resolve_context,
            expected_auth_fence_vector=vector,
        )
    except DecisionAccessRevokedError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found") from exc
    except TerminalProtocolMalformedError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": {"code": "effect_settlement_conflict"}},
        ) from exc
    return {
        "attempt_id": settled.attempt_id,
        "row_version": settled.row_version,
        "state": settled.state.value,
    }


@router.get("/{turn_id}/status")
async def get_turn_status(turn_id: str, request: Request) -> dict[str, Any]:
    """Lightweight liveness for the chat client (agent run limits, phase 3).

    The client asks this after a long quiet stretch with a dropped stream instead of
    declaring the turn failed on a timer: ``terminal`` means hydrate the final state,
    ``alive`` means the worker is still renewing its lease, and a running turn that is
    not alive has lost its owner until recovery settles it.
    """
    await _require_turn_access(turn_id, request, operation=SpaceOperation.READ_HISTORY)
    session_store = getattr(request.app.state, "session_store", None)
    if session_store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"error": {"code": "turn_status_unavailable"}})
    turn = await session_store.get_turn(turn_id)
    if turn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    running = str(getattr(turn, "status", "") or "") == "running"
    alive = running and await session_store.chat_turn_execution_alive(turn_id=turn_id)
    return {
        "turn_id": turn_id,
        "status": str(getattr(turn, "status", "") or ""),
        "terminal": not running,
        "alive": alive,
        "ended_at": getattr(turn, "ended_at", None),
    }


@router.get(
    "/{turn_id}/events",
    response_model=TurnEventsListDTO,
    response_model_exclude_none=True,
)
async def list_events(
    turn_id: str,
    request: Request,
    store: TurnLogStore | None = Depends(_optional_log),
) -> TurnEventsListDTO:
    await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    protocol_store = getattr(request.app.state, "terminal_protocol_store", None)
    session_store = getattr(request.app.state, "session_store", None)
    if protocol_store is not None and session_store is not None:
        turn = await session_store.get_turn(turn_id)
        session = await session_store.get_session(turn.session_id) if turn is not None else None
        if turn is None or session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
        try:
            team_id, persisted_session_id, protocol_version = await protocol_store.read_persisted_turn_scope(
                turn_id=turn_id,
            )
            persisted_protocol = resolve_persisted_chat_execution_protocol(
                protocol_version,
            )
        except Exception as exc:
            raise _effect_reader_http_error(exc) from exc
        if persisted_session_id != session.id:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                {"error": {"code": "terminal_protocol_malformed"}},
            )
        if persisted_protocol is PersistedChatExecutionProtocol.TERMINAL_V1:
            from bowhead_core.terminal_protocol import (
                ConsumerProfile,
                TerminalClassification,
                classify_terminal,
            )
            from bowhead_core.terminal_protocol_store import TerminalReadContext
            from bowhead_core.terminal_runtime_factory import (
                terminal_auth_resolve_context_values,
            )

            from bowhead_gateway.routes.chat import _terminal_v1_wire_payload

            # Storage consistency is bound to the immutable Session owner
            # vector; the caller (owner or current Team admin) was already
            # authorized by _require_turn_access above.
            resolve_context = terminal_auth_resolve_context_values(
                team_id=team_id,
                session_id=session.id,
                user_id=session.user_id,
                resource_ref=turn_id,
            )
            try:
                vector = await protocol_store.read_auth_fence_vector(resolve_context)
                read_context = TerminalReadContext(
                    team_id=team_id,
                    session_id=session.id,
                    turn_id=turn_id,
                    viewer=session.user_id,
                    auth_fence_vector_digest=vector.auth_fence_vector_digest,
                )
                checkpoint = await protocol_store.read_checkpoint(read_context)
                rows = await protocol_store.list_protocol_events(
                    read_context,
                    up_to_event_seq=checkpoint.persistent_event_highwater,
                )
                from bowhead_core.terminal_protocol import TerminalScope

                effect_overlay = await protocol_store.read_current_effect_disclosure(
                    TerminalScope(
                        team_id=team_id,
                        session_id=session.id,
                        turn_id=turn_id,
                        generation=checkpoint.current_generation,
                    )
                )
                _observe_effect_disclosure(effect_overlay)
            except Exception as exc:
                raise _effect_reader_http_error(exc) from exc
            try:
                safe_payloads = await project_event_payloads_for_viewer(
                    request,
                    turn_id,
                    [(row.event_type, row.payload) for row in rows],
                    event_ordinals=[row.event_seq for row in rows],
                )
            except ViewerSourceProjectionError as exc:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    {"error": {"code": "source_projection_unavailable"}},
                ) from exc
            current_receipt_id = checkpoint.current_receipt_summary.receipt_id if checkpoint.current_receipt_summary is not None else None
            events: list[TurnEventDTO] = []
            for row, safe_payload in zip(rows, safe_payloads, strict=True):
                classification = classify_terminal(
                    1,
                    ConsumerProfile.TURN_HISTORY,
                    row.as_terminal_event(),
                )
                if classification is TerminalClassification.MALFORMED:
                    raise HTTPException(
                        status.HTTP_500_INTERNAL_SERVER_ERROR,
                        {"error": {"code": "terminal_protocol_malformed"}},
                    )
                terminal_current = row.generation == checkpoint.current_generation and (
                    (classification is TerminalClassification.ROOT_COMMITTED and row.terminal_receipt_id == current_receipt_id)
                    or classification
                    in {
                        TerminalClassification.ROOT_NON_DURABLE,
                        TerminalClassification.ROOT_UNKNOWN,
                    }
                )
                events.append(
                    TurnEventDTO(
                        seq=row.event_seq,
                        event_type=row.event_type,
                        payload=_terminal_v1_wire_payload(
                            payload=safe_payload,
                            event_id=row.event_id,
                            event_seq=row.event_seq,
                            generation=row.generation,
                            terminal_receipt_id=row.terminal_receipt_id,
                            terminal_current=terminal_current,
                        ),
                        occurred_at=row.occurred_at,
                        event_id=row.event_id,
                        execution_generation=row.generation,
                        terminal_receipt_id=row.terminal_receipt_id,
                        terminal_current=terminal_current,
                    ),
                )
            return TurnEventsListDTO(
                turn_id=turn_id,
                events=events,
                terminal_protocol_version=1,
                current_generation=checkpoint.current_generation,
                persistent_event_highwater=checkpoint.persistent_event_highwater,
                current_effect_disclosure=(effect_overlay.payload if effect_overlay is not None else None),
            )
    if store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "turn log store not configured",
        )
    rows = await store.list_events(turn_id)
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found in event log")
    try:
        safe_payloads = await project_event_payloads_for_viewer(
            request,
            turn_id,
            [(row.event_type, row.payload) for row in rows],
        )
    except ViewerSourceProjectionError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"error": {"code": "source_projection_unavailable"}},
        ) from exc
    return TurnEventsListDTO(
        turn_id=turn_id,
        events=[
            TurnEventDTO(
                seq=r.seq,
                event_type=r.event_type,
                payload=safe_payload,
                occurred_at=r.occurred_at,
            )
            for r, safe_payload in zip(rows, safe_payloads, strict=True)
        ],
    )


@router.post("/{turn_id}/cancel", status_code=202)
async def cancel_turn(
    turn_id: str,
    body: CancelTurnRequest,
    request: Request,
) -> dict[str, Any]:
    """M2.1: 请求 cancel 跑动中的 turn.

    - turn 在跑 → orch.signal_cancel(turn_id) → TurnRunner 下次 boundary 检查抛
      TurnCancelledError → 进 _finish_cancelled → emit turn_cancelled + done(status=cancelled).
    - turn 已结束 / 不存在 → 返 noop, 不报错 (幂等).

    见 docs/M157-cancel-turn-design.md §4
    """
    session_store = _session_store(request)
    turn = await session_store.get_turn(turn_id)
    dev_fallback = _dev_user_fallback_enabled(request)
    if turn is None and not dev_fallback:
        user = await _current_user(request)
        _require_explicit_stop_request_id(
            actual=body.cancel_request_id,
            team_id=user.team_id,
            user_id=user.id,
            turn_id=turn_id,
            execution_generation=body.expected_execution_generation,
        )
        try:
            receipt = await session_store.get_cancel_turn_receipt(
                team_id=user.team_id,
                user_id=user.id,
                cancel_request_id=str(body.cancel_request_id),
                turn_id=turn_id,
                expected_execution_generation=body.expected_execution_generation,
            )
        except ValueError as exc:
            if str(exc) == "cancel_request_conflict":
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    {"error": {"code": "cancel_request_conflict"}},
                ) from exc
            raise
        if receipt is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
        # Erasure removes the Turn→Session join, but it must not freeze the old
        # Space authorization forever.  Re-evaluate current membership and
        # lifecycle against the workspace retained in the content-free receipt.
        await build_access_context_for_user_from_request(
            request,
            user_id=user.id,
            workspace_id=receipt.workspace_id,
            operation=SpaceOperation.EXECUTE,
        )
        # Re-enter the receipt transaction after current authorization.  Its
        # existing-receipt branch repairs a legacy RUNNING Plan by the retained
        # execution Turn identity even when erasure removed the Turn row.
        repaired = await session_store.request_cancel_turn(
            team_id=user.team_id,
            user_id=user.id,
            cancel_request_id=str(body.cancel_request_id),
            workspace_id=receipt.workspace_id,
            turn_id=turn_id,
            expected_execution_generation=body.expected_execution_generation,
            include_plan_outcome=True,
        )
        repaired_outcome, plan_cancelled = repaired
        if repaired_outcome != receipt.outcome:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"error": {"code": "cancel_receipt_changed"}},
            )
        if receipt.outcome == "generation_conflict":
            return {
                "status": "noop",
                "ack": "rejected",
                "target_state": "erased" if receipt.turn_erased else "present",
                "source": body.source,
                "turn_id": turn_id,
                "cancel_request_id": str(body.cancel_request_id),
                "execution_generation": body.expected_execution_generation,
                "durable": True,
                "reason": "execution_generation_changed",
            }
        if receipt.outcome == "cancelled":
            return {
                "status": "cancelled",
                "ack": "accepted",
                "target_state": "erased" if receipt.turn_erased else "present",
                "source": body.source,
                "turn_id": turn_id,
                "cancel_request_id": str(body.cancel_request_id),
                "execution_generation": body.expected_execution_generation,
                "durable": True,
                "plan_cancelled": plan_cancelled,
            }
        return {
            "status": "noop",
            "ack": "rejected",
            "target_state": "erased" if receipt.turn_erased else "present",
            "source": body.source,
            "turn_id": turn_id,
            "cancel_request_id": str(body.cancel_request_id),
            "execution_generation": body.expected_execution_generation,
            "durable": True,
            "reason": "turn_not_running",
        }
    # Establish opaque Turn visibility before reading the protocol
    # discriminator.  Otherwise a caller who only knows a Turn id could use
    # future/malformed responses to distinguish another user's persisted row.
    user = await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.EXECUTE,
        allow_owner_without_space_membership=True,
    )
    # Production callers must derive the receipt identity from the complete
    # immutable Stop tuple.  Besides being UUIDv5-shaped, this prevents one
    # tab/user/turn generation from adopting another tab's durable receipt.
    # Dev fallback stays permissive for historical route fixtures that do not
    # model authenticated team identity.
    if not dev_fallback:
        _require_explicit_stop_request_id(
            actual=body.cancel_request_id,
            team_id=user.team_id,
            user_id=user.id,
            turn_id=turn_id,
            execution_generation=body.expected_execution_generation,
        )
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "orchestrator not configured")
    session = await session_store.get_session(turn.session_id) if turn is not None else None
    if turn is None or session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    protocol_store = getattr(
        request.app.state,
        "terminal_protocol_store",
        None,
    )
    terminal_team_id: str | None = None
    terminal_session_id: str | None = None
    terminal_version: int | None = None
    terminal_protocol = PersistedChatExecutionProtocol.LEGACY_V0
    terminal_session = None
    terminal_auth_digest: str | None = None
    terminal_summary = None
    terminal_factory = None
    session_store = getattr(request.app.state, "session_store", None)
    if protocol_store is not None:
        if session_store is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "terminal_protocol_writer_not_ready",
            )
        visible_turn = await session_store.get_turn(turn_id)
        visible_session = await session_store.get_session(visible_turn.session_id) if visible_turn is not None else None
        if visible_turn is None or visible_session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
        if user.id != visible_session.user_id:
            # Safe-stop is owner-bound for every persisted protocol version.
            # An administrator cannot inspect the discriminator first and
            # then impersonate the immutable Session owner auth vector.
            raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
        try:
            (
                terminal_team_id,
                terminal_session_id,
                terminal_version,
            ) = await protocol_store.read_persisted_turn_scope(turn_id=turn_id)
            terminal_protocol = resolve_persisted_chat_execution_protocol(
                terminal_version,
            )
        except Exception as scope_error:
            from bowhead_core.terminal_protocol_store import (
                DecisionSourceNotReadyError,
                TerminalProtocolFutureVersionError,
            )

            if isinstance(
                scope_error,
                TerminalProtocolFutureVersionError,
            ):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "terminal_protocol_future_version",
                ) from scope_error
            if not (isinstance(scope_error, DecisionSourceNotReadyError) and str(scope_error) == "turn protocol scope is missing"):
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "terminal_protocol_writer_not_ready",
                ) from scope_error
            # A genuinely unknown Turn preserves the historical idempotent
            # projection.  No persisted/future/malformed row may enter this
            # branch.
            terminal_team_id = None
            terminal_session_id = None
            terminal_version = None
            terminal_protocol = PersistedChatExecutionProtocol.LEGACY_V0
        if terminal_protocol is PersistedChatExecutionProtocol.TERMINAL_V1:
            if session_store is None or not terminal_session_id:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "terminal_protocol_writer_not_ready",
                )
            terminal_session = await session_store.get_session(terminal_session_id)
            terminal_factory = getattr(
                request.app.state,
                "terminal_runtime_factory",
                None,
            )
            if terminal_session is None or terminal_factory is None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "terminal_protocol_writer_not_ready",
                )
            try:
                # Capture before the complete route ACL below.  The store
                # re-reads this vector under BEGIN IMMEDIATE immediately
                # before generation-bound signalling.
                terminal_auth_digest = await terminal_factory.prepare_web_auth_fence(
                    team_id=terminal_team_id,
                    session_id=terminal_session_id,
                    user_id=terminal_session.user_id,
                )
            except Exception as exc:
                from bowhead_gateway.terminal_protocol_http import (
                    terminal_protocol_http_error,
                )

                raise terminal_protocol_http_error(exc) from exc

    if terminal_protocol is PersistedChatExecutionProtocol.TERMINAL_V1 and terminal_session is not None and user.id != terminal_session.user_id:
        # Active generation control currently has an owner-bound auth vector;
        # Team-admin cancellation would require a second principal in that
        # immutable vector rather than impersonating the Session owner.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    if terminal_protocol is PersistedChatExecutionProtocol.TERMINAL_V1 and terminal_session is not None:
        from bowhead_gateway.terminal_decision_authorization import (
            authorize_web_cancel_access,
        )
        from bowhead_gateway.terminal_protocol_http import (
            terminal_protocol_http_error,
        )

        try:
            await authorize_web_cancel_access(
                app=request.app,
                team_id=terminal_team_id,
                session_id=terminal_session_id,
                requester_ref=terminal_session.user_id,
                workspace_id=terminal_session.workspace_id,
            )
        except Exception as exc:
            raise terminal_protocol_http_error(exc) from exc
        try:
            existing_cancel_receipt = await session_store.get_cancel_turn_receipt(
                team_id=terminal_team_id,
                user_id=terminal_session.user_id,
                cancel_request_id=str(body.cancel_request_id),
                turn_id=turn_id,
                expected_execution_generation=body.expected_execution_generation,
            )
        except ValueError as exc:
            if str(exc) == "cancel_request_conflict":
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    {"error": {"code": "cancel_request_conflict"}},
                ) from exc
            raise
        if existing_cancel_receipt is not None:
            if existing_cancel_receipt.workspace_id != terminal_session.workspace_id:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    {"error": {"code": "cancel_request_conflict"}},
                )
            if existing_cancel_receipt.outcome == "generation_conflict":
                return {
                    "status": "noop",
                    "ack": "rejected",
                    "target_state": "erased" if existing_cancel_receipt.turn_erased else "present",
                    "source": body.source,
                    "turn_id": turn_id,
                    "cancel_request_id": str(body.cancel_request_id),
                    "execution_generation": body.expected_execution_generation,
                    "durable": True,
                    "reason": "execution_generation_changed",
                    "plan_cancelled": False,
                }
            if existing_cancel_receipt.outcome == "cancelled":
                plan_cancelled = await _cancel_plan_for_turn(
                    request,
                    turn_id=turn_id,
                )
                return {
                    "status": "cancelled",
                    "ack": "accepted",
                    "target_state": "erased" if existing_cancel_receipt.turn_erased else "present",
                    "source": body.source,
                    "turn_id": turn_id,
                    "cancel_request_id": str(body.cancel_request_id),
                    "execution_generation": body.expected_execution_generation,
                    "durable": True,
                    "plan_cancelled": plan_cancelled,
                }
            return {
                "status": "noop",
                "ack": "rejected",
                "target_state": "erased" if existing_cancel_receipt.turn_erased else "present",
                "source": body.source,
                "turn_id": turn_id,
                "cancel_request_id": str(body.cancel_request_id),
                "execution_generation": body.expected_execution_generation,
                "durable": True,
                "reason": "turn_not_running",
                "plan_cancelled": False,
            }
        try:
            from bowhead_core.terminal_protocol_store import (
                TerminalProtocolMalformedError,
                TerminalReadContext,
            )

            terminal_summary = await protocol_store.read_protocol_summary(
                TerminalReadContext(
                    team_id=terminal_team_id,
                    session_id=terminal_session_id,
                    turn_id=turn_id,
                    viewer=terminal_session.user_id,
                    auth_fence_vector_digest=terminal_auth_digest,
                )
            )
        except LookupError as exc:
            raise terminal_protocol_http_error(TerminalProtocolMalformedError("persisted terminal scope has no protocol summary")) from exc
        except Exception as exc:
            raise terminal_protocol_http_error(exc) from exc
        if terminal_summary.current_generation != body.expected_execution_generation:
            successor_receipt = terminal_summary.current_receipt
            paused_cancel_committed = (
                terminal_summary.current_generation == body.expected_execution_generation + 1
                and terminal_summary.current_generation_kind == "control"
                and successor_receipt is not None
                and is_explicit_user_cancel_receipt(
                    status=successor_receipt.status,
                    reason_code=successor_receipt.reason_code,
                    generation_kind=(terminal_summary.current_generation_kind),
                    closing_reason=(terminal_summary.current_generation_closing_reason),
                )
            )
            if paused_cancel_committed:
                plan_cancelled = await _cancel_plan_for_turn(
                    request,
                    turn_id=turn_id,
                )
                await _record_terminal_cancel_receipt(
                    session_store,
                    team_id=terminal_team_id,
                    user_id=terminal_session.user_id,
                    cancel_request_id=str(body.cancel_request_id),
                    workspace_id=terminal_session.workspace_id,
                    turn_id=turn_id,
                    expected_execution_generation=body.expected_execution_generation,
                    outcome="cancelled",
                )
                return {
                    "status": "cancelled",
                    "ack": "accepted",
                    "target_state": "present",
                    "source": body.source,
                    "turn_id": turn_id,
                    "cancel_request_id": str(body.cancel_request_id),
                    "execution_generation": body.expected_execution_generation,
                    "durable": True,
                    "plan_cancelled": plan_cancelled,
                }
            await _record_terminal_cancel_receipt(
                session_store,
                team_id=terminal_team_id,
                user_id=terminal_session.user_id,
                cancel_request_id=str(body.cancel_request_id),
                workspace_id=terminal_session.workspace_id,
                turn_id=turn_id,
                expected_execution_generation=body.expected_execution_generation,
                outcome="generation_conflict",
            )
            return {
                "status": "noop",
                "ack": "rejected",
                "target_state": "present",
                "source": body.source,
                "turn_id": turn_id,
                "cancel_request_id": str(body.cancel_request_id),
                "execution_generation": body.expected_execution_generation,
                "durable": True,
                "reason": "execution_generation_changed",
                "plan_cancelled": False,
            }
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "orchestrator not configured")
    signaled = False
    terminal_active = False
    terminal_already_closed = False
    authorized_scope = None
    generation_signaler = None
    if terminal_protocol is PersistedChatExecutionProtocol.TERMINAL_V1:
        assert session_store is not None
        identity_reader = getattr(
            session_store,
            "get_active_turn_identity",
            None,
        )
        if not callable(identity_reader):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "terminal_protocol_writer_not_ready",
            )
        active_turn_id, active_protocol_version = await identity_reader(terminal_session_id)
        active_protocol = resolve_persisted_chat_execution_protocol(
            active_protocol_version,
        )
        terminal_active = active_turn_id == turn_id and active_protocol is PersistedChatExecutionProtocol.TERMINAL_V1
        if terminal_active:
            assert terminal_factory is not None
            assert terminal_team_id is not None
            assert terminal_session_id is not None
            assert terminal_auth_digest is not None
            try:
                authorized_scope = await terminal_factory.authorize_active_web_control(
                    team_id=terminal_team_id,
                    session_id=terminal_session_id,
                    user_id=terminal_session.user_id,
                    expected_turn_id=turn_id,
                    auth_fence_vector_digest=terminal_auth_digest,
                )
                generation_signaler = getattr(
                    orch,
                    "signal_cancel_generation",
                    None,
                )
                if not callable(generation_signaler):
                    raise RuntimeError("terminal_generation_cancel_unavailable")
            except Exception as exc:
                from bowhead_gateway.terminal_protocol_http import (
                    terminal_protocol_http_error,
                )

                raise terminal_protocol_http_error(exc) from exc
        else:
            from bowhead_core.terminal_protocol import (
                GenerationState,
                TerminalScope,
                TerminalStatus,
            )

            if terminal_summary is None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "terminal_protocol_writer_not_ready",
                )
            receipt = terminal_summary.current_receipt
            terminal_already_closed = terminal_summary.current_generation_state is GenerationState.CLOSED and (
                terminal_summary.current_generation_resolution == "fenced_deleted"
                or (
                    receipt is not None
                    and receipt.status
                    in {
                        TerminalStatus.COMPLETED,
                        TerminalStatus.FAILED,
                        TerminalStatus.CANCELLED,
                        TerminalStatus.ABORTED,
                    }
                )
            )
            resumable_pending = (
                terminal_summary.current_generation_state is GenerationState.CLOSED
                and receipt is not None
                and receipt.status
                in {
                    TerminalStatus.CLARIFICATION_PENDING,
                    TerminalStatus.APPROVAL_PENDING,
                }
            )
            if not terminal_already_closed and not resumable_pending:
                generation = terminal_summary.current_generation
                if isinstance(generation, int) and generation > 0:
                    try:
                        await terminal_factory.trigger_recovery(
                            TerminalScope(
                                team_id=terminal_team_id,
                                session_id=terminal_session_id,
                                turn_id=turn_id,
                                generation=generation,
                            ),
                            reason="cancel_owner_missing",
                            prior_error=RuntimeError("terminal_cancel_owner_missing"),
                        )
                    except Exception:
                        logger.warning(
                            "terminal.cancel_recovery_trigger_failed turn=%s",
                            turn_id,
                            exc_info=True,
                        )
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "terminal_dependency_recovering",
                )
    terminal_control_committed = False
    terminal_cancel_receipt_outcome: (
        Literal[
            "cancelled",
            "terminal",
            "generation_conflict",
        ]
        | None
    ) = None
    if protocol_store is not None and terminal_protocol is PersistedChatExecutionProtocol.TERMINAL_V1 and not terminal_already_closed:
        session_store = _session_store(request)
        turn = await session_store.get_turn(turn_id)
        session = await session_store.get_session(turn.session_id) if turn is not None else None
        if turn is not None and session is not None:
            try:
                workspace = await _user_store(request).get_workspace(session.workspace_id)
                target_team_id = workspace.get("team_id") if isinstance(workspace, dict) else None
                if not isinstance(target_team_id, str) or not target_team_id:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        "decision_source_malformed",
                    )
                bound_session_id, version = await protocol_store.read_persisted_turn_protocol_binding(
                    team_id=target_team_id,
                    turn_id=turn_id,
                )
                if resolve_persisted_chat_execution_protocol(version) is PersistedChatExecutionProtocol.TERMINAL_V1:
                    if bound_session_id != session.id:
                        raise HTTPException(status.HTTP_409_CONFLICT, "decision_source_malformed")
                    terminal_factory = getattr(request.app.state, "terminal_runtime_factory", None)
                    if terminal_factory is None:
                        raise HTTPException(
                            status.HTTP_503_SERVICE_UNAVAILABLE,
                            "terminal_protocol_writer_not_ready",
                        )
                    if terminal_active:
                        assert authorized_scope is not None
                        from bowhead_core.terminal_protocol import TerminalStatus

                        receipt = await terminal_factory.commit_active_web_cancel(
                            authorized_scope,
                        )
                        if receipt.status is TerminalStatus.CANCELLED:
                            terminal_control_committed = True
                        else:
                            # Another terminal outcome won the generation race.
                            # It is already durable, so Stop is an idempotent noop.
                            terminal_already_closed = True
                    else:
                        from bowhead_core.terminal_protocol import (
                            DecisionAction,
                            DecisionPrincipalSet,
                            DecisionSourceKind,
                        )

                        prepared = await terminal_factory.record_source_decision(
                            team_id=target_team_id,
                            session_id=session.id,
                            turn_id=turn_id,
                            source_kind=DecisionSourceKind.SYSTEM_CONTROL,
                            source_id="__auto_control__",
                            action=DecisionAction.CANCEL,
                            principals=DecisionPrincipalSet(
                                session.user_id,
                                "system:web-cancel",
                                session.user_id,
                            ),
                        )
                        await terminal_factory.commit_decision_control(
                            prepared,
                            reason_code="user_cancelled",
                            message_key="turn.cancelled",
                        )
                        terminal_control_committed = True
            except HTTPException:
                raise
            except Exception as exc:
                from bowhead_gateway.terminal_protocol_http import terminal_protocol_http_error

                raise terminal_protocol_http_error(exc) from exc
    # A committed terminal cancellation must always reach the exact process
    # generation (or recovery), even if the secondary HTTP ACK ledger is
    # temporarily unavailable.  The ledger is replay metadata, not the worker
    # stop fence.
    if terminal_protocol is PersistedChatExecutionProtocol.TERMINAL_V1 and terminal_active and terminal_control_committed:
        assert authorized_scope is not None
        assert callable(generation_signaler)
        try:
            signaled = generation_signaler(
                authorized_scope.turn_id,
                authorized_scope.generation,
            )
        except Exception:
            signaled = False
            logger.warning(
                "terminal.cancel_generation_signal_failed turn=%s generation=%s",
                authorized_scope.turn_id,
                authorized_scope.generation,
                exc_info=True,
            )
        if not signaled:
            try:
                await terminal_factory.trigger_recovery(
                    authorized_scope,
                    reason="cancel_owner_missing",
                    prior_error=RuntimeError("terminal_cancel_owner_missing"),
                )
            except Exception:
                logger.warning(
                    "terminal.cancel_recovery_trigger_failed turn=%s",
                    turn_id,
                    exc_info=True,
                )
    if terminal_protocol is PersistedChatExecutionProtocol.TERMINAL_V1:
        assert session_store is not None
        assert terminal_team_id is not None
        assert terminal_session is not None
        prior_receipt = getattr(terminal_summary, "current_receipt", None)
        prior_is_explicit_stop = bool(
            prior_receipt is not None
            and is_explicit_user_cancel_receipt(
                status=prior_receipt.status,
                reason_code=prior_receipt.reason_code,
                generation_kind=terminal_summary.current_generation_kind,
                closing_reason=(terminal_summary.current_generation_closing_reason),
            )
        )
        desired_outcome: Literal["cancelled", "terminal"] = "cancelled" if terminal_control_committed or prior_is_explicit_stop else "terminal"
        # This durable turn_id CAS deliberately precedes the HTTP receipt.
        # Therefore an accepted ACK implies the Plan is no longer RUNNING;
        # replaying an older ACK also repairs a crash between these writes.
        plan_cancelled = await _cancel_plan_for_turn(request, turn_id=turn_id) if desired_outcome == "cancelled" else False
        terminal_cancel_receipt_outcome = await _record_terminal_cancel_receipt(
            session_store,
            team_id=terminal_team_id,
            user_id=terminal_session.user_id,
            cancel_request_id=str(body.cancel_request_id),
            workspace_id=terminal_session.workspace_id,
            turn_id=turn_id,
            expected_execution_generation=body.expected_execution_generation,
            outcome=desired_outcome,
        )
    # Feishu 07.06 #7: turns executing plans must durably cancel the plan too (set sticky CANCELLED).
    # signal_cancel only stops the current turn; run_plan ignores process-local signals, so plans continue in background/revive on F5.
    # With CANCELLED recorded, run_plan stops at the next level boundary (execution-cancel button removed; Stop is the only entry).
    if terminal_protocol is PersistedChatExecutionProtocol.TERMINAL_V1:
        accepted = terminal_cancel_receipt_outcome == "cancelled"
        return {
            "status": "cancelled" if accepted else "noop",
            "ack": "accepted" if accepted else "rejected",
            "target_state": "present",
            "source": body.source,
            "turn_id": turn_id,
            "cancel_request_id": str(body.cancel_request_id),
            "execution_generation": body.expected_execution_generation,
            "durable": terminal_cancel_receipt_outcome is not None,
            "reason": None if accepted else "turn_not_running",
            "plan_cancelled": plan_cancelled,
        }
    try:
        cancel_result = await session_store.request_cancel_turn(
            team_id=user.team_id,
            user_id=user.id,
            cancel_request_id=str(body.cancel_request_id),
            workspace_id=session.workspace_id,
            turn_id=turn_id,
            expected_execution_generation=body.expected_execution_generation,
            include_plan_outcome=True,
        )
        durable_outcome, plan_cancelled = cancel_result
    except ValueError as exc:
        if str(exc) == "cancel_request_conflict":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"error": {"code": "cancel_request_conflict"}},
            ) from exc
        raise
    except Exception as exc:
        logger.exception(
            "cancel_turn.durable_stamp_failed turn=%s",
            turn_id,
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "turn cancellation could not be persisted",
        ) from exc
    if durable_outcome == "generation_conflict":
        return {
            "status": "noop",
            "ack": "rejected",
            "target_state": "present",
            "source": body.source,
            "turn_id": turn_id,
            "cancel_request_id": str(body.cancel_request_id),
            "execution_generation": body.expected_execution_generation,
            "durable": True,
            "reason": "execution_generation_changed",
            "plan_cancelled": plan_cancelled,
        }
    signaled = orch.signal_cancel(turn_id) if durable_outcome == "cancelled" else False
    # Plan cancellation was committed in the same SessionStore transaction as
    # the Turn stamp above.  Signal only after both durable states are visible.
    durable = durable_outcome in {"cancelled", "terminal"} or plan_cancelled
    if durable_outcome == "cancelled" or signaled:
        if not durable:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "turn cancellation was signaled but not persisted",
            )
        return {
            "status": "cancelled",
            "ack": "accepted",
            "target_state": "present",
            "source": body.source,
            "turn_id": turn_id,
            "cancel_request_id": str(body.cancel_request_id),
            "execution_generation": body.expected_execution_generation,
            "durable": durable,
            "plan_cancelled": plan_cancelled,
        }
    return {
        "status": "noop",
        "ack": "rejected",
        "target_state": "present",
        "source": body.source,
        "turn_id": turn_id,
        "cancel_request_id": str(body.cancel_request_id),
        "execution_generation": body.expected_execution_generation,
        "durable": durable,
        "reason": "turn_not_running",
        "plan_cancelled": plan_cancelled,
    }


@router.get("/{turn_id}/cancel/{cancel_request_id}")
async def get_cancel_turn_outcome(
    turn_id: str,
    cancel_request_id: UUID5,
    request: Request,
    expected_execution_generation: int = Query(ge=1),
) -> dict[str, Any]:
    """Reconcile a durable Stop receipt after reload or an unknown POST reply."""

    session_store = _session_store(request)
    turn = await session_store.get_turn(turn_id)
    dev_fallback = _dev_user_fallback_enabled(request)
    if turn is None and not dev_fallback:
        user = await _current_user(request)
    else:
        user = await _require_turn_access(
            turn_id,
            request,
            operation=SpaceOperation.EXECUTE,
            allow_owner_without_space_membership=True,
        )
    if not dev_fallback:
        _require_explicit_stop_request_id(
            actual=cancel_request_id,
            team_id=user.team_id,
            user_id=user.id,
            turn_id=turn_id,
            execution_generation=expected_execution_generation,
        )
    try:
        receipt = await session_store.get_cancel_turn_receipt(
            team_id=user.team_id,
            user_id=user.id,
            cancel_request_id=str(cancel_request_id),
            turn_id=turn_id,
            expected_execution_generation=expected_execution_generation,
        )
    except ValueError as exc:
        if str(exc) == "cancel_request_conflict":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"error": {"code": "cancel_request_conflict"}},
            ) from exc
        raise
    if receipt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cancel receipt not found")
    if turn is None and not dev_fallback:
        await build_access_context_for_user_from_request(
            request,
            user_id=user.id,
            workspace_id=receipt.workspace_id,
            operation=SpaceOperation.EXECUTE,
        )
    if receipt.outcome == "generation_conflict":
        return {
            "status": "noop",
            "ack": "rejected",
            "target_state": "erased" if receipt.turn_erased else "present",
            "source": "explicit_stop",
            "turn_id": turn_id,
            "cancel_request_id": str(cancel_request_id),
            "execution_generation": expected_execution_generation,
            "durable": True,
            "reason": "execution_generation_changed",
        }
    if receipt.outcome == "cancelled":
        await _cancel_plan_for_turn(
            request,
            turn_id=turn_id,
        )
        return {
            "status": "cancelled",
            "ack": "accepted",
            "target_state": "erased" if receipt.turn_erased else "present",
            "source": "explicit_stop",
            "turn_id": turn_id,
            "cancel_request_id": str(cancel_request_id),
            "execution_generation": expected_execution_generation,
            "durable": True,
        }
    return {
        "status": "noop",
        "ack": "rejected",
        "target_state": "erased" if receipt.turn_erased else "present",
        "source": "explicit_stop",
        "turn_id": turn_id,
        "cancel_request_id": str(cancel_request_id),
        "execution_generation": expected_execution_generation,
        "durable": True,
        "reason": "turn_not_running",
    }


# Q6 fix: backend enforces reason_codes allowlist, preventing FE/custom extensions from contaminating data.
# Keep synchronized with frontend turn-stream.tsx SAT_REASON_CODES.
KNOWN_SATISFACTION_REASONS = frozenset(
    {
        "inaccurate",  # Inaccurate numbers / calculation errors.
        "missed_dim",  # Missing dimensions / insufficient breakdown.
        "wrong_intent",  # Misinterpreted intent.
        "too_brief",  # Too brief / insufficient detail.
        "too_verbose",  # Too verbose / unclear priorities.
        "framework_off",  # Wrong analysis framework.
        "other",  # Fallback (allowed though FE does not list it).
    }
)


class SatisfactionRequest(BaseModel):
    """M158-13 paradigm v2 phase 3 · 用户对 turn done 的满意度.

    score=-1 (👎) 时, 用户**多选** reason_codes 帮我们结构化归类问题.
    code 必须 ∈ KNOWN_SATISFACTION_REASONS, 未知 code 自动归到 'other' (Q6 fix).
    """

    score: int = Field(..., ge=-1, le=1, description="1=👍, -1=👎, 0=neutral")
    reason_codes: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="-1 时用户多选问题原因 (e.g. ['inaccurate','too_brief'])",
    )
    comment: str | None = Field(default=None, max_length=500)

    def normalized_reason_codes(self) -> list[str]:
        """Q6: 未知 code 替换为 'other', 防 metric cardinality 爆 + 防 data quality 污染."""
        out: list[str] = []
        seen: set[str] = set()
        for code in self.reason_codes:
            normalized = code if code in KNOWN_SATISFACTION_REASONS else "other"
            if normalized not in seen:
                seen.add(normalized)
                out.append(normalized)
        return out


@router.post("/{turn_id}/satisfaction", status_code=200)
async def record_satisfaction(
    turn_id: str,
    payload: SatisfactionRequest,
    request: Request,
) -> dict[str, Any]:
    """记录用户对 turn 的满意度. 写 user_memory.satisfaction + emit SSE.

    幂等: 同一 (user_id, turn_id) 二次提交覆盖之前 score (用户改主意).
    """
    user_memory = getattr(request.app.state, "user_memory_store", None)
    if user_memory is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "user memory store not configured",
        )
    user = await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.WRITE,
    )
    user_id = user.id

    # Q6 fix: normalize with allowlist. Unknown code → other; deduplicate.
    safe_codes = payload.normalized_reason_codes()

    try:
        await user_memory.upsert_operational_memory(
            user_id=user_id,
            kind="satisfaction",
            key=turn_id,
            value={
                "score": payload.score,
                "reason_codes": safe_codes,
                "comment": payload.comment,
            },
            source="turn_satisfaction",
        )
    except Exception as e:
        logger.warning("satisfaction.upsert_failed turn=%s err=%s", turn_id, e)
        raise HTTPException(500, "failed to write satisfaction") from e

    # Metric: increment each normalized reason (fixed cardinality ≤7, avoiding Prometheus explosion).
    if safe_codes:
        try:
            from bowhead_core import metrics as _m

            for code in safe_codes:
                _m.inc(
                    "bowhead_turn_satisfaction_reason_total",
                    labels={"reason": code},
                    help="phase 3 满意度 reason 多选 (top 问题归类, 白名单 enforce)",
                )
        except Exception:  # noqa: S110
            pass

    # Emit SSE event (best effort, does not block response).
    # dingsiyan #11 related gap: post-terminal receipt emission must be out-of-band (seq seeding + closed-bus fallback).
    try:
        from bowhead_gateway.turn_service import turn_seq_seed

        _seed = await turn_seq_seed(getattr(request.app.state, "turn_log_store", None), turn_id)
        await get_event_registry().emit_out_of_band(
            turn_id,
            OrchestratorEvent(
                event_type="turn.satisfaction_recorded",
                turn_id=turn_id,
                payload={"score": payload.score, "user_id": user_id, "has_comment": bool(payload.comment)},
            ),
            fallback_initial_seq=_seed,
        )
    except Exception as e:
        logger.debug("satisfaction.event_emit_failed turn=%s err=%s", turn_id, e)

    # metric counter
    try:
        from bowhead_core import metrics as _m

        _m.inc(
            "bowhead_turn_satisfaction_total",
            labels={"score": str(payload.score)},
            help="M158 phase 3: turn done satisfaction (1=up, -1=down, 0=neutral)",
        )
    except Exception:  # noqa: S110
        pass

    return {"ok": True, "turn_id": turn_id, "score": payload.score}


# ─────────────────────────────────────────────────────────────
# chat-v4 CHANGE-W7.1 / W7.5 — retry endpoints
#
# W7.1: POST /api/v1/turns/{turn_id}/retry_dispatch
#   Body: {spawn_id} — enqueue a Lead-only retry turn using cached peer return.
#
# W7.5: POST /api/v1/turns/{turn_id}/retry_subtask
#   Body: {subtask_id} — retry an individual subtask within a turn
# (.v4-ob-actions ↻ Retry banner: partial_failure single-row retry).
#
# Both endpoints share a 60s in-memory dedup map keyed on
# ``(turn_id, spawn_id|subtask_id)`` → new_turn_id (SRE P0 fix:
# double-click idempotency without re-enqueueing).
# ─────────────────────────────────────────────────────────────

_RETRY_DEDUP_TTL_SEC = 60.0
# (turn_id, key_id) → (new_turn_id, expires_at_monotonic)
_retry_dispatch_dedup: dict[tuple[str, str], tuple[str, float]] = {}
_retry_subtask_dedup: dict[tuple[str, str], tuple[str, float]] = {}
# chat-redesign p0-p1-followup B1 — step-level dedup maps.
# Keyed on (turn_id, step_id) → new_step_id (placeholder until orchestrator
# re-dispatch wires). Double-click idempotency same as W7.1/W7.5.
_retry_step_dedup: dict[tuple[str, str], tuple[str, float]] = {}
_skip_step_dedup: dict[tuple[str, str], tuple[str, float]] = {}


def _retry_dedup_get(
    cache: dict[tuple[str, str], tuple[str, float]],
    key: tuple[str, str],
) -> str | None:
    now = asyncio.get_event_loop().time()
    entry = cache.get(key)
    if entry is None:
        return None
    new_id, exp = entry
    if exp < now:
        cache.pop(key, None)
        return None
    return new_id


def _retry_dedup_set(
    cache: dict[tuple[str, str], tuple[str, float]],
    key: tuple[str, str],
    new_id: str,
) -> None:
    now = asyncio.get_event_loop().time()
    cache[key] = (new_id, now + _RETRY_DEDUP_TTL_SEC)
    # Best-effort sweep of expired entries to bound size.
    expired = [k for k, (_, exp) in cache.items() if exp < now]
    for k in expired:
        cache.pop(k, None)


class RetryDispatchRequest(BaseModel):
    spawn_id: str = Field(..., min_length=1, max_length=80)


@router.post("/{turn_id}/retry_dispatch")
async def retry_dispatch(
    turn_id: str,
    payload: RetryDispatchRequest,
    request: Request,
    store: TurnLogStore = Depends(_log),
) -> dict[str, Any]:
    """chat-v4 W7.1 — enqueue a Lead-only retry turn for a specific peer.

    Logic:
      1. Verify turn exists + has events.
      2. Find a cached subagent_return event for ``spawn_id`` within the turn.
      3. (W4-fix CTO P0-3) Real enqueue NOT YET WIRED — returns 503.

    chat-v4 W4-fix CTO P0-3 — bf returned 202 + synthetic
    ``t_retry_{turn_id}_{spawn_id}`` without any asyncio.create_task / turn
    store insert / orchestrator dispatch. FE would GET
    ``/turns/{new_turn_id}/events`` → 404 forever.

    Option-(b) per CTO instructions: explicit 503 "not yet implemented" with
    clear error message + log marker. Followup task: wire
    ``orchestrator.run_turn`` with reconstructed TurnRequest using cached
    peer output (needs turn_started event payload extraction + user_store
    lookup for member_uid). Validation logic (turn lookup / spawn match /
    peer-returned gate / dedup map) PRESERVED so option-(a) can drop in
    cleanly later — only the synthetic-id return is replaced.

    LICENSING §3.6: when this (and retry_subtask) is wired to ``orchestrator.run_turn``, it MUST pre-flight through
    ``bowhead_gateway._entitlement_gate.gate_turn_http`` first (or it becomes a new ungated turn entry that bypasses
    the entitlement gate — the "forgotten Nth caller" regression §3 guards against).

    NOTE: in-memory dedup map (_retry_dispatch_dedup) is single-worker only.
    Multi-worker setups will not dedup cross-worker double-clicks; a redis
    backed dedup would be needed for that. Logged at retry attempt for
    SRE visibility.
    """
    spawn_id = payload.spawn_id.strip()
    if not spawn_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "spawn_id required")
    await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.EXECUTE,
    )

    # Idempotency dedup — check first BEFORE store lookup.
    # P1-C: in-memory dedup is single-worker only. Cross-worker double-click
    # races will not dedup until we move this to redis.
    dedup_key = (turn_id, spawn_id)
    existing = _retry_dedup_get(_retry_dispatch_dedup, dedup_key)
    if existing is not None:
        # Even a deduped repeat must surface the unimplemented state honestly;
        # the cached id is intentionally NOT a real queued turn id.
        logger.info(
            "retry_dispatch.placeholder_dedup_hit turn=%s spawn=%s existing=%s",
            turn_id,
            spawn_id,
            existing,
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "retry_dispatch_not_implemented",
                "message": (
                    "retry_dispatch endpoint is not yet wired to orchestrator. "
                    "Followup task: real enqueue with cached peer output. "
                    "Validation logic (turn/spawn lookup) succeeded; only "
                    "the dispatch step is stubbed."
                ),
                "turn_id": turn_id,
                "spawn_id": spawn_id,
            },
        )

    rows = await store.list_events(turn_id)
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")

    # Look for any event mentioning the spawn_id (peer_dispatched / agent_spawned /
    # lead.subagent_summary). A `subagent_return` payload would have status=
    # completed on agent_spawned event with this subagent_id; we accept any
    # event that references the spawn so retry is permissive.
    spawn_found = False
    peer_returned = False
    for r in rows:
        p = r.payload if isinstance(r.payload, dict) else {}
        ev_subid = p.get("spawn_id") or p.get("subagent_id")
        if ev_subid == spawn_id:
            spawn_found = True
            # agent_spawned status=completed → peer returned at least once
            if r.event_type == "agent_spawned" and p.get("status") in (
                "completed",
                "succeeded",
                "succeeded_with_warnings",
            ):
                peer_returned = True
            # lead.subagent_summary status=succeeded* signals return as well
            if r.event_type == "lead.subagent_summary" and (str(p.get("status") or "").startswith("succeeded")):
                peer_returned = True
    if not spawn_found:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"spawn_id {spawn_id!r} not found in turn {turn_id!r}",
        )
    if not peer_returned:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"spawn {spawn_id!r} did not return successfully — cannot retry dispatch without a cached peer payload",
        )

    # P0-3 option (b): Stash dedup key so a subsequent identical click still
    # gets a deterministic 503 (no flapping between 503 and "not found").
    placeholder_id = f"t_retry_pending_{turn_id}_{spawn_id}"
    _retry_dedup_set(_retry_dispatch_dedup, dedup_key, placeholder_id)
    logger.warning(
        "retry_dispatch.placeholder_503 turn=%s spawn=%s — endpoint stubbed; followup: wire orchestrator.run_turn with cached peer payload",
        turn_id,
        spawn_id,
    )
    raise HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "retry_dispatch_not_implemented",
            "message": (
                "retry_dispatch endpoint is not yet wired to orchestrator. "
                "Followup task: real enqueue with cached peer output. "
                "Validation logic (turn/spawn lookup) succeeded; only "
                "the dispatch step is stubbed."
            ),
            "turn_id": turn_id,
            "spawn_id": spawn_id,
        },
    )


class RetrySubtaskRequest(BaseModel):
    subtask_id: str = Field(..., min_length=1, max_length=80)


@router.post("/{turn_id}/retry_subtask")
async def retry_subtask(
    turn_id: str,
    payload: RetrySubtaskRequest,
    request: Request,
    store: TurnLogStore = Depends(_log),
) -> dict[str, Any]:
    """chat-v4 W7.5 — retry an individual subtask within a turn.

    Surfaced by the .v4-ob-actions ↻ 重试 banner on partial_failure rows.
    Similar dedup semantics to ``retry_dispatch``.

    chat-v4 W4-fix CTO P0-3 — same option-(b) treatment as retry_dispatch:
    return 503 instead of synthetic id. Validation (turn + subtask lookup)
    preserved for FE error-handling parity.
    """
    subtask_id = payload.subtask_id.strip()
    if not subtask_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "subtask_id required")
    await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.EXECUTE,
    )

    dedup_key = (turn_id, subtask_id)
    existing = _retry_dedup_get(_retry_subtask_dedup, dedup_key)
    if existing is not None:
        logger.info(
            "retry_subtask.placeholder_dedup_hit turn=%s subtask=%s existing=%s",
            turn_id,
            subtask_id,
            existing,
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "retry_subtask_not_implemented",
                "message": ("retry_subtask endpoint is not yet wired to orchestrator. Followup task: real enqueue with subtask scope."),
                "turn_id": turn_id,
                "subtask_id": subtask_id,
            },
        )

    rows = await store.list_events(turn_id)
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")

    # Subtasks can appear as subtask_id in phase.update / tool_result payloads.
    found = any(
        isinstance(r.payload, dict)
        and (r.payload.get("subtask_id") == subtask_id or r.payload.get("phase_id") == subtask_id or r.payload.get("id") == subtask_id)
        for r in rows
    )
    if not found:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"subtask_id {subtask_id!r} not found in turn {turn_id!r}",
        )

    placeholder_id = f"t_retry_sub_pending_{turn_id}_{subtask_id}"
    _retry_dedup_set(_retry_subtask_dedup, dedup_key, placeholder_id)
    logger.warning(
        "retry_subtask.placeholder_503 turn=%s subtask=%s — endpoint stubbed; followup: wire orchestrator.run_turn with subtask scope",
        turn_id,
        subtask_id,
    )
    raise HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "retry_subtask_not_implemented",
            "message": ("retry_subtask endpoint is not yet wired to orchestrator. Followup task: real enqueue with subtask scope."),
            "turn_id": turn_id,
            "subtask_id": subtask_id,
        },
    )


# ─────────────────────────────────────────────────────────────
# chat-redesign p0-p1-followup B1 — Step-level retry / skip
#
# Audit doc: docs/chat-redesign-prd-gap-audit-2026-05-23.md §2 P0-1
# PRD ref: §4.4 Failure ack + US-5 + R5 SLO (users can continue operating in ≥80% of failed turns).
#
# FE (FailureAckCard + StepFailureButtons) renders Retry / Skip buttons on
# failed steps. Pre-B1 those buttons no-op'd because no endpoint existed.
#
# These endpoints (option-(b) stub like W7.1/W7.5):
#   1. Validate step_id exists in turn events (looks for `step_started` /
#      `step_done` payloads carrying matching `step_id`).
#   2. Validate turn is not already done (no retry of closed turn).
#   3. Emit a `turn.step_retry_requested` / `turn.step_skip_requested` event
#      so FE renders "重试中..." or "已跳过" inline ack.
#   4. Return 202 + placeholder `new_step_id` (real orchestrator re-dispatch
#      is followup, same as W7.1/W7.5 wiring).
#
# 60s dedup on (turn_id, step_id) to absorb double-clicks idempotently.
# ─────────────────────────────────────────────────────────────


def _turn_done(rows: list[Any]) -> bool:
    """True if turn has a terminal `done` / `turn_failed` / `turn_cancelled` event.

    Used to reject retry/skip on a closed turn (semantically meaningless;
    a closed turn cannot resume).
    """
    return any(r.event_type in ("done", "turn_failed", "turn_cancelled") for r in rows)


def _find_step(rows: list[Any], step_id: str) -> dict[str, Any] | None:
    """Return the most recent payload referencing ``step_id`` (step_done
    preferred, falls back to step_started). None if not found.
    """
    last_started: dict[str, Any] | None = None
    last_done: dict[str, Any] | None = None
    for r in rows:
        p = r.payload if isinstance(r.payload, dict) else {}
        if p.get("step_id") != step_id:
            continue
        if r.event_type == "step_started":
            last_started = p
        elif r.event_type == "step_done":
            last_done = p
    return last_done or last_started


class StepRetryRequest(BaseModel):
    """Optional body for retry_step. Reason is free-text for future UX feedback."""

    reason: str | None = Field(default=None, max_length=200)


class StepSkipRequest(BaseModel):
    """Optional body for skip_step. Reason is free-text for future UX feedback."""

    reason: str | None = Field(default=None, max_length=200)


@router.post("/{turn_id}/retry_step/{step_id}", status_code=202)
async def retry_step(
    turn_id: str,
    step_id: str,
    request: Request,
    payload: StepRetryRequest | None = None,
    store: TurnLogStore = Depends(_log),
) -> dict[str, Any]:
    """chat-redesign p0-p1-followup B1 — request a step-level retry.

    ⚠ STUB(真话版, chat-solidify 2026-07-03): 只验证+发 ack 事件+返回占位
    new_step_id, **从不真重跑**。FE 的 retry 按钮已因此全部摘除(O5), 当前无调用方;
    端点保留为 API 兼容。接真 re-dispatch 时必须过 gate_turn_http(entitlement)
    否则成为免费 turn 入口 — 见下方 followup 注释与 chat-pipeline.md。
    原计划: reconstruct TurnRequest with single-step scope + cached
    attachments / tool_call args from event log。

    Idempotency: 60s in-memory dedup on (turn_id, step_id). Double-clicks
    return the same placeholder + same event seq (event is NOT re-emitted on
    dedup hit — FE already saw the first ack).
    """
    step_id_clean = step_id.strip()
    if not step_id_clean:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "step_id required")
    await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.EXECUTE,
    )

    dedup_key = (turn_id, step_id_clean)
    existing = _retry_dedup_get(_retry_step_dedup, dedup_key)
    if existing is not None:
        logger.info(
            "retry_step.dedup_hit turn=%s step=%s existing=%s",
            turn_id,
            step_id_clean,
            existing,
        )
        return {
            "success": True,
            "new_step_id": existing,
            "eta_seconds": 5,
            "deduped": True,
        }

    rows = await store.list_events(turn_id)
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")

    if _turn_done(rows):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"turn {turn_id!r} already done — cannot retry step on closed turn",
        )

    step_payload = _find_step(rows, step_id_clean)
    if step_payload is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"step_id {step_id_clean!r} not found in turn {turn_id!r}",
        )

    # Placeholder new_step_id — option-(b) stub. Real dispatch swaps for
    # orchestrator-allocated UUID. Format mirrors W7.5 placeholder shape.
    new_step_id = f"step_retry_pending_{turn_id}_{step_id_clean}"
    _retry_dedup_set(_retry_step_dedup, dedup_key, new_step_id)

    # Emit ack event so FE step row can render "重试中..." inline.
    # Best-effort: ack event failure must not block the API response.
    try:
        from bowhead_gateway.turn_service import turn_seq_seed

        _seed = await turn_seq_seed(store, turn_id)
        await get_event_registry().emit_out_of_band(
            turn_id,
            OrchestratorEvent(
                event_type="turn.step_retry_requested",
                turn_id=turn_id,
                payload={
                    "step_id": step_id_clean,
                    "new_step_id": new_step_id,
                    "eta_seconds": 5,
                    "reason": (payload.reason if payload else None),
                },
            ),
            fallback_initial_seq=_seed,
        )
    except Exception as e:
        logger.debug("retry_step.event_emit_failed turn=%s step=%s err=%s", turn_id, step_id_clean, e)

    logger.info(
        "retry_step.accepted turn=%s step=%s new_step=%s",
        turn_id,
        step_id_clean,
        new_step_id,
    )
    return {
        "success": True,
        "new_step_id": new_step_id,
        "eta_seconds": 5,
        "deduped": False,
    }


@router.post("/{turn_id}/skip_step/{step_id}", status_code=202)
async def skip_step(
    turn_id: str,
    step_id: str,
    request: Request,
    payload: StepSkipRequest | None = None,
    store: TurnLogStore = Depends(_log),
) -> dict[str, Any]:
    """chat-redesign p0-p1-followup B1 — mark a step as skipped.

    Mirrors retry_step pattern: validates lookup, emits ack event so FE step
    row renders "已跳过" inline, returns 202 + skip_marked=True. The turn-
    continue semantics ride on existing partial_success logic (Lead loop will
    treat subsequent iters as ok if peer turn returned partial; user-skip
    here just adds a UX-level ack to surface that policy).
    """
    step_id_clean = step_id.strip()
    if not step_id_clean:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "step_id required")
    await _require_turn_access(
        turn_id,
        request,
        operation=SpaceOperation.EXECUTE,
    )

    dedup_key = (turn_id, step_id_clean)
    existing = _retry_dedup_get(_skip_step_dedup, dedup_key)
    if existing is not None:
        logger.info(
            "skip_step.dedup_hit turn=%s step=%s existing=%s",
            turn_id,
            step_id_clean,
            existing,
        )
        return {
            "success": True,
            "skip_marked": True,
            "deduped": True,
        }

    rows = await store.list_events(turn_id)
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")

    if _turn_done(rows):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"turn {turn_id!r} already done — cannot skip step on closed turn",
        )

    step_payload = _find_step(rows, step_id_clean)
    if step_payload is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"step_id {step_id_clean!r} not found in turn {turn_id!r}",
        )

    skip_marker = f"step_skipped_{turn_id}_{step_id_clean}"
    _retry_dedup_set(_skip_step_dedup, dedup_key, skip_marker)

    try:
        from bowhead_gateway.turn_service import turn_seq_seed

        _seed = await turn_seq_seed(store, turn_id)
        await get_event_registry().emit_out_of_band(
            turn_id,
            OrchestratorEvent(
                event_type="turn.step_skip_requested",
                turn_id=turn_id,
                payload={
                    "step_id": step_id_clean,
                    "skip_marked": True,
                    "reason": (payload.reason if payload else None),
                },
            ),
            fallback_initial_seq=_seed,
        )
    except Exception as e:
        logger.debug("skip_step.event_emit_failed turn=%s step=%s err=%s", turn_id, step_id_clean, e)

    logger.info(
        "skip_step.accepted turn=%s step=%s",
        turn_id,
        step_id_clean,
    )
    return {
        "success": True,
        "skip_marked": True,
        "deduped": False,
    }


@router.get("/{turn_id}/replay")
async def replay(
    turn_id: str,
    request: Request,
    store: TurnLogStore | None = Depends(_optional_log),
    delay_ms: int = 0,
) -> EventSourceResponse:
    """Replay 持久化事件到 SSE，与活 turn 的 /chat/stream 格式一致."""
    history = await list_events(turn_id, request, store)
    if not history.events:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    # Keep the browser-facing replay route visibly inside the viewer-safe
    # projection boundary.  list_events() already projects its DTO, but this
    # final egress pass is intentional: future refactors cannot replace that
    # helper with a raw store read without the static egress guard noticing.
    # Stable persisted ordinals make the pass idempotent for source aliases.
    try:
        replay_payloads = await project_event_payloads_for_viewer(
            request,
            turn_id,
            [(event.event_type, event.payload) for event in history.events],
            event_ordinals=[event.seq for event in history.events],
        )
    except ViewerSourceProjectionError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"error": {"code": "source_projection_unavailable"}},
        ) from exc
    history = history.model_copy(
        update={
            "events": [
                event.model_copy(update={"payload": safe_payload})
                for event, safe_payload in zip(
                    history.events,
                    replay_payloads,
                    strict=True,
                )
            ],
        },
    )

    history_protocol = resolve_persisted_chat_execution_protocol(
        history.terminal_protocol_version,
    )

    async def gen():
        if history_protocol is PersistedChatExecutionProtocol.TERMINAL_V1:
            yield {
                "event": "stream.checkpoint",
                "data": json.dumps(
                    {
                        "terminal_protocol_version": 1,
                        "turn_id": turn_id,
                        "current_generation": history.current_generation,
                        "persistent_event_highwater": history.persistent_event_highwater,
                    },
                    ensure_ascii=False,
                ),
            }
        for event in history.events:
            frame = {
                "event": event.event_type,
                "data": json.dumps(event.payload, ensure_ascii=False),
            }
            if history_protocol is PersistedChatExecutionProtocol.TERMINAL_V1:
                frame["id"] = str(event.seq)
            yield frame
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)

    return EventSourceResponse(gen())
