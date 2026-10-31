"""Document checkpoints composed with the original shared card and Turn lease.

Every transition uses its caller's transaction. Domain adapters validate retained
planning evidence; this module owns card identity and generation reservations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiosqlite

from bowhead_core.clarification_resume_context import DocumentResumeContext, decode_clarification_context
from bowhead_core.clarification_store import ClarificationRequest, ClarificationStore
from bowhead_core.document_chat_continuation import (
    DocumentPlanningAttemptRef,
    DocumentPlanningCheckpointRef,
    DocumentResumeReservation,
    DocumentTurnOwner,
)

if TYPE_CHECKING:
    from bowhead_core.documents.continuation import DocumentCheckpointMaterial


class DocumentContinuationUnavailable(ValueError):
    def __init__(self) -> None:
        super().__init__("clarification_resume_unavailable")


async def _row(db, sql, params=()):
    async with db.execute(sql, params) as cursor:
        cursor.row_factory = aiosqlite.Row
        return await cursor.fetchone()


def _transaction(db) -> None:
    if not db.in_transaction:
        raise DocumentContinuationUnavailable()


async def installed(db) -> bool:
    return await _row(db, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='clarification_document_continuations'") is not None


async def document_card_id_in_tx(db, *, turn_id: str) -> str | None:
    if not await installed(db):
        return None
    row = await _row(db, "SELECT card_id FROM clarification_document_continuations WHERE turn_id=?", (turn_id,))
    return row["card_id"] if row else None


@dataclass(frozen=True)
class DocumentContinuationCard:
    card: ClarificationRequest
    snapshot: DocumentResumeContext
    reserved_generation: int | None
    consumed_generation: int | None
    consumed_owner_id: str | None

    def creation_worker(self) -> DocumentTurnOwner:
        snapshot = self.snapshot
        return DocumentTurnOwner(
            team_id=snapshot.team_id,
            user_id=snapshot.user_id,
            workspace_id=snapshot.workspace_id,
            session_id=snapshot.session_id,
            turn_id=snapshot.turn_id,
            generation=snapshot.continuation.creation_generation,
            owner_id=snapshot.continuation.creation_owner_id,
        )


async def read_document_card_in_tx(db, *, card_id: str) -> DocumentContinuationCard:
    association = await _row(db, "SELECT * FROM clarification_document_continuations WHERE card_id=?", (card_id,))
    row = await _row(db, "SELECT * FROM clarification_requests WHERE request_id=?", (card_id,))
    if association is None or row is None:
        raise DocumentContinuationUnavailable()
    card = ClarificationStore._row_to_request(row)
    snapshot = decode_clarification_context(card)
    if not isinstance(snapshot, DocumentResumeContext):
        raise DocumentContinuationUnavailable()
    continuation = snapshot.continuation
    checkpoint = continuation.checkpoint
    if (
        card.kind != "ambiguous_turn"
        or card.token
        or card.tool
        or card.multi_select
        or card.candidates
        or (card.turn_id, card.user_id, card.workspace_id, card.session_id)
        != (snapshot.turn_id, snapshot.user_id, snapshot.workspace_id, snapshot.session_id)
        or (association["turn_id"], association["job_id"], association["attempt_no"], association["checkpoint_digest"], association["source_hash"])
        != (snapshot.turn_id, checkpoint.job_id, checkpoint.attempt_no, checkpoint.checkpoint_digest, checkpoint.source_hash)
        or (association["creation_generation"], association["creation_owner_id"], association["runtime_snapshot_digest"])
        != (continuation.creation_generation, continuation.creation_owner_id, continuation.runtime_snapshot_digest)
    ):
        raise DocumentContinuationUnavailable()
    return DocumentContinuationCard(
        card, snapshot, association["reserved_generation"], association["consumed_generation"], association["consumed_owner_id"]
    )


def document_answer(card: ClarificationRequest) -> str:
    """Read one canonical free-text answer; display projections grant no authority."""
    answers = card.answers
    if card.status != "resolved" or not isinstance(answers, list) or len(answers) != 1:
        raise DocumentContinuationUnavailable()
    answer = answers[0]
    if (
        not isinstance(answer, dict)
        or set(answer) - {"question", "source", "answer", "skipped", "option_index"}
        or answer.get("question") != card.rationale
        or answer.get("source") != "custom"
        or answer.get("skipped") is not False
        or answer.get("option_index") is not None
        or not isinstance(answer.get("answer"), str)
        or not 1 <= len(answer["answer"]) <= 256
        or answer["answer"] != answer["answer"].strip()
    ):
        raise DocumentContinuationUnavailable()
    return answer["answer"]


def document_question(card: ClarificationRequest) -> list[dict]:
    from bowhead_core.agents.clarification_tool import normalize_clarification_questions

    question = card.rationale
    questions = [{"question": question, "type": "free_text", "options": []}]
    if not isinstance(question, str) or question != question.strip() or not 1 <= len(question) <= 200:
        raise DocumentContinuationUnavailable()
    if normalize_clarification_questions(questions) != questions:
        raise DocumentContinuationUnavailable()
    return questions


async def prepare_document_pause_in_tx(
    db,
    *,
    store: ClarificationStore,
    worker: DocumentTurnOwner,
    checkpoint: DocumentPlanningCheckpointRef,
    questions: list[dict],
) -> ClarificationRequest:
    from bowhead_core.documents.continuation import validate_document_pause_in_tx
    from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

    _transaction(db)
    material = await validate_document_pause_in_tx(db, worker=worker, checkpoint=checkpoint)
    expected_questions = [{"question": material.question, "type": "free_text", "options": []}]
    if questions != expected_questions:
        raise DocumentContinuationUnavailable()
    existing_id = await document_card_id_in_tx(db, turn_id=worker.turn_id)
    if existing_id:
        existing = await read_document_card_in_tx(db, card_id=existing_id)
        if existing.creation_worker() != worker or existing.snapshot.continuation.checkpoint != checkpoint or existing.card.status != "pending":
            raise DocumentContinuationUnavailable()
        return existing.card
    if await _row(db, "SELECT 1 FROM clarification_requests WHERE turn_id=? AND status<>'resolved'", (worker.turn_id,)):
        raise DocumentContinuationUnavailable()
    source = await _read_chat_turn_user_source_in_tx(
        db, turn_id=worker.turn_id, expected_user_id=worker.user_id, expected_workspace_id=worker.workspace_id
    )
    if source is None or source.session_id != worker.session_id:
        raise DocumentContinuationUnavailable()
    snapshot = DocumentResumeContext.model_validate(
        {
            "__schema_version": 3,
            "team_id": worker.team_id,
            "user_id": worker.user_id,
            "workspace_id": worker.workspace_id,
            "session_id": worker.session_id,
            "turn_id": worker.turn_id,
            "user_text": source.user_text,
            "continuation": {
                "kind": "document_planning",
                "version": 1,
                "checkpoint": checkpoint.model_dump(),
                "runtime_snapshot_digest": material.runtime_snapshot_digest,
                "creation_generation": worker.generation,
                "creation_owner_id": worker.owner_id,
            },
        }
    )
    card = await store.put(
        turn_id=worker.turn_id,
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        session_id=worker.session_id,
        token="",
        rationale=material.question,
        candidates=[],
        kind="ambiguous_turn",
        resume_context=snapshot.model_dump(by_alias=True),
        connection=db,
    )
    if document_question(card) != questions:
        raise DocumentContinuationUnavailable()
    await db.execute(
        """INSERT INTO clarification_document_continuations
        (card_id,job_id,attempt_no,turn_id,creation_generation,creation_owner_id,checkpoint_digest,source_hash,runtime_snapshot_digest,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            card.request_id,
            checkpoint.job_id,
            checkpoint.attempt_no,
            worker.turn_id,
            worker.generation,
            worker.owner_id,
            checkpoint.checkpoint_digest,
            checkpoint.source_hash,
            material.runtime_snapshot_digest,
            card.created_at,
        ),
    )
    return card


