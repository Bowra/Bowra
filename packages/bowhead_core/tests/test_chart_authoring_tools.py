"""Chart authoring respects the viewer projection, revision and original data."""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.canvas_tools import _add_chart, _add_image, _remove_chart
from bowhead_core.chart.authoring_tools import _simple_chart_kinds, edit_chart, read_chart
from bowhead_core.tool_outcome import classify_tool_outcome
from test_add_chart_validation import _FakeArtifact, _RichArtifactStore, _StubCtx


@pytest.fixture
async def chart(tmp_path, monkeypatch):
    from bowhead_core.agents import _write_guard

    monkeypatch.setattr(_write_guard, "check_write_trust_class", lambda: None)
    store = CanvasStore(tmp_path / "canvas.db")
    await store.initialize()
    artifact = _FakeArtifact(id="art_aaaaaaaaaaaa", filename="values.csv", path="values.csv")
    artifacts = _RichArtifactStore([artifact], {artifact.path: b"date,value\n2026-10-28,12\n2026-10-29,21\n"})
    ctx = _StubCtx(artifact_store=artifacts, canvas_store=store)

    class Bus:
        async def emit(self, event):
            return True

    ctx.parent_bus = Bus()
    result = await _add_chart(
        {
            "data_artifact_id": artifact.id,
            "title": "Values",
            "force_kind": "line",
            "axes": {"x": "date", "y": ["value"], "x_scale": "category"},
            "group_by": [],
            "column_types": {"date": "date"},
        },
        ctx,
    )
    assert "error" not in result

    async def project(section):
        return copy.deepcopy(section.to_report_section())

    ctx.chart_authoring_projection = project
    return store, ctx, result


async def test_read_edit_and_reload_preserve_data_and_source(chart):
    store, ctx, result = chart
    section_id = result["section_id"]
    before = (await store.list_sections(result["canvas_id"]))[0]
    read = await read_chart({"section_id": section_id}, ctx)
    assert read["views"][0]["sample"][0] == {"date": "2026-10-28", "value": 12}
    assert read["views"][0]["display_receipt"]["horizontal_axis"]["elapsed_time_proportional"] is False
    assert read["views"][0]["display_receipt"]["value_display"]["axis_magnitude"] == "renderer_selected_may_change"
    changed = await edit_chart(
        {
            "section_id": section_id,
            "expected_rev": read["rev"],
            "changes": {"title": "Updated", "formats": {"date": {"kind": "date", "pattern": "yyyyMMdd"}}},
        },
        ctx,
    )
    assert changed["rev"] == before.rev + 1
    assert changed["display_receipt"]["dimensions"] == [{"key": "date", "type": "date", "date_pattern": "yyyyMMdd"}]
    after = (await store.list_sections(result["canvas_id"]))[0]
    assert after.payload["spec"]["title"] == "Updated"
    assert after.payload["spec"]["rows"] == before.payload["spec"]["rows"]
    assert after.payload["spec"]["source_meta"] == before.payload["spec"]["source_meta"]
    assert next(c for c in after.payload["spec"]["columns"] if c["key"] == "date")["date_pattern"] == "yyyyMMdd"
    assert (await edit_chart({"section_id": section_id, "expected_rev": before.rev, "changes": {"title": "Stale"}}, ctx))["error"] == "rev_conflict"


async def test_denied_projection_and_invalid_changes_never_write(chart):
    store, ctx, result = chart
    section_id = result["section_id"]
    before = (await store.list_sections(result["canvas_id"]))[0]
    invalid = await edit_chart(
        {"section_id": section_id, "expected_rev": before.rev, "changes": {"formats": {"date": {"kind": "date", "pattern": "YYYY"}}}}, ctx
    )
    assert invalid["path"] == "/changes/formats/date"
    ctx.chart_authoring_projection = None
    assert (await read_chart({"section_id": section_id}, ctx))["error"] == "chart_source_projection_unavailable"
    assert (await edit_chart({"section_id": section_id, "expected_rev": before.rev, "changes": {"title": "Denied"}}, ctx))[
        "error"
    ] == "chart_source_projection_unavailable"
    after = (await store.list_sections(result["canvas_id"]))[0]
    assert after.rev == before.rev and after.payload == before.payload


async def test_failed_chart_edit_blocks_replacement_and_report_rewrite_across_peer_contexts(chart):
    from bowhead_core.agents.report_tools import _write_report

    store, ctx, result = chart
    section_id = result["section_id"]
    before = (await store.list_sections(result["canvas_id"]))[0]
    ctx.chart_authoring_projection = None
    assert (await read_chart({"section_id": section_id}, ctx))["error"] == "chart_source_projection_unavailable"
    peer = copy.copy(ctx)
    assert (await _add_chart({"data_artifact_id": "art_aaaaaaaaaaaa", "title": "Replacement"}, peer))["error"] == "chart_edit_incomplete"
    assert (await _add_image({"artifact_id": "art_aaaaaaaaaaaa"}, peer))["error"] == "chart_edit_incomplete"
    assert (await _remove_chart({"section_ids": [section_id], "reason": "replaced"}, peer))["error"] == "chart_edit_incomplete"
    assert (await _write_report({"content": "Replacement report"}, peer))["error"] == "chart_edit_incomplete"
    after = (await store.list_sections(result["canvas_id"]))[0]
    assert after.rev == before.rev and after.payload == before.payload and not after.superseded

    async def project(section):
        return copy.deepcopy(section.to_report_section())

    ctx.chart_authoring_projection = project
    changed = await edit_chart({"section_id": section_id, "expected_rev": before.rev, "changes": {"title": "Updated"}}, ctx)
    assert "error" not in changed
    assert (await _add_chart({"data_artifact_id": "art_aaaaaaaaaaaa", "title": "Additional chart"}, peer)).get("error") != "chart_edit_incomplete"


