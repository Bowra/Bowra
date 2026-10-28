"""Private first-BI pause material, bound to the original execution and card.

The existing Turn/dispatch lease owns scheduling. This row only prevents the
same pause from entering the native loop twice and retains its conversation.
All functions use their caller's transaction; none opens a second writer.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite

from bowhead_core.bi_workspace_continuation import (
    BiWorkspaceAttachmentContinuation,
    BiWorkspaceContinuation,
    BiWorkspaceReadContinuation,
    BiWorkspaceToolContinuation,
)
from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope, _row, _transaction
from bowhead_core.clarification_store import MAX_DIRECTIVE_CHARS, stamp_resume_context

if TYPE_CHECKING:
    from bowhead_core.chat_reference_execution import ReferencePrimaryInput
    from bowhead_core.chat_reference_runtime_store import ReferencePrimaryComposition

CONTRACT = "reference-bi-workspace.v1"
CONTEXT_KEY = "reference_continuation"
TABLE = "chat_reference_bi_continuations"
SKIP = "__bowhead_clarification_skip__"


def _hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


async def installed(db: aiosqlite.Connection) -> bool:
    return await _row(db, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)) is not None


def _json(raw: str, expected: type):
    if not isinstance(raw, str) or len(raw.encode()) > 1048576:
        raise ValueError("reference_continuation_changed")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError("reference_continuation_changed") from exc
    if type(value) is not expected:
        raise ValueError("reference_continuation_changed")
    return value


def _candidate_values(raw: str) -> list[str]:
    candidates = _json(raw, list)
    if not candidates or any(type(item) is not dict or not isinstance(item.get("value"), str) or not item["value"] for item in candidates):
        raise ValueError("reference_continuation_changed")
    return [item["value"] for item in candidates]


async def read_pause(db: aiosqlite.Connection, turn_id: str):
    _transaction(db)
    if not await installed(db):
        return None
    return await _row(
        db,
        "SELECT c.*,s.id AS session_id,s.user_id,s.workspace_id,w.team_id,p.created_policy_epoch "
        "FROM chat_reference_bi_continuations c JOIN chat_reference_resolutions r ON r.turn_id=c.turn_id AND r.resolution_seq=1 "
        "JOIN chat_reference_plans p ON p.turn_id=c.turn_id JOIN chat_reference_plan_retention a ON a.turn_id=p.turn_id "
        "JOIN turns t ON t.id=c.turn_id JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id "
        "WHERE c.turn_id=? AND c.execution_id=r.execution_id AND c.composition_digest=r.composition_digest "
        "AND c.source_hash=p.source_hash AND c.retention_state='available' AND a.available AND a.deadline>julianday('now')",
        (turn_id,),
    )


def pause_scope(row) -> ReferenceScope:
    return ReferenceScope(**{key: row[key] for key in ("team_id", "user_id", "workspace_id", "session_id")}, policy_epoch=row["created_policy_epoch"])


def open_checkpoint(row, *, cipher: ReferenceCipher) -> BiWorkspaceContinuation:
    content = cipher.open(scope=pause_scope(row), purpose=CONTRACT, identity=row["turn_id"], ciphertext=row["ciphertext"])
    if len(content) > 1048576 or _hash(content) != row["checkpoint_hash"]:
        raise ValueError("reference_continuation_changed")
    checkpoint_type = {
        1: BiWorkspaceContinuation,
        2: BiWorkspaceReadContinuation,
        3: BiWorkspaceAttachmentContinuation,
        4: BiWorkspaceToolContinuation,
    }.get(row["checkpoint_version"])
    if checkpoint_type is None:
        raise ValueError("reference_continuation_changed")
    checkpoint = checkpoint_type.model_validate_json(content)
    if checkpoint.next_iteration != row["captured_iterations"]:
        raise ValueError("reference_continuation_changed")
    if any(
        getattr(checkpoint, key) != row[key] for key in ("turn_id", "session_id", "team_id", "user_id", "workspace_id", "execution_id", "subagent_id")
    ):
        raise ValueError("reference_continuation_changed")
    return checkpoint


async def capture_pause(
    db: aiosqlite.Connection,
    *,
    scope: ReferenceScope,
    primary: ReferencePrimaryInput,
    composition: ReferencePrimaryComposition,
    checkpoint: BiWorkspaceContinuation,
    cipher: ReferenceCipher,
) -> None:
    _transaction(db)
    if (
        type(checkpoint) not in (BiWorkspaceContinuation, BiWorkspaceReadContinuation, BiWorkspaceAttachmentContinuation, BiWorkspaceToolContinuation)
        or any(getattr(checkpoint, key) != value for key, value in scope.identity().items())
        or checkpoint.turn_id != primary.source.turn_id
        or checkpoint.execution_id != primary.binding.execution_id
        or checkpoint.profile != primary.binding.runtime_profile_name
        or checkpoint.policy_fingerprint != primary.binding.policy_fingerprint
        or checkpoint.capability_fingerprint != primary.binding.capability_fingerprint
        or checkpoint.task_hash != composition.task_hash
        or bool(primary.source.attachment_ids) != bool(getattr(checkpoint, "attachment_context", None))
    ):
        raise ValueError("reference_continuation_changed")
    content = checkpoint.model_dump_json().encode()
    deadline = await _row(db, "SELECT expires_at FROM chat_reference_plans WHERE turn_id=?", (checkpoint.turn_id,))
    ciphertext = cipher.seal(scope=scope, purpose=CONTRACT, identity=checkpoint.turn_id, content=content)
    await db.execute(
        "INSERT INTO chat_reference_bi_continuations(turn_id,execution_id,schema_version,created_generation,subagent_id,"
        "source_hash,composition_digest,checkpoint_hash,ciphertext,retention_state,state,created_at,expires_at,checkpoint_version,captured_iterations) "
        "VALUES (?,?,1,1,?,?,?,?,?,'available','captured',?,?,?,?)",
        (
            checkpoint.turn_id,
            checkpoint.execution_id,
            checkpoint.subagent_id,
            primary.plan.source_hash,
            composition.digest,
            _hash(content),
            ciphertext,
            datetime.now(UTC).isoformat(),
            deadline["expires_at"],
            checkpoint.schema_version,
            checkpoint.next_iteration,
        ),
    )


async def require_question(db: aiosqlite.Connection, pause, *, resolved: bool = False, history: bool = False):
    if resolved and history:
        raise ValueError("reference_continuation_changed")
    question = await _row(db, "SELECT * FROM clarification_requests WHERE request_id=?", (pause["clarification_id"],))
    if (
        question is None
        or question["kind"] != "tool_context_select"
        or question["tool"] != "datarc"
        or question["multi_select"]
        or question["token"]
        or any(question[key] != pause[key] for key in ("turn_id", "session_id", "user_id", "workspace_id"))
        or question["status"]
        not in (("pending", "resolved", "expired", "dismissed") if history else ("resolved",) if resolved else ("pending", "resolved"))
    ):
        raise ValueError("reference_continuation_changed")
    context = _json(question["resume_context_json"] or "{}", dict)
    if context.get(CONTEXT_KEY) != {"contract": CONTRACT, "checkpoint_hash": pause["checkpoint_hash"]}:
        raise ValueError("reference_continuation_changed")
    return question


def decision(question) -> tuple[str, str | None, str]:
    values = _json(question["chosen_values_json"] or "[]", list)
    chosen = question["chosen_value"]
    if values != [chosen] or not isinstance(chosen, str) or not chosen:
        raise ValueError("reference_continuation_changed")
    candidates = _candidate_values(question["candidates_json"])
    if chosen != SKIP and chosen not in candidates:
        raise ValueError("reference_continuation_changed")
    if _json(question["answers_json"] or "[]", list) or _json(question["edited_subs_json"] or "[]", list):
        # This single workspace decision cannot consume Plan edits or answers
        # to other questions. Keep the saved decision for explicit recovery.
        raise ValueError("reference_continuation_changed")
    directive = question["user_directive"]
    if directive is not None and (not isinstance(directive, str) or len(directive) > MAX_DIRECTIVE_CHARS):
        raise ValueError("reference_continuation_changed")
    # Workspace IDs stay in connector metadata, never in NL2SQL query text.
    answer = (
        "I skipped the workspace selection. Continue the original task only if the original Agent's available tools allow it."
        if chosen == SKIP
        else "Continue the original task with the workspace selected in the tool context."
    )
    if directive:
        answer += "\n\nAdditional instruction from the user:\n" + directive
    material = {key: question[key] for key in ("request_id", "chosen_value", "chosen_values_json", "user_directive", "answers_json")}
    return _hash(json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()), None if chosen == SKIP else chosen, answer


async def bind_question(db: aiosqlite.Connection, *, turn_id: str, clarification_id: str, context: dict, cipher: ReferenceCipher) -> None:
    pause = await read_pause(db, turn_id)
    if pause is None or pause["state"] != "captured":
        raise ValueError("reference_continuation_unavailable")
    checkpoint = open_checkpoint(pause, cipher=cipher)
    question = await _row(db, "SELECT * FROM clarification_requests WHERE request_id=?", (clarification_id,))
    options = json.loads(checkpoint.result_json)["clarification"]["options"]
    if (
        question is None
        or question["status"] != "pending"
        or question["kind"] != "tool_context_select"
        or question["tool"] != "datarc"
        or question["multi_select"]
        or question["token"]
        or any(question[key] != pause[key] for key in ("turn_id", "session_id", "user_id", "workspace_id"))
        or _candidate_values(question["candidates_json"]) != [item["value"] for item in options]
    ):
        raise ValueError("reference_continuation_changed")
    encoded = json.dumps(
        stamp_resume_context({**context, CONTEXT_KEY: {"contract": CONTRACT, "checkpoint_hash": pause["checkpoint_hash"]}}),
        ensure_ascii=False,
        default=str,
    )
    await db.execute("UPDATE clarification_requests SET resume_context_json=? WHERE request_id=? AND status='pending'", (encoded, clarification_id))
    await db.execute(
        "UPDATE chat_reference_bi_continuations SET clarification_id=?,state='ready' WHERE turn_id=? AND state='captured'",
        (clarification_id, turn_id),
    )


async def pause_ready(db: aiosqlite.Connection, *, turn_id: str, clarification_id: str | None = None) -> bool:
    pause = await read_pause(db, turn_id)
    if pause is None or pause["state"] != "ready" or (clarification_id is not None and pause["clarification_id"] != clarification_id):
        return False
    try:
        await require_question(db, pause, resolved=clarification_id is not None)
    except ValueError:
        return False
    return not await _row(
        db,
        "SELECT 1 FROM chat_reference_model_invocations WHERE turn_id=? AND (created_generation<>1 OR status IN ('pending','delivery_unknown'))",
        (turn_id,),
    )


async def pause_recoverable(
    db: aiosqlite.Connection,
    *,
    turn_id: str,
    cipher: ReferenceCipher,
) -> bool:
    """Validate every persisted input needed by a future resume."""

    if not await pause_ready(db, turn_id=turn_id):
        return False
    pause = await read_pause(db, turn_id)
    if pause is None:
        return False
    try:
        open_checkpoint(pause, cipher=cipher)
        question = await require_question(db, pause)
        if question["status"] == "resolved":
            decision(question)
        else:
            _candidate_values(question["candidates_json"])
    except ValueError:
        return False
    return True


async def reserve_resume(db: aiosqlite.Connection, *, turn_id: str, clarification_id: str, generation: int) -> None:
    if not await pause_ready(db, turn_id=turn_id, clarification_id=clarification_id):
        raise ValueError("reference_continuation_unavailable")
    pause = await read_pause(db, turn_id)
    question = await require_question(db, pause, resolved=True)
    answer_hash, _selected, _answer = decision(question)
    cursor = await db.execute(
        "UPDATE chat_reference_bi_continuations SET state='reserved',resume_generation=?,answer_hash=? WHERE turn_id=? AND state='ready'",
        (generation, answer_hash, turn_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("reference_continuation_already_started")


async def require_resume(db: aiosqlite.Connection, *, turn_id: str, generation: int):
    pause = await read_pause(db, turn_id)
    if pause is None or pause["state"] not in ("reserved", "started") or pause["resume_generation"] != generation:
        raise ValueError("reference_continuation_unavailable")
    question = await require_question(db, pause, resolved=True)
    if decision(question)[0] != pause["answer_hash"]:
        raise ValueError("reference_continuation_changed")
    return pause, question


async def release_resume(db: aiosqlite.Connection, *, turn_id: str, generation: int) -> None:
    pause, _ = await require_resume(db, turn_id=turn_id, generation=generation)
    if pause["state"] != "reserved" or await _row(
        db, "SELECT 1 FROM chat_reference_model_invocations WHERE turn_id=? AND created_generation<>1", (turn_id,)
    ):
        raise ValueError("reference_continuation_already_started")
    await db.execute(
        "UPDATE chat_reference_bi_continuations SET state='ready',resume_generation=NULL,answer_hash=NULL WHERE turn_id=? AND state='reserved'",
        (turn_id,),
    )
