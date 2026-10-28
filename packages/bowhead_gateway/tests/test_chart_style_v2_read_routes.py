from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import aiosqlite
import jwt as jwt_lib
import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.chart_style import (
    KEY_TEAM_CHART_STYLE_POLICY,
    user_team_chart_style_selection_key,
)
from bowhead_core.chart_style_models import sha256_json
from bowhead_core.chart_style_read_service import ChartStyleReadService
from bowhead_core.chart_style_store import ChartStyleStore
from bowhead_core.chart_style_uow import ChartStyleUnitOfWork
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.routes import auth as auth_routes
from bowhead_gateway.routes import team_settings, theme
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_core import migrations

SECRET = "chart-style-v2-read-test-secret-long-enough"
NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)
V2_HEADER = {"X-Bowhead-Chart-Style-Contract": "2"}


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(migrations.__file__).resolve().parent),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    return config


def _jwt(user_id: str) -> str:
    return jwt_lib.encode(
        {
            "sub": user_id,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "pwd_v": 0,
        },
        SECRET,
        algorithm="HS256",
    )


def _as(user: User) -> dict[str, str]:
    return {"bowhead_jwt": _jwt(user.id)}


def _source(name: str = "Operations calm") -> dict:
    return {
        "schemaVersion": 2,
        "name": name,
        "description": "Presentation only",
        "overrides": {
            "schemaVersion": 2,
            "line": {"curve": "straight"},
            "components": {"tooltip": "disabled"},
        },
    }


