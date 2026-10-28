"""V3.5 PRD-C · MemoryNotesStore unit tests (~12 cases).

Covers:
  - get/upsert basic CRUD + auto-restore on conflict
  - char limit (NotesCharLimitError) at upsert + append
  - acquire_lock CAS (concurrent race → exactly one wins)
  - lock auto-expire after TTL
  - lock blocks append (NotesLockedError)
  - delete_bullet by index (skips freetext correctly)
  - soft_delete + hard_delete_old_trash
  - hard_delete_user
  - parse_notes_lines helper
  - acquire_lock for new user (INSERT OR IGNORE placeholder)
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from bowhead_core.memory_notes import (
    MAX_BULLET_CHARS,
    MAX_CONTENT_CHARS,
    MemoryNotesStore,
    NotesCharLimitError,
    NotesEditTokenError,
    NotesLockedError,
    NotesVersionConflictError,
    parse_notes_lines,
)


@pytest.fixture
async def store(tmp_path):
    s = MemoryNotesStore(tmp_path / "notes.db")
    await s.initialize()
    return s


# ─── basic CRUD ───────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_none_for_new_user(store):
    n = await store.get(user_id="u_new")
    assert n is None


@pytest.mark.asyncio
async def test_upsert_then_get_roundtrip(store):
    n = await store.upsert_content(user_id="u1", content="我是审计师")
    assert n.content == "我是审计师"
    got = await store.get(user_id="u1")
    assert got is not None
    assert got.content == "我是审计师"
    assert got.deleted_at is None


@pytest.mark.asyncio
async def test_upsert_replaces_existing(store):
    await store.upsert_content(user_id="u1", content="v1")
    await store.upsert_content(user_id="u1", content="v2")
    got = await store.get(user_id="u1")
    assert got.content == "v2"


@pytest.mark.asyncio
async def test_upsert_auto_restores_soft_deleted(store):
    await store.upsert_content(user_id="u1", content="v1")
    await store.soft_delete(user_id="u1")
    assert await store.get(user_id="u1") is None
    # Re-upsert clears deleted_at
    await store.upsert_content(user_id="u1", content="v2")
    got = await store.get(user_id="u1")
    assert got is not None
    assert got.content == "v2"
    assert got.deleted_at is None


# ─── char limit ───────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_rejects_over_limit(store):
    huge = "x" * (MAX_CONTENT_CHARS + 1)
    with pytest.raises(NotesCharLimitError) as exc:
        await store.upsert_content(user_id="u1", content=huge)
    assert exc.value.length == MAX_CONTENT_CHARS + 1


@pytest.mark.asyncio
async def test_append_rejects_oversize_total(store):
    """Append fails if resulting total content > MAX_CONTENT_CHARS."""
    near_limit = "x" * (MAX_CONTENT_CHARS - 5)
    await store.upsert_content(user_id="u1", content=near_limit)
    with pytest.raises(NotesCharLimitError):
        await store.append(user_id="u1", bullet="a" * 100)


@pytest.mark.asyncio
async def test_append_rejects_per_bullet_cap(store):
    """Single bullet > MAX_BULLET_CHARS → ValueError before total check."""
    with pytest.raises(ValueError):
        await store.append(user_id="u1", bullet="x" * (MAX_BULLET_CHARS + 1))


# ─── append + delete bullet ───────────────────────────


@pytest.mark.asyncio
async def test_append_pinned_then_bullet(store):
    await store.append(user_id="u1", bullet="重点", pinned=True)
    await store.append(user_id="u1", bullet="正常", pinned=False)
    n = await store.get(user_id="u1")
    assert "📌 重点" in n.content
    assert "• 正常" in n.content


@pytest.mark.asyncio
async def test_delete_bullet_skips_freetext(store):
    await store.upsert_content(
        user_id="u1",
        content="我是审计师\n📌 Q3 cross-check\n• 正式书面语\n\n• RPA = robotic process",
    )
    # Bullets in order: 📌 Q3 (idx 0), • 正式 (idx 1), • RPA (idx 2). freetext + blank skipped.
    await store.delete_bullet(user_id="u1", bullet_index=1)
    n = await store.get(user_id="u1")
    assert "正式书面语" not in n.content
    assert "📌 Q3 cross-check" in n.content
    assert "RPA" in n.content
    # Freetext "我是审计师" preserved
    assert "我是审计师" in n.content


@pytest.mark.asyncio
async def test_delete_bullet_out_of_range(store):
    await store.upsert_content(user_id="u1", content="• one")
    with pytest.raises(IndexError):
        await store.delete_bullet(user_id="u1", bullet_index=5)


# ─── lock CAS race ────────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_lock_first_call_wins(store):
    won = await store.acquire_lock(user_id="u1")
    assert won is True


@pytest.mark.asyncio
async def test_acquire_lock_second_concurrent_blocks(store):
    """Two parallel acquires for same user → exactly one True (sqlite serializes writes)."""

    async def acquire():
        return await store.acquire_lock(user_id="u1", ttl_seconds=300)

    a, b = await asyncio.gather(acquire(), acquire())
    assert (a, b).count(True) == 1


@pytest.mark.asyncio
async def test_acquire_lock_after_ttl_expiry(store, tmp_path):
    """Lock auto-expires; later acquire succeeds without explicit release."""
    await store.acquire_lock(user_id="u1", ttl_seconds=300)
    # Force-age the lock
    async with aiosqlite.connect(str(tmp_path / "notes.db")) as db:
        await db.execute(
            "UPDATE memory_notes SET editing_locked_until=datetime('now', '-1 hour') WHERE subject_type='user' AND subject_id='u1'",
        )
        await db.commit()
    won = await store.acquire_lock(user_id="u1", ttl_seconds=300)
    assert won is True


# ─── lock blocks append ───────────────────────────────


@pytest.mark.asyncio
async def test_append_raises_when_locked(store):
    await store.upsert_content(user_id="u1", content="seed")
    await store.acquire_lock(user_id="u1", ttl_seconds=300)
    with pytest.raises(NotesLockedError):
        await store.append(user_id="u1", bullet="should fail")


@pytest.mark.asyncio
async def test_append_with_ignore_lock_cannot_bypass(store):
    await store.upsert_content(user_id="u1", content="seed")
    await store.acquire_lock(user_id="u1", ttl_seconds=300)
    with pytest.raises(NotesLockedError):
        await store.append(
            user_id="u1",
            bullet="forced",
            source="dreaming_approved",
            ignore_lock=True,
        )


# ─── owner edit lease + version CAS ─────────────────


@pytest.mark.asyncio
async def test_edit_session_commit_requires_owner_token_and_version(store):
    original = await store.upsert_content(user_id="u1", content="before")
    lease = await store.begin_edit_session(
        user_id="u1",
        expected_version=original.version,
    )

    with pytest.raises(NotesEditTokenError):
        await store.commit_edit(
            user_id="u1",
            content="attacker",
            expected_version=original.version,
            edit_token="x" * 43,
        )

    updated = await store.commit_edit(
        user_id="u1",
        content="after",
        expected_version=original.version,
        edit_token=lease.edit_token,
    )
    assert updated.content == "after"
    assert updated.version == original.version + 1
    assert updated.created_source_type == original.created_source_type
    assert updated.last_modified_actor_type == "user"
    assert updated.last_modified_actor_id == "u1"
    assert await store.is_lock_active(user_id="u1") is False


@pytest.mark.asyncio
async def test_edit_session_stale_version_does_not_acquire(store):
    note = await store.upsert_content(user_id="u1", content="current")
    with pytest.raises(NotesVersionConflictError) as exc:
        await store.begin_edit_session(
            user_id="u1",
            expected_version=note.version - 1,
        )
    assert exc.value.current_version == note.version
    assert await store.is_lock_active(user_id="u1") is False


@pytest.mark.asyncio
async def test_second_device_cannot_take_or_release_owner_lease(store):
    note = await store.upsert_content(user_id="u1", content="current")
    lease = await store.begin_edit_session(
        user_id="u1",
        expected_version=note.version,
    )
    with pytest.raises(NotesLockedError):
        await store.begin_edit_session(
            user_id="u1",
            expected_version=note.version,
        )
    assert (
        await store.release_lock(
            user_id="u1",
            edit_token="x" * 43,
        )
        is False
    )
    assert await store.is_lock_active(user_id="u1") is True
    assert (
        await store.release_lock(
            user_id="u1",
            edit_token=lease.edit_token,
        )
        is True
    )


@pytest.mark.asyncio
async def test_edit_lease_does_not_change_content_updated_at(store):
    await store.upsert_content(user_id="u1", content="current")
    original_updated_at = "2020-01-02T03:04:05+00:00"
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE memory_notes SET updated_at=? WHERE subject_type='user' AND subject_id=?",
            (original_updated_at, "u1"),
        )
        await db.commit()

    note = await store.get(user_id="u1")
    assert note is not None
    lease = await store.begin_edit_session(
        user_id="u1",
        expected_version=note.version,
    )
    locked = await store.get(user_id="u1")
    assert locked is not None
    assert locked.updated_at == original_updated_at

    assert (
        await store.release_lock(
            user_id="u1",
            edit_token=lease.edit_token,
        )
        is True
    )
    released = await store.get(user_id="u1")
    assert released is not None
    assert released.updated_at == original_updated_at

    assert await store.acquire_lock(user_id="u1") is True
    legacy_locked = await store.get(user_id="u1")
    assert legacy_locked is not None
    assert legacy_locked.updated_at == original_updated_at
    assert await store.release_lock(user_id="u1") is True
    legacy_released = await store.get(user_id="u1")
    assert legacy_released is not None
    assert legacy_released.updated_at == original_updated_at


@pytest.mark.asyncio
async def test_edit_lease_blocks_all_legacy_writers_even_with_ignore(store):
    note = await store.upsert_content(user_id="u1", content="current")
    await store.begin_edit_session(
        user_id="u1",
        expected_version=note.version,
    )
    with pytest.raises(NotesLockedError):
        await store.upsert_content(user_id="u1", content="background overwrite")
    with pytest.raises(NotesLockedError):
        await store.append(
            user_id="u1",
            bullet="background append",
            ignore_lock=True,
        )


@pytest.mark.asyncio
async def test_edit_cas_does_not_resurrect_note_deleted_after_lease_started(store):
    note = await store.upsert_content(user_id="u1", content="before reset")
    lease = await store.begin_edit_session(
        user_id="u1",
        expected_version=note.version,
    )
    assert await store.soft_delete(user_id="u1") is True

    with pytest.raises(NotesVersionConflictError) as exc:
        await store.commit_edit(
            user_id="u1",
            content="stale draft",
            expected_version=note.version,
            edit_token=lease.edit_token,
        )
    assert exc.value.current_version == note.version + 1
    assert await store.get(user_id="u1") is None


# ─── release lock ─────────────────────────────────────


@pytest.mark.asyncio
async def test_release_lock_clears(store):
    await store.acquire_lock(user_id="u1")
    assert await store.is_lock_active(user_id="u1") is True
    await store.release_lock(user_id="u1")
    assert await store.is_lock_active(user_id="u1") is False


# ─── soft delete + cron ───────────────────────────────


@pytest.mark.asyncio
async def test_soft_delete_then_hard_delete_old(store, tmp_path):
    await store.upsert_content(user_id="u1", content="x")
    assert await store.soft_delete(user_id="u1") is True

    async with aiosqlite.connect(str(tmp_path / "notes.db")) as db:
        await db.execute(
            "UPDATE memory_notes SET deleted_at=datetime('now', '-60 days') WHERE subject_type='user' AND subject_id='u1'",
        )
        await db.commit()

    n = await store.hard_delete_old_trash(max_age_days=30)
    assert n == 1
    # row truly gone
    assert await store.get(user_id="u1", include_trashed=True) is None


@pytest.mark.asyncio
async def test_hard_delete_user_cascade(store):
    await store.upsert_content(user_id="u_gone", content="x")
    await store.upsert_content(user_id="u_keep", content="y")
    n = await store.hard_delete_user("u_gone")
    assert n == 1
    assert await store.get(user_id="u_gone", include_trashed=True) is None
    assert (await store.get(user_id="u_keep")).content == "y"


# ─── parse helper ─────────────────────────────────────


def test_parse_notes_lines_mixed():
    content = "我是审计师\n📌 重点\n• 正常\n\n• 另一条\n freetext line"
    out = parse_notes_lines(content)
    assert out["pinned_count"] == 1
    assert out["bullet_count"] == 2
    assert out["free_text_lines"] == 2  # "我是审计师" + " freetext line"
    assert out["char_count"] == len(content)


def test_parse_notes_lines_empty():
    out = parse_notes_lines("")
    assert out == {"char_count": 0, "pinned_count": 0, "bullet_count": 0, "free_text_lines": 0}


# V4 Sprint 2 — parse_notes_to_lines (per-line, mirrors frontend lib/notes-markdown.ts parseNotes)


def test_parse_notes_to_lines_extracts_text_per_line():
    from bowhead_core.memory_notes import parse_notes_to_lines

    content = "我是审计师\n📌 重点事实\n• 普通条目\n\n- 另一条\n  freetext"
    out = parse_notes_to_lines(content)
    # Match frontend NotesLine[] semantics: kind/lineIndex/bulletIndex/text
    assert [(item["kind"], item["text"]) for item in out] == [
        ("freetext", "我是审计师"),
        ("pinned", "重点事实"),
        ("bullet", "普通条目"),
        ("blank", ""),
        ("bullet", "另一条"),
        ("freetext", "freetext"),
    ]
    # bulletIndex on pinned/bullet only, sequential
    bullet_indices = [item["bulletIndex"] for item in out if item["kind"] in ("pinned", "bullet")]
    assert bullet_indices == [0, 1, 2]


def test_parse_notes_to_lines_empty():
    from bowhead_core.memory_notes import parse_notes_to_lines

    assert parse_notes_to_lines("") == []


def test_parse_notes_to_lines_pinned_uses_single_codepoint():
    """Python str: '📌' is 1 code point. text strip must remove the emoji + 1 space."""
    from bowhead_core.memory_notes import parse_notes_to_lines

    out = parse_notes_to_lines("📌 hello world")
    assert out[0]["kind"] == "pinned"
    assert out[0]["text"] == "hello world"
