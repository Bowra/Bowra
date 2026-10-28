"""批 3 revise_step 路由 (batch3-replan-design §2-S3).

Pins:
- 双 flag 门(404) / BLOCKED-only(409) / step 收单集(409)
- preview: LLM 出修订步不落库 + expected_rev + warning; LLM 坏输出 → 422
- confirm: 回传产物逐字持久(不调 LLM)、agent 校验 422、rev OCC 409、双击 409、
  下游复位(真 store 全链)
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from bowhead_core.plan_store import PlanStore, StepStatus, new_step
from bowhead_gateway.main import create_app
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.settings import Settings

_AGENTS = ("compute", "kb-analyst", "financial-analyst", "consultant")


class _Registry:
    def get(self, name: str, *, workspace_id: str) -> Any:
        if name not in _AGENTS:
            raise KeyError(name)
        return SimpleNamespace(description=f"{name} desc")

    def list_all(self, workspace_id: str) -> list[str]:
        return list(_AGENTS)


def _app(store: Any) -> Any:
    app = create_app()
    app.state.settings = Settings()
    app.state.plan_store = store
    app.state.orchestrator = SimpleNamespace(multi_agent_registry=_Registry())
    app.dependency_overrides[_current_user] = lambda: SimpleNamespace(id="u_dev_alice", team_id="")
    return app


async def _post(app: Any, plan_id: str, step_id: str, body: dict) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.post(f"/api/v1/plans/{plan_id}/revise_step/{step_id}", json={"workspace_id": "1", **body})


async def _real_store(tmp_path) -> tuple[PlanStore, str, list]:
    """真 store: blocked plan + failed 步 + blocked(upstream_failed) 下游."""
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan = await store.create_plan(session_id="s", workspace_id="1", user_id="u_dev_alice", title="t")
    a = new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="上游", level=0)
    b = new_step(plan_id=plan.id, seq=1, agent_name="financial-analyst", objective="下游", level=1, depends_on=(a.id,))
    await store.add_steps([a, b])
    await store.update_step_status(a.id, status="failed", error="boom", error_kind="TimeoutError")
    await store.update_step_status(b.id, status="blocked", error="所有上游步未成功", error_kind="upstream_failed")
    await store.update_plan_status(plan.id, "running")
    await store.update_plan_status(plan.id, "blocked")
    return store, plan.id, [a, b]


_REVISED = {
    "agent": "kb-analyst",
    "objective": "改从本地 KB 找 2025 零售数据",
    "deliverable": "数据表",
    "needs_corpus": True,
    "needs_web": False,
    "success_criteria": ["含 2025 各季度数值"],
}


@pytest.mark.asyncio
async def test_replan_flag_off_404(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_REPLAN", "0")
    store, plan_id, steps = await _real_store(tmp_path)
    resp = await _post(_app(store), plan_id, steps[0].id, {"confirm": False})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_plan_not_blocked_409(monkeypatch, tmp_path) -> None:
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan = await store.create_plan(session_id="s", workspace_id="1", user_id="u_dev_alice", title="t")
    s = new_step(plan_id=plan.id, seq=0, agent_name="compute")
    await store.add_steps([s])
    resp = await _post(_app(store), plan.id, s.id, {"confirm": False})  # PROPOSED
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_done_step_409(monkeypatch, tmp_path) -> None:
    store, plan_id, steps = await _real_store(tmp_path)
    await store.update_step_status(steps[1].id, status="done")
    resp = await _post(_app(store), plan_id, steps[1].id, {"confirm": False})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_preview_happy_no_persist(monkeypatch, tmp_path) -> None:
    store, plan_id, steps = await _real_store(tmp_path)

    class _Router:
        async def invoke(self, agent_type, req):
            return SimpleNamespace(content=json.dumps({**_REVISED, "criteria": _REVISED["success_criteria"]}), usage=None)

    async def _fake_router(state, team_id, *, entrypoint):
        return _Router()

    import bowhead_gateway.routes.plans as plans_mod

    monkeypatch.setattr(plans_mod, "team_router_or_409", _fake_router)
    resp = await _post(_app(store), plan_id, steps[0].id, {"confirm": False, "instruction": "换个思路"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["preview"] is True
    assert data["revised"]["agent_name"] == "kb-analyst"
    assert data["revised"]["success_criteria"] == ["含 2025 各季度数值"]
    assert data["expected_rev"] == 1
    # No persistence.
    by_id = {s.id: s for s in await store.list_steps(plan_id)}
    assert len(by_id) == 2
    assert by_id[steps[0].id].status == StepStatus.FAILED


@pytest.mark.asyncio
async def test_preview_model_call_uses_the_viewer_zone(monkeypatch, tmp_path) -> None:
    """The preview runs outside a Turn; its model call still dates periods on the viewer's calendar."""
    from bowhead_core.model_clock import turn_time_zone_name

    store, plan_id, steps = await _real_store(tmp_path)
    seen: list[str] = []

    class _Router:
        async def invoke(self, agent_type, req):
            seen.append(turn_time_zone_name())
            return SimpleNamespace(content=json.dumps({**_REVISED, "criteria": _REVISED["success_criteria"]}), usage=None)

    async def _fake_router(state, team_id, *, entrypoint):
        return _Router()

    import bowhead_gateway.routes.plans as plans_mod

    monkeypatch.setattr(plans_mod, "team_router_or_409", _fake_router)
    transport = httpx2.ASGITransport(app=_app(store))
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            f"/api/v1/plans/{plan_id}/revise_step/{steps[0].id}",
            json={"workspace_id": "1", "confirm": False},
            headers={"X-Bowhead-Time-Zone": "America/Los_Angeles"},
        )
    assert resp.status_code == 200
    assert seen and set(seen) == {"America/Los_Angeles"}
    assert turn_time_zone_name() == ""  # the binding ends with the call


