from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import httpx2
import jwt
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.subagent_authoring_catalog import AuthoringTool
from bowhead_core.system_plugins import builtin_plugin
from bowhead_core.team_plugin_store import TeamPluginStore
from bowhead_core.user_store import UserStore, _id, _now
from bowhead_gateway.main import create_app
from bowhead_gateway.routes import capability_catalog
from bowhead_gateway.settings import Settings
from cryptography.fernet import Fernet


@pytest.fixture
async def catalog_context(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test_navigation_catalog_secret_32")
    monkeypatch.setenv("FEATURE_MULTI_TEAM_AUTHZ", "1")
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "0")
    monkeypatch.delenv("BOWHEAD_PROD", raising=False)
    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    store = UserStore(str(path))
    await store.initialize()
    now = _now()
    async with aiosqlite.connect(store.db_path) as db:
        for team_id, user_id in (("team_a", "u_member"), ("team_b", "u_other")):
            await db.execute("INSERT INTO teams(id,name,status,created_at) VALUES (?,?,'active',?)", (team_id, team_id, now))
            await db.execute(
                "INSERT INTO users(id,team_id,email,email_verified,password_hash,status,created_at) VALUES (?,?,?,1,'','active',?)",
                (user_id, team_id, f"{user_id}@example.com", now),
            )
            await db.execute(
                "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,'member','active',?)",
                (_id("om"), user_id, team_id, now),
            )
        await db.commit()
    app = create_app()
    app.state.settings = Settings()
    app.state.user_store = store
    token = jwt.encode(
        {"sub": "u_member", "team_id": "team_a", "iat": int(time.time()), "exp": int(time.time()) + 3600},
        "test_navigation_catalog_secret_32",
        algorithm="HS256",
    )
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw", cookies={"bowhead_jwt": token}) as client:
        yield client, store


@pytest.mark.integration
async def test_member_catalog_is_scoped_metadata_without_source_configuration(catalog_context, monkeypatch):
    client, store = catalog_context
    tool = AuthoringTool(
        stable_ref="teammcp.source_1.read",
        display_name="Read inventory",
        description="Read team inventory",
        source_kind="team_mcp",
        source_display_name="Inventory",
        capability_classes=frozenset({"read"}),
        risk="read",
        discoverable=True,
        selectable=True,
        locked_reason=None,
        risk_source="mcp_annotation",
        approval_required=False,
        confirmation_summary="Private confirmation",
        dependency_revision="secret-revision",
        source_id="private-source-id",
        method="GET",
        path="/private-api",
    )
    build = AsyncMock(return_value=SimpleNamespace(native_tools=(), team_tools=(tool, replace(tool, stable_ref="hidden", discoverable=False))))
    monkeypatch.setattr(capability_catalog.SubagentAuthoringCatalogService, "build", build)
    plugins = TeamPluginStore(store.db_path)
    created = []
    for team, name in (("team_a", "Active bundle"), ("team_a", "Disabled bundle"), ("team_b", "Other team bundle")):
        created.append(
            await plugins.create(
                team_id=team, actor_user_id="u_member", name=name, description="Reusable reads", tool_refs=["web_search"], skill_grants=[]
            )
        )
    await plugins.set_enabled(team_id="team_a", plugin_id=created[1]["id"], enabled=False, expected_row_version=created[1]["row_version"])
    response = await client.get("/api/v1/capability-catalog", params={"team_id": "team_b"})
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    data = response.json()
    assert [item["display_name"] for item in data["tools"]] == ["Read inventory"]
    assert data["capabilities"] == [
        {"id": created[0]["id"], "name": "Active bundle", "description": "Reusable reads", "tool_count": 1, "skill_count": 0}
    ]
    assert build.await_args.kwargs["team_id"] == "team_a"
    assert build.await_args.kwargs["actor_user_id"] == "u_member"
    for private in ("private-source-id", "secret-revision", "Private confirmation", "/private-api", "tool_refs", "skill_grants"):
        assert private not in response.text
    assert (await client.post("/api/v1/capability-catalog", json={})).status_code == 405


