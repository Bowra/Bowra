"""§C (real-auth) — email-verification ENFORCEMENT tests (Task #8).

The new env flag `BOWHEAD_EMAIL_VERIFY_REQUIRED` (default 0) gates three places:
- register: flag ON + unverified → no session, returns `requires_verification` (verify mail enqueued).
- login: flag ON + unverified → 403 {code: email_not_verified}, no session.
- _current_user: flag ON + unverified → 403 on every authed route EXCEPT the recovery
  endpoints (me / logout / logout-everywhere / resend-verify).

Plus: a public, no-session, no-leak `/resend-verify-public`, the flag-gated `email_verified_init`
(101 auto-members stay auto-verified when the flag is OFF), and the loud dev-bypass startup warning.

flag OFF (default) MUST reproduce the pre-Task-#8 behavior exactly (101 untouched).
"""

from __future__ import annotations

import logging

import aiosqlite
import pytest
from bowhead_core.email_sender import make_verify_token
from bowhead_core.user_store import User, UserStore, _now, hash_password
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def secret():
    return "test-jwt-secret-very-long-for-hmac-256-sufficient"


@pytest.fixture
def env_setup(monkeypatch, secret):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", secret)
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_ENABLED", "1")
    # Disable SMTP — let the outbox queue but don't actually send.
    monkeypatch.delenv("BOWHEAD_SMTP_HOST", raising=False)
    # Default OFF; flag-ON tests opt in explicitly so flag-OFF stays the baseline.
    monkeypatch.delenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", raising=False)


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "test.db")
    await s.initialize()
    from bowhead_core.public_entries import PublicEntryStore

    await PublicEntryStore(s.db_path).initialize()
    return s


async def _seed_team(s: UserStore, team_id: str = "team_t", domain: str | None = None) -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, "
            "default_user_tier, team_credit_balance, team_credit_quota, "
            "tier_credits_json, status, created_at) "
            "VALUES (?, 'TestOrg', ?, 10, 'ultra', 0, 0, "
            "'{\"pro\":5000,\"ultra\":30000}', 'active', ?)",
            (team_id, domain, _now()),
        )
        await db.commit()
    from bowhead_core.public_entries import PublicEntryStore

    registry = PublicEntryStore(s.db_path)
    await registry.initialize()
    await registry.create_bootstrap_active(
        team_id=team_id,
        base_url="http://test.local",
        actor_id="test",
    )


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


async def _user_verified_in_db(s: UserStore, user_id: str) -> int:
    async with (
        aiosqlite.connect(s.db_path) as db,
        db.execute(
            "SELECT email_verified FROM users WHERE id = ?",
            (user_id,),
        ) as cur,
    ):
        row = await cur.fetchone()
    return int(row[0]) if row else -1


async def _verify_outbox_count(s: UserStore, email: str) -> int:
    async with (
        aiosqlite.connect(s.db_path) as db,
        db.execute(
            "SELECT COUNT(*) FROM smtp_outbox WHERE to_email = ? AND purpose = 'verify'",
            (email,),
        ) as cur,
    ):
        row = await cur.fetchone()
    return int(row[0])


# A tiny non-exempt route that depends on _current_user — isolates the gate from any
# real route's business logic. Its path (/api/v1/auth/_probe) is NOT a verify-exempt suffix.
def _build_app(store: UserStore) -> FastAPI:
    from bowhead_core.public_entries import PublicEntryStore
    from bowhead_gateway.public_entry_context import (
        PublicAccessContext,
        reset_public_access_context,
        set_public_access_context,
    )
    from bowhead_gateway.routes import auth as auth_routes

    auth_routes._RATE_BUCKET.clear()
    a = FastAPI()
    a.state.user_store = store
    a.state.public_entry_store = PublicEntryStore(store.db_path)
    a.state.deployment_mode = "saas"

    class _S:
        auth_mode = "prod"
        dev_default_user = "dev_user"

    a.state.settings = _S()

    @a.middleware("http")
    async def _public_entry_context(request, call_next):  # type: ignore[no-untyped-def]
        token = set_public_access_context(
            PublicAccessContext(
                base_url="http://test.local",
                origin="http://test.local",
                base_path="",
                app_root="http://test.local/",
                # The register page's same-origin request carries its browser address; a bare
                # direct Host has no known prefix and cannot address a mailed link.
                match_source="browser_hint",
            ),
        )
        try:
            return await call_next(request)
        finally:
            reset_public_access_context(token)

    a.include_router(auth_routes.router, prefix="/api/v1/auth")

    @a.get("/api/v1/auth/_probe/protected")
    async def _protected(user: User = Depends(auth_routes._current_user)):
        return {"id": user.id}

    return a


