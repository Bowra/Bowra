"""批 4 provenance 导出 route (batch4-provenance-design D-D).

Pins: 双 flag 404 / ownership 404 / happy(真 PlanStore+真 TurnLogStore 合成
tool_result 事件+fake canvas) / canvas 缺失退化 / list_events 类型过滤(P2-8)。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from bowhead_core.plan_store import PlanStore, new_step
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_gateway.main import create_app
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.settings import Settings


class _FakeCanvasStore:
    def __init__(self, sections: list[Any], canvas_id: str = "cv_1") -> None:
        self._sections = sections
        self._canvas_id = canvas_id

    async def get_canvas_by_session(self, session_id: str):
        return SimpleNamespace(id=self._canvas_id)

    async def list_sections(self, canvas_id: str, *, include_hidden: bool = False):
        return self._sections


def _app(store: Any, turn_log: Any = None, canvas: Any = None) -> Any:
    app = create_app()
    app.state.settings = Settings()
    app.state.plan_store = store
    if turn_log is not None:
        app.state.turn_log_store = turn_log
    if canvas is not None:
        app.state.canvas_store = canvas
    app.dependency_overrides[_current_user] = lambda: SimpleNamespace(id="u_dev_alice", team_id="")
    return app


async def _get(app: Any, plan_id: str) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.get(f"/api/v1/plans/{plan_id}/provenance", params={"workspace_id": "1"})


async def _seed(tmp_path):
    store = PlanStore(str(tmp_path / "p.db"))
    await store.initialize()
    plan = await store.create_plan(session_id="s_prov", workspace_id="1", user_id="u_dev_alice", title="t")
    a = new_step(plan_id=plan.id, seq=0, agent_name="compute", objective="a", level=0)
    await store.add_steps([a])
    await store.update_step_status(a.id, status="done", turn_id="t_exec", payload_json=json.dumps({"exec_summary": "ok"}))
    return store, plan.id, a


@pytest.mark.asyncio
async def test_flag_off_404(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_PROVENANCE", "0")
    store, plan_id, _ = await _seed(tmp_path)
    assert (await _get(_app(store), plan_id)).status_code == 404


@pytest.mark.asyncio
async def test_cross_tenant_404(tmp_path) -> None:
    store, plan_id, _ = await _seed(tmp_path)
    app = _app(store)
    app.dependency_overrides[_current_user] = lambda: SimpleNamespace(id="u_other", team_id="")
    assert (await _get(app, plan_id)).status_code == 404


@pytest.mark.asyncio
async def test_happy_full_chain_with_real_event_store(tmp_path) -> None:
    store, plan_id, step = await _seed(tmp_path)
    tlog = TurnLogStore(str(tmp_path / "t.db"))
    await tlog.initialize()
    await tlog.record(
        turn_id="t_exec",
        event_type="tool_result",
        payload={"subagent_id": f"sub_{step.id}", "tool_name": "bi_query", "call_id": "tl_9", "ok": True, "duration_ms": 7},
    )
    await tlog.record(
        turn_id="t_exec",
        event_type="tool_result",
        payload={"subagent_id": "sub_other", "tool_name": "kb_search", "call_id": "tl_x", "ok": True, "duration_ms": 3},
    )
    await tlog.record(turn_id="t_exec", event_type="step_done", payload={"plan_id": plan_id, "step_id": step.id})  # Dual-semantics noise.
    canvas = _FakeCanvasStore([SimpleNamespace(id="sec_1", kind="narrative", turn_id="t_exec", owner=f"writer-compute-{step.id}")])
    resp = await _get(_app(store, tlog, canvas), plan_id)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    st = data["steps"][0]
    assert st["subagent_id"] == f"sub_{step.id}"
    assert st["used_tools"] == [{"call_id": "tl_9", "tool_name": "bi_query", "ok": True, "duration_ms": 7}]
    assert data["sections"][0]["produced_by_step"] == step.id
    assert data["completeness"]["sections_traceable"] == "1/1"
    assert data["completeness"]["steps_with_tool_events"] == "1/1"


@pytest.mark.asyncio
async def test_no_canvas_no_events_degrades(tmp_path) -> None:
    store, plan_id, _ = await _seed(tmp_path)
    resp = await _get(_app(store), plan_id)  # No turn_log/canvas store.
    assert resp.status_code == 200
    data = resp.json()
    assert data["sections"] == []
    assert data["steps"][0]["used_tools"] == []


@pytest.mark.asyncio
async def test_list_events_type_filter_sql_side(tmp_path) -> None:
    tlog = TurnLogStore(str(tmp_path / "t2.db"))
    await tlog.initialize()
    await tlog.record(turn_id="t_z", event_type="tool_result", payload={"subagent_id": "sub_a"})
    await tlog.record(turn_id="t_z", event_type="canvas.section_chunk", payload={"big": "x" * 100})
    await tlog.record(turn_id="t_z", event_type="tool_result", payload={"subagent_id": "sub_b"})
    rows = await tlog.list_events("t_z", event_types=("tool_result",))
    assert [r.event_type for r in rows] == ["tool_result", "tool_result"]
    rows_all = await tlog.list_events("t_z")
    assert len(rows_all) == 3  # No filter = original semantics.
