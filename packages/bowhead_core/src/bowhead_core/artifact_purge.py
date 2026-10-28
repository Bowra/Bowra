"""Durable, create-once deletion identity shared by manual purge and GC.

A committed claim is irrevocable: uncertain storage outcomes are retried against
the same exact key, never unlocked by age. No database transaction spans blob I/O.
This is not document retention or authorization; callers still own those checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite
import structlog

from bowhead_core.artifact_store import _open_db
from bowhead_core.documents.retention import active_ref_sql

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bowhead_core.artifact_store import ArtifactBackend

log = structlog.get_logger(__name__)

SNAPSHOT_PIN_SQL = (
    "EXISTS(SELECT 1 FROM canvas_export_snapshot_assets p "
    "JOIN canvas_export_snapshots s ON s.id=p.snapshot_id "
    "WHERE p.artifact_id=artifacts.id AND julianday(s.expires_at)>julianday('now'))"
)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="microseconds")


async def purge_schema_available(db: aiosqlite.Connection) -> bool:
    """Read-only legacy-store compatibility; never bootstrap lifecycle DDL."""
    async with db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='artifact_purge_claims'") as cur:
        return await cur.fetchone() is not None


@dataclass(frozen=True)
class PurgeClaim:
    """Exact persisted target; never substitute a newly resolved storage key."""

    artifact_id: str
    path: str


@dataclass(frozen=True)
class PurgeResult:
    """Confirmed row removals and this invocation's storage operation outcomes."""

    rows_removed: int = 0
    files_removed: int = 0
    files_failed: int = 0


async def claim_artifact_purge(
    db_path: str,
    *,
    artifact_id: str,
    user_id: str | None,
    cutoff: str | None = None,
) -> PurgeClaim | None:
    """Recheck trash, owner/age and snapshot retention under a short writer lock.

    A missing migration fails before storage I/O. Already claimed identities may
    be resumed, including after a crash; a completed tombstone is a no-op.
    """
    async with _open_db(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT artifact_id,path,created_by,completed_at FROM artifact_purge_claims WHERE artifact_id=?",
            (artifact_id,),
        ) as cur:
            existing = await cur.fetchone()
        if existing is not None:
            if existing[3] is not None or (user_id is not None and existing[2] != user_id):
                return None
            return PurgeClaim(str(existing[0]), str(existing[1]))
        async with db.execute(
            "SELECT id,path FROM artifacts WHERE id=? AND deleted_at IS NOT NULL "
            "AND (? IS NULL OR created_by=?) AND (? IS NULL OR deleted_at<?) "
            f"AND NOT {SNAPSHOT_PIN_SQL} AND NOT {await active_ref_sql(db)} "
            "AND NOT EXISTS(SELECT 1 FROM artifacts other WHERE other.path=artifacts.path AND other.id<>artifacts.id)",
            (artifact_id, user_id, user_id, cutoff, cutoff),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        await db.execute(
            "INSERT INTO artifact_purge_claims(artifact_id,path,created_by,claimed_at) SELECT id,path,created_by,? FROM artifacts WHERE id=?",
            (_now(), artifact_id),
        )
        await db.commit()
        return PurgeClaim(str(row[0]), str(row[1]))


async def _complete_purge(db_path: str, claim: PurgeClaim) -> int:
    async with _open_db(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT path,completed_at FROM artifact_purge_claims WHERE artifact_id=?", (claim.artifact_id,)) as cur:
            row = await cur.fetchone()
        if row is None or row[0] != claim.path:
            raise RuntimeError("artifact purge identity mismatch")
        if row[1] is not None:
            return 0
        # Triggers prevent restoration/new references after claim. These legacy
        # attachment FKs predate CASCADE; all removals and completion commit once.
        await db.execute("DELETE FROM session_attachments WHERE artifact_id=?", (claim.artifact_id,))
        await db.execute("DELETE FROM turn_attachments WHERE artifact_id=?", (claim.artifact_id,))
        cur = await db.execute("DELETE FROM artifacts WHERE id=? AND path=?", (claim.artifact_id, claim.path))
        removed = cur.rowcount
        await db.execute(
            "UPDATE artifact_purge_claims SET completed_at=? WHERE artifact_id=? AND completed_at IS NULL",
            (_now(), claim.artifact_id),
        )
        await db.commit()
        return removed


async def purge_artifacts(
    db_path: str,
    backend: ArtifactBackend,
    artifact_ids: Sequence[str],
    *,
    user_id: str | None,
    cutoff: str | None = None,
) -> PurgeResult:
    """Delete exact claimed keys; failures keep the durable recovery work item."""
    removed = files = failed = 0
    for artifact_id in dict.fromkeys(artifact_ids):
        claim = await claim_artifact_purge(db_path, artifact_id=artifact_id, user_id=user_id, cutoff=cutoff)
        if claim is None:
            continue
        try:
            await backend.delete(claim.path)
            files += 1
        except FileNotFoundError:
            pass  # Confirmed absence is an idempotent successful storage result.
        except Exception as exc:  # noqa: BLE001 - next artifact may still be collectible
            failed += 1
            log.warning("artifact.purge_storage_failed", artifact_id=artifact_id, error_type=type(exc).__name__)
            continue
        removed += await _complete_purge(db_path, claim)
    return PurgeResult(rows_removed=removed, files_removed=files, files_failed=failed)
