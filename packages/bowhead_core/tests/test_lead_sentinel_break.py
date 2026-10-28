"""Sprint chat-v4-streaming Phase 4B-1 — Lead sentinel in-invocation-loop break tests.

Per tech-design.md §2.5 (T2.1-T2.5):
- T2.1: single sentinel break — Lead emit 1 clarification_ask, outer loop exits
- T2.2: batched 3 sentinels in 1 invocation (CH2 path) — all 3 accumulated, break only after
- T2.3: sentinel mid-invocation 不 abort — [bi_query, sentinel, bi_query] all 3 execute before break
- T2.4: skip (integration fixture, in Phase 5.5)
- T2.5: peer profile 不走 sentinel break — compute/modeler/analyst keep iterating

Critical invariants:
- I4 (CH2 batch consume-then-break): wait until all survived_tcs execute + append
- I5 (sentinel-break Lead-only): profile.name == "lead" gate
- I6 (invocations integrity): no truncation, lead_orchestrator sees all sentinels
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from bowhead_core.agents.clarification_tool import make_clarification_sentinel
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner, ToolInvocation
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    ToolCall,
)


def _router(*rs: LLMResponse) -> Router:
    """Build router with scripted MockAdapter responses (covers lead + peer profiles)."""
    a = MockAdapter(provider_name="p1")
    a.scripted_responses = list(rs)
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = a  # type: ignore[attr-defined]
    r = Router(reg)
    for t in ("lead", "compute", "modeler", "analyst", "echo", "judge", "verifier", "web_search"):
        r.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    return r


@dataclass
class _StubDatarc:
    """Minimal datarc stub — sentinel tests don't actually call BI."""

    async def aclose(self) -> None:
        pass


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t_sent_test",
        datarc=_StubDatarc(),  # type: ignore[arg-type]
    )


# ─────────────────────────────────────────────────────────────
# T2.1 — single sentinel break: Lead emits 1 clarification_ask,
# outer iteration loop exits at that iter (not max_iterations).
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_lead_single_sentinel_break() -> None:
    """Lead emits 1 clarification_ask at iter=0. Outer for-loop must break
    immediately after the invocation appends, NOT call LLM iter=1."""
    router = _router(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="c1", name="clarification_ask", arguments={"candidates": ["看营业额", "看利润"]}),
            ],
        ),
        # This 2nd response would only be hit if break failed → assertion fails.
        LLMResponse(content="should not reach", tool_calls=[]),
    )
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")

    # Patch tool exec to return sentinel directly (bypasses real clarification_tool dispatch).
    async def _stub_exec(tc: ToolCall, *args: Any, **kwargs: Any) -> ToolInvocation:
        if tc.name == "clarification_ask":
            sentinel = make_clarification_sentinel(
                candidates=tc.arguments.get("candidates", []),
                fallback_text=None,
                aborted_turn_id="t_sent_test",
            )
            return ToolInvocation(
                tool_name=tc.name,
                arguments=tc.arguments,
                result=sentinel,
                error=None,
            )
        return ToolInvocation(tool_name=tc.name, arguments=tc.arguments, result=None, error=None)

    runner._execute_tool = _stub_exec  # type: ignore[method-assign]

    bus = TurnEventBus()
    result = await runner.run(
        profile_name="lead",
        task="分析一下",
        parent_turn_id="t_sent_test",
        parent_bus=bus,
        parent_ctx=_ctx(),
    )

    assert len(result.tool_invocations) == 1, (
        f"expect exactly 1 invocation (sentinel break at iter=0), got "
        f"{len(result.tool_invocations)}: {[i.tool_name for i in result.tool_invocations]}"
    )
    assert result.tool_invocations[0].tool_name == "clarification_ask"
    sentinel_result = result.tool_invocations[0].result
    assert isinstance(sentinel_result, dict)
    assert sentinel_result.get("_clarification_pending") is True


# ─────────────────────────────────────────────────────────────
# T2.2 — batched 3 sentinels in 1 invocation (CH2 path): all 3
# tool_calls execute + accumulate, break ONLY after batch complete.
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_lead_batched_3_sentinels_one_invocation() -> None:
    """Lead emits 3 clarification_ask in 1 LLM response. Per I4 invariant
    (CH2 batch consume-then-break), all 3 must execute + append before
    outer loop breaks. lead_orchestrator.py:1430-1503 needs all 3 sentinels
    visible in result.tool_invocations for batched merge."""
    router = _router(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="c1", name="clarification_ask", arguments={"candidates": ["指标1"]}),
                ToolCall(id="c2", name="clarification_ask", arguments={"candidates": ["指标2"]}),
                ToolCall(id="c3", name="clarification_ask", arguments={"candidates": ["指标3"]}),
            ],
        ),
        LLMResponse(content="should not reach", tool_calls=[]),
    )
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")

    async def _stub_exec(tc: ToolCall, *args: Any, **kwargs: Any) -> ToolInvocation:
        sentinel = make_clarification_sentinel(
            candidates=tc.arguments.get("candidates", []),
            fallback_text=None,
            aborted_turn_id="t_sent_test",
        )
        return ToolInvocation(
            tool_name=tc.name,
            arguments=tc.arguments,
            result=sentinel,
            error=None,
        )

    runner._execute_tool = _stub_exec  # type: ignore[method-assign]

    bus = TurnEventBus()
    result = await runner.run(
        profile_name="lead",
        task="分析一下",
        parent_turn_id="t_sent_test",
        parent_bus=bus,
        parent_ctx=_ctx(),
    )

    # I4: all 3 sentinels accumulated (not just first) — batched HITL invariant
    assert len(result.tool_invocations) == 3, (
        f"CH2 invariant: all 3 sentinels in batch must execute + append before break. Got {len(result.tool_invocations)} invocations."
    )
    names = [i.tool_name for i in result.tool_invocations]
    assert names == ["clarification_ask"] * 3, f"unexpected order: {names}"
    # Verify all 3 are sentinels
    for inv in result.tool_invocations:
        assert isinstance(inv.result, dict)
        assert inv.result.get("_clarification_pending") is True


