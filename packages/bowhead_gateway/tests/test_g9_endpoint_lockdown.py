"""Sprint α G9 endpoint lockdown tests.

Covers:
- _require_workspace_role accepts admin / rejects member
- _require_workspace_role re-SELECT user.team_id (P0-S-6)
- _require_workspace_role rejects non-member (403)
- _require_team_role_dep accepts team_admin (is_team_admin model)
- _audit_403_denial rate-limited 5/min/user
- members.py 4 write endpoint require admin (P0 fix verified)
- assert_prod_auth_safe blocks BOWHEAD_DEV_USER_FALLBACK=1 in prod
"""

from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.user_store import UserStore, _now, hash_password
from bowhead_gateway.public_entry_context import (
    PublicAccessContext,
    reset_public_access_context,
    set_public_access_context,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "g9.db")
    await s.initialize()
    from bowhead_core.public_entries import PublicEntryStore

    await PublicEntryStore(s.db_path).initialize()
    return s


async def _seed_team(s: UserStore, team_id: str = "team_t") -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, max_seats, "
            "default_user_tier, team_credit_balance, team_credit_quota, "
            "tier_credits_json, status, created_at) "
            "VALUES (?, 'TestOrg', 10, 'pro', 0, 0, "
            "'{\"pro\":5000,\"ultra\":30000}', 'active', ?)",
            (team_id, _now()),
        )
        await db.commit()
    from bowhead_core.public_entries import PublicEntryStore

    await PublicEntryStore(s.db_path).create_bootstrap_active(
        team_id=team_id,
        base_url="http://test.local",
        actor_id="test",
    )


async def _seed_user(
    s: UserStore,
    user_id: str,
    email: str,
    *,
    team_id: str = "team_t",
    team_role: str = "member",
    is_creator: bool = False,
) -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, "
            "password_hash, tier, credit_balance, credit_quota, "
            "status, created_at) "
            "VALUES (?, ?, ?, 1, ?, 'ultra', 0, 0, 'active', ?)",
            (user_id, team_id, email, hash_password("pwd123"), _now()),
        )
        # Authorization after multi-team migration requires active team_members rows (same shape as production backfill).
        await db.execute(
            "INSERT INTO team_members(id, user_id, team_id, role, status, created_at) VALUES (?, ?, ?, ?, 'active', ?)",
            (f"om_{user_id}", user_id, team_id, team_role, _now()),
        )
        if is_creator:
            await db.execute(
                "UPDATE teams SET created_by_user_id = ? WHERE id = ?",
                (user_id, team_id),
            )
        await db.commit()


async def _seed_ws_member(
    s: UserStore,
    workspace_id: str,
    user_id: str,
    role: str,
) -> None:
    async with aiosqlite.connect(s.db_path) as db:
        # Authorization requires workspace_in_team → workspaces row must exist.
        await db.execute(
            "INSERT OR IGNORE INTO workspaces(id, team_id, name, status, created_at, updated_at) "
            "SELECT ?, u.team_id, 'WS', 'active', ?, ? FROM users u WHERE u.id = ?",
            (workspace_id, _now(), _now(), user_id),
        )
        await db.execute(
            "INSERT INTO workspace_members(workspace_id, user_id, role, joined_at, status) VALUES (?, ?, ?, ?, 'active')",
            (workspace_id, user_id, role, _now()),
        )
        await db.commit()


