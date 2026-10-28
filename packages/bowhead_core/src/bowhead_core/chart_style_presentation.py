"""Renderer-independent chart normalization and presentation resolution."""

from __future__ import annotations

import copy
import dataclasses
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from bowhead_core.chart_style_registry_generated import CHART_STYLE_PRESENTATION_POLICY


def _spec_dict(raw_spec: Mapping[str, Any] | object) -> dict[str, Any]:
    if dataclasses.is_dataclass(raw_spec) and not isinstance(raw_spec, type):
        return dataclasses.asdict(raw_spec)
    if isinstance(raw_spec, Mapping):
        return copy.deepcopy(dict(raw_spec))
    raise TypeError("chart spec must be a mapping or dataclass")


def _finite_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _coerce_finite_number(value: Any) -> float | int | None:
    """Match finite browser pivot conversion while preserving absent observations."""

    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    numeric = _finite_number(value)
    if numeric is not None:
        return numeric
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = float(int(stripped, 0)) if re.fullmatch(r"0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+", stripped) else float(stripped)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _string_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _infer_columns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sample = next((row for row in rows if row), None)
    if sample is None:
        return []
    return [
        {
            "key": key,
            "header": key,
            "type": "numeric" if _finite_number(value) is not None else "categorical",
        }
        for key, value in sample.items()
    ]


