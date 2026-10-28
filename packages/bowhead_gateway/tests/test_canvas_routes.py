"""Canvas HTTP routes — 集成测试."""

from __future__ import annotations

import io
from typing import Any

import httpx2
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_export import ExportJob
from bowhead_core.user_store import UserStore
from bowhead_gateway.main import create_app
from bowhead_gateway.routes.canvas import _job_dto
from bowhead_gateway.settings import Settings
from PIL import Image

from bowhead_core import CanvasStore, canvas_export


@pytest.fixture
async def app_ctx(tmp_path):
    db = tmp_path / "test.db"
    canvas = CanvasStore(db)
    await canvas.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.canvas_store = canvas
    app.state.artifact_fetch_secret = "test-artifact-fetch-secret"
    app.state.orchestrator = None
    app.state.datarc = None
    return app


def _png_bytes() -> bytes:
    img = Image.new("RGB", (80, 60), (240, 248, 255))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


async def _wire_artifact_store(app_ctx, tmp_path) -> ArtifactStore:
    store = ArtifactStore(
        backend=LocalBackend(root_dir=tmp_path / "files"),
        db_path=tmp_path / "artifacts.db",
    )
    await store.initialize()
    app_ctx.state.artifact_store = store
    return store


async def test_composed_chart_settings_round_trip_and_validation_are_atomic(app_ctx) -> None:
    import copy

    store = app_ctx.state.canvas_store
    canvas = await store.ensure_canvas(session_id="s_composed", workspace_id="w", user_id=app_ctx.state.settings.dev_default_user)
    child = {
        "kind": "table",
        "title": "Values",
        "row_count": 1,
        "data_artifact_id": "art_example",
        "columns": [{"key": "value", "header": "Value", "source_header": "Raw value", "type": "numeric"}],
        "rows": [{"value": 12}],
    }
    root = {
        **copy.deepcopy(child),
        "views": [{"id": "first", "kind": "chart", "spec": copy.deepcopy(child)}, {"id": "second", "kind": "chart", "spec": copy.deepcopy(child)}],
        "layout": {"type": "split", "children": [{"view": "first"}, {"view": "second"}]},
    }
    section = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": root})
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        detail = (await client.get(f"/api/v1/canvas/{canvas.id}")).json()
        incoming = copy.deepcopy(detail["sections"][0]["spec"])
        incoming["views"][1]["spec"]["kind"] = "kpi"
        incoming["views"][1]["spec"]["title"] = "Original values"
        incoming["layout"] = {"type": "grid2", "children": [{"view": "second"}, {"view": "first"}]}
        response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": incoming}, "expected_rev": 1})
        assert response.status_code == 200, response.text
        assert response.json()["rev"] == 2
        current = (await store.list_sections(canvas.id))[0]
        assert current.payload["spec"]["views"][0] == root["views"][0]
        assert current.payload["spec"]["views"][1]["spec"]["columns"] == child["columns"]
        refreshed = (await client.get(f"/api/v1/canvas/{canvas.id}")).json()["sections"][0]
        assert refreshed["spec"]["views"][1]["spec"]["kind"] == "kpi"
        assert refreshed["spec"]["layout"] == incoming["layout"]
        assert refreshed["spec"]["views"][1]["spec"]["title"] == "Original values"
        invalid_layout = copy.deepcopy(refreshed["spec"])
        invalid_layout["layout"]["children"][0]["view"] = "outside_root"
        response = await client.patch(
            f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": invalid_layout}, "expected_rev": 2}
        )
        assert response.status_code == 409
        assert len(await store.list_revisions(canvas_id=canvas.id)) == 2
        bad = copy.deepcopy(refreshed["spec"])
        bad["views"][1]["spec"]["show_legend"] = "false"
        response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": bad}, "expected_rev": 2})
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "invalid_chart_expression"
        assert len(await store.list_revisions(canvas_id=canvas.id)) == 2
        response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": incoming}, "expected_rev": 1})
        assert response.status_code == 409
        assert len(await store.list_sections(canvas.id)) == 1


@pytest.mark.integration
async def test_canvas_by_session_empty(app_ctx) -> None:
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/canvas/by-session/unknown")
        assert r.status_code == 200
        # B-1: CanvasDetailDTO adds sourceRefs, default [].
        assert r.json() == {
            "canvas": None,
            "sections": [],
            "sourceRefs": [],
            "projectionToken": None,
            "surfaceProjection": None,
            "surfaceToken": None,
            "exportV2Capabilities": [],
            # A3 (report-edit-audit): canvas detail includes seq-authoritative flag, even for empty canvases.
            "seqAuthoritative": False,
            # source-accessibility c1/review F5: None=unavailable this request/gate OFF (FE retains old values),
            # {}=actually no citations (FE clears them). Default gate OFF ⇒ null.
            "turnCitations": None,
        }


@pytest.mark.integration
async def test_canvas_by_session_missing_expected_report_placeholder(app_ctx) -> None:
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/canvas/by-session/unknown?expect_report=true")
        assert r.status_code == 403
        assert r.json()["detail"] == {"code": "report_unavailable"}


