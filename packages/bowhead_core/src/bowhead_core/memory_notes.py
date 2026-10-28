"""V3.5 PRD-C · MemoryNotesStore — single free-text segment per user.

Schema: memory_notes(id, subject_type, subject_id, content, source,
source_turn_id, editing_locked_until, created_at, updated_at, deleted_at).

UNIQUE(subject_type, subject_id) — each user has at most one row.

Methods:
  - initialize() — TABLE → ALTER (defensive) → INDEX three-step migration
  - get(user_id, *, include_trashed=False) → MemoryNote | None
  - upsert_content(user_id, content, *, source) — INSERT OR REPLACE clears deleted_at
  - append(user_id, bullet, *, pinned, source, source_turn_id) — bullet append
  - delete_bullet(user_id, bullet_index) — index in bullet/pinned set (skip freetext)
  - acquire_lock(user_id, *, ttl_seconds=300) — CAS UPDATE; INSERT OR IGNORE first
  - release_lock(user_id) — clear editing_locked_until
  - is_lock_active(user_id) — bool: lock held by anyone
  - soft_delete(user_id) — for /memory/reset extend
  - hard_delete_old_trash(*, max_age_days=30) — cron purge
  - hard_delete_user(user_id) — removed-user cascade
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.memory_privacy import (
    ensure_memory_write_allowed,
    is_memory_read_hidden,
)

logger = logging.getLogger(__name__)

# 3000 char hard limit per PRD-C §1.3
MAX_CONTENT_CHARS = 3000
# 500 char single-bullet append cap (independent of total)
MAX_BULLET_CHARS = 500


_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_notes (
    id                    TEXT PRIMARY KEY,
    subject_type          TEXT NOT NULL,
    subject_id            TEXT NOT NULL,
    content               TEXT NOT NULL,
    source                TEXT NOT NULL,
    source_turn_id        TEXT,
    editing_locked_until  TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    deleted_at            TEXT,
    verification_anchor   TEXT,                                -- V4 PRD-D-5 (JSON)
    verification_status   TEXT NOT NULL DEFAULT 'valid',       -- V4 PRD-D-5
    version               INTEGER NOT NULL DEFAULT 1,
    created_source_type   TEXT,
    created_source_turn_id TEXT,
    created_source_candidate_id TEXT,
    created_source_workspace_id TEXT,
    last_modified_actor_type TEXT,
    last_modified_actor_id TEXT,
    last_modified_at      TEXT,
    editing_lock_token_hash TEXT,
    editing_lock_owner_type TEXT,
    UNIQUE(subject_type, subject_id)
);
"""

