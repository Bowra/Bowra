"""Unsuccessful parent transitions retire only their live collaboration ownership."""

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core._db import open_shared_ctx
from bowhead_core.session_store import SessionStore
from test_agent_collaboration import collab as collab
from test_agent_collaboration_migration import config
from test_chat_reference_storage import _historical_fixture
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("exit_path", ["failed", "cancelled", "aborted", "recovery", "pause", "completed"])
async def test_unsuccessful_turn_closes_only_live_collaboration_executions(collab, exit_path):
    sessions = SessionStore(collab.path)
    await collab.bindings.mark_terminal(collab.requester, status="completed")
    if exit_path == "recovery":
        # Expire only this isolated fixture's actual claimed lease. Recovery
        # itself owns the resulting Turn transition and durable terminal events.
        async with open_shared_ctx(collab.path) as db:
            await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01T00:00:00Z' WHERE turn_id=?", (collab.worker.turn_id,))
            await db.commit()
        assert await sessions.recover_expired_chat_spawn_dispatches() == [collab.worker.turn_id]
    else:
        assert await transition(sessions, collab.worker, exit_path)
    async with open_shared_ctx(collab.path) as db:
        async with db.execute("SELECT status,error_kind FROM turns WHERE id=?", (collab.worker.turn_id,)) as cursor:
            turn = await cursor.fetchone()
        async with db.execute("SELECT id,status FROM turn_agent_execution WHERE turn_id=? ORDER BY id", (collab.worker.turn_id,)) as cursor:
            executions = dict(await cursor.fetchall())
    assert turn[0] == ("failed" if exit_path == "recovery" else "clarification_pending" if exit_path == "pause" else exit_path)
    assert executions[collab.lead] == (
        "running" if exit_path in {"pause", "completed"} else "failed" if exit_path in {"failed", "recovery"} else "cancelled"
    )
    assert executions[collab.requester] == "completed"
    assert executions[collab.recipient] == ("running" if exit_path in {"pause", "completed"} else "cancelled")
    if exit_path not in {"pause", "completed"}:
        from bowhead_core.agent_execution_binding import AgentExecutionBindingError

        before = snapshot(collab.path)
        with pytest.raises(AgentExecutionBindingError, match="agent_execution_transition_conflict"):
            await collab.bindings.mark_terminal(collab.recipient, status="completed")
        assert snapshot(collab.path) == before


async def transition(sessions, worker, exit_path):
    args = {"worker": worker, "turn_id": worker.turn_id}
    if exit_path == "failed":
        return await sessions.mark_failed(**args, error="synthetic failure", error_kind="lead_internal_error")
    if exit_path == "pause":
        return await sessions.mark_clarification_pending(**args, intent=None)
    if exit_path == "completed":
        return await sessions.mark_completed(**args, intent=None, final_answer="Done", tokens_input=7, tokens_output=3, cost_usd=0.01)
    return await getattr(sessions, "mark_" + exit_path)(**args)


@pytest.mark.parametrize("exit_path", ["failed", "cancelled", "aborted", "pause", "completed"])
@pytest.mark.parametrize("race", ["generation", "owner", "expired", "space", "turn"])
async def test_late_worker_cannot_transition_or_close_new_ownership(collab, exit_path, race):
    worker = collab.worker
    async with open_shared_ctx(collab.path) as db:
        if race == "generation":
            await db.execute("UPDATE turns SET execution_generation=execution_generation+1 WHERE id=?", (worker.turn_id,))
            await db.execute(
                "UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=lease_execution_generation+1 WHERE turn_id=?", (worker.turn_id,)
            )
        elif race == "owner":
            await db.execute("UPDATE chat_turn_spawn_dispatch SET owner_id='next-worker' WHERE turn_id=?", (worker.turn_id,))
        elif race == "expired":
            await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01T00:00:00Z' WHERE turn_id=?", (worker.turn_id,))
        elif race == "space":
            worker = worker.model_copy(update={"workspace_id": "ws_other"})
        else:
            worker = worker.model_copy(update={"turn_id": "t_other"})
        await db.commit()
    before = snapshot(collab.path)
    assert not await transition(SessionStore(collab.path), worker, exit_path)
    assert snapshot(collab.path) == before


