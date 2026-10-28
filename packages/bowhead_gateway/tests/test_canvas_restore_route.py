"""方案A (report-edit-audit) — `/revisions/{rid}/restore` 路由契约.

404 = revision 不存在/跨 canvas/非 delete（统一口径不泄漏，与 A0-4 同形）;
409 = 全员已在场; 422 = 旧格式无恢复 envelope; 列表 DTO 的 `restorable` 标记。
"""

from __future__ import annotations

import json

import httpx2
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import CanvasStore


@pytest.fixture
async def ctx(tmp_path):
    db = tmp_path / "restore_route.db"
    store = CanvasStore(db)
    await store.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.canvas_store = store
    app.state.artifact_fetch_secret = "test-artifact-fetch-secret"
    app.state.orchestrator = None
    app.state.datarc = None

    dev_user = Settings().dev_default_user
    mine = await store.ensure_canvas(session_id="s_mine", workspace_id="ws_t", user_id=dev_user)
    other = await store.ensure_canvas(session_id="s_other", workspace_id="ws_t", user_id=dev_user)
    return app, store, mine, other


def _client(app) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw")


async def _delete_revision_id(store: CanvasStore, canvas_id: str, section_id: str) -> str:
    async with (
        open_shared_ctx(str(store.db_path)) as db,
        db.execute(
            "SELECT id FROM canvas_revisions WHERE canvas_id=? AND op='delete' AND section_id=?",
            (canvas_id, section_id),
        ) as cur,
    ):
        return str((await cur.fetchone())[0])


@pytest.mark.integration
async def test_restore_happy_path_and_second_call_409(ctx) -> None:
    app, store, mine, _other = ctx
    chart = await store.append_section(canvas_id=mine.id, kind="chart", payload={"spec": {"t": 1}})
    host = await store.append_section(
        canvas_id=mine.id,
        kind="narrative",
        payload={"markdown": f"[figure:{chart.id}]"},
    )
    await store.delete_section(section_id=host.id, canvas_id=mine.id, cascade=True)
    rv = await _delete_revision_id(store, mine.id, host.id)

    async with _client(app) as c:
        r = await c.post(f"/api/v1/canvas/{mine.id}/revisions/{rv}/restore")
        assert r.status_code == 200
        assert set(r.json()["restoredSectionIds"]) == {host.id, chart.id}

        r2 = await c.post(f"/api/v1/canvas/{mine.id}/revisions/{rv}/restore")
        assert r2.status_code == 409
        assert r2.json()["error"] == "already_restored"


@pytest.mark.integration
async def test_restore_cross_canvas_is_404(ctx) -> None:
    app, store, mine, other = ctx
    sec = await store.append_section(canvas_id=mine.id, kind="narrative", payload={"markdown": "x"})
    await store.delete_section(section_id=sec.id, canvas_id=mine.id)
    rv = await _delete_revision_id(store, mine.id, sec.id)

    async with _client(app) as c:
        r = await c.post(f"/api/v1/canvas/{other.id}/revisions/{rv}/restore")
    assert r.status_code == 404
    # Never restored to the wrong canvas.
    assert {s.id for s in await store.list_sections(other.id)} == set()


@pytest.mark.integration
async def test_restore_legacy_revision_is_422(ctx) -> None:
    app, store, mine, _other = ctx
    async with open_shared_ctx(str(store.db_path)) as db:
        await db.execute(
            "INSERT INTO canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by_turn_id, created_at) "
            "VALUES ('rv_legacy', ?, 'sec_old', 'delete', ?, NULL, NULL, '2026-01-01T00:00:00+00:00')",
            (mine.id, json.dumps({"markdown": "旧"})),
        )
        await db.commit()

    async with _client(app) as c:
        r = await c.post(f"/api/v1/canvas/{mine.id}/revisions/rv_legacy/restore")
    assert r.status_code == 422
    assert r.json()["error"] == "restore_unsupported_legacy"


