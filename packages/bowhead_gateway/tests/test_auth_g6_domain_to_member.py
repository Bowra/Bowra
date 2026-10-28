"""Sprint α G6 domain → member tests.

Covers (per PRD r4):
- env rename: BOWHEAD_AUTO_MEMBER_DOMAINS new; BOWHEAD_AUTO_ADMIN_DOMAINS deprecated + warn once
- is_auto_member_email matches white-list + subdomain
- White-list register without provisioned team → 400 (Sec P0-S-7, NO auto-create)
- White-list register WITH provisioned team → member tier (NOT admin), email_verified=True, audit_action=domain_auto_member_added
- White-list register when team has no creator → bootstrap path → creator (one-time)
- Subsequent white-list register → not creator, audit_action=domain_auto_member_added
- Non-whitelist register without team_id → creates team, user is creator
- audit_action labels correct per scenario
"""

from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.user_store import UserStore, _now
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "g6.db")
    await s.initialize()
    from bowhead_core.public_entries import PublicEntryStore

    await PublicEntryStore(s.db_path).initialize()
    return s


async def _seed_team(s: UserStore, *, team_id: str, domain: str | None = None) -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, domain, max_seats, "
            "default_user_tier, team_credit_balance, team_credit_quota, "
            "tier_credits_json, status, created_at) "
            "VALUES (?, ?, ?, 10, 'pro', 0, 0, "
            "'{\"pro\":5000,\"ultra\":30000}', 'active', ?)",
            (team_id, f"Team {team_id}", domain, _now()),
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


@pytest.fixture
def app(store, monkeypatch):
    """Minimal app with auth router."""
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test-secret-long-enough-for-hs256")
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_ENABLED", "0")  # skip enqueue in test
    monkeypatch.delenv("BOWHEAD_SMTP_HOST", raising=False)
    # Clear caches: domain env cache + per-IP rate limit bucket
    from bowhead_core.public_entries import PublicEntryStore
    from bowhead_gateway.public_entry_context import (
        PublicAccessContext,
        reset_public_access_context,
        set_public_access_context,
    )
    from bowhead_gateway.routes import auth as auth_routes

    auth_routes._AUTO_MEMBER_DOMAINS_CACHE = None
    auth_routes._DEPRECATION_WARNED = False
    auth_routes._RATE_BUCKET.clear()
    a = FastAPI()
    # The register route returns code-only errors; the localized copy is produced by the
    # i18n exception handler, so the test app must carry it to see what a client sees.
    from bowhead_core.error_catalog import default_error_catalog_dir, load_error_catalog
    from bowhead_gateway.error_handler import register_error_handlers

    register_error_handlers(a)
    a.state.error_catalog = load_error_catalog(default_error_catalog_dir())
    a.state.user_store = store
    a.state.public_entry_store = PublicEntryStore(store.db_path)
    a.state.deployment_mode = "saas"

    @a.middleware("http")
    async def _public_entry_context(request, call_next):  # type: ignore[no-untyped-def]
        token = set_public_access_context(
            PublicAccessContext(
                base_url="http://test.local",
                origin="http://test.local",
                base_path="",
                app_root="http://test.local/",
                match_source="direct",
            ),
        )
        try:
            return await call_next(request)
        finally:
            reset_public_access_context(token)

    a.include_router(auth_routes.router, prefix="/api/v1/auth")
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


# ─── Unit: env + helper ────────────────────────────────────


def test_is_auto_member_email_new_env(monkeypatch):
    monkeypatch.setenv("BOWHEAD_AUTO_MEMBER_DOMAINS", "datarc.cn,partner.com")
    monkeypatch.delenv("BOWHEAD_AUTO_ADMIN_DOMAINS", raising=False)
    from bowhead_gateway.routes import auth as a

    a._AUTO_MEMBER_DOMAINS_CACHE = None  # reset cache
    assert a.is_auto_member_email("u@datarc.cn") is True
    assert a.is_auto_member_email("u@sub.datarc.cn") is True
    assert a.is_auto_member_email("u@other.com") is False


def test_is_auto_member_email_backward_compat_old_env(monkeypatch, caplog):
    monkeypatch.delenv("BOWHEAD_AUTO_MEMBER_DOMAINS", raising=False)
    monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "datarc.cn")
    from bowhead_gateway.routes import auth as a

    a._AUTO_MEMBER_DOMAINS_CACHE = None
    a._DEPRECATION_WARNED = False
    with caplog.at_level("WARNING"):
        assert a.is_auto_member_email("u@datarc.cn") is True
    # Deprecation warned
    assert any("deprecated" in r.message for r in caplog.records)


