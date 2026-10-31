"""Approval executor — 在 admin 批准后真正跑 tool.

设计：
    - decide(approved) → 不立即跑（API 返回快）；spawn asyncio.Task 跑
    - execute 阶段：从 approval 恢复 ToolExecutionContext，调用 ToolRegistry.execute
    - 结果写回 approval_store.record_execution；terminal event 统一走 out-of-band emit
    - 写操作（approve → execute）从 HTTP 角度看是异步的：
      client 先收到 200，轮询 GET /approvals/{id} 看 status
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_bi_bridge import DatarcClient
    from bowhead_core.approval_store import Approval, ApprovalStore
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.notification_service import NotificationService
    from bowhead_core.tool_registry import ToolRegistry
    from bowhead_core.turn_log_store import TurnLogStore
    from bowhead_core.user_store import UserStore

logger = logging.getLogger(__name__)


@dataclass
class ApprovalExecutor:
    approval_store: ApprovalStore
    tool_registry: ToolRegistry
    datarc: DatarcClient | None
    active_datarc_project: str | None = None
    datarc_binding: Any = None
    datarc_unavailable: bool = False
    vector_client: Any | None = None
    event_registry: EventBusRegistry | None = None
    turn_log_store: TurnLogStore | None = None
    user_store: UserStore | None = None
    access_control_store: Any | None = None
    notification_service: NotificationService | None = None
    # Batch 2/c9 (PRD §3.9): in per-user mode, the approver's member_uid is re-resolved against
    # the pinned snapshot workspace, overriding appr.member_uid (the requester's stored value may be invalid there;
    # identity is the current caller). v1 never falls back to requester; None preserves legacy behavior.
    approver_member_uid: str | None = None
    # v1 decisions pin the durable target team.  Legacy callers leave this
    # empty and retain the historical primary-team lookup.
    target_team_id: str | None = None
    # Terminal v1 opens a real N+1 TurnRequest even though the current worker
    # executes only the approved effect.  Preserve the target team's router so
    # no future orchestration hook can fall back to process-level assembly.
    team_router: Any | None = None

    async def _record_execution_and_notify(
        self,
        *,
        approval_id: str,
        ok: bool,
        result: Any,
        error: str | None,
    ) -> Approval | None:
        updated = await self.approval_store.record_execution(
            approval_id=approval_id,
            ok=ok,
            result=result,
            error=error,
        )
        if updated is not None and self.notification_service is not None:
            try:
                await self.notification_service.emit_approval_executed(updated)
            except Exception as exc:
                logger.warning(
                    "approval_executor.notification_emit_failed id=%s err=%s",
                    approval_id,
                    exc,
                )
        if updated is not None:
            await self._emit_execution_event(
                approval=updated,
                ok=ok,
                result=result,
                error=error,
            )
        return updated

    async def _access_snapshot(
        self,
        appr: Approval,
        *,
        effect_user_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, str, str | None]:
        user_id = effect_user_id or appr.user_id
        if self.user_store is None:
            return None, "", "user", None
        try:
            from bowhead_core.access_control import AccessContext, normalize_workspace_role

            team_id = self.target_team_id or await self.user_store.get_user_team_id(user_id)
            ws_team_id = await self.user_store.get_workspace_team_id(appr.workspace_id)
            role = await self.user_store.get_member_role(appr.workspace_id, user_id)
            if not team_id:
                return None, "", "user", "approval authz failed: user_not_active"
            if not ws_team_id or ws_team_id != team_id:
                return None, team_id, "user", "approval authz failed: workspace_team_mismatch"
            workspace_role = normalize_workspace_role(role)
            if not workspace_role:
                return None, team_id, "user", "approval authz failed: workspace_member_required"

            team_role: str | None = None
            is_team_admin = False
            try:
                # SP-AUTH-3: recognize only roles within the approval snapshot team; never fall back to team-less
                # is_team_admin() (which uses the primary team in users.team_id).
                team_role = await self.user_store.get_team_role(user_id, team_id)
                if self.target_team_id is not None and team_role is None:
                    return (
                        None,
                        team_id,
                        "user",
                        "approval authz failed: team_member_required",
                    )
                is_team_admin = team_role in {"owner", "team_admin"}
            except Exception as exc:
                logger.warning(
                    "approval_executor.team_role_lookup_failed id=%s user=%s err=%s",
                    appr.id,
                    user_id,
                    exc,
                )
                if self.target_team_id is not None:
                    return (
                        None,
                        team_id,
                        "user",
                        "approval authz failed: team_role_unavailable",
                    )

            group_ids: tuple[str, ...] = ()
            policy_epoch = 0
            if self.access_control_store is not None:
                group_ids = await self.access_control_store.principal_group_ids(
                    team_id,
                    user_id,
                    appr.workspace_id,
                )
                policy_epoch = await self.access_control_store.get_policy_epoch(team_id)

            ctx = AccessContext(
                user_id=user_id,
                team_id=team_id,
                workspace_id=appr.workspace_id,
                workspace_role=workspace_role,
                team_role=team_role,
                group_ids=group_ids,
                policy_epoch=policy_epoch,
                is_team_admin=is_team_admin,
            )
            if is_team_admin:
                kb_role = "team_admin"
            elif workspace_role == "ws_admin":
                kb_role = "ws_admin"
            else:
                kb_role = "user"
            return ctx.as_dict(), team_id, kb_role, None
        except Exception as exc:
            logger.warning(
                "approval_executor.access_context_failed id=%s user=%s ws=%s err=%s",
                appr.id,
                user_id,
                appr.workspace_id,
                exc,
            )
            return None, "", "user", "approval authz failed: access_context_error"

    async def _reference_source_denial(self, appr: Approval) -> str | None:
        # The approval DB is a different store and is never a Chat-source fallback.
        source_path = getattr(self.user_store, "db_path", None)
        if source_path is None:
            return None
        from bowhead_core.skill_reference_context import reference_turn_requires_context

        try:
            if await reference_turn_requires_context(source_path, turn_id=appr.turn_id):
                return "reference_approval_binding_required"
        except Exception:
            return "reference_approval_source_unavailable"
        return None

    async def execute(self, approval_id: str) -> Approval | None:
        appr = await self.approval_store.get(approval_id)
        if appr is None:
            logger.warning("approval_executor.not_found id=%s", approval_id)
            return None
        if appr.status != "approved":
            logger.warning(
                "approval_executor.bad_status id=%s status=%s",
                approval_id,
                appr.status,
            )
            return appr

        if not self.tool_registry.has(appr.tool_name):
            return await self._record_execution_and_notify(
                approval_id=approval_id,
                ok=False,
                result=None,
                error=f"tool '{appr.tool_name}' not registered",
            )

        # Defer import to avoid a cycle.
        from bowhead_core.tool_registry import ToolExecutionContext

        access_context, team_id, kb_role, access_error = await self._access_snapshot(appr)
        if access_error is not None:
            return await self._record_execution_and_notify(
                approval_id=approval_id,
                ok=False,
                result=None,
                error=access_error,
            )

        if denial := await self._reference_source_denial(appr):
            return await self._record_execution_and_notify(approval_id=approval_id, ok=False, result=None, error=denial)

        ctx = ToolExecutionContext(
            workspace_id=appr.workspace_id,
            member_uid=self.approver_member_uid or appr.member_uid,
            user_id=appr.user_id,
            turn_id=appr.turn_id,
            session_id=appr.session_id,
            reference_source_db_path=getattr(self.user_store, "db_path", None),
            active_datarc_project=self.active_datarc_project,
            datarc_binding=self.datarc_binding,
            datarc=None if self.datarc_unavailable else self.datarc,
            vector_client=self.vector_client,
            team_id=team_id,
            access_context=access_context,
            access_control_store=self.access_control_store,
            kb_role=kb_role,
        )

        # After approval, run spec.handler directly (registry.execute catches exceptions and records execution_failed).
        # 2026-07-21: the previously described four BI write stubs that always raised no longer exist; the entire family
        # and _make_write_stub were removed (datarc client has no write methods; see tool_registry.py).
        # Only tools added to approval_gate.SUPPORTED_NATIVE_WRITE_TOOLS in the future can reach this point.
        invocation = await self.tool_registry.execute(
            appr.tool_name,
            appr.arguments,
            ctx,
        )
        ok = invocation.error is None
        result = invocation.result if ok else None
        updated = await self._record_execution_and_notify(
            approval_id=approval_id,
            ok=ok,
            result=result,
            error=invocation.error,
        )
        logger.info(
            "approval_executor.done id=%s tool=%s ok=%s dur=%dms",
            approval_id,
            appr.tool_name,
            ok,
            invocation.duration_ms,
        )

        return updated

    async def execute_effect(
        self,
        approval_id: str,
        *,
        effect_user_id: str,
        idempotency_key: str,
    ) -> tuple[bool, Any, str | None]:
        """Execute only the provider call; v1 owns settlement and projection."""

        appr = await self.approval_store.get(approval_id)
        if appr is None:
            return False, None, "approval not found"
        if appr.status != "approved":
            return False, None, "approval is not approved"
        if not self.tool_registry.has(appr.tool_name):
            return False, None, f"tool '{appr.tool_name}' not registered"
        from bowhead_core.tool_registry import ToolExecutionContext

        access_context, team_id, kb_role, access_error = await self._access_snapshot(
            appr,
            effect_user_id=effect_user_id,
        )
        if access_error is not None:
            return False, None, access_error
        if self.target_team_id is not None and appr.datarc_workspace_id and not self.approver_member_uid:
            return (
                False,
                None,
                "approval authz failed: effect_member_required",
            )
        if denial := await self._reference_source_denial(appr):
            return False, None, denial

        ctx = ToolExecutionContext(
            workspace_id=appr.workspace_id,
            member_uid=(self.approver_member_uid or "" if self.target_team_id is not None else self.approver_member_uid or appr.member_uid),
            user_id=effect_user_id,
            turn_id=appr.turn_id,
            session_id=appr.session_id,
            reference_source_db_path=getattr(self.user_store, "db_path", None),
            active_datarc_project=self.active_datarc_project,
            datarc_binding=self.datarc_binding,
            datarc=None if self.datarc_unavailable else self.datarc,
            vector_client=self.vector_client,
            team_id=team_id,
            access_context=access_context,
            access_control_store=self.access_control_store,
            kb_role=kb_role,
            effect_idempotency_key=idempotency_key,
        )
        invocation = await self.tool_registry.execute(
            appr.tool_name,
            appr.arguments,
            ctx,
            propagate_ambiguous_transport=True,
        )
        return (
            invocation.error is None,
            invocation.result if invocation.error is None else None,
            invocation.error,
        )

    async def _emit_execution_event(
        self,
        *,
        approval: Approval,
        ok: bool,
        result: Any,
        error: str | None,
    ) -> None:
        """Persist the terminal approval fact even after the turn bus closes."""
        if self.event_registry is None:
            return

        fallback_initial_seq = 0
        if self.turn_log_store is not None:
            try:
                fallback_initial_seq = int(await self.turn_log_store.max_seq(approval.turn_id))
            except Exception as exc:
                logger.warning(
                    "approval_executor.turn_seq_seed_failed id=%s turn=%s err=%s",
                    approval.id,
                    approval.turn_id,
                    exc,
                )

        try:
            from bowhead_core.events import OrchestratorEvent
            from bowhead_core.token_estimator import count_tokens_of_obj

            try:
                result_token_count = count_tokens_of_obj(result) if ok else -1
            except Exception:
                result_token_count = -1
            try:
                preview = json.dumps(result, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                preview = str(result)
            if len(preview) > 500:
                preview = f"{preview[:500]}…"

            await self.event_registry.emit_out_of_band(
                approval.turn_id,
                OrchestratorEvent(
                    event_type="approval.executed",
                    turn_id=approval.turn_id,
                    payload={
                        "card_id": approval.turn_id,
                        "approval_id": approval.id,
                        "ok": ok,
                        "result_preview": preview if ok else None,
                        "result_token_count": result_token_count,
                        "error": error,
                    },
                ),
                fallback_initial_seq=fallback_initial_seq,
            )
        except Exception as exc:
            logger.warning(
                "approval_executor.event_emit_failed id=%s turn=%s err=%s",
                approval.id,
                approval.turn_id,
                exc,
            )
