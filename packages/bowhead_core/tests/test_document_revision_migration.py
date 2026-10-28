"""Additive revision lineage preserves all target ledgers and old document data."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _historical_fixture, _upgrade, assert_rows_survive_upgrade
from test_document_delivery import _review
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0148_document_revisions")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_revision_upgrade_preserves_target_data_and_repeats(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, old_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert_rows_survive_upgrade(before, snapshot(path))
    current = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == current
    if target != "core":
        assert current == old_schema


async def test_revision_upgrade_preserves_historical_review_and_private_files(ctx, tmp_path):
    await _review(ctx)
    path = tmp_path / "historical.db"
    _historical_fixture(ctx.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision)
    assert_rows_survive_upgrade(before, snapshot(path))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM document_jobs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM document_revisions").fetchone()[0] == 0
        assert db.execute("SELECT state FROM document_executions").fetchone()[0] == "review"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_revision_schema_failure_restores_original_admission_trigger(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before = schema(path)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if statement == "DROP TRIGGER trg_document_input_copy_admission":
            raise RuntimeError("Injected revision migration failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError, match="Injected revision"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before


def test_revision_upgrade_rejects_admission_schema_drift_without_changes(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER trg_document_input_copy_admission")
    before = schema(path)
    with pytest.raises(RuntimeError, match="schema drift"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before
