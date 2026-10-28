"""Bind the initial Skill composition to its retained closure in the same claim.

Revision ID: 0069_skill_reference_composition
Revises: 0068_reference_retention_policy

No rewrite/backfill. Other composition contracts and seven non-core targets keep
existing semantics. Application validation checks the complete member graph.
"""
from __future__ import annotations

from alembic import context

revision = "0069_skill_reference_composition"
down_revision = "0068_reference_retention_policy"
branch_labels = None
depends_on = None

TRIGGER = "trg_reference_skill_composition_material"
DDL = f"""
CREATE TRIGGER {TRIGGER} BEFORE INSERT ON chat_reference_resolutions
WHEN json_extract(NEW.composition_json,'$.runtime_policy')='initial-primary-skill-read.v1'
AND NOT EXISTS (
 SELECT 1 FROM chat_reference_content_snapshots c JOIN turn_agent_execution e ON e.id=NEW.execution_id
 JOIN chat_turn_spawn_dispatch d ON d.turn_id=NEW.turn_id
 WHERE c.turn_id=NEW.turn_id AND c.content_key='reference:primary:skill-closure'
 AND c.retention_state='available' AND c.created_generation=NEW.created_generation
 AND c.descriptor_json=json_extract(NEW.composition_json,'$.content')
 AND json_extract(c.descriptor_json,'$.content_kind')='skill_read_closure'
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


def upgrade() -> None:
    if context.config.attributes['bowhead_target'] != 'core':
        return
    db = context.get_bind()
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (TRIGGER,)).fetchone():
        raise RuntimeError('reference skill composition schema drift')
    db.exec_driver_sql(DDL)


def downgrade() -> None:
    raise NotImplementedError('Forward-only; restore from snapshot to roll back.')
