"""Sprint β G2 — /me/workspaces + /me/last-workspace endpoint tests."""

from __future__ import annotations

import json

import aiosqlite
import pytest
from bowhead_core.user_store import UserStore, _now, hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "g2.db")
    await s.initialize()
    return s


async def _seed_team(s, team_id="team_g2"):
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
    *,
    team_id="team_g2",
    is_creator=False,
):
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "tier, credit_balance, credit_quota, status, created_at) "
            "VALUES (?, ?, ?, 1, ?, 'ultra', 0, 0, 'active', ?)",
            (user_id, team_id, email, hash_password("pwd123"), _now()),
        )
        await db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,'member','active',?)",
            (f"om_{user_id}", user_id, team_id, _now()),
        )
        if is_creator:
            await db.execute(
                "UPDATE teams SET created_by_user_id=? WHERE id=?",
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


@pytest.mark.asyncio
async def test_list_my_workspaces_empty(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    jwt = _jwt("u1", SECRET)
    r = client.get("/api/v1/auth/me/workspaces", cookies={"bowhead_jwt": jwt})
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["active_id"] is None


@pytest.mark.asyncio
async def test_list_my_workspaces_returns_member_ws(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    ws_id = await store.sync_workspace_from_datarc(
        team_id="team_g2",
        datarc_workspace_id="dr_1",
        name="泰尔茂",
    )
    await store.add_member(workspace_id=ws_id, user_id="u1", role="admin")
    jwt = _jwt("u1", SECRET)
    r = client.get("/api/v1/auth/me/workspaces", cookies={"bowhead_jwt": jwt})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["workspace_id"] == ws_id
    assert body["items"][0]["name"] == "泰尔茂"
    assert body["items"][0]["role"] == "admin"


@pytest.mark.asyncio
async def test_list_active_id_falls_to_first_if_deleted(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    ws_id = await store.sync_workspace_from_datarc(
        team_id="team_g2",
        datarc_workspace_id="dr_1",
        name="ws1",
    )
    await store.add_member(workspace_id=ws_id, user_id="u1", role="member")
    # Set stale last_workspace_id pointing to non-existent ws
    await store.set_user_preference_key("u1", "last_workspace_id", "ws_does_not_exist")
    jwt = _jwt("u1", SECRET)
    r = client.get("/api/v1/auth/me/workspaces", cookies={"bowhead_jwt": jwt})
    body = r.json()
    # active_id should fall to items[0] (E-WS-7)
    assert body["active_id"] == ws_id


@pytest.mark.asyncio
async def test_put_last_workspace_idor_blocked(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    await _seed_user(store, "u2", "u2@t.com")
    ws_id = await store.sync_workspace_from_datarc(
        team_id="team_g2",
        datarc_workspace_id="dr_1",
        name="ws1",
    )
    # u2 is member, u1 is NOT
    await store.add_member(workspace_id=ws_id, user_id="u2", role="admin")
    jwt = _jwt("u1", SECRET)
    r = client.put(
        "/api/v1/auth/me/last-workspace",
        json={"workspace_id": ws_id},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "not_workspace_member"


@pytest.mark.asyncio
async def test_put_last_workspace_updates_pref_and_touches_active(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    ws_id = await store.sync_workspace_from_datarc(
        team_id="team_g2",
        datarc_workspace_id="dr_1",
        name="ws1",
    )
    await store.add_member(workspace_id=ws_id, user_id="u1", role="member")
    jwt = _jwt("u1", SECRET)
    r = client.put(
        "/api/v1/auth/me/last-workspace",
        json={"workspace_id": ws_id},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["workspace_id"] == ws_id
    assert body["role"] == "member"
    # Verify pref + last_active_at
    async with aiosqlite.connect(store.db_path) as db:
        async with db.execute("SELECT preferences_json FROM users WHERE id='u1'") as cur:
            row = await cur.fetchone()
        prefs = json.loads(row[0])
        assert prefs["last_workspace_id"] == ws_id
        async with db.execute(
            "SELECT last_active_at FROM workspace_members WHERE workspace_id=? AND user_id=?",
            (ws_id, "u1"),
        ) as cur:
            row = await cur.fetchone()
    assert row[0] is not None


@pytest.mark.asyncio
async def test_put_last_workspace_concurrent_no_pref_loss(client, store):
    """P1-S-3 atomic json_set: concurrent writes don't clobber other keys."""
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com")
    # Set a theme pref first
    await store.set_user_preference_key("u1", "theme", "dark")
    ws_id = await store.sync_workspace_from_datarc(
        team_id="team_g2",
        datarc_workspace_id="dr_1",
        name="ws1",
    )
    await store.add_member(workspace_id=ws_id, user_id="u1", role="member")
    jwt = _jwt("u1", SECRET)
    client.put(
        "/api/v1/auth/me/last-workspace",
        json={"workspace_id": ws_id},
        cookies={"bowhead_jwt": jwt},
    )
    # Verify theme NOT clobbered
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT preferences_json FROM users WHERE id='u1'") as cur:
        row = await cur.fetchone()
    prefs = json.loads(row[0])
    assert prefs["theme"] == "dark"
    assert prefs["last_workspace_id"] == ws_id


# ─── ws-datarc-decouple ② (R2.2 / #110): Bowhead-local workspace CRUD ─────
@pytest.mark.asyncio
async def test_create_workspace_roundtrip(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com", is_creator=True)
    jwt = _jwt("u1", SECRET)
    r = client.post(
        "/api/v1/auth/me/workspaces",
        json={"name": "营销分析", "description": "no datarc yet"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 201, r.text
    ws_id = r.json()["workspace_id"]
    assert ws_id.startswith("ws_")
    # creator is enrolled as workspace admin → shows up in their canonical list
    lst = client.get("/api/v1/auth/me/workspaces", cookies={"bowhead_jwt": jwt}).json()
    assert any(i["workspace_id"] == ws_id and i["name"] == "营销分析" for i in lst["items"])
    assert any(i["role"] == "admin" for i in lst["items"])


@pytest.mark.asyncio
async def test_rename_then_archive_workspace(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com", is_creator=True)
    jwt = _jwt("u1", SECRET)
    ws_id = client.post(
        "/api/v1/auth/me/workspaces",
        json={"name": "old"},
        cookies={"bowhead_jwt": jwt},
    ).json()["workspace_id"]
    # rename
    r = client.patch(
        f"/api/v1/auth/me/workspaces/{ws_id}",
        json={"name": "new name"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r.status_code == 200, r.text
    lst = client.get("/api/v1/auth/me/workspaces", cookies={"bowhead_jwt": jwt}).json()
    assert any(i["name"] == "new name" for i in lst["items"])
    # A Team must retain at least one active Space.
    keep_id = client.post(
        "/api/v1/auth/me/workspaces",
        json={"name": "keep active"},
        cookies={"bowhead_jwt": jwt},
    ).json()["workspace_id"]
    # archive → drops out of the active list
    r2 = client.patch(
        f"/api/v1/auth/me/workspaces/{ws_id}",
        json={"status": "archived"},
        cookies={"bowhead_jwt": jwt},
    )
    assert r2.status_code == 200, r2.text
    lst2 = client.get("/api/v1/auth/me/workspaces", cookies={"bowhead_jwt": jwt}).json()
    assert all(i["workspace_id"] != ws_id for i in lst2["items"])
    assert any(i["workspace_id"] == keep_id for i in lst2["items"])


@pytest.mark.asyncio
async def test_update_workspace_requires_admin(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com", is_creator=True)
    await _seed_user(store, "u2", "u2@t.com")  # plain member
    jwt1, jwt2 = _jwt("u1", SECRET), _jwt("u2", SECRET)
    ws_id = client.post(
        "/api/v1/auth/me/workspaces",
        json={"name": "x"},
        cookies={"bowhead_jwt": jwt1},
    ).json()["workspace_id"]
    await store.add_member(workspace_id=ws_id, user_id="u2", role="member")
    # non-admin member → 403
    r = client.patch(
        f"/api/v1/auth/me/workspaces/{ws_id}",
        json={"name": "hijack"},
        cookies={"bowhead_jwt": jwt2},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_update_workspace_accepts_legacy_admin_role(client, store):
    await _seed_team(store)
    await _seed_user(store, "u1", "u1@t.com", is_creator=True)
    await _seed_user(store, "u_legacy_admin", "legacy@t.com")
    ws_id = client.post(
        "/api/v1/auth/me/workspaces",
        json={"name": "legacy"},
        cookies={"bowhead_jwt": _jwt("u1", SECRET)},
    ).json()["workspace_id"]
    await store.add_member(workspace_id=ws_id, user_id="u_legacy_admin", role="admin")

    r = client.patch(
        f"/api/v1/auth/me/workspaces/{ws_id}",
        json={"name": "legacy renamed"},
        cookies={"bowhead_jwt": _jwt("u_legacy_admin", SECRET)},
    )

    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_update_workspace_cross_team_blocked(client, store):
    await _seed_team(store, "team_a")
    await _seed_team(store, "team_b")
    await _seed_user(store, "ua", "ua@t.com", team_id="team_a", is_creator=True)
    await _seed_user(store, "ub", "ub@t.com", team_id="team_b", is_creator=True)
    ws_a = client.post(
        "/api/v1/auth/me/workspaces",
        json={"name": "a-ws"},
        cookies={"bowhead_jwt": _jwt("ua", SECRET)},
    ).json()["workspace_id"]
    # Cross-Team governance objects are hidden.
    r = client.patch(
        f"/api/v1/auth/me/workspaces/{ws_a}",
        json={"name": "stolen"},
        cookies={"bowhead_jwt": _jwt("ub", SECRET)},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_create_workspace_requires_team_governance(client, store):
    await _seed_team(store)
    await _seed_user(store, "member", "member@t.com")

    response = client.post(
        "/api/v1/auth/me/workspaces",
        json={"name": "unauthorized"},
        cookies={"bowhead_jwt": _jwt("member", SECRET)},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "not_team_admin"
