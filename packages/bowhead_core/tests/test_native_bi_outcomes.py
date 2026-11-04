"""Native BI producers and adapters preserve the existing execution contract."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_bi_bridge.types import CutResult, DownloadDataResult
from bowhead_core.builtin_datarc_tool import build_builtin_datarc_specs, datarc_tool_ref
from bowhead_core.events import OrchestratorEvent, TurnEventBus
from bowhead_core.subagent import SubagentRunner, _is_inv_failure
from bowhead_core.tool_delivery_review import failed_calls, validate_delivery_review
from bowhead_core.tool_outcome import classify_tool_outcome, invocation_result
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec, _aggregate_bi_query_results, build_default_registry
from bowhead_core.tool_result_budget import DEFAULT_CAPS_TOKEN, _cap_bi_query_batch
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_core.types import ToolInvocation
from bowhead_router.types import ToolSchema
from test_builtin_datarc_tool import _Client, _Store

from bowhead_router import ToolCall


class _DataClient(_Client):
    """Replace only the Datarc HTTP boundary with deterministic responses."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**({"base_url": "https://datarc.example", "auth_key": "secret"} | kwargs))
        self.downloads: list[str] = []
        self.is_closed = False

    async def cut_question(self, **kwargs: Any) -> CutResult:
        return CutResult(tokens=[])

    async def download_data(self, *, text: str, datarc_workspace_id: str, member_uid: str) -> DownloadDataResult:
        assert datarc_workspace_id == "2"
        assert member_uid == "member_2"
        self.downloads.append(text)
        if text.startswith("provider failure"):
            raise RuntimeError("Synthetic provider rejection")
        rows = [] if text.startswith("empty") else [{"revenue": value} for value in range(24)]
        return DownloadDataResult(columns=["revenue"], rows=rows, total_rows=len(rows), csv_filename="revenue.csv")

    async def aclose(self) -> None:
        self.is_closed = True
        await super().aclose()


class _Artifacts:
    async def put_bytes(self, *, content: bytes, filename: str, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(id="art_" + hashlib.sha256(content).hexdigest()[:12], filename=filename)


@pytest.fixture
def native_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_local",
        user_id="user_1",
        member_uid="member_2",
        team_id="team_1",
        turn_id="turn_bi",
        role="analyst",
        datarc=_DataClient(),
        active_datarc_project="2",
        artifact_store=_Artifacts(),
    )


