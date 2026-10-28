"""0093 adds a Prompt metadata cache without rewriting source rows."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module(
    "bowhead_core.migrations.versions.0093_reference_mcp_prompt_cache",
)
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_adds_only_core_prompt_cache(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)
    if target == "core":
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO teams(id,name,status,created_at) VALUES ('team_prompt','Prompt','active','now')",
            )
            db.execute(
                "INSERT INTO team_tool_source(id,team_id,name,kind,base_url,created_at,updated_at) "
                "VALUES ('ots_prompt','team_prompt','Prompt server','mcp','https://example.com/mcp','now','now')",
            )

    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)

    if target == "core":
        with sqlite3.connect(path) as db:
            columns = {row[1]: (row[2], row[3], row[4]) for row in db.execute("PRAGMA table_info(team_tool_source)")}
            assert columns[MIGRATION.COLUMN] == ("TEXT", 1, "'[]'")
            assert db.execute(
                "SELECT prompts_schema_json FROM team_tool_source WHERE id='ots_prompt'",
            ).fetchone() == ("[]",)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    else:
        assert snapshot(path) == before
        assert schema(path) == previous


def test_prompt_cache_install_is_atomic_after_alter(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.down_revision, "core")
    before, previous = snapshot(path), schema(path)
    original = Connection.exec_driver_sql

    def fail_after_alter(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if statement.strip().startswith(
            f"ALTER TABLE {MIGRATION.TABLE} ADD COLUMN",
        ):
            raise RuntimeError("injected MCP Prompt cache migration failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail_after_alter)
    with pytest.raises(RuntimeError, match="injected MCP Prompt"):
        _upgrade(path, MIGRATION.revision, "core")

    assert snapshot(path) == before
    assert schema(path) == previous
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
