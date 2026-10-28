"""Durable single-generation identity for the Owner proposal HTTP entry point.

No request is reclaimed for another model invocation. Successful retries return
the stored proposal; interrupted and uncertain attempts require explicit review.
Only hashes and domain references are retained, not private prompts or secrets.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_change_proposal_store import (
    AgentChangeProposalError,
    AgentProposalBaseline,
    _document_from_working,
    _owned_working_conn,
    _proposal_by_source_conn,
    _require_owner_conn,
)


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class CoachGenerationClaim:
    id: str
    team_id: str
    agent_id: str
    owner_user_id: str
    key_hash: str
    request_hash: str
    source_ref: str
    baseline: AgentProposalBaseline
    state: str
    result_proposal_id: str | None
    failure_code: str | None
    response_deadline: str
    new: bool = False
    config_surface: dict[str, Any] | None = None

    async def require_conn(self, db: aiosqlite.Connection) -> None:
        await _require_owner_conn(db, team_id=self.team_id, agent_id=self.agent_id, actor_user_id=self.owner_user_id)
        async with db.execute("SELECT * FROM agent_coach_generation_request WHERE id=?", (self.id,)) as cursor:
            row = await cursor.fetchone()
        if row is None or any(
            row[name] != getattr(self, name) for name in ("team_id", "agent_id", "owner_user_id", "key_hash", "request_hash", "source_ref")
        ):
            raise AgentChangeProposalError("agent_coach_request_conflict")
        if (row["working_copy_id"], row["working_copy_version"], row["base_release_id"]) != (
            self.baseline.working_copy_id,
            self.baseline.working_copy_row_version,
            self.baseline.base_release_id,
        ) or row["state"] not in {"running", "unknown", "succeeded"}:
            raise AgentChangeProposalError("agent_coach_request_conflict")

    async def complete_conn(self, db: aiosqlite.Connection, proposal_id: str) -> None:
        await self.require_conn(db)
        await db.execute(
            "UPDATE agent_coach_generation_request SET state='succeeded',result_proposal_id=?,failure_code=NULL,updated_at=? WHERE id=?",
            (proposal_id, datetime.now(UTC).isoformat(), self.id),
        )


class CoachGenerationStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def authorize_invocation(self, claim: CoachGenerationClaim) -> None:
        """Reserve the single invocation immediately before the provider boundary."""
        now = datetime.now(UTC).isoformat()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await claim.require_conn(db)
                cursor = await db.execute(
                    """UPDATE agent_coach_generation_request SET provider_started_at=?,updated_at=?
                       WHERE id=? AND state='running' AND provider_started_at IS NULL AND response_deadline>?""",
                    (now, now, claim.id, now),
                )
                if cursor.rowcount != 1:
                    raise AgentChangeProposalError("agent_coach_invocation_conflict")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def begin(
        self,
        *,
        team_id: str,
        agent_id: str,
        owner_user_id: str,
        idempotency_key: str,
        intent: dict[str, Any],
        timeout_seconds: float,
    ) -> CoachGenerationClaim:
        if not idempotency_key or len(idempotency_key) > 200 or not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise AgentChangeProposalError("agent_coach_request_invalid")
        encoded = json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 256 * 1024:
            raise AgentChangeProposalError("agent_coach_request_too_large")
        key_hash, request_hash = _hash(idempotency_key), _hash(encoded)
        source_ref = intent.get("source_ref") or f"owner:{idempotency_key}"
        if not isinstance(source_ref, str) or not source_ref.strip() or len(source_ref) > 240:
            raise AgentChangeProposalError("agent_coach_request_invalid")
        source_ref = source_ref.strip()
        now = datetime.now(UTC)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=owner_user_id)
                async with db.execute(
                    "SELECT * FROM agent_coach_generation_request WHERE team_id=? AND agent_id=? AND owner_user_id=? AND key_hash=?",
                    (team_id, agent_id, owner_user_id, key_hash),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is not None:
                    if row["request_hash"] != request_hash or row["source_ref"] != source_ref:
                        raise AgentChangeProposalError("agent_change_proposal_idempotency_conflict")
                    await db.commit()
                    return self._claim(row, now=now)
                async with db.execute(
                    "SELECT 1 FROM agent_coach_generation_request WHERE team_id=? AND agent_id=? AND source_ref=?", (team_id, agent_id, source_ref)
                ) as cursor:
                    occupied = await cursor.fetchone()
                if occupied or await _proposal_by_source_conn(db, team_id=team_id, agent_id=agent_id, source_type="coach", source_ref=source_ref):
                    # Historical proposals have no original goal hash. Do not
                    # pretend an old key proves identical intent or pay again.
                    raise AgentChangeProposalError("agent_coach_source_conflict")
                working, _ = await _owned_working_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=owner_user_id)
                request_id = "acgr_" + uuid4().hex
                await db.execute(
                    """INSERT INTO agent_coach_generation_request(
                       id,team_id,agent_id,owner_user_id,key_hash,request_hash,source_ref,working_copy_id,working_copy_version,base_release_id,
                       state,created_at,response_deadline,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'running',?,?,?)""",
                    (
                        request_id,
                        team_id,
                        agent_id,
                        owner_user_id,
                        key_hash,
                        request_hash,
                        source_ref,
                        working["id"],
                        working["row_version"],
                        working["base_release_id"],
                        now.isoformat(),
                        (now + timedelta(seconds=timeout_seconds + 60)).isoformat(),
                        now.isoformat(),
                    ),
                )
                async with db.execute("SELECT * FROM agent_coach_generation_request WHERE id=?", (request_id,)) as cursor:
                    row = await cursor.fetchone()
                await db.commit()
                return self._claim(row, now=now, config_surface=_document_from_working(working))
            except BaseException:
                await db.rollback()
                raise

    async def fail(self, claim: CoachGenerationClaim, *, unknown: bool, code: str) -> None:
        # A stale worker may record its failed/uncertain outcome after losing
        # ownership; it cannot authorize another invocation or a green result.
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                """UPDATE agent_coach_generation_request SET state=?,failure_code=?,updated_at=?
                   WHERE id=? AND team_id=? AND agent_id=? AND owner_user_id=? AND state='running'""",
                (
                    "unknown" if unknown else "failed",
                    code,
                    datetime.now(UTC).isoformat(),
                    claim.id,
                    claim.team_id,
                    claim.agent_id,
                    claim.owner_user_id,
                ),
            )
            await db.commit()

    @staticmethod
    def _claim(row, *, now: datetime, config_surface: dict | None = None) -> CoachGenerationClaim:
        state = row["state"]
        if state == "running" and row["response_deadline"] < now.isoformat():
            state = "unknown"
        return CoachGenerationClaim(
            id=row["id"],
            team_id=row["team_id"],
            agent_id=row["agent_id"],
            owner_user_id=row["owner_user_id"],
            key_hash=row["key_hash"],
            request_hash=row["request_hash"],
            source_ref=row["source_ref"],
            baseline=AgentProposalBaseline(row["working_copy_id"], row["working_copy_version"], row["base_release_id"]),
            state=state,
            result_proposal_id=row["result_proposal_id"],
            failure_code=row["failure_code"],
            response_deadline=row["response_deadline"],
            new=config_surface is not None,
            config_surface=config_surface,
        )
