"""Native prepared frames preserve observed scope through admission and static export."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import date, timedelta

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
from test_chart_time_window import time_prepared
from test_export_snapshot import _request

RAW = b"month,region,revenue,cost\n2026-03,A,12.123456,5\n2026-01,B,-5,2\n2026-01,A,,3\n2026-02,B,0.000012345,4\n2026-02,A,2,7\n"


def frame_args(value, *, static=None):
    *_, args, _, _, raw = value
    bid = next(item["id"] for item in catalog(raw)["candidates"] if item["kind"] == "frame")
    return {
        **{key: cell for key, cell in args.items() if key not in {"force_kind", "series", "presentation_plan"}},
        "title": "Prepared monthly relationships",
        "views": [
            {"id": "relationship", "title": "Revenue and cost", "hint": "compare", "force_kind": "scatter", "series": ["revenue", "cost"]},
            {"id": "detail", "title": "Original observations", "hint": "raw", "force_kind": "table", "series": ["revenue", "cost"]},
        ],
        "layout": "primary_aside",
        "presentation_plan": {
            "version": 1,
            "interactions": [
                {
                    "preset": "frame_player",
                    "view_ids": ["relationship", "detail"],
                    "binding_id": bid,
                    **({"static_frame": static} if static is not None else {}),
                }
            ],
        },
    }


@pytest.mark.parametrize("frozen_style", [False, True])
@pytest.mark.parametrize("static", [None, "2026-01", "2026-02"])
async def test_frames_authorized_canvas_projection_and_frozen_export(prepared, static, frozen_style):
    value = await time_prepared(prepared, RAW)
    _, canvas, snapshots, ctx, _, data, _, _ = value
    args = frame_args(value, static=static)
    before = copy.deepcopy(args)
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    assert args == before
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    root = section.payload["spec"]
    validate_stored_composition(root)
    plan = root["presentation_plan"]
    bid = plan["interactions"][0]["binding_id"]
    assert plan["interactions"][0]["static_frame"] == (static or "2026-03")
    binding = plan["bindings"][bid]
    assert binding["frame_keys"] == ["2026-01", "2026-02", "2026-03"]
    assert binding["entity_values"] == ["A", "B"]
    assert binding["axis_domains"]["relationship"]["domains"]["x"][0] < -5
    assert len(root["views"][0]["spec"]["rows"]) == 5
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
    assert merged["spec"]["presentation_plan"] == plan
    changed["presentation_plan"]["interactions"][0]["static_frame"] = "2026-02" if static != "2026-02" else "2026-01"
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
    denied, _, _, _ = await project(value, section, denied=[data.id])
    assert denied["spec"]["presentation_plan"]["bindings"][bid] == {"kind": "frame", "availability": "unavailable"}
    validate_projected_notes_plan(denied["spec"]["presentation_plan"])

    async def projection(_canvas, sections):
        projected, _, _, _ = await project(value, sections[0])
        return [
            replace(sections[0], payload={key: val for key, val in projected.items() if key not in {"id", "kind", "rev", "parent_id", "order_index"}})
        ]

    request = replace(_request(), requested_by="u", locale="en-US")
    if frozen_style:
        request = replace(request, theme=build_chart_style_export_theme_v2(_effective_v2(), print_theme=current_print_theme_snapshot()))
    candidate = await snapshots.build_candidate(canvas_id=section.canvas_id, request=request, project_sections=projection)
    frozen_root = json.loads(candidate.payload_json)["sections"][0]["payload"]["spec"]
    if frozen_style:
        for view in frozen_root["views"]:
            assert {row["month"] for row in view["spec"]["rows"]} == {static or "2026-03"}
            assert "frozen_chart_presentation" in view
    snapshot = await snapshots.commit_candidate(
        candidate,
        expected_projection_token=candidate.content_projection.projection_token,
        expected_surface_token=candidate.surface_projection.surface_token,
    )
    visuals = [
        block
        for block in iter_export_blocks(normalize_export_snapshot(snapshot, asset_content_hashes={data.id: "sha256:" + "a" * 64}).blocks)
        if block.block_kind == "visual"
    ]
    assert len(visuals) == 2
    for block in visuals:
        frozen = block.content["payload"]["spec"]
        assert {row["month"] for row in frozen["rows"]} == {static or "2026-03"}
        assert frozen["subtitle"].count("Time frame:") == 1
        assert frozen["row_count"] == len(frozen["rows"])
        assert "presentation_plan" not in frozen
    assert visuals[0].content["payload"]["spec"]["_export_axis_domains"] == binding["axis_domains"]["relationship"]
    assert (await canvas.list_sections(section.canvas_id))[0].payload["spec"] == root


@pytest.mark.parametrize("damage", ["foreign", "unknown", "scene", "duplicate_player", "wrong_binding", "bad_frame", "unimplemented_kind"])
async def test_invalid_frame_declarations_write_nothing(prepared, damage):
    value = await time_prepared(prepared, RAW)
    artifacts, canvas, _, ctx, _, _, _, raw = value
    args = frame_args(value)
    item = args["presentation_plan"]["interactions"][0]
    if damage == "foreign":
        item["view_ids"].append("another_root")
    elif damage == "unknown":
        item["binding_id"] = "frame.invented"
    elif damage == "scene":
        item["speed"] = 20
    elif damage == "duplicate_player":
        args["presentation_plan"]["interactions"].append(copy.deepcopy(item))
    elif damage == "wrong_binding":
        item["binding_id"] = next(item["id"] for item in catalog(raw)["candidates"] if item["kind"] == "row")
    elif damage == "bad_frame":
        item["static_frame"] = "2026-04"
    else:
        args["views"][0]["force_kind"] = "line"
    before = set(artifacts.backend.root_dir.rglob("*"))
    result = await _add_chart(args, ctx)
    assert result.get("error_kind") == "invalid_chart_expression", result
    assert await canvas.get_canvas_by_session(ctx.session_id) is None
    assert set(artifacts.backend.root_dir.rglob("*")) == before


async def test_frame_saved_descriptors_and_export_only_fields_cannot_be_forged(prepared):
    value = await time_prepared(prepared, RAW)
    _, canvas, _, ctx, _, _, _, _ = value
    result = await _add_chart(frame_args(value), ctx)
    root = (await canvas.list_sections(result["canvas_id"]))[0].payload["spec"]
    for damage in ("bounds", "frame_order", "entities", "observations", "export_field", "coverage"):
        forged = copy.deepcopy(root)
        binding = next(iter(forged["presentation_plan"]["bindings"].values()))
        if damage == "bounds":
            binding["axis_domains"]["relationship"]["domains"]["x"] = [0, 1]
        elif damage == "frame_order":
            binding["frame_keys"].reverse()
        elif damage == "entities":
            binding["entity_values"].append("C")
        elif damage == "observations":
            forged["views"][0]["spec"]["rows"][0]["revenue"] = 100000
        elif damage == "export_field":
            forged["views"][0]["spec"]["_export_frame_entities"] = {"key": "region", "values": ["A"]}
        else:
            binding["coverage"]["artifact_rows"]["total"] = None
        with pytest.raises(ChartExpressionError):
            validate_stored_composition(forged)


async def test_excessive_frames_have_no_catalog_offer(prepared):
    raw = ("month,region,revenue,cost\n" + "\n".join(f"{date(2020, 1, 1) + timedelta(days=index)},A,1,2" for index in range(121))).encode()
    assert not any(item["kind"] == "frame" for item in catalog(raw)["candidates"])


async def test_six_views_retain_all_120_prepared_frames_without_table_preview_truncation(prepared):
    raw = (
        "month,region,revenue,cost\n"
        + "\n".join(
            f"{date(2026, 1, 1) + timedelta(days=frame)},E{entity},{frame + entity},{entity + 1}" for frame in range(120) for entity in range(10)
        )
    ).encode()
    value = await time_prepared(prepared, raw)
    _, canvas, _, ctx, _, _, _, _ = value
    args = frame_args(value)
    args["views"] = [{**args["views"][0], "id": f"plot{index}"} for index in range(5)] + [args["views"][1]]
    args["layout"] = "grid2"
    args["presentation_plan"]["interactions"][0]["view_ids"] = [view["id"] for view in args["views"]]
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    spec = (await canvas.list_sections(result["canvas_id"]))[0].payload["spec"]
    assert all(len(view["spec"]["rows"]) == 1200 for view in spec["views"])
    assert len(next(iter(spec["presentation_plan"]["bindings"].values()))["frame_keys"]) == 120
    validate_stored_composition(spec)
