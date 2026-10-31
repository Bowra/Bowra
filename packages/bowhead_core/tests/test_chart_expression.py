"""Native add_chart composition, arithmetic, permissions and export boundaries."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import bowhead_core.canvas_tools as canvas_tools
import pytest
from bowhead_core.canvas_tools import _add_chart, _add_chart_schema
from bowhead_core.chart.expression import composition, derive_columns
from bowhead_core.chart.types import ChartColumn
from bowhead_core.chart_style_export import build_chart_style_export_theme_v2
from bowhead_core.export_ir import iter_export_blocks, normalize_export_snapshot
from bowhead_core.export_snapshot import _freeze_chart_presentations
from bowhead_core.source_access_projection import (
    SourcePatchError,
    StaticSafeSourceMap,
    ViewerSourceAliasMap,
    merge_viewer_patch_preserving_provenance,
    project_section_for_static_export,
    project_sources_for_viewer,
)
from test_add_chart_validation import _CapturingCanvasStore, _FakeArtifact, _RichArtifactStore, _StubCtx
from test_chart_style_export_v2 import _effective_v2
from test_export_ir import _fixture_snapshot


def _args() -> dict:
    return {
        "data_artifact_id": "art_aaaaaaaaaaaa",
        "hint": "compare",
        "title": "Performance",
        "views": [
            {"id": "trend", "title": "Revenue and cost", "hint": "trend", "series": ["revenue", "cost"], "force_kind": "line"},
            {"id": "details", "title": "Exact values", "hint": "raw", "series": ["revenue", "cost"], "force_kind": "table"},
        ],
        "layout": {"type": "stack", "children": [{"view": "trend"}, {"view": "details"}]},
    }


@pytest.mark.parametrize("layout", [None, "stack", "split"], ids=["equal-peers-default", "explicit-vertical", "explicit-primary-detail"])
async def test_chart_authoring_saves_equal_peers_by_default_and_preserves_explicit_layout(layout: str | None) -> None:
    args = _args()
    if layout is None:
        args.pop("layout")
    else:
        args["layout"] = layout
    before = copy.deepcopy(args)
    canvas = _CapturingCanvasStore()
    art = _FakeArtifact(id=args["data_artifact_id"], filename="monthly.csv", path="monthly.csv")
    store = _RichArtifactStore([art], {art.path: b"month,revenue,cost\n2026-01,120,80\n2026-02,160,90\n"})
    result = await _add_chart(args, _StubCtx(artifact_store=store, canvas_store=canvas))
    assert "error" not in result, result
    saved = canvas.appended_payload["spec"]
    assert saved["layout"] == {"type": layout or "grid2", "children": [{"view": "trend"}, {"view": "details"}]}
    assert [view["spec"]["kind"] for view in saved["views"]] == ["line", "table"]
    assert saved["views"][0]["spec"]["rows"] == saved["views"][1]["spec"]["rows"]
    assert args == before


async def test_explicit_child_encodings_do_not_require_redundant_root_hint() -> None:
    args = _args()
    args.pop("hint")
    args["layout"] = "split"
    before = copy.deepcopy(args)
    canvas = _CapturingCanvasStore()
    art = _FakeArtifact(id=args["data_artifact_id"], filename="monthly.csv", path="monthly.csv")
    store = _RichArtifactStore([art], {art.path: b"month,revenue,cost\n2026-01,120,80\n2026-02,160,90\n"})
    ctx = _StubCtx(artifact_store=store, canvas_store=canvas)
    result = await _add_chart(args, ctx)
    assert "error" not in result, result
    assert args == before
    assert canvas.appended_payload["spec"]["layout"]["type"] == "split"
    three = {**args, "views": [*args["views"], {**args["views"][0], "id": "third"}]}
    with pytest.raises(ValueError):
        composition(three)


async def test_registered_composed_chart_does_not_deadlock_on_nested_preparation():
    from bowhead_core.canvas_tools import register_canvas_tools
    from bowhead_core.tool_registry import ToolRegistry

    args = _args()
    art = _FakeArtifact(id=args["data_artifact_id"], filename="monthly.csv", path="monthly.csv")
    store = _RichArtifactStore([art], {art.path: b"month,revenue,cost\n2026-01,120,80\n2026-02,160,90\n"})
    ctx = _StubCtx(artifact_store=store, canvas_store=_CapturingCanvasStore())
    registry = ToolRegistry()
    register_canvas_tools(registry)
    result = await asyncio.wait_for(registry.get("add_chart").handler(args, ctx), timeout=2)
    assert "error" not in result


@pytest.mark.unit
def test_nested_model_json_is_decoded_only_once_and_layout_is_checked() -> None:
    args = _args()
    args["layout"] = json.dumps(args["layout"])
    _, layout = composition(args)
    assert layout["children"] == [{"view": "trend"}, {"view": "details"}]
    args["layout"] = json.dumps(args["layout"])
    with pytest.raises(ValueError):
        composition(args)


@pytest.mark.unit
@pytest.mark.parametrize(
    "layout",
    [
        {"type": "split", "children": [{"view": "trend"}, {"view": "trend"}]},
        {"type": "stack", "children": [{"view": "trend"}, {"view": "missing"}]},
        {"type": "grid2", "children": [{"view": "trend"}]},
    ],
)
def test_invalid_or_incomplete_layout_cannot_discard_evidence(layout: dict) -> None:
    with pytest.raises(ValueError):
        composition({**_args(), "layout": layout})


@pytest.mark.unit
def test_row_arithmetic_preserves_input_and_declares_fractional_percent() -> None:
    rows = [{"revenue": 120, "cost": 100}, {"revenue": 120, "cost": 0}, {"revenue": None, "cost": 10}]
    cols = [ChartColumn("revenue", "Revenue", "numeric", unit="USD"), ChartColumn("cost", "Cost", "numeric", unit="USD")]
    definitions = [
        {
            "key": "change",
            "header": "Relative change",
            "expression": "(a-b)/b",
            "inputs": {"a": "revenue", "b": "cost"},
            "unit": "%",
            "percent_scale": "fraction",
        }
    ]
    output, columns, _, missing = derive_columns(definitions, rows, cols)
    assert output[0]["change"] == pytest.approx(0.2)
    assert output[1]["change"] is None and output[2]["change"] is None
    assert missing == {"change": 2}
    assert "change" not in rows[0]
    assert columns[-1].percent_scale == "fraction"


@pytest.mark.parametrize(
    ("input_unit", "target_unit", "input_value", "expected"),
    [
        ("亿元", "元", 0.0009, 90000),
        ("亿元", "万元", 0.0009, 9),
        ("万元", "元", -0.25, -2500),
        ("USD million", "USD", 1.25, 1250000),
        ("EUR", "EUR thousand", 90000, 90),
        ("EUR Mio.", "EUR", 1.5, 1500000),
        ("USD 1e9", "USD", 1, 1000000000),
        ("RMB", "万元", 90000, 9),
    ],
)
def test_explicit_currency_magnitude_conversion_preserves_original(input_unit, target_unit, input_value, expected):
    rows = [{"amount": input_value}, {"amount": None}]
    columns = [ChartColumn("amount", "Amount", "numeric", unit=input_unit)]
    result, _, metadata, missing = derive_columns(
        [{"key": "converted", "header": "Converted", "convert_from": "amount", "unit": target_unit}], rows, columns
    )
    assert result[0]["converted"] == expected
    assert result[0]["amount"] == input_value and result[1]["converted"] is None
    assert missing == {"converted": 1}
    assert metadata[0]["conversion"]["input_unit"] == input_unit


@pytest.mark.parametrize(
    ("input_unit", "target_unit"), [("USD", "EUR"), ("$", "USD"), ("", "元"), ("%", "元"), ("EUR billion", "EUR"), ("EUR trillion", "EUR")]
)
def test_currency_conversion_does_not_guess_currency_or_exchange_rates(input_unit, target_unit):
    with pytest.raises(ValueError, match="same currency"):
        derive_columns(
            [{"key": "converted", "header": "Converted", "convert_from": "amount", "unit": target_unit}],
            [{"amount": 1}],
            [ChartColumn("amount", "Amount", "numeric", unit=input_unit)],
        )


async def test_chart_conversion_requires_declared_source_unit():
    art = _FakeArtifact(id="art_aaaaaaaaaaaa", filename="data.csv", path="data.csv")
    store = _RichArtifactStore([art], {art.path: b"month,amount\n2026-01,0.0009\n"})
    context = _StubCtx(artifact_store=store, canvas_store=_CapturingCanvasStore())
    args = {
        "data_artifact_id": art.id,
        "title": "Converted",
        "force_kind": "table",
        "derived_columns": [{"key": "converted", "header": "Converted", "convert_from": "amount", "unit": "元"}],
    }
    assert "explicitly declared" in (await _add_chart(args, context))["error"]

    async def put(**kwargs):
        return SimpleNamespace(id="art_bbbbbbbbbbbb")

    store.put_bytes = put
    result = await _add_chart({**args, "column_units": {"amount": "亿元"}}, context)
    assert "error" not in result, result
    assert context.canvas_store.appended_payload["spec"]["rows"][0]["converted"] == 90000
    from bowhead_core.source_access_projection import unregistered_source_like_paths

    assert not unregistered_source_like_paths("chart", context.canvas_store.appended_payload)


@pytest.mark.unit
@pytest.mark.parametrize("expression", ["__import__('os')", "a.__class__", "a[0]", "a**2", "sum(a)", "a+unknown", "True+a", "a+1e309"])
def test_arithmetic_rejects_code_unknown_bindings_and_nonfinite_constants(expression: str) -> None:
    with pytest.raises(ValueError):
        derive_columns(
            [{"key": "derived", "header": "Derived", "expression": expression, "unit": "", "inputs": {"a": "value"}}],
            [{"value": 2}],
            [ChartColumn("value", "Value", "numeric")],
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_composition_uses_one_native_section_and_original_source() -> None:
    art = _FakeArtifact(id="art_aaaaaaaaaaaa", filename="monthly.csv", path="monthly.csv")
    store = _RichArtifactStore([art], {art.path: b"month,revenue,cost\n2026-01,120,80\n2026-02,160,90\n"})
    canvas = _CapturingCanvasStore()
    result = await _add_chart({**_args(), "column_types": {"month": "date"}}, _StubCtx(artifact_store=store, canvas_store=canvas))
    assert result["section_id"] == "sec_test"
    spec = canvas.appended_payload["spec"]
    assert spec["title"] == "Performance"
    assert [view["spec"]["kind"] for view in spec["views"]] == ["line", "table"]
    assert spec["source_meta"]["artifact_id"] == art.id
    assert all("source_meta" not in view["spec"] for view in spec["views"])
    assert all(owner == "u" for _, owner in store.seen)
    assert store.backend.read_paths == [art.path]
    assert store.seen == [(art.id, "u")]
    assert set(_add_chart_schema().input_schema["properties"]) >= {"views", "layout", "derived_columns"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bad_second_view_does_not_append_a_partial_chart() -> None:
    args = _args()
    args["views"][1]["series"] = ["invented"]
    art = _FakeArtifact(id=args["data_artifact_id"], filename="monthly.csv", path="monthly.csv")
    store = _RichArtifactStore([art], {art.path: b"month,revenue,cost\n2026-01,120,80\n2026-02,160,90\n"})
    canvas = _CapturingCanvasStore()
    result = await _add_chart(args, _StubCtx(artifact_store=store, canvas_store=canvas))
    assert result["error_kind"] == "invalid_chart_expression"
    assert result["view_id"] == "details"
    assert canvas.appended_payload is None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("sort_by", []), ("hint", []), ("force_kind", []), ("series", {"revenue": True})])
async def test_malformed_model_view_arguments_return_a_tool_error(field: str, value: object) -> None:
    args = _args()
    args["views"][1][field] = value
    art = _FakeArtifact(id=args["data_artifact_id"], filename="monthly.csv", path="monthly.csv")
    store = _RichArtifactStore([art], {art.path: b"month,revenue,cost\n2026-01,120,80\n2026-02,160,90\n"})
    canvas = _CapturingCanvasStore()
    result = await _add_chart(args, _StubCtx(artifact_store=store, canvas_store=canvas))
    assert result["error_kind"] == "invalid_chart_expression"
    assert canvas.appended_payload is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_declared_view_normalizes_to_the_existing_single_chart() -> None:
    args = _args()
    args["views"] = [args["views"][1]]
    args.pop("layout")
    art = _FakeArtifact(id=args["data_artifact_id"], filename="monthly.csv", path="monthly.csv")
    canvas = _CapturingCanvasStore()
    store = _RichArtifactStore([art], {art.path: b"month,revenue,cost\n2026-01,120,80\n2026-02,160,90\n"})
    result = await _add_chart(args, _StubCtx(artifact_store=store, canvas_store=canvas))
    assert result["normalized"] == "single_view"
    assert canvas.appended_payload["spec"]["kind"] == "table"
    assert "views" not in canvas.appended_payload["spec"]
    assert store.backend.read_paths == [art.path]
    args["force_kind"] = "line"
    canvas.appended_payload = None
    result = await _add_chart(args, _StubCtx(artifact_store=store, canvas_store=canvas))
    assert "omits the primary" in result["error"]
    assert canvas.appended_payload is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_composition_calculates_once_and_child_transforms_do_not_mutate_siblings(monkeypatch: pytest.MonkeyPatch) -> None:
    art = _FakeArtifact(id="art_aaaaaaaaaaaa", filename="regions.csv", path="regions.csv")
    store = _RichArtifactStore([art], {art.path: b"region,revenue,cost\nA,10,1\nB,30,5\nC,20,3\n"})
    canvas = _CapturingCanvasStore()
    calls = 0
    writes: list[dict] = []
    original = canvas_tools.derive_columns

    def derive(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    async def put(**kwargs):
        writes.append(kwargs)
        return SimpleNamespace(id="art_bbbbbbbbbbbb")

    monkeypatch.setattr(canvas_tools, "derive_columns", derive)
    store.put_bytes = put
    args = {
        "data_artifact_id": art.id,
        "hint": "compare",
        "title": "Regional results",
        "derived_columns": [{"key": "gap", "header": "Gap", "expression": "a-b", "inputs": {"a": "revenue", "b": "cost"}, "unit": "USD"}],
        "column_display": {"gap": {"decimal_places": 3, "decimal_mode": "fixed"}},
        "views": [
            {"id": "rank", "title": "Revenue ranking", "hint": "rank", "series": ["revenue"], "sort_by": "revenue", "limit": 1, "force_kind": "bar"},
            {"id": "detail", "title": "Original order and gap", "hint": "raw", "series": ["cost", "gap"], "force_kind": "table"},
        ],
    }
    result = await _add_chart(args, _StubCtx(artifact_store=store, canvas_store=canvas))
    assert "error" not in result
    assert calls == 1
    assert store.backend.read_paths == [art.path]
    assert len(writes) == 1
    root = canvas.appended_payload["spec"]
    assert [row["region"] for row in root["views"][0]["spec"]["rows"]] == ["B"]
    assert [row["region"] for row in root["views"][1]["spec"]["rows"]] == ["A", "B", "C"]
    assert [row["gap"] for row in root["views"][1]["spec"]["rows"]] == [9, 25, 17]
    gap = next(c for c in root["views"][1]["spec"]["columns"] if c["key"] == "gap")
    assert gap["display"] == {"decimal_places": 3, "decimal_mode": "fixed"}
    assert not result.get("presentation_fallbacks")
    assert all(view["spec"]["data_artifact_id"] == "art_bbbbbbbbbbbb" for view in root["views"])
    assert root["source_meta"]["artifact_id"] == art.id

    async def previous(*_args, **_kwargs):
        return [SimpleNamespace(id="sec_test", payload=canvas.appended_payload)]

    canvas.list_sections_appended_by_turn = previous
    repeated = await _add_chart(args, _StubCtx(artifact_store=store, canvas_store=canvas))
    assert repeated["deduped"] == "skipped_degenerate"
    assert len(writes) == 1, "A skipped duplicate must not materialize an unreferenced calculation artifact"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["layout", "calculation"])
async def test_same_source_and_title_preserve_distinct_composed_expressions(change: str) -> None:
    art = _FakeArtifact(id="art_aaaaaaaaaaaa", filename="monthly.csv", path="monthly.csv")
    store = _RichArtifactStore([art], {art.path: b"month,revenue,cost\n2026-01,120,80\n2026-02,160,90\n"})
    canvas = _CapturingCanvasStore()
    args = {**_args(), "column_types": {"month": "date"}}
    if change == "calculation":
        args["derived_columns"] = [{"key": "gap", "header": "Gap", "expression": "a-b", "inputs": {"a": "revenue", "b": "cost"}, "unit": "USD"}]

        async def put(**_kwargs):
            return SimpleNamespace(id="art_bbbbbbbbbbbb")

        store.put_bytes = put

    await _add_chart(args, _StubCtx(artifact_store=store, canvas_store=canvas))
    prior = copy.deepcopy(canvas.appended_payload)

    async def previous(*_args, **_kwargs):
        return [SimpleNamespace(id="sec_previous", payload=prior)]

    canvas.list_sections_appended_by_turn = previous
    if change == "layout":
        args["layout"]["type"] = "split"
    else:
        args["derived_columns"][0]["expression"] = "a+b"
    result = await _add_chart(args, _StubCtx(artifact_store=store, canvas_store=canvas))
    assert "deduped" not in result
    assert canvas.appended_payload != prior


@pytest.mark.unit
@pytest.mark.asyncio
async def test_complete_root_size_is_checked_before_canvas_allocation() -> None:
    art = _FakeArtifact(id="art_aaaaaaaaaaaa", filename="large-labels.csv", path="large-labels.csv")
    csv = "region,revenue,cost\n" + "".join(f"{index}{'x' * 19000},{index + 1},1\n" for index in range(50))
    store = _RichArtifactStore([art], {art.path: csv.encode()})

    class NoAllocation(_CapturingCanvasStore):
        async def ensure_canvas(self, **_kwargs):
            raise AssertionError("Oversized compositions must fail before canvas allocation")

    canvas = NoAllocation()
    result = await _add_chart(
        {
            "data_artifact_id": art.id,
            "hint": "raw",
            "title": "Complete size",
            "views": [
                {"id": "first", "title": "First", "hint": "raw", "series": ["revenue"], "sort_by": "revenue", "limit": 30, "force_kind": "table"},
                {"id": "second", "title": "Second", "hint": "raw", "series": ["cost"], "sort_by": "cost", "limit": 20, "force_kind": "table"},
            ],
        },
        _StubCtx(artifact_store=store, canvas_store=canvas),
    )
    assert result["error_kind"] == "invalid_chart_expression"
    assert "complete chart exceeds" in result["error"]
    assert canvas.appended_payload is None


@pytest.mark.unit
@pytest.mark.parametrize("expression", ["money-people", "money-people*1", "money-people/1"])
def test_equations_reject_known_incompatible_units(expression: str) -> None:
    cols = [ChartColumn("money", "Money", "numeric", unit="USD"), ChartColumn("people", "People", "numeric", unit="people")]
    with pytest.raises(ValueError, match="compatible units"):
        derive_columns([{"key": "gap", "header": "Gap", "expression": expression, "unit": "USD"}], [{"money": 20, "people": 3}], cols)


@pytest.mark.unit
@pytest.mark.parametrize(
    "definition",
    [
        {"key": "source_delta", "expression": "a", "inputs": {"a": "value"}},
        {"key": "delta", "expression": "source", "inputs": {"source": "value"}},
    ],
)
def test_generated_equation_identifiers_cannot_collide_with_provenance_fields(definition: dict) -> None:
    with pytest.raises(ValueError, match="reserved provenance word"):
        derive_columns([{**definition, "header": "Delta", "unit": ""}], [{"value": 2}], [ChartColumn("value", "Value", "numeric")])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calculation_materializes_full_dataset_for_table_completion() -> None:
    art = _FakeArtifact(id="art_aaaaaaaaaaaa", filename="monthly.csv", path="monthly.csv")
    store = _RichArtifactStore([art], {art.path: b"month,revenue,cost\n2026-01,120,80\n2026-02,160,90\n"})
    written = []

    async def put_bytes(**kwargs):
        written.append(kwargs)
        return SimpleNamespace(id="art_bbbbbbbbbbbb")

    store.put_bytes = put_bytes
    canvas = _CapturingCanvasStore()
    args = {
        "data_artifact_id": art.id,
        "title": "Difference",
        "hint": "raw",
        "force_kind": "table",
        "series": ["gap"],
        "derived_columns": [
            {"key": "gap", "header": "Revenue minus cost", "expression": "a-b", "inputs": {"a": "revenue", "b": "cost"}, "unit": "USD"}
        ],
    }
    result = await _add_chart(args, _StubCtx(artifact_store=store, canvas_store=canvas))
    assert "error" not in result
    spec = canvas.appended_payload["spec"]
    assert spec["data_artifact_id"] == "art_bbbbbbbbbbbb"
    assert spec["source_meta"]["artifact_id"] == art.id
    assert [row["gap"] for row in spec["rows"]] == [40, 70]
    assert written[0]["derived_from"] == art.id
    assert b"month,revenue,cost,gap" in written[0]["content"]


@pytest.mark.unit
def test_export_composition_preserves_every_child_chart_in_order() -> None:
    snapshot = _fixture_snapshot()
    sections = snapshot.payload["sections"]
    chart = next(section for section in sections if section["kind"] == "chart")
    base = copy.deepcopy(chart["payload"]["spec"])
    base["data_artifact_id"] = "art_aaaaaaaaaaaa"
    root = {
        **base,
        "title": "Comparison",
        "views": [
            {"id": "first", "kind": "chart", "spec": {**base, "title": "First"}},
            {"id": "second", "kind": "chart", "spec": {**base, "title": "Second"}},
        ],
        "layout": {"type": "split", "children": [{"view": "first"}, {"view": "second"}]},
    }
    chart["payload"]["spec"] = root
    snapshot = replace(snapshot, section_count=snapshot.section_count + len(root["views"]))
    snapshot.payload["request"]["theme"] = build_chart_style_export_theme_v2(_effective_v2(), print_theme=snapshot.payload["request"]["print_theme"])
    _freeze_chart_presentations(sections, theme=snapshot.payload["request"]["theme"], locale="zh-CN")
    ir = normalize_export_snapshot(
        snapshot,
        asset_content_hashes={base["data_artifact_id"]: f"sha256:{'a' * 64}", "art_image": f"sha256:{'b' * 64}", "art_bg": f"sha256:{'c' * 64}"},
    )
    child_charts = [block for block in iter_export_blocks(ir.blocks) if block.source.section_id == chart["id"] and block.block_kind == "visual"]
    assert [block.content["payload"]["spec"]["title"] for block in child_charts] == ["First", "Second"]


@pytest.mark.unit
def test_composed_views_keep_the_existing_source_projection_and_patch_boundary() -> None:
    aliases = ViewerSourceAliasMap.from_root_secret("synthetic-root", workspace_id="ws_1", report_scope_id="ses_1")
    spec = {
        "title": "Comparison",
        "source_meta": {"artifact_id": "art_original"},
        "views": [{"id": "trend", "kind": "chart", "spec": {"title": "Trend", "source_meta": {"artifact_id": "art_private_child"}}}],
    }
    viewer, _ = project_sources_for_viewer(
        [{"id": "sec_1", "kind": "chart", "spec": spec}], [], alias_map=aliases, access_by_identity={"art_original": True}
    )
    static, _ = project_section_for_static_export(
        section_id="sec_1", section_kind="chart", payload={"spec": spec}, source_refs=[], source_map=StaticSafeSourceMap()
    )
    for projected in [viewer[0], static]:
        assert "art_private_child" not in repr(projected)
        assert "source_meta" not in projected["spec"]["views"][0]["spec"]
    with pytest.raises(SourcePatchError, match="unregistered source field"):
        merge_viewer_patch_preserving_provenance(
            {"spec": spec}, {"spec": spec}, section_kind="chart", section_id="sec_1", alias_map=aliases, access_by_identity={}, allow_label_edit=False
        )


@pytest.mark.parametrize("expression,expected", [("end - start", 18.0), ("start + 0.1", 52.22345), ("(end - start) / 100", 0.18)])
def test_derived_arithmetic_uses_decimal_operands_without_rewriting_original_values(expression, expected):
    rows = [{"start": 52.12345, "end": 70.12345, "raw_tail": 18.000000000000007}]
    original = copy.deepcopy(rows)
    columns = [ChartColumn(key=key, header=key, type="numeric") for key in rows[0]]
    computed, _, _, missing = derive_columns([{"key": "delta", "header": "Change", "expression": expression, "unit": ""}], rows, columns)
    assert computed[0]["delta"] == expected
    assert computed[0]["raw_tail"] == original[0]["raw_tail"]
    assert rows == original
    assert missing == {}
