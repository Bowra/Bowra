"""Short-lived pre-draft conversation state for Subagent purpose collection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import aiosqlite

from bowhead_core._db import open_shared_ctx

CONTEXT_TTL_MINUTES = 30


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SubagentAuthoringContext:
    team_id: str
    owner_user_id: str
    session_id: str
    state: str
    purpose_text: str | None
    purpose_prompt_count: int
    insufficient_reprompt_count: int
    business_clarification_count: int
    draft_id: str | None
    row_version: int
    expires_at: str
    created_at: str
    updated_at: str


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


class SubagentAuthoringContextStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self._clock = clock or SystemClock()

    async def initialize(self) -> None:
        """Verify the Alembic-owned table; never create runtime DDL."""
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table'
                  AND name='team_subagent_authoring_context'
                """,
            ) as cursor,
        ):
            if await cursor.fetchone() is None:
                raise RuntimeError(
                    "subagent_authoring_context_schema_missing",
                )

    async def get(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        session_id: str,
    ) -> SubagentAuthoringContext | None:
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT *
                FROM team_subagent_authoring_context
                WHERE team_id=? AND owner_user_id=? AND session_id=?
                  AND expires_at>?
                """,
                (team_id, owner_user_id, session_id, now_s),
            ) as cursor:
                row = await cursor.fetchone()
        return _row_to_context(row) if row is not None else None

    async def record_purpose_prompt(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        session_id: str,
    ) -> SubagentAuthoringContext | None:
        now = self._clock.now()
        now_s = _timestamp(now)
        expires_s = _timestamp(now + timedelta(minutes=CONTEXT_TTL_MINUTES))
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO team_subagent_authoring_context(
                  team_id, owner_user_id, session_id, state,
                  purpose_text, purpose_prompt_count,
                  insufficient_reprompt_count,
                  business_clarification_count, draft_id, row_version,
                  expires_at, created_at, updated_at
                ) VALUES (
                  ?,?,?,'collecting_purpose',NULL,1,0,0,NULL,1,?,?,?
                )
                ON CONFLICT(team_id, owner_user_id, session_id) DO UPDATE SET
                  state='collecting_purpose',
                  purpose_prompt_count=purpose_prompt_count+1,
                  row_version=row_version+1,
                  expires_at=excluded.expires_at,
                  updated_at=excluded.updated_at
                WHERE team_subagent_authoring_context.state='collecting_purpose'
                  AND team_subagent_authoring_context.purpose_prompt_count<2
                """,
                (
                    team_id,
                    owner_user_id,
                    session_id,
                    expires_s,
                    now_s,
                    now_s,
                ),
            )
            await db.commit()
            if cursor.rowcount != 1:
                return None
        return await self.get(
            team_id=team_id,
            owner_user_id=owner_user_id,
            session_id=session_id,
        )

    async def record_insufficient_prompt(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        session_id: str,
        purpose_text: str,
    ) -> SubagentAuthoringContext | None:
        """Record the single allowed insufficient-purpose re-prompt.

        Purpose collection and business clarification have separate budgets.
        This counter is also independent from the absent-purpose prompt so a
        user who first supplied no purpose still gets one useful sufficiency
        hint, but never enters a repeated questionnaire loop.
        """
        now = self._clock.now()
        now_s = _timestamp(now)
        expires_s = _timestamp(now + timedelta(minutes=CONTEXT_TTL_MINUTES))
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO team_subagent_authoring_context(
                  team_id, owner_user_id, session_id, state,
                  purpose_text, purpose_prompt_count,
                  insufficient_reprompt_count,
                  business_clarification_count, draft_id, row_version,
                  expires_at, created_at, updated_at
                ) VALUES (
                  ?,?,?,'collecting_purpose',?,0,1,0,NULL,1,?,?,?
                )
                ON CONFLICT(team_id, owner_user_id, session_id) DO UPDATE SET
                  state='collecting_purpose',
                  purpose_text=excluded.purpose_text,
                  insufficient_reprompt_count=1,
                  row_version=row_version+1,
                  expires_at=excluded.expires_at,
                  updated_at=excluded.updated_at
                WHERE team_subagent_authoring_context.state='collecting_purpose'
                  AND team_subagent_authoring_context.insufficient_reprompt_count<1
                """,
                (
                    team_id,
                    owner_user_id,
                    session_id,
                    purpose_text,
                    expires_s,
                    now_s,
                    now_s,
                ),
            )
            await db.commit()
            if cursor.rowcount != 1:
                return None
        return await self.get(
            team_id=team_id,
            owner_user_id=owner_user_id,
            session_id=session_id,
        )

    async def upsert_clarifying(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        session_id: str,
        purpose_text: str,
        draft_id: str,
    ) -> SubagentAuthoringContext:
        """Persist the one-draft clarification pointer for the next turn."""
        now = self._clock.now()
        now_s = _timestamp(now)
        expires_s = _timestamp(now + timedelta(minutes=CONTEXT_TTL_MINUTES))
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO team_subagent_authoring_context(
                  team_id, owner_user_id, session_id, state,
                  purpose_text, purpose_prompt_count,
                  insufficient_reprompt_count,
                  business_clarification_count, draft_id, row_version,
                  expires_at, created_at, updated_at
                ) VALUES (
                  ?,?,?,'clarifying',?,0,0,1,?,1,?,?,?
                )
                ON CONFLICT(team_id, owner_user_id, session_id) DO UPDATE SET
                  state='clarifying',
                  purpose_text=excluded.purpose_text,
                  draft_id=excluded.draft_id,
                  business_clarification_count=1,
                  row_version=row_version+1,
                  expires_at=excluded.expires_at,
                  updated_at=excluded.updated_at
                """,
                (
                    team_id,
                    owner_user_id,
                    session_id,
                    purpose_text,
                    draft_id,
                    expires_s,
                    now_s,
                    now_s,
                ),
            )
            await db.commit()
        result = await self.get(
            team_id=team_id,
            owner_user_id=owner_user_id,
            session_id=session_id,
        )
        if result is None:
            raise RuntimeError("subagent_authoring_context_upsert_lost")
        return result

    async def consume(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        session_id: str,
        expected_version: int | None = None,
    ) -> bool:
        query = """
            DELETE FROM team_subagent_authoring_context
            WHERE team_id=? AND owner_user_id=? AND session_id=?
        """
        params: list[object] = [team_id, owner_user_id, session_id]
        if expected_version is not None:
            query += " AND row_version=?"
            params.append(expected_version)
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(query, tuple(params))
            await db.commit()
            return cursor.rowcount == 1

    async def purge_expired(self, *, limit: int = 100) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError("purge limit must be in [1,1000]")
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                """
                DELETE FROM team_subagent_authoring_context
                WHERE rowid IN (
                  SELECT rowid
                  FROM team_subagent_authoring_context
                  WHERE expires_at<=?
                  ORDER BY expires_at
                  LIMIT ?
                )
                """,
                (_timestamp(self._clock.now()), limit),
            )
            await db.commit()
            return max(cursor.rowcount, 0)


def _row_to_context(row: aiosqlite.Row) -> SubagentAuthoringContext:
    return SubagentAuthoringContext(
        team_id=str(row["team_id"]),
        owner_user_id=str(row["owner_user_id"]),
        session_id=str(row["session_id"]),
        state=str(row["state"]),
        purpose_text=row["purpose_text"],
        purpose_prompt_count=int(row["purpose_prompt_count"]),
        insufficient_reprompt_count=int(row["insufficient_reprompt_count"]),
        business_clarification_count=int(
            row["business_clarification_count"],
        ),
        draft_id=row["draft_id"],
        row_version=int(row["row_version"]),
        expires_at=str(row["expires_at"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


__all__ = [
    "SubagentAuthoringContext",
    "SubagentAuthoringContextStore",
]
