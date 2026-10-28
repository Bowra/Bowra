"""Scheduler stable-code classification keeps license denials skipped.

A denial returns a finite code and MUST NOT bump failure_count / trip the 3-strike auto-pause,
so the job stays resumable after license renewal or an ENFORCE→0 rollback. A real error still counts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from bowhead_core.scheduled_store import ScheduledJobStore
from bowhead_core.scheduler_loop import RunOutcome, SchedulerLoop


@pytest.fixture
async def store(tmp_path):
    s = ScheduledJobStore(str(tmp_path / "s.db"))
    await s.initialize()
    return s


async def _due_job(store):
    job = await store.create(user_id="u1", workspace_id="ws1", name="j", cron="* * * * *", query="q")
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE scheduled_jobs SET next_run_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), job.id),
        )
        await db.commit()
    return job


def _cb(result):
    async def run_cb(_job):
        return result

    return run_cb


async def test_entitlement_denial_is_skipped_not_failed(store):
    job = await _due_job(store)
    await SchedulerLoop(store, _cb(RunOutcome(error="over_grace")))._tick()
    after = await store.get(job.id)
    assert after.failure_count == 0  # NOT bumped — the LIC-1 fix
    runs = await store.list_runs(job.id)
    assert runs[0].status == "skipped"
    assert runs[0].error == "over_grace"


async def test_real_error_is_failed_and_bumps(store):
    job = await _due_job(store)
    await SchedulerLoop(
        store,
        _cb(RunOutcome(error="scheduled_turn_failed")),
    )._tick()
    after = await store.get(job.id)
    assert after.failure_count == 1  # a genuine error still counts toward the 3-strike pause
    runs = await store.list_runs(job.id)
    assert runs[0].status == "failed"


async def test_success_resets(store):
    job = await _due_job(store)
    await SchedulerLoop(store, _cb(RunOutcome(turn_id="t_sched_abc")))._tick()
    after = await store.get(job.id)
    assert after.failure_count == 0
    runs = await store.list_runs(job.id)
    assert runs[0].status == "success"
