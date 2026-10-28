"""Record per-attempt token and model-call usage on Agent Eval case results.

Adds four nullable metering columns to `agent_eval_case_result`. They are
all null or all non-null for one row; historical rows stay null and are never
backfilled. The existing immutability triggers already cover the new columns.
Only core installs DDL; every other target advances its ledger unchanged.
"""

from alembic import context

revision = "0156_agent_eval_attempt_usage"
down_revision = "0155_document_routing"
branch_labels = None
depends_on = None

DDL = (
    "ALTER TABLE agent_eval_case_result ADD COLUMN tokens_input INTEGER CHECK(tokens_input IS NULL OR tokens_input >= 0)",
    "ALTER TABLE agent_eval_case_result ADD COLUMN tokens_output INTEGER CHECK(tokens_output IS NULL OR tokens_output >= 0)",
    "ALTER TABLE agent_eval_case_result ADD COLUMN tokens_cached INTEGER CHECK(tokens_cached IS NULL OR tokens_cached >= 0)",
    # SQLite cannot add a table constraint in place, so the all-or-none rule is the
    # last column's CHECK; it may reference the earlier usage columns.
    """ALTER TABLE agent_eval_case_result ADD COLUMN model_call_count INTEGER CHECK(
      (model_call_count IS NULL AND tokens_input IS NULL AND tokens_output IS NULL AND tokens_cached IS NULL)
      OR (model_call_count IS NOT NULL AND model_call_count >= 0 AND tokens_input IS NOT NULL AND tokens_output IS NOT NULL AND tokens_cached IS NOT NULL)
    )""",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql("SAVEPOINT agent_eval_attempt_usage")
    try:
        for statement in DDL:
            connection.exec_driver_sql(statement)
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT agent_eval_attempt_usage")
        connection.exec_driver_sql("RELEASE SAVEPOINT agent_eval_attempt_usage")
        raise
    connection.exec_driver_sql("RELEASE SAVEPOINT agent_eval_attempt_usage")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
