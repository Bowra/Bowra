"""Explicit Table/plot delivery through the real shared Tool and saved spec."""

from __future__ import annotations

import copy
from dataclasses import asdict, replace

import pytest
from bowhead_core.canvas_tools import _add_chart, _add_chart_schema
from bowhead_core.chart.explicit import ExplicitPresentationError, compile_view
from bowhead_core.chart.types import spec_to_dict
from bowhead_core.chart_style_presentation import normalize_chart_for_rendering_v1
from test_add_chart_validation import _CapturingCanvasStore, _FakeArtifact, _RichArtifactStore, _StubCtx
from test_chart_evidence_plan import prepared as prepared
from test_chart_evidence_plan import project

pytestmark = pytest.mark.unit

DATA = b"unit,manager,region,decile,hospitals\nOBU,Manager,South,9,14\nOBU,Manager,South,10,12\nOBU,Manager,South,2,1\n"


def context(raw: bytes = DATA) -> tuple[_StubCtx, _CapturingCanvasStore]:
    artifact = _FakeArtifact("art_aaaaaaaaaaaa", "result.csv", "result.csv")
    store = _RichArtifactStore([artifact], {artifact.path: raw})
    canvas = _CapturingCanvasStore()
    return _StubCtx(artifact_store=store, canvas_store=canvas), canvas


def args(**fields: object) -> dict:
    return {"data_artifact_id": "art_aaaaaaaaaaaa", "title": "Hospitals", **fields}


async def test_default_table_preserves_every_column_and_prepared_order() -> None:
    ctx, canvas = context()
    result = await _add_chart(args(), ctx)
    assert "error" not in result, result
    spec = canvas.appended_payload["spec"]
    assert spec["kind"] == "table"
    assert [column["key"] for column in spec["columns"]] == ["unit", "manager", "region", "decile", "hospitals"]
    assert [row["decile"] for row in spec["rows"]] == [9, 10, 2]
    assert result["display_receipt"]["selected_columns"] == list(spec["rows"][0])
    assert result["display_receipt"]["explicit_presentation"] == spec["explicit_presentation"]
    assert result["display_receipt"]["columns"] == [
        {"key": "unit", "type": "categorical"},
        {"key": "manager", "type": "categorical"},
        {"key": "region", "type": "categorical"},
        {"key": "decile", "type": "numeric"},
        {"key": "hospitals", "type": "numeric"},
    ]
    assert "dimensions" not in result["display_receipt"]
    assert result["display_receipt"]["value_display"] == {"prepared_rows": "not_modified_by_table_formatting"}

    assert ctx.artifact_store.seen == [("art_aaaaaaaaaaaa", "u")]


async def test_exact_table_keeps_constant_region_and_numeric_decile_in_requested_order() -> None:
    ctx, canvas = context()
    requested = args(columns=["region", "decile", "hospitals"])
    before = copy.deepcopy(requested)
    result = await _add_chart(requested, ctx)
    assert "error" not in result, result
    spec = canvas.appended_payload["spec"]
    assert [column["key"] for column in spec["columns"]] == requested["columns"]
    assert spec["rows"][0] == {"region": "South", "decile": 9, "hospitals": 14}
    assert spec["explicit_presentation"]["columns"] == requested["columns"]
    assert requested == before


@pytest.mark.parametrize(
    "columns",
    [["missing"], ["hospital"], ["region", "region"], [], None, 4],
    ids=["absent", "no-substring", "duplicate", "empty", "null", "wrong-type"],
)
async def test_invalid_exact_columns_fail_before_saving(columns: object) -> None:
    ctx, canvas = context()
    result = await _add_chart(args(columns=columns), ctx)
    assert result["error_kind"] == "invalid_chart_expression"
    assert canvas.appended_payload is None


