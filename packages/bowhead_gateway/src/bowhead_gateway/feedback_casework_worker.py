"""教练办案 worker（B 线批 3a，tech-design §2.8.5）。

形制＝`notification_sweeper`（env 间隔、stop_event、CancelledError 纪律）。互斥在 casework 行上
（`claim` rowcount=1 才起草）；`try_acquire_gc_lock` 只做 tick 节流（时间租约，不是互斥）。

每 tick：①`sweep_expired` → 过期回执（`reopen_resolved=False`，成功后 `mark_receipt`）；
②候选年龄 ≥1 tick、open、无 casework 行或 failed(attempts<3)/blocked 的，按
(deep_dive, evidence_count desc, created_at) 排序，逐团队受疲劳闸约束；③逐条 `run_casework`。

功能已正式发布；权限、作用域和存储就绪校验仍须执行。"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from bowhead_core.feedback_candidate_store import FeedbackCandidate, FeedbackCandidateStore
from bowhead_core.feedback_casework_store import (
    MAX_ATTEMPTS,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    FeedbackCaseworkStore,
    iso_utc,
)
from bowhead_core.feedback_loop_flags import team_casework_enabled

from bowhead_gateway.feedback_casework import (
    REASON_CREDIT_BLOCKED,
    REASON_MODEL_UNCONFIGURED,
    resolve_coach_router,
    run_casework,
)
from bowhead_gateway.feedback_surfaces import COACH_KINDS

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 300
_DEFAULT_DAILY_CAP = 5
_DEFAULT_STALE_S = 900
_RETRY_AFTER = timedelta(hours=24)  # Attempts cap is not permanent abandonment: retry next day (failures are usually temporary).
_MAX_DRAFTS_PER_TICK = 20  # Global per-tick cap prevents an hour-long tick even with a large pool (review P2-10).
_RECEIPT_MAX_AGE = timedelta(days=7)  # Older expiries: mark only, no stale receipt backfill for everyone on the first tick (review P2-4).
_EPISODE_MIN_WINDOW_S = 120  # Minimum 402 episode continuity window (normally two tick intervals).


def env_interval_s() -> int:
    raw = os.environ.get("BOWHEAD_FEEDBACK_CASEWORK_INTERVAL_S")
    try:
        return max(60, int(raw)) if raw else _DEFAULT_INTERVAL_S
    except ValueError:
        return _DEFAULT_INTERVAL_S


def env_daily_cap() -> int:
    raw = os.environ.get("BOWHEAD_FEEDBACK_CASEWORK_DAILY_CAP")
    try:
        return max(0, int(raw)) if raw else _DEFAULT_DAILY_CAP
    except ValueError:
        return _DEFAULT_DAILY_CAP


def env_stale_s() -> int:
    raw = os.environ.get("BOWHEAD_FEEDBACK_CASEWORK_STALE_S")
    try:
        return max(60, int(raw)) if raw else _DEFAULT_STALE_S
    except ValueError:
        return _DEFAULT_STALE_S


def casework_disabled() -> bool:
    return os.environ.get("BOWHEAD_FEEDBACK_CASEWORK_DISABLED", "").strip() == "1"


def _priority(cand: FeedbackCandidate) -> tuple[int, int, str]:
    deep = 1 if cand.proposed.get("deep_dive") is True else 0
    return (-deep, -cand.evidence_count, cand.created_at)


async def sweep_expired_receipts(
    *,
    candidate_store: FeedbackCandidateStore,
    notification_service: Any | None,
    now: datetime | None = None,
) -> int:
    """过期回执：谁翻的都补；发成功才 mark，失败留给下 tick。"""

    now = now or datetime.now(UTC)
    rows = await candidate_store.sweep_expired(now)
    sent = 0
    for cand in rows:
        recipients = sorted({cand.proposer_id} | {c for e in cand.evidence if isinstance(c := e.get("contributor_id"), str) and c})
        # Even when notification-center-off makes emit return None, mark receipts; do not defer/backfill (same as route `_emit_receipts`).
        # Use expires_at, not updated_at, for age: sweep just changed historical rows to expired, setting updated_at to now.
        if notification_service is not None and (cand.expires_at or "") >= (now - _RECEIPT_MAX_AGE).isoformat():
            try:
                await notification_service.emit_feedback_candidate_receipt(
                    team_id=cand.team_id,
                    candidate_id=cand.id,
                    target_kind=cand.target_kind,
                    final_state="expired",
                    decided_at=cand.updated_at,
                    recipient_ids=recipients,
                    reopen_resolved=False,
                )
            except Exception:
                log.exception("feedback_casework.expired_receipt_failed id=%s", cand.id)
                continue
        await candidate_store.mark_receipt(cand.id)
        sent += 1
    return sent


async def _credit_blocked(user_store: Any, team_id: str) -> bool:
    try:
        team = await user_store.get_team(team_id)
    except Exception:
        log.warning("feedback_casework.credit_precheck_failed team=%s", team_id, exc_info=True)
        return False
    balance = getattr(team, "team_credit_balance", None)
    return isinstance(balance, int | float) and balance <= 0


async def _record_credit_episode(user_store: Any, cand: FeedbackCandidate) -> None:
    try:
        await user_store.record_credit_exhaustion_event(
            team_id=cand.team_id,
            user_id=cand.proposer_id,
            code="team_pool_exhausted",
        )
    except Exception:
        log.warning("feedback_casework.credit_event_failed team=%s", cand.team_id, exc_info=True)


async def casework_tick(
    app_state: Any,
    *,
    candidate_store: FeedbackCandidateStore,
    casework_store: FeedbackCaseworkStore,
    user_store: Any,
    session_store: Any,
    notification_service: Any | None,
    daily_cap: int,
    stale_s: int,
    min_age_s: int,
    now: datetime | None = None,
    team_settings_store: Any | None = None,
) -> dict[str, int]:
    now = now or datetime.now(UTC)
    stats = {"expired_receipts": 0, "drafted": 0, "blocked": 0, "failed": 0, "skipped_cap": 0, "skipped_team_off": 0}
    if casework_disabled():
        return stats
    stats["expired_receipts"] = await sweep_expired_receipts(candidate_store=candidate_store, notification_service=notification_service, now=now)
    pool = await candidate_store.list_casework_pool(older_than=now - timedelta(seconds=min_age_s))
    pool = [c for c in pool if c.target_kind in COACH_KINDS]
    existing = await casework_store.get_many([c.id for c in pool])
    todo: list[FeedbackCandidate] = []
    for cand in pool:
        row = existing.get(cand.id)
        if (
            row is None
            or row.status in (STATUS_QUEUED, STATUS_BLOCKED, STATUS_RUNNING)
            or (row.status == STATUS_FAILED and row.attempts < MAX_ATTEMPTS)
        ):
            todo.append(cand)  # Claim reclaims stale running; rejects unexpired claims.
        elif row.status == STATUS_FAILED and (row.updated_at or "") < iso_utc(now - _RETRY_AFTER):
            # Attempts cap is not permanent abandonment. 116 device (2026-09-09): the old 60s timeout exhausted candidates' attempts,
            # preventing drafts forever even after fixing the timeout. Transient failures became permanent loss (timeouts/upstream 402/quotas recover).
            # Retry once the next day, still bounded by daily cap, team switch and per-tick cap.
            todo.append(cand)
    todo.sort(key=_priority)
    per_team_started: dict[str, int] = {}
    per_team_enabled: dict[str, bool] = {}
    per_team_router: dict[str, Any] = {}
    per_team_credit_blocked: dict[str, bool] = {}
    per_team_episode_recorded: dict[str, bool] = {}
    drafts_this_tick = 0
    for cand in todo:
        if drafts_this_tick >= _MAX_DRAFTS_PER_TICK:
            break
        team_id = cand.team_id
        if team_id not in per_team_enabled:
            per_team_enabled[team_id] = await team_casework_enabled(team_settings_store, team_id)
        if not per_team_enabled[team_id]:
            stats["skipped_team_off"] += 1  # Team coach off: no row/block/cap use; explicit adoption unaffected.
            continue
        started = per_team_started.get(team_id)
        if started is None:
            started = await casework_store.count_started_today(team_id, now)
            per_team_started[team_id] = started
        if started >= daily_cap:
            stats["skipped_cap"] += 1
            continue
        # Check missing model / zero balance before claim: no lock/cap use; reevaluate each tick (review P1-1).
        if team_id not in per_team_credit_blocked:
            per_team_credit_blocked[team_id] = await _credit_blocked(user_store, team_id)
        if per_team_credit_blocked[team_id]:
            await casework_store.ensure_row(cand.id, team_id, trigger="cron")
            if team_id not in per_team_episode_recorded:
                since = now - timedelta(seconds=max(2 * min_age_s, _EPISODE_MIN_WINDOW_S))
                per_team_episode_recorded[team_id] = await casework_store.has_blocked(team_id, reason_code=REASON_CREDIT_BLOCKED, since=since)
            fresh = await casework_store.block(cand.id, reason_code=REASON_CREDIT_BLOCKED)
            if fresh and not per_team_episode_recorded[team_id]:
                await _record_credit_episode(user_store, cand)  # One record per team episode (review P2-5).
                per_team_episode_recorded[team_id] = True
            stats["blocked"] += 1
            continue
        if team_id not in per_team_router:
            per_team_router[team_id] = await resolve_coach_router(app_state, team_id)
        router = per_team_router[team_id]
        if router is None:
            await casework_store.ensure_row(cand.id, team_id, trigger="cron")
            await casework_store.block(cand.id, reason_code=REASON_MODEL_UNCONFIGURED)
            stats["blocked"] += 1
            continue
        trigger = "major_case" if (cand.proposed.get("deep_dive") is True or cand.evidence_count >= 3) else "cron"
        if not await casework_store.claim(cand.id, team_id, trigger=trigger, stale_after_s=stale_s):
            continue
        per_team_started[team_id] = started + 1
        drafts_this_tick += 1
        try:
            outcome = await run_casework(
                router=router,
                user_store=user_store,
                session_store=session_store,
                casework_store=casework_store,
                cand=cand,
                trigger=trigger,
            )
        except Exception:
            log.exception("feedback_casework.run_failed id=%s", cand.id)
            await casework_store.fail(cand.id, reason_code="coach_unavailable", running_only=True)
            stats["failed"] += 1
            continue
        key = "drafted" if outcome.status == "done" else outcome.status
        stats[key] = stats.get(key, 0) + 1
    return stats


async def run_feedback_casework_worker(
    app_state: Any,
    *,
    candidate_store: FeedbackCandidateStore,
    casework_store: FeedbackCaseworkStore,
    user_store: Any,
    session_store: Any,
    notification_service: Any | None,
    interval_s: int | None = None,
    stop_event: asyncio.Event | None = None,
    team_settings_store: Any | None = None,
) -> None:
    interval = interval_s or env_interval_s()
    log.info("feedback_casework.started interval_s=%s daily_cap=%s", interval, env_daily_cap())
    try:
        while True:
            try:
                stats = await casework_tick(
                    app_state,
                    candidate_store=candidate_store,
                    casework_store=casework_store,
                    user_store=user_store,
                    session_store=session_store,
                    notification_service=notification_service,
                    daily_cap=env_daily_cap(),
                    stale_s=env_stale_s(),
                    min_age_s=interval,
                    team_settings_store=team_settings_store,
                )
                if any(stats.values()):
                    log.info("feedback_casework.tick %s", stats)
            except Exception as exc:
                log.warning("feedback_casework.tick_failed err=%s", exc, exc_info=True)
            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                    break
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval)
    except asyncio.CancelledError:
        log.info("feedback_casework.cancelled")
        raise
    finally:
        log.info("feedback_casework.stopped")
