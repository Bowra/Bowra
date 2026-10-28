"""Bounded native-chart composition and row arithmetic; never execute model code."""

from __future__ import annotations

import ast
import json
import math
import operator
import re
from decimal import Decimal, DecimalException, localcontext
from typing import Any

from bowhead_router.tool_args import loads_tool_arguments

from bowhead_core.chart.types import VALID_HINTS, ChartColumn

MAX_VIEWS = 6
KINDS = {
    "kpi",
    "line",
    "bar",
    "hbar",
    "pie",
    "stacked_bar",
    "dual_axis",
    "combo",
    "scatter",
    "area_stacked",
    "waterfall",
    "sankey",
    "heatmap",
    "table",
}
VIEW_FIELDS = {"id", "title", "subtitle", "hint", "force_kind", "series", "sort_by", "limit"}
_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


class ChartExpressionError(ValueError):
    """A native composed chart cannot be written without its complete valid root."""


def schema_properties() -> dict[str, Any]:
    """Optional fields on the existing add_chart tool, using its native vocabulary."""
    return {
        "views": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_VIEWS,
            "description": "Optional 1-6 native views of this SAME dataset; one view becomes an ordinary chart. Every intended plot, including the primary, MUST be in views. Root force_kind does NOT add another plot. Use precomputed columns. Each view needs id, title and explicit numeric series. An omitted hint defaults to raw; an explicit force_kind is preserved. No HTML, CSS or code. Omit views for a single chart when possible.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "title", "series"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"},
                    "title": {"type": "string"},
                    "subtitle": {"type": "string"},
                    "hint": {"type": "string", "enum": sorted(VALID_HINTS), "default": "raw"},
                    "force_kind": {"type": "string", "enum": sorted(KINDS)},
                    "series": {"type": "array", "minItems": 1, "maxItems": 12, "items": {"type": "string"}},
                    "sort_by": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50000},
                },
            },
        },
        "layout": {
            "description": "Optional views layout. Multiple views default to grid2: equal-width columns for equally important analyses, including different chart kinds such as a trend and a scatter plot. Use stack for vertical reading order. Use split or primary_aside ONLY for an explicit primary chart with a supporting detail: exactly two children, first twice as wide as second (2:1). Different chart kinds alone do not imply a primary/detail relationship. To put two peer charts above a full-width Table, use stack containing a grid2 group and the Table leaf. A tree has at most 3 levels. Leaf: {view: id}. Group: {type: stack|split|grid2, children: [...]}; primary_aside is a split alias. A tree may be one JSON-encoded object layer. Reference every view exactly once, in reading order. An invalid bounded layout falls back to stack with every validated view in declaration order; the tool reports presentation_fallbacks. Narrow screens and document exports follow the saved order.",
            "anyOf": [
                {"type": "string", "enum": ["stack", "grid2", "split", "primary_aside"]},
                {"type": "object"},
                {"type": "string", "pattern": r"^\s*\{", "maxLength": 20000},
            ],
        },
        "derived_columns": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "description": "Optional simple row equations only when the needed value is not already computed. Finite literals, bound column names, + - * / and parentheses; no functions or cross-row calculations. inputs maps short identifiers to exact existing numeric column keys. Derived keys and input aliases must not contain the reserved provenance word source. Declare output unit (empty for dimensionless); unit % requires percent_scale fraction or points. Missing operands/zero division become null, never zero. Original columns stay intact.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "header", "expression", "unit"],
                "properties": {
                    "key": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"},
                    "header": {"type": "string"},
                    "expression": {"type": "string", "maxLength": 200},
                    "inputs": {"type": "object", "additionalProperties": {"type": "string"}},
                    "unit": {"type": "string", "maxLength": 30},
                    "percent_scale": {"type": "string", "enum": ["fraction", "points"]},
                },
            },
        },
    }


def structured(value: Any, field: str) -> Any:
    """Decode one bounded JSON layer, optionally in one complete JSON fence."""
    if isinstance(value, str):
        if len(value.encode()) > 20_000:
            raise ValueError(f"{field} exceeds the 20 KB limit")
        fence = re.fullmatch(r"\s*```(?:json)?[ \t]*\r?\n(.*?)\r?\n```\s*", value, re.DOTALL)
        if fence:
            value = fence[1]
        try:
            return loads_tool_arguments(value)
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"{field} must contain valid JSON") from exc
    return value