@pytest.mark.parametrize("legacy", [{"series": ["hospitals"]}, {"hint": "rank"}, {"limit": 1}, {"sort_by": "hospitals"}])
async def test_legacy_selectors_cannot_change_an_explicit_request(legacy: dict) -> None:
    ctx, canvas = context()
    result = await _add_chart(args(**legacy), ctx)
    assert result["error_kind"] == "invalid_chart_expression"
    assert canvas.appended_payload is None


async def test_category_axis_and_constant_group_are_explicit_without_type_overrides() -> None:
    ctx, canvas = context()
    result = await _add_chart(args(force_kind="bar", axes={"x": "decile", "y": ["hospitals"], "x_scale": "category"}, group_by=["region"]), ctx)
    assert "error" not in result, result
    spec = canvas.appended_payload["spec"]
    assert [column["key"] for column in spec["columns"]] == ["decile", "region", "hospitals"]
    assert spec["columns"][0]["type"] == "categorical"
    normalized, _ = normalize_chart_for_rendering_v1(spec)
    assert [row["decile"] for row in normalized["rows"]] == ["9", "10", "2"]
    assert normalized["columns"][1]["header"] == "South"
    assert [row["__pivot__South"] for row in normalized["rows"]] == [14, 12, 1]


@pytest.mark.parametrize(
    "fields",
    [
        {"force_kind": "bar"},
        {"force_kind": "bar", "axes": {"x": "decile", "y": ["hospitals"], "x_scale": "category"}},
        {"force_kind": "bar", "axes": {"x": "decile", "y": ["hospitals"]}, "group_by": []},
        {"force_kind": "bar", "axes": {"x": "missing", "y": ["hospitals"], "x_scale": "category"}, "group_by": []},
        {"force_kind": "bar", "axes": {"x": "decile", "y": ["region"], "x_scale": "category"}, "group_by": []},
        {"axes": {"y": ["hospitals"]}},
        {"force_kind": "bar", "columns": "all", "axes": {"x": "decile", "y": ["hospitals"], "x_scale": "category"}, "group_by": []},
    ],
    ids=["no-axes", "no-group-declaration", "no-scale", "missing-x", "text-y", "table-axes", "two-selectors"],
)
async def test_invalid_or_missing_plot_bindings_do_not_guess_or_save(fields: dict) -> None:
    ctx, canvas = context()
    result = await _add_chart(args(**fields), ctx)
    assert result["error_kind"] == "invalid_chart_expression", result
    assert canvas.appended_payload is None


async def test_duplicate_group_cells_are_not_summed() -> None:
    ctx, canvas = context(DATA + b"OBU,Manager,South,9,2\n")
    result = await _add_chart(args(force_kind="line", axes={"x": "decile", "y": ["hospitals"], "x_scale": "category"}, group_by=["region"]), ctx)
    assert "does not sum" in result["error"]
    assert canvas.appended_payload is None


async def test_table_accepts_raw_duplicate_rows_and_preserves_identifier_zeroes() -> None:
    ctx, canvas = context(b"code,value\n001,12\n001,12\n")
    result = await _add_chart(args(), ctx)
    assert "error" not in result, result
    assert canvas.appended_payload["spec"]["rows"] == [{"code": "001", "value": 12}, {"code": "001", "value": 12}]


async def test_repeated_source_headers_are_rejected_before_any_section_write() -> None:
    ctx, canvas = context(b"code,value,value\na,1,2\n")
    result = await _add_chart(args(), ctx)
    assert "headers" in result["error"]
    assert canvas.appended_payload is None


async def test_table_pagination_keeps_all_columns_and_total_count() -> None:
    raw = b"region,decile,value\n" + b"".join(f"South,{i},{100 - i}\n".encode() for i in range(60))
    ctx, canvas = context(raw)
    result = await _add_chart(args(), ctx)
    assert "error" not in result, result
    spec = canvas.appended_payload["spec"]
    assert spec["row_count"] == 60 and len(spec["rows"]) == 50
    assert [column["key"] for column in spec["columns"]] == ["region", "decile", "value"]
    assert result["display_receipt"]["rows_available"] == 60


