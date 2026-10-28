from __future__ import annotations

import json
import time
from types import SimpleNamespace

import jwt as jwt_lib
import pytest
from bowhead_core.personal_theme_models import (
    KEY_APPEARANCE_THEME_CONFIG_V3,
    KEY_APPEARANCE_THEME_UNDO_V3,
)
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.routes import user_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "theme-config-test-secret-long-enough-for-hs256"


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

    db_path = tmp_path / "theme-user-settings.db"
    users = UserStore(db_path)
    await users.initialize()
    settings = UserSettingsStore(db_path)
    await settings.init()

    team = await users.create_team(name="Theme Team", max_seats=10)
    member = await users.create_user(team_id=team.id, email="member@theme.example", password="Passw0rd!")
    other = await users.create_user(team_id=team.id, email="other@theme.example", password="Passw0rd!")

    app = FastAPI()
    app.state.user_store = users
    app.state.user_settings_store = settings
    app.state.deployment_mode = "saas"
    app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="dev_user")
    app.include_router(user_settings.router)

    return SimpleNamespace(app=app, settings=settings, member=member, other=other)


@pytest.fixture
def client(app_ctx) -> TestClient:
    return TestClient(app_ctx.app)


async def test_put_appearance_theme_config_normalizes_sparse_payload(client, app_ctx) -> None:
    payload = {
        "value": json.dumps(
            {
                "version": 1,
                "chartPalette": "colorblind-safe",
                "mode": "dark",
                "typeScale": "large",
            }
        ),
    }

    response = client.put(
        "/api/v1/user-settings/appearance_theme_config",
        json=payload,
        cookies=_as(app_ctx.member),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "value": '{"version":1,"mode":"dark","typeScale":"large","chartPalette":"colorblind-safe"}',
    }
    assert await app_ctx.settings.get(app_ctx.member.id, "appearance_theme_config") == (
        '{"version":1,"mode":"dark","typeScale":"large","chartPalette":"colorblind-safe"}'
    )

    listed = client.get("/api/v1/user-settings", cookies=_as(app_ctx.member))
    assert listed.status_code == 200, listed.text
    assert listed.json()["settings"] == {
        "appearance_theme_config": '{"version":1,"mode":"dark","typeScale":"large","chartPalette":"colorblind-safe"}',
    }


async def test_appearance_theme_config_rejects_unknown_fields(client, app_ctx) -> None:
    response = client.put(
        "/api/v1/user-settings/appearance_theme_config",
        json={"value": json.dumps({"version": 1, "backgroundColor": "#fff"})},
        cookies=_as(app_ctx.member),
    )

    assert response.status_code == 400
    assert await app_ctx.settings.get(app_ctx.member.id, "appearance_theme_config") is None


async def test_user_settings_delete_rejects_unknown_key(client, app_ctx) -> None:
    response = client.delete("/api/v1/user-settings/not_a_setting", cookies=_as(app_ctx.member))

    assert response.status_code == 400


async def test_appearance_theme_config_is_user_scoped(client, app_ctx) -> None:
    response = client.put(
        "/api/v1/user-settings/appearance_theme_config",
        json={"value": '{"version":1,"mode":"dark"}'},
        cookies=_as(app_ctx.member),
    )
    assert response.status_code == 200, response.text

    member_settings = client.get("/api/v1/user-settings", cookies=_as(app_ctx.member))
    other_settings = client.get("/api/v1/user-settings", cookies=_as(app_ctx.other))

    assert member_settings.status_code == 200, member_settings.text
    assert other_settings.status_code == 200, other_settings.text
    assert member_settings.json()["settings"] == {
        "appearance_theme_config": '{"version":1,"mode":"dark"}',
    }
    assert other_settings.json()["settings"] == {}


async def test_internal_personal_theme_keys_are_hidden_from_generic_list(client, app_ctx) -> None:
    await app_ctx.settings.set(app_ctx.member.id, KEY_APPEARANCE_THEME_CONFIG_V3, "secret-config")
    await app_ctx.settings.set(app_ctx.member.id, KEY_APPEARANCE_THEME_UNDO_V3, "secret-receipt")
    await app_ctx.settings.set(app_ctx.member.id, "ui_language", "zh-CN")

    response = client.get("/api/v1/user-settings", cookies=_as(app_ctx.member))

    assert response.status_code == 200, response.text
    assert response.json()["settings"] == {"ui_language": "zh-CN"}


@pytest.mark.parametrize(
    "key",
    [KEY_APPEARANCE_THEME_CONFIG_V3, KEY_APPEARANCE_THEME_UNDO_V3],
)
async def test_generic_user_settings_cannot_write_or_delete_internal_theme_keys(
    client,
    app_ctx,
    key: str,
) -> None:
    await app_ctx.settings.set(app_ctx.member.id, key, "service-owned")

    put_response = client.put(
        f"/api/v1/user-settings/{key}",
        json={"value": "attacker-owned"},
        cookies=_as(app_ctx.member),
    )
    delete_response = client.delete(
        f"/api/v1/user-settings/{key}",
        cookies=_as(app_ctx.member),
    )

    assert put_response.status_code == 400
    assert delete_response.status_code == 400
    assert await app_ctx.settings.get(app_ctx.member.id, key) == "service-owned"


@pytest.mark.parametrize("style", ["concise", "detailed", "technical", "business"])
async def test_put_answer_style_accepts_every_option_the_settings_page_offers(
    client,
    app_ctx,
    style: str,
) -> None:
    """Accept every answer-style option offered by the settings interface.

    Keep the settings allowlist aligned without rejecting persisted legacy values.
    """
    response = client.put(
        "/api/v1/user-settings/answer_style",
        json={"value": style},
        cookies=_as(app_ctx.member),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"value": style}
    assert await app_ctx.settings.get(app_ctx.member.id, "answer_style") == style

    listed = client.get("/api/v1/user-settings", cookies=_as(app_ctx.member))
    assert listed.status_code == 200, listed.text
    assert listed.json()["settings"]["answer_style"] == style


async def test_german_output_language_requires_feature_flag(client, app_ctx, monkeypatch) -> None:
    monkeypatch.delenv("FEATURE_GERMAN_AI_OUTPUT", raising=False)
    response = client.put(
        "/api/v1/user-settings/output_language",
        json={"value": "de-DE"},
        cookies=_as(app_ctx.member),
        headers={"Accept-Language": "de-DE,de;q=0.9"},
    )

    assert response.status_code == 400
    assert "nicht aktiviert" in response.json()["detail"]
    assert await app_ctx.settings.get(app_ctx.member.id, "output_language") is None

    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    enabled = client.put(
        "/api/v1/user-settings/output_language",
        json={"value": "de-DE"},
        cookies=_as(app_ctx.member),
    )
    assert enabled.status_code == 200, enabled.text
    assert await app_ctx.settings.get(app_ctx.member.id, "output_language") == "de-DE"