def composition(args: dict[str, Any], *, fallbacks: list[dict[str, str]] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate all views; authoring may recover presentation without losing a view.

    Passing a fallback receipt opts the existing tool into deterministic hint and
    layout recovery. Stored edits and strict callers keep their original guards.
    """
    views = structured(args.get("views"), "views")
    if not isinstance(views, list) or not 1 <= len(views) <= MAX_VIEWS:
        raise ValueError("views must contain 1 to 6 native chart views")
    ids: list[str] = []
    normalized: list[dict[str, Any]] = []
    for index, view in enumerate(views):
        if not isinstance(view, dict) or set(view) - VIEW_FIELDS:
            raise ValueError("each view must use only the declared view fields")
        vid = view.get("id")
        if not isinstance(vid, str) or not _ID.fullmatch(vid) or vid in ids:
            raise ValueError("view ids must be unique lowercase identifiers")
        ids.append(vid)
        if not isinstance(view.get("title"), str) or not view["title"].strip():
            raise ValueError(f"view {vid}: title is required")
        if "hint" not in view and fallbacks is not None:
            view = {**view, "hint": "raw"}
            fallbacks.append({"code": "hint_defaulted", "path": f"/views/{index}/hint"})
        if not isinstance(view.get("hint"), str) or view["hint"] not in VALID_HINTS:
            raise ValueError(f"view {vid}: hint must be one of {sorted(VALID_HINTS)}; heatmap is a force_kind, not a hint")
        if view.get("force_kind") is not None and (not isinstance(view["force_kind"], str) or view["force_kind"] not in KINDS):
            raise ValueError(f"view {vid}: unsupported force_kind")
        if not isinstance(view.get("series"), list) or not view["series"] or any(not isinstance(key, str) for key in view["series"]):
            raise ValueError(f"view {vid}: series must explicitly name the numeric columns to show")
        if len(view["series"]) > 12 or len(set(view["series"])) != len(view["series"]):
            raise ValueError(f"view {vid}: series contains duplicate columns")
        if "limit" in view and (type(view["limit"]) is not int or not 1 <= view["limit"] <= 50_000):
            raise ValueError(f"view {vid}: limit must be a positive integer at most 50000")
        normalized.append(view)
    views = normalized
    layout = args.get("layout", "grid2" if len(ids) > 1 else "stack")
    if args.get("force_kind") and not any(view.get("force_kind") == args["force_kind"] for view in views):
        raise ValueError("views omits the primary force_kind: put every plot inside views; root parameters do not add a plot")
    # Resource/nonfinite limits remain errors even when geometry can degrade.
    if len((layout if isinstance(layout, str) else json.dumps(layout, allow_nan=False)).encode()) > 20_000:
        raise ValueError("layout exceeds the 20 KB limit")
    try:
        if isinstance(layout, str) and layout in ("stack", "grid2", "split", "primary_aside"):
            layout = (
                {"view": ids[0]}
                if len(ids) == 1 and layout == "stack"
                else {"type": {"primary_aside": "split"}.get(layout, layout), "children": [{"view": vid} for vid in ids]}
            )
        else:
            layout = structured(layout, "layout")
        layout = normalize_layout_aliases(layout)
        seen = validate_layout(layout, ids)
    except (ValueError, TypeError):
        if fallbacks is None:
            raise
        layout = {"view": ids[0]} if len(ids) == 1 else {"type": "stack", "children": [{"view": vid} for vid in ids]}
        seen = ids
        fallbacks.append({"code": "layout_defaulted", "path": "/layout"})
    return sorted(views, key=lambda view: seen.index(view["id"])), layout


def normalize_layout_aliases(node: Any, depth: int = 1) -> Any:
    """An existing primary/aside name has exactly the native split geometry."""
    if depth > 3:
        raise ValueError("layout must be an object with at most 3 levels")
    if (
        not isinstance(node, dict)
        or set(node) != {"type", "children"}
        or node["type"] not in ("stack", "split", "grid2", "primary_aside")
        or not isinstance(node["children"], list)
        or len(node["children"]) > MAX_VIEWS
    ):
        return node
    return {
        "type": "split" if node["type"] == "primary_aside" else node["type"],
        "children": [normalize_layout_aliases(child, depth + 1) for child in node["children"]],
    }


def validate_layout(layout: Any, ids: list[str]) -> list[str]:
    """Validate the one native layout grammar used by authoring and persisted edits."""
    seen: list[str] = []

    def check(node: Any, depth: int) -> None:
        if not isinstance(node, dict) or depth > 3:
            raise ValueError("layout must be an object with at most 3 levels")
        if set(node) == {"view"}:
            if node["view"] not in ids or node["view"] in seen:
                raise ValueError("layout must reference each declared view exactly once")
            seen.append(node["view"])
            return
        if set(node) != {"type", "children"} or node["type"] not in ("stack", "split", "grid2"):
            raise ValueError("layout nodes support only stack, split, grid2 or a view reference")
        children = node["children"]
        if not isinstance(children, list) or not 2 <= len(children) <= MAX_VIEWS or (node["type"] == "split" and len(children) != 2):
            raise ValueError("layout groups need 2 to 6 children; split needs exactly 2")
        for child in children:
            check(child, depth + 1)

    check(layout, 1)
    if set(seen) != set(ids):
        raise ValueError("layout must include every declared view")
    return seen


def validate_stored_composition(spec: Any) -> None:
    """Protect native composition envelopes at every Canvas write/restore ingress.

    Legacy single-chart inference and native Table degradation remain unchanged.
    Literal evidence plans also validate single-chart observations. Other advanced
    plan fields remain unsupported until their consumers are implemented.
    """
    if not isinstance(spec, dict):
        return
    if set(spec) & {
        "_export_axis_domains",
        "_export_frame_entities",
        "_export_frame_scope",
        "_export_annotations",
        "_export_table_trend",
        "_export_analytical",
    }:
        raise ChartExpressionError("Export-only scale fields cannot be persisted in Canvas")
    if "views" not in spec and "layout" not in spec and "presentation_plan" not in spec:
        return
    try:
        if len(json.dumps(spec, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 1_000_000:
            raise ValueError("the complete chart exceeds the 1 MB limit")
        if "views" not in spec and "layout" not in spec:
            _check_observations(spec, "root")
            _validate_stored_plan(spec)
            return
        views = spec.get("views")
        minimum = 1 if spec.get("presentation_plan", {}).get("analytical_views") else 2
        if not isinstance(views, list) or not minimum <= len(views) <= MAX_VIEWS:
            raise ValueError("a stored composition requires 2 to 6 views")

        _check_observations(spec, "root")
        ids: list[str] = []
        for view in views:
            if not isinstance(view, dict) or set(view) != {"id", "kind", "spec"} or view["kind"] != "chart":
                raise ValueError("stored views require only id, kind chart and spec")
            vid = view["id"]
            if not isinstance(vid, str) or not _ID.fullmatch(vid) or vid in ids:
                raise ValueError("view ids must be unique lowercase identifiers")
            ids.append(vid)
            child = view["spec"]
            if not isinstance(child, dict) or any(
                key in child
                for key in (
                    "views",
                    "layout",
                    "source_meta",
                    "presentation_plan",
                    "_export_axis_domains",
                    "_export_frame_entities",
                    "_export_frame_scope",
                    "_export_annotations",
                    "_export_table_trend",
                    "_export_analytical",
                )
            ):
                raise ValueError(f"view {vid}: nested composition or independent provenance is not supported")
            if child.get("data_artifact_id") != spec.get("data_artifact_id") or not isinstance(spec.get("data_artifact_id"), str):
                raise ValueError(f"view {vid}: the root dataset must be preserved")
            _check_observations(child, f"view {vid}")
        validate_layout(spec.get("layout"), ids)
        _validate_stored_plan(spec)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ChartExpressionError(str(exc)) from exc


def _check_observations(chart: dict[str, Any], label: str) -> None:
    if chart.get("kind") not in KINDS or not isinstance(chart.get("title"), str):
        raise ValueError(f"{label}: kind and title must be valid")
    columns = chart.get("columns")
    if not isinstance(columns, list) or not columns:
        raise ValueError(f"{label}: columns are required")
    keys: set[str] = set()
    for column in columns:
        if not isinstance(column, dict) or not isinstance(column.get("key"), str) or column["key"] in keys:
            raise ValueError(f"{label}: column keys must be unique")
        keys.add(column["key"])
        if column.get("type") not in {"numeric", "categorical", "date", "geo"} or not isinstance(column.get("header"), str):
            raise ValueError(f"{label}: column type and header must be valid")
    rows = chart.get("rows", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) or set(row) - keys for row in rows):
        raise ValueError(f"{label}: observations must use the declared columns")
    if any(cell is not None and type(cell) not in (str, int, float) for row in rows for cell in row.values()):
        raise ValueError(f"{label}: observations must be string, finite number or null")
    count = chart.get("row_count")
    if type(count) is not int or count < len(rows):
        raise ValueError(f"{label}: row_count must cover the retained observations")
    for field in ("show_legend", "show_data_labels", "percent_stack"):
        if field in chart and type(chart[field]) is not bool:
            raise ValueError(f"{label}: {field} must be boolean")


def _validate_stored_plan(spec: dict[str, Any]) -> None:
    if "presentation_plan" not in spec:
        return
    from bowhead_core.chart.presentation_plan import ChartPlanError, validate_stored_notes_plan

    try:
        validate_stored_notes_plan(spec)
    except (ChartPlanError, TypeError, KeyError, AttributeError) as exc:
        raise ChartExpressionError(str(exc)) from exc


def derive_columns(
    value: Any, rows: list[dict[str, Any]], columns: list[ChartColumn]
) -> tuple[list[dict[str, Any]], list[ChartColumn], list[dict[str, Any]], dict[str, int]]:
    definitions = structured(value, "derived_columns")
    if not isinstance(definitions, list) or not 1 <= len(definitions) <= 3:
        raise ValueError("derived_columns must contain 1 to 3 equations")
    if len(rows) > 50_000:
        raise ValueError("row arithmetic supports at most 50000 rows; compute larger results upstream")
    numeric = {column.key: column for column in columns if column.type == "numeric"}
    existing = {column.key for column in columns}
    compiled: list[tuple[dict[str, Any], ast.Expression, dict[str, str]]] = []
    for definition in definitions:
        if not isinstance(definition, dict) or set(definition) - {"key", "header", "expression", "inputs", "unit", "percent_scale"}:
            raise ValueError("derived columns must use the declared equation fields")
        key, expression = definition.get("key"), definition.get("expression")
        if not isinstance(key, str) or not _ID.fullmatch(key) or key in existing:
            raise ValueError("derived column keys must be new, unique lowercase identifiers")
        if "source" in key:
            raise ValueError("derived column keys must not contain the reserved provenance word source")
        existing.add(key)
        if not isinstance(definition.get("header"), str) or not definition["header"].strip():
            raise ValueError(f"{key}: header is required")
        unit = definition.get("unit")
        if not isinstance(unit, str) or len(unit) > 30:
            raise ValueError(f"{key}: declare unit explicitly; use an empty string for dimensionless values")
        scale = definition.get("percent_scale")
        if (unit == "%" and scale not in ("fraction", "points")) or (unit != "%" and scale is not None):
            raise ValueError(f"{key}: percent_scale is required only for unit %")
        if not isinstance(expression, str) or not 1 <= len(expression) <= 200:
            raise ValueError(f"{key}: expression must contain at most 200 characters")
        try:
            tree = ast.parse(expression, mode="eval")
        except (SyntaxError, RecursionError) as exc:
            raise ValueError(f"{key}: invalid arithmetic expression") from exc
        nodes = list(ast.walk(tree))
        referenced_names = {node.id for node in nodes if isinstance(node, ast.Name)}
        inputs = definition.get("inputs", {name: name for name in referenced_names if name in numeric})
        if (
            not isinstance(inputs, dict)
            or len(inputs) > 12
            or any(
                not isinstance(alias, str) or not alias.isidentifier() or not isinstance(target, str) or target not in numeric
                for alias, target in inputs.items()
            )
        ):
            raise ValueError(f"{key}: inputs must bind identifiers to existing numeric column keys")
        if any("source" in alias.lower() for alias in inputs):
            raise ValueError(f"{key}: input aliases must not contain the reserved provenance word source; use an explicit short binding")
        if len(nodes) > 64 or any(
            not isinstance(
                node,
                (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load, ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UAdd, ast.USub),
            )
            for node in nodes
        ):
            raise ValueError(f"{key}: only column names, finite numbers, + - * / and parentheses are supported")
        for node in nodes:
            if isinstance(node, ast.Name) and node.id not in inputs:
                raise ValueError(f"{key}: unknown input {node.id!r}; bind it in inputs")
            if isinstance(node, ast.Constant) and (type(node.value) not in (int, float) or not math.isfinite(node.value)):
                raise ValueError(f"{key}: constants must be finite numbers")
        if not any(isinstance(node, ast.Name) for node in nodes):
            raise ValueError(f"{key}: equation must reference at least one existing column")

        def quantity(node: ast.AST, bindings: dict[str, str] = inputs, label: str = key) -> tuple[str, str | None] | None:
            if isinstance(node, ast.Expression):
                return quantity(node.body)
            if isinstance(node, ast.Constant):
                return ("", None)
            if isinstance(node, ast.Name):
                column = numeric[bindings[node.id]]
                return (column.unit, column.percent_scale) if column.unit else None
            if isinstance(node, ast.UnaryOp):
                return quantity(node.operand)
            if isinstance(node, ast.BinOp):
                left, right = quantity(node.left), quantity(node.right)
                if isinstance(node.op, (ast.Add, ast.Sub)):
                    if left and right and left[0] and right[0] and left != right:
                        raise ValueError(f"{label}: addition/subtraction requires compatible units and percentage scales")
                    if left == right or right == ("", None):
                        return left
                    if left == ("", None):
                        return right
                    return None
                if isinstance(node.op, ast.Mult):
                    if left == ("", None):
                        return right
                    if right == ("", None):
                        return left
                if isinstance(node.op, ast.Div) and right == ("", None):
                    return left
                if isinstance(node.op, ast.Div) and left and left == right:
                    return ("", None)
            return None

        quantity(tree)
        compiled.append((definition, tree, inputs))

    def evaluate(node: ast.AST, row: dict[str, Any], inputs: dict[str, str]) -> Decimal:
        if isinstance(node, ast.Expression):
            return evaluate(node.body, row, inputs)
        if isinstance(node, ast.Constant):
            return Decimal(str(node.value))
        if isinstance(node, ast.Name):
            cell = row.get(inputs[node.id])
            if type(cell) not in (int, float) or not math.isfinite(cell):
                raise ValueError("missing or non-finite operand")
            return Decimal(str(cell))
        if isinstance(node, ast.UnaryOp):
            operand = evaluate(node.operand, row, inputs)
            return -operand if isinstance(node.op, ast.USub) else operand
        if isinstance(node, ast.BinOp):
            return _OPS[type(node.op)](evaluate(node.left, row, inputs), evaluate(node.right, row, inputs))
        raise ValueError("unsupported arithmetic node")

    output = [dict(row) for row in rows]
    missing: dict[str, int] = {}
    metadata: list[dict[str, Any]] = []
    result_columns = list(columns)
    for definition, tree, inputs in compiled:
        key = definition["key"]
        for row in output:
            try:
                # Arithmetic uses represented decimal operands. Convert only the
                # new result to the existing JSON-number contract; never round sources.
                with localcontext() as arithmetic:
                    arithmetic.prec = 34
                    result = float(evaluate(tree, row, inputs))
                if not math.isfinite(result):
                    raise ValueError("non-finite result")
                row[key] = result
            except (ValueError, ZeroDivisionError, OverflowError, DecimalException):
                row[key] = None
                missing[key] = missing.get(key, 0) + 1
        result_columns.append(
            ChartColumn(
                key=key,
                header=definition["header"],
                type="numeric",
                unit=definition["unit"] or None,
                format="percent" if definition["unit"] == "%" else None,
                percent_scale=definition.get("percent_scale"),
                percent_shape_unknown=True if definition["unit"] != "%" else None,
            )
        )
        metadata.append({**definition, "inputs": inputs})
    return output, result_columns, metadata, missing
