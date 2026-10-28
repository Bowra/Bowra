"""Retain original Skill packages under the exact primary Agent in v2.

Revision ID: 0072_skill_baseline_composition
Revises: 0071_reference_model_invocation

No data rewrite. v1 compositions and their model contexts remain supported.
Only core changes; the historical context guard must match before replacement.
"""

from __future__ import annotations

from importlib import import_module

from alembic import context

revision = "0072_skill_baseline_composition"
down_revision = "0071_reference_model_invocation"
branch_labels = None
depends_on = None

TRIGGER = "trg_reference_skill_baseline_composition"
CONTEXT_TRIGGER = "trg_reference_model_context_scope"
PREVIOUS_CONTEXT = next(
    sql for sql in import_module("bowhead_core.migrations.versions.0070_reference_model_context").DDL
    if f"CREATE TRIGGER {CONTEXT_TRIGGER} " in sql
)
CONTEXT_DDL = PREVIOUS_CONTEXT.replace(
    "json_extract(r.composition_json,'$.runtime_policy')='initial-primary-skill-read.v1'",
    "json_extract(r.composition_json,'$.runtime_policy') IN ('initial-primary-skill-read.v1','initial-primary-skill-read.v2')",
)
DDL = f"""
CREATE TRIGGER {TRIGGER} BEFORE INSERT ON chat_reference_resolutions
WHEN json_extract(NEW.composition_json,'$.runtime_policy')='initial-primary-skill-read.v2'
AND NOT EXISTS (
 SELECT 1 FROM chat_reference_content_snapshots c JOIN turn_agent_execution e ON e.id=NEW.execution_id
 JOIN chat_turn_spawn_dispatch d ON d.turn_id=NEW.turn_id
 WHERE c.turn_id=NEW.turn_id AND c.content_key='reference:primary:skill-baseline'
 AND c.retention_state='available' AND c.created_generation=NEW.created_generation
 AND c.descriptor_json=json_extract(NEW.composition_json,'$.content')
 AND json_extract(c.descriptor_json,'$.content_kind')='skill_baseline'
 AND json_extract(c.descriptor_json,'$.node_id')=json_extract(NEW.composition_json,'$.node_id')
 AND json_extract(c.descriptor_json,'$.resource')=json_extract(NEW.composition_json,'$.resource')
 AND json_extract(c.descriptor_json,'$.resource.kind')='agent'
 AND json_extract(c.descriptor_json,'$.materialization_hash')=json_extract(NEW.composition_json,'$.receiver_digest')
 AND NEW.resolution_seq=1 AND NEW.idempotency_key='reference:primary'
 AND e.status='running' AND d.state='spawned' AND d.owner_id IS NOT NULL
 AND d.dispatch_kind='start' AND d.parent_turn_id IS NULL AND d.lease_kind='turn'
 AND d.lease_execution_generation=NEW.created_generation AND NEW.created_generation=1
 AND json_extract(NEW.composition_json,'$.receiver_status')='admitted'
 AND json_extract(NEW.composition_json,'$.parameter_source')='initial-source.v1'
 AND json_extract(NEW.composition_json,'$.render_contract')='skill-read-json.v1'
 AND json_type(NEW.composition_json,'$.members')='array'
 AND json_array_length(NEW.composition_json,'$.members') BETWEEN 1 AND 32
 AND NOT EXISTS (SELECT 1 FROM json_each(NEW.composition_json,'$.members') m WHERE COALESCE(
   json_extract(m.value,'$.operation')='read' AND json_extract(m.value,'$.provenance')='baseline',0)=0)
)
BEGIN SELECT RAISE(ABORT,'reference skill composition material mismatch'); END
"""


def _normalized(sql: str) -> str:
    return " ".join(sql.split()).rstrip(";")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    previous = db.exec_driver_sql("SELECT type,sql FROM sqlite_master WHERE name=?", (CONTEXT_TRIGGER,)).fetchone()
    if (
        db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (TRIGGER,)).fetchone()
        or previous is None or previous[0] != "trigger" or _normalized(previous[1]) != _normalized(PREVIOUS_CONTEXT)
    ):
        raise RuntimeError("reference skill baseline schema drift")
    db.exec_driver_sql("SAVEPOINT skill_baseline_upgrade")
    try:
        db.exec_driver_sql(DDL)
        db.exec_driver_sql(f"DROP TRIGGER {CONTEXT_TRIGGER}")
        db.exec_driver_sql(CONTEXT_DDL)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT skill_baseline_upgrade")
        db.exec_driver_sql("RELEASE SAVEPOINT skill_baseline_upgrade")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT skill_baseline_upgrade")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
