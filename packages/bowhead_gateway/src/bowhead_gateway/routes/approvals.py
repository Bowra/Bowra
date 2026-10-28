"""Approval HTTP API — admin 看板 + 决策.

GET    /api/v1/approvals                → 列 pending（admin 审批看板）
GET    /api/v1/approvals?scope=all      → 最近全部 50 条
GET    /api/v1/approvals/{id}           → 详情
POST   /api/v1/approvals/{id}/approve   → 标 approved + spawn exec task
POST   /api/v1/approvals/{id}/reject    → 标 rejected
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.access_context import (
    build_access_context_for_user_from_request,
)
from bowhead_gateway.dtos import ApprovalDTO, ApprovalListDTO
from bowhead_gateway.routes.auth import _authenticated_identity
from bowhead_gateway.terminal_protocol_rollout import (
    PersistedChatExecutionProtocol,
    resolve_persisted_chat_execution_protocol,
)

if TYPE_CHECKING:
    from bowhead_core.approval_store import Approval, ApprovalStore
    from bowhead_core.notification_service import NotificationService
    from bowhead_core.user_store import User, UserStore

    from bowhead_core import Orchestrator

logger = logging.getLogger(__name__)

router = APIRouter()
_REQUIRE_APPROVAL_ADMIN = _authenticated_identity


async def _approval_target_team(
    approval: Approval,
    *,
    request: Request,
) -> str:
    store = _user_store(request)
    team_id = await store.get_workspace_team_id(approval.workspace_id)
    if not team_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "approval not found",
        )
    return team_id


def _store(request: Request) -> ApprovalStore:
    s: ApprovalStore | None = getattr(request.app.state, "approval_store", None)
    if s is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "approval store not configured")
    return s


def _user_store(request: Request) -> UserStore:
    s: UserStore | None = getattr(request.app.state, "user_store", None)
    if s is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "user_store not configured")
    return s


def _notification_service(request: Request) -> NotificationService | None:
    return getattr(request.app.state, "notification_service", None)


async def _terminal_approval_factory(
    request: Request,
    approval: Approval,
    *,
    team_id: str,
) -> Any | None:
    """Select v1 only from the persisted Turn discriminator."""

    state = request.app.state
    if not hasattr(state, "terminal_protocol_store"):
        # Lifespan-less route harnesses model a pre-0008 legacy deployment.
        # A real startup always installs this attribute, so production cannot
        # silently use this compatibility branch.
        return None
    protocol_store = getattr(request.app.state, "terminal_protocol_store", None)
    if protocol_store is None:
        if getattr(state, "legacy_session_lifecycle_allowed", None) is True:
            return None
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "terminal_protocol_writer_not_ready",
        )
    try:
        session_id, version = await protocol_store.read_persisted_turn_protocol_binding(
            team_id=team_id,
            turn_id=approval.turn_id,
        )
        protocol = resolve_persisted_chat_execution_protocol(version)
    except Exception as exc:
        from bowhead_gateway.terminal_protocol_http import (
            terminal_protocol_http_error,
        )

        raise terminal_protocol_http_error(exc) from exc
    if approval.session_id is not None and approval.session_id != session_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "terminal_protocol_malformed",
        )
    if protocol is PersistedChatExecutionProtocol.TERMINAL_V1 and approval.session_id is None:
        approval.session_id = session_id
    if protocol is PersistedChatExecutionProtocol.LEGACY_V0:
        return None
    factory = getattr(request.app.state, "terminal_runtime_factory", None)
    if factory is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "terminal_protocol_writer_not_ready",
        )
    return factory


async def _emit_approval_rejected_event(request: Request, approval: Approval) -> None:
    """R-2：拒绝后补发 chat 终态事件 approval.rejected（additive，不动通知路）.

    审批决策通常发生在 turn 已终态、bus 已关闭之后，所以**必须**走
    ``emit_out_of_band``（裸 get_or_create+emit：<5s 命中 closed bus 静默吞、
    >5s 新 bus seq 从 0 撞号被 F5 去重误杀——chat-pipeline.md）。seq seed
    取 ``turn_log_store.max_seq(turn_id)``，registry 内部再叠内存高水位续接。
    reject route 不建 executor，故在此自取 event_registry / turn_log_store。
    """
    orch: Orchestrator | None = getattr(request.app.state, "orchestrator", None)
    event_registry = getattr(orch, "event_registry", None) if orch is not None else None
    if event_registry is None:
        return

    fallback_initial_seq = 0
    turn_log_store = getattr(request.app.state, "turn_log_store", None)
    if turn_log_store is not None:
        try:
            fallback_initial_seq = int(await turn_log_store.max_seq(approval.turn_id))
        except Exception as exc:
            logger.warning(
                "approval.reject_turn_seq_seed_failed id=%s turn=%s err=%s",
                approval.id,
                approval.turn_id,
                exc,
            )

    try:
        from bowhead_core.events import OrchestratorEvent

        await event_registry.emit_out_of_band(
            approval.turn_id,
            OrchestratorEvent(
                event_type="approval.rejected",
                turn_id=approval.turn_id,
                payload={
                    "card_id": approval.turn_id,
                    "approval_id": approval.id,
                },
            ),
            fallback_initial_seq=fallback_initial_seq,
        )
    except Exception as exc:
        logger.warning(
            "approval.reject_event_emit_failed id=%s turn=%s err=%s",
            approval.id,
            approval.turn_id,
            exc,
        )


async def _approval_in_admin_team(
    approval: Approval,
    *,
    admin: User,
    request: Request,
) -> bool:
    store = _user_store(request)
    target_team = await _approval_target_team(
        approval,
        request=request,
    )
    role = await store.get_team_role(admin.id, target_team)
    return role in {"owner", "team_admin"}


async def _require_approval_lifecycle(
    approval: Approval,
    *,
    admin: User,
    request: Request,
    operation: SpaceOperation,
) -> None:
    from bowhead_gateway.routes.auth import _dev_user_fallback_enabled

    if _dev_user_fallback_enabled(request):
        return
    target_team = await _approval_target_team(
        approval,
        request=request,
    )
    await build_access_context_for_user_from_request(
        request,
        user_id=admin.id,
        workspace_id=approval.workspace_id,
        operation=operation,
        expected_team_id=target_team,
    )


async def _filter_admin_team(
    approvals: list[Approval],
    *,
    admin: User,
    request: Request,
) -> list[Approval]:
    out: list[Approval] = []
    for approval in approvals:
        if not await _approval_in_admin_team(
            approval,
            admin=admin,
            request=request,
        ):
            continue
        try:
            await _require_approval_lifecycle(
                approval,
                admin=admin,
                request=request,
                operation=SpaceOperation.READ_HISTORY,
            )
        except HTTPException as exc:
            if exc.status_code not in {
                status.HTTP_403_FORBIDDEN,
                status.HTTP_404_NOT_FOUND,
            }:
                raise
            continue
        out.append(approval)
    return out


async def _get_scoped_approval(
    approval_id: str,
    *,
    admin: User,
    request: Request,
    store: ApprovalStore,
    operation: SpaceOperation = SpaceOperation.READ_HISTORY,
) -> Approval:
    approval = await store.get(approval_id)
    if approval is None or not await _approval_in_admin_team(approval, admin=admin, request=request):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    await _require_approval_lifecycle(
        approval,
        admin=admin,
        request=request,
        operation=operation,
    )
    return approval


def _to_dto(
    a: Approval,
    *,
    decision_id: str | None = None,
    decision_state: str | None = None,
) -> ApprovalDTO:
    return ApprovalDTO(
        id=a.id,
        turnId=a.turn_id,
        sessionId=a.session_id,
        workspaceId=a.workspace_id,
        memberUid=a.member_uid,
        userId=a.user_id,
        toolName=a.tool_name,
        arguments=a.arguments or {},
        reason=a.reason,
        riskLevel=a.risk_level,
        status=a.status,
        decidedBy=a.decided_by,
        decidedAt=a.decided_at,
        executionOk=a.execution_ok,
        executionResult=a.execution_result,
        executionError=a.execution_error,
        executedAt=a.executed_at,
        createdAt=a.created_at,
        updatedAt=a.updated_at,
        decisionId=decision_id,
        decisionState=decision_state,
    )


@router.get("", response_model=ApprovalListDTO)
async def list_approvals(
    request: Request,
    scope: str = "pending",
    store: ApprovalStore = Depends(_store),
    admin: User = Depends(_REQUIRE_APPROVAL_ADMIN),
) -> ApprovalListDTO:
    if scope == "pending":
        items = await store.list_pending()
    else:
        items = await store.list_recent()
    items = await _filter_admin_team(items, admin=admin, request=request)
    return ApprovalListDTO(items=[_to_dto(a) for a in items], scope=scope)


@router.get("/{approval_id}", response_model=ApprovalDTO)
async def get_approval(
    approval_id: str,
    request: Request,
    store: ApprovalStore = Depends(_store),
    admin: User = Depends(_REQUIRE_APPROVAL_ADMIN),
) -> ApprovalDTO:
    a = await _get_scoped_approval(approval_id, admin=admin, request=request, store=store)
    return _to_dto(a)


class DecideBody(BaseModel):
    reason: str | None = None


@router.post(
    "/{approval_id}/approve",
    response_model=ApprovalDTO,
    dependencies=[Depends(require_not_readonly)],
)
async def approve(
    approval_id: str,
    body: DecideBody,
    request: Request,
    store: ApprovalStore = Depends(_store),
    admin: User = Depends(_REQUIRE_APPROVAL_ADMIN),
) -> ApprovalDTO:
    # Licensing: gate approve→execute WRITE before mutating state (§3.5/§7.5).
    # A frozen team must not trigger tool execution. A nonexistent approval falls
    # through to set_decision's 404.
    current = await _get_scoped_approval(
        approval_id,
        admin=admin,
        request=request,
        store=store,
        operation=SpaceOperation.EXECUTE,
    )
    return await _approve_decision(approval_id, request=request, store=store, actor=admin, current=current)


async def _approve_decision(
    approval_id: str,
    *,
    request: Request,
    store: ApprovalStore,
    actor: User,
    current: Approval,
) -> ApprovalDTO:
    """Record an approval by ``actor`` and execute; callers have already scoped ``current``."""
    decided_by = actor.id
    target_team_id = await _approval_target_team(current, request=request)
    terminal_factory = await _terminal_approval_factory(
        request,
        current,
        team_id=target_team_id,
    )
    if terminal_factory is not None and current.status in {
        "executed",
        "execution_failed",
    }:
        from bowhead_core.terminal_protocol import (
            DecisionAction,
            DecisionPrincipalSet,
            DecisionSourceKind,
        )

        if current.session_id is None or current.decided_by is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "decision_source_malformed",
            )
        replay = await terminal_factory.read_existing_source_decision(
            team_id=target_team_id,
            session_id=current.session_id,
            turn_id=current.turn_id,
            source_kind=DecisionSourceKind.APPROVAL,
            source_id=approval_id,
            action=DecisionAction.APPROVE,
            principals=DecisionPrincipalSet(
                current.user_id,
                current.decided_by,
                current.decided_by,
            ),
        )
        if replay is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "decision_source_malformed",
            )
        return _to_dto(
            current,
            decision_id=replay.decision.context.decision_id,
            decision_state=replay.decision.state.value,
        )
    if (terminal_factory is None and current.status != "pending") or (terminal_factory is not None and current.status not in {"pending", "approved"}):
        return _to_dto(current)
    from bowhead_gateway._entitlement_gate import gate_turn_http

    await gate_turn_http(request, target_team_id)

    terminal_approval_runner = getattr(
        request.app.state,
        "terminal_approval_runner",
        None,
    )
    if terminal_factory is not None and terminal_approval_runner is None:
        # First release certifies no native write tools.  Never consume a v1
        # approval unless dispatch is protected by the durable effect contract.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "terminal_protocol_writer_not_ready",
        )

    notification_service = _notification_service(request)
    exec_: Any | None = None
    orch: Orchestrator | None = getattr(request.app.state, "orchestrator", None)
    if orch is not None and terminal_factory is None:
        from bowhead_gateway.datarc_context import resolve_datarc_turn_context

        try:
            datarc_ctx = await resolve_datarc_turn_context(
                request,
                team_id=target_team_id,
                workspace_id=current.workspace_id,
                # Batch 2/c9 (review P1): add user_id, the only omission among 5 callers (also required for PBI
                # per-user UPN). Without it, per-user mode always lacks BI.
                user_id=actor.id,
                # Pin short-circuit (architecture review, 2026-07-13): artifact snapshot skips the per-user ladder,
                # avoiding an accessible call + headless preference writes.
                pinned_project=(getattr(current, "datarc_workspace_id", None)),
                log=logger,
            )
        except Exception as exc:
            logger.warning(
                "approval.approve_prepare_executor_failed id=%s err=%s",
                approval_id,
                exc,
            )
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "approval execution context unavailable",
            ) from exc
        # Datarc comes only from the team connection registry; never fall back to a
        # deployment-level client.
        datarc = datarc_ctx.datarc_client

        # Batch 2/c9 (PRD §3.9 replay): per-user workspace **pins the artifact snapshot**
        # (appr.datarc_workspace_id), not approver preferences. B approving A's report must never silently
        # switch its data scope. Resolve member_uid anew for the current caller (approver) in the snapshot workspace;
        # denied access -> 403 BI-absence error (fail-closed, never reuse the requester's saved identity).
        _pinned_ws = getattr(current, "datarc_workspace_id", None)
        _approver_member: str | None = None

        _active_project = datarc_ctx.active_datarc_project
        if _pinned_ws:
            _active_project = _pinned_ws
            from bowhead_gateway.routes.chat import _resolve_datarc_member_uid
            from bowhead_gateway.ws_authz import ws_membership_enforce_enabled

            _approver_member = await _resolve_datarc_member_uid(
                request,
                resolver=getattr(request.app.state, "member_resolver", None),
                datarc_client=datarc_ctx.datarc_client,
                datarc_ws=_pinned_ws,
                body_member_uid=None,
                ws_enforce=ws_membership_enforce_enabled(),
                log=logger,
            )

        from bowhead_core.approval_executor import ApprovalExecutor

        _team_router = None
        if terminal_factory is not None:
            from bowhead_gateway.terminal_pending_execution import (
                resolve_terminal_team_runtime,
            )

            _team_router, _team_kb = await resolve_terminal_team_runtime(
                request.app.state,
                target_team_id,
            )

        exec_ = ApprovalExecutor(
            approval_store=store,
            tool_registry=orch.tool_registry,
            datarc=datarc,
            active_datarc_project=_active_project,
            approver_member_uid=_approver_member,
            datarc_unavailable=datarc_ctx.datarc_unavailable,
            vector_client=datarc_ctx.vector_client,
            event_registry=orch.event_registry,
            turn_log_store=getattr(request.app.state, "turn_log_store", None),
            user_store=getattr(request.app.state, "user_store", None),
            access_control_store=getattr(request.app.state, "access_control_store", None),
            notification_service=notification_service,
            target_team_id=(target_team_id if terminal_factory is not None else None),
            team_router=_team_router,
        )

    if terminal_factory is None:
        a, transitioned = await store.set_decision_once(
            approval_id=approval_id,
            status="approved",
            decided_by=decided_by,
        )
    else:
        from bowhead_core.terminal_protocol import (
            DecisionAction,
            DecisionPrincipalSet,
            DecisionSourceKind,
        )

        if current.session_id is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "decision_source_malformed")
        try:
            from bowhead_gateway.terminal_decision_authorization import (
                TerminalDecisionAuthorizer,
            )

            principals = DecisionPrincipalSet(
                current.user_id,
                decided_by,
                decided_by,
            )
            existing = await terminal_factory.read_existing_source_decision(
                team_id=target_team_id,
                session_id=current.session_id,
                turn_id=current.turn_id,
                source_kind=DecisionSourceKind.APPROVAL,
                source_id=approval_id,
                action=DecisionAction.APPROVE,
                principals=principals,
            )
            if existing is None:
                await TerminalDecisionAuthorizer(app=request.app).authorize_pending_source(
                    team_id=target_team_id,
                    session_id=current.session_id,
                    workspace_id=current.workspace_id,
                    requester_ref=current.user_id,
                )
            prepared = await terminal_factory.record_source_decision(
                team_id=target_team_id,
                session_id=current.session_id,
                turn_id=current.turn_id,
                source_kind=DecisionSourceKind.APPROVAL,
                source_id=approval_id,
                action=DecisionAction.APPROVE,
                principals=principals,
                source_values={
                    "arguments_digest": current.arguments_digest or "",
                },
            )
        except Exception as exc:
            from bowhead_gateway.terminal_protocol_http import terminal_protocol_http_error

            raise terminal_protocol_http_error(exc) from exc
        a = await store.get(approval_id)
        transitioned = not prepared.decision.replayed
    if a is None:
        if terminal_factory is not None:
            from bowhead_gateway.terminal_protocol_http import (
                terminal_post_decision_http_error,
            )

            raise terminal_post_decision_http_error(
                RuntimeError("terminal_approval_projection_missing"),
                decision_id=prepared.decision.context.decision_id,
            )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    if a.status != "approved" or (terminal_factory is None and not transitioned):
        # Legacy replay must not spawn again.  A v1 replay is handed to the
        # durable runner so it can attach/reconcile the existing effect attempt.
        return _to_dto(
            a,
            decision_id=(prepared.decision.context.decision_id if terminal_factory is not None else None),
            decision_state=("generation_opened" if terminal_factory is not None else None),
        )

    # No fallible awaits between the committed approval transition and executor spawn.
    if terminal_factory is not None:
        try:
            from bowhead_gateway.terminal_pending_execution import (
                build_terminal_approval_executor,
            )

            exec_ = await build_terminal_approval_executor(
                request.app,
                approval_store=store,
                approval=a,
                team_id=prepared.decision.context.scope.team_id,
                effect_user_id=(prepared.decision.context.principals.effect_principal_ref),
            )
            projected = await terminal_approval_runner.execute(
                prepared=prepared,
                approval=a,
                legacy_executor=exec_,
            )
            if projected is not None:
                a = projected
        except Exception as exc:
            from bowhead_gateway.terminal_protocol_http import (
                terminal_post_decision_http_error,
            )

            raise terminal_post_decision_http_error(
                exc,
                decision_id=prepared.decision.context.decision_id,
            ) from exc
    elif exec_ is not None:
        asyncio.create_task(exec_.execute(approval_id))
    else:
        logger.warning(
            "approval.approve_no_executor id=%s",
            approval_id,
        )

    if transitioned and notification_service is not None:
        try:
            await notification_service.resolve_approval_required(a, reason="approved")
            await notification_service.emit_approval_decided(a)
        except Exception as exc:
            logger.warning(
                "approval.approve_notification_failed id=%s err=%s",
                approval_id,
                exc,
            )
    return _to_dto(
        a,
        decision_id=(prepared.decision.context.decision_id if terminal_factory is not None else None),
        decision_state=("generation_opened" if terminal_factory is not None else None),
    )


@router.post(
    "/{approval_id}/reject",
    response_model=ApprovalDTO,
    dependencies=[Depends(require_not_readonly)],
)
async def reject(
    approval_id: str,
    body: DecideBody,
    request: Request,
    store: ApprovalStore = Depends(_store),
    admin: User = Depends(_REQUIRE_APPROVAL_ADMIN),
) -> ApprovalDTO:
    current = await _get_scoped_approval(
        approval_id,
        admin=admin,
        request=request,
        store=store,
        operation=SpaceOperation.EXECUTE,
    )
    return await _reject_decision(approval_id, request=request, store=store, actor=admin, current=current)


async def _reject_decision(
    approval_id: str,
    *,
    request: Request,
    store: ApprovalStore,
    actor: User,
    current: Approval,
) -> ApprovalDTO:
    """Record a rejection by ``actor``; callers have already scoped ``current``."""
    decided_by = actor.id
    target_team_id = await _approval_target_team(current, request=request)
    terminal_factory = await _terminal_approval_factory(
        request,
        current,
        team_id=target_team_id,
    )
    if terminal_factory is None:
        a, transitioned = await store.set_decision_once(
            approval_id=approval_id,
            status="rejected",
            decided_by=decided_by,
        )
    else:
        from bowhead_core.terminal_protocol import (
            DecisionAction,
            DecisionPrincipalSet,
            DecisionSourceKind,
        )

        if current.session_id is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "decision_source_malformed")
        try:
            from bowhead_gateway.terminal_decision_authorization import (
                TerminalDecisionAuthorizer,
            )

            authorizer = TerminalDecisionAuthorizer(app=request.app)
            principals = DecisionPrincipalSet(
                current.user_id,
                decided_by,
                decided_by,
            )
            existing = await terminal_factory.read_existing_source_decision(
                team_id=target_team_id,
                session_id=current.session_id,
                turn_id=current.turn_id,
                source_kind=DecisionSourceKind.APPROVAL,
                source_id=approval_id,
                action=DecisionAction.REJECT,
                principals=principals,
            )
            if existing is None:
                await authorizer.authorize_pending_source(
                    team_id=target_team_id,
                    session_id=current.session_id,
                    workspace_id=current.workspace_id,
                    requester_ref=current.user_id,
                )
            prepared = await terminal_factory.record_source_decision(
                team_id=target_team_id,
                session_id=current.session_id,
                turn_id=current.turn_id,
                source_kind=DecisionSourceKind.APPROVAL,
                source_id=approval_id,
                action=DecisionAction.REJECT,
                principals=principals,
            )
        except Exception as exc:
            from bowhead_gateway.terminal_protocol_http import terminal_protocol_http_error

            raise terminal_protocol_http_error(exc) from exc
        try:
            from bowhead_core.terminal_protocol import DecisionState

            if prepared.decision.state is DecisionState.DECISION_RECORDED:
                await authorizer.authorize(
                    prepared,
                    workspace_id=current.workspace_id,
                )
                await terminal_factory.commit_decision_control(
                    prepared,
                    reason_code="approval_rejected",
                    message_key="turn.approval_rejected",
                )
        except Exception as exc:
            from bowhead_core.terminal_protocol_store import (
                DecisionAccessRevokedError,
            )

            from bowhead_gateway.terminal_protocol_http import (
                terminal_post_decision_http_error,
                terminal_protocol_http_error,
            )

            if isinstance(exc, DecisionAccessRevokedError):
                await terminal_factory.commit_decision_authorization_denial(
                    prepared,
                    denial_reason_code=getattr(
                        exc,
                        "reason_code",
                        "access_revoked",
                    ),
                )
                raise terminal_protocol_http_error(exc) from exc
            raise terminal_post_decision_http_error(
                exc,
                decision_id=prepared.decision.context.decision_id,
            ) from exc
        a = await store.get(approval_id)
        transitioned = not prepared.decision.replayed
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    if transitioned and a.status == "rejected":
        notification_service = _notification_service(request)
        if notification_service is not None:
            try:
                await notification_service.resolve_approval_required(a, reason="rejected")
                await notification_service.emit_approval_decided(a)
            except Exception as exc:
                logger.warning(
                    "approval.reject_notification_failed id=%s err=%s",
                    approval_id,
                    exc,
                )
        # R-2: additive terminal chat events, alongside the notification-center path above without replacing it.
        if terminal_factory is None:
            await _emit_approval_rejected_event(request, a)
    return _to_dto(
        a,
        decision_id=(prepared.decision.context.decision_id if terminal_factory is not None else None),
        decision_state=("terminal_control_committed" if terminal_factory is not None else None),
    )
