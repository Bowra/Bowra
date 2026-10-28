"""Expand the existing encrypted checkpoint without losing old paused tasks."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0105_reference_attachment_checkpoint")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_attachment_checkpoint_upgrade_preserves_data_and_guards(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    excluded = {MIGRATION.TABLE, MIGRATION.PREVIOUS.CAPTURE} if target == "core" else set()
    assert [r for r in schema(path) if r[1] not in excluded] == [r for r in previous if r[1] not in excluded]
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        if target == "core":
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (MIGRATION.TABLE,)).fetchone()[0]
            assert "checkpoint_version IN (1,2,3)" in sql


@pytest.mark.parametrize("failure", ["drift", "copy", "drop", "rename", "scope"])
def test_attachment_checkpoint_failure_is_atomic(tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute(f"DROP TRIGGER {MIGRATION.READ.IMMUTABLE}")
    before, previous = snapshot(path), schema(path)
    original = Connection.exec_driver_sql
    prefix = {
        "copy": f"INSERT INTO {MIGRATION.SHADOW}",
        "drop": f"DROP TABLE {MIGRATION.TABLE}",
        "rename": f"ALTER TABLE {MIGRATION.SHADOW}",
        "scope": f"CREATE TRIGGER {MIGRATION.PREVIOUS.CAPTURE}",
    }.get(failure)

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if prefix and statement.strip().startswith(prefix):
            raise RuntimeError("injected migration failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError):
        _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before and schema(path) == previous
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
