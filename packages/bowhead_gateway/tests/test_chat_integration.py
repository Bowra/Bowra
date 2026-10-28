"""Full-turn integration test: gateway + real Orchestrator + team Datarc connection (to a Datarc fake).

Runs "send -> stream -> done" on the current architecture:

- The Datarc client is resolved **only** from the caller team's connection registry
  (``datarc_connection_store`` + ``datarc_client_factory``); no deployment-level datarc is
  mounted on the app and there is no environment-variable fallback.
- Lead holds no BI tools and delegates the query to compute (``subagent_spawn``); compute runs
  ``bi_query`` through the team client against the Datarc fake, then ``subagent_return``.
- The chat-surface return from compute becomes the final answer directly (exec summary plus
  KPI lines); Lead takes no second model call, so the scripted Lead only ever spawns.
- The LLM is a role-scripted fake (Lead sees ``subagent_spawn``, compute sees ``bi_query``).

Asserts the live SSE event sequence and compares it item by item with the persisted replay
from ``/turns/{id}/events`` and the stored session (chat invariant: live, persisted and replayed
data agree). A second case covers a team without a connection while deployment-level Datarc
decoys are wired: the turn must take the data-unavailable path.
"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import time
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx2
import jwt
import pytest
import respx
from bi_mock.main import app as bi_mock_app
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.session_store import SessionStore
from bowhead_core.subagent import PROFILES
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.main import create_app
from bowhead_gateway.routes import chat as chat_routes
from bowhead_gateway.settings import Settings
from fakes_tool_context import FakeUserMemoryToolContext

from bowhead_bi_bridge import DatarcClient
from bowhead_core import (
    Orchestrator,
    OrchestratorConfig,
    build_default_registry,
    get_event_registry,
)
from bowhead_router import (
    AgentBinding,
    LLMChunk,
    LLMRequest,
    LLMResponse,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
    ToolCall,
)


@pytest.fixture(autouse=True)
def _instance_encryption_root(monkeypatch):
    """Deployments always set BOWHEAD_FERNET_KEY; since 9c6fe1654 every chat worker turn builds the
    physical-usage receipt cipher from it. Without .env in the checkout nothing else provides it."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")


_JWT_SECRET = "test_secret_chat_integration_roundtrip"
_TEAM_ID = "team_chat_integration"
_TEAM_BASE_URL = "http://datarc-team.example/"
_TEAM_AUTH = "team_conn_auth"
_DATARC_WS = "1"  # Datarc workspace actually present in bi_mock.
# Values the scripted compute returns; the final answer is rendered from them verbatim.
_COMPUTE_SUMMARY = "今年营业额 29.3 亿元。"
_COMPUTE_KPI = {"label": "今年营业额", "value": "29.3亿", "sentiment": "neutral"}
_FINAL_ANSWER = f"{_COMPUTE_SUMMARY}\n\n- {_COMPUTE_KPI['label']}: {_COMPUTE_KPI['value']}"
# Deployment-level Datarc decoys for the negative case: an app.state client and the
# historical env settings. Neither may ever be used; Datarc comes only from the team registry.
_DEPLOY_BASE_URL = "http://datarc-deploy.example/"
_DEPLOY_ENV = {
    "BOWHEAD_BI_BASE_URL": _DEPLOY_BASE_URL,
    "BOWHEAD_DATARC_URL": _DEPLOY_BASE_URL,
    "BOWHEAD_DATARC_BASE_URL": _DEPLOY_BASE_URL,
    "BOWHEAD_DATARC_DEV_AUTH_KEY": "deploy_env_auth",
    "BOWHEAD_DATARC_DEV_ACCOUNT_UID": "deploy_account",
    "BOWHEAD_DATARC_DEV_MEMBER_UID": "deploy_member",
    "BOWHEAD_DATARC_IFRAME_IDENTITY": "deploy_identity",
    "BOWHEAD_DEV_DEFAULT_WORKSPACE": _DATARC_WS,
}


