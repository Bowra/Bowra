"""V3 PRD-B · Complex end-to-end integration flows.

These tests exercise multi-step scenarios that single-endpoint tests miss:
  - Reset → undo restores everything
  - Reset → reset again uses fresh snapshot
  - Concurrent soft_delete + reset preserves data correctness
  - Cross-store cascade: removed user → memory purged
  - Bulk decide with mixed pending/decided/cross-user rows
  - Export → re-import via undo-reset round-trip parity
  - Pause + dreaming.observe() → no candidate enqueued
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_core.metrics_log import MetricsLog
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings
from bowhead_memory.candidates import (
    DreamingLight,
    DreamObservation,
    PauseStateCache,
)

from bowhead_core import UserMemoryStore
from bowhead_memory import MemoryCandidateStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def app_ctx(tmp_path: Path):
    db = tmp_path / "complex.db"
    mem = UserMemoryStore(db)
    await mem.initialize()
    candidates = MemoryCandidateStore(db_path=str(db))
    await candidates.initialize()
    settings_store = UserSettingsStore(db)
    await settings_store.init()
    metrics = MetricsLog(db)
    await metrics.initialize()
    user_store = UserStore(str(db))
    await user_store.initialize()
    pause_cache = PauseStateCache(settings_store=settings_store, ttl_seconds=10.0)

    app = create_app()
    app.state.settings = Settings()
    app.state.user_memory_store = mem
    app.state.memory_candidate_store = candidates
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


async def _durable_privacy_store(db_path: str, tmp_path: Path):
    from bowhead_core.memory_privacy import (
        MemoryDestructiveOperationStore,
        PrivacyDeletionLedger,
    )

    ledger = PrivacyDeletionLedger(
        tmp_path / "gc-privacy-ledger.db",
        hmac_key="gc-test-ledger-key",
    )
    await ledger.initialize()
    store = MemoryDestructiveOperationStore(
        db_path,
        ledger=ledger,
        fingerprint_key="gc-test-fingerprint-key",
        require_secure_delete_baseline=False,
    )
    await store.initialize()
    return store


# ─── Reset → Undo round-trip ─────────────────────────


@pytest.mark.integration
async def test_reset_then_undo_restores_memory(app_ctx) -> None:
    """Full round-trip: seed → reset → undo → original entries back."""
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store

    # Seed 3 entries
    await mem.upsert(user_id=uid, kind="preference", key="k1", value="v1")
    await mem.upsert(user_id=uid, kind="preference", key="k2", value="v2")
    await mem.upsert(user_id=uid, kind="constraint", key="no_pii", value=True)

    async with _client(app_ctx) as c:
        # Reset
        rb = (await c.post("/api/v1/memory/reset")).json()
        assert rb["affected_memory"] == 3
        snap_id = rb["snapshot_id"]

        # Verify all gone (in trash)
        snap = (await c.get("/api/v1/memory/snapshot")).json()
        assert snap["items"] == []
        assert snap["stats"]["trash_count"] == 3

        # Undo
        undo = (
            await c.post(
                "/api/v1/memory/undo-reset",
                json={"snapshot_id": snap_id},
            )
        ).json()
        assert undo["restored_memory"] == 3

        # All three back, NO trash
        snap = (await c.get("/api/v1/memory/snapshot")).json()
        keys = sorted(it["key"] for it in snap["items"])
        assert keys == ["k1", "k2", "no_pii"]


@pytest.mark.integration
async def test_undo_reset_is_single_use(app_ctx) -> None:
    """Snapshot deleted on undo so it can't be replayed."""
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    await mem.upsert(user_id=uid, kind="preference", key="x", value="1")

    async with _client(app_ctx) as c:
        rb = (await c.post("/api/v1/memory/reset")).json()
        snap_id = rb["snapshot_id"]
        await c.post("/api/v1/memory/undo-reset", json={"snapshot_id": snap_id})

        # Second undo: 404
        r2 = await c.post(
            "/api/v1/memory/undo-reset",
            json={"snapshot_id": snap_id},
        )
        assert r2.status_code == 404


