"""Artifacts written by an agent run are attributed to the run's accountable user.

Every origin hands the orchestrator a ``TurnRequest``; the turn runner copies its ``user_id``
into the tool context, and artifact-writing tools record it as ``created_by``.

- Web chat runs end to end: real gateway ``/chat/send`` → real ``Orchestrator``.
- Feishu and scheduled jobs run their real entry points until they hand off the
  ``TurnRequest``; that captured request is then run through a real ``Orchestrator``.

In each run the scripted Lead calls ``web_fetch`` through the real dispatch. The registered spec is
wrapped by a probe that receives the tool context the turn runner built and calls the real
``web_fetch`` and ``image_generate`` handlers with it (only the network/model clients are
faked; ``image_generate`` gets the shallow copy a peer would receive), then reads every
artifact back the way agent tools do (``ArtifactStore.get(..., check_created_by=ctx.user_id)``)
in the same run and in a later turn.

An owner-less write (``created_by`` NULL) still passes that read gate, so read-back alone would
not catch a regression: the tests also pin ``created_by`` to the accountable user.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.media import image_gen_tool
from bowhead_core.media.config_loader import MediaModelRegistry, MediaModelSpec
from bowhead_core.media.image_gen import GeneratedImage
from bowhead_core.session_store import SessionStore
from bowhead_core.web_fetch import register_web_fetch_tool

from bowhead_core import EventBusRegistry, Orchestrator, OrchestratorConfig, TurnRequest, build_default_registry
from bowhead_router import (
    AgentBinding,
    LLMChunk,
    LLMRequest,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
    ToolCall,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _instance_encryption_root(monkeypatch):
    """Deployments always set BOWHEAD_FERNET_KEY; since 9c6fe1654 every chat worker turn builds the
    physical-usage receipt cipher from it. Without .env in the checkout nothing else provides it."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_PAGE = "https://example.com/page"


class _WebStream:
    status_code = 200
    headers = {"content-type": "text/html"}  # noqa: RUF012

    async def __aenter__(self) -> _WebStream:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def aiter_bytes(self):  # noqa: ANN201
        yield b"<html><title>Page</title><body><p>fetched body</p></body></html>"


class _WebClient:
    async def __aenter__(self) -> _WebClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def stream(self, method: str, url: str, headers: dict[str, str] | None = None) -> _WebStream:
        return _WebStream()


class _ImageClient:
    def __init__(self, spec: MediaModelSpec) -> None:
        self.spec = spec

    async def generate(self, **_kwargs: Any) -> list[GeneratedImage]:
        return [GeneratedImage(image_bytes=_PNG, mime="image/png", width=8, height=8, revised_prompt="p")]


def _image_handler():  # noqa: ANN202
    spec = MediaModelSpec(
        name="img",
        capability="image_gen",
        provider="aliyun_dashscope_native",
        real_id="img",
        base_url="https://media.example/api/v1",
        api_key="sk-test",
        timeout_sec=30,
        protocol="dashscope_async",
    )
    registry = MediaModelRegistry(by_name={spec.name: spec}, active={"image_gen": spec.name})

    async def resolve(_team_id: str) -> MediaModelRegistry:
        return registry

    return image_gen_tool._make_handler(registry, resolve)