async def _wrapped_registry(monkeypatch: pytest.MonkeyPatch, base: ToolRegistry) -> tuple[ToolRegistry, str, _Store, list[_DataClient]]:
    import bowhead_core.builtin_datarc_tool as module

    clients: list[_DataClient] = []

    def client_factory(**kwargs: Any) -> _DataClient:
        client = _DataClient(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(module, "DatarcClient", client_factory)
    store = _Store()
    tool = await store.get_team_builtin_tool_instance("team_1", "ibt_1")
    registry = ToolRegistry()
    for spec in build_builtin_datarc_specs(team_id="team_1", tools=[tool], store=store, base_registry=base):
        registry.register(spec)
    return registry, datarc_tool_ref("ibt_1"), store, clients


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
@pytest.mark.parametrize(
    ("queries", "state", "child_ok"),
    [
        (["how to query", "how to analyze"], "failure", [False, False]),
        (["revenue by region", "how to query"], "partial", [True, False]),
        (["provider failure revenue", "provider failure profit"], "failure", [False, False]),
        (["revenue by region", "provider failure profit"], "partial", [True, False]),
        (["revenue by region", "revenue by store"], "success", [True, True]),
        (["empty revenue", "empty profit"], "success", [True, True]),
    ],
    ids=["all-rejected", "mixed-rejection", "all-provider-failed", "mixed-provider", "all-success", "empty-success"],
)
@pytest.mark.unit
async def test_native_batch_classifies_real_results(
    monkeypatch: pytest.MonkeyPatch, native_context: ToolExecutionContext, parallel: bool, queries: list[str], state: str, child_ok: list[bool]
) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_BI_BATCH", str(int(parallel)))
    invocation = await build_default_registry().execute("bi_query", {"queries": queries}, native_context)
    result = invocation.result
    assert invocation.error is None
    assert classify_tool_outcome(result).state == state
    assert result["ok"] is (state != "failure")
    assert [child["ok"] for child in result["results"]] == child_ok
    assert [child["query_text"] for child in result["results"]] == queries
    assert _is_inv_failure(invocation) is (state == "failure")
    assert bool(failed_calls([invocation])) is (state != "success")
    for child in result["results"]:
        if child["ok"] and not child.get("_no_data"):
            assert child["sample_rows"]
            assert child["artifact_id"] == child["_ref_artifact_id"]
        elif child["ok"]:
            assert child["_no_data"] is True
        else:
            assert child["error_kind"] in {"non_query_text", "datarc_error"}
    if state == "failure":
        assert result["error_kind"] == "bi_query_batch_failed"
    elif state == "partial":
        assert result["completion"] == "partial"


@pytest.mark.parametrize(
    ("children", "state", "code"),
    [
        ([{"ok": True, "records": [{"ok": False, "status": "error", "error": "Business column"}]}], "success", None),
        ([{"ok": False, "error_kind": "needs_clarification", "clarification": {"options": ["A", "B"]}}], "success", None),
        ([{"ok": False, "error_kind": "pending_user_confirmation", "accessible": []}], "success", None),
        ([{"ok": True, "status": "processing"}], "success", None),
        ([{"ok": True, "_partial_data": True}], "success", None),
        ([{"ok": True, "completion": "partial"}, {"ok": True}], "partial", None),
        ([{"ok": False, "error_kind": "effect_outcome_unknown"}, {"ok": True}], "failure", "tool_execution_unknown"),
    ],
    ids=["business-json", "clarification", "expected-wait", "processing", "data-caveat", "declared-partial", "unknown"],
)
@pytest.mark.unit
def test_native_aggregation_preserves_declared_semantics(children: list[dict[str, Any]], state: str, code: str | None) -> None:
    result = _aggregate_bi_query_results(children)
    outcome = classify_tool_outcome(result)
    assert (outcome.state, outcome.code) == (state, code)
    assert result["results"] is children


@pytest.mark.parametrize("question", ["how to query", "revenue by region", "empty revenue"])
@pytest.mark.unit
async def test_wrapper_preserves_real_native_result_and_audit(
    monkeypatch: pytest.MonkeyPatch, native_context: ToolExecutionContext, question: str
) -> None:
    from bowhead_core.datarc_selection import accepted_datarc_selection

    registry, name, store, clients = await _wrapped_registry(monkeypatch, build_default_registry())
    invocation = await registry.execute(name, {"question": question}, native_context)
    result = invocation.result
    expected = "failure" if question == "how to query" else "success"
    assert invocation.error is None
    assert classify_tool_outcome(result).state == expected
    assert classify_tool_outcome(result["result"]).state == expected
    assert result["connection"]
    assert result["workspace"]["id"] == "2"
    assert store.calls[-1]["status"] == ("error" if expected == "failure" else "ok")
    assert store.calls[-1]["error_kind"] == ("non_query_text" if expected == "failure" else None)
    assert store.calls[-1]["source_id"] == "ibt_1"
    assert store.calls[-1]["user_id"] == "user_1"
    assert clients[0].is_closed
    assert accepted_datarc_selection(native_context).member_uid == "member_2"
    if expected == "failure":
        assert result["error_kind"] == "non_query_text"
        assert result["error_message"] == result["result"]["error_message"]
        assert clients[0].downloads == []
    elif question.startswith("empty"):
        assert result["result"]["_no_data"] is True
    else:
        assert result["result"]["artifact_id"]


@pytest.mark.parametrize("operation", ["datarc_query", "datarc_schema"])
@pytest.mark.parametrize(
    ("native_result", "state", "audit_kind"),
    [
        ({"ok": False, "error_kind": "invalid_input", "error_message": "Correct the input"}, "failure", "invalid_input"),
        ({"ok": True, "completion": "partial", "rows": [{"revenue": 1}]}, "partial", "datarc_partial_result"),
        ({"ok": True, "rows": []}, "success", None),
        ({"ok": False, "error_kind": "needs_clarification", "clarification": {"options": ["A", "B"]}}, "success", None),
        ({"ok": False, "error_kind": "effect_outcome_unknown"}, "failure", "tool_outcome_unknown"),
    ],
    ids=["failure", "partial", "empty-success", "expected-outcome", "unknown"],
)
@pytest.mark.unit
async def test_wrapper_projects_declared_delegate_contract(
    monkeypatch: pytest.MonkeyPatch,
    native_context: ToolExecutionContext,
    operation: str,
    native_result: dict[str, Any],
    state: str,
    audit_kind: str | None,
) -> None:
    from bowhead_core.datarc_selection import accepted_datarc_selection

    async def delegate(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        selection = accepted_datarc_selection(ctx)
        assert selection.binding.connection_id == "ibt_1"
        assert selection.binding.revision == "1"
        assert selection.member_uid == "member_2"
        assert selection.user_id == "user_1"
        return native_result

    base = ToolRegistry()
    for name in ("bi_query", "bi_schema"):
        base.register(ToolSpec(schema=ToolSchema(name=name, description="Native delegate", input_schema={}), handler=delegate))
    registry, _, store, clients = await _wrapped_registry(monkeypatch, base)
    invocation = await registry.execute(datarc_tool_ref("ibt_1", operation), {"question": "revenue by region"}, native_context)
    result = invocation.result
    assert classify_tool_outcome(result).state == state
    assert result["result"] is native_result
    assert result["ok"] is (state != "failure")
    assert store.calls[-1]["status"] == ("ok" if state == "success" else "error")
    assert store.calls[-1]["error_kind"] == audit_kind
    assert clients[0].is_closed
    if state == "partial":
        assert result["completion"] == "partial"
    if audit_kind == "tool_outcome_unknown":
        assert classify_tool_outcome(result).code == "tool_execution_unknown"


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
@pytest.mark.parametrize("capped", [False, True], ids=["full", "capped"])
@pytest.mark.parametrize("state", ["failure", "partial", "success"])
@pytest.mark.integration
async def test_native_batch_live_persist_replay_and_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, native_context: ToolExecutionContext, parallel: bool, capped: bool, state: str
) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_BI_BATCH", str(int(parallel)))
    monkeypatch.setenv("BOWHEAD_PSI_CAP_ENABLED", "1" if capped else "0")
    monkeypatch.setitem(DEFAULT_CAPS_TOKEN, "bi_query", 100)
    queries = {
        "failure": ["how to query", "how to analyze"],
        "partial": ["revenue by region", "how to query"],
        "success": ["revenue by region", "revenue by store"],
    }[state]
    if capped and state == "failure":
        queries = [query + " x" * 1000 for query in queries]
    log = TurnLogStore(tmp_path / "events.db")
    await log.initialize()
    live: list[OrchestratorEvent] = []

    async def sink(event: OrchestratorEvent) -> None:
        await log.record(turn_id=event.turn_id, event_type=event.event_type, payload=event.payload)
        live.append(event)

    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    runner = SubagentRunner(router=SimpleNamespace(), tool_registry=build_default_registry())
    invocation = await runner._execute_tool(
        ToolCall(id="call_batch", name="bi_query", arguments={"queries": queries}),
        native_context,
        bus,
        parent_turn_id="turn_bi",
        subagent_id="analyst_bi",
        running_profile="analyst",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="turn_bi", payload={}))
    tool = next(event.payload for event in live if event.event_type == "tool_result")
    step = next(event.payload for event in live if event.event_type == "step_done")
    assert tool["call_id"] == step["call_id"] == "call_batch"
    assert tool["ok"] is (state != "failure")
    assert step["state"] == state
    assert step["ok"] is (state == "success")
    assert classify_tool_outcome(invocation_result(invocation)).state == state
    assert classify_tool_outcome(invocation.result).state == state
    if state == "failure":
        assert tool["failure_code"] == "tool_execution_failed"
        assert tool["diagnostics"] == step["error"]["diagnostics"]
        if capped:
            assert tool["result_inline"] is None
            assert step["result_inline"] is None
    for event_type in ("tool_result", "step_done"):
        original = next(event.payload for event in live if event.event_type == event_type)
        persisted = next(event.payload for event in await log.list_events("turn_bi") if event.event_type == event_type)
        assert persisted == original
    if capped:
        assert invocation.result_full["results"][0]["query_text"] == queries[0]
        assert invocation.result is not invocation.result_full
    if state != "success":
        assert set(failed_calls([invocation])) == {"call_batch"}
        review = {
            "requirements": [{"requirement": "Revenue analysis", "status": "unfulfilled", "evidence_call_ids": []}],
            "issues": [{"call_id": "call_batch", "resolution": "unfulfilled", "evidence_call_ids": []}],
        }
        checked = validate_delivery_review(review, [invocation])
        assert checked.valid and not checked.complete
        review["requirements"][0].update(status="fulfilled", evidence_call_ids=["call_batch"])
        assert not validate_delivery_review(review, [invocation]).valid
        replacement = ToolInvocation("table_read", {}, call_id="replacement", result={"ok": True})
        review["requirements"][0].update(status="fulfilled", evidence_call_ids=["replacement"])
        review["issues"][0].update(
            resolution="covered", evidence_call_ids=["replacement"], explanation="The equivalent successful table covers the required data."
        )
        checked = validate_delivery_review(review, [invocation, replacement])
        assert checked.valid and checked.complete