async def _retained_material(db, record: DocumentContinuationCard) -> DocumentCheckpointMaterial:
    from bowhead_core.documents.continuation import load_document_checkpoint_in_tx

    material = await load_document_checkpoint_in_tx(db, checkpoint=record.snapshot.continuation.checkpoint, creation_worker=record.creation_worker())
    if material.question != record.card.rationale or material.runtime_snapshot_digest != record.snapshot.continuation.runtime_snapshot_digest:
        raise DocumentContinuationUnavailable()
    return material


async def require_document_pause_in_tx(db, *, card_id: str, answered: bool = False) -> DocumentContinuationCard:
    from bowhead_core.documents.continuation import validate_document_resume_in_tx

    record = await read_document_card_in_tx(db, card_id=card_id)
    if record.card.status not in {"pending", "resolved"} or record.reserved_generation is not None or record.consumed_generation is not None:
        raise DocumentContinuationUnavailable()
    material = await validate_document_resume_in_tx(db, checkpoint=record.snapshot.continuation.checkpoint, creation_worker=record.creation_worker())
    if material.question != record.card.rationale or material.runtime_snapshot_digest != record.snapshot.continuation.runtime_snapshot_digest:
        raise DocumentContinuationUnavailable()
    row = await _row(
        db,
        """SELECT t.execution_generation,d.owner_id,d.lease_kind FROM turns t JOIN sessions s ON s.id=t.session_id
        JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=s.id
        WHERE t.id=? AND s.id=? AND t.status='clarification_pending' AND s.deleted_at IS NULL
        AND (s.active_turn_id IS NULL OR s.active_turn_id=t.id) AND d.state IN ('terminal','failed')
        AND d.lease_execution_generation=t.execution_generation AND d.cancel_requested_at IS NULL""",
        (record.snapshot.turn_id, record.snapshot.session_id),
    )
    creation = record.creation_worker()
    if row is None or row["execution_generation"] < creation.generation:
        raise DocumentContinuationUnavailable()
    if row["execution_generation"] == creation.generation:
        if row["owner_id"] not in {creation.owner_id, None}:
            raise DocumentContinuationUnavailable()
    elif row["lease_kind"] != "resume" or row["owner_id"] is not None:
        raise DocumentContinuationUnavailable()
    if answered:
        document_answer(record.card)
    return record


