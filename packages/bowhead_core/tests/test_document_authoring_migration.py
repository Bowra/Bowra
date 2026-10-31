"""Authoring extends original ledgers and preserves every historical byte."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from sqlalchemy import create_engine
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_document_planning_continuation import planning
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0201_document_authoring")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_all_targets_preserve_rows_and_repeat_upgrade(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    assert {key: value for key, value in before.items() if key != "alembic_version"} == {
        key: value for key, value in after.items() if key != "alembic_version"
    }
    if target != "core":
        assert schema(path) == previous_schema
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == after


async def test_populated_legacy_rows_and_foreign_keys_survive_with_enforcement_enabled(ctx, tmp_path, monkeypatch):
    await planning(ctx)
    path = tmp_path / "historical.db"
    _historical_fixture(ctx.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as db:
        db.exec_driver_sql("PRAGMA foreign_keys=ON")
        monkeypatch.setattr(MIGRATION.context, "get_bind", lambda: db)
        monkeypatch.setattr(MIGRATION.context, "config", type("Config", (), {"attributes": {"bowhead_target": "core"}})(), raising=False)
        MIGRATION.upgrade()
        assert db.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert db.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    assert snapshot(path) == before


def test_partial_rebuild_rolls_back_original_rows_guards_and_constraints(tmp_path, monkeypatch):
    path = tmp_path / "rollback.db"
    _upgrade(path, MIGRATION.down_revision)
    before, before_schema = snapshot(path), schema(path)
    engine = create_engine(f"sqlite:///{path}")
    original = MIGRATION._rebuild

    def interrupted(db, table, expected):
        original(db, table, expected)
        if table == "document_model_calls":
            raise RuntimeError("Synthetic interrupted rebuild")

    monkeypatch.setattr(MIGRATION, "_rebuild", interrupted)
    with engine.connect() as db:
        monkeypatch.setattr(MIGRATION.context, "get_bind", lambda: db)
        monkeypatch.setattr(MIGRATION.context, "config", type("Config", (), {"attributes": {"bowhead_target": "core"}})(), raising=False)
        with pytest.raises(RuntimeError, match="Synthetic interrupted"):
            MIGRATION.upgrade()
    assert snapshot(path) == before
    assert schema(path) == before_schema
