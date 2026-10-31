"""In-place resume of helper runs (agent run limits, phase 2 §6).

When a helper run ends completed or partial, its message list and reference inventory are kept as a
sealed checkpoint. A later turn resumes the same ``subagent_id`` from that transcript with a new
instruction and a fresh budget, instead of dispatching an empty helper. The Continue button records
which checkpoints to resume with the user message (``subagent_resume_requests``); a typed follow-up
lets Lead choose from the resumable runs listed in its turn tail and resume one with
``subagent_spawn(resume_checkpoint_id=...)``.

Checks run in two places. :func:`claim` checks ownership, expiry, the same-turn rule and concurrent
use before anything is emitted. The runner checks that the Agent revision and the advertised tool
names are unchanged before it restores the transcript (:func:`resume_refusal_for`); when they
changed, the helper runs fresh from :func:`fallback_task` and the step list shows why.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from bowhead_router.types import Message, ToolCall

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_knowledge_policy import KnowledgeScope
from bowhead_core.datarc_selection import DatarcSelectionSnapshot, require_datarc_context_migration
from bowhead_core.kb_search_budget import KnowledgeReadState

logger = logging.getLogger(__name__)

PURPOSE = "subagent-resume.v1"
RETENTION_DAYS = 30
MAX_REQUESTS = 4
MAX_PLAIN_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 4000
LIVE_LIST_LIMIT = 8
_CLEARED = "[Removed when this run was saved: an earlier tool result. Re-run the tool only if you need its exact rows again.]"

# Codes Lead and the step list see when a run cannot be resumed.
REFUSAL_CODES = frozenset(
    {
        "not_found",
        "superseded",
        "expired",
        "in_use",
        "same_turn",
        "agent_changed",
        "tools_changed",
        "unreadable",
        "datarc_resume_scope_missing",
        "datarc_resume_scope_changed",
    }
)


class ResumeRefused(Exception):
    """A checkpoint cannot be resumed; ``code`` is one of :data:`REFUSAL_CODES`."""

    def __init__(self, code: str, checkpoint: ResumeCheckpoint | None = None) -> None:
        if code not in REFUSAL_CODES:
            raise ValueError(f"unknown resume refusal {code!r}")
        super().__init__(code)
        self.code = code
        # Known metadata of the refused run, so a fresh helper can take over its task.
        self.checkpoint = checkpoint


@dataclass(frozen=True)
class ResumeCheckpoint:
    id: str
    team_id: str
    user_id: str
    workspace_id: str
    session_id: str
    turn_id: str
    run_subagent_id: str
    profile: str
    agent_id: str | None
    agent_revision_id: str | None
    tool_names_hash: str
    task: str
    completion: str
    stop_reason: str | None
    partial_done: str | None
    partial_remaining: str | None
    state: str
    claimed_by_turn_id: str | None
    created_at: str
    expires_at: str
    ciphertext: str | None = None


@dataclass(frozen=True)
class ResumeMaterial:
    messages: list[Message]
    reference_inventory: list[dict[str, Any]]
    knowledge_read: KnowledgeReadState | None = None
    knowledge_scope: KnowledgeScope | None = None
    knowledge_sources: dict[str, str | None] | None = None
    datarc_selection: DatarcSelectionSnapshot | None = None


@dataclass
class ResumeGrant:
    """A checkpoint handed to the runner for one dispatch.

    With ``material`` the runner continues the saved transcript unless the Agent or its tools
    changed; it then records the reason in ``refused`` and starts fresh from the saved task.
    Without ``material`` (``refused`` already set) it always starts fresh.
    """

    checkpoint: ResumeCheckpoint
    material: ResumeMaterial | None
    instruction: str
    refused: str | None = None


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def tool_names_hash(names: Iterable[str]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(sorted(set(names))).encode()).hexdigest()


def _cipher():
    from bowhead_core.chat_reference_storage import ReferenceCipher

    return ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", ""))


def _scope(checkpoint_scope: dict[str, str]):
    from bowhead_core.chat_reference_storage import ReferenceScope

    return ReferenceScope(policy_epoch=0, **checkpoint_scope)


def _message_dict(message: Message) -> dict[str, Any]:
    # Images are not kept; the transcript keeps the text the model saw beside them.
    return {
        "role": message.role,
        "content": message.content or "",
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": [asdict(call) for call in message.tool_calls] if message.tool_calls else None,
    }


def transcript_is_resumable(messages: Sequence[Message]) -> bool:
    """Every tool call has its result and the list starts with the task."""
    if not messages or messages[0].role != "user":
        return False
    pending: set[str] = set()
    for message in messages:
        if message.tool_calls:
            if pending:
                return False
            pending = {call.id for call in message.tool_calls}
        elif message.role == "tool":
            if message.tool_call_id not in pending:
                return False
            pending.discard(message.tool_call_id)
        elif pending:
            return False
    return not pending


def encode_material(
    messages: Sequence[Message],
    reference_inventory: list[dict[str, Any]],
    *,
    max_sealed: int,
    knowledge_read: KnowledgeReadState | None = None,
    knowledge_scope: KnowledgeScope | None = None,
    knowledge_sources: dict[str, str | None] | None = None,
    datarc_selection: DatarcSelectionSnapshot | None = None,
    segmented: bool = False,
) -> tuple[bytes, int]:
    """Compress the transcript; clear the oldest tool results until it fits ``max_sealed``.

    Returns the compressed bytes and the plaintext size. Raises ``ValueError`` when even a
    transcript without tool results does not fit.
    """
    from dataclasses import asdict

    from bowhead_core.context_segments import MAX_CONTEXT_BYTES

    items = [asdict(message) if segmented else _message_dict(message) for message in messages]
    tool_indexes = [index for index, item in enumerate(items) if item["role"] == "tool"]
    cleared = 0
    while True:
        plain = json.dumps(
            {
                **({"storage_format": "segmented.v1"} if segmented else {}),
                "schema_version": 4 if datarc_selection is not None else 3 if knowledge_scope is not None else 2 if knowledge_read is not None else 1,
                **({"datarc_selection": datarc_selection.model_dump(mode="json")} if datarc_selection is not None else {}),
                **(
                    {"knowledge_scope": knowledge_scope.model_dump(mode="json"), "knowledge_sources": knowledge_sources or {}}
                    if knowledge_scope is not None
                    else {}
                ),
                "messages": items,
                "reference_inventory": reference_inventory,
                **({"knowledge_read": knowledge_read.model_dump()} if knowledge_read is not None else {}),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        packed = zlib.compress(plain, 6)
        if segmented:
            if len(plain) > MAX_CONTEXT_BYTES or len(packed) > MAX_CONTEXT_BYTES:
                raise ValueError("resume_checkpoint_storage_limit")
            return packed, len(plain)
        # Base64 inside the sealed envelope grows the payload by a third.
        if len(plain) <= MAX_PLAIN_BYTES and len(packed) * 4 // 3 + 1024 <= max_sealed:
            return packed, len(plain)
        if cleared >= len(tool_indexes):
            raise ValueError("resume_checkpoint_too_large")
        # Clear a quarter of the remaining results per pass.
        step = max(1, (len(tool_indexes) - cleared) // 4)
        for index in tool_indexes[cleared : cleared + step]:
            items[index]["content"] = _CLEARED
        cleared += step


def decode_material(packed: bytes, *, segmented: bool = False) -> ResumeMaterial:
    from bowhead_core.context_segments import MAX_CONTEXT_BYTES, _message

    limit = MAX_CONTEXT_BYTES if segmented else MAX_PLAIN_BYTES
    inflater = zlib.decompressobj()
    plain = inflater.decompress(packed, limit + 1)
    if len(plain) > limit or inflater.unconsumed_tail or not inflater.eof or inflater.unused_data:
        raise ValueError("resume_checkpoint_too_large")
    value = json.loads(plain)
    if isinstance(value, dict):
        storage_format = value.pop("storage_format", None)
        if storage_format not in (None, "segmented.v1") or (storage_format is not None) != segmented:
            raise ValueError("resume_checkpoint_storage_format")
    if not isinstance(value, dict) or value.get("schema_version") not in {1, 2, 3, 4}:
        raise ValueError("resume_checkpoint_version")
    knowledge_read = (
        KnowledgeReadState.model_validate(value.get("knowledge_read"))
        if value["schema_version"] == 2 or (value["schema_version"] in {3, 4} and "knowledge_read" in value)
        else None
    )
    if value["schema_version"] == 3:
        knowledge_scope = KnowledgeScope.model_validate_json(json.dumps(value.get("knowledge_scope")))
        knowledge_sources = value.get("knowledge_sources")
    elif value["schema_version"] == 4:
        if type(value["schema_version"]) is not int:
            raise ValueError("resume_checkpoint_version")
        knowledge_scope = (
            KnowledgeScope.model_validate_json(json.dumps(value["knowledge_scope"])) if value.get("knowledge_scope") is not None else None
        )
        knowledge_sources = value.get("knowledge_sources", {})
    else:
        knowledge_scope = knowledge_sources = None
    if value["schema_version"] not in {3, 4} and ("knowledge_scope" in value or "knowledge_sources" in value):
        raise ValueError("resume_checkpoint_version")
    if value["schema_version"] in {3, 4} and (
        not isinstance(knowledge_sources, dict)
        or any(not isinstance(k, str) or (v is not None and not isinstance(v, str)) for k, v in knowledge_sources.items())
    ):
        raise ValueError("resume_checkpoint_shape")
    if value["schema_version"] == 1 and "knowledge_read" in value:
        raise ValueError("resume_checkpoint_version")
    if value["schema_version"] != 4 and "datarc_selection" in value:
        raise ValueError("resume_checkpoint_version")
    datarc_selection = DatarcSelectionSnapshot.model_validate(value.get("datarc_selection")) if value["schema_version"] == 4 else None
    if value["schema_version"] == 4 and not set(value) <= {
        "schema_version",
        "messages",
        "reference_inventory",
        "knowledge_scope",
        "knowledge_sources",
        "knowledge_read",
        "datarc_selection",
    }:
        raise ValueError("resume_checkpoint_shape")
    raw_messages = value.get("messages")
    inventory = value.get("reference_inventory")
    if (
        not isinstance(raw_messages, list)
        or not raw_messages
        or (not segmented and len(raw_messages) > MAX_MESSAGES)
        or not isinstance(inventory, list)
    ):
        raise ValueError("resume_checkpoint_shape")
    messages: list[Message] = []
    for item in raw_messages:
        if segmented:
            messages.append(_message(item))
            continue
        if (
            not isinstance(item, dict)
            or set(item) != {"role", "content", "name", "tool_call_id", "tool_calls"}
            or item["role"] not in {"user", "assistant", "tool"}
            or not isinstance(item["content"], str)
        ):
            raise ValueError("resume_checkpoint_shape")
        calls = item["tool_calls"]
        tool_calls = None
        if calls is not None:
            if item["role"] != "assistant" or not isinstance(calls, list) or not calls:
                raise ValueError("resume_checkpoint_shape")
            tool_calls = []
            for call in calls:
                if (
                    not isinstance(call, dict)
                    or set(call) != {"id", "name", "arguments"}
                    or not isinstance(call["id"], str)
                    or not isinstance(call["name"], str)
                    or not isinstance(call["arguments"], dict)
                ):
                    raise ValueError("resume_checkpoint_shape")
                tool_calls.append(ToolCall(id=call["id"], name=call["name"], arguments=call["arguments"]))
        messages.append(
            Message(
                role=item["role"],
                content=item["content"],
                name=item["name"] if isinstance(item["name"], str) else None,
                tool_call_id=item["tool_call_id"] if isinstance(item["tool_call_id"], str) else None,
                tool_calls=tool_calls,
            )
        )
    if not transcript_is_resumable(messages):
        raise ValueError("resume_checkpoint_shape")
    return ResumeMaterial(
        messages=messages,
        reference_inventory=inventory,
        knowledge_read=knowledge_read,
        knowledge_scope=knowledge_scope,
        knowledge_sources=knowledge_sources,
        datarc_selection=datarc_selection,
    )


def closing_note(*, completion: str, stop_reason: str | None, partial_done: str | None, partial_remaining: str | None) -> str:
    """The assistant turn that ends a saved transcript, so the resumed run knows where it stopped."""
    if completion == "partial":
        lines = [f"I stopped here because this run reached its limit ({stop_reason or 'run limit'}) and returned partial work."]
        if partial_done:
            lines.append(f"Done: {partial_done}")
        if partial_remaining:
            lines.append(f"Remaining: {partial_remaining}")
        return "\n".join(lines)
    return "I finished this task and returned my result with subagent_return."


def resume_instruction(instruction: str, checkpoint: ResumeCheckpoint) -> str:
    text = instruction.strip()
    if checkpoint.completion == "partial":
        base = "Continue this task from where you stopped. Do not redo finished work; finish what remains."
    else:
        base = "Continue working on this task with the new request below. Reuse your earlier results where they still apply."
    tail = (
        " Re-run a query when the data, period or scope it covered may have changed."
        " When done, return the complete result with subagent_return, including what you found earlier that still applies."
    )
    return f"{base}{tail}\n\n{text}" if text else f"{base}{tail}"


def fallback_task(checkpoint: ResumeCheckpoint, instruction: str) -> str:
    """Task for a fresh helper when the saved run cannot be resumed."""
    parts = [checkpoint.task.strip()]
    if checkpoint.completion == "partial":
        parts.append("An earlier run of this task stopped at a limit before finishing.")
        if checkpoint.partial_done:
            parts.append(f"It reported as done: {checkpoint.partial_done}")
        if checkpoint.partial_remaining:
            parts.append(f"It reported as remaining: {checkpoint.partial_remaining}")
        parts.append("Its working notes are not available. Finish the remaining part; redo earlier steps only when you need their results.")
    if instruction.strip():
        parts.append(f"New request: {instruction.strip()}")
    return "\n\n".join(part for part in parts if part)


def _row(row: Sequence[Any]) -> ResumeCheckpoint:
    return ResumeCheckpoint(*row)


_COLUMNS = (
    "id, team_id, user_id, workspace_id, session_id, turn_id, run_subagent_id, profile, agent_id, agent_revision_id, "
    "tool_names_hash, task, completion, stop_reason, partial_done, partial_remaining, state, claimed_by_turn_id, "
    "created_at, expires_at, ciphertext"
)


async def capture(
    db_path: str,
    *,
    team_id: str,
    user_id: str,
    workspace_id: str,
    session_id: str,
    turn_id: str,
    run_subagent_id: str,
    profile: str,
    agent_id: str | None,
    agent_revision_id: str | None,
    tool_names: Iterable[str],
    task: str,
    completion: str,
    stop_reason: str | None,
    partial_done: str | None,
    partial_remaining: str | None,
    messages: Sequence[Message],
    reference_inventory: list[dict[str, Any]],
    knowledge_read: KnowledgeReadState | None = None,
    knowledge_scope: KnowledgeScope | None = None,
    knowledge_sources: dict[str, str | None] | None = None,
    datarc_selection: DatarcSelectionSnapshot | None = None,
    segmented: bool = False,
) -> str:
    """Seal and store a run; the previous checkpoint of the same lineage in this session is superseded."""
    from bowhead_core.chat_reference_storage import MAX_REFERENCE_STORAGE_BYTES

    if completion not in {"complete", "partial"}:
        raise ValueError("resume_checkpoint_completion")
    note = closing_note(completion=completion, stop_reason=stop_reason, partial_done=partial_done, partial_remaining=partial_remaining)
    transcript = [*messages, Message(role="assistant", content=note)]
    if not transcript_is_resumable(transcript):
        raise ValueError("resume_checkpoint_unpaired")
    checkpoint_id = f"rsc_{uuid4().hex[:20]}"
    scope = {"team_id": team_id or "-", "user_id": user_id, "workspace_id": workspace_id or "-", "session_id": session_id}

    def _seal() -> tuple[str, int, bytes | None, tuple[str, ...]]:
        packed, plain_bytes = encode_material(
            transcript,
            reference_inventory,
            max_sealed=MAX_REFERENCE_STORAGE_BYTES,
            knowledge_read=knowledge_read,
            knowledge_scope=knowledge_scope,
            knowledge_sources=knowledge_sources,
            datarc_selection=datarc_selection,
            segmented=segmented,
        )
        manifest, parts = None, ()
        if segmented:
            from bowhead_core.continuation_payload import seal_payload

            manifest, parts = seal_payload(
                packed, kind="helper", turn_id=turn_id, scope=_scope(scope), identity=checkpoint_id, purpose=PURPOSE, cipher=_cipher()
            )
        return _cipher().seal(scope=_scope(scope), purpose=PURPOSE, identity=checkpoint_id, content=manifest or packed), plain_bytes, manifest, parts

    # Serializing and compressing a long transcript takes long enough to stall other turns.
    ciphertext, plain_bytes, manifest, parts = await asyncio.to_thread(_seal)
    now = _now()
    async with open_shared_ctx(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            if datarc_selection is not None:
                await require_datarc_context_migration(db)
            await db.execute(
                "UPDATE subagent_resume_checkpoints SET state='superseded', ciphertext=NULL "
                "WHERE session_id=? AND run_subagent_id=? AND state IN ('live','claimed')",
                (session_id, run_subagent_id),
            )
            inserted = await db.execute(
                f"INSERT INTO subagent_resume_checkpoints({_COLUMNS}, payload_bytes) SELECT "  # noqa: S608
                "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'live',NULL,?,?,?,? "
                # A session deleted while the run was going keeps nothing.
                "WHERE EXISTS(SELECT 1 FROM sessions WHERE id=? AND deleted_at IS NULL)",
                (
                    checkpoint_id,
                    team_id,
                    user_id,
                    workspace_id,
                    session_id,
                    turn_id,
                    run_subagent_id,
                    profile,
                    agent_id,
                    agent_revision_id,
                    tool_names_hash(tool_names),
                    task[:4000],
                    completion,
                    stop_reason,
                    (partial_done or None) and partial_done[:4000],
                    (partial_remaining or None) and partial_remaining[:4000],
                    _iso(now),
                    _iso(now + timedelta(days=RETENTION_DAYS)),
                    ciphertext,
                    plain_bytes,
                    session_id,
                ),
            )
            if manifest is not None and inserted.rowcount:
                from bowhead_core.continuation_payload import write_payload_in_tx

                await write_payload_in_tx(db, manifest=manifest, ciphertexts=parts, scope=_scope(scope), identity=checkpoint_id)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return checkpoint_id


_RESUMABLE = (
    "ciphertext IS NOT NULL AND expires_at>? AND (state='live' OR (state='claimed' AND NOT EXISTS("
    "SELECT 1 FROM turns t WHERE t.id=claimed_by_turn_id AND t.ended_at IS NULL)))"
)


async def list_resumable(
    db_path: str, *, session_id: str, user_id: str, exclude_turn_id: str = "", limit: int = LIVE_LIST_LIMIT
) -> list[ResumeCheckpoint]:
    """Newest resumable runs of this user's session (not ones captured in ``exclude_turn_id``)."""
    async with (
        open_shared_ctx(db_path) as db,
        db.execute(
            f"SELECT {_COLUMNS.removesuffix(', ciphertext')} FROM subagent_resume_checkpoints "  # noqa: S608
            f"WHERE session_id=? AND user_id=? AND turn_id<>? AND {_RESUMABLE} "
            "AND EXISTS(SELECT 1 FROM sessions s WHERE s.id=session_id AND s.deleted_at IS NULL) "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (session_id, user_id, exclude_turn_id, _iso(_now()), int(limit)),
        ) as cursor,
    ):
        rows = await cursor.fetchall()
    return [_row(row) for row in rows]


