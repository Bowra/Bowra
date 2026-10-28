"""email-config sprint M1c — admin email-config 端点 + resend 断链回归 gateway 测试.

设计:docs/sprints/email-config-2026-06-12/tech-design.md §6b/§6d/§10。
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
from bowhead_core.user_store import UserStore, _now, hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "test-secret-long-enough-for-hs256-32-bytes"
FERNET = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "ec.db")
    await s.initialize()
    return s


async def _seed_team(s, team_id="team_ec") -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, 'O', 10, 'ultra', 5000, 30000, "
            "'{\"pro\":5000,\"ultra\":30000}', 'active', ?)",
            (team_id, _now()),
        )
        await db.commit()


async def _seed_user(s, user_id, email, *, team_id="team_ec", is_creator=False, role=None) -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "tier, credit_balance, credit_quota, status, created_at) "
            "VALUES (?, ?, ?, 1, ?, 'ultra', 0, 0, 'active', ?)",
            (user_id, team_id, email, hash_password("pwd123"), _now()),
        )
        if is_creator:
            await db.execute(
                "UPDATE teams SET created_by_user_id = ? WHERE id = ?",
                (user_id, team_id),
            )
        await db.commit()


def _jwt(user_id, secret=SECRET, pwd_v=0):
    import time

    import jwt as _jwt

    return _jwt.encode(
        {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600, "pwd_v": pwd_v},
        secret,
        algorithm="HS256",
    )


@pytest.fixture
def app(store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", FERNET)
    monkeypatch.delenv("BOWHEAD_SMTP_HOST", raising=False)
    monkeypatch.delenv("BOWHEAD_TEAM_EMAIL_CONFIG", raising=False)
    from bowhead_gateway.routes import admin_email_config as ec_routes
    from bowhead_gateway.routes import auth as auth_routes

    auth_routes._RATE_BUCKET.clear()
    a = FastAPI()
    a.state.user_store = store

    class _S:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    a.state.settings = _S()
    a.include_router(auth_routes.router, prefix="/api/v1/auth")
    a.include_router(ec_routes.router, prefix="/api/v1/admin/email-config")
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


_VALID = {"host": "8.8.8.8", "port": 587, "user": "u", "password": "secret", "from_addr": "no-reply@x.com", "from_name": "Acme", "tls": True}


# ── gate ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_forbidden(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_member", "m@x.com")  # Neither creator nor admin.
    r = client.get("/api/v1/admin/email-config", cookies={"bowhead_jwt": _jwt("u_member")})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_anon_unauthorized(client, store):
    await _seed_team(store)
    assert client.get("/api/v1/admin/email-config").status_code == 401


# ── GET three states ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_none_then_team(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_c", "c@x.com", is_creator=True)
    jwt = _jwt("u_c")
    r = client.get("/api/v1/admin/email-config", cookies={"bowhead_jwt": jwt})
    assert r.json()["configured"] == "none"
    client.put("/api/v1/admin/email-config", json=_VALID, cookies={"bowhead_jwt": jwt})
    r2 = client.get("/api/v1/admin/email-config", cookies={"bowhead_jwt": jwt})
    body = r2.json()
    assert body["configured"] == "team"
    # Password is fingerprinted only, never returned plaintext.
    assert body["config"]["password_set"] is True
    assert "password_fingerprint" not in body["config"]  # Hash no longer returned.
    assert "secret" not in json.dumps(body)


@pytest.mark.asyncio
async def test_get_env_no_details(client, store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "env-relay.internal")
    await _seed_team(store)
    await _seed_user(store, "u_c", "c@x.com", is_creator=True)
    r = client.get("/api/v1/admin/email-config", cookies={"bowhead_jwt": _jwt("u_c")})
    body = r.json()
    assert body["configured"] == "env"
    assert "config" not in body  # Never expose env host to team admins.
    assert "env-relay" not in json.dumps(body)


# ── PUT validation / password retention / migration ───────────


@pytest.mark.asyncio
async def test_put_accepts_nonstandard_port_in_local_deployment(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_c", "c@x.com", is_creator=True)
    r = client.put("/api/v1/admin/email-config", json={**_VALID, "port": 8080}, cookies={"bowhead_jwt": _jwt("u_c")})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_put_password_preserved_when_blank(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_c", "c@x.com", is_creator=True)
    jwt = _jwt("u_c")
    client.put("/api/v1/admin/email-config", json=_VALID, cookies={"bowhead_jwt": jwt})
    # Repeat PUT with empty password → preserve old password.
    client.put("/api/v1/admin/email-config", json={**_VALID, "password": "", "from_name": "New"}, cookies={"bowhead_jwt": jwt})
    cfg = await store.get_team_email_config_decrypted("team_ec")
    assert cfg["password"] == "secret"  # Preserved.
    assert cfg["from_name"] == "New"  # Other fields updated.


@pytest.mark.asyncio
async def test_put_plaintext_gate_500(client, store, monkeypatch):
    # No FERNET_KEY + plaintext disallowed → PUT returns 500.
    monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)
    monkeypatch.delenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", raising=False)
    await _seed_team(store)
    await _seed_user(store, "u_c", "c@x.com", is_creator=True)
    r = client.put("/api/v1/admin/email-config", json=_VALID, cookies={"bowhead_jwt": _jwt("u_c")})
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "email_config_encryption_required"


@pytest.mark.asyncio
async def test_delete_clears(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_c", "c@x.com", is_creator=True)
    jwt = _jwt("u_c")
    client.put("/api/v1/admin/email-config", json=_VALID, cookies={"bowhead_jwt": jwt})
    client.delete("/api/v1/admin/email-config", cookies={"bowhead_jwt": jwt})
    assert (await store.get_team_email_config_decrypted("team_ec")) == {}


# ── Audit excludes passwords ──────────────────────────────────


@pytest.mark.asyncio
async def test_audit_has_no_password(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_c", "c@x.com", is_creator=True)
    client.put("/api/v1/admin/email-config", json=_VALID, cookies={"bowhead_jwt": _jwt("u_c")})
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT payload_json FROM admin_audit_log WHERE action = 'email_config_set'",
        ) as cur:
            rows = await cur.fetchall()
    assert rows
    for r in rows:
        assert "secret" not in (r["payload_json"] or "")
        assert "password" not in json.loads(r["payload_json"] or "{}")


# ── Test endpoint ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_not_configured_422(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_c", "c@x.com", is_creator=True)
    r = client.post("/api/v1/admin/email-config/test", cookies={"bowhead_jwt": _jwt("u_c")})
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": "email_not_configured"}


@pytest.mark.asyncio
async def test_test_rate_limited(client, store, monkeypatch):
    # Route binds via from ... import deliver_email; patch the reference on the route module.
    monkeypatch.setattr(
        "bowhead_gateway.routes.admin_email_config.deliver_email",
        _fake_deliver_ok,
    )
    rate_limit_calls: list[tuple[str, int, int]] = []
    original_rate_limit_check = store.rate_limit_check

    async def record_rate_limit(bucket_key: str, *, limit: int, window_s: int) -> bool:
        rate_limit_calls.append((bucket_key, limit, window_s))
        return await original_rate_limit_check(bucket_key, limit=limit, window_s=window_s)

    monkeypatch.setattr(store, "rate_limit_check", record_rate_limit)
    await _seed_team(store)
    await _seed_user(store, "u_c", "c@x.com", is_creator=True)
    jwt = _jwt("u_c")
    client.put("/api/v1/admin/email-config", json=_VALID, cookies={"bowhead_jwt": jwt})
    r1 = client.post("/api/v1/admin/email-config/test", cookies={"bowhead_jwt": jwt})
    assert r1.status_code == 200
    assert r1.json() == {
        "ok": True,
        "source": "team",
        "error_code": None,
        "error": None,
    }
    r2 = client.post("/api/v1/admin/email-config/test", cookies={"bowhead_jwt": jwt})
    assert r2.status_code == 429
    assert r2.json()["detail"] == {"code": "rate_limited"}
    assert rate_limit_calls == [("email_test:team:team_ec", 1, 2), ("email_test:team:team_ec", 1, 2)]


@pytest.mark.asyncio
async def test_test_delivery_failure_hides_smtp_diagnostic(
    client,
    store,
    monkeypatch,
):
    async def fake_deliver_failed(*args, **kwargs):
        del args, kwargs
        return False, "SMTPAuthenticationError secret relay diagnostic"

    monkeypatch.setattr(
        "bowhead_gateway.routes.admin_email_config.deliver_email",
        fake_deliver_failed,
    )
    await _seed_team(store)
    await _seed_user(store, "u_c", "c@x.com", is_creator=True)
    jwt = _jwt("u_c")
    client.put("/api/v1/admin/email-config", json=_VALID, cookies={"bowhead_jwt": jwt})

    response = client.post(
        "/api/v1/admin/email-config/test",
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "source": "team",
        "error_code": "email_test_delivery_failed",
        "error": "email_test_delivery_failed",
    }
    assert "secret relay diagnostic" not in response.text


@pytest.mark.asyncio
async def test_test_email_rate_limit_does_not_limit_business_outbox(client, store, monkeypatch):
    """测试接口被限流后，邀请/验证等业务邮件仍可正常进入 SMTP outbox。"""
    monkeypatch.setattr(
        "bowhead_gateway.routes.admin_email_config.deliver_email",
        _fake_deliver_ok,
    )
    await _seed_team(store)
    await _seed_user(store, "u_c", "c@x.com", is_creator=True)
    jwt = _jwt("u_c")
    client.put("/api/v1/admin/email-config", json=_VALID, cookies={"bowhead_jwt": jwt})

    assert client.post("/api/v1/admin/email-config/test", cookies={"bowhead_jwt": jwt}).status_code == 200
    assert client.post("/api/v1/admin/email-config/test", cookies={"bowhead_jwt": jwt}).status_code == 429

    outbox_id = await store.enqueue_smtp(
        to_email="invitee@x.com",
        subject="邀请加入 Bowhead",
        body="业务邮件",
        purpose="team_invite",
        team_id="team_ec",
    )
    claimed = await store.claim_pending_smtp()
    assert [row["id"] for row in claimed] == [outbox_id]


async def _fake_deliver_ok(to, subject, body, *, config, timeout=15.0):
    return True, None


def test_flag_off_router_disabled(monkeypatch):
    monkeypatch.setenv("BOWHEAD_TEAM_EMAIL_CONFIG", "0")
    from bowhead_gateway.routes import admin_email_config as ec_routes

    assert ec_routes.email_config_router_enabled() is False


def test_saas_router_enabled(monkeypatch):
    monkeypatch.setenv("BOWHEAD_TEAM_EMAIL_CONFIG", "1")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    from bowhead_gateway.routes import admin_email_config as ec_routes

    assert ec_routes.email_config_router_enabled() is True


@pytest.mark.asyncio
async def test_saas_admin_can_read_save_test_and_clear(client, store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    from bowhead_gateway.routes import admin_email_config as ec_routes

    monkeypatch.setattr(ec_routes, "deliver_email", _fake_deliver_ok)
    await _seed_team(store)
    await _seed_user(store, "u_c", "c@x.com", is_creator=True)
    jwt = {"bowhead_jwt": _jwt("u_c")}

    assert client.get("/api/v1/admin/email-config", cookies=jwt).json()["configured"] == "none"
    saved = client.put("/api/v1/admin/email-config", json=_VALID, cookies=jwt)
    assert saved.status_code == 200, saved.text
    body = client.get("/api/v1/admin/email-config", cookies=jwt).json()
    assert body["configured"] == "team" and body["config"]["host"] == "8.8.8.8"
    tested = client.post("/api/v1/admin/email-config/test", cookies=jwt)
    assert tested.status_code == 200 and tested.json()["ok"] is True
    assert client.delete("/api/v1/admin/email-config", cookies=jwt).status_code == 200
    assert client.get("/api/v1/admin/email-config", cookies=jwt).json()["configured"] == "none"


@pytest.mark.asyncio
async def test_saas_member_cannot_read_or_save(client, store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    await _seed_team(store)
    await _seed_user(store, "u_member", "m@x.com")
    jwt = {"bowhead_jwt": _jwt("u_member")}

    assert client.get("/api/v1/admin/email-config", cookies=jwt).status_code == 403
    assert client.put("/api/v1/admin/email-config", json=_VALID, cookies=jwt).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "resolved", "port", "code"),
    [
        ("10.0.0.5", None, 587, "smtp_host_blocked"),
        ("127.0.0.1", None, 587, "smtp_host_blocked"),
        ("smtp.internal.example", "192.168.1.20", 587, "smtp_host_blocked"),
        ("8.8.8.8", None, 8080, "smtp_port_unsupported"),
    ],
)
async def test_saas_rejects_non_public_smtp_targets(client, store, monkeypatch, host, resolved, port, code):
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    import bowhead_router.ssrf_guard as guard

    monkeypatch.setattr(guard.socket, "getaddrinfo", lambda *_a, **_k: [(2, 1, 6, "", (resolved, 0))])
    await _seed_team(store)
    await _seed_user(store, "u_c", "c@x.com", is_creator=True)

    response = client.put(
        "/api/v1/admin/email-config",
        json={**_VALID, "host": host, "port": port},
        cookies={"bowhead_jwt": _jwt("u_c")},
    )

    assert response.status_code == 422, response.text
    assert [issue["code"] for issue in response.json()["detail"]["issues"]] == [code]
    assert client.get("/api/v1/admin/email-config", cookies={"bowhead_jwt": _jwt("u_c")}).json()["configured"] == "none"
