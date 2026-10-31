"""Resume an original primary first dispatched after a consumed Lead question."""

import re
from importlib import import_module

from alembic import context

revision = "0209_reference_question_origin"
down_revision = "0208_document_reference_completion"
branch_labels = None
depends_on = None

PAUSE = import_module("bowhead_core.migrations.versions.0112_reference_lead_postdispatch")
MCP = import_module("bowhead_core.migrations.versions.0128_reference_lead_mcp")
MODEL = import_module("bowhead_core.migrations.versions.0133_reference_plan_steps")
COMPLETION = import_module("bowhead_core.migrations.versions.0208_document_reference_completion")
PROMPT = import_module("bowhead_core.migrations.versions.0129_reference_lead_prompt")

TABLE = "chat_reference_lead_postdispatch_attempts"
SHADOW = TABLE + "_0209"
ATTEMPT_NAME = "trg_reference_lead_postdispatch_attempt"
QUESTION_NAME = "trg_clarification_round_identity"


def _lead_card(card: str) -> str:
    return f"""{card}.turn_id=plan.turn_id AND {card}.session_id=session.id
 AND {card}.user_id=session.user_id AND {card}.workspace_id=session.workspace_id
 AND {card}.status='resolved' AND {card}.kind='ambiguous_turn' AND {card}.token='' AND COALESCE({card}.tool,'')=''
 AND json_extract({card}.resume_context_json,'$.reference_continuation.contract')='reference-lead-predispatch.v1'
 AND json_extract({card}.resume_context_json,'$.reference_continuation.execution_id')=actual.execution_id
 AND json_extract({card}.resume_context_json,'$.reference_continuation.source_hash')=plan.source_hash
 AND json_extract({card}.resume_context_json,'$.reference_continuation.composition_digest')=original.composition_digest
 AND json_extract({card}.resume_context_json,'$.session_id')=session.id
 AND json_extract({card}.resume_context_json,'$.user_id')=session.user_id
 AND json_extract({card}.resume_context_json,'$.workspace_id')=session.workspace_id
 AND json_extract({card}.resume_context_json,'$.team_id')=space.team_id
 AND json_extract({card}.resume_context_json,'$.user_text')=source.user_text
 AND json_extract({card}.resume_context_json,'$.question_round.contract')='effect-free-questions.v1'"""


def _consumed(card: str, upper: str) -> str:
    return f"""(SELECT MAX(json_extract(notice.payload_json,'$.execution_generation')) FROM turn_events notice
 WHERE notice.turn_id=plan.turn_id AND notice.event_type='turn.resumed' AND json_valid(notice.payload_json)
 AND json_type(notice.payload_json,'$.execution_generation')='integer'
 AND json_extract(notice.payload_json,'$.request_id')={card}.request_id
 AND json_extract(notice.payload_json,'$.execution_generation')>json_extract({card}.resume_context_json,'$.question_round.generation')
 AND json_extract(notice.payload_json,'$.execution_generation')<={upper})={upper}"""


