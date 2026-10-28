"""KbPerRowJobsStore — async job tracking for Sprint 25 per-row extraction.

Independent SQLite db `kb_per_row_jobs.db`. Persists per-file extraction job
state across worker restarts (resume via heartbeat scanner). Per tech-design
r2 §3.2 + PRD r3 §3.4 D1.

Schema:
    job_id / file_id / doc_type_id / workspace_id / status / attempt_id /
    total_batches / completed_batches / rows_extracted / rows_skipped_dup /
    error_code (enum) / error_hint (constant) / checkpoint_json (pydantic-strict) /
    started_at / heartbeat_at / finished_at

P0-TD-SEC-6: checkpoint_json is pydantic-validated with strict whitelist on
write (`extra="forbid"`) + lenient on read (`extra="ignore"`) — backward
compat for Sprint 26+ schema additions without breaking 0.44.x resume.

P0-TD-SEC-4: error_code is enum; error_hint is constant string (NOT LLM raw
body / stack trace fragments).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, Field, ValidationError

from bowhead_core._db import bootstrap_shared_db, open_shared_ctx
from bowhead_core.kb.telemetry import emit_audit

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ─── Status / error_code enums ──────────────────────────────


JOB_STATUS_VALUES = (
    "queued",  # created, task not yet running
    "running",  # task active
    "succeeded",  # all rows extracted
    "failed",  # terminal failure (token overflow / parse fail / unknown)
    "failed_retry",  # transient failure (LLM timeout / heartbeat zombie) → scanner resume
    "cancelled",  # force re-trigger cancelled this attempt
)

ERROR_CODE_ENUM = (
    "LLM_TIMEOUT",
    "LLM_INVALID_JSON",
    "PARSE_FAILED",
    "TOKEN_OVERFLOW",
    "DEDUP_VIOLATION",
    "QUOTA_EXCEEDED",
    "UNKNOWN",
)


# ─── Pydantic checkpoint state (P0-TD-SEC-6) ────────────────


class CheckpointStateWrite(BaseModel):
    """Strict whitelist on write — no `extra_data` / `cell_content` injection.

    Per tech-design §3.2 + P0-SEC-6. Resume reads the same JSON via
    CheckpointStateRead (extra=ignore) so Sprint 26+ schema additions don't
    break 0.44.x resume.
    """

    last_completed_batch_idx: int = Field(..., ge=0)
    last_completed_row_seq: int = Field(..., ge=0)
    total_batches: int = Field(..., ge=1)
    attempt_id: int = Field(..., ge=1)

    model_config = {"extra": "forbid"}


class CheckpointStateRead(BaseModel):
    """Lenient on read (P0-TD-SEC-5). Backward-compat for Sprint 26+ checkpoint
    fields that 0.44.x doesn't know — silently ignore extras."""

    last_completed_batch_idx: int = Field(..., ge=0)
    last_completed_row_seq: int = Field(..., ge=0)
    total_batches: int = Field(..., ge=1)
    attempt_id: int = Field(..., ge=1)

    model_config = {"extra": "ignore"}


# ─── DTO ────────────────────────────────────────────────────


@dataclass
class PerRowJobDTO:
    job_id: str
    file_id: str
    doc_type_id: str
    workspace_id: str
    status: str
    attempt_id: int
    total_batches: int
    completed_batches: int
    rows_extracted: int
    rows_skipped_dup: int
    error_code: str | None
    error_hint: str | None
    checkpoint: CheckpointStateRead | None
    started_at: str
    heartbeat_at: str
    finished_at: str | None


