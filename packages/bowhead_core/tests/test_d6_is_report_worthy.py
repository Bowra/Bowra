"""D6 (canvas-empty bug 2026-05-25) — is_report_worthy unit tests.

Cover the section-type composition rule that decides whether a turn's
canvas sections are "report-worthy" (keep visible) or chat-mode (hide).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bowhead_core.agents.lead_orchestrator import is_report_worthy


@dataclass
class FakeSection:
    kind: str
    payload: dict[str, Any]


def _sec(kind: str, **payload: Any) -> FakeSection:
    return FakeSection(kind=kind, payload=payload)


def test_empty_returns_not_worthy() -> None:
    keep, reason = is_report_worthy([])
    assert keep is False
    assert "no sections" in reason


def test_h1_kpi_only_hides() -> None:
    """Lead self-narrate + D3 fallback path: h1 + kpi_row only."""
    sections = [
        _sec("h1", text="数据查询结果"),
        _sec("kpi_row", cells=[{"lbl": "GMV", "val": "51.8亿"}]),
    ]
    keep, reason = is_report_worthy(sections)
    assert keep is False
    assert "chat-mode" in reason
    assert "kpi_row" in reason


def test_insight_card_keeps() -> None:
    """compute peer with insights → keep canvas."""
    sections = [
        _sec("h1", text="同比分析"),
        _sec("kpi_row", cells=[{"lbl": "GMV", "val": "51.8亿"}]),
        _sec("insight_card", title="持平", thesis="..."),
    ]
    keep, reason = is_report_worthy(sections)
    assert keep is True
    assert "insight_card" in reason


def test_recommendations_keeps() -> None:
    """recommendations section → keep canvas (analysis action items)."""
    sections = [
        _sec("h1", text="..."),
        _sec("chart", spec={}),
        _sec("recommendations", items=[{"action": "..."}]),
    ]
    keep, reason = is_report_worthy(sections)
    assert keep is True
    assert "recommendations" in reason


def test_executive_narrative_keeps() -> None:
    """narrative with style=executive (exec_summary) → keep canvas."""
    sections = [
        _sec("h1", text="..."),
        _sec("narrative", markdown="**关键发现**...", style="executive"),
        _sec("kpi_row", cells=[]),
    ]
    keep, reason = is_report_worthy(sections)
    assert keep is True
    assert "narrative(executive)" in reason


def test_caveats_narrative_alone_hides() -> None:
    """narrative with style=caveats only → not a report signal (caveats are
    meta-info, can appear without analysis)."""
    sections = [
        _sec("h1", text="..."),
        _sec("kpi_row", cells=[]),
        _sec("narrative", markdown="数据局限...", style="caveats"),
    ]
    keep, reason = is_report_worthy(sections)
    assert keep is False, f"caveats-only narrative should hide: {reason}"


def test_multi_peer_aggregate_keeps() -> None:
    """Multi-peer turn: compute + financial each writes mini-report.
    Any peer's insight_card triggers keep."""
    sections = [
        # compute peer
        _sec("h1", text="GMV 同比"),
        _sec("kpi_row", cells=[]),
        # financial-analyst peer
        _sec("chart", spec={}),
        _sec("insight_card", title="ROE 上升"),
    ]
    keep, reason = is_report_worthy(sections)
    assert keep is True
    assert "insight_card" in reason


def test_kb_narrative_only_hides() -> None:
    """kb-analyst returns narrative + sources only → hide (knowledge retrieval
    is chat-like, not analysis)."""
    sections = [
        _sec("narrative", markdown="找到 3 篇文档...", style="executive"),
        _sec("sources", entries=[]),
    ]
    # Note: this returns True because narrative has style=executive. The user's
    # framework had this as edge case ("kb-analyst is chat-like"). For now we
    # accept that executive-narrative is treated as report-worthy across all
    # peers; if kb dogfood shows over-canvas, narrow narrative rule to require
    # accompanying insight.
    keep, _ = is_report_worthy(sections)
    assert keep is True  # current rule


