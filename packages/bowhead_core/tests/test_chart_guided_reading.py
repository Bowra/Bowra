"""Reading steps remain one source-protected Canvas figure and ordered static evidence."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import jsonschema
import pytest
from bowhead_core.canvas_tools import _add_chart, _add_chart_schema
from bowhead_core.chart.presentation_plan import validate_projected_notes_plan
from bowhead_core.export_ir import iter_export_blocks, normalize_export_snapshot
from bowhead_core.source_access_projection import SourcePatchError, merge_viewer_patch_preserving_provenance
from test_chart_evidence_plan import prepared as prepared
from test_chart_evidence_plan import project
from test_chart_time_window import time_prepared, window_args
from test_export_snapshot import _request


def guide_args(value, *, window=False):
    args = window_args(value)
    if not window:
        args["presentation_plan"] = {"version": 1}
    args["presentation_plan"].setdefault("interactions", []).append({"preset": "guided_steps"})
    args["presentation_plan"]["guide_steps"] = [
        {"title": "Read the revenue trend", "view_id": "trend"},
        {"title": "Inspect the original observations <exact>", "view_id": "detail"},
    ]
    return args


@pytest.mark.parametrize("window", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
async def test_guides_tool_source_patch_snapshot_and_ordered_ir(prepared, window, explicit):
    value = await time_prepared(prepared)
    _, canvas, snapshots, ctx, _, data, _, _ = value
    args = guide_args(value, window=window)
    if not explicit:
        args["presentation_plan"]["interactions"] = [item for item in args["presentation_plan"]["interactions"] if item["preset"] != "guided_steps"]
        if not args["presentation_plan"]["interactions"]:
            args["presentation_plan"].pop("interactions")
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    spec = section.payload["spec"]
    plan = spec["presentation_plan"]
    assert plan["guide_steps"][1]["view_id"] == "detail"
    assert "/presentation_plan/guide_steps/1/title" in plan["text_dependencies"]
    safe, aliases, access, validity = await project(value, section)
    validate_projected_notes_plan(safe["spec"]["presentation_plan"])
    changed = copy.deepcopy(safe["spec"])
    changed["views"][0]["spec"]["show_data_labels"] = True
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
    updated = await canvas.patch_section(section_id=section.id, patch=merged, expected_rev=section.rev)
    assert updated.payload["spec"]["presentation_plan"]["guide_steps"] == plan["guide_steps"]
    assert updated.payload["spec"]["views"][1] == spec["views"][1]
    forged = copy.deepcopy(changed)
    forged["presentation_plan"]["guide_steps"][0]["title"] = "Invented conclusion"
    with pytest.raises(SourcePatchError):
        merge_viewer_patch_preserving_provenance(
            section.to_report_section(),
            {"spec": forged},
            alias_map=aliases,
            access_by_identity=access,
            evidence_validity_by_digest=validity,
            section_kind="chart",
            section_id=section.id,
            allow_label_edit=True,
        )
    denied, aliases, access, validity = await project(value, section, denied=[data.id])
    assert all(step["title"] == "" for step in denied["spec"]["presentation_plan"]["guide_steps"])
    validate_projected_notes_plan(denied["spec"]["presentation_plan"])
    restored = merge_viewer_patch_preserving_provenance(
        section.to_report_section(),
        {"spec": denied["spec"]},
        alias_map=aliases,
        access_by_identity=access,
        evidence_validity_by_digest=validity,
        section_kind="chart",
        section_id=section.id,
        allow_label_edit=True,
    )
    assert restored["spec"]["presentation_plan"]["guide_steps"] == plan["guide_steps"]

    async def projection(_canvas, sections):
        projected, _, _, _ = await project(value, sections[0])
        return [
            replace(sections[0], payload={key: val for key, val in projected.items() if key not in {"id", "kind", "rev", "parent_id", "order_index"}})
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
    assert len([block for block in blocks if block.block_kind == "visual"]) == 2
    prose = [block.content for block in blocks if block.block_kind != "visual"]
    serialized = str(prose)
    assert serialized.index("Read the revenue trend") < serialized.index("Inspect the original observations")
    # Guide titles are literal text; the output renderer escapes markup.
    exact_step = next(content for content in prose if "Inspect the original observations" in content.get("text", ""))
    assert "Inspect the original observations <exact>" in exact_step["text"]
    assert "markdown" not in exact_step
    assert all(len(block.content["payload"]["spec"]["rows"]) == 4 for block in blocks if block.block_kind == "visual")


@pytest.mark.parametrize("damage", ["foreign", "missing_steps", "extra_preset", "empty", "too_many", "unicode", "annotation", "scene", "singleton"])
async def test_invalid_guides_reject_before_artifact_or_canvas_writes(prepared, damage):
    value = await time_prepared(prepared)
    artifacts, canvas, _, ctx, _, _, _, _ = value
    args = guide_args(value)
    plan = args["presentation_plan"]
    if damage == "foreign":
        plan["guide_steps"][0]["view_id"] = "another_root"
    elif damage == "missing_steps":
        plan.pop("guide_steps")
    elif damage == "extra_preset":
        plan["interactions"].append({"preset": "guided_steps"})
    elif damage == "empty":
        plan["guide_steps"] = []
    elif damage == "too_many":
        plan["guide_steps"] *= 3
    elif damage == "unicode":
        plan["guide_steps"][0]["title"] = "🦋" * 81
    elif damage == "annotation":
        plan["guide_steps"][0]["annotation_id"] = "not_admitted"
    elif damage == "scene":
        plan["guide_steps"][0]["frame"] = "2026-01"
    else:
        args["views"] = args["views"][:1]
        args["layout"] = {"view": "trend"}
        plan["guide_steps"] = plan["guide_steps"][:1]
    before = set(artifacts.backend.root_dir.rglob("*"))
    result = await _add_chart(args, ctx)
    assert result.get("error_kind") == "invalid_chart_expression", result
    assert "canvas_id" not in result
    assert set(artifacts.backend.root_dir.rglob("*")) == before
    assert await canvas.get_canvas_by_session(ctx.session_id) is None


async def test_note_only_loss_redacts_guides_without_disabling_independent_data(prepared):
    value = await time_prepared(prepared)
    _, canvas, snapshots, ctx, original_args, data, note, _ = value
    args = guide_args(value, window=True)
    args["presentation_plan"]["evidence_notes"] = original_args["presentation_plan"]["evidence_notes"]
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    canonical = section.payload["spec"]
    denied, _, _, _ = await project(value, section, denied=[note.id])
    safe = denied["spec"]
    validate_projected_notes_plan(safe["presentation_plan"])
    assert safe["presentation_plan"]["dataset"]["availability"] == "available"
    assert safe["presentation_plan"]["evidence_notes"][0]["availability"] == "unavailable"
    assert all(step["title"] == "" for step in safe["presentation_plan"]["guide_steps"])
    assert safe["title"] == ""
    assert all(view["spec"]["title"] == "" for view in safe["views"])
    assert [view["spec"]["rows"] for view in safe["views"]] == [view["spec"]["rows"] for view in canonical["views"]]
    assert all(binding["availability"] == "available" for binding in safe["presentation_plan"]["bindings"].values())

    async def projection(_canvas, sections):
        payload, _, _, _ = await project(value, sections[0], denied=[note.id])
        return [
            replace(sections[0], payload={key: val for key, val in payload.items() if key not in {"id", "kind", "rev", "parent_id", "order_index"}})
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
    assert len([block for block in blocks if block.block_kind == "visual"]) == 2
    assert "Read the revenue trend" not in str(blocks)
    assert "Revenue includes returns." not in str(blocks)


@pytest.mark.parametrize("encoded", [False, True])
async def test_existing_layout_alias_and_implicit_guide_declaration_compile_to_one_native_root(prepared, encoded):
    value = await time_prepared(prepared)
    _, canvas, _, ctx, _, _, _, _ = value
    args = guide_args(value)
    args["presentation_plan"].pop("interactions")
    args["views"].append({"id": "cost", "title": "Cost", "hint": "compare", "force_kind": "bar", "series": ["cost"]})
    layout = {"type": "primary_aside", "children": [{"view": "trend"}, {"type": "stack", "children": [{"view": "cost"}, {"view": "detail"}]}]}
    args["layout"] = json.dumps(layout) if encoded else layout
    jsonschema.validate(args, _add_chart_schema().input_schema)
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    sections = await canvas.list_sections(result["canvas_id"])
    assert len(sections) == 1
    spec = sections[0].payload["spec"]
    assert spec["layout"]["type"] == "split"
    assert [view["id"] for view in spec["views"]] == ["trend", "cost", "detail"]
    assert spec["presentation_plan"]["interactions"] == [{"preset": "guided_steps"}]
    assert all(len(view["spec"]["rows"]) == 4 for view in spec["views"])


async def test_unknown_binding_returns_authorized_correction_ids_without_writing(prepared):
    value = await time_prepared(prepared)
    artifacts, canvas, _, ctx, _, _, _, _ = value
    args = guide_args(value, window=True)
    offered = args["presentation_plan"]["interactions"][0]["binding_id"]
    args["presentation_plan"]["view_bindings"] = dict.fromkeys(("trend", "detail"), "invented")
    for interaction in args["presentation_plan"]["interactions"]:
        if "binding_id" in interaction:
            interaction["binding_id"] = "invented"
    before = set(artifacts.backend.root_dir.rglob("*"))
    result = await _add_chart(args, ctx)
    assert result["code"] == "binding_unknown"
    assert result["corrective_ids"] == [offered]
    assert await canvas.get_canvas_by_session(ctx.session_id) is None
    assert set(artifacts.backend.root_dir.rglob("*")) == before


async def test_root_reading_steps_relocate_without_guessing_or_conflicts(prepared):
    value = await time_prepared(prepared)
    args = guide_args(value)
    declared = args["presentation_plan"].pop("guide_steps")
    args["presentation_plan"].pop("interactions", None)
    args["guide_steps"] = declared
    result = await _add_chart(args, value[3])
    assert "error" not in result, result
    spec = (await value[1].list_sections(result["canvas_id"]))[0].payload["spec"]
    assert spec["presentation_plan"]["guide_steps"] == declared
    before = len(await value[1].list_sections(result["canvas_id"]))
    args["presentation_plan"]["guide_steps"] = [{"title": "Different", "view_id": declared[0]["view_id"]}]
    invalid = await _add_chart(args, value[3])
    assert invalid.get("error_kind") == "invalid_chart_expression"
    assert len(await value[1].list_sections(result["canvas_id"])) == before
