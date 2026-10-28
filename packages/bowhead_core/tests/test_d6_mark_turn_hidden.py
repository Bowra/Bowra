"""D6 (canvas-empty bug 2026-05-25) — canvas_store mark_turn_hidden + unhide
integration tests. Real sqlite, no mocks.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from bowhead_core.canvas_store import CanvasStore


@pytest.fixture
async def store() -> CanvasStore:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = Path(tmp.name)
    s = CanvasStore(db_path=db_path)
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_mark_turn_hidden_basic(store: CanvasStore) -> None:
    """Append sections by turn, mark hidden, verify list_sections default
    filters them out + include_hidden returns all."""
    canvas = await store.ensure_canvas(
        session_id="s_test1",
        workspace_id="ws1",
        user_id="u1",
    )
    s1 = await store.append_section(
        canvas_id=canvas.id,
        kind="h1",
        payload={"text": "数据查询结果"},
        by_turn_id="t_abc",
    )
    s2 = await store.append_section(
        canvas_id=canvas.id,
        kind="kpi_row",
        payload={"cells": [{"lbl": "GMV", "val": "51.8亿"}]},
        by_turn_id="t_abc",
    )
    # Before mark: list_sections default shows both
    before = await store.list_sections(canvas.id)
    assert len(before) == 2
    assert all(not s.hidden_for_chat_mode for s in before)
    # Mark turn hidden
    hidden_ids = await store.mark_turn_hidden_for_chat_mode(
        "t_abc",
        "chat-mode (no insight): h1×1,kpi_row×1",
    )
    assert set(hidden_ids) == {s1.id, s2.id}
    # After mark: default list filters them out (canvas tab view)
    after_default = await store.list_sections(canvas.id)
    assert len(after_default) == 0
    # include_hidden=True shows them with flag set
    after_all = await store.list_sections(canvas.id, include_hidden=True)
    assert len(after_all) == 2
    assert all(s.hidden_for_chat_mode for s in after_all)
    assert all(s.surface_rationale == "chat-mode (no insight): h1×1,kpi_row×1" for s in after_all)


@pytest.mark.asyncio
async def test_mark_turn_hidden_only_appended_by_turn(store: CanvasStore) -> None:
    """Sections appended by OTHER turns must not be affected."""
    canvas = await store.ensure_canvas(
        session_id="s_test2",
        workspace_id="ws1",
        user_id="u1",
    )
    s_keep = await store.append_section(
        canvas_id=canvas.id,
        kind="insight_card",
        payload={"title": "..."},
        by_turn_id="t_keep",
    )
    s_hide = await store.append_section(
        canvas_id=canvas.id,
        kind="kpi_row",
        payload={"cells": []},
        by_turn_id="t_hide",
    )
    hidden_ids = await store.mark_turn_hidden_for_chat_mode(
        "t_hide",
        "chat-mode",
    )
    assert hidden_ids == [s_hide.id]
    visible = await store.list_sections(canvas.id)
    assert len(visible) == 1
    assert visible[0].id == s_keep.id
    assert not visible[0].hidden_for_chat_mode


@pytest.mark.asyncio
async def test_unhide_turn_reverses(store: CanvasStore) -> None:
    """unhide_turn (admin/promote) reverses hidden flag."""
    canvas = await store.ensure_canvas(
        session_id="s_test3",
        workspace_id="ws1",
        user_id="u1",
    )
    s1 = await store.append_section(
        canvas_id=canvas.id,
        kind="kpi_row",
        payload={"cells": []},
        by_turn_id="t_x",
    )
    await store.mark_turn_hidden_for_chat_mode("t_x", "initial hide")
    assert await store.list_sections(canvas.id) == []
    # Admin promotes turn
    unhidden = await store.unhide_turn("t_x", rationale="manually_promoted")
    assert unhidden == [s1.id]
    visible = await store.list_sections(canvas.id)
    assert len(visible) == 1
    assert visible[0].id == s1.id
    assert not visible[0].hidden_for_chat_mode
    assert visible[0].surface_rationale == "manually_promoted"


@pytest.mark.asyncio
async def test_empty_turn_id_safe(store: CanvasStore) -> None:
    """Empty turn_id returns [] without error."""
    assert await store.mark_turn_hidden_for_chat_mode("", "test") == []
    assert await store.unhide_turn("", rationale="test") == []


@pytest.mark.asyncio
async def test_mark_idempotent_via_re_mark(store: CanvasStore) -> None:
    """Calling mark twice doesn't error — second call same effect."""
    canvas = await store.ensure_canvas(
        session_id="s_test4",
        workspace_id="ws1",
        user_id="u1",
    )
    await store.append_section(
        canvas_id=canvas.id,
        kind="kpi_row",
        payload={},
        by_turn_id="t_y",
    )
    ids1 = await store.mark_turn_hidden_for_chat_mode("t_y", "first")
    ids2 = await store.mark_turn_hidden_for_chat_mode("t_y", "second")
    # Both return the section id; rationale overwritten to second
    assert ids1 == ids2
    secs = await store.list_sections(canvas.id, include_hidden=True)
    assert secs[0].surface_rationale == "second"


