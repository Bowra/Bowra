from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from bowhead_core.access_control import AccessContext
from bowhead_core.capability_resolver import CapabilityPrincipal, CapabilityRef, ResolvedCapabilities
from bowhead_core.home_proposal_cache_store import (
    HomeProposalCacheEntry,
    HomeProposalCacheStore,
)
from bowhead_core.home_proposals import (
    ContextRef,
    HomeProposal,
    build_envelope,
    generate_deterministic_proposals,
    platform_capability_refs,
    proposal_copy_fingerprint,
)
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_core.user_store import User
from bowhead_gateway.routes import home_proposals
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from bowhead_core import home_proposals as core_home_proposals
from bowhead_core import metrics
from bowhead_gateway import turn_service


@pytest.fixture
def user() -> User:
    return cast("User", SimpleNamespace(id="user_1", team_id="team_1"))


class _WorkspaceStore:
    async def list_workspaces_for_user(self, user_id: str) -> list[dict[str, str]]:
        assert user_id == "user_1"
        return [{"workspace_id": "ws_1", "name": "Operations"}]

    async def get_team_llm_config_with_version(self, team_id: str) -> tuple[dict[str, object], int]:
        assert team_id == "team_1"
        # Batch 2-a site 7: previously returned {}, making `_proposal_router` fall back to the **process-level
        # router**, exactly the shape this batch eliminates. Use real v2 team configuration (with home_proposal
        # binding), retaining the LLM path and every assertion below.
        return _TEAM_V2_CFG, 1

    async def list_team_skills(self, team_id: str, *, only_enabled: bool = False) -> list[object]:
        assert team_id == "team_1"
        assert only_enabled is True
        return []


_TEAM_V2_CFG: dict[str, object] = {
    "schema_version": 2,
    "providers": {"p1": {"type": "openai_compatible"}},
    "agents": {"home_proposal": {"primary": ["p1", "m"]}},
}


class _TeamRouter:
    """团队 router 替身——绑了 home_proposal。"""

    def resolve(self, agent_type: str, profile: object = None, user_pref: object = None) -> object:
        if agent_type != "home_proposal":
            raise KeyError(agent_type)
        return object()


class _TeamRouterRegistry:
    async def get(self, team_id: str, llm_cfg: object, version: int) -> object:
        return _TeamRouter()


class _HomeRouter:
    def resolve(self, agent_type: str) -> object:
        assert agent_type == "home_proposal"
        return object()


def _install_team_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _HomeRouter()

    async def _resolve(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(available=True, router=router, kb_retriever=None)

    monkeypatch.setattr(turn_service, "resolve_team_model_runtime", _resolve)


class _CountingCache:
    def __init__(self) -> None:
        self.get_calls = 0

    async def get(self, **_kwargs: str) -> None:
        self.get_calls += 1


def _app(user: User) -> FastAPI:
    app = FastAPI()
    app.include_router(home_proposals.router, prefix="/api/v1/home")
    app.dependency_overrides[_current_user] = lambda: user
    app.state.user_store = _WorkspaceStore()
    app.state.orchestrator = SimpleNamespace(
        multi_agent_registry=None,
        tool_registry=None,
        user_store=None,
        tool_auth_registry=None,
        access_control_store=None,
    )
    app.state.access_control_store = None
    app.state.skill_catalog = None
    app.state.skill_instance_store = None
    return app


@pytest.mark.asyncio
async def test_visible_skill_refs_include_runtime_visible_user_and_shared_team_skills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", "1")

    def spec(name: str, title: str) -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            title=title,
            description=f"Description for {name}",
            category="documents",
            version="1",
            kind="tool",
            audience="all",
        )

    builtin = spec("pptx", "Presentations")
    user_spec = spec("pptx-user", "User presentations")
    team_spec = spec("xlsx-team", "Shared workbooks")

    class Catalog:
        def list_specs(self) -> list[object]:
            return [builtin]

    class Instances:
        async def get(self, **_kwargs: str) -> None:
            return None

        async def list_for_user(self, *, user_id: str) -> list[object]:
            assert user_id == "user_1"
            return [
                SimpleNamespace(
                    name="pptx-user",
                    source_type="user_upload",
                    source_path="/user/pptx-user",
                    enabled=True,
                ),
                SimpleNamespace(
                    name="disabled-user",
                    source_type="user_upload",
                    source_path="/user/disabled-user",
                    enabled=False,
                ),
            ]

    class TeamStore:
        async def list_team_skills(self, team_id: str, *, only_enabled: bool) -> list[object]:
            assert (team_id, only_enabled) == ("team_1", True)
            return [
                SimpleNamespace(
                    id="oskill_1",
                    name="xlsx-team",
                    source_path="/team/xlsx-team",
                    visibility="creator_only",
                    created_by="user_2",
                ),
            ]

    monkeypatch.setattr(
        home_proposals,
        "load_skill",
        lambda path: user_spec if str(path) == "/user/pptx-user" else team_spec,
    )
    request = cast(
        "Request",
        SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    skill_catalog=Catalog(),
                    skill_instance_store=Instances(),
                ),
            ),
        ),
    )
    ctx = ToolExecutionContext(
        workspace_id="ws_1",
        member_uid="",
        user_id="user_1",
        turn_id="home_1",
        datarc=cast("object", None),
        team_id="team_1",
    )

    refs = await home_proposals._visible_skill_refs(
        request,
        ctx,
        user_id="user_1",
        team_id="team_1",
        user_store=cast("object", TeamStore()),
        shared_skill_ids=frozenset({"oskill_1"}),
    )

    assert {ref.id for ref in refs} == {"skill:pptx", "skill:pptx-user", "skill:xlsx-team"}