async def prepare_document_resume_in_tx(db, *, card_id: str, next_generation: int) -> DocumentResumeReservation:
    _transaction(db)
    record = await require_document_pause_in_tx(db, card_id=card_id, answered=True)
    turn = await _row(db, "SELECT execution_generation FROM turns WHERE id=?", (record.snapshot.turn_id,))
    if type(next_generation) is not int or next_generation != turn["execution_generation"] + 1:
        raise DocumentContinuationUnavailable()
    cursor = await db.execute(
        "UPDATE clarification_document_continuations SET reserved_generation=? WHERE card_id=? AND reserved_generation IS NULL AND consumed_generation IS NULL",
        (next_generation, card_id),
    )
    if cursor.rowcount != 1:
        raise DocumentContinuationUnavailable()
    return DocumentResumeReservation(card_id=card_id, checkpoint=record.snapshot.continuation.checkpoint, generation=next_generation)


async def consume_document_resume_in_tx(db, *, worker: DocumentTurnOwner, reservation: DocumentResumeReservation) -> DocumentPlanningAttemptRef:
    from bowhead_core.documents.continuation import consume_document_attempt_in_tx

    _transaction(db)
    record = await read_document_card_in_tx(db, card_id=reservation.card_id)
    original = record.creation_worker()
    if (
        record.snapshot.continuation.checkpoint != reservation.checkpoint
        or record.reserved_generation != reservation.generation
        or record.consumed_generation is not None
        or worker.generation != reservation.generation
        or worker.model_dump(exclude={"generation", "owner_id"}) != original.model_dump(exclude={"generation", "owner_id"})
    ):
        raise DocumentContinuationUnavailable()
    await _retained_material(db, record)
    answer = document_answer(record.card)
    attempt = await consume_document_attempt_in_tx(
        db, worker=worker, checkpoint=reservation.checkpoint, card_id=reservation.card_id, answer_text=answer
    )
    if attempt != DocumentPlanningAttemptRef(job_id=reservation.checkpoint.job_id, attempt_no=2, worker=worker):
        raise DocumentContinuationUnavailable()
    cursor = await db.execute(
        "UPDATE clarification_document_continuations SET consumed_generation=?,consumed_owner_id=? "
        "WHERE card_id=? AND reserved_generation=? AND consumed_generation IS NULL",
        (worker.generation, worker.owner_id, reservation.card_id, reservation.generation),
    )
    if cursor.rowcount != 1:
        raise DocumentContinuationUnavailable()
    return attempt


