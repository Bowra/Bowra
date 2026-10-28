"""Transactional Plan, Batch and Item persistence for model verification V2."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.llm_config_crypto import (
    decrypt_llm_config,
    encrypt_llm_config,
)
from bowhead_core.team_llm_evidence import SIGNATURE_VERSION

if TYPE_CHECKING:
    from pathlib import Path

_TERMINAL_ITEM_STATES = frozenset(
    {
        "verified",
        "failed",
        "not_testable",
        "errored",
        "cancelled",
        "interrupted",
    }
)


class TeamLlmBatchStoreError(RuntimeError):
    code = "verification_batch_error"


class PlanNotFound(TeamLlmBatchStoreError):
    code = "verification_plan_not_found"


class PlanExpired(TeamLlmBatchStoreError):
    code = "verification_plan_expired"


class IdempotencyConflict(TeamLlmBatchStoreError):
    code = "idempotency_conflict"


class BatchOwnershipLost(TeamLlmBatchStoreError):
    code = "verification_item_ownership_lost"


@dataclass(frozen=True, slots=True)
class VerificationPlanItem:
    provider_id: str
    provider_display_name: str
    model_id: str
    capability: str
    config_signature: str
    testability: Literal["testable", "not_testable"]
    testability_contract_version: str
    probe_contract_version: str | None
    probe_execution_policy_version: str
    provider_request_count: int
    safe_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_display_name": self.provider_display_name,
            "model_id": self.model_id,
            "capability": self.capability,
            "config_signature": self.config_signature,
            "testability": self.testability,
            "testability_contract_version": self.testability_contract_version,
            "probe_contract_version": self.probe_contract_version,
            "probe_execution_policy_version": self.probe_execution_policy_version,
            "provider_request_count": self.provider_request_count,
            "safe_code": self.safe_code,
        }


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    plan_id: str
    team_id: str
    actor_user_id: str
    request_hash: str
    items: tuple[VerificationPlanItem, ...]
    provider_request_count: int
    created_at: datetime
    expires_at: datetime
    consumed_batch_id: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationBatch:
    batch_id: str
    team_id: str
    actor_user_id: str
    plan_id: str
    base_config_version: int
    state: str
    revision: int
    created_at: datetime
    runner_id: str | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ClaimedItem:
    item_id: str
    batch_id: str
    team_id: str
    provider_id: str
    model_id: str
    capability: str
    config_signature: str
    testability_contract_version: str
    probe_contract_version: str | None
    probe_execution_policy_version: str
    request_count: int
    runner_id: str


@dataclass(frozen=True, slots=True)
class ItemCompletion:
    state: Literal[
        "verified",
        "failed",
        "not_testable",
        "errored",
        "cancelled",
        "interrupted",
    ]
    safe_code: str
    safe_http_status: int | None
    safe_summary_code: str | None
    duration_ms: int
    vector_dimension: int | None = None
    #: Provider-reported model ID. Diagnostic only when it differs from config, but always record it:
    #: without the actual returned ID, operators cannot tell whether a mismatch reflects bad config or a bad criterion.
    actual_model_id: str | None = None


@dataclass(frozen=True, slots=True)
class RetentionSweepResult:
    evidence: int
    items: int
    batches: int
    plans: int


def _id(prefix: str) -> str:
    return f"{prefix}{uuid4().hex}"


def _iso(value: datetime) -> str:
    normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _plan_item(raw: dict[str, Any]) -> VerificationPlanItem:
    return VerificationPlanItem(
        provider_id=str(raw["provider_id"]),
        provider_display_name=str(raw["provider_display_name"]),
        model_id=str(raw["model_id"]),
        capability=str(raw["capability"]),
        config_signature=str(raw["config_signature"]),
        testability=str(raw["testability"]),  # type: ignore[arg-type]
        testability_contract_version=str(raw["testability_contract_version"]),
        probe_contract_version=(str(raw["probe_contract_version"]) if raw.get("probe_contract_version") else None),
        probe_execution_policy_version=str(raw["probe_execution_policy_version"]),
        provider_request_count=int(raw["provider_request_count"]),
        safe_code=raw.get("safe_code"),
    )


class TeamLlmBatchStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def create_plan(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        request_hash: str,
        items: list[VerificationPlanItem],
        now: datetime | None = None,
        ttl: timedelta = timedelta(minutes=15),
    ) -> VerificationPlan:
        timestamp = now or datetime.now(UTC)
        plan = VerificationPlan(
            plan_id=_id("lvplan_"),
            team_id=team_id,
            actor_user_id=actor_user_id,
            request_hash=request_hash,
            items=tuple(items),
            provider_request_count=sum(item.provider_request_count for item in items),
            created_at=timestamp,
            expires_at=timestamp + ttl,
        )
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO team_llm_verification_plans(
                    id, team_id, actor_user_id, request_hash, items_json,
                    item_count, provider_request_count, created_at, expires_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    plan.plan_id,
                    team_id,
                    actor_user_id,
                    request_hash,
                    json.dumps(
                        [item.as_dict() for item in items],
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    len(items),
                    plan.provider_request_count,
                    _iso(timestamp),
                    _iso(plan.expires_at),
                ),
            )
            await db.commit()
        return plan

    async def get_plan(
        self,
        *,
        team_id: str,
        plan_id: str,
    ) -> VerificationPlan | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT * FROM team_llm_verification_plans
                    WHERE team_id = ? AND id = ?
                    """,
                    (team_id, plan_id),
                )
            ).fetchone()
        if row is None:
            return None
        raw_items = json.loads(str(row["items_json"]))
        return VerificationPlan(
            plan_id=str(row["id"]),
            team_id=str(row["team_id"]),
            actor_user_id=str(row["actor_user_id"]),
            request_hash=str(row["request_hash"]),
            items=tuple(_plan_item(item) for item in raw_items),
            provider_request_count=int(row["provider_request_count"]),
            created_at=_datetime(row["created_at"]),
            expires_at=_datetime(row["expires_at"]),
            consumed_batch_id=(str(row["consumed_batch_id"]) if row["consumed_batch_id"] else None),
        )

    async def create_batch(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        plan_id: str,
        base_config_version: int,
        idempotency_key: str,
        request_hash: str,
        probe_context: dict[str, Any],
        runner_id: str,
        now: datetime | None = None,
        lease_ttl: timedelta = timedelta(seconds=45),
    ) -> VerificationBatch:
        encrypted = encrypt_llm_config(probe_context)
        if not encrypted.startswith("FERNET:"):
            raise TeamLlmBatchStoreError("BOWHEAD_FERNET_KEY is required for verification batches")
        timestamp = now or datetime.now(UTC)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    """
                    SELECT * FROM team_llm_verification_batches
                    WHERE team_id = ? AND actor_user_id = ?
                      AND idempotency_key = ?
                    """,
                    (team_id, actor_user_id, idempotency_key),
                )
            ).fetchone()
            if existing is not None:
                await db.commit()
                if str(existing["request_hash"]) != request_hash:
                    raise IdempotencyConflict("Idempotency-Key was reused with a different request")
                return _batch_from_row(existing, replayed=True)

            plan_row = await (
                await db.execute(
                    """
                    SELECT * FROM team_llm_verification_plans
                    WHERE team_id = ? AND actor_user_id = ? AND id = ?
                    """,
                    (team_id, actor_user_id, plan_id),
                )
            ).fetchone()
            if plan_row is None:
                await db.rollback()
                raise PlanNotFound("verification Plan was not found")
            if _datetime(plan_row["expires_at"]) < timestamp:
                await db.rollback()
                raise PlanExpired("verification Plan expired")
            if plan_row["consumed_batch_id"]:
                consumed = await (
                    await db.execute(
                        """
                        SELECT * FROM team_llm_verification_batches
                        WHERE team_id = ? AND id = ?
                        """,
                        (team_id, str(plan_row["consumed_batch_id"])),
                    )
                ).fetchone()
                await db.commit()
                if consumed is None:
                    raise PlanNotFound("consumed verification Batch is missing")
                return _batch_from_row(consumed, replayed=True)
            if str(plan_row["request_hash"]) != request_hash:
                await db.rollback()
                raise IdempotencyConflict("verification Plan no longer matches the request")

            batch_id = _id("lvb_")
            await db.execute(
                """
                INSERT INTO team_llm_verification_batches(
                    id, team_id, actor_user_id, plan_id, base_config_version,
                    idempotency_key, request_hash, state,
                    probe_context_ciphertext, revision, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    batch_id,
                    team_id,
                    actor_user_id,
                    plan_id,
                    base_config_version,
                    idempotency_key,
                    request_hash,
                    "queued",
                    encrypted,
                    0,
                    _iso(timestamp),
                ),
            )
            raw_items = json.loads(str(plan_row["items_json"]))
            for ordinal, raw in enumerate(raw_items):
                await db.execute(
                    """
                    INSERT INTO team_llm_verification_items(
                        id, batch_id, team_id, ordinal, provider_id, model_id,
                        capability, config_signature,
                        testability_contract_version, probe_contract_version,
                        probe_execution_policy_version, request_count, state,
                        runner_id, lease_until, revision
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        _id("lvi_"),
                        batch_id,
                        team_id,
                        ordinal,
                        raw["provider_id"],
                        raw["model_id"],
                        raw["capability"],
                        raw["config_signature"],
                        raw["testability_contract_version"],
                        raw.get("probe_contract_version"),
                        raw["probe_execution_policy_version"],
                        raw["provider_request_count"],
                        "queued",
                        runner_id,
                        _iso(timestamp + lease_ttl),
                        0,
                    ),
                )
            await db.execute(
                """
                UPDATE team_llm_verification_plans
                SET consumed_batch_id = ?, consumed_at = ?
                WHERE id = ? AND consumed_batch_id IS NULL
                """,
                (batch_id, _iso(timestamp), plan_id),
            )
            await db.commit()
        return VerificationBatch(
            batch_id=batch_id,
            team_id=team_id,
            actor_user_id=actor_user_id,
            plan_id=plan_id,
            base_config_version=base_config_version,
            state="queued",
            revision=0,
            created_at=timestamp,
            runner_id=runner_id,
        )

    async def get_probe_context(
        self,
        *,
        team_id: str,
        batch_id: str,
    ) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            row = await (
                await db.execute(
                    """
                    SELECT probe_context_ciphertext
                    FROM team_llm_verification_batches
                    WHERE team_id = ? AND id = ?
                    """,
                    (team_id, batch_id),
                )
            ).fetchone()
        if row is None or not row[0]:
            raise PlanNotFound("verification Batch context is unavailable")
        return decrypt_llm_config(str(row[0]))

    async def heartbeat_runner(
        self,
        *,
        team_id: str,
        batch_id: str,
        runner_id: str,
        now: datetime | None = None,
        lease_ttl: timedelta = timedelta(seconds=45),
    ) -> int:
        """Extend only this runner's live Item leases."""

        timestamp = now or datetime.now(UTC)
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE team_llm_verification_items
                SET lease_until = ?
                WHERE team_id = ? AND batch_id = ? AND runner_id = ?
                  AND state IN ('queued', 'verifying')
                  AND lease_until >= ?
                """,
                (
                    _iso(timestamp + lease_ttl),
                    team_id,
                    batch_id,
                    runner_id,
                    _iso(timestamp),
                ),
            )
            await db.commit()
        return max(0, cursor.rowcount)

    async def claim_next_item(
        self,
        *,
        team_id: str,
        batch_id: str,
        runner_id: str,
        now: datetime | None = None,
        lease_ttl: timedelta = timedelta(seconds=45),
        team_concurrency: int = 3,
    ) -> ClaimedItem | None:
        timestamp = now or datetime.now(UTC)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await self._interrupt_expired_items(db, team_id, timestamp)
            active = await (
                await db.execute(
                    """
                    SELECT COUNT(*) FROM team_llm_verification_items
                    WHERE team_id = ? AND state = 'verifying'
                      AND lease_until >= ?
                    """,
                    (team_id, _iso(timestamp)),
                )
            ).fetchone()
            if int(active[0]) >= team_concurrency:
                await db.commit()
                return None
            row = await (
                await db.execute(
                    """
                    SELECT * FROM team_llm_verification_items
                    WHERE team_id = ? AND batch_id = ? AND runner_id = ?
                      AND state = 'queued' AND lease_until >= ?
                    ORDER BY ordinal
                    LIMIT 1
                    """,
                    (team_id, batch_id, runner_id, _iso(timestamp)),
                )
            ).fetchone()
            if row is None:
                await self._refresh_batch_state(db, batch_id, timestamp)
                await db.commit()
                return None
            revision = await self._next_revision(db, batch_id)
            cursor = await db.execute(
                """
                UPDATE team_llm_verification_items
                SET state = 'verifying', request_started_at = ?,
                    lease_until = ?, revision = ?
                WHERE id = ? AND state = 'queued' AND runner_id = ?
                """,
                (
                    _iso(timestamp),
                    _iso(timestamp + lease_ttl),
                    revision,
                    row["id"],
                    runner_id,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            await db.execute(
                """
                UPDATE team_llm_verification_batches
                SET state = 'running', started_at = COALESCE(started_at, ?)
                WHERE id = ?
                """,
                (_iso(timestamp), batch_id),
            )
            await db.commit()
        return ClaimedItem(
            item_id=str(row["id"]),
            batch_id=str(row["batch_id"]),
            team_id=str(row["team_id"]),
            provider_id=str(row["provider_id"]),
            model_id=str(row["model_id"]),
            capability=str(row["capability"]),
            config_signature=str(row["config_signature"]),
            testability_contract_version=str(row["testability_contract_version"]),
            probe_contract_version=(str(row["probe_contract_version"]) if row["probe_contract_version"] else None),
            probe_execution_policy_version=str(row["probe_execution_policy_version"]),
            request_count=int(row["request_count"]),
            runner_id=runner_id,
        )

    async def complete_item(
        self,
        *,
        item: ClaimedItem,
        completion: ItemCompletion,
        initiated_by: str,
        provider_display_name: str,
        now: datetime | None = None,
        fresh_for: timedelta = timedelta(hours=24),
    ) -> str:
        timestamp = now or datetime.now(UTC)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            current = await (
                await db.execute(
                    """
                    SELECT state, runner_id, lease_until, cancel_requested_at
                    FROM team_llm_verification_items
                    WHERE team_id = ? AND batch_id = ? AND id = ?
                    """,
                    (item.team_id, item.batch_id, item.item_id),
                )
            ).fetchone()
            if (
                current is None
                or str(current["state"]) != "verifying"
                or str(current["runner_id"]) != item.runner_id
                or _datetime(current["lease_until"]) < timestamp
            ):
                await db.rollback()
                raise BatchOwnershipLost("verification Item lease is no longer owned")
            state = "cancelled" if current["cancel_requested_at"] else completion.state
            safe_code = "cancelled" if current["cancel_requested_at"] else completion.safe_code
            revision = await self._next_revision(db, item.batch_id)
            await db.execute(
                """
                UPDATE team_llm_verification_items
                SET state = ?, safe_code = ?, safe_http_status = ?,
                    safe_summary_code = ?, duration_ms = ?, completed_at = ?,
                    lease_until = NULL, revision = ?,
                    actual_model_id = ?
                WHERE id = ? AND state = 'verifying' AND runner_id = ?
                """,
                (
                    state,
                    safe_code,
                    completion.safe_http_status,
                    completion.safe_summary_code,
                    completion.duration_ms,
                    _iso(timestamp),
                    revision,
                    completion.actual_model_id,
                    item.item_id,
                    item.runner_id,
                ),
            )
            if state in {"verified", "failed", "not_testable"}:
                await db.execute(
                    """
                    INSERT INTO team_llm_capability_evidence(
                        id, team_id, provider_id,
                        provider_display_name_snapshot, model_id, capability,
                        config_signature, signature_version,
                        testability_contract_version, probe_contract_version,
                        probe_execution_policy_version, outcome, safe_code,
                        safe_http_status, safe_summary_code, vector_dimension,
                        batch_id, item_id, initiated_by, observed_at,
                        fresh_until, duration_ms, actual_model_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        _id("lve_"),
                        item.team_id,
                        item.provider_id,
                        provider_display_name,
                        item.model_id,
                        item.capability,
                        item.config_signature,
                        SIGNATURE_VERSION,
                        item.testability_contract_version,
                        item.probe_contract_version,
                        item.probe_execution_policy_version,
                        state,
                        safe_code,
                        completion.safe_http_status,
                        completion.safe_summary_code,
                        completion.vector_dimension,
                        item.batch_id,
                        item.item_id,
                        initiated_by,
                        _iso(timestamp),
                        _iso(timestamp + fresh_for),
                        completion.duration_ms,
                        completion.actual_model_id,
                    ),
                )
            await self._refresh_batch_state(db, item.batch_id, timestamp)
            await db.commit()
        return state

    async def cancel_batch(
        self,
        *,
        team_id: str,
        batch_id: str,
        now: datetime | None = None,
    ) -> VerificationBatch:
        timestamp = now or datetime.now(UTC)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            batch = await (
                await db.execute(
                    """
                    SELECT * FROM team_llm_verification_batches
                    WHERE team_id = ? AND id = ?
                    """,
                    (team_id, batch_id),
                )
            ).fetchone()
            if batch is None:
                await db.rollback()
                raise PlanNotFound("verification Batch was not found")
            if str(batch["state"]) in {
                "completed",
                "cancelled",
                "interrupted",
            }:
                await db.commit()
                return _batch_from_row(batch)
            revision = await self._next_revision(db, batch_id)
            await db.execute(
                """
                UPDATE team_llm_verification_items
                SET state = 'cancelled', safe_code = 'cancelled',
                    completed_at = ?, lease_until = NULL, revision = ?
                WHERE batch_id = ? AND state = 'queued'
                """,
                (_iso(timestamp), revision, batch_id),
            )
            await db.execute(
                """
                UPDATE team_llm_verification_items
                SET cancel_requested_at = ?, revision = ?
                WHERE batch_id = ? AND state = 'verifying'
                """,
                (_iso(timestamp), revision, batch_id),
            )
            await db.execute(
                """
                UPDATE team_llm_verification_batches
                SET state = 'cancelling', cancel_requested_at = ?
                WHERE id = ?
                """,
                (_iso(timestamp), batch_id),
            )
            await self._refresh_batch_state(db, batch_id, timestamp)
            refreshed = await (
                await db.execute(
                    "SELECT * FROM team_llm_verification_batches WHERE id = ?",
                    (batch_id,),
                )
            ).fetchone()
            await db.commit()
        assert refreshed is not None
        return _batch_from_row(refreshed)

    async def interrupt_runner(
        self,
        *,
        team_id: str,
        batch_id: str,
        runner_id: str,
        now: datetime | None = None,
    ) -> None:
        """Finish live Items after process loss or best-effort cancellation."""

        timestamp = now or datetime.now(UTC)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            rows = await (
                await db.execute(
                    """
                    SELECT id, cancel_requested_at
                    FROM team_llm_verification_items
                    WHERE team_id = ? AND batch_id = ? AND runner_id = ?
                      AND state IN ('queued', 'verifying')
                    """,
                    (team_id, batch_id, runner_id),
                )
            ).fetchall()
            if rows:
                revision = await self._next_revision(db, batch_id)
                for row in rows:
                    cancelled = bool(row["cancel_requested_at"])
                    await db.execute(
                        """
                        UPDATE team_llm_verification_items
                        SET state = ?, safe_code = ?, completed_at = ?,
                            lease_until = NULL, revision = ?
                        WHERE id = ? AND state IN ('queued', 'verifying')
                        """,
                        (
                            "cancelled" if cancelled else "interrupted",
                            "cancelled" if cancelled else "runner_interrupted",
                            _iso(timestamp),
                            revision,
                            row["id"],
                        ),
                    )
                await self._refresh_batch_state(db, batch_id, timestamp)
            await db.commit()

    async def interrupt_expired_runners(
        self,
        *,
        now: datetime | None = None,
    ) -> int:
        """Terminalize leases owned by processes that stopped heartbeating."""

        timestamp = now or datetime.now(UTC)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            teams = await (
                await db.execute(
                    """
                    SELECT DISTINCT team_id
                    FROM team_llm_verification_items
                    WHERE state IN ('queued', 'verifying')
                      AND lease_until < ?
                    """,
                    (_iso(timestamp),),
                )
            ).fetchall()
            for row in teams:
                await self._interrupt_expired_items(
                    db,
                    str(row["team_id"]),
                    timestamp,
                )
            await db.commit()
        return len(teams)

    async def sweep_retention(
        self,
        *,
        now: datetime | None = None,
        retention: timedelta = timedelta(days=90),
        batch_size: int = 500,
    ) -> RetentionSweepResult:
        """Delete bounded old history while retaining current Apply evidence."""

        timestamp = now or datetime.now(UTC)
        cutoff = _iso(timestamp - retention)
        bounded_size = min(max(batch_size, 1), 5000)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            snapshot_rows = await (
                await db.execute(
                    """
                    SELECT snapshot.decision_json
                    FROM team_llm_apply_snapshots AS snapshot
                    JOIN (
                        SELECT team_id, MAX(config_version) AS version
                        FROM team_llm_apply_snapshots
                        GROUP BY team_id
                    ) AS latest
                      ON latest.team_id = snapshot.team_id
                     AND latest.version = snapshot.config_version
                    """
                )
            ).fetchall()
            retained_evidence_ids: set[str] = set()
            for row in snapshot_rows:
                try:
                    decision = json.loads(str(row["decision_json"]))
                except (TypeError, ValueError):
                    continue
                for item in decision.get("rows") or []:
                    if isinstance(item, dict) and isinstance(item.get("evidence_id"), str):
                        retained_evidence_ids.add(str(item["evidence_id"]))

            retained_clause = ""
            evidence_params: list[Any] = [cutoff]
            if retained_evidence_ids:
                placeholders = ",".join("?" for _ in retained_evidence_ids)
                retained_clause = f" AND id NOT IN ({placeholders})"
                evidence_params.extend(sorted(retained_evidence_ids))
            evidence_params.append(bounded_size)
            evidence_cursor = await db.execute(
                f"""
                DELETE FROM team_llm_capability_evidence
                WHERE id IN (
                    SELECT id
                    FROM team_llm_capability_evidence
                    WHERE observed_at < ? {retained_clause}
                    ORDER BY observed_at, id
                    LIMIT ?
                )
                """,
                evidence_params,
            )
            items_cursor = await db.execute(
                """
                DELETE FROM team_llm_verification_items
                WHERE id IN (
                    SELECT id
                    FROM team_llm_verification_items
                    WHERE state IN (
                        'verified', 'failed', 'not_testable',
                        'errored', 'cancelled', 'interrupted'
                    )
                      AND completed_at < ?
                    ORDER BY completed_at, id
                    LIMIT ?
                )
                """,
                (cutoff, bounded_size),
            )
            batches_cursor = await db.execute(
                """
                DELETE FROM team_llm_verification_batches
                WHERE id IN (
                    SELECT id
                    FROM team_llm_verification_batches
                    WHERE state IN (
                        'completed', 'cancelled', 'interrupted'
                    )
                      AND completed_at < ?
                    ORDER BY completed_at, id
                    LIMIT ?
                )
                """,
                (cutoff, bounded_size),
            )
            plans_cursor = await db.execute(
                """
                DELETE FROM team_llm_verification_plans
                WHERE id IN (
                    SELECT id
                    FROM team_llm_verification_plans
                    WHERE created_at < ?
                    ORDER BY created_at, id
                    LIMIT ?
                )
                """,
                (cutoff, bounded_size),
            )
            await db.commit()
        return RetentionSweepResult(
            evidence=max(0, evidence_cursor.rowcount),
            items=max(0, items_cursor.rowcount),
            batches=max(0, batches_cursor.rowcount),
            plans=max(0, plans_cursor.rowcount),
        )

    async def list_active_batches(
        self,
        *,
        team_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        bounded_limit = min(max(limit, 1), 10)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT id, plan_id, state, revision, base_config_version,
                           actor_user_id, created_at, started_at
                    FROM team_llm_verification_batches
                    WHERE team_id = ?
                      AND state IN ('queued', 'running', 'cancelling')
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (team_id, bounded_limit),
                )
            ).fetchall()
        return [
            {
                "batch_id": str(row["id"]),
                "plan_id": str(row["plan_id"]),
                "state": str(row["state"]),
                "revision": int(row["revision"]),
                "base_config_version": int(row["base_config_version"]),
                "actor_user_id": str(row["actor_user_id"]),
                "created_at": str(row["created_at"]),
                "started_at": row["started_at"],
            }
            for row in rows
        ]

    async def list_evidence(
        self,
        *,
        team_id: str,
        provider_id: str | None = None,
        model_id: str | None = None,
        capability: str | None = None,
        before: tuple[str, str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        bounded_limit = min(max(limit, 1), 100)
        where = ["team_id = ?"]
        params: list[Any] = [team_id]
        if provider_id:
            where.append("provider_id = ?")
            params.append(provider_id)
        if model_id:
            where.append("model_id = ?")
            params.append(model_id)
        if capability:
            where.append("capability = ?")
            params.append(capability)
        if before:
            where.append("(observed_at < ? OR (observed_at = ? AND id < ?))")
            params.extend([before[0], before[0], before[1]])
        params.append(bounded_limit)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"""
                    SELECT id, provider_id, provider_display_name_snapshot,
                           model_id, capability, outcome, safe_code,
                           safe_http_status, safe_summary_code,
                           vector_dimension, batch_id, observed_at,
                           fresh_until, duration_ms
                    FROM team_llm_capability_evidence
                    WHERE {" AND ".join(where)}
                    ORDER BY observed_at DESC, id DESC
                    LIMIT ?
                    """,
                    params,
                )
            ).fetchall()
        return [
            {
                "evidence_id": str(row["id"]),
                "provider_id": str(row["provider_id"]),
                "provider_display_name": str(row["provider_display_name_snapshot"]),
                "model_id": str(row["model_id"]),
                "capability": str(row["capability"]),
                "outcome": str(row["outcome"]),
                "safe_code": str(row["safe_code"]),
                "safe_http_status": row["safe_http_status"],
                "safe_summary_code": row["safe_summary_code"],
                "vector_dimension": row["vector_dimension"],
                "batch_id": str(row["batch_id"]),
                "observed_at": str(row["observed_at"]),
                "fresh_until": str(row["fresh_until"]),
                "duration_ms": int(row["duration_ms"]),
            }
            for row in rows
        ]

    async def write_ops_audit(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        action: str,
        batch_id: str,
        payload: dict[str, Any],
        now: datetime | None = None,
    ) -> None:
        timestamp = now or datetime.now(UTC)
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO admin_audit_log(
                    id, actor_user_id, team_id, action,
                    target_kind, target_id, scope,
                    payload_json, ts
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    _id("aud_"),
                    actor_user_id,
                    team_id,
                    action,
                    "llm_verification_batch",
                    batch_id,
                    "team",
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    _iso(timestamp),
                ),
            )
            await db.commit()

    async def get_batch(
        self,
        *,
        team_id: str,
        batch_id: str,
        after_revision: int | None = None,
    ) -> dict[str, Any] | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            batch = await (
                await db.execute(
                    """
                    SELECT * FROM team_llm_verification_batches
                    WHERE team_id = ? AND id = ?
                    """,
                    (team_id, batch_id),
                )
            ).fetchone()
            if batch is None:
                return None
            query = """
                SELECT id, ordinal, provider_id, model_id, capability, state,
                       revision, safe_code, safe_http_status,
                       safe_summary_code, duration_ms, request_started_at,
                       completed_at
                FROM team_llm_verification_items
                WHERE team_id = ? AND batch_id = ?
            """
            params: list[Any] = [team_id, batch_id]
            if after_revision is not None:
                query += " AND revision > ?"
                params.append(after_revision)
            query += " ORDER BY ordinal LIMIT 100"
            items = await (await db.execute(query, params)).fetchall()
            count_rows = await (
                await db.execute(
                    """
                    SELECT state, COUNT(*) AS count
                    FROM team_llm_verification_items
                    WHERE team_id = ? AND batch_id = ?
                    GROUP BY state
                    """,
                    (team_id, batch_id),
                )
            ).fetchall()
        counts = {str(row["state"]): int(row["count"]) for row in count_rows}
        return {
            "batch_id": str(batch["id"]),
            "plan_id": str(batch["plan_id"]),
            "state": str(batch["state"]),
            "revision": int(batch["revision"]),
            "base_config_version": int(batch["base_config_version"]),
            "created_at": str(batch["created_at"]),
            "started_at": batch["started_at"],
            "completed_at": batch["completed_at"],
            "counts": counts,
            "items": [
                {
                    "item_id": str(row["id"]),
                    "ordinal": int(row["ordinal"]),
                    "provider_id": str(row["provider_id"]),
                    "model_id": str(row["model_id"]),
                    "capability": str(row["capability"]),
                    "state": str(row["state"]),
                    "revision": int(row["revision"]),
                    "safe_code": row["safe_code"],
                    "safe_http_status": row["safe_http_status"],
                    "safe_summary_code": row["safe_summary_code"],
                    "duration_ms": row["duration_ms"],
                    "request_started_at": row["request_started_at"],
                    "completed_at": row["completed_at"],
                }
                for row in items
            ],
        }

    async def _interrupt_expired_items(
        self,
        db: aiosqlite.Connection,
        team_id: str,
        now: datetime,
    ) -> None:
        rows = await (
            await db.execute(
                """
                SELECT DISTINCT batch_id
                FROM team_llm_verification_items
                WHERE team_id = ? AND state IN ('queued', 'verifying')
                  AND lease_until < ?
                """,
                (team_id, _iso(now)),
            )
        ).fetchall()
        for row in rows:
            batch_id = str(row[0])
            revision = await self._next_revision(db, batch_id)
            await db.execute(
                """
                UPDATE team_llm_verification_items
                SET state = 'interrupted', safe_code = 'runner_interrupted',
                    completed_at = ?, lease_until = NULL, revision = ?
                WHERE team_id = ? AND batch_id = ?
                  AND state IN ('queued', 'verifying') AND lease_until < ?
                """,
                (
                    _iso(now),
                    revision,
                    team_id,
                    batch_id,
                    _iso(now),
                ),
            )
            await self._refresh_batch_state(db, batch_id, now)

    @staticmethod
    async def _next_revision(
        db: aiosqlite.Connection,
        batch_id: str,
    ) -> int:
        await db.execute(
            """
            UPDATE team_llm_verification_batches
            SET revision = revision + 1
            WHERE id = ?
            """,
            (batch_id,),
        )
        row = await (
            await db.execute(
                "SELECT revision FROM team_llm_verification_batches WHERE id = ?",
                (batch_id,),
            )
        ).fetchone()
        if row is None:
            raise PlanNotFound("verification Batch was not found")
        return int(row[0])

    @staticmethod
    async def _refresh_batch_state(
        db: aiosqlite.Connection,
        batch_id: str,
        now: datetime,
    ) -> None:
        rows = await (
            await db.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM team_llm_verification_items
                WHERE batch_id = ?
                GROUP BY state
                """,
                (batch_id,),
            )
        ).fetchall()
        states = {str(row[0]): int(row[1]) for row in rows}
        if not states:
            return
        batch = await (
            await db.execute(
                """
                SELECT cancel_requested_at
                FROM team_llm_verification_batches
                WHERE id = ?
                """,
                (batch_id,),
            )
        ).fetchone()
        cancelling = bool(batch and batch[0])
        active = sum(count for state, count in states.items() if state not in _TERMINAL_ITEM_STATES)
        if active:
            state = "cancelling" if cancelling else "running" if states.get("verifying") else "queued"
            completed_at = None
        else:
            state = "cancelled" if states.get("cancelled") else "interrupted" if states.get("interrupted") else "completed"
            completed_at = _iso(now)
        await db.execute(
            """
            UPDATE team_llm_verification_batches
            SET state = ?, completed_at = ?,
                probe_context_ciphertext = CASE
                    WHEN ? IS NOT NULL THEN NULL
                    ELSE probe_context_ciphertext
                END
            WHERE id = ?
            """,
            (state, completed_at, completed_at, batch_id),
        )


def _batch_from_row(
    row: aiosqlite.Row,
    *,
    replayed: bool = False,
) -> VerificationBatch:
    return VerificationBatch(
        batch_id=str(row["id"]),
        team_id=str(row["team_id"]),
        actor_user_id=str(row["actor_user_id"]),
        plan_id=str(row["plan_id"]),
        base_config_version=int(row["base_config_version"]),
        state=str(row["state"]),
        revision=int(row["revision"]),
        created_at=_datetime(row["created_at"]),
        replayed=replayed,
    )