@pytest.fixture
def faked_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake only the outbound HTTP and image-model clients; tool handlers stay real."""
    monkeypatch.setattr("bowhead_core.web_fetch.validate_url", lambda *a, **k: None)
    monkeypatch.setattr("bowhead_core.web_fetch.build_guarded_async_client", lambda **k: _WebClient())
    monkeypatch.setattr(image_gen_tool, "ImageGenClient", _ImageClient)


@pytest.fixture(autouse=True)
def _restore_profiles():  # noqa: ANN202
    """``bootstrap_multi_agent`` loads system manifests into the global PROFILES; put them back."""
    from bowhead_core.subagent import PROFILES

    before = dict(PROFILES)
    yield
    PROFILES.clear()
    PROFILES.update(before)


@dataclass
class _LeadCallsWebFetch(MockAdapter):
    """The Lead (the only profile offered ``subagent_spawn``) calls ``web_fetch`` once, then answers.

    Other model calls (session title and the like) get plain text.
    """

    provider_name: str = "p1"

    async def invoke(self, req: LLMRequest) -> LLMResponse:
        tools = {tool.name for tool in req.tools or []}
        called = any(message.role == "tool" for message in req.messages)
        calls = [ToolCall(id="fetch_page", name="web_fetch", arguments={"url": _PAGE})] if "web_fetch" in tools and not called else []
        return LLMResponse(
            content="" if calls else "Done.",
            tool_calls=calls,
            finish_reason="tool_use" if calls else "stop",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            provider=self.provider_name,
            model=req.model or "mock",
        )

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMChunk]:
        resp = await self.invoke(req)
        if resp.content:
            yield LLMChunk(delta=resp.content)
        for call in resp.tool_calls:
            yield LLMChunk(tool_call_delta=call)
        yield LLMChunk(finish_reason=resp.finish_reason, usage=resp.usage)


class _AgentRuntime:
    """Real Orchestrator + real ArtifactStore, wired like gateway startup (system manifests loaded).

    The scripted Lead calls ``web_fetch`` each turn. The first call writes through the real tool
    handlers; every call reads all written ids back the way agent tools do.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.writes: list[str] = []
        self.sources: dict[str, str] = {}
        self.seen_users: dict[str, str] = {}
        self.readback: dict[str, list[str | None]] = {}
        self.adapter = _LeadCallsWebFetch()

    async def start(self, db_path: Path | None = None) -> None:
        from bowhead_cli.migrate import TARGET_FILES, upgrade

        db = db_path or self.tmp_path / TARGET_FILES["core"]
        if db_path is None:
            upgrade(f"sqlite+aiosqlite:///{db}", target="core")
        self.sessions = SessionStore(db)
        await self.sessions.initialize()
        self.store = ArtifactStore(backend=LocalBackend(root_dir=self.tmp_path / "blobs"), db_path=db)
        await self.store.initialize()

    async def _probe(self, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        self.seen_users[ctx.turn_id] = ctx.user_id
        assert ctx.artifact_store is self.store
        if not self.writes:
            # The URL provenance gate only lets the agent fetch hosts it has seen in this turn.
            seen = getattr(ctx, "_seen_url_hosts", None)
            if isinstance(seen, set):
                seen.add("example.com")
            fetched = await self._web_fetch(args, ctx)
            assert "artifact_id" in fetched, fetched
            # A peer (marketing-analyst) gets a shallow copy of this context; call the real
            # image tool with it. The team id only selects the faked image model.
            image = await _image_handler()({"prompt": "poster"}, replace(copy.copy(ctx), team_id=ctx.team_id or "team_probe"))
            assert image.get("images"), image
            self.sources[fetched["artifact_id"]] = "web_fetch"
            for item in image["images"]:
                self.sources[item["artifact_id"]] = "image_generate"
            self.writes = list(self.sources)
        found = [await ctx.artifact_store.get(aid, check_created_by=ctx.user_id) for aid in self.writes]
        self.readback[ctx.turn_id] = [art.id if art is not None else None for art in found]
        return {"url": _PAGE, "title": "Page", "content": "fetched body"}

    def build(self, event_registry: EventBusRegistry) -> Orchestrator:
        from bowhead_core.agents.turn_runner_v2_entry import bootstrap_multi_agent
        from bowhead_core.subagent import register_subagent_tool
        from bowhead_core.subagent_return_tool import register_subagent_return_tool
        from bowhead_core.turn_runner import _make_subagent_runner_with_catalog

        providers = ProviderRegistry()
        providers.load([ProviderConfig(name="p1", type="mock")])
        providers._adapters["p1"] = self.adapter  # type: ignore[attr-defined]  # noqa: SLF001
        router = Router(providers)
        for agent_type in ("lead", "analyst", "compute", "skill", "echo", "judge", "composer"):
            router.bind(AgentBinding(agent_type=agent_type, primary=("p1", "mock")))
        # Same order as gateway startup: default tools, web_fetch, spawn/return, then manifests.
        tools = build_default_registry()
        assert register_web_fetch_tool(tools)
        register_subagent_tool(tools)
        register_subagent_return_tool(tools)
        # Wrap the registered web_fetch spec so the real dispatch hands the probe the tool
        # context the turn runner built; the probe calls the real handler with it.
        real_web_fetch = tools._specs["web_fetch"]  # noqa: SLF001
        self._web_fetch = real_web_fetch.handler
        tools.register(replace(real_web_fetch, handler=self._probe))
        orch = Orchestrator(router=router, datarc=None, tool_registry=tools, event_registry=event_registry, config=OrchestratorConfig())
        orch.session_store = self.sessions
        orch.artifact_store = self.store
        orch.multi_agent_registry = bootstrap_multi_agent(tools)  # type: ignore[attr-defined]
        orch.subagent_runner = _make_subagent_runner_with_catalog(  # type: ignore[attr-defined]
            router=router, tool_registry=tools, custom_store=None, product_terms_catalog=None
        )
        return orch

    async def run(self, request: TurnRequest) -> None:
        async def sink(_event: object) -> bool:
            return True

        orch = self.build(EventBusRegistry(default_log_sink=sink))
        # The origin recorded its session/turn rows in its own store; mirror them here.
        await self.sessions.ensure_session(session_id=request.session_id, workspace_id=request.workspace_id, user_id=request.user_id)
        await self.sessions.record_turn(turn_id=request.turn_id, session_id=request.session_id, user_text=request.text)
        if request.team_router is not None:
            # The origin resolved its team model through a stub; swap in the scripted model only.
            request = replace(request, team_router=orch.router)
        # The origin's dispatch lease and Space membership live in the origin's store, not in this
        # mirror, so the mirror runs the captured turn as an unleased worker (no execution owner).
        request = replace(request, execution_owner_id=None)
        await orch.event_registry.get_or_create(request.turn_id, persist_before_live=True)
        await orch.run_turn(request)

    async def assert_attributed(self, owner: str) -> None:
        assert len(self.writes) == 2 and set(self.sources.values()) == {"web_fetch", "image_generate"}
        for aid in self.writes:
            art = await self.store.get(aid)
            assert art is not None
            assert art.created_by == owner, f"{self.sources[aid]} artifact written without its accountable owner"
            assert await self.store.get(aid, check_created_by="u_stranger") is None


async def _run_captured(runtime: _AgentRuntime, request: TurnRequest, owner: str) -> None:
    """Run the origin's own TurnRequest, then a later turn by the same user, then a stranger."""
    await runtime.run(request)
    assert runtime.seen_users[request.turn_id] == owner
    await runtime.assert_attributed(owner)
    assert runtime.readback[request.turn_id] == runtime.writes  # same run

    # A later turn by the same user in a new conversation (web chat covers the same session).
    later = TurnRequest(session_id="s_later", workspace_id=request.workspace_id, user_id=owner, member_uid=request.member_uid, text="again")
    await runtime.run(later)
    assert runtime.readback[later.turn_id] == runtime.writes  # later turn, same user

    stranger = TurnRequest(session_id="s_stranger", workspace_id=request.workspace_id, user_id="u_stranger", member_uid="m_other", text="peek")
    await runtime.run(stranger)
    assert runtime.readback[stranger.turn_id] == [None] * len(runtime.writes)


# ─── origins ───


async def _wait_for_done(client: httpx2.AsyncClient, stream_url: str) -> list[str]:
    events: list[str] = []
    try:
        async with asyncio.timeout(20):
            async with client.stream("GET", stream_url) as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("event:"):
                        events.append(line[len("event:") :].strip())
                        if events[-1] == "done":
                            return events
    except TimeoutError:
        pass
    return events


@pytest.mark.integration
async def test_web_chat_attributes_artifacts_to_the_signed_in_user(tmp_path, faked_clients) -> None:
    from bowhead_cli.migrate import TARGET_FILES, upgrade
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
    from bowhead_gateway.main import create_app
    from bowhead_gateway.settings import Settings

    from bowhead_core import get_event_registry

    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}", target="core") == ("core",)
    runtime = _AgentRuntime(tmp_path)
    await runtime.start(core_path)
    turn_log_store = TurnLogStore(str(core_path))
    await turn_log_store.initialize()
    event_registry = get_event_registry()
    previous_sink = event_registry._default_log_sink  # noqa: SLF001
    event_registry.set_log_sink(OrderedTurnLogSink(turn_log_store))

    app = create_app()
    settings = Settings()
    orch = runtime.build(event_registry)
    orch.dispatch_owner_id = "artifact_owner_attribution_test"  # type: ignore[attr-defined]
    app.state.settings = settings
    app.state.orchestrator = orch
    app.state.session_store = runtime.sessions
    app.state.artifact_store = runtime.store
    app.state.turn_log_store = turn_log_store
    signed_in = settings.dev_default_user

    try:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as client:
            session_id = None
            for text in ("read the page and draw a poster", "show them again"):
                payload: dict[str, Any] = {"workspace_id": "ws_web", "member_uid": "m_web", "message": text}
                if session_id:
                    payload["session_id"] = session_id
                resp = await client.post("/api/v1/chat/send", json=payload)
                assert resp.status_code == 200, resp.text
                body = resp.json()
                session_id = body.get("session_id") or session_id
                assert "done" in await _wait_for_done(client, body["stream_url"])
    finally:
        event_registry.set_log_sink(previous_sink)

    turns = list(runtime.readback)
    assert len(turns) == 2, runtime.readback
    assert {runtime.seen_users[t] for t in turns} == {signed_in}
    await runtime.assert_attributed(signed_in)
    assert runtime.readback[turns[0]] == runtime.writes  # same run
    assert runtime.readback[turns[1]] == runtime.writes  # later turn, same user


