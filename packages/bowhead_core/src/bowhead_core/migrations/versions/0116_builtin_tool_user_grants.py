"""Per-user OAuth grants for image-bundled third-party tools (Google Workspace first).

Rebuilds ``team_builtin_tool`` to admit provider ``google_workspace`` and a generic,
provider-owned ``config_json`` column (non-secret settings; ``scope_*`` stays the
Datarc key-scope snapshot). Adds ``builtin_tool_user_grant`` (one member's refresh
token for one team connection) and ``builtin_tool_oauth_state`` (single-use state).

仅 core 目标；其余七库 no-op。

Revision ID: 0116_builtin_tool_user_grants
Revises: 0115_session_attachments_artifact_index
Create Date: 2026-09-24
"""

from __future__ import annotations

from alembic import context

revision = "0116_builtin_tool_user_grants"
down_revision = "0115_session_attachments_artifact_index"
branch_labels = None
depends_on = None

_COLUMNS = (
    "id,team_id,provider,display_name,endpoint,status,auth_ref,key_hint,"
    "scope_state,scope_json,scope_fingerprint,scope_checked_at,created_by,created_at,updated_at"
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT builtin_tool_user_grants")
    try:
        db.exec_driver_sql(
            """CREATE TABLE team_builtin_tool_new (
              id TEXT PRIMARY KEY,
              team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
              provider TEXT NOT NULL CHECK(provider IN ('tavily','datarc','google_workspace')),
              display_name TEXT NOT NULL COLLATE NOCASE,
              endpoint TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'disabled' CHECK(status IN ('enabled','disabled')),
              auth_ref TEXT NOT NULL,
              key_hint TEXT,
              scope_state TEXT NOT NULL DEFAULT 'unknown' CHECK(scope_state IN ('unknown','verified','invalid')),
              scope_json TEXT,
              scope_fingerprint TEXT,
              scope_checked_at TEXT,
              config_json TEXT,
              created_by TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(team_id, provider, display_name)
            )"""
        )
        db.exec_driver_sql(f"INSERT INTO team_builtin_tool_new({_COLUMNS}) SELECT {_COLUMNS} FROM team_builtin_tool")  # noqa: S608 - static columns
        old_count = db.exec_driver_sql("SELECT COUNT(*) FROM team_builtin_tool").scalar_one()
        new_count = db.exec_driver_sql("SELECT COUNT(*) FROM team_builtin_tool_new").scalar_one()
        if old_count != new_count:
            raise RuntimeError("team_builtin_tool copy row count mismatch")
        db.exec_driver_sql("DROP TABLE team_builtin_tool")
        db.exec_driver_sql("ALTER TABLE team_builtin_tool_new RENAME TO team_builtin_tool")
        db.exec_driver_sql("CREATE INDEX idx_team_builtin_tool_team ON team_builtin_tool(team_id, status)")
        db.exec_driver_sql(
            """CREATE TABLE builtin_tool_user_grant (
              id TEXT PRIMARY KEY,
              tool_id TEXT NOT NULL REFERENCES team_builtin_tool(id) ON DELETE CASCADE,
              team_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              provider_subject TEXT NOT NULL,
              provider_email TEXT NOT NULL,
              granted_scopes TEXT NOT NULL DEFAULT '[]',
              refresh_cipher TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','needs_reauth')),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              last_used_at TEXT,
              UNIQUE(tool_id, user_id)
            )"""
        )
        db.exec_driver_sql("CREATE INDEX idx_builtin_tool_user_grant_user ON builtin_tool_user_grant(user_id, team_id)")
        db.exec_driver_sql(
            """CREATE TABLE builtin_tool_oauth_state (
              state_hash TEXT PRIMARY KEY,
              tool_id TEXT NOT NULL REFERENCES team_builtin_tool(id) ON DELETE CASCADE,
              team_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              requested_scopes TEXT NOT NULL,
              redirect_uri TEXT NOT NULL,
              verifier_cipher TEXT NOT NULL,
              expires_at TEXT NOT NULL
            )"""
        )
        db.exec_driver_sql("RELEASE SAVEPOINT builtin_tool_user_grants")
    except Exception:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT builtin_tool_user_grants")
        db.exec_driver_sql("RELEASE SAVEPOINT builtin_tool_user_grants")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
