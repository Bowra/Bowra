"""SP-AUTH-4 (概念术语审计 P1-5) — GET /workspaces/{ws}/members 的 team 归属校验.

修复前该端点只查 workspace_members 成员行, 不校验空间属于调用者当前团队 —
陈旧/跨团队成员行 (用户换队后残留) 可枚举他队空间成员名单. 修复后与写路径
(_require_workspace_role 的 workspace_in_team) 对齐.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

import aiosqlite
import jwt as jwt_lib
import pytest
from bowhead_core.user_store import UserStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

SECRET = "test-secret-long-enough-for-hs256-32-bytes"


def _jwt(user_id: str) -> str:
    return jwt_lib.encode(
        {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600, "pwd_v": 0},
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture
async def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    from bowhead_gateway.routes import auth as auth_routes
    from bowhead_gateway.routes import members as members_routes

    auth_routes._RATE_BUCKET.clear()

    store = UserStore(tmp_path / "members.db")
    await store.initialize()

    team_a = await store.create_team(name="Team A", max_seats=10)
    member_a = await store.create_user(team_id=team_a.id, email="a@a.example", password="Passw0rd!")
    ws_a = await store.sync_workspace_from_datarc(
        team_id=team_a.id,
        datarc_workspace_id="100",
        name="Sales",
        description="a",
    )
    await store.add_member(workspace_id=ws_a, user_id=member_a.id, role="ws_admin")

    # drifter: legitimately joined ws_a in team A, then moved entirely to team B,
    # leaving a stale cross-team workspace_members row that occurs in real deployments.
    drifter = await store.create_user(team_id=team_a.id, email="drifter@a.example", password="Passw0rd!")
    await store.add_member(workspace_id=ws_a, user_id=drifter.id, role="member")
    team_b = await store.create_team(name="Team B", max_seats=10)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET team_id = ? WHERE id = ?", (team_b.id, drifter.id))
        await db.execute(
            "UPDATE team_members SET team_id = ? WHERE user_id = ?",
            (team_b.id, drifter.id),
        )
        await db.commit()

    app = FastAPI()
    app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="dev_user")
    app.state.user_store = store
    app.include_router(members_routes.router, prefix="/api/v1/workspaces")
    return SimpleNamespace(app=app, ws_a=ws_a, member_a=member_a, drifter=drifter)


def _as(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_jwt(user_id)}"}


@pytest.mark.asyncio
async def test_member_in_team_can_list(ctx: SimpleNamespace) -> None:
    client = TestClient(ctx.app)
    r = client.get(f"/api/v1/workspaces/{ctx.ws_a}/members", headers=_as(ctx.member_a.id))
    assert r.status_code == 200
    ids = {m["user_id"] for m in r.json()["members"]}
    assert ctx.member_a.id in ids


@pytest.mark.asyncio
async def test_stale_cross_team_member_row_cannot_list(ctx: SimpleNamespace) -> None:
    """换队后残留的成员行不得再枚举原空间成员名单 (403, 与写路径同码)."""
    client = TestClient(ctx.app)
    r = client.get(f"/api/v1/workspaces/{ctx.ws_a}/members", headers=_as(ctx.drifter.id))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "not_workspace_member"


@pytest.mark.asyncio
async def test_member_can_list_archived_space_roster(
    ctx: SimpleNamespace,
) -> None:
    async with aiosqlite.connect(ctx.app.state.user_store.db_path) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id=?",
            (ctx.ws_a,),
        )
        await db.commit()

    response = TestClient(ctx.app).get(
        f"/api/v1/workspaces/{ctx.ws_a}/members",
        headers=_as(ctx.member_a.id),
    )

    assert response.status_code == 200, response.text
    assert ctx.member_a.id in {member["user_id"] for member in response.json()["members"]}