@pytest.mark.asyncio
async def test_inline_artifacts_path_sees_hidden(store: CanvasStore) -> None:
    """list_sections_appended_by_turn must NOT filter hidden — chat bubble
    inline_artifacts wire depends on seeing all sections for the turn even
    if canvas-hidden. Critical invariant for D6 architecture."""
    canvas = await store.ensure_canvas(
        session_id="s_test5",
        workspace_id="ws1",
        user_id="u1",
    )
    s_kpi = await store.append_section(
        canvas_id=canvas.id,
        kind="kpi_row",
        payload={"cells": []},
        by_turn_id="t_chat",
    )
    await store.mark_turn_hidden_for_chat_mode("t_chat", "chat-mode")
    # list_sections (canvas tab path): hidden filtered
    assert await store.list_sections(canvas.id) == []
    # list_sections_appended_by_turn (inline_artifacts path): must still surface
    inline = await store.list_sections_appended_by_turn("t_chat")
    assert len(inline) == 1
    assert inline[0].id == s_kpi.id
    assert inline[0].hidden_for_chat_mode is True  # flag is visible


@pytest.mark.asyncio
async def test_unhide_sections_for_report_lifts_only_the_referenced_sections(store: CanvasStore) -> None:
    """A report anchoring one chart of a chat-delivered turn brings back that
    chart only; its siblings stay hidden and the call is idempotent."""
    canvas = await store.ensure_canvas(session_id="s_rep1", workspace_id="ws1", user_id="u1")
    a = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {}}, by_turn_id="t_chat")
    b = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {}}, by_turn_id="t_chat")
    await store.mark_turn_hidden_for_chat_mode("t_chat", "declared: chat")

    assert await store.unhide_sections_for_report([a.id], "report_reference:t_rep") == [a.id]
    visible = await store.list_sections(canvas.id)
    assert [s.id for s in visible] == [a.id]
    assert visible[0].surface_rationale == "report_reference:t_rep"
    assert await store.unhide_sections_for_report([a.id], "again") == []  # already visible
    assert b.id not in {s.id for s in await store.list_sections(canvas.id)}


@pytest.mark.asyncio
async def test_unhide_sections_for_report_honours_an_explicit_report_reference_over_chat_target(store: CanvasStore) -> None:
    """`unhide_turn` deliberately keeps sections the agent declared
    `surface_target='chat'`; an explicit anchor in the user's report is a newer,
    stronger signal, so this path lifts them. Superseded sections never come back."""
    canvas = await store.ensure_canvas(session_id="s_rep2", workspace_id="ws1", user_id="u1")
    declared = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {}}, by_turn_id="t_chat")
    await store.set_section_surface_target(declared.id, "chat")
    replaced = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {}}, by_turn_id="t_chat")
    await store.set_section_surface_target(replaced.id, "chat")
    await store.mark_sections_superseded(section_ids=[replaced.id], superseded_by_turn="t_new")

    lifted = await store.unhide_sections_for_report([declared.id, replaced.id], "report_reference:t_rep")
    assert lifted == [declared.id]
    assert {s.id for s in await store.list_sections(canvas.id)} == {declared.id}