async def test_revoked_member_can_close_current_failure_but_cannot_start_work(collab):
    from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable, require_live_worker_in_tx

    async with open_shared_ctx(collab.path) as db:
        await db.execute("DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", (collab.worker.workspace_id, collab.worker.user_id))
        await db.commit()
        with pytest.raises(OrdinaryContinuationUnavailable):
            await require_live_worker_in_tx(db, worker=collab.worker)
    assert await transition(SessionStore(collab.path), collab.worker, "failed")


async def test_terminal_failure_is_atomic_and_does_not_rewrite_settled_or_unjoined_executions(collab, monkeypatch):
    sessions = SessionStore(collab.path)
    await collab.bindings.mark_terminal(collab.requester, status="failed", failure_code="synthetic_helper_failure")
    async with open_shared_ctx(collab.path) as db:
        await db.execute("DELETE FROM agent_collaboration_participant WHERE execution_id=?", (collab.recipient,))
        await db.commit()
    before = snapshot(collab.path)
    original_rollup = sessions._rollup_session_usage

    async def failed_rollup(*_args):
        raise RuntimeError("Synthetic transaction failure")

    monkeypatch.setattr(sessions, "_rollup_session_usage", failed_rollup)
    with pytest.raises(RuntimeError, match="Synthetic transaction failure"):
        await transition(sessions, collab.worker, "failed")
    assert snapshot(collab.path) == before
    monkeypatch.setattr(sessions, "_rollup_session_usage", original_rollup)
    assert await transition(sessions, collab.worker, "failed")
    with sqlite3.connect(collab.path) as db:
        ended_at = db.execute("SELECT ended_at FROM turns WHERE id=?", (collab.worker.turn_id,)).fetchone()[0]
        assert db.execute("SELECT status,ended_at,failure_code FROM turn_agent_execution WHERE id=?", (collab.lead,)).fetchone() == (
            "failed",
            ended_at,
            "lead_internal_error",
        )
        assert db.execute("SELECT status,failure_code FROM turn_agent_execution WHERE id=?", (collab.requester,)).fetchone() == (
            "failed",
            "synthetic_helper_failure",
        )
        assert db.execute("SELECT status FROM turn_agent_execution WHERE id=?", (collab.recipient,)).fetchone() == ("running",)
    settled = snapshot(collab.path)
    assert not await transition(sessions, collab.worker, "failed")
    assert snapshot(collab.path) == settled


MIGRATION = import_module("bowhead_core.migrations.versions.0169_collaboration_terminal_ownership")


@pytest.mark.parametrize("target", tuple(TARGET_FILES))
async def test_terminal_trigger_upgrade_preserves_all_targets(tmp_path, target):
    path = tmp_path / "migration.db"
    command.upgrade(config(path, target), MIGRATION.down_revision)
    before, old_schema = snapshot(path), schema(path)
    command.upgrade(config(path, target), MIGRATION.revision)
    command.upgrade(config(path, target), MIGRATION.revision)
    assert snapshot(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='trg_agent_collaboration_turn_closed'").fetchall() == (
            [("trg_agent_collaboration_turn_closed",)] if target == "core" else []
        )
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    if target != "core":
        assert schema(path) == old_schema


async def test_upgrade_does_not_backfill_historical_terminal_executions(collab, tmp_path):
    path = tmp_path / "old.db"
    _historical_fixture(collab.path, path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE turns SET status='failed',ended_at='2026-10-10T00:00:00Z' WHERE id=?", (collab.worker.turn_id,))
    before = snapshot(path)
    command.upgrade(config(path), MIGRATION.revision)
    assert snapshot(path) == before


async def test_trigger_replacement_failure_keeps_previous_revision_and_trigger(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "drift.db"
    command.upgrade(config(path), MIGRATION.down_revision)
    before, previous_schema = snapshot(path), schema(path)
    execute = Connection.exec_driver_sql

    def fail_after_drop(self, statement, *args, **kwargs):
        if statement == MIGRATION.TRIGGER:
            raise RuntimeError("Synthetic trigger installation failure")
        return execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", fail_after_drop)
    with pytest.raises(RuntimeError, match="Synthetic trigger installation failure"):
        command.upgrade(config(path), MIGRATION.revision)
    assert snapshot(path) == before
    assert schema(path) == previous_schema
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