@pytest.mark.integration
async def test_canvas_by_session_design_payload_roundtrip(app_ctx) -> None:
    """Editable design sections hydrate with their full structured payload."""
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(
        session_id="s_design_hydrate",
        workspace_id="w",
        user_id=uid,
    )
    await store.append_section(
        canvas_id=canvas.id,
        kind="design",
        payload={
            "title": "奶茶促销海报",
            "size_preset": "portrait_3_4",
            "canvas_width": 1140,
            "canvas_height": 1472,
            "background": {
                "type": "artifact",
                "artifact_id": "art_123456789abc",
                "fit": "cover",
                "opacity": 0.92,
            },
            "layers": [
                {
                    "id": "headline",
                    "type": "text",
                    "text": "夏日冰爽",
                    "x": 0.12,
                    "y": 0.1,
                    "w": 0.76,
                    "h": 0.12,
                    "font_family": "Noto Sans SC",
                    "font_size": 72,
                    "font_weight": 800,
                    "color": "#0055AA",
                    "align": "center",
                    "line_height": 1.1,
                    "letter_spacing": 1.2,
                    "rotation": 0,
                    "z": 10,
                    "locked": False,
                },
            ],
            "flattened_artifact_id": "art_flattened001",
            "flattened_source_rev": 1,
            "flattened_at": "2026-06-10T00:00:00Z",
        },
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/canvas/by-session/s_design_hydrate")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["canvas"]["id"] == canvas.id
    assert body["sourceRefs"] == []
    assert body["projectionToken"].startswith("sha256:")
    assert body["surfaceToken"].startswith("sha256:")
    assert body["surfaceProjection"]["contentProjectionToken"] == body["projectionToken"]
    assert body["surfaceProjection"]["surfaceToken"] == body["surfaceToken"]
    surface_entry = body["surfaceProjection"]["entries"][0]
    assert surface_entry["visualInputSha256"] is None
    assert {key: value for key, value in surface_entry.items() if key != "visualInputSha256"} == {
        "sectionId": body["sections"][0]["id"],
        "sectionRev": 1,
        "order": 0,
        "placement": "report_body",
        "parentSectionId": None,
        "effectiveKind": "design",
        "parityRequirement": "exact_visual",
    }
    assert body["surfaceProjection"]["exclusions"] == []
    section = body["sections"][0]
    assert section["kind"] == "design"
    assert section["title"] == "奶茶促销海报"
    assert section["background"]["artifact_id"] == "art_123456789abc"
    assert section["layers"][0]["font_weight"] == 800
    assert section["layers"][0]["line_height"] == 1.1
    assert section["layers"][0]["letter_spacing"] == 1.2
    assert section["flattened_artifact_id"] == "art_flattened001"
    assert section["flattened_source_rev"] == 1
    assert section["rev"] == 1


@pytest.mark.integration
async def test_canvas_full_lifecycle(app_ctx) -> None:
    """通过 store 直接注入，再走 HTTP 读/改/删."""
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id=uid)
    sec = await store.append_section(
        canvas_id=canvas.id,
        kind="h1",
        payload={"text": "Hi"},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get(f"/api/v1/canvas/{canvas.id}")
        assert r.status_code == 200
        body = r.json()
        assert body["canvas"]["id"] == canvas.id
        assert body["sections"][0]["text"] == "Hi"
        assert body["sections"][0]["rev"] == 1
        initial_projection_token = body["projectionToken"]
        initial_surface_token = body["surfaceToken"]

        r = await c.patch(
            f"/api/v1/canvas/{canvas.id}/sections/{sec.id}",
            json={"patch": {"text": "Hello"}},
        )
        assert r.status_code == 200
        assert r.json()["section"]["text"] == "Hello"
        assert r.json()["section"]["rev"] == 2
        assert r.json()["rev"] == 2

        r = await c.post(
            f"/api/v1/canvas/{canvas.id}/title",
            json={"title": "Demo Report"},
        )
        assert r.status_code == 200

        r = await c.get(f"/api/v1/canvas/{canvas.id}")
        assert r.status_code == 200
        assert r.json()["projectionToken"] != initial_projection_token
        assert r.json()["surfaceToken"] != initial_surface_token

        r = await c.delete(f"/api/v1/canvas/{canvas.id}/sections/{sec.id}")
        assert r.status_code == 200

        r = await c.get(f"/api/v1/canvas/{canvas.id}/revisions")
        assert r.status_code == 200
        # W11-10: DTO is CanvasRevisionsDTO.items, not .revisions.
        ops = sorted(v["op"] for v in r.json()["items"])
        assert ops == ["append", "delete", "patch"]


@pytest.mark.integration
async def test_canvas_revisions_carry_turn_ordinal(app_ctx) -> None:
    """外观刷新第 4b 批: 每条修改带 camelCase 的 turnOrdinal (整份报告内第几轮 AI 更新);
    手动编辑为 null; 非 owner 仍拿不到修改历史。"""
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(session_id="s_turn_ordinal", workspace_id="w", user_id=uid)
    first = await store.append_section(canvas_id=canvas.id, kind="narrative", payload={"markdown": "一"}, by_turn_id="turn_x")
    await store.append_section(canvas_id=canvas.id, kind="narrative", payload={"markdown": "二"}, by_turn_id="turn_y")
    await store.patch_section(section_id=first.id, patch={"markdown": "一改"})
    others = await store.ensure_canvas(session_id="s_turn_ordinal_other", workspace_id="w", user_id="someone_else")
    await store.append_section(canvas_id=others.id, kind="narrative", payload={"markdown": "别人的"}, by_turn_id="turn_z")

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get(f"/api/v1/canvas/{canvas.id}/revisions")
        assert r.status_code == 200
        items = r.json()["items"]
        assert all("turnOrdinal" in item and "turn_ordinal" not in item for item in items)
        assert sorted(((item["byTurnId"], item["turnOrdinal"]) for item in items), key=lambda pair: pair[0] or "") == [
            (None, None),
            ("turn_x", 1),
            ("turn_y", 2),
        ]

        denied = await c.get(f"/api/v1/canvas/{others.id}/revisions")
        assert denied.status_code == 403
        assert "items" not in denied.json()


@pytest.mark.integration
async def test_canvas_status_machine_routes(app_ctx) -> None:
    """R2: GET/POST /status + list/detail 带 effective_status (含 quarantine 派生)."""
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(session_id="s_status", workspace_id="w", user_id=uid)
    sec = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "正文内容海豚"},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        # Initially active.
        r = await c.get(f"/api/v1/canvas/{canvas.id}/status")
        assert r.status_code == 200
        assert r.json() == {"status": "active", "effectiveStatus": "active", "quarantinedCount": 0}

        # mark verified
        r = await c.post(f"/api/v1/canvas/{canvas.id}/status", json={"status": "verified"})
        assert r.status_code == 200 and r.json()["status"] == "verified"
        r = await c.get(f"/api/v1/canvas/{canvas.id}/status")
        assert r.json()["effectiveStatus"] == "verified"

        # Detail also includes effectiveStatus.
        r = await c.get(f"/api/v1/canvas/{canvas.id}")
        assert r.json()["canvas"]["effectiveStatus"] == "verified"

        # Invalid status →400.
        r = await c.post(f"/api/v1/canvas/{canvas.id}/status", json={"status": "bogus"})
        assert r.status_code == 400

        # Quarantine a section → effective becomes stale (stored status still verified).
        await store.set_quarantine(section_id=sec.id, quarantined=True, reason="t")
        r = await c.get(f"/api/v1/canvas/{canvas.id}/status")
        body = r.json()
        assert body["status"] == "verified"
        assert body["effectiveStatus"] == "stale"
        assert body["quarantinedCount"] == 1

        # List endpoint includes R2 fields + R4 full-body search.
        r = await c.get("/api/v1/canvas?q=海豚")
        items = r.json()["items"]
        assert len(items) == 1 and items[0]["id"] == canvas.id
        assert items[0]["effective_status"] == "stale"
        assert items[0]["quarantined_count"] == 1


@pytest.mark.integration
async def test_canvas_status_forbidden_for_non_owner(app_ctx) -> None:
    """R2: 改别人报告状态 → 403 (owner guard)."""
    store: CanvasStore = app_ctx.state.canvas_store
    canvas = await store.ensure_canvas(session_id="s_other", workspace_id="w", user_id="someone_else")
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/canvas/{canvas.id}/status", json={"status": "archived"})
        assert r.status_code == 403