_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_memory_notes_subject
    ON memory_notes(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_memory_notes_deleted_at
    ON memory_notes(subject_type, subject_id, deleted_at);
"""

# V4 PRD-D-5: per-row verification_anchor + verification_status. Per-row
# (not per-bullet) is V4 limitation — UI shows row-level header anchor when set,
# Sprint 2+ adds per-bullet structure. See V4-sprint-1-tech-design.md §4.5.
_VALID_VERIFICATION_STATUS = ("valid", "stale", "unknown")


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@dataclass
class MemoryNote:
    id: str
    subject_type: str
    subject_id: str
    content: str
    source: str
    source_turn_id: str | None
    editing_locked_until: str | None
    created_at: str
    updated_at: str
    deleted_at: str | None = None
    verification_anchor: dict[str, Any] | None = None  # V4 PRD-D-5
    verification_status: str = "valid"  # V4 PRD-D-5
    version: int = 1
    created_source_type: str | None = None
    created_source_turn_id: str | None = None
    created_source_candidate_id: str | None = None
    created_source_workspace_id: str | None = None
    last_modified_actor_type: str | None = None
    last_modified_actor_id: str | None = None
    last_modified_at: str | None = None


@dataclass(frozen=True)
class NotesEditLease:
    edit_token: str
    version: int
    expires_at: str


# ─── exceptions ────────────────────────────────────────


class NotesLockedError(Exception):
    """Editing lock held by another device. Includes lock_expires_at."""

    def __init__(self, lock_expires_at: str) -> None:
        super().__init__(f"locked until {lock_expires_at}")
        self.lock_expires_at = lock_expires_at


class NotesCharLimitError(ValueError):
    """Content > MAX_CONTENT_CHARS."""

    def __init__(self, length: int, limit: int = MAX_CONTENT_CHARS) -> None:
        super().__init__(f"{length} > {limit}")
        self.length = length
        self.limit = limit


class NotesEditTokenError(PermissionError):
    """The supplied About-you edit token is absent, expired, or not the owner."""


class NotesVersionConflictError(RuntimeError):
    """An About-you mutation used a stale public version."""

    def __init__(self, current: MemoryNote | None, current_version: int) -> None:
        super().__init__("memory notes version conflict")
        self.current = current
        self.current_version = current_version


# ─── store ─────────────────────────────────────────────


class MemoryNotesStore:
    """Async sqlite-backed single-segment-per-user notes store."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def initialize(self) -> None:
        """三步 migration: TABLE → ALTER (legacy DBs) → INDEX.
        PRD-B 教训: 索引引用新列前先 ALTER 加列, 否则在老 DB 上 CREATE INDEX 失败.
        V4 PRD-D-5: 加 verification_anchor + verification_status. 老 DB (PRD-C ship)
        通过 ALTER 加列, 新 DB 在 CREATE TABLE 已有 — try/except OperationalError 区别.
        """
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_TABLE_SCHEMA)
            # V4 PRD-D-5 ALTER for legacy DBs (PRD-C ship pre V4)
            for stmt in (
                "ALTER TABLE memory_notes ADD COLUMN verification_anchor TEXT",
                "ALTER TABLE memory_notes ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'valid'",
                "ALTER TABLE memory_notes ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE memory_notes ADD COLUMN created_source_type TEXT",
                "ALTER TABLE memory_notes ADD COLUMN created_source_turn_id TEXT",
                "ALTER TABLE memory_notes ADD COLUMN created_source_candidate_id TEXT",
                "ALTER TABLE memory_notes ADD COLUMN created_source_workspace_id TEXT",
                "ALTER TABLE memory_notes ADD COLUMN last_modified_actor_type TEXT",
                "ALTER TABLE memory_notes ADD COLUMN last_modified_actor_id TEXT",
                "ALTER TABLE memory_notes ADD COLUMN last_modified_at TEXT",
                "ALTER TABLE memory_notes ADD COLUMN editing_lock_token_hash TEXT",
                "ALTER TABLE memory_notes ADD COLUMN editing_lock_owner_type TEXT",
            ):
                try:  # noqa: SIM105
                    await db.execute(stmt)
                except aiosqlite.OperationalError:
                    pass  # column already exists (new DB)
            await db.executescript(_INDEX_SCHEMA)
            await db.commit()

    async def get(
        self,
        *,
        user_id: str,
        include_trashed: bool = False,
    ) -> MemoryNote | None:
        sql = "SELECT * FROM memory_notes WHERE subject_type='user' AND subject_id=?"
        if not include_trashed:
            sql += " AND deleted_at IS NULL"
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if await is_memory_read_hidden(db, user_id=user_id):
                return None
            async with db.execute(sql, (user_id,)) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        # NOTE: empty-content placeholder rows (created by acquire_lock then never
        # used) are returned as-is, NOT skipped — callers see a note with content="".
        # (The lock-aware remember path keys on is_lock_active, not on this being None.)
        return _row_to_note(row)

    async def upsert_content(
        self,
        *,
        user_id: str,
        content: str,
        source: str = "user_explicit",
        source_turn_id: str | None = None,
    ) -> MemoryNote:
        """Legacy/background whole-content write that respects any active edit lease."""

        if len(content) > MAX_CONTENT_CHARS:
            raise NotesCharLimitError(len(content))
        now = _now()
        note_id = f"note_{uuid4().hex[:12]}"
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await ensure_memory_write_allowed(
                    db,
                    user_id=user_id,
                )
                existing = await _get_note_row(db, user_id=user_id)
                _raise_if_row_locked(existing)
                await db.execute(
                    "INSERT INTO memory_notes "
                    "(id, subject_type, subject_id, content, source, source_turn_id, "
                    "created_at, updated_at, created_source_type, created_source_turn_id, "
                    "last_modified_actor_type, last_modified_at) "
                    "VALUES (?, 'user', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(subject_type, subject_id) DO UPDATE SET "
                    "content=excluded.content, source=excluded.source, "
                    "source_turn_id=excluded.source_turn_id, "
                    "updated_at=excluded.updated_at, deleted_at=NULL, "
                    "version=version+1, "
                    "last_modified_actor_type=excluded.last_modified_actor_type, "
                    "last_modified_at=excluded.last_modified_at",
                    (
                        note_id,
                        user_id,
                        content,
                        source,
                        source_turn_id,
                        now,
                        now,
                        source,
                        source_turn_id,
                        source,
                        now,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        n = await self.get(user_id=user_id)
        assert n is not None
        return n

    async def append(
        self,
        *,
        user_id: str,
        bullet: str,
        pinned: bool = False,
        source: str = "user_explicit",
        source_turn_id: str | None = None,
        ignore_lock: bool = False,
    ) -> MemoryNote:
        """Append a bullet atomically; ``ignore_lock`` is a compatibility no-op."""

        if not bullet or not bullet.strip():
            raise ValueError("bullet cannot be empty")
        bullet_trim = bullet.strip().replace("\n", " ")
        if len(bullet_trim) > MAX_BULLET_CHARS:
            raise ValueError(
                f"bullet > {MAX_BULLET_CHARS} char (per-bullet cap)",
            )
        _ = ignore_lock
        prefix = "📌 " if pinned else "• "
        new_line = prefix + bullet_trim
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await ensure_memory_write_allowed(
                    db,
                    user_id=user_id,
                )
                existing = await _get_note_row(db, user_id=user_id)
                _raise_if_row_locked(existing)
                existing_content = str(existing["content"] or "") if existing is not None and existing["deleted_at"] is None else ""
                new_content = f"{existing_content.rstrip()}\n{new_line}".lstrip("\n")
                if len(new_content) > MAX_CONTENT_CHARS:
                    raise NotesCharLimitError(len(new_content))
                await db.execute(
                    """
                    INSERT INTO memory_notes(
                        id, subject_type, subject_id, content, source, source_turn_id,
                        created_at, updated_at, version,
                        created_source_type, created_source_turn_id,
                        last_modified_actor_type, last_modified_at
                    )
                    VALUES (?, 'user', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    ON CONFLICT(subject_type, subject_id) DO UPDATE SET
                        content=excluded.content,
                        source=excluded.source,
                        source_turn_id=excluded.source_turn_id,
                        updated_at=excluded.updated_at,
                        deleted_at=NULL,
                        version=version+1,
                        last_modified_actor_type=excluded.last_modified_actor_type,
                        last_modified_at=excluded.last_modified_at
                    """,
                    (
                        f"note_{uuid4().hex[:12]}",
                        user_id,
                        new_content,
                        source,
                        source_turn_id,
                        now,
                        now,
                        source,
                        source_turn_id,
                        source,
                        now,
                    ),
                )
                updated = await _get_note_row(db, user_id=user_id)
                assert updated is not None
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _row_to_note(updated)

    async def delete_bullet(
        self,
        *,
        user_id: str,
        bullet_index: int,
        ignore_lock: bool = False,
    ) -> MemoryNote:
        """Legacy bullet delete; active edit leases cannot be bypassed."""

        if bullet_index < 0:
            raise IndexError(f"bullet_index {bullet_index} out of range")
        _ = ignore_lock
        existing = await self.get(user_id=user_id)
        if existing is None or not existing.content:
            raise IndexError(f"bullet_index {bullet_index} out of range (no content)")
        new_content = _delete_bullet_at_index(existing.content, bullet_index)
        return await self.upsert_content(
            user_id=user_id,
            content=new_content,
            source=existing.source,
            source_turn_id=existing.source_turn_id,
        )

    async def delete_bullet_with_edit(
        self,
        *,
        user_id: str,
        bullet_index: int,
        expected_version: int,
        edit_token: str,
    ) -> MemoryNote:
        """Delete a bullet through the same owner lease and CAS as full edits."""

        if bullet_index < 0:
            raise IndexError(f"bullet_index {bullet_index} out of range")
        existing = await self.get(user_id=user_id)
        if existing is None or not existing.content:
            raise IndexError(f"bullet_index {bullet_index} out of range (no content)")
        new_content = _delete_bullet_at_index(existing.content, bullet_index)
        return await self.commit_edit(
            user_id=user_id,
            content=new_content,
            expected_version=expected_version,
            edit_token=edit_token,
        )

    async def acquire_lock(
        self,
        *,
        user_id: str,
        ttl_seconds: int = 300,
    ) -> bool:
        """Acquire a tokenless legacy lease used only by internal compatibility tests.

        Returns True if lock acquired (or extended under an expired lock).
        Returns False if another active lock held.

        New users (no row): INSERT OR IGNORE creates placeholder, then UPDATE acquires.
        """
        now = _now()
        token_hash = _hash_edit_token(secrets.token_urlsafe(32))
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
            )
            await db.execute(
                "INSERT OR IGNORE INTO memory_notes "
                "(id, subject_type, subject_id, content, source, created_at, updated_at, version) "
                "VALUES (?, 'user', ?, '', 'user_explicit', ?, ?, 0)",
                (f"note_{uuid4().hex[:12]}", user_id, now, now),
            )
            cur = await db.execute(
                "UPDATE memory_notes "
                "SET editing_locked_until=datetime('now', ?), "
                "editing_lock_token_hash=?, editing_lock_owner_type='legacy' "
                "WHERE subject_type='user' AND subject_id=? "
                "AND (editing_locked_until IS NULL "
                "     OR datetime(editing_locked_until) < datetime('now'))",
                (f"+{int(ttl_seconds)} seconds", token_hash, user_id),
            )
            await db.commit()
            return (cur.rowcount or 0) > 0

    async def release_lock(
        self,
        *,
        user_id: str,
        edit_token: str | None = None,
    ) -> bool:
        """Release only the matching owner token, or a tokenless legacy lease."""

        if edit_token:
            predicate = "editing_lock_token_hash=?"
            predicate_value = _hash_edit_token(edit_token)
        else:
            predicate = "editing_lock_owner_type='legacy'"
            predicate_value = None
        async with open_shared_ctx(self._db_path) as db:
            params: list[str] = [user_id]
            if predicate_value is not None:
                params.append(predicate_value)
            cursor = await db.execute(
                "UPDATE memory_notes "
                "SET editing_locked_until=NULL, editing_lock_token_hash=NULL, "
                "editing_lock_owner_type=NULL "
                "WHERE subject_type='user' AND subject_id=? "
                f"AND {predicate}",
                tuple(params),
            )
            await db.commit()
            return (cursor.rowcount or 0) > 0

    async def begin_edit_session(
        self,
        *,
        user_id: str,
        expected_version: int,
        edit_token: str | None = None,
        ttl_seconds: int = 300,
    ) -> NotesEditLease:
        """Acquire or renew the user-owned About-you edit lease."""

        if expected_version < 0:
            raise ValueError("expected_version must be non-negative")
        if not 1 <= ttl_seconds <= 900:
            raise ValueError("ttl_seconds must be between 1 and 900")
        token = edit_token or secrets.token_urlsafe(32)
        token_hash = _hash_edit_token(token)
        now = datetime.now(tz=UTC)
        now_iso = now.isoformat(timespec="seconds")
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat(
            timespec="seconds",
        )

        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await ensure_memory_write_allowed(
                    db,
                    user_id=user_id,
                )
                row = await _get_note_row(db, user_id=user_id)
                if row is None:
                    if expected_version != 0:
                        raise NotesVersionConflictError(None, 0)
                    await db.execute(
                        """
                        INSERT INTO memory_notes(
                            id, subject_type, subject_id, content, source,
                            created_at, updated_at, version,
                            editing_locked_until, editing_lock_token_hash,
                            editing_lock_owner_type
                        )
                        VALUES (
                            ?, 'user', ?, '', 'user_explicit', ?, ?, 0,
                            ?, ?, 'user_edit'
                        )
                        """,
                        (
                            f"note_{uuid4().hex[:12]}",
                            user_id,
                            now_iso,
                            now_iso,
                            expires_at,
                            token_hash,
                        ),
                    )
                    public_version = 0
                else:
                    public_version = _public_note_version(row)
                    if public_version != expected_version:
                        raise NotesVersionConflictError(
                            _active_note_from_row(row),
                            public_version,
                        )
                    if _row_lock_active(row):
                        current_hash = str(row["editing_lock_token_hash"] or "")
                        if row["editing_lock_owner_type"] != "user_edit" or not edit_token or not hmac.compare_digest(current_hash, token_hash):
                            raise NotesLockedError(
                                str(row["editing_locked_until"]),
                            )
                    await db.execute(
                        """
                        UPDATE memory_notes
                        SET editing_locked_until=?, editing_lock_token_hash=?,
                            editing_lock_owner_type='user_edit'
                        WHERE subject_type='user' AND subject_id=?
                        """,
                        (expires_at, token_hash, user_id),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return NotesEditLease(
            edit_token=token,
            version=public_version,
            expires_at=expires_at,
        )

    async def commit_edit(
        self,
        *,
        user_id: str,
        content: str,
        expected_version: int,
        edit_token: str,
    ) -> MemoryNote:
        """Save About-you content with owner-token validation and version CAS."""

        if len(content) > MAX_CONTENT_CHARS:
            raise NotesCharLimitError(len(content))
        if not edit_token:
            raise NotesEditTokenError("edit token is required")
        token_hash = _hash_edit_token(edit_token)
        now = _now()

        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await ensure_memory_write_allowed(
                    db,
                    user_id=user_id,
                )
                row = await _get_note_row(db, user_id=user_id)
                if row is None:
                    raise NotesEditTokenError("edit session does not exist")
                _validate_edit_token(row, token_hash)
                public_version = _public_note_version(row)
                if public_version != expected_version:
                    raise NotesVersionConflictError(
                        _active_note_from_row(row),
                        public_version,
                    )
                current_version = int(row["version"])
                cursor = await db.execute(
                    """
                    UPDATE memory_notes
                    SET content=?, source='user_explicit', source_turn_id=NULL,
                        updated_at=?, deleted_at=NULL, version=version+1,
                        created_source_type=COALESCE(
                            created_source_type, 'user_explicit'
                        ),
                        last_modified_actor_type='user',
                        last_modified_actor_id=?, last_modified_at=?,
                        editing_locked_until=NULL,
                        editing_lock_token_hash=NULL,
                        editing_lock_owner_type=NULL
                    WHERE subject_type='user' AND subject_id=?
                      AND version=? AND editing_lock_token_hash=?
                    """,
                    (
                        content,
                        now,
                        user_id,
                        now,
                        user_id,
                        current_version,
                        token_hash,
                    ),
                )
                if (cursor.rowcount or 0) != 1:
                    latest = await _get_note_row(db, user_id=user_id)
                    raise NotesVersionConflictError(
                        _active_note_from_row(latest),
                        _public_note_version(latest),
                    )
                updated = await _get_note_row(db, user_id=user_id)
                assert updated is not None
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _row_to_note(updated)

    async def is_lock_active(self, *, user_id: str) -> bool:
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT editing_locked_until FROM memory_notes WHERE subject_type='user' AND subject_id=?",
                (user_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        if row is None or row[0] is None:
            return False
        try:
            expiry = datetime.fromisoformat(str(row[0]).replace(" ", "T"))
        except ValueError:
            return False
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        return expiry > datetime.now(tz=UTC)

    async def get_lock_expiry(self, *, user_id: str) -> str | None:
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT editing_locked_until FROM memory_notes WHERE subject_type='user' AND subject_id=?",
                (user_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return row[0] if row and row[0] else None

    async def soft_delete(self, *, user_id: str) -> bool:
        """For /memory/reset cascade. Returns True if a row was updated."""
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
            )
            cur = await db.execute(
                "UPDATE memory_notes SET deleted_at=?, updated_at=?, version=version+1, "
                "last_modified_actor_type='user', last_modified_actor_id=?, last_modified_at=? "
                "WHERE subject_type='user' AND subject_id=? AND deleted_at IS NULL",
                (now, now, user_id, now, user_id),
            )
            await db.commit()
            return (cur.rowcount or 0) > 0

    async def hard_delete_old_trash(self, *, max_age_days: int = 30) -> int:
        """Cron: hard-delete soft-deleted rows older than max_age_days."""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "DELETE FROM memory_notes WHERE deleted_at IS NOT NULL AND datetime(deleted_at) < datetime('now', ?)",
                (f"-{int(max_age_days)} days",),
            )
            await db.commit()
            return cur.rowcount or 0

    async def hard_delete_user(self, user_id: str) -> int:
        """Cron: removed-user GC cascade."""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
            )
            cur = await db.execute(
                "DELETE FROM memory_notes WHERE subject_type='user' AND subject_id=?",
                (user_id,),
            )
            await db.commit()
            return cur.rowcount or 0

    async def save_from_canvas(
        self,
        *,
        user_id: str,
        canvas_id: str,
        section_id: str,
        summary_text: str,
        verifier_result_hash: str,
        saved_at: str | None = None,
        pinned: bool = True,
    ) -> MemoryNote:
        """V4 PRD-D-5: append a verified-fact bullet anchored to a canvas section.

        Stores per-row (PRD-C limitation: memory_notes is single segment per user).
        Sprint 1 anchor: the latest save wins for the row's anchor — ok because
        UI shows row-level header tooltip only (not per-bullet) per spike.
        Returns the updated note (with verification_anchor populated).

        Dedup: if the bullet line for this exact summary_text is already present
        in content, skip the append (per design §2.3 "skip dup"). The row's
        anchor is still refreshed to the latest (canvas_id, section_id, hash).

        Source label is 'verified_in_report' (per PRD-D-5 §3.4). Active
        About-you edit leases always block this write; ``ignore_lock`` cannot be
        used by Canvas to overwrite a user's draft.
        """
        if not summary_text or not summary_text.strip():
            raise ValueError("summary_text cannot be empty")
        text = summary_text.strip()
        if len(text) > 200:
            raise ValueError("summary_text > 200 char")
        anchor_iso = saved_at or _now()
        anchor = {
            "canvas_id": canvas_id,
            "section_id": section_id,
            "verifier_result_hash": verifier_result_hash,
            "saved_at": anchor_iso,
        }
        anchor_json = json.dumps(anchor, ensure_ascii=False, sort_keys=True)
        # Dedup: skip the bullet append if exact same line already exists.
        existing = await self.get(user_id=user_id, include_trashed=True)
        prefix = "📌 " if pinned else "• "
        candidate_line = prefix + text
        already_present = bool(
            existing and existing.content and any(ln.rstrip() == candidate_line.rstrip() for ln in existing.content.split("\n")),
        )
        if not already_present:
            await self.append(
                user_id=user_id,
                bullet=text,
                pinned=pinned,
                source="verified_in_report",
                source_turn_id=None,
            )
        # Patch anchor + status onto the row (per-row, not per-bullet — V4 limitation).
        # Done even on dedup hit so anchor reflects most recent save click.
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
            )
            # Make sure the row exists (for new users with dedup-hit on no row)
            await db.execute(
                "INSERT OR IGNORE INTO memory_notes "
                "(id, subject_type, subject_id, content, source, "
                "created_at, updated_at, verification_status) "
                "VALUES (?, 'user', ?, '', 'verified_in_report', ?, ?, 'valid')",
                (f"note_{uuid4().hex[:12]}", user_id, _now(), _now()),
            )
            await db.execute(
                "UPDATE memory_notes "
                "SET verification_anchor=?, verification_status='valid', "
                "deleted_at=NULL, updated_at=? "
                "WHERE subject_type='user' AND subject_id=?",
                (anchor_json, _now(), user_id),
            )
            await db.commit()
        n = await self.get(user_id=user_id)
        assert n is not None
        return n

    # ─── helpers ──────────────────────────────────────


