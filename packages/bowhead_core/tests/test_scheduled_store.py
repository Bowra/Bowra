"""ScheduledJobStore unit tests (#84)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "scheduled.db")


@pytest.mark.asyncio
async def test_create_and_get(db_path: str) -> None:
    from bowhead_core.scheduled_store import ScheduledJobStore

    store = ScheduledJobStore(db_path)
    await store.initialize()
    job = await store.create(
        user_id="u1",
        workspace_id="w1",
        name="daily",
        cron="0 9 * * *",
        query="今年 GMV",
        public_entry_id="pe_example",
    )
    assert job.id.startswith("sj_")
    assert job.status == "active"
    fetched = await store.get(job.id)
    assert fetched is not None
    assert fetched.name == "daily"
    assert fetched.public_entry_id == "pe_example"


@pytest.mark.asyncio
async def test_list_for_user_isolation(db_path: str) -> None:
    from bowhead_core.scheduled_store import ScheduledJobStore

    store = ScheduledJobStore(db_path)
    await store.initialize()
    await store.create(user_id="u1", workspace_id="w1", name="a", cron="* * * * *", query="q")
    await store.create(user_id="u2", workspace_id="w1", name="b", cron="* * * * *", query="q")
    u1 = await store.list_for_user("u1")
    u2 = await store.list_for_user("u2")
    assert len(u1) == 1 and u1[0].name == "a"
    assert len(u2) == 1 and u2[0].name == "b"


@pytest.mark.asyncio
async def test_active_cap_rejects_11th(db_path: str) -> None:
    from bowhead_core.scheduled_store import MAX_ACTIVE_JOBS_PER_USER, ScheduledJobStore

    store = ScheduledJobStore(db_path)
    await store.initialize()
    for i in range(MAX_ACTIVE_JOBS_PER_USER):
        await store.create(
            user_id="u1",
            workspace_id="w1",
            name=f"j{i}",
            cron="* * * * *",
            query="q",
        )
    with pytest.raises(ValueError, match="max"):
        await store.create(
            user_id="u1",
            workspace_id="w1",
            name="overflow",
            cron="* * * * *",
            query="q",
        )


@pytest.mark.asyncio
async def test_patch_and_delete(db_path: str) -> None:
    from bowhead_core.scheduled_store import ScheduledJobStore

    store = ScheduledJobStore(db_path)
    await store.initialize()
    job = await store.create(user_id="u1", workspace_id="w1", name="x", cron="* * * * *", query="q")
    assert await store.patch(job.id, status="paused") is True
    updated = await store.get(job.id)
    assert updated is not None and updated.status == "paused"

    with pytest.raises(ValueError):
        await store.patch(job.id, status="nonsense")

    assert await store.delete(job.id) is True
    assert await store.get(job.id) is None


@pytest.mark.asyncio
async def test_run_lifecycle(db_path: str) -> None:
    from bowhead_core.scheduled_store import ScheduledJobStore

    store = ScheduledJobStore(db_path)
    await store.initialize()
    job = await store.create(user_id="u1", workspace_id="w1", name="x", cron="* * * * *", query="q")
    run = await store.start_run(job.id)
    assert run.status == "running"
    await store.finish_run(run.id, status="success", turn_id="t_123")
    runs = await store.list_runs(job.id)
    assert len(runs) == 1
    assert runs[0].status == "success"
    assert runs[0].turn_id == "t_123"


async def test_has_active_run(db_path: str) -> None:
    from bowhead_core.scheduled_store import ScheduledJobStore

    store = ScheduledJobStore(db_path)
    await store.initialize()
    job = await store.create(user_id="u1", workspace_id="w1", name="x", cron="* * * * *", query="q")
    assert await store.has_active_run(job.id) is False
    run = await store.start_run(job.id)
    assert await store.has_active_run(job.id) is True
    await store.finish_run(run.id, status="success", turn_id="t1")
    assert await store.has_active_run(job.id) is False


async def test_recover_running_runs(db_path: str) -> None:
    from bowhead_core.scheduled_store import ScheduledJobStore

    store = ScheduledJobStore(db_path)
    await store.initialize()
    # The partial unique index allows at most one 'running' run per job, so a crash leaves one orphan
    # per job — simulate two jobs each mid-run pre-restart.
    job1 = await store.create(user_id="u1", workspace_id="w1", name="a", cron="0 9 * * *", query="q")
    job2 = await store.create(user_id="u1", workspace_id="w1", name="b", cron="0 9 * * *", query="q")
    await store.start_run(job1.id)
    await store.start_run(job2.id)
    assert await store.has_active_run(job1.id) is True
    assert await store.has_active_run(job2.id) is True
    n = await store.recover_running_runs()
    assert n == 2
    assert await store.has_active_run(job1.id) is False
    assert await store.has_active_run(job2.id) is False
    runs = await store.list_runs(job1.id) + await store.list_runs(job2.id)
    assert all(r.status == "failed" for r in runs)
    assert all("interrupted" in (r.error or "") for r in runs)


async def test_patch_full_fields_and_next_run(db_path: str) -> None:
    import pytest
    from bowhead_core.scheduled_store import ScheduledJobStore

    store = ScheduledJobStore(db_path)
    await store.initialize()
    job = await store.create(user_id="u1", workspace_id="w1", name="x", cron="0 9 * * *", query="q")
    await store.patch(
        job.id,
        query="新查询",
        tz="UTC",
        session_strategy="anchor",
        notify_channels={"email": ["a@x.com"], "deliver_on_success": True},
    )
    got = await store.get(job.id)
    assert got is not None
    assert got.query == "新查询" and got.tz == "UTC" and got.session_strategy == "anchor"
    assert got.notify_channels == {"email": ["a@x.com"], "deliver_on_success": True}
    with pytest.raises(ValueError):
        await store.patch(job.id, session_strategy="bogus")
    # set_next_run_at: set then clear (e.g. @manual)
    await store.set_next_run_at(job.id, "2026-07-01T09:00:00+00:00")
    assert (await store.get(job.id)).next_run_at == "2026-07-01T09:00:00+00:00"
    await store.set_next_run_at(job.id, None)
    assert (await store.get(job.id)).next_run_at is None


async def test_start_run_enforces_one_running_per_job(db_path: str) -> None:
    import pytest
    from bowhead_core.scheduled_store import ActiveRunExistsError, ScheduledJobStore

    store = ScheduledJobStore(db_path)
    await store.initialize()
    job = await store.create(user_id="u1", workspace_id="w1", name="x", cron="0 9 * * *", query="q")
    r1 = await store.start_run(job.id)
    # Second start_run is blocked by the partial unique index (the TOCTOU fix), not just the
    # app-level has_active_run check — so a cron tick racing run-now can't double-fire.
    with pytest.raises(ActiveRunExistsError):
        await store.start_run(job.id)
    # Once the first finishes (status != 'running'), the index slot frees and a fresh run is allowed.
    await store.finish_run(r1.id, status="completed", turn_id="t1", error=None)
    r2 = await store.start_run(job.id)
    assert r2.id != r1.id


async def test_patch_unpause_resets_failure_count(db_path: str) -> None:
    import aiosqlite
    from bowhead_core.scheduled_store import ScheduledJobStore

    store = ScheduledJobStore(db_path)
    await store.initialize()
    job = await store.create(user_id="u1", workspace_id="w1", name="x", cron="0 9 * * *", query="q")
    async with aiosqlite.connect(db_path) as db:  # simulate 3-strike auto-pause
        await db.execute(
            "UPDATE scheduled_jobs SET failure_count=3, status='paused' WHERE id=?",
            (job.id,),
        )
        await db.commit()
    await store.patch(job.id, status="active")
    got = await store.get(job.id)
    assert got is not None and got.status == "active" and got.failure_count == 0


async def test_pause_active_for_workspace_is_targeted_and_preserves_existing_pauses(
    db_path: str,
) -> None:
    import aiosqlite
    from bowhead_core.scheduled_store import ScheduledJobStore

    store = ScheduledJobStore(db_path)
    await store.initialize()
    target = await store.create(
        user_id="u1",
        workspace_id="w1",
        name="target",
        cron="0 9 * * *",
        query="q",
    )
    already_paused = await store.create(
        user_id="u1",
        workspace_id="w1",
        name="manual pause",
        cron="0 10 * * *",
        query="q",
    )
    other = await store.create(
        user_id="u1",
        workspace_id="w2",
        name="other",
        cron="0 11 * * *",
        query="q",
    )
    await store.patch(already_paused.id, status="paused")
    async with aiosqlite.connect(db_path) as db:
        # Pause only while the space remains archived: w1 archived, w2 still enabled.
        await db.execute("CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        await db.execute("INSERT INTO workspaces(id,status) VALUES ('w1','archived'),('w2','active')")
        await db.commit()
    assert await store.pause_active_for_workspace("w2", reason="workspace_archived") == 0

    paused = await store.pause_active_for_workspace(
        "w1",
        reason="workspace_archived",
    )

    assert paused == 1
    target_after = await store.get(target.id)
    manual_after = await store.get(already_paused.id)
    other_after = await store.get(other.id)
    assert target_after is not None
    assert target_after.status == "paused"
    assert target_after.pause_reason == "workspace_archived"
    assert manual_after is not None
    assert manual_after.status == "paused"
    assert manual_after.pause_reason is None
    assert other_after is not None
    assert other_after.status == "active"
    assert other_after.pause_reason is None

    await store.patch(target.id, status="active")
    reactivated = await store.get(target.id)
    assert reactivated is not None
    assert reactivated.status == "active"
    assert reactivated.pause_reason is None


async def test_initialize_migrates_pause_reason_and_workspace_status_index(
    db_path: str,
) -> None:
    import aiosqlite
    from bowhead_core.scheduled_store import ScheduledJobStore

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE scheduled_jobs("
            "id TEXT PRIMARY KEY,user_id TEXT NOT NULL,workspace_id TEXT NOT NULL,"
            "name TEXT NOT NULL,cron TEXT NOT NULL,tz TEXT NOT NULL DEFAULT 'UTC',"
            "query TEXT NOT NULL,session_strategy TEXT NOT NULL DEFAULT 'fresh',"
            "anchor_session_id TEXT,notify_channels TEXT NOT NULL DEFAULT '{}',"
            "status TEXT NOT NULL DEFAULT 'active',failure_count INTEGER NOT NULL DEFAULT 0,"
            "next_run_at TEXT,last_run_at TEXT,last_turn_id TEXT,created_at TEXT NOT NULL,"
            "updated_at TEXT NOT NULL)",
        )
        await db.commit()

    store = ScheduledJobStore(db_path)
    await store.initialize()

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("PRAGMA table_info(scheduled_jobs)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        async with db.execute("PRAGMA index_list(scheduled_jobs)") as cur:
            indexes = {row[1] for row in await cur.fetchall()}
    assert "pause_reason" in columns
    assert "idx_scheduled_jobs_workspace_status" in indexes


async def test_initialize_heals_preexisting_dup_running(db_path: str) -> None:
    """A DB from before the partial unique index (an unclean shutdown left ≥2 'running' for one job)
    must NOT IntegrityError-crash initialize() — the index is healed-then-created, not crash-on-create."""
    import aiosqlite
    import pytest
    from bowhead_core.scheduled_store import ActiveRunExistsError, ScheduledJobStore

    async with aiosqlite.connect(db_path) as db:  # pre-this-version runs table, no unique index, dup running
        await db.execute(
            "CREATE TABLE scheduled_job_runs (id TEXT PRIMARY KEY, job_id TEXT, turn_id TEXT, "
            "started_at TEXT, finished_at TEXT, status TEXT, error TEXT)",
        )
        await db.execute(
            "INSERT INTO scheduled_job_runs(id,job_id,started_at,status) VALUES ('r1','j1','t','running')",
        )
        await db.execute(
            "INSERT INTO scheduled_job_runs(id,job_id,started_at,status) VALUES ('r2','j1','t','running')",
        )
        await db.commit()
    store = ScheduledJobStore(db_path)
    await store.initialize()  # must not raise
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (
            await db.execute(
                "SELECT status FROM scheduled_job_runs WHERE job_id='j1'",
            )
        ).fetchall()
    assert [r["status"] for r in rows] == ["failed", "failed"]  # both pre-existing 'running' swept
    # and the unique index is now live (enforces one running per job going forward)
    job = await store.create(user_id="u1", workspace_id="w1", name="x", cron="0 9 * * *", query="q")
    await store.start_run(job.id)
    with pytest.raises(ActiveRunExistsError):
        await store.start_run(job.id)


# ─── S3: suspended state and CAS transitions ────────────────────


async def _seed(db_path: str):
    from bowhead_core.scheduled_store import ScheduledJobStore

    store = ScheduledJobStore(db_path)
    await store.initialize()
    return store


async def _job(store, *, user_id: str = "u1"):
    return await store.create(user_id=user_id, workspace_id="ws1", name="j", cron="0 9 * * *", query="q")


@pytest.mark.asyncio
async def test_mark_run_awaiting_is_cas_from_running(db_path: str) -> None:
    store = await _seed(db_path)
    job = await _job(store)
    run = await store.start_run(job.id)

    assert await store.mark_run_awaiting(run.id, "t_1") is True
    assert await store.mark_run_awaiting(run.id, "t_2") is False, "已经挂起的 run 不该被二次转移"

    rows = await store.list_runs(job.id)
    assert rows[0].status == "awaiting_input"
    assert rows[0].turn_id == "t_1", "必须写 turn_id，否则 reconciliation 找不回它在等哪个 turn"
    assert rows[0].finished_at is None, "挂起不是终态"


@pytest.mark.asyncio
async def test_finish_run_cas_loser_is_a_noop(db_path: str) -> None:
    """★三条语义相反的路径并发终结同一个 awaiting run，只能有一个赢家。

    无 CAS 时是 last-writer-wins → 可能既记了失败又发了送达，账对不上。
    """
    store, clarification_store = await _shared_store_with_clarification(db_path)
    job = await _job(store)
    run = await store.start_run(job.id)
    request = await _pending(clarification_store, "t_1")
    assert (await store.mark_run_awaiting_if_pending(run.id, "t_1")).won

    won = await store.finish_awaiting_with_clarification_invalidation(
        run.id,
        run_status="failed",
        run_error="scheduled_input_timeout",
        clarification_status="expired",
        clarification_reason="scheduled_input_timeout",
    )
    lost = await store.finish_awaiting_with_clarification_invalidation(
        run.id,
        run_status="skipped",
        run_error="scheduled_input_superseded",
        clarification_status="dismissed",
        clarification_reason="scheduled_input_superseded",
    )

    assert won.won is True
    assert lost.won is False
    rows = await store.list_runs(job.id)
    assert rows[0].status == "failed", "输家不得覆盖赢家的终态"
    assert rows[0].error == "scheduled_input_timeout"
    assert (await clarification_store.get(request.request_id)).status == "expired"

    with pytest.raises(ValueError, match="atomic settlement"):
        await store.finish_run(
            run.id,
            status="success",
            expected_status="awaiting_input",
        )


@pytest.mark.asyncio
async def test_awaiting_run_still_blocks_a_second_run(db_path: str) -> None:
    """挂起中的 run 仍占在飞位——同 job 再起一个 turn 会让两条结果撞车。"""
    from bowhead_core.scheduled_store import ActiveRunExistsError

    store = await _seed(db_path)
    job = await _job(store)
    run = await store.start_run(job.id)
    await store.mark_run_awaiting(run.id, "t_1")

    assert await store.has_active_run(job.id) is True
    with pytest.raises(ActiveRunExistsError):
        await store.start_run(job.id)


@pytest.mark.asyncio
async def test_awaiting_turn_ids_feeds_the_clarification_sweeper(db_path: str) -> None:
    """系统级 24h TTL 会把 pending clarification 清成 expired；挂起中的 turn 必须豁免，
    否则周更任务的挂起活不到下次 fire（深链进去无卡可答）。"""
    store = await _seed(db_path)
    j1, j2 = await _job(store), await _job(store)
    r1, r2 = await store.start_run(j1.id), await store.start_run(j2.id)
    await store.mark_run_awaiting(r1.id, "t_await")
    await store.finish_run(r2.id, status="success", turn_id="t_done")

    assert await store.awaiting_turn_ids() == {"t_await"}


@pytest.mark.asyncio
async def test_get_run_by_turn_identifies_scheduled_origin(db_path: str) -> None:
    """resume 端点靠它认出「这个 turn 是定时任务出身」——session origin 与 turn 前缀都判不准。"""
    store = await _seed(db_path)
    job = await _job(store)
    run = await store.start_run(job.id)
    await store.mark_run_awaiting(run.id, "t_sched_x")

    found = await store.get_run_by_turn("t_sched_x")
    assert found is not None and found.job_id == job.id
    assert await store.get_run_by_turn("t_unrelated") is None


@pytest.mark.asyncio
async def test_boot_recovery_leaves_awaiting_alone(db_path: str) -> None:
    """挂起要跨重启存活：它在等人，不是被中断的执行。"""
    store = await _seed(db_path)
    j1, j2 = await _job(store), await _job(store)
    r_await = await store.start_run(j1.id)
    await store.mark_run_awaiting(r_await.id, "t_await")
    await store.start_run(j2.id)  # A genuinely interrupted running run.

    recovered = await store.recover_running_runs()

    assert recovered == 1
    assert (await store.list_runs(j1.id))[0].status == "awaiting_input"
    assert (await store.list_runs(j2.id))[0].status == "failed"


async def _shared_store_with_clarification(db_path: str):
    from bowhead_core.clarification_store import ClarificationStore
    from bowhead_core.scheduled_store import ScheduledJobStore

    store = ScheduledJobStore(db_path)
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


async def _pending(clarification_store, turn_id: str):
    from bowhead_core.clarification_store import ClarificationCandidate

    return await clarification_store.put(
        turn_id=turn_id,
        user_id="u1",
        workspace_id="ws1",
        token="",
        rationale="口径不明",
        candidates=[ClarificationCandidate(value="a", label="A")],
        kind="ambiguous_turn",
        resume_context={"user_text": "q"},
    )


@pytest.mark.asyncio
async def test_finish_awaiting_invalidates_card_in_the_same_transaction(
    db_path: str,
) -> None:
    store, clarification_store = await _shared_store_with_clarification(db_path)
    job = await _job(store)
    run = await store.start_run(job.id)
    request = await _pending(clarification_store, "t_atomic_timeout")
    assert (await store.mark_run_awaiting_if_pending(run.id, request.turn_id)).won

    transition = await store.finish_awaiting_with_clarification_invalidation(
        run.id,
        run_status="failed",
        run_error="scheduled_input_timeout",
        clarification_status="expired",
        clarification_reason="scheduled_input_timeout",
    )

    assert transition.won is True
    assert [item.request_id for item in transition.invalidations] == [
        request.request_id,
    ]
    assert (await store.list_runs(job.id))[0].status == "failed"
    assert (await clarification_store.get(request.request_id)).status == "expired"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT target_status,reason_code,state FROM clarification_invalidation_outbox WHERE request_id=?",
            (request.request_id,),
        ).fetchone() == ("expired", "scheduled_input_timeout", "pending")


@pytest.mark.asyncio
async def test_finish_awaiting_rolls_back_run_when_outbox_insert_fails(
    db_path: str,
) -> None:
    store, clarification_store = await _shared_store_with_clarification(db_path)
    job = await _job(store)
    run = await store.start_run(job.id)
    request = await _pending(clarification_store, "t_atomic_rollback")
    assert (await store.mark_run_awaiting_if_pending(run.id, request.turn_id)).won
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_scheduled_invalidation BEFORE INSERT ON "
            "clarification_invalidation_outbox BEGIN "
            "SELECT RAISE(ABORT, 'invalidation rejected'); END"
        )

    with pytest.raises(sqlite3.IntegrityError):
        await store.finish_awaiting_with_clarification_invalidation(
            run.id,
            run_status="skipped",
            run_error="scheduled_input_superseded",
            clarification_status="dismissed",
            clarification_reason="scheduled_input_superseded",
        )

    assert (await store.list_runs(job.id))[0].status == "awaiting_input"
    assert (await clarification_store.get(request.request_id)).status == "pending"


@pytest.mark.asyncio
async def test_finish_awaiting_preserves_an_older_opposite_terminal_row(
    db_path: str,
) -> None:
    """An upgrade-era dismissed sibling must not make timeout permanently stick."""
    store, clarification_store = await _shared_store_with_clarification(db_path)
    job = await _job(store)
    run = await store.start_run(job.id)
    old_terminal = await _pending(clarification_store, "t_mixed_terminal")
    still_pending = await _pending(clarification_store, "t_mixed_terminal")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE clarification_requests SET status='dismissed' WHERE request_id=?",
            (old_terminal.request_id,),
        )
    assert (
        await store.mark_run_awaiting_if_pending(
            run.id,
            still_pending.turn_id,
        )
    ).won

    transition = await store.finish_awaiting_with_clarification_invalidation(
        run.id,
        run_status="failed",
        run_error="scheduled_input_timeout",
        clarification_status="expired",
        clarification_reason="scheduled_input_timeout",
    )

    assert transition.won is True
    assert (await clarification_store.get(old_terminal.request_id)).status == "dismissed"
    assert (await clarification_store.get(still_pending.request_id)).status == "expired"
    assert (await store.list_runs(job.id))[0].status == "failed"


@pytest.mark.asyncio
async def test_delete_awaiting_job_invalidates_before_removing_ownership(
    db_path: str,
) -> None:
    store, clarification_store = await _shared_store_with_clarification(db_path)
    job = await _job(store)
    run = await store.start_run(job.id)
    request = await _pending(clarification_store, "t_delete_awaiting")
    assert (await store.mark_run_awaiting_if_pending(run.id, request.turn_id)).won

    deleted = await store.delete_with_clarification_invalidation(job.id)

    assert deleted.deleted is True
    assert (await clarification_store.get(request.request_id)).status == "dismissed"
    assert await store.get(job.id) is None
    assert await store.list_runs(job.id) == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT reason_code FROM clarification_invalidation_outbox WHERE request_id=?",
            (request.request_id,),
        ).fetchone() == ("scheduled_job_deleted",)


@pytest.mark.asyncio
async def test_source_aware_start_preserves_paused_run_now_but_fences_scheduler(
    db_path: str,
) -> None:
    from bowhead_core.scheduled_store import ScheduledRunSourceError

    store = await _seed(db_path)
    job = await _job(store)
    assert await store.patch(job.id, status="paused")

    with pytest.raises(ScheduledRunSourceError) as scheduler_error:
        await store.start_run(job.id, source="scheduler")
    assert scheduler_error.value.cause == "not_active"

    run = await store.start_run(job.id, source="run_now")
    assert run.status == "running"
    await store.finish_run(run.id, status="success")
    assert await store.delete(job.id)
    with pytest.raises(ScheduledRunSourceError) as deleted_error:
        await store.start_run(job.id, source="run_now")
    assert deleted_error.value.cause == "not_found"


@pytest.mark.asyncio
async def test_run_now_cannot_bypass_an_agent_authorization_pause(db_path: str) -> None:
    from bowhead_core.scheduled_store import ScheduledRunSourceError

    store = await _seed(db_path)
    job = await _job(store)
    assert await store.patch(
        job.id,
        status="paused",
        pause_reason="agent_unhired:asa_1",
    )

    with pytest.raises(ScheduledRunSourceError) as denied:
        await store.start_run(job.id, source="run_now")

    assert denied.value.cause == "agent_authorization_paused"
    assert await store.list_runs(job.id) == []


@pytest.mark.asyncio
async def test_stale_restore_and_run_snapshots_lose_to_authorization_pause(db_path: str) -> None:
    from bowhead_core.scheduled_store import ScheduledRunSourceError

    store = await _seed(db_path)
    job = await _job(store)
    assert await store.patch(
        job.id,
        status="paused",
        pause_reason="agent_unhired:asa_1",
    )

    # Both requests were authorized against the older active row. Neither may
    # overwrite/execute the newer pause written by the unhire transaction.
    assert (
        await store.patch(
            job.id,
            status="active",
            expected_updated_at=job.updated_at,
        )
        is False
    )
    with pytest.raises(ScheduledRunSourceError) as changed:
        await store.start_run(
            job.id,
            source="run_now",
            expected_job_updated_at=job.updated_at,
        )
    assert changed.value.cause == "job_changed"
    current = await store.get(job.id)
    assert current is not None
    assert current.status == "paused"
    assert current.pause_reason == "agent_unhired:asa_1"


@pytest.mark.asyncio
async def test_restore_assignment_fence_cannot_race_a_personal_disable(db_path: str) -> None:
    store = await _seed(db_path)
    job = await _job(store)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE agent_assignment(
              team_id TEXT NOT NULL,user_id TEXT NOT NULL,agent_id TEXT NOT NULL,
              personal_state TEXT NOT NULL,row_version INTEGER NOT NULL
            )
            """,
        )
        connection.execute(
            "INSERT INTO agent_assignment VALUES ('team_1','u1','osa_1','active',1)",
        )
        connection.commit()
    assert await store.patch(job.id, status="paused", pause_reason="agent_personal_disabled:asa_1")
    paused = await store.get(job.id)
    assert paused is not None
    fence = ("team_1", "u1", "osa_1", 1)
    assert await store.patch(
        job.id,
        status="active",
        expected_updated_at=paused.updated_at,
        expected_agent_assignment=fence,
    )

    assert await store.patch(job.id, status="paused", pause_reason="agent_personal_disabled:asa_1")
    paused_again = await store.get(job.id)
    assert paused_again is not None
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE agent_assignment SET personal_state='disabled',row_version=2",
        )
        connection.commit()
    assert not await store.patch(
        job.id,
        status="active",
        expected_updated_at=paused_again.updated_at,
        expected_agent_assignment=fence,
    )
    current = await store.get(job.id)
    assert current is not None
    assert current.status == "paused"
