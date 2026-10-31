"""Read original Reference answers for their exact native document job."""

from __future__ import annotations

import json

import aiosqlite

from bowhead_core.chat_reference_lead_postdispatch import CONTRACT, answer, read_pause
from bowhead_core.chat_reference_question_origin import ReferenceQuestionOrigin
from bowhead_core.chat_reference_storage import _row, reference_source_hash
from bowhead_core.chat_turn_source import chat_turn_user_source_projection
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.documents.agent_caller import document_job_caller_in_tx
from bowhead_core.documents.request import DocumentScope
from bowhead_core.documents.store import _digest, _json
from bowhead_core.session_store import _read_chat_turn_user_source_in_tx


async def read_reference_document_answers_in_tx(
    db: aiosqlite.Connection, *, worker: DocumentTurnOwner, job_id: str, cards: list[aiosqlite.Row]
) -> list[dict] | None:
    """Use consumed Reference evidence, never a new answer or a live resume claim."""
    if not db.in_transaction:
        raise ValueError("document_reference_answer_unavailable")
    present = await _row(
        db,
        "SELECT request_id FROM clarification_requests WHERE turn_id=? AND CASE WHEN json_valid(resume_context_json) "
        "THEN json_extract(resume_context_json,'$.reference_continuation.contract') END=?",
        (worker.turn_id, CONTRACT),
    )
    if present is None:
        return None
    row = await read_pause(db, worker.turn_id)
    if row is None:
        raise ValueError("document_reference_answer_unavailable")
    origin = row["question_origin"]
    if not isinstance(origin, ReferenceQuestionOrigin) or tuple(card["request_id"] for card in cards) != (*origin.card_ids, row["request_id"]):
        raise ValueError("document_reference_answer_unavailable")
    scope = DocumentScope(team_id=worker.team_id, user_id=worker.user_id, workspace_id=worker.workspace_id)
    caller = await document_job_caller_in_tx(db, scope=scope, turn_id=worker.turn_id, job_id=job_id)
    job = await _row(
        db,
        "SELECT j.team_id,j.requested_by,j.workspace_id,j.session_id,j.origin_turn_id,j.user_source_sha256,"
        "d.creation_generation,d.owner_id,d.source_hash FROM document_jobs j "
        "JOIN document_turn_dispatches d ON d.job_id=j.id WHERE j.id=?",
        (job_id,),
    )
    attempt = await _row(db, "SELECT * FROM chat_reference_lead_postdispatch_attempts WHERE turn_id=?", (worker.turn_id,))
    source = await _read_chat_turn_user_source_in_tx(
        db, turn_id=worker.turn_id, expected_user_id=worker.user_id, expected_workspace_id=worker.workspace_id
    )
    if (
        job is None
        or attempt is None
        or source is None
        or source.session_id != worker.session_id
        or (
            job["team_id"],
            job["requested_by"],
            job["workspace_id"],
            job["session_id"],
            job["origin_turn_id"],
            job["creation_generation"],
            job["owner_id"],
        )
        != (worker.team_id, worker.user_id, worker.workspace_id, worker.session_id, worker.turn_id, worker.generation, worker.owner_id)
        or (row["original_team_id"], row["original_user_id"], row["original_workspace_id"], row["original_session_id"], row["primary_execution_id"])
        != (worker.team_id, worker.user_id, worker.workspace_id, worker.session_id, caller)
        or (attempt["clarification_id"], attempt["execution_id"], attempt["generation"], attempt["owner_id"])
        != (row["request_id"], caller, worker.generation, worker.owner_id)
        or job["source_hash"] != job["user_source_sha256"]
        or row["source_hash"] != reference_source_hash(source)
        or job["source_hash"] != _digest(_json(chat_turn_user_source_projection(source)))
    ):
        raise ValueError("document_reference_answer_unavailable")
    return [*origin.answers, {"question": row["rationale"], "answer": json.loads(answer(row))}]
