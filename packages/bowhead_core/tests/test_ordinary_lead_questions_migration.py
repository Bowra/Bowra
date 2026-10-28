"""The ordinary checkpoint migration advances every ledger without backfill."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0152_ordinary_lead_questions")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_old_cards_and_advances_each_target(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO clarification_requests(request_id,turn_id,user_id,workspace_id,token,rationale,candidates_json,status,created_at,resume_context_json) "
                "VALUES ('old-card','old-turn','old-user','old-space','','Saved question','[]','pending','2026-01-01','{\"user_text\":\"Original request\"}')"
            )
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    if target == "core":
        assert all(row[-2:] == (None, None) for row in after["clarification_requests"])
        after["clarification_requests"] = [row[:-2] for row in after["clarification_requests"]]
    assert after == before
    installed = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == installed
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_guard_installation_rolls_back_both_columns(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision, "core")
    before = schema(path)
    execute = Connection.exec_driver_sql

    def interrupted(self, statement, *args, **kwargs):
        if statement.startswith("CREATE TRIGGER trg_ordinary_question_identity"):
            raise RuntimeError("injected migration failure")
        return execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", interrupted)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        _upgrade(path, MIGRATION.revision, "core")
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
