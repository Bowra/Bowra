"""Allow one exact, effect-free Lead primary to resume after its question."""

from importlib import import_module

from alembic import context

revision = "0112_reference_lead_postdispatch"
down_revision = "0111_reference_lead_delegation"
branch_labels = None
depends_on = None

PREVIOUS = import_module("bowhead_core.migrations.versions.0111_reference_lead_delegation")
CURRENT = {
    "trg_reference_model_context_scope": PREVIOUS.EXTRA_DDL["trg_reference_model_context_scope"],
    "trg_chat_reference_evidence_scope": PREVIOUS.PREVIOUS.DDL["trg_chat_reference_evidence_scope"],
    "trg_reference_primary_completion": PREVIOUS.EXTRA_DDL["trg_reference_primary_completion"],
}


def _postdispatch_fence(generation: str, subagent: str) -> str:
    return f"""EXISTS (
 SELECT 1 FROM chat_reference_plans plan
 JOIN chat_reference_resolutions original ON original.turn_id=plan.turn_id AND original.resolution_seq=1
 JOIN chat_reference_resolutions actual ON actual.turn_id=plan.turn_id AND actual.resolution_seq=2
 JOIN chat_reference_evidence started ON started.turn_id=plan.turn_id AND started.execution_id=actual.execution_id
 JOIN clarification_requests card ON card.turn_id=plan.turn_id
 JOIN chat_turn_spawn_dispatch dispatch ON dispatch.turn_id=plan.turn_id
 JOIN chat_reference_lead_postdispatch_attempts attempt ON attempt.turn_id=plan.turn_id
 WHERE plan.turn_id=r.turn_id AND json_extract(plan.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND r.execution_id=actual.execution_id AND r.resolution_seq IN (1,2)
 AND r.composition_digest=CASE WHEN r.resolution_seq=1 THEN original.composition_digest ELSE actual.composition_digest END
 AND actual.created_generation=1 AND json_extract(actual.composition_json,'$.runtime_policy')='lead-delegation.v1'
 AND started.resolution_seq=2 AND started.kind='delegate_started' AND started.created_generation=1
 AND started.subagent_id={subagent} AND {generation}=2
 AND card.status='resolved' AND card.session_id=dispatch.session_id
 AND attempt.clarification_id=card.request_id AND attempt.execution_id=actual.execution_id
 AND attempt.generation={generation} AND attempt.owner_id=dispatch.owner_id
 AND json_extract(card.resume_context_json,'$.reference_continuation.contract')='reference-lead-postdispatch.v1'
 AND json_extract(card.resume_context_json,'$.reference_continuation.execution_id')=actual.execution_id
 AND json_extract(card.resume_context_json,'$.reference_continuation.source_hash')=plan.source_hash
 AND json_extract(card.resume_context_json,'$.reference_continuation.composition_digest')=original.composition_digest
 AND json_extract(card.resume_context_json,'$.reference_continuation.delegation_digest')=actual.composition_digest
 AND dispatch.state='spawned' AND dispatch.owner_id IS NOT NULL AND dispatch.lease_kind='resume'
 AND dispatch.lease_execution_generation={generation} AND dispatch.cancel_requested_at IS NULL
 AND julianday(dispatch.lease_expires_at)>julianday('now')
)"""


MODEL_NEEDLE = "r.created_generation=NEW.created_generation OR"
EVIDENCE_NEEDLE = "r.created_generation=NEW.created_generation OR"
COMPLETION_NEEDLE = "r.created_generation=NEW.execution_generation OR"
if any(CURRENT[name].count(needle) != 1 for name, needle in (
    ("trg_reference_model_context_scope", MODEL_NEEDLE),
    ("trg_chat_reference_evidence_scope", EVIDENCE_NEEDLE),
    ("trg_reference_primary_completion", COMPLETION_NEEDLE),
)):
    raise RuntimeError("reference Lead postdispatch source drift")

