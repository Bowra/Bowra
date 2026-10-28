"""Sprint β G10 — change-password + active sessions endpoint tests."""

from __future__ import annotations

import json

import aiosqlite
import pytest
from bowhead_core.user_store import UserStore, _now, hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "g10.db")
    await s.initialize()
    return s


async def _seed_team(s, team_id="team_g10"):
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, 'O', 10, 'ultra', 0, 0, "
            "'{\"pro\":5000,\"ultra\":30000}', 'active', ?)",
            (team_id, _now()),
        )
        await db.commit()


async def _seed_user(
    s,
    user_id,
    email,
    password="OldPassword123",
    *,
    team_id="team_g10",  # noqa: S107
    oauth_only=False,
):
    async with aiosqlite.connect(s.db_path) as db:
        pw_hash = None if oauth_only else hash_password(password)
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "oauth_providers_json, tier, credit_balance, credit_quota, status, created_at) "
            "VALUES (?, ?, ?, 1, ?, ?, 'ultra', 0, 0, 'active', ?)",
            (user_id, team_id, email, pw_hash, '["feishu"]' if oauth_only else "[]", _now()),
        )
        await db.commit()


def _jwt(user_id, secret, pwd_v=0):
    import time

    import jwt as _jwt

    return _jwt.encode(
        {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600, "pwd_v": pwd_v},
        secret,
        algorithm="HS256",
    )


SECRET = "test-secret-long-enough-for-hs256-32-bytes"


