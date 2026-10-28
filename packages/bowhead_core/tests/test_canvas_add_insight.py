"""M157-M1 · canvas.add_insight tool 单测.

覆盖:
- 字段校验 (title/thesis/narrative 长度, evidence_section_ids 非空)
- P0 #4 evidence_section_ids 必须真实存在
- 成功 path: payload 写入 canvas_store + emit SSE event
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.canvas_tools import _add_insight
from bowhead_core.tool_registry import ToolExecutionContext

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _CapturingBus:
    events: list[Any] = None

    def __post_init__(self) -> None:
        if self.events is None:
            self.events = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
async def ctx(tmp_path: Path) -> ToolExecutionContext:
    cs = CanvasStore(tmp_path / "t.db")
    await cs.initialize()
    return ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t1",
        datarc=None,  # type: ignore[arg-type]
        canvas_store=cs,
        session_id="s1",
        parent_bus=_CapturingBus(),
    )


@pytest.fixture
async def chart_section_id(ctx: ToolExecutionContext) -> str:
    """预先 append 一个 chart section 拿真实 ID 给 add_insight 引用."""
    canvas = await ctx.canvas_store.ensure_canvas(
        session_id=ctx.session_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
    )
    s = await ctx.canvas_store.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload={"spec": {"kind": "bar", "title": "test"}},
        by_turn_id=ctx.turn_id,
    )
    return s.id


# ─── Field validation ────────────────────────


async def test_missing_title_rejects(ctx, chart_section_id) -> None:
    out = await _add_insight(
        {
            "title": "",
            "thesis": "thesis",
            "narrative": "x" * 50,
            "evidence_section_ids": [chart_section_id],
        },
        ctx,
    )
    assert "error" in out
    assert "title" in out["error"]


async def test_thesis_too_long_rejects(ctx, chart_section_id) -> None:
    out = await _add_insight(
        {
            "title": "test",
            "thesis": "x" * 200,
            "narrative": "y" * 60,
            "evidence_section_ids": [chart_section_id],
        },
        ctx,
    )
    assert "error" in out and "thesis" in out["error"]


async def test_narrative_too_short_rejects(ctx, chart_section_id) -> None:
    out = await _add_insight(
        {
            "title": "test",
            "thesis": "thesis",
            "narrative": "短",  # <30 characters
            "evidence_section_ids": [chart_section_id],
        },
        ctx,
    )
    assert "error" in out and "太短" in out["error"]


async def test_narrative_too_long_rejects(ctx, chart_section_id) -> None:
    out = await _add_insight(
        {
            "title": "test",
            "thesis": "thesis",
            "narrative": "x" * 1300,  # Hard cap 1200 after the default-ghostwriter decision (raised from 600)
            "evidence_section_ids": [chart_section_id],
        },
        ctx,
    )
    assert "error" in out and "太长" in out["error"]


async def test_evidence_empty_rejects(ctx) -> None:
    out = await _add_insight(
        {
            "title": "test",
            "thesis": "thesis",
            "narrative": "y" * 60,
            "evidence_section_ids": [],
        },
        ctx,
    )
    assert "error" in out and "evidence" in out["error"]


# ─── P0 #4: evidence_section_ids must actually exist ───


async def test_evidence_id_not_exists_rejects(ctx) -> None:
    """ID 是编的, canvas 里不存在 → 拒, 含明确提示."""
    out = await _add_insight(
        {
            "title": "test",
            "thesis": "thesis",
            "narrative": "y" * 60,
            "evidence_section_ids": ["sec_made_up_id_xxx"],
        },
        ctx,
    )
    assert "error" in out
    assert "不存在" in out["error"]
    assert "先 add_chart" in out["error"]
    assert "sec_made_up_id_xxx" in out["error"]


async def test_evidence_id_partial_missing_rejects(ctx, chart_section_id) -> None:
    """有真有假 → 仍然拒."""
    out = await _add_insight(
        {
            "title": "test",
            "thesis": "thesis",
            "narrative": "y" * 60,
            "evidence_section_ids": [chart_section_id, "sec_fake"],
        },
        ctx,
    )
    assert "error" in out and "sec_fake" in out["error"]


# ─── Success path ────────────────────────────


async def test_success_basic(ctx, chart_section_id) -> None:
    out = await _add_insight(
        {
            "title": "体系合规分化",
            "thesis": "信息技术体系 50% 远超均值",
            "narrative": "信息技术体系合规率 50.16% (309/616 单), 远超平均 7.88%；其余体系均 < 5%, 表现差距巨大. 投资公共行政体系合规率 3.78% 排第二.",
            "evidence_section_ids": [chart_section_id],
        },
        ctx,
    )
    assert "section_id" in out
    assert out["kind"] == "insight_card"
    assert out["title"] == "体系合规分化"
    assert out["evidence_count"] == 1
    # Verify the SSE event was emitted.
    bus_events = ctx.parent_bus.events
    assert len(bus_events) == 1
    assert bus_events[0].event_type == "canvas.section_added"


async def test_success_with_optional_fields(ctx, chart_section_id) -> None:
    out = await _add_insight(
        {
            "title": "test",
            "thesis": "thesis",
            "narrative": "narrative " * 8,  # 80 characters
            "evidence_section_ids": [chart_section_id],
            "impact": {"quantity": 12.5, "unit": "%", "direction": "up"},
            "confidence": "high",
            "anchor_heading": "总体合规",
        },
        ctx,
    )
    assert "section_id" in out


async def test_success_multiple_evidence(ctx, chart_section_id) -> None:
    # Add a second chart to obtain its ID.
    canvas = await ctx.canvas_store.ensure_canvas(
        session_id=ctx.session_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
    )
    s2 = await ctx.canvas_store.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload={"spec": {"kind": "pie", "title": "second"}},
        by_turn_id=ctx.turn_id,
    )
    out = await _add_insight(
        {
            "title": "test",
            "thesis": "thesis",
            "narrative": "x" * 60,
            "evidence_section_ids": [chart_section_id, s2.id],
        },
        ctx,
    )
    assert out.get("evidence_count") == 2
