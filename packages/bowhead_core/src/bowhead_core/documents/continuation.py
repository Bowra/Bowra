"""Exact retained planning material for the shared clarification lifecycle.

These transaction helpers do not schedule work, create cards or commit. The
shared Turn owner remains responsible for generation claims and answer intake.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

import aiosqlite
from bowhead_router.invocation import PhysicalInvocationSpec
from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, ValidationError, model_serializer

from bowhead_core.chat_turn_source import chat_turn_user_source_projection
from bowhead_core.document_chat_continuation import Digest, DocumentPlanningAttemptRef, DocumentPlanningCheckpointRef, DocumentTurnOwner
from bowhead_core.documents.delivery import _execution, _now, _scope
from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.pptx_plan import PlannerProfile, planning_system_prompt
from bowhead_core.documents.request import DocumentRequestError, DocumentScope
from bowhead_core.documents.store import _digest, _json
from bowhead_core.language_context import LanguageContext
from bowhead_core.rate_card import RATE_CARD_VERSION
from bowhead_core.session_store import _read_chat_turn_user_source_in_tx


class DocumentLanguageSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    ui_language: Literal["en-US", "zh-CN", "de-DE", "fr-FR"]
    output_language: Literal["en-US", "zh-CN", "de-DE", "fr-FR"]
    data_language: Literal["en-US", "zh-CN"]
    week_start_day: Literal["monday", "sunday"]
    # Viewer zone admitted with the turn (LanguageContext.time_zone); "" = none.
    time_zone: str = Field(default="", max_length=64)

    @model_serializer(mode="wrap")
    def _omit_empty_time_zone(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        # Snapshots without a viewer zone keep their original shape, so stored
        # runtime_snapshot_digest values computed before this field still match.
        data = handler(self)
        if not data.get("time_zone"):
            data.pop("time_zone", None)
        return data

    def language_context(self) -> LanguageContext:
        return LanguageContext.from_dict(self.model_dump())


class DocumentRuntimeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model_config_version: int = Field(ge=0)
    planner_profile: PlannerProfile
    language: DocumentLanguageSnapshot


@dataclass(frozen=True)
class DocumentCheckpointMaterial:
    checkpoint: DocumentPlanningCheckpointRef
    creation_worker: DocumentTurnOwner
    runtime_snapshot: DocumentRuntimeSnapshot
    runtime_snapshot_digest: str
    question: str
    question_digest: str
    provider: str
    model: str


@dataclass(frozen=True)
class DocumentPlanningPause:
    job_id: str
    checkpoint: DocumentPlanningCheckpointRef
    question: str
    kind: Literal["pause"] = field(default="pause", init=False)


@dataclass(frozen=True)
class DocumentPlanningReview:
    job_id: str
    draft_digest: str
    kind: Literal["review"] = field(default="review", init=False)


@dataclass(frozen=True)
class DocumentPlanningFailure:
    kind: Literal["failed", "incomplete"]
    job_id: str
    error_code: str


DocumentPlanningOutcome = DocumentPlanningPause | DocumentPlanningReview | DocumentPlanningFailure


class _CheckpointPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    job_id: str
    attempt_no: Literal[1]
    source_hash: Digest
    runtime_snapshot_digest: Digest
    inputs_digest: Digest
    context_digest: Digest
    response: str = Field(min_length=1, max_length=64_000)
    response_digest: Digest
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    last_call_no: int = Field(ge=1, le=2)
    question: str = Field(min_length=1, max_length=200)
    question_digest: Digest


@dataclass(frozen=True)
class DocumentCallRef:
    job_id: str
    attempt_no: int
    call_no: int
    worker: DocumentTurnOwner


_DOCUMENT_CALL: ContextVar[DocumentCallRef | None] = ContextVar("document_planning_call", default=None)


def current_document_call() -> DocumentCallRef | None:
    return _DOCUMENT_CALL.get()


@contextmanager
def document_call_scope(call: DocumentCallRef):
    if type(call.attempt_no) is not int or call.attempt_no not in (1, 2) or type(call.call_no) is not int or call.call_no not in (1, 2):
        raise DocumentRequestError("document_execution_fenced")
    token = _DOCUMENT_CALL.set(call)
    try:
        yield
    finally:
        _DOCUMENT_CALL.reset(token)


async def _one(connection, sql, params=()):
    if not connection.in_transaction:
        raise DocumentRequestError("document_execution_fenced")
    connection.row_factory = aiosqlite.Row
    async with connection.execute(sql, params) as cursor:
        return await cursor.fetchone()


async def validate_document_worker_in_tx(connection, *, worker: DocumentTurnOwner, job_id: str):
    """Recheck the actual active Turn lease and current write authorization."""
    worker = DocumentTurnOwner.model_validate(worker.model_dump())
    job = await _scope(
        connection, DocumentScope(team_id=worker.team_id, workspace_id=worker.workspace_id, user_id=worker.user_id), job_id, write=True
    )
    row = await _one(
        connection,
        """SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id
      JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id WHERE t.id=? AND s.id=? AND s.active_turn_id=t.id
        AND t.status='running' AND t.execution_generation=? AND d.session_id=s.id AND d.state='spawned'
        AND d.owner_id=? AND d.lease_execution_generation=t.execution_generation AND d.lease_kind IN ('turn','resume')
        AND d.cancel_requested_at IS NULL AND julianday(d.lease_expires_at)>julianday('now')""",
        (worker.turn_id, worker.session_id, worker.generation, worker.owner_id),
    )
    if job["status"] != "queued" or job["origin_turn_id"] != worker.turn_id or job["session_id"] != worker.session_id or (row is None):
        raise DocumentRequestError("document_execution_fenced")
    return job


async def register_document_turn_in_tx(connection, *, worker: DocumentTurnOwner, job_id: str, runtime: DocumentRuntimeSnapshot) -> None:
    """Called only inside the original job admission transaction by its writer."""
    from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable, validate_ordinary_document_admission_in_tx

    job = await validate_document_worker_in_tx(connection, worker=worker, job_id=job_id)
    runtime = DocumentRuntimeSnapshot.model_validate(runtime.model_dump())
    source = await _read_chat_turn_user_source_in_tx(
        connection, turn_id=worker.turn_id, expected_user_id=worker.user_id, expected_workspace_id=worker.workspace_id
    )
    if source is None or _digest(_json(chat_turn_user_source_projection(source))) != job["user_source_sha256"]:
        raise DocumentRequestError("document_source_unavailable")
    try:
        await validate_ordinary_document_admission_in_tx(connection, worker=worker, source_hash=job["user_source_sha256"])
    except OrdinaryContinuationUnavailable as exc:
        # A saved answer alone cannot authorize a newly admitted native job.
        raise DocumentRequestError("document_history_unavailable") from exc
    payload = _json(runtime.model_dump())
    await connection.execute(
        """INSERT INTO document_turn_dispatches
      (job_id,creation_generation,owner_id,source_hash,runtime_snapshot_json,runtime_snapshot_digest,created_at)
      VALUES (?,?,?,?,?,?,?)""",
        (job_id, worker.generation, worker.owner_id, job["user_source_sha256"], payload, _digest(payload), _now()),
    )


async def _input_digest(connection, job_id: str) -> str:
    frozen = await _one(connection, "SELECT * FROM document_input_sets WHERE job_id=? AND frozen_at IS NOT NULL", (job_id,))
    async with connection.execute(
        """SELECT c.*,a.status AS artifact_status,a.deleted_at,a.path AS artifact_path,a.size AS artifact_size,
      a.created_by,a.workspace_id,r.released_at,p.artifact_id AS purging FROM document_input_copies c
      JOIN document_jobs j ON j.id=c.job_id JOIN artifacts a ON a.id=c.artifact_id
      JOIN document_artifact_refs r ON r.artifact_id=c.artifact_id AND r.owner_id=c.job_id
      LEFT JOIN artifact_purge_claims p ON p.artifact_id=c.artifact_id
      WHERE c.job_id=? AND a.created_by=j.requested_by AND a.workspace_id=j.workspace_id ORDER BY c.input_index""",
        (job_id,),
    ) as cursor:
        copies = list(await cursor.fetchall())
    expected = await _one(connection, "SELECT count(*) FROM document_input_copies WHERE job_id=?", (job_id,))
    if (
        frozen is None
        or not copies
        or len(copies) != expected[0]
        or len(copies) != frozen["input_count"]
        or any(
            row["verified_at"] is None
            or row["cleanup_claimed_at"] is not None
            or row["released_at"] is not None
            or row["deleted_at"] is not None
            or row["purging"] is not None
            or row["artifact_status"] != "ready"
            or row["artifact_path"] != row["path"]
            or row["artifact_size"] != row["size_bytes"]
            for row in copies
        )
    ):
        raise DocumentRequestError("document_input_unavailable")
    return _digest(
        _json(
            [
                {
                    key: row[key]
                    for key in ("input_index", "artifact_id", "source_artifact_id", "role", "filename", "mime", "path", "sha256", "size_bytes")
                }
                for row in copies
            ]
        )
    )


async def begin_document_planning_in_tx(connection, *, worker: DocumentTurnOwner, job_id: str, instruction: str) -> None:
    """Reserve attempt one after input freezing, before inventory or any call."""
    await validate_document_worker_in_tx(connection, worker=worker, job_id=job_id)
    dispatch = await _one(connection, "SELECT * FROM document_turn_dispatches WHERE job_id=?", (job_id,))
    if dispatch is None or (dispatch["creation_generation"], dispatch["owner_id"]) != (worker.generation, worker.owner_id):
        raise DocumentRequestError("document_execution_fenced")
    digest = await _input_digest(connection, job_id)
    now = _now()
    await connection.execute(
        """INSERT INTO document_executions(job_id,state,owner,lease_until,rate_card_version,created_at,updated_at)
      VALUES (?,'preparing',?,?,?,?,?)""",
        (job_id, worker.owner_id, time.time() + 600, RATE_CARD_VERSION, now, now),
    )
    await connection.execute(
        """INSERT INTO document_planning_attempts(job_id,attempt_no,creation_generation,owner_id,instruction,inputs_digest,created_at)
      VALUES (?,1,?,?,?,?,?)""",
        (job_id, worker.generation, worker.owner_id, instruction, digest, now),
    )


def _validated_context(value, instruction: str, *, profile: str) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) != {"payload", "digest"}
        or not isinstance(value["payload"], str)
        or len(value["payload"].encode()) > 64_000
    ):
        raise DocumentRequestError("document_checkpoint_unavailable")
    try:
        if _digest(planning_system_prompt(profile) + "\n" + value["payload"]) != value["digest"]:
            raise DocumentRequestError("document_checkpoint_unavailable")
        payload = json.loads(value["payload"])
    except (ValueError, TypeError, OfficePackageError) as exc:
        raise DocumentRequestError("document_checkpoint_unavailable") from exc
    if not isinstance(payload, dict) or payload.get("profile") != profile or payload.get("instruction") != instruction:
        raise DocumentRequestError("document_checkpoint_unavailable")
    return value


async def retain_document_context_in_tx(connection, *, worker: DocumentTurnOwner, job_id: str, attempt_no: int, context: dict) -> None:
    await validate_document_worker_in_tx(connection, worker=worker, job_id=job_id)
    attempt = await _one(connection, "SELECT * FROM document_planning_attempts WHERE job_id=? AND attempt_no=?", (job_id, attempt_no))
    if attempt is None or (attempt["owner_id"], attempt["creation_generation"]) != (worker.owner_id, worker.generation):
        raise DocumentRequestError("document_execution_fenced")
    dispatch = await _one(connection, "SELECT creation_generation,owner_id FROM document_turn_dispatches WHERE job_id=?", (job_id,))
    if dispatch is None:
        raise DocumentRequestError("document_checkpoint_unavailable")
    creation = worker.model_copy(update={"generation": dispatch["creation_generation"], "owner_id": dispatch["owner_id"]})
    _, runtime = await _dispatch_material(connection, job_id=job_id, creation_worker=creation)
    _validated_context(context, attempt["instruction"], profile=runtime.planner_profile)
    template = await _one(connection, "SELECT sha256 FROM document_input_copies WHERE job_id=? AND role='template'", (job_id,))
    if template is None or json.loads(context["payload"]).get("template_sha256") != template[0]:
        raise DocumentRequestError("document_checkpoint_unavailable")
    await connection.execute(
        "UPDATE document_planning_attempts SET context_json=? WHERE job_id=? AND attempt_no=?", (_json(context), job_id, attempt_no)
    )


async def _dispatch_material(connection, *, job_id: str, creation_worker: DocumentTurnOwner):
    creation_worker = DocumentTurnOwner.model_validate(creation_worker.model_dump())
    scope = DocumentScope(team_id=creation_worker.team_id, workspace_id=creation_worker.workspace_id, user_id=creation_worker.user_id)
    job = await _scope(connection, scope, job_id, write=True)
    dispatch = await _one(connection, "SELECT * FROM document_turn_dispatches WHERE job_id=?", (job_id,))
    if (
        dispatch is None
        or job["status"] != "queued"
        or (job["session_id"], job["origin_turn_id"], dispatch["creation_generation"], dispatch["owner_id"])
        != (creation_worker.session_id, creation_worker.turn_id, creation_worker.generation, creation_worker.owner_id)
    ):
        raise DocumentRequestError("document_checkpoint_unavailable")
    source = await _read_chat_turn_user_source_in_tx(
        connection, turn_id=creation_worker.turn_id, expected_user_id=creation_worker.user_id, expected_workspace_id=creation_worker.workspace_id
    )
    if (
        source is None
        or _digest(_json(chat_turn_user_source_projection(source))) != dispatch["source_hash"]
        or dispatch["source_hash"] != job["user_source_sha256"]
    ):
        raise DocumentRequestError("document_source_unavailable")
    try:
        runtime = DocumentRuntimeSnapshot.model_validate_json(dispatch["runtime_snapshot_json"])
        if _digest(_json(runtime.model_dump())) != dispatch["runtime_snapshot_digest"]:
            raise ValueError("Runtime digest changed")
    except (ValueError, ValidationError) as exc:
        raise DocumentRequestError("document_checkpoint_unavailable") from exc
    admitted = await _one(connection, "SELECT image_id,model_config_version FROM document_dispatches WHERE job_id=?", (job_id,))
    if admitted is None or (admitted["image_id"], admitted["model_config_version"]) != (runtime.image_id, runtime.model_config_version):
        raise DocumentRequestError("document_checkpoint_unavailable")
    return dispatch, runtime


async def retain_document_question_in_tx(
    connection, *, worker: DocumentTurnOwner, job_id: str, question: str, last_call_no: int
) -> DocumentPlanningCheckpointRef:
    await validate_document_worker_in_tx(connection, worker=worker, job_id=job_id)
    dispatch, runtime = await _dispatch_material(connection, job_id=job_id, creation_worker=worker)
    attempt = await _one(connection, "SELECT * FROM document_planning_attempts WHERE job_id=? AND attempt_no=1", (job_id,))
    execution = await _execution(connection, job_id)
    call = await _one(
        connection, "SELECT * FROM document_model_calls WHERE job_id=? AND attempt_no=1 AND call_no=? AND state='completed'", (job_id, last_call_no)
    )
    if (
        type(last_call_no) is not int
        or last_call_no not in (1, 2)
        or not isinstance(question, str)
        or question != question.strip()
        or not 1 <= len(question) <= 200
        or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in question)
    ):
        raise DocumentRequestError("planning_invalid_question")
    if (
        attempt is None
        or attempt["context_json"] is None
        or execution is None
        or execution["state"] != "rendering"
        or not execution["response"]
        or call is None
    ):
        raise DocumentRequestError("document_checkpoint_unavailable")
    try:
        proposal = json.loads(execution["response"])
    except ValueError as exc:
        raise DocumentRequestError("document_checkpoint_unavailable") from exc
    if not isinstance(proposal, dict) or proposal.get("action") != "ask" or proposal.get("question") != question:
        raise DocumentRequestError("document_checkpoint_unavailable")
    await _known_observations_in_tx(connection, job_id)
    context = _validated_context(json.loads(attempt["context_json"]), attempt["instruction"], profile=runtime.planner_profile)
    if _json(context) != execution["context_json"] or attempt["inputs_digest"] != await _input_digest(connection, job_id):
        raise DocumentRequestError("document_checkpoint_unavailable")
    payload = {
        "schema_version": 1,
        "job_id": job_id,
        "attempt_no": 1,
        "source_hash": dispatch["source_hash"],
        "runtime_snapshot_digest": dispatch["runtime_snapshot_digest"],
        "inputs_digest": attempt["inputs_digest"],
        "context_digest": context["digest"],
        "response": execution["response"],
        "response_digest": call["response_digest"],
        "provider": call["provider"],
        "model": call["model"],
        "last_call_no": last_call_no,
        "question": question,
        "question_digest": _digest(question),
    }
    digest = _digest(_json(payload))
    await connection.execute(
        "UPDATE document_planning_attempts SET checkpoint_json=?,checkpoint_digest=? WHERE job_id=? AND attempt_no=1",
        (_json(payload), digest, job_id),
    )
    await connection.execute(
        "UPDATE document_executions SET state='question',question=?,lease_until=0,updated_at=? WHERE job_id=? AND owner=?",
        (question, _now(), job_id, worker.owner_id),
    )
    return DocumentPlanningCheckpointRef(job_id=job_id, attempt_no=1, checkpoint_digest=digest, source_hash=dispatch["source_hash"])


async def load_document_checkpoint_in_tx(
    connection, *, checkpoint: DocumentPlanningCheckpointRef, creation_worker: DocumentTurnOwner
) -> DocumentCheckpointMaterial:
    checkpoint = DocumentPlanningCheckpointRef.model_validate(checkpoint.model_dump())
    dispatch, runtime = await _dispatch_material(connection, job_id=checkpoint.job_id, creation_worker=creation_worker)
    attempt = await _one(
        connection, "SELECT * FROM document_planning_attempts WHERE job_id=? AND attempt_no=?", (checkpoint.job_id, checkpoint.attempt_no)
    )
    if (
        checkpoint.attempt_no != 1
        or attempt is None
        or attempt["checkpoint_digest"] != checkpoint.checkpoint_digest
        or dispatch["source_hash"] != checkpoint.source_hash
    ):
        raise DocumentRequestError("document_checkpoint_unavailable")
    try:
        payload = _CheckpointPayload.model_validate_json(attempt["checkpoint_json"]).model_dump()
        context = _validated_context(json.loads(attempt["context_json"]), attempt["instruction"], profile=runtime.planner_profile)
    except (ValueError, TypeError) as exc:
        raise DocumentRequestError("document_checkpoint_unavailable") from exc
    if _digest(_json(payload)) != checkpoint.checkpoint_digest:
        raise DocumentRequestError("document_checkpoint_unavailable")
    if (
        payload["schema_version"] != 1
        or payload["job_id"] != checkpoint.job_id
        or payload["attempt_no"] != 1
        or payload["source_hash"] != checkpoint.source_hash
        or payload["runtime_snapshot_digest"] != dispatch["runtime_snapshot_digest"]
        or payload["inputs_digest"] != attempt["inputs_digest"]
        or payload["inputs_digest"] != await _input_digest(connection, checkpoint.job_id)
        or payload["context_digest"] != context["digest"]
        or not isinstance(payload["question"], str)
        or not 1 <= len(payload["question"]) <= 200
        or payload["question_digest"] != _digest(payload["question"])
    ):
        raise DocumentRequestError("document_checkpoint_unavailable")
    call = await _one(
        connection, "SELECT * FROM document_model_calls WHERE job_id=? AND attempt_no=1 AND call_no=?", (checkpoint.job_id, payload["last_call_no"])
    )
    if call is None or (call["state"], call["provider"], call["model"], call["response_digest"]) != (
        "completed",
        payload["provider"],
        payload["model"],
        payload["response_digest"],
    ):
        raise DocumentRequestError("document_checkpoint_unavailable")
    return DocumentCheckpointMaterial(
        checkpoint,
        creation_worker,
        runtime,
        dispatch["runtime_snapshot_digest"],
        payload["question"],
        payload["question_digest"],
        payload["provider"],
        payload["model"],
    )


async def _known_observations_in_tx(connection, job_id: str) -> None:
    uncertain = await _one(
        connection,
        """SELECT 1 FROM document_physical_receipt_links l
      JOIN llm_usage_events u ON u.id=l.receipt_id LEFT JOIN document_model_calls c
      ON c.job_id=l.job_id AND c.attempt_no=l.attempt_no AND c.call_no=l.call_no
      WHERE l.job_id=? AND (c.job_id IS NULL OR c.state<>'completed' OR u.receipt_state<>'completed'
        OR u.response_digest IS NOT c.response_digest OR u.tokens_input IS NOT c.tokens_input
        OR u.tokens_output IS NOT c.tokens_output OR u.tokens_cached IS NOT c.tokens_cached)
      UNION ALL SELECT 1 FROM document_model_calls c LEFT JOIN document_physical_receipt_links l
      ON l.job_id=c.job_id AND l.attempt_no=c.attempt_no AND l.call_no=c.call_no
      WHERE c.job_id=? AND l.receipt_id IS NULL""",
        (job_id, job_id),
    )
    if uncertain is not None:
        raise DocumentRequestError("document_usage_unresolved")


async def _pause_ready(connection, material: DocumentCheckpointMaterial) -> None:
    job_id = material.checkpoint.job_id
    await _known_observations_in_tx(connection, job_id)
    execution = await _execution(connection, job_id)
    unsettled = await _one(
        connection,
        """SELECT 1 FROM document_model_calls c LEFT JOIN document_usage_receipts r
      ON r.job_id=c.job_id AND r.attempt_no=c.attempt_no AND r.call_no=c.call_no
      WHERE c.job_id=? AND (c.state<>'completed' OR r.settled_at IS NULL)""",
        (job_id,),
    )
    candidate = await _one(connection, "SELECT 1 FROM document_draft_files WHERE job_id=?", (job_id,))
    if (
        execution is None
        or execution["state"] != "question"
        or execution["owner"] != material.creation_worker.owner_id
        or execution["question"] != material.question
        or execution["lease_until"] != 0
        or unsettled is not None
        or candidate is not None
        or await document_resume_started_in_tx(connection, checkpoint=material.checkpoint)
    ):
        raise DocumentRequestError("document_checkpoint_unavailable")


async def validate_document_pause_in_tx(
    connection, *, worker: DocumentTurnOwner, checkpoint: DocumentPlanningCheckpointRef
) -> DocumentCheckpointMaterial:
    await validate_document_worker_in_tx(connection, worker=worker, job_id=checkpoint.job_id)
    material = await load_document_checkpoint_in_tx(connection, checkpoint=checkpoint, creation_worker=worker)
    await _pause_ready(connection, material)
    return material


async def validate_document_resume_in_tx(
    connection, *, checkpoint: DocumentPlanningCheckpointRef, creation_worker: DocumentTurnOwner
) -> DocumentCheckpointMaterial:
    material = await load_document_checkpoint_in_tx(connection, checkpoint=checkpoint, creation_worker=creation_worker)
    await _pause_ready(connection, material)
    row = await _one(
        connection,
        """SELECT 1 FROM turns t JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id
      WHERE t.id=? AND t.session_id=? AND d.session_id=t.session_id
        AND t.status='clarification_pending' AND d.state IN ('terminal','failed')
        AND d.lease_execution_generation=t.execution_generation AND d.cancel_requested_at IS NULL""",
        (creation_worker.turn_id, creation_worker.session_id),
    )
    if row is None:
        raise DocumentRequestError("document_checkpoint_unavailable")
    current = await _one(connection, "SELECT llm_config_version FROM teams WHERE id=?", (creation_worker.team_id,))
    if current is None or current[0] != material.runtime_snapshot.model_config_version:
        raise DocumentRequestError("document_model_configuration_changed")
    return material


async def document_resume_started_in_tx(connection, *, checkpoint: DocumentPlanningCheckpointRef) -> bool:
    return await _one(connection, "SELECT 1 FROM document_planning_attempts WHERE job_id=? AND attempt_no=2", (checkpoint.job_id,)) is not None


async def invalidate_document_pause_in_tx(connection, *, card_id: str, turn_id: str, now: str) -> bool:
    """End only the original retained pause during shared lifecycle recovery.

    The caller decides that continuation is unavailable. This content-free
    cleanup must also work after revocation; it grants no access or
    execution authority. Preserve immutable checkpoints, files and receipts.
    """
    if not connection.in_transaction:
        raise DocumentRequestError("document_execution_fenced")
    cursor = await connection.execute(
        """UPDATE document_executions SET state='cancelled',lease_until=0,
      error_code='clarification_resume_unavailable',updated_at=?
      WHERE state IN ('question','preparing') AND draft_digest IS NULL AND EXISTS(
        SELECT 1 FROM clarification_document_continuations c
        JOIN clarification_requests q ON q.request_id=c.card_id
        JOIN document_jobs j ON j.id=c.job_id
        JOIN document_turn_dispatches d ON d.job_id=j.id
        JOIN document_planning_attempts a ON a.job_id=j.id AND a.attempt_no=c.attempt_no
        JOIN turns t ON t.id=c.turn_id AND t.id=j.origin_turn_id AND t.session_id=j.session_id
        JOIN chat_turn_spawn_dispatch w ON w.turn_id=t.id AND w.session_id=j.session_id
        WHERE c.card_id=? AND c.turn_id=? AND c.job_id=document_executions.job_id
          AND c.attempt_no=1 AND document_executions.owner=c.creation_owner_id
          AND c.reserved_generation IS NULL AND c.consumed_generation IS NULL AND c.consumed_owner_id IS NULL
          AND q.turn_id=t.id AND q.session_id=j.session_id AND q.user_id=j.requested_by AND q.workspace_id=j.workspace_id
          AND d.owner_id=c.creation_owner_id AND d.creation_generation=c.creation_generation
          AND d.source_hash=c.source_hash AND j.user_source_sha256=c.source_hash
          AND d.runtime_snapshot_digest=c.runtime_snapshot_digest
          AND a.owner_id=c.creation_owner_id AND a.creation_generation=c.creation_generation AND a.checkpoint_digest=c.checkpoint_digest
          AND NOT EXISTS(SELECT 1 FROM document_planning_attempts p WHERE p.job_id=j.id AND p.attempt_no=2)
          AND NOT EXISTS(SELECT 1 FROM document_confirmations f WHERE f.job_id=j.id)
          AND (w.state IN ('terminal','failed','drain_pending') OR
            w.state='spawned' AND w.owner_id=c.creation_owner_id AND w.lease_execution_generation=c.creation_generation
            AND t.execution_generation=c.creation_generation)
          AND (t.status IN ('cancelled','failed','aborted') OR
            t.status='clarification_pending' AND w.state IN ('terminal','failed','drain_pending') OR
            t.status IN ('running','clarification_pending') AND w.state='spawned' AND julianday(w.lease_expires_at)<=julianday(?)))""",
        (now, card_id, turn_id, now),
    )
    return cursor.rowcount == 1


async def consume_document_attempt_in_tx(
    connection, *, worker: DocumentTurnOwner, checkpoint: DocumentPlanningCheckpointRef, card_id: str, answer_text: str
) -> DocumentPlanningAttemptRef:
    """Prepare attempt two beside the shared reservation's atomic consumption."""
    from bowhead_core.clarification_resume_context import DocumentResumeContext, decode_resume_context
    from bowhead_core.documents.planning_resume import prepare_answer_context
    from bowhead_core.documents.pptx_plan import PlanningContext

    await validate_document_worker_in_tx(connection, worker=worker, job_id=checkpoint.job_id)
    association = await _one(connection, "SELECT * FROM clarification_document_continuations WHERE card_id=?", (card_id,))
    card = await _one(connection, "SELECT * FROM clarification_requests WHERE request_id=?", (card_id,))
    if association is None or card is None or card["status"] != "resolved":
        raise DocumentRequestError("document_checkpoint_unavailable")
    try:
        snapshot = decode_resume_context(json.loads(card["resume_context_json"]))
        answers = json.loads(card["answers_json"])
    except (ValueError, TypeError) as exc:
        raise DocumentRequestError("document_checkpoint_unavailable") from exc
    if not isinstance(snapshot, DocumentResumeContext) or snapshot.continuation.checkpoint != checkpoint:
        raise DocumentRequestError("document_checkpoint_unavailable")
    creation = DocumentTurnOwner(
        team_id=snapshot.team_id,
        user_id=snapshot.user_id,
        workspace_id=snapshot.workspace_id,
        session_id=snapshot.session_id,
        turn_id=snapshot.turn_id,
        generation=snapshot.continuation.creation_generation,
        owner_id=snapshot.continuation.creation_owner_id,
    )
    if (
        worker.model_dump(exclude={"generation", "owner_id"}) != creation.model_dump(exclude={"generation", "owner_id"})
        or worker.generation <= creation.generation
        or association["reserved_generation"] != worker.generation
        or association["consumed_generation"] is not None
        or association["consumed_owner_id"] is not None
        or (
            association["job_id"],
            association["turn_id"],
            association["attempt_no"],
            association["creation_generation"],
            association["creation_owner_id"],
        )
        != (checkpoint.job_id, worker.turn_id, 1, creation.generation, creation.owner_id)
        or (association["checkpoint_digest"], association["source_hash"], association["runtime_snapshot_digest"])
        != (checkpoint.checkpoint_digest, checkpoint.source_hash, snapshot.continuation.runtime_snapshot_digest)
        or (card["turn_id"], card["session_id"], card["user_id"], card["workspace_id"], card["kind"], card["token"])
        != (worker.turn_id, worker.session_id, worker.user_id, worker.workspace_id, "ambiguous_turn", "")
    ):
        raise DocumentRequestError("document_checkpoint_unavailable")
    material = await load_document_checkpoint_in_tx(connection, checkpoint=checkpoint, creation_worker=creation)
    await _pause_ready(connection, material)
    if material.question != card["rationale"] or material.runtime_snapshot_digest != association["runtime_snapshot_digest"]:
        raise DocumentRequestError("document_checkpoint_unavailable")
    expected_answer = {"question": material.question, "source": "custom", "answer": answer_text, "skipped": False, "option_index": None}
    if answers != [expected_answer] or not isinstance(answer_text, str) or not answer_text.strip() or answer_text != answer_text.strip():
        raise DocumentRequestError("document_answer_unavailable")
    current = await _one(connection, "SELECT llm_config_version FROM teams WHERE id=?", (worker.team_id,))
    if current is None or current[0] != material.runtime_snapshot.model_config_version:
        raise DocumentRequestError("document_model_configuration_changed")
    previous = await _one(connection, "SELECT * FROM document_planning_attempts WHERE job_id=? AND attempt_no=1", (checkpoint.job_id,))
    count = await _one(connection, "SELECT count(*) FROM document_input_copies WHERE job_id=? AND role<>'template'", (checkpoint.job_id,))
    from bowhead_core.documents.planning_instruction import document_instruction_in_tx

    initial = await document_instruction_in_tx(connection, worker=creation, job_id=checkpoint.job_id)
    if initial.text != previous["instruction"]:
        raise DocumentRequestError("document_checkpoint_unavailable")
    resumed = prepare_answer_context(
        PlanningContext(**json.loads(previous["context_json"])),
        question=material.question,
        answer=answer_text,
        external_input_count=count[0] + int(initial.history is not None),
    )
    await connection.execute(
        """INSERT INTO document_planning_attempts
      (job_id,attempt_no,creation_generation,owner_id,card_id,instruction,inputs_digest,created_at)
      VALUES (?,2,?,?,?,?,?,?)""",
        (checkpoint.job_id, worker.generation, worker.owner_id, card_id, resumed.instruction, previous["inputs_digest"], _now()),
    )
    await connection.execute(
        """UPDATE document_executions SET state='planning',owner=?,lease_until=?,context_json=?,response=NULL,
      question=NULL,error_code=NULL,updated_at=? WHERE job_id=? AND state='question' AND owner=?""",
        (
            worker.owner_id,
            time.time() + 600,
            _json({"payload": resumed.context.payload, "digest": resumed.context.digest}),
            _now(),
            checkpoint.job_id,
            creation.owner_id,
        ),
    )
    await retain_document_context_in_tx(
        connection,
        worker=worker,
        job_id=checkpoint.job_id,
        attempt_no=2,
        context={"payload": resumed.context.payload, "digest": resumed.context.digest},
    )
    return DocumentPlanningAttemptRef(job_id=checkpoint.job_id, attempt_no=2, worker=worker)


