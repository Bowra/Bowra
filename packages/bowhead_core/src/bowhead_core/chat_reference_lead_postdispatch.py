"""Resume a sole, effect-free primary question after the Lead dispatched it."""

from __future__ import annotations

import hashlib
import json

import aiosqlite

from bowhead_core.chat_reference_lead_continuation import CONTEXT_KEY
from bowhead_core.chat_reference_storage import _row
from bowhead_core.clarification_store import ClarificationStore

CONTRACT = "reference-lead-postdispatch.v1"


async def read_pause(db: aiosqlite.Connection, turn_id: str):
    if not db.in_transaction:
        raise ValueError("reference_shared_transaction_required")
    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        rows = await db.execute_fetchall(
            "SELECT c.*,s.id AS original_session_id,s.user_id AS original_user_id,s.workspace_id AS original_workspace_id,"
            "w.team_id AS original_team_id,p.plan_json,p.source_hash,p.created_policy_epoch,source.user_text AS original_user_text,"
            "e.id AS primary_execution_id,e.status AS primary_status,"
            "r.composition_digest,a.composition_digest AS delegation_digest,a.created_generation AS delegation_generation "
            "FROM clarification_requests c JOIN turns t ON t.id=c.turn_id "
            "JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id "
            "JOIN chat_reference_plans p ON p.turn_id=t.id "
            "JOIN chat_reference_plan_retention retention ON retention.turn_id=p.turn_id "
            "JOIN chat_turn_user_sources source ON source.turn_id=t.id "
            "JOIN turn_agent_binding b ON b.turn_id=t.id JOIN turn_agent_execution e ON e.id=b.primary_execution_id "
            "JOIN chat_reference_resolutions r ON r.turn_id=t.id AND r.resolution_seq=1 "
            "JOIN chat_reference_resolutions a ON a.turn_id=t.id AND a.resolution_seq=2 AND a.execution_id=e.id "
            "JOIN chat_reference_evidence v ON v.turn_id=t.id AND v.execution_id=e.id AND v.kind='delegate_started' "
            "AND v.resolution_seq=2 AND v.created_generation=a.created_generation "
            "WHERE c.turn_id=? AND p.retention_state='available' AND retention.available AND retention.deadline>julianday('now') "
            "AND CASE WHEN json_valid(c.resume_context_json) THEN "
            "json_extract(c.resume_context_json,'$.reference_continuation.contract') END=?",
            (turn_id, CONTRACT),
        )
    finally:
        db.row_factory = original_factory
    if len(rows) != 1:
        return None
    from bowhead_core.chat_reference_question_origin import read_question_origin_in_tx

    row = dict(rows[0])
    row["question_origin"] = await read_question_origin_in_tx(db, turn_id=turn_id, postdispatch_card_id=row["request_id"])
    return row


