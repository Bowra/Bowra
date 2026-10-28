"""只有壳没有内容的 payload 不许进画布（dev 1.0.77 dogfood P2-e）。

实测形态：用户说「把画布上这份报告导出成 Word 文档（.docx）给我」，那一轮什么分析
都没做，peer 却回了个 `title` = 用户那句指令的 payload ⇒ 画布上多出一段「标题 = 那
句指令」的空报告。它本身难看，更坏的是**污染此后每一次导出**——导出读整份画布投影，
于是这句指令被当成正文标题烤进 xlsx/docx/pdf（`art_f2dd91d8ea22` 里就有第二个 Title）。
"""

from __future__ import annotations

from bowhead_core.canvas_composer import _build_sections_from_payload


def _kinds(sections):
    return [s.get("kind") for s in sections]


def test_title_only_payload_emits_nothing() -> None:
    """★ 核心：导出轮那个 payload 的真实形状。"""
    out = _build_sections_from_payload(
        {"title": "把画布上这份报告导出成 Word 文档（.docx）给我"},
        None,
        tool_invocations=[],
    )
    assert out == []


def test_title_eyebrow_meta_shell_emits_nothing() -> None:
    """壳凑齐了也还是壳。"""
    out = _build_sections_from_payload(
        {"title": "导出成 PDF 发我", "eyebrow": "导出"},
        None,
        tool_invocations=[],
    )
    assert out == []


def test_real_report_with_narrative_survives() -> None:
    """有正文就是真报告——一段都不能少。"""
    out = _build_sections_from_payload(
        {"title": "8 月经营汇报", "exec_summary": "8 月营收 2.1 亿，同比增长 12%。"},
        None,
        tool_invocations=[],
    )
    assert "h1" in _kinds(out)
    assert "narrative" in _kinds(out)


def test_report_with_only_kpis_survives() -> None:
    """短报告也是报告：只要有 KPI 这类可读内容就放行。"""
    out = _build_sections_from_payload(
        {"title": "本月速览", "kpis": [{"label": "营收", "value": "¥2.1亿"}]},
        None,
        tool_invocations=[],
    )
    assert "kpi_row" in _kinds(out)
    assert "h1" in _kinds(out)


def test_empty_payload_still_empty() -> None:
    """本来就空的不受影响（不是本次改动引入的新行为）。"""
    assert _build_sections_from_payload({}, None, tool_invocations=[]) == []


def test_shell_with_meta_pill_still_skipped() -> None:
    """intent 非 None 时 composer 会补一段 meta —— 壳更像样了，但仍是壳。"""
    from bowhead_core.types import IntentType

    out = _build_sections_from_payload(
        {"title": "导出成 Word"},
        next(iter(IntentType)),
        tool_invocations=[],
    )
    assert out == []
