"""Real Team registry, encrypted credentials, catalog and Shopify Plugin flows."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx2
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.builtin_shopify_tool import build_builtin_shopify_specs
from bowhead_core.shopify_client import ADMIN_API_VERSION, ShopifyClient
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.routes import shopify_content_confirmations
from bowhead_gateway.routes import team_builtin_tools as routes
from bowhead_gateway.routes.auth import _current_user
from cryptography.fernet import Fernet
from fastapi import FastAPI
from test_agents_routes import _seed

from bowhead_skill import default_catalog

pytestmark = pytest.mark.integration


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    from bowhead_gateway.routes import auth

    from bowhead_core import builtin_shopify_tool, shopify_client

    monkeypatch.setenv("FEATURE_SHOPIFY_PLUGIN", "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    _seed(path)
    store = UserStore(path)
    app = FastAPI()
    app.state.user_store = store
    app.state.skill_catalog = default_catalog()
    app.include_router(routes.router, prefix="/api/v1/team-builtin-tools")
    state = SimpleNamespace(
        user=User(id="user_1", team_id="team_1", email="user_1@example.com", status="active"),
        calls=[],
        scopes="read_products,read_orders,read_all_orders,read_inventory,read_content,read_locations,read_customers",
        objects={},
    )
    app.dependency_overrides[_current_user] = lambda: state.user

    async def authenticated(_request):
        return state.user

    monkeypatch.setattr(auth, "_current_user", authenticated)

    async def external(request):
        state.calls.append(request)
        if request.url.path.endswith("access_token"):
            return httpx2.Response(
                200,
                json={
                    "access_token": "fixture-access",
                    "expires_in": 86399,
                    "scope": state.scopes,
                },
            )
        payload = json.loads(request.content)
        if "Content(" in payload.get("query", ""):
            resource = "product" if "product" in payload["query"] else "page"
            if payload["query"].startswith("mutation"):
                patch = {key: value for key, value in payload["variables"]["input"].items() if key != "id"}
                state.objects[resource].update(patch, updatedAt="2026-10-09T12:01:00Z")
                data = {resource + "Update": {resource: copy.deepcopy(state.objects[resource]), "userErrors": []}}
            else:
                data = {resource: copy.deepcopy(state.objects[resource])}
            return httpx2.Response(200, headers={"X-Shopify-API-Version": ADMIN_API_VERSION}, json={"data": data})
        return httpx2.Response(
            200,
            headers={"X-Shopify-API-Version": ADMIN_API_VERSION},
            json={
                "data": {"shop": {"id": "gid://shopify/Shop/1", "name": "Fixture shop", "currencyCode": "USD", "ianaTimezone": "America/New_York"}}
            },
        )

    def client(credential):
        return ShopifyClient(credential, transport=httpx2.MockTransport(external))

    monkeypatch.setattr(shopify_client, "ShopifyClient", client)
    monkeypatch.setattr(builtin_shopify_tool, "ShopifyClient", client)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as http:
        yield SimpleNamespace(http=http, app=app, store=store, state=state)


_INPUT = {"name": "Commerce", "shop_domain": "fixture-shop.myshopify.com", "client_id": "fixture-client", "client_secret": "fixture-secret"}


async def test_admin_connection_plugin_and_agent_tool_use_the_same_shop(setup):
    response = await setup.http.post("/api/v1/team-builtin-tools/shopify", json=_INPUT)
    assert response.status_code == 200, response.text
    connection = response.json()
    tool_id = connection["id"]
    assert "fixture-secret" not in response.text and "auth_ref" not in connection
    assert connection["connection_revision"] == 1
    saved = await setup.store.get_team_builtin_tool_instance("team_1", tool_id)
    assert saved.auth_ref.startswith("FERNET:")
    assert "fixture-secret" not in saved.auth_ref
    assert saved.config["shop_gid"] == "gid://shopify/Shop/1"
    assert await setup.store.get_team_builtin_tool_instance("other_team", tool_id) is None
    created = await setup.http.post(f"/api/v1/team-builtin-tools/shopify/{tool_id}/plugin")
    assert created.status_code == 200, created.text
    plugin = created.json()
    assert plugin["status"] == "active"
    assert plugin["skill_grants"][0]["skill_ref"]["id"] == "builtin:shopify-analysis"
    assert plugin["skill_grants"][0]["permissions"] == ["read"]
    assert all(ref.startswith(f"teammcp.{tool_id}.shopify_") for ref in plugin["tool_refs"])
    assert len(plugin["tool_refs"]) == 11
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.team_plugin_store import expand_plugins_conn

    async with open_shared_ctx(setup.store.db_path) as db:
        expanded = await expand_plugins_conn(db, team_id="team_1", definition={"capability_ids": [plugin["id"]]})
    assert expanded["tool_refs"] == sorted(plugin["tool_refs"])
    assert expanded["skill_grants"] == plugin["skill_grants"]
    view = build_builtin_shopify_specs(team_id="team_1", tools=[saved], store=setup.store, base_registry=None)
    selected = next(spec for spec in view if spec.schema.name in plugin["tool_refs"] and spec.schema.name.endswith("shopify_shop_context"))
    ctx = ToolExecutionContext(workspace_id="ws_1", member_uid="", user_id="owner_1", turn_id="turn_1", datarc=None, team_id="team_1")
    assert (await selected.handler({}, ctx))["data"]["shop"]["id"] == "gid://shopify/Shop/1"
    disabled = await setup.http.patch(f"/api/v1/team-builtin-tools/shopify/{tool_id}/status", json={"enabled": False, "connection_revision": 1})
    assert disabled.status_code == 200
    assert disabled.json()["connection_revision"] == 2
    before = len(setup.state.calls)
    assert (await selected.handler({}, ctx))["ok"] is False
    assert len(setup.state.calls) == before
    stale = await setup.http.patch(f"/api/v1/team-builtin-tools/shopify/{tool_id}", json={"name": "Other", "connection_revision": 1})
    assert stale.status_code == 409
    assert len(setup.state.calls) == before


async def test_member_cannot_manage_or_read_connection_credentials(setup):
    created = await setup.http.post("/api/v1/team-builtin-tools/shopify", json=_INPUT)
    tool_id = created.json()["id"]
    setup.state.user = User(id="owner_1", team_id="team_1", email="owner_1@example.com", status="active")
    calls = len(setup.state.calls)
    for method, url, body in [
        ("GET", "/api/v1/team-builtin-tools", None),
        ("POST", "/api/v1/team-builtin-tools/shopify", _INPUT),
        ("PATCH", f"/api/v1/team-builtin-tools/shopify/{tool_id}", {"connection_revision": 1, "client_secret": "new-secret"}),
        ("PATCH", f"/api/v1/team-builtin-tools/shopify/{tool_id}/status", {"connection_revision": 1, "enabled": False}),
        ("DELETE", f"/api/v1/team-builtin-tools/shopify/{tool_id}", None),
        ("POST", f"/api/v1/team-builtin-tools/shopify/{tool_id}/plugin", None),
    ]:
        response = await setup.http.request(method, url, json=body)
        assert response.status_code == 403, response.text
    assert len(setup.state.calls) == calls


async def test_shop_identity_is_immutable_and_feature_off_hides_connections(setup, monkeypatch):
    response = await setup.http.post("/api/v1/team-builtin-tools/shopify", json=_INPUT)
    tool_id = response.json()["id"]
    denied = await setup.http.patch(
        f"/api/v1/team-builtin-tools/shopify/{tool_id}", json={"connection_revision": 1, "shop_domain": "other.myshopify.com"}
    )
    assert denied.status_code == 422
    monkeypatch.setenv("FEATURE_SHOPIFY_PLUGIN", "0")
    assert (await setup.http.get("/api/v1/team-builtin-tools")).json() == []
    count = len(setup.state.calls)
    assert (await setup.http.post("/api/v1/team-builtin-tools/shopify", json=_INPUT)).status_code == 404
    assert (await setup.http.post(f"/api/v1/team-builtin-tools/shopify/{tool_id}/plugin")).status_code == 404
    assert len(setup.state.calls) == count


async def test_credential_rotation_uses_revision_compare_and_swap(setup):
    response = await setup.http.post("/api/v1/team-builtin-tools/shopify", json=_INPUT)
    tool_id = response.json()["id"]
    changed = await setup.http.patch(
        f"/api/v1/team-builtin-tools/shopify/{tool_id}", json={"connection_revision": 1, "client_secret": "replacement-secret"}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["connection_revision"] == 2
    assert "replacement-secret" not in changed.text
    assert (await setup.store.get_team_builtin_tool_instance_auth_decrypted("team_1", tool_id))["client_secret"] == "replacement-secret"
    repeated = await setup.http.patch(
        f"/api/v1/team-builtin-tools/shopify/{tool_id}", json={"connection_revision": 1, "client_secret": "stale-secret"}
    )
    assert repeated.status_code == 409
    token_request = next(request for request in reversed(setup.state.calls) if request.url.path.endswith("access_token"))
    assert json.loads(token_request.content)["client_secret"] == "replacement-secret"


async def test_content_plugin_http_review_and_native_write_keep_analysis_readonly(setup, monkeypatch):
    import anyio

    from bowhead_core import shopify_content

    monkeypatch.setenv("FEATURE_SHOPIFY_CONTENT_WRITES", "1")
    monkeypatch.setattr(shopify_content, "_default", shopify_content.ShopifyContentConfirmations(timeout_seconds=2))
    setup.app.include_router(shopify_content_confirmations.router, prefix="/api/v1/shopify-content-confirmations")
    setup.state.scopes += ",write_products,write_content"
    original = {
        "id": "gid://shopify/Page/1",
        "title": "About us",
        "handle": "about-us",
        "body": "<p>Original story</p>",
        "isPublished": False,
        "createdAt": "2026-10-01T00:00:00Z",
        "updatedAt": "2026-10-09T12:00:00Z",
        "publishedAt": None,
    }
    setup.state.objects["page"] = original
    connection = (await setup.http.post("/api/v1/team-builtin-tools/shopify", json=_INPUT)).json()
    tool_id = connection["id"]
    analysis = (await setup.http.post(f"/api/v1/team-builtin-tools/shopify/{tool_id}/plugin")).json()
    assert len(analysis["tool_refs"]) == 11
    assert not any("content_update" in ref for ref in analysis["tool_refs"])
    response = await setup.http.post(f"/api/v1/team-builtin-tools/shopify/{tool_id}/content-plugin")
    assert response.status_code == 200, response.text
    plugin = response.json()
    assert plugin["skill_grants"][0]["skill_ref"]["id"] == "builtin:shopify-content-optimization"
    assert plugin["skill_grants"][0]["permissions"] == ["read"]
    assert f"teammcp.{tool_id}.shopify_apply_content_update" in plugin["tool_refs"]
    assert not any("orders" in ref or "inventory" in ref for ref in plugin["tool_refs"])
    saved = await setup.store.get_team_builtin_tool_instance("team_1", tool_id)
    view = {spec.schema.name: spec for spec in build_builtin_shopify_specs(team_id="team_1", tools=[saved], store=setup.store, base_registry=None)}
    workspace_id = await setup.store.create_local_workspace(team_id="team_1", name="Content", creator_user_id="user_1")
    ctx = ToolExecutionContext(
        workspace_id=workspace_id,
        member_uid="",
        user_id="user_1",
        turn_id="t_content",
        session_id="s_content",
        datarc=None,
        team_id="team_1",
        config_lite_supported=True,
    )
    args = {"resource": "page", "object_id": original["id"], "changes": {"body_html": "<p>Reviewed story</p>"}}
    preview = await view[f"teammcp.{tool_id}.shopify_preview_content_update"].handler(args, ctx)
    assert preview["write_performed"] is False
    waiting = asyncio.create_task(
        view[f"teammcp.{tool_id}.shopify_apply_content_update"].handler({**args, "review_token": preview["review_token"]}, ctx)
    )
    with anyio.fail_after(2):
        while not (items := (await setup.http.get("/api/v1/shopify-content-confirmations")).json()["confirmations"]):
            await anyio.sleep(0.001)
    assert original["body"] == "<p>Original story</p>"
    assert items[0]["edits"] == [{"field": "body_html", "before": "<p>Original story</p>", "after": "<p>Reviewed story</p>"}]
    path = f"/api/v1/shopify-content-confirmations/{items[0]['id']}/decision"
    setup.state.user = User(id="owner_1", team_id="team_1", email="owner_1@example.com", status="active")
    assert (await setup.http.get("/api/v1/shopify-content-confirmations")).json()["confirmations"] == []
    assert (await setup.http.post(path, json={"decision": "approve"})).status_code == 409
    setup.state.user = User(id="user_1", team_id="team_1", email="user_1@example.com", status="active")
    assert (await setup.http.post(path, json={"decision": "approve", "changes": {"body": "forged"}})).status_code == 422
    assert (await setup.http.post(path, json={"decision": "approve"})).status_code == 200
    assert (await setup.http.post(path, json={"decision": "approve"})).status_code == 409
    result = await waiting
    assert result["status"] == "updated_and_verified", result
    assert result["data"]["body"] == "<p>Reviewed story</p>"
    assert result["data"]["handle"] == "about-us" and result["data"]["isPublished"] is False
    assert len([request for request in setup.state.calls if json.loads(request.content).get("query", "").startswith("mutation")]) == 1
    assert "fixture-secret" not in repr(items) + repr(result) + repr(plugin)


async def test_content_plugin_is_unavailable_without_admission_and_members_cannot_create_it(setup, monkeypatch):
    tool_id = (await setup.http.post("/api/v1/team-builtin-tools/shopify", json=_INPUT)).json()["id"]
    assert (await setup.http.post(f"/api/v1/team-builtin-tools/shopify/{tool_id}/content-plugin")).status_code == 422
    monkeypatch.setenv("FEATURE_SHOPIFY_CONTENT_WRITES", "1")
    setup.state.user = User(id="owner_1", team_id="team_1", email="owner_1@example.com", status="active")
    assert (await setup.http.post(f"/api/v1/team-builtin-tools/shopify/{tool_id}/content-plugin")).status_code == 403
