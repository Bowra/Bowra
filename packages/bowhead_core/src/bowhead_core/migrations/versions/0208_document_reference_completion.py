"""Recognize the original completed primary after exact native document resume."""

from importlib import import_module

from alembic import context

revision = "0208_document_reference_completion"
down_revision = "0207_context_maintenance_terminal"
branch_labels = None
depends_on = None

PREVIOUS = import_module("bowhead_core.migrations.versions.0112_reference_lead_postdispatch")
NAME = "trg_reference_primary_completion"
CURRENT = PREVIOUS.DDL[NAME]
NEEDLE = "r.created_generation=NEW.execution_generation OR"

# The primary output belongs to document creation. Native resume does not run
# that Agent again or create replacement Reference model/output evidence.
NATIVE_RESUME = """EXISTS (
 SELECT 1 FROM document_jobs j
 JOIN document_dispatches admission ON admission.job_id=j.id
 JOIN turn_capability_execution capability ON capability.id=admission.capability_execution_id
 JOIN document_turn_dispatches original ON original.job_id=j.id
 JOIN document_planning_attempts first ON first.job_id=j.id AND first.attempt_no=1
 JOIN clarification_document_continuations card ON card.job_id=j.id
 JOIN clarification_requests question ON question.request_id=card.card_id
 JOIN document_planning_attempts resumed ON resumed.job_id=j.id AND resumed.attempt_no=2
 JOIN document_executions document ON document.job_id=j.id
 JOIN sessions session ON session.id=j.session_id
 JOIN workspaces space ON space.id=j.workspace_id
 JOIN chat_turn_spawn_dispatch lease ON lease.turn_id=j.origin_turn_id
 WHERE j.origin_turn_id=NEW.id AND j.session_id=NEW.session_id AND j.status='queued'
 AND session.user_id=j.requested_by AND session.workspace_id=j.workspace_id AND space.team_id=j.team_id
 AND session.deleted_at IS NULL AND session.active_turn_id=NEW.id
 AND capability.agent_execution_id=e.id AND capability.turn_id=NEW.id AND capability.team_id=j.team_id
 AND capability.status='completed' AND capability.output_contract='document.admission.v1'
 AND card.turn_id=NEW.id AND card.creation_generation=original.creation_generation
 AND card.creation_owner_id=original.owner_id AND first.creation_generation=original.creation_generation
 AND first.owner_id=original.owner_id AND first.checkpoint_digest=card.checkpoint_digest
 AND j.user_source_sha256=original.source_hash AND card.source_hash=original.source_hash
 AND card.runtime_snapshot_digest=original.runtime_snapshot_digest
 AND question.turn_id=NEW.id AND question.session_id=j.session_id
 AND question.user_id=j.requested_by AND question.workspace_id=j.workspace_id AND question.status='resolved'
 AND card.reserved_generation=NEW.execution_generation AND card.consumed_generation=NEW.execution_generation
 AND NEW.execution_generation>original.creation_generation AND o.created_generation=original.creation_generation
 AND o.retention_state='available'
 AND resumed.card_id=card.card_id AND resumed.creation_generation=card.consumed_generation
 AND resumed.owner_id=card.consumed_owner_id AND document.owner=card.consumed_owner_id
 AND document.state IN ('review','published')
 AND lease.session_id=j.session_id AND lease.state='spawned' AND lease.lease_kind='resume'
 AND lease.owner_id=card.consumed_owner_id AND lease.lease_execution_generation=card.consumed_generation
 AND lease.cancel_requested_at IS NULL AND julianday(lease.lease_expires_at)>julianday('now')
)"""

if CURRENT.count(NEEDLE) != 1:
    raise RuntimeError("Document Reference completion source drift")
DDL = CURRENT.replace(NEEDLE, f"{NEEDLE} {NATIVE_RESUME} OR", 1)


def _normalized(sql):
    return " ".join(sql.split()).rstrip(";")


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    row = db.exec_driver_sql("SELECT type,sql FROM sqlite_schema WHERE name=?", (NAME,)).fetchone()
    if row is None or row[0] != "trigger" or _normalized(row[1]) != _normalized(CURRENT):
        raise RuntimeError("Document Reference completion schema drift")
    db.exec_driver_sql("SAVEPOINT document_reference_completion")
    try:
        db.exec_driver_sql(f"DROP TRIGGER {NAME}")
        db.exec_driver_sql(DDL)
        db.exec_driver_sql("RELEASE SAVEPOINT document_reference_completion")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT document_reference_completion")
        db.exec_driver_sql("RELEASE SAVEPOINT document_reference_completion")
        raise


def downgrade():
    raise NotImplementedError("Restore the paired database snapshot to roll back.")
