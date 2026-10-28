"""0102 widens one model-context guard only for committed Prompt results."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0102_reference_mcp_prompt_model")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_model_context_upgrade_keeps_data_and_unrelated_schema(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    excluded = {MIGRATION.TRIGGER} if target == "core" else set()
    assert [r for r in schema(path) if r[1] not in excluded] == [r for r in previous if r[1] not in excluded]
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        if target == "core":
            actual = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (MIGRATION.TRIGGER,)).fetchone()[0]
            assert MIGRATION.PREVIOUS._normalized(actual) == MIGRATION.PREVIOUS._normalized(MIGRATION.DDL)


@pytest.mark.parametrize("failure", ["drift", "create"])
def test_prompt_model_upgrade_rejects_drift_and_rolls_back(tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute(f"DROP TRIGGER {MIGRATION.TRIGGER}")
            db.execute(f"CREATE TRIGGER {MIGRATION.TRIGGER} BEFORE INSERT ON users BEGIN SELECT 1; END")
    before, previous = snapshot(path), schema(path)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if failure == "create" and statement.strip().startswith(f"CREATE TRIGGER {MIGRATION.TRIGGER} "):
            raise RuntimeError("injected failure after create")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError):
        _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before and schema(path) == previous
