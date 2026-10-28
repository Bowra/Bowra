"""Presigned-upload orphan sweeper — Sprint file-upload-presign Phase 1.5.

Background task started in gateway lifespan. Every _INTERVAL seconds it scans
``artifacts WHERE status='pending_upload' AND expires_at < now()`` and cleans:
  1. delete-from-storage FIRST (P1-S-10 — orphan storage on sweeper failure
     is worse than orphan DB row).
  2. cascade DELETE child rows (kb_files, session_attachments, turn_attachments)
     since the source tables lack ON DELETE CASCADE on artifact_id.
  3. DELETE the artifacts row last.

If backend.delete() raises non-404, the DB row is RETAINED so the next sweep
can retry — avoids unreachable storage orphans.

Tech-design §5.4.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite
from bowhead_core.artifact_store import _open_db
from bowhead_core.documents.inputs import cleanup_document_inputs
from bowhead_core.documents.upload_confirmation import cleanup_confirmations, has_confirmation_schema

if TYPE_CHECKING:
    from bowhead_core.kb.files_store import KbFilesStore

    from bowhead_core import ArtifactStore

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 600  # 10 min
_BATCH_LIMIT = 200  # cap per sweep tick


async def _sweep_once(
    *,
    artifact_store: ArtifactStore,
    kb_files_store: KbFilesStore | None,
    now_iso: str,
) -> dict[str, int]:
    """One sweep pass. Returns counter dict for telemetry.

    Selects pending_upload rows past expires_at, with per-row cleanup:
      0. CAS-CLAIM row: UPDATE status='pending_upload' → 'expiring'. Only the
         winner proceeds — confirm CAS (which only matches 'pending_upload')
         loses the race cleanly. Phase 1.5 review P0-1 fix for confirm-vs-
         sweeper TOCTOU.
      1. backend.delete(path) — S3Backend.delete now swallows 404 (Phase 1.5
         review P0-2); other errors abort + leave row in 'expiring' for next
         sweep retry.
      2. DELETE kb_files / session_attachments / turn_attachments cascade
      3. DELETE artifacts row WHERE status='expiring' (final gate)
    """
    metrics: dict[str, int] = {
        "scanned": 0,
        "cleaned": 0,
        "delete_failed": 0,
        "cascade_partial": 0,
    }
    # This protection is a storage invariant, not a generation-feature switch.
    await cleanup_confirmations(artifact_store, now=datetime.fromisoformat(now_iso).timestamp())
    await cleanup_document_inputs(artifact_store, now=datetime.fromisoformat(now_iso).timestamp())
    async with _open_db(artifact_store.db_path) as db:
        db.row_factory = aiosqlite.Row
        confirmation_guard = (
            " AND NOT EXISTS (SELECT 1 FROM artifact_upload_confirmations c WHERE c.artifact_id=artifacts.id)"
            if await has_confirmation_schema(db)
            else ""
        )
        cur = await db.execute(
            "SELECT id, path FROM artifacts WHERE status='pending_upload' AND expires_at IS NOT NULL AND expires_at < ?"
            + confirmation_guard
            + " LIMIT ?",
            (now_iso, _BATCH_LIMIT),
        )
        rows = await cur.fetchall()

    metrics["scanned"] = len(rows)
    for row in rows:
        aid = row["id"]
        path = row["path"]

        # 0. CAS-claim: flip pending_upload → expiring atomically. Loser
        #    (confirm racing us) does NOT match this predicate, so it can't
        #    proceed either. Wins exactly one of us.
        async with _open_db(artifact_store.db_path) as db:
            cas = await db.execute(
                "UPDATE artifacts SET status='expiring' WHERE id=? AND status='pending_upload' AND expires_at IS NOT NULL AND expires_at < ?"
                + confirmation_guard,
                (aid, now_iso),
            )
            claimed = cas.rowcount == 1
            await db.commit()
        if not claimed:
            # Confirm beat us, OR another sweeper pod (multi-process) claimed.
            log.info("presign_sweeper.race_lost aid=%s", aid)
            continue

        # 1. Storage delete. S3Backend.delete is 404-tolerant now (review
        #    P0-2 fix in artifact_store.py); any raised exception is a REAL
        #    failure — leave row in 'expiring' so next sweep retries.
        try:
            await artifact_store.backend.delete(path)
        except Exception as e:
            metrics["delete_failed"] += 1
            log.warning(
                "presign_sweeper.backend_delete_failed aid=%s err=%s — row stays in expiring, will retry next sweep",
                aid,
                e,
            )
            continue

        # 2. Cascade child rows. Track per-cascade success so cleaned only
        #    increments when ALL cascades + final DELETE succeed (review
        #    P1-3 metrics overcount fix).
        cascade_ok = True
        try:
            if kb_files_store is not None:
                async with aiosqlite.connect(kb_files_store._db_path) as kb_db:
                    await kb_db.execute(
                        "DELETE FROM kb_files WHERE artifact_id=?",
                        (aid,),
                    )
                    await kb_db.commit()
        except Exception as e:
            cascade_ok = False
            log.warning(
                "presign_sweeper.kb_files_cascade_failed aid=%s err=%s",
                aid,
                e,
            )

        async with _open_db(artifact_store.db_path) as db:
            try:
                await db.execute(
                    "DELETE FROM session_attachments WHERE artifact_id=?",
                    (aid,),
                )
            except aiosqlite.OperationalError as e:
                # Review P1-1: log instead of silent pass — schema drift /
                # disk full / readonly should surface to ops.
                cascade_ok = False
                log.warning(
                    "presign_sweeper.session_attachments_op_error aid=%s err=%s",
                    aid,
                    e,
                )
            try:
                await db.execute(
                    "DELETE FROM turn_attachments WHERE artifact_id=?",
                    (aid,),
                )
            except aiosqlite.OperationalError as e:
                cascade_ok = False
                log.warning(
                    "presign_sweeper.turn_attachments_op_error aid=%s err=%s",
                    aid,
                    e,
                )
            # 3. Artifact row last — gated on 'expiring' so a future
            #    concurrent (impossible in single-pod, defensive otherwise)
            #    actor can't lose work.
            final = await db.execute(
                "DELETE FROM artifacts WHERE id=? AND status='expiring'",
                (aid,),
            )
            await db.commit()
            if final.rowcount != 1:
                cascade_ok = False

        if cascade_ok:
            metrics["cleaned"] += 1
        else:
            metrics["cascade_partial"] += 1

    if metrics["scanned"]:
        log.info(
            "presign_sweeper.tick scanned=%s cleaned=%s delete_failed=%s cascade_partial=%s",
            metrics["scanned"],
            metrics["cleaned"],
            metrics["delete_failed"],
            metrics["cascade_partial"],
        )
    return metrics


async def run_presign_sweeper(
    *,
    artifact_store: ArtifactStore,
    kb_files_store: KbFilesStore | None,
    interval_s: int = _DEFAULT_INTERVAL_S,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Long-running lifespan task. Cancellable via stop_event.set() OR
    asyncio.Task.cancel().

    Sleeps between sweeps; runs first sweep immediately at startup so
    expired-but-stuck rows get cleaned promptly after a long gateway downtime.
    """
    log.info("presign_sweeper.started interval_s=%s", interval_s)
    try:
        while True:
            now_iso = datetime.now(tz=UTC).isoformat(timespec="seconds")
            try:
                await _sweep_once(
                    artifact_store=artifact_store,
                    kb_files_store=kb_files_store,
                    now_iso=now_iso,
                )
            except Exception as e:
                log.warning("presign_sweeper.tick_failed err=%s", e)
            # Sleep with cancellation support
            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
                    # Event set → exit loop
                    break
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        log.info("presign_sweeper.cancelled")
        raise
    finally:
        log.info("presign_sweeper.stopped")


def env_interval_s() -> int:
    """Allow ops override via env without restarting via settings.py edit."""
    raw = os.environ.get("BOWHEAD_PRESIGN_SWEEPER_INTERVAL_S")
    if not raw:
        return _DEFAULT_INTERVAL_S
    try:
        v = int(raw)
        return max(30, v)  # floor at 30s to prevent runaway loops
    except ValueError:
        return _DEFAULT_INTERVAL_S
