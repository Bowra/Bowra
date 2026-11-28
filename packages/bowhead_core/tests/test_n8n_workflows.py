"""Approved n8n workflows as derived team tools (FEATURE_N8N_WORKFLOW_TOOLS, tech design D1–D6)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from bowhead_core.n8n_workflow_run import HostRequestPacer, run_n8n_workflow
from bowhead_core.n8n_workflows import (
    SCHEDULE_TRIGGER,
    WEBHOOK_TRIGGER,
    NotApprovable,
    TooManyApprovals,
    approval_from_record,
    build_approval,
    draft_input_schema,
    is_n8n_source,
    load_n8n_approvals,
    n8n_workflow_entry,
    save_n8n_approval,
)
from bowhead_core.resource_execution_gate import check_team_tool_execution
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.team_tool_loader import load_team_tool_entries
from bowhead_core.team_tool_registry_view import build_team_specs, describe_remote_failure
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_core.user_store import TeamToolSource, UserStore

_TEAM = "team_a"
_SRC = "ots_n8n000000001"
_URL = "https://n8n.example.com/mcp-server/http"
_RAW = [
    {"name": name, "description": name, "input_schema": {"type": "object"}, "annotations": {"readOnlyHint": name != "execute_workflow"}}
    for name in ("search_workflows", "get_workflow_details", "execute_workflow", "get_workflow_execution", "search_executions")
]
_SCHEMA = {"type": "object", "properties": {"month": {"type": "string"}}, "required": ["month"], "additionalProperties": False}


def _workflow(**overrides: Any) -> dict[str, Any]:
    workflow = {
        "id": "wfMoM",
        "name": "MoM",
        "active": True,
        "isArchived": False,
        "versionId": "v2",
        "activeVersionId": "v1",
        "canExecute": True,
        "nodes": [
            {"name": "Webhook", "type": WEBHOOK_TRIGGER, "parameters": {"path": "calc-mom"}},
            {"name": "Pick", "type": "n8n-nodes-base.set", "parameters": {"value": "={{ $json.body.month }} {{ $json.body['region'] }}"}},
        ],
        "activeVersion": {
            "sameAsDraft": False,
            "nodes": [
                {"name": "Webhook", "type": WEBHOOK_TRIGGER, "parameters": {"path": "calc-mom"}},
                {"name": "Pick", "type": "n8n-nodes-base.set", "parameters": {"value": "={{ $json.body?.month }}"}},
            ],
        },
    }
    workflow.update(overrides)
    return workflow


def _approval(category: str = "read", **workflow: Any):
    approval = build_approval(
        workflow_id="wfMoM",
        workflow=_workflow(**workflow),
        description="Month-over-month sales by region.",
        input_schema=_SCHEMA,
        category=category,
        actor_user_id="usr_admin",
        now="2026-11-27T00:00:00+00:00",
    )
    assert not isinstance(approval, NotApprovable), approval
    return approval


# ── recognition, approval and draft schema ─────────────────────────────────


def test_an_n8n_source_is_recognized_by_its_mcp_path_and_run_tools() -> None:
    assert is_n8n_source("mcp", _URL, _RAW)
    assert is_n8n_source("mcp", _URL + "/", _RAW)
    assert not is_n8n_source("mcp", "https://n8n.example.com/mcp", _RAW)
    assert not is_n8n_source("openapi", _URL, _RAW)
    assert not is_n8n_source("mcp", _URL, [tool for tool in _RAW if tool["name"] != "get_workflow_execution"])


def test_approval_binds_the_published_trigger_and_version() -> None:
    approval = _approval()
    assert (approval.trigger_node, approval.trigger_type, approval.approved_version_id) == ("Webhook", WEBHOOK_TRIGGER, "v1")
    assert approval.takes_body
    assert approval_from_record("wfMoM", approval.to_record()) == approval
    tampered = {**approval.to_record(), "category": "destructive"}
    assert approval_from_record("wfMoM", tampered) is None
    assert approval_from_record("../etc", approval.to_record()) is None


@pytest.mark.parametrize(
    ("overrides", "category", "schema", "code"),
    [
        ({"isArchived": True}, "read", _SCHEMA, "archived"),
        ({"active": False, "activeVersionId": None}, "read", _SCHEMA, "not_published"),
        ({"canExecute": False}, "read", _SCHEMA, "cannot_execute"),
        ({"activeVersion": {"sameAsDraft": False, "nodes": []}}, "read", _SCHEMA, "no_supported_trigger"),
        ({}, "unknown", _SCHEMA, "category_invalid"),
        ({}, "read", {"type": "array"}, "schema_not_object"),
        ({}, "read", {"type": "object", "properties": {"a": {"type": 3}}}, "schema_invalid"),
    ],
)
def test_approval_refuses_workflows_bowhead_cannot_run(overrides, category, schema, code) -> None:
    result = build_approval(
        workflow_id="wfMoM", workflow=_workflow(**overrides), description="d", input_schema=schema, category=category, actor_user_id="u"
    )
    assert isinstance(result, NotApprovable) and result.code == code and result.message


def test_a_schedule_workflow_takes_no_input() -> None:
    nodes = [{"name": "Every day", "type": SCHEDULE_TRIGGER, "parameters": {}}]
    workflow = _workflow(nodes=nodes, activeVersion={"sameAsDraft": True})
    refused = build_approval(workflow_id="wfMoM", workflow=workflow, description="d", input_schema=_SCHEMA, category="read", actor_user_id="u")
    assert isinstance(refused, NotApprovable) and refused.code == "schedule_takes_no_input"
    approval = build_approval(
        workflow_id="wfMoM",
        workflow=workflow,
        description="d",
        input_schema=draft_input_schema(nodes, takes_body=False),
        category="read",
        actor_user_id="u",
    )
    assert not isinstance(approval, NotApprovable) and not approval.takes_body


def test_the_draft_schema_lists_body_fields_the_published_nodes_read() -> None:
    draft = draft_input_schema(_workflow()["nodes"], takes_body=True)
    assert draft["properties"] == {"month": {}, "region": {}}
    assert draft_input_schema([], takes_body=True)["properties"] == {}


# ── storage, loader and gate ───────────────────────────────────────────────


@pytest.fixture
async def store(tmp_path) -> UserStore:
    path = tmp_path / "bowhead.db"
    users = UserStore(str(path))
    await users.initialize()
    await TeamSettingsStore(path).init()
    await users.create_team_tool_source(
        TeamToolSource(
            id=_SRC,
            team_id=_TEAM,
            name="n8n",
            kind="mcp",
            base_url=_URL,
            tools_schema=_RAW,
            selected_op_ids=["search_workflows"],
            created_at="2026-11-27T00:00:00+00:00",
            updated_at="2026-11-27T00:00:00+00:00",
        )
    )
    return users


@pytest.mark.asyncio
async def test_derived_tools_load_only_with_the_switch_and_a_stored_approval(store, monkeypatch) -> None:
    approval = _approval()
    stored = await save_n8n_approval(store.db_path, team_id=_TEAM, source_id=_SRC, workflow_id="wfMoM", approval=approval, actor_user_id="usr_admin")
    assert stored is not None and stored["wfMoM"]["digest"] == approval.digest
    assert (await store.get_team_tool_source(_TEAM, _SRC)).updated_at != "2026-11-27T00:00:00+00:00"

    monkeypatch.delenv("FEATURE_N8N_WORKFLOW_TOOLS", raising=False)
    entries, _ = await load_team_tool_entries(_TEAM, store)
    assert [entry.dispatch_name for entry in entries] == ["search_workflows"]

    monkeypatch.setenv("FEATURE_N8N_WORKFLOW_TOOLS", "1")
    entries, by_id = await load_team_tool_entries(_TEAM, store)
    derived = [entry for entry in entries if entry.n8n_workflow is not None]
    assert [entry.dispatch_name for entry in derived] == ["n8n_wf_wfMoM"]
    assert derived[0].input_schema == _SCHEMA and derived[0].admin_category == "read" and _SRC in by_id
    # execute_workflow is not selected, so the model never sees the raw run tool.
    assert "execute_workflow" not in {entry.dispatch_name for entry in entries}

    assert await save_n8n_approval(store.db_path, team_id=_TEAM, source_id=_SRC, workflow_id="wfMoM", approval=None, actor_user_id="u") == {}
    assert await load_n8n_approvals(store.db_path, _TEAM) == {}
    assert (
        await save_n8n_approval(store.db_path, team_id=_TEAM, source_id="ots_missing0001", workflow_id="wfMoM", approval=approval, actor_user_id="u")
        is None
    )


@pytest.mark.asyncio
async def test_a_source_holds_a_bounded_number_of_approvals(store, monkeypatch) -> None:
    monkeypatch.setattr("bowhead_core.n8n_workflows.MAX_APPROVED_PER_SOURCE", 1)
    await save_n8n_approval(store.db_path, team_id=_TEAM, source_id=_SRC, workflow_id="wfMoM", approval=_approval(), actor_user_id="u")
    with pytest.raises(TooManyApprovals):
        await save_n8n_approval(store.db_path, team_id=_TEAM, source_id=_SRC, workflow_id="wfOther", approval=_approval(), actor_user_id="u")
    # Replacing an existing approval is not a new one.
    await save_n8n_approval(store.db_path, team_id=_TEAM, source_id=_SRC, workflow_id="wfMoM", approval=_approval("destructive"), actor_user_id="u")


@pytest.mark.asyncio
async def test_the_gate_checks_the_current_approval_not_the_raw_selection(store, monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_N8N_WORKFLOW_TOOLS", "1")
    approval = _approval()
    await save_n8n_approval(store.db_path, team_id=_TEAM, source_id=_SRC, workflow_id="wfMoM", approval=approval, actor_user_id="u")
    ctx = SimpleNamespace(team_id=_TEAM, skill_instance_store=SimpleNamespace(db_path=store.db_path))
    kwargs = {"team_id": _TEAM, "source_id": _SRC, "operation_id": "n8n_wf_wfMoM", "advertised_name": "teammcp.x.n8n_wf_wfMoM"}
    assert await check_team_tool_execution(ctx, **kwargs, n8n_workflow_digest=approval.digest) is None
    # The approval changed after this Turn loaded the tool.
    await save_n8n_approval(store.db_path, team_id=_TEAM, source_id=_SRC, workflow_id="wfMoM", approval=_approval("destructive"), actor_user_id="u")
    denied = await check_team_tool_execution(ctx, **kwargs, n8n_workflow_digest=approval.digest)
    assert denied is not None and denied["error_kind"] == "operation_unavailable"
    monkeypatch.setenv("FEATURE_N8N_WORKFLOW_TOOLS", "0")
    assert (await check_team_tool_execution(ctx, **kwargs, n8n_workflow_digest=approval.digest))["error_kind"] == "operation_unavailable"


# ── run and wait ───────────────────────────────────────────────────────────


def _result(body: dict[str, Any], *, is_error: bool = False) -> SimpleNamespace:
    return SimpleNamespace(is_error=is_error, structured_content=body, content=[SimpleNamespace(text=json.dumps(body))])


def _status_error(status: int, retry_after: str | None = None) -> httpx2.HTTPStatusError:
    request = httpx2.Request("POST", _URL)
    headers = {"retry-after": retry_after} if retry_after else {}
    return httpx2.HTTPStatusError("n8n", request=request, response=httpx2.Response(status, headers=headers, request=request))


class FakeN8n:
    """A scripted n8n MCP server: one queue of answers (or exceptions) per tool."""

    def __init__(self, **answers: list[Any]) -> None:
        self.answers = {name: list(values) for name, values in answers.items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sessions = 0
        self.base_url = _URL

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        answer = self.answers[name].pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @asynccontextmanager
    async def session(self):
        self.sessions += 1
        yield self


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


_DETAILS = _result({"workflow": {"id": "wfMoM", "active": True, "isArchived": False, "activeVersionId": "v1"}})
_STARTED = _result({"executionId": "7400", "status": "started"})


def _execution(status: str) -> SimpleNamespace:
    return _result({"execution": {"id": "7400", "status": status, "startedAt": "2026-11-27T01:00:00Z", "stoppedAt": None}})


def _data(last: str, items: list[dict[str, Any]] | None = None, error: dict[str, Any] | None = None) -> SimpleNamespace:
    run_data = {last: [{"data": {"main": [[{"json": item} for item in items]]}}]} if items is not None else {}
    result_data: dict[str, Any] = {"lastNodeExecuted": last, "runData": run_data}
    if error:
        result_data["error"] = error
    return _result({"execution": {"id": "7400", "status": "success"}, "data": {"resultData": result_data}})


async def _run(fake: FakeN8n, arguments: dict[str, Any], *, approval=None, deadline: float = 300.0, clock: Clock | None = None) -> dict[str, Any]:
    clock = clock or Clock()
    return await run_n8n_workflow(
        fake.session,
        approval or _approval(),
        arguments,
        source_id=_SRC,
        source_label="n8n",
        base_url=_URL,
        deadline_seconds=deadline,
        describe_failure=lambda exc: describe_remote_failure(exc, _SRC),
        pacer=HostRequestPacer(clock=clock, sleep=clock.sleep),
        clock=clock,
        sleep=clock.sleep,
    )


@pytest.mark.asyncio
async def test_a_run_executes_the_published_version_and_waits_for_the_last_node() -> None:
    fake = FakeN8n(
        get_workflow_details=[_DETAILS, _DETAILS],
        execute_workflow=[_STARTED],
        get_workflow_execution=[
            _execution("running"),
            _execution("running"),
            _execution("success"),
            _data("Respond"),
            _data("Respond", [{"region": "East", "mom": -4.6}]),
        ],
    )
    clock = Clock()
    result = await _run(fake, {"month": "2025-06"}, clock=clock)
    assert result["ok"] is True and result["status"] == "success"
    assert result["output"] == [{"region": "East", "mom": -4.6}] and result["item_count"] == 1 and result["truncated"] is False
    assert result["execution_url"] == "https://n8n.example.com/workflow/wfMoM/executions/7400"
    assert result["source"]["kind"] == "n8n" and result["version_changed_during_run"] is False
    assert fake.sessions == 1
    execute = next(arguments for name, arguments in fake.calls if name == "execute_workflow")
    assert execute == {
        "workflowId": "wfMoM",
        "executionMode": "production",
        "triggerNodeName": "Webhook",
        "inputs": {"webhookData": {"body": {"month": "2025-06"}}},
    }
    assert clock.slept[:3] == [1.0, 2.0, 4.0]
    reads = [arguments for name, arguments in fake.calls if name == "get_workflow_execution" and arguments.get("includeData")]
    assert [read["nodeNames"] for read in reads] == [[], ["Respond"]]


@pytest.mark.asyncio
async def test_a_changed_published_version_runs_nothing() -> None:
    changed = _result({"workflow": {"id": "wfMoM", "active": True, "isArchived": False, "activeVersionId": "v9"}})
    fake = FakeN8n(get_workflow_details=[changed], execute_workflow=[], get_workflow_execution=[])
    result = await _run(fake, {"month": "2025-06"})
    assert result["ok"] is False and result["error_kind"] == "version_changed" and result["error"].startswith("Nothing ran.")
    assert [name for name, _ in fake.calls] == ["get_workflow_details"]


@pytest.mark.asyncio
async def test_n8n_refusals_and_failed_runs_name_their_cause() -> None:
    refused = FakeN8n(
        get_workflow_details=[_DETAILS],
        execute_workflow=[_result({"executionId": None, "status": "error", "error": 'Trigger node "Webhook" requires inputs'})],
    )
    result = await _run(refused, {"month": "2025-06"})
    assert result["error_kind"] == "workflow_not_started" and "requires inputs" in result["error"] and result["execution_id"] is None

    failed = FakeN8n(
        get_workflow_details=[_DETAILS, _DETAILS],
        execute_workflow=[_STARTED],
        get_workflow_execution=[_execution("error"), _data("Pick", error={"message": "month is missing", "node": {"name": "Pick"}})],
    )
    result = await _run(failed, {"month": "2025-06"})
    assert result["ok"] is False and result["error_kind"] == "workflow_failed"
    assert "at node 'Pick'" in result["error"] and "month is missing" in result["error"] and result["execution_id"] == "7400"


@pytest.mark.asyncio
async def test_a_run_past_its_deadline_reports_running_with_the_link() -> None:
    fake = FakeN8n(get_workflow_details=[_DETAILS], execute_workflow=[_STARTED], get_workflow_execution=[_execution("running")] * 20)
    result = await _run(fake, {"month": "2025-06"}, deadline=20.0)
    assert result["ok"] is True and result["status"] == "running" and result["execution_url"].endswith("/7400")
    assert "do not start it again" in result["note"]
    assert [name for name, _ in fake.calls].count("execute_workflow") == 1


@pytest.mark.asyncio
async def test_a_rate_limited_read_waits_once_but_execute_is_never_repeated() -> None:
    fake = FakeN8n(
        get_workflow_details=[_DETAILS, _DETAILS],
        execute_workflow=[_STARTED],
        get_workflow_execution=[_status_error(429, "3"), _execution("success"), _data("Respond"), _data("Respond", [{"ok": 1}])],
    )
    result = await _run(fake, {"month": "2025-06"})
    assert result["ok"] is True and fake.sessions == 2
    assert [name for name, _ in fake.calls].count("execute_workflow") == 1

    limited = FakeN8n(get_workflow_details=[_DETAILS], execute_workflow=[_status_error(429, "3")])
    result = await _run(limited, {"month": "2025-06"})
    assert result["error_kind"] == "rate_limited" and result["error"].startswith("Nothing ran.") and limited.sessions == 1

    unknown = FakeN8n(get_workflow_details=[_DETAILS], execute_workflow=[_status_error(502)])
    result = await _run(unknown, {"month": "2025-06"})
    assert result["error_kind"] == "upstream_5xx" and "Whether n8n started the workflow is unknown" in result["error"]


def test_the_pacer_keeps_each_host_under_its_window() -> None:
    clock = Clock()
    pacer = HostRequestPacer(limit=3, window=10.0, clock=clock, sleep=clock.sleep)
    assert pacer.wait_needed("h", 3) == 0.0
    pacer._sent["h"].extend([clock.now] * 3)
    assert pacer.wait_needed("h", 1) == 10.0
    assert pacer.wait_needed("other", 1) == 0.0
    assert pacer.wait_needed("h", 4) == float("inf")


# ── the derived tool ───────────────────────────────────────────────────────


def _context(**values: Any) -> ToolExecutionContext:
    return ToolExecutionContext(workspace_id="ws", member_uid="m", user_id="user", turn_id="turn", datarc=None, team_id=_TEAM, **values)


@pytest.mark.asyncio
async def test_the_derived_tool_refuses_invalid_input_before_contacting_n8n() -> None:
    fake = FakeN8n()
    (spec,) = build_team_specs(_TEAM, [n8n_workflow_entry(_SRC, _approval())], fake)
    result = await spec.handler({"month": 6}, _context())
    assert result["error_kind"] == "input_invalid" and "month" in result["error"] and fake.sessions == 0
    assert spec.schema.input_schema == _SCHEMA and spec.annotations.read_only is True


def test_the_approved_category_decides_confirmation_not_n8n_annotations(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_TEAM_TOOL_CONFIRMATION", "1")
    read, write = build_team_specs(_TEAM, [n8n_workflow_entry(_SRC, _approval()), n8n_workflow_entry(_SRC, _approval("external_write"))], FakeN8n())
    assert (read.needs_approval, read.risk_level) == (False, "low")
    assert (write.needs_approval, write.risk_level, write.annotations.destructive) == (False, "medium", False)
    monkeypatch.setenv("FEATURE_TEAM_TOOL_CONFIRMATION", "0")
    read, write = build_team_specs(_TEAM, [n8n_workflow_entry(_SRC, _approval()), n8n_workflow_entry(_SRC, _approval("external_write"))], FakeN8n())
    assert read.needs_approval is False and write.needs_approval is True


@pytest.mark.asyncio
async def test_a_scheduled_turn_waits_longer(monkeypatch) -> None:
    seen: dict[str, float] = {}

    async def fake_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen["deadline"] = kwargs["deadline_seconds"]
        return {"ok": True}

    monkeypatch.setattr("bowhead_core.n8n_workflow_run.run_n8n_workflow", fake_run)
    (spec,) = build_team_specs(_TEAM, [n8n_workflow_entry(_SRC, _approval())], FakeN8n())
    await spec.handler({"month": "2025-06"}, _context())
    assert seen["deadline"] == 300.0
    await spec.handler({"month": "2025-06"}, _context(scheduled_run=SimpleNamespace(id="run")))
    assert seen["deadline"] == 900.0


# ── a real Streamable HTTP session ─────────────────────────────────────────


def _n8n_like_app():
    import asyncio as _asyncio

    from mcp.server import MCPServer

    server = MCPServer("n8n-like")
    polls = {"count": 0}

    @server.tool()
    async def get_workflow_details(workflowId: str, detailLevel: str = "full") -> dict[str, Any]:  # noqa: N803 — n8n's argument names
        return {"workflow": {"id": workflowId, "active": True, "isArchived": False, "activeVersionId": "v1"}, "triggerInfo": "webhook"}

    @server.tool()
    async def execute_workflow(workflowId: str, executionMode: str, triggerNodeName: str = "", inputs: dict | None = None) -> dict[str, Any]:  # noqa: N803
        await _asyncio.sleep(0)
        if executionMode != "production":
            return {"executionId": None, "status": "error", "error": "manual mode"}
        return {"executionId": "9001", "status": "started"}

    @server.tool()
    async def get_workflow_execution(
        workflowId: str,  # noqa: N803
        executionId: str,  # noqa: N803
        includeData: bool = False,  # noqa: N803
        nodeNames: list[str] | None = None,  # noqa: N803
        truncateData: int | None = None,  # noqa: N803
    ) -> dict[str, Any]:
        polls["count"] += 1
        status = "running" if polls["count"] < 2 else "success"
        execution = {"id": executionId, "workflowId": workflowId, "status": status, "startedAt": "2026-11-27T01:00:00Z", "stoppedAt": None}
        if not includeData:
            return {"execution": execution}
        run_data = {"Respond": [{"data": {"main": [[{"json": {"region": "East", "mom": -4.6}}]]}}]} if nodeNames else {}
        return {"execution": execution, "data": {"resultData": {"lastNodeExecuted": "Respond", "runData": run_data}}}

    return server.streamable_http_app(stateless_http=True, json_response=True)


@pytest.fixture(scope="module")
def n8n_like_url():
    import threading
    import time as _time

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(_n8n_like_app(), host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = _time.monotonic() + 30
    while _time.monotonic() < deadline and not (server.started and server.servers and server.servers[0].sockets):
        _time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.asyncio
async def test_a_run_holds_one_real_session_from_check_to_result(n8n_like_url) -> None:
    from bowhead_core.openapi_tool_source import RemoteMcpToolClient

    client = RemoteMcpToolClient(n8n_like_url, {})
    clock = Clock()
    result = await run_n8n_workflow(
        client.session,
        _approval(),
        {"month": "2025-06"},
        source_id=_SRC,
        source_label="n8n",
        base_url=client.base_url,
        deadline_seconds=60.0,
        describe_failure=lambda exc: describe_remote_failure(exc, _SRC),
        pacer=HostRequestPacer(clock=clock, sleep=clock.sleep),
        clock=clock,
        sleep=clock.sleep,
    )
    assert result["ok"] is True and result["status"] == "success" and result["execution_id"] == "9001"
    assert result["output"] == [{"region": "East", "mom": -4.6}]

    # A finished envelope raised inside the SDK's task groups still comes back as the result.
    stale = build_approval(
        workflow_id="wfMoM",
        workflow=_workflow(activeVersionId="v0"),
        description="d",
        input_schema=_SCHEMA,
        category="read",
        actor_user_id="u",
    )
    result = await run_n8n_workflow(
        client.session,
        stale,
        {"month": "2025-06"},
        source_id=_SRC,
        source_label="n8n",
        base_url=client.base_url,
        deadline_seconds=60.0,
        describe_failure=lambda exc: describe_remote_failure(exc, _SRC),
        pacer=HostRequestPacer(clock=clock, sleep=clock.sleep),
        clock=clock,
        sleep=clock.sleep,
    )
    assert result["error_kind"] == "version_changed"