@pytest.fixture
def app(env_setup, store):
    return _build_app(store)


@pytest.fixture
def client(app):
    return TestClient(app)


def _set_auth_cookie(client: TestClient, user: User, secret: str) -> None:
    """Mint a real access JWT for an (already-fetched) user and set it as the auth cookie."""
    from bowhead_gateway.routes import auth as auth_routes

    token = auth_routes._issue_jwt(user, secret)
    client.cookies.set("bowhead_jwt", token)


# ─── register: flag OFF (101 baseline) ──────────────────────


@pytest.mark.asyncio
async def test_register_flag_off_auto_logs_in(store, client):
    """flag OFF → register auto-logs-in (token + cookie), no requires_verification (101 unchanged)."""
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "alice@test.com", "password": "Passw0rd1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("requires_verification") is None
    assert body.get("token")
    assert len(list(client.cookies.jar)) >= 1  # session issued


# ─── register: self-registration kill switch (101 lock-down) ─


@pytest.mark.asyncio
async def test_register_disabled_flag_blocks_and_mints_nothing(store, client, monkeypatch):
    """BOWHEAD_REGISTRATION_DISABLED=1 → /register 403, no session, no account row (101 lock-down)."""
    monkeypatch.setenv("BOWHEAD_REGISTRATION_DISABLED", "1")
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "stray@test.com", "password": "Passw0rd1"},
    )
    assert r.status_code == 403, r.text
    assert len(list(client.cookies.jar)) == 0  # no session issued
    assert await store.get_user_by_email("stray@test.com") is None  # no account minted


@pytest.mark.asyncio
async def test_register_disabled_default_off_still_registers(store, client, monkeypatch):
    """Default OFF (116 SaaS) → registration unaffected (no regression)."""
    monkeypatch.delenv("BOWHEAD_REGISTRATION_DISABLED", raising=False)
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "newcustomer@test.com", "password": "Passw0rd1"},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("token")


# ─── register: flag ON ──────────────────────────────────────


@pytest.mark.asyncio
async def test_register_flag_on_requires_verification_no_session(store, client, monkeypatch):
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "bob@test.com", "password": "Passw0rd1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requires_verification"] is True
    assert body["user"]["email"] == "bob@test.com"
    assert body["user"]["email_verified"] is False
    assert "token" not in body  # NO auto-login
    assert len(list(client.cookies.jar)) == 0  # NO session cookie
    # The verify mail was enqueued.
    assert await _verify_outbox_count(store, "bob@test.com") == 1
    # DB row exists, unverified.
    u = await store.get_user_by_email("bob@test.com")
    assert u is not None and u.email_verified is False


