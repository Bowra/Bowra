"""The post-dispatch lease guard is append-only and migrates atomically."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0112_reference_lead_postdispatch")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_existing_data_and_is_idempotent(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after_data = snapshot(path)
    assert {name: after_data[name] for name in before} == before
    after = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == after
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        if target == "core":
            assert db.execute("SELECT COUNT(*) FROM chat_reference_lead_postdispatch_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["drift", "replace"])
def test_upgrade_rejects_drift_and_rolls_back_partial_replacement(tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.down_revision, "core")
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER trg_reference_model_context_scope")
    before = schema(path)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        if failure == "replace" and statement == MIGRATION.DDL["trg_chat_reference_evidence_scope"]:
            raise RuntimeError("injected migration failure")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError):
        _upgrade(path, MIGRATION.revision, "core")
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='chat_reference_lead_postdispatch_attempts'").fetchone() is None
