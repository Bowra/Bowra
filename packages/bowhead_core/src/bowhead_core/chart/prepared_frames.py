"""Prepared observations, stable entity identities and deterministic frame scope."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from bowhead_core.chart.binding_catalog import _time_order, typed_tuple
from bowhead_core.chart.types import ChartColumn

MAX_FRAME_ROWS = 1_200


def _fail(message: str) -> None:
    from bowhead_core.chart.presentation_plan import ChartPlanError

    raise ChartPlanError("binding_ineligible", "/presentation_plan/interactions", message)


def eligible_frame_view(spec: Mapping[str, Any], binding: Mapping[str, Any]) -> bool:
    dimensions = [column["key"] for column in spec.get("columns", []) if column.get("type") != "numeric"]
    numeric = [column["key"] for column in spec.get("columns", []) if column.get("type") == "numeric"]
    rows = spec.get("rows", [])
    return bool(
        spec.get("kind") in {"scatter", "table"}
        and len(binding.get("entity_keys", [])) == 1
        and set(dimensions) == set(binding["observation_keys"])
        and numeric
        and set(numeric) <= set(binding["measure_keys"])
        and (spec["kind"] != "scatter" or len(numeric) >= 2)
        and rows
        and len(rows) <= MAX_FRAME_ROWS
        and spec.get("row_count") == len(rows)
        and not spec.get("top_n")
        and not spec.get("other_bucket_label")
        and not any(spec.get(f"{key}_shown", 0) < spec.get(f"{key}_total", 0) for key in ("points", "series"))
        and all(all(key in row for key in dimensions) for row in rows)
        and len({typed_tuple(row, dimensions) for row in rows}) == len(rows)
    )


def compile_frame_binding(candidate: Mapping[str, Any], views: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if candidate.get("kind") != "frame" or len(candidate.get("entity_keys", [])) != 1:
        _fail("Choose an offered frame candidate with one exact entity key")
    time, entity = candidate["time_key"], candidate["entity_keys"][0]
    if (
        not rows
        or len(rows) > MAX_FRAME_ROWS
        or any(
            type(row.get(entity)) not in (str, int, float)
            or (type(row[entity]) in (int, float) and (not math.isfinite(row[entity]) or abs(row[entity]) > 2**53 - 1))
            for row in rows
        )
    ):
        _fail("Prepare at most 1200 finite, unambiguous entity/frame observations")
    order = _time_order(ChartColumn(key=time, header=time, type="date"), rows)
    if order is None:
        _fail("Frame keys require complete ISO dates/months or numeric calendar years")
    frames = sorted({row[time] for row in rows})
    if len(frames) > 120:
        _fail("Prepare at most 120 existing time frames")
    originals = {typed_tuple(row, candidate["observation_keys"]) for row in rows}
    if len(originals) != len(rows):
        _fail("Duplicate entity/frame observations require upstream preparation")
    entities = {typed_tuple(row, [entity]): row[entity] for row in rows}
    # Native labels stringify entity keys; distinct typed aliases cannot share a label.
    values = [entities[key] for key in sorted(entities)]
    if len({str(value) for value in values}) != len(values):
        _fail("Entity labels must remain distinct at the native chart boundary")
    domains = {}
    from bowhead_core.chart.shared_scales import _fixed_domain, _view_evidence

    for vid, child in views.items():
        if not eligible_frame_view(child, candidate) or {typed_tuple(row, candidate["observation_keys"]) for row in child["rows"]} != originals:
            _fail("Frames require complete native scatter/Table observations with one entity key; keep aggregates and sampled views independent")
        domains[vid] = (
            {"encoding": "scatter", "domains": {axis: _fixed_domain("scatter", _view_evidence(child, axis)[2]) for axis in ("x", "y")}}
            if child["kind"] == "scatter"
            else None
        )
    return {
        **{key: candidate[key] for key in ("kind", "dimension_keys", "observation_keys", "measure_keys", "time_key", "entity_keys")},
        "order": order,
        "frame_keys": frames,
        "entity_values": values,
        "axis_domains": domains,
        "eligible_views": list(views),
        "coverage": {
            "artifact_rows": {"available": len(rows), "total": len(rows)},
            "views": {vid: {"retained_rows": len(child["rows"])} for vid, child in views.items()},
        },
    }


def validate_saved_frame(binding: Mapping[str, Any], participants: list[str], spec: Mapping[str, Any] | None, *, projected: bool) -> None:
    expected = {
        "kind",
        "dimension_keys",
        "observation_keys",
        "measure_keys",
        "time_key",
        "entity_keys",
        "order",
        "frame_keys",
        "entity_values",
        "axis_domains",
        "eligible_views",
        "coverage",
    }
    if set(binding) != expected | ({"availability"} if projected else set()) or (projected and binding.get("availability") != "available"):
        _fail("Frame descriptors use a closed canonical shape")
    for key, maximum in (("dimension_keys", 1), ("entity_keys", 1), ("observation_keys", 2), ("measure_keys", 12), ("eligible_views", 6)):
        values = binding[key]
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= maximum
            or any(not isinstance(value, str) or not value for value in values)
            or len(set(values)) != len(values)
        ):
            _fail("Frame descriptor keys must be bounded and distinct")
    if (
        len(binding["observation_keys"]) != 2
        or binding["dimension_keys"] != binding["entity_keys"]
        or not isinstance(binding["time_key"], str)
        or set(binding["observation_keys"]) != {binding["time_key"], *binding["entity_keys"]}
        or set(binding["eligible_views"]) != set(participants)
    ):
        _fail("Frame targets and observation keys must match the saved plan")
    frames, entities = binding["frame_keys"], binding["entity_values"]
    if not isinstance(frames, list) or not 1 <= len(frames) <= 120 or not isinstance(entities, list) or not 1 <= len(entities) <= MAX_FRAME_ROWS:
        _fail("Saved frames and entities must stay bounded")
    if any(
        type(value) not in (str, int, float) or (type(value) in (int, float) and (not math.isfinite(value) or abs(value) > 2**53 - 1))
        for value in [*frames, *entities]
    ):
        _fail("Frame/entity identities must be finite safe typed cells")
    time = binding["time_key"]
    if (
        _time_order(ChartColumn(key=time, header=time, type="date"), [{time: frame} for frame in frames]) != binding["order"]
        or frames != sorted(set(frames))
        or len({str(value) for value in entities}) != len(entities)
    ):
        _fail("Saved time order and entity identities must be exact")
    if not isinstance(binding["axis_domains"], dict) or set(binding["axis_domains"]) != set(participants):
        _fail("Every frame view requires its fixed native numeric domains")
    for domain in binding["axis_domains"].values():
        if domain is None:
            continue
        if (
            not isinstance(domain, dict)
            or set(domain) != {"encoding", "domains"}
            or domain["encoding"] != "scatter"
            or not isinstance(domain["domains"], dict)
            or set(domain["domains"]) != {"x", "y"}
        ):
            _fail("Frame domains support native scatter x/y axes")
        for bounds in domain["domains"].values():
            if (
                not isinstance(bounds, list)
                or len(bounds) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in bounds)
                or bounds[0] >= bounds[1]
            ):
                _fail("Frame axis bounds must be finite and ordered")
    coverage = binding["coverage"]
    if (
        not isinstance(coverage, dict)
        or set(coverage) != {"artifact_rows", "views"}
        or not isinstance(coverage["artifact_rows"], dict)
        or set(coverage["artifact_rows"]) != {"available", "total"}
        or type(coverage["artifact_rows"]["available"]) is not int
        or not 1 <= coverage["artifact_rows"]["available"] <= MAX_FRAME_ROWS
        or type(coverage["artifact_rows"]["total"]) is not int
        or coverage["artifact_rows"]["available"] != coverage["artifact_rows"]["total"]
        or coverage["views"] != {vid: {"retained_rows": coverage["artifact_rows"]["available"]} for vid in participants}
    ):
        _fail("Frame views must retain the complete prepared population")
    if any(type(counts["retained_rows"]) is not int for counts in coverage["views"].values()):
        _fail("Frame coverage must contain integer counts")
    if spec is not None:
        children = {view["id"]: view["spec"] for view in spec.get("views", []) if view["id"] in participants}
        rows = next(iter(children.values()))["rows"] if children else []
        candidate = {key: binding[key] for key in ("kind", "dimension_keys", "observation_keys", "measure_keys", "time_key", "entity_keys")}
        rebuilt = compile_frame_binding(candidate, children, rows)
        if {key: value for key, value in binding.items() if key != "availability"} != rebuilt:
            _fail("Saved frames must match canonical observations and fixed domains")


def static_frame_views(spec: Mapping[str, Any], *, locale: str) -> list[dict[str, Any]]:
    """Freeze the authored frame before presentation, independent of browser playback."""
    plan = spec.get("presentation_plan", {})
    player = next((item for item in plan.get("interactions", []) if item.get("preset") == "frame_player"), None)
    if not player:
        return list(spec.get("views", []))
    binding = plan.get("bindings", {}).get(player["binding_id"])
    if not binding or binding.get("availability") == "unavailable":
        # No accessible frame descriptor means no invented slice or unit/domain.
        return list(spec.get("views", []))
    time, entity = binding["time_key"], binding["entity_keys"][0]
    output = []
    positions = {typed_tuple({entity: value}, [entity]): index for index, value in enumerate(binding["entity_values"])}
    for view in spec["views"]:
        child = dict(view["spec"])
        if view["id"] in player["view_ids"] and child.get("_export_frame_scope") != {"time_key": time, "key": player["static_frame"]}:
            rows = [row for row in child["rows"] if row[time] == player["static_frame"]]
            rows.sort(key=lambda row: positions[typed_tuple(row, [entity])])
            label = ("时间帧：" if locale.lower().startswith("zh") else "Time frame: ") + str(player["static_frame"])
            child.update(
                _export_frame_scope={"time_key": time, "key": player["static_frame"]},
                rows=rows,
                row_count=len(rows),
                subtitle=" · ".join(filter(None, [child.get("subtitle"), label])),
            )
            if child["kind"] == "scatter":
                child["columns"] = [column for column in child["columns"] if column["key"] != time]
                child["_export_axis_domains"] = binding["axis_domains"][view["id"]]
                child["_export_frame_entities"] = {"key": entity, "values": binding["entity_values"]}
        output.append({**view, "spec": child})
    return output
