"""Scheduler loop (#126 Step 2 — in-process APScheduler-alternative, zero new dep).

Runs as a background asyncio task inside the gateway process. Every TICK_SECONDS,
scans `scheduled_jobs` for active jobs whose `next_run_at <= now`, invokes
`run_scheduled_job(job)` (orchestrator + notifier), computes next `next_run_at`.

Cron parsing: standard 5-field cron via `cronsim`（day-of-month / month / 列表 / 区间 / `L` 月末均支持，
dow `0 = Sunday`）. Policy（语法之外的平台规则）: sub-hourly 一律拒绝（`MIN_INTERVAL_MINUTES`），
`@manual` 表示无自动调度. 非法/解析失败的 cron → job 置 status=expired 并 warn log.

默认 OFF (BOWHEAD_SCHEDULER_ENABLED=1 才启动)，避免 dev 误跑影响测试.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cronsim import CronSim, CronSimError

from bowhead_core.iana_time_zone import load_iana_time_zone
from bowhead_core.published_diagnostics import (
    SCHEDULED_SKIPPED_CODES,
    normalize_scheduled_failure_code,
)
from bowhead_core.scheduled_store import (
    ScheduledJob,
    ScheduledJobStore,
    ScheduledRunSourceError,
)

logger = logging.getLogger(__name__)

TICK_SECONDS = 30


@dataclass(frozen=True)
class RunOutcome:
    """一次派发的结果。

    为什么不是原来的 ``(turn_id, error)`` 二元组：挂起（跑到歧义点等 owner 回答）既不是
    成功也不是失败，二元组表达不了 —— 返 ``(turn_id, None)`` 会被判成 success 并触发送达，
    返 ``(None, code)`` 则 run 落 failed 且 turn_id 为空、深链断掉。``session_id`` 是给
    needs_input 通知做深链用的（run 身份在调用方手里，通知也在调用方发）。
    """

    turn_id: str | None = None
    error: str | None = None
    awaiting: bool = False
    session_id: str | None = None


RunCallback = Callable[[ScheduledJob], Coroutine[Any, Any, "RunOutcome"]]
AdmissionCallback = Callable[[ScheduledJob], Coroutine[Any, Any, bool]]
ReconcileCallback = Callable[[], Coroutine[Any, Any, None]]


MANUAL_CRON = "@manual"  # a routine with no schedule — runs only via run-now


def parse_cron_to_next(cron: str, now: datetime) -> datetime | None:
    """Return next fire time > now (standard 5-field cron via cronsim, incl. day-of-month / month /
    `L`). None if invalid or @manual."""
    c = cron.strip()
    if c == MANUAL_CRON:
        return None  # manual routine: no auto-schedule (next_run_at stays NULL → never picked by _tick)
    try:
        return next(CronSim(c, now))
    except (CronSimError, StopIteration):
        return None


def next_run_for_job(cron: str, tz: str | None, now_utc: datetime) -> datetime | None:
    """Next fire time in UTC for a job whose ``cron`` is interpreted in its own ``tz`` (IANA name).

    Localizes ``now_utc`` → ``tz``, computes via ``parse_cron_to_next`` (so "0 9 * * *" means 9am local),
    then converts back to UTC for storage/compare. Falls back to UTC on an empty/invalid tz. This is the
    fix for the v1 bug where cron was compared in UTC regardless of the job's tz.
    """
    zone = load_iana_time_zone(tz) if tz else UTC
    if zone is None:
        # Unknown names, directories ("America") and non-zone files ("posixrules") never stop the loop.
        logger.warning("scheduler.bad_tz tz=%r → UTC", tz)
        zone = UTC
    nxt_local = parse_cron_to_next(cron, now_utc.astimezone(zone))
    return nxt_local.astimezone(UTC) if nxt_local is not None else None


MIN_INTERVAL_MINUTES = 60
# Frequency-policy probe: fixed deterministic start; simulate up to N fires / horizon and check minimum intervals.
_PROBE_START = datetime(2000, 1, 1, tzinfo=UTC)
_PROBE_HORIZON = timedelta(days=400)
_PROBE_MAX_FIRES = 400


def cron_validation_error(cron: str) -> str | None:
    """Validate a routine cron: standard 5-field syntax (cronsim — day-of-month / month / lists /
    ranges / `L` supported), not sub-hourly, schedulable. ``@manual`` is valid. Returns an error
    message or None. Shared by the create route and the conversational scheduled_job_create tool."""
    c = cron.strip()
    if c == MANUAL_CRON:
        return None
    parts = c.split()
    if len(parts) != 5:
        return f"invalid cron (expect 5 fields, got {len(parts)}): {cron!r}"
    minute = parts[0]
    if minute.startswith("*/") and minute[2:].isdigit() and int(minute[2:]) > 60:
        # cron minute steps don't carry across the hour: */90 lands at minute 0 every hour
        # → silently runs hourly, not every 90 min. Reject rather than mislead.
        step = int(minute[2:])
        return (
            f"'*/{step}' (every {step} min) isn't expressible as a cron minute step — it would "
            f"silently run hourly. Use '0 * * * *' for hourly. Got {cron!r}."
        )
    try:
        fires = CronSim(c, _PROBE_START)
    except CronSimError as exc:
        return f"invalid cron {cron!r}: {exc}"
    horizon = _PROBE_START + _PROBE_HORIZON
    prev: datetime | None = None
    fired = False
    for _ in range(_PROBE_MAX_FIRES):
        try:
            fire = next(fires)
        except StopIteration:
            break
        if fire > horizon:
            break
        fired = True
        if prev is not None and fire - prev < timedelta(minutes=MIN_INTERVAL_MINUTES):
            return f"schedule too frequent: minimum interval is {MIN_INTERVAL_MINUTES} minutes (sub-hourly not allowed). Got cron {cron!r}."
        prev = fire
    if not fired:
        return f"unschedulable cron {cron!r}: it never fires within {_PROBE_HORIZON.days} days."
    return None


def classify_run_status(turn_id: str | None, err: str | None) -> str:
    """Map a ``RunCallback`` ``(turn_id, err)`` result to a run status. License/credit denials are
    ``skipped`` (NOT ``failed``) so they don't bump failure_count / trip the 3-strike auto-pause and stay
    resumable once the license/credit is restored. Shared by the scheduler loop and the run-now endpoint —
    single source of truth for the skip-vs-fail rule.
    """
    if turn_id and not err:
        return "success"
    if not turn_id and (err is None or err in SCHEDULED_SKIPPED_CODES):
        return "skipped"
    return "failed"


class SchedulerLoop:
    def __init__(
        self,
        store: ScheduledJobStore,
        run_cb: RunCallback,
        tick_seconds: int = TICK_SECONDS,
        notify: Any = None,
        admit_cb: AdmissionCallback | None = None,
        reconcile_cb: ReconcileCallback | None = None,
        resume_probe: Callable[[str], Coroutine[Any, Any, bool]] | None = None,
    ):
        self.store = store
        self.run_cb = run_cb
        self.tick = tick_seconds
        self.notify = notify  # NotificationService | None — failed/paused/recovered notifications
        self.admit_cb = admit_cb
        # S3 pause callbacks (gateway-injected; core does not know app.state):
        # reconcile_cb first reconciles answered pauses each tick; resume_probe checks whether a paused turn
        # is resuming (do not expire an active resumption). Run + clarification expiration uses the store's
        # single-transaction API, without cross-store best-effort callbacks.
        self.reconcile_cb = reconcile_cb
        self.resume_probe = resume_probe
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="scheduler-loop")
        logger.info("scheduler_loop.started tick=%ds", self.tick)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                self._task.cancel()
            self._task = None
        logger.info("scheduler_loop.stopped")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                logger.exception("scheduler_loop.tick_failed: %s", e)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick)

    async def _tick(self) -> None:
        now = datetime.now(tz=UTC)
        # Reconcile paused runs first (S3): a user may have answered elsewhere and the turn finished while run remains paused.
        # Before scanning, so due-time expiration below sees only pauses **still genuinely awaiting users**.
        if self.reconcile_cb is not None:
            try:
                await self.reconcile_cb()
            except Exception as exc:
                logger.exception("scheduler.reconcile_failed err=%s", exc)
        # Fetch all active jobs with next_run_at ≤ now (simple scan; v2 adds index-based query).
        active = await self._list_active_due(now_iso=now.isoformat())
        for job in active:
            # Due while prior run awaits input (S3): expire the old pause and replace it rather than skip indefinitely.
            # Check **absence of resolved rows**, not still-pending rows: system 24h TTL converts
            # pending to expired; checking still-pending misreads swept rows as answered,
            # permanently taking in-flight skip and deadlocking the paused run.
            if await self._expire_stale_awaiting(job):
                continue
            # Skip if a run is already in-flight (run-now, or a still-running prior cron turn) — the same
            # job must never run two turns at once (double-charge + duplicate result). Pairs with the
            # run-now has_active_run check. The job stays due (next_run_at unchanged) and fires on a later
            # tick once the in-flight run clears.
            if await self.store.has_active_run(job.id):
                logger.info("scheduler.skip_in_flight job=%s", job.id)
                continue
            if self.admit_cb is not None:
                try:
                    admitted = await self.admit_cb(job)
                except Exception as exc:
                    logger.exception(
                        "scheduler.admission_failed job=%s err=%s",
                        job.id,
                        exc,
                    )
                    admitted = False
                if not admitted:
                    logger.info("scheduler.lifecycle_denied job=%s", job.id)
                    continue
            from bowhead_core.scheduled_store import ActiveRunExistsError

            try:
                run = await self.store.start_run(job.id, source="scheduler")
            except ActiveRunExistsError:
                # Lost the start_run race (run-now claimed it between our has_active_run check and
                # the INSERT). DB unique index held the line; just skip this tick.
                logger.info("scheduler.skip_in_flight_race job=%s", job.id)
                continue
            except ScheduledRunSourceError as exc:
                logger.info(
                    "scheduler.skip_stale_source job=%s cause=%s",
                    job.id,
                    exc.cause,
                )
                continue
            try:
                outcome = await self.run_cb(job)
                # Pause: leave run nonterminal, transition to awaiting_input for owner response (S3). **Do not** use
                # finish_run / strikes / failure notices; it has not failed, only remains unfinished.
                if outcome.awaiting and outcome.turn_id:
                    awaiting = await self.store.mark_run_awaiting_if_pending(
                        run.id,
                        outcome.turn_id,
                    )
                    if awaiting.won:
                        await self._notify_needs_input(job, run.id, outcome.session_id)
                    else:
                        logger.warning(
                            "scheduler.awaiting_rejected job=%s run=%s turn=%s cause=%s",
                            job.id,
                            run.id,
                            outcome.turn_id,
                            awaiting.cause,
                        )
                        await self.store.finish_run(
                            run.id,
                            status="failed",
                            turn_id=outcome.turn_id,
                            error="scheduled_turn_failed",
                            expected_status="running",
                        )
                    # A due time during pause must not trigger immediate catch-up on completion; advance next_run_at normally
                    # (as loop tail below does), enabling Q2's survival until the next fire.
                    await self._advance_next_run(job, now)
                    continue
                turn_id, err = outcome.turn_id, normalize_scheduled_failure_code(outcome.error)
                # entitlement/credit denials → `skipped` (not `failed`): no failure_count bump / no 3-strike
                # auto-pause, resumable after license renewal or credit top-up. See classify_run_status.
                status = classify_run_status(turn_id, err)
                await self.store.finish_run(run.id, status=status, turn_id=turn_id, error=err)
                # Update failure count + notify (failed/paused/recovered).
                if status == "failed":
                    paused = await self._bump_failure_count(job)
                    await self._notify_failed(job, err, paused=paused)
                else:
                    recovered = status == "success" and job.failure_count > 0
                    await self._reset_failure_count(job)
                    if recovered:
                        await self._notify_recovered(job, run.id)
            except Exception as e:
                logger.exception("scheduler_loop.run_cb_failed job=%s: %s", job.id, e)
                await self.store.finish_run(
                    run.id,
                    status="failed",
                    error="scheduled_run_dispatch_failed",
                )
                await self._bump_failure_count(job)
            await self._advance_next_run(job, now)

    async def _expire_stale_awaiting(self, job: ScheduledJob) -> bool:
        """job 到点了、上一轮却还挂着等人 → 过期换新。返回 True 表示本轮已处理完（跳过后续）。

        「等到下一次 fire」正是 Q2 的语义：挂起活满一个周期。到点仍没人答，就记
        ``scheduled_input_timeout``（计三振）并让新一轮跑起来 —— 周报不能因为上周没人回话
        就永远停摆。resume 正在进行中（已有人回答）时不过期，让它跑完。
        """
        runs = await self.store.list_awaiting_runs()
        mine = [r for r in runs if r.job_id == job.id]
        if not mine:
            return False
        for run in mine:
            if self.resume_probe is not None and run.turn_id:
                try:
                    if await self.resume_probe(run.turn_id):
                        logger.info("scheduler.awaiting_resume_in_flight job=%s run=%s", job.id, run.id)
                        return True  # Answered and resuming → treat as in flight, do not claim it.
                except Exception as exc:
                    logger.warning("scheduler.resume_probe_failed run=%s err=%s", run.id, exc)
            transition = await self.store.finish_awaiting_with_clarification_invalidation(
                run.id,
                run_status="failed",
                run_error="scheduled_input_timeout",
                clarification_status="expired",
                clarification_reason="scheduled_input_timeout",
            )
            if transition.won:
                logger.info("scheduler.awaiting_expired job=%s run=%s", job.id, run.id)
                paused = await self._bump_failure_count(job)
                await self._notify_failed(job, "scheduled_input_timeout", paused=paused)
                # Expiration also leaves awaiting: without closing its badge, the needs-input notification stays red
                # forever while its card is invalid, leaving no question to answer.
                await self._resolve_needs_input(job, run.id, reason="input_timeout")
        return False  # After expiration, this tick can start a new run normally.

    async def _advance_next_run(self, job: ScheduledJob, now: datetime) -> None:
        """按 job.tz 解释 cron 算下一次触发时刻（cron 不受支持 → 暂停）。"""
        nxt = next_run_for_job(job.cron, job.tz, now)
        if nxt is None:
            logger.warning(
                "scheduler_loop.cron_unsupported job=%s cron=%s → pausing",
                job.id,
                job.cron,
            )
            await self.store.patch(job.id, status="paused")
        else:
            await self._set_next_run_at(job.id, nxt.isoformat())

    async def _list_active_due(self, *, now_iso: str) -> list[ScheduledJob]:
        import aiosqlite

        async with aiosqlite.connect(self.store._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scheduled_jobs "
                "WHERE status = 'active' AND next_run_at IS NOT NULL "
                "AND next_run_at <= ? "
                "ORDER BY next_run_at ASC LIMIT 50",
                (now_iso,),
            ) as cur:
                rows = await cur.fetchall()
        from bowhead_core.scheduled_store import _row_to_job

        return [_row_to_job(r) for r in rows]

    async def _set_next_run_at(self, job_id: str, iso_ts: str) -> None:
        # Single writer: the store sets updated_at = now (the old inline UPDATE wrote the *future*
        # fire time into updated_at). Keeps next_run_at persistence in one place.
        await self.store.set_next_run_at(job_id, iso_ts)

    async def _bump_failure_count(self, job: ScheduledJob) -> bool:
        """Increment failure_count; auto-pause at 3. Returns True if this run tripped the pause."""
        import aiosqlite

        new_count = job.failure_count + 1
        new_status = "paused" if new_count >= 3 else job.status
        async with aiosqlite.connect(self.store._db_path) as db:
            await db.execute(
                "UPDATE scheduled_jobs SET failure_count = ?, status = ?, updated_at = ? WHERE id = ?",
                (new_count, new_status, datetime.now(tz=UTC).isoformat(), job.id),
            )
            await db.commit()
        paused = new_status == "paused" and job.status != "paused"
        if paused:
            logger.warning(
                "scheduler_loop.auto_paused job=%s failure_count=%d",
                job.id,
                new_count,
            )
        return paused

    async def _reset_failure_count(self, job: ScheduledJob) -> None:
        if job.failure_count == 0:
            return
        import aiosqlite

        async with aiosqlite.connect(self.store._db_path) as db:
            await db.execute(
                "UPDATE scheduled_jobs SET failure_count = 0, updated_at = ? WHERE id = ?",
                (datetime.now(tz=UTC).isoformat(), job.id),
            )
            await db.commit()

    async def _notify_failed(self, job: ScheduledJob, err: str | None, *, paused: bool) -> None:
        """Best-effort failed/paused notification — never let a notification error break the loop."""
        if self.notify is None:
            return
        try:
            await self.notify.emit_scheduled_failed(job, err)
            if paused:
                await self.notify.emit_scheduled_paused(job)
        except Exception as exc:
            logger.warning("scheduler.notify_failed job=%s err=%s", job.id, exc)

    async def _notify_needs_input(self, job: ScheduledJob, run_id: str, session_id: str | None) -> None:
        """挂起通知（best-effort）。通知失败不该让任务卡死——run 已经是 awaiting 了，
        用户仍能从设置页的运行历史看到并点进去。"""
        if self.notify is None:
            return
        try:
            await self.notify.emit_routine_needs_input(job, run_id=run_id, session_id=session_id)
        except Exception as exc:
            logger.warning("scheduler.notify_needs_input_failed job=%s err=%s", job.id, exc)

    async def _resolve_needs_input(self, job: ScheduledJob, run_id: str, *, reason: str) -> None:
        """关掉挂起通知的待办徽章（best-effort）。

        `needs_input` 在 ``ACTION_REQUIRED_TYPES`` 里，action 计数只看 ``state='open'``——
        **离开 awaiting 的每一条路都得关它**，否则铃铛永久红，用户学会无视铃铛后整个挂起
        机制形同虚设。`resolve_routine_needs_input` 本身幂等（没有 open 条目就返 None），
        所以只绑在 CAS 赢家上、重复调用也无害。
        """
        if self.notify is None:
            return
        try:
            await self.notify.resolve_routine_needs_input(job, run_id=run_id, reason=reason)
        except Exception as exc:
            logger.warning("scheduler.resolve_needs_input_failed job=%s err=%s", job.id, exc)

    async def _notify_recovered(self, job: ScheduledJob, run_id: str) -> None:
        if self.notify is None:
            return
        try:
            await self.notify.emit_scheduled_recovered(job, run_id)
        except Exception as exc:
            logger.warning("scheduler.notify_recovered_failed job=%s err=%s", job.id, exc)


def is_scheduler_enabled() -> bool:
    return (os.environ.get("BOWHEAD_SCHEDULER_ENABLED") or "0") == "1"
