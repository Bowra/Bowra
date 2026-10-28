"""Scheduler loop + cron parser tests (#126 Step 2)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from bowhead_core.scheduler_loop import RunOutcome

if TYPE_CHECKING:
    from pathlib import Path


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


def test_cron_every_minute() -> None:
    from bowhead_core.scheduler_loop import parse_cron_to_next

    now = _dt("2026-04-25T12:00:30")
    nxt = parse_cron_to_next("* * * * *", now)
    assert nxt == _dt("2026-04-25T12:01:00")


def test_cron_every_n_minutes() -> None:
    from bowhead_core.scheduler_loop import parse_cron_to_next

    now = _dt("2026-04-25T12:00:30")
    nxt = parse_cron_to_next("*/5 * * * *", now)
    # Next five-minute boundary
    assert nxt == _dt("2026-04-25T12:05:00")


def test_cron_hourly_at_minute() -> None:
    from bowhead_core.scheduler_loop import parse_cron_to_next

    now = _dt("2026-04-25T12:30:00")
    nxt = parse_cron_to_next("15 * * * *", now)
    # 12:15 passed → next 13:15
    assert nxt == _dt("2026-04-25T13:15:00")


def test_cron_daily_at_hour() -> None:
    from bowhead_core.scheduler_loop import parse_cron_to_next

    now = _dt("2026-04-25T12:00:00")
    nxt = parse_cron_to_next("0 9 * * *", now)
    # Today's 9:00 passed → tomorrow 9:00
    assert nxt == _dt("2026-04-26T09:00:00")

    now2 = _dt("2026-04-25T08:00:00")
    nxt2 = parse_cron_to_next("0 9 * * *", now2)
    # Today's 9:00 has not arrived.
    assert nxt2 == _dt("2026-04-25T09:00:00")


def test_cron_weekly_at_hour_dow() -> None:
    from bowhead_core.scheduler_loop import parse_cron_to_next

    # 2026-04-25 Saturday
    now = _dt("2026-04-25T12:00:00")
    nxt = parse_cron_to_next("0 10 * * 1", now)  # Monday 10:00 (cron 1 = Monday)
    # Next Monday 2026-04-27 (Python weekday Mon=0)
    assert nxt.weekday() == 0  # Monday
    assert nxt.hour == 10


def test_cron_invalid_returns_none() -> None:
    from bowhead_core.scheduler_loop import parse_cron_to_next

    now = _dt("2026-04-25T12:00:00")
    assert parse_cron_to_next("malformed", now) is None
    assert parse_cron_to_next("0 25 * * *", now) is None  # Invalid hour
    assert parse_cron_to_next("0 0 31 2 *", now) is None  # Impossible date (February 31)


def test_cron_calendar_patterns() -> None:
    """标准 cron 全语法 (cronsim): 按月/按季/按年/月末 L/一天多时刻/指定月."""
    from bowhead_core.scheduler_loop import parse_cron_to_next

    now = _dt("2026-04-25T12:00:00")  # Saturday
    monthly = parse_cron_to_next("0 10 1 * *", now)  # Monthly on day 1 at 10:00
    assert (monthly.month, monthly.day, monthly.hour) == (5, 1, 10)
    quarterly = parse_cron_to_next("0 9 1 1,4,7,10 *", now)  # First day of the quarter
    assert (quarterly.month, quarterly.day) == (7, 1)
    yearly = parse_cron_to_next("0 9 1 1 *", now)  # January 1 annually
    assert (yearly.year, yearly.month, yearly.day) == (2027, 1, 1)
    month_end = parse_cron_to_next("0 23 L * *", now)  # Month end (April has 30 days)
    assert (month_end.month, month_end.day, month_end.hour) == (4, 30, 23)
    multi = parse_cron_to_next("0 9,12,18 * * *", now)  # Today's 12:00 passed → 18:00
    assert (multi.day, multi.hour) == (25, 18)
    june_only = parse_cron_to_next("0 9 * 6 *", now)  # June only
    assert (june_only.month, june_only.day) == (6, 1)


def test_is_scheduler_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.scheduler_loop import is_scheduler_enabled

    monkeypatch.delenv("BOWHEAD_SCHEDULER_ENABLED", raising=False)
    assert is_scheduler_enabled() is False
    monkeypatch.setenv("BOWHEAD_SCHEDULER_ENABLED", "1")
    assert is_scheduler_enabled() is True
    monkeypatch.setenv("BOWHEAD_SCHEDULER_ENABLED", "0")
    assert is_scheduler_enabled() is False


@pytest.mark.asyncio
async def test_scheduler_loop_fires_due_jobs(tmp_path: Path) -> None:
    """创建一个 next_run_at = 过去的 job，tick 一次应触发 run_cb."""
    from bowhead_core.scheduled_store import ScheduledJobStore
    from bowhead_core.scheduler_loop import SchedulerLoop

    store = ScheduledJobStore(str(tmp_path / "s.db"))
    await store.initialize()
    job = await store.create(
        user_id="u1",
        workspace_id="w1",
        name="test",
        cron="* * * * *",
        query="hi",
    )
    # Manually set next_run_at in the past.
    import aiosqlite

    past = (datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat()
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE scheduled_jobs SET next_run_at = ? WHERE id = ?",
            (past, job.id),
        )
        await db.commit()

    called = {"n": 0}

    async def _cb(j):
        called["n"] += 1
        return RunOutcome(turn_id="t_fake")

    loop = SchedulerLoop(store, _cb)
    await loop._tick()
    assert called["n"] == 1
    runs = await store.list_runs(job.id)
    assert len(runs) == 1
    assert runs[0].status == "success"
    assert runs[0].turn_id == "t_fake"
    # next_run_at advanced into the future.
    refreshed = await store.get(job.id)
    assert refreshed is not None
    assert refreshed.next_run_at is not None
    assert datetime.fromisoformat(refreshed.next_run_at) > datetime.now(tz=UTC)


@pytest.mark.asyncio
async def test_scheduler_loop_skips_job_with_in_flight_run(tmp_path: Path) -> None:
    """A due job that already has an in-flight 'running' run (e.g. from run-now) is skipped — no
    second concurrent turn / double charge."""
    import aiosqlite
    from bowhead_core.scheduled_store import ScheduledJobStore
    from bowhead_core.scheduler_loop import SchedulerLoop

    store = ScheduledJobStore(str(tmp_path / "s.db"))
    await store.initialize()
    job = await store.create(user_id="u1", workspace_id="w1", name="t", cron="* * * * *", query="hi")
    past = (datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat()
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute("UPDATE scheduled_jobs SET next_run_at = ? WHERE id = ?", (past, job.id))
        await db.commit()
    await store.start_run(job.id)  # simulate an in-flight run-now run

    called = {"n": 0}

    async def _cb(j):
        called["n"] += 1
        return RunOutcome(turn_id="t_fake")

    await SchedulerLoop(store, _cb)._tick()
    assert called["n"] == 0  # cron skipped the in-flight job
    runs = await store.list_runs(job.id)
    assert len(runs) == 1 and runs[0].status == "running"  # no second run started


@pytest.mark.asyncio
async def test_scheduler_loop_rechecks_admission_before_start_run(
    tmp_path: Path,
) -> None:
    import aiosqlite
    from bowhead_core.scheduled_store import ScheduledJobStore
    from bowhead_core.scheduler_loop import SchedulerLoop

    store = ScheduledJobStore(str(tmp_path / "s.db"))
    await store.initialize()
    job = await store.create(
        user_id="u1",
        workspace_id="w1",
        name="t",
        cron="* * * * *",
        query="hi",
    )
    past = (datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat()
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE scheduled_jobs SET next_run_at=? WHERE id=?",
            (past, job.id),
        )
        await db.commit()

    callback_calls = 0

    async def _run(_job):
        raise AssertionError("denied job must not run")

    async def _deny(_job):
        nonlocal callback_calls
        callback_calls += 1
        return False

    await SchedulerLoop(store, _run, admit_cb=_deny)._tick()

    assert callback_calls == 1
    assert await store.list_runs(job.id) == []


@pytest.mark.asyncio
async def test_scheduler_loop_notifies_failure_pause_recovery(tmp_path: Path) -> None:
    """_tick drives failed/paused/recovered notifications through the injected service."""
    import aiosqlite
    from bowhead_core.scheduled_store import ScheduledJobStore
    from bowhead_core.scheduler_loop import SchedulerLoop

    store = ScheduledJobStore(str(tmp_path / "s.db"))
    await store.initialize()

    class _Notify:
        def __init__(self) -> None:
            self.calls: list = []

        async def emit_scheduled_failed(self, job, err):
            self.calls.append(("failed", job.id))

        async def emit_scheduled_paused(self, job):
            self.calls.append(("paused", job.id))

        async def emit_scheduled_recovered(self, job, run_id):
            self.calls.append(("recovered", job.id))

    notify = _Notify()

    async def _set_due(jid: str, *, failure_count: int | None = None) -> None:
        past = (datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat()
        async with aiosqlite.connect(store._db_path) as db:
            await db.execute("UPDATE scheduled_jobs SET next_run_at=?, status='active' WHERE id=?", (past, jid))
            if failure_count is not None:
                await db.execute("UPDATE scheduled_jobs SET failure_count=? WHERE id=?", (failure_count, jid))
            await db.commit()

    async def _fail(j):
        return RunOutcome(error="scheduled_turn_failed")

    async def _ok(j):
        return RunOutcome(turn_id="t_ok")

    job = await store.create(user_id="u1", workspace_id="w1", name="t", cron="0 * * * *", query="q")

    # 1) a failure → failed notification
    await _set_due(job.id)
    await SchedulerLoop(store, _fail, notify=notify)._tick()
    assert ("failed", job.id) in notify.calls

    # 2) success after prior failure → recovered
    notify.calls.clear()
    await _set_due(job.id)
    await SchedulerLoop(store, _ok, notify=notify)._tick()
    assert ("recovered", job.id) in notify.calls

    # 3) third consecutive failure → auto-pause (failed + paused)
    notify.calls.clear()
    await _set_due(job.id, failure_count=2)
    await SchedulerLoop(store, _fail, notify=notify)._tick()
    assert ("failed", job.id) in notify.calls and ("paused", job.id) in notify.calls


@pytest.mark.asyncio
async def test_scheduler_auto_pauses_after_3_failures(tmp_path: Path) -> None:
    from bowhead_core.scheduled_store import ScheduledJobStore
    from bowhead_core.scheduler_loop import SchedulerLoop

    store = ScheduledJobStore(str(tmp_path / "s.db"))
    await store.initialize()
    job = await store.create(
        user_id="u1",
        workspace_id="w1",
        name="failing",
        cron="* * * * *",
        query="x",
    )

    async def _fail(j):
        return RunOutcome(error="boom")

    loop = SchedulerLoop(store, _fail)
    import aiosqlite

    for _i in range(3):
        # Manually set next_run_at in the past.
        past = (datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat()
        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE scheduled_jobs SET next_run_at = ?, status = 'active' WHERE id = ?",
                (past, job.id),
            )
            await db.commit()
        await loop._tick()

    final = await store.get(job.id)
    assert final is not None
    assert final.status == "paused"
    assert final.failure_count >= 3


def test_smtp_unconfigured_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    # email-config sprint: smtp_configured() removed; env detection centralized in email_config.env_smtp_config.
    from bowhead_core.email_config import env_smtp_config

    for k in ("BOWHEAD_SMTP_HOST", "BOWHEAD_SMTP_USER", "BOWHEAD_SMTP_PASS", "BOWHEAD_SMTP_FROM"):
        monkeypatch.delenv(k, raising=False)
    assert env_smtp_config() is None


@pytest.mark.asyncio
async def test_send_notification_unconfigured_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core.smtp_notifier import send_notification

    for k in ("BOWHEAD_SMTP_HOST", "BOWHEAD_SMTP_USER", "BOWHEAD_SMTP_PASS", "BOWHEAD_SMTP_FROM"):
        monkeypatch.delenv(k, raising=False)
    ok, err = await send_notification("alice@example.com", "sub", "body")
    assert ok is False
    assert "not configured" in err


def test_classify_run_status() -> None:
    """Shared skip-vs-fail rule (used by both the cron loop and the run-now endpoint)."""
    from bowhead_core.scheduler_loop import classify_run_status

    assert classify_run_status("t_1", None) == "success"
    assert classify_run_status(None, None) == "skipped"
    assert classify_run_status(None, "over_grace") == "skipped"
    assert classify_run_status(None, "user_credit_exhausted") == "skipped"
    assert classify_run_status(None, "ws_access_denied") == "failed"
    assert classify_run_status(None, "scheduled_turn_failed") == "failed"


def test_next_run_for_job_respects_tz() -> None:
    """cron is interpreted in the job's tz, result returned in UTC."""
    from bowhead_core.scheduler_loop import next_run_for_job

    now = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)  # = 08:00 Asia/Shanghai
    # "0 9 * * *" in Shanghai (UTC+8) → 09:00 CST today == 01:00 UTC
    nxt = next_run_for_job("0 9 * * *", "Asia/Shanghai", now)
    assert nxt is not None and nxt.hour == 1
    # same cron interpreted in UTC → 09:00 UTC
    assert next_run_for_job("0 9 * * *", "UTC", now).hour == 9