def test_deprecation_warns_once(monkeypatch, caplog):
    monkeypatch.delenv("BOWHEAD_AUTO_MEMBER_DOMAINS", raising=False)
    monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "x.com")
    from bowhead_gateway.routes import auth as a

    a._AUTO_MEMBER_DOMAINS_CACHE = None
    a._DEPRECATION_WARNED = False
    with caplog.at_level("WARNING"):
        a.is_auto_member_email("u@x.com")
        a.is_auto_member_email("u@x.com")
        a.is_auto_member_email("u@x.com")
    # Only one deprecation warning logged
    dep_warns = [r for r in caplog.records if "deprecated" in r.message]
    assert len(dep_warns) == 1


def test_is_auto_admin_email_is_alias(monkeypatch):
    """Backward compat alias remains."""
    monkeypatch.setenv("BOWHEAD_AUTO_MEMBER_DOMAINS", "x.com")
    from bowhead_gateway.routes import auth as a

    a._AUTO_MEMBER_DOMAINS_CACHE = None
    assert a.is_auto_admin_email("u@x.com") is True


# ─── HTTP: register flow ────────────────────────────────────


def test_register_whitelist_no_team_returns_400(monkeypatch, client):
    """Sec P0-S-7: white-list domain + no provisioned team → reject, NO auto-create."""
    monkeypatch.setenv("BOWHEAD_AUTO_MEMBER_DOMAINS", "datarc.cn")
    from bowhead_gateway.routes import auth as a

    a._AUTO_MEMBER_DOMAINS_CACHE = None

    r = client.post(
        "/api/v1/auth/register",
        json={"email": "alice@datarc.cn", "password": "Password123"},
    )
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["detail"]["code"] == "whitelist_domain_no_team_provisioned"


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en-US", "No team has been configured for this email domain"),
        ("zh-CN", "尚未配置可加入的团队"),
    ],
)
def test_register_whitelist_no_team_message_is_localized(monkeypatch, client, language, expected):
    """这条曾带中文硬编码 message，把自己 code 的翻译顶掉了（英文会话照样看到中文）。"""
    monkeypatch.setenv("BOWHEAD_AUTO_MEMBER_DOMAINS", "datarc.cn")
    from bowhead_gateway.routes import auth as a

    a._AUTO_MEMBER_DOMAINS_CACHE = None

    r = client.post(
        "/api/v1/auth/register",
        json={"email": "alice@datarc.cn", "password": "Password123"},
        cookies={"NEXT_LOCALE": language},
    )

    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "whitelist_domain_no_team_provisioned"
    assert expected in detail["message"]


@pytest.mark.asyncio
async def test_register_whitelist_with_provisioned_team_is_member(
    monkeypatch,
    client,
    store,
):
    """White-list + provisioned team → member tier, email_verified=True, NOT creator."""
    monkeypatch.setenv("BOWHEAD_AUTO_MEMBER_DOMAINS", "datarc.cn")
    from bowhead_gateway.routes import auth as a

    a._AUTO_MEMBER_DOMAINS_CACHE = None

    await _seed_team(store, team_id="team_datarc", domain="datarc.cn")

    # First white-list user — becomes creator (bootstrap)
    r1 = client.post(
        "/api/v1/auth/register",
        json={"email": "first@datarc.cn", "password": "Password123"},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["user"]["tier"] == "ultra"
    assert r1.json()["user"]["email_verified"] is True

    # 2nd white-list user — NOT creator (race lost-ish; team already has creator)
    r2 = client.post(
        "/api/v1/auth/register",
        json={"email": "second@datarc.cn", "password": "Password123"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["user"]["tier"] == "ultra"

    # First became creator, second did NOT
    first_id = r1.json()["user"]["id"]
    second_id = r2.json()["user"]["id"]
    team_id = (await store.get_user(first_id)).team_id
    assert (await store.is_team_admin(first_id, team_id)) is True
    assert (await store.is_team_admin(second_id, team_id)) is False


@pytest.mark.asyncio
async def test_register_whitelist_audit_action_bootstrap(monkeypatch, client, store):
    """audit_action=domain_auto_admin_bootstrap when 1st white-list user creates creator."""
    monkeypatch.setenv("BOWHEAD_AUTO_MEMBER_DOMAINS", "datarc.cn")
    from bowhead_gateway.routes import auth as a

    a._AUTO_MEMBER_DOMAINS_CACHE = None

    await _seed_team(store, team_id="team_datarc", domain="datarc.cn")
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "alice@datarc.cn", "password": "Password123"},
    )
    assert r.status_code == 200

    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute("SELECT payload_json FROM user_audit_log WHERE event_type='register_success' ORDER BY created_at DESC LIMIT 1") as cur,
    ):
        row = await cur.fetchone()
    import json

    pl = json.loads(row[0])
    assert pl["audit_action"] == "domain_auto_admin_bootstrap"
    assert pl["whitelist_domain"] is True
    assert pl["is_empty_team_bootstrap"] is True


