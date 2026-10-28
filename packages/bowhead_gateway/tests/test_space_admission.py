"""SP-AUTH-5 (概念术语审计 P1-3) — 「进空间」判据收敛到 _require_space_admission.

此前三套判据互不重叠: 空间邀请=仅空间 admin; 管理面直加=仅团队 owner/team_admin
(不要求空间成员). 收敛后邀请族端点判据 = 当前团队 owner/team_admin ∪ 该空间
ws_admin, 且空间必须属于当前团队 (fail-closed) — 管理面直加的门是其子集.
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


def _as(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_jwt(user_id)}"}


@pytest.fixture
async def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.delenv("BOWHEAD_SMTP_HOST", raising=False)
    from bowhead_gateway.routes import auth as auth_routes
    from bowhead_gateway.routes import members as members_routes

    auth_routes._RATE_BUCKET.clear()

    store = UserStore(tmp_path / "admission.db")
    await store.initialize()

    team_a = await store.create_team(name="Team A", max_seats=10)
    boss = await store.create_user(team_id=team_a.id, email="boss@a.example", password="Passw0rd!")
    await store.set_team_creator(team_a.id, boss.id)
    tadmin = await store.create_user(team_id=team_a.id, email="tadmin@a.example", password="Passw0rd!")
    await store.promote_team_role(user_id=tadmin.id, team_id=team_a.id, new_role="team_admin", promoted_by=boss.id)
    ws_admin_u = await store.create_user(team_id=team_a.id, email="wsadmin@a.example", password="Passw0rd!")
    normal_u = await store.create_user(team_id=team_a.id, email="normal@a.example", password="Passw0rd!")
    ws_a = await store.sync_workspace_from_datarc(
        team_id=team_a.id,
        datarc_workspace_id="100",
        name="Sales",
        description="a",
    )
    await store.add_member(workspace_id=ws_a, user_id=ws_admin_u.id, role="ws_admin")
    await store.add_member(workspace_id=ws_a, user_id=normal_u.id, role="member")

    team_b = await store.create_team(name="Team B", max_seats=10)
    intruder = await store.create_user(team_id=team_b.id, email="boss@b.example", password="Passw0rd!")
    await store.set_team_creator(team_b.id, intruder.id)

    app = FastAPI()
    app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="dev_user")
    app.state.user_store = store
    app.include_router(members_routes.router, prefix="/api/v1/workspaces")
    return SimpleNamespace(
        app=app,
        ws_a=ws_a,
        boss=boss,
        tadmin=tadmin,
        ws_admin=ws_admin_u,
        normal=normal_u,
        intruder=intruder,
    )


@pytest.mark.asyncio
async def test_team_owner_without_ws_membership_can_manage_invites(ctx: SimpleNamespace) -> None:
    """核心行为变化: 团队 owner 无需先加入空间即可管理该空间的邀请."""
    client = TestClient(ctx.app)
    r = client.get(f"/api/v1/workspaces/{ctx.ws_a}/invites", headers=_as(ctx.boss.id))
    assert r.status_code == 200, r.text
    # Since c7a14d057, spaces grant access only to team members: direct addition via POST /members (user_id).
    r2 = client.post(
        f"/api/v1/workspaces/{ctx.ws_a}/members",
        json={"user_id": ctx.tadmin.id, "role": "member"},
        headers=_as(ctx.boss.id),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"ok": True, "user_id": ctx.tadmin.id, "role": "member"}


@pytest.mark.asyncio
async def test_team_admin_without_ws_membership_can_manage_invites(ctx: SimpleNamespace) -> None:
    client = TestClient(ctx.app)
    r = client.get(f"/api/v1/workspaces/{ctx.ws_a}/invites", headers=_as(ctx.tadmin.id))
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_space_admin_still_allowed(ctx: SimpleNamespace) -> None:
    client = TestClient(ctx.app)
    r = client.get(f"/api/v1/workspaces/{ctx.ws_a}/invites", headers=_as(ctx.ws_admin.id))
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_plain_member_denied(ctx: SimpleNamespace) -> None:
    client = TestClient(ctx.app)
    r = client.get(f"/api/v1/workspaces/{ctx.ws_a}/invites", headers=_as(ctx.normal.id))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "insufficient_role"


@pytest.mark.asyncio
async def test_cross_team_owner_denied_fail_closed(ctx: SimpleNamespace) -> None:
    """别团队的 owner 对本团队空间无 admission 权 (workspace_in_team fail-closed)."""
    client = TestClient(ctx.app)
    r = client.get(f"/api/v1/workspaces/{ctx.ws_a}/invites", headers=_as(ctx.intruder.id))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "not_workspace_member"


@pytest.mark.asyncio
async def test_archived_space_allows_governance_read_but_rejects_write(
    ctx: SimpleNamespace,
) -> None:
    async with aiosqlite.connect(ctx.app.state.user_store.db_path) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id=?",
            (ctx.ws_a,),
        )
        await db.commit()
    client = TestClient(ctx.app)

    listed = client.get(
        f"/api/v1/workspaces/{ctx.ws_a}/invites",
        headers=_as(ctx.boss.id),
    )
    checked = client.get(
        f"/api/v1/workspaces/{ctx.ws_a}/check-invite-eligibility?email=newbie@a.example",
        headers=_as(ctx.boss.id),
    )
    invited = client.post(
        f"/api/v1/workspaces/{ctx.ws_a}/members",
        json={"user_id": ctx.tadmin.id, "role": "member"},
        headers=_as(ctx.boss.id),
    )

    assert listed.status_code == 200, listed.text
    assert checked.status_code == 409
    assert checked.json()["detail"]["code"] == "workspace_archived"
    assert invited.status_code == 409
    assert invited.json()["detail"]["code"] == "workspace_archived"