# T2.3 (sentinel mid-batch with mixed tool_calls) — folded into T2.2.
# Original concept: [bi_query, sentinel, bi_query] tests consume-then-break.
# But lead profile filters non-lead tools (bi_query goes to peers), so the
# realistic prod scenario for I4 invariant is N×clarification_ask in 1 batch
# which T2.2 already covers exhaustively (verify all 3 sentinels accumulated
# before break fires).


# T2.5 / T2.5a (peer sentinel break NOT triggered) — folded into impl gate audit.
#
# Per I5 invariant: profile.name == "lead" gate in subagent.py:1668+ is the only
# code path that runs sentinel detection. Peer profiles (compute / modeler / analyst /
# verifier / web_search / planner) never even have `clarification_ask` in their
# tool registry allowlist — survived_tcs filter drops them at line 1420 before
# any iter loop body runs. Defense-in-depth gate is literal `profile.name == "lead"`
# string compare; runtime test is unreachable in prod.
#
# Cross-caller audit (r2 R-P0-2): verified that the gate string compare matches
# ONLY the lead profile (subagent_profiles.yaml + lead_orchestrator.py callers).
# No need for peer-profile UT case — registry filter prevents the test scenario
# from existing.


# ─────────────────────────────────────────────────────────────
# plan_propose must also terminate the loop immediately (116 real machine on 2026-08-15)
# Observed: the model actually called `plan_propose` with a good reason, but its sentinel was fed back as a normal
# tool result. The act-first model kept dispatching until iter=11; by loop completion,
# all work was done but no card appeared: exactly the tool description's forbidden halfway-through proposal.
# The clarification comments above already explained why; it was simply the only producer then.
# ─────────────────────────────────────────────────────────────
@pytest.fixture()
def _lead_can_plan_propose():
    """测试环境的 lead profile 白名单未必含 plan_propose —— 不补的话工具在
    **执行前**就被白名单挡掉，这条测试会为错误的理由绿/红（第一版就是这样）。"""
    from dataclasses import replace as _replace

    from bowhead_core.subagent import PROFILES

    prev = PROFILES.get("lead")
    if prev is not None:
        PROFILES["lead"] = _replace(prev, tool_whitelist=(*prev.tool_whitelist, "plan_propose", "subagent_spawn"))
    try:
        yield
    finally:
        if prev is not None:
            PROFILES["lead"] = prev


@pytest.mark.asyncio
async def test_lead_plan_propose_sentinel_breaks_before_dispatch(_lead_can_plan_propose) -> None:
    """调了 plan_propose 之后**不许再派活**：第二轮 LLM 不该被调到。

    突变：删掉 subagent.py 里的 plan_propose sentinel break → 第二轮 spawn 会执行、本条红。
    """
    from bowhead_core.agents.plan_propose_tool import SENTINEL_PLAN_PROPOSE

    router = _router(
        LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="plan_propose", arguments={"reason": "多域尽调"})]),
        # Without break, the model reaches this response and dispatches work, as it did on the real machine
        LLMResponse(content="", tool_calls=[ToolCall(id="c2", name="subagent_spawn", arguments={"profile": "compute", "task": "查数"})]),
        LLMResponse(content="不该走到这里", tool_calls=[]),
    )
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")

    executed: list[str] = []

    async def _stub_exec(tc: ToolCall, *args: Any, **kwargs: Any) -> ToolInvocation:
        executed.append(tc.name)
        result = {SENTINEL_PLAN_PROPOSE: True, "reason": tc.arguments.get("reason", "")} if tc.name == "plan_propose" else {"ok": True}
        return ToolInvocation(tool_name=tc.name, arguments=tc.arguments, result=result, error=None)

    runner._execute_tool = _stub_exec  # type: ignore[method-assign]

    result = await runner.run(
        profile_name="lead",
        task="做一份三方向尽调",
        parent_turn_id="t_pp_break",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx(),
    )

    assert executed == ["plan_propose"], f"出卡前不许再派活，实际执行了: {executed}"
    assert len(result.tool_invocations) == 1
    # Sentinel must remain in invocations; downstream `_lead_loop` uses it to enter the card path
    assert result.tool_invocations[0].result.get(SENTINEL_PLAN_PROPOSE) is True


@pytest.mark.asyncio
async def test_peer_profile_unaffected_by_plan_propose_break() -> None:
    """负对照：peer 拿不到这个工具，循环行为逐字不变（break 是 lead 面的事）。"""
    router = _router(
        LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="bi_query", arguments={"queries": ["x"]})]),
        LLMResponse(content="peer 正常收尾", tool_calls=[]),
    )
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
    executed: list[str] = []

    async def _stub_exec(tc: ToolCall, *args: Any, **kwargs: Any) -> ToolInvocation:
        executed.append(tc.name)
        return ToolInvocation(tool_name=tc.name, arguments=tc.arguments, result={"ok": True}, error=None)

    runner._execute_tool = _stub_exec  # type: ignore[method-assign]
    await runner.run(
        profile_name="compute",
        task="查数",
        parent_turn_id="t_pp_peer",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx(),
    )
    assert executed == ["bi_query"]
