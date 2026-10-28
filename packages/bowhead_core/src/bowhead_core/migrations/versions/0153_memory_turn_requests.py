"""Record structured per-turn "explicitly asked to remember" facts for the memory receipt."""

from alembic import context

revision = "0153_memory_turn_requests"
down_revision = "0152_ordinary_lead_questions"
branch_labels = None
depends_on = None

DDL = (
    """CREATE TABLE memory_turn_requests (
      user_id TEXT NOT NULL,
      turn_id TEXT NOT NULL,
      source TEXT NOT NULL CHECK (source IN ('remember_tool','capture','language_setting','nl_intent')),
      outcome TEXT NOT NULL CHECK (outcome IN ('remembered','candidate','not_remembered')),
      reason TEXT,
      entry_id TEXT,
      candidate_id TEXT,
      created_at TEXT NOT NULL,
      PRIMARY KEY (user_id, turn_id, source)
    )""",
    "CREATE INDEX idx_memory_turn_requests_turn ON memory_turn_requests(turn_id)",
    # Rows hold only finite codes and IDs about one turn, so they leave with that turn and with the account.
    """CREATE TRIGGER trg_memory_turn_requests_turn_delete AFTER DELETE ON turns
      BEGIN DELETE FROM memory_turn_requests WHERE turn_id=OLD.id; END""",
    """CREATE TRIGGER trg_memory_turn_requests_user_delete AFTER DELETE ON users
      BEGIN DELETE FROM memory_turn_requests WHERE user_id=OLD.id; END""",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql("SAVEPOINT memory_turn_requests")
    try:
        for statement in DDL:
            connection.exec_driver_sql(statement)
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT memory_turn_requests")
        connection.exec_driver_sql("RELEASE SAVEPOINT memory_turn_requests")
        raise
    connection.exec_driver_sql("RELEASE SAVEPOINT memory_turn_requests")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
