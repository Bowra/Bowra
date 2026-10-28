"""chat-hydrate-overhaul: persist + read trace_steps + summary."""

from __future__ import annotations

import pytest
from bowhead_core.session_store import (
    SessionStore,
    TraceStepRow,
)


@pytest.fixture
async def store(tmp_path):
    db = tmp_path / "test.db"
    s = SessionStore(str(db))
    await s.initialize()
    # Create a session + turn so FK is satisfied (REFERENCES turns(id))
    sess = await s.ensure_session(
        session_id=None,
        workspace_id="ws1",
        user_id="u1",
    )
    turn_id = "t_test_1"
    await s.record_turn(
        turn_id=turn_id,
        session_id=sess.id,
        user_text="hello",
    )
    return s, turn_id


async def test_persist_trace_round_trip(store) -> None:
    s, turn_id = store
    rows = [
        TraceStepRow(
            turn_id=turn_id,
            seq=0,
            step_id="classify",
            kind="success",
            title="Classification",
            tool_name=None,
            duration_ms=None,
            summary="intent=lookup",
        ),
        TraceStepRow(turn_id=turn_id, seq=1, step_id="tool_t1", kind="success", title=None, tool_name="bi.fetch", duration_ms=120, summary="rows=42"),
    ]
    await s.persist_turn_trace(turn_id=turn_id, steps=rows)

    out = await s.list_trace_steps_for_turns([turn_id])
    assert turn_id in out
    got = out[turn_id]
    assert len(got) == 2
    assert got[0].step_id == "classify"
    assert got[1].step_id == "tool_t1"
    assert got[1].tool_name == "bi.fetch"
    assert got[1].duration_ms == 120
    assert got[0].subagent_id is None
    assert got[1].subagent_id is None


async def test_persist_trace_with_nested_subagent(store) -> None:
    """Round 5: nested children 用 subagent_id 关联到 spawn parent step."""
    s, turn_id = store
    spawn_call_id = "tl_spawn_1"
    rows = [
        TraceStepRow(
            turn_id=turn_id,
            seq=0,
            step_id=f"tool_{spawn_call_id}",
            kind="success",
            title=None,
            tool_name="subagent_spawn",
            duration_ms=8000,
            summary="ok",
            subagent_id=None,
        ),
        # Nested children use the spawn call_id as subagent_id.
        TraceStepRow(
            turn_id=turn_id,
            seq=1,
            step_id="tool_nested_a",
            kind="success",
            title=None,
            tool_name="bi.fetch",
            duration_ms=200,
            summary="rows=10",
            subagent_id=spawn_call_id,
        ),
        TraceStepRow(
            turn_id=turn_id,
            seq=2,
            step_id="tool_nested_b",
            kind="success",
            title=None,
            tool_name="bi_attribute",
            duration_ms=300,
            summary="dim=region",
            subagent_id=spawn_call_id,
        ),
    ]
    await s.persist_turn_trace(turn_id=turn_id, steps=rows)

    out = await s.list_trace_steps_for_turns([turn_id])
    got = out[turn_id]
    assert len(got) == 3
    assert got[0].subagent_id is None  # Spawn itself is top-level.
    assert got[1].subagent_id == spawn_call_id
    assert got[2].subagent_id == spawn_call_id


async def test_persist_summary_round_trip(store) -> None:
    s, turn_id = store
    await s.persist_turn_summary(
        turn_id=turn_id,
        total_ms=2500,
        tools_count=3,
        llm_count=1,
        tokens_input=400,
        tokens_output=200,
    )
    out = await s.get_turn_summaries([turn_id])
    assert turn_id in out
    got = out[turn_id]
    assert got.total_ms == 2500
    assert got.tools_count == 3
    assert got.tokens_input == 400


async def test_old_turn_no_rows_returns_empty_graceful(store) -> None:
    """老 session 升级 (无 trace_step row) → batch read 返空, 不报错."""
    s, turn_id = store
    # Write no rows.
    trace_out = await s.list_trace_steps_for_turns([turn_id])
    summary_out = await s.get_turn_summaries([turn_id])
    assert trace_out == {turn_id: []}
    assert summary_out == {}


async def test_batch_read_avoids_n_plus_1(store) -> None:
    """3 turn → 1 SQL query (IN-batch). 仅 schema 验证, 不计 SQL count."""
    s, turn_id1 = store
    sess = await s.ensure_session(session_id=None, workspace_id="ws1", user_id="u1")
    turn_id2 = "t_test_2"
    turn_id3 = "t_test_3"
    await s.mark_completed(
        turn_id=turn_id1,
        intent=None,
        final_answer="done",
        tokens_input=0,
        tokens_output=0,
        cost_usd=0,
    )
    await s.finalize_turn(
        session_id=sess.id,
        current_turn_id=turn_id1,
        next_turn_id=None,
    )
    await s.record_turn(turn_id=turn_id2, session_id=sess.id, user_text="q2")
    await s.mark_completed(
        turn_id=turn_id2,
        intent=None,
        final_answer="done",
        tokens_input=0,
        tokens_output=0,
        cost_usd=0,
    )
    await s.finalize_turn(
        session_id=sess.id,
        current_turn_id=turn_id2,
        next_turn_id=None,
    )
    await s.record_turn(turn_id=turn_id3, session_id=sess.id, user_text="q3")

    for tid in (turn_id1, turn_id2):
        await s.persist_turn_trace(
            turn_id=tid,
            steps=[
                TraceStepRow(turn_id=tid, seq=0, step_id="classify", kind="success", title=None, tool_name=None, duration_ms=None, summary=None),
            ],
        )

    out = await s.list_trace_steps_for_turns([turn_id1, turn_id2, turn_id3])
    assert len(out[turn_id1]) == 1
    assert len(out[turn_id2]) == 1
    assert out[turn_id3] == []  # empty for turn3


async def test_summary_truncated_to_1kb(store) -> None:
    s, turn_id = store
    big = "x" * 5000
    await s.persist_turn_trace(
        turn_id=turn_id,
        steps=[
            TraceStepRow(turn_id=turn_id, seq=0, step_id="tool_t", kind="success", title=None, tool_name="bi.x", duration_ms=1, summary=big),
        ],
    )
    out = await s.list_trace_steps_for_turns([turn_id])
    got = out[turn_id][0]
    assert got.summary is not None
    assert len(got.summary) == 1024
