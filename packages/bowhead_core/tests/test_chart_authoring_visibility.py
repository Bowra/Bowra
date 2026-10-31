"""Visibility choices use the existing chart renderer and defaults."""

from pathlib import Path

import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.canvas_tools import _add_chart
from test_add_chart_validation import _FakeArtifact, _RichArtifactStore, _StubCtx


@pytest.fixture
async def setup_chart(tmp_path: Path):
    store = CanvasStore(tmp_path / "canvas.db")
    await store.initialize()
    artifact = _FakeArtifact(id="art_aaaaaaaaaaaa", filename="regions.csv", path="regions.csv")
    artifacts = _RichArtifactStore([artifact], {artifact.path: b"region,revenue\nA,12\nB,21\n"})
    context = _StubCtx(artifact_store=artifacts, canvas_store=store)
    args = {"data_artifact_id": artifact.id, "title": "Regions", "hint": "rank", "force_kind": "bar"}
    return store, context, args


@pytest.mark.parametrize("labels", [True, False])
async def test_explicit_visibility_survives_storage_and_dedup(setup_chart, labels: bool) -> None:
    store, context, args = setup_chart
    result = await _add_chart({**args, "show_legend": False, "show_data_labels": labels}, context)
    assert "error" not in result
    section = (await store.list_sections(result["canvas_id"]))[0]
    assert section.payload["spec"]["show_legend"] is False
    assert section.payload["spec"]["show_data_labels"] is labels
    repeated = await _add_chart({**args, "show_legend": False, "show_data_labels": labels}, context)
    assert repeated["section_id"] == section.id
    changed = await _add_chart({**args, "show_legend": False, "show_data_labels": not labels}, context)
    assert changed["section_id"] != section.id


async def test_omitted_labels_preserve_responsive_default(setup_chart) -> None:
    store, context, args = setup_chart
    result = await _add_chart(args, context)
    section = (await store.list_sections(result["canvas_id"]))[0]
    assert "show_data_labels" not in section.payload["spec"]
    assert section.payload["spec"]["show_legend"] is True


async def test_composed_views_inherit_and_override_visibility(setup_chart) -> None:
    store, context, args = setup_chart
    result = await _add_chart(
        {
            **args,
            "show_data_labels": True,
            "views": [
                {"id": "trend", "title": "Trend", "series": ["revenue"], "force_kind": "bar"},
                {"id": "table", "title": "Table", "series": ["revenue"], "force_kind": "table", "show_data_labels": False},
            ],
        },
        context,
    )
    assert "error" not in result
    section = (await store.list_sections(result["canvas_id"]))[0]
    views = {view["id"]: view["spec"] for view in section.payload["spec"]["views"]}
    assert views["trend"]["show_data_labels"] is True
    assert views["table"]["show_data_labels"] is False


@pytest.mark.parametrize("value", [None, "false", 0, [], {}])
async def test_invalid_visibility_is_rejected_before_writes(setup_chart, value) -> None:
    store, context, args = setup_chart
    result = await _add_chart({**args, "show_data_labels": value}, context)
    assert result["path"] == "/show_data_labels"
    assert await store.get_canvas_by_session(context.session_id) is None


async def test_composed_chart_with_different_view_visibility_remains_distinct(setup_chart) -> None:
    _, context, args = setup_chart
    views = [
        {"id": "bars", "title": "Bars", "series": ["revenue"], "force_kind": "bar"},
        {"id": "details", "title": "Details", "series": ["revenue"], "force_kind": "table"},
    ]
    first = await _add_chart({**args, "views": views}, context)
    changed = await _add_chart({**args, "views": [{**views[0], "show_legend": False}, views[1]]}, context)
    assert "error" not in first and "error" not in changed
    assert first["section_id"] != changed["section_id"]


async def test_invalid_child_visibility_rejects_composition_without_writing(setup_chart) -> None:
    store, context, args = setup_chart
    views = [
        {"id": "bars", "title": "Bars", "series": ["revenue"], "force_kind": "bar"},
        {"id": "details", "title": "Details", "series": ["revenue"], "force_kind": "table", "show_legend": "false"},
    ]
    result = await _add_chart({**args, "views": views}, context)
    assert result["error_kind"] == "invalid_chart_expression"
    assert "show_legend" in result["error"]
    assert await store.get_canvas_by_session(context.session_id) is None
