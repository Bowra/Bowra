"""☠ 一张画不进导出的图，不该让整份报告导不出来。

2026-09-20 在 116（1.3.2）复现：图改成带上全部要画的行之后，**编辑态把图种从折线切成柱状**
就能让这张图的冻结样式超过 64KB（`(门店, 日期, 金额)` 的柱状图按列序取 x 轴 ⇒ 每天一条系列）。
`_freeze_chart_presentations` 把它抛出去，`_build_snapshot_payload` 转成
`SnapshotError("snapshot_chart_style_invalid")`，用户看到的是「导出服务暂不可用，请稍后重试」——
而重试永远不会成功。建图时的闸（`chart/infer.py::_undrawable_reason`）只管新图，
编辑态改图种、存量图都绕得过它。

所以这里兜住：**只把这一张降级成表格**（数据一行不少，表格页脚会写「共 N 行」），
其余的图和整份导出照常。主题/契约坏了仍然大声失败——那是另一回事，见本文件最后一条。
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.chart import ChartColumn, infer_chart_spec, spec_to_dict
from bowhead_core.chart_style_export import (
    ChartStyleExportError,
    build_chart_style_export_theme_v2,
    parse_chart_style_export_theme,
)
from bowhead_core.export_snapshot import _freeze_chart_presentations
from bowhead_core.print_theme import current_print_theme_snapshot
from test_chart_style_export_v2 import _effective_v2


def _theme() -> dict[str, Any]:
    return parse_chart_style_export_theme(build_chart_style_export_theme_v2(_effective_v2(), print_theme=current_print_theme_snapshot()))


def _cols(*defs: tuple[str, str]) -> list[ChartColumn]:
    return [ChartColumn(key=k, header=k, type=t) for k, t in defs]  # type: ignore[arg-type]


def _oversized_chart_section() -> dict[str, Any]:
    """编辑态把一张长表折线切成柱状：x 轴按列序取「门店」，于是每一天都成了一条系列。"""
    rows = [{"门店": f"S{k}", "日期": f"2025-D{d:03d}", "金额": float(d * 7 + k)} for k in range(5) for d in range(400)]
    spec = spec_to_dict(
        infer_chart_spec(
            rows,
            _cols(("门店", "categorical"), ("日期", "date"), ("金额", "numeric")),
            hint="trend",
            title="各店金额趋势",
            force_kind="line",
        )
    )
    spec["kind"] = "bar"  # ← Chart kind selected while editing, bypassing the creation gate.
    # Size-capped charts carry `top_n` (reproduce that shape here); clear it when degrading to a table:
    # the table footer reports counts itself, so retaining it repeats the same information.
    spec["top_n"] = 1500
    return {"id": "sec_big", "kind": "chart", "payload": {"spec": spec}}


def _small_chart_section() -> dict[str, Any]:
    rows = [{"月": f"2025-{m:02d}", "金额": float(m)} for m in range(1, 13)]
    spec = spec_to_dict(infer_chart_spec(rows, _cols(("月", "date"), ("金额", "numeric")), hint="trend", title="月度金额"))
    return {"id": "sec_small", "kind": "chart", "payload": {"spec": spec}}


@pytest.mark.unit
def test_one_oversized_chart_degrades_to_table_instead_of_failing_the_export() -> None:
    big, small = _oversized_chart_section(), _small_chart_section()
    sections = [big, small]

    _freeze_chart_presentations(sections, theme=_theme(), locale="zh-CN")

    assert big["payload"]["spec"]["kind"] == "table", "画不进导出的那张降级成表格"
    # Retain every spec row (export renders the first `EXPORT_MAX_ROWS` as a table; the footer reports total N rows,
    # so readers see "showing 1-25 / total 2000", not silent omission).
    assert len(big["payload"]["spec"]["rows"]) == 2000
    assert big["payload"]["spec"]["row_count"] == 2000
    assert big["payload"]["spec"]["top_n"] is None, "表格由页脚报数，别再打一次截断标记"
    assert big["frozen_chart_presentation"]["normalizedRenderShape"]["kind"] == "table"
    assert small["payload"]["spec"]["kind"] == "line", "其余的图不受影响"
    assert small["frozen_chart_presentation"]["normalizedRenderShape"]["kind"] == "line"


@pytest.mark.unit
def test_nested_sections_are_degraded_too() -> None:
    """报告正文里的图嵌在 `section.body` 里——那条路同样要兜住。"""
    nested = {"kind": "section", "payload": {"body": [_oversized_chart_section()]}}

    _freeze_chart_presentations([nested], theme=_theme(), locale="zh-CN")

    inner = nested["payload"]["body"][0]
    assert inner["payload"]["spec"]["kind"] == "table"
    assert inner["frozen_chart_presentation"]["normalizedRenderShape"]["kind"] == "table"


@pytest.mark.unit
def test_degraded_chart_tells_the_reader_why_it_is_a_table() -> None:
    """降级只发一条服务端日志是不够的：读者在画布上看到的是图、在 PDF 里看到的是表。"""
    big = _oversized_chart_section()
    _freeze_chart_presentations([big], theme=_theme(), locale="zh-CN")
    assert "数据量过大" in big["payload"]["spec"]["subtitle"]

    en = _oversized_chart_section()
    _freeze_chart_presentations([en], theme=_theme(), locale="en-US")
    assert "exported as a table" in en["payload"]["spec"]["subtitle"]


@pytest.mark.unit
def test_a_broken_theme_still_fails_loudly() -> None:
    """负对照：主题/契约坏了必须整份停下来，不能被「降级成表格」这条路吞掉。"""
    theme = _theme()
    theme["style"]["resolvedPresetSnapshot"]["kindTokens"] = {}  # Broken preset snapshot

    with pytest.raises(ChartStyleExportError):
        _freeze_chart_presentations([_small_chart_section()], theme=theme, locale="zh-CN")


@pytest.mark.unit
def test_only_the_too_large_error_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 上一条负对照管不住「把 except 放宽成所有导出样式错误」这个突变——

    坏主题在降级后的**第二次**冻结时照样抛，于是测试照样绿（复审逮到的空转）。
    这里直接钉「非尺寸类错误必须原样抛」：让冻结只抛一次普通 `ChartStyleExportError`，
    降级那条路一旦把它也吞掉，第二次冻结会成功、异常消失，本例立刻红。
    """
    import bowhead_core.export_snapshot as snapshot_module

    calls = {"n": 0}

    def fake_freeze(spec: dict[str, Any], *, theme: dict[str, Any], locale: str) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ChartStyleExportError("structural theme binding is invalid")
        return {"normalizedRenderShape": {"kind": spec.get("kind")}}

    monkeypatch.setattr(snapshot_module, "freeze_chart_presentation_v2", fake_freeze)

    with pytest.raises(ChartStyleExportError):
        _freeze_chart_presentations([_small_chart_section()], theme=_theme(), locale="zh-CN")
    assert calls["n"] == 1, "不该为了降级再冻一次——这类错误本就该原样抛"
