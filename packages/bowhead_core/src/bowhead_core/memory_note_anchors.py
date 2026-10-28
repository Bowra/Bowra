"""V4 Sprint 2 PRD-D-4-1 · MemoryNoteAnchorsStore.

Per-bullet sidecar table for memory_notes verification anchors. Replaces
Sprint 1 row-level `memory_notes.verification_anchor` JSON column with one
row per (user, canvas, section, bullet_text_norm) — enabling true per-bullet
status tracking.

Status flow:
  - 'valid'   : anchor active, hash matches latest verifier output
  - 'stale'   : source canvas/section deleted or payload changed
  - 'unknown' : reserved for future (e.g. canvas read failure mid-recompute)

Cap: MAX_ANCHORS_PER_USER=50 valid anchors per user (stale don't count;
they get GC'd at 180d).

3-step migration pattern (TABLE → ALTER (空) → INDEX), per PRD-B 教训.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
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


MAX_ANCHORS_PER_USER = 50  # cap on status='valid'; stale don't count


_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_note_anchors (
    id                    TEXT PRIMARY KEY,
    user_id               TEXT NOT NULL,
    bullet_text           TEXT NOT NULL,
    bullet_text_norm      TEXT NOT NULL,
    canvas_id             TEXT NOT NULL,
    section_id            TEXT NOT NULL,
    verifier_result_hash  TEXT NOT NULL,
    saved_at              TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'valid',
    invalidated_at        TEXT,
    invalidation_reason   TEXT,
    UNIQUE(user_id, canvas_id, section_id, bullet_text_norm)
);
"""

_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_anchors_user_status
    ON memory_note_anchors(user_id, status);
CREATE INDEX IF NOT EXISTS idx_anchors_canvas
    ON memory_note_anchors(canvas_id);
CREATE INDEX IF NOT EXISTS idx_anchors_canvas_section
    ON memory_note_anchors(canvas_id, section_id);
CREATE INDEX IF NOT EXISTS idx_anchors_invalidated_at
    ON memory_note_anchors(invalidated_at)
    WHERE invalidated_at IS NOT NULL;
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _normalize(bullet_text: str) -> str:
    """Dedup key normalize: strip + lowercase. UI keeps raw; DB uses norm for UNIQUE."""
    return bullet_text.strip().lower()


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    """SHA256 hex digest of canonical JSON serialization of canvas section.payload.

    Stable across whitespace / key order so anchor stays valid when payload
    semantically unchanged. Used as `verifier_result_hash` in anchor records.

    Re-exported by gateway routes to keep core ↔ gateway dependency one-way.
    """
    canon = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


@dataclass
class MemoryNoteAnchor:
    id: str
    user_id: str
    bullet_text: str
    bullet_text_norm: str
    canvas_id: str
    section_id: str
    verifier_result_hash: str
    saved_at: str
    status: str
    invalidated_at: str | None = None
    invalidation_reason: str | None = None


class AnchorCapExceeded(Exception):
    """Raised when user already has MAX_ANCHORS_PER_USER status='valid' anchors."""


