"""A same-Turn restart for a Lead question before its reserved primary starts.

The clarification row is the checkpoint. This contract is only valid while the
primary is still admitted and the first Lead run made no other tool call.
"""

from __future__ import annotations

import json

import aiosqlite

from bowhead_core.chat_reference_storage import _row
from bowhead_core.clarification_store import ClarificationStore

CONTRACT = "reference-lead-predispatch.v1"
CONTEXT_KEY = "reference_continuation"


async def read_pause(db: aiosqlite.Connection, turn_id: str, *, resume_generation: int | None = None):
    if not db.in_transaction:
        raise ValueError("reference_shared_transaction_required")
    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        rows = await db.execute_fetchall(
            "SELECT c.*,s.id AS original_session_id,s.user_id AS original_user_id,s.workspace_id AS original_workspace_id,"
            "w.team_id AS original_team_id,p.plan_json,p.source_hash,p.created_policy_epoch,source.user_text AS original_user_text,"
            "e.id AS primary_execution_id,e.status AS primary_status,"
            "r.composition_digest FROM clarification_requests c "
            "JOIN turns t ON t.id=c.turn_id JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id "
            "JOIN chat_reference_plans p ON p.turn_id=t.id "
            "JOIN chat_reference_plan_retention a ON a.turn_id=p.turn_id "
            "JOIN chat_turn_user_sources source ON source.turn_id=t.id "
            "JOIN turn_agent_binding b ON b.turn_id=t.id "
            "JOIN turn_agent_execution e ON e.id=b.primary_execution_id "
            "JOIN chat_reference_resolutions r ON r.turn_id=t.id AND r.resolution_seq=1 "
            "WHERE c.turn_id=? AND p.retention_state='available' AND a.available AND a.deadline>julianday('now') "
            "AND CASE WHEN json_valid(c.resume_context_json) THEN "
            "json_extract(c.resume_context_json,'$.reference_continuation.contract') END=? ORDER BY c.created_at,c.request_id",
            (turn_id, CONTRACT),
        )
    finally:
        db.row_factory = original_factory
    origin = None
    if rows:
        post = await _row(
            db,
            "SELECT request_id FROM clarification_requests WHERE turn_id=? AND CASE WHEN json_valid(resume_context_json) "
            "THEN json_extract(resume_context_json,'$.reference_continuation.contract') END='reference-lead-postdispatch.v1'",
            (turn_id,),
        )
        if post is not None:
            from bowhead_core.chat_reference_question_origin import read_question_origin_in_tx

            origin = await read_question_origin_in_tx(db, turn_id=turn_id, postdispatch_card_id=post["request_id"])
            if tuple(row["request_id"] for row in rows) != origin.card_ids:
                raise ValueError("reference_continuation_changed")
    if rows and "question_round" in json.loads(rows[-1]["resume_context_json"]):
        from bowhead_core.clarification_rounds import history_contexts, read_history, resume_generation_bounds

        contexts = history_contexts(rows)
        row = rows[-1]
        history = await read_history(
            db, turn_id=turn_id, user_id=row["original_user_id"], workspace_id=row["original_workspace_id"], session_id=row["original_session_id"]
        )
        if origin is None and len(history) != len(rows):
            raise ValueError("reference_continuation_changed")
        if any(row["status"] != "resolved" for row in rows[:-1]):
            raise ValueError("reference_continuation_changed")
        if resume_generation is not None:
            for index, row in enumerate(rows):
                lower, upper = resume_generation_bounds(contexts, index)
                if upper is None and origin is not None:
                    upper = origin.generation
                if lower < resume_generation and (upper is None or resume_generation <= upper):
                    return {**dict(row), "question_origin": origin} if origin is not None else row
            return None
        return {**dict(rows[-1]), "question_origin": origin} if origin is not None else rows[-1]
    return rows[0] if len(rows) == 1 else None


def _context(row) -> dict:
    try:
        context = json.loads(row["resume_context_json"])
        plan = json.loads(row["plan_json"])
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("reference_continuation_changed") from exc
    marker = context.get(CONTEXT_KEY) if isinstance(context, dict) else None
    if (
        not isinstance(marker, dict)
        or marker
        != {
            "contract": CONTRACT,
            "execution_id": row["primary_execution_id"],
            "source_hash": row["source_hash"],
            "composition_digest": row["composition_digest"],
        }
        or not isinstance(plan, dict)
        or plan.get("orchestration_contract") != "lead-delegation.v1"
        or row["session_id"] != row["original_session_id"]
        or row["user_id"] != row["original_user_id"]
        or row["workspace_id"] != row["original_workspace_id"]
        or context.get("session_id") != row["original_session_id"]
        or context.get("user_id") != row["original_user_id"]
        or context.get("workspace_id") != row["original_workspace_id"]
        or context.get("team_id") != row["original_team_id"]
        or context.get("user_text") != row["original_user_text"]
        or row["kind"] != "ambiguous_turn"
        or row["token"]
        or row["tool"]
    ):
        raise ValueError("reference_continuation_changed")
    return context


