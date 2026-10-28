"""Registration and verification mail without a registered public entry.

A new SaaS Team has no public entry, so the verification link depends on the request's address. An
optional link that cannot be addressed must not turn an already created account into a 500, and a
resend must not invalidate the previous link before the new one can be built.
"""

from __future__ import annotations

import aiosqlite
import httpx2
import jwt
import pytest
from bowhead_core.public_entries import PublicEntryStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.public_entry_context import PublicEntryContextMiddleware
from bowhead_gateway.routes import auth as auth_routes
from fastapi import FastAPI

pytestmark = pytest.mark.unit

_INTERNAL = "http://127.0.0.1:8000"


@pytest.fixture
async def app(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test-jwt-secret-very-long-for-hmac-256-sufficient")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.setenv("BOWHEAD_ACCOUNT_CREATION_POLICY", "open")
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFICATION_POLICY", "not_required")
    for name in ("BOWHEAD_EMAIL_VERIFY_ENABLED", "BOWHEAD_EMAIL_VERIFY_REQUIRED", "BOWHEAD_REGISTRATION_DISABLED", "BOWHEAD_SMTP_HOST"):
        monkeypatch.delenv(name, raising=False)
    auth_routes._RATE_BUCKET.clear()
    users = UserStore(tmp_path / "users.db")
    await users.initialize()
    entries = PublicEntryStore(users.db_path)
    await entries.initialize()
    application = FastAPI()
    application.state.user_store = users
    application.state.public_entry_store = entries
    application.state.deployment_mode = "saas"

    class _Settings:
        auth_mode = "prod"
        dev_default_user = "dev_user"

    application.state.settings = _Settings()
    application.add_middleware(PublicEntryContextMiddleware)
    application.include_router(auth_routes.router, prefix="/api/v1/auth")
    return application, users


def _client(application: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=application), base_url=_INTERNAL)


async def _verify_mail(users: UserStore, email: str) -> list[str]:
    async with (
        aiosqlite.connect(users.db_path) as db,
        db.execute("SELECT body FROM smtp_outbox WHERE to_email=? AND purpose='verify'", (email,)) as cur,
    ):
        return [row[0] for row in await cur.fetchall()]


async def _jti(users: UserStore, email: str) -> str | None:
    user = await users.get_user_by_email(email)
    assert user is not None
    async with aiosqlite.connect(users.db_path) as db, db.execute("SELECT verify_token_jti FROM users WHERE id=?", (user.id,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def test_register_without_any_public_address_creates_the_account_and_signs_in(app) -> None:
    application, users = app
    async with _client(application) as client:
        response = await client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": "password123", "team_name": "Acme"})

    assert response.status_code == 200, response.text
    assert response.json().get("token") and not response.json().get("requires_verification")
    assert await users.get_user_by_email("a@example.com") is not None
    assert await _verify_mail(users, "a@example.com") == []


async def test_register_under_not_required_mails_nothing_even_with_a_browser_address(app) -> None:
    application, users = app
    async with _client(application) as client:
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "n@example.com", "password": "password123", "team_name": "Acme"},
            headers={"X-Bowhead-Public-Base": "https://public.example/bowhead"},
        )

    assert response.status_code == 200, response.text
    assert response.json().get("token")
    assert await _verify_mail(users, "n@example.com") == []
    assert await _jti(users, "n@example.com") is None


@pytest.mark.parametrize("prefix", ["", "/bowhead"])
async def test_register_from_the_browser_mails_a_link_on_its_entry_prefix(app, monkeypatch, prefix: str) -> None:
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFICATION_POLICY", "smtp_required")
    application, users = app
    async with _client(application) as client:
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "b@example.com", "password": "password123", "team_name": "Acme"},
            headers={"X-Bowhead-Public-Base": f"https://public.example:8443{prefix}"},
        )

    assert response.status_code == 200, response.text
    assert response.json().get("requires_verification")
    [body] = await _verify_mail(users, "b@example.com")
    assert f"https://public.example:8443{prefix}/api/v1/auth/verify-email?token=" in body


