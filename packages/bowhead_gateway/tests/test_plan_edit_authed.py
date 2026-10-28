"""2026-07: PATCH /steps + POST /amend 从 dev-only 升生产 authed 后的鉴权钉死。
此前双门 _dev_enabled → 116 prod 编辑操作全 404(可编辑计划卡编辑功能生产死)。
现 _current_user + _planning_enabled + ownership 进 get_plan WHERE。仿 test_plan_skip_step。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from bowhead_core.plan_store import PlanStatus
from bowhead_gateway.main import create_app
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.settings import Settings


class _FakeStore:
    def __init__(self, status: str = PlanStatus.PROPOSED) -> None:
        self._status = status
        self.replaced = False
        self.bumped = False

    async def get_plan(self, plan_id: str, *, user_id: str, workspace_id: str) -> Any:
        if user_id != "u_dev_alice":  # ownership: cross-tenant lookup finds nothing.
            return None
        return SimpleNamespace(id=plan_id, status=self._status, title="T", current_rev=1)

    async def list_steps(self, plan_id: str) -> list[Any]:
        return [_step("ps_1")]

    async def replace_steps(self, plan_id: str, steps: list[Any], *, expected_rev: int) -> bool:
        self.replaced = True
        self.bumped = True
        return True


class _FakeRegistry:
    def get(self, name: str, *, workspace_id: str = "") -> Any:
        if name == "compute":
            return SimpleNamespace(description="compute agent")
        raise KeyError(name)

    def list_all(self, workspace_id: str) -> list[str]:
        return ["compute", "canvas-writer"]


def _step(sid: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=sid,
        seq=0,
        level=0,
        agent_name="compute",
        objective="o",
        deliverable="",
        depends_on=(),
        needs_corpus=False,
        needs_web=False,
        status="pending",
        included=True,
        origin="initial",
        error=None,
        error_kind=None,
        result_digest="",
    )


def _app(store: _FakeStore, user_id: str = "u_dev_alice") -> Any:
    app = create_app()
    app.state.settings = Settings()
    app.state.plan_store = store
    app.state.orchestrator = SimpleNamespace(multi_agent_registry=_FakeRegistry())
    # team_id: amend route uses it to get the team router (other cases never reach that step; extra field has no side effects).
    app.dependency_overrides[_current_user] = lambda: SimpleNamespace(id=user_id, team_id="t_1")
    return app


async def _patch(app: Any, plan_id: str = "plan_x") -> httpx2.Response:
    body = {"workspace_id": "1", "user_id": "ignored-by-server", "steps": [{"id": "s1", "agent": "compute", "objective": "分析销量"}]}
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        return await c.patch(f"/api/v1/plans/{plan_id}/steps", json=body)


@pytest.mark.asyncio
async def test_edit_authed_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    store = _FakeStore()
    resp = await _patch(_app(store))
    assert resp.status_code == 200
    assert store.replaced and store.bumped  # Actually persists under production authentication.


@pytest.mark.asyncio
async def test_edit_cross_tenant_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    resp = await _patch(_app(_FakeStore(), user_id="u_intruder"))
    assert resp.status_code == 404  # ownership enters get_plan WHERE.


@pytest.mark.asyncio
async def test_edit_flag_off_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")
    resp = await _patch(_app(_FakeStore()))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_edit_non_proposed_409(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    resp = await _patch(_app(_FakeStore(status=PlanStatus.RUNNING)))
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_amend_cross_tenant_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """amend 也升 authed:跨租户 404(不深入 LLM,gate 在 get_plan 前拦)。"""
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    app = _app(_FakeStore(), user_id="u_intruder")
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/api/v1/plans/plan_x/amend", json={"workspace_id": "1", "user_id": "x", "instruction": "加一步"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_amend_flag_off_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")
    app = _app(_FakeStore())
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/api/v1/plans/plan_x/amend", json={"workspace_id": "1", "user_id": "x", "instruction": "加一步"})
    assert r.status_code == 404


# ── ★116-dogfood P1: regenerate criteria after amend ────────────────────────
#
# amend reuses author_plan's schema (AuthoredStep lacks criteria), and to_plan_steps
# **mints new IDs for every step**→preview criteria always empty→FE PATCH rewrites entire list from preview→persisted
# success_criteria=[]. Batch 1's planned backfill by step ID cannot match new IDs, also clearing untouched
# steps; empty criteria = verifier skipped = batch 1 verification silently disabled for the entire plan.
# 116 plan_c4899ee19444 observed: all 3 steps had criteria before amend, all [] afterward.


async def _amend(app: Any, plan_id: str = "plan_x") -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        return await c.post(
            f"/api/v1/plans/{plan_id}/amend",
            json={"workspace_id": "1", "user_id": "x", "instruction": "给第二步加一条验收标准"},
        )


def _wire_amend_happy_path(monkeypatch: pytest.MonkeyPatch, *, criteria_calls: list[Any]) -> None:
    """把 amend 的两个外部依赖(团队 router / 出图 LLM)替掉, 只留路由自身的逻辑。"""
    from bowhead_core.plan_authoring import AuthoredStep
    from bowhead_gateway.routes import plans as _plans

    async def _fake_amend(_router: Any, **_kw: Any) -> list[Any]:
        return [
            AuthoredStep(llm_id="a", agent_name="compute", objective="按省统计周转天数"),
            AuthoredStep(llm_id="b", agent_name="compute", objective="按品类统计滞销占比"),
        ]

    async def _fake_router(*_a: Any, **_kw: Any) -> Any:
        return SimpleNamespace(name="team-router")

    async def _fake_criteria(_router: Any, *, steps: Any, trace_id: str = "", **_kw: Any) -> dict[str, tuple[str, ...]]:
        # Build mapping from **actual received step IDs**; if route omits reified steps or fails to map back by ID,
        # assertions fail (substitute no more permissive than real implementation).
        criteria_calls.append([s.id for s in steps])
        return {s.id: (f"含{s.objective[:4]}的具体数值",) for s in steps}

    monkeypatch.setattr(_plans, "amend_plan", _fake_amend)
    monkeypatch.setattr(_plans, "team_router_or_409", _fake_router)
    monkeypatch.setattr(_plans, "author_step_criteria", _fake_criteria)


@pytest.mark.asyncio
async def test_amend_preview_carries_regenerated_criteria(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_PLANNING_VERIFY", "1")
    calls: list[Any] = []
    _wire_amend_happy_path(monkeypatch, criteria_calls=calls)

    r = await _amend(_app(_FakeStore()))
    assert r.status_code == 200
    assert r.json()["expected_rev"] == 1
    steps = r.json()["steps"]
    assert len(steps) == 2
    # Core assertion: every preview step has nonempty criteria (old behavior always [] here).
    assert all(s["success_criteria"] for s in steps), steps
    # Second generation receives exactly the new reified IDs, with results mapped back to those same steps.
    assert calls == [[s["id"] for s in steps]]


@pytest.mark.asyncio
async def test_amend_preview_model_calls_use_the_viewer_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Amend runs outside a Turn; both model calls (steps, then criteria) date on the viewer's calendar."""
    from bowhead_core.model_clock import turn_time_zone_name
    from bowhead_core.plan_authoring import AuthoredStep
    from bowhead_gateway.routes import plans as _plans

    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_PLANNING_VERIFY", "1")
    _wire_amend_happy_path(monkeypatch, criteria_calls=[])
    seen: list[tuple[str, str]] = []

    async def _amend_sees_zone(_router: Any, **_kw: Any) -> list[Any]:
        seen.append(("steps", turn_time_zone_name()))
        return [AuthoredStep(llm_id="a", agent_name="compute", objective="按省统计周转天数")]

    async def _criteria_sees_zone(_router: Any, *, steps: Any, **_kw: Any) -> dict[str, tuple[str, ...]]:
        seen.append(("criteria", turn_time_zone_name()))
        return {}

    monkeypatch.setattr(_plans, "amend_plan", _amend_sees_zone)
    monkeypatch.setattr(_plans, "author_step_criteria", _criteria_sees_zone)
    transport = httpx2.ASGITransport(app=_app(_FakeStore()))
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/plans/plan_x/amend",
            json={"workspace_id": "1", "user_id": "x", "instruction": "加一步"},
            headers={"X-Bowhead-Time-Zone": "America/Los_Angeles"},
        )
    assert r.status_code == 200, r.text
    assert seen == [("steps", "America/Los_Angeles"), ("criteria", "America/Los_Angeles")]
    assert turn_time_zone_name() == ""


