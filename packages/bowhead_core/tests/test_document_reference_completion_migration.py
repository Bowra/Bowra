"""Append the native completion proof without rewriting retained work."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0208_document_reference_completion")
pytestmark = pytest.mark.integration


def version(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT version_num FROM alembic_version").fetchone()[0]


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_native_completion_upgrade_preserves_data_and_other_guards(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        with sqlite3.connect(path) as db:
            db.execute("""INSERT INTO artifacts(id,filename,mime,size,source,path,created_at)
                VALUES ('art_historical_native','retained.pptx','application/octet-stream',5,'upload','/synthetic/retained','2026-10-01')""")
    before, previous_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    after, current_schema = snapshot(path), schema(path)
    assert version(path) == MIGRATION.revision
    assert all(after[table] == rows for table, rows in before.items())
    if target == "core":
        assert [row for row in current_schema if row[1] != MIGRATION.NAME] == [row for row in previous_schema if row[1] != MIGRATION.NAME]
        with sqlite3.connect(path) as db:
            installed = db.execute("SELECT sql FROM sqlite_schema WHERE name=?", (MIGRATION.NAME,)).fetchone()[0]
        assert MIGRATION._normalized(installed) == MIGRATION._normalized(MIGRATION.DDL)
    else:
        assert current_schema == previous_schema
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == after and schema(path) == current_schema and version(path) == MIGRATION.revision


@pytest.mark.parametrize("missing", [False, True])
def test_completion_guard_drift_refuses_upgrade_without_advancing_ledger(tmp_path, missing):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute(f"DROP TRIGGER {MIGRATION.NAME}")
        if not missing:
            db.execute(f"CREATE TRIGGER {MIGRATION.NAME} BEFORE UPDATE OF status ON turns WHEN 0 BEGIN SELECT RAISE(ABORT,'changed'); END")
    before, data = schema(path), snapshot(path)
    with pytest.raises(RuntimeError, match="Document Reference completion schema drift"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before and snapshot(path) == data and version(path) == MIGRATION.down_revision


@pytest.mark.parametrize("stage", ["DROP TRIGGER", "CREATE TRIGGER"])
def test_interrupted_completion_replacement_restores_guard_and_ledger(tmp_path, monkeypatch, stage):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before, data = schema(path), snapshot(path)
    original = Connection.exec_driver_sql

    def fault(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if sql.lstrip().startswith(f"{stage} {MIGRATION.NAME}"):
            raise RuntimeError("Injected completion replacement failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fault)
    with pytest.raises(RuntimeError, match="Injected completion replacement failure"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before and snapshot(path) == data and version(path) == MIGRATION.down_revision
