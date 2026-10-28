"""run-now 撞挂起 → 作废旧挂起换新（S3 · PRD FR-5.5 · 116 dogfood P1-B）.

背景（116 prod 2026-08-05 实测）：`run_now` 只有 `has_active_run → 409` 一条路，而 S3 把
该谓词加宽到含 `awaiting_input` ⇒ 任务一挂起，owner 手动重跑恒 409。叠加「回答也 409」
（P1-A）后，`@manual` 任务挂起即永久砖化：答不了、跑不了，`next_run_at` 恒 NULL 又永不 due，
tick 的过期分支也够不着。稳定码 `scheduled_input_superseded` 当时齐整地躺在错误词表、
i18n、FE 码表和两个「词表相等」单测里，**没有任何生产代码写入它**——这批测试守的正是写入点。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from bowhead_core.clarification_store import (
    ClarificationCandidate,
    ClarificationStore,
)
from bowhead_core.scheduled_store import ScheduledJobStore
from bowhead_gateway.routes.scheduled import (
    _advance_next_run_for_awaiting,
    _supersede_awaiting_runs,
)


class _Turn:
    def __init__(self, status: str) -> None:
        self.status = status  # ⚠ Real shape is str (Turn.status), not enum with .value.


class _SessionStore:
    def __init__(self, turns: dict[str, _Turn]) -> None:
        self._turns = turns

    async def get_turn(self, turn_id: str):
        return self._turns.get(turn_id)


def _request(store, turns: dict[str, _Turn], clarify=None):
    orch = SimpleNamespace(session_store=_SessionStore(turns))
    state = SimpleNamespace(
        scheduled_store=store,
        orchestrator=orch,
        clarification_store=clarify or store._test_clarification_store,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.fixture
async def store(tmp_path):
    db_path = tmp_path / "s.db"
    s = ScheduledJobStore(str(db_path))
    await s.initialize()
    clarification_store = ClarificationStore(db_path)
    await clarification_store.initialize()
    # Production gets this table from Alembic 0017.  This unit fixture keeps
    # both stores on the same migrated SQLite file so atomicity is exercised
    # instead of being hidden behind a permissive fake.
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
    s._test_clarification_store = clarification_store
    return s


async def _job_with_awaiting(store, *, cron: str = "0 9 * * *", turn_id: str = "t_sched_1"):
    job = await store.create(
        user_id="u1",
        workspace_id="ws1",
        name="周报",
        cron=cron,
        query="算一下本月的复购率",
        notify_channels={},
    )
    run = await store.start_run(job.id)
    await store._test_clarification_store.put(
        turn_id=turn_id,
        user_id="u1",
        workspace_id="ws1",
        token="",
        rationale="口径不明",
        candidates=[ClarificationCandidate(value="a", label="A")],
        kind="ambiguous_turn",
        resume_context={"user_text": "算一下本月的复购率"},
    )
    await store.mark_run_awaiting(run.id, turn_id)
    return job, run


async def test_supersede_frees_the_job_and_writes_the_stable_code(store):
    """挂起 run 让位：skipped + `scheduled_input_superseded`，且 job 不再被判在飞。"""
    job, run = await _job_with_awaiting(store)
    assert await store.has_active_run(job.id) is True  # run-now stopped here before fix.

    await _supersede_awaiting_runs(_request(store, {"t_sched_1": _Turn("clarification_pending")}), store, job)

    rows = await store.list_runs(job.id)
    assert rows[0].status == "skipped"
    assert rows[0].error == "scheduled_input_superseded"
    assert await store.has_active_run(job.id) is False


async def test_supersede_does_not_count_a_strike(store):
    """主动换新不是失败：failure_count 不动（三振只留给真失败）。"""
    job, _ = await _job_with_awaiting(store)

    await _supersede_awaiting_runs(_request(store, {"t_sched_1": _Turn("clarification_pending")}), store, job)

    refreshed = await store.get(job.id)
    assert refreshed.failure_count == 0
    assert refreshed.status == "active"


async def test_supersede_expires_the_stale_clarification_card(store):
    """旧卡同批作废：否则深链还能点，续跑扣了费却没有 run 认领结果。"""
    job, _ = await _job_with_awaiting(store)
    clarify = store._test_clarification_store
    await clarify.put(
        turn_id="t_sched_1",
        user_id="u1",
        workspace_id="ws1",
        token="",
        rationale="另一条口径不明",
        candidates=[ClarificationCandidate(value="b", label="B")],
        kind="ambiguous_turn",
        resume_context={"user_text": "算一下本月的复购率"},
    )
    pending = await clarify.get_pending_for_turn("t_sched_1")

    await _supersede_awaiting_runs(
        _request(store, {"t_sched_1": _Turn("clarification_pending")}, clarify=clarify),
        store,
        job,
    )

    assert await clarify.get_pending_for_turn("t_sched_1") == []
    assert {str((await clarify.get(row.request_id)).status) for row in pending} == {"dismissed"}


async def test_resume_in_flight_is_not_stolen(store):
    """有人刚回答、turn 已在 running → 不抢。旧 run 仍在飞，run-now 照常 409。

    抢掉等于把用户那次回答连同已扣的费一起丢掉，且续跑完成后 reconciliation 找不到
    awaiting 行 → 交付静默蒸发。
    """
    job, _ = await _job_with_awaiting(store)

    await _supersede_awaiting_runs(_request(store, {"t_sched_1": _Turn("running")}), store, job)

    rows = await store.list_runs(job.id)
    assert rows[0].status == "awaiting_input"
    assert await store.has_active_run(job.id) is True


async def test_finished_turn_awaiting_reconcile_is_not_stolen(store):
    """★ 用户已答完、turn 已 completed、只差 reconciliation 收尾 → **不许抢**。

    自评 review 逮到的回归：守卫原本只认 `running`，于是「答完到下一 tick 收尾」这段窗口里
    点一下「立即运行」，就会把这条 run CAS 成 skipped/superseded；reconciliation 下一 tick
    再扫已经没有 awaiting 行 —— 那份用户真答出来、真跑完、真扣了费的结果**永远不会送达**，
    账面上还记成 skipped，像什么都没发生过（tech-design §3.3「第三态」）。
    """
    job, _ = await _job_with_awaiting(store)

    await _supersede_awaiting_runs(_request(store, {"t_sched_1": _Turn("completed")}), store, job)

    rows = await store.list_runs(job.id)
    assert rows[0].status == "awaiting_input", "已跑完待收尾的 run 被抢走 = 交付静默蒸发"
    assert await store.has_active_run(job.id) is True  # run-now therefore 409; self-resolves after one tick.


async def test_failed_turn_awaiting_reconcile_is_not_stolen(store):
    """同理：失败的也留给 reconciliation 记账（它要计三振，抢成 skipped 就把失败洗白了）。"""
    job, _ = await _job_with_awaiting(store)

    await _supersede_awaiting_runs(_request(store, {"t_sched_1": _Turn("failed")}), store, job)

    assert (await store.list_runs(job.id))[0].status == "awaiting_input"


async def test_orphan_run_without_turn_row_is_superseded(store):
    """turn 行都没了的孤儿 run **要**抢：reconciliation 对 turn=None 是 `continue`，
    没人会来收它，不抢就永久占着在飞位。"""
    job, _ = await _job_with_awaiting(store)

    await _supersede_awaiting_runs(_request(store, {}), store, job)  # Turn not found.

    assert (await store.list_runs(job.id))[0].status == "skipped"


async def test_turn_lookup_failure_does_not_steal(store):
    """读 turn 抛错 = 不确定 → 不抢。宁可让 owner 多等一个 tick，也不能吃掉一次交付。"""

    class _BoomSessionStore:
        async def get_turn(self, _turn_id: str):
            raise RuntimeError("session store down")

    job, _ = await _job_with_awaiting(store)
    req = _request(store, {})
    req.app.state.orchestrator = SimpleNamespace(session_store=_BoomSessionStore())

    await _supersede_awaiting_runs(req, store, job)

    assert (await store.list_runs(job.id))[0].status == "awaiting_input"


async def test_other_jobs_awaiting_runs_are_untouched(store):
    """只动本 job 的挂起——别的任务在等自己的答复。"""
    job_a, _ = await _job_with_awaiting(store, turn_id="t_sched_a")
    job_b, run_b = await _job_with_awaiting(store, turn_id="t_sched_b")

    await _supersede_awaiting_runs(
        _request(store, {"t_sched_a": _Turn("clarification_pending"), "t_sched_b": _Turn("clarification_pending")}),
        store,
        job_a,
    )

    assert (await store.list_runs(job_b.id))[0].status == "awaiting_input"
    assert await store.has_active_run(job_b.id) is True


async def test_no_awaiting_run_is_a_noop(store):
    """没有挂起时零副作用——run-now 的常规路径不受影响。"""
    job = await store.create(user_id="u1", workspace_id="ws1", name="日报", cron="0 9 * * *", query="q")
    run = await store.start_run(job.id)

    await _supersede_awaiting_runs(_request(store, {}), store, job)

    rows = await store.list_runs(job.id)
    assert len(rows) == 1
    assert rows[0].id == run.id
    assert rows[0].status == "running"  # Never supersede running as suspended.


async def test_run_now_awaiting_advances_next_run_at(store):
    """run-now 出身的挂起也推进 next_run_at。

    否则 cron 任务的 next_run_at 若已到期，这次挂起会在下一个 tick 就被过期分支秒杀记
    `scheduled_input_timeout`——Q2「挂起活满一个周期」对 run-now 出身不成立。
    """
    job, _ = await _job_with_awaiting(store)
    await store.set_next_run_at(job.id, "2000-01-01T00:00:00+00:00")  # Long overdue.

    await _advance_next_run_for_awaiting(store, job)

    refreshed = await store.get(job.id)
    assert refreshed.next_run_at is not None
    assert refreshed.next_run_at > "2020"  # Advanced to the next future cron time.


async def test_manual_job_keeps_null_next_run_at(store):
    """`@manual` 没有下一次触发时刻——保持 NULL，别写进一个假的未来时间。"""
    job, _ = await _job_with_awaiting(store, cron="@manual")

    await _advance_next_run_for_awaiting(store, job)

    assert (await store.get(job.id)).next_run_at is None


# ── Through the gate itself: real POST /run-now response (116 dogfood reproduced exactly this) ──


def _client(store, monkeypatch, turns=None, clarify=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    from bowhead_gateway.routes import scheduled as scheduled_routes

    app = FastAPI()
    app.state.scheduled_store = store
    app.state.orchestrator = SimpleNamespace(session_store=_SessionStore(turns or {}))
    app.state.clarification_store = clarify or store._test_clarification_store
    app.state.settings = SimpleNamespace(auth_mode="dev", dev_default_user="u1")
    app.include_router(scheduled_routes.router, prefix="/api/v1/scheduled")
    # Actual turn execution is outside scope: verify gate admission + old suspended run yielding.
    monkeypatch.setattr(
        scheduled_routes,
        "_run_now_and_finish",
        lambda *a, **k: _noop(),
    )
    return TestClient(app)


async def _noop() -> None:
    return None


async def test_run_now_no_longer_409s_on_an_awaiting_run(store, monkeypatch):
    """P1-B 的用户动作：任务挂起时点「立即运行」。修复前恒 409（owner 被锁在自己的 routine 外）。"""
    job, _ = await _job_with_awaiting(store)
    client = _client(store, monkeypatch, turns={"t_sched_1": _Turn("clarification_pending")})

    resp = client.post(f"/api/v1/scheduled/jobs/{job.id}/run-now")

    assert resp.status_code == 200, resp.text
    runs = await store.list_runs(job.id)
    assert [r.status for r in runs][-1:] == ["skipped"]  # Oldest = superseded suspended run.
    assert runs[-1].error == "scheduled_input_superseded"
    assert runs[0].status == "running"  # New run started.


async def test_run_now_still_409s_while_a_resume_is_running(store, monkeypatch):
    """有人正在回答 → 仍 409。此时 409 的含义是「确实有一轮在跑」，不是「你被锁住了」。"""
    job, _ = await _job_with_awaiting(store)
    client = _client(store, monkeypatch, turns={"t_sched_1": _Turn("running")})

    resp = client.post(f"/api/v1/scheduled/jobs/{job.id}/run-now")

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "scheduled_run_conflict"
    assert (await store.list_runs(job.id))[0].status == "awaiting_input"


async def test_run_now_fails_closed_and_pauses_when_bound_agent_access_is_lost(store, monkeypatch):
    from bowhead_gateway.routes import scheduled as scheduled_routes

    job = await store.create(
        user_id="u1",
        workspace_id="ws1",
        name="日报",
        cron="0 9 * * *",
        query="q",
    )
    client = _client(store, monkeypatch)

    async def _lost(*_args, **_kwargs):
        return "scheduled_recipe_lost"

    monkeypatch.setattr(scheduled_routes, "_stored_recipe_error", _lost)

    response = client.post(f"/api/v1/scheduled/jobs/{job.id}/run-now")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "scheduled_recipe_lost"
    current = await store.get(job.id)
    assert current is not None
    assert current.status == "paused"
    assert current.pause_reason == "scheduled_recipe_lost"
    assert await store.list_runs(job.id) == []


# ── P2 batch (116 dogfood): badge lifecycle / deletion cleanup / list binding echo ──


class _Notifier:
    def __init__(self) -> None:
        self.resolved: list[tuple[str, str]] = []
        self.subject_resolved: list[str] = []

    async def resolve_routine_needs_input(self, job, *, run_id: str, reason: str = "answered"):
        self.resolved.append((run_id, reason))
        return

    async def resolve_scheduled_job_notifications(self, job, *, reason: str = "job_deleted") -> int:
        self.subject_resolved.append(job.id)
        return 2


async def test_supersede_closes_the_needs_input_badge(store):
    """换新之后那条「需要你补充信息」必须关掉——它在 ACTION_REQUIRED 集里，
    只看 state='open'，不关就是铃铛永久红，而它指向的卡已经作废。"""
    job, run = await _job_with_awaiting(store)
    req = _request(store, {"t_sched_1": _Turn("clarification_pending")})
    notifier = _Notifier()
    req.app.state.notification_service = notifier

    await _supersede_awaiting_runs(req, store, job)

    assert notifier.resolved == [(run.id, "superseded")]


async def test_resume_in_flight_does_not_close_the_badge(store):
    """没抢就别关：那条挂起还活着，用户正在答。"""
    job, _ = await _job_with_awaiting(store)
    req = _request(store, {"t_sched_1": _Turn("running")})
    notifier = _Notifier()
    req.app.state.notification_service = notifier

    await _supersede_awaiting_runs(req, store, job)

    assert notifier.resolved == []


async def test_delete_job_resolves_its_open_notifications(store, monkeypatch):
    """删任务 → 名下 open 通知一起关（P2-4）。

    不关的话铃铛留着「定时任务需要你补充信息」，点进去的深链指向一个已无 job/run 归属的
    会话；`scheduled.paused` 那条还同时挂在全体管理员待办里。
    """
    job, _ = await _job_with_awaiting(store)
    client = _client(store, monkeypatch, turns={"t_sched_1": _Turn("clarification_pending")})
    notifier = _Notifier()
    client.app.state.notification_service = notifier

    resp = client.request("DELETE", f"/api/v1/scheduled/jobs/{job.id}")

    assert resp.status_code == 204
    assert notifier.subject_resolved == [job.id]
    assert await store.get(job.id) is None


async def test_delete_survives_notification_cleanup_failure(store, monkeypatch):
    """通知没关干净不该把已经成功的删除翻成 5xx。"""

    class _Boom:
        async def resolve_scheduled_job_notifications(self, job, *, reason: str = "job_deleted") -> int:
            raise RuntimeError("notification store down")

    job = await store.create(user_id="u1", workspace_id="ws1", name="日报", cron="0 9 * * *", query="q")
    client = _client(store, monkeypatch)
    client.app.state.notification_service = _Boom()

    resp = client.request("DELETE", f"/api/v1/scheduled/jobs/{job.id}")

    assert resp.status_code == 204
    assert await store.get(job.id) is None


async def test_delete_running_job_returns_stable_busy_code(store, monkeypatch):
    job = await store.create(
        user_id="u1",
        workspace_id="ws1",
        name="日报",
        cron="0 9 * * *",
        query="q",
    )
    await store.start_run(job.id)
    client = _client(store, monkeypatch)

    resp = client.request("DELETE", f"/api/v1/scheduled/jobs/{job.id}")

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "scheduled_job_run_active"
    assert await store.get(job.id) is not None


async def test_delete_answered_awaiting_job_does_not_eat_resume(
    store,
    monkeypatch,
):
    job, _ = await _job_with_awaiting(store)
    pending = await store._test_clarification_store.get_pending_for_turn(
        "t_sched_1",
    )
    await store._test_clarification_store.resolve(
        pending[0].request_id,
        "a",
    )
    client = _client(store, monkeypatch, turns={"t_sched_1": _Turn("running")})

    resp = client.request("DELETE", f"/api/v1/scheduled/jobs/{job.id}")

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "scheduled_job_run_active"
    assert (await store.list_runs(job.id))[0].status == "awaiting_input"
