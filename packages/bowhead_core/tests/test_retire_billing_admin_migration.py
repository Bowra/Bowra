"""Migration compatibility: retain accounts and Space access while retiring billing authority."""

from __future__ import annotations

import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.credit_activation import credit_capabilities_for_role
from bowhead_core.migrations.baseline_schema import INITIAL_SCHEMA_B85
from bowhead_core.user_store import UserStore

from bowhead_core import migrations

MIGRATION = import_module("bowhead_core.migrations.versions.0097_retire_billing_admin")
pytestmark = pytest.mark.integration


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


async def test_upgrade_preserves_memberships_and_removes_old_authority(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO teams(id,name,created_at) VALUES ('team_a','A','2026-09-01')")
        for uid, role, status in (
            ("old", "billing_admin", "active"),
            ("removed", "billing_admin", "removed"),
            ("admin", "team_admin", "active"),
            ("auditor", "audit_admin", "active"),
        ):
            db.execute("INSERT INTO users(id,team_id,email,created_at) VALUES (?,'team_a',?,'2026-09-01')", (uid, f"{uid}@example.com"))
            db.execute(
                "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,'team_a',?,?,'2026-09-01')", (uid, uid, role, status)
            )
        db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES ('ws_a','team_a','Space','2026-09-01','2026-09-01')")
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES ('ws_a','old','admin','2026-09-01')")
        db.execute(
            "INSERT INTO team_invite_tokens(id,team_id,team_role,invited_email,token_hash,created_at,expires_at) VALUES ('inv','team_a','billing_admin','new@example.com','hash','2026-09-01','2099-01-01')"
        )
        db.execute(
            "INSERT INTO api_tokens(id,user_id,name,hash,role_snapshot,created_at) VALUES ('token','old','legacy','hash','billing_admin','2026-09-01')"
        )
        db.execute(
            "INSERT INTO terminal_auth_scope_epochs(scope_kind,team_id,scope_ref,epoch,updated_at) VALUES ('team_membership','team_a','team_a',3,'2026-09-01')"
        )
        db.execute(
            "INSERT INTO admin_audit_log(id,actor_user_id,team_id,action,scope,payload_json,ts) VALUES ('audit','old','team_a','role_change','team','{\"new_role\":\"billing_admin\"}','2026-09-01')"
        )
        before_indexes = db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='team_members' ORDER BY name").fetchall()
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.stamp(config, MIGRATION.down_revision)
    # Replay only this revision, then restore the ledger to head; later revisions (e.g. 0063 tables/MCP Prompt structural prechecks) may not rerun on upgraded databases.
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        # Verify replay reached this revision (checking after stamp head is tautological and meaningless).
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    command.stamp(config, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT user_id,role,status FROM team_members ORDER BY user_id").fetchall() == [
            ("admin", "team_admin", "active"),
            ("auditor", "audit_admin", "active"),
            ("old", "member", "active"),
            ("removed", "member", "removed"),
        ]
        assert db.execute("SELECT role FROM workspace_members WHERE user_id='old'").fetchone() == ("admin",)
        assert db.execute("SELECT team_role FROM team_invite_tokens").fetchone() == ("member",)
        assert db.execute("SELECT role_snapshot FROM api_tokens").fetchone() == ("member",)
        assert db.execute("SELECT epoch FROM terminal_auth_scope_epochs").fetchone() == (4,)
        assert db.execute("SELECT payload_json FROM admin_audit_log").fetchone() == ('{"new_role":"billing_admin"}',)
        assert (
            db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='team_members' ORDER BY name").fetchall() == before_indexes
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        for sql in [
            "UPDATE team_members SET role='billing_admin' WHERE user_id='old'",
            "INSERT INTO team_members(id,user_id,team_id,role,created_at) VALUES ('bad','other','team_a','billing_admin','2026-09-01')",
            "UPDATE team_invite_tokens SET team_role='billing_admin' WHERE id='inv'",
            "UPDATE api_tokens SET role_snapshot='billing_admin' WHERE id='token'",
        ]:
            with pytest.raises(sqlite3.IntegrityError, match="unsupported_team_role"):
                db.execute(sql)
    store = UserStore(path)
    role = await store.get_team_role("old", "team_a")
    assert role == "member"
    assert credit_capabilities_for_role(role).credits_read is False
    assert await store.get_member_role("ws_a", "old") == "admin"
    assert credit_capabilities_for_role(await store.get_team_role("admin", "team_a")).credit_grant is True


@pytest.mark.parametrize("target", list(INITIAL_SCHEMA_B85))
def test_upgrade_empty_targets(tmp_path: Path, target: str) -> None:
    config = _config(tmp_path / f"{target}.db", target)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