def _later_origin(*, active_session: bool = True) -> str:
    """Use the original bounded Lead prefix, never a resolved card alone.

    The shared budget has at most three rounds. A postdispatch card therefore
    has one or two preceding Lead cards. The original request-bound resume
    notices identify which worker consumed each answer, including a legitimate
    pre-worker retry that skipped an unused generation.
    """
    first_generation = "json_extract(first.resume_context_json,'$.question_round.generation')"
    lead_generation = "json_extract(lead.resume_context_json,'$.question_round.generation')"
    sql = f"""EXISTS (
 SELECT 1 FROM sessions session JOIN workspaces space ON space.id=session.workspace_id
 JOIN chat_turn_user_sources source ON source.turn_id=plan.turn_id
 JOIN clarification_requests lead ON lead.request_id=json_extract(card.resume_context_json,'$.question_round.prior_request_id')
 WHERE session.id=card.session_id AND session.id=dispatch.session_id AND session.active_turn_id=plan.turn_id
 AND session.deleted_at IS NULL AND card.user_id=session.user_id AND card.workspace_id=session.workspace_id
 AND actual.created_generation>1 AND started.created_generation=actual.created_generation
 AND started.content_hash=json_extract(actual.composition_json,'$.task_hash')
 AND json_extract(card.resume_context_json,'$.question_round.contract')='effect-free-questions.v1'
 AND json_type(card.resume_context_json,'$.question_round.round')='integer'
 AND json_extract(card.resume_context_json,'$.question_round.round') IN (2,3)
 AND json_type(card.resume_context_json,'$.question_round.generation')='integer'
 AND json_extract(card.resume_context_json,'$.question_round.generation')=actual.created_generation
 AND json_extract(card.resume_context_json,'$.session_id')=session.id
 AND json_extract(card.resume_context_json,'$.user_id')=session.user_id
 AND json_extract(card.resume_context_json,'$.workspace_id')=session.workspace_id
 AND json_extract(card.resume_context_json,'$.team_id')=space.team_id
 AND json_extract(card.resume_context_json,'$.user_text')=source.user_text
 AND {_lead_card("lead")}
 AND json_type(lead.resume_context_json,'$.question_round.round')='integer'
 AND json_type(lead.resume_context_json,'$.question_round.generation')='integer'
 AND json_extract(lead.resume_context_json,'$.question_round.round')=json_extract(card.resume_context_json,'$.question_round.round')-1
 AND {lead_generation}<actual.created_generation
 AND {_consumed("lead", "actual.created_generation")}
 AND (SELECT COUNT(*) FROM clarification_requests prior WHERE prior.turn_id=plan.turn_id
      AND (prior.created_at<card.created_at OR prior.created_at=card.created_at AND prior.request_id<card.request_id))
     =json_extract(card.resume_context_json,'$.question_round.round')-1
 AND ((json_extract(lead.resume_context_json,'$.question_round.round')=1 AND {lead_generation}=1
       AND json_type(lead.resume_context_json,'$.question_round.prior_request_id')='null')
  OR (json_extract(lead.resume_context_json,'$.question_round.round')=2 AND EXISTS (
   SELECT 1 FROM clarification_requests first
   WHERE first.request_id=json_extract(lead.resume_context_json,'$.question_round.prior_request_id')
   AND {_lead_card("first")}
   AND json_type(first.resume_context_json,'$.question_round.round')='integer'
   AND json_type(first.resume_context_json,'$.question_round.generation')='integer'
   AND json_extract(first.resume_context_json,'$.question_round.round')=1 AND {first_generation}=1
   AND json_type(first.resume_context_json,'$.question_round.prior_request_id')='null'
   AND {first_generation}<{lead_generation}
   AND (first.created_at<lead.created_at OR first.created_at=lead.created_at AND first.request_id<lead.request_id)
   AND {_consumed("first", lead_generation)}
  )))
)"""
    if not active_session:
        sql = sql.replace(" AND session.active_turn_id=plan.turn_id", "").replace(" AND session.deleted_at IS NULL", "")
    return sql


def late_origin_metadata_sql(*, plan: str, original: str, actual: str, card: str, dispatch: str) -> str:
    """Classify an original late-origin pause without granting execution.

    Callers retain their original recovery-dispatch and retention rules. A stale
    lease, inactive Session or Session deletion must not hide the accepted
    contract from recovery; its execution reader still checks current authority.
    All aliases are fixed server SQL identifiers, never browser/model input.
    """
    sql = f"""EXISTS (SELECT 1 FROM chat_reference_evidence started
 WHERE started.turn_id=plan.turn_id AND started.execution_id=actual.execution_id
 AND started.resolution_seq=2 AND started.kind='delegate_started' AND {_later_origin(active_session=False)})"""
    for source, target in (("plan", plan), ("original", original), ("actual", actual), ("card", card), ("dispatch", dispatch)):
        sql = re.sub(r"\b" + source + r"\.", target + ".", sql)
    return sql


