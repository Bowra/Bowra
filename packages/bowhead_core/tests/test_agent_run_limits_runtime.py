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


def _registry(*, changing: bool = False, status_poll: bool = False) -> ToolRegistry:
    """``read`` returns its key; with ``changing`` every call also returns a new reading."""
    registry = ToolRegistry()
    readings = iter(range(1, 10_000))

    async def read(arguments: dict[str, object], _ctx: ToolExecutionContext) -> dict[str, object]:
        return {"ok": True, "value": arguments.get("key"), **({"reading": next(readings)} if changing else {})}

    registry.register(
        ToolSpec(schema=ToolSchema(name="read", description="Read", input_schema={"type": "object"}), handler=read, status_poll=status_poll)
    )
    register_subagent_return_tool(registry)
    return registry


class _NoCanvasStore:
    """A session without a Canvas: the reference refresh still adds a `tl` reference per tool call."""

    async def get_canvas_by_session(self, _session_id: str) -> None:
        return None


class _NoArtifactStore:
    async def list_for_session(self, *_args: object, **_kwargs: object) -> list[object]:
        return []


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    router: _LoopingRouter,
    limits: rl.AgentRunLimits | None,
    *,
    max_iterations: int = 40,
    profile_name: str = "run-limits-test",
    model_budget: rl.TurnModelCallBudget | None = None,
    registry: ToolRegistry | None = None,
    with_reference_refresh: bool = False,
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
    if model_budget is None and limits is not None:
        model_budget = rl.TurnModelCallBudget(limits.model_call_limit)
    model_budget_token = rl.set_model_call_budget(model_budget)
    deadline_token = set_turn_deadline(time.monotonic() + (limits.turn_time_limit_s if limits else 1800))
    try:
        return await SubagentRunner(router=router, tool_registry=registry or _registry()).run(
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
                **({"canvas_store": _NoCanvasStore(), "artifact_store": _NoArtifactStore()} if with_reference_refresh else {}),
            ),
            **run_options,
        )
    finally:
        reset_turn_deadline(deadline_token)
        rl.reset_model_call_budget(model_budget_token)
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


def _tool_messages(request: LLMRequest) -> list[str]:
    return [str(message.content) for message in request.messages if message.role == "tool"]


async def test_identical_repeats_get_notices_then_are_refused_and_wrap_up(monkeypatch):
    """The same call with the same result: notices at 3 and 5, not run from the 8th, and the
    refused-decision bound ends the run as partial (tech design §3.3–§3.4, AR-06 as amended)."""
    router = _LoopingRouter(steps_before_return=35, distinct=False)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(tool_call_limit=1000), max_iterations=12)
    assert (result.completion, result.stop_reason) == ("partial", "no_progress"), result.error
    refusal_decisions = rl.REFUSED_DECISION_LIMIT
    executed = rl.REPEAT_REFUSE_FROM - 1
    assert [inv.tool_name for inv in result.tool_invocations] == ["read"] * executed
    assert len(router.requests) == executed + refusal_decisions + 1  # then the tool-free summary
    assert router.requests[-1].tool_choice == "none"
    final_tools = _tool_messages(router.requests[-1])
    notices = [message for message in final_tools if "[repeat notice]" in message]
    assert [notice.split("This is call ")[1].split(" ")[0] for notice in notices] == ["3", "5"]
    refusals = [message for message in final_tools if message.startswith("not_run:")]
    assert [refusal.split("this is call ")[1].split(" ")[0] for refusal in refusals] == ["8", "9", "10"]


async def test_identical_calls_with_changing_results_stay_advisory(monkeypatch):
    """AR-06 for useful work: the same arguments with a new result each time are never refused;
    the corrected progress counter still nudges once (tool-log references are not progress)."""
    router = _LoopingRouter(steps_before_return=35, distinct=False)
    result = await _run(
        monkeypatch,
        router,
        rl.AgentRunLimits(tool_call_limit=1000),
        max_iterations=12,
        registry=_registry(changing=True),
        with_reference_refresh=True,
    )
    assert result.ok, result.error
    assert len(router.requests) == 36
    final_tools = _tool_messages(router.requests[-1])
    assert not [message for message in final_tools if "[repeat notice]" in message or message.startswith("not_run:")]
    nudges = [m for m in router.requests[-1].messages if m.role == "user" and "steps produced no new results" in str(m.content)]
    assert len(nudges) == 1


async def test_status_poll_tool_is_never_refused(monkeypatch):
    router = _LoopingRouter(steps_before_return=20, distinct=False)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(tool_call_limit=1000), registry=_registry(status_poll=True))
    assert result.ok, result.error
    assert len(router.requests) == 21
    assert not [message for message in _tool_messages(router.requests[-1]) if "[repeat notice]" in message or message.startswith("not_run:")]


