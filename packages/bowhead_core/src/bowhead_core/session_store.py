"""Session / Turn 持久化（M1 最小版）.

只做能驱动前端 sidebar + history 的最小表：
    sessions(id, workspace_id, user_id, title, started_at, last_active_at, status, cost_usd)
    turns(id, session_id, seq, user_text, intent, status, started_at, ended_at)

M2 对齐 TD §1.2 完整结构（agent_calls / llm_calls / tool_calls / checkpoints / artifacts）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx

if TYPE_CHECKING:
    from bowhead_core.chat_reference_admission import ReferenceStartAdmission
    from bowhead_core.chat_reference_storage import ChatReferenceStore, QueuedReferenceInput, ReferenceScope
    from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.chat_turn_source import (
    ChatTurnUserSource,
    ReferenceExecutionUnavailableError,
    chat_turn_user_source_projection,
    snapshot_chat_turn_user_source,
)
from bowhead_core.document_revision_target import DocumentRevisionTarget
from bowhead_core.published_diagnostics import chat_recovery_action
from bowhead_core.terminal_protocol import (
    TERMINAL_PROTOCOL_TABLES,
    TerminalProtocolError,
    TerminalProtocolMalformedError,
    canonical_json_v1,
)
from bowhead_core.types import (
    LEGACY_TURN_WORKER_SETTLED_STATUSES,
    normalize_legacy_turn_status,
)


async def _begin_worker_transition(db: aiosqlite.Connection, *, turn_id: str, worker: DocumentTurnOwner | None) -> bool:
    """Fence a worker-owned state write and its terminal cleanup atomically."""
    if worker is None:
        return True  # Existing administrative/cancellation/recovery writers own their admission.
    from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable, require_current_worker_in_tx

    await db.execute("BEGIN IMMEDIATE")
    if worker.turn_id != turn_id:
        await db.rollback()
        return False
    try:
        await require_current_worker_in_tx(db, worker=worker)
    except OrdinaryContinuationUnavailable:
        await db.rollback()
        return False
    return True


#: Mirrors Alembic 0157 for the legacy initializer; see `SessionStore.initialize`.
SESSION_TITLE_SOURCE_COLUMN_DDL = "ALTER TABLE sessions ADD COLUMN title_source TEXT NOT NULL DEFAULT 'auto' CHECK(title_source IN ('auto','user'))"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    workspace_id    TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    title           TEXT,
    started_at      TEXT NOT NULL,
    last_active_at  TEXT NOT NULL,
    ended_at        TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    cost_usd        REAL NOT NULL DEFAULT 0.0,
    tokens_total    INTEGER NOT NULL DEFAULT 0,
    intents         TEXT NOT NULL DEFAULT '[]'       -- JSON array of seen intents
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_ws
    ON sessions(user_id, workspace_id, last_active_at DESC);

CREATE TABLE IF NOT EXISTS turns (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    seq             INTEGER NOT NULL,
    user_text       TEXT NOT NULL,
    intent          TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    final_answer    TEXT,
    tokens_input    INTEGER DEFAULT 0,
    tokens_output   INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0.0,
    generation_base_tokens_input  INTEGER NOT NULL DEFAULT 0,
    generation_base_tokens_output INTEGER NOT NULL DEFAULT 0,
    generation_base_cost_usd      REAL NOT NULL DEFAULT 0.0,
    digest_json     TEXT,             -- TurnDigest (mode=simple/aggregate/empty/failed)
    agent_type      TEXT,             -- B-4 / W11-6: who responded (lead / analyst / modeler / ...).
                                      -- 默 NULL = lead (frontend 不渲 badge,降默认噪音)
    UNIQUE(session_id, seq)
);

-- W12-3 (#74) session badges 真信号:
-- 用户进 session 时(前端 hydrate 调 setSessionSeen)记 last_seen_at;
-- list_sessions 时 join: unread = sessions.last_active_at > views.last_seen_at OR 没记录.
CREATE TABLE IF NOT EXISTS user_session_views (
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    last_seen_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, session_id)
);

-- W12-3b (#87) pinned 真信号: 用户显式 pin / unpin 一个 session.
-- list_sessions 时 left join 取 pinned_at IS NOT NULL → pinned=true.
CREATE TABLE IF NOT EXISTS user_session_pins (
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    pinned_at       TEXT NOT NULL,
    PRIMARY KEY (user_id, session_id)
);

-- Option A session archive: per-user work-list preference, deliberately
-- separate from sessions.deleted_at and from the shared content lifecycle.
CREATE TABLE IF NOT EXISTS user_session_archives (
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    archived_at     TEXT NOT NULL,
    PRIMARY KEY (user_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_user_session_archives_recent
    ON user_session_archives(user_id, archived_at DESC);

CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, seq);

-- D7c: the `messages` table was dead (created in M1, never read or written;
-- 0 rows DB-wide). Conversation history is reconstructed from turns + turn_events.
-- DDL removed; existing prod DBs keep an empty unreferenced table (drop manually).

CREATE TABLE IF NOT EXISTS turn_attachments (
    turn_id         TEXT NOT NULL REFERENCES turns(id),
    artifact_id     TEXT NOT NULL REFERENCES artifacts(id),
    seq             INTEGER NOT NULL,
    PRIMARY KEY (turn_id, artifact_id)
);
CREATE INDEX IF NOT EXISTS idx_turn_attachments_turn ON turn_attachments(turn_id, seq);

-- T1 session-level pinning：任一 turn 上传的附件自动 pin 到 session，后续 turn 起手
-- 会自动 surface 成轻量 stub（仅 id + 摘要）进 system prompt. 用户可通过 UI 取消 pin.
-- detached_at IS NULL = 仍生效；IS NOT NULL = 用户移除了.
CREATE TABLE IF NOT EXISTS session_attachments (
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    artifact_id     TEXT NOT NULL REFERENCES artifacts(id),
    pinned_at       TEXT NOT NULL,
    detached_at     TEXT,
    PRIMARY KEY (session_id, artifact_id)
);
CREATE INDEX IF NOT EXISTS idx_session_attachments_active
    ON session_attachments(session_id, detached_at);

-- chat-hydrate-overhaul: 持久化 per-turn trace steps (FE traceSteps[] 单源).
-- 写时机: turn done / cancel / error 三处, single batch INSERT (不是 per-event).
-- 读时机: get_session_detail 一次 IN-batch query for all turns 的 trace.
CREATE TABLE IF NOT EXISTS turn_trace_steps (
    turn_id      TEXT NOT NULL REFERENCES turns(id),
    seq          INTEGER NOT NULL,         -- 0-based 在 turn 内的最终顺序 (含 nested)
    step_id      TEXT NOT NULL,            -- FE TraceStep.id (eg "classify" / "tool_5e3a1b")
    kind         TEXT NOT NULL,            -- "running" | "success" | "error" | "pending"
    title        TEXT,                     -- 可选 (BE 留空, FE 端 re-derive 自 toolName)
    tool_name    TEXT,
    duration_ms  INTEGER,
    summary      TEXT,                     -- ≤1KB, 截断后的 result preview
    -- chat-hydrate-overhaul Round 5: nested subagent_spawn 内部 tool 走 subagent_id
    -- 关联到 spawn parent step. None = top-level (Lead 直接调). FE 用此字段把 nested
    -- 渲染到 spawn 节点下而非 sibling.
    subagent_id  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (turn_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_trace_turn ON turn_trace_steps(turn_id);

-- chat-hydrate-overhaul: 持久化 per-turn summary (FE TurnSummary).
-- 一行一 turn (PK turn_id), turn done 时 INSERT OR REPLACE.
CREATE TABLE IF NOT EXISTS turn_summaries (
    turn_id      TEXT PRIMARY KEY REFERENCES turns(id),
    total_ms     INTEGER NOT NULL DEFAULT 0,
    cost_text    TEXT,                          -- V1 缓存 "$0.012"
    cost_cents   INTEGER,                       -- 兜底 (V2 复用)
    tools_count  INTEGER NOT NULL DEFAULT 0,
    llm_count    INTEGER NOT NULL DEFAULT 0,
    tokens_input INTEGER,
    tokens_output INTEGER,
    raw_json     TEXT,                          -- 全 fields JSON, 后续加字段不改 schema
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- prod-bug-bash-039 #6 citation reform (0.39.2):
-- LLM emits [cite:N] (1-based seq); BE registry maps N → artifact_id at turn finalize.
-- Replaces unreliable [data:art_<hex>] LLM-emit pattern (per ι-15 教训).
-- SEPARATE from turn_attachments (which is user-upload-only invariant) — mixing would
-- break attachments_injector / artifact_store readers (per design r2.1 P0-NEW-2).
-- Written via batched record_turn_citations at narrative finalize site.
CREATE TABLE IF NOT EXISTS turn_citations (
    turn_id        TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,    -- 1-based; LLM emits [cite:N] where N == seq
    artifact_id    TEXT NOT NULL,       -- no FK: keep citation row if artifact GC'd
    label          TEXT,                -- human-friendly hint (FE chip tooltip)
    kind           TEXT NOT NULL DEFAULT 'data_ref',  -- data_ref | chart_section | tool_output | kb_chunk
    registered_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (turn_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_turn_citations_turn ON turn_citations(turn_id);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


_SESSION_ORIGINS = frozenset({"normal", "workflow_dryrun", "agent_eval"})
# Failure codes forwarded unchanged to the frontend in the recovery projection (all others become lead_internal_error).
_CREDIT_FAILURE_REASON_CODES = frozenset({"team_pool_exhausted", "user_credit_exhausted"})


async def _ensure_chat_session_in_tx(
    connection: aiosqlite.Connection,
    *,
    session_id: str,
    workspace_id: str,
    user_id: str,
    now: str,
    origin: str = "normal",
) -> bool:
    """First-winner Session creation for callers that already own a transaction."""

    if origin not in _SESSION_ORIGINS:
        raise ValueError("chat_session_origin_invalid")
    cursor = await connection.execute(
        "INSERT OR IGNORE INTO sessions(id,workspace_id,user_id,started_at,last_active_at,origin) VALUES (?,?,?,?,?,?)",
        (session_id, workspace_id, user_id, now, now, origin),
    )
    async with connection.execute(
        "SELECT workspace_id,user_id,origin FROM sessions WHERE id=?",
        (session_id,),
    ) as scope_cursor:
        scope = await scope_cursor.fetchone()
    if scope is None or str(scope[0]) != workspace_id or str(scope[1]) != user_id or str(scope[2]) != origin:
        raise ValueError("chat_session_scope_conflict")
    return cursor.rowcount == 1


def _chat_turn_user_source_json(
    source: ChatTurnUserSource,
) -> tuple[str, str, str | None]:
    attachment_ids_json = canonical_json_v1(source.attachment_ids)
    attachment_refs_json = canonical_json_v1(source.attachment_refs)
    parts_json = canonical_json_v1(source.parts) if source.parts is not None else None
    return attachment_ids_json, attachment_refs_json, parts_json


def _chat_turn_user_source_from_row(row: aiosqlite.Row) -> ChatTurnUserSource:
    from bowhead_core.chat_references import decode_input_document
    from bowhead_core.document_revision_target import DocumentRevisionTarget

    attachment_ids = json.loads(str(row["attachment_ids_json"]))
    attachment_refs = json.loads(str(row["attachment_refs_json"]))
    raw_parts = json.loads(str(row["parts_json"])) if row["parts_json"] is not None else None
    if not isinstance(attachment_ids, list) or not all(isinstance(item, str) for item in attachment_ids):
        raise ValueError("invalid_chat_turn_user_source")
    if not isinstance(attachment_refs, list) or not all(isinstance(item, dict) for item in attachment_refs):
        raise ValueError("invalid_chat_turn_user_source")
    if raw_parts is not None and (not isinstance(raw_parts, list) or not all(isinstance(item, dict) for item in raw_parts)):
        raise ValueError("invalid_chat_turn_user_source")
    return ChatTurnUserSource(
        turn_id=str(row["turn_id"]),
        session_id=str(row["session_id"]),
        user_id=str(row["user_id"]),
        workspace_id=str(row["workspace_id"]),
        user_text=str(row["user_text"]),
        attachment_ids=tuple(attachment_ids),
        attachment_refs=tuple(attachment_refs),
        parts=tuple(raw_parts) if raw_parts is not None else None,
        input_document=(decode_input_document(str(row["input_document_json"])) if row["input_document_json"] is not None else None),
        document_revision_target=(
            DocumentRevisionTarget.model_validate_json(row["document_revision_target_json"])
            if row["document_revision_target_json"] is not None
            else None
        ),
    )


async def _read_chat_turn_user_source_in_tx(
    connection: aiosqlite.Connection,
    *,
    turn_id: str,
    expected_user_id: str | None = None,
    expected_workspace_id: str | None = None,
) -> ChatTurnUserSource | None:
    if (expected_user_id is None) != (expected_workspace_id is None):
        raise ValueError("chat Turn source scope must be complete")
    where = "turn_id=?"
    params: tuple[str, ...] = (turn_id,)
    if expected_user_id is not None and expected_workspace_id is not None:
        where += " AND user_id=? AND workspace_id=?"
        params += (expected_user_id, expected_workspace_id)
    previous_factory = connection.row_factory
    connection.row_factory = aiosqlite.Row
    try:
        async with connection.execute(
            f"SELECT * FROM chat_turn_user_sources WHERE {where}",
            params,
        ) as cursor:
            row = await cursor.fetchone()
    finally:
        connection.row_factory = previous_factory
    return _chat_turn_user_source_from_row(row) if row is not None else None


_REFERENCE_TURN_SQL = (
    "(EXISTS(SELECT 1 FROM chat_turn_user_sources source WHERE source.turn_id=t.id AND source.input_document_json IS NOT NULL) "
    "OR EXISTS(SELECT 1 FROM chat_reference_plans plan WHERE plan.turn_id=t.id))"
)

_BLOCKED_REFERENCE_QUEUE_ANCHOR_SQL = (
    "(t.status='failed' AND t.error_kind='reference_queue_blocked' "
    "AND EXISTS(SELECT 1 FROM chat_reference_queued_inputs queued "
    "WHERE queued.materialized_turn_id=t.id AND queued.state='materialized') "
    "AND NOT EXISTS(SELECT 1 FROM chat_reference_plans plan WHERE plan.turn_id=t.id) "
    "AND NOT EXISTS(SELECT 1 FROM turn_agent_binding binding WHERE binding.turn_id=t.id) "
    "AND NOT EXISTS(SELECT 1 FROM turn_agent_execution execution WHERE execution.turn_id=t.id))"
)


async def _requires_reference_execution_in_tx(connection: aiosqlite.Connection, turn_id: str) -> bool:
    """Probe durable markers without parsing (or downgrading) unknown input.

    A retained plan, including a purge tombstone, still fences execution when
    the source body has been cleared. Neither runtime flags nor caller text
    can opt a structured Turn into the legacy adapter.
    """
    async with connection.execute(
        f"SELECT 1 FROM turns t WHERE t.id=? AND {_REFERENCE_TURN_SQL}",
        (turn_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row is not None


async def _is_blocked_reference_queue_anchor_in_tx(connection: aiosqlite.Connection, turn_id: str) -> bool:
    """Identify the only structured failed child allowed to carry a suffix."""
    async with connection.execute(
        f"SELECT 1 FROM turns t WHERE t.id=? AND {_BLOCKED_REFERENCE_QUEUE_ANCHOR_SQL}",
        (turn_id,),
    ) as cursor:
        return await cursor.fetchone() is not None


async def _insert_chat_turn_user_source_in_tx(
    connection: aiosqlite.Connection,
    *,
    source: ChatTurnUserSource,
    created_at: str,
) -> bool:
    """Insert one source or validate an exact idempotent replay in the same transaction."""

    if not isinstance(source, ChatTurnUserSource):
        raise TypeError("chat Turn user source is invalid")
    if (source.input_document is not None or source.document_revision_target is not None) and not connection.in_transaction:
        raise ValueError("chat_reference_source_transaction_required")
    source_snapshot = snapshot_chat_turn_user_source(source)
    if any(ref.get("selectedContext") is not None for ref in source_snapshot.attachment_refs):
        from bowhead_core.chat_attachment_access import validate_chat_attachment_access_in_connection

        await validate_chat_attachment_access_in_connection(
            connection,
            refs=source_snapshot.attachment_refs,
            runtime_ids=source_snapshot.attachment_ids,
            user_id=source_snapshot.user_id,
            workspace_id=source_snapshot.workspace_id,
            session_id=source_snapshot.session_id,
        )
    source_projection_json = canonical_json_v1(
        chat_turn_user_source_projection(source_snapshot),
    )
    attachment_ids_json, attachment_refs_json, parts_json = _chat_turn_user_source_json(
        source_snapshot,
    )
    async with connection.execute(
        "SELECT t.session_id,t.user_text,s.user_id,s.workspace_id FROM turns t JOIN sessions s ON s.id=t.session_id WHERE t.id=?",
        (source_snapshot.turn_id,),
    ) as scope_cursor:
        scope = await scope_cursor.fetchone()
    if scope is None or (
        str(scope[0]) != source_snapshot.session_id
        or str(scope[1]) != source_snapshot.user_text
        or str(scope[2]) != source_snapshot.user_id
        or str(scope[3]) != source_snapshot.workspace_id
    ):
        raise ValueError("chat_turn_user_source_scope_conflict")
    existing = await _read_chat_turn_user_source_in_tx(connection, turn_id=source_snapshot.turn_id)
    if existing is not None:
        if canonical_json_v1(chat_turn_user_source_projection(existing)) != source_projection_json:
            raise ValueError("chat_turn_user_source_conflict")
        return False
    if source_snapshot.document_revision_target is not None:
        from bowhead_core.document_revision_target import document_reuse_enabled
        from bowhead_core.documents.request import DocumentRequestError
        from bowhead_core.documents.revision import validate_revision_target_in_tx

        if not document_reuse_enabled():
            raise DocumentRequestError("document_revision_unavailable")
        await validate_revision_target_in_tx(connection, source=source_snapshot)
    cursor = await connection.execute(
        "INSERT OR IGNORE INTO chat_turn_user_sources("
        "turn_id,session_id,user_id,workspace_id,user_text,attachment_ids_json,"
        "attachment_refs_json,parts_json,created_at,input_document_json,document_revision_target_json"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            source_snapshot.turn_id,
            source_snapshot.session_id,
            source_snapshot.user_id,
            source_snapshot.workspace_id,
            source_snapshot.user_text,
            attachment_ids_json,
            attachment_refs_json,
            parts_json,
            created_at,
            source_snapshot.input_document.model_dump_json(exclude_none=True) if source_snapshot.input_document is not None else None,
            source_snapshot.document_revision_target.model_dump_json() if source_snapshot.document_revision_target is not None else None,
        ),
    )
    if cursor.rowcount == 1:
        # Commit current live-file associations before dispatch. Queued/recovered
        # sources can outlive their files; retain their source without reviving
        # deleted inputs. Readers still enforce readiness, ownership and scope.
        # Exact replays must not repair old rows.
        await connection.executemany(
            "INSERT OR IGNORE INTO turn_attachments(turn_id,artifact_id,seq) "
            "SELECT ?,id,? FROM artifacts WHERE id=? AND status='ready' AND deleted_at IS NULL",
            [(source_snapshot.turn_id, seq, artifact_id) for seq, artifact_id in enumerate(source_snapshot.attachment_ids)],
        )
        return True
    existing = await _read_chat_turn_user_source_in_tx(
        connection,
        turn_id=source_snapshot.turn_id,
    )
    if (
        existing is None
        or canonical_json_v1(
            chat_turn_user_source_projection(existing),
        )
        != source_projection_json
    ):
        raise ValueError("chat_turn_user_source_conflict")
    return False


CHAT_SPAWN_DISPATCH_ORPHAN_GRACE_SECONDS = 30


class SessionProtocolManagedError(RuntimeError):
    """The Session has persisted protocol state and rejects legacy lifecycle writes."""


class _LegacyContinuationRowError(ValueError):
    """One persisted legacy continuation row is deterministically invalid."""


@dataclass(frozen=True, slots=True)
class _LegacySteerProjection:
    texts: tuple[str, ...]
    submission_ids: tuple[str, ...]
    user_text: str
    attachment_ids: tuple[str, ...]
    attachment_refs: tuple[dict[str, Any], ...]
    source_parts: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _SteerBufferSegment:
    items: tuple[dict[str, Any], ...]
    suffix: tuple[dict[str, Any], ...]
    reference_submission_id: str | None


def _decode_steer_buffer(raw_json: str | None) -> list[dict[str, Any]]:
    try:
        buffer = json.loads(str(raw_json or "[]"))
    except (json.JSONDecodeError, TypeError) as exc:
        raise _LegacyContinuationRowError(
            "legacy continuation buffer is malformed",
        ) from exc
    if not isinstance(buffer, list) or any(
        not isinstance(item, dict)
        or not item.keys() <= {"text", "attachment_ids", "attachment_refs", "submission_id", "reference_submission_id", "ts"}
        or not isinstance(item.get("text", ""), str)
        or not isinstance(item.get("attachment_ids", []), list)
        or any(not isinstance(attachment_id, str) or not attachment_id for attachment_id in item.get("attachment_ids", []))
        or not isinstance(item.get("attachment_refs", []), list)
        or any(not isinstance(attachment_ref, dict) for attachment_ref in item.get("attachment_refs", []))
        or ("submission_id" in item and (not isinstance(item["submission_id"], str) or not item["submission_id"]))
        or (
            "reference_submission_id" in item
            and (
                not isinstance(item["reference_submission_id"], str)
                or not item["reference_submission_id"]
                or item.get("submission_id") != item["reference_submission_id"]
            )
        )
        for item in buffer
    ):
        raise _LegacyContinuationRowError(
            "legacy continuation buffer is malformed",
        )
    return buffer


def _segment_steer_buffer(raw_json: str | None) -> _SteerBufferSegment:
    """Validate one buffer and select only its ordered head segment.

    Adjacent legacy inputs retain their existing batching. Every reference
    pointer is a hard segment boundary and occupies one segment by itself.
    """
    buffer = _decode_steer_buffer(raw_json)
    buffer = [
        item
        for item in buffer
        if item.get("text") or bool(item.get("attachment_ids", [])) or bool(item.get("attachment_refs", [])) or "reference_submission_id" in item
    ]
    if not buffer:
        return _SteerBufferSegment(items=(), suffix=(), reference_submission_id=None)
    if isinstance(buffer[0].get("reference_submission_id"), str):
        return _SteerBufferSegment(
            items=(buffer[0],),
            suffix=tuple(buffer[1:]),
            reference_submission_id=buffer[0]["reference_submission_id"],
        )
    boundary = next(
        (index for index, item in enumerate(buffer) if isinstance(item.get("reference_submission_id"), str)),
        len(buffer),
    )
    return _SteerBufferSegment(
        items=tuple(buffer[:boundary]),
        suffix=tuple(buffer[boundary:]),
        reference_submission_id=None,
    )


async def _materialize_blocked_reference_recovery_child_in_tx(
    db: aiosqlite.Connection,
    *,
    reference_store: ChatReferenceStore | None,
    session_id: str,
    parent_turn_id: str,
    segment: _SteerBufferSegment,
    now: str,
) -> str | None:
    """Preserve one accepted reference after owner loss without executing it."""
    submission_id = segment.reference_submission_id
    if submission_id is None or len(segment.items) != 1:
        raise _LegacyContinuationRowError(
            "reference continuation segment is malformed",
        )
    from bowhead_core.chat_reference_runtime_store import REFERENCE_QUEUE_HOLDER_SQL
    from bowhead_core.chat_reference_storage import get_cancelled_queued_input_for_recovery_in_connection

    # Resolve the immutable accepted origin from the current queue holder.
    # The storage readers below still enforce the original actor and scope.
    async with db.execute(
        "SELECT q.accepted_parent_turn_id FROM chat_reference_queued_inputs q "
        "JOIN turns qh ON qh.id=? AND qh.session_id=q.session_id "
        "WHERE q.session_id=? AND q.client_submission_id=? AND " + REFERENCE_QUEUE_HOLDER_SQL,
        (parent_turn_id, session_id, submission_id),
    ) as cursor:
        origin = await cursor.fetchone()
    if origin is None:
        raise _LegacyContinuationRowError("reference continuation origin is unavailable")
    accepted_parent_turn_id = str(origin[0])
    try:
        recovered = await get_cancelled_queued_input_for_recovery_in_connection(
            db,
            session_id=session_id,
            accepted_parent_turn_id=accepted_parent_turn_id,
            submission_id=submission_id,
        )
        if recovered is None and reference_store is not None:
            recovered = await reference_store.get_queued_input_for_recovery_in_connection(
                db,
                session_id=session_id,
                accepted_parent_turn_id=accepted_parent_turn_id,
                submission_id=submission_id,
            )
    except ValueError as exc:
        raise _LegacyContinuationRowError(
            "reference continuation source is unavailable",
        ) from exc
    if recovered is None:
        raise _LegacyContinuationRowError(
            "reference continuation source is unavailable",
        )
    if recovered.state == "cancelled":
        if recovered.payload is not None or recovered.cancel_reason is None:
            raise _LegacyContinuationRowError(
                "reference continuation cancellation changed",
            )
        return None
    if recovered.state != "queued" or recovered.payload is None:
        raise _LegacyContinuationRowError(
            "reference continuation source is unavailable",
        )
    payload = recovered.payload
    head = segment.items[0]
    if (
        payload.input_document.display_text() != head.get("text", "")
        or payload.attachment_ids != head.get("attachment_ids", [])
        or payload.attachment_refs != head.get("attachment_refs", [])
    ):
        raise _LegacyContinuationRowError(
            "reference continuation source changed",
        )
    child_turn_id = (
        "t_"
        + hashlib.sha256(
            f"bowhead/reference-steer-recovery/v1\0{parent_turn_id}\0{submission_id}".encode(),
        ).hexdigest()[:12]
    )
    async with db.execute(
        "SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE session_id=?",
        (session_id,),
    ) as seq_cursor:
        seq_row = await seq_cursor.fetchone()
    seq = int(seq_row[0]) if seq_row else 1
    await db.execute(
        "INSERT OR IGNORE INTO turns(id,session_id,seq,user_text,status,started_at,ended_at,error,error_kind) VALUES (?,?,?,?,'failed',?,?,?,?)",
        (
            child_turn_id,
            session_id,
            seq,
            payload.input_document.display_text(),
            now,
            now,
            "accepted reference could not resume after dispatch owner loss",
            "reference_queue_blocked",
        ),
    )
    try:
        await _insert_chat_turn_user_source_in_tx(
            db,
            source=ChatTurnUserSource(
                turn_id=child_turn_id,
                session_id=session_id,
                user_id=recovered.scope.user_id,
                workspace_id=recovered.scope.workspace_id,
                user_text=payload.input_document.display_text(),
                attachment_ids=tuple(payload.attachment_ids),
                attachment_refs=tuple(payload.attachment_refs),
                parts=None,
                input_document=payload.input_document,
            ),
            created_at=now,
        )
    except ValueError as exc:
        raise _LegacyContinuationRowError(
            "reference continuation source conflicts",
        ) from exc
    await db.execute(
        "INSERT OR IGNORE INTO chat_turn_spawn_dispatch("
        "turn_id,session_id,source_submission_id,parent_turn_id,"
        "source_submission_ids_json,dispatch_kind,state,owner_id,"
        "lease_expires_at,created_at,updated_at"
        ") VALUES (?,?,?,?,?,'follow_up','failed',NULL,?,?,?)",
        (
            child_turn_id,
            session_id,
            submission_id,
            parent_turn_id,
            json.dumps([submission_id], separators=(",", ":")),
            now,
            now,
            now,
        ),
    )
    async with db.execute(
        "SELECT source_submission_id,parent_turn_id,state FROM chat_turn_spawn_dispatch WHERE turn_id=? AND session_id=?",
        (child_turn_id, session_id),
    ) as dispatch_cursor:
        dispatch = await dispatch_cursor.fetchone()
    if dispatch is None or tuple(dispatch) != (submission_id, parent_turn_id, "failed"):
        raise _LegacyContinuationRowError(
            "reference continuation dispatch conflicts",
        )
    await db.execute(
        "INSERT OR IGNORE INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,1,'turn_failed',?,?)",
        (
            f"evt_reference_recovery_failed_{child_turn_id}",
            child_turn_id,
            json.dumps(
                {
                    "turn_id": child_turn_id,
                    "reason_code": "reference_continuation_unavailable",
                    # Direct SQL bypasses TurnEventBus.emit, so add the card action here (D5).
                    "recovery_action": chat_recovery_action("reference_continuation_unavailable"),
                    "seq": 1,
                    "evt_seq": 1,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            now,
        ),
    )
    await db.execute(
        "INSERT OR IGNORE INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,2,'done',?,?)",
        (
            f"evt_reference_recovery_done_{child_turn_id}",
            child_turn_id,
            json.dumps(
                {
                    "turn_id": child_turn_id,
                    "status": "failed",
                    "error": "reference_queue_blocked",
                    "error_kind": "reference_queue_blocked",
                    "reason_code": "reference_continuation_unavailable",
                    "recovery_action": chat_recovery_action("reference_continuation_unavailable"),
                    "seq": 2,
                    "evt_seq": 2,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            now,
        ),
    )
    try:
        if reference_store is None:
            raise _LegacyContinuationRowError(
                "reference continuation source is unavailable",
            )
        settled = await reference_store.settle_blocked_queued_input_in_connection(
            db,
            recovery=recovered,
            blocked_turn_id=child_turn_id,
        )
    except ValueError as exc:
        raise _LegacyContinuationRowError(
            "reference continuation queue changed",
        ) from exc
    if not settled:
        raise _LegacyContinuationRowError(
            "reference continuation queue changed",
        )
    return child_turn_id


def _project_legacy_steer_buffer(
    raw_json: str | None,
) -> _LegacySteerProjection:
    """Validate and deterministically project one legacy steer buffer."""
    try:
        buffer = json.loads(str(raw_json or "[]"))
    except (json.JSONDecodeError, TypeError) as exc:
        raise _LegacyContinuationRowError(
            "legacy continuation buffer is malformed",
        ) from exc
    if not isinstance(buffer, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("text", ""), str)
        or not isinstance(item.get("attachment_ids", []), list)
        or any(not isinstance(attachment_id, str) or not attachment_id for attachment_id in item.get("attachment_ids", []))
        or not isinstance(item.get("attachment_refs", []), list)
        or any(not isinstance(attachment_ref, dict) for attachment_ref in item.get("attachment_refs", []))
        or ("submission_id" in item and (not isinstance(item["submission_id"], str) or not item["submission_id"]))
        or "reference_submission_id" in item
        for item in buffer
    ):
        raise _LegacyContinuationRowError(
            "legacy continuation buffer is malformed",
        )
    parts = tuple(item for item in buffer if item.get("text") or bool(item.get("attachment_ids", [])) or bool(item.get("attachment_refs", [])))
    texts = tuple(str(item.get("text", "")) for item in parts)
    attachment_ids = tuple(
        dict.fromkeys(attachment_id for item in parts for attachment_id in item.get("attachment_ids", []) if isinstance(attachment_id, str))
    )
    attachment_refs = tuple(ref for item in parts for ref in item.get("attachment_refs", []) if isinstance(ref, dict))
    submission_ids = tuple(dict.fromkeys(str(item["submission_id"]) for item in parts if isinstance(item.get("submission_id"), str)))
    return _LegacySteerProjection(
        texts=texts,
        submission_ids=submission_ids,
        user_text="\n\n".join(text for text in texts if text).strip(),
        attachment_ids=attachment_ids,
        attachment_refs=attachment_refs,
        source_parts=tuple(
            {
                "text": str(item.get("text", "")),
                "attachments": [ref for ref in item.get("attachment_refs", []) if isinstance(ref, dict)],
            }
            for item in parts
        ),
    )


_LEGACY_CONTINUATION_SETTLED_STATUS_SQL = "(" + ",".join(f"'{status}'" for status in sorted(LEGACY_TURN_WORKER_SETTLED_STATUSES)) + ")"
_DEFERRED_REFERENCE_PAUSE_RECOVERY_BATCH_SIZE = 32


async def _legacy_generation_floor_in_tx(
    db: aiosqlite.Connection,
    *,
    turn_id: str,
    lease_kind: str,
    lease_execution_generation: int,
) -> int:
    if lease_kind != "resume":
        return 0
    async with db.execute(
        "SELECT COALESCE(MAX(seq),0) FROM turn_events "
        "WHERE turn_id=? AND event_type='turn.resumed' "
        "AND json_valid(payload_json) "
        "AND CAST(json_extract(payload_json,'$.execution_generation') AS INTEGER)=?",
        (turn_id, lease_execution_generation),
    ) as floor_cursor:
        floor_row = await floor_cursor.fetchone()
    generation_floor = int(floor_row[0]) if floor_row else 0
    if generation_floor:
        return generation_floor
    # Without a persisted resume notice, no historical terminal can prove the
    # claimed generation. Fence every existing row behind the current floor.
    async with db.execute(
        "SELECT COALESCE(MAX(seq),0) FROM turn_events WHERE turn_id=?",
        (turn_id,),
    ) as max_cursor:
        max_row = await max_cursor.fetchone()
    return int(max_row[0]) if max_row else 0


async def _next_legacy_event_cursors_in_tx(
    db: aiosqlite.Connection,
    *,
    turn_id: str,
) -> tuple[int, int]:
    """Return the next row seq and replay cursor for direct repair writes.

    Old rows may predate ``evt_seq``. Newer idempotent writes may instead leave
    payload cursor gaps, so neither column alone is a safe restart floor.
    """
    async with db.execute(
        "SELECT COALESCE(MAX(seq),0),"
        "COALESCE(MAX(CASE WHEN json_valid(payload_json) "
        "THEN CAST(json_extract(payload_json,'$.evt_seq') AS INTEGER) "
        "ELSE 0 END),0),"
        "COALESCE(MAX(CASE WHEN json_valid(payload_json) "
        "THEN CAST(json_extract(payload_json,'$.seq') AS INTEGER) "
        "ELSE 0 END),0) FROM turn_events WHERE turn_id=?",
        (turn_id,),
    ) as cursor:
        row = await cursor.fetchone()
    row_floor = int(row[0]) if row else 0
    payload_floor = max(int(row[1]), int(row[2])) if row else 0
    return row_floor + 1, max(row_floor, payload_floor) + 1


async def _legacy_root_terminal_matches_in_tx(
    db: aiosqlite.Connection,
    *,
    turn_id: str,
    generation_floor: int,
    canonical_status: str,
    canonical_final_answer: str = "",
) -> bool:
    async with db.execute(
        "SELECT seq,event_type,payload_json FROM turn_events "
        "WHERE turn_id=? AND seq>? AND (event_type='done' OR "
        "(event_type='error' AND json_valid(payload_json) AND "
        "COALESCE(CAST(json_extract(payload_json,'$.parent_agent_id') AS TEXT),'')='')) "
        "ORDER BY seq DESC LIMIT 1",
        (turn_id, generation_floor),
    ) as terminal_cursor:
        wire_terminal = await terminal_cursor.fetchone()
    if wire_terminal is None:
        return False
    event_type = str(wire_terminal["event_type"])
    if event_type == "error":
        status_matches = normalize_legacy_turn_status(canonical_status) == "failed"
    else:
        try:
            terminal_payload = json.loads(str(wire_terminal["payload_json"]))
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(terminal_payload, dict):
            return False
        status_matches = normalize_legacy_turn_status(
            terminal_payload.get("status"),
        ) == normalize_legacy_turn_status(canonical_status)
    if not status_matches:
        return False
    expected_answer = canonical_final_answer.strip()
    if not expected_answer:
        return True
    # A matching answer after done is not a valid ordered tail: clients may
    # close the stream at the terminal frame and never render it.
    async with db.execute(
        "SELECT payload_json FROM turn_events WHERE turn_id=? AND seq>? AND seq<? AND event_type='final_answer' ORDER BY seq DESC LIMIT 1",
        (turn_id, generation_floor, int(wire_terminal["seq"])),
    ) as answer_cursor:
        answer_row = await answer_cursor.fetchone()
    if answer_row is None:
        return False
    try:
        answer_payload = json.loads(str(answer_row[0]))
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(isinstance(answer_payload, dict) and answer_payload.get("text") == canonical_final_answer)


async def _terminal_protocol_schema_mode(
    db: aiosqlite.Connection,
) -> Literal["legacy", "migrated"]:
    """Classify only complete pre-0008 and complete migrated schemas."""

    async with db.execute("PRAGMA table_info(turns)") as cursor:
        turn_columns = {row[1] for row in await cursor.fetchall()}
    async with db.execute("PRAGMA table_info(sessions)") as cursor:
        session_columns = {row[1] for row in await cursor.fetchall()}
    async with db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
        protocol_tables = {str(row[0]) for row in await cursor.fetchall()} & TERMINAL_PROTOCOL_TABLES
    has_turn_discriminator = "terminal_protocol_version" in turn_columns
    has_session_discriminator = "active_turn_protocol_version" in session_columns
    if not has_turn_discriminator and not has_session_discriminator and not protocol_tables:
        return "legacy"
    if has_turn_discriminator and has_session_discriminator and protocol_tables == TERMINAL_PROTOCOL_TABLES:
        return "migrated"
    raise SessionProtocolManagedError("terminal protocol schema is partial")


async def _cancel_running_plan_for_turn_in_tx(
    db: aiosqlite.Connection,
    *,
    turn_id: str,
    updated_at: str,
) -> bool:
    """Project one durable Turn cancellation onto its exact running Plan."""

    async with db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plans'",
    ) as table_cursor:
        if await table_cursor.fetchone() is None:
            return False
    cursor = await db.execute(
        "UPDATE plans SET status='cancelled',updated_at=? WHERE execution_turn_id=? AND status='running'",
        (updated_at, turn_id),
    )
    if cursor.rowcount == 1:
        return True
    async with db.execute(
        "SELECT status FROM plans WHERE execution_turn_id=?",
        (turn_id,),
    ) as plan_cursor:
        row = await plan_cursor.fetchone()
    return row is not None and str(row["status"]) == "cancelled"


async def _legacy_turn_protocol_filter(db: aiosqlite.Connection) -> str:
    """Return the legacy filter only for complete migrated or pre-0008 schema."""

    mode = await _terminal_protocol_schema_mode(db)
    return " AND terminal_protocol_version IS NULL" if mode == "migrated" else ""


async def _assert_legacy_session_lifecycle(
    db: aiosqlite.Connection,
) -> None:
    """Fence direct lifecycle writes against migration and v1 admission."""

    if await _terminal_protocol_schema_mode(db) != "legacy":
        raise SessionProtocolManagedError("session lifecycle requires protocol coordinator")


async def _read_exact_legacy_turn_or_fence_in_tx(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    turn_id: str,
    user_text: str,
    allow_exact_replay: bool,
) -> aiosqlite.Row | None:
    """Replay an exact legacy Turn or fence a fresh legacy insert."""

    async with db.execute(
        "SELECT * FROM turns WHERE id=?",
        (turn_id,),
    ) as cursor:
        existing = await cursor.fetchone()
    if existing is not None:
        existing_columns = set(existing.keys())
        protocol_version = existing["terminal_protocol_version"] if "terminal_protocol_version" in existing_columns else None
        if protocol_version is None:
            if not (str(existing["session_id"]) == session_id and str(existing["user_text"]) == user_text):
                raise ValueError("submission_turn_conflict")

            async with db.execute(
                "SELECT * FROM sessions WHERE id=?",
                (session_id,),
            ) as cursor:
                existing_session = await cursor.fetchone()
            if existing_session is None:
                raise TerminalProtocolMalformedError(
                    "legacy Turn Session is missing",
                )
            mode = await _terminal_protocol_schema_mode(db)
            if mode == "migrated":
                from bowhead_core.terminal_protocol_store import (
                    read_session_protocol_facts_in_tx,
                )

                facts = await read_session_protocol_facts_in_tx(
                    db,
                    existing_session,
                )
                if facts.has_v1_history:
                    raise SessionProtocolManagedError(
                        "Session is managed by Terminal Protocol v1",
                    )
                active_turn_id = facts.legacy_active_turn_id
            else:
                session_columns = set(existing_session.keys())
                active_turn_id = existing_session["active_turn_id"] if "active_turn_id" in session_columns else None
            if allow_exact_replay and str(existing["status"]) == "running" and active_turn_id == turn_id:
                return existing
            raise ValueError("submission_turn_conflict")

        async with db.execute(
            "SELECT * FROM sessions WHERE id=?",
            (str(existing["session_id"]),),
        ) as cursor:
            existing_session = await cursor.fetchone()
        if existing_session is None:
            raise TerminalProtocolMalformedError(
                "terminal Turn Session is missing",
            )
        from bowhead_core.terminal_protocol_store import (
            read_session_protocol_facts_in_tx,
        )

        await read_session_protocol_facts_in_tx(db, existing_session)
        raise SessionProtocolManagedError(
            "Turn is managed by Terminal Protocol v1",
        )

    mode = await _terminal_protocol_schema_mode(db)
    async with db.execute(
        "SELECT * FROM sessions WHERE id=?",
        (session_id,),
    ) as cursor:
        session = await cursor.fetchone()
    if session is None:
        raise ValueError("submission_session_scope_conflict")

    if mode == "migrated":
        from bowhead_core.terminal_protocol_store import (
            read_session_protocol_facts_in_tx,
        )

        facts = await read_session_protocol_facts_in_tx(db, session)
        if facts.has_v1_history:
            raise SessionProtocolManagedError(
                "Session is managed by Terminal Protocol v1",
            )
        active_turn_id = facts.legacy_active_turn_id
    else:
        session_columns = set(session.keys())
        active_turn_id = session["active_turn_id"] if "active_turn_id" in session_columns else None
    if active_turn_id is not None:
        raise ValueError("submission_session_active")
    return None


async def _read_legacy_continuation_parent_in_tx(
    db: aiosqlite.Connection,
    *,
    turn_id: str,
    session_id: str | None = None,
    allow_idle_session: bool = False,
    require_active_parent: bool = True,
    schema_mode: Literal["legacy", "migrated"] | None = None,
) -> tuple[aiosqlite.Row, aiosqlite.Row] | None:
    """Return one exact legacy continuation parent without crossing protocol."""

    mode = schema_mode or await _terminal_protocol_schema_mode(db)
    async with db.execute(
        "SELECT * FROM turns WHERE id=?",
        (turn_id,),
    ) as cursor:
        turn = await cursor.fetchone()
    if turn is None:
        return None
    persisted_session_id = str(turn["session_id"])
    if session_id is not None and persisted_session_id != session_id:
        return None
    async with db.execute(
        "SELECT * FROM sessions WHERE id=?",
        (persisted_session_id,),
    ) as cursor:
        session = await cursor.fetchone()
    if session is None:
        raise TerminalProtocolMalformedError(
            "continuation parent Session is missing",
        )

    if mode == "migrated":
        from bowhead_core.terminal_protocol_store import (
            read_session_protocol_facts_in_tx,
        )

        facts = await read_session_protocol_facts_in_tx(db, session)
        if turn["terminal_protocol_version"] is not None or facts.has_v1_history:
            raise SessionProtocolManagedError(
                "continuation parent is managed by Terminal Protocol v1",
            )
        active_turn_id = facts.legacy_active_turn_id
    else:
        session_columns = set(session.keys())
        active_turn_id = session["active_turn_id"] if "active_turn_id" in session_columns else None
    if require_active_parent and active_turn_id != turn_id and not (allow_idle_session and active_turn_id is None):
        return None
    return session, turn


async def _isolate_legacy_continuation_in_tx(
    db: aiosqlite.Connection,
    *,
    turn_id: str,
    session_id: str,
    now: str,
    reason: str,
    parent_is_legacy: bool,
    discard_malformed_buffer: bool = False,
) -> None:
    """Retire one legacy continuation marker without touching a v1 parent."""

    if parent_is_legacy:
        legacy_filter = await _legacy_turn_protocol_filter(db)
        session_protocol_filter = " AND active_turn_protocol_version IS NULL" if legacy_filter else ""
        await db.execute(
            "UPDATE turns SET status='failed',ended_at=COALESCE(ended_at,?),"
            "error=COALESCE(error,?),"
            "error_kind=COALESCE(error_kind,'legacy_protocol_isolated') "
            "WHERE id=? AND status='running'"
            f"{legacy_filter}",
            (now, reason, turn_id),
        )
        if discard_malformed_buffer:
            # This buffer has been proven structurally unreadable, so it cannot
            # be attributed to any future Turn. Retire it with the isolated
            # parent; keeping active=NULL plus opaque work would poison every
            # later admission in this Session.
            await db.execute(
                f"UPDATE sessions SET active_turn_id=NULL,steer_buffer_json='[]' WHERE id=? AND active_turn_id=?{session_protocol_filter}",
                (session_id, turn_id),
            )
        # For any other uncertain continuation, preserve both the active fence
        # and raw buffer. That is the fail-closed alternative to silently
        # assigning accepted work to a new Turn.
    await db.execute(
        "UPDATE chat_turn_spawn_dispatch SET state='failed',"
        "owner_id=NULL,lease_expires_at=?,updated_at=? "
        "WHERE turn_id=? AND state NOT IN ('terminal','failed')",
        (now, now, turn_id),
    )


ArchiveMutationResult = Literal["ok", "not_found", "active"]
ArchiveView = Literal["active", "archived"]
#: Keyset position in a session list: ``(sort timestamp, session id)``.
SessionListKey = tuple[str, str]


def _like_contains(term: str) -> str:
    """``LIKE ... ESCAPE '\\'`` pattern matching ``term`` literally anywhere."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@dataclass
class Session:
    id: str
    workspace_id: str
    user_id: str
    title: str | None
    started_at: str
    last_active_at: str
    ended_at: str | None
    status: str
    cost_usd: float
    tokens_total: int
    intents: list[str]
    # W12-3 (#74): session badges use real signals. Default False/None; calculated by the backend.
    skill_applied: bool = False
    # last_seen_at: ISO timestamp of the last view for this (user_id, session_id). None = never seen.
    # Populated only by the list_sessions(user_id=...) join; not by get_session.
    last_seen_at: str | None = None
    # W12-3b (#87): pinned_at — None = unpinned; ISO timestamp = pin time. Only filled by list_sessions joins.
    pinned_at: str | None = None
    # Option A: per-user archive projection. get_session intentionally leaves it None.
    archived_at: str | None = None
    # Transactional running signal. Unlike intents, this is authoritative.
    active_turn_id: str | None = None
    # feedback-0713 F2: R1 soft-delete timestamp. None = not deleted. get/ensure deliberately expose deleted sessions
    # for audits/deep links; turn admission reads this field to reject new turns (turn_service.is_session_deleted).
    deleted_at: str | None = None
    # workflow-solidify / Agent Eval: session origin. 'normal' = regular chat;
    # 'workflow_dryrun' / 'agent_eval' = the corresponding internal scratch session (filtered from default lists).
    origin: str = "normal"


@dataclass
class Turn:
    id: str
    session_id: str
    seq: int
    user_text: str
    intent: str | None
    status: str
    started_at: str
    ended_at: str | None
    final_answer: str | None
    cost_usd: float
    execution_generation: int = 1
    terminal_protocol_version: int | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    # W11-6: agent_type — Lead / Analyst / Modeler / Thinker. None = Lead (default)
    agent_type: str | None = None
    # Sprint π Phase 0 (0.10.17.1): structured error fields, NULL on old rows.
    error: str | None = None
    error_kind: str | None = None  # enum-via-app: timeout / network / cap / internal / ν1_unresolvable / llm_failed


@dataclass(frozen=True)
class TurnSpawnRecord:
    """一次 Lead 的 `subagent_spawn`（r4：turn 级历史用它把「派了活」渲回历史）。

    全部字段来自落库事件（`lead.subagent_summary` 为锚，`agent_spawned` 补 task；r7 起未完成轮没有 summary 时
    由 `peer_dispatched` 合成，``synthesized=True``——那时 status 记的是**轮**状态、画布写入情况未知），不含会重算的量。
    """

    call_id: str
    profile: str
    task: str
    rationale: str
    status: str
    failure_reason: str | None
    child_tool_count: int
    canvas_sections: tuple[tuple[str, str], ...]  # (kind, title)
    outcome: str
    delivery: str = ""
    canvas_mode: str = ""
    synthesized: bool = False


@dataclass
class TraceStepRow:
    """chat-hydrate-overhaul: 一条 trace step. 字段对齐 FE TraceStep."""

    turn_id: str
    seq: int
    step_id: str
    kind: str
    title: str | None
    tool_name: str | None
    duration_ms: int | None
    summary: str | None
    # Round 5: subagent_id links to the spawn parent. None = top-level.
    subagent_id: str | None = None


@dataclass
class TurnSummaryRow:
    """chat-hydrate-overhaul: turn-end summary. 对齐 FE TurnSummary."""

    turn_id: str
    total_ms: int
    cost_text: str | None
    cost_cents: int | None
    tools_count: int
    llm_count: int
    tokens_input: int | None
    tokens_output: int | None
    usage_partial: bool = False


@dataclass(frozen=True, slots=True)
class FirstTurnReadinessFacts:
    has_any_normal_turn: bool
    has_completed_turn: bool
    has_successful_turn: bool


@dataclass(frozen=True, slots=True)
class ChatSubmissionReceipt:
    team_id: str
    user_id: str
    client_submission_id: str
    workspace_id: str
    requested_session_id: str | None
    actual_session_id: str | None
    turn_id: str | None
    submission_kind: str
    submission_contract_version: str
    model_control_batch: str
    request_fingerprint: str
    outcome: str
    response: dict[str, Any]
    created_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class QueuedReferenceResource:
    kind: str
    label: str


@dataclass(frozen=True, slots=True)
class QueuedReferenceSubmission:
    client_submission_id: str
    display_text: str
    resources: tuple[QueuedReferenceResource, ...]
    attachment_count: int
    accepted_at: str


@dataclass(frozen=True, slots=True)
class TurnCancelReceipt:
    team_id: str
    user_id: str
    cancel_request_id: str
    workspace_id: str
    turn_id: str
    expected_execution_generation: int
    outcome: Literal["cancelled", "terminal", "generation_conflict"]
    turn_erased: bool
    created_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class ChatSubmissionClaim:
    kind: Literal[
        "claimed",
        "receipt",
        "pending",
        "conflict",
        "abandoned",
        "not_found",
        "auth_generation_inactive",
    ]
    receipt: ChatSubmissionReceipt | None = None
    lease_owner: str | None = None
    lease_version: int | None = None


class SessionStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    @property
    def db_path(self) -> str:
        """Public accessor — sister modules (RegisteredFactStore, etc.) construct
        peer stores at the same DB path without poking at private state."""
        return self._db_path

    async def chat_submission_protocol_schema_ready(self) -> bool:
        """Return whether the immutable batch-A protocol schema is installed.

        Readiness uses this narrow probe before advertising Composer A.  It is
        intentionally read-only and checks columns as well as table names so a
        partially applied migration cannot admit browser submissions.
        """
        required = {
            "turns": {
                "id",
                "execution_generation",
                "generation_base_tokens_input",
                "generation_base_tokens_output",
                "generation_base_cost_usd",
            },
            "chat_submission_receipts": {
                "team_id",
                "user_id",
                "client_submission_id",
                "request_fingerprint",
                "fingerprint_key_id",
                "auth_session_generation_hash",
                "response_json",
                "expires_at",
            },
            "chat_submission_inflight": {
                "team_id",
                "user_id",
                "client_submission_id",
                "fingerprint_key_id",
                "auth_session_generation_hash",
                "lease_owner",
                "lease_version",
                "lease_expires_at",
            },
            "chat_submission_abandon_tombstones": {
                "team_id",
                "user_id",
                "client_submission_id",
                "expires_at",
            },
            "chat_auth_session_generations": {
                "user_id",
                "auth_session_generation_hash",
                "state",
                "invalidated_at",
            },
            "chat_auth_user_generation_fences": {
                "user_id",
                "invalidated_before_epoch",
                "updated_at",
            },
            "chat_submission_fingerprint_epochs": {
                "fingerprint_key_id",
                "first_seen_at",
                "last_seen_at",
            },
            "security_key_epochs": {
                "purpose",
                "key_id",
                "activated_at",
                "retired_at",
                "accept_until",
            },
            "composer_telemetry_dedupe": {
                "user_id",
                "event_id",
                "occurred_at",
                "received_at",
            },
            "chat_turn_user_sources": {
                "turn_id",
                "session_id",
                "user_id",
                "workspace_id",
                "user_text",
                "attachment_ids_json",
                "attachment_refs_json",
                "parts_json",
                "created_at",
                "input_document_json",
            },
            "chat_attachment_bundle_members": {
                "bundle_artifact_id",
                "child_artifact_id",
            },
            "chat_turn_spawn_dispatch": {
                "turn_id",
                "session_id",
                "parent_turn_id",
                "source_submission_ids_json",
                "state",
                "lease_kind",
                "lease_execution_generation",
                "owner_id",
                "cancel_requested_at",
                "lease_expires_at",
            },
            "turn_cancel_receipts": {
                "team_id",
                "user_id",
                "cancel_request_id",
                "turn_id",
                "expected_execution_generation",
                "outcome",
                "turn_erased",
                "created_at",
                "expires_at",
            },
            "clarification_resolution_outbox": {
                "request_id",
                "turn_id",
                "state",
                "channel",
                "work_json",
                "payload_json",
                "lease_owner",
                "lease_expires_at",
                "attempt_count",
                "created_at",
                "updated_at",
            },
        }
        try:
            async with open_shared_ctx(self._db_path) as db:
                async with db.execute(
                    "SELECT version_num FROM alembic_version LIMIT 1",
                ) as head_cursor:
                    head_row = await head_cursor.fetchone()
                # Require schema at least 0010 rather than an exact match: every subsequent
                # revision invalidates exact matching (first triggered by 0011, graph-mode batch 1). Table/column/trigger
                # validation below checks the actual schema; the version only blocks databases not yet upgraded to 0010.
                if head_row is None:
                    return False
                try:
                    head_num = int(str(head_row[0]).split("_", 1)[0])
                except ValueError:
                    return False
                if head_num < 10:
                    return False
                for table, columns in required.items():
                    async with db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,),
                    ) as cursor:
                        if await cursor.fetchone() is None:
                            return False
                    async with db.execute(f"PRAGMA table_info({table})") as cursor:
                        actual = {str(row[1]) for row in await cursor.fetchall()}
                    if not columns.issubset(actual):
                        return False
                async with db.execute(
                    "SELECT tbl_name, sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    ("trg_turn_delete_cancel_receipt_tombstone",),
                ) as trigger_cursor:
                    trigger_row = await trigger_cursor.fetchone()
                if trigger_row is None or str(trigger_row[0]) != "turns":
                    return False
                trigger_sql = "".join(str(trigger_row[1] or "").lower().split())
                trigger_head = "createtriggertrg_turn_delete_cancel_receipt_tombstonebeforedeleteonturns"
                trigger_body = "beginupdateturn_cancel_receiptssetturn_erased=1whereturn_id=old.id;end"
                # 0179_account_deletion adds a guard that skips the tombstone only for rows
                # frozen by a due account purge; both installed forms are canonical.
                account_purge_guard = (
                    "whennotexists(select1fromaccount_deletion_rowsrjoinaccount_deletionsd"
                    "ond.operation_id=r.operation_idwherer.table_name='turns'"
                    "andr.row_key=json_array(old.\"id\")andd.state='purging'"
                    "andjulianday(d.purge_after)<=julianday('now')andd.lease_ownerisnotnull"
                    "andjulianday(d.lease_expires_at)>julianday('now')"
                    "andexists(select1fromusersuwhereu.id=d.user_idandu.status='deleted'))"
                )
                if trigger_sql not in {trigger_head + trigger_body, trigger_head + account_purge_guard + trigger_body}:
                    return False
        except Exception:
            return False
        return True

    async def cleanup_stale_running_turns(self, *, older_than_min: int = 10) -> int:
        """#136: 把 `started_at` 超过调用方年龄门的 untracked legacy turn
        标 'failed'. 防止旧 Gateway 中断后 DB 永远显示 running，
        导致 sidebar 上 'hasResume' 判定失误，用户切回去看不到内容.

        Sprint chat-redesign-2026-05-21 Phase 1.1: 同步清掉没有 accepted steer
        的 stale active pointer。非空 steer buffer 必须先转成 drain_pending，
        让紧随的 dispatch recovery 物化 child，不能丢失已确认工作。
        活跃 tracked lease 始终排除；Gateway 周期维护使用大于旧 writer
        最大执行窗的门限，不在 startup 零年龄扫描。

        返修复条数（log 用）.
        """
        from datetime import datetime as _dt
        from datetime import timedelta

        threshold = (_dt.now(tz=UTC) - timedelta(minutes=older_than_min)).isoformat()
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                schema_mode = await _terminal_protocol_schema_mode(db)
                protocol_filter = " AND terminal_protocol_version IS NULL" if schema_mode == "migrated" else ""
                aliased_protocol_filter = protocol_filter.replace(
                    "terminal_protocol_version",
                    "t.terminal_protocol_version",
                )

                async def is_pure_legacy_parent(
                    *,
                    turn_id: str,
                    session_id: str,
                    require_active_parent: bool,
                ) -> bool:
                    try:
                        parent = await _read_legacy_continuation_parent_in_tx(
                            db,
                            turn_id=turn_id,
                            session_id=session_id,
                            require_active_parent=require_active_parent,
                            schema_mode=schema_mode,
                        )
                    except (SessionProtocolManagedError, TerminalProtocolError):
                        return False
                    return parent is not None

                async with db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_turn_spawn_dispatch'",
                ) as table_cursor:
                    dispatch_exists = await table_cursor.fetchone() is not None
                row_count = 0
                if dispatch_exists:
                    # Snapshot only pre-D0b untracked rows. Tracked dispatches,
                    # including a lease that expired between the preceding
                    # recovery pass and this transaction, have exactly one
                    # owner: ``recover_expired_chat_spawn_dispatches``.
                    stale_query = (
                        "SELECT t.id,t.session_id,s.active_turn_id,"
                        "s.steer_buffer_json FROM turns t JOIN sessions s "
                        "ON s.id=t.session_id WHERE t.status='running' "
                        "AND t.started_at<? AND NOT EXISTS (SELECT 1 "
                        "FROM chat_turn_spawn_dispatch d WHERE d.turn_id=t.id)"
                        f"{aliased_protocol_filter}"
                    )
                    stale_params = (threshold,)
                else:
                    stale_query = f"SELECT id,session_id FROM turns WHERE status='running' AND started_at<?{protocol_filter}"
                    stale_params = (threshold,)
                async with db.execute(stale_query, stale_params) as stale_cursor:
                    stale_rows = await stale_cursor.fetchall()
                for stale_row in stale_rows:
                    turn_id = str(stale_row["id"])
                    # The supported pre-protocol cleanup schema predates both
                    # structured sources and their migration-owned tables.
                    if schema_mode == "migrated" and await _requires_reference_execution_in_tx(db, turn_id):
                        continue
                    session_id = str(stale_row["session_id"])
                    active_turn_id = stale_row["active_turn_id"] if dispatch_exists else None
                    has_buffer = False
                    if dispatch_exists and active_turn_id == turn_id:
                        raw_buffer = str(
                            stale_row["steer_buffer_json"] or "[]",
                        )
                        try:
                            parsed_buffer = json.loads(raw_buffer)
                            has_buffer = not isinstance(
                                parsed_buffer,
                                list,
                            ) or bool(parsed_buffer)
                        except json.JSONDecodeError:
                            has_buffer = True
                        if has_buffer:
                            # Publish the recovery marker before validating the
                            # session's protocol history. A mixed-history parent
                            # must remain untouched here, but recovery still
                            # needs a durable row through which to isolate the
                            # unsupported continuation instead of abandoning an
                            # accepted steer buffer without an owner.
                            await db.execute(
                                "INSERT INTO chat_turn_spawn_dispatch("
                                "turn_id,session_id,source_submission_id,"
                                "parent_turn_id,source_submission_ids_json,"
                                "dispatch_kind,state,owner_id,lease_expires_at,"
                                "created_at,updated_at) VALUES (?,?,NULL,NULL,"
                                "'[]','start','drain_pending',NULL,?,?,?)",
                                (
                                    turn_id,
                                    session_id,
                                    now,
                                    now,
                                    now,
                                ),
                            )
                    if not await is_pure_legacy_parent(
                        turn_id=turn_id,
                        session_id=session_id,
                        require_active_parent=False,
                    ):
                        continue
                    updated = await db.execute(
                        "UPDATE turns SET status='failed',error_kind='interrupted',"
                        "ended_at=?,final_answer=COALESCE(final_answer, "
                        "'[error] turn was interrupted by gateway restart') "
                        "WHERE id=? AND status='running' AND started_at<?",
                        (now, turn_id, threshold),
                    )
                    row_count += updated.rowcount
                    if dispatch_exists and updated.rowcount == 1 and active_turn_id == turn_id and not has_buffer:
                        await db.execute(
                            "UPDATE sessions SET active_turn_id=NULL,steer_buffer_json='[]' WHERE id=? AND active_turn_id=?",
                            (session_id, turn_id),
                        )

                # Complete pre-dispatch schemas have no central recovery
                # substrate. Preserve their historical terminal-pointer
                # cleanup; migrated deployments route every tracked state to
                # ``recover_expired_chat_spawn_dispatches`` instead.
                if not dispatch_exists:
                    terminal_query = (
                        "SELECT t.id,t.session_id FROM turns t JOIN sessions s "
                        "ON s.active_turn_id=t.id WHERE t.status!='running' "
                        f"AND t.started_at<?{aliased_protocol_filter}"
                    )
                    terminal_params = (threshold,)
                    async with db.execute(
                        terminal_query,
                        terminal_params,
                    ) as terminal_cursor:
                        terminal_rows = await terminal_cursor.fetchall()
                    for terminal_row in terminal_rows:
                        turn_id = str(terminal_row["id"])
                        session_id = str(terminal_row["session_id"])
                        if not await is_pure_legacy_parent(
                            turn_id=turn_id,
                            session_id=session_id,
                            require_active_parent=True,
                        ):
                            continue
                        await db.execute(
                            "UPDATE sessions SET active_turn_id=NULL,steer_buffer_json='[]' WHERE id=? AND active_turn_id=?",
                            (session_id, turn_id),
                        )
                await db.commit()
                return row_count
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def terminal_protocol_schema_mode(self) -> Literal["legacy", "migrated"]:
        """Return only a complete pre-0008 or complete sentinel-level schema."""

        async with open_shared_ctx(self._db_path) as db:
            return await _terminal_protocol_schema_mode(db)

    async def initialize(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with open_shared_ctx(self._db_path) as db:
            # Fail closed before the legacy initializer can mutate a database
            # that carries only part of the terminal-protocol migration.  A
            # complete legacy or complete migrated schema is safe to inspect;
            # a partial footprint must be repaired by the migration chain.
            await _terminal_protocol_schema_mode(db)
            await db.executescript(SCHEMA)
            await db.execute("BEGIN IMMEDIATE")
            # Incremental migration: ALTER existing turns tables that lack digest_json to add the column.
            # SQLite ADD COLUMN has no IF NOT EXISTS; check with PRAGMA first.
            cur = await db.execute("PRAGMA table_info(turns)")
            cols = {row[1] for row in await cur.fetchall()}
            if "digest_json" not in cols:
                await db.execute("ALTER TABLE turns ADD COLUMN digest_json TEXT")
            # W11-6: incremental agent_type migration
            if "agent_type" not in cols:
                await db.execute("ALTER TABLE turns ADD COLUMN agent_type TEXT")
            # Sprint η: incremental hot_state_json migration (HOT-layer persistence, BowheadState serialization)
            if "hot_state_json" not in cols:
                await db.execute("ALTER TABLE turns ADD COLUMN hot_state_json TEXT")
            # Sprint θ Phase 8c-A: incremental turn_schema_version migration
            # /resume checks this field for compatibility with the current server schema (per tech-design §5.5).
            # Old turns default to NULL = legacy_v1; new turns in 0.9.0+ write 'v2'.
            if "turn_schema_version" not in cols:
                await db.execute("ALTER TABLE turns ADD COLUMN turn_schema_version TEXT")
            # Sprint π Phase 0 (0.10.17.1 hotfix): incremental error + error_kind migration
            # Ship 0.10.17.1 first; mark_failed/mark_clarification_pending write these columns
            # from 0.11.0 onward. The 0.10.17.1 binary only needs tolerant reads (NULL for old rows).
            if "error" not in cols:
                await db.execute("ALTER TABLE turns ADD COLUMN error TEXT")
            if "error_kind" not in cols:
                await db.execute("ALTER TABLE turns ADD COLUMN error_kind TEXT")
            # Composer A same-Turn resume usage ledger. Alembic 0008 owns the
            # deployed schema; this legacy initializer mirrors the columns for
            # isolated core tests and local embedded stores.
            if "generation_base_tokens_input" not in cols:
                await db.execute("ALTER TABLE turns ADD COLUMN generation_base_tokens_input INTEGER NOT NULL DEFAULT 0")
            if "generation_base_tokens_output" not in cols:
                await db.execute("ALTER TABLE turns ADD COLUMN generation_base_tokens_output INTEGER NOT NULL DEFAULT 0")
            if "generation_base_cost_usd" not in cols:
                await db.execute("ALTER TABLE turns ADD COLUMN generation_base_cost_usd REAL NOT NULL DEFAULT 0.0")
            # Sprint δ: registered_facts + registered_facts_history tables (η pattern)
            await db.execute(
                "CREATE TABLE IF NOT EXISTS registered_facts ("
                "fact_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, "
                "project_id TEXT, user_id TEXT, fact_key TEXT NOT NULL, "
                "fact_type TEXT NOT NULL, value_json TEXT NOT NULL, source TEXT NOT NULL, "
                "is_user_corrected INTEGER NOT NULL DEFAULT 0, confidence INTEGER, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "source_session_id TEXT, source_turn_id TEXT)"
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_facts ON registered_facts("
                "workspace_id, COALESCE(project_id, ''), COALESCE(user_id, ''), fact_key)"
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_facts_corrected ON registered_facts(workspace_id, is_user_corrected)")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS registered_facts_history ("
                "history_id TEXT PRIMARY KEY, fact_id TEXT NOT NULL, "
                "workspace_id TEXT NOT NULL, fact_key TEXT NOT NULL, fact_type TEXT, "
                "value_json TEXT, source TEXT, superseded_at TEXT NOT NULL, "
                "superseded_by_session_id TEXT, superseded_by_turn_id TEXT)"
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_facts_history_key ON registered_facts_history(workspace_id, fact_key)")
            # W12-3 (#74): incremental sessions.skill_applied migration
            cur2 = await db.execute("PRAGMA table_info(sessions)")
            scols = {row[1] for row in await cur2.fetchall()}
            if "skill_applied" not in scols:
                await db.execute(
                    "ALTER TABLE sessions ADD COLUMN skill_applied INTEGER NOT NULL DEFAULT 0",
                )
            # Sprint chat-redesign-2026-05-21 Phase 1.1 — steer mode infra
            # active_turn_id: which turn is currently running (gateway uses to decide
            #   "intercept new msg as steer vs start new turn"). NULL = idle.
            # steer_buffer_json: queue of user msgs received during an active turn.
            #   Drained atomically at turn end + spawned as next turn (resume pattern).
            if "active_turn_id" not in scols:
                await db.execute(
                    "ALTER TABLE sessions ADD COLUMN active_turn_id TEXT",
                )
            if "steer_buffer_json" not in scols:
                await db.execute(
                    "ALTER TABLE sessions ADD COLUMN steer_buffer_json TEXT NOT NULL DEFAULT '[]'",
                )
            # workflow-solidify S3 (§7): sessions.origin — dry-run scratch sessions tagged
            # 'workflow_dryrun' so they're isolatable from normal chat (never touch real canvas).
            if "origin" not in scols:
                await db.execute(
                    "ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT 'normal'",
                )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_workspace_origin ON sessions(workspace_id, origin, id)",
            )
            # feedback-0713 R1: sessions.deleted_at — deleting a report also soft-deletes its source session.
            # Soft deletion only filters list_sessions; get_session/turns/trace remain auditable,
            # and scheduled-job run-history deep links still resolve (their semantics are unchanged).
            if "deleted_at" not in scols:
                await db.execute(
                    "ALTER TABLE sessions ADD COLUMN deleted_at TEXT",
                )
            # Session rename: Alembic 0157 owns the deployed column; this legacy initializer
            # mirrors the identical statement for isolated core stores (both sides idempotent).
            if "title_source" not in scols:
                await db.execute(SESSION_TITLE_SOURCE_COLUMN_DDL)
            # chat-hydrate-overhaul Round 5: incremental turn_trace_steps.subagent_id migration
            # (The schema lacked this column when Rounds 1–4 shipped; Round 5 needs it for flattened nested traces.)
            try:
                cur3 = await db.execute("PRAGMA table_info(turn_trace_steps)")
                tcols = {row[1] for row in await cur3.fetchall()}
                if tcols and "subagent_id" not in tcols:
                    await db.execute(
                        "ALTER TABLE turn_trace_steps ADD COLUMN subagent_id TEXT",
                    )
            except Exception:  # noqa: S110
                # Table absent (new database): CREATE IF NOT EXISTS already added the column; skip.
                pass
            # ─── Clean up stale running turns ────────────────────
            # Runtime recovery deliberately does not live in schema initialize.
            # Gateway first recovers tracked dispatch leases. Periodic
            # maintenance alone may run the age-bounded untracked cleanup and,
            # when it creates drain_pending, immediately invokes dispatch
            # recovery. An older initialize-time cleanup erased steer work.
            await db.commit()

    async def first_turn_readiness_facts_for_team(
        self,
        team_id: str,
    ) -> FirstTurnReadinessFacts:
        """Return content-free normal-turn evidence scoped through active spaces."""
        any_query = (
            "SELECT 1 FROM workspaces w "
            "JOIN sessions s ON s.workspace_id = w.id "
            "JOIN turns t ON t.session_id = s.id "
            "WHERE w.team_id = ? AND w.status = 'active' "
            "AND s.origin = 'normal' LIMIT 1"
        )
        completed_query = any_query.replace(
            "AND s.origin = 'normal' LIMIT 1",
            "AND s.origin = 'normal' AND t.status = 'completed' LIMIT 1",
        )
        successful_query = any_query.replace(
            "AND s.origin = 'normal' LIMIT 1",
            "AND s.origin = 'normal' AND t.status = 'completed' AND LENGTH(TRIM(COALESCE(t.final_answer, ''))) > 0 LIMIT 1",
        )
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                f"SELECT EXISTS({any_query}) AS has_any, EXISTS({completed_query}) AS has_completed, EXISTS({successful_query}) AS has_successful",
                (team_id, team_id, team_id),
            ) as cur,
        ):
            row = await cur.fetchone()
        return FirstTurnReadinessFacts(
            has_any_normal_turn=bool(row[0]) if row else False,
            has_completed_turn=bool(row[1]) if row else False,
            has_successful_turn=bool(row[2]) if row else False,
        )

    # ─── Session ────────────────────────────────────────────
    async def set_origin(self, session_id: str, origin: str) -> bool:
        """Tag an internal scratch producer after ``ensure_session``."""
        if origin not in _SESSION_ORIGINS:
            raise ValueError("chat_session_origin_invalid")
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "UPDATE sessions SET origin=? WHERE id=?",
                (origin, session_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def soft_delete_session(self, session_id: str) -> bool:
        """feedback-0713 R1: 软删会话（删报告联动）。

        只收敛 list_sessions 列表面; get_session/turns/trace 保留可审计,
        定时任务 run 历史深链仍可解析。幂等（重复调用返 False）。
        """
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _assert_legacy_session_lifecycle(db)
                cur = await db.execute(
                    "UPDATE sessions SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
                    (_now(), session_id),
                )
                await db.commit()
                return cur.rowcount > 0
            except BaseException:
                if db.in_transaction:
                    await db.rollback()
                raise

    async def delete_session(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> ArchiveMutationResult:
        """用户发起的会话软删（feedback-triage-2026-08-24：客户要「对话要可以删掉」）。

        软删而非硬删：`deleted_at` 一置，会话就从所有列表面消失、turn 准入层拒收新 turn
        （`turn_service.is_session_deleted`），但 turn/trace/账单流水保持可审计，
        定时任务的历史深链也仍能解析。

        与 `soft_delete_session` 的区别：那个是**不带 owner 收敛的裸原语**（删报告联动时
        由调用方先校验过）。`revive_session` 的注释已经点名警告过——裸 undelete 原语接到
        用户可控 session_id 上就是越权。所以用户面走这一个：owner 收敛写进 WHERE，
        并且照 `archive_session` 的规矩拒绝有活跃 turn 的会话（删掉一个正在跑的会话
        只会让那轮 turn 继续烧额度却无处可见）。

        幂等：已删或不属于该用户都返回 "not_found"（存在性一并隐藏）。
        """
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _assert_legacy_session_lifecycle(db)
                async with db.execute(
                    "SELECT s.active_turn_id, EXISTS("
                    "  SELECT 1 FROM turns t WHERE t.session_id=s.id AND t.status='running'"
                    ") AS has_running_turn "
                    "FROM sessions s "
                    "WHERE s.id=? AND s.user_id=? AND s.deleted_at IS NULL",
                    (session_id, user_id),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await db.execute("ROLLBACK")
                    return "not_found"
                if row[0] is not None or bool(row[1]):
                    await db.execute("ROLLBACK")
                    return "active"
                await db.execute(
                    "UPDATE sessions SET deleted_at=? WHERE id=? AND user_id=? AND deleted_at IS NULL",
                    (_now(), session_id, user_id),
                )
                await db.commit()
                return "ok"
            except BaseException:
                if db.in_transaction:
                    await db.rollback()
                raise

    async def undelete_session(self, *, user_id: str, session_id: str) -> bool:
        """撤销上一步的软删（用户面「撤销」用）。owner 收敛由 revive_session 落实。"""
        return await self.revive_session(session_id, user_id=user_id)

    async def revive_session(self, session_id: str, *, user_id: str | None = None) -> bool:
        """feedback-0713 F2: 复活软删会话（清 deleted_at, 回到列表可见）。

        仅供 IM 通道（feishu/teams）: IM 线程↔session 绑定是确定性的（无法换绑），
        用户在线程里再发消息=明确要继续用 → 复活消除「隐形计费」。web/resume/
        scheduler 走 410/失败拒绝（turn_service.is_session_deleted），不得调这里。
        幂等（未删返 False）。

        review 安全加固: 传 user_id 时 WHERE 收敛到 owner —— 防这个裸 undelete 原语
        将来被误接到用户可控 session_id（如 web「撤销删除」端点）成越权复活。IM caller
        必传其绑定链解析出的 user_id（与能 spawn turn 的 principal 同源）。
        """
        where = "id=? AND deleted_at IS NOT NULL"
        params: list = [session_id]
        if user_id is not None:
            where += " AND user_id=?"
            params.append(user_id)
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _assert_legacy_session_lifecycle(db)
                cur = await db.execute(f"UPDATE sessions SET deleted_at=NULL WHERE {where}", params)
                await db.commit()
                return cur.rowcount > 0
            except BaseException:
                if db.in_transaction:
                    await db.rollback()
                raise

    async def ensure_session(
        self,
        *,
        session_id: str | None,
        workspace_id: str,
        user_id: str,
        origin: str = "normal",
    ) -> Session:
        """拿到现有 session 或新建."""
        now = _now()
        sid = session_id or f"s_{uuid4().hex[:12]}"
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _ensure_chat_session_in_tx(
                    db,
                    session_id=sid,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    now=now,
                    origin=origin,
                )
                await db.commit()
            except BaseException:
                if db.in_transaction:
                    await db.rollback()
                raise
        persisted = await self.get_session(sid)
        if persisted is None:  # pragma: no cover - committed invariant
            raise RuntimeError("chat Session disappeared after ensure")
        return persisted

    async def get_session(self, session_id: str) -> Session | None:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sessions WHERE id=?",
                (session_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_session(row) if row else None

    async def workspace_ids_for_sessions(self, session_ids: list[str]) -> dict[str, str | None]:
        """`get_session(sid).workspace_id` 的批量版：``{session_id: workspace_id}``；不存在的会话不在字典里。

        与单条版同一张表、同样不加任何过滤。给一次要推很多产物归属的调用方（全局搜索的文件来源）用。
        """
        ids = list(dict.fromkeys(sid for sid in session_ids if sid))
        out: dict[str, str | None] = {}
        for start in range(0, len(ids), 500):  # Well below SQLite's bound-parameter limit
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            async with (
                open_shared_ctx(self._db_path) as db,
                db.execute(f"SELECT id, workspace_id FROM sessions WHERE id IN ({placeholders})", chunk) as cur,  # noqa: S608 - placeholders only
            ):
                for session_id, workspace_id in await cur.fetchall():
                    out[str(session_id)] = workspace_id
        return out

    async def list_sessions(
        self,
        *,
        user_id: str,
        workspace_id: str | None = None,
        q: str | None = None,
        limit: int = 50,
        before: str | None = None,
        since: str | None = None,
        archive_view: ArchiveView = "active",
        workspace_ids: Sequence[str] | None = None,
        after: SessionListKey | None = None,
        skip: tuple[SessionListKey, SessionListKey] | None = None,
        pinned_only: bool = False,
    ) -> list[Session]:
        """List the user's ordinary, undeleted sessions, newest first.

        Order is ``(last_active_at, id)`` descending for the active view and
        ``(archived_at, id)`` descending for the archived view; ``id`` breaks
        ties so keyset paging never skips or repeats rows.

        - ``q`` matches the title, the id, or the text of any user message in
          the session (titles are rewritten after the first turn, so the
          user's own words are often only in ``turns.user_text``).
        - ``workspace_ids`` is a readable-Space allowlist applied in SQL, so
          ``limit`` counts only rows the caller may see; an empty allowlist
          returns nothing.
        - ``after`` is the sort key of the last row already returned.
        - ``skip`` excludes an inclusive ``(newest, oldest)`` key range that
          the caller already received (the first sidebar page sends the
          newest older-than-a-week rows out of stream order).
        - ``before`` / ``since`` bound ``last_active_at`` (exclusive / inclusive).
        """
        if archive_view not in {"active", "archived"}:
            raise ValueError(f"unsupported archive view: {archive_view}")
        if workspace_ids is not None and not workspace_ids:
            return []

        # W12-3 + W12-3b: LEFT JOIN user_session_views (last_seen_at) +
        # user_session_pins (pinned_at). Archive is another per-user projection.
        sql = (
            "SELECT s.*, v.last_seen_at AS _last_seen_at, "
            "p.pinned_at AS _pinned_at, a.archived_at AS _archived_at "
            "FROM sessions s "
            "LEFT JOIN user_session_views v "
            "  ON v.session_id = s.id AND v.user_id = ? "
            "LEFT JOIN user_session_pins p "
            "  ON p.session_id = s.id AND p.user_id = ? "
            "LEFT JOIN user_session_archives a   ON a.session_id = s.id AND a.user_id = ? "
            # feedback-0713 R1: exclude soft-deleted sessions from lists (see deleted_at migration in initialize).
            # Internal scratch producers are auditable by exact id but never appear
            # in the user's ordinary conversation history.
            "WHERE s.user_id=? AND s.deleted_at IS NULL AND s.origin='normal'"
        )
        params: list[Any] = [user_id, user_id, user_id, user_id]
        sql += " AND a.archived_at IS " + ("NULL" if archive_view == "active" else "NOT NULL")
        if workspace_id:
            sql += " AND s.workspace_id=?"
            params.append(workspace_id)
        if workspace_ids is not None:
            unique_ids = sorted(set(workspace_ids))
            sql += f" AND s.workspace_id IN ({','.join('?' for _ in unique_ids)})"
            params.extend(unique_ids)
        if pinned_only:
            sql += " AND p.pinned_at IS NOT NULL"
        if q and q.strip():
            like = _like_contains(q.strip())
            # Per-user scan: the outer query is already restricted to this user's
            # sessions through idx_sessions_user_ws, and the correlated lookup uses
            # idx_turns_session, so the cost is bounded by one user's own turns.
            sql += (
                " AND (s.title LIKE ? ESCAPE '\\' OR s.id LIKE ? ESCAPE '\\'"
                " OR EXISTS (SELECT 1 FROM turns t WHERE t.session_id = s.id AND t.user_text LIKE ? ESCAPE '\\'))"
            )
            params.extend((like, like, like))
        # sidebar-older-bucket: filter the older group by an upper bound on last_active_at, with its own
        # query limit, so it does not compete with the last-seven-days window for a shared LIMIT.
        if before:
            sql += " AND s.last_active_at < ?"
            params.append(before)
        if since:
            sql += " AND s.last_active_at >= ?"
            params.append(since)
        sort_column = "a.archived_at" if archive_view == "archived" else "s.last_active_at"
        if after is not None:
            sql += f" AND ({sort_column} < ? OR ({sort_column} = ? AND s.id < ?))"
            params.extend((after[0], after[0], after[1]))
        if skip is not None:
            (hi_at, hi_id), (lo_at, lo_id) = skip
            # Inside the range means: key <= hi AND key >= lo.
            sql += f" AND NOT (({sort_column} < ? OR ({sort_column} = ? AND s.id <= ?)) AND ({sort_column} > ? OR ({sort_column} = ? AND s.id >= ?)))"
            params.extend((hi_at, hi_at, hi_id, lo_at, lo_at, lo_id))
        sql += f" ORDER BY {sort_column} DESC, s.id DESC LIMIT ?"
        params.append(limit)

        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        return [_row_to_session(r) for r in rows]

    async def list_session_workspace_ids(self, *, user_id: str) -> list[str]:
        """Distinct Spaces of the user's ordinary, undeleted sessions.

        Uses idx_sessions_user_ws for ``user_id`` and reads deleted_at/origin from the
        table rows, so the cost is one pass over this user's sessions.
        """
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                "SELECT DISTINCT workspace_id FROM sessions WHERE user_id=? AND deleted_at IS NULL AND origin='normal'",
                (user_id,),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return sorted(str(row[0]) for row in rows if row[0])

    async def first_matching_user_texts(
        self,
        *,
        session_ids: Sequence[str],
        q: str,
        max_chars: int = 2000,
    ) -> dict[str, str]:
        """``{session_id: text}``: the first ``max_chars`` of the earliest user message containing ``q``.

        Callers pass session ids they already authorized; sessions without a
        matching message are absent.
        """
        ids = list(dict.fromkeys(sid for sid in session_ids if sid))
        if not ids or not q.strip():
            return {}
        like = _like_contains(q.strip())
        out: dict[str, str] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            # SQLite's bare-column rule: with MIN(seq), user_text comes from that same row.
            # Only a bounded prefix is read back; the caller builds a short snippet from it.
            sql = (
                "SELECT t.session_id, MIN(t.seq), substr(t.user_text, 1, ?) FROM turns t "  # noqa: S608 - placeholders only
                f"WHERE t.session_id IN ({placeholders}) AND t.user_text LIKE ? ESCAPE '\\' "
                "GROUP BY t.session_id"
            )
            async with open_shared_ctx(self._db_path) as db, db.execute(sql, (max_chars, *chunk, like)) as cur:
                for session_id, _seq, user_text in await cur.fetchall():
                    out[str(session_id)] = str(user_text or "")
        return out

    async def count_archived_sessions(
        self,
        *,
        user_id: str,
        workspace_id: str | None = None,
        workspace_ids: Sequence[str] | None = None,
    ) -> int:
        """Count all owner/space archived sessions before q/limit filtering.

        ``workspace_ids`` is the same readable-Space allowlist ``list_sessions`` takes.
        """
        if workspace_ids is not None and not workspace_ids:
            return 0
        sql = (
            "SELECT COUNT(*) FROM user_session_archives a "
            "JOIN sessions s ON s.id = a.session_id "
            "WHERE a.user_id=? AND s.user_id=? AND s.deleted_at IS NULL AND s.origin='normal'"
        )
        params: list[str] = [user_id, user_id]
        if workspace_id:
            sql += " AND s.workspace_id=?"
            params.append(workspace_id)
        if workspace_ids is not None:
            unique_ids = sorted(set(workspace_ids))
            sql += f" AND s.workspace_id IN ({','.join('?' for _ in unique_ids)})"
            params.extend(unique_ids)
        async with open_shared_ctx(self._db_path) as db, db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_sessions(
        self,
        *,
        user_id: str,
        q: str,
        workspace_ids: Sequence[str],
    ) -> int:
        """Count the active-view rows ``list_sessions(user_id=…, q=…, workspace_ids=…)`` pages through.

        Same predicate as ``list_sessions`` for the active view: owned, undeleted,
        ordinary, not archived, inside the readable-Space allowlist, and ``q`` on the
        title, the id or any user message.
        """
        if not workspace_ids or not q.strip():
            return 0
        unique_ids = sorted(set(workspace_ids))
        like = _like_contains(q.strip())
        sql = (
            "SELECT COUNT(*) FROM sessions s "
            "LEFT JOIN user_session_archives a ON a.session_id = s.id AND a.user_id = ? "
            "WHERE s.user_id=? AND s.deleted_at IS NULL AND s.origin='normal' AND a.archived_at IS NULL"
            f" AND s.workspace_id IN ({','.join('?' for _ in unique_ids)})"
            " AND (s.title LIKE ? ESCAPE '\\' OR s.id LIKE ? ESCAPE '\\'"
            " OR EXISTS (SELECT 1 FROM turns t WHERE t.session_id = s.id AND t.user_text LIKE ? ESCAPE '\\'))"
        )
        params: list[str] = [user_id, user_id, *unique_ids, like, like, like]
        async with open_shared_ctx(self._db_path) as db, db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def archive_session(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> ArchiveMutationResult:
        """Archive an owned, undeleted, idle session in a serialized transaction."""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT s.active_turn_id, EXISTS("
                    "  SELECT 1 FROM turns t WHERE t.session_id=s.id AND t.status='running'"
                    ") AS has_running_turn "
                    "FROM sessions s "
                    "WHERE s.id=? AND s.user_id=? AND s.deleted_at IS NULL",
                    (session_id, user_id),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await db.execute("ROLLBACK")
                    return "not_found"
                if row[0] is not None or bool(row[1]):
                    await db.execute("ROLLBACK")
                    return "active"
                await db.execute(
                    "INSERT INTO user_session_archives(user_id, session_id, archived_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(user_id, session_id) DO NOTHING",
                    (user_id, session_id, _now()),
                )
                await db.commit()
                return "ok"
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def restore_session(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> ArchiveMutationResult:
        """Restore an owned, undeleted session; repeated restores are successful."""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT 1 FROM sessions WHERE id=? AND user_id=? AND deleted_at IS NULL",
                    (session_id, user_id),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await db.execute("ROLLBACK")
                    return "not_found"
                await db.execute(
                    "DELETE FROM user_session_archives WHERE user_id=? AND session_id=?",
                    (user_id, session_id),
                )
                await db.commit()
                return "ok"
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    # W12-3 (#74) session badges helpers ────────────────────────
    async def set_session_seen(self, *, user_id: str, session_id: str) -> None:
        """前端 hydrate session 时调,记 (user_id, session_id) last_seen_at = now."""
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "INSERT INTO user_session_views(user_id, session_id, last_seen_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, session_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (user_id, session_id, now),
            )
            await db.commit()

    async def mark_session_skill_applied(self, *, session_id: str) -> None:
        """turn finalize 时,若 invocations 含 skill_invoke 调一次. idempotent."""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "UPDATE sessions SET skill_applied=1 WHERE id=?",
                (session_id,),
            )
            await db.commit()

    # W12-3b (#87) pin / unpin helpers ────────────────────────
    async def pin_session(self, *, user_id: str, session_id: str) -> None:
        """idempotent pin. 多次调不变 pinned_at(用 ON CONFLICT DO NOTHING 而非 UPDATE)."""
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "INSERT INTO user_session_pins(user_id, session_id, pinned_at) VALUES (?, ?, ?) ON CONFLICT(user_id, session_id) DO NOTHING",
                (user_id, session_id, now),
            )
            await db.commit()

    async def unpin_session(self, *, user_id: str, session_id: str) -> None:
        """idempotent unpin."""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "DELETE FROM user_session_pins WHERE user_id=? AND session_id=?",
                (user_id, session_id),
            )
            await db.commit()

    # ─── Sprint chat-redesign-2026-05-21 Phase 1.1 — Steer mode infra ───────
    # Gateway uses these to decide "new user msg = steer (buffer + emoji_ack)
    # vs new turn". Turn-end atomically pops buffer + clears active in single
    # transaction so msgs arriving mid-pop aren't lost (R14 race).
    # ────────────────────────────────────────────────────────────────────────

    async def get_active_turn(self, session_id: str) -> str | None:
        """Returns active_turn_id (the turn currently running for this session),
        or None if no turn is active. Gateway uses this to decide whether a
        newly-arriving user msg should buffer (steer) or start a new turn.
        """
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                "SELECT active_turn_id FROM sessions WHERE id = ?",
                (session_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        if row is None:
            return None
        return row[0]  # None or str

    async def get_active_turn_identity(
        self,
        session_id: str,
    ) -> tuple[str | None, int | None]:
        """Return the active pointer and persisted protocol discriminator."""

        try:
            async with (
                open_shared_ctx(self._db_path) as db,
                db.execute(
                    """
                    SELECT active_turn_id,active_turn_protocol_version
                    FROM sessions WHERE id=?
                    """,
                    (session_id,),
                ) as cur,
            ):
                row = await cur.fetchone()
        except sqlite3.OperationalError as exc:
            if "no such column: active_turn_protocol_version" not in str(exc):
                raise
            # Only a zero-footprint pre-0008 database is legacy. A partial or
            # rolled-back 0008 schema must not erase the protocol identity.
            async with open_shared_ctx(self._db_path) as db:
                if await _terminal_protocol_schema_mode(db) != "legacy":
                    raise SessionProtocolManagedError("active turn protocol identity is unavailable") from exc
            return await self.get_active_turn(session_id), None
        if row is None:
            return None, None
        protocol_version = row[1]
        return row[0], (int(protocol_version) if protocol_version is not None else None)

    async def append_steer_buffer(
        self,
        *,
        session_id: str,
        text: str,
        expected_turn_id: str | None = None,
    ) -> bool:
        """Atomic R-M-W: append a user msg to steer_buffer_json IFF a turn
        is currently active. Returns True if appended, False if no active
        turn or if the caller's expected turn no longer matches.

        Re-checking active_turn_id inside BEGIN IMMEDIATE closes the race
        where gateway saw active=X, but worker X finished + cleared between
        the gateway's read and this append → would silently bury the msg.
        Tracked workers hand it to dispatch recovery; pre-D0b untracked rows
        are handled only after the age-bounded maintenance fence.
        """
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                schema_mode = await _terminal_protocol_schema_mode(db)
                if schema_mode == "migrated":
                    active_query = (
                        "SELECT s.active_turn_id,s.steer_buffer_json,"
                        "t.error_kind,d.state AS dispatch_state FROM sessions s "
                        "LEFT JOIN turns t ON t.id=s.active_turn_id "
                        "LEFT JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id "
                        "WHERE s.id=?"
                    )
                else:
                    active_query = "SELECT active_turn_id,steer_buffer_json,NULL,NULL FROM sessions WHERE id=?"
                async with db.execute(active_query, (session_id,)) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await db.execute("ROLLBACK")
                    return False
                active_turn_id, buf_json = row[0], row[1]
                if active_turn_id is None:
                    # No turn running — caller must route as fresh turn.
                    await db.execute("ROLLBACK")
                    return False
                if expected_turn_id and active_turn_id != expected_turn_id:
                    await db.execute("ROLLBACK")
                    return False
                if row[2] == "legacy_protocol_isolated" or row[3] == "failed":
                    # Isolation preserves the active pointer and previously
                    # accepted raw buffer as a fail-closed fence. It must never
                    # acknowledge additional steer work that no recovery owner
                    # can execute.
                    await db.execute("ROLLBACK")
                    return False
                buf = json.loads(buf_json or "[]")
                buf.append({"text": text, "ts": _now()})
                await db.execute(
                    "UPDATE sessions SET steer_buffer_json = ? WHERE id = ?",
                    (json.dumps(buf, ensure_ascii=False), session_id),
                )
                await db.commit()
                return True
            except BaseException:
                # rollback before close to release writer lock cleanly
                await db.execute("ROLLBACK")
                raise

    async def finalize_turn(
        self,
        *,
        session_id: str,
        current_turn_id: str,
        next_turn_id: str | None,
    ) -> list[str]:
        """Atomic turn-end pop + active swap (single BEGIN IMMEDIATE tx).

        Read steer_buffer. Two outcomes, both within one transaction:
        - **Empty buffer**: clear active_turn_id (set NULL). Return [].
        - **Non-empty buffer + next_turn_id**: SWAP active_turn_id to next_turn_id
          + clear buffer. Return the buffered texts.
        - **Non-empty buffer + next_turn_id=None**: clear active_turn_id + clear
          buffer. Return the buffered texts without reserving a follow-up turn.

        Caller spawns the follow-up turn iff this returns a non-empty list;
        the spawn target is `next_turn_id` when provided (already wired as active here, so
        any concurrent /send arriving mid-spawn routes straight to the new
        turn's steer path — no orphan window).

        If `active_turn_id != current_turn_id` (another path already swapped),
        we LEAVE state untouched and return [] — caller MUST skip spawn.
        """
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT active_turn_id, steer_buffer_json FROM sessions WHERE id = ?",
                    (session_id,),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await db.execute("ROLLBACK")
                    return []
                current_active, buf_json = row[0], row[1]
                # If another path already swapped active, don't touch state.
                if current_active is not None and current_active != current_turn_id:
                    await db.execute("ROLLBACK")
                    return []
                buf = json.loads(buf_json or "[]")
                texts = [item.get("text", "") for item in buf if isinstance(item, dict)]
                if texts:
                    if next_turn_id is not None:
                        await db.execute(
                            "UPDATE sessions SET active_turn_id = ?, steer_buffer_json = '[]' WHERE id = ?",
                            (next_turn_id, session_id),
                        )
                    else:
                        await db.execute(
                            "UPDATE sessions SET active_turn_id = NULL, steer_buffer_json = '[]' WHERE id = ?",
                            (session_id,),
                        )
                else:
                    await db.execute(
                        "UPDATE sessions SET active_turn_id = NULL, steer_buffer_json = '[]' WHERE id = ?",
                        (session_id,),
                    )
                await db.commit()
                return texts
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def set_title(self, *, session_id: str, title: str) -> bool:
        """Write an automatically generated title (LLM summary write-back).

        Only rows whose title is still automatic are touched: once the owner renames a
        session (`title_source='user'`), automatic writers must never overwrite it, even
        when a title generation started before the rename finishes after it. Returns
        whether the title was written.
        """
        async with open_shared_ctx(self._db_path) as db:
            cursor = await db.execute(
                "UPDATE sessions SET title=?, last_active_at=? WHERE id=? AND title_source='auto'",
                (title, _now(), session_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def rename_session(self, *, user_id: str, session_id: str, title: str) -> Session | None:
        """Set an owner-chosen title and mark it user-set so automatic titles never replace it.

        Owner and soft-deletion checks live in the WHERE clause; `None` means no owned,
        undeleted session matched. `last_active_at` is left alone so renaming does not
        reorder the session list.
        """
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "UPDATE sessions SET title=?, title_source='user' WHERE id=? AND user_id=? AND deleted_at IS NULL",
                (title, session_id, user_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            await db.commit()
            async with db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)) as cur:
                row = await cur.fetchone()
        return _row_to_session(row) if row is not None else None

    async def touch_session(
        self,
        *,
        session_id: str,
        title_hint: str | None = None,
        intent: str | None = None,
        cost_delta: float = 0.0,
        tokens_delta: int = 0,
    ) -> None:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT intents, title FROM sessions WHERE id=?", (session_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                return
            intents = set(json.loads(row["intents"] or "[]"))
            if intent:
                intents.add(intent)
            title = row["title"] or title_hint

            # The title was read outside this UPDATE; a rename committed in between is user-set
            # and must survive, so only automatic titles are written back.
            await db.execute(
                """
                UPDATE sessions SET
                    last_active_at=?,
                    cost_usd=cost_usd + ?,
                    tokens_total=tokens_total + ?,
                    intents=?,
                    title=CASE WHEN title_source='auto' THEN ? ELSE title END
                WHERE id=? AND status!='cancelled'
                """,
                (_now(), cost_delta, tokens_delta, json.dumps(sorted(intents)), title, session_id),
            )
            await db.commit()

    @staticmethod
    async def _rollup_session_usage(db: aiosqlite.Connection, turn_id: str) -> None:
        """D7b: recompute the session's token/cost aggregate from its turns.

        Recompute-from-SUM (NOT increment), so it is idempotent — a clarification
        turn that is marked pending then completed on resume can't be double-counted,
        and re-entrant terminal writes are safe. Was previously unwired (`touch_session`
        had zero callers), so every session showed tokens_total=0 / cost_usd=0.
        Caller passes an open connection so this runs inside the same transaction.
        """
        await db.execute(
            """
            UPDATE sessions SET
                tokens_total = COALESCE((
                    SELECT SUM(COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0))
                    FROM turns WHERE session_id = sessions.id), 0),
                cost_usd = COALESCE((
                    SELECT SUM(COALESCE(cost_usd, 0))
                    FROM turns WHERE session_id = sessions.id), 0)
            WHERE id = (SELECT session_id FROM turns WHERE id = ?)
            """,
            (turn_id,),
        )

    # ─── Turn ───────────────────────────────────────────────
    async def session_ids_for_turns(self, turn_ids: list[str]) -> dict[str, str]:
        """Map turn_id → session_id for the given turns. Used to deep-link scheduled-run results
        (the run records turn_id; the result page opens by session via /?session=)."""
        ids = list(dict.fromkeys(t for t in turn_ids if t))
        out: dict[str, str] = {}
        for start in range(0, len(ids), 500):  # Well below SQLite's bound-parameter limit; artifact ownership may require thousands of turns.
            chunk = ids[start : start + 500]
            placeholders = ",".join(["?"] * len(chunk))
            async with (
                open_shared_ctx(self._db_path) as db,
                db.execute(
                    f"SELECT id, session_id FROM turns WHERE id IN ({placeholders})",  # noqa: S608 — ids are placeholders
                    chunk,
                ) as cur,
            ):
                for turn_id, session_id in await cur.fetchall():
                    out[turn_id] = session_id
        return out

    # ─── Composer batch A submission receipts ────────────────────────
    async def chat_submission_fingerprint_keyring_aligned(
        self,
        *,
        configured_key_ids: tuple[str, ...],
        previous_accept_until: str | None = None,
    ) -> bool:
        """Align immutable DB epoch truth with the process current/previous pair.

        Normal rotation is accepted exactly once: the configured previous must
        equal the DB current, the new current must never have been retired, and
        the previous deadline is future-dated by at most eight days and becomes
        immutable when the new epoch is activated in the database.
        Expired orphan claims are converted to content-free abandonment fences
        so one crashed request cannot hold every replica unready forever.
        """
        if not configured_key_ids or len(configured_key_ids) > 2:
            return False
        current_key_id = configured_key_ids[0]
        previous_key_id = configured_key_ids[1] if len(configured_key_ids) == 2 else None
        now_dt = datetime.now(tz=UTC)
        now = now_dt.isoformat()
        accept_dt: datetime | None = None
        if previous_key_id is not None:
            if not previous_accept_until:
                return False
            try:
                accept_dt = datetime.fromisoformat(
                    previous_accept_until.replace("Z", "+00:00"),
                )
            except ValueError:
                return False
            if accept_dt.tzinfo is None or not (now_dt < accept_dt <= now_dt + timedelta(days=8)):
                return False
            accept_dt = accept_dt.astimezone(UTC)
        elif previous_accept_until:
            return False

        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM security_key_epochs WHERE purpose='chat_submission_fingerprint' ORDER BY activated_at",
                ) as cursor:
                    epochs = await cursor.fetchall()
                current_rows = [row for row in epochs if row["retired_at"] is None]
                if len(current_rows) > 1:
                    await db.execute("ROLLBACK")
                    return False
                current_row = current_rows[0] if current_rows else None
                historical_ids = {str(row["key_id"]) for row in epochs}

                if current_row is None:
                    if current_key_id in historical_ids:
                        await db.execute("ROLLBACK")
                        return False
                    activated_at = now
                    if previous_key_id is not None:
                        if previous_key_id in historical_ids:
                            await db.execute("ROLLBACK")
                            return False
                        await db.execute(
                            "INSERT INTO security_key_epochs("
                            "purpose,key_id,activated_at,retired_at,accept_until"
                            ") VALUES ('chat_submission_fingerprint',?,?,?,?)",
                            (
                                previous_key_id,
                                activated_at,
                                activated_at,
                                accept_dt.isoformat(),
                            ),
                        )
                    await db.execute(
                        "INSERT INTO security_key_epochs("
                        "purpose,key_id,activated_at,retired_at,accept_until"
                        ") VALUES ('chat_submission_fingerprint',?,?,NULL,NULL)",
                        (current_key_id, activated_at),
                    )
                elif str(current_row["key_id"]) != current_key_id:
                    if previous_key_id != str(current_row["key_id"]) or accept_dt is None or current_key_id in historical_ids:
                        await db.execute("ROLLBACK")
                        return False
                    unexpired_previous = [
                        row
                        for row in epochs
                        if row["retired_at"] is not None and datetime.fromisoformat(str(row["accept_until"])).astimezone(UTC) > now_dt
                    ]
                    if unexpired_previous:
                        await db.execute("ROLLBACK")
                        return False
                    activated_at = now
                    await db.execute(
                        "UPDATE security_key_epochs SET retired_at=?,accept_until=? "
                        "WHERE purpose='chat_submission_fingerprint' AND key_id=? "
                        "AND retired_at IS NULL",
                        (activated_at, accept_dt.isoformat(), previous_key_id),
                    )
                    await db.execute(
                        "INSERT INTO security_key_epochs("
                        "purpose,key_id,activated_at,retired_at,accept_until"
                        ") VALUES ('chat_submission_fingerprint',?,?,NULL,NULL)",
                        (current_key_id, activated_at),
                    )
                elif previous_key_id is not None and previous_key_id not in historical_ids:
                    await db.execute("ROLLBACK")
                    return False

                async with db.execute(
                    "SELECT key_id,retired_at,accept_until FROM security_key_epochs WHERE purpose='chat_submission_fingerprint'",
                ) as cursor:
                    epochs = await cursor.fetchall()
                accepted_db_ids = {
                    str(row["key_id"])
                    for row in epochs
                    if row["retired_at"] is None or datetime.fromisoformat(str(row["accept_until"])).astimezone(UTC) > now_dt
                }
                if accepted_db_ids != set(configured_key_ids):
                    await db.execute("ROLLBACK")
                    return False
                if previous_key_id is not None:
                    previous_row = next(row for row in epochs if str(row["key_id"]) == previous_key_id)
                    if datetime.fromisoformat(str(previous_row["accept_until"])).astimezone(UTC) != accept_dt:
                        await db.execute("ROLLBACK")
                        return False

                async with db.execute(
                    "SELECT i.* FROM chat_submission_inflight i "
                    "JOIN security_key_epochs e ON e.purpose='chat_submission_fingerprint' "
                    "AND e.key_id=i.fingerprint_key_id "
                    "WHERE e.accept_until IS NOT NULL AND e.accept_until<=? "
                    "AND i.lease_expires_at<=?",
                    (now, now),
                ) as cursor:
                    expired_inflight = await cursor.fetchall()
                expires_at = (now_dt + timedelta(days=30)).isoformat()
                for row in expired_inflight:
                    await db.execute(
                        "INSERT OR IGNORE INTO chat_submission_abandon_tombstones("
                        "team_id,user_id,client_submission_id,workspace_id,"
                        "requested_session_id,created_at,expires_at"
                        ") VALUES (?,?,?,?,?,?,?)",
                        (
                            str(row["team_id"]),
                            str(row["user_id"]),
                            str(row["client_submission_id"]),
                            str(row["workspace_id"]),
                            row["requested_session_id"],
                            now,
                            expires_at,
                        ),
                    )
                    await db.execute(
                        "DELETE FROM chat_submission_inflight WHERE team_id=? AND user_id=? AND client_submission_id=? AND lease_expires_at<=?",
                        (
                            str(row["team_id"]),
                            str(row["user_id"]),
                            str(row["client_submission_id"]),
                            now,
                        ),
                    )

                retry_floor = (now_dt - timedelta(days=8)).isoformat()
                async with db.execute(
                    "SELECT DISTINCT fingerprint_key_id FROM ("
                    "SELECT fingerprint_key_id FROM chat_submission_inflight "
                    "UNION ALL SELECT r.fingerprint_key_id "
                    "FROM chat_submission_receipts r JOIN security_key_epochs e "
                    "ON e.purpose='chat_submission_fingerprint' "
                    "AND e.key_id=r.fingerprint_key_id "
                    "WHERE r.created_at>=? AND "
                    "(e.accept_until IS NULL OR e.accept_until>?)"
                    ")",
                    (retry_floor, now),
                ) as cursor:
                    required_rows = await cursor.fetchall()
                required = {str(row[0]) for row in required_rows}
                if not required.issubset(set(configured_key_ids)):
                    await db.execute("ROLLBACK")
                    return False
                await db.commit()
                return True
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def claim_chat_submission(
        self,
        *,
        team_id: str,
        user_id: str,
        client_submission_id: str,
        workspace_id: str,
        requested_session_id: str | None,
        submission_kind: str,
        submission_contract_version: str,
        model_control_batch: str,
        request_fingerprint: str,
        accepted_request_fingerprints: tuple[str, ...],
        fingerprint_key_id: str,
        auth_session_generation_hash: str,
        lease_seconds: int = 30,
    ) -> ChatSubmissionClaim:
        """Claim a semantic submission before any Turn mutation."""
        now = _now()
        lease_expires_at = (datetime.now(tz=UTC) + timedelta(seconds=lease_seconds)).isoformat()
        lease_owner = f"sub_{uuid4().hex}"
        key = (team_id, user_id, client_submission_id)
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT state FROM chat_auth_session_generations WHERE user_id=? AND auth_session_generation_hash=?",
                    (user_id, auth_session_generation_hash),
                ) as auth_cursor:
                    auth_row = await auth_cursor.fetchone()
                if auth_row is None or str(auth_row["state"]) != "active":
                    await db.execute("ROLLBACK")
                    return ChatSubmissionClaim(kind="auth_generation_inactive")
                async with db.execute(
                    "SELECT key_id FROM security_key_epochs WHERE purpose='chat_submission_fingerprint' AND retired_at IS NULL",
                ) as epoch_cursor:
                    current_epoch = await epoch_cursor.fetchone()
                if current_epoch is None:
                    await db.execute(
                        "INSERT INTO security_key_epochs("
                        "purpose,key_id,activated_at,retired_at,accept_until"
                        ") VALUES ('chat_submission_fingerprint',?,?,NULL,NULL)",
                        (fingerprint_key_id, now),
                    )
                elif str(current_epoch["key_id"]) != fingerprint_key_id:
                    raise ValueError("submission_fingerprint_epoch_misaligned")
                await db.execute(
                    "INSERT INTO chat_submission_fingerprint_epochs("
                    "fingerprint_key_id,first_seen_at,last_seen_at) VALUES (?,?,?) "
                    "ON CONFLICT(fingerprint_key_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                    (fingerprint_key_id, now, now),
                )
                # Expiry is enforced in the claim transaction, not left to a
                # best-effort sweeper.  A quiet deployment may not run cleanup
                # for days; an expired receipt/tombstone must still stop
                # influencing the first request that reuses the identifier.
                await db.execute(
                    "DELETE FROM chat_submission_receipts WHERE team_id=? AND user_id=? "
                    "AND client_submission_id=? AND expires_at<=? AND (turn_id IS NULL "
                    "OR NOT EXISTS (SELECT 1 FROM turns t WHERE "
                    "t.id=chat_submission_receipts.turn_id AND "
                    "t.status NOT IN ('completed','failed','cancelled','aborted')))",
                    (*key, now),
                )
                await db.execute(
                    "DELETE FROM chat_submission_abandon_tombstones WHERE team_id=? "
                    "AND user_id=? AND client_submission_id=? AND expires_at<=? "
                    "AND NOT EXISTS (SELECT 1 FROM chat_submission_receipts r "
                    "WHERE r.team_id=? AND r.user_id=? AND r.client_submission_id=?) "
                    "AND NOT EXISTS (SELECT 1 FROM chat_submission_inflight i "
                    "WHERE i.team_id=? AND i.user_id=? AND i.client_submission_id=?)",
                    (*key, now, *key, *key),
                )
                async with db.execute(
                    "SELECT * FROM chat_submission_abandon_tombstones WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    key,
                ) as cur:
                    abandoned = await cur.fetchone()
                if abandoned is not None:
                    await db.execute("ROLLBACK")
                    kind = (
                        "abandoned"
                        if _submission_scope_matches(
                            abandoned,
                            workspace_id=workspace_id,
                            requested_session_id=requested_session_id,
                        )
                        else "not_found"
                    )
                    return ChatSubmissionClaim(kind=kind)

                async with db.execute(
                    "SELECT * FROM chat_submission_receipts WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    key,
                ) as cur:
                    receipt_row = await cur.fetchone()
                if receipt_row is not None:
                    await db.execute("ROLLBACK")
                    if not _submission_scope_matches(
                        receipt_row,
                        workspace_id=workspace_id,
                        requested_session_id=requested_session_id,
                    ):
                        return ChatSubmissionClaim(kind="not_found")
                    receipt = _row_to_submission_receipt(receipt_row)
                    if (
                        receipt.request_fingerprint not in accepted_request_fingerprints
                        or str(receipt_row["auth_session_generation_hash"]) != auth_session_generation_hash
                    ):
                        return ChatSubmissionClaim(kind="conflict")
                    return ChatSubmissionClaim(kind="receipt", receipt=receipt)

                async with db.execute(
                    "SELECT * FROM chat_submission_inflight WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    key,
                ) as cur:
                    inflight = await cur.fetchone()
                if inflight is not None:
                    if not _submission_scope_matches(
                        inflight,
                        workspace_id=workspace_id,
                        requested_session_id=requested_session_id,
                    ):
                        await db.execute("ROLLBACK")
                        return ChatSubmissionClaim(kind="not_found")
                    if (
                        str(inflight["request_fingerprint"]) not in accepted_request_fingerprints
                        or str(inflight["auth_session_generation_hash"]) != auth_session_generation_hash
                    ):
                        await db.execute("ROLLBACK")
                        return ChatSubmissionClaim(kind="conflict")
                    if str(inflight["lease_expires_at"]) >= now:
                        await db.execute("ROLLBACK")
                        return ChatSubmissionClaim(kind="pending")
                    lease_version = int(inflight["lease_version"]) + 1
                    await db.execute(
                        "UPDATE chat_submission_inflight SET lease_owner=?, lease_version=?, lease_expires_at=? "
                        "WHERE team_id=? AND user_id=? AND client_submission_id=?",
                        (lease_owner, lease_version, lease_expires_at, *key),
                    )
                else:
                    lease_version = 1
                    await db.execute(
                        "INSERT INTO chat_submission_inflight("
                        "team_id,user_id,client_submission_id,workspace_id,requested_session_id,"
                        "submission_kind,submission_contract_version,model_control_batch,"
                        "request_fingerprint,fingerprint_key_id,auth_session_generation_hash,"
                        "lease_owner,lease_version,lease_expires_at,created_at"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            team_id,
                            user_id,
                            client_submission_id,
                            workspace_id,
                            requested_session_id,
                            submission_kind,
                            submission_contract_version,
                            model_control_batch,
                            request_fingerprint,
                            fingerprint_key_id,
                            auth_session_generation_hash,
                            lease_owner,
                            lease_version,
                            lease_expires_at,
                            now,
                        ),
                    )
                await db.commit()
            except BaseException:
                await db.execute("ROLLBACK")
                raise
        return ChatSubmissionClaim(
            kind="claimed",
            lease_owner=lease_owner,
            lease_version=lease_version,
        )

    async def invalidate_chat_auth_session_generation(
        self,
        *,
        user_id: str,
        auth_session_generation_hash: str,
    ) -> bool:
        """Permanently fence one login chain, including before its first claim."""
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            cursor = await db.execute(
                "INSERT INTO chat_auth_session_generations("
                "user_id,auth_session_generation_hash,state,created_at,invalidated_at"
                ") VALUES (?,?,'invalidated',?,?) "
                "ON CONFLICT(user_id,auth_session_generation_hash) DO UPDATE SET "
                "state='invalidated',invalidated_at=COALESCE("
                "chat_auth_session_generations.invalidated_at,excluded.invalidated_at)",
                (user_id, auth_session_generation_hash, now, now),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def register_chat_auth_session_generation(
        self,
        *,
        user_id: str,
        auth_session_generation_hash: str,
        issued_at_epoch: float | None = None,
    ) -> bool:
        """Register a signed login chain without ever reviving an invalidated one."""
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT state FROM chat_auth_session_generations WHERE user_id=? AND auth_session_generation_hash=?",
                (user_id, auth_session_generation_hash),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                async with db.execute(
                    "SELECT invalidated_before_epoch FROM chat_auth_user_generation_fences WHERE user_id=?",
                    (user_id,),
                ) as cursor:
                    fence = await cursor.fetchone()
                if fence is not None and (issued_at_epoch is None or issued_at_epoch <= float(fence["invalidated_before_epoch"])):
                    await db.execute("ROLLBACK")
                    return False
                await db.execute(
                    "INSERT INTO chat_auth_session_generations("
                    "user_id,auth_session_generation_hash,state,created_at,invalidated_at"
                    ") VALUES (?,?,'active',?,NULL)",
                    (user_id, auth_session_generation_hash, now),
                )
                row = {"state": "active"}
            await db.commit()
        return str(row["state"]) == "active"

    async def is_chat_auth_session_generation_active(
        self,
        *,
        user_id: str,
        auth_session_generation_hash: str,
    ) -> bool:
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                "SELECT 1 FROM chat_auth_session_generations WHERE user_id=? AND auth_session_generation_hash=? AND state='active'",
                (user_id, auth_session_generation_hash),
            ) as cursor,
        ):
            return await cursor.fetchone() is not None

    async def invalidate_all_chat_auth_session_generations(
        self,
        *,
        user_id: str,
    ) -> int:
        """Fence every known Composer login chain for logout-everywhere."""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            count = await self._invalidate_all_chat_auth_session_generations_conn(db, user_id=user_id)
            await db.commit()
            return count

    @staticmethod
    async def _invalidate_all_chat_auth_session_generations_conn(db: aiosqlite.Connection, *, user_id: str) -> int:
        """Fence known and unseen login chains in the caller's identity transaction."""
        now = _now()
        cursor = await db.execute(
            "UPDATE chat_auth_session_generations SET state='invalidated',invalidated_at=? WHERE user_id=? AND state='active'",
            (now, user_id),
        )
        await db.execute(
            "INSERT INTO chat_auth_user_generation_fences(user_id,invalidated_before_epoch,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET invalidated_before_epoch=MAX("
            "chat_auth_user_generation_fences.invalidated_before_epoch,excluded.invalidated_before_epoch),updated_at=excluded.updated_at",
            (user_id, datetime.now(tz=UTC).timestamp(), now),
        )
        return cursor.rowcount

    async def cleanup_chat_submission_protocol(self, *, batch_size: int = 5000) -> int:
        """Delete one bounded batch of expired submission protocol state.

        Receipt and abandonment retention is exactly governed by ``expires_at``.
        In-flight rows do not contain prompt data and remain reclaimable through
        their lease; deleting them speculatively could turn an unknown outcome
        into a duplicate execution.  They are therefore deliberately excluded.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        now = _now()
        deleted_total = 0
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                receipt_cursor = await db.execute(
                    "DELETE FROM chat_submission_receipts WHERE rowid IN ("
                    "SELECT r.rowid FROM chat_submission_receipts r "
                    "WHERE r.expires_at<=? AND (r.turn_id IS NULL OR NOT EXISTS ("
                    "SELECT 1 FROM turns t WHERE t.id=r.turn_id AND "
                    "t.status NOT IN ('completed','failed','cancelled','aborted'))) "
                    "ORDER BY r.expires_at LIMIT ?)",
                    (now, batch_size),
                )
                deleted_total += max(receipt_cursor.rowcount, 0)
                tombstone_cursor = await db.execute(
                    "DELETE FROM chat_submission_abandon_tombstones WHERE rowid IN ("
                    "SELECT a.rowid FROM chat_submission_abandon_tombstones a "
                    "WHERE a.expires_at<=? AND NOT EXISTS (SELECT 1 FROM "
                    "chat_submission_receipts r WHERE r.team_id=a.team_id AND "
                    "r.user_id=a.user_id AND r.client_submission_id=a.client_submission_id) "
                    "AND NOT EXISTS (SELECT 1 FROM chat_submission_inflight i "
                    "WHERE i.team_id=a.team_id AND i.user_id=a.user_id AND "
                    "i.client_submission_id=a.client_submission_id) "
                    "ORDER BY a.expires_at LIMIT ?)",
                    (now, batch_size),
                )
                deleted_total += max(tombstone_cursor.rowcount, 0)
                cancel_cursor = await db.execute(
                    "DELETE FROM turn_cancel_receipts WHERE rowid IN ("
                    "SELECT c.rowid FROM turn_cancel_receipts c "
                    "WHERE c.expires_at<=? AND (c.turn_erased=1 OR NOT EXISTS ("
                    "SELECT 1 FROM turns t WHERE t.id=c.turn_id) OR EXISTS ("
                    "SELECT 1 FROM turns t WHERE t.id=c.turn_id AND "
                    "t.status IN ('completed','failed','cancelled','aborted'))) "
                    "ORDER BY c.expires_at LIMIT ?)",
                    (now, batch_size),
                )
                deleted_total += max(cancel_cursor.rowcount, 0)
                await db.commit()
            except BaseException:
                await db.execute("ROLLBACK")
                raise
        return deleted_total

    async def record_composer_telemetry_event(
        self,
        *,
        user_id: str,
        event_id: str,
        occurred_at: datetime,
        received_at: datetime | None = None,
    ) -> bool:
        """Persist the content-free product-event fence.

        Returns ``True`` only for the first accepted ``(user_id, event_id)``.
        Server receive time is the retention truth; client time is bounded and
        cannot prolong the row lifetime.
        """
        received = received_at or datetime.now(tz=UTC)
        if received.tzinfo is None or occurred_at.tzinfo is None:
            raise ValueError("composer telemetry timestamps must be timezone-aware")
        received = received.astimezone(UTC)
        occurred = occurred_at.astimezone(UTC)
        if occurred < received - timedelta(days=30) or occurred > received + timedelta(minutes=10):
            raise ValueError("composer telemetry occurred_at outside accepted window")
        async with open_shared_ctx(self._db_path) as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO composer_telemetry_dedupe(user_id,event_id,occurred_at,received_at) VALUES (?,?,?,?)",
                (
                    user_id,
                    event_id,
                    occurred.isoformat(timespec="seconds"),
                    received.isoformat(timespec="seconds"),
                ),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def cleanup_composer_telemetry_dedupe(
        self,
        *,
        batch_size: int = 5000,
        now: datetime | None = None,
    ) -> int:
        """Delete one bounded batch after the frozen 30 day + 2 hour window."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        cutoff = (now or datetime.now(tz=UTC)).astimezone(UTC) - timedelta(days=30, hours=2)
        async with open_shared_ctx(self._db_path) as db:
            cursor = await db.execute(
                "DELETE FROM composer_telemetry_dedupe WHERE rowid IN ("
                "SELECT rowid FROM composer_telemetry_dedupe WHERE received_at<? "
                "ORDER BY received_at LIMIT ?)",
                (cutoff.isoformat(timespec="seconds"), batch_size),
            )
            await db.commit()
        return max(cursor.rowcount, 0)

    async def get_chat_turn_user_source(
        self,
        *,
        turn_id: str,
        user_id: str,
        workspace_id: str,
    ) -> ChatTurnUserSource | None:
        """Read one canonical replay source without cross-scope enumeration."""
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            source = await _read_chat_turn_user_source_in_tx(
                db,
                turn_id=turn_id,
                expected_user_id=user_id,
                expected_workspace_id=workspace_id,
            )
        return source

    async def get_document_revision_receipt(
        self,
        *,
        target: DocumentRevisionTarget,
        session_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict | None:
        """Title and draft number of the draft a revision Turn revises (``None`` when unavailable)."""
        from bowhead_core.documents.revision import revision_receipt_in_tx

        async with open_shared_ctx(self._db_path) as db:
            return await revision_receipt_in_tx(db, target=target, session_id=session_id, user_id=user_id, workspace_id=workspace_id)

    async def list_turn_completions(self, *, user_id: str, after: str, limit: int = 20) -> list[dict[str, Any]]:
        """Chat turns of the user's normal sessions that ended after ``after`` (ISO time), oldest first.

        Feeds the completion popup (agent run limits, phase 3). ``partial`` is set when a
        helper of the turn stopped at a run limit (``lead.subagent_summary`` status partial).
        """
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                "SELECT t.id, t.session_id, s.title, s.workspace_id, t.status, t.ended_at, "
                "EXISTS(SELECT 1 FROM turn_events e WHERE e.turn_id=t.id AND e.event_type='lead.subagent_summary' "
                "AND json_valid(e.payload_json) AND json_extract(e.payload_json,'$.status')='partial') "
                "FROM turns t JOIN sessions s ON s.id=t.session_id "
                "WHERE s.user_id=? AND s.deleted_at IS NULL AND s.origin='normal' "
                "AND t.status IN ('completed','failed','cancelled','aborted') AND t.ended_at IS NOT NULL AND t.ended_at>? "
                "ORDER BY t.ended_at, t.id LIMIT ?",
                (user_id, after, int(limit)),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return [
            {
                "turn_id": str(row[0]),
                "session_id": str(row[1]),
                "title": str(row[2] or ""),
                "workspace_id": str(row[3] or ""),
                "outcome": ("partial" if row[6] else "completed")
                if row[4] == "completed"
                else "cancelled"
                if row[4] in ("cancelled", "aborted")
                else "failed",
                "ended_at": str(row[5]),
            }
            for row in rows
        ]

    async def chat_turn_execution_alive(self, *, turn_id: str) -> bool:
        """Whether a running chat turn's worker is still renewing its dispatch lease.

        Used by the frontend liveness check (agent run limits, phase 3): a quiet stream
        is not treated as failed while this is true. A turn without a dispatch row
        (legacy path) cannot be proven dead, so it counts as alive; maintenance
        terminalizes such turns when they go stale.
        """
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                "SELECT state, lease_expires_at FROM chat_turn_spawn_dispatch WHERE turn_id=?",
                (turn_id,),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            return True
        state, lease_expires_at = row[0], row[1]
        if state in ("terminal", "failed"):
            return False
        try:
            expires = datetime.fromisoformat(str(lease_expires_at))
        except (TypeError, ValueError):
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires > datetime.now(tz=UTC)

    async def get_chat_spawn_dispatch_projection_state(
        self,
        *,
        turn_id: str,
    ) -> Literal["active", "settled", "untracked"]:
        """Return the durable fence used by remote SSE follow-up projection.

        Only ``terminal`` / ``failed`` prove the worker has finished its
        ordered event-log tail.  An expired active lease remains ``active``
        until recovery terminalizes it, so a remote Gateway cannot project a
        colliding sequence while the owning process may still be draining.
        """
        async with open_shared_ctx(self._db_path) as db:
            async with db.execute(
                "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
                (turn_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                # A legacy worker can hit a storage outage before it creates a
                # drain_pending row. The accepted buffer itself is still a
                # durable recovery fact: while its terminal parent remains the
                # session's active Turn, SSE must EOF instead of synthesizing
                # done. Maintenance creates the missing row on its next pass.
                async with db.execute(
                    "SELECT 1 FROM sessions s JOIN turns t ON t.id=s.active_turn_id "
                    "WHERE t.id=? AND t.status IN "
                    f"{_LEGACY_CONTINUATION_SETTLED_STATUS_SQL} "
                    "AND CASE "
                    "WHEN NOT json_valid(COALESCE(s.steer_buffer_json,'[]')) THEN 1 "
                    "WHEN json_type(COALESCE(s.steer_buffer_json,'[]'))<>'array' THEN 1 "
                    "ELSE json_array_length(COALESCE(s.steer_buffer_json,'[]'))>0 END",
                    (turn_id,),
                ) as orphan_cursor:
                    orphaned_buffer = await orphan_cursor.fetchone()
                if orphaned_buffer is not None:
                    return "active"
        if row is None:
            return "untracked"
        state = str(row[0])
        if state in {"terminal", "failed"}:
            return "settled"
        return "active"

    async def require_legacy_chat_execution(self, *, turn_id: str) -> None:
        """Execution gate only; does not restrict history/source/receipt reads."""
        async with open_shared_ctx(self._db_path) as db:
            if await _requires_reference_execution_in_tx(db, turn_id):
                raise ReferenceExecutionUnavailableError()

    async def claim_chat_spawn_dispatch_for_worker(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_seconds: int = 30,
    ) -> Literal["claimed", "untracked", "rejected"]:
        """Claim a durable dispatch before a worker can be scheduled.

        Legacy channel entrypoints are materialized into the same dispatch
        substrate in this transaction. Every scheduled Turn therefore has a
        cross-process Stop fence and an ordered-tail settlement barrier.
        ``untracked`` is now reserved for a missing Turn row.
        """
        now = _now()
        lease_expires_at = (datetime.now(tz=UTC) + timedelta(seconds=lease_seconds)).isoformat()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                try:
                    parent = await _read_legacy_continuation_parent_in_tx(
                        db,
                        turn_id=turn_id,
                        allow_idle_session=True,
                    )
                except (SessionProtocolManagedError, TerminalProtocolError):
                    await db.execute("ROLLBACK")
                    return "rejected"
                if parent is None:
                    async with db.execute(
                        "SELECT 1 FROM turns WHERE id=?",
                        (turn_id,),
                    ) as turn_cursor:
                        turn_exists = await turn_cursor.fetchone() is not None
                    await db.execute("ROLLBACK")
                    return "rejected" if turn_exists else "untracked"
                _parent_session, _parent_turn = parent
                if await _requires_reference_execution_in_tx(db, turn_id):
                    await db.rollback()
                    return "rejected"
                async with db.execute(
                    "SELECT d.state,d.owner_id,d.session_id AS dispatch_session_id,"
                    "d.lease_kind,d.lease_execution_generation,t.status,"
                    "t.session_id AS turn_session_id,t.execution_generation "
                    "FROM chat_turn_spawn_dispatch d JOIN turns t ON t.id=d.turn_id "
                    "WHERE d.turn_id=?",
                    (turn_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    if str(_parent_turn["status"]) != "running":
                        await db.execute("ROLLBACK")
                        return "rejected"
                    await db.execute(
                        "INSERT INTO chat_turn_spawn_dispatch("
                        "turn_id,session_id,source_submission_id,parent_turn_id,"
                        "source_submission_ids_json,dispatch_kind,state,lease_kind,"
                        "lease_execution_generation,owner_id,cancel_requested_at,"
                        "lease_expires_at,created_at,updated_at"
                        ") VALUES (?,?,NULL,NULL,'[]','start','spawned','turn',?,"
                        "?,NULL,?,?,?)",
                        (
                            turn_id,
                            str(_parent_turn["session_id"]),
                            max(
                                1,
                                int(_parent_turn["execution_generation"] or 1),
                            ),
                            owner_id,
                            lease_expires_at,
                            now,
                            now,
                        ),
                    )
                    await db.execute(
                        "UPDATE sessions SET active_turn_id=? WHERE id=?",
                        (turn_id, str(_parent_turn["session_id"])),
                    )
                    await db.commit()
                    return "claimed"
                lease_kind = str(row["lease_kind"] or "")
                try:
                    lease_generation = int(row["lease_execution_generation"])
                    turn_generation = int(row["execution_generation"])
                except (TypeError, ValueError):
                    await db.execute("ROLLBACK")
                    return "rejected"
                if (
                    str(row["state"]) != "claimed"
                    or row["owner_id"] is not None
                    or str(row["status"]) != "running"
                    or str(row["dispatch_session_id"]) != str(row["turn_session_id"])
                    or lease_kind not in {"turn", "resume"}
                    or lease_generation != turn_generation
                ):
                    await db.execute("ROLLBACK")
                    return "rejected"
                updated = await db.execute(
                    "UPDATE chat_turn_spawn_dispatch SET state='spawned',owner_id=?,"
                    "lease_expires_at=?,updated_at=? WHERE turn_id=? AND state='claimed' "
                    "AND owner_id IS NULL AND session_id=? AND lease_kind=? "
                    "AND lease_execution_generation=? AND EXISTS (SELECT 1 FROM turns t "
                    "WHERE t.id=? AND t.session_id=chat_turn_spawn_dispatch.session_id "
                    "AND t.status='running' AND t.execution_generation=?)",
                    (
                        owner_id,
                        lease_expires_at,
                        now,
                        turn_id,
                        str(row["turn_session_id"]),
                        lease_kind,
                        lease_generation,
                        turn_id,
                        turn_generation,
                    ),
                )
                if updated.rowcount != 1:
                    await db.execute("ROLLBACK")
                    return "rejected"
                await db.execute(
                    "UPDATE sessions SET active_turn_id=? WHERE id=? AND EXISTS (SELECT 1 FROM turns WHERE id=? AND status='running')",
                    (turn_id, str(row["turn_session_id"]), turn_id),
                )
                await db.commit()
                return "claimed"
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def heartbeat_chat_spawn_dispatch(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_seconds: int = 30,
        settling: bool = False,
    ) -> Literal["renewed", "cancel_requested", "lost"]:
        """Renew a worker lease, including its ordered-log settlement phase.

        Once the worker coroutine has exited, ``settling=True`` keeps the same
        owner alive while its accepted event-log tail drains.  A late cancel is
        still a Turn fact, but must not let maintenance declare this live owner
        lost before persistence becomes quiescent.
        """
        now = _now()
        lease_expires_at = (datetime.now(tz=UTC) + timedelta(seconds=lease_seconds)).isoformat()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT d.state,d.owner_id,d.cancel_requested_at,"
                    "d.session_id AS dispatch_session_id,d.lease_kind,d.lease_expires_at,"
                    "d.lease_execution_generation,t.status,"
                    "t.session_id AS turn_session_id,t.execution_generation "
                    "FROM chat_turn_spawn_dispatch d JOIN turns t ON t.id=d.turn_id "
                    "WHERE d.turn_id=?",
                    (turn_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.execute("ROLLBACK")
                    return "lost"
                try:
                    lease_generation = int(row["lease_execution_generation"])
                    turn_generation = int(row["execution_generation"])
                except (TypeError, ValueError):
                    await db.execute("ROLLBACK")
                    return "lost"
                lease_kind = str(row["lease_kind"] or "")
                if (
                    str(row["state"]) != "spawned"
                    or str(row["owner_id"] or "") != owner_id
                    or str(row["dispatch_session_id"]) != str(row["turn_session_id"])
                    or lease_kind not in {"turn", "resume"}
                    or lease_generation != turn_generation
                ):
                    await db.execute("ROLLBACK")
                    return "lost"
                if not settling and (row["cancel_requested_at"] is not None or str(row["status"]) == "cancelled"):
                    await db.execute("ROLLBACK")
                    return "cancel_requested"
                if await _requires_reference_execution_in_tx(db, turn_id=turn_id):
                    from bowhead_core.chat_reference_retention import reference_material_available_in_connection
                    from bowhead_core.chat_reference_runtime_store import is_reference_runtime_in_connection
                    from bowhead_core.chat_reference_storage import _time

                    if not await is_reference_runtime_in_connection(db, turn_id=turn_id) or _time(row["lease_expires_at"]) <= datetime.now(UTC):
                        await db.rollback()
                        return "lost"
                    if not settling and not await reference_material_available_in_connection(db, turn_id=turn_id):
                        await db.rollback()
                        return "lost"
                # Compute deadlines under the writer lock, after validation.
                now = _now()
                lease_expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
                cancel_fence = "" if settling else " AND cancel_requested_at IS NULL"
                cursor = await db.execute(
                    "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=?,updated_at=? "
                    "WHERE turn_id=? AND state='spawned' AND owner_id=? "
                    "AND session_id=? AND lease_kind=? AND lease_execution_generation=? "
                    "AND EXISTS (SELECT 1 FROM turns t WHERE t.id=? "
                    "AND t.session_id=chat_turn_spawn_dispatch.session_id "
                    "AND t.execution_generation=?)" + cancel_fence,
                    (
                        lease_expires_at,
                        now,
                        turn_id,
                        owner_id,
                        str(row["turn_session_id"]),
                        lease_kind,
                        lease_generation,
                        turn_id,
                        turn_generation,
                    ),
                )
                if cursor.rowcount != 1:
                    await db.execute("ROLLBACK")
                    return "lost"
                await db.commit()
                return "renewed"
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def mark_chat_spawn_dispatch_terminal(
        self,
        *,
        turn_id: str,
        owner_id: str | None = None,
        failed: bool = False,
    ) -> bool:
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT t.status,t.final_answer,t.execution_generation,d.lease_kind,"
                    "d.lease_execution_generation,s.deleted_at FROM turns t "
                    "JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id "
                    "JOIN sessions s ON s.id=t.session_id "
                    "WHERE t.id=? AND t.session_id=d.session_id",
                    (turn_id,),
                ) as proof_cursor:
                    proof = await proof_cursor.fetchone()
                if proof is None:
                    await db.execute("ROLLBACK")
                    return False
                canonical_status = str(proof["status"] or "")
                try:
                    execution_generation = int(proof["execution_generation"])
                    lease_execution_generation = int(
                        proof["lease_execution_generation"],
                    )
                except (TypeError, ValueError):
                    await db.execute("ROLLBACK")
                    return False
                if canonical_status not in LEGACY_TURN_WORKER_SETTLED_STATUSES or execution_generation != lease_execution_generation:
                    await db.execute("ROLLBACK")
                    return False
                async with db.execute(
                    "SELECT CASE "
                    "WHEN NOT json_valid(COALESCE(s.steer_buffer_json,'[]')) THEN 1 "
                    "WHEN json_type(COALESCE(s.steer_buffer_json,'[]'))<>'array' THEN 1 "
                    "ELSE json_array_length(COALESCE(s.steer_buffer_json,'[]'))>0 END "
                    "FROM sessions s JOIN chat_turn_spawn_dispatch d "
                    "ON d.session_id=s.id WHERE d.turn_id=? "
                    "AND s.active_turn_id=d.turn_id",
                    (turn_id,),
                ) as continuation_cursor:
                    continuation = await continuation_cursor.fetchone()
                has_work = bool(continuation and continuation[0])
                if not has_work:
                    generation_floor = await _legacy_generation_floor_in_tx(
                        db,
                        turn_id=turn_id,
                        lease_kind=str(proof["lease_kind"] or ""),
                        lease_execution_generation=lease_execution_generation,
                    )
                    if not await _legacy_root_terminal_matches_in_tx(
                        db,
                        turn_id=turn_id,
                        generation_floor=generation_floor,
                        canonical_status=canonical_status,
                        canonical_final_answer=str(proof["final_answer"] or ""),
                    ):
                        await db.execute("ROLLBACK")
                        return False
                reference_execution = await _requires_reference_execution_in_tx(
                    db,
                    turn_id=turn_id,
                )
                deferred_reference_work = False
                recover_reference_work = False
                if reference_execution and canonical_status == "clarification_pending" and has_work:
                    from bowhead_core.chat_reference_continuation import pause_ready

                    deferred_reference_work = proof["deleted_at"] is None and await pause_ready(
                        db,
                        turn_id=turn_id,
                    )
                    recover_reference_work = not deferred_reference_work
                if reference_execution:
                    from bowhead_core.chat_reference_runtime_store import settle_reference_primary_in_connection
                    from bowhead_core.chat_reference_storage import _row, _time

                    reference_owner = await _row(
                        db, "SELECT state,owner_id,lease_expires_at FROM chat_turn_spawn_dispatch WHERE turn_id=?", (turn_id,)
                    )
                    if (
                        (has_work and not (deferred_reference_work or recover_reference_work))
                        or not owner_id
                        or reference_owner["state"] != "spawned"
                        or reference_owner["owner_id"] != owner_id
                        or _time(reference_owner["lease_expires_at"]) <= datetime.now(UTC)
                    ):
                        await db.rollback()
                        return False
                    if not recover_reference_work:
                        await settle_reference_primary_in_connection(db, turn_id=turn_id, status=canonical_status, now=_now())
                from bowhead_core.document_clarification_lifecycle import document_pause_retained_in_tx

                document_pause = canonical_status == "clarification_pending" and await document_pause_retained_in_tx(db, turn_id=turn_id)
                drain_work = recover_reference_work or (has_work and not reference_execution and not document_pause)
                cursor = await db.execute(
                    "UPDATE chat_turn_spawn_dispatch SET state=?,"
                    "owner_id=CASE WHEN ? THEN NULL ELSE owner_id END,"
                    "lease_expires_at=CASE WHEN ? THEN ? ELSE lease_expires_at END,"
                    "updated_at=? WHERE turn_id=? AND ("
                    "(? IS NULL AND state IN ('claimed','spawned')) OR "
                    "(? IS NOT NULL AND state='spawned' AND owner_id=?))",
                    (
                        "drain_pending" if drain_work else ("failed" if failed else "terminal"),
                        drain_work,
                        drain_work,
                        now,
                        now,
                        turn_id,
                        owner_id,
                        owner_id,
                        owner_id,
                    ),
                )
                if cursor.rowcount == 1 and reference_execution and not (deferred_reference_work or drain_work):
                    await db.execute(
                        "UPDATE sessions SET active_turn_id=NULL,steer_buffer_json='[]' WHERE id=(SELECT session_id FROM turns WHERE id=?) AND active_turn_id=?",
                        (turn_id, turn_id),
                    )
                await db.commit()
            except BaseException:
                await db.execute("ROLLBACK")
                raise
        return cursor.rowcount == 1

    async def prepare_chat_spawn_failure(
        self,
        *,
        turn_id: str,
        session_id: str,
        expected_owner_id: str | None = None,
    ) -> Literal["drain_pending", "foreign_active", "ready", "untracked"]:
        """Close steer admission before a pre-worker failure emits terminal.

        A concurrent same-receipt retry can expose the Turn before
        ``spawn_turn`` returns. From that point a steer may commit while the
        route is handling a dispatch failure. Under one writer lock, publish
        the active-session fence and bind the exact failing owner. The caller
        then persists its specific failure before handing that same row to
        ``drain_pending`` recovery, so maintenance cannot race ahead with a
        generic owner-lost result.
        """
        now = _now()
        recovery_lease_expires_at = (datetime.now(tz=UTC) + timedelta(seconds=30)).isoformat()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                parent = await _read_legacy_continuation_parent_in_tx(
                    db,
                    turn_id=turn_id,
                    session_id=session_id,
                    allow_idle_session=True,
                )
                if parent is None:
                    await db.execute("ROLLBACK")
                    return "untracked"
                session, parent_turn = parent
                if str(parent_turn["status"]) != "running":
                    # This handoff is only allowed to fence a pre-worker
                    # attempt. A retry after recovery must not resurrect an
                    # already-settled Turn by republishing active_turn_id.
                    await db.execute("ROLLBACK")
                    return "untracked"
                async with db.execute(
                    "SELECT state,owner_id,lease_kind FROM chat_turn_spawn_dispatch WHERE turn_id=? AND session_id=?",
                    (turn_id, session_id),
                ) as existing_cursor:
                    existing_dispatch = await existing_cursor.fetchone()
                if existing_dispatch is not None:
                    existing_state = str(existing_dispatch["state"])
                    existing_lease_kind = str(existing_dispatch["lease_kind"] or "")
                    if existing_state in {"terminal", "failed"}:
                        await db.execute("ROLLBACK")
                        return "untracked"
                    if existing_state == "drain_pending":
                        await db.execute("ROLLBACK")
                        return "drain_pending"
                    if existing_lease_kind != "turn":
                        await db.execute("ROLLBACK")
                        return "foreign_active" if existing_state == "spawned" else "untracked"
                active_turn_id = session["active_turn_id"]
                if active_turn_id is None:
                    activated = await db.execute(
                        "UPDATE sessions SET active_turn_id=? WHERE id=? AND active_turn_id IS NULL",
                        (turn_id, session_id),
                    )
                    if activated.rowcount != 1:
                        await db.execute("ROLLBACK")
                        return "untracked"
                elif str(active_turn_id) != turn_id:
                    await db.execute("ROLLBACK")
                    return "untracked"

                execution_generation = max(
                    1,
                    int(parent_turn["execution_generation"] or 1),
                )
                await db.execute(
                    "INSERT OR IGNORE INTO chat_turn_spawn_dispatch("
                    "turn_id,session_id,source_submission_id,parent_turn_id,"
                    "source_submission_ids_json,dispatch_kind,state,lease_kind,"
                    "lease_execution_generation,owner_id,lease_expires_at,"
                    "created_at,updated_at"
                    ") VALUES (?,?,NULL,NULL,'[]','start','claimed','turn',?,"
                    "NULL,?,?,?)",
                    (
                        turn_id,
                        session_id,
                        execution_generation,
                        recovery_lease_expires_at,
                        now,
                        now,
                    ),
                )
                if expected_owner_id is not None:
                    # Fence the exact failing attempt before it writes the Turn
                    # failure. Do not publish drain_pending yet: maintenance
                    # could otherwise recover it before the caller commits its
                    # specific error_kind. Accepted steer remains on the active
                    # Session and is handed off after that commit.
                    await db.execute(
                        "UPDATE chat_turn_spawn_dispatch SET state='spawned',"
                        "owner_id=?,lease_expires_at=?,updated_at=? "
                        "WHERE turn_id=? AND session_id=? AND lease_kind='turn' "
                        "AND lease_execution_generation=? AND ("
                        "(state='claimed' AND owner_id IS NULL) OR "
                        "(state='spawned' AND owner_id=?))",
                        (
                            expected_owner_id,
                            recovery_lease_expires_at,
                            now,
                            turn_id,
                            session_id,
                            execution_generation,
                            expected_owner_id,
                        ),
                    )
                async with db.execute(
                    "SELECT state,owner_id,lease_kind FROM chat_turn_spawn_dispatch WHERE turn_id=? AND session_id=?",
                    (turn_id, session_id),
                ) as cursor:
                    tracked = await cursor.fetchone()
                await db.commit()
                if tracked is None:
                    return "untracked"
                state = str(tracked[0])
                if str(tracked[2] or "") != "turn":
                    # Clarification resume has its own rollback policy. Never
                    # let the generic fresh-start handoff terminalize that
                    # generation as dispatch_start_failed.
                    return "foreign_active" if state == "spawned" else "untracked"
                if state == "drain_pending":
                    return "drain_pending"
                if state == "spawned":
                    if expected_owner_id is not None and str(tracked[1] or "") == expected_owner_id:
                        return "ready"
                    return "foreign_active"
                return "untracked"
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def mark_chat_spawn_dispatch_drain_pending(
        self,
        *,
        turn_id: str,
        owner_id: str | None = None,
        session_id: str | None = None,
        allow_terminal_session_idle: bool = False,
    ) -> bool:
        """Hand an accepted steer buffer to durable maintenance retry.

        The worker calls this after a transient materialization failure.  The
        lease monitor only terminalizes ``spawned`` rows, so changing state in
        this transaction prevents it from erasing the retry marker. Legacy
        workers may not have a dispatch row yet; ``session_id`` lets the same
        transaction create a recovery-owned marker when accepted work exists.
        """
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                try:
                    parent = await _read_legacy_continuation_parent_in_tx(
                        db,
                        turn_id=turn_id,
                        session_id=session_id,
                        require_active_parent=(not allow_terminal_session_idle),
                    )
                except (SessionProtocolManagedError, TerminalProtocolError):
                    await db.execute("ROLLBACK")
                    return False
                if parent is None:
                    await db.execute("ROLLBACK")
                    return False
                parent_session, parent_turn = parent
                if allow_terminal_session_idle and (
                    normalize_legacy_turn_status(parent_turn["status"]) != "cancelled"
                    or str(parent_session["active_turn_id"] or "") not in {"", turn_id}
                ):
                    await db.execute("ROLLBACK")
                    return False
                aliased_legacy_filter = (await _legacy_turn_protocol_filter(db)).replace(
                    "terminal_protocol_version",
                    "t.terminal_protocol_version",
                )
                if session_id is not None:
                    await db.execute(
                        "INSERT OR IGNORE INTO chat_turn_spawn_dispatch("
                        "turn_id,session_id,source_submission_id,parent_turn_id,"
                        "source_submission_ids_json,dispatch_kind,state,owner_id,"
                        "lease_expires_at,created_at,updated_at"
                        ") SELECT t.id,t.session_id,NULL,NULL,'[]','start',"
                        "'drain_pending',NULL,?,?,? FROM turns t JOIN sessions s "
                        "ON s.id=t.session_id WHERE t.id=? AND t.session_id=? "
                        "AND s.active_turn_id=t.id "
                        "AND CASE "
                        "WHEN NOT json_valid(COALESCE(s.steer_buffer_json,'[]')) THEN 1 "
                        "WHEN json_type(COALESCE(s.steer_buffer_json,'[]'))<>'array' THEN 1 "
                        "ELSE json_array_length(COALESCE(s.steer_buffer_json,'[]'))>0 END"
                        f"{aliased_legacy_filter}",
                        (now, now, now, turn_id, session_id),
                    )
                if owner_id is None:
                    await db.execute(
                        "UPDATE chat_turn_spawn_dispatch SET state='drain_pending',"
                        "owner_id=NULL,lease_expires_at=?,updated_at=? "
                        "WHERE turn_id=? AND state IN ('claimed','spawned') "
                        "AND EXISTS (SELECT 1 FROM turns t WHERE t.id=?"
                        f"{aliased_legacy_filter})",
                        (now, now, turn_id, turn_id),
                    )
                else:
                    await db.execute(
                        "UPDATE chat_turn_spawn_dispatch SET state='drain_pending',"
                        "owner_id=NULL,lease_expires_at=?,updated_at=? "
                        "WHERE turn_id=? AND state='spawned' AND owner_id=? "
                        "AND EXISTS (SELECT 1 FROM turns t WHERE t.id=?"
                        f"{aliased_legacy_filter})",
                        (now, now, turn_id, owner_id, turn_id),
                    )
                async with db.execute(
                    "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
                    (turn_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                await db.commit()
                return row is not None and str(row[0]) == "drain_pending"
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def recover_expired_chat_spawn_dispatches(
        self,
        *,
        reference_store: ChatReferenceStore | None = None,
    ) -> list[str]:
        """Terminalize only dispatch leases whose owning worker is no longer live.

        This is safe during rolling startup: a healthy old instance keeps its
        lease fresh, so a new instance cannot mark that Turn failed.
        """
        if reference_store is not None:
            from bowhead_core.chat_reference_storage import ChatReferenceStore

            if type(reference_store) is not ChatReferenceStore:
                raise ValueError("reference_recovery_store_invalid")
        now = _now()
        orphan_cutoff = (datetime.now(tz=UTC) - timedelta(seconds=CHAT_SPAWN_DISPATCH_ORPHAN_GRACE_SECONDS)).isoformat(timespec="seconds")
        turn_ids: list[str] = []
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                aliased_legacy_filter = (await _legacy_turn_protocol_filter(db)).replace(
                    "terminal_protocol_version",
                    "t.terminal_protocol_version",
                )
                aliased_legacy_filter += f" AND (NOT {_REFERENCE_TURN_SQL} OR {_BLOCKED_REFERENCE_QUEUE_ANCHOR_SQL})"
                # Storage may have recovered after both the terminal hook and
                # finalizer failed to create a legacy marker. Bootstrap those
                # orphan terminal-active buffers before the normal dispatch
                # scan, in the same transaction that materializes recovery.
                # Even an empty buffer starts as drain_pending: a missing
                # dispatch row is not proof that the root wire terminal was
                # committed, so the common scan below must verify/repair it.
                await db.execute(
                    "INSERT OR IGNORE INTO chat_turn_spawn_dispatch("
                    "turn_id,session_id,source_submission_id,parent_turn_id,"
                    "source_submission_ids_json,dispatch_kind,state,owner_id,"
                    "lease_expires_at,created_at,updated_at"
                    ") SELECT t.id,s.id,NULL,NULL,'[]','start','drain_pending',"
                    "NULL,?,?,? FROM sessions s JOIN turns t ON t.id=s.active_turn_id "
                    "WHERE t.status IN "
                    f"{_LEGACY_CONTINUATION_SETTLED_STATUS_SQL} "
                    "AND t.ended_at IS NOT NULL AND t.ended_at<=?"
                    f"{aliased_legacy_filter}",
                    (now, now, now, orphan_cutoff),
                )
                # Older workers could persist clarification_pending and clear
                # the Session pointer before dispatch settlement existed. Do
                # not treat the missing row as immediate proof: only after the
                # same orphan grace may maintenance claim recovery for an
                # otherwise idle, empty legacy Session. The normal recovery
                # scan below repairs/verifies its root done before settling.
                await db.execute(
                    "INSERT OR IGNORE INTO chat_turn_spawn_dispatch("
                    "turn_id,session_id,source_submission_id,parent_turn_id,"
                    "source_submission_ids_json,dispatch_kind,state,owner_id,"
                    "lease_expires_at,created_at,updated_at"
                    ") SELECT t.id,s.id,NULL,NULL,'[]','start','drain_pending',NULL,"
                    "?,?,? FROM turns t JOIN sessions s ON s.id=t.session_id "
                    "WHERE t.status='clarification_pending' "
                    "AND t.ended_at IS NOT NULL AND t.ended_at<=? "
                    "AND s.active_turn_id IS NULL "
                    "AND json_valid(COALESCE(s.steer_buffer_json,'[]')) "
                    "AND json_type(COALESCE(s.steer_buffer_json,'[]'))='array' "
                    "AND json_array_length(COALESCE(s.steer_buffer_json,'[]'))=0"
                    f"{aliased_legacy_filter}",
                    (now, now, now, orphan_cutoff),
                )
                # INSERT OR IGNORE also encounters deployments that already
                # wrote a terminal/failed dispatch while leaving the terminal
                # parent active. Accepted or malformed buffered work must take
                # ownership of recovery instead of remaining hidden behind that
                # old row.
                await db.execute(
                    "UPDATE chat_turn_spawn_dispatch SET state='drain_pending',"
                    "owner_id=NULL,lease_expires_at=?,updated_at=? WHERE state IN "
                    "('terminal','failed') AND turn_id IN (SELECT t.id FROM turns t "
                    "JOIN sessions s ON s.id=t.session_id WHERE s.active_turn_id=t.id "
                    "AND t.status IN "
                    f"{_LEGACY_CONTINUATION_SETTLED_STATUS_SQL} "
                    "AND t.ended_at IS NOT NULL AND t.ended_at<=? AND CASE "
                    "WHEN NOT json_valid(COALESCE(s.steer_buffer_json,'[]')) THEN 1 "
                    "WHEN json_type(COALESCE(s.steer_buffer_json,'[]'))<>'array' THEN 1 "
                    "ELSE json_array_length(COALESCE(s.steer_buffer_json,'[]'))>0 END"
                    f"{aliased_legacy_filter})",
                    (now, now, orphan_cutoff),
                )
                # Empty terminal parents have no continuation to materialize.
                # Clear the exact active fence in the same writer transaction
                # that guarantees a settled dispatch row. A concurrent append
                # therefore either lands before this transaction and promotes
                # the row to drain_pending, or observes active=NULL afterwards.
                await db.execute(
                    "UPDATE sessions SET active_turn_id=NULL,steer_buffer_json='[]' "
                    "WHERE active_turn_id IN (SELECT t.id FROM turns t "
                    "WHERE t.session_id=sessions.id AND t.status IN "
                    f"{_LEGACY_CONTINUATION_SETTLED_STATUS_SQL} "
                    "AND t.ended_at IS NOT NULL AND t.ended_at<=? "
                    "AND EXISTS (SELECT 1 FROM chat_turn_spawn_dispatch d "
                    "WHERE d.turn_id=t.id AND d.state IN ('terminal','failed'))"
                    f"{aliased_legacy_filter}) AND json_valid(COALESCE(steer_buffer_json,'[]')) "
                    "AND json_type(COALESCE(steer_buffer_json,'[]'))='array' "
                    "AND json_array_length(COALESCE(steer_buffer_json,'[]'))=0",
                    (orphan_cutoff,),
                )
                from bowhead_core.document_clarification_lifecycle import recover_document_pauses_in_tx

                await recover_document_pauses_in_tx(db, now=now)
                reference_runtime_filter = f"(NOT {_REFERENCE_TURN_SQL} OR {_BLOCKED_REFERENCE_QUEUE_ANCHOR_SQL})"
                async with db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_reference_resolutions'") as schema_cursor:
                    if await schema_cursor.fetchone():
                        from bowhead_core.chat_reference_runtime_store import REFERENCE_RECOVERY_SQL

                        reference_runtime_filter = f"({reference_runtime_filter} OR {REFERENCE_RECOVERY_SQL})"
                from bowhead_core.chat_reference_bi_continuation import installed

                if await installed(db):
                    from bowhead_core.chat_reference_runtime_store import (
                        REFERENCE_BI_RESUME_SQL,
                        REFERENCE_LEAD_POSTDISPATCH_RESUME_SQL,
                        REFERENCE_LEAD_RESUME_SQL,
                        REFERENCE_RECOVERY_SQL,
                    )

                    reference_runtime_filter = (
                        f"({reference_runtime_filter} OR {REFERENCE_BI_RESUME_SQL} OR {REFERENCE_LEAD_RESUME_SQL} "
                        f"OR {REFERENCE_LEAD_POSTDISPATCH_RESUME_SQL})"
                    )
                    # Re-evaluate settled BI pauses even without queued work.
                    # Dismissal/expiry must close the original execution, not
                    # leave a permanently pending Turn after its card is gone.
                    # Any accepted steer still uses the same recovery below.
                    async with db.execute(
                        "SELECT t.id FROM turns t JOIN sessions s ON s.id=t.session_id "
                        "JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=s.id "
                        "WHERE d.state='terminal' AND t.status='clarification_pending' "
                        f"AND ({REFERENCE_RECOVERY_SQL} OR {REFERENCE_BI_RESUME_SQL} OR {REFERENCE_LEAD_RESUME_SQL} "
                        f"OR {REFERENCE_LEAD_POSTDISPATCH_RESUME_SQL}) "
                        "ORDER BY d.updated_at,d.turn_id LIMIT ?",
                        (_DEFERRED_REFERENCE_PAUSE_RECOVERY_BATCH_SIZE,),
                    ) as deferred_cursor:
                        deferred_candidates = await deferred_cursor.fetchall()
                    from bowhead_core.chat_reference_continuation import pause_recoverable

                    for deferred_candidate in deferred_candidates:
                        deferred_turn_id = str(deferred_candidate[0])
                        pause_is_recoverable = await pause_recoverable(db, turn_id=deferred_turn_id, reference_store=reference_store)
                        if pause_is_recoverable:
                            # Rotate healthy rows behind the bounded batch so
                            # a stable prefix cannot starve later candidates.
                            await db.execute(
                                "UPDATE chat_turn_spawn_dispatch SET updated_at=? WHERE turn_id=? AND state='terminal'",
                                (now, deferred_turn_id),
                            )
                            continue
                        await db.execute(
                            "UPDATE chat_turn_spawn_dispatch SET state='drain_pending',"
                            "owner_id=NULL,lease_expires_at=?,updated_at=? "
                            "WHERE turn_id=? AND state='terminal'",
                            (now, now, deferred_turn_id),
                        )
                async with db.execute(
                    "SELECT d.turn_id,d.session_id,d.state,d.lease_kind,"
                    "d.lease_execution_generation,s.workspace_id,s.user_id,"
                    "s.steer_buffer_json,t.status AS turn_status,t.error_kind,"
                    "t.intent,t.final_answer,t.tokens_input,"
                    "t.tokens_output,t.cost_usd,t.execution_generation "
                    "FROM chat_turn_spawn_dispatch d "
                    "JOIN sessions s ON s.id=d.session_id "
                    "JOIN turns t ON t.id=d.turn_id "
                    "WHERE ((d.state IN ('claimed','spawned') AND d.lease_expires_at<=?) "
                    "OR d.state='drain_pending') AND NOT (d.state='claimed' "
                    "AND d.parent_turn_id IS NOT NULL AND EXISTS (SELECT 1 "
                    "FROM chat_turn_spawn_dispatch parent_dispatch "
                    "WHERE parent_dispatch.turn_id=d.parent_turn_id "
                    "AND parent_dispatch.session_id=d.session_id "
                    "AND parent_dispatch.state='spawned' "
                    "AND parent_dispatch.lease_expires_at>?)) "
                    f"AND {reference_runtime_filter}",
                    (now, now),
                ) as cur:
                    rows = await cur.fetchall()
                for raw_row in rows:
                    row = dict(raw_row)
                    turn_id = str(row["turn_id"])
                    session_id = str(row["session_id"])
                    parent_is_validated_legacy = False
                    owns_session_continuation = False
                    buffer_is_malformed = False
                    reference_runtime = False
                    reference_continuation = False
                    await db.execute("SAVEPOINT recover_chat_dispatch_row")
                    try:
                        parent = await _read_legacy_continuation_parent_in_tx(
                            db,
                            turn_id=turn_id,
                            session_id=session_id,
                            require_active_parent=False,
                        )
                        if parent is None:
                            async with db.execute(
                                "SELECT t.session_id,s.workspace_id,s.user_id,"
                                "s.steer_buffer_json,t.status AS turn_status,"
                                "t.error_kind,t.intent,t.final_answer,"
                                "t.tokens_input,t.tokens_output,t.cost_usd,"
                                "t.execution_generation "
                                "FROM turns t JOIN sessions s ON s.id=t.session_id "
                                "WHERE t.id=?",
                                (turn_id,),
                            ) as actual_cursor:
                                actual_row = await actual_cursor.fetchone()
                            if actual_row is not None and str(actual_row["session_id"]) != session_id:
                                actual_session_id = str(actual_row["session_id"])
                                parent = await _read_legacy_continuation_parent_in_tx(
                                    db,
                                    turn_id=turn_id,
                                    session_id=actual_session_id,
                                    require_active_parent=False,
                                )
                                if parent is not None:
                                    session_id = actual_session_id
                                    row.update(dict(actual_row))
                        if parent is None:
                            await db.execute(
                                "UPDATE chat_turn_spawn_dispatch SET state='failed',"
                                "owner_id=NULL,lease_expires_at=?,updated_at=? "
                                "WHERE turn_id=? AND state NOT IN ('terminal','failed')",
                                (now, now, turn_id),
                            )
                            await db.execute(
                                "RELEASE SAVEPOINT recover_chat_dispatch_row",
                            )
                            turn_ids.append(turn_id)
                            continue
                        reference_runtime = await _requires_reference_execution_in_tx(db, turn_id=turn_id)
                        if reference_runtime and await _is_blocked_reference_queue_anchor_in_tx(db, turn_id):
                            # This child already proves that the accepted
                            # reference was never executed. It may anchor later
                            # buffered work, but must never enter a reference
                            # runtime or be treated as a fresh structured input.
                            reference_runtime = False
                        if reference_runtime:
                            # Prove the current reference execution before its
                            # accepted continuation is recovered below.
                            from bowhead_core.chat_reference_runtime_store import is_reference_recoverable_in_connection

                            if not await is_reference_recoverable_in_connection(db, turn_id=turn_id):
                                await db.execute("RELEASE SAVEPOINT recover_chat_dispatch_row")
                                continue
                        parent_is_validated_legacy = True
                        parent_session, _parent_turn = parent
                        owns_session_continuation = str(parent_session["active_turn_id"] or "") == turn_id
                        is_resume_lease = str(row["lease_kind"]) == "resume"
                        try:
                            lease_execution_generation = int(row["lease_execution_generation"]) if is_resume_lease else None
                            turn_execution_generation = int(row["execution_generation"]) if is_resume_lease else None
                        except (TypeError, ValueError) as exc:
                            raise _LegacyContinuationRowError(
                                "legacy resume generation is malformed",
                            ) from exc
                        if is_resume_lease and lease_execution_generation != turn_execution_generation:
                            raise _LegacyContinuationRowError(
                                "legacy resume generation does not match Turn",
                            )
                        preworker_resume = (
                            is_resume_lease
                            and owns_session_continuation
                            and str(row["state"]) == "claimed"
                            and str(row["turn_status"]) == "running"
                            and lease_execution_generation == turn_execution_generation
                        )
                        from bowhead_core.document_clarification_lifecycle import document_card_id_in_tx, release_document_resume_in_tx

                        document_card_id = await document_card_id_in_tx(db, turn_id=turn_id)
                        if preworker_resume and document_card_id:
                            try:
                                await release_document_resume_in_tx(db, card_id=document_card_id, generation=lease_execution_generation)
                            except ValueError:
                                preworker_resume = False
                        if preworker_resume and not document_card_id:
                            from bowhead_core.ordinary_lead_continuation import ordinary_card_id_in_tx, read_ordinary_card_in_tx

                            ordinary_card_id = await ordinary_card_id_in_tx(db, turn_id=turn_id)
                            if ordinary_card_id:
                                try:
                                    ordinary = await read_ordinary_card_in_tx(db, card_id=ordinary_card_id)
                                    preworker_resume = ordinary.consumed_generation is None
                                except ValueError:
                                    preworker_resume = False
                        if preworker_resume and reference_runtime:
                            from bowhead_core.chat_reference_continuation import release_resume

                            try:
                                await release_resume(db, turn_id=turn_id, generation=lease_execution_generation)
                            except ValueError:
                                # A purged/invalid answer cannot be restored to
                                # ready. Settle the original execution below.
                                preworker_resume = False
                        if preworker_resume:
                            # A resume lease is published before any worker can be
                            # scheduled.  If that pre-worker owner disappears, the
                            # durable clarification remains the retry intent: put
                            # the same Turn back into its resumable state instead
                            # of converting a confirmed choice into a hard failure.
                            await db.execute(
                                "UPDATE turns SET status='clarification_pending',ended_at=? WHERE id=? AND status='running' AND execution_generation=?",
                                (
                                    now,
                                    turn_id,
                                    lease_execution_generation,
                                ),
                            )
                            await db.execute(
                                "UPDATE chat_turn_spawn_dispatch SET state='terminal',"
                                "owner_id=NULL,lease_expires_at=?,updated_at=? "
                                "WHERE turn_id=? AND state='claimed' "
                                "AND lease_kind='resume' "
                                "AND lease_execution_generation=?",
                                (
                                    now,
                                    now,
                                    turn_id,
                                    lease_execution_generation,
                                ),
                            )
                            await db.execute(
                                "UPDATE sessions SET active_turn_id=CASE "
                                "WHEN json_valid(COALESCE(steer_buffer_json,'[]')) "
                                "AND json_type(COALESCE(steer_buffer_json,'[]'))='array' "
                                "AND json_array_length(COALESCE(steer_buffer_json,'[]'))=0 "
                                "THEN NULL ELSE active_turn_id END "
                                "WHERE id=? AND active_turn_id=?",
                                (session_id, turn_id),
                            )
                            await db.execute(
                                "RELEASE SAVEPOINT recover_chat_dispatch_row",
                            )
                            turn_ids.append(turn_id)
                            continue
                        deferred_reference_work = False
                        if (
                            reference_runtime
                            and owns_session_continuation
                            and parent_session["deleted_at"] is None
                            and str(row["turn_status"]) == "clarification_pending"
                            and _decode_steer_buffer(row["steer_buffer_json"])
                        ):
                            from bowhead_core.chat_reference_continuation import pause_recoverable

                            deferred_reference_work = await pause_recoverable(db, turn_id=turn_id, reference_store=reference_store)
                        # Once a follow-up child becomes active, the Session
                        # buffer belongs to that child. An expired parent may
                        # still need its missing done reconstructed, but must
                        # never parse, drain or clear the child's continuation.
                        steer_projection: _LegacySteerProjection | None = None
                        steer_suffix: tuple[dict[str, Any], ...] = ()
                        recovery_child_turn_id: str | None = None
                        if owns_session_continuation and not deferred_reference_work:
                            # Deletion cancels every buffered continuation. This
                            # guard applies before both reference and legacy
                            # projection so a delete between maintenance passes
                            # cannot create a new Turn or attachment source.
                            if parent_session["deleted_at"] is None:
                                buffer_is_malformed = True
                                steer_segment = _segment_steer_buffer(
                                    row["steer_buffer_json"],
                                )
                                if steer_segment.reference_submission_id is not None:
                                    reference_continuation = True
                                    recovery_child_turn_id = await _materialize_blocked_reference_recovery_child_in_tx(
                                        db,
                                        reference_store=reference_store,
                                        session_id=session_id,
                                        parent_turn_id=turn_id,
                                        segment=steer_segment,
                                        now=now,
                                    )
                                    if recovery_child_turn_id is None and steer_segment.suffix:
                                        # Retention cleanup already made the
                                        # queue decision durable and removed the
                                        # source. Consume only that exact pointer;
                                        # the same expired parent keeps ownership
                                        # of the suffix for the next pass.
                                        updated_session = await db.execute(
                                            "UPDATE sessions SET steer_buffer_json=? WHERE id=? AND active_turn_id=?",
                                            (
                                                json.dumps(steer_segment.suffix, ensure_ascii=False),
                                                session_id,
                                                turn_id,
                                            ),
                                        )
                                        if updated_session.rowcount != 1:
                                            raise _LegacyContinuationRowError(
                                                "reference continuation parent changed",
                                            )
                                        await db.execute("RELEASE SAVEPOINT recover_chat_dispatch_row")
                                        continue
                                else:
                                    steer_projection = _project_legacy_steer_buffer(
                                        json.dumps(steer_segment.items, ensure_ascii=False),
                                    )
                                steer_suffix = steer_segment.suffix
                                buffer_is_malformed = False
                        if steer_projection is not None and steer_projection.source_parts:
                            child_turn_id = (
                                "t_"
                                + hashlib.sha256(
                                    f"bowhead/steer-recovery/v1\0{turn_id}".encode(),
                                ).hexdigest()[:12]
                            )
                            recovery_child_turn_id = child_turn_id
                            async with db.execute(
                                "SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE session_id=?",
                                (session_id,),
                            ) as seq_cursor:
                                seq_row = await seq_cursor.fetchone()
                            seq = int(seq_row[0]) if seq_row else 1
                            await db.execute(
                                "INSERT OR IGNORE INTO turns("
                                "id,session_id,seq,user_text,status,started_at,ended_at,error,error_kind"
                                ") VALUES (?,?,?,?,'failed',?,?,?,?)",
                                (
                                    child_turn_id,
                                    session_id,
                                    seq,
                                    steer_projection.user_text,
                                    now,
                                    now,
                                    "accepted steer recovered after dispatch owner loss",
                                    "steer_recovery_required",
                                ),
                            )
                            try:
                                await _insert_chat_turn_user_source_in_tx(
                                    db,
                                    source=ChatTurnUserSource(
                                        turn_id=child_turn_id,
                                        session_id=session_id,
                                        user_id=str(row["user_id"]),
                                        workspace_id=str(row["workspace_id"]),
                                        user_text=steer_projection.user_text,
                                        attachment_ids=steer_projection.attachment_ids,
                                        attachment_refs=steer_projection.attachment_refs,
                                        parts=steer_projection.source_parts,
                                    ),
                                    created_at=now,
                                )
                            except ValueError as exc:
                                raise _LegacyContinuationRowError(
                                    "legacy continuation source conflicts",
                                ) from exc
                            await db.execute(
                                "INSERT OR IGNORE INTO chat_turn_spawn_dispatch("
                                "turn_id,session_id,source_submission_id,parent_turn_id,"
                                "source_submission_ids_json,dispatch_kind,state,owner_id,"
                                "lease_expires_at,created_at,updated_at"
                                ") VALUES (?,?,?,?,?,'follow_up','failed',NULL,?,?,?)",
                                (
                                    child_turn_id,
                                    session_id,
                                    steer_projection.submission_ids[0] if steer_projection.submission_ids else None,
                                    turn_id,
                                    json.dumps(
                                        steer_projection.submission_ids,
                                        separators=(",", ":"),
                                    ),
                                    now,
                                    now,
                                    now,
                                ),
                            )
                            # The recovery child is a durable failed Turn, so its
                            # stream must expose a durable terminal pair even when
                            # no process ever owned an in-memory child bus.
                            await db.execute(
                                "INSERT OR IGNORE INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,1,'turn_failed',?,?)",
                                (
                                    f"evt_steer_recovery_failed_{child_turn_id}",
                                    child_turn_id,
                                    json.dumps(
                                        {
                                            "turn_id": child_turn_id,
                                            "reason_code": "lead_internal_error",
                                            # Direct SQL bypasses TurnEventBus.emit (D5).
                                            "recovery_action": chat_recovery_action("lead_internal_error"),
                                            "seq": 1,
                                            "evt_seq": 1,
                                        },
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    ),
                                    now,
                                ),
                            )
                            await db.execute(
                                "INSERT OR IGNORE INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,2,'done',?,?)",
                                (
                                    f"evt_steer_recovery_done_{child_turn_id}",
                                    child_turn_id,
                                    json.dumps(
                                        {
                                            "turn_id": child_turn_id,
                                            "status": "failed",
                                            "error": "steer_recovery_required",
                                            "error_kind": "steer_recovery_required",
                                            "reason_code": "lead_internal_error",
                                            "recovery_action": chat_recovery_action("lead_internal_error"),
                                            "seq": 2,
                                            "evt_seq": 2,
                                        },
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    ),
                                    now,
                                ),
                            )
                        # An expired dispatch may be fenced only after its
                        # generation has a durable wire terminal. The Store CAS
                        # can commit before the final event write fails, so
                        # recovery reconstructs that missing event from the
                        # canonical Turn row without changing a settled status.
                        turn_status = str(row["turn_status"])
                        error_kind = str(row["error_kind"] or "")
                        from bowhead_core.agent_collaboration import recover_collaboration_publications_in_connection

                        recovered_collaboration = await recover_collaboration_publications_in_connection(db, turn_id=turn_id)
                        generation_floor = await _legacy_generation_floor_in_tx(
                            db,
                            turn_id=turn_id,
                            lease_kind=str(row["lease_kind"] or ""),
                            lease_execution_generation=int(
                                lease_execution_generation or row["execution_generation"],
                            ),
                        )
                        unrecoverable_reference_pause = reference_runtime and turn_status == "clarification_pending" and not deferred_reference_work
                        rephrased_reference_pause = False
                        if unrecoverable_reference_pause:
                            async with db.execute(
                                "SELECT 1 FROM clarification_requests q "
                                "JOIN clarification_invalidation_outbox o ON o.request_id=q.request_id AND o.turn_id=q.turn_id "
                                "WHERE q.turn_id=? AND q.status='dismissed' AND o.target_status='dismissed' "
                                "AND o.reason_code='user_rephrase' AND o.actor_kind='user' "
                                "AND (EXISTS (SELECT 1 FROM chat_reference_bi_continuations c "
                                "WHERE c.turn_id=q.turn_id AND c.clarification_id=q.request_id) "
                                "OR EXISTS (SELECT 1 FROM chat_reference_plans p "
                                "JOIN chat_reference_resolutions r ON r.turn_id=p.turn_id AND r.resolution_seq=1 "
                                "WHERE p.turn_id=q.turn_id "
                                "AND json_extract(p.plan_json,'$.orchestration_contract')='lead-delegation.v1' "
                                "AND CASE WHEN json_valid(q.resume_context_json) THEN "
                                "json_extract(q.resume_context_json,'$.reference_continuation.contract') END "
                                "IN ('reference-lead-predispatch.v1','reference-lead-postdispatch.v1') "
                                "AND CASE WHEN json_valid(q.resume_context_json) THEN "
                                "json_extract(q.resume_context_json,'$.reference_continuation.execution_id') END=r.execution_id "
                                "AND CASE WHEN json_valid(q.resume_context_json) THEN "
                                "json_extract(q.resume_context_json,'$.reference_continuation.source_hash') END=p.source_hash "
                                "AND CASE WHEN json_valid(q.resume_context_json) THEN "
                                "json_extract(q.resume_context_json,'$.reference_continuation.composition_digest') END=r.composition_digest "
                                "AND (CASE WHEN json_valid(q.resume_context_json) THEN "
                                "json_extract(q.resume_context_json,'$.reference_continuation.contract') END='reference-lead-predispatch.v1' "
                                "OR EXISTS (SELECT 1 FROM chat_reference_resolutions a "
                                "WHERE a.turn_id=p.turn_id AND a.resolution_seq=2 AND a.execution_id=r.execution_id "
                                "AND CASE WHEN json_valid(q.resume_context_json) THEN "
                                "json_extract(q.resume_context_json,'$.reference_continuation.delegation_digest') END=a.composition_digest))))",
                                (turn_id,),
                            ) as rephrase_cursor:
                                rephrased_reference_pause = await rephrase_cursor.fetchone() is not None
                        recovered_status = (
                            "cancelled"
                            if rephrased_reference_pause
                            else "failed"
                            if turn_status == "running" or unrecoverable_reference_pause
                            else (normalize_legacy_turn_status(turn_status) or turn_status)
                        )
                        if reference_runtime:
                            from bowhead_core.chat_reference_runtime_store import settle_reference_primary_in_connection

                            await settle_reference_primary_in_connection(db, turn_id=turn_id, status=recovered_status, now=now)
                        wire_terminal_matches = await _legacy_root_terminal_matches_in_tx(
                            db,
                            turn_id=turn_id,
                            generation_floor=generation_floor,
                            canonical_status=recovered_status,
                            canonical_final_answer=str(row["final_answer"] or ""),
                        )
                        if recovered_collaboration:
                            wire_terminal_matches = False
                        final_answer_matches = False
                        recovered_final_answer = str(row["final_answer"] or "")
                        has_recovered_final_answer = bool(
                            recovered_final_answer.strip(),
                        )
                        if has_recovered_final_answer:
                            async with db.execute(
                                "SELECT payload_json FROM turn_events WHERE turn_id=? "
                                "AND seq>? AND event_type='final_answer' "
                                "ORDER BY seq DESC LIMIT 1",
                                (turn_id, generation_floor),
                            ) as final_answer_cursor:
                                final_answer_event = await final_answer_cursor.fetchone()
                            if final_answer_event is not None:
                                try:
                                    final_payload = json.loads(
                                        str(final_answer_event[0]),
                                    )
                                except (json.JSONDecodeError, TypeError):
                                    final_payload = None
                                final_answer_matches = bool(
                                    isinstance(final_payload, dict)
                                    and isinstance(
                                        final_payload.get("text"),
                                        str,
                                    )
                                    and final_payload["text"] == recovered_final_answer
                                )
                        final_answer_needs_repair = has_recovered_final_answer and not final_answer_matches
                        if not wire_terminal_matches or final_answer_needs_repair:
                            recovered_error_kind = ""
                            recovered_error = ""
                            if rephrased_reference_pause:
                                recovered_error_kind = "user_cancel"
                            elif turn_status == "running":
                                recovered_error_kind = "dispatch_owner_lost"
                                recovered_error = "turn_dispatch_owner_lost"
                            elif unrecoverable_reference_pause:
                                recovered_error_kind = "reference_continuation_unavailable"
                                recovered_error = "reference_continuation_unavailable"
                            elif recovered_status == "failed":
                                safe_failure_errors = {
                                    "dispatch_start_failed": "turn_dispatch_unavailable",
                                    "dispatch_owner_lost": "turn_dispatch_owner_lost",
                                    "timeout": "timeout",
                                    "network": "network",
                                    "cap": "cap",
                                    "llm_failed": "llm_failed",
                                    "access_context_missing": "access_context_missing",
                                    "team_model_unconfigured": "team_model_unconfigured",
                                    # Resumed child turn blocked by quota admission: preserve the code so the frontend can explain quota exhaustion.
                                    "team_pool_exhausted": "team_pool_exhausted",
                                    "user_credit_exhausted": "user_credit_exhausted",
                                    "internal": "internal_error",
                                }
                                recovered_error_kind = error_kind if error_kind in safe_failure_errors else "internal"
                                recovered_error = safe_failure_errors[recovered_error_kind]
                            elif recovered_status == "cancelled":
                                recovered_error_kind = "user_cancel"
                            elif recovered_status == "aborted":
                                recovered_error_kind = "aborted"
                            elif recovered_status == "clarification_pending":
                                safe_clarification_kinds = {
                                    "needs_clarification",
                                    "needs_user_input",
                                    "plan_proposed",
                                    "plan_needs_context",
                                    "pending_user_confirmation",
                                    "clarification_refused_on_resume",
                                }
                                recovered_error_kind = error_kind if error_kind in safe_clarification_kinds else "needs_clarification"
                            semantic_event_type = {
                                "failed": "turn_failed",
                                "cancelled": "turn_cancelled",
                                "aborted": "turn_aborted",
                            }.get(recovered_status)
                            semantic_event = None
                            if semantic_event_type is not None:
                                async with db.execute(
                                    "SELECT 1 FROM turn_events WHERE turn_id=? AND seq>? AND event_type=? LIMIT 1",
                                    (
                                        turn_id,
                                        generation_floor,
                                        semantic_event_type,
                                    ),
                                ) as semantic_cursor:
                                    semantic_event = await semantic_cursor.fetchone()
                            row_seq, event_seq = await _next_legacy_event_cursors_in_tx(
                                db,
                                turn_id=turn_id,
                            )
                            if has_recovered_final_answer and not final_answer_matches:
                                await db.execute(
                                    "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,?,'final_answer',?,?)",
                                    (
                                        f"evt_{uuid4().hex[:12]}",
                                        turn_id,
                                        row_seq,
                                        json.dumps(
                                            {
                                                "text": recovered_final_answer,
                                                "card_id": turn_id,
                                                "seq": event_seq,
                                                "evt_seq": event_seq,
                                            },
                                            ensure_ascii=False,
                                            separators=(",", ":"),
                                        ),
                                        now,
                                    ),
                                )
                                row_seq += 1
                                event_seq += 1
                            recovered_reason_code: str | None = None
                            if recovered_status == "failed":
                                # Other failures still project to lead_internal_error; forward quota codes unchanged
                                # (the user should replenish quota and retry). If the owning process disappeared (service
                                # update/restart interruption), describe an interruption instead of a system-processing error.
                                recovered_reason_code = (
                                    recovered_error_kind
                                    if recovered_error_kind in _CREDIT_FAILURE_REASON_CODES
                                    else "service_interrupted"
                                    if recovered_error_kind == "dispatch_owner_lost"
                                    else "lead_internal_error"
                                )
                            if semantic_event_type is not None and semantic_event is None:
                                semantic_payload: dict[str, Any] = {
                                    "turn_id": turn_id,
                                    "seq": event_seq,
                                    "evt_seq": event_seq,
                                }
                                if recovered_status == "failed":
                                    semantic_payload.update(
                                        {
                                            "reason": recovered_error,
                                            "reason_code": recovered_reason_code,
                                            # Direct SQL bypasses TurnEventBus.emit (D5).
                                            "recovery_action": chat_recovery_action(recovered_reason_code),
                                        },
                                    )
                                elif recovered_error_kind:
                                    semantic_payload["reason"] = recovered_error_kind
                                await db.execute(
                                    "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,?,?,?,?)",
                                    (
                                        f"evt_{uuid4().hex[:12]}",
                                        turn_id,
                                        row_seq,
                                        semantic_event_type,
                                        json.dumps(
                                            semantic_payload,
                                            ensure_ascii=False,
                                            separators=(",", ":"),
                                        ),
                                        now,
                                    ),
                                )
                                row_seq += 1
                                event_seq += 1
                            done_payload: dict[str, Any] = {
                                "turn_id": turn_id,
                                "status": recovered_status,
                                "schema_version": "0.9.0",
                                "tokens_input": int(row["tokens_input"] or 0),
                                "tokens_output": int(row["tokens_output"] or 0),
                                "cost_usd": float(row["cost_usd"] or 0.0),
                                "seq": event_seq,
                                "evt_seq": event_seq,
                            }
                            if row["intent"] is not None:
                                done_payload["intent"] = str(row["intent"])
                            if recovered_error:
                                done_payload["error"] = recovered_error
                            if recovered_error_kind:
                                done_payload["error_kind"] = recovered_error_kind
                            if recovered_reason_code is not None:
                                done_payload["reason_code"] = recovered_reason_code
                                recovered_action = chat_recovery_action(recovered_reason_code)
                                if recovered_action is not None:
                                    done_payload["recovery_action"] = recovered_action
                            await db.execute(
                                "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,?,'done',?,?)",
                                (
                                    f"evt_{uuid4().hex[:12]}",
                                    turn_id,
                                    row_seq,
                                    json.dumps(
                                        done_payload,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    ),
                                    now,
                                ),
                            )
                        await db.execute(
                            "UPDATE turns SET status=CASE WHEN ? THEN 'cancelled' ELSE 'failed' END,"
                            "error_kind=CASE WHEN ? THEN 'user_cancel' WHEN status='clarification_pending' "
                            "THEN 'reference_continuation_unavailable' ELSE 'dispatch_owner_lost' END,"
                            "error=CASE WHEN ? THEN NULL WHEN status='clarification_pending' "
                            "THEN 'reference_continuation_unavailable' ELSE 'turn dispatch owner was lost' END,"
                            "ended_at=? WHERE id=? AND (status='running' OR (? AND status='clarification_pending'))",
                            (
                                rephrased_reference_pause,
                                rephrased_reference_pause,
                                rephrased_reference_pause,
                                now,
                                turn_id,
                                unrecoverable_reference_pause,
                            ),
                        )
                        if not deferred_reference_work:
                            await db.execute(
                                "UPDATE sessions SET active_turn_id=?,steer_buffer_json=? WHERE id=? AND active_turn_id=?",
                                (
                                    recovery_child_turn_id if steer_suffix else None,
                                    json.dumps(steer_suffix, ensure_ascii=False),
                                    session_id,
                                    turn_id,
                                ),
                            )
                        await db.execute(
                            "UPDATE chat_turn_spawn_dispatch SET state=?,owner_id=NULL,lease_expires_at=?,updated_at=? "
                            "WHERE turn_id=? AND ((state IN ('claimed','spawned') "
                            "AND lease_expires_at<=?) OR state='drain_pending')",
                            (
                                "terminal" if deferred_reference_work or rephrased_reference_pause else "failed",
                                now,
                                now,
                                turn_id,
                                now,
                            ),
                        )
                        await db.execute(
                            "RELEASE SAVEPOINT recover_chat_dispatch_row",
                        )
                        turn_ids.append(turn_id)

                    except (
                        SessionProtocolManagedError,
                        TerminalProtocolError,
                    ):
                        await db.execute(
                            "ROLLBACK TO SAVEPOINT recover_chat_dispatch_row",
                        )
                        await _isolate_legacy_continuation_in_tx(
                            db,
                            turn_id=turn_id,
                            session_id=session_id,
                            now=now,
                            reason=("legacy continuation recovery was isolated"),
                            parent_is_legacy=False,
                        )
                        await db.execute(
                            "RELEASE SAVEPOINT recover_chat_dispatch_row",
                        )
                        turn_ids.append(turn_id)
                        continue
                    except (
                        json.JSONDecodeError,
                        _LegacyContinuationRowError,
                        sqlite3.IntegrityError,
                    ) as exc:
                        if reference_runtime or reference_continuation:
                            # A reference terminal and its primary ledger must
                            # commit together. Legacy isolation would retire the
                            # dispatch after rolling back only the ledger/event,
                            # leaving no reservation that recovery can retry.
                            await db.execute("ROLLBACK TO SAVEPOINT recover_chat_dispatch_row")
                            await db.execute("RELEASE SAVEPOINT recover_chat_dispatch_row")
                            logging.getLogger(__name__).warning(
                                "reference_dispatch_recovery_retry turn_id=%s error_type=%s", turn_id, type(exc).__name__
                            )
                            continue
                        await db.execute(
                            "ROLLBACK TO SAVEPOINT recover_chat_dispatch_row",
                        )
                        await _isolate_legacy_continuation_in_tx(
                            db,
                            turn_id=turn_id,
                            session_id=session_id,
                            now=now,
                            reason=("legacy continuation recovery was isolated"),
                            parent_is_legacy=parent_is_validated_legacy,
                            discard_malformed_buffer=(isinstance(exc, json.JSONDecodeError) or buffer_is_malformed),
                        )
                        await db.execute(
                            "RELEASE SAVEPOINT recover_chat_dispatch_row",
                        )
                        turn_ids.append(turn_id)
                        continue
                await db.commit()
            except BaseException:
                if db.in_transaction:
                    await db.execute("ROLLBACK")
                raise
        return turn_ids

    async def materialize_steer_follow_up(
        self,
        *,
        session_id: str,
        current_turn_id: str,
        next_turn_id: str,
    ) -> list[str]:
        """Atomically turn an accepted steer buffer into a durable child Turn.

        The centralized adapter must already own a live parent dispatch. The
        child relation cannot become visible outside that ordered-tail fence.
        """
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                try:
                    parent = await _read_legacy_continuation_parent_in_tx(
                        db,
                        turn_id=current_turn_id,
                        session_id=session_id,
                    )
                except (SessionProtocolManagedError, TerminalProtocolError):
                    await _isolate_legacy_continuation_in_tx(
                        db,
                        turn_id=current_turn_id,
                        session_id=session_id,
                        now=now,
                        reason="legacy continuation protocol was isolated",
                        parent_is_legacy=False,
                    )
                    await db.commit()
                    raise SessionProtocolManagedError(
                        "legacy continuation protocol was isolated",
                    )
                if parent is None:
                    await db.execute("ROLLBACK")
                    return []
                session, parent_turn = parent
                from bowhead_core.document_clarification_lifecycle import document_pause_retained_in_tx

                if await document_pause_retained_in_tx(db, turn_id=current_turn_id):
                    await db.rollback()
                    return []
                reference_parent = await _requires_reference_execution_in_tx(
                    db,
                    current_turn_id,
                )
                if reference_parent:
                    from bowhead_core.chat_reference_runtime_store import (
                        is_reference_recoverable_in_connection,
                    )

                    if not await is_reference_recoverable_in_connection(
                        db,
                        turn_id=current_turn_id,
                    ):
                        raise ReferenceExecutionUnavailableError()
                try:
                    steer_segment = _segment_steer_buffer(session["steer_buffer_json"])
                except _LegacyContinuationRowError:
                    await _isolate_legacy_continuation_in_tx(
                        db,
                        turn_id=current_turn_id,
                        session_id=session_id,
                        now=now,
                        reason="legacy continuation buffer was malformed",
                        parent_is_legacy=True,
                        discard_malformed_buffer=True,
                    )
                    await db.commit()
                    raise SessionProtocolManagedError(
                        "legacy continuation buffer was isolated",
                    )
                if steer_segment.reference_submission_id is not None:
                    await db.execute("ROLLBACK")
                    raise ReferenceExecutionUnavailableError()
                steer_projection = _project_legacy_steer_buffer(
                    json.dumps(steer_segment.items, ensure_ascii=False),
                )
                async with db.execute(
                    "SELECT 1 FROM chat_turn_spawn_dispatch d JOIN turns t "
                    "ON t.id=d.turn_id WHERE d.turn_id=? AND d.session_id=? "
                    "AND d.state='spawned' AND d.owner_id IS NOT NULL "
                    "AND d.lease_expires_at>? AND d.lease_kind IN ('turn','resume') "
                    "AND d.lease_execution_generation=t.execution_generation",
                    (current_turn_id, session_id, now),
                ) as dispatch_cursor:
                    parent_dispatch_active = await dispatch_cursor.fetchone()
                if parent_dispatch_active is None:
                    await db.execute("ROLLBACK")
                    raise RuntimeError("legacy_parent_dispatch_not_active")
                if not steer_projection.source_parts:
                    if reference_parent:
                        # A BI clarification emits a terminal wire marker while
                        # retaining this same Turn for resume. With no accepted
                        # continuation there is nothing to drain here.
                        await db.execute("ROLLBACK")
                        return []
                    await db.execute(
                        "UPDATE sessions SET active_turn_id=?,steer_buffer_json=? WHERE id=?",
                        (
                            current_turn_id if steer_segment.suffix else None,
                            json.dumps(steer_segment.suffix, ensure_ascii=False),
                            session_id,
                        ),
                    )
                    await db.commit()
                    return []
                if reference_parent and str(parent_turn["status"]) == "clarification_pending":
                    # The same reference Turn must remain active until its BI
                    # answer advances the execution generation. Accepted steer
                    # work is drained only after that resumed generation ends.
                    await db.execute("ROLLBACK")
                    return []
                async with db.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE session_id=?",
                    (session_id,),
                ) as cur:
                    seq_row = await cur.fetchone()
                seq = int(seq_row[0]) if seq_row else 1
                await db.execute(
                    "INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES (?,?,?,?,?)",
                    (
                        next_turn_id,
                        session_id,
                        seq,
                        steer_projection.user_text,
                        now,
                    ),
                )
                await _insert_chat_turn_user_source_in_tx(
                    db,
                    source=ChatTurnUserSource(
                        turn_id=next_turn_id,
                        session_id=session_id,
                        user_id=str(session["user_id"]),
                        workspace_id=str(session["workspace_id"]),
                        user_text=steer_projection.user_text,
                        attachment_ids=steer_projection.attachment_ids,
                        attachment_refs=steer_projection.attachment_refs,
                        parts=steer_projection.source_parts,
                    ),
                    created_at=now,
                )
                await db.execute(
                    "INSERT INTO chat_turn_spawn_dispatch("
                    "turn_id,session_id,source_submission_id,parent_turn_id,"
                    "source_submission_ids_json,dispatch_kind,state,owner_id,"
                    "lease_expires_at,created_at,updated_at"
                    ") VALUES (?,?,?,?,?,'follow_up','claimed',NULL,?,?,?)",
                    (
                        next_turn_id,
                        session_id,
                        steer_projection.submission_ids[0] if steer_projection.submission_ids else None,
                        current_turn_id,
                        json.dumps(
                            steer_projection.submission_ids,
                            separators=(",", ":"),
                        ),
                        (datetime.now(tz=UTC) + timedelta(seconds=30)).isoformat(),
                        now,
                        now,
                    ),
                )
                await db.execute(
                    "UPDATE sessions SET active_turn_id=?,steer_buffer_json=? WHERE id=?",
                    (
                        next_turn_id,
                        json.dumps(steer_segment.suffix, ensure_ascii=False),
                        session_id,
                    ),
                )
                await db.commit()
                return list(steer_projection.texts)
            except BaseException:
                if db.in_transaction:
                    await db.execute("ROLLBACK")
                raise

    async def read_reference_steer_head(
        self,
        *,
        session_id: str,
        current_turn_id: str,
        reference_store: ChatReferenceStore,
        reference_scope: ReferenceScope,
    ) -> tuple[str, QueuedReferenceInput, int] | None:
        """Read one queued reference head for transaction-external preparation."""
        from bowhead_core.chat_reference_storage import ChatReferenceStore, ReferenceScope

        if type(reference_store) is not ChatReferenceStore or type(reference_scope) is not ReferenceScope:
            raise ValueError("reference_submission_materializer_unavailable")
        reference_scope = ReferenceScope.model_validate(reference_scope.model_dump(mode="python", warnings="error"))
        if reference_scope.session_id != session_id:
            raise ValueError("reference_submission_scope_invalid")
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            try:
                parent = await _read_legacy_continuation_parent_in_tx(
                    db,
                    turn_id=current_turn_id,
                    session_id=session_id,
                )
                if parent is None:
                    await db.execute("ROLLBACK")
                    return None
                from bowhead_core.document_clarification_lifecycle import document_pause_retained_in_tx

                if await document_pause_retained_in_tx(db, turn_id=current_turn_id):
                    await db.rollback()
                    return None
                segment = _segment_steer_buffer(parent[0]["steer_buffer_json"])
                submission_id = segment.reference_submission_id
                if submission_id is None:
                    await db.execute("ROLLBACK")
                    return None
                saved = await reference_store.get_queued_input_in_connection(
                    db,
                    scope=reference_scope,
                    submission_id=submission_id,
                )
                if saved is None or saved[1] != "queued":
                    raise ValueError("reference_queued_input_unavailable")
                payload, _state, version = saved
                head = segment.items[0]
                if (
                    payload.input_document.display_text() != head.get("text", "")
                    or payload.attachment_ids != head.get("attachment_ids", [])
                    or payload.attachment_refs != head.get("attachment_refs", [])
                ):
                    raise ValueError("reference_submission_source_changed")
                await db.execute("ROLLBACK")
                return submission_id, payload, version
            except BaseException:
                if db.in_transaction:
                    await db.execute("ROLLBACK")
                raise

    async def materialize_reference_steer_follow_up(
        self,
        *,
        session_id: str,
        current_turn_id: str,
        next_turn_id: str,
        submission_id: str,
        expected_queue_version: int,
        reference_store: ChatReferenceStore,
        reference_scope: ReferenceScope,
        reference_start: ReferenceStartAdmission,
    ) -> list[str]:
        """Atomically turn one prepared reference head into its durable child."""
        from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
        from bowhead_core.chat_reference_admission import ReferenceStartAdmission, admit_reference_start_in_connection
        from bowhead_core.chat_reference_storage import ChatReferenceStore, ReferenceScope

        if (
            type(reference_store) is not ChatReferenceStore
            or type(reference_scope) is not ReferenceScope
            or type(reference_start) is not ReferenceStartAdmission
        ):
            raise ValueError("reference_submission_materializer_unavailable")
        reference_scope = ReferenceScope.model_validate(reference_scope.model_dump(mode="python", warnings="error"))
        reference_start = reference_start.snapshot()
        if reference_scope.session_id != session_id or reference_scope.policy_epoch != reference_start.policy_epoch or expected_queue_version < 1:
            raise ValueError("reference_submission_scope_invalid")
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                parent = await _read_legacy_continuation_parent_in_tx(
                    db,
                    turn_id=current_turn_id,
                    session_id=session_id,
                )
                if parent is None:
                    await db.execute("ROLLBACK")
                    return []
                session, _parent_turn = parent
                from bowhead_core.document_clarification_lifecycle import document_pause_retained_in_tx

                if await document_pause_retained_in_tx(db, turn_id=current_turn_id):
                    await db.rollback()
                    return []
                segment = _segment_steer_buffer(session["steer_buffer_json"])
                if segment.reference_submission_id != submission_id:
                    raise ValueError("reference_queue_head_changed")
                saved = await reference_store.get_queued_input_in_connection(
                    db,
                    scope=reference_scope,
                    submission_id=submission_id,
                )
                if saved is None or saved[1] != "queued" or saved[2] != expected_queue_version:
                    raise ValueError("reference_queued_input_changed")
                payload = saved[0]
                head = segment.items[0]
                if (
                    payload.input_document != reference_start.input_document
                    or payload.automatic_receiver != reference_start.automatic_receiver
                    or payload.orchestration_contract != reference_start.orchestration_contract
                    or payload.input_document.display_text() != head.get("text", "")
                    or payload.attachment_ids != head.get("attachment_ids", [])
                    or payload.attachment_refs != head.get("attachment_refs", [])
                    or reference_start.has_attachments != bool(payload.attachment_ids)
                ):
                    raise ValueError("reference_submission_source_changed")
                if payload.attachment_ids or payload.attachment_refs:
                    from bowhead_core.chat_attachment_access import (
                        ChatAttachmentAccessError,
                        validate_chat_attachment_access_in_connection,
                    )

                    try:
                        await validate_chat_attachment_access_in_connection(
                            db,
                            refs=payload.attachment_refs,
                            runtime_ids=payload.attachment_ids,
                            user_id=reference_scope.user_id,
                            workspace_id=reference_scope.workspace_id,
                            session_id=session_id,
                        )
                    except ChatAttachmentAccessError as exc:
                        raise ValueError("reference_attachment_unavailable") from exc
                async with db.execute(
                    "SELECT 1 FROM chat_turn_spawn_dispatch d JOIN turns t "
                    "ON t.id=d.turn_id WHERE d.turn_id=? AND d.session_id=? "
                    "AND d.state='spawned' AND d.owner_id IS NOT NULL "
                    "AND d.lease_expires_at>? AND d.lease_kind IN ('turn','resume') "
                    "AND d.lease_execution_generation=t.execution_generation",
                    (current_turn_id, session_id, now),
                ) as dispatch_cursor:
                    if await dispatch_cursor.fetchone() is None:
                        raise RuntimeError("reference_parent_dispatch_not_active")
                async with db.execute(
                    "SELECT expires_at FROM chat_reference_queued_inputs WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    (reference_scope.team_id, reference_scope.user_id, submission_id),
                ) as queue_cursor:
                    queue_row = await queue_cursor.fetchone()
                if queue_row is None:
                    raise ValueError("reference_queued_input_unavailable")
                async with db.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE session_id=?",
                    (session_id,),
                ) as seq_cursor:
                    seq_row = await seq_cursor.fetchone()
                seq = int(seq_row[0]) if seq_row else 1
                source = ChatTurnUserSource(
                    turn_id=next_turn_id,
                    session_id=session_id,
                    user_id=reference_scope.user_id,
                    workspace_id=reference_scope.workspace_id,
                    user_text=payload.input_document.display_text(),
                    attachment_ids=tuple(payload.attachment_ids),
                    attachment_refs=tuple(payload.attachment_refs),
                    parts=None,
                    input_document=payload.input_document,
                )
                await db.execute(
                    "INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES (?,?,?,?,?)",
                    (next_turn_id, session_id, seq, source.user_text, now),
                )
                await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=now)
                await admit_reference_start_in_connection(
                    db,
                    admission=reference_start,
                    source=source,
                    team_id=reference_scope.team_id,
                    bindings=AgentExecutionBindingStore(self._db_path),
                    expires_at=str(queue_row["expires_at"]),
                    requested_session_id=session_id,
                )
                await db.execute(
                    "INSERT INTO chat_turn_spawn_dispatch("
                    "turn_id,session_id,source_submission_id,parent_turn_id,"
                    "source_submission_ids_json,dispatch_kind,state,owner_id,"
                    "lease_expires_at,created_at,updated_at"
                    ") VALUES (?,?,?,?,?,'follow_up','claimed',NULL,?,?,?)",
                    (
                        next_turn_id,
                        session_id,
                        submission_id,
                        current_turn_id,
                        json.dumps([submission_id], separators=(",", ":")),
                        (datetime.now(tz=UTC) + timedelta(seconds=30)).isoformat(),
                        now,
                        now,
                    ),
                )
                if not await reference_store.settle_queued_input_in_connection(
                    db,
                    scope=reference_scope,
                    submission_id=submission_id,
                    expected_version=expected_queue_version,
                    materialized_turn_id=next_turn_id,
                ):
                    raise ValueError("reference_queued_input_changed")
                updated_session = await db.execute(
                    "UPDATE sessions SET active_turn_id=?,steer_buffer_json=? WHERE id=? AND active_turn_id=?",
                    (
                        next_turn_id,
                        json.dumps(segment.suffix, ensure_ascii=False),
                        session_id,
                        current_turn_id,
                    ),
                )
                if updated_session.rowcount != 1:
                    raise ValueError("reference_parent_changed")
                await db.commit()
                return [source.user_text]
            except BaseException:
                if db.in_transaction:
                    await db.execute("ROLLBACK")
                raise

    async def get_chat_follow_up(self, *, parent_turn_id: str) -> tuple[str, int] | None:
        """Return the durable parent→child relation used by SSE replay."""
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT turn_id,source_submission_ids_json FROM chat_turn_spawn_dispatch WHERE parent_turn_id=? ORDER BY created_at DESC LIMIT 1",
                (parent_turn_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        try:
            source_ids = json.loads(str(row["source_submission_ids_json"] or "[]"))
        except json.JSONDecodeError:
            source_ids = []
        count = len(source_ids) if isinstance(source_ids, list) else 0
        return str(row["turn_id"]), count

    async def release_chat_submission_claim(
        self,
        *,
        team_id: str,
        user_id: str,
        client_submission_id: str,
        lease_owner: str,
        lease_version: int,
    ) -> bool:
        """Release only this handler's uncommitted lease."""
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM chat_submission_inflight "
                "WHERE team_id=? AND user_id=? AND client_submission_id=? "
                "AND lease_owner=? AND lease_version=? "
                "AND NOT EXISTS (SELECT 1 FROM chat_submission_receipts r "
                "WHERE r.team_id=? AND r.user_id=? AND r.client_submission_id=?)",
                (
                    team_id,
                    user_id,
                    client_submission_id,
                    lease_owner,
                    lease_version,
                    team_id,
                    user_id,
                    client_submission_id,
                ),
            )
            await db.commit()
            return cur.rowcount > 0

    async def finalize_started_chat_submission(
        self,
        *,
        team_id: str,
        user_id: str,
        client_submission_id: str,
        workspace_id: str,
        requested_session_id: str | None,
        actual_session_id: str,
        turn_id: str,
        user_text: str,
        attachment_ids: list[str],
        submission_kind: str,
        submission_contract_version: str,
        model_control_batch: str,
        request_fingerprint: str,
        lease_owner: str,
        lease_version: int,
        response: dict[str, Any],
        attachment_refs: list[dict[str, Any]] | None = None,
        parts: list[dict[str, Any]] | None = None,
        reference_start: ReferenceStartAdmission | None = None,
        document_revision_target: DocumentRevisionTarget | None = None,
    ) -> ChatSubmissionReceipt:
        """Atomically persist a Turn/receipt and optional prepared Agent/Skill references.

        reference_start is server-only. The HTTP reference reader and legacy
        execution barriers stay closed until the reference worker is ready.
        """
        is_reference = submission_contract_version == "chat-references.v1" or model_control_batch == "references"
        if document_revision_target is not None and (
            submission_kind != "start"
            or is_reference
            or reference_start is not None
            or requested_session_id != actual_session_id
            or parts is not None
            or response.get("document_revision_target") != document_revision_target.model_dump(mode="json")
        ):
            raise ValueError("document_revision_submission_invalid")
        prepared_source = None
        if reference_start is None:
            if is_reference:
                raise ValueError("reference_submission_materializer_unavailable")
        else:
            if (submission_contract_version, model_control_batch) != ("chat-references.v1", "references"):
                raise ValueError("reference_submission_contract_invalid")
            if submission_kind != "start" or parts is not None:
                raise ValueError("reference_start_unsupported")
            if requested_session_id is not None and actual_session_id != requested_session_id:
                raise ValueError("submission_session_scope_conflict")
            if response.get("session_id") != actual_session_id or response.get("turn_id") != turn_id or response.get("status") != "started":
                raise ValueError("reference_submission_response_invalid")
            reference_start = reference_start.snapshot()
            prepared_source = snapshot_chat_turn_user_source(
                ChatTurnUserSource(
                    turn_id=turn_id,
                    session_id=actual_session_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    user_text=user_text,
                    attachment_ids=tuple(attachment_ids),
                    attachment_refs=tuple(attachment_refs or ()),
                    parts=None,
                    input_document=reference_start.input_document,
                )
            )
        now = _now()
        expires_at = (datetime.now(tz=UTC) + timedelta(days=30)).isoformat()
        key = (team_id, user_id, client_submission_id)
        response = {
            **response,
            "execution_generation": 1,
        }
        response_json = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                if reference_start is not None:
                    # Waiting for the writer lock can outlive the claim lease.
                    now = _now()
                async with db.execute(
                    "SELECT * FROM chat_submission_receipts WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    key,
                ) as cur:
                    existing = await cur.fetchone()
                if existing is not None:
                    if str(existing["request_fingerprint"]) != request_fingerprint:
                        raise ValueError("submission_id_conflict")
                    if reference_start is not None:
                        if not _submission_scope_matches(existing, workspace_id=workspace_id, requested_session_id=requested_session_id):
                            raise ValueError("submission_session_scope_conflict")
                        if (existing["submission_contract_version"], existing["model_control_batch"], existing["submission_kind"]) != (
                            submission_contract_version,
                            model_control_batch,
                            submission_kind,
                        ):
                            raise ValueError("reference_submission_contract_invalid")
                        async with db.execute(
                            "SELECT state FROM chat_auth_session_generations WHERE user_id=? AND auth_session_generation_hash=?",
                            (user_id, str(existing["auth_session_generation_hash"])),
                        ) as auth_cursor:
                            auth_row = await auth_cursor.fetchone()
                        if auth_row is None or str(auth_row["state"]) != "active":
                            raise ValueError("submission_auth_generation_inactive")
                    await db.execute("ROLLBACK")
                    return _row_to_submission_receipt(existing)

                async with db.execute(
                    "SELECT * FROM chat_submission_inflight WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    key,
                ) as cur:
                    inflight = await cur.fetchone()
                if (
                    inflight is None
                    or str(inflight["lease_owner"]) != lease_owner
                    or int(inflight["lease_version"]) != lease_version
                    or str(inflight["request_fingerprint"]) != request_fingerprint
                ):
                    raise ValueError("submission_claim_lost")
                if reference_start is not None:
                    if not _submission_scope_matches(inflight, workspace_id=workspace_id, requested_session_id=requested_session_id):
                        raise ValueError("submission_session_scope_conflict")
                    if (inflight["submission_contract_version"], inflight["model_control_batch"], inflight["submission_kind"]) != (
                        submission_contract_version,
                        model_control_batch,
                        submission_kind,
                    ):
                        raise ValueError("reference_submission_contract_invalid")
                    if str(inflight["lease_expires_at"]) <= now:
                        raise ValueError("submission_claim_lost")
                elif inflight["submission_contract_version"] == "chat-references.v1" or inflight["model_control_batch"] == "references":
                    raise ValueError("reference_submission_materializer_unavailable")
                async with db.execute(
                    "SELECT state FROM chat_auth_session_generations WHERE user_id=? AND auth_session_generation_hash=?",
                    (user_id, str(inflight["auth_session_generation_hash"])),
                ) as auth_cursor:
                    auth_row = await auth_cursor.fetchone()
                if auth_row is None or str(auth_row["state"]) != "active":
                    raise ValueError("submission_auth_generation_inactive")
                async with db.execute(
                    "SELECT 1 FROM chat_submission_abandon_tombstones WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    key,
                ) as cur:
                    if await cur.fetchone() is not None:
                        raise ValueError("submission_abandoned")

                expected_source = prepared_source or ChatTurnUserSource(
                    turn_id=turn_id,
                    session_id=actual_session_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    user_text=user_text,
                    attachment_ids=tuple(attachment_ids),
                    attachment_refs=tuple(attachment_refs or ()),
                    parts=tuple(parts) if parts is not None else None,
                    document_revision_target=document_revision_target,
                )

                if reference_start is not None and (expected_source.attachment_ids or expected_source.attachment_refs):
                    from bowhead_core.chat_attachment_access import ChatAttachmentAccessError, validate_chat_attachment_access_in_connection

                    try:
                        await validate_chat_attachment_access_in_connection(
                            db,
                            refs=expected_source.attachment_refs,
                            runtime_ids=expected_source.attachment_ids,
                            user_id=user_id,
                            workspace_id=workspace_id,
                            session_id=requested_session_id,
                        )
                    except ChatAttachmentAccessError as exc:
                        raise ValueError("reference_attachment_unavailable") from exc

                await _ensure_chat_session_in_tx(
                    db,
                    session_id=actual_session_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    now=now,
                )
                await _read_exact_legacy_turn_or_fence_in_tx(
                    db,
                    session_id=actual_session_id,
                    turn_id=turn_id,
                    user_text=user_text,
                    allow_exact_replay=False,
                )
                async with db.execute(
                    "SELECT workspace_id,user_id,active_turn_id FROM sessions WHERE id=?",
                    (actual_session_id,),
                ) as cur:
                    session_scope = await cur.fetchone()
                if session_scope is None or str(session_scope["workspace_id"]) != workspace_id or str(session_scope["user_id"]) != user_id:
                    raise ValueError("submission_session_scope_conflict")
                async with db.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE session_id=?",
                    (actual_session_id,),
                ) as cur:
                    seq_row = await cur.fetchone()
                seq = int(seq_row[0]) if seq_row else 1
                await db.execute(
                    "INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES (?,?,?,?,?)",
                    (turn_id, actual_session_id, seq, user_text, now),
                )
                await _insert_chat_turn_user_source_in_tx(
                    db,
                    source=expected_source,
                    created_at=now,
                )
                if reference_start is not None:
                    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
                    from bowhead_core.chat_reference_admission import admit_reference_start_in_connection

                    await admit_reference_start_in_connection(
                        db,
                        admission=reference_start,
                        source=expected_source,
                        team_id=team_id,
                        bindings=AgentExecutionBindingStore(self._db_path),
                        expires_at=expires_at,
                        requested_session_id=requested_session_id,
                    )
                await db.execute(
                    "DELETE FROM user_session_archives WHERE session_id=? AND user_id=?",
                    (actual_session_id, user_id),
                )
                await db.execute(
                    "UPDATE sessions SET active_turn_id=?,last_active_at=? WHERE id=?",
                    (turn_id, now, actual_session_id),
                )
                if seq == 1:
                    lines = (user_text or "").strip().splitlines()
                    snippet = (lines[0][:40] if lines else "") or "新对话"
                    await db.execute(
                        "UPDATE sessions SET title=? WHERE id=? AND title_source='auto' AND (title IS NULL OR title='' OR title=id)",
                        (snippet, actual_session_id),
                    )
                await db.execute(
                    "INSERT INTO chat_submission_receipts("
                    "team_id,user_id,client_submission_id,workspace_id,requested_session_id,"
                    "actual_session_id,turn_id,submission_kind,submission_contract_version,"
                    "model_control_batch,request_fingerprint,fingerprint_key_id,"
                    "auth_session_generation_hash,outcome,response_json,created_at,expires_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        team_id,
                        user_id,
                        client_submission_id,
                        workspace_id,
                        requested_session_id,
                        actual_session_id,
                        turn_id,
                        submission_kind,
                        submission_contract_version,
                        model_control_batch,
                        request_fingerprint,
                        str(inflight["fingerprint_key_id"]),
                        str(inflight["auth_session_generation_hash"]),
                        "started",
                        response_json,
                        now,
                        expires_at,
                    ),
                )
                await db.execute(
                    "INSERT INTO chat_turn_spawn_dispatch("
                    "turn_id,session_id,source_submission_id,parent_turn_id,"
                    "source_submission_ids_json,dispatch_kind,state,owner_id,"
                    "lease_expires_at,created_at,updated_at"
                    ") VALUES (?,?,?,NULL,?,?,?,?,?,?,?)",
                    (
                        turn_id,
                        actual_session_id,
                        client_submission_id,
                        json.dumps(
                            [client_submission_id],
                            separators=(",", ":"),
                        ),
                        "start",
                        "claimed",
                        None,
                        (datetime.now(tz=UTC) + timedelta(seconds=30)).isoformat(),
                        now,
                        now,
                    ),
                )
                deleted = await db.execute(
                    "DELETE FROM chat_submission_inflight "
                    "WHERE team_id=? AND user_id=? AND client_submission_id=? "
                    "AND lease_owner=? AND lease_version=?",
                    (*key, lease_owner, lease_version),
                )
                if deleted.rowcount != 1:
                    raise ValueError("submission_claim_lost")
                await db.commit()
            except BaseException:
                await db.execute("ROLLBACK")
                raise
        return ChatSubmissionReceipt(
            team_id=team_id,
            user_id=user_id,
            client_submission_id=client_submission_id,
            workspace_id=workspace_id,
            requested_session_id=requested_session_id,
            actual_session_id=actual_session_id,
            turn_id=turn_id,
            submission_kind=submission_kind,
            submission_contract_version=submission_contract_version,
            model_control_batch=model_control_batch,
            request_fingerprint=request_fingerprint,
            outcome="started",
            response=response,
            created_at=now,
            expires_at=expires_at,
        )

    async def finalize_rejected_chat_submission(
        self,
        *,
        team_id: str,
        user_id: str,
        client_submission_id: str,
        workspace_id: str,
        requested_session_id: str | None,
        submission_kind: str,
        submission_contract_version: str,
        model_control_batch: str,
        request_fingerprint: str,
        lease_owner: str,
        lease_version: int,
        response: dict[str, Any],
    ) -> ChatSubmissionReceipt:
        """Persist a definitive zero-business-mutation rejection."""
        now = _now()
        expires_at = (datetime.now(tz=UTC) + timedelta(days=30)).isoformat()
        key = (team_id, user_id, client_submission_id)
        response_json = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM chat_submission_receipts WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    key,
                ) as cur:
                    existing = await cur.fetchone()
                if existing is not None:
                    if str(existing["request_fingerprint"]) != request_fingerprint:
                        raise ValueError("submission_id_conflict")
                    await db.execute("ROLLBACK")
                    return _row_to_submission_receipt(existing)
                async with db.execute(
                    "SELECT * FROM chat_submission_inflight WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    key,
                ) as cur:
                    inflight = await cur.fetchone()
                if (
                    inflight is None
                    or str(inflight["lease_owner"]) != lease_owner
                    or int(inflight["lease_version"]) != lease_version
                    or str(inflight["request_fingerprint"]) != request_fingerprint
                ):
                    raise ValueError("submission_claim_lost")
                async with db.execute(
                    "SELECT state FROM chat_auth_session_generations WHERE user_id=? AND auth_session_generation_hash=?",
                    (user_id, str(inflight["auth_session_generation_hash"])),
                ) as auth_cursor:
                    auth_row = await auth_cursor.fetchone()
                if auth_row is None or str(auth_row["state"]) != "active":
                    raise ValueError("submission_auth_generation_inactive")
                await db.execute(
                    "INSERT INTO chat_submission_receipts("
                    "team_id,user_id,client_submission_id,workspace_id,requested_session_id,"
                    "actual_session_id,turn_id,submission_kind,submission_contract_version,"
                    "model_control_batch,request_fingerprint,fingerprint_key_id,"
                    "auth_session_generation_hash,outcome,response_json,created_at,expires_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        team_id,
                        user_id,
                        client_submission_id,
                        workspace_id,
                        requested_session_id,
                        None,
                        None,
                        submission_kind,
                        submission_contract_version,
                        model_control_batch,
                        request_fingerprint,
                        str(inflight["fingerprint_key_id"]),
                        str(inflight["auth_session_generation_hash"]),
                        "rejected",
                        response_json,
                        now,
                        expires_at,
                    ),
                )
                deleted = await db.execute(
                    "DELETE FROM chat_submission_inflight "
                    "WHERE team_id=? AND user_id=? AND client_submission_id=? "
                    "AND lease_owner=? AND lease_version=?",
                    (*key, lease_owner, lease_version),
                )
                if deleted.rowcount != 1:
                    raise ValueError("submission_claim_lost")
                await db.commit()
            except BaseException:
                await db.execute("ROLLBACK")
                raise
        return ChatSubmissionReceipt(
            team_id=team_id,
            user_id=user_id,
            client_submission_id=client_submission_id,
            workspace_id=workspace_id,
            requested_session_id=requested_session_id,
            actual_session_id=None,
            turn_id=None,
            submission_kind=submission_kind,
            submission_contract_version=submission_contract_version,
            model_control_batch=model_control_batch,
            request_fingerprint=request_fingerprint,
            outcome="rejected",
            response=response,
            created_at=now,
            expires_at=expires_at,
        )

    async def finalize_steer_chat_submission(
        self,
        *,
        team_id: str,
        user_id: str,
        client_submission_id: str,
        workspace_id: str,
        requested_session_id: str,
        expected_parent_turn_id: str,
        expected_execution_generation: int,
        text: str,
        attachment_ids: list[str],
        submission_contract_version: str,
        model_control_batch: str,
        request_fingerprint: str,
        lease_owner: str,
        lease_version: int,
        attachment_refs: list[dict[str, Any]] | None = None,
        reference_store: ChatReferenceStore | None = None,
        reference_scope: ReferenceScope | None = None,
        reference_input: QueuedReferenceInput | None = None,
    ) -> ChatSubmissionReceipt:
        """Atomically apply one steer decision, source envelope and receipt.

        A reference buffer item remains fenced from the legacy materializer.
        The dedicated segmented materializer must settle its queued source before
        this writer is connected to an HTTP entry mode.
        """
        is_reference = submission_contract_version == "chat-references.v1" or model_control_batch == "references"
        if is_reference:
            from bowhead_core.chat_reference_storage import ChatReferenceStore, QueuedReferenceInput, ReferenceScope

            if (submission_contract_version, model_control_batch) != ("chat-references.v1", "references"):
                raise ValueError("reference_submission_contract_invalid")
            if (
                type(reference_store) is not ChatReferenceStore
                or type(reference_scope) is not ReferenceScope
                or type(reference_input) is not QueuedReferenceInput
            ):
                raise ValueError("reference_submission_materializer_unavailable")
            reference_scope = ReferenceScope.model_validate(reference_scope.model_dump(mode="python", warnings="error"))
            reference_input = QueuedReferenceInput.model_validate(reference_input.model_dump(mode="python", warnings="error"))
            if reference_scope.identity() != {
                "team_id": team_id,
                "user_id": user_id,
                "workspace_id": workspace_id,
                "session_id": requested_session_id,
            }:
                raise ValueError("reference_submission_scope_invalid")
            if (
                reference_input.input_document.display_text() != text
                or reference_input.attachment_ids != attachment_ids
                or reference_input.attachment_refs != (attachment_refs or [])
            ):
                raise ValueError("reference_submission_source_changed")
        elif any(value is not None for value in (reference_store, reference_scope, reference_input)):
            raise ValueError("reference_submission_contract_invalid")
        now = _now()
        expires_at = (datetime.now(tz=UTC) + timedelta(days=30)).isoformat()
        key = (team_id, user_id, client_submission_id)
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM chat_submission_receipts WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    key,
                ) as cur:
                    existing = await cur.fetchone()
                if existing is not None:
                    if str(existing["request_fingerprint"]) != request_fingerprint:
                        raise ValueError("submission_id_conflict")
                    await db.execute("ROLLBACK")
                    return _row_to_submission_receipt(existing)

                async with db.execute(
                    "SELECT * FROM chat_submission_inflight WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    key,
                ) as cur:
                    inflight = await cur.fetchone()
                if (
                    inflight is None
                    or str(inflight["lease_owner"]) != lease_owner
                    or int(inflight["lease_version"]) != lease_version
                    or str(inflight["request_fingerprint"]) != request_fingerprint
                ):
                    raise ValueError("submission_claim_lost")
                if is_reference:
                    if (inflight["submission_contract_version"], inflight["model_control_batch"], inflight["submission_kind"]) != (
                        submission_contract_version,
                        model_control_batch,
                        "steer",
                    ):
                        raise ValueError("reference_submission_contract_invalid")
                    if str(inflight["lease_expires_at"]) <= now:
                        raise ValueError("submission_claim_lost")
                elif inflight["submission_contract_version"] == "chat-references.v1" or inflight["model_control_batch"] == "references":
                    raise ValueError("reference_submission_materializer_unavailable")
                async with db.execute(
                    "SELECT state FROM chat_auth_session_generations WHERE user_id=? AND auth_session_generation_hash=?",
                    (user_id, str(inflight["auth_session_generation_hash"])),
                ) as auth_cursor:
                    auth_row = await auth_cursor.fetchone()
                if auth_row is None or str(auth_row["state"]) != "active":
                    raise ValueError("submission_auth_generation_inactive")

                parent = await _read_legacy_continuation_parent_in_tx(
                    db,
                    turn_id=expected_parent_turn_id,
                    session_id=requested_session_id,
                )
                if parent is None:
                    async with db.execute(
                        "SELECT workspace_id,user_id,active_turn_id,steer_buffer_json FROM sessions WHERE id=?",
                        (requested_session_id,),
                    ) as cur:
                        session = await cur.fetchone()
                    parent_turn = None
                else:
                    session, parent_turn = parent
                if session is None or str(session["workspace_id"]) != workspace_id or str(session["user_id"]) != user_id:
                    raise ValueError("submission_session_scope_conflict")

                active_turn_id = session["active_turn_id"]
                if (
                    parent_turn is None
                    or active_turn_id is None
                    or str(active_turn_id) != expected_parent_turn_id
                    or int(parent_turn["execution_generation"] or 0) != expected_execution_generation
                    or str(parent_turn["status"] or "") != "running"
                ):
                    outcome = "rejected"
                    response_turn_id = str(active_turn_id or expected_parent_turn_id)
                else:
                    outcome = "buffered"
                    response_turn_id = str(active_turn_id)
                    buffer = json.loads(str(session["steer_buffer_json"] or "[]"))
                    if not isinstance(buffer, list):
                        raise ValueError("invalid_steer_buffer")
                    buffered_input = {
                        "text": text,
                        "attachment_ids": attachment_ids,
                        "attachment_refs": attachment_refs or [],
                        "submission_id": client_submission_id,
                        "ts": now,
                    }
                    if is_reference:
                        buffered_input["reference_submission_id"] = client_submission_id
                    buffer.append(buffered_input)
                    await db.execute(
                        "UPDATE sessions SET steer_buffer_json=? WHERE id=?",
                        (json.dumps(buffer, ensure_ascii=False), requested_session_id),
                    )

                if outcome == "rejected":
                    response = {
                        "http_status": 409,
                        "detail": {"error": {"code": "steer_parent_not_active"}},
                    }
                else:
                    response = {
                        "session_id": requested_session_id,
                        "turn_id": response_turn_id,
                        "stream_url": f"/api/v1/chat/stream/{response_turn_id}",
                        "status": outcome,
                        "execution_generation": expected_execution_generation,
                    }
                response_json = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
                await db.execute(
                    "INSERT INTO chat_submission_receipts("
                    "team_id,user_id,client_submission_id,workspace_id,requested_session_id,"
                    "actual_session_id,turn_id,submission_kind,submission_contract_version,"
                    "model_control_batch,request_fingerprint,fingerprint_key_id,"
                    "auth_session_generation_hash,outcome,response_json,created_at,expires_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        team_id,
                        user_id,
                        client_submission_id,
                        workspace_id,
                        requested_session_id,
                        None if outcome == "rejected" else requested_session_id,
                        None if outcome == "rejected" else response_turn_id,
                        "steer",
                        submission_contract_version,
                        model_control_batch,
                        request_fingerprint,
                        str(inflight["fingerprint_key_id"]),
                        str(inflight["auth_session_generation_hash"]),
                        outcome,
                        response_json,
                        now,
                        expires_at,
                    ),
                )
                if is_reference and outcome == "buffered":
                    assert reference_store is not None and reference_scope is not None and reference_input is not None
                    await reference_store.put_queued_input_in_connection(
                        db,
                        scope=reference_scope,
                        submission_id=client_submission_id,
                        accepted_parent_turn_id=expected_parent_turn_id,
                        accepted_generation=expected_execution_generation,
                        payload=reference_input,
                        expires_at=expires_at,
                    )
                deleted = await db.execute(
                    "DELETE FROM chat_submission_inflight "
                    "WHERE team_id=? AND user_id=? AND client_submission_id=? "
                    "AND lease_owner=? AND lease_version=?",
                    (*key, lease_owner, lease_version),
                )
                if deleted.rowcount != 1:
                    raise ValueError("submission_claim_lost")
                await db.commit()
            except BaseException:
                await db.execute("ROLLBACK")
                raise
        return ChatSubmissionReceipt(
            team_id=team_id,
            user_id=user_id,
            client_submission_id=client_submission_id,
            workspace_id=workspace_id,
            requested_session_id=requested_session_id,
            actual_session_id=None if outcome == "rejected" else requested_session_id,
            turn_id=None if outcome == "rejected" else response_turn_id,
            submission_kind="steer",
            submission_contract_version=submission_contract_version,
            model_control_batch=model_control_batch,
            request_fingerprint=request_fingerprint,
            outcome=outcome,
            response=response,
            created_at=now,
            expires_at=expires_at,
        )

    async def cancel_queued_reference_submission(
        self,
        *,
        team_id: str,
        user_id: str,
        client_submission_id: str,
        workspace_id: str,
        session_id: str,
        auth_session_generation_hash: str,
    ) -> Literal["cancelled", "materialized", "not_found"]:
        """Cancel one accepted reference follow-up without stopping its holder.

        The queue decision and removal of the exact buffer pointer commit in
        one transaction. Cancellation does not open the encrypted input, so it
        remains available when reference key material is temporarily absent.
        """
        from bowhead_core.chat_reference_storage import ChatReferenceStore, ReferenceScope

        key = (team_id, user_id, client_submission_id)
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT q.state,q.row_version,q.workspace_id,q.session_id,
                           s.steer_buffer_json,s.deleted_at,
                           w.status AS workspace_status,COALESCE(p.epoch,0) AS policy_epoch
                    FROM chat_reference_queued_inputs q
                    JOIN sessions s ON s.id=q.session_id AND s.user_id=q.user_id
                    JOIN workspaces w ON w.id=q.workspace_id AND w.id=s.workspace_id
                                     AND w.team_id=q.team_id
                    JOIN users u ON u.id=q.user_id AND u.status='active'
                    JOIN teams t ON t.id=q.team_id AND t.status='active'
                    JOIN team_members tm ON tm.team_id=q.team_id AND tm.user_id=q.user_id
                                        AND tm.status='active'
                    JOIN workspace_members wm ON wm.workspace_id=q.workspace_id
                                             AND wm.user_id=q.user_id AND wm.status='active'
                    LEFT JOIN access_policy_state p ON p.team_id=q.team_id
                    WHERE q.team_id=? AND q.user_id=? AND q.client_submission_id=?
                    """,
                    key,
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None or str(row["workspace_id"]) != workspace_id or str(row["session_id"]) != session_id:
                    await db.execute("ROLLBACK")
                    return "not_found"
                async with db.execute(
                    """
                    SELECT 1 FROM chat_auth_session_generations
                    WHERE user_id=? AND auth_session_generation_hash=?
                      AND state='active'
                    """,
                    (user_id, auth_session_generation_hash),
                ) as generation_cursor:
                    if await generation_cursor.fetchone() is None:
                        raise ValueError("submission_auth_generation_inactive")
                state = str(row["state"])
                if state == "materialized":
                    await db.execute("ROLLBACK")
                    return "materialized"
                if state not in {"queued", "cancelled"}:
                    raise ValueError("reference_queue_cancel_unavailable")
                if row["deleted_at"] is not None or str(row["workspace_status"]) != "active":
                    if state == "cancelled":
                        await db.execute("ROLLBACK")
                        return "cancelled"
                    raise ValueError("reference_queue_cancel_unavailable")

                original_buffer_json = str(row["steer_buffer_json"] or "[]")
                try:
                    buffer = _decode_steer_buffer(original_buffer_json)
                except _LegacyContinuationRowError as exc:
                    raise ValueError("reference_queue_cancel_buffer_changed") from exc
                matching = [index for index, item in enumerate(buffer) if item.get("reference_submission_id") == client_submission_id]
                if len(matching) > 1 or (state == "queued" and not matching):
                    raise ValueError("reference_queue_cancel_buffer_changed")
                if not matching:
                    await db.execute("ROLLBACK")
                    return "cancelled"
                item = buffer[matching[0]]
                if item.get("submission_id") != client_submission_id:
                    raise ValueError("reference_queue_cancel_buffer_changed")

                if state == "queued":
                    settled = await ChatReferenceStore.cancel_queued_input_in_connection(
                        db,
                        scope=ReferenceScope(
                            team_id=team_id,
                            user_id=user_id,
                            workspace_id=workspace_id,
                            session_id=session_id,
                            policy_epoch=int(row["policy_epoch"]),
                        ),
                        submission_id=client_submission_id,
                        expected_version=int(row["row_version"]),
                        cancel_reason="user_cancelled",
                    )
                    if not settled:
                        raise ValueError("reference_queue_cancel_changed")
                del buffer[matching[0]]
                updated = await db.execute(
                    "UPDATE sessions SET steer_buffer_json=? WHERE id=? AND steer_buffer_json=?",
                    (
                        json.dumps(buffer, ensure_ascii=False),
                        session_id,
                        original_buffer_json,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError("reference_queue_cancel_buffer_changed")
                await db.commit()
            except BaseException:
                if db.in_transaction:
                    await db.execute("ROLLBACK")
                raise
        return "cancelled"

    async def list_queued_reference_submissions(
        self,
        *,
        team_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        auth_session_generation_hash: str,
        reference_store: ChatReferenceStore | None,
    ) -> tuple[QueuedReferenceSubmission, ...]:
        """Project the ordered, still-pending reference follow-ups for one Session.

        The buffer supplies queue order only. User text and resource labels are
        reopened from the immutable encrypted source, so a stale display pointer
        cannot become the UI's source of truth. This reader remains available
        after the writer feature flag is disabled.
        """
        from bowhead_core.chat_reference_storage import ChatReferenceStore, ReferenceScope
        from bowhead_core.chat_references import ReferenceNode

        if reference_store is not None and type(reference_store) is not ChatReferenceStore:
            raise TypeError("reference_store must be ChatReferenceStore")
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            try:
                async with db.execute(
                    """
                    SELECT s.steer_buffer_json,s.deleted_at,
                           w.status AS workspace_status,COALESCE(p.epoch,0) AS policy_epoch
                    FROM sessions s
                    JOIN workspaces w ON w.id=s.workspace_id AND w.team_id=?
                    JOIN users u ON u.id=s.user_id AND u.status='active'
                    JOIN teams t ON t.id=w.team_id AND t.status='active'
                    JOIN team_members tm ON tm.team_id=t.id AND tm.user_id=s.user_id
                                        AND tm.status='active'
                    JOIN workspace_members wm ON wm.workspace_id=w.id AND wm.user_id=s.user_id
                                             AND wm.status='active'
                    LEFT JOIN access_policy_state p ON p.team_id=t.id
                    WHERE s.id=? AND s.user_id=? AND s.workspace_id=?
                    """,
                    (team_id, session_id, user_id, workspace_id),
                ) as cursor:
                    session = await cursor.fetchone()
                if session is None:
                    await db.execute("ROLLBACK")
                    return ()
                async with db.execute(
                    """
                    SELECT 1 FROM chat_auth_session_generations
                    WHERE user_id=? AND auth_session_generation_hash=? AND state='active'
                    """,
                    (user_id, auth_session_generation_hash),
                ) as generation_cursor:
                    if await generation_cursor.fetchone() is None:
                        raise ValueError("submission_auth_generation_inactive")
                if session["deleted_at"] is not None or str(session["workspace_status"]) not in {"active", "archived"}:
                    await db.execute("ROLLBACK")
                    return ()
                try:
                    buffer = _decode_steer_buffer(str(session["steer_buffer_json"] or "[]"))
                except _LegacyContinuationRowError as exc:
                    raise ValueError("reference_queue_list_unavailable") from exc
                pointer_ids = [str(item["reference_submission_id"]) for item in buffer if isinstance(item.get("reference_submission_id"), str)]
                if len(pointer_ids) != len(set(pointer_ids)):
                    raise ValueError("reference_queue_list_unavailable")

                async with db.execute(
                    """
                    SELECT client_submission_id,state,created_at
                    FROM chat_reference_queued_inputs
                    WHERE team_id=? AND user_id=? AND workspace_id=? AND session_id=?
                    """,
                    (team_id, user_id, workspace_id, session_id),
                ) as queued_cursor:
                    rows = await queued_cursor.fetchall()
                queued_rows = {str(row["client_submission_id"]): row for row in rows}
                if any(str(row["state"]) == "queued" and submission_id not in pointer_ids for submission_id, row in queued_rows.items()):
                    raise ValueError("reference_queue_list_unavailable")
                if not pointer_ids:
                    await db.execute("ROLLBACK")
                    return ()
                if reference_store is None:
                    raise ValueError("reference_queue_list_unavailable")

                scope = ReferenceScope(
                    team_id=team_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    policy_epoch=int(session["policy_epoch"]),
                )
                projected: list[QueuedReferenceSubmission] = []
                for submission_id in pointer_ids:
                    row = queued_rows.get(submission_id)
                    if row is None or str(row["state"]) == "materialized":
                        raise ValueError("reference_queue_list_unavailable")
                    if str(row["state"]) == "cancelled":
                        continue
                    saved = await reference_store.get_queued_input_in_connection(
                        db,
                        scope=scope,
                        submission_id=submission_id,
                    )
                    if saved is None or saved[1] != "queued":
                        raise ValueError("reference_queue_list_unavailable")
                    payload = saved[0]
                    projected.append(
                        QueuedReferenceSubmission(
                            client_submission_id=submission_id,
                            display_text=payload.input_document.display_text(),
                            resources=tuple(
                                QueuedReferenceResource(
                                    kind=node.resource.kind,
                                    label=node.label,
                                )
                                for node in payload.input_document.nodes
                                if isinstance(node, ReferenceNode)
                            ),
                            attachment_count=len(payload.attachment_ids),
                            accepted_at=str(row["created_at"]),
                        )
                    )
                await db.execute("ROLLBACK")
                return tuple(projected)
            except BaseException:
                if db.in_transaction:
                    await db.execute("ROLLBACK")
                raise

    async def get_chat_submission_receipt(
        self,
        *,
        team_id: str,
        user_id: str,
        client_submission_id: str,
        workspace_id: str,
        requested_session_id: str | None,
    ) -> ChatSubmissionReceipt | None:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM chat_submission_receipts WHERE team_id=? AND user_id=? AND client_submission_id=? AND expires_at>?",
                (team_id, user_id, client_submission_id, _now()),
            ) as cur:
                row = await cur.fetchone()
        if row is None or not _submission_scope_matches(
            row,
            workspace_id=workspace_id,
            requested_session_id=requested_session_id,
        ):
            return None
        return _row_to_submission_receipt(row)

    async def abandon_chat_submission(
        self,
        *,
        team_id: str,
        user_id: str,
        client_submission_id: str,
        workspace_id: str,
        requested_session_id: str | None,
    ) -> ChatSubmissionReceipt | Literal["abandoned", "not_found"]:
        """Fence an unresolved ID without requiring its original request body."""
        now = _now()
        expires_at = (datetime.now(tz=UTC) + timedelta(days=30)).isoformat()
        key = (team_id, user_id, client_submission_id)
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "DELETE FROM chat_submission_receipts WHERE team_id=? AND user_id=? AND client_submission_id=? AND expires_at<=?",
                    (*key, now),
                )
                await db.execute(
                    "DELETE FROM chat_submission_abandon_tombstones WHERE team_id=? AND user_id=? AND client_submission_id=? AND expires_at<=?",
                    (*key, now),
                )
                async with db.execute(
                    "SELECT * FROM chat_submission_receipts WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    key,
                ) as cur:
                    receipt_row = await cur.fetchone()
                if receipt_row is not None:
                    await db.execute("ROLLBACK")
                    if not _submission_scope_matches(
                        receipt_row,
                        workspace_id=workspace_id,
                        requested_session_id=requested_session_id,
                    ):
                        return "not_found"
                    return _row_to_submission_receipt(receipt_row)
                async with db.execute(
                    "SELECT * FROM chat_submission_abandon_tombstones WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    key,
                ) as cur:
                    tombstone = await cur.fetchone()
                if tombstone is not None and not _submission_scope_matches(
                    tombstone,
                    workspace_id=workspace_id,
                    requested_session_id=requested_session_id,
                ):
                    await db.execute("ROLLBACK")
                    return "not_found"
                async with db.execute(
                    "SELECT * FROM chat_submission_inflight WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    key,
                ) as cur:
                    inflight = await cur.fetchone()
                if inflight is not None and not _submission_scope_matches(
                    inflight,
                    workspace_id=workspace_id,
                    requested_session_id=requested_session_id,
                ):
                    await db.execute("ROLLBACK")
                    return "not_found"
                await db.execute(
                    "INSERT OR IGNORE INTO chat_submission_abandon_tombstones("
                    "team_id,user_id,client_submission_id,workspace_id,requested_session_id,created_at,expires_at"
                    ") VALUES (?,?,?,?,?,?,?)",
                    (*key, workspace_id, requested_session_id, now, expires_at),
                )
                await db.execute(
                    "DELETE FROM chat_submission_inflight WHERE team_id=? AND user_id=? AND client_submission_id=?",
                    key,
                )
                await db.commit()
            except BaseException:
                await db.execute("ROLLBACK")
                raise
        return "abandoned"

    async def record_turn(
        self,
        *,
        turn_id: str,
        session_id: str,
        user_text: str,
        user_source: ChatTurnUserSource | None = None,
    ) -> Turn:
        """Persist a Turn and its optional canonical source in one transaction."""
        source = snapshot_chat_turn_user_source(user_source) if user_source is not None else None
        if source is not None and (
            source.turn_id != turn_id or source.session_id != session_id or source.user_text != user_text or source.input_document is not None
        ):
            raise ValueError("chat_turn_user_source_scope_conflict")
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing = await _read_exact_legacy_turn_or_fence_in_tx(
                    db,
                    session_id=session_id,
                    turn_id=turn_id,
                    user_text=user_text,
                    allow_exact_replay=True,
                )
                if existing is not None:
                    if source is not None:
                        saved_source = await _read_chat_turn_user_source_in_tx(db, turn_id=turn_id)
                        if saved_source is None or canonical_json_v1(chat_turn_user_source_projection(saved_source)) != canonical_json_v1(
                            chat_turn_user_source_projection(source)
                        ):
                            raise ValueError("chat_turn_user_source_conflict")
                    await db.execute("ROLLBACK")
                    return _row_to_turn(existing)
                async with db.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM turns WHERE session_id=?",
                    (session_id,),
                ) as cur:
                    r = await cur.fetchone()
                seq = int(r[0]) if r else 1
                inserted = await db.execute(
                    "INSERT INTO turns(id, session_id, seq, user_text, started_at) VALUES (?, ?, ?, ?, ?)",
                    (turn_id, session_id, seq, user_text, now),
                )
                if inserted.rowcount != 1:
                    raise RuntimeError("legacy Turn insert was not committed")
                if source is not None:
                    await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=now)
                await db.execute(
                    "DELETE FROM user_session_archives WHERE session_id=? AND user_id=(  SELECT user_id FROM sessions WHERE id=?)",
                    (session_id, session_id),
                )
                await db.execute(
                    "UPDATE sessions SET active_turn_id=? WHERE id=?",
                    (turn_id, session_id),
                )
                # #133: set a temporary title immediately on the first turn (the real AI title overwrites it later).
                # Only replace an empty title or a session_id-shaped title; preserve existing AI titles.
                if seq == 1:
                    lines = (user_text or "").strip().splitlines()
                    snippet = (lines[0][:40] if lines else "") or "新对话"
                    await db.execute(
                        "UPDATE sessions SET title = ? WHERE id = ? AND title_source = 'auto' AND (title IS NULL OR title = '' OR title = id)",
                        (snippet, session_id),
                    )
                # prod-bug-bash-038 #9-B: bump last_active_at so follow-up
                # turns in the same session sort correctly. Idempotent late
                # record calls are not new activity and must not reorder it.
                await db.execute(
                    "UPDATE sessions SET last_active_at=? WHERE id=? AND last_active_at < ?",
                    (now, session_id, now),
                )
                async with db.execute(
                    "SELECT * FROM turns WHERE id=?",
                    (turn_id,),
                ) as cursor:
                    persisted = await cursor.fetchone()
                if persisted is None:
                    raise RuntimeError("legacy Turn row disappeared")
                await db.commit()
            except BaseException:
                await db.execute("ROLLBACK")
                raise
        return _row_to_turn(persisted)

    # ─── Sprint π Phase A — terminal-status persisters ──────────────────
    #
    # Per tech-design r2 §A: the 5 `mark_*` methods are the *authoritative*
    # write path for turn terminal status. ``complete_turn`` remains only for
    # the explicit rollback drill until D6b removes that final compatibility.
    # All 5 write `ended_at = now`; `mark_failed` / `mark_clarification_pending`
    # additionally persist `error_kind` when present so the row carries enough
    # context for /sessions UI to render reason chips without re-derivation.
    async def mark_completed(
        self,
        *,
        turn_id: str,
        worker: DocumentTurnOwner | None = None,
        intent: str | None,
        final_answer: str,
        tokens_input: int,
        tokens_output: int,
        cost_usd: float,
        error_kind: str | None = None,
    ) -> bool:
        async with open_shared_ctx(self._db_path) as db:
            if not await _begin_worker_transition(db, turn_id=turn_id, worker=worker):
                return False
            now = _now()
            cursor = await db.execute(
                """
                UPDATE turns SET
                    status='completed',
                    ended_at=?,
                    intent=?,
                    final_answer=?,
                    tokens_input=generation_base_tokens_input+?,
                    tokens_output=generation_base_tokens_output+?,
                    cost_usd=generation_base_cost_usd+?,
                    error_kind=?
                WHERE id=? AND status='running'
                """,
                # D7a: write error_kind directly (NOT COALESCE). A turn that reaches
                # `completed` succeeded, so error_kind=None on success must CLEAR any
                # value left by an earlier phase. Same-Turn resume first advances
                # clarification_pending→running in spawn_resume_turn, so completion
                # never needs to overwrite a paused terminal row directly.
                # (mark_aborted/mark_cancelled/mark_clarification_pending keep COALESCE
                #  on purpose — those are non-success terminal states.)
                (now, intent, final_answer, tokens_input, tokens_output, cost_usd, error_kind, turn_id),
            )
            transitioned = cursor.rowcount == 1
            if transitioned:
                # D7b: roll the winner's tokens+cost into the session. A late
                # terminal loser must have no aggregate or ordering side effects.
                await self._rollup_session_usage(db, turn_id)
                await db.execute(
                    "UPDATE sessions SET last_active_at=? WHERE id=(SELECT session_id FROM turns WHERE id=?) AND last_active_at < ?",
                    (now, turn_id, now),
                )
            await db.commit()
        return transitioned

    async def mark_cancelled(
        self,
        *,
        turn_id: str,
        worker: DocumentTurnOwner | None = None,
        intent: str | None = None,
        tokens_input: int = 0,
        tokens_output: int = 0,
        cost_usd: float = 0.0,
        error_kind: str | None = None,
    ) -> bool:
        """M2.1: 用户 stop 后落库. 跟 mark_completed 类似但 status='cancelled' + 不要求 final_answer.

        计费: tokens 已用部分仍累计 (业界 ChatGPT/Claude 都按 used 收费即使 stop).
        见 docs/M157-cancel-turn-design.md §5
        """
        async with open_shared_ctx(self._db_path) as db:
            if not await _begin_worker_transition(db, turn_id=turn_id, worker=worker):
                return False
            now = _now()
            cursor = await db.execute(
                """
                UPDATE turns SET
                    status='cancelled',
                    ended_at=?,
                    intent=COALESCE(?, intent),
                    tokens_input=generation_base_tokens_input+?,
                    tokens_output=generation_base_tokens_output+?,
                    cost_usd=generation_base_cost_usd+?,
                    error_kind=COALESCE(?, error_kind)
                WHERE id=? AND status='running'
                """,
                (
                    now,
                    intent,
                    tokens_input,
                    tokens_output,
                    cost_usd,
                    error_kind,
                    turn_id,
                ),
            )
            transitioned = cursor.rowcount == 1
            if transitioned:
                await self._rollup_session_usage(db, turn_id)
                await db.execute(
                    "UPDATE sessions SET last_active_at=? WHERE id=(SELECT session_id FROM turns WHERE id=?) AND last_active_at < ?",
                    (now, turn_id, now),
                )
            await db.commit()
        return transitioned

    async def finalize_cancelled_usage(
        self,
        *,
        turn_id: str,
        expected_execution_generation: int,
        intent: str | None = None,
        tokens_input: int = 0,
        tokens_output: int = 0,
        cost_usd: float = 0.0,
    ) -> bool:
        """Idempotently enrich a durable Stop with its owning worker's usage.

        The HTTP Stop transaction wins the terminal CAS before signalling the
        worker.  That worker may still report exact partial usage, but only for
        the same execution generation; this method never changes terminal state.
        """
        async with open_shared_ctx(self._db_path) as db:
            cursor = await db.execute(
                "UPDATE turns SET intent=COALESCE(?,intent),"
                "tokens_input=MAX(tokens_input,generation_base_tokens_input+?),"
                "tokens_output=MAX(tokens_output,generation_base_tokens_output+?),"
                "cost_usd=MAX(cost_usd,generation_base_cost_usd+?) "
                "WHERE id=? AND status='cancelled' AND execution_generation=?",
                (
                    intent,
                    tokens_input,
                    tokens_output,
                    cost_usd,
                    turn_id,
                    expected_execution_generation,
                ),
            )
            finalized = cursor.rowcount == 1
            if finalized:
                await self._rollup_session_usage(db, turn_id)
            await db.commit()
        return finalized

    async def try_claim_auto_plan_for_live_turn(
        self,
        *,
        plan_id: str,
        turn_id: str,
    ) -> bool:
        """Claim an Auto Plan only while its exact admitted Turn is live.

        Auto discovers its Plan inside an already-running Turn, so unlike an
        explicit approval it cannot bind the Plan before Turn admission.  The
        Plan CAS and the legacy/Terminal Stop facts must therefore share this
        transaction: Stop-first rejects the claim; claim-first lets Stop cancel
        the newly bound Plan in its existing transaction.
        """
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "UPDATE plans SET status='running',execution_turn_id=?,updated_at=? "
                    "WHERE id=? AND status IN ('proposed','blocked') "
                    "AND execution_turn_id IS NULL AND EXISTS ("
                    "SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id "
                    "WHERE t.id=? AND t.status='running' AND s.active_turn_id=t.id "
                    "AND plans.session_id=t.session_id "
                    "AND plans.workspace_id=s.workspace_id "
                    "AND plans.user_id=s.user_id AND ("
                    "(t.terminal_protocol_version IS NULL AND EXISTS ("
                    "SELECT 1 FROM chat_turn_spawn_dispatch d WHERE d.turn_id=t.id "
                    "AND d.state IN ('claimed','spawned') AND d.cancel_requested_at IS NULL "
                    "AND d.lease_expires_at>?)) OR "
                    "(t.terminal_protocol_version=1 AND s.active_turn_protocol_version=1 "
                    "AND s.active_turn_generation=t.current_generation AND EXISTS ("
                    "SELECT 1 FROM turn_generations g WHERE g.turn_id=t.id "
                    "AND g.generation=t.current_generation AND g.protocol_version=1 "
                    "AND g.state='running' AND g.lease_expires_at>?))"
                    "))",
                    (turn_id, now, plan_id, turn_id, now, now),
                )
                await db.commit()
                return cursor.rowcount == 1
            except BaseException:
                with contextlib.suppress(Exception):
                    await db.execute("ROLLBACK")
                raise

    async def request_cancel_turn(
        self,
        *,
        team_id: str,
        user_id: str,
        cancel_request_id: str,
        workspace_id: str,
        turn_id: str,
        expected_execution_generation: int,
        include_plan_outcome: bool = False,
    ) -> (
        Literal["cancelled", "terminal", "generation_conflict", "missing"]
        | tuple[
            Literal["cancelled", "terminal", "generation_conflict", "missing"],
            bool,
        ]
    ):
        """Durably stamp a user cancellation before the HTTP acknowledgement.

        The in-memory signal still stops the worker and lets its finalizer write
        exact partial usage.  This transaction supplies the crash-safe boundary:
        once the endpoint returns, reload cannot resurrect a running Turn.  A
        previously acknowledged steer queue remains attached to the parent and
        is materialized by the normal terminal/recovery path; Stop is not a
        queue-abandon operation.
        """
        now = _now()
        expires_at = (datetime.now(tz=UTC) + timedelta(days=30)).isoformat()
        plan_cancelled = False
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT turn_id,workspace_id,expected_execution_generation,outcome "
                    "FROM turn_cancel_receipts WHERE team_id=? AND user_id=? "
                    "AND cancel_request_id=?",
                    (team_id, user_id, cancel_request_id),
                ) as receipt_cursor:
                    existing = await receipt_cursor.fetchone()
                if existing is not None:
                    if (
                        str(existing["turn_id"]) != turn_id
                        or str(existing["workspace_id"]) != workspace_id
                        or int(existing["expected_execution_generation"]) != expected_execution_generation
                    ):
                        raise ValueError("cancel_request_conflict")
                    existing_outcome = str(existing["outcome"])
                    if include_plan_outcome and existing_outcome in (
                        "cancelled",
                        "terminal",
                    ):
                        plan_cancelled = await _cancel_running_plan_for_turn_in_tx(
                            db,
                            turn_id=turn_id,
                            updated_at=now,
                        )
                        await db.commit()
                    else:
                        await db.execute("ROLLBACK")
                    if include_plan_outcome:
                        return existing_outcome, plan_cancelled  # type: ignore[return-value]
                    return existing_outcome  # type: ignore[return-value]

                async with db.execute(
                    "SELECT t.session_id,t.status,t.execution_generation,"
                    "s.workspace_id,s.user_id,s.steer_buffer_json FROM turns t "
                    "JOIN sessions s ON s.id=t.session_id WHERE t.id=?",
                    (turn_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.execute("ROLLBACK")
                    return "missing"
                if str(row["workspace_id"]) != workspace_id or str(row["user_id"]) != user_id:
                    raise ValueError("cancel_turn_scope_conflict")
                session_id = str(row["session_id"])
                if int(row["execution_generation"] or 1) != expected_execution_generation:
                    outcome = "generation_conflict"
                elif str(row["status"]) != "running":
                    outcome = "terminal"
                else:
                    outcome = "cancelled"
                    await db.execute(
                        "UPDATE turns SET status='cancelled',ended_at=?,error_kind='user_cancel' "
                        "WHERE id=? AND status='running' AND execution_generation=?",
                        (now, turn_id, expected_execution_generation),
                    )
                    await db.execute(
                        "UPDATE sessions SET active_turn_id=CASE WHEN steer_buffer_json='[]' "
                        "THEN NULL ELSE active_turn_id END,last_active_at=? "
                        "WHERE id=? AND active_turn_id=?",
                        (now, session_id, turn_id),
                    )
                    await db.execute(
                        "UPDATE chat_turn_spawn_dispatch SET cancel_requested_at=?,updated_at=? WHERE turn_id=? AND state IN ('claimed','spawned')",
                        (now, now, turn_id),
                    )
                # Turn and its bound Plan are one cancellation unit.  Keeping
                # this in the same BEGIN IMMEDIATE transaction means a durable
                # HTTP acknowledgement can never expose cancelled Turn +
                # RUNNING Plan after a process crash or transient DB failure.
                if include_plan_outcome and outcome in ("cancelled", "terminal"):
                    plan_cancelled = await _cancel_running_plan_for_turn_in_tx(
                        db,
                        turn_id=turn_id,
                        updated_at=now,
                    )
                await db.execute(
                    "INSERT INTO turn_cancel_receipts("
                    "team_id,user_id,cancel_request_id,workspace_id,turn_id,"
                    "expected_execution_generation,outcome,turn_erased,created_at,expires_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        team_id,
                        user_id,
                        cancel_request_id,
                        workspace_id,
                        turn_id,
                        expected_execution_generation,
                        outcome,
                        0,
                        now,
                        expires_at,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.execute("ROLLBACK")
                raise
        if include_plan_outcome:
            return outcome, plan_cancelled
        return outcome

    async def get_cancel_turn_receipt(
        self,
        *,
        team_id: str,
        user_id: str,
        cancel_request_id: str,
        turn_id: str,
        expected_execution_generation: int,
    ) -> TurnCancelReceipt | None:
        """Read an unexpired Stop receipt even after its Turn was erased."""
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM turn_cancel_receipts WHERE team_id=? AND user_id=? AND cancel_request_id=? AND expires_at>?",
                (team_id, user_id, cancel_request_id, _now()),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        if str(row["turn_id"]) != turn_id or int(row["expected_execution_generation"]) != expected_execution_generation:
            raise ValueError("cancel_request_conflict")
        return TurnCancelReceipt(
            team_id=str(row["team_id"]),
            user_id=str(row["user_id"]),
            cancel_request_id=str(row["cancel_request_id"]),
            workspace_id=str(row["workspace_id"]),
            turn_id=str(row["turn_id"]),
            expected_execution_generation=int(row["expected_execution_generation"]),
            outcome=str(row["outcome"]),  # type: ignore[arg-type]
            turn_erased=bool(row["turn_erased"]),
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
        )

    async def record_terminal_cancel_receipt(
        self,
        *,
        team_id: str,
        user_id: str,
        cancel_request_id: str,
        workspace_id: str,
        turn_id: str,
        expected_execution_generation: int,
        outcome: Literal["cancelled", "terminal", "generation_conflict"],
    ) -> Literal["cancelled", "terminal", "generation_conflict"]:
        """Record a replayable HTTP Stop ACK without rewriting Terminal v1.

        Terminal v1 owns the Turn terminal transition through its finalizer.
        This second, idempotent ledger records only the deterministic Web
        request outcome so a lost POST response can be reconciled by GET.
        """

        if outcome not in {"cancelled", "terminal", "generation_conflict"}:
            raise ValueError("cancel receipt outcome is invalid")
        now = _now()
        expires_at = (datetime.now(tz=UTC) + timedelta(days=30)).isoformat()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT turn_id,workspace_id,expected_execution_generation,outcome "
                    "FROM turn_cancel_receipts WHERE team_id=? AND user_id=? "
                    "AND cancel_request_id=?",
                    (team_id, user_id, cancel_request_id),
                ) as receipt_cursor:
                    existing = await receipt_cursor.fetchone()
                if existing is not None:
                    if (
                        str(existing["turn_id"]) != turn_id
                        or str(existing["workspace_id"]) != workspace_id
                        or int(existing["expected_execution_generation"]) != expected_execution_generation
                        or str(existing["outcome"]) != outcome
                    ):
                        raise ValueError("cancel_request_conflict")
                    await db.execute("ROLLBACK")
                    return outcome

                async with db.execute(
                    "SELECT t.status,t.current_generation,t.terminal_protocol_version,"
                    "s.workspace_id,s.user_id FROM turns t JOIN sessions s ON s.id=t.session_id "
                    "WHERE t.id=?",
                    (turn_id,),
                ) as turn_cursor:
                    row = await turn_cursor.fetchone()
                if row is None:
                    raise ValueError("cancel_turn_scope_conflict")
                if str(row["workspace_id"]) != workspace_id or str(row["user_id"]) != user_id or int(row["terminal_protocol_version"] or 0) != 1:
                    raise ValueError("cancel_turn_scope_conflict")
                generation = int(row["current_generation"] or 1)
                turn_status = str(row["status"])
                if outcome == "generation_conflict":
                    valid = generation != expected_execution_generation
                elif outcome == "cancelled":
                    valid = (
                        generation
                        in {
                            expected_execution_generation,
                            expected_execution_generation + 1,
                        }
                        and turn_status == "cancelled"
                    )
                else:
                    valid = generation == expected_execution_generation and turn_status != "running"
                if not valid:
                    raise ValueError("cancel_receipt_state_conflict")
                await db.execute(
                    "INSERT INTO turn_cancel_receipts("
                    "team_id,user_id,cancel_request_id,workspace_id,turn_id,"
                    "expected_execution_generation,outcome,turn_erased,created_at,expires_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        team_id,
                        user_id,
                        cancel_request_id,
                        workspace_id,
                        turn_id,
                        expected_execution_generation,
                        outcome,
                        0,
                        now,
                        expires_at,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.execute("ROLLBACK")
                raise
        return outcome

    async def mark_failed(
        self,
        *,
        turn_id: str,
        worker: DocumentTurnOwner | None = None,
        error: str,
        error_kind: str,
        intent: str | None = None,
        final_answer: str | None = None,
        tokens_input: int = 0,
        tokens_output: int = 0,
        cost_usd: float = 0.0,
    ) -> bool:
        """Sprint π Phase A — terminal status FAILED.

        Writes `error` + `error_kind` (per A-ErrorKind), `ended_at`, optional
        intent / final_answer / tokens / cost. status='failed'. Returns whether
        this call actually transitioned a running Turn.
        """
        async with open_shared_ctx(self._db_path) as db:
            if not await _begin_worker_transition(db, turn_id=turn_id, worker=worker):
                return False
            now = _now()
            cursor = await db.execute(
                """
                UPDATE turns SET
                    status='failed',
                    ended_at=?,
                    intent=COALESCE(?, intent),
                    final_answer=COALESCE(?, final_answer),
                    tokens_input=generation_base_tokens_input+?,
                    tokens_output=generation_base_tokens_output+?,
                    cost_usd=generation_base_cost_usd+?,
                    error=?,
                    error_kind=?
                WHERE id=? AND status='running'
                """,
                (now, intent, final_answer, tokens_input, tokens_output, cost_usd, error, error_kind, turn_id),
            )
            transitioned = cursor.rowcount == 1
            if transitioned:
                await self._rollup_session_usage(db, turn_id)
                await db.execute(
                    "UPDATE sessions SET last_active_at=? WHERE id=(SELECT session_id FROM turns WHERE id=?) AND last_active_at < ?",
                    (now, turn_id, now),
                )
            await db.commit()
        return transitioned

    async def mark_clarification_pending(
        self,
        *,
        turn_id: str,
        worker: DocumentTurnOwner | None = None,
        intent: str | None,
        final_answer: str | None = None,
        tokens_input: int = 0,
        tokens_output: int = 0,
        cost_usd: float = 0.0,
        error_kind: str | None = None,
    ) -> bool:
        """Sprint π Phase A — terminal status CLARIFICATION_PENDING.

        Used when Lead emitted a clarification_ask sentinel; the turn is paused
        awaiting user reply. Writes ended_at so /sessions stops showing 'running'.
        """
        async with open_shared_ctx(self._db_path) as db:
            if not await _begin_worker_transition(db, turn_id=turn_id, worker=worker):
                return False
            now = _now()
            cursor = await db.execute(
                """
                UPDATE turns SET
                    status='clarification_pending',
                    ended_at=?,
                    intent=?,
                    final_answer=COALESCE(?, final_answer),
                    tokens_input=generation_base_tokens_input+?,
                    tokens_output=generation_base_tokens_output+?,
                    cost_usd=generation_base_cost_usd+?,
                    error_kind=COALESCE(?, error_kind)
                WHERE id=? AND status='running'
                """,
                (now, intent, final_answer, tokens_input, tokens_output, cost_usd, error_kind, turn_id),
            )
            transitioned = cursor.rowcount == 1
            if transitioned:
                await self._rollup_session_usage(db, turn_id)
                await db.execute(
                    "UPDATE sessions SET last_active_at=? WHERE id=(SELECT session_id FROM turns WHERE id=?) AND last_active_at < ?",
                    (now, turn_id, now),
                )
            await db.commit()
        return transitioned

    async def mark_aborted(
        self,
        *,
        turn_id: str,
        worker: DocumentTurnOwner | None = None,
        intent: str | None = None,
        final_answer: str | None = None,
        tokens_input: int = 0,
        tokens_output: int = 0,
        cost_usd: float = 0.0,
        error_kind: str | None = "aborted",
    ) -> bool:
        """Sprint π Phase A — terminal status ABORTED.

        Default error_kind='aborted' (user/system cancelled mid-turn).
        """
        async with open_shared_ctx(self._db_path) as db:
            if not await _begin_worker_transition(db, turn_id=turn_id, worker=worker):
                return False
            now = _now()
            cursor = await db.execute(
                """
                UPDATE turns SET
                    status='aborted',
                    ended_at=?,
                    intent=COALESCE(?, intent),
                    final_answer=COALESCE(?, final_answer),
                    tokens_input=generation_base_tokens_input+?,
                    tokens_output=generation_base_tokens_output+?,
                    cost_usd=generation_base_cost_usd+?,
                    error_kind=COALESCE(?, error_kind)
                WHERE id=? AND status='running'
                """,
                (now, intent, final_answer, tokens_input, tokens_output, cost_usd, error_kind, turn_id),
            )
            transitioned = cursor.rowcount == 1
            if transitioned:
                await self._rollup_session_usage(db, turn_id)
                await db.execute(
                    "UPDATE sessions SET last_active_at=? WHERE id=(SELECT session_id FROM turns WHERE id=?) AND last_active_at < ?",
                    (now, turn_id, now),
                )
            await db.commit()
        return transitioned

    # ─── Rollback compatibility (remove in D6b) ─────────────────────────
    async def complete_turn(
        self,
        *,
        turn_id: str,
        intent: str | None,
        final_answer: str,
        tokens_input: int,
        tokens_output: int,
        cost_usd: float,
    ) -> None:
        """Rollback-only compatibility wrapper; remove with legacy writer in D6b."""
        await self.mark_completed(
            turn_id=turn_id,
            intent=intent,
            final_answer=final_answer,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            cost_usd=cost_usd,
        )

    async def list_turns(self, *, session_id: str, limit: int | None = None) -> list[Turn]:
        """会话的 turn，按 seq 升序。``limit`` 给了就只取**最近** ``limit`` 轮（仍升序返回）——
        批 3 的 turn 级历史用它，IM 会话永不结束，不能每轮把整个会话拖进内存（review 修）。"""
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if limit is not None and limit > 0:
                async with db.execute(
                    "SELECT * FROM (SELECT * FROM turns WHERE session_id=? ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                    (session_id, int(limit)),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with db.execute(
                    "SELECT * FROM turns WHERE session_id=? ORDER BY seq",
                    (session_id,),
                ) as cur:
                    rows = await cur.fetchall()
        return [_row_to_turn(r) for r in rows]

    async def list_turn_spawns(
        self,
        turn_ids: list[str],
        *,
        incomplete_status: Mapping[str, str] | None = None,
    ) -> dict[str, list[TurnSpawnRecord]]:
        """给定轮次里 Lead 的 `subagent_spawn` 记录（r4；turn 级历史的 tool_calls 原料）。

        读 `turn_events` 里的两类事件：`lead.subagent_summary`（一次 spawn 一条，带 call_id / profile / rationale /
        status / child_tool_count / child_canvas_sections / outcome_message；r4 起还带 task 全文 / delivery / canvas_mode）为锚，
        老轮的 summary 没有 task 时由 `agent_spawned`（同 spawn_id，task 只有前 200 字）补。
        Lead 自己的 `agent_spawned` 没有 summary，自然排除。按事件 seq 排序；没记录的轮不在返回里。

        **r7**：``incomplete_status``（turn_id → cancelled / aborted / failed）里的轮，凡 `peer_dispatched` 的 call_id **没有对应 summary**
        （用户中途 stop，peer 被杀在 summary 之前——116 第五轮 8/8 的取消轮都这样；同轮里先回的 peer 照走 summary），
        退回 `peer_dispatched`（call_id / profile / rationale / spawn_id）+ `agent_spawned`（task）合成记录（``synthesized=True``，
        status 记**轮**状态，子工具数按该 peer 的 `tool_result` 条数——`json_extract` 不拉 payload，坏行按 `json_valid` 跳过）。
        与 `load_prior_peer_run` 退回 `agent_spawned` 同一思路。三类事件读一遍，记录按事件 seq 合并排序。
        """
        ids = [str(t) for t in dict.fromkeys(turn_ids) if t]
        if not ids:
            return {}
        fallback_status = {str(k): str(v) for k, v in (incomplete_status or {}).items() if k and v}
        rows: list[Any] = []
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            for i in range(0, len(ids), 200):
                chunk = ids[i : i + 200]
                ph = ",".join("?" for _ in chunk)
                async with db.execute(
                    f"SELECT turn_id, seq, event_type, payload_json FROM turn_events "  # noqa: S608 — placeholders only
                    f"WHERE turn_id IN ({ph}) AND event_type IN ('agent_spawned','lead.subagent_summary','peer_dispatched') ORDER BY turn_id, seq",
                    tuple(chunk),
                ) as cur:
                    rows.extend(await cur.fetchall())
            tasks: dict[tuple[str, str], str] = {}
            summaries: list[tuple[str, int, dict[str, Any]]] = []
            dispatched: dict[str, list[tuple[int, dict[str, Any]]]] = {}
            for r in rows:
                try:
                    payload = json.loads(str(r["payload_json"]))
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                tid = str(r["turn_id"])
                if r["event_type"] == "agent_spawned":
                    sid = str(payload.get("subagent_id") or "")
                    task = payload.get("task")
                    if sid and isinstance(task, str) and task.strip() and (tid, sid) not in tasks:
                        tasks[(tid, sid)] = task.strip()
                elif r["event_type"] == "peer_dispatched":
                    if tid in fallback_status and payload.get("call_id"):
                        dispatched.setdefault(tid, []).append((int(r["seq"]), payload))
                else:
                    summaries.append((tid, int(r["seq"]), payload))
            summarised_calls = {(tid, str(p.get("call_id") or "")) for tid, _seq, p in summaries}
            orphans: dict[str, list[tuple[int, dict[str, Any]]]] = {}
            for tid, items in dispatched.items():
                for seq, p in items:
                    if (tid, str(p.get("call_id") or "")) not in summarised_calls:
                        orphans.setdefault(tid, []).append((seq, p))
            tool_counts: dict[tuple[str, str], int] = {}
            for tid in orphans:
                async with db.execute(
                    "SELECT json_extract(payload_json, '$.subagent_id') AS sid, COUNT(*) AS n FROM turn_events "
                    "WHERE turn_id=? AND event_type='tool_result' AND json_valid(payload_json) GROUP BY sid",
                    (tid,),
                ) as cur:
                    for c in await cur.fetchall():
                        if c["sid"]:
                            tool_counts[(tid, str(c["sid"]))] = int(c["n"] or 0)
        # (turn_id, seq, record): merge summaries and synthetic records by event seq to preserve spawn order within a turn.
        ordered: list[tuple[str, int, TurnSpawnRecord]] = []
        for tid, items in orphans.items():
            for seq, p in items:
                sid = str(p.get("spawn_id") or p.get("subagent_id") or "")
                ordered.append(
                    (
                        tid,
                        seq,
                        TurnSpawnRecord(
                            call_id=str(p.get("call_id") or ""),
                            profile=str(p.get("profile") or p.get("peer_name") or ""),
                            task=tasks.get((tid, sid), ""),
                            rationale=str(p.get("rationale") or ""),
                            status=fallback_status[tid],
                            failure_reason=None,
                            child_tool_count=tool_counts.get((tid, sid), 0),
                            canvas_sections=(),
                            outcome="",
                            delivery="",
                            canvas_mode="",
                            synthesized=True,
                        ),
                    )
                )
        for tid, seq, payload in summaries:
            call_id = str(payload.get("call_id") or "")
            if not call_id:
                continue
            sid = str(payload.get("spawn_id") or payload.get("subagent_id") or "")
            sections: list[tuple[str, str]] = []
            for sec in payload.get("child_canvas_sections") or []:
                if isinstance(sec, dict):
                    sections.append((str(sec.get("kind") or ""), str(sec.get("title") or "")))
            _task_full = payload.get("task")
            ordered.append(
                (
                    tid,
                    seq,
                    TurnSpawnRecord(
                        call_id=call_id,
                        profile=str(payload.get("profile") or ""),
                        task=(_task_full.strip() if isinstance(_task_full, str) and _task_full.strip() else tasks.get((tid, sid), "")),
                        rationale=str(payload.get("rationale") or ""),
                        status=str(payload.get("status") or "unknown"),
                        failure_reason=(str(payload["failure_reason"]) if payload.get("failure_reason") else None),
                        child_tool_count=int(payload.get("child_tool_count") or 0),
                        canvas_sections=tuple(sections),
                        outcome=str(payload.get("outcome_message") or "")[:200],
                        delivery=str(payload.get("delivery") or ""),
                        canvas_mode=str(payload.get("canvas_mode") or ""),
                    ),
                )
            )
        out: dict[str, list[TurnSpawnRecord]] = {}
        for tid, _seq, rec in sorted(ordered, key=lambda x: (x[0], x[1])):
            out.setdefault(tid, []).append(rec)
        return out

    async def load_prior_peer_run(self, *, session_id: str, profile: str, before_turn_id: str, scan_turns: int = 10) -> Any:
        """同会话、同 profile 的**最近一次** spawn 跑了什么（子 agent「上次工作备忘」的原料；`peer_prior_run.py`）。

        只读 `turn_events`，两次查询（review 修：原来逐轮扫、每轮拉全部事件）：
        ① 最近 ``scan_turns`` 个已结束的轮（completed **和 failed**——失败轮正是错误重演的原料）里一次拿全部
        `lead.subagent_summary`，挑最新的轮里该 profile **最后一条** summary（同轮并行派了多个同 profile 时取落库最后的那个，确定性）；
        ② 只拉那一轮的 `agent_spawned` / `step_started` / `tool_result`，按 `parent_agent_id == spawn_id` 取嵌套工具。
        批量 bi_query 整体 ok=True 时局部失败藏在 `results[]` 里，逐条取出（`failed_queries`）；查询文本优先从结果的 `query_text` 取（参数预览会被截断）。
        没有就返回 None；任何异常按 None 处理（备忘缺席不挡派活）。
        """
        import re as _re

        from bowhead_core.peer_prior_run import PeerPriorRun, PeerToolCall

        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur_seq_row = await (await db.execute("SELECT seq FROM turns WHERE id=?", (before_turn_id,))).fetchone()
            cur_seq = int(cur_seq_row["seq"]) if cur_seq_row else None
            async with db.execute(
                "SELECT id, seq, status, user_text FROM turns WHERE session_id=? AND status IN ('completed','failed','cancelled') AND id != ? "
                + ("AND seq < ? " if cur_seq is not None else "")
                + "ORDER BY seq DESC LIMIT ?",
                ((session_id, before_turn_id, cur_seq, int(scan_turns)) if cur_seq is not None else (session_id, before_turn_id, int(scan_turns))),
            ) as cur:
                turns = await cur.fetchall()
            if not turns:
                return None
            turn_ids = [str(t["id"]) for t in turns]
            ph = ",".join("?" for _ in turn_ids)
            async with db.execute(
                f"SELECT turn_id, seq, event_type, payload_json FROM turn_events WHERE turn_id IN ({ph}) "  # noqa: S608 — placeholders only
                "AND event_type IN ('lead.subagent_summary','agent_spawned') ORDER BY seq",
                tuple(turn_ids),
            ) as scur:
                summary_rows = await scur.fetchall()
            summaries_by_turn: dict[str, dict[str, Any]] = {}
            spawned_by_turn: dict[str, dict[str, Any]] = {}
            for r in summary_rows:
                try:
                    payload = json.loads(str(r["payload_json"]))
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict) or str(payload.get("profile") or "") != profile:
                    continue
                if str(r["event_type"]) == "lead.subagent_summary":
                    summaries_by_turn[str(r["turn_id"])] = payload  # Multiple records in one turn: traverse by ascending seq; the last record wins.
                elif payload.get("subagent_id") and payload.get("status") in (None, "running", "cancelled", "aborted"):
                    spawned_by_turn[str(r["turn_id"])] = payload
            # 116 dogfood round 4: stopping a turn kills its peer, leaving no lead.subagent_summary, but its findings are crucial to the next turn;
            # without a summary, fall back to agent_spawned (persisted on delegation), using the turn's status.
            hit = next((t for t in turns if str(t["id"]) in summaries_by_turn or str(t["id"]) in spawned_by_turn), None)
            if hit is None:
                return None
            summary = summaries_by_turn.get(str(hit["id"]))
            if summary is None:
                sp = spawned_by_turn[str(hit["id"])]
                summary = {
                    "spawn_id": sp.get("subagent_id"),
                    "profile": profile,
                    "task": sp.get("task"),
                    "status": str(hit["status"] or "cancelled"),
                    "failure_reason": None,
                }
            spawn_id = str(summary.get("spawn_id") or summary.get("subagent_id") or "")
            async with db.execute(
                "SELECT seq, event_type, payload_json FROM turn_events WHERE turn_id=? "
                "AND event_type IN ('agent_spawned','step_started','step_done','tool_result') ORDER BY seq",
                (str(hit["id"]),),
            ) as ecur:
                rows = await ecur.fetchall()
        events: list[tuple[int, str, dict[str, Any]]] = []
        for r in rows:
            try:
                payload = json.loads(str(r["payload_json"]))
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                events.append((int(r["seq"]), str(r["event_type"]), payload))
        task = str(summary.get("task") or "")
        if not task:
            task = next(
                (
                    str(p.get("task") or "")
                    for _s, et, p in events
                    if et == "agent_spawned" and str(p.get("subagent_id") or "") == spawn_id and p.get("task")
                ),
                "",
            )
        started: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        results: dict[str, dict[str, Any]] = {}
        dones: dict[str, dict[str, Any]] = {}
        for _s, et, p in events:
            owner = str(p.get("parent_agent_id") or p.get("subagent_id") or "")
            if owner != spawn_id:
                continue
            cid = str(p.get("call_id") or "")
            if not cid:
                continue
            if et == "step_started" and cid not in started:
                started[cid] = p
                order.append(cid)
            elif et == "tool_result":
                results[cid] = p
            elif et == "step_done":
                dones[cid] = p

        def _as_text(v: Any) -> str:
            if v is None:
                return ""
            return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

        def _parse_json(text: str) -> Any:
            text = text.strip()
            if not text or text[0] not in "{[":
                return None
            try:
                return json.loads(text)
            except ValueError:
                return None

        tools: list[PeerToolCall] = []
        rejections: list[str] = []
        for cid in order:
            st = started[cid]
            res = results.get(cid) or {}
            tool = str(st.get("tool_name") or res.get("tool_name") or "")
            args_text = _as_text(st.get("arguments_preview"))
            inline = res.get("result_inline")
            preview_text = _as_text(inline if inline is not None else res.get("result_preview"))
            error = res.get("error")
            error_text = str(error) if error else None
            ok = res.get("ok") if "ok" in res else None
            if error_text is None and ok is False:
                # Review round 2 (P2): event-level error contains only Python exceptions; subagent_return rejections / bi_query failures are in the result dict
                # (error / error_message). Without extracting them, only a truncated JSON prefix remains, cutting off the instruction not to rephrase and retry.
                parsed_for_err = inline if isinstance(inline, dict) else _parse_json(preview_text)
                if isinstance(parsed_for_err, dict):
                    err_v = parsed_for_err.get("error") or parsed_for_err.get("error_message")
                    if isinstance(err_v, str) and err_v.strip():
                        error_text = err_v.strip()
                    # 116 dogfood round 4: the outer sandbox error is only "exit_code=1"; the real traceback is in result_json.stderr
                    # (the outer wrapper process exits 0). The last two nonempty lines (exception type + message) are far more useful than "exit_code=1".
                    inner_rj = parsed_for_err.get("result_json")
                    for src in (parsed_for_err, inner_rj if isinstance(inner_rj, dict) else None):
                        if src is None:
                            continue
                        stderr_v = src.get("stderr")
                        if isinstance(stderr_v, str) and stderr_v.strip():
                            tail = [ln.strip() for ln in stderr_v.strip().splitlines() if ln.strip()][-2:]
                            error_text = ((error_text + "：") if error_text and error_text.startswith("exit_code") else "") + " ".join(tail)
                            break
            arts: list[str] = []
            meta = res.get("result_meta") or {}
            if isinstance(meta, dict):
                for src in meta.get("sub_sources") or []:
                    if isinstance(src, dict) and src.get("artifact_id"):
                        arts.append(str(src["artifact_id"]))
                for out in meta.get("output_artifacts") or []:
                    if isinstance(out, dict) and out.get("artifact_id"):
                        arts.append(str(out["artifact_id"]))
                if meta.get("artifact_id"):
                    arts.append(str(meta["artifact_id"]))
            if not arts:
                arts = list(dict.fromkeys(_re.findall(r"art_[0-9a-f]{8,}", preview_text)))[:6]
            rows_m = _re.search(r"total_rows[\"\']?:\s*(\d+)", preview_text)
            rows_n = int(rows_m.group(1)) if rows_m else None
            queries: list[str] = []
            q_arts: list[tuple[str, str]] = []
            failed_q: list[tuple[str, str]] = []
            if tool == "bi_query":
                parsed_res = _parse_json(preview_text) if isinstance(inline, (dict, list)) or preview_text.startswith("{") else None
                sub_results = parsed_res.get("results") if isinstance(parsed_res, dict) else None
                if isinstance(sub_results, list):
                    for sub in sub_results:
                        if not isinstance(sub, dict):
                            continue
                        qt = str(sub.get("query_text") or "").strip()
                        if qt and qt not in queries:
                            queries.append(qt)
                        if sub.get("ok") is False or sub.get("error_message") or sub.get("error_kind"):
                            failed_q.append((qt, str(sub.get("error_message") or sub.get("error_kind") or "failed")))
                if not queries:
                    a = _parse_json(args_text)
                    qs = a.get("queries") if isinstance(a, dict) else None
                    if isinstance(qs, list):
                        queries = [str(q) for q in qs if isinstance(q, str) and q.strip()]
                    elif isinstance(a, dict) and a.get("query"):
                        queries = [str(a["query"])]
                if isinstance(meta, dict):
                    for src in meta.get("sub_sources") or []:
                        if isinstance(src, dict) and src.get("query") and src.get("artifact_id"):
                            q_arts.append((str(src["query"]), str(src["artifact_id"])))
                # Review round 2 (P2): result_inline has a 4 KB cap; multi-query batches often exceed it and cannot be parsed. step_done.sub_steps
                # (label=query text, state, error_kind) has no size cap and serves as a fallback.
                sub_steps = (dones.get(cid) or {}).get("sub_steps")
                if isinstance(sub_steps, list) and sub_steps:
                    labels = [str(s.get("label") or "").strip() for s in sub_steps if isinstance(s, dict)]
                    if not queries or len(labels) > len(queries):
                        queries = [lb for lb in labels if lb]
                    if not failed_q:
                        for s in sub_steps:
                            if isinstance(s, dict) and s.get("state") == "failure" and s.get("label"):
                                failed_q.append((str(s["label"]).strip(), str(s.get("error_kind") or "failed")))
                # Review round 2 (P2): flat N=1 bi_query results (the most common shape) lack results[] / sub_sources; the artifact is at meta.artifact_id.
                if not q_arts and len(queries) == 1 and isinstance(meta, dict) and meta.get("artifact_id") and ok is not False:
                    q_arts.append((queries[0], str(meta["artifact_id"])))
            if tool == "subagent_return" and ok is False:
                rejections.append(error_text or preview_text)
            tools.append(
                PeerToolCall(
                    tool=tool,
                    call_id=cid,
                    args_preview=args_text,
                    ok=ok if isinstance(ok, bool) else None,
                    error=error_text,
                    result_preview=preview_text[:600],
                    artifact_ids=tuple(dict.fromkeys(arts)),
                    rows=rows_n,
                    queries=tuple(queries),
                    query_artifacts=tuple(q_arts),
                    failed_queries=tuple(failed_q),
                )
            )
        return PeerPriorRun(
            turn_id=str(hit["id"]),
            turn_seq=int(hit["seq"]),
            user_text=str(hit["user_text"] or ""),
            profile=profile,
            task=task,
            status=str(summary.get("status") or "unknown"),
            failure_reason=(str(summary["failure_reason"]) if summary.get("failure_reason") else None),
            tools=tuple(tools),
            return_rejections=tuple(rejections),
            is_previous_turn=(cur_seq is not None and int(hit["seq"]) == cur_seq - 1),
        )

    async def list_recent_turn_ids(
        self,
        *,
        workspace_ids: list[str],
        user_id: str | None,
        limit: int,
    ) -> list[str]:
        """List recent canonical Turns within an already-authorized scope."""

        scoped_workspace_ids = list(dict.fromkeys(item for item in workspace_ids if item))
        if not scoped_workspace_ids or limit <= 0:
            return []
        placeholders = ",".join("?" for _ in scoped_workspace_ids)
        where = [f"s.workspace_id IN ({placeholders})", "s.origin='normal'"]
        params: list[Any] = [*scoped_workspace_ids]
        if user_id is not None:
            where.append("s.user_id=?")
            params.append(user_id)
        params.append(limit)
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                "SELECT t.id FROM turns t "
                "JOIN sessions s ON s.id=t.session_id "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY t.started_at DESC,t.id DESC LIMIT ?",
                params,
            ) as cur,
        ):
            rows = await cur.fetchall()
        return [str(row[0]) for row in rows]

    async def get_turn(self, turn_id: str) -> Turn | None:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM turns WHERE id=?",
                (turn_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_turn(row) if row else None

    async def is_turn_resume_ready(self, turn_id: str) -> bool:
        """Return whether a clarification answer can safely consume the card.

        The Turn must already be durably paused and its previous generation's
        single-row dispatch must be settled. This closes the visible-card gap
        between ``clarification_required`` and the worker monitor tail.
        """
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            async with db.execute(
                "SELECT status FROM turns WHERE id=?",
                (turn_id,),
            ) as turn_cursor:
                turn = await turn_cursor.fetchone()
            if turn is None or str(turn["status"]) != "clarification_pending":
                return False
            async with db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_turn_spawn_dispatch'",
            ) as table_cursor:
                if await table_cursor.fetchone() is None:
                    return False
            from bowhead_core.document_clarification_lifecycle import document_card_id_in_tx, require_document_pause_in_tx

            document_card_id = await document_card_id_in_tx(db, turn_id=turn_id)
            if document_card_id:
                try:
                    await require_document_pause_in_tx(db, card_id=document_card_id)
                except ValueError:
                    return False
            else:
                from bowhead_core.ordinary_lead_continuation import ordinary_card_id_in_tx, require_ordinary_pause_in_tx

                ordinary_card_id = await ordinary_card_id_in_tx(db, turn_id=turn_id)
                if ordinary_card_id:
                    try:
                        await require_ordinary_pause_in_tx(db, card_id=ordinary_card_id)
                    except ValueError:
                        return False
            if await _requires_reference_execution_in_tx(db, turn_id):
                from bowhead_core.chat_reference_continuation import pause_ready

                if not await pause_ready(db, turn_id=turn_id):
                    return False
            async with db.execute(
                "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
                (turn_id,),
            ) as dispatch_cursor:
                dispatch = await dispatch_cursor.fetchone()
        return dispatch is not None and str(dispatch["state"]) in (
            "terminal",
            "failed",
        )

    async def advance_turn_execution_generation(
        self,
        *,
        turn_id: str,
        lease_seconds: int = 30,
        reference_clarification_id: str | None = None,
        clarification_id: str | None = None,
        document_clarification_id: str | None = None,
    ) -> int | None:
        """Fence a same-Turn resume and publish its pre-worker lease atomically.

        The generation transition and ``claimed`` resume dispatch share one
        writer transaction.  Startup/maintenance therefore cannot mistake a
        healthy resume of an old clarification for an untracked stale Turn.
        An owner that disappears before the worker claim is recoverable back to
        ``clarification_pending`` after this lease expires.
        """
        now = _now()
        lease_expires_at = (datetime.now(tz=UTC) + timedelta(seconds=lease_seconds)).isoformat()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                try:
                    parent = await _read_legacy_continuation_parent_in_tx(
                        db,
                        turn_id=turn_id,
                        allow_idle_session=True,
                    )
                except SessionProtocolManagedError:
                    await db.execute("ROLLBACK")
                    return None
                if parent is None:
                    await db.execute("ROLLBACK")
                    return None
                session, parent_turn = parent
                from bowhead_core.document_clarification_lifecycle import document_card_id_in_tx, prepare_document_resume_in_tx

                document_card_id = await document_card_id_in_tx(db, turn_id=turn_id)
                if document_card_id is not None:
                    if document_card_id != document_clarification_id or reference_clarification_id or clarification_id:
                        await db.rollback()
                        return None
                    try:
                        await prepare_document_resume_in_tx(
                            db, card_id=document_card_id, next_generation=int(parent_turn["execution_generation"]) + 1
                        )
                    except ValueError:
                        await db.rollback()
                        return None
                elif document_clarification_id is not None:
                    await db.rollback()
                    return None
                if document_card_id is None:
                    from bowhead_core.ordinary_lead_continuation import ordinary_card_id_in_tx, require_ordinary_pause_in_tx

                    ordinary_card_id = await ordinary_card_id_in_tx(db, turn_id=turn_id)
                    if ordinary_card_id:
                        if ordinary_card_id != clarification_id or reference_clarification_id:
                            await db.rollback()
                            return None
                        try:
                            await require_ordinary_pause_in_tx(db, card_id=ordinary_card_id, answered=True)
                        except ValueError:
                            await db.rollback()
                            return None
                from bowhead_core.clarification_rounds import resume_ready as question_resume_ready

                if document_card_id is None and not await question_resume_ready(
                    db, turn=parent_turn, session=session, clarification_id=clarification_id or reference_clarification_id
                ):
                    await db.rollback()
                    return None
                reference_resume = await _requires_reference_execution_in_tx(db, turn_id)
                if reference_resume:
                    from bowhead_core.chat_reference_continuation import pause_ready

                    if not reference_clarification_id or not await pause_ready(db, turn_id=turn_id, clarification_id=reference_clarification_id):
                        await db.rollback()
                        return None
                elif reference_clarification_id is not None:
                    await db.rollback()
                    return None
                if str(parent_turn["status"]) != "clarification_pending":
                    await db.execute("ROLLBACK")
                    return None
                async with db.execute(
                    "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
                    (turn_id,),
                ) as dispatch_cursor:
                    prior_dispatch = await dispatch_cursor.fetchone()
                if prior_dispatch is not None and str(prior_dispatch["state"]) not in (
                    "terminal",
                    "failed",
                ):
                    # The previous generation still owns the single-row dispatch
                    # lease.  Never steal it during the worker's settlement tail;
                    # the caller can retry after monitor finalization.
                    await db.execute("ROLLBACK")
                    return None
                cursor = await db.execute(
                    "UPDATE turns SET "
                    "generation_base_tokens_input=tokens_input,"
                    "generation_base_tokens_output=tokens_output,"
                    "generation_base_cost_usd=cost_usd,"
                    "execution_generation=execution_generation+1,"
                    "status='running',ended_at=NULL WHERE id=? "
                    "AND status='clarification_pending'",
                    (turn_id,),
                )
                if cursor.rowcount != 1:
                    await db.execute("ROLLBACK")
                    return None
                activated = await db.execute(
                    "UPDATE sessions SET active_turn_id=? WHERE id=? AND (active_turn_id IS NULL OR active_turn_id=?)",
                    (turn_id, str(session["id"]), turn_id),
                )
                if activated.rowcount != 1:
                    await db.execute("ROLLBACK")
                    return None
                async with db.execute(
                    "SELECT execution_generation,session_id FROM turns WHERE id=?",
                    (turn_id,),
                ) as generation_cursor:
                    row = await generation_cursor.fetchone()
                if row is None:
                    raise RuntimeError("resume generation row disappeared")
                generation = int(row["execution_generation"])
                session_id = str(row["session_id"])
                if reference_resume:
                    from bowhead_core.chat_reference_continuation import reserve_resume

                    await reserve_resume(db, turn_id=turn_id, clarification_id=reference_clarification_id, generation=generation)
                await db.execute(
                    "INSERT INTO chat_turn_spawn_dispatch("
                    "turn_id,session_id,source_submission_id,parent_turn_id,"
                    "source_submission_ids_json,dispatch_kind,state,lease_kind,"
                    "lease_execution_generation,owner_id,cancel_requested_at,"
                    "lease_expires_at,created_at,updated_at"
                    ") VALUES (?,?,NULL,NULL,'[]','start','claimed','resume',?,"
                    "NULL,NULL,?,?,?) ON CONFLICT(turn_id) DO UPDATE SET "
                    "session_id=excluded.session_id,state='claimed',"
                    "lease_kind='resume',lease_execution_generation=excluded.lease_execution_generation,"
                    "owner_id=NULL,cancel_requested_at=NULL,"
                    "lease_expires_at=excluded.lease_expires_at,updated_at=excluded.updated_at",
                    (
                        turn_id,
                        session_id,
                        generation,
                        lease_expires_at,
                        now,
                        now,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.execute("ROLLBACK")
                raise
        return generation

    async def rollback_turn_resume_generation(
        self,
        *,
        turn_id: str,
        expected_execution_generation: int,
        owner_id: str | None,
    ) -> bool:
        """Return a pre-worker resume failure to its durable retry state."""
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                try:
                    parent = await _read_legacy_continuation_parent_in_tx(
                        db,
                        turn_id=turn_id,
                    )
                except (SessionProtocolManagedError, TerminalProtocolError):
                    await db.execute("ROLLBACK")
                    return False
                if parent is None:
                    await db.execute("ROLLBACK")
                    return False
                _parent_session, parent_turn = parent
                from bowhead_core.document_clarification_lifecycle import document_card_id_in_tx, release_document_resume_in_tx

                document_card_id = await document_card_id_in_tx(db, turn_id=turn_id)
                if document_card_id is not None:
                    try:
                        await release_document_resume_in_tx(db, card_id=document_card_id, generation=expected_execution_generation)
                    except ValueError:
                        await db.rollback()
                        return False
                else:
                    from bowhead_core.ordinary_lead_continuation import ordinary_card_id_in_tx, read_ordinary_card_in_tx

                    ordinary_card_id = await ordinary_card_id_in_tx(db, turn_id=turn_id)
                    if ordinary_card_id:
                        try:
                            retained = await read_ordinary_card_in_tx(db, card_id=ordinary_card_id)
                            if retained.consumed_generation is not None:
                                await db.rollback()
                                return False
                        except ValueError:
                            await db.rollback()
                            return False
                if await _requires_reference_execution_in_tx(db, turn_id):
                    from bowhead_core.chat_reference_continuation import release_resume

                    try:
                        await release_resume(db, turn_id=turn_id, generation=expected_execution_generation)
                    except ValueError:
                        await db.rollback()
                        return False
                if owner_id is None:
                    owner_fence = "state='claimed' AND owner_id IS NULL"
                    params: tuple[Any, ...] = ()
                else:
                    owner_fence = "state='spawned' AND owner_id=?"
                    params = (owner_id,)
                dispatch = await db.execute(
                    "UPDATE chat_turn_spawn_dispatch SET state='terminal',"
                    "owner_id=NULL,lease_expires_at=?,updated_at=? WHERE turn_id=? "
                    "AND lease_kind='resume' AND lease_execution_generation=? AND " + owner_fence,
                    (
                        now,
                        now,
                        turn_id,
                        expected_execution_generation,
                        *params,
                    ),
                )
                if dispatch.rowcount != 1:
                    await db.execute("ROLLBACK")
                    return False
                turn = await db.execute(
                    "UPDATE turns SET status='clarification_pending',ended_at=? WHERE id=? AND status='running' AND execution_generation=?",
                    (now, turn_id, expected_execution_generation),
                )
                if turn.rowcount != 1:
                    await db.execute("ROLLBACK")
                    return False
                async with db.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM turn_events "
                    "WHERE turn_id=? AND event_type='turn.resumed' "
                    "AND json_valid(payload_json) "
                    "AND CAST(json_extract(payload_json,'$.execution_generation') AS INTEGER)=?",
                    (turn_id, expected_execution_generation),
                ) as resume_cursor:
                    resume_row = await resume_cursor.fetchone()
                resume_floor = int(resume_row[0]) if resume_row else 0
                if resume_floor:
                    async with db.execute(
                        "SELECT 1 FROM turn_events WHERE turn_id=? AND seq>? AND ("
                        "event_type='done' OR (event_type='error' "
                        "AND json_valid(payload_json) AND "
                        "COALESCE(CAST(json_extract(payload_json,'$.parent_agent_id') AS TEXT),'')='')) LIMIT 1",
                        (turn_id, resume_floor),
                    ) as terminal_cursor:
                        current_terminal = await terminal_cursor.fetchone()
                    if current_terminal is None:
                        row_seq, event_seq = await _next_legacy_event_cursors_in_tx(
                            db,
                            turn_id=turn_id,
                        )
                        done_payload: dict[str, Any] = {
                            "turn_id": turn_id,
                            "status": "clarification_pending",
                            "schema_version": "0.9.0",
                            "execution_generation": expected_execution_generation,
                            "tokens_input": int(parent_turn["tokens_input"] or 0),
                            "tokens_output": int(parent_turn["tokens_output"] or 0),
                            "cost_usd": float(parent_turn["cost_usd"] or 0.0),
                            "seq": event_seq,
                            "evt_seq": event_seq,
                        }
                        if parent_turn["intent"] is not None:
                            done_payload["intent"] = str(parent_turn["intent"])
                        if parent_turn["error_kind"] is not None:
                            done_payload["error_kind"] = str(parent_turn["error_kind"])
                        await db.execute(
                            "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,?,'done',?,?)",
                            (
                                f"evt_{uuid4().hex[:12]}",
                                turn_id,
                                row_seq,
                                json.dumps(
                                    done_payload,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                now,
                            ),
                        )
                cleared = await db.execute(
                    "UPDATE sessions SET active_turn_id=CASE "
                    "WHEN json_valid(COALESCE(steer_buffer_json,'[]')) "
                    "AND json_type(COALESCE(steer_buffer_json,'[]'))='array' "
                    "AND json_array_length(COALESCE(steer_buffer_json,'[]'))=0 "
                    "THEN NULL ELSE active_turn_id END WHERE id=("
                    "SELECT session_id FROM turns WHERE id=?) AND active_turn_id=?",
                    (turn_id, turn_id),
                )
                if cleared.rowcount != 1:
                    await db.execute("ROLLBACK")
                    return False
                await db.commit()
                return True
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def get_active_resume_dispatch_generation(
        self,
        *,
        turn_id: str,
    ) -> int | None:
        """Return a remote resume generation only with a live worker lease."""
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            try:
                try:
                    parent = await _read_legacy_continuation_parent_in_tx(
                        db,
                        turn_id=turn_id,
                    )
                except SessionProtocolManagedError:
                    return None
                if parent is None:
                    return None
                async with db.execute(
                    "SELECT t.execution_generation FROM turns t "
                    "JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id "
                    "WHERE t.id=? AND t.status='running' "
                    "AND d.lease_kind='resume' AND d.state='spawned' "
                    "AND d.lease_execution_generation=t.execution_generation "
                    "AND d.lease_expires_at>?",
                    (turn_id, _now()),
                ) as cursor:
                    row = await cursor.fetchone()
            finally:
                if db.in_transaction:
                    await db.rollback()
        return int(row[0]) if row is not None else None

    # ─── Turn attachments (record each turn's user-upload artifact IDs) ─────
    async def record_turn_attachments(
        self,
        *,
        turn_id: str,
        artifact_ids: list[str],
    ) -> None:
        if not artifact_ids:
            return
        async with open_shared_ctx(self._db_path) as db:
            await db.executemany(
                "INSERT OR IGNORE INTO turn_attachments(turn_id, artifact_id, seq) VALUES (?, ?, ?)",
                [(turn_id, aid, i) for i, aid in enumerate(artifact_ids)],
            )
            await db.commit()

    async def list_turn_attachments(self, *, turn_id: str) -> list[str]:
        """返 artifact_ids（按 seq 排）."""
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                "SELECT artifact_id FROM turn_attachments WHERE turn_id=? ORDER BY seq",
                (turn_id,),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return [str(r[0]) for r in rows]

    # ─── Turn citations (prod-bug-bash-039 #6 citation reform, 0.39.2) ──────
    async def record_turn_citations(
        self,
        *,
        turn_id: str,
        citations: list[tuple[int, str, str | None, str]],
        kb_provenance: dict[int, tuple[str | None, str | None]] | None = None,
    ) -> None:
        """Persist turn-scoped citations registry to turn_citations table.

        Called once at narrative finalize (after `_resolve_citations()` runs).
        Mirrors `record_turn_attachments` batch INSERT pattern.

        Args:
            turn_id: turn id
            citations: list of (seq, artifact_id, label, kind) tuples in
                       insertion order — typically derived from
                       `TurnCitationRegistry.all_citations()`.

        Idempotent via PK (turn_id, seq) + INSERT OR IGNORE — re-emit of the
        same registry produces no duplicate rows.
        """
        if not citations:
            return
        async with open_shared_ctx(self._db_path) as db:
            await db.executemany(
                "INSERT OR IGNORE INTO turn_citations(turn_id, seq, artifact_id, label, kind) VALUES (?, ?, ?, ?, ?)",
                [(turn_id, seq, aid, label, kind) for (seq, aid, label, kind) in citations],
            )
            if kb_provenance:
                await db.executemany(
                    "UPDATE turn_citations SET kb_generation_id=?,kb_source_check_state=? WHERE turn_id=? AND seq=? AND kind='kb_chunk'",
                    [(generation, state, turn_id, seq) for seq, (generation, state) in kb_provenance.items()],
                )
            await db.commit()

    async def record_turn_kb_source_checks(self, turn_id: str, checks: list[dict[str, Any]]) -> None:
        async with open_shared_ctx(self._db_path) as db:
            await db.executemany(
                "INSERT INTO turn_kb_source_checks(turn_id,file_id,generation_id,check_state,checked_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(turn_id,file_id,generation_id) DO UPDATE SET check_state=excluded.check_state,checked_at=excluded.checked_at",
                [(turn_id, item["file_id"], item.get("generation_id") or "", item["check_state"], item["checked_at"]) for item in checks],
            )
            await db.commit()

    async def list_kb_citation_provenance(self, turn_ids: list[str]) -> dict[str, dict[int, dict[str, str]]]:
        if not turn_ids:
            return {}
        result: dict[str, dict[int, dict[str, str]]] = {}
        async with open_shared_ctx(self._db_path) as db:
            # Bounded SQL parameters for long reports.
            for offset in range(0, len(turn_ids), 200):
                batch = turn_ids[offset : offset + 200]
                rows = await (
                    await db.execute(
                        f"SELECT turn_id,seq,kb_generation_id,kb_source_check_state FROM turn_citations WHERE turn_id IN ({','.join('?' for _ in batch)}) AND kb_generation_id IS NOT NULL",
                        batch,
                    )
                ).fetchall()
                for turn, seq, generation, state in rows:
                    result.setdefault(turn, {})[seq] = {"kb_generation_id": generation, "kb_source_check_state": state or "unverified"}
        return result

    async def list_turn_citations(
        self,
        *,
        turn_id: str,
    ) -> list[tuple[int, str, str | None, str]]:
        """Read citations registry for `turn_id` in seq order.

        Returns list of (seq, artifact_id, label, kind). Used by hydrate path
        for archived turns to repopulate FE `citations[]` state on session
        reload (so old `[cite:N]` chips resolve correctly without re-running
        the registry from event replay).
        """
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                "SELECT seq, artifact_id, label, kind FROM turn_citations WHERE turn_id=? ORDER BY seq",
                (turn_id,),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return [(int(r[0]), str(r[1]), (str(r[2]) if r[2] is not None else None), str(r[3])) for r in rows]

    async def list_turn_citations_for_turns(
        self,
        *,
        turn_ids: list[str],
    ) -> dict[str, list[tuple[int, str, str | None, str]]]:
        """Batch read: turn_id → citations in seq order（单条 IN 查询，分块 400）。

        source-accessibility review F7：canvas detail 的 sidecar 逐 turn 调
        ``list_turn_citations`` 是 N+1（每次开一个连接），而 detail 是画布最热的
        读端点（打开报告 / refreshCanvas / 每次 turn 收尾对账都打）。
        """
        ordered = list(dict.fromkeys(t for t in turn_ids if t))
        out: dict[str, list[tuple[int, str, str | None, str]]] = {t: [] for t in ordered}
        if not ordered:
            return out
        async with open_shared_ctx(self._db_path) as db:
            for start in range(0, len(ordered), 400):
                chunk = ordered[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                async with db.execute(
                    f"SELECT turn_id, seq, artifact_id, label, kind FROM turn_citations "  # noqa: S608 — placeholders only
                    f"WHERE turn_id IN ({placeholders}) ORDER BY turn_id, seq",
                    chunk,
                ) as cur:
                    async for r in cur:
                        out[str(r[0])].append(
                            (int(r[1]), str(r[2]), (str(r[3]) if r[3] is not None else None), str(r[4])),
                        )
        return out

    async def list_session_attachments(self, *, session_id: str) -> list[str]:
        """返 session 范围内所有 turn 上传的 artifact_ids（按 turn seq + 附件 seq 排）.
        用于 canvas export 找模板 / 未来跨轮引用.
        """
        q = "SELECT ta.artifact_id FROM turn_attachments ta JOIN turns t ON ta.turn_id = t.id WHERE t.session_id = ? ORDER BY t.seq, ta.seq"
        async with open_shared_ctx(self._db_path) as db, db.execute(q, (session_id,)) as cur:
            rows = await cur.fetchall()
        return [str(r[0]) for r in rows]

    # ─── T1 Session pinning ──────────────────────────────────────────────
    async def pin_session_attachments(
        self,
        *,
        session_id: str,
        artifact_ids: list[str],
    ) -> None:
        """把 artifacts pin 到 session（upsert + reactivate detached）. 幂等."""
        if not artifact_ids:
            return
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            for aid in artifact_ids:
                # INSERT OR REPLACE clears detached_at (reactivating a previously unpinned item).
                await db.execute(
                    "INSERT INTO session_attachments(session_id, artifact_id, pinned_at, detached_at) "
                    "VALUES (?, ?, ?, NULL) "
                    "ON CONFLICT(session_id, artifact_id) DO UPDATE SET detached_at=NULL",
                    (session_id, aid, now),
                )
            await db.commit()

    async def unpin_session_attachment(
        self,
        *,
        session_id: str,
        artifact_id: str,
    ) -> bool:
        """取消 pin（DB 里 `detached_at` 置当前时间作软删时间戳；对外术语用 unpin）."""
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "UPDATE session_attachments SET detached_at=? WHERE session_id=? AND artifact_id=? AND detached_at IS NULL",
                (_now(), session_id, artifact_id),
            )
            await db.commit()
            return cur.rowcount > 0

    # Backward-compatible alias (referenced by existing code; remove in a future version)
    detach_session_attachment = unpin_session_attachment

    async def list_pinned_attachments(self, *, session_id: str) -> list[str]:
        """返当前 pinned 的 artifact_ids（按 pinned_at 正序，旧的先出来）."""
        q = "SELECT artifact_id FROM session_attachments WHERE session_id=? AND detached_at IS NULL ORDER BY pinned_at"
        async with open_shared_ctx(self._db_path) as db, db.execute(q, (session_id,)) as cur:
            rows = await cur.fetchall()
        return [str(r[0]) for r in rows]

    # ─── Turn digest (for <conversation_context> in the next turn's Lead system prompt) ───
    async def set_turn_agent_type(self, *, turn_id: str, agent_type: str | None) -> None:
        """W11-6: 设 turn.agent_type. None / 'lead' = 隐式默认 Lead.
        若 turn 主要 spawn 给 analyst/modeler 则记非 lead."""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "UPDATE turns SET agent_type=? WHERE id=?",
                (agent_type, turn_id),
            )
            await db.commit()

    async def save_digest(self, *, turn_id: str, digest_json: str) -> None:
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "UPDATE turns SET digest_json=? WHERE id=?",
                (digest_json, turn_id),
            )
            await db.commit()

    async def update_turn_hot_state(self, *, turn_id: str, hot_state_json: str) -> None:
        """Sprint η — persist HOT layer state JSON to turns row.

        Called by ``state_lifecycle.DefaultStateLifecycle`` on turn end /
        error / HITL pause / cancellation. Single UPDATE, atomic.
        """
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "UPDATE turns SET hot_state_json=? WHERE id=?",
                (hot_state_json, turn_id),
            )
            await db.commit()

    # ─── chat-hydrate-overhaul: persist traces + summaries ────────────
    _SUMMARY_TRUNCATE = 1024

    async def persist_turn_trace(
        self,
        *,
        turn_id: str,
        steps: list[TraceStepRow],
    ) -> None:
        """Single batch INSERT OR REPLACE. fire-and-forget — caller wraps in
        try/except + log.warning + telemetry, 不冒泡到 turn_runner.run_turn.

        seq 用 caller 传的 (TraceStepRow.seq), 通常是 0..N-1 在 turn 内的最终顺序
        (含 nested children, 跟 spawn parent 平铺但带 subagent_id 标识嵌套关系).
        """
        if not steps:
            return
        rows = [
            (
                s.turn_id,
                s.seq,
                s.step_id,
                s.kind,
                s.title,
                s.tool_name,
                s.duration_ms,
                (s.summary or "")[: self._SUMMARY_TRUNCATE] if s.summary else None,
                s.subagent_id,
            )
            for s in steps
        ]
        async with open_shared_ctx(self._db_path) as db:
            await db.executemany(
                "INSERT OR REPLACE INTO turn_trace_steps "
                "(turn_id, seq, step_id, kind, title, tool_name, duration_ms, "
                " summary, subagent_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            await db.commit()

    async def persist_turn_summary(
        self,
        *,
        turn_id: str,
        total_ms: int,
        tools_count: int,
        llm_count: int,
        cost_text: str | None = None,
        cost_cents: int | None = None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        raw_json: str | None = None,
    ) -> None:
        """INSERT OR REPLACE turn_summaries. fire-and-forget."""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO turn_summaries "
                "(turn_id, total_ms, cost_text, cost_cents, tools_count, llm_count, "
                " tokens_input, tokens_output, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (turn_id, total_ms, cost_text, cost_cents, tools_count, llm_count, tokens_input, tokens_output, raw_json),
            )
            await db.commit()

    async def persist_turn_operation_usage(
        self,
        *,
        turn_id: str,
        skill_loads: int,
        tool_loads: int,
        tool_calls: int,
    ) -> None:
        """Keep bounded, content-free operation counts for team reporting."""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "INSERT INTO turn_operation_usage(turn_id,skill_loads,tool_loads,tool_calls) "
                "VALUES (?,?,?,?) ON CONFLICT(turn_id) DO UPDATE SET "
                "skill_loads=MAX(skill_loads,excluded.skill_loads), "
                "tool_loads=MAX(tool_loads,excluded.tool_loads), "
                "tool_calls=MAX(tool_calls,excluded.tool_calls)",
                (turn_id, skill_loads, tool_loads, tool_calls),
            )
            await db.commit()

    async def list_trace_steps_for_turns(
        self,
        turn_ids: list[str],
    ) -> dict[str, list[TraceStepRow]]:
        """Batch fetch trace steps for 多 turn (避 N+1).
        返回 dict[turn_id, list[TraceStepRow] sorted by seq]."""
        if not turn_ids:
            return {}
        placeholders = ",".join("?" * len(turn_ids))
        q = (
            f"SELECT turn_id, seq, step_id, kind, title, tool_name, "
            f"       duration_ms, summary, subagent_id "
            f"FROM turn_trace_steps WHERE turn_id IN ({placeholders}) "
            f"ORDER BY turn_id, seq"
        )
        out: dict[str, list[TraceStepRow]] = {tid: [] for tid in turn_ids}
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(q, turn_ids) as cur:
                async for row in cur:
                    tid = row["turn_id"]
                    # Round 5: subagent_id may be absent (old database without ALTER); fall back safely.
                    subagent_id: str | None = None
                    with contextlib.suppress(KeyError, IndexError):
                        subagent_id = row["subagent_id"]
                    out.setdefault(tid, []).append(
                        TraceStepRow(
                            turn_id=tid,
                            seq=int(row["seq"]),
                            step_id=row["step_id"],
                            kind=row["kind"],
                            title=row["title"],
                            tool_name=row["tool_name"],
                            duration_ms=(int(row["duration_ms"]) if row["duration_ms"] is not None else None),
                            summary=row["summary"],
                            subagent_id=subagent_id,
                        )
                    )
        return out

    async def list_legacy_failure_answer_turn_ids(self, turn_ids: list[str]) -> set[str]:
        """Turns whose last persisted ``final_answer`` is a system-written failure fallback.

        Chat turn feedback D3 (design §8 Q1): before that change, a failed turn also persisted a
        generic failure sentence as its answer, tagged with the public failure ``reason_code`` or
        ``silent_done_fallback``. Callers pass only failed turns and hide that answer, so the failure
        card stays the single failure statement for old turns too. Decided from persisted events,
        the same rule the browser applies to the event stream.
        """
        if not turn_ids:
            return set()
        from bowhead_core.published_diagnostics import PUBLIC_TURN_FAILURE_CODES

        suppressible = PUBLIC_TURN_FAILURE_CODES | {"silent_done_fallback"}
        placeholders = ",".join("?" * len(turn_ids))
        q = (
            f"SELECT turn_id, payload_json FROM turn_events WHERE turn_id IN ({placeholders}) "  # noqa: S608 — placeholders only
            "AND event_type='final_answer' ORDER BY turn_id, seq"
        )
        last_reason: dict[str, object] = {}
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(q, turn_ids) as cur:
                async for row in cur:
                    try:
                        payload = json.loads(str(row["payload_json"]))
                    except (TypeError, ValueError):
                        payload = None
                    last_reason[str(row["turn_id"])] = payload.get("reason_code") if isinstance(payload, dict) else None
        return {turn_id for turn_id, reason in last_reason.items() if isinstance(reason, str) and reason in suppressible}

    async def get_turn_summaries(
        self,
        turn_ids: list[str],
    ) -> dict[str, TurnSummaryRow]:
        """Batch fetch turn summaries. dict[turn_id, TurnSummaryRow], 缺则不在 dict."""
        if not turn_ids:
            return {}
        placeholders = ",".join("?" * len(turn_ids))
        q = (
            f"SELECT turn_id, total_ms, cost_text, cost_cents, tools_count, "
            f"       llm_count, tokens_input, tokens_output "
            f"FROM turn_summaries WHERE turn_id IN ({placeholders})"
        )
        out: dict[str, TurnSummaryRow] = {}
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(q, turn_ids) as cur:
                async for row in cur:
                    tid = row["turn_id"]
                    out[tid] = TurnSummaryRow(
                        turn_id=tid,
                        total_ms=int(row["total_ms"] or 0),
                        cost_text=row["cost_text"],
                        cost_cents=(int(row["cost_cents"]) if row["cost_cents"] is not None else None),
                        tools_count=int(row["tools_count"] or 0),
                        llm_count=int(row["llm_count"] or 0),
                        tokens_input=(int(row["tokens_input"]) if row["tokens_input"] is not None else None),
                        tokens_output=(int(row["tokens_output"]) if row["tokens_output"] is not None else None),
                    )
            async with db.execute("SELECT 1 FROM sqlite_master WHERE name='document_turn_dispatches'") as cursor:
                document_schema = await cursor.fetchone()
            document_turn_ids = []
            if document_schema:
                async with db.execute(
                    "SELECT DISTINCT j.origin_turn_id FROM document_turn_dispatches d JOIN document_jobs j ON j.id=d.job_id "
                    f"WHERE j.origin_turn_id IN ({placeholders})",
                    turn_ids,
                ) as cursor:
                    document_turn_ids = [row[0] for row in await cursor.fetchall()]
        from bowhead_core.document_chat_usage import document_usage_projection

        for turn_id in document_turn_ids:
            if turn_id not in out:
                continue
            usage = await document_usage_projection(self.db_path, turn_id=turn_id)
            if usage is not None:
                out[turn_id].tokens_input = usage["tokens_input"]
                out[turn_id].tokens_output = usage["tokens_output"]
                out[turn_id].usage_partial = usage["partial"]
        return out

    async def list_recent_digests(
        self,
        *,
        session_id: str,
        limit: int = 3,
        exclude_turn_id: str | None = None,
    ) -> list[dict]:
        """最近 `limit` 条已完成 turn 的 digest（按 seq 降序取后再 reverse 成从远到近）.
        仅返回 digest_json 非空的（turn 已完成且 digest 已生成）.
        """
        q = "SELECT digest_json FROM turns WHERE session_id=? AND digest_json IS NOT NULL AND digest_json != ''"
        params: list = [session_id]
        if exclude_turn_id:
            q += " AND id != ?"
            params.append(exclude_turn_id)
        q += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        async with open_shared_ctx(self._db_path) as db, db.execute(q, params) as cur:
            rows = await cur.fetchall()
        out: list[dict] = []
        for r in reversed(rows):  # Oldest to newest
            try:
                out.append(json.loads(r[0]))
            except (ValueError, TypeError):
                continue
        return out


def _submission_scope_matches(
    row: aiosqlite.Row,
    *,
    workspace_id: str,
    requested_session_id: str | None,
) -> bool:
    return str(row["workspace_id"]) == workspace_id and row["requested_session_id"] == requested_session_id


def _row_to_submission_receipt(row: aiosqlite.Row) -> ChatSubmissionReceipt:
    response = json.loads(str(row["response_json"]))
    if not isinstance(response, dict):
        raise ValueError("invalid chat submission receipt payload")
    return ChatSubmissionReceipt(
        team_id=str(row["team_id"]),
        user_id=str(row["user_id"]),
        client_submission_id=str(row["client_submission_id"]),
        workspace_id=str(row["workspace_id"]),
        requested_session_id=row["requested_session_id"],
        actual_session_id=row["actual_session_id"],
        turn_id=row["turn_id"],
        submission_kind=str(row["submission_kind"]),
        submission_contract_version=str(row["submission_contract_version"]),
        model_control_batch=str(row["model_control_batch"]),
        request_fingerprint=str(row["request_fingerprint"]),
        outcome=str(row["outcome"]),
        response=response,
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]),
    )


def _row_to_session(row: aiosqlite.Row) -> Session:
    # W12-3: old databases may lack skill_applied / _last_seen_at; read compatibly.
    skill_applied = False
    with contextlib.suppress(KeyError, IndexError):
        skill_applied = bool(row["skill_applied"])
    last_seen_at: str | None = None
    with contextlib.suppress(KeyError, IndexError):
        last_seen_at = row["_last_seen_at"]
    pinned_at: str | None = None
    with contextlib.suppress(KeyError, IndexError):
        pinned_at = row["_pinned_at"]
    archived_at: str | None = None
    with contextlib.suppress(KeyError, IndexError):
        archived_at = row["_archived_at"]
    active_turn_id: str | None = None
    with contextlib.suppress(KeyError, IndexError):
        active_turn_id = row["active_turn_id"]
    origin = "normal"
    with contextlib.suppress(KeyError, IndexError):
        origin = row["origin"] or "normal"
    # feedback-0713 F2: old databases may lack deleted_at before migration; read defensively.
    deleted_at: str | None = None
    with contextlib.suppress(KeyError, IndexError):
        deleted_at = row["deleted_at"]
    return Session(
        id=row["id"],
        workspace_id=row["workspace_id"],
        user_id=row["user_id"],
        title=row["title"],
        started_at=row["started_at"],
        last_active_at=row["last_active_at"],
        ended_at=row["ended_at"],
        status=row["status"],
        cost_usd=float(row["cost_usd"] or 0.0),
        tokens_total=int(row["tokens_total"] or 0),
        intents=json.loads(row["intents"] or "[]"),
        skill_applied=skill_applied,
        last_seen_at=last_seen_at,
        pinned_at=pinned_at,
        archived_at=archived_at,
        active_turn_id=active_turn_id,
        deleted_at=deleted_at,
        origin=origin,
    )


def _row_to_turn(row: aiosqlite.Row) -> Turn:
    # W11-6: agent_type may be absent (old/unmigrated database); fall back safely.
    agent_type: str | None = None
    with contextlib.suppress(KeyError, IndexError):
        agent_type = row["agent_type"]
    # Sprint π Phase 0 (0.10.17.1): old databases may lack error / error_kind; read defensively.
    err: str | None = None
    err_kind: str | None = None
    with contextlib.suppress(KeyError, IndexError):
        err = row["error"]
    with contextlib.suppress(KeyError, IndexError):
        err_kind = row["error_kind"]
    execution_generation = 1
    with contextlib.suppress(KeyError, IndexError):
        execution_generation = max(1, int(row["execution_generation"] or 1))
    terminal_protocol_version: int | None = None
    with contextlib.suppress(KeyError, IndexError, TypeError, ValueError):
        raw_protocol_version = row["terminal_protocol_version"]
        terminal_protocol_version = int(raw_protocol_version) if raw_protocol_version is not None else None
    tokens_input = 0
    tokens_output = 0
    with contextlib.suppress(KeyError, IndexError, TypeError, ValueError):
        tokens_input = max(0, int(row["tokens_input"] or 0))
    with contextlib.suppress(KeyError, IndexError, TypeError, ValueError):
        tokens_output = max(0, int(row["tokens_output"] or 0))
    # Sprint π Phase 0: tolerate unknown status values from forward-shipped 0.11.0
    # (rollback safety). If reading a status this binary doesn't know, fall back
    # to 'completed' so /sessions render doesn't crash.
    raw_status = row["status"]
    normalized_status = normalize_legacy_turn_status(raw_status)
    status_safe = normalized_status if normalized_status in (LEGACY_TURN_WORKER_SETTLED_STATUSES | frozenset({"running"})) else "completed"
    return Turn(
        id=row["id"],
        session_id=row["session_id"],
        seq=int(row["seq"]),
        user_text=row["user_text"],
        intent=row["intent"],
        status=status_safe,
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        final_answer=row["final_answer"],
        cost_usd=float(row["cost_usd"] or 0.0),
        execution_generation=execution_generation,
        terminal_protocol_version=terminal_protocol_version,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        agent_type=agent_type,
        error=err,
        error_kind=err_kind,
    )
