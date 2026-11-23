"""delivery-surface C4 —— subagent_spawn 的 delivery 参数。

Lead 拿主意（chat=聊天短答 / report=画布报告），经 spawn 参数结构化下传，
注入 peer 的 Sub-task context——这是 peer 第一次能看到用户的交付诉求
（task= 是 Lead 转述，用户原话不下传）。
"""

from __future__ import annotations

import pytest
from bowhead_core.subagent import SubagentResult, register_subagent_tool
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry


def test_spawn_schema_advertises_delivery() -> None:
    registry = build_default_registry()
    register_subagent_tool(registry)
    props = registry.get("subagent_spawn").schema.input_schema["properties"]
    assert props["delivery"]["enum"] == ["chat", "report"]
    assert "delivery" not in registry.get("subagent_spawn").schema.input_schema["required"]


class _CapturingRunner:
    """假 runner：只捕获 extra_instructions，返回最小成功结果。"""

    def __init__(self, result: SubagentResult | None = None) -> None:
        self.kwargs: dict = {}
        self.result = result

    async def run(self, **kwargs):
        self.kwargs = kwargs
        return self.result or SubagentResult(
            profile=str(kwargs.get("profile_name") or "compute"),
            subagent_id="sub_test",
            ok=True,
            final_answer="42",
        )


class _FakeBus:
    async def emit(self, ev) -> None:  # noqa: ARG002
        return None


@pytest.mark.parametrize("stop_reason", [None, "step_limit"])
async def test_partial_receipt_preserves_text_and_actual_stop_reason(stop_reason: str | None) -> None:
    registry = build_default_registry()
    register_subagent_tool(registry)
    partial = SubagentResult(
        profile="compute",
        subagent_id="sub_partial",
        ok=True,
        completion="partial",
        final_answer="Revenue is 4200; required profit is not delivered.",
        stop_reason=stop_reason,
        partial_done="Revenue is 4200." if stop_reason else None,
    )
    ctx = ToolExecutionContext(
        workspace_id="ws_test",
        member_uid="member",
        user_id="user",
        turn_id="t_partial",
        datarc=None,
        subagent_runner=_CapturingRunner(partial),
        parent_bus=_FakeBus(),
    )
    invocation = await registry.execute("subagent_spawn", {"profile": "compute", "task": "Deliver revenue and profit."}, ctx)
    receipt = invocation.result
    assert receipt["completion"] == "partial"
    assert receipt["stop_reason"] == stop_reason
    assert receipt["done"] == (partial.partial_done or partial.final_answer)
    assert ("stopped at" in receipt["directive"]) is bool(stop_reason)
    assert "a run limit" not in receipt["directive"]


@pytest.mark.parametrize(
    ("denial", "fact", "action"),
    [
        ("team_pool_exhausted", "the Team's credit pool is used up", "a Team owner or administrator adds Team credits"),
        ("user_credit_exhausted", "the user's own credit allowance is used up", "allocates more credits to the user"),
        (None, "stopped at the available credit", None),
    ],
)
async def test_a_credit_stop_tells_lead_which_balance_ran_out_and_who_restores_it(denial: str | None, fact: str, action: str | None) -> None:
    """Observed execution: told only "the available credit", Lead dispatched another helper into the same stop
    and offered to "continue" without saying credits had to be added. Replayed with these facts, 8 of 8 answers named
    the Team credits and who adds them (0 of 8 before)."""
    registry = build_default_registry()
    register_subagent_tool(registry)
    partial = SubagentResult(
        profile="compute",
        subagent_id="sub_credit",
        ok=False,
        completion="partial",
        final_answer="",
        stop_reason="budget",
        partial_done="Template structure read.",
        credit_denial=denial,
    )
    ctx = ToolExecutionContext(
        workspace_id="ws_test",
        member_uid="member",
        user_id="user",
        turn_id="t_credit",
        datarc=None,
        subagent_runner=_CapturingRunner(partial),
        parent_bus=_FakeBus(),
    )
    invocation = await registry.execute("subagent_spawn", {"profile": "compute", "task": "Fill the template."}, ctx)
    directive = invocation.result["directive"]
    assert fact in directive
    assert ("every helper dispatched in this turn stops after its first step" in directive) is bool(denial)
    if action:
        assert action in directive
    assert "Do not dispatch this task again in this turn." in directive