async def test_plot_keeps_more_than_thirty_categories_without_top_n_or_reordering() -> None:
    raw = b"decile,value\n" + b"".join(f"{i},{i}\n".encode() for i in range(45))
    ctx, canvas = context(raw)
    result = await _add_chart(args(force_kind="bar", axes={"x": "decile", "y": ["value"], "x_scale": "category"}, group_by=[]), ctx)
    assert "error" not in result, result
    spec = canvas.appended_payload["spec"]
    assert len(spec["rows"]) == 45
    assert spec["rows"][0]["value"] == 0
    assert "top_n" not in spec and "series_shown" not in spec
    assert spec["explicit_presentation"]["group_by"] == []


async def test_composed_views_share_one_source_read_and_invalid_view_is_atomic() -> None:
    ctx, canvas = context()
    views = [{"id": "all", "title": "All"}, {"id": "detail", "title": "Details", "columns": ["region", "decile", "hospitals"]}]
    result = await _add_chart(args(views=views), ctx)
    assert "error" not in result, result
    assert ctx.artifact_store.backend.read_paths == ["result.csv"]
    spec = canvas.appended_payload["spec"]
    assert [len(view["spec"]["columns"]) for view in spec["views"]] == [5, 3]
    ctx, canvas = context()
    views[1]["columns"] = ["absent"]
    result = await _add_chart(args(views=views), ctx)
    assert result["view_id"] == "detail"
    assert canvas.appended_payload is None


def test_registered_chart_contract_declares_exact_columns_axes_and_grouping() -> None:
    props = _add_chart_schema().input_schema["properties"]
    assert {"columns", "axes", "group_by"} <= props.keys()
    assert not {"series", "hint", "limit", "sort_by"} & props.keys()
    assert "series" not in props["views"]["items"]["properties"]


def test_saved_explicit_binding_survives_serialization_and_source_type_inference() -> None:
    from bowhead_core.canvas_tools import _parse_csv_with_types

    rows, cols = _parse_csv_with_types(DATA)
    before = [asdict(column) for column in cols]
    spec = compile_view(
        args(force_kind="bar", axes={"x": "decile", "y": ["hospitals"], "x_scale": "category"}, group_by=[]),
        rows,
        cols,
        artifact_id="art_aaaaaaaaaaaa",
        raw=DATA,
    )
    saved = spec_to_dict(spec)
    assert saved["explicit_presentation"]["group_by"] == []
    assert [asdict(column) for column in cols] == before
    assert rows[0]["decile"] == 9
    with pytest.raises(ExplicitPresentationError, match="absent"):
        compile_view(args(columns=["absent"]), rows, cols, artifact_id="art_aaaaaaaaaaaa", raw=DATA)


async def test_source_failure_does_not_find_a_replacement_or_modify_an_existing_result() -> None:
    ctx, canvas = context()
    previous = {"spec": {"title": "Previous", "rows": [{"value": 1}]}}
    canvas.appended_payload = previous
    result = await _add_chart({**args(), "data_artifact_id": "art_bbbbbbbbbbbb"}, ctx)
    assert result["error_kind"] == "artifact_not_found"
    assert canvas.appended_payload is previous
    assert ctx.artifact_store.backend.read_paths == []


async def test_scatter_uses_declared_numeric_axes_and_constant_group() -> None:
    ctx, canvas = context()
    result = await _add_chart(args(force_kind="scatter", axes={"x": "decile", "y": ["hospitals"], "x_scale": "linear"}, group_by=["region"]), ctx)
    assert "error" not in result, result
    spec = canvas.appended_payload["spec"]
    assert [(column["key"], column["role"], column["type"]) for column in spec["columns"]] == [
        ("decile", "x", "numeric"),
        ("region", "color", "categorical"),
        ("hospitals", "y", "numeric"),
    ]
    assert [row["decile"] for row in spec["rows"]] == [9, 10, 2]


