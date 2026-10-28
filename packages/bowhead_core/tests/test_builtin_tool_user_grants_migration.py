"""0116_builtin_tool_user_grants 最小升级测试：仅 core 重建表并保留存量行与索引、接受新 provider、授权随连接级联删除。"""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from test_artifact_workspace_cutover_migration import _config

MIGRATION = import_module("bowhead_core.migrations.versions.0116_builtin_tool_user_grants")
_TARGETS = ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"]


@pytest.mark.parametrize("target", _TARGETS)
def test_grant_tables_only_on_core_and_existing_connections_survive(tmp_path, target) -> None:
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    if target == "core":
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO teams(id,name,max_seats,default_user_tier,status,created_at) VALUES ('team_1','Team',1,'pro','active','t')")
            db.execute(
                "INSERT INTO team_builtin_tool(id,team_id,provider,display_name,endpoint,status,auth_ref,key_hint,"
                "scope_state,scope_json,scope_fingerprint,scope_checked_at,created_by,created_at,updated_at) VALUES "
                "('ibt_1','team_1','datarc','财务','https://datarc.example','enabled','cipher','hint',"
                "'verified','{\"workspaces\":[]}','fp','t2','usr_1','t','t')"
            )
            db.commit()
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)

    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if target != "core":
            assert not {"builtin_tool_user_grant", "builtin_tool_oauth_state"} & tables
            return
        assert db.execute(
            "SELECT provider,display_name,scope_state,scope_json,scope_fingerprint,scope_checked_at,config_json FROM team_builtin_tool WHERE id='ibt_1'"
        ).fetchone() == ("datarc", "财务", "verified", '{"workspaces":[]}', "fp", "t2", None)
        indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='team_builtin_tool'")}
        assert "idx_team_builtin_tool_team" in indexes

        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "INSERT INTO team_builtin_tool(id,team_id,provider,display_name,status,auth_ref,created_at,updated_at) "
            "VALUES ('ibt_2','team_1','google_workspace','Google','disabled','cipher','t','t')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO team_builtin_tool(id,team_id,provider,display_name,status,auth_ref,created_at,updated_at) "
                "VALUES ('ibt_3','team_1','gmail','Mail','disabled','cipher','t','t')"
            )
        db.execute(
            "INSERT INTO builtin_tool_user_grant(id,tool_id,team_id,user_id,provider_subject,provider_email,refresh_cipher,created_at,updated_at) "
            "VALUES ('btg_1','ibt_2','team_1','usr_1','sub','a@example.com','cipher','t','t')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO builtin_tool_user_grant(id,tool_id,team_id,user_id,provider_subject,provider_email,refresh_cipher,created_at,updated_at) "
                "VALUES ('btg_2','ibt_2','team_1','usr_1','sub','a@example.com','cipher','t','t')"
            )
        db.execute(
            "INSERT INTO builtin_tool_oauth_state(state_hash,tool_id,team_id,user_id,requested_scopes,redirect_uri,verifier_cipher,expires_at) "
            "VALUES ('h','ibt_2','team_1','usr_1','[]','https://x/cb','cipher','t')"
        )
        db.execute("DELETE FROM team_builtin_tool WHERE id='ibt_2'")
        assert db.execute("SELECT COUNT(*) FROM builtin_tool_user_grant").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM builtin_tool_oauth_state").fetchone() == (0,)
