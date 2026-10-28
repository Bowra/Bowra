"""kb_lifespan_markers — lightweight key-value store for lifespan hooks.

Sprint 2 kb-structured-tables marker keys:
  - ``catalog_seeded_at_ws:{workspace_id}`` → ISO datetime; presence = seed 已跑

Future Sprint 3+ marker keys reuse this same table.

Lives in the same DB file as ``kb_doc_types.db`` (shared connection pool +
locking semantics — per tech-design Q-T7).

Schema:
    CREATE TABLE kb_lifespan_markers (
        marker_key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 (runtime-used as `str | Path` arg, kept import-time)
from typing import Any

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_lifespan_markers (
    marker_key  TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


class KbLifespanMarkerStore:
    """Key-value lifespan marker store. Idempotent set + atomic check-and-set."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def initialize(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def has_marker(self, marker_key: str) -> bool:
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT 1 FROM kb_lifespan_markers WHERE marker_key = ?",
                (marker_key,),
            ) as cur,
        ):
            return await cur.fetchone() is not None

    async def get(self, marker_key: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT value_json FROM kb_lifespan_markers WHERE marker_key = ?",
                (marker_key,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            return None

    async def set(self, marker_key: str, value: dict[str, Any]) -> None:
        """UPSERT marker."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO kb_lifespan_markers (marker_key, value_json, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(marker_key) DO UPDATE SET "
                "value_json = excluded.value_json, updated_at = excluded.updated_at",
                (marker_key, json.dumps(value, ensure_ascii=False), _now()),
            )
            await db.commit()

    @staticmethod
    def catalog_marker_key(workspace_id: str) -> str:
        return f"catalog_seeded_at_ws:{workspace_id}"
