"""Canvas input metadata expands only the original core document lifecycle."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0204_document_canvas_inputs")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_canvas_input_upgrade_preserves_rows_and_repeat_upgrade(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        with sqlite3.connect(path) as db:
            db.execute("""INSERT INTO artifacts(id,filename,mime,size,source,path,created_at)
                VALUES ('art_historical_canvas_input','original.csv','text/csv',5,'upload','/synthetic/original','2026-10-01')""")
    before, previous_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    assert all(after[table] == rows for table, rows in before.items())
    if target != "core":
        assert schema(path) == previous_schema
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        if target == "core":
            assert db.execute("SELECT count(*) FROM document_canvas_inputs").fetchone() == (0,)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == after


@pytest.mark.parametrize("trigger", ["trg_document_ref_admission", "trg_document_ref_release"])
def test_original_reference_guard_drift_rolls_back_new_schema(tmp_path, trigger):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute(f"DROP TRIGGER {trigger}")
        db.execute(f"CREATE TRIGGER {trigger} BEFORE INSERT ON document_artifact_refs WHEN 0 BEGIN SELECT RAISE(ABORT,'changed'); END")
    before = schema(path)
    with pytest.raises(RuntimeError, match="Document reference schema drift"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before


def test_interrupted_reference_expansion_rolls_back_as_one_batch(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before, data = schema(path), snapshot(path)
    original = Connection.exec_driver_sql

    def fault(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if sql.startswith("CREATE TRIGGER trg_document_ref_release"):
            raise RuntimeError("Injected reference installation failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fault)
    with pytest.raises(RuntimeError, match="Injected"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before and snapshot(path) == data
