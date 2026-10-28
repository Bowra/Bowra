"""Native document outcomes return to the shared finalizer without another Lead call."""

import pytest
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.document_chat_continuation import DocumentPlanningCheckpointRef
from bowhead_core.documents.continuation import DocumentPlanningFailure, DocumentPlanningPause, DocumentPlanningReview
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from test_document_tool import proposal
from test_document_tool import runtime as runtime
from test_subagent import _router

from bowhead_router import LLMResponse, ToolCall

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("kind", ["review", "pause", "incomplete", "failed"])
async def test_trusted_document_outcome_stops_lead_before_paraphrase(runtime, kind):
    registry, context, service = runtime
    checkpoint = DocumentPlanningCheckpointRef(job_id="djob_test", attempt_no=1, checkpoint_digest="b" * 64, source_hash="c" * 64)
    outcome = {
        "review": DocumentPlanningReview("djob_test", "a" * 64),
        "pause": DocumentPlanningPause("djob_test", checkpoint, "Which quarter should be used?"),
        "incomplete": DocumentPlanningFailure("incomplete", "djob_test", "document_planning_incomplete"),
        "failed": DocumentPlanningFailure("failed", "djob_test", "document_execution_interrupted"),
    }[kind]
    service.run_turn.return_value = outcome
    router = _router(
        LLMResponse(
            content="", tool_calls=[ToolCall(id="native_document", name="template_document_submit", arguments=proposal())], finish_reason="tool_calls"
        ),
        LLMResponse(content="This unsupported paraphrase must never be requested."),
    )
    runner = SubagentRunner(router=router, tool_registry=registry)
    runner._registry = AgentRegistry.bootstrap()
    result = await runner.run(
        profile_name="lead", task=proposal()["evidence"]["text"], parent_turn_id=context.turn_id, parent_bus=TurnEventBus(), parent_ctx=context
    )
    service.run_turn.assert_awaited_once()
    assert context.document_turn_state.planning_result is outcome
    assert router._registry._adapters["p1"]._script_index == 1
    assert result.final_answer == ""
    assert result.tool_invocations[-1].result["document_terminal"] is True


async def test_repairable_document_arguments_still_reach_legacy_receipt(runtime):
    registry, context, service = runtime
    invalid = proposal()
    del invalid["evidence"]
    router = _router(
        LLMResponse(
            content="", tool_calls=[ToolCall(id="invalid_document", name="template_document_submit", arguments=invalid)], finish_reason="tool_calls"
        ),
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="repaired_document", name="template_document_submit", arguments=proposal())],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="This unsupported paraphrase must never be requested."),
    )
    runner = SubagentRunner(router=router, tool_registry=registry)
    runner._registry = AgentRegistry.bootstrap()
    result = await runner.run(
        profile_name="lead", task=proposal()["evidence"]["text"], parent_turn_id=context.turn_id, parent_bus=TurnEventBus(), parent_ctx=context
    )
    service.submit.assert_awaited_once()
    assert context.document_turn_state.planning_result is None
    assert router._registry._adapters["p1"]._script_index == 2
    assert not result.tool_invocations[0].result.get("document_terminal")
    assert result.final_answer == result.tool_invocations[-1].result["message"]
