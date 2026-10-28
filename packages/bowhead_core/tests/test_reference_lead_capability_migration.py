"""The image Lead schema preserves historical rows and upgrades atomically."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_reference_model_context_migration import snapshot
from test_reference_primary_reader import accepted_case as _accepted_case
from test_reference_primary_reader import start_case as _start_case
from test_skill_invocation_migration import schema

accepted_case, start_case = _accepted_case, _start_case
MIGRATION = import_module("bowhead_core.migrations.versions.0125_reference_lead_capability")
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
        if target == "core":
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name='trg_reference_lead_capability_plan'").fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
async def test_existing_reference_task_keeps_its_source_and_execution(accepted_case, tmp_path):
    path = tmp_path / "existing.db"
    _historical_fixture(accepted_case.fixture.db_path, path, MIGRATION.down_revision)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before


@pytest.mark.parametrize("failure", ["drift", "replace"])
def test_upgrade_rejects_drift_and_rolls_back(tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.down_revision, "core")
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute(f"DROP TRIGGER {MIGRATION.LEAD.NAME}")
    before = schema(path)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        if failure == "replace" and statement == MIGRATION.CAPABILITY_DDL:
            raise RuntimeError("injected migration failure")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError):
        _upgrade(path, MIGRATION.revision, "core")
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='trg_reference_lead_capability_plan'").fetchone() is None