@pytest.mark.asyncio
async def test_register_flag_on_auto_member_also_unverified(store, client, monkeypatch):
    """datarc.cn auto-member must ALSO verify when the flag is ON (email_verified_init gate)."""
    from bowhead_gateway.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "_AUTO_MEMBER_DOMAINS_CACHE", None)
    monkeypatch.setenv("BOWHEAD_AUTO_MEMBER_DOMAINS", "test.com")
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    await _seed_team(store, team_id="team_wl", domain="test.com")  # pre-provisioned team for the domain

    r = client.post(
        "/api/v1/auth/register",
        json={"email": "carol@test.com", "password": "Passw0rd1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["requires_verification"] is True
    u = await store.get_user_by_email("carol@test.com")
    assert u is not None
    assert u.tier == "ultra"  # auto-member tier preserved
    assert u.email_verified is False  # but NOT auto-verified (flag ON)


@pytest.mark.asyncio
async def test_register_flag_off_auto_member_stays_verified(store, client, monkeypatch):
    """flag OFF → datarc.cn auto-member is auto-verified (G6 behavior, 101 unchanged)."""
    from bowhead_gateway.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "_AUTO_MEMBER_DOMAINS_CACHE", None)
    monkeypatch.setenv("BOWHEAD_AUTO_MEMBER_DOMAINS", "test.com")
    await _seed_team(store, team_id="team_wl", domain="test.com")

    r = client.post(
        "/api/v1/auth/register",
        json={"email": "dave@test.com", "password": "Passw0rd1"},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("requires_verification") is None  # auto-login
    u = await store.get_user_by_email("dave@test.com")
    assert u is not None and u.email_verified is True
    # auto-verified → NO verify mail enqueued
    assert await _verify_outbox_count(store, "dave@test.com") == 0


# ─── login enforcement ──────────────────────────────────────


@pytest.mark.asyncio
async def test_login_flag_on_unverified_403(store, client, monkeypatch):
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    await _seed_team(store)
    await _seed_user(store, email="u@test.com", verified=False)
    r = client.post("/api/v1/auth/login", json={"email": "u@test.com", "password": "password123"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "email_not_verified"
    assert r.json()["detail"]["email"] == "u@test.com"
    assert len(list(client.cookies.jar)) == 0  # no session


@pytest.mark.asyncio
async def test_login_flag_on_verified_ok(store, client, monkeypatch):
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    await _seed_team(store)
    await _seed_user(store, email="v@test.com", verified=True)
    r = client.post("/api/v1/auth/login", json={"email": "v@test.com", "password": "password123"})
    assert r.status_code == 200, r.text
    assert r.json().get("token")


@pytest.mark.asyncio
async def test_login_flag_off_unverified_ok(store, client):
    """flag OFF → unverified user logs in fine (101 unchanged)."""
    await _seed_team(store)
    await _seed_user(store, email="u@test.com", verified=False)
    r = client.post("/api/v1/auth/login", json={"email": "u@test.com", "password": "password123"})
    assert r.status_code == 200, r.text
    assert r.json().get("token")


@pytest.mark.asyncio
async def test_login_wrong_password_still_401_when_unverified(store, client, monkeypatch):
    """Password check precedes the verify gate — a bad password leaks nothing about verify state."""
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    await _seed_team(store)
    await _seed_user(store, email="u@test.com", verified=False)
    r = client.post("/api/v1/auth/login", json={"email": "u@test.com", "password": "WRONGpass1"})
    assert r.status_code == 401
    # Body is the generic string — the verify gate runs AFTER the password check, so a bad
    # password leaks nothing about verify state (not the structured email_not_verified object).
    assert r.json()["detail"] == "invalid credentials"


# ─── _current_user gate (defense-in-depth + exemptions) ─────


@pytest.mark.asyncio
async def test_current_user_flag_on_unverified_blocked_on_protected(store, client, secret, monkeypatch):
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    await _seed_team(store)
    await _seed_user(store, verified=False)
    user = await store.get_user("u_t")
    assert user is not None
    _set_auth_cookie(client, user, secret)
    r = client.get("/api/v1/auth/_probe/protected")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "email_not_verified"


@pytest.mark.asyncio
async def test_current_user_flag_on_unverified_allowed_on_exempt(store, client, secret, monkeypatch):
    """me / logout / resend-verify stay reachable so the user can recover."""
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    await _seed_team(store)
    await _seed_user(store, verified=False)
    user = await store.get_user("u_t")
    assert user is not None
    _set_auth_cookie(client, user, secret)
    # /me — exempt → 200
    assert client.get("/api/v1/auth/me").status_code == 200
    # /resend-verify — exempt → 200 (enqueues a fresh link)
    assert client.post("/api/v1/auth/resend-verify").status_code == 200
    assert await _verify_outbox_count(store, "u@test.com") >= 1
    # /logout-everywhere — exempt AND actually _current_user-gated (plain /logout has no
    # _current_user dependency, so it would pass regardless — this exercises the real exemption).
    assert client.post("/api/v1/auth/logout-everywhere").status_code == 200


@pytest.mark.asyncio
async def test_current_user_flag_on_unverified_blocked_on_me_subpath(store, client, secret, monkeypatch):
    """Suffix-match boundary: /auth/me is exempt but /auth/me/workspaces is NOT — stays gated."""
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    await _seed_team(store)
    await _seed_user(store, verified=False)
    user = await store.get_user("u_t")
    assert user is not None
    _set_auth_cookie(client, user, secret)
    r = client.get("/api/v1/auth/me/workspaces")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "email_not_verified"


@pytest.mark.asyncio
async def test_current_user_flag_on_verified_allowed_on_protected(store, client, secret, monkeypatch):
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    await _seed_team(store)
    await _seed_user(store, verified=True)
    user = await store.get_user("u_t")
    assert user is not None
    _set_auth_cookie(client, user, secret)
    r = client.get("/api/v1/auth/_probe/protected")
    assert r.status_code == 200
    assert r.json()["id"] == "u_t"


@pytest.mark.asyncio
async def test_current_user_flag_off_unverified_allowed_on_protected(store, client, secret):
    """flag OFF → no enforcement at the chokepoint (101 unchanged)."""
    await _seed_team(store)
    await _seed_user(store, verified=False)
    user = await store.get_user("u_t")
    assert user is not None
    _set_auth_cookie(client, user, secret)
    r = client.get("/api/v1/auth/_probe/protected")
    assert r.status_code == 200


# ─── resend-verify-public (no session, no leak) ─────────────


@pytest.mark.asyncio
async def test_resend_public_unverified_enqueues(store, client, monkeypatch):
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    await _seed_team(store)
    await _seed_user(store, email="u@test.com", verified=False)
    r = client.post("/api/v1/auth/resend-verify-public", json={"email": "u@test.com"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert await _verify_outbox_count(store, "u@test.com") == 1


@pytest.mark.asyncio
async def test_resend_public_unknown_email_no_leak(store, client):
    """Nonexistent email → 200, nothing enqueued (mirrors /forgot-password)."""
    r = client.post("/api/v1/auth/resend-verify-public", json={"email": "ghost@test.com"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert await _verify_outbox_count(store, "ghost@test.com") == 0


@pytest.mark.asyncio
async def test_resend_public_already_verified_noop(store, client):
    await _seed_team(store)
    await _seed_user(store, email="v@test.com", verified=True)
    r = client.post("/api/v1/auth/resend-verify-public", json={"email": "v@test.com"})
    assert r.status_code == 200
    assert await _verify_outbox_count(store, "v@test.com") == 0


@pytest.mark.asyncio
async def test_resend_public_case_insensitive_email(store, client):
    """Email is normalized (lower/strip) so mixed-case still finds the user."""
    await _seed_team(store)
    await _seed_user(store, email="u@test.com", verified=False)
    r = client.post("/api/v1/auth/resend-verify-public", json={"email": "  U@Test.com "})
    assert r.status_code == 200
    assert await _verify_outbox_count(store, "u@test.com") == 1


# ─── end-to-end: register (flag ON) → verify → login ────────


@pytest.mark.asyncio
async def test_e2e_register_verify_login_loop_closes(store, client, secret, monkeypatch):
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    # 1. register → requires_verification, no session
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "eve@test.com", "password": "Passw0rd1"},
    )
    assert r.status_code == 200 and r.json()["requires_verification"] is True

    # login now is blocked
    r = client.post("/api/v1/auth/login", json={"email": "eve@test.com", "password": "Passw0rd1"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "email_not_verified"

    # 2. consume the verify link (mint from the stored jti)
    u = await store.get_user_by_email("eve@test.com")
    assert u is not None
    jti = await store.get_verify_token_jti(u.id)
    from bowhead_gateway.routes.auth import derive_jwt_secret_keys

    token = make_verify_token(u.id, u.email, derive_jwt_secret_keys("email-verify")[0][1], jti=jti)
    r = client.post("/api/v1/auth/verify-email", json={"token": token})
    assert r.status_code == 200 and r.json()["status"] == "verified"
    assert await _user_verified_in_db(store, u.id) == 1

    # 3. login now succeeds
    r = client.post("/api/v1/auth/login", json={"email": "eve@test.com", "password": "Passw0rd1"})
    assert r.status_code == 200, r.text
    assert r.json().get("token")


# ─── assert_prod_auth_safe: loud dev-bypass warning + hard aborts ─


def test_assert_prod_auth_safe_warns_on_saas_dev_fallback(monkeypatch, caplog):
    """saas+dev+fallback survives both hard-aborts but MUST log critical (the 免密登录 gap)."""
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    with caplog.at_level(logging.CRITICAL):
        assert_prod_auth_safe()  # must NOT raise
    assert any("DEV AUTH BYPASS ACTIVE" in rec.message for rec in caplog.records)


def test_assert_prod_auth_safe_aborts_on_prod_fallback(monkeypatch):
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    with pytest.raises(RuntimeError):
        assert_prod_auth_safe()


def test_assert_prod_auth_safe_aborts_on_local_fallback(monkeypatch):
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    with pytest.raises(RuntimeError):
        assert_prod_auth_safe()


def test_assert_prod_auth_safe_clean_when_no_fallback(monkeypatch, caplog):
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    with caplog.at_level(logging.CRITICAL):
        assert_prod_auth_safe()
    assert not any("DEV AUTH BYPASS ACTIVE" in rec.message for rec in caplog.records)


def test_assert_prod_auth_safe_clean_dev_mode_no_fallback(monkeypatch, caplog):
    """dev mode but fallback OFF → NO warning. Pins the warning to the fallback flag, not the mode."""
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    with caplog.at_level(logging.CRITICAL):
        assert_prod_auth_safe()
    assert not any("DEV AUTH BYPASS ACTIVE" in rec.message for rec in caplog.records)


# ─── folded from CTO review: rate limits, lockout-misconfig, join path, /refresh gate ─


@pytest.mark.asyncio
async def test_resend_public_per_user_rate_limit(store, client):
    """Two resends for the same unverified email in one window → both 200, but only ONE mail (1/min/user)."""
    await _seed_team(store)
    await _seed_user(store, email="u@test.com", verified=False)
    assert client.post("/api/v1/auth/resend-verify-public", json={"email": "u@test.com"}).status_code == 200
    assert client.post("/api/v1/auth/resend-verify-public", json={"email": "u@test.com"}).status_code == 200
    assert await _verify_outbox_count(store, "u@test.com") == 1  # 2nd blocked by the per-user cap


@pytest.mark.asyncio
async def test_resend_public_per_ip_rate_limit_429(store, client):
    """6th resend-public from the same IP in the window → 429 (shared register/login bucket)."""
    await _seed_team(store)
    await _seed_user(store, email="u@test.com", verified=False)
    for _ in range(5):
        assert (
            client.post(
                "/api/v1/auth/resend-verify-public",
                json={"email": "ghost@test.com"},
            ).status_code
            == 200
        )
    assert (
        client.post(
            "/api/v1/auth/resend-verify-public",
            json={"email": "ghost@test.com"},
        ).status_code
        == 429
    )


@pytest.mark.asyncio
async def test_required_implies_enabled_no_lockout(store, client, monkeypatch):
    """REQUIRED=1 + ENABLED=0 must NOT brick signups — REQUIRED implies ENABLED so the mail still sends."""
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_ENABLED", "0")
    r = client.post("/api/v1/auth/register", json={"email": "fred@test.com", "password": "Passw0rd1"})
    assert r.status_code == 200 and r.json()["requires_verification"] is True
    assert await _verify_outbox_count(store, "fred@test.com") == 1  # mail STILL sent despite ENABLED=0
    # resend-public path is reachable too (returns 200; same per-user window keeps the count at 1)
    assert (
        client.post(
            "/api/v1/auth/resend-verify-public",
            json={"email": "fred@test.com"},
        ).status_code
        == 200
    )


@pytest.mark.asyncio
async def test_register_flag_on_direct_team_join_requires_invite(store, client, monkeypatch):
    """Flag ON + direct body.team_id join path is rejected; existing-team joins require invite."""
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    await _seed_team(store, team_id="team_join")
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "grace@other.com", "password": "Passw0rd1", "team_id": "team_join"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "direct_team_join_disabled"
    u = await store.get_user_by_email("grace@other.com")
    assert u is None


@pytest.mark.asyncio
async def test_refresh_flag_on_unverified_blocked_and_revoked(store, client, secret, monkeypatch):
    """Flag ON: an unverified user holding a refresh cookie cannot rotate it into a fresh session,
    and the token is revoked (no indefinite 30d keep-alive)."""
    import secrets as _s

    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", "1")
    await _seed_team(store)
    await _seed_user(store, verified=False)
    raw = _s.token_urlsafe(16)
    await store.record_refresh_token(
        user_id="u_t",
        raw_token=raw,
        expires_at="2099-01-01T00:00:00+00:00",
        user_agent=None,
        ip=None,
    )
    client.cookies.set("bowhead_refresh", raw)
    r = client.post("/api/v1/auth/refresh")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "email_not_verified"
    # token was REVOKED (not just the cookie cleared) → replay 401s
    client.cookies.set("bowhead_refresh", raw)
    assert client.post("/api/v1/auth/refresh").status_code == 401


@pytest.mark.asyncio
async def test_refresh_flag_off_unverified_ok(store, client, secret):
    """Flag OFF: refresh works for an unverified user (101 unchanged)."""
    import secrets as _s

    await _seed_team(store)
    await _seed_user(store, verified=False)
    raw = _s.token_urlsafe(16)
    await store.record_refresh_token(
        user_id="u_t",
        raw_token=raw,
        expires_at="2099-01-01T00:00:00+00:00",
        user_agent=None,
        ip=None,
    )
    client.cookies.set("bowhead_refresh", raw)
    r = client.post("/api/v1/auth/refresh")
    assert r.status_code == 200, r.text
    assert r.json().get("token")
