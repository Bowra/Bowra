"""Durable V2 export job creation and idempotency transaction."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.error_catalog import CodedError
from bowhead_core.export_copy_contract import (
    CURRENT_MANIFEST_PRODUCER,
    output_metadata_matches_format,
    output_producer_for,
)
from bowhead_core.export_snapshot import ExportSnapshotCandidate, ExportSnapshotStore

if TYPE_CHECKING:
    from bowhead_core.artifact_store import ArtifactStore, StagedArtifact

EXPORT_JOB_RETENTION_DAYS = 90
EXPORT_LEASE_SECONDS = 90
EXPORT_MAX_ATTEMPTS = 2
EXPORT_USER_RUNNING_LIMIT = 2
EXPORT_TEAM_RUNNING_LIMIT = 4
EXPORT_CANVAS_ACTIVE_LIMIT = 3
EXPORT_USER_ACTIVE_LIMIT = 6
EXPORT_TEAM_ACTIVE_LIMIT = 12
EXPORT_GLOBAL_ACTIVE_LIMIT = 32

EXPORT_PHASES = (
    "queued",
    "resolving_assets",
    "normalizing",
    "materializing",
    "planning",
    "rendering",
    "validating",
    "persisting",
    "terminal",
)
_PHASE_ORDER = {phase: index for index, phase in enumerate(EXPORT_PHASES)}

_EXPORT_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS canvas_export_job_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL,
    attempt       INTEGER NOT NULL,
    sequence      INTEGER NOT NULL,
    event_type    TEXT NOT NULL,
    from_status   TEXT,
    to_status     TEXT,
    from_phase    TEXT,
    to_phase      TEXT,
    reason_code   TEXT,
    payload_json  TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE(job_id, attempt, sequence),
    FOREIGN KEY(job_id) REFERENCES canvas_export_jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_export_job_events_job
ON canvas_export_job_events(job_id, id);
"""


class ExportStoreError(CodedError):
    """Stable error raised while creating a durable V2 export job."""


@dataclass(frozen=True, slots=True)
class ExportJobV2:
    id: str
    canvas_id: str
    fmt: str
    status: str
    contract_version: int
    renderer_contract_version: int
    profile: str
    parity_contract: str
    surface_token: str
    phase: str
    quality_status: str
    warning_count: int
    snapshot_id: str
    requested_by: str
    idempotency_key: str
    request_sha256: str
    attempt: int
    worker_id: str | None
    runner_kind: str | None
    runner_task_id: str | None
    lease_expires_at: str | None
    progress_json: str | None
    artifact_id: str | None
    manifest_artifact_id: str | None
    url: str | None
    filename: str | None
    size: int | None
    error_code: str | None
    error: str | None
    started_at: str | None
    finished_at: str | None
    expires_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CreateExportJobResult:
    job: ExportJobV2
    created: bool
    writer_lock_duration_ms: float


@dataclass(frozen=True, slots=True)
class ExportJobEvent:
    id: int
    job_id: str
    attempt: int
    sequence: int
    event_type: str
    from_status: str | None
    to_status: str | None
    from_phase: str | None
    to_phase: str | None
    reason_code: str | None
    payload_json: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class ExportCompletion:
    artifact_id: str
    manifest_artifact_id: str
    filename: str
    size: int
    quality_status: str
    warning_count: int = 0
    url: str | None = None
    committed: bool = False


@dataclass(frozen=True, slots=True)
class ExportDatabaseRetention:
    jobs_deleted: int
    snapshots_deleted: int


