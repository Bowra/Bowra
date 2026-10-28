"""V4 Sprint 2 PRD-D-4-1 · MemoryNoteAnchorsStore tests.

Covers:
  - schema initialization (table + indexes idempotent)
  - insert_or_revive: new row, dedup (no-op), revive (stale→valid+new hash)
  - cap exceeded → AnchorCapExceeded
  - list_for_user cross-user isolation
  - get_by_bullet normalize match
  - invalidate_by_canvas / invalidate_by_section scoping
  - recheck_hash detects payload change
  - delete_by_bullet cascade
  - hard_delete_user
  - hard_delete_old_invalidated TTL with datetime() normalize
  - mark_orphan_anchors detects bullet removed from content
  - bullet_text_norm dedup edge cases (case + whitespace)
"""

from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.memory_note_anchors import (
    AnchorCapExceeded,
    MemoryNoteAnchorsStore,
    _normalize,
)


@pytest.fixture
async def store(tmp_path):
    s = MemoryNoteAnchorsStore(tmp_path / "anchors.db")
    await s.initialize()
    return s


# ─── schema ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initialize_idempotent(tmp_path):
    s = MemoryNoteAnchorsStore(tmp_path / "x.db")
    await s.initialize()
    await s.initialize()  # second call must not raise


@pytest.mark.asyncio
async def test_normalize_helper():
    assert _normalize("  Q1 营收 ") == "q1 营收"
    assert _normalize("HELLO") == "hello"
    assert _normalize("") == ""


# ─── insert_or_revive ────────────────────────────────────


@pytest.mark.asyncio
async def test_insert_creates_new_row(store):
    a, created = await store.insert_or_revive(
        user_id="u1",
        bullet_text="Q1 营收 1200万",
        canvas_id="cnv_1",
        section_id="sec_1",
        verifier_result_hash="h1",
    )
    assert created is True
    assert a.user_id == "u1"
    assert a.bullet_text == "Q1 营收 1200万"
    assert a.bullet_text_norm == "q1 营收 1200万"
    assert a.status == "valid"
    assert a.verifier_result_hash == "h1"


@pytest.mark.asyncio
async def test_insert_dedup_same_hash(store):
    a1, c1 = await store.insert_or_revive(
        user_id="u1",
        bullet_text="fact",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h1",
    )
    a2, c2 = await store.insert_or_revive(
        user_id="u1",
        bullet_text="fact",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h1",
    )
    assert c1 is True
    assert c2 is False  # dedup
    assert a1.id == a2.id


@pytest.mark.asyncio
async def test_revive_stale_anchor(store):
    """Revival: stale anchor with new hash → status=valid, new hash."""
    a1, _ = await store.insert_or_revive(
        user_id="u1",
        bullet_text="fact",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h1",
    )
    # Mark stale via cascade
    await store.invalidate_by_canvas(canvas_id="c", reason="canvas_deleted")
    # Revive with new hash
    a2, c2 = await store.insert_or_revive(
        user_id="u1",
        bullet_text="fact",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h2",
    )
    assert c2 is False  # not new row
    assert a2.id == a1.id
    assert a2.status == "valid"
    assert a2.verifier_result_hash == "h2"
    assert a2.invalidated_at is None
    assert a2.invalidation_reason is None


@pytest.mark.asyncio
async def test_revive_valid_with_different_hash(store):
    """Same bullet, valid anchor, different hash → updates hash, stays valid."""
    a1, _ = await store.insert_or_revive(
        user_id="u1",
        bullet_text="fact",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h1",
    )
    a2, _ = await store.insert_or_revive(
        user_id="u1",
        bullet_text="fact",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h2",
    )
    assert a2.id == a1.id
    assert a2.verifier_result_hash == "h2"
    assert a2.status == "valid"


