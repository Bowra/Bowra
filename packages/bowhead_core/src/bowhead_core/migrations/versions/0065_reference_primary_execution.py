"""Persist an exact primary execution on an immutable reference root binding.

Revision ID: 0065_reference_primary_execution
Revises: 0064_team_skill_import_provenance

Core only, expand-only; existing bindings remain NULL. No reader infers a
primary execution for history. The reference HTTP writer remains closed.
"""

from __future__ import annotations

from alembic import context

revision = "0065_reference_primary_execution"
down_revision = "0064_team_skill_import_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(turn_agent_binding)")}
    if not columns or "primary_execution_id" in columns:
        raise RuntimeError("reference primary binding schema drift")
    names = ("idx_turn_agent_binding_primary_execution", "trg_turn_agent_binding_primary_execution", "trg_turn_agent_binding_primary_no_replace")
    if connection.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name IN (?,?,?)", names).fetchone() is not None:
        raise RuntimeError("reference primary binding object drift")
    connection.exec_driver_sql(
        "ALTER TABLE turn_agent_binding ADD COLUMN primary_execution_id TEXT REFERENCES turn_agent_execution(id) DEFERRABLE INITIALLY DEFERRED"
    )
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX idx_turn_agent_binding_primary_execution ON turn_agent_binding(primary_execution_id) WHERE primary_execution_id IS NOT NULL"
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER trg_turn_agent_binding_primary_execution BEFORE INSERT ON turn_agent_binding
        WHEN NEW.primary_execution_id IS NOT NULL
        BEGIN
          SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM turn_agent_execution e
            JOIN chat_reference_plans p ON p.turn_id=e.turn_id AND p.retention_state='available'
            JOIN chat_turn_user_sources s ON s.turn_id=e.turn_id AND s.input_document_json IS NOT NULL
            WHERE e.id=NEW.primary_execution_id AND e.turn_id=NEW.turn_id AND e.team_id=NEW.team_id
              AND e.agent_id=NEW.requested_agent_id AND e.agent_source_kind=NEW.agent_source_kind
              AND e.revision_ref=NEW.revision_ref AND e.release_id IS NEW.release_id
              AND e.capability_fingerprint=NEW.capability_fingerprint AND e.policy_fingerprint=NEW.policy_fingerprint
              AND e.parent_execution_id IS NULL AND e.status='admitted'
              AND e.dispatch_source='explicit' AND e.dispatch_key='reference:primary'
              AND NEW.binding_source='mention' AND s.user_id=NEW.user_id
          ) THEN RAISE(ABORT,'reference primary execution mismatch') END;
        END
        """
    )
    # Existing UPDATE guards also freeze the added column. Prevent REPLACE from
    # deleting/recreating a primary binding while leaving its execution alive.
    connection.exec_driver_sql(
        """
        CREATE TRIGGER trg_turn_agent_binding_primary_no_replace BEFORE INSERT ON turn_agent_binding
        WHEN EXISTS (SELECT 1 FROM turn_agent_binding WHERE turn_id=NEW.turn_id AND primary_execution_id IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'reference primary binding is immutable'); END
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
