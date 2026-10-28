"""Lightweight async event log for V3 PRD-B (spike #6).

Records (event_name, user_id, attrs_json, created_at) into `metrics_event_log`.
Best-effort; failures are swallowed and logged. Aggregation / dashboard cron
留 V3 ship 后 (out of scope for PRD-B).

Typical events (用 dot-namespace 命名):
- memory.candidate.approve / .reject / .auto_reject
- memory.entry.create / .delete / .restore / .reset
- memory.export.requested
- memory.dreaming.paused / .resumed / .skipped

API:
    log = MetricsLog(db_path)
    await log.initialize()
    await log.track("memory.candidate.approve", user_id=u, attrs={"cid": cid})

Schema:
    metrics_event_log(id, name, user_id, attrs_json, created_at)

Co-located with user_memory store (shared sqlite db_path).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics_event_log (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    user_id     TEXT,
    attrs_json  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_name_time
    ON metrics_event_log(name, created_at);
CREATE INDEX IF NOT EXISTS idx_metrics_user_time
    ON metrics_event_log(user_id, created_at);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


class MetricsLog:
    """Async sqlite-backed best-effort metrics event log."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def initialize(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def track(
        self,
        name: str,
        *,
        user_id: str | None = None,
        attrs: dict[str, object] | None = None,
    ) -> None:
        """Insert one event row. Best-effort: failures logged, not raised."""
        try:
            event_id = f"evt_{uuid4().hex[:12]}"
            payload = json.dumps(attrs or {}, ensure_ascii=False, default=str)
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    "INSERT INTO metrics_event_log (id, name, user_id, attrs_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (event_id, name, user_id, payload, _now()),
                )
                await db.commit()
        except Exception as exc:
            logger.warning(
                "metrics.track_failed name=%s user=%s err=%s",
                name,
                user_id,
                exc,
            )

    async def query_recent(
        self,
        *,
        name: str | None = None,
        user_id: str | None = None,
        hours: int = 24,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        """Read recent events (ad-hoc dashboards / debug). Not high-perf."""
        sql = "SELECT id, name, user_id, attrs_json, created_at FROM metrics_event_log WHERE 1=1"
        params: list = []
        if name is not None:
            sql += " AND name = ?"
            params.append(name)
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " AND datetime(created_at) >= datetime('now', ?)"
        params.append(f"-{int(hours)} hours")
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        out: list[dict[str, object]] = []
        for r in rows:
            try:
                attrs = json.loads(r["attrs_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                attrs = {}
            out.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "user_id": r["user_id"],
                    "attrs": attrs,
                    "created_at": r["created_at"],
                }
            )
        return out

    async def hard_delete_old(self, *, max_age_days: int = 90) -> int:
        """Cron: trim event log older than max_age_days. Returns deleted count."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM metrics_event_log WHERE datetime(created_at) < datetime('now', ?)",
                (f"-{int(max_age_days)} days",),
            )
            await db.commit()
            return cur.rowcount or 0