async def release_document_resume_in_tx(db, *, card_id: str, generation: int) -> None:
    from bowhead_core.documents.continuation import document_resume_started_in_tx

    _transaction(db)
    record = await read_document_card_in_tx(db, card_id=card_id)
    if record.reserved_generation != generation or record.consumed_generation is not None:
        raise DocumentContinuationUnavailable()
    if await document_resume_started_in_tx(db, checkpoint=record.snapshot.continuation.checkpoint):
        raise DocumentContinuationUnavailable()
    dispatch = await _row(
        db,
        "SELECT 1 FROM chat_turn_spawn_dispatch WHERE turn_id=? AND lease_kind='resume' "
        "AND lease_execution_generation=? AND state IN ('claimed','spawned')",
        (record.snapshot.turn_id, generation),
    )
    if dispatch is None:
        raise DocumentContinuationUnavailable()
    await db.execute(
        "UPDATE clarification_document_continuations SET reserved_generation=NULL WHERE card_id=? AND reserved_generation=? AND consumed_generation IS NULL",
        (card_id, generation),
    )


async def prior_document_answers_in_tx(db, *, worker: DocumentTurnOwner, job_id: str | None = None) -> list[dict]:
    """Retain supported ordinary questions without trusting reconstructed model text."""
    from bowhead_core.clarification_rounds import CONTEXT_KEY, history_contexts, question_hashes, read_history

    rows = await read_history(db, turn_id=worker.turn_id, user_id=worker.user_id, workspace_id=worker.workspace_id, session_id=worker.session_id)
    document_card_id = await document_card_id_in_tx(db, turn_id=worker.turn_id)
    if document_card_id is not None:
        record = await read_document_card_in_tx(db, card_id=document_card_id)
        if (
            record.creation_worker() != worker
            or job_id is not None
            and record.snapshot.continuation.checkpoint.job_id != job_id
            or not any(row["request_id"] == document_card_id for row in rows)
        ):
            raise DocumentContinuationUnavailable()
        rows = [row for row in rows if row["request_id"] != document_card_id]
    if job_id is not None:
        from bowhead_core.documents.reference_answers import read_reference_document_answers_in_tx
        from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable, read_ordinary_document_answer_in_tx

        reference_answers = await read_reference_document_answers_in_tx(db, worker=worker, job_id=job_id, cards=rows)
        if reference_answers is not None:
            return reference_answers

        dispatch = await _row(db, "SELECT source_hash FROM document_turn_dispatches WHERE job_id=?", (job_id,))
        if dispatch is None:
            raise DocumentContinuationUnavailable()
        try:
            retained = await read_ordinary_document_answer_in_tx(db, creation_worker=worker, source_hash=dispatch["source_hash"], job_id=job_id)
        except OrdinaryContinuationUnavailable as exc:
            raise DocumentContinuationUnavailable() from exc
        if retained is not None:
            # Later domain attempts read the original admission, not today's worker lease.
            return [dict(answer) for answer in retained.answers]
    contexts = history_contexts(rows)
    if (worker.generation == 1) != (not rows):
        raise DocumentContinuationUnavailable()
    result = []
    for row, context in zip(rows, contexts, strict=True):
        marker = context[CONTEXT_KEY]
        if row["status"] != "resolved" or marker["generation"] >= worker.generation or row["kind"] != "ambiguous_turn" or row["token"]:
            raise DocumentContinuationUnavailable()
        answers = json.loads(row["answers_json"] or "null")
        if not isinstance(answers, list) or not answers:
            raise DocumentContinuationUnavailable()
        questions = [{"question": answer.get("question")} for answer in answers if isinstance(answer, dict)]
        if len(questions) != len(answers) or question_hashes(questions, None) != marker["question_hashes"]:
            raise DocumentContinuationUnavailable()
        for answer in answers:
            if (
                set(answer) - {"question", "source", "answer", "skipped", "option_index"}
                or answer.get("source") not in {"candidate", "custom", "skip"}
                or type(answer.get("skipped")) is not bool
                or answer["skipped"] != (answer["source"] == "skip")
                or (answer["source"] == "skip" and answer.get("answer") is not None)
                or (answer["source"] != "skip" and (not isinstance(answer.get("answer"), str) or not 1 <= len(answer["answer"]) <= 256))
            ):
                raise DocumentContinuationUnavailable()
        result.extend(answers)
    return result


