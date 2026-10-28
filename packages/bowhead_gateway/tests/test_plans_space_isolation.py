"""计划（plans）路由的空间判定——被移出空间的属主不再能读 / 改 / 执行该空间里自己的计划。

此前这一族只判 ownership（``get_plan`` 的 WHERE 里有 user_id + workspace_id），被移出的人照样能读计划标题步骤、
改状态、触发 amend / replan（调用模型、产生费用）——2026-09-24 默认打开前评审发现（``review-default-on-2026-09-24.md``）。

判别方式：空间判定排在各路由自己的业务校验**之前**。计划处于 RUNNING：仍是成员的人过了空间判定，会撞上各自的
业务校验（「不可编辑 / 不可修订 / 不可重规划」409，读路由 200）；被移出的人在空间判定就被拒（404「plan not found」，
与计划不存在同一个口径）。每条路由的 (状态码, 原因) 都不同于被拒时的那一个——证明拦住的是空间判定。
取消（``/cancel``）放行：属主被移出后仍能停掉自己的计划，止住后续费用。
"""

from __future__ import annotations

from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.plan_store import PlanStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import plans as plans_routes
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

_DENIED = (404, "plan not found")


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    for name, value in (
        ("BOWHEAD_PLANNING_MODE", "1"),
        ("BOWHEAD_PLANNING_REPLAN", "1"),
        ("BOWHEAD_PLANNING_PROVENANCE", "1"),
        ("BOWHEAD_AUTH_MODE", "prod"),
        ("BOWHEAD_DEV_USER_FALLBACK", "0"),
    ):
        monkeypatch.setenv(name, value)
    db = tmp_path / "p.db"
    users = UserStore(db)
    await users.initialize()
    team = await users.create_team(name="T", max_seats=10)
    kept = await users.create_user(team_id=team.id, email="kept@example.com", password="Passw0rd!", email_verified=True)
    removed = await users.create_user(team_id=team.id, email="removed@example.com", password="Passw0rd!", email_verified=True)
    workspace_id = await users.create_local_workspace(team_id=team.id, name="S", creator_user_id=kept.id)
    await users.add_member(workspace_id=workspace_id, user_id=removed.id, role="member")
    plans = PlanStore(str(db))
    await plans.initialize()
    plan_of = {}
    for who in (kept, removed):
        plan = await plans.create_plan(session_id=f"s_{who.id}", workspace_id=workspace_id, user_id=who.id, title=f"{who.email} 的计划")
        plan_of[who.id] = plan.id
    async with aiosqlite.connect(db) as conn:  # RUNNING: after space checks, each route stops at business validation without calling models.
        await conn.execute("UPDATE plans SET status='running'")
        await conn.commit()
    assert await users.remove_member(workspace_id, removed.id)

    app = FastAPI()
    app.include_router(plans_routes.router)
    app.state.user_store = users
    app.state.plan_store = plans
    app.state.access_control_store = None
    app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="unused")
    client = TestClient(app, raise_server_exceptions=False)

    def as_user(user):
        app.dependency_overrides[_current_user] = lambda: user
        return client

    return SimpleNamespace(
        as_user=as_user, users=users, team=team, kept=kept, removed=removed, workspace_id=workspace_id, plan_of=plan_of, plans=plans
    )


def _outcome(response) -> tuple[int, object]:
    detail = response.json().get("detail")
    if isinstance(detail, dict):
        detail = detail.get("error", {}).get("code", detail)
    return response.status_code, detail


def _calls(ctx, user) -> dict[str, tuple[int, object]]:
    client = ctx.as_user(user)
    plan_id, ws = ctx.plan_of[user.id], ctx.workspace_id
    base = f"/api/v1/plans/{plan_id}"
    return {
        "state": _outcome(client.get(f"{base}/state", params={"workspace_id": ws})),
        "provenance": _outcome(client.get(f"{base}/provenance", params={"workspace_id": ws})),
        "edit": _outcome(client.patch(f"{base}/steps", json={"workspace_id": ws, "user_id": user.id, "steps": []})),
        "amend": _outcome(client.post(f"{base}/amend", json={"workspace_id": ws, "user_id": user.id, "instruction": "改一下"})),
        "skip": _outcome(client.post(f"{base}/skip_step/st_x", json={"workspace_id": ws})),
        "revise": _outcome(client.post(f"{base}/revise_step/st_x", json={"workspace_id": ws, "instruction": "改一下"})),
        "replan": _outcome(client.post(f"{base}/replan", json={"workspace_id": ws, "instruction": "重来"})),
    }