@pytest.mark.parametrize("tool_name", ["read_chart", "edit_chart"])
async def test_registered_chart_operations_wait_for_inflight_failed_read_or_edit(chart, monkeypatch, tool_name):
    from bowhead_core.agents.report_tools import register_report_tools
    from bowhead_core.canvas_tools import register_canvas_tools
    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

    from bowhead_core import canvas_revise

    store, ctx, result = chart
    monkeypatch.setattr(canvas_revise, "section_revise_enabled", lambda: True)
    registry = ToolRegistry()
    register_canvas_tools(registry)
    register_report_tools(registry)
    started, release = asyncio.Event(), asyncio.Event()

    async def unavailable(section):
        started.set()
        await release.wait()
        return

    runtime = ToolExecutionContext(
        workspace_id=ctx.workspace_id,
        member_uid="member",
        user_id=ctx.user_id,
        turn_id=ctx.turn_id,
        datarc=None,
        session_id=ctx.session_id,
        canvas_store=store,
        artifact_store=ctx.artifact_store,
        chart_authoring_projection=unavailable,
        parent_bus=ctx.parent_bus,
    )
    args = {"section_id": result["section_id"]}
    if tool_name == "edit_chart":
        args.update({"expected_rev": 1, "changes": {"title": "Changed"}})
    reader = asyncio.create_task(registry.execute(tool_name, args, runtime))
    await started.wait()
    peer = copy.copy(runtime)
    writes = [
        asyncio.create_task(registry.execute("add_chart", {"data_artifact_id": "art_aaaaaaaaaaaa", "title": "Replacement"}, peer)),
        asyncio.create_task(registry.execute("remove_chart", {"section_ids": [result["section_id"]], "reason": "replaced"}, peer)),
        asyncio.create_task(registry.execute("write_report", {"content": "Replacement report"}, peer)),
        asyncio.create_task(registry.execute("add_image", {"artifact_id": "art_aaaaaaaaaaaa"}, peer)),
    ]
    await asyncio.sleep(0)
    assert all(not task.done() for task in writes)
    release.set()
    assert (await reader).result["error"] == "chart_source_projection_unavailable"
    assert all(item.result["error"] == "chart_edit_incomplete" for item in await asyncio.gather(*writes))
    sections = await store.list_sections(result["canvas_id"])
    assert len(sections) == 1 and sections[0].id == result["section_id"] and not sections[0].superseded


async def test_chart_coordination_does_not_serialize_independent_unblocked_writes():
    from bowhead_core.chart.edit_guard import coordinate_chart_handler

    started, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def write(args, ctx):
        nonlocal count
        count += 1
        if count == 2:
            started.set()
        await release.wait()
        return {"ok": True}

    handler = coordinate_chart_handler(write)
    ctx = SimpleNamespace(parent_bus=SimpleNamespace())
    first = asyncio.create_task(handler({}, ctx))
    second = asyncio.create_task(handler({}, ctx))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
    finally:
        release.set()
        await asyncio.gather(first, second)


async def test_chart_failure_does_not_block_unrelated_turns_or_guessed_ids(chart):
    from bowhead_core.chart.edit_guard import blocked

    _, ctx, result = chart
    await edit_chart({"section_id": "unknown", "expected_rev": 1, "changes": {"title": "Bad"}}, ctx)
    assert not await blocked(ctx)
    await edit_chart({"section_id": result["section_id"], "expected_rev": 1, "changes": {"unknown": True}}, ctx)
    assert result["section_id"] in await blocked(ctx)
    other_turn = copy.copy(ctx)
    other_turn.turn_id = "another_turn"
    assert not await blocked(other_turn)


async def test_failed_chart_guard_is_restored_from_recorded_turn_results(chart, tmp_path):
    from bowhead_core.chart.edit_guard import blocked
    from bowhead_core.subagent import _public_tool_result, _tool_result_meta
    from bowhead_core.turn_log_store import TurnLogStore

    _, ctx, result = chart
    log = TurnLogStore(db_path=str(tmp_path / "turn-log.db"))
    await log.initialize()
    ctx.session_store = SimpleNamespace(db_path=log.db_path)
    ctx.chart_authoring_projection = None
    failure = await read_chart({"section_id": result["section_id"]}, ctx)
    assert failure["chart_guarded"] is True
    public_failure, _ = _public_tool_result("read_chart", failure)
    assert public_failure["chart_guarded"] is True and "views" not in public_failure
    await log.record(
        turn_id=ctx.turn_id,
        event_type="tool_result",
        payload={"tool_name": "read_chart", "result_inline": None, "result_meta": _tool_result_meta(public_failure)},
    )
    resumed = copy.copy(ctx)
    resumed.parent_bus = SimpleNamespace()
    assert result["section_id"] in await blocked(resumed)
    assert (await _add_chart({"data_artifact_id": "art_aaaaaaaaaaaa", "title": "Replacement"}, resumed))["error"] == "chart_edit_incomplete"
    success = {"section_id": result["section_id"], "rev": 2, "chart_guarded": False}
    await log.record(
        turn_id=ctx.turn_id,
        event_type="tool_result",
        payload={"tool_name": "edit_chart", "result_inline": None, "result_meta": _tool_result_meta(success)},
    )
    resumed.parent_bus = SimpleNamespace()
    assert not await blocked(resumed)