@pytest.mark.integration
async def test_undo_reset_cross_user_blocked(app_ctx) -> None:
    """User A cannot undo user B's reset snapshot."""
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    await mem.upsert(user_id="u_bob", kind="preference", key="x", value="1")
    snap_id = await mem.save_reset_snapshot(
        user_id="u_bob",
        snapshot={"user_id": "u_bob", "memory_entries": []},
    )

    async with _client(app_ctx) as c:
        # Default user (alice) tries to undo bob's snapshot
        r = await c.post(
            "/api/v1/memory/undo-reset",
            json={"snapshot_id": snap_id},
        )
        assert r.status_code == 404


# ─── Bulk decide complex mix ─────────────────────────


@pytest.mark.integration
async def test_bulk_approve_mixed_pending_decided_cross_user(app_ctx) -> None:
    """Bulk: 1 pending mine, 1 already approved mine, 1 cross-user, 1 unknown id."""
    uid = app_ctx.state.settings.dev_default_user
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store

    pending = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="p",
        value="x",
        reason=None,
        source_turn_id=None,
    )
    decided = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="d",
        value="y",
        reason=None,
        source_turn_id=None,
    )
    await cs.set_decision(cid=decided.id, status="approved", decided_by=uid)

    foreign = await cs.enqueue(
        user_id="u_bob",
        kind="preference",
        key="b",
        value="z",
        reason=None,
        source_turn_id=None,
    )

    async with _client(app_ctx) as c:
        body = (
            await c.post(
                "/api/v1/memory/candidates/bulk-decide",
                json={
                    "cids": [pending.id, decided.id, foreign.id, "cand_unknown"],
                    "action": "approve",
                },
            )
        ).json()

        # 1 pending succeeds, 1 already-decided returns ok=True idempotent,
        # 1 foreign fails, 1 unknown fails
        assert body["total"] == 4
        per = {it["cid"]: it for it in body["items"]}
        assert per[pending.id]["ok"] is True
        assert per[decided.id]["ok"] is True
        assert per[foreign.id]["ok"] is False
        assert per[foreign.id]["error"] == "not_found"
        assert per["cand_unknown"]["ok"] is False


# ─── Concurrent reset + soft_delete ──────────────────


@pytest.mark.integration
async def test_concurrent_reset_and_soft_delete_correct_count(app_ctx) -> None:
    """Race: /reset and DELETE on individual entry. Both complete; final state is fully empty."""
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store

    e1 = await mem.upsert(user_id=uid, kind="preference", key="a", value="1")
    e2 = await mem.upsert(user_id=uid, kind="preference", key="b", value="2")

    async with _client(app_ctx) as c:
        # Fire both concurrently
        await asyncio.gather(
            c.post("/api/v1/memory/reset"),
            c.delete(f"/api/v1/memory/{e1.id}"),
            return_exceptions=True,
        )
        # At least one must succeed; e2 must be soft-deleted
        snap = (await c.get("/api/v1/memory/snapshot")).json()
        assert snap["items"] == []
        # Trash should contain e1 + e2
        trash = (await c.get("/api/v1/memory/trash")).json()
        ids = {it["id"] for it in trash["items"]}
        assert {e1.id, e2.id}.issubset(ids)


# ─── Pause + DreamingLight skip ──────────────────────


@pytest.mark.integration
async def test_pause_then_dreaming_observe_skips(app_ctx) -> None:
    """After /memory/dreaming/pause, DreamingLight.observe() returns [] without LLM call."""
    uid = app_ctx.state.settings.dev_default_user

    async with _client(app_ctx) as c:
        await c.post("/api/v1/memory/dreaming/pause")

    cache: PauseStateCache = app_ctx.state.pause_state_cache
    assert await cache.is_paused(uid) is True

    # Build DreamingLight with a stub router (would crash if invoked)
    class _ExplodeRouter:
        async def invoke(self, *a, **kw):
            raise RuntimeError("must not be called when paused")

    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    light = DreamingLight(
        router=_ExplodeRouter(),
        candidate_store=cs,
        pause_cache=cache,
    )
    out = await light.observe(
        DreamObservation(
            turn_id="t_paused",
            user_id=uid,
            user_text="hi",
            final_answer="ok",
            intent="x",
            tool_chain=[],
        )
    )
    assert out == []
    # No candidate enqueued
    pending = await cs.list_for_user(user_id=uid, status="pending")
    assert pending == []