@pytest.mark.asyncio
async def test_visible_skill_refs_apply_instance_preferences_from_one_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", "0")

    def spec(name: str) -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            title=name,
            description=f"Description for {name}",
            category="documents",
            version="1",
            kind="tool",
            audience="all",
        )

    class Catalog:
        def list_specs(self) -> list[object]:
            return [spec("pptx"), spec("docx"), spec("xlsx")]

    reads: list[str] = []

    class Instances:
        async def get(self, **_kwargs: str) -> None:
            raise AssertionError("instances are read once per request, not per catalog skill")

        async def list_for_user(self, *, user_id: str) -> list[object]:
            reads.append(user_id)
            return [
                # A built-in the user switched off stays hidden.
                SimpleNamespace(name="docx", source_type="builtin", source_path="", enabled=False),
                # A user upload that collides with a built-in name does not hide the built-in.
                SimpleNamespace(name="xlsx", source_type="user_upload", source_path="/user/xlsx", enabled=False),
            ]

    request = cast(
        "Request",
        SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(skill_catalog=Catalog(), skill_instance_store=Instances())),
        ),
    )
    ctx = ToolExecutionContext(
        workspace_id="ws_1",
        member_uid="",
        user_id="user_1",
        turn_id="home_1",
        datarc=cast("object", None),
        team_id="team_1",
    )

    refs = await home_proposals._visible_skill_refs(
        request,
        ctx,
        user_id="user_1",
        team_id="team_1",
        user_store=cast("object", None),
        shared_skill_ids=None,
    )

    assert reads == ["user_1"]
    assert {ref.id for ref in refs} == {"skill:pptx", "skill:xlsx"}


@pytest.mark.asyncio
async def test_visible_subagent_refs_reuse_lead_peer_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_SUBAGENT_SPEC_ROUTING_ENABLED", "1")
    lead = SimpleNamespace(callable_peers=("compute",))
    compute = SimpleNamespace(name="compute", description="System compute")

    class Registry:
        def get(self, name: str, _workspace_id: str) -> object:
            if name == "lead":
                return lead
            if name == "compute":
                return compute
            raise KeyError(name)

    class CustomStore:
        async def list_for_user(self, user_id: str, status: str = "active") -> list[object]:
            assert (user_id, status) == ("user_1", "active")
            return [SimpleNamespace(name="personal-writer", description="Personal writer")]

    class TeamStore:
        async def list_team_subagents(self, team_id: str, *, only_enabled: bool) -> list[object]:
            assert (team_id, only_enabled) == ("team_1", True)
            return [
                SimpleNamespace(
                    id="osa_1",
                    name="shared-reviewer",
                    description="Shared reviewer",
                    visibility="creator_only",
                    created_by="user_2",
                ),
            ]

    orchestrator = SimpleNamespace(
        multi_agent_registry=Registry(),
        custom_subagent_store=CustomStore(),
        user_store=TeamStore(),
    )

    refs = await home_proposals._visible_subagent_refs(
        orchestrator,
        "ws_1",
        user_id="user_1",
        team_id="team_1",
        shared_subagent_ids=frozenset({"osa_1"}),
    )

    assert {ref.id for ref in refs} == {
        "subagent:compute",
        "subagent:personal-writer",
        "subagent:shared-reviewer",
    }


