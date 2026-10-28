"""Sprint chat-redesign-2026-05-21 Phase 1.1 — steer mode session_store methods.

Covers:
- Schema migration adds active_turn_id + steer_buffer_json columns.
- record_turn owns active_turn_id for a new legacy Turn.
- append_steer_buffer accumulates msgs in chronological order.
- finalize_turn drains + clears/swaps active_turn_id atomically.
- cleanup_stale_running_turns clears active+buffer for stale failed turns (R14).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import open_shared_ctx
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.session_store import SessionStore

if TYPE_CHECKING:
    from pathlib import Path


async def _record_active_turn(
    store: SessionStore,
    *,
    session_id: str,
    turn_id: str,
) -> None:
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text=f"fixture {turn_id}",
    )


async def _buffered_texts(store: SessionStore, session_id: str) -> list[str]:
    async with (
        open_shared_ctx(store.db_path) as db,
        db.execute(
            "SELECT steer_buffer_json FROM sessions WHERE id=?",
            (session_id,),
        ) as cursor,
    ):
        row = await cursor.fetchone()
    buffer = json.loads((row[0] if row else None) or "[]")
    return [item.get("text", "") for item in buffer if isinstance(item, dict)]


@pytest.mark.asyncio
async def test_schema_adds_active_turn_id_and_steer_buffer(tmp_path: Path) -> None:
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    async with open_shared_ctx(s.db_path) as db, db.execute("PRAGMA table_info(sessions)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    assert "active_turn_id" in cols
    assert "steer_buffer_json" in cols


@pytest.mark.asyncio
async def test_get_active_turn_defaults_none(tmp_path: Path) -> None:
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u")
    assert await s.get_active_turn("s1") is None


@pytest.mark.asyncio
async def test_get_active_turn_returns_none_for_unknown_session(tmp_path: Path) -> None:
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    assert await s.get_active_turn("nope") is None


@pytest.mark.asyncio
async def test_append_steer_buffer_accumulates_in_order(tmp_path: Path) -> None:
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u")
    await _record_active_turn(s, session_id="s1", turn_id="t_active")
    ok1 = await s.append_steer_buffer(session_id="s1", text="第一条")
    ok2 = await s.append_steer_buffer(session_id="s1", text="第二条")
    ok3 = await s.append_steer_buffer(session_id="s1", text="third")
    assert ok1 is True and ok2 is True and ok3 is True
    # peek via raw query to verify shape
    async with (
        open_shared_ctx(s.db_path) as db,
        db.execute(
            "SELECT steer_buffer_json FROM sessions WHERE id=?",
            ("s1",),
        ) as cur,
    ):
        row = await cur.fetchone()
    buf = json.loads(row[0])
    assert len(buf) == 3
    assert [b["text"] for b in buf] == ["第一条", "第二条", "third"]
    for b in buf:
        assert "ts" in b


@pytest.mark.asyncio
async def test_append_steer_buffer_refused_when_no_active_turn(tmp_path: Path) -> None:
    """Phase 1.1 P1: append returns False if active_turn_id is None inside the tx."""
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u")
    # No admitted Turn → buffer append must refuse.
    ok = await s.append_steer_buffer(session_id="s1", text="orphan")
    assert ok is False
    # Buffer remains empty
    async with (
        open_shared_ctx(s.db_path) as db,
        db.execute(
            "SELECT steer_buffer_json FROM sessions WHERE id=?",
            ("s1",),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert json.loads(row[0]) == []


@pytest.mark.asyncio
async def test_append_steer_buffer_refused_when_expected_turn_changed(tmp_path: Path) -> None:
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u")
    await _record_active_turn(s, session_id="s1", turn_id="t_new")

    ok = await s.append_steer_buffer(
        session_id="s1",
        text="stale steer",
        expected_turn_id="t_old",
    )

    assert ok is False
    assert await s.get_active_turn("s1") == "t_new"
    async with (
        open_shared_ctx(s.db_path) as db,
        db.execute(
            "SELECT steer_buffer_json FROM sessions WHERE id=?",
            ("s1",),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert json.loads(row[0]) == []


@pytest.mark.asyncio
async def test_concurrent_appends_serialize_safely(tmp_path: Path) -> None:
    """BEGIN IMMEDIATE ensures concurrent appends don't lose data."""
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u")
    await _record_active_turn(s, session_id="s1", turn_id="t_active")
    # Fire 20 concurrent appends
    results = await asyncio.gather(*[s.append_steer_buffer(session_id="s1", text=f"msg_{i}") for i in range(20)])
    assert all(r is True for r in results)
    texts = await _buffered_texts(s, "s1")
    assert len(texts) == 20
    # Order isn't deterministic under concurrency, but every msg must be present
    assert set(texts) == {f"msg_{i}" for i in range(20)}


