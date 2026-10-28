"""Bind existing model receipts to the source and a live Plan step."""

from importlib import import_module

from alembic import context

revision = "0133_reference_plan_steps"
down_revision = "0132_reference_office_reads"
branch_labels = None
depends_on = None

PREVIOUS = import_module("bowhead_core.migrations.versions.0129_reference_lead_prompt")
INVOCATIONS = import_module("bowhead_core.migrations.versions.0071_reference_model_invocation")
EXTENSIONS = import_module("bowhead_core.migrations.versions.0081_skill_read_extensions")

VIEW = """
CREATE VIEW chat_reference_plan_step_scope AS
SELECT p.id AS plan_id,p.current_rev AS plan_revision,p.spec_json,s.id AS step_id,
 r.turn_id AS source_turn_id,r.execution_id AS source_execution_id,r.composition_digest,
 t.id AS execution_turn_id,e.id AS execution_id,d.owner_id,t.execution_generation AS generation,
 session.id AS session_id,session.user_id,session.workspace_id,w.team_id
FROM plans p
JOIN plan_steps s ON s.plan_id=p.id
JOIN turns t ON t.id=p.execution_turn_id AND t.session_id=p.session_id
JOIN sessions session ON session.id=t.session_id AND session.active_turn_id=t.id
JOIN workspaces w ON w.id=session.workspace_id
JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=session.id
JOIN turn_agent_execution e ON e.turn_id=t.id AND e.dispatch_source='plan'
JOIN chat_reference_resolutions r ON r.turn_id=json_extract(p.spec_json,'$.reference.source_turn_id') AND r.resolution_seq=1
JOIN chat_reference_plans source ON source.turn_id=r.turn_id
JOIN turns original ON original.id=source.turn_id AND original.session_id=session.id
JOIN turn_agent_execution receiver ON receiver.id=r.execution_id AND receiver.turn_id=original.id
JOIN chat_reference_plan_retention retained ON retained.turn_id=source.turn_id
WHERE json_valid(p.spec_json) AND json_extract(p.spec_json,'$.reference.contract')='reference-plan.v1'
 AND json_extract(p.spec_json,'$.reference.revision')=p.current_rev
 AND json_extract(p.spec_json,'$.reference.source_hash')=source.source_hash
 AND json_extract(p.spec_json,'$.reference.source_execution_id')=receiver.id
 AND json_extract(p.spec_json,'$.reference.composition_digest')=r.composition_digest
 AND json_extract(p.spec_json,'$.reference.profile')=receiver.runtime_profile_name
 AND EXISTS(SELECT 1 FROM json_each(p.spec_json,'$.reference.main_steps') selected WHERE selected.value=s.id)
 AND json_array_length(json_extract(p.spec_json,'$.reference.node_ids'))=json_array_length(json_extract(source.plan_json,'$.admissions'))
 AND NOT EXISTS(SELECT 1 FROM json_each(source.plan_json,'$.admissions') admission WHERE NOT EXISTS(
   SELECT 1 FROM json_each(p.spec_json,'$.reference.node_ids') node WHERE node.value=json_extract(admission.value,'$.node_id')))

 AND p.user_id=session.user_id AND p.workspace_id=session.workspace_id AND p.status='running'
 AND s.status='running' AND s.included=1 AND s.agent_name=receiver.runtime_profile_name
 AND e.dispatch_key='step:'||s.id||':rev:'||p.current_rev||':attempt:'||CAST(substr(e.dispatch_key,length('step:'||s.id||':rev:'||p.current_rev||':attempt:')+1) AS INTEGER)
 AND CAST(substr(e.dispatch_key,length('step:'||s.id||':rev:'||p.current_rev||':attempt:')+1) AS INTEGER)>0
 AND e.agent_id=receiver.agent_id AND e.team_id=w.team_id AND receiver.team_id=w.team_id
 AND e.agent_source_kind='team' AND receiver.agent_source_kind='team'
 AND e.revision_ref=receiver.revision_ref AND e.release_id=receiver.release_id
 AND e.policy_fingerprint=receiver.policy_fingerprint AND e.capability_fingerprint=receiver.capability_fingerprint
 AND e.status='running' AND receiver.status='admitted' AND receiver.started_at IS NULL
 AND json_extract(source.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND json_extract(r.composition_json,'$.runtime_policy') IN ('initial-primary-skill-read.v1','initial-primary-skill-read.v2','initial-primary-skill-baseline.v1','initial-primary-baseline.v1','initial-primary-skill-extension.v1')
 AND r.created_generation=1 AND retained.available AND retained.deadline>julianday('now')
 AND t.status='running' AND t.execution_generation=1 AND d.lease_execution_generation=1
 AND d.state='spawned' AND d.owner_id IS NOT NULL AND d.lease_kind='turn'
 AND d.dispatch_kind='start' AND d.parent_turn_id IS NULL AND d.cancel_requested_at IS NULL
 AND julianday(d.lease_expires_at)>julianday('now')
"""
# SQLite may reorder joins before WHERE predicates. Ordinary Plans have an
# empty spec; a reference query must not make those rows raise malformed JSON.
VIEW = VIEW.replace("json_extract(p.spec_json,", "json_extract(CASE WHEN json_valid(p.spec_json) THEN p.spec_json ELSE '{}' END,")
VIEW = VIEW.replace("json_each(p.spec_json,", "json_each(CASE WHEN json_valid(p.spec_json) THEN p.spec_json ELSE '{}' END,")

