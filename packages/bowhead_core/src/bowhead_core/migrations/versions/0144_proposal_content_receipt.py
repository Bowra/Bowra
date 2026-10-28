"""Retain accepted content independently of subsequent safety metadata."""

from alembic import context

revision = "0144_proposal_content_receipt"
down_revision = "0143_coach_ci_delegation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT proposal_content_receipt")
    try:
        for statement in _DDL:
            db.exec_driver_sql(statement)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT proposal_content_receipt")
        db.exec_driver_sql("RELEASE SAVEPOINT proposal_content_receipt")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT proposal_content_receipt")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")


_DDL = (
    "ALTER TABLE agent_change_proposal_decision ADD COLUMN result_content_hash TEXT CHECK(result_content_hash IS NULL OR result_content_hash LIKE 'sha256:%')",
    """CREATE TRIGGER trg_agent_proposal_content_receipt_shape BEFORE INSERT ON agent_change_proposal_decision
       WHEN NEW.result_content_hash IS NOT NULL AND NEW.decision_kind!='patch_accept'
       BEGIN SELECT RAISE(ABORT,'Accepted content belongs to a definition acceptance'); END""",
)