def test_membership_is_checked_before_cache_read(
    monkeypatch: pytest.MonkeyPatch,
    user: User,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSALS", "1")
    cache = _CountingCache()
    app = _app(user)
    app.state.home_proposal_cache_store = cache

    response = TestClient(app).get(
        "/api/v1/home/proposals",
        params={"workspace_id": "ws_victim", "locale": "zh-CN"},
    )

    assert response.status_code == 404
    assert cache.get_calls == 0


def test_deterministic_response_is_cached_per_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user: User,
) -> None:
    monkeypatch.delenv("BOWHEAD_HOME_PROPOSALS", raising=False)

    async def access_context(*_args: object, **_kwargs: object) -> AccessContext:
        return AccessContext(
            user_id="user_1",
            team_id="team_1",
            workspace_id="ws_1",
            workspace_role="member",
        )

    monkeypatch.setattr(
        home_proposals,
        "build_access_context_from_request",
        access_context,
    )
    cache = HomeProposalCacheStore(tmp_path / "proposal.db")
    app = _app(user)
    app.state.home_proposal_cache_store = cache

    async def initialize_and_request() -> tuple[dict[str, object], dict[str, object]]:
        await cache.initialize()
        with TestClient(app) as client:
            first = client.get(
                "/api/v1/home/proposals",
                params={"workspace_id": "ws_1", "locale": "en-US"},
            )
            second = client.get(
                "/api/v1/home/proposals",
                params={"workspace_id": "ws_1", "locale": "en-US"},
            )
        assert first.status_code == 200
        assert second.status_code == 200
        return first.json(), second.json()

    first, second = asyncio.run(initialize_and_request())

    assert first["version"] == "home.proposals.v1"
    assert first["strategy"] == "deterministic"
    assert first["cached"] is False
    assert second["cached"] is True
    assert first["generationId"] == second["generationId"]
    first_items = cast("list[dict[str, object]]", first["items"])
    assert len(first_items) == 1
    assert first_items[0]["capabilityRefs"] == ["platform:lead"]
    assert "outputFormat" not in first_items[0]


def test_cache_rows_are_keyed_by_each_supported_locale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user: User,
) -> None:
    monkeypatch.delenv("BOWHEAD_HOME_PROPOSALS", raising=False)

    async def access_context(*_args: object, **_kwargs: object) -> AccessContext:
        return AccessContext(user_id="user_1", team_id="team_1", workspace_id="ws_1", workspace_role="member")

    monkeypatch.setattr(home_proposals, "build_access_context_from_request", access_context)
    lookups: list[str] = []

    class RecordingCache(HomeProposalCacheStore):
        async def get(self, *, user_id: str, workspace_id: str, locale: str) -> HomeProposalCacheEntry | None:
            lookups.append(locale)
            return await super().get(user_id=user_id, workspace_id=workspace_id, locale=locale)

    cache = RecordingCache(tmp_path / "proposal.db")
    app = _app(user)
    app.state.home_proposal_cache_store = cache
    locales = ("en-US", "de-DE", "fr-FR", "zh-CN")

    async def scenario() -> tuple[dict[str, dict[str, object]], dict[str, object]]:
        await cache.initialize()
        first: dict[str, dict[str, object]] = {}
        with TestClient(app) as client:
            for locale in locales:
                lookups.clear()
                first[locale] = client.get("/api/v1/home/proposals", params={"workspace_id": "ws_1", "locale": locale}).json()
                # Both the optimistic read and the read under the generation lock use the request locale.
                assert lookups == [locale, locale]
            rows = {locale: await HomeProposalCacheStore.get(cache, user_id="user_1", workspace_id="ws_1", locale=locale) for locale in locales}
        return first, rows

    first, rows = asyncio.run(scenario())

    # An English row cached before the German request must not be served as the German answer.
    assert all(response["cached"] is False for response in first.values())
    assert len({response["generationId"] for response in first.values()}) == len(locales)
    for locale in locales:
        row = rows[locale]
        assert row is not None
        assert row.locale == locale
        assert row.payload["copyFingerprint"] == proposal_copy_fingerprint(locale)
    assert first["zh-CN"]["items"][0]["title"] != first["en-US"]["items"][0]["title"]  # type: ignore[index]


def test_copy_table_edit_invalidates_only_that_locales_cached_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user: User,
) -> None:
    monkeypatch.delenv("BOWHEAD_HOME_PROPOSALS", raising=False)

    async def access_context(*_args: object, **_kwargs: object) -> AccessContext:
        return AccessContext(user_id="user_1", team_id="team_1", workspace_id="ws_1", workspace_role="member")

    monkeypatch.setattr(home_proposals, "build_access_context_from_request", access_context)
    cache = HomeProposalCacheStore(tmp_path / "proposal.db")
    app = _app(user)
    app.state.home_proposal_cache_store = cache

    def fetch(client: TestClient, locale: str) -> dict[str, object]:
        return client.get("/api/v1/home/proposals", params={"workspace_id": "ws_1", "locale": locale}).json()

    async def scenario() -> list[dict[str, object]]:
        await cache.initialize()
        with TestClient(app) as client:
            responses = [fetch(client, "de-DE"), fetch(client, "en-US")]
            monkeypatch.setitem(core_home_proposals._PROPOSAL_COPY["de-DE"], "lead_title", "Ziel in einen Arbeitsplan zerlegen")
            responses += [fetch(client, "de-DE"), fetch(client, "en-US")]
        return responses

    de_before, en_before, de_after, en_after = asyncio.run(scenario())

    assert de_after["cached"] is False
    assert de_after["items"][0]["title"] == "Ziel in einen Arbeitsplan zerlegen"  # type: ignore[index]
    # 295035392 gave de-DE its own translated copy, so the two locales no longer share a title;
    # the edit must change only the de-DE row while en-US keeps its cached title.
    assert de_before["items"][0]["title"] != de_after["items"][0]["title"]  # type: ignore[index]
    assert en_after["items"][0]["title"] == en_before["items"][0]["title"]  # type: ignore[index]
    assert en_after["cached"] is True
    assert en_after["generationId"] == en_before["generationId"]