def _context(row) -> dict:
    from bowhead_core.chat_reference_question_origin import ReferenceQuestionOrigin

    try:
        context = json.loads(row["resume_context_json"])
        plan = json.loads(row["plan_json"])
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("reference_continuation_changed") from exc
    marker = context.get(CONTEXT_KEY) if isinstance(context, dict) else None
    sealed = context.get("reference_read_question") if isinstance(context, dict) else None
    origin = dict(row).get("question_origin")
    valid_origin = row["delegation_generation"] == 1 or (
        isinstance(origin, ReferenceQuestionOrigin)
        and (origin.turn_id, origin.execution_id, origin.generation) == (row["turn_id"], row["primary_execution_id"], row["delegation_generation"])
    )
    expected_marker = {
        "contract": CONTRACT,
        "execution_id": row["primary_execution_id"],
        "source_hash": row["source_hash"],
        "composition_digest": row["composition_digest"],
        "delegation_digest": row["delegation_digest"],
    }
    if sealed is not None:
        if not isinstance(sealed, str):
            raise ValueError("reference_continuation_changed")
        expected_marker["read_checkpoint_hash"] = "sha256:" + hashlib.sha256(sealed.encode()).hexdigest()
    if (
        not isinstance(marker, dict)
        or marker != expected_marker
        or not isinstance(plan, dict)
        or plan.get("orchestration_contract") != "lead-delegation.v1"
        or not valid_origin
        or row["primary_status"] not in ("running", "completed", "failed")
        or row["session_id"] != row["original_session_id"]
        or row["user_id"] != row["original_user_id"]
        or row["workspace_id"] != row["original_workspace_id"]
        or context.get("session_id") != row["original_session_id"]
        or context.get("user_id") != row["original_user_id"]
        or context.get("workspace_id") != row["original_workspace_id"]
        or context.get("team_id") != row["original_team_id"]
        or context.get("user_text") != row["original_user_text"]
        or any(type(context.get(key)) is not int or context[key] < 1 for key in ("reference_primary_model_turn_count",))
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
        or not 1 <= len(questions) <= 3
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


async def all_answers(db: aiosqlite.Connection, row) -> str:
    """Retain each original canonical answer without changing historical single cards."""
    from bowhead_core.chat_reference_question_origin import read_question_origin_in_tx

    current = answer(row)
    origin = await read_question_origin_in_tx(db, turn_id=row["turn_id"], postdispatch_card_id=row["request_id"])
    if not origin.answers:
        return current
    encoded = json.dumps([*(item["answer"] for item in origin.answers), json.loads(current)], ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 65536:
        raise ValueError("reference_continuation_changed")
    return encoded


async def open_read_question(db: aiosqlite.Connection, row, *, scope, cipher):
    """Validate the original sealed read history before claiming or consuming it."""
    from bowhead_core.reference_mcp_runtime import require_saved_mcp_reads
    from bowhead_core.reference_read_question import ReferenceReadQuestion

    context = _context(row)
    sealed = context.get("reference_read_question")
    if sealed is None:
        return None
    checkpoint = ReferenceReadQuestion.model_validate_json(
        cipher.open(
            scope=scope,
            purpose="reference_read_question",
            identity=f"{row['turn_id']}/{row['primary_execution_id']}",
            ciphertext=sealed,
        )
    )
    if (
        checkpoint.turn_id != row["turn_id"]
        or checkpoint.execution_id != row["primary_execution_id"]
        or any(getattr(checkpoint, key) != value for key, value in scope.identity().items())
        or checkpoint.next_iteration != context["reference_primary_model_turn_count"]
    ):
        raise ValueError("reference_continuation_changed")
    await require_saved_mcp_reads(db, checkpoint=checkpoint, scope=scope, cipher=cipher)
    return checkpoint


async def pause_ready(db: aiosqlite.Connection, *, turn_id: str, clarification_id: str | None = None) -> bool:
    try:
        row = await read_pause(db, turn_id)
    except ValueError:
        return False
    if row is None or row["primary_status"] != "running" or row["status"] not in ("pending", "resolved"):
        return False
    if clarification_id is not None and (row["request_id"] != clarification_id or row["status"] != "resolved"):
        return False
    try:
        _context(row)
        if row["status"] == "resolved":
            answer(row)
    except ValueError:
        return False
    if await _row(db, "SELECT 1 FROM chat_reference_evidence WHERE turn_id=? AND kind='output_received'", (turn_id,)):
        return False
    if await _row(db, "SELECT 1 FROM chat_reference_lead_postdispatch_attempts WHERE turn_id=?", (turn_id,)):
        return False
    return not await _row(
        db,
        "SELECT 1 FROM chat_reference_model_invocations WHERE turn_id=? AND created_generation>?",
        (turn_id, row["delegation_generation"]),
    )


async def require_resume(db: aiosqlite.Connection, *, turn_id: str, generation: int):
    row = await read_pause(db, turn_id)
    if row is None or row["status"] != "resolved" or generation <= row["delegation_generation"]:
        raise ValueError("reference_continuation_unavailable")
    answer(row)
    output = await _row(
        db,
        "SELECT 1 FROM chat_reference_evidence WHERE turn_id=? AND kind='output_received' AND execution_id=? AND created_generation=?",
        (turn_id, row["primary_execution_id"], generation),
    )
    if (row["primary_status"] != "running") != (output is not None):
        raise ValueError("reference_continuation_changed")
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
    if row["primary_status"] != "running":
        raise ValueError("reference_continuation_already_started")
    if await _row(db, "SELECT 1 FROM chat_reference_lead_postdispatch_attempts WHERE turn_id=?", (turn_id,)):
        raise ValueError("reference_continuation_already_started")
    if await _row(db, "SELECT 1 FROM chat_reference_model_invocations WHERE turn_id=? AND created_generation=?", (turn_id, generation)):
        raise ValueError("reference_continuation_already_started")
