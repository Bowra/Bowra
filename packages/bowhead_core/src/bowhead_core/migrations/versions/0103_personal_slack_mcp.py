"""Store one-time, owner-bound Slack MCP OAuth states."""

from alembic import context

revision = "0103_personal_slack_mcp"
down_revision = "0102_reference_mcp_prompt_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT personal_slack_mcp")
    try:
        db.exec_driver_sql(
            """CREATE TABLE IF NOT EXISTS personal_slack_oauth_states (
              state TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES slack_connections(id) ON DELETE CASCADE,
              owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
              expires_at TEXT NOT NULL, created_at TEXT NOT NULL
            )"""
        )
        db.exec_driver_sql("CREATE INDEX IF NOT EXISTS personal_slack_oauth_state_expiry ON personal_slack_oauth_states(expires_at)")
        db.exec_driver_sql("RELEASE SAVEPOINT personal_slack_mcp")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT personal_slack_mcp")
        db.exec_driver_sql("RELEASE SAVEPOINT personal_slack_mcp")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
