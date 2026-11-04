"""Explicit native presentation: parsing never chooses displayed fields."""

from __future__ import annotations

import copy
import csv
import io
import json
import math
import re
from typing import Any

from bowhead_core.chart.expression import _currency_magnitude
from bowhead_core.chart.numeric_display import currency_identity
from bowhead_core.chart.types import ChartColumn, ChartSpec

PLOT_KINDS = frozenset(
    {"kpi", "line", "bar", "hbar", "pie", "stacked_bar", "dual_axis", "combo", "scatter", "area_stacked", "waterfall", "sankey", "heatmap"}
)
GROUP_KINDS = frozenset({"line", "bar", "hbar", "stacked_bar", "area_stacked", "heatmap", "sankey", "scatter"})


def _declared_unit(column: ChartColumn) -> str | None:
    """Compare declared quantities without names, ranges or automatic placement."""
    magnitude = _currency_magnitude(column.unit)
    return (
        currency_identity(column.currency_code)
        or (magnitude[0] if magnitude else None)
        or ("%" if column.percent_scale or column.format == "percent" else column.unit)
    )


class ExplicitPresentationError(ValueError):
    def __init__(self, message: str, path: str = "/axes") -> None:
        super().__init__(message)
        self.path = path


def input_properties() -> dict[str, Any]:
    return {
        "columns": {
            "description": "Table only. Omit or use all for every original column in source order; otherwise name exact keys in display order. Narrow only for a user-requested subset; otherwise use all. Constants remain. Missing or duplicate keys fail before saving.",
            "anyOf": [{"type": "string", "enum": ["all"]}, {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string"}}],
        },
        "axes": {
            "type": "object",
            "additionalProperties": False,
            "description": "Plots only. Bind exact x and y fields, never infer them. Category scales have equal spacing. Scatter uses linear x and exactly one y. Combo uses the first declared y as bars and remaining y fields as lines on the same axis. KPI uses exactly one y only. Heatmap/sankey use x and one group_by field as the second dimension. Dual axis explicitly names y_right. Prepare aggregation and Top N upstream.",
            "required": ["y"],
            "properties": {
                "x": {"type": "string"},
                "y": {"type": "array", "minItems": 1, "maxItems": 12, "uniqueItems": True, "items": {"type": "string"}},
                "x_scale": {"type": "string", "enum": ["category", "linear"]},
                "y_right": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string"}},
            },
        },
        "group_by": {
            "type": "array",
            "maxItems": 1,
            "uniqueItems": True,
            "items": {"type": "string"},
            "description": "Required on ordinary plots. Exact grouping field, or [] explicitly for no split. At most one grouping field; prepare a composite group upstream when needed. No group is inferred.",
        },
    }


def validate_request(args: dict[str, Any], *, analytical: bool = False) -> None:
    for key in ("series", "hint", "limit", "sort_by"):
        if key in args:
            raise ExplicitPresentationError(
                f"{key} is not supported by explicit presentation. Use Table columns or plot axes/group_by; prepare sorting, ranking and sampling upstream.",
                f"/{key}",
            )
    kind = args.get("force_kind", "table")
    if not isinstance(kind, str) or kind not in {"table", *PLOT_KINDS}:
        raise ExplicitPresentationError("Choose a supported force_kind or omit it for a Table.", "/force_kind")
    if analytical:
        if any(key in args for key in ("columns", "axes", "group_by")):
            raise ExplicitPresentationError("Analytical views already declare their exact fields; omit ordinary columns/axes/group_by.")
        return
    if kind == "table":
        if "axes" in args or "group_by" in args:
            raise ExplicitPresentationError("Tables use columns, not axes or group_by.")
        selection = args.get("columns", "all")
        if selection != "all":
            _keys(selection, "/columns")
        return
    if "columns" in args:
        raise ExplicitPresentationError("Plots use axes and group_by, not Table columns.", "/columns")
    axes = args.get("axes")
    if not isinstance(axes, dict) or set(axes) - {"x", "y", "x_scale", "y_right"}:
        raise ExplicitPresentationError("Declare axes with exact x, y and x_scale keys; KPI uses y only.")
    y = _keys(axes.get("y"), "/axes/y")
    if len(y) > 12:
        raise ExplicitPresentationError("At most twelve y fields fit a native plot; prepare a smaller view or use a Table.", "/axes/y")
    groups = _keys(args.get("group_by"), "/group_by", empty=True)
    if len(groups) > 1:
        raise ExplicitPresentationError("At most one explicit group is supported; prepare a composite grouping field upstream.", "/group_by")
    if kind == "kpi":
        if set(axes) != {"y"} or groups or len(y) != 1:
            raise ExplicitPresentationError("KPI uses exactly one axes.y field and group_by: [] only.")
        return
    if not isinstance(axes.get("x"), str) or not axes["x"]:
        raise ExplicitPresentationError("Declare the exact x field.", "/axes/x")
    required_scale = "linear" if kind == "scatter" else "category"
    if axes.get("x_scale") != required_scale:
        raise ExplicitPresentationError(
            f"{kind} requires x_scale={required_scale!r}. Continuous time/numeric Line axes use the existing analytical profile.", "/axes/x_scale"
        )
    if groups and (kind not in GROUP_KINDS or len(y) != 1):
        raise ExplicitPresentationError(
            "This native shape cannot represent these groups and y fields without aggregation; prepare the intended result or use a Table.",
            "/group_by",
        )
    if kind in {"heatmap", "sankey"} and (len(groups) != 1 or len(y) != 1):
        raise ExplicitPresentationError(f"{kind} requires one group_by field and exactly one y field.", "/group_by")
    if kind in {"scatter", "pie", "waterfall"} and len(y) != 1:
        raise ExplicitPresentationError(f"{kind} requires exactly one y field.", "/axes/y")
    if kind in {"dual_axis", "combo"} and len(y) < 2:
        raise ExplicitPresentationError(f"{kind} requires at least two explicitly named y fields.", "/axes/y")
    if kind == "dual_axis":
        right = _keys(axes.get("y_right"), "/axes/y_right")
        if not set(right) < set(y):
            raise ExplicitPresentationError("y_right must be a nonempty proper subset of axes.y.", "/axes/y_right")
    elif "y_right" in axes:
        raise ExplicitPresentationError("Only dual_axis accepts y_right.", "/axes/y_right")
    _keys([axes["x"], *groups, *y], "/axes")


def _keys(value: Any, path: str, *, empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not empty) or any(not isinstance(key, str) or not key for key in value):
        raise ExplicitPresentationError("Provide an explicit list of exact field keys (use [] only for no grouping).", path)
    if len(value) != len(set(value)):
        raise ExplicitPresentationError("Field keys must not be repeated.", path)
    return value


def compile_view(
    args: dict[str, Any],
    rows: list[dict[str, Any]],
    columns: list[ChartColumn],
    *,
    artifact_id: str,
    raw: bytes,
    analytical: dict[str, Any] | None = None,
    full_table: bool = False,
    observation_keys: list[str] | None = None,
) -> ChartSpec:
    """Compile one view without field discovery, column pruning or data reduction."""
    validate_request(args, analytical=analytical is not None)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExplicitPresentationError("Prepare a UTF-8 CSV before displaying its exact fields.", "/columns") from exc
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)) or any(not key for key in headers):
        raise ExplicitPresentationError("CSV headers must be nonempty and unique; prepare unambiguous headers upstream.", "/columns")
    original = list(reader)
    if any(None in row for row in original):
        raise ExplicitPresentationError("CSV rows contain values beyond the declared headers; prepare a rectangular result upstream.", "/columns")
    kind = args.get("force_kind", "table")
    by_key = {column.key: column for column in columns}
    if analytical:
        from bowhead_core.chart.analytical import analytical_columns

        keys = [column.key for column in columns if column.key in analytical_columns(analytical)]
        contract: dict[str, Any] = {"version": 1, "mode": "analytical", "columns": keys}
    elif kind == "table":
        keys = list(by_key) if args.get("columns", "all") == "all" else list(args["columns"])
        contract = {"version": 1, "mode": "table", "columns": keys}
    else:
        axes, groups = args["axes"], args["group_by"]
        keys = ([axes["x"]] if "x" in axes else []) + list(groups) + axes["y"]
        contract = {"version": 1, "mode": "plot", "axes": copy.deepcopy(axes), "group_by": list(groups)}
    if contract["mode"] == "plot":
        keys += [key for key in observation_keys or [] if key not in keys]
    missing = [key for key in keys if key not in by_key]
    if missing:
        raise ExplicitPresentationError(
            f"Requested fields are absent: {missing}. Available fields: {list(by_key)}.", "/columns" if kind == "table" else "/axes"
        )
    selected = [copy.deepcopy(by_key[key]) for key in keys]
    values = [{key: row.get(key) for key in keys} for row in rows]
    if contract["mode"] == "plot":
        axes, groups = contract["axes"], contract["group_by"]
        categorical = ([axes["x"]] if axes.get("x_scale") == "category" else []) + groups
        numeric = axes["y"] + ([axes["x"]] if axes.get("x_scale") == "linear" else [])
        for column in selected:
            if column.key in categorical:
                column.type = "date" if column.type == "date" and kind not in {"pie", "waterfall", "sankey"} else "categorical"
                column.role = "x" if column.key == axes.get("x") else "color"
                if column.type == "date" or column.key in (observation_keys or []):
                    continue
                for index, row in enumerate(values):
                    row[column.key] = (
                        original[index].get(column.key)
                        if index < len(original) and column.key in headers
                        else str(row[column.key])
                        if row[column.key] is not None
                        else None
                    )
            if column.key in numeric:
                column.type = "numeric"
                column.role = "x" if column.key == axes.get("x") else "y"
                column.y_axis_id = int(column.key in axes.get("y_right", []))
                for row in values:
                    value = row[column.key]
                    if value is not None and (
                        isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > 2**53 - 1 or not math.isfinite(value)
                    ):
                        raise ExplicitPresentationError(
                            f"Field {column.key!r} requires finite, safely representable numeric values; prepare numeric data upstream or use a Table.",
                            "/axes",
                        )
        for key in axes["y"]:
            if not any(row[key] is not None for row in values):
                raise ExplicitPresentationError(f"Field {key!r} has no usable observations; use a Table to show missing data.", "/axes/y")
        for axis_id in (0, 1):
            units = {
                _declared_unit(column) for column in selected if column.key in axes["y"] and column.y_axis_id == axis_id and _declared_unit(column)
            }
            if len(units) > 1:
                raise ExplicitPresentationError(
                    "Fields with different declared units cannot share a value axis; declare dual-axis placement or use separate views.", "/axes/y"
                )
        if kind == "dual_axis":
            units = {_declared_unit(column) for column in selected if column.key in axes["y"]}
            if len(units) == 1 and None not in units:
                raise ExplicitPresentationError(
                    "Fields with the same declared unit must share one axis; use combo or separate views.", "/axes/y_right"
                )
        if kind == "kpi" and len(values) != 1:
            raise ExplicitPresentationError("KPI requires one prepared row; aggregate upstream or use a Table.", "/axes/y")
        if kind in {"line", "area_stacked"} and len(values) < 2:
            raise ExplicitPresentationError("A line requires at least two prepared observations; use a Table for a scalar.", "/axes/x")
        if kind != "scatter" and kind != "kpi":
            seen: set[tuple[Any, ...]] = set()
            for row in values:
                identity = tuple(row[key] or "" for key in [axes["x"], *groups])
                if identity in seen:
                    raise ExplicitPresentationError(
                        "Repeated axis/group cells require explicit upstream preparation; the renderer does not sum them.", "/group_by"
                    )
                seen.add(identity)
        if len(values) > 50_000 or len(json.dumps(values, ensure_ascii=False).encode()) > 1_000_000:
            raise ExplicitPresentationError(
                "The complete plot exceeds native row/byte limits; prepare a smaller result explicitly or use a complete Table."
            )
        if kind in {"pie", "sankey"} and any(row[axes["y"][0]] is not None and row[axes["y"][0]] < 0 for row in values):
            raise ExplicitPresentationError("This plot cannot represent negative values; use a Table or another explicit kind.", "/axes/y")
    elif kind == "table":
        for column in selected:
            if column.key in headers and any(re.fullmatch(r"[+-]?0\d+(?:\.\d+)?", (row.get(column.key) or "").strip()) for row in original):
                column.type = "categorical"
                for index, row in enumerate(values):
                    if index < len(original):
                        row[column.key] = original[index].get(column.key)
    spec = ChartSpec(
        kind=kind,
        title=str(args.get("title") or "Data"),
        subtitle=args.get("subtitle"),
        rows=values if kind != "table" or full_table else values[:50],
        columns=selected,
        row_count=len(values),
        data_artifact_id=artifact_id,
        explicit_presentation=contract,
    )
    if kind != "table" and not analytical:
        from dataclasses import replace

        from bowhead_core.chart.validation import _undrawable_reason

        bound = {*args["axes"].get("y", []), *args["group_by"], *([args["axes"]["x"]] if "x" in args["axes"] else [])}
        reason = _undrawable_reason(replace(spec, columns=[column for column in spec.columns if column.key in bound]))
        if reason:
            raise ExplicitPresentationError(
                f"The complete requested plot cannot be rendered: {reason}. Prepare a smaller result explicitly or use a Table."
            )
    return spec
