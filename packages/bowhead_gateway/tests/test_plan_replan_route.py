"""批 3 全量 replan 路由 (batch3-replan-design §2-S4).

Pins:
- preview: done 步进「固定产出清单」、LLM 只出未完成子图、known_external_ids 直连、
  新步 criteria 第二遍、不落库
- confirm: 回传子图逐字持久、悬空依赖 422、**done 步依赖 superseded 行时 level
  重算不炸(review R1-F2/R2-F4 的 P0 场景铁断言)**、diff 正确、CAS 409
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


async def _post(app: Any, plan_id: str, body: dict) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.post(f"/api/v1/plans/{plan_id}/replan", json={"workspace_id": "1", **body})


async def _partial_ok_store(tmp_path):
    """P0 场景形状: C(done) 依赖 [A(done), F(failed)]——F 将被 supersede,
    C 的 depends_on 留着悬空引用(D3.5 不改写)。另有 blocked 下游 G。"""
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan = await store.create_plan(session_id="s", workspace_id="1", user_id="u_dev_alice", title="t")
    a = new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="A 上游", level=0)
    f = new_step(plan_id=plan.id, seq=1, agent_name="kb-analyst", objective="F 失败腿", level=0)
    c = new_step(plan_id=plan.id, seq=2, agent_name="financial-analyst", objective="C 部分ok已成", level=1, depends_on=(a.id, f.id))
    g = new_step(plan_id=plan.id, seq=3, agent_name="consultant", objective="G 下游", level=2, depends_on=(f.id,))
    await store.add_steps([a, f, c, g])
    await store.update_step_status(a.id, status="done", payload_json=json.dumps({"exec_summary": "A ok"}), result_digest="A ok")
    await store.update_step_status(f.id, status="failed", error="boom", error_kind="TimeoutError")
    await store.update_step_status(c.id, status="done", payload_json=json.dumps({"exec_summary": "C ok"}), result_digest="C ok")
    await store.update_step_status(g.id, status="blocked", error="所有上游步未成功", error_kind="upstream_failed")
    await store.update_plan_status(plan.id, "running")
    await store.update_plan_status(plan.id, "blocked")
    return store, plan.id, {"a": a, "f": f, "c": c, "g": g}


@pytest.mark.asyncio
async def test_preview_renders_done_and_replans_unfinished(monkeypatch, tmp_path) -> None:
    store, plan_id, S = await _partial_ok_store(tmp_path)
    captured: dict[str, str] = {}

    class _Router:
        async def invoke(self, agent_type, req):
            user_msg = req.messages[0].content
            if "验收官" in (req.system or ""):
                return SimpleNamespace(content=json.dumps({"criteria": {}}), usage=None)
            captured["user"] = user_msg
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "steps": [
                            {"id": "n1", "agent_name": "consultant", "objective": "换思路补失败腿", "deliverable": "", "depends_on": [S["a"].id]},
                        ]
                    }
                ),
                usage=None,
            )

    async def _fake_router(state, team_id, *, entrypoint):
        return _Router()

    import bowhead_gateway.routes.plans as plans_mod

    monkeypatch.setattr(plans_mod, "team_router_or_409", _fake_router)
    resp = await _post(_app(store), plan_id, {"confirm": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["preview"] is True
    # done steps enter the fixed list; unfinished parts carry failure notes.
    assert S["a"].id in captured["user"] and S["c"].id in captured["user"]
    assert "F 失败腿" in captured["user"] and "TimeoutError" in captured["user"]
    assert "C 部分ok已成" not in captured["user"].split("未完成的部分")[1]  # done excluded from re-authoring domain.
    # known_external_ids direct connection: new steps depend on real done IDs.
    assert data["new_steps"][0]["depends_on"] == [S["a"].id]
    assert data["new_steps"][0]["origin"] == "replan"
    assert set(data["superseded"]) == {S["f"].id, S["g"].id}
    assert set(data["kept_done"]) == {S["a"].id, S["c"].id}
    # No persistence.
    assert len(await store.list_steps(plan_id)) == 4


@pytest.mark.asyncio
async def test_preview_model_calls_use_the_viewer_zone(monkeypatch, tmp_path) -> None:
    """Replanning runs outside a Turn; both model calls (steps, then criteria) date on the viewer's calendar."""
    from bowhead_core.model_clock import turn_time_zone_name

    store, plan_id, S = await _partial_ok_store(tmp_path)
    seen: list[tuple[str, str]] = []

    class _Router:
        async def invoke(self, agent_type, req):
            if "验收官" in (req.system or ""):
                seen.append(("criteria", turn_time_zone_name()))
                return SimpleNamespace(content=json.dumps({"criteria": {}}), usage=None)
            seen.append(("steps", turn_time_zone_name()))
            step = {"id": "n1", "agent_name": "consultant", "objective": "补失败腿", "deliverable": "", "depends_on": [S["a"].id]}
            return SimpleNamespace(content=json.dumps({"steps": [step]}), usage=None)

    async def _fake_router(state, team_id, *, entrypoint):
        return _Router()

    import bowhead_gateway.routes.plans as plans_mod

    monkeypatch.setattr(plans_mod, "team_router_or_409", _fake_router)
    transport = httpx2.ASGITransport(app=_app(store))
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            f"/api/v1/plans/{plan_id}/replan",
            json={"workspace_id": "1", "confirm": False},
            headers={"X-Bowhead-Time-Zone": "America/Los_Angeles"},
        )
    assert resp.status_code == 200, resp.text
    assert {kind for kind, _ in seen} == {"steps", "criteria"}
    assert {zone for _, zone in seen} == {"America/Los_Angeles"}
    assert turn_time_zone_name() == ""