@pytest.mark.integration
async def test_canvas_patch_expected_rev_conflict_returns_current_rev(app_ctx) -> None:
    """PATCH expected_rev is a real optimistic-concurrency contract."""
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(session_id="s_rev_conflict", workspace_id="w", user_id=uid)
    sec = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "Original"},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.patch(
            f"/api/v1/canvas/{canvas.id}/sections/{sec.id}",
            json={"patch": {"markdown": "First edit"}, "expected_rev": 1},
        )
        assert r.status_code == 200, r.text
        assert r.json()["rev"] == 2

        stale = await c.patch(
            f"/api/v1/canvas/{canvas.id}/sections/{sec.id}",
            json={"patch": {"markdown": "Stale overwrite"}, "expected_rev": 1},
        )
        assert stale.status_code == 409
        assert stale.json() == {"current_rev": 2}

        latest = await c.get(f"/api/v1/canvas/{canvas.id}")
        assert latest.status_code == 200
        assert latest.json()["sections"][0]["markdown"] == "First edit"


async def test_canvas_patch_bakes_legacy_markers_at_ingress(app_ctx) -> None:
    """Legacy canonical markers can round-trip only through viewer aliases, then bake."""
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(session_id="s_bake_ingress", workspace_id="w", user_id=uid)
    sec = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "旧稿 [num:5.2:亿元:src:tl_old] 与 [ref:chunk_ab12cd34ef56]。"},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        detail = await c.get(f"/api/v1/canvas/{canvas.id}")
        assert detail.status_code == 200, detail.text
        viewer_markdown = detail.json()["sections"][0]["markdown"]
        assert "tl_old" not in viewer_markdown
        assert "chunk_ab12cd34ef56" not in viewer_markdown
        r = await c.patch(
            f"/api/v1/canvas/{canvas.id}/sections/{sec.id}",
            json={
                "patch": {
                    "markdown": viewer_markdown,
                    "sql": "SELECT 1 -- [num:1:%] 字面量保留",
                },
                "expected_rev": 1,
            },
        )
        assert r.status_code == 200, r.text

    stored = next(x for x in await store.list_sections(canvas.id) if x.id == sec.id)
    assert stored.payload["markdown"] == "旧稿 5.2亿元 与。"
    assert "[num:" not in stored.payload["markdown"]
    assert stored.payload["sql"] == "SELECT 1 -- [num:1:%] 字面量保留"


@pytest.mark.integration
async def test_canvas_sources_projection_and_noop_autosave_preserve_canonical_provenance(app_ctx, tmp_path) -> None:
    store: CanvasStore = app_ctx.state.canvas_store
    artifact_store = await _wire_artifact_store(app_ctx, tmp_path)
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(session_id="s_source_projection", workspace_id="w", user_id=uid)
    artifact = await artifact_store.put_bytes(
        content=b"region,revenue\nNorth,42\n",
        filename="revenue.csv",
        mime="text/csv",
        source="bi_query",
        created_by=uid,
        session_id=canvas.session_id,
    )
    canonical = {
        "label": "Data sources",
        "entries": [
            {
                "artifact_id": artifact.id,
                "label": "Revenue",
                "query": "revenue by region",
                "preview_columns": ["region", "revenue"],
                "preview_rows": [{"region": "North", "revenue": "42"}],
            },
            {
                "artifact_id": "art_missing_private",
                "label": "Private customers",
                "query": "select ssn",
                "preview_rows": [{"ssn": "123"}],
            },
        ],
        "source_refs": [artifact.id, "art_missing_private"],
    }
    sec = await store.append_section(canvas_id=canvas.id, kind="sources", payload=canonical)

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get(f"/api/v1/canvas/by-session/{canvas.session_id}")
        assert response.status_code == 200, response.text
        projected = response.json()["sections"][0]
        assert projected["entries"][0]["access_state"] == "linked_accessible"
        assert projected["entries"][0]["safe_label"] == "Revenue"
        assert projected["entries"][1]["access_state"] == "linked_unavailable"
        assert "Private customers" not in repr(projected)
        assert "select ssn" not in repr(projected)
        assert "art_missing_private" not in repr(projected)

        patch = {key: value for key, value in projected.items() if key not in {"id", "kind", "rev", "seq"}}
        saved = await client.patch(
            f"/api/v1/canvas/{canvas.id}/sections/{sec.id}",
            json={"patch": patch, "expected_rev": 1},
        )
        assert saved.status_code == 200, saved.text
        assert "art_missing_private" not in repr(saved.json())

    stored = next(section for section in await store.list_sections(canvas.id) if section.id == sec.id)
    assert stored.payload == canonical


@pytest.mark.integration
async def test_canvas_projected_marker_noop_preserves_canonical_source_identity(app_ctx, tmp_path) -> None:
    store: CanvasStore = app_ctx.state.canvas_store
    artifact_store = await _wire_artifact_store(app_ctx, tmp_path)
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(session_id="s_marker_noop", workspace_id="w", user_id=uid)
    artifact = await artifact_store.put_bytes(
        content=b"value\n42\n",
        filename="value.csv",
        mime="text/csv",
        source="bi_query",
        created_by=uid,
        session_id=canvas.session_id,
    )
    canonical = {
        "markdown": f"Revenue [ref:{artifact.id}]",
        "source_refs": [artifact.id],
    }
    sec = await store.append_section(canvas_id=canvas.id, kind="narrative", payload=canonical)

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        detail = await client.get(f"/api/v1/canvas/{canvas.id}")
        projected = detail.json()["sections"][0]
        assert artifact.id not in projected["markdown"]
        patch = {key: value for key, value in projected.items() if key not in {"id", "kind", "rev", "seq"}}
        saved = await client.patch(
            f"/api/v1/canvas/{canvas.id}/sections/{sec.id}",
            json={"patch": patch, "expected_rev": 1},
        )
        assert saved.status_code == 200, saved.text
        assert artifact.id not in saved.text

    stored = next(section for section in await store.list_sections(canvas.id) if section.id == sec.id)
    assert stored.payload == canonical


