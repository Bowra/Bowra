"""Prepare reservations without rewriting existing composition or source bytes."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_reference_model_context_migration import snapshot
from test_reference_primary_claim import _claim
from test_reference_primary_reader import accepted_case as _accepted_case
from test_reference_primary_reader import start_case as _start_case
from test_skill_invocation_migration import schema

accepted_case, start_case = _accepted_case, _start_case
MIGRATION = import_module("bowhead_core.migrations.versions.0111_reference_lead_delegation")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_data_and_is_idempotent(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    after = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == after
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("failure", [None, "drift", "replace"])
async def test_old_running_task_survives_upgrade_or_atomic_rollback(accepted_case, tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    await _claim(accepted_case)
    path = tmp_path / "before.db"
    _historical_fixture(accepted_case.fixture.db_path, path, MIGRATION.down_revision)
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute(f"DROP TRIGGER {MIGRATION.NAMES[0]}")
    before, previous = snapshot(path), schema(path)
    execute = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        if failure == "replace" and statement == MIGRATION.DDL[MIGRATION.NAMES[1]]:
            raise RuntimeError("injected migration failure")
        return execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    if failure:
        with pytest.raises(RuntimeError):
            _upgrade(path, MIGRATION.revision)
        assert schema(path) == previous
    else:
        _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before
