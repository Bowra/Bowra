from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import aiosqlite
import jwt as jwt_lib
import pytest
from bowhead_core.chart_style import (
    KEY_CHART_ACCESSIBILITY_PREFERENCES,
    KEY_TEAM_CHART_STYLE_POLICY,
    user_team_chart_style_selection_key,
)
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.theme_config import KEY_APPEARANCE_THEME_CONFIG, KEY_TEAM_THEME_POLICY
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.conditional_headers import ConditionalHeaderCanonicalizationMiddleware
from bowhead_gateway.routes import auth as auth_routes
from bowhead_gateway.routes import team_settings, theme, user_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "theme-policy-test-secret-long-enough-for-hs256"
NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def _jwt(user_id: str) -> str:
    return jwt_lib.encode(
        {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600, "pwd_v": 0},
        SECRET,
        algorithm="HS256",
    )


def _as(user: User) -> dict[str, str]:
    return {"bowhead_jwt": _jwt(user.id)}


@pytest.fixture
async def app_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    auth_routes._RATE_BUCKET.clear()

    db_path = tmp_path / "theme-policy.db"
    users = UserStore(db_path)
    await users.initialize()
    user_settings_store = UserSettingsStore(db_path)
    await user_settings_store.init()
    team_settings_store = TeamSettingsStore(db_path)
    await team_settings_store.init()

    team = await users.create_team(name="Theme Team", max_seats=10)
    creator = await users.create_user(team_id=team.id, email="creator@theme.example", password="Passw0rd!")
    member = await users.create_user(team_id=team.id, email="member@theme.example", password="Passw0rd!")
    await users.set_team_creator(team.id, creator.id)

    app = FastAPI()
    # Production mounts this (main.py): If-Match W/ stripped before route reads it. Otherwise tests pass, but every production write returns 409.
    app.add_middleware(ConditionalHeaderCanonicalizationMiddleware)
    app.state.user_store = users
    app.state.user_settings_store = user_settings_store
    app.state.team_settings_store = team_settings_store
    app.state.deployment_mode = "saas"
    app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="dev_user")
    app.include_router(user_settings.router)
    app.include_router(team_settings.router)
    app.include_router(theme.router)

    return SimpleNamespace(
        app=app,
        users=users,
        user_settings=user_settings_store,
        team_settings=team_settings_store,
        team=team,
        creator=creator,
        member=member,
    )


@pytest.fixture
def client(app_ctx) -> TestClient:
    return TestClient(app_ctx.app)


async def _audit_rows(db_path) -> list[tuple[str, str]]:
    async with (
        aiosqlite.connect(db_path) as db,
        db.execute(
            "SELECT action, payload_json FROM admin_audit_log ORDER BY ts ASC",
        ) as cur,
    ):
        return await cur.fetchall()


async def test_effective_theme_resolves_team_lock_over_sparse_user_config(client, app_ctx) -> None:
    await app_ctx.team_settings.set(
        app_ctx.team.id,
        KEY_TEAM_THEME_POLICY,
        ('{"version":1,"defaults":{"colorProfile":{"type":"preset","id":"ocean"}},"locks":{"colorProfile":true},"brand":{"name":"Acme"}}'),
        updated_by=app_ctx.creator.id,
    )
    await app_ctx.user_settings.set(
        app_ctx.member.id,
        KEY_APPEARANCE_THEME_CONFIG,
        ('{"version":1,"colorProfile":{"type":"preset","id":"plum"},"chartPalette":"colorblind-safe","typeScale":"large"}'),
    )

    response = client.get("/api/v1/theme/effective", cookies=_as(app_ctx.member))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["effective"]["colorProfile"] == {"type": "preset", "id": "ocean"}
    assert body["effective"]["chartPalette"] == "colorblind-safe"
    assert body["effective"]["typeScale"] == "large"
    assert body["locks"] == {"colorProfile": True}
    assert body["source"]["colorProfile"] == "team_lock"
    assert body["source"]["chartPalette"] == "user"
    assert body["userConfig"] == {
        "version": 1,
        "colorProfile": {"type": "preset", "id": "plum"},
        "chartPalette": "colorblind-safe",
        "typeScale": "large",
    }
    assert body["team"] == {"brandName": "Acme"}


async def test_effective_theme_ignores_corrupted_rows_without_500(client, app_ctx) -> None:
    await app_ctx.team_settings.set(
        app_ctx.team.id,
        KEY_TEAM_THEME_POLICY,
        "not-json",
        updated_by=app_ctx.creator.id,
    )
    await app_ctx.user_settings.set(app_ctx.member.id, KEY_APPEARANCE_THEME_CONFIG, "not-json")

    response = client.get("/api/v1/theme/effective", cookies=_as(app_ctx.member))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["effective"]["colorProfile"] == {"type": "preset", "id": "bowhead"}
    assert body["effective"]["chartPalette"] == "default"
    assert body["locks"] == {}
    assert body["userConfig"] == {"version": 1}


