"""Native scale groups are derived atomically and survive protected exports."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from bowhead_core.canvas_tools import _add_chart
from bowhead_core.chart.expression import ChartExpressionError
from bowhead_core.chart.presentation_plan import ChartPlanError, parse_plan_input, validate_projected_notes_plan
from bowhead_core.chart.shared_scales import compile_scale_groups
from bowhead_core.export_ir import iter_export_blocks, normalize_export_snapshot
from bowhead_core.export_visual_identity import compute_visual_input_sha256
from bowhead_core.source_access_projection import SourcePatchError, merge_viewer_patch_preserving_provenance
from test_chart_evidence_plan import prepared as prepared
from test_chart_evidence_plan import project
from test_chart_time_window import time_prepared, window_args
from test_export_snapshot import _request
from test_export_visual_identity_projection_invariance import _THEME


def scale_args(value):
    args = window_args(value)
    args["views"][1].update(force_kind="line", hint="trend", series=["revenue"])
    args["presentation_plan"] = {"version": 1, "scale_groups": [{"view_ids": ["trend", "detail"], "axis": "y"}]}
    return args


async def test_shared_scale_tool_projection_patch_history_and_static_domains(prepared):
    value = await time_prepared(prepared)
    _, canvas, snapshots, ctx, _, data, _, _ = value
    result = await _add_chart(scale_args(value), ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    spec = section.payload["spec"]
    group = spec["presentation_plan"]["scale_groups"][0]
    assert group["measure_keys"] == ["revenue"]
    assert group["domain"][0] < -5 and group["domain"][1] > 12.12345
    safe, aliases, access, validity = await project(value, section)
    validate_projected_notes_plan(safe["spec"]["presentation_plan"])
    changed = copy.deepcopy(safe["spec"])
    changed["views"][0]["spec"]["show_data_labels"] = True
    patch = merge_viewer_patch_preserving_provenance(
        section.to_report_section(),
        {"spec": changed},
        alias_map=aliases,
        access_by_identity=access,
        evidence_validity_by_digest=validity,
        section_kind="chart",
        section_id=section.id,
        allow_label_edit=True,
    )
    updated = await canvas.patch_section(section_id=section.id, patch=patch, expected_rev=section.rev)
    assert updated.payload["spec"]["presentation_plan"]["scale_groups"] == [group]
    assert updated.payload["spec"]["views"][1] == spec["views"][1]
    forged = copy.deepcopy(changed)
    forged["presentation_plan"]["scale_groups"][0]["domain"][1] = 100000
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
    incompatible = copy.deepcopy(updated.payload["spec"])
    incompatible["views"][0]["spec"]["kind"] = "hbar"
    with pytest.raises(ChartExpressionError):
        await canvas.patch_section(section_id=section.id, patch={"spec": incompatible}, expected_rev=updated.rev)
    assert (await canvas.list_sections(result["canvas_id"]))[0].rev == updated.rev
    denied, _, _, _ = await project(value, section, denied=[data.id])
    assert denied["spec"]["presentation_plan"]["scale_groups"] == [group]
    validate_projected_notes_plan(denied["spec"]["presentation_plan"])

    # Source access can disable browsing without changing the saved visual scale.
    def identity(chart):
        return compute_visual_input_sha256(section_kind="chart", payload={"spec": chart}, locale="en-US", theme=_THEME)

    assert identity(spec) == identity(safe["spec"])
    neutral = copy.deepcopy(denied["spec"])
    neutral["presentation_plan"] = spec["presentation_plan"]
    assert identity(neutral) == identity(denied["spec"])
    edited_domain = copy.deepcopy(neutral)
    edited_domain["presentation_plan"]["scale_groups"][0]["domain"][1] += 1
    assert identity(neutral) != identity(edited_domain)

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
    leaves = [block.content["payload"]["spec"] for block in blocks if block.block_kind == "visual"]
    assert len(leaves) == 2
    assert all(leaf["_export_axis_domains"] == {"encoding": "line", "domains": {"y": group["domain"]}} for leaf in leaves)
    assert all(len(leaf["rows"]) == 4 for leaf in leaves)


@pytest.mark.parametrize("damage", ["foreign", "bounds", "duplicate", "singleton", "wrong_axis", "different_key", "ranked", "kind", "missing"])
async def test_invalid_scale_plans_reject_before_any_write(prepared, damage):
    value = await time_prepared(prepared)
    artifacts, canvas, _, ctx, _, _, _, _ = value
    args = scale_args(value)
    group = args["presentation_plan"]["scale_groups"][0]
    if damage == "foreign":
        group["view_ids"][1] = "foreign"
    elif damage == "bounds":
        group["min"] = 0
    elif damage == "duplicate":
        args["presentation_plan"]["scale_groups"].append(copy.deepcopy(group))
    elif damage == "singleton":
        group["view_ids"] = ["trend"]
    elif damage == "wrong_axis":
        group["axis"] = "x"
    elif damage == "different_key":
        args["views"][1]["series"] = ["cost"]
    elif damage == "ranked":
        args["views"][1]["top_n"] = 2
    elif damage == "kind":
        args["views"][1].update(force_kind="bar", hint="compare")
    else:
        args["presentation_plan"]["scale_groups"] = []
    before = set(artifacts.backend.root_dir.rglob("*"))
    result = await _add_chart(args, ctx)
    assert "error" in result, result
    assert await canvas.get_canvas_by_session(ctx.session_id) is None
    assert set(artifacts.backend.root_dir.rglob("*")) == before


def root(kind="bar"):
    chart = {
        "kind": kind,
        "title": "Observed",
        "row_count": 2,
        "columns": [
            {"key": "region", "header": "Region", "type": "categorical"},
            {"key": "a", "header": "A", "type": "numeric", "unit": "USD"},
            {"key": "b", "header": "B", "type": "numeric", "unit": "USD"},
        ],
        "rows": [{"region": "A", "a": 10, "b": -3}, {"region": "B", "a": 20, "b": None}],
    }
    return {"views": [{"id": "first", "spec": copy.deepcopy(chart)}, {"id": "second", "spec": copy.deepcopy(chart)}]}


def compile_group(spec, axis="y"):
    return compile_scale_groups([{"view_ids": ["first", "second"], "axis": axis}], spec)[0]


def test_domains_preserve_negative_stacks_zero_and_fraction_positions():
    wide = root("stacked_bar")
    group = compile_group(wide)
    assert group["domain"][0] < -3 and group["domain"][1] > 20
    for view in wide["views"]:
        for column in view["spec"]["columns"][1:]:
            column.update(unit="%", format="percent", percent_scale="fraction")
        view["spec"]["rows"] = [{"region": "A", "a": 0.2, "b": 0.3}, {"region": "B", "a": 0.1, "b": None}]
    group = compile_group(wide)
    assert group["domain"][0] == 0 and 0.5 < group["domain"][1] < 1
    wide["views"][1]["spec"]["columns"][1]["percent_scale"] = "points"
    with pytest.raises(ChartPlanError):
        compile_group(wide)


def test_scatter_shares_x_without_confusing_its_y_unit_or_missing_pairs():
    spec = root("scatter")
    spec["views"][1]["spec"]["columns"][2]["unit"] = "people"
    group = compile_group(spec, "x")
    assert group["measure_keys"] == ["a"]
    assert group["domain"][0] < 10 < group["domain"][1] < 20
    with pytest.raises(ChartPlanError):
        compile_group(spec, "y")


def test_scale_input_is_closed_and_budgeted():
    assert parse_plan_input({"version": 1, "scale_groups": [{"view_ids": ["first", "second"], "axis": "y"}]})["scale_groups"]
    with pytest.raises(ChartPlanError):
        parse_plan_input({"version": 1, "scale_groups": [{"view_ids": ["first", "second"], "axis": "y", "domain": [0, 10]}]})
    spec = root("line")
    for view in spec["views"]:
        view["spec"]["rows"] = [{"region": "A", "a": None, "b": None}, {"region": "B", "a": None, "b": None}]
    with pytest.raises(ChartPlanError):
        compile_group(spec)
