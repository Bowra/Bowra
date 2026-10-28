"""Document card associations preserve existing data on every database target."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0150_document_clarifications")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_document_card_upgrade_preserves_history_without_backfill(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO clarification_requests(request_id,turn_id,user_id,workspace_id,token,rationale,candidates_json,status,created_at,resume_context_json) "
                "VALUES ('legacy-card','legacy-turn','legacy-user','legacy-space','','Historical question','[]','pending','2026-01-01','{\"user_text\":\"Historical request\"}')"
            )
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    actual = snapshot(path)
    assert {name: actual[name] for name in before} == before
    after = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == after
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        if target == "core":
            assert db.execute("SELECT count(*) FROM clarification_document_continuations").fetchone()[0] == 0


def test_interrupted_document_card_schema_rolls_back_as_one_batch(tmp_path, monkeypatch):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision, "core")
    before = schema(path)
    from sqlalchemy.engine import Connection

    original = Connection.exec_driver_sql

    def fail_after_table(self, statement, *args, **kwargs):
        if statement.startswith("CREATE TRIGGER trg_document_clarification_admission"):
            raise RuntimeError("injected migration failure")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", fail_after_table)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        _upgrade(path, MIGRATION.revision, "core")
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
