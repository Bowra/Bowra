"""Retire live collaboration ownership with unsuccessful parent transitions."""

from alembic import context

revision = "0169_collaboration_terminal_ownership"
down_revision = "0168_ordinary_session_context"
branch_labels = None
depends_on = None

TRIGGER = """CREATE TRIGGER trg_agent_collaboration_turn_closed
AFTER UPDATE OF status ON turns
WHEN NEW.status IN ('completed','failed','cancelled','aborted') AND NEW.status IS NOT OLD.status
BEGIN
  UPDATE agent_collaboration_operation SET response_status=CASE WHEN response_status='pending'
      THEN CASE WHEN NEW.status='completed' THEN 'unavailable' ELSE 'cancelled' END ELSE response_status END,
    dependency_status=CASE WHEN dependency_status='open' THEN 'cancelled' ELSE dependency_status END
    WHERE turn_id=NEW.id AND action='ask';
  UPDATE agent_collaboration_participant SET wait_operation_id=NULL,wait_revision=NULL WHERE turn_id=NEW.id;
  UPDATE turn_agent_execution
  SET status=CASE WHEN NEW.status='failed' AND id IN (
        SELECT owner_execution_id FROM agent_collaboration_turn WHERE turn_id=NEW.id)
      THEN 'failed' ELSE 'cancelled' END,
      ended_at=COALESCE(ended_at,NEW.ended_at),
      failure_code=CASE WHEN NEW.status='failed' AND id IN (
        SELECT owner_execution_id FROM agent_collaboration_turn WHERE turn_id=NEW.id)
      THEN COALESCE(NEW.error_kind,'lead_internal_error') ELSE NULL END
  WHERE NEW.status IN ('failed','cancelled','aborted') AND turn_id=NEW.id AND status IN ('admitted','running') AND id IN (
    SELECT owner_execution_id FROM agent_collaboration_turn WHERE turn_id=NEW.id
    UNION SELECT execution_id FROM agent_collaboration_participant WHERE turn_id=NEW.id);
END"""


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT collaboration_terminal")
    try:
        # Preserve parent cancellation semantics before execution-close triggers
        # classify recipient exits. Separate AFTER triggers have no stable order.
        db.exec_driver_sql("DROP TRIGGER trg_agent_collaboration_turn_closed")
        db.exec_driver_sql(TRIGGER)
        db.exec_driver_sql("RELEASE SAVEPOINT collaboration_terminal")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT collaboration_terminal")
        db.exec_driver_sql("RELEASE SAVEPOINT collaboration_terminal")
        raise