def test_public_chart_results_do_not_persist_display_receipts():
    from bowhead_core.subagent import _public_tool_result

    receipt = {"dimensions": [{"key": "private_date", "type": "date"}]}
    for tool, result in [
        ("read_chart", {"section_id": "sec_1", "views": [{"display_receipt": receipt}]}),
        ("edit_chart", {"section_id": "sec_1", "display_receipt": receipt, "chart_guarded": False}),
        ("add_chart", {"section_id": "sec_1", "display_receipt": receipt, "views": [{"id": "child", "display_receipt": receipt}]}),
    ]:
        public, _ = _public_tool_result(tool, result)
        assert "private_date" not in str(public)


async def test_unavailable_turn_history_never_permits_a_chart_or_report_write(chart, monkeypatch):
    from bowhead_core.agents.report_tools import _write_report
    from bowhead_core.turn_log_store import TurnLogStore

    _, ctx, result = chart
    ctx.session_store = SimpleNamespace(db_path="unavailable.db")
    ctx.parent_bus = SimpleNamespace()

    async def unavailable(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(TurnLogStore, "list_events", unavailable)
    for value in [
        await read_chart({"section_id": result["section_id"]}, ctx),
        await edit_chart({"section_id": result["section_id"], "expected_rev": 1, "changes": {"title": "Changed"}}, ctx),
        await _add_chart({"data_artifact_id": "art_aaaaaaaaaaaa", "title": "Replacement"}, ctx),
        await _remove_chart({"section_ids": [result["section_id"]], "reason": "replaced"}, ctx),
        await _write_report({"content": "Replacement report"}, ctx),
    ]:
        assert value["error"] == "chart_guard_history_unavailable"


async def test_unverified_dataset_cannot_be_read_or_edited(chart):
    store, ctx, result = chart
    section = (await store.list_sections(result["canvas_id"]))[0]

    async def unverified(current):
        projected = copy.deepcopy(current.to_report_section())
        projected["spec"]["presentation_plan"] = {"dataset": {"availability": "unavailable"}}
        return projected

    ctx.chart_authoring_projection = unverified
    assert (await read_chart({"section_id": section.id}, ctx))["error"] == "chart_source_projection_unavailable"
    assert (await edit_chart({"section_id": section.id, "expected_rev": section.rev, "changes": {"title": "Changed"}}, ctx))[
        "error"
    ] == "chart_source_projection_unavailable"


async def test_other_session_cannot_read_chart(chart):
    _, ctx, result = chart
    ctx.session_id = "another_session"
    assert (await read_chart({"section_id": result["section_id"]}, ctx))["error"] == "chart_unavailable"


async def test_edit_without_live_event_channel_does_not_write(chart):
    store, ctx, result = chart
    section = (await store.list_sections(result["canvas_id"]))[0]
    ctx.parent_bus = None
    response = await edit_chart({"section_id": section.id, "expected_rev": section.rev, "changes": {"title": "Changed"}}, ctx)
    assert response["error"] == "chart_update_unavailable"
    assert (await store.list_sections(result["canvas_id"]))[0].rev == section.rev


async def test_recoverable_nested_json_and_reset(chart):
    store, ctx, result = chart
    section_id = result["section_id"]
    before = (await store.list_sections(result["canvas_id"]))[0]
    changed = await edit_chart(
        {"section_id": section_id, "expected_rev": before.rev, "changes": '{"formats":{"date":{"kind":"date","pattern":"yyyymmdd"}}}}'}, ctx
    )
    assert "error" not in changed
    normalized = (await store.list_sections(result["canvas_id"]))[0]
    assert next(c for c in normalized.payload["spec"]["columns"] if c["key"] == "date")["date_pattern"] == "yyyyMMdd"
    reset = await edit_chart({"section_id": section_id, "expected_rev": changed["rev"], "changes": {}, "reset": {"formats": ["date"]}}, ctx)
    assert "error" not in reset
    updated = (await store.list_sections(result["canvas_id"]))[0]
    assert "date_pattern" not in next(c for c in updated.payload["spec"]["columns"] if c["key"] == "date")


async def test_date_presets_validate_grain_and_preserve_defaults(chart):
    store, ctx, result = chart
    section = (await store.list_sections(result["canvas_id"]))[0]
    read = await read_chart({"section_id": section.id}, ctx)
    assert "compact_date" in read["date_presets"]
    invalid = await edit_chart(
        {"section_id": section.id, "expected_rev": section.rev, "changes": {"formats": {"date": {"kind": "date", "preset": "year_month"}}}}, ctx
    )
    assert "indistinguishable" in invalid["hint"]
    assert (await store.list_sections(result["canvas_id"]))[0].rev == section.rev
    changed = await edit_chart(
        {"section_id": section.id, "expected_rev": section.rev, "changes": {"formats": {"date": {"kind": "date", "preset": "compact_date"}}}}, ctx
    )
    assert "error" not in changed
    reset = await edit_chart(
        {"section_id": section.id, "expected_rev": changed["rev"], "changes": {"formats": {"date": {"kind": "date", "preset": "auto"}}}}, ctx
    )
    assert "error" not in reset
    final = (await store.list_sections(result["canvas_id"]))[0]
    assert "date_pattern" not in next(column for column in final.payload["spec"]["columns"] if column["key"] == "date")


async def test_date_edits_do_not_validate_only_an_unloaded_table_preview(chart):
    store, ctx, result = chart
    section = (await store.list_sections(result["canvas_id"]))[0]
    spec = copy.deepcopy(section.payload["spec"])
    spec["kind"] = "table"
    spec["row_count"] = len(spec["rows"]) + 1
    section = await store.patch_section(section_id=section.id, patch={"spec": spec}, expected_rev=section.rev)
    response = await edit_chart(
        {"section_id": section.id, "expected_rev": section.rev, "changes": {"formats": {"date": {"kind": "date", "preset": "compact_date"}}}}, ctx
    )
    assert "unloaded rows" in response["hint"]
    assert (await store.list_sections(result["canvas_id"]))[0].rev == section.rev


async def test_invalid_preset_shape_returns_a_corrective_error(chart):
    store, ctx, result = chart
    section = (await store.list_sections(result["canvas_id"]))[0]
    response = await edit_chart(
        {"section_id": section.id, "expected_rev": section.rev, "changes": {"formats": {"date": {"kind": "date", "preset": []}}}}, ctx
    )
    assert response["path"] == "/changes/formats/date"


async def test_percent_and_currency_precision_preserve_source_semantics(chart):
    store, ctx, _ = chart
    artifact = _FakeArtifact(id="art_bbbbbbbbbbbb", filename="metrics.csv", path="metrics.csv")
    ctx.artifact_store.artifacts[artifact.id] = artifact
    ctx.artifact_store.artifact_list.append(artifact)
    ctx.artifact_store.backend.files[artifact.path] = b"date,ratio,revenue\n2026-10-28,0.123456,100.25\n"
    result = await _add_chart(
        {
            "data_artifact_id": artifact.id,
            "title": "Metrics",
            "force_kind": "table",
            "column_types": {"date": "date", "ratio": "percent_fraction"},
            "column_units": {"revenue": "CAD"},
        },
        ctx,
    )
    section = next(item for item in await store.list_sections(result["canvas_id"]) if item.id == result["section_id"])
    read = await read_chart({"section_id": section.id}, ctx)
    columns = {column["key"]: column for column in read["views"][0]["columns"]}
    assert columns["ratio"]["editable_format_kinds"] == ["number", "percent"]
    assert columns["revenue"]["editable_format_kinds"] == ["number", "currency"]
    changes = {
        "formats": {
            "ratio": {"kind": "percent", "decimal_places": 2, "decimal_mode": "fixed"},
            "revenue": {"kind": "currency", "decimal_places": 1, "decimal_mode": "max"},
        }
    }
    edited = await edit_chart({"section_id": section.id, "expected_rev": section.rev, "changes": changes}, ctx)
    assert "error" not in edited
    after = next(item for item in await store.list_sections(result["canvas_id"]) if item.id == section.id)
    assert after.payload["spec"]["rows"] == section.payload["spec"]["rows"]
    columns = {column["key"]: column for column in after.payload["spec"]["columns"]}
    assert columns["ratio"]["percent_scale"] == "fraction"
    assert columns["revenue"]["currency_code"] == "CAD"
    assert columns["ratio"]["display"] == {"decimal_places": 2, "decimal_mode": "fixed"}
    assert columns["revenue"]["display"] == {"decimal_places": 1, "decimal_mode": "max"}
    rejected = await edit_chart(
        {
            "section_id": section.id,
            "expected_rev": edited["rev"],
            "changes": {"formats": {"ratio": {"kind": "currency", "decimal_places": 2, "decimal_mode": "fixed"}}},
        },
        ctx,
    )
    assert rejected["path"] == "/changes/formats/ratio"


async def test_historical_kind_edits_preserve_existing_chart_and_source(chart):
    store, ctx, result = chart
    section = (await store.list_sections(result["canvas_id"]))[0]
    historical = copy.deepcopy(section.payload["spec"])
    historical.pop("explicit_presentation")
    await store.patch_section(section_id=section.id, patch={"spec": historical}, by_turn_id=ctx.turn_id)
    before = (await store.list_sections(result["canvas_id"]))[0]
    read = await read_chart({"section_id": before.id}, ctx)
    assert read["supported_kinds"] == ["line", "bar", "hbar", "area_stacked", "table"]
    edited = await edit_chart({"section_id": before.id, "expected_rev": before.rev, "changes": {"kind": "bar"}}, ctx)
    assert "error" not in edited
    after = (await store.list_sections(result["canvas_id"]))[0]
    assert after.payload["spec"]["kind"] == "bar"
    for key in ("rows", "columns", "data_artifact_id", "source_meta"):
        assert after.payload["spec"][key] == before.payload["spec"][key]
    table = await edit_chart({"section_id": before.id, "expected_rev": after.rev, "changes": {"kind": "table"}}, ctx)
    assert "error" not in table


async def test_chart_drawn_with_declared_axes_switches_kind_in_place(chart):
    store, ctx, result = chart
    before = (await store.list_sections(result["canvas_id"]))[0]
    read = await read_chart({"section_id": before.id}, ctx)
    assert "kind" in read["allowed_changes"]
    assert read["supported_kinds"] == ["line", "bar", "hbar", "area_stacked"]
    edited = await edit_chart({"section_id": before.id, "expected_rev": before.rev, "changes": {"kind": "area_stacked"}}, ctx)
    assert "error" not in edited
    after = (await store.list_sections(result["canvas_id"]))[0]
    assert after.id == before.id and after.payload["spec"]["kind"] == "area_stacked"
    for key in ("rows", "columns", "row_count", "data_artifact_id", "source_meta", "explicit_presentation"):
        assert after.payload["spec"][key] == before.payload["spec"][key]
    assert (await read_chart({"section_id": before.id}, ctx))["supported_kinds"] == ["line", "bar", "hbar", "area_stacked"]
    refused = await edit_chart({"section_id": before.id, "expected_rev": after.rev, "changes": {"kind": "pie"}}, ctx)
    assert refused["error"] == "invalid_chart_change" and refused["path"] == "/changes/kind"
    assert (await store.list_sections(result["canvas_id"]))[0].payload == after.payload


def _explicit_spec(kind, axes, group_by, rows):
    keys = [axes["x"], *group_by, *axes["y"]] if "x" in axes else axes["y"]
    return {
        "kind": kind,
        "title": "Values",
        "columns": [{"key": key, "header": key, "type": "numeric" if key in axes["y"] else "categorical"} for key in keys],
        "rows": rows,
        "row_count": len(rows),
        "explicit_presentation": {"version": 1, "mode": "plot", "axes": axes, "group_by": group_by},
    }


_CATEGORY = {"x": "region", "y": ["sales"], "x_scale": "category"}
_TWO_ROWS = [{"region": "East", "sales": 3}, {"region": "West", "sales": 5}]
_GROUPED = [{"region": r, "channel": c, "sales": 1} for r in ("East", "West") for c in ("Online", "Store")]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (_explicit_spec("bar", _CATEGORY, [], _TWO_ROWS), ["line", "bar", "hbar", "area_stacked"]),
        # A group stacks distinct series, so stacked bars become a real choice.
        (_explicit_spec("line", _CATEGORY, ["channel"], _GROUPED), ["line", "bar", "hbar", "area_stacked", "stacked_bar"]),
        (
            _explicit_spec("bar", {**_CATEGORY, "y": ["sales", "cost"]}, [], [{"region": "East", "sales": 3, "cost": 1}]),
            ["bar", "hbar", "stacked_bar"],
        ),
        # A line needs two observations; one prepared row still switches between bar directions.
        (_explicit_spec("bar", _CATEGORY, [], _TWO_ROWS[:1]), ["bar", "hbar"]),
        # Vertical bars have a slot limit that lines and horizontal bars do not share.
        (
            _explicit_spec("line", _CATEGORY, [], [{"region": f"R{index}", "sales": index} for index in range(1001)]),
            ["line", "hbar", "area_stacked"],
        ),
        # Kinds that bind fields differently keep their encoding.
        (_explicit_spec("pie", _CATEGORY, [], _TWO_ROWS), []),
        (_explicit_spec("combo", {**_CATEGORY, "y": ["sales", "cost"]}, [], [{"region": "East", "sales": 3, "cost": 1}]), []),
        (_explicit_spec("scatter", {"x": "cost", "y": ["sales"], "x_scale": "linear"}, [], [{"cost": 1, "sales": 3}]), []),
        (_explicit_spec("kpi", {"y": ["sales"]}, [], [{"sales": 3}]), []),
        ({**_explicit_spec("bar", _CATEGORY, [], _TWO_ROWS), "explicit_presentation": {"version": 1, "mode": "table", "columns": ["region"]}}, []),
        ({**_explicit_spec("bar", _CATEGORY, [], _TWO_ROWS), "row_count": 3}, []),
    ],
)
def test_declared_axis_charts_offer_only_kinds_their_binding_supports(spec, expected):
    assert _simple_chart_kinds(spec) == expected


