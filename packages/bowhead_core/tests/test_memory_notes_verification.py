"""V4 Sprint 1 PRD-D-5 · MemoryNotesStore verification_anchor + save_from_canvas tests.

Covers:
  - schema: new column verification_anchor (TEXT/JSON nullable) + verification_status default 'valid'
  - schema: ALTER on legacy DB (PRD-C ship before V4) succeeds
  - save_from_canvas: appends bullet, sets anchor, status='valid', source='verified_in_report'
  - save_from_canvas: rejects empty / >200 char summary
  - save_from_canvas: each call refreshes anchor (later save wins for the row)
  - save_from_canvas: cannot bypass an existing About-you editing lease
  - JSON round-trip via _row_to_note
"""

from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.memory_notes import MemoryNotesStore, NotesLockedError


@pytest.fixture
async def store(tmp_path):
    s = MemoryNotesStore(tmp_path / "notes_v.db")
    await s.initialize()
    return s


# ─── schema ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schema_has_new_columns(store):
    async with aiosqlite.connect(store._db_path) as db, db.execute("PRAGMA table_info(memory_notes)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    assert "verification_anchor" in cols
    assert "verification_status" in cols


@pytest.mark.asyncio
async def test_alter_on_legacy_db_succeeds(tmp_path):
    """Simulate a PRD-C-shipped DB without the V4 columns; initialize() ALTERs them."""
    db_path = tmp_path / "legacy.db"
    # 1. Manually create legacy schema (no V4 columns)
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """CREATE TABLE memory_notes (
                id TEXT PRIMARY KEY,
                subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
                content TEXT NOT NULL, source TEXT NOT NULL,
                source_turn_id TEXT, editing_locked_until TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                deleted_at TEXT,
                UNIQUE(subject_type, subject_id))""",
        )
        await db.commit()
    # 2. Run V4 initialize → must ALTER columns
    s = MemoryNotesStore(db_path)
    await s.initialize()
    # 3. Verify columns exist
    async with aiosqlite.connect(str(db_path)) as db, db.execute("PRAGMA table_info(memory_notes)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    assert "verification_anchor" in cols
    assert "verification_status" in cols
    # 4. Run initialize again (idempotent — ALTER fails silently on existing)
    await s.initialize()


# ─── save_from_canvas ────────────────────────────────────


@pytest.mark.asyncio
async def test_save_from_canvas_appends_bullet_and_sets_anchor(store):
    note = await store.save_from_canvas(
        user_id="u1",
        canvas_id="cnv_abc",
        section_id="sec_01",
        summary_text="Q1 营收 1200万 (vs 1000万 Q4)",
        verifier_result_hash="sha256_abcdef",
    )
    assert note.verification_status == "valid"
    assert note.verification_anchor is not None
    assert note.verification_anchor["canvas_id"] == "cnv_abc"
    assert note.verification_anchor["section_id"] == "sec_01"
    assert note.verification_anchor["verifier_result_hash"] == "sha256_abcdef"
    assert "saved_at" in note.verification_anchor
    # bullet appended with 📌 prefix (pinned default True) and the summary
    assert "📌" in note.content
    assert "Q1 营收 1200万" in note.content
    assert note.source == "verified_in_report"


@pytest.mark.asyncio
async def test_save_from_canvas_rejects_empty(store):
    with pytest.raises(ValueError):
        await store.save_from_canvas(
            user_id="u1",
            canvas_id="cnv",
            section_id="sec",
            summary_text="",
            verifier_result_hash="h",
        )
    with pytest.raises(ValueError):
        await store.save_from_canvas(
            user_id="u1",
            canvas_id="cnv",
            section_id="sec",
            summary_text="   \n  ",
            verifier_result_hash="h",
        )


@pytest.mark.asyncio
async def test_save_from_canvas_rejects_over_200_char(store):
    with pytest.raises(ValueError):
        await store.save_from_canvas(
            user_id="u1",
            canvas_id="cnv",
            section_id="sec",
            summary_text="x" * 201,
            verifier_result_hash="h",
        )


@pytest.mark.asyncio
async def test_save_from_canvas_unpinned(store):
    note = await store.save_from_canvas(
        user_id="u1",
        canvas_id="cnv",
        section_id="sec",
        summary_text="some fact",
        verifier_result_hash="h",
        pinned=False,
    )
    # 📌 only used for pinned; unpinned uses •
    assert "•" in note.content
    assert "📌 some fact" not in note.content


@pytest.mark.asyncio
async def test_save_from_canvas_dedupes_same_summary(store):
    """Per design §2.3: clicking save twice with same text → no duplicate bullet."""
    note1 = await store.save_from_canvas(
        user_id="u1",
        canvas_id="cnv",
        section_id="sec",
        summary_text="同一事实",
        verifier_result_hash="h1",
    )
    n1_count = note1.content.count("同一事实")
    assert n1_count == 1

    note2 = await store.save_from_canvas(
        user_id="u1",
        canvas_id="cnv",
        section_id="sec",
        summary_text="同一事实",
        verifier_result_hash="h1",
    )
    # Still only one occurrence even after second save click
    assert note2.content.count("同一事实") == 1
    # Anchor refreshes to reflect latest save click
    assert note2.verification_anchor["verifier_result_hash"] == "h1"


@pytest.mark.asyncio
async def test_save_from_canvas_subsequent_save_wins(store):
    """Per-row anchor: the latest save updates the row's anchor."""
    await store.save_from_canvas(
        user_id="u1",
        canvas_id="cnv_old",
        section_id="sec_old",
        summary_text="first",
        verifier_result_hash="h1",
    )
    note = await store.save_from_canvas(
        user_id="u1",
        canvas_id="cnv_new",
        section_id="sec_new",
        summary_text="second",
        verifier_result_hash="h2",
    )
    # Both bullets present
    assert "first" in note.content
    assert "second" in note.content
    # But anchor is the latest one (V4 limitation: per-row, not per-bullet)
    assert note.verification_anchor["canvas_id"] == "cnv_new"
    assert note.verification_anchor["section_id"] == "sec_new"


@pytest.mark.asyncio
async def test_save_from_canvas_cannot_bypass_editing_lease(store):
    """Canvas writes must not overwrite or append while About-you is edited."""

    locked = await store.acquire_lock(user_id="u1", ttl_seconds=300)
    assert locked is True
    with pytest.raises(NotesLockedError):
        await store.save_from_canvas(
            user_id="u1",
            canvas_id="cnv",
            section_id="sec",
            summary_text="lock-bypassing fact",
            verifier_result_hash="h",
        )


# ─── _row_to_note JSON round-trip ────────────────────────


@pytest.mark.asyncio
async def test_anchor_json_round_trip(store):
    await store.save_from_canvas(
        user_id="u1",
        canvas_id="cnv_x",
        section_id="sec_y",
        summary_text="hello",
        verifier_result_hash="hashy",
    )
    note = await store.get(user_id="u1")
    assert note is not None
    assert note.verification_anchor == {
        "canvas_id": "cnv_x",
        "section_id": "sec_y",
        "verifier_result_hash": "hashy",
        "saved_at": note.verification_anchor["saved_at"],  # echo timestamp
    }
    assert note.verification_status == "valid"


@pytest.mark.asyncio
async def test_default_status_for_pre_v4_rows(store):
    """A row written via plain upsert (no anchor) should default status='valid', anchor=None."""
    await store.upsert_content(user_id="u1", content="plain content no anchor")
    note = await store.get(user_id="u1")
    assert note is not None
    assert note.verification_anchor is None
    assert note.verification_status == "valid"


@pytest.mark.asyncio
async def test_corrupt_anchor_json_returns_none(store, tmp_path):
    """If verification_anchor JSON is malformed, _row_to_note returns None for it."""
    await store.upsert_content(user_id="u1", content="content")
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE memory_notes SET verification_anchor='{invalid json' WHERE subject_id=?",
            ("u1",),
        )
        await db.commit()
    note = await store.get(user_id="u1")
    assert note is not None
    assert note.verification_anchor is None
