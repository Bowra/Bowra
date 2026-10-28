"""Sprint α G3 verify-email endpoint tests.

Covers:
- GET /verify-email landing renders HTML + sets Referrer-Policy
- POST /verify-email valid token → 200 verified
- POST /verify-email invalid/expired → 410
- POST /verify-email jti mismatch (superseded) → 410
- POST /verify-email replay (already verified) → 200 already_verified
- POST /verify-email old token without jti (backward compat) → 200
- POST /resend-verify rate limit 1/min
- Register flow enqueues verify mail to outbox
- Whitelist domain (email_verified=True) skips enqueue
"""

from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.email_sender import make_verify_token
from bowhead_core.user_store import UserStore, _now, hash_password
from bowhead_gateway.public_entry_context import (
    PublicAccessContext,
    reset_public_access_context,
    set_public_access_context,
)
from fastapi.testclient import TestClient


@pytest.fixture
def secret():
    return "test-jwt-secret-very-long-for-hmac-256-sufficient"


@pytest.fixture
def env_setup(monkeypatch, secret):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", secret)
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_ENABLED", "1")
    # Disable SMTP — let outbox queue but don't actually send
    monkeypatch.delenv("BOWHEAD_SMTP_HOST", raising=False)


def _email_verify_secret() -> str:
    from bowhead_gateway.routes.auth import derive_jwt_secret_keys

    return derive_jwt_secret_keys("email-verify")[0][1]


def test_jwt_derived_keys_are_purpose_scoped_and_keep_rotation_keyring(monkeypatch) -> None:
    from bowhead_gateway.routes.auth import derive_jwt_secret_keys

    monkeypatch.setenv("BOWHEAD_JWT_SECRETS", "active=current-key,previous=previous-key")

    email_keys = derive_jwt_secret_keys("email-verify")
    artifact_keys = derive_jwt_secret_keys("artifact-fetch")

    assert [key_id for key_id, _secret in email_keys] == ["active", "previous"]
    assert email_keys[0][1] != "current-key"
    assert email_keys[0][1] != artifact_keys[0][1]


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "test.db")
    await s.initialize()
    return s


async def _seed_team(s: UserStore, team_id: str = "team_t") -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, "
            "default_user_tier, team_credit_balance, team_credit_quota, "
            "tier_credits_json, status, created_at) "
            "VALUES (?, 'TestOrg', NULL, 10, 'ultra', 0, 0, "
            "'{\"pro\":5000,\"ultra\":30000}', 'active', ?)",
            (team_id, _now()),
        )
        await db.commit()


async def _seed_user(
    s: UserStore,
    user_id: str = "u_t",
    *,
    email: str = "u@test.com",
    team_id: str = "team_t",
    verified: bool = False,
    jti: str | None = None,
) -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, "
            "password_hash, tier, credit_balance, credit_quota, "
            "verify_token_jti, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'ultra', 0, 0, ?, 'active', ?)",
            (user_id, team_id, email, int(verified), hash_password("password123"), jti, _now()),
        )
        await db.commit()


# ─── Unit-level: store atomic verify ────────────────────────


@pytest.mark.asyncio
async def test_atomic_consume_with_matching_jti(store):
    await _seed_team(store)
    await _seed_user(store, jti="jti_abc")
    ok = await store.mark_email_verified_atomic("u_t", "jti_abc")
    assert ok
    # Replay fails
    assert not await store.mark_email_verified_atomic("u_t", "jti_abc")


@pytest.mark.asyncio
async def test_atomic_consume_supersede(store):
    await _seed_team(store)
    await _seed_user(store, jti="jti_v2")  # stored: v2
    # Old token v1 attempt → rejected
    ok = await store.mark_email_verified_atomic("u_t", "jti_v1")
    assert not ok
    # v2 → succeeds
    assert await store.mark_email_verified_atomic("u_t", "jti_v2")