def test_explicit_axes_validate_declared_units_without_reassigning_fields() -> None:
    from bowhead_core.chart.types import ChartColumn

    raw = b"label,count,cost\nA,4,20\nB,3,10\n"
    rows = [{"label": "A", "count": 4, "cost": 20}, {"label": "B", "count": 3, "cost": 10}]
    cols = [
        ChartColumn(key="label", header="label", type="categorical"),
        ChartColumn(key="count", header="count", type="numeric", unit="people"),
        ChartColumn(key="cost", header="cost", type="numeric", unit="USD"),
    ]
    request = args(force_kind="bar", axes={"x": "label", "y": ["count", "cost"], "x_scale": "category"}, group_by=[])
    with pytest.raises(ExplicitPresentationError, match="different declared units"):
        compile_view(request, rows, cols, artifact_id="art_aaaaaaaaaaaa", raw=raw)
    request["force_kind"] = "dual_axis"
    request["axes"]["y_right"] = ["cost"]
    spec = compile_view(request, rows, cols, artifact_id="art_aaaaaaaaaaaa", raw=raw)
    assert [(column.key, column.y_axis_id) for column in spec.columns if column.role == "y"] == [("count", 0), ("cost", 1)]
    cols[2].unit = "people"
    with pytest.raises(ExplicitPresentationError, match="same declared unit"):
        compile_view(request, rows, cols, artifact_id="art_aaaaaaaaaaaa", raw=raw)
    cols[1].unit, cols[2].unit = "USD", "USD million"
    request["force_kind"] = "bar"
    request["axes"].pop("y_right")
    spec = compile_view(request, rows, cols, artifact_id="art_aaaaaaaaaaaa", raw=raw)
    assert [column.unit for column in spec.columns if column.role == "y"] == ["USD", "USD million"]


async def test_plot_with_only_missing_values_fails_without_creating_a_chart() -> None:
    ctx, canvas = context(b"label,value\nA,\nB,\n")
    result = await _add_chart(args(force_kind="bar", axes={"x": "label", "y": ["value"], "x_scale": "category"}, group_by=[]), ctx)
    assert "no usable observations" in result["error"]
    assert canvas.appended_payload is None


async def test_kpi_rejects_multiple_values_without_choosing_the_first() -> None:
    ctx, canvas = context()
    result = await _add_chart(args(force_kind="kpi", axes={"y": ["decile", "hospitals"]}, group_by=[]), ctx)
    assert "exactly one" in result["error"]
    assert canvas.appended_payload is None


@pytest.mark.parametrize("raw", [b"label,value\n\xff,1\n", b"label,value\nA,1,unbound\n"])
async def test_malformed_exact_source_fails_before_writing(raw: bytes) -> None:
    ctx, canvas = context(raw)
    result = await _add_chart(args(), ctx)
    assert "error" in result
    assert canvas.appended_payload is None


