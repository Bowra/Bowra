"""Team agent run limits settings: member read, admin write, ranges, concurrency and the feature switch."""

from __future__ import annotations

import pytest
from bowhead_gateway.routes import team_settings
from fastapi.testclient import TestClient
from test_team_chart_styles_routes import _as
from test_team_chart_styles_routes import app_ctx as _app_ctx

app_ctx = _app_ctx
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
URL = "/api/v1/team-settings/agent-run-limits"


@pytest.fixture
def client(app_ctx, monkeypatch):
    app_ctx.app.include_router(team_settings.router)
    return TestClient(app_ctx.app)


def _body(version: str, **patch):
    return {"turn_time_limit_s": 3600, "step_limit": None, "tool_call_limit": 500, "expected_version": version, **patch}


async def test_only_owners_and_admins_read_and_write(client, app_ctx):
    assert client.get(URL, cookies=_as(app_ctx.member)).status_code == 403
    read = client.get(URL, cookies=_as(app_ctx.admin))
    assert read.status_code == 200
    view = read.json()
    assert (view["turn_time_limit_s"], view["step_limit"], view["tool_call_limit"]) == (1800, None, 300)
    assert view["is_default"] is True and view["wrap_up_reserve_s"] == 300
    assert view["ranges"]["turn_time_limit_s"] == {"min": 600, "max": 36000}

    assert client.put(URL, cookies=_as(app_ctx.member), json=_body(view["version"])).status_code == 403
    saved = client.put(URL, cookies=_as(app_ctx.admin), json=_body(view["version"], step_limit=200))
    assert saved.status_code == 200
    assert (saved.json()["turn_time_limit_s"], saved.json()["step_limit"]) == (3600, 200)
    assert client.get(URL, cookies=_as(app_ctx.owner)).json()["step_limit"] == 200
    assert client.get(URL, cookies=_as(app_ctx.other_owner)).json()["is_default"] is True


async def test_unlimited_steps_are_stored_explicitly(client, app_ctx):
    version = client.get(URL, cookies=_as(app_ctx.owner)).json()["version"]
    assert client.put(URL, cookies=_as(app_ctx.owner), json=_body(version, step_limit=None)).status_code == 200
    stored = await app_ctx.app.state.team_settings_store.get(app_ctx.team.id, "agent_run_limits")
    assert '"step_limit":null' in stored


async def test_system_policy_is_read_only_and_reset_keeps_the_ceiling(client, app_ctx, monkeypatch):
    from bowhead_core.agent_run_limits import get_runtime_controls

    monkeypatch.setenv("BOWHEAD_AGENT_MAX_STEPS", "100")
    get_runtime_controls.cache_clear()
    try:
        first = client.get(URL, cookies=_as(app_ctx.owner)).json()
        assert first["step_limit"] is None
        assert first["runtime_controls"]["effective_step_limit"] == 100
        saved = client.put(URL, cookies=_as(app_ctx.owner), json=_body(first["version"], step_limit=200)).json()
        assert saved["step_limit"] == 200
        assert saved["runtime_controls"]["effective_step_limit"] == 100
        denied = client.put(URL, cookies=_as(app_ctx.owner), json=_body(saved["version"], runtime_controls={"system_step_limit": None}))
        assert denied.status_code == 422
        reset = client.delete(URL, cookies=_as(app_ctx.owner)).json()
        assert reset["step_limit"] is None
        assert reset["runtime_controls"]["effective_step_limit"] == 100
        assert await app_ctx.app.state.team_settings_store.get(app_ctx.team.id, "agent_run_limits") is None
    finally:
        get_runtime_controls.cache_clear()


@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"turn_time_limit_s": 599}, "turn_time_limit_s"),
        ({"turn_time_limit_s": 36001}, "turn_time_limit_s"),
        ({"step_limit": 0}, "step_limit"),
        ({"tool_call_limit": 0}, "tool_call_limit"),
        ({"tool_call_limit": True}, "tool_call_limit"),
        ({"model_call_time_limit_s": 29}, "model_call_time_limit_s"),
        ({"model_call_time_limit_s": 36001}, "model_call_time_limit_s"),
        ({"model_call_time_limit_s": True}, "model_call_time_limit_s"),
    ],
)
async def test_out_of_range_values_name_the_field_and_range(client, app_ctx, patch, field):
    version = client.get(URL, cookies=_as(app_ctx.owner)).json()["version"]
    response = client.put(URL, cookies=_as(app_ctx.owner), json=_body(version, **patch))
    assert response.status_code == 422
    error = response.json()["detail"]["error"]
    assert error["code"] == "agent_run_limits_invalid" and error["field"] == field
    assert client.get(URL, cookies=_as(app_ctx.owner)).json()["is_default"] is True


async def test_stale_version_is_rejected(client, app_ctx):
    version = client.get(URL, cookies=_as(app_ctx.owner)).json()["version"]
    assert client.put(URL, cookies=_as(app_ctx.owner), json=_body(version)).status_code == 200
    stale = client.put(URL, cookies=_as(app_ctx.admin), json=_body(version, tool_call_limit=900))
    assert stale.status_code == 409 and stale.json()["detail"]["error"]["code"] == "agent_run_limits_conflict"


async def test_delete_restores_defaults(client, app_ctx):
    version = client.get(URL, cookies=_as(app_ctx.owner)).json()["version"]
    client.put(URL, cookies=_as(app_ctx.owner), json=_body(version))
    assert client.delete(URL, cookies=_as(app_ctx.member)).status_code == 403
    restored = client.delete(URL, cookies=_as(app_ctx.owner))
    assert restored.status_code == 200 and restored.json()["is_default"] is True


async def test_model_call_deadline_persists_and_resets_to_follow_turn(client, app_ctx):
    initial = client.get(URL, cookies=_as(app_ctx.owner)).json()
    assert initial["model_call_time_limit_s"] is None
    assert initial["ranges"]["model_call_time_limit_s"] == {"min": 30, "max": 36000}
    saved = client.put(URL, cookies=_as(app_ctx.owner), json=_body(initial["version"], model_call_time_limit_s=900))
    assert saved.status_code == 200
    assert client.get(URL, cookies=_as(app_ctx.owner)).json()["model_call_time_limit_s"] == 900
    stored = await app_ctx.app.state.team_settings_store.get(app_ctx.team.id, "agent_run_limits")
    assert '"model_call_time_limit_s":900' in stored
    legacy = client.put(URL, cookies=_as(app_ctx.owner), json=_body(saved.json()["version"], tool_call_limit=600))
    assert legacy.status_code == 200 and legacy.json()["model_call_time_limit_s"] == 900
    automatic = client.put(URL, cookies=_as(app_ctx.owner), json=_body(legacy.json()["version"], model_call_time_limit_s=None))
    assert automatic.status_code == 200 and automatic.json()["model_call_time_limit_s"] is None
    assert client.delete(URL, cookies=_as(app_ctx.owner)).json()["model_call_time_limit_s"] is None
