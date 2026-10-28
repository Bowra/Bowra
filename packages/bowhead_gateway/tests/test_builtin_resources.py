"""Built-in Plugin and Tool state endpoints (Capability Center tech design §8.5)."""

from __future__ import annotations

import time

import aiosqlite
import httpx2
import jwt
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.team_plugin_store import TeamPluginStore
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import UserStore, _id, _now
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

_SECRET = "test_builtin_resources_secret_32b"


def _client(app, user_id: str) -> httpx2.AsyncClient:
    token = jwt.encode({"sub": user_id, "team_id": "team_a", "iat": int(time.time()), "exp": int(time.time()) + 3600}, _SECRET, algorithm="HS256")
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw", cookies={"bowhead_jwt": token})


@pytest.fixture
async def context(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _SECRET)
    monkeypatch.setenv("FEATURE_MULTI_TEAM_AUTHZ", "1")
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "0")
    monkeypatch.delenv("BOWHEAD_PROD", raising=False)
    path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    store = UserStore(str(path))
    await store.initialize()
    now = _now()
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("INSERT INTO teams(id,name,status,created_at) VALUES ('team_a','team_a','active',?)", (now,))
        for user_id, role in (("u_admin", "team_admin"), ("u_member", "member")):
            await db.execute(
                "INSERT INTO users(id,team_id,email,email_verified,password_hash,status,created_at) VALUES (?,'team_a',?,1,'','active',?)",
                (user_id, f"{user_id}@example.com", now),
            )
            await db.execute(
                "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,'team_a',?,'active',?)",
                (_id("om"), user_id, role, now),
            )
        await db.commit()
    app = create_app()
    app.state.settings = Settings()
    app.state.user_store = store
    app.state.team_settings_store = TeamSettingsStore(str(path))
    app.state.user_settings_store = UserSettingsStore(str(path))
    await app.state.team_settings_store.init()
    await app.state.user_settings_store.init()
    async with _client(app, "u_admin") as admin, _client(app, "u_member") as member:
        yield admin, member, store


async def test_team_disable_is_admin_only_and_blocks_personal_restore(context):
    admin, member, _store = context
    assert (await member.put("/api/v1/builtin-plugins/web-research/team-state", json={"disabled": True})).status_code == 403
    response = await admin.put("/api/v1/builtin-plugins/web-research/team-state", json={"disabled": True})
    assert response.status_code == 200, response.text
    restored = await member.put("/api/v1/builtin-plugins/web-research/my-state", json={"disabled": False})
    assert restored.status_code == 409
    plugins = {item["name"]: item for item in (await member.get("/api/v1/builtin-plugins")).json()["plugins"]}
    assert plugins["web-research"]["team_disabled"] is True
    assert plugins["data-analysis"]["listing"]["en-US"]["name"] == "Data analysis"
    assert plugins["settings-space-preferences"]["version"] == 5
    assert plugins["designs"]["ready"] is True and plugins["designs"]["unavailable_tools"] == []
    assert (await admin.put("/api/v1/builtin-plugins/nope/team-state", json={"disabled": True})).status_code == 404
    # Settings Plugins are disabled like any other: the settings assistant refuses their forms.
    for path in ("team-state", "my-state"):
        turned_off = await admin.put(f"/api/v1/builtin-plugins/settings-space-preferences/{path}", json={"disabled": True})
        assert turned_off.status_code == 200, turned_off.text
        cleared = await admin.put(f"/api/v1/builtin-plugins/settings-space-preferences/{path}", json={"disabled": False})
        assert cleared.status_code == 200, cleared.text


async def test_personal_disable_affects_only_that_user(context):
    admin, member, _store = context
    assert (await member.put("/api/v1/builtin-plugins/designs/my-state", json={"disabled": True})).status_code == 200
    mine = {item["name"]: item for item in (await member.get("/api/v1/builtin-plugins")).json()["plugins"]}
    theirs = {item["name"]: item for item in (await admin.get("/api/v1/builtin-plugins")).json()["plugins"]}
    assert mine["designs"]["my_disabled"] is True
    assert theirs["designs"]["my_disabled"] is False


async def test_high_risk_tool_needs_acknowledgement_to_turn_on(context):
    admin, member, _store = context
    tools = {item["name"]: item for item in (await member.get("/api/v1/builtin-tools")).json()["tools"]}
    assert len(tools) == 34
    # High-risk Tools stay enabled by default until per-call confirmation ships (option A).
    assert tools["send_email"] == {"name": "send_email", "risk": "external_write", "enabled": True}
    assert tools["web_search"]["enabled"] is True
    turned_off = await admin.put("/api/v1/builtin-tools/send_email/team-state", json={"enabled": False})
    assert turned_off.status_code == 200, turned_off.text
    refused = await admin.put("/api/v1/builtin-tools/send_email/team-state", json={"enabled": True})
    assert refused.status_code == 422
    accepted = await admin.put("/api/v1/builtin-tools/send_email/team-state", json={"enabled": True, "acknowledge_risk": True})
    assert accepted.status_code == 200, accepted.text
    # "Always confirm" was removed: the field is refused, and a stored value is ignored.
    stale = await admin.put("/api/v1/builtin-tools/send_email/team-state", json={"enabled": True, "always_confirm": True, "acknowledge_risk": True})
    assert stale.status_code == 422
    tools = {item["name"]: item for item in (await member.get("/api/v1/builtin-tools")).json()["tools"]}
    assert tools["send_email"] == {"name": "send_email", "risk": "external_write", "enabled": True}
    assert (await member.put("/api/v1/builtin-tools/web_search/team-state", json={"enabled": False})).status_code == 403
    assert (await admin.put("/api/v1/builtin-tools/skill_read/team-state", json={"enabled": False})).status_code == 404


async def test_team_plugin_personal_state(context):
    admin, member, store = context
    plugins = TeamPluginStore(store.db_path)
    created = await plugins.create(
        team_id="team_a", actor_user_id="u_admin", name="Pack", description="Reads", tool_refs=["web_search"], skill_grants=[]
    )
    assert (await member.put(f"/api/v1/team-capabilities/{created['id']}/my-state", json={"disabled": True})).status_code == 200
    assert (await member.get("/api/v1/builtin-plugins")).json()["team_plugins_my_disabled"] == [created["id"]]
    await plugins.set_enabled(team_id="team_a", plugin_id=created["id"], enabled=False, expected_row_version=created["row_version"])
    restored = await member.put(f"/api/v1/team-capabilities/{created['id']}/my-state", json={"disabled": False})
    assert restored.status_code == 409
    assert (await member.put("/api/v1/team-capabilities/tcap_missing/my-state", json={"disabled": True})).status_code == 404


async def test_impact_preview_is_admin_only_and_runs_on_the_real_schema(context):
    admin, member, _store = context
    assert (await member.get("/api/v1/resources/impact", params={"kind": "builtin_tool", "id": "send_email"})).status_code == 403
    response = await admin.get("/api/v1/resources/impact", params={"kind": "builtin_tool", "id": "send_email"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "kind": "builtin_tool",
        "id": "send_email",
        "system_agents": [{"name": "lead"}],
        "team_agents": [],
        "scheduled_tasks": [],
    }
    assert (await admin.get("/api/v1/resources/impact", params={"kind": "builtin_plugin", "id": "nope"})).status_code == 404
    assert (await admin.get("/api/v1/resources/impact", params={"kind": "skill", "id": "x"})).status_code == 422