async def hydrate_document_resume_request(session_store, request) -> None:
    """Load frozen presentation/model selection without reading attachments again."""
    from bowhead_core._db import open_shared_ctx

    async with open_shared_ctx(session_store.db_path) as db:
        await db.execute("BEGIN")
        record = await require_document_pause_in_tx(db, card_id=request.document_clarification_id, answered=True)
        snapshot = record.snapshot
        if (request.turn_id, request.session_id, request.workspace_id, request.user_id, request.team_id) != (
            snapshot.turn_id,
            snapshot.session_id,
            snapshot.workspace_id,
            snapshot.user_id,
            snapshot.team_id,
        ) or request.document_checkpoint != snapshot.continuation.checkpoint:
            raise DocumentContinuationUnavailable()
        material = await _retained_material(db, record)
        request.language_context = material.runtime_snapshot.language.language_context()
        request.preferred_provider = material.provider
        request.preferred_model = material.model


def document_card_payload(card: ClarificationRequest) -> dict:
    return {
        "clarification_id": card.request_id,
        "kind": card.kind,
        "fallback_text": card.rationale,
        "questions": document_question(card),
        "candidates": [],
        "multi_select": False,
        "options": None,
        "remember_option": None,
        "tool": None,
        # The chat card presents a document task's question as "Needs information · Document draft" with
        # "Continue generating"; ordinary questions carry no purpose.
        "purpose": "document_draft",
    }


async def document_pause_retained_in_tx(db, *, turn_id: str) -> bool:
    if await _row(db, "SELECT 1 FROM turns WHERE id=? AND status='clarification_pending'", (turn_id,)) is None:
        return False
    card_id = await document_card_id_in_tx(db, turn_id=turn_id)
    if card_id is None:
        return False
    try:
        record = await read_document_card_in_tx(db, card_id=card_id)
        await _retained_material(db, record)
        return record.card.status in {"pending", "resolved"} and record.reserved_generation is None and record.consumed_generation is None
    except ValueError:
        return False


