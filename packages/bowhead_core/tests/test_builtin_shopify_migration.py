"""Shopify migration preserves all existing native connection dependencies."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from sqlalchemy import event
from sqlalchemy.engine import Engine
from test_artifact_workspace_cutover_migration import _config

MIGRATION = import_module("bowhead_core.migrations.versions.0162_builtin_shopify")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_upgrade_preserves_native_rows_and_advances_every_target(tmp_path, target):
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    expected = {}
    if target == "core":
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO teams(id,name,status,created_at) VALUES ('team_1','Team','active','t')")
            for provider in ("tavily", "datarc", "google_workspace", "powerbi"):
                db.execute(
                    """INSERT INTO team_builtin_tool(id,team_id,provider,display_name,auth_ref,created_at,updated_at)
                    VALUES(?, 'team_1', ?, ?, 'FERNET:retained-bytes', 't','t')""",
                    (provider, provider, provider),
                )
            db.execute("""INSERT INTO builtin_tool_user_grant(id,tool_id,team_id,user_id,provider_subject,provider_email,refresh_cipher,created_at,updated_at)
                VALUES('grant','google_workspace','team_1','user','subject','mail','cipher','t','t')""")
            db.execute("INSERT INTO builtin_tool_oauth_state VALUES('state','google_workspace','team_1','user','[]','uri','cipher','later')")
            db.execute("INSERT INTO builtin_pbi_template VALUES('template','team_1','powerbi','Template','EVALUATE X','t','t')")
            for table in ("team_builtin_tool", "builtin_tool_user_grant", "builtin_tool_oauth_state", "builtin_pbi_template"):
                expected[table] = db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()

    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    event.listen(Engine, "connect", foreign_keys)
    try:
        command.upgrade(config, MIGRATION.revision)
        command.upgrade(config, MIGRATION.revision)
    finally:
        event.remove(Engine, "connect", foreign_keys)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision
        if target != "core":
            assert not db.execute("SELECT name FROM sqlite_master WHERE name='team_builtin_tool'").fetchall()
            return
        for table, rows in expected.items():
            current = db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            assert [row[:-1] for row in current] == rows if table == "team_builtin_tool" else current == rows
        db.execute("UPDATE team_builtin_tool SET status='enabled' WHERE id='powerbi'")
        db.execute("UPDATE team_builtin_tool SET status='disabled' WHERE id='powerbi'")
        assert db.execute("SELECT connection_revision FROM team_builtin_tool WHERE id='powerbi'").fetchone()[0] == 3
        db.execute("""INSERT INTO team_builtin_tool(id,team_id,provider,display_name,auth_ref,created_at,updated_at)
            VALUES('shopify','team_1','shopify','Commerce','cipher','t','t')""")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("""INSERT INTO team_builtin_tool(id,team_id,provider,display_name,auth_ref,created_at,updated_at)
                VALUES('unknown','team_1','unknown','Unknown','cipher','t','t')""")
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_failed_copy_leaves_existing_schema_and_ciphertext_intact(tmp_path):
    path = tmp_path / "core.db"
    config = _config(path, "core")
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO teams(id,name,status,created_at) VALUES ('team','Team','active','t')")
        db.execute("""INSERT INTO team_builtin_tool(id,team_id,provider,display_name,auth_ref,created_at,updated_at)
            VALUES('datarc','team','datarc','Datarc','cipher','t','t')""")

    def fail_install(connection, _cursor, statement, _parameters, _context, _many):
        if "CREATE TRIGGER trg_builtin_connection_revision" in statement:
            raise RuntimeError("Controlled migration interruption")

    event.listen(Engine, "before_cursor_execute", fail_install)
    try:
        with pytest.raises(RuntimeError, match="Controlled migration interruption"):
            command.upgrade(config, MIGRATION.revision)
    finally:
        event.remove(Engine, "before_cursor_execute", fail_install)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.down_revision
        assert db.execute("SELECT auth_ref FROM team_builtin_tool").fetchone()[0] == "cipher"
        assert "connection_revision" not in {row[1] for row in db.execute("PRAGMA table_info(team_builtin_tool)")}
    command.upgrade(config, MIGRATION.revision)
