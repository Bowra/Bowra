from __future__ import annotations

import time

import aiosqlite
import httpx2
import jwt
import pytest
from bowhead_core.user_store import UserStore, _id, _now
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings


@pytest.fixture
async def app_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test_secret_auth_me_credit_caps")
    monkeypatch.delenv("BOWHEAD_PROD", raising=False)
    db = tmp_path / "auth_me_credit_caps.db"
    user_store = UserStore(str(db))
    await user_store.initialize()
    app = create_app()
    app.state.settings = Settings()
    app.state.user_store = user_store
    return app, user_store


async def _seed_team_and_user(
    store: UserStore,
    *,
    team_id: str = "team_a",
    user_id: str = "u_credit_admin",
    role: str = "team_admin",
) -> None:
    now = _now()
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (team_id, "Test Team", "test.com", 10, "ultra", 100_000, 100_000, '{"pro":5000,"ultra":30000}', "active", now),
        )
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "oauth_providers_json, tier, credit_balance, credit_quota, "
            "credit_period_start, locale, timezone, preferences_json, status, created_at) "
            "VALUES (?, ?, ?, 1, '', '[]', 'ultra', 30000, 30000, ?, 'zh-CN', "
            "'UTC', '{}', 'active', ?)",
            (user_id, team_id, f"{user_id}@test.com", now, now),
        )
        await db.execute(
            "INSERT INTO team_members(id, user_id, team_id, role, promoted_by, promoted_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
            (_id("om"), user_id, team_id, role, "u_creator", now, now),
        )
        await db.commit()


def _jwt(user_id: str, team_id: str = "team_a") -> str:
    payload = {
        "sub": user_id,
        "team_id": team_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, "test_secret_auth_me_credit_caps", algorithm="HS256")


@pytest.mark.integration
@pytest.mark.parametrize("email_verified", [True, False])
@pytest.mark.parametrize("has_membership", [True, False])
async def test_auth_me_returns_email_verification_state(app_ctx, monkeypatch, email_verified, has_membership) -> None:
    monkeypatch.setenv("FEATURE_MULTI_TEAM_AUTHZ", "1")
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "0")
    app, store = app_ctx
    await _seed_team_and_user(store)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET email_verified = ? WHERE id = ?", (int(email_verified), "u_credit_admin"))
        if not has_membership:
            await db.execute("UPDATE team_members SET status = 'removed' WHERE user_id = ?", ("u_credit_admin",))
        await db.commit()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as client:
        client.cookies.set("bowhead_jwt", _jwt("u_credit_admin"))
        response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    assert response.json()["email_verified"] is email_verified
    assert response.json().get("membership_required", False) is (not has_membership)