def _switch_fixtures() -> list[dict]:
    path = Path(__file__).resolve().parents[3] / "config/chart_style/explicit_switch_kinds_fixtures.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == 1
    return payload["fixtures"]


@pytest.mark.parametrize("fixture", _switch_fixtures(), ids=lambda fixture: fixture["name"])
def test_chat_and_report_switcher_offer_the_same_declared_axis_kinds(fixture):
    # The report switcher (`lib/chart/explicit-switch-kinds.ts`) runs the same fixture.
    spec = copy.deepcopy(fixture["spec"])
    if generated := fixture.get("generatedRows"):
        spec["rows"] = [{generated["x"]: f"b{index}", generated["y"]: index} for index in range(generated["count"])]
        spec["row_count"] = generated["count"]
    assert _simple_chart_kinds(spec) == fixture["expected"]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"row_count": 3}, []),
        ({"row_count": None}, []),
        ({"row_count": 0}, []),
        ({"top_n": 1}, []),
        ({"series_shown": 1, "series_total": 2}, []),
        ({"points_shown": 1, "points_total": 2}, []),
        ({"series_shown": 1}, []),
        ({"presentation_plan": {}}, []),
        ({"rows": [{"date": "2026-10-28", "value": "9007199254740993"}], "row_count": 1}, ["table"]),
        ({"rows": [{"date": None, "value": 12}], "row_count": 1}, ["table"]),
        ({"rows": [{"date": "2026-10-28", "value": None}], "row_count": 1}, ["table"]),
    ],
)
def test_chart_kind_candidates_do_not_guess_missing_or_unsafe_data(changes, expected):
    spec = {
        "kind": "line",
        "columns": [{"key": "date", "type": "date"}, {"key": "value", "type": "numeric"}],
        "rows": [{"date": "2026-10-28", "value": 12}],
        "row_count": 1,
    }
    assert _simple_chart_kinds({**spec, **changes}) == expected


