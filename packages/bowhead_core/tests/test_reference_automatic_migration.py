"""0084 preserves the existing primary ledger and atomically expands its origin."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import restore
from test_skill_reference_claim import start_case as _start_case

claimed_case, receiver_case, start_case = _claimed_case, _receiver_case, _start_case
MIGRATION = import_module("bowhead_core.migrations.versions.0084_reference_auto_receiver")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_all_rows_and_unrelated_schema(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        if target == "core":
            table = db.execute("SELECT sql FROM sqlite_master WHERE name='turn_agent_binding'").fetchone()[0]
            assert "'schedule','plan','lead'" in table
    if target == "core":
        changed = {MIGRATION.TABLE, MIGRATION.GUARD, "trg_turn_agent_binding_primary_execution"}
        assert [r for r in schema(path) if r[1] not in changed] == [r for r in previous if r[1] not in changed]
        assert len(schema(path)) == len(previous) + 1
    else:
        assert schema(path) == previous


@pytest.mark.parametrize("change", ["table", "trigger", "index", "collision"])
def test_old_schema_drift_does_not_start_a_partial_copy(tmp_path, change):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if change == "table":
            db.execute("ALTER TABLE turn_agent_binding ADD COLUMN unexpected TEXT")
        elif change == "trigger":
            db.execute("DROP TRIGGER trg_turn_agent_binding_primary_execution")
        elif change == "index":
            db.execute("DROP INDEX idx_turn_agent_binding_primary_execution")
        else:
            db.execute(f"CREATE TABLE {MIGRATION.TEMP}(value TEXT)")
    before, previous = snapshot(path), schema(path)
    with pytest.raises(RuntimeError, match="schema drift"):
        _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before and schema(path) == previous


@pytest.mark.parametrize(
    "stage",
    [
        "INSERT INTO turn_agent_binding__auto_receiver",
        "DROP TABLE turn_agent_binding",
        "ALTER TABLE turn_agent_binding__auto_receiver",
        "CREATE TRIGGER trg_reference_automatic_receiver_source",
    ],
)
def test_failure_after_each_copy_stage_restores_schema_and_revision(tmp_path, monkeypatch, stage):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before, previous = snapshot(path), schema(path)
    original = Connection.exec_driver_sql
    reached = []

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if statement.strip().startswith(stage):
            reached.append(stage)
            raise RuntimeError("injected copy failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError, match="injected copy failure"):
        _upgrade(path, MIGRATION.revision)
    assert reached == [stage]
    assert snapshot(path) == before and schema(path) == previous
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_previous_primary_and_encrypted_materials_survive_copy(claimed_case, tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    case = claimed_case
    assert (await claim_reference_skill_primary_for_worker(**case.args)).outcome == "claimed"
    expected = await restore(case)
    path = tmp_path / "previous.db"
    _historical_fixture(case.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if statement == "DROP TABLE turn_agent_binding":
            raise RuntimeError("injected populated copy failure")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "exec_driver_sql", fail)
        with pytest.raises(RuntimeError, match="injected populated copy failure"):
            _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before
    _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before
    # Current readers/writers run only at head (container startup first runs `upgrade head`); this revision was already asserted above.
    _upgrade(path)
    case.path = path
    assert await restore(case) == expected
