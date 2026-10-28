"""V3 PRD-B · Memory GC cron — daily housekeeping.

Tasks (idempotent, all use datetime() SQL normalize per spike #19):
  1. Hard-delete trash > 30d in memory_entries
  3. Auto-reject pending candidates > 30d
  Account erasure owns removed-user Memory; activity time is never a deletion deadline.
  5. Hard-delete reset snapshots > 30d
  6. Trim metrics_event_log > 90d
  7. Hard-delete memory_pack_snapshots > 180d (V4 PRD-D-4-4)
  8. Hard-delete invalidated memory_note_anchors > 180d (V4 PRD-D-4-1)
  9. Mark orphan memory_note_anchors stale (bullet_text_lost in content) (V4 PRD-D-4-1)

Singleton lock: each task uses a single-row UPDATE on `memory_gc_state` —
rowcount > 0 means this worker won the lock for the next interval.

Scheduling: `memory_gc_loop(...)` runs once at startup + every 24h thereafter.
Registered from gateway main.py lifespan (background task).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


async def try_acquire_gc_lock(
    db_path: str | Path,
    *,
    cron_name: str,
    interval_seconds: int = 24 * 3600,
) -> bool:
    """Single-row UPDATE-based singleton lock.

    First call inserts the row (rowcount=1). Subsequent calls only update
    if `last_run_at` is older than `interval_seconds` ago. Other workers
    racing on the same cron_name see rowcount=0 and skip.

    Uses `datetime()` to normalize ISO 'T' vs sqlite ' ' format (spike #19).
    """
    interval_arg = f"-{int(interval_seconds)} seconds"
    async with aiosqlite.connect(str(db_path)) as db:
        # Step 1: try insert (first run only). OR IGNORE: silent fail if row exists.
        await db.execute(
            "INSERT OR IGNORE INTO memory_gc_state (cron_name, last_run_at) VALUES (?, datetime('now', '-100 years'))",
            (cron_name,),
        )
        # Step 2: race-safe update. Only one worker wins.
        cur = await db.execute(
            "UPDATE memory_gc_state SET last_run_at = ? WHERE cron_name = ? AND datetime(last_run_at) < datetime('now', ?)",
            (_now(), cron_name, interval_arg),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def run_memory_gc_once(
    *,
    db_path: str | Path,
    user_memory_store: Any,  # UserMemoryStore
    candidate_store: Any | None = None,  # MemoryCandidateStore | None
    metrics_log: Any | None = None,  # MetricsLog | None
    notes_store: Any | None = None,  # MemoryNotesStore | None (V3.5 PRD-C)
    snapshots_store: Any | None = None,  # MemoryPackSnapshotsStore | None (V4 PRD-D-4-4)
    anchors_store: Any | None = None,  # MemoryNoteAnchorsStore | None (V4 PRD-D-4-1)
    notification_service: Any | None = None,  # NotificationService | None (review-fix P1: badge resolve)
    destructive_operation_store: Any | None = None,
    trash_max_age_days: int = 30,
    candidate_max_age_days: int = 30,
    expiring_warn_days: int = 23,  # S2 C3: pending expiry warning threshold (< candidate_max_age_days).
    metrics_max_age_days: int = 90,
    snapshots_max_age_days: int = 180,
    anchors_invalidated_max_age_days: int = 180,
) -> dict[str, int]:
    """Run all GC tasks once. Returns per-task counts (rows affected)."""
    stats: dict[str, int] = {}

    # 1/2/2b/5. All content-bearing 30-day expiry runs through the durable
    # privacy ledger + per-user barrier. If readiness is closed, retain data
    # longer rather than performing an unverifiable hard delete.
    retention_result: dict[str, int] = {}
    if destructive_operation_store is not None:
        try:
            retention_result = await destructive_operation_store.gc_expired_trash(
                max_age_days=trash_max_age_days,
            )
        except Exception as e:
            logger.warning(
                "memory_gc.durable_retention_failed: %s",
                e,
            )
    else:
        logger.warning(
            "memory_gc.durable_retention_unavailable; content-bearing purge skipped",
        )
    stats["memory_trash_purged"] = retention_result.get("entries", 0)
    stats["notes_trash_purged"] = retention_result.get("notes", 0)
    stats["reset_snapshots_purged"] = retention_result.get(
        "reset_snapshots",
        0,
    )

    # 3a. S2 C3: impending expiry warning for pending (age > expiring_warn_days, once ever), before auto-reject,
    # giving users one warning before silently auto-rejecting candidates (discovery §5.3).
    stats["candidates_expiry_warned"] = 0  # Default 0, like other stats; keep the key when disabled or on error.
    if candidate_store is not None and notification_service is not None:
        warned_users = 0
        try:
            expiring_users = await candidate_store.list_users_with_unwarned_expiring(
                warn_days=expiring_warn_days,
                max_age_days=candidate_max_age_days,
            )
            for uid in expiring_users:
                try:
                    # Mark then emit: mark once ever, then notify. Emission failure loses only one notification
                    # (creation badge and expiry auto-reject remain); acceptable best effort.
                    cnt = await candidate_store.mark_expiry_warned(
                        user_id=uid,
                        warn_days=expiring_warn_days,
                        max_age_days=candidate_max_age_days,
                    )
                    if cnt > 0:
                        await notification_service.emit_memory_candidates_expiring(
                            user_id=uid,
                            expiring_count=cnt,
                        )
                        warned_users += 1
                except Exception as e:
                    logger.debug("memory_gc.expiry_warn_failed user=%s: %s", uid, e)
        except Exception as e:
            logger.warning("memory_gc.expiry_warn_scan_failed: %s", e)
        stats["candidates_expiry_warned"] = warned_users

    # 3. candidates pending > 30d → auto reject
    if candidate_store is not None:
        try:
            # review-fix P1 (memory-revive S1): record affected users first; after auto-reject clears
            # pending, resolve their memory.candidate badges (otherwise the bell stays red forever).
            stale_users: list[str] = []
            if notification_service is not None:
                try:
                    stale_users = await candidate_store.list_users_with_stale_pending(
                        max_age_days=candidate_max_age_days,
                    )
                except Exception as e:
                    logger.debug("memory_gc.stale_users_probe_failed: %s", e)
            n = await candidate_store.auto_reject_stale(
                max_age_days=candidate_max_age_days,
            )
            stats["candidates_auto_rejected"] = n
            for stale_uid in stale_users:
                try:
                    remaining = await candidate_store.list_for_user(user_id=stale_uid, status="pending", limit=1)
                    if not remaining and notification_service is not None:
                        await notification_service.resolve_memory_candidates(user_id=stale_uid)
                except Exception as e:
                    logger.debug("memory_gc.badge_resolve_failed user=%s: %s", stale_uid, e)
        except Exception as e:
            logger.warning("memory_gc.candidate_reject_failed: %s", e)
            stats["candidates_auto_rejected"] = 0

    # 6. metrics_event_log > 90d
    if metrics_log is not None:
        try:
            n = await metrics_log.hard_delete_old(max_age_days=metrics_max_age_days)
            stats["metrics_log_trimmed"] = n
        except Exception as e:
            logger.warning("memory_gc.metrics_trim_failed: %s", e)
            stats["metrics_log_trimmed"] = 0

    # 7/8. Content-bearing 180-day retention uses durable resource receipts.
    auxiliary_result: dict[str, int] = {}
    if destructive_operation_store is not None and (snapshots_store is not None or anchors_store is not None):
        try:
            auxiliary_result = await destructive_operation_store.gc_expired_auxiliary(
                max_age_days=max(
                    snapshots_max_age_days,
                    anchors_invalidated_max_age_days,
                ),
            )
        except Exception as e:
            logger.warning(
                "memory_gc.auxiliary_retention_failed: %s",
                e,
            )
    stats["pack_snapshots_purged"] = auxiliary_result.get(
        "pack_snapshots",
        0,
    )
    stats["anchor_invalidated_purged"] = auxiliary_result.get(
        "anchors",
        0,
    )

    # 9. Mark orphan anchors stale (bullet_text removed from notes content)
    if anchors_store is not None and notes_store is not None:
        try:
            n = await anchors_store.mark_orphan_anchors(notes_store=notes_store)
            stats["anchor_orphans_marked"] = n
        except Exception as e:
            logger.warning("memory_gc.anchor_orphan_scan_failed: %s", e)
            stats["anchor_orphans_marked"] = 0

    return stats


async def memory_gc_loop(
    *,
    db_path: str | Path,
    user_memory_store: Any,
    candidate_store: Any | None = None,
    metrics_log: Any | None = None,
    notes_store: Any | None = None,  # V3.5 PRD-C
    snapshots_store: Any | None = None,  # V4 PRD-D-4-4
    anchors_store: Any | None = None,  # V4 PRD-D-4-1
    notification_service: Any | None = None,  # review-fix P1: badge resolve
    destructive_operation_store: Any | None = None,
    initial_delay_seconds: float = 60.0,
    interval_seconds: int = 24 * 3600,
    cron_name: str = "memory_gc_daily",
) -> None:
    """Runs once after `initial_delay_seconds`, then every `interval_seconds`.

    Uses `try_acquire_gc_lock` to ensure only one worker (multi-worker safe).
    Cancellable; on CancelledError, exits cleanly.
    """
    try:
        await asyncio.sleep(initial_delay_seconds)
        while True:
            try:
                acquired = await try_acquire_gc_lock(
                    db_path,
                    cron_name=cron_name,
                    interval_seconds=interval_seconds,
                )
                if acquired:
                    stats = await run_memory_gc_once(
                        db_path=db_path,
                        user_memory_store=user_memory_store,
                        candidate_store=candidate_store,
                        metrics_log=metrics_log,
                        notes_store=notes_store,
                        snapshots_store=snapshots_store,
                        anchors_store=anchors_store,
                        notification_service=notification_service,
                        destructive_operation_store=(destructive_operation_store),
                    )
                    if any(v for v in stats.values()):
                        logger.info("memory_gc.run_done %s", stats)
                else:
                    logger.debug("memory_gc.lock_skipped (other worker holds it)")
            except Exception as e:
                logger.warning("memory_gc.loop_iteration_failed: %s", e)
            # Sleep ~1h granularity, lock prevents duplicate runs
            await asyncio.sleep(min(3600, interval_seconds))
    except asyncio.CancelledError:
        pass
