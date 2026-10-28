"""Retain a single primary baseline without manufacturing selected methods.

Revision ID: 0076_reference_primary_baseline
Revises: 0075_reference_skill_invocation
"""
from __future__ import annotations

from importlib import import_module

from alembic import context

revision = "0076_reference_primary_baseline"
down_revision = "0075_reference_skill_invocation"
branch_labels = None
depends_on = None

PREVIOUS = import_module("bowhead_core.migrations.versions.0075_reference_skill_invocation")
TRIGGER = "trg_reference_primary_baseline_composition"
CONTEXT_TRIGGER = PREVIOUS.CONTEXT_TRIGGER
PREVIOUS_CONTEXT = PREVIOUS.CONTEXT_DDL
CONTEXT_DDL = PREVIOUS_CONTEXT.replace(
    "'initial-primary-skill-baseline.v1'",
    "'initial-primary-skill-baseline.v1','initial-primary-baseline.v1'",
)
DDL = (PREVIOUS.DDL.replace(PREVIOUS.TRIGGER, TRIGGER)
       .replace("'initial-primary-skill-baseline.v1'", "'initial-primary-baseline.v1'")
       .replace("json_array_length(NEW.composition_json,'$.members') BETWEEN 1 AND 32",
                "json_array_length(NEW.composition_json,'$.members')=0"))


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
        raise RuntimeError("reference primary baseline schema drift")
    db.exec_driver_sql("SAVEPOINT primary_baseline_upgrade")
    try:
        db.exec_driver_sql(DDL)
        db.exec_driver_sql(f"DROP TRIGGER {CONTEXT_TRIGGER}")
        db.exec_driver_sql(CONTEXT_DDL)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT primary_baseline_upgrade")
        db.exec_driver_sql("RELEASE SAVEPOINT primary_baseline_upgrade")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT primary_baseline_upgrade")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