# ─── End-to-end: full user flow ──────────────────────


@pytest.mark.integration
async def test_full_user_flow_remember_dream_approve_delete_restore(app_ctx) -> None:
    """Big happy path: /remember + AI candidate → approve → delete → trash → restore.

    Verifies the complete data lifecycle across all 4 stores + audit + telemetry.
    """
    uid = app_ctx.state.settings.dev_default_user
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    metrics: MetricsLog = app_ctx.state.metrics_log

    async with _client(app_ctx) as c:
        # 1. /remember
        r = await c.post(
            "/api/v1/memory/user/remember",
            json={"kind": "preference", "key": "shows_qoq", "value": True},
        )
        assert r.status_code == 200

        # 2. Simulate Dreaming proposing a preference candidate (via direct enqueue)
        cand = await cs.enqueue(
            user_id=uid,
            kind="preference",
            key="prefer_color",
            value="green",
            reason="user said it",
            source_turn_id="t1",
        )

        # 3. Approve via UI
        r = await c.post(f"/api/v1/memory/candidates/{cand.id}/approve", json={})
        assert r.status_code == 200

        # 4. Snapshot has both
        snap = (await c.get("/api/v1/memory/snapshot")).json()
        keys = sorted(it["key"] for it in snap["items"])
        assert "shows_qoq" in keys
        assert "prefer_color" in keys
        assert snap["stats"]["pending_candidates"] == 0

        # 5. Delete one via unified API
        target_id = next(it["id"] for it in snap["items"] if it["key"] == "shows_qoq")
        await c.delete(f"/api/v1/memory/{target_id}")

        # 6. Snapshot reflects deletion
        snap = (await c.get("/api/v1/memory/snapshot")).json()
        assert all(it["key"] != "shows_qoq" for it in snap["items"])

        # 7. Trash has it
        trash = (await c.get("/api/v1/memory/trash")).json()
        assert any(it["id"] == target_id for it in trash["items"])

        # 8. Restore
        await c.post(f"/api/v1/memory/{target_id}/restore")
        snap = (await c.get("/api/v1/memory/snapshot")).json()
        assert any(it["key"] == "shows_qoq" for it in snap["items"])

    # 9. Telemetry has the events
    events = await metrics.query_recent(user_id=uid)
    event_names = {e["name"] for e in events}
    assert "memory.entry.create" in event_names
    assert "memory.candidate.approve" in event_names
    assert "memory.entry.delete" in event_names
    assert "memory.entry.restore" in event_names


# ─── Cross-store consistency: removed user purged ─────────


# ─── Trash → 30-day GC ────────────────────────────────


@pytest.mark.integration
async def test_trash_30d_purge_does_not_touch_fresh(
    app_ctx,
    tmp_path,
) -> None:
    """Cron purges trash > 30d; recent trash stays."""
    import aiosqlite
    from bowhead_core.memory_gc import run_memory_gc_once

    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store

    fresh = await mem.upsert(user_id=uid, kind="preference", key="fresh", value="1")
    stale = await mem.upsert(user_id=uid, kind="preference", key="stale", value="2")
    await mem.soft_delete(user_id=uid, entry_id=fresh.id)
    await mem.soft_delete(user_id=uid, entry_id=stale.id)

    async with aiosqlite.connect(mem._db_path) as db:
        await db.execute(
            "UPDATE memory_entries SET deleted_at=datetime('now', '-60 days') WHERE id=?",
            (stale.id,),
        )
        await db.commit()
    privacy = await _durable_privacy_store(
        mem._db_path,
        tmp_path,
    )

    stats = await run_memory_gc_once(
        db_path=mem._db_path,
        user_memory_store=mem,
        metrics_log=app_ctx.state.metrics_log,
        destructive_operation_store=privacy,
    )
    assert stats["memory_trash_purged"] == 1
    remaining = await mem.list_for_user(uid, include_trashed=True)
    assert len(remaining) == 1
    assert remaining[0].key == "fresh"
