"""V3 PRD-B · memory_gc cron tests.

Covers:
  - try_acquire_gc_lock: first call wins, second within interval skipped, races safe
  - run_memory_gc_once: trash purge, candidate auto-reject, removed-user cascade,
    snapshot purge, metrics trim
  - datetime() SQL normalize works (ISO 'T' vs sqlite ' ')
"""

from __future__ import annotations

import asyncio
import contextlib

import aiosqlite
import pytest
from bowhead_core.memory_gc import (
    memory_gc_loop,
    run_memory_gc_once,
    try_acquire_gc_lock,
)
from bowhead_core.memory_privacy import (
    MemoryDestructiveOperationStore,
    PrivacyDeletionLedger,
)
from bowhead_core.metrics_log import MetricsLog
from bowhead_core.user_memory import UserMemoryStore
from bowhead_core.user_store import UserStore
from bowhead_memory.candidates import MemoryCandidateStore


@pytest.fixture
async def stores(tmp_path):
    db = tmp_path / "gc.db"
    mem = UserMemoryStore(db)
    await mem.initialize()
    candidates = MemoryCandidateStore(db_path=str(db))
    await candidates.initialize()
    metrics = MetricsLog(db)
    await metrics.initialize()
    user_store = UserStore(str(db))
    await user_store.initialize()
    ledger = PrivacyDeletionLedger(
        tmp_path / "privacy-ledger.db",
        hmac_key="test-ledger-key",
    )
    await ledger.initialize()
    destructive = MemoryDestructiveOperationStore(
        db,
        ledger=ledger,
        fingerprint_key="test-fingerprint-key",
        require_secure_delete_baseline=False,
    )
    await destructive.initialize()
    return {
        "db_path": str(db),
        "mem": mem,
        "candidates": candidates,
        "metrics": metrics,
        "user_store": user_store,
        "destructive": destructive,
    }


# ─── try_acquire_gc_lock ────────────────────────────────


@pytest.mark.asyncio
async def test_lock_first_call_wins(stores):
    won = await try_acquire_gc_lock(
        stores["db_path"],
        cron_name="t1",
        interval_seconds=60,
    )
    assert won is True


@pytest.mark.asyncio
async def test_lock_second_call_within_interval_skipped(stores):
    await try_acquire_gc_lock(
        stores["db_path"],
        cron_name="t2",
        interval_seconds=60,
    )
    won2 = await try_acquire_gc_lock(
        stores["db_path"],
        cron_name="t2",
        interval_seconds=60,
    )
    assert won2 is False


@pytest.mark.asyncio
async def test_lock_after_interval_expires_wins(stores):
    await try_acquire_gc_lock(
        stores["db_path"],
        cron_name="t3",
        interval_seconds=60,
    )
    # Force-age the row
    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(
            "UPDATE memory_gc_state SET last_run_at=datetime('now', '-2 hours') WHERE cron_name=?",
            ("t3",),
        )
        await db.commit()

    won = await try_acquire_gc_lock(
        stores["db_path"],
        cron_name="t3",
        interval_seconds=3600,
    )
    assert won is True


@pytest.mark.asyncio
async def test_lock_concurrent_two_workers_only_one_wins(stores):
    """Race two acquires for same cron_name simultaneously — exactly one wins."""
    db_path = stores["db_path"]

    async def acquire():
        return await try_acquire_gc_lock(
            db_path,
            cron_name="t_race",
            interval_seconds=60,
        )

    a, b = await asyncio.gather(acquire(), acquire())
    # SQLite serializes writes, so exactly one True
    assert (a, b).count(True) == 1


# ─── run_memory_gc_once ─────────────────────────────────


@pytest.mark.asyncio
async def test_gc_purges_old_memory_trash(stores):
    mem: UserMemoryStore = stores["mem"]
    e_old = await mem.upsert(user_id="u1", kind="preference", key="old", value="x")
    e_fresh = await mem.upsert(user_id="u1", kind="preference", key="fresh", value="y")
    await mem.soft_delete(user_id="u1", entry_id=e_old.id)
    await mem.soft_delete(user_id="u1", entry_id=e_fresh.id)

    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(
            "UPDATE memory_entries SET deleted_at=datetime('now', '-60 days') WHERE id=?",
            (e_old.id,),
        )
        await db.commit()

    stats = await run_memory_gc_once(
        db_path=stores["db_path"],
        user_memory_store=mem,
        candidate_store=stores["candidates"],
        metrics_log=stores["metrics"],
        destructive_operation_store=stores["destructive"],
    )
    assert stats["memory_trash_purged"] == 1

    remaining = await mem.list_for_user("u1", include_trashed=True)
    keys = {e.key for e in remaining}
    assert keys == {"fresh"}


