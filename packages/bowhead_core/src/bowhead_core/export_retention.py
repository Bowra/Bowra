"""Retention and orphan-staging cleanup for Canvas Export V2."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bowhead_core.export_store import ExportStore

_JOB_DIR_RE = re.compile(r"exp_[a-zA-Z0-9_-]+")
_MAX_STAGING_MEMBERS_PER_JOB = 20_000


@dataclass(frozen=True, slots=True)
class ExportRetentionResult:
    jobs_deleted: int
    snapshots_deleted: int
    staging_job_dirs_deleted: int
    staging_bytes_deleted: int
    staging_dirs_skipped: int


async def run_export_retention(
    *,
    store: ExportStore,
    staging_root: Path,
    staging_retention_hours: int = 24,
    now: datetime | None = None,
) -> ExportRetentionResult:
    if not 1 <= staging_retention_hours <= 24 * 30:
        raise ValueError("staging retention must be between 1 hour and 30 days")
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    database = await store.cleanup_expired(
        now=current.isoformat(timespec="seconds"),
    )
    active_job_ids = await store.list_active_job_ids()
    staging = cleanup_stale_export_staging(
        staging_root=staging_root,
        active_job_ids=active_job_ids,
        cutoff=current - timedelta(hours=staging_retention_hours),
    )
    return ExportRetentionResult(
        jobs_deleted=database.jobs_deleted,
        snapshots_deleted=database.snapshots_deleted,
        staging_job_dirs_deleted=staging.staging_job_dirs_deleted,
        staging_bytes_deleted=staging.staging_bytes_deleted,
        staging_dirs_skipped=staging.staging_dirs_skipped,
    )


def cleanup_stale_export_staging(
    *,
    staging_root: Path,
    active_job_ids: frozenset[str],
    cutoff: datetime,
) -> ExportRetentionResult:
    if not staging_root.is_absolute() or staging_root.is_symlink() or not staging_root.exists():
        return ExportRetentionResult(0, 0, 0, 0, 0)
    if not staging_root.is_dir():
        raise ValueError("export staging root must be a directory")
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    deleted_dirs = 0
    deleted_bytes = 0
    skipped = 0
    for job_dir in staging_root.iterdir():
        if not _JOB_DIR_RE.fullmatch(job_dir.name) or job_dir.name in active_job_ids or job_dir.is_symlink() or not job_dir.is_dir():
            continue
        inspection = _inspect_staging_job_dir(job_dir)
        if inspection is None:
            skipped += 1
            continue
        newest_mtime, size_bytes = inspection
        if newest_mtime > cutoff:
            continue
        shutil.rmtree(job_dir)
        deleted_dirs += 1
        deleted_bytes += size_bytes
    return ExportRetentionResult(
        jobs_deleted=0,
        snapshots_deleted=0,
        staging_job_dirs_deleted=deleted_dirs,
        staging_bytes_deleted=deleted_bytes,
        staging_dirs_skipped=skipped,
    )


def _inspect_staging_job_dir(
    job_dir: Path,
) -> tuple[datetime, int] | None:
    newest_mtime = datetime.fromtimestamp(
        job_dir.stat().st_mtime,
        tz=UTC,
    )
    size_bytes = 0
    for member_count, member in enumerate(
        job_dir.rglob("*"),
        start=1,
    ):
        if member_count > _MAX_STAGING_MEMBERS_PER_JOB or member.is_symlink():
            return None
        stat = member.stat()
        newest_mtime = max(
            newest_mtime,
            datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )
        if member.is_file():
            size_bytes += stat.st_size
    return newest_mtime, size_bytes


__all__ = [
    "ExportRetentionResult",
    "cleanup_stale_export_staging",
    "run_export_retention",
]
