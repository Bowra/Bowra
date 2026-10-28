"""followup-context-deck #2 — workspace member 最后一个 admin 自锁保护.

Bug: members.py update_role/remove_member 无 last-admin 守卫 → 唯一 ws admin 可把自己
降级/移除 → workspace 0 admin, 再没人过 _require_workspace_admin → 谁都管不了成员.
修: get_member_role(None→404) + count_active_workspace_admins ≤1 时拦 422 last_workspace_admin.
"""

from __future__ import annotations

import time

import aiosqlite
import jwt
import pytest
from bowhead_core.user_store import UserStore, _now, hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SECRET = "test-secret-long-for-hs256-32-bytes"


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "lastadmin.db")
    await s.initialize()
    return s


async def _seed_team(s: UserStore, team_id: str = "team_t") -> None:
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES (?, 'TestOrg', 10, 'pro', 0, 0, '{\"pro\":5000}', 'active', ?)",
            (team_id, _now()),
        )
        await db.commit()


async def _seed_user(s: UserStore, user_id: str, email: str, team_id: str = "team_t") -> None:
    # After multi-team migration, (_require_workspace_role) authorization requires active team_members rows,
    # matching production backfill; inserting users alone is insufficient.
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, tier, "
            "credit_balance, credit_quota, status, created_at) "
            "VALUES (?, ?, ?, 1, ?, 'ultra', 0, 0, 'active', ?)",
            (user_id, team_id, email, hash_password("pwd123"), _now()),
        )
        await db.execute(
            "INSERT INTO team_members(id, user_id, team_id, role, status, created_at) VALUES (?, ?, ?, 'member', 'active', ?)",
            (f"om_{user_id}", user_id, team_id, _now()),
        )
        await db.commit()


async def _seed_workspace(s: UserStore, workspace_id: str, team_id: str = "team_t") -> None:
    # Authorization requires workspace_in_team → workspaces row must exist.
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO workspaces(id, team_id, name, status, created_at, updated_at) VALUES (?, ?, 'WS', 'active', ?, ?)",
            (workspace_id, team_id, _now(), _now()),
        )
        await db.commit()


async def _seed_ws_member(s: UserStore, workspace_id: str, user_id: str, role: str) -> None:
    await _seed_workspace(s, workspace_id)
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO workspace_members(workspace_id, user_id, role, joined_at, status) VALUES (?, ?, ?, ?, 'active')",
            (workspace_id, user_id, role, _now()),
        )
        await db.commit()


def _jwt(user_id: str) -> str:
    return jwt.encode(
        {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600},
        _SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def app(store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setenv("BOWHEAD_EMAIL_VERIFY_ENABLED", "0")
    from bowhead_gateway.routes import auth as auth_routes
    from bowhead_gateway.routes import members as members_routes

    auth_routes._RATE_BUCKET.clear()
    a = FastAPI()
    a.state.user_store = store

    class _Settings:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    a.state.settings = _Settings()
    a.include_router(auth_routes.router, prefix="/api/v1/auth")
    a.include_router(members_routes.router, prefix="/api/v1/workspaces")
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


# ─── store helper ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_count_active_workspace_admins(store):
    await _seed_team(store)
    await _seed_user(store, "u_a", "a@t.com")
    await _seed_user(store, "u_b", "b@t.com")
    await _seed_user(store, "u_c", "c@t.com")
    await _seed_user(store, "u_deleted", "deleted@t.com")
    await _seed_ws_member(store, "ws_1", "u_a", "admin")
    await _seed_ws_member(store, "ws_1", "u_b", "member")
    await _seed_ws_member(store, "ws_1", "u_deleted", "admin")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET status='deleted' WHERE id='u_deleted'")
        await db.commit()
    assert await store.count_active_workspace_admins("ws_1") == 1
    await _seed_ws_member(store, "ws_1", "u_c", "ws_admin")
    assert await store.count_active_workspace_admins("ws_1") == 2
    # Removed admin does not count.
    await store.remove_member("ws_1", "u_c")
    assert await store.count_active_workspace_admins("ws_1") == 1


# ─── route guards ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_demote_sole_admin_blocked_422(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_admin", "admin@t.com")
    await _seed_user(store, "u_member", "member@t.com")
    await _seed_ws_member(store, "ws_1", "u_admin", "admin")
    await _seed_ws_member(store, "ws_1", "u_member", "member")

    r = client.put(
        "/api/v1/workspaces/ws_1/members/u_admin/role",
        json={"role": "member"},
        cookies={"bowhead_jwt": _jwt("u_admin")},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"code": "last_workspace_admin"}
    # Role unchanged (still admin).
    assert await store.get_member_role("ws_1", "u_admin") == "admin"


@pytest.mark.asyncio
async def test_remove_sole_admin_blocked_422(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_admin", "admin@t.com")
    await _seed_ws_member(store, "ws_1", "u_admin", "admin")

    r = client.delete(
        "/api/v1/workspaces/ws_1/members/u_admin",
        cookies={"bowhead_jwt": _jwt("u_admin")},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"code": "last_workspace_admin"}
    assert await store.get_member_role("ws_1", "u_admin") == "admin"


@pytest.mark.asyncio
async def test_demote_one_of_two_admins_ok(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_admin", "admin@t.com")
    await _seed_user(store, "u_admin2", "admin2@t.com")
    await _seed_ws_member(store, "ws_1", "u_admin", "admin")
    await _seed_ws_member(store, "ws_1", "u_admin2", "admin")

    # 2 admins → demote one OK (1 remains).
    r = client.put(
        "/api/v1/workspaces/ws_1/members/u_admin2/role",
        json={"role": "member"},
        cookies={"bowhead_jwt": _jwt("u_admin")},
    )
    assert r.status_code == 200, r.text
    assert await store.get_member_role("ws_1", "u_admin2") == "member"


@pytest.mark.asyncio
async def test_role_update_nonmember_404(client, store):
    # Gap2: target is not a space member → 404 (get_member_role None), no silent no-op.
    await _seed_team(store)
    await _seed_user(store, "u_admin", "admin@t.com")
    await _seed_ws_member(store, "ws_1", "u_admin", "admin")

    r = client.put(
        "/api/v1/workspaces/ws_1/members/u_ghost/role",
        json={"role": "member"},
        cookies={"bowhead_jwt": _jwt("u_admin")},
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == {"code": "workspace_member_not_found"}


@pytest.mark.asyncio
async def test_remove_nonmember_returns_stable_code(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_admin", "admin@t.com")
    await _seed_ws_member(store, "ws_1", "u_admin", "admin")

    r = client.delete(
        "/api/v1/workspaces/ws_1/members/u_ghost",
        cookies={"bowhead_jwt": _jwt("u_admin")},
    )

    assert r.status_code == 404, r.text
    assert r.json()["detail"] == {"code": "workspace_member_not_found"}
