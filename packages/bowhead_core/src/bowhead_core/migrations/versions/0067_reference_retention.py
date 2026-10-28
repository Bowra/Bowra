"""Fence reference material at Session deletion and parent retention.

Revision ID: 0067_reference_retention
Revises: 0066_reference_runtime
"""

from __future__ import annotations

import re

from alembic import context

revision = "0067_reference_retention"
down_revision = "0066_reference_runtime"
branch_labels = None
depends_on = None

DDL = (
    """
    CREATE TRIGGER trg_reference_session_delete AFTER UPDATE OF deleted_at ON sessions
    WHEN OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL
    BEGIN
      UPDATE chat_reference_plans SET retention_state='purged'
        WHERE turn_id IN (SELECT id FROM turns WHERE session_id=NEW.id) AND retention_state='available';
      UPDATE chat_reference_queued_inputs SET retention_state='purged',ciphertext=NULL,
        state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,
        cancel_reason=CASE WHEN state='queued' THEN 'source_deleted' ELSE cancel_reason END,
        row_version=row_version+1
        WHERE session_id=NEW.id AND retention_state='available';
    END
    """,
    """
    CREATE TRIGGER trg_reference_content_purge AFTER UPDATE OF retention_state ON chat_reference_plans
    WHEN NEW.retention_state='purged'
    BEGIN
      UPDATE chat_reference_content_snapshots SET retention_state='purged',ciphertext=NULL
        WHERE turn_id=NEW.turn_id AND retention_state='available';
    END
    """,
    """
    CREATE TRIGGER trg_reference_plan_lifecycle BEFORE INSERT ON chat_reference_plans
    WHEN NOT EXISTS (SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id
      WHERE t.id=NEW.turn_id AND s.deleted_at IS NULL)
    BEGIN SELECT RAISE(ABORT,'reference source unavailable'); END
    """,
    """
    CREATE TRIGGER trg_reference_plan_parent_delete BEFORE DELETE ON chat_reference_plans
    WHEN EXISTS (SELECT 1 FROM turns WHERE id=OLD.turn_id)
    BEGIN SELECT RAISE(ABORT,'reference plan must follow parent lifecycle'); END
    """,
    """
    CREATE TRIGGER trg_reference_completion_lifecycle BEFORE UPDATE OF status ON turns
    WHEN NEW.status='completed' AND OLD.status IS NOT NEW.status AND EXISTS (
      SELECT 1 FROM chat_reference_plans p JOIN sessions s ON s.id=NEW.session_id
      WHERE p.turn_id=NEW.id AND (p.retention_state='purged' OR s.deleted_at IS NOT NULL
        OR COALESCE(julianday(p.expires_at)<=julianday('now'),1))
    )
    BEGIN SELECT RAISE(ABORT,'reference material unavailable for completion'); END
    """,
    """
    CREATE TRIGGER trg_reference_queue_lifecycle BEFORE INSERT ON chat_reference_queued_inputs
    WHEN NOT EXISTS (SELECT 1 FROM sessions WHERE id=NEW.session_id AND deleted_at IS NULL)
      OR EXISTS (SELECT 1 FROM chat_reference_plans WHERE turn_id=NEW.accepted_parent_turn_id AND retention_state='purged')
    BEGIN SELECT RAISE(ABORT,'reference source unavailable'); END
    """,
    *(
        f"""
        CREATE TRIGGER trg_{table}_lifecycle BEFORE INSERT ON {table}
        WHEN NOT EXISTS (SELECT 1 FROM chat_reference_plans p JOIN turns t ON t.id=p.turn_id
          JOIN sessions s ON s.id=t.session_id
          WHERE p.turn_id=NEW.turn_id AND p.retention_state='available' AND s.deleted_at IS NULL)
        BEGIN SELECT RAISE(ABORT,'reference source unavailable'); END
        """
        for table in ("chat_reference_content_snapshots", "chat_reference_resolutions", "chat_reference_evidence")
    ),
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    names = [re.search(r"CREATE TRIGGER (\w+)", sql).group(1) for sql in DDL]
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name IN (" + ",".join("?" for _ in names) + ")", tuple(names)).fetchone():
        raise RuntimeError("reference retention schema drift")
    for sql in DDL:
        db.exec_driver_sql(sql)


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