# ─── pure helpers ─────────────────────────────────────


async def _get_note_row(
    db: aiosqlite.Connection,
    *,
    user_id: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        """
        SELECT *
        FROM memory_notes
        WHERE subject_type='user' AND subject_id=?
        """,
        (user_id,),
    ) as cursor:
        return await cursor.fetchone()


def _hash_edit_token(edit_token: str) -> str:
    return hashlib.sha256(edit_token.encode("utf-8")).hexdigest()


def _row_lock_active(row: aiosqlite.Row | None) -> bool:
    if row is None or not row["editing_locked_until"]:
        return False
    try:
        expiry = datetime.fromisoformat(
            str(row["editing_locked_until"]).replace(" ", "T"),
        )
    except ValueError:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry > datetime.now(tz=UTC)


def _raise_if_row_locked(row: aiosqlite.Row | None) -> None:
    if _row_lock_active(row):
        assert row is not None
        raise NotesLockedError(str(row["editing_locked_until"]))


def _validate_edit_token(row: aiosqlite.Row, token_hash: str) -> None:
    current_hash = str(row["editing_lock_token_hash"] or "")
    if row["editing_lock_owner_type"] != "user_edit" or not _row_lock_active(row) or not hmac.compare_digest(current_hash, token_hash):
        raise NotesEditTokenError("edit token is invalid or expired")


