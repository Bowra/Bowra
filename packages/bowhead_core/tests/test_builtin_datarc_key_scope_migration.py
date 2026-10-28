"""Built-in Datarc key-scope persistence migration contract."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from test_artifact_workspace_cutover_migration import _config

MIGRATION = import_module("bowhead_core.migrations.versions.0110_builtin_datarc_key_scope")


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_datarc_scope_migration_is_scoped_and_marks_existing_rows_unknown(tmp_path, target):
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    if target == "core":
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO teams(id,name,max_seats,default_user_tier,status,created_at) VALUES ('team_1','Team',1,'pro','active','t')")
            db.execute(
                "INSERT INTO team_builtin_tool VALUES "
                "('ibt_1','team_1','datarc','财务','https://datarc.example','enabled','cipher','hint',NULL,'t','t')"
            )
            db.commit()
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        present = bool(db.execute("SELECT 1 FROM sqlite_schema WHERE name='team_builtin_tool'").fetchone())
        assert present == (target == "core")
        if target != "core":
            return
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(team_builtin_tool)")}
        assert {"scope_state", "scope_json", "scope_fingerprint", "scope_checked_at"}.issubset(columns)
        assert db.execute("SELECT scope_state,scope_json,scope_fingerprint,scope_checked_at FROM team_builtin_tool WHERE id='ibt_1'").fetchone() == (
            "unknown",
            None,
            None,
            None,
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE team_builtin_tool SET scope_state='bad' WHERE id='ibt_1'")