def _jwt() -> str:
    now = int(time.time())
    return jwt.encode({"sub": "u_alice", "team_id": _TEAM_ID, "iat": now, "exp": now + 3600}, _JWT_SECRET, algorithm="HS256")


@dataclass
class _RoleScriptedAdapter:
    """Dispatches the script by the tools each request advertises: Lead spawns, compute
    queries and returns (that return is the final answer).

    Other model calls (such as the session title) get plain text and do not advance the script.
    """

    provider_name: str = "p1"
    calls: list[str] = field(default_factory=list)
    lead_systems: list[str] = field(default_factory=list)

    def _respond(self, req: LLMRequest) -> LLMResponse:
        tools = {tool.name for tool in req.tools or []}
        tool_turns = sum(1 for message in req.messages if message.role == "tool")
        if "subagent_spawn" in tools:
            self.calls.append(f"lead:{tool_turns}")
            self.lead_systems.append(req.system or "")
            return LLMResponse(
                content="我让 compute 去查今年营业额。",
                tool_calls=[
                    ToolCall(
                        id="spawn_compute",
                        name="subagent_spawn",
                        # The whole request is delegated, so compute's return is the final answer;
                        # helper or unlabelled dispatches return to Lead (composer-reference-closeout.md).
                        arguments={"profile": "compute", "task": "查询今年营业额并返回结果", "delegation_role": "primary"},
                    )
                ],
            )
        if "bi_query" in tools:
            self.calls.append(f"compute:{tool_turns}")
            if tool_turns == 0:
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="query_gmv", name="bi_query", arguments={"queries": ["今年营业额"]})],
                )
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="return_gmv",
                        name="subagent_return",
                        arguments={
                            "payload": {
                                "delivery_surface": "chat",
                                "exec_summary": _COMPUTE_SUMMARY,
                                "kpis": [_COMPUTE_KPI],
                            }
                        },
                    )
                ],
            )
        self.calls.append("other")
        return LLMResponse(content="营业额查询")

    async def invoke(self, req: LLMRequest) -> LLMResponse:
        resp = self._respond(req)
        return LLMResponse(
            content=resp.content,
            tool_calls=list(resp.tool_calls),
            finish_reason="tool_use" if resp.tool_calls else "stop",
            usage=TokenUsage(input_tokens=10, output_tokens=10),
            raw={"mock": True},
            provider=self.provider_name,
            model=req.model or "mock-model",
        )

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMChunk]:
        resp = await self.invoke(req)
        if resp.content:
            yield LLMChunk(delta=resp.content)
        for call in resp.tool_calls:
            yield LLMChunk(tool_call_delta=call)
        yield LLMChunk(finish_reason=resp.finish_reason, usage=resp.usage)

    async def close(self) -> None:
        pass