def _issue_jwt(user_id: str, secret: str) -> str:
    import time

    import jwt as _jwt

    return _jwt.encode(
        {
            "sub": user_id,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        secret,
        algorithm="HS256",
    )


@pytest.fixture
def app(store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test-secret-long-for-hs256-32-bytes")
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.delenv("BOWHEAD_SMTP_HOST", raising=False)
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_ENABLED", "0")
    from bowhead_core.public_entries import PublicEntryStore
    from bowhead_gateway.public_entry_context import public_access_context_for_entry
    from bowhead_gateway.routes import auth as auth_routes
    from bowhead_gateway.routes import members as members_routes

    auth_routes._RATE_BUCKET.clear()
    a = FastAPI()
    a.state.user_store = store
    a.state.deployment_mode = "saas"
    registry = PublicEntryStore(store.db_path)
    a.state.public_entry_store = registry

    class _Settings:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    a.state.settings = _Settings()

    @a.middleware("http")
    async def public_entry_context(request, call_next):  # type: ignore[no-untyped-def]
        entries = await registry.find_active_by_base_url("http://test.local")
        context = (
            public_access_context_for_entry(entries[0], source="direct")
            if len(entries) == 1
            else PublicAccessContext(
                base_url="http://test.local",
                origin="http://test.local",
                base_path="",
                app_root="http://test.local/",
                match_source="direct",
            )
        )
        token = set_public_access_context(
            context,
        )
        try:
            return await call_next(request)
        finally:
            reset_public_access_context(token)

    a.include_router(auth_routes.router, prefix="/api/v1/auth")
    a.include_router(members_routes.router, prefix="/api/v1/workspaces")
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


# ─── assert_prod_auth_safe ────────────────────────────────


def test_assert_prod_auth_safe_blocks_dev_fallback_in_prod(monkeypatch):
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    with pytest.raises(RuntimeError, match="FATAL"):
        assert_prod_auth_safe()


def test_assert_prod_auth_safe_allows_dev_mode(monkeypatch):
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    # Deployment default changed to local (private deployment), where dev fallback is hard-rejected; this case expresses
    # fallback allowed for non-local deployment + dev mode, so explicitly declare deployment mode.
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    assert_prod_auth_safe()  # no raise


def test_assert_prod_auth_safe_rejects_dev_fallback_in_local_mode(monkeypatch):
    """私有化 (local) 部署禁止开发身份 fallback — 授权体系.md §4."""
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    with pytest.raises(RuntimeError, match="not allowed when BOWHEAD_DEPLOYMENT_MODE=local"):
        assert_prod_auth_safe()


def test_assert_prod_auth_safe_allows_prod_when_fallback_off(monkeypatch):
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    assert_prod_auth_safe()  # no raise


def test_assert_prod_auth_safe_prod_env_requires_fernet(monkeypatch):
    monkeypatch.setenv("BOWHEAD_ENV", "prod")
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)
    monkeypatch.delenv("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG", raising=False)
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    with pytest.raises(RuntimeError, match="BOWHEAD_FERNET_KEY"):
        assert_prod_auth_safe()


def test_assert_prod_auth_safe_prod_env_accepts_fernet(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_ENV", "prod")
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    from bowhead_gateway.routes.auth import assert_prod_auth_safe

    assert_prod_auth_safe()


# ─── members.py P0 gates ─────────────────────────────────


@pytest.mark.asyncio
async def test_add_existing_member_requires_space_governance(client, store, app):
    await _seed_team(store)
    await _seed_user(store, "u_admin", "admin@t.com")
    await _seed_user(store, "u_member", "member@t.com")
    await _seed_ws_member(store, "ws_1", "u_admin", "admin")
    await _seed_ws_member(store, "ws_1", "u_member", "member")

    secret = "test-secret-long-for-hs256-32-bytes"
    member_jwt = _issue_jwt("u_member", secret)

    r = client.post(
        "/api/v1/workspaces/ws_1/members",
        json={"user_id": "u_admin", "role": "member"},
        cookies={"bowhead_jwt": member_jwt},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] in ("insufficient_role", "not_workspace_member")


@pytest.mark.asyncio
async def test_add_existing_team_member_succeeds_with_admin(client, store, monkeypatch):
    await _seed_team(store)
    await _seed_user(store, "u_admin", "admin@t.com", team_role="team_admin")
    await _seed_user(store, "u_target", "target@t.com")
    await _seed_ws_member(store, "ws_1", "u_admin", "admin")

    secret = "test-secret-long-for-hs256-32-bytes"
    admin_jwt = _issue_jwt("u_admin", secret)

    r = client.post(
        "/api/v1/workspaces/ws_1/members",
        json={"user_id": "u_target", "role": "member"},
        cookies={"bowhead_jwt": admin_jwt},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_add_member_from_another_team_is_rejected(client, store):
    await _seed_team(store, team_id="team_a")
    await _seed_team(store, team_id="team_b")
    await _seed_user(store, "u_admin", "admin@a.com", team_id="team_a")
    await _seed_user(store, "u_other", "other@b.com", team_id="team_b")
    ws_id = await store.create_local_workspace(
        team_id="team_a",
        name="Team A",
        creator_user_id="u_admin",
    )

    secret = "test-secret-long-for-hs256-32-bytes"
    admin_jwt = _issue_jwt("u_admin", secret)
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"user_id": "u_other", "role": "member"},
        cookies={"bowhead_jwt": admin_jwt},
    )

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "workspace_team_mismatch"


@pytest.mark.asyncio
async def test_members_role_change_requires_admin(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_member", "member@t.com")
    await _seed_user(store, "u_target", "target@t.com")
    await _seed_ws_member(store, "ws_1", "u_member", "member")
    await _seed_ws_member(store, "ws_1", "u_target", "member")

    secret = "test-secret-long-for-hs256-32-bytes"
    member_jwt = _issue_jwt("u_member", secret)

    r = client.put(
        "/api/v1/workspaces/ws_1/members/u_target/role",
        json={"role": "admin"},
        cookies={"bowhead_jwt": member_jwt},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_members_remove_requires_admin(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_member", "member@t.com")
    await _seed_user(store, "u_target", "target@t.com")
    await _seed_ws_member(store, "ws_1", "u_member", "member")
    await _seed_ws_member(store, "ws_1", "u_target", "member")

    secret = "test-secret-long-for-hs256-32-bytes"
    member_jwt = _issue_jwt("u_member", secret)

    r = client.delete(
        "/api/v1/workspaces/ws_1/members/u_target",
        cookies={"bowhead_jwt": member_jwt},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_members_revoke_invite_requires_admin(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_member", "member@t.com")
    await _seed_ws_member(store, "ws_1", "u_member", "member")

    secret = "test-secret-long-for-hs256-32-bytes"
    member_jwt = _issue_jwt("u_member", secret)

    r = client.delete(
        "/api/v1/workspaces/ws_1/invites/invite_abc",
        cookies={"bowhead_jwt": member_jwt},
    )
    assert r.status_code == 403


# ─── no JWT → 401 ─────────────────────────────────────────


def test_add_member_no_jwt_returns_401(client):
    r = client.post(
        "/api/v1/workspaces/ws_1/members",
        json={"user_id": "u_target", "role": "member"},
    )
    assert r.status_code == 401


# ─── _audit_403_denial rate-limit ─────────────────────────


@pytest.mark.asyncio
async def test_audit_403_denial_rate_limited(store):
    """5/min/user, then stops writing."""
    await _seed_team(store)
    await _seed_user(store, "u_x", "x@t.com")
    user = await store.get_user("u_x")

    from bowhead_gateway.routes.auth import _audit_403_denial

    # 5 calls within window should write 5 audit rows
    for _ in range(5):
        await _audit_403_denial(user, "ws:test", "viewer", ["admin"], store)
    # 6th call rate-limited → no new audit row
    await _audit_403_denial(user, "ws:test", "viewer", ["admin"], store)

    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT COUNT(*) FROM user_audit_log WHERE event_type='endpoint_403_denial' AND user_id=?",
            ("u_x",),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row[0] == 5
