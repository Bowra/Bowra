"""Time browsing admits exact ordered observations and preserves canonical scope."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, timedelta

import pytest
from bowhead_core.canvas_tools import _add_chart
from bowhead_core.chart.expression import ChartExpressionError, validate_stored_composition
from bowhead_core.chart.presentation_plan import validate_projected_notes_plan
from bowhead_core.export_ir import iter_export_blocks, normalize_export_snapshot
from bowhead_core.source_access_projection import SourcePatchError, merge_viewer_patch_preserving_provenance
from test_chart_evidence_plan import prepared as prepared
from test_chart_evidence_plan import project
from test_chart_linked_focus import catalog
from test_export_snapshot import _request

RAW = b"month,revenue,cost\n2026-03,12.12345,5\n2026-01,-5,2\n2026-02,,3\n2026-04,0.000012345,4\n"


async def time_prepared(prepared, raw=RAW):
    artifacts, canvas, snapshots, ctx, args, _, note, _ = prepared
    data = await artifacts.put_bytes(content=raw, filename="months.csv", mime="text/csv", created_by="u", scope="user")
    ctx._ν15_allowlist.add("data", data.id)
    return artifacts, canvas, snapshots, ctx, {**args, "data_artifact_id": data.id}, data, note, raw


def window_args(prepared):
    _, _, _, _, args, _, _, raw = prepared
    bid = next(candidate["id"] for candidate in catalog(raw)["candidates"] if candidate["kind"] == "time")
    return {
        **{key: value for key, value in args.items() if key not in {"force_kind", "series", "presentation_plan"}},
        "title": "Saved monthly observations",
        "views": [
            {"id": "trend", "title": "Revenue trend", "hint": "trend", "force_kind": "line", "series": ["revenue"]},
            {"id": "detail", "title": "Original observations", "hint": "raw", "force_kind": "table", "series": ["revenue", "cost"]},
        ],
        "layout": "primary_aside",
        "presentation_plan": {
            "version": 1,
            "view_bindings": {"trend": bid, "detail": bid},
            "interactions": [
                {"preset": "time_window", "view_ids": ["trend", "detail"], "binding_id": bid},
                {"preset": "linked_focus", "from_view": "detail", "to_views": ["trend"], "binding_id": bid},
            ],
        },
    }


async def test_time_window_and_focus_round_trip_exact_scope_and_protected_descriptor(prepared):
    prepared = await time_prepared(prepared)
    _, canvas, _, ctx, _, data, _, _ = prepared
    result = await _add_chart(window_args(prepared), ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    plan = section.payload["spec"]["presentation_plan"]
    bid, descriptor = next(iter(plan["bindings"].items()))
    assert descriptor["kind"] == "time"
    assert descriptor["time_key"] == "month"
    assert descriptor["order"] == "date"
    assert descriptor["coverage"]["artifact_rows"] == {"available": 4, "total": 4}
    assert any(row.get("revenue") is None for row in section.payload["spec"]["views"][0]["spec"]["rows"])
    safe, aliases, access, validity = await project(prepared, section)
    validate_projected_notes_plan(safe["spec"]["presentation_plan"])
    changed = copy.deepcopy(safe["spec"])
    changed["views"][0]["spec"]["kind"] = "bar"
    restored = merge_viewer_patch_preserving_provenance(
        section.to_report_section(),
        {"spec": changed},
        alias_map=aliases,
        access_by_identity=access,
        evidence_validity_by_digest=validity,
        section_kind="chart",
        section_id=section.id,
        allow_label_edit=True,
    )
    validate_stored_composition(restored["spec"])
    assert restored["spec"]["presentation_plan"] == plan
    changed["presentation_plan"]["bindings"][bid]["time_key"] = "revenue"
    with pytest.raises(SourcePatchError):
        merge_viewer_patch_preserving_provenance(
            section.to_report_section(),
            {"spec": changed},
            alias_map=aliases,
            access_by_identity=access,
            evidence_validity_by_digest=validity,
            section_kind="chart",
            section_id=section.id,
            allow_label_edit=True,
        )
    denied, _, _, _ = await project(prepared, section, denied=[data.id])
    assert denied["spec"]["presentation_plan"]["bindings"][bid] == {"kind": "time", "availability": "unavailable"}
    validate_projected_notes_plan(denied["spec"]["presentation_plan"])
    assert denied["spec"]["rows"] == section.payload["spec"]["rows"]
    forged = copy.deepcopy(restored["spec"])
    forged["views"][0]["spec"]["rows"][0]["month"] = "2026-02-31"
    with pytest.raises(ChartExpressionError):
        validate_stored_composition(forged)


async def test_snapshot_and_static_ir_keep_all_saved_dates_without_browser_bounds(prepared):
    prepared = await time_prepared(prepared)
    _, canvas, snapshots, ctx, _, data, _, _ = prepared
    result = await _add_chart(window_args(prepared), ctx)
    section = (await canvas.list_sections(result["canvas_id"]))[0]

    async def projection(_canvas, sections):
        safe, _, _, _ = await project(prepared, sections[0])
        return [
            replace(sections[0], payload={key: value for key, value in safe.items() if key not in {"id", "kind", "rev", "parent_id", "order_index"}})
        ]

    candidate = await snapshots.build_candidate(
        canvas_id=section.canvas_id, request=replace(_request(), requested_by="u"), project_sections=projection
    )
    snapshot = await snapshots.commit_candidate(
        candidate,
        expected_projection_token=candidate.content_projection.projection_token,
        expected_surface_token=candidate.surface_projection.surface_token,
    )
    blocks = list(iter_export_blocks(normalize_export_snapshot(snapshot, asset_content_hashes={data.id: "sha256:" + "a" * 64}).blocks))
    visuals = [block for block in blocks if block.block_kind == "visual"]
    assert visuals
    for block in visuals:
        spec = block.content["payload"]["spec"]
        assert len(spec["rows"]) == 4
        assert "presentation_plan" not in spec
    assert (await canvas.list_sections(section.canvas_id))[0].rev == section.rev


async def test_host_derives_redundant_view_map_without_repairing_explicit_conflicts(prepared):
    prepared = await time_prepared(prepared)
    _, canvas, _, ctx, _, _, _, _ = prepared
    args = window_args(prepared)
    expected = args["presentation_plan"].pop("view_bindings")
    args["evidence_notes"] = [{"id": "definition", "measure_keys": ["revenue"], "ref_id": "2", "excerpt": "Revenue includes returns."}]
    args["presentation_plan"]["evidence_notes"] = copy.deepcopy(args["evidence_notes"])
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    saved = (await canvas.list_sections(result["canvas_id"]))[0].payload["spec"]["presentation_plan"]
    assert saved["view_bindings"] == expected
    assert saved["evidence_notes"][0]["excerpt"] == "Revenue includes returns."
    assert len(saved["evidence_notes"]) == 1
    assert "view_bindings" not in args["presentation_plan"]


async def test_host_unifies_only_verified_equivalent_row_and_time_candidates(prepared):
    value = await time_prepared(prepared)
    _, canvas, _, ctx, _, _, _, raw = value
    args = window_args(value)
    plan = args["presentation_plan"]
    time_id = plan["interactions"][0]["binding_id"]
    plan.pop("view_bindings")
    plan["interactions"][1]["binding_id"] = next(candidate["id"] for candidate in catalog(raw)["candidates"] if candidate["kind"] == "row")
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    saved = (await canvas.list_sections(result["canvas_id"]))[0].payload["spec"]["presentation_plan"]
    assert saved["view_bindings"] == {"trend": time_id, "detail": time_id}
    assert all(interaction["binding_id"] == time_id for interaction in saved["interactions"])
    assert plan["interactions"][1]["binding_id"] != time_id


@pytest.mark.parametrize(
    "damage",
    ["two_windows", "foreign", "bounds", "entity", "singleton", "ranked", "forged_root_notes", "conflicting_notes", "misplaced_interactions"],
)
async def test_invalid_windows_are_atomic_and_never_create_a_canvas(prepared, damage):
    prepared = await time_prepared(prepared)
    artifacts, canvas, _, ctx, _, _, _, raw = prepared
    args = window_args(prepared)
    plan = args["presentation_plan"]
    if damage == "two_windows":
        plan["interactions"].append(copy.deepcopy(plan["interactions"][0]))
    elif damage == "foreign":
        plan["interactions"][0]["view_ids"][1] = "foreign"
    elif damage == "bounds":
        plan["interactions"][0]["start"] = "2026-01"
    elif damage == "singleton":
        plan["interactions"][0]["view_ids"] = ["trend"]
    elif damage == "ranked":
        args["views"][0]["top_n"] = 2
    elif damage == "forged_root_notes":
        args["evidence_notes"] = [{"id": "definition", "measure_keys": ["revenue"], "ref_id": "2", "excerpt": "Invented methodology."}]
    elif damage == "conflicting_notes":
        plan["evidence_notes"] = [{"id": "nested", "measure_keys": ["revenue"], "ref_id": "2", "excerpt": "Revenue includes returns."}]
        args["evidence_notes"] = [{"id": "root", "measure_keys": ["revenue"], "ref_id": "2", "excerpt": "Revenue includes returns."}]
    elif damage == "misplaced_interactions":
        args["interactions"] = args["presentation_plan"].pop("interactions")
    else:
        bid = next(candidate["id"] for candidate in catalog(raw)["candidates"] if candidate["kind"] == "row")
        plan["view_bindings"] = dict.fromkeys(plan["view_bindings"], bid)
        for interaction in plan["interactions"]:
            interaction["binding_id"] = bid
    before = set(artifacts.backend.root_dir.rglob("*"))
    result = await _add_chart(args, ctx)
    assert "error" in result, result
    assert await canvas.get_canvas_by_session(ctx.session_id) is None
    assert set(artifacts.backend.root_dir.rglob("*")) == before


def test_time_catalog_rejects_ambiguous_grain_and_invalid_dates():
    for raw in [b"month,revenue\n2026-01,12\n2026-01-01,21\n", b"month,revenue\n2026-02-31,12\n2026-03-01,21\n", b"month,revenue\n1,12\n2,21\n"]:
        assert not any(candidate["kind"] == "time" for candidate in catalog(raw)["candidates"])
    assert any(
        candidate["kind"] == "time" and candidate["order"] == "numeric" for candidate in catalog(b"year,revenue\n2026,12\n2025,21\n")["candidates"]
    )


async def test_windows_reject_wider_grain_and_excessive_time_options_before_writes(prepared):
    many = ("month,revenue,cost\n" + "\n".join(f"{date(2020, 1, 1) + timedelta(days=index)},12,5" for index in range(1201))).encode()
    for raw in [b"month,region,revenue,cost\n2026-01,A,12,5\n2026-02,A,21,9\n", many]:
        value = await time_prepared(prepared, raw)
        _, canvas, _, ctx, _, _, _, _ = value
        result = await _add_chart(window_args(value), ctx)
        assert result.get("code") == "binding_ineligible", result
        assert await canvas.get_canvas_by_session(ctx.session_id) is None


@pytest.mark.parametrize("damage", ["ambiguous", "duplicate", "explicit_conflict", "invalid_id", "null_id"])
async def test_automatic_binding_does_not_guess_or_bypass_admission(prepared, damage):
    raw = RAW
    if damage == "ambiguous":
        raw = b"month,other_month,revenue,cost\n2026-01,2025-01,12,5\n2026-02,2025-02,13,6\n"
    elif damage == "duplicate":
        raw = b"month,revenue,cost\n2026-01,12,5\n2026-01,13,6\n"
    value = await time_prepared(prepared, raw)
    artifacts, canvas, _, ctx, _, data, _, _ = value
    args = window_args(await time_prepared(prepared))
    args["data_artifact_id"] = data.id
    args["presentation_plan"] = {"version": 2, "interactions": [{"preset": "time_window", "view_ids": ["trend", "detail"]}]}
    if damage == "explicit_conflict":
        ids = catalog(raw)["candidates"]
        args["presentation_plan"]["view_bindings"] = {"trend": ids[0]["id"], "detail": ids[-1]["id"]}
    elif damage in {"invalid_id", "null_id"}:
        args["presentation_plan"]["interactions"][0]["binding_id"] = None if damage == "null_id" else "time.invented"
    before = set(artifacts.backend.root_dir.rglob("*"))
    result = await _add_chart(args, ctx)
    assert "error" in result, result
    if damage == "ambiguous":
        assert len(result["corrective_ids"]) == 2
    assert await canvas.get_canvas_by_session(ctx.session_id) is None
    assert set(artifacts.backend.root_dir.rglob("*")) == before