async def test_resend_without_a_public_address_explains_and_keeps_the_previous_link(app, monkeypatch) -> None:
    application, users = app

    async def _no_rate_limit(self, *args, **kwargs) -> bool:
        return True

    monkeypatch.setattr(UserStore, "rate_limit_check", _no_rate_limit)
    async with _client(application) as client:
        registered = await client.post(
            "/api/v1/auth/register",
            json={"email": "c@example.com", "password": "password123", "team_name": "Acme"},
            headers={"X-Bowhead-Public-Base": "https://public.example/bowhead"},
        )
        assert registered.status_code == 200, registered.text
        auth = {"Authorization": f"Bearer {registered.json()['token']}"}
        # A member verifying from their profile: the browser request carries its address.
        first = await client.post("/api/v1/auth/resend-verify", headers={**auth, "X-Bowhead-Public-Base": "https://public.example/bowhead"})
        assert first.status_code == 200, first.text
        previous = await _jti(users, "c@example.com")
        resent = await client.post("/api/v1/auth/resend-verify", headers=auth)

    assert resent.status_code == 409, resent.text
    assert resent.json()["detail"]["code"] == "public_entry_required"
    assert previous is not None and await _jti(users, "c@example.com") == previous
    assert len(await _verify_mail(users, "c@example.com")) == 1


# ─── verification mail outcome + register receipt ──────────────────────────────


@pytest.fixture
def smtp_required(monkeypatch):
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFICATION_POLICY", "smtp_required")
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "smtp.example.com")
    # Several register/resend calls per test; the shared per-IP bucket is covered elsewhere.
    monkeypatch.setattr(auth_routes, "_check_rate_limit", lambda request: None)


async def _register(client: httpx2.AsyncClient, email: str, base: str | None = "https://public.example") -> httpx2.Response:
    headers = {"X-Bowhead-Public-Base": base} if base else {}
    return await client.post("/api/v1/auth/register", json={"email": email, "password": "password123", "team_name": "Acme"}, headers=headers)


async def _resend(client: httpx2.AsyncClient, email: str, receipt: str | None, base: str | None = "https://public.example") -> httpx2.Response:
    headers = {"X-Bowhead-Public-Base": base} if base else {}
    body = {"email": email} if receipt is None else {"email": email, "receipt": receipt}
    return await client.post("/api/v1/auth/resend-verify-public", json=body, headers=headers)


@pytest.mark.parametrize("prefix", ["", "/bowhead"])
async def test_register_reports_the_mail_as_queued_with_a_receipt(app, smtp_required, prefix: str) -> None:
    application, users = app
    async with _client(application) as client:
        response = await _register(client, "q@example.com", f"https://public.example{prefix}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requires_verification"] is True
    assert body["verification_mail"] == {"status": "queued"}
    assert isinstance(body["verification_receipt"], str) and body["verification_receipt"]
    [mail] = await _verify_mail(users, "q@example.com")
    assert f"https://public.example{prefix}/api/v1/auth/verify-email?token=" in mail


async def test_register_with_an_unaddressable_link_keeps_the_account_and_reports_not_sent(app, smtp_required) -> None:
    application, users = app
    async with _client(application) as client:
        response = await _register(client, "u@example.com", base=None)

    assert response.status_code == 200, response.text
    outcome = response.json()["verification_mail"]
    assert outcome["status"] == "not_sent" and outcome["reason"] == "public_entry_required"
    assert outcome["reference"]
    assert await users.get_user_by_email("u@example.com") is not None
    assert await _verify_mail(users, "u@example.com") == []


async def test_register_outbox_failure_keeps_the_account_and_reports_not_sent(app, smtp_required, monkeypatch, caplog) -> None:
    application, users = app

    async def _broken_outbox(self, **kwargs) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(UserStore, "enqueue_smtp", _broken_outbox)
    async with _client(application) as client:
        response = await _register(client, "f@example.com")

    assert response.status_code == 200, response.text
    outcome = response.json()["verification_mail"]
    assert outcome["status"] == "not_sent" and outcome["reason"] == "delivery_unavailable"
    # The reference the registrant quotes is the one in the gateway log.
    assert outcome["reference"] in caplog.text
    assert "database is locked" not in response.text
    assert await users.get_user_by_email("f@example.com") is not None


async def test_register_without_mail_configuration_reports_not_sent(app, smtp_required, monkeypatch) -> None:
    monkeypatch.delenv("BOWHEAD_SMTP_HOST", raising=False)
    application, _users = app
    async with _client(application) as client:
        response = await _register(client, "n@example.com")

    assert response.status_code == 200, response.text
    outcome = response.json()["verification_mail"]
    assert outcome["status"] == "not_sent" and outcome["reason"] == "email_not_configured"


