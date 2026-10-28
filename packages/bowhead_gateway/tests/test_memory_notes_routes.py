"""V3.5 PRD-C · /api/v1/memory/notes routes e2e (~20 cases)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_core.memory_notes import MemoryNotesStore
from bowhead_core.metrics_log import MetricsLog
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings
from bowhead_memory.candidates import PauseStateCache

from bowhead_core import UserMemoryStore
from bowhead_memory import MemoryCandidateStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def app_ctx(tmp_path: Path):
    db = tmp_path / "t.db"
    mem = UserMemoryStore(db)
    await mem.initialize()
    candidates = MemoryCandidateStore(db_path=str(db))
    await candidates.initialize()
    notes = MemoryNotesStore(db)
    await notes.initialize()
    settings_store = UserSettingsStore(db)
    await settings_store.init()
    metrics = MetricsLog(db)
    await metrics.initialize()
    user_store = UserStore(str(db))
    await user_store.initialize()
    pause_cache = PauseStateCache(settings_store=settings_store, ttl_seconds=60.0)

    app = create_app()
    app.state.settings = Settings()
    app.state.user_memory_store = mem
    app.state.memory_candidate_store = candidates
    app.state.memory_notes_store = notes
    app.state.user_settings_store = settings_store
    app.state.metrics_log = metrics
    app.state.user_store = user_store
    app.state.pause_state_cache = pause_cache
    app.state.orchestrator = None
    app.state.datarc = None
    return app


def _client(app):
    transport = httpx2.ASGITransport(app=app)
    return httpx2.AsyncClient(transport=transport, base_url="http://gw")


async def _lock_notes(
    client: httpx2.AsyncClient,
    *,
    version: str,
) -> dict:
    response = await client.post(
        "/api/v1/memory/notes/lock",
        json={"expected_version": version},
    )
    assert response.status_code == 200
    return response.json()


async def _patch_notes(
    client: httpx2.AsyncClient,
    *,
    content: str,
) -> httpx2.Response:
    current = (await client.get("/api/v1/memory/notes")).json()
    lease = await _lock_notes(client, version=current["version"])
    return await client.patch(
        "/api/v1/memory/notes",
        json={
            "content": content,
            "version": lease["version"],
            "edit_token": lease["edit_token"],
        },
    )


# ─── GET /notes ──────────────────────────────────────


@pytest.mark.integration
async def test_get_notes_empty_for_new_user(app_ctx) -> None:
    async with _client(app_ctx) as c:
        r = await c.get("/api/v1/memory/notes")
        assert r.status_code == 200
        body = r.json()
        assert body["content"] == ""
        assert body["char_count"] == 0
        assert body["is_locked"] is False


# ─── PATCH /notes ────────────────────────────────────


@pytest.mark.integration
async def test_patch_notes_basic_roundtrip(app_ctx) -> None:
    async with _client(app_ctx) as c:
        r = await _patch_notes(
            c,
            content="我是审计师\n📌 Q3 cross-check\n• 偏好正式书面语",
        )
        assert r.status_code == 200
        body = r.json()
        assert "我是审计师" in body["content"]
        assert body["pinned_count"] == 1
        assert body["bullet_count"] == 1
        assert body["free_text_lines"] == 1

        r2 = await c.get("/api/v1/memory/notes")
        assert "审计师" in r2.json()["content"]


@pytest.mark.integration
async def test_patch_notes_413_oversize(app_ctx) -> None:
    async with _client(app_ctx) as c:
        r = await c.patch(
            "/api/v1/memory/notes",
            json={
                "content": "x" * 4000,
                "version": "0",
                "edit_token": "x" * 43,
            },
        )
        assert r.status_code == 422  # Pydantic max_length validation runs first
        # Confirm: 3001 should also reject
        r2 = await c.patch(
            "/api/v1/memory/notes",
            json={
                "content": "x" * 3001,
                "version": "0",
                "edit_token": "x" * 43,
            },
        )
        assert r2.status_code == 422


@pytest.mark.integration
async def test_patch_notes_requires_matching_owner_lease(app_ctx) -> None:
    async with _client(app_ctx) as c:
        lease = await _lock_notes(c, version="0")
        r = await c.patch(
            "/api/v1/memory/notes",
            json={
                "content": "save",
                "version": "0",
                "edit_token": "x" * 43,
            },
        )
        assert r.status_code == 423

        saved = await c.patch(
            "/api/v1/memory/notes",
            json={
                "content": "save",
                "version": lease["version"],
                "edit_token": lease["edit_token"],
            },
        )
        assert saved.status_code == 200
        assert saved.json()["version"] == "1"


@pytest.mark.integration
async def test_post_append_blocks_when_locked(app_ctx) -> None:
    """Append (agent path) DOES respect lock — that's the whole point of lock."""
    notes: MemoryNotesStore = app_ctx.state.memory_notes_store
    uid = app_ctx.state.settings.dev_default_user
    await notes.acquire_lock(user_id=uid, ttl_seconds=300)

    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/notes/append",
            json={"bullet": "agent_proposal", "source": "dreaming_approved"},
        )
        assert r.status_code == 423


