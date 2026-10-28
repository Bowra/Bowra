"""Team policy history; tightening is permanent for already admitted material.

A record captures the current monotonically increasing version, not wall-clock
ordering. The SQL retention views apply the shortest policy from that version
onwards. Later loosening therefore cannot resurrect material before cleanup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.user_store import _read_team_role_in_tx

MAX_REFERENCE_RETENTION_DAYS = 180


class ReferenceRetentionPolicyError(ValueError):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class ReferenceRetentionPolicy:
    days: int = MAX_REFERENCE_RETENTION_DAYS
    version: int = 0


async def read_retention_policy_in_connection(db: aiosqlite.Connection, *, team_id: str) -> ReferenceRetentionPolicy:
    if not db.in_transaction:
        raise ValueError("reference_transaction_required")
    async with db.execute(
        "SELECT days,version FROM team_reference_retention_policies WHERE team_id=? ORDER BY version DESC LIMIT 1", (team_id,)
    ) as cur:
        row = await cur.fetchone()
    return ReferenceRetentionPolicy(row[0], row[1]) if row else ReferenceRetentionPolicy()


async def _require_role(db: aiosqlite.Connection, *, team_id: str, user_id: str, write: bool) -> None:
    role = await _read_team_role_in_tx(db, user_id=user_id, team_id=team_id, require_active_principal=True)
    if role is None or (write and role not in {"owner", "team_admin"}):
        raise ReferenceRetentionPolicyError("reference_retention_policy_forbidden", 403)


async def get_retention_policy(db_path: str | Path, *, team_id: str, user_id: str) -> ReferenceRetentionPolicy:
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        await _require_role(db, team_id=team_id, user_id=user_id, write=False)
        return await read_retention_policy_in_connection(db, team_id=team_id)


async def set_retention_policy(db_path: str | Path, *, team_id: str, user_id: str, days: int, expected_version: int) -> ReferenceRetentionPolicy:
    if type(days) is not int or not 1 <= days <= MAX_REFERENCE_RETENTION_DAYS or type(expected_version) is not int or expected_version < 0:
        raise ReferenceRetentionPolicyError("reference_retention_policy_invalid", 422)
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _require_role(db, team_id=team_id, user_id=user_id, write=True)
            current = await read_retention_policy_in_connection(db, team_id=team_id)
            if current.version != expected_version:
                raise ReferenceRetentionPolicyError("reference_retention_policy_conflict", 409)
            if current.days == days:
                await db.rollback()
                return current
            result = ReferenceRetentionPolicy(days, current.version + 1)
            await db.execute(
                "INSERT INTO team_reference_retention_policies(team_id,version,days,updated_by,updated_at) VALUES(?,?,?,?,?)",
                (team_id, result.version, days, user_id, datetime.now(UTC).isoformat()),
            )
            await db.commit()
            return result
        except BaseException:
            if db.in_transaction:
                await db.rollback()
            raise
