"""Native transforms preserve complete original observation grain and missingness."""

from __future__ import annotations

import copy

import pytest
from bowhead_core.canvas_tools import _add_chart
from bowhead_core.chart.expression import validate_stored_composition
from bowhead_core.chart.linked_focus import _eligible_view
from bowhead_core.chart.selected_context import resolve_selected_contexts
from bowhead_core.chart_style_presentation import normalize_chart_for_rendering_v1
from bowhead_core.session_store import SessionStore
from bowhead_core.source_access_projection import merge_viewer_patch_preserving_provenance
from test_chart_evidence_plan import project
from test_chart_linked_focus import catalog
from test_chart_selected_context import prepared as prepared
from test_chart_time_window import time_prepared
from test_document_store import migrated_db as migrated_db

RAW = b"region,month,revenue,cost\nEast,2026-02,0,5\nWest,2026-01,0.000012345,0.05\nEast,2026-01,52.12345,30.16789\nWest,2026-02,,-5\n"


def arguments(prepared, kind: str):
    *_, data, _, raw = prepared
    bid = next(candidate["id"] for candidate in catalog(raw)["candidates"] if candidate["kind"] == kind)
    views = [
        {"id": "trend", "title": "Revenue by region", "hint": "trend", "force_kind": "line", "series": ["revenue"]},
        {"id": "relationship", "title": "Revenue and cost", "hint": "correlation", "force_kind": "scatter", "series": ["revenue", "cost"]},
        {"id": "detail", "title": "Original observations", "hint": "raw", "force_kind": "table", "series": ["revenue", "cost"]},
    ]
    interactions = [{"preset": "linked_focus", "from_view": "trend", "to_views": ["relationship", "detail"], "binding_id": bid}]
    if kind == "time":
        interactions.append({"preset": "time_window", "view_ids": [view["id"] for view in views], "binding_id": bid})
    return {
        "data_artifact_id": data.id,
        "title": "Regional observations",
        "views": views,
        "layout": "grid2",
        "presentation_plan": {"version": 1, "interactions": interactions},
    }


@pytest.mark.parametrize("kind", ["row", "entity", "time"])
async def test_tool_preserves_compound_grain_through_source_projection_and_layout_edits(prepared, kind):
    value = await time_prepared(prepared, RAW)
    _, canvas, _, ctx, _, data, _, _ = value
    result = await _add_chart(arguments(value, kind), ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    spec = section.payload["spec"]
    plan = spec["presentation_plan"]
    descriptor = next(iter(plan["bindings"].values()))
    assert descriptor["observation_keys"] == ["region", "month"]
    assert all([column["key"] for column in view["spec"]["columns"][:2]] == ["region", "month"] for view in spec["views"])
    normalized, _ = normalize_chart_for_rendering_v1(spec["views"][0]["spec"])
    missing = next(row for row in normalized["rows"] if row["month"] == "2026-02")
    assert missing["__pivot__East"] == 0 and missing["__pivot__West"] is None
    safe, aliases, access, validity = await project(value, section)
    assert next(iter(safe["spec"]["presentation_plan"]["bindings"].values()))["availability"] == "available"
    safe["spec"]["layout"] = {"type": "stack", "children": [{"view": view["id"]} for view in spec["views"]]}
    patch = merge_viewer_patch_preserving_provenance(
        section.to_report_section(),
        {"spec": safe["spec"]},
        alias_map=aliases,
        access_by_identity=access,
        evidence_validity_by_digest=validity,
        section_kind="chart",
        section_id=section.id,
        allow_label_edit=True,
    )
    validate_stored_composition(patch["spec"])
    await canvas.patch_section(section_id=section.id, patch=patch, expected_rev=1)
    assert (await canvas.list_sections(section.canvas_id))[0].payload["spec"]["presentation_plan"] == plan
    denied, *_ = await project(value, section, denied=[data.id])
    assert next(iter(denied["spec"]["presentation_plan"]["bindings"].values())) == {"kind": kind, "availability": "unavailable"}


async def test_send_reads_exact_compound_tuples_and_the_declared_time_column(prepared):
    value = await time_prepared(prepared, RAW)
    artifacts, canvas, _, ctx, _, data, _, _ = value
    result = await _add_chart(arguments(value, "time"), ctx)
    root = (await canvas.list_sections(result["canvas_id"]))[0]
    sessions = SessionStore(artifacts.db_path)
    await sessions.ensure_session(session_id=ctx.session_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    await sessions.pin_session_attachments(session_id=ctx.session_id, artifact_ids=[data.id])
    plan = root.payload["spec"]["presentation_plan"]
    ref = {
        "fileId": data.id,
        "filename": data.filename,
        "mime": data.mime,
        "size": data.size,
        "referenceKind": "result",
        "selectedContext": {
            "version": 1,
            "sectionId": root.id,
            "sectionRev": root.rev,
            "viewId": "detail",
            "bindingId": plan["view_bindings"]["detail"],
            "datasetDigest": plan["dataset"]["content_digest"],
            "measureKeys": ["revenue"],
            "window": {"lower": "2026-02", "upper": "2026-02"},
            "tuples": [["East", "2026-02"], ["West", "2026-02"]],
        },
    }
    facts = (await resolve_selected_contexts(artifacts, [ref], user_id=ctx.user_id, workspace_id=ctx.workspace_id, session_id=ctx.session_id))[0]
    assert facts["observations"] == [{"region": "East", "month": "2026-02", "revenue": 0}, {"region": "West", "month": "2026-02", "revenue": None}]
    forged = copy.deepcopy(ref)
    forged["selectedContext"]["tuples"] = [["2026-02", "West"]]
    with pytest.raises(ValueError, match="chart_selection_unavailable"):
        await resolve_selected_contexts(artifacts, [forged], user_id=ctx.user_id, workspace_id=ctx.workspace_id, session_id=ctx.session_id)


def test_native_admission_excludes_duplicate_aggregates_and_ambiguous_display_aliases():
    descriptor = {"kind": "row", "observation_keys": ["region", "month"], "measure_keys": ["revenue"]}
    columns = [{"key": "region", "type": "categorical"}, {"key": "month", "type": "date"}, {"key": "revenue", "type": "numeric"}]
    rows = [
        {"region": "East", "month": "2026-01", "revenue": 0},
        {"region": "East", "month": "2026-02", "revenue": None},
        {"region": "West", "month": "2026-01", "revenue": 0.000012345},
    ]
    child = {"kind": "line", "columns": columns, "rows": rows}
    assert _eligible_view(child, descriptor)
    assert not _eligible_view({**child, "rows": [*rows, rows[0]]}, descriptor)
    aliases = [{"region": 1, "month": "2026-01", "revenue": 5}, {"region": "1", "month": "2026-02", "revenue": 7}]
    assert not _eligible_view({**child, "rows": aliases}, descriptor)
    assert _eligible_view({**child, "kind": "table", "rows": aliases}, descriptor)
