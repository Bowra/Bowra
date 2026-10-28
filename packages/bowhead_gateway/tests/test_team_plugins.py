"""Team Capability management is team scoped and administrator guarded."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx2
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.routes import agents, team_plugins
from bowhead_gateway.routes.auth import _current_user, _store
from fastapi import FastAPI
from test_agents_routes import _seed


@pytest.mark.asyncio
async def test_only_team_admin_can_manage_plugin(tmp_path, monkeypatch):
    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    _seed(path)
    store = UserStore(path)
    current = User(id="user_1", team_id="team_1", email="user_1@example.com", status="active")
    monkeypatch.setattr(team_plugins, "_check_resources", AsyncMock(return_value=[]))
    app = FastAPI()
    app.include_router(team_plugins.router, prefix="/api/v1/team-capabilities")
    app.dependency_overrides[_current_user] = lambda: current
    app.dependency_overrides[_store] = lambda: store
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        payload = {"name": "Research", "description": "Research resources", "tool_refs": ["web_search"], "skill_grants": []}
        created = await client.post("/api/v1/team-capabilities", json=payload)
        assert created.status_code == 200, created.text
        capability_id = created.json()["id"]
        assert (await client.get("/api/v1/team-capabilities")).json()[0]["id"] == capability_id

        current = User(id="owner_1", team_id="team_1", email="owner_1@example.com", status="active")
        assert (await client.get(f"/api/v1/team-capabilities/{capability_id}")).status_code == 200
        denied = await client.put(f"/api/v1/team-capabilities/{capability_id}", json={**payload, "row_version": 1})
        assert denied.status_code == 403
        denied_status = await client.patch(f"/api/v1/team-capabilities/{capability_id}/status", json={"enabled": False, "row_version": 1})
        assert denied_status.status_code == 403
        denied_delete = await client.delete(f"/api/v1/team-capabilities/{capability_id}?row_version=1")
        assert denied_delete.status_code == 403
        current = User(id="user_1", team_id="team_1", email="user_1@example.com", status="active")
        assert (await client.delete(f"/api/v1/team-capabilities/{capability_id}?row_version=2")).status_code == 409
        assert (await client.delete(f"/api/v1/team-capabilities/{capability_id}?row_version=1")).status_code == 204
        assert (await client.get(f"/api/v1/team-capabilities/{capability_id}")).status_code == 404


@pytest.mark.asyncio
async def test_plugin_accepts_selectable_builtin_third_party_tool(tmp_path, monkeypatch):
    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    _seed(path)
    store = UserStore(path)
    current = User(id="user_1", team_id="team_1", email="user_1@example.com", status="active")
    tool_ref = "teammcp.ibt_sales.datarc_query"
    monkeypatch.setattr(
        agents,
        "agent_authoring_catalog",
        AsyncMock(return_value={"tools": [{"stable_ref": tool_ref, "selectable": True}], "skills": []}),
    )
    app = FastAPI()
    app.include_router(team_plugins.router, prefix="/api/v1/team-capabilities")
    app.dependency_overrides[_current_user] = lambda: current
    app.dependency_overrides[_store] = lambda: store
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        payload = {"name": "Sales", "tool_refs": [tool_ref]}
        created = await client.post("/api/v1/team-capabilities", json=payload)
        assert created.status_code == 200, created.text
        assert created.json()["tool_refs"] == [tool_ref]
        unavailable = await client.post("/api/v1/team-capabilities", json={"name": "Unavailable", "tool_refs": ["teammcp.ibt_missing.datarc_query"]})
        assert unavailable.status_code == 422
        assert unavailable.json()["detail"]["code"] == "capability_tool_unavailable"


@pytest.mark.asyncio
async def test_plugin_errors_reach_the_browser_as_localized_catalog_copy(tmp_path, monkeypatch):
    from bowhead_core.error_catalog import default_error_catalog_dir, load_error_catalog
    from bowhead_gateway.error_handler import register_error_handlers

    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    _seed(path)
    store = UserStore(path)
    current = User(id="user_1", team_id="team_1", email="user_1@example.com", status="active")
    monkeypatch.setattr(agents, "agent_authoring_catalog", AsyncMock(return_value={"tools": [], "skills": []}))
    app = FastAPI()
    register_error_handlers(app)
    app.state.error_catalog = load_error_catalog(default_error_catalog_dir())
    app.include_router(team_plugins.router, prefix="/api/v1/team-capabilities")
    app.dependency_overrides[_current_user] = lambda: current
    app.dependency_overrides[_store] = lambda: store
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(
            "/api/v1/team-capabilities", json={"name": "Missing", "tool_refs": ["web_search"]}, headers={"Accept-Language": "zh-CN"}
        )
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail["code"] == "capability_tool_unavailable"
    assert "团队设置 → 工具" in detail["message"]


def _team_skill_grant(revision: str) -> dict:
    return {
        "skill_ref": {"kind": "team", "id": "tsk_sales", "name": "sales-report"},
        "permissions": ["read"],
        "revision": revision,
        "distribution": "reference",
    }


@pytest.mark.asyncio
async def test_plugin_edit_after_skill_reapproval_keeps_the_skill_and_stores_its_current_revision(tmp_path, monkeypatch):
    """Skills follow their latest approved revision; a re-approval must not block editing the Plugin."""
    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    _seed(path)
    store = UserStore(path)
    current = User(id="user_1", team_id="team_1", email="user_1@example.com", status="active")
    first, second = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    catalog = {"tools": [], "skills": [{"source_kind": "team", "grant": _team_skill_grant(first)}]}
    monkeypatch.setattr(agents, "agent_authoring_catalog", AsyncMock(side_effect=lambda *_args: catalog))
    app = FastAPI()
    app.include_router(team_plugins.router, prefix="/api/v1/team-capabilities")
    app.dependency_overrides[_current_user] = lambda: current
    app.dependency_overrides[_store] = lambda: store
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/v1/team-capabilities", json={"name": "Sales", "skill_grants": [_team_skill_grant(first)]})
        assert created.status_code == 200, created.text
        saved = created.json()
        # The Skill is edited and re-approved; the catalog now offers its new revision.
        catalog["skills"] = [{"source_kind": "team", "grant": _team_skill_grant(second)}]
        # The editor resubmits the Plugin's stored grants unchanged and only renames it.
        renamed = await client.put(
            f"/api/v1/team-capabilities/{saved['id']}",
            json={
                "name": "Sales analysis",
                "description": saved["description"],
                "tool_refs": saved["tool_refs"],
                "skill_grants": saved["skill_grants"],
                "row_version": saved["row_version"],
            },
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["skill_grants"] == [_team_skill_grant(second)]
        # A different permission set is still refused.
        changed = {**_team_skill_grant(second), "permissions": ["invoke", "read"]}
        refused = await client.put(
            f"/api/v1/team-capabilities/{saved['id']}",
            json={"name": "Sales analysis", "skill_grants": [changed], "row_version": renamed.json()["row_version"]},
        )
        assert refused.status_code == 422
        assert refused.json()["detail"]["code"] == "capability_skill_unavailable"
