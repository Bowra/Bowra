"""Observed point/range annotations, without invented events or thresholds."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from typing import Any

from bowhead_core.chart.binding_catalog import build_binding_catalog, typed_tuple
from bowhead_core.chart.linked_focus import _eligible_view
from bowhead_core.chart.types import ChartColumn


def _fail(code: str, message: str) -> None:
    from bowhead_core.chart.presentation_plan import ChartPlanError

    raise ChartPlanError(code, "/presentation_plan/annotations", message)


def _cell(value: Any) -> bool:
    if value is None or isinstance(value, str):
        return True
    if type(value) is int:
        return abs(value) <= 2**53 - 1
    return type(value) is float and math.isfinite(value) and (not value.is_integer() or abs(value) <= 2**53 - 1)


def validate_annotation_input(items: Any, *, version: int = 1) -> None:
    import re

    if not isinstance(items, list) or not 1 <= len(items) <= 6:
        _fail("plan_shape", "Use one to six observed point or interval annotations")
    ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            _fail("plan_shape", "Use a closed annotation object")
        base = {"id", "view_id", "binding_id", "kind", "measure_key"}
        shape = {"tuple"} if item.get("kind") == "point" else {"lower", "upper"} if item.get("kind") == "interval" else set()
        if not shape or not base | shape <= set(item) or set(item) - base - shape - {"label"} - ({"note_ids", "role"} if version >= 2 else set()):
            _fail("static_representation_missing", "Use observed points or supplied time intervals; arbitrary thresholds/events are unsupported")
        if (
            any(not isinstance(item[key], str) or not item[key] for key in base)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", item["id"])
            or item["id"] in ids
        ):
            _fail("plan_shape", "Annotation IDs must be distinct lowercase identifiers and targets must be exact")
        ids.add(item["id"])
        if "label" in item and (not isinstance(item["label"], str) or not item["label"].strip() or len(item["label"]) > 80):
            _fail("plan_shape", "Use a short observational label of at most 80 Unicode characters")
        if shape == {"tuple"}:
            if not isinstance(item["tuple"], list) or len(item["tuple"]) != 1 or not _cell(item["tuple"][0]):
                _fail("binding_ineligible", "Point annotations require one original typed observation key")
        elif any(not _cell(item[key]) or item[key] is None for key in shape):
            _fail("binding_ineligible", "Intervals require exact supplied ordered time keys")


def _resolve(item: Mapping[str, Any], child: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    if item.get("role") in ("event", "coverage") and candidate["kind"] != "time":
        _fail("binding_ineligible", "Source statements require an exact ordered time binding")
    if not _eligible_view(child, candidate) or len(candidate["observation_keys"]) != 1:
        _fail("binding_ineligible", "Annotations require unique single-dimension native observations, without implicit aggregates")
    if item["measure_key"] not in {column["key"] for column in child["columns"] if column["type"] == "numeric"}:
        _fail("binding_ineligible", "Choose an exact retained numeric measure")
    keys = candidate["observation_keys"]
    rows = child["rows"]
    if item["kind"] == "point":
        wanted = typed_tuple(dict(zip(keys, item["tuple"], strict=True)), keys)
        if wanted not in {typed_tuple(row, keys) for row in rows}:
            _fail("binding_ineligible", "The annotated observation must exist in the retained native view")
    else:
        if (
            candidate["kind"] != "time"
            or len(rows) > 1200
            or child.get("row_count") != len(rows)
            or child.get("top_n")
            or child.get("other_bucket_label")
        ):
            _fail("binding_ineligible", "Intervals require complete, unsampled ordered time observations")
        domain = sorted({row[keys[0]] for row in rows})
        if item["lower"] not in domain or item["upper"] not in domain or domain.index(item["lower"]) > domain.index(item["upper"]):
            _fail("binding_ineligible", "Interval endpoints must be actual supplied time keys in increasing order")
    return {**copy.deepcopy(item), "observation_keys": list(keys), "encoding": child["kind"]}


def compile_annotations(
    items: Any, spec: Mapping[str, Any], columns: Sequence[ChartColumn], rows: Sequence[Mapping[str, Any]], *, raw: bytes, version: int = 1
) -> list[dict[str, Any]]:
    validate_annotation_input(items, version=version)
    from bowhead_core.chart.binding_catalog import source_binding_columns

    source_columns = source_binding_columns(columns, rows, raw)
    candidates = {item["id"]: item for item in build_binding_catalog(source_columns, rows, raw=raw)["candidates"]}
    views = {view["id"]: view["spec"] for view in spec.get("views", [])}
    if len(views) < 2:
        _fail("target_invalid", "Annotations require declared views inside one native composition")
    result = []
    for item in items:
        if item["view_id"] not in views or item["binding_id"] not in candidates:
            _fail("target_invalid", "Choose a view in this root and an offered row/entity/time candidate")
        candidate = candidates[item["binding_id"]]
        if item.get("role") in {"event", "coverage"} and candidate["kind"] != "time":
            _fail("binding_ineligible", "Source-supplied event and coverage annotations require an offered ordered time binding")
        if item["kind"] == "interval" and len(views[item["view_id"]]["rows"]) != len(rows):
            _fail("binding_ineligible", "An interval must preserve the complete prepared time population")
        result.append(_resolve(item, views[item["view_id"]], candidate))
    return result


def validate_saved_annotations(plan: Mapping[str, Any], spec: Mapping[str, Any] | None = None, *, projected: bool = False) -> None:
    if "annotations" not in plan:
        return
    items = plan["annotations"]
    if not isinstance(items, list) or not 1 <= len(items) <= 6:
        _fail("plan_shape", "Save one to six bounded annotations")
    views = {view["id"]: view["spec"] for view in spec.get("views", [])} if spec is not None else None
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in seen or not isinstance(item.get("view_id"), str):
            _fail("plan_shape", "Preserve distinct annotation identity and root-local targets")
        seen.add(item["id"])
        if projected and item.get("availability") == "unavailable":
            if set(item) != {"id", "view_id", "availability"}:
                _fail("plan_shape", "Unavailable annotations cannot retain keys, values or labels")
            continue
        if projected and item.get("availability") != "available":
            _fail("plan_shape", "Projected annotations require current availability")
        declared = {key: value for key, value in item.items() if key not in {"observation_keys", "encoding", "availability"}}
        validate_annotation_input([declared], version=plan["version"])
        if (
            item.get("observation_keys") is None
            or not isinstance(item["observation_keys"], list)
            or len(item["observation_keys"]) != 1
            or not isinstance(item["observation_keys"][0], str)
            or item.get("encoding") not in {"table", "line", "bar", "hbar", "scatter", "dual_axis", "combo"}
        ):
            _fail("plan_shape", "Annotations need an exact observed grain and saved native encoding")
        if views is not None:
            child = views.get(item["view_id"])
            if child is None:
                _fail("target_invalid", "Annotation targets must remain inside this root")
            columns = [
                ChartColumn(**{key: value for key, value in column.items() if key in ChartColumn.__dataclass_fields__}) for column in child["columns"]
            ]
            candidate = next(
                (entry for entry in build_binding_catalog(columns, child["rows"], raw=b"")["candidates"] if entry["id"] == item["binding_id"]), None
            )
            if candidate is None or _resolve(declared, child, candidate) != item:
                _fail("binding_ineligible", "Saved annotations must match current observed data and encoding")


def annotation_views(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Derive static leaf marks from projected annotations, without browser state."""
    items = spec.get("presentation_plan", {}).get("annotations", [])
    result = []
    for view in spec.get("views", []):
        annotations = [item for item in items if item.get("availability") == "available" and item["view_id"] == view["id"]]
        child = {**view["spec"], "_export_annotations": annotations} if annotations else view["spec"]
        result.append({**view, "spec": child})
    return result
