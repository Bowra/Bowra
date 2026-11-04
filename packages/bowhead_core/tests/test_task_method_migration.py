"""Task method guards preserve historical authority and all existing ledgers."""

import sqlite3
from importlib import import_module

import pytest
from core_legacy_database import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0214_task_method_references")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_task_method_upgrade_preserves_all_rows_and_unrelated_schema(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    data, before = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    after = schema(path)
    assert snapshot(path) == data
    changed = {*MIGRATION.CURRENT, MIGRATION.METHOD}
    if target == "core":
        assert [row for row in before if row[1] not in changed] == [row for row in after if row[1] not in changed]
        with sqlite3.connect(path) as db:
            for name, expected in {**MIGRATION.DDL, MIGRATION.METHOD: MIGRATION.METHOD_DDL}.items():
                (sql,) = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
                assert MIGRATION._normalized(sql) == MIGRATION._normalized(expected)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    else:
        assert after == before
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == after and snapshot(path) == data


@pytest.mark.parametrize("drift", [*MIGRATION.CURRENT, MIGRATION.METHOD])
def test_task_method_upgrade_rejects_unknown_guard_definitions(tmp_path, drift):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if drift == MIGRATION.METHOD:
            db.execute(f"CREATE TRIGGER {drift} BEFORE INSERT ON turns BEGIN SELECT 1; END")
        else:
            db.execute(f"DROP TRIGGER {drift}")
            db.execute(f"CREATE TRIGGER {drift} BEFORE INSERT ON turns BEGIN SELECT 1; END")
    before, data = schema(path), snapshot(path)
    with pytest.raises(RuntimeError, match="task method schema drift"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before and snapshot(path) == data
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.down_revision


@pytest.mark.parametrize("stage", [*MIGRATION.DDL, MIGRATION.METHOD])
def test_task_method_guard_interruption_rolls_back_atomically(tmp_path, monkeypatch, stage):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before, data = schema(path), snapshot(path)
    original = MIGRATION.context.get_bind

    class Interrupted:
        def __init__(self, connection):
            self.connection = connection

        def exec_driver_sql(self, sql, *args):
            result = self.connection.exec_driver_sql(sql, *args)
            if sql.split()[:3] == ["CREATE", "TRIGGER", stage]:
                raise RuntimeError("synthetic guard interruption")
            return result

    monkeypatch.setattr(MIGRATION.context, "get_bind", lambda: Interrupted(original()))
    with pytest.raises(RuntimeError, match="synthetic guard interruption"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before and snapshot(path) == data
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.down_revision
