"""Real BI context selection cannot become an initial reference's final output.

The BI registry itself produces the sentinel, before any network call. Only
provider HTTP is replaced; the primary runner, exact grants and receipts run.
"""

from __future__ import annotations

import json

import httpx2
import pytest
import respx
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import build_default_registry
from test_skill_model_invocation import adapter_for, router_for
from test_skill_reference_runtime import claimed_case as _claimed_case
from test_skill_reference_runtime import receiver_case as _receiver_case
from test_skill_reference_runtime import rows
from test_skill_reference_runtime import runtime_case as _runtime_case
from test_skill_reference_runtime import start_case as _start_case

claimed_case, receiver_case, runtime_case, start_case = _claimed_case, _receiver_case, _runtime_case, _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
    pytest.mark.parametrize("claimed_case", ["canonical_input", "agent_baseline"], indirect=True),
]


def workspace_question(_request):
    return httpx2.Response(
        200,
        json={
            "id": "bi-context-question",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "I will check the data source before calculating.",
                        "tool_calls": [
                            {
                                "id": "select-context",
                                "type": "function",
                                "function": {"name": "bi_query", "arguments": json.dumps({"query": "revenue"})},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        },
    )


def bind_ambiguous_workspace(ctx):
    # The real registry returns its connector sentinel before touching a
    # client; this object deliberately has no network methods to fall back to.
    ctx.datarc = object()
    ctx.accessible_datarc_workspaces = ({"id": "project_a", "name": "A"}, {"id": "project_b", "name": "B"})
    ctx.active_datarc_project = None


async def test_initial_reference_cannot_record_a_bi_question_as_completed(runtime_case):
    case = runtime_case
    bind_ambiguous_workspace(case.ctx)
    adapter = adapter_for("openai")
    runner = SubagentRunner(router=router_for(adapter), tool_registry=build_default_registry(), team_subagent_store=case.users)
    bus = TurnEventBus()
    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
            result = await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=bus)
            assert not result.ok and result.error == "reference_continuation_unavailable"
            assert result.final_answer == "" and result.payload is None
            assert result.tokens_input == 100 and result.tokens_output == 20
            assert result.tool_invocations[0].result["clarification"]["kind"] == "tool_context_select"
            assert route.call_count == 1
        assert [row["kind"] for row in rows(case, "chat_reference_evidence")] == ["delegate_started"]
        assert rows(case, "turn_agent_execution")[0]["status"] == "running"
        assert [row["status"] for row in rows(case, "chat_reference_model_invocations")] == ["completed"]
        assert case.ctx.active_datarc_project is None
    finally:
        await adapter.close()
