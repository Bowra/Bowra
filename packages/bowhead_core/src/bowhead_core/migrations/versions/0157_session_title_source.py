"""Record whether a session title was set by its owner or generated automatically.

Adds `sessions.title_source` (`auto` | `user`). Every existing row reads `auto`
through the constant default, so `ADD COLUMN` copies no table and nothing is
backfilled. An owner rename writes `user`; automatic title writers only touch
rows that are still `auto`, so a user-set title is never overwritten.

`sessions` keeps its legacy Store initializer for isolated core stores, which
mirrors the same column; both sides check the column first and are idempotent
(the 0126/0127 dual-ownership exception). Only core installs DDL; every other
target advances its ledger unchanged.
"""

from alembic import context

revision = "0157_session_title_source"
down_revision = "0156_agent_eval_attempt_usage"
branch_labels = None
depends_on = None

#: `SessionStore.initialize` mirrors this statement; the migration test pins both to one definition.
TITLE_SOURCE_COLUMN_DDL = "ALTER TABLE sessions ADD COLUMN title_source TEXT NOT NULL DEFAULT 'auto' CHECK(title_source IN ('auto','user'))"


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql("SAVEPOINT session_title_source")
    try:
        columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(sessions)").fetchall()}
        if "title_source" not in columns:
            connection.exec_driver_sql(TITLE_SOURCE_COLUMN_DDL)
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT session_title_source")
        connection.exec_driver_sql("RELEASE SAVEPOINT session_title_source")
        raise
    connection.exec_driver_sql("RELEASE SAVEPOINT session_title_source")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
