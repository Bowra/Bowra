"""Read the exact first primary generation and its consumed Lead answers."""

from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

from bowhead_core.chat_reference_lead_continuation import CONTRACT, _context, answer
from bowhead_core.chat_reference_storage import _row
from bowhead_core.clarification_rounds import history_contexts, read_history, resume_generation_bounds


@dataclass(frozen=True, slots=True)
class ReferenceQuestionOrigin:
    turn_id: str
    execution_id: str
    generation: int
    card_ids: tuple[str, ...]
    answers: tuple[dict, ...]


async def read_question_origin_in_tx(db: aiosqlite.Connection, *, turn_id: str, postdispatch_card_id: str | None = None) -> ReferenceQuestionOrigin:
    """Validate retained origin facts without claiming a worker or another attempt."""
    if not db.in_transaction:
        raise ValueError("reference_shared_transaction_required")
    origin = await _row(
        db,
        "SELECT s.id AS original_session_id,s.user_id AS original_user_id,s.workspace_id AS original_workspace_id,"
        "w.team_id AS original_team_id,p.plan_json,p.source_hash,source.user_text AS original_user_text,"
        "e.id AS primary_execution_id,e.status AS primary_status,r.composition_digest,a.created_generation "
        "FROM turns t JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id "
        "JOIN chat_reference_plans p ON p.turn_id=t.id "
        "JOIN chat_reference_plan_retention retention ON retention.turn_id=p.turn_id "
        "JOIN chat_turn_user_sources source ON source.turn_id=t.id "
        "JOIN turn_agent_binding b ON b.turn_id=t.id JOIN turn_agent_execution e ON e.id=b.primary_execution_id "
        "JOIN chat_reference_resolutions r ON r.turn_id=t.id AND r.resolution_seq=1 AND r.execution_id=e.id "
        "JOIN chat_reference_resolutions a ON a.turn_id=t.id AND a.resolution_seq=2 AND a.execution_id=e.id "
        "JOIN chat_reference_evidence v ON v.turn_id=t.id AND v.execution_id=e.id AND v.resolution_seq=2 "
        "AND v.kind='delegate_started' AND v.created_generation=a.created_generation "
        "WHERE t.id=? AND p.retention_state='available' AND retention.available AND retention.deadline>julianday('now') "
        "AND json_extract(a.composition_json,'$.runtime_policy')='lead-delegation.v1'",
        (turn_id,),
    )
    if origin is None or type(origin["created_generation"]) is not int or origin["created_generation"] < 1:
        raise ValueError("reference_continuation_unavailable")
    generation = origin["created_generation"]
    if await _row(
        db,
        "SELECT 1 FROM chat_reference_model_invocations WHERE turn_id=? AND execution_id=? AND created_generation<?",
        (turn_id, origin["primary_execution_id"], generation),
    ):
        raise ValueError("reference_continuation_changed")
    rows = await read_history(
        db,
        turn_id=turn_id,
        user_id=origin["original_user_id"],
        workspace_id=origin["original_workspace_id"],
        session_id=origin["original_session_id"],
    )
    post = None
    if postdispatch_card_id is not None:
        positions = [index for index, row in enumerate(rows) if row["request_id"] == postdispatch_card_id]
        if len(positions) != 1:
            raise ValueError("reference_continuation_changed")
        post = rows[positions[0]]
        rows = rows[: positions[0]]
    if generation == 1:
        if rows:
            raise ValueError("reference_continuation_changed")
        return ReferenceQuestionOrigin(turn_id, origin["primary_execution_id"], generation, (), ())
    if not rows or any(row["status"] != "resolved" for row in rows):
        raise ValueError("reference_continuation_changed")
    try:
        contexts = [json.loads(row["resume_context_json"]) for row in rows]
    except (TypeError, ValueError) as exc:
        raise ValueError("reference_continuation_changed") from exc
    if any(
        not isinstance(context, dict)
        or not isinstance(context.get("reference_continuation"), dict)
        or context["reference_continuation"].get("contract") != CONTRACT
        for context in contexts
    ):
        raise ValueError("reference_continuation_changed")
    if any("question_round" in context for context in contexts):
        contexts = history_contexts(rows)
        if contexts[-1]["question_round"]["generation"] >= generation:
            raise ValueError("reference_continuation_changed")
        if post is not None:
            complete = history_contexts([*rows, post])
            if complete[-1]["question_round"]["generation"] != generation:
                raise ValueError("reference_continuation_changed")
        for index, card in enumerate(rows):
            lower, upper = resume_generation_bounds(contexts, index)
            upper = generation if upper is None else upper
            consumed = await _row(
                db,
                "SELECT MAX(json_extract(payload_json,'$.execution_generation')) AS generation FROM turn_events "
                "WHERE turn_id=? AND event_type='turn.resumed' AND json_valid(payload_json) "
                "AND json_type(payload_json,'$.execution_generation')='integer' "
                "AND json_extract(payload_json,'$.execution_generation')>? "
                "AND json_extract(payload_json,'$.execution_generation')<=? "
                "AND json_extract(payload_json,'$.request_id')=?",
                (turn_id, lower, upper, card["request_id"]),
            )
            if consumed is None or consumed["generation"] != upper:
                raise ValueError("reference_continuation_changed")
    elif len(rows) != 1 or post is not None:
        # Historical single-round Lead cards keep their original reader; they
        # cannot acquire a new mixed bounded history by dropping its markers.
        raise ValueError("reference_continuation_changed")
    answers = []
    for card in rows:
        retained = {**dict(origin), **dict(card)}
        _context(retained)
        answers.append({"question": card["rationale"], "answer": json.loads(answer(retained))})
    if len(json.dumps(answers, ensure_ascii=False).encode()) > 65536:
        raise ValueError("reference_continuation_changed")
    return ReferenceQuestionOrigin(turn_id, origin["primary_execution_id"], generation, tuple(row["request_id"] for row in rows), tuple(answers))
