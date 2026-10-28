"""Sprint β G7 — billing admin endpoint tests."""

from __future__ import annotations

import json

import aiosqlite
import pytest
from bowhead_core.user_store import UserStore, _now, hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
async def store(tmp_path):
    # Filename must be bowhead.db: `upgrade(target="core")` resolves only the **directory** from the URL;
    # target filename is fixed at TARGET_FILES["core"] (migrate.py:12), so another name sends migration elsewhere.
    s = UserStore(tmp_path / "bowhead.db")
    # `GET /admin/llm-config` projects team LLM readiness when model_discovery_v2 is enabled;
    # those tables (team_llm_v2_enrollments / team_llm_provider_identities…) are created by alembic
    # `0005_team_llm_verification`, **no longer by Store.initialize() DDL**
    # (see docs/design/数据库迁移.md). A fixture using only initialize() leaves the route without those tables
    # and fails closed with 503. Same approach as test_team_llm_config_api.py's store fixture.
    import sqlite3

    from bowhead_cli.migrate import upgrade
    from bowhead_core.migrations.baseline_schema import initial_schema

    with sqlite3.connect(s.db_path) as connection:
        connection.executescript(initial_schema("core"))
    upgrade(f"sqlite+aiosqlite:///{s.db_path}", target="core")
    await s.initialize()
    return s


async def _seed_team(s, team_id="team_g7", *, balance=5000, quota=30000):
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, 'O', 10, 'ultra', ?, ?, "
            "'{\"pro\":5000,\"ultra\":30000}', 'active', ?)",
            (team_id, balance, quota, _now()),
        )
        await db.commit()


async def _seed_user(s, user_id, email, *, team_id="team_g7", is_creator=False):
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
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    monkeypatch.delenv("BOWHEAD_SMTP_HOST", raising=False)
    from bowhead_gateway.routes import admin_billing as admin_billing_routes
    from bowhead_gateway.routes import auth as auth_routes

    auth_routes._RATE_BUCKET.clear()
    a = FastAPI()
    a.state.user_store = store

    class _S:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    a.state.settings = _S()
    a.include_router(auth_routes.router, prefix="/api/v1/auth")
    a.include_router(admin_billing_routes.router)
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


# ─── Key fingerprint security (P0-S-1) ──────────────────────


def test_key_fingerprint_no_plaintext():
    """P0-S-1: fingerprint must NOT expose any plaintext key chars."""
    from bowhead_gateway.routes.admin_billing import _key_fingerprint

    key = "sk-prod-1234567890abcdefghij"
    fp = _key_fingerprint(key)
    # NO plaintext key chars allowed in fingerprint
    for plaintext in ("sk-", "prod", "1234", "abcd", key[:8], key[:4]):
        assert plaintext not in fp, f"fingerprint leaks plaintext: {plaintext!r} in {fp!r}"
    assert fp.startswith("sha256:")
    assert len(fp) == len("sha256:") + 16


