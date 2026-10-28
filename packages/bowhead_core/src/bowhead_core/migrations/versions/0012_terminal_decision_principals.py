"""Bind all S5 decision principals and the canonical source mutation.

Revision ID: 0012_terminal_decision_principals
Revises: 0011_plan_step_verify
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import context

revision = "0012_terminal_decision_principals"
down_revision = "0011_plan_step_verify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    columns = {
        str(row[1])
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(turn_control_decisions)"
        ).fetchall()
    }
    additions = (
        ("requester_ref", "TEXT"),
        ("effect_principal_ref", "TEXT"),
        ("source_projection_json", "TEXT"),
        ("source_projection_digest", "TEXT"),
        ("prior_receipt_id", "TEXT"),
        ("prior_generation_transition_epoch", "INTEGER"),
    )
    for name, definition in additions:
        if name not in columns:
            connection.exec_driver_sql(
                f"ALTER TABLE turn_control_decisions ADD COLUMN {name} {definition}"
            )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS terminal_decision_bound_insert
        BEFORE INSERT ON turn_control_decisions
        WHEN NEW.requester_ref IS NULL OR NEW.effect_principal_ref IS NULL
          OR NEW.source_projection_json IS NULL OR NEW.source_projection_digest IS NULL
          OR NEW.prior_receipt_id IS NULL
          OR NEW.prior_generation_transition_epoch IS NULL
        BEGIN
          SELECT RAISE(ABORT, 'terminal decision projection must bind all principals');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS terminal_decision_projection_immutable
        BEFORE UPDATE OF decision_id,source_kind,source_id,turn_id,prior_generation,action,
          decision_digest,actor_ref,requester_ref,effect_principal_ref,
          source_projection_json,source_projection_digest,prior_receipt_id,
          prior_generation_transition_epoch,
          authz_snapshot_digest,created_at,recorded_at
        ON turn_control_decisions
        WHEN NEW.decision_id IS NOT OLD.decision_id
          OR NEW.source_kind IS NOT OLD.source_kind OR NEW.source_id IS NOT OLD.source_id
          OR NEW.turn_id IS NOT OLD.turn_id OR NEW.prior_generation IS NOT OLD.prior_generation
          OR NEW.action IS NOT OLD.action OR NEW.decision_digest IS NOT OLD.decision_digest
          OR NEW.actor_ref IS NOT OLD.actor_ref OR NEW.requester_ref IS NOT OLD.requester_ref
          OR NEW.effect_principal_ref IS NOT OLD.effect_principal_ref
          OR NEW.source_projection_json IS NOT OLD.source_projection_json
          OR NEW.source_projection_digest IS NOT OLD.source_projection_digest
          OR NEW.prior_receipt_id IS NOT OLD.prior_receipt_id
          OR NEW.prior_generation_transition_epoch IS NOT OLD.prior_generation_transition_epoch
          OR NEW.authz_snapshot_digest IS NOT OLD.authz_snapshot_digest
          OR NEW.created_at IS NOT OLD.created_at OR NEW.recorded_at IS NOT OLD.recorded_at
        BEGIN
          SELECT RAISE(ABORT, 'terminal decision identity is immutable');
        END
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