async def test_team_chart_style_policy_requires_admin_and_current_etag(
    client,
    app_ctx,
) -> None:
    initial = client.get(
        "/api/v1/team-settings/chart-style-policy",
        cookies=_as(app_ctx.creator),
    )
    assert initial.status_code == 200, initial.text
    assert initial.json()["policy"]["defaultPresetId"] == "clean-blue"  # product default since 1.5.0
    etag = initial.json()["etag"]
    policy = {
        "version": 1,
        "defaultPresetId": "clean-blue",
        "enabledPresetIds": ["bowhead-editorial", "clean-blue"],
    }

    member_read = client.get(
        "/api/v1/team-settings/chart-style-policy",
        cookies=_as(app_ctx.member),
    )
    assert member_read.status_code == 403

    member_write = client.put(
        "/api/v1/team-settings/chart-style-policy",
        json=policy,
        headers={"If-Match": etag},
        cookies=_as(app_ctx.member),
    )
    assert member_write.status_code == 403
    missing_precondition = client.put(
        "/api/v1/team-settings/chart-style-policy",
        json=policy,
        cookies=_as(app_ctx.creator),
    )
    assert missing_precondition.status_code == 428

    saved = client.put(
        "/api/v1/team-settings/chart-style-policy",
        json=policy,
        headers={"If-Match": etag},
        cookies=_as(app_ctx.creator),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["policy"] == policy
    assert saved.json()["etag"] != etag
    assert await app_ctx.team_settings.get(app_ctx.team.id, KEY_TEAM_CHART_STYLE_POLICY) == (
        '{"version":1,"defaultPresetId":"clean-blue","enabledPresetIds":["bowhead-editorial","clean-blue"]}'
    )

    stale = client.put(
        "/api/v1/team-settings/chart-style-policy",
        json={**policy, "defaultPresetId": "bowhead-editorial"},
        headers={"If-Match": etag},
        cookies=_as(app_ctx.creator),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"]["code"] == "chart_style_policy_conflict"


async def test_v1_chart_style_policy_route_projects_but_does_not_overwrite_v2(
    client,
    app_ctx,
) -> None:
    raw_v2 = (
        '{"version":2,"defaultStyleId":"tcs_0123456789abcdef01234567",'
        '"enabledStyleIds":["tcs_0123456789abcdef01234567","clean-blue"],'
        '"fallbackBuiltInPresetId":"clean-blue"}'
    )
    await app_ctx.team_settings.set(
        app_ctx.team.id,
        KEY_TEAM_CHART_STYLE_POLICY,
        raw_v2,
        updated_by=app_ctx.creator.id,
    )

    current = client.get(
        "/api/v1/team-settings/chart-style-policy",
        cookies=_as(app_ctx.creator),
    )
    assert current.status_code == 200, current.text
    assert current.json()["policy"] == {
        "version": 1,
        "defaultPresetId": "clean-blue",
        "enabledPresetIds": ["bowhead-editorial", "clean-blue", "analytical-neutral"],
    }

    overwrite = client.put(
        "/api/v1/team-settings/chart-style-policy",
        json={
            "version": 1,
            "defaultPresetId": "bowhead-editorial",
            "enabledPresetIds": ["bowhead-editorial", "clean-blue"],
        },
        headers={"If-Match": current.json()["etag"]},
        cookies=_as(app_ctx.creator),
    )
    assert overwrite.status_code == 409
    assert overwrite.json()["detail"]["error"]["code"] == "chart_style_policy_requires_newer_client"
    assert await app_ctx.team_settings.get(app_ctx.team.id, KEY_TEAM_CHART_STYLE_POLICY) == raw_v2


async def test_personal_chart_style_settings_are_independent_from_legacy_appearance(
    client,
    app_ctx,
) -> None:
    await app_ctx.user_settings.set(
        app_ctx.member.id,
        KEY_APPEARANCE_THEME_CONFIG,
        '{"version":1,"chartPalette":"colorblind-safe"}',
    )

    initial = client.get("/api/v1/theme/effective", cookies=_as(app_ctx.member))
    assert initial.status_code == 200, initial.text
    assert initial.json()["chartStyle"]["accessibility"] == {
        "contrast": "standard",
        "colorVision": "standard",
        "source": {"contrast": "product", "colorVision": "product"},
    }

    accessibility = client.put(
        "/api/v1/theme/chart-style/accessibility",
        json={"contrast": "high"},
        cookies=_as(app_ctx.member),
    )
    selection = client.put(
        "/api/v1/theme/chart-style/selection",
        json={"presetId": "analytical-neutral"},
        cookies=_as(app_ctx.member),
    )
    assert accessibility.status_code == 200, accessibility.text
    assert selection.status_code == 200, selection.text
    assert (
        await app_ctx.user_settings.get(
            app_ctx.member.id,
            KEY_CHART_ACCESSIBILITY_PREFERENCES,
        )
        == '{"version":1,"contrast":"high"}'
    )
    assert (
        await app_ctx.user_settings.get(
            app_ctx.member.id,
            user_team_chart_style_selection_key(app_ctx.team.id),
        )
        == '{"version":1,"presetId":"analytical-neutral"}'
    )

    effective = client.get("/api/v1/theme/effective", cookies=_as(app_ctx.member))
    assert effective.status_code == 200, effective.text
    style = effective.json()["chartStyle"]
    assert style["preset"]["id"] == "analytical-neutral"
    assert style["selection"]["source"] == "user"
    assert style["accessibility"]["contrast"] == "high"
    assert style["accessibility"]["source"]["contrast"] == "user"
    assert style["accessibility"]["colorVision"] == "standard"


async def test_member_cannot_select_style_disabled_by_team(client, app_ctx) -> None:
    await app_ctx.team_settings.set(
        app_ctx.team.id,
        KEY_TEAM_CHART_STYLE_POLICY,
        '{"version":1,"defaultPresetId":"clean-blue","enabledPresetIds":["bowhead-editorial","clean-blue"]}',
        updated_by=app_ctx.creator.id,
    )

    response = client.put(
        "/api/v1/theme/chart-style/selection",
        json={"presetId": "analytical-neutral"},
        cookies=_as(app_ctx.member),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "chart_style_preset_disabled_by_team"
