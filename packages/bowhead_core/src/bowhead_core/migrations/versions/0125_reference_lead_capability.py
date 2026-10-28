"""Admit the existing image Capability through exact Lead delegation."""

from importlib import import_module

from alembic import context

revision = "0125_reference_lead_capability"
down_revision = "0124_file_category_codes"
branch_labels = None
depends_on = None

LEAD = import_module("bowhead_core.migrations.versions.0113_reference_lead_skill_only")
CAPABILITY = import_module("bowhead_core.migrations.versions.0088_reference_capability_skill")

PLAN_DDL = """
CREATE TRIGGER trg_reference_lead_plan BEFORE INSERT ON chat_reference_plans
WHEN json_type(NEW.plan_json,'$.orchestration_contract') IS NOT NULL
AND NOT COALESCE(json_extract(NEW.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND json_array_length(NEW.plan_json,'$.admissions') BETWEEN 1 AND 5
 AND (SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='agent') BETWEEN 0 AND 1
 AND (SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='skill') BETWEEN 0 AND 3
 AND (SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='capability') BETWEEN 0 AND 1
 AND NOT EXISTS (SELECT 1 FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE COALESCE(json_extract(a.value,'$.resource.kind') IN ('agent','skill','capability'),0)=0),0)
BEGIN SELECT RAISE(ABORT,'reference Lead plan mismatch'); END
"""

AUTO_DDL = LEAD.AUTO_GUARD
for old, new in (
    ("json_extract(n.value,'$.resource.kind')!='skill'", "json_extract(n.value,'$.resource.kind') NOT IN ('skill','capability')"),
    ("json_extract(a.value,'$.operation')='read'", "json_extract(a.value,'$.operation')=CASE json_extract(n.value,'$.resource.kind') WHEN 'skill' THEN 'read' ELSE 'execute' END"),
    ("json_extract(a.value,'$.resource.kind')='skill'", "json_extract(a.value,'$.resource.kind')=json_extract(n.value,'$.resource.kind')"),
    ("json_extract(a.value,'$.resource.source_kind')=json_extract(n.value,'$.resource.source_kind')", "json_extract(a.value,'$.resource.source_kind') IS json_extract(n.value,'$.resource.source_kind')"),
):
    if AUTO_DDL.count(old) != 1:
        raise RuntimeError("reference Lead Capability source drift")
    AUTO_DDL = AUTO_DDL.replace(old, new)

# The original composition still validates the Capability against source and
# baseline. A second resolution can only describe its exact Lead delegation;
# the independent Lead guard also binds the task, execution, worker and card.
DELEGATION = """AND NOT EXISTS (
    SELECT 1 FROM chat_reference_plans p
    JOIN chat_reference_resolutions original ON original.turn_id=p.turn_id AND original.resolution_seq=1
    WHERE p.turn_id=NEW.turn_id AND p.source_turn_id=NEW.source_turn_id
      AND json_extract(p.plan_json,'$.orchestration_contract')='lead-delegation.v1'
      AND json_extract(original.composition_json,'$.orchestration_contract')='lead-delegation.v1'
      AND json_extract(original.composition_json,'$.capability.resource.id')='cap_visualize'
      AND json_extract(original.composition_json,'$.capability.resource.version')=1
      AND json_extract(original.composition_json,'$.capability.implementation_ref')='capi_image_generation_native@1'
      AND NEW.resolution_seq=2 AND NEW.idempotency_key='reference:primary:delegation'
      AND NEW.execution_id=original.execution_id
      AND json_extract(NEW.composition_json,'$.runtime_policy')='lead-delegation.v1'
      AND json_extract(NEW.composition_json,'$.source_composition_digest')=original.composition_digest
      AND json_type(NEW.composition_json,'$.capability') IS NULL
  ) """
NEEDLE = "THEN RAISE(ABORT,'reference Capability composition mismatch')"
if CAPABILITY.CAPABILITY_DDL.count(NEEDLE) != 1:
    raise RuntimeError("reference Lead Capability composition source drift")
CAPABILITY_DDL = CAPABILITY.CAPABILITY_DDL.replace(NEEDLE, DELEGATION + NEEDLE)

GUARD_NAME = "trg_reference_lead_capability_plan"
GUARD = f"""
CREATE TRIGGER {GUARD_NAME} BEFORE INSERT ON chat_reference_plans
WHEN json_extract(NEW.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND EXISTS (SELECT 1 FROM json_each(NEW.plan_json,'$.admissions') a
   WHERE json_extract(a.value,'$.resource.kind')='capability'
   AND NOT COALESCE(json_extract(a.value,'$.resource.id')='cap_visualize'
     AND json_extract(a.value,'$.resource.version')=1
     AND json_extract(a.value,'$.operation')='execute',0))
BEGIN SELECT RAISE(ABORT,'reference Lead Capability plan mismatch'); END
"""
CHANGES = {
    LEAD.NAME: (LEAD.DDL, PLAN_DDL),
    "trg_reference_lead_skill_only_plan": (LEAD.AUTO_GUARD, AUTO_DDL),
    CAPABILITY.CAPABILITY_TRIGGER: (CAPABILITY.CAPABILITY_DDL, CAPABILITY_DDL),
}


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    normalize = CAPABILITY.PREVIOUS._normalized
    for name, (current, _) in CHANGES.items():
        row = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        if row is None or normalize(row[0]) != normalize(current):
            raise RuntimeError(f"reference Lead Capability schema drift: {name}")
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (GUARD_NAME,)).fetchone():
        raise RuntimeError("reference Lead Capability schema drift")
    db.exec_driver_sql("SAVEPOINT reference_lead_capability")
    try:
        for name, (_, replacement) in CHANGES.items():
            db.exec_driver_sql(f"DROP TRIGGER {name}")
            db.exec_driver_sql(replacement)
        db.exec_driver_sql(GUARD)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_lead_capability")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_lead_capability")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_lead_capability")
        raise


def downgrade():
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