@pytest.fixture
def app(store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.delenv("BOWHEAD_SMTP_HOST", raising=False)
    from bowhead_gateway.routes import auth as auth_routes

    auth_routes._RATE_BUCKET.clear()
    a = FastAPI()
    a.state.user_store = store

    class _S:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    a.state.settings = _S()
    a.include_router(auth_routes.router, prefix="/api/v1/auth")
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


# ─── change-password tests ─────────────────────────────────


@pytest.mark.asyncio
async def test_change_password_oauth_only_friendly_422(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com", oauth_only=True)
    jwt = _jwt("u1", SECRET)
    r = client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "doesnt_matter", "new_password": "NewPassword456"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["detail"]["code"] == "oauth_user_no_password"
    assert "feishu" in body["detail"]["providers"]


@pytest.mark.asyncio
async def test_change_password_wrong_old_401(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    jwt = _jwt("u1", SECRET)
    r = client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "WrongPassword", "new_password": "NewPassword456"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "wrong_old_password"
    # audit row for failure
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute("SELECT COUNT(*) FROM admin_audit_log WHERE action='password_change_failed'") as cur,
    ):
        row = await cur.fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_change_password_success_bumps_pwd_version(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    v0 = await store.get_pwd_version("u1")
    jwt = _jwt("u1", SECRET, pwd_v=v0)
    r = client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "OldPassword123", "new_password": "NewPassword456"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "access_token" in body
    # Verify pwd_version bumped
    v1 = await store.get_pwd_version("u1")
    assert v1 == v0 + 1


@pytest.mark.asyncio
async def test_change_password_audit_row(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    jwt = _jwt("u1", SECRET)
    client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "OldPassword123", "new_password": "NewPassword456"},
        cookies={"bowhead_jwt": jwt},
    )
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute("SELECT action, payload_json FROM admin_audit_log WHERE action='password_change' AND actor_user_id='u1'") as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    pl = json.loads(row[1])
    assert "ip_masked" in pl


@pytest.mark.asyncio
async def test_change_password_concurrent_lock_409(client, store):
    """Second call within 30s while first is held → 409."""
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    # Manually acquire lock first
    await store.try_acquire_pwd_change_lock("u1", ttl_s=30)
    jwt = _jwt("u1", SECRET)
    r = client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "OldPassword123", "new_password": "NewPassword456"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "concurrent_change_in_progress"


@pytest.mark.asyncio
async def test_change_password_revokes_refresh_tokens(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    # Seed 2 active refresh tokens
    await store.record_refresh_token(
        user_id="u1",
        raw_token="token_A",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    await store.record_refresh_token(
        user_id="u1",
        raw_token="token_B",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    jwt = _jwt("u1", SECRET)
    r = client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "OldPassword123", "new_password": "NewPassword456"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200
    r.json()
    # 2 revoked + 1 new = revoked_count includes the new one's revoke; "other_devices_kicked" = revoked - 1
    # Both old tokens marked revoked
    sessions = await store.list_refresh_tokens("u1", active_only=True)
    # Should have only the newly-issued session
    assert len(sessions) == 1


# ─── pwd_v JWT invalidation ────────────────────────────────


@pytest.mark.asyncio
async def test_old_jwt_rejected_after_password_change(client, store):
    """P0-S-3 fix: stale pwd_v JWT → 401 password_changed_relogin_required."""
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    # Change pwd → bumps pwd_version to 1
    await store.update_user_password("u1", hash_password("NewPwd"))
    # Old token (pwd_v=0) — should be rejected
    old_jwt = _jwt("u1", SECRET, pwd_v=0)
    r = client.get("/api/v1/auth/me", cookies={"bowhead_jwt": old_jwt})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "password_changed_relogin_required"


@pytest.mark.asyncio
async def test_fresh_jwt_accepted_after_password_change(client, store):
    """New pwd_v JWT → still works."""
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    await store.update_user_password("u1", hash_password("NewPwd"))
    # Fresh token (pwd_v=1) — OK
    new_jwt = _jwt("u1", SECRET, pwd_v=1)
    r = client.get("/api/v1/auth/me", cookies={"bowhead_jwt": new_jwt})
    assert r.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("oauth_only", "expected_has_password", "expected_providers"),
    [
        (False, True, []),
        (True, False, ["feishu"]),
    ],
)
async def test_auth_me_exposes_password_capability_without_hash(
    client,
    store,
    oauth_only,
    expected_has_password,
    expected_providers,
):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com", oauth_only=oauth_only)
    jwt = _jwt("u1", SECRET)

    r = client.get("/api/v1/auth/me", cookies={"bowhead_jwt": jwt})

    assert r.status_code == 200
    body = r.json()
    assert body["has_password"] is expected_has_password
    assert body["oauth_providers"] == expected_providers
    assert "password_hash" not in body


# ─── Sessions list / revoke ────────────────────────────────


@pytest.mark.asyncio
async def test_list_active_sessions(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    await store.record_refresh_token(
        user_id="u1",
        raw_token="token_X",
        expires_at="2099-01-01T00:00:00+00:00",
        user_agent="Chrome",
        ip="1.2.3.4",
    )
    jwt = _jwt("u1", SECRET)
    r = client.get("/api/v1/auth/sessions/active", cookies={"bowhead_jwt": jwt})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["ip_masked"] == "1.2.3.x"
    assert body["items"][0]["user_agent"] == "Chrome"


@pytest.mark.asyncio
async def test_revoke_session_idor_safe(client, store):
    """User can't revoke another user's session."""
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    await _seed_user(store, "u2", "u2@t.com")
    sid_u2 = await store.record_refresh_token(
        user_id="u2",
        raw_token="u2_token",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    jwt_u1 = _jwt("u1", SECRET)
    r = client.delete(
        f"/api/v1/auth/sessions/{sid_u2}",
        cookies={"bowhead_jwt": jwt_u1},
    )
    assert r.status_code == 404  # 404 not 403 — no info leak


@pytest.mark.asyncio
async def test_revoke_own_session(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    sid = await store.record_refresh_token(
        user_id="u1",
        raw_token="my_token",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    jwt = _jwt("u1", SECRET)
    r = client.delete(
        f"/api/v1/auth/sessions/{sid}",
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200
    # Verify audit row
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute("SELECT COUNT(*) FROM admin_audit_log WHERE action='session_revoked' AND actor_user_id='u1'") as cur,
    ):
        row = await cur.fetchone()
    assert row[0] == 1
