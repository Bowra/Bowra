"""Session-scoped personal MCP allowance migration contract."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from test_artifact_workspace_cutover_migration import _config

MIGRATION = import_module("bowhead_core.migrations.versions.0101_personal_mcp_session_allowance")


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_session_allowance_migration_is_scoped_and_idempotent(tmp_path, target):
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        present = bool(db.execute("SELECT 1 FROM sqlite_schema WHERE name='personal_mcp_session_tool_allowance'").fetchone())
        assert present == (target == "core")
        if target == "core":
            db.execute("INSERT INTO personal_mcp_session_tool_allowance VALUES ('u_1','s_1','pcm_1','pmt_1','t')")
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("INSERT INTO personal_mcp_session_tool_allowance VALUES ('u_1','s_1','pcm_other','pmt_1','t')")
