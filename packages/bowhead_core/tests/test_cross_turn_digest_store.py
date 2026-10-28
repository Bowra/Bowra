"""Integration: cross-turn digest write→store→read→render round-trip (P0-A).

Proves the full data path the live wiring depends on:
  build_cross_turn_summary → json → session_store.save_digest →
  session_store.list_recent_digests (with exclude_turn_id) → render_recent_turns_block
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from bowhead_core.cross_turn_digest import (
    build_cross_turn_summary,
    render_recent_turns_block,
)
from bowhead_core.session_store import SessionStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_digest_roundtrip_and_exclude_current(tmp_path: Path) -> None:
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    sid = "s_round"
    await s.ensure_session(session_id=sid, workspace_id="ws", user_id="u")

    # Turn 3 — the "report" turn (delegated; payload carries the real data).
    await s.record_turn(turn_id="t_report", session_id=sid, user_text="各药品销售变化 summary")
    summary = build_cross_turn_summary(
        turn_id="t_report",
        user_text="各药品销售变化 summary",
        intent="report",
        final_answer="完整报告见画布。",
        aggregated_payload={
            "exec_summary": "2025各产品线同比基本持平，约六成下滑。",
            "kpis": [{"label": "整体同比", "value": "持平", "delta": "+0.3%"}],
            "insights": [{"title": "呼吸/抗肿瘤量价齐升"}],
            "sources": [{"a": 1}],
        },
        canvas_id="cv_367b",
    )
    await s.save_digest(turn_id="t_report", digest_json=json.dumps(summary, ensure_ascii=False))
    await s.mark_completed(
        turn_id="t_report",
        intent="report",
        final_answer="完整报告见画布。",
        tokens_input=0,
        tokens_output=0,
        cost_usd=0,
    )
    await s.finalize_turn(
        session_id=sid,
        current_turn_id="t_report",
        next_turn_id=None,
    )

    # Turn 4 — the current "做个ppt" turn (also gets a digest later, but the
    # read for THIS turn must exclude itself).
    await s.record_turn(turn_id="t_ppt", session_id=sid, user_text="好你帮我生成一个分析ppt？")

    # Read prior digests for turn 4 — exclude self.
    recent = await s.list_recent_digests(session_id=sid, limit=3, exclude_turn_id="t_ppt")
    assert len(recent) == 1, "should see the report turn's digest"
    d = recent[0]
    assert d["turn_id"] == "t_report"
    assert d["conclusion"].startswith("2025各产品线")
    assert d["key_facts"][0]["label"] == "整体同比"
    assert d["canvas_id"] == "cv_367b"

    # Render into the Lead-prompt block.
    block = render_recent_turns_block(recent)
    assert "各药品销售变化" in block
    assert "canvas cv_367b" in block
    assert "do not rerun the data" in block  # the reuse-as-deck nudge


@pytest.mark.asyncio
async def test_turn_without_digest_is_invisible(tmp_path: Path) -> None:
    """A turn whose digest was never saved (e.g. clarification-only) must not
    surface — list_recent_digests filters digest_json IS NOT NULL."""
    s = SessionStore(str(tmp_path / "s.db"))
    await s.initialize()
    sid = "s_partial"
    await s.ensure_session(session_id=sid, workspace_id="ws", user_id="u")
    await s.record_turn(turn_id="t_nodigest", session_id=sid, user_text="hi")
    await s.mark_completed(
        turn_id="t_nodigest",
        intent=None,
        final_answer="done",
        tokens_input=0,
        tokens_output=0,
        cost_usd=0,
    )
    await s.finalize_turn(
        session_id=sid,
        current_turn_id="t_nodigest",
        next_turn_id=None,
    )
    await s.record_turn(turn_id="t_cur", session_id=sid, user_text="now")
    recent = await s.list_recent_digests(session_id=sid, limit=3, exclude_turn_id="t_cur")
    assert recent == []
    assert render_recent_turns_block(recent) == ""
