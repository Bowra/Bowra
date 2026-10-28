"""请求级判定数据缓存（`request_access_cache`）：**判定结果必须与不缓存时逐格相同**，开库次数不随空间数增长。

判定逻辑本身（`build_access_context`、各路由的逐条过滤）一行没改，缓存层只换了它们读数据的地方——
所以这里钉的是「数据层等价」：批量方法与对应单条方法逐个相同；在同一份数据上，经缓存层与直连原 store
得到的判定（成功时的 AccessContext、失败时的异常码）逐格相同。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.user_store import UserStore
from bowhead_gateway.access_context import AccessContextError, build_access_context
from bowhead_gateway.request_access_cache import CachedAccessControlStore, CachedUserStore, request_with_cached_access


@pytest.fixture
async def world(tmp_path):
    db = tmp_path / "w.db"
    users = UserStore(db)
    await users.initialize()
    access = AccessControlStore(db)
    await access.initialize()
    team = await users.create_team(name="T", max_seats=20)
    other_team = await users.create_team(name="Other", max_seats=5)
    owner = await users.create_user(team_id=team.id, email="owner@example.com", password="Passw0rd!", email_verified=True)
    await users.set_team_creator(team.id, owner.id)
    admin = await users.create_user(team_id=team.id, email="admin@example.com", password="Passw0rd!", email_verified=True)
    await users.promote_team_role(user_id=admin.id, team_id=team.id, new_role="team_admin", promoted_by=owner.id)
    member = await users.create_user(team_id=team.id, email="member@example.com", password="Passw0rd!", email_verified=True)
    stranger = await users.create_user(team_id=other_team.id, email="x@example.com", password="Passw0rd!", email_verified=True)

    spaces = {}
    for name in ("joined", "removed", "never", "archived_joined", "archived_removed"):
        spaces[name] = await users.create_local_workspace(team_id=team.id, name=name, creator_user_id=owner.id)
    spaces["foreign"] = await users.create_local_workspace(team_id=other_team.id, name="foreign", creator_user_id=stranger.id)
    spaces["datarc"] = await users.sync_workspace_from_datarc(team_id=team.id, datarc_workspace_id="336", name="Datarc")
    for name in ("joined", "removed", "archived_joined", "archived_removed"):
        await users.add_member(workspace_id=spaces[name], user_id=member.id, role="member")
    for name in ("removed", "archived_removed"):
        assert await users.remove_member(spaces[name], member.id)
    for name in ("archived_joined", "archived_removed"):
        assert await users.set_workspace_status(workspace_id=spaces[name], team_id=team.id, status="archived")

    # Groups: team-level (empty space), space-only, and disabled team-level; test principal_group_ids filtering by space/status.
    async with aiosqlite.connect(db) as conn:
        for gid, ws, status in (
            ("g_team", "", "active"),
            ("g_joined", spaces["joined"], "active"),
            ("g_never", spaces["never"], "active"),
            ("g_retired", "", "archived"),
        ):
            await conn.execute(
                "INSERT INTO groups(id, team_id, workspace_id, name, status, created_at, updated_at) VALUES (?,?,?,?,?, '2026-01-01', '2026-01-01')",
                (gid, team.id, ws, gid, status),
            )
            await conn.execute(
                "INSERT INTO group_memberships(group_id, team_id, principal_type, principal_id, status, created_at) VALUES (?,?, 'user', ?, 'active', '2026-01-01')",
                (gid, team.id, member.id),
            )
        await conn.commit()
    return SimpleNamespace(users=users, access=access, team=team, owner=owner, admin=admin, member=member, spaces=spaces)


async def _verdict(user_store, access_store, user_id, workspace_id, operation, override):
    try:
        ctx = await build_access_context(
            user_store=user_store,
            access_store=access_store,
            user_id=user_id,
            workspace_id=workspace_id,
            operation=operation,
            allow_team_admin_override=override,
        )
    except AccessContextError as exc:
        return ("deny", exc.code, exc.status_code)
    return ("allow", ctx.team_id, ctx.workspace_role, ctx.team_role, ctx.group_ids, ctx.policy_epoch, ctx.is_team_admin)


async def test_bulk_lookups_equal_their_single_row_versions(world) -> None:
    roles = await world.users.active_member_roles_for_user(world.member.id)
    rows = await world.users.workspaces_for_team(world.team.id)
    for workspace_id in [*world.spaces.values(), "ws_does_not_exist"]:
        assert roles.get(workspace_id) == await world.users.get_member_role(workspace_id, world.member.id), workspace_id
        if workspace_id in rows:
            assert rows[workspace_id] == await world.users.get_workspace(workspace_id), workspace_id
    assert set(rows) == {ws for name, ws in world.spaces.items() if name != "foreign"}
    cached_access = CachedAccessControlStore(world.access)
    for workspace_id in [None, "", *world.spaces.values()]:
        expected = await world.access.principal_group_ids(world.team.id, world.member.id, workspace_id)
        assert await cached_access.principal_group_ids(world.team.id, world.member.id, workspace_id) == expected, workspace_id
    assert "g_retired" not in await cached_access.principal_group_ids(world.team.id, world.member.id, "")  # Control: disabled group filtered.


async def test_a_group_with_a_null_space_matches_no_space() -> None:
    """老库里组的空间列可能是 NULL：单条版 ``g.workspace_id='' OR g.workspace_id=?`` 不匹配它，缓存层也不能把它当团队级组。"""

    class Store:
        async def principal_group_scopes(self, _team_id, _user_id):
            return (("g_null", None), ("g_team", ""), ("g_ws", "ws_1"))

    cached = CachedAccessControlStore(Store())
    assert await cached.principal_group_ids("t", "u", "ws_1") == ("g_team", "g_ws")
    assert await cached.principal_group_ids("t", "u", None) == ("g_team",)


def test_writes_to_the_proxied_state_reach_the_real_state(world) -> None:
    real_state = SimpleNamespace(user_store=world.users, access_control_store=world.access)
    request = SimpleNamespace(app=SimpleNamespace(state=real_state), headers={})
    proxied = request_with_cached_access(request, team_id=world.team.id)
    proxied.app.state.marker = "x"
    assert real_state.marker == "x"


async def test_calls_carrying_a_transaction_connection_bypass_the_cache(world, monkeypatch) -> None:
    """调用方带着自己的事务连接时是事务语义：不能用请求开头读到的快照答它。"""
    cached_access = CachedAccessControlStore(world.access)
    seen = []
    real_ids, real_epoch = world.access.principal_group_ids, world.access.get_policy_epoch

    async def ids(team_id, user_id, workspace_id=None, **kwargs):
        seen.append(("ids", kwargs))
        return await real_ids(team_id, user_id, workspace_id)

    async def epoch(team_id, **kwargs):
        seen.append(("epoch", kwargs))
        return await real_epoch(team_id)

    monkeypatch.setattr(world.access, "principal_group_ids", ids)
    monkeypatch.setattr(world.access, "get_policy_epoch", epoch)
    sentinel = object()
    await cached_access.principal_group_ids(world.team.id, world.member.id, "", connection=sentinel)
    await cached_access.get_policy_epoch(world.team.id, connection=sentinel)
    assert seen == [("ids", {"connection": sentinel}), ("epoch", {"connection": sentinel})]


@pytest.mark.parametrize("operation", [SpaceOperation.READ_HISTORY, SpaceOperation.WRITE])
@pytest.mark.parametrize("override", [False, True])
async def test_every_verdict_is_the_same_with_and_without_the_cache(world, operation, override) -> None:
    """人 × 空间 × 操作 × 管理员豁免：直连与经缓存（先后问多个空间，缓存已被别的空间填过）逐格相同。"""
    cached_users = CachedUserStore(world.users, team_id=world.team.id)
    cached_access = CachedAccessControlStore(world.access)
    people = {"owner": world.owner.id, "admin": world.admin.id, "member": world.member.id, "ghost": "usr_missing"}
    spaces = [*world.spaces.values(), "ws_does_not_exist"]
    for who, user_id in people.items():
        for workspace_id in spaces:
            direct = await _verdict(world.users, world.access, user_id, workspace_id, operation, override)
            cached = await _verdict(cached_users, cached_access, user_id, workspace_id, operation, override)
            assert cached == direct, (who, workspace_id)
    # Discriminating control: matrix includes both allow and various denials, not one-sided outcomes.
    outcomes = {(await _verdict(world.users, world.access, world.member.id, ws, operation, override))[:2] for ws in spaces}
    assert {o[0] for o in outcomes} == {"allow", "deny"}


async def test_space_id_resolution_is_the_same_with_and_without_the_cache(world) -> None:
    """报告判定先把空间引用（``ws_*`` 或北极九章旧 id）规范化；本团队的答案来自缓存的团队全量，其它团队交回原方法。"""
    cached = CachedUserStore(world.users, team_id=world.team.id)
    other_team_id = (await world.users.get_workspace(world.spaces["foreign"]))["team_id"]
    refs = [*world.spaces.values(), "336", "999", "", "ws_does_not_exist"]
    for team_id in (world.team.id, other_team_id, ""):
        for ref in refs:
            assert await cached.resolve_workspace_id_for_team(team_id, ref) == await world.users.resolve_workspace_id_for_team(team_id, ref), (
                team_id,
                ref,
            )
    assert await cached.resolve_workspace_id_for_team(world.team.id, "336") == world.spaces["datarc"]  # Control: old ID normalized.


async def test_concurrent_lookups_of_the_same_value_hit_the_store_once(world, monkeypatch) -> None:
    calls = {"n": 0}
    real = world.users.active_member_roles_for_user

    async def counting(user_id):
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return await real(user_id)

    monkeypatch.setattr(world.users, "active_member_roles_for_user", counting)
    cached = CachedUserStore(world.users, team_id=world.team.id)
    results = await asyncio.gather(*(cached.get_member_role(ws, world.member.id) for ws in world.spaces.values()))
    assert calls["n"] == 1
    assert results == [await world.users.get_member_role(ws, world.member.id) for ws in world.spaces.values()]


async def test_cached_workspace_rows_are_copies(world) -> None:
    cached = CachedUserStore(world.users, team_id=world.team.id)
    row = await cached.get_workspace(world.spaces["joined"])
    row["status"] = "archived"
    assert (await cached.get_workspace(world.spaces["joined"]))["status"] == "active"


async def test_minimal_stores_get_the_original_request_back() -> None:
    """测试里的精简夹具（没有批量方法）：原样返回原请求，不换任何东西。"""
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(user_store=SimpleNamespace())))
    assert request_with_cached_access(request, team_id="t") is request


async def test_proxy_only_swaps_the_two_stores(world) -> None:
    sentinel = object()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(user_store=world.users, access_control_store=world.access, canvas_store=sentinel)),
        headers={"x": "1"},
    )
    proxied = request_with_cached_access(request, team_id=world.team.id)
    assert isinstance(proxied.app.state.user_store, CachedUserStore)
    assert isinstance(proxied.app.state.access_control_store, CachedAccessControlStore)
    assert proxied.app.state.canvas_store is sentinel
    assert proxied.headers == {"x": "1"}


async def _search_world(tmp_path, monkeypatch, n_spaces: int):
    """一个成员，命中分布在 ``n_spaces`` 个空间（每个空间一条会话 / 一份报告 / 一个文件），另有一个已被移出的空间。"""
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.canvas_store import CanvasStore
    from bowhead_core.session_store import SessionStore
    from bowhead_gateway.error_handler import register_error_handlers
    from bowhead_gateway.routes import unified_search as unified_search_routes
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from test_space_removed_member_read_paths import _SECRET, _token

    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    db = tmp_path / f"s{n_spaces}.db"
    users = UserStore(db)
    await users.initialize()
    team = await users.create_team(name="T", max_seats=10)
    user = await users.create_user(team_id=team.id, email="u@example.com", password="Passw0rd!", email_verified=True)
    sessions = SessionStore(db)
    await sessions.initialize()
    canvases = CanvasStore(db)
    await canvases.initialize()
    artifacts = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / f"b{n_spaces}"), db_path=db)
    await artifacts.initialize()
    for i in range(n_spaces + 1):
        ws = await users.create_local_workspace(team_id=team.id, name=f"S{i}", creator_user_id=user.id)
        s = await sessions.ensure_session(session_id=None, workspace_id=ws, user_id=user.id)
        await sessions.set_title(session_id=s.id, title=f"alpha {i}")
        await canvases.ensure_canvas(session_id=s.id, workspace_id=ws, user_id=user.id, title=f"alpha {i}")
        await artifacts.put_bytes(content=b"x", filename=f"alpha-{i}.csv", mime="text/csv", created_by=user.id, session_id=s.id)
        if i == n_spaces:
            assert await users.remove_member(ws, user.id)
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(unified_search_routes.router)
    app.state.user_store, app.state.session_store, app.state.canvas_store, app.state.artifact_store = users, sessions, canvases, artifacts
    app.state.access_control_store = None
    app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="unused")
    client = TestClient(app, raise_server_exceptions=False)
    return lambda: client.post("/api/v1/search", json={"query": "alpha", "limit": 10}, cookies={"bowhead_jwt": _token(user)})


async def test_search_connection_count_does_not_grow_with_the_number_of_spaces(tmp_path, monkeypatch) -> None:
    """命中分布在 10 个还是 20 个空间，一次搜索的开库次数相同（判定数据按请求批量取一次）。

    修复前每个来源要对每个不同空间各跑一次完整判定（约 5–7 次开库），合成的大库上一次搜索 500–900 次开库。
    两边都让 limit=10 的首窗恰好含一条被移出空间的命中 ⇒ 都加宽一轮，取数轮数相同，差别只剩空间数。
    """
    small, large = await _search_world(tmp_path, monkeypatch, 10), await _search_world(tmp_path, monkeypatch, 20)
    real_connect = aiosqlite.connect
    opened = {"n": 0}

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", counting)
    counts = {}
    for label, search, expected in (("10", small, 10), ("20", large, 10)):
        search()  # Warm up (exclude one-time route initialization).
        opened["n"] = 0
        response = search()
        counts[label] = opened["n"]
        groups = {g["kind"]: g for g in response.json()["groups"]}
        for kind in ("session", "report", "file"):
            assert groups[kind]["unavailable"] is False
            assert len(groups[kind]["items"]) == expected, (label, kind)  # Removed space member excluded.
    # Doubling spaces barely changes DB opens (pre-fix each space added ~5–7; 10→20 spaces meant 240→340).
    # Absolute counts may differ by 1–2 (row shapes in wider window, concurrent scheduling), unrelated to space count; allow +3 to avoid flakes.
    assert counts["20"] <= counts["10"] + 3, counts
    assert counts["10"] < 40, counts
