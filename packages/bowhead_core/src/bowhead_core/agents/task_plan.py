"""TaskPlan + TaskPlanCardService — H2 approval flow (sprint multi-agent-v1 §5.7 PR-#10).

HIGH 复杂度 (≥3 signal) WORK query 进 H2 approval:
- emit TaskPlanCard 给前端 (WebSocket)
- 600s 总超时, 60/300/540 三档 warning push (T-FE-P1.2 fallback Web Push 未授权)
- workspace setting `auto_approve_high_complexity=true` skip approval (CTO-P1.4)
- decision 用 nonce signed (T-SEC-P1.3)

Frontend wiring (PR-#9 TaskPlanCard React 组件) + WebSocket 双向: 留 PR-#11
turn_runner integration. 本 PR 提供 backend service + 接口.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from time import time
from typing import TYPE_CHECKING

from .complexity import Complexity

if TYPE_CHECKING:
    from .task_spec import TaskSpec

# Default timeout / warnings (CTO-P1.6 fix: 60s → 600s + three warning levels)
_DEFAULT_TOTAL_TIMEOUT_S = 600
_DEFAULT_WARNINGS_S: tuple[int, ...] = (60, 300, 540)
# T-SEC-P1.3: nonce / timestamp signed message prevents replay
_DECISION_MAX_AGE_S = 300  # 5 min, > _MAX_AGE timestamp reject


class TaskPlanOutcome(StrEnum):
    CONFIRM = "confirm"
    EDIT = "edit"
    CANCEL = "cancel"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class PlannedTask:
    """单 task 在 plan 中的 entry — 含估算 (用户看)."""

    agent_name: str
    summary: str  # User-friendly descriptions such as "财务分析"
    tokens_estimate: int
    latency_estimate_s: int
    selected: bool = True  # User can deselect in edit mode


@dataclass(frozen=True)
class TaskPlan:
    """完整 plan, 给前端 emit 用."""

    tasks: tuple[PlannedTask, ...]
    complexity: Complexity
    total_tokens_estimate: int
    total_latency_estimate_s: int
    user_question: str
    routing_reason: str
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    issued_at: int = field(default_factory=lambda: int(time()))


@dataclass(frozen=True)
class UserDecision:
    """用户响应 (从 WebSocket signed message 解出)."""

    outcome: TaskPlanOutcome
    edited_plan: TaskPlan | None = None  # User-edited plan when outcome=edit
    nonce: str = ""
    decided_at: int = 0


@dataclass(frozen=True)
class WorkspaceApprovalSettings:
    """Workspace 级 approval setting (CTO-P1.4 fix)."""

    auto_approve_high_complexity: bool = False
    """True: HIGH 跳 H2 approval 直接 dispatch (power user batch 跑尽调时关掉 prompt)."""


# Type alias: frontend emission + callback waiting for response
EmitPlanFn = Callable[[TaskPlan], Awaitable[None]]
EmitWarningFn = Callable[[TaskPlan, int], Awaitable[None]]
WaitDecisionFn = Callable[[str, int], Awaitable[UserDecision | None]]
"""(turn_id_or_session_id, timeout_s) → UserDecision (None on timeout)."""


def build_task_plan(
    *,
    user_question: str,
    routing_decision_reason: str,
    complexity: Complexity,
    tasks_with_estimates: list[TaskSpec],
) -> TaskPlan:
    """从 TaskSpec list 构造 TaskPlan (含 nonce + total estimates)."""
    planned = tuple(
        PlannedTask(
            agent_name=t.agent_name,
            summary=t.routing_reason or t.agent_name,
            tokens_estimate=t.tokens_estimate,
            latency_estimate_s=t.latency_estimate_s,
            selected=True,
        )
        for t in tasks_with_estimates
    )
    return TaskPlan(
        tasks=planned,
        complexity=complexity,
        total_tokens_estimate=sum(t.tokens_estimate for t in planned),
        total_latency_estimate_s=max((t.latency_estimate_s for t in planned), default=0),
        user_question=user_question,
        routing_reason=routing_decision_reason,
    )


class TaskPlanCardService:
    """H2 approval orchestration service.

    负责: emit plan → schedule warnings → await user decision → 解析签名校验.

    使用方式 (PR-#11 turn_runner integration 调此入口):
        service = TaskPlanCardService(emit_plan=..., emit_warning=..., wait_decision=...)
        decision = await service.await_user_decision(plan, session_id, ws_settings)
        # decision.outcome ∈ {CONFIRM, EDIT, CANCEL, TIMEOUT}
    """

    def __init__(
        self,
        *,
        emit_plan: EmitPlanFn,
        emit_warning: EmitWarningFn,
        wait_decision: WaitDecisionFn,
        total_timeout_s: int = _DEFAULT_TOTAL_TIMEOUT_S,
        warnings_at_s: tuple[int, ...] = _DEFAULT_WARNINGS_S,
    ) -> None:
        self.emit_plan = emit_plan
        self.emit_warning = emit_warning
        self.wait_decision = wait_decision
        self.total_timeout_s = total_timeout_s
        self.warnings_at_s = tuple(sorted(warnings_at_s))

    async def maybe_skip_approval(
        self,
        complexity: Complexity,
        ws_settings: WorkspaceApprovalSettings,
    ) -> bool:
        """CTO-P1.4: HIGH 复杂度 default 走 approval, 但 workspace 可 override.

        Returns:
            True: 跳 approval 直接 dispatch (LOW/MED/HIGH+auto_approve_true).
            False: 必走 approval (HIGH + auto_approve_false).
        """
        if complexity != Complexity.HIGH:
            return True
        return ws_settings.auto_approve_high_complexity

    async def await_user_decision(
        self,
        plan: TaskPlan,
        session_id: str,
        ws_settings: WorkspaceApprovalSettings | None = None,
    ) -> UserDecision:
        """主入口: emit plan + 三档 warning + await decision (or timeout).

        Args:
            plan: 已 build 好的 TaskPlan (含 nonce).
            session_id: WebSocket session 引用.
            ws_settings: 若 auto_approve_high_complexity=True 直接 confirm
                (LOW/MED 由 caller 决定不调此方法; HIGH 才入此入口).

        Returns:
            UserDecision: outcome ∈ CONFIRM / EDIT / CANCEL / TIMEOUT.
            校验失败 (invalid nonce / 时差 > 5 min) 也返 TIMEOUT (treat 同弃决).
        """
        if ws_settings and ws_settings.auto_approve_high_complexity:
            return UserDecision(
                outcome=TaskPlanOutcome.CONFIRM,
                nonce=plan.nonce,
                decided_at=int(time()),
            )

        await self.emit_plan(plan)

        # Start warning tasks in parallel.
        warning_tasks = [asyncio.create_task(self._emit_warning_at(plan, t)) for t in self.warnings_at_s if t < self.total_timeout_s]

        try:
            decision = await self.wait_decision(session_id, self.total_timeout_s)
        finally:
            # Clean up warning tasks.
            for t in warning_tasks:
                if not t.done():
                    t.cancel()

        if decision is None:
            return UserDecision(
                outcome=TaskPlanOutcome.TIMEOUT,
                nonce=plan.nonce,
                decided_at=int(time()),
            )

        if not self._validate_decision(decision, plan):
            # Invalid signature / nonce / time difference → timeout (T-SEC-P1.3)
            return UserDecision(
                outcome=TaskPlanOutcome.TIMEOUT,
                nonce=plan.nonce,
                decided_at=int(time()),
            )
        return decision

    async def _emit_warning_at(self, plan: TaskPlan, after_s: int) -> None:
        """Sleep + emit warning. Cancel safe."""
        try:
            await asyncio.sleep(after_s)
            await self.emit_warning(plan, after_s)
        except asyncio.CancelledError:
            pass

    def _validate_decision(self, decision: UserDecision, plan: TaskPlan) -> bool:
        """T-SEC-P1.3: 校验 nonce 一致 + 时差 ≤ 5 min."""
        if decision.nonce != plan.nonce:
            return False
        now = int(time())
        if now - decision.decided_at > _DECISION_MAX_AGE_S:
            return False
        # decided_at cannot be too far ahead of issued_at (clock skew tolerance).
        return decision.decided_at >= plan.issued_at - 60


def filter_selected(plan: TaskPlan) -> tuple[PlannedTask, ...]:
    """编辑模式后取 selected=True 的 tasks (用户取消勾选的不 dispatch)."""
    return tuple(t for t in plan.tasks if t.selected)
