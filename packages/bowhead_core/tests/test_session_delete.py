"""会话软删的持久化契约（feedback-triage-2026-08-24：客户要「对话要可以删掉」）。

软删本身在 store 里早就有了（`deleted_at` 列 + 迁移 + `soft_delete_session` +
`revive_session`），当初是为「删报告联动软删来源会话」做的；缺的一直是用户入口。
这里钉的是新加的**用户面**入口 `delete_session` / `undelete_session` 的三条不变量：

1. owner 收敛 —— `soft_delete_session` 是不带 owner 校验的裸原语，
   `revive_session` 的注释专门警告过：接到用户可控 session_id 上就是越权。
2. 有活跃 turn 时拒绝 —— 删掉一个正在跑的会话，那轮 turn 会继续烧额度却无处可见。
3. 软删只收敛列表面，详情/turn/trace 保留可审计。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.session_store import SessionStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> SessionStore:
    value = SessionStore(tmp_path / "sessions.db")
    await value.initialize()
    return value


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


async def _listed_ids(store: SessionStore, user_id: str = "u1") -> list[str]:
    rows = await store.list_sessions(user_id=user_id)
    return [session.id for session in rows]


@pytest.mark.asyncio
async def test_delete_hides_session_from_the_list(store: SessionStore) -> None:
    await _session(store, session_id="s_gone")
    await _session(store, session_id="s_kept")

    assert await store.delete_session(user_id="u1", session_id="s_gone") == "ok"

    listed = await _listed_ids(store)
    assert "s_gone" not in listed
    assert "s_kept" in listed


@pytest.mark.asyncio
async def test_delete_keeps_the_row_auditable(store: SessionStore) -> None:
    """★ 软删不是硬删：详情仍拿得到，只是列表面看不见。"""
    await _session(store, session_id="s_soft")
    await store.delete_session(user_id="u1", session_id="s_soft")

    session = await store.get_session("s_soft")
    assert session is not None
    assert session.deleted_at is not None


@pytest.mark.asyncio
async def test_delete_is_owner_scoped(store: SessionStore) -> None:
    """★ 别人的会话删不动，且存在性一并隐藏（返回 not_found 而不是 forbidden）。"""
    await _session(store, session_id="s_mine", user_id="u1")

    assert await store.delete_session(user_id="u2", session_id="s_mine") == "not_found"

    session = await store.get_session("s_mine")
    assert session is not None
    assert session.deleted_at is None


@pytest.mark.asyncio
async def test_delete_refuses_a_session_with_a_running_turn(store: SessionStore) -> None:
    """★ 正在跑的会话不许删 —— 删了那轮 turn 会继续烧额度却无处可见。"""
    await _session(store, session_id="s_busy")
    await store.record_turn(turn_id="t_busy", session_id="s_busy", user_text="跑一下")

    assert await store.delete_session(user_id="u1", session_id="s_busy") == "active"

    session = await store.get_session("s_busy")
    assert session is not None
    assert session.deleted_at is None


@pytest.mark.asyncio
async def test_delete_is_idempotent(store: SessionStore) -> None:
    await _session(store, session_id="s_twice")
    assert await store.delete_session(user_id="u1", session_id="s_twice") == "ok"
    assert await store.delete_session(user_id="u1", session_id="s_twice") == "not_found"


@pytest.mark.asyncio
async def test_undelete_restores_the_session(store: SessionStore) -> None:
    await _session(store, session_id="s_back")
    await store.delete_session(user_id="u1", session_id="s_back")

    assert await store.undelete_session(user_id="u1", session_id="s_back") is True
    assert "s_back" in await _listed_ids(store)


@pytest.mark.asyncio
async def test_undelete_is_owner_scoped(store: SessionStore) -> None:
    """★ 反向对照：裸 undelete 原语接到用户可控 id 上就是越权复活（store 注释原话）。"""
    await _session(store, session_id="s_theirs", user_id="u1")
    await store.delete_session(user_id="u1", session_id="s_theirs")

    assert await store.undelete_session(user_id="u2", session_id="s_theirs") is False

    session = await store.get_session("s_theirs")
    assert session is not None
    assert session.deleted_at is not None


@pytest.mark.asyncio
async def test_undelete_on_a_live_session_is_a_noop(store: SessionStore) -> None:
    await _session(store, session_id="s_live")
    assert await store.undelete_session(user_id="u1", session_id="s_live") is False