@pytest.mark.asyncio
async def test_finalize_turn_empty_buffer_clears_active(tmp_path: Path) -> None:
    """Phase 1.1 P1: finalize_turn with empty buffer → active=NULL, return []."""
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u")
    await _record_active_turn(s, session_id="s1", turn_id="t_orig")
    texts = await s.finalize_turn(
        session_id="s1",
        current_turn_id="t_orig",
        next_turn_id="t_next_unused",
    )
    assert texts == []
    assert await s.get_active_turn("s1") is None


@pytest.mark.asyncio
async def test_finalize_turn_nonempty_buffer_swaps_active(tmp_path: Path) -> None:
    """Phase 1.1 P1: finalize_turn with buffer → swap active to next_turn_id."""
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u")
    await _record_active_turn(s, session_id="s1", turn_id="t_orig")
    await s.append_steer_buffer(session_id="s1", text="continuation")
    texts = await s.finalize_turn(
        session_id="s1",
        current_turn_id="t_orig",
        next_turn_id="t_next",
    )
    assert texts == ["continuation"]
    # Active swapped atomically
    assert await s.get_active_turn("s1") == "t_next"


@pytest.mark.asyncio
async def test_finalize_turn_nonempty_buffer_can_drop_without_next_turn(tmp_path: Path) -> None:
    """Entitlement/cancel paths can drain buffer while clearing active."""
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u")
    await _record_active_turn(s, session_id="s1", turn_id="t_orig")
    await s.append_steer_buffer(session_id="s1", text="drop me")
    texts = await s.finalize_turn(
        session_id="s1",
        current_turn_id="t_orig",
        next_turn_id=None,
    )
    assert texts == ["drop me"]
    assert await s.get_active_turn("s1") is None
    assert await _buffered_texts(s, "s1") == []


@pytest.mark.asyncio
async def test_finalize_turn_active_already_swapped_aborts(tmp_path: Path) -> None:
    """Phase 1.1 P1: if active != current (another path swapped), finalize is a no-op."""
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u")
    await _record_active_turn(s, session_id="s1", turn_id="t_other")
    texts = await s.finalize_turn(
        session_id="s1",
        current_turn_id="t_orig",
        next_turn_id="t_next",
    )
    assert texts == []
    # Active state unchanged
    assert await s.get_active_turn("s1") == "t_other"


@pytest.mark.asyncio
async def test_cleanup_stale_running_turns_clears_active_and_buffer(tmp_path: Path) -> None:
    """Phase 1.1 R14: when a stale turn is marked failed, the session's
    active_turn_id + steer_buffer must clear so gateway doesn't perpetually
    intercept new msgs as steer for a turn that never finishes.
    """
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u")

    # Manually insert a stale running turn (started 30 min ago) + wire session active_turn
    stale_started = (datetime.now(tz=UTC) - timedelta(minutes=30)).isoformat(timespec="seconds")
    async with open_shared_ctx(s.db_path) as db:
        await db.execute(
            "INSERT INTO turns(id, session_id, seq, user_text, status, started_at) VALUES ('t_stale', 's1', 1, 'q', 'running', ?)",
            (stale_started,),
        )
        await db.execute(
            "UPDATE sessions SET active_turn_id='t_stale' WHERE id='s1'",
        )
        await db.commit()
    await s.append_steer_buffer(session_id="s1", text="dropped msg")

    n = await s.cleanup_stale_running_turns(older_than_min=10)
    assert n == 1
    # Active turn cleared
    assert await s.get_active_turn("s1") is None
    # Buffer cleared (cleanup drops anything tied to stale turn)
    assert await _buffered_texts(s, "s1") == []
    # 0708 ⑩: the cleaned turn records status=failed + error_kind=interrupted so the
    # failure carries its cause (FE surfaces it / fails stuck "running" steps).
    async with open_shared_ctx(s.db_path) as db, db.execute("SELECT status, error_kind FROM turns WHERE id='t_stale'") as cur:
        row = await cur.fetchone()
    assert row[0] == "failed"
    assert row[1] == "interrupted"


