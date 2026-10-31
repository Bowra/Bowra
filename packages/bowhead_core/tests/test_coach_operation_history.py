"""Original receipt identity survives real stored events and fresh dispatches."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.coach_operation_history import (
    MAX_RECEIPTS,
    CoachOperationHistory,
    context_token_budget,
    parse_metadata,
    receipt_metadata,
    render_history,
)
from bowhead_core.session_store import SessionStore
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_core.types import ToolInvocation
from test_agent_coach import enabled as enabled
from test_agent_execution_binding import _Fixture, _setup

pytestmark = pytest.mark.unit
HistoryDb = tuple[_Fixture, SessionStore, TurnLogStore, ReferenceRequestScope]
POINTER = {"kind": "eval_cases", "agent_id": "osa_original", "proposal_id": "acp_original"}


def invocation(pointer: dict[str, str] = POINTER, *, call_id: str = "call_original", ok: bool = True) -> ToolInvocation:
    return ToolInvocation(
        "agent_eval_case_propose", {}, call_id=call_id, result={"ok": ok, "operation_ref": pointer, "private": "hidden criterion" * 1000}
    )


@pytest.fixture
async def history_db(tmp_path: Path) -> HistoryDb:
    f = await _setup(tmp_path)
    store = SessionStore(f.db_path)
    log = TurnLogStore(f.db_path)
    scope = ReferenceRequestScope(user_id=f.member_id, team_id=f.team_id, workspace_id=f.workspace_id, session_id="s_m3b")
    return f, store, log, scope


async def end(store: SessionStore, tid: str) -> None:
    await store.mark_completed(
        turn_id=tid, intent=None, final_answer="The requested operation was saved.", tokens_input=0, tokens_output=0, cost_usd=0
    )
    await store.finalize_turn(session_id="s_m3b", current_turn_id=tid, next_turn_id=None)


async def saved_events(
    log: TurnLogStore,
    tid: str,
    *,
    metadata: bool = True,
    summary: bool = True,
    pointer: dict[str, str] = POINTER,
    profile: str = "agent-coach",
    call_id: str = "call_original",
) -> None:
    await log.record(turn_id=tid, event_type="agent_spawned", payload={"subagent_id": "sub_coach", "profile": profile})
    meta = receipt_metadata([invocation(pointer, call_id=call_id)]).model_dump(mode="json")
    payload = {
        "subagent_id": "sub_coach",
        "parent_agent_id": "sub_coach",
        "call_id": call_id,
        "tool_name": "agent_eval_case_propose",
        "ok": True,
        "result_inline": None,
        "result_preview": "truncated",
    }
    if metadata:
        payload["result_meta"] = {"coach_operations": meta}
    else:
        payload["result_inline"] = {"operation_ref": pointer}
    await log.record(turn_id=tid, event_type="tool_result", payload=payload)
    if summary:
        await log.record(
            turn_id=tid,
            event_type="lead.subagent_summary",
            payload={"spawn_id": "sub_coach", "profile": profile, "call_id": "spawn_call", "coach_operations": meta},
        )


async def followup(store: SessionStore, *, tid: str = "t_followup") -> Any:
    return await store.record_turn(turn_id=tid, session_id="s_m3b", user_text="Open the operation we just accepted.")


def context(history: CoachOperationHistory, **changes: Any) -> SimpleNamespace:
    scope = history.scope
    values = {
        "user_id": scope.user_id,
        "team_id": scope.team_id,
        "workspace_id": scope.workspace_id,
        "session_id": scope.session_id,
        "turn_id": history.before_turn_id,
        "coach_operation_history": history,
    }
    return SimpleNamespace(**{**values, **changes})


@pytest.mark.parametrize("summary", [True, False])
async def test_large_saved_result_survives_intervening_read_and_missing_summary(history_db: HistoryDb, summary: bool) -> None:
    f, store, log, scope = history_db
    await saved_events(log, f.turn_id, summary=summary)
    await end(store, f.turn_id)
    await followup(store, tid="t_read")
    await log.record(turn_id="t_read", event_type="agent_spawned", payload={"profile": "agent-coach", "subagent_id": "sub_read"})
    await end(store, "t_read")
    await followup(store)
    history = await store.load_coach_operation_history(scope, "t_followup")
    assert history.availability == "available"
    assert len(history.entries) == 1
    assert history.entries[0].receipt.operation_ref.model_dump() == POINTER
    block = render_history(context(history))
    assert "acp_original" in block and "hidden criterion" not in block
    assert "sub_coach" in block and "t_followup" not in block


@pytest.mark.parametrize("field", ["user_id", "team_id", "workspace_id", "session_id"])
async def test_history_never_crosses_owned_scope(history_db: HistoryDb, field: str) -> None:
    f, store, log, scope = history_db
    await saved_events(log, f.turn_id)
    await end(store, f.turn_id)
    await followup(store)
    denied = await store.load_coach_operation_history(scope.model_copy(update={field: "other_identity"}), "t_followup")
    assert denied.availability == "unavailable" and not denied.entries
    good = await store.load_coach_operation_history(scope, "t_followup")
    assert "acp_original" not in render_history(context(good, **{field: "other_identity"}))


async def test_legacy_child_reference_is_read_without_backfill(history_db: HistoryDb) -> None:
    f, store, log, scope = history_db
    await saved_events(log, f.turn_id, metadata=False, summary=False)
    await end(store, f.turn_id)
    await followup(store)
    history = await store.load_coach_operation_history(scope, "t_followup")
    assert [e.receipt.operation_ref.proposal_id for e in history.entries] == ["acp_original"]


async def test_legacy_spawn_list_recovers_without_fabricating_child_call(history_db: HistoryDb) -> None:
    f, store, log, scope = history_db
    await log.record(turn_id=f.turn_id, event_type="agent_spawned", payload={"subagent_id": "sub_coach", "profile": "agent-coach"})
    await log.record(
        turn_id=f.turn_id,
        event_type="tool_result",
        payload={
            "tool_name": "subagent_spawn",
            "call_id": "spawn_call",
            "result_inline": {
                "subagent_id": "sub_coach",
                "coach_operation_receipts": [{"tool_name": "agent_eval_case_propose", "operation_ref": POINTER, "reported_ok": True}],
            },
        },
    )
    await end(store, f.turn_id)
    await followup(store)
    history = await store.load_coach_operation_history(scope, "t_followup")
    assert history.availability == "partial"
    assert history.entries[0].receipt.call_id is None
    assert history.entries[0].source_event_seq > 0


@pytest.mark.parametrize("bad", [None, {"schema_version": 2, "receipts": [], "truncated": False}])
async def test_malformed_modern_metadata_cannot_downgrade_to_inline(history_db: HistoryDb, bad: Any) -> None:
    f, store, log, scope = history_db
    await saved_events(log, f.turn_id, summary=False)
    await log.record(
        turn_id=f.turn_id,
        event_type="tool_result",
        payload={
            "subagent_id": "sub_coach",
            "parent_agent_id": "sub_coach",
            "tool_name": "agent_eval_case_propose",
            "call_id": "bad",
            "result_meta": {"coach_operations": bad},
            "result_inline": {"operation_ref": POINTER},
        },
    )
    await end(store, f.turn_id)
    await followup(store)
    history = await store.load_coach_operation_history(scope, "t_followup")
    assert history.availability == "partial"
    assert len(history.entries) == 1 and history.entries[0].receipt.call_id == "call_original"


async def test_other_peer_and_current_turn_results_are_not_candidates(history_db: HistoryDb) -> None:
    f, store, log, scope = history_db
    await saved_events(log, f.turn_id, profile="compute")
    await end(store, f.turn_id)
    await followup(store)
    await saved_events(log, "t_followup")
    assert not (await store.load_coach_operation_history(scope, "t_followup")).entries


async def test_same_invocation_conflicting_summary_is_not_selected_by_arrival(history_db: HistoryDb) -> None:
    f, store, log, scope = history_db
    await saved_events(log, f.turn_id, summary=False)
    conflicting = receipt_metadata([invocation({**POINTER, "proposal_id": "acp_other"})]).model_dump(mode="json")
    await log.record(
        turn_id=f.turn_id,
        event_type="lead.subagent_summary",
        payload={"profile": "agent-coach", "spawn_id": "sub_coach", "call_id": "spawn_call", "coach_operations": conflicting},
    )
    await end(store, f.turn_id)
    await followup(store)
    history = await store.load_coach_operation_history(scope, "t_followup")
    assert history.availability == "partial" and not history.entries


def test_projection_retains_unknown_receipts_and_bounds_whole_entries() -> None:
    calls = [invocation({**POINTER, "proposal_id": f"acp_{i}"}, call_id=f"call_{i}", ok=False) for i in range(25)]
    meta = receipt_metadata(calls)
    assert meta.truncated and len(meta.receipts) == MAX_RECEIPTS
    assert meta.receipts[0].operation_ref.proposal_id == "acp_5"
    assert not any(r.reported_ok for r in meta.receipts)
    assert "hidden criterion" not in meta.model_dump_json()
    assert parse_metadata(meta.model_dump_json()) == meta


def test_malformed_individual_receipt_preserves_other_valid_entries() -> None:
    raw = receipt_metadata([invocation()]).model_dump(mode="json")
    raw["receipts"].append({"operation_ref": {"kind": "unknown"}})
    meta = parse_metadata(json.dumps(raw))
    assert meta.truncated and len(meta.receipts) == 1


async def test_scan_limits_report_partial_and_do_not_reach_past_window(history_db: HistoryDb, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core import coach_operation_history

    f, store, log, scope = history_db
    await saved_events(log, f.turn_id)
    await end(store, f.turn_id)
    await followup(store, tid="t_read")
    await end(store, "t_read")
    await followup(store)
    monkeypatch.setattr(coach_operation_history, "MAX_TURNS", 1)
    history = await store.load_coach_operation_history(scope, "t_followup")
    assert history.availability == "partial" and not history.entries
    monkeypatch.setattr(coach_operation_history, "MAX_TURNS", 60)
    monkeypatch.setattr(coach_operation_history, "MAX_EVENTS", 1)
    bounded = await store.load_coach_operation_history(scope, "t_followup")
    assert bounded.availability == "partial"
    assert bounded.entries[0].receipt.operation_ref.proposal_id == "acp_original"


async def test_context_budget_removes_whole_references_and_scope_is_not_model_input(history_db: HistoryDb) -> None:
    f, store, log, scope = history_db
    await saved_events(log, f.turn_id)
    await end(store, f.turn_id)
    await followup(store)
    history = await store.load_coach_operation_history(scope, "t_followup")
    from bowhead_core.token_estimator import estimate_tokens

    rendered = render_history(context(history), max_tokens=estimate_tokens(render_history(context(history))) - 1)
    assert "acp_original" not in rendered
    assert scope.user_id not in rendered
    assert render_history(context(history), max_tokens=0) == ""
    assert context_token_budget(SimpleNamespace(context_window_for=lambda *a: 3200), "lead", context(history)) == 100


@pytest.mark.parametrize("history_on", ["0", "1"])
async def test_actual_lead_and_fresh_coach_receive_stored_pointer_without_task_id(
    history_db: HistoryDb, enabled: None, monkeypatch: pytest.MonkeyPatch, history_on: str
) -> None:
    from bowhead_core.agents.lead_orchestrator import LeadRuntime, _lead_loop
    from bowhead_core.agents.turn_context import SerializableTurnContext
    from bowhead_core.events import TurnEventBus
    from bowhead_core.subagent import SubagentResult, register_subagent_tool
    from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
    from test_agent_coach import DraftAdapter

    f, store, log, scope = history_db
    await saved_events(log, f.turn_id)
    await end(store, f.turn_id)
    await followup(store, tid="t_read")
    await end(store, "t_read")
    await followup(store)
    monkeypatch.setenv("BOWHEAD_CTX_TURN_HISTORY", history_on)
    monkeypatch.setenv("BOWHEAD_LEAD_RECENT_TURNS", history_on)
    task = "Open the operation we just accepted. Do not change or run anything."
    captures = []

    async def run(**kwargs: Any) -> Any:
        captures.append(kwargs)
        return SubagentResult(profile=kwargs["profile_name"], subagent_id="sub_result", ok=True, final_answer="Ready to review.", tool_invocations=[])

    runner = SimpleNamespace(run=run, router=None)
    ctx = ToolExecutionContext(
        user_id=scope.user_id,
        team_id=scope.team_id,
        workspace_id=scope.workspace_id,
        session_id=scope.session_id,
        turn_id="t_followup",
        member_uid="m",
        datarc=None,
        role="lead",
        session_store=store,
        subagent_runner=runner,
        parent_bus=TurnEventBus(),
    )
    runtime = LeadRuntime(
        router=None, tool_registry=build_default_registry(), parent_bus=ctx.parent_bus, parent_ctx=ctx, session_store=store, subagent_runner=runner
    )
    sctx = SerializableTurnContext(
        user_id=ctx.user_id, team_id=ctx.team_id, workspace_id=ctx.workspace_id, session_id=ctx.session_id, turn_id=ctx.turn_id, role="lead"
    )
    await _lead_loop(sctx, task, runtime)
    lead_call = captures[-1]
    assembled = lead_call.get("extra_instructions", "") + (lead_call.get("turn_context_block") or "")
    assert "acp_original" in assembled
    assert "acp_original" not in lead_call["task"]
    assert ctx.coach_operation_history.entries
    ctx.subagent_draft_editor = SimpleNamespace(coach_adapter=lambda: DraftAdapter((ctx.user_id, ctx.team_id, ctx.workspace_id)))
    registry = build_default_registry()
    register_subagent_tool(registry)
    result = await registry.execute("subagent_spawn", {"profile": "agent-coach", "task": task}, ctx)
    assert result.error is None, result.error
    coach_call = captures[-1]
    assert coach_call["profile_name"] == "agent-coach"
    assert "acp_original" in coach_call["extra_instructions"]
    assert "acp_original" not in coach_call["task"]


async def test_real_coach_engine_persists_pointer_before_large_result_inline_cap(
    history_db: HistoryDb, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bowhead_core.access_control import AccessControlStore
    from bowhead_core.agent_coach import register_agent_coach_tools
    from bowhead_core.events import TurnEventBus
    from bowhead_core.skill_bridge import build_default_skill_stack, register_skill_tools
    from bowhead_core.subagent import SubagentRunner, register_subagent_tool
    from bowhead_core.tool_registry import build_default_registry
    from test_agent_coach import StreamingTools
    from test_agent_coach import context as coach_context

    from bowhead_router import AgentBinding, LLMResponse, ProviderConfig, ProviderRegistry, Router, ToolCall

    f, store, log, scope = history_db
    ctx, draft = coach_context()
    ctx.user_id = scope.user_id
    ctx.team_id = scope.team_id
    ctx.workspace_id = scope.workspace_id
    ctx.session_id = scope.session_id
    ctx.turn_id = f.turn_id
    ctx.access_control_store = AccessControlStore(f.db_path)
    draft.caller = (ctx.user_id, ctx.team_id, ctx.workspace_id)

    async def save(patch: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "proposal": {"id": "sas_0123456789ab", "draft_id": "sad_original", "status": "pending"},
            "large_details": "private details" * 1000,
        }

    draft.propose = save

    async def sink(event: Any) -> None:
        await log.record(turn_id=event.turn_id, event_type=event.event_type, payload=event.payload)

    ctx.parent_bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    adapter = StreamingTools(provider_name="mock")
    adapter.scripted_responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id="method", name="skill_read", arguments={"name": "agent-authoring"})]),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="save_original", name="agent_authoring_propose", arguments={"patch": {"name": "focused-reader"}, "request_id": "save"})
            ],
        ),
        LLMResponse(content="The suggestion is saved for review."),
    ]
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="mock", type="mock")])
    providers._adapters["mock"] = adapter
    router = Router(providers)
    router.bind(AgentBinding(agent_type="analyst", primary=("mock", "model")))
    registry = build_default_registry()
    catalog, runtime = build_default_skill_stack()
    register_skill_tools(registry, catalog, runtime)
    register_agent_coach_tools(registry)
    register_subagent_tool(registry)
    ctx.subagent_runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=f.users)
    outcome = await registry.execute("subagent_spawn", {"profile": "agent-coach", "task": "Save the requested name change only."}, ctx)
    assert outcome.error is None, outcome.error
    assert outcome.result["coach_operation_receipts"]
    import sqlite3

    with sqlite3.connect(f.db_path) as db:
        events = [
            json.loads(r[0]) for r in db.execute("SELECT payload_json FROM turn_events WHERE turn_id=? AND event_type='tool_result'", (f.turn_id,))
        ]
    saved = next(e for e in events if e.get("tool_name") == "agent_authoring_propose")
    live = next(
        e.payload for e in ctx.parent_bus._events if e.event_type == "tool_result" and e.payload.get("tool_name") == "agent_authoring_propose"
    )
    assert live["result_meta"] == saved["result_meta"]
    assert saved["result_inline"] is None
    assert saved["result_meta"] is not None and "coach_operations" in saved["result_meta"]
    assert saved["result_meta"]["coach_operations"]["receipts"][0]["call_id"] == "save_original"
    assert "private details" not in json.dumps(saved["result_meta"])
    await end(store, f.turn_id)
    await followup(store)
    history = await store.load_coach_operation_history(scope, "t_followup")
    assert len(history.entries) == 1
    assert history.entries[0].receipt.operation_ref.suggestion_id == "sas_0123456789ab"
