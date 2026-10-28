"""0090 adds a durable exact read-call/result journal without rewriting history."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0090_reference_mcp_calls")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_adds_only_the_core_mcp_call_journal(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    assert all(after[name] == rows for name, rows in before.items())
    assert set(after) - set(before) == ({MIGRATION.TABLE} if target == "core" else set())
    if target == "core":
        assert after[MIGRATION.TABLE] == []
    current = schema(path)
    added = {MIGRATION.TABLE, *MIGRATION.INDEXES, *MIGRATION.TRIGGERS} if target == "core" else set()
    added.update(row[1] for row in current if row[1].startswith(f"sqlite_autoindex_{MIGRATION.TABLE}_"))
    assert [row for row in current if row[1] not in added] == previous
    assert len(current) == len(previous) + len(added)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("failure", ["drift", "create"])
def test_call_journal_install_is_atomic(tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute(f"CREATE TABLE {MIGRATION.TABLE}(id TEXT)")
    before, previous = snapshot(path), schema(path)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if failure == "create" and statement.strip().startswith(f"CREATE TABLE {MIGRATION.TABLE} "):
            raise RuntimeError("injected MCP call migration failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError):
        _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before and schema(path) == previous
