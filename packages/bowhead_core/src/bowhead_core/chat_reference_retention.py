"""Bounded private-material cleanup, independent of writer flags and user access.

Stored deadlines are immutable ceilings. This worker needs no decryption key or
active membership, and cannot revive content, dispatch work or emit Turn events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from bowhead_core._db import open_shared_ctx


@dataclass(frozen=True, slots=True)
class ReferenceRetentionResult:
    plans: int = 0
    queued_inputs: int = 0


async def purge_expired_reference_material(db_path: str | Path, *, limit: int = 100) -> ReferenceRetentionResult:
    """Purge at most ``limit`` plan families and ``limit`` queue records per pass.

    Also catches deleted Sessions from before 0067. A shorter captured-material
    deadline invalidates its whole plan; no later prepare may refill that plan.
    Time is taken under the write lock. SQLite date conversion respects offsets in
    historical deadlines; malformed dates are treated as unavailable, not immortal.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("reference_retention_limit_invalid")
    async with open_shared_ctx(str(db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_reference_plans'") as cur:
                if await cur.fetchone() is None:
                    await db.rollback()
                    return ReferenceRetentionResult()
            # Do not silently run partial cleanup on an incompletely upgraded DB.
            async with db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE "
                "(type='trigger' AND name='trg_reference_session_delete') OR "
                "(type='view' AND name IN ('chat_reference_plan_retention','chat_reference_queue_retention'))"
            ) as cur:
                if (await cur.fetchone())[0] != 3:
                    raise RuntimeError("reference_retention_schema_unavailable")
            now = datetime.now(UTC).isoformat()
            plans = await db.execute(
                """
                UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id IN (
                  SELECT p.turn_id FROM chat_reference_plans p JOIN chat_reference_plan_retention r ON r.turn_id=p.turn_id
                  WHERE p.retention_state='available' AND (NOT r.available OR COALESCE(r.deadline<=julianday(?),1))
                  ORDER BY r.deadline,p.turn_id LIMIT ?
                )
                """,
                (now, limit),
            )
            queued = await db.execute(
                """
                UPDATE chat_reference_queued_inputs SET retention_state='purged',ciphertext=NULL,
                  state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,
                  cancel_reason=CASE WHEN state='queued' THEN
                    CASE WHEN EXISTS(SELECT 1 FROM sessions s WHERE s.id=chat_reference_queued_inputs.session_id
                      AND s.deleted_at IS NOT NULL) THEN 'source_deleted' ELSE 'retention_expired' END
                    ELSE cancel_reason END,row_version=row_version+1
                WHERE (team_id,user_id,client_submission_id) IN (
                  SELECT q.team_id,q.user_id,q.client_submission_id FROM chat_reference_queued_inputs q
                  JOIN chat_reference_queue_retention r ON r.team_id=q.team_id AND r.user_id=q.user_id
                    AND r.client_submission_id=q.client_submission_id
                  WHERE q.retention_state='available' AND (NOT r.available OR COALESCE(r.deadline<=julianday(?),1))
                  ORDER BY r.deadline,q.team_id,q.user_id,q.client_submission_id LIMIT ?
                )
                """,
                (now, limit),
            )
            result = ReferenceRetentionResult(plans.rowcount, queued.rowcount)
            await db.commit()
            return result
        except BaseException:
            if db.in_transaction:
                await db.rollback()
            raise


async def reference_material_available_in_connection(db: aiosqlite.Connection, *, turn_id: str, now: datetime | None = None) -> bool:
    """Execution liveness check only; does not replace resource authorization."""
    from bowhead_core.chat_reference_storage import _row, _transaction

    _transaction(db)
    row = await _row(
        db,
        "SELECT available AND COALESCE(deadline>julianday(?),0) AS live FROM chat_reference_plan_retention WHERE turn_id=?",
        ((now or datetime.now(UTC)).isoformat(), turn_id),
    )
    return row is not None and bool(row["live"])