@pytest.mark.integration
async def test_post_append_with_ignore_lock_still_blocks(app_ctx) -> None:
    notes: MemoryNotesStore = app_ctx.state.memory_notes_store
    uid = app_ctx.state.settings.dev_default_user
    await notes.acquire_lock(user_id=uid, ttl_seconds=300)

    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/notes/append",
            json={"bullet": "forced", "ignore_lock": True},
        )
        assert r.status_code == 423


@pytest.mark.integration
async def test_patch_notes_empty_content_clears(app_ctx) -> None:
    async with _client(app_ctx) as c:
        await _patch_notes(c, content="stuff")
        r = await _patch_notes(c, content="")
        assert r.status_code == 200
        assert r.json()["content"] == ""
        assert r.json()["char_count"] == 0


# ─── POST /notes/append ──────────────────────────────


@pytest.mark.integration
async def test_append_pinned_then_bullet(app_ctx) -> None:
    async with _client(app_ctx) as c:
        await c.post("/api/v1/memory/notes/append", json={"bullet": "重点", "pinned": True})
        await c.post("/api/v1/memory/notes/append", json={"bullet": "正常"})
        body = (await c.get("/api/v1/memory/notes")).json()
        assert "📌 重点" in body["content"]
        assert "• 正常" in body["content"]
        assert body["pinned_count"] == 1
        assert body["bullet_count"] == 1


@pytest.mark.integration
async def test_append_400_on_bullet_too_long(app_ctx) -> None:
    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/notes/append",
            json={"bullet": "x" * 1000},
        )
        # Pydantic max_length=500 → 422
        assert r.status_code == 422


# ─── DELETE /notes/bullet ────────────────────────────


@pytest.mark.integration
async def test_delete_bullet_by_index_skips_freetext(app_ctx) -> None:
    async with _client(app_ctx) as c:
        saved = await _patch_notes(
            c,
            content="我是审计师\n📌 Q3\n• 正式\n• RPA",
        )
        lease = await _lock_notes(c, version=saved.json()["version"])
        # Bullets: 0=📌Q3, 1=•正式, 2=•RPA
        r = await c.delete(
            "/api/v1/memory/notes/bullet?bullet_index=1",
            headers={
                "If-Match": f'"{lease["version"]}"',
                "X-Memory-Edit-Token": lease["edit_token"],
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert "正式" not in body["content"]
        assert "RPA" in body["content"]
        assert "我是审计师" in body["content"]  # freetext kept


@pytest.mark.integration
async def test_delete_bullet_400_out_of_range(app_ctx) -> None:
    async with _client(app_ctx) as c:
        saved = await _patch_notes(c, content="• one")
        lease = await _lock_notes(c, version=saved.json()["version"])
        r = await c.delete(
            "/api/v1/memory/notes/bullet?bullet_index=5",
            headers={
                "If-Match": f'"{lease["version"]}"',
                "X-Memory-Edit-Token": lease["edit_token"],
            },
        )
        assert r.status_code == 400


# ─── POST /notes/lock + /unlock ──────────────────────


@pytest.mark.integration
async def test_lock_then_unlock(app_ctx) -> None:
    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/notes/lock",
            json={"expected_version": "0"},
        )
        assert r.status_code == 200
        assert "locked_until" in r.json()
        assert "edit_token" in r.json()

        r2 = await c.post(
            "/api/v1/memory/notes/unlock",
            json={"edit_token": r.json()["edit_token"]},
        )
        assert r2.status_code == 200


@pytest.mark.integration
async def test_lock_423_when_other_holds(app_ctx) -> None:
    notes: MemoryNotesStore = app_ctx.state.memory_notes_store
    uid = app_ctx.state.settings.dev_default_user
    await notes.acquire_lock(user_id=uid, ttl_seconds=300)

    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/notes/lock",
            json={"expected_version": "0"},
        )
        assert r.status_code == 423


