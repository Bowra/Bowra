"""0087 expands only exact automatic Capability admission/composition guards."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0087_reference_capability")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_rows_and_unrelated_schema(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    changed = {MIGRATION.SOURCE_TRIGGER, MIGRATION.COMPOSITION_TRIGGER, MIGRATION.CAPABILITY_TRIGGER} if target == "core" else set()
    assert [row for row in schema(path) if row[1] not in changed] == [row for row in previous if row[1] not in changed]
    assert len(schema(path)) == len(previous) + (1 if target == "core" else 0)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("failure", ["drift", MIGRATION.SOURCE_TRIGGER, MIGRATION.COMPOSITION_TRIGGER, MIGRATION.CAPABILITY_TRIGGER])
def test_guard_replacement_is_atomic(tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute(f"DROP TRIGGER {MIGRATION.SOURCE_TRIGGER}")
    before, previous = snapshot(path), schema(path)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if statement.strip().startswith(f"CREATE TRIGGER {failure} "):
            raise RuntimeError("injected Capability migration failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError):
        _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before and schema(path) == previous
