"""Artifact-backed notes preserve native chart and Canvas boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from bowhead_core._ref_allowlist import RefAllowlist
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.canvas_tools import _add_chart
from bowhead_core.chart.expression import ChartExpressionError, validate_stored_composition
from bowhead_core.chart.presentation_plan import ChartPlanError, parse_plan_input, verify_saved_plan_evidence
from bowhead_core.export_ir import iter_export_blocks, normalize_export_snapshot
from bowhead_core.export_snapshot import ExportSnapshotStore, SnapshotError
from bowhead_core.results_report_relations import project_report_relations
from bowhead_core.session_store import SessionStore
from bowhead_core.source_access_projection import (
    SourcePatchError,
    ViewerSourceAliasMap,
    merge_viewer_patch_preserving_provenance,
    project_sources_for_viewer,
)
from test_add_chart_validation import _StubCtx
from test_export_snapshot import _request


@pytest.fixture
async def prepared(tmp_path: Path):
    db = tmp_path / "canvas.db"
    artifacts = ArtifactStore(db, LocalBackend(tmp_path / "files"))
    canvas = CanvasStore(db)
    snapshots = ExportSnapshotStore(db)
    await SessionStore(db).initialize()
    await artifacts.initialize()
    await canvas.initialize()
    await snapshots.initialize()
    raw = b"region,revenue\nA,12\nB,21\n"
    data = await artifacts.put_bytes(content=raw, filename="regions.csv", mime="text/csv", created_by="u", scope="user")
    note = await artifacts.put_bytes(
        content=b"Definition\nRevenue includes returns.\n", filename="definition.txt", mime="text/plain", created_by="u", scope="user"
    )
    refs = RefAllowlist()
    refs.add("data", data.id)
    nid = refs.add("data", note.id)
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=canvas, workspace_id="ws_1")
    ctx._ν15_allowlist = refs
    args = {
        "data_artifact_id": data.id,
        "title": "Regions",
        "hint": "compare",
        "series": ["revenue"],
        "force_kind": "bar",
        "presentation_plan": {
            "version": 1,
            "evidence_notes": [
                {
                    "id": "revenue_definition",
                    "measure_keys": ["revenue"],
                    "ref_id": str(nid),
                    "excerpt": "Revenue includes returns.",
                    "locator": "Definition",
                }
            ],
        },
    }
    return artifacts, canvas, snapshots, ctx, args, data, note, raw


async def create(prepared):
    artifacts, canvas, snapshots, ctx, args, data, note, raw = prepared
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    return section


async def project(prepared, section, *, denied=()):
    artifacts, _, _, _, _, data, note, _ = prepared
    access = {aid: aid not in denied for aid in (data.id, note.id)}
    validity = await verify_saved_plan_evidence([section.to_report_section()], artifacts, access)
    aliases = ViewerSourceAliasMap.from_root_secret("root", workspace_id="ws_1", report_scope_id="s1")
    projected, _ = project_sources_for_viewer(
        [section.to_report_section()], [], alias_map=aliases, access_by_identity=access, evidence_validity_by_digest=validity
    )
    return projected[0], aliases, access, validity


async def test_native_tool_seals_exact_bytes_and_verifiable_notes_without_new_root(prepared):
    section = await create(prepared)
    _, canvas, _, ctx, args, data, note, raw = prepared
    spec = section.payload["spec"]
    plan = spec["presentation_plan"]
    assert plan["dataset"]["content_digest"] == hashlib.sha256(raw).hexdigest()
    assert plan["dataset"]["artifact_id"] == spec["data_artifact_id"] == data.id
    assert plan["evidence_notes"][0]["source_ref"]["artifact_id"] == note.id
    assert set(plan["text_dependencies"]) == {"/title"}
    relations = project_report_relations([section])
    assert relations.roles_for(note.id) == ("ordinary_source",)
    assert note.id not in relations.direct_ids
    result = await _add_chart(args, ctx)
    assert "error" not in result
    assert len(await canvas.list_sections(section.canvas_id)) == 1


@pytest.mark.parametrize(
    "damage,code",
    [("unregistered", "evidence_unverified"), ("invented", "evidence_unverified"), ("measure", "binding_ineligible"), ("digest", "plan_shape")],
)
async def test_invalid_notes_fail_before_canvas_or_artifact_writes(prepared, damage, code):
    artifacts, canvas, _, ctx, args, _, _, _ = prepared
    args = copy.deepcopy(args)
    note = args["presentation_plan"]["evidence_notes"][0]
    if damage == "unregistered":
        note["ref_id"] = "art_ffffffffffff"
    if damage == "invented":
        note["excerpt"] = "Revenue excludes returns."
    if damage == "measure":
        note["measure_keys"] = ["missing"]
    if damage == "digest":
        note["verified_content_digest"] = "a" * 64
    files_before = set(artifacts.backend.root_dir.rglob("*"))
    result = await _add_chart(args, ctx)
    assert result["code"] == code, result
    assert await canvas.get_canvas_by_session(ctx.session_id) is None
    assert set(artifacts.backend.root_dir.rglob("*")) == files_before


def test_unicode_limits_one_decode_and_closed_server_fields():
    note = {"id": "definition", "measure_keys": ["revenue"], "ref_id": "1", "excerpt": "😀" * 300}
    value = {"version": 1, "evidence_notes": [note]}
    assert parse_plan_input(json.dumps(value)) == value
    for bad in (
        {**value, "dataset": {}},
        json.dumps(json.dumps(value)),
        {**value, "version": True},
        {**value, "evidence_notes": [{**note, "excerpt": note["excerpt"] + "😀"}]},
    ):
        with pytest.raises(ChartPlanError):
            parse_plan_input(bad)
    with pytest.raises(ChartPlanError) as caught:
        parse_plan_input({"version": 1, "annotations": []})
    assert caught.value.code == "plan_shape"


@pytest.mark.parametrize("damage", ["rows", "oversize", "dependencies", "source"])
async def test_single_plan_root_has_complete_write_validation(prepared, damage):
    section = await create(prepared)
    spec = copy.deepcopy(section.payload["spec"])
    if damage == "rows":
        spec["rows"][0]["revenue"] = float("nan")
    if damage == "oversize":
        spec["title"] = "x" * 1_000_000
    if damage == "dependencies":
        spec["presentation_plan"]["text_dependencies"] = {}
    if damage == "source":
        spec["presentation_plan"]["evidence_notes"][0]["source_ref"]["url"] = "https://example.invalid/private"
    with pytest.raises(ChartExpressionError):
        validate_stored_composition(spec)


@pytest.mark.parametrize("change", ["denied_note", "changed_note", "changed_data"])
async def test_stale_or_restricted_dependencies_remove_quote_and_dependent_copy(prepared, change):
    section = await create(prepared)
    artifacts, _, _, _, _, data, note, _ = prepared
    denied = ()
    if change == "denied_note":
        denied = (note.id,)
    if change == "changed_note":
        Path(note.path).write_bytes(b"Definition\nChanged text")
    if change == "changed_data":
        Path(data.path).write_bytes(b"region,revenue\nA,999\n")
    projected, aliases, access, validity = await project(prepared, section, denied=denied)
    root = projected["spec"]
    assert root["title"] == "" and root["rows"] == section.payload["spec"]["rows"]
    plan = root["presentation_plan"]
    if change != "changed_data":
        assert "Revenue includes returns." not in repr(projected)
        assert "verified_content_digest" not in plan["evidence_notes"][0]
    else:
        assert plan["dataset"] == {"artifact_id": plan["dataset"]["artifact_id"], "availability": "unavailable"}
    root["kind"] = "table"
    merged = merge_viewer_patch_preserving_provenance(
        section_id=section.id,
        section_kind="chart",
        current_payload=section.payload,
        incoming_patch={"spec": root},
        alias_map=aliases,
        access_by_identity=access,
        evidence_validity_by_digest=validity,
        allow_label_edit=True,
    )
    assert merged["spec"]["title"] == "Regions"
    assert merged["spec"]["presentation_plan"] == section.payload["spec"]["presentation_plan"]
    assert merged["spec"]["kind"] == "table"


async def test_echo_preserves_canonical_evidence_and_rejects_literal_forgery(prepared):
    section = await create(prepared)
    projected, aliases, access, validity = await project(prepared, section)
    assert projected["spec"]["presentation_plan"]["evidence_notes"][0]["availability"] == "available"
    root = projected["spec"]
    root["presentation_plan"]["evidence_notes"][0]["excerpt"] = "Forged"
    with pytest.raises(SourcePatchError):
        merge_viewer_patch_preserving_provenance(
            section_id=section.id,
            section_kind="chart",
            current_payload=section.payload,
            incoming_patch={"spec": root},
            alias_map=aliases,
            access_by_identity=access,
            evidence_validity_by_digest=validity,
            allow_label_edit=True,
        )


@pytest.mark.parametrize("denied", [False, True])
@pytest.mark.parametrize("composed", [False, True])
async def test_snapshot_requires_current_projection_and_materializes_notes_once(prepared, denied, composed):
    if composed:
        prepared[4]["views"] = [
            {"id": "comparison", "title": "Comparison", "hint": "compare", "series": ["revenue"], "force_kind": "bar"},
            {"id": "details", "title": "Values", "hint": "raw", "series": ["revenue"], "force_kind": "table"},
        ]
    section = await create(prepared)
    artifacts, canvas, snapshots, _, _, data, note, _ = prepared
    with pytest.raises(SnapshotError) as caught:
        await snapshots.build_candidate(canvas_id=section.canvas_id, request=replace(_request(), requested_by="u"))
    assert caught.value.code == "chart_plan_projection_required"

    async def project_sections(stable_canvas, stable_sections):
        assert stable_canvas.id == section.canvas_id
        dto, _, _, _ = await project(prepared, stable_sections[0], denied=(note.id, data.id) if denied else ())
        return [replace(stable_sections[0], payload={key: value for key, value in dto.items() if key not in {"id", "kind", "rev"}})]

    candidate = await snapshots.build_candidate(
        canvas_id=section.canvas_id, request=replace(_request(), requested_by="u"), project_sections=project_sections
    )
    snapshot = await snapshots.commit_candidate(
        candidate,
        expected_projection_token=candidate.content_projection.projection_token,
        expected_surface_token=candidate.surface_projection.surface_token,
    )
    assert note.id not in candidate.payload_json
    assert not any(ref.artifact_id == note.id for ref in candidate.asset_refs)
    if denied:
        assert "Revenue includes returns." not in candidate.payload_json
        assert not candidate.asset_refs
    ir = normalize_export_snapshot(snapshot, asset_content_hashes={data.id: "sha256:" + "a" * 64} if not denied else {})
    paragraphs = [block for block in iter_export_blocks(ir.blocks) if block.block_kind == "paragraph"]
    assert len(paragraphs) == 1
    assert ("Revenue includes returns" in repr(paragraphs)) is not denied


async def test_calculation_notes_seal_the_materialized_dataset_once(prepared):
    prepared[4]["derived_columns"] = [{"key": "double", "header": "Double revenue", "expression": "revenue * 2", "unit": "USD"}]
    prepared[4]["series"] = ["double"]
    prepared[4]["presentation_plan"]["evidence_notes"][0]["measure_keys"] = ["double"]
    section = await create(prepared)
    artifacts = prepared[0]
    spec = section.payload["spec"]
    dataset = spec["presentation_plan"]["dataset"]
    assert spec["data_artifact_id"] != prepared[5].id
    art = await artifacts.get(spec["data_artifact_id"])
    assert art is not None
    raw = await artifacts.backend.read(art.path)
    assert dataset["content_digest"] == hashlib.sha256(raw).hexdigest()
    dto, _, _, _ = await project(prepared, section)
    # This helper explicitly grants only original data/note refs; the derived
    # artifact requires its own report closure entry before any byte read.
    assert dto["spec"]["presentation_plan"]["dataset"]["availability"] == "unavailable"


def test_invalid_unicode_and_encoded_plan_budget_return_corrective_errors():
    for value in (
        "\ud800",
        {"version": 1, "evidence_notes": [{"id": "definition", "measure_keys": ["value"], "ref_id": "x" * 20_000, "excerpt": "Literal"}]},
    ):
        with pytest.raises(ChartPlanError):
            parse_plan_input(value)


async def test_unknown_projected_plan_fails_static_export_explicitly(prepared):
    section = await create(prepared)
    snapshots = prepared[2]

    async def project_sections(stable_canvas, stable_sections):
        dto, _, _, _ = await project(prepared, stable_sections[0])
        dto["spec"]["presentation_plan"] = {"version": 2, "availability": "unavailable"}
        return [replace(stable_sections[0], payload={key: value for key, value in dto.items() if key not in {"id", "kind", "rev"}})]

    with pytest.raises(SnapshotError) as caught:
        await snapshots.build_candidate(canvas_id=section.canvas_id, request=replace(_request(), requested_by="u"), project_sections=project_sections)
    assert caught.value.code == "snapshot_invalid_chart_plan"