async def link_physical_receipt_in_tx(connection, *, call: DocumentCallRef, spec: PhysicalInvocationSpec, receipt_id: str) -> None:
    if type(call.attempt_no) is not int or call.attempt_no not in (1, 2) or type(call.call_no) is not int or call.call_no not in (1, 2):
        raise DocumentRequestError("document_execution_fenced")
    await validate_document_worker_in_tx(connection, worker=call.worker, job_id=call.job_id)
    attempt = await _one(connection, "SELECT * FROM document_planning_attempts WHERE job_id=? AND attempt_no=?", (call.job_id, call.attempt_no))
    receipt = await _one(connection, "SELECT * FROM llm_usage_events WHERE id=?", (receipt_id,))
    if attempt is None or (attempt["owner_id"], attempt["creation_generation"]) != (call.worker.owner_id, call.worker.generation) or receipt is None:
        raise DocumentRequestError("document_execution_fenced")
    if (
        receipt["provider"] != spec.provider
        or receipt["model"] != spec.model
        or json.loads(receipt["receipt_json"]).get("request_digest") != spec.request_digest
    ):
        raise DocumentRequestError("document_execution_fenced")
    await connection.execute(
        "INSERT INTO document_physical_receipt_links(job_id,attempt_no,call_no,receipt_id) VALUES (?,?,?,?)",
        (call.job_id, call.attempt_no, call.call_no, receipt_id),
    )
