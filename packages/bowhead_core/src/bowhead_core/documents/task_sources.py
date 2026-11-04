"""Explicit prior-user provenance for the existing private document task."""

from __future__ import annotations

import json
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bowhead_core.chat_turn_source import ChatTurnUserSource, chat_turn_user_source_projection
from bowhead_core.documents.request import DocumentRequestError, Identity, UserQuote
from bowhead_core.ordinary_lead_continuation import content_hash
from bowhead_core.session_store import _read_chat_turn_user_source_in_tx


class DocumentTaskSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1]
    turn_id: Identity
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def task_sources_enabled() -> bool:
    return os.environ.get("FEATURE_DOCUMENT_TASK_SOURCES", "0") == "1"


def task_source_guidance() -> str:
    if not task_sources_enabled():
        return ""
    return (
        "For generate, provide inputs with artifact_id and role, requested_outputs, and original_request_quote. "
        "Use original_request_quote=null when the active canonical request contains the requirements, including a native question resume. "
        "A quotation matching that active request also reuses it directly. For a new ordinary follow-up to an earlier task, "
        "copy a short unique exact phrase from the EARLIER USER request into original_request_quote, not the current reply. "
        "The service supplies both complete user messages. Do not paraphrase them or provide evidence fields for generate. "
        "For example, after 'Use this template; change Acme to Aurora' followed by 'Revenue is 20', "
        "original_request_quote can be 'change Acme to Aurora'; the service reads 'Revenue is 20' itself. "
    )


_EARLIER_SOURCES = """SELECT u.turn_id FROM chat_turn_user_sources u
JOIN turns t ON t.id=u.turn_id AND t.session_id=u.session_id
JOIN turns current_turn ON current_turn.id=? AND current_turn.session_id=u.session_id
JOIN sessions s ON s.id=u.session_id
WHERE u.session_id=? AND u.user_id=? AND u.workspace_id=?
  AND s.user_id=u.user_id AND s.workspace_id=u.workspace_id AND s.deleted_at IS NULL
  AND t.seq<current_turn.seq AND t.user_text=u.user_text
  AND u.input_document_json IS NULL AND u.document_revision_target_json IS NULL
  AND u.context_maintenance_json IS NULL AND (u.parts_json IS NULL OR json_array_length(u.parts_json)<=1)
"""


async def _read_earlier(db, *, current: ChatTurnUserSource, turn_id: str) -> ChatTurnUserSource:
    async with db.execute(
        _EARLIER_SOURCES + " AND u.turn_id=?",
        (current.turn_id, current.session_id, current.user_id, current.workspace_id, turn_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise DocumentRequestError("document_task_source_unavailable", field="task_source")
    source = await _read_chat_turn_user_source_in_tx(
        db, turn_id=turn_id, expected_user_id=current.user_id, expected_workspace_id=current.workspace_id
    )
    if source is None or source.session_id != current.session_id:
        raise DocumentRequestError("document_task_source_unavailable", field="task_source")
    return source


async def resolve_task_source_in_tx(db, *, current: ChatTurnUserSource, quote: UserQuote) -> tuple[DocumentTaskSource, ChatTurnUserSource]:
    """Select only an explicitly quoted, unique earlier human source."""
    if not task_sources_enabled():
        raise DocumentRequestError("document_task_source_disabled", field="task_source")
    async with db.execute(
        _EARLIER_SOURCES + " AND instr(u.user_text,?)>0 ORDER BY t.seq DESC LIMIT 2",
        (current.turn_id, current.session_id, current.user_id, current.workspace_id, quote.text),
    ) as cursor:
        rows = await cursor.fetchall()
    if not rows:
        raise DocumentRequestError("document_task_source_unavailable", field="task_source")
    if len(rows) != 1:
        raise DocumentRequestError("document_task_source_ambiguous", field="task_source")
    source = await _read_earlier(db, current=current, turn_id=rows[0][0])
    quote.verify(source.user_text, field="task_source")
    instruction_with_task_source(current=current, original=source)
    return DocumentTaskSource(version=1, turn_id=source.turn_id, source_hash=content_hash(chat_turn_user_source_projection(source))), source


async def read_task_source_in_tx(db, *, current: ChatTurnUserSource, snapshot: DocumentTaskSource, quote: UserQuote) -> ChatTurnUserSource:
    """Recover by pinned identity and hash; never select a newer matching source."""
    source = await _read_earlier(db, current=current, turn_id=snapshot.turn_id)
    if content_hash(chat_turn_user_source_projection(source)) != snapshot.source_hash:
        raise DocumentRequestError("document_task_source_changed", field="task_source")
    quote.verify(source.user_text, field="task_source")
    instruction_with_task_source(current=current, original=source)
    return source


def instruction_with_task_source(*, current: ChatTurnUserSource, original: ChatTurnUserSource) -> str:
    text = (
        "Apply the original user request and the current supplement together. "
        "The supplement updates only what it explicitly changes; preserve all other requirements.\n\n"
        "Original user request:\n" + original.user_text + "\n\nCurrent user supplement:\n" + current.user_text
    )
    if len(text) > 8000:
        raise DocumentRequestError("document_task_source_limit", field="task_source")
    return text


async def job_task_source_in_tx(db, *, job) -> tuple[DocumentTaskSource, ChatTurnUserSource, ChatTurnUserSource] | None:
    """Validate optional persisted provenance under the caller's original job ACL."""
    request = json.loads(job["request_json"])
    snapshot = request.get("task_source")
    quote = request["proposal"].get("task_source")
    if snapshot is None and quote is None:
        return None
    try:
        snapshot = DocumentTaskSource.model_validate_json(json.dumps(snapshot))
        quote = UserQuote.model_validate_json(json.dumps(quote))
    except ValidationError as exc:
        raise DocumentRequestError("document_task_source_changed", field="task_source") from exc
    current = await _read_chat_turn_user_source_in_tx(
        db, turn_id=job["origin_turn_id"], expected_user_id=job["requested_by"], expected_workspace_id=job["workspace_id"]
    )
    if (
        current is None
        or current.session_id != job["session_id"]
        or content_hash(chat_turn_user_source_projection(current)) != job["user_source_sha256"]
    ):
        raise DocumentRequestError("document_source_unavailable")
    original = await read_task_source_in_tx(db, current=current, snapshot=snapshot, quote=quote)
    return snapshot, current, original


async def task_instruction_in_tx(db, *, job, fallback: str) -> str:
    material = await job_task_source_in_tx(db, job=job)
    return instruction_with_task_source(current=material[1], original=material[2]) if material else fallback
