"""Read-only clarification facts for topic continuity, never resume authority."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope


def _card_data(row: Any) -> dict[str, Any]:
    questions = json.loads(row["questions_json"] or "[]")
    if row["context_version"] == 3:
        questions = [{"question": row["rationale"], "type": "free_text", "options": []}]
    if not isinstance(questions, list):
        raise ValueError("Invalid historical questions")
    questions = [
        {key: question[key] for key in ("question", "type", "options") if key in question} for question in questions if isinstance(question, dict)
    ]
    if not questions and row["rationale"]:
        candidates = json.loads(row["candidates_json"] or "[]")
        questions = [
            {
                "question": row["rationale"],
                "options": [candidate.get("label") or candidate.get("value") for candidate in candidates if isinstance(candidate, dict)],
            }
        ]
    answers = json.loads(row["answers_json"] or "[]")
    if not isinstance(answers, list):
        raise ValueError("Invalid historical answers")
    data = {
        "status": row["status"],
        "kind": row["kind"],
        "questions": questions,
        "answers": [
            {key: answer[key] for key in ("question", "source", "answer", "skipped", "option_index") if key in answer}
            for answer in answers
            if isinstance(answer, dict)
        ],
    }
    if not answers:
        data["legacy_selected_values"] = json.loads(row["chosen_values_json"] or "[]") or ([row["chosen_value"]] if row["chosen_value"] else [])
    return data


async def load_history(
    db_path: str, scope: ReferenceRequestScope, before_turn_id: str, *, turn_ids: tuple[str, ...] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Project displayed questions and saved answers inside one owned Chat.

    Read the latest 200 prior Turns, matching the generic history window. The
    card cap bounds legacy data too. Checkpoint ciphertext, tokens, grants and
    source manifests never leave SQL; reading cannot reconcile or resume work.
    """
    if not scope.session_id:
        return {}
    if turn_ids is not None and (not 1 <= len(turn_ids) <= 100 or any(not isinstance(value, str) or not value for value in turn_ids)):
        raise ValueError("Invalid clarification history page")
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        await require_reference_request_scope(db, scope)
        async with db.execute("SELECT seq FROM turns WHERE id=? AND session_id=?", (before_turn_id, scope.session_id)) as cursor:
            current = await cursor.fetchone()
        if current is None:
            raise ValueError("Clarification history Turn is unavailable")
        page = " AND id IN (" + ",".join("?" for _ in turn_ids) + ")" if turn_ids is not None else " ORDER BY seq DESC LIMIT 200"
        card_limit = "" if turn_ids is not None else " LIMIT 600"
        async with db.execute(
            "WITH prior AS (SELECT id,status,seq FROM turns WHERE session_id=? AND operation_kind IS NULL AND seq<?" + page + ") "
            "SELECT c.turn_id,c.status,c.kind,c.rationale,c.candidates_json,c.answers_json,c.chosen_value,c.chosen_values_json,"
            "CASE WHEN json_valid(c.resume_context_json) THEN json_extract(c.resume_context_json,'$.__schema_version') END AS context_version,"
            "CASE WHEN json_valid(c.resume_context_json) THEN CASE json_extract(c.resume_context_json,'$.__schema_version') "
            "WHEN 2 THEN json_extract(c.resume_context_json,'$.card_questions') "
            "WHEN 4 THEN json_extract(c.resume_context_json,'$.questions') END END AS questions_json "
            "FROM prior t JOIN clarification_requests c ON c.turn_id=t.id "
            "WHERE t.status='clarification_pending' AND c.session_id=? AND c.user_id=? AND c.workspace_id=? "
            "ORDER BY t.seq DESC,c.created_at DESC,c.request_id DESC" + card_limit,
            (scope.session_id, current["seq"], *(turn_ids or ()), scope.session_id, scope.user_id, scope.workspace_id),
        ) as cursor:
            rows = await cursor.fetchall()
    result: dict[str, list[dict[str, Any]]] = {}
    for row in reversed(rows):
        try:
            card = _card_data(row)
        except (TypeError, ValueError):
            card = {"availability": "unavailable"}
        result.setdefault(row["turn_id"], []).append(card)
    return result