async def test_invalid_or_source_changing_patch_is_atomic(chart):
    store, ctx, result = chart
    section_id = result["section_id"]
    before = (await store.list_sections(result["canvas_id"]))[0]
    response = await edit_chart({"section_id": section_id, "expected_rev": before.rev, "changes": {"title": "Changed", "rows": [{"value": 0}]}}, ctx)
    assert response["error"] == "invalid_chart_change"
    # The trace classifies the refusal as rejected input and names the change.
    outcome = classify_tool_outcome(response)
    assert outcome.code == "tool_arguments_invalid" and outcome.field == response["path"]
    after = (await store.list_sections(result["canvas_id"]))[0]
    assert after.rev == before.rev and after.payload == before.payload


async def test_prior_turn_edit_emits_existing_canvas_update_event(chart):
    store, ctx, result = chart
    section = (await store.list_sections(result["canvas_id"]))[0]

    class Bus:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

    ctx.turn_id = "next_turn"
    ctx.parent_bus = Bus()
    changed = await edit_chart({"section_id": section.id, "expected_rev": section.rev, "changes": {"show_legend": False}}, ctx)
    assert "error" not in changed
    assert [event.event_type for event in ctx.parent_bus.events] == ["canvas.turn_streaming", "canvas.section_patched"]
    assert ctx.parent_bus.events[-1].payload["rev"] == changed["rev"]


