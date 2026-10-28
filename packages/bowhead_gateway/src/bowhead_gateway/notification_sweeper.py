"""Notification Center archive-retention sweeper.

Runs in the gateway lifespan when Notification Center is enabled. It prunes
only archived recipient rows older than the configured retention period, then
removes orphan notification_items. Active action items and unread updates are
never eligible for this cleanup path.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bowhead_core.approval_store import ApprovalStore
    from bowhead_core.notification_service import NotificationService
    from bowhead_core.notification_store import NotificationStore

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 86_400
_DEFAULT_RETENTION_DAYS = 180
_BATCH_LIMIT = 500


def _metric_inc(name: str, labels: dict[str, str], value: float = 1.0) -> None:
    try:
        from bowhead_core import metrics as _m

        _m.inc(name, labels=labels, value=value)
    except Exception:  # noqa: S110
        pass


async def sweep_notification_archive_once(
    *,
    store: NotificationStore,
    notification_service: NotificationService | None = None,
    approval_store: ApprovalStore | None = None,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
    limit: int = _BATCH_LIMIT,
    now: datetime | None = None,
) -> dict[str, int]:
    retention_days = max(1, retention_days)
    now = now or datetime.now(tz=UTC)
    cutoff = (now - timedelta(days=retention_days)).isoformat(timespec="seconds")
    if notification_service is not None and approval_store is not None:
        reconciled = await notification_service.reconcile_approval_required(
            approval_store,
            limit=limit,
        )
        if reconciled:
            log.info("notification_sweeper.reconciled approval_required=%s", reconciled)
    try:
        counts = await store.prune_archived(older_than=cutoff, limit=limit)
    except Exception:
        _metric_inc(
            "bowhead_notification_sweeper_runs_total",
            labels={"outcome": "failed"},
        )
        raise

    _metric_inc(
        "bowhead_notification_sweeper_runs_total",
        labels={"outcome": "success"},
    )
    recipients_deleted = counts.get("recipients_deleted", 0)
    items_deleted = counts.get("items_deleted", 0)
    if recipients_deleted:
        _metric_inc(
            "bowhead_notification_sweeper_deleted_total",
            labels={"kind": "recipient"},
            value=float(recipients_deleted),
        )
    if items_deleted:
        _metric_inc(
            "bowhead_notification_sweeper_deleted_total",
            labels={"kind": "item"},
            value=float(items_deleted),
        )
    if recipients_deleted or items_deleted:
        log.info(
            "notification_sweeper.tick recipients_deleted=%s items_deleted=%s",
            recipients_deleted,
            items_deleted,
        )
    return counts


async def run_notification_sweeper(
    *,
    store: NotificationStore,
    notification_service: NotificationService | None = None,
    approval_store: ApprovalStore | None = None,
    interval_s: int = _DEFAULT_INTERVAL_S,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
    stop_event: asyncio.Event | None = None,
) -> None:
    log.info(
        "notification_sweeper.started interval_s=%s retention_days=%s",
        interval_s,
        retention_days,
    )
    try:
        while True:
            try:
                await sweep_notification_archive_once(
                    store=store,
                    notification_service=notification_service,
                    approval_store=approval_store,
                    retention_days=retention_days,
                )
            except Exception as exc:
                log.warning("notification_sweeper.tick_failed err=%s", exc)
            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
                    break
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        log.info("notification_sweeper.cancelled")
        raise
    finally:
        log.info("notification_sweeper.stopped")


def env_interval_s() -> int:
    raw = os.environ.get("BOWHEAD_NOTIFICATION_SWEEPER_INTERVAL_S")
    if not raw:
        return _DEFAULT_INTERVAL_S
    try:
        return max(300, int(raw))
    except ValueError:
        return _DEFAULT_INTERVAL_S


def env_retention_days() -> int:
    raw = os.environ.get("BOWHEAD_NOTIFICATION_RETENTION_DAYS")
    if not raw:
        return _DEFAULT_RETENTION_DAYS
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_RETENTION_DAYS
