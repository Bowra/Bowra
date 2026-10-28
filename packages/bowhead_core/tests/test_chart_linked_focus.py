"""Same-result linkage retains exact grain, source access and one Canvas root."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from bowhead_core.canvas_tools import _add_chart, _parse_csv_with_types
from bowhead_core.chart.binding_catalog import build_binding_catalog, typed_tuple
from bowhead_core.chart.expression import ChartExpressionError, validate_stored_composition
from bowhead_core.chart.linked_focus import compile_focus
from bowhead_core.chart.presentation_plan import ChartPlanError, validate_projected_notes_plan
from bowhead_core.export_ir import iter_export_blocks, normalize_export_snapshot
from bowhead_core.source_access_projection import SourcePatchError, merge_viewer_patch_preserving_provenance
from test_chart_evidence_plan import prepared as prepared
from test_chart_evidence_plan import project
from test_export_snapshot import _request


def catalog(raw: bytes):
    rows, columns = _parse_csv_with_types(raw)
    return build_binding_catalog(columns, rows, raw=raw)


def focus_args(args, raw):
    bid = next(candidate["id"] for candidate in catalog(raw)["candidates"] if candidate["kind"] == "entity")
    return {
        **{key: value for key, value in args.items() if key not in {"force_kind", "series", "presentation_plan"}},
        "title": "Regional comparison",
        "views": [
            {"id": "comparison", "title": "Revenue by region", "hint": "compare", "force_kind": "bar", "series": ["revenue"]},
            {"id": "detail", "title": "Regional observations", "hint": "raw", "force_kind": "table", "series": ["revenue"]},
        ],
        "layout": {"type": "split", "children": [{"view": "comparison"}, {"view": "detail"}]},
        "presentation_plan": {
            "version": 1,
            "view_bindings": {"comparison": bid, "detail": bid},
            "interactions": [
                {"preset": "linked_focus", "from_view": "comparison", "to_views": ["detail"], "binding_id": bid},
                {"preset": "linked_focus", "from_view": "detail", "to_views": ["comparison"], "binding_id": bid},
            ],
        },
    }


def test_catalog_uses_unique_typed_grain_and_bounded_prepared_time():
    value = catalog(b"month,region,revenue\n2026-01,A,12\n2026-02,A,21\n2026-01,B,9\n")
    assert {candidate["kind"] for candidate in value["candidates"]} == {"row", "entity", "time", "frame"}
    assert len(json.dumps(value, ensure_ascii=False).encode()) <= 4_000
    assert value["artifact_rows"] == 3
    assert typed_tuple({"key": 1}, ["key"]) != typed_tuple({"key": "1"}, ["key"])
    assert typed_tuple({"key": 1}, ["key"]) == typed_tuple({"key": 1.0}, ["key"])
    assert typed_tuple({"key": None}, ["key"]) != typed_tuple({"key": ""}, ["key"])
    duplicate = catalog(b"region,revenue\nA,12\nA,21\n")
    assert duplicate["candidates"] == []
    assert "Duplicate" in duplicate["omission_reason"]
    ambiguous = catalog(b"month,region,revenue\n1,A,12\n2,A,21\n")
    assert not any(candidate["kind"] in {"time", "frame"} for candidate in ambiguous["candidates"])
    many = catalog(("region,revenue\n" + "\n".join(f"R{i},1" for i in range(150))).encode())
    assert many["artifact_rows"] == 150
    assert len(many["candidates"]) <= 12


async def test_native_tool_compiles_focus_and_catalog_survives_authorized_context_refresh(prepared):
    artifacts, canvas, _, ctx, args, data, _, raw = prepared
    result = await _add_chart(focus_args(args, raw), ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    plan = section.payload["spec"]["presentation_plan"]
    assert plan["dataset"]["content_digest"] == hashlib.sha256(raw).hexdigest()
    assert plan["evidence_notes"] == []
    binding = next(iter(plan["bindings"].values()))
    assert binding["observation_keys"] == ["region"]
    assert binding["coverage"]["artifact_rows"] == {"available": 2, "total": 2}
    assert binding["coverage"]["views"] == {"comparison": {"retained_rows": 2}, "detail": {"retained_rows": 2}}
    projected, _, _, _ = await project(prepared, section)
    reordered = json.loads(json.dumps(projected["spec"]["presentation_plan"], sort_keys=True))
    validate_projected_notes_plan(reordered)
    again = await _add_chart(focus_args(args, raw), ctx)
    assert again["section_id"] == section.id
    assert len(await canvas.list_sections(section.canvas_id)) == 1
    refs = ctx._ν15_allowlist
    invocation = SimpleNamespace(
        tool_name="bi_query", call_id="call_catalog", result={"results": [{"artifact_id": data.id, "chart_catalog": catalog(raw)}]}
    )
    await refs.refresh_from_stores(
        canvas_store=canvas,
        artifact_store=artifacts,
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        canvas_id=section.canvas_id,
        tool_invocations=[invocation],
    )
    refs.add("data", data.id, origin="Updated metadata")
    xml = refs.to_prompt_xml()
    assert '<chart_catalog data_ref="1">' in xml
    assert next(iter(plan["bindings"])) in xml
    assert "Updated metadata" in xml


@pytest.mark.parametrize(
    "damage,code",
    [
        ("unknown", "binding_unknown"),
        ("self", "target_invalid"),
        ("unused", "target_invalid"),
        ("kpi", "binding_ineligible"),
        ("single", "target_invalid"),
    ],
)
async def test_bad_focus_fails_before_canvas_or_artifact_writes(prepared, damage, code):
    artifacts, canvas, _, ctx, args, _, _, raw = prepared
    value = focus_args(args, raw)
    plan = value["presentation_plan"]
    if damage == "unknown":
        plan["view_bindings"] = dict.fromkeys(plan["view_bindings"], "entity.stale")
        for interaction in plan["interactions"]:
            interaction["binding_id"] = "entity.stale"
    if damage == "self":
        plan["interactions"][0]["to_views"] = ["comparison"]
    if damage == "unused":
        plan["view_bindings"]["unused"] = next(iter(plan["view_bindings"].values()))
    if damage == "kpi":
        value["views"][1]["force_kind"] = "kpi"
    if damage == "single":
        value["views"] = value["views"][:1]
        value.pop("layout")
    files_before = set(artifacts.backend.root_dir.rglob("*"))
    result = await _add_chart(value, ctx)
    assert result["code"] == code, result
    assert await canvas.get_canvas_by_session(ctx.session_id) is None
    assert set(artifacts.backend.root_dir.rglob("*")) == files_before


async def test_focus_projection_patch_and_static_export_preserve_full_evidence(prepared):
    _, canvas, snapshots, ctx, args, data, _, raw = prepared
    result = await _add_chart(focus_args(args, raw), ctx)
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    safe, aliases, access, validity = await project(prepared, section)
    bid = next(iter(safe["spec"]["presentation_plan"]["bindings"]))
    assert safe["spec"]["presentation_plan"]["bindings"][bid]["availability"] == "available"
    incoming = copy.deepcopy(safe)
    incoming["spec"]["views"][0]["spec"]["kind"] = "hbar"
    incoming["spec"]["layout"] = {"type": "grid2", "children": [{"view": "detail"}, {"view": "comparison"}]}
    restored = merge_viewer_patch_preserving_provenance(
        section.to_report_section(),
        {"spec": incoming["spec"]},
        alias_map=aliases,
        access_by_identity=access,
        evidence_validity_by_digest=validity,
        section_kind="chart",
        section_id=section.id,
        allow_label_edit=True,
    )
    validate_stored_composition(restored["spec"])
    assert restored["spec"]["layout"] == incoming["spec"]["layout"]
    assert restored["spec"]["presentation_plan"] == section.payload["spec"]["presentation_plan"]
    invalid_layout = copy.deepcopy(incoming)
    invalid_layout["spec"]["layout"]["children"][0]["view"] = "outside_root"
    with pytest.raises(SourcePatchError):
        merge_viewer_patch_preserving_provenance(
            section.to_report_section(),
            {"spec": invalid_layout["spec"]},
            alias_map=aliases,
            access_by_identity=access,
            evidence_validity_by_digest=validity,
            section_kind="chart",
            section_id=section.id,
            allow_label_edit=True,
        )
    forged = copy.deepcopy(incoming)
    forged["spec"]["presentation_plan"]["bindings"][bid]["dimension_keys"] = ["revenue"]
    with pytest.raises(SourcePatchError):
        merge_viewer_patch_preserving_provenance(
            section.to_report_section(),
            {"spec": forged["spec"]},
            alias_map=aliases,
            access_by_identity=access,
            evidence_validity_by_digest=validity,
            section_kind="chart",
            section_id=section.id,
            allow_label_edit=True,
        )
    incompatible = copy.deepcopy(restored["spec"])
    incompatible["views"][0]["spec"]["kind"] = "pie"
    with pytest.raises(ChartExpressionError):
        validate_stored_composition(incompatible)
    denied, _, _, _ = await project(prepared, section, denied=[data.id])
    assert denied["spec"]["presentation_plan"]["bindings"][bid] == {"kind": "entity", "availability": "unavailable"}
    assert denied["spec"]["views"][0]["spec"]["rows"] == section.payload["spec"]["views"][0]["spec"]["rows"]

    async def projection(_canvas, sections):
        projected, _, _, _ = await project(prepared, sections[0])
        return [
            replace(
                sections[0], payload={key: value for key, value in projected.items() if key not in {"id", "kind", "rev", "parent_id", "order_index"}}
            )
        ]

    snapshot = await snapshots.build_candidate(
        canvas_id=section.canvas_id, request=replace(_request(), requested_by="u"), project_sections=projection
    )
    committed = await snapshots.commit_candidate(
        snapshot,
        expected_projection_token=snapshot.content_projection.projection_token,
        expected_surface_token=snapshot.surface_projection.surface_token,
    )
    blocks = list(iter_export_blocks(normalize_export_snapshot(committed, asset_content_hashes={data.id: "sha256:" + "a" * 64}).blocks))
    assert any(block.block_kind == "visual" for block in blocks)
    assert all("selection" not in json.dumps(asdict(block)) for block in blocks)


@pytest.mark.parametrize("omit_binding", [False, True])
def test_model_type_overrides_cannot_create_source_binding_eligibility(omit_binding):
    raw = b"identifier,value\n10,12\n20,21\n"
    rows, columns = _parse_csv_with_types(raw, type_overrides={"identifier": "categorical"})
    offered = build_binding_catalog(columns, rows, raw=raw)
    bid = next(candidate["id"] for candidate in offered["candidates"] if candidate["kind"] == "entity")
    child = {"kind": "table", "columns": [{"key": column.key, "type": column.type} for column in columns], "rows": rows}
    root = {"views": [{"id": "primary", "spec": child}, {"id": "detail", "spec": child}]}
    plan = {
        "view_bindings": {"primary": bid, "detail": bid},
        "interactions": [{"preset": "linked_focus", "from_view": "primary", "to_views": ["detail"], "binding_id": bid}],
    }
    if omit_binding:
        plan.pop("view_bindings")
        plan["interactions"][0].pop("binding_id")
    with pytest.raises(ChartPlanError) as caught:
        compile_focus(plan, root, columns, rows, raw=raw)
    assert caught.value.code == "binding_unknown"
