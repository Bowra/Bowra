"""SQLite store for team-scoped model verification identities and Evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.team_llm_evidence import (
    SIGNATURE_VERSION,
    AttemptRecord,
    Capability,
    EvidenceOutcome,
    EvidenceProjection,
    EvidenceRecord,
    legacy_provider_id,
    new_provider_id,
    project_latest_evidence,
)

if TYPE_CHECKING:
    from pathlib import Path


class TeamLlmVerificationStoreError(RuntimeError):
    code = "verification_store_error"


class ProviderIdentityConflict(TeamLlmVerificationStoreError):
    code = "provider_identity_conflict"


class ProviderIdentityNotFound(TeamLlmVerificationStoreError):
    code = "provider_identity_not_found"


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    team_id: str
    provider_id: str
    runtime_key: str
    state: str
    creation_source: str
    created_by: str | None
    created_at: datetime
    reservation_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    evidence_id: str
    team_id: str
    provider_id: str
    provider_display_name_snapshot: str
    model_id: str
    capability: Capability
    config_signature: str
    testability_contract_version: str
    probe_contract_version: str | None
    probe_execution_policy_version: str
    outcome: EvidenceOutcome
    safe_code: str
    safe_http_status: int | None
    safe_summary_code: str | None
    vector_dimension: int | None
    batch_id: str
    item_id: str
    initiated_by: str
    observed_at: datetime
    fresh_until: datetime
    duration_ms: int
    actual_model_id: str | None = None


def _iso(value: datetime) -> str:
    normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _optional_datetime(value: Any) -> datetime | None:
    return _datetime(value) if value else None


def _identity_from_row(row: aiosqlite.Row) -> ProviderIdentity:
    return ProviderIdentity(
        team_id=str(row["team_id"]),
        provider_id=str(row["provider_id"]),
        runtime_key=str(row["runtime_key"]),
        state=str(row["state"]),
        creation_source=str(row["creation_source"]),
        created_by=str(row["created_by"]) if row["created_by"] else None,
        created_at=_datetime(row["created_at"]),
        reservation_expires_at=_optional_datetime(row["reservation_expires_at"]),
    )


class TeamLlmVerificationStore:
    """Narrow store; schema ownership remains with Alembic revision 0004."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def assert_schema(self) -> None:
        required = {
            "team_llm_provider_identities",
            "team_llm_verification_plans",
            "team_llm_verification_batches",
            "team_llm_verification_items",
            "team_llm_capability_evidence",
            "team_llm_risk_acceptances",
            "team_llm_v2_enrollments",
            "team_llm_apply_snapshots",
        }
        async with open_shared_ctx(self.db_path) as db:
            rows = await (
                await db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'",
                )
            ).fetchall()
        present = {str(row[0]) for row in rows}
        missing = sorted(required - present)
        if missing:
            raise TeamLlmVerificationStoreError(f"team LLM verification schema missing: {', '.join(missing)}")

    async def reserve_provider_identity(
        self,
        *,
        team_id: str,
        runtime_key: str,
        actor_user_id: str,
        now: datetime | None = None,
        ttl: timedelta = timedelta(hours=24),
    ) -> ProviderIdentity:
        timestamp = now or datetime.now(UTC)
        provider_id = new_provider_id()
        try:
            async with open_shared_ctx(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("BEGIN IMMEDIATE")
                existing = await (
                    await db.execute(
                        """
                        SELECT * FROM team_llm_provider_identities
                        WHERE team_id = ? AND runtime_key = ?
                          AND state IN ('reserved', 'active')
                        LIMIT 1
                        """,
                        (team_id, runtime_key),
                    )
                ).fetchone()
                if existing is not None:
                    identity = _identity_from_row(existing)
                    reservation_active = (
                        identity.state == "reserved" and identity.reservation_expires_at is not None and identity.reservation_expires_at > timestamp
                    )
                    if reservation_active and identity.created_by == actor_user_id:
                        await db.commit()
                        return identity
                    if identity.state == "reserved" and not reservation_active:
                        await db.execute(
                            """
                            UPDATE team_llm_provider_identities
                            SET state = 'deleted', deleted_at = ?,
                                reservation_expires_at = NULL
                            WHERE team_id = ? AND provider_id = ?
                              AND state = 'reserved'
                            """,
                            (_iso(timestamp), team_id, identity.provider_id),
                        )
                    else:
                        await db.rollback()
                        raise ProviderIdentityConflict(
                            "an active or reserved Provider already uses this runtime key",
                        )
                await db.execute(
                    """
                    INSERT INTO team_llm_provider_identities(
                        team_id, provider_id, runtime_key, state, created_by,
                        creation_source, created_at, reservation_expires_at
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        team_id,
                        provider_id,
                        runtime_key,
                        "reserved",
                        actor_user_id,
                        "user",
                        _iso(timestamp),
                        _iso(timestamp + ttl),
                    ),
                )
                await db.commit()
        except aiosqlite.IntegrityError as exc:
            raise ProviderIdentityConflict("an active or reserved Provider already uses this runtime key") from exc
        return ProviderIdentity(
            team_id=team_id,
            provider_id=provider_id,
            runtime_key=runtime_key,
            state="reserved",
            creation_source="user",
            created_by=actor_user_id,
            created_at=timestamp,
            reservation_expires_at=timestamp + ttl,
        )

    async def ensure_legacy_provider_identity(
        self,
        *,
        team_id: str,
        runtime_key: str,
        root_key: str | bytes | None = None,
        now: datetime | None = None,
    ) -> ProviderIdentity:
        timestamp = now or datetime.now(UTC)
        provider_id = legacy_provider_id(
            team_id,
            runtime_key,
            root_key=root_key,
        )
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    """
                    SELECT * FROM team_llm_provider_identities
                    WHERE team_id = ? AND runtime_key = ?
                      AND state IN ('reserved', 'active')
                    """,
                    (team_id, runtime_key),
                )
            ).fetchone()
            if existing is not None:
                if str(existing["provider_id"]) != provider_id:
                    await db.rollback()
                    raise ProviderIdentityConflict("runtime key is already bound to a different Provider identity")
                await db.commit()
                return _identity_from_row(existing)
            await db.execute(
                """
                INSERT INTO team_llm_provider_identities(
                    team_id, provider_id, runtime_key, state, created_by,
                    creation_source, created_at, activated_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    team_id,
                    provider_id,
                    runtime_key,
                    "active",
                    None,
                    "legacy_migration",
                    _iso(timestamp),
                    _iso(timestamp),
                ),
            )
            await db.commit()
        return ProviderIdentity(
            team_id=team_id,
            provider_id=provider_id,
            runtime_key=runtime_key,
            state="active",
            creation_source="legacy_migration",
            created_by=None,
            created_at=timestamp,
            reservation_expires_at=None,
        )

    async def get_provider_identity(
        self,
        *,
        team_id: str,
        provider_id: str,
    ) -> ProviderIdentity | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT * FROM team_llm_provider_identities
                    WHERE team_id = ? AND provider_id = ?
                    """,
                    (team_id, provider_id),
                )
            ).fetchone()
        return _identity_from_row(row) if row else None

    async def get_provider_identity_by_runtime_key(
        self,
        *,
        team_id: str,
        runtime_key: str,
    ) -> ProviderIdentity | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT * FROM team_llm_provider_identities
                    WHERE team_id = ? AND runtime_key = ?
                      AND state IN ('reserved', 'active')
                    ORDER BY CASE state WHEN 'active' THEN 0 ELSE 1 END
                    LIMIT 1
                    """,
                    (team_id, runtime_key),
                )
            ).fetchone()
        return _identity_from_row(row) if row else None

    async def delete_provider_identity(
        self,
        *,
        team_id: str,
        provider_id: str,
        now: datetime | None = None,
    ) -> None:
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE team_llm_provider_identities
                SET state = 'deleted', deleted_at = ?,
                    reservation_expires_at = NULL
                WHERE team_id = ? AND provider_id = ?
                  AND state IN ('reserved', 'active')
                """,
                (_iso(now or datetime.now(UTC)), team_id, provider_id),
            )
            if cursor.rowcount != 1:
                raise ProviderIdentityNotFound("active Provider identity was not found")
            await db.commit()

    async def insert_evidence(
        self,
        evidence: CapabilityEvidence,
    ) -> None:
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO team_llm_capability_evidence(
                    id, team_id, provider_id, provider_display_name_snapshot,
                    model_id, capability, config_signature, signature_version,
                    testability_contract_version, probe_contract_version,
                    probe_execution_policy_version, outcome, safe_code,
                    safe_http_status, safe_summary_code, vector_dimension,
                    batch_id, item_id, initiated_by, observed_at, fresh_until,
                    duration_ms, actual_model_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    evidence.evidence_id,
                    evidence.team_id,
                    evidence.provider_id,
                    evidence.provider_display_name_snapshot,
                    evidence.model_id,
                    evidence.capability,
                    evidence.config_signature,
                    SIGNATURE_VERSION,
                    evidence.testability_contract_version,
                    evidence.probe_contract_version,
                    evidence.probe_execution_policy_version,
                    evidence.outcome,
                    evidence.safe_code,
                    evidence.safe_http_status,
                    evidence.safe_summary_code,
                    evidence.vector_dimension,
                    evidence.batch_id,
                    evidence.item_id,
                    evidence.initiated_by,
                    _iso(evidence.observed_at),
                    _iso(evidence.fresh_until),
                    evidence.duration_ms,
                    evidence.actual_model_id,
                ),
            )
            await db.commit()

    async def project_evidence(
        self,
        *,
        team_id: str,
        provider_id: str,
        model_id: str,
        capability: Capability,
        current_signature: str,
        now: datetime | None = None,
        attempt: AttemptRecord | None = None,
    ) -> EvidenceProjection:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT id, config_signature, outcome, safe_code,
                           observed_at, fresh_until, actual_model_id
                    FROM team_llm_capability_evidence
                    WHERE team_id = ? AND provider_id = ?
                      AND model_id = ? AND capability = ?
                    ORDER BY observed_at DESC, id DESC
                    LIMIT 100
                    """,
                    (team_id, provider_id, model_id, capability),
                )
            ).fetchall()
            if attempt is None:
                attempt_row = await (
                    await db.execute(
                        """
                        SELECT state, safe_code, completed_at
                        FROM team_llm_verification_items
                        WHERE team_id = ? AND provider_id = ?
                          AND model_id = ? AND capability = ?
                          AND config_signature = ?
                          AND state IN (
                              'queued', 'verifying', 'errored',
                              'cancelled', 'interrupted'
                          )
                        ORDER BY COALESCE(
                            completed_at,
                            request_started_at,
                            lease_until
                        ) DESC, id DESC
                        LIMIT 1
                        """,
                        (
                            team_id,
                            provider_id,
                            model_id,
                            capability,
                            current_signature,
                        ),
                    )
                ).fetchone()
                if attempt_row is not None:
                    attempt = AttemptRecord(
                        state=str(attempt_row["state"]),  # type: ignore[arg-type]
                        safe_code=(str(attempt_row["safe_code"]) if attempt_row["safe_code"] else None),
                        completed_at=_optional_datetime(attempt_row["completed_at"]),
                    )
        records = [
            EvidenceRecord(
                evidence_id=str(row["id"]),
                config_signature=str(row["config_signature"]),
                outcome=str(row["outcome"]),  # type: ignore[arg-type]
                safe_code=str(row["safe_code"]),
                observed_at=_datetime(row["observed_at"]),
                fresh_until=_datetime(row["fresh_until"]),
                actual_model_id=(str(row["actual_model_id"]) if row["actual_model_id"] else None),
            )
            for row in rows
        ]
        return project_latest_evidence(
            records,
            current_signature=current_signature,
            now=now or datetime.now(UTC),
            attempt=attempt,
        )
