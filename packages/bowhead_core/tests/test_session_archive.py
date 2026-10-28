"""Option A session archive persistence and concurrency contracts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.session_store import SessionStore

if TYPE_CHECKING:
    from pathlib import Path


async def _session(
    store: SessionStore,
    *,
    session_id: str,
    user_id: str = "u1",
    workspace_id: str = "w1",
) -> None:
    await store.ensure_session(
        session_id=session_id,
        workspace_id=workspace_id,
        user_id=user_id,
    )


@pytest.fixture
async def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionStore:
    value = SessionStore(tmp_path / "sessions.db")
    await value.initialize()
    return value


@pytest.mark.asyncio
async def test_archive_schema_migration_is_idempotent(store: SessionStore) -> None:
    await store.initialize()
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_session_archives'",
        ) as cur,
    ):
        assert await cur.fetchone() == ("user_session_archives",)


@pytest.mark.asyncio
async def test_archive_restore_are_idempotent_and_preserve_pin(
    store: SessionStore,
) -> None:
    await _session(store, session_id="s1")
    await store.pin_session(user_id="u1", session_id="s1")

    assert await store.archive_session(user_id="u1", session_id="s1") == "ok"
    assert await store.archive_session(user_id="u1", session_id="s1") == "ok"
    assert await store.list_sessions(user_id="u1", archive_view="active") == []

    archived = await store.list_sessions(user_id="u1", archive_view="archived")
    assert [session.id for session in archived] == ["s1"]
    assert archived[0].archived_at is not None
    assert archived[0].pinned_at is not None
    assert await store.count_archived_sessions(user_id="u1") == 1

    assert await store.restore_session(user_id="u1", session_id="s1") == "ok"
    assert await store.restore_session(user_id="u1", session_id="s1") == "ok"
    active = await store.list_sessions(user_id="u1", archive_view="active")
    assert [session.id for session in active] == ["s1"]
    assert active[0].pinned_at is not None
    assert active[0].archived_at is None


@pytest.mark.asyncio
async def test_archive_conceals_foreign_deleted_and_missing_sessions(
    store: SessionStore,
) -> None:
    await _session(store, session_id="foreign", user_id="u2")
    await _session(store, session_id="deleted")
    await store.soft_delete_session("deleted")

    for session_id in ("foreign", "deleted", "missing"):
        assert await store.archive_session(user_id="u1", session_id=session_id) == "not_found"
        assert await store.restore_session(user_id="u1", session_id=session_id) == "not_found"


@pytest.mark.asyncio
async def test_archived_list_filters_orders_and_counts_before_query_limit(
    store: SessionStore,
) -> None:
    for session_id, title in (
        ("s1", "Quarterly revenue"),
        ("s2", "Quarterly margin"),
        ("s3", "Other work"),
    ):
        await _session(store, session_id=session_id)
        await store.set_title(session_id=session_id, title=title)
        assert await store.archive_session(user_id="u1", session_id=session_id) == "ok"

    async with aiosqlite.connect(store.db_path) as db:
        for session_id, archived_at in (
            ("s1", "2026-07-26T00:00:00+00:00"),
            ("s2", "2026-07-28T00:00:00+00:00"),
            ("s3", "2026-07-27T00:00:00+00:00"),
        ):
            await db.execute(
                "UPDATE user_session_archives SET archived_at=? WHERE session_id=?",
                (archived_at, session_id),
            )
        await db.commit()

    rows = await store.list_sessions(
        user_id="u1",
        archive_view="archived",
        q="Quarterly",
        limit=1,
    )
    assert [session.id for session in rows] == ["s2"]
    assert await store.count_archived_sessions(user_id="u1") == 3


@pytest.mark.asyncio
async def test_running_turn_blocks_archive_even_before_active_marker(
    store: SessionStore,
) -> None:
    await _session(store, session_id="s1")
    await store.record_turn(turn_id="t1", session_id="s1", user_text="run")
    # Reproduce the old Web ordering gap: persisted running turn, marker absent.
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE sessions SET active_turn_id=NULL WHERE id='s1'",
        )
        await db.commit()

    assert await store.archive_session(user_id="u1", session_id="s1") == "active"


@pytest.mark.asyncio
async def test_manual_recovery_clears_interrupted_active_marker(
    store: SessionStore,
) -> None:
    """The explicit cleanup primitive remains available after rollout drain."""
    await _session(store, session_id="s1")
    await store.record_turn(turn_id="t1", session_id="s1", user_text="run")

    assert await store.cleanup_stale_running_turns(older_than_min=0) == 1

    assert await store.get_active_turn("s1") is None
    assert await store.archive_session(user_id="u1", session_id="s1") == "ok"


@pytest.mark.asyncio
async def test_new_turn_reactivates_archive_but_terminal_replay_does_not(
    store: SessionStore,
) -> None:
    await _session(store, session_id="s1")
    await store.record_turn(turn_id="t1", session_id="s1", user_text="first")
    await store.mark_completed(
        turn_id="t1",
        intent="lookup",
        final_answer="done",
        tokens_input=0,
        tokens_output=0,
        cost_usd=0,
    )
    await store.finalize_turn(
        session_id="s1",
        current_turn_id="t1",
        next_turn_id=None,
    )
    assert await store.archive_session(user_id="u1", session_id="s1") == "ok"

    # Re-running a terminal Turn would re-dispatch provider work. The fresh
    # writer rejects that replay and must leave the archive projection intact.
    before = await store.get_session("s1")
    assert before is not None
    with pytest.raises(ValueError, match="submission_turn_conflict"):
        await store.record_turn(turn_id="t1", session_id="s1", user_text="first")
    after = await store.get_session("s1")
    assert after is not None
    assert after.last_active_at == before.last_active_at
    assert [s.id for s in await store.list_sessions(user_id="u1", archive_view="archived")] == ["s1"]

    await store.record_turn(turn_id="t2", session_id="s1", user_text="new activity")
    assert [s.id for s in await store.list_sessions(user_id="u1", archive_view="active")] == ["s1"]
    assert await store.list_sessions(user_id="u1", archive_view="archived") == []


@pytest.mark.asyncio
async def test_archive_and_new_turn_race_always_finishes_active(
    store: SessionStore,
) -> None:
    await _session(store, session_id="s1")

    archive_result, _turn = await asyncio.gather(
        store.archive_session(user_id="u1", session_id="s1"),
        store.record_turn(turn_id="t1", session_id="s1", user_text="new"),
    )

    assert archive_result in {"ok", "active"}
    assert [s.id for s in await store.list_sessions(user_id="u1", archive_view="active")] == ["s1"]
    assert await store.list_sessions(user_id="u1", archive_view="archived") == []