@pytest.mark.parametrize("prefix", ["", "/bowhead"])
async def test_receipt_backed_resend_reports_the_real_outcome(app, smtp_required, monkeypatch, prefix: str) -> None:
    application, users = app

    async def _no_rate_limit(self, *args, **kwargs) -> bool:
        return True

    monkeypatch.setattr(UserStore, "rate_limit_check", _no_rate_limit)
    async with _client(application) as client:
        registered = await _register(client, "r@example.com", base=None)
        receipt = registered.json()["verification_receipt"]
        failed = await _resend(client, "r@example.com", receipt, base=None)
        queued = await _resend(client, "r@example.com", receipt, base=f"https://public.example{prefix}")

    assert failed.status_code == 200, failed.text
    assert failed.json()["verification_mail"]["status"] == "not_sent"
    assert failed.json()["verification_mail"]["reason"] == "public_entry_required"
    assert queued.json() == {"ok": True, "verification_mail": {"status": "queued"}}
    [mail] = await _verify_mail(users, "r@example.com")
    assert f"https://public.example{prefix}/api/v1/auth/verify-email?token=" in mail


async def test_resend_without_a_valid_receipt_keeps_the_no_leak_response(app, smtp_required, monkeypatch) -> None:
    application, users = app

    async def _no_rate_limit(self, *args, **kwargs) -> bool:
        return True

    monkeypatch.setattr(UserStore, "rate_limit_check", _no_rate_limit)
    async with _client(application) as client:
        mine = (await _register(client, "a1@example.com", base=None)).json()
        other = (await _register(client, "b1@example.com", base=None)).json()
        user = await users.get_user_by_email("a1@example.com")
        assert user is not None
        expired = jwt.encode(
            {"purpose": "register_verify_receipt", "sub": user.id, "email": user.email, "iat": 1, "exp": 2},
            auth_routes._register_receipt_keys()[0],
            algorithm="HS256",
        )
        forged = jwt.encode(
            {"purpose": "register_verify_receipt", "sub": user.id, "email": user.email, "exp": 4102444800},
            "not-the-derived-receipt-key-but-long-enough-for-hs256",
            algorithm="HS256",
        )
        responses = [
            await _resend(client, "a1@example.com", None, base=None),
            await _resend(client, "a1@example.com", "garbage", base=None),
            await _resend(client, "a1@example.com", expired, base=None),
            await _resend(client, "a1@example.com", forged, base=None),
            await _resend(client, "a1@example.com", other["verification_receipt"], base=None),
            # A valid receipt says nothing about an address that has no account.
            await _resend(client, "ghost@example.com", mine["verification_receipt"], base=None),
        ]

    for response in responses:
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True}


async def test_receipt_backed_resend_reports_the_cooldown_only_to_the_registrant(app, smtp_required) -> None:
    application, _users = app
    async with _client(application) as client:
        receipt = (await _register(client, "c1@example.com")).json()["verification_receipt"]
        first = await _resend(client, "c1@example.com", receipt)
        anonymous = await _resend(client, "c1@example.com", None)
        registrant = await _resend(client, "c1@example.com", receipt)

    assert first.json()["verification_mail"] == {"status": "queued"}
    assert anonymous.status_code == 200 and anonymous.json() == {"ok": True}
    assert registrant.status_code == 429
    assert registrant.json()["detail"]["code"] == "resend_too_frequent"


async def test_register_receipt_grants_no_session_or_verification(app, smtp_required) -> None:
    from bowhead_core.email_sender import decode_verify_token

    application, users = app
    async with _client(application) as client:
        receipt = (await _register(client, "g@example.com")).json()["verification_receipt"]
        as_bearer = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {receipt}"})
        client.cookies.set("bowhead_jwt", receipt)
        as_cookie = await client.get("/api/v1/auth/me")
        await client.get(f"/api/v1/auth/verify-email?token={receipt}", follow_redirects=False)

    assert as_bearer.status_code == 401
    assert as_cookie.status_code == 401
    assert decode_verify_token(receipt, auth_routes._email_verify_secret_keys()) is None
    user = await users.get_user_by_email("g@example.com")
    assert user is not None and not user.email_verified
