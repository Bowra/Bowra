"""Retain initial evaluation-suite intent until explicit Owner adoption."""

from alembic import context

revision = "0145_initial_eval_suite_proposals"
down_revision = "0144_proposal_content_receipt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT initial_eval_suite_proposals")
    try:
        for statement in _DDL:
            db.exec_driver_sql(statement)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT initial_eval_suite_proposals")
        db.exec_driver_sql("RELEASE SAVEPOINT initial_eval_suite_proposals")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT initial_eval_suite_proposals")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")


_DDL = (
    "ALTER TABLE agent_change_proposal ADD COLUMN initial_suite_name TEXT CHECK(initial_suite_name IS NULL OR length(trim(initial_suite_name)) BETWEEN 1 AND 120)",
    """CREATE TRIGGER trg_initial_eval_suite_immutable BEFORE UPDATE ON agent_change_proposal
       WHEN NEW.initial_suite_name IS NOT OLD.initial_suite_name
       BEGIN SELECT RAISE(ABORT,'Initial evaluation suite intent is immutable'); END""",
    """CREATE TRIGGER trg_initial_eval_suite_shape BEFORE INSERT ON agent_change_proposal
       WHEN NEW.initial_suite_name IS NOT NULL AND (
         NEW.base_suite_revision_id IS NOT NULL OR NEW.base_working_copy_id IS NOT NULL
         OR NEW.base_working_copy_version IS NOT NULL OR NEW.base_release_id IS NOT NULL
         OR json_array_length(NEW.patch_json)!=0 OR json_array_length(NEW.atomic_groups_json)!=0
         OR json_array_length(NEW.suggested_cases_json)=0 OR EXISTS (
           SELECT 1 FROM agent_eval_suite WHERE team_id=NEW.team_id AND agent_id=NEW.agent_id
             AND suite_kind='owner_quality' AND state!='archived'
         ))
       BEGIN SELECT RAISE(ABORT,'Initial evaluation suite requires absent Owner suites and independent cases'); END""",
)
