"""Session-scoped owner allowances for personal MCP tools."""

from alembic import context

revision = "0101_personal_mcp_session_allowance"
down_revision = "0100_personal_mcp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT personal_mcp_session_allowance_schema")
    try:
        db.exec_driver_sql("""CREATE TABLE IF NOT EXISTS personal_mcp_session_tool_allowance (
          owner_user_id TEXT NOT NULL, session_id TEXT NOT NULL, connection_id TEXT NOT NULL,
          tool_id TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(owner_user_id,session_id,tool_id)
        )""")
        db.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS personal_mcp_session_allowance_connection ON personal_mcp_session_tool_allowance(connection_id)"
        )
        db.exec_driver_sql("RELEASE SAVEPOINT personal_mcp_session_allowance_schema")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT personal_mcp_session_allowance_schema")
        db.exec_driver_sql("RELEASE SAVEPOINT personal_mcp_session_allowance_schema")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
