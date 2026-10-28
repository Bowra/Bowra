"""Shared Developer API credentials remain useful without Organization API code."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.openapi_store import OpenApiStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.edition_extensions import GatewayExtensions
from bowhead_gateway.routes import team_openapi
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bowhead_core import migrations


@pytest.fixture
async def community(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "community-api-test-key")
    users = UserStore(tmp_path / "community.db")
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{users.db_path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    await users.initialize()
    team = await users.create_team(name="Community")
    owner = await users.create_user(team_id=team.id, email="owner@example.com", password="Passw0rd!")
    keys = OpenApiStore(str(users.db_path))
    app = FastAPI()
    app.state.gateway_extensions = GatewayExtensions()
    app.state.openapi_store = keys
    app.include_router(team_openapi.manage_router)
    app.include_router(team_openapi.api_router)
    app.dependency_overrides[team_openapi._ADMIN] = lambda: owner
    return app, keys, owner


async def test_community_metadata_describes_agent_automation(community, monkeypatch):
    app, _, _ = community
    with TestClient(app) as client:
        reference = client.get("/api/openapi/v1/reference.md")
        document = client.get("/api/v1/team-openapi/documentation").json()
        assert reference.status_code == 200
        assert document["scopes"] == ["agent_versions:read", "agent_versions:write"]
        assert "/agents/{agent_id}/version-proposals" in reference.text
        assert "/users" not in reference.text
        assert client.get("/api/openapi/v1/users").status_code == 404


async def test_community_can_create_and_update_agent_keys_but_not_organization_scopes(community):
    app, keys, owner = community
    request = SimpleNamespace(app=app)
    created = await team_openapi.create_key(team_openapi.KeyCreate(name="CI", scopes=["agent_versions:read"], permanent=True), request, owner)
    key_id = created["key"]["id"]
    updated = await team_openapi.update_key(key_id, team_openapi.KeyUpdate(scopes=["agent_versions:write"], permanent=True), request, owner)
    assert updated["id"] == key_id
    assert (await keys.authenticate(created["token"]))["scopes"] == ["agent_versions:write"]

    for scope in ("users:write", "spaces:read", "space_members:write", "unknown:read"):
        with pytest.raises(HTTPException) as create_denied:
            await team_openapi.create_key(team_openapi.KeyCreate(name="Denied", scopes=[scope]), request, owner)
        assert create_denied.value.status_code == 422
        with pytest.raises(HTTPException) as update_denied:
            await team_openapi.update_key(key_id, team_openapi.KeyUpdate(scopes=[scope], permanent=True), request, owner)
        assert update_denied.value.status_code == 422
    assert len(await keys.list_keys(owner.team_id)) == 1
    assert (await keys.get_key(owner.team_id, key_id))["scopes"] == ["agent_versions:write"]


async def test_mixed_persisted_key_retains_agent_access_and_revocation(community):
    app, keys, owner = community
    key, token = await keys.create_key(
        team_id=owner.team_id, name="Existing mixed key", scopes=["agent_versions:write", "users:write"], expires_at=None
    )
    request = SimpleNamespace(app=app, headers={"authorization": f"Bearer {token}"}, method="GET", url=SimpleNamespace(path="/api/openapi/v1/scopes"))
    credential = await team_openapi.openapi_credential(request, "agent_versions:read")
    assert credential["id"] == key["id"]
    assert (await team_openapi.get_scopes(request, credential))["scopes"] == ["agent_versions:write"]
    with pytest.raises(HTTPException) as denied:
        await team_openapi.openapi_credential(request, "users:read")
    assert denied.value.status_code == 403
    assert (await keys.get_key(owner.team_id, key["id"]))["scopes"] == ["agent_versions:write", "users:write"]

    await keys.set_status(owner.team_id, key["id"], "disabled")
    with pytest.raises(HTTPException) as revoked:
        await team_openapi.openapi_credential(request, "agent_versions:read")
    assert revoked.value.status_code == 401
