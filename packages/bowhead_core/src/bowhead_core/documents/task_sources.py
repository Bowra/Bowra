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


class DocumentTaskContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    through_turn_id: Identity
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class DocumentTaskSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1]
    turn_id: Identity
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    continuation: DocumentTaskContinuation | None = Field(default=None, exclude_if=lambda value: value is None)


def task_sources_enabled() -> bool:
    return os.environ.get("FEATURE_DOCUMENT_TASK_SOURCES", "0") == "1"


def task_source_guidance() -> str:
    if not task_sources_enabled():
        return ""
    return (
        "For generate, provide inputs with artifact_id and role, requested_outputs, and original_request_quote. "
        "For a new ordinary follow-up to an earlier task, "
        "copy a short unique exact phrase from the EARLIER USER request into original_request_quote, not the current reply. "
        "The current reply is already included automatically. Use null only when the current request contains the complete requirements, including a native question resume. "
        "Use the original template artifact for a continued edit unless the user asks to change templates. "
        "The service supplies the user messages and confirmed answers from that task through the current request. "
        "Copy the quotation exactly and omit evidence fields for generate. "
        "For example, after 'Use this template; change Acme to Aurora' followed by 'Revenue is 20', "
        "original_request_quote can be 'change Acme to Aurora'; the service reads 'Revenue is 20' itself. "
    )


_EARLIER_SOURCES = """FROM chat_turn_user_sources u
JOIN turns t ON t.id=u.turn_id AND t.session_id=u.session_id
JOIN turns current_turn ON current_turn.id=? AND current_turn.session_id=u.session_id
JOIN sessions s ON s.id=u.session_id
WHERE u.session_id=? AND u.user_id=? AND u.workspace_id=?
  AND s.user_id=u.user_id AND s.workspace_id=u.workspace_id AND s.deleted_at IS NULL
  AND t.seq<current_turn.seq AND t.user_text=u.user_text
  AND u.input_document_json IS NULL AND u.document_revision_target_json IS NULL
  AND u.context_maintenance_json IS NULL AND (u.parts_json IS NULL OR json_array_length(u.parts_json)<=1)
"""

_CONTEXT_TURN_LIMIT = 32
_CONTEXT_ANSWER_LIMIT = 64


async def _continuation_context(db, *, current: ChatTurnUserSource, original: ChatTurnUserSource) -> list[dict]:
    """Read ordered canonical user input between the quoted task and its supplement."""
    async with db.execute(
        "SELECT u.turn_id,u.user_text " + _EARLIER_SOURCES + " AND t.seq >= (SELECT seq FROM turns WHERE id=?) ORDER BY t.seq LIMIT ?",
        (current.turn_id, current.session_id, current.user_id, current.workspace_id, original.turn_id, _CONTEXT_TURN_LIMIT + 1),
    ) as cursor:
        turns = dict(await cursor.fetchall())
    if len(turns) > _CONTEXT_TURN_LIMIT:
        raise DocumentRequestError("document_task_source_limit", field="task_source")
    if next(iter(turns), None) != original.turn_id:
        raise DocumentRequestError("document_task_source_unavailable", field="task_source")
    placeholders = ",".join("?" for _ in turns)
    async with db.execute(
        f"""SELECT turn_id, request_id, answers_json FROM clarification_requests
        WHERE session_id=? AND user_id=? AND workspace_id=? AND status='resolved'
          AND answers_json IS NOT NULL AND turn_id IN ({placeholders})
        ORDER BY created_at, request_id LIMIT ?""",
        (current.session_id, current.user_id, current.workspace_id, *turns, _CONTEXT_ANSWER_LIMIT + 1),
    ) as cursor:
        cards = await cursor.fetchall()
    if len(cards) > _CONTEXT_ANSWER_LIMIT:
        raise DocumentRequestError("document_task_source_limit", field="task_source")
    answers_by_turn: dict[str, list[dict]] = {}
    for turn_id, request_id, encoded in cards:
        try:
            for answer in json.loads(encoded):
                if answer["source"] in {"candidate", "custom"} and not answer["skipped"]:
                    question, value = answer["question"], answer["answer"]
                    if not isinstance(question, str) or not isinstance(value, str) or not value:
                        raise ValueError("Invalid resolved clarification")
                    answers_by_turn.setdefault(turn_id, []).append({"request_id": request_id, "question": question, "answer": value})
        except (TypeError, KeyError, ValueError) as exc:
            raise DocumentRequestError("document_task_source_changed", field="task_source") from exc
    context = []
    for turn_id, user_text in turns.items():
        answers = answers_by_turn.get(turn_id, [])
        if turn_id == original.turn_id:
            if answers:
                context.append({"turn_id": turn_id, "answers": answers})
            continue
        context.append({"turn_id": turn_id, "user_text": user_text, "answers": answers})
    return context


