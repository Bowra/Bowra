"""Allow one exact Capability beside the existing initial Skill selection.

Revision ID: 0088_reference_capability_skill
Revises: 0087_reference_capability

No rows are rewritten. The existing lead-source and Capability-composition
guards are replaced atomically; all retained Skill material guards remain in
force independently.
"""

from __future__ import annotations

from importlib import import_module

from alembic import context

revision = "0088_reference_capability_skill"
down_revision = "0087_reference_capability"
branch_labels = None
depends_on = None

PREVIOUS = import_module("bowhead_core.migrations.versions.0087_reference_capability")
SOURCE_TRIGGER = PREVIOUS.SOURCE_TRIGGER
CAPABILITY_TRIGGER = PREVIOUS.CAPABILITY_TRIGGER

SOURCE_DDL = f"""
CREATE TRIGGER {SOURCE_TRIGGER} BEFORE INSERT ON turn_agent_binding
WHEN NEW.binding_source='lead'
BEGIN
  SELECT CASE WHEN NEW.primary_execution_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM chat_reference_plans p
    JOIN chat_turn_user_sources s ON s.turn_id=p.turn_id AND s.user_id=NEW.user_id
    WHERE p.turn_id=NEW.turn_id AND p.source_turn_id=NEW.turn_id
      AND p.logical_source_turn_id=NEW.turn_id AND p.snapshot_owner_turn_id=NEW.turn_id
      AND json_valid(s.input_document_json) AND json_extract(s.input_document_json,'$.schema_version')=1
      AND json_array_length(p.plan_json,'$.admissions')=(SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n WHERE json_extract(n.value,'$.type')='reference')
      AND (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n WHERE json_extract(n.value,'$.type')='reference') BETWEEN 1 AND 4
      AND (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n
        WHERE json_extract(n.value,'$.type')='reference' AND json_extract(n.value,'$.resource.kind')='skill') BETWEEN 0 AND 3
      AND (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n
        WHERE json_extract(n.value,'$.type')='reference' AND json_extract(n.value,'$.resource.kind')='capability') BETWEEN 0 AND 1
      AND (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n WHERE json_extract(n.value,'$.type')='reference') =
        (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n
          WHERE json_extract(n.value,'$.type')='reference' AND json_extract(n.value,'$.resource.kind') IN ('skill','capability'))
      AND NOT EXISTS (SELECT 1 FROM json_each(p.plan_json,'$.admissions') a WHERE NOT (
        COALESCE(json_extract(a.value,'$.resource.kind')='skill' AND json_extract(a.value,'$.operation')='read',0)
        OR COALESCE(json_extract(a.value,'$.resource.kind')='capability' AND json_extract(a.value,'$.operation')='execute',0)))
      AND NOT EXISTS (SELECT 1 FROM json_each(s.input_document_json,'$.nodes') n
        WHERE json_extract(n.value,'$.type')='reference' AND NOT EXISTS (
          SELECT 1 FROM json_each(p.plan_json,'$.admissions') a
          WHERE json_extract(a.value,'$.node_id')=json_extract(n.value,'$.node_id')
            AND json_extract(a.value,'$.resource.source_kind') IS json_extract(n.value,'$.resource.source_kind')
            AND json_extract(a.value,'$.resource.id')=json_extract(n.value,'$.resource.id')
            AND json_extract(a.value,'$.resource.version')=json_extract(n.value,'$.resource.version')))
  ) THEN RAISE(ABORT,'automatic reference receiver source mismatch') END;
END
"""

