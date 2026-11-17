"""Mid-run steering inside SubagentRunner: who asks the inbox, and when."""

from __future__ import annotations

import time

import pytest
from bowhead_core.agents._lead_context import reset_turn_deadline, set_turn_deadline
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext
from test_agent_run_limits_runtime import _LoopingRouter, _registry

from bowhead_core import agent_run_limits as rl
from bowhead_router import LLMChunk, LLMResponse

pytestmark = pytest.mark.unit

INSTRUCTION = "Use green bars instead."


@pytest.fixture(autouse=True)
def runtime_policy():
    rl.get_runtime_controls.cache_clear()
    yield
    rl.get_runtime_controls.cache_clear()


class Inbox:
    """Records every step that asks for instructions and hands out what is queued for that step."""

    def __init__(self, queued: dict[int, list[str]]) -> None:
        self.queued = queued
        self.asked: list[int] = []

    async def take(self, ctx: ToolExecutionContext, *, iteration: int, while_waiting: bool = False) -> list[str]:
        self.asked.append(iteration)
        return self.queued.pop(iteration, [])


class AnsweringRouter:
    """Answers in text on every step, with the scripted answers in order."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.requests: list = []

    async def invoke(self, _agent_type: str, request, **_kwargs: object) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(content=self.answers[len(self.requests) - 1])

    async def stream(self, _agent_type: str, request, **_kwargs: object):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        yield LLMChunk(text=self.answers[len(self.requests) - 1])
        yield LLMChunk(finish_reason="stop")


async def run(monkeypatch: pytest.MonkeyPatch, router, inbox: Inbox, *, profile_name: str, budget: rl.TurnModelCallBudget, final_answer_gate=None):
    profile = SubagentProfile(
        name=profile_name,
        llm_agent_type="analyst",
        tool_whitelist=("read", "subagent_return"),
        system_prompt="Read and return.",
        max_iterations=40,
        max_output_tokens=2000,
    )
    monkeypatch.setitem(PROFILES, profile.name, profile)
    limits = rl.AgentRunLimits(model_call_limit=budget.limit)
    limits_token = rl.set_run_limits(limits)
    tool_token = rl.set_tool_call_budget(rl.TurnToolCallBudget(limits.tool_call_limit))
    model_token = rl.set_model_call_budget(budget)
    deadline_token = set_turn_deadline(time.monotonic() + limits.turn_time_limit_s)
    context = ToolExecutionContext(
        workspace_id="ws_test",
        member_uid="m_test",
        user_id="u_test",
        turn_id="t_mid_turn_steer",
        session_id="s_test",
        datarc=None,  # type: ignore[arg-type]
        steer_inbox=inbox,
    )
    if final_answer_gate is not None:
        context._final_answer_gate = final_answer_gate  # type: ignore[attr-defined]
    try:
        return await SubagentRunner(router=router, tool_registry=_registry()).run(
            profile_name=profile.name,
            task="Read until done",
            parent_turn_id="t_mid_turn_steer",
            parent_bus=TurnEventBus(),
            parent_ctx=context,
        )
    finally:
        reset_turn_deadline(deadline_token)
        rl.reset_model_call_budget(model_token)
        rl.reset_tool_call_budget(tool_token)
        rl.reset_run_limits(limits_token)


def user_contents(request) -> list[str]:
    return [message.content for message in request.messages if message.role == "user" and isinstance(message.content, str)]


async def test_lead_takes_an_instruction_before_its_step_and_not_once_it_must_answer_now(monkeypatch: pytest.MonkeyPatch) -> None:
    # Three of six model calls are spent: after one more step helpers cannot decide, so the Lead answers now.
    router = _LoopingRouter(steps_before_return=30, distinct=True)
    inbox = Inbox({0: [INSTRUCTION], 1: ["Add a title."]})

    result = await run(monkeypatch, router, inbox, profile_name="lead", budget=rl.TurnModelCallBudget(6, used=3))

    assert [request.tool_choice for request in router.requests] == ["auto", "none"]
    assert INSTRUCTION in user_contents(router.requests[0]), "the instruction joins the transcript before the step's model call, in the user's words"
    assert inbox.asked == [0], "a Lead told to answer now takes nothing; the instruction runs as a follow-up Turn"
    assert "Add a title." not in "\n".join(user_contents(router.requests[1]))
    assert result.ok, result.error


async def test_a_helper_never_asks_the_inbox(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _LoopingRouter(steps_before_return=2, distinct=True)
    inbox = Inbox({0: [INSTRUCTION], 1: [INSTRUCTION]})

    result = await run(monkeypatch, router, inbox, profile_name="mid-turn-steer-helper", budget=rl.TurnModelCallBudget(20))

    assert result.ok, result.error
    assert len(router.requests) == 3
    assert inbox.asked == [], "only the Lead takes the user's instructions"
    assert all(INSTRUCTION not in "\n".join(user_contents(request)) for request in router.requests)


async def test_a_lead_correcting_its_written_answer_takes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # 116 the regression case: the answer was sent back for a figure without evidence, the instruction was taken at
    # the correction step, and the Lead answered the instruction alone. The written answer was replaced.
    router = AnsweringRouter(["A long answer with one unsupported figure.", "The long answer, corrected."])
    inbox = Inbox({1: ["Add one sentence in English at the end."]})
    sent_back: list[str] = []

    async def gate(text: str, _profile: str, _invocations: list):
        if sent_back:
            return None
        sent_back.append(text)
        return "Remove the figure that has no evidence and answer again."

    result = await run(monkeypatch, router, inbox, profile_name="lead", budget=rl.TurnModelCallBudget(20), final_answer_gate=gate)

    assert result.ok, result.error
    assert sent_back == ["A long answer with one unsupported figure."]
    assert len(router.requests) == 2
    assert inbox.asked == [0], "an instruction that arrives while the answer is written or corrected runs as a follow-up Turn"
    assert "Add one sentence in English at the end." not in "\n".join(user_contents(router.requests[1]))
    assert result.final_answer == "The long answer, corrected."