async def test_scheduled_job_attributes_artifacts_to_the_job_owner(tmp_path, monkeypatch, faked_clients) -> None:
    from bowhead_core.user_store import UserStore
    from bowhead_gateway._scheduled_runner import run_scheduled_turn
    from test_scheduled_runner import _app, _FakeOrch, _job, _resolver, _TeamRouterRegistryStub

    async def team_router(app_state: Any, team_id: str) -> tuple[object | None, None]:
        return await _TeamRouterRegistryStub().get(team_id, {}, 0), None

    monkeypatch.setattr("bowhead_gateway.turn_service.resolve_team_byo_overrides", team_router)

    users = UserStore(str(tmp_path / "users.db"))
    await users.initialize()
    team = await users.create_team(name="A")
    owner = await users.create_user(team_id=team.id, email="owner@a.com", password="Passw0rd!x")
    captured = _FakeOrch()
    outcome = await run_scheduled_turn(_app(captured, _resolver(users)), _job(owner.id))
    assert outcome.error is None and captured.request is not None

    runtime = _AgentRuntime(tmp_path)
    await runtime.start()
    await _run_captured(runtime, captured.request, owner.id)


async def test_feishu_attributes_artifacts_to_the_bound_bowhead_user(tmp_path, monkeypatch, faked_clients) -> None:
    from test_feishu_adapter import _answer_cards, _build_bridge_fixture, _message_event, _wait_for, _wait_for_dispatch_settlement_and_close

    fixture = await _build_bridge_fixture(tmp_path, monkeypatch, workspace_count=1)
    result = await fixture.bridge.handle_message_event(_message_event(content={"text": "看 GMV"}))
    assert result["status"] == "started"
    await _wait_for(lambda: len(_answer_cards(fixture.fake_client)) == 1)
    request = fixture.orchestrator.requests[0]
    await _wait_for_dispatch_settlement_and_close(fixture.orchestrator, request.turn_id)

    runtime = _AgentRuntime(tmp_path / "agent")
    (tmp_path / "agent").mkdir()
    await runtime.start()
    await _run_captured(runtime, request, fixture.user.id)


async def test_run_without_an_accountable_user_stays_owner_less(tmp_path, faked_clients) -> None:
    """No origin starts a turn without a user today; if one did, its writes stay owner-less."""
    runtime = _AgentRuntime(tmp_path)
    await runtime.start()
    await runtime.run(TurnRequest(session_id="s_anon", workspace_id="ws_a", user_id="", member_uid="m", text="hi"))
    for aid in runtime.writes:
        art = await runtime.store.get(aid)
        assert art is not None and art.created_by is None
