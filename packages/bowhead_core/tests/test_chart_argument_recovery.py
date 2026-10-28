"""Native authoring recovery preserves every validated observation and source."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.canvas_tools import _add_chart
from bowhead_core.chart.expression import ChartExpressionError, composition, structured
from bowhead_core.chart.presentation_plan import ChartPlanError, parse_plan_input
from bowhead_core.export_ir import iter_export_blocks, normalize_export_snapshot
from test_add_chart_validation import _CapturingCanvasStore, _FakeArtifact, _RichArtifactStore, _StubCtx
from test_chart_expression import _args
from test_export_ir import _fixture_snapshot

pytestmark = pytest.mark.unit


def _source() -> _RichArtifactStore:
    art = _FakeArtifact(id="art_aaaaaaaaaaaa", filename="monthly.csv", path="monthly.csv")
    return _RichArtifactStore([art], {art.path: b"month,revenue,cost\n2026-01,0.000001,-80\n2026-02,160,\n"})


@pytest.mark.parametrize(
    "layout",
    [
        '{"type":"split","children":[',
        {"type": "split", "children": [{"view": "trend"}, {"view": "trend"}]},
        {"type": "stack", "children": [{"view": "details"}]},
        {"type": "stack", "children": [{"view": "trend"}, {"view": "unknown"}]},
        {"type": "columns", "children": [{"view": "trend"}, {"view": "details"}]},
        None,
    ],
    ids=["truncated-json", "duplicate-view", "omitted-view", "unknown-view", "unknown-geometry", "null"],
)
async def test_bad_layout_saves_all_valid_views_and_exact_values(layout: object) -> None:
    args = {**_args(), "layout": layout}
    before = copy.deepcopy(args)
    canvas = _CapturingCanvasStore()
    source = _source()
    result = await _add_chart(args, _StubCtx(artifact_store=source, canvas_store=canvas))
    assert "error" not in result, result
    assert result["presentation_fallbacks"] == [{"code": "layout_defaulted", "path": "/layout"}]
    spec = canvas.appended_payload["spec"]
    assert spec["layout"] == _args()["layout"]
    assert [view["id"] for view in spec["views"]] == ["trend", "details"]
    assert [view["spec"]["kind"] for view in spec["views"]] == ["line", "table"]
    assert all(view["spec"]["data_artifact_id"] == args["data_artifact_id"] for view in spec["views"])
    assert spec["source_meta"]["artifact_id"] == args["data_artifact_id"]
    for view in spec["views"]:
        assert [row["revenue"] for row in view["spec"]["rows"]] == [0.000001, 160]
        assert [row.get("cost") for row in view["spec"]["rows"]] == [-80, None]
    assert source.backend.read_paths == ["monthly.csv"]
    assert args == before


async def test_missing_child_hints_keep_explicit_chart_types_and_report_defaults() -> None:
    args = _args()
    args.pop("hint")
    for view in args["views"]:
        view.pop("hint")
    before = copy.deepcopy(args)
    canvas = _CapturingCanvasStore()
    result = await _add_chart(args, _StubCtx(artifact_store=_source(), canvas_store=canvas))
    assert "error" not in result, result
    assert result["presentation_fallbacks"] == [
        {"code": "hint_defaulted", "path": "/views/0/hint"},
        {"code": "hint_defaulted", "path": "/views/1/hint"},
    ]
    assert [view["spec"]["kind"] for view in canvas.appended_payload["spec"]["views"]] == ["line", "table"]
    assert args == before


async def test_missing_hint_uses_existing_raw_inference() -> None:
    canvas = _CapturingCanvasStore()
    args = {"data_artifact_id": "art_aaaaaaaaaaaa", "title": "Original observations", "series": ["revenue", "cost"]}
    result = await _add_chart(
        args,
        _StubCtx(artifact_store=_source(), canvas_store=canvas),
    )
    assert "error" not in result, result
    expected = _CapturingCanvasStore()
    await _add_chart({**args, "hint": "raw"}, _StubCtx(artifact_store=_source(), canvas_store=expected))
    assert canvas.appended_payload["spec"] == expected.appended_payload["spec"]


@pytest.mark.parametrize("wrapper", ["fence", "closers"], ids=["complete-json-fence", "extra-closing-braces"])
def test_bounded_nested_json_preserves_literal_notes_and_layout(wrapper: str) -> None:
    layout = _args()["layout"]
    plan = {"version": 1, "evidence_notes": [{"id": "definition", "measure_keys": ["revenue"], "ref_id": "1", "excerpt": "Keep {literal} text."}]}

    def encode(value: dict) -> str:
        raw = json.dumps(value)
        return "```json\n" + raw + "\n```" if wrapper == "fence" else raw + "}]"

    assert composition({**_args(), "layout": encode(layout)})[1] == layout
    assert parse_plan_input(encode(plan)) == plan
    assert structured("```json\n" + json.dumps(_args()["views"]) + "\n```", "views") == _args()["views"]


@pytest.mark.parametrize("damage", ["invented-series", "formula", "source", "truncated-root", "truncated-plan", "forged-plan", "invalid-hint"])
async def test_presentation_recovery_never_defaults_business_or_source_errors(damage: str) -> None:
    args = {**_args(), "layout": "broken layout"}
    if damage == "invented-series":
        args["views"][1]["series"] = ["invented"]
    elif damage == "formula":
        args["derived_columns"] = [{"key": "gap", "header": "Gap", "expression": "a+unknown", "inputs": {"a": "revenue"}, "unit": "USD"}]
    elif damage == "source":
        args["data_artifact_id"] = "art_ffffffffffff"
    elif damage == "truncated-root":
        args = {"_args_parse_error": "arguments were truncated"}
    elif damage == "truncated-plan":
        args["presentation_plan"] = '{"version":1,"evidence_notes":['
    elif damage == "forged-plan":
        args["presentation_plan"] = {"version": 1, "dataset": {"artifact_id": "art_ffffffffffff"}}
    elif damage == "invalid-hint":
        args["views"][0]["hint"] = "invented"
    canvas = _CapturingCanvasStore()
    result = await _add_chart(args, _StubCtx(artifact_store=_source(), canvas_store=canvas))
    assert "error" in result, result
    assert "presentation_fallbacks" not in result
    assert canvas.appended_payload is None


@pytest.mark.parametrize(
    "layout", ["x" * 20_001, {"text": "x" * 20_001}, {"type": float("nan")}], ids=["string-budget", "object-budget", "nonfinite"]
)
async def test_layout_recovery_keeps_resource_limits(layout: object) -> None:
    source = _source()
    canvas = _CapturingCanvasStore()
    result = await _add_chart({**_args(), "layout": layout}, _StubCtx(artifact_store=source, canvas_store=canvas))
    assert result["error_kind"] == "invalid_chart_expression"
    assert canvas.appended_payload is None
    assert source.backend.read_paths == []


@pytest.mark.parametrize("raw", ['{"a":1}{"b":2}', 'prefix {"a":1}', '```json\n{"a":1}\n```\nother text', '{"a":'])
def test_json_compatibility_does_not_extract_partial_or_conflicting_objects(raw: str) -> None:
    with pytest.raises(ValueError):
        structured(raw, "views")
    with pytest.raises(ChartPlanError):
        parse_plan_input(raw)


async def test_six_view_recovery_reload_export_and_strict_edit_boundary(tmp_path: Path) -> None:
    args = _args()
    args["views"] = [{**args["views"][1], "id": f"detail_{index}", "title": f"Detail {index}"} for index in range(6)]
    args["layout"] = "split"
    canvas = CanvasStore(tmp_path / "canvas.db")
    await canvas.initialize()
    result = await _add_chart(args, _StubCtx(artifact_store=_source(), canvas_store=canvas))
    assert "error" not in result, result
    section = (await canvas.list_sections(result["canvas_id"]))[0]
    spec = section.payload["spec"]
    assert [child["view"] for child in spec["layout"]["children"]] == [view["id"] for view in args["views"]]
    assert "presentation_fallbacks" not in spec

    snapshot = _fixture_snapshot()
    chart = next(item for item in snapshot.payload["sections"] if item["kind"] == "chart")
    chart["payload"]["spec"] = copy.deepcopy(spec)
    snapshot = replace(snapshot, section_count=snapshot.section_count + len(spec["views"]))
    ir = normalize_export_snapshot(
        snapshot,
        asset_content_hashes={"art_aaaaaaaaaaaa": f"sha256:{'a' * 64}", "art_image": f"sha256:{'b' * 64}", "art_bg": f"sha256:{'c' * 64}"},
    )
    exported = [block for block in iter_export_blocks(ir.blocks) if block.source.section_id == chart["id"] and block.block_kind == "visual"]
    assert [block.content["payload"]["spec"]["title"] for block in exported] == [view["title"] for view in args["views"]]

    invalid = copy.deepcopy(section.payload)
    invalid["spec"]["layout"]["children"] = [{"view": "detail_0"}]
    with pytest.raises(ChartExpressionError):
        await canvas.patch_section(section_id=section.id, patch=invalid, expected_rev=1)
    reloaded = (await canvas.list_sections(result["canvas_id"]))[0]
    assert reloaded.rev == 1 and reloaded.payload == section.payload
