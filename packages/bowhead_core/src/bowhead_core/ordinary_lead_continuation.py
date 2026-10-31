"""The original ordinary question owns its retained execution and first answer.

These helpers use the caller's transaction and never schedule work. Document
consumers receive typed user data, never the encrypted execution transcript.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import aiosqlite

from bowhead_core.chat_turn_source import chat_turn_user_source_projection
from bowhead_core.clarification_resume_context import OrdinaryLeadResumeContext, decode_clarification_context
from bowhead_core.clarification_store import ClarificationRequest, ClarificationStore
from bowhead_core.document_chat_continuation import DocumentTurnOwner

if TYPE_CHECKING:
    from bowhead_core.chat_reference_storage import ReferenceScope
    from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadCapture, OrdinaryLeadCheckpoint
    from bowhead_core.tool_registry import ToolExecutionContext
    from bowhead_core.types import ToolInvocation


class OrdinaryContinuationUnavailable(ValueError):
    def __init__(self, *, stage: Literal["clarification_capture"] | None = None) -> None:
        super().__init__("clarification_resume_unavailable")
        self.stage = stage


class OrdinaryQuestionUnavailable(OrdinaryContinuationUnavailable):
    """An optional new pause cannot retain this live execution shape."""

    def __init__(self, note: str) -> None:
        super().__init__(stage="clarification_capture")
        self.note = note


def ordinary_multi_helper_question_enabled() -> bool:
    return os.environ.get("FEATURE_CHAT_MULTI_HELPER_QUESTION", "1") == "1"


def ordinary_failure_collection_enabled(ctx: ToolExecutionContext) -> bool:
    """Only a live ordinary collector can retain a native helper failure."""
    from bowhead_router.invocation import physical_invocation_required

    state = getattr(ctx, "ordinary_lead_state", None)
    return bool(state is not None and state.checkpoint is None and ordinary_multi_helper_question_enabled() and not physical_invocation_required())


def ordinary_execution_refusal() -> dict:
    """Frozen feedback used only to validate historical encrypted transcripts."""
    return {
        "ok": False,
        "error_code": "ordinary_execution_uncertain",
        "error_kind": "ordinary_execution_uncertain",
        "retryable": False,
        "execution_outcome": "not_dispatched",
        "directive": "An earlier operation has an unknown outcome. Explain retained facts or ask the user; do not repeat or initiate tool execution.",
    }


# Tools that pause, switch the run's context or own a separate continuation
# contract. Their effect is not captured by the Lead transcript alone.
_UNRETAINED_LEAD_TOOLS = frozenset({"clarification_ask", "plan_propose", "tool_switch_context", "template_document_submit", "subagent_return"})


def retained_lead_call(invocation: ToolInvocation) -> bool:
    """A settled call made by Lead itself before its question.

    Its result is already a tool message in the encrypted Lead transcript, so a
    resume continues from that result and never executes the call again. This
    holds for any risk class, including external writes.
    """
    return (
        invocation.tool_name != "subagent_spawn"
        and invocation.tool_name not in _UNRETAINED_LEAD_TOOLS
        and bool(invocation.ended_at)
        and not invocation.nested
        and not (invocation.result == ordinary_execution_refusal() and not invocation.error)
    )


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


async def _row(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
    if not db.in_transaction:
        raise OrdinaryContinuationUnavailable()
    async with db.execute(sql, params) as cursor:
        cursor.row_factory = aiosqlite.Row
        return await cursor.fetchone()


@dataclass(frozen=True)
class OrdinaryQuestionAnswer:
    card_id: str
    source_hash: str
    questions: tuple[dict, ...]
    answers: tuple[dict, ...]


@dataclass(frozen=True)
class OrdinaryContinuationCard:
    card: ClarificationRequest
    snapshot: OrdinaryLeadResumeContext
    consumed_generation: int | None
    consumed_owner_id: str | None
    source_db_path: str | None = None

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


def validate_questions(questions: list[dict]) -> None:
    from bowhead_core.agents.clarification_tool import normalize_clarification_questions

    # Keep the exact localized option labels in the immutable question. The
    # model's original arguments remain separately bound inside the checkpoint.
    canonical = [{key: value for key, value in question.items() if key != "option_labels"} for question in questions]
    if not questions or normalize_clarification_questions(canonical) != canonical:
        raise OrdinaryContinuationUnavailable()
    for question in questions:
        labels = question.get("option_labels")
        if labels is not None and (
            not isinstance(labels, list)
            or len(labels) != len(question["options"])
            or any(not isinstance(label, str) or not label.strip() for label in labels)
        ):
            raise OrdinaryContinuationUnavailable()
    if len({question["question"] for question in questions}) != len(questions):
        raise OrdinaryContinuationUnavailable()


def typed_answer(record: OrdinaryContinuationCard) -> OrdinaryQuestionAnswer:
    card, snapshot = record.card, record.snapshot
    answers = card.answers
    if card.status != "resolved" or not isinstance(answers, list) or not answers or len(answers) > 15:
        raise OrdinaryContinuationUnavailable()
    offset = 0
    pairs = []
    for question in snapshot.questions:
        group = []
        while offset < len(answers) and isinstance(answers[offset], dict) and answers[offset].get("question") == question["question"]:
            group.append(answers[offset])
            offset += 1
        # The existing card permits a partial response. An unanswered question
        # remains absent; never turn it into an explicit user skip.
        if not group:
            continue
        if question["type"] != "multi_select" and len(group) != 1:
            raise OrdinaryContinuationUnavailable()
        if len(group) > 1 and any(answer.get("source") == "skip" for answer in group):
            raise OrdinaryContinuationUnavailable()
        identities = [canonical_json([answer.get("source"), answer.get("option_index")]) for answer in group]
        if len(set(identities)) != len(identities):
            raise OrdinaryContinuationUnavailable()
        pairs.extend((question, answer) for answer in group)
    if offset != len(answers):
        raise OrdinaryContinuationUnavailable()
    for question, answer in pairs:
        if (
            not isinstance(answer, dict)
            or set(answer) != {"question", "source", "answer", "skipped", "option_index"}
            or answer["question"] != question["question"]
            or answer["source"] not in {"candidate", "custom", "skip"}
            or type(answer["skipped"]) is not bool
            or answer["skipped"] != (answer["source"] == "skip")
        ):
            raise OrdinaryContinuationUnavailable()
        if answer["source"] == "skip":
            if answer["answer"] is not None or answer["option_index"] is not None:
                raise OrdinaryContinuationUnavailable()
        elif not isinstance(answer["answer"], str) or not 1 <= len(answer["answer"]) <= 256:
            raise OrdinaryContinuationUnavailable()
        elif answer["source"] == "custom":
            if answer["option_index"] is not None:
                raise OrdinaryContinuationUnavailable()
        else:
            index = answer["option_index"]
            if (
                type(index) is not int
                or question["type"] not in {"single_select", "multi_select"}
                or not 0 <= index < len(question["options"])
                or answer["answer"] != question["options"][index]
            ):
                raise OrdinaryContinuationUnavailable()
    return OrdinaryQuestionAnswer(
        card.request_id,
        snapshot.continuation.source_hash,
        tuple(json.loads(canonical_json(snapshot.questions))),
        tuple(json.loads(canonical_json(answers))),
    )


async def ordinary_card_id_in_tx(db: aiosqlite.Connection, *, turn_id: str) -> str | None:
    if await _row(db, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='clarification_requests'") is None:
        return None
    row = await _row(
        db,
        "SELECT request_id FROM clarification_requests WHERE turn_id=? AND json_valid(resume_context_json) "
        "AND (json_extract(resume_context_json,'$.__schema_version')=4 OR json_extract(resume_context_json,'$.continuation.kind')='ordinary_lead')",
        (turn_id,),
    )
    return row[0] if row else None


def _cipher_scope(snapshot: OrdinaryLeadResumeContext) -> ReferenceScope:
    from bowhead_core.chat_reference_storage import ReferenceScope

    return ReferenceScope(
        team_id=snapshot.team_id, user_id=snapshot.user_id, workspace_id=snapshot.workspace_id, session_id=snapshot.session_id, policy_epoch=0
    )


async def bind_ordinary_capture_in_tx(db: aiosqlite.Connection, *, card: ClarificationRequest, capture: OrdinaryLeadCapture) -> dict:
    from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
    from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadCapture
    from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

    if type(capture) is not OrdinaryLeadCapture:
        raise OrdinaryContinuationUnavailable()
    source = json.loads(capture.checkpoint.request_json).get("datarc_selection")
    if source is not None:
        from bowhead_core.datarc_selection import DatarcSelectionSnapshot, require_datarc_context_migration

        DatarcSelectionSnapshot.model_validate(source)
        await require_datarc_context_migration(db)
    if not ordinary_multi_helper_question_enabled():
        checkpoint = capture.checkpoint
        extended = checkpoint.contract in {
            "ordinary-lead-loop.v5",
            "ordinary-lead-loop.v6",
            "ordinary-lead-loop.v7",
            "ordinary-lead-loop.v8",
            "ordinary-lead-loop.v10",
            "ordinary-lead-loop.v11",
            "ordinary-lead-loop.v12",
            "ordinary-lead-loop.v13",
        }
        if checkpoint.contract == "ordinary-lead-loop.v9":
            helpers = checkpoint.helper_materials()
            # Collaboration keeps the original single successful Team helper
            # boundary when the existing extended-continuation switch is off.
            source = await _row(db, "SELECT agent_source_kind FROM turn_agent_execution WHERE id=?", (helpers[0]["execution_id"],))
            extended = (
                len(helpers) != 1
                or source is None
                or source[0] != "team"
                or helpers[0]["outcome"]["status"] != "completed"
                or any(tool["kind"] in {"attachment_read", "read_result"} for tool in helpers[0]["tools"])
            )
        if extended:
            raise OrdinaryContinuationUnavailable(stage="clarification_capture")
    worker = capture.worker
    if worker.generation != 1 or (card.turn_id, card.user_id, card.workspace_id, card.session_id) != (
        worker.turn_id,
        worker.user_id,
        worker.workspace_id,
        worker.session_id,
    ):
        raise OrdinaryContinuationUnavailable()
    await require_live_worker_in_tx(db, worker=worker)
    if (
        await _row(db, "SELECT 1 FROM sqlite_master WHERE name='trg_ordinary_question_identity'") is None
        or await _row(db, "SELECT 1 FROM clarification_requests WHERE turn_id=?", (worker.turn_id,))
        or await _row(db, "SELECT 1 FROM document_jobs WHERE origin_turn_id=?", (worker.turn_id,))
    ):
        raise OrdinaryContinuationUnavailable()
    source = await _read_chat_turn_user_source_in_tx(
        db, turn_id=worker.turn_id, expected_user_id=worker.user_id, expected_workspace_id=worker.workspace_id
    )
    if source is None or source.session_id != worker.session_id or source.input_document is not None or source.document_revision_target is not None:
        raise OrdinaryContinuationUnavailable()
    validate_questions(capture.questions)
    payload = capture.checkpoint.encode()
    scope = ReferenceScope(**worker.model_dump(exclude={"turn_id", "generation", "owner_id"}), policy_epoch=0)
    cipher = ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", ""))
    sealed_content = payload
    if capture.checkpoint.storage_format is not None:
        from bowhead_core.continuation_payload import seal_payload, write_payload_in_tx

        sealed_content, segments = seal_payload(
            payload,
            kind="ordinary",
            turn_id=worker.turn_id,
            scope=scope,
            identity=card.request_id,
            purpose="ordinary-lead-question.v1",
            cipher=cipher,
        )
        await write_payload_in_tx(db, manifest=sealed_content, ciphertexts=segments, scope=scope, identity=card.request_id)
    ciphertext = cipher.seal(scope=scope, purpose="ordinary-lead-question.v1", identity=card.request_id, content=sealed_content)
    return OrdinaryLeadResumeContext.model_validate(
        {
            "__schema_version": 4,
            "team_id": worker.team_id,
            "user_id": worker.user_id,
            "workspace_id": worker.workspace_id,
            "session_id": worker.session_id,
            "turn_id": worker.turn_id,
            "user_text": source.user_text,
            "questions": capture.questions,
            "continuation": {
                "kind": "ordinary_lead",
                "contract": "ordinary-lead-question.v1",
                "card_id": card.request_id,
                "creation_generation": 1,
                "creation_owner_id": worker.owner_id,
                "source_hash": content_hash(chat_turn_user_source_projection(source)),
                "question_hash": content_hash(capture.questions),
                "checkpoint_hash": hashlib.sha256(payload).hexdigest(),
                "checkpoint_ciphertext": ciphertext,
            },
        }
    ).model_dump(by_alias=True)


def reopen_ordinary_checkpoint(record: OrdinaryContinuationCard, *, db_path=None) -> OrdinaryLeadCheckpoint:
    from bowhead_core.chat_reference_storage import ReferenceCipher
    from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadCheckpoint

    snapshot = record.snapshot
    continuation = snapshot.continuation
    try:
        cipher = ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", ""))
        payload = cipher.open(
            scope=_cipher_scope(snapshot),
            purpose="ordinary-lead-question.v1",
            identity=record.card.request_id,
            ciphertext=continuation.checkpoint_ciphertext,
        )
        from bowhead_core.continuation_payload import open_payload

        payload = open_payload(
            payload,
            db_path=db_path or record.source_db_path,
            kind="ordinary",
            turn_id=snapshot.turn_id,
            scope=_cipher_scope(snapshot),
            identity=record.card.request_id,
            purpose="ordinary-lead-question.v1",
            cipher=cipher,
        )
        if hashlib.sha256(payload).hexdigest() != continuation.checkpoint_hash:
            raise OrdinaryContinuationUnavailable()
        checkpoint = OrdinaryLeadCheckpoint.decode(payload)
        if checkpoint.document_routing is not None and checkpoint.document_routing.source_hash != continuation.source_hash:
            raise OrdinaryContinuationUnavailable()
        return checkpoint
    except ValueError as error:
        raise OrdinaryContinuationUnavailable() from error


def ordinary_resume_request_material(card: ClarificationRequest, *, db_path=None) -> tuple[OrdinaryLeadCheckpoint, dict]:
    """Decrypt server-authored routing; database/worker checks still precede use."""
    snapshot = decode_clarification_context(card)
    if not isinstance(snapshot, OrdinaryLeadResumeContext):
        raise OrdinaryContinuationUnavailable()
    checkpoint = reopen_ordinary_checkpoint(OrdinaryContinuationCard(card, snapshot, None, None), db_path=db_path)
    context = json.loads(checkpoint.request_json)
    if isinstance(context, dict) and context.get("datarc_selection") is not None:
        from bowhead_core.datarc_selection import DatarcSelectionSnapshot

        DatarcSelectionSnapshot.model_validate(context["datarc_selection"])
    if not isinstance(context, dict) or (
        context.get("user_id"),
        context.get("team_id"),
        context.get("workspace_id"),
        context.get("session_id"),
        context.get("user_text"),
    ) != (snapshot.user_id, snapshot.team_id, snapshot.workspace_id, snapshot.session_id, snapshot.user_text):
        raise OrdinaryContinuationUnavailable()
    return checkpoint, context


async def read_ordinary_card_in_tx(db: aiosqlite.Connection, *, card_id: str, document_job_id: str | None = None) -> OrdinaryContinuationCard:
    from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

    row = await _row(db, "SELECT * FROM clarification_requests WHERE request_id=?", (card_id,))
    if row is None:
        raise OrdinaryContinuationUnavailable()
    card = ClarificationStore._row_to_request(row)
    snapshot = decode_clarification_context(card)
    if not isinstance(snapshot, OrdinaryLeadResumeContext):
        raise OrdinaryContinuationUnavailable()
    validate_questions(snapshot.questions)
    if content_hash(snapshot.questions) != snapshot.continuation.question_hash:
        raise OrdinaryContinuationUnavailable()
    source = await _read_chat_turn_user_source_in_tx(db, turn_id=card.turn_id, expected_user_id=card.user_id, expected_workspace_id=card.workspace_id)
    scope = await _row(
        db,
        "SELECT 1 FROM sessions s JOIN workspaces w ON w.id=s.workspace_id WHERE s.id=? AND s.user_id=? "
        "AND s.workspace_id=? AND w.team_id=? AND s.deleted_at IS NULL",
        (card.session_id, card.user_id, card.workspace_id, snapshot.team_id),
    )
    if (
        scope is None
        or source is None
        or source.session_id != card.session_id
        or source.user_text != snapshot.user_text
        or source.input_document is not None
        or source.document_revision_target is not None
        or content_hash(chat_turn_user_source_projection(source)) != snapshot.continuation.source_hash
    ):
        raise OrdinaryContinuationUnavailable()
    async with db.execute("SELECT request_id FROM clarification_requests WHERE turn_id=? AND request_id<>?", (card.turn_id, card_id)) as cursor:
        others = [item[0] for item in await cursor.fetchall()]
    if others:
        from bowhead_core.document_clarification_lifecycle import read_document_card_in_tx

        if document_job_id is None or len(others) != 1:
            raise OrdinaryContinuationUnavailable()
        domain = await read_document_card_in_tx(db, card_id=others[0])
        if domain.snapshot.continuation.checkpoint.job_id != document_job_id:
            raise OrdinaryContinuationUnavailable()
    generation, owner = row["continuation_generation"], row["continuation_owner_id"]
    if (generation is None) != (owner is None) or (generation is None) != (card.injected_at is None):
        raise OrdinaryContinuationUnavailable()
    async with db.execute("PRAGMA database_list") as cursor:
        source_db_path = next((item[2] for item in await cursor.fetchall() if item[1] == "main"), None)
    return OrdinaryContinuationCard(card, snapshot, generation, owner, source_db_path)


async def require_live_worker_in_tx(db: aiosqlite.Connection, *, worker: DocumentTurnOwner) -> None:
    from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope

    worker = DocumentTurnOwner.model_validate(worker.model_dump())
    try:
        await require_reference_request_scope(db, ReferenceRequestScope(**worker.model_dump(exclude={"turn_id", "generation", "owner_id"})))
    except ValueError as error:
        raise OrdinaryContinuationUnavailable() from error
    await require_current_worker_in_tx(db, worker=worker)


async def require_current_worker_in_tx(db: aiosqlite.Connection, *, worker: DocumentTurnOwner) -> None:
    """Fence lifecycle writes without requiring permission to start new work.

    Terminal cleanup must remain possible after resource access is revoked.
    Callers admitting new work must use ``require_live_worker_in_tx`` instead.
    """
    worker = DocumentTurnOwner.model_validate(worker.model_dump())
    row = await _row(
        db,
        """SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id
        JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=s.id
        WHERE t.id=? AND s.id=? AND s.user_id=? AND s.workspace_id=? AND w.team_id=?
          AND s.deleted_at IS NULL AND s.active_turn_id=t.id AND t.status='running' AND COALESCE(t.terminal_protocol_version,0)=0
          AND t.execution_generation=? AND d.state='spawned' AND d.owner_id=?
          AND d.lease_execution_generation=t.execution_generation AND d.cancel_requested_at IS NULL
          AND julianday(d.lease_expires_at)>julianday('now')""",
        (worker.turn_id, worker.session_id, worker.user_id, worker.workspace_id, worker.team_id, worker.generation, worker.owner_id),
    )
    if row is None:
        raise OrdinaryContinuationUnavailable()


def _same_scope(record: OrdinaryContinuationCard, worker: DocumentTurnOwner) -> bool:
    return record.creation_worker().model_dump(exclude={"generation", "owner_id"}) == worker.model_dump(exclude={"generation", "owner_id"})


async def require_ordinary_pause_in_tx(db: aiosqlite.Connection, *, card_id: str, answered: bool = False) -> OrdinaryContinuationCard:
    record = await read_ordinary_card_in_tx(db, card_id=card_id)
    if record.card.status not in {"pending", "resolved"} or record.consumed_generation is not None:
        raise OrdinaryContinuationUnavailable()
    worker = record.creation_worker()
    row = await _row(
        db,
        """SELECT t.execution_generation,d.owner_id,d.lease_kind FROM turns t JOIN sessions s ON s.id=t.session_id
        JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=s.id
        WHERE t.id=? AND t.status='clarification_pending' AND s.deleted_at IS NULL
          AND (s.active_turn_id IS NULL OR s.active_turn_id=t.id) AND d.state IN ('terminal','failed')
          AND d.lease_execution_generation=t.execution_generation AND d.cancel_requested_at IS NULL""",
        (worker.turn_id,),
    )
    if row is None or row["execution_generation"] < worker.generation:
        raise OrdinaryContinuationUnavailable()
    if row["execution_generation"] == worker.generation:
        if row["owner_id"] not in (None, worker.owner_id):
            raise OrdinaryContinuationUnavailable()
    elif row["lease_kind"] != "resume" or row["owner_id"] is not None:
        raise OrdinaryContinuationUnavailable()
    if answered:
        typed_answer(record)
    return record


async def consume_ordinary_resume_in_tx(db: aiosqlite.Connection, *, worker: DocumentTurnOwner, card_id: str) -> OrdinaryContinuationCard:
    """Commit this before provider/Tool work; uncertain started work cannot retry."""
    record = await read_ordinary_card_in_tx(db, card_id=card_id)
    await require_live_worker_in_tx(db, worker=worker)
    typed_answer(record)
    if not _same_scope(record, worker) or worker.generation <= record.creation_worker().generation or record.consumed_generation is not None:
        raise OrdinaryContinuationUnavailable()
    dispatch = await _row(db, "SELECT lease_kind FROM chat_turn_spawn_dispatch WHERE turn_id=?", (worker.turn_id,))
    if dispatch[0] != "resume":
        raise OrdinaryContinuationUnavailable()
    cursor = await db.execute(
        "UPDATE clarification_requests SET injected_at=?,continuation_generation=?,continuation_owner_id=? "
        "WHERE request_id=? AND status='resolved' AND injected_at IS NULL AND continuation_generation IS NULL",
        (datetime.now(tz=UTC).isoformat(), worker.generation, worker.owner_id, card_id),
    )
    if cursor.rowcount != 1:
        raise OrdinaryContinuationUnavailable()
    return await read_ordinary_card_in_tx(db, card_id=card_id)


async def validate_ordinary_document_admission_in_tx(
    db: aiosqlite.Connection, *, worker: DocumentTurnOwner, source_hash: str
) -> OrdinaryQuestionAnswer | None:
    card_id = await ordinary_card_id_in_tx(db, turn_id=worker.turn_id)
    if card_id is None:
        return None
    record = await read_ordinary_card_in_tx(db, card_id=card_id)
    await require_live_worker_in_tx(db, worker=worker)
    if (
        not _same_scope(record, worker)
        or (record.consumed_generation, record.consumed_owner_id) != (worker.generation, worker.owner_id)
        or record.snapshot.continuation.source_hash != source_hash
    ):
        raise OrdinaryContinuationUnavailable()
    return typed_answer(record)


async def read_ordinary_document_answer_in_tx(
    db: aiosqlite.Connection, *, creation_worker: DocumentTurnOwner, source_hash: str, job_id: str
) -> OrdinaryQuestionAnswer | None:
    card_id = await ordinary_card_id_in_tx(db, turn_id=creation_worker.turn_id)
    if card_id is None:
        return None
    record = await read_ordinary_card_in_tx(db, card_id=card_id, document_job_id=job_id)
    job = await _row(
        db,
        """SELECT 1 FROM document_jobs j JOIN document_turn_dispatches d ON d.job_id=j.id
        WHERE j.id=? AND j.team_id=? AND j.requested_by=? AND j.workspace_id=? AND j.session_id=? AND j.origin_turn_id=?
          AND j.user_source_sha256=? AND d.source_hash=j.user_source_sha256 AND d.creation_generation=? AND d.owner_id=?""",
        (
            job_id,
            creation_worker.team_id,
            creation_worker.user_id,
            creation_worker.workspace_id,
            creation_worker.session_id,
            creation_worker.turn_id,
            source_hash,
            creation_worker.generation,
            creation_worker.owner_id,
        ),
    )
    if (
        job is None
        or not _same_scope(record, creation_worker)
        or (record.consumed_generation, record.consumed_owner_id) != (creation_worker.generation, creation_worker.owner_id)
        or source_hash != record.snapshot.continuation.source_hash
    ):
        raise OrdinaryContinuationUnavailable()
    return typed_answer(record)
