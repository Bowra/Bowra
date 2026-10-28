"""Pin independent case proposals to an immutable evaluation suite revision."""

from alembic import context

revision = "0142_case_only_proposals"
down_revision = "0141_coach_plugin_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT case_only_proposals")
    try:
        for statement in _DDL:
            db.exec_driver_sql(statement)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT case_only_proposals")
        db.exec_driver_sql("RELEASE SAVEPOINT case_only_proposals")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT case_only_proposals")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")


_DDL = (
    "ALTER TABLE agent_change_proposal ADD COLUMN base_suite_revision_id TEXT REFERENCES agent_eval_suite_revision(id) ON DELETE RESTRICT",
    """CREATE TRIGGER trg_case_proposal_baseline_immutable BEFORE UPDATE ON agent_change_proposal
       WHEN NEW.base_suite_revision_id IS NOT OLD.base_suite_revision_id
       BEGIN SELECT RAISE(ABORT,'Case proposal suite baseline is immutable'); END""",
    """CREATE TRIGGER trg_case_proposal_scope BEFORE INSERT ON agent_change_proposal
       WHEN NEW.base_suite_revision_id IS NOT NULL AND (
         NEW.base_working_copy_id IS NOT NULL OR NEW.base_working_copy_version IS NOT NULL OR NEW.base_release_id IS NOT NULL
         OR json_array_length(NEW.patch_json)!=0 OR json_array_length(NEW.atomic_groups_json)!=0
         OR json_array_length(NEW.suggested_cases_json)=0 OR NOT EXISTS (
           SELECT 1 FROM agent_eval_suite_revision r JOIN agent_eval_suite s ON s.id=r.suite_id
           WHERE r.id=NEW.base_suite_revision_id AND s.team_id=NEW.team_id AND s.agent_id=NEW.agent_id
             AND s.suite_kind='owner_quality' AND s.state!='archived'
             AND r.sequence=(SELECT MAX(sequence) FROM agent_eval_suite_revision WHERE suite_id=s.id)
         ))
       BEGIN SELECT RAISE(ABORT,'Case proposal requires the current owned suite baseline'); END""",
)