def _public_note_version(row: aiosqlite.Row | None) -> int:
    if row is None:
        return 0
    return int(row["version"])


def _active_note_from_row(row: aiosqlite.Row | None) -> MemoryNote | None:
    if row is None or row["deleted_at"] is not None:
        return None
    return _row_to_note(row)


def _row_to_note(row: aiosqlite.Row) -> MemoryNote:
    keys = set(row.keys())
    anchor: dict[str, Any] | None = None
    if "verification_anchor" in keys and row["verification_anchor"]:
        try:
            parsed = json.loads(row["verification_anchor"])
            if isinstance(parsed, dict):
                anchor = parsed
        except (json.JSONDecodeError, TypeError):
            anchor = None
    status = "valid"
    if "verification_status" in keys and row["verification_status"]:
        status = row["verification_status"]
    return MemoryNote(
        id=row["id"],
        subject_type=row["subject_type"],
        subject_id=row["subject_id"],
        content=row["content"] or "",
        source=row["source"],
        source_turn_id=row["source_turn_id"],
        editing_locked_until=row["editing_locked_until"] if "editing_locked_until" in keys else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"] if "deleted_at" in keys else None,
        verification_anchor=anchor,
        verification_status=status,
        version=int(row["version"]) if "version" in keys else 1,
        created_source_type=row["created_source_type"] if "created_source_type" in keys else None,
        created_source_turn_id=row["created_source_turn_id"] if "created_source_turn_id" in keys else None,
        created_source_candidate_id=row["created_source_candidate_id"] if "created_source_candidate_id" in keys else None,
        created_source_workspace_id=row["created_source_workspace_id"] if "created_source_workspace_id" in keys else None,
        last_modified_actor_type=row["last_modified_actor_type"] if "last_modified_actor_type" in keys else None,
        last_modified_actor_id=row["last_modified_actor_id"] if "last_modified_actor_id" in keys else None,
        last_modified_at=row["last_modified_at"] if "last_modified_at" in keys else None,
    )


