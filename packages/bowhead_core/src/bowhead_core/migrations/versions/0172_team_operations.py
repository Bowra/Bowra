"""Add team reporting time zone and idempotent Turn operation counters."""

from alembic import context

revision = "0172_team_operations"
down_revision = "0171_chart_analytical"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT team_operations")
    try:
        columns = {str(row[1]) for row in db.exec_driver_sql("PRAGMA table_info(teams)").fetchall()}
        if "report_timezone" not in columns:
            db.exec_driver_sql("ALTER TABLE teams ADD COLUMN report_timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'")
        db.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS turn_operation_usage (
                turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
                skill_loads INTEGER NOT NULL DEFAULT 0 CHECK(skill_loads >= 0),
                tool_loads INTEGER NOT NULL DEFAULT 0 CHECK(tool_loads >= 0),
                tool_calls INTEGER NOT NULL DEFAULT 0 CHECK(tool_calls >= 0),
                recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_turn_operation_usage_at ON turn_operation_usage(recorded_at)")
        db.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_user_audit_team_event_at ON user_audit_log(team_id,event_type,created_at)")
        db.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at)")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT team_operations")
        db.exec_driver_sql("RELEASE SAVEPOINT team_operations")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT team_operations")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