async def record_requests(db_path: str, *, turn_id: str, checkpoint_ids: Sequence[str]) -> None:
    """Persist the checkpoints a Continue click asked this turn to resume."""
    ids = list(dict.fromkeys(checkpoint_ids))[:MAX_REQUESTS]
    if not ids:
        return
    now = _iso(_now())
    async with open_shared_ctx(db_path) as db:
        await db.executemany(
            "INSERT OR IGNORE INTO subagent_resume_requests(turn_id, seq, checkpoint_id, created_at) VALUES (?,?,?,?)",
            [(turn_id, seq, checkpoint_id, now) for seq, checkpoint_id in enumerate(ids)],
        )
        await db.commit()


async def requested_checkpoint_ids(db_path: str, *, turn_id: str) -> list[str]:
    async with (
        open_shared_ctx(db_path) as db,
        db.execute("SELECT checkpoint_id FROM subagent_resume_requests WHERE turn_id=? ORDER BY seq", (turn_id,)) as cursor,
    ):
        return [str(row[0]) for row in await cursor.fetchall()]


async def claim(db_path: str, *, checkpoint_id: str, session_id: str, user_id: str, turn_id: str) -> ResumeCheckpoint:
    """Reserve a checkpoint for ``turn_id``; idempotent for the same turn (recovery)."""
    now = _iso(_now())
    async with open_shared_ctx(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                f"SELECT {_COLUMNS} FROM subagent_resume_checkpoints WHERE id=? AND session_id=? AND user_id=?",  # noqa: S608
                (checkpoint_id, session_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise ResumeRefused("not_found")
            checkpoint = _row(row[:-1])
            ciphertext = row[-1]
            if checkpoint.turn_id == turn_id:
                raise ResumeRefused("same_turn")
            if checkpoint.state == "claimed" and checkpoint.claimed_by_turn_id == turn_id and ciphertext is not None:
                await db.rollback()
                return ResumeCheckpoint(*row)
            if checkpoint.state == "superseded":
                # A newer run of this lineage holds the latest work; never restart from an older one.
                raise ResumeRefused("superseded")
            if ciphertext is None or checkpoint.expires_at <= now:
                raise ResumeRefused("expired", checkpoint)
            async with db.execute(
                f"UPDATE subagent_resume_checkpoints SET state='claimed', claimed_by_turn_id=? WHERE id=? AND {_RESUMABLE}",  # noqa: S608
                (turn_id, checkpoint_id, now),
            ) as cursor:
                if cursor.rowcount != 1:
                    raise ResumeRefused("in_use")
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return ResumeCheckpoint(*row[:-5], "claimed", turn_id, row[-3], row[-2], ciphertext)


async def release(db_path: str, *, checkpoint_id: str, turn_id: str) -> None:
    """Return a claimed checkpoint when its resumed run produced no newer one."""
    async with open_shared_ctx(db_path) as db:
        await db.execute(
            "UPDATE subagent_resume_checkpoints SET state='live', claimed_by_turn_id=NULL WHERE id=? AND state='claimed' AND claimed_by_turn_id=?",
            (checkpoint_id, turn_id),
        )
        await db.commit()


async def retire(db_path: str, *, checkpoint_id: str) -> None:
    """Stop offering a run whose saved work the current Agent can no longer use."""
    async with open_shared_ctx(db_path) as db:
        await db.execute(
            "UPDATE subagent_resume_checkpoints SET state='superseded', ciphertext=NULL WHERE id=? AND state IN ('live','claimed')",
            (checkpoint_id,),
        )
        await db.commit()


async def open_material_async(checkpoint: ResumeCheckpoint, *, db_path=None) -> ResumeMaterial:
    """:func:`open_material` off the event loop (decryption and decompression of up to 8 MiB)."""
    return await asyncio.to_thread(open_material, checkpoint, db_path=db_path)


def open_material(checkpoint: ResumeCheckpoint, *, db_path=None) -> ResumeMaterial:
    if checkpoint.ciphertext is None:
        raise ResumeRefused("expired")
    scope = {
        "team_id": checkpoint.team_id or "-",
        "user_id": checkpoint.user_id,
        "workspace_id": checkpoint.workspace_id or "-",
        "session_id": checkpoint.session_id,
    }
    try:
        packed = _cipher().open(scope=_scope(scope), purpose=PURPOSE, identity=checkpoint.id, ciphertext=checkpoint.ciphertext)
        from bowhead_core.continuation_payload import PREFIX, open_payload

        segmented = packed.startswith(PREFIX)
        packed = open_payload(
            packed,
            db_path=db_path,
            kind="helper",
            turn_id=checkpoint.turn_id,
            scope=_scope(scope),
            identity=checkpoint.id,
            purpose=PURPOSE,
            cipher=_cipher(),
        )
        return decode_material(packed, segmented=segmented)
    except (ValueError, TypeError, json.JSONDecodeError, zlib.error) as exc:
        logger.warning("subagent_resume.unreadable checkpoint=%s error=%s", checkpoint.id, exc)
        raise ResumeRefused("unreadable") from exc


def resume_refusal_for(checkpoint: ResumeCheckpoint, *, profile: str, agent_revision_id: str | None, tool_names: Iterable[str]) -> str | None:
    """Why the current Agent cannot continue the saved transcript, or ``None``."""
    if checkpoint.profile != profile or (checkpoint.agent_revision_id or None) != (agent_revision_id or None):
        return "agent_changed"
    if checkpoint.tool_names_hash != tool_names_hash(tool_names):
        return "tools_changed"
    return None


async def purge_expired(db_path: str) -> int:
    """Drop the transcript of expired checkpoints; the metadata stays with its session."""
    async with open_shared_ctx(db_path) as db:
        cursor = await db.execute(
            "UPDATE subagent_resume_checkpoints SET state='purged', ciphertext=NULL WHERE ciphertext IS NOT NULL AND expires_at<=?",
            (_iso(_now()),),
        )
        await db.commit()
        return int(cursor.rowcount or 0)


def _age(created_at: str, now: datetime) -> str:
    try:
        minutes = int((now - datetime.fromisoformat(created_at)).total_seconds() // 60)
    except ValueError:
        return "unknown age"
    if minutes < 60:
        return f"{max(minutes, 0)} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


def render_resumable_block(checkpoints: Sequence[ResumeCheckpoint], *, now: datetime | None = None) -> str:
    """Turn-tail list of runs Lead can continue with ``subagent_resume``."""
    if not checkpoints:
        return ""
    current = now or _now()
    lines = [
        "<resumable_runs>",
        "Earlier helper runs in this session that can be continued in place: call subagent_spawn with resume_checkpoint_id=<checkpoint_id> and task=what to do now.",
        "- A run that stopped at a limit (partial): when the user asks to continue, resume it rather than dispatching a new helper.",
        "- A completed run: resume it when the user follows up on, refines or extends that same work; dispatch a new helper for a "
        "different task or when the data range, object or source changed.",
    ]
    for checkpoint in checkpoints:
        task = " ".join(checkpoint.task.split())[:200]
        status = f"partial, stopped at {checkpoint.stop_reason}" if checkpoint.completion == "partial" else "completed"
        lines.append(
            f'- checkpoint_id={checkpoint.id} agent={checkpoint.profile} status={status} {_age(checkpoint.created_at, current)} task="{task}"'
        )
        if checkpoint.completion == "partial" and checkpoint.partial_remaining:
            lines.append(f"  remaining: {' '.join(checkpoint.partial_remaining.split())[:300]}")
    lines.append("</resumable_runs>")
    return "\n".join(lines)
