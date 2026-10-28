"""Current chart-composer and attachment behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# ─────────────────────────────────────────────────────────────
# #92 chart reorder unit tests
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# #71 force_spawn conditions
# ─────────────────────────────────────────────────────────────
def test_force_spawn_triggers_on_data_attachment_high_complexity() -> None:
    from dataclasses import dataclass

    # 2026-07-14 contract update: helpers now have public names and live in turn_runner (re-exports removed).
    from bowhead_core.turn_runner import (
        data_attachment_ids,
        user_explicitly_wants_bi,
    )
    from bowhead_core.types import IntentType

    @dataclass
    class _P:
        artifact_id: str
        filename: str
        mime: str

    prepared = [_P("art_1", "data.csv", "text/csv")]
    ids = data_attachment_ids(prepared)
    assert "art_1" in ids

    wants_bi = user_explicitly_wants_bi("分析这份附件合规")
    assert wants_bi is False

    # force_spawn decision conditions (reproduce orchestrator conditions)
    intent = IntentType.REPORT
    complexity = 0.8
    should_force = bool(ids) and not wants_bi and intent in (IntentType.REPORT, IntentType.COMPUTE, IntentType.CAUSAL) and complexity >= 0.5
    assert should_force is True


def test_force_spawn_bypass_whenuser_explicitly_wants_bi() -> None:
    from dataclasses import dataclass

    # 2026-07-14 contract update: helpers now have public names and live in turn_runner (re-exports removed).
    from bowhead_core.turn_runner import (
        data_attachment_ids,
        user_explicitly_wants_bi,
    )
    from bowhead_core.types import IntentType

    @dataclass
    class _P:
        artifact_id: str
        filename: str
        mime: str

    prepared = [_P("art_1", "data.csv", "text/csv")]
    wants_bi = user_explicitly_wants_bi("基于附件去 datarc 查今年消费")
    assert wants_bi is True

    ids = data_attachment_ids(prepared)
    should_force = bool(ids) and not wants_bi and IntentType.REPORT in (IntentType.REPORT,) and 0.8 >= 0.5
    assert should_force is False


def test_force_spawn_skip_when_no_attachment() -> None:
    from bowhead_core.turn_runner import data_attachment_ids

    assert data_attachment_ids([]) == []


# ─────────────────────────────────────────────────────────────
# #106 prompt version history: stable hash + consistent length
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# #112 API-based anchor_heading: heading text matching
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_anchor_heading_matches_narrative_heading() -> None:
    """anchor_heading='月度趋势' → composed 里 h2='月度趋势' 的 section → chart ref 生成."""
    from bowhead_core.canvas_composer import build_api_chart_refs

    # mock canvas_store.list_sections
    chart1 = SimpleNamespace(
        id="sec_chart_a",
        kind="chart",
        payload={"spec": {"kind": "bar"}, "anchor_heading": "月度趋势"},
    )
    chart2 = SimpleNamespace(
        id="sec_chart_b",
        kind="chart",
        payload={"spec": {"kind": "line"}, "anchor_heading": "按省排名"},
    )
    other = SimpleNamespace(id="sec_nar1", kind="narrative", payload={})

    async def _list(canvas_id: str):
        return [chart1, chart2, other]

    composed = [
        {"id": "sec_eye", "kind": "eyebrow"},
        {"id": "sec_h1", "kind": "h1", "text": "复盘概要"},
        {"id": "sec_trend", "kind": "section", "h2": "月度趋势"},
        {"id": "sec_narr1", "kind": "narrative", "markdown": "..."},
        {"id": "sec_rank", "kind": "section", "h2": "按省排名"},
        {"id": "sec_narr2", "kind": "narrative", "markdown": "..."},
    ]
    appended = [s["id"] for s in composed]

    refs = await build_api_chart_refs(
        canvas_store=SimpleNamespace(list_sections=_list),
        canvas_id="cv_1",
        composed_sections=composed,
        appended_ids=appended,
    )
    # Monthly-trend heading at index 2 → chart at 3; province-ranking heading at 4 → chart at 5.
    assert (3, "sec_chart_a") in refs
    assert (5, "sec_chart_b") in refs


@pytest.mark.asyncio
async def test_anchor_heading_no_match_returns_empty() -> None:
    """anchor_heading 找不到匹配 heading → 不生成 ref（chart 堆末尾兜底）."""
    from bowhead_core.canvas_composer import build_api_chart_refs

    chart = SimpleNamespace(
        id="sec_chart_x",
        kind="chart",
        payload={"anchor_heading": "这个标题根本不存在"},
    )

    async def _list(canvas_id: str):
        return [chart]

    composed = [
        {"id": "sec_h1", "kind": "h1", "text": "报告"},
        {"id": "sec_sec", "kind": "section", "h2": "别的标题"},
    ]
    refs = await build_api_chart_refs(
        canvas_store=SimpleNamespace(list_sections=_list),
        canvas_id="cv_1",
        composed_sections=composed,
        appended_ids=[s["id"] for s in composed],
    )
    assert refs == []


@pytest.mark.asyncio
async def test_anchor_heading_normalize_spaces_and_case() -> None:
    """anchor_heading '  月度 GMV' vs h2 '月度GMV' → 归一化后命中."""
    from bowhead_core.canvas_composer import build_api_chart_refs

    chart = SimpleNamespace(
        id="sec_chart_c",
        kind="chart",
        payload={"anchor_heading": "  月度 GMV  "},
    )

    async def _list(canvas_id: str):
        return [chart]

    composed = [
        {"id": "sec_sec", "kind": "section", "h2": "**月度GMV**"},
    ]
    refs = await build_api_chart_refs(
        canvas_store=SimpleNamespace(list_sections=_list),
        canvas_id="cv_1",
        composed_sections=composed,
        appended_ids=["sec_sec"],
    )
    assert refs == [(1, "sec_chart_c")]


@pytest.mark.asyncio
async def test_anchor_heading_ignored_when_absent() -> None:
    """payload 没 anchor_heading → 不生成 ref."""
    from bowhead_core.canvas_composer import build_api_chart_refs

    chart = SimpleNamespace(id="sec_chart_d", kind="chart", payload={"spec": {}})

    async def _list(canvas_id: str):
        return [chart]

    refs = await build_api_chart_refs(
        canvas_store=SimpleNamespace(list_sections=_list),
        canvas_id="cv_1",
        composed_sections=[{"id": "s1", "kind": "section", "h2": "任何"}],
        appended_ids=["s1"],
    )
    assert refs == []


@pytest.mark.parametrize("missing", [False, True])
async def test_chart_reorder_persists_and_emits_the_current_order(missing: bool) -> None:
    from unittest.mock import AsyncMock

    from bowhead_core.canvas_composer import reorder_for_chart_refs

    sections = [
        SimpleNamespace(id="chart", kind="chart"),
        SimpleNamespace(id="title", kind="h1"),
        SimpleNamespace(id="intro", kind="narrative"),
        SimpleNamespace(id="detail", kind="narrative"),
        SimpleNamespace(id="sources", kind="sources"),
    ]
    store = SimpleNamespace(list_sections=AsyncMock(return_value=sections), reorder_sections=AsyncMock())
    bus = SimpleNamespace(emit=AsyncMock())
    await reorder_for_chart_refs(
        canvas_store=store,
        canvas_id="cv_test",
        appended_ids=["title", "intro", "detail", "sources"],
        chart_ref_order=[(2, "missing" if missing else "chart")],
        turn_id="t_test",
        bus=bus,
    )
    expected = ["title", "intro", "detail", "chart", "sources"] if missing else ["title", "intro", "chart", "detail", "sources"]
    store.reorder_sections.assert_awaited_once_with(canvas_id="cv_test", ordered_ids=expected, by_turn_id="t_test")
    event = bus.emit.await_args.args[0]
    assert event.event_type == "canvas.reordered"
    assert event.payload["order"] == expected