@pytest.mark.asyncio
async def test_confirm_survives_done_step_with_dangling_dep_iron(tmp_path) -> None:
    """P0 铁断言(R1-F2/R2-F4): C(done) 依赖将被 superseded 的 F——confirm 的
    level 重算必须 compute-only 剥离悬空引用而非 DAGUnknownDependencyError。"""
    store, plan_id, S = await _partial_ok_store(tmp_path)
    new_id = "ps_aaaa????bbbb".replace("?", "0")
    body = {
        "confirm": True,
        "expected_rev": 1,
        "new_steps": [
            {
                "id": new_id,
                "agent": "consultant",
                "objective": "换思路补失败腿",
                "deliverable": "",
                "depends_on": [S["a"].id],
                "needs_corpus": False,
                "needs_web": False,
                "success_criteria": ["含具体数值"],
            }
        ],
    }
    resp = await _post(_app(store), plan_id, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert set(data["superseded"]) == {S["f"].id, S["g"].id}
    assert data["new"] == [new_id]
    assert data["current_rev"] == 2
    by_id = {s.id: s for s in await store.list_steps(plan_id)}
    # Leave done rows unchanged (retain payload fuel), including dangling depends_on (D3.5).
    assert by_id[S["c"].id].status == StepStatus.DONE
    assert by_id[S["c"].id].payload_json
    assert S["f"].id in by_id[S["c"].id].depends_on
    # Old non-done rows → superseded + generation sentinel.
    assert by_id[S["f"].id].status == StepStatus.SUPERSEDED
    assert by_id[S["f"].id].superseded_by == "replan:rev2"
    assert by_id[S["g"].id].status == StepStatus.SUPERSEDED
    # Persist new rows verbatim.
    assert by_id[new_id].objective == "换思路补失败腿"
    assert by_id[new_id].success_criteria == ("含具体数值",)
    assert by_id[new_id].origin == "replan"
    after = await store.get_plan(plan_id, user_id="u_dev_alice", workspace_id="1")
    assert after.replan_count == 1
    assert after.current_rev == 2


@pytest.mark.asyncio
async def test_confirm_dangling_dep_and_bad_id_422(tmp_path) -> None:
    store, plan_id, S = await _partial_ok_store(tmp_path)
    # Dependency on superseded F → 422 (may reference only done/new steps).
    body = {
        "confirm": True,
        "expected_rev": 1,
        "new_steps": [{"id": "ps_000000000001", "agent": "consultant", "objective": "x", "depends_on": [S["f"].id]}],
    }
    resp = await _post(_app(store), plan_id, body)
    assert resp.status_code == 422
    # ID collides with existing row → 422.
    body2 = {
        "confirm": True,
        "expected_rev": 1,
        "new_steps": [{"id": S["a"].id, "agent": "consultant", "objective": "x", "depends_on": []}],
    }
    resp2 = await _post(_app(store), plan_id, body2)
    assert resp2.status_code == 422
    assert len(await store.list_steps(plan_id)) == 4  # Zero changes.


@pytest.mark.asyncio
async def test_confirm_wrong_rev_409(tmp_path) -> None:
    store, plan_id, S = await _partial_ok_store(tmp_path)
    body = {
        "confirm": True,
        "expected_rev": 42,
        "new_steps": [{"id": "ps_000000000002", "agent": "consultant", "objective": "x", "depends_on": []}],
    }
    resp = await _post(_app(store), plan_id, body)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_replan_flag_off_404_and_not_blocked_409(monkeypatch, tmp_path) -> None:
    store, plan_id, S = await _partial_ok_store(tmp_path)
    monkeypatch.setenv("BOWHEAD_PLANNING_REPLAN", "0")
    assert (await _post(_app(store), plan_id, {"confirm": False})).status_code == 404
    monkeypatch.delenv("BOWHEAD_PLANNING_REPLAN")
    await store.update_plan_status(plan_id, "running")  # Not blocked.
    assert (await _post(_app(store), plan_id, {"confirm": False})).status_code == 409
