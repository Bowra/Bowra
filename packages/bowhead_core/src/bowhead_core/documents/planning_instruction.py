"""Derive planner inputs only from canonical source and shared typed answers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiosqlite

from bowhead_core.documents.authoring_protocol import PROFILE as AUTHORING_PROFILE
from bowhead_core.documents.executor import WorkerInput
from bowhead_core.documents.pptx_plan import CURRENT_PROFILE
from bowhead_core.documents.request import DocumentRequestError
from bowhead_core.documents.store import _json
from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

if TYPE_CHECKING:
    from aiosqlite import Connection

    from bowhead_core.document_chat_continuation import DocumentTurnOwner


@dataclass(frozen=True)
class DocumentInstruction:
    text: str
    history: WorkerInput | None


def _answer_history(answers: list[dict]) -> WorkerInput:
    content = _json({"answers": [{"question": answer["question"], "answer": answer["answer"]} for answer in answers]}).encode()
    return WorkerInput("Document clarification history.json", "data", content)


def _retained_history_sources(context: dict, *, instruction: str, history: WorkerInput, profile: str) -> tuple[list, dict]:
    from bowhead_core.documents.continuation import _validated_context

    _validated_context(context, instruction, profile=profile)
    payload = json.loads(context["payload"])
    sources = payload.get("inputs" if profile == AUTHORING_PROFILE else "sources")
    if not isinstance(sources, list) or not sources:
        raise DocumentRequestError("document_checkpoint_unavailable")
    expected = {"name": history.name, "sha256": hashlib.sha256(history.content).hexdigest()}
    if profile == AUTHORING_PROFILE:
        if sources[-1] != {**expected, "alias": f"data-{len(sources) - 2}", "role": "data"}:
            raise DocumentRequestError("document_checkpoint_unavailable")
        sources = [{"name": item.get("name"), "sha256": item.get("sha256")} if isinstance(item, dict) else None for item in sources]
    return sources, expected


async def clarification_evidence_sources_in_tx(db: Connection, *, job) -> list[dict[str, str]]:
    """Derive display metadata after authorized job reading, without a live worker."""
    from bowhead_core.chat_turn_source import chat_turn_user_source_projection
    from bowhead_core.document_chat_continuation import DocumentTurnOwner
    from bowhead_core.document_clarification_lifecycle import prior_document_answers_in_tx
    from bowhead_core.documents.store import _digest

    db.row_factory = aiosqlite.Row
    async with db.execute(
        """SELECT d.creation_generation,d.owner_id,d.source_hash,p.instruction,p.context_json
        FROM document_turn_dispatches d JOIN document_planning_attempts p ON p.job_id=d.job_id AND p.attempt_no=1
        WHERE d.job_id=?""",
        (job["id"],),
    ) as cursor:
        retained = await cursor.fetchone()
    if retained is None or retained["context_json"] is None:
        return []
    try:
        source = await _read_chat_turn_user_source_in_tx(
            db, turn_id=job["origin_turn_id"], expected_user_id=job["requested_by"], expected_workspace_id=job["workspace_id"]
        )
        if (
            source is None
            or source.session_id != job["session_id"]
            or (
                _digest(_json(chat_turn_user_source_projection(source))) != retained["source_hash"]
                or retained["source_hash"] != job["user_source_sha256"]
            )
        ):
            return []
        worker = DocumentTurnOwner(
            team_id=job["team_id"],
            user_id=job["requested_by"],
            workspace_id=job["workspace_id"],
            session_id=job["session_id"],
            turn_id=job["origin_turn_id"],
            generation=retained["creation_generation"],
            owner_id=retained["owner_id"],
        )
        answers = await prior_document_answers_in_tx(db, worker=worker, job_id=job["id"])
        if not answers:
            return []
        history = _answer_history(answers)
        context = json.loads(retained["context_json"])
        sources, expected = _retained_history_sources(
            context, instruction=retained["instruction"], history=history, profile=json.loads(context["payload"])["profile"]
        )
        # The server appends canonical answers last. Ambiguous identical uploads
        # and unsupported historical encodings keep neutral source wording.
        if not isinstance(sources, list) or not sources or sources[-1] != expected or sources.count(expected) != 1:
            return []
        return [{"source_name": history.name, "source_sha256": expected["sha256"]}]
    except (ValueError, TypeError, KeyError, IndexError):
        return []


def _clarified_instruction(text: str) -> str:
    return (
        text
        + "\n\nThe last evidence source named Document clarification history.json contains the canonical user answers to earlier clarification questions, not alternative business data. "
        "Use each explicit answer as the user's decision for its question, superseding earlier alternatives; do not ask it again. "
        "A null or uncertain answer resolves nothing. Other missing data, conflicts and unsupported changes remain unresolved. "
        "Literal replacement text supplied in an answer needs no duplicate attachment evidence. "
        "Use answer facts or exact unique quotations from this request for replacements, and sum business data only when the chosen scope calls for it. "
        "An answer selects a decision; it is not text to paste indiscriminately. Preserve field value types and units. "
        "For numeric slots, use only the chosen numeric value, not explanatory answer labels. Never replace a chosen value with a different calculable value."
    )


def _clarified_instruction_v5(text: str, answers: list[dict]) -> str:
    return (
        text + "\n\nConfirmed user decisions from the original authenticated clarification cards follow. "
        "Each question only identifies its context; its answer is the user's decision, superseding earlier alternatives. "
        "Do not ask an answered question again. A null or uncertain answer resolves nothing. "
        "Copy only the requested title or value, not the surrounding explanation. "
        "Do not recalculate a selected business value from different data. "
        "Other missing requirements and unsupported locations remain unresolved.\n"
        + _json({"decisions": [{"question": answer["question"], "answer": answer["answer"]} for answer in answers]})
    )


async def document_instruction_in_tx(db: Connection, *, worker: DocumentTurnOwner, job_id: str) -> DocumentInstruction:
    from bowhead_core.document_clarification_lifecycle import prior_document_answers_in_tx
    from bowhead_core.documents.continuation import _dispatch_material

    _, runtime = await _dispatch_material(db, job_id=job_id, creation_worker=worker)

    source = await _read_chat_turn_user_source_in_tx(
        db, turn_id=worker.turn_id, expected_user_id=worker.user_id, expected_workspace_id=worker.workspace_id
    )
    if source is None or source.session_id != worker.session_id:
        raise DocumentRequestError("document_source_unavailable")
    async with db.execute(
        "SELECT instruction FROM document_continuations WHERE job_id=? UNION ALL SELECT instruction FROM document_revisions WHERE job_id=?",
        (job_id, job_id),
    ) as cursor:
        prior = await cursor.fetchone()
    text = prior[0] if prior else source.user_text
    legacy_text = text
    try:
        answers = await prior_document_answers_in_tx(db, worker=worker, job_id=job_id)
    except ValueError as exc:
        # Unsupported history cannot be reconstructed from displayed prose.
        raise DocumentRequestError("document_history_unavailable") from exc
    history = legacy_history = None
    if answers:
        legacy_history = WorkerInput("Document clarification history.json", "data", _json({"request": text, "prior_answers": answers}).encode())
        # Control fields such as option_index are not business facts the model may copy into a slide.
        history = _answer_history(answers)
        legacy_text += "\n\nUse the canonical earlier answers in Document clarification history.json to resolve the prior questions."
        text = _clarified_instruction_v5(text, answers) if runtime.planner_profile == CURRENT_PROFILE else _clarified_instruction(text)
    async with db.execute("SELECT instruction,context_json FROM document_planning_attempts WHERE job_id=? AND attempt_no=1", (job_id,)) as cursor:
        retained = await cursor.fetchone()
    if retained is not None:
        # Preserve immutable old checkpoints, but never trust arbitrary retained instructions.
        if runtime.planner_profile != CURRENT_PROFILE and retained[0] == legacy_text:
            history = legacy_history
        elif retained[0] != text:
            raise DocumentRequestError("document_checkpoint_unavailable")
        if history is not None and retained[1] is not None:
            # The server appends this input last; a matching uploaded name grants no authority.
            try:
                sources, expected = _retained_history_sources(
                    json.loads(retained[1]), instruction=retained[0], history=history, profile=runtime.planner_profile
                )
                if sources[-1] != expected:
                    raise ValueError("Retained history differs from canonical answers")
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                raise DocumentRequestError("document_checkpoint_unavailable") from exc
        text = retained[0]
    if not 1 <= len(text) <= 8000 or history is not None and len(history.content) > 64_000:
        raise DocumentRequestError("planning_input_limit")
    return DocumentInstruction(text, history)
