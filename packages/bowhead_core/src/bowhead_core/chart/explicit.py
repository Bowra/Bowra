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
#: Kinds whose y fields with exactly two declared units the add_chart Tool draws on a dual axis.
UNIT_SPLIT_KINDS = frozenset({"combo", "bar", "line", "hbar"})


def _declared_unit(column: ChartColumn) -> str | None:
    """Compare declared quantities without names, ranges or automatic placement."""
    magnitude = _currency_magnitude(column.unit)
    return (
        currency_identity(column.currency_code)
        or (magnitude[0] if magnitude else None)
        or ("%" if column.percent_scale or column.format == "percent" else column.unit)
    )


class ExplicitPresentationError(ValueError):
    def __init__(self, message: str, path: str = "/axes", *, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.path = path
        #: Requested fields absent from the data, so the caller can say which data it read.
        self.missing = missing or []


class _Problems:
    """Every independent problem of one request, raised together.

    A model fixes what an error names and resends. One rule per reply cost the 116 helper
    the regression case (the regression case) three calls on one combo chart, and the third failure in
    a row disabled `add_chart` for it. A single problem keeps its original message and path.
    """

    def __init__(self) -> None:
        self.found: list[tuple[str, str]] = []

    def add(self, message: str, path: str = "/axes") -> None:
        self.found.append((message, path))

    def take(self, error: ExplicitPresentationError) -> None:
        self.add(str(error), error.path)

    def error(self) -> ExplicitPresentationError:
        if len(self.found) == 1:
            return ExplicitPresentationError(*self.found[0])
        listed = " ".join(f"{index}. {message}" for index, (message, _path) in enumerate(self.found, 1))
        return ExplicitPresentationError(f"Fix all {len(self.found)} problems before resending: {listed}", self.found[0][1])

    def raise_found(self) -> None:
        if self.found:
            raise self.error()


def _y_right_refusal(kind: str) -> str:
    if kind == "combo":
        return (
            "combo draws every y field on one value axis (the first as bars, the rest as lines) and does not accept y_right. "
            "For a right-hand axis use force_kind dual_axis and name those fields in y_right."
        )
    return "Only dual_axis accepts y_right."


def input_properties() -> dict[str, Any]:
    return {
        "columns": {
            "description": "Table only. Omit or use all for every original column in source order; otherwise name exact keys in display order. Narrow only for a user-requested subset; otherwise use all. Constants remain. Missing or duplicate keys fail before saving.",
            "anyOf": [{"type": "string", "enum": ["all"]}, {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string"}}],
        },
        "axes": {
            "type": "object",
            "additionalProperties": False,
            "description": "Plots only. Bind exact x and y fields, never infer them. Category scales have equal spacing. Scatter uses linear x and exactly one y. Combo uses the first declared y as bars and remaining y fields as lines on the same axis. KPI uses exactly one y only. Heatmap/sankey use x and one group_by field as the second dimension. Dual axis names its right-axis fields in y_right; they are plotted even when omitted from y, and at least one y field stays on the left axis. Prepare aggregation and Top N upstream.",
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


def completed_axes(args: dict[str, Any]) -> dict[str, Any]:
    """A dual_axis request with every y_right field also in axes.y (Regression check M1).

    A field named only in y_right is still explicitly named, so it is plotted on the right axis
    rather than refused. Returns a copy when it adds fields; the caller's arguments (the model's
    recorded tool call) stay unchanged, and the saved binding records the completed axes.
    """
    axes = args.get("axes")
    if args.get("force_kind") != "dual_axis" or not isinstance(axes, dict):
        return args
    y, right = axes.get("y"), axes.get("y_right")
    if not isinstance(y, list) or not isinstance(right, list):
        return args
    missing = [key for key in right if isinstance(key, str) and key and key not in y]
    return {**args, "axes": {**axes, "y": [*y, *missing]}} if missing else args


def validate_request(args: dict[str, Any], *, analytical: bool = False) -> None:
    """Refuse a request that explicit presentation cannot draw, naming every independent problem.

    Problems that later checks depend on (an unsupported kind, a missing axes object, unreadable
    field lists) still end the check where they are found.
    """
    args = completed_axes(args)
    problems = _Problems()
    for key in ("series", "hint", "limit", "sort_by"):
        if key in args:
            problems.add(
                f"{key} is not supported by explicit presentation. Use Table columns or plot axes/group_by; prepare sorting, ranking and sampling upstream.",
                f"/{key}",
            )
    kind = args.get("force_kind", "table")
    if not isinstance(kind, str) or kind not in {"table", *PLOT_KINDS}:
        problems.add("Choose a supported force_kind or omit it for a Table.", "/force_kind")
        raise problems.error()
    if analytical:
        if any(key in args for key in ("columns", "axes", "group_by")):
            problems.add("Analytical views already declare their exact fields; omit ordinary columns/axes/group_by.")
        problems.raise_found()
        return
    if kind == "table":
        if "axes" in args or "group_by" in args:
            problems.add("Tables use columns, not axes or group_by.")
        selection = args.get("columns", "all")
        if selection != "all":
            try:
                _keys(selection, "/columns")
            except ExplicitPresentationError as exc:
                problems.take(exc)
        problems.raise_found()
        return
    if "columns" in args:
        problems.add("Plots use axes and group_by, not Table columns.", "/columns")
    axes = args.get("axes")
    if not isinstance(axes, dict) or set(axes) - {"x", "y", "x_scale", "y_right"}:
        problems.add("Declare axes with exact x, y and x_scale keys; KPI uses y only.")
        raise problems.error()
    try:
        y = _keys(axes.get("y"), "/axes/y")
    except ExplicitPresentationError as exc:
        problems.take(exc)
        raise problems.error() from None
    if len(y) > 12:
        problems.add("At most twelve y fields fit a native plot; prepare a smaller view or use a Table.", "/axes/y")
    try:
        groups = _keys(args.get("group_by"), "/group_by", empty=True)
    except ExplicitPresentationError as exc:
        problems.take(exc)
        raise problems.error() from None
    if len(groups) > 1:
        problems.add("At most one explicit group is supported; prepare a composite grouping field upstream.", "/group_by")
    if kind == "kpi":
        if set(axes) != {"y"} or groups or len(y) != 1:
            problems.add("KPI uses exactly one axes.y field and group_by: [] only.")
        problems.raise_found()
        return
    has_x = isinstance(axes.get("x"), str) and bool(axes["x"])
    if not has_x:
        problems.add("Declare the exact x field.", "/axes/x")
    required_scale = "linear" if kind == "scatter" else "category"
    if axes.get("x_scale") != required_scale:
        problems.add(
            f"{kind} requires x_scale={required_scale!r}. Continuous time/numeric Line axes use the existing analytical profile.", "/axes/x_scale"
        )
    if groups and (kind not in GROUP_KINDS or len(y) != 1):
        problems.add(
            "This native shape cannot represent these groups and y fields without aggregation; prepare the intended result or use a Table.",
            "/group_by",
        )
    if kind in {"heatmap", "sankey"} and (len(groups) != 1 or len(y) != 1):
        problems.add(f"{kind} requires one group_by field and exactly one y field.", "/group_by")
    if kind in {"scatter", "pie", "waterfall"} and len(y) != 1:
        problems.add(f"{kind} requires exactly one y field.", "/axes/y")
    if kind in {"dual_axis", "combo"} and len(y) < 2:
        problems.add(f"{kind} requires at least two explicitly named y fields.", "/axes/y")
    if kind == "dual_axis":
        try:
            right = _keys(axes.get("y_right"), "/axes/y_right")
        except ExplicitPresentationError as exc:
            problems.take(exc)
        else:
            if not set(right) < set(y):
                problems.add("y_right names the axes.y fields drawn on the right axis; keep at least one y field on the left axis.", "/axes/y_right")
    elif "y_right" in axes:
        problems.add(_y_right_refusal(kind), "/axes/y_right")
    if has_x:
        try:
            _keys([axes["x"], *groups, *y], "/axes")
        except ExplicitPresentationError as exc:
            problems.take(exc)
    problems.raise_found()


def _split_two_units(kind: str, axes: dict[str, Any], selected: list[ChartColumn], fallbacks: list[dict[str, str]]) -> str:
    """Draw y fields with exactly two declared units as dual_axis: the first y field's unit on the left.

    116 refusals were mostly an amount and a rate written as one combo ("bars for revenue, a line for
    year-on-year"); the dual-axis renderer draws the left axis as bars and the right as a line.
    """
    from bowhead_core.chart.compat import receipt

    declared = [(column, _declared_unit(column)) for column in selected if column.key in axes["y"]]
    units = list(dict.fromkeys(unit for _column, unit in declared if unit))
    if len(units) != 2:
        return kind
    right = [column.key for column, unit in declared if unit == units[1]]
    shown = [next(column.unit or unit for column, declared_unit in declared if declared_unit == unit) for unit in units]
    axes["y_right"] = right
    for column in selected:
        if column.key in axes["y"]:
            column.y_axis_id = int(column.key in right)
    detail = f"{kind} fields carry two units ({shown[0]}, {shown[1]}); drew dual_axis with {right} on the right axis."
    if kind == "hbar":
        detail += " A dual axis is vertical."
    receipt(fallbacks, "two_units_drawn_as_dual_axis", "/force_kind", detail)
    return "dual_axis"


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
    fallbacks: list[dict[str, str]] | None = None,
) -> ChartSpec:
    """Compile one view without field discovery, column pruning or data reduction.

    `fallbacks` is passed by the add_chart Tool only: a plot whose y fields carry two declared
    units is then drawn on a dual axis and the change is recorded there. Strict callers pass nothing.
    """
    args = completed_axes(args)
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
            f"Requested fields are absent: {missing}. Available fields: {list(by_key)}.",
            "/columns" if kind == "table" else "/axes",
            missing=missing,
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
        if fallbacks is not None and kind in UNIT_SPLIT_KINDS and not axes.get("y_right"):
            kind = _split_two_units(kind, axes, selected, fallbacks)
        for axis_id in (0, 1):
            units = {
                _declared_unit(column) for column in selected if column.key in axes["y"] and column.y_axis_id == axis_id and _declared_unit(column)
            }
            if len(units) > 1:
                raise ExplicitPresentationError(
                    "Fields with different declared units cannot share a value axis. Use force_kind dual_axis with every plotted field in "
                    "axes.y and the right-axis fields also in axes.y_right, or use separate views.",
                    "/axes/y",
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
