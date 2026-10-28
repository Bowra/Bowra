"""ν2 v2 — _compute_response_kind post-hoc routing.

Per tech-design-v2-pivot.md §核心改动: response_kind decided post-hoc from
Lead's tool_invocations. Canvas-write tools (canvas.add_*) or spawn(compute) →
CANVAS; everything else → CHAT.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bowhead_core.agents.lead_orchestrator import _compute_response_kind


@dataclass
class _MockInv:
    tool_name: str
    arguments: dict[str, Any]


def test_zero_tools_returns_chat() -> None:
    """Lead 0 tool (greet / capability / definition direct) → chat."""
    assert _compute_response_kind([]) == "chat"
    assert _compute_response_kind(None) == "chat"


def test_bi_only_read_tools_returns_chat() -> None:
    """Lead 调 bi.list_measures / bi.schema / bi.query but no canvas-write → chat.

    Covers the "数据能算 EBITDA 么" case: Lead 试 BI lookup 失败 (或成功) 后 prose 答.
    """
    invs = [
        _MockInv("bi_list_measures", {}),
        _MockInv("bi_schema", {"kind": "measures"}),
    ]
    assert _compute_response_kind(invs) == "chat"


def test_kb_only_read_tools_returns_chat() -> None:
    """Lead 调 kb.search / kb.list_tables but no canvas-write → chat."""
    invs = [
        _MockInv("kb_search", {"query": "EBITDA"}),
        _MockInv("kb_list_tables", {}),
    ]
    assert _compute_response_kind(invs) == "chat"


def test_add_chart_returns_canvas() -> None:
    """Lead 调 canvas.add_chart → canvas."""
    invs = [
        _MockInv("bi_query", {}),
        _MockInv("add_chart", {"chart_section_id": "sec_x"}),
    ]
    assert _compute_response_kind(invs) == "canvas"


def test_canvas_add_kpi_row_returns_canvas() -> None:
    """canvas.add_kpi_row triggers canvas surface."""
    invs = [_MockInv("canvas_add_kpi_row", {})]
    assert _compute_response_kind(invs) == "canvas"


def test_spawn_compute_returns_canvas() -> None:
    """spawn(profile=compute) → 报告类 → canvas."""
    invs = [
        _MockInv("subagent_spawn", {"profile": "compute", "task": "report"}),
    ]
    assert _compute_response_kind(invs) == "canvas"


def test_spawn_kb_analyst_returns_canvas() -> None:
    """fleet retrofit (2026-06-01): kb-analyst upgraded from a read-only retrieval
    bot to a research analyst returning a report-level AnalysisPayload, so it was
    removed from _CHAT_ONLY_PEER_PROFILES → a kb-analyst spawn now surfaces on
    canvas like any analyst (deny-list: non-chat-only profile → canvas)."""
    invs = [
        _MockInv("subagent_spawn", {"profile": "kb-analyst", "task": "kb_research"}),
    ]
    assert _compute_response_kind(invs) == "canvas"


def test_spawn_specialist_returns_canvas() -> None:
    """spawn(profile=<any non-chat-only specialist>) → canvas via the deny-list.
    (Replaces the old diagnostic/competitive-stub tests — those dead Set-A stub
    profiles were deleted 2026-06-01; the deny-list is profile-name-agnostic, so a
    real specialist exercises the same path.)"""
    for prof in ("financial-analyst", "business-analyst", "compliance-analyst", "consultant"):
        invs = [_MockInv("subagent_spawn", {"profile": prof, "task": "分析"})]
        assert _compute_response_kind(invs) == "canvas", f"{prof} should → canvas"


def test_mixed_canvas_wins() -> None:
    """bi.* + canvas.add_chart 共存 → canvas (canvas write wins)."""
    invs = [
        _MockInv("bi_list_measures", {}),
        _MockInv("bi_query", {}),
        _MockInv("add_chart", {}),
        _MockInv("canvas_add_insight", {}),
    ]
    assert _compute_response_kind(invs) == "canvas"


def test_unknown_tool_returns_chat() -> None:
    """Unknown / new tool not in canvas-write list → chat (safe default).

    Ensures the helper doesn't false-positive canvas on未来 new tools.
    """
    invs = [_MockInv("some.future.tool", {})]
    assert _compute_response_kind(invs) == "chat"


def test_spawn_with_missing_profile_returns_chat() -> None:
    """spawn 缺 profile arg → chat (defensive, no false canvas).

    Covers edge case where subagent.spawn was called malformed.
    """
    invs = [_MockInv("subagent_spawn", {})]
    assert _compute_response_kind(invs) == "chat"
