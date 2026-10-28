"""☠ 一张退化成 table 的图，能让整份报告的 PPTX/PDF/XLSX 导出 503。

2026-09-08 在 116 复现并抓到根因：`resolve_chart_presentation_v2` 里是裸下标
`kind_tokens[shape["kind"]]`，而预设快照的 `kindTokens` 按 `CHART_STYLE_KINDS`
（12 档 Chart.js 图种）建，**没有 `table`/`kpi`/`geo`**。
导出 IR 在 `renderer_contract_version=2` 下又要求**每个** chart 段都带一份合法的
冻结呈现（`export_ir.py::_frozen_chart_presentation`），于是
`_freeze_chart_presentations` 一 `KeyError`，整个 `POST /canvas/{id}/exports` 就 503。

116 实测影响面：**203/582 = 34.9% 的画布**（table 191 / kpi 20）导不出来。
★ 而且 503 发生在**建任务之前**，`canvas_export_jobs` 里一条 error 都不留
（近 30 天 0 条）——「查作业表没失败」证明不了导出是好的。

★ 前端同一处 `tokensFor`（chart-style-presentation.ts）一直有兜底，Python 侧没有。
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.chart_style_export import export_chart_render_context_v2
from bowhead_core.chart_style_materializer import CHART_STYLE_KINDS
from bowhead_core.chart_style_presentation import (
    KINDS_WITHOUT_STYLE_TOKENS,
    normalize_chart_for_rendering_v1,
    resolve_chart_presentation_v2,
)
from bowhead_core.chart_style_registry_generated import CHART_STYLE_PRESENTATION_POLICY

_PALETTE = ["#2C5F2D", "#97BC62", "#F4A261", "#E76F51"]
_ACCESSIBILITY = {"contrast": "standard", "colorVision": "standard"}


def _kind_tokens() -> dict[str, Any]:
    """与 materializer 同构：所有 kind 共用一套 tooltip/grid。"""
    tokens: dict[str, Any] = {}
    for kind in CHART_STYLE_KINDS:
        entry: dict[str, Any] = {"tooltip": "enabled", "grid": "quiet"}
        if kind in {"line", "area_stacked", "dual_axis", "combo"}:
            entry["line"] = {"curve": "monotone", "widthPx": 2, "points": "auto", "pointRadiusPx": 3}
        if kind in {"bar", "hbar", "stacked_bar", "waterfall"}:
            entry["bar"] = {"radiusPx": 4}
        tokens[kind] = entry
    return tokens


def _spec(kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "title": "各省营业额",
        "columns": [
            {"key": "省", "header": "省", "type": "geo"},
            {"key": "v", "header": "营业额", "type": "numeric"},
        ],
        "rows": [{"省": "上海", "v": 3557}, {"省": "北京", "v": 3435}],
        "row_count": 2,
    }


def _resolve(kind: str, tokens: dict[str, Any] | None = None) -> dict[str, Any]:
    _, shape = normalize_chart_for_rendering_v1(_spec(kind))
    return resolve_chart_presentation_v2(
        shape,
        kind_tokens=_kind_tokens() if tokens is None else tokens,
        categorical_series_palette=_PALETTE,
        accessibility=_ACCESSIBILITY,
        render_context=export_chart_render_context_v2(locale="zh-CN"),
        color_only_series_capacity=None,
    )


@pytest.mark.unit
def test_the_three_kinds_really_are_absent_from_the_snapshot() -> None:
    """正对照：这个兜底集合必须**正好**是快照缺的那几档。

    写死 `{"table","kpi","geo"}` 而快照其实有它们的话，下面几条就成了空转；
    反过来若 `CHART_STYLE_KINDS` 以后少了一档，这里会先红，而不是等生产 503。
    """
    assert KINDS_WITHOUT_STYLE_TOKENS.isdisjoint(CHART_STYLE_KINDS)
    known = set(CHART_STYLE_PRESENTATION_POLICY["familiesByKind"])
    assert known - set(CHART_STYLE_KINDS) == KINDS_WITHOUT_STYLE_TOKENS


@pytest.mark.unit
@pytest.mark.parametrize("kind", sorted(KINDS_WITHOUT_STYLE_TOKENS))
def test_kinds_without_tokens_resolve_instead_of_raising(kind: str) -> None:
    """★ 核心：这三档必须**解析得出来**，不能抛——抛一次就是整份报告导不出来。"""
    resolved = _resolve(kind)
    assert resolved["requested"]["tooltip"]
    assert resolved["requested"]["grid"]


@pytest.mark.unit
def test_fallback_reads_the_preset_values_rather_than_inventing_them() -> None:
    """兜底的 tooltip/grid 是从快照里**读**的，不是写死的默认值。

    materializer 对所有 kind 用同一套（:255），团队 override 也对
    `supportedChartKinds` 全量套用（:315），所以任意一档的值就是这份预设的值。
    """
    tokens = _kind_tokens()
    for entry in tokens.values():
        entry["tooltip"] = "disabled"
        entry["grid"] = "standard"
    resolved = _resolve("table", tokens)
    # Static exports downgrade tooltip; requested contains the original value read by the fallback.
    assert resolved["requested"]["tooltip"] == "disabled"
    assert resolved["requested"]["grid"] == "standard"


@pytest.mark.unit
def test_a_missing_chartjs_kind_still_raises_loudly() -> None:
    """⚠ 只对那三档兜底。真 Chart.js 图种缺 token = 预设/注册表坏了，要大声挂，
    不能悄悄拿默认值把一张真图渲出来。"""
    tokens = _kind_tokens()
    del tokens["bar"]
    with pytest.raises(KeyError):
        _resolve("bar", tokens)
