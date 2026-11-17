"""Helpers for filling an existing PowerPoint file with python-pptx.

    from pptx import Presentation
    from pptx_fill import describe, find_text, set_text, set_table, resize_table, set_chart, add_chart

    prs = Presentation(INPUTS["template"]["path"])
    print(describe(prs))                     # shape ids, tables, merges, charts, formats per slide
    set_text(prs, 1, 2, "2026年1-10月经营回顾")
    resize_table(prs, 4, 8, rows=11)         # header + 10 body rows
    set_table(prs, 4, 8, rows, first_row=1)  # rows: list of lists of display text
    set_chart(prs, 2, 12, categories=["1月", "2月"], series=[("销售额", [1.2, 3.4]), ("同比", [0.05, -0.02])])
    prs.save("deck.pptx")

Slides are numbered from 1. Shapes are addressed by their id, as `describe` prints it.
Values are written as given: pass the text a reader should see ("1,234.5", "12.3%").
Each function edits the presentation in memory; nothing is saved until `prs.save(...)`.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Sequence
from typing import Any

from pptx.chart.data import CategoryChartData, XyChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.oxml.ns import qn
from pptx.text.text import TextFrame
from pptx.util import Pt

__all__ = ["add_chart", "describe", "find_text", "resize_table", "set_chart", "set_table", "set_text"]

_EMU_PER_POINT = 12700
_TEXT_HEAD = 40


# ── finding things ─────────────────────────────────────────────────────────


def _slide(prs: Any, slide: int) -> Any:
    count = len(prs.slides)
    if not isinstance(slide, int) or not 1 <= slide <= count:
        raise ValueError(f"slide {slide!r} does not exist; the presentation has {count} slides, numbered from 1.")
    return prs.slides[slide - 1]


def _walk(shapes: Iterable[Any]) -> Iterable[Any]:
    for shape in shapes:
        yield shape
        if shape.shape_type is not None and "GROUP" in str(shape.shape_type):
            yield from _walk(shape.shapes)


def _shape(prs: Any, slide: int, shape_id: int) -> Any:
    for shape in _walk(_slide(prs, slide).shapes):
        if shape.shape_id == shape_id:
            return shape
    listed = ", ".join(f"{shape.shape_id} '{shape.name}'" for shape in _walk(_slide(prs, slide).shapes))
    raise ValueError(f"slide {slide} has no shape with id {shape_id!r}; its shapes are: {listed}.")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _head(text: str, limit: int = _TEXT_HEAD) -> str:
    flat = _flat(text)
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _plot_kind(plot: Any) -> str:
    return plot._element.tag.split("}")[1].removesuffix("Chart")


def _series_format(series: Any) -> str | None:
    element = series._element
    for tag in ("c:val", "c:yVal"):
        node = element.find(qn(tag))
        if node is not None:
            code = node.find(".//" + qn("c:formatCode"))
            if code is not None and code.text:
                return code.text
    return None


def _merges(table: Any) -> list[str]:
    ranges = []
    for r, row in enumerate(table.rows):
        for c, cell in enumerate(row.cells):
            if cell.is_merge_origin:
                rows = f"r{r + 1}" if cell.span_height == 1 else f"r{r + 1}-{r + cell.span_height}"
                cols = f"c{c + 1}" if cell.span_width == 1 else f"c{c + 1}-{c + cell.span_width}"
                ranges.append(rows + " " + cols)
    return ranges


def _describe_shape(shape: Any) -> str:
    label = f"id {shape.shape_id} '{shape.name}'"
    if getattr(shape, "has_table", False) and shape.has_table:
        table = shape.table
        merges = _merges(table)
        first = " | ".join(_head(row.cells[0].text, 16) for row in table.rows)
        return (
            f"{label} table {len(table.rows)} rows x {len(table.columns)} columns"
            + (f"; merged: {', '.join(merges)}" if merges else "")
            + f"; first column: {first}"
        )
    if getattr(shape, "has_chart", False) and shape.has_chart:
        chart = shape.chart
        plots = list(chart.plots)
        kinds = "+".join(_plot_kind(plot) for plot in plots)
        names = [series.name or "(unnamed)" for plot in plots for series in plot.series]
        formats = sorted({code for plot in plots for series in plot.series if (code := _series_format(series))})
        try:
            categories = len(plots[0].categories) if plots and _plot_kind(plots[0]) not in {"scatter", "bubble"} else None
        except Exception:  # noqa: BLE001 - a chart without category references still has series
            categories = None
        per_plot = "; ".join(f"{_plot_kind(plot)}: {len(list(plot.series))} series" for plot in plots) if len(plots) > 1 else ""
        return (
            f"{label} chart {kinds}; series ({len(names)}): {', '.join(names)}"
            + (f" [{per_plot}]" if per_plot else "")
            + (f"; categories: {categories}" if categories is not None else "")
            + (f"; value formats: {', '.join(repr(code) for code in formats)}" if formats else "")
        )
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame and shape.text_frame.text.strip():
        return f"{label} text: '{_head(shape.text_frame.text)}'"
    return ""


def describe(prs: Any, slides: Sequence[int] | None = None) -> str:
    """A text listing of the shapes that carry text, tables or charts, per slide."""
    numbers = list(slides) if slides is not None else list(range(1, len(prs.slides) + 1))
    width, height = prs.slide_width / _EMU_PER_POINT, prs.slide_height / _EMU_PER_POINT
    lines = [f"{len(prs.slides)} slides, {width:.0f}x{height:.0f} pt."]
    for number in numbers:
        slide = _slide(prs, number)
        entries, other = [], 0
        for shape in _walk(slide.shapes):
            entry = _describe_shape(shape)
            if entry:
                entries.append(entry)
            elif shape.shape_type is None or "GROUP" not in str(shape.shape_type):
                other += 1
        lines.append(f"slide {number}:" + (f" ({other} other shapes without text)" if other else ""))
        lines.extend("  " + entry for entry in entries)
    return "\n".join(lines)


def find_text(prs: Any, needle: str) -> list[dict[str, Any]]:
    """Every shape and table cell whose text contains `needle`."""
    found = []
    for number, slide in enumerate(prs.slides, 1):
        for shape in _walk(slide.shapes):
            if getattr(shape, "has_table", False) and shape.has_table:
                for r, row in enumerate(shape.table.rows):
                    for c, cell in enumerate(row.cells):
                        if needle in cell.text:
                            found.append({"slide": number, "shape_id": shape.shape_id, "name": shape.name, "row": r, "column": c, "text": cell.text})
            elif getattr(shape, "has_text_frame", False) and shape.has_text_frame and needle in shape.text_frame.text:
                found.append({"slide": number, "shape_id": shape.shape_id, "name": shape.name, "text": shape.text_frame.text})
    return found


# ── text ───────────────────────────────────────────────────────────────────


def _write(text_frame: Any, text: Any) -> None:
    """Replace the text, keeping the first paragraph's and first run's formatting for every line."""
    body = text_frame._txBody
    paragraphs = body.findall(qn("a:p"))
    first = paragraphs[0] if paragraphs else None
    paragraph_properties = first.find(qn("a:pPr")) if first is not None else None
    run_properties = None
    if first is not None:
        run = first.find(qn("a:r"))
        if run is not None and run.find(qn("a:rPr")) is not None:
            run_properties = run.find(qn("a:rPr"))
        elif first.find(qn("a:endParaRPr")) is not None:
            run_properties = first.find(qn("a:endParaRPr"))
    paragraph_properties = copy.deepcopy(paragraph_properties) if paragraph_properties is not None else None
    run_properties = copy.deepcopy(run_properties) if run_properties is not None else None
    for paragraph in paragraphs:
        body.remove(paragraph)
    for line in ("" if text is None else str(text)).split("\n"):
        paragraph = body.add_p()
        if paragraph_properties is not None:
            paragraph.insert(0, copy.deepcopy(paragraph_properties))
        run = paragraph.add_r()
        run.text = line
        if run_properties is not None:
            properties = copy.deepcopy(run_properties)
            properties.tag = qn("a:rPr")
            run.insert(0, properties)


def set_text(prs: Any, slide: int, shape_id: int, text: str) -> None:
    """Replace a text shape's text; each line keeps the format of the shape's first paragraph and run."""
    shape = _shape(prs, slide, shape_id)
    if not getattr(shape, "has_text_frame", False) or not shape.has_text_frame:
        raise ValueError(f"shape {shape_id} '{shape.name}' on slide {slide} has no text frame; use set_table for a table.")
    _write(shape.text_frame, text)


# ── tables ─────────────────────────────────────────────────────────────────


def _table(prs: Any, slide: int, shape_id: int) -> tuple[Any, Any]:
    shape = _shape(prs, slide, shape_id)
    if not getattr(shape, "has_table", False) or not shape.has_table:
        raise ValueError(f"shape {shape_id} '{shape.name}' on slide {slide} is not a table.")
    return shape, shape.table


def set_table(prs: Any, slide: int, shape_id: int, values: Sequence[Sequence[Any]], first_row: int = 0, first_col: int = 0) -> dict[str, Any]:
    """Write a grid of values into a table, starting at (first_row, first_col), counted from 0.

    None leaves a cell as it is. A value for a cell covered by a merge is not written; such cells are
    listed in the return value.
    """
    shape, table = _table(prs, slide, shape_id)
    rows, columns = len(table.rows), len(table.columns)
    height = len(values)
    width = max((len(row) for row in values), default=0)
    if first_row + height > rows or first_col + width > columns:
        raise ValueError(
            f"table '{shape.name}' (id {shape_id}) on slide {slide} has {rows} rows x {columns} columns; "
            f"the values reach row {first_row + height} and column {first_col + width}. resize_table changes the row count."
        )
    written, merged = 0, []
    for i, row in enumerate(values):
        for j, value in enumerate(row):
            if value is None:
                continue
            cell = table.cell(first_row + i, first_col + j)
            if cell.is_spanned:
                merged.append((first_row + i, first_col + j))
                continue
            _write(cell.text_frame, value)
            written += 1
    return {"written": written, "not_written_merged_cells": merged}


def _clear_merge_marks(row_element: Any) -> None:
    for cell in row_element.findall(qn("a:tc")):
        for attribute in ("rowSpan", "vMerge"):
            if attribute in cell.attrib:
                del cell.attrib[attribute]


def resize_table(prs: Any, slide: int, shape_id: int, rows: int, copy_row: int | None = None) -> dict[str, Any]:
    """Set a table's total row count.

    Rows are added after the last row as copies of `copy_row` (default: the last row), with their text
    emptied and their formatting kept; rows are removed from the end. The frame height follows the rows.
    Returns the new frame bottom, the slide height and the shapes the frame overlaps now but did not before.
    """
    shape, table = _table(prs, slide, shape_id)
    before = _rect(shape)
    tbl = table._tbl
    current = list(tbl.tr_lst)
    if not isinstance(rows, int) or rows < 1:
        raise ValueError(f"rows must be at least 1; the table has {len(current)}.")
    if rows > len(current):
        source = current[-1 if copy_row is None else copy_row]
        for _ in range(rows - len(current)):
            row = copy.deepcopy(source)
            _clear_merge_marks(row)
            tbl.append(row)
            for cell in row.findall(qn("a:tc")):
                body = cell.find(qn("a:txBody"))
                if body is not None:
                    _write(TextFrame(body, None), "")
    elif rows < len(current):
        for index in range(len(current) - 1, rows - 1, -1):
            # A merge that started above a removed row loses that row.
            for above in range(index):
                for cell in current[above].findall(qn("a:tc")):
                    span = int(cell.get("rowSpan", "1"))
                    if span > 1 and above + span - 1 >= index:
                        if span - 1 > 1:
                            cell.set("rowSpan", str(span - 1))
                        else:
                            del cell.attrib["rowSpan"]
            tbl.remove(current[index])
    total = sum(row.height for row in table.rows)
    shape.height = total
    bottom = (shape.top + total) / _EMU_PER_POINT
    after = _rect(shape)
    newly = [
        f"id {other.shape_id} '{other.name}'"
        for other in _slide(prs, slide).shapes
        if other is not shape
        and (rect := _rect(other)) is not None
        and _intersects(after, rect)
        and not (before is not None and _intersects(before, rect))
    ]
    return {
        "rows": len(table.rows),
        "frame_bottom_pt": round(bottom, 1),
        "slide_height_pt": round(prs.slide_height / _EMU_PER_POINT, 1),
        "frame_now_overlaps": newly,
    }


def _rect(shape: Any) -> tuple[int, int, int, int] | None:
    if None in (shape.left, shape.top, shape.width, shape.height):
        return None
    return (shape.left, shape.top, shape.left + shape.width, shape.top + shape.height)


def _intersects(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
    return min(first[2], second[2]) > max(first[0], second[0]) and min(first[3], second[3]) > max(first[1], second[1])


# ── charts ─────────────────────────────────────────────────────────────────

_SCATTER = {"scatter", "bubble"}


def _chart(prs: Any, slide: int, shape_id: int) -> tuple[Any, Any]:
    shape = _shape(prs, slide, shape_id)
    if not getattr(shape, "has_chart", False) or not shape.has_chart:
        raise ValueError(f"shape {shape_id} '{shape.name}' on slide {slide} is not a chart.")
    return shape, shape.chart


def _pairs(series: Any) -> list[tuple[str, Any]]:
    items = list(series.items()) if isinstance(series, dict) else list(series)
    for item in items:
        if not isinstance(item, (tuple, list)) or len(item) != 2 or not isinstance(item[0], str):
            raise ValueError("series is a list of (name, values) pairs or a dict of name -> values.")
    return [(name, values) for name, values in items]


def set_chart(prs: Any, slide: int, shape_id: int, categories: Sequence[Any] | None = None, series: Any = ()) -> dict[str, Any]:
    """Replace a chart's data, keeping its styling and each series' number format.

    Category charts (column, bar, line, area, pie, and combinations of them): `categories` and
    `series` as [(name, [values])]. For more than one plot the series count stays the same and
    the series fill the plots in their current order.
    Scatter charts: `series` as [(name, [(x, y), ...])]; `categories` is not used.
    """
    shape, chart = _chart(prs, slide, shape_id)
    plots = list(chart.plots)
    existing = [s for plot in plots for s in plot.series]
    pairs = _pairs(series)
    kinds = [_plot_kind(plot) for plot in plots]
    if len(plots) > 1 and len(pairs) != len(existing):
        counts = ", ".join(f"{kind}: {len(list(plot.series))}" for kind, plot in zip(kinds, plots, strict=True))
        raise ValueError(
            f"chart '{shape.name}' (id {shape_id}) on slide {slide} has {len(plots)} plots ({counts}), "
            f"{len(existing)} series in all; {len(pairs)} series were given."
        )
    formats = [_series_format(s) for s in existing]
    if any(kind in _SCATTER for kind in kinds):
        data = XyChartData()
        for index, (name, points) in enumerate(pairs):
            code = formats[index] if index < len(formats) and formats[index] not in (None, "General") else None
            target = data.add_series(name, number_format=code)
            for point in points:
                if not isinstance(point, (tuple, list)) or len(point) != 2:
                    raise ValueError(f"scatter series '{name}' takes (x, y) pairs.")
                target.add_data_point(point[0], point[1])
    else:
        if categories is None:
            raise ValueError(f"chart '{shape.name}' (id {shape_id}) is a {'+'.join(kinds)} chart; pass categories.")
        labels = list(categories)
        data = CategoryChartData()
        data.categories = labels
        for index, (name, values) in enumerate(pairs):
            values = list(values)
            if len(values) != len(labels):
                raise ValueError(f"series '{name}' has {len(values)} values for {len(labels)} categories.")
            code = formats[index] if index < len(formats) and formats[index] not in (None, "General") else None
            data.add_series(name, values, number_format=code)
    chart.replace_data(data)
    return {"plots": kinds, "series": len(pairs), "categories": None if any(kind in _SCATTER for kind in kinds) else len(list(categories or []))}


_KINDS = {
    "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "bar": XL_CHART_TYPE.BAR_CLUSTERED,
    "stacked_column": XL_CHART_TYPE.COLUMN_STACKED,
    "line": XL_CHART_TYPE.LINE_MARKERS,
    "area": XL_CHART_TYPE.AREA,
    "pie": XL_CHART_TYPE.PIE,
    "scatter": XL_CHART_TYPE.XY_SCATTER,
}


def add_chart(
    prs: Any,
    slide: int,
    box: Sequence[float],
    kind: str,
    categories: Sequence[Any] | None = None,
    series: Any = (),
    *,
    number_format: str | None = None,
    title: str | None = None,
    font_name: str | None = None,
    font_size: float | None = None,
    legend: bool = True,
) -> int:
    """Add a native chart in `box` = (left, top, width, height) in points; returns the new shape id.

    kind: column, bar, stacked_column, line, area, pie or scatter. Data as for set_chart.
    """
    if kind not in _KINDS:
        raise ValueError(f"kind {kind!r} is not one of: {', '.join(_KINDS)}.")
    if len(box) != 4:
        raise ValueError("box is (left, top, width, height) in points.")
    pairs = _pairs(series)
    if kind == "scatter":
        data: Any = XyChartData(number_format=number_format) if number_format else XyChartData()
        for name, points in pairs:
            target = data.add_series(name)
            for x, y in points:
                target.add_data_point(x, y)
    else:
        data = CategoryChartData(number_format=number_format) if number_format else CategoryChartData()
        data.categories = list(categories or [])
        for name, values in pairs:
            data.add_series(name, list(values))
    left, top, width, height = (Pt(value) for value in box)
    frame = _slide(prs, slide).shapes.add_chart(_KINDS[kind], left, top, width, height, data)
    chart = frame.chart
    chart.has_legend = legend and (len(pairs) > 1 or kind == "pie")
    if title:
        chart.has_title = True
        chart.chart_title.text_frame.text = title
    else:
        chart.has_title = False
    if font_name or font_size:
        if font_name:
            chart.font.name = font_name
        if font_size:
            chart.font.size = Pt(font_size)
    return frame.shape_id