async def test_lead_uses_the_shared_ceiling_beyond_twelve(monkeypatch):
    router = _LoopingRouter(steps_before_return=30, distinct=True)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(step_limit=14), max_iterations=12, profile_name="lead")
    assert result.stop_reason == "step_limit", result.error
    assert len(router.requests) == 14


async def test_helper_stops_at_the_turn_model_call_limit_before_lead_reserve(monkeypatch):
    router = _LoopingRouter(steps_before_return=20, distinct=True)
    budget = rl.TurnModelCallBudget(6)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(model_call_limit=6), model_budget=budget)
    assert (result.completion, result.stop_reason, result.error) == ("partial", "model_call_limit", "run_limit_model_call_limit")
    # Two decisions stay with Lead, and the stop makes no wrap-up model call.
    assert len(router.requests) == 4 and budget.used == 4
    assert all(request.tool_choice != "none" for request in router.requests)
    assert len(result.tool_invocations) == 4 and result.partial_done
    assert result.step_usage == {"used": 4, "limit": 6}


async def test_helper_runs_share_one_turn_model_call_budget(monkeypatch):
    budget = rl.TurnModelCallBudget(10)
    first = await _run(monkeypatch, _LoopingRouter(steps_before_return=2, distinct=True), rl.AgentRunLimits(model_call_limit=10), model_budget=budget)
    assert first.ok and budget.used == 3
    second = _LoopingRouter(steps_before_return=20, distinct=True)
    result = await _run(monkeypatch, second, rl.AgentRunLimits(model_call_limit=10), model_budget=budget)
    assert result.stop_reason == "model_call_limit" and len(second.requests) == 5


async def test_unlimited_model_calls_do_not_stop_a_run(monkeypatch):
    router = _LoopingRouter(steps_before_return=12, distinct=True)
    budget = rl.TurnModelCallBudget(None)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(model_call_limit=None), model_budget=budget)
    assert result.ok, result.error
    assert budget.used == len(router.requests) == 13


async def test_lead_answers_with_its_reserved_model_calls(monkeypatch):
    router = _LoopingRouter(steps_before_return=30, distinct=True)
    budget = rl.TurnModelCallBudget(6, used=3)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(model_call_limit=6), profile_name="lead", model_budget=budget)
    # One read, then helpers are out of decisions and Lead answers without Tools.
    assert [request.tool_choice for request in router.requests] == ["auto", "none"]
    assert "Team limit of 6 model calls" in router.requests[-1].messages[-1].content
    assert result.ok, result.error
    assert result.final_answer.startswith("Done: read keys 1-4")
    assert budget.used == 5


async def test_lead_without_an_answer_stops_at_the_model_call_limit(monkeypatch):
    class EmptyRouter(_LoopingRouter):
        def _next(self):
            return LLMResponse(content="")

    router = EmptyRouter(steps_before_return=0, distinct=True)
    budget = rl.TurnModelCallBudget(6, used=4)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(model_call_limit=6), profile_name="lead", model_budget=budget)
    assert (result.stop_reason, result.error) == ("model_call_limit", "run_limit_model_call_limit")
    assert len(router.requests) == 1 and router.requests[0].tool_choice == "none"


async def test_empty_last_decision_reports_step_exhaustion(monkeypatch):
    class EmptyRouter(_LoopingRouter):
        def _next(self):
            return LLMResponse(content="")

    router = EmptyRouter(steps_before_return=0, distinct=True)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(step_limit=1), profile_name="lead")
    assert result.stop_reason == "step_limit", result.error
    assert len(router.requests) == 1


async def test_repeatedly_refused_calls_wrap_up_as_partial(monkeypatch):
    """Refused calls never reach the executed-failure counters and runtime controls have no
    step stop; consecutive fully refused decisions are the bounded policy (the original regression)."""

    class RefusedRouter(_LoopingRouter):
        def _next(self):
            if self.requests[-1].tool_choice == "none":
                return super()._next()
            step = len(self.requests)
            # `write` is outside the helper's whitelist: refused before execution every time.
            return LLMResponse(content="", tool_calls=[ToolCall(id=f"w{step}", name="write", arguments={"key": str(step)})])

    router = RefusedRouter(steps_before_return=99, distinct=True)
    result = await _run(monkeypatch, router, rl.AgentRunLimits(tool_call_limit=1000))
    assert (result.completion, result.stop_reason) == ("partial", "no_progress"), result.error
    assert len(router.requests) == rl.REFUSED_DECISION_LIMIT + 1  # the refused decisions, then the tool-free summary
    assert router.requests[-1].tool_choice == "none"
    assert result.partial_done and result.partial_done.startswith("read keys 1-4")