@pytest.mark.integration
async def test_canvas_source_patch_allows_accessible_label_only_and_rejects_identity_injection(app_ctx, tmp_path) -> None:
    store: CanvasStore = app_ctx.state.canvas_store
    artifact_store = await _wire_artifact_store(app_ctx, tmp_path)
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(session_id="s_source_patch", workspace_id="w", user_id=uid)
    artifact = await artifact_store.put_bytes(
        content=b"value\n1\n",
        filename="value.csv",
        mime="text/csv",
        source="bi_query",
        created_by=uid,
        session_id=canvas.session_id,
    )
    sec = await store.append_section(
        canvas_id=canvas.id,
        kind="sources",
        payload={
            "entries": [
                {"artifact_id": artifact.id, "label": "Allowed", "query": "safe"},
                {"artifact_id": "art_missing", "label": "Denied", "query": "secret"},
            ],
        },
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        detail = await client.get(f"/api/v1/canvas/{canvas.id}")
        projected = detail.json()["sections"][0]
        projected["entries"][0]["safe_label"] = "Allowed reviewed"
        projected["entries"][1]["safe_label"] = "Attacker overwrite"
        patch = {key: value for key, value in projected.items() if key not in {"id", "kind", "rev", "seq"}}
        edited = await client.patch(
            f"/api/v1/canvas/{canvas.id}/sections/{sec.id}",
            json={"patch": patch, "expected_rev": 1},
        )
        assert edited.status_code == 200, edited.text

        injected = await client.patch(
            f"/api/v1/canvas/{canvas.id}/sections/{sec.id}",
            json={
                "patch": {
                    "entries": [
                        {**projected["entries"][0], "artifact_id": "art_attacker"},
                        projected["entries"][1],
                    ],
                },
                "expected_rev": 2,
            },
        )
        assert injected.status_code == 422
        assert injected.json()["error"]["code"] == "source_identity_injection"

        stale = await client.patch(
            f"/api/v1/canvas/{canvas.id}/sections/{sec.id}",
            json={
                "patch": {
                    "entries": [
                        {**projected["entries"][0], "id": "source-ref-v1:stale", "source_alias": "source-ref-v1:stale"},
                        projected["entries"][1],
                    ],
                },
                "expected_rev": 2,
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"] == {"code": "source_alias_stale", "refresh_required": True}

    stored = next(section for section in await store.list_sections(canvas.id) if section.id == sec.id)
    assert stored.payload["entries"][0]["label"] == "Allowed reviewed"
    assert stored.payload["entries"][0]["artifact_id"] == artifact.id
    assert stored.payload["entries"][1] == {"artifact_id": "art_missing", "label": "Denied", "query": "secret"}


@pytest.mark.integration
async def test_canvas_flatten_design_endpoint_success(app_ctx, tmp_path, monkeypatch) -> None:
    """Design flatten route returns a PNG artifact and the updated design section."""
    monkeypatch.setenv("BOWHEAD_DESIGN_V2A", "1")
    monkeypatch.setenv("BOWHEAD_DESIGN_FONT_ALLOW_FALLBACK", "1")
    store: CanvasStore = app_ctx.state.canvas_store
    artifact_store = await _wire_artifact_store(app_ctx, tmp_path)
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(
        session_id="s_flatten_http",
        workspace_id="w",
        user_id=uid,
    )
    background = await artifact_store.put_bytes(
        content=_png_bytes(),
        filename="bg.png",
        mime="image/png",
        source="image_generate",
        created_by=uid,
        session_id=canvas.session_id,
        kind="tool_data",
        inject="stub",
        producer_kind="image_generate",
    )
    section = await store.append_section(
        canvas_id=canvas.id,
        kind="design",
        payload={
            "title": "Promo",
            "size_preset": "square_1_1",
            "canvas_width": 320,
            "canvas_height": 240,
            "background": {
                "type": "artifact",
                "artifact_id": background.id,
                "fit": "cover",
                "opacity": 1,
            },
            "layers": [
                {
                    "id": "headline",
                    "type": "text",
                    "text": "SUMMER",
                    "x": 0.1,
                    "y": 0.1,
                    "w": 0.8,
                    "h": 0.2,
                    "font_size": 32,
                    "color": "#111111",
                    "align": "center",
                    "z": 10,
                },
            ],
        },
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            f"/api/v1/canvas/{canvas.id}/sections/{section.id}/flatten",
            json={"expected_rev": section.rev},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == f"/api/v1/files/{body['artifact_id']}"
    assert body["section"]["flattened_artifact_id"] == body["artifact_id"]
    assert body["section"]["flattened_source_rev"] == section.rev
    assert body["rev"] == section.rev + 1
    artifact = await artifact_store.get(body["artifact_id"])
    assert artifact is not None
    assert artifact.source == "design_flatten"
    assert artifact.kind == "deliverable"


@pytest.mark.integration
async def test_canvas_flatten_design_endpoint_rev_conflict(app_ctx, tmp_path, monkeypatch) -> None:
    """Stale expected_rev maps to a stable 409 JSON response."""
    monkeypatch.setenv("BOWHEAD_DESIGN_V2A", "1")
    store: CanvasStore = app_ctx.state.canvas_store
    artifact_store = await _wire_artifact_store(app_ctx, tmp_path)
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(
        session_id="s_flatten_http_conflict",
        workspace_id="w",
        user_id=uid,
    )
    background = await artifact_store.put_bytes(
        content=_png_bytes(),
        filename="bg.png",
        mime="image/png",
        source="image_generate",
        created_by=uid,
        session_id=canvas.session_id,
        kind="tool_data",
        inject="stub",
        producer_kind="image_generate",
    )
    section = await store.append_section(
        canvas_id=canvas.id,
        kind="design",
        payload={
            "title": "Promo",
            "size_preset": "square_1_1",
            "canvas_width": 320,
            "canvas_height": 240,
            "background": {
                "type": "artifact",
                "artifact_id": background.id,
            },
            "layers": [],
        },
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            f"/api/v1/canvas/{canvas.id}/sections/{section.id}/flatten",
            json={"expected_rev": section.rev + 1},
        )

    assert r.status_code == 409
    assert r.json() == {
        "error": "rev_conflict",
        "detail": "section revision conflict",
        "current_rev": section.rev,
    }


@pytest.mark.integration
async def test_canvas_flatten_design_endpoint_disabled(app_ctx, tmp_path, monkeypatch) -> None:
    """Feature flag off blocks new design exports without exposing section internals."""
    monkeypatch.setenv("BOWHEAD_DESIGN_V2A", "0")
    await _wire_artifact_store(app_ctx, tmp_path)
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(
        session_id="s_flatten_http_disabled",
        workspace_id="w",
        user_id=uid,
    )
    section = await store.append_section(
        canvas_id=canvas.id,
        kind="design",
        payload={
            "title": "Promo",
            "size_preset": "square_1_1",
            "canvas_width": 320,
            "canvas_height": 240,
            "background": {"type": "artifact", "artifact_id": "art_deadbeef0000"},
            "layers": [],
        },
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            f"/api/v1/canvas/{canvas.id}/sections/{section.id}/flatten",
            json={"expected_rev": section.rev},
        )

    assert r.status_code == 404
    assert r.json() == {
        "error": "design_v2a_disabled",
        "detail": "editable design is disabled",
    }


@pytest.mark.integration
async def test_canvas_split_via_http_with_source_refs(app_ctx) -> None:
    """W12-10 (#81): split via HTTP endpoint, narrative 携带 source_refs.

    citation-audit M3-5 换形态：原场景是 gen2（正文里写着 `[num:…:src:s_001]` /
    `[ref:s_002]`，split 能按「这半段真用了谁」精确过滤）。gen3 正文只有 `[N]` 下标、
    反查不到 id → 契约改为「有编号引用的半段整份继承、无引用的半段干净移除」
    （store 层同款锁 test_split_numbered_cite_inherits_refs 记了这条能力差）。
    本测试守的是 **HTTP 面**：split 端点 200 + 两半 refs 按新契约归属。
    """
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(
        session_id="s_split_http",
        workspace_id="w",
        user_id=uid,
    )
    md = "Q1 GMV 同比 84.9万元 [1] 上升, 主因结构改善 [2].\n\n后半段纯结论, 无引用."
    narr = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": md, "source_refs": ["s_001", "s_002"]},
    )

    # Split at first \n\n boundary (first half includes s_001 + s_002, not s_003).
    boundary = md.find("\n\n") + 2
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            f"/api/v1/canvas/{canvas.id}/sections/{narr.id}/split",
            json={"offset": boundary},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # First half includes [1][2] → inherit all references; second half has none → remove cleanly.
        viewer_refs = body["updated"].get("source_refs") or []
        assert len(viewer_refs) == 2
        assert all(ref.startswith("source-ref-v1:") for ref in viewer_refs)
        assert all(raw not in repr(body) for raw in ("s_001", "s_002"))
        assert "source_refs" not in body["new"]
    stored = next(section for section in await store.list_sections(canvas.id) if section.id == narr.id)
    assert stored.payload["source_refs"] == ["s_001", "s_002"]


@pytest.mark.integration
async def test_canvas_split_boundary_offset_422(app_ctx) -> None:
    """affordance r3 §5.2: 边界 offset 的退化 split（一半为空）→ 422, 不造空壳。"""
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(
        session_id="s_split_http_degenerate",
        workspace_id="w",
        user_id=uid,
    )
    narr = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "abc"},
    )
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        for bad in (0, 3, 999):
            r = await c.post(
                f"/api/v1/canvas/{canvas.id}/sections/{narr.id}/split",
                json={"offset": bad},
            )
            assert r.status_code == 422, (bad, r.text)
    secs = await store.list_sections(canvas.id)
    assert [s.payload["markdown"] for s in secs if s.kind == "narrative"] == ["abc"]


@pytest.mark.integration
async def test_canvas_merge_via_http(app_ctx) -> None:
    """W12-6 (#77): merge_with_prev via HTTP endpoint, atomic patch+delete."""
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(
        session_id="s_merge_http",
        workspace_id="w",
        user_id=uid,
    )
    a = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "段一。", "source_refs": ["s_a"]},
    )
    b = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "段二。", "source_refs": ["s_b"]},
    )
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/canvas/{canvas.id}/sections/{b.id}/merge")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["survivorSectionId"] == a.id
        assert body["deletedSectionId"] == b.id
        assert body["updated"]["markdown"] == "段一。\n\n段二。"
        viewer_refs = body["updated"].get("source_refs", [])
        assert len(viewer_refs) == 2
        assert all(ref.startswith("source-ref-v1:") for ref in viewer_refs)
        assert all(raw not in repr(body) for raw in ("s_a", "s_b"))
    stored = next(section for section in await store.list_sections(canvas.id) if section.id == a.id)
    assert stored.payload["source_refs"] == ["s_a", "s_b"]


