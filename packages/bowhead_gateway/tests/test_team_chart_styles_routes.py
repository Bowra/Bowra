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
from bowhead_core.audit_actions import AuditAction
from bowhead_core.chart_style_store import ChartStyleStore
from bowhead_core.chart_style_uow import ChartStyleUnitOfWork
from bowhead_core.entitlements import CommunityResolver
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.routes import auth as auth_routes
from bowhead_gateway.routes import team_chart_styles
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_core import migrations

SECRET = "chart-style-routes-test-secret-long-enough"
NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    return config


def _jwt(user_id: str) -> str:
    return jwt_lib.encode(
        {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600, "pwd_v": 0},
        SECRET,
        algorithm="HS256",
    )


def _as(user: User) -> dict[str, str]:
    return {"bowhead_jwt": _jwt(user.id)}


def _source(name: str = "Sales Editorial") -> dict:
    return {
        "schemaVersion": 2,
        "name": name,
        "description": "Presentation only",
        "overrides": {"schemaVersion": 2},
    }


def _create_body(name: str = "Sales Editorial") -> dict:
    return {
        "requestId": str(uuid4()),
        "basePresetId": "clean-blue",
        "source": _source(name),
    }


@pytest.fixture
async def app_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    auth_routes._RATE_BUCKET.clear()

    db_path = tmp_path / "chart-style-routes.db"
    command.upgrade(_config(db_path), "head")
    users = UserStore(db_path)
    await users.initialize()
    settings = TeamSettingsStore(db_path)
    await settings.init()
    store = ChartStyleStore(db_path)
    uow = ChartStyleUnitOfWork(db_path)

    team = await users.create_team(name="Chart Style Team", max_seats=10)
    owner = await users.create_user(team_id=team.id, email="owner@chart.example", password="Passw0rd!")
    admin = await users.create_user(team_id=team.id, email="admin@chart.example", password="Passw0rd!")
    member = await users.create_user(team_id=team.id, email="member@chart.example", password="Passw0rd!")
    await users.set_team_creator(team.id, owner.id)
    await users.promote_team_role(
        user_id=admin.id,
        team_id=team.id,
        new_role="team_admin",
        promoted_by=owner.id,
    )
    other_team = await users.create_team(name="Other Team", max_seats=3)
    other_owner = await users.create_user(team_id=other_team.id, email="other@chart.example", password="Passw0rd!")
    await users.set_team_creator(other_team.id, other_owner.id)

    app = FastAPI()
    app.state.user_store = users
    app.state.team_settings_store = settings
    app.state.chart_style_store = store
    app.state.chart_style_uow = uow
    app.state.deployment_mode = "saas"
    app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="dev_user")
    app.include_router(team_chart_styles.router)
    return SimpleNamespace(
        app=app,
        users=users,
        team=team,
        owner=owner,
        admin=admin,
        member=member,
        other_owner=other_owner,
    )


@pytest.fixture
def client(app_ctx) -> TestClient:
    return TestClient(app_ctx.app)


