from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import jwt as jwt_lib
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.effective_theme import DefaultThemeResolver, ResolvedThemeBundle
from bowhead_core.legacy_theme_registry import legacy_m1_compiled_scheme
from bowhead_core.personal_appearance_service import (
    PersonalAppearanceBaseIncompatible,
    PersonalAppearanceService,
)
from bowhead_core.personal_appearance_store import PersonalAppearanceStore
from bowhead_core.personal_theme_models import KEY_APPEARANCE_THEME_CONFIG_V3
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.theme_config import KEY_APPEARANCE_THEME_CONFIG, resolve_effective_theme
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.conditional_headers import (
    ConditionalHeaderCanonicalizationMiddleware,
)
from bowhead_gateway.routes import auth as auth_routes
from bowhead_gateway.routes import theme, user_settings
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

SECRET = "personal-theme-routes-secret-long-enough-for-hs256"


def _jwt(user_id: str) -> str:
    return jwt_lib.encode(
        {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600, "pwd_v": 0},
        SECRET,
        algorithm="HS256",
    )


def _as(user: User) -> dict[str, str]:
    return {"bowhead_jwt": _jwt(user.id)}


class _LegacyResolver:
    async def read_state(self, team_id: str) -> None:
        return None

    async def revision_checksum(self, team_id: str, revision_id: str) -> None:
        return None

    async def resolve(
        self,
        *,
        team_id: str,
        team_policy: dict[str, Any],
        user_config: dict[str, Any],
        allow_custom: bool,
        render_safe_mode: bool,
    ) -> ResolvedThemeBundle:
        resolved = resolve_effective_theme(team_policy, user_config)
        profile_id = resolved["effective"]["colorProfile"]["id"]
        compiled = legacy_m1_compiled_scheme(profile_id)
        resolved["effective"]["colorScheme"] = {
            "tokenSchemaVersion": compiled["tokenSchemaVersion"],
            "light": compiled["light"],
            "dark": compiled["dark"],
            "checksum": compiled["checksum"],
        }
        resolved["schemaVersion"] = 2
        resolved["brand"] = {
            "applicationName": "Bowhead",
            "logoAlt": "Bowhead",
            "logoOnLight": {"kind": "text", "text": "Bowhead"},
            "logoOnDark": {"kind": "text", "text": "Bowhead"},
            "mark": {"kind": "generatedInitial", "text": "B"},
            "favicon": {"kind": "generatedInitial", "text": "B"},
        }
        resolved["team"] = {
            "id": team_id,
            "themeRevision": None,
            "revisionId": None,
            "configurationType": "legacy",
        }
        return ResolvedThemeBundle(
            payload=resolved,
            revision_id="legacy",
            revision_number=0,
            revision_checksum=compiled["checksum"],
        )


@pytest.fixture
async def app_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    auth_routes._RATE_BUCKET.clear()

    db_path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    users = UserStore(db_path)
    await users.initialize()
    user_store = UserSettingsStore(db_path)
    await user_store.init()
    team_store = TeamSettingsStore(db_path)
    await team_store.init()
    personal_store = PersonalAppearanceStore(db_path)
    await personal_store.initialize()

    team_row = await users.create_team(name="Theme Team", max_seats=10)
    member = await users.create_user(
        team_id=team_row.id,
        email="member@personal-theme.example",
        password="Passw0rd!",
    )
    app = FastAPI()
    app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="dev_user")
    app.state.deployment_mode = "saas"
    app.state.user_store = users
    app.state.user_settings_store = user_store
    app.state.team_settings_store = team_store
    app.state.personal_appearance_store = personal_store
    app.state.personal_appearance_service = PersonalAppearanceService(personal_store)
    app.state.effective_theme_resolver = _LegacyResolver()
    app.include_router(user_settings.router)
    app.include_router(theme.router)
    return SimpleNamespace(
        app=app,
        member=member,
        user_store=user_store,
        personal_store=personal_store,
    )


@pytest.fixture
def client(app_ctx) -> TestClient:
    return TestClient(app_ctx.app)


def test_incompatible_personal_theme_base_has_a_distinct_http_error() -> None:
    with pytest.raises(HTTPException) as caught:
        theme._raise_personal_http(PersonalAppearanceBaseIncompatible())

    assert caught.value.status_code == 409
    assert caught.value.detail == {"error": {"code": "personal_theme_base_incompatible"}}


