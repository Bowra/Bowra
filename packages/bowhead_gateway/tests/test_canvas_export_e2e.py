"""Canvas export 端到端：seed canvas → POST export → poll → GET download."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import ArtifactStore, CanvasStore, LocalBackend
from bowhead_skill import SkillRuntime, default_catalog

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def app_ctx(tmp_path: Path):
    db = tmp_path / "test.db"
    art_root = tmp_path / "artifacts"
    canvas = CanvasStore(db)
    await canvas.initialize()
    art = ArtifactStore(db_path=str(db), backend=LocalBackend(root_dir=art_root))
    await art.initialize()
    # Export pin validation queries session_attachments **through the artifact-store connection**
    # (export_assets.py in_session check). Production artifacts/sessions share one database, so this table
    # exists; independent test databases without SessionStore initialization lack only that table, causing export
    # no such table: session_attachments. Initialize in the **same database file**.
    from bowhead_core.session_store import SessionStore

    sessions = SessionStore(db)
    await sessions.initialize()
    # canvas_export._db_path is **module-global**, set by production lifespan's
    # init_export_jobs (main.py:582). These e2e apps skip lifespan,
    # relying on the global happening to be None. If earlier same-process tests call
    # `init_export_jobs`（`test_export_copy_contract` / `test_export_operations`
    # both do), this picks their database path, violating the legacy/artifact shared-database guard.
    # Explicit initialization matches production and removes test-order dependency.
    from bowhead_core.canvas_export import init_export_jobs

    await init_export_jobs(str(db))

    cat = default_catalog()
    cat.refresh()
    rt = SkillRuntime()

    app = create_app()
    app.state.settings = Settings()
    app.state.canvas_store = canvas
    app.state.artifact_store = art
    app.state.session_store = sessions
    app.state.skill_catalog = cat
    app.state.skill_runtime = rt
    app.state.orchestrator = None
    app.state.datarc = None
    return app


@pytest.mark.integration
async def test_export_pdf_e2e(app_ctx) -> None:
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    # Exports pin artifacts to sessions (session_attachments FK to sessions(id)),
    # so the canvas session must actually exist, as it does in production.
    await app_ctx.state.session_store.ensure_session(
        session_id="sX",
        workspace_id="w",
        user_id=uid,
    )
    canvas = await store.ensure_canvas(session_id="sX", workspace_id="w", user_id=uid)
    await store.append_section(canvas_id=canvas.id, kind="eyebrow", payload={"text": "YTD"})
    await store.append_section(canvas_id=canvas.id, kind="h1", payload={"text": "Q1 Demo"})
    await store.append_section(
        canvas_id=canvas.id,
        kind="kpi_row",
        payload={"cells": [{"lbl": "GMV", "val": "29.3 亿"}]},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        # enqueue
        r = await c.post(f"/api/v1/canvas/{canvas.id}/export", json={"fmt": "pdf"})
        assert r.status_code == 200
        job = r.json()
        assert job["status"] in ("queued", "running")
        jid = job["jobId"]

        # poll (<= 10s)
        for _ in range(30):
            await asyncio.sleep(0.3)
            r = await c.get(f"/api/v1/canvas/{canvas.id}/export/{jid}")
            assert r.status_code == 200
            state = r.json()
            if state["status"] in ("done", "failed"):
                break
        assert state["status"] == "done", f"export failed: {state}"
        assert state["url"]
        assert state["artifactId"]

        # download
        r = await c.get(state["url"])
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/pdf")
        assert len(r.content) > 500  # real PDF body
        assert r.content[:4] == b"%PDF"


@pytest.mark.integration
async def test_export_xlsx_e2e(app_ctx) -> None:
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    await app_ctx.state.session_store.ensure_session(
        session_id="sY",
        workspace_id="w",
        user_id=uid,
    )
    canvas = await store.ensure_canvas(session_id="sY", workspace_id="w", user_id=uid)
    await store.append_section(canvas_id=canvas.id, kind="h1", payload={"text": "XLSX test"})
    await store.append_section(
        canvas_id=canvas.id,
        kind="data_table",
        payload={"caption": "x", "columns": [{"key": "a", "header": "A"}], "rows": [{"a": 1}, {"a": 2}]},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/canvas/{canvas.id}/export", json={"fmt": "xlsx"})
        jid = r.json()["jobId"]
        for _ in range(30):
            await asyncio.sleep(0.3)
            r = await c.get(f"/api/v1/canvas/{canvas.id}/export/{jid}")
            state = r.json()
            if state["status"] in ("done", "failed"):
                break
        assert state["status"] == "done", f"failed: {state}"
        r = await c.get(state["url"])
        assert r.status_code == 200
        # XLSX = ZIP container, first 4 bytes PK\x03\x04.
        assert r.content[:2] == b"PK"


@pytest.mark.integration
async def test_export_empty_canvas_fails(app_ctx) -> None:
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    await app_ctx.state.session_store.ensure_session(
        session_id="sZ",
        workspace_id="w",
        user_id=uid,
    )
    canvas = await store.ensure_canvas(session_id="sZ", workspace_id="w", user_id=uid)

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/canvas/{canvas.id}/export", json={"fmt": "pdf"})
        jid = r.json()["jobId"]
        for _ in range(30):
            await asyncio.sleep(0.2)
            r = await c.get(f"/api/v1/canvas/{canvas.id}/export/{jid}")
            if r.json()["status"] in ("done", "failed"):
                break
        assert r.json()["status"] == "failed"
        assert "empty" in r.json()["error"].lower()


@pytest.mark.integration
async def test_legacy_export_never_claims_clean(app_ctx) -> None:
    """0-A：legacy 产物不许自称 clean（真 prod 的 30 张图全变浮点数表，job 却写着
    quality_status='clean', warning_count=0）。

    ⚠ 这条必须走**真导出链路**：把 `_persist_legacy_export_pair` 里的两处写回
    'clean' 后，`pytest -k export` 288 条全绿——原来的单测直接调函数，从没穿过闸
    （对抗性 review 的 positive control 实证）。
    """
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    await app_ctx.state.session_store.ensure_session(session_id="sQ", workspace_id="w", user_id=uid)
    canvas = await store.ensure_canvas(session_id="sQ", workspace_id="w", user_id=uid)
    await store.append_section(canvas_id=canvas.id, kind="h1", payload={"text": "Legacy"})

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/canvas/{canvas.id}/export", json={"fmt": "pdf"})
        assert r.status_code == 200
        jid = r.json()["jobId"]
        for _ in range(30):
            await asyncio.sleep(0.3)
            state = (await c.get(f"/api/v1/canvas/{canvas.id}/export/{jid}")).json()
            if state["status"] in ("done", "failed"):
                break
        assert state["status"] == "done", state

    from bowhead_core import canvas_export as ce

    job = await ce.get_job(jid)
    assert job is not None
    # Legacy output (contract_version not 3) lacks V2 visual fidelity; never label it clean.
    assert job.quality_status == "warnings", f"legacy 产物自称 {job.quality_status}"
    assert job.warning_count >= 1


@pytest.mark.integration
async def test_export_payload_reorders_sections_through_production_path(app_ctx, monkeypatch) -> None:
    """0-C leak#3：章节序兜底必须真的接在导出链上。

    ⚠ 原单测直接调 `ce._canonical_export_order(raw)`，把生产调用点整个删掉后
    `pytest -k "export or canvas"` 751 条全绿（review 实证）。这里改为拦截真正
    送进渲染脚本的 payload，断言 h1 排在 peer 期落库的 code_block 之前。
    """
    # The canonical-order fallback applies only when stored seq is not authoritative (explicit 0).
    monkeypatch.setenv("FEATURE_REPORT_EDIT_SYNC_V2", "0")
    store: CanvasStore = app_ctx.state.canvas_store
    uid = app_ctx.state.settings.dev_default_user
    await app_ctx.state.session_store.ensure_session(session_id="sR", workspace_id="w", user_id=uid)
    canvas = await store.ensure_canvas(session_id="sR", workspace_id="w", user_id=uid)
    # Actual persistence order: peer code_block first, writer header afterward.
    await store.append_section(canvas_id=canvas.id, kind="code_block", payload={"lang": "python", "code": "print(1)"})
    await store.append_section(canvas_id=canvas.id, kind="h1", payload={"text": "标题应在最前"})

    seen: list[list[str]] = []
    from bowhead_core import canvas_export as ce

    original = ce._static_safe_legacy_sections

    def _capture(value, *, locale):  # type: ignore[no-untyped-def]
        result = original(value, locale=locale)
        seen.append([str(s.get("kind")) for s in result])
        return result

    monkeypatch.setattr(ce, "_static_safe_legacy_sections", _capture)

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/canvas/{canvas.id}/export", json={"fmt": "pdf"})
        jid = r.json()["jobId"]
        for _ in range(30):
            await asyncio.sleep(0.3)
            state = (await c.get(f"/api/v1/canvas/{canvas.id}/export/{jid}")).json()
            if state["status"] in ("done", "failed"):
                break

    assert seen, "没截到送进渲染的 sections —— 这条闸会假绿"
    kinds = seen[-1]
    assert kinds.index("h1") < kinds.index("code_block"), f"导出 payload 未按展示规范序重排（真 prod 症状：PDF 标题在第 3 页）：{kinds}"
