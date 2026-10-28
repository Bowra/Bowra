"""W12-3 (#74) session badges 真信号 — set_session_seen / mark_session_skill_applied
+ list_sessions 透传 last_seen_at / skill_applied.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_skill_applied_default_false(tmp_path: Path) -> None:
    """ensure_session 后 skill_applied 默 False."""
    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u1")
    sessions = await s.list_sessions(user_id="u1", workspace_id="w")
    assert len(sessions) == 1
    assert sessions[0].skill_applied is False


@pytest.mark.asyncio
async def test_mark_session_skill_applied_idempotent(tmp_path: Path) -> None:
    """调一次 / 多次 都让 skill_applied=True, 不抛错."""
    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u1")
    await s.mark_session_skill_applied(session_id="s1")
    sessions = await s.list_sessions(user_id="u1", workspace_id="w")
    assert sessions[0].skill_applied is True
    # Call again; still True (idempotent)
    await s.mark_session_skill_applied(session_id="s1")
    sessions2 = await s.list_sessions(user_id="u1", workspace_id="w")
    assert sessions2[0].skill_applied is True


@pytest.mark.asyncio
async def test_list_sessions_last_seen_at_null_initially(tmp_path: Path) -> None:
    """user 未 view 该 session → list_sessions 返 last_seen_at=None."""
    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u1")
    sessions = await s.list_sessions(user_id="u1", workspace_id="w")
    assert sessions[0].last_seen_at is None


@pytest.mark.asyncio
async def test_list_sessions_query_stays_owner_scoped(tmp_path: Path) -> None:
    from bowhead_core.session_store import SessionStore

    store = SessionStore(str(tmp_path / "sessions.db"))
    await store.initialize()
    owned = await store.ensure_session(
        session_id=None,
        workspace_id="ws_1",
        user_id="u1",
    )
    other = await store.ensure_session(
        session_id=None,
        workspace_id="ws_1",
        user_id="u2",
    )
    await store.touch_session(
        session_id=owned.id,
        title_hint="Quarterly revenue review",
        intent="lookup",
    )
    await store.touch_session(
        session_id=other.id,
        title_hint="Quarterly revenue private",
        intent="lookup",
    )

    matches = await store.list_sessions(user_id="u1", q="revenue")

    assert [session.id for session in matches] == [owned.id]


@pytest.mark.asyncio
async def test_set_session_seen_updates_last_seen_at(tmp_path: Path) -> None:
    """set_session_seen → list_sessions 后 last_seen_at 非 None."""
    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u1")
    await s.set_session_seen(user_id="u1", session_id="s1")
    sessions = await s.list_sessions(user_id="u1", workspace_id="w")
    assert sessions[0].last_seen_at is not None
    # ISO timestamp shape
    assert "T" in sessions[0].last_seen_at  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_set_session_seen_per_user(tmp_path: Path) -> None:
    """user_session_views 是 (user_id, session_id) 复合 key — 不同 user 各自独立."""
    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    # Two users, one session (synthetic scenario: shared view)
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u1")
    # u2 borrows s1: created under u1's user_id, but listed as u2 to retrieve last_seen_at.
    await s.set_session_seen(user_id="u1", session_id="s1")

    # u2 must not inherit u1's last_seen_at.
    sess_u1 = await s.list_sessions(user_id="u1", workspace_id="w")
    assert len(sess_u1) == 1
    assert sess_u1[0].last_seen_at is not None
    # list_sessions(user_id=u2) sees nothing: session.user_id=u1, and listing filters by user_id.
    sess_u2 = await s.list_sessions(user_id="u2", workspace_id="w")
    assert sess_u2 == []


@pytest.mark.asyncio
async def test_set_session_seen_upsert(tmp_path: Path) -> None:
    """重复调 set_session_seen → last_seen_at 更新 (upsert)."""
    import asyncio

    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u1")
    await s.set_session_seen(user_id="u1", session_id="s1")
    sessions1 = await s.list_sessions(user_id="u1", workspace_id="w")
    seen1 = sessions1[0].last_seen_at
    await asyncio.sleep(1.05)  # Cross a second boundary (ts isoformat precision is seconds).
    await s.set_session_seen(user_id="u1", session_id="s1")
    sessions2 = await s.list_sessions(user_id="u1", workspace_id="w")
    seen2 = sessions2[0].last_seen_at
    assert seen1 != seen2
    assert seen2 > seen1  # type: ignore[operator]


# ─── W12-3b (#87) pin / unpin ────────────────────────


@pytest.mark.asyncio
async def test_pin_session_sets_pinned_at(tmp_path: Path) -> None:
    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u1")
    await s.pin_session(user_id="u1", session_id="s1")
    sessions = await s.list_sessions(user_id="u1", workspace_id="w")
    assert sessions[0].pinned_at is not None


@pytest.mark.asyncio
async def test_pin_session_idempotent(tmp_path: Path) -> None:
    """多次 pin → pinned_at 不变 (ON CONFLICT DO NOTHING 保留首次时间)."""
    import asyncio

    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u1")
    await s.pin_session(user_id="u1", session_id="s1")
    sessions1 = await s.list_sessions(user_id="u1", workspace_id="w")
    pin1 = sessions1[0].pinned_at
    await asyncio.sleep(1.05)
    await s.pin_session(user_id="u1", session_id="s1")  # Pin again.
    sessions2 = await s.list_sessions(user_id="u1", workspace_id="w")
    assert sessions2[0].pinned_at == pin1  # Unchanged


@pytest.mark.asyncio
async def test_unpin_session_removes_pinned_at(tmp_path: Path) -> None:
    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u1")
    await s.pin_session(user_id="u1", session_id="s1")
    await s.unpin_session(user_id="u1", session_id="s1")
    sessions = await s.list_sessions(user_id="u1", workspace_id="w")
    assert sessions[0].pinned_at is None


@pytest.mark.asyncio
async def test_unpin_session_idempotent(tmp_path: Path) -> None:
    """unpin 一个未 pin 的 session 也不抛错."""
    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="w", user_id="u1")
    await s.unpin_session(user_id="u1", session_id="s1")  # never pinned
    sessions = await s.list_sessions(user_id="u1", workspace_id="w")
    assert sessions[0].pinned_at is None
