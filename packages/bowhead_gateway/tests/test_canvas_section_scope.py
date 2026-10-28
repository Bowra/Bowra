"""A0-4 (report-edit-audit) — `/sections/{sid}` 族路由的跨 canvas IDOR 防护 + 删除并发防护.

此前路由只验 canvas 所有权、不验 sid∈canvas: 拥有任意一个 canvas 即可
PATCH/DELETE/split/merge 他人 canvas 的 section。
"""

from __future__ import annotations

import httpx2
import pytest
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import CanvasStore


@pytest.fixture
async def ctx(tmp_path):
    db = tmp_path / "scope.db"
    store = CanvasStore(db)
    await store.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.canvas_store = store
    app.state.orchestrator = None
    app.state.datarc = None

    dev_user = Settings().dev_default_user
    mine = await store.ensure_canvas(session_id="s_mine", workspace_id="ws_t", user_id=dev_user)
    other = await store.ensure_canvas(session_id="s_other", workspace_id="ws_t", user_id="usr_victim")
    my_sec = await store.append_section(canvas_id=mine.id, kind="narrative", payload={"markdown": "mine"})
    victim_sec = await store.append_section(canvas_id=other.id, kind="narrative", payload={"markdown": "victim"})
    return app, store, mine, other, my_sec, victim_sec


def _client(app) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw")


@pytest.mark.integration
async def test_delete_cross_canvas_section_is_404_and_untouched(ctx) -> None:
    app, store, mine, _other, _my_sec, victim_sec = ctx
    async with _client(app) as c:
        r = await c.delete(f"/api/v1/canvas/{mine.id}/sections/{victim_sec.id}")
    assert r.status_code == 404
    # Victim section remains unchanged.
    assert await store.get_section_canvas_id(victim_sec.id) is not None


@pytest.mark.integration
async def test_patch_cross_canvas_section_is_404_and_untouched(ctx) -> None:
    app, store, mine, other, _my_sec, victim_sec = ctx
    async with _client(app) as c:
        r = await c.patch(
            f"/api/v1/canvas/{mine.id}/sections/{victim_sec.id}",
            json={"patch": {"markdown": "pwned"}},
        )
    assert r.status_code == 404
    rows = await store.list_sections(other.id)
    assert rows[0].payload["markdown"] == "victim"


@pytest.mark.integration
async def test_split_and_merge_cross_canvas_are_rejected(ctx) -> None:
    app, _store, mine, _other, _my_sec, victim_sec = ctx
    async with _client(app) as c:
        r1 = await c.post(f"/api/v1/canvas/{mine.id}/sections/{victim_sec.id}/split", json={"offset": 1})
        r2 = await c.post(f"/api/v1/canvas/{mine.id}/sections/{victim_sec.id}/merge", json={})
    assert r1.status_code == 404
    assert r2.status_code == 404


@pytest.mark.integration
async def test_delete_expected_rev_conflict_and_success(ctx) -> None:
    app, store, mine, _other, my_sec, _victim_sec = ctx
    async with _client(app) as c:
        # Revision mismatch →409 + current_rev.
        r = await c.delete(f"/api/v1/canvas/{mine.id}/sections/{my_sec.id}", params={"expected_rev": 99})
        assert r.status_code == 409
        assert r.json()["current_rev"] == my_sec.rev
        # Matching revision → successful deletion.
        r2 = await c.delete(f"/api/v1/canvas/{mine.id}/sections/{my_sec.id}", params={"expected_rev": my_sec.rev})
        assert r2.status_code == 200
        assert r2.json()["deleted"] == my_sec.id
    assert await store.get_section_canvas_id(my_sec.id) is None


@pytest.mark.integration
async def test_cascade_route_returns_deleted_ids_and_removes_inline_figures(ctx) -> None:
    """A2: cascade=1 → 宿主 + 独占内联图, 回传实删清单 (FE 提示用真实数量)."""
    app, store, mine, _other, _my_sec, _victim_sec = ctx
    chart = await store.append_section(canvas_id=mine.id, kind="chart", payload={"spec": {}})
    host = await store.append_section(
        canvas_id=mine.id,
        kind="narrative",
        payload={"markdown": f"正文 [figure:{chart.id}]"},
    )
    async with _client(app) as c:
        r = await c.delete(f"/api/v1/canvas/{mine.id}/sections/{host.id}", params={"cascade": "1"})
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == host.id
    assert set(body["deletedSectionIds"]) == {host.id, chart.id}
    assert await store.get_section_canvas_id(chart.id) is None


@pytest.mark.integration
async def test_route_without_cascade_keeps_inline_figure(ctx) -> None:
    app, store, mine, _other, _my_sec, _victim_sec = ctx
    chart = await store.append_section(canvas_id=mine.id, kind="chart", payload={"spec": {}})
    host = await store.append_section(
        canvas_id=mine.id,
        kind="narrative",
        payload={"markdown": f"正文 [figure:{chart.id}]"},
    )
    async with _client(app) as c:
        r = await c.delete(f"/api/v1/canvas/{mine.id}/sections/{host.id}")
    assert r.status_code == 200
    assert r.json()["deletedSectionIds"] == [host.id]
    assert await store.get_section_canvas_id(chart.id) == mine.id


@pytest.mark.integration
async def test_reorder_route_returns_actual_full_order(ctx) -> None:
    """A3 review: reorder 是"补全重编"语义 → 路由必须回传**实际落库序**, 不是请求序。"""
    app, store, mine, _other, my_sec, _victim_sec = ctx
    chart = await store.append_section(canvas_id=mine.id, kind="chart", payload={"spec": {}})
    async with _client(app) as c:
        # Request only one id (partial order, as agent tools do).
        r = await c.post(f"/api/v1/canvas/{mine.id}/reorder", json={"order": [chart.id]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Append omitted my_sec at the end, and include it in the response.
    assert body["order"] == [chart.id, my_sec.id]
    assert [s.id for s in await store.list_sections(mine.id)] == [chart.id, my_sec.id]
    assert [s.seq for s in await store.list_sections(mine.id)] == [1, 2]
