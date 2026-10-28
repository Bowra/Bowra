"""B 线批 1c · POST /api/v1/memory/candidates/{cid}/redraft（「改一下」）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from bowhead_core.memory_auto_capture import _CaptureRateLimiter
from bowhead_core.metrics_log import MetricsLog
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings
from bowhead_memory.candidates import ORIGIN_FEEDBACK_DRAFT, PauseStateCache

from bowhead_core import UserMemoryStore
from bowhead_memory import MemoryCandidateStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def app_ctx(tmp_path: Path, monkeypatch):
    db = tmp_path / "t.db"
    mem = UserMemoryStore(db)
    await mem.initialize()
    candidates = MemoryCandidateStore(db_path=str(db))
    await candidates.initialize()
    settings_store = UserSettingsStore(db)
    await settings_store.init()
    metrics = MetricsLog(db)
    await metrics.initialize()
    user_store = UserStore(str(db))
    await user_store.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.user_memory_store = mem
    app.state.memory_candidate_store = candidates
    app.state.user_settings_store = settings_store
    app.state.metrics_log = metrics
    app.state.user_store = user_store
    app.state.pause_state_cache = PauseStateCache(settings_store=settings_store, ttl_seconds=60.0)
    app.state.orchestrator = None
    app.state.datarc = None

    # Drafter: this case concerns **endpoint orchestration** (what yields, what is created, what writes no suppression),
    # not model writing quality, so replace both router resolution and drafting with deterministic substitutes.
    monkeypatch.setattr(
        "bowhead_gateway.routes.memory_candidates.resolve_team_router",
        _fake_resolve,
    )
    monkeypatch.setattr(
        "bowhead_gateway.routes.memory_candidates.build_router_drafter",
        lambda _router, trace_id="": _echo_drafter,
    )
    # ⚠ Cost guard is a **module singleton**: unreplaced, quota consumed in one case leaks into the next,
    # making cases execution-order dependent (10s cooldown gives the second case 429).
    # Dedicated cases test rate limiting itself.
    monkeypatch.setattr(
        "bowhead_gateway.routes.memory_candidates._REDRAFT_LIMITER",
        _CaptureRateLimiter(min_interval_seconds=0.0, daily_cap=10**6),
    )
    return app


async def _fake_resolve(_state: Any, _team: Any, **_kw: Any):
    return object(), None


async def _echo_drafter(prompt: str) -> str:
    # Real drafter makes focused revisions; here only **prove user feedback reaches the drafting input**.
    return f"改后：{prompt.splitlines()[-1]}"


def _client(app):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw")


async def _seed(app, *, origin: str = ORIGIN_FEEDBACK_DRAFT, key_suffix: str = ""):
    uid = app.state.settings.dev_default_user
    cs: MemoryCandidateStore = app.state.memory_candidate_store
    return uid, await cs.enqueue(
        user_id=uid,
        kind="preference",
        key=f"ranking_chart_type{key_suffix}",
        value=f"排名图用横向条形图{key_suffix}",
        reason=None,
        source_turn_id="t1",
        origin=origin,
    )


@pytest.mark.integration
async def test_redraft_lets_the_old_one_step_aside_without_writing_a_suppression(app_ctx) -> None:
    """★★ 「改一下」**不是驳回**：旧的让位、新的接着问，审计里记的是「重起草」。

    ★ 决定性的断言是 `decision_kind == "feedback_redrafted"`。
    用 `user_rejected` 的话，用户说「我要换个说法」会被记成「我不要这条」——
    审计里读起来完全相反，而下一个给起草档加抑制来源的人会照着这个 kind 划池。

    ⚠ **等价突变（实测记录）**：把 `decided_by` 换成真实 uid（＝真驳回语义）
    ⇒ 抑制表**仍然是空的**，那条断言不红。原因是抑制的写入早就被 origin 白名单
    挡在存储层（只有 dreaming 那一池写抑制）。所以「不写抑制」是**存储层已有的保护**，
    不是本端点提供的 —— 它留作回归守卫，但**区分不了**这两种实现。
    我初版把它当成决定性断言写在注释里，是错的。
    """
    uid, cand = await _seed(app_ctx)
    async with _client(app_ctx) as c:
        r = await c.post(f"/api/v1/memory/candidates/{cand.id}/redraft", json={"comment": "不用九大区，我只看东北"})
    assert r.status_code == 200, r.text
    fresh = r.json()
    assert fresh["id"] != cand.id
    assert "东北" in str(fresh.get("display_value") or fresh.get("value") or ""), fresh

    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    old = await cs.get(cand.id)
    assert old is not None and old.status == "rejected", "旧的要让位"
    assert old.decision_kind == "feedback_redrafted", "不能记成 user_rejected——审计里读起来是相反的意思"

    import aiosqlite

    async with aiosqlite.connect(cs.db_path) as db:
        rows = await (await db.execute("SELECT COUNT(*) FROM memory_candidate_suppressions")).fetchone()
    # Regression guard (not decisive; see equivalent mutation explanation in docstring).
    assert rows[0] == 0, "「改一下」一条抑制都不许写——否则用户的诉求被自己压掉"


@pytest.mark.integration
async def test_only_a_feedback_draft_can_be_redrafted(app_ctx) -> None:
    """★ dreaming 蒸馏出来的候选不是「对用户某句话的一版改写」，没有「再写一遍」的语义。

    400 而不是 404：候选存在、也是你的，只是这个动作对它没有意义。
    """
    _uid, cand = await _seed(app_ctx, origin="dreaming_light")
    async with _client(app_ctx) as c:
        r = await c.post(f"/api/v1/memory/candidates/{cand.id}/redraft", json={"comment": "换个说法"})
    assert r.status_code == 400, r.text


@pytest.mark.integration
async def test_an_empty_comment_is_refused(app_ctx) -> None:
    """★ 「改一下」的全部信息量就在这句意见里。空着重跑一遍只会拿到几乎一样的东西，
    白烧一次 LLM 调用还让用户以为自己已经说过了。"""
    _uid, cand = await _seed(app_ctx)
    async with _client(app_ctx) as c:
        r = await c.post(f"/api/v1/memory/candidates/{cand.id}/redraft", json={"comment": "   "})
    assert r.status_code == 400, r.text


@pytest.mark.integration
async def test_a_second_redraft_within_the_cooldown_is_refused(app_ctx, monkeypatch) -> None:
    """★★ 「改一下」是**用户点一下就发一次 LLM 调用**，比直写路更容易触发
    （那条至少还有关键词粗筛挡着，这条只有一个按钮）。
    直写路早就有冷却+日上限，这条一开始一条都没有 —— 2026-09-09 review 逮到。

    ★ 429 而不是静默：用户按了按钮，必须知道为什么没反应。

    ★ 突变验证：把 `_REDRAFT_LIMITER.take(uid)` 那段删掉 ⇒ 本用例红。
    """
    monkeypatch.setattr(
        "bowhead_gateway.routes.memory_candidates._REDRAFT_LIMITER",
        _CaptureRateLimiter(min_interval_seconds=60.0, daily_cap=40),
    )
    _uid, first = await _seed(app_ctx)
    _uid2, second = await _seed(app_ctx, key_suffix="_b")
    async with _client(app_ctx) as c:
        r1 = await c.post(f"/api/v1/memory/candidates/{first.id}/redraft", json={"comment": "只看东北"})
        r2 = await c.post(f"/api/v1/memory/candidates/{second.id}/redraft", json={"comment": "再改一次"})
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 429, r2.text


@pytest.mark.integration
async def test_a_rejected_request_does_not_burn_quota(app_ctx, monkeypatch) -> None:
    """★ 参数不合法的请求**不该消耗额度**：那几次根本没花钱，
    而连点几次空意见就把自己的额度耗光是说不通的。

    ★ 突变验证：把护栏挪到 comment 校验**之前** ⇒ 本用例红。
    """
    monkeypatch.setattr(
        "bowhead_gateway.routes.memory_candidates._REDRAFT_LIMITER",
        _CaptureRateLimiter(min_interval_seconds=60.0, daily_cap=40),
    )
    _uid, cand = await _seed(app_ctx)
    async with _client(app_ctx) as c:
        bad = await c.post(f"/api/v1/memory/candidates/{cand.id}/redraft", json={"comment": "  "})
        ok = await c.post(f"/api/v1/memory/candidates/{cand.id}/redraft", json={"comment": "只看东北"})
    assert bad.status_code == 400
    assert ok.status_code == 200, "空意见那次不该占掉额度"


@pytest.mark.integration
async def test_the_dto_carries_origin_so_the_ui_can_hide_a_button_that_would_400(app_ctx) -> None:
    """★★ 前端要靠 `origin` 决定「改一下」按不按得动。

    116 dogfood（2026-09-09）：生产 92 条候选**全是** `dreaming_light`，
    而后端对非起草档返 400 ⇒ 不给前端这个字段的话，**100% 的卡都带一个
    点了必报错的按钮**。「后端能拒」不等于「界面不该画」。

    ★ 突变验证：把 `candidate_to_dto` 里的 `origin=` 去掉（回落默认）
    ⇒ 本用例红（起草档也会被报成 dreaming_light，按钮反而消失）。
    """
    _uid, draft = await _seed(app_ctx)
    _uid2, dream = await _seed(app_ctx, origin="dreaming_light", key_suffix="_d")
    async with _client(app_ctx) as c:
        r = await c.get("/api/v1/memory/candidates", params={"status": "pending", "limit": 50})
    by_id = {i["id"]: i for i in r.json()["items"]}
    assert by_id[draft.id]["origin"] == "feedback_draft"
    assert by_id[dream.id]["origin"] == "dreaming_light"