def test_only_chart_kpi_data_table_hides() -> None:
    """Pure data-viz combo (chart + kpi + data_table) → hide (visualization
    without analysis = chat-mode by Dim2 rule)."""
    sections = [
        _sec("chart", spec={}),
        _sec("kpi_row", cells=[]),
        _sec("data_table", rows=[]),
    ]
    keep, reason = is_report_worthy(sections)
    assert keep is False
    assert "chat-mode" in reason


# ── pptx-from-template M3 (RD-7 / CR-6): deliverable keep-signal ──────────────


def test_deliverable_keeps_even_with_viz_only_sections() -> None:
    """A deck-generate turn may add only viz sections (chart/kpi) + a deliverable
    artifact. Without the signal it would chat-mode-hide and the user sees
    nothing; has_deliverable=True forces keep."""
    sections = [_sec("chart", spec={}), _sec("kpi_row", cells=[])]
    keep, reason = is_report_worthy(sections, has_deliverable=True)
    assert keep is True
    assert "deliverable" in reason


def test_deliverable_keeps_even_with_no_sections() -> None:
    """A pure render/edit turn (no canvas section) + deliverable → keep
    (short-circuits before the empty-sections check)."""
    keep, reason = is_report_worthy([], has_deliverable=True)
    assert keep is True
    assert "deliverable" in reason


def test_design_section_keeps_even_without_deliverable() -> None:
    sections = [
        _sec(
            "design",
            title="夏季奶茶海报",
            layers=[{"id": "title", "type": "text", "text": "夏日冰爽"}],
        ),
    ]
    keep, reason = is_report_worthy(sections, has_deliverable=False)
    assert keep is True
    assert "design" in reason


def test_no_deliverable_default_preserves_legacy_behavior() -> None:
    """Default has_deliverable=False → viz-only still hides (no regression)."""
    sections = [_sec("kpi_row", cells=[])]
    keep, _ = is_report_worthy(sections)
    assert keep is False
    keep2, _ = is_report_worthy(sections, has_deliverable=False)
    assert keep2 is False


# ── Planning Mode: a plan-execution turn is always report-worthy ──────────────


def test_plan_execution_keeps_when_planner_declared_report() -> None:
    """A plan-execution turn keeps its canvas when the PLANNER said report — viz-only
    sections don't matter (its narrative can be mis-tagged; live e2e 2026-06-25:
    31 charts chat-mode-hidden). 批 5ⓐ 起判据是「声明」而不是「是不是 plan 轮」：
    没有声明的 plan 轮落证据（见 test_plan_execution_without_declaration_falls_to_evidence）。"""
    sections = [_sec("chart", spec={}), _sec("chart", spec={}), _sec("kpi_row", cells=[])]
    keep, reason = is_report_worthy(sections, is_plan_execution=True, plan_deliverable_kind="report")
    assert keep is True
    assert "declared: canvas" in reason


def test_plan_execution_without_declaration_falls_to_evidence() -> None:
    """没有 planner 声明也没有 compose 期决定的 plan 轮：按证据判（viz-only → chat）。"""
    sections = [_sec("chart", spec={}), _sec("chart", spec={}), _sec("kpi_row", cells=[])]
    keep, _ = is_report_worthy(sections, is_plan_execution=True)
    assert keep is False


def test_plan_execution_default_false_preserves_legacy() -> None:
    """Default is_plan_execution=False → a single-chart viz turn still hides (no
    regression for normal quick-answer turns)."""
    sections = [_sec("chart", spec={}), _sec("kpi_row", cells=[])]
    keep, _ = is_report_worthy(sections)
    assert keep is False


# ── #7 (dingsiyan 2026-07-07): chart-heavy reports must not vanish ────────────