@pytest.mark.asyncio
async def test_gc_retains_content_when_privacy_ledger_is_unavailable(stores):
    mem: UserMemoryStore = stores["mem"]
    entry = await mem.upsert(
        user_id="u1",
        kind="preference",
        key="retain",
        value="x",
    )
    await mem.soft_delete(user_id="u1", entry_id=entry.id)
    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(
            """
            UPDATE memory_entries
            SET deleted_at=datetime('now', '-60 days')
            WHERE id=?
            """,
            (entry.id,),
        )
        await db.commit()

    stats = await run_memory_gc_once(
        db_path=stores["db_path"],
        user_memory_store=mem,
    )

    assert stats["memory_trash_purged"] == 0
    remaining = await mem.list_for_user(
        "u1",
        include_trashed=True,
    )
    assert [item.id for item in remaining] == [entry.id]


@pytest.mark.asyncio
async def test_gc_auto_rejects_old_candidates(stores):
    cs: MemoryCandidateStore = stores["candidates"]
    fresh = await cs.enqueue(
        user_id="u1",
        kind="preference",
        key="fresh",
        value="x",
        reason=None,
        source_turn_id=None,
    )
    stale = await cs.enqueue(
        user_id="u1",
        kind="preference",
        key="stale",
        value="y",
        reason=None,
        source_turn_id=None,
    )
    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(
            "UPDATE memory_candidates SET created_at=datetime('now', '-60 days') WHERE id=?",
            (stale.id,),
        )
        await db.commit()

    stats = await run_memory_gc_once(
        db_path=stores["db_path"],
        user_memory_store=stores["mem"],
        candidate_store=cs,
    )
    assert stats["candidates_auto_rejected"] == 1
    assert (await cs.get(stale.id)).status == "rejected"
    assert (await cs.get(fresh.id)).status == "pending"


