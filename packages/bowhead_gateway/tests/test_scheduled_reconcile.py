"""reconcile_awaiting_runs —— 挂起 run 的收尾（scheduled-recipe-runs S3 / tech-design §3.4）.

为什么需要它：用户在 chat 里回答后，续跑走的是**普通 chat 路**——它对定时任务一无所知，
跑完不会有人去终结那条 awaiting run。所以由调度侧反过来查 turn 现态并收尾。
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
from bowhead_gateway._scheduled_runner import (
    is_scheduled_resume_in_flight,
    reconcile_awaiting_runs,
)


class _Turn:
    def __init__(self, status: str, *, session_id: str = "s_1", final_answer: str | None = None) -> None:
        self.status = status  # ⚠ Real shape is str, not an enum with .value.
        self.session_id = session_id
        self.final_answer = final_answer


class _SessionStore:
    def __init__(self, turns: dict[str, _Turn]) -> None:
        self._turns = turns

    async def get_turn(self, turn_id: str):
        return self._turns.get(turn_id)


class _Notifier:
    def __init__(self) -> None:
        self.delivered: list[str] = []
        self.resolved: list[str] = []

    async def emit_routine_delivered(self, job, *, run_id, session_id, summary):
        self.delivered.append(summary)

    async def resolve_routine_needs_input(self, job, *, run_id, reason="answered"):
        self.resolved.append(reason)


def _app(store, turns: dict[str, _Turn], *, notifier=None, deleted_sessions: set[str] | None = None):
    session_store = _SessionStore(turns)
    orch = SimpleNamespace(session_store=session_store)
    state = SimpleNamespace(
        scheduled_store=store,
        orchestrator=orch,
        notification_service=notifier,
        user_store=None,
        clarification_store=store._test_clarification_store,
        turn_log_store=None,
        _deleted=deleted_sessions or set(),
    )
    return SimpleNamespace(state=state)


@pytest.fixture
async def store(tmp_path):
    db_path = tmp_path / "s.db"
    s = ScheduledJobStore(str(db_path))
    await s.initialize()
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
    s._test_clarification_store = clarification_store
    return s


async def _awaiting(
    store,
    turn_id: str = "t_1",
    *,
    notify_channels=None,
    answered: bool = False,
):
    job = await store.create(
        user_id="u1",
        workspace_id="ws1",
        name="周报",
        cron="0 9 * * *",
        query="q",
        notify_channels=notify_channels or {"deliver_on_success": True},
    )
    run = await store.start_run(job.id)
    request = await store._test_clarification_store.put(
        turn_id=turn_id,
        user_id="u1",
        workspace_id="ws1",
        token="",
        rationale="口径不明",
        candidates=[ClarificationCandidate(value="a", label="A")],
        kind="ambiguous_turn",
        resume_context={"user_text": "q"},
    )
    assert (await store.mark_run_awaiting_if_pending(run.id, turn_id)).won
    if answered:
        await store._test_clarification_store.resolve(request.request_id, "a")
    return job, run, request


async def test_completed_turn_finishes_run_and_delivers(store):
    """用户答完、turn 跑到 completed → run 记成功并送达（迟到的周报也要送）。"""
    job, run, _ = await _awaiting(store, answered=True)
    notifier = _Notifier()
    app = _app(store, {"t_1": _Turn("completed", final_answer="本周营业额 2.03 亿")}, notifier=notifier)

    await reconcile_awaiting_runs(app)

    rows = await store.list_runs(job.id)
    assert rows[0].status == "success"
    assert notifier.delivered == ["本周营业额 2.03 亿"]
    assert notifier.resolved == ["answered"], "待办徽章必须关掉——它在 ACTION_REQUIRED 集里，不 resolve 就永远红着"


async def test_failed_turn_finishes_run_as_failed(store):
    job, run, _ = await _awaiting(store, answered=True)
    notifier = _Notifier()
    app = _app(store, {"t_1": _Turn("failed")}, notifier=notifier)

    await reconcile_awaiting_runs(app)

    rows = await store.list_runs(job.id)
    assert rows[0].status == "failed"
    assert rows[0].error == "scheduled_turn_failed"
    assert notifier.delivered == [], "失败不得走送达路径"
    assert notifier.resolved == ["failed"]


async def test_still_pending_turn_is_left_alone(store):
    """还在等人 → 不动。到点过期由 scheduler_loop 负责，不是这里。"""
    job, run, _ = await _awaiting(store)
    app = _app(store, {"t_1": _Turn("clarification_pending")})

    await reconcile_awaiting_runs(app)

    assert (await store.list_runs(job.id))[0].status == "awaiting_input"


async def test_cas_loser_does_not_deliver(store):
    """★过期分支已经抢先终结了这条 run → reconciliation 必须什么都不做。

    无 CAS 时会出现「既记了超时又发了成功报告」这种对不上的账。
    """
    job, run, request = await _awaiting(store)
    transition = await store.finish_awaiting_with_clarification_invalidation(
        run.id,
        run_status="failed",
        run_error="scheduled_input_timeout",
        clarification_status="expired",
        clarification_reason="scheduled_input_timeout",
    )
    assert transition.won
    notifier = _Notifier()
    app = _app(store, {"t_1": _Turn("completed", final_answer="迟到的结果")}, notifier=notifier)

    await reconcile_awaiting_runs(app)

    rows = await store.list_runs(job.id)
    assert rows[0].status == "failed", "输家不得覆盖已终结的状态"
    assert (await store._test_clarification_store.get(request.request_id)).status == "expired"
    assert notifier.delivered == [], "输家不得送达"


async def test_empty_final_answer_still_gets_a_summary(store):
    """纯 canvas 产出（没有 prose）也要送达一句话，不能发个空邮件。"""
    job, run, _ = await _awaiting(store, answered=True)
    notifier = _Notifier()
    app = _app(store, {"t_1": _Turn("completed", final_answer="")}, notifier=notifier)

    await reconcile_awaiting_runs(app)

    # The job prompt "q" carries English and the owner has no saved preference.
    assert notifier.delivered == ["周报 completed"]


async def test_reconciled_summary_uses_the_turns_persisted_presentation_locale(store):
    job, run, _ = await _awaiting(store, answered=True)
    notifier = _Notifier()
    app = _app(store, {"t_1": _Turn("completed", final_answer="")}, notifier=notifier)
    lookups: list[tuple[str, str]] = []

    class _Users:
        async def get_user_team_id(self, _user_id: str) -> str:
            return "team_1"

    class _Terminal:
        async def read_persisted_turn_presentation_locale(self, *, team_id: str, turn_id: str) -> str:
            lookups.append((team_id, turn_id))
            return "zh-CN"

    app.state.user_store = _Users()
    app.state.terminal_protocol_store = _Terminal()

    await reconcile_awaiting_runs(app)

    assert lookups == [("team_1", "t_1")]
    assert notifier.delivered == ["周报 已完成"]


async def test_missing_turn_is_skipped_not_crashed(store):
    """turn 查不到（被清理/库不一致）→ 跳过，不能让整轮收编挂掉。"""
    job, _, _ = await _awaiting(store, "t_gone")
    app = _app(store, {})

    await reconcile_awaiting_runs(app)

    assert (await store.list_runs(job.id))[0].status == "awaiting_input"


async def test_cancelled_pending_turn_invalidates_the_card_atomically(store):
    job, _, request = await _awaiting(store, "t_cancelled")
    notifier = _Notifier()
    app = _app(store, {"t_cancelled": _Turn("cancelled")}, notifier=notifier)

    await reconcile_awaiting_runs(app)

    run = (await store.list_runs(job.id))[0]
    assert run.status == "failed"
    assert run.error == "scheduled_turn_failed"
    assert (await store._test_clarification_store.get(request.request_id)).status == "dismissed"
    with sqlite3.connect(store._db_path) as connection:
        assert connection.execute(
            "SELECT reason_code FROM clarification_invalidation_outbox WHERE request_id=?",
            (request.request_id,),
        ).fetchone() == ("scheduled_turn_terminated",)
    assert notifier.resolved == ["failed"]


async def test_resume_probe_reports_running_turn(store):
    """续跑判据：turn 回到 running = 有人答了正在跑，过期分支据此让路。"""
    app = _app(store, {"t_run": _Turn("running"), "t_wait": _Turn("clarification_pending")})

    assert await is_scheduled_resume_in_flight(app, "t_run") is True
    assert await is_scheduled_resume_in_flight(app, "t_wait") is False
    assert await is_scheduled_resume_in_flight(app, "t_missing") is False
