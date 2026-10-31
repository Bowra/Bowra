"""Manual maintenance agrees across real legacy fanout, persistence and replay."""

import json
import time
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_run_limits import DEFAULT_LIMITS, get_runtime_controls
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.context_action import ContextMaintenanceAction
from bowhead_core.context_manual import run_manual_context
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.events import TurnEventBus
from bowhead_core.session_store import SessionStore
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_core.types import TurnRequest
from bowhead_core.user_store import UserStore
from cryptography.fernet import Fernet
from test_agent_collaboration import collab as collab
from test_context_maintenance import context_case as context_case

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("reduction", [True, False])
async def test_legacy_manual_success_has_the_same_live_and_replayed_completion(collab, context_case, monkeypatch, reduction):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = SessionStore(collab.path)
    worker = collab.worker
    session = await store.ensure_session(session_id="s_manual_legacy", user_id=worker.user_id, workspace_id=worker.workspace_id)
    async with open_shared_ctx(collab.path) as db:
        for index in range(4):
            await db.execute(
                "INSERT INTO turns(id,session_id,seq,user_text,status,started_at,final_answer) VALUES (?,?,?,?,'completed','2026-10-30',?)",
                (f"t_manual_history_{index}", session.id, index + 1, "evidence " * (1500 if reduction else 8), "Acknowledged evidence."),
            )
        await db.commit()
    action = ContextMaintenanceAction(expected_generation=None)
    request = TurnRequest(
        session_id=session.id,
        workspace_id=worker.workspace_id,
        user_id=worker.user_id,
        member_uid=worker.user_id,
        text="",
        turn_id="t_manual_legacy",
        team_id=worker.team_id,
        context_maintenance=action,
    )
    source = ChatTurnUserSource(
        turn_id=request.turn_id,
        session_id=session.id,
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        user_text="",
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
        context_maintenance=action,
    )
    await store.record_turn(turn_id=request.turn_id, session_id=session.id, user_text="", user_source=source)
    assert await store.claim_chat_spawn_dispatch_for_worker(turn_id=request.turn_id, owner_id="manual-worker", lease_seconds=600) == "claimed"
    owner = DocumentTurnOwner(
        team_id=worker.team_id,
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        session_id=session.id,
        turn_id=request.turn_id,
        generation=1,
        owner_id="manual-worker",
    )
    sink = OrderedTurnLogSink(TurnLogStore(collab.path))
    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    agent_runner = SubagentRunner(router=context_case.router, tool_registry=ToolRegistry(), team_subagent_store=UserStore(collab.path))
    policy = context_case.registry.definition_for("provider")["models"][0]["context_policy"]
    policy.update(enabled=False, keep_recent_tokens=1)
    if not reduction:
        context_case.response.content = "Summary detail. " * 180
    ctx = ToolExecutionContext(
        workspace_id=worker.workspace_id,
        member_uid=worker.user_id,
        user_id=worker.user_id,
        turn_id=request.turn_id,
        datarc=None,
        team_id=worker.team_id,
        session_id=session.id,
        execution_owner_id=owner.owner_id,
        execution_generation=1,
        session_store=store,
        parent_bus=bus,
        subagent_runner=agent_runner,
        agent_run_limits=DEFAULT_LIMITS,
        agent_runtime_budget=get_runtime_controls().budget(DEFAULT_LIMITS),
    )

    async def persist(**values):
        values.pop("status_value")
        values.pop("error")
        values.pop("error_kind")
        return await store.mark_completed(**values, worker=owner, context_publication=getattr(bus, "_context_publication", None))

    runner = SimpleNamespace(_t_start=time.perf_counter(), _check_cancel=lambda: None, _persist_turn_state=persist)
    result = await run_manual_context(runner, request, ctx=ctx, bus=bus, agent_runner=agent_runner)
    await sink.drain()
    assert result.status.value == "completed" and result.final_answer == ""
    assert len(context_case.sent) == 1 and context_case.sent[0].summary_only
    live = [(event.event_type, event.payload) for event in bus.snapshot()]
    async with open_shared_ctx(collab.path) as db:
        replay = [
            (row[0], json.loads(row[1]))
            for row in await (
                await db.execute(
                    "SELECT event_type,payload_json FROM turn_events WHERE turn_id=? ORDER BY seq",
                    (request.turn_id,),
                )
            ).fetchall()
        ]
    assert live == replay
    activities = [payload for kind, payload in live if kind == "agent.context"]
    assert [payload["phase"] for payload in activities] == ["running", "committed" if reduction else "skipped"]
    if not reduction:
        assert activities[-1]["reason"] == "no_change"
    assert bool(await store.context_conversation_generation(session_id=session.id)) is reduction