@pytest.mark.asyncio
async def test_amend_criteria_failure_degrades_but_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """第二遍生成炸了 → criteria 空(诚实降级), 但 amend 预览照出(不挡用户)。"""
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_PLANNING_VERIFY", "1")
    _wire_amend_happy_path(monkeypatch, criteria_calls=[])
    from bowhead_gateway.routes import plans as _plans

    async def _boom(*_a: Any, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("llm down")

    monkeypatch.setattr(_plans, "author_step_criteria", _boom)

    r = await _amend(_app(_FakeStore()))
    assert r.status_code == 200
    assert all(s["success_criteria"] == [] for s in r.json()["steps"])


# ── P2: amend rate limiting (CTO review fast-follow) ────────────────────────
def test_amend_rate_limit_per_user() -> None:
    """per-user 滑动窗口:MAX 次内放行,超限 False,别的 user 不受影响。"""
    from bowhead_gateway.routes.plans import _AMEND_RL, _AMEND_RL_MAX, _amend_rate_ok

    _AMEND_RL.clear()
    uid = "u_rl_test"
    for _ in range(_AMEND_RL_MAX):
        assert _amend_rate_ok(uid) is True
    assert _amend_rate_ok(uid) is False  # Attempt MAX+1 exceeds limit → 429.
    assert _amend_rate_ok("u_other") is True  # Per-user isolation.
    _AMEND_RL.clear()


# ── graph-mode batch 1 implementation review B2/F-1: PATCH must not clear user-approved acceptance criteria ──


class _CriteriaStore(_FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.captured: list[Any] = []

    async def list_steps(self, plan_id: str) -> list[Any]:
        if self.captured:
            return self.captured
        s = _step("ps_1")
        s.success_criteria = ("含 2025 各季度数字",)
        return [s]

    async def replace_steps(self, plan_id: str, steps: list[Any], *, expected_rev: int) -> bool:
        self.replaced = True
        self.captured = list(steps)
        return True


async def _patch_body(app: Any, steps: list[dict], plan_id: str = "plan_x", *, expected_rev: Any = None) -> httpx2.Response:
    body = {"workspace_id": "1", "user_id": "x", "steps": steps}
    if expected_rev is not None:
        body["expected_rev"] = expected_rev
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        return await c.patch(f"/api/v1/plans/{plan_id}/steps", json=body)


@pytest.mark.asyncio
async def test_browser_edit_preserves_displayed_revision_across_tabs(tmp_path, monkeypatch) -> None:
    from bowhead_core.plan_store import PlanStore, new_step

    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    store = PlanStore(str(tmp_path / "browser-edit.db"))
    await store.initialize()
    plan = await store.create_plan(session_id="s", workspace_id="1", user_id="u_dev_alice", title="shared preview")
    original = new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="original")
    await store.add_steps([original])
    app = _app(store)
    first_steps = [{"id": original.id, "agent": "compute", "objective": "first tab saved"}]
    first = await _patch_body(app, first_steps, plan.id, expected_rev=1)
    assert first.status_code == 200
    assert first.json()["current_rev"] == 2
    assert first.json()["steps"][0]["objective"] == "first tab saved"
    stale = await _patch_body(app, [{**first_steps[0], "objective": "old tab"}], plan.id, expected_rev=1)
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"]["code"] == "plan_not_approvable"
    assert [s.objective for s in await store.list_steps(plan.id)] == ["first tab saved"]
    second = await _patch_body(app, [{**first_steps[0], "objective": "second edit"}], plan.id, expected_rev=2)
    assert second.status_code == 200
    assert second.json()["current_rev"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [0, -1, True, 1.5, "1"])
async def test_invalid_browser_edit_revision_rejected(monkeypatch, revision) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    store = _FakeStore()
    response = await _patch_body(_app(store), [{"agent": "compute", "objective": "edit"}], expected_rev=revision)
    assert response.status_code == 422
    assert not store.replaced


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["approve", "edit"])
async def test_late_plan_edit_cannot_overwrite_execution_or_newer_edit(tmp_path, monkeypatch, winner: str) -> None:
    from bowhead_core.plan_store import PlanStore, new_step
    from bowhead_gateway.routes import plans as plans_routes

    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    store = PlanStore(str(tmp_path / "edit-race.db"))
    await store.initialize()
    plan = await store.create_plan(session_id="s", workspace_id="1", user_id="u_dev_alice", title="reviewed plan")
    original = new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="original")
    await store.add_steps([original])
    app = _app(store)
    original_lookup = plans_routes._plan_lookup

    async def lookup_with_concurrent_change(*args: Any, **kwargs: Any) -> Any:
        result = await original_lookup(*args, **kwargs)
        if winner == "approve":
            assert await store.try_claim_running(plan.id, "t_winner", expected_rev=plan.current_rev)
        else:
            assert await store.replace_steps(
                plan.id,
                [new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="winning edit")],
                expected_rev=plan.current_rev,
            )
        return result

    monkeypatch.setattr(plans_routes, "_plan_lookup", lookup_with_concurrent_change)
    response = await _patch_body(app, [{"id": original.id, "agent": "compute", "objective": "late edit"}], plan.id)
    assert response.status_code == 409
    saved = await store.get_plan(plan.id, user_id="u_dev_alice", workspace_id="1")
    assert saved.current_rev == plan.current_rev + (winner == "edit")
    assert saved.status == (PlanStatus.PROPOSED if winner == "edit" else PlanStatus.RUNNING)
    assert [step.objective for step in await store.list_steps(plan.id)] == ["winning edit" if winner == "edit" else "original"]


