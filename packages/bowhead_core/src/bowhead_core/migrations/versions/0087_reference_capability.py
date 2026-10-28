"""Bind the first exact Capability reference to automatic primary execution.

Revision ID: 0087_reference_capability
Revises: 0086_reference_auto_composition
"""

from __future__ import annotations

from importlib import import_module

from alembic import context

revision = "0087_reference_capability"
down_revision = "0086_reference_auto_composition"
branch_labels = None
depends_on = None

AUTO = import_module("bowhead_core.migrations.versions.0084_reference_auto_receiver")
COMPOSITION = import_module("bowhead_core.migrations.versions.0086_reference_auto_composition")
SOURCE_TRIGGER = AUTO.GUARD
COMPOSITION_TRIGGER = COMPOSITION.TRIGGER
CAPABILITY_TRIGGER = "trg_reference_capability_composition"

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
      AND (
        ((SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n WHERE json_extract(n.value,'$.type')='reference') BETWEEN 1 AND 3
          AND NOT EXISTS (SELECT 1 FROM json_each(s.input_document_json,'$.nodes') n
            WHERE json_extract(n.value,'$.type')='reference' AND COALESCE(json_extract(n.value,'$.resource.kind')='skill',0)=0)
          AND NOT EXISTS (SELECT 1 FROM json_each(p.plan_json,'$.admissions') a
            WHERE COALESCE(json_extract(a.value,'$.operation')='read' AND json_extract(a.value,'$.resource.kind')='skill',0)=0))
        OR
        ((SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n WHERE json_extract(n.value,'$.type')='reference')=1
          AND NOT EXISTS (SELECT 1 FROM json_each(s.input_document_json,'$.nodes') n
            WHERE json_extract(n.value,'$.type')='reference' AND COALESCE(json_extract(n.value,'$.resource.kind')='capability',0)=0)
          AND NOT EXISTS (SELECT 1 FROM json_each(p.plan_json,'$.admissions') a
            WHERE COALESCE(json_extract(a.value,'$.operation')='execute' AND json_extract(a.value,'$.resource.kind')='capability',0)=0))
      )
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

COMPOSITION_DDL = COMPOSITION.DDL[0].replace(
    "'initial-primary-skill-extension.v1')",
    "'initial-primary-skill-extension.v1','initial-primary-baseline.v1')",
)

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
      AND json_extract(NEW.composition_json,'$.runtime_policy')='initial-primary-baseline.v1'
      AND json_array_length(NEW.composition_json,'$.members')=0
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


def _normalized(value: str) -> str:
    return "".join(value.lower().replace('"', "").split()).rstrip(";")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    expected = {
        SOURCE_TRIGGER: AUTO.SOURCE_GUARD,
        COMPOSITION_TRIGGER: COMPOSITION.DDL[0],
    }
    for name, ddl in expected.items():
        row = db.exec_driver_sql("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or row[0] != "trigger" or _normalized(row[1]) != _normalized(ddl):
            raise RuntimeError("reference Capability schema drift")
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (CAPABILITY_TRIGGER,)).fetchone():
        raise RuntimeError("reference Capability schema drift")
    db.exec_driver_sql("SAVEPOINT reference_capability")
    try:
        db.exec_driver_sql(f"DROP TRIGGER {SOURCE_TRIGGER}")
        db.exec_driver_sql(SOURCE_DDL)
        db.exec_driver_sql(f"DROP TRIGGER {COMPOSITION_TRIGGER}")
        db.exec_driver_sql(COMPOSITION_DDL)
        db.exec_driver_sql(CAPABILITY_DDL)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_capability")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_capability")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_capability")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