@pytest.mark.integration
async def test_canvas_merge_first_section_returns_400(app_ctx) -> None:
    """W12-6 corner: section 是首条 (无 prev) → 400."""
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(
        session_id="s_merge_first",
        workspace_id="w",
        user_id=uid,
    )
    a = await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "only"},
    )
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/canvas/{canvas.id}/sections/{a.id}/merge")
        assert r.status_code == 400


@pytest.mark.integration
async def test_canvas_export_stub(app_ctx) -> None:
    """无 skill stack 配置 → enqueue_export 走 stub 路径，job.status=queued 不前进."""
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(session_id="s2", workspace_id="w", user_id=uid)

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/canvas/{canvas.id}/export", json={"fmt": "pdf"})
        assert r.status_code == 200
        body = r.json()
        assert body["fmt"] == "pdf"
        assert body["status"] == "queued"
        assert body["jobId"].startswith("exp_")
        assert body["contractVersion"] == 1

        history = await c.get(f"/api/v1/canvas/{canvas.id}/exports")
        assert history.status_code == 200
        assert history.json()["items"][0]["contractVersion"] == 1

        # docx **supported** since deliverable-format-fidelity batch 2 (previously used here as a negative example).
        r = await c.post(f"/api/v1/canvas/{canvas.id}/export", json={"fmt": "docx"})
        assert r.status_code == 200
        assert r.json()["fmt"] == "docx"

        # Unsupported fmt still requires 400; use an actually unimplemented format to guard this contract.
        r = await c.post(f"/api/v1/canvas/{canvas.id}/export", json={"fmt": "rtf"})
        assert r.status_code == 400


def test_export_job_dto_uses_persisted_contract_version() -> None:
    job = ExportJob(
        id="exp_v2_dto",
        canvas_id="cv_v2_dto",
        fmt="pdf",
        status="queued",
        contract_version=2,
    )

    assert _job_dto(job).contractVersion == 2


