"""Agent run limits inside SubagentRunner: steps, shared tool-call budgets, time and credit."""

from __future__ import annotations

import time

import pytest
from bowhead_core.agents._lead_context import reset_turn_deadline, set_turn_deadline
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner
from bowhead_core.subagent_return_tool import register_subagent_return_tool
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_router.types import LLMChunk, LLMRequest, LLMResponse, ToolCall, ToolSchema

from bowhead_core import agent_run_limits as rl

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def runtime_policy(monkeypatch):
    rl.get_runtime_controls.cache_clear()
    yield
    rl.get_runtime_controls.cache_clear()


class _LoopingRouter:
    """Calls ``read`` with a fresh or repeated argument on every step, then returns."""

    def __init__(self, *, steps_before_return: int, distinct: bool) -> None:
        self.steps_before_return = steps_before_return
        self.distinct = distinct
        self.requests: list[LLMRequest] = []

    def _next(self) -> LLMResponse:
        step = len(self.requests)
        if self.requests[-1].tool_choice == "none":
            return LLMResponse(content="Done: read keys 1-4, values verified.\nRemaining: read the other keys.")
        if step > self.steps_before_return:
            payload = {"delivery_surface": "chat", "exec_summary": f"Done after {step} steps."}
            return LLMResponse(content="", tool_calls=[ToolCall(id=f"r{step}", name="subagent_return", arguments={"payload": payload})])
        key = str(step) if self.distinct else "same"
        return LLMResponse(content="", tool_calls=[ToolCall(id=f"c{step}", name="read", arguments={"key": key})])

    async def invoke(self, _agent_type: str, request: LLMRequest, **_kwargs: object) -> LLMResponse:
        self.requests.append(request)
        return self._next()

    async def stream(self, _agent_type: str, request: LLMRequest, **_kwargs: object):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        response = self._next()
        for call in response.tool_calls:
            yield LLMChunk(tool_call_delta=call)
        yield LLMChunk(finish_reason="tool_use")


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def read(arguments: dict[str, object], _ctx: ToolExecutionContext) -> dict[str, object]:
        return {"ok": True, "value": arguments.get("key")}

    registry.register(ToolSpec(schema=ToolSchema(name="read", description="Read", input_schema={"type": "object"}), handler=read))
    register_subagent_return_tool(registry)
    return registry


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    router: _LoopingRouter,
    limits: rl.AgentRunLimits | None,
    *,
    max_iterations: int = 40,
    profile_name: str = "run-limits-test",
    **run_options,
):
    profile = SubagentProfile(
        name=profile_name,
        llm_agent_type="analyst",
        tool_whitelist=("read", "subagent_return"),
        system_prompt="Read and return.",
        max_iterations=max_iterations,
        max_output_tokens=2000,
    )
    monkeypatch.setitem(PROFILES, profile.name, profile)
    limits_token = rl.set_run_limits(limits)
    budget_token = rl.set_tool_call_budget(rl.TurnToolCallBudget(limits.tool_call_limit) if limits else None)
    deadline_token = set_turn_deadline(time.monotonic() + (limits.turn_time_limit_s if limits else 1800))
    try:
        return await SubagentRunner(router=router, tool_registry=_registry()).run(
            profile_name=profile.name,
            task="Read until done",
            parent_turn_id="t_run_limits",
            parent_bus=TurnEventBus(),
            parent_ctx=ToolExecutionContext(
                workspace_id="ws_test",
                member_uid="m_test",
                user_id="u_test",
                turn_id="t_run_limits",
                session_id="s_test",
                datarc=None,  # type: ignore[arg-type]
            ),
            **run_options,
        )
    finally:
        reset_turn_deadline(deadline_token)
        rl.reset_tool_call_budget(budget_token)
        rl.reset_run_limits(limits_token)


async def test_unlimited_steps_run_past_the_old_force_bail_and_manifest_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _LoopingRouter(steps_before_return=45, distinct=True)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(step_limit=None, tool_call_limit=1000), max_iterations=10)
    assert result.ok, result.error
    assert len(router.requests) == 46  # 45 reads, then the return


async def test_tool_calls_share_one_turn_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _LoopingRouter(steps_before_return=6, distinct=True)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(step_limit=None, tool_call_limit=3))
    refused = [inv for inv in result.tool_invocations if inv.error and inv.error.startswith("tool_call_limit")]
    assert len(refused) == 1  # the first refused call ends the run at the next step
    assert (result.completion, result.stop_reason) == ("partial", "tool_call_limit")