@pytest.mark.asyncio
async def test_register_whitelist_audit_action_member(monkeypatch, client, store):
    """audit_action=domain_auto_member_added for 2nd white-list user."""
    monkeypatch.setenv("BOWHEAD_AUTO_MEMBER_DOMAINS", "datarc.cn")
    from bowhead_gateway.routes import auth as a

    a._AUTO_MEMBER_DOMAINS_CACHE = None

    await _seed_team(store, team_id="team_datarc", domain="datarc.cn")
    r1 = client.post(
        "/api/v1/auth/register",
        json={"email": "first@datarc.cn", "password": "Password123"},
    )
    r1.json()["user"]["id"]
    r2 = client.post(
        "/api/v1/auth/register",
        json={"email": "second@datarc.cn", "password": "Password123"},
    )
    assert r2.status_code == 200, f"r2 failed: {r2.status_code} {r2.text}"
    user_2_id = r2.json()["user"]["id"]

    # Find audit row for user_2 specifically
    import json

    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT payload_json FROM user_audit_log WHERE event_type='register_success' AND user_id = ?",
            (user_2_id,),
        ) as cur,
    ):
        row = await cur.fetchone()
    pl = json.loads(row[0])
    assert pl["audit_action"] == "domain_auto_member_added"
    assert pl["whitelist_domain"] is True
    assert pl["is_empty_team_bootstrap"] is False


def test_register_non_whitelist_creates_own_team(client, monkeypatch):
    """Non-white-list + no team_id → creates team, user is creator."""
    monkeypatch.delenv("BOWHEAD_AUTO_MEMBER_DOMAINS", raising=False)
    from bowhead_gateway.routes import auth as a

    a._AUTO_MEMBER_DOMAINS_CACHE = None

    r = client.post(
        "/api/v1/auth/register",
        json={"email": "founder@new-company.com", "password": "Password123"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["tier"] == "pro"
    assert r.json()["user"]["email_verified"] is False


@pytest.mark.asyncio
async def test_register_rejects_public_team_id_join(client, monkeypatch, store):
    """Existing-team joins must use invite/domain provisioning, never public team_id."""
    monkeypatch.delenv("BOWHEAD_AUTO_MEMBER_DOMAINS", raising=False)
    from bowhead_gateway.routes import auth as a

    a._AUTO_MEMBER_DOMAINS_CACHE = None

    await _seed_team(store, team_id="team_existing", domain="example.com")
    r = client.post(
        "/api/v1/auth/register",
        json={
            "email": "intruder@elsewhere.com",
            "password": "Password123",
            "team_id": "team_existing",
        },
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "direct_team_join_disabled"
    assert await store.get_user_by_email("intruder@elsewhere.com") is None


@pytest.mark.asyncio
async def test_register_whitelist_audit_rejection(monkeypatch, client, store):
    """register_rejected audit row written when whitelist domain has no team."""
    monkeypatch.setenv("BOWHEAD_AUTO_MEMBER_DOMAINS", "ghost.com")
    from bowhead_gateway.routes import auth as a

    a._AUTO_MEMBER_DOMAINS_CACHE = None

    r = client.post(
        "/api/v1/auth/register",
        json={"email": "attacker@ghost.com", "password": "Password123"},
    )
    assert r.status_code == 400

    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute("SELECT payload_json FROM user_audit_log WHERE event_type='register_rejected' LIMIT 1") as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    import json

    pl = json.loads(row[0])
    assert pl["reason"] == "whitelist_domain_no_team_provisioned"
    assert pl["domain"] == "ghost.com"
