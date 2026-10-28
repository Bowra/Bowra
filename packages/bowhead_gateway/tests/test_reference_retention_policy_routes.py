"""Authenticated team settings routes backed by the actual retention store."""

from __future__ import annotations

import sqlite3

import pytest
from bowhead_core.entitlements import CommunityResolver
from bowhead_gateway.routes import team_settings
from fastapi.testclient import TestClient
from test_team_chart_styles_routes import _as
from test_team_chart_styles_routes import app_ctx as _app_ctx

app_ctx = _app_ctx
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
URL = "/api/v1/team-settings/reference-retention"


@pytest.fixture
def client(app_ctx):
    app_ctx.app.include_router(team_settings.router)
    return TestClient(app_ctx.app)


async def test_member_reads_owner_admin_update_and_cross_team_stays_separate(client, app_ctx):
    assert client.get(URL).status_code == 401
    read = client.get(URL, cookies=_as(app_ctx.member))
    assert read.status_code == 200 and read.json() == {"days": 180, "version": 0}
    assert read.headers["cache-control"] == "private, no-store"
    assert client.put(URL, cookies=_as(app_ctx.member), json={"days": 7, "expected_version": 0}).status_code == 403
    for actor, days, old in ((app_ctx.admin, 7, 0), (app_ctx.owner, 30, 1)):
        updated = client.put(URL, cookies=_as(actor), json={"days": days, "expected_version": old})
        assert updated.status_code == 200 and updated.json() == {"days": days, "version": old + 1}
    stale = client.put(URL, cookies=_as(app_ctx.owner), json={"days": 180, "expected_version": 0})
    assert stale.status_code == 409 and stale.json()["detail"]["error"]["code"] == "reference_retention_policy_conflict"
    assert client.get(URL, cookies=_as(app_ctx.other_owner)).json() == {"days": 180, "version": 0}
    with sqlite3.connect(app_ctx.users.db_path) as db:
        assert db.execute("SELECT team_id,days,updated_by FROM team_reference_retention_policies ORDER BY version").fetchall() == [
            (app_ctx.team.id, 7, app_ctx.admin.id),
            (app_ctx.team.id, 30, app_ctx.owner.id),
        ]


@pytest.mark.parametrize("patch", [{"days": 0}, {"days": 181}, {"days": True}, {"days": "7"}, {"expected_version": True}, {"team_id": "other"}])
async def test_wire_rejects_noninteger_and_scope_override(client, app_ctx, patch):
    response = client.put(URL, cookies=_as(app_ctx.owner), json={"days": 7, "expected_version": 0, **patch})
    assert response.status_code == 422
    assert client.get(URL, cookies=_as(app_ctx.owner)).json() == {"days": 180, "version": 0}


async def test_current_role_is_rechecked_inside_writer_transaction(client, app_ctx, monkeypatch):
    real_write = team_settings.set_retention_policy

    async def demote_then_write(*args, **kwargs):
        with sqlite3.connect(app_ctx.users.db_path) as db:
            db.execute("UPDATE team_members SET role='member' WHERE team_id=? AND user_id=?", (app_ctx.team.id, app_ctx.admin.id))
        return await real_write(*args, **kwargs)

    monkeypatch.setattr(team_settings, "set_retention_policy", demote_then_write)
    result = client.put(URL, cookies=_as(app_ctx.admin), json={"days": 7, "expected_version": 0})
    assert result.status_code == 403 and result.json()["detail"]["error"]["code"] == "reference_retention_policy_forbidden"
    with sqlite3.connect(app_ctx.users.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_reference_retention_policies").fetchone() == (0,)


async def test_license_readonly_blocks_policy_changes(client, app_ctx):
    app_ctx.app.state.entitlements = CommunityResolver(mode="saas", user_store=app_ctx.users)
    with sqlite3.connect(app_ctx.users.db_path) as db:
        db.execute("UPDATE teams SET status='suspended' WHERE id=?", (app_ctx.team.id,))
    response = client.put(URL, cookies=_as(app_ctx.owner), json={"days": 7, "expected_version": 0})
    assert response.status_code == 403 and response.json()["detail"]["error"]["code"] == "suspended"