def _late_postdispatch_fence(generation: str, subagent: str) -> str:
    old = PAUSE._postdispatch_fence(generation, subagent)
    new = old.replace("actual.created_generation=1", "actual.created_generation>1", 1).replace(
        f"started.created_generation=1\n AND started.subagent_id={subagent} AND {generation}=2",
        f"started.created_generation=actual.created_generation\n AND started.subagent_id={subagent} AND {generation}>actual.created_generation",
        1,
    )
    if new == old or "started.created_generation=1" in new:
        raise RuntimeError("Reference question origin source drift")
    return new.replace(" AND card.status='resolved'", f" AND {_later_origin()}\n AND card.status='resolved'", 1)


CURRENT = {
    "trg_reference_model_context_scope": MODEL.CONTEXT_DDL,
    "trg_chat_reference_evidence_scope": PAUSE.DDL["trg_chat_reference_evidence_scope"],
    "trg_reference_primary_completion": COMPLETION.DDL,
    "trg_reference_mcp_call_scope": MCP.CALL_SCOPE,
}
GENERATIONS = {
    "trg_reference_model_context_scope": ("NEW.created_generation", "NEW.subagent_id"),
    "trg_chat_reference_evidence_scope": ("NEW.created_generation", "NEW.subagent_id"),
    "trg_reference_primary_completion": ("NEW.execution_generation", "o.subagent_id"),
    "trg_reference_mcp_call_scope": ("NEW.created_generation", "started.subagent_id"),
}
DDL = {}
for _name, (_generation, _subagent) in GENERATIONS.items():
    _old = PAUSE._postdispatch_fence(_generation, _subagent)
    if CURRENT[_name].count(_old) != 1:
        raise RuntimeError("Reference question origin source drift")
    DDL[_name] = CURRENT[_name].replace(_old, f"({_old} OR {_late_postdispatch_fence(_generation, _subagent)})", 1)
ATTEMPT_READERS = frozenset(CURRENT)


def _prompt_first_generation() -> str:
    """Prove the original consumed Lead history before any primary dispatch."""
    lead_card = _lead_card("question").replace("actual.execution_id", "c.execution_id").replace("original.composition_digest", "c.composition_digest")
    next_generation = """COALESCE((SELECT json_extract(next.resume_context_json,'$.question_round.generation')
 FROM clarification_requests next WHERE next.turn_id=plan.turn_id
 AND json_extract(next.resume_context_json,'$.question_round.round')=json_extract(question.resume_context_json,'$.question_round.round')+1),NEW.created_generation)"""
    return f"""EXISTS (
 SELECT 1 FROM chat_reference_plans plan
 JOIN turns turn ON turn.id=plan.turn_id JOIN sessions session ON session.id=turn.session_id
 JOIN workspaces space ON space.id=session.workspace_id JOIN chat_turn_user_sources source ON source.turn_id=turn.id
 JOIN chat_reference_plan_retention retained ON retained.turn_id=plan.turn_id
 JOIN turn_agent_execution execution ON execution.id=c.execution_id
 JOIN chat_turn_spawn_dispatch dispatch ON dispatch.turn_id=turn.id AND dispatch.session_id=session.id
 JOIN clarification_requests card ON card.turn_id=turn.id
 WHERE plan.turn_id=c.turn_id AND json_extract(plan.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND plan.retention_state='available' AND retained.available AND retained.deadline>julianday('now')
 AND execution.status='admitted' AND execution.started_at IS NULL
 AND turn.status='running' AND turn.execution_generation=NEW.created_generation
 AND session.active_turn_id=turn.id AND session.deleted_at IS NULL
 AND dispatch.state='spawned' AND dispatch.owner_id IS NOT NULL AND dispatch.lease_kind='resume'
 AND dispatch.lease_execution_generation=NEW.created_generation AND dispatch.cancel_requested_at IS NULL
 AND julianday(dispatch.lease_expires_at)>julianday('now')
 AND NOT EXISTS (SELECT 1 FROM chat_reference_resolutions actual WHERE actual.turn_id=turn.id AND actual.resolution_seq=2)
 AND NOT EXISTS (SELECT 1 FROM chat_reference_model_invocations invocation WHERE invocation.turn_id=turn.id)
 AND json_type(card.resume_context_json,'$.question_round.round')='integer'
 AND json_extract(card.resume_context_json,'$.question_round.round') BETWEEN 1 AND 3
 AND (SELECT COUNT(*) FROM clarification_requests total WHERE total.turn_id=turn.id)=json_extract(card.resume_context_json,'$.question_round.round')
 AND NOT EXISTS (SELECT 1 FROM clarification_requests question WHERE question.turn_id=turn.id AND NOT COALESCE((
  {lead_card}
  AND json_type(question.resume_context_json,'$.question_round.round')='integer'
  AND json_type(question.resume_context_json,'$.question_round.generation')='integer'
  AND json_extract(question.resume_context_json,'$.question_round.round') BETWEEN 1 AND json_extract(card.resume_context_json,'$.question_round.round')
  AND json_extract(question.resume_context_json,'$.question_round.generation')<NEW.created_generation
  AND (SELECT COUNT(*) FROM clarification_requests same_round WHERE same_round.turn_id=turn.id
       AND json_extract(same_round.resume_context_json,'$.question_round.round')=json_extract(question.resume_context_json,'$.question_round.round'))=1
  AND ((json_extract(question.resume_context_json,'$.question_round.round')=1
        AND json_extract(question.resume_context_json,'$.question_round.generation')=1
        AND json_type(question.resume_context_json,'$.question_round.prior_request_id')='null')
   OR EXISTS (SELECT 1 FROM clarification_requests previous
    WHERE previous.turn_id=turn.id AND previous.request_id=json_extract(question.resume_context_json,'$.question_round.prior_request_id')
    AND json_extract(previous.resume_context_json,'$.question_round.round')=json_extract(question.resume_context_json,'$.question_round.round')-1
    AND json_extract(previous.resume_context_json,'$.question_round.generation')<json_extract(question.resume_context_json,'$.question_round.generation')
    AND (previous.created_at<question.created_at OR previous.created_at=question.created_at AND previous.request_id<question.request_id)))
  AND {_consumed("question", next_generation)}
 ),0))
)"""


