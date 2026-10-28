"""Version-two evidence relations and prepared entity trends in native charts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from bowhead_core.chart.binding_catalog import build_binding_catalog
from bowhead_core.chart.prepared_frames import compile_frame_binding, validate_saved_frame
from bowhead_core.chart.types import ChartColumn


def _fail(path: str, message: str) -> None:
    from bowhead_core.chart.presentation_plan import ChartPlanError

    raise ChartPlanError("evidence_unverified", "/presentation_plan/" + path, message)


def validate_annotation_evidence(plan: Mapping[str, Any], *, projected: bool = False) -> None:
    """A relation identifies literal source context, never proves a causal claim."""
    notes = {note["id"]: note for note in plan.get("evidence_notes", [])}
    for item in plan.get("annotations", []):
        if projected and item.get("availability") == "unavailable":
            continue
        ids = item.get("note_ids")
        role = item.get("role", "observation")
        if ids is None and role == "observation":
            continue
        if plan.get("version") not in (2, 3):
            _fail("annotations", "Evidence relations require presentation plan version 2")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 3 or any(not isinstance(nid, str) for nid in ids) or len(set(ids)) != len(ids):
            _fail("annotations", "Choose one to three distinct note IDs already declared in this root")
        if any(nid not in notes or item["measure_key"] not in notes[nid]["measure_keys"] for nid in ids):
            _fail("annotations", "Each cited note must belong to this root and apply to the annotated measure")
        selected = [notes[nid] for nid in ids]
        if projected and any(note.get("availability") != "available" for note in selected):
            _fail("annotations", "An available annotation cannot cite unavailable evidence")
        if role == "observation":
            if item.get("label") and not any(item["label"] in note["excerpt"] for note in selected):
                _fail("annotations", "A cited caption must copy literal source text; do not invent an explanation")
            continue
        if not isinstance(role, str) or role not in {"event", "coverage"} or len(selected) != 1:
            _fail("annotations", "A supplied event or partial-period qualification requires exactly one structured source note")
        try:
            statement = json.loads(selected[0]["excerpt"])
        except (ValueError, TypeError):
            _fail("annotations", "Copy a literal JSON statement from an existing Artifact for an event or coverage role")
        shape = {"at"} if role == "event" else {"lower", "upper"}
        if (
            not isinstance(statement, dict)
            or set(statement) != {"role", "time_key", "measure_keys", "text", *shape}
            or statement["role"] != role
            or statement["measure_keys"] != selected[0]["measure_keys"]
            or not isinstance(statement["time_key"], str)
            or not isinstance(statement["text"], str)
            or not statement["text"].strip()
            or len(statement["text"]) > 80
            or item.get("label") != statement["text"]
        ):
            _fail("annotations", "Preserve the source statement's role, measure scope and literal text")
        if "observation_keys" in item and item["observation_keys"] != [statement["time_key"]]:
            _fail("annotations", "The source statement must name the annotation's exact time dimension")
        bounds_match = (
            item["kind"] == "point" and item["tuple"] == [statement["at"]]
            if role == "event"
            else item["kind"] == "interval" and item["lower"] == statement["lower"] and item["upper"] == statement["upper"]
        )
        if not bounds_match:
            _fail("annotations", "Use only the source statement's supplied point or interval; never infer event dates or coverage")


def validate_trend_input(items: Any, *, allow_omitted_binding: bool = False) -> None:
    if not isinstance(items, list) or not 1 <= len(items) <= 3:
        _fail("table_trends", "Use one to three Table trend declarations")
    views: set[str] = set()
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) | ({"binding_id"} if allow_omitted_binding else set()) != {"view_id", "binding_id", "measure_key"}
            or any(not isinstance(value, str) or not value for value in item.values())
            or item["view_id"] in views
        ):
            _fail("table_trends", "Choose one offered frame binding and one exact measure per distinct Table view")
        views.add(item["view_id"])


def compile_trends(
    items: Any, spec: Mapping[str, Any], columns: Sequence[ChartColumn], rows: Sequence[Mapping[str, Any]], *, raw: bytes
) -> list[dict[str, Any]]:
    validate_trend_input(items, allow_omitted_binding=True)
    from bowhead_core.chart.binding_catalog import source_binding_columns

    source_columns = source_binding_columns(columns, rows, raw)
    candidates = {candidate["id"]: candidate for candidate in build_binding_catalog(source_columns, rows, raw=raw)["candidates"]}
    views = {view["id"]: view["spec"] for view in spec.get("views", [])}
    result = []
    for index, item in enumerate(items):
        child, candidate = views.get(item["view_id"]), candidates.get(item.get("binding_id"))
        from bowhead_core.chart.presentation_plan import ChartPlanError

        path = f"/presentation_plan/table_trends/{index}"
        if not child or child["kind"] != "table":
            raise ChartPlanError(
                "target_invalid",
                path + "/view_id",
                "Choose an existing native Table view",
                corrective_ids=[vid for vid, view in views.items() if view["kind"] == "table"],
            )
        if "binding_id" not in item:
            eligible = {}
            for cid, offered in candidates.items():
                if offered["kind"] != "frame":
                    continue
                try:
                    compile_frame_binding(offered, {item["view_id"]: child}, rows)
                except ChartPlanError:
                    continue
                eligible[cid] = offered
            if len(eligible) != 1:
                raise ChartPlanError(
                    "binding_unknown",
                    path + "/binding_id",
                    "No unique eligible entity/time frame exists. Choose an offered corrective ID; do not aggregate the original observations.",
                    corrective_ids=list(eligible),
                )
            item = {**item, "binding_id": next(iter(eligible))}
            candidate = eligible[item["binding_id"]]
        if not candidate or candidate["kind"] != "frame":
            raise ChartPlanError(
                "binding_unknown",
                path + "/binding_id",
                "Choose an offered entity/time frame candidate",
                corrective_ids=[cid for cid, value in candidates.items() if value["kind"] == "frame"],
            )
        if item["measure_key"] not in {column["key"] for column in child["columns"] if column["type"] == "numeric"}:
            _fail("table_trends", "Choose a retained numeric measure")
        binding = compile_frame_binding(candidate, {item["view_id"]: child}, rows)
        result.append({**item, "binding": binding})
    return result


def validate_saved_trends(plan: Mapping[str, Any], spec: Mapping[str, Any] | None = None, *, projected: bool = False) -> None:
    items = plan.get("table_trends")
    if items is None:
        return
    if plan["version"] not in (2, 3) or not isinstance(items, list) or not 1 <= len(items) <= 3:
        _fail("table_trends", "Bounded Table trends require presentation plan version 2")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("view_id"), str) or item["view_id"] in seen:
            _fail("table_trends", "Preserve distinct Table trend targets")
        seen.add(item["view_id"])
        if projected and item.get("availability") == "unavailable":
            if set(item) != {"view_id", "availability"}:
                _fail("table_trends", "Unavailable trends cannot retain descriptors")
            continue
        expected = {"view_id", "binding_id", "measure_key", "binding"} | ({"availability"} if projected else set())
        if set(item) != expected or (projected and item["availability"] != "available"):
            _fail("table_trends", "Preserve the closed trend descriptor")
        validate_trend_input([{key: item[key] for key in ("view_id", "binding_id", "measure_key")}])
        binding = item["binding"]
        if not isinstance(binding, dict):
            _fail("table_trends", "A verified frame descriptor is required")
        validate_saved_frame(binding, [item["view_id"]], spec, projected=False)
        if item["measure_key"] not in binding["measure_keys"] or binding["axis_domains"] != {item["view_id"]: None}:
            _fail("table_trends", "A trend must retain its native Table and exact measure")
        if spec is not None:
            child = next(view["spec"] for view in spec["views"] if view["id"] == item["view_id"])
            columns = [
                ChartColumn(**{key: value for key, value in column.items() if key in ChartColumn.__dataclass_fields__}) for column in child["columns"]
            ]
            offered = build_binding_catalog(columns, child["rows"], raw=b"")["candidates"]
            if not any(candidate["id"] == item["binding_id"] and candidate["kind"] == "frame" for candidate in offered):
                _fail("table_trends", "Saved trends must retain their exact offered frame identity")
        if any(
            interaction.get("preset") == "frame_player" and item["view_id"] in interaction["view_ids"] for interaction in plan.get("interactions", [])
        ):
            _fail("table_trends", "Use separate Table views for whole-period trends and individual frame playback")


def contextual_views(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Pass authorized semantics to the existing static leaf renderer."""
    plan = spec.get("presentation_plan", {})
    trends = {item["view_id"]: item for item in plan.get("table_trends", []) if item.get("availability") == "available"}
    return [
        {**view, "spec": {**view["spec"], "_export_table_trend": trends[view["id"]]}} if view["id"] in trends else view
        for view in spec.get("views", [])
    ]
