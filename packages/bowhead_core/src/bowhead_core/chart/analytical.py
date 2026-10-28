"""Bounded analytical bindings for native interval Tables and series plots."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date
from typing import Any

MAX_ROWS = 1200
ROLES = {"observed", "reference", "primary", "current", "optimistic", "baseline", "pessimistic"}
SCENARIOS = {"optimistic", "baseline", "pessimistic"}
COMMON = {"view_id", "preset", "binding_id", "range"}
FIELDS = {
    "interval_rows": COMMON | {"row_keys", "group_key", "points", "detail_keys"},
    "series_plot": COMMON | {"x_key", "x_kind", "value_key", "series_key", "facet", "series_roles", "summary_keys"},
}


def _fail(path: str, message: str, code: str = "binding_ineligible") -> None:
    from bowhead_core.chart.presentation_plan import ChartPlanError

    raise ChartPlanError(code, "/presentation_plan/analytical_views" + path, message)


def _keys(value: Any, path: str, maximum: int, minimum: int = 1) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum or any(not isinstance(v, str) or not v for v in value):
        _fail(path, f"Use {minimum} to {maximum} exact column keys")
    if len(set(value)) != len(value):
        _fail(path, "Keys must be distinct")
    return value


def _typed(value: Any) -> str:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        _fail("", "Observation keys must be non-null strings or finite numbers")
    if isinstance(value, (int, float)) and (not math.isfinite(value) or (float(value).is_integer() and abs(value) > 2**53 - 1)):
        _fail("", "Observation keys must be finite and integers must be safe")
    return json.dumps(
        ["s" if isinstance(value, str) else "n", int(value) if isinstance(value, float) and value.is_integer() else value], ensure_ascii=False
    )


def _number(value: Any, path: str) -> float | int | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        _fail(path, "Use a finite numeric value or null; prepare numeric values upstream")
    return value


def validate_analytical_input(items: Any) -> None:
    if not isinstance(items, list) or not 1 <= len(items) <= 6:
        _fail("", "Use one to six analytical view declarations", "plan_shape")
    seen = set()
    for index, item in enumerate(items):
        path = f"/{index}"
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("preset"), str)
            or item.get("preset") not in FIELDS
            or set(item) - FIELDS[item["preset"]]
        ):
            _fail(path, "Use a closed interval_rows or series_plot declaration", "plan_shape")
        vid = item.get("view_id")
        if not isinstance(vid, str) or not vid or vid in seen:
            _fail(path + "/view_id", "Target each declared view at most once")
        seen.add(vid)
        if "binding_id" in item and (not isinstance(item["binding_id"], str) or not item["binding_id"]):
            _fail(path + "/binding_id", "Use the offered binding ID or omit it")
        if item["preset"] == "interval_rows":
            _keys(item.get("row_keys"), path + "/row_keys", 6)
            if "group_key" in item and (not isinstance(item["group_key"], str) or not item["group_key"]):
                _fail(path + "/group_key", "Use an exact grouping key")
            _keys(item.get("detail_keys", []), path + "/detail_keys", 6, 0)
            points = item.get("points", [])
            if not isinstance(points, list) or len(points) > 3:
                _fail(path + "/points", "Use at most three independent point bindings")
            point_keys, point_roles = set(), set()
            for point in points:
                if (
                    not isinstance(point, dict)
                    or set(point) != {"key", "role"}
                    or not isinstance(point.get("role"), str)
                    or point.get("role") not in {"primary", "current", "reference"}
                ):
                    _fail(path + "/points", "Points require an exact key and primary, current or reference role")
                if not isinstance(point["key"], str) or not point["key"] or point["key"] in point_keys or point["role"] in point_roles:
                    _fail(path + "/points", "Point keys and roles must be distinct")
                point_keys.add(point["key"])
                point_roles.add(point["role"])
            if "range" not in item:
                _fail(path + "/range", "An interval Table requires supplied lower and upper columns")
        else:
            for key in ("x_key", "value_key", "series_key"):
                if not isinstance(item.get(key), str) or not item[key]:
                    _fail(path + "/" + key, "Use an exact retained column key")
            if not isinstance(item.get("x_kind"), str) or item.get("x_kind") not in {"numeric", "date"}:
                _fail(path + "/x_kind", "Declare numeric elapsed positions or date positions")
            _keys(item.get("summary_keys", []), path + "/summary_keys", 2, 0)
            facet = item.get("facet")
            if facet is not None:
                if not isinstance(facet, dict) or set(facet) - {"key", "order"} or not isinstance(facet.get("key"), str) or not facet["key"]:
                    _fail(path + "/facet", "Use an exact facet key and optional complete order")
                if "order" in facet:
                    if not isinstance(facet["order"], list) or not 1 <= len(facet["order"]) <= 6:
                        _fail(path + "/facet/order", "Use one to six actual facet values")
                    if len({_typed(v) for v in facet["order"]}) != len(facet["order"]):
                        _fail(path + "/facet/order", "Facet values must be distinct")
            roles = item.get("series_roles", [])
            if not isinstance(roles, list) or len(roles) > 12:
                _fail(path + "/series_roles", "Use at most twelve series roles")
            role_keys = set()
            for role in roles:
                if (
                    not isinstance(role, dict)
                    or set(role) - {"series_value", "role", "note_ids"}
                    or not isinstance(role.get("role"), str)
                    or role.get("role") not in ROLES
                ):
                    _fail(path + "/series_roles", "Use an exact series_value and a supported role")
                key = _typed(role.get("series_value"))
                if key in role_keys:
                    _fail(path + "/series_roles", "Declare each series role once")
                role_keys.add(key)
                if "note_ids" in role:
                    _keys(role["note_ids"], path + "/series_roles/note_ids", 3)
                if role["role"] in SCENARIOS and not role.get("note_ids"):
                    _fail(path + "/series_roles/note_ids", "Scenario roles require applicable prepared evidence notes")
        bounds = item.get("range")
        if bounds is not None:
            if not isinstance(bounds, dict) or set(bounds) - {"lower_key", "upper_key", "kind", "note_ids", "origin_key", "coverage_key"}:
                _fail(path + "/range", "Use a closed supplied range binding")
            for key in ("lower_key", "upper_key"):
                if not isinstance(bounds.get(key), str) or not bounds[key]:
                    _fail(path + "/range/" + key, "Bind an exact numeric endpoint column")
            if (
                bounds["lower_key"] == bounds["upper_key"]
                or not isinstance(bounds.get("kind"), str)
                or bounds.get("kind") not in {"scenario", "estimate", "confidence"}
            ):
                _fail(path + "/range", "Use distinct endpoints and declare scenario, estimate or confidence")
            _keys(bounds.get("note_ids"), path + "/range/note_ids", 3)
            if (bounds["kind"] == "confidence") != ("coverage_key" in bounds):
                _fail(path + "/range/coverage_key", "Only confidence intervals require a supplied coverage fraction column")
            for key in ("origin_key", "coverage_key"):
                if key in bounds and (not isinstance(bounds[key], str) or not bounds[key]):
                    _fail(path + "/range/" + key, "Use an exact supplied column key")


def analytical_columns(item: Mapping[str, Any]) -> set[str]:
    keys = set(item.get("row_keys", [])) | set(item.get("detail_keys", [])) | set(item.get("summary_keys", []))
    keys.update(item[key] for key in ("group_key", "x_key", "value_key", "series_key") if key in item)
    keys.update(point["key"] for point in item.get("points", []))
    keys.update(value for key, value in item.get("range", {}).items() if key.endswith("_key"))
    if "facet" in item:
        keys.add(item["facet"]["key"])
    return keys


def _domain(values: list[float | int]) -> list[float | int]:
    if not values:
        _fail("", "At least one finite supplied mark is required")
    low, high = min(values), max(values)
    padding = (high - low) * 0.05 if high != low else abs(low) * 0.05 or 1
    domain = [low - padding, high + padding]
    if not all(math.isfinite(value) for value in domain):
        _fail("", "The supplied domain exceeds finite rendering limits")
    return domain


def _note_refs(ids: list[str], keys: set[str], notes: list[dict[str, Any]]) -> None:
    available = {note["id"]: note for note in notes}
    if any(nid not in available or not keys <= set(available[nid]["measure_keys"]) for nid in ids):
        _fail("", "Range and scenario notes must reference existing same-root notes applicable to every bound measure")


def _compile_one(item: dict[str, Any], chart: Mapping[str, Any], notes: list[dict[str, Any]]) -> dict[str, Any]:
    expected = "table" if item["preset"] == "interval_rows" else "line"
    if chart.get("kind") != expected:
        _fail("", f"{item['preset']} requires a native {expected} view")
    rows = chart.get("rows", [])
    if not rows or len(rows) > MAX_ROWS or chart.get("row_count") != len(rows) or chart.get("top_n"):
        _fail("", "Analytical views require 1 to 1200 complete prepared rows; no sampled or ranked truncation")
    columns = {column["key"]: column for column in chart["columns"]}
    missing = analytical_columns(item) - columns.keys()
    if missing:
        _fail("", f"Retain the referenced columns before drawing: {', '.join(sorted(missing))}")
    bounds = item.get("range", {})
    measures = {bounds[key] for key in ("lower_key", "upper_key") if key in bounds}
    measures.update(point["key"] for point in item.get("points", []))
    if "value_key" in item:
        measures.add(item["value_key"])
    if sum(column["type"] == "numeric" for column in columns.values()) > 12:
        _fail("", "Retain at most twelve numeric measures per analytical view")
    if any(columns[key]["type"] != "numeric" for key in measures):
        _fail("", "Every bound and point must reference a numeric column")
    semantics = {
        (columns[key].get("unit"), columns[key].get("percent_scale"), columns[key].get("percent_shape_unknown"), columns[key].get("currency_code"))
        for key in measures
    }
    if len(semantics) != 1 or any(
        columns[key].get("format") == "percent" and columns[key].get("percent_scale") not in {"fraction", "points"} for key in measures
    ):
        _fail("", "Co-plotted bounds and points need the same actual unit and explicit percentage shape", "unit_incompatible")
    _metric_basis(measures, notes, columns)
    if bounds:
        _note_refs(bounds["note_ids"], {bounds["lower_key"], bounds["upper_key"]}, notes)
    for role in item.get("series_roles", []):
        if role.get("note_ids"):
            _note_refs(role["note_ids"], {item["value_key"]}, notes)
    identity = item.get("row_keys") or [*([item["facet"]["key"]] if "facet" in item else []), item["series_key"], item["x_key"]]
    seen, values, positions = set(), [], []
    groups, series = {}, {}
    summary_values: dict[str, dict[str, set[str]]] = {}
    date_lengths = set()
    for row in rows:
        key = tuple(_typed(row.get(key)) for key in identity)
        if key in seen:
            _fail("", "Original analytical observation keys must be unique; prepare duplicates upstream")
        seen.add(key)
        for measure in measures:
            value = _number(row.get(measure), "/" + measure)
            if value is not None:
                values.append(value)
        if bounds:
            low, high = row.get(bounds["lower_key"]), row.get(bounds["upper_key"])
            if low is not None and high is not None and low > high:
                _fail("/range", "A lower bound exceeds its upper bound; correct the prepared data")
            if "coverage_key" in bounds:
                coverage = _number(row.get(bounds["coverage_key"]), "/range/coverage_key")
                if coverage is None or not 0 < coverage < 1:
                    _fail("/range/coverage_key", "Confidence coverage must be a supplied fraction strictly between zero and one")
            if "origin_key" in bounds and row.get(bounds["origin_key"]) not in {"reported", "estimated", None}:
                _fail("/range/origin_key", "Origin classification must be reported, estimated or null; keep other text in details")
        group_key = item.get("group_key") or item.get("facet", {}).get("key")
        if group_key:
            group_value = row.get(group_key)
            groups.setdefault(_typed(group_value), group_value)
        if item["preset"] == "series_plot":
            series_value = row[item["series_key"]]
            series.setdefault(_typed(series_value), series_value)
            x = row[item["x_key"]]
            if item["x_kind"] == "numeric":
                position = _number(x, "/x_key")
            else:
                try:
                    if not isinstance(x, str) or len(x) not in {4, 7, 10}:
                        raise ValueError
                    date_lengths.add(len(x))
                    position = (
                        date.fromisoformat(x + ("-01-01" if len(x) == 4 else "-01" if len(x) == 7 else "")).toordinal() * 86400000
                        - date(1970, 1, 1).toordinal() * 86400000
                    )
                except ValueError:
                    _fail("/x_key", "Date x needs consistent ISO years, months or days")
            if position is None:
                _fail("/x_key", "Every supplied series row needs an exact x position")
            positions.append(position)
            group = json.dumps([*key[:-1]])
            for field in item.get("summary_keys", []):
                if row.get(field) is not None:
                    summary_values.setdefault(group, {}).setdefault(field, set()).add(_typed(row[field]))
    if len(date_lengths) > 1:
        _fail("/x_key", "Date x must use one consistent granularity")
    if any(len(values) > 1 for group in summary_values.values() for values in group.values()):
        _fail("/summary_keys", "Summary fields must be constant within each series and facet")
    if len(series) > 12 or ("facet" in item and len(groups) > 6):
        _fail("", "Use at most twelve series and six facet groups")
    role_keys = {_typed(role["series_value"]) for role in item.get("series_roles", [])}
    if role_keys and role_keys != set(series):
        _fail("/series_roles", "A declared role map must match every retained series exactly once")
    facet_order = item.get("facet", {}).get("order")
    if facet_order is not None and {_typed(value) for value in facet_order} != set(groups):
        _fail("/facet/order", "Facet order must include every supplied group exactly once")
    binding = {"observation_keys": identity, "measure_keys": sorted(measures), "value_domain": _domain(values)}
    if positions:
        binding.update(x_domain=_domain(positions), series_values=list(series.values()))
    if group_key:
        binding["group_values"] = facet_order if facet_order is not None else list(groups.values())
    bid = "analytical_" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:12]
    if "binding_id" in item and item["binding_id"] != bid:
        _fail("/binding_id", "The binding ID does not match these exact keys; omit it to use the unique declared grain")
    return {**copy.deepcopy(item), "binding_id": bid, "binding": binding}


def compile_analytical_views(items: Any, spec: Mapping[str, Any], notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validate_analytical_input(items)
    views = {view["id"]: view["spec"] for view in spec.get("views", [])}
    result = []
    for index, item in enumerate(items):
        if item["view_id"] not in views:
            _fail(f"/{index}/view_id", "Declare the target in views, including a single analytical view")
        result.append(_compile_one(item, views[item["view_id"]], notes))
    panels = len(views) + sum(len(item["binding"]["group_values"]) - 1 for item in result if "facet" in item)
    if panels > 6:
        _fail("", "The complete root may contain at most six panels after facet expansion")
    return result


def validate_saved_analytical(plan: Mapping[str, Any], spec: Mapping[str, Any] | None = None, *, projected: bool = False) -> None:
    items = plan.get("analytical_views")
    if items is None:
        return
    if plan.get("version") != 3 or not isinstance(items, list) or not 1 <= len(items) <= 6:
        _fail("", "Analytical views require the complete version 3 envelope")
    inputs = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("view_id"), str) or item["view_id"] in seen:
            _fail("", "Analytical view targets must remain distinct")
        seen.add(item["view_id"])
        if projected and item.get("availability") == "unavailable":
            if set(item) != {"view_id", "availability"}:
                _fail("", "Unavailable analytical views cannot retain protected descriptors")
            continue
        if projected and item.get("availability") != "available":
            _fail("", "Projected analytical views require current availability")
        descriptor = item.get("binding")
        if not isinstance(descriptor, dict) or set(descriptor) - {
            "observation_keys",
            "measure_keys",
            "value_domain",
            "x_domain",
            "series_values",
            "group_values",
        }:
            _fail("", "Preserve the closed compiled analytical binding")
        _keys(descriptor.get("observation_keys"), "/binding/observation_keys", 6)
        _keys(descriptor.get("measure_keys"), "/binding/measure_keys", 12)
        for axis in ("value_domain", *(["x_domain"] if item.get("preset") == "series_plot" else [])):
            domain = descriptor.get(axis)
            if (
                not isinstance(domain, list)
                or len(domain) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in domain)
                or domain[0] >= domain[1]
            ):
                _fail("/binding/" + axis, "Preserve a finite increasing compiled domain")
        inputs.append({key: value for key, value in item.items() if key not in {"binding", "availability"}})
    if inputs:
        validate_analytical_input(inputs)
    targets = {item["view_id"] for item in items}
    for field in ("annotations", "table_trends", "scale_groups"):
        if any(targets.intersection(item.get("view_ids", [item.get("view_id")])) for item in plan.get(field, [])):
            _fail("", f"Use separate views for analytical profiles and {field}")
    if any(item.get("preset") != "guided_steps" for item in plan.get("interactions", [])):
        _fail("", "Analytical roots currently support whole-view guides; omit incompatible focus, window or player declarations")
    if spec is not None and compile_analytical_views(inputs, spec, plan.get("evidence_notes", [])) != items:
        _fail("", "Saved analytical bindings must match the complete canonical observations")


def materialize_analytical(spec: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(spec)
    items = {
        item["view_id"]: item for item in spec.get("presentation_plan", {}).get("analytical_views", []) if item.get("availability") == "available"
    }
    unavailable = {
        item["view_id"] for item in spec.get("presentation_plan", {}).get("analytical_views", []) if item.get("availability") == "unavailable"
    }
    result["views"] = [
        {**view, "spec": {**view["spec"], "_export_analytical": items[view["id"]]}}
        if view["id"] in items
        else {**view, "spec": {**view["spec"], "kind": "table", "rows": [], "row_count": 0}}
        if view["id"] in unavailable
        else view
        for view in spec.get("views", [])
    ]
    return result


def static_analytical_panels(spec: Mapping[str, Any], *, locale: str) -> list[dict[str, Any]]:
    """Partition complete observations into readable static panels, retaining union domains."""
    item = spec.get("_export_analytical")
    if not isinstance(item, dict) or spec.get("views"):
        return []
    rows = spec.get("rows", [])
    parts: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
    if item.get("facet"):
        key = item["facet"]["key"]
        for value in item["binding"]["group_values"]:
            profile = {k: v for k, v in item.items() if k != "facet"}
            parts.append((str(value), [row for row in rows if _typed(row[key]) == _typed(value)], profile))
    elif item.get("preset") == "interval_rows":
        key = item.get("group_key")
        ordered = [row for value in item["binding"]["group_values"] for row in rows if _typed(row[key]) == _typed(value)] if key else rows
        template = {
            "zh": "第 {start}–{end} 行，共 {total} 行",
            "de": "Zeilen {start}–{end} von {total}",
            "fr": "Lignes {start}–{end} sur {total}",
        }.get(locale[:2].lower(), "Rows {start}–{end} of {total}")
        # The fixed 900 px frame also contains title, legend, axis and padding.
        # Group headings consume a row slot; never hide the final observation.
        chunks: list[list[dict[str, Any]]] = []
        chunk: list[dict[str, Any]] = []
        slots = 0
        for row in ordered:
            new_group = bool(key) and (not chunk or _typed(chunk[-1][key]) != _typed(row[key]))
            cost = 1 + int(new_group)
            if chunk and slots + cost > 10:
                chunks.append(chunk)
                chunk, slots = [], 0
                cost = 1 + int(bool(key))
            chunk.append(row)
            slots += cost
        if chunk:
            chunks.append(chunk)
        if len(chunks) < 2:
            return []
        start = 0
        for chunk in chunks:
            parts.append((template.format(start=start + 1, end=start + len(chunk), total=len(rows)), chunk, item))
            start += len(chunk)
    return [
        {
            "id": f"analytical-panel-{index}",
            "kind": "chart",
            "spec": {
                **spec,
                "title": " · ".join(filter(None, [str(spec.get("title") or ""), label])),
                "rows": panel_rows,
                "row_count": len(panel_rows),
                "_export_analytical": profile,
            },
        }
        for index, (label, panel_rows, profile) in enumerate(parts)
    ]


def analytical_schema() -> dict[str, Any]:
    """Expose field bindings, never executable rendering instructions or invented data."""
    key = {"type": "string", "minLength": 1}

    def keys(maximum: int, minimum: int = 0) -> dict[str, Any]:
        return {"type": "array", "minItems": minimum, "maxItems": maximum, "uniqueItems": True, "items": key}

    def obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": False, "required": required, "properties": properties}

    scalar = {"type": ["string", "number"]}
    bounds = obj(
        {
            "lower_key": key,
            "upper_key": key,
            "kind": {"enum": ["scenario", "estimate", "confidence"]},
            "note_ids": keys(3, 1),
            "origin_key": key,
            "coverage_key": key,
        },
        ["lower_key", "upper_key", "kind", "note_ids"],
    )
    common = {"view_id": key, "binding_id": key, "range": bounds}
    interval = obj(
        {
            **common,
            "preset": {"const": "interval_rows"},
            "row_keys": keys(6, 1),
            "group_key": key,
            "points": {
                "type": "array",
                "maxItems": 3,
                "items": obj({"key": key, "role": {"enum": ["primary", "current", "reference"]}}, ["key", "role"]),
            },
            "detail_keys": keys(6),
        },
        ["view_id", "preset", "row_keys", "range"],
    )
    series = obj(
        {
            **common,
            "preset": {"const": "series_plot"},
            "x_key": key,
            "x_kind": {"enum": ["numeric", "date"]},
            "value_key": key,
            "series_key": key,
            "summary_keys": keys(2),
            "facet": obj({"key": key, "order": {"type": "array", "minItems": 1, "maxItems": 6, "items": scalar}}, ["key"]),
            "series_roles": {
                "type": "array",
                "maxItems": 12,
                "items": obj({"series_value": scalar, "role": {"enum": sorted(ROLES)}, "note_ids": keys(3, 1)}, ["series_value", "role"]),
            },
        },
        ["view_id", "preset", "x_key", "x_kind", "value_key", "series_key"],
    )
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": 6,
        "items": {"oneOf": [interval, series]},
        "description": "Version 3 only. Bind a declared native Table (interval_rows) or Line (series_plot) view, including singletons. Use complete prepared rows, maximum 1200, at most 12 series and 6 total panels. Bind exact columns, including bounds and supporting fields; omit binding_id. Every plotted measure needs an applicable literal Artifact JSON evidence note with role metric_basis, measure_keys, value_kind amount/index/rate and adjustment nominal/real/not_applicable; index also requires index_base. Range/scenario notes must explain the prepared definition. Never infer bounds, units, forecast roles, confidence, index base or dates. No limit, focus, window, player or overlapping trend/annotation/scale declarations; whole-view guides are supported. Missing required data must be prepared upstream or use a qualified ordinary chart.",
    }


def _metric_basis(measures: set[str], notes: list[dict[str, Any]], columns: Mapping[str, Any]) -> None:
    bases: dict[str, set[str]] = {key: set() for key in measures}
    for note in notes:
        try:
            statement = json.loads(note["excerpt"])
        except (ValueError, TypeError, KeyError):
            continue
        if not isinstance(statement, dict) or statement.get("role") != "metric_basis":
            continue
        if set(statement) - {"role", "measure_keys", "value_kind", "index_base", "adjustment"}:
            _fail("", "metric_basis supports only role, measure_keys, value_kind, index_base and adjustment")
        fields = _keys(statement.get("measure_keys"), "/metric_basis/measure_keys", 12)
        if not set(fields) <= set(note["measure_keys"]):
            _fail("", "metric_basis fields must be covered by their evidence note")
        kind, adjustment = statement.get("value_kind"), statement.get("adjustment")
        if (
            not isinstance(kind, str)
            or not isinstance(adjustment, str)
            or kind not in {"amount", "index", "rate"}
            or adjustment not in {"nominal", "real", "not_applicable"}
        ):
            _fail("", "Supply the value kind and adjustment in the source metric_basis statement")
        if kind == "amount" and adjustment == "not_applicable":
            _fail("", "Amounts require a supplied nominal or real basis")
        if kind == "index":
            base = _number(statement.get("index_base"), "/metric_basis/index_base")
            if base is None or base <= 0:
                _fail("", "Index comparisons require their positive supplied index base")
        elif "index_base" in statement:
            _fail("", "Only indices have an index_base")
        signature = json.dumps({k: v for k, v in statement.items() if k not in {"role", "measure_keys"}}, sort_keys=True)
        for key in set(fields) & measures:
            if kind == "rate" and columns[key].get("percent_scale") not in {"points", "fraction"}:
                _fail("", f"Rate {key} requires an explicit percentage storage shape")
            if kind != "rate" and (columns[key].get("percent_scale") is not None or columns[key].get("format") == "percent"):
                _fail("", f"{key} cannot use percentage storage for an amount or index")
            if kind == "amount" and not columns[key].get("unit"):
                _fail("", f"Amount {key} requires its actual source unit")
            bases[key].add(signature)
    if any(len(value) != 1 for value in bases.values()) or len(set.union(*bases.values())) != 1:
        _fail(
            "",
            "Every co-plotted measure needs one compatible source metric_basis; missing or conflicting basis must be resolved upstream",
            "unit_incompatible",
        )
