"""Separate a prepared reference reservation from its actual Lead delegation."""
from importlib import import_module

from alembic import context

revision = "0111_reference_lead_delegation"
down_revision = "0110_builtin_datarc_key_scope"
branch_labels = None
depends_on = None

PREVIOUS = import_module("bowhead_core.migrations.versions.0106_reference_queue_lineage")
NAMES = (
    "trg_reference_skill_composition_material",
    "trg_reference_skill_baseline_composition",
    "trg_reference_skill_invocation_composition",
    "trg_reference_primary_baseline_composition",
    "trg_reference_skill_extension_composition",
)
CURRENT = {name: PREVIOUS.DDL[name] for name in NAMES}
RESERVED = """(e.status='running' OR (e.status='admitted' AND e.started_at IS NULL
 AND json_extract(NEW.composition_json,'$.orchestration_contract')='lead-delegation.v1'
 AND EXISTS (SELECT 1 FROM chat_reference_plans rp WHERE rp.turn_id=NEW.turn_id
 AND json_extract(rp.plan_json,'$.orchestration_contract')='lead-delegation.v1')))"""
DDL = {name: sql.replace("e.status='running'", RESERVED) for name, sql in CURRENT.items()}
if any(sql.count("e.status='running'") != 1 for sql in CURRENT.values()):
    raise RuntimeError("reference Lead composition source drift")
MODEL_CURRENT = PREVIOUS.DDL["trg_reference_model_context_scope"]
COMPLETION_CURRENT = PREVIOUS.PREVIOUS.CURRENT["trg_reference_primary_completion"]
LEAD_MODEL_FENCE = """EXISTS (
 SELECT 1 FROM chat_reference_plans p JOIN chat_reference_resolutions actual
 ON actual.turn_id=p.turn_id AND actual.resolution_seq=2
 JOIN chat_reference_evidence v ON v.turn_id=actual.turn_id AND v.execution_id=actual.execution_id
 JOIN chat_turn_spawn_dispatch d ON d.turn_id=actual.turn_id
 JOIN clarification_requests q ON q.turn_id=actual.turn_id
 WHERE p.turn_id=r.turn_id AND json_extract(p.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND r.resolution_seq=1 AND actual.execution_id=r.execution_id
 AND actual.created_generation=NEW.created_generation AND NEW.created_generation>1
 AND json_extract(actual.composition_json,'$.runtime_policy')='lead-delegation.v1'
 AND v.resolution_seq=2 AND v.kind='delegate_started' AND v.created_generation=NEW.created_generation
 AND v.subagent_id=NEW.subagent_id
 AND q.status='resolved' AND json_extract(q.resume_context_json,'$.reference_continuation.contract')='reference-lead-predispatch.v1'
 AND q.session_id=d.session_id AND json_extract(q.resume_context_json,'$.reference_continuation.execution_id')=r.execution_id
 AND json_extract(q.resume_context_json,'$.reference_continuation.source_hash')=p.source_hash
 AND json_extract(q.resume_context_json,'$.reference_continuation.composition_digest')=r.composition_digest
 AND d.state='spawned' AND d.lease_kind='resume' AND d.lease_execution_generation=NEW.created_generation
 AND d.cancel_requested_at IS NULL AND julianday(d.lease_expires_at)>julianday('now')
)"""
MODEL_NEEDLE = "r.created_generation=NEW.created_generation OR EXISTS ("
COMPLETION_NEEDLE = "WHERE r.turn_id=NEW.id AND r.resolution_seq=1 AND"
if MODEL_CURRENT.count(MODEL_NEEDLE) != 1 or COMPLETION_CURRENT.count(COMPLETION_NEEDLE) != 1:
    raise RuntimeError("reference Lead runtime source drift")
EXTRA_CURRENT = {
    "trg_reference_model_context_scope": MODEL_CURRENT,
    "trg_reference_primary_completion": COMPLETION_CURRENT,
}
EXTRA_DDL = {
    "trg_reference_model_context_scope": MODEL_CURRENT.replace(MODEL_NEEDLE, f"r.created_generation=NEW.created_generation OR {LEAD_MODEL_FENCE} OR EXISTS ("),
    "trg_reference_primary_completion": COMPLETION_CURRENT.replace(
        COMPLETION_NEEDLE,
        "WHERE r.turn_id=NEW.id AND (r.resolution_seq=1 OR (r.resolution_seq=2 "
        "AND json_extract(r.composition_json,'$.runtime_policy')='lead-delegation.v1')) AND",
    ),
}

