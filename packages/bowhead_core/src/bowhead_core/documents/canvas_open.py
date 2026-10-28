"""Trusted native review pointers, separate from document content and publication.

The caller owns the transaction and generation-fenced event delivery. A Tool
result or model-authored dictionary is never an input to the producer.
"""

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bowhead_core.chat_turn_source import chat_turn_user_source_projection
from bowhead_core.document_chat_continuation import Digest, DocumentTurnOwner, Identity
from bowhead_core.documents.continuation import DocumentPlanningReview, validate_document_worker_in_tx
from bowhead_core.documents.delivery import _execution, _scope
from bowhead_core.documents.request import DocumentRequestError, DocumentScope
from bowhead_core.documents.store import _digest, _json
from bowhead_core.events import OrchestratorEvent, make_event
from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

_EVENT: Final = "document.draft_open"


class DocumentDraftOpen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[1]
    turn_id: Identity
    session_id: Identity
    user_id: Identity
    team_id: Identity
    workspace_id: Identity
    job_id: str = Field(pattern=r"^djob_[A-Za-z0-9_]{1,123}$")
    draft_digest: Digest

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value):
        if type(value) is not int:
            raise ValueError("Invalid document opening version")
        return value


async def validate_document_draft_open_in_tx(connection, *, pointer: DocumentDraftOpen) -> None:
    """Reauthorize an existing pointer for reading; never revive its worker."""
    if not connection.in_transaction:
        raise DocumentRequestError("document_execution_fenced")
    scope = DocumentScope(team_id=pointer.team_id, workspace_id=pointer.workspace_id, user_id=pointer.user_id)
    job = await _scope(connection, scope, pointer.job_id, write=False)
    execution = await _execution(connection, pointer.job_id)
    async with connection.execute("SELECT source_hash FROM document_turn_dispatches WHERE job_id=?", (pointer.job_id,)) as cursor:
        dispatch = await cursor.fetchone()
    if (
        job["session_id"] != pointer.session_id
        or job["origin_turn_id"] != pointer.turn_id
        or job["status"] != "queued"
        or dispatch is None
        or execution is None
        or execution["state"] not in {"review", "published"}
        or execution["draft_digest"] != pointer.draft_digest
        or not execution["draft_json"]
        or dispatch["source_hash"] != job["user_source_sha256"]
    ):
        raise DocumentRequestError("document_not_found")
    source = await _read_chat_turn_user_source_in_tx(
        connection, turn_id=pointer.turn_id, expected_user_id=pointer.user_id, expected_workspace_id=pointer.workspace_id
    )
    if source is None or _digest(_json(chat_turn_user_source_projection(source))) != dispatch["source_hash"]:
        raise DocumentRequestError("document_source_unavailable")


async def build_document_draft_open_in_tx(connection, *, worker: DocumentTurnOwner, review: DocumentPlanningReview) -> OrchestratorEvent:
    """Build one optional open event before the original worker finalizes."""
    if type(review) is not DocumentPlanningReview:
        raise DocumentRequestError("document_execution_fenced")
    await validate_document_worker_in_tx(connection, worker=worker, job_id=review.job_id)
    pointer = DocumentDraftOpen(
        version=1,
        turn_id=worker.turn_id,
        session_id=worker.session_id,
        user_id=worker.user_id,
        team_id=worker.team_id,
        workspace_id=worker.workspace_id,
        job_id=review.job_id,
        draft_digest=review.draft_digest,
    )
    await validate_document_draft_open_in_tx(connection, pointer=pointer)
    execution = await _execution(connection, review.job_id)
    if execution["state"] != "review":
        raise DocumentRequestError("document_execution_fenced")
    return make_event(_EVENT, worker.turn_id, pointer.model_dump(), persist_before_delivery=True)
