"""A helper that worked and then answered in prose is told once that it has returned nothing."""

import pytest
from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import PROFILES, UNRETURNED_WORK_NOTE, SubagentRunner
from bowhead_core.subagent_return_tool import register_subagent_return_tool
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from test_subagent import _router

from bowhead_router import LLMResponse, ToolCall, ToolSchema

pytestmark = [pytest.mark.integration]


async def _run(monkeypatch, *script):
    monkeypatch.setitem(PROFILES, "compute", manifest_to_profile(AgentRegistry.bootstrap().get("compute")))
    registry = ToolRegistry()

    async def listed(_args, _ctx):
        return [{"name": "pptx", "description": "decks"}]

    registry.register(ToolSpec(schema=ToolSchema(name="skill_list", description="list", input_schema={"type": "object"}), handler=listed))
    register_subagent_return_tool(registry)
    router = _router(
        LLMResponse(content="", tool_calls=[ToolCall(id="listed", name="skill_list", arguments={})], finish_reason="tool_calls"),
        *script,
    )
    adapter = router._registry._adapters["p1"]
    invoke = adapter.invoke
    requests = []

    async def capture(request):
        requests.append(request)
        return await invoke(request)

    monkeypatch.setattr(adapter, "invoke", capture)
    context = ToolExecutionContext(user_id="u1", member_uid="u1", workspace_id="ws_test", turn_id="t_note", session_id="s_note", datarc=None)
    result = await SubagentRunner(router, registry).run(
        profile_name="compute", task="List the Skills and report them.", parent_ctx=context, parent_bus=TurnEventBus(), parent_turn_id=context.turn_id
    )
    return result, requests


def _notes(request) -> int:
    return sum(message.content == UNRETURNED_WORK_NOTE for message in request.messages)


async def test_prose_after_work_gets_the_facts_once_with_tools_still_offered(monkeypatch):
    prose = LLMResponse(content="Here is a summary of what I looked at.", finish_reason="stop")
    _result, requests = await _run(monkeypatch, prose, prose)
    assert [_notes(request) for request in requests] == [0, 0, 1]
    # The note states facts and withholds nothing: the same Tools are offered again.
    assert {tool.name for tool in requests[2].tools} == {tool.name for tool in requests[1].tools} >= {"skill_list", "subagent_return"}


async def test_a_second_prose_ending_is_accepted_as_before(monkeypatch):
    prose = LLMResponse(content="Still only a summary.", finish_reason="stop")
    result, requests = await _run(monkeypatch, prose, prose)
    assert len(requests) == 3 and _notes(requests[-1]) == 1
    assert result.payload is None and result.final_answer.startswith("Still only a summary.")