async def test_personal_get_projects_legacy_and_returns_private_etag(client, app_ctx) -> None:
    await app_ctx.user_store.set(
        app_ctx.member.id,
        KEY_APPEARANCE_THEME_CONFIG,
        '{"version":1,"mode":"dark","colorProfile":{"type":"preset","id":"ocean"}}',
    )

    response = client.get("/api/v1/theme/personal", cookies=_as(app_ctx.member))

    assert response.status_code == 200, response.text
    assert response.json()["materialized"] is False
    assert response.json()["overrides"]["accent"]["id"] == "ocean"
    assert response.headers["etag"].startswith('"personal-v3:')
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.json()["registries"]["palette"]["items"][0]["id"] == "saas-blue"
    assert response.json()["registries"]["palette"]["items"][0]["swatch"]["light"]["secondary"] == "#F8FAFC"
    assert response.json()["registries"]["background"]["items"][1]["swatch"]["light"]["primary"] == "#FFFFFF"
    assert await app_ctx.user_store.get(app_ctx.member.id, KEY_APPEARANCE_THEME_CONFIG_V3) is None


async def test_personal_patch_requires_etag_and_materializes_normalized_color(client, app_ctx) -> None:
    current = client.get("/api/v1/theme/personal", cookies=_as(app_ctx.member))
    missing = client.patch(
        "/api/v1/theme/personal",
        json={"changes": {"accent": {"kind": "custom", "seed": "#2f6f84"}}},
        cookies=_as(app_ctx.member),
    )
    assert missing.status_code == 428
    assert missing.json()["detail"]["error"]["code"] == "personal_theme_precondition_required"

    saved = client.patch(
        "/api/v1/theme/personal",
        headers={"If-Match": current.headers["etag"]},
        json={"changes": {"accent": {"kind": "custom", "seed": "#2f6f84"}}},
        cookies=_as(app_ctx.member),
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["materialized"] is True
    assert saved.json()["rowVersion"] == 1
    assert saved.json()["overrides"]["accent"]["seed"] == "#2F6F84"
    stale = client.patch(
        "/api/v1/theme/personal",
        headers={"If-Match": current.headers["etag"]},
        json={"changes": {"mode": "dark"}},
        cookies=_as(app_ctx.member),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"]["code"] == "personal_theme_conflict"

    invalid = client.patch(
        "/api/v1/theme/personal",
        headers={"If-Match": saved.headers["etag"], "Content-Type": "application/json"},
        content='{"changes":{},"user_id":"someone-else"}',
        cookies=_as(app_ctx.member),
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["error"]["code"] == "personal_theme_invalid_json"


async def test_compile_reset_retry_and_undo_contract(client, app_ctx) -> None:
    current = client.get("/api/v1/theme/personal", cookies=_as(app_ctx.member))
    compiled = client.post(
        "/api/v1/theme/personal/compile",
        json={
            "baseEtag": current.headers["etag"],
            "candidate": {"background": {"kind": "preset", "registryVersion": 1, "id": "pure-white"}},
            "candidateId": "picker-1",
        },
        cookies=_as(app_ctx.member),
    )
    assert compiled.status_code == 200, compiled.text
    assert compiled.json()["candidateId"] == "picker-1"
    assert compiled.json()["colorScheme"]["light"]["background"] == "#FFFFFF"

    reset = client.delete(
        "/api/v1/theme/personal",
        headers={"If-Match": current.headers["etag"], "Idempotency-Key": "reset-route-1"},
        cookies=_as(app_ctx.member),
    )
    retry = client.delete(
        "/api/v1/theme/personal",
        headers={"If-Match": current.headers["etag"], "Idempotency-Key": "reset-route-1"},
        cookies=_as(app_ctx.member),
    )
    assert reset.status_code == 200, reset.text
    assert retry.status_code == 200, retry.text
    assert retry.json()["undo"] == reset.json()["undo"]
    assert retry.headers["etag"] == reset.headers["etag"]

    undo = client.post(
        "/api/v1/theme/personal/undo",
        headers={"If-Match": reset.headers["etag"]},
        json={"resetId": reset.json()["undo"]["resetId"]},
        cookies=_as(app_ctx.member),
    )
    assert undo.status_code == 200, undo.text
    assert undo.json()["materialized"] is True
    assert undo.json()["pendingUndo"] is None


async def test_effective_wire_v2_uses_personal_overlay_and_keeps_chart_in_legacy_domain(client, app_ctx) -> None:
    await app_ctx.user_store.set(
        app_ctx.member.id,
        KEY_APPEARANCE_THEME_CONFIG,
        '{"version":1,"chartPalette":"colorblind-safe"}',
    )
    current = client.get("/api/v1/theme/personal", cookies=_as(app_ctx.member))
    saved = client.patch(
        "/api/v1/theme/personal",
        headers={"If-Match": current.headers["etag"]},
        json={
            "changes": {
                "accent": {"kind": "custom", "seed": "#2F6F84"},
                "background": {"kind": "preset", "registryVersion": 1, "id": "pure-white"},
            }
        },
        cookies=_as(app_ctx.member),
    )
    assert saved.status_code == 200, saved.text

    effective = client.get("/api/v1/theme/effective", cookies=_as(app_ctx.member))

    assert effective.status_code == 200, effective.text
    payload = effective.json()
    assert payload["schemaVersion"] == 2
    assert payload["effective"]["colorScheme"]["light"]["background"] == "#FFFFFF"
    assert payload["locks"] == {}
    assert payload["userConfig"] == {
        "version": 1,
        "colorProfile": {"type": "preset", "id": "bowhead"},
        "chartPalette": "colorblind-safe",
    }
    assert "degradations" not in payload
    assert "registries" not in payload


async def test_materialized_v3_allows_only_legacy_chart_change(client, app_ctx) -> None:
    await app_ctx.user_store.set(
        app_ctx.member.id,
        KEY_APPEARANCE_THEME_CONFIG,
        '{"version":1,"mode":"dark","chartPalette":"default"}',
    )
    current = client.get("/api/v1/theme/personal", cookies=_as(app_ctx.member))
    saved = client.patch(
        "/api/v1/theme/personal",
        headers={"If-Match": current.headers["etag"]},
        json={"changes": {"mode": "light"}},
        cookies=_as(app_ctx.member),
    )
    assert saved.status_code == 200, saved.text

    chart_only = client.put(
        f"/api/v1/user-settings/{KEY_APPEARANCE_THEME_CONFIG}",
        json={"value": '{"version":1,"mode":"dark","chartPalette":"high-contrast"}'},
        cookies=_as(app_ctx.member),
    )
    assert chart_only.status_code == 200, chart_only.text
    non_chart = client.put(
        f"/api/v1/user-settings/{KEY_APPEARANCE_THEME_CONFIG}",
        json={"value": '{"version":1,"mode":"light","chartPalette":"high-contrast"}'},
        cookies=_as(app_ctx.member),
    )
    deleted = client.delete(
        f"/api/v1/user-settings/{KEY_APPEARANCE_THEME_CONFIG}",
        cookies=_as(app_ctx.member),
    )
    assert non_chart.status_code == 409
    assert non_chart.json()["detail"]["error"]["code"] == "appearance_v3_managed"
    assert deleted.status_code == 409


async def test_weakened_etag_still_drives_the_real_personal_route(app_ctx) -> None:
    """A compressing CDN weakens our strong ETag; the real route must still accept it.

    Regression cover for the customer outage in
    ``docs/sprints/brand-appearance-repair-2026-09-09/spike.md`` §2 (A1/A2): the
    browser echoes ``W/"personal-v3:<sha>"`` back, and both the ``If-Match``
    precondition and the ``If-None-Match`` revalidation compared it byte-for-byte.
    """
    wrapped = TestClient(ConditionalHeaderCanonicalizationMiddleware(app_ctx.app))

    current = wrapped.get("/api/v1/theme/personal", cookies=_as(app_ctx.member))
    assert current.status_code == 200, current.text
    strong = current.headers["etag"]
    weak = f"W/{strong}"

    # Positive control: without the middleware this is the observed production failure.
    bare = TestClient(app_ctx.app).get(
        "/api/v1/theme/personal",
        headers={"If-None-Match": weak},
        cookies=_as(app_ctx.member),
    )
    assert bare.status_code == 200

    revalidated = wrapped.get(
        "/api/v1/theme/personal",
        headers={"If-None-Match": weak},
        cookies=_as(app_ctx.member),
    )
    assert revalidated.status_code == 304

    saved = wrapped.patch(
        "/api/v1/theme/personal",
        headers={"If-Match": weak},
        json={"changes": {"accent": {"kind": "custom", "seed": "#503291"}}},
        cookies=_as(app_ctx.member),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["overrides"]["accent"] == {"kind": "custom", "seed": "#503291"}


async def test_community_personal_appearance_persists_and_renders_without_branding_store(client, app_ctx) -> None:
    app_ctx.app.state.effective_theme_resolver = DefaultThemeResolver()
    current = client.get("/api/v1/theme/personal", cookies=_as(app_ctx.member))
    assert current.status_code == 200, current.text
    saved = client.patch(
        "/api/v1/theme/personal",
        headers={"If-Match": current.headers["etag"]},
        json={"changes": {"mode": "dark", "accent": {"kind": "custom", "seed": "#2f6f84"}}},
        cookies=_as(app_ctx.member),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["overrides"]["accent"]["seed"] == "#2F6F84"
    effective = client.get("/api/v1/theme/effective", cookies=_as(app_ctx.member))
    assert effective.status_code == 200, effective.text
    payload = effective.json()
    assert payload["brand"]["applicationName"] == "Bowra"
    assert payload["brand"]["logoAlt"] == "Bowra"
    assert payload["effective"]["mode"] == "dark"
    assert payload["effective"]["colorScheme"]["light"]["accent"] == "#2F6F84"
    assert await app_ctx.user_store.get(app_ctx.member.id, KEY_APPEARANCE_THEME_CONFIG_V3) is not None
    stale = client.patch(
        "/api/v1/theme/personal",
        headers={"If-Match": current.headers["etag"]},
        json={"changes": {"mode": "light"}},
        cookies=_as(app_ctx.member),
    )
    assert stale.status_code == 409