@pytest.mark.asyncio
async def test_edit_without_criteria_backfills_from_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    """客户端不传 success_criteria(老客户端/字段缺失) → 按 id 回填库里既有验收."""
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    store = _CriteriaStore()
    resp = await _patch_body(_app(store), [{"id": "ps_1", "agent": "compute", "objective": "改了目标"}])
    assert resp.status_code == 200
    assert store.captured[0].success_criteria == ("含 2025 各季度数字",)
    # Response _step_view also passes it through.
    assert resp.json()["steps"][0]["success_criteria"] == ["含 2025 各季度数字"]


@pytest.mark.asyncio
async def test_edit_explicit_criteria_sanitized_and_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    store = _CriteriaStore()
    resp = await _patch_body(
        _app(store),
        [{"id": "ps_1", "agent": "compute", "objective": "o", "success_criteria": ["x" * 100, "引用 ≥2 信源", "", "a4", "a5"]}],
    )
    assert resp.status_code == 200
    got = store.captured[0].success_criteria
    assert len(got) <= 3 and all(len(c) <= 60 for c in got)
    assert "引用 ≥2 信源" in got


@pytest.mark.asyncio
async def test_edit_explicit_empty_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    store = _CriteriaStore()
    resp = await _patch_body(_app(store), [{"id": "ps_1", "agent": "compute", "objective": "o", "success_criteria": []}])
    assert resp.status_code == 200
    assert store.captured[0].success_criteria == ()