# ─── snapshot includes notes (V3.5) ──────────────────


@pytest.mark.integration
async def test_snapshot_includes_notes(app_ctx) -> None:
    async with _client(app_ctx) as c:
        await _patch_notes(c, content="📌 hello")
        body = (await c.get("/api/v1/memory/snapshot")).json()
        assert body["notes"] is not None
        assert int(body["notes"]["version"]) > 0
        assert body["notes"]["pinned_count"] == 1
        assert "hello" in body["notes"]["content"]


@pytest.mark.integration
async def test_snapshot_notes_null_for_new_user(app_ctx) -> None:
    """Empty notes return MemoryNoteDTO with empty content (not null)."""
    async with _client(app_ctx) as c:
        body = (await c.get("/api/v1/memory/snapshot")).json()
        assert body["notes"] is not None
        assert body["notes"]["content"] == ""


@pytest.mark.integration
async def test_snapshot_hides_trashed_notes_but_preserves_version(app_ctx) -> None:
    async with _client(app_ctx) as c:
        await _patch_notes(c, content="will be reset")
        before = (await c.get("/api/v1/memory/snapshot")).json()["notes"]
        await c.post("/api/v1/memory/reset")
        after = (await c.get("/api/v1/memory/snapshot")).json()["notes"]

    assert after["content"] == ""
    assert int(after["version"]) > int(before["version"])


# ─── /memory/reset includes notes ───────────────────


@pytest.mark.integration
async def test_reset_soft_deletes_notes(app_ctx) -> None:
    async with _client(app_ctx) as c:
        await _patch_notes(c, content="stuff")
        r = await c.post("/api/v1/memory/reset")
        assert r.json()["affected_notes"] == 1
        # Notes content cleared
        body = (await c.get("/api/v1/memory/notes")).json()
        assert body["content"] == ""


@pytest.mark.integration
async def test_reset_then_undo_restores_notes(app_ctx) -> None:
    async with _client(app_ctx) as c:
        await _patch_notes(c, content="原文")
        snap_id = (await c.post("/api/v1/memory/reset")).json()["snapshot_id"]
        body = (
            await c.post(
                "/api/v1/memory/undo-reset",
                json={"snapshot_id": snap_id},
            )
        ).json()
        assert body["restored_notes"] == 1

        notes_body = (await c.get("/api/v1/memory/notes")).json()
        assert notes_body["content"] == "原文"


# ─── /memory/export includes notes ──────────────────


@pytest.mark.integration
async def test_export_includes_notes(app_ctx) -> None:
    async with _client(app_ctx) as c:
        await _patch_notes(c, content="exported")
        text = (await c.get("/api/v1/memory/export")).text
        import json as _json

        body = _json.loads(text)
        assert body.get("_schema_version") == 3
        assert body["about_you"]["content"] == "exported"


# ─── candidates approve target=notes ───────────────


