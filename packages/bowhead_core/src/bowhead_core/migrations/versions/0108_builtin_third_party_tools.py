"""Add team-scoped connections for image-bundled third-party tools.

Revision ID: 0108_builtin_third_party_tools
Revises: 0107_reference_bi_tool_checkpoint
"""
from __future__ import annotations

from alembic import context

revision = "0108_builtin_third_party_tools"
down_revision = "0107_reference_bi_tool_checkpoint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql(
        """CREATE TABLE IF NOT EXISTS team_builtin_tool (
          id TEXT PRIMARY KEY,
          team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
          provider TEXT NOT NULL CHECK(provider IN ('tavily')),
          status TEXT NOT NULL DEFAULT 'disabled' CHECK(status IN ('enabled','disabled')),
          auth_ref TEXT NOT NULL,
          created_by TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(team_id, provider)
        )"""
    )
    db.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_team_builtin_tool_team ON team_builtin_tool(team_id, status)")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