@pytest.mark.integration
async def test_catalog_rechecks_active_membership_before_reading_metadata(catalog_context, monkeypatch):
    client, store = catalog_context
    build = AsyncMock()
    monkeypatch.setattr(capability_catalog.SubagentAuthoringCatalogService, "build", build)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE team_members SET status='removed' WHERE user_id='u_member'")
        await db.commit()
    response = await client.get("/api/v1/capability-catalog")
    assert response.status_code == 403, response.text
    build.assert_not_awaited()


@pytest.mark.integration
async def test_system_plugins_follow_enabled_agent_and_current_manifest_revisions(catalog_context, monkeypatch):
    client, _ = catalog_context
    monkeypatch.setattr(
        capability_catalog.SubagentAuthoringCatalogService, "build", AsyncMock(return_value=SimpleNamespace(native_tools=(), team_tools=()))
    )
    enabled = await client.get("/api/v1/capability-catalog")
    assert enabled.status_code == 200
    plugins = enabled.json()["builtin_plugins"]
    manifest = AgentRegistry.bootstrap().get("configuration-agent")
    assert [item["id"] for item in plugins] == manifest.plugins
    assert len({item["name"] for item in plugins}) == len(plugins)
    for item in plugins:
        assert set(item) == {"id", "name", "tool_count", "skill_count"}
        assert item["tool_count"] == len(builtin_plugin(item["id"]).resources) == 7
        assert item["skill_count"] == 1


@pytest.mark.integration
async def test_third_party_operations_follow_connection_and_deployment_enablement(catalog_context, monkeypatch):
    client, store = catalog_context
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FEATURE_GOOGLE_WORKSPACE", "1")
    for provider in ("google_workspace", "powerbi"):
        await store.create_team_builtin_tool_instance(
            team_id="team_a",
            provider=provider,
            display_name=provider,
            endpoint="",
            auth={"test_secret": "private-catalog-secret"},
            key_hint=None,
            status="disabled",
            created_by="u_member",
        )
    disabled = (await client.get("/api/v1/capability-catalog")).json()
    assert not any(item["source_kind"] != "native" for item in disabled["tools"])
    for provider in ("google_workspace", "powerbi"):
        await store.update_team_builtin_tool_status("team_a", provider, "enabled")
    response = await client.get("/api/v1/capability-catalog")
    assert response.status_code == 200, response.text
    sources = {item["source_display_name"] for item in response.json()["tools"]}
    assert any(source.startswith("Google Workspace - ") for source in sources)
    assert any(source.startswith("Power BI - ") for source in sources)
    assert "private-catalog-secret" not in response.text
    monkeypatch.setenv("FEATURE_GOOGLE_WORKSPACE", "0")
    gated = (await client.get("/api/v1/capability-catalog")).json()
    sources = {item["source_display_name"] for item in gated["tools"]}
    assert not any(source.startswith("Google Workspace - ") for source in sources)
    assert any(source.startswith("Power BI - ") for source in sources)


async def test_system_parts_are_not_listed_as_tools(catalog_context, monkeypatch):
    client, _store = catalog_context
    tool = AuthoringTool(
        stable_ref="bi_query",
        display_name="Query Data",
        description="Pull rows",
        source_kind="native",
        source_display_name="Built-in tools",
        capability_classes=frozenset({"data_query"}),
        risk="read",
        discoverable=True,
        selectable=True,
        locked_reason=None,
        risk_source="native_registry",
        approval_required=False,
        confirmation_summary=None,
        dependency_revision="native-authoring-v2",
    )
    parts = (replace(tool, stable_ref="configuration_apply", risk="unknown", selectable=False), replace(tool, stable_ref="skill_read"))
    monkeypatch.setattr(
        capability_catalog.SubagentAuthoringCatalogService,
        "build",
        AsyncMock(return_value=SimpleNamespace(native_tools=(tool, *parts), team_tools=())),
    )
    response = await client.get("/api/v1/capability-catalog")
    assert response.status_code == 200, response.text
    assert [item["display_name"] for item in response.json()["tools"]] == ["Query Data"]