@pytest.mark.unit
def test_batch_cap_retains_failure_and_success_identity() -> None:
    children = [
        {"ok": True, "artifact_id": "art_revenue", "_ref_artifact_id": "art_revenue", "sample_rows": [{"value": "x" * 5000}], "total_rows": 1},
        {"ok": False, "error_kind": "non_query_text", "error_message": "Use a data question", "sample_rows": []},
    ]
    original = _aggregate_bi_query_results(children)
    capped = _cap_bi_query_batch(original, 100)
    assert classify_tool_outcome(capped).state == "partial"
    assert capped["results"][0]["artifact_id"] == "art_revenue"
    assert capped["results"][0]["_ref_artifact_id"] == "art_revenue"
    assert capped["results"][1]["error_kind"] == "non_query_text"
    assert capped["results"][1]["error_message"] == "Use a data question"
    assert children[0]["sample_rows"]


@pytest.mark.parametrize("state", ["failure", "partial", "success", "unknown"])
@pytest.mark.integration
async def test_wrapped_native_receipt_live_persist_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, native_context: ToolExecutionContext, state: str
) -> None:
    # The current wrapper accepts one question. A retained native batch receipt
    # exercises partial forwarding without adding a production batch API.
    queries = {
        "failure": ["how to query", "how to analyze"],
        "partial": ["revenue by region", "how to query"],
        "success": ["revenue by region", "revenue by store"],
    }
    if state == "failure":
        queries[state] = [query + " x" * 1000 for query in queries[state]]
    if state == "unknown":
        native_result = _aggregate_bi_query_results([{"ok": False, "error_kind": "tool_outcome_unknown"}, {"ok": True}])
    else:
        native_result = await build_default_registry().get("bi_query").handler({"queries": queries[state]}, native_context)

    async def delegate(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        return native_result

    base = ToolRegistry()
    for name in ("bi_query", "bi_schema"):
        base.register(ToolSpec(schema=ToolSchema(name=name, description="Retained native receipt", input_schema={}), handler=delegate))
    registry, name, store, clients = await _wrapped_registry(monkeypatch, base)
    log = TurnLogStore(tmp_path / "events.db")
    await log.initialize()
    live: list[OrchestratorEvent] = []

    async def sink(event: OrchestratorEvent) -> None:
        await log.record(turn_id=event.turn_id, event_type=event.event_type, payload=event.payload)
        live.append(event)

    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    runner = SubagentRunner(router=SimpleNamespace(), tool_registry=registry)
    invocation = await runner._execute_tool(
        ToolCall(id="call_wrapped", name=name, arguments={"question": "revenue by region"}),
        native_context,
        bus,
        parent_turn_id="turn_bi",
        subagent_id="analyst_bi",
        running_profile="analyst",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="turn_bi", payload={}))
    expected = "failure" if state == "unknown" else state
    assert invocation.result["result"] is native_result
    assert classify_tool_outcome(invocation_result(invocation)).state == expected
    assert store.calls[-1]["status"] == ("ok" if state == "success" else "error")
    assert clients[0].is_closed
    replay = await log.list_events("turn_bi")
    for event_type in ("tool_result", "step_done"):
        payload = next(event.payload for event in live if event.event_type == event_type)
        assert payload == next(event.payload for event in replay if event.event_type == event_type)
        assert payload["call_id"] == "call_wrapped"
        if event_type == "step_done":
            assert payload["state"] == expected
        if state == "failure":
            assert payload["result_inline"] is None
        if state == "unknown" and event_type == "tool_result":
            assert payload["failure_code"] == "tool_execution_unknown"


