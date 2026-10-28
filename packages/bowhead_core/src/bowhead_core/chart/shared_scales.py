"""Source-checked domains for native, observation-preserving Cartesian views."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

KINDS = frozenset({"line", "bar", "hbar", "combo", "dual_axis", "scatter", "stacked_bar", "area_stacked"})


def _fail(code: str, path: str, message: str) -> None:
    from bowhead_core.chart.presentation_plan import ChartPlanError

    raise ChartPlanError(code, "/presentation_plan/scale_groups" + path, message)


def validate_scale_input(groups: Any, *, compiled: bool = False) -> None:
    if not isinstance(groups, list) or not 1 <= len(groups) <= 3:
        _fail("plan_shape", "", "Use 1 to 3 shared scale groups")
    used: set[tuple[str, str]] = set()
    for index, group in enumerate(groups):
        fields = {"view_ids", "axis"} | ({"measure_keys", "encoding", "domain", "scope_digest"} if compiled else set())
        if not isinstance(group, dict) or set(group) != fields or group.get("axis") not in {"x", "y", "y1"}:
            _fail("plan_shape", f"/{index}", "Use a closed shared scale object with a numeric axis")
        ids = group["view_ids"]
        if not isinstance(ids, list) or not 2 <= len(ids) <= 6 or any(not isinstance(vid, str) for vid in ids) or len(set(ids)) != len(ids):
            _fail("target_invalid", f"/{index}", "Each group needs 2 to 6 distinct root-local views")
        for vid in ids:
            if (vid, group["axis"]) in used:
                _fail("target_invalid", f"/{index}", "An axis cannot belong to more than one shared group")
            used.add((vid, group["axis"]))
        if compiled:
            keys, domain = group["measure_keys"], group["domain"]
            if (
                group["encoding"] not in KINDS
                or not isinstance(keys, list)
                or not 1 <= len(keys) <= 12
                or any(not isinstance(key, str) or not key for key in keys)
                or len(set(keys)) != len(keys)
                or not isinstance(domain, list)
                or len(domain) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in domain)
                or domain[0] >= domain[1]
                or not isinstance(group["scope_digest"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", group["scope_digest"])
            ):
                _fail("plan_shape", f"/{index}", "Saved scales require exact measures, finite bounds and a scope seal")


def _axis_columns(chart: Mapping[str, Any], axis: str) -> list[dict[str, Any]]:
    from bowhead_core.chart_style_presentation import normalize_chart_for_rendering_v1

    kind = chart.get("kind")
    numeric = [column for column in chart.get("columns", []) if column.get("type") == "numeric"]
    dims = [column for column in chart.get("columns", []) if column.get("type") in {"date", "categorical", "geo"}]
    normalized, _ = normalize_chart_for_rendering_v1(chart)
    # Multi-dimension pivots require their native contributor/domain adapter.
    if (
        kind not in KINDS
        or chart.get("percent_stack")
        or (kind != "scatter" and len(dims) != 1)
        or [column["key"] for column in normalized["columns"]] != [column["key"] for column in chart.get("columns", [])]
    ):
        _fail("binding_ineligible", "", "Shared scales require a native numeric axis without an implicit pivot or percentage stack")
    if kind == "scatter":
        if len(numeric) < 2 or axis not in {"x", "y"}:
            _fail("binding_ineligible", "", "Scatter shares its exact x or y measure independently")
        return [numeric[0 if axis == "x" else 1]]
    if kind == "dual_axis":
        bars = [column for column in numeric if column.get("y_axis_id", 0) == 0]
        lines = [column for column in numeric if column.get("y_axis_id", 0) == 1]
        if not bars or not lines:
            bars, lines = numeric[:1], numeric[1:]
        result = bars if axis == "y" else lines if axis == "y1" else []
    else:
        result = numeric if axis == ("x" if kind == "hbar" else "y") else []
    if not result:
        _fail("binding_ineligible", "", "Choose an existing numeric axis of every target view")
    return result


def _number(value: Any) -> float | int | None:
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _view_evidence(chart: Mapping[str, Any], axis: str) -> tuple[list[Any], str, list[float | int]]:
    columns = _axis_columns(chart, axis)
    rows = chart.get("rows", [])
    if (
        not rows
        or chart.get("row_count") != len(rows)
        or chart.get("top_n") is not None
        or chart.get("other_bucket_label")
        or any(chart.get(f"{prefix}_shown", 0) < chart.get(f"{prefix}_total", 0) for prefix in ("series", "points"))
    ):
        _fail("binding_ineligible", "", "Share only the same complete prepared observation scope")
    semantics = []
    for column in columns:
        scale = column.get("percent_scale")
        if column.get("format") == "percent" and scale not in {"fraction", "points"}:
            _fail("unit_incompatible", "", "Shared percentage axes require an explicit fraction or points shape")
        semantics.append(
            [column["key"], column.get("unit"), column.get("format"), scale, column.get("percent_shape_unknown"), column.get("currency_code")]
        )
    if len({json.dumps([item[1], item[3], item[4], item[5]], sort_keys=True) for item in semantics}) != 1:
        _fail("unit_incompatible", "", "Every measure on a shared axis must use the same unit and percentage shape")
    dimensions = [column["key"] for column in chart["columns"] if column.get("type") != "numeric"]
    scope_cells = [[*[row.get(key) for key in dimensions], *[row.get(column["key"]) for column in columns]] for row in rows]
    ordered = sorted(json.dumps(cells, ensure_ascii=False, allow_nan=False, separators=(",", ":")) for cells in scope_cells)
    scope = hashlib.sha256(json.dumps([dimensions, ordered], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    numeric = [column for column in chart["columns"] if column.get("type") == "numeric"]
    values: list[float | int] = []
    for row in rows:
        if chart["kind"] == "scatter" and any(_number(row.get(column["key"])) is None for column in numeric[:2]):
            continue
        observed = [_number(row.get(column["key"])) for column in columns]
        # Native single-shape axes retain raw positions; only mixed percentage
        # shapes trigger the existing renderer's conversion, rejected above.
        present = [value for value in observed if value is not None]
        if present and chart["kind"] in {"stacked_bar", "area_stacked"}:
            values.extend([sum(value for value in present if value >= 0), sum(value for value in present if value < 0)])
        else:
            values.extend(present)
    if values and chart["kind"] in {"bar", "hbar", "combo", "dual_axis", "stacked_bar", "area_stacked"}:
        values.append(0)
    if not values or any(not math.isfinite(value) for value in values):
        _fail("binding_ineligible", "", "The shared axis needs finite represented observations")
    return semantics, scope, values


def _fixed_domain(kind: str, values: list[float | int]) -> list[float | int]:
    lower, upper = min(values), max(values)
    padding = (upper / 20 - lower / 20) if lower != upper else (abs(lower) / 20 or 1)
    zero = kind in {"bar", "hbar", "combo", "dual_axis", "stacked_bar", "area_stacked"}
    domain = [lower if zero and lower == 0 else lower - padding, upper if zero and upper == 0 and lower < 0 else upper + padding]
    if any(not math.isfinite(value) for value in domain) or domain[0] >= domain[1]:
        _fail("binding_ineligible", "", "The represented range cannot produce finite fixed bounds")
    return domain


def compile_scale_groups(groups: Any, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    validate_scale_input(groups)
    views = {view["id"]: view["spec"] for view in spec.get("views", [])}
    compiled = []
    for index, group in enumerate(groups):
        if not set(group["view_ids"]) <= set(views):
            _fail("target_invalid", f"/{index}", "Shared scale targets must be declared views in this root")
        charts = [views[vid] for vid in group["view_ids"]]
        evidence = [_view_evidence(chart, group["axis"]) for chart in charts]
        if len({chart["kind"] for chart in charts}) != 1 or any(item[:2] != evidence[0][:2] for item in evidence):
            _fail("unit_incompatible", f"/{index}", "Shared axes need the same exact measures, units, encoding and prepared scope")
        values = [value for item in evidence for value in item[2]]
        domain = _fixed_domain(charts[0]["kind"], values)
        compiled.append(
            {
                **group,
                "measure_keys": [item[0] for item in evidence[0][0]],
                "encoding": charts[0]["kind"],
                "domain": domain,
                "scope_digest": evidence[0][1],
            }
        )
    return compiled


def validate_saved_scales(plan: Mapping[str, Any], spec: Mapping[str, Any] | None = None) -> None:
    groups = plan["scale_groups"]
    validate_scale_input(groups, compiled=True)
    if spec is not None:
        inputs = [{key: group[key] for key in ("view_ids", "axis")} for group in groups]
        if compile_scale_groups(inputs, spec) != groups:
            _fail("binding_ineligible", "", "Saved shared scales must match canonical observations and encoding")


def domains_for_view(groups: Sequence[Mapping[str, Any]], view_id: str) -> dict[str, Any] | None:
    selected = [group for group in groups if view_id in group["view_ids"]]
    return {"encoding": selected[0]["encoding"], "domains": {group["axis"]: group["domain"] for group in selected}} if selected else None
