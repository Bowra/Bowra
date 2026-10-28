"""Per-principal cache for homepage work proposals."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import aiosqlite

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS home_proposal_cache (
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    locale TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    capability_fingerprint TEXT NOT NULL,
    context_fingerprint TEXT NOT NULL,
    strategy TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (user_id, workspace_id, locale)
);
CREATE INDEX IF NOT EXISTS idx_home_proposal_cache_expiry
    ON home_proposal_cache(expires_at);
"""


@dataclass(frozen=True)
class HomeProposalCacheEntry:
    user_id: str
    workspace_id: str
    locale: str
    generation_id: str
    generated_at: str
    expires_at: str
    capability_fingerprint: str
    context_fingerprint: str
    strategy: Literal["llm", "deterministic"]
    payload: dict[str, Any]


class HomeProposalCacheStore:
    """SQLite cache keyed by user, canonical Bowhead space, and locale."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)

    async def initialize(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def get(
        self,
        *,
        user_id: str,
        workspace_id: str,
        locale: str,
    ) -> HomeProposalCacheEntry | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM home_proposal_cache WHERE user_id=? AND workspace_id=? AND locale=?",
                (user_id, workspace_id, locale),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        strategy = str(row["strategy"])
        if strategy not in {"llm", "deterministic"}:
            return None
        return HomeProposalCacheEntry(
            user_id=str(row["user_id"]),
            workspace_id=str(row["workspace_id"]),
            locale=str(row["locale"]),
            generation_id=str(row["generation_id"]),
            generated_at=str(row["generated_at"]),
            expires_at=str(row["expires_at"]),
            capability_fingerprint=str(row["capability_fingerprint"]),
            context_fingerprint=str(row["context_fingerprint"]),
            strategy=cast("Literal['llm', 'deterministic']", strategy),
            payload=payload,
        )

    async def upsert(self, entry: HomeProposalCacheEntry) -> None:
        payload_json = json.dumps(
            entry.payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO home_proposal_cache "
                "(user_id, workspace_id, locale, generation_id, generated_at, "
                "expires_at, capability_fingerprint, context_fingerprint, "
                "strategy, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, workspace_id, locale) DO UPDATE SET "
                "generation_id=excluded.generation_id, "
                "generated_at=excluded.generated_at, "
                "expires_at=excluded.expires_at, "
                "capability_fingerprint=excluded.capability_fingerprint, "
                "context_fingerprint=excluded.context_fingerprint, "
                "strategy=excluded.strategy, payload_json=excluded.payload_json",
                (
                    entry.user_id,
                    entry.workspace_id,
                    entry.locale,
                    entry.generation_id,
                    entry.generated_at,
                    entry.expires_at,
                    entry.capability_fingerprint,
                    entry.context_fingerprint,
                    entry.strategy,
                    payload_json,
                ),
            )
            await db.commit()