class MemoryNoteAnchorsStore:
    """Per-bullet anchor sidecar to memory_notes."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def initialize(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with open_shared_ctx(self._db_path) as db:
            await db.executescript(_TABLE_SCHEMA)
            # ALTER step reserved for future column additions.
            await db.executescript(_INDEX_SCHEMA)
            await db.commit()

    async def insert_or_revive(
        self,
        *,
        user_id: str,
        bullet_text: str,
        canvas_id: str,
        section_id: str,
        verifier_result_hash: str,
        saved_at: str | None = None,
    ) -> tuple[MemoryNoteAnchor, bool]:
        """INSERT new OR UPSERT existing. Returns (anchor, created).

        Behaviour:
          - existing row + status='valid' + same hash → dedup, no-op (created=False)
          - existing row + status='stale' OR different hash → revive (status='valid',
            new hash), no row count change (created=False)
          - no existing row → cap check, INSERT new (created=True)

        Raises AnchorCapExceeded when cap=50 hit and no existing row matches.
        """
        norm = _normalize(bullet_text)
        ts = saved_at or _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
            )
            # Look up existing
            async with db.execute(
                "SELECT * FROM memory_note_anchors WHERE user_id=? AND canvas_id=? AND section_id=? AND bullet_text_norm=?",
                (user_id, canvas_id, section_id, norm),
            ) as cur:
                existing = await cur.fetchone()

            if existing is not None:
                if existing["status"] == "valid" and existing["verifier_result_hash"] == verifier_result_hash:
                    return _row_to_anchor(existing), False
                # Revive / refresh
                await db.execute(
                    "UPDATE memory_note_anchors "
                    "SET status='valid', verifier_result_hash=?, saved_at=?, "
                    "    invalidated_at=NULL, invalidation_reason=NULL "
                    "WHERE id=?",
                    (verifier_result_hash, ts, existing["id"]),
                )
                await db.commit()
                async with db.execute(
                    "SELECT * FROM memory_note_anchors WHERE id=?",
                    (existing["id"],),
                ) as cur:
                    refreshed = await cur.fetchone()
                return _row_to_anchor(refreshed), False

            # Cap check on status='valid' only
            async with db.execute(
                "SELECT COUNT(*) AS n FROM memory_note_anchors WHERE user_id=? AND status='valid'",
                (user_id,),
            ) as cur:
                cap_row = await cur.fetchone()
            if int(cap_row["n"] or 0) >= MAX_ANCHORS_PER_USER:
                raise AnchorCapExceeded(
                    f"user {user_id} has {cap_row['n']} valid anchors (cap={MAX_ANCHORS_PER_USER})",
                )

            new_id = f"anc_{uuid4().hex[:12]}"
            await db.execute(
                "INSERT INTO memory_note_anchors "
                "(id, user_id, bullet_text, bullet_text_norm, canvas_id, section_id, "
                " verifier_result_hash, saved_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'valid')",
                (new_id, user_id, bullet_text, norm, canvas_id, section_id, verifier_result_hash, ts),
            )
            await db.commit()
            async with db.execute(
                "SELECT * FROM memory_note_anchors WHERE id=?",
                (new_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_anchor(row), True

    async def list_for_user(self, user_id: str) -> list[MemoryNoteAnchor]:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if await is_memory_read_hidden(db, user_id=user_id):
                return []
            async with db.execute(
                "SELECT * FROM memory_note_anchors WHERE user_id=? ORDER BY saved_at DESC",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_anchor(r) for r in rows]

    async def get_by_bullet(
        self,
        *,
        user_id: str,
        bullet_text: str,
    ) -> MemoryNoteAnchor | None:
        norm = _normalize(bullet_text)
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if await is_memory_read_hidden(db, user_id=user_id):
                return None
            async with db.execute(
                "SELECT * FROM memory_note_anchors WHERE user_id=? AND bullet_text_norm=? LIMIT 1",
                (user_id, norm),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_anchor(row) if row else None

    async def invalidate_by_canvas(
        self,
        *,
        canvas_id: str,
        reason: str = "canvas_deleted",
    ) -> int:
        """Mark all status='valid' anchors for this canvas as stale.
        Returns rowcount."""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "UPDATE memory_note_anchors SET status='stale', invalidated_at=?, invalidation_reason=? WHERE canvas_id=? AND status='valid'",
                (_now(), reason, canvas_id),
            )
            await db.commit()
            return cur.rowcount or 0

    async def invalidate_by_section(
        self,
        *,
        canvas_id: str,
        section_id: str,
        reason: str = "section_deleted",
    ) -> int:
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "UPDATE memory_note_anchors "
                "SET status='stale', invalidated_at=?, invalidation_reason=? "
                "WHERE canvas_id=? AND section_id=? AND status='valid'",
                (_now(), reason, canvas_id, section_id),
            )
            await db.commit()
            return cur.rowcount or 0

    async def recheck_hash(
        self,
        *,
        canvas_id: str,
        section_id: str,
        new_payload: dict,
    ) -> int:
        """For each valid anchor on this section, recompute hash from new_payload.
        Mismatch → mark stale, reason='payload_changed'. Returns count invalidated.
        """
        new_hash = canonical_payload_hash(new_payload or {})
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE memory_note_anchors "
                "SET status='stale', invalidated_at=?, invalidation_reason='payload_changed' "
                "WHERE canvas_id=? AND section_id=? AND status='valid' "
                "  AND verifier_result_hash != ?",
                (_now(), canvas_id, section_id, new_hash),
            )
            await db.commit()
            return cur.rowcount or 0

    async def delete_by_bullet(
        self,
        *,
        user_id: str,
        bullet_text: str,
    ) -> int:
        norm = _normalize(bullet_text)
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
            )
            cur = await db.execute(
                "DELETE FROM memory_note_anchors WHERE user_id=? AND bullet_text_norm=?",
                (user_id, norm),
            )
            await db.commit()
            return cur.rowcount or 0

    async def hard_delete_user(self, user_id: str) -> int:
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
            )
            cur = await db.execute(
                "DELETE FROM memory_note_anchors WHERE user_id=?",
                (user_id,),
            )
            await db.commit()
            return cur.rowcount or 0

    async def hard_delete_old_invalidated(
        self,
        *,
        max_age_days: int = 180,
    ) -> int:
        """Cron TTL: delete invalidated rows > max_age_days. Spike #19 datetime() normalize."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM memory_note_anchors WHERE invalidated_at IS NOT NULL   AND datetime(invalidated_at) < datetime('now', ?)",
                (f"-{int(max_age_days)} days",),
            )
            await db.commit()
            return cur.rowcount or 0

    async def mark_orphan_anchors(
        self,
        *,
        notes_store: object,
    ) -> int:
        """For each user, scan valid anchors. If bullet_text_norm not present in
        notes content (parse_notes_to_lines + normalize), mark stale.

        Reason='bullet_text_lost'. Catches: user PATCH /notes 改 content 但 anchor
        没 cascade 删 (delete_by_bullet 仅在 DELETE /bullet route 调).
        """
        from bowhead_core.memory_notes import parse_notes_to_lines

        marked = 0
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT DISTINCT user_id FROM memory_note_anchors WHERE status='valid'",
            ) as cur:
                user_rows = await cur.fetchall()

        for ur in user_rows:
            uid = ur["user_id"]
            note = await notes_store.get(user_id=uid)  # type: ignore[attr-defined]
            present_norms = set()
            if note and note.content:
                for ln in parse_notes_to_lines(note.content):
                    if ln["kind"] in ("pinned", "bullet") and ln["text"]:
                        present_norms.add(_normalize(ln["text"]))
            anchors = await self.list_for_user(uid)
            for a in anchors:
                if a.status == "valid" and a.bullet_text_norm not in present_norms:
                    async with aiosqlite.connect(self._db_path) as db:
                        cur = await db.execute(
                            "UPDATE memory_note_anchors "
                            "SET status='stale', invalidated_at=?, "
                            "    invalidation_reason='bullet_text_lost' "
                            "WHERE id=? AND status='valid'",
                            (_now(), a.id),
                        )
                        await db.commit()
                        marked += cur.rowcount or 0
        return marked


def _row_to_anchor(row: aiosqlite.Row) -> MemoryNoteAnchor:
    return MemoryNoteAnchor(
        id=row["id"],
        user_id=row["user_id"],
        bullet_text=row["bullet_text"],
        bullet_text_norm=row["bullet_text_norm"],
        canvas_id=row["canvas_id"],
        section_id=row["section_id"],
        verifier_result_hash=row["verifier_result_hash"],
        saved_at=row["saved_at"],
        status=row["status"],
        invalidated_at=row["invalidated_at"],
        invalidation_reason=row["invalidation_reason"],
    )


__all__ = [
    "MAX_ANCHORS_PER_USER",
    "AnchorCapExceeded",
    "MemoryNoteAnchor",
    "MemoryNoteAnchorsStore",
    "_normalize",
    "replace",  # re-export for callers using DTO replace
]
