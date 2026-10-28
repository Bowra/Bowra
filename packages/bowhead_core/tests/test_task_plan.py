"""Sprint multi-agent-v1 PR-#10 — TaskPlanCardService 单测.

覆盖:
- TaskPlan 构造 (含 nonce + total estimates)
- maybe_skip_approval 行为 (LOW/MED 跳, HIGH+auto_approve 跳, HIGH+default 不跳)
- await_user_decision: confirm / edit / cancel / timeout
- 三档 warning push
- 签名校验 (nonce 不一致 / 时差 > 5 min → 当 TIMEOUT)
- filter_selected
"""

from __future__ import annotations

import asyncio
from time import time

import pytest
from bowhead_core.agents.complexity import Complexity
from bowhead_core.agents.task_plan import (
    PlannedTask,
    TaskPlan,
    TaskPlanCardService,
    TaskPlanOutcome,
    UserDecision,
    WorkspaceApprovalSettings,
    build_task_plan,
    filter_selected,
)
from bowhead_core.agents.task_spec import TaskSpec


def _mk_specs(n: int = 3) -> list[TaskSpec]:
    return [
        TaskSpec(
            agent_name=f"agent-{i}",
            user_question=f"q{i}",
            routing_reason=f"reason-{i}",
            tokens_estimate=1000 * (i + 1),
            latency_estimate_s=30 * (i + 1),
        )
        for i in range(n)
    ]


# -- build_task_plan --


def test_build_plan_basic() -> None:
    specs = _mk_specs(3)
    plan = build_task_plan(
        user_question="test question",
        routing_decision_reason="multi-domain",
        complexity=Complexity.HIGH,
        tasks_with_estimates=specs,
    )
    assert len(plan.tasks) == 3
    assert plan.total_tokens_estimate == 1000 + 2000 + 3000
    assert plan.total_latency_estimate_s == 90  # max
    assert plan.complexity == Complexity.HIGH
    assert len(plan.nonce) > 16  # secrets.token_urlsafe(16) ≥ 22 chars
    assert plan.issued_at > 0


def test_build_plan_empty_tasks() -> None:
    plan = build_task_plan(
        user_question="q",
        routing_decision_reason="r",
        complexity=Complexity.LOW,
        tasks_with_estimates=[],
    )
    assert plan.tasks == ()
    assert plan.total_latency_estimate_s == 0


# -- WorkspaceApprovalSettings + maybe_skip_approval --


@pytest.mark.asyncio
async def test_maybe_skip_low_med() -> None:
    """LOW / MED 总是跳 (default + auto_approve 都跳)."""
    service = _mk_service()
    assert await service.maybe_skip_approval(Complexity.LOW, WorkspaceApprovalSettings())
    assert await service.maybe_skip_approval(Complexity.MED, WorkspaceApprovalSettings())


@pytest.mark.asyncio
async def test_maybe_skip_high_default_does_not_skip() -> None:
    """HIGH default (auto_approve=false) 不跳 — 走 approval."""
    service = _mk_service()
    assert not await service.maybe_skip_approval(Complexity.HIGH, WorkspaceApprovalSettings())


@pytest.mark.asyncio
async def test_maybe_skip_high_auto_approve_skips() -> None:
    """HIGH 但 workspace 设 auto_approve=True → 跳 (CTO-P1.4 power user)."""
    service = _mk_service()
    assert await service.maybe_skip_approval(
        Complexity.HIGH,
        WorkspaceApprovalSettings(auto_approve_high_complexity=True),
    )


# -- await_user_decision --


def _mk_plan() -> TaskPlan:
    return build_task_plan(
        user_question="q",
        routing_decision_reason="r",
        complexity=Complexity.HIGH,
        tasks_with_estimates=_mk_specs(2),
    )


def _mk_service(
    *,
    decision: UserDecision | None = None,
    timeout_s: int = 600,
    warnings_at_s: tuple[int, ...] = (60, 300, 540),
    emit_plan_calls: list | None = None,
    emit_warning_calls: list | None = None,
) -> TaskPlanCardService:
    """构造一个 service, decision 是 wait_decision 返回值 (None=timeout)."""
    emit_plan_calls = emit_plan_calls if emit_plan_calls is not None else []
    emit_warning_calls = emit_warning_calls if emit_warning_calls is not None else []

    async def _emit_plan(plan: TaskPlan) -> None:
        emit_plan_calls.append(plan)

    async def _emit_warning(plan: TaskPlan, after: int) -> None:
        emit_warning_calls.append((plan, after))

    async def _wait_decision(_sid: str, _t: int) -> UserDecision | None:
        return decision

    return TaskPlanCardService(
        emit_plan=_emit_plan,
        emit_warning=_emit_warning,
        wait_decision=_wait_decision,
        total_timeout_s=timeout_s,
        warnings_at_s=warnings_at_s,
    )


@pytest.mark.asyncio
async def test_await_decision_confirm() -> None:
    plan = _mk_plan()
    decision = UserDecision(
        outcome=TaskPlanOutcome.CONFIRM,
        nonce=plan.nonce,
        decided_at=plan.issued_at + 5,
    )
    service = _mk_service(decision=decision)
    result = await service.await_user_decision(plan, "s_1")
    assert result.outcome == TaskPlanOutcome.CONFIRM


@pytest.mark.asyncio
async def test_await_decision_cancel() -> None:
    plan = _mk_plan()
    decision = UserDecision(
        outcome=TaskPlanOutcome.CANCEL,
        nonce=plan.nonce,
        decided_at=plan.issued_at + 10,
    )
    service = _mk_service(decision=decision)
    result = await service.await_user_decision(plan, "s_1")
    assert result.outcome == TaskPlanOutcome.CANCEL