@pytest.mark.integration
async def test_auth_me_returns_credit_capabilities_for_team_admin(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team_and_user(store)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        c.cookies.set("bowhead_jwt", _jwt("u_credit_admin"))
        r = await c.get("/api/v1/auth/me")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["team_role"] == "team_admin"
    caps = data["credit_capabilities"]
    assert caps["credits_read"] is True
    assert caps["credit_grant"] is True
    assert caps["credit_user_quota_write"] is True
    assert caps["credit_team_quota_write"] is True
    assert caps["credit_attention_dismiss"] is True
    assert caps["credit_user_tier_write"] is True
    assert caps["credit_period_reset"] is True
    assert data["capabilities"]["credit"] == caps


@pytest.mark.integration
async def test_auth_me_returns_current_team_name(app_ctx) -> None:
    app, store = app_ctx
    await _seed_team_and_user(store, role="member")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as client:
        client.cookies.set("bowhead_jwt", _jwt("u_credit_admin"))
        response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["team_name"] == "Test Team"
    assert data["team_id"] == "team_a"
    assert data["team_role"] == "member"


@pytest.mark.integration
async def test_auth_me_returns_own_credit_period(app_ctx, monkeypatch) -> None:
    from datetime import UTC, datetime

    import bowhead_gateway.routes.auth as auth_routes

    app, store = app_ctx
    await _seed_team_and_user(store, role="member")
    await _seed_team_and_user(store, team_id="team_b", user_id="u_other", role="member")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET credit_period_start = ? WHERE id = ?", ("2026-09-01T00:05:00+00:00", "u_credit_admin"))
        await db.execute("UPDATE users SET credit_period_start = ? WHERE id = ?", ("2026-08-01T00:05:00+00:00", "u_other"))
        await db.commit()
    monkeypatch.setattr(auth_routes, "next_monthly_credit_reset", lambda: datetime(2026, 10, 1, tzinfo=UTC))
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as client:
        client.cookies.set("bowhead_jwt", _jwt("u_credit_admin"))
        response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["credit_period_start"] == "2026-09-01T00:05:00+00:00"
    assert data["credit_period_resets_at"] == "2026-10-01T00:00:00+00:00"
    assert (data["credit_balance"], data["credit_quota"]) == (30000, 30000)


@pytest.mark.integration
async def test_auth_me_team_name_is_empty_without_membership(app_ctx, monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_MULTI_TEAM_AUTHZ", "1")
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "0")
    app, store = app_ctx
    await _seed_team_and_user(store)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE team_members SET status = 'removed' WHERE user_id = ?", ("u_credit_admin",))
        await db.commit()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as client:
        client.cookies.set("bowhead_jwt", _jwt("u_credit_admin"))
        response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    assert response.json()["membership_required"] is True
    assert response.json()["team_name"] == ""
    assert response.json()["credit_period_resets_at"] is None
    assert response.json()["credit_period_start"] is None


async def test_auth_me_exposes_feedback_loop_capability(app_ctx, monkeypatch) -> None:
    # agent-feedback-loop FE batch A: suggestion-inbox entry follows the single server gate, without independent decisions.
    app, store = app_ctx
    await _seed_team_and_user(store)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        c.cookies.set("bowhead_jwt", _jwt("u_credit_admin"))
        r = await c.get("/api/v1/auth/me")
    assert r.status_code == 200, r.text
    assert r.json()["capabilities"]["feedback_loop"] is True


@pytest.mark.parametrize(
    "boundary", ["ready", "flag_off", "worker_off", "unreleased", "bad_matrix", "unknown_adapter", "wrong_store", "no_key", "no_sources"]
)
async def test_auth_me_reference_input_requires_released_agent_and_local_runtime(app_ctx, monkeypatch, boundary):
    from types import SimpleNamespace

    from cryptography.fernet import Fernet

    from bowhead_core import chat_reference_catalog

    app, store = app_ctx
    await _seed_team_and_user(store)
    original = chat_reference_catalog.reference_entry_support

    def support(mode):
        cells = original(mode)
        if boundary == "unreleased":
            cells = [
                cell.model_copy(update={"supported": False, "adapter_id": "unavailable", "reason_code": "runtime_not_released"})
                if cell.resource_kind == "agent"
                else cell
                for cell in cells
            ]
        else:
            cells = [
                cell.model_copy(
                    update={
                        "supported": True,
                        "adapter_id": "future" if boundary == "unknown_adapter" else "initial-agent-skill",
                        "adapter_version": 1,
                        "reason_code": None,
                    }
                )
                if cell.resource_kind == "agent"
                else cell
                for cell in cells
            ]
        return cells[:-1] if boundary == "bad_matrix" else cells

    monkeypatch.setattr(chat_reference_catalog, "reference_entry_support", support)
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "0" if boundary == "worker_off" else "1")
    monkeypatch.setenv("FEATURE_AGENT_START_USE_V1", "0" if boundary == "flag_off" else "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "invalid" if boundary == "no_key" else Fernet.generate_key().decode())
    app.state.session_store = store
    app.state.orchestrator = SimpleNamespace(
        session_store=object() if boundary == "wrong_store" else store,
        reference_skill_sources_factory=None if boundary == "no_sources" else lambda scope: None,
    )
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as client:
        client.cookies.set("bowhead_jwt", _jwt("u_credit_admin"))
        response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    assert response.json()["capabilities"]["composer"]["reference_input"] is (boundary in {"ready", "flag_off"})
    assert response.json()["capabilities"]["composer"]["agent_start_use"] is (boundary == "ready")


async def test_migrated_member_uses_current_role_with_existing_login(app_ctx, tmp_path) -> None:
    """Upgrade compatibility: an existing login loses retired team authority."""
    from importlib import import_module
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from bowhead_core import migrations

    migration = import_module("bowhead_core.migrations.versions.0097_retire_billing_admin")
    path = tmp_path / "prior.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, migration.down_revision)
    app, _ = app_ctx
    store = UserStore(path)
    app.state.user_store = store
    await _seed_team_and_user(store, user_id="u_migrated", role="billing_admin")
    # The same signed login remains valid after the database migration.
    token = _jwt("u_migrated")
    command.upgrade(config, "head")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as client:
        client.cookies.set("bowhead_jwt", token)
        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["team_role"] == "member"
        assert not any(me.json()["credit_capabilities"].values())
        for endpoint in ("/api/v1/admin/credits", "/api/v1/admin/audit", "/api/v1/admin/members"):
            response = await client.get(endpoint)
            assert response.status_code == 403, response.text


@pytest.mark.parametrize(("flag", "expected"), [(None, False), ("0", False), ("1", True)])
@pytest.mark.parametrize(
    ("capability", "environment"),
    [
        ("kb_health_observation", "FEATURE_KB_TRUST_V2"),
        # Settings › Account offers Deutsch as an answer language only while German model output is admitted.
        ("german_ai_output", "FEATURE_GERMAN_AI_OUTPUT"),
    ],
)
async def test_auth_me_exposes_deployment_switch_capabilities(app_ctx, monkeypatch, flag, expected, capability, environment) -> None:
    if flag is None:
        monkeypatch.delenv(environment, raising=False)
    else:
        monkeypatch.setenv(environment, flag)
    app, store = app_ctx
    await _seed_team_and_user(store)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as client:
        client.cookies.set("bowhead_jwt", _jwt("u_credit_admin"))
        response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    assert response.json()["capabilities"][capability] is expected


@pytest.mark.parametrize("plugin_flag", [None, "0", "1"])
@pytest.mark.parametrize("content_flag", [None, "0", "1"])
async def test_auth_me_gates_shopify_content_confirmations_on_both_switches(app_ctx, monkeypatch, plugin_flag, content_flag) -> None:
    for name, value in (("FEATURE_SHOPIFY_PLUGIN", plugin_flag), ("FEATURE_SHOPIFY_CONTENT_WRITES", content_flag)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    app, store = app_ctx
    await _seed_team_and_user(store)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as client:
        client.cookies.set("bowhead_jwt", _jwt("u_credit_admin"))
        response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    capabilities = response.json()["capabilities"]
    assert capabilities["shopify_plugin"] is (plugin_flag == "1")
    assert capabilities["shopify_content_confirmations"] is (plugin_flag == "1" and content_flag == "1")
