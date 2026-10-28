"""V2.9 Ship-prep #1 · HTTP e2e: 模板克隆导出全链路.

跑实际 ASGI HTTP 路径 (不是 mock 模块):
  1. 注册 artifact_store + canvas_store + 合成 .pptx artifact
  2. Seed persisted Canvas sections for the current clone exporter.
  3. POST /api/v1/canvas/{cid}/export?mode=clone&template_artifact_id=...
  4. poll job 状态 → done
  5. 验证输出 artifact 是合法 .pptx + 表 cell 真填了

V2.9 之前所有测试都是 module 级 unit, 这是首次 HTTP-level e2e (CTO review #1).
"""

from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _make_synthetic_template(out: Path) -> None:
    """合成 1-slide .pptx with 1 table (2x2)."""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    blank = prs.slide_layouts[6]
    slide = prs.slides.add_slide(blank)
    tb = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(9), Inches(0.6))
    tb.text_frame.text = "测试章节"
    tbl = slide.shapes.add_table(
        rows=2,
        cols=2,
        left=Inches(0.5),
        top=Inches(1.5),
        width=Inches(8),
        height=Inches(2),
    ).table
    tbl.rows[0].cells[0].text = "区域"
    tbl.rows[0].cells[1].text = "销售额"
    tbl.rows[1].cells[0].text = "(填充)"
    tbl.rows[1].cells[1].text = "(填充)"
    prs.save(str(out))


@pytest.fixture
async def app_ctx(tmp_path: Path):
    """完整 app + ArtifactStore + CanvasStore 接通."""
    from bowhead_core.artifact_store import ArtifactStore
    from bowhead_core.canvas_store import CanvasStore
    from bowhead_core.clarification_store import ClarificationStore
    from bowhead_gateway.main import create_app
    from bowhead_gateway.settings import Settings

    app = create_app()
    app.state.settings = Settings()

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    from bowhead_core.artifact_store import LocalBackend

    backend = LocalBackend(root_dir=artifact_root)
    shared_db = tmp_path / "app.db"
    artifact_store = ArtifactStore(
        db_path=str(shared_db),
        backend=backend,
    )
    await artifact_store.initialize()
    # Export pin validation queries `session_attachments` **on the artifact-store connection**
    # (`export_assets.py` in_session check). Real deployments share artifacts/session tables in one DB,
    # so the table always exists; tests using separate DBs without SessionStore initialization lack it, and export
    # raises `no such table: session_attachments`. Initialize on the **same DB file**.
    from bowhead_core.session_store import SessionStore

    sessions = SessionStore(tmp_path / "art.db")
    await sessions.initialize()
    # Export pins artifacts to sessions (`session_attachments` FK to `sessions(id)`),
    # so sessions used here must actually exist, as in production.
    for sid in ("s_e2e", "s_e2e_cross_user"):
        await sessions.ensure_session(session_id=sid, workspace_id="w", user_id="u_dev_alice")
    app.state.session_store = sessions
    # `canvas_export._db_path` is a **module global**, set in production by lifespan's
    # `init_export_jobs` (main.py:582). These two e2e tests create apps without lifespan,
    # relying on the global accidentally being None; if another same-process test already called
    # `init_export_jobs`（`test_export_copy_contract` / `test_export_operations`
    # both do), this uses another DB path, hitting the legacy/artifact same-DB guard.
    # Explicit initialization matches production and removes test-order dependence.
    from bowhead_core.canvas_export import init_export_jobs

    await init_export_jobs(str(tmp_path / "art.db"))
    app.state.artifact_store = artifact_store

    from bowhead_core.session_store import SessionStore

    session_store = SessionStore(shared_db)
    await session_store.initialize()
    app.state.session_store = session_store

    canvas_store = CanvasStore(db_path=shared_db)
    await canvas_store.initialize()
    app.state.canvas_store = canvas_store
    from bowhead_core.canvas_export import init_export_jobs

    await init_export_jobs(str(shared_db))

    clarify = ClarificationStore(tmp_path / "c.db")
    await clarify.initialize()
    app.state.clarification_store = clarify

    # W11-10b: register SkillCatalog + SkillRuntime so canvas_export uses real path (canvas-pptx skill).
    from bowhead_skill.runtime import SkillRuntime

    from bowhead_skill.catalog import SkillCatalog, builtin_catalog_dir

    catalog = SkillCatalog(roots=[builtin_catalog_dir()])
    catalog.refresh()
    app.state.skill_catalog = catalog
    app.state.skill_runtime = SkillRuntime()

    return app


@pytest.fixture
async def http(app_ctx):
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        yield c


