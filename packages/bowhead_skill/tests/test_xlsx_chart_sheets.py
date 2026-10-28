"""canvas-xlsx 的 chart 支与数值格式（116 prod 1.0.89/1.0.90 dogfood）。

1.0.89 实测：同一份画布导 Word 39084 字节两张完整数据表，导 Excel **5217 字节
只有标题和 Tags**——Data sheets 循环只认 data_table/attribution/trend_chart，
而 `add_chart` 产出的 `chart` 才是 BI 报告里表格数据的主力。

1.0.90 复验：chart 支已生效（4 张图各成一个 sheet），但**数字全是裸浮点**
`3268829891.0369`——`_write_table` 的数值判据只认 `numeric: true`，而 chart 的
columns 携带的是 `type: "numeric"`，于是新加的这些 sheet 一律 General 格式。
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from bowhead_skill.catalog import builtin_catalog_dir


@pytest.fixture(scope="module")
def mod() -> Any:
    path = builtin_catalog_dir() / "canvas-xlsx/scripts/render_from_canvas.py"
    spec = importlib.util.spec_from_file_location("_r_xlsx", path)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# Actual shape on 116 for `各省店均效率对比（2026年）` (two numeric series + a geo axis).
_CHART = {
    "kind": "chart",
    "spec": {
        "title": "各省店均效率对比（2026年）",
        "kind": "bar",
        "columns": [
            {"key": "省", "header": "省", "type": "geo"},
            {"key": "店均营业额", "header": "店均营业额", "type": "numeric"},
            {"key": "店均利润", "header": "店均利润", "type": "numeric"},
        ],
        "rows": [
            {"省": "上海", "店均营业额": 297166353.7306273, "店均利润": 53666707.67077997},
            {"省": "北京", "店均营业额": 243670388.5570232, "店均利润": 41549928.46705715},
        ],
    },
}


def _render(mod: Any, sections: list[dict]) -> Any:
    from openpyxl import load_workbook

    wd = Path(tempfile.mkdtemp())
    (wd / "input.json").write_text(json.dumps({"sections": sections, "title": "T"}, ensure_ascii=False), encoding="utf-8")
    import sys

    argv = sys.argv
    sys.argv = ["x", "--input", str(wd / "input.json"), "--workdir", str(wd)]
    try:
        mod.main()
    finally:
        sys.argv = argv
    return load_workbook(wd / "output.xlsx")


def test_chart_becomes_its_own_sheet(mod: Any) -> None:
    """★ 核心：修复前一份只有 chart 的画布导出后没有任何数据 sheet。"""
    wb = _render(mod, [_CHART])
    assert "各省店均效率对比（2026年）" in wb.sheetnames
    ws = wb["各省店均效率对比（2026年）"]
    vals = [[c for c in row if c is not None] for row in ws.iter_rows(values_only=True)]
    flat = [str(v) for row in vals for v in row]
    assert "省" in flat and "店均营业额" in flat and "上海" in flat


def test_numeric_cells_are_formatted(mod: Any) -> None:
    """★ chart 的列用 `type: numeric`，不是 `numeric: true`——只认后者就全是裸浮点。"""
    wb = _render(mod, [_CHART])
    ws = wb["各省店均效率对比（2026年）"]
    fmts = [
        ws.cell(row=r, column=c).number_format for r in range(1, ws.max_row + 1) for c in (2, 3) if isinstance(ws.cell(row=r, column=c).value, float)
    ]
    assert fmts, "没有找到数值单元格"
    assert all(f == "#,##0.00" for f in fmts), f"数值列没套千分位格式: {set(fmts)}"


def test_legacy_numeric_flag_still_works(mod: Any) -> None:
    """★ 反向闸：data_table 的老形态 `numeric: true` 不能被改坏。"""
    wb = _render(
        mod,
        [
            {
                "kind": "data_table",
                "caption": "老表",
                "columns": [{"key": "a", "header": "A"}, {"key": "b", "header": "B", "numeric": True}],
                "rows": [{"a": "x", "b": 1234.5678}],
            }
        ],
    )
    ws = wb["老表"]
    cell = next(ws.cell(row=r, column=2) for r in range(1, ws.max_row + 1) if isinstance(ws.cell(row=r, column=2).value, float))
    assert cell.number_format == "#,##0.00"


def test_two_charts_with_same_title_do_not_crash(mod: Any) -> None:
    """★ openpyxl 遇重名 sheet 直接抛——两张同标题的图会让整个导出挂掉。"""
    wb = _render(mod, [_CHART, json.loads(json.dumps(_CHART))])
    named = [s for s in wb.sheetnames if s.startswith("各省店均效率对比")]
    assert len(named) == 2, wb.sheetnames


def test_chart_without_rows_is_skipped(mod: Any) -> None:
    """空图不该产出一个空 sheet。"""
    empty = {"kind": "chart", "spec": {"title": "空图", "columns": [{"key": "a", "type": "numeric"}], "rows": []}}
    wb = _render(mod, [empty])
    assert "空图" not in wb.sheetnames