def test_multi_chart_alone_is_no_longer_a_report_signal() -> None:
    """批 5ⓐ（用户拍板 2026-08-20）：**几张图算不算报告是 agent 的判断，不由后端数**。
    「≥2 图 ⇒ 报告」这条 2026-07-07 的补丁删除——当年它存在是因为 peer 没有表达交付面的通道；
    现在 peer/planner/Lead 都能声明，声明缺席才落证据。
    """
    sections = [
        _sec("chart", spec={}),
        _sec("chart", spec={}),
        _sec("code_block", code="import pandas"),
    ]
    keep, reason = is_report_worthy(sections)
    assert keep is False
    assert "chat-mode" in reason


def test_declared_canvas_keeps_chart_heavy_turn() -> None:
    """「报告失踪」（#7, 116 上 10 轮）的新守卫：用户要报告 ⇒ Lead/peer 声明 canvas ⇒ 留画布，
    与图的张数无关。"""
    sections = [_sec("chart", spec={}), _sec("code_block", code="")]
    keep, reason = is_report_worthy(sections, declared="canvas")
    assert keep is True
    assert "declared: canvas" in reason


def test_declared_chat_never_destroys_unrenderable_content() -> None:
    """★ review-R3/R4：声明 chat 决定的是**交付面**，不是「把已经写出来的东西删掉」。

    chat 气泡只能内联 chart/kpi_row/data_table；本轮若已落 executive narrative（M-C 亲笔正文）、
    insight_card、recommendations，藏画布＝内容凭空消失。交付面仍归 agent，内容不许销毁。
    """
    keep, reason = is_report_worthy([_sec("narrative", style="executive")], declared="chat")
    assert keep is True and "can't render" in reason
    keep2, _ = is_report_worthy([_sec("insight_card")], declared="chat")
    assert keep2 is True
    # canvas-elements: document projections render insight cards / recommendations as narratives with style=insight / recommendations;
    # split chapters remain insights and body content. Hiding the canvas destroys them too (fallback found in dogfood §5.4).
    for _style in ("insight", "recommendations"):
        keep_p, reason_p = is_report_worthy([_sec("narrative", style=_style)], declared="chat")
        assert keep_p is True and f"narrative({_style})" in reason_p
    # Continue reclaiming shells and inlineable content.
    keep3, reason3 = is_report_worthy([_sec("h1"), _sec("meta"), _sec("chart", spec={}), _sec("code_block", code="")], declared="chat")
    assert keep3 is False and "declared: chat" in reason3
    # A plain narrative is an aside, not body content (same as historical chat-mode hiding).
    keep4, _ = is_report_worthy([_sec("narrative", style="plain")], declared="chat")
    assert keep4 is False
    # review2-R1: images can be inlined in chat bubbles (`_inline_image_artifact`); do not turn a chat declaration into a report,
    # or both chat and canvas render a copy while the chat directive explicitly forbids mentioning the canvas, leaving users unaware of it.
    keep5, _ = is_report_worthy([_sec("image", artifact_id="art_png")], declared="chat")
    assert keep5 is False


def test_declared_canvas_without_sections_is_not_an_empty_report() -> None:
    """★ review-R2：声明只决定交付面，不能凭空造内容——没 section 就别留空画布（D6 canvas-empty）。"""
    keep, reason = is_report_worthy([], declared="canvas")
    assert keep is False and "nothing materialized" in reason


def test_deliverable_outranks_a_chat_declaration() -> None:
    """★ review-R7：「别写报告，做成 PPT」= chat 声明 + 真产物；按声明藏画布会把 deck preview
    一起藏掉（pptx M3 RD-7/CR-6）。产物在，画布留。"""
    keep, reason = is_report_worthy([_sec("chart", spec={})], has_deliverable=True, declared="chat")
    assert keep is True and "deliverable" in reason


def test_single_chart_still_chat_mode() -> None:
    """A single chart (+ code) with no declaration stays chat-mode (evidence fallback)."""
    sections = [_sec("chart", spec={}), _sec("code_block", code="")]
    keep, reason = is_report_worthy(sections)
    assert keep is False
    assert "chat-mode" in reason