@pytest.mark.asyncio
async def test_atomic_consume_jti_none_compat(store):
    """Old token without jti — backward compat per Q8."""
    await _seed_team(store)
    await _seed_user(store, jti=None)
    ok = await store.mark_email_verified_atomic("u_t", None)
    assert ok


# ─── make_verify_token: TTL 7d + jti ────────────────────────


def test_make_verify_token_default_ttl_7d(secret):
    """r4 UX P0-U-4: default ttl_hours=24*7."""
    import jwt as _jwt

    t = make_verify_token("u_id", "u@test.com", secret)
    payload = _jwt.decode(t, secret, algorithms=["HS256"])
    duration = payload["exp"] - payload["iat"]
    assert duration == 7 * 24 * 3600


def test_make_verify_token_with_jti(secret):
    """r4 Sec P1-S-7: jti claim included if passed."""
    import jwt as _jwt

    t = make_verify_token("u_id", "u@test.com", secret, jti="my_jti")
    payload = _jwt.decode(t, secret, algorithms=["HS256"])
    assert payload["jti"] == "my_jti"
    assert payload["purpose"] == "email_verify"


def test_make_verify_token_without_jti_backward_compat(secret):
    """No jti when not passed — old behavior preserved."""
    import jwt as _jwt

    t = make_verify_token("u_id", "u@test.com", secret)
    payload = _jwt.decode(t, secret, algorithms=["HS256"])
    assert "jti" not in payload


# ─── mask_email ────────────────────────


def test_mask_email():
    from bowhead_core._email_utils import mask_email

    assert mask_email("alice@datarc.cn") == "a***@datarc.cn"
    assert mask_email("ab@x.com") == "a***@x.com"
    assert mask_email("a@x.com") == "***@x.com"
    assert mask_email("") == "***"
    assert mask_email(None) == "***"
    assert mask_email("no-at-sign") == "***"


# ─── HTTP integration: GET landing ────────────────────────


@pytest.fixture
def app(env_setup, store):
    """Minimal FastAPI app with auth routes."""
    from bowhead_gateway.routes import auth as auth_routes
    from fastapi import FastAPI

    a = FastAPI()
    a.state.user_store = store
    a.include_router(auth_routes.router, prefix="/api/v1/auth")
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


def test_verify_landing_no_token_400(client):
    r = client.get("/api/v1/auth/verify-email")
    assert r.status_code == 400


def test_verify_landing_renders_html(client, secret):
    token = make_verify_token("u_id", "user@test.com", _email_verify_secret(), jti="jti_x")
    r = client.get(f"/api/v1/auth/verify-email?token={token}")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # F-3: token in form body, masked email shown
    assert "Verify your email" in r.text
    assert 'lang="en-US"' in r.text
    assert "u***@test.com" in r.text  # masked: first char + *** + @domain
    assert token in r.text  # in <input hidden>
    assert 'const configuredBasePath = "";' in r.text
    assert "browserPath.endsWith(internalPath)" in r.text
    assert "!derivedCandidate.startsWith('//')" in r.text
    assert "derivedBasePath === null ? configuredBasePath" in r.text
    assert "fetch(appPath('/api/v1/auth/verify-email')" in r.text
    assert "window.location.replace(appPath(nextRoute))" in r.text
    # P1-S-16: Referrer-Policy header
    assert r.headers.get("referrer-policy") == "no-referrer"
    assert r.headers.get("x-frame-options") == "DENY"


def test_verify_landing_uses_request_public_entry_prefix(client, secret):
    token = make_verify_token("u_id", "user@test.com", _email_verify_secret(), jti="jti_x")
    context = PublicAccessContext(
        base_url="https://deployment.example/ai/apps/bowhead",
        origin="https://deployment.example",
        base_path="/ai/apps/bowhead",
        app_root="https://deployment.example/ai/apps/bowhead/",
        match_source="forwarded_url",
    )
    context_token = set_public_access_context(context)
    try:
        r = client.get(f"/api/v1/auth/verify-email?token={token}")
    finally:
        reset_public_access_context(context_token)

    assert r.status_code == 200
    assert 'const configuredBasePath = "/ai/apps/bowhead";' in r.text
    assert "fetch(appPath('/api/v1/auth/verify-email')" in r.text
    assert "window.location.replace(appPath(nextRoute))" in r.text