SOURCE_GUARD = """
CREATE TRIGGER trg_reference_plan_source_immutable BEFORE UPDATE OF spec_json ON plans
WHEN json_valid(OLD.spec_json) AND json_type(CASE WHEN json_valid(OLD.spec_json) THEN OLD.spec_json ELSE '{}' END,'$.reference') IS NOT NULL AND (
 NOT json_valid(NEW.spec_json) OR
 json_remove(OLD.spec_json,'$.reference.revision','$.reference.main_steps') IS NOT
 json_remove(CASE WHEN json_valid(NEW.spec_json) THEN NEW.spec_json ELSE '{}' END,'$.reference.revision','$.reference.main_steps'))
BEGIN SELECT RAISE(ABORT,'reference Plan source is immutable'); END
"""


def _trigger(module, name):
    return next(sql for sql in module.DDL if f"CREATE TRIGGER {name} " in sql)


CONTEXT_FENCE = """EXISTS(SELECT 1 FROM chat_reference_plan_step_scope step
 WHERE step.source_turn_id=NEW.turn_id AND step.execution_id=NEW.execution_id
 AND step.composition_digest=NEW.composition_digest AND step.generation=NEW.created_generation
 AND NEW.resolution_seq=1 AND NEW.retention_state='available')"""
CONTEXT_DDL = PREVIOUS._replace(
    PREVIOUS.MODEL_DDL,
    "WHEN NEW.retention_state<>'available' OR NOT EXISTS",
    f"WHEN NOT {CONTEXT_FENCE} AND (NEW.retention_state<>'available' OR NOT EXISTS",
)
CONTEXT_DDL = PREVIOUS._replace(CONTEXT_DDL, "BEGIN SELECT RAISE", ") BEGIN SELECT RAISE")

INVOCATION_OLD = _trigger(INVOCATIONS, "trg_reference_model_invocation_scope")
INVOCATION_FENCE = """EXISTS(SELECT 1 FROM chat_reference_plan_step_scope step
 JOIN chat_reference_model_contexts c ON c.turn_id=step.source_turn_id AND c.execution_id=step.execution_id
 WHERE c.turn_id=NEW.turn_id AND c.context_id=NEW.context_id AND c.execution_id=NEW.execution_id
 AND c.created_generation=NEW.created_generation AND c.subagent_id=NEW.subagent_id
 AND c.composition_digest=step.composition_digest AND c.created_generation=step.generation
 AND c.retention_state='available')"""
INVOCATION_DDL = PREVIOUS._replace(
    INVOCATION_OLD,
    "OR NOT EXISTS (",
    f"OR (NOT {INVOCATION_FENCE} AND NOT EXISTS (",
)
INVOCATION_DDL = PREVIOUS._replace(INVOCATION_DDL, "OR EXISTS (SELECT 1 FROM chat_reference_model_invocations", ") OR EXISTS (SELECT 1 FROM chat_reference_model_invocations")

COMPLETION_OLD = _trigger(INVOCATIONS, "trg_reference_model_invocation_completion")
COMPLETION_DDL = PREVIOUS._replace(
    COMPLETION_OLD,
    "WHERE turn_id=NEW.id",
    "WHERE (turn_id=NEW.id OR execution_id IN(SELECT id FROM turn_agent_execution WHERE turn_id=NEW.id))",
)
EXTENSION_OLD = _trigger(EXTENSIONS, "trg_reference_extension_invocation_proof")
EXTENSION_DDL = PREVIOUS._replace(
    EXTENSION_OLD,
    "r.turn_id=NEW.turn_id AND r.execution_id=NEW.execution_id",
    "r.turn_id=NEW.turn_id AND (r.execution_id=NEW.execution_id OR EXISTS(SELECT 1 FROM chat_reference_plan_step_scope step "
    "WHERE step.source_turn_id=r.turn_id AND step.source_execution_id=r.execution_id AND step.execution_id=NEW.execution_id))",
)
CHANGES = {
    "trg_reference_model_context_scope": (PREVIOUS.MODEL_DDL, CONTEXT_DDL),
    "trg_reference_model_invocation_scope": (INVOCATION_OLD, INVOCATION_DDL),
    "trg_reference_model_invocation_completion": (COMPLETION_OLD, COMPLETION_DDL),
    "trg_reference_extension_invocation_proof": (EXTENSION_OLD, EXTENSION_DDL),
}


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    for name, (old, _) in CHANGES.items():
        row = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or PREVIOUS.RESULTS._normalized(row[0]) != PREVIOUS.RESULTS._normalized(old):
            raise RuntimeError(f"reference Plan schema drift: {name}")
    db.exec_driver_sql("SAVEPOINT reference_plan_steps")
    try:
        db.exec_driver_sql(VIEW)
        db.exec_driver_sql(SOURCE_GUARD)
        db.exec_driver_sql("CREATE UNIQUE INDEX idx_reference_plan_source ON plans(json_extract(CASE WHEN json_valid(spec_json) THEN spec_json ELSE '{}' END,'$.reference.source_turn_id'))")
        for name, (_, replacement) in CHANGES.items():
            db.exec_driver_sql(f"DROP TRIGGER {name}")
            db.exec_driver_sql(replacement)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_plan_steps")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_plan_steps")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_plan_steps")
        raise


def downgrade():
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