@pytest.mark.integration
@pytest.mark.parametrize(
    (
        "request_body",
        "clone_flag",
        "legacy_clone_flag",
        "expected_status",
        "expected_template_id",
        "expected_clone",
    ),
    [
        pytest.param(
            {"fmt": "pptx", "mode": "default"},
            None,
            None,
            200,
            None,
            False,
            id="default-ignores-saved-template",
        ),
        pytest.param(
            {"fmt": "pptx", "mode": "default", "template_artifact_id": "art_explicit"},
            None,
            None,
            200,
            None,
            False,
            id="default-ignores-explicit-template",
        ),
        pytest.param(
            {"fmt": "pptx", "mode": "auto"},
            None,
            None,
            200,
            None,
            False,
            id="auto-does-not-read-saved-template",
        ),
        pytest.param(
            {"fmt": "pptx", "mode": "auto", "template_artifact_id": "art_explicit"},
            None,
            None,
            422,
            None,
            False,
            id="auto-rejects-explicit-template",
        ),
        pytest.param(
            {"fmt": "pptx", "mode": "clone", "template_artifact_id": "art_explicit"},
            None,
            None,
            422,
            None,
            False,
            id="clone-disabled-by-default",
        ),
        pytest.param(
            {"fmt": "pptx", "mode": "clone", "template_artifact_id": "art_explicit"},
            "1",
            None,
            200,
            "art_explicit",
            True,
            id="clone-enabled-explicitly",
        ),
        pytest.param(
            {"fmt": "pptx", "mode": "clone", "template_artifact_id": "art_explicit"},
            None,
            "1",
            200,
            "art_explicit",
            True,
            id="legacy-emergency-alias-enabled-explicitly",
        ),
        pytest.param(
            {"fmt": "pptx", "mode": "clone", "template_artifact_id": "art_explicit"},
            "0",
            "1",
            422,
            None,
            False,
            id="canonical-off-overrides-legacy-alias",
        ),
        pytest.param(
            {"fmt": "pptx", "mode": "clone"},
            "1",
            None,
            400,
            None,
            False,
            id="clone-requires-explicit-template",
        ),
    ],
)
async def test_canvas_export_pptx_mode_contract(
    app_ctx: Any,
    monkeypatch: pytest.MonkeyPatch,
    request_body: dict[str, str],
    clone_flag: str | None,
    legacy_clone_flag: str | None,
    expected_status: int,
    expected_template_id: str | None,
    expected_clone: bool,
) -> None:
    """W0-A: hidden settings must never change legacy PPTX renderer selection."""

    class _SavedTemplateSettings:
        def __init__(self) -> None:
            self.keys: list[str] = []

        async def get(self, _user_id: str, key: str) -> str:
            self.keys.append(key)
            return "art_saved"

    settings_store = _SavedTemplateSettings()
    app_ctx.state.user_settings_store = settings_store
    app_ctx.state.session_store = object()
    if clone_flag is None:
        monkeypatch.delenv("FEATURE_CANVAS_PPTX_CLONE", raising=False)
    else:
        monkeypatch.setenv("FEATURE_CANVAS_PPTX_CLONE", clone_flag)
    if legacy_clone_flag is None:
        monkeypatch.delenv("BOWHEAD_PPTX_USER_TEMPLATE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("BOWHEAD_PPTX_USER_TEMPLATE_ENABLED", legacy_clone_flag)

    captured: dict[str, object] = {}

    async def _fake_enqueue_export(**kwargs: object) -> ExportJob:
        captured.update(kwargs)
        return ExportJob(
            id="exp_mode_contract",
            canvas_id=str(kwargs["canvas_id"]),
            fmt=str(kwargs["fmt"]),
            status="queued",
        )

    monkeypatch.setattr(canvas_export, "enqueue_export", _fake_enqueue_export)

    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(
        session_id=(
            f"s_mode_{request_body['mode']}_{clone_flag or 'off'}_{legacy_clone_flag or 'off'}_{request_body.get('template_artifact_id', 'none')}"
        ),
        workspace_id="w",
        user_id=uid,
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        response = await c.post(
            f"/api/v1/canvas/{canvas.id}/export",
            json=request_body,
        )

    assert response.status_code == expected_status, response.text
    # Saved PPTX templates (pptx_template_artifact_id) never influence renderer selection. Since 57c06762b, error responses
    # localize using logged-in users' ui_language, so non-200 branches read exactly this setting and no others.
    assert settings_store.keys == ([] if expected_status == 200 else ["ui_language"])
    if expected_status != 200:
        assert captured == {}
        return
    assert captured["template_artifact_id"] == expected_template_id
    assert captured["use_clone_render"] is expected_clone
    assert captured["session_store"] is None


@pytest.mark.integration
async def test_list_reports_returns_first_section_title(app_ctx, tmp_path) -> None:
    """chat-page-polish B1: /api/v1/canvas list 必须返 first_section_title.

    FE 在 canvas.title 为 null 时拿这字段做 fallback, 避免显 raw cv_xxx ID.
    """
    store: CanvasStore = app_ctx.state.canvas_store
    users = UserStore(str(tmp_path / "users.db"))
    await users.initialize()
    team = await users.create_team(name="Acme", max_seats=10)
    user = await users.create_user(
        team_id=team.id,
        email="reports@example.com",
        password="Passw0rd!",
    )
    ws_id = await users.create_local_workspace(
        team_id=team.id,
        name="Sales Reports",
        creator_user_id=user.id,
    )
    app_ctx.state.user_store = users
    canvas_with = await store.ensure_canvas(
        session_id="s_b1_route_a",
        workspace_id=ws_id,
        user_id=user.id,
    )
    await store.append_section(
        canvas_id=canvas_with.id,
        kind="section",
        payload={"h1": "Q1 GMV 同比分析", "label": ""},
        by_turn_id="t_visible_report",
    )
    await store.append_section(
        canvas_id=canvas_with.id,
        kind="chart",
        payload={"title": "仅供聊天内联的隐藏图"},
        by_turn_id="t_hidden_chat",
    )
    await store.mark_turn_hidden_for_chat_mode(
        "t_hidden_chat",
        "declared: chat (agent/user decision)",
    )
    canvas_empty = await store.ensure_canvas(
        session_id="s_b1_route_b",
        workspace_id=ws_id,
        user_id=user.id,
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/canvas", headers={"X-Bowhead-User-Id": user.id})
        assert r.status_code == 200
        items = r.json().get("items", [])
        by_id = {it["id"]: it for it in items}
        assert canvas_with.id in by_id
        assert by_id[canvas_with.id]["first_section_title"] == "Q1 GMV 同比分析"
        assert by_id[canvas_with.id]["sections_count"] == 1
        assert by_id[canvas_with.id]["workspace_name"] == "Sales Reports"
        detail = await c.get(
            f"/api/v1/canvas/by-session/{canvas_with.session_id}",
            headers={"X-Bowhead-User-Id": user.id},
        )
        assert detail.status_code == 200
        assert len(detail.json()["sections"]) == by_id[canvas_with.id]["sections_count"]
        # canvas_empty has no sections → first_section_title is None
        assert canvas_empty.id in by_id
        assert by_id[canvas_empty.id]["first_section_title"] is None
        assert by_id[canvas_empty.id]["sections_count"] == 0

        visible_only = await c.get(
            "/api/v1/canvas?include_empty=false",
            headers={"X-Bowhead-User-Id": user.id},
        )
        assert visible_only.status_code == 200
        visible_ids = {item["id"] for item in visible_only.json()["items"]}
        assert canvas_with.id in visible_ids
        assert canvas_empty.id not in visible_ids


async def test_chart_evidence_read_history_edit_and_revocation_use_current_artifacts(app_ctx, tmp_path) -> None:
    import copy
    from pathlib import Path

    from bowhead_core.chart.presentation_plan import artifact_text_representation, compile_notes_plan
    from bowhead_core.chart.types import ChartColumn

    store = app_ctx.state.canvas_store
    artifacts = await _wire_artifact_store(app_ctx, tmp_path)
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(session_id="s_notes", workspace_id="w", user_id=uid)
    raw = b"value\n12\n"
    data = await artifacts.put_bytes(content=raw, filename="value.csv", mime="text/csv", created_by=uid)
    note = await artifacts.put_bytes(content=b"Revenue includes returns.", filename="definition.txt", mime="text/plain", created_by=uid)
    representation = await artifact_text_representation(note, artifacts)
    assert representation is not None
    root = {
        "kind": "table",
        "title": "Revenue",
        "row_count": 1,
        "data_artifact_id": data.id,
        "columns": [{"key": "value", "header": "Value", "type": "numeric"}],
        "rows": [{"value": 12}],
    }
    verified = [
        {
            "id": "definition",
            "measure_keys": ["value"],
            "excerpt": "Revenue includes returns.",
            "source_ref": {"id": note.id, "artifact_id": note.id, "tool": note.source, "label": note.filename},
            "verified_content_digest": representation[1],
        }
    ]
    root["presentation_plan"] = compile_notes_plan(
        root, verified, artifact_id=data.id, raw=raw, columns=[ChartColumn(key="value", header="Value", type="numeric")]
    )
    section = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": root})
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        detail = (await client.get(f"/api/v1/canvas/{canvas.id}")).json()
        projected = detail["sections"][0]["spec"]
        assert projected["presentation_plan"]["evidence_notes"][0]["availability"] == "available"
        forged = copy.deepcopy(projected)
        forged["presentation_plan"]["evidence_notes"][0]["excerpt"] = "Forged definition"
        response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": forged}, "expected_rev": 1})
        assert response.status_code == 422
        assert len(await store.list_revisions(canvas_id=canvas.id)) == 1
        projected["kind"] = "kpi"
        response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": projected}, "expected_rev": 1})
        assert response.status_code == 200, response.text
        current = (await store.list_sections(canvas.id))[0]
        assert current.payload["spec"]["presentation_plan"] == root["presentation_plan"]
        from types import SimpleNamespace

        from bowhead_gateway.source_access_projection import project_event_payloads_for_viewer, project_live_event_payload_for_viewer

        class EventSessions:
            async def get_turn(self, turn_id):
                return SimpleNamespace(id=turn_id, session_id=canvas.session_id)

            async def get_session(self, session_id):
                return SimpleNamespace(id=session_id, workspace_id=canvas.workspace_id)

        app_ctx.state.session_store = EventSessions()
        event_request = SimpleNamespace(app=app_ctx)
        event_payload = {"canvas_id": canvas.id, "seq": current.seq, "section": current.to_report_section()}
        live = await project_live_event_payload_for_viewer(event_request, "t_notes", "canvas.section_added", event_payload, event_ordinal=1)
        replay = await project_event_payloads_for_viewer(event_request, "t_notes", [("canvas.section_added", event_payload)], event_ordinals=[1])
        assert live == replay[0]
        assert "Revenue includes returns." in repr(live)
        Path(note.path).write_bytes(b"Changed definition")
        stale_replay = await project_event_payloads_for_viewer(
            event_request, "t_notes", [("canvas.section_added", event_payload)], event_ordinals=[1]
        )
        assert "Revenue includes returns." not in repr(stale_replay)

        for suffix in ("", "/revisions"):
            response = await client.get(f"/api/v1/canvas/{canvas.id}{suffix}")
            assert response.status_code == 200, response.text
            assert "Revenue includes returns." not in response.text
            assert "definition.txt" in response.text
        refreshed = (await client.get(f"/api/v1/canvas/{canvas.id}")).json()["sections"][0]["spec"]
        assert refreshed["title"] == ""
        refreshed["show_legend"] = False
        response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": refreshed}, "expected_rev": 2})
        assert response.status_code == 200, response.text
        current = (await store.list_sections(canvas.id))[0]
        assert current.payload["spec"]["title"] == "Revenue"
        assert current.payload["spec"]["presentation_plan"] == root["presentation_plan"]
        stale = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": refreshed}, "expected_rev": 2})
        assert stale.status_code == 409
        assert len(await store.list_revisions(canvas_id=canvas.id)) == 3


@pytest.mark.parametrize("binding_kind", ["entity", "time"])
async def test_linked_chart_bindings_round_trip_through_http_and_current_dataset_projection(app_ctx, tmp_path, binding_kind) -> None:
    import copy
    from pathlib import Path

    from bowhead_core.canvas_tools import _parse_csv_with_types
    from bowhead_core.chart.binding_catalog import build_binding_catalog
    from bowhead_core.chart.presentation_plan import compile_notes_plan

    store = app_ctx.state.canvas_store
    artifacts = await _wire_artifact_store(app_ctx, tmp_path)
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(session_id="s_linked", workspace_id="w", user_id=uid)
    raw = b"region,revenue\nEast,52.12345\nWest,-5\n" if binding_kind == "entity" else b"month,revenue\n2026-01,52.12345\n2026-02,-5\n"
    data = await artifacts.put_bytes(content=raw, filename="regions.csv", mime="text/csv", created_by=uid)
    rows, columns = _parse_csv_with_types(raw)
    bid = next(candidate["id"] for candidate in build_binding_catalog(columns, rows, raw=raw)["candidates"] if candidate["kind"] == binding_kind)
    child = {
        "kind": "bar",
        "title": "Revenue by region",
        "data_artifact_id": data.id,
        "row_count": len(rows),
        "rows": rows,
        "columns": [{"key": column.key, "header": column.header, "type": column.type} for column in columns],
    }
    root = {
        **child,
        "views": [{"id": "primary", "kind": "chart", "spec": child}, {"id": "detail", "kind": "chart", "spec": {**child, "kind": "table"}}],
        "layout": {"type": "split", "children": [{"view": "primary"}, {"view": "detail"}]},
    }
    input_plan = {
        "version": 1,
        "view_bindings": {"primary": bid, "detail": bid},
        "interactions": [{"preset": "linked_focus", "from_view": "primary", "to_views": ["detail"], "binding_id": bid}],
    }
    if binding_kind == "time":
        input_plan["interactions"].append({"preset": "time_window", "view_ids": ["primary", "detail"], "binding_id": bid})
    root["presentation_plan"] = compile_notes_plan(root, [], artifact_id=data.id, raw=raw, columns=columns, rows=rows, input_plan=input_plan)
    section = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": root})
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app_ctx), base_url="http://gw") as client:
        safe = (await client.get(f"/api/v1/canvas/{canvas.id}")).json()["sections"][0]["spec"]
        assert safe["presentation_plan"]["bindings"][bid]["availability"] == "available"
        forged = copy.deepcopy(safe)
        forged["presentation_plan"]["bindings"][bid]["observation_keys"] = ["revenue"]
        response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": forged}, "expected_rev": 1})
        assert response.status_code == 422
        assert len(await store.list_revisions(canvas_id=canvas.id)) == 1
        if binding_kind == "time":
            forged = copy.deepcopy(safe)
            forged["presentation_plan"]["bindings"][bid]["order"] = "numeric"
            response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": forged}, "expected_rev": 1})
            assert response.status_code == 422
        safe["views"][0]["spec"]["kind"] = "hbar"
        safe["layout"] = {"type": "stack", "children": [{"view": "detail"}, {"view": "primary"}]}
        response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": safe}, "expected_rev": 1})
        assert response.status_code == 200, response.text
        current = (await store.list_sections(canvas.id))[0]
        assert current.payload["spec"]["presentation_plan"] == root["presentation_plan"]
        assert current.payload["spec"]["layout"] == safe["layout"]
        assert current.payload["spec"]["views"][1]["id"] == "detail"
        stale_revision = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": safe}, "expected_rev": 1})
        assert stale_revision.status_code == 409
        history = await client.get(f"/api/v1/canvas/{canvas.id}/revisions")
        assert history.status_code == 200
        assert bid in history.text
        Path(data.path).write_bytes(b"region,revenue\nEast,999\n")
        stale = (await client.get(f"/api/v1/canvas/{canvas.id}")).json()["sections"][0]["spec"]
        assert stale["presentation_plan"]["bindings"][bid] == {"kind": binding_kind, "availability": "unavailable"}
        assert stale["views"][0]["spec"]["rows"] == rows
        assert stale["title"] == ""
        stale["show_legend"] = False
        response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": stale}, "expected_rev": 2})
        assert response.status_code == 200, response.text
        assert (await store.list_sections(canvas.id))[0].payload["spec"]["presentation_plan"] == root["presentation_plan"]


async def test_shared_chart_scales_have_protected_http_edits_and_current_projection(app_ctx, tmp_path) -> None:
    import copy
    from pathlib import Path

    from bowhead_core.canvas_tools import _parse_csv_with_types
    from bowhead_core.chart.presentation_plan import compile_notes_plan

    store = app_ctx.state.canvas_store
    artifacts = await _wire_artifact_store(app_ctx, tmp_path)
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(session_id="s_shared_scale", workspace_id="w", user_id=uid)
    raw = b"region,revenue\nEast,52.12345\nWest,-5\n"
    data = await artifacts.put_bytes(content=raw, filename="regions.csv", mime="text/csv", created_by=uid)
    rows, columns = _parse_csv_with_types(raw)
    child = {
        "kind": "bar",
        "title": "Revenue",
        "data_artifact_id": data.id,
        "row_count": 2,
        "rows": rows,
        "columns": [{"key": column.key, "header": column.header, "type": column.type} for column in columns],
    }
    root = {
        **child,
        "views": [{"id": "primary", "kind": "chart", "spec": child}, {"id": "comparison", "kind": "chart", "spec": copy.deepcopy(child)}],
        "layout": {"type": "split", "children": [{"view": "primary"}, {"view": "comparison"}]},
    }
    plan = {"version": 1, "scale_groups": [{"view_ids": ["primary", "comparison"], "axis": "y"}]}
    root["presentation_plan"] = compile_notes_plan(root, [], artifact_id=data.id, raw=raw, columns=columns, rows=rows, input_plan=plan)
    section = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": root})
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app_ctx), base_url="http://gw") as client:
        safe = (await client.get(f"/api/v1/canvas/{canvas.id}")).json()["sections"][0]["spec"]
        groups = safe["presentation_plan"]["scale_groups"]
        for damage in ("domain", "axis", "encoding"):
            forged = copy.deepcopy(safe)
            if damage == "domain":
                forged["presentation_plan"]["scale_groups"][0]["domain"][1] = 100000
            elif damage == "axis":
                forged["presentation_plan"]["scale_groups"][0]["axis"] = "x"
            else:
                forged["views"][0]["spec"]["kind"] = "hbar"
            response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": forged}, "expected_rev": 1})
            assert response.status_code == 422, response.text
        assert len(await store.list_revisions(canvas_id=canvas.id)) == 1
        safe["views"][0]["spec"]["show_data_labels"] = True
        response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": safe}, "expected_rev": 1})
        assert response.status_code == 200, response.text
        assert (await store.list_sections(canvas.id))[0].payload["spec"]["views"][1] == root["views"][1]
        assert (
            await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": safe}, "expected_rev": 1})
        ).status_code == 409
        Path(data.path).write_bytes(b"region,revenue\nEast,999\n")
        stale = (await client.get(f"/api/v1/canvas/{canvas.id}")).json()["sections"][0]["spec"]
        assert stale["presentation_plan"]["dataset"]["availability"] == "unavailable"
        assert stale["presentation_plan"]["scale_groups"] == groups
        assert stale["views"][0]["spec"]["rows"] == rows
        assert (await client.get(f"/api/v1/canvas/{canvas.id}/revisions")).status_code == 200


