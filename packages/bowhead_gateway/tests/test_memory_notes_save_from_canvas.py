"""V4 Sprint 1 PRD-D-5 · POST /api/v1/memory/notes/save-from-canvas route tests.

Covers:
  - 200 happy path: valid kpi_row section → bullet appended + anchor populated
  - 400 empty / >200 char summary
  - 400 wrong section.kind (narrative / insight_card not in whitelist)
  - 404 canvas owned by different user (opaque)
  - 404 canvas missing
  - 404 section_id missing
  - 503 canvas store not configured
  - 200 chart kind also accepted
  - 200 data_table kind also accepted
  - audit log row written
  - anchor includes verifier_result_hash that matches sha256 of payload
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import aiosqlite
import httpx2
import pytest
from bowhead_core.memory_notes import MemoryNotesStore
from bowhead_core.metrics_log import MetricsLog
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import UserStore, _now
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import CanvasStore, UserMemoryStore

if TYPE_CHECKING:
    from pathlib import Path

    from bowhead_core.canvas_store import Canvas, Section


@pytest.fixture
async def app_ctx(tmp_path: Path):
    db = tmp_path / "t.db"
    mem = UserMemoryStore(db)
    await mem.initialize()
    notes = MemoryNotesStore(db)
    await notes.initialize()
    settings_store = UserSettingsStore(db)
    await settings_store.init()
    metrics = MetricsLog(db)
    await metrics.initialize()
    user_store = UserStore(str(db))
    await user_store.initialize()
    canvas_store = CanvasStore(db)
    await canvas_store.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.user_memory_store = mem
    app.state.memory_notes_store = notes
    app.state.user_settings_store = settings_store
    app.state.metrics_log = metrics
    app.state.user_store = user_store
    app.state.canvas_store = canvas_store
    app.state.orchestrator = None
    # V4 PRD-D-5: kill-switch — tests run with feature on
    app.state.memory_from_canvas_enabled = True
    # V4 Sprint 2 PRD-D-4-1: per-bullet anchors enabled
    from bowhead_core.memory_note_anchors import MemoryNoteAnchorsStore

    anchors = MemoryNoteAnchorsStore(db)
    await anchors.initialize()
    app.state.memory_note_anchors_store = anchors
    app.state.anchor_invalidation_enabled = True
    return app, canvas_store


def _client(app):
    transport = httpx2.ASGITransport(app=app)
    return httpx2.AsyncClient(transport=transport, base_url="http://gw")


async def _seed_canvas(
    cs: CanvasStore,
    *,
    canvas_id: str = "cnv_t1",
    user_id: str = "u_dev_alice",
    section_kind: str = "kpi_row",
    payload: dict | None = None,
    section_id: str = "sec_1",
) -> tuple[Canvas, Section]:
    """Manually insert canvas + section rows (CanvasStore lacks public seed API)."""
    payload = payload or {"label": "Q1 营收", "value": "1200万", "delta": "+200"}
    now = _now()
    async with aiosqlite.connect(cs.db_path) as db:
        await db.execute(
            "INSERT INTO canvases(id, session_id, workspace_id, user_id, title, "
            "status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            (canvas_id, "s1", "ws1", user_id, "Test", now, now),
        )
        await db.execute(
            "INSERT INTO canvas_sections(id, canvas_id, seq, kind, payload, "
            "rev, created_at, updated_at, quarantined, quarantine_reason) "
            "VALUES (?, ?, 0, ?, ?, 1, ?, ?, 0, NULL)",
            (section_id, canvas_id, section_kind, json.dumps(payload), now, now),
        )
        await db.commit()
    canvas = await cs.get_canvas(canvas_id)
    sections = await cs.list_sections(canvas_id)
    return canvas, sections[0]


def _expected_hash(payload: dict) -> str:
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ─── happy paths ─────────────────────────────────────────


@pytest.mark.integration
async def test_save_from_canvas_200_kpi_row(app_ctx):
    app, cs = app_ctx
    payload = {"label": "Q1 营收", "value": "1200万"}
    await _seed_canvas(cs, section_kind="kpi_row", payload=payload)
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": "cnv_t1",
                "section_id": "sec_1",
                "summary_text": "Q1 营收达到 1200万",
            },
        )
    assert r.status_code == 200, r.text
    d = r.json()
    assert "Q1 营收达到 1200万" in d["content"]
    # V4 Sprint 2: anchor stored in bullet_anchors[] sidecar (not row-level)
    anchors = d["bullet_anchors"]
    assert len(anchors) == 1
    a0 = anchors[0]
    assert a0["bullet_text"] == "Q1 营收达到 1200万"
    assert a0["canvas_id"] == "cnv_t1"
    assert a0["section_id"] == "sec_1"
    assert a0["verifier_result_hash"] == _expected_hash(payload)
    assert a0["status"] == "valid"
    assert "saved_at" in a0


@pytest.mark.integration
async def test_save_from_canvas_200_chart(app_ctx):
    app, cs = app_ctx
    await _seed_canvas(cs, section_kind="chart", payload={"type": "bar", "data": []})
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": "cnv_t1",
                "section_id": "sec_1",
                "summary_text": "趋势看到回升",
            },
        )
    assert r.status_code == 200


@pytest.mark.integration
async def test_save_from_canvas_200_data_table(app_ctx):
    app, cs = app_ctx
    await _seed_canvas(cs, section_kind="data_table", payload={"rows": [[1]]})
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": "cnv_t1",
                "section_id": "sec_1",
                "summary_text": "数据看到 X",
            },
        )
    assert r.status_code == 200


# ─── validation 400 ──────────────────────────────────────


@pytest.mark.integration
async def test_400_empty_summary(app_ctx):
    app, cs = app_ctx
    await _seed_canvas(cs)
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={"canvas_id": "cnv_t1", "section_id": "sec_1", "summary_text": "   "},
        )
    assert r.status_code == 400


@pytest.mark.integration
async def test_400_oversize_summary(app_ctx):
    app, cs = app_ctx
    await _seed_canvas(cs)
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": "cnv_t1",
                "section_id": "sec_1",
                "summary_text": "x" * 201,
            },
        )
    assert r.status_code == 400


@pytest.mark.integration
async def test_400_kind_not_in_whitelist(app_ctx):
    """narrative is LLM-text, not verified-fact."""
    app, cs = app_ctx
    await _seed_canvas(cs, section_kind="narrative", payload={"md": "blah"})
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": "cnv_t1",
                "section_id": "sec_1",
                "summary_text": "fact",
            },
        )
    assert r.status_code == 400
    assert "verified-fact" in r.text or "whitelist" in r.text


@pytest.mark.integration
async def test_400_kind_kpi_old_name(app_ctx):
    """Spike confirmed real kind is 'kpi_row' not 'kpi'. 'kpi' alone must NOT be whitelisted."""
    app, cs = app_ctx
    await _seed_canvas(cs, section_kind="kpi", payload={})
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": "cnv_t1",
                "section_id": "sec_1",
                "summary_text": "fact",
            },
        )
    assert r.status_code == 400


# ─── 404 not-found / cross-user ──────────────────────────


@pytest.mark.integration
async def test_404_canvas_not_owned_by_user(app_ctx):
    app, cs = app_ctx
    # Canvas owned by a different user
    await _seed_canvas(cs, user_id="u_other")
    async with _client(app) as c:
        # default test user is u_dev_alice — won't match u_other
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": "cnv_t1",
                "section_id": "sec_1",
                "summary_text": "fact",
            },
        )
    assert r.status_code == 404


@pytest.mark.integration
async def test_404_canvas_missing(app_ctx):
    app, _cs = app_ctx
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": "cnv_unknown",
                "section_id": "sec_1",
                "summary_text": "fact",
            },
        )
    assert r.status_code == 404


@pytest.mark.integration
async def test_404_section_missing(app_ctx):
    app, cs = app_ctx
    await _seed_canvas(cs)
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": "cnv_t1",
                "section_id": "sec_nope",
                "summary_text": "fact",
            },
        )
    assert r.status_code == 404


# ─── infra error ─────────────────────────────────────────


@pytest.mark.integration
async def test_503_when_canvas_store_missing(app_ctx):
    app, _cs = app_ctx
    app.state.canvas_store = None
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": "cnv_t1",
                "section_id": "sec_1",
                "summary_text": "fact",
            },
        )
    assert r.status_code == 503


@pytest.mark.integration
async def test_503_when_kill_switch_disabled(app_ctx):
    """V4 PRD-D-5 §7: BOWHEAD_MEMORY_FROM_CANVAS_ENABLED=0 → 503 (instant kill)."""
    app, cs = app_ctx
    await _seed_canvas(cs)
    app.state.memory_from_canvas_enabled = False
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": "cnv_t1",
                "section_id": "sec_1",
                "summary_text": "fact",
            },
        )
    assert r.status_code == 503


# ─── audit ───────────────────────────────────────────────


@pytest.mark.integration
async def test_audit_row_written(app_ctx):
    app, cs = app_ctx
    await _seed_canvas(cs)
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": "cnv_t1",
                "section_id": "sec_1",
                "summary_text": "audited fact",
            },
        )
    assert r.status_code == 200
    user_store: UserStore = app.state.user_store
    async with aiosqlite.connect(user_store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT event_type, payload_json FROM user_audit_log WHERE event_type='memory.notes.save_from_canvas'",
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    p = json.loads(row["payload_json"] or "{}")
    assert p["canvas_id"] == "cnv_t1"
    assert p["section_id"] == "sec_1"
    assert p["section_kind"] == "kpi_row"
    assert "verifier_result_hash" in p