def test_deterministic_row_without_copy_fingerprint_is_regenerated() -> None:
    envelope = build_envelope(
        workspace_id="ws_1",
        workspace_name="Operations",
        capabilities=platform_capability_refs(()),
        locale="de-DE",
    )
    now = datetime.now(tz=UTC)
    proposals = generate_deterministic_proposals(envelope)

    def entry(payload: dict[str, object]) -> HomeProposalCacheEntry:
        return HomeProposalCacheEntry(
            user_id="user_1",
            workspace_id="ws_1",
            locale="de-DE",
            generation_id="hp_1",
            generated_at=now.isoformat(),
            expires_at=(now + timedelta(hours=1)).isoformat(),
            capability_fingerprint=envelope.capability_fingerprint,
            context_fingerprint=envelope.context_fingerprint,
            strategy="deterministic",
            payload={"items": [item.to_cache_payload() for item in proposals], **payload},
        )

    assert home_proposals._proposals_from_cache(entry({}), envelope=envelope, now=now) is None
    assert home_proposals._proposals_from_cache(entry({"copyFingerprint": "stale"}), envelope=envelope, now=now) is None
    assert (
        home_proposals._proposals_from_cache(
            entry({"copyFingerprint": proposal_copy_fingerprint("de-DE")}),
            envelope=envelope,
            now=now,
        )
        == proposals
    )


def test_viewer_scope_omits_routine_for_unannotated_scheduled_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user: User,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSALS", "1")

    async def access_context(*_args: object, **_kwargs: object) -> AccessContext:
        return AccessContext(
            user_id="user_1",
            team_id="team_1",
            workspace_id="ws_1",
            workspace_role="viewer",
        )

    async def resolve(
        _resolver: object,
        _stack: object,
        principal: CapabilityPrincipal,
        _execution_context: object,
        **_kwargs: object,
    ) -> ResolvedCapabilities:
        assert principal.read_only is True
        scheduled_job_ref = CapabilityRef(
            id="tool:scheduled_job_create",
            kind="tool",
            label="Scheduled job",
            description="Create a recurring Bowhead job after confirmation.",
            operations=frozenset({"read", "execute"}),
            output_formats=(),
            version="v1",
        )
        return ResolvedCapabilities(
            tool_view=None,
            refs=(scheduled_job_ref,),
            fingerprint="viewer",
        )

    monkeypatch.setattr(home_proposals, "build_access_context_from_request", access_context)
    monkeypatch.setattr(home_proposals.CapabilityResolver, "resolve", resolve)
    cache = HomeProposalCacheStore(tmp_path / "proposal.db")
    asyncio.run(cache.initialize())
    app = _app(user)
    app.state.home_proposal_cache_store = cache

    response = TestClient(app).get(
        "/api/v1/home/proposals",
        params={"workspace_id": "ws_1", "locale": "en-US"},
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["kind"] for item in items] == ["analyze"]
    assert items[0]["capabilityRefs"] == ["platform:lead"]


@pytest.mark.integration
def test_search_connection_change_invalidates_home_capability_cache(monkeypatch, tmp_path, user) -> None:
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.tool_registry import ToolRegistry, ToolSpec
    from bowhead_core.types import ToolAnnotations
    from bowhead_router.types import ToolSchema

    monkeypatch.setenv("BOWHEAD_HOME_PROPOSALS", "1")
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSAL_LLM", "0")

    async def access_context(*_args, **_kwargs):
        return AccessContext(user_id="user_1", team_id="team_1", workspace_id="ws_1", workspace_role="member")

    configured = False

    async def search_connection(_team_id):
        return SimpleNamespace(get_active=lambda _capability: SimpleNamespace(api_key="fixture-key") if configured else None)

    async def handler(_args, _ctx):
        raise AssertionError("Discovery must not execute the tool")

    monkeypatch.setattr(home_proposals, "build_access_context_from_request", access_context)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(name="web_search", description="Search public information", input_schema={"type": "object", "properties": {}}),
            handler=handler,
            allowed_for_roles=frozenset({"lead"}),
            annotations=ToolAnnotations(read_only=True),
        )
    )
    app = _app(user)
    app.state.orchestrator.tool_registry = registry
    app.state.orchestrator.multi_agent_registry = AgentRegistry.bootstrap()
    app.state.orchestrator.team_search_resolver = search_connection
    cache = HomeProposalCacheStore(tmp_path / "proposal.db")
    asyncio.run(cache.initialize())
    app.state.home_proposal_cache_store = cache
    with TestClient(app) as client:
        first = client.get("/api/v1/home/proposals", params={"workspace_id": "ws_1", "locale": "en-US"})
        assert first.status_code == 200, first.text
        before = asyncio.run(cache.get(user_id="user_1", workspace_id="ws_1", locale="en-US"))
        configured = True
        second = client.get("/api/v1/home/proposals", params={"workspace_id": "ws_1", "locale": "en-US"})
        assert second.status_code == 200, second.text
        after = asyncio.run(cache.get(user_id="user_1", workspace_id="ws_1", locale="en-US"))
    assert before is not None and after is not None
    assert before.capability_fingerprint != after.capability_fingerprint
    assert second.json()["cached"] is False