class _FakeNotify:
    """记录 expiry 预告调用；resolve 兜底 no-op（stale 分支可能调）。"""

    def __init__(self) -> None:
        self.expiring_calls: list[tuple[str, int]] = []

    async def emit_memory_candidates_expiring(self, *, user_id: str, expiring_count: int) -> None:
        self.expiring_calls.append((user_id, expiring_count))

    async def resolve_memory_candidates(self, *, user_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_gc_emits_expiry_warning_once(stores):
    """S2 C3: age∈(23,30) 的 pending 在 GC 里发一次预告，第二次 GC 不再发（一生一次）。"""
    cs: MemoryCandidateStore = stores["candidates"]
    c = await cs.enqueue(user_id="u1", kind="preference", key="k", value="v", reason=None, source_turn_id=None)
    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(
            "UPDATE memory_candidates SET created_at=datetime('now', '-25 days') WHERE id=?",
            (c.id,),
        )
        await db.commit()

    notify = _FakeNotify()
    stats = await run_memory_gc_once(
        db_path=stores["db_path"],
        user_memory_store=stores["mem"],
        candidate_store=cs,
        notification_service=notify,
    )
    assert stats["candidates_expiry_warned"] == 1
    assert notify.expiring_calls == [("u1", 1)]
    assert (await cs.get(c.id)).status == "pending"  # Warning does not change status.

    # Second GC: already marked → no repeat warning.
    notify2 = _FakeNotify()
    stats2 = await run_memory_gc_once(
        db_path=stores["db_path"],
        user_memory_store=stores["mem"],
        candidate_store=cs,
        notification_service=notify2,
    )
    assert stats2["candidates_expiry_warned"] == 0
    assert notify2.expiring_calls == []


@pytest.mark.asyncio
async def test_gc_purges_old_reset_snapshots(stores, tmp_path):
    mem: UserMemoryStore = stores["mem"]
    snap_id = await mem.save_reset_snapshot(user_id="u1", snapshot={"k": 1})
    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(
            "UPDATE memory_reset_snapshots SET created_at=datetime('now', '-60 days') WHERE id=?",
            (snap_id,),
        )
        await db.commit()

    stats = await run_memory_gc_once(
        db_path=stores["db_path"],
        user_memory_store=mem,
        destructive_operation_store=stores["destructive"],
    )
    assert stats["reset_snapshots_purged"] == 1


@pytest.mark.asyncio
async def test_gc_trims_old_metrics_log(stores):
    log: MetricsLog = stores["metrics"]
    await log.track("memory.entry.create", user_id="u1")
    await log.track("memory.entry.create", user_id="u1")

    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(
            "UPDATE metrics_event_log SET created_at=datetime('now', '-200 days') WHERE user_id='u1'",
        )
        await db.commit()

    stats = await run_memory_gc_once(
        db_path=stores["db_path"],
        user_memory_store=stores["mem"],
        metrics_log=log,
    )
    assert stats["metrics_log_trimmed"] == 2


# ─── memory_gc_loop integration ─────────────────────────


@pytest.mark.asyncio
async def test_gc_purges_old_notes_trash(stores, tmp_path):
    """V3.5 PRD-C: memory_notes trash > 30d hard-purged."""
    from bowhead_core.memory_notes import MemoryNotesStore

    notes = MemoryNotesStore(stores["db_path"])
    await notes.initialize()
    await notes.upsert_content(user_id="u1", content="old")
    await notes.soft_delete(user_id="u1")

    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(
            "UPDATE memory_notes SET deleted_at=datetime('now', '-60 days') WHERE subject_type='user' AND subject_id='u1'",
        )
        await db.commit()

    stats = await run_memory_gc_once(
        db_path=stores["db_path"],
        user_memory_store=stores["mem"],
        notes_store=notes,
        destructive_operation_store=stores["destructive"],
    )
    assert stats["notes_trash_purged"] == 1
    assert await notes.get(user_id="u1", include_trashed=True) is None


@pytest.mark.asyncio
async def test_gc_purges_old_pack_snapshots(stores, tmp_path):
    """V4 PRD-D-4-4: memory_pack_snapshots > 180d hard-purged."""
    from bowhead_core.memory_pack_snapshots import MemoryPackSnapshotsStore

    snaps = MemoryPackSnapshotsStore(stores["db_path"])
    await snaps.initialize()
    await snaps.save(
        turn_id="t_old",
        user_id="u1",
        team_id="org1",
        workspace_id="ws1",
        content="old pack",
        sections_breakdown={"notes": 8},
        pack_version="v2",
    )
    await snaps.save(
        turn_id="t_fresh",
        user_id="u1",
        team_id="org1",
        workspace_id="ws1",
        content="fresh pack",
        sections_breakdown={"notes": 10},
        pack_version="v2",
    )

    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(
            "UPDATE memory_pack_snapshots SET created_at=datetime('now', '-200 days') WHERE turn_id=?",
            ("t_old",),
        )
        await db.commit()

    stats = await run_memory_gc_once(
        db_path=stores["db_path"],
        user_memory_store=stores["mem"],
        snapshots_store=snaps,
        destructive_operation_store=stores["destructive"],
    )
    assert stats["pack_snapshots_purged"] == 1
    assert await snaps.get_by_turn_for_user(turn_id="t_old", user_id="u1") is None
    assert await snaps.get_by_turn_for_user(turn_id="t_fresh", user_id="u1") is not None


@pytest.mark.asyncio
async def test_gc_purges_old_invalidated_anchors(stores, tmp_path):
    """V4 PRD-D-4-1: invalidated anchors > 180d hard-purged."""
    from bowhead_core.memory_note_anchors import MemoryNoteAnchorsStore

    anchors = MemoryNoteAnchorsStore(stores["db_path"])
    await anchors.initialize()
    await anchors.insert_or_revive(
        user_id="u1",
        bullet_text="old",
        canvas_id="c_old",
        section_id="s",
        verifier_result_hash="h",
    )
    await anchors.insert_or_revive(
        user_id="u1",
        bullet_text="fresh",
        canvas_id="c_fresh",
        section_id="s",
        verifier_result_hash="h",
    )
    await anchors.invalidate_by_canvas(canvas_id="c_old")
    await anchors.invalidate_by_canvas(canvas_id="c_fresh")

    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(
            "UPDATE memory_note_anchors SET invalidated_at=datetime('now', '-200 days') WHERE canvas_id='c_old'",
        )
        await db.commit()

    stats = await run_memory_gc_once(
        db_path=stores["db_path"],
        user_memory_store=stores["mem"],
        anchors_store=anchors,
        destructive_operation_store=stores["destructive"],
    )
    assert stats["anchor_invalidated_purged"] == 1
    rows = await anchors.list_for_user("u1")
    assert {r.canvas_id for r in rows} == {"c_fresh"}


@pytest.mark.asyncio
async def test_gc_marks_orphan_anchors(stores, tmp_path):
    """V4 PRD-D-4-1: orphan anchor (bullet removed from content) → mark stale."""
    from bowhead_core.memory_note_anchors import MemoryNoteAnchorsStore
    from bowhead_core.memory_notes import MemoryNotesStore

    notes = MemoryNotesStore(stores["db_path"])
    await notes.initialize()
    anchors = MemoryNoteAnchorsStore(stores["db_path"])
    await anchors.initialize()

    await notes.upsert_content(user_id="u1", content="📌 keep this\n• drop this")
    await anchors.insert_or_revive(
        user_id="u1",
        bullet_text="keep this",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h",
    )
    await anchors.insert_or_revive(
        user_id="u1",
        bullet_text="drop this",
        canvas_id="c",
        section_id="s2",
        verifier_result_hash="h",
    )
    # User PATCHes notes, removing "drop this"
    await notes.upsert_content(user_id="u1", content="📌 keep this")

    stats = await run_memory_gc_once(
        db_path=stores["db_path"],
        user_memory_store=stores["mem"],
        notes_store=notes,
        anchors_store=anchors,
    )
    assert stats["anchor_orphans_marked"] == 1
    rows = {r.bullet_text: r.status for r in await anchors.list_for_user("u1")}
    assert rows["keep this"] == "valid"
    assert rows["drop this"] == "stale"


@pytest.mark.asyncio
async def test_loop_runs_once_then_cancels(stores):
    """Smoke: loop kicks off after initial_delay, executes a run, then cancellable."""
    mem: UserMemoryStore = stores["mem"]
    e = await mem.upsert(user_id="u1", kind="preference", key="t", value="v")
    await mem.soft_delete(user_id="u1", entry_id=e.id)
    async with aiosqlite.connect(stores["db_path"]) as db:
        await db.execute(
            "UPDATE memory_entries SET deleted_at=datetime('now', '-60 days') WHERE id=?",
            (e.id,),
        )
        await db.commit()

    task = asyncio.create_task(
        memory_gc_loop(
            db_path=stores["db_path"],
            user_memory_store=mem,
            candidate_store=stores["candidates"],
            metrics_log=stores["metrics"],
            destructive_operation_store=stores["destructive"],
            initial_delay_seconds=0.05,
            interval_seconds=3600,
            cron_name="loop_smoke",
        ),
    )
    # Wait for the GC pass to run + complete
    await asyncio.sleep(0.5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # Old trashed entry should now be hard-deleted
    remaining = await mem.list_for_user("u1", include_trashed=True)
    assert remaining == []


# ─── review-fix P1 (memory-revive S1): resolve badges after GC auto-reject ────────


@pytest.mark.asyncio
async def test_gc_auto_reject_resolves_badge(tmp_path):
    """GC 把用户 pending 清零后必须 resolve 其 memory.candidate 通知。"""
    import aiosqlite
    from bowhead_core.user_memory import UserMemoryStore
    from bowhead_memory.candidates import MemoryCandidateStore

    db = tmp_path / "gc.db"
    ums = UserMemoryStore(db)
    await ums.initialize()
    cs = MemoryCandidateStore(db_path=str(db))
    await cs.initialize()

    c = await cs.enqueue(user_id="u_stale", kind="note", key="", value="旧候选", reason=None, source_turn_id=None)
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute(
            "UPDATE memory_candidates SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (c.id,),
        )
        await conn.commit()

    resolved: list[str] = []

    class _FakeNotify:
        async def resolve_memory_candidates(self, *, user_id):
            resolved.append(user_id)

    from bowhead_core.memory_gc import run_memory_gc_once

    stats = await run_memory_gc_once(
        db_path=db,
        user_memory_store=ums,
        candidate_store=cs,
        notification_service=_FakeNotify(),
    )
    assert stats["candidates_auto_rejected"] == 1
    assert resolved == ["u_stale"]
