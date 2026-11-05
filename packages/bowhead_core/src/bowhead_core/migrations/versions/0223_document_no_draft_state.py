"""Let an execution with no draft record its question or failure.

Revision ID: 0223_document_no_draft_state
Revises: 0222_artifact_producer_metadata
"""

from alembic import context

revision = "0223_document_no_draft_state"
down_revision = "0222_artifact_producer_metadata"
branch_labels = None
depends_on = None

# Content-domain purge and artifact identity guards use execution state only
# for jobs referenced by a draft file. File admission is fenced separately.
DOCUMENT_EXECUTION_UPDATE_TRIGGER = """CREATE TRIGGER trg_domain_mutation_document_executions_update
BEFORE UPDATE OF draft_json, job_id, state ON document_executions
WHEN OLD.job_id IS NOT NEW.job_id OR OLD.draft_json IS NOT NEW.draft_json
  OR (OLD.state IS NOT NEW.state AND NOT (
    OLD.draft_json IS NULL AND (
      (OLD.state = 'rendering' AND NEW.state = 'question')
      OR (OLD.state IN ('preparing', 'planning', 'rendering') AND NEW.state = 'failed')
    ) AND NOT EXISTS (SELECT 1 FROM document_draft_files WHERE job_id = OLD.job_id)
  ))
BEGIN SELECT bowhead_domain_mutation_v2('document_executions', 'update'); END"""


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "execution":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT document_no_draft_state")
    try:
        db.exec_driver_sql("DROP TRIGGER trg_domain_mutation_document_executions_update")
        db.exec_driver_sql(DOCUMENT_EXECUTION_UPDATE_TRIGGER)
        db.exec_driver_sql("RELEASE document_no_draft_state")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO document_no_draft_state")
        db.exec_driver_sql("RELEASE document_no_draft_state")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