def test_next_run_for_job_bad_tz_falls_back_utc() -> None:
    from bowhead_core.scheduler_loop import next_run_for_job

    now = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    nxt = next_run_for_job("0 9 * * *", "Not/AZone", now)
    assert nxt is not None and nxt.hour == 9  # invalid tz → UTC fallback


@pytest.mark.parametrize("tz", ["America", "Etc", "posixrules", "../etc/passwd", "/etc/localtime"])
def test_next_run_for_job_directory_or_non_zone_tz_falls_back_utc(tz: str, caplog) -> None:
    """Directory names raised IsADirectoryError out of ZoneInfo and would stop the scheduler tick."""
    from bowhead_core.scheduler_loop import next_run_for_job

    now = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    with caplog.at_level("WARNING"):
        nxt = next_run_for_job("0 9 * * *", tz, now)
    assert nxt is not None and nxt.hour == 9
    assert "scheduler.bad_tz" in caplog.text


def test_parse_cron_weekday_range() -> None:
    """工作日 (dow range 1-5) — lands Mon–Fri at the given time; single weekday still works."""
    from bowhead_core.scheduler_loop import parse_cron_to_next

    base = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    nxt = parse_cron_to_next("30 8 * * 1-5", base)
    assert nxt is not None
    assert nxt > base and nxt.weekday() in {0, 1, 2, 3, 4} and nxt.hour == 8 and nxt.minute == 30
    nxt1 = parse_cron_to_next("0 10 * * 1", base)  # single weekday regression
    assert nxt1 is not None and nxt1.weekday() == 0 and nxt1.hour == 10


