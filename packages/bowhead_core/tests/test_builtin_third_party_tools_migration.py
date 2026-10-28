"""Built-in third-party tools schema migration contract."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from test_artifact_workspace_cutover_migration import _config

MIGRATION = import_module("bowhead_core.migrations.versions.0108_builtin_third_party_tools")
TABLE = "team_builtin_tool"


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_builtin_tool_migration_is_scoped_and_idempotent(tmp_path, target):
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        present = bool(db.execute("SELECT 1 FROM sqlite_schema WHERE name=?", (TABLE,)).fetchone())
        assert present == (target == "core")
        if target == "core":
            db.execute("INSERT INTO teams(id,name,max_seats,default_user_tier,status,created_at) VALUES ('team_1','Team',1,'pro','active','t')")
            db.execute("INSERT INTO team_builtin_tool VALUES ('ibt_1','team_1','tavily','enabled','cipher',NULL,'t','t')")
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("INSERT INTO team_builtin_tool VALUES ('ibt_2','team_1','tavily','enabled','cipher',NULL,'t','t')")
