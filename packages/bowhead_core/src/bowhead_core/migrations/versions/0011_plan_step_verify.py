"""Add plan_steps success_criteria / verify_json (graph-mode batch 1).

success_criteria: JSON array of checkable acceptance items authored at plan time
(second-pass generation); verify_json: VerifyOutcome recorded after the step's
payload is checked against those items. Guarded ALTERs — plan_steps is the one
store-created legacy table still maintained on the dual path (store initialize()
guard + this revision; see graph-mode design review B1 and the 0010
execution_turn_id precedent), so both sides must be idempotent.

Revision ID: 0011_plan_step_verify
Revises: 0010_composer_submission_protocol
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import context

revision = "0011_plan_step_verify"
down_revision = "0010_composer_submission_protocol"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    step_columns = {
        str(row[1])
        for row in connection.exec_driver_sql("PRAGMA table_info(plan_steps)").fetchall()
    }
    if step_columns and "success_criteria" not in step_columns:
        connection.exec_driver_sql(
            "ALTER TABLE plan_steps ADD COLUMN success_criteria TEXT NOT NULL DEFAULT '[]'",
        )
    if step_columns and "verify_json" not in step_columns:
        connection.exec_driver_sql(
            "ALTER TABLE plan_steps ADD COLUMN verify_json TEXT",
        )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