GUARDS = {
    "trg_reference_lead_plan": """
CREATE TRIGGER trg_reference_lead_plan BEFORE INSERT ON chat_reference_plans
WHEN json_type(NEW.plan_json,'$.orchestration_contract') IS NOT NULL
AND NOT COALESCE(json_extract(NEW.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND (SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='agent')=1
 AND NOT EXISTS (SELECT 1 FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind') NOT IN ('agent','skill')),0)
BEGIN SELECT RAISE(ABORT,'reference Lead plan mismatch'); END
""",
    "trg_reference_lead_delegation": """
CREATE TRIGGER trg_reference_lead_delegation BEFORE INSERT ON chat_reference_resolutions
WHEN json_extract(NEW.composition_json,'$.runtime_policy')='lead-delegation.v1'
AND NOT EXISTS (
 SELECT 1 FROM chat_reference_plans p
 JOIN chat_reference_resolutions original ON original.turn_id=p.turn_id AND original.resolution_seq=1
 JOIN turn_agent_binding b ON b.turn_id=p.turn_id
 JOIN turn_agent_execution e ON e.id=b.primary_execution_id
 JOIN chat_turn_spawn_dispatch d ON d.turn_id=p.turn_id
 JOIN chat_reference_content_snapshots c ON c.turn_id=p.turn_id
  AND c.content_key=json_extract(NEW.composition_json,'$.task.content_key')
 WHERE p.turn_id=NEW.turn_id AND p.retention_state='available'
 AND json_extract(p.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND json_extract(original.composition_json,'$.orchestration_contract')='lead-delegation.v1'
 AND NEW.resolution_seq=2 AND NEW.idempotency_key='reference:primary:delegation'
 AND NEW.source_turn_id=p.source_turn_id AND NEW.execution_id=e.id
 AND e.status IN ('admitted','running') AND NEW.created_generation>=1
 AND json_extract(NEW.composition_json,'$.delegation_role')='primary'
 AND json_extract(NEW.composition_json,'$.node_id')=json_extract(original.composition_json,'$.node_id')
 AND json_extract(NEW.composition_json,'$.source_hash')=json_extract(p.plan_json,'$.source_hash')
 AND json_extract(NEW.composition_json,'$.source_composition_digest')=original.composition_digest
 AND json_extract(NEW.composition_json,'$.owner_id')=d.owner_id
 AND d.state='spawned' AND d.lease_kind=CASE WHEN NEW.created_generation=1 THEN 'turn' ELSE 'resume' END
 AND d.lease_execution_generation=NEW.created_generation
 AND (NEW.created_generation=1 OR EXISTS (
   SELECT 1 FROM clarification_requests q WHERE q.turn_id=NEW.turn_id AND q.status='resolved'
   AND json_extract(q.resume_context_json,'$.reference_continuation.contract')='reference-lead-predispatch.v1'
   AND json_extract(q.resume_context_json,'$.reference_continuation.execution_id')=NEW.execution_id
   AND json_extract(q.resume_context_json,'$.reference_continuation.source_hash')=p.source_hash
   AND json_extract(q.resume_context_json,'$.reference_continuation.composition_digest')=original.composition_digest
   AND q.session_id=d.session_id
 ))
 AND d.cancel_requested_at IS NULL AND julianday(d.lease_expires_at)>julianday('now')
 AND c.retention_state='available' AND c.created_generation=NEW.created_generation
 AND c.descriptor_json=json_extract(NEW.composition_json,'$.task')
 AND json_extract(c.descriptor_json,'$.content_kind')='delegation_task'
 AND json_extract(c.descriptor_json,'$.resource')=json_extract(original.composition_json,'$.resource')
 AND json_extract(c.descriptor_json,'$.node_id')=json_extract(original.composition_json,'$.node_id')
 AND julianday(c.expires_at)>julianday('now')
)
BEGIN SELECT RAISE(ABORT,'reference Lead delegation mismatch'); END
""",
    "trg_reference_lead_task": """
CREATE TRIGGER trg_reference_lead_task BEFORE INSERT ON chat_reference_content_snapshots
WHEN json_extract(NEW.descriptor_json,'$.content_kind')='delegation_task'
AND NOT EXISTS (SELECT 1 FROM chat_reference_plans p WHERE p.turn_id=NEW.turn_id
 AND json_extract(p.plan_json,'$.orchestration_contract')='lead-delegation.v1')
BEGIN SELECT RAISE(ABORT,'reference delegation task mismatch'); END
""",
}


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    normalize = PREVIOUS.FOLLOW_UP._normalized
    for name, sql in {**CURRENT, **EXTRA_CURRENT}.items():
        row = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        if row is None or normalize(row[0]) != normalize(sql):
            raise RuntimeError("reference Lead schema drift")
    if any(db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() for name in GUARDS):
        raise RuntimeError("reference Lead schema drift")
    db.exec_driver_sql("SAVEPOINT reference_lead_delegation")
    try:
        for name, sql in {**DDL, **EXTRA_DDL}.items():
            db.exec_driver_sql(f"DROP TRIGGER {name}")
            db.exec_driver_sql(sql)
        for sql in GUARDS.values():
            db.exec_driver_sql(sql)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_lead_delegation")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_lead_delegation")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_lead_delegation")
        raise


def downgrade():
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