def _is_bullet_line(line: str) -> bool:
    s = line.lstrip()
    return s.startswith(("📌", "• ", "- "))


def _delete_bullet_at_index(content: str, target_index: int) -> str:
    lines = content.split("\n")
    bullet_count = 0
    out: list[str] = []
    deleted = False
    for ln in lines:
        if _is_bullet_line(ln):
            if bullet_count == target_index:
                deleted = True
                # skip
            else:
                out.append(ln)
            bullet_count += 1
        else:
            out.append(ln)
    if not deleted:
        raise IndexError(f"bullet_index {target_index} out of range (only {bullet_count} bullets)")
    return "\n".join(out)


def parse_notes_to_lines(content: str) -> list[dict[str, Any]]:
    """V4 Sprint 2: 跟 frontend lib/notes-markdown.ts parseNotes 对齐.

    返每行 {kind, lineIndex, bulletIndex, text}.
    text 是 stripped, 无 prefix. orphan scan + delete_by_bullet 用此提取 bullet_text.

    Python str: "📌" 是 1 code point → stripped[1:] (vs JS slice(2)).
    """
    if not content:
        return []
    out: list[dict[str, Any]] = []
    bullet_count = 0
    for i, raw in enumerate(content.split("\n")):
        stripped = raw.strip()
        if not stripped:
            out.append({"kind": "blank", "lineIndex": i, "bulletIndex": None, "text": ""})
            continue
        if stripped.startswith("📌"):
            out.append(
                {
                    "kind": "pinned",
                    "lineIndex": i,
                    "bulletIndex": bullet_count,
                    "text": stripped[1:].strip(),
                }
            )
            bullet_count += 1
        elif stripped.startswith(("• ", "- ")):
            out.append(
                {
                    "kind": "bullet",
                    "lineIndex": i,
                    "bulletIndex": bullet_count,
                    "text": stripped[2:].strip(),
                }
            )
            bullet_count += 1
        else:
            out.append(
                {
                    "kind": "freetext",
                    "lineIndex": i,
                    "bulletIndex": None,
                    "text": stripped,
                }
            )
    return out


def parse_notes_lines(content: str) -> dict[str, int]:
    """Parser for char_count / pinned_count / bullet_count / free_text_lines.
    Mirrors frontend lib/notes-markdown.ts (must stay in sync).
    """
    if not content:
        return {"char_count": 0, "pinned_count": 0, "bullet_count": 0, "free_text_lines": 0}
    pinned = bullet = freetext = 0
    for ln in content.split("\n"):
        s = ln.lstrip()
        if not s:
            continue
        if s.startswith("📌"):
            pinned += 1
        elif s.startswith(("• ", "- ")):
            bullet += 1
        else:
            freetext += 1
    return {
        "char_count": len(content),
        "pinned_count": pinned,
        "bullet_count": bullet,
        "free_text_lines": freetext,
    }
