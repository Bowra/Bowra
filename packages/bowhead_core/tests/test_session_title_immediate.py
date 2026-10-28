"""Test #133: record_turn 首轮立刻把 user_text[:40] 当 session 临时 title.

Pre-fix: session.title stayed NULL until AI 生成（异步 ~5s），sidebar 期间显示
session_id（不可点 + 体验差）. After fix: 1st turn 写入立刻置 user_text 截断.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_first_turn_sets_title_to_user_text_snippet(tmp_path: Path) -> None:
    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(
        session_id="s_t1",
        workspace_id="1",
        user_id="u1",
    )
    # First turn → set the title immediately.
    await s.record_turn(
        turn_id="t_1",
        session_id="s_t1",
        user_text="2025年 GMV 同比，分品牌看 top 5",
    )
    sess = await s.get_session("s_t1")
    assert sess is not None
    assert sess.title == "2025年 GMV 同比，分品牌看 top 5"


@pytest.mark.asyncio
async def test_user_text_truncated_to_40(tmp_path: Path) -> None:
    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="1", user_id="u1")
    long_text = "这是一段非常长的提问" * 30  # > 40 characters.
    await s.record_turn(turn_id="t1", session_id="s1", user_text=long_text)
    sess = await s.get_session("s1")
    assert sess is not None
    assert sess.title is not None
    assert len(sess.title) == 40


@pytest.mark.asyncio
async def test_does_not_override_ai_generated_title(tmp_path: Path) -> None:
    """AI 已生成 title (非空 + 非 session_id) → 不覆盖."""
    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="1", user_id="u1")
    # Simulate an AI-generated title.
    await s.set_title(session_id="s1", title="GMV 复盘报告 2025")

    # The second turn record must not overwrite it (actually seq=1, since no turn was inserted).
    await s.record_turn(turn_id="t1", session_id="s1", user_text="新提问")
    sess = await s.get_session("s1")
    assert sess is not None
    assert sess.title == "GMV 复盘报告 2025"  # Preserve the AI title.


@pytest.mark.asyncio
async def test_second_turn_does_not_overwrite_existing_title(tmp_path: Path) -> None:
    """seq>1 不再走临时 title 设值（只有首轮才用 user_text 占位）."""
    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="1", user_id="u1")
    await s.record_turn(turn_id="t1", session_id="s1", user_text="第一句")
    sess1 = await s.get_session("s1")
    assert sess1 is not None and sess1.title == "第一句"

    await s.mark_completed(
        turn_id="t1",
        intent=None,
        final_answer="done",
        tokens_input=0,
        tokens_output=0,
        cost_usd=0,
    )
    await s.finalize_turn(
        session_id="s1",
        current_turn_id="t1",
        next_turn_id=None,
    )
    await s.record_turn(turn_id="t2", session_id="s1", user_text="第二句不应覆盖")
    sess2 = await s.get_session("s1")
    assert sess2 is not None
    assert sess2.title == "第一句"  # Unchanged.


@pytest.mark.asyncio
async def test_empty_user_text_falls_back_to_default(tmp_path: Path) -> None:
    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="1", user_id="u1")
    await s.record_turn(turn_id="t1", session_id="s1", user_text="   ")
    sess = await s.get_session("s1")
    assert sess is not None
    assert sess.title == "新对话"


@pytest.mark.asyncio
async def test_title_uses_first_line_only(tmp_path: Path) -> None:
    """多行 user_text 只取第一行（避免长 prompt 把 title 撑乱）."""
    from bowhead_core.session_store import SessionStore

    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    await s.ensure_session(session_id="s1", workspace_id="1", user_id="u1")
    await s.record_turn(
        turn_id="t1",
        session_id="s1",
        user_text="第一行问题\n\n详细背景: ...\n更多",
    )
    sess = await s.get_session("s1")
    assert sess is not None
    assert sess.title == "第一行问题"
