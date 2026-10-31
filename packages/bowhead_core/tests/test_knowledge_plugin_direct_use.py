"""Shared KB reading keeps the task owner, authority and continuation budget intact."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.kb_search_budget import KnowledgeReadState, KnowledgeSearchBudget, admit_knowledge_reading
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner
from bowhead_core.subagent_return_tool import register_subagent_return_tool
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_router.types import LLMResponse, ToolCall, ToolSchema
from test_kb_search_budget import closeout, search_response
from test_peer_empty_closeout import ScriptedRouter
from test_subagent_resume_store import _db, _fernet  # noqa: F401

from bowhead_core import subagent_resume as sr

pytestmark = pytest.mark.unit


def context(**kwargs):
    return ToolExecutionContext(workspace_id="ws_1", user_id="u_1", member_uid="m", turn_id="t_1", session_id="s_1", datarc=None, **kwargs)


async def run_shared(
    monkeypatch, responses, *, profile_name="business-test", ctx=None, allow_search=True, resume_grant=None, tool_deny=frozenset(), chunks=None
):
    profile = SubagentProfile(
        name=profile_name,
        llm_agent_type="lead" if profile_name == "lead" else "analyst",
        system_prompt="Complete the business task.",
        tool_whitelist=("calculate", "kb_list_tables", "subagent_return") + (("kb_search",) if allow_search else ()),
        max_iterations=12,
    )
    monkeypatch.setitem(PROFILES, profile_name, profile)
    calls = []
    registry = ToolRegistry()

    async def search(args, _ctx):
        calls.append(("kb_search", args))
        return {"chunks": chunks or []}

    async def calculate(args, _ctx):
        calls.append(("calculate", args))
        return {"value": 42}

    async def tables(args, _ctx):
        calls.append(("kb_list_tables", args))
        return {"tables": []}

    for name, handler in [("kb_search", search), ("calculate", calculate), ("kb_list_tables", tables)]:
        registry.register(ToolSpec(schema=ToolSchema(name=name, description=name, input_schema={"type": "object"}), handler=handler))
    register_subagent_return_tool(registry)
    ctx = ctx or context(knowledge_direct_use=True)
    ctx._actor_scoped_prompt_peers = (SimpleNamespace(name="kb-analyst"),)
    router = ScriptedRouter(responses)
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name=profile_name,
        task="Read evidence and calculate.",
        parent_turn_id=ctx.turn_id,
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
        resume_grant=resume_grant,
        tool_deny=tool_deny,
    )
    return result, calls, router


def calculate_response():
    return LLMResponse(content="", tool_calls=[ToolCall(id="calc", name="calculate", arguments={})])


async def test_lead_reads_with_specialist_available_and_keeps_plain_answer(monkeypatch):
    result, calls, router = await run_shared(
        monkeypatch,
        [search_response(i) for i in range(3)]
        + [calculate_response(), LLMResponse(content="The calculation is 42; source evidence is unavailable.")],
        profile_name="lead",
    )
    assert result.ok and result.final_answer.startswith("The calculation is 42")
    assert [name for name, _ in calls] == ["kb_search"] * 3 + ["calculate"]
    assert "kb_search" in {tool.name for tool in router.requests[0].tools}
    assert "kb_search" not in {tool.name for tool in router.requests[-1].tools}
    assert "calculate" in {tool.name for tool in router.requests[-1].tools}
    assert "<knowledge_reading>" in router.requests[0].system


async def test_business_agent_keeps_calculation_and_tables_after_search_exhaustion(monkeypatch):
    attempted = calculate_response()
    attempted.tool_calls.extend(
        [ToolCall(id="extra", name="kb_search", arguments={"query": "another"}), ToolCall(id="table", name="kb_list_tables", arguments={})]
    )
    result, calls, router = await run_shared(monkeypatch, [search_response(i, 4) for i in range(3)] + [attempted, closeout()])
    assert result.ok
    assert [name for name, _ in calls] == ["kb_search"] * 9 + ["calculate", "kb_list_tables"]
    assert {tool.name for tool in router.requests[-1].tools} == {"calculate", "kb_list_tables", "subagent_return"}
    assert any('"searches_executed": 9' in message.content for message in router.requests[-1].messages)


async def test_shared_policy_cannot_grant_an_unassigned_tool(monkeypatch):
    result, calls, router = await run_shared(monkeypatch, [search_response(0), calculate_response(), closeout()], allow_search=False)
    assert result.ok and calls == [("calculate", {})]
    assert all("kb_search" not in {tool.name for tool in request.tools} for request in router.requests)


def test_admission_uses_retained_policy_and_keeps_other_entry_paths_legacy(monkeypatch):
    monkeypatch.setenv("FEATURE_KNOWLEDGE_PLUGIN_DIRECT_USE", "1")
    fresh = context(ordinary_lead_state=SimpleNamespace(checkpoint=None))
    admit_knowledge_reading(fresh, resuming=False)
    assert fresh.knowledge_direct_use
    monkeypatch.setenv("FEATURE_KNOWLEDGE_PLUGIN_DIRECT_USE", "0")
    assert fresh.knowledge_direct_use
    for state, expected in [(None, False), (KnowledgeReadState(), True)]:
        fresh.ordinary_lead_state.checkpoint = SimpleNamespace(knowledge_read=state)
        admit_knowledge_reading(fresh, resuming=True)
        assert fresh.knowledge_direct_use is expected
    bare = context()
    monkeypatch.setenv("FEATURE_KNOWLEDGE_PLUGIN_DIRECT_USE", "1")
    admit_knowledge_reading(bare, resuming=False)
    assert not bare.knowledge_direct_use


@pytest.mark.parametrize("shared", [True, False])
async def test_helper_continue_uses_saved_policy_and_budget_over_current_flag(tmp_path, monkeypatch, shared):
    from bowhead_core.session_store import SessionStore

    db_path = await _db(tmp_path)
    ctx = context(team_id="team_1", session_store=SessionStore(db_path), knowledge_direct_use=shared)
    first, calls, _ = await run_shared(monkeypatch, [search_response(i) for i in range(3)] + [closeout()], ctx=ctx)
    assert first.ok and first.resume_checkpoint_id
    checkpoint = await sr.claim(db_path, checkpoint_id=first.resume_checkpoint_id, session_id="s_1", user_id="u_1", turn_id="t_2")
    material = sr.open_material(checkpoint, db_path=db_path)
    assert (material.knowledge_read is not None) is shared
    resumed_ctx = replace(ctx, turn_id="t_2", knowledge_direct_use=not shared)
    result, resumed_calls, router = await run_shared(
        monkeypatch,
        [search_response(4), calculate_response(), closeout()],
        ctx=resumed_ctx,
        resume_grant=sr.ResumeGrant(checkpoint=checkpoint, material=material, instruction="Complete the calculation."),
    )
    assert result.ok
    assert resumed_calls == ([("calculate", {})] if shared else [("kb_search", {"query": "topic 4 0"}), ("calculate", {})])
    assert ("kb_search" in {tool.name for tool in router.requests[0].tools}) is not shared
    if shared:
        assert material.knowledge_read.empty_rounds == 3
        assert material.knowledge_read.searches_executed == len(calls)


def test_saved_budget_keeps_duplicate_queries_and_evidence():
    budget = KnowledgeSearchBudget()
    assert budget.admit({"query": " Topic "})
    invocation = SimpleNamespace(tool_name="kb_search", error=None, result={"chunks": [{"chunk_id": "chunk_1"}]})
    budget.observe([invocation], search_requested=True, shared=True)
    restored = KnowledgeSearchBudget.restore(budget.snapshot())
    assert not restored.admit({"query": "topic"})
    restored.observe([invocation], search_requested=True, shared=True)
    assert restored.empty_rounds == 1
    assert restored.receipt() == {"searches_executed": 2, "searches_succeeded": 2}


async def test_tool_denial_still_blocks_shared_reading(monkeypatch):
    result, calls, router = await run_shared(monkeypatch, [search_response(0), closeout()], tool_deny=frozenset({"kb_search"}))
    assert result.ok and calls == []
    assert "kb_search" not in {tool.name for tool in router.requests[0].tools}


async def test_direct_owner_cites_returned_chunks_without_a_report(monkeypatch):
    from bowhead_core.agents.lead_orchestrator import _resolve_citations
    from bowhead_core.citations import ensure_registry

    chunk = "chunk_aaaaaaaaaaaaaaaaaaaaaaaa"
    ctx = context(knowledge_direct_use=True)
    result, calls, _ = await run_shared(
        monkeypatch,
        [search_response(0), LLMResponse(content=f"Payment is due in 45 days [ref:{chunk}].")],
        profile_name="lead",
        ctx=ctx,
        chunks=[{"chunk_id": chunk, "file_id": "kbf_1", "file_name": "Contract.md", "content": "Payment is due in 45 days."}],
    )
    assert result.ok and len(calls) == 1
    answer = _resolve_citations(result.final_answer, ctx, invocations=result.tool_invocations)
    assert answer == "Payment is due in 45 days [cite:1]."
    assert ensure_registry(ctx).resolve(1).kind == "kb_chunk"


def test_direct_prompt_keeps_optional_researcher_without_mandatory_delegation():
    from bowhead_core.agents.prompts.lead import build_lead_system_prompt
    from bowhead_core.agents.prompts.peer_block import PeerEntry

    peers = [PeerEntry(name="kb-analyst", description="Research source documents.", source="system")]
    legacy = build_lead_system_prompt("ws_1", peers=peers, facts=[])
    shared = build_lead_system_prompt("ws_1", peers=peers, knowledge_direct_use=True, facts=[])
    assert "delegate directly to kb-analyst" in legacy
    assert "delegate directly to kb-analyst" not in shared
    assert "KB question → kb_search" in shared
    assert "kb-analyst" in shared


@pytest.mark.parametrize("invalid", [{"policy": "unknown"}, {"empty_rounds": -1}, {"round_queries": 4}, {"file_ids": ["outside"]}])
def test_sealed_budget_rejects_unknown_policy_and_scope_injection(invalid):
    import json
    import zlib

    message = {"role": "user", "content": "Continue the task.", "name": None, "tool_call_id": None, "tool_calls": None}
    with pytest.raises(ValueError):
        sr.decode_material(
            zlib.compress(json.dumps({"schema_version": 2, "messages": [message], "reference_inventory": [], "knowledge_read": invalid}).encode())
        )
