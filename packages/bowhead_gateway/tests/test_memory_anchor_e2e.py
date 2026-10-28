"""V4 Sprint 2 PRD-D-4-1 · End-to-end cross-sprint integration tests.

Verifies the full Sprint 1 → Sprint 2 flow:
  1. save-from-canvas creates bullet + sidecar anchor
  2. delete canvas (via canvas_store) → anchor goes stale via cascade hook
  3. memory page (GET /notes) shows bullet with stale anchor + correct reason
  4. patch canvas section → anchor goes stale (payload_changed)
  5. cross-user / cross-canvas isolation

Also verifies the cascade audit log row is written on canvas mutation.
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
from bowhead_core.user_store import UserStore
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

    # Wire canvas_store cascade hook (mimic main.py wiring)
    async def _hook(event_kind, canvas_id, section_id, new_payload):
        n = 0
        if event_kind == "canvas_deleted":
            n = await anchors.invalidate_by_canvas(
                canvas_id=canvas_id,
                reason="canvas_deleted",
            )
        elif event_kind == "section_deleted":
            n = await anchors.invalidate_by_section(
                canvas_id=canvas_id,
                section_id=section_id,
                reason="section_deleted",
            )
        elif event_kind == "payload_changed":
            n = await anchors.recheck_hash(
                canvas_id=canvas_id,
                section_id=section_id,
                new_payload=new_payload or {},
            )
        if n > 0:
            await user_store.log_audit(
                user_id=None,
                team_id=None,
                event_type=f"memory.anchor.invalidated_by_{event_kind.split('_')[0]}",
                payload={
                    "canvas_id": canvas_id,
                    "section_id": section_id,
                    "affected_count": n,
                },
            )

    canvas_store._anchor_invalidation_hook = _hook

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
    return app, canvas_store, anchors


def _client(app):
    transport = httpx2.ASGITransport(app=app)
    return httpx2.AsyncClient(transport=transport, base_url="http://gw")


async def _seed_canvas(cs: CanvasStore, *, canvas_id: str, payload: dict):
    """Seed canvas + 1 kpi_row section."""
    canvas = await cs.ensure_canvas(
        session_id=f"s_{canvas_id}",
        workspace_id="ws",
        user_id="u_dev_alice",
        title=f"T_{canvas_id}",
    )
    section = await cs.append_section(
        canvas_id=canvas.id,
        kind="kpi_row",
        payload=payload,
    )
    return canvas, section


def _expected_hash(payload: dict) -> str:
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ─── full flow: save → delete → stale ────────────────────


@pytest.mark.integration
async def test_save_then_delete_canvas_makes_anchor_stale(app_ctx):
    app, cs, _anchors = app_ctx
    canvas, section = await _seed_canvas(cs, canvas_id="cnv1", payload={"v": 1})
    async with _client(app) as c:
        # save-from-canvas
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": canvas.id,
                "section_id": section.id,
                "summary_text": "Q1 fact",
            },
        )
        assert r.status_code == 200
        # Verify valid anchor in initial GET
        r1 = await c.get("/api/v1/memory/notes")
        a1 = r1.json()["bullet_anchors"]
        assert len(a1) == 1
        assert a1[0]["status"] == "valid"

        # Delete the canvas (triggers cascade hook)
        await cs.delete_canvas(canvas_id=canvas.id)

        # Re-fetch — anchor now stale
        r2 = await c.get("/api/v1/memory/notes")
        a2 = r2.json()["bullet_anchors"]
        assert len(a2) == 1
        assert a2[0]["status"] == "stale"
        # cascade reason 'canvas_deleted' OR runtime fallback 'canvas_deleted_runtime'
        assert "canvas_deleted" in (a2[0]["invalidation_reason"] or "")


@pytest.mark.integration
async def test_save_then_delete_section_makes_anchor_stale(app_ctx):
    app, cs, _anchors = app_ctx
    canvas, section = await _seed_canvas(cs, canvas_id="cnv1", payload={"v": 1})
    async with _client(app) as c:
        await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": canvas.id,
                "section_id": section.id,
                "summary_text": "fact",
            },
        )
        # Delete just the section
        await cs.delete_section(section_id=section.id)

        r = await c.get("/api/v1/memory/notes")
        a = r.json()["bullet_anchors"]
        assert len(a) == 1
        assert a[0]["status"] == "stale"
        assert a[0]["invalidation_reason"] in (
            "section_deleted",
            "canvas_deleted_runtime",  # depends on whether canvas still exists
        )


@pytest.mark.integration
async def test_patch_section_payload_makes_anchor_stale(app_ctx):
    app, cs, _anchors = app_ctx
    canvas, section = await _seed_canvas(cs, canvas_id="cnv1", payload={"v": 1})
    async with _client(app) as c:
        await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": canvas.id,
                "section_id": section.id,
                "summary_text": "fact",
            },
        )
        # Patch payload — different hash
        await cs.patch_section(section_id=section.id, patch={"v": 2})

        r = await c.get("/api/v1/memory/notes")
        a = r.json()["bullet_anchors"]
        assert a[0]["status"] == "stale"
        assert a[0]["invalidation_reason"] in (
            "payload_changed",
            "payload_changed_runtime",
        )


@pytest.mark.integration
async def test_cascade_writes_audit_log(app_ctx):
    app, cs, _anchors = app_ctx
    canvas, section = await _seed_canvas(cs, canvas_id="cnv1", payload={"v": 1})
    async with _client(app) as c:
        await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": canvas.id,
                "section_id": section.id,
                "summary_text": "fact",
            },
        )
        await cs.delete_canvas(canvas_id=canvas.id)

    user_store: UserStore = app.state.user_store
    async with aiosqlite.connect(user_store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT event_type, payload_json FROM user_audit_log WHERE event_type LIKE 'memory.anchor.invalidated_by_%' ORDER BY created_at",
        ) as cur:
            rows = await cur.fetchall()
    assert len(rows) >= 1
    e0 = rows[0]
    assert "invalidated_by" in e0["event_type"]
    p = json.loads(e0["payload_json"])
    assert p["canvas_id"] == canvas.id
    assert p["affected_count"] >= 1


# ─── reverify (stale → valid) ────────────────────────────


@pytest.mark.integration
async def test_reverify_stale_anchor_via_save_again(app_ctx):
    """User-flow: anchor stale → user clicks 重新 verify (which re-calls
    save-from-canvas) → anchor turns valid with new hash."""
    app, cs, _anchors = app_ctx
    canvas, section = await _seed_canvas(cs, canvas_id="cnv1", payload={"v": 1})
    async with _client(app) as c:
        # 1) save
        await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": canvas.id,
                "section_id": section.id,
                "summary_text": "fact",
            },
        )
        # 2) patch payload — anchor goes stale
        await cs.patch_section(section_id=section.id, patch={"v": 2})
        # 3) re-save (frontend "重新 verify" button does this) → revive anchor
        r = await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": canvas.id,
                "section_id": section.id,
                "summary_text": "fact",
            },
        )
        assert r.status_code == 200
        a = r.json()["bullet_anchors"]
        assert len(a) == 1  # dedup, still single anchor
        assert a[0]["status"] == "valid"
        assert a[0]["verifier_result_hash"] == _expected_hash({"v": 2})


# ─── cross-user isolation ────────────────────────────────


@pytest.mark.integration
async def test_canvas_delete_does_not_invalidate_other_user_anchors(app_ctx):
    """Anchors are user-scoped; canvas deletion only affects that canvas's anchors,
    which by design belong to the canvas owner."""
    app, cs, anchors = app_ctx
    # Seed: u_dev_alice's canvas + her anchor
    canvas, section = await _seed_canvas(cs, canvas_id="cnv1", payload={"v": 1})
    async with _client(app) as c:
        await c.post(
            "/api/v1/memory/notes/save-from-canvas",
            json={
                "canvas_id": canvas.id,
                "section_id": section.id,
                "summary_text": "her fact",
            },
        )
    # Manually plant anchor for other user pointing to a DIFFERENT canvas
    await anchors.insert_or_revive(
        user_id="u_other",
        bullet_text="his fact",
        canvas_id="other_canvas",
        section_id="other_sec",
        verifier_result_hash="h",
    )
    # Delete u_dev_alice's canvas
    await cs.delete_canvas(canvas_id=canvas.id)
    # u_other's anchor untouched
    other_rows = await anchors.list_for_user("u_other")
    assert len(other_rows) == 1
    assert other_rows[0].status == "valid"