@pytest.mark.asyncio
async def test_template_clone_export_e2e(
    app_ctx: Any,
    http: httpx2.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整 HTTP 路径: 模板 artifact → canvas + sections → POST export?mode=clone
    → poll done → GET artifact → 验 .pptx 真填."""
    artifact_store = app_ctx.state.artifact_store
    canvas_store = app_ctx.state.canvas_store
    monkeypatch.setenv("FEATURE_CANVAS_PPTX_CLONE", "1")

    # 1. Synthesize .pptx template + put_file into artifact_store.
    tpl_path = tmp_path / "tpl.pptx"
    _make_synthetic_template(tpl_path)
    art = await artifact_store.put_file(
        src_path=tpl_path,
        filename="my_template.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        source="user_upload",
        created_by="u_e2e",
    )
    template_artifact_id = art.id

    # 2. Precreate a Canvas section for the current clone exporter.
    await app_ctx.state.session_store.ensure_session(
        session_id="s_e2e",
        workspace_id="ws_e2e",
        user_id="u_e2e",
    )
    canvas = await canvas_store.ensure_canvas(
        session_id="s_e2e",
        workspace_id="ws_e2e",
        user_id="u_e2e",
        title="测试 canvas",
    )
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="breakdown",
        payload={
            "title": "测试章节",
            "chapter_tag": "",
            "data": [{"区域": "华东", "销售额": 100}],
            "narrative": "示例",
            "needs_user_input": False,
        },
        by_turn_id="t_e2e",
    )

    # 3. HTTP POST export (user-id header matches canvas owner).
    hdrs = {"X-Bowhead-User-Id": "u_e2e"}
    r = await http.post(
        f"/api/v1/canvas/{canvas.id}/export",
        json={
            "fmt": "pptx",
            "mode": "clone",
            "template_artifact_id": template_artifact_id,
        },
        headers=hdrs,
    )
    assert r.status_code == 200, f"export 提交失败: {r.text}"
    job = r.json()
    job_id = job["jobId"]
    assert job["status"] in ("queued", "running", "done")

    # 4. Poll until done (5min limit; should actually take <10s).
    final_status = job["status"]
    final_artifact_id = job.get("artifactId")
    for _ in range(30):
        if final_status == "done":
            break
        if final_status == "failed":
            pytest.fail(f"export job failed: {job}")
        await asyncio.sleep(1)
        # W11-10b: user-id header required for _user_id auth (otherwise 403).
        r2 = await http.get(f"/api/v1/canvas/{canvas.id}/export/{job_id}", headers=hdrs)
        if r2.status_code != 200:
            continue
        job = r2.json()
        final_status = job["status"]
        final_artifact_id = job.get("artifactId")

    assert final_status == "done", f"job 没 done: {job}"
    assert final_artifact_id, "done 但没 artifact_id"

    # 5. GET artifact (retrieve .pptx bytes for verification).
    out_art = await artifact_store.get(final_artifact_id)
    assert out_art is not None, "output artifact 不存在"
    assert out_art.filename.endswith(".pptx")

    # 6. Open output .pptx, verify row 1 cell actually filled.
    out_bytes = await artifact_store.open_read(out_art)
    from pptx import Presentation

    prs = Presentation(io.BytesIO(out_bytes))
    slide0 = next(iter(prs.slides))
    tbl = next((s.table for s in slide0.shapes if s.has_table), None)
    assert tbl is not None, "输出 .pptx 没 table"
    assert tbl.rows[1].cells[0].text == "华东", f"row 1 col 0 应 '华东', 实 {tbl.rows[1].cells[0].text!r}"
    assert tbl.rows[1].cells[1].text == "100"


@pytest.mark.asyncio
async def test_template_clone_cross_user_artifact_fails_closed(
    app_ctx: Any,
    http: httpx2.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W0-B: queued clone jobs must never read another user's deck."""
    monkeypatch.setenv("FEATURE_CANVAS_PPTX_CLONE", "1")
    artifact_store = app_ctx.state.artifact_store
    canvas_store = app_ctx.state.canvas_store
    foreign_template = await artifact_store.put_bytes(
        content=b"foreign deck bytes must not be read",
        filename="foreign.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        source="user_upload",
        created_by="u_other",
    )
    canvas = await canvas_store.ensure_canvas(
        session_id="s_e2e_cross_user",
        workspace_id="ws_e2e",
        user_id="u_e2e",
        title="测试 canvas",
    )
    await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"title": "测试章节", "narrative": "正文"},
        by_turn_id="t_e2e_cross_user",
    )
    headers = {"X-Bowhead-User-Id": "u_e2e"}
    response = await http.post(
        f"/api/v1/canvas/{canvas.id}/export",
        json={
            "fmt": "pptx",
            "mode": "clone",
            "template_artifact_id": foreign_template.id,
        },
        headers=headers,
    )
    assert response.status_code == 200
    job = response.json()

    for _ in range(20):
        if job["status"] == "failed":
            break
        await asyncio.sleep(0.05)
        polled = await http.get(
            f"/api/v1/canvas/{canvas.id}/export/{job['jobId']}",
            headers=headers,
        )
        assert polled.status_code == 200
        job = polled.json()

    assert job["status"] == "failed"
    assert job["error"] == "template artifact missing_or_forbidden"
    assert job["artifactId"] is None