def test_parse_cron_manual_returns_none() -> None:
    """@manual routines have no auto-schedule (next_run_at stays NULL → never picked by _tick)."""
    from bowhead_core.scheduler_loop import MANUAL_CRON, parse_cron_to_next

    assert MANUAL_CRON == "@manual"
    assert parse_cron_to_next("@manual", datetime(2026, 6, 20, tzinfo=UTC)) is None


def test_cron_validation_step_over_hourly_rejected() -> None:
    """'*/N' with N>60 would silently collapse to hourly (cron steps don't carry across the hour) —
    reject it instead of misleading the user. '*/60' (exactly hourly) stays valid."""
    from bowhead_core.scheduler_loop import cron_validation_error

    assert cron_validation_error("0 9 * * *") is None  # daily, valid
    assert cron_validation_error("*/60 * * * *") is None  # exactly hourly, valid
    assert cron_validation_error("*/90 * * * *") is not None  # would run hourly → rejected
    assert cron_validation_error("*/120 * * * *") is not None
    assert cron_validation_error("*/30 * * * *") is not None  # sub-hourly → rejected
    assert cron_validation_error("@manual") is None


def test_cron_validation_calendar_and_frequency_probe() -> None:
    """全语法放开后, 频率策略靠触发点间隔探针而非 pattern 枚举."""
    from bowhead_core.scheduler_loop import cron_validation_error

    assert cron_validation_error("0 10 1 * *") is None  # Monthly
    assert cron_validation_error("0 9 1 1,4,7,10 *") is None  # Quarterly
    assert cron_validation_error("0 23 L * *") is None  # Month end
    assert cron_validation_error("0 9,12,18 * * *") is None  # Three times daily, intervals ≥60min
    assert cron_validation_error("0 9 1 1 *") is None  # Annually
    assert cron_validation_error("0,30 9 * * *") is not None  # 9:00+9:30: 30min interval → reject
    assert cron_validation_error("* * * * *") is not None  # Every minute → reject
    assert cron_validation_error("0 0 31 2 *") is not None  # Impossible date → reject
    assert cron_validation_error("bad") is not None  # Wrong field count


