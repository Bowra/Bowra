"""Retain the installed Slack App ID for direct configuration links.

Revision ID: 0091_slack_app_settings
Revises: 0090_reference_mcp_calls
"""

from alembic import context

revision = "0091_slack_app_settings"
down_revision = "0090_reference_mcp_calls"
branch_labels = None
depends_on = None

TABLE = "slack_connections"
COLUMN = "slack_app_id"


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    columns = {row[1] for row in db.exec_driver_sql(f"PRAGMA table_info({TABLE})").fetchall()}
    if COLUMN in columns:
        raise RuntimeError("Slack App settings schema drift")
    db.exec_driver_sql(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TEXT NOT NULL DEFAULT ''")
    actual = {row[1]: row[2] for row in db.exec_driver_sql(f"PRAGMA table_info({TABLE})").fetchall()}
    if actual.get(COLUMN) != "TEXT":
        raise RuntimeError("Slack App settings schema drift")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore a matching backup to roll back.")
