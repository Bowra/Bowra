"""Model-context storage upgrades preserve existing records and rollback atomically."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0206_model_context")
TABLES = {
    "continuation_payload_segments",
    "model_context_segments",
    "model_context_generations",
    "model_context_heads",
    "model_context_conversation_heads",
}
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_model_context_upgrade_preserves_prior_records_and_repeats(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    assert {name: after[name] for name in before} == before
    assert set(after) - set(before) == (TABLES if target == "core" else set())
    assert all(after[name] == [] for name in set(after) - set(before))
    installed = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == installed
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_model_context_failed_install_restores_schema_and_ledger(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before = schema(path)
    original = Connection.exec_driver_sql

    def fault(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if sql.startswith("CREATE TRIGGER trg_model_context_session_purge"):
            raise RuntimeError("injected context migration failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fault)
    with pytest.raises(RuntimeError, match="injected context migration failure"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before