# ─── S3: pause / expire-and-replace / reconcile ───────────────────────────


async def _due_job(store, *, cron="0 * * * *"):
    import aiosqlite

    job = await store.create(user_id="u1", workspace_id="w1", name="t", cron=cron, query="q")
    past = (datetime.now(tz=UTC) - timedelta(minutes=5)).isoformat()
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute("UPDATE scheduled_jobs SET next_run_at=? WHERE id=?", (past, job.id))
        await db.commit()
    return job


async def _initialize_shared_scheduled_fixture(db_path):
    """Model the post-0017 shared core DB used by production.

    Scheduler terminal transitions and clarification invalidation are one
    transaction now, so a ScheduledJobStore-only fixture is no longer a valid
    deployment shape.
    """
    from bowhead_core.clarification_store import ClarificationStore
    from bowhead_core.scheduled_store import ScheduledJobStore

    store = ScheduledJobStore(str(db_path))
    await store.initialize()
    clarification_store = ClarificationStore(db_path)
    await clarification_store.initialize()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE clarification_invalidation_outbox ("
            "request_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, "
            "event_id TEXT NOT NULL UNIQUE, target_status TEXT NOT NULL, "
            "reason_code TEXT NOT NULL, actor_kind TEXT NOT NULL, "
            "state TEXT NOT NULL DEFAULT 'pending', lease_owner TEXT, "
            "lease_expires_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, delivered_at TEXT)"
        )
    return store, clarification_store


