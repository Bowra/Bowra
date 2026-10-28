"""V4 Sprint 2 PRD-D-4-1 · GET /api/v1/memory/notes per-bullet anchor + read-time recompute.

Covers:
  - bullet_anchors empty when kill switch off
  - bullet_anchors empty when no anchors saved
  - bullet_anchors populated after save-from-canvas
  - read-time recompute: canvas section payload changed → status='stale'
  - read-time recompute: canvas not found → status='stale'
  - cap exceeded (50) → 413 on save
  - DELETE /bullet cascades sidecar anchor removal
  - dedup: same bullet_text saved twice → single sidecar row
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import aiosqlite
import httpx2
import pytest
from bowhead_core.memory_note_anchors import MemoryNoteAnchorsStore
from bowhead_core.memory_notes import MemoryNotesStore
from bowhead_core.metrics_log import MetricsLog
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import UserStore, _now
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import CanvasStore, UserMemoryStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def app_ctx(tmp_path: Path):
    db = tmp_path / "t.db"
    mem = UserMemoryStore(db)
    await mem.initialize()
    notes = MemoryNotesStore(db)
    await notes.initialize()
    anchors = MemoryNoteAnchorsStore(db)
    await anchors.initialize()
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
    app.state.memory_note_anchors_store = anchors
    app.state.user_settings_store = settings_store
    app.state.metrics_log = metrics
    app.state.user_store = user_store
    app.state.canvas_store = canvas_store
    app.state.orchestrator = None
    app.state.memory_from_canvas_enabled = True
    app.state.anchor_invalidation_enabled = True
    return app, canvas_store, anchors, notes


def _client(app):
    transport = httpx2.ASGITransport(app=app)
    return httpx2.AsyncClient(transport=transport, base_url="http://gw")


async def _seed(cs: CanvasStore, *, payload: dict, kind: str = "kpi_row"):
    now = _now()
    async with aiosqlite.connect(cs.db_path) as db:
        await db.execute(
            "INSERT INTO canvases(id, session_id, workspace_id, user_id, title, "
            "status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            ("cnv_t1", "s1", "ws1", "u_dev_alice", "T", now, now),
        )
        await db.execute(
            "INSERT INTO canvas_sections(id, canvas_id, seq, kind, payload, "
            "rev, created_at, updated_at, quarantined, quarantine_reason) "
            "VALUES (?, ?, 0, ?, ?, 1, ?, ?, 0, NULL)",
            ("sec_1", "cnv_t1", kind, json.dumps(payload), now, now),
        )
        await db.commit()


def _expected_hash(payload: dict) -> str:
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ─── kill switch ─────────────────────────────────────────


@pytest.mark.integration
async def test_get_notes_no_anchors_when_kill_switch_off(app_ctx):
    app, _cs, anchors, _n = app_ctx
    app.state.anchor_invalidation_enabled = False
    await anchors.insert_or_revive(
        user_id="u_dev_alice",
        bullet_text="x",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h",
    )
    async with _client(app) as c:
        r = await c.get("/api/v1/memory/notes")
    assert r.status_code == 200
    assert r.json()["bullet_anchors"] == []


@pytest.mark.integration
async def test_get_notes_empty_anchors_when_no_data(app_ctx):
    app, *_ = app_ctx
    async with _client(app) as c:
        r = await c.get("/api/v1/memory/notes")
    assert r.status_code == 200
    assert r.json()["bullet_anchors"] == []


# ─── happy path ──────────────────────────────────────────


@pytest.mark.integration
async def test_save_from_canvas_populates_bullet_anchors(app_ctx):
    app, cs, _anchors, _ = app_ctx
    payload = {"label": "Q1", "value": "100"}
    await _seed(cs, payload=payload)
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": "cnv_t1",
                "section_id": "sec_1",
                "summary_text": "fact A",
            },
        )
    assert r.status_code == 200
    a = r.json()["bullet_anchors"]
    assert len(a) == 1
    assert a[0]["bullet_text"] == "fact A"
    assert a[0]["status"] == "valid"
    assert a[0]["verifier_result_hash"] == _expected_hash(payload)


@pytest.mark.integration
async def test_save_from_canvas_dedups_same_bullet(app_ctx):
    app, cs, _anchors, _ = app_ctx
    await _seed(cs, payload={"label": "Q1"})
    async with _client(app) as c:
        await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={"canvas_id": "cnv_t1", "section_id": "sec_1", "summary_text": "fact"},
        )
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={"canvas_id": "cnv_t1", "section_id": "sec_1", "summary_text": "  FACT  "},  # case + whitespace differ
        )
    assert r.status_code == 200
    # Should still be 1 anchor (dedup via norm)
    a = r.json()["bullet_anchors"]
    assert len(a) == 1


# ─── read-time recompute ─────────────────────────────────


@pytest.mark.integration
async def test_read_time_recompute_detects_payload_change(app_ctx):
    app, cs, anchors, _ = app_ctx
    old_payload = {"label": "Q1", "value": "100"}
    await _seed(cs, payload=old_payload)
    # Save anchor based on old payload
    await anchors.insert_or_revive(
        user_id="u_dev_alice",
        bullet_text="fact",
        canvas_id="cnv_t1",
        section_id="sec_1",
        verifier_result_hash=_expected_hash(old_payload),
    )
    # Add the bullet to notes (so DTO renders)
    notes = app.state.memory_notes_store
    await notes.upsert_content(user_id="u_dev_alice", content="📌 fact")
    # Patch the section payload — bypass canvas_store hook (just direct DB write
    # to simulate "cascade missed it" for runtime fallback)
    async with aiosqlite.connect(cs.db_path) as db:
        await db.execute(
            "UPDATE canvas_sections SET payload=? WHERE id='sec_1'",
            (json.dumps({"label": "Q1", "value": "999"}),),
        )
        await db.commit()
    async with _client(app) as c:
        r = await c.get("/api/v1/memory/notes")
    a = r.json()["bullet_anchors"]
    assert len(a) == 1
    assert a[0]["status"] == "stale"
    assert a[0]["invalidation_reason"] == "payload_changed_runtime"


@pytest.mark.integration
async def test_read_time_recompute_detects_canvas_missing(app_ctx):
    app, _cs, anchors, _ = app_ctx
    await anchors.insert_or_revive(
        user_id="u_dev_alice",
        bullet_text="fact",
        canvas_id="cnv_ghost",
        section_id="sec_x",
        verifier_result_hash="h",
    )
    notes = app.state.memory_notes_store
    await notes.upsert_content(user_id="u_dev_alice", content="📌 fact")
    async with _client(app) as c:
        r = await c.get("/api/v1/memory/notes")
    a = r.json()["bullet_anchors"]
    assert len(a) == 1
    assert a[0]["status"] == "stale"
    assert a[0]["invalidation_reason"] == "canvas_deleted_runtime"


# ─── cap ─────────────────────────────────────────────────


@pytest.mark.integration
async def test_save_returns_413_on_cap_exceeded(app_ctx, monkeypatch):
    app, cs, _anchors, _ = app_ctx
    monkeypatch.setattr(
        "bowhead_core.memory_note_anchors.MAX_ANCHORS_PER_USER",
        1,
    )
    await _seed(cs, payload={"v": 1})
    async with _client(app) as c:
        r1 = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={"canvas_id": "cnv_t1", "section_id": "sec_1", "summary_text": "first"},
        )
        assert r1.status_code == 200
        # Add a second canvas+section to attempt second save
        async with aiosqlite.connect(cs.db_path) as db:
            await db.execute(
                "INSERT INTO canvas_sections(id, canvas_id, seq, kind, payload, "
                "rev, created_at, updated_at, quarantined, quarantine_reason) "
                "VALUES (?, ?, 1, 'kpi_row', ?, 1, ?, ?, 0, NULL)",
                ("sec_2", "cnv_t1", json.dumps({"v": 2}), _now(), _now()),
            )
            await db.commit()
        r2 = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={"canvas_id": "cnv_t1", "section_id": "sec_2", "summary_text": "second"},
        )
    assert r2.status_code == 413


# ─── DELETE bullet cascade ───────────────────────────────


@pytest.mark.integration
async def test_delete_bullet_cascades_sidecar(app_ctx):
    app, cs, anchors, _notes = app_ctx
    await _seed(cs, payload={"v": 1})
    async with _client(app) as c:
        await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={"canvas_id": "cnv_t1", "section_id": "sec_1", "summary_text": "fact A"},
        )
        # Verify anchor exists
        rows = await anchors.list_for_user("u_dev_alice")
        assert len(rows) == 1
        note = (await c.get("/api/v1/memory/notes")).json()
        lease = (
            await c.post(
                "/api/v1/memory/notes/lock",
                json={"expected_version": note["version"]},
            )
        ).json()
        # Delete bullet via route (bullet_index=0)
        r = await c.delete(
            "/api/v1/memory/notes/bullet?bullet_index=0",
            headers={
                "If-Match": f'"{lease["version"]}"',
                "X-Memory-Edit-Token": lease["edit_token"],
            },
        )
        assert r.status_code == 200
        # Anchor cascaded
        rows = await anchors.list_for_user("u_dev_alice")
        assert rows == []