def _role_ordered_dimensions(dimensions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order dimensions by the producer's x and color roles.

    Mirror frontend roleOrderedDims: hierarchical stacked bars put the child on the category axis and the parent
    in the series. Specs without both declared roles retain their original column order.
    """
    x = next((column for column in dimensions if column.get("role") == "x"), None)
    color = next((column for column in dimensions if column.get("role") == "color" and column is not x), None)
    if x is None or color is None:
        return dimensions
    return [x, color, *(column for column in dimensions if column is not x and column is not color)]


def _pivot_long_spec(spec: dict[str, Any]) -> dict[str, Any] | None:
    policy = CHART_STYLE_PRESENTATION_POLICY["normalization"]
    if spec.get("kind") not in policy["pivotableKinds"]:
        return None
    columns = spec.get("columns") or []
    rows = spec.get("rows") or []
    dimensions = _role_ordered_dimensions([column for column in columns if column.get("type") in {"date", "categorical", "geo"}])
    numeric = [column for column in columns if column.get("type") == "numeric"]
    if len(numeric) != 1 or len(dimensions) != 2 or len(rows) < 2:
        return None
    x_column = (
        dimensions[0]
        if spec.get("kind") in {"bar", "hbar", "stacked_bar"}
        else next((column for column in dimensions if column.get("type") == "date"), dimensions[0])
    )
    group_column = next(column for column in dimensions if column.get("key") != x_column.get("key"))
    x_values = list(dict.fromkeys(_string_cell(row.get(x_column["key"])) for row in rows))
    group_values = list(dict.fromkeys(_string_cell(row.get(group_column["key"])) for row in rows))
    if len(x_values) < 2:
        return None
    fill = len(rows) / (len(x_values) * max(1, len(group_values)))
    if fill < policy["minimumLongTableFillRatio"]:
        return None

    metric = numeric[0]
    cells: dict[str, dict[str, float | int]] = {}
    for row in rows:
        x_value = _string_cell(row.get(x_column["key"]))
        group_value = _string_cell(row.get(group_column["key"]))
        value = _coerce_finite_number(row.get(metric["key"]))
        if value is None:
            continue
        bucket = cells.setdefault(x_value, {})
        bucket[group_value] = bucket.get(group_value, 0) + value
    if len(group_values) < 2:
        return None
    series_columns = []
    for group_value in group_values:
        column = copy.deepcopy(metric)
        column.update(
            {
                "key": f"__pivot__{group_value}",
                "header": group_value,
                "source_header": metric.get("source_header") or metric.get("header"),
                "measure_header": metric.get("header"),
            }
        )
        series_columns.append(column)
    wide_rows = []
    for x_value in x_values:
        row: dict[str, Any] = {x_column["key"]: x_value}
        for group_value, column in zip(group_values, series_columns, strict=True):
            row[column["key"]] = cells.get(x_value, {}).get(group_value)
        wide_rows.append(row)
    result = copy.deepcopy(spec)
    result["columns"] = [copy.deepcopy(x_column), *series_columns]
    result["rows"] = wide_rows
    result["row_count"] = len(wide_rows)
    return result


def _transpose_time_wide_spec(spec: dict[str, Any]) -> dict[str, Any] | None:
    policy = CHART_STYLE_PRESENTATION_POLICY["normalization"]
    if spec.get("kind") not in policy["transposableKinds"]:
        return None
    columns = spec.get("columns") or []
    rows = spec.get("rows") or []
    if len(rows) < 2 or any(column.get("type") == "date" for column in columns):
        return None
    dimensions = [column for column in columns if column.get("type") in {"categorical", "geo"}]
    numeric = [column for column in columns if column.get("type") == "numeric"]
    if len(dimensions) != 1 or len(numeric) < policy["minimumTimeColumns"]:
        return None
    time_pattern = re.compile(policy["timeHeaderPattern"])
    if not all(time_pattern.fullmatch(str(column.get("header") or column.get("key") or "")) for column in numeric):
        return None
    dimension = dimensions[0]
    series_columns = []
    for index, row in enumerate(rows):
        column = copy.deepcopy(numeric[0])
        column.update(
            {
                "key": f"__t__{index}",
                "header": _string_cell(row.get(dimension["key"])) or f"#{index + 1}",
                "source_header": numeric[0].get("source_header") or numeric[0].get("header"),
                "measure_header": numeric[0].get("measure_header") or "",
            }
        )
        series_columns.append(column)
    time_column = {"key": dimension["key"], "header": "", "type": "date"}
    wide_rows = []
    for numeric_column in numeric:
        row: dict[str, Any] = {dimension["key"]: str(numeric_column.get("header") or numeric_column.get("key") or "")}
        for index, source_row in enumerate(rows):
            row[series_columns[index]["key"]] = _coerce_finite_number(source_row.get(numeric_column["key"]))
        wide_rows.append(row)
    result = copy.deepcopy(spec)
    result["columns"] = [time_column, *series_columns]
    result["rows"] = wide_rows
    result["row_count"] = len(wide_rows)
    return result


def _grapheme_length(value: str) -> int:
    count = 0
    joined = False
    regional_open = False
    for character in value:
        codepoint = ord(character)
        if character == "\u200d":
            joined = True
            continue
        if unicodedata.combining(character) or 0xFE00 <= codepoint <= 0xFE0F or 0x1F3FB <= codepoint <= 0x1F3FF:
            continue
        regional = 0x1F1E6 <= codepoint <= 0x1F1FF
        if joined:
            joined = False
        elif regional and regional_open:
            regional_open = False
        else:
            count += 1
            regional_open = regional
    return count


def _series_entry(key: str, role: str, values: list[float | int | None], stack_key: str | None) -> dict[str, Any]:
    return {
        "key": key,
        "role": role,
        "pointCount": len(values),
        "stackKey": stack_key,
        "hasPositive": any(value is not None and value > 0 for value in values),
        "hasNegative": any(value is not None and value < 0 for value in values),
    }


def _dual_axis_columns(columns: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    numeric = [column for column in columns if column.get("type") == "numeric"]
    if any(isinstance(column.get("y_axis_id"), int) for column in numeric):
        bars = [column for column in numeric if column.get("y_axis_id", 0) == 0]
        lines = [column for column in numeric if column.get("y_axis_id", 0) == 1]
        if bars and lines:
            return bars, lines
    return numeric[:1], numeric[1:]


def _derive_series(spec: dict[str, Any]) -> tuple[int, list[dict[str, Any]], list[list[float | int | None]]]:
    kind = spec.get("kind")
    rows = spec.get("rows") or []
    columns = spec.get("columns") or []
    numeric = [column for column in columns if column.get("type") == "numeric"]
    dimensions = [column for column in columns if column.get("type") in {"categorical", "date", "geo"}]

    def values(column: dict[str, Any]) -> list[float | int | None]:
        return [_finite_number(row.get(column["key"])) for row in rows]

    if kind == "pie":
        metric = numeric[0] if numeric else None
        values_by_series = [[_finite_number(row.get(metric["key"])) if metric else None] for row in rows]
        return (
            len(rows),
            [_series_entry(f"segment:{index}", "segment", series_values, None) for index, series_values in enumerate(values_by_series)],
            values_by_series,
        )
    if kind == "scatter":
        if len(numeric) < 2:
            return len(rows), [], []
        x_column, y_column = numeric[:2]
        group_column = next((column for column in dimensions if column.get("type") == "categorical"), None)
        valid_rows = [
            row for row in rows if _finite_number(row.get(x_column["key"])) is not None and _finite_number(row.get(y_column["key"])) is not None
        ]
        if group_column is None:
            series_values = [_finite_number(row.get(y_column["key"])) for row in valid_rows]
            return len(valid_rows), [_series_entry("point:0", "point", series_values, None)], [series_values]
        groups: dict[str, list[float | int | None]] = {}
        for row in valid_rows:
            groups.setdefault(_string_cell(row.get(group_column["key"])), []).append(_finite_number(row.get(y_column["key"])))
        values_by_series = list(groups.values())
        return (
            sum(map(len, values_by_series)),
            [_series_entry(f"point:{index}", "point", series_values, None) for index, series_values in enumerate(values_by_series)],
            values_by_series,
        )
    if kind == "stacked_bar" and len(dimensions) >= 2 and numeric:
        x_column, group_column = _role_ordered_dimensions(dimensions)[:2]
        metric = next((column for column in numeric if column.get("key") == spec.get("sort_by")), numeric[0])
        # Deduplicate with ordered dicts (O(rows)). Previously used category not in categories on a list: rows × categories,
        # invisible with only 50 chart rows, but sparse stacked bars with all 10000 rows then took tens of seconds,
        # synchronously on the gateway event loop (chart/infer.py's series gate and export freezing both use this).
        categories: dict[str, None] = {}
        groups: dict[str, None] = {}
        cells: dict[tuple[str, str], float | int] = {}
        for row in rows:
            category = _string_cell(row.get(x_column["key"]))
            group = _string_cell(row.get(group_column["key"]))
            categories.setdefault(category)
            groups.setdefault(group)
            cell_key = (category, group)
            cells[cell_key] = cells.get(cell_key, 0) + (_finite_number(row.get(metric["key"])) or 0)
        values_by_series = [[cells.get((category, group), 0) for category in categories] for group in groups]
        return (
            len(categories),
            [_series_entry(f"bar:{index}", "bar", series_values, "stack:0") for index, series_values in enumerate(values_by_series)],
            values_by_series,
        )
    if kind == "dual_axis":
        bars, lines = _dual_axis_columns(columns)
        ordered = [(column, "bar") for column in bars] + [(column, "line") for column in lines]
        values_by_series = [values(column) for column, _ in ordered]
        return (
            len(rows),
            [_series_entry(f"{role}:{index}", role, values_by_series[index], None) for index, (_column, role) in enumerate(ordered)],
            values_by_series,
        )
    if kind == "combo":
        values_by_series = [values(column) for column in numeric]
        return (
            len(rows),
            [
                _series_entry(
                    f"{'bar' if index == 0 else 'line'}:{index}",
                    "bar" if index == 0 else "line",
                    values_by_series[index],
                    None,
                )
                for index, _column in enumerate(numeric)
            ],
            values_by_series,
        )
    if kind == "waterfall":
        metric_values = values(numeric[0]) if numeric else []
        values_by_series = [[*metric_values, sum(value or 0 for value in metric_values)]]
        return len(rows) + 1, [_series_entry("bar:0", "bar", values_by_series[0], None)], values_by_series
    if kind in {"heatmap", "sankey"}:
        metric_values = values(numeric[0]) if numeric else []
        return len(rows), [_series_entry("segment:0", "segment", metric_values, None)], [metric_values]
    role = "area" if kind == "area_stacked" else "line" if kind == "line" else "bar"
    stacked = kind in {"area_stacked", "stacked_bar"}
    values_by_series = [values(column) for column in numeric]
    return (
        len(rows),
        [_series_entry(f"{role}:{index}", role, values_by_series[index], "stack:0" if stacked else None) for index, _column in enumerate(numeric)],
        values_by_series,
    )


def _bar_stack_edges(values_by_series: list[list[float | int | None]], category_count: int) -> dict[str, Any]:
    positive: list[int | None] = []
    negative: list[int | None] = []
    for category_index in range(category_count):
        positive_index = None
        negative_index = None
        for series_index, values in enumerate(values_by_series):
            value = values[category_index] if category_index < len(values) else None
            if value is not None and value > 0:
                positive_index = series_index
            if value is not None and value < 0:
                negative_index = series_index
        positive.append(positive_index)
        negative.append(negative_index)
    return {
        "positiveOuterSeriesIndexByCategory": positive,
        "negativeOuterSeriesIndexByCategory": negative,
    }


def derive_normalized_chart_render_shape_v1(normalized_spec: Mapping[str, Any] | object) -> dict[str, Any]:
    """Derive the canonical data-free shape from an already normalized spec."""

    spec = _spec_dict(normalized_spec)
    rows = spec.get("rows") or []
    columns = spec.get("columns") or []
    dimensions = [column for column in columns if column.get("type") in {"categorical", "date", "geo"}]
    numeric = [column for column in columns if column.get("type") == "numeric"]
    category_count, series, values_by_series = _derive_series(spec)
    analytical = spec.get("_export_analytical", {})
    if analytical.get("preset") == "series_plot":
        category_count = len({json.dumps(row.get(analytical["x_key"]), ensure_ascii=False, separators=(",", ":")) for row in rows})
        values_by_series = [
            [_finite_number(row.get(analytical["value_key"])) for row in rows if row.get(analytical["series_key"]) == value]
            for value in analytical["binding"]["series_values"]
        ]
        series = [
            {
                "key": "analytical:" + json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                "role": "line",
                "pointCount": len(values),
                "stackKey": None,
                "hasPositive": any(v is not None and v > 0 for v in values),
                "hasNegative": any(v is not None and v < 0 for v in values),
            }
            for value, values in zip(analytical["binding"]["series_values"], values_by_series, strict=True)
        ]
    all_values = [value for values in values_by_series for value in values]
    stacked = spec.get("kind") in {"area_stacked", "stacked_bar"}
    shape = {
        "version": CHART_STYLE_PRESENTATION_POLICY["renderShapeVersion"],
        "kind": spec.get("kind"),
        "family": CHART_STYLE_PRESENTATION_POLICY["familiesByKind"][spec.get("kind")],
        "rowCount": len(rows),
        "categoryCount": category_count,
        "seriesCount": len(series),
        "longestLabelGraphemes": max(
            (_grapheme_length(_string_cell(row.get(column["key"]))) for row in rows for column in dimensions),
            default=0,
        ),
        "series": series,
        "flags": {
            "stacked": stacked,
            "crossesZero": any(value is not None and value > 0 for value in all_values)
            and any(value is not None and value < 0 for value in all_values),
            "hasMissingValues": any(_finite_number(row.get(column["key"])) is None for row in rows for column in numeric),
        },
    }
    if stacked and any(item["role"] == "bar" for item in series):
        shape["barStackEdges"] = _bar_stack_edges(values_by_series, category_count)
    return shape


def normalize_chart_for_rendering_v1(raw_spec: Mapping[str, Any] | object) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the sole infer -> pivot -> transpose recipe without mutating ChartSpec."""

    spec = _spec_dict(raw_spec)
    if not spec.get("columns"):
        spec["columns"] = _infer_columns(spec.get("rows") or [])
    if spec.get("_export_analytical"):
        return spec, derive_normalized_chart_render_shape_v1(spec)
    spec = _pivot_long_spec(spec) or spec
    spec = _transpose_time_wide_spec(spec) or spec
    return spec, derive_normalized_chart_render_shape_v1(spec)


#: Three kinds bypass Chart.js, each using its own component (table / KPI card / map),
#: so preset snapshots already omit them from kindTokens. CHART_STYLE_KINDS
#: (chart_style_materializer.py:28) lists only 12 Chart.js kinds.
#: Frontend build-config.ts:171 likewise keeps these three out of builders.
KINDS_WITHOUT_STYLE_TOKENS: frozenset[str] = frozenset({"table", "kpi", "geo"})


def _kind_tokens_for(kind_tokens: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    """取这一档的风格 token；`table`/`kpi`/`geo` 没有就兜底。

    ☠ 此前这里是裸下标 `kind_tokens[kind]`。而导出 IR 在 `renderer_contract_version=2`
    下要求**每个** chart 段都带一份合法的冻结呈现
    （`export_ir.py::_frozen_chart_presentation`：值为 None 直接 `invalid_...`），
    于是报告里只要有**一张退化成 table 的图**，`_freeze_chart_presentations` 就
    `KeyError: 'table'` ⇒ **整份 PPTX/PDF/XLSX 导出 503**。
    116 实测 **203/582 = 34.9% 的画布**中招（table 191 / kpi 20）。
    ★ 而且这个 503 发生在**建任务之前**，`canvas_export_jobs` 里一条 error 都不留
    （近 30 天 0 条）——「查作业表没失败」证明不了导出是好的。

    ★ 前端同一处 `tokensFor`（chart-style-presentation.ts:72）一直有兜底，
    Python 侧没有：又一次「一个概念两处实现，一处有兜底一处没有」。

    兜底只取 `tooltip`/`grid`，且是从快照里**读**出来的、不是编的：materializer
    对所有 kind 用同一个 grid/tooltip 建表（:255），团队 override 也是对
    `supportedChartKinds` 全量套用（:315），所以任意一档的值就是这份预设的值。
    这三档不经 Chart.js，`line`/`bar` 子 token 用不上，不补。

    ⚠ 其余 kind 缺 token **仍然抛**：那意味着预设/注册表坏了，得大声挂，
    不能悄悄拿默认值把一张真图渲出来。
    """
    supplied = kind_tokens.get(kind)
    if supplied is not None:
        return supplied
    if kind not in KINDS_WITHOUT_STYLE_TOKENS:
        raise KeyError(kind)
    sibling = next((value for value in kind_tokens.values() if isinstance(value, Mapping)), None)
    if sibling is None:
        raise KeyError(kind)
    return {"tooltip": sibling["tooltip"], "grid": sibling["grid"]}


#: Roles whose marks are areas; they carry the fill-pattern channel.
_FILLED_ROLES = frozenset({"bar", "area", "segment"})


def _effective_capacity(capacity: int, palette_length: int) -> int:
    cap = CHART_STYLE_PRESENTATION_POLICY["nonColor"]["colorOnlySeriesCap"]
    capacity = min(capacity, palette_length) if cap is None else min(capacity, palette_length, int(cap))
    return max(1, capacity)


def assign_series_encodings(
    roles: list[str],
    *,
    palette_length: int,
    capacity: int,
    color_safe: bool,
    stacked: bool,
) -> list[dict[str, Any]] | None:
    """Assign every series its (color, second channel) pair, or ``None`` when color alone is enough.

    Mirrors ``assignSeriesEncodings`` in ``lib/chart/chart-style-presentation.ts``; both run
    ``presentation_fixtures.json`` and ``series_encoding_grid.json`` so the export paints
    exactly the pattern the screen paints.

    * Color never changes meaning: series ``i`` keeps palette color ``i % palette_length``,
      exactly as before textures existed, so no chart shows fewer distinct colors than it
      used to (a low-capacity team palette must not collapse two series into one color).
    * ``len(roles) <= capacity`` (and not color-safe) keeps color only: unchanged pixels.
    * Above it the second channel is layered on top: series ``i`` asks for channel
      ``i // capacity`` (color-safe: ``i``), i.e. block ``b`` of ``capacity`` series gets the
      ``b``-th pattern / dash. Within a block all colors are distinct; colors repeated or
      too close across blocks differ in channel.
    * The (color, channel) pair stays unique per mark family as long as no color is used by
      more series than there are variants (always true up to ``palette_length × variants``
      series): a taken pair moves to the color's next free channel. Only when a color has used
      every variant do its pairs start repeating; the color itself never moves.
    * Only stacked bar segments get the 1px surface-colored separator: segments touch, and
      every palette color has ≥3:1 against the surface, so the seam is always visible.
      Grouped bars do not touch; there the border only ate into thin bars.
    """

    policy = CHART_STYLE_PRESENTATION_POLICY["nonColor"]
    palette_length = max(1, palette_length)
    capacity = _effective_capacity(capacity, palette_length)
    if not color_safe and len(roles) <= capacity:
        return None
    line_variants = min(len(policy["lineDashes"]), len(policy["pointStyles"]))
    used: dict[tuple[str, int], set[int]] = {}
    encodings: list[dict[str, Any]] = []
    for index, role in enumerate(roles):
        family = "fill" if role in _FILLED_ROLES else "line"
        variants = len(policy["fillPatterns"]) if family == "fill" else line_variants
        color = index % palette_length
        taken = used.setdefault((family, color), set())
        if len(taken) >= variants:
            taken.clear()
        preferred_channel = (index if color_safe else index // capacity) % variants
        channel = next(candidate for step in range(variants) for candidate in [(preferred_channel + step) % variants] if candidate not in taken)
        taken.add(channel)
        encodings.append(
            {
                "paletteIndex": color,
                "lineDash": copy.deepcopy(policy["lineDashes"][channel % len(policy["lineDashes"])]),
                "pointStyle": policy["pointStyles"][channel % len(policy["pointStyles"])],
                "fillPattern": policy["fillPatterns"][channel] if family == "fill" else "solid",
                "boundaryWidthPx": policy["filledMarkBoundaryWidthPx"] if stacked and role == "bar" else 0,
            }
        )
    return encodings


def _encoding_label(series: Mapping[str, Any]) -> str:
    return (
        f"{series['key']}:dash={'-'.join(map(str, series['lineDash']))}:point={series['pointStyle']}"
        f":fill={series['fillPattern']}:boundary={series['boundaryWidthPx']}"
    )


def resolve_chart_presentation_v2(
    shape: Mapping[str, Any],
    *,
    kind_tokens: Mapping[str, Any],
    categorical_series_palette: list[str],
    accessibility: Mapping[str, str],
    render_context: Mapping[str, Any],
    color_only_series_capacity: int | None,
) -> dict[str, Any]:
    """Resolve the same canonical presentation contract used by the browser."""

    policy = CHART_STYLE_PRESENTATION_POLICY
    reasons = policy["reasonCodes"]
    source = _kind_tokens_for(kind_tokens, str(shape["kind"]))
    requested: dict[str, Any] = {
        "tooltip": source["tooltip"],
        "grid": source["grid"],
        "series": [
            {
                "key": item["key"],
                "paletteIndex": index,
                "lineDash": [],
                "pointStyle": "circle",
                "fillPattern": "solid",
                "boundaryWidthPx": 0,
            }
            for index, item in enumerate(shape["series"])
        ],
    }
    if "line" in source:
        requested["line"] = {
            **source["line"],
            "hitRadiusPx": policy["line"]["hiddenHitRadiusPx"],
            "pointHoverRadiusPx": max(policy["line"]["minimumHoverRadiusPx"], source["line"]["pointRadiusPx"] + 1),
        }
    if "bar" in source:
        requested["bar"] = copy.deepcopy(source["bar"])
    effective = copy.deepcopy(requested)
    adjustments: dict[str, dict[str, Any]] = {}

    def adjust(path: str, requested_value: Any, effective_value: Any, owner: str, reason: str) -> None:
        if requested_value == effective_value:
            return
        if path in adjustments:
            adjustments[path]["effective"] = effective_value
            adjustments[path]["adjustedBy"] = owner
            adjustments[path]["reasonCodes"] = sorted({*adjustments[path]["reasonCodes"], reason})
            return
        adjustments[path] = {
            "path": path,
            "requested": requested_value,
            "effective": effective_value,
            "adjustedBy": owner,
            "reasonCodes": [reason],
        }

    if render_context["output"]["static"]:
        effective["tooltip"] = "disabled"
        adjust("tooltip", requested["tooltip"], effective["tooltip"], "context", reasons["staticTooltip"])
    elif requested["tooltip"] == "disabled" and render_context["exactDataAlternative"]["kind"] == "none":
        effective["tooltip"] = "enabled"
        adjust("tooltip", requested["tooltip"], effective["tooltip"], "capability", reasons["missingExactData"])

    if effective["grid"] != "standard" and (render_context.get("forcedColors") or accessibility.get("contrast") == "high"):
        effective["grid"] = "standard"
        adjust("grid", requested["grid"], effective["grid"], "accessibility", reasons["gridStructure"])

    if "line" in effective:
        if (render_context.get("forcedColors") or accessibility.get("contrast") == "high") and effective["line"]["widthPx"] < policy["line"][
            "highContrastMinimumWidthPx"
        ]:
            effective["line"]["widthPx"] = policy["line"]["highContrastMinimumWidthPx"]
            adjust(
                "line.widthPx",
                requested["line"]["widthPx"],
                effective["line"]["widthPx"],
                "accessibility",
                reasons["lineWidthMinimum"],
            )
        point_count = max(
            (item["pointCount"] for item in shape["series"] if item["role"] in {"line", "area"}),
            default=0,
        )
        width = max(1, render_context["bounds"]["widthPx"])
        width_class = "compact" if width <= 420 else "wide" if width >= 1000 else "regular"
        show_maximum = policy["line"]["showPointMaximumByWidthClass"][width_class]
        if effective["line"]["points"] == "show" and point_count > show_maximum:
            effective["line"]["points"] = "auto"
            adjust("line.points", requested["line"]["points"], effective["line"]["points"], "correctness", reasons["pointDensity"])
        visible = effective["line"]["points"] == "show" or (
            effective["line"]["points"] == "auto" and point_count <= policy["line"]["autoPointMaximum"]
        )
        if not visible:
            effective["line"]["pointRadiusPx"] = 0

    if "bar" in effective and shape["categoryCount"] > 0:
        bars = [item for item in shape["series"] if item["role"] == "bar"]
        groups = len({item["stackKey"] or item["key"] for item in bars}) if shape["flags"]["stacked"] else len(bars)
        groups = max(1, groups)
        axis_span = render_context["bounds"]["heightPx"] if shape["kind"] == "hbar" else render_context["bounds"]["widthPx"]
        estimated = axis_span / shape["categoryCount"] / groups * policy["bar"]["reviewedOccupancy"]
        effective["bar"]["radiusPx"] = round(max(0, min(effective["bar"]["radiusPx"], estimated / 2)), 3)
        adjust(
            "bar.radiusPx",
            requested["bar"]["radiusPx"],
            effective["bar"]["radiusPx"],
            "correctness",
            reasons["barRadiusLimited"],
        )

    palette_length = max(1, len(categorical_series_palette))
    capacity = palette_length if color_only_series_capacity is None else color_only_series_capacity
    color_safe = accessibility.get("colorVision") == "color-safe" or bool(render_context.get("forcedColors"))
    encodings = assign_series_encodings(
        [item["role"] for item in shape["series"]],
        palette_length=palette_length,
        capacity=capacity,
        color_safe=color_safe,
        stacked=bool(shape["flags"]["stacked"]),
    )
    if encodings is not None:
        effective["series"] = [{**series, **encodings[index]} for index, series in enumerate(effective["series"])]
        adjust(
            "seriesEncodings",
            [f"{series['key']}:color" for series in requested["series"]],
            [_encoding_label(series) for series in effective["series"]],
            "accessibility" if color_safe else "correctness",
            reasons["nonColorRequired"] if color_safe else reasons["paletteOverflow"],
        )

    owner_rank = policy["adjusterRanks"]
    ordered_adjustments = sorted(
        adjustments.values(),
        key=lambda item: (item["path"], owner_rank[item["adjustedBy"]], item["reasonCodes"]),
    )
    return {
        "requested": requested,
        "effective": effective,
        "adjustments": ordered_adjustments,
        "renderShapeVersion": policy["renderShapeVersion"],
        "presentationPolicyVersion": policy["presentationPolicyVersion"],
    }


__all__ = [
    "assign_series_encodings",
    "derive_normalized_chart_render_shape_v1",
    "normalize_chart_for_rendering_v1",
    "resolve_chart_presentation_v2",
]
