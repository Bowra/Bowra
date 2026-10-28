"""Analytical figures retain original observations across the native lifecycle."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest
from bowhead_core.canvas_tools import _add_chart
from bowhead_core.chart.expression import validate_stored_composition
from bowhead_core.chart.presentation_plan import validate_projected_notes_plan
from bowhead_core.chart_style_export import build_chart_style_export_theme_v2
from bowhead_core.export_ir import iter_export_blocks, normalize_export_snapshot
from bowhead_core.print_theme import current_print_theme_snapshot
from bowhead_core.source_access_projection import SourcePatchError, merge_viewer_patch_preserving_provenance
from test_chart_evidence_plan import prepared as prepared
from test_chart_evidence_plan import project
from test_chart_style_export_v2 import _effective_v2
from test_chart_time_window import time_prepared
from test_export_snapshot import _request

RAW = b"city,tier,low,high,central,current,official,price,period\nA,First,-40,-24,-30,-20,-17,35800,2029Q4\nB,First,,-30,-36,-25,-22,,2029Q4\nC,Second,-45,-45,-45,,-31,10200,2029Q4\n"
SERIES = b"tier,series,x,value,low,high,drop,period\nFirst,History,-2,80,,,,-\nFirst,History,0,100,,,,-\nFirst,History,5,70,,,,-\nFirst,Baseline,5,65,60,70,-40,2029Q4\nFirst,Baseline,7.5,,,, -40,2029Q4\nFirst,Baseline,10,60,55,65,-40,2029Q4\nSecond,History,0,100,,,,-\nSecond,History,5,60,,,,-\nSecond,Baseline,5,60,55,65,-50,2029Q4\nSecond,Baseline,10,50,45,55,-50,2029Q4\n"


async def analytical_case(prepared, series=False, raw=None):
    value = await time_prepared(prepared, raw or (SERIES if series else RAW))
    artifacts, canvas, snapshots, ctx, old, data, _, raw = value
    measures = ["value", "low", "high"] if series else ["low", "high", "central", "current", "official"]
    basis = {
        "role": "metric_basis",
        "measure_keys": measures,
        "value_kind": "index" if series else "rate",
        "adjustment": "real" if series else "not_applicable",
    }
    if series:
        basis["index_base"] = 100
    excerpt = json.dumps(basis, separators=(",", ":"))
    note = await artifacts.put_bytes(content=excerpt.encode(), filename="basis.json", mime="application/json", created_by="u", scope="user")
    ctx._ν15_allowlist.add("data", note.id)
    descriptor = {
        "view_id": "comparison",
        "preset": "interval_rows",
        "row_keys": ["city"],
        "group_key": "tier",
        "points": [{"key": "central", "role": "primary"}, {"key": "current", "role": "current"}, {"key": "official", "role": "reference"}],
        "detail_keys": ["price", "period"],
    }
    if series:
        descriptor = {
            "view_id": "comparison",
            "preset": "series_plot",
            "x_key": "x",
            "x_kind": "numeric",
            "value_key": "value",
            "series_key": "series",
            "facet": {"key": "tier"},
            "summary_keys": ["drop", "period"],
            "series_roles": [
                {"series_value": "History", "role": "observed"},
                {"series_value": "Baseline", "role": "baseline", "note_ids": ["basis"]},
            ],
        }
    descriptor["range"] = {"lower_key": "low", "upper_key": "high", "kind": "estimate", "note_ids": ["basis"]}
    args = {
        "data_artifact_id": data.id,
        "title": "Prepared comparisons",
        "views": [{"id": "comparison", "title": "Prepared comparison", "force_kind": "line" if series else "table", "series": measures}],
        "column_types": dict.fromkeys(measures, "numeric" if series else "percent_points"),
        "presentation_plan": {
            "version": 3,
            "analytical_views": [descriptor],
            "evidence_notes": [{"id": "basis", "measure_keys": measures, "ref_id": note.id, "excerpt": excerpt}],
        },
    }
    return artifacts, canvas, snapshots, ctx, args, data, note, raw


@pytest.mark.parametrize("series,tall", [(False, False), (False, True), (True, False)])
@pytest.mark.parametrize("frozen_style", [False, True])
async def test_native_analytical_save_projection_protected_edit_and_export(prepared, series, tall, frozen_style):
    raw = (
        RAW.splitlines()[0] + b"\n" + b"\n".join(b"City" + str(index).encode() + RAW.splitlines()[1][1:] for index in range(29)) + b"\n"
        if tall
        else None
    )
    value = await analytical_case(prepared, series, raw)
    _, canvas, snapshots, ctx, args, data, note, _ = value
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    spec = section.payload["spec"]
    assert len(spec["views"]) == 1
    validate_stored_composition(spec)
    assert spec["views"][0]["spec"]["row_count"] == (10 if series else 29 if tall else 3)
    safe, aliases, access, validity = await project(value, section)
    validate_projected_notes_plan(safe["spec"]["presentation_plan"])
    from bowhead_core.export_visual_identity import compute_visual_input_sha256

    def identity(chart):
        return compute_visual_input_sha256(section_kind="chart", payload={"spec": chart}, locale="en-US", theme={"kind": "default"})

    assert identity(safe["spec"]) == identity(spec)
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
    assert merged["spec"]["presentation_plan"] == spec["presentation_plan"]
    changed["presentation_plan"]["analytical_views"][0]["binding"]["value_domain"] = [-999, 999]
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
    assert denied["spec"]["presentation_plan"]["analytical_views"] == [{"view_id": "comparison", "availability": "unavailable"}]
    assert identity(denied["spec"]) != identity(spec)
    validate_projected_notes_plan(denied["spec"]["presentation_plan"])

    async def projection(_canvas, sections):
        dto, *_ = await project(value, sections[0])
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
    blocks = list(iter_export_blocks(normalize_export_snapshot(snapshot, asset_content_hashes={data.id: "sha256:" + "a" * 64}).blocks))
    leaves = [block.content["payload"]["spec"] for block in blocks if block.block_kind == "visual"]
    assert leaves[0]["_export_analytical"]["preset"] == ("series_plot" if series else "interval_rows")
    assert [row for leaf in leaves for row in leaf["rows"]] == spec["views"][0]["spec"]["rows"]
    assert len(leaves) == (2 if series else 4 if tall else 1)
    assert all(leaf["_export_analytical"]["binding"]["value_domain"] == leaves[0]["_export_analytical"]["binding"]["value_domain"] for leaf in leaves)


@pytest.mark.parametrize("mutation", ["missing_bound", "reversed", "duplicate", "basis", "unknown", "unit", "limit", "wrong_kind", "roles"])
async def test_invalid_analytical_data_never_creates_partial_canvas(prepared, mutation):
    raw = RAW
    if mutation == "reversed":
        raw = raw.replace(b"-40,-24", b"-24,-40")
    if mutation == "duplicate":
        raw += raw.splitlines()[1] + b"\n"
    value = await analytical_case(prepared, mutation == "roles", raw=None if mutation == "roles" else raw)
    _, canvas, _, ctx, args, *_ = value
    item = args["presentation_plan"]["analytical_views"][0]
    if mutation == "missing_bound":
        item["range"]["lower_key"] = "missing"
    elif mutation == "basis":
        args["presentation_plan"]["evidence_notes"] = []
    elif mutation == "unknown":
        item["pixels"] = 800
    elif mutation == "unit":
        args["column_types"]["current"] = "numeric"
    elif mutation == "limit":
        args["views"][0]["limit"] = 1
    elif mutation == "wrong_kind":
        args["views"][0]["force_kind"] = "bar"
    elif mutation == "roles":
        item["series_roles"].pop()
    result = await _add_chart(args, ctx)
    assert "error" in result, result
    assert await canvas.get_canvas_by_session(ctx.session_id) is None


def _compiled_fixture(name):
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    return json.loads((root / "config/chart_style/analytical_fixtures.json").read_text())[name]["section"]["spec"]


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "bounds",
        "basis_type",
        "basis_conflict",
        "series_limit",
        "facet_limit",
        "date_granularity",
        "summary_conflict",
        "unknown_range",
        "infinite",
        "confidence",
        "missing_field",
    ],
)
def test_analytical_complete_data_boundary_rejects_misleading_comparisons(mutation):
    from bowhead_core.chart.analytical import compile_analytical_views
    from bowhead_core.chart.presentation_plan import ChartPlanError

    spec = _compiled_fixture("aligned_cycles")
    chart = spec["views"][0]["spec"]
    item = spec["presentation_plan"]["analytical_views"][0]
    item = {key: value for key, value in item.items() if key not in {"binding", "availability", "binding_id"}}
    notes = spec["presentation_plan"]["evidence_notes"]
    rows = chart["rows"]
    if mutation == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
        chart["row_count"] += 1
    elif mutation == "bounds":
        rows[0].update(low=20, high=10)
    elif mutation == "basis_type":
        statement = json.loads(notes[0]["excerpt"])
        statement["value_kind"] = []
        notes[0]["excerpt"] = json.dumps(statement)
    elif mutation == "basis_conflict":
        note = copy.deepcopy(notes[0])
        statement = json.loads(note["excerpt"])
        statement["index_base"] = 1000
        note["excerpt"] = json.dumps(statement)
        notes.append(note)
    elif mutation == "series_limit":
        for index in range(13):
            rows.append({**rows[0], "series": str(index)})
        chart["row_count"] = len(rows)
    elif mutation == "facet_limit":
        item["facet"] = {"key": "series"}
        for index in range(7):
            rows.append({**rows[0], "series": str(index)})
        chart["row_count"] = len(rows)
    elif mutation == "date_granularity":
        item["x_kind"] = "date"
        for index, row in enumerate(rows):
            row["elapsed_years"] = f"2026-01-{index + 1:02}"
        rows[0]["elapsed_years"] = "2026"
    elif mutation == "summary_conflict":
        item["summary_keys"] = ["value"]
    elif mutation == "unknown_range":
        item["range"]["midpoint"] = 0
    elif mutation == "infinite":
        rows[0]["value"] = float("inf")
    elif mutation == "confidence":
        item["range"]["kind"] = "confidence"
    else:
        item["range"]["lower_key"] = "missing"
    with pytest.raises(ChartPlanError):
        compile_analytical_views([item], spec, notes)


def test_static_interval_pages_preserve_every_original_row_and_shared_scale():
    from bowhead_core.chart.analytical import materialize_analytical, static_analytical_panels

    spec = materialize_analytical(_compiled_fixture("scenario_rows"))["views"][0]["spec"]
    pages = static_analytical_panels(spec, locale="de-DE")
    assert len(pages) == 4
    assert [row for page in pages for row in page["spec"]["rows"]] == spec["rows"]
    assert all(len(page["spec"]["rows"]) + len({row["tier"] for row in page["spec"]["rows"]}) <= 10 for page in pages)
    assert all(
        page["spec"]["_export_analytical"]["binding"]["value_domain"] == spec["_export_analytical"]["binding"]["value_domain"] for page in pages
    )
    assert pages[-1]["spec"]["title"].endswith("Zeilen 26–29 von 29")
    for row in spec["rows"]:
        row["tier"] = row["city"]
    spec["_export_analytical"]["binding"]["group_values"] = [row["tier"] for row in spec["rows"]]
    pages = static_analytical_panels(spec, locale="en-US")
    assert len(pages) == 6
    assert [row for page in pages for row in page["spec"]["rows"]] == spec["rows"]
    assert all(len(page["spec"]["rows"]) <= 5 for page in pages)


def test_date_positions_preserve_actual_elapsed_distances():
    from bowhead_core.chart.analytical import compile_analytical_views

    spec = _compiled_fixture("aligned_cycles")
    chart = spec["views"][0]["spec"]
    item = spec["presentation_plan"]["analytical_views"][0]
    item = {key: value for key, value in item.items() if key not in {"binding", "availability", "binding_id"}}
    item["x_kind"] = "date"
    for index, row in enumerate(chart["rows"]):
        row["elapsed_years"] = ["2026-01-01", "2026-01-02", "2026-03-01"][index % 3]
    chart["rows"] = [chart["rows"][index] for index in [0, 1, 2, 9, 10, 11, 18, 19, 20]]
    chart["row_count"] = len(chart["rows"])
    result = compile_analytical_views([item], spec, spec["presentation_plan"]["evidence_notes"])[0]
    assert result["binding"]["x_domain"][1] - result["binding"]["x_domain"][0] == 59 * 86400000 * 1.1


@pytest.mark.parametrize("series", [False, True])
async def test_selected_analytical_followup_resolves_original_keys_and_rejects_forged_scope(prepared, series):
    from pathlib import Path

    from bowhead_core.chart.selected_context import resolve_selected_contexts
    from bowhead_core.session_store import SessionStore

    artifacts, canvas, _, ctx, args, data, _, _ = await analytical_case(prepared, series)
    result = await _add_chart(args, ctx)
    root = (await canvas.list_sections(result["canvas_id"]))[0]
    store = SessionStore(artifacts.db_path)
    await store.ensure_session(session_id=ctx.session_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    await store.pin_session_attachments(session_id=ctx.session_id, artifact_ids=[data.id])
    spec = root.payload["spec"]
    plan = spec["presentation_plan"]
    profile = plan["analytical_views"][0]
    keys = profile["binding"]["observation_keys"]
    row = spec["views"][0]["spec"]["rows"][0]
    scope = {
        "version": 1,
        "sectionId": root.id,
        "sectionRev": root.rev,
        "viewId": "comparison",
        "bindingId": profile["binding_id"],
        "datasetDigest": plan["dataset"]["content_digest"],
        "tuples": [[row[key] for key in keys]],
        "measureKeys": profile["binding"]["measure_keys"],
    }
    ref = {"fileId": data.id, "filename": data.filename, "mime": data.mime, "size": data.size, "referenceKind": "result", "selectedContext": scope}

    async def resolve(value):
        return await resolve_selected_contexts(artifacts, [value], user_id=ctx.user_id, workspace_id=ctx.workspace_id, session_id=ctx.session_id)

    facts = (await resolve(ref))[0]
    assert facts["observations"] == [{key: row.get(key) for key in [*keys, *scope["measureKeys"]]}]
    assert facts["selectedRows"] == 1
    for invalid in [
        {"bindingId": "forged"},
        {"viewId": "other"},
        {"sectionRev": root.rev + 1},
        {"measureKeys": ["price" if not series else "drop"]},
        {"tuples": [["invented"]]},
        {"window": {"lower": 0, "upper": 1}},
        {"frameKey": "First", "tuples": None},
    ]:
        forged = copy.deepcopy(ref)
        forged["selectedContext"].update(invalid)
        with pytest.raises(ValueError):
            await resolve(forged)
    Path(data.path).write_bytes(b"changed dataset")
    with pytest.raises(ValueError, match="chart_selection_unavailable"):
        await resolve(ref)


async def test_six_facets_share_union_scales_and_keep_absent_series_absent(prepared):
    import csv
    import io

    source = list(csv.DictReader(io.StringIO(SERIES.decode())))
    rows = []
    for index in range(6):
        for row in source:
            if row["tier"] != "First" or (index == 5 and row["series"] == "Baseline"):
                continue
            rows.append({**row, "tier": f"Tier {index + 1}"})
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    value = await analytical_case(prepared, True, output.getvalue().encode())
    _, canvas, _, ctx, args, _, _, _ = value
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    from bowhead_core.chart.analytical import materialize_analytical, static_analytical_panels

    safe, *_ = await project(value, (await canvas.list_sections(result["canvas_id"]))[0])
    child = materialize_analytical(safe["spec"])["views"][0]["spec"]
    panels = static_analytical_panels(child, locale="en-US")
    assert len(panels) == 6
    assert all(
        panel["spec"]["_export_analytical"]["binding"]["value_domain"] == child["_export_analytical"]["binding"]["value_domain"] for panel in panels
    )
    assert {row["series"] for row in panels[-1]["spec"]["rows"]} == {"History"}
