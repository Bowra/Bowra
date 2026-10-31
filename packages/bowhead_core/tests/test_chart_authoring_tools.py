"""Chart authoring respects the viewer projection, revision and original data."""

import asyncio
import copy
from types import SimpleNamespace

import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.canvas_tools import _add_chart, _remove_chart
from bowhead_core.chart.authoring_tools import _simple_chart_kinds, edit_chart, read_chart
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
    result = await _add_chart({"data_artifact_id": artifact.id, "title": "Values", "force_kind": "line", "column_types": {"date": "date"}}, ctx)
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
    assert read["views"][0]["display_receipt"]["value_display"]["actual_axis_unit"] == "not_available_in_tool_receipt"
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


async def test_simple_kind_edits_reuse_the_existing_chart_and_data(chart):
    store, ctx, result = chart
    before = (await store.list_sections(result["canvas_id"]))[0]
    read = await read_chart({"section_id": before.id}, ctx)
    assert read["supported_kinds"] == ["line", "bar", "hbar", "table"]
    edited = await edit_chart({"section_id": before.id, "expected_rev": before.rev, "changes": {"kind": "bar"}}, ctx)
    assert "error" not in edited
    after = (await store.list_sections(result["canvas_id"]))[0]
    assert after.payload["spec"]["kind"] == "bar"
    for key in ("rows", "columns", "data_artifact_id", "source_meta"):
        assert after.payload["spec"][key] == before.payload["spec"][key]
    table = await edit_chart({"section_id": before.id, "expected_rev": after.rev, "changes": {"kind": "table"}}, ctx)
    assert "error" not in table


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
                {"id": "trend", "title": "Trend", "force_kind": "line", "series": ["value"]},
                {"id": "values", "title": "Values", "force_kind": "table", "series": ["value"]},
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
