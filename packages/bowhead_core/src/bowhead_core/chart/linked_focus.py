"""Closed same-result focus admission and saved structural validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from bowhead_core.chart.binding_catalog import build_binding_catalog, typed_tuple
from bowhead_core.chart.types import ChartColumn

FOCUS_KINDS = frozenset({"table", "line", "bar", "hbar", "scatter", "dual_axis", "combo"})
MAX_WINDOW_OBSERVATIONS = 1_200


def _fail(code: str, path: str, message: str, *, corrective_ids: list[str] | None = None) -> None:
    from bowhead_core.chart.presentation_plan import ChartPlanError

    raise ChartPlanError(code, "/presentation_plan" + path, message, corrective_ids=corrective_ids)


def validate_focus_input(
    plan: Mapping[str, Any], *, derive_bindings: bool = False, defer_equivalence: bool = False, allow_omitted_binding: bool = False
) -> dict[str, str]:
    bindings = None if derive_bindings and "view_bindings" not in plan else plan.get("view_bindings", {})
    interactions = plan.get("interactions", [])
    if bindings is not None and (
        not isinstance(bindings, dict)
        or not bindings
        or len(bindings) > 6
        or any(not isinstance(key, str) or not isinstance(value, str) or not value for key, value in bindings.items())
    ):
        _fail("plan_shape", "/view_bindings", "Declare exact view IDs and offered binding IDs for every focus participant")
    if not isinstance(interactions, list) or not 1 <= len(interactions) <= 3:
        _fail("plan_shape", "/interactions", "Declare 1 to 3 linked focus interactions")
    used: dict[str, str | None] = {}
    sources: set[str] = set()
    windows = 0
    players = 0
    deferred = False
    for i, interaction in enumerate(interactions):
        path = f"/interactions/{i}"
        if not isinstance(interaction, dict):
            _fail("plan_shape", path, "Use a closed interaction object")
        binding = interaction.get("binding_id")
        fields = set(interaction) | ({"binding_id"} if allow_omitted_binding else set())
        if interaction.get("preset") == "linked_focus" and fields == {"preset", "from_view", "to_views", "binding_id"}:
            source, targets = interaction["from_view"], interaction["to_views"]
            if (
                not isinstance(source, str)
                or source in sources
                or not isinstance(targets, list)
                or not 1 <= len(targets) <= 5
                or any(not isinstance(target, str) for target in targets)
                or len(set(targets)) != len(targets)
                or source in targets
            ):
                _fail("target_invalid", path, "Use one declaration per source and distinct other target views")
            sources.add(source)
            participants = [source, *targets]
        elif interaction.get("preset") == "time_window" and fields == {"preset", "view_ids", "binding_id"}:
            windows += 1
            participants = interaction["view_ids"]
            if (
                windows > 1
                or not isinstance(participants, list)
                or not 2 <= len(participants) <= 6
                or any(not isinstance(view, str) for view in participants)
                or len(set(participants)) != len(participants)
            ):
                _fail("target_invalid", path, "Use one time window with 2 to 6 distinct root-local views")
        elif interaction.get("preset") == "frame_player" and fields in (
            {"preset", "view_ids", "binding_id"},
            {"preset", "view_ids", "binding_id", "static_frame"},
        ):
            players += 1
            participants = interaction["view_ids"]
            frame = interaction.get("static_frame")
            if (
                players > 1
                or not isinstance(participants, list)
                or not 1 <= len(participants) <= 6
                or any(not isinstance(view, str) for view in participants)
                or len(set(participants)) != len(participants)
            ):
                _fail("target_invalid", path, "Use one prepared player with 1 to 6 distinct root-local views")
            if "static_frame" in interaction and type(frame) not in (str, int, float):
                _fail("binding_ineligible", path, "Choose an existing frame key or omit it for the latest prepared frame")
        else:
            _fail("static_representation_missing", path, "Use an implemented native interaction preset")
        omitted = allow_omitted_binding and "binding_id" not in interaction
        if omitted:
            deferred = True
        elif not isinstance(binding, str) or not binding:
            _fail("binding_ineligible", path, "Choose an offered binding ID")
        for view in participants:
            if bindings is not None and not omitted and bindings.get(view) != binding:
                _fail(
                    "binding_ineligible",
                    path,
                    "Use the same exact offered candidate ID for every participant and interaction; column names and new binding names are not candidate IDs",
                )
            if view in used and used[view] is not None and binding is not None and used[view] != binding:
                if not (bindings is None and defer_equivalence):
                    _fail("binding_ineligible", path, "Each view needs one verified observation grain")
                deferred = True
            used[view] = binding
    if not (1 if players else 2) <= len(used) <= 6 or (bindings is not None and (set(bindings) != set(used) or (not deferred and bindings != used))):
        _fail("target_invalid", "/view_bindings", "Omit unused binding declarations")
    return {} if deferred else {view: binding for view, binding in used.items() if binding is not None}


def _eligible_view(spec: Mapping[str, Any], descriptor: Mapping[str, Any]) -> bool:
    if descriptor.get("kind") == "frame":
        from bowhead_core.chart.prepared_frames import eligible_frame_view

        return eligible_frame_view(spec, descriptor)
    columns, rows = spec.get("columns", []), spec.get("rows", [])
    keys = descriptor["observation_keys"]
    dimensions = [column["key"] for column in columns if column.get("type") in {"categorical", "date", "geo"}]
    numeric = [column["key"] for column in columns if column.get("type") == "numeric"]
    if (
        spec.get("kind") not in FOCUS_KINDS
        or dimensions != keys
        or not 1 <= len(keys) <= 6
        or not numeric
        or not set(numeric) <= set(descriptor["measure_keys"])
        or spec.get("other_bucket_label")
        or (descriptor["kind"] == "time" and not any(column["key"] == descriptor["time_key"] and column.get("type") == "date" for column in columns))
    ):
        return False
    if spec["kind"] == "scatter" and len(numeric) < 2:
        return False
    if not all(all(key in row for key in keys) for row in rows):
        return False
    tuples = [typed_tuple(row, keys) for row in rows]
    # Existing native builders stringify categories; reject typed aliases at that boundary.
    if not rows or len(set(tuples)) != len(rows):
        return False
    if spec["kind"] == "table":
        return True
    from bowhead_core.chart_style_presentation import _pivot_long_spec, _string_cell

    if any(len({_string_cell(row[key]) for row in rows}) != len({typed_tuple(row, [key]) for row in rows}) for key in keys):
        return False
    if spec["kind"] == "scatter" or len(keys) == 1:
        return True
    normalized = _pivot_long_spec(dict(spec))
    return normalized is not None and len({column["key"] for column in normalized["columns"]}) == len(normalized["columns"])


def compile_focus(
    plan: Mapping[str, Any], spec: Mapping[str, Any], columns: Sequence[ChartColumn], rows: Sequence[Mapping[str, Any]], *, raw: bytes
) -> dict[str, Any]:
    validate_focus_input(plan, derive_bindings=True, defer_equivalence=True, allow_omitted_binding=True)
    plan = {**plan, "interactions": [dict(item) for item in plan["interactions"]]}
    views = {view["id"]: view["spec"] for view in spec.get("views", [])}
    # Display type declarations cannot create a source binding grant.
    from bowhead_core.chart.binding_catalog import source_binding_columns

    source_columns = source_binding_columns(columns, rows, raw)
    candidates = {candidate["id"]: candidate for candidate in build_binding_catalog(source_columns, rows, raw=raw)["candidates"]}
    # Resolve the most constrained presets first. Selection never creates a grant:
    # the existing complete-scope and native-grain admission below still runs.
    for index, interaction in sorted(enumerate(plan["interactions"]), key=lambda item: item[1]["preset"] == "linked_focus"):
        if "binding_id" in interaction:
            continue
        participants = interaction.get("view_ids", [interaction.get("from_view"), *interaction.get("to_views", [])])
        if not set(participants) <= set(views):
            _fail("target_invalid", f"/interactions/{index}", "Choose existing root-local views")
        kinds = (
            {"time"}
            if interaction["preset"] == "time_window"
            else {"frame"}
            if interaction["preset"] == "frame_player"
            else {"row", "entity", "time"}
        )
        offered = {
            cid: candidate
            for cid, candidate in candidates.items()
            if candidate["kind"] in kinds and all(_eligible_view(views[view], candidate) for view in participants)
        }
        required = {plan["view_bindings"][view] for view in participants if view in plan.get("view_bindings", {})}
        for other in plan["interactions"]:
            other_views = other.get("view_ids", [other.get("from_view"), *other.get("to_views", [])])
            if other.get("binding_id") and set(other_views) & set(participants):
                required.add(other["binding_id"])
        if required:
            offered = {cid: candidate for cid, candidate in offered.items() if required == {cid}}
        # Row/entity/time candidates with identical selection and observation keys
        # represent the same focus; prefer their stable catalog order.
        grains = {(tuple(candidate["dimension_keys"]), tuple(candidate["observation_keys"])) for candidate in offered.values()}
        if len(grains) != 1:
            _fail(
                "binding_unknown",
                f"/interactions/{index}/binding_id",
                "No unique eligible binding exists. Choose an offered corrective ID, or keep incompatible views independent.",
                corrective_ids=list(offered),
            )
        interaction["binding_id"] = next(iter(offered))
    if "view_bindings" not in plan:
        interactions = [dict(interaction) for interaction in plan["interactions"]]
        window = next((interaction for interaction in interactions if interaction["preset"] == "time_window"), None)
        preferred = candidates.get(window["binding_id"]) if window else None
        if preferred and preferred["kind"] == "time":
            for interaction in interactions:
                candidate = candidates.get(interaction["binding_id"])
                if (
                    interaction["preset"] == "linked_focus"
                    and candidate is not None
                    and candidate["kind"] in {"row", "entity"}
                    and candidate["dimension_keys"] == preferred["dimension_keys"]
                    and candidate["observation_keys"] == preferred["observation_keys"]
                    and {interaction["from_view"], *interaction["to_views"]} & set(window["view_ids"])
                ):
                    interaction["binding_id"] = window["binding_id"]
        plan = {**plan, "interactions": interactions}
        plan["view_bindings"] = validate_focus_input(plan, derive_bindings=True)
    validate_focus_input(plan)
    if len(views) < 2 or not set(plan["view_bindings"]) <= set(views):
        _fail("target_invalid", "/view_bindings", "View-targeted plans require at least two declared root-local views")
    bindings = {}
    for bid in sorted(set(plan["view_bindings"].values())):
        candidate = candidates.get(bid)
        if candidate is None:
            participants = [vid for vid in views if plan["view_bindings"].get(vid) == bid]
            window = any(interaction["preset"] == "time_window" and interaction["binding_id"] == bid for interaction in plan["interactions"])
            player_target = any(interaction["preset"] == "frame_player" and interaction["binding_id"] == bid for interaction in plan["interactions"])
            offered = [
                cid
                for cid, descriptor in candidates.items()
                if descriptor["kind"] in ({"frame"} if player_target else {"time"} if window else {"row", "entity", "time"})
                and all(_eligible_view(views[vid], descriptor) for vid in participants)
            ]
            _fail("binding_unknown", "/view_bindings", "Choose an offered binding from the current prepared dataset catalog", corrective_ids=offered)
        player = next(
            (interaction for interaction in plan["interactions"] if interaction["preset"] == "frame_player" and interaction["binding_id"] == bid),
            None,
        )
        if player is not None:
            from bowhead_core.chart.prepared_frames import compile_frame_binding

            participants = [vid for vid in views if plan["view_bindings"].get(vid) == bid]
            if set(participants) != set(player["view_ids"]) or any(
                item["binding_id"] == bid and item["preset"] != "frame_player" for item in plan["interactions"]
            ):
                _fail("binding_ineligible", "/interactions", "Frame participants need an independent frame grain")
            bindings[bid] = compile_frame_binding(candidate, {vid: views[vid] for vid in participants}, rows)
            key = player.get("static_frame", bindings[bid]["frame_keys"][-1])
            if type(key) not in (str, int, float) or key not in bindings[bid]["frame_keys"]:
                _fail("binding_ineligible", "/interactions", "The static frame must be an existing prepared key")
            player["static_frame"] = key
            continue
        if candidate["kind"] not in {"entity", "row", "time"}:
            _fail("binding_ineligible", "/view_bindings", "Choose a row, entity or ordered time candidate")
        if (
            any(interaction["preset"] == "time_window" and interaction["binding_id"] == bid for interaction in plan["interactions"])
            and candidate["kind"] != "time"
        ):
            _fail("binding_ineligible", "/view_bindings", "Time windows require an offered ordered time candidate")
        if candidate["kind"] == "time" and len({typed_tuple(row, [candidate["time_key"]]) for row in rows}) > MAX_WINDOW_OBSERVATIONS:
            _fail("binding_ineligible", "/view_bindings", "Prepare at most 1200 unique time observations for local time browsing")
        participants = [vid for vid in views if plan["view_bindings"].get(vid) == bid]
        if not all(_eligible_view(views[vid], candidate) for vid in participants):
            _fail(
                "binding_ineligible",
                "/view_bindings",
                "Focus requires unique original observations with a supported native pivot or point mapping; keep aggregates and different grains independent",
            )
        window_views = {
            vid
            for interaction in plan["interactions"]
            if interaction["preset"] == "time_window" and interaction["binding_id"] == bid
            for vid in interaction["view_ids"]
        }
        for vid in window_views:
            child = views[vid]
            if (
                len(child["rows"]) != len(rows)
                or child.get("top_n")
                or any(child.get(f"{key}_shown", 0) < child.get(f"{key}_total", 0) for key in ("points", "series"))
            ):
                _fail(
                    "binding_ineligible",
                    "/interactions",
                    "Time windows require the complete retained prepared population; keep sampled or ranked children independent",
                )
        original = {typed_tuple(row, candidate["observation_keys"]): row for row in rows}
        for vid in participants:
            if any(typed_tuple(row, candidate["observation_keys"]) not in original for row in views[vid]["rows"]):
                _fail("binding_ineligible", "/view_bindings", "A child contains observations outside the prepared population")
        bindings[bid] = {
            **{key: candidate[key] for key in ("kind", "dimension_keys", "observation_keys", "measure_keys")},
            **{key: candidate[key] for key in ("time_key", "order") if candidate["kind"] == "time"},
            "eligible_views": participants,
            "coverage": {
                "artifact_rows": {"available": len(rows), "total": len(rows)},
                "views": {
                    vid: {
                        "retained_rows": len(views[vid]["rows"]),
                        **{
                            key: views[vid][key]
                            for key in ("top_n", "series_shown", "series_total", "points_shown", "points_total")
                            if key in views[vid]
                        },
                    }
                    for vid in participants
                },
            },
        }
    return {"view_bindings": dict(plan["view_bindings"]), "interactions": plan["interactions"], "bindings": bindings}


def validate_saved_focus(plan: Mapping[str, Any], spec: Mapping[str, Any] | None = None, *, projected: bool = False) -> None:
    validate_focus_input(plan)
    bindings = plan.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != set(plan["view_bindings"].values()):
        _fail("plan_shape", "/bindings", "Every used binding requires its saved descriptor")
    for bid, descriptor in bindings.items():
        if not isinstance(descriptor, dict) or descriptor.get("kind") not in {"row", "entity", "time", "frame"}:
            _fail("plan_shape", "/bindings", "Invalid focus descriptor")
        player = next((item for item in plan["interactions"] if item["preset"] == "frame_player" and item["binding_id"] == bid), None)
        if (player is not None) != (descriptor["kind"] == "frame") or (player is not None and "static_frame" not in player):
            _fail("binding_ineligible", "/interactions", "Frame declarations require a canonical frame binding and static key")
        if projected and descriptor.get("availability") == "unavailable":
            if set(descriptor) != {"kind", "availability"}:
                _fail("plan_shape", "/bindings", "Unavailable descriptors cannot retain keys or coverage")
            continue
        if descriptor["kind"] == "frame":
            from bowhead_core.chart.prepared_frames import validate_saved_frame

            participants = [vid for vid, offered in plan["view_bindings"].items() if offered == bid]
            validate_saved_frame(descriptor, participants, spec, projected=projected)
            player = next((item for item in plan["interactions"] if item["preset"] == "frame_player" and item["binding_id"] == bid), None)
            if player is None or player.get("static_frame") not in descriptor["frame_keys"] or set(player["view_ids"]) != set(participants):
                _fail("binding_ineligible", "/interactions", "Saved players require an exact static frame and independent targets")
            continue
        fields = {"kind", "dimension_keys", "observation_keys", "measure_keys", "eligible_views", "coverage"}
        if descriptor["kind"] == "time":
            fields |= {"time_key", "order"}
        if set(descriptor) != fields | ({"availability"} if projected else set()) or (projected and descriptor["availability"] != "available"):
            _fail("plan_shape", "/bindings", "Focus descriptors use a closed canonical shape")
        for key in ("dimension_keys", "observation_keys", "measure_keys", "eligible_views"):
            values = descriptor[key]
            if (
                not isinstance(values, list)
                or not 1 <= len(values) <= (12 if key == "measure_keys" else 6)
                or any(not isinstance(value, str) or not value for value in values)
                or len(set(values)) != len(values)
            ):
                _fail("plan_shape", "/bindings", "Descriptor keys must be bounded and distinct")
        participants = [vid for vid, offered in plan["view_bindings"].items() if offered == bid]
        if descriptor["kind"] == "time" and (
            descriptor["dimension_keys"] != [descriptor["time_key"]]
            or descriptor["time_key"] not in descriptor["observation_keys"]
            or descriptor["order"] not in {"date", "numeric"}
        ):
            _fail("binding_ineligible", "/bindings", "Time descriptors require one exact ordered time dimension in the observation grain")
        if (
            any(interaction["preset"] == "time_window" and interaction["binding_id"] == bid for interaction in plan["interactions"])
            and descriptor["kind"] != "time"
        ):
            _fail("binding_ineligible", "/bindings", "Time windows require an ordered time descriptor")
        if set(descriptor["eligible_views"]) != set(participants) or not set(descriptor["dimension_keys"]) <= set(descriptor["observation_keys"]):
            _fail("binding_ineligible", "/bindings", "Descriptor targets and focus keys must match the saved plan")
        coverage = descriptor["coverage"]
        if (
            not isinstance(coverage, dict)
            or set(coverage) != {"artifact_rows", "views"}
            or not isinstance(coverage["artifact_rows"], dict)
            or set(coverage["artifact_rows"]) != {"available", "total"}
            or type(coverage["artifact_rows"]["available"]) is not int
            or coverage["artifact_rows"]["available"] < 0
            or (
                coverage["artifact_rows"]["total"] is not None
                and (
                    type(coverage["artifact_rows"]["total"]) is not int or coverage["artifact_rows"]["total"] < coverage["artifact_rows"]["available"]
                )
            )
            or not isinstance(coverage["views"], dict)
            or set(coverage["views"]) != set(participants)
        ):
            _fail("plan_shape", "/bindings", "Coverage must describe the prepared artifact and each participant")
        for vid, counts in coverage["views"].items():
            if (
                not isinstance(counts, dict)
                or set(counts) - {"retained_rows", "top_n", "series_shown", "series_total", "points_shown", "points_total"}
                or "retained_rows" not in counts
                or any(type(count) is not int or count < 0 for count in counts.values())
                or counts["retained_rows"] > coverage["artifact_rows"]["available"]
            ):
                _fail("plan_shape", "/bindings", "Retained counts must be finite nonnegative integers within the artifact population")
            if any(interaction["preset"] == "time_window" and vid in interaction["view_ids"] for interaction in plan["interactions"]) and (
                counts["retained_rows"] != coverage["artifact_rows"]["available"]
                or counts.get("top_n")
                or any(counts.get(f"{key}_shown", 0) < counts.get(f"{key}_total", 0) for key in ("points", "series"))
                or counts["retained_rows"] > MAX_WINDOW_OBSERVATIONS
            ):
                _fail("binding_ineligible", "/bindings", "Window participants require a bounded complete prepared population")
            if spec is not None:
                child = next((view["spec"] for view in spec.get("views", []) if view["id"] == vid), None)
                if child is None or not _eligible_view(child, descriptor) or len(child["rows"]) != counts["retained_rows"]:
                    _fail("binding_ineligible", "/bindings", "The saved child no longer represents its admitted observation grain")
                if descriptor["kind"] == "time":
                    from bowhead_core.chart.binding_catalog import _time_order

                    column = ChartColumn(key=descriptor["time_key"], header=descriptor["time_key"], type="date")
                    if _time_order(column, child["rows"]) != descriptor["order"]:
                        _fail("binding_ineligible", "/bindings", "Saved time observations must retain their admitted chronology")