def answer(row) -> str:
    context = _context(row)
    if row["status"] != "resolved":
        raise ValueError("reference_continuation_unavailable")
    questions = context.get("reference_questions", [])
    fallback = context.get("reference_fallback")
    if (
        not isinstance(questions, list)
        or len(questions) > 5
        or any(not isinstance(item, dict) for item in questions)
        or fallback is not None
        and (not isinstance(fallback, str) or len(fallback) > 200)
    ):
        raise ValueError("reference_continuation_changed")
    request = ClarificationStore._row_to_request(row)
    material = {
        "questions_asked": questions,
        "fallback_question": fallback or row["rationale"],
        "options_shown": [{"value": candidate.value, "label": candidate.label} for candidate in request.candidates],
        "chosen_value": request.chosen_value,
        "chosen_values": request.chosen_values,
        "answers": request.answers,
        "user_directive": request.user_directive,
    }
    if not request.chosen_value and not request.chosen_values and not request.answers and not request.user_directive:
        raise ValueError("reference_continuation_changed")
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > 65536:
        raise ValueError("reference_continuation_changed")
    return encoded


async def all_answers(db, row) -> str:
    """Carry every answered card into the final original-task delegation."""
    from bowhead_core.chat_reference_question_origin import ReferenceQuestionOrigin

    origin = dict(row).get("question_origin")
    if isinstance(origin, ReferenceQuestionOrigin):
        _context(row)
        return json.dumps([item["answer"] for item in origin.answers], ensure_ascii=False, separators=(",", ":"))
    if "question_round" not in _context(row):
        return answer(row)
    from bowhead_core.clarification_rounds import history_contexts, read_history

    rows = await read_history(
        db, turn_id=row["turn_id"], user_id=row["original_user_id"], workspace_id=row["original_workspace_id"], session_id=row["original_session_id"]
    )
    history_contexts(rows)
    encoded = json.dumps([json.loads(answer({**dict(row), **dict(card)})) for card in rows], ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 65536:
        raise ValueError("reference_continuation_changed")
    return encoded


async def pause_ready(db: aiosqlite.Connection, *, turn_id: str, clarification_id: str | None = None) -> bool:
    if await _row(db, "SELECT 1 FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=2", (turn_id,)):
        return False
    row = await read_pause(db, turn_id)
    if row is None or row["primary_status"] != "admitted" or row["status"] not in ("pending", "resolved"):
        return False
    if clarification_id is not None and (row["request_id"] != clarification_id or row["status"] != "resolved"):
        return False
    try:
        _context(row)
        if row["status"] == "resolved":
            answer(row)
    except ValueError:
        return False
    if await _row(db, "SELECT 1 FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=2", (turn_id,)):
        return False
    return not await _row(db, "SELECT 1 FROM chat_reference_model_invocations WHERE turn_id=?", (turn_id,))


async def require_resume(db: aiosqlite.Connection, *, turn_id: str, generation: int):
    # A newly staged next card does not replace the answered card that owns
    # this running generation. Recovery of the pending card uses read_pause.
    row = await read_pause(db, turn_id, resume_generation=generation)
    if generation <= 1 or row is None or row["status"] != "resolved" or row["primary_status"] not in ("admitted", "running", "completed"):
        raise ValueError("reference_continuation_unavailable")
    _context(row)
    answer(row)
    if row["primary_status"] == "admitted" and not await pause_ready(db, turn_id=turn_id):
        raise ValueError("reference_continuation_unavailable")
    dispatch = await _row(
        db,
        "SELECT 1 FROM chat_turn_spawn_dispatch d JOIN turns t ON t.id=d.turn_id "
        "WHERE d.turn_id=? AND d.lease_kind='resume' AND d.lease_execution_generation=? "
        "AND t.execution_generation=? AND t.status='running'",
        (turn_id, generation, generation),
    )
    if dispatch is None:
        raise ValueError("reference_continuation_unavailable")
    return row


async def release_resume(db: aiosqlite.Connection, *, turn_id: str, generation: int) -> None:
    row = await require_resume(db, turn_id=turn_id, generation=generation)
    if row["primary_status"] != "admitted" or not await pause_ready(db, turn_id=turn_id):
        raise ValueError("reference_continuation_already_started")