# ─── Add provider ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_creator_add_provider(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)
    r = client.post(
        "/api/v1/admin/llm-config/providers",
        json={"provider_name": "deepseek", "api_key": "sk-deepseek-test-key-1234", "role": "lead"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider_name"] == "deepseek"
    assert body["fingerprint"].startswith("sha256:")
    # No plaintext leak in response
    assert "sk-deepseek" not in json.dumps(body)


@pytest.mark.asyncio
async def test_member_cannot_add_provider(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_member", "m@t.com")
    jwt = _jwt("u_member", SECRET)
    r = client.post(
        "/api/v1/admin/llm-config/providers",
        json={"provider_name": "deepseek", "api_key": "sk-x-12345678901234567890", "role": "lead"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_audit_admin_cannot_add_provider(client, store):
    # WS8/D6: model config (incl. providers) is creator/team_admin only; audit_admin
    # manages credits, not models. Removes the old 403 asymmetry (POST providers used
    # to allow audit_admin while PUT full-config did not).
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    await _seed_user(store, "u_auditor", "b@t.com")
    await store.promote_team_role(
        user_id="u_auditor",
        team_id="team_g7",
        new_role="audit_admin",
        promoted_by="u_creator",
    )
    jwt = _jwt("u_auditor", SECRET)
    r = client.post(
        "/api/v1/admin/llm-config/providers",
        json={"provider_name": "doubao", "api_key": "sk-doubao-test-key-5678", "role": "narrative"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 403, r.text


# ─── Get LLM config — fingerprint only ─────────────────────


@pytest.mark.asyncio
async def test_get_llm_config_redacts_keys(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)
    # Add provider first
    client.post(
        "/api/v1/admin/llm-config/providers",
        json={"provider_name": "deepseek", "api_key": "sk-deepseek-test-key-1234secret", "role": "lead"},
        cookies={"bowhead_jwt": jwt},
    )
    r = client.get("/api/v1/admin/llm-config", cookies={"bowhead_jwt": jwt})
    assert r.status_code == 200, r.text
    body = r.json()
    response_str = json.dumps(body)
    # P0-S-1: NEVER any plaintext
    assert "sk-deepseek" not in response_str
    assert "secret" not in response_str
    assert "api_key" not in response_str
    # Has fingerprint
    assert body["providers"][0]["fingerprint"].startswith("sha256:")


# ─── Rotate ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotate_audit_before_verify(client, store):
    """P0-S-2: rotate_attempt audit fires BEFORE verify-ping."""
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)
    # Add provider
    client.post(
        "/api/v1/admin/llm-config/providers",
        json={"provider_name": "deepseek", "api_key": "sk-old-key-1234567890", "role": "lead"},
        cookies={"bowhead_jwt": jwt},
    )
    # Rotate
    r = client.post(
        "/api/v1/admin/llm-config/rotate",
        json={"provider_name": "deepseek", "new_api_key": "sk-new-rotated-9876543210"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200, r.text
    # Check audit log — should have BOTH attempt + completed rows
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT action FROM admin_audit_log "
            "WHERE actor_user_id='u_creator' "
            "AND action IN ('llm_config_rotate_attempt', 'llm_config_rotate') "
            "ORDER BY ts"
        ) as cur,
    ):
        rows = await cur.fetchall()
    actions = [r[0] for r in rows]
    # attempt MUST come first
    assert actions == ["llm_config_rotate_attempt", "llm_config_rotate"], actions


@pytest.mark.asyncio
async def test_rotate_force_flag_skips_verify(client, store):
    """P0-O-6: force=true bypasses verify-ping."""
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)
    client.post(
        "/api/v1/admin/llm-config/providers",
        json={"provider_name": "deepseek", "api_key": "sk-old-key-1234567890", "role": "lead"},
        cookies={"bowhead_jwt": jwt},
    )
    r = client.post(
        "/api/v1/admin/llm-config/rotate",
        json={"provider_name": "deepseek", "new_api_key": "sk-new-rotated-9876543210", "force": True},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200
    assert r.json()["verified_by_ping"] is False


@pytest.mark.asyncio
async def test_rotate_provider_not_configured_404(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)
    r = client.post(
        "/api/v1/admin/llm-config/rotate",
        json={"provider_name": "unknown_provider", "new_api_key": "sk-new-12345678901234567890"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == {"code": "provider_not_configured"}


# ─── Verify (test-connection, WS4) ────────────────────────


@pytest.mark.asyncio
async def test_verify_provider_accepts_private_ip(client, store, monkeypatch):
    """Local deployments accept private endpoints before capability probes run."""
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    from bowhead_gateway.routes import admin_billing

    async def _verify_private_endpoint(**kwargs):
        assert kwargs["base_url"] == "https://10.0.0.1/v1"
        return {"ok": False, "code": "unreachable"}

    monkeypatch.setattr(admin_billing, "_verify_provider_request", _verify_private_endpoint)
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)
    r = client.post(
        "/api/v1/admin/llm-config/providers/verify",
        json={"provider_type": "openai_compatible", "base_url": "https://10.0.0.1/v1", "api_key": "sk-probe-1234567890"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "unreachable"
    # never echoes the plaintext key
    assert "sk-probe" not in json.dumps(body)
    # OPS audit row exists with fingerprint (not plaintext)
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute("SELECT payload_json FROM admin_audit_log WHERE actor_user_id='u_creator' AND action='llm_provider_verify'") as cur,
    ):
        rows = await cur.fetchall()
    assert len(rows) == 1, rows
    assert "sk-probe" not in rows[0][0]
    assert "sha256:" in rows[0][0]


@pytest.mark.asyncio
async def test_member_cannot_verify_provider(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_member", "m@t.com")
    jwt = _jwt("u_member", SECRET)
    r = client.post(
        "/api/v1/admin/llm-config/providers/verify",
        json={"provider_type": "openai_compatible", "base_url": "https://api.example.com/v1", "api_key": "sk-x-12345678901234567890"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_verify_provider_passes_values_through(client, store, monkeypatch):
    """The endpoint forwards the by-value fields to verify_provider and returns its
    result dict verbatim (mock the probe so no network is touched)."""
    import bowhead_router

    captured: dict = {}

    async def _fake_verify(**kwargs):
        captured.update(kwargs)
        from bowhead_router import VerifyResult

        return VerifyResult(True, "ok", http_status=200, message="连接正常", model_count=3).as_dict()

    monkeypatch.setattr(bowhead_router, "verify_provider_capabilities", _fake_verify)
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)
    r = client.post(
        "/api/v1/admin/llm-config/providers/verify",
        json={"provider_type": "anthropic_compatible", "base_url": "https://api.anthropic.com", "api_key": "sk-real-1234567890", "model": "claude-x"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "code": "ok", "http_status": 200, "message": "连接正常", "model_count": 3}
    assert captured["provider_type"] == "anthropic_compatible"
    assert captured["base_url"] == "https://api.anthropic.com"
    assert captured["api_key"] == "sk-real-1234567890"
    assert captured["models"] == [{"id": "claude-x", "capabilities": ["chat"]}]


# ─── Delete provider ──────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_provider(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)
    client.post(
        "/api/v1/admin/llm-config/providers",
        json={"provider_name": "doubao", "api_key": "sk-doubao-key-1234567890", "role": "narrative"},
        cookies={"bowhead_jwt": jwt},
    )
    r = client.request(
        "DELETE",
        "/api/v1/admin/llm-config/providers",
        json={"provider_name": "doubao"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200
    # Verify removed
    r2 = client.get("/api/v1/admin/llm-config", cookies={"bowhead_jwt": jwt})
    assert all(p["provider_name"] != "doubao" for p in r2.json()["providers"])


@pytest.mark.asyncio
async def test_delete_provider_not_configured_404(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)

    response = client.request(
        "DELETE",
        "/api/v1/admin/llm-config/providers",
        json={"provider_name": "missing"},
        cookies={"bowhead_jwt": jwt},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "provider_not_configured"}


# ─── Credits + quota ──────────────────────────────────────


@pytest.mark.asyncio
async def test_get_credits(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)
    r = client.get("/api/v1/admin/credits", cookies={"bowhead_jwt": jwt})
    assert r.status_code == 200
    body = r.json()
    assert body["team_credit_balance"] == 5000
    assert body["team_credit_quota"] == 30000


@pytest.mark.asyncio
async def test_put_quota_with_reason(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)
    r = client.put(
        "/api/v1/admin/credits/quota",
        json={"new_quota": 50000, "reason": "Q4 budget increase approved"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["new_quota"] == 50000
    assert body["old_quota"] == 30000
    assert body["old_balance"] == 5000
    assert body["new_balance"] == 25000
    # Audit row
    async with aiosqlite.connect(store.db_path) as db:
        async with db.execute("SELECT team_credit_balance, team_credit_quota FROM teams WHERE id = 'team_g7'") as cur:
            team_row = await cur.fetchone()
        async with db.execute("SELECT payload_json FROM admin_audit_log WHERE action='credit_quota_change' AND actor_user_id='u_creator'") as cur:
            row = await cur.fetchone()
    assert team_row == (25000, 50000)
    pl = json.loads(row[0])
    assert pl["new_quota"] == 50000
    assert pl["new_balance"] == 25000
    assert pl["reason"] == "Q4 budget increase approved"


@pytest.mark.asyncio
async def test_put_quota_replenishes_pool_and_protects_team_attention(client, store):
    await _seed_team(store, balance=100, quota=100000)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    item = next(i for i in await store.materialize_credit_attention("team_g7") if i["policy_key"] == "team_pool_critical")
    jwt = _jwt("u_creator", SECRET)

    r = client.put(
        "/api/v1/admin/credits/quota",
        json={"new_quota": 500000, "reason": "contract expansion approved"},
        cookies={"bowhead_jwt": jwt},
    )

    assert r.status_code == 200, r.text
    assert r.json()["new_balance"] == 400100
    # Within the verification window the acted item must NOT protect yet — it stays acted.
    assert await store.materialize_credit_attention("team_g7") == []
    async with aiosqlite.connect(store.db_path) as db:
        async with db.execute(
            "SELECT state FROM credit_attention_state WHERE attention_item_id = ?",
            (item["attention_item_id"],),
        ) as cur:
            assert (await cur.fetchone())[0] == "acted"
        # Fast-forward past the 7d verification window, then it protects (pool recovered,
        # no team_pool_exhausted hard block in window).
        await db.execute(
            "UPDATE credit_attention_state SET acted_at = datetime('now', '-8 days') WHERE attention_item_id = ?",
            (item["attention_item_id"],),
        )
        await db.commit()
    assert await store.materialize_credit_attention("team_g7") == []
    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT state, state_reason FROM credit_attention_state WHERE attention_item_id = ?",
            (item["attention_item_id"],),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row == ("protected", "materializer_protected")


@pytest.mark.asyncio
async def test_put_quota_succeeds_when_attention_marking_fails(client, store, monkeypatch):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)

    async def _fail_mark_attention(**_kwargs):
        raise RuntimeError("attention store unavailable")

    monkeypatch.setattr(store, "mark_credit_attention_acted", _fail_mark_attention)
    jwt = _jwt("u_creator", SECRET)

    r = client.put(
        "/api/v1/admin/credits/quota",
        json={"new_quota": 50000, "reason": "Q4 budget increase approved"},
        cookies={"bowhead_jwt": jwt},
    )

    assert r.status_code == 200
    assert r.json()["new_quota"] == 50000


@pytest.mark.asyncio
async def test_put_quota_reason_too_short(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_creator", "c@t.com", is_creator=True)
    jwt = _jwt("u_creator", SECRET)
    r = client.put(
        "/api/v1/admin/credits/quota",
        json={"new_quota": 50000, "reason": "too short"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 422