async def _put_pending_clarification(clarification_store, turn_id: str) -> None:
    from bowhead_core.clarification_store import ClarificationCandidate

    await clarification_store.put(
        turn_id=turn_id,
        user_id="u1",
        workspace_id="w1",
        token="",
        rationale="口径不明",
        candidates=[ClarificationCandidate(value="a", label="A")],
        kind="ambiguous_turn",
        resume_context={"user_text": "q"},
    )


@pytest.mark.asyncio
async def test_awaiting_outcome_parks_run_and_advances_next_run(tmp_path):
    """★Q2 的载重实现：挂起时必须**主动推进 next_run_at**。

    _tick 的 in-flight skip 分支不推进（注释写着 job stays due），照它实现的话挂起 30 秒后
    的下一个 tick 就算「下次 fire」——「活满一个周期」直接坍缩成半分钟。
    """
    from bowhead_core.scheduler_loop import SchedulerLoop

    store, clarification_store = await _initialize_shared_scheduled_fixture(tmp_path / "s.db")
    job = await _due_job(store)

    async def _cb(_j):
        await _put_pending_clarification(clarification_store, "t_await")
        return RunOutcome(turn_id="t_await", awaiting=True, session_id="s_1")

    await SchedulerLoop(store, _cb)._tick()

    runs = await store.list_runs(job.id)
    assert runs[0].status == "awaiting_input"
    assert runs[0].turn_id == "t_await"
    after = await store.get(job.id)
    assert after.next_run_at is not None
    assert after.next_run_at > datetime.now(tz=UTC).isoformat(), "挂起必须推进到下一个 cron 时刻"
    assert after.failure_count == 0, "挂起不是失败，不该计振"


