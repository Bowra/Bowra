"""Power BI connections and DAX templates retain their identity during migration."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from bowhead_cli.migrate import upgrade as upgrade_database
from bowhead_core.llm_config_crypto import encrypt_llm_config
from bowhead_core.user_store import UserStore
from cryptography.fernet import Fernet
from sqlalchemy import event
from sqlalchemy.engine import Engine
from test_artifact_workspace_cutover_migration import _config

from bowhead_core import migrations

MIGRATION = import_module("bowhead_core.migrations.versions.0122_builtin_powerbi")


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
async def test_powerbi_migration_preserves_connections_templates_and_ciphertext(tmp_path, monkeypatch, target):
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    if target == "core":
        monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
        auth = encrypt_llm_config(
            {
                "tenant_id": "tenant",
                "client_id": "client",
                "client_secret": "secret",
                "workspace_id": "workspace",
                "dataset_id": "dataset",
            }
        )
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO teams(id,name,max_seats,default_user_tier,status,created_at) VALUES ('team_1','Team',1,'pro','active','t')")
            db.execute("INSERT INTO pbi_connection VALUES (?,?,?,?,?,?,?,?)", ("pbi_one", "team_1", "财务", auth, "cret", "active", "t", "t"))
            db.execute("INSERT INTO pbi_connection VALUES (?,?,?,?,?,?,?,?)", ("pbi_two", "team_1", "财务", auth, "cret", "archived", "t", "t"))
            db.execute("INSERT INTO pbi_dax_template VALUES (?,?,?,?,?,?,?)", ("tpl_1", "team_1", "pbi_one", "本月", 'EVALUATE ROW("v",1)', "t", "t"))
            db.execute(
                """INSERT INTO team_builtin_tool(id,team_id,provider,display_name,endpoint,status,auth_ref,created_at,updated_at)
                   VALUES('ibt_google','team_1','google_workspace','Google','', 'enabled', ?, 't','t')""",
                (auth,),
            )
            db.execute(
                """INSERT INTO builtin_tool_user_grant
                   (id,tool_id,team_id,user_id,provider_subject,provider_email,refresh_cipher,created_at,updated_at)
                   VALUES('grant_1','ibt_google','team_1','user_1','subject','u@example.com','cipher','t','t')"""
            )
            db.commit()

    def enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    if target == "core":
        event.listen(Engine, "connect", enable_foreign_keys)
    try:
        command.upgrade(config, MIGRATION.revision)
        command.upgrade(config, MIGRATION.revision)
    finally:
        if target == "core":
            event.remove(Engine, "connect", enable_foreign_keys)
    if target != "core":
        return
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT id,display_name,status,auth_ref FROM team_builtin_tool WHERE provider='powerbi' ORDER BY id").fetchall()
        assert [(row[0], row[2]) for row in rows] == [("pbi_one", "enabled"), ("pbi_two", "disabled")]
        assert rows[0][3] == auth
        assert rows[0][1] != rows[1][1]
        assert db.execute("SELECT id,tool_id,name FROM builtin_pbi_template").fetchone() == ("tpl_1", "pbi_one", "本月")
        assert db.execute("SELECT id,tool_id FROM builtin_tool_user_grant").fetchone() == ("grant_1", "ibt_google")
    store = UserStore(path)
    assert (await store.get_team_builtin_tool_instance_auth_decrypted("team_1", "pbi_one"))["client_secret"] == "secret"
    assert len(await store.list_builtin_pbi_templates("team_1", "pbi_one")) == 1
    assert await store.delete_team_builtin_tool_instance("team_1", "pbi_one")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM builtin_pbi_template").fetchone()[0] == 0
        assert db.execute("SELECT id FROM builtin_tool_user_grant").fetchone() == ("grant_1",)


@pytest.mark.asyncio
async def test_powerbi_migration_accepts_legacy_delegated_registry_at_0121(tmp_path, monkeypatch):
    """An old token_hint registry must not be interpreted as SP credentials."""
    path = tmp_path / "bowhead.db"
    config = _config(path, "core")
    command.upgrade(config, MIGRATION.down_revision)
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    delegated_auth = encrypt_llm_config({"refresh_token": "delegated-token", "client_id": "client"})
    with sqlite3.connect(path) as db:
        db.executescript(
            """DROP TABLE pbi_dax_template;
               DROP TABLE pbi_connection;
               CREATE TABLE pbi_connection (
                 id TEXT PRIMARY KEY, team_id TEXT NOT NULL, name TEXT NOT NULL,
                 tenant_id TEXT NOT NULL DEFAULT '', workspace_id TEXT NOT NULL,
                 dataset_id TEXT NOT NULL, master_upn TEXT NOT NULL DEFAULT '',
                 per_user_rls INTEGER NOT NULL DEFAULT 0, auth_ref TEXT,
                 token_hint TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
                 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
               );
               CREATE TABLE pbi_dax_template (
                 id TEXT PRIMARY KEY, team_id TEXT NOT NULL, connection_id TEXT NOT NULL,
                 name TEXT NOT NULL, dax_template TEXT NOT NULL,
                 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
               );"""
        )
        db.execute("INSERT INTO teams(id,name,max_seats,default_user_tier,status,created_at) VALUES ('team_1','Team',1,'pro','active','t')")
        db.execute(
            """INSERT INTO pbi_connection
               (id,team_id,name,tenant_id,workspace_id,dataset_id,auth_ref,token_hint,status,created_at,updated_at)
               VALUES('legacy_pbi','team_1','Legacy','tenant','workspace','dataset',?,'oken','active','t','t')""",
            (delegated_auth,),
        )
        db.execute("INSERT INTO pbi_dax_template VALUES ('tpl_1','team_1','legacy_pbi','旧模板','EVALUATE ROW(\"v\",1)','t','t')")
        db.execute(
            """INSERT INTO team_builtin_tool(id,team_id,provider,display_name,endpoint,status,auth_ref,created_at,updated_at)
               VALUES('ibt_google','team_1','google_workspace','Google','', 'enabled', ?, 't','t')""",
            (delegated_auth,),
        )
        db.execute(
            """INSERT INTO builtin_tool_user_grant
               (id,tool_id,team_id,user_id,provider_subject,provider_email,refresh_cipher,created_at,updated_at)
               VALUES('grant_1','ibt_google','team_1','user_1','subject','u@example.com','cipher','t','t')"""
        )
        db.commit()

    def enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    event.listen(Engine, "connect", enable_foreign_keys)
    try:
        db_url = f"sqlite+aiosqlite:///{path}"
        assert upgrade_database(db_url, target="core") == ("core",)
        assert upgrade_database(db_url, target="core") == ("core",)
    finally:
        event.remove(Engine, "connect", enable_foreign_keys)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (migrations.chain_head(),)
        assert db.execute("SELECT COUNT(*) FROM team_builtin_tool WHERE provider='powerbi'").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM builtin_pbi_template").fetchone() == (0,)
        assert db.execute("SELECT id FROM team_builtin_tool WHERE provider='google_workspace'").fetchone() == ("ibt_google",)
        assert db.execute("SELECT id,tool_id FROM builtin_tool_user_grant").fetchone() == ("grant_1", "ibt_google")
        assert db.execute("SELECT name FROM sqlite_master WHERE name='pbi_connection'").fetchone() is None
    tool = await UserStore(path).create_team_builtin_tool_instance(
        team_id="team_1",
        provider="powerbi",
        display_name="New",
        endpoint="",
        auth={"tenant_id": "tenant", "client_id": "client", "client_secret": "secret", "workspace_id": "workspace", "dataset_id": "dataset"},
        key_hint="cret",
        status="enabled",
        created_by=None,
    )
    assert tool.provider == "powerbi"
