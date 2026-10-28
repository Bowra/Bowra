"""Accepted @Agent -> exact runner -> durable output -> worker recovery.

Only the external model is scripted in positive execution tests. Database
admission, revision resolution, runner, shared Lead tail and evidence are real.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
from bowhead_core.chat_reference_storage import ChatReferenceStore, ReferenceCipher
from bowhead_core.events import TerminalDurabilityAbort, TurnEventBus
from bowhead_core.subagent import SubagentResult, SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from cryptography.fernet import Fernet
from test_reference_primary_claim import _claim
from test_reference_primary_reader import accepted_case as _accepted_case_fixture
from test_reference_primary_reader import start_case as _start_case_fixture
from test_subagent import _router

from bowhead_router import LLMResponse

accepted_case = _accepted_case_fixture
start_case = _start_case_fixture
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _runtime(case):
    await _claim(case)
    return ReferencePrimaryRuntime(
        scope=case.scope,
        turn_id="t_reference",
        owner_id="worker-a",
        bindings=AgentExecutionBindingStore(case.fixture.db_path),
        cipher=ReferenceCipher(Fernet.generate_key().decode()),
    )


def _ctx(case):
    return ToolExecutionContext(**case.scope.identity(), turn_id="t_reference", member_uid=case.fixture.member_id, datarc=None)


def _rows(case, table):
    with sqlite3.connect(case.fixture.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE turn_id='t_reference'")]


async def _cancel(case):
    return await case.store.request_cancel_turn(
        team_id=case.scope.team_id,
        user_id=case.scope.user_id,
        workspace_id=case.scope.workspace_id,
        turn_id="t_reference",
        expected_execution_generation=1,
        cancel_request_id="cancel_reference",
    )


def _expire(case):
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id='t_reference'",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )


async def test_real_runner_consumes_saved_primary_and_records_encrypted_output(accepted_case, monkeypatch):
    case = accepted_case
    runtime = await _runtime(case)
    primary = await runtime.validate()
    ctx = _ctx(case)
    ctx._final_answer_gate = AsyncMock(side_effect=AssertionError("Lead hook leaked"))
    router = _router(LLMResponse(content="盈利分析已完成。"))
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), team_subagent_store=case.fixture.users)
    original_run = runner.run

    async def check_original_task(**kwargs):
        assert kwargs["task"] == primary.source.input_document.text_content()
        assert kwargs["exact_binding"] == primary.binding
        assert not kwargs.get("extra_instructions") and not kwargs.get("history")
        return await original_run(**kwargs)

    monkeypatch.setattr(runner, "run", check_original_task)
    result = await runtime.run(runner=runner, parent_ctx=ctx, parent_bus=TurnEventBus())
    assert result.ok and result.final_answer == "盈利分析已完成。"
    ctx._final_answer_gate.assert_not_called()
    executions = _rows(case, "turn_agent_execution")
    assert len(executions) == 1 and executions[0]["id"] == primary.binding.execution_id
    assert executions[0]["status"] == "completed"
    composition = json.loads(_rows(case, "chat_reference_resolutions")[0]["composition_json"])
    assert composition["resource"] == primary.plan.admissions[0].resource.model_dump(mode="json", exclude_none=True)
    assert composition["baseline"] == primary.binding.behavior_descriptor.model_dump(mode="json", exclude_none=True)
    evidence = _rows(case, "chat_reference_evidence")
    assert [e["kind"] for e in evidence] == ["delegate_started", "output_received"]
    assert all(e["subagent_id"] == result.subagent_id for e in evidence)
    assert "盈利分析" not in evidence[1]["ciphertext"]
    decoded = runtime.cipher.open(
        scope=case.scope, purpose="reference_output", identity=f"t_reference/{primary.binding.execution_id}", ciphertext=evidence[1]["ciphertext"]
    )
    assert json.loads(decoded) == {"final_answer": result.final_answer, "payload": None}


@pytest.mark.parametrize("boundary", ["duplicate", "owner", "expired", "cancel", "generation", "source_purged"])
async def test_dispatch_fences_prevent_any_second_or_unauthorized_call(accepted_case, boundary):
    case = accepted_case
    runtime = await _runtime(case)
    calls = 0

    async def fake_run(**kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("provider disconnected")

    runner = SimpleNamespace(run=fake_run)
    if boundary == "duplicate":
        with pytest.raises(RuntimeError, match="provider disconnected"):
            await runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus())
    elif boundary == "owner":
        runtime = replace(runtime, owner_id="worker-b")
    elif boundary == "expired":
        _expire(case)
    elif boundary == "cancel":
        await _cancel(case)
    elif boundary == "generation":
        runtime = replace(runtime, generation=2)
    else:
        with sqlite3.connect(case.fixture.db_path) as db:
            db.execute("UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id='t_reference'")
    with pytest.raises(TerminalDurabilityAbort):
        await runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus())
    assert calls == int(boundary == "duplicate")


@pytest.mark.parametrize("outcome", ["exception", "cancel", "completed", "before_dispatch"])
async def test_expired_worker_recovery_closes_original_execution_without_reinvoking(accepted_case, outcome):
    case = accepted_case
    runtime = await _runtime(case)
    runner = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("disconnected")))
    if outcome in ("exception", "cancel"):
        with pytest.raises(RuntimeError):
            await runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus())
    if outcome == "cancel":
        await _cancel(case)
    elif outcome == "completed":
        real_runner = SubagentRunner(
            router=_router(LLMResponse(content="分析完成。")), tool_registry=build_default_registry(), team_subagent_store=case.fixture.users
        )
        await runtime.run(runner=real_runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus())
        await case.store.mark_completed(turn_id="t_reference", intent=None, tokens_input=0, tokens_output=0, cost_usd=0, final_answer="分析完成。")
    _expire(case)
    assert await case.store.heartbeat_chat_spawn_dispatch(turn_id="t_reference", owner_id="worker-a", settling=True) == "lost"
    assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    assert await case.store.recover_expired_chat_spawn_dispatches() == []
    expected = "cancelled" if outcome == "cancel" else "completed" if outcome == "completed" else "failed"
    execution = _rows(case, "turn_agent_execution")
    assert len(execution) == 1 and execution[0]["status"] == expected
    events = _rows(case, "turn_events")
    assert [e["event_type"] for e in events].count("done") == 1
    assert json.loads(next(e["payload_json"] for e in events if e["event_type"] == "done"))["status"] == expected
    assert runner.run.await_count == int(outcome in ("exception", "cancel"))


async def test_completion_cannot_skip_required_primary(accepted_case):
    case = accepted_case
    await _runtime(case)
    with pytest.raises(sqlite3.IntegrityError, match="required reference primary"):
        await case.store.mark_completed(
            turn_id="t_reference", intent=None, tokens_input=0, tokens_output=0, cost_usd=0, final_answer="Lead guessed success"
        )
    assert _rows(case, "turn_agent_execution")[0]["status"] == "running"


async def test_shared_lead_tail_delivers_primary_prose(accepted_case):
    from bowhead_core.agents.lead_orchestrator import LeadRuntime, run_turn
    from bowhead_core.agents.turn_context import SerializableTurnContext

    case = accepted_case
    runtime = await _runtime(case)
    ctx = _ctx(case)
    ctx._reference_runtime = runtime
    registry = build_default_registry()
    router = _router(LLMResponse(content="这是选定 Agent 的答复。"))
    runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=case.fixture.users)
    bus = TurnEventBus()
    primary = await runtime.validate()
    result = await run_turn(
        SerializableTurnContext(**case.scope.identity(), turn_id="t_reference", role="user"),
        primary.source.user_text,
        runtime=LeadRuntime(router=router, tool_registry=registry, parent_bus=bus, parent_ctx=ctx, subagent_runner=runner),
    )
    assert result.status == "completed"
    assert result.final_answer == "这是选定 Agent 的答复。"
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"


async def test_gateway_worker_uses_reference_adapter_and_one_durable_terminal(accepted_case, monkeypatch):
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.orchestrator import Orchestrator
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
    from bowhead_core.types import TurnRequest
    from bowhead_gateway.turn_service import admit_legacy_chat_execution, spawn_turn
    from test_reference_primary_reader import _read

    case = accepted_case
    primary = await _read(case)
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    registry = build_default_registry()
    router = _router(LLMResponse(content="选定 Agent 已完成任务。"))
    logs = TurnLogStore(case.fixture.db_path)
    events = EventBusRegistry(default_log_sink=OrderedTurnLogSink(logs))
    orchestrator = Orchestrator(
        router=router, datarc=None, session_store=case.store, tool_registry=registry, event_registry=events, user_store=case.fixture.users
    )
    orchestrator.dispatch_owner_id = "worker-a"
    orchestrator.subagent_runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=case.fixture.users)
    request = TurnRequest(
        **case.scope.identity(),
        member_uid=case.fixture.member_id,
        turn_id="t_reference",
        text=primary.source.user_text,
        reference_scope=case.scope,
        planning_mode="off",
        team_router=router,
    )
    # Empty JSON buffers are semantic values, not one specific serialization.
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE sessions SET steer_buffer_json='[ ]' WHERE id='s_reference'")
    bus = await admit_legacy_chat_execution(orchestrator, request)
    task = await spawn_turn(orchestrator, request, admitted_legacy_bus=bus)
    from bowhead_gateway.routes.chat import _inflight_tasks

    monitor = next(t for t in _inflight_tasks if t.get_name() == "turn_dispatch_lease:t_reference")
    await asyncio.wait_for(asyncio.gather(task, monitor), timeout=30)
    # Await both the worker and its ordered tail settlement.
    execution = _rows(case, "turn_agent_execution")
    assert len(execution) == 1 and execution[0]["status"] == "completed"
    assert request.reference_runtime is not None
    saved = _rows(case, "turn_events")
    terminals = [json.loads(e["payload_json"]) for e in saved if e["event_type"] == "done"]
    assert len(terminals) == 1 and terminals[0]["status"] == "completed"
    final = [json.loads(e["payload_json"]) for e in saved if e["event_type"] == "final_answer"]
    assert final and final[-1]["text"] == "选定 Agent 已完成任务。"
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT status,final_answer FROM turns WHERE id='t_reference'").fetchone() == ("completed", final[-1]["text"])
        assert db.execute("SELECT active_turn_id FROM sessions WHERE id='s_reference'").fetchone() == (None,)
    assert _rows(case, "chat_turn_spawn_dispatch")[0]["state"] == "terminal"
    replay = [e async for e in bus.subscribe()]
    assert [e.payload["text"] for e in replay if e.event_type == "final_answer"][-1] == final[-1]["text"]


@pytest.mark.parametrize("start_case", ["system"], indirect=True)
async def test_structured_primary_payload_reaches_shared_output_tail(accepted_case):
    from bowhead_core.agents.lead_orchestrator import LeadRuntime, run_turn
    from bowhead_core.agents.turn_context import SerializableTurnContext
    from bowhead_core.subagent_return_tool import register_subagent_return_tool

    from bowhead_router import ToolCall

    case = accepted_case
    runtime = await _runtime(case)
    ctx = _ctx(case)
    ctx._reference_runtime = runtime
    registry = build_default_registry()
    register_subagent_return_tool(registry)
    answer = "需要补充成本明细，才能解释利润变化。"
    router = _router(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="return", name="subagent_return", arguments={"payload": {"delivery_surface": "chat", "exec_summary": answer}})],
        )
    )
    runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=case.fixture.users)
    primary = await runtime.validate()
    result = await run_turn(
        SerializableTurnContext(**case.scope.identity(), turn_id="t_reference", role="user"),
        primary.source.user_text,
        runtime=LeadRuntime(router=router, tool_registry=registry, parent_bus=TurnEventBus(), parent_ctx=ctx, subagent_runner=runner),
    )
    assert result.status == "completed" and answer in result.final_answer
    assert ctx._m137_subagent_payloads[0]["payload"]["exec_summary"] == answer
    evidence = next(e for e in _rows(case, "chat_reference_evidence") if e["kind"] == "output_received")
    assert (
        json.loads(
            runtime.cipher.open(
                scope=case.scope,
                purpose="reference_output",
                identity=f"t_reference/{primary.binding.execution_id}",
                ciphertext=evidence["ciphertext"],
            )
        )["payload"]["exec_summary"]
        == answer
    )


@pytest.mark.parametrize("failure", ["output_insert", "wrong_receiver", "expire_after_result"])
async def test_output_failure_never_reinvokes_or_completes_without_proof(accepted_case, failure):
    case = accepted_case
    runtime = await _runtime(case)

    async def produce(**kwargs):
        if failure == "output_insert":
            with sqlite3.connect(case.fixture.db_path) as db:
                db.execute(
                    "CREATE TRIGGER fail_reference_output BEFORE INSERT ON chat_reference_evidence WHEN NEW.kind='output_received' BEGIN SELECT RAISE(ABORT,'disk failure'); END"
                )
        elif failure == "expire_after_result":
            _expire(case)
        return SubagentResult(
            profile=kwargs["profile_name"],
            subagent_id="wrong" if failure == "wrong_receiver" else kwargs["pre_minted_subagent_id"],
            ok=True,
            final_answer="private output",
        )

    runner = SimpleNamespace(run=AsyncMock(side_effect=produce))
    with pytest.raises((TerminalDurabilityAbort, ValueError)):
        await runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus())
    assert [e["kind"] for e in _rows(case, "chat_reference_evidence")] == ["delegate_started"]
    assert _rows(case, "turn_agent_execution")[0]["status"] == "running"
    with pytest.raises(TerminalDurabilityAbort):
        await runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus())
    assert runner.run.await_count == 1
    _expire(case)
    assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    assert _rows(case, "turn_agent_execution")[0]["status"] == "failed"


@pytest.mark.parametrize("state", ["owner", "expired", "cancel", "generation", "team"])
async def test_terminal_writer_checks_owner_in_same_transaction(accepted_case, state):
    case = accepted_case
    runtime = await _runtime(case)
    if state == "owner":
        runtime = replace(runtime, owner_id="foreign")
    elif state == "expired":
        _expire(case)
    elif state == "cancel":
        await _cancel(case)
    elif state == "team":
        runtime = replace(runtime, scope=runtime.scope.model_copy(update={"team_id": "team_foreign"}))
    else:
        runtime = replace(runtime, generation=2)
    assert not await runtime.persist_turn_state(
        store=case.store,
        turn_id="t_reference",
        status_value="failed",
        intent=None,
        final_answer="late answer",
        tokens_input=7,
        tokens_output=8,
        cost_usd=0.2,
        error="failure",
        error_kind="internal",
    )
    with sqlite3.connect(case.fixture.db_path) as db:
        row = db.execute("SELECT status,final_answer,tokens_input FROM turns WHERE id='t_reference'").fetchone()
    assert row[0] == ("cancelled" if state == "cancel" else "running") and not row[1] and row[2] == 0
    assert _rows(case, "turn_agent_execution")[0]["status"] == "running"


async def test_active_evidence_is_immutable_and_purge_erases_output(accepted_case):
    case = accepted_case
    runtime = await _runtime(case)
    real_runner = SubagentRunner(
        router=_router(LLMResponse(content="private output")), tool_registry=build_default_registry(), team_subagent_store=case.fixture.users
    )
    await runtime.run(runner=real_runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus())
    with sqlite3.connect(case.fixture.db_path) as db:
        for statement in (
            "DELETE FROM chat_reference_evidence",
            "DELETE FROM chat_reference_resolutions",
            "UPDATE chat_reference_resolutions SET composition_digest='sha256:'||printf('%064d',0)",
            "UPDATE chat_reference_evidence SET subagent_id='forged'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await ChatReferenceStore(runtime.cipher).purge_in_connection(db, scope=case.scope)
        await db.commit()
    assert all(e["retention_state"] == "purged" and e["ciphertext"] is None for e in _rows(case, "chat_reference_evidence"))


@pytest.mark.parametrize("scenario", ["switched_off", "cancel", "expired", "callback_error", "changed_request"])
async def test_gateway_preworker_failures_do_not_call_runner(accepted_case, monkeypatch, scenario):
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
    from bowhead_core.types import TurnRequest
    from bowhead_gateway.turn_service import TurnDispatchAlreadyOwnedError, admit_legacy_chat_execution, spawn_turn
    from test_reference_primary_reader import _read

    case = accepted_case
    primary = await _read(case)
    # 5a7b5fed7 turned the primary runtime on by default; the rollback path needs an explicit 0.
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "0" if scenario == "switched_off" else "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    orchestrator = SimpleNamespace(
        session_store=case.store,
        event_registry=EventBusRegistry(default_log_sink=OrderedTurnLogSink(TurnLogStore(case.fixture.db_path))),
        dispatch_owner_id="worker-a",
        _cancel_events={},
    )
    request = TurnRequest(
        **case.scope.identity(),
        member_uid=case.fixture.member_id,
        turn_id="t_reference",
        text=primary.source.user_text,
        reference_scope=case.scope,
        planning_mode="off",
    )
    bus = await admit_legacy_chat_execution(orchestrator, request)
    forbidden = AsyncMock(side_effect=AssertionError("worker must not be scheduled"))
    monkeypatch.setattr("bowhead_gateway.routes.chat._run_turn_and_cleanup", forbidden)

    async def before_schedule():
        if scenario == "cancel":
            await _cancel(case)
        elif scenario == "expired":
            _expire(case)
        elif scenario == "changed_request":
            request.execution_generation = 2
        elif scenario == "callback_error":
            raise RuntimeError("callback failed")

    with pytest.raises((ValueError, RuntimeError, asyncio.CancelledError, TurnDispatchAlreadyOwnedError)):
        await spawn_turn(orchestrator, request, admitted_legacy_bus=bus, before_schedule=before_schedule)
    forbidden.assert_not_called()
    assert _rows(case, "chat_reference_evidence") == []
    if scenario == "switched_off":
        assert _rows(case, "chat_reference_resolutions") == []
        assert _rows(case, "turn_agent_execution")[0]["status"] == "admitted"
    else:
        assert _rows(case, "turn_agent_execution")[0]["status"] == ("cancelled" if scenario == "cancel" else "failed")
        assert _rows(case, "chat_turn_spawn_dispatch")[0]["state"] == "failed"


@pytest.mark.parametrize("stage", ["delegate_started", "output_received"])
async def test_lost_commit_ack_preserves_evidence_and_never_retries_call(accepted_case, monkeypatch, stage):
    import aiosqlite

    case = accepted_case
    runtime = await _runtime(case)
    real_commit = aiosqlite.Connection.commit
    lost = False

    async def lose_one_ack(db):
        nonlocal lost
        async with db.execute("SELECT 1 FROM chat_reference_evidence WHERE kind=?", (stage,)) as cursor:
            written = await cursor.fetchone()
        await real_commit(db)
        if written and not lost:
            lost = True
            raise RuntimeError("commit ACK lost")

    monkeypatch.setattr(aiosqlite.Connection, "commit", lose_one_ack)

    async def produce(**kwargs):
        return SubagentResult(profile=kwargs["profile_name"], subagent_id=kwargs["pre_minted_subagent_id"], ok=True, final_answer="complete")

    runner = SimpleNamespace(run=AsyncMock(side_effect=produce))
    with pytest.raises(TerminalDurabilityAbort):
        await runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus())
    assert lost
    before = _rows(case, "chat_reference_evidence")
    with pytest.raises(TerminalDurabilityAbort):
        await runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus())
    assert _rows(case, "chat_reference_evidence") == before
    assert runner.run.await_count == int(stage == "output_received")
    _expire(case)
    assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    assert _rows(case, "turn_agent_execution")[0]["status"] == ("completed" if stage == "output_received" else "failed")


async def test_concurrent_runtime_dispatchers_invoke_once(accepted_case):
    case = accepted_case
    runtime = await _runtime(case)

    async def produce(**kwargs):
        await asyncio.sleep(0)
        return SubagentResult(profile=kwargs["profile_name"], subagent_id=kwargs["pre_minted_subagent_id"], ok=True, final_answer="complete")

    runner = SimpleNamespace(run=AsyncMock(side_effect=produce))
    outcomes = await asyncio.gather(
        *(runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus()) for _ in range(2)), return_exceptions=True
    )
    assert sum(isinstance(r, SubagentResult) for r in outcomes) == 1
    assert sum(isinstance(r, TerminalDurabilityAbort) for r in outcomes) == 1
    assert runner.run.await_count == 1
    assert len(_rows(case, "chat_reference_evidence")) == 2


@pytest.mark.parametrize("start_case", ["system"], indirect=True)
@pytest.mark.parametrize("cancelled", [False, True])
async def test_recovery_closes_active_capability_children_before_parent(accepted_case, cancelled):
    from bowhead_core.agent_capability_execution import CapabilityExecutionSelection, CapabilityExecutionStore

    case = accepted_case
    runtime = await _runtime(case)
    primary = await runtime.validate()
    caps = CapabilityExecutionStore(case.fixture.db_path)
    children = []
    for index in range(3):
        child = await caps.select(
            agent_execution_id=primary.binding.execution_id,
            expected_agent_capability_fingerprint=primary.binding.capability_fingerprint,
            selection=CapabilityExecutionSelection(
                composition_node_id=f"node_{index}",
                attempt_generation=1,
                capability_id="cap_test",
                capability_version=1,
                implementation_id="capi_test",
                implementation_version=1,
                adapter_id=None,
                prompt_module_fingerprint="sha256:" + "0" * 64,
                dependency_fingerprint="sha256:" + "1" * 64,
                input_artifact_refs=(),
                output_contract="test.output.v1",
            ),
        )
        children.append(child)
        if index:
            await caps.mark_running(child.execution_id)
        if index == 2:
            await caps.mark_terminal(child.execution_id, status="completed")
    if cancelled:
        await _cancel(case)
    _expire(case)
    assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    rows = _rows(case, "turn_capability_execution")
    assert [r["status"] for r in rows] == (["cancelled"] * 2 if cancelled else ["blocked"] * 2) + ["completed"]
    assert _rows(case, "turn_agent_execution")[0]["status"] == ("cancelled" if cancelled else "failed")


async def test_cancelled_reference_recovery_preserves_a_new_turn_buffer(accepted_case):
    case = accepted_case
    await _runtime(case)
    await _cancel(case)
    await case.store.record_turn(turn_id="t_next", session_id="s_reference", user_text="next task")
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE sessions SET active_turn_id='t_next',steer_buffer_json='[\"next work\"]' WHERE id='s_reference'")
    _expire(case)
    assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id='s_reference'").fetchone() == ("t_next", '["next work"]')
    assert _rows(case, "turn_agent_execution")[0]["status"] == "cancelled"


async def test_evidence_only_deletes_with_its_parent_lifecycle(accepted_case):
    case = accepted_case
    runtime = await _runtime(case)
    runner = SubagentRunner(
        router=_router(LLMResponse(content="complete")), tool_registry=build_default_registry(), team_subagent_store=case.fixture.users
    )
    await runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus())
    assert await runtime.persist_turn_state(
        store=case.store,
        turn_id="t_reference",
        status_value="completed",
        intent=None,
        final_answer="complete",
        tokens_input=0,
        tokens_output=0,
        cost_usd=0,
        error=None,
        error_kind=None,
    )
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="parent lifecycle"):
            db.execute("DELETE FROM chat_reference_evidence")
        db.execute("DELETE FROM turns WHERE id='t_reference'")
        db.commit()
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT COUNT(*) FROM chat_reference_evidence").fetchone() == (0,)
