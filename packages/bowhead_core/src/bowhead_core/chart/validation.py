"""Native chart capacity checks; validation never selects or reduces content."""

from __future__ import annotations

import json
import math
from typing import Any

from bowhead_core.chart.types import (
    ChartColumn,
    ChartSpec,
    spec_to_dict,
)

_MAX_LINE_POINTS = 3000

_LINE_KINDS = frozenset({"line", "area_stacked", "dual_axis", "combo"})

_MAX_BAR_SLOTS = 1000

_TIME_AXIS_KINDS = frozenset({"line", "area_stacked", "bar", "stacked_bar", "dual_axis", "combo", "waterfall"})

_MAX_DRAWN_SERIES = 100

_MAX_DRAWN_SHAPE_BYTES = 30_000

_MAX_STACK_CELLS = _MAX_DRAWN_SERIES * (_MAX_DRAWN_SHAPE_BYTES // 7)

_MAX_HEATMAP_Y = 40


def _col_attr(col: Any, name: str) -> Any:
    """Read a column attribute from a dataclass or persisted dictionary."""
    if isinstance(col, dict):
        return col.get(name)
    return getattr(col, name, None)


def indistinguishable_rows_reason(rows: list[dict[str, Any]], columns: list[Any]) -> str | None:
    """Detect multiple rows whose categorical labels are all constant.

    Purely numeric scatter data remains valid. This is a narrow validation guard
    after column projection, not a replacement for preserving distinguishing columns.
    """
    if len(rows) < 2:
        return None
    label_keys = [k for c in columns if (k := _col_attr(c, "key")) and _col_attr(c, "type") != "numeric"]
    if not label_keys:
        return None
    constants: list[str] = []
    for key in label_keys:
        seen = {str(r.get(key)) for r in rows}
        if len(seen) > 1:
            return None  # Rows are distinguishable if any column differentiates them.
        constants.append(f"{key}={next(iter(seen))!r}")
    return f"All labels are constant across {len(rows)} rows ({', '.join(constants[:3])})"


_DIM_TYPES = ("date", "categorical", "geo")


def _not_a_flow_reason(rows: list[dict[str, Any]], columns: list[ChartColumn]) -> str | None:
    """Return why disconnected Sankey rows cannot represent a flow, or None.

    Independent stage-to-label bands compare widths without connecting stages and may mix incompatible units.
    Reject only when no node is shared by two positive links. Do not require flow conservation: entries outside
    the supplied data can legitimately make an intermediate node emit more than it receives. Connected mixed-unit
    flows remain undetected. Mirror frontend notAFlowReason with the same dimensions and positive-flow filter.
    """
    dims = [c for c in columns if c.type in ("categorical", "date")]
    flow_col = next((c for c in columns if c.type == "numeric"), None)
    if len(dims) < 2 or flow_col is None:
        return None
    degree: dict[str, int] = {}
    links = 0
    for r in rows:
        src, dst, flow = r.get(dims[0].key), r.get(dims[1].key), r.get(flow_col.key)
        if src in (None, "") or dst in (None, ""):
            continue
        if isinstance(flow, bool) or not isinstance(flow, (int, float)) or not flow > 0:
            continue
        links += 1
        for node in (str(src), str(dst)):
            degree[node] = degree.get(node, 0) + 1
    if links and all(d == 1 for d in degree.values()):
        return f"Sankey links share no node ({links} independent bands) — not a flow, widths would compare unrelated rows"
    return None


def _undrawable_before_normalizing(spec: ChartSpec) -> str | None:
    """Reject excessive shapes before materializing a category/group matrix."""
    if spec.kind == "heatmap":
        # Match frontend `build-config.ts` `pickAxes`: it recognizes categorical/date, **not geo**.
        # Counting `_DIM_TYPES` measures the wrong column when geo is present, as review found.
        dims = [c for c in spec.columns if c.type in ("categorical", "date")]
        if len(dims) >= 2:
            y_values = len({str(r.get(dims[1].key)) for r in spec.rows or []})
            if y_values > _MAX_HEATMAP_Y:
                return f"Heatmap has {y_values} row categories; maximum: {_MAX_HEATMAP_Y}"

    if spec.kind == "sankey":
        not_a_flow = _not_a_flow_reason(spec.rows or [], spec.columns)
        if not_a_flow is not None:
            return not_a_flow

    if spec.kind == "stacked_bar":
        dims = [c for c in spec.columns if c.type in _DIM_TYPES][:2]
        if len(dims) == 2:
            cells = math.prod(len({str(r.get(c.key)) for r in spec.rows or []}) for c in dims)
            if cells > _MAX_STACK_CELLS:
                return f"Stacked category/group cells: {cells}; maximum: {_MAX_STACK_CELLS}"
    return None


def _undrawable_reason(spec: ChartSpec, shape: dict[str, Any] | None = None) -> str | None:
    """Validate complete native capacity without selecting, aggregating or removing content."""
    cheap = _undrawable_before_normalizing(spec)
    if cheap is not None:
        return cheap

    if shape is None:
        from bowhead_core.chart_style_presentation import normalize_chart_for_rendering_v1

        _, shape = normalize_chart_for_rendering_v1(spec_to_dict(spec))
    n_series = int(shape.get("seriesCount") or 0)
    if n_series > _MAX_DRAWN_SERIES:
        return f"Rendered series: {n_series}; maximum: {_MAX_DRAWN_SERIES}"
    if spec.kind in _TIME_AXIS_KINDS:
        # Actual horizontal-axis slot count, **not** the time-point count in the data: long tables have one slot per time only when pivotable
        # (one numeric column + two dimensions + sufficient fill); otherwise each row gets a slot.
        # Measurement: 12 months × 100 brands × **two measures** cannot pivot, producing 1200 slots with each month repeated 100 times.
        slots = int(shape.get("categoryCount") or 0)
        # Validate the complete supplied result against the native line/bar limits.
        slots_cap = _MAX_LINE_POINTS if spec.kind in _LINE_KINDS else _MAX_BAR_SLOTS
        if slots > slots_cap:
            return f"Rendered axis slots: {slots}; maximum: {slots_cap}"
    shape_bytes = len(json.dumps(shape, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
    if shape_bytes > _MAX_DRAWN_SHAPE_BYTES:
        return f"Rendered structure: {shape_bytes} bytes; maximum: {_MAX_DRAWN_SHAPE_BYTES}"
    return None