async def recover_document_pauses_in_tx(db, *, now: str) -> None:
    """Finish a prepared pause or invalidate it, without re-executing planning.

    Runs inside shared dispatch maintenance. Healthy cards retain queued steer;
    invalidated cards return to the existing terminal-event/steer repair path.
    """
    from uuid import uuid4

    from bowhead_core.documents.continuation import invalidate_document_pause_in_tx
    from bowhead_core.session_store import _legacy_generation_floor_in_tx, _legacy_root_terminal_matches_in_tx, _next_legacy_event_cursors_in_tx

    if not await installed(db):
        return
    async with db.execute(
        """SELECT c.card_id,t.id,t.status,t.execution_generation,t.tokens_input,t.tokens_output,
        d.state,d.owner_id,d.lease_kind,d.lease_execution_generation,d.lease_expires_at,d.cancel_requested_at
        FROM clarification_document_continuations c JOIN turns t ON t.id=c.turn_id
        JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id
        JOIN document_executions e ON e.job_id=c.job_id
        WHERE c.reserved_generation IS NULL AND c.consumed_generation IS NULL
        AND (t.status='clarification_pending' AND d.state IN ('terminal','failed','drain_pending')
          OR t.status IN ('running','clarification_pending') AND t.execution_generation=c.creation_generation
          AND d.state='spawned' AND d.owner_id=c.creation_owner_id AND d.lease_execution_generation=c.creation_generation
          AND d.lease_expires_at<=?
          OR t.status IN ('cancelled','failed','aborted') AND e.state='question'
          AND e.owner=c.creation_owner_id
          AND (d.state IN ('terminal','failed','drain_pending') OR d.state='spawned'
            AND d.owner_id=c.creation_owner_id AND d.lease_execution_generation=c.creation_generation
            AND t.execution_generation=c.creation_generation))
        ORDER BY d.updated_at,c.card_id LIMIT 64""",
        (now,),
    ) as cursor:
        cursor.row_factory = aiosqlite.Row
        rows = await cursor.fetchall()
    for row in rows:
        if row["status"] in {"cancelled", "failed", "aborted"}:
            # Stop may settle the Turn before its prepared pause is finalized.
            # Close only the retained domain pause; never reconstruct that Turn.
            await invalidate_document_pause_in_tx(db, card_id=row["card_id"], turn_id=row["id"], now=now)
            continue
        await db.execute("SAVEPOINT document_pause_recovery")
        try:
            record = await read_document_card_in_tx(db, card_id=row["card_id"])
            creation = record.creation_worker()
            if (
                row["lease_execution_generation"] != row["execution_generation"]
                or row["cancel_requested_at"] is not None
                or row["status"] == "running"
                and (row["execution_generation"] != creation.generation or row["owner_id"] != creation.owner_id)
            ):
                raise DocumentContinuationUnavailable()
            await db.execute(
                "UPDATE turns SET status='clarification_pending',ended_at=?,error_kind='needs_clarification' WHERE id=?", (now, row["id"])
            )
            await db.execute("UPDATE chat_turn_spawn_dispatch SET state='terminal',updated_at=? WHERE turn_id=?", (now, row["id"]))
            await require_document_pause_in_tx(db, card_id=row["card_id"])
            event = await _row(
                db,
                "SELECT 1 FROM turn_events WHERE turn_id=? AND event_type='clarification_required' "
                "AND json_extract(payload_json,'$.clarification_id')=?",
                (row["id"], row["card_id"]),
            )
            payloads = []
            if event is None:
                payloads.append(("clarification_required", document_card_payload(record.card)))
            floor = await _legacy_generation_floor_in_tx(
                db, turn_id=row["id"], lease_kind=row["lease_kind"], lease_execution_generation=row["lease_execution_generation"]
            )
            if not await _legacy_root_terminal_matches_in_tx(
                db, turn_id=row["id"], generation_floor=floor, canonical_status="clarification_pending", canonical_final_answer=""
            ):
                from bowhead_core.document_chat_usage import document_usage_projection_in_tx

                payloads.append(
                    (
                        "done",
                        {
                            "turn_id": row["id"],
                            "status": "clarification_pending",
                            "error_kind": "needs_clarification",
                            "tokens_input": row["tokens_input"],
                            "tokens_output": row["tokens_output"],
                            "display_usage": await document_usage_projection_in_tx(db, turn_id=row["id"]),
                            "schema_version": "0.9.0",
                        },
                    )
                )
            seq, event_seq = await _next_legacy_event_cursors_in_tx(db, turn_id=row["id"])
            for event_type, payload in payloads:
                payload.update(seq=event_seq, evt_seq=event_seq)
                await db.execute(
                    "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,?,?,?,?)",
                    (f"evt_{uuid4().hex[:12]}", row["id"], seq, event_type, json.dumps(payload, ensure_ascii=False), now),
                )
                seq += 1
                event_seq += 1
        except ValueError:
            await db.execute("ROLLBACK TO SAVEPOINT document_pause_recovery")
            await invalidate_document_pause_in_tx(db, card_id=row["card_id"], turn_id=row["id"], now=now)
            card = await _row(db, "SELECT status FROM clarification_requests WHERE request_id=?", (row["card_id"],))
            status = "cancelled" if card and card[0] == "dismissed" else "failed"
            await db.execute(
                "UPDATE turns SET status=?,ended_at=?,error_kind='clarification_resume_unavailable',error='clarification_resume_unavailable' "
                "WHERE id=? AND status IN ('running','clarification_pending')",
                (status, now, row["id"]),
            )
            await db.execute(
                "UPDATE chat_turn_spawn_dispatch SET state='drain_pending',lease_expires_at=?,updated_at=? WHERE turn_id=?", (now, now, row["id"])
            )
        finally:
            await db.execute("RELEASE SAVEPOINT document_pause_recovery")