@pytest.mark.integration
async def test_revisions_list_marks_restorable(ctx) -> None:
    app, store, mine, _other = ctx
    sec = await store.append_section(canvas_id=mine.id, kind="narrative", payload={"markdown": "x"})
    await store.delete_section(section_id=sec.id, canvas_id=mine.id)
    async with open_shared_ctx(str(store.db_path)) as db:
        await db.execute(
            "INSERT INTO canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by_turn_id, created_at) "
            "VALUES ('rv_legacy', ?, 'sec_old', 'delete', ?, NULL, NULL, '2026-01-01T00:00:00+00:00')",
            (mine.id, json.dumps({"markdown": "旧"})),
        )
        await db.commit()

    async with _client(app) as c:
        r = await c.get(f"/api/v1/canvas/{mine.id}/revisions")
    assert r.status_code == 200
    by_id = {item["id"]: item for item in r.json()["items"]}
    flags = {item["op"]: item["restorable"] for item in by_id.values() if item["op"] != "delete"}
    # All non-delete items False.
    assert all(v is False for v in flags.values())
    # New-format delete=True; old-format=False.
    delete_items = [i for i in by_id.values() if i["op"] == "delete"]
    assert {i["restorable"] for i in delete_items if i["id"] != "rv_legacy"} == {True}
    assert by_id["rv_legacy"]["restorable"] is False


@pytest.mark.integration
async def test_revisions_list_survives_malformed_null_id_row(ctx) -> None:
    """防御: 带外写入的 id=NULL 行不许拖垮整个列表 (dogfood 实锤 500)。"""
    app, store, mine, _other = ctx
    sec = await store.append_section(canvas_id=mine.id, kind="narrative", payload={"markdown": "x"})
    async with open_shared_ctx(str(store.db_path)) as db:
        await db.execute(
            "INSERT INTO canvas_revisions(id, canvas_id, section_id, op, prev_payload, new_payload, by_turn_id, created_at) "
            "VALUES (NULL, ?, ?, 'append', NULL, ?, NULL, '2026-01-01T00:00:00+00:00')",
            (mine.id, sec.id, json.dumps({"markdown": "x"})),
        )
        await db.commit()

    async with _client(app) as c:
        r = await c.get(f"/api/v1/canvas/{mine.id}/revisions")
    assert r.status_code == 200
    ids = [item["id"] for item in r.json()["items"]]
    assert all(ids), "NULL id 行应被跳过而不是 500"
    assert len(ids) >= 1


@pytest.mark.integration
async def test_revisions_project_source_metadata_in_regular_and_restore_payloads(ctx) -> None:
    app, store, mine, _other = ctx
    sec = await store.append_section(
        canvas_id=mine.id,
        kind="sources",
        payload={
            "entries": [
                {
                    "artifact_id": "art_private_revision",
                    "label": "Private payroll",
                    "query": "select salary",
                },
            ],
        },
    )
    await store.delete_section(section_id=sec.id, canvas_id=mine.id)

    async with _client(app) as client:
        response = await client.get(f"/api/v1/canvas/{mine.id}/revisions")
    assert response.status_code == 200, response.text
    serialized = repr(response.json())
    assert "source-ref-v1:" in serialized
    assert "art_private_revision" not in serialized
    assert "Private payroll" not in serialized
    assert "select salary" not in serialized


@pytest.mark.integration
async def test_sources_section_delete_is_refused_with_a_reason(ctx) -> None:
    """canvas-elements 来源合并批：来源段是整份文档共用的溯源载体，用户面拒删。

    ★ 判据挂在**路由**不挂在 `delete_section`：store 是原语，上面那条
    「删掉的来源段不许在 revisions 里泄漏 artifact_id/label/query」的安全测试要用它。
    """
    app, store, mine, _other = ctx
    sec = await store.append_section(canvas_id=mine.id, kind="sources", payload={"entries": [{"label": "x"}]})
    narrative = await store.append_section(canvas_id=mine.id, kind="narrative", payload={"markdown": "y"})

    async with _client(app) as client:
        refused = await client.delete(f"/api/v1/canvas/{mine.id}/sections/{sec.id}")
        # Positive control: ordinary sections delete through the same route, proving rejection is not a broken route.
        allowed = await client.delete(f"/api/v1/canvas/{mine.id}/sections/{narrative.id}")

    assert refused.status_code == 409, refused.text
    assert "数据来源" in refused.text
    assert allowed.status_code == 200, allowed.text
    assert [s.id for s in await store.list_sections(mine.id)] == [sec.id]