@pytest.mark.asyncio
async def test_preview_llm_garbage_422(monkeypatch, tmp_path) -> None:
    store, plan_id, steps = await _real_store(tmp_path)

    class _Router:
        async def invoke(self, agent_type, req):
            return SimpleNamespace(content="not json", usage=None)

    async def _fake_router(state, team_id, *, entrypoint):
        return _Router()

    import bowhead_gateway.routes.plans as plans_mod

    monkeypatch.setattr(plans_mod, "team_router_or_409", _fake_router)
    resp = await _post(_app(store), plan_id, steps[0].id, {"confirm": False})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_confirm_persists_verbatim_and_resets_downstream(tmp_path) -> None:
    store, plan_id, steps = await _real_store(tmp_path)
    resp = await _post(_app(store), plan_id, steps[0].id, {"confirm": True, "expected_rev": 1, "revised": _REVISED})
    assert resp.status_code == 200
    data = resp.json()
    new_id = data["revised_step_id"]
    assert data["current_rev"] == 2
    by_id = {s.id: s for s in await store.list_steps(plan_id)}
    assert by_id[steps[0].id].status == StepStatus.SUPERSEDED
    assert by_id[steps[0].id].superseded_by == new_id
    # Persist verbatim (R2-F1): approved object == persisted object.
    assert by_id[new_id].agent_name == "kb-analyst"
    assert by_id[new_id].objective == _REVISED["objective"]
    assert by_id[new_id].success_criteria == ("含 2025 各季度数值",)
    assert by_id[new_id].needs_corpus is True
    assert by_id[new_id].depends_on == ()  # Inherit old step's empty dependencies.
    assert by_id[new_id].level == 0
    assert by_id[new_id].origin == "revision"
    # Reset downstream.
    assert data["downstream_reset"] == [steps[1].id]
    assert by_id[steps[1].id].status == StepStatus.PENDING
    # Second double-click request: old row already superseded → 409, no orphan row.
    resp2 = await _post(_app(store), plan_id, steps[0].id, {"confirm": True, "expected_rev": 2, "revised": _REVISED})
    assert resp2.status_code == 409
    assert len(await store.list_steps(plan_id)) == 3


@pytest.mark.asyncio
async def test_confirm_wrong_rev_409_unknown_agent_422(tmp_path) -> None:
    store, plan_id, steps = await _real_store(tmp_path)
    resp = await _post(_app(store), plan_id, steps[0].id, {"confirm": True, "expected_rev": 99, "revised": _REVISED})
    assert resp.status_code == 409
    bad = {**_REVISED, "agent": "no-such-agent", "needs_corpus": False}
    resp2 = await _post(_app(store), plan_id, steps[0].id, {"confirm": True, "expected_rev": 1, "revised": bad})
    assert resp2.status_code == 422
    assert len(await store.list_steps(plan_id)) == 2  # Zero changes.


@pytest.mark.asyncio
async def test_confirm_missing_payload_422(tmp_path) -> None:
    store, plan_id, steps = await _real_store(tmp_path)
    resp = await _post(_app(store), plan_id, steps[0].id, {"confirm": True})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_second_generation_reset_traces_chain_root(tmp_path) -> None:
    """R1-F7: 二代修订的下游闭包沿链回溯原始 id——reset 不落空."""
    store, plan_id, steps = await _real_store(tmp_path)
    # First-generation revision.
    r1 = await _post(_app(store), plan_id, steps[0].id, {"confirm": True, "expected_rev": 1, "revised": _REVISED})
    gen1_id = r1.json()["revised_step_id"]
    # First revision also fails; downstream blocked again (upstream_failed).
    await store.update_step_status(gen1_id, status="failed", error="again", error_kind="TimeoutError")
    await store.update_step_status(steps[1].id, status="blocked", error="所有上游步未成功", error_kind="upstream_failed")
    await store.update_plan_status(plan_id, "running")
    await store.update_plan_status(plan_id, "blocked")
    # Second revision: downstream depends_on points to original ID; closure must trace the chain to find it.
    plan = await store.get_plan(plan_id, user_id="u_dev_alice", workspace_id="1")
    r2 = await _post(
        _app(store),
        plan_id,
        gen1_id,
        {"confirm": True, "expected_rev": plan.current_rev, "revised": {**_REVISED, "agent": "consultant", "needs_corpus": False}},
    )
    assert r2.status_code == 200
    assert r2.json()["downstream_reset"] == [steps[1].id]
    by_id = {s.id: s for s in await store.list_steps(plan_id)}
    assert by_id[steps[1].id].status == StepStatus.PENDING


