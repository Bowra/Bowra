"""Add plan_steps superseded_by / input_manifest_json + plans replan_count (graph-mode batch 3).

superseded_by: revision pointer — a successor step id ("ps_*") written by
revise_step, or a generation sentinel ("replan:rev{N}") written by full replan;
input_manifest_json: the upstream inputs a step actually received at dispatch
(expected vs received, degraded/excluded buckets); replan_count: soft guardrail
counter bumped by each revise/replan commit. Guarded ALTERs — plans/plan_steps
are store-created legacy tables maintained on the dual path (store initialize()
guard + this revision; 0011/0010 precedent), so both sides must be idempotent.

Revision ID: 0016_plan_replan
Revises: 0015_scheduled_recipe_runs
Create Date: 2026-08-05
"""

from __future__ import annotations

from alembic import context

revision = "0016_plan_replan"
down_revision = "0015_scheduled_recipe_runs"
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
    if step_columns and "superseded_by" not in step_columns:
        connection.exec_driver_sql(
            "ALTER TABLE plan_steps ADD COLUMN superseded_by TEXT",
        )
    if step_columns and "input_manifest_json" not in step_columns:
        connection.exec_driver_sql(
            "ALTER TABLE plan_steps ADD COLUMN input_manifest_json TEXT",
        )
    plan_columns = {
        str(row[1])
        for row in connection.exec_driver_sql("PRAGMA table_info(plans)").fetchall()
    }
    if plan_columns and "replan_count" not in plan_columns:
        connection.exec_driver_sql(
            "ALTER TABLE plans ADD COLUMN replan_count INTEGER NOT NULL DEFAULT 0",
        )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