@pytest.mark.unit
async def test_native_unknown_outcome_is_not_downgraded_by_legacy_batch_presentation(native_context: ToolExecutionContext) -> None:
    async def handler(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        return _aggregate_bi_query_results([{"ok": False, "error_kind": "effect_outcome_unknown"}, {"ok": True}])

    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema(name="bi_query", description="Native result fixture", input_schema={}), handler=handler))
    live: list[OrchestratorEvent] = []

    async def sink(event: OrchestratorEvent) -> None:
        live.append(event)

    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    runner = SubagentRunner(router=SimpleNamespace(), tool_registry=registry)
    await runner._execute_tool(
        ToolCall(id="call_unknown", name="bi_query", arguments={"queries": ["revenue by region", "revenue by store"]}),
        native_context,
        bus,
        parent_turn_id="turn_bi",
        subagent_id="analyst_bi",
        running_profile="analyst",
    )
    step = next(event.payload for event in live if event.event_type == "step_done")
    assert step["state"] == "failure"
    assert step["error"]["code"] == "tool_execution_unknown"
    assert [child["state"] for child in step["sub_steps"]] == ["failure", "success"]


@pytest.mark.unit
async def test_wrapper_rechecks_live_revision_and_membership(monkeypatch: pytest.MonkeyPatch, native_context: ToolExecutionContext) -> None:
    from bowhead_core.datarc_selection import accepted_datarc_selection

    revisions: list[str] = []

    async def delegate(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        revisions.append(accepted_datarc_selection(ctx).binding.revision)
        return {"ok": True}

    base = ToolRegistry()
    for name in ("bi_query", "bi_schema"):
        base.register(ToolSpec(schema=ToolSchema(name=name, description="Native delegate", input_schema={}), handler=delegate))
    registry, name, store, clients = await _wrapped_registry(monkeypatch, base)
    tool = await store.get_team_builtin_tool_instance("team_1", "ibt_1")
    tool.connection_revision = 7

    async def live_tool(team_id: str, tool_id: str) -> SimpleNamespace:
        return tool

    monkeypatch.setattr(store, "get_team_builtin_tool_instance", live_tool)
    assert (await registry.execute(name, {"question": "revenue by region"}, native_context)).result["ok"]
    assert revisions == ["7"]

    async def revoked_membership(user_id: str, team_id: str) -> bool:
        return False

    monkeypatch.setattr(store, "user_has_active_team_membership", revoked_membership)
    denied = await registry.execute(name, {"question": "revenue by region"}, native_context)
    assert denied.result["error_kind"] == "datarc_identity_unavailable"
    assert revisions == ["7"]
    assert len(clients) == 1
    assert store.calls[-1]["status"] == "error"


@pytest.mark.unit
async def test_wrapper_cleanup_error_cannot_leave_a_success_audit(monkeypatch: pytest.MonkeyPatch, native_context: ToolExecutionContext) -> None:
    registry, name, store, clients = await _wrapped_registry(monkeypatch, build_default_registry())

    async def failing_close(self: _DataClient) -> None:
        self.is_closed = True
        raise RuntimeError("Synthetic cleanup failure")

    monkeypatch.setattr(_DataClient, "aclose", failing_close)
    invocation = await registry.execute(name, {"question": "revenue by region"}, native_context)
    assert invocation.result["error_kind"] == "datarc_operation_failed"
    assert store.calls[-1]["status"] == "error"
    assert store.calls[-1]["error_kind"] == "RuntimeError"
    assert clients[0].is_closed


@pytest.mark.unit
async def test_wrapper_cancellation_propagates_and_closes_client(monkeypatch: pytest.MonkeyPatch, native_context: ToolExecutionContext) -> None:
    async def cancelled(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
        raise asyncio.CancelledError

    base = ToolRegistry()
    for name in ("bi_query", "bi_schema"):
        base.register(ToolSpec(schema=ToolSchema(name=name, description="Cancelled native call", input_schema={}), handler=cancelled))
    registry, name, store, clients = await _wrapped_registry(monkeypatch, base)
    with pytest.raises(asyncio.CancelledError):
        await registry.execute(name, {"question": "revenue by region"}, native_context)
    assert clients[0].is_closed
    assert store.calls[-1]["status"] == "error"


@pytest.mark.parametrize("capped", [False, True], ids=["full", "capped"])
async def test_wrapper_non_query_reason_survives_event_projection_and_capping(monkeypatch, tmp_path, native_context, capped):
    monkeypatch.setenv("BOWHEAD_PSI_CAP_ENABLED", "1" if capped else "0")
    registry, name, store, clients = await _wrapped_registry(monkeypatch, build_default_registry())
    monkeypatch.setitem(DEFAULT_CAPS_TOKEN, name, 100)
    log = TurnLogStore(tmp_path / "refusal-events.db")
    await log.initialize()
    live = []

    async def sink(event):
        await log.record(turn_id=event.turn_id, event_type=event.event_type, payload=event.payload)
        live.append(event)

    bus = TurnEventBus(log_sink=sink, persist_before_live=True)
    runner = SubagentRunner(router=SimpleNamespace(), tool_registry=registry)
    invocation = await runner._execute_tool(
        ToolCall(id="refused", name=name, arguments={"question": "how to query" + (" x" * 2000 if capped else "")}),
        native_context,
        bus,
        parent_turn_id="turn_bi",
        subagent_id="analyst_bi",
        running_profile="analyst",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="turn_bi", payload={}))
    step = next(event.payload for event in live if event.event_type == "step_done")
    assert step["state"] == "failure"
    assert step["error"]["message_zh"] == "non_query_text"
    assert step["error"]["code"] == "tool_execution_failed"
    assert step["error"]["diagnostics"]
    assert invocation_result(invocation)["error_kind"] == "non_query_text"
    assert store.calls[-1]["status"] == "error" and store.calls[-1]["error_kind"] == "non_query_text"
    assert clients[0].downloads == [] and clients[0].is_closed
    if capped:
        assert step["result_inline"] is None
    saved = await log.list_events("turn_bi")
    assert [(event.event_type, event.payload) for event in saved] == [(event.event_type, event.payload) for event in live]
