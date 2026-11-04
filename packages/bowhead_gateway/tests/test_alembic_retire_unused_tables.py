"""Retirement removes only obsolete data and its completed erasure inventory."""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

from bowhead_core import migrations

MIGRATION = importlib.import_module("bowhead_core.migrations.versions.0219_retire_unused_tables")


def test_retirement_is_defensive_and_preserves_active_data(tmp_path: Path) -> None:
    path = tmp_path / "bowhead.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE "artifact_stdout"')
        connection.execute("CREATE TABLE retirement_probe(identity TEXT PRIMARY KEY, payload BLOB)")
        connection.execute("INSERT INTO retirement_probe VALUES (?,?)", ("retained", b"\x00\xff"))
        # This fixture deliberately leaves foreign keys off: old frozen inventory
        # may outlive its original task, but retirement must only finish known items.
        connection.execute("INSERT INTO account_deletion_rows VALUES (?,?,?)", ("task", "business_rules", '["old"]'))
        connection.execute("INSERT INTO account_deletion_rows VALUES (?,?,?)", ("task", "artifacts", '["active"]'))
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert not set(MIGRATION._RETIRED_TABLES) & tables
        assert {"artifacts", "personal_mcp_binding", "team_tool_source", "team_builtin_tool", "builtin_pbi_template"} <= tables
        assert connection.execute("SELECT * FROM retirement_probe").fetchall() == [("retained", b"\x00\xff")]
        assert connection.execute("SELECT table_name,row_key FROM account_deletion_rows").fetchall() == [("artifacts", '["active"]')]
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
