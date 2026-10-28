"""Team administrators configure sandbox and HTML limits with atomic version checks."""

import aiosqlite
import pytest
from bowhead_core.entitlements import CommunityResolver
from bowhead_gateway.routes import team_settings
from fastapi.testclient import TestClient
from test_team_chart_styles_routes import _as
from test_team_chart_styles_routes import app_ctx as _app_ctx

app_ctx = _app_ctx
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
URL = "/api/v1/team-settings/execution-limits"


@pytest.fixture
def client(app_ctx, monkeypatch):
    monkeypatch.setenv("FEATURE_AGENT_RUN_LIMITS", "0")
    monkeypatch.setenv("BOWHEAD_SANDBOX_MAX_TIMEOUT", "600")
    monkeypatch.delenv("BOWHEAD_SKILL_WALL_TIMEOUT_S", raising=False)
    app_ctx.app.include_router(team_settings.router)
    return TestClient(app_ctx.app)


def body(version="default", **patch):
    return {
        "expected_version": version,
        "limits": {"sandbox_default_timeout_s": 240, "sandbox_max_timeout_s": 500, "html_max_bytes": 8388608, **patch},
    }


async def test_admin_configuration_team_isolation_and_reset(client, app_ctx):
    assert client.get(URL, cookies=_as(app_ctx.member)).status_code == 403
    assert client.put(URL, cookies=_as(app_ctx.member), json=body()).status_code == 403
    view = client.get(URL, cookies=_as(app_ctx.owner))
    assert view.status_code == 200 and view.json()["is_default"] is True
    assert view.headers["cache-control"] == "private, no-store"
    saved = client.put(URL, cookies=_as(app_ctx.admin), json=body())
    assert saved.status_code == 200 and saved.json()["sandbox_default_timeout_s"] == 240
    assert client.get(URL, cookies=_as(app_ctx.owner)).json()["html_max_bytes"] == 8388608
    assert client.get(URL, cookies=_as(app_ctx.other_owner)).json()["is_default"] is True
    assert client.put(URL, cookies=_as(app_ctx.owner), json=body()).status_code == 409
    reset = client.put(URL, cookies=_as(app_ctx.owner), json={"limits": None, "expected_version": saved.json()["version"]})
    assert reset.status_code == 200 and reset.json()["is_default"] is True
    assert reset.json()["sandbox_default_timeout_s"] == reset.json()["defaults"]["sandbox_default_timeout_s"]
    stale = client.put(URL, cookies=_as(app_ctx.owner), json={"limits": None, "expected_version": saved.json()["version"]})
    assert stale.status_code == 409


@pytest.mark.parametrize(
    "patch,field",
    [
        ({"sandbox_default_timeout_s": 501}, "sandbox_default_timeout_s"),
        ({"sandbox_max_timeout_s": 601}, "sandbox_max_timeout_s"),
        ({"html_max_bytes": 52428801}, "html_max_bytes"),
        ({"sandbox_default_timeout_s": True}, "sandbox_default_timeout_s"),
    ],
)
async def test_invalid_configuration_is_not_persisted(client, app_ctx, patch, field):
    response = client.put(URL, cookies=_as(app_ctx.owner), json=body(**patch))
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["field"] == field
    assert client.get(URL, cookies=_as(app_ctx.owner)).json()["is_default"] is True


async def test_readonly_team_cannot_save_or_reset(client, app_ctx):
    app_ctx.app.state.entitlements = CommunityResolver(mode="saas", user_store=app_ctx.users)
    async with aiosqlite.connect(app_ctx.users.db_path) as db:
        await db.execute("UPDATE teams SET status='suspended' WHERE id=?", (app_ctx.team.id,))
        await db.commit()
    for payload in (body(), {"limits": None, "expected_version": "default"}):
        response = client.put(URL, cookies=_as(app_ctx.owner), json=payload)
        assert response.status_code == 403
        assert response.json()["detail"]["error"]["code"] == "suspended"
