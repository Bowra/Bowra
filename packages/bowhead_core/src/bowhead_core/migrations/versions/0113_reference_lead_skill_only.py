"""Allow a Lead-selected primary for a Skill-only reference source."""

from importlib import import_module

from alembic import context

revision = "0113_reference_lead_skill_only"
down_revision = "0112_reference_lead_postdispatch"
branch_labels = None
depends_on = None

PREVIOUS = import_module("bowhead_core.migrations.versions.0112_reference_lead_postdispatch")
LEAD = PREVIOUS.PREVIOUS
AUTOMATIC = import_module("bowhead_core.migrations.versions.0086_reference_auto_composition")
PROMPT = import_module("bowhead_core.migrations.versions.0095_reference_mcp_prompt_results")
NAME = "trg_reference_lead_plan"
CURRENT = LEAD.GUARDS[NAME]
AGENT_COUNT = """(SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='agent')=1"""
if CURRENT.count(AGENT_COUNT) != 1:
    raise RuntimeError("reference Lead plan source drift")
DDL = CURRENT.replace(
    AGENT_COUNT,
    """((SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='agent')=1
 OR ((SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='agent')=0
 AND (SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='skill') BETWEEN 1 AND 3))""",
)

DELEGATION_NAME = "trg_reference_lead_delegation"
DELEGATION_CURRENT = LEAD.GUARDS[DELEGATION_NAME]
DELEGATION_DDL = DELEGATION_CURRENT
for needle in (
    "json_extract(NEW.composition_json,'$.node_id')=json_extract(original.composition_json,'$.node_id')",
    "json_extract(c.descriptor_json,'$.node_id')=json_extract(original.composition_json,'$.node_id')",
):
    if DELEGATION_DDL.count(needle) != 1:
        raise RuntimeError("reference Lead Skill-only delegation source drift")
    DELEGATION_DDL = DELEGATION_DDL.replace(needle, needle.replace("=json_extract", " IS json_extract"))

AUTO_COMPOSITION_CURRENT, AUTO_MATERIAL_CURRENT = PROMPT.AUTO_DDL, AUTOMATIC.DDL[1]
COMPOSITION_NEEDLE = "AND json_type(NEW.composition_json,'$.content.node_id') IS NULL)))"
MATERIAL_NEEDLE = "AND json_extract(NEW.descriptor_json,'$.content_kind')='skill_extension')))"
if AUTO_COMPOSITION_CURRENT.count(COMPOSITION_NEEDLE) != 1 or AUTO_MATERIAL_CURRENT.count(MATERIAL_NEEDLE) != 1:
    raise RuntimeError("reference Lead automatic source drift")
AUTO_COMPOSITION_DDL = AUTO_COMPOSITION_CURRENT.replace(
    COMPOSITION_NEEDLE,
    """AND json_type(NEW.composition_json,'$.content.node_id') IS NULL)
        OR (b.binding_source='lead' AND e.dispatch_source='lead'
          AND NEW.resolution_seq=2
          AND json_extract(NEW.composition_json,'$.runtime_policy')='lead-delegation.v1'
          AND EXISTS (SELECT 1 FROM chat_reference_plans p WHERE p.turn_id=NEW.turn_id
            AND json_extract(p.plan_json,'$.orchestration_contract')='lead-delegation.v1')
          AND json_extract(NEW.composition_json,'$.task.resource.kind')='agent'
          AND json_extract(NEW.composition_json,'$.task.resource.id')=b.requested_agent_id
          AND json_extract(NEW.composition_json,'$.task.resource.source_kind')=b.agent_source_kind
          AND json_extract(NEW.composition_json,'$.task.resource.version')=b.revision_ref
          AND json_extract(NEW.composition_json,'$.task.resource.release_id') IS b.release_id
          AND json_extract(NEW.composition_json,'$.task.resource.assignment_id') IS b.assignment_id)))""",
)
AUTO_MATERIAL_DDL = AUTO_MATERIAL_CURRENT.replace(
    MATERIAL_NEEDLE,
    """AND json_extract(NEW.descriptor_json,'$.content_kind')='skill_extension')
          OR (NEW.content_key='reference:primary:delegation-task'
            AND json_extract(NEW.descriptor_json,'$.content_kind')='delegation_task'
            AND EXISTS (SELECT 1 FROM chat_reference_plans p WHERE p.turn_id=NEW.turn_id
              AND json_extract(p.plan_json,'$.orchestration_contract')='lead-delegation.v1'))))""",
)

AUTO_GUARD = """
CREATE TRIGGER trg_reference_lead_skill_only_plan BEFORE INSERT ON chat_reference_plans
WHEN json_extract(NEW.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND NOT EXISTS (SELECT 1 FROM json_each(NEW.plan_json,'$.admissions') a
   WHERE json_extract(a.value,'$.resource.kind')='agent')
 AND NOT EXISTS (
   SELECT 1 FROM chat_turn_user_sources s WHERE s.turn_id=NEW.turn_id
   AND json_valid(s.input_document_json)
   AND json_array_length(NEW.plan_json,'$.admissions')=(
     SELECT COUNT(*) FROM json_each(s.input_document_json,'$.nodes') n
     WHERE json_extract(n.value,'$.type')='reference')
   AND NOT EXISTS (SELECT 1 FROM json_each(s.input_document_json,'$.nodes') n
     WHERE json_extract(n.value,'$.type')='reference'
     AND (json_extract(n.value,'$.resource.kind')!='skill' OR NOT EXISTS (
       SELECT 1 FROM json_each(NEW.plan_json,'$.admissions') a
       WHERE json_extract(a.value,'$.node_id')=json_extract(n.value,'$.node_id')
       AND json_extract(a.value,'$.operation')='read'
       AND json_extract(a.value,'$.resource.kind')='skill'
       AND json_extract(a.value,'$.resource.source_kind')=json_extract(n.value,'$.resource.source_kind')
       AND json_extract(a.value,'$.resource.id')=json_extract(n.value,'$.resource.id')
       AND json_extract(a.value,'$.resource.version')=json_extract(n.value,'$.resource.version'))))
 )
BEGIN SELECT RAISE(ABORT,'reference Lead Skill-only source mismatch'); END
"""


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    normalize = LEAD.PREVIOUS.FOLLOW_UP._normalized
    for name, sql in (
        (NAME, CURRENT),
        (DELEGATION_NAME, DELEGATION_CURRENT),
        (AUTOMATIC.TRIGGER, AUTO_COMPOSITION_CURRENT),
        (AUTOMATIC.CONTENT_TRIGGER, AUTO_MATERIAL_CURRENT),
    ):
        row = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        if row is None or normalize(row[0]) != normalize(sql):
            raise RuntimeError(f"reference Lead Skill-only schema drift: {name}")
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='trg_reference_lead_skill_only_plan'").fetchone():
        raise RuntimeError("reference Lead Skill-only schema drift")
    db.exec_driver_sql("SAVEPOINT reference_lead_skill_only")
    try:
        for name, sql in (
            (NAME, DDL),
            (DELEGATION_NAME, DELEGATION_DDL),
            (AUTOMATIC.TRIGGER, AUTO_COMPOSITION_DDL),
            (AUTOMATIC.CONTENT_TRIGGER, AUTO_MATERIAL_DDL),
        ):
            db.exec_driver_sql(f"DROP TRIGGER {name}")
            db.exec_driver_sql(sql)
        db.exec_driver_sql(AUTO_GUARD)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_lead_skill_only")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_lead_skill_only")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_lead_skill_only")
        raise


def downgrade():
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
