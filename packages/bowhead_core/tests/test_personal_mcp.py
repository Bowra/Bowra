"""Real migrated SQLite and HTTP consent; only the remote server is replaced.

Mutation checks remove the owner fence, approval wait and single-use claim in
turn: the corresponding cross-user, pending and replay assertions must fail.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace

import httpx2
import pytest
import pytest_asyncio
from alembic import command
from bowhead_core.agent_assignment_store import AgentAssignmentStore
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.personal_mcp import PersonalMcpError, PersonalMcpStore, build_personal_specs, canonical, digest, normalize_endpoint
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from cryptography.fernet import Fernet
from fastapi import FastAPI
from test_agent_execution_binding import _create_team_agent, _setup
from test_artifact_workspace_cutover_migration import _config

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0100_personal_mcp")


@pytest_asyncio.fixture
async def personal(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    fixture = await _setup(tmp_path)
    await _create_team_agent(replace(fixture, owner_id=fixture.member_id))
    await AgentAssignmentStore(fixture.db_path).backfill_existing_access(apply=True, source_build="test")
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE team_subagent SET audience_mode='private',visibility='creator_only' WHERE id='osa_exact'")
    binding = await AgentExecutionBindingStore(fixture.db_path).admit_team_execution(
        team_id=fixture.team_id, user_id=fixture.member_id, turn_id=fixture.turn_id, profile_name="exact-agent", dispatch_key="personal-call"
    )
    from bowhead_core.entitlements import CommunityResolver
    from bowhead_gateway._entitlement_gate import feature_license_checker

    license_app = FastAPI()
    license_app.state.deployment_mode = "local"
    license_app.state.entitlements = CommunityResolver(mode="local", user_store=fixture.users)
    license_check = feature_license_checker(license_app, "mcp")
    registry = PersonalMcpStore(fixture.db_path, license_check=license_check)
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}
    tool = {
        "remote_name": "search",
        "description": "Search documents",
        "schema_json": canonical(schema),
        "schema_digest": digest(schema),
        "risk": "unknown",
    }
    identity = await registry.save_connection(
        fixture.member_id, "My CRM", "http://localhost:9000/mcp", "bearer", {"value": "sensitive-personal-token"}, [tool]
    )
    connection = (await registry.connections(fixture.member_id))[0]
    tool_id = connection["tools"][0]["id"]
    await registry.configure(fixture.member_id, identity, 1, "ready", [tool_id])
    await registry.bind(fixture.member_id, fixture.team_id, "osa_exact", binding.revision_ref, [tool_id], 0)
    ctx = ToolExecutionContext(
        workspace_id=fixture.workspace_id,
        user_id=fixture.member_id,
        member_uid="",
        turn_id=fixture.turn_id,
        datarc=None,
        session_id="s_m3b",
        team_id=fixture.team_id,
        role="analyst",
        personal_mcp_execution_id=binding.execution_id,
        personal_mcp_call_id="call_one",
        personal_mcp_license_check=license_check,
    )
    return SimpleNamespace(
        fixture=fixture, registry=registry, binding=binding, ctx=ctx, connection_id=identity, tool_id=tool_id, license_app=license_app
    )


async def _client(personal, user_id=None):
    from bowhead_gateway.routes import personal_mcp as routes
    from bowhead_gateway.routes.auth import _current_user

    app = FastAPI()
    app.state = personal.license_app.state
    app.include_router(routes.router, prefix="/personal")
    user = await personal.fixture.users.get_user(user_id or personal.ctx.user_id)
    app.dependency_overrides[_current_user] = lambda: user
    app.dependency_overrides[routes._registry] = lambda: personal.registry
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test")


async def _wait_pending(personal, task):
    # Poll the stored row cheaply, then read the owner's pending view once. Polling
    # `pending()` itself revalidated licence and bindings on every iteration and,
    # under load, starved the handler past a 3-second wall clock. A handler that
    # finishes without enqueuing fails immediately; the deadline is only a hang guard.
    async def wait():
        while True:
            with sqlite3.connect(personal.fixture.db_path) as db:
                found = db.execute(
                    "SELECT 1 FROM personal_mcp_invocation WHERE owner_user_id=? AND status='pending' LIMIT 1", (personal.ctx.user_id,)
                ).fetchone()
            if found:
                return
            assert not task.done(), f"handler finished before requesting consent: {task.result()!r}"
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), 60)
    pending = await personal.registry.pending(personal.ctx.user_id)
    assert pending, "pending invocation was denied on revalidation"
    return pending[0]


def _remote(monkeypatch, *, failure=False):
    calls = []

    class Remote:
        async def call_tool(self, name, args):
            calls.append((name, args))
            if failure:
                raise TimeoutError("remote error with sensitive-personal-token")
            return SimpleNamespace(is_error=False, structured_content={"answer": "sensitive-personal-token", "query": args["query"]}, content=[])

    @asynccontextmanager
    async def connect(endpoint, headers, **kwargs):
        assert headers == {"Authorization": "Bearer sensitive-personal-token"}
        yield Remote()

    monkeypatch.setattr("bowhead_core.personal_mcp.connect_remote_mcp", connect)
    return calls


async def test_owner_consent_is_required_and_is_single_use(personal, monkeypatch):
    calls = _remote(monkeypatch)
    specs = await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx)
    assert len(specs) == 1
    assert len(specs[0].schema.name.replace(".", "__")) <= 64
    base = ToolRegistry()
    view = base.clone_with_spec_override(specs[0])
    assert not base.has(specs[0].schema.name)
    assert specs[0].schema.name not in view.overlay_names
    task = asyncio.create_task(specs[0].handler({"query": "private-query"}, personal.ctx))
    pending = await _wait_pending(personal, task)
    assert calls == []
    assert pending["arguments"] == {"query": "private-query"}
    async with await _client(personal, personal.fixture.owner_id) as other:
        assert (await other.get("/personal/calls/confirmations")).json() == {"confirmations": []}
        response = await other.post(f"/personal/calls/{pending['id']}/decision", json={"decision": "approve"})
        assert response.status_code == 404
    assert calls == []
    async with await _client(personal) as client:
        response = await client.post(f"/personal/calls/{pending['id']}/decision", json={"decision": "approve"})
        assert response.status_code == 200, response.text
    result = await asyncio.wait_for(task, 3)
    assert result == {"ok": True, "data": {"answer": "[redacted]", "query": "private-query"}}
    assert len(calls) == 1
    replay = await specs[0].handler({"query": "private-query"}, personal.ctx)
    assert replay["ok"] is False
    assert len(calls) == 1
    rows = await personal.registry.rows("SELECT * FROM personal_mcp_invocation")
    assert rows[0]["status"] == "ok" and rows[0]["args_cipher"] is None
    assert "private-query" not in canonical(rows)
    assert "sensitive-personal-token" not in canonical(await personal.registry.connections(personal.ctx.user_id))


async def test_rejection_and_disabled_connection_do_not_dispatch(personal, monkeypatch):
    calls = _remote(monkeypatch)
    spec = (await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx))[0]
    task = asyncio.create_task(spec.handler({"query": "private-query"}, personal.ctx))
    pending = await _wait_pending(personal, task)
    async with await _client(personal) as client:
        assert (await client.post(f"/personal/calls/{pending['id']}/decision", json={"decision": "reject"})).status_code == 200
    assert (await task)["error_kind"] == "personal_mcp_confirmation_rejected"
    personal.ctx.personal_mcp_call_id = "call_two"
    task = asyncio.create_task(spec.handler({"query": "private-query"}, personal.ctx))
    await _wait_pending(personal, task)
    await personal.registry.configure(personal.ctx.user_id, personal.connection_id, 2, "disabled", [])
    assert (await task)["ok"] is False
    assert calls == []


async def test_session_allowance_is_exact_and_revoked_with_the_connection(personal, monkeypatch):
    calls = _remote(monkeypatch)
    spec = (await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx))[0]
    first = asyncio.create_task(spec.handler({"query": "first"}, personal.ctx))
    pending = await _wait_pending(personal, first)
    async with await _client(personal) as client:
        response = await client.post(f"/personal/calls/{pending['id']}/decision", json={"decision": "approve_session"})
        assert response.status_code == 200, response.text
    assert (await asyncio.wait_for(first, 3))["ok"] is True
    personal.ctx.personal_mcp_call_id = "call_two"
    second = await asyncio.wait_for(spec.handler({"query": "second"}, personal.ctx), 3)
    assert second["ok"] is True
    assert calls == [("search", {"query": "first"}), ("search", {"query": "second"})]
    manifest = await personal.registry.manifest(personal.ctx.user_id, personal.binding)
    tool = json.loads(manifest["tools_json"])[0]
    assert not await personal.registry.session_allows(personal.ctx.user_id, "s_other", tool)
    await personal.registry.configure(personal.ctx.user_id, personal.connection_id, 2, "ready", [personal.tool_id])
    assert await personal.registry.rows("SELECT * FROM personal_mcp_session_tool_allowance") == []


async def test_timeout_and_restart_never_resend_an_external_action(personal, monkeypatch):
    calls = _remote(monkeypatch, failure=True)
    spec = (await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx))[0]
    task = asyncio.create_task(spec.handler({"query": "private-query"}, personal.ctx))
    pending = await _wait_pending(personal, task)
    async with await _client(personal) as client:
        assert (await client.post(f"/personal/calls/{pending['id']}/decision", json={"decision": "approve"})).status_code == 200
    result = await task
    assert result["error_kind"] == "personal_mcp_outcome_unknown"
    assert "sensitive-personal-token" not in canonical(result)
    await personal.registry.recover()
    assert (await spec.handler({"query": "private-query"}, personal.ctx))["ok"] is False
    assert len(calls) == 1


async def test_scope_schema_and_binding_changes_fail_closed(personal, monkeypatch):
    calls = _remote(monkeypatch)
    specs = await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx)
    other_ctx = copy.copy(personal.ctx)
    other_ctx.user_id = personal.fixture.owner_id
    assert (await asyncio.wait_for(specs[0].handler({"query": "x"}, other_ctx), 1))["error_kind"] == "personal_mcp_scope_denied"
    with sqlite3.connect(personal.fixture.db_path) as db:
        db.execute("UPDATE personal_mcp_tool SET schema_digest='changed' WHERE id=?", (personal.tool_id,))
    with pytest.raises(PersonalMcpError, match="binding_unavailable"):
        await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx)
    assert (await specs[0].handler({"query": "x"}, personal.ctx))["ok"] is False
    assert calls == []


async def test_shared_agent_keeps_owner_tools_out_of_other_users_runs(personal):
    with sqlite3.connect(personal.fixture.db_path) as db:
        db.execute("UPDATE team_subagent SET audience_mode='teamwide',visibility='team_wide' WHERE id='osa_exact'")
    assert len(await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx)) == 1
    other_ctx = copy.copy(personal.ctx)
    other_ctx.user_id = personal.fixture.owner_id
    other_binding = replace(personal.binding, execution_id="aex_other_user")
    assert await build_personal_specs(personal.registry, other_ctx.user_id, other_binding, other_ctx) == []
    assert (await personal.registry.agents(personal.ctx.user_id, personal.ctx.team_id))[0]["id"] == "osa_exact"
    await personal.registry.bind(personal.ctx.user_id, personal.ctx.team_id, "osa_exact", personal.binding.revision_ref, [personal.tool_id], 1)
    assert (await personal.registry.agents(personal.ctx.user_id, personal.ctx.team_id))[0]["binding_version"] == 2
    with pytest.raises(PersonalMcpError, match="binding_unavailable"):
        await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx)


async def test_expired_approval_cannot_be_claimed(personal):
    manifest = await personal.registry.manifest(personal.ctx.user_id, personal.binding)
    tool = json.loads(manifest["tools_json"])[0]
    call = await personal.registry.enqueue(personal.ctx.user_id, manifest, tool, "expires", {"query": "x"})
    with sqlite3.connect(personal.fixture.db_path) as db:
        db.execute("UPDATE personal_mcp_invocation SET expires_at='1970-01-01' WHERE id=?", (call["id"],))
    assert not await personal.registry.transition(personal.ctx.user_id, call["id"], "pending", "approved")
    await personal.registry.expire(personal.ctx.user_id)
    row = (await personal.registry.rows("SELECT status,args_cipher FROM personal_mcp_invocation WHERE id=?", (call["id"],)))[0]
    assert row == {"status": "expired", "args_cipher": None}


@pytest.mark.parametrize("audience", ["private", "teamwide"])
async def test_real_runner_advertises_personal_tools_only_to_owner_execution(personal, audience):
    from bowhead_core.events import TurnEventBus
    from bowhead_core.subagent import SubagentRunner
    from bowhead_router.types import LLMResponse

    class CapturingRouter:
        def __init__(self):
            self.tools = []

        async def invoke(self, agent_type, request, **kwargs):
            self.tools.append([tool.name for tool in request.tools or []])
            return LLMResponse(content="Done")

    if audience == "teamwide":
        with sqlite3.connect(personal.fixture.db_path) as db:
            db.execute("UPDATE team_subagent SET audience_mode='teamwide',visibility='team_wide' WHERE id='osa_exact'")
    router = CapturingRouter()
    base = ToolRegistry()
    runner = SubagentRunner(router=router, tool_registry=base, team_subagent_store=personal.fixture.users)
    result = await runner.run(
        profile_name=personal.binding.runtime_profile_name,
        task="Find documents",
        parent_turn_id=personal.ctx.turn_id,
        parent_bus=TurnEventBus(),
        parent_ctx=personal.ctx,
        exact_binding=personal.binding,
    )
    assert result.ok
    assert any(name.startswith("personalmcp.") for tools in router.tools for name in tools)
    assert not any(name.startswith("personalmcp.") for name in (schema.name for schema in base.to_schemas()))
    assert personal.ctx.tool_view is None


async def test_refresh_removes_bindings_and_invalidates_original_manifest(personal):
    manifest = await personal.registry.manifest(personal.ctx.user_id, personal.binding)
    tool = json.loads(manifest["tools_json"])[0]
    with sqlite3.connect(personal.fixture.db_path) as db:
        db.execute(
            "INSERT INTO personal_mcp_session_tool_allowance VALUES (?,?,?,?,?)",
            (personal.ctx.user_id, personal.ctx.session_id, personal.connection_id, personal.tool_id, "t"),
        )
    original = await personal.registry.connection(personal.ctx.user_id, personal.connection_id)
    await personal.registry.save_connection(
        personal.ctx.user_id,
        original["name"],
        original["endpoint"],
        "bearer",
        {"value": "updated-token"},
        [tool],
        connection_id=personal.connection_id,
        expected_version=2,
    )
    agents = await personal.registry.agents(personal.ctx.user_id, personal.ctx.team_id)
    assert json.loads(agents[0]["tool_ids_json"]) == []
    assert (await personal.registry.connections(personal.ctx.user_id))[0]["tools"][0]["selected"] == 0
    assert await personal.registry.rows("SELECT * FROM personal_mcp_session_tool_allowance") == []
    with pytest.raises(PersonalMcpError, match="binding_unavailable"):
        await personal.registry.revalidate(personal.ctx.user_id, manifest, tool, personal.ctx)


async def test_binding_list_carries_slug_for_agents_without_display_name(personal):
    with sqlite3.connect(personal.fixture.db_path) as db:
        db.execute("UPDATE team_subagent SET display_name='' WHERE id='osa_exact'")
        slug = db.execute("SELECT name FROM team_subagent WHERE id='osa_exact'").fetchone()[0]
    agents = await personal.registry.agents(personal.ctx.user_id, personal.ctx.team_id)
    assert [(agent["id"], agent["display_name"], agent["slug"]) for agent in agents] == [("osa_exact", "", slug)]
    assert slug


async def test_execution_trace_uses_friendly_name_without_consent_arguments(personal, monkeypatch):
    from bowhead_core.events import TurnEventBus
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.turn_runner import _invocations_to_trace_rows

    from bowhead_router import ToolCall

    _remote(monkeypatch)
    spec = (await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx))[0]
    base = ToolRegistry()
    personal.ctx.tool_view = base.clone_with_spec_override(spec)
    personal.ctx.personal_mcp_labels = {spec.schema.name: spec.personal_mcp_label}
    runner = SubagentRunner(router=SimpleNamespace(), tool_registry=base)
    bus = TurnEventBus()
    task = asyncio.create_task(
        runner._execute_tool(
            ToolCall(id="trace_call", name=spec.schema.name, arguments={"query": "private-query"}),
            personal.ctx,
            bus,
            parent_turn_id=personal.ctx.turn_id,
            subagent_id="osa_exact",
            running_profile="exact-agent",
        )
    )
    pending = await _wait_pending(personal, task)
    async with await _client(personal) as client:
        assert (await client.post(f"/personal/calls/{pending['id']}/decision", json={"decision": "reject"})).status_code == 200
    invocation = await asyncio.wait_for(task, 3)
    assert invocation.arguments == {}
    rows = _invocations_to_trace_rows(turn_id=personal.ctx.turn_id, intent=None, invocations=[invocation])
    assert rows[0].title == "My CRM · search"
    assert "private-query" not in str(rows)


async def test_lost_membership_hides_pending_parameters_and_prevents_execution(personal, monkeypatch):
    calls = _remote(monkeypatch)
    spec = (await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx))[0]
    task = asyncio.create_task(spec.handler({"query": "private-query"}, personal.ctx))
    pending = await _wait_pending(personal, task)
    with sqlite3.connect(personal.fixture.db_path) as db:
        db.execute("UPDATE workspace_members SET status='removed' WHERE user_id=?", (personal.ctx.user_id,))
    async with await _client(personal) as client:
        assert (await client.get("/personal/calls/confirmations")).json() == {"confirmations": []}
        assert (await client.post(f"/personal/calls/{pending['id']}/decision", json={"decision": "approve"})).status_code == 409
    assert (await asyncio.wait_for(task, 3))["ok"] is False
    assert calls == []


async def test_connection_discovery_ignores_readonly_hints_and_keeps_credentials_private(personal, monkeypatch):
    tools = [
        SimpleNamespace(
            name="read_records", description="Read records", input_schema={"type": "object"}, annotations=SimpleNamespace(read_only_hint=True)
        )
    ]
    observed = []

    class Remote:
        async def list_tools(self, *, params=None):
            return SimpleNamespace(tools=tools, next_cursor=None)

    @asynccontextmanager
    async def connect(endpoint, headers, **kwargs):
        observed.append((endpoint, headers))
        yield Remote()

    monkeypatch.setattr("bowhead_core.personal_mcp.connect_remote_mcp", connect)
    async with await _client(personal) as client:
        body = {
            "name": "Other service",
            "endpoint": "http://localhost:9001/mcp",
            "auth_type": "api_key",
            "header": "X-API-Key",
            "value": "another-secret",
        }
        response = await client.post("/personal", json=body)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["changes"]["added"] == ["read_records"]
        connection = next(item for item in payload["connections"] if item["id"] == payload["connection_id"])
        assert connection["tools"][0]["risk"] == "unknown" and connection["tools"][0]["selected"] == 0
        assert "another-secret" not in response.text and "auth_cipher" not in response.text
        body.pop("value")
        body["version"] = connection["version"]
        tools[0].input_schema = {"type": "object", "properties": {"id": {"type": "string"}}}
        refreshed = await client.put(f"/personal/{connection['id']}", json=body)
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["changes"]["changed"] == ["read_records"]
    assert observed == [("http://localhost:9001/mcp", {"X-API-Key": "another-secret"})] * 2
    async with await _client(personal, personal.fixture.owner_id) as other:
        assert (await other.put(f"/personal/{connection['id']}", json=body)).status_code == 404
        assert (await other.get("/personal")).json() == {"connections": []}


async def test_invalid_arguments_never_request_consent_or_reach_remote_service(personal, monkeypatch):
    calls = _remote(monkeypatch)
    spec = (await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx))[0]
    result = await spec.handler({"query": 42}, personal.ctx)
    assert result["error_kind"] == "personal_mcp_invalid_arguments"
    assert await personal.registry.rows("SELECT id FROM personal_mcp_invocation") == []
    assert calls == []


async def test_cold_worker_start_is_not_charged_to_the_validation_limit(personal, monkeypatch):
    import anyio.to_process

    from bowhead_core import personal_mcp

    original_open_process = anyio.to_process.open_process
    spawns = []

    async def slow_open_process(*args, **kwargs):
        # A busy host: starting the worker interpreter alone exceeds the limit.
        spawns.append(args)
        await asyncio.sleep(1.5)
        return await original_open_process(*args, **kwargs)

    monkeypatch.setattr(anyio.to_process, "open_process", slow_open_process)
    monkeypatch.setattr(personal_mcp, "VALIDATION_SECONDS", 1)
    calls = _remote(monkeypatch)
    spec = (await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx))[0]
    task = asyncio.create_task(spec.handler({"query": "private-query"}, personal.ctx))
    pending = await _wait_pending(personal, task)
    assert spawns, "the test must exercise a cold worker start"
    assert pending["arguments"] == {"query": "private-query"}
    assert calls == []
    async with await _client(personal) as client:
        assert (await client.post(f"/personal/calls/{pending['id']}/decision", json={"decision": "reject"})).status_code == 200
    assert (await asyncio.wait_for(task, 10))["error_kind"] == "personal_mcp_confirmation_rejected"


FAILED_COPY = "本次个人工具调用未执行。请检查确认状态，或到个人设置的我的连接修复；不要自动重试。"
# Catastrophic backtracking: never finishes within the validation limit.
HANGING_SCHEMA = {"type": "object", "properties": {"query": {"type": "string", "pattern": "^(a+)+$"}}}
HANGING_ARGUMENTS = {"query": "a" * 64 + "!"}


@pytest.fixture(autouse=True)
def _clear_hung_schemas():
    from bowhead_core import personal_mcp

    personal_mcp._hung_schemas.clear()
    yield
    personal_mcp._hung_schemas.clear()


def _validation_logs(logs):
    return [entry for entry in logs if entry["event"] == "personal_mcp.validation_unavailable"]


@asynccontextmanager
async def _slot_held_elsewhere():
    # Another task owns the slot, as a concurrent validation would.
    from bowhead_core import personal_mcp

    slot = personal_mcp._slot()
    acquired, release = asyncio.Event(), asyncio.Event()

    async def hold():
        async with slot:
            acquired.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await acquired.wait()
    try:
        yield slot
    finally:
        release.set()
        await holder


async def test_slow_validation_times_out_and_is_not_rerun(personal, monkeypatch):
    from structlog.testing import capture_logs

    from bowhead_core import personal_mcp

    real_validate = personal_mcp.validate_arguments
    runs = []

    async def hanging_schema(schema, arguments, key):
        runs.append(key)
        return await real_validate(HANGING_SCHEMA, HANGING_ARGUMENTS, key)

    monkeypatch.setattr(personal_mcp, "VALIDATION_SECONDS", 1)
    monkeypatch.setattr(personal_mcp, "validate_arguments", hanging_schema)
    calls = _remote(monkeypatch)
    spec = (await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx))[0]
    with capture_logs() as logs:
        result = await asyncio.wait_for(spec.handler({"query": "private-query"}, personal.ctx), 30)
    assert result == {"ok": False, "error_kind": "personal_mcp_call_failed", "error": FAILED_COPY}
    [entry] = _validation_logs(logs)
    assert entry["stage"] == "validate"
    assert entry["connection_id"] == personal.connection_id
    assert entry["tool"] == "search"
    assert entry["duration_ms"] >= 900
    assert "private-query" not in repr(logs) and HANGING_ARGUMENTS["query"] not in repr(logs)

    # The same schema is rejected at once, without waiting for (or taking) the slot.
    personal.ctx.personal_mcp_call_id = "call_two"
    async with _slot_held_elsewhere():
        with capture_logs() as logs:
            result = await asyncio.wait_for(spec.handler({"query": "private-query"}, personal.ctx), 1)
    assert result == {"ok": False, "error_kind": "personal_mcp_call_failed", "error": FAILED_COPY}
    assert [entry["stage"] for entry in _validation_logs(logs)] == ["known_hung"]
    assert len(runs) == 2
    assert await personal.registry.rows("SELECT id FROM personal_mcp_invocation") == []
    assert calls == []


async def test_validation_queue_wait_is_bounded_and_cancelled_waiters_release(monkeypatch):
    from structlog.testing import capture_logs

    from bowhead_core import personal_mcp

    monkeypatch.setattr(personal_mcp, "VALIDATION_QUEUE_SECONDS", 0.3)
    async with _slot_held_elsewhere() as slot:
        with capture_logs() as logs, pytest.raises(personal_mcp.ValidationUnavailable):
            await asyncio.wait_for(personal_mcp.validate_arguments({"type": "object"}, {}, ("pcm_queue", 1, "search")), 5)
        assert [entry["stage"] for entry in _validation_logs(logs)] == ["queue"]
        waiter = asyncio.create_task(personal_mcp.validate_arguments({"type": "object"}, {}, ("pcm_queue", 1, "search")))
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert slot.locked()
    # Neither the timed-out nor the cancelled waiter kept the slot.
    assert not slot.locked()
    assert await asyncio.wait_for(personal_mcp.validate_arguments({"type": "object"}, {}, ("pcm_queue", 1, "search")), 60)
    assert not slot.locked()


async def test_worker_start_failure_is_logged_at_the_warm_stage(personal, monkeypatch):
    import anyio.to_process
    from structlog.testing import capture_logs

    from bowhead_core import personal_mcp

    async def stuck_open_process(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(anyio.to_process, "open_process", stuck_open_process)
    monkeypatch.setattr(personal_mcp, "VALIDATION_WORKER_START_SECONDS", 0.3)
    calls = _remote(monkeypatch)
    spec = (await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx))[0]
    with capture_logs() as logs:
        result = await asyncio.wait_for(spec.handler({"query": "private-query"}, personal.ctx), 5)
    assert result == {"ok": False, "error_kind": "personal_mcp_call_failed", "error": FAILED_COPY}
    [entry] = _validation_logs(logs)
    assert (entry["stage"], entry["connection_id"], entry["tool"]) == ("warm", personal.connection_id, "search")
    # A start-up failure says nothing about the schema, so it is not remembered as hung.
    assert personal_mcp._hung_schemas == {}
    assert calls == []


async def test_dispatched_parameters_are_the_exact_confirmed_snapshot(personal, monkeypatch):
    calls = _remote(monkeypatch)
    spec = (await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx))[0]
    arguments = {"query": "private-query"}
    task = asyncio.create_task(spec.handler(arguments, personal.ctx))
    pending = await _wait_pending(personal, task)
    arguments["query"] = "changed-after-confirmation"
    assert pending["arguments"] == {"query": "private-query"}
    async with await _client(personal) as client:
        assert (await client.post(f"/personal/calls/{pending['id']}/decision", json={"decision": "approve"})).status_code == 200
    assert (await asyncio.wait_for(task, 3))["data"]["query"] == "private-query"
    assert calls == [("search", {"query": "private-query"})]


async def test_unwired_local_license_fails_closed(personal):
    personal.ctx.personal_mcp_license_check = None
    assert await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx) == []


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_migration_upgrades_existing_targets_and_is_idempotent(tmp_path, target):
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT name,sql FROM sqlite_schema WHERE type='table'").fetchall()
    command.upgrade(config, MIGRATION.revision)
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        after = db.execute("SELECT name,sql FROM sqlite_schema WHERE type='table'").fetchall()
        assert bool(db.execute("SELECT 1 FROM sqlite_schema WHERE name='personal_mcp_connection'").fetchone()) == (target == "core")
    if target != "core":
        assert before == after


@pytest.mark.unit
def test_deployment_address_policy(monkeypatch):
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    assert normalize_endpoint("http://127.0.0.1:9000/mcp") == "http://127.0.0.1:9000/mcp"
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    with pytest.raises(PersonalMcpError, match="address_blocked"):
        normalize_endpoint("http://127.0.0.1:9000/mcp")
