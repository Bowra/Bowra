"""Persist canonical execution envelopes separately from admission idempotency.

Revision ID: 0013_terminal_execution_envelopes
Revises: 0012_terminal_decision_principals
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import context

revision = "0013_terminal_execution_envelopes"
down_revision = "0012_terminal_decision_principals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    columns = {
        str(row[1])
        for row in connection.exec_driver_sql("PRAGMA table_info(turn_generations)").fetchall()
    }
    if "admission_request_digest" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE turn_generations ADD COLUMN admission_request_digest TEXT"
        )
    if "execution_intent_json" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE turn_generations ADD COLUMN execution_intent_json TEXT"
        )
    if "execution_authz_snapshot_digest" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE turn_generations ADD COLUMN execution_authz_snapshot_digest TEXT"
        )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS terminal_generation_envelope_immutable
        BEFORE UPDATE OF admission_request_digest,execution_intent_json,
                         execution_authz_snapshot_digest ON turn_generations
        WHEN NEW.admission_request_digest IS NOT OLD.admission_request_digest
          OR NEW.execution_intent_json IS NOT OLD.execution_intent_json
          OR NEW.execution_authz_snapshot_digest IS NOT OLD.execution_authz_snapshot_digest
        BEGIN
          SELECT RAISE(ABORT, 'terminal generation execution envelope is immutable');
        END
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