async def test_chart_reading_steps_have_protected_http_targets_and_redacted_history(app_ctx, tmp_path) -> None:
    import copy
    from pathlib import Path

    from bowhead_core.canvas_tools import _parse_csv_with_types
    from bowhead_core.chart.presentation_plan import compile_notes_plan

    store = app_ctx.state.canvas_store
    artifacts = await _wire_artifact_store(app_ctx, tmp_path)
    uid = app_ctx.state.settings.dev_default_user
    canvas = await store.ensure_canvas(session_id="s_reading_steps", workspace_id="w", user_id=uid)
    raw = b"region,revenue\nEast,52.12345\nWest,-5\n"
    data = await artifacts.put_bytes(content=raw, filename="regions.csv", mime="text/csv", created_by=uid)
    rows, columns = _parse_csv_with_types(raw)
    child = {
        "kind": "bar",
        "title": "Revenue",
        "data_artifact_id": data.id,
        "row_count": 2,
        "rows": rows,
        "columns": [{"key": column.key, "header": column.header, "type": column.type} for column in columns],
    }
    root = {
        **child,
        "views": [
            {"id": "primary", "kind": "chart", "spec": child},
            {"id": "detail", "kind": "chart", "spec": {**copy.deepcopy(child), "kind": "table"}},
        ],
        "layout": {"type": "split", "children": [{"view": "primary"}, {"view": "detail"}]},
    }
    plan = {
        "version": 1,
        "interactions": [{"preset": "guided_steps"}],
        "guide_steps": [{"title": "Read the regional pattern", "view_id": "primary"}, {"title": "Inspect exact observations", "view_id": "detail"}],
    }
    root["presentation_plan"] = compile_notes_plan(root, [], artifact_id=data.id, raw=raw, columns=columns, rows=rows, input_plan=plan)
    section = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": root})
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app_ctx), base_url="http://gw") as client:
        safe = (await client.get(f"/api/v1/canvas/{canvas.id}")).json()["sections"][0]["spec"]
        for damage in ("title", "view", "missing"):
            forged = copy.deepcopy(safe)
            if damage == "title":
                forged["presentation_plan"]["guide_steps"][0]["title"] = "Invented conclusion"
            elif damage == "view":
                forged["presentation_plan"]["guide_steps"][0]["view_id"] = "outside_root"
            else:
                forged["presentation_plan"].pop("guide_steps")
            response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": forged}, "expected_rev": 1})
            assert response.status_code == 422, response.text
        assert len(await store.list_revisions(canvas_id=canvas.id)) == 1
        safe["views"][0]["spec"]["show_data_labels"] = True
        response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": safe}, "expected_rev": 1})
        assert response.status_code == 200, response.text
        assert (await store.list_sections(canvas.id))[0].payload["spec"]["presentation_plan"]["guide_steps"] == plan["guide_steps"]
        Path(data.path).write_bytes(b"region,revenue\nEast,999\n")
        stale = (await client.get(f"/api/v1/canvas/{canvas.id}")).json()["sections"][0]["spec"]
        assert all(step["title"] == "" for step in stale["presentation_plan"]["guide_steps"])
        history = await client.get(f"/api/v1/canvas/{canvas.id}/revisions")
        assert history.status_code == 200
        assert "Read the regional pattern" not in history.text
        stale["show_legend"] = False
        response = await client.patch(f"/api/v1/canvas/{canvas.id}/sections/{section.id}", json={"patch": {"spec": stale}, "expected_rev": 2})
        assert response.status_code == 200, response.text
        assert (await store.list_sections(canvas.id))[0].payload["spec"]["presentation_plan"]["guide_steps"] == plan["guide_steps"]
