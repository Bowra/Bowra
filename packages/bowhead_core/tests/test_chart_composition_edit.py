"""Native composed figures preserve their root through edits and restore."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from bowhead_core.canvas_store import CanvasRevConflictError, CanvasStore
from bowhead_core.canvas_tools import _add_chart
from bowhead_core.chart.expression import ChartExpressionError
from test_add_chart_validation import _FakeArtifact, _RichArtifactStore, _StubCtx


async def test_empty_annotation_plan_returns_a_corrective_error_before_reads_or_writes() -> None:
    result = await _add_chart({"presentation_plan": {"version": 1, "annotations": []}}, _StubCtx())
    assert result["error_kind"] == "invalid_chart_expression"
    assert result["code"] == "plan_shape"
    assert result["path"] == "/presentation_plan/annotations"


@pytest.fixture
async def prepared(tmp_path: Path):
    store = CanvasStore(tmp_path / "canvas.db")
    await store.initialize()
    art = _FakeArtifact(id="art_aaaaaaaaaaaa", filename="regions.csv", path="regions.csv")
    artifacts = _RichArtifactStore([art], {art.path: b"region,revenue\nA,12\nB,21\n"})
    result = await _add_chart(
        {
            "data_artifact_id": art.id,
            "title": "Regions",
            "hint": "compare",
            "column_units": {"revenue": "USD"},
            "column_display": {"revenue": {"decimal_places": 3, "decimal_mode": "fixed"}},
            "views": [
                {"id": "comparison", "title": "Comparison", "hint": "compare", "series": ["revenue"], "force_kind": "bar"},
                {"id": "details", "title": "Values", "hint": "raw", "series": ["revenue"], "force_kind": "table"},
            ],
        },
        _StubCtx(artifact_store=artifacts, canvas_store=store),
    )
    assert "error" not in result
    section = (await store.list_sections(result["canvas_id"]))[0]
    return store, section


async def test_child_edit_reload_conflict_and_deleted_restore_preserve_one_root(prepared) -> None:
    store, section = prepared
    original = copy.deepcopy(section.payload)
    for view in original["spec"]["views"]:
        column = next(c for c in view["spec"]["columns"] if c["key"] == "revenue")
        assert column["display"] == {"decimal_places": 3, "decimal_mode": "fixed"}
        assert column["currency_code"] == "USD"
    changed = copy.deepcopy(original)
    changed["spec"]["views"][1]["spec"]["kind"] = "kpi"
    updated = await store.patch_section(section_id=section.id, patch=changed, expected_rev=1)
    assert updated.rev == 2
    assert updated.payload["spec"]["views"][0] == original["spec"]["views"][0]
    assert len(await store.list_sections(section.canvas_id)) == 1
    with pytest.raises(CanvasRevConflictError):
        await store.patch_section(section_id=section.id, patch=original, expected_rev=1)
    assert len(await store.list_revisions(canvas_id=section.canvas_id)) == 2
    await store.delete_section(section_id=section.id)
    deleted = next(rev for rev in await store.list_revisions(canvas_id=section.canvas_id) if rev.op == "delete")
    assert await store.restore_deleted_sections(canvas_id=section.canvas_id, revision_id=deleted.id) == [section.id]
    restored = (await store.list_sections(section.canvas_id))[0]
    assert restored.payload == changed
    assert restored.rev == 2


async def test_layout_and_child_title_round_trip_through_saved_history(prepared) -> None:
    store, section = prepared
    changed = copy.deepcopy(section.payload)
    changed["spec"]["layout"] = {"type": "grid2", "children": [{"view": "details"}, {"view": "comparison"}]}
    changed["spec"]["views"][1]["spec"]["title"] = "Original revenue values"
    updated = await store.patch_section(section_id=section.id, patch=changed, expected_rev=1)
    assert updated.rev == 2
    assert (await store.list_sections(section.canvas_id))[0].payload == changed
    assert changed["spec"]["views"][0] == section.payload["spec"]["views"][0]
    reverted = await store.patch_section(section_id=section.id, patch=section.payload, expected_rev=2)
    assert reverted.rev == 3 and reverted.payload == section.payload
    redone = await store.patch_section(section_id=section.id, patch=changed, expected_rev=3)
    assert redone.rev == 4 and redone.payload == changed
    assert len(await store.list_revisions(canvas_id=section.canvas_id)) == 4


@pytest.mark.parametrize("damage", ["duplicate", "missing_layout_target", "dataset", "nested", "plan"])
async def test_invalid_composition_patch_creates_no_revision(prepared, damage: str) -> None:
    store, section = prepared
    changed = copy.deepcopy(section.payload)
    root = changed["spec"]
    if damage == "duplicate":
        root["views"][1]["id"] = root["views"][0]["id"]
    elif damage == "missing_layout_target":
        root["layout"]["children"][1] = {"view": "absent"}
    elif damage == "dataset":
        root["views"][1]["spec"]["data_artifact_id"] = "art_bbbbbbbbbbbb"
    elif damage == "nested":
        root["views"][1]["spec"]["views"] = root["views"][:1]
    else:
        root["presentation_plan"] = {"version": 1}
    with pytest.raises(ChartExpressionError):
        await store.patch_section(section_id=section.id, patch=changed, expected_rev=1)
    current = (await store.list_sections(section.canvas_id))[0]
    assert current.rev == 1 and current.payload == section.payload
    assert len(await store.list_revisions(canvas_id=section.canvas_id)) == 1
