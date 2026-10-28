"""Keep Prompt material separate from Lead delegation and reuse it on resume."""

from importlib import import_module

from alembic import context

revision = "0129_reference_lead_prompt"
down_revision = "0128_reference_lead_mcp"
branch_labels = None
depends_on = None

LEAD = import_module("bowhead_core.migrations.versions.0128_reference_lead_mcp")
RESULTS = import_module("bowhead_core.migrations.versions.0095_reference_mcp_prompt_results")
AUTO = import_module("bowhead_core.migrations.versions.0113_reference_lead_skill_only")
PAUSE = import_module("bowhead_core.migrations.versions.0112_reference_lead_postdispatch")


def _replace(sql, old, new):
    if sql.count(old) != 1:
        raise RuntimeError("reference Lead Prompt migration source drift")
    return sql.replace(old, new)


PROMPT_PLAN = """COALESCE(json_extract(NEW.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND (SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='agent'
       AND json_extract(a.value,'$.resource.source_kind')='team' AND json_extract(a.value,'$.operation')='delegate')=1
 AND (SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='agent')=1
 AND (SELECT COUNT(*) FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='mcp_prompt'
       AND json_extract(a.value,'$.operation')='expand') BETWEEN 1 AND 3
 AND json_array_length(NEW.plan_json,'$.admissions') BETWEEN 2 AND 4
 AND NOT EXISTS (SELECT 1 FROM json_each(NEW.plan_json,'$.admissions') a
      WHERE COALESCE(json_extract(a.value,'$.resource.kind') IN ('agent','mcp_prompt'),0)=0),0)"""