PROMPT_CURRENT = {
    PROMPT.RESULTS.SNAPSHOT_TRIGGER: PROMPT.SNAPSHOT_DDL,
    PROMPT.RESULTS.RESOLUTION_TRIGGER: PROMPT.RESOLUTION_DDL,
}
PROMPT_GENERATION = f"({PROMPT.GENERATION} OR ({PROMPT.IS_LEAD} AND NEW.created_generation>2 AND {_prompt_first_generation()}))"
for _name, _sql in PROMPT_CURRENT.items():
    if _sql.count(PROMPT.GENERATION) != 1:
        raise RuntimeError("Reference Prompt question origin source drift")
    CURRENT[_name] = _sql
    DDL[_name] = _sql.replace(PROMPT.GENERATION, PROMPT_GENERATION, 1)

ATTEMPT_TABLE = PAUSE.ATTEMPT_TABLE.replace(f"CREATE TABLE {TABLE}", f"CREATE TABLE {SHADOW}", 1).replace(
    "CHECK(generation=2)", "CHECK(generation>=2)", 1
)
ATTEMPT_GUARD = f"""
CREATE TRIGGER {ATTEMPT_NAME} BEFORE INSERT ON {TABLE}
WHEN NOT EXISTS (
 SELECT 1 FROM turns turn JOIN chat_reference_plans plan ON plan.turn_id=turn.id
 JOIN chat_reference_resolutions original ON original.turn_id=turn.id AND original.resolution_seq=1
 JOIN chat_reference_resolutions actual ON actual.turn_id=turn.id AND actual.resolution_seq=2
 JOIN turn_agent_execution execution ON execution.id=actual.execution_id
 JOIN clarification_requests card ON card.request_id=NEW.clarification_id AND card.turn_id=turn.id
 JOIN chat_turn_spawn_dispatch dispatch ON dispatch.turn_id=turn.id AND dispatch.session_id=turn.session_id
 WHERE turn.id=NEW.turn_id AND turn.status='running' AND NEW.generation=turn.execution_generation
 AND json_extract(plan.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND execution.id=NEW.execution_id AND execution.status='running' AND actual.execution_id=original.execution_id
 AND card.status='resolved' AND card.session_id=dispatch.session_id
 AND json_extract(card.resume_context_json,'$.reference_continuation.contract')='reference-lead-postdispatch.v1'
 AND json_extract(card.resume_context_json,'$.reference_continuation.execution_id')=execution.id
 AND json_extract(card.resume_context_json,'$.reference_continuation.delegation_digest')=actual.composition_digest
 AND dispatch.state='spawned' AND dispatch.lease_kind='resume' AND dispatch.lease_execution_generation=NEW.generation
 AND dispatch.owner_id=NEW.owner_id AND dispatch.cancel_requested_at IS NULL AND julianday(dispatch.lease_expires_at)>julianday('now')
 AND ((actual.created_generation=1 AND NEW.generation=2) OR (
  NEW.generation>actual.created_generation
  AND json_extract(actual.composition_json,'$.runtime_policy')='lead-delegation.v1'
  AND json_extract(card.resume_context_json,'$.reference_continuation.source_hash')=plan.source_hash
  AND json_extract(card.resume_context_json,'$.reference_continuation.composition_digest')=original.composition_digest
  AND EXISTS (SELECT 1 FROM chat_reference_evidence started
   WHERE started.turn_id=turn.id AND started.execution_id=actual.execution_id
   AND started.resolution_seq=2 AND started.kind='delegate_started' AND {_later_origin()})
 ))
)
BEGIN SELECT RAISE(ABORT,'reference Lead postdispatch attempt mismatch'); END
"""