# ─── Schema ─────────────────────────────────────────────────


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_per_row_jobs (
    job_id              TEXT PRIMARY KEY,
    file_id             TEXT NOT NULL,
    doc_type_id         TEXT NOT NULL,
    workspace_id        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'queued',
    attempt_id          INTEGER NOT NULL DEFAULT 1,
    total_batches       INTEGER NOT NULL DEFAULT 0,
    completed_batches   INTEGER NOT NULL DEFAULT 0,
    rows_extracted      INTEGER NOT NULL DEFAULT 0,
    rows_skipped_dup    INTEGER NOT NULL DEFAULT 0,
    error_code          TEXT,
    error_hint          TEXT,
    checkpoint_json     TEXT,
    started_at          TEXT NOT NULL,
    heartbeat_at        TEXT NOT NULL,
    finished_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_kb_per_row_jobs_file
    ON kb_per_row_jobs(file_id, attempt_id DESC);

CREATE INDEX IF NOT EXISTS idx_kb_per_row_jobs_status
    ON kb_per_row_jobs(status, heartbeat_at);
"""


# ─── helpers ────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _parse_checkpoint(raw: str | None) -> CheckpointStateRead | None:
    """Parse checkpoint_json with lenient schema (P0-TD-SEC-5).
    Returns None on parse fail (caller treats as fresh start)."""
    if not raw:
        return None
    try:
        return CheckpointStateRead.model_validate_json(raw)
    except ValidationError as e:
        logger.warning("checkpoint parse fail (degraded — restart from batch 0): %s", e)
        return None


def _row_to_dto(row: aiosqlite.Row) -> PerRowJobDTO:
    return PerRowJobDTO(
        job_id=row["job_id"],
        file_id=row["file_id"],
        doc_type_id=row["doc_type_id"],
        workspace_id=row["workspace_id"],
        status=row["status"],
        attempt_id=int(row["attempt_id"]),
        total_batches=int(row["total_batches"]),
        completed_batches=int(row["completed_batches"]),
        rows_extracted=int(row["rows_extracted"]),
        rows_skipped_dup=int(row["rows_skipped_dup"]),
        error_code=row["error_code"],
        error_hint=row["error_hint"],
        checkpoint=_parse_checkpoint(row["checkpoint_json"]),
        started_at=row["started_at"],
        heartbeat_at=row["heartbeat_at"],
        finished_at=row["finished_at"],
    )


# ─── Store class ────────────────────────────────────────────


class KbPerRowJobsStore:
    """Async job tracking. Independent SQLite db file."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def initialize(self) -> None:
        await bootstrap_shared_db(self._db_path)
        async with open_shared_ctx(self._db_path) as db:
            for stmt in _SCHEMA.split(";"):
                stmt = stmt.strip()
                if stmt:
                    await db.execute(stmt)
            await db.commit()

    async def create_job(
        self,
        *,
        file_id: str,
        doc_type_id: str,
        workspace_id: str,
        total_batches: int = 0,
        attempt_id: int = 1,
        status: str = "queued",
    ) -> str:
        """Create job row. Called from worker BEFORE asyncio.create_task spawn
        (P0-UX-3: queued state visible immediately in FE pill)."""
        if status not in JOB_STATUS_VALUES:
            raise ValueError(f"status must be one of {JOB_STATUS_VALUES}")
        job_id = f"perrow_{uuid4().hex[:14]}"
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                """INSERT INTO kb_per_row_jobs
                   (job_id, file_id, doc_type_id, workspace_id, status,
                    attempt_id, total_batches, completed_batches, rows_extracted,
                    rows_skipped_dup, started_at, heartbeat_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?)""",
                (
                    job_id,
                    file_id,
                    doc_type_id,
                    workspace_id,
                    status,
                    attempt_id,
                    total_batches,
                    now,
                    now,
                ),
            )
            await db.commit()
        return job_id

    async def update_checkpoint(
        self,
        job_id: str,
        checkpoint: CheckpointStateWrite,
    ) -> None:
        """Atomic UPDATE before each batch LLM call (P1-PERF-8).
        Strict whitelist via CheckpointStateWrite (P0-TD-SEC-6)."""
        # pydantic strict — caller validates by passing typed object
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "UPDATE kb_per_row_jobs SET checkpoint_json = ?, heartbeat_at = ? WHERE job_id = ?",
                (checkpoint.model_dump_json(), now, job_id),
            )
            await db.commit()

    async def update_progress(
        self,
        job_id: str,
        *,
        completed_batches: int,
        rows_extracted: int,
        rows_skipped_dup: int = 0,
        heartbeat: bool = False,
    ) -> None:
        """Per-batch progress + heartbeat throttle (P1-PERF-1: every 5 batches)."""
        if heartbeat:
            now = _now()
            async with open_shared_ctx(self._db_path) as db:
                await db.execute(
                    "UPDATE kb_per_row_jobs SET completed_batches=?, rows_extracted=?, rows_skipped_dup=?, heartbeat_at=? WHERE job_id=?",
                    (completed_batches, rows_extracted, rows_skipped_dup, now, job_id),
                )
                await db.commit()
        else:
            async with open_shared_ctx(self._db_path) as db:
                await db.execute(
                    "UPDATE kb_per_row_jobs SET completed_batches=?, rows_extracted=?, rows_skipped_dup=? WHERE job_id=?",
                    (completed_batches, rows_extracted, rows_skipped_dup, job_id),
                )
                await db.commit()

    async def mark_status(
        self,
        job_id: str,
        status: str,
        *,
        error_code: str | None = None,
        error_hint: str | None = None,
        total_batches: int | None = None,
    ) -> None:
        """Mark terminal state. error_code MUST be enum (P0-SEC-4);
        error_hint MUST be constant string (no LLM raw body)."""
        if status not in JOB_STATUS_VALUES:
            raise ValueError(f"status must be one of {JOB_STATUS_VALUES}")
        if error_code is not None and error_code not in ERROR_CODE_ENUM:
            raise ValueError(f"error_code must be one of {ERROR_CODE_ENUM}")
        now = _now()
        finished_at = now if status in ("succeeded", "failed", "cancelled") else None
        async with open_shared_ctx(self._db_path) as db:
            if total_batches is not None:
                await db.execute(
                    "UPDATE kb_per_row_jobs SET status=?, error_code=?, error_hint=?, finished_at=?, total_batches=?, heartbeat_at=? WHERE job_id=?",
                    (status, error_code, error_hint, finished_at, total_batches, now, job_id),
                )
            else:
                await db.execute(
                    "UPDATE kb_per_row_jobs SET status=?, error_code=?, error_hint=?, finished_at=?, heartbeat_at=? WHERE job_id=?",
                    (status, error_code, error_hint, finished_at, now, job_id),
                )
            await db.commit()

    # ─── Read ──

    async def get(self, job_id: str) -> PerRowJobDTO | None:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_per_row_jobs WHERE job_id = ?",
                (job_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_dto(row) if row else None

    async def get_latest_for_file(self, file_id: str) -> PerRowJobDTO | None:
        """Used by GET /extract_rows_status — returns latest attempt."""
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_per_row_jobs WHERE file_id = ? ORDER BY attempt_id DESC LIMIT 1",
                (file_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_dto(row) if row else None

    async def list_for_file_ids(
        self,
        file_ids: list[str],
        workspace_id: str,
    ) -> list[PerRowJobDTO]:
        """Batch endpoint (P1-ENG-8): single SELECT N file_ids,
        return latest attempt per file_id. Cross-ws filter at SQL layer."""
        if not file_ids:
            return []
        # Cap at 50 file_ids per call (matches endpoint cap)
        file_ids = file_ids[:50]
        placeholders = ",".join("?" for _ in file_ids)
        # Use ROW_NUMBER window to get latest attempt per file_id
        sql = (
            "SELECT * FROM ("
            "  SELECT *, ROW_NUMBER() OVER ("
            "    PARTITION BY file_id ORDER BY attempt_id DESC"
            "  ) as rn "
            "  FROM kb_per_row_jobs "
            f"  WHERE file_id IN ({placeholders}) AND workspace_id = ?"
            ") WHERE rn = 1"
        )
        params = [*file_ids, workspace_id]
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    async def mark_zombies(self, threshold_minutes: int = 15) -> int:
        """Heartbeat scanner: atomic single-statement UPDATE (P1-SEC-4).

        Detects jobs marked 'running' but no heartbeat in `threshold_minutes`.
        Updates them to 'failed_retry' so resume picks up.
        Returns count marked.
        """
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "UPDATE kb_per_row_jobs "
                "SET status='failed_retry', error_code='UNKNOWN', "
                "error_hint='zombie_no_heartbeat' "
                "WHERE status='running' "
                f"AND datetime(heartbeat_at) < datetime('now', '-{int(threshold_minutes)} minutes')",
            )
            await db.commit()
            count = cur.rowcount
        if count > 0:
            emit_audit(
                "kb.row_extract_zombie_detected",
                {
                    "count": count,
                    "threshold_minutes": threshold_minutes,
                },
            )
        return count