def _assert_all_reached_their_own_checks(got: dict[str, tuple[int, object]]) -> None:
    assert got["state"][0] == 200 and got["provenance"][0] == 200, got
    for route in ("edit", "amend", "skip", "revise", "replan"):
        assert got[route][0] == 409, (route, got[route])  # Each route's state check (plan RUNNING).
    assert _DENIED not in got.values(), got


async def test_member_reaches_each_route_own_checks(ctx) -> None:
    _assert_all_reached_their_own_checks(_calls(ctx, ctx.kept))


async def test_removed_owner_is_stopped_at_the_space_check(ctx) -> None:
    got = _calls(ctx, ctx.removed)
    assert got == dict.fromkeys(got, _DENIED), got


async def test_archived_space_keeps_reads_and_refuses_writes_with_the_lifecycle_code(ctx) -> None:
    """归档空间：仍是成员的人读得到（READ_HISTORY），改 / 执行得到 409 ``workspace_archived``——不被吞成 404，也不是业务校验那个 409。"""
    assert await ctx.users.set_workspace_status(workspace_id=ctx.workspace_id, team_id=ctx.team.id, status="archived")
    got = _calls(ctx, ctx.kept)
    assert got["state"][0] == 200 and got["provenance"][0] == 200, got
    for route in ("edit", "amend", "skip", "revise", "replan"):
        assert got[route] == (409, "workspace_archived"), (route, got[route])


async def test_removed_owner_can_still_cancel_their_plan(ctx) -> None:
    plan_id = ctx.plan_of[ctx.removed.id]
    response = ctx.as_user(ctx.removed).post(f"/api/v1/plans/{plan_id}/cancel", json={"workspace_id": ctx.workspace_id})
    assert response.status_code == 200, response.text
    assert (await ctx.plans.get_plan(plan_id, user_id=ctx.removed.id, workspace_id=ctx.workspace_id)).status == "cancelled"


async def test_member_of_a_second_team_reads_their_plan_in_that_team(tmp_path, monkeypatch) -> None:
    """多团队用户（主团队 A，也在团队 B）读 B 团队空间里自己的计划：按**空间所属团队**判，不按 ``users.team_id``。"""
    from dataclasses import replace

    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    db = tmp_path / "multi.db"
    users = UserStore(db)
    await users.initialize()
    team_a = await users.create_team(name="A", max_seats=10)
    team_b = await users.create_team(name="B", max_seats=10)
    user = await users.create_user(team_id=team_a.id, email="u@example.com", password="Passw0rd!", email_verified=True)
    b_owner = await users.create_user(team_id=team_b.id, email="bo@example.com", password="Passw0rd!", email_verified=True)
    await users.add_existing_user_to_local_team(user_id=user.id, team_id=team_b.id)
    assert await users.user_has_active_team_membership(user.id, team_b.id)
    workspace_id = await users.create_local_workspace(team_id=team_b.id, name="SB", creator_user_id=b_owner.id)
    await users.add_member(workspace_id=workspace_id, user_id=user.id, role="member")
    assert await users.get_user_team_id(user.id) == team_a.id  # Primary team still A.
    plans = PlanStore(str(db))
    await plans.initialize()
    plan = await plans.create_plan(session_id="s1", workspace_id=workspace_id, user_id=user.id, title="t")

    app = FastAPI()
    app.include_router(plans_routes.router)
    app.state.user_store, app.state.plan_store, app.state.access_control_store = users, plans, None
    app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="unused")
    app.dependency_overrides[_current_user] = lambda: replace(user, team_id=team_b.id)
    response = TestClient(app, raise_server_exceptions=False).get(f"/api/v1/plans/{plan.id}/state", params={"workspace_id": workspace_id})
    assert response.status_code == 200, response.text
