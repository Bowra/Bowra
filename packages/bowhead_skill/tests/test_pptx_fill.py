"""`pptx_fill` — filling an existing PowerPoint file inside the sandbox.

Grounded in the 116 template fill of 2026-11-16 (the regression case): 11 of 15 script failures were one
IndexError from writing more rows than a table had, and a hand-written chart edit left `c:ptCount`
behind its points so LibreOffice refused the file. The template had bar-and-line combinations, a
scatter chart and tables with vertical merges; the fixtures below reproduce those shapes.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType

import pytest
from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData, XyChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

pytestmark = [pytest.mark.unit]

_SCRIPTS = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts"


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fill = _load("pptx_fill_under_test", "pptx_fill.py")


def _combo(slide) -> int:
    """A clustered column chart whose last two series are moved into a line plot on the same axes."""
    data = CategoryChartData(number_format="#,##0")
    data.categories = ["A", "B", "C"]
    for name in ("Actual", "Target", "LY", "Ach%"):
        data.add_series(name, (1.0, 2.0, 3.0))
    frame = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.5), Inches(0.5), Inches(5), Inches(3), data)
    plot_area = frame.chart._chartSpace.find(".//" + qn("c:plotArea"))
    bar = plot_area.find(qn("c:barChart"))
    line = etree.SubElement(plot_area, qn("c:lineChart"))
    bar.addnext(line)
    etree.SubElement(line, qn("c:grouping")).set("val", "standard")
    etree.SubElement(line, qn("c:varyColors")).set("val", "0")
    for series in bar.findall(qn("c:ser"))[2:]:
        line.append(series)
    etree.SubElement(line, qn("c:marker")).set("val", "1")
    for axis in bar.findall(qn("c:axId")):
        etree.SubElement(line, qn("c:axId")).set("val", axis.get("val"))
    # The percent series keeps its own format, as in the template.
    ach = line.findall(qn("c:ser"))[1]
    ach.find(".//" + qn("c:val") + "//" + qn("c:formatCode")).text = "0%"
    return frame.shape_id


def _scatter(slide) -> int:
    data = XyChartData()
    series = data.add_series("Stores")
    series.add_data_point(1, 2)
    series.add_data_point(3, 4)
    return slide.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER, Inches(6), Inches(0.5), Inches(3), Inches(3), data).shape_id


def _merged_table(slide) -> int:
    frame = slide.shapes.add_table(5, 4, Inches(0.5), Inches(4), Inches(6), Inches(1.5))
    table = frame.table
    for r in range(5):
        for c in range(4):
            cell = table.cell(r, c)
            cell.text = f"r{r}c{c}"
            run = cell.text_frame.paragraphs[0].runs[0]
            run.font.size = Pt(9)
            run.font.bold = r == 0
    table.cell(2, 3).merge(table.cell(3, 3))
    return frame.shape_id


def _deck() -> tuple[object, dict[str, int]]:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(0.5), Inches(6.8), Inches(4), Inches(0.4))
    box.text_frame.text = "Sample title"
    run = box.text_frame.paragraphs[0].runs[0]
    run.font.bold = True
    run.font.size = Pt(20)
    ids = {"combo": _combo(slide), "scatter": _scatter(slide), "table": _merged_table(slide), "title": box.shape_id}
    return prs, ids


def _reopen(prs):
    buffer = io.BytesIO()
    prs.save(buffer)
    return Presentation(io.BytesIO(buffer.getvalue()))


def test_describe_lists_tables_merges_and_chart_plots():
    prs, ids = _deck()
    text = fill.describe(prs)
    assert text.startswith("1 slides, 720x540 pt.")
    assert f"id {ids['table']} " in text and "table 5 rows x 4 columns; merged: r3-4 c4" in text
    assert "chart bar+line; series (4): Actual, Target, LY, Ach% [bar: 2 series; line: 2 series]; categories: 3" in text
    assert "'0%'" in text and "chart scatter; series (1): Stores" in text
    assert "text: 'Sample title'" in text
    assert fill.find_text(prs, "r1c2") == [{"slide": 1, "shape_id": ids["table"], "name": "Table 4", "row": 1, "column": 2, "text": "r1c2"}]


def test_set_text_keeps_the_first_run_format_on_every_line():
    prs, ids = _deck()
    fill.set_text(prs, 1, ids["title"], "2026 review\nNational")
    shape = next(s for s in _reopen(prs).slides[0].shapes if s.shape_id == ids["title"])
    paragraphs = shape.text_frame.paragraphs
    assert [p.text for p in paragraphs] == ["2026 review", "National"]
    assert all(p.runs[0].font.bold and p.runs[0].font.size == Pt(20) for p in paragraphs)


def test_set_table_writes_cells_skips_merged_ones_and_states_both_sizes_when_too_large():
    prs, ids = _deck()
    outcome = fill.set_table(prs, 1, ids["table"], [["a", None, "c", "d"], ["e", "f", "g", "h"], ["i", "j", "k", "l"]], first_row=1)
    assert outcome == {"written": 10, "not_written_merged_cells": [(3, 3)]}
    table = next(s for s in _reopen(prs).slides[0].shapes if s.shape_id == ids["table"]).table
    assert table.cell(1, 0).text == "a" and table.cell(1, 1).text == "r1c1"
    assert table.cell(1, 0).text_frame.paragraphs[0].runs[0].font.size == Pt(9)
    with pytest.raises(ValueError, match=r"has 5 rows x 4 columns; the values reach row 6 and column 4\. resize_table"):
        fill.set_table(prs, 1, ids["table"], [["x"] * 4] * 5, first_row=1)


def test_resize_table_adds_and_removes_rows_across_a_merge_and_keeps_the_frame_with_the_rows():
    prs, ids = _deck()
    grown = fill.resize_table(prs, 1, ids["table"], 10, copy_row=1)
    shape = next(s for s in prs.slides[0].shapes if s.shape_id == ids["table"])
    assert grown["rows"] == 10 and shape.height == sum(row.height for row in shape.table.rows)
    assert grown["frame_now_overlaps"] == [f"id {ids['title']} 'TextBox 1'"]
    added = shape.table.cell(9, 0)
    assert added.text == "" and added.text_frame.paragraphs[0].runs[0].font.size == Pt(9)
    shrunk = fill.resize_table(prs, 1, ids["table"], 3)
    table = next(s for s in _reopen(prs).slides[0].shapes if s.shape_id == ids["table"]).table
    assert shrunk["rows"] == 3 and len(table.rows) == 3
    # The merge over rows 3-4 lost row 4, so row 3 is an ordinary cell again.
    assert not table.cell(2, 3).is_merge_origin and not table.cell(2, 3).is_spanned


def test_set_chart_replaces_a_combination_and_a_scatter_chart_keeping_formats_and_point_counts():
    prs, ids = _deck()
    result = fill.set_chart(
        prs,
        1,
        ids["combo"],
        categories=["Jan", "Feb", "Mar", "Apr", "May"],
        series=[("Actual", [1, 2, 3, 4, 5]), ("Target", [2, 2, 2, 2, 2]), ("LY", [1, 1, 1, 1, 1]), ("Ach%", [0.5, 1, 1.5, 2, 2.5])],
    )
    assert result == {"plots": ["bar", "line"], "series": 4, "categories": 5}
    chart = next(s for s in _reopen(prs).slides[0].shapes if s.shape_id == ids["combo"]).chart
    assert [list(series.values) for series in chart.plots[1].series][1] == [0.5, 1, 1.5, 2, 2.5]
    formats = [fill._series_format(series) for plot in chart.plots for series in plot.series]
    assert formats == ["#,##0", "#,##0", "#,##0", "0%"]
    for cache in chart._chartSpace.iter(qn("c:numCache"), qn("c:strCache")):
        points = [int(point.get("idx")) for point in cache.findall(qn("c:pt"))]
        assert int(cache.find(qn("c:ptCount")).get("val")) == max(points) + 1
    with pytest.raises(ValueError, match=r"has 2 plots \(bar: 2, line: 2\), 4 series in all; 1 series were given"):
        fill.set_chart(prs, 1, ids["combo"], categories=["a"], series=[("x", [1])])

    fill.set_chart(prs, 1, ids["scatter"], series=[("Stores", [(1, 1), (2, 4), (3, 9)])])
    scatter = next(s for s in _reopen(prs).slides[0].shapes if s.shape_id == ids["scatter"]).chart
    assert list(scatter.plots[0].series[0].values) == [1, 4, 9]


def test_add_chart_adds_each_kind_in_a_box_given_in_points():
    prs, _ = _deck()
    for kind in ("column", "bar", "stacked_column", "line", "area", "pie"):
        shape_id = fill.add_chart(prs, 1, (10, 10, 200, 120), kind, ["a", "b"], [("S", [1, 2])], number_format="0.0", title="T", font_size=9)
        assert any(s.shape_id == shape_id and s.has_chart for s in prs.slides[0].shapes)
    shape_id = fill.add_chart(prs, 1, (10, 10, 200, 120), "scatter", series=[("S", [(1, 2), (2, 3)])])
    frame = next(s for s in prs.slides[0].shapes if s.shape_id == shape_id)
    assert frame.left == Pt(10) and frame.width == Pt(200)
    with pytest.raises(ValueError, match="kind 'donut' is not one of"):
        fill.add_chart(prs, 1, (0, 0, 10, 10), "donut", ["a"], [("S", [1])])


def test_an_unknown_shape_or_slide_lists_what_exists():
    prs, ids = _deck()
    with pytest.raises(ValueError, match=rf"slide 1 has no shape with id 999; its shapes are: .*{ids['table']} 'Table 4'"):
        fill.set_text(prs, 1, 999, "x")
    with pytest.raises(ValueError, match="slide 2 does not exist; the presentation has 1 slides"):
        fill.describe(prs, slides=[2])


def test_the_runner_copies_the_module_into_every_call(tmp_path):
    runner = _load("runner_for_pptx_fill", "runner.py")
    runner._stage_sandbox_helpers(tmp_path)
    assert (tmp_path / "pptx_fill.py").read_bytes() == (_SCRIPTS / "pptx_fill.py").read_bytes()
