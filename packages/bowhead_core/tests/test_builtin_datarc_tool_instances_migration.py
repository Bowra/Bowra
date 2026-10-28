"""Multi-instance Datarc built-in tool migration contract."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from bowhead_core.user_store import UserStore
from cryptography.fernet import Fernet
from test_artifact_workspace_cutover_migration import _config

MIGRATION = import_module("bowhead_core.migrations.versions.0109_builtin_datarc_tool_instances")


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_builtin_datarc_instance_migration_is_scoped_and_preserves_tavily(tmp_path, target):
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    if target == "core":
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO teams(id,name,max_seats,default_user_tier,status,created_at) VALUES ('team_1','Team',1,'pro','active','t')")
            db.execute("INSERT INTO team_builtin_tool VALUES ('ibt_t','team_1','tavily','enabled','cipher',NULL,'t','t')")
            db.commit()
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        present = bool(db.execute("SELECT 1 FROM sqlite_schema WHERE name='team_builtin_tool'").fetchone())
        assert present == (target == "core")
        if target != "core":
            return
        assert db.execute("SELECT provider,display_name,endpoint,auth_ref FROM team_builtin_tool WHERE id='ibt_t'").fetchone() == (
            "tavily",
            "Tavily",
            "https://api.tavily.com",
            "cipher",
        )
        db.execute(
            """INSERT INTO team_builtin_tool VALUES
               ('ibt_1','team_1','datarc','财务','https://one.example','enabled','cipher1','1111',NULL,'t','t'),
               ('ibt_2','team_1','datarc','销售','https://two.example','enabled','cipher2','2222',NULL,'t','t')"""
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO team_builtin_tool VALUES ('ibt_3','team_1','datarc','财务','https://three.example','enabled','c','3333',NULL,'t','t')"
            )


@pytest.mark.asyncio
async def test_builtin_datarc_instances_round_trip_encrypted_credentials(tmp_path, monkeypatch):
    path = tmp_path / "core.db"
    config = _config(path, "core")
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO teams(id,name,max_seats,default_user_tier,status,created_at) VALUES ('team_1','Team',1,'pro','active','t')")
        db.commit()
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(path)

    first = await store.create_team_builtin_tool_instance(
        team_id="team_1",
        provider="datarc",
        display_name="财务",
        endpoint="https://one.example",
        auth={"connection_key": "secret-one"},
        key_hint="-one",
        status="enabled",
        created_by="user_1",
        scope_state="verified",
        scope={"key_id": "key-1", "permissions": ["account", "workspace"], "workspaces": []},
        scope_fingerprint="sha256:test",
        scope_checked_at="2026-09-23T00:00:00+00:00",
    )
    second = await store.create_team_builtin_tool_instance(
        team_id="team_1",
        provider="datarc",
        display_name="销售",
        endpoint="https://two.example",
        auth={"connection_key": "secret-two"},
        key_hint="-two",
        status="enabled",
        created_by="user_1",
    )

    listed = await store.list_team_builtin_tools("team_1")
    assert [tool.display_name for tool in listed] == ["财务", "销售"]
    assert listed[0].scope == {"key_id": "key-1", "permissions": ["account", "workspace"], "workspaces": []}
    assert listed[0].scope_fingerprint == "sha256:test"
    assert await store.get_team_builtin_tool_instance_auth_decrypted("team_1", first.id) == {"connection_key": "secret-one"}
    assert await store.get_team_builtin_tool_instance("other_team", first.id) is None
    updated = await store.update_team_builtin_tool_instance(
        team_id="team_1",
        tool_id=second.id,
        display_name="销售分析",
        status="disabled",
    )
    assert updated is not None
    assert (updated.display_name, updated.status) == ("销售分析", "disabled")
    assert await store.delete_team_builtin_tool_instance("team_1", first.id) is True
    assert await store.get_team_builtin_tool_instance("team_1", first.id) is None