@pytest.mark.asyncio
async def test_edit_preserves_or_explicitly_clears_needs_web(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    store = _CriteriaStore()
    existing = _step("ps_1")
    existing.success_criteria = ()
    existing.needs_web = True
    store.captured = [existing]

    preserved = await _patch_body(
        _app(store),
        [{"id": "ps_1", "agent": "compute", "objective": "仍需联网"}],
    )
    assert preserved.status_code == 200
    assert store.captured[0].needs_web is True

    cleared = await _patch_body(
        _app(store),
        [{"id": "ps_1", "agent": "compute", "objective": "改用已有数据", "needs_web": False}],
    )
    assert cleared.status_code == 200
    assert store.captured[0].needs_web is False


# ── _step_view verify serialization (implementation review F-8) ──


def test_step_view_serializes_criteria_and_verify() -> None:
    from bowhead_gateway.routes.plans import _step_view

    s = _step("ps_v")
    s.success_criteria = ("含数字",)
    s.verify_json = '{"verdict": "partial", "items": [{"text": "含数字", "status": "unmet", "note": "缺"}], "note": ""}'
    view = _step_view(s)
    assert view["success_criteria"] == ["含数字"]
    # 2026-08-15: add `items` (per-item status + evidence). This fixture **already contained**
    # `"note": "缺"`; evidence was always in data but never exposed through API, leaving FE to show a bare
    # ✕. Observed consequence on 116: all three criteria unmet with no explanation of which or why.
    assert view["verify"] == {
        "verdict": "partial",
        "unmet": ["含数字"],
        "note": "",
        "items": [{"text": "含数字", "status": "unmet", "note": "缺"}],
    }
    # Bad JSON → None (no crash).
    s.verify_json = "{broken"
    assert _step_view(s)["verify"] is None
    # Old object without new attributes → empty default.
    bare = _step("ps_old")
    assert _step_view(bare)["success_criteria"] == [] and _step_view(bare)["verify"] is None
