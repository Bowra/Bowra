"""Policy tightening changes real storage/runtime gates before the cleaner runs.

Mutations remove the historical MIN, version fence, role/CAS and lifecycle gates;
tests use actual policy writes, SQLite, admission, worker claims and cleanup.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_retention import ReferenceRetentionResult, purge_expired_reference_material
from bowhead_core.events import TurnEventBus
from bowhead_core.reference_retention_policy import (
    ReferenceRetentionPolicy,
    ReferenceRetentionPolicyError,
    get_retention_policy,
    set_retention_policy,
)
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import build_default_registry
from test_chat_reference_storage import SUBMISSION_ID, _plan, _queue, _receipt
from test_chat_reference_storage import case as _case
from test_reference_primary_reader import _prepare_case
from test_reference_primary_runtime import _ctx, _expire, _rows, _runtime
from test_reference_primary_runtime import start_case as _start_case
from test_reference_retention import _second_plan, _seed
from test_subagent import _router

from bowhead_router import LLMResponse

case = _case
start_case = _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _set(case, days, version):
    return await set_retention_policy(case.path, team_id=case.scope.team_id, user_id=case.scope.user_id, days=days, expected_version=version)


async def _old_material(case, monkeypatch, *, plain_parent=False):
    with monkeypatch.context() as clock:
        clock.setattr("bowhead_core.chat_reference_storage._now", lambda: datetime.now(UTC) - timedelta(days=2))
        if plain_parent:
            async with open_shared_ctx(str(case.path)) as db:
                await db.execute("BEGIN IMMEDIATE")
                await _receipt(db, case)
                await _queue(db, case)
                await db.commit()
        else:
            await _seed(case)


async def test_defaults_noop_cas_and_append_only_audit(case):
    assert await get_retention_policy(case.path, team_id=case.scope.team_id, user_id=case.scope.user_id) == ReferenceRetentionPolicy(180, 0)
    assert await _set(case, 180, 0) == ReferenceRetentionPolicy(180, 0)
    results = await asyncio.gather(_set(case, 7, 0), _set(case, 30, 0), return_exceptions=True)
    assert sum(isinstance(r, ReferenceRetentionPolicy) for r in results) == 1
    assert [r.code for r in results if isinstance(r, ReferenceRetentionPolicyError)] == ["reference_retention_policy_conflict"]
    with sqlite3.connect(case.path) as db:
        rows = db.execute("SELECT team_id,version,days,updated_by,updated_at FROM team_reference_retention_policies").fetchall()
        assert len(rows) == 1 and rows[0][:2] == (case.scope.team_id, 1)
        assert rows[0][2] in (7, 30) and rows[0][3] == case.scope.user_id
        assert datetime.fromisoformat(rows[0][4]).tzinfo is not None
        for sql in (
            "UPDATE team_reference_retention_policies SET days=180",
            "DELETE FROM team_reference_retention_policies",
            "INSERT OR REPLACE INTO team_reference_retention_policies SELECT * FROM team_reference_retention_policies",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)
        assert db.execute("SELECT * FROM team_reference_retention_policies").fetchall() == rows


@pytest.mark.parametrize("days,version", [(0, 0), (181, 0), (True, 0), (1.0, 0), (7, -1), (7, False)])
async def test_invalid_policy_rejected_before_database_access(tmp_path, days, version):
    with pytest.raises(ReferenceRetentionPolicyError, match="policy_invalid"):
        await set_retention_policy(tmp_path / "missing" / "db", team_id="none", user_id="none", days=days, expected_version=version)


@pytest.mark.parametrize("plain_parent", [False, True])
async def test_tightening_then_loosening_never_revives_old_material(case, monkeypatch, plain_parent):
    await _old_material(case, monkeypatch, plain_parent=plain_parent)
    await _set(case, 1, 0)
    # No cleaner ran between these changes. Current-value-only implementations revive it.
    await _set(case, 180, 1)
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        if not plain_parent:
            with pytest.raises(ValueError, match="source_unavailable"):
                await case.store.get_plan_in_connection(db, scope=case.scope, turn_id="t_ref")
            with pytest.raises(ValueError, match="source_unavailable"):
                await case.store.get_encrypted_content_in_connection(db, scope=case.scope, turn_id="t_ref", content_key=case.descriptor.content_key)
        with pytest.raises(ValueError, match="source_unavailable"):
            await case.store.get_queued_input_in_connection(db, scope=case.scope, submission_id=SUBMISSION_ID)
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT retention_state,ciphertext IS NOT NULL FROM chat_reference_queued_inputs").fetchone() == ("available", 1)
    assert await purge_expired_reference_material(case.path) == ReferenceRetentionResult(0 if plain_parent else 1, 1)
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT state,ciphertext FROM chat_reference_queued_inputs").fetchone() == ("cancelled", None)


async def test_new_material_uses_current_version_and_preserves_original_expiry(case, monkeypatch):
    await _old_material(case, monkeypatch)
    await _set(case, 1, 0)
    await _set(case, 180, 1)
    await _second_plan(case)
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT retention_policy_version FROM chat_reference_plans WHERE turn_id='t_other'").fetchone() == (2,)
        assert db.execute("SELECT deadline=julianday(?) FROM chat_reference_plan_retention WHERE turn_id='t_other'", (case.expires,)).fetchone() == (
            1,
        )
        with pytest.raises(sqlite3.IntegrityError, match="version is immutable"):
            db.execute("UPDATE chat_reference_plans SET retention_policy_version=2 WHERE turn_id='t_ref'")
    assert await purge_expired_reference_material(case.path) == ReferenceRetentionResult(1, 1)
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN")
        assert await case.store.get_plan_in_connection(db, scope=case.scope.model_copy(update={"session_id": "s_other"}), turn_id="t_other")


@pytest.mark.parametrize("kind", ["plan", "queue"])
async def test_admission_captures_current_policy_and_sql_rejects_stale_version(case, monkeypatch, kind):
    await _set(case, 7, 0)

    async def stale_policy(*args, **kwargs):
        return ReferenceRetentionPolicy()

    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        if kind == "queue":
            await _receipt(db, case)
        write = _plan if kind == "plan" else _queue
        with monkeypatch.context() as stale:
            stale.setattr("bowhead_core.reference_retention_policy.read_retention_policy_in_connection", stale_policy)
            with pytest.raises(sqlite3.IntegrityError, match="policy version conflict"):
                await write(db, case)
        assert await write(db, case)
        table = "chat_reference_plans" if kind == "plan" else "chat_reference_queued_inputs"
        async with db.execute(f"SELECT retention_policy_version FROM {table}") as cur:
            assert tuple(await cur.fetchone()) == (1,)
        view = "chat_reference_plan_retention" if kind == "plan" else "chat_reference_queue_retention"
        async with db.execute(f"SELECT abs(r.deadline-(julianday(s.created_at)+7))<0.000001 FROM {view} r,{table} s") as cur:
            assert tuple(await cur.fetchone()) == (1,)
        extra = ",row_version=row_version+1" if kind == "queue" else ""
        with pytest.raises(sqlite3.IntegrityError, match="version is immutable"):
            await db.execute(f"UPDATE {table} SET retention_policy_version=0{extra}")


async def test_late_sql_material_insert_cannot_bypass_policy_gate(case, monkeypatch):
    await _old_material(case, monkeypatch)
    with sqlite3.connect(case.path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM chat_reference_content_snapshots").fetchone())
        row["content_key"] = "skill:dupont:late"
        row["descriptor_json"] = json.dumps({**json.loads(row["descriptor_json"]), "content_key": row["content_key"]})
        sql = "INSERT INTO chat_reference_content_snapshots(" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")"
        db.execute(sql, tuple(row.values()))  # A valid late prepare before policy changes.
        db.rollback()
    await _set(case, 1, 0)
    with sqlite3.connect(case.path) as db, pytest.raises(sqlite3.IntegrityError, match="reference source unavailable"):
        db.execute(sql, tuple(row.values()))


async def test_policy_write_failure_rolls_back_audit_and_material_deadline(case, monkeypatch):
    from contextlib import asynccontextmanager

    import bowhead_core.reference_retention_policy as policy_module

    await _seed(case)

    @asynccontextmanager
    async def interrupted_commit(path):
        async with open_shared_ctx(path) as db:

            async def fail():
                raise asyncio.CancelledError()

            db.commit = fail
            yield db

    monkeypatch.setattr(policy_module, "open_shared_ctx", interrupted_commit)
    with pytest.raises(asyncio.CancelledError):
        await _set(case, 1, 0)
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_reference_retention_policies").fetchone() == (0,)
        assert db.execute("SELECT available,deadline=julianday(?) FROM chat_reference_plan_retention", (case.expires,)).fetchone() == (1, 1)


@pytest.mark.parametrize("stage", ["admitted", "running", "output"])
async def test_policy_expiry_stops_original_worker_and_recovery_closes_it(start_case, monkeypatch, stage):
    case = await _prepare_case(start_case)
    await case.fixture.users.set_team_creator(case.fixture.team_id, case.fixture.owner_id)
    with monkeypatch.context() as clock:
        clock.setattr("bowhead_core.chat_reference_storage._now", lambda: datetime.now(UTC) - timedelta(days=2))
        await case.store.finalize_started_chat_submission(**case.args)
    runtime = None if stage == "admitted" else await _runtime(case)
    if stage == "output":
        runner = SubagentRunner(
            router=_router(LLMResponse(content="盈利分析已完成。")), tool_registry=build_default_registry(), team_subagent_store=case.fixture.users
        )
        await runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus())
    await set_retention_policy(case.fixture.db_path, team_id=case.scope.team_id, user_id=case.fixture.owner_id, days=1, expected_version=0)
    if runtime is None:
        with pytest.raises(ValueError, match="source_unavailable"):
            await _runtime(case)
    else:
        assert await case.store.heartbeat_chat_spawn_dispatch(turn_id="t_reference", owner_id="worker-a") == "lost"
        assert not await runtime.persist_turn_state(
            store=case.store,
            turn_id="t_reference",
            status_value="completed",
            intent=None,
            final_answer="late",
            tokens_input=7,
            tokens_output=8,
            cost_usd=1,
            error=None,
            error_kind=None,
        )
        if stage == "output":
            with sqlite3.connect(case.fixture.db_path) as db, pytest.raises(sqlite3.IntegrityError, match="material unavailable"):
                db.execute("UPDATE turns SET status='completed' WHERE id='t_reference'")
    _expire(case)
    assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    assert len(_rows(case, "turn_agent_execution")) == 1
    assert _rows(case, "turn_agent_execution")[0]["status"] == ("completed" if stage == "output" else "failed")
    assert await purge_expired_reference_material(case.fixture.db_path) == ReferenceRetentionResult(1, 0)
