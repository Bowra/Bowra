"""Prompt BI continuation changes two guards atomically, retaining prior data."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0104_reference_prompt_continuation")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_prompt_resume_upgrade_preserves_schema_and_rows(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    excluded = set(MIGRATION.CURRENT) if target == "core" else set()
    assert [r for r in schema(path) if r[1] not in excluded] == [r for r in previous if r[1] not in excluded]
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        if target == "core":
            for name, ddl in MIGRATION.DDL.items():
                actual = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
                assert MIGRATION.FOLLOW_UP._normalized(actual) == MIGRATION.FOLLOW_UP._normalized(ddl)


@pytest.mark.parametrize("name", list(MIGRATION.CURRENT))
@pytest.mark.parametrize("failure", ["drift", "create"])
def test_prompt_resume_upgrade_drift_or_failure_is_atomic(tmp_path, monkeypatch, name, failure):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute(f"DROP TRIGGER {name}")
            db.execute(f"CREATE TRIGGER {name} BEFORE INSERT ON users BEGIN SELECT 1; END")
    before, previous = snapshot(path), schema(path)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if failure == "create" and statement.strip().startswith(f"CREATE TRIGGER {name} "):
            raise RuntimeError("injected failure after create")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError):
        _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before and schema(path) == previous
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
