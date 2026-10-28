"""Persist verified capability scopes for built-in Datarc connections.

Revision ID: 0110_builtin_datarc_key_scope
Revises: 0109_builtin_datarc_tool_instances
"""
from __future__ import annotations

from alembic import context

revision = "0110_builtin_datarc_key_scope"
down_revision = "0109_builtin_datarc_tool_instances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT builtin_datarc_scope")
    try:
        db.exec_driver_sql(
            "ALTER TABLE team_builtin_tool ADD COLUMN scope_state TEXT NOT NULL DEFAULT 'unknown' "
            "CHECK(scope_state IN ('unknown','verified','invalid'))"
        )
        db.exec_driver_sql("ALTER TABLE team_builtin_tool ADD COLUMN scope_json TEXT")
        db.exec_driver_sql("ALTER TABLE team_builtin_tool ADD COLUMN scope_fingerprint TEXT")
        db.exec_driver_sql("ALTER TABLE team_builtin_tool ADD COLUMN scope_checked_at TEXT")
        db.exec_driver_sql("RELEASE SAVEPOINT builtin_datarc_scope")
    except Exception:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT builtin_datarc_scope")
        db.exec_driver_sql("RELEASE SAVEPOINT builtin_datarc_scope")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
