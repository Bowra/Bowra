"""Release A application compatibility admission and heartbeat registry.

This module is deliberately limited to process compatibility.  It does not
activate Owner, audience, lifecycle, or database trigger semantics.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.team_subagent_store import RELEASE_A_SCHEMA_VERSION

if TYPE_CHECKING:
    from collections.abc import Sequence

_COMPATIBILITY_VERSION_WIDTH = 4
_DEFAULT_LIVE_WINDOW_SECONDS = 90
_DEFAULT_STALE_RETENTION_SECONDS = 24 * 60 * 60
_DEFAULT_CLEANUP_LIMIT = 100
_DEFAULT_CLEANUP_SCAN_LIMIT = 1_000
_MAX_FLEET_SCAN_ROWS = 10_000


class Clock(Protocol):
    """Clock used for persisted RFC 3339 UTC timestamps."""

    def now(self) -> datetime: ...


class SystemClock:
    """Production UTC clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ApplicationCompatibilityDecision:
    schema_version: int
    reader_version: str
    writer_version: str
    minimum_reader_version: str
    minimum_writer_version: str
    reader_compatible: bool
    writer_compatible: bool
    compatible: bool
    checked_at: str


@dataclass(frozen=True, slots=True)
class DeploymentAdmissionReport:
    schema_version: int | None
    checked_at: str
    live_window_seconds: int
    candidate_reader_version: str
    candidate_writer_version: str
    minimum_reader_version: str | None
    minimum_writer_version: str | None
    candidate_compatible: bool | None
    total_registered_instances: int
    live_instances: int
    compatible_live_instances: int
    incompatible_live_instances: int
    malformed_instances: int
    stale_instances: int
    scan_truncated: bool
    migration_pending: bool
    ready: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class TeamSubagentCompatibilityMetadataError(RuntimeError):
    """Compatibility metadata is missing or malformed."""

    code = "team_subagent_compatibility_metadata_invalid"


class TeamSubagentCompatibilityDatabaseError(RuntimeError):
    """The database itself could not be read, so metadata says nothing.

    Kept distinct from the metadata error because the operator response is
    opposite: this one is an access/environment fault to fix, not a schema
    state to migrate.
    """

    code = "team_subagent_compatibility_database_unreadable"


class TeamSubagentApplicationIncompatibleError(RuntimeError):
    """The application reader/writer is below the persisted version floor."""

    code = "team_subagent_application_incompatible"

    def __init__(self, decision: ApplicationCompatibilityDecision) -> None:
        self.decision = decision
        super().__init__(self.code)


