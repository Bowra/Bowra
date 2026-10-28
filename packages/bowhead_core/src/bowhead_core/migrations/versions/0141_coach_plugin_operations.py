"""Atomically retain Coach Plugin save receipts with their domain revisions."""

from alembic import context

revision = "0141_coach_plugin_operations"
down_revision = "0140_coach_tool_requirements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT coach_plugin_operations")
    try:
        for statement in _DDL:
            db.exec_driver_sql(statement)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT coach_plugin_operations")
        db.exec_driver_sql("RELEASE SAVEPOINT coach_plugin_operations")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT coach_plugin_operations")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")


_DDL = (
    """CREATE TABLE coach_plugin_operation (
       id TEXT PRIMARY KEY CHECK(id LIKE 'cpw_%'), team_id TEXT NOT NULL,
       user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
       workspace_id TEXT NOT NULL, origin_session_id TEXT NOT NULL, origin_turn_id TEXT NOT NULL,
       request_hash TEXT NOT NULL, result_json TEXT NOT NULL CHECK(json_valid(result_json)),
       created_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_coach_plugin_operation_owner ON coach_plugin_operation(team_id,user_id,workspace_id)",
    """CREATE TRIGGER trg_coach_plugin_operation_immutable BEFORE UPDATE ON coach_plugin_operation
       BEGIN SELECT RAISE(ABORT,'Coach Plugin operation is immutable'); END""",
)