@dataclass
class ExportStore:
    db_path: str | Path
    snapshot_store: ExportSnapshotStore
    _path: str = field(init=False)

    def __post_init__(self) -> None:
        self._path = str(self.db_path)

    async def initialize(self) -> None:
        async with open_shared_ctx(self._path) as db:
            await db.executescript(_EXPORT_EVENT_SCHEMA)
            await db.commit()

    async def create_export_job(
        self,
        *,
        candidate: ExportSnapshotCandidate,
        expected_projection_token: str,
        expected_surface_token: str,
        idempotency_key: str,
        request_sha256: str,
        job_id: str | None = None,
    ) -> CreateExportJobResult:
        if not 1 <= len(idempotency_key.encode("utf-8")) <= 128:
            raise ExportStoreError("invalid_idempotency_key")
        if not request_sha256.startswith("sha256:"):
            raise ExportStoreError("invalid_request_hash")
        requested_job_id = job_id or _new_job_id()
        if not requested_job_id.startswith("exp_"):
            raise ExportStoreError("invalid_export_job_id")

        lock_duration_ms = 0.0
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            lock_started = time.perf_counter()
            try:
                existing = await _get_idempotent_job(
                    db,
                    requested_by=candidate.requested_by,
                    idempotency_key=idempotency_key,
                )
                if existing is not None:
                    if existing.request_sha256 != request_sha256:
                        raise ExportStoreError("idempotency_conflict")
                    await db.commit()
                    duration_ms = _elapsed_ms(lock_started)
                    return CreateExportJobResult(
                        job=existing,
                        created=False,
                        writer_lock_duration_ms=duration_ms,
                    )

                await _enforce_admission_limits(db, candidate)
                await self.snapshot_store.insert_candidate_in_transaction(
                    db,
                    candidate,
                    expected_projection_token=expected_projection_token,
                    expected_surface_token=expected_surface_token,
                )
                await db.execute(
                    "INSERT INTO canvas_export_jobs("
                    "id, canvas_id, fmt, status, contract_version, renderer_contract_version, profile, parity_contract, "
                    "surface_token, phase, quality_status, warning_count, snapshot_id, "
                    "requested_by, idempotency_key, request_sha256, attempt, runner_kind, "
                    "expires_at, created_at, updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        requested_job_id,
                        candidate.canvas_id,
                        candidate.fmt,
                        "queued",
                        2,
                        candidate.renderer_contract_version,
                        candidate.profile,
                        candidate.parity_contract,
                        candidate.surface_projection.surface_token,
                        "queued",
                        "pending",
                        0,
                        candidate.id,
                        candidate.requested_by,
                        idempotency_key,
                        request_sha256,
                        0,
                        "local",
                        _job_expires_at(candidate.created_at),
                        candidate.created_at,
                        candidate.created_at,
                    ),
                )
                await _append_event(
                    db,
                    job_id=requested_job_id,
                    attempt=0,
                    event_type="queued",
                    from_status=None,
                    to_status="queued",
                    from_phase=None,
                    to_phase="queued",
                    created_at=candidate.created_at,
                )
                await db.commit()
                lock_duration_ms = _elapsed_ms(lock_started)
            except sqlite3.IntegrityError as exc:
                await db.rollback()
                winner = await self.get_idempotent_job(
                    requested_by=candidate.requested_by,
                    idempotency_key=idempotency_key,
                )
                if winner is not None:
                    if winner.request_sha256 != request_sha256:
                        raise ExportStoreError("idempotency_conflict") from exc
                    duration_ms = _elapsed_ms(lock_started)
                    return CreateExportJobResult(
                        job=winner,
                        created=False,
                        writer_lock_duration_ms=duration_ms,
                    )
                raise ExportStoreError("export_job_insert_failed") from exc
            except BaseException:
                await db.rollback()
                raise

        job = await self.get_job(requested_job_id)
        if job is None:  # pragma: no cover - committed INSERT must be readable
            raise RuntimeError("committed V2 export job is missing")
        return CreateExportJobResult(
            job=job,
            created=True,
            writer_lock_duration_ms=lock_duration_ms,
        )

    async def get_job(self, job_id: str) -> ExportJobV2 | None:
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"{_SELECT_JOB_SQL} WHERE id=? AND contract_version=2",
                (job_id,),
            ) as cursor:
                row = await cursor.fetchone()
        return _row_to_job(row) if row is not None else None

    async def list_jobs(
        self,
        *,
        canvas_id: str,
        requested_by: str,
        limit: int = 20,
    ) -> tuple[ExportJobV2, ...]:
        bounded_limit = max(1, min(limit, 100))
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"{_SELECT_JOB_SQL} WHERE canvas_id=? AND requested_by=? AND contract_version=2 ORDER BY created_at DESC, id DESC LIMIT ?",
                (canvas_id, requested_by, bounded_limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return tuple(_row_to_job(row) for row in rows)

    async def cancel_job(
        self,
        *,
        job_id: str,
        canvas_id: str,
        requested_by: str,
    ) -> ExportJobV2 | None:
        """Cancel a live V2 job and fence any late result with a new attempt."""
        finished_at = _now()
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    f"{_SELECT_JOB_SQL} WHERE id=? AND canvas_id=? AND requested_by=? AND contract_version=2",
                    (job_id, canvas_id, requested_by),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.commit()
                    return None
                job = _row_to_job(row)
                if job.status not in {"queued", "running"}:
                    raise ExportStoreError("job_not_cancelable", status=job.status)
                cursor = await db.execute(
                    "UPDATE canvas_export_jobs SET status='canceled', phase='terminal', "
                    "quality_status='pending', attempt=attempt+1, worker_id=NULL, "
                    "runner_task_id=NULL, lease_expires_at=NULL, finished_at=?, updated_at=? "
                    "WHERE id=? AND contract_version=2 AND status IN ('queued','running')",
                    (finished_at, finished_at, job_id),
                )
                if cursor.rowcount != 1:
                    raise ExportStoreError("job_not_cancelable")
                await _append_event(
                    db,
                    job_id=job.id,
                    attempt=job.attempt + 1,
                    event_type="canceled",
                    from_status=job.status,
                    to_status="canceled",
                    from_phase=job.phase,
                    to_phase="terminal",
                    created_at=finished_at,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_job(job_id)

    async def get_idempotent_job(
        self,
        *,
        requested_by: str,
        idempotency_key: str,
    ) -> ExportJobV2 | None:
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            return await _get_idempotent_job(
                db,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
            )

    async def claim_next(
        self,
        *,
        worker_id: str,
        supported_targets: tuple[tuple[str, str, int], ...],
        lease_seconds: int = EXPORT_LEASE_SECONDS,
        now: str | None = None,
    ) -> ExportJobV2 | None:
        if not worker_id or lease_seconds <= 0:
            raise ValueError("worker_id and a positive lease are required")
        if supported_targets == ():
            return None
        claimed_at = now or _now()
        lease_expires_at = _plus_seconds(claimed_at, lease_seconds)
        supported_sql, supported_params = _supported_target_filter(supported_targets)
        claimed_job_id: str | None = None
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _fail_exhausted_expired_jobs(
                    db,
                    now=claimed_at,
                    supported_targets=supported_targets,
                )
                async with db.execute(
                    f"{_SELECT_JOB_SQL} WHERE contract_version=2 AND ("
                    "status='queued' OR (status='running' AND "
                    "(lease_expires_at IS NULL OR lease_expires_at<=?))) "
                    f"{supported_sql} "
                    "ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, created_at, id LIMIT 1",
                    (claimed_at, *supported_params),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.commit()
                    return None
                previous = _row_to_job(row)
                next_attempt = previous.attempt + 1
                cursor = await db.execute(
                    "UPDATE canvas_export_jobs SET status='running', phase='resolving_assets', "
                    "quality_status='pending', attempt=?, worker_id=?, runner_kind='local', "
                    "lease_expires_at=?, started_at=COALESCE(started_at, ?), updated_at=? "
                    "WHERE id=? AND contract_version=2 AND attempt=? AND ("
                    "status='queued' OR (status='running' AND "
                    "(lease_expires_at IS NULL OR lease_expires_at<=?)))",
                    (
                        next_attempt,
                        worker_id,
                        lease_expires_at,
                        claimed_at,
                        claimed_at,
                        previous.id,
                        previous.attempt,
                        claimed_at,
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return None
                await _append_event(
                    db,
                    job_id=previous.id,
                    attempt=next_attempt,
                    event_type="reclaimed" if previous.status == "running" else "claimed",
                    from_status=previous.status,
                    to_status="running",
                    from_phase=previous.phase,
                    to_phase="resolving_assets",
                    created_at=claimed_at,
                )
                await db.commit()
                claimed_job_id = previous.id
            except BaseException:
                await db.rollback()
                raise
        return await self.get_job(claimed_job_id) if claimed_job_id is not None else None

    async def fail_unclaimable_queued_jobs(
        self,
        *,
        supported_pairs: tuple[tuple[str, str], ...],
        now: str | None = None,
    ) -> int:
        """Fail queued jobs that this deployment can no longer process."""
        failed_at = now or _now()
        unsupported_sql, unsupported_params = _unsupported_filter(
            supported_pairs,
        )
        failed_count = 0
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    f"SELECT * FROM canvas_export_jobs WHERE contract_version=2 AND status='queued' {unsupported_sql} ORDER BY created_at, id",
                    unsupported_params,
                ) as cursor:
                    rows = await cursor.fetchall()
                for row in rows:
                    job = _row_to_job(row)
                    changed = await db.execute(
                        "UPDATE canvas_export_jobs SET status='failed', "
                        "phase='terminal', error_code='export_runner_unavailable', "
                        "error='该导出格式暂不可用，请重新导出', "
                        "finished_at=?, updated_at=? "
                        "WHERE id=? AND contract_version=2 AND status='queued'",
                        (
                            failed_at,
                            failed_at,
                            job.id,
                        ),
                    )
                    if changed.rowcount != 1:
                        continue
                    await _append_event(
                        db,
                        job_id=job.id,
                        attempt=job.attempt,
                        event_type="failed",
                        from_status="queued",
                        to_status="failed",
                        from_phase="queued",
                        to_phase="terminal",
                        reason_code="export_runner_unavailable",
                        created_at=failed_at,
                    )
                    failed_count += 1
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return failed_count

    async def heartbeat(
        self,
        *,
        job_id: str,
        worker_id: str,
        attempt: int,
        lease_seconds: int = EXPORT_LEASE_SECONDS,
        now: str | None = None,
    ) -> bool:
        heartbeat_at = now or _now()
        lease_expires_at = _plus_seconds(heartbeat_at, lease_seconds)
        async with open_shared_ctx(self._path) as db:
            cursor = await db.execute(
                "UPDATE canvas_export_jobs SET lease_expires_at=?, updated_at=? "
                "WHERE id=? AND contract_version=2 AND status='running' "
                "AND worker_id=? AND attempt=?",
                (lease_expires_at, heartbeat_at, job_id, worker_id, attempt),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def advance_phase(
        self,
        *,
        job_id: str,
        worker_id: str,
        attempt: int,
        phase: str,
        progress: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> bool:
        if phase not in _PHASE_ORDER or phase in {"queued", "terminal"}:
            raise ExportStoreError("invalid_job_transition", phase=phase)
        progress_json = _progress_json(progress)
        changed_at = now or _now()
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                current = await _get_owned_attempt(
                    db,
                    job_id=job_id,
                    worker_id=worker_id,
                    attempt=attempt,
                )
                if current is None:
                    await db.commit()
                    return False
                if current.status != "running" or _PHASE_ORDER[phase] < _PHASE_ORDER.get(current.phase, -1):
                    raise ExportStoreError(
                        "invalid_job_transition",
                        from_phase=current.phase,
                        to_phase=phase,
                    )
                cursor = await db.execute(
                    "UPDATE canvas_export_jobs SET phase=?, progress_json=COALESCE(?, progress_json), "
                    "updated_at=? WHERE id=? AND contract_version=2 AND status='running' "
                    "AND worker_id=? AND attempt=? AND phase=?",
                    (
                        phase,
                        progress_json,
                        changed_at,
                        job_id,
                        worker_id,
                        attempt,
                        current.phase,
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return False
                await _append_event(
                    db,
                    job_id=job_id,
                    attempt=attempt,
                    event_type="progress" if phase == current.phase else "phase_changed",
                    from_status="running",
                    to_status="running",
                    from_phase=current.phase,
                    to_phase=phase,
                    payload_json=progress_json,
                    created_at=changed_at,
                )
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def complete_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        attempt: int,
        completion: ExportCompletion,
        now: str | None = None,
    ) -> bool:
        _validate_completion(completion)
        if completion.committed:
            raise ExportStoreError("invalid_job_completion")
        finished_at = now or _now()
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                current = await _get_owned_attempt(
                    db,
                    job_id=job_id,
                    worker_id=worker_id,
                    attempt=attempt,
                )
                if current is None:
                    await db.commit()
                    return False
                if current.status != "running" or current.phase != "persisting":
                    raise ExportStoreError(
                        "invalid_job_transition",
                        from_phase=current.phase,
                        to_phase="terminal",
                    )
                cursor = await db.execute(
                    "UPDATE canvas_export_jobs SET status='done', phase='terminal', "
                    "quality_status=?, warning_count=?, artifact_id=?, manifest_artifact_id=?, "
                    "url=?, filename=?, size=?, error_code=NULL, error=NULL, worker_id=NULL, "
                    "runner_task_id=NULL, lease_expires_at=NULL, finished_at=?, updated_at=? "
                    "WHERE id=? AND contract_version=2 AND status='running' "
                    "AND worker_id=? AND attempt=? AND phase='persisting'",
                    (
                        completion.quality_status,
                        completion.warning_count,
                        completion.artifact_id,
                        completion.manifest_artifact_id,
                        completion.url,
                        completion.filename,
                        completion.size,
                        finished_at,
                        finished_at,
                        job_id,
                        worker_id,
                        attempt,
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return False
                await _append_event(
                    db,
                    job_id=job_id,
                    attempt=attempt,
                    event_type="completed",
                    from_status="running",
                    to_status="done",
                    from_phase="persisting",
                    to_phase="terminal",
                    created_at=finished_at,
                )
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def complete_job_with_staged_artifacts(
        self,
        *,
        job_id: str,
        worker_id: str,
        attempt: int,
        completion: ExportCompletion,
        artifact_store: ArtifactStore,
        output_artifact: StagedArtifact,
        manifest_artifact: StagedArtifact,
        now: str | None = None,
    ) -> bool:
        """Atomically publish output, manifest, and terminal job metadata."""
        _validate_completion(completion)
        _validate_staged_completion(
            store_path=self._path,
            artifact_store=artifact_store,
            completion=completion,
            output_artifact=output_artifact,
            manifest_artifact=manifest_artifact,
        )
        finished_at = now or _now()
        current: ExportJobV2 | None = None
        committed = False
        try:
            async with open_shared_ctx(self._path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("BEGIN IMMEDIATE")
                try:
                    current = await _get_owned_attempt(
                        db,
                        job_id=job_id,
                        worker_id=worker_id,
                        attempt=attempt,
                    )
                    if current is None:
                        await db.rollback()
                    else:
                        if current.status != "running" or current.phase != "persisting":
                            raise ExportStoreError(
                                "invalid_job_transition",
                                from_phase=current.phase,
                                to_phase="terminal",
                            )
                        _validate_staged_ownership(
                            job=current,
                            output_artifact=output_artifact,
                            manifest_artifact=manifest_artifact,
                        )
                        await artifact_store.insert_staged_artifacts_in_transaction(
                            db,
                            (
                                manifest_artifact,
                                output_artifact,
                            ),
                        )
                        cursor = await db.execute(
                            "UPDATE canvas_export_jobs SET status='done', "
                            "phase='terminal', quality_status=?, "
                            "warning_count=?, artifact_id=?, "
                            "manifest_artifact_id=?, url=?, filename=?, size=?, "
                            "error_code=NULL, error=NULL, worker_id=NULL, "
                            "runner_task_id=NULL, lease_expires_at=NULL, "
                            "finished_at=?, updated_at=? "
                            "WHERE id=? AND contract_version=2 "
                            "AND status='running' AND worker_id=? "
                            "AND attempt=? AND phase='persisting'",
                            (
                                completion.quality_status,
                                completion.warning_count,
                                completion.artifact_id,
                                completion.manifest_artifact_id,
                                completion.url,
                                completion.filename,
                                completion.size,
                                finished_at,
                                finished_at,
                                job_id,
                                worker_id,
                                attempt,
                            ),
                        )
                        if cursor.rowcount != 1:
                            await db.rollback()
                        else:
                            await _append_event(
                                db,
                                job_id=job_id,
                                attempt=attempt,
                                event_type="completed",
                                from_status="running",
                                to_status="done",
                                from_phase="persisting",
                                to_phase="terminal",
                                created_at=finished_at,
                            )
                            await _commit_publish_transaction(db)
                            committed = True
                except BaseException:
                    await db.rollback()
                    raise
        except BaseException:
            await artifact_store.discard_staged_artifacts(
                (
                    manifest_artifact,
                    output_artifact,
                )
            )
            raise
        if not committed or current is None:
            await artifact_store.discard_staged_artifacts(
                (
                    manifest_artifact,
                    output_artifact,
                )
            )
            return False
        return True

    async def fail_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        attempt: int,
        error_code: str,
        public_error: str,
        quality_status: str = "pending",
        now: str | None = None,
    ) -> bool:
        if not error_code or len(error_code) > 128 or len(public_error) > 500:
            raise ValueError("bounded error code and public message are required")
        if quality_status not in {"pending", "rejected"}:
            raise ExportStoreError("invalid_job_transition", quality_status=quality_status)
        finished_at = now or _now()
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                current = await _get_owned_attempt(
                    db,
                    job_id=job_id,
                    worker_id=worker_id,
                    attempt=attempt,
                )
                if current is None or current.status != "running":
                    await db.commit()
                    return False
                cursor = await db.execute(
                    "UPDATE canvas_export_jobs SET status='failed', phase='terminal', "
                    "quality_status=?, error_code=?, error=?, worker_id=NULL, runner_task_id=NULL, "
                    "lease_expires_at=NULL, finished_at=?, updated_at=? WHERE id=? "
                    "AND contract_version=2 AND status='running' AND worker_id=? AND attempt=?",
                    (
                        quality_status,
                        error_code,
                        public_error,
                        finished_at,
                        finished_at,
                        job_id,
                        worker_id,
                        attempt,
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return False
                await _append_event(
                    db,
                    job_id=job_id,
                    attempt=attempt,
                    event_type="failed",
                    from_status="running",
                    to_status="failed",
                    from_phase=current.phase,
                    to_phase="terminal",
                    reason_code=error_code,
                    created_at=finished_at,
                )
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def is_current_attempt(
        self,
        *,
        job_id: str,
        worker_id: str,
        attempt: int,
    ) -> bool:
        async with (
            open_shared_ctx(self._path) as db,
            db.execute(
                "SELECT 1 FROM canvas_export_jobs WHERE id=? AND contract_version=2 AND status='running' AND worker_id=? AND attempt=?",
                (job_id, worker_id, attempt),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return row is not None

    async def list_events(self, job_id: str) -> tuple[ExportJobEvent, ...]:
        async with open_shared_ctx(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, job_id, attempt, sequence, event_type, from_status, to_status, "
                "from_phase, to_phase, reason_code, payload_json, created_at "
                "FROM canvas_export_job_events WHERE job_id=? ORDER BY id",
                (job_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return tuple(_row_to_event(row) for row in rows)

    async def list_active_job_ids(self) -> frozenset[str]:
        async with (
            open_shared_ctx(self._path) as db,
            db.execute("SELECT id FROM canvas_export_jobs WHERE contract_version=2 AND status IN ('queued','running')") as cursor,
        ):
            rows = await cursor.fetchall()
        return frozenset(str(row[0]) for row in rows)

    async def cleanup_expired(
        self,
        *,
        now: str | None = None,
    ) -> ExportDatabaseRetention:
        """Delete only expired terminal metadata and inactive snapshots."""
        cutoff = now or _now()
        async with open_shared_ctx(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                jobs = await db.execute(
                    "DELETE FROM canvas_export_jobs "
                    "WHERE contract_version=2 "
                    "AND status IN ('done','failed','canceled') "
                    "AND expires_at IS NOT NULL AND expires_at<=?",
                    (cutoff,),
                )
                snapshots = await db.execute(
                    "DELETE FROM canvas_export_snapshots "
                    "WHERE expires_at<=? AND NOT EXISTS ("
                    "SELECT 1 FROM canvas_export_jobs j "
                    "WHERE j.snapshot_id=canvas_export_snapshots.id "
                    "AND j.contract_version=2 "
                    "AND j.status IN ('queued','running'))",
                    (cutoff,),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return ExportDatabaseRetention(
            jobs_deleted=max(0, jobs.rowcount),
            snapshots_deleted=max(0, snapshots.rowcount),
        )


_SELECT_JOB_SQL = (
    "SELECT id, canvas_id, fmt, status, contract_version, renderer_contract_version, profile, parity_contract, "
    "surface_token, phase, quality_status, warning_count, snapshot_id, requested_by, "
    "idempotency_key, request_sha256, attempt, worker_id, runner_kind, runner_task_id, "
    "lease_expires_at, progress_json, artifact_id, manifest_artifact_id, url, filename, size, "
    "error_code, error, started_at, finished_at, expires_at, created_at, updated_at "
    "FROM canvas_export_jobs"
)


async def _get_idempotent_job(
    db: aiosqlite.Connection,
    *,
    requested_by: str,
    idempotency_key: str,
) -> ExportJobV2 | None:
    async with db.execute(
        f"{_SELECT_JOB_SQL} WHERE requested_by=? AND idempotency_key=? AND contract_version=2",
        (requested_by, idempotency_key),
    ) as cursor:
        row = await cursor.fetchone()
    return _row_to_job(row) if row is not None else None


async def _get_owned_attempt(
    db: aiosqlite.Connection,
    *,
    job_id: str,
    worker_id: str,
    attempt: int,
) -> ExportJobV2 | None:
    async with db.execute(
        f"{_SELECT_JOB_SQL} WHERE id=? AND contract_version=2 AND worker_id=? AND attempt=?",
        (job_id, worker_id, attempt),
    ) as cursor:
        row = await cursor.fetchone()
    return _row_to_job(row) if row is not None else None


async def _enforce_admission_limits(
    db: aiosqlite.Connection,
    candidate: ExportSnapshotCandidate,
) -> None:
    checks = (
        (
            "canvas",
            "SELECT COUNT(*) FROM canvas_export_jobs WHERE contract_version=2 AND canvas_id=? AND status IN ('queued','running')",
            (candidate.canvas_id,),
            EXPORT_CANVAS_ACTIVE_LIMIT,
        ),
        (
            "user",
            "SELECT COUNT(*) FROM canvas_export_jobs WHERE contract_version=2 AND requested_by=? AND status='running'",
            (candidate.requested_by,),
            EXPORT_USER_RUNNING_LIMIT,
        ),
        (
            "user_active",
            "SELECT COUNT(*) FROM canvas_export_jobs WHERE contract_version=2 AND requested_by=? AND status IN ('queued','running')",
            (candidate.requested_by,),
            EXPORT_USER_ACTIVE_LIMIT,
        ),
        (
            "global_active",
            "SELECT COUNT(*) FROM canvas_export_jobs WHERE contract_version=2 AND status IN ('queued','running')",
            (),
            EXPORT_GLOBAL_ACTIVE_LIMIT,
        ),
    )
    for scope, query, params, limit in checks:
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
        if row is not None and int(row[0]) >= limit:
            raise ExportStoreError(
                "export_capacity_exceeded",
                scope=scope,
                limit=limit,
            )
    if candidate.team_id:
        for scope, statuses, limit in (
            ("team", "('running')", EXPORT_TEAM_RUNNING_LIMIT),
            (
                "team_active",
                "('queued','running')",
                EXPORT_TEAM_ACTIVE_LIMIT,
            ),
        ):
            async with db.execute(
                "SELECT COUNT(*) FROM canvas_export_jobs j "
                "JOIN canvas_export_snapshots s ON s.id=j.snapshot_id "
                f"WHERE j.contract_version=2 AND j.status IN {statuses} "
                "AND s.team_id=?",
                (candidate.team_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None and int(row[0]) >= limit:
                raise ExportStoreError(
                    "export_capacity_exceeded",
                    scope=scope,
                    limit=limit,
                )


async def _fail_exhausted_expired_jobs(
    db: aiosqlite.Connection,
    *,
    now: str,
    supported_targets: tuple[tuple[str, str, int], ...],
) -> None:
    supported_sql, supported_params = _supported_target_filter(supported_targets)
    async with db.execute(
        f"{_SELECT_JOB_SQL} WHERE contract_version=2 AND status='running' "
        "AND (lease_expires_at IS NULL OR lease_expires_at<=?) AND attempt>=? "
        f"{supported_sql} "
        "ORDER BY created_at, id",
        (now, EXPORT_MAX_ATTEMPTS, *supported_params),
    ) as cursor:
        expired_rows = await cursor.fetchall()
    for row in expired_rows:
        job = _row_to_job(row)
        cursor = await db.execute(
            "UPDATE canvas_export_jobs SET status='failed', phase='terminal', "
            "quality_status='pending', error_code='worker_lost', "
            "error='导出执行器中断，请重新导出', worker_id=NULL, runner_task_id=NULL, "
            "lease_expires_at=NULL, finished_at=?, updated_at=? WHERE id=? "
            "AND contract_version=2 AND status='running' AND attempt=? "
            "AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
            (now, now, job.id, job.attempt, now),
        )
        if cursor.rowcount != 1:
            continue
        await _append_event(
            db,
            job_id=job.id,
            attempt=job.attempt,
            event_type="failed",
            from_status="running",
            to_status="failed",
            from_phase=job.phase,
            to_phase="terminal",
            reason_code="worker_lost",
            created_at=now,
        )


def _supported_target_filter(
    supported_targets: tuple[tuple[str, str, int], ...],
) -> tuple[str, tuple[object, ...]]:
    if len(supported_targets) > 32:
        raise ValueError("too many export processor targets")
    predicates = " OR ".join("(fmt=? AND profile=? AND renderer_contract_version=?)" for _ in supported_targets)
    params = tuple(value for target in supported_targets for value in target)
    return f"AND ({predicates})", params


def _unsupported_filter(
    supported_pairs: tuple[tuple[str, str], ...],
) -> tuple[str, tuple[str, ...]]:
    if len(supported_pairs) > 16:
        raise ValueError("too many export processor pairs")
    if not supported_pairs:
        return "", ()
    predicates = " OR ".join("(fmt=? AND profile=?)" for _ in supported_pairs)
    params = tuple(value for pair in supported_pairs for value in pair)
    return f"AND NOT ({predicates})", params


async def _commit_publish_transaction(
    db: aiosqlite.Connection,
) -> None:
    """Once publish commit starts, cancellation must not delete committed blobs."""
    commit_task = asyncio.create_task(db.commit())
    try:
        await asyncio.shield(commit_task)
    except asyncio.CancelledError:
        await commit_task


async def _append_event(
    db: aiosqlite.Connection,
    *,
    job_id: str,
    attempt: int,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    from_phase: str | None,
    to_phase: str | None,
    created_at: str,
    reason_code: str | None = None,
    payload_json: str | None = None,
) -> None:
    async with db.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM canvas_export_job_events WHERE job_id=? AND attempt=?",
        (job_id, attempt),
    ) as cursor:
        row = await cursor.fetchone()
    sequence = int(row[0]) if row is not None else 1
    await db.execute(
        "INSERT INTO canvas_export_job_events("
        "job_id, attempt, sequence, event_type, from_status, to_status, from_phase, "
        "to_phase, reason_code, payload_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            attempt,
            sequence,
            event_type,
            from_status,
            to_status,
            from_phase,
            to_phase,
            reason_code,
            payload_json,
            created_at,
        ),
    )


def _progress_json(progress: dict[str, Any] | None) -> str | None:
    if progress is None:
        return None
    try:
        payload = json.dumps(
            progress,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ExportStoreError("invalid_job_progress") from exc
    if len(payload.encode("utf-8")) > 16 * 1024:
        raise ExportStoreError("invalid_job_progress")
    return payload


def _validate_staged_completion(
    *,
    store_path: str,
    artifact_store: ArtifactStore,
    completion: ExportCompletion,
    output_artifact: StagedArtifact,
    manifest_artifact: StagedArtifact,
) -> None:
    output = output_artifact.artifact
    manifest = manifest_artifact.artifact
    if (
        not completion.committed
        or Path(store_path).resolve() != Path(artifact_store.db_path).resolve()
        or completion.artifact_id != output.id
        or completion.manifest_artifact_id != manifest.id
        or completion.filename != output.filename
        or completion.size != output.size
        or output.manifest_artifact_id != manifest.id
        or manifest.manifest_artifact_id is not None
        or output_artifact.session_id != manifest_artifact.session_id
    ):
        raise ExportStoreError("invalid_job_completion")


def _validate_staged_ownership(
    *,
    job: ExportJobV2,
    output_artifact: StagedArtifact,
    manifest_artifact: StagedArtifact,
) -> None:
    for staged in (
        output_artifact,
        manifest_artifact,
    ):
        artifact = staged.artifact
        if (
            artifact.canvas_id != job.canvas_id
            or artifact.created_by != job.requested_by
            or artifact.source != "canvas_export"
            or artifact.kind != "deliverable"
        ):
            raise ExportStoreError("invalid_job_completion")
    if (
        manifest_artifact.artifact.producer_kind != CURRENT_MANIFEST_PRODUCER
        or output_artifact.artifact.producer_kind != output_producer_for(engine="durable", fmt=job.fmt)
        or not output_metadata_matches_format(output_artifact.artifact, job.fmt)
        or manifest_artifact.artifact.mime != "application/json"
        or not manifest_artifact.artifact.filename.lower().endswith(".manifest.json")
        or output_artifact.artifact.manifest_artifact_id != manifest_artifact.artifact.id
    ):
        raise ExportStoreError("invalid_job_completion")


def _validate_completion(completion: ExportCompletion) -> None:
    if not completion.artifact_id.startswith("art_") or not completion.manifest_artifact_id.startswith("art_"):
        raise ExportStoreError("invalid_job_completion")
    if not completion.filename or completion.size < 0:
        raise ExportStoreError("invalid_job_completion")
    if completion.quality_status not in {"clean", "warnings"}:
        raise ExportStoreError("invalid_job_completion")
    if completion.warning_count < 0:
        raise ExportStoreError("invalid_job_completion")
    if completion.quality_status == "clean" and completion.warning_count != 0:
        raise ExportStoreError("invalid_job_completion")
    if completion.quality_status == "warnings" and completion.warning_count == 0:
        raise ExportStoreError("invalid_job_completion")


def compute_export_request_sha256(request: dict[str, Any]) -> str:
    canonical_request = dict(request)
    canonical_request.pop("idempotency_key", None)
    payload = json.dumps(
        canonical_request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _row_to_job(row: aiosqlite.Row) -> ExportJobV2:
    return ExportJobV2(
        id=row["id"],
        canvas_id=row["canvas_id"],
        fmt=row["fmt"],
        status=row["status"],
        contract_version=int(row["contract_version"]),
        renderer_contract_version=int(row["renderer_contract_version"]),
        profile=row["profile"],
        parity_contract=row["parity_contract"],
        surface_token=row["surface_token"],
        phase=row["phase"],
        quality_status=row["quality_status"],
        warning_count=int(row["warning_count"]),
        snapshot_id=row["snapshot_id"],
        requested_by=row["requested_by"],
        idempotency_key=row["idempotency_key"],
        request_sha256=row["request_sha256"],
        attempt=int(row["attempt"]),
        worker_id=row["worker_id"],
        runner_kind=row["runner_kind"],
        runner_task_id=row["runner_task_id"],
        lease_expires_at=row["lease_expires_at"],
        progress_json=row["progress_json"],
        artifact_id=row["artifact_id"],
        manifest_artifact_id=row["manifest_artifact_id"],
        url=row["url"],
        filename=row["filename"],
        size=int(row["size"]) if row["size"] is not None else None,
        error_code=row["error_code"],
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_event(row: aiosqlite.Row) -> ExportJobEvent:
    return ExportJobEvent(
        id=int(row["id"]),
        job_id=row["job_id"],
        attempt=int(row["attempt"]),
        sequence=int(row["sequence"]),
        event_type=row["event_type"],
        from_status=row["from_status"],
        to_status=row["to_status"],
        from_phase=row["from_phase"],
        to_phase=row["to_phase"],
        reason_code=row["reason_code"],
        payload_json=row["payload_json"],
        created_at=row["created_at"],
    )


def _new_job_id() -> str:
    return f"exp_{uuid4().hex[:12]}"


def _elapsed_ms(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _plus_seconds(value: str, seconds: int) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (parsed + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _job_expires_at(created_at: str) -> str:
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (created + timedelta(days=EXPORT_JOB_RETENTION_DAYS)).isoformat(timespec="seconds")


__all__ = [
    "CreateExportJobResult",
    "EXPORT_JOB_RETENTION_DAYS",
    "EXPORT_LEASE_SECONDS",
    "EXPORT_MAX_ATTEMPTS",
    "EXPORT_PHASES",
    "ExportCompletion",
    "ExportDatabaseRetention",
    "ExportJobEvent",
    "ExportJobV2",
    "ExportStore",
    "ExportStoreError",
    "compute_export_request_sha256",
]