@pytest.mark.parametrize("rejected", [False, True])
async def test_failed_update_event_is_reported_as_an_unknown_outcome(chart, rejected):
    from bowhead_router.errors import InvocationOutcomeUnknownError

    store, ctx, result = chart
    section = (await store.list_sections(result["canvas_id"]))[0]

    class FailingBus:
        async def emit(self, event):
            if rejected:
                return False
            raise RuntimeError("event delivery failed")

    ctx.parent_bus = FailingBus()
    with pytest.raises(InvocationOutcomeUnknownError) as error:
        await edit_chart({"section_id": section.id, "expected_rev": section.rev, "changes": {"title": "Updated"}}, ctx)
    assert error.value.reason_code == "chart_update_event_unconfirmed"
    assert (await store.list_sections(result["canvas_id"]))[0].rev == section.rev + 1


async def test_layout_edit_preserves_all_composed_views(chart):
    store, ctx, _ = chart
    result = await _add_chart(
        {
            "data_artifact_id": "art_aaaaaaaaaaaa",
            "title": "Combined",
            "column_types": {"date": "date"},
            "views": [
                {"id": "trend", "title": "Trend", "force_kind": "line", "axes": {"x": "date", "y": ["value"], "x_scale": "category"}, "group_by": []},
                {"id": "values", "title": "Values", "force_kind": "table"},
            ],
        },
        ctx,
    )
    before = next(section for section in await store.list_sections(result["canvas_id"]) if section.id == result["section_id"])
    read = await read_chart({"section_id": before.id}, ctx)
    assert "layout" in read["allowed_changes"]
    changed = await edit_chart({"section_id": before.id, "expected_rev": before.rev, "changes": {"layout": "split"}}, ctx)
    assert "error" not in changed
    after = next(section for section in await store.list_sections(result["canvas_id"]) if section.id == before.id)
    assert after.payload["spec"]["views"] == before.payload["spec"]["views"]
    assert after.payload["spec"]["layout"] == {"type": "split", "children": [{"view": "trend"}, {"view": "values"}]}


def test_public_tool_event_does_not_replay_source_samples():
    from bowhead_core.subagent import _public_tool_result

    public, _ = _public_tool_result("read_chart", {"section_id": "sec_1", "rev": 1, "views": [{"sample": [{"value": "private"}]}]})
    assert public == {"section_id": "sec_1", "rev": 1}


async def test_registered_tool_calls_use_runtime_context(chart):
    from bowhead_core.canvas_tools import register_canvas_tools
    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

    store, ctx, result = chart
    registry = ToolRegistry()
    register_canvas_tools(registry)
    runtime = ToolExecutionContext(
        workspace_id=ctx.workspace_id,
        member_uid="member",
        user_id=ctx.user_id,
        turn_id=ctx.turn_id,
        datarc=None,
        session_id=ctx.session_id,
        canvas_store=store,
        chart_authoring_projection=ctx.chart_authoring_projection,
        parent_bus=ctx.parent_bus,
    )
    read = await registry.execute("read_chart", {"section_id": result["section_id"]}, runtime)
    assert read.error is None and read.result["views"][0]["sample"]
    edit = await registry.execute(
        "edit_chart", {"section_id": result["section_id"], "expected_rev": read.result["rev"], "changes": {"show_data_labels": True}}, runtime
    )
    assert edit.error is None and edit.result["rev"] == read.result["rev"] + 1


async def test_registered_edit_settles_in_durable_execution(chart, tmp_path):
    import sqlite3

    from bowhead_core.canvas_tools import register_canvas_tools
    from bowhead_core.events import OrchestratorEvent
    from bowhead_core.terminal_llm_invocation import durable_llm_invocation_scope, emit_tool_step_started_ticket, tool_invocation_ticket_scope
    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
    from test_terminal_tool_invocation import _create_db, _intent, _runtime

    store, ctx, result = chart
    section = (await store.list_sections(result["canvas_id"]))[0]
    db_path = _create_db(tmp_path)
    runtime = await _runtime(db_path, execution_intent=_intent())
    registry = ToolRegistry()
    register_canvas_tools(registry)
    context = ToolExecutionContext(
        workspace_id=ctx.workspace_id,
        member_uid="member",
        user_id=ctx.user_id,
        turn_id="t_1",
        datarc=None,
        session_id=ctx.session_id,
        canvas_store=store,
        chart_authoring_projection=ctx.chart_authoring_projection,
        parent_bus=runtime.bus,
    )
    async with durable_llm_invocation_scope(runtime, cost_estimator=lambda _i, _o: 0.0):
        ticket = await emit_tool_step_started_ticket(
            runtime.bus, OrchestratorEvent(event_type="step_started", turn_id="t_1", payload={"call_id": "chart", "step_id": "step_chart"})
        )
        assert ticket is not None
        with tool_invocation_ticket_scope(ticket):
            invocation = await registry.execute(
                "edit_chart", {"section_id": section.id, "expected_rev": section.rev, "changes": {"show_legend": False}}, context
            )
    assert invocation.error is None and invocation.result["rev"] == section.rev + 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state, non_retriable FROM turn_effect_attempts").fetchone() == ("settled", 1)
        assert connection.execute("SELECT outcome FROM turn_effect_settlements").fetchone() == ("confirmed_applied",)
        assert connection.execute("SELECT COUNT(*) FROM turn_events WHERE event_type='canvas.section_patched'").fetchone() == (1,)


