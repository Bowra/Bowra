"""KB assignments stop retrieval without fabricating absence or discarding hits."""

from __future__ import annotations

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner
from bowhead_core.subagent_return_tool import register_subagent_return_tool
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from test_peer_empty_closeout import ScriptedRouter

from bowhead_router import LLMResponse, ToolCall, ToolSchema


def search_response(round_number, count=1):
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id=f"q{round_number}_{i}", name="kb_search", arguments={"query": f"topic {round_number} {i}"}) for i in range(count)],
    )


def closeout(*, malicious=False, summary="No supporting evidence was found; the unanswered facts remain unverified."):
    calls = [ToolCall(id="extra", name="kb_search", arguments={"query": "unrelated"})] if malicious else []
    if malicious:
        calls.append(ToolCall(id="table", name="kb_list_tables", arguments={}))
    return LLMResponse(
        content="",
        tool_calls=calls
        + [
            ToolCall(
                id="return",
                name="subagent_return",
                arguments={
                    "payload": {
                        "delivery_surface": "chat",
                        "exec_summary": summary,
                    }
                },
            )
        ],
    )


async def run_assignment(monkeypatch, responses, result_for_query):
    profile = SubagentProfile(
        name="kb-analyst",
        llm_agent_type="analyst",
        system_prompt="Retrieve and cite evidence.",
        tool_whitelist=("kb_search", "kb_list_tables", "subagent_return", "write_report"),
        max_iterations=20,
    )
    monkeypatch.setitem(PROFILES, profile.name, profile)
    registry = ToolRegistry()
    calls = []

    async def search(args, _ctx):
        calls.append(args["query"])
        return {"chunks": result_for_query(args["query"]), "total_candidates": 0}

    async def tables(_args, _ctx):
        calls.append("list_tables")
        return {"tables": []}

    async def write(_args, _ctx):
        return {"ok": True, "section_id": "sec_verified"}

    for name, handler in [("kb_search", search), ("kb_list_tables", tables), ("write_report", write)]:
        registry.register(ToolSpec(schema=ToolSchema(name=name, description=name, input_schema={"type": "object"}), handler=handler))
    register_subagent_return_tool(registry)
    router = ScriptedRouter(responses)
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name=profile.name,
        task="Find the requested facts.",
        parent_turn_id="t_kb_budget",
        parent_bus=TurnEventBus(),
        parent_ctx=ToolExecutionContext(
            workspace_id="ws_test", user_id="u_test", member_uid="m_test", turn_id="t_kb_budget", session_id="s_test", datarc=None
        ),
    )
    return result, calls, router


@pytest.mark.unit
async def test_no_hits_bound_parallel_queries_and_close_out(monkeypatch):
    result, calls, router = await run_assignment(monkeypatch, [search_response(i, 4) for i in range(3)] + [closeout(malicious=True)], lambda q: [])
    assert result.ok and len(calls) == 9 and len(router.requests) == 4
    assert "unrelated" not in calls and "list_tables" not in calls
    assert {tool.name for tool in router.requests[-1].tools} == {"subagent_return", "write_report"}
    assert any("do not prove the knowledge base is empty" in message.content for message in router.requests[-1].messages)


@pytest.mark.unit
async def test_meaningful_reformulation_can_find_evidence_and_reset_budget(monkeypatch):
    result, calls, router = await run_assignment(
        monkeypatch,
        [search_response(i) for i in range(6)] + [closeout(summary="Verified source fact; remaining subquestions are unverified.")],
        lambda q: [{"chunk_id": "found", "content": "Verified source fact"}] if q == "topic 2 0" else [],
    )
    assert result.ok and len(calls) == 6 and len(router.requests) == 7
    assert result.payload["exec_summary"].startswith("Verified source fact")
    assert any(inv.result.get("chunks") for inv in result.tool_invocations if inv.tool_name == "kb_search")


@pytest.mark.unit
async def test_duplicate_hits_do_not_sustain_unbounded_search(monkeypatch):
    result, calls, router = await run_assignment(
        monkeypatch, [search_response(i) for i in range(4)] + [closeout()], lambda q: [{"chunk_id": "same", "content": "Same fact"}]
    )
    assert result.ok and len(calls) == 4
    assert {tool.name for tool in router.requests[-1].tools} == {"subagent_return", "write_report"}


@pytest.mark.unit
async def test_repeated_query_is_not_reexecuted_and_report_can_finish(monkeypatch):
    report = LLMResponse(content="", tool_calls=[ToolCall(id="report", name="write_report", arguments={})])
    result, calls, router = await run_assignment(monkeypatch, [search_response(0) for _ in range(3)] + [report, closeout()], lambda q: [])
    assert result.ok and calls == ["topic 0 0"] and len(router.requests) == 5
    assert [inv.tool_name for inv in result.tool_invocations][-2:] == ["write_report", "subagent_return"]


@pytest.mark.unit
async def test_model_cannot_extend_exhausted_retrieval_by_ignoring_closeout(monkeypatch):
    result, calls, router = await run_assignment(monkeypatch, [search_response(i) for i in range(5)], lambda q: [])
    assert not result.ok and len(calls) == 3 and len(router.requests) == 5


@pytest.mark.unit
async def test_execution_receipt_counts_only_executed_searches(monkeypatch):
    from bowhead_core.kb_search_budget import retrieval_receipt

    result, calls, router = await run_assignment(monkeypatch, [search_response(0, 4), search_response(0, 4), closeout()], lambda q: [])
    assert len(calls) == 4
    assert retrieval_receipt(result.tool_invocations) == {"searches_executed": 4, "searches_succeeded": 4}
    assert any('"searches_executed": 4' in message.content for message in router.requests[-1].messages)


@pytest.mark.unit
@pytest.mark.parametrize("specialist_available", [True, False])
async def test_lead_delegates_kb_without_duplicate_search_but_retains_fallback(monkeypatch, specialist_available):
    from types import SimpleNamespace

    monkeypatch.setitem(
        PROFILES,
        "lead",
        SubagentProfile(
            name="lead",
            llm_agent_type="lead",
            system_prompt="Delegate KB tasks.",
            tool_whitelist=("kb_search", "subagent_spawn"),
            max_iterations=2,
        ),
    )
    calls = []
    registry = ToolRegistry()

    async def search(_args, _ctx):
        calls.append("search")
        return {"chunks": []}

    async def spawn(_args, _ctx):
        calls.append("spawn")
        return {"ok": True, "final_answer": "No relevant authorized evidence found."}

    for name, handler in [("kb_search", search), ("subagent_spawn", spawn)]:
        registry.register(ToolSpec(schema=ToolSchema(name=name, description=name, input_schema={"type": "object"}), handler=handler))
    router = ScriptedRouter(
        [
            LLMResponse(content="", tool_calls=[ToolCall(id="search", name="kb_search", arguments={"query": "Skill"})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="spawn", name="subagent_spawn", arguments={"profile": "kb-analyst"})]),
            LLMResponse(content="No relevant authorized evidence found."),
        ]
    )
    ctx = ToolExecutionContext(workspace_id="ws_test", user_id="u_test", member_uid="m_test", turn_id="t_lead_kb", session_id="s_test", datarc=None)
    ctx._actor_scoped_prompt_peers = (SimpleNamespace(name="kb-analyst"),) if specialist_available else ()
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name="lead",
        task="Find Skill documentation",
        parent_turn_id=ctx.turn_id,
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
    )
    assert result.ok
    assert calls == (["spawn"] if specialist_available else ["search", "spawn"])
    assert ("kb_search" in {schema.name for schema in router.requests[0].tools}) is not specialist_available
