"""delivery-surface C7 —— 报告轮 chat 内联的有界选取。

原二值互斥（keep → 只留 file chip）把 BI 报告轮的 chat 档饿死：116 修复后窗口
17 轮 inline_artifacts 里 chart 全来自纯 sandbox 轮。C7 改为报告轮也内联一小组：
≤1 kpi_row + 正文 [figure:] 引用最多的 ≤1 张 chart。
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.agents.lead_orchestrator import _select_bounded_inline


def _sec(kind: str, sec_id: str, payload: dict | None = None) -> SimpleNamespace:
    s = SimpleNamespace(kind=kind, id=sec_id, payload=payload or {})
    s.to_report_section = lambda: {"id": sec_id, "kind": kind, **(payload or {})}
    return s


def test_selects_most_referenced_chart() -> None:
    sections = [
        _sec("h1", "sec_h1", {"text": "T"}),
        _sec("chart", "sec_c1", {"spec": {"kind": "line"}}),
        _sec("chart", "sec_c2", {"spec": {"kind": "bar"}}),
        _sec("narrative", "sec_n", {"markdown": "见 [figure:sec_c2] 与 [figure:sec_c2]，另 [figure:sec_c1]。"}),
    ]
    out = _select_bounded_inline(sections)
    charts = [a for a in out if a["kind"] == "chart"]
    assert [c["id"] for c in charts] == ["sec_c2"], "该选正文引用最多的那张"


def test_zero_refs_falls_back_to_first_chart() -> None:
    sections = [
        _sec("chart", "sec_c1", {"spec": {"kind": "line"}}),
        _sec("chart", "sec_c2", {"spec": {"kind": "bar"}}),
    ]
    out = _select_bounded_inline(sections)
    charts = [a for a in out if a["kind"] == "chart"]
    assert [c["id"] for c in charts] == ["sec_c1"], "零引用取 seq 序首张"


def test_kpi_row_capped_at_one() -> None:
    sections = [
        _sec("kpi_row", "sec_k1", {"cells": [1]}),
        _sec("kpi_row", "sec_k2", {"cells": [2]}),
    ]
    out = _select_bounded_inline(sections)
    kpis = [a for a in out if a["kind"] == "kpi"]
    assert [k["id"] for k in kpis] == ["sec_k1"]


def test_specless_chart_never_selected() -> None:
    """spec 为空的 chart FE 渲不了——即使引用最多也跳过。"""
    sections = [
        _sec("chart", "sec_bad", {}),
        _sec("chart", "sec_ok", {"spec": {"kind": "pie"}}),
        _sec("narrative", "sec_n", {"markdown": "[figure:sec_bad]"}),
    ]
    out = _select_bounded_inline(sections)
    charts = [a for a in out if a["kind"] == "chart"]
    assert [c["id"] for c in charts] == ["sec_ok"]


def test_no_charts_no_kpi_returns_empty() -> None:
    assert _select_bounded_inline([_sec("h1", "s", {"text": "t"}), _sec("narrative", "n", {"markdown": "x"})]) == []