class TeamSubagentCompatibilityStore:
    """Own process admission and heartbeats for the Release A schema."""

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self.db_path = str(db_path)
        self._clock = clock or SystemClock()

    async def check_application(
        self,
        *,
        reader_version: str,
        writer_version: str,
    ) -> ApplicationCompatibilityDecision:
        """Evaluate the startup/readiness fence without writing a heartbeat."""
        validate_compatibility_version(reader_version)
        validate_compatibility_version(writer_version)
        checked_at = _rfc3339_utc(self._clock.now())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            metadata = await self._read_metadata_conn(db)
        return self._decision_from_metadata(
            metadata,
            reader_version=reader_version,
            writer_version=writer_version,
            checked_at=checked_at,
        )

    async def record_heartbeat(
        self,
        *,
        instance_id: str,
        reader_version: str,
        writer_version: str,
    ) -> ApplicationCompatibilityDecision:
        """Admit one compatible process and atomically refresh its heartbeat."""
        instance_id = _validate_instance_id(instance_id)
        validate_compatibility_version(reader_version)
        validate_compatibility_version(writer_version)
        now = self._clock.now()
        checked_at = _rfc3339_utc(now)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                metadata = await self._read_metadata_conn(db)
                decision = self._decision_from_metadata(
                    metadata,
                    reader_version=reader_version,
                    writer_version=writer_version,
                    checked_at=checked_at,
                )
                if not decision.compatible:
                    raise TeamSubagentApplicationIncompatibleError(decision)
                await db.execute(
                    """
                    INSERT INTO team_subagent_app_compatibility (
                      instance_id,
                      reader_version,
                      writer_version,
                      started_at,
                      heartbeat_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(instance_id) DO UPDATE SET
                      reader_version = excluded.reader_version,
                      writer_version = excluded.writer_version,
                      heartbeat_at = excluded.heartbeat_at
                    """,
                    (
                        instance_id,
                        reader_version,
                        writer_version,
                        checked_at,
                        checked_at,
                    ),
                )
                await self._cleanup_stale_rows_conn(db, now=now)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return decision

    async def remove_heartbeat(self, instance_id: str) -> None:
        """Remove a gracefully stopped process; crashed rows age out instead."""
        instance_id = _validate_instance_id(instance_id)
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                """
                DELETE FROM team_subagent_app_compatibility
                WHERE instance_id = ?
                """,
                (instance_id,),
            )
            await db.commit()

    async def deployment_admission(
        self,
        *,
        reader_version: str,
        writer_version: str,
        live_window_seconds: int = _DEFAULT_LIVE_WINDOW_SECONDS,
    ) -> DeploymentAdmissionReport:
        """Read-only candidate and live-fleet admission report.

        If compatibility metadata is pending its Alembic revision, this check
        defers to startup migration without modifying the database.
        """
        validate_compatibility_version(reader_version)
        validate_compatibility_version(writer_version)
        if live_window_seconds <= 0:
            raise ValueError("live_window_seconds must be positive")
        db_path = Path(self.db_path)
        if not db_path.is_file():
            raise FileNotFoundError(f"database not found: {db_path}")
        checked_at_value = self._clock.now()
        checked_at = _rfc3339_utc(checked_at_value)
        cutoff = checked_at_value.astimezone(UTC) - timedelta(
            seconds=live_window_seconds,
        )
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                metadata = await self._read_metadata_conn(db)
            except TeamSubagentCompatibilityMetadataError as exc:
                if str(exc) != "team_subagent_schema_meta_missing":
                    raise
                return DeploymentAdmissionReport(
                    schema_version=None,
                    checked_at=checked_at,
                    live_window_seconds=live_window_seconds,
                    candidate_reader_version=reader_version,
                    candidate_writer_version=writer_version,
                    minimum_reader_version=None,
                    minimum_writer_version=None,
                    candidate_compatible=None,
                    total_registered_instances=0,
                    live_instances=0,
                    compatible_live_instances=0,
                    incompatible_live_instances=0,
                    malformed_instances=0,
                    stale_instances=0,
                    scan_truncated=False,
                    migration_pending=True,
                    ready=True,
                )
            async with db.execute(
                "SELECT COUNT(*) FROM team_subagent_app_compatibility",
            ) as cursor:
                total_registered = int((await cursor.fetchone())[0])
            async with db.execute(
                """
                SELECT reader_version, writer_version, heartbeat_at
                FROM team_subagent_app_compatibility
                ORDER BY heartbeat_at DESC, instance_id ASC
                LIMIT ?
                """,
                (_MAX_FLEET_SCAN_ROWS,),
            ) as cursor:
                rows = await cursor.fetchall()

        decision = self._decision_from_metadata(
            metadata,
            reader_version=reader_version,
            writer_version=writer_version,
            checked_at=checked_at,
        )
        fleet = _classify_fleet(
            rows,
            cutoff=cutoff,
            minimum_reader_version=decision.minimum_reader_version,
            minimum_writer_version=decision.minimum_writer_version,
        )
        scan_truncated = total_registered > _MAX_FLEET_SCAN_ROWS
        ready = decision.compatible and fleet["incompatible_live_instances"] == 0 and fleet["malformed_instances"] == 0 and not scan_truncated
        return DeploymentAdmissionReport(
            schema_version=decision.schema_version,
            checked_at=checked_at,
            live_window_seconds=live_window_seconds,
            candidate_reader_version=reader_version,
            candidate_writer_version=writer_version,
            minimum_reader_version=decision.minimum_reader_version,
            minimum_writer_version=decision.minimum_writer_version,
            candidate_compatible=decision.compatible,
            total_registered_instances=total_registered,
            live_instances=fleet["live_instances"],
            compatible_live_instances=fleet["compatible_live_instances"],
            incompatible_live_instances=fleet["incompatible_live_instances"],
            malformed_instances=fleet["malformed_instances"],
            stale_instances=fleet["stale_instances"],
            scan_truncated=scan_truncated,
            migration_pending=False,
            ready=ready,
        )

    async def _read_metadata_conn(
        self,
        db: aiosqlite.Connection,
    ) -> aiosqlite.Row:
        try:
            async with db.execute(
                """
                SELECT schema_version,
                       minimum_reader_version,
                       minimum_writer_version
                FROM team_subagent_schema_meta
                WHERE singleton = 1
                """,
            ) as cursor:
                row = await cursor.fetchone()
        except aiosqlite.Error as exc:
            # Two very different faults arrive as the same driver error, and they
            # demand opposite responses: an absent table is a real schema verdict
            # (migrate), while an unopenable database means nothing was learned
            # (fix access). The latter bit the 1.0.0 rollout — production runs
            # WAL, which must create a `-shm` sidecar even for a mode=ro reader,
            # so a read-only filesystem fails the read and the gate blamed a
            # migration that did not exist. Split them on the driver's own
            # wording rather than on where the failure surfaced: SQLite can raise
            # the open error either at connect or at first read.
            if "no such table" in str(exc).lower():
                raise TeamSubagentCompatibilityMetadataError(
                    "team_subagent_schema_meta_missing",
                ) from exc
            raise TeamSubagentCompatibilityDatabaseError(
                f"team_subagent_metadata_unreadable: {exc}",
            ) from exc
        if row is None:
            raise TeamSubagentCompatibilityMetadataError(
                "team_subagent_schema_meta_missing",
            )
        try:
            validate_compatibility_version(str(row["minimum_reader_version"]))
            validate_compatibility_version(str(row["minimum_writer_version"]))
            schema_version = int(row["schema_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TeamSubagentCompatibilityMetadataError(
                "team_subagent_schema_meta_invalid",
            ) from exc
        if schema_version < RELEASE_A_SCHEMA_VERSION:
            raise TeamSubagentCompatibilityMetadataError(
                "team_subagent_schema_meta_invalid",
            )
        return row

    def _decision_from_metadata(
        self,
        metadata: aiosqlite.Row,
        *,
        reader_version: str,
        writer_version: str,
        checked_at: str,
    ) -> ApplicationCompatibilityDecision:
        minimum_reader_version = str(metadata["minimum_reader_version"])
        minimum_writer_version = str(metadata["minimum_writer_version"])
        reader_compatible = reader_version >= minimum_reader_version
        writer_compatible = writer_version >= minimum_writer_version
        return ApplicationCompatibilityDecision(
            schema_version=int(metadata["schema_version"]),
            reader_version=reader_version,
            writer_version=writer_version,
            minimum_reader_version=minimum_reader_version,
            minimum_writer_version=minimum_writer_version,
            reader_compatible=reader_compatible,
            writer_compatible=writer_compatible,
            compatible=reader_compatible and writer_compatible,
            checked_at=checked_at,
        )

    async def _cleanup_stale_rows_conn(
        self,
        db: aiosqlite.Connection,
        *,
        now: datetime,
    ) -> None:
        cutoff_value = now.astimezone(UTC) - timedelta(
            seconds=_DEFAULT_STALE_RETENTION_SECONDS,
        )
        cutoff = _rfc3339_utc(cutoff_value)
        async with db.execute(
            """
            SELECT instance_id, heartbeat_at
            FROM team_subagent_app_compatibility
            WHERE heartbeat_at < ?
            ORDER BY heartbeat_at ASC, instance_id ASC
            LIMIT ?
            """,
            (cutoff, _DEFAULT_CLEANUP_SCAN_LIMIT),
        ) as cursor:
            candidates = await cursor.fetchall()

        stale_instance_ids: list[tuple[str]] = []
        for row in candidates:
            try:
                heartbeat_at = _parse_rfc3339_utc(str(row["heartbeat_at"]))
            except ValueError:
                continue
            if heartbeat_at < cutoff_value:
                stale_instance_ids.append((str(row["instance_id"]),))
            if len(stale_instance_ids) >= _DEFAULT_CLEANUP_LIMIT:
                break
        if stale_instance_ids:
            await db.executemany(
                """
                DELETE FROM team_subagent_app_compatibility
                WHERE instance_id = ?
                """,
                stale_instance_ids,
            )


def validate_compatibility_version(version: str) -> None:
    """Require the sortable four-digit compatibility version contract."""
    if len(version) != _COMPATIBILITY_VERSION_WIDTH or not version.isascii() or not version.isdigit():
        raise ValueError(
            "compatibility version must be a four-digit sortable value",
        )


def _validate_instance_id(instance_id: str) -> str:
    value = instance_id.strip()
    if not value or len(value) > 200:
        raise ValueError("instance_id must contain 1-200 characters")
    return value


def _classify_fleet(
    rows: Sequence[aiosqlite.Row],
    *,
    cutoff: datetime,
    minimum_reader_version: str,
    minimum_writer_version: str,
) -> dict[str, int]:
    counts = {
        "live_instances": 0,
        "compatible_live_instances": 0,
        "incompatible_live_instances": 0,
        "malformed_instances": 0,
        "stale_instances": 0,
    }
    for row in rows:
        try:
            heartbeat_at = _parse_rfc3339_utc(str(row["heartbeat_at"]))
        except ValueError:
            counts["malformed_instances"] += 1
            continue
        if heartbeat_at < cutoff:
            counts["stale_instances"] += 1
            continue
        counts["live_instances"] += 1
        reader_version = str(row["reader_version"])
        writer_version = str(row["writer_version"])
        try:
            validate_compatibility_version(reader_version)
            validate_compatibility_version(writer_version)
        except ValueError:
            counts["malformed_instances"] += 1
            counts["incompatible_live_instances"] += 1
            continue
        if reader_version >= minimum_reader_version and writer_version >= minimum_writer_version:
            counts["compatible_live_instances"] += 1
        else:
            counts["incompatible_live_instances"] += 1
    return counts


def _rfc3339_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("team_subagent_clock_must_return_aware_datetime")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="seconds")
        .replace(
            "+00:00",
            "Z",
        )
    )


def _parse_rfc3339_utc(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("invalid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid RFC 3339 timestamp")
    return parsed.astimezone(UTC)
