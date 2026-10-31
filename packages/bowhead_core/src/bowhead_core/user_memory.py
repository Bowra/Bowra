"""User Memory 最小实现（M1）.

存储：SQLite `memory_entries` 表（subject_type='user'）。
注入：Orchestrator 每次 turn 开始时把 overridable 记忆拼入 Lead system prompt。
API：`/api/v1/memory/user` GET/POST（见 gateway/routes/memory.py）。

M2 会扩展到 Dreaming / short-term / persistent 的完整分层（见 PRD §7）。
这里只做最小可用：key-value，不分层。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.datarc_selection import DatarcPersistenceReceipt, DatarcPreferenceScope
from bowhead_core.memory_privacy import (
    MemoryIdempotencyConflictError,
    ensure_memory_write_allowed,
    is_memory_read_hidden,
)

_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_entries (
    id              TEXT PRIMARY KEY,
    subject_type    TEXT NOT NULL,            -- user | workspace | system
    subject_id      TEXT NOT NULL,
    kind            TEXT NOT NULL,            -- preference | constraint | datarc_binding
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,            -- JSON
    locked          INTEGER NOT NULL DEFAULT 0,
    lock_reason     TEXT,
    source          TEXT NOT NULL DEFAULT 'user_explicit',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    deleted_at      TEXT,                     -- NULL=active, ISO ts=trashed (soft delete, V3 PRD-B)
    workspace_id    TEXT,                     -- memory-revive S2: Bowhead Space ws_*, NULL=全局
    version         INTEGER NOT NULL DEFAULT 1,
    memory_role     TEXT NOT NULL DEFAULT 'unclassified',
    value_schema    TEXT,
    memory_policy_version INTEGER NOT NULL DEFAULT 0,
    created_source_type TEXT,
    created_source_turn_id TEXT,
    created_source_candidate_id TEXT,
    created_source_workspace_id TEXT,
    last_modified_actor_type TEXT,
    last_modified_actor_id TEXT,
    last_modified_at TEXT,
    -- 记忆重做 批0（migration 0024）: per-条元数据。存量行一律 NULL = 未标注。
    memory_type     TEXT,                     -- procedural | semantic
    confidence      TEXT,                     -- high(显式) | low(推断)
    last_confirmed_at TEXT,
    superseded_by   TEXT,                     -- 跨 key 取代时指向新条 id
    previous_value  TEXT,                     -- 同 key 覆盖时留上一版（只留一版）
    previous_value_at TEXT
    -- ★ 身份键**不再**是表级 UNIQUE(subject_type, subject_id, kind, key)：作用域进了身份
    -- （空间 A 记 top_n=10、空间 B 记 top_n=5 不该互相顶掉）。表级 UNIQUE 装不下表达式，
    -- 所以由 _INDEX_SCHEMA 的 idx_memory_entries_identity_scoped 承载，见 migration 0024。
);

CREATE TABLE IF NOT EXISTS memory_reset_snapshots (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    snapshot_json   TEXT NOT NULL,            -- JSON dump of all entries before reset
    created_at      TEXT NOT NULL,
    schema_version  INTEGER NOT NULL DEFAULT 1,
    snapshot_type   TEXT NOT NULL DEFAULT 'reset',
    target_resource_type TEXT,
    target_resource_id TEXT,
    expires_at      TEXT,
    consumed_at     TEXT,
    counts_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memory_reset_snapshot_items (
    snapshot_id        TEXT NOT NULL,
    restore_group_id   TEXT NOT NULL,
    resource_type      TEXT NOT NULL,
    resource_id        TEXT NOT NULL,
    display_type       TEXT,
    resource_version   INTEGER NOT NULL,
    deleted_version    INTEGER NOT NULL,
    payload_json       TEXT NOT NULL,
    restore_state      TEXT NOT NULL DEFAULT 'pending',
    restore_error      TEXT,
    restored_at        TEXT,
    PRIMARY KEY(snapshot_id, resource_type, resource_id)
);

CREATE TABLE IF NOT EXISTS memory_gc_state (
    cron_name       TEXT PRIMARY KEY,         -- e.g. 'hard_delete_old_trash'
    last_run_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_migration_state (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_mutation_idempotency (
    user_id              TEXT NOT NULL,
    idempotency_key      TEXT NOT NULL,
    request_fingerprint  TEXT NOT NULL,
    result_json          TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    PRIMARY KEY(user_id, idempotency_key)
);
"""

# Indexes are created AFTER ALTER TABLE migrations so they can reference
# columns that may have been added to pre-existing tables (deleted_at).
_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_memory_subject_kind
    ON memory_entries(subject_type, subject_id, kind);

CREATE INDEX IF NOT EXISTS idx_memory_deleted_at
    ON memory_entries(subject_type, subject_id, deleted_at);

CREATE INDEX IF NOT EXISTS idx_memory_ws
    ON memory_entries(subject_type, subject_id, workspace_id);

