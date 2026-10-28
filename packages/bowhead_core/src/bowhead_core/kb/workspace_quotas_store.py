"""KbWorkspaceQuotasStore — per-workspace monthly LLM token quota for Sprint 25/26.

Per PRD r3 §3.4 D4 + CTO P1-PERF-1 (defer). Caps LLM cost abuse from a single
workspace burning unbounded tokens via repeated re-trigger or massive file
uploads. Default cap: 50M tokens/month/workspace (~$0.05 cost cap at qwen-flash
pricing, much higher at qwen-plus).

Lifecycle:
- Table init in `initialize()`. Idempotent (CREATE IF NOT EXISTS).
- `check_quota(ws_id)` → True if under monthly cap, False if exceeded.
- `increment_usage(ws_id, tokens)` post-LLM-call += tokens_used for current month.
- Monthly key = "yyyymm" (e.g. "202605"). Auto-reset on month boundary (just
  start a new row).
- `get_summary(ws_id)` returns current month's usage + cap for admin endpoint.

NOT in lifespan startup hot path — store is constructed alongside per_row_jobs
db (same path co-location convenient).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite

from bowhead_core._db import bootstrap_shared_db, open_shared_ctx
from bowhead_core.kb.telemetry import emit_audit

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_TOKENS_CAP = 50_000_000  # 50M tokens/month default


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_workspace_quotas (
    workspace_id    TEXT NOT NULL,
    month_yyyymm    TEXT NOT NULL,
    tokens_used     INTEGER NOT NULL DEFAULT 0,
    tokens_cap      INTEGER NOT NULL DEFAULT 50000000,
    last_reset_at   TEXT NOT NULL,
    last_used_at    TEXT,
    PRIMARY KEY (workspace_id, month_yyyymm)
);
CREATE INDEX IF NOT EXISTS idx_kb_quotas_ws
    ON kb_workspace_quotas (workspace_id);
"""


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _current_month() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m")


@dataclass
class WorkspaceQuotaDTO:
    workspace_id: str
    month_yyyymm: str
    tokens_used: int
    tokens_cap: int
    last_reset_at: str
    last_used_at: str | None


class KbWorkspaceQuotasStore:
    """Per-ws monthly LLM token quota tracking. Independent SQLite db
    (co-located with per_row_jobs.db OR doc_types.db at caller discretion)."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def initialize(self) -> None:
        await bootstrap_shared_db(self._db_path)
        async with open_shared_ctx(self._db_path) as db:
            for stmt in _SCHEMA.split(";"):
                stmt = stmt.strip()
                if stmt:
                    await db.execute(stmt)
            await db.commit()

    async def check_quota(self, workspace_id: str) -> bool:
        """Returns True if workspace is under its monthly cap.
        Auto-creates row for current month if missing (with default cap)."""
        month = _current_month()
        async with open_shared_ctx(self._db_path) as db:
            async with db.execute(
                "SELECT tokens_used, tokens_cap FROM kb_workspace_quotas WHERE workspace_id = ? AND month_yyyymm = ?",
                (workspace_id, month),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                # Auto-create for current month
                await db.execute(
                    "INSERT OR IGNORE INTO kb_workspace_quotas "
                    "(workspace_id, month_yyyymm, tokens_used, tokens_cap, last_reset_at) "
                    "VALUES (?, ?, 0, ?, ?)",
                    (workspace_id, month, DEFAULT_TOKENS_CAP, _now_iso()),
                )
                await db.commit()
                return True
            tokens_used, tokens_cap = int(row[0]), int(row[1])
            if tokens_used >= tokens_cap:
                emit_audit(
                    "kb.row_extract_quota_exceeded",
                    {
                        "workspace_id": workspace_id,
                        "month_yyyymm": month,
                        "tokens_used": tokens_used,
                        "tokens_cap": tokens_cap,
                    },
                )
                return False
            return True

    async def increment_usage(self, workspace_id: str, tokens: int) -> None:
        """+= tokens_used for the workspace's current month row."""
        if tokens <= 0:
            return
        month = _current_month()
        now = _now_iso()
        async with open_shared_ctx(self._db_path) as db:
            # Ensure row exists
            await db.execute(
                "INSERT OR IGNORE INTO kb_workspace_quotas "
                "(workspace_id, month_yyyymm, tokens_used, tokens_cap, last_reset_at) "
                "VALUES (?, ?, 0, ?, ?)",
                (workspace_id, month, DEFAULT_TOKENS_CAP, now),
            )
            await db.execute(
                "UPDATE kb_workspace_quotas SET tokens_used = tokens_used + ?, last_used_at = ? WHERE workspace_id = ? AND month_yyyymm = ?",
                (tokens, now, workspace_id, month),
            )
            await db.commit()

    async def get_summary(self, workspace_id: str) -> WorkspaceQuotaDTO | None:
        """Current month's quota state for admin endpoint."""
        month = _current_month()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_workspace_quotas WHERE workspace_id = ? AND month_yyyymm = ?",
                (workspace_id, month),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return WorkspaceQuotaDTO(
            workspace_id=row["workspace_id"],
            month_yyyymm=row["month_yyyymm"],
            tokens_used=int(row["tokens_used"]),
            tokens_cap=int(row["tokens_cap"]),
            last_reset_at=row["last_reset_at"],
            last_used_at=row["last_used_at"],
        )

    async def set_cap(self, workspace_id: str, new_cap: int) -> None:
        """Admin override for ws cap (e.g. enterprise customer needs more)."""
        if new_cap <= 0:
            raise ValueError("tokens_cap must be > 0")
        month = _current_month()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO kb_workspace_quotas "
                "(workspace_id, month_yyyymm, tokens_used, tokens_cap, last_reset_at) "
                "VALUES (?, ?, 0, ?, ?)",
                (workspace_id, month, new_cap, _now_iso()),
            )
            await db.execute(
                "UPDATE kb_workspace_quotas SET tokens_cap = ? WHERE workspace_id = ? AND month_yyyymm = ?",
                (new_cap, workspace_id, month),
            )
            await db.commit()