@pytest.mark.parametrize("series_profile", [False, True])
async def test_explicit_analytical_profile_preserves_its_own_declared_fields(prepared, series_profile) -> None:
    from test_chart_analytical import analytical_case

    value = await analytical_case(prepared, series=series_profile)
    _, canvas, _, ctx, request, *_ = value
    result = await _add_chart(request, ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    child = section.payload["spec"]["views"][0]["spec"]
    assert child["explicit_presentation"]["mode"] == "analytical"
    assert child["row_count"] == len(child["rows"])
    safe, *_ = await project(value, section)
    assert safe["spec"]["views"][0]["spec"]["explicit_presentation"] == child["explicit_presentation"]


async def test_exact_table_survives_real_store_projection_and_frozen_export(prepared) -> None:
    from bowhead_core.export_ir import iter_export_blocks, normalize_export_snapshot
    from test_export_snapshot import _request

    artifacts, canvas, snapshots, ctx, _, data, _, _ = prepared
    result = await _add_chart({"data_artifact_id": data.id, "title": "Exact original fields", "columns": ["revenue", "region"]}, ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    spec = section.payload["spec"]
    assert spec["explicit_presentation"]["columns"] == ["revenue", "region"]
    safe, *_ = await project(prepared, section)
    assert safe["spec"]["explicit_presentation"] == spec["explicit_presentation"]

    async def projection(_canvas, sections):
        dto, *_ = await project(prepared, sections[0])
        return [
            replace(sections[0], payload={key: value for key, value in dto.items() if key not in {"id", "kind", "rev", "parent_id", "order_index"}})
        ]

    candidate = await snapshots.build_candidate(
        canvas_id=section.canvas_id, request=replace(_request(), requested_by="u", locale="en-US"), project_sections=projection
    )
    snapshot = await snapshots.commit_candidate(
        candidate,
        expected_projection_token=candidate.content_projection.projection_token,
        expected_surface_token=candidate.surface_projection.surface_token,
    )
    blocks = list(iter_export_blocks(normalize_export_snapshot(snapshot, asset_content_hashes={data.id: "sha256:" + "a" * 64}).blocks))
    tables = [block for block in blocks if block.block_kind == "visual"]
    assert len(tables) == 1
    assert tables[0].content["payload"]["spec"]["explicit_presentation"] == spec["explicit_presentation"]
    assert tables[0].content["payload"]["spec"]["rows"] == spec["rows"]


@pytest.mark.parametrize("root_field,value", [("columns", ["region"]), ("axes", {"y": ["hospitals"]}), ("force_kind", "table"), ("group_by", [])])
async def test_composition_does_not_silently_ignore_root_selection(root_field, value) -> None:
    ctx, canvas = context()
    result = await _add_chart(args(views=[{"id": "detail", "title": "Prepared fields"}], **{root_field: value}), ctx)
    assert "does not accept" in result["error"]
    assert canvas.appended_payload is None


async def test_unknown_selector_is_rejected_instead_of_ignored() -> None:
    ctx, canvas = context()
    result = await _add_chart(args(dimensions=["region"]), ctx)
    assert "Unknown explicit presentation arguments" in result["error"]
    assert canvas.appended_payload is None


@pytest.mark.parametrize(
    "mutation",
    ["binding", "drop-column", "reorder-columns", "kind", "child-kind", "rows", "render-projection", "child-render-projection", "preserve"],
)
async def test_viewer_edit_preserves_explicit_bindings_and_prepared_values(prepared, mutation) -> None:
    from bowhead_core.source_access_projection import SourcePatchError, merge_viewer_patch_preserving_provenance

    _, canvas, _, ctx, _, data, _, _ = prepared
    request = {"data_artifact_id": data.id, "title": "Original fields"}
    if mutation in {"child-kind", "child-render-projection"}:
        request["views"] = [{"id": "all", "title": "All fields"}, {"id": "detail", "title": "Detail"}]
    result = await _add_chart(request, ctx)
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    safe, aliases, access, validity = await project(prepared, section)
    changed = copy.deepcopy(safe["spec"])
    if mutation == "binding":
        changed["explicit_presentation"]["columns"] = ["revenue"]
    elif mutation == "drop-column":
        changed["columns"] = changed["columns"][:1]
    elif mutation == "reorder-columns":
        changed["columns"].reverse()
    elif mutation == "kind":
        changed["kind"] = "bar"
    elif mutation == "child-kind":
        changed["views"][1]["spec"]["kind"] = "bar"
    elif mutation == "rows":
        changed["rows"][0]["revenue"] = 999
    elif mutation == "render-projection":
        changed["_explicit_pivoted"] = True
    elif mutation == "child-render-projection":
        changed["views"][1]["spec"]["_explicit_pivoted"] = True
    else:
        changed.pop("explicit_presentation")
        changed["show_data_labels"] = True
    kwargs = {
        "alias_map": aliases,
        "access_by_identity": access,
        "evidence_validity_by_digest": validity,
        "section_kind": "chart",
        "section_id": section.id,
        "allow_label_edit": True,
    }
    if mutation == "preserve":
        merged = merge_viewer_patch_preserving_provenance(section.to_report_section(), {"spec": changed}, **kwargs)
        assert merged["spec"]["explicit_presentation"] == section.payload["spec"]["explicit_presentation"]
        assert merged["spec"]["show_data_labels"] is True
    else:
        with pytest.raises(SourcePatchError, match="composition observation" if mutation == "child-render-projection" else "Explicit|Internal"):
            merge_viewer_patch_preserving_provenance(section.to_report_section(), {"spec": changed}, **kwargs)


@pytest.mark.parametrize("code", ["001.20", " 001 "])
async def test_default_table_preserves_source_code_text_with_leading_zeroes(code: str) -> None:
    ctx, canvas = context(f"code,value\n{code},1\n".encode())
    result = await _add_chart(args(), ctx)
    assert "error" not in result, result
    assert canvas.appended_payload["spec"]["rows"][0]["code"] == code


async def test_unbound_unsafe_numeric_identifier_does_not_block_explicit_marks() -> None:
    ctx, canvas = context(b"code,decile,hospitals\n9007199254740993,9,14\n9007199254740995,10,12\n")
    result = await _add_chart(args(force_kind="bar", axes={"x": "decile", "y": ["hospitals"], "x_scale": "category"}, group_by=[]), ctx)
    assert "error" not in result, result
    assert [column["key"] for column in canvas.appended_payload["spec"]["columns"]] == ["decile", "hospitals"]


async def test_date_group_cannot_replace_explicit_category_axis_during_export() -> None:
    ctx, canvas = context(b"decile,month,hospitals\n9,2026-02,14\n10,2026-02,12\n")
    result = await _add_chart(args(force_kind="line", axes={"x": "decile", "y": ["hospitals"], "x_scale": "category"}, group_by=["month"]), ctx)
    assert "error" not in result, result
    spec = canvas.appended_payload["spec"]
    normalized, _ = normalize_chart_for_rendering_v1(spec)
    repeated, _ = normalize_chart_for_rendering_v1(normalized)
    assert normalized["columns"][0]["key"] == "decile"
    assert [row["decile"] for row in normalized["rows"]] == ["9", "10"]
    assert normalized == repeated


async def test_scatter_date_group_is_retained_in_export_shape() -> None:
    raw = "x,y,month\n" + "\n".join(f"{index},{index + 10},2026-{index + 1:02d}" for index in range(9))
    ctx, canvas = context(raw.encode())
    result = await _add_chart(args(force_kind="scatter", axes={"x": "x", "y": ["y"], "x_scale": "linear"}, group_by=["month"]), ctx)
    assert "error" not in result, result
    _, shape = normalize_chart_for_rendering_v1(canvas.appended_payload["spec"])
    assert shape["seriesCount"] == 9


async def test_plot_receipt_uses_declared_fields_without_observation_context_roles() -> None:
    from bowhead_core.chart.display_receipt import chart_display_receipt

    ctx, canvas = context()
    result = await _add_chart(args(force_kind="line", axes={"x": "decile", "y": ["hospitals"], "x_scale": "category"}, group_by=["region"]), ctx)
    assert "error" not in result, result
    spec = canvas.appended_payload["spec"]
    spec["columns"].append({"key": "year", "header": "Year", "type": "date"})
    for row in spec["rows"]:
        row["year"] = 2026
    before = copy.deepcopy(spec)
    receipt = chart_display_receipt(spec)
    assert receipt["selected_columns"] == ["decile", "region", "hospitals"]
    assert [column["key"] for column in receipt["columns"]] == ["decile", "region", "hospitals"]
    assert [column["key"] for column in receipt["dimensions"]] == ["decile", "region"]
    assert receipt["horizontal_axis"]["column"] == "decile"
    assert spec == before
