"""V4 Sprint 1 PRD-D-4-4 · MemoryPackSnapshotsStore.

Audit-immutable append-only store for what was injected into Lead system prompt
at each turn. 1 row per turn. PRIMARY KEY turn_id (idempotent INSERT OR IGNORE).

Cross-user isolation via user_id filter on read.
Cross-team admin scope via team_id filter (NULL team_id row = orphan, not in any
admin scope per PRD-D-4-4 §5.2).

6-month TTL via memory_gc cron extension (180 days default).

Schema migration follows PRD-B/C 3-step pattern (TABLE → ALTER → INDEX) so
future ALTER additions don't trip on legacy DBs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.memory_privacy import (
    ensure_memory_write_allowed,
    is_memory_read_hidden,
)

logger = logging.getLogger(__name__)


_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_pack_snapshots (
    turn_id                 TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    team_id                  TEXT,
    workspace_id            TEXT,
    content                 TEXT NOT NULL,
    char_count              INTEGER NOT NULL,
    sections_breakdown_json TEXT,
    pack_version            TEXT NOT NULL,
    created_at              TEXT NOT NULL
);
"""

_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_memory_pack_snapshots_user
    ON memory_pack_snapshots(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_pack_snapshots_team
    ON memory_pack_snapshots(team_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_pack_snapshots_created
    ON memory_pack_snapshots(created_at);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@dataclass
class MemoryPackSnapshot:
    turn_id: str
    user_id: str
    team_id: str | None
    workspace_id: str | None
    content: str
    char_count: int
    sections_breakdown: dict[str, int]
    pack_version: str
    created_at: str


class MemoryPackSnapshotsStore:
    """Append-only audit log of memory injected into Lead prompts (per turn)."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def initialize(self) -> None:
        """Create table + indexes. PRD-B/C 3-step pattern (TABLE → ALTER → INDEX).
        Rename the pre-team schema column before creating the team-scoped index.
        """
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with open_shared_ctx(self._db_path) as db:
            await db.executescript(_TABLE_SCHEMA)
            columns = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(memory_pack_snapshots)")}
            if "org_id" in columns and "team_id" not in columns:
                await db.execute("ALTER TABLE memory_pack_snapshots RENAME COLUMN org_id TO team_id")
            # Memory rework batch 3: **persist executor blocks in the same snapshot row too**.
            # Otherwise the live == persist == replay invariant is false on the peer path:
            # `/memory/replay` can return only Lead's pack, and audit cannot see the actual bytes received by each executor
            # and report author, although chat-pipeline / REPORT_WRITING /
            # memory_pack / memory_injection all explicitly state this invariant.
            # Added **before enabling the gate**. This table is self-managed (outside Alembic); add the column through this idempotent ALTER.
            if "executor_block" not in columns:
                await db.execute("ALTER TABLE memory_pack_snapshots ADD COLUMN executor_block TEXT")
            await db.executescript(_INDEX_SCHEMA)
            await db.commit()

    async def save(
        self,
        *,
        turn_id: str,
        user_id: str,
        team_id: str | None,
        workspace_id: str | None,
        content: str,
        sections_breakdown: dict[str, int],
        pack_version: str,
        executor_block: str = "",
    ) -> bool:
        """Insert one snapshot row. Returns True if inserted, False if skipped.

        Skipped when:
          - content empty (no point in storing zero-byte snapshot)
          - turn_id already exists (PRIMARY KEY conflict, idempotent)
        """
        if not content:
            return False
        char_count = len(content)
        breakdown_json = json.dumps(
            sections_breakdown,
            ensure_ascii=False,
            sort_keys=True,
        )
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_memory_write_allowed(
                db,
                user_id=user_id,
            )
            cur = await db.execute(
                "INSERT OR IGNORE INTO memory_pack_snapshots "
                "(turn_id, user_id, team_id, workspace_id, content, char_count, "
                "sections_breakdown_json, pack_version, created_at, executor_block) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    turn_id,
                    user_id,
                    team_id,
                    workspace_id,
                    content,
                    char_count,
                    breakdown_json,
                    pack_version,
                    _now(),
                    executor_block,
                ),
            )
            await db.commit()
            return (cur.rowcount or 0) > 0

    async def get_by_turn_for_user(
        self,
        *,
        turn_id: str,
        user_id: str,
    ) -> MemoryPackSnapshot | None:
        """User-self path: returns row if turn belongs to this user, None otherwise.

        Cross-user attempts (user A queries user B's turn) → returns None (opaque 404).
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if await is_memory_read_hidden(db, user_id=user_id):
                return None
            async with db.execute(
                "SELECT * FROM memory_pack_snapshots WHERE turn_id = ? AND user_id = ?",
                (turn_id, user_id),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_snapshot(row) if row else None

    async def get_by_turn_for_admin(
        self,
        *,
        turn_id: str,
        admin_team_id: str,
    ) -> MemoryPackSnapshot | None:
        """Admin path: returns row if turn's team matches admin's team.

        NULL team_id rows (orphan, dev/legacy) are NOT in any admin scope (returns None).
        Cross-team access blocked (admin from team X cannot see team Y turns).
        """
        if not admin_team_id:
            # Admin without team_id can't scope; return None to be safe.
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM memory_pack_snapshots WHERE turn_id = ? AND team_id IS NOT NULL AND team_id = ?",
                (turn_id, admin_team_id),
            ) as cur:
                row = await cur.fetchone()
            if row and await is_memory_read_hidden(
                db,
                user_id=str(row["user_id"]),
            ):
                return None
        return _row_to_snapshot(row) if row else None

    async def hard_delete_old(self, *, max_age_days: int = 180) -> int:
        """Cron TTL: hard-delete snapshots > max_age_days old. Returns count.

        Uses datetime() to normalize ISO 'T' vs sqlite ' ' format (PRD-B spike #19).
        """
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM memory_pack_snapshots WHERE datetime(created_at) < datetime('now', ?)",
                (f"-{int(max_age_days)} days",),
            )
            await db.commit()
            return cur.rowcount or 0


def _row_to_snapshot(row: aiosqlite.Row) -> MemoryPackSnapshot:
    try:
        breakdown = json.loads(row["sections_breakdown_json"] or "{}")
        if not isinstance(breakdown, dict):
            breakdown = {}
    except (json.JSONDecodeError, TypeError):
        breakdown = {}
    return MemoryPackSnapshot(
        turn_id=row["turn_id"],
        user_id=row["user_id"],
        team_id=row["team_id"],
        workspace_id=row["workspace_id"],
        content=row["content"] or "",
        char_count=int(row["char_count"] or 0),
        sections_breakdown=breakdown,
        pack_version=row["pack_version"],
        created_at=row["created_at"],
    )
