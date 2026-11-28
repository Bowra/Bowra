"""Approving n8n workflows as derived team tools (FEATURE_N8N_WORKFLOW_TOOLS, tech design D1 and D3)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.user_store import TeamToolSource, User, UserStore, _now
from bowhead_gateway.routes import team_n8n_workflows as routes
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SRC = "ots_n8n000000001"
_BASE = f"/api/v1/team-tool-sources/{_SRC}/n8n-workflows"
_RAW = [
    {"name": name, "input_schema": {"type": "object"}}
    for name in ("search_workflows", "get_workflow_details", "execute_workflow", "get_workflow_execution")
]
_WEBHOOK = "n8n-nodes-base.webhook"
_DETAILS = {
    "id": "wfMoM",
    "name": "MoM",
    "description": "Month-over-month sales",
    "active": True,
    "isArchived": False,
    "activeVersionId": "v1",
    "canExecute": True,
    "nodes": [
        {"name": "Webhook", "type": _WEBHOOK, "parameters": {}},
        {"name": "Pick", "type": "n8n-nodes-base.set", "parameters": {"v": "={{ $json.body.month }}"}},
    ],
    "activeVersion": {"sameAsDraft": True},
}


def _run(coro):
    return asyncio.run(coro)


def _result(body: dict[str, Any], *, is_error: bool = False) -> SimpleNamespace:
    return SimpleNamespace(is_error=is_error, structured_content=body, content=[SimpleNamespace(text=json.dumps(body))])


class FakeN8n:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.details = dict(_DETAILS)
        self.listing = [
            {"id": "wfMoM", "name": "MoM", "description": "Month-over-month sales", "active": True, "availableInMCP": True},
            {"id": "wfMail", "name": "Send mail to the board", "active": True, "availableInMCP": False},
            {"id": "wfDraft", "name": "Draft only", "active": False, "availableInMCP": True},
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
        self.calls.append(name)
        if name == "search_workflows":
            return _result({"data": self.listing, "count": len(self.listing)})
        if name == "get_workflow_details":
            return _result({"workflow": self.details, "triggerInfo": "webhook"})
        raise AssertionError(f"unexpected {name}")

    @asynccontextmanager
    async def session(self):
        yield self


@pytest.fixture
def store(tmp_path, monkeypatch) -> UserStore:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FEATURE_N8N_WORKFLOW_TOOLS", "1")
    path = tmp_path / "bowhead.db"
    users = UserStore(str(path))
    _run(users.initialize())
    _run(TeamSettingsStore(path).init())
    _run(
        users.create_team_tool_source(
            TeamToolSource(
                id=_SRC,
                team_id="team_a",
                name="n8n",
                kind="mcp",
                base_url="https://n8n.example.com/mcp-server/http",
                tools_schema=_RAW,
                selected_op_ids=[],
                created_at=_now(),
                updated_at=_now(),
            )
        )
    )
    return users


@pytest.fixture
def n8n(monkeypatch) -> FakeN8n:
    fake = FakeN8n()
    monkeypatch.setattr(routes, "_client", lambda request, src: fake)
    return fake


def _client(store: UserStore, team_id: str = "team_a") -> TestClient:
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/team-tool-sources")
    app.state.user_store = store
    app.dependency_overrides[routes._REQUIRE_TEAM_ADMIN] = lambda: User(id="usr_admin", team_id=team_id, email="a@x.com")
    return TestClient(app)


def test_listing_names_only_mcp_enabled_published_workflows(store, n8n):
    body = _client(store).get(_BASE).json()
    assert [item["id"] for item in body["items"]] == ["wfMoM"]
    assert body["hidden_count"] == 2
    assert "Send mail to the board" not in json.dumps(body)


def test_opening_a_workflow_proposes_a_draft_schema_and_stores_nothing(store, n8n):
    body = _client(store).get(f"{_BASE}/wfMoM").json()
    assert body["trigger"] == {"node": "Webhook", "kind": "webhook"} and body["problem"] is None
    assert body["draft_input_schema"]["properties"] == {"month": {}}
    assert body["approval"] is None
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_settings WHERE key LIKE 'n8n_workflows:%'").fetchone()[0] == 0


def test_approval_rereads_n8n_and_binds_the_published_version(store, n8n):
    client = _client(store)
    schema = {"type": "object", "properties": {"month": {"type": "string"}}, "required": ["month"]}
    saved = client.put(f"{_BASE}/wfMoM", json={"category": "read", "description": "Sales change by region", "input_schema": schema})
    assert saved.status_code == 200, saved.text
    approval = saved.json()["approval"]
    assert approval["approved_version_id"] == "v1" and approval["tool_ref"].endswith(".n8n_wf_wfMoM")
    assert n8n.calls == ["get_workflow_details"]
    with sqlite3.connect(store.db_path) as db:
        actions = [row[0] for row in db.execute("SELECT action FROM admin_audit_log WHERE target_id=?", (_SRC,))]
    assert actions == ["team_tool_category_changed"]

    n8n.details = {**_DETAILS, "activeVersionId": "v2"}
    assert client.get(f"{_BASE}/wfMoM").json()["version_changed"] is True
    listed = client.get(_BASE).json()["items"][0]
    assert listed["approval"]["category"] == "read"

    assert client.delete(f"{_BASE}/wfMoM").status_code == 200
    assert client.get(_BASE).json()["items"][0]["approval"] is None


def test_workflows_bowhead_cannot_run_are_refused_with_the_reason(store, n8n):
    n8n.details = {**_DETAILS, "canExecute": False}
    refused = _client(store).put(f"{_BASE}/wfMoM", json={"category": "read", "description": "d", "input_schema": {"type": "object"}})
    assert refused.status_code == 422
    error = refused.json()["detail"]["error"]
    assert error["code"] == "n8n_workflow_not_approvable" and error["details"]["reason_code"] == "cannot_execute"


def test_routes_need_the_switch_an_n8n_source_and_the_same_team(store, n8n, monkeypatch):
    assert _client(store, team_id="team_b").get(_BASE).status_code == 404
    assert _client(store).get(f"{_BASE}/..%2Fx").status_code in (404, 422)
    monkeypatch.setenv("FEATURE_N8N_WORKFLOW_TOOLS", "0")
    refused = _client(store).get(_BASE)
    assert refused.status_code == 409 and refused.json()["detail"]["error"]["code"] == "n8n_workflows_unavailable"
    monkeypatch.setenv("FEATURE_N8N_WORKFLOW_TOOLS", "1")
    _run(
        store.create_team_tool_source(
            TeamToolSource(
                id="ots_other00000001",
                team_id="team_a",
                name="CRM",
                kind="mcp",
                base_url="https://crm.example.com/mcp",
                tools_schema=_RAW,
                created_at=_now(),
                updated_at=_now(),
            )
        )
    )
    not_n8n = _client(store).get("/api/v1/team-tool-sources/ots_other00000001/n8n-workflows")
    assert not_n8n.status_code == 422 and not_n8n.json()["detail"]["error"]["code"] == "n8n_workflows_not_n8n"


def test_n8n_failures_become_coded_errors(store, monkeypatch):
    import httpx2

    class Limited(FakeN8n):
        async def call_tool(self, name, arguments):
            request = httpx2.Request("POST", "https://n8n.example.com/mcp-server/http")
            raise httpx2.HTTPStatusError("429", request=request, response=httpx2.Response(429, headers={"retry-after": "30"}, request=request))

    monkeypatch.setattr(routes, "_client", lambda request, src: Limited())
    limited = _client(store).get(_BASE)
    assert limited.status_code == 429
    assert limited.json()["detail"]["error"] == {"code": "n8n_rate_limited", "details": {"retry_after_seconds": 30}}