@pytest.mark.asyncio
async def test_await_decision_edit() -> None:
    plan = _mk_plan()
    edited = build_task_plan(
        user_question=plan.user_question,
        routing_decision_reason=plan.routing_reason,
        complexity=Complexity.HIGH,
        tasks_with_estimates=_mk_specs(1),  # User canceled 1 task.
    )
    decision = UserDecision(
        outcome=TaskPlanOutcome.EDIT,
        edited_plan=edited,
        nonce=plan.nonce,
        decided_at=plan.issued_at + 30,
    )
    service = _mk_service(decision=decision)
    result = await service.await_user_decision(plan, "s_1")
    assert result.outcome == TaskPlanOutcome.EDIT
    assert result.edited_plan is not None
    assert len(result.edited_plan.tasks) == 1


@pytest.mark.asyncio
async def test_await_decision_timeout() -> None:
    """wait_decision 返 None → TIMEOUT."""
    plan = _mk_plan()
    service = _mk_service(decision=None)
    result = await service.await_user_decision(plan, "s_1")
    assert result.outcome == TaskPlanOutcome.TIMEOUT


@pytest.mark.asyncio
async def test_await_decision_invalid_nonce_treated_as_timeout() -> None:
    """T-SEC-P1.3: nonce 不一致 → 当 timeout reject."""
    plan = _mk_plan()
    decision = UserDecision(
        outcome=TaskPlanOutcome.CONFIRM,
        nonce="wrong-nonce",
        decided_at=plan.issued_at + 5,
    )
    service = _mk_service(decision=decision)
    result = await service.await_user_decision(plan, "s_1")
    assert result.outcome == TaskPlanOutcome.TIMEOUT


@pytest.mark.asyncio
async def test_await_decision_old_timestamp_rejected() -> None:
    """T-SEC-P1.3: decided_at > 5 min 前 → reject."""
    plan = _mk_plan()
    old_ts = int(time()) - 600  # 10 min ago
    decision = UserDecision(
        outcome=TaskPlanOutcome.CONFIRM,
        nonce=plan.nonce,
        decided_at=old_ts,
    )
    service = _mk_service(decision=decision)
    result = await service.await_user_decision(plan, "s_1")
    assert result.outcome == TaskPlanOutcome.TIMEOUT


@pytest.mark.asyncio
async def test_await_decision_auto_approve_high_skips_emit() -> None:
    """auto_approve_high=True 直接 confirm 不 emit."""
    plan = _mk_plan()
    emit_calls: list = []
    service = _mk_service(decision=None, emit_plan_calls=emit_calls)
    result = await service.await_user_decision(
        plan,
        "s_1",
        ws_settings=WorkspaceApprovalSettings(auto_approve_high_complexity=True),
    )
    assert result.outcome == TaskPlanOutcome.CONFIRM
    assert emit_calls == []  # No emission


@pytest.mark.asyncio
async def test_await_decision_emits_plan() -> None:
    """正常 flow: 应调 emit_plan."""
    plan = _mk_plan()
    decision = UserDecision(
        outcome=TaskPlanOutcome.CONFIRM,
        nonce=plan.nonce,
        decided_at=plan.issued_at + 5,
    )
    emit_calls: list = []
    service = _mk_service(decision=decision, emit_plan_calls=emit_calls)
    await service.await_user_decision(plan, "s_1")
    assert len(emit_calls) == 1
    assert emit_calls[0] is plan


@pytest.mark.asyncio
async def test_warning_schedule() -> None:
    """warning callback 在 sleep 后调."""
    plan = _mk_plan()
    decision = UserDecision(
        outcome=TaskPlanOutcome.CONFIRM,
        nonce=plan.nonce,
        decided_at=plan.issued_at + 5,
    )
    warning_calls: list = []

    async def _emit_plan(_p: TaskPlan) -> None:
        pass

    async def _emit_warning(p: TaskPlan, after: int) -> None:
        warning_calls.append((after, p))

    async def _wait(_sid: str, _t: int) -> UserDecision | None:
        # Wait 0.3s to trigger the first 2 warnings.
        await asyncio.sleep(0.3)
        return decision

    service = TaskPlanCardService(
        emit_plan=_emit_plan,
        emit_warning=_emit_warning,
        wait_decision=_wait,
        total_timeout_s=5,
        warnings_at_s=(0.05, 0.15, 0.25),  # type: ignore[arg-type]
    )
    await service.await_user_decision(plan, "s_1")
    # 0.05 + 0.15 + 0.25 all fire within 0.3s.
    afters = sorted(c[0] for c in warning_calls)
    assert afters == [0.05, 0.15, 0.25]


# -- filter_selected --


def test_filter_selected_keeps_selected() -> None:
    plan = TaskPlan(
        tasks=(
            PlannedTask(
                agent_name="a",
                summary="A",
                tokens_estimate=100,
                latency_estimate_s=10,
                selected=True,
            ),
            PlannedTask(
                agent_name="b",
                summary="B",
                tokens_estimate=100,
                latency_estimate_s=10,
                selected=False,
            ),
            PlannedTask(
                agent_name="c",
                summary="C",
                tokens_estimate=100,
                latency_estimate_s=10,
                selected=True,
            ),
        ),
        complexity=Complexity.MED,
        total_tokens_estimate=300,
        total_latency_estimate_s=10,
        user_question="q",
        routing_reason="r",
    )
    selected = filter_selected(plan)
    assert len(selected) == 2
    assert [t.agent_name for t in selected] == ["a", "c"]
