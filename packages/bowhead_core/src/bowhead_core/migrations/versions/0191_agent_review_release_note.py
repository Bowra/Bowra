"""Keep the release note a shared Agent's Owner writes when submitting for review.

Adds the nullable `agent_eval_candidate_control.review_release_note` in core. "Test this change"
prepares the candidate without a note, and the candidate's own note is part of the candidate hash the
evaluation evidence pins, so the note given in "Submit for review (as v2)" lives on the submission
record beside the other `review_*` fields instead. Reviewers read it, and approval copies it onto the
release. `ADD COLUMN` with a NULL default copies no table; existing rows read NULL (their candidate's
note, if any, still applies). Only core installs DDL; every other target advances its ledger unchanged.
"""

from alembic import context

revision = "0191_agent_review_release_note"
down_revision = "0190_remove_agent_version_webhooks"
branch_labels = None
depends_on = None

REVIEW_RELEASE_NOTE_COLUMN_DDL = (
    "ALTER TABLE agent_eval_candidate_control ADD COLUMN review_release_note TEXT "
    "CHECK(review_release_note IS NULL OR length(review_release_note) BETWEEN 1 AND 2000)"
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql("SAVEPOINT agent_review_release_note")
    try:
        columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_eval_candidate_control)").fetchall()}
        if not columns:
            raise RuntimeError("agent_eval_candidate_control is missing; upgrade through 0073 first")
        if "review_release_note" not in columns:
            connection.exec_driver_sql(REVIEW_RELEASE_NOTE_COLUMN_DDL)
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT agent_review_release_note")
        connection.exec_driver_sql("RELEASE SAVEPOINT agent_review_release_note")
        raise
    connection.exec_driver_sql("RELEASE SAVEPOINT agent_review_release_note")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