@pytest.mark.integration
async def test_approve_target_notes(app_ctx) -> None:
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    uid = app_ctx.state.settings.dev_default_user
    cand = await cs.enqueue(
        user_id=uid,
        kind="note",
        key="",
        value="用户偏好按渠道拆 GMV",
        reason="dreaming proposed",
        source_turn_id="t1",
    )
    async with _client(app_ctx) as c:
        r = await c.post(
            f"/api/v1/memory/candidates/{cand.id}/approve?target=notes",
            json={},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "approved"

    # Verify wrote to notes_store, not memory_entries
    notes = app_ctx.state.memory_notes_store
    note = await notes.get(user_id=uid)
    assert note is not None
    assert "按渠道拆 GMV" in note.content


@pytest.mark.integration
async def test_approve_kind_note_blocked_from_structured(app_ctx) -> None:
    """V3.5 P1: kind='note' cannot go to structured (memory_entries doesn't accept)."""
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    uid = app_ctx.state.settings.dev_default_user
    cand = await cs.enqueue(
        user_id=uid,
        kind="note",
        key="",
        value="x",
        reason=None,
        source_turn_id=None,
    )
    async with _client(app_ctx) as c:
        r = await c.post(
            f"/api/v1/memory/candidates/{cand.id}/approve?target=structured",
            json={},
        )
        assert r.status_code == 400


@pytest.mark.integration
async def test_approve_default_target_structured(app_ctx) -> None:
    """No target query param → default 'structured' (PRD-B compat)."""
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    uid = app_ctx.state.settings.dev_default_user
    cand = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="prefer_color",
        value="green",
        reason=None,
        source_turn_id=None,
    )
    async with _client(app_ctx) as c:
        r = await c.post(
            f"/api/v1/memory/candidates/{cand.id}/approve",
            json={},
        )
        assert r.status_code == 200

    # Wrote to memory_entries, not notes
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    entries = await mem.list_for_user(uid)
    assert any(e.key == "prefer_color" for e in entries)
    notes = app_ctx.state.memory_notes_store
    note = await notes.get(user_id=uid)
    assert note is None or note.content == ""


# ─── candidates enqueue_if_new dedupe ──────────────


@pytest.mark.integration
async def test_enqueue_if_new_dedupes_same_value(app_ctx) -> None:
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    uid = app_ctx.state.settings.dev_default_user

    a = await cs.enqueue_if_new(
        user_id=uid,
        kind="note",
        key="",
        value="用户老板叫张总",
        reason="r1",
        source_turn_id="t1",
    )
    assert a is not None

    # Same kind + value within 7 days → return None (dedupe)
    b = await cs.enqueue_if_new(
        user_id=uid,
        kind="note",
        key="",
        value="用户老板叫张总",
        reason="r2",
        source_turn_id="t2",
    )
    assert b is None


@pytest.mark.integration
async def test_enqueue_kind_note_accepted(app_ctx) -> None:
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    cand = await cs.enqueue(
        user_id="u1",
        kind="note",
        key="",
        value="x",
        reason=None,
        source_turn_id=None,
    )
    assert cand.kind == "note"


@pytest.mark.integration
async def test_enqueue_invalid_kind_rejected(app_ctx) -> None:
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    with pytest.raises(ValueError):
        await cs.enqueue(
            user_id="u1",
            kind="bogus",
            key="",
            value="x",
            reason=None,
            source_turn_id=None,
        )


# ─── audit + telemetry ─────────────────────────────


@pytest.mark.integration
async def test_audit_written_on_patch(app_ctx) -> None:
    async with _client(app_ctx) as c:
        await _patch_notes(c, content="audit-me")

    import aiosqlite

    db_path = str(app_ctx.state.user_memory_store._db_path)
    async with aiosqlite.connect(db_path) as db, db.execute("SELECT COUNT(*) FROM user_audit_log WHERE event_type='memory.notes.patch'") as cur:
        row = await cur.fetchone()
    assert row[0] >= 1


@pytest.mark.integration
async def test_metrics_written_on_append(app_ctx) -> None:
    async with _client(app_ctx) as c:
        await c.post("/api/v1/memory/notes/append", json={"bullet": "tracked"})

    log: MetricsLog = app_ctx.state.metrics_log
    rows = await log.query_recent(name="memory.notes.append")
    assert len(rows) >= 1
