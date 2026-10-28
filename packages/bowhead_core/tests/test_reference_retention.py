"""Real lifecycle/expiry transactions, including late writers and worker recovery.

Mutation checks remove deletion/expiry/worker gates separately. Queue tests use
the existing internal receipt+storage contract; no queue scheduler is advertised.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_retention import ReferenceRetentionResult, purge_expired_reference_material
from bowhead_core.chat_reference_storage import reference_source_hash
from bowhead_core.events import TurnEventBus
from bowhead_core.session_lifecycle import SessionLifecycleCoordinator, SessionLifecycleError
from bowhead_core.session_store import SessionStore
from bowhead_core.subagent import SubagentRunner
from bowhead_core.terminal_protocol_store import TerminalProtocolStore
from bowhead_core.tool_registry import build_default_registry
from test_chat_reference_storage import SUBMISSION_ID, _content, _plan, _queue, _receipt
from test_chat_reference_storage import case as _storage_case
from test_reference_primary_runtime import _ctx, _expire, _rows, _runtime
from test_reference_primary_runtime import accepted_case as _accepted_case
from test_reference_primary_runtime import start_case as _start_case
from test_subagent import _router

from bowhead_router import LLMResponse

case = _storage_case
accepted_case = _accepted_case
start_case = _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _seed(case, *, content_deadline=None):
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _plan(db, case)
        await _content(db, case, expires_at=content_deadline or case.expires)
        await _receipt(db, case)
        await _queue(db, case)
        await db.commit()


async def _second_plan(case):
    from bowhead_core.session_store import _insert_chat_turn_user_source_in_tx

    source = replace(case.source, turn_id="t_other", session_id="s_other")
    scope = case.scope.model_copy(update={"session_id": "s_other"})
    plan = case.plan.model_copy(
        update={
            "source_turn_id": "t_other",
            "logical_source_turn_id": "t_other",
            "snapshot_owner_turn_id": "t_other",
            "source_hash": reference_source_hash(source),
        }
    )
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "INSERT INTO sessions(id,user_id,workspace_id,started_at,last_active_at) VALUES ('s_other',?,?,?,?)",
            (scope.user_id, scope.workspace_id, case.now, case.now),
        )
        await db.execute("INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES ('t_other','s_other',1,?,?)", (source.user_text, case.now))
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=case.now)
        await case.store.put_plan_in_connection(db, scope=scope, turn_id="t_other", plan=plan, expires_at=case.expires)
        await db.commit()


def _facts(path):
    with sqlite3.connect(path) as db:
        return {
            table: db.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "chat_reference_plans",
                "chat_reference_content_snapshots",
                "chat_reference_queued_inputs",
                "chat_turn_user_sources",
                "chat_submission_receipts",
                "turns",
                "sessions",
            )
        }


def _advance(monkeypatch, when):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz)

    monkeypatch.setattr("bowhead_core.chat_reference_retention.datetime", Clock)


async def test_session_delete_and_undo_do_not_restore_private_material(case):
    await _seed(case)
    await _second_plan(case)
    coordinator = SessionLifecycleCoordinator(SessionStore(str(case.path)), TerminalProtocolStore(str(case.path)))
    before = _facts(case.path)
    result = await coordinator.soft_delete(
        session_id=case.scope.session_id,
        actor_audit_ref=case.scope.user_id,
        request_id="delete-reference",
        expected_user_id=case.scope.user_id,
    )
    assert result.changed
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT turn_id,retention_state FROM chat_reference_plans ORDER BY turn_id").fetchall() == [
            ("t_other", "available"),
            ("t_ref", "purged"),
        ]
        assert db.execute("SELECT ciphertext,retention_state FROM chat_reference_content_snapshots").fetchone() == (None, "purged")
        assert db.execute("SELECT ciphertext,retention_state,state,cancel_reason,row_version FROM chat_reference_queued_inputs").fetchone() == (
            None,
            "purged",
            "cancelled",
            "source_deleted",
            2,
        )
    assert (
        await coordinator.revive(
            session_id=case.scope.session_id,
            actor_audit_ref=case.scope.user_id,
            request_id="undo-reference",
            expected_user_id=case.scope.user_id,
        )
    ).changed
    after = _facts(case.path)
    for table in ("chat_turn_user_sources", "chat_submission_receipts", "turns"):
        assert after[table] == before[table]  # Soft deletion is not transcript erasure.
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="reference_source_unavailable"):
            await _content(db, case, descriptor=case.descriptor.model_copy(update={"content_key": "late"}))
    assert await purge_expired_reference_material(case.path) == ReferenceRetentionResult()


async def test_delete_is_owner_scoped_and_rolls_back_with_visibility(case):
    await _seed(case)
    before = _facts(case.path)
    coordinator = SessionLifecycleCoordinator(SessionStore(str(case.path)), TerminalProtocolStore(str(case.path)))
    with pytest.raises(SessionLifecycleError, match="owner_mismatch"):
        await coordinator.soft_delete(session_id=case.scope.session_id, actor_audit_ref="foreign", request_id="no", expected_user_id="foreign")
    assert _facts(case.path) == before
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE sessions SET deleted_at=? WHERE id=?", (case.now, case.scope.session_id))
        assert _facts(case.path) == before  # No separate cleanup commit.
        await db.rollback()
    assert _facts(case.path) == before


@pytest.mark.parametrize("reason", ["plan", "content", "offset", "old_deleted"])
async def test_expiry_purges_family_and_queue_without_membership_or_key(case, monkeypatch, reason):
    if reason == "offset":
        case.expires = (datetime.now(UTC) + timedelta(days=1)).astimezone(timezone(timedelta(hours=8))).isoformat()
    short_deadline = datetime.now(UTC) + timedelta(minutes=1)
    await _seed(case, content_deadline=short_deadline.isoformat() if reason == "content" else None)
    before = _facts(case.path)
    deadline = datetime.fromisoformat(case.expires) + timedelta(seconds=1)
    if reason == "content":
        deadline = short_deadline + timedelta(seconds=1)
    elif reason == "old_deleted":
        # A deletion committed by the previous release, before 0067 was installed.
        with sqlite3.connect(case.path) as db:
            db.execute("DROP TRIGGER trg_reference_session_delete")
            db.execute("UPDATE sessions SET deleted_at=? WHERE id=?", (case.now, case.scope.session_id))
            from importlib import import_module

            migration = import_module("bowhead_core.migrations.versions.0067_reference_retention")
            db.execute(migration.DDL[0])
        deadline = datetime.now(UTC)
    with sqlite3.connect(case.path) as db:
        db.execute("UPDATE team_members SET status='removed' WHERE user_id=?", (case.scope.user_id,))
    _advance(monkeypatch, deadline)
    assert await purge_expired_reference_material(case.path) == ReferenceRetentionResult(1, 1)
    after = _facts(case.path)
    for table in ("chat_turn_user_sources", "chat_submission_receipts", "turns"):
        assert after[table] == before[table]
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT retention_state FROM chat_reference_plans").fetchone() == ("purged",)
        assert db.execute("SELECT ciphertext FROM chat_reference_content_snapshots").fetchone() == (None,)
        assert db.execute("SELECT state,cancel_reason,row_version FROM chat_reference_queued_inputs").fetchone() == (
            "cancelled",
            "source_deleted" if reason == "old_deleted" else "retention_expired",
            2,
        )
    assert await purge_expired_reference_material(case.path) == ReferenceRetentionResult()


async def test_unexpired_content_is_untouched_and_failed_cleanup_is_atomic(case, monkeypatch):
    await _seed(case)
    before = _facts(case.path)
    assert await purge_expired_reference_material(case.path) == ReferenceRetentionResult()
    assert _facts(case.path) == before
    _advance(monkeypatch, datetime.fromisoformat(case.expires) + timedelta(seconds=1))
    with sqlite3.connect(case.path) as db:
        db.execute("CREATE TRIGGER fail_queue_cleanup BEFORE UPDATE ON chat_reference_queued_inputs BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        await purge_expired_reference_material(case.path)
    assert _facts(case.path) == before
    with sqlite3.connect(case.path) as db:
        db.execute("DROP TRIGGER fail_queue_cleanup")
    assert await purge_expired_reference_material(case.path) == ReferenceRetentionResult(1, 1)


async def test_due_batches_are_bounded_and_repeatable(case, monkeypatch):
    await _seed(case)
    await _second_plan(case)
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "INSERT INTO chat_submission_receipts SELECT team_id,user_id,'second',workspace_id,requested_session_id,actual_session_id,"
            "turn_id,submission_kind,submission_contract_version,model_control_batch,request_fingerprint,fingerprint_key_id,"
            "auth_session_generation_hash,outcome,response_json,created_at,expires_at FROM chat_submission_receipts"
        )
        await _queue(db, case, submission_id="second")
        await db.commit()
    _advance(monkeypatch, datetime.fromisoformat(case.expires) + timedelta(seconds=1))
    assert await purge_expired_reference_material(case.path, limit=1) == ReferenceRetentionResult(1, 1)
    assert await purge_expired_reference_material(case.path, limit=1) == ReferenceRetentionResult(1, 1)
    assert await purge_expired_reference_material(case.path, limit=1) == ReferenceRetentionResult()


@pytest.mark.parametrize("stage", ["admitted", "running", "output"])
async def test_deleted_primary_rejects_late_write_and_recovers_original_execution(accepted_case, stage):
    case = accepted_case
    runtime = None
    if stage != "admitted":
        runtime = await _runtime(case)
    if stage == "output":
        runner = SubagentRunner(
            router=_router(LLMResponse(content="private output")), tool_registry=build_default_registry(), team_subagent_store=case.fixture.users
        )
        await runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus())
    coordinator = SessionLifecycleCoordinator(case.store, TerminalProtocolStore(str(case.fixture.db_path)))
    await coordinator.soft_delete(
        session_id=case.scope.session_id, actor_audit_ref=case.scope.user_id, request_id="delete", expected_user_id=case.scope.user_id
    )
    if stage == "output":
        # The old completion proof still exists after purge. Even a writer that
        # omits the new application check cannot mark that Turn completed.
        with sqlite3.connect(case.fixture.db_path) as db, pytest.raises(sqlite3.IntegrityError, match="material unavailable for completion"):
            db.execute("UPDATE turns SET status='completed' WHERE id='t_reference'")
    if runtime is not None:
        assert await case.store.heartbeat_chat_spawn_dispatch(turn_id="t_reference", owner_id="worker-a") == "lost"
        assert not await runtime.persist_turn_state(
            store=case.store,
            turn_id="t_reference",
            status_value="completed",
            intent=None,
            final_answer="late answer",
            tokens_input=7,
            tokens_output=8,
            cost_usd=1,
            error=None,
            error_kind=None,
        )
    _expire(case)
    assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    executions = _rows(case, "turn_agent_execution")
    assert len(executions) == 1
    assert executions[0]["status"] == ("completed" if stage == "output" else "failed")
    assert all(e["ciphertext"] is None and e["retention_state"] == "purged" for e in _rows(case, "chat_reference_evidence"))
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT status,final_answer FROM turns WHERE id='t_reference'").fetchone() == ("failed", None)
        assert db.execute("SELECT active_turn_id FROM sessions WHERE id=?", (case.scope.session_id,)).fetchone() == (None,)
    assert await case.store.recover_expired_chat_spawn_dispatches() == []
    if stage == "admitted":
        assert _rows(case, "chat_reference_resolutions") == []
        assert _rows(case, "chat_reference_evidence") == []


async def test_expired_plan_stops_heartbeat_before_sweeper_runs(accepted_case, monkeypatch):
    case = accepted_case
    await _runtime(case)
    _advance(monkeypatch, datetime.now(UTC) + timedelta(days=31))
    assert await case.store.heartbeat_chat_spawn_dispatch(turn_id="t_reference", owner_id="worker-a") == "lost"
    assert _rows(case, "chat_reference_plans")[0]["retention_state"] == "available"


@pytest.mark.parametrize("cleanup_failed", [False, True])
async def test_actual_gateway_maintenance_runs_cleanup_with_writer_off(case, monkeypatch, cleanup_failed):
    await _seed(case)
    _advance(monkeypatch, datetime.fromisoformat(case.expires) + timedelta(seconds=1))
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "0")
    if cleanup_failed:
        monkeypatch.setattr(
            "bowhead_core.chat_reference_retention.purge_expired_reference_material", AsyncMock(side_effect=RuntimeError("storage offline"))
        )
    # Execute the actual nested production loop, without booting unrelated LLM,
    # email and external service lifespans. Only those unrelated stores are fake.
    import bowhead_gateway.main as main

    tree = ast.parse(Path(main.__file__).read_text())
    loop = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_chat_submission_maintenance_loop")
    sessions = SimpleNamespace(
        recover_expired_chat_spawn_dispatches=AsyncMock(return_value=[]),
        cleanup_chat_submission_protocol=AsyncMock(),
        cleanup_composer_telemetry_dedupe=AsyncMock(),
    )
    recovery_store = object()
    namespace = {
        "db_path": str(case.path),
        "session_store": sessions,
        "plan_store": SimpleNamespace(recover_orphans=AsyncMock()),
        "_legacy_untracked_min_age": None,
        "_chat_reference_recovery_store": recovery_store,
        "log": Mock(),
        "asyncio": SimpleNamespace(sleep=AsyncMock(side_effect=asyncio.CancelledError)),
    }
    exec(compile(ast.Module(body=[loop], type_ignores=[]), main.__file__, "exec"), namespace)  # noqa: S102 - checked-in loop, never user input.
    with pytest.raises(asyncio.CancelledError):
        await namespace["_chat_submission_maintenance_loop"]()
    sessions.recover_expired_chat_spawn_dispatches.assert_awaited_once_with(
        reference_store=recovery_store,
    )
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT retention_state FROM chat_reference_plans").fetchone() == ("available" if cleanup_failed else "purged",)


async def test_database_rejects_refill_after_purge_and_preserves_tombstone(case):
    await _seed(case)
    with sqlite3.connect(case.path) as db:
        original = db.execute("SELECT ciphertext FROM chat_reference_content_snapshots").fetchone()[0]
        db.execute("UPDATE chat_reference_plans SET retention_state='purged'")
        with pytest.raises(sqlite3.IntegrityError, match="source unavailable"):
            db.execute(
                "INSERT INTO chat_reference_content_snapshots SELECT turn_id,'late',schema_version,"
                "json_set(descriptor_json,'$.content_key','late'),content_hash,byte_count,?,'available',"
                "created_generation,created_policy_epoch,created_at,expires_at FROM chat_reference_content_snapshots",
                (original,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="parent lifecycle"):
            db.execute("DELETE FROM chat_reference_plans")
        assert db.execute("SELECT ciphertext FROM chat_reference_content_snapshots").fetchone() == (None,)


async def test_cancelled_queue_keeps_decision_when_material_expires(case, monkeypatch):
    await _seed(case)

    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert await case.store.settle_queued_input_in_connection(
            db, scope=case.scope, submission_id=SUBMISSION_ID, expected_version=1, cancel_reason="user_cancelled"
        )
        await db.commit()
    _advance(monkeypatch, datetime.fromisoformat(case.expires) + timedelta(seconds=1))
    assert await purge_expired_reference_material(case.path) == ReferenceRetentionResult(1, 1)
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT state,cancel_reason,row_version,ciphertext FROM chat_reference_queued_inputs").fetchone() == (
            "cancelled",
            "user_cancelled",
            3,
            None,
        )


async def test_concurrent_cleaners_only_purge_once(case, monkeypatch):
    await _seed(case)
    _advance(monkeypatch, datetime.fromisoformat(case.expires) + timedelta(seconds=1))
    results = await asyncio.gather(purge_expired_reference_material(case.path), purge_expired_reference_material(case.path))
    assert sum(r.plans for r in results) == sum(r.queued_inputs for r in results) == 1
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT row_version FROM chat_reference_queued_inputs").fetchone() == (2,)


async def test_cancelled_cleanup_rolls_back_completed_sql(case, monkeypatch):
    await _seed(case)
    before = _facts(case.path)
    _advance(monkeypatch, datetime.fromisoformat(case.expires) + timedelta(seconds=1))

    @asynccontextmanager
    async def interrupted_connection(path):
        async with open_shared_ctx(path) as db:
            execute = db.execute

            async def execute_then_cancel(sql, *args):
                result = await execute(sql, *args)
                if "UPDATE chat_reference_queued_inputs" in sql:
                    raise asyncio.CancelledError
                return result

            # Intercept only awaited mutations, retaining execute's async context
            # manager behavior for all actual SELECT cursors.
            db.execute = lambda sql, *args: execute_then_cancel(sql, *args) if "UPDATE chat_reference_queued_inputs" in sql else execute(sql, *args)
            yield db

    monkeypatch.setattr("bowhead_core.chat_reference_retention.open_shared_ctx", interrupted_connection)
    with pytest.raises(asyncio.CancelledError):
        await purge_expired_reference_material(case.path)
    assert _facts(case.path) == before


async def test_queue_cannot_outlive_parent_before_its_own_deadline(case):
    await _seed(case)
    with sqlite3.connect(case.path) as db:
        db.execute("UPDATE chat_reference_plans SET retention_state='purged'")
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="reference_source_unavailable"):
            await case.store.get_queued_input_in_connection(db, scope=case.scope, submission_id=SUBMISSION_ID)
        with pytest.raises(ValueError, match="reference_source_unavailable"):
            await _queue(db, case)
    assert await purge_expired_reference_material(case.path) == ReferenceRetentionResult(0, 1)


async def test_database_rejects_late_output_even_if_worker_omits_read_check(accepted_case):
    case = accepted_case
    runtime = await _runtime(case)
    with pytest.raises(RuntimeError, match="runner interrupted"):
        await runtime.run(
            runner=SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("runner interrupted"))), parent_ctx=_ctx(case), parent_bus=TurnEventBus()
        )
    execution = _rows(case, "turn_agent_execution")[0]["id"]
    ciphertext = runtime.cipher.seal(scope=case.scope, purpose="reference_output", identity=f"t_reference/{execution}", content=b"late")
    from bowhead_core.chat_reference_storage import _digest

    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE chat_reference_plans SET retention_state='purged'")
        with pytest.raises(sqlite3.IntegrityError, match="source unavailable"):
            db.execute(
                "INSERT INTO chat_reference_evidence SELECT turn_id,'late-output',schema_version,resolution_seq,execution_id,"
                "created_generation,'output_received',subagent_id,?,?,'available',created_at,expires_at "
                "FROM chat_reference_evidence WHERE kind='delegate_started'",
                (_digest(b"late"), ciphertext),
            )
    assert [r["kind"] for r in _rows(case, "chat_reference_evidence")] == ["delegate_started"]


@pytest.mark.parametrize("shape", ["accepted", "foreign_owner", "generation", "missing_root"])
async def test_preclaim_recovery_only_closes_exact_initial_admission(accepted_case, shape):
    case = accepted_case
    with sqlite3.connect(case.fixture.db_path) as db:
        if shape == "foreign_owner":
            db.execute("UPDATE chat_turn_spawn_dispatch SET owner_id='unknown-owner'")
        elif shape == "generation":
            db.execute("UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=2")
        elif shape == "missing_root":
            db.execute("DELETE FROM turn_agent_binding WHERE turn_id='t_reference'")
    _expire(case)
    before = _rows(case, "turn_agent_execution")
    assert await case.store.recover_expired_chat_spawn_dispatches() == (["t_reference"] if shape == "accepted" else [])
    assert _rows(case, "chat_reference_resolutions") == _rows(case, "chat_reference_evidence") == []
    after = _rows(case, "turn_agent_execution")
    if shape == "accepted":
        assert len(after) == 1 and after[0]["id"] == before[0]["id"] and after[0]["status"] == "failed"
        terminals = [json.loads(row["payload_json"]) for row in _rows(case, "turn_events") if row["event_type"] == "done"]
        assert len(terminals) == 1 and terminals[0]["status"] == "failed"
        assert await case.store.recover_expired_chat_spawn_dispatches() == []
    else:
        assert after == before