async def _read_earlier(db, *, current: ChatTurnUserSource, turn_id: str) -> ChatTurnUserSource:
    async with db.execute(
        "SELECT u.turn_id " + _EARLIER_SOURCES + " AND u.turn_id=?",
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
        "SELECT u.turn_id,u.user_text=? AS exact_match "
        + _EARLIER_SOURCES
        + " AND instr(u.user_text,?)>0 ORDER BY exact_match DESC,t.seq DESC LIMIT 2",
        (quote.text, current.turn_id, current.session_id, current.user_id, current.workspace_id, quote.text),
    ) as cursor:
        rows = await cursor.fetchall()
    if not rows:
        raise DocumentRequestError("document_task_source_unavailable", field="task_source")
    if rows[0][1]:
        rows = [row for row in rows if row[1]]
    if len(rows) != 1:
        raise DocumentRequestError("document_task_source_ambiguous", field="task_source")
    source = await _read_earlier(db, current=current, turn_id=rows[0][0])
    quote.verify(source.user_text, field="task_source")
    context = await _continuation_context(db, current=current, original=source)
    instruction_with_task_source(current=current, original=source, context=context)
    continuation = DocumentTaskContinuation(through_turn_id=current.turn_id, context_hash=content_hash(context)) if context else None
    return DocumentTaskSource(
        version=1, turn_id=source.turn_id, source_hash=content_hash(chat_turn_user_source_projection(source)), continuation=continuation
    ), source


async def read_task_source_in_tx(
    db, *, current: ChatTurnUserSource, snapshot: DocumentTaskSource, quote: UserQuote
) -> tuple[ChatTurnUserSource, str]:
    """Recover by pinned identity and hash; never select a newer matching source."""
    source = await _read_earlier(db, current=current, turn_id=snapshot.turn_id)
    if content_hash(chat_turn_user_source_projection(source)) != snapshot.source_hash:
        raise DocumentRequestError("document_task_source_changed", field="task_source")
    quote.verify(source.user_text, field="task_source")
    context = None
    if snapshot.continuation is not None:
        through = snapshot.continuation.through_turn_id
        end = current if through == current.turn_id else await _read_earlier(db, current=current, turn_id=through)
        context = await _continuation_context(db, current=end, original=source)
        if content_hash(context) != snapshot.continuation.context_hash:
            raise DocumentRequestError("document_task_source_changed", field="task_source")
    return source, instruction_with_task_source(current=current, original=source, context=context)


def instruction_with_task_source(*, current: ChatTurnUserSource, original: ChatTurnUserSource, context: list[dict] | None = None) -> str:
    text = (
        "Apply the original user request and the current supplement together. "
        "The supplement updates only what it explicitly changes; preserve all other requirements.\n\n"
        "Original user request:\n" + original.user_text
    )
    for item in context or ():
        if "user_text" in item:
            text += "\n\nEarlier user supplement:\n" + item["user_text"]
        for answer in item["answers"]:
            text += "\n\nClarification question:\n" + answer["question"] + "\nUser's confirmed answer:\n" + answer["answer"]
    text += "\n\nCurrent user supplement:\n" + current.user_text
    if len(text) > 8000:
        raise DocumentRequestError("document_task_source_limit", field="task_source")
    return text


async def job_task_source_in_tx(db, *, job) -> tuple[DocumentTaskSource, ChatTurnUserSource, ChatTurnUserSource, str] | None:
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
    original, instruction = await read_task_source_in_tx(db, current=current, snapshot=snapshot, quote=quote)
    return snapshot, current, original, instruction


async def task_instruction_in_tx(db, *, job, fallback: str) -> str:
    material = await job_task_source_in_tx(db, job=job)
    return material[3] if material else fallback