QUESTION_CURRENT = """
CREATE TRIGGER trg_clarification_round_identity BEFORE UPDATE ON clarification_requests
WHEN json_valid(OLD.resume_context_json) AND json_type(OLD.resume_context_json,'$.question_round') IS NOT NULL
AND (NEW.request_id IS NOT OLD.request_id OR NEW.created_at IS NOT OLD.created_at
 OR NEW.kind IS NOT OLD.kind OR NEW.tool IS NOT OLD.tool OR NEW.token IS NOT OLD.token
 OR NEW.turn_id IS NOT OLD.turn_id OR NEW.user_id IS NOT OLD.user_id
 OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.session_id IS NOT OLD.session_id
 OR NEW.resume_context_json IS NOT OLD.resume_context_json)
BEGIN SELECT RAISE(ABORT,'clarification round identity is immutable'); END
"""
RETIREMENT = f"""(
 OLD.status='resolved' AND NEW.status='resolved' AND json_valid(NEW.resume_context_json)
 AND json_extract(OLD.resume_context_json,'$.reference_continuation.contract')='reference-lead-postdispatch.v1'
 AND json_type(OLD.resume_context_json,'$.reference_read_question')='text'
 AND json_type(OLD.resume_context_json,'$.reference_continuation.read_checkpoint_hash')='text'
 AND NEW.resume_context_json=json_remove(OLD.resume_context_json,'$.reference_read_question','$.reference_continuation.read_checkpoint_hash')
 AND NEW.rationale IS OLD.rationale AND NEW.candidates_json IS OLD.candidates_json AND NEW.multi_select IS OLD.multi_select
 AND NEW.chosen_value IS OLD.chosen_value AND NEW.chosen_values_json IS OLD.chosen_values_json
 AND NEW.answers_json IS OLD.answers_json AND NEW.chosen_at IS OLD.chosen_at AND NEW.user_directive IS OLD.user_directive
 AND NEW.edited_subs_json IS OLD.edited_subs_json AND NEW.injected_at IS OLD.injected_at
 AND NEW.continuation_generation IS OLD.continuation_generation AND NEW.continuation_owner_id IS OLD.continuation_owner_id
 AND NEW.dismissed_at IS OLD.dismissed_at AND NEW.directive_cancelled_at IS OLD.directive_cancelled_at
 AND EXISTS (SELECT 1 FROM {TABLE} attempt
  JOIN turns turn ON turn.id=attempt.turn_id JOIN sessions session ON session.id=turn.session_id
  JOIN chat_turn_spawn_dispatch dispatch ON dispatch.turn_id=turn.id AND dispatch.session_id=session.id
  WHERE attempt.turn_id=OLD.turn_id AND attempt.clarification_id=OLD.request_id
  AND attempt.execution_id=json_extract(OLD.resume_context_json,'$.reference_continuation.execution_id')
  AND turn.status='running' AND turn.execution_generation=attempt.generation
  AND session.id=OLD.session_id AND session.user_id=OLD.user_id AND session.workspace_id=OLD.workspace_id
  AND session.active_turn_id=turn.id AND session.deleted_at IS NULL
  AND dispatch.state='spawned' AND dispatch.lease_kind='resume' AND dispatch.lease_execution_generation=attempt.generation
  AND dispatch.owner_id=attempt.owner_id AND dispatch.cancel_requested_at IS NULL
  AND julianday(dispatch.lease_expires_at)>julianday('now'))
)"""
QUESTION_DDL = QUESTION_CURRENT.replace(
    "OR NEW.resume_context_json IS NOT OLD.resume_context_json)",
    f"OR (NEW.resume_context_json IS NOT OLD.resume_context_json AND NOT COALESCE({RETIREMENT},0)))",
    1,
)


