"""0095 adds atomic MCP Prompt result snapshot and resolution guards."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0095_reference_mcp_prompt_results")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_adds_only_core_prompt_result_guards(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    added = {MIGRATION.SNAPSHOT_TRIGGER, MIGRATION.RESOLUTION_TRIGGER} if target == "core" else set()
    replaced = {MIGRATION.AUTO_TRIGGER} if target == "core" else set()
    assert [row for row in schema(path) if row[1] not in added | replaced] == [row for row in previous if row[1] not in replaced]
    assert len(schema(path)) == len(previous) + len(added)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        if target == "core":
            installed = db.execute(
                "SELECT sql FROM sqlite_master WHERE name=?",
                (MIGRATION.AUTO_TRIGGER,),
            ).fetchone()[0]
            assert MIGRATION._normalized(installed) == MIGRATION._normalized(MIGRATION.AUTO_DDL)


@pytest.mark.parametrize("failure", ["drift", "first", "second"])
def test_prompt_result_guard_install_is_atomic(tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute(f"CREATE TRIGGER {MIGRATION.SNAPSHOT_TRIGGER} BEFORE INSERT ON users BEGIN SELECT 1; END")
    before, previous = snapshot(path), schema(path)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if failure == "first" and statement.strip().startswith(f"CREATE TRIGGER {MIGRATION.SNAPSHOT_TRIGGER} "):
            raise RuntimeError("injected Prompt result snapshot migration failure")
        if failure == "second" and statement.strip().startswith(f"CREATE TRIGGER {MIGRATION.RESOLUTION_TRIGGER} "):
            raise RuntimeError("injected Prompt result resolution migration failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError):
        _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before and schema(path) == previous