@pytest.mark.asyncio
async def test_preview_warning_on_replan_count(monkeypatch, tmp_path) -> None:
    store, plan_id, steps = await _real_store(tmp_path)
    # Directly raise replan_count to the threshold.
    import aiosqlite

    async with aiosqlite.connect(store._db_path) as db:  # noqa: SLF001
        await db.execute("UPDATE plans SET replan_count = 2 WHERE id = ?", (plan_id,))
        await db.commit()

    class _Router:
        async def invoke(self, agent_type, req):
            return SimpleNamespace(content=json.dumps({**_REVISED, "criteria": _REVISED["success_criteria"]}), usage=None)

    async def _fake_router(state, team_id, *, entrypoint):
        return _Router()

    import bowhead_gateway.routes.plans as plans_mod

    monkeypatch.setattr(plans_mod, "team_router_or_409", _fake_router)
    resp = await _post(_app(store), plan_id, steps[0].id, {"confirm": False})
    assert resp.status_code == 200
    assert "warning" in resp.json()


@pytest.mark.asyncio
async def test_preview_to_confirm_round_trip_verbatim(monkeypatch, tmp_path) -> None:
    """实现 review P0-1 回归钉: confirm body = preview 响应 revised **逐字回传**
    (键形状=agent_name)。此前 confirm 只读 agent 键 → 真实用户 100% 422,
    而测试 fixture 手造 agent 键遮住了 round-trip。"""
    store, plan_id, steps = await _real_store(tmp_path)

    class _Router:
        async def invoke(self, agent_type, req):
            return SimpleNamespace(content=json.dumps({**_REVISED, "criteria": _REVISED["success_criteria"]}), usage=None)

    async def _fake_router(state, team_id, *, entrypoint):
        return _Router()

    import bowhead_gateway.routes.plans as plans_mod

    monkeypatch.setattr(plans_mod, "team_router_or_409", _fake_router)
    pv = await _post(_app(store), plan_id, steps[0].id, {"confirm": False})
    assert pv.status_code == 200
    body = pv.json()
    # Return preview output verbatim (no key rewriting).
    resp = await _post(
        _app(store),
        plan_id,
        steps[0].id,
        {"confirm": True, "expected_rev": body["expected_rev"], "revised": body["revised"]},
    )
    assert resp.status_code == 200, resp.text
    by_id = {s.id: s for s in await store.list_steps(plan_id)}
    new_id = resp.json()["revised_step_id"]
    assert by_id[new_id].agent_name == body["revised"]["agent_name"]
    assert by_id[new_id].objective == body["revised"]["objective"]


@pytest.mark.asyncio
async def test_confirm_empty_criteria_inherits_old(tmp_path) -> None:
    """R2-F11 confirm 侧: 回传空验收不得让 criteria_unmet 判死的步修订后免检."""
    store, plan_id, steps = await _real_store(tmp_path)
    import aiosqlite

    async with aiosqlite.connect(store._db_path) as db:  # noqa: SLF001
        await db.execute("UPDATE plan_steps SET success_criteria=? WHERE id=?", ('["旧验收: 含官方信源"]', steps[0].id))
        await db.commit()
    resp = await _post(
        _app(store),
        plan_id,
        steps[0].id,
        {"confirm": True, "expected_rev": 1, "revised": {**_REVISED, "success_criteria": []}},
    )
    assert resp.status_code == 200
    by_id = {s.id: s for s in await store.list_steps(plan_id)}
    assert by_id[resp.json()["revised_step_id"]].success_criteria == ("旧验收: 含官方信源",)


@pytest.mark.asyncio
async def test_confirm_does_not_return_successor_graph_as_reviewed(monkeypatch, tmp_path) -> None:
    store, plan_id, steps = await _real_store(tmp_path)
    real_supersede = store.supersede_step

    async def supersede_then_edit(**kwargs):
        committed = await real_supersede(**kwargs)
        assert committed
        current = await store.list_steps(plan_id)
        replacement = next(step for step in current if step.id == kwargs["new_step"].id)
        await store.update_step_status(replacement.id, status="failed")
        other = new_step(plan_id=plan_id, seq=9, agent_name="compute", objective="another browser")
        assert await real_supersede(plan_id=plan_id, old_step_id=replacement.id, new_step=other, downstream_reset_ids=[], expected_rev=2)
        return committed

    monkeypatch.setattr(store, "supersede_step", supersede_then_edit)
    response = await _post(_app(store), plan_id, steps[0].id, {"confirm": True, "expected_rev": 1, "revised": _REVISED})
    assert response.status_code == 409
    assert (await store.get_plan(plan_id, user_id="u_dev_alice", workspace_id="1")).current_rev == 3