async def test_palette_edit_and_reset_keep_native_identity_rows_and_source(chart):
    store, ctx, created = chart

    async def palette():
        return [{"index": i, "light": c, "dark": c} for i, c in enumerate(["#2563A7", "#2F7F87", "#52607B", "#B06B33", "#718356", "#3B4654"])]

    ctx.report_read_access = SimpleNamespace(chart_palette=palette)
    args = {"section_id": created["section_id"]}
    before = (await store.list_sections(created["canvas_id"]))[0]
    read = await read_chart(args, ctx)
    assert read["views"][0]["editable_color_series"] == [{"key": "value", "label": "value", "color_index": None}]
    assert read["color_options"][4]["light"] == "#718356"
    edited = await edit_chart({**args, "expected_rev": before.rev, "changes": {"series_colors": {"value": 4}}}, ctx)
    after = (await store.list_sections(created["canvas_id"]))[0]
    assert edited["rev"] == before.rev + 1
    assert edited["series_colors"] == {"value": 4}
    assert after.id == before.id
    assert after.payload["spec"]["rows"] == before.payload["spec"]["rows"]
    assert after.payload["spec"]["source_meta"] == before.payload["spec"]["source_meta"]
    assert after.payload["spec"]["columns"][1]["color_index"] == 4
    reset = await edit_chart({**args, "expected_rev": after.rev, "changes": {}, "reset": {"series_colors": ["value"]}}, ctx)
    assert reset["series_colors"] == {"value": None}
    assert "color_index" not in ((await store.list_sections(created["canvas_id"]))[0]).payload["spec"]["columns"][1]


@pytest.mark.parametrize("value", [True, -1, 6, "green", "#12", "#GG0000", "#008000FF", " #008000", "rgb(0,128,0)", {"backgroundColor": "green"}])
async def test_color_edits_reject_unoffered_values_without_writing(chart, value):
    store, ctx, created = chart

    async def palette():
        return [{"index": i, "light": "#718356", "dark": "#718356"} for i in range(6)]

    ctx.report_read_access = SimpleNamespace(chart_palette=palette)
    before = (await store.list_sections(created["canvas_id"]))[0]
    result = await edit_chart({"section_id": before.id, "expected_rev": before.rev, "changes": {"series_colors": {"value": value}}}, ctx)
    assert result["error"] == "invalid_chart_change"
    assert ((await store.list_sections(created["canvas_id"]))[0]).payload == before.payload
    assert ((await store.list_sections(created["canvas_id"]))[0]).rev == before.rev


@pytest.mark.parametrize(("value", "canonical"), [("#00a651", "#00A651"), ("#0a6", "#00AA66"), ("#000000", "#000000")])
async def test_exact_hex_edit_round_trip_palette_switch_and_reset(chart, value, canonical):
    store, ctx, created = chart
    before = (await store.list_sections(created["canvas_id"]))[0]
    args = {"section_id": before.id}
    # Exact colors and reset do not depend on palette availability.
    result = await edit_chart({**args, "expected_rev": before.rev, "changes": {"series_colors": {"value": value}}}, ctx)
    assert result["series_colors"] == {"value": canonical}
    after = (await store.list_sections(created["canvas_id"]))[0]
    assert after.id == before.id and after.rev == before.rev + 1
    assert after.payload["spec"]["rows"] == before.payload["spec"]["rows"]
    assert after.payload["spec"]["source_meta"] == before.payload["spec"]["source_meta"]
    column = after.payload["spec"]["columns"][1]
    assert column["color_hex"] == canonical and "color_index" not in column
    read = await read_chart(args, ctx)
    assert read["views"][0]["editable_color_series"][0]["color_hex"] == canonical

    async def palette():
        return [{"index": 0, "light": "#2563A7", "dark": "#8CA8BF"}]

    ctx.report_read_access = SimpleNamespace(chart_palette=palette)
    switched = await edit_chart({**args, "expected_rev": after.rev, "changes": {"series_colors": {"value": 0}}}, ctx)
    column = (await store.list_sections(created["canvas_id"]))[0].payload["spec"]["columns"][1]
    assert column["color_index"] == 0 and "color_hex" not in column
    exact = await edit_chart({**args, "expected_rev": switched["rev"], "changes": {"series_colors": {"value": value}}}, ctx)
    reset = await edit_chart({**args, "expected_rev": exact["rev"], "changes": {}, "reset": {"series_colors": ["value"]}}, ctx)
    assert reset["series_colors"] == {"value": None}
    column = (await store.list_sections(created["canvas_id"]))[0].payload["spec"]["columns"][1]
    assert "color_hex" not in column and "color_index" not in column


