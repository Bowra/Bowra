"""Versioned contextual reading stays source-bound across native consumers."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest
from bowhead_core.canvas_tools import _add_chart
from bowhead_core.chart.expression import ChartExpressionError, validate_stored_composition
from bowhead_core.chart.presentation_plan import ChartPlanError, parse_plan_input, validate_projected_notes_plan
from bowhead_core.export_ir import iter_export_blocks, normalize_export_snapshot
from bowhead_core.source_access_projection import SourcePatchError, merge_viewer_patch_preserving_provenance
from test_chart_annotations import annotation_args
from test_chart_evidence_plan import prepared as prepared
from test_chart_evidence_plan import project
from test_chart_prepared_frames import RAW, frame_args
from test_chart_time_window import time_prepared
from test_export_snapshot import _request


async def event_case(prepared, role="coverage"):
    value = await time_prepared(prepared)
    artifacts, canvas, snapshots, ctx, args, data, _, raw = value
    statement = {
        "role": role,
        "time_key": "month",
        "measure_keys": ["revenue"],
        "text": "Partial reporting period" if role == "coverage" else "Store opening",
    }
    statement.update({"lower": "2026-01", "upper": "2026-04"} if role == "coverage" else {"at": "2026-03"})
    excerpt = json.dumps(statement, separators=(",", ":"))
    note = await artifacts.put_bytes(content=excerpt.encode(), filename="reporting-scope.json", mime="application/json", created_by="u", scope="user")
    nid = ctx._ν15_allowlist.add("data", note.id)
    args = annotation_args(value)
    plan = args["presentation_plan"]
    plan["version"] = 2
    plan.pop("guide_steps")
    item = plan["annotations"][2 if role == "coverage" else 0]
    item.update(role=role, note_ids=["scope"], label=statement["text"])
    plan["annotations"] = [item]
    plan["evidence_notes"] = [{"id": "scope", "measure_keys": ["revenue"], "ref_id": str(nid), "excerpt": excerpt}]
    return artifacts, canvas, snapshots, ctx, args, data, note, raw


@pytest.mark.parametrize("role", ["event", "coverage"])
async def test_source_statement_survives_native_save_projection_patch_and_export(prepared, role):
    value = await event_case(prepared, role)
    _, canvas, snapshots, ctx, args, data, note, _ = value
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    validate_stored_composition(section.payload["spec"])
    safe, aliases, access, validity = await project(value, section)
    plan = safe["spec"]["presentation_plan"]
    validate_projected_notes_plan(plan)
    assert plan["version"] == 2 and plan["annotations"][0]["role"] == role
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
    assert merged["spec"]["presentation_plan"] == section.payload["spec"]["presentation_plan"]
    changed["presentation_plan"]["annotations"][0]["note_ids"] = ["invented"]
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
    denied, *_ = await project(value, section, denied=[note.id])
    validate_projected_notes_plan(denied["spec"]["presentation_plan"])
    assert denied["spec"]["presentation_plan"]["annotations"][0] == {
        "id": plan["annotations"][0]["id"],
        "view_id": "trend",
        "availability": "unavailable",
    }
    assert args["presentation_plan"]["annotations"][0]["label"] not in json.dumps(denied)

    async def projection(_canvas, sections):
        dto, *_ = await project(value, sections[0])
        return [replace(sections[0], payload={key: val for key, val in dto.items() if key not in {"id", "kind", "rev", "parent_id", "order_index"}})]

    candidate = await snapshots.build_candidate(
        canvas_id=section.canvas_id, request=replace(_request(), requested_by="u", locale="en-US"), project_sections=projection
    )
    snapshot = await snapshots.commit_candidate(
        candidate,
        expected_projection_token=candidate.content_projection.projection_token,
        expected_surface_token=candidate.surface_projection.surface_token,
    )
    blocks = list(iter_export_blocks(normalize_export_snapshot(snapshot, asset_content_hashes={data.id: "sha256:" + "a" * 64}).blocks))
    leaves = [block.content["payload"]["spec"] for block in blocks if block.block_kind == "visual"]
    assert leaves[0]["_export_annotations"][0]["role"] == role
    assert any(note.id not in json.dumps(block.content) and "source-ordinal:" in json.dumps(block.content) for block in blocks)
    note_block = next(block for block in blocks if block.block_kind == "paragraph" and block.source_refs)
    visible = " ".join(note_block.semantic_tokens)
    assert plan["annotations"][0]["label"] in visible
    assert ("2026-03" if role == "event" else "2026-01 – 2026-04") in visible
    assert '"measure_keys"' not in visible and "\\" not in visible
    assert "[" + note_block.source_refs[0].split(":")[-1] + "]" in visible


@pytest.mark.parametrize("mutation", ["version", "note", "measure", "bounds", "text", "role"])
async def test_invalid_evidence_never_writes_a_partial_figure(prepared, mutation):
    value = await event_case(prepared)
    _, canvas, _, ctx, args, *_ = value
    plan = args["presentation_plan"]
    item = plan["annotations"][0]
    if mutation == "version":
        plan["version"] = 1
    elif mutation == "note":
        item["note_ids"] = ["missing"]
    elif mutation == "measure":
        item["measure_key"] = "cost"
    elif mutation == "bounds":
        item["upper"] = "2026-03"
    elif mutation == "text":
        item["label"] = "Revenue rose because of the campaign"
    else:
        item["role"] = "forecast"
    result = await _add_chart(args, ctx)
    assert "error" in result, result
    assert await canvas.get_canvas_by_session(ctx.session_id) is None


async def test_table_trends_reuse_prepared_frame_grain_and_protect_replay(prepared):
    value = await time_prepared(prepared, RAW)
    _, canvas, _, ctx, _, data, _, _ = value
    args = frame_args(value)
    bid = args["presentation_plan"]["interactions"][0]["binding_id"]
    args["presentation_plan"] = {"version": 2, "table_trends": [{"view_id": "detail", "binding_id": bid, "measure_key": "revenue"}]}
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    validate_stored_composition(section.payload["spec"])
    safe, aliases, access, validity = await project(value, section)
    validate_projected_notes_plan(safe["spec"]["presentation_plan"])
    forged = copy.deepcopy(section.payload["spec"])
    forged["presentation_plan"]["table_trends"][0]["binding_id"] = "frame.invented"
    with pytest.raises(ChartExpressionError):
        validate_stored_composition(forged)
    item = safe["spec"]["presentation_plan"]["table_trends"][0]
    assert item["binding"]["frame_keys"] == ["2026-01", "2026-02", "2026-03"]
    assert item["binding"]["entity_values"] == ["A", "B"]
    changed = copy.deepcopy(safe["spec"])
    changed["presentation_plan"]["table_trends"][0]["measure_key"] = "cost"
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
    denied, *_ = await project(value, section, denied=[data.id])
    assert denied["spec"]["presentation_plan"]["table_trends"] == [{"view_id": "detail", "availability": "unavailable"}]
    validate_projected_notes_plan(denied["spec"]["presentation_plan"])
    for bad in [
        {**args["presentation_plan"], "version": 1},
        {"version": 2, "table_trends": [{"view_id": "detail", "binding_id": bid, "measure_key": "revenue", "values": [100]}]},
    ]:
        with pytest.raises(ChartPlanError):
            parse_plan_input(bad)


async def test_existing_structured_json_recovery_preserves_v2_evidence_validation(prepared):
    value = await event_case(prepared)
    _, canvas, _, ctx, args, *_ = value
    args["presentation_plan"] = "```json\n" + json.dumps(args["presentation_plan"]) + "\n```"
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    assert section.payload["spec"]["presentation_plan"]["annotations"][0]["role"] == "coverage"


@pytest.mark.parametrize("field", ["table_trends", "_export_table_trend"])
async def test_misplaced_trend_semantics_are_not_silently_dropped(prepared, field):
    _, canvas, _, ctx, args, *_ = prepared
    result = await _add_chart({**args, field: []}, ctx)
    assert result.get("code") == "plan_shape"
    assert result.get("path") == "/" + field
    assert await canvas.get_canvas_by_session(ctx.session_id) is None


async def test_csv_context_catalog_and_corrective_ids_allow_table_trend_retry(prepared):
    from bowhead_core._ref_allowlist import RefAllowlist

    value = await time_prepared(prepared, RAW)
    artifacts, canvas, _, ctx, _, _, _, _ = value
    from bowhead_core.session_store import SessionStore

    await SessionStore(artifacts.db_path).ensure_session(session_id=ctx.session_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    data = await artifacts.put_bytes(
        content=RAW, filename="sandbox-observations.csv", mime="text/csv", created_by=ctx.user_id, session_id=ctx.session_id, source="python_sandbox"
    )
    refs = RefAllowlist()
    await refs.refresh_from_stores(
        canvas_store=canvas, artifact_store=artifacts, session_id=ctx.session_id, user_id=ctx.user_id, canvas_id=None, tool_invocations=[]
    )
    entry = next(row for row in refs.snapshot() if row["id"] == data.id)
    catalog = entry["meta"]["chart_catalog"]
    candidate = next(item["id"] for item in catalog["candidates"] if item["kind"] == "frame")
    assert candidate in refs.to_prompt_xml()
    ctx._ν15_allowlist = refs
    args = frame_args(value)
    args["data_artifact_id"] = data.id
    args["presentation_plan"] = {"version": 2, "table_trends": [{"view_id": "detail", "binding_id": "frame.invented", "measure_key": "revenue"}]}
    rejected = await _add_chart(args, ctx)
    assert rejected["code"] == "binding_unknown", rejected
    assert rejected["path"] == "/presentation_plan/table_trends/0/binding_id"
    assert candidate in rejected["corrective_ids"]
    assert await canvas.get_canvas_by_session(ctx.session_id) is None
    args["presentation_plan"]["table_trends"][0]["binding_id"] = candidate
    accepted = await _add_chart(args, ctx)
    assert "error" not in accepted, accepted
    assert len(await canvas.list_sections(accepted["canvas_id"])) == 1


@pytest.mark.parametrize("change", ["formatting", "text", "bounds", "duplicate"])
async def test_json_note_formatting_recovery_keeps_exact_source_and_rejects_changed_facts(prepared, change):
    value = await event_case(prepared)
    artifacts, canvas, _, ctx, args, _, note, _ = value
    source = json.loads(args["presentation_plan"]["evidence_notes"][0]["excerpt"])
    literal = json.dumps(source, ensure_ascii=False)
    from pathlib import Path

    Path(note.path).write_text(literal)
    submitted = args["presentation_plan"]["evidence_notes"][0]
    if change == "text":
        submitted["excerpt"] = submitted["excerpt"].replace("Partial reporting period", "Complete reporting period")
    elif change == "bounds":
        submitted["excerpt"] = submitted["excerpt"].replace("2026-04", "2026-05")
    elif change == "duplicate":
        submitted["excerpt"] = submitted["excerpt"].replace("{", '{"role":"invented",', 1)
    result = await _add_chart(args, ctx)
    if change != "formatting":
        assert result["code"] == "evidence_unverified", result
        assert await canvas.get_canvas_by_session(ctx.session_id) is None
        return
    assert "error" not in result, result
    assert result["presentation_fallbacks"][0]["code"] == "note_excerpt_normalized"
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    assert section.payload["spec"]["presentation_plan"]["evidence_notes"][0]["excerpt"] == literal
    safe, *_ = await project(value, section)
    assert safe["spec"]["presentation_plan"]["evidence_notes"][0]["availability"] == "available"


@pytest.mark.parametrize("unit", ["CNY 10k", "%", "", "x" * 33])
async def test_csv_units_are_per_measure_and_never_rescale_original_values(prepared, unit):
    value = await time_prepared(prepared, b"month,revenue,margin\n2026-01,12.34567,0.42121\n2026-02,21.23456,0.51234\n")
    artifacts, canvas, _, ctx, base, data, _, raw = value
    args = {
        "data_artifact_id": data.id,
        "title": "Revenue and margin",
        "column_types": {"margin": "percent_fraction"},
        "column_units": {"revenue": unit, "margin": "%"},
        "views": [
            {"id": "metrics", "title": "Revenue and margin", "force_kind": "dual_axis", "series": ["revenue", "margin"]},
            {"id": "detail", "title": "Original observations", "force_kind": "table", "series": ["revenue", "margin"]},
        ],
    }
    result = await _add_chart(args, ctx)
    if unit != "CNY 10k":
        assert result["path"] == "/column_units", result
        assert await canvas.get_canvas_by_session(ctx.session_id) is None
        return
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    for view in section.payload["spec"]["views"]:
        columns = {column["key"]: column for column in view["spec"]["columns"]}
        assert columns["revenue"]["unit"] == "CNY 10k"
        assert columns["margin"]["unit"] == "%"
        assert columns["margin"]["percent_scale"] == "fraction"
        assert view["spec"]["rows"][0]["revenue"] == 12.34567
        assert view["spec"]["rows"][0]["margin"] == 0.42121
    assert await artifacts.backend.read(data.path) == raw


async def test_composed_export_accounts_for_canvas_owners_and_rejects_missing_or_foreign_roots(prepared):
    from bowhead_core.export_validation import ExportValidationError, validate_export_output
    from bowhead_core.export_visual_identity import EXPORT_VISUAL_RENDERER_VERSION
    from test_export_validation import _matching_evidence

    value = await event_case(prepared)
    _, canvas, snapshots, ctx, args, data, _, _ = value
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]

    async def projection(_canvas, sections):
        safe, *_ = await project(value, sections[0])
        return [replace(sections[0], payload={key: val for key, val in safe.items() if key not in {"id", "kind", "rev", "parent_id", "order_index"}})]

    candidate = await snapshots.build_candidate(
        canvas_id=section.canvas_id, request=replace(_request(), requested_by="u"), project_sections=projection
    )
    snapshot = await snapshots.commit_candidate(
        candidate,
        expected_projection_token=candidate.content_projection.projection_token,
        expected_surface_token=candidate.surface_projection.surface_token,
    )
    ir = normalize_export_snapshot(snapshot, asset_content_hashes={data.id: "sha256:" + "a" * 64})
    assert snapshot.section_count == 3
    assert len({block.source.section_id for block in iter_export_blocks(ir.blocks)}) == 1

    def validate(value, evidence):
        return validate_export_output(
            job_id="exp_composed",
            snapshot=snapshot,
            export_ir=value,
            target="pdf_full",
            include_appendix=True,
            renderer_name="canvas-reportlab-pdf-v2",
            renderer_version=EXPORT_VISUAL_RENDERER_VERSION,
            evidence=evidence,
        )

    evidence = _matching_evidence(ir)
    assert validate(ir, evidence).validation.result == "clean"
    for damaged in [replace(ir, blocks=()), replace(ir, blocks=(replace(ir.blocks[0], source=replace(ir.blocks[0].source, section_id="foreign")),))]:
        with pytest.raises(ExportValidationError, match="export_ir_section_accounting_mismatch"):
            validate(damaged, evidence)
    visual_keys = {block.block_key for block in iter_export_blocks(ir.blocks) if block.visual_input_sha256}
    missing = replace(evidence, sections=tuple(item for item in evidence.sections if item.block_key not in visual_keys))
    assert validate(ir, missing).validation.result == "rejected"


@pytest.mark.parametrize("kind", ["line", "dual_axis"])
async def test_host_resolves_native_trend_and_time_window_without_model_binding_ids(prepared, kind):
    raw = (
        b"month,region,revenue,margin\n2026-01,East,52.12345,0.42121\n2026-01,West,43.23456,0.31\n"
        b"2026-02,East,63.45678,0.4325\n2026-02,West,48.34567,0.32\n"
        b"2026-03,East,68.9,0.445\n2026-03,West,,0.33\n"
        b"2026-04,East,70.12345,0.448\n2026-04,West,55.67891,0.34\n"
    )
    # A dual-axis time series has one observation per period; grouped line data
    # instead keeps the original entity/time grain, including the missing cell.
    if kind == "dual_axis":
        raw = b"month,revenue,margin\n2026-01,52.12345,0.42121\n2026-02,63.45678,0.4325\n"
    value = await time_prepared(prepared, raw)
    _, canvas, _, ctx, _, _, _, _ = value
    args = {
        "data_artifact_id": value[5].id,
        "title": "Original observations",
        "column_units": {"revenue": "CNY 10k"},
        "column_types": {"margin": "percent_fraction"},
        "views": [
            {"id": "trend", "title": "Trend", "force_kind": kind, "series": ["revenue"] if kind == "line" else ["revenue", "margin"]},
            {"id": "detail", "title": "Details", "force_kind": "table", "series": ["revenue"] if kind == "line" else ["revenue", "margin"]},
        ],
        "presentation_plan": {"version": 2, "interactions": [{"preset": "time_window", "view_ids": ["trend", "detail"]}]},
    }
    if kind == "line":
        args["presentation_plan"]["table_trends"] = [{"view_id": "detail", "measure_key": "revenue"}]
    original = copy.deepcopy(args)
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    spec = section.payload["spec"]
    validate_stored_composition(spec)
    safe, *_ = await project(value, section)
    validate_projected_notes_plan(safe["spec"]["presentation_plan"])
    plan = spec["presentation_plan"]
    bid = plan["interactions"][0]["binding_id"]
    assert plan["view_bindings"] == {"trend": bid, "detail": bid}
    assert plan["bindings"][bid]["kind"] == "time"
    assert len(spec["views"][0]["spec"]["rows"]) == (8 if kind == "line" else 2)
    if kind == "line":
        trend = plan["table_trends"][0]
        assert trend["binding_id"].startswith("frame.")
        assert trend["binding"]["entity_values"] == ["East", "West"]
        assert trend["binding"]["frame_keys"] == ["2026-01", "2026-02", "2026-03", "2026-04"]
        assert any(row.get("revenue") is None for row in spec["views"][0]["spec"]["rows"])
    else:
        columns = {column["key"]: column for column in spec["views"][0]["spec"]["columns"]}
        assert columns["margin"]["percent_scale"] == "fraction"
        assert columns["revenue"]["unit"] == "CNY 10k"
    assert args == original
    assert (await _add_chart(args, ctx))["section_id"] == section.id


@pytest.mark.parametrize("unit", ["fraction", "percentage points", "%"])
async def test_percentage_storage_shape_cannot_replace_its_display_unit(prepared, unit):
    value = await time_prepared(prepared, b"month,growth\n2026-01,42.121\n2026-02,43.25\n")
    artifacts, canvas, _, ctx, _, data, _, raw = value
    args = {
        "data_artifact_id": data.id,
        "title": "Growth",
        "force_kind": "table",
        "column_types": {"growth": "percent_points"},
        "column_units": {"growth": unit},
    }
    result = await _add_chart(args, ctx)
    if unit != "%":
        assert result["path"] == "/column_units/growth"
        assert await canvas.get_canvas_by_session(ctx.session_id) is None
    else:
        assert "error" not in result, result
        spec = (await canvas.list_sections(result["canvas_id"]))[0].payload["spec"]
        assert next(column for column in spec["columns"] if column["key"] == "growth")["percent_scale"] == "points"
        assert spec["rows"][0]["growth"] == 42.121
    assert await artifacts.backend.read(data.path) == raw


@pytest.mark.parametrize("preset", ["linked_focus", "frame_player"])
async def test_native_focus_and_frames_resolve_unique_omitted_binding(prepared, preset):
    from test_chart_linked_focus import focus_args

    value = await time_prepared(prepared, RAW) if preset == "frame_player" else prepared
    _, canvas, _, ctx, *_ = value
    args = frame_args(value) if preset == "frame_player" else focus_args(value[4], value[-1])
    plan = args["presentation_plan"]
    plan.pop("view_bindings", None)
    for interaction in plan["interactions"]:
        interaction.pop("binding_id")
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    spec = (await canvas.list_sections(result["canvas_id"]))[0].payload["spec"]
    validate_stored_composition(spec)
    assert all(item["binding_id"] in spec["presentation_plan"]["bindings"] for item in spec["presentation_plan"]["interactions"])
    corrupted = copy.deepcopy(spec)
    corrupted["presentation_plan"]["interactions"][0].pop("binding_id")
    with pytest.raises((ChartPlanError, ChartExpressionError)):
        validate_stored_composition(corrupted)