def test_verify_landing_with_invalid_token_still_renders(client):
    """Landing page renders even with garbage token (FE shows generic error on POST)."""
    r = client.get("/api/v1/auth/verify-email?token=garbage")
    assert r.status_code == 200


# ─── HTTP integration: POST consume ────────────────────────


@pytest.mark.asyncio
async def test_post_verify_succeeds_with_valid_token(store, client, secret):
    await _seed_team(store)
    await _seed_user(store, jti="jti_v1")
    token = make_verify_token("u_t", "u@test.com", _email_verify_secret(), jti="jti_v1")
    r = client.post("/api/v1/auth/verify-email", json={"token": token})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "status": "verified"}

    # DB state: verified=1 + jti cleared
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT email_verified, verify_token_jti FROM users WHERE id='u_t'") as cur:
        row = await cur.fetchone()
    assert row[0] == 1
    assert row[1] is None


@pytest.mark.asyncio
async def test_post_verify_replay_returns_already_verified(store, client, secret):
    await _seed_team(store)
    await _seed_user(store, verified=True, jti=None)
    token = make_verify_token("u_t", "u@test.com", _email_verify_secret(), jti="jti_v1")
    r = client.post("/api/v1/auth/verify-email", json={"token": token})
    assert r.status_code == 200
    assert r.json()["status"] == "already_verified"


@pytest.mark.asyncio
async def test_post_verify_superseded_jti_410(store, client, secret):
    await _seed_team(store)
    await _seed_user(store, jti="jti_v2")  # stored: v2
    token = make_verify_token("u_t", "u@test.com", _email_verify_secret(), jti="jti_v1")  # old
    r = client.post("/api/v1/auth/verify-email", json={"token": token})
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "verify_token_superseded"


def test_post_verify_invalid_token_410(client):
    r = client.post("/api/v1/auth/verify-email", json={"token": "garbage"})
    assert r.status_code == 410


@pytest.mark.asyncio
async def test_post_verify_old_token_no_jti_compat(store, client, secret):
    """Q8 compat: old token without jti → still verifies if user is unverified."""
    await _seed_team(store)
    await _seed_user(store, jti=None)  # no jti stored
    # Token issued WITHOUT jti (old style)
    token = make_verify_token("u_t", "u@test.com", _email_verify_secret())
    r = client.post("/api/v1/auth/verify-email", json={"token": token})
    assert r.status_code == 200
    assert r.json()["status"] == "verified"


def test_verify_landing_preserves_explicit_chinese(client, secret) -> None:
    token = make_verify_token("u_id", "user@test.com", _email_verify_secret(), jti="jti_zh")
    client.cookies.set("NEXT_LOCALE", "zh-CN")
    response = client.get(f"/api/v1/auth/verify-email?token={token}")
    assert response.status_code == 200
    assert 'lang="zh-CN"' in response.text
    assert "验证你的邮箱" in response.text


def test_verify_landing_follows_a_chinese_browser(client, secret) -> None:
    token = make_verify_token("u_id", "user@test.com", _email_verify_secret(), jti="jti_en")
    response = client.get(f"/api/v1/auth/verify-email?token={token}", headers={"Accept-Language": "zh-CN"})
    assert response.status_code == 200
    assert 'lang="zh-CN"' in response.text
    assert "验证你的邮箱" in response.text


def test_verify_landing_explicit_english_overrides_chinese_browser(client, secret) -> None:
    token = make_verify_token("u_id", "user@test.com", _email_verify_secret(), jti="jti_explicit_en")
    client.cookies.set("NEXT_LOCALE", "en-US")
    response = client.get(f"/api/v1/auth/verify-email?token={token}", headers={"Accept-Language": "zh-CN"})
    assert response.status_code == 200
    assert 'lang="en-US"' in response.text