class _Datarc:
    """北极九章替身：schema 端点交给 bi_mock，行级取数（cut + taskType=22）在这里模拟。

    记下每个请求的路径与凭据，用来证明查数走的是团队连接。
    """

    obs_url = "https://obs.datarc-mock.example/rows.zip"

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self._tasks: dict[str, dict[str, Any]] = {}

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await bi_mock_app(scope, receive, send)
            return
        headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", [])}
        path = scope["path"]
        self.requests.append((path, headers.get("authorization", "")))
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        data: Any
        if path.endswith("/data/cut/"):
            data = [
                {"word": "今年", "prop": "dateRange_2026010120261231", "unitIdx": 0},
                {"word": "营业额", "prop": "m17", "unitIdx": 1},
            ]
        elif path == "/openapi/v1/async-tasks/" and scope["method"] == "POST":
            uid = f"task_{len(self._tasks) + 1}"
            params = json.loads(body or b"{}").get("params", {})
            self._tasks[uid] = {"uid": uid, "taskType": 22, "status": 2, "params": params, "result": {"url": self.obs_url}}
            data = self._tasks[uid]
        elif path.startswith("/openapi/v1/async-tasks/"):
            data = self._tasks[path.rstrip("/").rsplit("/", 1)[-1]]
        else:

            async def replay_body() -> dict[str, Any]:
                return {"type": "http.request", "body": body, "more_body": False}

            await bi_mock_app(scope, replay_body, send)
            return
        payload = json.dumps({"errcode": 200, "errmsg": "success", "data": data}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": payload})


def _rows_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("rows.csv", "年份,营业额\n2026,2930123456.7\n")
    return buffer.getvalue()


class _TeamConnectionStore:
    """团队保存的北极九章连接——client 的唯一来源。"""

    def __init__(self, *, connected: bool = True) -> None:
        self.teams: list[str] = []
        self.connected = connected

    async def list_connections(self, team_id: str) -> list[Any]:
        self.teams.append(team_id)
        return [SimpleNamespace(id="conn_team", base_url=_TEAM_BASE_URL)] if self.connected and team_id == _TEAM_ID else []

    async def get_connection_auth(self, conn_id: str, team_id: str) -> str:
        assert (conn_id, team_id) == ("conn_team", _TEAM_ID)
        return _TEAM_AUTH


class _DatarcClientFactory:
    """真 DatarcClient，只把传输层接到北极九章替身（不出网）。"""

    def __init__(self, datarc: _Datarc) -> None:
        self._datarc = datarc
        self.calls: list[tuple[str, str]] = []
        self.clients: list[DatarcClient] = []

    def get_client(self, base_url: str, auth_key: str) -> DatarcClient:
        self.calls.append((base_url, auth_key))
        client = _asgi_datarc_client(base_url, auth_key, self._datarc)
        self.clients.append(client)
        return client


def _asgi_datarc_client(base_url: str, auth_key: str, datarc: _Datarc) -> DatarcClient:
    """A real DatarcClient whose transport goes to an in-process Datarc fake."""
    client = DatarcClient(base_url, auth_key)
    headers = dict(client._http.headers)  # noqa: SLF001
    client._http = httpx2.AsyncClient(  # noqa: SLF001
        transport=httpx2.ASGITransport(app=datarc),
        base_url=client._base_url,  # noqa: SLF001
        headers=headers,
        timeout=10,
    )
    return client


class _ToolContextService:
    async def accessible_workspaces(self, datarc_client: object, user_id: str) -> tuple[Any, ...]:
        from bowhead_gateway.connector_context_service import DatarcWorkspaceRef

        assert isinstance(datarc_client, DatarcClient)
        return (DatarcWorkspaceRef(_DATARC_WS, "零售集团"),)

    async def accessible_workspaces_ex(self, datarc_client: object, user_id: str) -> tuple[tuple[Any, ...], str]:
        return await self.accessible_workspaces(datarc_client, user_id), ""


class _MemberResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    async def resolve(self, identity: str, workspace_id: str, *, datarc: Any = None) -> str:
        self.calls.append((identity, workspace_id, datarc))
        return "member_alice"


def _seed_membership(core_path: Any) -> None:
    """Persist the active Team/Space membership that the JWT claims.

    Lead delegation admits peer collaboration (16e44bbc5), which re-reads current
    membership from the core database; real turns always have these rows.
    Chat send also reads the signed-in user from the user store for Coach binding
    and credit admission, so the user and Team carry credit balances.
    """
    now = "2026-10-01T00:00:00+00:00"
    with sqlite3.connect(core_path) as db:
        db.execute(
            "INSERT INTO teams(id, name, team_credit_balance, created_at) VALUES (?, ?, ?, ?)",
            (_TEAM_ID, "Chat integration", 1_000_000, now),
        )
        db.execute(
            "INSERT INTO users(id, team_id, email, credit_balance, created_at) VALUES (?, ?, ?, ?, ?)",
            ("u_alice", _TEAM_ID, "alice@example.com", 1_000_000, now),
        )
        db.execute("INSERT INTO team_members(user_id, team_id, role, created_at) VALUES (?, ?, ?, ?)", ("u_alice", _TEAM_ID, "member", now))
        db.execute(
            "INSERT INTO workspaces(id, team_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("ws_chat", _TEAM_ID, "Chat integration", now, now),
        )
        db.execute("INSERT INTO workspace_members(workspace_id, user_id, joined_at) VALUES (?, ?, ?)", ("ws_chat", "u_alice", now))


@asynccontextmanager
async def _gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, *, team_connected: bool) -> AsyncIterator[SimpleNamespace]:
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")
    # This hand-built Orchestrator has no reference materializer (main.py wires one); 5a7b5fed7 defaults it on.
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "0")
    if not team_connected:
        for name, value in _DEPLOY_ENV.items():
            monkeypatch.setenv(name, value)

    adapter = _RoleScriptedAdapter()
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="p1", type="mock")])
    providers._adapters["p1"] = adapter  # type: ignore[attr-defined]  # noqa: SLF001
    router = Router(providers)
    for agent_type in ("lead", "analyst", "compute", "skill", "echo", "judge"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("p1", "mock-model")))

    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}", target="core") == ("core",)
    _seed_membership(core_path)
    session_store = SessionStore(core_path)
    await session_store.initialize()
    turn_log_store = TurnLogStore(str(core_path))
    await turn_log_store.initialize()
    event_registry = get_event_registry()
    previous_log_sink = event_registry._default_log_sink  # noqa: SLF001
    event_registry.set_log_sink(OrderedTurnLogSink(turn_log_store))

    # Load actual system manifests into global PROFILES (Lead delegates; compute has bi_query), restoring unchanged afterward.
    profiles_before = dict(PROFILES)
    from bowhead_core.agents.turn_runner_v2_entry import bootstrap_multi_agent
    from bowhead_core.subagent import register_subagent_tool
    from bowhead_core.subagent_return_tool import register_subagent_return_tool
    from bowhead_core.turn_runner import _make_subagent_runner_with_catalog

    # Same order as Gateway lifespan: default tools + delegation/return tools, then bootstrap manifest injection.
    tool_registry = build_default_registry()
    register_subagent_tool(tool_registry)
    register_subagent_return_tool(tool_registry)
    orchestrator = Orchestrator(
        router=router,
        datarc=None,
        tool_registry=tool_registry,
        event_registry=event_registry,
        config=OrchestratorConfig(),
    )
    orchestrator.dispatch_owner_id = "gateway_chat_integration_test"
    orchestrator.session_store = session_store
    orchestrator.multi_agent_registry = bootstrap_multi_agent(tool_registry)  # type: ignore[attr-defined]
    orchestrator.subagent_runner = _make_subagent_runner_with_catalog(  # type: ignore[attr-defined]
        router=router,
        tool_registry=tool_registry,
        custom_store=None,
        product_terms_catalog=None,
    )

    datarc = _Datarc()
    factory = _DatarcClientFactory(datarc)
    connections = _TeamConnectionStore(connected=team_connected)
    resolver = _MemberResolver()
    app = create_app()
    app.state.settings = Settings()
    app.state.orchestrator = orchestrator
    app.state.session_store = session_store
    # Chat send binds Coach resources to the signed-in user, which it reads from the user store.
    app.state.user_store = UserStore(str(core_path))
    app.state.turn_log_store = turn_log_store
    app.state.datarc_connection_store = connections
    app.state.datarc_client_factory = factory
    app.state.tool_context_service = _ToolContextService()
    app.state.user_memory_store = FakeUserMemoryToolContext()
    app.state.member_resolver = resolver
    deploy_datarc = _Datarc()
    deploy_client = None
    if team_connected:
        assert getattr(app.state, "datarc", None) is None  # no deployment-level Datarc connection
    else:
        deploy_client = _asgi_datarc_client(_DEPLOY_BASE_URL, "deploy_state_auth", deploy_datarc)
        app.state.datarc = deploy_client

    async def _verified_email(_request: Any) -> str:
        return "alice@example.com"

    monkeypatch.setattr(chat_routes, "_caller_verified_email", _verified_email)

    from bowhead_gateway import turn_service

    async def _team_runtime(*_args: Any, **_kwargs: Any) -> Any:
        return turn_service.TeamModelRuntimeResolution(router=router, result="team_safe_success", reason="resolved")

    monkeypatch.setattr(turn_service, "resolve_team_model_runtime", _team_runtime)

    try:
        yield SimpleNamespace(
            app=app,
            adapter=adapter,
            datarc=datarc,
            factory=factory,
            connections=connections,
            resolver=resolver,
            session_store=session_store,
            deploy_datarc=deploy_datarc,
        )
    finally:
        if deploy_client is not None:
            await deploy_client.aclose()
        event_registry.set_log_sink(previous_log_sink)
        PROFILES.clear()
        PROFILES.update(profiles_before)
        for client in factory.clients:
            await client.aclose()
        await providers.close_all()


