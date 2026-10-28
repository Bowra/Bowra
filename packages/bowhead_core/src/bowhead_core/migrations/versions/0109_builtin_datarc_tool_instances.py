"""Allow multiple configured instances of image-bundled third-party tools.

Revision ID: 0109_builtin_datarc_tool_instances
Revises: 0108_builtin_third_party_tools
"""
from __future__ import annotations

from alembic import context

revision = "0109_builtin_datarc_tool_instances"
down_revision = "0108_builtin_third_party_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT builtin_datarc_instances")
    try:
        db.exec_driver_sql(
            """CREATE TABLE team_builtin_tool_new (
              id TEXT PRIMARY KEY,
              team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
              provider TEXT NOT NULL CHECK(provider IN ('tavily','datarc')),
              display_name TEXT NOT NULL COLLATE NOCASE,
              endpoint TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'disabled' CHECK(status IN ('enabled','disabled')),
              auth_ref TEXT NOT NULL,
              key_hint TEXT,
              created_by TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(team_id, provider, display_name)
            )"""
        )
        db.exec_driver_sql(
            """INSERT INTO team_builtin_tool_new(
              id,team_id,provider,display_name,endpoint,status,auth_ref,key_hint,
              created_by,created_at,updated_at
            )
            SELECT id,team_id,provider,'Tavily','https://api.tavily.com',status,
                   auth_ref,NULL,created_by,created_at,updated_at
            FROM team_builtin_tool"""
        )
        old_count = db.exec_driver_sql("SELECT COUNT(*) FROM team_builtin_tool").scalar_one()
        new_count = db.exec_driver_sql("SELECT COUNT(*) FROM team_builtin_tool_new").scalar_one()
        if old_count != new_count:
            raise RuntimeError("team_builtin_tool copy row count mismatch")
        db.exec_driver_sql("DROP TABLE team_builtin_tool")
        db.exec_driver_sql("ALTER TABLE team_builtin_tool_new RENAME TO team_builtin_tool")
        db.exec_driver_sql("CREATE INDEX idx_team_builtin_tool_team ON team_builtin_tool(team_id, status)")
        db.exec_driver_sql("RELEASE SAVEPOINT builtin_datarc_instances")
    except Exception:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT builtin_datarc_instances")
        db.exec_driver_sql("RELEASE SAVEPOINT builtin_datarc_instances")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