@pytest.mark.asyncio
async def test_dedup_bullet_text_normalize(store):
    """Case + whitespace differences treated as same anchor."""
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="Q1 营收",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h1",
    )
    _a2, c2 = await store.insert_or_revive(
        user_id="u1",
        bullet_text="  q1 营收  ",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h1",
    )
    assert c2 is False  # dedup via norm
    rows = await store.list_for_user("u1")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_cap_exceeded(store, monkeypatch):
    """Exceeding MAX_ANCHORS_PER_USER raises AnchorCapExceeded."""
    monkeypatch.setattr(
        "bowhead_core.memory_note_anchors.MAX_ANCHORS_PER_USER",
        3,
    )
    for i in range(3):
        await store.insert_or_revive(
            user_id="u1",
            bullet_text=f"fact_{i}",
            canvas_id=f"c_{i}",
            section_id="s",
            verifier_result_hash="h",
        )
    with pytest.raises(AnchorCapExceeded):
        await store.insert_or_revive(
            user_id="u1",
            bullet_text="fact_overflow",
            canvas_id="c_x",
            section_id="s",
            verifier_result_hash="h",
        )


@pytest.mark.asyncio
async def test_cap_does_not_count_stale(store, monkeypatch):
    """Stale anchors don't count toward cap."""
    monkeypatch.setattr(
        "bowhead_core.memory_note_anchors.MAX_ANCHORS_PER_USER",
        2,
    )
    # 2 valid
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="a",
        canvas_id="c1",
        section_id="s",
        verifier_result_hash="h",
    )
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="b",
        canvas_id="c2",
        section_id="s",
        verifier_result_hash="h",
    )
    # Invalidate canvas c1 → 1 stale + 1 valid
    await store.invalidate_by_canvas(canvas_id="c1")
    # Should now allow new INSERT (cap of 2 valid only)
    _a, created = await store.insert_or_revive(
        user_id="u1",
        bullet_text="c",
        canvas_id="c3",
        section_id="s",
        verifier_result_hash="h",
    )
    assert created is True


# ─── list_for_user ───────────────────────────────────────


@pytest.mark.asyncio
async def test_list_for_user_cross_user_isolation(store):
    await store.insert_or_revive(
        user_id="u_a",
        bullet_text="a fact",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h",
    )
    await store.insert_or_revive(
        user_id="u_b",
        bullet_text="b fact",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h",
    )
    rows = await store.list_for_user("u_a")
    assert len(rows) == 1
    assert rows[0].user_id == "u_a"


# ─── get_by_bullet ───────────────────────────────────────


@pytest.mark.asyncio
async def test_get_by_bullet_normalize_match(store):
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="Q1 营收",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h",
    )
    a = await store.get_by_bullet(user_id="u1", bullet_text="  q1 营收  ")
    assert a is not None
    assert a.bullet_text == "Q1 营收"  # raw preserved


# ─── invalidate_by_canvas / by_section ───────────────────


@pytest.mark.asyncio
async def test_invalidate_by_canvas(store):
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="a",
        canvas_id="cnv_X",
        section_id="s1",
        verifier_result_hash="h",
    )
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="b",
        canvas_id="cnv_X",
        section_id="s2",
        verifier_result_hash="h",
    )
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="c",
        canvas_id="cnv_Y",
        section_id="s1",
        verifier_result_hash="h",
    )
    n = await store.invalidate_by_canvas(canvas_id="cnv_X", reason="canvas_deleted")
    assert n == 2
    rows = await store.list_for_user("u1")
    by_canvas = {r.canvas_id: r.status for r in rows}
    assert by_canvas == {"cnv_X": "stale", "cnv_Y": "valid"}


@pytest.mark.asyncio
async def test_invalidate_by_section_scoped(store):
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="a",
        canvas_id="cnv",
        section_id="sec_1",
        verifier_result_hash="h",
    )
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="b",
        canvas_id="cnv",
        section_id="sec_2",
        verifier_result_hash="h",
    )
    n = await store.invalidate_by_section(
        canvas_id="cnv",
        section_id="sec_1",
        reason="section_deleted",
    )
    assert n == 1
    rows = await store.list_for_user("u1")
    by_sec = {r.section_id: r.status for r in rows}
    assert by_sec == {"sec_1": "stale", "sec_2": "valid"}


# ─── recheck_hash ────────────────────────────────────────


