"""Idempotent credit reservation and usage settlement for draft generation."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.rate_card import ModelTokenUsage, ModelUsageLine, credits_for_model_lines, credits_for_tokens
from bowhead_core.user_store import _id, _insert_credit_model_usage_in_tx

logger = logging.getLogger(__name__)
DEFAULT_RESERVED_CREDITS = 10
RESERVATION_LEASE_MINUTES = 10


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SubagentDraftCreditError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


class SubagentDraftCreditAccountant:
    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Clock | None = None,
        reserved_credits: int = DEFAULT_RESERVED_CREDITS,
    ) -> None:
        if not 1 <= reserved_credits <= 100:
            raise ValueError("reserved_credits must be in [1,100]")
        self.db_path = str(db_path)
        self._clock = clock or SystemClock()
        self._reserved_credits = reserved_credits

    async def initialize(self) -> None:
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='team_subagent_draft_usage'
                """,
            ) as cursor,
        ):
            if await cursor.fetchone() is None:
                raise RuntimeError(
                    "subagent_draft_usage_schema_missing",
                )

    async def preflight(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        request_key_hash: str,
    ) -> None:
        now = self._clock.now()
        now_s = _timestamp(now)
        lease_s = _timestamp(
            now + timedelta(minutes=RESERVATION_LEASE_MINUTES),
        )
        ledger_id = _id("led")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT state
                    FROM team_subagent_draft_usage
                    WHERE team_id=? AND owner_user_id=?
                      AND request_key_hash=?
                    """,
                    (team_id, owner_user_id, request_key_hash),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is not None:
                    await db.commit()
                    return
                user_update = await db.execute(
                    """
                    UPDATE users
                    SET credit_balance=credit_balance-?
                    WHERE id=? AND team_id=? AND status='active'
                      AND credit_balance>=?
                    """,
                    (
                        self._reserved_credits,
                        owner_user_id,
                        team_id,
                        self._reserved_credits,
                    ),
                )
                if user_update.rowcount != 1:
                    raise SubagentDraftCreditError(
                        "user_credit_exhausted",
                    )
                team_update = await db.execute(
                    """
                    UPDATE teams
                    SET team_credit_balance=team_credit_balance-?
                    WHERE id=? AND status='active'
                      AND team_credit_balance>=?
                    """,
                    (
                        self._reserved_credits,
                        team_id,
                        self._reserved_credits,
                    ),
                )
                if team_update.rowcount != 1:
                    raise SubagentDraftCreditError(
                        "team_pool_exhausted",
                    )
                await db.execute(
                    """
                    INSERT INTO credit_ledger(
                      id,user_id,team_id,workspace_id,turn_id,delta,reason,
                      tokens_input,tokens_output,created_at,rate_card_version
                    ) VALUES (?,?,?,NULL,?,?,?,0,0,?,'v2')
                    """,
                    (
                        ledger_id,
                        owner_user_id,
                        team_id,
                        f"sadreq_{request_key_hash[:12]}",
                        -self._reserved_credits,
                        "subagent_draft_reservation",
                        now_s,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO team_subagent_draft_usage(
                      team_id,owner_user_id,request_key_hash,state,
                      reserved_credits,actual_credits,input_tokens,
                      output_tokens,attempts,draft_id,
                      reservation_ledger_id,adjustment_ledger_id,
                      usage_event_id,lease_expires_at,created_at,updated_at
                    ) VALUES (
                      ?,?,?,'reserved',?,NULL,NULL,NULL,NULL,NULL,
                      ?,NULL,NULL,?,?,?
                    )
                    """,
                    (
                        team_id,
                        owner_user_id,
                        request_key_hash,
                        self._reserved_credits,
                        ledger_id,
                        lease_s,
                        now_s,
                        now_s,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def settle(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        request_key_hash: str,
        draft_id: str | None,
        input_tokens: int,
        output_tokens: int,
        attempts: int,
        model_usage: tuple[ModelUsageLine, ...] = (),
    ) -> None:
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT *
                    FROM team_subagent_draft_usage
                    WHERE team_id=? AND owner_user_id=?
                      AND request_key_hash=?
                    """,
                    (team_id, owner_user_id, request_key_hash),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise SubagentDraftCreditError(
                        "subagent_draft_credit_reservation_missing",
                    )
                version = row["billing_rate_card_version"] or "v1"
                lines = (
                    tuple(ModelUsageLine.from_json(item) for item in json.loads(row["model_usage_json"]))
                    if row["model_usage_json"] is not None
                    else model_usage
                )
                if version == "v2" and not lines:
                    lines = (ModelUsageLine(None, None, ModelTokenUsage(input_tokens, 0, output_tokens)),)
                actual = credits_for_model_lines(lines) if version == "v2" else credits_for_tokens(input_tokens, output_tokens)
                if str(row["state"]) == "settled":
                    await db.commit()
                    return
                if str(row["state"]) not in {
                    "reserved",
                    "settlement_pending",
                }:
                    raise SubagentDraftCreditError(
                        "subagent_draft_credit_reservation_released",
                    )
                if str(row["state"]) == "settlement_pending" and (
                    int(row["actual_credits"]) != actual
                    or int(row["input_tokens"]) != input_tokens
                    or int(row["output_tokens"]) != output_tokens
                    or int(row["attempts"]) != attempts
                    or row["draft_id"] != draft_id
                ):
                    raise SubagentDraftCreditError(
                        "subagent_draft_settlement_mismatch",
                    )
                reserved = int(row["reserved_credits"])
                adjustment = reserved - actual
                adjustment_ledger_id: str | None = None
                if adjustment != 0:
                    adjustment_ledger_id = _id("led")
                    if adjustment > 0:
                        await db.execute(
                            """
                            UPDATE users
                            SET credit_balance=credit_balance+?
                            WHERE id=? AND team_id=?
                            """,
                            (adjustment, owner_user_id, team_id),
                        )
                        await db.execute(
                            """
                            UPDATE teams
                            SET team_credit_balance=team_credit_balance+?
                            WHERE id=?
                            """,
                            (adjustment, team_id),
                        )
                    else:
                        extra = -adjustment
                        user_update = await db.execute(
                            """
                            UPDATE users
                            SET credit_balance=credit_balance-?
                            WHERE id=? AND team_id=? AND credit_balance>=?
                            """,
                            (extra, owner_user_id, team_id, extra),
                        )
                        team_update = await db.execute(
                            """
                            UPDATE teams
                            SET team_credit_balance=team_credit_balance-?
                            WHERE id=? AND team_credit_balance>=?
                            """,
                            (extra, team_id, extra),
                        )
                        if user_update.rowcount != 1 or team_update.rowcount != 1:
                            raise SubagentDraftCreditError(
                                "subagent_draft_reservation_underestimated",
                            )
                    await db.execute(
                        """
                        INSERT INTO credit_ledger(
                          id,user_id,team_id,workspace_id,turn_id,delta,
                          reason,tokens_input,tokens_output,created_at,rate_card_version
                        ) VALUES (?,?,?,NULL,?,?,?,0,0,?,?)
                        """,
                        (
                            adjustment_ledger_id,
                            owner_user_id,
                            team_id,
                            f"sadreq_{request_key_hash[:12]}",
                            adjustment,
                            ("subagent_draft_reservation_refund" if adjustment > 0 else "subagent_draft_reservation_extra"),
                            now_s,
                            version,
                        ),
                    )
                usage_event_id = _id("llmu")
                if version == "v2":
                    await _insert_credit_model_usage_in_tx(
                        db,
                        ledger_id=str(row["reservation_ledger_id"]),
                        model_usage=lines,
                    )
                await db.execute(
                    """
                    INSERT INTO llm_usage_events(
                      id,team_id,user_id,workspace_id,turn_id,reason,
                      agent_type,provider,model,tokens_input,tokens_output,
                      tokens_cached,streamed,created_at
                    ) VALUES (
                      ?,?,?,NULL,?,'subagent_draft_generation',
                      'subagent_draft',NULL,NULL,?,?,0,0,?
                    )
                    """,
                    (
                        usage_event_id,
                        team_id,
                        owner_user_id,
                        f"sadreq_{request_key_hash[:12]}",
                        input_tokens,
                        output_tokens,
                        now_s,
                    ),
                )
                await db.execute(
                    """
                    UPDATE team_subagent_draft_usage
                    SET state='settled',actual_credits=?,input_tokens=?,
                        output_tokens=?,attempts=?,draft_id=?,
                        adjustment_ledger_id=?,usage_event_id=?,updated_at=?
                    WHERE team_id=? AND owner_user_id=?
                      AND request_key_hash=?
                      AND state IN ('reserved', 'settlement_pending')
                    """,
                    (
                        actual,
                        input_tokens,
                        output_tokens,
                        attempts,
                        draft_id,
                        adjustment_ledger_id,
                        usage_event_id,
                        now_s,
                        team_id,
                        owner_user_id,
                        request_key_hash,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def prepare_settlement(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        request_key_hash: str,
        draft_id: str | None,
        input_tokens: int,
        output_tokens: int,
        attempts: int,
        model_usage: tuple[ModelUsageLine, ...] = (),
    ) -> None:
        """Persist recoverable usage before applying credit adjustments."""
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT state,actual_credits,input_tokens,output_tokens,
                           attempts,draft_id,billing_rate_card_version,model_usage_json
                    FROM team_subagent_draft_usage
                    WHERE team_id=? AND owner_user_id=?
                      AND request_key_hash=?
                    """,
                    (team_id, owner_user_id, request_key_hash),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise SubagentDraftCreditError(
                        "subagent_draft_credit_reservation_missing",
                    )
                version = row["billing_rate_card_version"] or "v1"
                lines = model_usage
                if version == "v2" and not lines:
                    lines = (ModelUsageLine(None, None, ModelTokenUsage(input_tokens, 0, output_tokens)),)
                actual = credits_for_model_lines(lines) if version == "v2" else credits_for_tokens(input_tokens, output_tokens)
                lines_json = json.dumps([line.as_json() for line in lines], sort_keys=True) if version == "v2" else None
                state = str(row["state"])
                if state == "settled":
                    await db.commit()
                    return
                if state == "released":
                    raise SubagentDraftCreditError(
                        "subagent_draft_credit_reservation_released",
                    )
                if state == "settlement_pending":
                    if (
                        int(row["actual_credits"]) != actual
                        or int(row["input_tokens"]) != input_tokens
                        or int(row["output_tokens"]) != output_tokens
                        or int(row["attempts"]) != attempts
                        or row["draft_id"] != draft_id
                        or row["model_usage_json"] != lines_json
                    ):
                        raise SubagentDraftCreditError(
                            "subagent_draft_settlement_mismatch",
                        )
                    await db.commit()
                    return
                cursor = await db.execute(
                    """
                    UPDATE team_subagent_draft_usage
                    SET state='settlement_pending',actual_credits=?,
                        input_tokens=?,output_tokens=?,attempts=?,draft_id=?,
                        model_usage_json=?,updated_at=?
                    WHERE team_id=? AND owner_user_id=?
                      AND request_key_hash=? AND state='reserved'
                    """,
                    (
                        actual,
                        input_tokens,
                        output_tokens,
                        attempts,
                        draft_id,
                        lines_json,
                        now_s,
                        team_id,
                        owner_user_id,
                        request_key_hash,
                    ),
                )
                if cursor.rowcount != 1:
                    raise SubagentDraftCreditError(
                        "subagent_draft_settlement_prepare_conflict",
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def settle_pending(self, *, limit: int = 100) -> int:
        """Retry bounded recoverable settlements left by a transient failure."""
        if not 1 <= limit <= 1000:
            raise ValueError("settlement limit must be in [1,1000]")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT team_id,owner_user_id,request_key_hash,draft_id,
                       input_tokens,output_tokens,attempts
                FROM team_subagent_draft_usage
                WHERE state='settlement_pending'
                ORDER BY updated_at
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        settled = 0
        for row in rows:
            try:
                await self.settle(
                    team_id=str(row["team_id"]),
                    owner_user_id=str(row["owner_user_id"]),
                    request_key_hash=str(row["request_key_hash"]),
                    draft_id=(str(row["draft_id"]) if row["draft_id"] is not None else None),
                    input_tokens=int(row["input_tokens"]),
                    output_tokens=int(row["output_tokens"]),
                    attempts=int(row["attempts"]),
                )
                settled += 1
            except Exception:
                logger.exception(
                    "subagent_draft.pending_settlement_failed request=%s",
                    str(row["request_key_hash"])[:12],
                )
        return settled

    async def release(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        request_key_hash: str,
        reason_code: str,
    ) -> None:
        del reason_code  # stable product code is kept by the request claim
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._release_one(
                    db,
                    team_id=team_id,
                    owner_user_id=owner_user_id,
                    request_key_hash=request_key_hash,
                    now_s=_timestamp(self._clock.now()),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def release_stale(self, *, limit: int = 100) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError("release limit must be in [1,1000]")
        now_s = _timestamp(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT team_id,owner_user_id,request_key_hash
                    FROM team_subagent_draft_usage
                    WHERE state='reserved' AND lease_expires_at<=?
                    ORDER BY lease_expires_at
                    LIMIT ?
                    """,
                    (now_s, limit),
                ) as cursor:
                    rows = await cursor.fetchall()
                released = 0
                for row in rows:
                    released += await self._release_one(
                        db,
                        team_id=str(row["team_id"]),
                        owner_user_id=str(row["owner_user_id"]),
                        request_key_hash=str(row["request_key_hash"]),
                        now_s=now_s,
                    )
                await db.commit()
                return released
            except BaseException:
                await db.rollback()
                raise

    async def _release_one(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        owner_user_id: str,
        request_key_hash: str,
        now_s: str,
    ) -> int:
        async with db.execute(
            """
            SELECT reserved_credits
            FROM team_subagent_draft_usage
            WHERE team_id=? AND owner_user_id=?
              AND request_key_hash=? AND state='reserved'
            """,
            (team_id, owner_user_id, request_key_hash),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return 0
        reserved = int(row["reserved_credits"])
        ledger_id = _id("led")
        await db.execute(
            """
            UPDATE users SET credit_balance=credit_balance+?
            WHERE id=? AND team_id=?
            """,
            (reserved, owner_user_id, team_id),
        )
        await db.execute(
            """
            UPDATE teams SET team_credit_balance=team_credit_balance+?
            WHERE id=?
            """,
            (reserved, team_id),
        )
        await db.execute(
            """
            INSERT INTO credit_ledger(
              id,user_id,team_id,workspace_id,turn_id,delta,reason,
              tokens_input,tokens_output,created_at
            ) VALUES (?,?,?,NULL,?,?,?,0,0,?)
            """,
            (
                ledger_id,
                owner_user_id,
                team_id,
                f"sadreq_{request_key_hash[:12]}",
                reserved,
                "subagent_draft_reservation_release",
                now_s,
            ),
        )
        cursor = await db.execute(
            """
            UPDATE team_subagent_draft_usage
            SET state='released',actual_credits=0,input_tokens=0,
                output_tokens=0,attempts=1,adjustment_ledger_id=?,
                updated_at=?
            WHERE team_id=? AND owner_user_id=?
              AND request_key_hash=? AND state='reserved'
            """,
            (
                ledger_id,
                now_s,
                team_id,
                owner_user_id,
                request_key_hash,
            ),
        )
        return 1 if cursor.rowcount == 1 else 0


__all__ = [
    "SubagentDraftCreditAccountant",
    "SubagentDraftCreditError",
]
