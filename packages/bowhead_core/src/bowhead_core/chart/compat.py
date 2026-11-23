"""Accept add_chart requests whose intent has one reading, and say what was adjusted.

A request may write a value in the wrong place or omit a value its chart kind fixes. When the
misplaced value agrees with what is declared elsewhere, it is moved or ignored; when it contradicts,
the request is left as written and the exact refusal follows. Nothing here chooses data fields,
aggregation, sorting, row limits or what a value means. Each adjustment becomes one
`presentation_fallbacks` entry `{code, path, detail}` in the Tool result.

On 116 since 2026-11-02, 115 of 416 add_chart calls were refused; replayed on synthetic data, these
rules draw 60 of the 66 refusals that did not depend on the real data or Canvas state.
"""

from __future__ import annotations

import copy
from collections.abc import Collection
from typing import Any

from bowhead_core.chart.explicit import PLOT_KINDS
from bowhead_core.chart.expression import VIEW_FIELDS, structured

#: The one x scale each plot kind draws; KPI has no x.
X_SCALE = {kind: ("linear" if kind == "scatter" else "category") for kind in PLOT_KINDS if kind != "kpi"}
_JSON_FIELDS = ("columns", "axes", "group_by", "column_units", "column_types", "views")
_ROOT_SELECTION = ("axes", "force_kind", "group_by", "columns")

Receipts = list[dict[str, str]]


def receipt(receipts: Receipts, code: str, path: str, detail: str) -> None:
    entry = {"code": code, "path": path or "/", "detail": detail}
    if entry not in receipts:
        receipts.append(entry)


def _read_json_text(request: dict[str, Any], base: str, receipts: Receipts) -> None:
    for key in _JSON_FIELDS:
        value = request.get(key)
        if not isinstance(value, str) or value.strip()[:1] not in ("[", "{"):
            continue
        try:
            request[key] = structured(value, key)
        except ValueError:
            continue
        receipt(receipts, "json_text_read", f"{base}/{key}", f"{key} was sent as JSON text; read it as JSON.")


def _accept_one(request: dict[str, Any], base: str, receipts: Receipts) -> None:
    """Rules for one chart request: a single chart or one composed view."""
    axes = request.get("axes")
    if isinstance(axes, dict) and "group_by" in axes and request.get("group_by", axes["group_by"]) == axes["group_by"]:
        request["group_by"] = axes.pop("group_by")
        receipt(receipts, "group_by_moved", f"{base}/axes/group_by", "group_by belongs beside axes; moved it there.")
    kind = request.get("force_kind")
    if kind == "table":
        for key in ("axes", "group_by"):
            if key in request:
                del request[key]
                receipt(receipts, "table_plot_field_ignored", f"{base}/{key}", f"A Table shows its columns; ignored {key}.")
        return
    if not isinstance(axes, dict) or not isinstance(kind, str) or kind not in PLOT_KINDS:
        return
    _read_axis_field_shapes(axes, base, receipts)
    groups = request.get("group_by")
    # x is a single field, but a list or any other value must reach validation instead of failing here.
    x = axes.get("x")
    bound = {*([x] if isinstance(x, str) else _strings(x)), *_strings(axes.get("y")), *_strings(axes.get("y_right")), *_strings(groups)}
    columns = request.get("columns")
    if isinstance(columns, list) and columns and len(_strings(columns)) == len(columns) and set(columns) <= bound:
        del request["columns"]
        receipt(receipts, "plot_columns_ignored", f"{base}/columns", "A plot shows its axes and group_by fields; ignored columns.")
    if kind == "combo" and axes.get("y_right"):
        request["force_kind"] = kind = "dual_axis"
        receipt(
            receipts,
            "combo_drawn_as_dual_axis",
            f"{base}/force_kind",
            "combo draws one value axis; drew dual_axis so the y_right fields get the right-hand axis.",
        )
    if kind in X_SCALE and "x" in axes and axes.get("x_scale") != X_SCALE[kind]:
        given = axes.get("x_scale")
        axes["x_scale"] = X_SCALE[kind]
        receipt(
            receipts,
            "x_scale_set",
            f"{base}/axes/x_scale",
            f"{kind} draws a {X_SCALE[kind]} x axis" + (f", not {given}." if given else "; set x_scale to it."),
        )


def _read_axis_field_shapes(axes: dict[str, Any], base: str, receipts: Receipts) -> None:
    """Read one field key written in the other container: `x` as a one-key list, `y` or `y_right` as text.

    An observed request an hbar sent x=["销售额_亿元"] twice and x and y as text in between.
    Any other shape is left for the existing refusal.
    """
    x = axes.get("x")
    if isinstance(x, list) and len(x) == 1 and isinstance(x[0], str) and x[0]:
        axes["x"] = x[0]
        receipt(receipts, "axis_field_shape_read", f"{base}/axes/x", "x takes one field key; read the one listed key.")
    for key in ("y", "y_right"):
        value = axes.get(key)
        if isinstance(value, str) and value.strip() and value.strip()[0] not in "[{":
            axes[key] = [value]
            receipt(receipts, "axis_field_shape_read", f"{base}/axes/{key}", f"{key} takes a list of field keys; read the one key as a list.")


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _lift_view_fields(request: dict[str, Any], view: dict[str, Any], base: str, shared_fields: Collection[str], receipts: Receipts) -> None:
    """Move top-level fields written inside a view to the top level when nothing conflicts."""
    for key in [key for key in view if key not in VIEW_FIELDS and key in shared_fields]:
        value, current = view[key], request.get(key)
        if isinstance(value, dict) and isinstance(current, dict | None):
            merged = dict(current or {})
            if any(name in merged and merged[name] != item for name, item in value.items()):
                continue
            request[key] = {**merged, **value}
        elif key not in request or current == value:
            request[key] = value
        else:
            continue
        del view[key]
        receipt(receipts, "view_field_lifted", f"{base}/{key}", f"{key} applies to every view; moved it beside views.")


def accept_request(args: dict[str, Any], *, shared_fields: Collection[str], receipts: Receipts) -> dict[str, Any]:
    """Return a copy of `args` with one-reading mistakes adjusted; record each adjustment."""
    request = copy.deepcopy(args)
    _read_json_text(request, "", receipts)
    views = request.get("views")
    if not isinstance(views, list):
        _accept_one(request, "", receipts)
        return request
    for key in _ROOT_SELECTION:
        if key in request and any(isinstance(view, dict) and view.get(key) == request[key] for view in views):
            del request[key]
            receipt(receipts, "root_selection_ignored", f"/{key}", f"Each view declares its own {key}; ignored the copy beside views.")
    for index, view in enumerate(views):
        if not isinstance(view, dict):
            continue
        base = f"/views/{index}"
        _read_json_text(view, base, receipts)
        _lift_view_fields(request, view, base, shared_fields, receipts)
        _accept_one(view, base, receipts)
    return request