CAPABILITY_DDL = f"""
CREATE TRIGGER {CAPABILITY_TRIGGER} BEFORE INSERT ON chat_reference_resolutions
WHEN EXISTS (
  SELECT 1 FROM chat_reference_plans p, json_each(p.plan_json,'$.admissions') a
  WHERE p.turn_id=NEW.turn_id AND json_extract(a.value,'$.resource.kind')='capability'
) OR json_type(NEW.composition_json,'$.capability') IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM chat_reference_plans p
    JOIN chat_turn_user_sources s ON s.turn_id=p.turn_id
    JOIN json_each(p.plan_json,'$.admissions') a
    JOIN json_each(s.input_document_json,'$.nodes') n
    WHERE p.turn_id=NEW.turn_id AND p.source_turn_id=NEW.source_turn_id
      AND NEW.resolution_seq=1 AND NEW.idempotency_key='reference:primary'
      AND json_array_length(p.plan_json,'$.admissions')=(SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') x
        WHERE json_extract(x.value,'$.type')='reference')
      AND (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') x
        WHERE json_extract(x.value,'$.type')='reference') =
          (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') x
            WHERE json_extract(x.value,'$.type')='reference'
              AND json_extract(x.value,'$.resource.kind') IN ('agent','skill','capability'))
      AND (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') x
        WHERE json_extract(x.value,'$.resource.kind')='agent') BETWEEN 0 AND 1
      AND NOT EXISTS (SELECT 1 FROM json_each(p.plan_json,'$.admissions') x WHERE NOT (
        COALESCE(json_extract(x.value,'$.resource.kind')='agent' AND json_extract(x.value,'$.operation')='delegate',0)
        OR COALESCE(json_extract(x.value,'$.resource.kind')='skill' AND json_extract(x.value,'$.operation')='read',0)
        OR COALESCE(json_extract(x.value,'$.resource.kind')='capability' AND json_extract(x.value,'$.operation')='execute',0)))
      AND NOT EXISTS (SELECT 1 FROM json_each(s.input_document_json,'$.nodes') x
        WHERE json_extract(x.value,'$.type')='reference' AND NOT EXISTS (
          SELECT 1 FROM json_each(p.plan_json,'$.admissions') y
          WHERE json_extract(y.value,'$.node_id')=json_extract(x.value,'$.node_id')
            AND json_extract(y.value,'$.resource.kind')=json_extract(x.value,'$.resource.kind')
            AND json_extract(y.value,'$.resource.source_kind') IS json_extract(x.value,'$.resource.source_kind')
            AND json_extract(y.value,'$.resource.id')=json_extract(x.value,'$.resource.id')
            AND json_extract(y.value,'$.resource.version')=json_extract(x.value,'$.resource.version')))
      AND json_type(NEW.composition_json,'$.members')='array'
      AND (
        (json_extract(NEW.composition_json,'$.runtime_policy')='initial-primary-baseline.v1'
          AND json_array_length(NEW.composition_json,'$.members')=0
          AND (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') x
            WHERE json_extract(x.value,'$.resource.kind')='skill')=0
          AND (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') x
            WHERE json_extract(x.value,'$.resource.kind')='skill')=0)
        OR
        (json_extract(NEW.composition_json,'$.runtime_policy') IN (
            'initial-primary-skill-read.v1','initial-primary-skill-read.v2',
            'initial-primary-skill-baseline.v1','initial-primary-skill-extension.v1')
          AND json_array_length(NEW.composition_json,'$.members') BETWEEN 1 AND 32
          AND (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') x
            WHERE json_extract(x.value,'$.resource.kind')='skill') BETWEEN 1 AND 3
          AND (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') x
            WHERE json_extract(x.value,'$.resource.kind')='skill') =
              (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') x
                WHERE json_extract(x.value,'$.resource.kind')='skill')
          AND NOT EXISTS (SELECT 1 FROM json_each(s.input_document_json,'$.nodes') x
            WHERE json_extract(x.value,'$.resource.kind')='skill' AND NOT EXISTS (
              SELECT 1 FROM json_each(p.plan_json,'$.admissions') y
              WHERE json_extract(y.value,'$.node_id')=json_extract(x.value,'$.node_id')
                AND json_extract(y.value,'$.resource.kind')='skill'
                AND json_extract(y.value,'$.operation')='read'
                AND json_extract(y.value,'$.resource.source_kind') IS json_extract(x.value,'$.resource.source_kind')
                AND json_extract(y.value,'$.resource.id')=json_extract(x.value,'$.resource.id')
                AND json_extract(y.value,'$.resource.version')=json_extract(x.value,'$.resource.version'))))
      )
      AND json_extract(a.value,'$.resource.kind')='capability'
      AND json_extract(a.value,'$.operation')='execute'
      AND json_extract(n.value,'$.type')='reference'
      AND json_extract(n.value,'$.node_id')=json_extract(a.value,'$.node_id')
      AND json_extract(n.value,'$.resource.kind')=json_extract(a.value,'$.resource.kind')
      AND json_extract(n.value,'$.resource.source_kind') IS json_extract(a.value,'$.resource.source_kind')
      AND json_extract(n.value,'$.resource.id')=json_extract(a.value,'$.resource.id')
      AND json_extract(n.value,'$.resource.version')=json_extract(a.value,'$.resource.version')
      AND json_extract(NEW.composition_json,'$.capability.node_id')=json_extract(a.value,'$.node_id')
      AND json_extract(NEW.composition_json,'$.capability.resource.kind')=json_extract(a.value,'$.resource.kind')
      AND json_extract(NEW.composition_json,'$.capability.resource.source_kind') IS json_extract(a.value,'$.resource.source_kind')
      AND json_extract(NEW.composition_json,'$.capability.resource.id')=json_extract(a.value,'$.resource.id')
      AND json_extract(NEW.composition_json,'$.capability.resource.version')=json_extract(a.value,'$.resource.version')
      AND json_extract(NEW.composition_json,'$.capability.operation')='execute'
      AND json_extract(NEW.composition_json,'$.capability.policy_digest')=json_extract(a.value,'$.policy_digest')
      AND json_extract(NEW.composition_json,'$.capability.implementation_ref')='capi_image_generation_native@1'
      AND json_extract(NEW.composition_json,'$.capability.output_contract')='visual.artifact.v1'
      AND (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') x WHERE json_extract(x.value,'$.resource.kind')='capability')=1
      AND (SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') x WHERE json_extract(x.value,'$.resource.kind')='capability')=1
  ) THEN RAISE(ABORT,'reference Capability composition mismatch') END;
END
"""


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    expected = {
        SOURCE_TRIGGER: PREVIOUS.SOURCE_DDL,
        CAPABILITY_TRIGGER: PREVIOUS.CAPABILITY_DDL,
    }
    for name, ddl in expected.items():
        row = db.exec_driver_sql("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or row[0] != "trigger" or PREVIOUS._normalized(row[1]) != PREVIOUS._normalized(ddl):
            raise RuntimeError("reference Capability Skill schema drift")
    db.exec_driver_sql("SAVEPOINT reference_capability_skill")
    try:
        for name in expected:
            db.exec_driver_sql(f"DROP TRIGGER {name}")
        db.exec_driver_sql(SOURCE_DDL)
        db.exec_driver_sql(CAPABILITY_DDL)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_capability_skill")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_capability_skill")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_capability_skill")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