class _ActualSurfaceResolver:
    async def resolve(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(
            payload={
                "effective": {
                    "colorScheme": {
                        "light": {"surface": "#F5E8DC"},
                        "dark": {"surface": "#242A2E"},
                        "checksum": "actual-team-scheme",
                    }
                }
            },
            revision_id="theme-rev-1",
            revision_checksum="theme-checksum-1",
        )


@pytest.fixture
async def app_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    auth_routes._RATE_BUCKET.clear()

    db_path = tmp_path / "chart-style-v2-read.db"
    command.upgrade(_config(db_path), "head")
    users = UserStore(db_path)
    await users.initialize()
    user_settings = UserSettingsStore(db_path)
    await user_settings.init()
    team_settings_store = TeamSettingsStore(db_path)
    await team_settings_store.init()
    chart_store = ChartStyleStore(db_path)
    chart_uow = ChartStyleUnitOfWork(db_path, now_fn=lambda: NOW.isoformat())
    chart_read = ChartStyleReadService(db_path)

    team = await users.create_team(name="Chart Style V2", max_seats=5)
    owner = await users.create_user(
        team_id=team.id,
        email="owner@chart-v2.example",
        password="Passw0rd!",
    )
    member = await users.create_user(
        team_id=team.id,
        email="member@chart-v2.example",
        password="Passw0rd!",
    )
    await users.set_team_creator(team.id, owner.id)

    app = FastAPI()
    app.state.user_store = users
    app.state.user_settings_store = user_settings
    app.state.team_settings_store = team_settings_store
    app.state.chart_style_store = chart_store
    app.state.chart_style_uow = chart_uow
    app.state.chart_style_read_service = chart_read
    app.state.deployment_mode = "saas"
    app.state.settings = SimpleNamespace(
        auth_mode="prod",
        dev_default_user="dev_user",
    )
    app.include_router(team_settings.router)
    app.include_router(theme.router)
    return SimpleNamespace(
        app=app,
        db_path=db_path,
        users=users,
        user_settings=user_settings,
        team_settings=team_settings_store,
        chart_store=chart_store,
        chart_uow=chart_uow,
        team=team,
        owner=owner,
        member=member,
    )


@pytest.fixture
def client(app_ctx) -> TestClient:
    return TestClient(app_ctx.app)


async def _published_custom(app_ctx, *, name: str = "Operations calm"):
    created = await app_ctx.chart_uow.create(
        team_id=app_ctx.team.id,
        actor_id=app_ctx.owner.id,
        request_id=str(uuid4()),
        base_preset_id="clean-blue",
        source=_source(name),
    )
    style_id = created.detail["style"]["id"]
    preview = await app_ctx.chart_uow.preview(
        team_id=app_ctx.team.id,
        style_id=style_id,
        source=_source(name),
    )
    published = await app_ctx.chart_uow.publish(
        team_id=app_ctx.team.id,
        actor_id=app_ctx.owner.id,
        style_id=style_id,
        if_match=created.response_etag,
        acknowledged_validation_fingerprint=preview["validationFingerprint"],
    )
    return style_id, published


async def test_v2_builtin_effective_and_options_are_strong_cached_contracts(
    client: TestClient,
    app_ctx,
) -> None:
    effective = client.get(
        "/api/v1/theme/effective",
        headers=V2_HEADER,
        cookies=_as(app_ctx.member),
    )
    assert effective.status_code == 200, effective.text
    assert not effective.headers["etag"].startswith("W/")
    assert "X-Bowhead-Chart-Style-Contract" in effective.headers["vary"]
    style = effective.json()["chartStyle"]
    assert style["schemaVersion"] == 2
    assert style["style"]["kind"] == "builtIn"
    assert style["style"]["id"] == "clean-blue"  # product default since 1.5.0
    assert len(style["modeColors"]["light"]["categoricalSeriesPalette"]) == 16
    assert style["modeColors"]["dark"]["finalPaletteQA"]["hardInvariantsPass"] is True

    conditional = client.get(
        "/api/v1/theme/effective",
        headers={**V2_HEADER, "If-None-Match": effective.headers["etag"]},
        cookies=_as(app_ctx.member),
    )
    assert conditional.status_code == 304

    options = client.get(
        "/api/v1/theme/chart-style/options",
        headers=V2_HEADER,
        cookies=_as(app_ctx.member),
    )
    assert options.status_code == 200, options.text
    assert not options.headers["etag"].startswith("W/")
    assert [item["id"] for item in options.json()["items"]] == [
        "bowhead-editorial",
        "clean-blue",
        "analytical-neutral",
    ]
    assert all("resolvedPresetSnapshot" in item for item in options.json()["items"])
    assert len(options.json()["items"]) <= 15
    assert len(options.content) < 512 * 1024
    assert (
        client.get(
            "/api/v1/theme/chart-style/options",
            cookies=_as(app_ctx.member),
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/v1/theme/effective",
            headers={"X-Bowhead-Chart-Style-Contract": "99"},
            cookies=_as(app_ctx.member),
        ).status_code
        == 400
    )

    legacy = client.get(
        "/api/v1/theme/effective",
        cookies=_as(app_ctx.member),
    )
    assert legacy.status_code == 200
    assert legacy.json()["chartStyle"]["schemaVersion"] == 1

    app_ctx.app.state.effective_theme_resolver = _ActualSurfaceResolver()
    actual = client.get(
        "/api/v1/theme/effective",
        headers=V2_HEADER,
        cookies=_as(app_ctx.member),
    )
    assert actual.status_code == 200, actual.text
    light = actual.json()["chartStyle"]["modeColors"]["light"]
    assert light["finalPaletteQA"]["surfaceChecksum"] == sha256_json({"surface": "#F5E8DC"})
    assert all(adjustment["effectiveColorSchemeChecksum"] == "actual-team-scheme" for adjustment in light["adjustments"])


async def test_v2_policy_custom_selection_exact_revision_and_inactive_fallback(
    client: TestClient,
    app_ctx,
) -> None:
    style_id, published = await _published_custom(app_ctx)
    initial = client.get(
        "/api/v1/team-settings/chart-style-policy",
        headers=V2_HEADER,
        cookies=_as(app_ctx.owner),
    )
    assert initial.status_code == 200, initial.text
    assert not initial.headers["etag"].startswith("W/")

    policy = {
        "version": 2,
        "defaultStyleId": style_id,
        "enabledStyleIds": [style_id, "clean-blue", "bowhead-editorial"],
        "fallbackBuiltInPresetId": "clean-blue",
    }
    saved = client.put(
        "/api/v1/team-settings/chart-style-policy",
        headers={**V2_HEADER, "If-Match": initial.headers["etag"]},
        json=policy,
        cookies=_as(app_ctx.owner),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["policy"] == policy
    assert saved.json()["registryVersion"] == 2

    stale = client.put(
        "/api/v1/team-settings/chart-style-policy",
        headers={**V2_HEADER, "If-Match": initial.headers["etag"]},
        json={**policy, "defaultStyleId": "clean-blue"},
        cookies=_as(app_ctx.owner),
    )
    assert stale.status_code == 412
    assert stale.json()["detail"]["error"]["code"] == ("chart_style_policy_precondition_failed")

    options = client.get(
        "/api/v1/theme/chart-style/options",
        headers=V2_HEADER,
        cookies=_as(app_ctx.member),
    )
    custom = next(item for item in options.json()["items"] if item["id"] == style_id)
    assert custom["kind"] == "teamCustom"
    assert custom["revision"] == published.detail["activeRevision"]["id"]
    assert "overrides" not in custom
    assert "authoringBaseSnapshot" not in custom
    assert "createdBy" not in custom

    selection = client.put(
        "/api/v1/theme/chart-style/selection",
        headers=V2_HEADER,
        json={"version": 2, "styleId": style_id},
        cookies=_as(app_ctx.member),
    )
    assert selection.status_code == 200, selection.text
    assert json.loads(
        await app_ctx.user_settings.get(
            app_ctx.member.id,
            user_team_chart_style_selection_key(app_ctx.team.id),
        )
    ) == {
        "version": 2,
        "styleId": style_id,
        "fallbackBuiltInPresetId": "clean-blue",
    }

    effective = client.get(
        "/api/v1/theme/effective",
        headers=V2_HEADER,
        cookies=_as(app_ctx.member),
    )
    identity = effective.json()["chartStyle"]
    assert identity["style"]["id"] == style_id
    assert identity["style"]["revision"] == published.detail["activeRevision"]["id"]
    assert identity["selection"] == {
        "source": "user",
        "savedStyleId": style_id,
        "unavailableReason": None,
    }
    assert identity["style"]["resolvedPresetSnapshot"]["kindTokens"]["line"]["line"]["curve"] == "straight"

    legacy = client.get(
        "/api/v1/theme/effective",
        cookies=_as(app_ctx.member),
    )
    assert legacy.status_code == 200
    assert legacy.json()["chartStyle"]["preset"]["id"] == "clean-blue"

    next_policy = {
        **policy,
        "defaultStyleId": "clean-blue",
    }
    changed = client.put(
        "/api/v1/team-settings/chart-style-policy",
        headers={**V2_HEADER, "If-Match": saved.headers["etag"]},
        json=next_policy,
        cookies=_as(app_ctx.owner),
    )
    assert changed.status_code == 200, changed.text
    await app_ctx.chart_uow.deactivate(
        team_id=app_ctx.team.id,
        actor_id=app_ctx.owner.id,
        style_id=style_id,
        if_match=published.response_etag,
    )

    fallback = client.get(
        "/api/v1/theme/effective",
        headers=V2_HEADER,
        cookies=_as(app_ctx.member),
    ).json()["chartStyle"]
    assert fallback["style"]["id"] == "clean-blue"
    assert fallback["selection"]["savedStyleId"] == style_id
    assert fallback["selection"]["unavailableReason"] == "inactive"
    assert fallback["resolutionAdjustments"][0]["reason"] == ("selection.inactive")


async def test_v2_selection_is_scope_safe_and_checksum_failure_falls_back(
    client: TestClient,
    app_ctx,
) -> None:
    style_id, published = await _published_custom(app_ctx, name="Finance calm")
    initial = client.get(
        "/api/v1/team-settings/chart-style-policy",
        headers=V2_HEADER,
        cookies=_as(app_ctx.owner),
    )
    policy = {
        "version": 2,
        "defaultStyleId": style_id,
        "enabledStyleIds": ["clean-blue", style_id],
        "fallbackBuiltInPresetId": "bowhead-editorial",
    }
    saved = client.put(
        "/api/v1/team-settings/chart-style-policy",
        headers={**V2_HEADER, "If-Match": initial.headers["etag"]},
        json=policy,
        cookies=_as(app_ctx.owner),
    )
    assert saved.status_code == 200, saved.text

    hidden = client.put(
        "/api/v1/theme/chart-style/selection",
        headers=V2_HEADER,
        json={"version": 2, "styleId": "tcs_ffffffffffffffffffffffff"},
        cookies=_as(app_ctx.member),
    )
    assert hidden.status_code == 404
    assert hidden.json()["detail"]["error"]["code"] == "chart_style_not_found"

    selected = client.put(
        "/api/v1/theme/chart-style/selection",
        headers=V2_HEADER,
        json={"version": 2, "styleId": style_id},
        cookies=_as(app_ctx.member),
    )
    assert selected.status_code == 200
    async with aiosqlite.connect(app_ctx.db_path) as db:
        # Simulate an externally corrupted restore after verifying that normal
        # application writes are protected by the immutable-row trigger.
        await db.execute("DROP TRIGGER trg_team_chart_style_revision_update")
        await db.execute(
            "UPDATE team_chart_style_revisions SET snapshot_checksum=? WHERE id=?",
            ("sha256:" + "0" * 64, published.detail["activeRevision"]["id"]),
        )
        await db.commit()

    fallback = client.get(
        "/api/v1/theme/effective",
        headers=V2_HEADER,
        cookies=_as(app_ctx.member),
    )
    assert fallback.status_code == 200, fallback.text
    chart_style = fallback.json()["chartStyle"]
    assert chart_style["style"]["id"] == "bowhead-editorial"
    assert chart_style["selection"]["unavailableReason"] == "checksum_mismatch"
    assert chart_style["resolutionAdjustments"][0]["reason"] == ("selection.checksum_mismatch")


async def test_v2_policy_rejects_unpublished_or_cross_team_custom_id(
    client: TestClient,
    app_ctx,
) -> None:
    initial = client.get(
        "/api/v1/team-settings/chart-style-policy",
        headers=V2_HEADER,
        cookies=_as(app_ctx.owner),
    )
    response = client.put(
        "/api/v1/team-settings/chart-style-policy",
        headers={**V2_HEADER, "If-Match": initial.headers["etag"]},
        json={
            "version": 2,
            "defaultStyleId": "clean-blue",
            "enabledStyleIds": [
                "clean-blue",
                "tcs_ffffffffffffffffffffffff",
            ],
            "fallbackBuiltInPresetId": "bowhead-editorial",
        },
        cookies=_as(app_ctx.owner),
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "chart_style_not_found"
    assert (
        await app_ctx.team_settings.get(
            app_ctx.team.id,
            KEY_TEAM_CHART_STYLE_POLICY,
        )
        is None
    )


async def test_product_default_switch_only_affects_teams_without_an_explicit_choice(
    client: TestClient,
    app_ctx,
) -> None:
    """1.5.0 makes clean-blue the product default; saved choices must survive the switch."""

    def effective_style() -> dict:
        response = client.get("/api/v1/theme/effective", headers=V2_HEADER, cookies=_as(app_ctx.member))
        assert response.status_code == 200, response.text
        return response.json()["chartStyle"]

    # 1. No team policy saved: the product default applies.
    implicit = effective_style()
    assert implicit["style"]["id"] == "clean-blue"
    assert implicit["selection"]["source"] == "product"

    # 2. A team that explicitly chose the editorial preset keeps it.
    initial = client.get("/api/v1/team-settings/chart-style-policy", headers=V2_HEADER, cookies=_as(app_ctx.owner))
    editorial_policy = {
        "version": 2,
        "defaultStyleId": "bowhead-editorial",
        "enabledStyleIds": ["bowhead-editorial", "clean-blue"],
        "fallbackBuiltInPresetId": "bowhead-editorial",
    }
    saved = client.put(
        "/api/v1/team-settings/chart-style-policy",
        headers={**V2_HEADER, "If-Match": initial.headers["etag"]},
        json=editorial_policy,
        cookies=_as(app_ctx.owner),
    )
    assert saved.status_code == 200, saved.text
    explicit = effective_style()
    assert explicit["style"]["id"] == "bowhead-editorial"
    assert explicit["selection"]["source"] == "team"

    # 3. A published custom style stays the team default, with its own frozen revision.
    style_id, published = await _published_custom(app_ctx)
    custom_policy = {**editorial_policy, "defaultStyleId": style_id, "enabledStyleIds": [style_id, "bowhead-editorial"]}
    saved = client.put(
        "/api/v1/team-settings/chart-style-policy",
        headers={**V2_HEADER, "If-Match": saved.headers["etag"]},
        json=custom_policy,
        cookies=_as(app_ctx.owner),
    )
    assert saved.status_code == 200, saved.text
    custom = effective_style()
    assert custom["style"]["id"] == style_id
    assert custom["style"]["revision"] == published.detail["activeRevision"]["id"]