async def test_subagent_return_is_never_refused_by_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _LoopingRouter(steps_before_return=2, distinct=True)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(step_limit=None, tool_call_limit=2))
    assert result.ok and result.completion == "complete"


@pytest.mark.parametrize("call_limit", [None, 900])
async def test_actual_model_call_deadline_uses_the_team_policy(monkeypatch, call_limit):
    from bowhead_core import subagent

    wait_for = subagent.asyncio.wait_for
    deadlines = []

    async def record(awaitable, *, timeout):
        deadlines.append(timeout)
        return await wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(subagent.asyncio, "wait_for", record)
    router = _LoopingRouter(steps_before_return=0, distinct=True)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(model_call_time_limit_s=call_limit))
    assert result.ok
    assert any(800 < deadline <= (900 if call_limit else 1500) for deadline in deadlines)
    assert not any(299 <= deadline <= 300 for deadline in deadlines)


async def test_running_out_of_credit_stops_with_partial_work(monkeypatch: pytest.MonkeyPatch) -> None:
    async def balances():
        return 0, 1000

    router = _LoopingRouter(steps_before_return=20, distinct=True)
    token = rl.set_credit_gate(rl.TurnCreditGate(balances, lambda: 0))
    try:
        result = await _run(monkeypatch, router, rl.AgentRunLimits(step_limit=None, tool_call_limit=1000))
    finally:
        rl.reset_credit_gate(token)
    assert (result.completion, result.stop_reason) == ("partial", "budget")
    assert "available credit" in router.requests[-1].messages[-1].content


@pytest.mark.parametrize("limit", [1, 3, 5])
async def test_exact_decision_ceiling_keeps_last_tool_batch_without_extra_summary(monkeypatch, limit):
    router = _LoopingRouter(steps_before_return=20, distinct=True)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(step_limit=limit))
    assert (result.completion, result.stop_reason) == ("partial", "step_limit")
    assert len(router.requests) == limit
    assert len(result.tool_invocations) == limit
    assert result.model_turn_count == limit
    assert result.partial_done


@pytest.mark.parametrize("limit", [1, 3])
async def test_natural_completion_on_last_admitted_decision_wins(monkeypatch, limit):
    router = _LoopingRouter(steps_before_return=limit - 1, distinct=True)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(step_limit=limit))
    assert result.ok, result.error
    assert len(router.requests) == limit


async def test_system_ceiling_applies_to_an_unlimited_team(monkeypatch):
    monkeypatch.setenv("BOWHEAD_AGENT_MAX_STEPS", "2")
    router = _LoopingRouter(steps_before_return=20, distinct=True)
    result = await _run(monkeypatch, router, rl.AgentRunLimits())
    assert result.stop_reason == "step_limit"
    assert len(router.requests) == 2


async def test_forced_tools_do_not_consume_model_steps(monkeypatch):
    router = _LoopingRouter(steps_before_return=0, distinct=True)
    result = await _run(
        monkeypatch,
        router,
        rl.AgentRunLimits(step_limit=1),
        forced_tool_calls=[ToolCall(id="forced", name="read", arguments={"key": "original"})],
    )
    assert result.ok, result.error
    assert len(router.requests) == 1
    assert [inv.tool_name for inv in result.tool_invocations] == ["read", "subagent_return"]


async def test_repeated_calls_are_advisory_with_runtime_controls(monkeypatch):
    router = _LoopingRouter(steps_before_return=35, distinct=False)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(tool_call_limit=1000), max_iterations=12)
    assert result.ok, result.error
    assert len(router.requests) == 36


async def test_lead_uses_the_shared_ceiling_beyond_twelve(monkeypatch):
    router = _LoopingRouter(steps_before_return=30, distinct=True)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(step_limit=14), max_iterations=12, profile_name="lead")
    assert result.stop_reason == "step_limit", result.error
    assert len(router.requests) == 14


async def test_empty_last_decision_reports_step_exhaustion(monkeypatch):
    class EmptyRouter(_LoopingRouter):
        def _next(self):
            return LLMResponse(content="")

    router = EmptyRouter(steps_before_return=0, distinct=True)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(step_limit=1), profile_name="lead")
    assert result.stop_reason == "step_limit", result.error
    assert len(router.requests) == 1