@pytest.mark.asyncio
async def test_cleanup_stale_running_turn_preserves_accepted_steer_for_recovery(
    tmp_path: Path,
) -> None:
    """Age-bounded legacy cleanup transfers, rather than deletes, accepted work."""
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    store = SessionStore(db_path)
    await store.initialize()
    await store.ensure_session(
        session_id="s_stale_buffered",
        workspace_id="ws_1",
        user_id="u_1",
    )
    # The Store primitive accepts a caller-owned threshold; production uses a
    # much older rolling-safe age fence, while this fixture exercises transfer.
    old = (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat()
    async with open_shared_ctx(store.db_path) as db:
        await db.execute(
            "INSERT INTO turns(id,session_id,seq,user_text,status,started_at) VALUES ('t_stale_buffered','s_stale_buffered',1,'q','running',?)",
            (old,),
        )
        await db.execute(
            "UPDATE sessions SET active_turn_id='t_stale_buffered' WHERE id='s_stale_buffered'",
        )
        await db.commit()
    assert await store.append_steer_buffer(
        session_id="s_stale_buffered",
        expected_turn_id="t_stale_buffered",
        text="accepted before crash",
    )

    # Gateway setup begins with initialize(). Schema setup must
    # not terminalize the parent or erase acknowledged work before recovery.
    await store.initialize()
    parent = await store.get_turn("t_stale_buffered")
    assert parent is not None and parent.status == "running"
    assert await store.get_active_turn("s_stale_buffered") == "t_stale_buffered"

    assert await store.recover_expired_chat_spawn_dispatches() == []
    assert await store.cleanup_stale_running_turns(older_than_min=0) == 1
    assert await store.get_active_turn("s_stale_buffered") == "t_stale_buffered"
    async with (
        open_shared_ctx(store.db_path) as db,
        db.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_stale_buffered'",
        ) as cursor,
    ):
        assert await cursor.fetchone() == ("drain_pending",)

    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_stale_buffered",
    ]
    assert await store.get_active_turn("s_stale_buffered") is None
    assert (
        await store.get_chat_follow_up(
            parent_turn_id="t_stale_buffered",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_cleanup_clears_pointer_to_completed_turn(tmp_path: Path) -> None:
    """empty-canvas follow-up: 死指针不只来自 failed turn。

    `record_turn` 在 **store 层**认领 active_turn_id（所有通道），释放却只在 chat
    worker 的 finalize_turn ⇒ 不走 chat worker 的通道（定时任务直接调
    orchestrator.run_turn）跑完会留下指向 **completed** turn 的指针。旧判据只认
    `status='failed'`，永远清不掉它，而 steer_or_proceed 见到非空指针就把用户消息
    buffer 进那个已结束的 turn（chat 直接返回不 spawn）⇒ 静默吞消息。
    """
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s_sched", workspace_id="w", user_id="u")

    old = (datetime.now(tz=UTC) - timedelta(minutes=30)).isoformat(timespec="seconds")
    async with open_shared_ctx(s.db_path) as db:
        await db.execute(
            "INSERT INTO turns(id, session_id, seq, user_text, status, started_at, ended_at) "
            "VALUES ('t_done', 's_sched', 1, 'routine', 'completed', ?, ?)",
            (old, old),
        )
        await db.execute(
            "UPDATE sessions SET active_turn_id='t_done' WHERE id='s_sched'",
        )
        await db.commit()

    await s.cleanup_stale_running_turns(older_than_min=10)

    assert await s.get_active_turn("s_sched") is None
    # Do not change a completed turn to failed; this only clears the pointer.
    async with open_shared_ctx(s.db_path) as db, db.execute("SELECT status FROM turns WHERE id='t_done'") as cur:
        row = await cur.fetchone()
    assert row[0] == "completed"


@pytest.mark.asyncio
async def test_cleanup_keeps_pointer_to_recent_terminal_turn(tmp_path: Path) -> None:
    """年龄守卫：刚结束的 turn 与 finalize_turn（drain steer buffer 换下一轮）之间有个
    合法窗口，指针此时正当地指向 completed turn，不能被清。"""
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s_fresh", workspace_id="w", user_id="u")

    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    async with open_shared_ctx(s.db_path) as db:
        await db.execute(
            "INSERT INTO turns(id, session_id, seq, user_text, status, started_at, ended_at) "
            "VALUES ('t_just_done', 's_fresh', 1, 'q', 'completed', ?, ?)",
            (now, now),
        )
        await db.execute(
            "UPDATE sessions SET active_turn_id='t_just_done' WHERE id='s_fresh'",
        )
        await db.commit()

    await s.cleanup_stale_running_turns(older_than_min=10)

    assert await s.get_active_turn("s_fresh") == "t_just_done"
