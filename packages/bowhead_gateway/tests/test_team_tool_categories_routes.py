"""Administrator category confirmation for remote MCP team tools (FEATURE_TEAM_TOOL_CONFIRMATION step 2)."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.user_store import TeamToolSource, User, UserStore, _now
from bowhead_gateway.routes import team_tool_sources as ots
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SRC = "ots_crm000000001"
_BASE = f"/api/v1/team-tool-sources/{_SRC}/tool-categories"
_TOOLS = [
    {"name": "lookup", "description": "Find a contact", "input_schema": {"type": "object"}, "destructive": False},
    {
        "name": "remove",
        "description": "Delete a contact",
        "input_schema": {"type": "object"},
        "destructive": True,
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path, monkeypatch) -> UserStore:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    path = tmp_path / "bowhead.db"
    users = UserStore(str(path))
    _run(users.initialize())
    _run(TeamSettingsStore(path).init())
    _run(
        users.create_team_tool_source(
            TeamToolSource(
                id=_SRC,
                team_id="team_a",
                name="CRM",
                kind="mcp",
                base_url="https://93.184.216.34/mcp",
                tools_schema=_TOOLS,
                selected_op_ids=["lookup", "remove"],
                created_at=_now(),
                updated_at=_now(),
            )
        )
    )
    return users


def _client(store: UserStore, team_id: str = "team_a") -> TestClient:
    app = FastAPI()
    app.include_router(ots.router, prefix="/api/v1/team-tool-sources")
    app.state.user_store = store
    app.dependency_overrides[ots._REQUIRE_TEAM_ADMIN] = lambda: User(id="usr_admin", team_id=team_id, email="a@x.com")
    return TestClient(app)


def test_categories_need_the_switch_and_stay_within_the_team(store, monkeypatch):
    monkeypatch.delenv("FEATURE_TEAM_TOOL_CONFIRMATION", raising=False)
    client = _client(store)
    listed = client.get(_BASE)
    assert listed.status_code == 200
    assert listed.json()["enabled"] is False
    refused = client.put(_BASE, json={"categories": {"lookup": "read"}})
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "team_tool_category_unavailable"
    monkeypatch.setenv("FEATURE_TEAM_TOOL_CONFIRMATION", "1")
    assert _client(store, team_id="team_b").put(_BASE, json={"categories": {"lookup": "read"}}).status_code == 404


def test_administrator_confirms_settles_and_removes_a_category(store, monkeypatch):
    monkeypatch.setenv("FEATURE_TEAM_TOOL_CONFIRMATION", "1")
    client = _client(store)
    before = {item["name"]: item for item in client.get(_BASE).json()["items"]}
    assert before["lookup"]["annotation_category"] == "unknown" and before["lookup"]["confirmed_category"] is None
    assert before["remove"]["effective_category"] == "destructive"

    saved = client.put(_BASE, json={"categories": {"lookup": "read"}})
    assert saved.status_code == 200, saved.text
    items = {item["name"]: item for item in saved.json()["items"]}
    assert items["lookup"]["confirmed_category"] == "read"
    assert items["lookup"]["effective_category"] == "read"
    assert items["lookup"]["confirmed_at"]
    with sqlite3.connect(store.db_path) as db:
        actions = [row[0] for row in db.execute("SELECT action FROM admin_audit_log WHERE target_id=?", (_SRC,))]
    assert actions == ["team_tool_category_changed"]

    cleared = client.put(_BASE, json={"categories": {"lookup": None}})
    assert {item["name"]: item for item in cleared.json()["items"]}["lookup"]["confirmed_category"] is None


def test_a_category_cannot_be_lower_than_the_annotation_or_name_an_unknown_tool(store, monkeypatch):
    monkeypatch.setenv("FEATURE_TEAM_TOOL_CONFIRMATION", "1")
    client = _client(store)
    lower = client.put(_BASE, json={"categories": {"remove": "read"}})
    assert lower.status_code == 422
    assert lower.json()["detail"]["error"] == {
        "code": "team_tool_category_below_annotation",
        "details": {"tool": "remove", "annotation_category": "destructive"},
    }
    unknown = client.put(_BASE, json={"categories": {"missing": "read"}})
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["error"]["details"]["tools"] == ["missing"]


def test_a_changed_definition_asks_for_reconfirmation(store, monkeypatch):
    monkeypatch.setenv("FEATURE_TEAM_TOOL_CONFIRMATION", "1")
    client = _client(store)
    assert client.put(_BASE, json={"categories": {"lookup": "read"}}).status_code == 200
    changed = [{**_TOOLS[0], "description": "Find and update a contact"}, _TOOLS[1]]
    _run(store.update_team_tool_source_meta("team_a", _SRC, tools_schema=changed))
    item = {item["name"]: item for item in client.get(_BASE).json()["items"]}["lookup"]
    assert item["confirmed_category"] is None
    assert item["needs_reconfirmation"] is True
    assert item["effective_category"] == "unknown"