async def test_admin_contract_create_preview_publish_history_and_audit(client: TestClient, app_ctx) -> None:
    create_body = _create_body()
    created = client.post(
        "/api/v1/team-settings/chart-styles/",
        json=create_body,
        cookies=_as(app_ctx.admin),
    )
    assert created.status_code == 200, created.text
    created_body = created.json()
    style_id = created_body["detail"]["style"]["id"]
    assert created.headers["etag"] == created_body["detail"]["draftEtag"]
    assert not created.headers["etag"].startswith("W/")

    replay = client.post(
        "/api/v1/team-settings/chart-styles/",
        json=create_body,
        cookies=_as(app_ctx.admin),
    )
    assert replay.status_code == 200
    assert replay.json()["detail"]["style"]["id"] == style_id

    detail = client.get(f"/api/v1/team-settings/chart-styles/{style_id}", cookies=_as(app_ctx.owner))
    assert detail.status_code == 200
    assert detail.headers["etag"] == detail.json()["resourceEtag"]

    preview = client.post(
        "/api/v1/team-settings/chart-styles/preview",
        json={"styleId": style_id, "candidate": _source(), "surface": "team-base"},
        cookies=_as(app_ctx.admin),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["candidateSnapshotChecksum"].startswith("sha256:")

    missing_precondition = client.post(
        f"/api/v1/team-settings/chart-styles/{style_id}/publish",
        json={},
        cookies=_as(app_ctx.admin),
    )
    assert missing_precondition.status_code == 428
    assert missing_precondition.json()["detail"]["error"]["code"] == "chart_style_precondition_required"

    published = client.post(
        f"/api/v1/team-settings/chart-styles/{style_id}/publish",
        json={"acknowledgedValidationFingerprint": preview.json()["validationFingerprint"]},
        headers={"If-Match": created.headers["etag"]},
        cookies=_as(app_ctx.admin),
    )
    assert published.status_code == 200, published.text
    assert published.headers["etag"] == published.json()["detail"]["headEtag"]
    assert published.json()["detail"]["state"] == "active_clean"
    assert published.json()["registryVersion"] == 1

    history = client.get(
        f"/api/v1/team-settings/chart-styles/{style_id}/revisions",
        cookies=_as(app_ctx.owner),
    )
    assert history.status_code == 200
    assert history.json()["items"][0]["operation"] == "publish"
    revision_id = history.json()["items"][0]["id"]
    exact = client.get(
        f"/api/v1/team-settings/chart-styles/{style_id}/revisions/{revision_id}",
        cookies=_as(app_ctx.owner),
    )
    assert exact.status_code == 200
    assert exact.json()["snapshotChecksum"] == history.json()["items"][0]["snapshotChecksum"]

    async with (
        aiosqlite.connect(app_ctx.users.db_path) as db,
        db.execute(
            "SELECT action,payload_json FROM admin_audit_log WHERE action IN (?,?) ORDER BY ts",
            (AuditAction.TEAM_CHART_STYLE_CREATED, AuditAction.TEAM_CHART_STYLE_PUBLISHED),
        ) as cursor,
    ):
        rows = await cursor.fetchall()
    assert [row[0] for row in rows] == [
        AuditAction.TEAM_CHART_STYLE_CREATED,
        AuditAction.TEAM_CHART_STYLE_PUBLISHED,
    ]
    assert "Sales Editorial" not in rows[0][1]


async def test_member_cannot_read_management_and_cross_team_is_hidden(client: TestClient, app_ctx) -> None:
    created = client.post(
        "/api/v1/team-settings/chart-styles/",
        json=_create_body(),
        cookies=_as(app_ctx.owner),
    )
    style_id = created.json()["detail"]["style"]["id"]

    member_list = client.get("/api/v1/team-settings/chart-styles/", cookies=_as(app_ctx.member))
    member_get = client.get(f"/api/v1/team-settings/chart-styles/{style_id}", cookies=_as(app_ctx.member))
    cross_team = client.get(f"/api/v1/team-settings/chart-styles/{style_id}", cookies=_as(app_ctx.other_owner))
    assert member_list.status_code == 403
    assert member_get.status_code == 403
    assert cross_team.status_code == 404


async def test_stale_etag_and_draft_only_delete_contract(client: TestClient, app_ctx) -> None:
    created = client.post(
        "/api/v1/team-settings/chart-styles/",
        json=_create_body(),
        cookies=_as(app_ctx.owner),
    )
    body = created.json()
    style_id = body["detail"]["style"]["id"]
    updated = client.put(
        f"/api/v1/team-settings/chart-styles/{style_id}/draft",
        json={"source": _source("Updated Style")},
        headers={"If-Match": body["detail"]["draftEtag"]},
        cookies=_as(app_ctx.owner),
    )
    assert updated.status_code == 200

    stale = client.put(
        f"/api/v1/team-settings/chart-styles/{style_id}/draft",
        json={"source": _source("Stale Style")},
        headers={"If-Match": body["detail"]["draftEtag"]},
        cookies=_as(app_ctx.admin),
    )
    assert stale.status_code == 412
    assert stale.json()["detail"]["error"]["currentEtag"] == updated.json()["detail"]["draftEtag"]

    deleted = client.delete(
        f"/api/v1/team-settings/chart-styles/{style_id}",
        headers={"If-Match": updated.json()["detail"]["resourceEtag"]},
        cookies=_as(app_ctx.owner),
    )
    assert deleted.status_code == 204
    assert "etag" not in deleted.headers
    assert deleted.content == b""


async def test_writes_are_blocked_for_readonly_team(client: TestClient, app_ctx) -> None:
    app_ctx.app.state.entitlements = CommunityResolver(mode="saas", user_store=app_ctx.users)
    async with aiosqlite.connect(app_ctx.users.db_path) as db:
        await db.execute("UPDATE teams SET status='suspended' WHERE id=?", (app_ctx.team.id,))
        await db.commit()

    response = client.post(
        "/api/v1/team-settings/chart-styles/",
        json=_create_body(),
        cookies=_as(app_ctx.owner),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "suspended"


async def test_management_body_rejects_unknown_renderer_keys(client: TestClient, app_ctx) -> None:
    body = _create_body()
    body["source"]["overrides"] = {"schemaVersion": 2, "plugins": {"tooltip": {"enabled": False}}}
    response = client.post(
        "/api/v1/team-settings/chart-styles/",
        json=body,
        cookies=_as(app_ctx.owner),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "chart_style_unknown_field"
    assert "plugins" in json.dumps(response.json())


async def test_preview_is_rate_limited_per_user_and_team(
    client: TestClient,
    app_ctx,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = client.post(
        "/api/v1/team-settings/chart-styles/",
        json=_create_body(),
        cookies=_as(app_ctx.admin),
    )
    style_id = created.json()["detail"]["style"]["id"]
    calls: list[tuple[str, int, int]] = []

    async def deny(bucket_key: str, *, limit: int, window_s: int) -> bool:
        calls.append((bucket_key, limit, window_s))
        return False

    monkeypatch.setattr(app_ctx.users, "rate_limit_check", deny)
    response = client.post(
        "/api/v1/team-settings/chart-styles/preview",
        json={"styleId": style_id, "candidate": _source(), "surface": "team-base"},
        cookies=_as(app_ctx.admin),
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert response.json()["detail"]["error"]["code"] == "chart_style_preview_rate_limited"
    assert calls == [(f"chart_style_preview:user:{app_ctx.admin.id}:team:{app_ctx.team.id}", 10, 1)]


async def test_management_body_over_64_kib_is_rejected(client: TestClient, app_ctx) -> None:
    body = _create_body()
    body["source"]["description"] = "x" * (65 * 1024)
    response = client.post(
        "/api/v1/team-settings/chart-styles/",
        json=body,
        cookies=_as(app_ctx.owner),
    )
    assert response.status_code == 413
    assert response.json()["detail"]["error"] == {
        "code": "chart_style_request_too_large",
        "maximumBytes": 64 * 1024,
    }