PLAN_DDL = _replace(LEAD.PLAN_DDL, "BEGIN SELECT RAISE", f"AND NOT {PROMPT_PLAN}\nBEGIN SELECT RAISE")
GUARD_NAME = "trg_reference_lead_prompt_plan"
GUARD = f"""
CREATE TRIGGER {GUARD_NAME} BEFORE INSERT ON chat_reference_plans
WHEN json_extract(NEW.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND EXISTS (SELECT 1 FROM json_each(NEW.plan_json,'$.admissions') a
   WHERE json_extract(a.value,'$.resource.kind')='mcp_prompt')
 AND NOT {PROMPT_PLAN}
BEGIN SELECT RAISE(ABORT,'reference Lead Prompt plan mismatch'); END
"""
IS_LEAD = "json_extract(c.composition_json,'$.orchestration_contract')='lead-delegation.v1'"
# Material can first be requested after the Lead's original question. It must
# precede dispatch, under the exact resolved card and live generation-two lease.
GENERATION = f"""(NEW.created_generation=c.created_generation OR ({IS_LEAD}
 AND NEW.created_generation=2 AND EXISTS (
 SELECT 1 FROM clarification_requests q
 JOIN chat_reference_plans p ON p.turn_id=q.turn_id
 JOIN chat_turn_spawn_dispatch d ON d.turn_id=p.turn_id
 JOIN turn_agent_execution e ON e.id=c.execution_id
 WHERE p.turn_id=c.turn_id AND q.status='resolved' AND q.session_id=d.session_id
 AND json_extract(p.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND json_extract(q.resume_context_json,'$.reference_continuation.contract')='reference-lead-predispatch.v1'
 AND json_extract(q.resume_context_json,'$.reference_continuation.execution_id')=c.execution_id
 AND json_extract(q.resume_context_json,'$.reference_continuation.source_hash')=p.source_hash
 AND json_extract(q.resume_context_json,'$.reference_continuation.composition_digest')=c.composition_digest
 AND e.status='admitted' AND e.started_at IS NULL
 AND d.state='spawned' AND d.owner_id IS NOT NULL AND d.lease_kind='resume'
 AND d.lease_execution_generation=NEW.created_generation AND d.cancel_requested_at IS NULL
 AND julianday(d.lease_expires_at)>julianday('now')
)))"""
SNAPSHOT_DDL = _replace(RESULTS.SNAPSHOT_DDL, "NEW.created_generation=c.created_generation", GENERATION)
# Legacy results keep position two; Lead results use their semantic identity.
# Position two is reserved for the existing delegation guard.
RESOLUTION_DDL = _replace(
    RESULTS.RESOLUTION_DDL,
    "WHEN json_extract(NEW.composition_json,'$.runtime_policy')='initial-primary-mcp-prompt-results.v1'",
    "WHEN json_extract(NEW.composition_json,'$.runtime_policy') IN ('initial-primary-mcp-prompt-results.v1','lead-mcp-prompt-results.v1')\n"
    "  OR NEW.idempotency_key='reference:primary:mcp-prompt-results'",
)
RESOLUTION_DDL = _replace(
    RESOLUTION_DDL,
    "AND json_extract(c.composition_json,'$.runtime_policy')='initial-primary-mcp-prompt.v1'\n  ))",
    "AND json_extract(c.composition_json,'$.runtime_policy')='initial-primary-mcp-prompt.v1'\n"
    "      AND json_extract(c.composition_json,'$.orchestration_contract') IS NULL\n  ))",
)
RESOLUTION_DDL = _replace(
    RESOLUTION_DDL, "NEW.resolution_seq=2 AND NEW.schema_version=1", "(CASE WHEN " + IS_LEAD + " THEN NEW.resolution_seq>2 ELSE NEW.resolution_seq=2 END) AND NEW.schema_version=1",
)
RESOLUTION_DDL = _replace(RESOLUTION_DDL, "NEW.created_generation=c.created_generation", GENERATION)
RESOLUTION_DDL = _replace(
    RESOLUTION_DDL,
    "json_extract(NEW.composition_json,'$.runtime_policy')='initial-primary-mcp-prompt-results.v1'",
    "json_extract(NEW.composition_json,'$.runtime_policy')=CASE WHEN " + IS_LEAD + " THEN 'lead-mcp-prompt-results.v1' ELSE 'initial-primary-mcp-prompt-results.v1' END",
)
AUTO_DDL = _replace(
    AUTO.AUTO_COMPOSITION_DDL,
    "json_extract(NEW.composition_json,'$.runtime_policy') IS NOT 'initial-primary-mcp-prompt-results.v1'",
    "COALESCE(json_extract(NEW.composition_json,'$.runtime_policy') NOT IN ('initial-primary-mcp-prompt-results.v1','lead-mcp-prompt-results.v1'),1)",
)
MODEL_DDL = PAUSE.DDL["trg_reference_model_context_scope"]
MODEL_DDL = _replace(MODEL_DDL, "result.turn_id=r.turn_id AND result.resolution_seq=2", "result.turn_id=r.turn_id")
MODEL_DDL = _replace(
    MODEL_DDL,
    "result.execution_id=r.execution_id AND result.created_generation=r.created_generation\n      AND json_extract(result.composition_json,'$.runtime_policy')='initial-primary-mcp-prompt-results.v1'",
    """result.execution_id=r.execution_id AND (
        (json_extract(r.composition_json,'$.orchestration_contract') IS NULL AND result.resolution_seq=2
         AND result.created_generation=r.created_generation
         AND json_extract(result.composition_json,'$.runtime_policy')='initial-primary-mcp-prompt-results.v1')
        OR (json_extract(r.composition_json,'$.orchestration_contract')='lead-delegation.v1' AND result.resolution_seq>2
         AND result.created_generation BETWEEN 1 AND NEW.created_generation
         AND json_extract(result.composition_json,'$.runtime_policy')='lead-mcp-prompt-results.v1'))""",
)
CHANGES = {
    "trg_reference_lead_plan": (LEAD.PLAN_DDL, PLAN_DDL),
    RESULTS.SNAPSHOT_TRIGGER: (RESULTS.SNAPSHOT_DDL, SNAPSHOT_DDL),
    RESULTS.RESOLUTION_TRIGGER: (RESULTS.RESOLUTION_DDL, RESOLUTION_DDL),
    AUTO.AUTOMATIC.TRIGGER: (AUTO.AUTO_COMPOSITION_DDL, AUTO_DDL),
    "trg_reference_model_context_scope": (PAUSE.DDL["trg_reference_model_context_scope"], MODEL_DDL),
}


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    for name, (old, _) in CHANGES.items():
        row = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or RESULTS._normalized(row[0]) != RESULTS._normalized(old):
            raise RuntimeError(f"reference Lead Prompt schema drift: {name}")
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (GUARD_NAME,)).fetchone():
        raise RuntimeError("reference Lead Prompt schema drift")
    db.exec_driver_sql("SAVEPOINT reference_lead_prompt")
    try:
        for name, (_, replacement) in CHANGES.items():
            db.exec_driver_sql(f"DROP TRIGGER {name}")
            db.exec_driver_sql(replacement)
        db.exec_driver_sql(GUARD)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_lead_prompt")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_lead_prompt")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_lead_prompt")
        raise


def downgrade():
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