@pytest.mark.parametrize(
    ("balances", "denial", "action"),
    [
        ((5000, 0), "team_pool_exhausted", "a Team owner or administrator adds Team credits"),
        ((0, 5000), "user_credit_exhausted", "a Team owner or administrator allocates more credits to the user"),
        ((5000, 5000), None, None),
    ],
)
async def test_no_helper_is_started_while_the_turns_credit_is_used_up(balances: tuple[int, int], denial: str | None, action: str | None) -> None:
    """Observed execution: after a credit stop Lead dispatched again and that helper stopped after one step.
    Replayed with this refusal, 8 of 8 runs answered at once and 7 of 8 named the Team credits and who adds them."""
    from bowhead_core import agent_run_limits as rl

    async def read_balances():
        return balances

    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _CapturingRunner()
    ctx = ToolExecutionContext(
        workspace_id="ws_test",
        member_uid="member",
        user_id="user",
        turn_id="t_credit_dispatch",
        datarc=None,
        subagent_runner=runner,
        parent_bus=_FakeBus(),
    )
    token = rl.set_credit_gate(rl.TurnCreditGate(read_balances, lambda: 0))
    try:
        invocation = await registry.execute("subagent_spawn", {"profile": "compute", "task": "Fill the template."}, ctx)
    finally:
        rl.reset_credit_gate(token)
    result = invocation.result
    if denial is None:
        assert result["ok"] is True and runner.kwargs
        return
    assert (result["ok"], result["error_kind"], result["credit_denial"]) == (False, "credit_exhausted", denial)
    assert "no helper was started" in result["error"] and action in result["error"]
    assert runner.kwargs == {}


async def _spawn_with(args: dict, bus: _FakeBus | None = None) -> _CapturingRunner:
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _CapturingRunner()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="parent_t",
        datarc=None,
        subagent_runner=runner,
        parent_bus=bus if bus is not None else _FakeBus(),
    )
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "查今年利润前三的品牌", **args}, ctx)
    assert inv.error is None, inv.error
    assert runner.kwargs, f"runner.run 未被调用: {inv.result}"
    return runner


async def test_delivery_chat_injected_into_peer_context() -> None:
    runner = await _spawn_with({"delivery": "chat"})
    extra = str(runner.kwargs.get("extra_instructions") or "")
    assert "delivery" in extra
    assert "delivery_surface='chat'" in extra
    # Delivery-surface autonomy (batch 5ⓐ): chat may include inline charts; canvas_add_* is no longer forbidden.
    assert "不要写画布报告" in extra


async def test_delivery_report_injected_into_peer_context() -> None:
    runner = await _spawn_with({"delivery": "report"})
    extra = str(runner.kwargs.get("extra_instructions") or "")
    assert "report" in extra
    assert "AnalysisPayload" in extra


async def test_delivery_omitted_leaves_context_untouched() -> None:
    """省略 delivery（证据驱动默认）：不注入任何 delivery 行——行为与 C4 之前逐字节一致。"""
    runner = await _spawn_with({})
    extra = str(runner.kwargs.get("extra_instructions") or "")
    assert "delivery" not in extra


async def test_delivery_invalid_value_treated_as_omitted() -> None:
    """LLM 可写面防御：非法值按省略处理，不进 peer 上下文。"""
    runner = await _spawn_with({"delivery": "canvas"})
    extra = str(runner.kwargs.get("extra_instructions") or "")
    assert "delivery" not in extra


async def test_spawn_stamps_delivery_on_bus_report_wins() -> None:
    """review #13b：spawn 决定钉到 turn 级 bus（ψ-E eager 位点读它），
    冲突口径与 `_lead_spawn_delivery` 同 = report 赢，且不受 spawn 顺序影响。"""
    bus = _FakeBus()
    await _spawn_with({"delivery": "chat"}, bus=bus)
    assert bus._lead_delivery_spawn == "chat"
    await _spawn_with({"delivery": "report"}, bus=bus)
    assert bus._lead_delivery_spawn == "report"
    # Once report is chosen, later chat spawns cannot change it back.
    await _spawn_with({"delivery": "chat"}, bus=bus)
    assert bus._lead_delivery_spawn == "report"


async def test_spawn_without_delivery_leaves_bus_unstamped() -> None:
    bus = _FakeBus()
    await _spawn_with({}, bus=bus)
    assert not hasattr(bus, "_lead_delivery_spawn")
