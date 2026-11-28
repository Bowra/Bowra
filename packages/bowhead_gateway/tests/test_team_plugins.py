"""Team Capability management is team scoped and administrator guarded."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.routes import agents, team_plugins
from bowhead_gateway.routes.auth import _current_user, _store
from fastapi import FastAPI
from test_agents_routes import _seed


@pytest.mark.asyncio
async def test_only_team_admin_can_manage_plugin(tmp_path, monkeypatch, initialize_migrated_core_database):
    path = tmp_path / "bowhead.db"
    initialize_migrated_core_database(path.with_name("bowhead.db"))
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
async def test_plugin_accepts_selectable_builtin_third_party_tool(tmp_path, monkeypatch, initialize_migrated_core_database):
    path = tmp_path / "bowhead.db"
    initialize_migrated_core_database(path.with_name("bowhead.db"))
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
async def test_plugin_keeps_a_tool_the_team_turned_off_but_refuses_adding_it(tmp_path, monkeypatch, initialize_migrated_core_database):
    path = tmp_path / "bowhead.db"
    initialize_migrated_core_database(path.with_name("bowhead.db"))
    _seed(path)
    store = UserStore(path)
    current = User(id="user_1", team_id="team_1", email="user_1@example.com", status="active")
    catalog = {"tools": [{"stable_ref": "web_search", "selectable": True}], "team_disabled_tools": [], "skills": []}
    monkeypatch.setattr(agents, "agent_authoring_catalog", AsyncMock(side_effect=lambda *_args: catalog))
    app = FastAPI()
    app.include_router(team_plugins.router, prefix="/api/v1/team-capabilities")
    app.dependency_overrides[_current_user] = lambda: current
    app.dependency_overrides[_store] = lambda: store
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/v1/team-capabilities", json={"name": "Research", "tool_refs": ["web_search"]})
        assert created.status_code == 200, created.text
        # The team turns 网络搜索 off: the catalog stops offering it and names it as turned off.
        catalog.update(tools=[], team_disabled_tools=["web_search"])
        renamed = await client.put(
            f"/api/v1/team-capabilities/{created.json()['id']}",
            json={"name": "Research desk", "tool_refs": ["web_search"], "row_version": created.json()["row_version"]},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["tool_refs"] == ["web_search"]
        refused = await client.post("/api/v1/team-capabilities", json={"name": "Search", "tool_refs": ["web_search"]})
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "tool_disabled_by_team"


@pytest.mark.asyncio
async def test_plugin_errors_reach_the_browser_as_localized_catalog_copy(tmp_path, monkeypatch, initialize_migrated_core_database):
    from bowhead_core.error_catalog import default_error_catalog_dir, load_error_catalog
    from bowhead_gateway.error_handler import register_error_handlers

    path = tmp_path / "bowhead.db"
    initialize_migrated_core_database(path.with_name("bowhead.db"))
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
async def test_plugin_edit_after_skill_reapproval_keeps_the_skill_and_stores_its_current_revision(
    tmp_path, monkeypatch, initialize_migrated_core_database
):
    """Skills follow their latest approved revision; a re-approval must not block editing the Plugin."""
    path = tmp_path / "bowhead.db"
    initialize_migrated_core_database(path.with_name("bowhead.db"))
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


def _lose_lease(monkeypatch, times: int) -> list[int]:
    """Make the next Plugin write statements fail inside their transaction with the runtime's lease error."""
    from bowhead_core import sqlite_domain_runtime, team_plugin_store

    attempts: list[int] = []
    original = team_plugin_store._stamp

    def stamp() -> str:
        attempts.append(len(attempts) + 1)
        if len(attempts) <= times:
            raise sqlite_domain_runtime._busy()
        return original()

    monkeypatch.setattr(team_plugin_store, "_stamp", stamp)
    return attempts


@pytest.mark.asyncio
async def test_plugin_writes_repeat_after_losing_the_authority_lease(tmp_path, monkeypatch, initialize_migrated_core_database):
    from bowhead_core.team_plugin_store import TeamPluginStore

    path = tmp_path / "bowhead.db"
    initialize_migrated_core_database(path.with_name("bowhead.db"))
    _seed(path)
    plugins = TeamPluginStore(str(path))
    contents = {"description": "", "tool_refs": ["web_search"], "skill_grants": []}

    attempts = _lose_lease(monkeypatch, 1)
    created = await plugins.create(team_id="team_1", actor_user_id="user_1", name="Research", **contents)
    assert attempts == [1, 2]
    attempts = _lose_lease(monkeypatch, 1)
    updated = await plugins.update(
        team_id="team_1", plugin_id=created["id"], actor_user_id="user_1", expected_row_version=created["row_version"], name="Research 2", **contents
    )
    attempts = _lose_lease(monkeypatch, 1)
    # Only the status transaction repeats; the read that follows it must not meet its own new row version.
    disabled = await plugins.set_enabled(team_id="team_1", plugin_id=created["id"], enabled=False, expected_row_version=updated["row_version"])
    assert disabled["status"] == "disabled"
    assert disabled["row_version"] == updated["row_version"] + 1
    attempts = _lose_lease(monkeypatch, 1)
    await plugins.delete(team_id="team_1", plugin_id=created["id"], expected_row_version=disabled["row_version"])
    assert attempts == [1, 2]
    assert await plugins.list("team_1", include_disabled=True) == []


@pytest.mark.asyncio
async def test_plugin_write_that_never_gets_the_lease_answers_busy_and_writes_nothing(tmp_path, monkeypatch, initialize_migrated_core_database):
    from bowhead_core.error_catalog import default_error_catalog_dir, load_error_catalog
    from bowhead_gateway.error_handler import register_error_handlers

    from bowhead_core import _db, sqlite_request_deadline

    path = tmp_path / "bowhead.db"
    initialize_migrated_core_database(path.with_name("bowhead.db"))
    _seed(path)
    store = UserStore(path)
    current = User(id="user_1", team_id="team_1", email="user_1@example.com", status="active")
    monkeypatch.setattr(team_plugins, "_check_resources", AsyncMock(return_value=[]))
    elapsed = 0.0

    async def pause(delay: float) -> None:
        nonlocal elapsed
        elapsed += delay

    monkeypatch.setattr(_db, "AUTHORITY_BUSY_WAIT_SECONDS", 0.05)
    clock = SimpleNamespace(monotonic=lambda: elapsed, time=_db.time.time)
    monkeypatch.setattr(_db, "time", clock)
    # A request write's deadline (sqlite_request_deadline) is read against the same clock as the repeat.
    monkeypatch.setattr(sqlite_request_deadline, "time", clock)
    monkeypatch.setattr(_db, "anyio", SimpleNamespace(sleep=pause))
    attempts = _lose_lease(monkeypatch, 10_000)
    app = FastAPI()
    register_error_handlers(app)
    app.state.error_catalog = load_error_catalog(default_error_catalog_dir())
    app.include_router(team_plugins.router, prefix="/api/v1/team-capabilities")
    app.dependency_overrides[_current_user] = lambda: current
    app.dependency_overrides[_store] = lambda: store
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        busy = await client.post(
            "/api/v1/team-capabilities", json={"name": "Research", "tool_refs": ["web_search"]}, headers={"Accept-Language": "zh-CN"}
        )
        assert busy.status_code == 503
        assert busy.headers["Retry-After"] == "2"
        assert busy.json()["detail"]["code"] == "capability_store_busy"
        assert "请几秒后重试" in busy.json()["detail"]["message"]
        assert len(attempts) > 1
        assert (await client.get("/api/v1/team-capabilities")).json() == []