def test_cache_is_invalidated_when_capability_fingerprint_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user: User,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSALS", "1")

    async def access_context(*_args: object, **_kwargs: object) -> AccessContext:
        return AccessContext(
            user_id="user_1",
            team_id="team_1",
            workspace_id="ws_1",
            workspace_role="member",
        )

    calls = 0

    async def resolve(*_args: object, **_kwargs: object) -> ResolvedCapabilities:
        nonlocal calls
        calls += 1
        capability_version = 1 if calls <= 2 else 2
        ref = CapabilityRef(
            id="tool:workspace.read",
            kind="tool",
            label="Workspace reader",
            description=f"Capability version {capability_version}",
            operations=frozenset({"read", "execute"}),
            output_formats=(),
            version=f"v{capability_version}",
        )
        return ResolvedCapabilities(
            tool_view=None,
            refs=(ref,),
            fingerprint=f"resolver-{capability_version}",
        )

    monkeypatch.setattr(home_proposals, "build_access_context_from_request", access_context)
    monkeypatch.setattr(home_proposals.CapabilityResolver, "resolve", resolve)
    cache = HomeProposalCacheStore(tmp_path / "proposal.db")
    asyncio.run(cache.initialize())
    app = _app(user)
    app.state.home_proposal_cache_store = cache

    with TestClient(app) as client:
        first = client.get(
            "/api/v1/home/proposals",
            params={"workspace_id": "ws_1", "locale": "zh-CN"},
        )
        second = client.get(
            "/api/v1/home/proposals",
            params={"workspace_id": "ws_1", "locale": "zh-CN"},
        )
        third = client.get(
            "/api/v1/home/proposals",
            params={"workspace_id": "ws_1", "locale": "zh-CN"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200
    assert first.json()["items"]
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert second.json()["generationId"] == first.json()["generationId"]
    assert third.json()["cached"] is False
    assert third.json()["generationId"] != first.json()["generationId"]
    assert calls == 3


def test_valid_empty_response_is_cached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user: User,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSALS", "1")
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSAL_LLM", "1")

    async def access_context(*_args: object, **_kwargs: object) -> AccessContext:
        return AccessContext(
            user_id="user_1",
            team_id="team_1",
            workspace_id="ws_1",
            workspace_role="member",
        )

    llm_calls = 0

    async def failed_llm(*_args: object, **_kwargs: object) -> tuple[HomeProposal, ...]:
        nonlocal llm_calls
        llm_calls += 1
        raise TimeoutError

    monkeypatch.setattr(home_proposals, "build_access_context_from_request", access_context)
    monkeypatch.setattr(home_proposals, "generate_llm_proposals", failed_llm)
    monkeypatch.setattr(home_proposals, "generate_deterministic_proposals", lambda *_args, **_kwargs: ())
    cache = HomeProposalCacheStore(tmp_path / "proposal.db")
    asyncio.run(cache.initialize())
    app = _app(user)
    app.state.home_proposal_cache_store = cache
    app.state.team_router_registry = _TeamRouterRegistry()
    app.state.router = object()
    _install_team_runtime(monkeypatch)

    with TestClient(app) as client:
        first = client.get(
            "/api/v1/home/proposals",
            params={"workspace_id": "ws_1", "locale": "en-US"},
        )
        second = client.get(
            "/api/v1/home/proposals",
            params={"workspace_id": "ws_1", "locale": "en-US"},
        )

    assert first.status_code == 200
    assert first.json()["items"] == []
    assert first.json()["cached"] is False
    assert second.status_code == 200
    assert second.json()["items"] == []
    assert second.json()["cached"] is True
    assert second.json()["generationId"] == first.json()["generationId"]
    assert llm_calls == 1


def test_context_fingerprint_uses_six_hour_cache_grace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user: User,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSALS", "1")

    async def access_context(*_args: object, **_kwargs: object) -> AccessContext:
        return AccessContext(
            user_id="user_1",
            team_id="team_1",
            workspace_id="ws_1",
            workspace_role="member",
        )

    context_calls = 0

    async def context_refs(*_args: object, **_kwargs: object) -> tuple[ContextRef, ...]:
        nonlocal context_calls
        context_calls += 1
        title = "Original context" if context_calls == 1 else "Updated context"
        return (
            ContextRef(
                id="session:s_1",
                kind="session",
                title=title,
                format=None,
                updated_at="2026-07-16T00:00:00+00:00",
            ),
        )

    def deterministic_proposals(*_args: object, **_kwargs: object) -> tuple[HomeProposal, ...]:
        return (
            HomeProposal(
                id="hpi_generic",
                kind="analyze",
                title="Clarify a work goal",
                reason="Bowhead can clarify the expected result.",
                prompt="Clarify my expected result, constraints, and required inputs.",
                capability_refs=("platform:lead",),
            ),
        )

    monkeypatch.setattr(home_proposals, "build_access_context_from_request", access_context)
    monkeypatch.setattr(home_proposals, "_context_refs", context_refs)
    monkeypatch.setattr(home_proposals, "generate_deterministic_proposals", deterministic_proposals)
    cache = HomeProposalCacheStore(tmp_path / "proposal.db")
    asyncio.run(cache.initialize())
    app = _app(user)
    app.state.home_proposal_cache_store = cache

    with TestClient(app) as client:
        first = client.get(
            "/api/v1/home/proposals",
            params={"workspace_id": "ws_1", "locale": "en-US"},
        )
        second = client.get(
            "/api/v1/home/proposals",
            params={"workspace_id": "ws_1", "locale": "en-US"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["cached"] is True
    assert first.json()["generationId"] == second.json()["generationId"]

    entry = asyncio.run(cache.get(user_id="user_1", workspace_id="ws_1", locale="en-US"))
    assert entry is not None
    now = datetime.now(tz=UTC)
    asyncio.run(
        cache.upsert(
            replace(
                entry,
                generated_at=(now - timedelta(hours=7)).isoformat(),
                expires_at=(now + timedelta(hours=1)).isoformat(),
            )
        )
    )

    third = TestClient(app).get(
        "/api/v1/home/proposals",
        params={"workspace_id": "ws_1", "locale": "en-US"},
    )

    assert third.status_code == 200
    assert third.json()["cached"] is False
    assert third.json()["generationId"] != second.json()["generationId"]
    assert context_calls == 3


def test_llm_cache_revalidation_rejects_a_partially_stale_group() -> None:
    envelope = build_envelope(
        workspace_id="ws_1",
        workspace_name="Operations",
        capabilities=(
            CapabilityRef(
                id="platform:lead",
                kind="platform",
                label="Bowhead",
                description="Plan available work.",
                operations=frozenset({"read", "execute"}),
                output_formats=(),
                version="v1",
            ),
        ),
        contexts=(
            ContextRef(
                id="session:s_current",
                kind="session",
                title="Current session",
                format=None,
                updated_at="2026-07-17T00:00:00+00:00",
            ),
        ),
        locale="en-US",
    )
    now = datetime.now(tz=UTC)
    proposals = (
        HomeProposal(
            id="hpi_stale",
            kind="analyze",
            title="Continue the previous review",
            reason="Bowhead can organize the selected material.",
            prompt="Review the selected material and identify open decisions.",
            capability_refs=("platform:lead",),
            context_refs=("session:s_removed",),
        ),
        HomeProposal(
            id="hpi_valid",
            kind="analyze",
            title="Clarify a work goal",
            reason="Bowhead can clarify the expected result.",
            prompt="Clarify my expected result, constraints, and required inputs.",
            capability_refs=("platform:lead",),
        ),
    )
    entry = HomeProposalCacheEntry(
        user_id="user_1",
        workspace_id="ws_1",
        locale="en-US",
        generation_id="hp_1",
        generated_at=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
        capability_fingerprint=envelope.capability_fingerprint,
        context_fingerprint="stale-context-fingerprint",
        strategy="llm",
        payload={"items": [item.to_cache_payload() for item in proposals]},
    )

    assert home_proposals._proposals_from_cache(entry, envelope=envelope, now=now) is None


def test_llm_response_is_cached_after_validation_layer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user: User,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSALS", "1")
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSAL_LLM", "1")

    async def access_context(*_args: object, **_kwargs: object) -> AccessContext:
        return AccessContext(
            user_id="user_1",
            team_id="team_1",
            workspace_id="ws_1",
            workspace_role="member",
        )

    calls = 0
    attempt_outcomes: list[str] = []

    async def llm_proposals(*_args: object, **_kwargs: object) -> tuple[HomeProposal, ...]:
        nonlocal calls
        calls += 1
        return (
            HomeProposal(
                id="hpi_1",
                kind="analyze",
                title="Clarify a work goal",
                reason="Bowhead can clarify the expected result.",
                prompt="Clarify my expected result, constraints, and required inputs.",
                capability_refs=("platform:lead",),
            ),
            HomeProposal(
                id="hpi_2",
                kind="analyze",
                title="Build an execution plan",
                reason="Bowhead can break a goal into executable steps.",
                prompt="Turn my confirmed goal into decisions, inputs, and execution steps.",
                capability_refs=("platform:lead",),
            ),
        )

    monkeypatch.setattr(
        home_proposals,
        "build_access_context_from_request",
        access_context,
    )
    monkeypatch.setattr(home_proposals, "generate_llm_proposals", llm_proposals)
    monkeypatch.setattr(home_proposals, "_record_proposal_llm_attempt", attempt_outcomes.append)
    cache = HomeProposalCacheStore(tmp_path / "proposal.db")
    asyncio.run(cache.initialize())
    app = _app(user)
    app.state.home_proposal_cache_store = cache
    app.state.team_router_registry = _TeamRouterRegistry()
    app.state.router = object()
    _install_team_runtime(monkeypatch)

    with TestClient(app) as client:
        first = client.get(
            "/api/v1/home/proposals",
            params={"workspace_id": "ws_1", "locale": "en-US"},
        )
        second = client.get(
            "/api/v1/home/proposals",
            params={"workspace_id": "ws_1", "locale": "en-US"},
        )

    assert first.status_code == 200
    assert first.json()["strategy"] == "llm"
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert calls == 1
    assert attempt_outcomes == ["success"]


def test_disabling_llm_replaces_cached_llm_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user: User,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSALS", "1")
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSAL_LLM", "1")

    async def access_context(*_args: object, **_kwargs: object) -> AccessContext:
        return AccessContext(
            user_id="user_1",
            team_id="team_1",
            workspace_id="ws_1",
            workspace_role="member",
        )

    async def llm_proposals(*_args: object, **_kwargs: object) -> tuple[HomeProposal, ...]:
        return (
            HomeProposal(
                id="hpi_1",
                kind="analyze",
                title="Clarify a work goal",
                reason="Bowhead can clarify the expected result.",
                prompt="Clarify my expected result, constraints, and required inputs.",
                capability_refs=("platform:lead",),
            ),
            HomeProposal(
                id="hpi_2",
                kind="analyze",
                title="Build an execution plan",
                reason="Bowhead can break a goal into executable steps.",
                prompt="Turn my confirmed goal into decisions, inputs, and execution steps.",
                capability_refs=("platform:lead",),
            ),
        )

    monkeypatch.setattr(home_proposals, "build_access_context_from_request", access_context)
    monkeypatch.setattr(home_proposals, "generate_llm_proposals", llm_proposals)
    cache = HomeProposalCacheStore(tmp_path / "proposal.db")
    asyncio.run(cache.initialize())
    app = _app(user)
    app.state.home_proposal_cache_store = cache
    app.state.team_router_registry = _TeamRouterRegistry()
    app.state.router = object()
    _install_team_runtime(monkeypatch)

    with TestClient(app) as client:
        first = client.get(
            "/api/v1/home/proposals",
            params={"workspace_id": "ws_1", "locale": "en-US"},
        )
        monkeypatch.setenv("BOWHEAD_HOME_PROPOSAL_LLM", "0")
        second = client.get(
            "/api/v1/home/proposals",
            params={"workspace_id": "ws_1", "locale": "en-US"},
        )

    assert first.json()["strategy"] == "llm"
    assert second.json()["strategy"] == "deterministic"
    assert second.json()["cached"] is False
    assert first.json()["generationId"] != second.json()["generationId"]


def test_team_llm_without_home_binding_does_not_use_platform_router(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user: User,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSALS", "1")
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSAL_LLM", "1")

    async def access_context(*_args: object, **_kwargs: object) -> AccessContext:
        return AccessContext(
            user_id="user_1",
            team_id="team_1",
            workspace_id="ws_1",
            workspace_role="member",
        )

    class _TeamStore(_WorkspaceStore):
        async def get_team_llm_config_with_version(self, team_id: str) -> tuple[dict[str, object], int]:
            assert team_id == "team_1"
            return {
                "schema_version": 2,
                "providers": {"team": {}},
                "agents": {"lead": {}},
            }, 3

    async def unexpected_llm(*_args: object, **_kwargs: object) -> tuple[HomeProposal, ...]:
        raise AssertionError("platform router must not receive team proposal data")

    monkeypatch.setattr(home_proposals, "build_access_context_from_request", access_context)
    monkeypatch.setattr(home_proposals, "generate_llm_proposals", unexpected_llm)
    cache = HomeProposalCacheStore(tmp_path / "proposal.db")
    asyncio.run(cache.initialize())
    app = _app(user)
    app.state.user_store = _TeamStore()
    app.state.home_proposal_cache_store = cache
    app.state.team_router_registry = _TeamRouterRegistry()

    response = TestClient(app).get(
        "/api/v1/home/proposals",
        params={"workspace_id": "ws_1", "locale": "en-US"},
    )

    assert response.status_code == 200
    assert response.json()["strategy"] == "deterministic"


def test_llm_failure_returns_and_temporarily_caches_deterministic_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user: User,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSALS", "1")
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSAL_LLM", "1")

    async def access_context(*_args: object, **_kwargs: object) -> AccessContext:
        return AccessContext(
            user_id="user_1",
            team_id="team_1",
            workspace_id="ws_1",
            workspace_role="member",
        )

    calls = 0
    fallback_reasons: list[str] = []
    attempt_outcomes: list[str] = []

    async def failed_llm(*_args: object, **_kwargs: object) -> tuple[HomeProposal, ...]:
        nonlocal calls
        calls += 1
        raise TimeoutError

    monkeypatch.setattr(
        home_proposals,
        "build_access_context_from_request",
        access_context,
    )
    monkeypatch.setattr(home_proposals, "generate_llm_proposals", failed_llm)
    monkeypatch.setattr(
        home_proposals,
        "_record_proposal_fallback",
        fallback_reasons.append,
    )
    monkeypatch.setattr(home_proposals, "_record_proposal_llm_attempt", attempt_outcomes.append)
    cache = HomeProposalCacheStore(tmp_path / "proposal.db")
    asyncio.run(cache.initialize())
    app = _app(user)
    app.state.home_proposal_cache_store = cache
    app.state.team_router_registry = _TeamRouterRegistry()
    app.state.router = object()
    _install_team_runtime(monkeypatch)

    with TestClient(app) as client:
        first = client.get(
            "/api/v1/home/proposals",
            params={"workspace_id": "ws_1", "locale": "zh-CN"},
        )
        second = client.get(
            "/api/v1/home/proposals",
            params={"workspace_id": "ws_1", "locale": "zh-CN"},
        )

    assert first.status_code == 200
    assert first.json()["strategy"] == "deterministic"
    assert len(first.json()["items"]) == 1
    assert second.json()["cached"] is True
    assert calls == 1
    assert fallback_reasons == ["timeout"]
    assert attempt_outcomes == ["fallback"]


def test_proposal_reason_metrics_use_fixed_reason_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[str, dict[str, str] | None]] = []

    def record_metric(
        name: str,
        labels: dict[str, str] | None = None,
        **_kwargs: object,
    ) -> None:
        recorded.append((name, labels))

    monkeypatch.setattr(metrics, "inc", record_metric)

    home_proposals._record_proposal_rejection("unsupported_specific")
    home_proposals._record_proposal_candidate("rejected")
    home_proposals._record_proposal_fallback("invalid_json")
    home_proposals._record_proposal_llm_attempt("fallback")

    assert recorded == [
        (
            "bowhead_home_proposal_rejected_total",
            {"reason": "unsupported_specific"},
        ),
        (
            "bowhead_home_proposal_candidates_total",
            {"outcome": "rejected"},
        ),
        (
            "bowhead_home_proposal_fallback_total",
            {"reason": "invalid_json"},
        ),
        (
            "bowhead_home_proposal_llm_attempts_total",
            {"outcome": "fallback"},
        ),
    ]


def test_capability_projection_failure_falls_back_to_lead(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user: User,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSALS", "1")

    async def access_context(*_args: object, **_kwargs: object) -> AccessContext:
        return AccessContext(
            user_id="user_1",
            team_id="team_1",
            workspace_id="ws_1",
            workspace_role="member",
        )

    async def failed_resolve(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("resolver unavailable")

    monkeypatch.setattr(
        home_proposals,
        "build_access_context_from_request",
        access_context,
    )
    monkeypatch.setattr(home_proposals.CapabilityResolver, "resolve", failed_resolve)
    cache = HomeProposalCacheStore(tmp_path / "proposal.db")
    asyncio.run(cache.initialize())
    app = _app(user)
    app.state.home_proposal_cache_store = cache

    response = TestClient(app).get(
        "/api/v1/home/proposals",
        params={"workspace_id": "ws_1", "locale": "zh-CN"},
    )

    assert response.status_code == 200
    assert response.json()["strategy"] == "deterministic"
    assert response.json()["items"][0]["capabilityRefs"] == ["platform:lead"]


def test_proposal_flag_explicit_off_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    user: User,
) -> None:
    monkeypatch.setenv("BOWHEAD_HOME_PROPOSALS", "0")
    app = _app(user)
    app.state.home_proposal_cache_store = _CountingCache()

    response = TestClient(app).get(
        "/api/v1/home/proposals",
        params={"workspace_id": "ws_1"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "home proposals disabled"}
