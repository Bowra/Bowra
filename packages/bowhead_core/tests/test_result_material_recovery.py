"""Native producer, serialization and reconstructed-history recovery boundaries."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.agents.report_tools import _write_report
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.context_assembly import build_turn_history
from bowhead_core.result_materials import (
    MaterialOrigin,
    MaterialReference,
    aggregate_references,
    bounded_references,
    capture_material_references,
    model_reference_view,
    parse_references,
)
from bowhead_core.subagent import _serialize
from bowhead_core.types import ToolInvocation

pytestmark = pytest.mark.integration


@pytest.fixture
async def recovery(tmp_path, monkeypatch):
    monkeypatch.delenv("FEATURE_CONTEXT_MATERIAL_RECOVERY", raising=False)
    store = CanvasStore(tmp_path / "canvas.db")
    await store.initialize()
    await store.ensure_canvas(session_id="session", workspace_id="space", user_id="user")
    return SimpleNamespace(
        canvas_store=store, session_id="session", turn_id="turn", workspace_id="space", user_id="user", parent_bus=SimpleNamespace()
    )


async def test_multi_section_write_survives_summary_without_claiming_visible_body(recovery):
    args = {"content": "## Coverage\nCollect the missing data.\n\n## Decision\nNo investment priority is established."}
    result = await _write_report(args, recovery)
    inv = ToolInvocation("write_report", args, call_id="write", result=result)
    await capture_material_references(inv, recovery, turn_id="turn", producer_id="child")
    assert {item.locator["section_id"] for item in inv.material_refs.items} == set(result["section_ids"])
    child = inv.material_refs
    parent = ToolInvocation("subagent_spawn", {}, call_id="spawn", result={"summary": "Report saved"}, nested=[inv])
    await capture_material_references(parent, recovery, turn_id="turn", producer_id=None)
    projected = json.loads(_serialize(parent))
    assert projected["material_refs"]["content_in_result"] == "omitted"
    assert "No investment priority" not in _serialize(parent)
    assert parent.material_refs.items == child.items
    assert all(item.origin.call_id == "write" for item in parent.material_refs.items)
    turn = SimpleNamespace(id="turn", seq=1, status="completed", user_text="Analyze coverage", final_answer="Report saved")
    history = build_turn_history([turn], exclude_turn_id="next", budget_tokens=0, material_refs={"turn": model_reference_view(parent.material_refs)})
    assert all(identity in history.messages[-1]["content"] for identity in result["section_ids"])
    assert turn.final_answer == "Report saved"


async def test_failed_metadata_preserves_confirmed_write_and_unknown_coverage(recovery):
    result = await _write_report({"content": "Saved native text"}, recovery)
    inv = ToolInvocation("write_report", {}, result=result)
    recovery.canvas_store = SimpleNamespace(get_canvas_by_session=AsyncMock(side_effect=OSError("unavailable")))
    await capture_material_references(inv, recovery, turn_id="turn", producer_id="child")
    assert inv.result is result and inv.error is None and result["ok"]
    assert inv.material_refs.coverage.references == "unknown"


def test_aggregation_deduplicates_mirrors_and_caps_the_whole_turn():
    items = [
        MaterialReference(
            kind="artifact",
            locator={"artifact_id": f"art_{i}"},
            relation="created",
            origin=MaterialOrigin(kind="tool", turn_id="turn", producer_id="child", call_id=f"call_{i}"),
        )
        for i in range(90)
    ]
    receipt = bounded_references(items)
    assert len(receipt.items) <= 32 and len(receipt.model_dump_json().encode()) <= 8192
    assert receipt.coverage.omitted_count == 90 - len(receipt.items)
    first = ToolInvocation("python_exec", {})
    first.material_refs = bounded_references(items[:2])
    mirror = ToolInvocation("subagent_spawn", {})
    mirror.material_refs = first.material_refs
    assert len(aggregate_references([first, mirror]).items) == 2
    assert parse_references({"version": 99, "items": []}) is None
    assert parse_references({**first.material_refs.model_dump(), "reader": "execute_code"}) is None


async def test_external_fields_and_scalar_results_cannot_supply_native_metadata(recovery):
    for name, result in [
        ("teammcp.external", {"artifact_id": "art_forged", "material_refs": {"version": 1}}),
        ("get_current_user", {"name": "Synthetic"}),
    ]:
        inv = ToolInvocation(name, {}, result=result)
        await capture_material_references(inv, recovery, turn_id="turn", producer_id="child")
        assert inv.material_refs is None
        assert "material_refs" not in json.loads(_serialize(inv))


async def test_edit_keeps_update_relation_and_original_revision(recovery):
    created = await _write_report({"content": "First text"}, recovery)
    args = {"section_id": created["section_id"], "expected_rev": created["rev"], "content": "Updated text"}
    updated = await _write_report(args, recovery)
    assert updated["ok"]
    inv = ToolInvocation("write_report", args, result=updated)
    await capture_material_references(inv, recovery, turn_id="turn", producer_id="child")
    assert inv.material_refs.items[0].relation == "updated"
    assert inv.material_refs.items[0].observed_version == {"rev": updated["rev"]}


@pytest.mark.parametrize(
    "tool,result",
    [
        ("bi_query", {"results": [{"artifact_id": "art_one"}, {"_ref_artifact_id": "art_two", "error": "Later operation failed"}]}),
        ("python_exec", {"output_artifacts": [{"artifact_id": "art_one"}, {"artifact_id": "art_two"}], "error": "Later operation failed"}),
        ("skill_invoke", {"output_artifacts": [{"artifact_id": "art_one"}, {"artifact_id": "art_two"}]}),
    ],
)
async def test_acknowledged_artifacts_survive_partial_failure_without_reexecution(recovery, tool, result):
    recovery.artifact_store = SimpleNamespace(get=AsyncMock(side_effect=lambda identity, **kwargs: SimpleNamespace(id=identity)))
    invocation = ToolInvocation(tool, {}, call_id="original", result=result)
    await capture_material_references(invocation, recovery, turn_id="turn", producer_id="child")
    assert [item.locator["artifact_id"] for item in invocation.material_refs.items] == ["art_one", "art_two"]
    assert recovery.artifact_store.get.await_count == 2
    assert invocation.result is result


async def test_native_tool_dispatch_emits_the_same_receipt_as_model_projection(recovery):
    import asyncio

    from bowhead_core.events import OrchestratorEvent, TurnEventBus
    from test_subagent_step_emit_contract import _make_ctx, _make_runner

    from bowhead_router import ToolCall

    runner = _make_runner({"write_report": _write_report})
    ctx = _make_ctx("turn")
    ctx.canvas_store, ctx.session_id = recovery.canvas_store, recovery.session_id
    bus = TurnEventBus()
    events = []

    async def consume():
        async for event in bus.subscribe():
            events.append(event)

    consumer = asyncio.create_task(consume())
    invocation = await runner._execute_tool(
        ToolCall("write", "write_report", {"content": "## Evidence\nEvidence is incomplete.\n\n## Next step\nCollect data."}),
        ctx,
        bus,
        parent_turn_id="turn",
        subagent_id="writer",
        running_profile="narrative",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="turn", payload={}))
    await consumer
    assert invocation.result["ok"], invocation.result
    receipt = json.loads(_serialize(invocation))["material_refs"]
    result = next(event for event in events if event.event_type == "tool_result")
    step = next(event for event in events if event.event_type == "step_done")
    assert result.payload["result_meta"]["material_refs"]["items"] == receipt["items"]
    assert step.payload["result_meta"]["material_refs"]["items"] == receipt["items"]
    assert len(receipt["items"]) == 2