def _normalized(sql):
    return " ".join(sql.split()).rstrip(";")


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    expected = {TABLE: PAUSE.ATTEMPT_TABLE, ATTEMPT_NAME: PAUSE.ATTEMPT_GUARD, QUESTION_NAME: QUESTION_CURRENT, **CURRENT}
    for name, sql in expected.items():
        row = db.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE name=?", (name,)).fetchone()
        if row is None or _normalized(row[0]) != _normalized(sql):
            raise RuntimeError(f"Reference question origin schema drift: {name}")
    dependents = {
        row[0] for row in db.exec_driver_sql("SELECT name FROM sqlite_schema WHERE sql IS NOT NULL AND instr(sql,?)>0", (TABLE,)).fetchall()
    }
    if dependents != {TABLE, ATTEMPT_NAME, *ATTEMPT_READERS} or db.exec_driver_sql("SELECT 1 FROM sqlite_schema WHERE name=?", (SHADOW,)).fetchone():
        raise RuntimeError("Reference question origin dependency drift")
    db.exec_driver_sql("SAVEPOINT reference_question_origin")
    try:
        for name in (*CURRENT, ATTEMPT_NAME, QUESTION_NAME):
            db.exec_driver_sql(f"DROP TRIGGER {name}")
        db.exec_driver_sql(ATTEMPT_TABLE)
        db.exec_driver_sql(f"INSERT INTO {SHADOW} SELECT * FROM {TABLE}")
        if (
            db.exec_driver_sql(f"SELECT COUNT(*) FROM {SHADOW}").scalar() != db.exec_driver_sql(f"SELECT COUNT(*) FROM {TABLE}").scalar()
            or db.exec_driver_sql(f"SELECT 1 FROM (SELECT * FROM {SHADOW} EXCEPT SELECT * FROM {TABLE}) LIMIT 1").fetchone()
            or db.exec_driver_sql(f"SELECT 1 FROM (SELECT * FROM {TABLE} EXCEPT SELECT * FROM {SHADOW}) LIMIT 1").fetchone()
        ):
            raise RuntimeError("Reference question origin copy mismatch")
        db.exec_driver_sql(f"DROP TABLE {TABLE}")
        db.exec_driver_sql(f"ALTER TABLE {SHADOW} RENAME TO {TABLE}")
        for sql in (*DDL.values(), ATTEMPT_GUARD, QUESTION_DDL):
            db.exec_driver_sql(sql)
        if db.exec_driver_sql("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Reference question origin foreign key mismatch")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_question_origin")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_question_origin")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_question_origin")
        raise


def downgrade():
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
