"""Allow retained Prompt results through the existing exact BI resume fence.

Revision ID: 0104_reference_prompt_continuation
Revises: 0103_personal_slack_mcp
"""
from __future__ import annotations

from importlib import import_module

from alembic import context

revision = "0104_reference_prompt_continuation"
down_revision = "0103_personal_slack_mcp"
branch_labels = None
depends_on = None

PROMPT = import_module("bowhead_core.migrations.versions.0102_reference_mcp_prompt_model")
FOLLOW_UP = import_module("bowhead_core.migrations.versions.0092_reference_follow_up_runtime")
CAPTURE = "trg_reference_bi_continuation_scope"
CURRENT = {PROMPT.TRIGGER: PROMPT.DDL, CAPTURE: FOLLOW_UP.DDL[CAPTURE]}
INITIAL_ONLY = "  AND NEW.created_generation=1\n"
CAPTURE_POLICIES = "json_extract(r.composition_json,'$.runtime_policy') IN ('initial-primary-skill-read.v2','initial-primary-skill-baseline.v1','initial-primary-baseline.v1','initial-primary-skill-extension.v1')"
if CURRENT[PROMPT.TRIGGER].count(INITIAL_ONLY) != 1 or CURRENT[CAPTURE].count(CAPTURE_POLICIES) != 1:
    raise RuntimeError("reference Prompt continuation source drift")
# The outer model guard still requires either the original generation or the
# exact started pause + live resume lease. Only its Prompt-specific first-send
# restriction is removed; retained result identity/availability stays required.
DDL = {
    PROMPT.TRIGGER: CURRENT[PROMPT.TRIGGER].replace(INITIAL_ONLY, ""),
    CAPTURE: CURRENT[CAPTURE].replace(CAPTURE_POLICIES, f"({CAPTURE_POLICIES} OR {PROMPT.PROMPT})"),
}


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    for name, sql in CURRENT.items():
        row = db.exec_driver_sql("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or row[0] != "trigger" or FOLLOW_UP._normalized(row[1]) != FOLLOW_UP._normalized(sql):
            raise RuntimeError("reference Prompt continuation schema drift")
    db.exec_driver_sql("SAVEPOINT reference_prompt_continuation")
    try:
        for name, sql in DDL.items():
            db.exec_driver_sql(f"DROP TRIGGER {name}")
            db.exec_driver_sql(sql)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_prompt_continuation")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_prompt_continuation")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_prompt_continuation")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