async def test_chart_tools_use_scoped_report_access_without_relying_on_http_projection(chart):
    _, ctx, created = chart
    project = ctx.chart_authoring_projection
    ctx.chart_authoring_projection = None
    ctx.report_read_access = SimpleNamespace(
        session_id=ctx.session_id,
        workspace_id=ctx.workspace_id,
        team_id=getattr(ctx, "team_id", None),
        user_id=ctx.user_id,
        project_chart=project,
    )
    read = await read_chart({"section_id": created["section_id"]}, ctx)
    assert "error" not in read
    assert read["section_id"] == created["section_id"]
    assert read["views"][0]["sample"]
    ctx.report_read_access.user_id = "another-user"
    denied = await read_chart({"section_id": created["section_id"]}, ctx)
    assert denied["error"] == "chart_source_projection_unavailable"


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("source_access_denied", "source_access_denied"),
        ("section_outside_report_closure", "section_outside_report_closure"),
        ("provenance_unavailable", "provenance_unavailable"),
        ("error", "projection_error"),
        ("none", "projection_unavailable"),
        ("absent", "channel_unsupported"),
    ],
)
async def test_projection_failures_report_the_failing_check_and_log_a_reference(chart, caplog, outcome, reason):
    from bowhead_core.chart.authoring_tools import ChartProjectionUnavailable

    store, ctx, result = chart
    before = (await store.list_sections(result["canvas_id"]))[0]

    async def project(section):
        if outcome == "error":
            raise RuntimeError("synthetic projection fault")
        if outcome != "none":
            raise ChartProjectionUnavailable(outcome)

    ctx.chart_authoring_projection = None if outcome == "absent" else project
    with caplog.at_level("WARNING", logger="bowhead_core.chart.authoring_tools"):
        read = await read_chart({"section_id": result["section_id"]}, ctx)
    assert read["error"] == "chart_source_projection_unavailable"
    assert read["reason"] == reason
    assert read["diagnostic_ref"].startswith("chp_")
    # The hint names the failing check instead of always blaming source access.
    assert ("access" in read["hint"]) == (reason in {"source_access_denied", "channel_unsupported"})
    if reason in {"projection_error", "projection_unavailable"}:
        assert read["diagnostic_ref"] in read["hint"]
    logged = [record for record in caplog.records if "chart_source_projection_unavailable" in record.getMessage()]
    assert len(logged) == 1 and f"reason={reason}" in logged[0].getMessage() and read["diagnostic_ref"] in logged[0].getMessage()
    assert bool(logged[0].exc_info) == (reason == "projection_error")
    after = (await store.list_sections(result["canvas_id"]))[0]
    assert after.rev == before.rev and after.payload == before.payload


async def test_failed_read_protects_only_that_chart_from_replacement(chart):
    store, ctx, result = chart
    section_id = result["section_id"]
    other = _FakeArtifact(id="art_bbbbbbbbbbbb", filename="other.csv", path="other.csv")
    ctx.artifact_store.artifacts[other.id] = other
    ctx.artifact_store.backend.files[other.path] = b"region,amount\nNorth,3\nSouth,5\n"
    ctx.chart_authoring_projection = None
    assert (await read_chart({"section_id": section_id}, ctx))["chart_guarded"] is True
    peer = copy.copy(ctx)
    unrelated = await _add_chart(
        {
            "data_artifact_id": other.id,
            "title": "Regional amounts",
            "force_kind": "bar",
            "axes": {"x": "region", "y": ["amount"], "x_scale": "category"},
            "group_by": [],
        },
        peer,
    )
    assert "error" not in unrelated, unrelated
    same_data = await _add_chart({"data_artifact_id": "art_aaaaaaaaaaaa", "title": "Replacement"}, peer)
    assert same_data["error"] == "chart_edit_incomplete" and same_data["section_id"] == section_id
    assert "Charts with other data and titles can still be added" in same_data["hint"]
    bare_hex = await _add_chart({"data_artifact_id": "aaaaaaaaaaaa", "title": "Replacement"}, peer)
    assert bare_hex["error"] == "chart_edit_incomplete"
    same_title = await _add_chart(
        {
            "data_artifact_id": other.id,
            "title": "  values ",
            "force_kind": "bar",
            "axes": {"x": "region", "y": ["amount"], "x_scale": "category"},
            "group_by": [],
        },
        peer,
    )
    assert same_title["error"] == "chart_edit_incomplete" and same_title["section_id"] == section_id
    composed = await _add_chart({"title": "Pair", "views": [{"id": "a", "data_artifact_id": "art_aaaaaaaaaaaa", "title": "A"}]}, peer)
    assert composed["error"] == "chart_edit_incomplete"
    sections = await store.list_sections(result["canvas_id"])
    original = next(s for s in sections if s.id == section_id)
    assert original.rev == 1 and not original.superseded
    assert len(sections) == 2


async def test_chat_delivered_chart_is_edited_in_place_without_opening_canvas(chart):
    from bowhead_core.results_report_relations import project_report_relations

    store, ctx, result = chart
    section_id = result["section_id"]
    await store.set_section_surface_target(section_id, "chat")
    section = next(s for s in await store.list_sections(result["canvas_id"], include_hidden=True) if s.id == section_id)
    assert section.hidden_for_chat_mode

    class Bus:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)
            return True

    later = copy.copy(ctx)
    later.turn_id = "later_turn"
    later.parent_bus = Bus()
    changed = await edit_chart({"section_id": section_id, "expected_rev": section.rev, "changes": {"title": "Green values"}}, later)
    assert "error" not in changed and changed["rev"] == section.rev + 1
    # No Canvas turn is opened, and the browser is told to update the chat figure in place.
    assert [event.event_type for event in later.parent_bus.events] == ["canvas.section_patched"]
    payload = later.parent_bus.events[0].payload
    assert payload["hidden_for_chat_mode"] is True and payload["rev"] == changed["rev"]
    assert payload["patch"]["spec"]["title"] == "Green values"
    stored = next(s for s in await store.list_sections(result["canvas_id"], include_hidden=True) if s.id == section_id)
    assert stored.hidden_for_chat_mode and stored.payload["spec"]["title"] == "Green values"
    assert section_id not in {s.id for s in await store.list_sections(result["canvas_id"])}
    assert not project_report_relations([stored]).closure_ids
