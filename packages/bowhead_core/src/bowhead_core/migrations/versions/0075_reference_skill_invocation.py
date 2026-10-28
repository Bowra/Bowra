"""Add an independent complete-baseline invocation contract; preserve v1/v2.

Revision ID: 0075_reference_skill_invocation
Revises: 0074_teams_personal_connection
"""
from __future__ import annotations

from importlib import import_module

from alembic import context

revision = "0075_reference_skill_invocation"
down_revision = "0074_teams_personal_connection"
branch_labels = None
depends_on = None

PREVIOUS = import_module("bowhead_core.migrations.versions.0072_skill_baseline_composition")
TRIGGER = "trg_reference_skill_invocation_composition"
CONTEXT_TRIGGER = PREVIOUS.CONTEXT_TRIGGER
PREVIOUS_CONTEXT = PREVIOUS.CONTEXT_DDL
CONTEXT_DDL = PREVIOUS_CONTEXT.replace(
    "'initial-primary-skill-read.v1','initial-primary-skill-read.v2'",
    "'initial-primary-skill-read.v1','initial-primary-skill-read.v2','initial-primary-skill-baseline.v1'",
)
DDL = PREVIOUS.DDL.replace(PREVIOUS.TRIGGER, TRIGGER).replace("'initial-primary-skill-read.v2'", "'initial-primary-skill-baseline.v1'")


def _normalized(sql: str) -> str:
    return " ".join(sql.split()).rstrip(";")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    previous = db.exec_driver_sql("SELECT type,sql FROM sqlite_master WHERE name=?", (CONTEXT_TRIGGER,)).fetchone()
    baseline = db.exec_driver_sql("SELECT type,sql FROM sqlite_master WHERE name=?", (PREVIOUS.TRIGGER,)).fetchone()
    if (
        db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (TRIGGER,)).fetchone()
        or previous is None or previous[0] != "trigger" or _normalized(previous[1]) != _normalized(PREVIOUS_CONTEXT)
        or baseline is None or baseline[0] != "trigger" or _normalized(baseline[1]) != _normalized(PREVIOUS.DDL)
    ):
        raise RuntimeError("reference skill invocation schema drift")
    db.exec_driver_sql("SAVEPOINT skill_invocation_upgrade")
    try:
        db.exec_driver_sql(DDL)
        db.exec_driver_sql(f"DROP TRIGGER {CONTEXT_TRIGGER}")
        db.exec_driver_sql(CONTEXT_DDL)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT skill_invocation_upgrade")
        db.exec_driver_sql("RELEASE SAVEPOINT skill_invocation_upgrade")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT skill_invocation_upgrade")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