@pytest.mark.asyncio
async def test_recheck_hash_detects_payload_change(store):
    # Insert anchor with hash computed from canonical {"a":1}
    import hashlib
    import json

    payload_old = {"a": 1}
    h_old = hashlib.sha256(
        json.dumps(payload_old, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="x",
        canvas_id="c",
        section_id="s",
        verifier_result_hash=h_old,
    )
    # New payload changes → recheck
    payload_new = {"a": 2}
    n = await store.recheck_hash(canvas_id="c", section_id="s", new_payload=payload_new)
    assert n == 1
    a = await store.get_by_bullet(user_id="u1", bullet_text="x")
    assert a.status == "stale"
    assert a.invalidation_reason == "payload_changed"


@pytest.mark.asyncio
async def test_recheck_hash_no_change_noop(store):
    import hashlib
    import json

    payload = {"a": 1}
    h = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="x",
        canvas_id="c",
        section_id="s",
        verifier_result_hash=h,
    )
    n = await store.recheck_hash(canvas_id="c", section_id="s", new_payload=payload)
    assert n == 0
    a = await store.get_by_bullet(user_id="u1", bullet_text="x")
    assert a.status == "valid"


# ─── delete_by_bullet ────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_by_bullet_cascade(store):
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="Q1 营收",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h",
    )
    n = await store.delete_by_bullet(user_id="u1", bullet_text="  q1 营收  ")
    assert n == 1
    rows = await store.list_for_user("u1")
    assert rows == []


# ─── hard_delete ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_hard_delete_user(store):
    await store.insert_or_revive(
        user_id="u_gone",
        bullet_text="a",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h",
    )
    await store.insert_or_revive(
        user_id="u_keep",
        bullet_text="b",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h",
    )
    n = await store.hard_delete_user("u_gone")
    assert n == 1
    assert await store.list_for_user("u_keep") != []
    assert await store.list_for_user("u_gone") == []


@pytest.mark.asyncio
async def test_hard_delete_old_invalidated_ttl(store):
    """Spike #19 datetime() normalize: ISO 'T' format must be parsed by SQL datetime()."""
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="old",
        canvas_id="c1",
        section_id="s",
        verifier_result_hash="h",
    )
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="fresh",
        canvas_id="c2",
        section_id="s",
        verifier_result_hash="h",
    )
    await store.invalidate_by_canvas(canvas_id="c1")  # mark stale, invalidated_at=now
    await store.invalidate_by_canvas(canvas_id="c2")  # mark stale, invalidated_at=now

    # Age the first to 200 days
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE memory_note_anchors SET invalidated_at=datetime('now', '-200 days') WHERE canvas_id='c1'",
        )
        await db.commit()

    purged = await store.hard_delete_old_invalidated(max_age_days=180)
    assert purged == 1
    rows = await store.list_for_user("u1")
    assert len(rows) == 1
    assert rows[0].canvas_id == "c2"


@pytest.mark.asyncio
async def test_hard_delete_old_invalidated_skips_valid(store):
    """Valid anchors (invalidated_at IS NULL) never purged regardless of age."""
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="a",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h",
    )
    purged = await store.hard_delete_old_invalidated(max_age_days=0)
    assert purged == 0


# ─── mark_orphan_anchors ─────────────────────────────────


@pytest.mark.asyncio
async def test_mark_orphan_anchors_detects_missing_bullet(store, tmp_path):
    """If memory_notes content no longer contains the bullet, anchor goes stale."""
    from bowhead_core.memory_notes import MemoryNotesStore

    notes = MemoryNotesStore(store._db_path)
    await notes.initialize()
    await notes.upsert_content(user_id="u1", content="📌 Q1 营收\n• 其他事实")
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="Q1 营收",
        canvas_id="c",
        section_id="s",
        verifier_result_hash="h",
    )
    await store.insert_or_revive(
        user_id="u1",
        bullet_text="其他事实",
        canvas_id="c",
        section_id="s2",
        verifier_result_hash="h",
    )
    # User PATCHes notes, removing "其他事实"
    await notes.upsert_content(user_id="u1", content="📌 Q1 营收")

    n = await store.mark_orphan_anchors(notes_store=notes)
    assert n == 1
    rows = await store.list_for_user("u1")
    by_text = {r.bullet_text: r for r in rows}
    assert by_text["Q1 营收"].status == "valid"
    assert by_text["其他事实"].status == "stale"
    assert by_text["其他事实"].invalidation_reason == "bullet_text_lost"