@pytest.fixture
async def gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> AsyncIterator[SimpleNamespace]:
    async with _gateway(monkeypatch, tmp_path, team_connected=True) as gw:
        yield gw


@pytest.fixture
async def gateway_without_team_connection(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> AsyncIterator[SimpleNamespace]:
    """The team registry has no connection, while deployment-level Datarc decoys are wired."""
    async with _gateway(monkeypatch, tmp_path, team_connected=False) as gw:
        yield gw


def _parse_sse(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("event:"):
            events.append({"event": line[len("event:") :].strip()})
        elif line.startswith("data:") and events:
            try:
                events[-1]["data"] = json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                events[-1]["data"] = line[len("data:") :].strip()
    return events


@pytest.mark.integration
@pytest.mark.slow  # bi_query waits one real 1.5s poll interval on the async fetch task; the call phase takes about 4s
async def test_chat_send_and_stream_roundtrip(gateway: SimpleNamespace) -> None:
    """发送 → 流式 → done：团队连接查数、Lead 派 compute、live 与持久化重放一致。"""
    headers = {"Authorization": f"Bearer {_jwt()}"}
    transport = httpx2.ASGITransport(app=gateway.app)
    with respx.mock(assert_all_called=True) as obs:
        # Query-result ZIP in object storage (presigned URL without credentials); other requests use ASGI, no network.
        obs.get(_Datarc.obs_url).mock(return_value=httpx2.Response(200, content=_rows_zip()))
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw", headers=headers) as client:
            resp = await client.post(
                "/api/v1/chat/send",
                json={"workspace_id": "ws_chat", "message": "今年营业额是多少？"},
            )
            assert resp.status_code == 200, resp.text
            send_body = resp.json()
            turn_id = send_body["turn_id"]
            assert turn_id.startswith("t_")

            lines: list[str] = []
            async with asyncio.timeout(60):
                async with client.stream("GET", send_body["stream_url"]) as stream:
                    async for line in stream.aiter_lines():
                        lines.append(line)
            live = _parse_sse(lines)

            replay_resp = await client.get(f"/api/v1/turns/{turn_id}/events")
            assert replay_resp.status_code == 200, replay_resp.text

    # ── Live sequence: Lead delegates to compute, compute queries/returns, turn completes ──
    names = [e["event"] for e in live]
    assert names[0] == "turn_started", names
    assert names[-1] == "done", names
    assert live[-1]["data"]["status"] == "completed"
    spawned = [(e["data"]["profile"], e["data"].get("status")) for e in live if e["event"] == "agent_spawned"]
    assert spawned == [("lead", None), ("compute", None), ("compute", "completed"), ("lead", "completed")], spawned
    tool_results = [(e["data"]["tool_name"], e["data"]["ok"]) for e in live if e["event"] == "tool_result"]
    assert tool_results == [("bi_query", True), ("subagent_return", True), ("subagent_spawn", True)], tool_results
    step_ids = [e["data"]["step_id"] for e in live if e["event"] == "step_done"]
    assert step_ids == ["step_query_gmv", "step_return_gmv", "step_spawn_compute"], step_ids
    final = [e["data"]["text"] for e in live if e["event"] == "final_answer"]
    assert final == [_FINAL_ANSWER], final

    # ── Only caller-team connections, queries initiated by compute ──
    assert set(gateway.connections.teams) == {_TEAM_ID}
    assert gateway.factory.calls and set(gateway.factory.calls) == {(_TEAM_BASE_URL, _TEAM_AUTH)}
    assert {auth for _path, auth in gateway.datarc.requests} == {f"Bearer {_TEAM_AUTH}"}
    task_creates = [path for path, _auth in gateway.datarc.requests if path == "/openapi/v1/async-tasks/"]
    assert len(task_creates) == 1, gateway.datarc.requests
    assert [call for call in gateway.adapter.calls if call != "other"] == ["lead:0", "compute:0", "compute:1"]
    assert gateway.resolver.calls and {call[1] for call in gateway.resolver.calls} == {_DATARC_WS}
    assert gateway.adapter.lead_systems and not any("<no_data_source>" in system for system in gateway.adapter.lead_systems)

    # ── Persistence/replay: same events and final answer as live ──
    replay = replay_resp.json()["events"]
    assert [e["event_type"] for e in replay] == names
    assert [e["payload"]["evt_seq"] for e in replay] == [e["data"]["evt_seq"] for e in live]
    replay_final = [e["payload"]["text"] for e in replay if e["event_type"] == "final_answer"]
    assert replay_final == final
    persisted = await gateway.session_store.get_turn(turn_id)
    assert persisted is not None
    assert persisted.status == "completed"
    assert persisted.final_answer == final[0]


@pytest.mark.integration
async def test_team_without_connection_never_falls_back_to_deployment_datarc(
    gateway_without_team_connection: SimpleNamespace,
) -> None:
    """No team connection means BI is unavailable for the turn, even with deployment-level Datarc wired."""
    gw = gateway_without_team_connection
    headers = {"Authorization": f"Bearer {_jwt()}"}
    transport = httpx2.ASGITransport(app=gw.app)
    with respx.mock(assert_all_called=False) as outbound:
        # Any request to the env-configured Datarc host would land here instead of the network.
        deploy_env_route = outbound.route(host="datarc-deploy.example").mock(return_value=httpx2.Response(500))
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw", headers=headers) as client:
            resp = await client.post(
                "/api/v1/chat/send",
                json={"workspace_id": "ws_chat", "message": "今年营业额是多少？"},
            )
            assert resp.status_code == 200, resp.text
            send_body = resp.json()
            lines: list[str] = []
            async with asyncio.timeout(60):
                async with client.stream("GET", send_body["stream_url"]) as stream:
                    async for line in stream.aiter_lines():
                        lines.append(line)
    live = _parse_sse(lines)

    assert live[-1]["event"] == "done", [e["event"] for e in live]
    # The Lead is told this space has no data source (the no-connection variant of the notice).
    assert gw.adapter.lead_systems
    assert all("no BI data source (datarc) connected" in system for system in gw.adapter.lead_systems)
    bi_results = [e["data"] for e in live if e["event"] == "tool_result" and e["data"]["tool_name"] == "bi_query"]
    assert bi_results and not any(result["ok"] for result in bi_results), bi_results
    assert all("DatarcUnavailableError" in json.dumps(result, ensure_ascii=False) for result in bi_results), bi_results

    # Neither deployment-level decoy nor the team fake saw a single request.
    assert gw.deploy_datarc.requests == []
    assert deploy_env_route.call_count == 0
    assert gw.datarc.requests == []
    assert gw.factory.calls == []
    assert gw.resolver.calls == []
    assert set(gw.connections.teams) == {_TEAM_ID}