DDL = {
    "trg_reference_model_context_scope": CURRENT["trg_reference_model_context_scope"].replace(
        MODEL_NEEDLE, f"{MODEL_NEEDLE} {_postdispatch_fence('NEW.created_generation', 'NEW.subagent_id')} OR"
    ),
    "trg_chat_reference_evidence_scope": CURRENT["trg_chat_reference_evidence_scope"].replace(
        EVIDENCE_NEEDLE,
        f"{EVIDENCE_NEEDLE} (NEW.kind='output_received' AND {_postdispatch_fence('NEW.created_generation', 'NEW.subagent_id')}) OR",
    ),
    "trg_reference_primary_completion": CURRENT["trg_reference_primary_completion"].replace(
        COMPLETION_NEEDLE,
        f"{COMPLETION_NEEDLE} {_postdispatch_fence('NEW.execution_generation', 'o.subagent_id')} OR",
    ),
}
ATTEMPT_TABLE = """
CREATE TABLE chat_reference_lead_postdispatch_attempts (
 turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
 clarification_id TEXT NOT NULL REFERENCES clarification_requests(request_id),
 execution_id TEXT NOT NULL REFERENCES turn_agent_execution(id),
 generation INTEGER NOT NULL CHECK(generation=2),
 owner_id TEXT NOT NULL,
 started_at TEXT NOT NULL
)
"""
ATTEMPT_GUARD = """
CREATE TRIGGER trg_reference_lead_postdispatch_attempt BEFORE INSERT ON chat_reference_lead_postdispatch_attempts
WHEN NOT EXISTS (
 SELECT 1 FROM turns t JOIN chat_reference_plans p ON p.turn_id=t.id
 JOIN chat_reference_resolutions actual ON actual.turn_id=t.id AND actual.resolution_seq=2
 JOIN turn_agent_execution e ON e.id=actual.execution_id
 JOIN clarification_requests q ON q.turn_id=t.id
 JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=t.session_id
 WHERE t.id=NEW.turn_id AND t.status='running' AND t.execution_generation=2
 AND json_extract(p.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND actual.created_generation=1 AND e.id=NEW.execution_id AND e.status='running'
 AND q.request_id=NEW.clarification_id AND q.status='resolved'
 AND json_extract(q.resume_context_json,'$.reference_continuation.contract')='reference-lead-postdispatch.v1'
 AND json_extract(q.resume_context_json,'$.reference_continuation.execution_id')=e.id
 AND json_extract(q.resume_context_json,'$.reference_continuation.delegation_digest')=actual.composition_digest
 AND d.state='spawned' AND d.lease_kind='resume' AND d.lease_execution_generation=2
 AND d.owner_id=NEW.owner_id AND d.cancel_requested_at IS NULL AND julianday(d.lease_expires_at)>julianday('now')
)
BEGIN SELECT RAISE(ABORT,'reference Lead postdispatch attempt mismatch'); END
"""


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    normalize = PREVIOUS.PREVIOUS.FOLLOW_UP._normalized
    for name, sql in CURRENT.items():
        row = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        if row is None or normalize(row[0]) != normalize(sql):
            raise RuntimeError("reference Lead postdispatch schema drift")
    if any(
        db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
        for name in ("chat_reference_lead_postdispatch_attempts", "trg_reference_lead_postdispatch_attempt")
    ):
        raise RuntimeError("reference Lead postdispatch schema drift")
    db.exec_driver_sql("SAVEPOINT reference_lead_postdispatch")
    try:
        db.exec_driver_sql(ATTEMPT_TABLE)
        db.exec_driver_sql(ATTEMPT_GUARD)
        for name, sql in DDL.items():
            db.exec_driver_sql(f"DROP TRIGGER {name}")
            db.exec_driver_sql(sql)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_lead_postdispatch")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_lead_postdispatch")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_lead_postdispatch")
        raise


def downgrade():
    raise NotImplementedError("Forward-only; restore the matching database snapshot to roll back.")