@pytest.mark.asyncio
async def test_due_job_with_stale_awaiting_expires_and_starts_fresh(tmp_path):
    """到点仍没人答 → 记 input_timeout（计振）并起新一轮。周报不能因为上周没人回话就永远停摆。"""
    from bowhead_core.scheduler_loop import SchedulerLoop

    store, clarification_store = await _initialize_shared_scheduled_fixture(tmp_path / "s.db")
    job = await _due_job(store)
    stale = await store.start_run(job.id)
    await _put_pending_clarification(clarification_store, "t_old")
    await store.mark_run_awaiting(stale.id, "t_old")

    calls = {"n": 0}

    async def _cb(_j):
        calls["n"] += 1
        return RunOutcome(turn_id="t_new")

    await SchedulerLoop(store, _cb)._tick()

    runs = {r.id: r for r in await store.list_runs(job.id)}
    assert runs[stale.id].status == "failed"
    assert runs[stale.id].error == "scheduled_input_timeout"
    assert calls["n"] == 1, "过期后必须真的起新一轮"
    assert (await store.get(job.id)).failure_count == 1


@pytest.mark.asyncio
async def test_resume_in_flight_is_not_expired(tmp_path):
    """★竞态：用户刚答完、turn 正在续跑时到点了——不能把它判超时。

    否则 run 被标失败、新 run 并行起（双扣费），续跑完成后 reconciliation 找不到 awaiting
    行 → 交付静默丢失。
    """
    from bowhead_core.scheduler_loop import SchedulerLoop

    store, clarification_store = await _initialize_shared_scheduled_fixture(tmp_path / "s.db")
    job = await _due_job(store)
    run = await store.start_run(job.id)
    await _put_pending_clarification(clarification_store, "t_resuming")
    await store.mark_run_awaiting(run.id, "t_resuming")

    started = {"n": 0}

    async def _cb(_j):
        started["n"] += 1
        return RunOutcome(turn_id="t_new")

    async def _probe(turn_id: str) -> bool:
        return turn_id == "t_resuming"

    await SchedulerLoop(store, _cb, resume_probe=_probe)._tick()

    assert (await store.list_runs(job.id))[0].status == "awaiting_input", "续跑中的挂起不得被过期"
    assert started["n"] == 0, "不得在续跑期间并行起新 run"


@pytest.mark.asyncio
async def test_reconcile_runs_before_the_scan(tmp_path):
    """收编要在扫描之前：用户可能刚答完，先把已到终态的挂起收掉，过期分支才只面对真在等人的。"""
    from bowhead_core.scheduled_store import ScheduledJobStore
    from bowhead_core.scheduler_loop import SchedulerLoop

    store = ScheduledJobStore(str(tmp_path / "s.db"))
    await store.initialize()
    order: list[str] = []

    async def _cb(_j):
        order.append("run")
        return RunOutcome(turn_id="t")

    async def _reconcile():
        order.append("reconcile")

    await _due_job(store)
    await SchedulerLoop(store, _cb, reconcile_cb=_reconcile)._tick()

    assert order[0] == "reconcile"


@pytest.mark.asyncio
async def test_reconcile_failure_does_not_stop_the_tick(tmp_path):
    """收编挂了不能拖垮整个 tick——别的任务还等着到点跑。"""
    from bowhead_core.scheduled_store import ScheduledJobStore
    from bowhead_core.scheduler_loop import SchedulerLoop

    store = ScheduledJobStore(str(tmp_path / "s.db"))
    await store.initialize()
    job = await _due_job(store)

    async def _cb(_j):
        return RunOutcome(turn_id="t_ok")

    async def _boom():
        raise RuntimeError("reconcile down")

    await SchedulerLoop(store, _cb, reconcile_cb=_boom)._tick()

    assert (await store.list_runs(job.id))[0].status == "success"