CREATE INDEX IF NOT EXISTS idx_memory_reset_user_time
    ON memory_reset_snapshots(user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_reset_items_resource
    ON memory_reset_snapshot_items(resource_type, resource_id);

CREATE INDEX IF NOT EXISTS idx_memory_user_updated
    ON memory_entries(subject_type, subject_id, deleted_at, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_user_role
    ON memory_entries(subject_type, subject_id, memory_role, kind, deleted_at);

-- 记忆重做 批0（migration 0024）: 作用域身份键。取代了旧的表级
-- UNIQUE(subject_type, subject_id, kind, key)。COALESCE 让「全局」(NULL) 成为一个
-- 具体的作用域值而不是「谁都不撞」——否则同 key 的多条全局记忆会并存。
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_entries_identity_scoped
    ON memory_entries(subject_type, subject_id, kind, key, COALESCE(workspace_id, '*'));
"""

# Backward-compat: ProjectMemoryStore + external readers may import SCHEMA.
SCHEMA = _TABLE_SCHEMA + _INDEX_SCHEMA


class MemoryRole(StrEnum):
    PRODUCT_MEMORY = "product_memory"
    OPERATIONAL = "operational"
    UNCLASSIFIED = "unclassified"


CURRENT_MEMORY_POLICY_VERSION = 1
MEMORY_POLICY_CUTOVER_KEY = f"memory_policy_cutover_complete:v{CURRENT_MEMORY_POLICY_VERSION}"

#: Memory redesign §5: marker that a user's legacy notes have migrated into `memory_entries`.
#: ★ Must be per-user: migration is a one-time job, but before and after it completes,
#: `build_memory_pack` handles the notes section differently. A global switch would also stop reading notes for
#: unmigrated users, immediately removing their memories from the prompt.
LEGACY_NOTES_MIGRATED_KEY_PREFIX = "legacy_notes_migrated:v1:"


def legacy_notes_migrated_key(user_id: str) -> str:
    return f"{LEGACY_NOTES_MIGRATED_KEY_PREFIX}{user_id}"


@dataclass
class MemoryEntry:
    id: str
    subject_type: str
    subject_id: str
    kind: str
    key: str
    value: str  # JSON-serialized
    locked: bool
    lock_reason: str | None
    source: str
    created_at: str
    updated_at: str
    deleted_at: str | None = None
    # memory-revive S2: source/applicable Space (Bowhead Space ws_*; None=global fallback).
    workspace_id: str | None = None
    version: int = 1
    memory_role: str = MemoryRole.UNCLASSIFIED
    value_schema: str | None = None
    memory_policy_version: int = 0
    created_source_type: str | None = None
    created_source_turn_id: str | None = None
    created_source_candidate_id: str | None = None
    created_source_workspace_id: str | None = None
    last_modified_actor_type: str | None = None
    last_modified_actor_id: str | None = None
    last_modified_at: str | None = None
    # Memory redesign batch 0 (migration 0024): per-entry metadata. Batch 0 only reads columns/fields;
    # no gate may depend on them (collect values and observe real distributions before deciding whether to use them as criteria).
    memory_type: str | None = None
    confidence: str | None = None
    last_confirmed_at: str | None = None
    superseded_by: str | None = None
    previous_value: str | None = None
    previous_value_at: str | None = None

    def parsed_value(self) -> object:
        try:
            return json.loads(self.value)
        except json.JSONDecodeError:
            return self.value


@dataclass(frozen=True)
class MemoryWriteResult:
    """`UserMemoryStore.commit_memory_write` 的裁决结果（记忆重做 批1）。

    `action`：
    - `created` —— 新建了一条；
    - `updated` —— 同 `key` 同作用域覆盖，旧值留在 `previous_value`（只留一版）；
    - `confirmed` —— 规范化逐字判重命中既有条 ⇒ **只 bump `last_confirmed_at`，不新增行**
      （这条直接解掉「净利率被提议 5 次」：模型一直看得见已有记忆，缺的是后端认账）；
    - `blocked` —— `low` 想覆盖 `high`，被裁决表拦下，`blocked_by` 是被保护的那条。

    `near_duplicates` 是**提示**不是动作：后端绝不自动合并（误伤方向是「静默吃掉一条
    真新偏好」，比多留一条更伤），由调用方决定怎么点名。
    """

    action: str
    entry: MemoryEntry | None
    superseded: tuple[str, ...] = ()
    supersede_blocked: tuple[str, ...] = ()
    near_duplicates: tuple[str, ...] = ()
    blocked_by: MemoryEntry | None = None


@dataclass(frozen=True)
class MemoryResetV3Result:
    snapshot_id: str
    expires_at: str
    counts: dict[str, int]


@dataclass(frozen=True)
class MemoryRestoreV3Result:
    snapshot_id: str
    status: str
    restored_counts: dict[str, int]
    conflicts: list[dict[str, str]]
    unavailable_counts: dict[str, int]
    remaining_recoverable: int


class MemoryResetBlockedError(RuntimeError):
    """Reset cannot proceed while About you has an active owner lease."""

    def __init__(self, lock_expires_at: str) -> None:
        super().__init__("About you is being edited")
        self.lock_expires_at = lock_expires_at


class MemoryResetSnapshotNotFoundError(LookupError):
    """A reset snapshot is absent, expired, consumed, or belongs to another user."""


class MemoryAboutYouNotFoundError(LookupError):
    """The active About you singleton does not exist."""


class MemoryAboutYouVersionConflictError(RuntimeError):
    """An About you delete used a stale public version."""

    def __init__(self, current_version: int) -> None:
        super().__init__("About you version conflict")
        self.current_version = current_version


class MemoryScopeConflictError(ValueError):
    """A write attempted to move an existing Memory entry between scopes."""

    def __init__(
        self,
        *,
        entry_id: str,
        existing_workspace_id: str | None,
        requested_workspace_id: str | None,
    ) -> None:
        super().__init__("memory entry already exists in a different scope")
        self.entry_id = entry_id
        self.existing_workspace_id = existing_workspace_id
        self.requested_workspace_id = requested_workspace_id


class MemoryConfidenceConflictError(RuntimeError):
    """记忆重做 批1（tech-design §4.2 b）：`low` 想覆盖 `high`，被裁决表拦下。

    唯一被拒的那一格。后台推断（Dreaming）永远不能悄悄改掉用户亲口说的
    ——「误覆盖比多留一条更伤」（PRD §7）。抛给调用方，由调用方决定怎么落成候选。
    """

    def __init__(self, *, entry_id: str, existing_confidence: str | None) -> None:
        self.entry_id = entry_id
        self.existing_confidence = existing_confidence
        super().__init__(f"low-confidence write cannot overwrite {entry_id} (confidence={existing_confidence})")


class MemoryEntryLockedError(PermissionError):
    """A locked Memory entry cannot be overwritten."""

    def __init__(self, entry_id: str) -> None:
        super().__init__("memory entry is locked")
        self.entry_id = entry_id


class MemoryEntryNotFoundError(LookupError):
    """A user-owned product Memory entry does not exist."""


class MemoryEntryStateError(ValueError):
    """A Memory entry cannot be mutated from its current lifecycle state."""


class MemoryEntrySchemaError(ValueError):
    """A typed Memory patch does not match the registered edit schema."""


class MemoryVersionConflictError(RuntimeError):
    """A CAS mutation used a stale Memory version."""

    def __init__(self, current: MemoryEntry) -> None:
        super().__init__("memory version conflict")
        self.current = current


class UserMemoryStore:
    """异步 SQLite 封装。生产可换 PG，保持 API 一致."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    @property
    def db_path(self) -> str:
        return self._db_path

    async def initialize(self) -> None:
        """建表 + 确保目录存在 + 迁移 deleted_at 列 (V3 PRD-B).

        Order matters: tables → ALTER migrations → indexes. Indexes can
        reference deleted_at, which is added by ALTER on pre-existing tables.
        Reversing this order crashes startup on legacy DBs (P0 caught in smoke).
        """
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_TABLE_SCHEMA)
            await _migrate_deleted_at(db)
            await _migrate_workspace_id(db)
            await _migrate_governance_columns(db)
            await _migrate_memory_item_columns(db)
            await _migrate_reset_v3(db)
            await _migrate_reset_item_v3(db)
            # Must follow all ALTER migrations (the rebuilt table copies the complete post-0024 column list) and
            # precede _INDEX_SCHEMA (rebuilding drops the old table and its indexes, which _INDEX_SCHEMA restores).
            await _migrate_scoped_identity(db)
            await _mark_empty_policy_cutover(db)
            await db.executescript(_INDEX_SCHEMA)
            await db.commit()

    async def list_for_user(
        self,
        user_id: str,
        *,
        include_trashed: bool = False,
        workspace_id: str | None = None,
    ) -> list[MemoryEntry]:
        sql = "SELECT * FROM memory_entries WHERE subject_type='user' AND subject_id=?"
        params: list = [user_id]
        if not include_trashed:
            sql += " AND deleted_at IS NULL"
        # memory-revive S2: a nonempty workspace_id filters to the current Space (NULL=global fallback; red-line
        # invariant 4 recalls old/global memory in every Space). None/'' = no filter (/memory, export, etc. need all entries,
        # preserving behavior; turns without a Space also use all entries). Always use the request argument, never infer the filter key.
        if workspace_id:
            sql += " AND (workspace_id = ? OR workspace_id IS NULL)"
            params.append(workspace_id)
        sql += " ORDER BY kind, key"
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if await is_memory_read_hidden(db, user_id=user_id):
                return []
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        return [_row_to_entry(r) for r in rows]

    async def list_created_in_turn(self, user_id: str, *, turn_id: str) -> list[MemoryEntry]:
        """Visible memory entries this turn created, **including trashed ones** (the memory receipt).

        Covers the `remember` tool, zero-click capture and candidates approved from this turn
        (approval copies the candidate's turn into `created_source_turn_id`). Trashed rows stay
        so the receipt can derive `undone`; a purged row is gone and therefore omitted. The
        receipt state is read from persisted memory, so live polling and a refresh agree.
        """
        from bowhead_core.memory_kind_policy import resolve_memory_policy

        sql = (
            "SELECT * FROM memory_entries WHERE subject_type='user' AND subject_id=? "
            "AND created_source_turn_id=? AND memory_role=? ORDER BY created_at, id"
        )
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if await is_memory_read_hidden(db, user_id=user_id):
                return []
            async with db.execute(sql, (user_id, turn_id, MemoryRole.PRODUCT_MEMORY)) as cur:
                rows = await cur.fetchall()
        entries = [_row_to_entry(r) for r in rows]
        return [
            entry
            for entry in entries
            if resolve_memory_policy(
                memory_role=entry.memory_role,
                memory_policy_version=entry.memory_policy_version,
                kind=entry.kind,
                value_schema=entry.value_schema,
                value=entry.parsed_value(),
            ).user_visible
        ]

    async def list_product_for_user(
        self,
        user_id: str,
        *,
        include_trashed: bool = False,
        workspace_id: str | None = None,
    ) -> list[MemoryEntry]:
        """Return only current-policy rows with a registered product adapter."""

        from bowhead_core.memory_kind_policy import resolve_memory_policy

        sql = "SELECT * FROM memory_entries WHERE subject_type='user' AND subject_id=? AND memory_role=? AND memory_policy_version=?"
        params: list[object] = [
            user_id,
            MemoryRole.PRODUCT_MEMORY,
            CURRENT_MEMORY_POLICY_VERSION,
        ]
        if not include_trashed:
            sql += " AND deleted_at IS NULL"
        if workspace_id:
            sql += " AND (workspace_id=? OR workspace_id IS NULL)"
            params.append(workspace_id)
        sql += " ORDER BY updated_at DESC, id DESC"
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if await is_memory_read_hidden(db, user_id=user_id):
                return []
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()

        entries = [_row_to_entry(row) for row in rows]
        return [
            entry
            for entry in entries
            if resolve_memory_policy(
                memory_role=entry.memory_role,
                memory_policy_version=entry.memory_policy_version,
                kind=entry.kind,
                value_schema=entry.value_schema,
                value=entry.parsed_value(),
            ).user_visible
        ]

    async def is_legacy_notes_migrated(self, user_id: str) -> bool:
        """这个用户的存量 notes 是否已迁进 `memory_entries`（记忆重做 §5）。

        迁移**不清空 notes**（那是回滚位，§5.0/§7）：闸一关就得能重新读回原文。
        所以「还要不要读 notes 段」只能靠这个标记判，不能靠「notes 是不是空的」。
        """
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                "SELECT value FROM memory_migration_state WHERE key=?",
                (legacy_notes_migrated_key(user_id),),
            ) as cur,
        ):
            row = await cur.fetchone()
        return bool(row) and str(row[0]) == "complete"

    async def mark_legacy_notes_migrated(self, user_id: str) -> None:
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO memory_migration_state(key, value, updated_at)
                VALUES (?, 'complete', ?)
                ON CONFLICT(key) DO UPDATE SET value='complete', updated_at=excluded.updated_at
                """,
                (legacy_notes_migrated_key(user_id), now),
            )
            await db.commit()

    async def mark_policy_cutover_complete(self) -> None:
        """补写 `memory_policy_cutover_complete:v1`（记忆重做 §5.1）。

        ⚠ `_mark_empty_policy_cutover` 只在**零 user 行的全新库**写这个标记 ⇒
        **任何从旧库升上来的部署都没有它**，`v2_readiness()` 会一直返
        `memory_policy_cutover_incomplete`。补写它是 §5 作业的活，不是 revision 的活。
        """
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO memory_migration_state(key, value, updated_at)
                VALUES (?, 'complete', ?)
                ON CONFLICT(key) DO UPDATE SET value='complete', updated_at=excluded.updated_at
                """,
                (MEMORY_POLICY_CUTOVER_KEY, now),
            )
            await db.commit()

    async def v2_readiness(self) -> tuple[bool, str | None]:
        """Check the persisted policy cutover and fail-closed row inventory."""

        from bowhead_core.memory_kind_policy import resolve_memory_policy

        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT value FROM memory_migration_state WHERE key=?",
                (MEMORY_POLICY_CUTOVER_KEY,),
            ) as cur:
                marker = await cur.fetchone()
            if marker is None or marker["value"] != "complete":
                return False, "memory_policy_cutover_incomplete"

            async with db.execute(
                """
                SELECT *
                FROM memory_entries
                WHERE subject_type='user'
                  AND deleted_at IS NULL
                  AND (
                    memory_policy_version != ?
                    OR memory_role = ?
                  )
                LIMIT 1
                """,
                (
                    CURRENT_MEMORY_POLICY_VERSION,
                    MemoryRole.UNCLASSIFIED,
                ),
            ) as cur:
                legacy = await cur.fetchone()
            if legacy is not None:
                # ⚠ `deleted_at IS NULL` above is required: this query has no user filter
                # (`subject_type='user'` spans the instance), and `_require_memory_ready` turns the result
                # into a 503: one user's bad row can disable everyone's memory page.
                # Classification backfill handles only `deleted_at IS NULL` rows, so an unclassified legacy row
                # in trash permanently blocks this gate: the job cannot reach it; only the 30-day hard-delete cron can.
                # Caught in final review on 2026-09-04 (production happened to have 0 such rows: an untriggered hazard).
                return False, "memory_policy_backfill_incomplete"

            async with db.execute(
                """
                SELECT *
                FROM memory_entries
                WHERE subject_type='user' AND memory_role=?
                """,
                (MemoryRole.PRODUCT_MEMORY,),
            ) as cur:
                product_rows = await cur.fetchall()

        for row in product_rows:
            entry = _row_to_entry(row)
            policy = resolve_memory_policy(
                memory_role=entry.memory_role,
                memory_policy_version=entry.memory_policy_version,
                kind=entry.kind,
                value_schema=entry.value_schema,
                value=entry.parsed_value(),
            )
            if not policy.user_visible:
                return False, "memory_product_policy_incomplete"
        return True, None

    async def list_by_kind_pattern(
        self,
        kind: str,
        key_prefix: str = "",
    ) -> list[MemoryEntry]:
        """M158-18: 跨 user 拉某 kind 的全部 entries (cron 聚合用).
        key_prefix 非空时按前缀 filter. 始终排除 trashed."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if key_prefix:
                sql = (
                    "SELECT * FROM memory_entries WHERE subject_type='user' AND kind=? AND key LIKE ? AND deleted_at IS NULL ORDER BY subject_id, key"
                )
                params: tuple = (kind, f"{key_prefix}%")
            else:
                sql = "SELECT * FROM memory_entries WHERE subject_type='user' AND kind=? AND deleted_at IS NULL ORDER BY subject_id, key"
                params = (kind,)
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [_row_to_entry(r) for r in rows]

    async def upsert(
        self,
        *,
        user_id: str,
        kind: str,
        key: str,
        value: object,
        source: str = "user_explicit",
        locked: bool = False,
        lock_reason: str | None = None,
        workspace_id: str | None = None,
        scope_specified: bool = False,
        memory_role: MemoryRole | str | None = None,
        value_schema: str | None = None,
        memory_policy_version: int | None = None,
        #: Pass only when the caller actually knows. ★ Do not use `normalize_memory_type(None)` here
        #: to fall back to `semantic`: that would present an unclassified value as fact to the user
        #: (the detail view's presentation/business-semantics chip reads it directly). Leave NULL when unknown;
        #: the UI displays unclassified. §6.1's fail-safe covers a missing model value on the write path,
        #: not inventing one for existing rows.
        memory_type: str | None = None,
        connection: aiosqlite.Connection | None = None,
    ) -> MemoryEntry:
        from datetime import datetime
        from uuid import uuid4

        from bowhead_core.memory_readiness import memory_storage_ready
        from bowhead_core.memory_write_policy import confidence_for_source

        now = datetime.now(tz=UTC).isoformat(timespec="seconds")
        entry_id = f"mem_{uuid4().hex[:12]}"
        val_json = json.dumps(value, ensure_ascii=False)
        role_value = _validate_memory_role(memory_role)
        # Memory redesign batch 1 (tech-design §4.2 b): manual creation on `/memory` (source='user_explicit')
        # must also receive `high`; otherwise a user-written memory counts as `low` in the decision table,
        # and an approved background-inferred candidate can overwrite it: exactly the forbidden case this table guards.
        # With the gate off, always None ⇒ columns written match today's behavior.
        stamped_confidence = confidence_for_source(source) if memory_storage_ready() else None
        if memory_policy_version is not None and memory_policy_version < 0:
            raise ValueError("memory_policy_version must be non-negative")

        async with nullcontext(connection) if connection is not None else open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if connection is None:
                await db.execute("BEGIN IMMEDIATE")
            try:
                await ensure_memory_write_allowed(
                    db,
                    user_id=user_id,
                )
                async with db.execute(
                    """
                    SELECT *
                    FROM memory_entries
                    WHERE subject_type='user' AND subject_id=? AND kind=? AND key=?
                    """,
                    (user_id, kind, key),
                ) as cur:
                    existing = await cur.fetchone()

                if existing is None:
                    await db.execute(
                        """
                        INSERT INTO memory_entries
                            (id, subject_type, subject_id, kind, key, value,
                             locked, lock_reason, source, created_at, updated_at, workspace_id,
                             memory_role, value_schema, memory_policy_version,
                             created_source_type, created_source_workspace_id,
                             last_modified_actor_type, last_modified_at,
                             confidence, last_confirmed_at, memory_type)
                        VALUES (?, 'user', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            entry_id,
                            user_id,
                            kind,
                            key,
                            val_json,
                            1 if locked else 0,
                            lock_reason,
                            source,
                            now,
                            now,
                            workspace_id,
                            role_value or MemoryRole.UNCLASSIFIED,
                            value_schema,
                            memory_policy_version or 0,
                            source,
                            workspace_id,
                            source,
                            now,
                            stamped_confidence,
                            now if stamped_confidence else None,
                            memory_type,
                        ),
                    )
                else:
                    if bool(existing["locked"]):
                        raise MemoryEntryLockedError(existing["id"])
                    existing_workspace_id = existing["workspace_id"]
                    if (scope_specified or workspace_id is not None) and workspace_id != existing_workspace_id:
                        raise MemoryScopeConflictError(
                            entry_id=existing["id"],
                            existing_workspace_id=existing_workspace_id,
                            requested_workspace_id=workspace_id,
                        )
                    await db.execute(
                        """
                        UPDATE memory_entries
                        SET value=?, updated_at=?, deleted_at=NULL,
                            version=version+1,
                            memory_role=COALESCE(?, memory_role),
                            value_schema=COALESCE(?, value_schema),
                            memory_policy_version=COALESCE(?, memory_policy_version),
                            -- ★ 绝不降级：`upsert` 不过裁决表，若直接 COALESCE 就会把
                            -- 用户手改出来的 `high` 悄悄写回 `low`。真实调用点是
                            -- undo-reset 恢复（routes/memory.py 用行里的旧 source 回灌）：
                            -- 一条被用户手改过的候选来源条目，恢复后 confidence 掉回 low，
                            -- 从此后台推断可以直接盖掉用户亲手写的那版。
                            confidence=CASE
                                WHEN ? IS NULL THEN confidence
                                WHEN confidence='high' AND ?='low' THEN confidence
                                ELSE ?
                            END,
                            memory_type=COALESCE(?, memory_type),
                            last_confirmed_at=COALESCE(?, last_confirmed_at),
                            last_modified_actor_type=?,
                            last_modified_at=?
                        WHERE id=?
                        """,
                        (
                            val_json,
                            now,
                            role_value,
                            value_schema,
                            memory_policy_version,
                            stamped_confidence,
                            stamped_confidence,
                            stamped_confidence,
                            memory_type,
                            now if stamped_confidence else None,
                            source,
                            now,
                            existing["id"],
                        ),
                    )

                async with db.execute(
                    "SELECT * FROM memory_entries WHERE subject_type='user' AND subject_id=? AND kind=? AND key=?",
                    (user_id, kind, key),
                ) as cur:
                    row = await cur.fetchone()
                if connection is None:
                    await db.commit()
            except BaseException:
                if connection is None:
                    await db.rollback()
                raise
        assert row is not None
        return _row_to_entry(row)

    async def upsert_product_memory(
        self,
        *,
        user_id: str,
        kind: str,
        key: str,
        value: object,
        source: str = "user_explicit",
        workspace_id: str | None = None,
        scope_specified: bool = False,
        locked: bool = False,
        lock_reason: str | None = None,
        memory_type: str | None = None,
    ) -> MemoryEntry:
        return await self.upsert(
            user_id=user_id,
            kind=kind,
            key=key,
            value=value,
            source=source,
            locked=locked,
            lock_reason=lock_reason,
            workspace_id=workspace_id,
            scope_specified=scope_specified,
            memory_role=MemoryRole.PRODUCT_MEMORY,
            value_schema=_value_schema_for(value),
            memory_policy_version=CURRENT_MEMORY_POLICY_VERSION,
            memory_type=memory_type,
        )

    async def upsert_operational_memory(
        self,
        *,
        user_id: str,
        kind: str,
        key: str,
        value: object,
        source: str,
    ) -> MemoryEntry:
        return await self.upsert(
            user_id=user_id,
            kind=kind,
            key=key,
            value=value,
            source=source,
            memory_role=MemoryRole.OPERATIONAL,
            value_schema=_value_schema_for(value),
            memory_policy_version=CURRENT_MEMORY_POLICY_VERSION,
        )

    async def commit_memory_write(
        self,
        *,
        user_id: str,
        kind: str,
        key: str,
        value: object,
        source: str,
        memory_type: str | None = None,
        workspace_id: str | None = None,
        scope_specified: bool = False,
        supersedes: Sequence[str] = (),
        created_source_turn_id: str | None = None,
        created_source_candidate_id: str | None = None,
        #: Source type, defaulting to `source`. ★ They must be separable: §5 migration needs
        #: `source='user_directive'` (confidence derives from writer identity ⇒ high),
        #: but source type must be `legacy_notes_migration` (the detail view labels it migrated from legacy memory).
        #: Conflating them makes migrated entries appear to be directly stated by the user, with no source.
        created_source_type: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
    ) -> MemoryWriteResult:
        """记忆重做 批1 —— canonical 记忆写入的**唯一裁决点**（tech-design §4.2 b/c/d）。

        与 `upsert` 的区别不是「多写几列」，是这里有**判重与取代**：

        1. 规范化逐字判重命中既有条 ⇒ bump `last_confirmed_at`，**不新增行**；
        2. 同 `key` 同作用域 ⇒ 覆盖，旧值进 `previous_value`（只留一版，够回退一步）；
        3. 显式 `supersedes=[mem_id]` ⇒ 跨 `key` 取代，旧条进回收站并记 `superseded_by`
           （软取代，可恢复；没有硬删）；
        4. `low` 想覆盖 `high` ⇒ **拒绝**，返回 `action='blocked'`。

        ★ **没有 `confidence` 参数**：置信度由 `source`（写入身份）确定性推出，调用方
        无法传入。模型自评置信度是它自己的输出，拿它做「谁能覆盖谁」的判据等于让写入方
        给自己发通行证（tech-design §8b）。要改一条记忆的置信度，只能换一个写入身份。

        ⚠ 被拦下时本方法**返回** `action='blocked'`，而候选 approve 那条路
        （`personal_memory_governance`）**抛** `MemoryConfidenceConflictError` —— 因为它
        要让整个跨表事务回滚、候选保持 pending。两种形态，**同一张裁决表**
        （`memory_write_policy.can_overwrite`）。再加第三条写路时接那个函数，别再写一份判据。
        """

        from bowhead_core.memory_write_policy import (
            CONFIDENCE_HIGH,
            can_overwrite,
            comparable_text,
            confidence_for_source,
            is_verbatim_duplicate,
            near_duplicate_matcher,
            normalize_confidence,
            normalize_memory_type,
        )

        # ★ Operational rows do not enter this path. This is the product-memory decision point for deduplication, overwrites,
        # supersession and confidence. None applies to machine state such as `datarc_binding` (batch 1 established that
        # gates/supersession must not touch operational rows). Deduplication scans and identity lookup exclude them, but exclusion
        # alone is insufficient: identity lookup then misses the existing binding row and attempts INSERT,
        # immediately violating `idx_memory_entries_identity_scoped`,
        # replacing silent BI-binding rewrites with approve crashes. Route at entry to
        # `upsert` (today's operational-row path) to preserve gate-off semantics.
        if kind == "datarc_binding":
            entry = await self.upsert(
                user_id=user_id,
                kind=kind,
                key=key,
                value=value,
                source=source,
                workspace_id=workspace_id,
                memory_role=MemoryRole.OPERATIONAL,
            )
            return MemoryWriteResult(action="updated", entry=entry)

        now = _now()
        confidence = confidence_for_source(source)
        type_value = normalize_memory_type(memory_type)
        val_json = json.dumps(value, ensure_ascii=False)
        new_text = comparable_text(val_json)
        # The kind='note' policy key is `note_text_v1`, not `_value_schema_for`'s
        # `text_v1`; a one-character mismatch fails `resolve_memory_policy` and silently drops the memory from
        # `list_product_for_user` (= the gate-on injection path).
        schema_value = "note_text_v1" if kind == "note" else _value_schema_for(value)
        want_ids = [str(s).strip() for s in (supersedes or []) if str(s or "").strip()]

        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await ensure_memory_write_allowed(db, user_id=user_id)

                # ── Deduplication/near-duplicate scan scope = identity lookup scope ──
                # ⚠ By default, do not partition by scope: today `(kind, key)` is identity (`upsert`'s
                # `scope_specified=False` semantics), which this batch preserves entirely. Defaulting writes to the current Space
                # belongs to batch 4 (`FEATURE_MEMORY_SCOPE_WRITE`). Partitioning now would leave the same key
                # in both global and Space scopes, injecting both ⇒ supersession creates a contradiction.
                # Use scope from the request argument, never infer it from stored rows (memory-revive S2 red line).
                scan_sql = (
                    "SELECT * FROM memory_entries "
                    "WHERE subject_type='user' AND subject_id=? AND deleted_at IS NULL "
                    "AND memory_role != ? AND kind != 'datarc_binding'"
                )
                scan_params: list[object] = [user_id, MemoryRole.OPERATIONAL]
                if scope_specified:
                    scan_sql += " AND COALESCE(workspace_id, '*') = COALESCE(?, '*')"
                    scan_params.append(workspace_id)
                async with db.execute(scan_sql, tuple(scan_params)) as cur:
                    scan_rows = await cur.fetchall()

                # ① Exact-text deduplication: only bump last_confirmed_at on a match (no version+1; the value did not change,
                #    so this is repetition rather than modification).
                #    ⚠ Do not return early after a match: the user may repeat old semantics while explicitly superseding another entry
                #    (keep only Top 5 and remove the Top 10 entry). An early return swallows supersedes.
                confirmed_id: str | None = None
                for row in scan_rows:
                    if is_verbatim_duplicate(new_text, comparable_text(row["value"])):
                        # ★ Raise confidence according to this write's identity (upward only).
                        # Merely bumping `last_confirmed_at` leaves a background-inferred memory at `low` after the user
                        # personally repeats it. The decision table's sole prohibition (low overwriting high) then never applies,
                        # and the next background write can still overwrite what the user just stated.
                        # Confidence derived from writer identity previously covered only creation/overwrite,
                        # missing repetition. Caught in final review on 2026-09-04.
                        # ⚠ Upward only: a background repetition matching a high row must not downgrade it to low.
                        merged_confidence = (
                            CONFIDENCE_HIGH if CONFIDENCE_HIGH in (confidence, normalize_confidence(row["confidence"])) else confidence
                        )
                        await db.execute(
                            "UPDATE memory_entries SET last_confirmed_at=?, confidence=? WHERE id=?",
                            (now, merged_confidence, row["id"]),
                        )
                        confirmed_id = str(row["id"])
                        break

                # ② Identity lookup. With `scope_specified=True`, the conflict target must be
                #    identical to `idx_memory_entries_identity_scoped` (batch 0 lesson:
                #    changing constraints changes the interface). By default, keep today's `(kind, key)` identity and
                #    overwrite whichever row matches in place; never create another row for the same key.
                # ⚠ Apply the same operational-row exclusions as the scan above. Otherwise a
                # `kind='datarc_binding'` write matches the operational BI binding and rewrites it into a
                # product row (dict value ⇒ schema `object_v1` is outside the visibility table),
                # dropping it from injection and causing `v2_readiness()` (without a user filter) to
                # return 503 for everyone's memory page. Batch 1's operational-row exclusion previously applied only to scanning.
                identity_sql = """
                    SELECT * FROM memory_entries
                    WHERE subject_type='user' AND subject_id=? AND kind=? AND key=?
                      AND memory_role != 'operational' AND kind != 'datarc_binding'
                """
                identity_params: list[object] = [user_id, kind, key]
                if scope_specified:
                    identity_sql += " AND COALESCE(workspace_id, '*') = COALESCE(?, '*')"
                    identity_params.append(workspace_id)
                identity_sql += " ORDER BY (workspace_id IS NULL) DESC, id LIMIT 1"
                existing = None
                if confirmed_id is None:
                    async with db.execute(identity_sql, tuple(identity_params)) as cur:
                        existing = await cur.fetchone()

                if confirmed_id is not None:
                    entry_id = confirmed_id
                    action = "confirmed"
                elif existing is not None:
                    if bool(existing["locked"]):
                        raise MemoryEntryLockedError(existing["id"])
                    # Trashed entries no longer have authority to block new writes: the user deleted them,
                    # so continued blocking lets a deleted memory decide for the user.
                    if existing["deleted_at"] is None and not can_overwrite(
                        new_confidence=confidence,
                        existing_confidence=existing["confidence"],
                    ):
                        blocked = _row_to_entry(existing)
                        await db.commit()
                        return MemoryWriteResult(
                            action="blocked",
                            entry=None,
                            blocked_by=blocked,
                        )
                    entry_id = str(existing["id"])
                    # ★ This row may be in trash because it was superseded. Directly setting `deleted_at=NULL`
                    # keeps it and its successor alive simultaneously, letting supersession create a contradiction:
                    # the user first says only Top 5 (top_n superseded by top_n_v2), then changes to Top 3,
                    # and the model reuses top_n per the prompt ⇒ the prompt contains both Top 3 and Top 5.
                    # Revival applies only to user-deleted rows; reviving a superseded row must also retire its successor.
                    revived_successor = existing["superseded_by"] if existing["deleted_at"] else None
                    if revived_successor:
                        want_ids.append(str(revived_successor))
                    await db.execute(
                        """
                        UPDATE memory_entries
                        SET value=?, updated_at=?, deleted_at=NULL, version=version+1,
                            memory_role=?, value_schema=?, memory_policy_version=?,
                            memory_type=?, confidence=?, last_confirmed_at=?,
                            previous_value=value, previous_value_at=updated_at,
                            superseded_by=NULL,
                            last_modified_actor_type=?, last_modified_actor_id=?,
                            last_modified_at=?
                        WHERE id=?
                        """,
                        (
                            val_json,
                            now,
                            MemoryRole.PRODUCT_MEMORY,
                            schema_value,
                            CURRENT_MEMORY_POLICY_VERSION,
                            type_value,
                            confidence,
                            now,
                            actor_type or source,
                            actor_id,
                            now,
                            entry_id,
                        ),
                    )
                    action = "updated"
                else:
                    entry_id = f"mem_{uuid4().hex[:12]}"
                    await db.execute(
                        """
                        INSERT INTO memory_entries(
                            id, subject_type, subject_id, kind, key, value, locked,
                            lock_reason, source, created_at, updated_at, workspace_id,
                            version, memory_role, value_schema, memory_policy_version,
                            created_source_type, created_source_turn_id,
                            created_source_candidate_id, created_source_workspace_id,
                            last_modified_actor_type, last_modified_actor_id,
                            last_modified_at, memory_type, confidence, last_confirmed_at
                        )
                        VALUES (?, 'user', ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?, 1, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            entry_id,
                            user_id,
                            kind,
                            key,
                            val_json,
                            source,
                            now,
                            now,
                            workspace_id,
                            MemoryRole.PRODUCT_MEMORY,
                            schema_value,
                            CURRENT_MEMORY_POLICY_VERSION,
                            created_source_type or source,
                            created_source_turn_id,
                            created_source_candidate_id,
                            workspace_id,
                            actor_type or source,
                            actor_id,
                            now,
                            type_value,
                            confidence,
                            now,
                        ),
                    )
                    action = "created"

                # ③ Cross-key supersession: soft-delete + superseded_by points to the new entry ⇒ existing trash, restorable.
                superseded: list[str] = []
                supersede_blocked: list[str] = []
                for target_id in dict.fromkeys(want_ids):
                    if target_id == entry_id:
                        continue
                    async with db.execute(
                        """
                        SELECT id, locked, confidence FROM memory_entries
                        WHERE id=? AND subject_type='user' AND subject_id=?
                          AND deleted_at IS NULL
                          -- 取代只能打到真记忆：运维行（tool_context / datarc_binding）
                          -- 也在这张表里，被 supersede 掉就是把 BI 上下文状态删了。
                          AND memory_role != 'operational' AND kind != 'datarc_binding'
                        """,
                        (target_id, user_id),
                    ) as cur:
                        target = await cur.fetchone()
                    if target is None:
                        continue
                    if bool(target["locked"]) or not can_overwrite(
                        new_confidence=confidence,
                        existing_confidence=target["confidence"],
                    ):
                        supersede_blocked.append(target_id)
                        continue
                    await db.execute(
                        """
                        UPDATE memory_entries
                        SET deleted_at=?, superseded_by=?, updated_at=?, version=version+1
                        WHERE id=?
                        """,
                        (now, entry_id, now, target_id),
                    )
                    superseded.append(target_id)

                # ④ Only identify near duplicates; do not merge (v1 decision: automatic merging silently consumes real new preferences).
                gone = set(superseded)
                # Compute the new text's token set once (this loop holds the write lock; see near_duplicate_matcher).
                looks_near = near_duplicate_matcher(new_text)
                near = tuple(
                    str(row["id"])
                    for row in scan_rows
                    if str(row["id"]) not in gone and str(row["id"]) != entry_id and looks_near(comparable_text(row["value"]))
                )

                written = await _select_entry(db, entry_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

        return MemoryWriteResult(
            action=action,
            entry=written,
            superseded=tuple(superseded),
            supersede_blocked=tuple(supersede_blocked),
            near_duplicates=near,
        )

    async def patch_product_value(
        self,
        *,
        user_id: str,
        entry_id: str,
        expected_version: int,
        edit_schema: str,
        value: object,
    ) -> MemoryEntry:
        """Apply a type-checked, version-aware value-only product Memory edit."""

        from bowhead_core.memory_kind_policy import resolve_memory_policy

        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await ensure_memory_write_allowed(
                    db,
                    user_id=user_id,
                )
                current = await _get_owned_entry(
                    db,
                    user_id=user_id,
                    entry_id=entry_id,
                )
                if current is None:
                    raise MemoryEntryNotFoundError(entry_id)
                entry = _row_to_entry(current)
                if entry.deleted_at is not None:
                    raise MemoryEntryStateError("memory entry is trashed")
                if entry.locked:
                    raise MemoryEntryLockedError(entry.id)
                if entry.version != expected_version:
                    raise MemoryVersionConflictError(entry)

                policy = resolve_memory_policy(
                    memory_role=entry.memory_role,
                    memory_policy_version=entry.memory_policy_version,
                    kind=entry.kind,
                    value_schema=entry.value_schema,
                    value=entry.parsed_value(),
                )
                updated_policy = resolve_memory_policy(
                    memory_role=entry.memory_role,
                    memory_policy_version=entry.memory_policy_version,
                    kind=entry.kind,
                    value_schema=entry.value_schema,
                    value=value,
                )
                if (
                    not policy.user_visible
                    or policy.edit_schema is None
                    or policy.edit_schema != edit_schema
                    or not updated_policy.user_visible
                    or updated_policy.edit_schema != edit_schema
                ):
                    raise MemoryEntrySchemaError(
                        "memory edit schema does not match the registered policy",
                    )

                # Memory redesign batch 1: manual edits come from the user ⇒ `high`, preserving the previous version for rollback.
                # With the gate off, leave all three new columns unchanged (COALESCE preserves existing values).
                from bowhead_core.memory_readiness import memory_storage_ready
                from bowhead_core.memory_write_policy import CONFIDENCE_HIGH

                edit_confidence = CONFIDENCE_HIGH if memory_storage_ready() else None
                cursor = await db.execute(
                    """
                    UPDATE memory_entries
                    SET value=?, updated_at=?, version=version+1,
                        confidence=COALESCE(?, confidence),
                        last_confirmed_at=COALESCE(?, last_confirmed_at),
                        previous_value=CASE WHEN ? IS NULL THEN previous_value ELSE value END,
                        previous_value_at=CASE WHEN ? IS NULL THEN previous_value_at ELSE updated_at END,
                        last_modified_actor_type='user',
                        last_modified_actor_id=?, last_modified_at=?
                    WHERE id=? AND subject_type='user' AND subject_id=?
                      AND version=? AND locked=0 AND deleted_at IS NULL
                    """,
                    (
                        json.dumps(value, ensure_ascii=False),
                        now,
                        edit_confidence,
                        now if edit_confidence else None,
                        edit_confidence,
                        edit_confidence,
                        user_id,
                        now,
                        entry_id,
                        user_id,
                        expected_version,
                    ),
                )
                if (cursor.rowcount or 0) != 1:
                    latest = await _get_owned_entry(
                        db,
                        user_id=user_id,
                        entry_id=entry_id,
                    )
                    if latest is None:
                        raise MemoryEntryNotFoundError(entry_id)
                    raise MemoryVersionConflictError(_row_to_entry(latest))

                updated = await _get_owned_entry(
                    db,
                    user_id=user_id,
                    entry_id=entry_id,
                )
                assert updated is not None
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _row_to_entry(updated)

    async def soft_delete(
        self,
        *,
        user_id: str,
        entry_id: str,
        expected_version: int | None = None,
    ) -> bool:
        """软删 (set deleted_at). locked entries 不会被改 (WHERE locked=0)."""

        return await self._set_deleted_state(
            user_id=user_id,
            entry_id=entry_id,
            expected_version=expected_version,
            restore=False,
        )

    async def restore(
        self,
        *,
        user_id: str,
        entry_id: str,
        expected_version: int | None = None,
    ) -> bool:
        """从 trash 还原 (set deleted_at = NULL)."""

        return await self._set_deleted_state(
            user_id=user_id,
            entry_id=entry_id,
            expected_version=expected_version,
            restore=True,
        )

    async def list_trashed(self, user_id: str) -> list[MemoryEntry]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM memory_entries "
                "WHERE subject_type='user' AND subject_id=? AND deleted_at IS NOT NULL "
                "ORDER BY datetime(deleted_at) DESC",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_entry(r) for r in rows]

    async def count_trashed(self, user_id: str) -> int:
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT COUNT(*) FROM memory_entries WHERE subject_type='user' AND subject_id=? AND deleted_at IS NOT NULL",
                (user_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get_entries_by_ids(self, user_id: str, ids: Sequence[str]) -> dict[str, MemoryEntry]:
        """按 id 批量取条目（记忆重做 · 条目详情面 C/D 两张画面）。

        ``superseded_by`` 与候选的 ``supersedes`` 存的都是 ``memory_entries.id``，
        而界面上要显示的是 key（「已被『ranking_scope』取代」），画面 D 还要显示
        被取代那条的**当前值**。不做这层解析，前端只能把 ``mem_1bf370c9c86a``
        原样摆给用户看。

        ⚠ 必须带 ``user_id`` 过滤：id 全局唯一，但拿别人的 id 解析出内容就是跨用户读。
        取代关系只可能发生在同一 subject 内，加这个条件不会少解出任何一条。
        ⚠ **不**过滤 ``deleted_at``：取代方自己可能又被取代进了回收站，
        那时仍要显示它的 key，否则回收站上「已被…取代」会退化成空。
        解不出来的 id **不出现在返回的 dict 里**（已被硬删），由调用方决定怎么兜底 ——
        不能编一个 key 出来。
        """
        wanted = [str(i) for i in ids if i]
        if not wanted:
            return {}
        out: dict[str, MemoryEntry] = {}
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            # ⚠ Same discipline as `list_for_user` / `list_product_for_user` / notes / snapshots:
            # while destructive privacy operations hold the gate, all memory reads fail closed.
            # Initially this method bypassed it with bare `aiosqlite.connect`, but its two consumers
            # (candidate cards' `supersedes`, entry pages' `superseded_by`) display the key and current value
            # directly in the UI, exposing a read during a window when every other path must remain hidden.
            if await is_memory_read_hidden(db, user_id=user_id):
                return {}
            # SQLite allows 999 bound variables; chunk by 500 to avoid errors on large pages.
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                async with db.execute(
                    f"SELECT * FROM memory_entries WHERE subject_type='user' AND subject_id=? AND id IN ({placeholders})",
                    (user_id, *chunk),
                ) as cur:
                    for row in await cur.fetchall():
                        entry = _row_to_entry(row)
                        out[entry.id] = entry
        return out

    async def resolve_entry_keys(self, user_id: str, ids: Sequence[str]) -> dict[str, str]:
        """`get_entries_by_ids` 的 id→key 投影。取代关系只需要 key 的那些路径用它。"""
        return {eid: e.key for eid, e in (await self.get_entries_by_ids(user_id, ids)).items()}

    async def hard_delete_old_trash(self, *, max_age_days: int = 30) -> int:
        """Cron: hard delete entries trashed > max_age_days ago.
        Uses datetime() to normalize ISO 'T' vs sqlite ' ' (spike #19)."""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "DELETE FROM memory_entries WHERE subject_type='user' AND deleted_at IS NOT NULL AND datetime(deleted_at) < datetime('now', ?)",
                (f"-{int(max_age_days)} days",),
            )
            await db.commit()
            return cur.rowcount or 0

    async def hard_delete_user(self, user_id: str) -> int:
        """完全清除 user 所有 entries (cron: removed-user GC after 30 days)."""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
            )
            cur = await db.execute(
                "DELETE FROM memory_entries WHERE subject_type='user' AND subject_id=?",
                (user_id,),
            )
            await db.commit()
            return cur.rowcount or 0

    async def soft_delete_all_for_user(self, user_id: str) -> int:
        """重置: set deleted_at on all unlocked active entries. 返回数量."""
        now = datetime.now(tz=UTC).isoformat(timespec="seconds")
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
            )
            cur = await db.execute(
                "UPDATE memory_entries SET deleted_at=?, updated_at=?, version=version+1, "
                "last_modified_actor_type='user', last_modified_actor_id=?, last_modified_at=? "
                "WHERE subject_type='user' AND subject_id=? AND locked=0 AND deleted_at IS NULL",
                (now, now, user_id, now, user_id),
            )
            await db.commit()
            return cur.rowcount or 0

    async def delete_about_you_v3(
        self,
        *,
        user_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """Snapshot and delete the About you singleton as one recovery group."""

        now_dt = datetime.now(tz=UTC)
        now = now_dt.isoformat(timespec="seconds")
        expires_at = (now_dt + timedelta(days=30)).isoformat(
            timespec="seconds",
        )
        snapshot_id = f"snap_{uuid4().hex}"
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await ensure_memory_write_allowed(
                    db,
                    user_id=user_id,
                )
                if not await _table_exists(db, "memory_notes"):
                    raise MemoryAboutYouNotFoundError
                async with db.execute(
                    """
                    SELECT *
                    FROM memory_notes
                    WHERE subject_type='user' AND subject_id=?
                      AND deleted_at IS NULL AND content<>''
                    """,
                    (user_id,),
                ) as cursor:
                    note = await cursor.fetchone()
                if note is None:
                    raise MemoryAboutYouNotFoundError
                if _note_row_lock_active(note, now_dt):
                    raise MemoryResetBlockedError(
                        str(note["editing_locked_until"]),
                    )
                if int(note["version"]) != expected_version:
                    raise MemoryAboutYouVersionConflictError(
                        int(note["version"]),
                    )
                anchors: list[aiosqlite.Row] = []
                if await _table_exists(db, "memory_note_anchors"):
                    async with db.execute(
                        """
                        SELECT *
                        FROM memory_note_anchors
                        WHERE user_id=?
                        ORDER BY id
                        """,
                        (user_id,),
                    ) as cursor:
                        anchors = list(await cursor.fetchall())
                counts = {
                    "notes": 1,
                    "entries": 0,
                    "anchors": len(anchors),
                    "locked_skipped": 0,
                    "pending_preserved": 0,
                }
                await db.execute(
                    """
                    INSERT INTO memory_reset_snapshots(
                        id, user_id, snapshot_json, created_at,
                        schema_version, snapshot_type,
                        target_resource_type, target_resource_id,
                        expires_at, counts_json
                    )
                    VALUES (?, ?, '{}', ?, 3, 'item_delete',
                            'memory_note', ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        user_id,
                        now,
                        note["id"],
                        expires_at,
                        json.dumps(counts, sort_keys=True),
                    ),
                )
                restore_group_id = f"mrg_{uuid4().hex}"
                await _insert_reset_item(
                    db,
                    snapshot_id=snapshot_id,
                    restore_group_id=restore_group_id,
                    resource_type="memory_note",
                    resource_id=str(note["id"]),
                    resource_version=int(note["version"]),
                    deleted_version=int(note["version"]) + 1,
                    payload=_note_reset_payload(note),
                )
                for anchor in anchors:
                    await _insert_reset_item(
                        db,
                        snapshot_id=snapshot_id,
                        restore_group_id=restore_group_id,
                        resource_type="note_anchor",
                        resource_id=str(anchor["id"]),
                        resource_version=1,
                        deleted_version=1,
                        payload=_anchor_reset_payload(anchor),
                    )
                cursor = await db.execute(
                    """
                    UPDATE memory_notes
                    SET deleted_at=?, updated_at=?, version=version+1,
                        last_modified_actor_type='user',
                        last_modified_actor_id=?, last_modified_at=?,
                        editing_locked_until=NULL,
                        editing_lock_token_hash=NULL,
                        editing_lock_owner_type=NULL
                    WHERE id=? AND subject_type='user' AND subject_id=?
                      AND version=? AND deleted_at IS NULL
                    """,
                    (
                        now,
                        now,
                        user_id,
                        now,
                        note["id"],
                        user_id,
                        expected_version,
                    ),
                )
                if (cursor.rowcount or 0) != 1:
                    raise RuntimeError(
                        "About you changed during delete transaction",
                    )
                if anchors:
                    await db.execute(
                        "DELETE FROM memory_note_anchors WHERE user_id=?",
                        (user_id,),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "recovery_id": snapshot_id,
            "purge_at": expires_at,
            "version": str(int(note["version"]) + 1),
        }

    async def list_about_you_recoveries(
        self,
        *,
        user_id: str,
    ) -> list[dict[str, Any]]:
        now = datetime.now(tz=UTC).isoformat(timespec="seconds")
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if await is_memory_read_hidden(db, user_id=user_id):
                return []
            async with db.execute(
                """
                SELECT s.id AS snapshot_id, s.created_at, s.expires_at,
                       i.resource_id, i.deleted_version, i.payload_json,
                       i.restore_state
                FROM memory_reset_snapshots AS s
                JOIN memory_reset_snapshot_items AS i
                  ON i.snapshot_id=s.id
                 AND i.resource_type='memory_note'
                WHERE s.user_id=? AND s.schema_version=3
                  AND s.snapshot_type='item_delete'
                  AND s.consumed_at IS NULL
                  AND datetime(s.expires_at)>datetime(?)
                  AND i.payload_json<>'{}'
                ORDER BY s.created_at DESC, s.id DESC
                """,
                (user_id, now),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "recovery_id": row["snapshot_id"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "resource_id": row["resource_id"],
                "deleted_version": str(row["deleted_version"]),
                "restore_state": row["restore_state"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    async def preview_reset_v3(self, *, user_id: str) -> dict[str, int]:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            targets = await _collect_reset_targets(db, user_id=user_id)
        return _reset_counts(targets)

    async def reset_v3(
        self,
        *,
        user_id: str,
        idempotency_key: str | None = None,
    ) -> MemoryResetV3Result:
        """Atomically snapshot and soft-delete the current product Memory set."""

        if idempotency_key is not None:
            _validate_idempotency_key(idempotency_key)
        now_dt = datetime.now(tz=UTC)
        now = now_dt.isoformat(timespec="seconds")
        expires_at = (now_dt + timedelta(days=30)).isoformat(timespec="seconds")
        snapshot_id = f"snap_{uuid4().hex}"
        request_fingerprint = "reset_v3"

        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key is not None:
                    cached = await _get_memory_mutation_result(
                        db,
                        user_id=user_id,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                    )
                    if cached is not None:
                        await db.rollback()
                        return _memory_reset_result_from_json(cached)
                await ensure_memory_write_allowed(
                    db,
                    user_id=user_id,
                )
                targets = await _collect_reset_targets(
                    db,
                    user_id=user_id,
                )
                note = targets["note"]
                if note is not None and _note_row_lock_active(note, now_dt):
                    raise MemoryResetBlockedError(
                        str(note["editing_locked_until"]),
                    )
                counts = _reset_counts(targets)
                await db.execute(
                    """
                    INSERT INTO memory_reset_snapshots(
                        id, user_id, snapshot_json, created_at,
                        schema_version, snapshot_type, expires_at, counts_json
                    )
                    VALUES (?, ?, '{}', ?, 3, 'reset', ?, ?)
                    """,
                    (
                        snapshot_id,
                        user_id,
                        now,
                        expires_at,
                        json.dumps(counts, sort_keys=True),
                    ),
                )

                for row in targets["entries"]:
                    group_id = f"mrg_{uuid4().hex}"
                    payload = _memory_entry_reset_payload(row)
                    await _insert_reset_item(
                        db,
                        snapshot_id=snapshot_id,
                        restore_group_id=group_id,
                        resource_type="memory_entry",
                        resource_id=str(row["id"]),
                        resource_version=int(row["version"]),
                        deleted_version=int(row["version"]) + 1,
                        payload=payload,
                    )
                    cursor = await db.execute(
                        """
                        UPDATE memory_entries
                        SET deleted_at=?, updated_at=?, version=version+1,
                            last_modified_actor_type='user',
                            last_modified_actor_id=?, last_modified_at=?
                        WHERE id=? AND subject_type='user' AND subject_id=?
                          AND version=? AND deleted_at IS NULL AND locked=0
                        """,
                        (
                            now,
                            now,
                            user_id,
                            now,
                            row["id"],
                            user_id,
                            row["version"],
                        ),
                    )
                    if (cursor.rowcount or 0) != 1:
                        raise RuntimeError(
                            "memory reset manifest changed during transaction",
                        )

                if note is not None:
                    note_group_id = f"mrg_{uuid4().hex}"
                    await _insert_reset_item(
                        db,
                        snapshot_id=snapshot_id,
                        restore_group_id=note_group_id,
                        resource_type="memory_note",
                        resource_id=str(note["id"]),
                        resource_version=int(note["version"]),
                        deleted_version=int(note["version"]) + 1,
                        payload=_note_reset_payload(note),
                    )
                    for anchor in targets["anchors"]:
                        await _insert_reset_item(
                            db,
                            snapshot_id=snapshot_id,
                            restore_group_id=note_group_id,
                            resource_type="note_anchor",
                            resource_id=str(anchor["id"]),
                            resource_version=1,
                            deleted_version=1,
                            payload=_anchor_reset_payload(anchor),
                        )
                    cursor = await db.execute(
                        """
                        UPDATE memory_notes
                        SET deleted_at=?, updated_at=?, version=version+1,
                            last_modified_actor_type='user',
                            last_modified_actor_id=?, last_modified_at=?,
                            editing_locked_until=NULL,
                            editing_lock_token_hash=NULL,
                            editing_lock_owner_type=NULL
                        WHERE id=? AND subject_type='user' AND subject_id=?
                          AND version=? AND deleted_at IS NULL
                        """,
                        (
                            now,
                            now,
                            user_id,
                            now,
                            note["id"],
                            user_id,
                            note["version"],
                        ),
                    )
                    if (cursor.rowcount or 0) != 1:
                        raise RuntimeError(
                            "About you reset manifest changed during transaction",
                        )
                    if targets["anchors"]:
                        await db.execute(
                            "DELETE FROM memory_note_anchors WHERE user_id=?",
                            (user_id,),
                        )

                result = MemoryResetV3Result(
                    snapshot_id=snapshot_id,
                    expires_at=expires_at,
                    counts=counts,
                )
                if idempotency_key is not None:
                    await _record_memory_mutation_result(
                        db,
                        user_id=user_id,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        result={
                            "snapshot_id": result.snapshot_id,
                            "expires_at": result.expires_at,
                            "counts": result.counts,
                        },
                        now=now,
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

        return result

    async def list_reset_v3_summaries(
        self,
        *,
        user_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        now = datetime.now(tz=UTC).isoformat(timespec="seconds")
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if await is_memory_read_hidden(db, user_id=user_id):
                return []
            async with db.execute(
                """
                SELECT *
                FROM memory_reset_snapshots
                WHERE user_id=? AND schema_version=3
                  AND snapshot_type='reset' AND consumed_at IS NULL
                  AND expires_at IS NOT NULL
                  AND datetime(expires_at)>datetime(?)
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, now, limit),
            ) as cursor:
                parents = await cursor.fetchall()
            summaries = []
            for parent in parents:
                state_counts, remaining = await _reset_snapshot_state_counts(
                    db,
                    snapshot_id=str(parent["id"]),
                )
                summaries.append(
                    {
                        "id": parent["id"],
                        "created_at": parent["created_at"],
                        "expires_at": parent["expires_at"],
                        "counts": json.loads(parent["counts_json"]),
                        "state_counts": state_counts,
                        "remaining_recoverable": remaining,
                        "recoverable": remaining > 0,
                        "reason": None if remaining > 0 else "consumed",
                    },
                )
        return summaries

    async def get_reset_v3_detail(
        self,
        *,
        snapshot_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        now = datetime.now(tz=UTC).isoformat(timespec="seconds")
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if await is_memory_read_hidden(db, user_id=user_id):
                return None
            parent = await _get_active_v3_reset_snapshot(
                db,
                snapshot_id=snapshot_id,
                user_id=user_id,
                now=now,
            )
            if parent is None:
                return None
            state_counts, remaining = await _reset_snapshot_state_counts(
                db,
                snapshot_id=snapshot_id,
            )
            async with db.execute(
                """
                SELECT resource_type, resource_id, display_type, payload_json,
                       restore_state, restore_error
                FROM memory_reset_snapshot_items
                WHERE snapshot_id=?
                ORDER BY restore_group_id, resource_type, resource_id
                """,
                (snapshot_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            items = []
            for row in rows:
                payload = json.loads(row["payload_json"]) if row["payload_json"] != "{}" else {}
                items.append(
                    {
                        "resource_type": row["resource_type"],
                        "resource_id": row["resource_id"],
                        "display_type": row["display_type"] or payload.get("display_type"),
                        "restore_state": row["restore_state"],
                        "reason": row["restore_error"],
                        "governance_target": await _governance_target(
                            db,
                            user_id=user_id,
                            resource_type=str(row["resource_type"]),
                            resource_id=str(row["resource_id"]),
                        ),
                        "can_retry": (row["payload_json"] != "{}" and row["restore_state"] in ("pending", "conflict", "unavailable")),
                    },
                )
        return {
            "id": parent["id"],
            "created_at": parent["created_at"],
            "expires_at": parent["expires_at"],
            "state_counts": state_counts,
            "remaining_recoverable": remaining,
            "items": items,
        }

    async def restore_reset_v3(
        self,
        *,
        snapshot_id: str,
        user_id: str,
        accessible_space_ids: set[str],
        idempotency_key: str | None = None,
    ) -> MemoryRestoreV3Result:
        """Restore non-conflicting v3 snapshot groups without overwriting newer data."""

        if idempotency_key is not None:
            _validate_idempotency_key(idempotency_key)
        now_dt = datetime.now(tz=UTC)
        now = now_dt.isoformat(timespec="seconds")
        request_fingerprint = f"restore_reset_v3:{snapshot_id}"
        restored_counts = {
            "entries": 0,
            "notes": 0,
            "anchors": 0,
        }
        unavailable_counts = {
            "entries": 0,
        }
        conflicts: list[dict[str, str]] = []

        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key is not None:
                    cached = await _get_memory_mutation_result(
                        db,
                        user_id=user_id,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                    )
                    if cached is not None:
                        await db.rollback()
                        return _memory_restore_result_from_json(cached)
                await ensure_memory_write_allowed(
                    db,
                    user_id=user_id,
                )
                snapshot = await _get_active_v3_reset_snapshot(
                    db,
                    snapshot_id=snapshot_id,
                    user_id=user_id,
                    now=now,
                )
                if snapshot is None:
                    raise MemoryResetSnapshotNotFoundError
                async with db.execute(
                    """
                    SELECT *
                    FROM memory_reset_snapshot_items
                    WHERE snapshot_id=?
                      AND restore_state IN ('pending', 'conflict', 'unavailable')
                      AND payload_json<>'{}'
                    ORDER BY restore_group_id, resource_type, resource_id
                    """,
                    (snapshot_id,),
                ) as cursor:
                    items = await cursor.fetchall()

                groups: dict[str, list[aiosqlite.Row]] = {}
                for item in items:
                    groups.setdefault(
                        str(item["restore_group_id"]),
                        [],
                    ).append(item)

                for group_items in groups.values():
                    decoded = [
                        (
                            item,
                            json.loads(str(item["payload_json"])),
                        )
                        for item in group_items
                    ]
                    unavailable_reason = _restore_group_unavailable_reason(
                        decoded,
                        accessible_space_ids=accessible_space_ids,
                    )
                    if unavailable_reason is not None:
                        await _mark_restore_group(
                            db,
                            snapshot_id=snapshot_id,
                            group_items=group_items,
                            state="unavailable",
                            error=unavailable_reason,
                        )
                        for item, _payload in decoded:
                            bucket = _restore_count_bucket(
                                str(item["resource_type"]),
                            )
                            if bucket in unavailable_counts:
                                unavailable_counts[bucket] += 1
                        continue

                    conflict_reason = await _restore_group_conflict_reason(
                        db,
                        user_id=user_id,
                        decoded=decoded,
                    )
                    if conflict_reason is not None:
                        await _mark_restore_group(
                            db,
                            snapshot_id=snapshot_id,
                            group_items=group_items,
                            state="conflict",
                            error=conflict_reason,
                        )
                        conflicts.append(
                            {
                                "resource_type": str(
                                    group_items[0]["resource_type"],
                                ),
                                "reason": conflict_reason,
                            },
                        )
                        continue

                    for item, payload in decoded:
                        resource_type = str(item["resource_type"])
                        await _restore_snapshot_item(
                            db,
                            user_id=user_id,
                            item=item,
                            payload=payload,
                            now=now,
                        )
                        restored_counts[_restore_count_bucket(resource_type)] += 1
                    await _mark_restore_group(
                        db,
                        snapshot_id=snapshot_id,
                        group_items=group_items,
                        state="restored",
                        error=None,
                        restored_at=now,
                        scrub_payload=True,
                    )

                async with db.execute(
                    """
                    SELECT COUNT(*)
                    FROM memory_reset_snapshot_items
                    WHERE snapshot_id=? AND payload_json<>'{}'
                    """,
                    (snapshot_id,),
                ) as cursor:
                    remaining_row = await cursor.fetchone()
                remaining = int(remaining_row[0]) if remaining_row else 0
                if remaining == 0:
                    await db.execute(
                        """
                        UPDATE memory_reset_snapshots
                        SET consumed_at=?
                        WHERE id=? AND user_id=?
                        """,
                        (now, snapshot_id, user_id),
                    )
                result = MemoryRestoreV3Result(
                    snapshot_id=snapshot_id,
                    status=("restored" if not conflicts and not any(unavailable_counts.values()) else "partially_restored"),
                    restored_counts=restored_counts,
                    conflicts=conflicts,
                    unavailable_counts=unavailable_counts,
                    remaining_recoverable=remaining,
                )
                if idempotency_key is not None:
                    await _record_memory_mutation_result(
                        db,
                        user_id=user_id,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        result={
                            "snapshot_id": result.snapshot_id,
                            "status": result.status,
                            "restored_counts": result.restored_counts,
                            "conflicts": result.conflicts,
                            "unavailable_counts": result.unavailable_counts,
                            "remaining_recoverable": (result.remaining_recoverable),
                        },
                        now=now,
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

        return result

    async def save_reset_snapshot(self, *, user_id: str, snapshot: object) -> str:
        """重置前快照 (用于 undo / audit). 返回 snapshot_id."""
        from datetime import datetime
        from uuid import uuid4

        snap_id = f"snap_{uuid4().hex[:12]}"
        now = datetime.now(tz=UTC).isoformat(timespec="seconds")
        payload = json.dumps(snapshot, ensure_ascii=False)
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
            )
            await db.execute(
                "INSERT INTO memory_reset_snapshots (id, user_id, snapshot_json, created_at) VALUES (?, ?, ?, ?)",
                (snap_id, user_id, payload, now),
            )
            await db.commit()
        return snap_id

    async def hard_delete_old_reset_snapshots(self, *, max_age_days: int = 30) -> int:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                DELETE FROM memory_reset_snapshot_items
                WHERE snapshot_id IN (
                    SELECT id
                    FROM memory_reset_snapshots
                    WHERE (
                        expires_at IS NOT NULL
                        AND datetime(expires_at) < datetime('now')
                    ) OR (
                        expires_at IS NULL
                        AND datetime(created_at) < datetime('now', ?)
                    )
                )
                """,
                (f"-{int(max_age_days)} days",),
            )
            cur = await db.execute(
                """
                DELETE FROM memory_reset_snapshots
                WHERE (
                    expires_at IS NOT NULL
                    AND datetime(expires_at) < datetime('now')
                ) OR (
                    expires_at IS NULL
                    AND datetime(created_at) < datetime('now', ?)
                )
                """,
                (f"-{int(max_age_days)} days",),
            )
            await db.execute(
                """
                DELETE FROM memory_mutation_idempotency
                WHERE datetime(created_at)<datetime('now', ?)
                """,
                (f"-{int(max_age_days)} days",),
            )
            await db.commit()
            return cur.rowcount or 0

    async def count_recent_reset_snapshots(
        self,
        *,
        user_id: str,
        max_age_days: int = 30,
    ) -> int:
        async with open_shared_ctx(self._db_path) as db:
            if await is_memory_read_hidden(db, user_id=user_id):
                return 0
            async with db.execute(
                """
                SELECT COUNT(*)
                FROM memory_reset_snapshots
                WHERE user_id=?
                  AND consumed_at IS NULL
                  AND (
                      (
                          schema_version=3
                          AND snapshot_type='reset'
                          AND expires_at IS NOT NULL
                          AND datetime(expires_at) > datetime('now')
                      ) OR (
                          schema_version<3
                          AND datetime(created_at) >= datetime('now', ?)
                      )
                  )
                """,
                (
                    user_id,
                    f"-{int(max_age_days)} days",
                ),
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get_reset_snapshot(
        self,
        *,
        snapshot_id: str,
        user_id: str,
    ) -> dict | None:
        """V3 PRD-B: read snapshot for undo-reset (only if owned by user_id)."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT snapshot_json, created_at
                FROM memory_reset_snapshots
                WHERE id=? AND user_id=? AND schema_version<3
                """,
                (snapshot_id, user_id),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["snapshot_json"])
            payload["_created_at"] = row["created_at"]
            return payload
        except (json.JSONDecodeError, TypeError):
            return None

    async def delete_reset_snapshot(
        self,
        *,
        snapshot_id: str,
        user_id: str,
    ) -> bool:
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
            )
            await db.execute(
                "DELETE FROM memory_reset_snapshot_items WHERE snapshot_id=?",
                (snapshot_id,),
            )
            cur = await db.execute(
                "DELETE FROM memory_reset_snapshots WHERE id=? AND user_id=?",
                (snapshot_id, user_id),
            )
            await db.commit()
            return (cur.rowcount or 0) > 0

    async def iter_for_user(
        self,
        user_id: str,
        *,
        include_trashed: bool = False,
    ):
        """流式 yield MemoryEntry — 用于 export StreamingResponse (避免一次性 load)."""
        sql = "SELECT * FROM memory_entries WHERE subject_type='user' AND subject_id=?"
        if not include_trashed:
            sql += " AND deleted_at IS NULL"
        sql += " ORDER BY kind, key"
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if await is_memory_read_hidden(db, user_id=user_id):
                return
            async with db.execute(sql, (user_id,)) as cur:
                async for row in cur:
                    yield _row_to_entry(row)

    async def get_datarc_member_uid(self, user_id: str) -> str | None:
        """便捷方法：查 user 绑定的 datarc memberUid (skip trashed)."""
        async with open_shared_ctx(self._db_path) as db:
            if await is_memory_read_hidden(db, user_id=user_id):
                return None
            async with db.execute(
                "SELECT value FROM memory_entries "
                "WHERE subject_type='user' AND subject_id=? "
                "AND kind='datarc_binding' AND key='member_uid' AND deleted_at IS NULL",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return row[0] if isinstance(row[0], str) else None

    # ─── Batch 2 (workspace-space-split): user-level tool context ───
    # Generic shape (PRD §3.1): kind='tool_context', key='{tool}/{slot}',
    # value={"id","name","source","updated_at"}. datarc is the first instance
    # (key='datarc/workspace'); future tools such as PBI need no schema changes.
    # source enum: explicit(tool panel/remember choice) | auto(automatic when set size=1) |
    # session(session soft preference, do not remember) | switch(in-chat switch) | last_used(writeback after BI success).
    # Note: list_for_user has no kind filter, so this kind appears in the memory UI alongside
    # datarc_binding (existing shape). UI exclusion is follow-up work, not a storage-layer responsibility.

    async def get_datarc_workspace(self, user_id: str, scope: DatarcPreferenceScope, *, session_id: str | None = None) -> dict | None:
        """Read only the exact caller/connection/session preference address."""
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                "SELECT value FROM memory_entries WHERE subject_type='user' AND subject_id=? "
                "AND kind='tool_context' AND key=? AND deleted_at IS NULL",
                (user_id, scope.key(session_id)),
            ) as cursor,
        ):
            if await is_memory_read_hidden(db, user_id=user_id):
                return None
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (ValueError, TypeError):
            return None
        if not isinstance(value, dict) or value.get("schema_version") != 2 or type(value.get("schema_version")) is not int:
            return None
        if (value.get("team_id"), value.get("connection_id")) != (scope.team_id, scope.connection_id):
            return None
        if session_id and value.get("session_id") != session_id:
            return None
        if not value.get("id") or value.get("source") not in {"session", "explicit", "auto"}:
            return None
        return value

    async def _clear_datarc_workspace_in_connection(
        self, db: aiosqlite.Connection, user_id: str, scope: DatarcPreferenceScope, session_id: str | None
    ) -> None:
        await ensure_memory_write_allowed(db, user_id=user_id)
        now = _now()
        await db.execute(
            "UPDATE memory_entries SET deleted_at=?, updated_at=?, version=version+1, "
            "last_modified_actor_type='system', last_modified_at=? WHERE subject_type='user' "
            "AND subject_id=? AND kind='tool_context' AND key=? AND deleted_at IS NULL",
            (now, now, now, user_id, scope.key(session_id)),
        )

    async def clear_datarc_workspace(self, user_id: str, scope: DatarcPreferenceScope, *, session_id: str | None = None) -> None:
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._clear_datarc_workspace_in_connection(db, user_id, scope, session_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def commit_datarc_workspace(
        self,
        user_id: str,
        scope: DatarcPreferenceScope,
        *,
        context_id: str,
        context_name: str = "",
        remember: bool,
        session_id: str | None,
        source: str | None = None,
    ) -> DatarcPersistenceReceipt:
        """Commit one selection, including default/overlay changes in one transaction."""
        from bowhead_core.datarc_selection import (
            DatarcCommitUnknown,
            require_datarc_context_migration,
        )

        destination = "default" if remember else "session"
        if not remember and not session_id:
            return DatarcPersistenceReceipt("skipped", destination, "missing_session")
        if not user_id or not context_id:
            raise ValueError("Missing Datarc preference identity")
        value = {
            "schema_version": 2,
            "team_id": scope.team_id,
            "connection_id": scope.connection_id,
            "id": context_id,
            "name": context_name,
            "source": source or ("explicit" if remember else "session"),
            "updated_at": _now(),
            **({"session_id": session_id} if not remember else {}),
        }
        committing = False
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await require_datarc_context_migration(db)
                await self.upsert(
                    user_id=user_id,
                    kind="tool_context",
                    key=scope.key(None if remember else session_id),
                    value=value,
                    source="tool_runtime",
                    memory_role=MemoryRole.OPERATIONAL,
                    value_schema=_value_schema_for(value),
                    memory_policy_version=CURRENT_MEMORY_POLICY_VERSION,
                    connection=db,
                )
                if remember and session_id:
                    await self._clear_datarc_workspace_in_connection(db, user_id, scope, session_id)
                committing = True
                await db.commit()
            except BaseException as error:
                with suppress(Exception):
                    await db.rollback()
                if committing and isinstance(error, Exception):
                    raise DatarcCommitUnknown() from error
                raise
        return DatarcPersistenceReceipt("saved", destination)

    @staticmethod
    def _tool_context_key(tool: str, slot: str, *, session_scoped: bool) -> str:
        """BUG1 修复：session 覆盖层与永久默认分开存，session 写不再 clobber explicit。
        永久默认 key=`{tool}/{slot}`（explicit/auto/last_used）；session 覆盖层
        key=`{tool}/{slot}@session`（换 XX / 不记住，带 session_id）。"""
        return f"{tool}/{slot}@session" if session_scoped else f"{tool}/{slot}"

    async def get_tool_context(
        self,
        user_id: str,
        tool: str,
        slot: str = "workspace",
        *,
        session_scoped: bool = False,
    ) -> dict | None:
        """读用户级 tool 上下文（如「当前北极九章工作区」）。返回 value dict 或 None.

        session_scoped=True 读 session 覆盖层（换 XX / 不记住的临时选择）。"""
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT value FROM memory_entries "
                "WHERE subject_type='user' AND subject_id=? "
                "AND kind='tool_context' AND key=? AND deleted_at IS NULL",
                (user_id, self._tool_context_key(tool, slot, session_scoped=session_scoped)),
            ) as cur,
        ):
            row = await cur.fetchone()
        if row is None or row[0] is None:
            return None
        try:
            val = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None
        return val if isinstance(val, dict) else None

    async def set_tool_context(
        self,
        user_id: str,
        tool: str,
        *,
        context_id: str,
        context_name: str = "",
        source: str = "explicit",
        slot: str = "workspace",
        session_id: str | None = None,
    ) -> None:
        """写用户级 tool 上下文。幂等 upsert；空 context_id 直接 no-op（防误清）.

        session_id: source='session'（会话级软偏好，「不记住」两态之一）时携带——
        解析侧只在同 session 内认它（PRD §2.1 step3），跨会话自然过期重新引导。"""
        if not user_id or not context_id:
            return
        from datetime import datetime

        value: dict[str, str] = {
            "id": str(context_id),
            "name": str(context_name or ""),
            "source": source,
            "updated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        }
        if session_id:
            value["session_id"] = str(session_id)
        # BUG1 fix: source=session writes the session-overlay key (`@session`), preserving
        # permanent defaults (explicit/auto/last_used). The docstring always promised to preserve explicit,
        # but the shared key overwrote it; source-specific keys now implement that contract.
        await self.upsert_operational_memory(
            user_id=user_id,
            kind="tool_context",
            key=self._tool_context_key(tool, slot, session_scoped=(source == "session")),
            value=value,
            source="tool_runtime",
        )

    async def clear_tool_context(
        self,
        user_id: str,
        tool: str,
        slot: str = "workspace",
        *,
        session_scoped: bool = False,
    ) -> None:
        """清用户级 tool 上下文（撤权/失效时用——PRD §2.1 step 1 校验失败清偏好）.

        session_scoped=True 清 session 覆盖层（BUG1：记住 explicit 时顺手清当前
        会话临时切换，让本会话立刻改用新记住的默认）。

        软删（deleted_at）与 upsert 的 DO UPDATE SET deleted_at=NULL 对偶：
        清后再 set 会原地复活同一行。"""
        from datetime import datetime

        now = datetime.now(tz=UTC).isoformat(timespec="seconds")
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
            )
            await db.execute(
                "UPDATE memory_entries SET deleted_at=?, updated_at=?, version=version+1, "
                "last_modified_actor_type='system', last_modified_at=? "
                "WHERE subject_type='user' AND subject_id=? "
                "AND kind='tool_context' AND key=? AND deleted_at IS NULL",
                (
                    now,
                    now,
                    now,
                    user_id,
                    self._tool_context_key(tool, slot, session_scoped=session_scoped),
                ),
            )
            await db.commit()

    async def _set_deleted_state(
        self,
        *,
        user_id: str,
        entry_id: str,
        expected_version: int | None,
        restore: bool,
    ) -> bool:
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await ensure_memory_write_allowed(
                    db,
                    user_id=user_id,
                )
                current = await _get_owned_entry(
                    db,
                    user_id=user_id,
                    entry_id=entry_id,
                )
                if current is None:
                    await db.rollback()
                    return False
                entry = _row_to_entry(current)
                state_matches = entry.deleted_at is not None if restore else entry.deleted_at is None and not entry.locked
                if not state_matches:
                    await db.rollback()
                    return False
                if expected_version is not None and entry.version != expected_version:
                    raise MemoryVersionConflictError(entry)

                deleted_at = None if restore else now
                cursor = await db.execute(
                    """
                    UPDATE memory_entries
                    SET deleted_at=?, updated_at=?, version=version+1,
                        last_modified_actor_type='user',
                        last_modified_actor_id=?, last_modified_at=?
                    WHERE id=? AND subject_type='user' AND subject_id=?
                      AND version=?
                    """,
                    (
                        deleted_at,
                        now,
                        user_id,
                        now,
                        entry_id,
                        user_id,
                        entry.version,
                    ),
                )
                if (cursor.rowcount or 0) != 1:
                    latest = await _get_owned_entry(
                        db,
                        user_id=user_id,
                        entry_id=entry_id,
                    )
                    if latest is None:
                        await db.rollback()
                        return False
                    raise MemoryVersionConflictError(_row_to_entry(latest))
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise


class ProjectMemoryStore:
    """M158-10 paradigm v2 · 跨 user 聚合的 project-level memory.

    复用同一个 sqlite memory_entries 表 (subject_type='project', subject_id=workspace_id).
    存的内容包括: high-impact sub_q patterns / typical satisfaction / 跨 user 投票统计.

    设计文档: docs/M158-paradigm-shift-lead-as-manager.md §6.1 project_memory_store schema.

    数据写入时机:
    - cron 每周聚合 user_memory.preferences (跨 user) → project_memory.patterns (M158-11)
    - clarification.resolved 时增量累计 pick_count (lightweight 实时更新)

    数据读取时机:
    - workspace-datarc-decouple (R8): gateway turn-start 调 get_datarc_default_project()
      → TurnRequest.active_datarc_project (datarc 工具 _ws 用). 当前唯一 live 的读路径.
    - (历史/未接线) M158-12 计划的 "Lead system prompt 注入 prefetch (patterns/satisfaction)"
      从未真正接线 — patterns / pick_count 仍 write-only, 待真用时再补 reader.

    隐私: project_memory 跨 user 聚合, 必须**匿名化** — 不存原 prompt 文本, 只存 cluster id /
    pattern 信号 (hashed prompt signature). 参考 M158 §6.4.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def initialize(self) -> None:
        """复用 user_memory schema (memory_entries), 仅 subject_type='project'."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_TABLE_SCHEMA)
            await _migrate_deleted_at(db)
            await _migrate_workspace_id(db)
            await _migrate_governance_columns(db)
            await _migrate_memory_item_columns(db)
            await _migrate_scoped_identity(db)
            await db.executescript(_INDEX_SCHEMA)
            await db.commit()

    async def list_for_workspace(self, workspace_id: str) -> list[MemoryEntry]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM memory_entries WHERE subject_type='project' AND subject_id=? AND deleted_at IS NULL ORDER BY kind, key",
                (workspace_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_entry(r) for r in rows]

    async def upsert(
        self,
        *,
        workspace_id: str,
        kind: str,  # "pattern" / "subq_template" / "typical_satisfaction"
        key: str,
        value: object,
        source: str = "aggregator",
    ) -> MemoryEntry:
        from datetime import datetime
        from uuid import uuid4

        now = datetime.now(tz=UTC).isoformat(timespec="seconds")
        entry_id = f"prj_{uuid4().hex[:12]}"
        val_json = json.dumps(value, ensure_ascii=False)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """
                INSERT INTO memory_entries
                    (id, subject_type, subject_id, kind, key, value,
                     locked, lock_reason, source, created_at, updated_at)
                VALUES (?, 'project', ?, ?, ?, ?, 0, NULL, ?, ?, ?)
                -- 记忆重做 批0（migration 0024）: 身份键带上作用域后，四列冲突目标不再
                -- 匹配任何唯一约束，SQLite 会直接报 "ON CONFLICT clause does not match
                -- any PRIMARY KEY or UNIQUE constraint"。冲突目标必须与
                -- idx_memory_entries_identity_scoped 逐字同形（含 COALESCE 表达式）。
                ON CONFLICT(subject_type, subject_id, kind, key, COALESCE(workspace_id, '*')) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    deleted_at = NULL
                """,
                (entry_id, workspace_id, kind, key, val_json, source, now, now),
            )
            await db.commit()
            async with db.execute(
                "SELECT * FROM memory_entries WHERE subject_type='project' AND subject_id=? AND kind=? AND key=?",
                (workspace_id, kind, key),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        return _row_to_entry(row)

    async def increment_counter(
        self,
        *,
        workspace_id: str,
        kind: str,
        key: str,
        delta: int = 1,
    ) -> int:
        """Read-modify-write 简易计数器, 用于 pick_count / skip_count 累加.
        竞态会丢失个别更新但 paradigm v2 数据不要求强一致, 后续 cron 重算修正.
        """
        existing = await self.list_for_workspace(workspace_id)
        cur_val = 0
        for e in existing:
            if e.kind == kind and e.key == key:
                try:
                    parsed = json.loads(e.value)
                    if isinstance(parsed, int):
                        cur_val = parsed
                    elif isinstance(parsed, dict) and "count" in parsed:
                        cur_val = int(parsed.get("count", 0))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
                break
        new_val = cur_val + delta
        await self.upsert(
            workspace_id=workspace_id,
            kind=kind,
            key=key,
            value={"count": new_val},
            source="counter",
        )
        return new_val

    async def get_datarc_default_project(self, workspace_id: str) -> str | None:
        """workspace-datarc-decouple (R8): the workspace's seeded/default datarc project
        id (kind='datarc_pref', key='default_project'), or None if unset.

        Mirrors ``UserMemoryStore.get_datarc_member_uid`` — a direct single-value read.
        Called at gateway turn-start to populate ``TurnRequest.active_datarc_project``,
        which ``ToolExecutionContext.resolve_datarc_project`` / the bi-tool ``_ws`` then
        prefer over ``workspace_id``. None → callers fall back to ``workspace_id``
        (== pre-decouple behavior, before migration seeds the default project)."""
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT value FROM memory_entries "
                "WHERE subject_type='project' AND subject_id=? "
                "AND kind='datarc_pref' AND key='default_project' "
                "AND deleted_at IS NULL",
                (workspace_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        if row is None or row[0] is None:
            return None
        try:
            val = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            val = row[0]
        return str(val) if val is not None else None

    async def get_datarc_last_used_project(self, workspace_id: str) -> str | None:
        """workspace-datarc-decouple (R8): the datarc project last SUCCESSFULLY queried in this
        workspace (kind='datarc_pref', key='last_used'), or None. Runtime-written by the bi
        tools; the gateway prefers it over the seeded ``default_project`` so a workspace stays
        on the project the user last actually used (project continuity across turns)."""
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT value FROM memory_entries "
                "WHERE subject_type='project' AND subject_id=? "
                "AND kind='datarc_pref' AND key='last_used' "
                "AND deleted_at IS NULL",
                (workspace_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        if row is None or row[0] is None:
            return None
        try:
            val = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            val = row[0]
        return str(val) if val is not None else None

    async def set_datarc_last_used_project(self, workspace_id: str, project: str) -> None:
        """workspace-datarc-decouple (R8): record the datarc project just successfully queried.
        Called fire-and-forget from the bi-tool success path — best-effort, never blocks the
        turn. Idempotent (upsert on the (workspace, datarc_pref, last_used) key)."""
        if not workspace_id or not project:
            return
        await self.upsert(
            workspace_id=workspace_id,
            kind="datarc_pref",
            key="last_used",
            value=project,
            source="bi_runtime",
        )


async def _get_owned_entry(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    entry_id: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        """
        SELECT *
        FROM memory_entries
        WHERE id=? AND subject_type='user' AND subject_id=?
        """,
        (entry_id, user_id),
    ) as cursor:
        return await cursor.fetchone()


def _now() -> str:
    from datetime import datetime

    return datetime.now(tz=UTC).isoformat(timespec="seconds")


async def _select_entry(
    db: aiosqlite.Connection,
    entry_id: str,
) -> MemoryEntry | None:
    async with db.execute(
        "SELECT * FROM memory_entries WHERE id=?",
        (entry_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_entry(row) if row is not None else None


def _row_to_entry(row: aiosqlite.Row) -> MemoryEntry:
    keys = set(row.keys())
    return MemoryEntry(
        id=row["id"],
        subject_type=row["subject_type"],
        subject_id=row["subject_id"],
        kind=row["kind"],
        key=row["key"],
        value=row["value"],
        locked=bool(row["locked"]),
        lock_reason=row["lock_reason"],
        source=row["source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"] if "deleted_at" in keys else None,
        workspace_id=row["workspace_id"] if "workspace_id" in keys else None,
        version=int(row["version"]) if "version" in keys else 1,
        memory_role=row["memory_role"] if "memory_role" in keys else MemoryRole.UNCLASSIFIED,
        value_schema=row["value_schema"] if "value_schema" in keys else None,
        memory_policy_version=int(row["memory_policy_version"]) if "memory_policy_version" in keys else 0,
        created_source_type=row["created_source_type"] if "created_source_type" in keys else None,
        created_source_turn_id=row["created_source_turn_id"] if "created_source_turn_id" in keys else None,
        created_source_candidate_id=row["created_source_candidate_id"] if "created_source_candidate_id" in keys else None,
        created_source_workspace_id=row["created_source_workspace_id"] if "created_source_workspace_id" in keys else None,
        last_modified_actor_type=row["last_modified_actor_type"] if "last_modified_actor_type" in keys else None,
        last_modified_actor_id=row["last_modified_actor_id"] if "last_modified_actor_id" in keys else None,
        last_modified_at=row["last_modified_at"] if "last_modified_at" in keys else None,
        memory_type=row["memory_type"] if "memory_type" in keys else None,
        confidence=row["confidence"] if "confidence" in keys else None,
        last_confirmed_at=row["last_confirmed_at"] if "last_confirmed_at" in keys else None,
        superseded_by=row["superseded_by"] if "superseded_by" in keys else None,
        previous_value=row["previous_value"] if "previous_value" in keys else None,
        previous_value_at=row["previous_value_at"] if "previous_value_at" in keys else None,
    )


async def _collect_reset_targets(
    db: aiosqlite.Connection,
    *,
    user_id: str,
) -> dict[str, Any]:
    from bowhead_core.memory_kind_policy import resolve_memory_policy

    async with db.execute(
        """
        SELECT *
        FROM memory_entries
        WHERE subject_type='user' AND subject_id=?
          AND memory_role=? AND memory_policy_version=?
          AND deleted_at IS NULL
        ORDER BY id
        """,
        (
            user_id,
            MemoryRole.PRODUCT_MEMORY,
            CURRENT_MEMORY_POLICY_VERSION,
        ),
    ) as cursor:
        candidate_entries = await cursor.fetchall()
    entries: list[aiosqlite.Row] = []
    locked_skipped = 0
    for row in candidate_entries:
        entry = _row_to_entry(row)
        policy = resolve_memory_policy(
            memory_role=entry.memory_role,
            memory_policy_version=entry.memory_policy_version,
            kind=entry.kind,
            value_schema=entry.value_schema,
            value=entry.parsed_value(),
        )
        if not policy.regular_delete_reset:
            continue
        if entry.locked:
            locked_skipped += 1
        else:
            entries.append(row)

    note = None
    if await _table_exists(db, "memory_notes"):
        async with db.execute(
            """
            SELECT *
            FROM memory_notes
            WHERE subject_type='user' AND subject_id=?
              AND deleted_at IS NULL AND content<>''
            """,
            (user_id,),
        ) as cursor:
            note = await cursor.fetchone()

    anchors: list[aiosqlite.Row] = []
    if note is not None and await _table_exists(db, "memory_note_anchors"):
        async with db.execute(
            """
            SELECT *
            FROM memory_note_anchors
            WHERE user_id=?
            ORDER BY id
            """,
            (user_id,),
        ) as cursor:
            anchors = list(await cursor.fetchall())

    pending_preserved = 0
    if await _table_exists(db, "memory_candidates"):
        async with db.execute(
            """
            SELECT COUNT(*)
            FROM memory_candidates
            WHERE user_id=? AND status='pending'
            """,
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        pending_preserved = int(row[0]) if row else 0

    return {
        "entries": entries,
        "note": note,
        "anchors": anchors,
        "locked_skipped": locked_skipped,
        "pending_preserved": pending_preserved,
    }


def _reset_counts(targets: dict[str, Any]) -> dict[str, int]:
    return {
        "notes": 1 if targets["note"] is not None else 0,
        "entries": len(targets["entries"]),
        "anchors": len(targets["anchors"]),
        "locked_skipped": int(targets["locked_skipped"]),
        "pending_preserved": int(targets["pending_preserved"]),
    }


def _note_row_lock_active(
    row: aiosqlite.Row,
    now: datetime,
) -> bool:
    value = row["editing_locked_until"]
    if not value:
        return False
    try:
        expiry = datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry > now


def _memory_entry_reset_payload(row: aiosqlite.Row) -> dict[str, Any]:
    fields = (
        "id",
        "kind",
        "key",
        "value",
        "locked",
        "lock_reason",
        "source",
        "created_at",
        "updated_at",
        "workspace_id",
        "memory_role",
        "value_schema",
        "memory_policy_version",
        "created_source_type",
        "created_source_turn_id",
        "created_source_candidate_id",
        "created_source_workspace_id",
        "last_modified_actor_type",
        "last_modified_actor_id",
        "last_modified_at",
    )
    payload = {field: row[field] for field in fields}
    payload["display_type"] = _reset_display_type(row)
    return payload


def _note_reset_payload(row: aiosqlite.Row) -> dict[str, Any]:
    fields = (
        "id",
        "content",
        "source",
        "source_turn_id",
        "created_at",
        "updated_at",
        "verification_anchor",
        "verification_status",
        "created_source_type",
        "created_source_turn_id",
        "created_source_candidate_id",
        "created_source_workspace_id",
        "last_modified_actor_type",
        "last_modified_actor_id",
        "last_modified_at",
    )
    payload = {field: row[field] for field in fields}
    payload["display_type"] = "background"
    return payload


def _anchor_reset_payload(row: aiosqlite.Row) -> dict[str, Any]:
    fields = (
        "id",
        "user_id",
        "bullet_text",
        "bullet_text_norm",
        "canvas_id",
        "section_id",
        "verifier_result_hash",
        "saved_at",
        "status",
        "invalidated_at",
        "invalidation_reason",
    )
    payload = {field: row[field] for field in fields}
    payload["display_type"] = "background_anchor"
    return payload


def _reset_display_type(row: aiosqlite.Row) -> str:
    from bowhead_core.memory_kind_policy import resolve_memory_policy

    entry = _row_to_entry(row)
    policy = resolve_memory_policy(
        memory_role=entry.memory_role,
        memory_policy_version=entry.memory_policy_version,
        kind=entry.kind,
        value_schema=entry.value_schema,
        value=entry.parsed_value(),
    )
    return policy.display_type or "unknown"


async def _insert_reset_item(
    db: aiosqlite.Connection,
    *,
    snapshot_id: str,
    restore_group_id: str,
    resource_type: str,
    resource_id: str,
    resource_version: int,
    deleted_version: int,
    payload: dict[str, Any],
) -> None:
    await db.execute(
        """
        INSERT INTO memory_reset_snapshot_items(
            snapshot_id, restore_group_id, resource_type, resource_id,
            display_type, resource_version, deleted_version, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            restore_group_id,
            resource_type,
            resource_id,
            payload.get("display_type"),
            resource_version,
            deleted_version,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )


async def _get_active_v3_reset_snapshot(
    db: aiosqlite.Connection,
    *,
    snapshot_id: str,
    user_id: str,
    now: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        """
        SELECT *
        FROM memory_reset_snapshots
        WHERE id=? AND user_id=? AND schema_version=3
          AND snapshot_type IN ('reset', 'item_delete')
          AND consumed_at IS NULL
          AND expires_at IS NOT NULL AND datetime(expires_at)>datetime(?)
        """,
        (snapshot_id, user_id, now),
    ) as cursor:
        return await cursor.fetchone()


async def _reset_snapshot_state_counts(
    db: aiosqlite.Connection,
    *,
    snapshot_id: str,
) -> tuple[dict[str, int], int]:
    state_counts = {
        "pending": 0,
        "restored": 0,
        "conflict": 0,
        "unavailable": 0,
        "permanently_deleted": 0,
    }
    async with db.execute(
        """
        SELECT restore_state, COUNT(*)
        FROM memory_reset_snapshot_items
        WHERE snapshot_id=?
        GROUP BY restore_state
        """,
        (snapshot_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    for row in rows:
        state = str(row[0])
        if state in state_counts:
            state_counts[state] = int(row[1])
    async with db.execute(
        """
        SELECT COUNT(*)
        FROM memory_reset_snapshot_items
        WHERE snapshot_id=? AND payload_json<>'{}'
        """,
        (snapshot_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return state_counts, int(row[0]) if row else 0


async def _governance_target(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    resource_type: str,
    resource_id: str,
) -> str | None:
    if resource_type == "memory_entry":
        query = """
            SELECT 1
            FROM memory_entries
            WHERE id=? AND subject_type='user' AND subject_id=?
              AND deleted_at IS NULL
        """
    elif resource_type == "memory_note":
        query = """
            SELECT 1
            FROM memory_notes
            WHERE id=? AND subject_type='user' AND subject_id=?
              AND deleted_at IS NULL
        """
    else:
        return None
    async with db.execute(query, (resource_id, user_id)) as cursor:
        exists = await cursor.fetchone() is not None
    return f"/settings/memory/?item={resource_id}" if exists else None


def _restore_group_unavailable_reason(
    decoded: list[tuple[aiosqlite.Row, dict[str, Any]]],
    *,
    accessible_space_ids: set[str],
) -> str | None:
    for item, payload in decoded:
        resource_type = str(item["resource_type"])
        if resource_type == "memory_entry":
            workspace_id = payload.get("workspace_id")
        else:
            workspace_id = None
        if workspace_id and workspace_id not in accessible_space_ids:
            return "space_unavailable"
    return None


async def _restore_group_conflict_reason(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    decoded: list[tuple[aiosqlite.Row, dict[str, Any]]],
) -> str | None:
    for item, payload in decoded:
        resource_type = str(item["resource_type"])
        resource_id = str(item["resource_id"])
        deleted_version = int(item["deleted_version"])
        if resource_type == "memory_entry":
            query = """
                SELECT version, deleted_at
                FROM memory_entries
                WHERE id=? AND subject_type='user' AND subject_id=?
            """
            params: tuple[Any, ...] = (resource_id, user_id)
        elif resource_type == "memory_note":
            query = """
                SELECT version, deleted_at
                FROM memory_notes
                WHERE id=? AND subject_type='user' AND subject_id=?
            """
            params = (resource_id, user_id)
        elif resource_type == "note_anchor":
            async with db.execute(
                """
                SELECT 1
                FROM memory_note_anchors
                WHERE id=? OR (
                    user_id=? AND canvas_id=? AND section_id=?
                    AND bullet_text_norm=?
                )
                LIMIT 1
                """,
                (
                    resource_id,
                    user_id,
                    payload["canvas_id"],
                    payload["section_id"],
                    payload["bullet_text_norm"],
                ),
            ) as cursor:
                if await cursor.fetchone() is not None:
                    return "newer_version_exists"
            continue
        else:
            return "unsupported_resource_type"
        async with db.execute(query, params) as cursor:
            current = await cursor.fetchone()
        if current is None or current["deleted_at"] is None or int(current["version"]) != deleted_version:
            return "newer_version_exists"
    return None


async def _restore_snapshot_item(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    item: aiosqlite.Row,
    payload: dict[str, Any],
    now: str,
) -> None:
    resource_type = str(item["resource_type"])
    resource_id = str(item["resource_id"])
    deleted_version = int(item["deleted_version"])
    if resource_type == "memory_entry":
        await db.execute(
            """
            UPDATE memory_entries
            SET kind=?, key=?, value=?, locked=?, lock_reason=?, source=?,
                created_at=?, updated_at=?, deleted_at=NULL, workspace_id=?,
                version=version+1, memory_role=?, value_schema=?,
                memory_policy_version=?, created_source_type=?,
                created_source_turn_id=?, created_source_candidate_id=?,
                created_source_workspace_id=?, last_modified_actor_type=?,
                last_modified_actor_id=?, last_modified_at=?
            WHERE id=? AND subject_type='user' AND subject_id=?
              AND version=? AND deleted_at IS NOT NULL
            """,
            (
                payload["kind"],
                payload["key"],
                payload["value"],
                payload["locked"],
                payload["lock_reason"],
                payload["source"],
                payload["created_at"],
                payload["updated_at"],
                payload["workspace_id"],
                payload["memory_role"],
                payload["value_schema"],
                payload["memory_policy_version"],
                payload["created_source_type"],
                payload["created_source_turn_id"],
                payload["created_source_candidate_id"],
                payload["created_source_workspace_id"],
                payload["last_modified_actor_type"],
                payload["last_modified_actor_id"],
                payload["last_modified_at"],
                resource_id,
                user_id,
                deleted_version,
            ),
        )
    elif resource_type == "memory_note":
        await db.execute(
            """
            UPDATE memory_notes
            SET content=?, source=?, source_turn_id=?, created_at=?,
                updated_at=?, deleted_at=NULL, verification_anchor=?,
                verification_status=?, version=version+1,
                created_source_type=?, created_source_turn_id=?,
                created_source_candidate_id=?,
                created_source_workspace_id=?, last_modified_actor_type=?,
                last_modified_actor_id=?, last_modified_at=?,
                editing_locked_until=NULL, editing_lock_token_hash=NULL,
                editing_lock_owner_type=NULL
            WHERE id=? AND subject_type='user' AND subject_id=?
              AND version=? AND deleted_at IS NOT NULL
            """,
            (
                payload["content"],
                payload["source"],
                payload["source_turn_id"],
                payload["created_at"],
                payload["updated_at"],
                payload["verification_anchor"],
                payload["verification_status"],
                payload["created_source_type"],
                payload["created_source_turn_id"],
                payload["created_source_candidate_id"],
                payload["created_source_workspace_id"],
                payload["last_modified_actor_type"],
                payload["last_modified_actor_id"],
                payload["last_modified_at"],
                resource_id,
                user_id,
                deleted_version,
            ),
        )
    elif resource_type == "note_anchor":
        await db.execute(
            """
            INSERT INTO memory_note_anchors(
                id, user_id, bullet_text, bullet_text_norm, canvas_id,
                section_id, verifier_result_hash, saved_at, status,
                invalidated_at, invalidation_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resource_id,
                user_id,
                payload["bullet_text"],
                payload["bullet_text_norm"],
                payload["canvas_id"],
                payload["section_id"],
                payload["verifier_result_hash"],
                payload["saved_at"],
                payload["status"],
                payload["invalidated_at"],
                payload["invalidation_reason"],
            ),
        )
    else:
        raise RuntimeError(f"unsupported reset resource: {resource_type}")


async def _mark_restore_group(
    db: aiosqlite.Connection,
    *,
    snapshot_id: str,
    group_items: list[aiosqlite.Row],
    state: str,
    error: str | None,
    restored_at: str | None = None,
    scrub_payload: bool = False,
) -> None:
    for item in group_items:
        await db.execute(
            """
            UPDATE memory_reset_snapshot_items
            SET restore_state=?, restore_error=?, restored_at=?,
                payload_json=CASE WHEN ? THEN '{}' ELSE payload_json END
            WHERE snapshot_id=? AND resource_type=? AND resource_id=?
            """,
            (
                state,
                error,
                restored_at,
                1 if scrub_payload else 0,
                snapshot_id,
                item["resource_type"],
                item["resource_id"],
            ),
        )


def _restore_count_bucket(resource_type: str) -> str:
    return {
        "memory_entry": "entries",
        "memory_note": "notes",
        "note_anchor": "anchors",
    }[resource_type]


async def _get_memory_mutation_result(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    idempotency_key: str,
    request_fingerprint: str,
) -> dict[str, Any] | None:
    async with db.execute(
        """
        SELECT request_fingerprint, result_json
        FROM memory_mutation_idempotency
        WHERE user_id=? AND idempotency_key=?
        """,
        (user_id, idempotency_key),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    if str(row["request_fingerprint"]) != request_fingerprint:
        raise MemoryIdempotencyConflictError
    result = json.loads(str(row["result_json"]))
    if not isinstance(result, dict):
        raise RuntimeError("invalid memory mutation idempotency result")
    return result


async def _record_memory_mutation_result(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    idempotency_key: str,
    request_fingerprint: str,
    result: dict[str, Any],
    now: str,
) -> None:
    await db.execute(
        """
        INSERT INTO memory_mutation_idempotency(
            user_id, idempotency_key, request_fingerprint,
            result_json, created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            user_id,
            idempotency_key,
            request_fingerprint,
            json.dumps(result, sort_keys=True),
            now,
        ),
    )


def _memory_reset_result_from_json(
    value: dict[str, Any],
) -> MemoryResetV3Result:
    counts = value.get("counts")
    if not isinstance(counts, dict):
        raise RuntimeError("invalid cached reset result")
    return MemoryResetV3Result(
        snapshot_id=str(value["snapshot_id"]),
        expires_at=str(value["expires_at"]),
        counts={str(key): int(count) for key, count in counts.items()},
    )


def _memory_restore_result_from_json(
    value: dict[str, Any],
) -> MemoryRestoreV3Result:
    restored_counts = value.get("restored_counts")
    unavailable_counts = value.get("unavailable_counts")
    conflicts = value.get("conflicts")
    if not isinstance(restored_counts, dict) or not isinstance(unavailable_counts, dict) or not isinstance(conflicts, list):
        raise RuntimeError("invalid cached reset restore result")
    return MemoryRestoreV3Result(
        snapshot_id=str(value["snapshot_id"]),
        status=str(value["status"]),
        restored_counts={str(key): int(count) for key, count in restored_counts.items()},
        conflicts=[{str(key): str(detail) for key, detail in conflict.items()} for conflict in conflicts if isinstance(conflict, dict)],
        unavailable_counts={str(key): int(count) for key, count in unavailable_counts.items()},
        remaining_recoverable=int(value["remaining_recoverable"]),
    )


def _validate_idempotency_key(value: str) -> None:
    if len(value) > 36:
        raise ValueError("Idempotency-Key must be a canonical UUID")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("Idempotency-Key must be a canonical UUID")


async def _table_exists(
    db: aiosqlite.Connection,
    table_name: str,
) -> bool:
    async with db.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type='table' AND name=?
        """,
        (table_name,),
    ) as cursor:
        return await cursor.fetchone() is not None


async def _migrate_deleted_at(db: aiosqlite.Connection) -> None:
    """添加 deleted_at 列到旧 DB (V3 PRD-B 软删 migration). Idempotent."""
    try:  # noqa: SIM105
        await db.execute("ALTER TABLE memory_entries ADD COLUMN deleted_at TEXT")
    except aiosqlite.OperationalError:
        pass  # column already exists


async def _migrate_workspace_id(db: aiosqlite.Connection) -> None:
    """memory-revive S2: 加 workspace_id 列（Bowhead Space ws_*，None=全局）. Idempotent.

    **必须两个 store 都调**：memory_entries 同表被 UserMemoryStore 与 ProjectMemoryStore
    共用（各自 initialize），只在一处迁移会因初始化顺序导致列态不一致（review M1）。
    """
    try:  # noqa: SIM105
        await db.execute("ALTER TABLE memory_entries ADD COLUMN workspace_id TEXT")
    except aiosqlite.OperationalError:
        pass  # column already exists


async def _migrate_governance_columns(db: aiosqlite.Connection) -> None:
    columns = (
        "version INTEGER NOT NULL DEFAULT 1",
        "memory_role TEXT NOT NULL DEFAULT 'unclassified'",
        "value_schema TEXT",
        "memory_policy_version INTEGER NOT NULL DEFAULT 0",
        "created_source_type TEXT",
        "created_source_turn_id TEXT",
        "created_source_candidate_id TEXT",
        "created_source_workspace_id TEXT",
        "last_modified_actor_type TEXT",
        "last_modified_actor_id TEXT",
        "last_modified_at TEXT",
    )
    for definition in columns:
        with suppress(aiosqlite.OperationalError):
            await db.execute(f"ALTER TABLE memory_entries ADD COLUMN {definition}")


#: Complete post-0024 `memory_entries` column order (copy-migrate copies columns in this order).
#: Identical to `_CANONICAL_COLUMNS` in `migrations/versions/0024_memory_item_metadata.py`;
#: the revision freezes a copy because it is immutable; this is the runtime source of truth. Changes must update both.
_MEMORY_ENTRY_COLUMNS: tuple[str, ...] = (
    "id",
    "subject_type",
    "subject_id",
    "kind",
    "key",
    "value",
    "locked",
    "lock_reason",
    "source",
    "created_at",
    "updated_at",
    "deleted_at",
    "workspace_id",
    "version",
    "memory_role",
    "value_schema",
    "memory_policy_version",
    "created_source_type",
    "created_source_turn_id",
    "created_source_candidate_id",
    "created_source_workspace_id",
    "last_modified_actor_type",
    "last_modified_actor_id",
    "last_modified_at",
    "memory_type",
    "confidence",
    "last_confirmed_at",
    "superseded_by",
    "previous_value",
    "previous_value_at",
)

_LEGACY_IDENTITY_COLUMNS = ["subject_type", "subject_id", "kind", "key"]


async def _migrate_memory_item_columns(db: aiosqlite.Connection) -> None:
    """记忆重做 批0: per-条元数据列（migration 0024 的 store 侧镜像）. Idempotent."""
    columns = (
        "memory_type TEXT",
        "confidence TEXT",
        "last_confirmed_at TEXT",
        "superseded_by TEXT",
        "previous_value TEXT",
        "previous_value_at TEXT",
    )
    for definition in columns:
        with suppress(aiosqlite.OperationalError):
            await db.execute(f"ALTER TABLE memory_entries ADD COLUMN {definition}")


async def _has_legacy_identity_unique(db: aiosqlite.Connection) -> bool:
    """旧形状 = 存在一个由表级 `UNIQUE(...)` 生成的、恰好覆盖那四列的自动索引。

    判据走 `PRAGMA index_list` + `index_info` 而不是 grep 建表 SQL：后者对空白/换行敏感，
    而这段 DDL 在 baseline、`_TABLE_SCHEMA`、revision 0024 三处各有一份写法。
    表达式索引在 `index_info` 里那一列的名字是 NULL ⇒ 新形状不会被误判成旧形状。
    """
    async with db.execute("PRAGMA index_list(memory_entries)") as cur:
        indexes = await cur.fetchall()
    for row in indexes:
        name, unique, origin = str(row[1]), int(row[2]), str(row[3])
        if not unique or origin != "u":
            continue
        async with db.execute(f'PRAGMA index_info("{name}")') as cur:
            info = await cur.fetchall()
        if [r[2] for r in info] == _LEGACY_IDENTITY_COLUMNS:
            return True
    return False


async def _migrate_scoped_identity(db: aiosqlite.Connection) -> None:
    """记忆重做 批0: 身份键带上作用域（migration 0024 的 store 侧镜像）. Idempotent.

    `UNIQUE(subject_type, subject_id, kind, key)`
      → `UNIQUE(subject_type, subject_id, kind, key, COALESCE(workspace_id,'*'))`。
    SQLite 不能原地改约束，也装不下表达式形式的表级 UNIQUE ⇒ copy-migrate 到不带内联
    UNIQUE 的新表，作用域身份改由 `_INDEX_SCHEMA` 里的表达式唯一索引承载。

    新键严格细于旧键 ⇒ 存量数据不可能在新索引上撞唯一。索引重建交给紧随其后的
    `_INDEX_SCHEMA`（全是 IF NOT EXISTS），本函数只负责换表。
    """
    async with db.execute("PRAGMA table_info(memory_entries)") as cur:
        table_info = await cur.fetchall()
    if not table_info:  # Table does not exist yet (caller immediately creates it with executescript)
        return
    if not await _has_legacy_identity_unique(db):
        return

    present = {str(row[1]) for row in table_info}
    missing = [name for name in _MEMORY_ENTRY_COLUMNS if name not in present]
    if missing:
        # Caller must finish all ALTER migrations first; missing columns indicate broken ordering. Do not silently lose data.
        msg = f"memory_entries missing columns before copy-migrate: {missing}"
        raise RuntimeError(msg)
    extra = [name for name in present if name not in _MEMORY_ENTRY_COLUMNS]
    if extra:
        msg = f"memory_entries has unexpected columns, refusing copy-migrate: {sorted(extra)}"
        raise RuntimeError(msg)

    column_list = ", ".join(_MEMORY_ENTRY_COLUMNS)
    async with db.execute("SELECT COUNT(*) FROM memory_entries") as cur:
        before = (await cur.fetchone())[0]
    await db.execute("DROP TABLE IF EXISTS memory_entries_0024_new")
    await db.executescript(_SCOPED_IDENTITY_NEW_TABLE)
    await db.execute(
        f"INSERT INTO memory_entries_0024_new ({column_list}) SELECT {column_list} FROM memory_entries",
    )
    async with db.execute("SELECT COUNT(*) FROM memory_entries_0024_new") as cur:
        after = (await cur.fetchone())[0]
    if before != after:
        msg = f"memory_entries copy-migrate row count mismatch: {before} → {after}"
        raise RuntimeError(msg)
    await db.execute("DROP TABLE memory_entries")
    await db.execute("ALTER TABLE memory_entries_0024_new RENAME TO memory_entries")


#: Intermediate copy-migrate table. Column order/types match memory_entries in `_TABLE_SCHEMA`, excluding
#: inline UNIQUE (scoped identity is enforced by `_INDEX_SCHEMA`'s unique expression index).
_SCOPED_IDENTITY_NEW_TABLE = """
CREATE TABLE memory_entries_0024_new (
    id              TEXT PRIMARY KEY,
    subject_type    TEXT NOT NULL,
    subject_id      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    locked          INTEGER NOT NULL DEFAULT 0,
    lock_reason     TEXT,
    source          TEXT NOT NULL DEFAULT 'user_explicit',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    deleted_at      TEXT,
    workspace_id    TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    memory_role     TEXT NOT NULL DEFAULT 'unclassified',
    value_schema    TEXT,
    memory_policy_version INTEGER NOT NULL DEFAULT 0,
    created_source_type TEXT,
    created_source_turn_id TEXT,
    created_source_candidate_id TEXT,
    created_source_workspace_id TEXT,
    last_modified_actor_type TEXT,
    last_modified_actor_id TEXT,
    last_modified_at TEXT,
    memory_type     TEXT,
    confidence      TEXT,
    last_confirmed_at TEXT,
    superseded_by   TEXT,
    previous_value  TEXT,
    previous_value_at TEXT
);
"""


async def _migrate_reset_v3(db: aiosqlite.Connection) -> None:
    columns = (
        "schema_version INTEGER NOT NULL DEFAULT 1",
        "snapshot_type TEXT NOT NULL DEFAULT 'reset'",
        "target_resource_type TEXT",
        "target_resource_id TEXT",
        "expires_at TEXT",
        "consumed_at TEXT",
        "counts_json TEXT NOT NULL DEFAULT '{}'",
    )
    for definition in columns:
        with suppress(aiosqlite.OperationalError):
            await db.execute(
                f"ALTER TABLE memory_reset_snapshots ADD COLUMN {definition}",
            )


async def _migrate_reset_item_v3(db: aiosqlite.Connection) -> None:
    with suppress(aiosqlite.OperationalError):
        await db.execute(
            """
            ALTER TABLE memory_reset_snapshot_items
            ADD COLUMN display_type TEXT
            """,
        )


async def _mark_empty_policy_cutover(db: aiosqlite.Connection) -> None:
    """Fresh installations have no legacy rows and can atomically start on v1."""

    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    await db.execute(
        """
        INSERT INTO memory_migration_state(key, value, updated_at)
        SELECT ?, 'complete', ?
        WHERE NOT EXISTS (
            SELECT 1 FROM memory_entries WHERE subject_type='user'
        )
        ON CONFLICT(key) DO NOTHING
        """,
        (MEMORY_POLICY_CUTOVER_KEY, now),
    )


def _validate_memory_role(role: MemoryRole | str | None) -> str | None:
    if role is None:
        return None
    try:
        return MemoryRole(role).value
    except ValueError as exc:
        raise ValueError(f"invalid memory role: {role}") from exc


def _value_schema_for(value: object) -> str:
    if isinstance(value, bool):
        return "boolean_v1"
    if isinstance(value, str):
        return "text_v1"
    if isinstance(value, (int, float)):
        return "number_v1"
    if isinstance(value, list):
        return "list_v1"
    if isinstance(value, dict):
        return "object_v1"
    if value is None:
        return "null_v1"
    raise ValueError(f"unsupported memory value type: {type(value).__name__}")


def summarize_for_prompt(entries: list[MemoryEntry], *, with_ids: bool = False) -> str:
    """把 user memory 渲染成 Lead system prompt 的可读段.

    功能已正式发布；权限、作用域和存储就绪校验仍须执行。

        默认 `False`：Dreaming 的 `_existing_memory_digest` 也调本函数，它喂的是蒸馏 prompt，
        不需要 id，也不该因为记忆闸的开关改变形状。
    """
    if not entries:
        return ""

    by_kind: dict[str, list[MemoryEntry]] = {}
    for e in entries:
        by_kind.setdefault(e.kind, []).append(e)

    lines: list[str] = []
    for kind, items in sorted(by_kind.items()):
        if kind == "datarc_binding":
            continue  # Exclude from prompt (sensitive identifiers)
        pretty = {
            "preference": "User preferences",
            "constraint": "User constraints",
        }.get(kind, kind)
        lines.append(f"- {pretty}:")
        for e in items:
            try:
                v = json.loads(e.value)
            except json.JSONDecodeError:
                v = e.value
            v_str = json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v)
            marker = f"[{e.id}] " if with_ids else ""
            lines.append(f"    · {marker}{e.key} = {v_str}")
    return "\n".join(lines)
