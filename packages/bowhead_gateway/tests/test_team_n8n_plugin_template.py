"""The "n8n workflows" Plugin template bundles approved workflow tools with the n8n-workflows Skill."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.n8n_workflows import build_approval, save_n8n_approval
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.user_store import TeamToolSource, User, UserStore, _now
from bowhead_gateway.routes import team_builtin_tools as routes
from bowhead_gateway.routes import team_plugins
from bowhead_gateway.routes.auth import _current_user
from cryptography.fernet import Fernet
from fastapi import FastAPI
from gateway_legacy_database import upgrade
from test_agents_routes import _seed

from bowhead_skill import default_catalog

pytestmark = pytest.mark.integration

_SRC = "ots_n8n000000001"
_RAW = [
    {"name": name, "input_schema": {"type": "object"}, "annotations": {"readOnlyHint": True, "destructiveHint": False}}
    for name in ("search_workflows", "get_workflow_details", "execute_workflow", "get_workflow_execution")
]
_WORKFLOW = {
    "id": "wfMoM",
    "name": "MoM",
    "active": True,
    "isArchived": False,
    "activeVersionId": "v1",
    "canExecute": True,
    "nodes": [{"name": "Webhook", "type": "n8n-nodes-base.webhook", "parameters": {}}],
    "activeVersion": {"sameAsDraft": True},
}


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    from bowhead_gateway.routes import auth

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FEATURE_N8N_WORKFLOW_TOOLS", "1")
    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    _seed(path)
    store = UserStore(path)
    await TeamSettingsStore(path).init()
    await store.create_team_tool_source(
        TeamToolSource(
            id=_SRC,
            team_id="team_1",
            name="Sales n8n",
            kind="mcp",
            base_url="https://n8n.example.com/mcp-server/http",
            tools_schema=_RAW,
            selected_op_ids=[],
            created_at=_now(),
            updated_at=_now(),
        )
    )
    app = FastAPI()
    app.state.user_store = store
    app.state.skill_catalog = default_catalog()
    app.include_router(routes.router, prefix="/api/v1/team-builtin-tools")
    app.include_router(team_plugins.router, prefix="/api/v1/team-capabilities")
    user = User(id="user_1", team_id="team_1", email="user_1@example.com", status="active")
    app.dependency_overrides[_current_user] = lambda: user

    async def authenticated(_request):
        return user

    monkeypatch.setattr(auth, "_current_user", authenticated)
    import httpx2

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as http:
        yield SimpleNamespace(http=http, store=store)


async def _templates(http) -> dict[str, dict]:
    response = await http.get("/api/v1/team-builtin-tools/plugin-templates")
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()["templates"]}


async def test_the_template_offers_approved_workflows_with_the_skill(setup, monkeypatch):
    assert f"n8n_workflows:{_SRC}" not in await _templates(setup.http)
    approval = build_approval(
        workflow_id="wfMoM",
        workflow=_WORKFLOW,
        description="Sales change by region",
        input_schema={"type": "object"},
        category="read",
        actor_user_id="u",
    )
    await save_n8n_approval(setup.store.db_path, team_id="team_1", source_id=_SRC, workflow_id="wfMoM", approval=approval, actor_user_id="u")
    template = (await _templates(setup.http))[f"n8n_workflows:{_SRC}"]
    assert template["kind"] == "n8n_workflows" and template["connection_name"] == "Sales n8n"
    assert [ref.rsplit(".", 1)[-1] for ref in template["tool_refs"]] == ["n8n_wf_wfMoM"]
    assert template["skill_grants"][0]["skill_ref"]["id"] == "builtin:n8n-workflows"
    body = {"name": "n8n", "description": "", "tool_refs": template["tool_refs"], "skill_grants": template["skill_grants"]}
    created = await setup.http.post("/api/v1/team-capabilities", json=body)
    assert created.status_code == 200, created.text

    monkeypatch.setenv("FEATURE_N8N_WORKFLOW_TOOLS", "0")
    assert f"n8n_workflows:{_SRC}" not in await _templates(setup.http)
