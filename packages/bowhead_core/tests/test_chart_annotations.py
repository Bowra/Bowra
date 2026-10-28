"""Observed annotations preserve evidence through native admission and export."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest
from bowhead_core.canvas_tools import _add_chart
from bowhead_core.chart.expression import ChartExpressionError, validate_stored_composition
from bowhead_core.chart.presentation_plan import validate_projected_notes_plan
from bowhead_core.chart_style_export import build_chart_style_export_theme_v2
from bowhead_core.export_ir import iter_export_blocks, normalize_export_snapshot
from bowhead_core.print_theme import current_print_theme_snapshot
from bowhead_core.source_access_projection import SourcePatchError, merge_viewer_patch_preserving_provenance
from test_chart_evidence_plan import prepared as prepared
from test_chart_evidence_plan import project
from test_chart_linked_focus import catalog
from test_chart_style_export_v2 import _effective_v2
from test_chart_time_window import time_prepared, window_args
from test_export_snapshot import _request


def annotation_args(value, *, notes=False):
    args = window_args(value)
    bid = next(item["id"] for item in catalog(value[-1])["candidates"] if item["kind"] == "time")
    plan = {
        "version": 1,
        "annotations": [
            {
                "id": "observed",
                "view_id": "trend",
                "binding_id": bid,
                "kind": "point",
                "measure_key": "revenue",
                "tuple": ["2026-03"],
                "label": "Observed revenue",
            },
            {
                "id": "missing",
                "view_id": "detail",
                "binding_id": bid,
                "kind": "point",
                "measure_key": "revenue",
                "tuple": ["2026-02"],
                "label": "Missing observation",
            },
            {
                "id": "period",
                "view_id": "trend",
                "binding_id": bid,
                "kind": "interval",
                "measure_key": "revenue",
                "lower": "2026-01",
                "upper": "2026-04",
                "label": "Supplied period",
            },
        ],
        "guide_steps": [
            {"title": "Observed point", "view_id": "trend", "annotation_id": "observed"},
            {"title": "Missing value", "view_id": "detail", "annotation_id": "missing"},
        ],
    }
    if notes:
        plan["evidence_notes"] = value[4]["presentation_plan"]["evidence_notes"]
    return {**args, "presentation_plan": plan}


@pytest.mark.parametrize("frozen_style", [False, True])
async def test_observed_annotations_project_patch_and_export_without_browser_state(prepared, frozen_style):
    value = await time_prepared(prepared)
    _, canvas, snapshots, ctx, _, data, _, _ = value
    args = annotation_args(value)
    before = copy.deepcopy(args)
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    assert args == before
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    root = section.payload["spec"]
    validate_stored_composition(root)
    safe, aliases, access, validity = await project(value, section)
    validate_projected_notes_plan(safe["spec"]["presentation_plan"])
    items = safe["spec"]["presentation_plan"]["annotations"]
    assert items[0]["tuple"] == ["2026-03"]
    assert items[0]["observation_keys"] == ["month"] and items[0]["availability"] == "available"
    changed = copy.deepcopy(safe["spec"])
    changed["views"][0]["spec"]["show_legend"] = False
    merged = merge_viewer_patch_preserving_provenance(
        section.to_report_section(),
        {"spec": changed},
        alias_map=aliases,
        access_by_identity=access,
        evidence_validity_by_digest=validity,
        section_kind="chart",
        section_id=section.id,
        allow_label_edit=True,
    )
    assert merged["spec"]["presentation_plan"] == root["presentation_plan"]
    changed["presentation_plan"]["annotations"][0]["tuple"] = ["2026-04"]
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
    forged = copy.deepcopy(root)
    forged["views"][0]["spec"]["kind"] = "bar"
    with pytest.raises(ChartExpressionError):
        validate_stored_composition(forged)
    denied, _, _, _ = await project(value, section, denied=[data.id])
    validate_projected_notes_plan(denied["spec"]["presentation_plan"])
    assert denied["spec"]["presentation_plan"]["annotations"] == [
        {"id": item["id"], "view_id": item["view_id"], "availability": "unavailable"} for item in items
    ]

    async def projection(_canvas, sections):
        dto, _, _, _ = await project(value, sections[0])
        return [replace(sections[0], payload={key: val for key, val in dto.items() if key not in {"id", "kind", "rev", "parent_id", "order_index"}})]

    request = replace(_request(), requested_by="u", locale="en-US")
    if frozen_style:
        request = replace(request, theme=build_chart_style_export_theme_v2(_effective_v2(), print_theme=current_print_theme_snapshot()))
    candidate = await snapshots.build_candidate(canvas_id=section.canvas_id, request=request, project_sections=projection)
    snapshot = await snapshots.commit_candidate(
        candidate,
        expected_projection_token=candidate.content_projection.projection_token,
        expected_surface_token=candidate.surface_projection.surface_token,
    )
    leaves = [
        block.content["payload"]["spec"]
        for block in iter_export_blocks(normalize_export_snapshot(snapshot, asset_content_hashes={data.id: "sha256:" + "a" * 64}).blocks)
        if block.block_kind == "visual"
    ]
    assert [len(leaf["_export_annotations"]) for leaf in leaves] == [2, 1]
    assert all(len(leaf["rows"]) == 4 for leaf in leaves)
    assert (await canvas.list_sections(section.canvas_id))[0].payload["spec"] == root


async def test_note_only_loss_removes_annotation_copy_and_keys(prepared):
    value = await time_prepared(prepared)
    _, canvas, _, ctx, _, _, note, _ = value
    args = annotation_args(value, notes=True)
    args["evidence_notes"] = []
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    safe, _, _, _ = await project(value, section, denied=[note.id])
    validate_projected_notes_plan(safe["spec"]["presentation_plan"])
    assert all(set(item) == {"id", "view_id", "availability"} for item in safe["spec"]["presentation_plan"]["annotations"])
    assert "Observed revenue" not in str(safe) and "2026-03" in str(safe["spec"]["views"][0]["spec"]["rows"])


@pytest.mark.parametrize("changed_source", ["note", "data"])
async def test_changed_source_bytes_remove_annotation_descriptors(prepared, changed_source):
    value = await time_prepared(prepared)
    _, canvas, _, ctx, _, data, note, _ = value
    result = await _add_chart(annotation_args(value, notes=True), ctx)
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    Path((note if changed_source == "note" else data).path).write_bytes(b"Changed evidence")
    safe, _, _, _ = await project(value, section)
    validate_projected_notes_plan(safe["spec"]["presentation_plan"])
    assert all(set(item) == {"id", "view_id", "availability"} for item in safe["spec"]["presentation_plan"]["annotations"])
    assert "Observed revenue" not in str(safe)


@pytest.mark.parametrize(
    "damage", ["foreign", "unknown", "measure", "point", "threshold", "interval", "label", "duplicate", "guide", "value", "null_bound", "boolean"]
)
async def test_invalid_annotations_write_no_partial_figure(prepared, damage):
    value = await time_prepared(prepared)
    artifacts, canvas, _, ctx, *_ = value
    args = annotation_args(value)
    item = args["presentation_plan"]["annotations"][0]
    if damage == "foreign":
        item["view_id"] = "another_root"
    elif damage == "unknown":
        item["binding_id"] = "row.invented"
    elif damage == "measure":
        item["measure_key"] = "invented"
    elif damage == "point":
        item["tuple"] = ["2026-05"]
    elif damage == "threshold":
        item.update(kind="threshold", value=99)
    elif damage == "interval":
        args["presentation_plan"]["annotations"][2]["upper"] = "2025-12"
    elif damage == "label":
        item["label"] = "a" * 81
    elif damage == "duplicate":
        args["presentation_plan"]["annotations"].append(copy.deepcopy(item))
    elif damage == "guide":
        args["presentation_plan"]["guide_steps"][0]["annotation_id"] = "missing"
    elif damage == "value":
        item["value"] = 99
    elif damage == "null_bound":
        args["presentation_plan"]["annotations"][2]["lower"] = None
    else:
        item["tuple"] = [True]
    before = set(artifacts.backend.root_dir.rglob("*"))
    result = await _add_chart(args, ctx)
    assert result.get("error_kind") == "invalid_chart_expression", result
    assert await canvas.get_canvas_by_session(ctx.session_id) is None
    assert set(artifacts.backend.root_dir.rglob("*")) == before
