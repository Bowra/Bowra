"""Distinguish explicit task method text from published Agent extensions."""

from importlib import import_module

from alembic import context

revision = "0214_task_method_references"
down_revision = "0213_knowledge_navigation"
branch_labels = None
depends_on = None

LEAD = import_module("bowhead_core.migrations.versions.0111_reference_lead_delegation")
EXTENSION = import_module("bowhead_core.migrations.versions.0082_extension_policy_epoch")
MATERIAL = "trg_reference_skill_extension_composition"
METHOD = "trg_reference_method_composition"
SCOPE = EXTENSION.SCOPE_TRIGGER
EPOCH = EXTENSION.TRIGGER
CURRENT = {MATERIAL: LEAD.DDL[MATERIAL], SCOPE: EXTENSION.SCOPE_DDL, EPOCH: EXTENSION.DDL}
MARKER = "json_extract(NEW.composition_json,'$.method_reference')"
OWNER = """ AND e.agent_source_kind='team'
 AND EXISTS (SELECT 1 FROM team_subagent_revision_extensions x WHERE x.team_id=e.team_id AND x.agent_id=e.agent_id
   AND x.revision_ref=e.revision_ref AND x.definition_hash=json_extract(NEW.composition_json,'$.baseline.source_definition_hash')
   AND x.extension_policy_digest=json_extract(NEW.composition_json,'$.extension_policy_digest'))"""
METHOD_ORIGIN = """ AND json_extract(NEW.composition_json,'$.method_reference')='user-selected-method.v1'
 AND json_extract(NEW.composition_json,'$.runtime_policy')='initial-primary-skill-extension.v1'
 AND EXISTS (SELECT 1 FROM chat_reference_plans p WHERE p.turn_id=NEW.turn_id
   AND p.source_hash=json_extract(NEW.composition_json,'$.source_hash') AND p.retention_state='available'
   AND NOT EXISTS (SELECT 1 FROM json_each(NEW.composition_json,'$.members') m
     WHERE json_extract(m.value,'$.provenance')='readonly_extension' AND NOT EXISTS (
       SELECT 1 FROM json_each(p.plan_json,'$.admissions') a
       WHERE json_extract(a.value,'$.resource')=json_extract(m.value,'$.resource')
         AND json_extract(a.value,'$.node_id')=json_extract(m.value,'$.node_id')
         AND json_extract(a.value,'$.operation')='read')))"""
if CURRENT[MATERIAL].count(OWNER) != 1:
    raise RuntimeError("task method composition source drift")
METHOD_DDL = CURRENT[MATERIAL].replace(MATERIAL, METHOD).replace(
    "WHEN json_extract(NEW.composition_json,'$.runtime_policy')='initial-primary-skill-extension.v1'",
    f"WHEN {MARKER} IS NOT NULL",
).replace(OWNER, METHOD_ORIGIN)

METHOD_CHECK = """EXISTS (
 SELECT 1 FROM chat_reference_model_contexts c
 JOIN chat_reference_resolutions r ON r.turn_id=c.turn_id AND r.resolution_seq=c.resolution_seq
 JOIN turn_agent_execution e ON e.id=c.execution_id
 JOIN chat_reference_plans p ON p.turn_id=c.turn_id
 WHERE c.turn_id=NEW.turn_id AND c.context_id=NEW.context_id AND c.execution_id=NEW.execution_id
 AND c.created_generation=NEW.created_generation AND c.composition_digest=NEW.composition_digest
 AND c.retention_state='available' AND e.status='running'
 AND r.kind='composition' AND r.resolution_seq=1 AND r.composition_digest=c.composition_digest
 AND p.retention_state='available' AND p.source_hash=json_extract(r.composition_json,'$.source_hash')
 AND json_extract(r.composition_json,'$.method_reference')='user-selected-method.v1'
 AND json_extract(r.composition_json,'$.runtime_policy')='initial-primary-skill-extension.v1'
 AND json_type(NEW.policy_check_json,'$.schema_version')='integer'
 AND json_extract(NEW.policy_check_json,'$.schema_version')=3
 AND json_extract(NEW.policy_check_json,'$.source_hash')=p.source_hash
 AND json_extract(NEW.policy_check_json,'$.receiver_digest')=json_extract(r.composition_json,'$.receiver_digest')
 AND json_extract(NEW.policy_check_json,'$.original_extension_digest')=json_extract(r.composition_json,'$.extension_digest')
 AND json_extract(NEW.policy_check_json,'$.current_revision_ref')=e.revision_ref
 AND json_extract(NEW.policy_check_json,'$.current_release_id') IS e.release_id
 AND json_extract(NEW.policy_check_json,'$.current_definition_hash')=json_extract(r.composition_json,'$.baseline.source_definition_hash')
 AND json_extract(NEW.policy_check_json,'$.current_extension_policy_digest')=json_extract(r.composition_json,'$.extension_policy_digest')
 AND json_type(NEW.policy_check_json,'$.current_access_epoch')='integer'
 AND json_extract(NEW.policy_check_json,'$.current_access_epoch')=COALESCE((SELECT epoch FROM access_policy_state WHERE team_id=e.team_id),0)
)"""


def _with_task_branch(sql):
    guard, body = sql.split("WHEN NOT ", 1)
    condition, failure = body.split("BEGIN ", 1)
    condition = condition.rstrip()
    return guard + "WHEN NOT (" + condition + " OR " + METHOD_CHECK + ")\nBEGIN " + failure


DDL = {
    MATERIAL: CURRENT[MATERIAL].replace("AND NOT EXISTS (", f"AND {MARKER} IS NULL\nAND NOT EXISTS (", 1),
    SCOPE: _with_task_branch(CURRENT[SCOPE]),
    EPOCH: _with_task_branch(CURRENT[EPOCH]),
}


def _normalized(sql):
    return " ".join(sql.split()).rstrip(";")


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    for name, expected in CURRENT.items():
        row = db.exec_driver_sql("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or row[0] != "trigger" or _normalized(row[1]) != _normalized(expected):
            raise RuntimeError("task method schema drift")
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (METHOD,)).fetchone():
        raise RuntimeError("task method schema drift")
    db.exec_driver_sql("SAVEPOINT task_method_references")
    try:
        for name, sql in DDL.items():
            db.exec_driver_sql(f"DROP TRIGGER {name}")
            db.exec_driver_sql(sql)
        db.exec_driver_sql(METHOD_DDL)
        db.exec_driver_sql("RELEASE SAVEPOINT task_method_references")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT task_method_references")
        db.exec_driver_sql("RELEASE SAVEPOINT task_method_references")
        raise


def downgrade():
    raise NotImplementedError("Forward-only; retain compatible readers or restore the paired backup.")
