"""Removed space members cannot read resources from an otherwise active space.

Fixtures keep the team membership and space lifecycle active while removing
only space membership. Retained members provide a positive control, and file
reads exercise the shared authorization boundary. Real JWT authentication
avoids development-mode bypasses that would invalidate the isolation check.
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import aiosqlite
import jwt
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.session_store import SessionStore
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.routes import canvas as canvas_routes
from bowhead_gateway.routes import files as files_routes
from bowhead_gateway.routes import turns as turns_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SECRET = "space-removal-runtime-secret-0123456789"
_REMOVED_BODY = b"removed-member-rows,42\n"
_KEPT_BODY = b"kept-member-rows,7\n"
_REMOVED_TURN_TEXT = "被移出成员那一轮的原文"


def _token(user) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user.id,
            "iat": now,
            "exp": now + 3600,
            "pwd_v": int(getattr(user, "pwd_version", 0) or 0),
        },
        _SECRET,
        algorithm="HS256",
    )


@pytest.fixture
async def removal_ctx(tmp_path, monkeypatch):
    """空间 active + removed 成员仍在团队里 —— 此前测试从未覆盖的组合."""

    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")

    db_path = tmp_path / "bowhead.db"
    users = UserStore(db_path)
    await users.initialize()
    team = await users.create_team(name="Space Removal", max_seats=10)
    removed = await users.create_user(
        team_id=team.id,
        email="removed@example.com",
        password="Passw0rd!",
        email_verified=True,
    )
    kept = await users.create_user(
        team_id=team.id,
        email="kept@example.com",
        password="Passw0rd!",
        email_verified=True,
    )
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="Shared Space",
        creator_user_id=removed.id,
    )
    await users.add_member(workspace_id=workspace_id, user_id=kept.id, role="member")

    sessions = SessionStore(db_path)
    await sessions.initialize()
    logs = TurnLogStore(str(db_path))
    await logs.initialize()
    canvases = CanvasStore(db_path=tmp_path / "canvas.db")
    await canvases.initialize()
    backend = LocalBackend(root_dir=tmp_path / "blobs")
    artifacts = ArtifactStore(backend=backend, db_path=db_path)
    await artifacts.initialize()

    seeded: dict[str, dict[str, str]] = {}
    for label, user, body, text in (
        ("removed", removed, _REMOVED_BODY, _REMOVED_TURN_TEXT),
        ("kept", kept, _KEPT_BODY, "仍是成员那一轮的原文"),
    ):
        session = await sessions.ensure_session(
            session_id=None,
            workspace_id=workspace_id,
            user_id=user.id,
        )
        turn_id = f"turn_{label}"
        await sessions.record_turn(
            turn_id=turn_id,
            session_id=session.id,
            user_text=text,
        )
        await logs.record(
            turn_id=turn_id,
            event_type="assistant_message",
            payload={"text": text},
        )
        artifact = await artifacts.put_bytes(
            content=body,
            filename=f"{label}-rows.csv",
            mime="text/csv",
            source="upload",
            created_by=user.id,
            session_id=session.id,
            kind="user_upload",
        )
        # First level of files.py:623-680's four-level fallback; without this binding, STRONG gate fails open
        # early (files.py:591), defeating the discriminating control.
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO artifact_upload_bindings(artifact_id, workspace_id, user_id, session_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    artifact.id,
                    workspace_id,
                    user.id,
                    session.id,
                    "2026-09-17T00:00:00+00:00",
                ),
            )
            await db.commit()
        canvas = await canvases.ensure_canvas(
            session_id=session.id,
            workspace_id=workspace_id,
            user_id=user.id,
            title=f"{label}-report",  # Global report search matches titles; no title means no hit and vacuous pass.
        )
        await canvases.append_section(
            canvas_id=canvas.id,
            kind="artifact",
            payload={"artifact_id": artifact.id},
        )
        seeded[label] = {
            "session_id": session.id,
            "turn_id": turn_id,
            "artifact_id": artifact.id,
            "canvas_id": canvas.id,
        }

    # ★ Sole state change: remove only space membership; space remains active, team membership unchanged.
    # Use real production write path (user_store.py:4570 ``UPDATE ... SET status='removed'``).
    assert await users.remove_member(workspace_id, removed.id) is True

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(canvas_routes.router, prefix="/api/v1/canvas")
    app.include_router(turns_routes.router, prefix="/api/v1/turns")
    app.include_router(files_routes.router, prefix="/api/v1/files")
    app.state.user_store = users
    app.state.session_store = sessions
    app.state.turn_log_store = logs
    app.state.canvas_store = canvases
    app.state.artifact_store = artifacts
    app.state.artifact_backend = backend
    app.state.access_control_store = None
    app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="unused")

    return SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False),
        users=users,
        team_id=team.id,
        workspace_id=workspace_id,
        removed=removed,
        kept=kept,
        seeded=seeded,
    )


def _read_surfaces(ctx, label: str, user):
    """同一身份把三条读取面 + 判别对照一次性打完."""

    seed = ctx.seeded[label]
    cookies = {"bowhead_jwt": _token(user)}
    client = ctx.client
    return SimpleNamespace(
        report=client.get(
            f"/api/v1/canvas/by-session/{seed['session_id']}",
            cookies=cookies,
        ),
        events=client.get(
            f"/api/v1/turns/{seed['turn_id']}/events",
            cookies=cookies,
        ),
        artifact=client.get(
            f"/api/v1/canvas/by-session/{seed['session_id']}/artifacts/{seed['artifact_id']}",
            cookies=cookies,
        ),
        file_download=client.get(
            f"/api/v1/files/{seed['artifact_id']}",
            cookies=cookies,
        ),
    )


async def test_fixture_is_the_untested_combination(removal_ctx) -> None:
    """夹具自证: 空间仍 active、团队资格仍在、只有空间成员资格是 removed.

    这一条先跑 —— 下面每条断言的含义都取决于夹具确实造出了这个组合, 而不是
    顺手把空间也归档了 (那样测的还是 ``test_space_lifecycle_admission.py:118``
    已覆盖的旧组合)。
    """
    ctx = removal_ctx
    workspace = await ctx.users.get_workspace(ctx.workspace_id)
    assert workspace is not None
    assert workspace["status"] == "active"

    # Space membership gone.
    assert await ctx.users.get_member_role(ctx.workspace_id, ctx.removed.id) is None
    # Row still exists (soft deletion, not row deletion); user_store.py:4598 has_member_record semantics.
    assert await ctx.users.has_member_record(ctx.workspace_id, ctx.removed.id) is True
    # Team identity still valid, prerequisite for this exposure (§2.3: team removal cascades to close everything).
    assert await ctx.users.get_team_role(ctx.removed.id, ctx.team_id) is not None
    # Positive control user remains an active space member.
    assert await ctx.users.get_member_role(ctx.workspace_id, ctx.kept.id) is not None


async def test_active_member_reads_every_surface(removal_ctx) -> None:
    """正对照: 未被移出的成员四条路全部 200 —— 证明夹具/路由/认证本身是好的.

    没有这一条, 下面的 403 断言无法区分「闸生效」和「夹具根本跑不通」。
    ★ 它证明批2 收紧的是「已被移出的人」, 不是把成员一起拦了。
    """
    got = _read_surfaces(removal_ctx, "kept", removal_ctx.kept)

    assert got.report.status_code == 200, got.report.text
    assert got.events.status_code == 200, got.events.text
    assert got.artifact.status_code == 200, got.artifact.text
    assert got.artifact.content == _KEPT_BODY
    assert got.file_download.status_code == 200, got.file_download.text


async def test_removed_member_download_is_blocked(
    removal_ctx,
    caplog,
) -> None:
    """Discriminating control: ``GET /files/{id}`` goes through the unified gate and is refused.

    它和下面三条读的是**同一个** artifact。这条若也放行, 说明夹具没造对
    (例如 upload binding 没落库 → §3.2 的 fail-open), 本文件其余结论一律作废。
    """
    caplog.set_level(logging.INFO, logger="bowhead_gateway.routes.files")
    got = _read_surfaces(removal_ctx, "removed", removal_ctx.removed)

    # The denial answers like a missing file; the server log proves it was the Space gate.
    assert got.file_download.status_code == 404, got.file_download.text
    assert got.file_download.json()["detail"]["error"]["code"] == "file_not_found"
    assert "reason=workspace_access_denied" in caplog.text
    assert _REMOVED_BODY not in got.file_download.content


async def test_removed_member_report_detail_is_blocked(
    removal_ctx,
) -> None:
    """``GET /canvas/by-session/{sid}`` —— 403.

    403 是这条路自己的拒绝口径 (``canvas.py`` 的 ``_REPORT_UNAVAILABLE``, FE 占位页契约)。
    统一入口抛的是 ``ws_access_denied:*``, 由 ``_can_read_canvas`` 翻回
    ``report_unavailable`` —— docs-review D3: **入口只负责判定, 拒绝码由各路由映射**。
    """
    got = _read_surfaces(removal_ctx, "removed", removal_ctx.removed)

    assert got.report.status_code == 403, got.report.text
    assert got.report.json()["detail"]["code"] == "report_unavailable"


async def test_removed_owner_can_still_stop_their_own_turn(removal_ctx) -> None:
    """停止（``POST /turns/{id}/cancel``）与查询停止结果（``GET /turns/{id}/cancel/{stop_id}``）对 turn 属主放行，即使他已被移出该空间。

    否则那一轮会继续以他的身份跑完、写内容、扣额度，界面还一直提示「请重试」而重试永远 404（2026-09-24 评审）。
    正对照：仍是成员的人得到同一个结果（夹具没装编排器，过了访问判定就停在 503 / 500——两人相同＝被移出的属主也过了访问判定；
    修复前他在访问判定就是 404「turn not found」）。
    判别对照：同一个人读 turn 事件仍是 404——只放开了「停」这一件事。
    空间归档时，放行后仍照常判生命周期：两人都是 409 ``workspace_archived``（豁免只跳过成员资格，不跳过生命周期）。
    """
    from bowhead_gateway.routes.turns import explicit_stop_request_id

    ctx = removal_ctx

    def stop_both(label: str, user) -> tuple[tuple[int, object], tuple[int, object]]:
        turn_id = ctx.seeded[label]["turn_id"]
        stop_id = str(explicit_stop_request_id(team_id=user.team_id, user_id=user.id, turn_id=turn_id, execution_generation=1))
        cookies = {"bowhead_jwt": _token(user)}
        post = ctx.client.post(
            f"/api/v1/turns/{turn_id}/cancel",
            json={"cancel_request_id": stop_id, "source": "explicit_stop", "expected_execution_generation": 1},
            cookies=cookies,
        )
        get = ctx.client.get(f"/api/v1/turns/{turn_id}/cancel/{stop_id}", params={"expected_execution_generation": 1}, cookies=cookies)
        return (post.status_code, post.json()), (get.status_code, get.json())

    removed, kept = stop_both("removed", ctx.removed), stop_both("kept", ctx.kept)
    assert removed == kept, (removed, kept)
    assert all(outcome[0] != 404 for outcome in removed), removed
    events = _read_surfaces(ctx, "removed", ctx.removed).events
    assert events.status_code == 404

    assert await ctx.users.set_workspace_status(workspace_id=ctx.workspace_id, team_id=ctx.team_id, status="archived")
    archived = stop_both("removed", ctx.removed)
    assert archived == ((409, {"detail": {"error": {"code": "workspace_archived"}}}),) * 2, archived


async def test_removed_member_turn_events_are_blocked(
    removal_ctx,
) -> None:
    """``GET /turns/{tid}/events`` —— 404 且不吐该轮原文.

    ★ **故意**是 404 而不是 403: ``_require_turn_access`` 整族拒绝都是
    404 反枚举。统一入口抛 403, 路由把**授权失败**映射回 404 (docs-review D3);
    生命周期失败 (409 ``workspace_archived``) 不在此列, 另有用例钉住。
    """
    got = _read_surfaces(removal_ctx, "removed", removal_ctx.removed)

    assert got.events.status_code == 404, got.events.text
    assert _REMOVED_TURN_TEXT not in got.events.text


async def test_removed_member_report_artifact_is_blocked(
    removal_ctx,
) -> None:
    """``GET /canvas/by-session/{sid}/artifacts/{aid}`` —— 403, 不吐字节.

    这条是全代理流式 (canvas.py 刻意不发 presigned URL), 所以放行意味着内容
    **真的经服务端吐给了已被移出的人** —— 和上面判别对照里 403 的是同一个 artifact,
    同一个用户, 同一次夹具。「同一份内容换个 URL 就绕过去」必须不成立。
    """
    got = _read_surfaces(removal_ctx, "removed", removal_ctx.removed)

    assert got.artifact.status_code == 403, got.artifact.text
    assert got.artifact.content != _REMOVED_BODY


# ───────────────────── Global search (``POST /api/v1/search``) ───────────────
#
# Global search bypassed list routes: report lists / turn lists / report details / byte outputs already enforced membership,
# but search still exposed removed members' **session titles (=raw first message), filenames, report titles** in that space.
# Now all three search paths reuse their list routes' existing per-item readability checks.

_SEARCH_LIMIT = 5


def _mount_search(ctx) -> None:
    from bowhead_gateway.routes import unified_search as unified_search_routes

    app = ctx.client.app
    if not any(getattr(r, "path", "") == "/api/v1/search" for r in app.routes):
        app.include_router(unified_search_routes.router)


def _search_ids(ctx, user, query: str, *, limit: int = _SEARCH_LIMIT) -> dict[str, list[str]]:
    resp = ctx.client.post(
        "/api/v1/search",
        json={"query": query, "limit": limit},
        cookies={"bowhead_jwt": _token(user)},
    )
    assert resp.status_code == 200, resp.text
    groups = {g["kind"]: g for g in resp.json()["groups"]}
    for kind in ("session", "report", "file"):
        # Failed sources also produce empty lists; do not mistake that for filtering or the empty-result assertions falsely pass.
        assert groups[kind]["unavailable"] is False, f"{kind} 来源不可用：空结果不代表被过滤"
    return {kind: [it["id"] for it in g["items"]] for kind, g in groups.items()}


async def test_removed_member_search_hides_the_removed_space(removal_ctx) -> None:
    """被移出成员搜不到该空间里自己的会话 / 文件 / 报告。

    ★ 判别对照：**文件列表路由自己**也把这个文件藏起来了——搜索与列表同口径。
    ★ 正对照：仍是成员的 ``kept`` 搜得到自己的三类对象——收紧的是「被移出的人」，不是把成员一起拦了。
    """
    ctx = removal_ctx
    _mount_search(ctx)
    removed_seed, kept_seed = ctx.seeded["removed"], ctx.seeded["kept"]

    # Trailing slash: route mounted at ``""``; omitting it gives 307, but per-request cookies do not follow redirects (401, unrelated to authorization).
    def listed_file_ids(user) -> list[str]:
        listed = ctx.client.get("/api/v1/files/?source=all", cookies={"bowhead_jwt": _token(user)})
        assert listed.status_code == 200, listed.text
        return [item["file_id"] for item in listed.json()["items"]]  # file_id, not id: wrong key passes empty lists, raises KeyError on nonempty.

    assert kept_seed["artifact_id"] in listed_file_ids(ctx.kept), "正对照失效：文件列表连仍是成员的人自己的文件都没给，下面的「没有」什么也证明不了"
    assert removed_seed["artifact_id"] not in listed_file_ids(ctx.removed), "判别对照失效：文件列表也放行了，夹具没造对"

    got_removed = {
        "session": _search_ids(ctx, ctx.removed, "那一轮")["session"],
        "file": _search_ids(ctx, ctx.removed, "removed-rows")["file"],
        "report": _search_ids(ctx, ctx.removed, "removed-report")["report"],
    }
    got_kept = {
        "session": _search_ids(ctx, ctx.kept, "那一轮")["session"],
        "file": _search_ids(ctx, ctx.kept, "kept-rows")["file"],
        "report": _search_ids(ctx, ctx.kept, "kept-report")["report"],
    }

    assert got_kept == {
        "session": [kept_seed["session_id"]],
        "file": [kept_seed["artifact_id"]],
        "report": [kept_seed["canvas_id"]],
    }, "正对照失败：仍是成员的人也搜不到自己的东西"
    assert got_removed == {"session": [], "file": [], "report": []}


async def test_search_fetches_more_than_limit_so_removed_space_items_do_not_crowd_out_the_rest(mixed_space_ctx) -> None:
    """用户在 ``ws_keep`` 仍是成员、在 ``ws_gone`` 被移出；被移出那边的条目**更新**、排在前面。

    ``limit=5`` 时前 5 条全是 ``ws_gone`` 的：若只取 ``limit`` 条再过滤，剩 0 条，而用户在 ``ws_keep`` 明明有 2 条命中。
    要加宽重取再过滤、最后切回 ``limit``。
    """
    ctx = mixed_space_ctx
    _mount_search(ctx)
    got = _search_ids(ctx, ctx.user, "alpha", limit=_SEARCH_LIMIT)

    for kind in ("session", "file", "report"):
        assert sorted(got[kind]) == sorted(ctx.keep_ids[kind]), f"{kind}：应只剩仍可读空间的 2 条，实得 {got[kind]}"


async def test_search_never_returns_more_than_limit_after_overfetch(mixed_space_ctx) -> None:
    """多取是为了过滤，不是为了多给：结果条数仍不得超过 ``limit``。

    查询词用 ``keep``：只命中仍可读空间的 2 条（``ws_gone`` 的标题里没有这个词）。取数窗口（1×4）里就有这 2 条、
    过滤后仍是 2 条，**必须靠最后的 ``[:limit]``** 才是 1 条——用 ``alpha`` 查的话被移出空间的条目占满了窗口，
    过滤后本来就 ≤ limit，漏掉切片也测不出来。
    """
    ctx = mixed_space_ctx
    _mount_search(ctx)
    got = _search_ids(ctx, ctx.user, "keep", limit=1)
    for kind in ("session", "file", "report"):
        # Exactly 1, and **newest**: count alone misses selecting oldest or reversed sorting.
        assert got[kind] == [ctx.keep_ids[kind][-1]], f"{kind}: 应恰好是最新的 1 条，实得 {got[kind]}"


async def test_search_widens_when_the_newest_hits_are_all_in_removed_spaces(mixed_space_ctx) -> None:
    """``ws_gone`` 有 6 条更新的命中、``ws_keep`` 有 2 条较旧的：``limit=1`` 时第一窗（1×4）全是 ``ws_gone`` 的。

    只取一窗会搜到 0 条，而用户在 ``ws_keep`` 明明有命中（前端默认 ``limit=5``，被移出空间里命中多的人一样踩到）。
    加宽重取后应给出**最新的那条可读命中**。
    """
    ctx = mixed_space_ctx
    _mount_search(ctx)
    got = _search_ids(ctx, ctx.user, "alpha", limit=1)
    for kind in ("session", "file", "report"):
        assert got[kind] == [ctx.keep_ids[kind][-1]], f"{kind}: 应给出仍可读空间里最新的那条，实得 {got[kind]}"


@pytest.mark.parametrize("limit", [5, 1], ids=["one_window", "widened"])
async def test_search_files_judge_each_scope_once_not_once_per_row(mixed_space_ctx, monkeypatch, limit) -> None:
    """文件来源的逐条判定只取决于产物的归属：8 条命中分属 2 个空间，只该判 2 次；每行的归属只推导一次（共 8 行），
    且按批推导（每取一窗批一次），不是逐行各查一遍。加宽重取（5 → 20；1 → 4 → 16）也不重复推导已查过的行。
    """
    counts = {"access": 0, "scope_rows": 0, "scope_batches": 0}
    real_access, real_scopes = files_routes._require_artifact_scope_access, files_routes._artifact_scopes

    async def spy_access(*args, **kwargs):
        counts["access"] += 1
        return await real_access(*args, **kwargs)

    async def spy_scopes(request, store, arts):
        counts["scope_batches"] += 1
        counts["scope_rows"] += len(arts)
        return await real_scopes(request, store, arts)

    monkeypatch.setattr(files_routes, "_require_artifact_scope_access", spy_access)
    monkeypatch.setattr(files_routes, "_artifact_scopes", spy_scopes)
    ctx = mixed_space_ctx
    _mount_search(ctx)
    got = _search_ids(ctx, ctx.user, "alpha", limit=limit)["file"]
    assert len(got) == min(limit, 2) and set(got) <= set(ctx.keep_ids["file"]), f"应只给仍可读空间的文件，实得 {got}"
    windows = {5: 2, 1: 3}[limit]  # Windows read (first all removed-space rows; final exhausted).
    assert counts == {"access": 2, "scope_rows": 8, "scope_batches": windows}


async def test_search_first_window_is_exactly_limit(removal_ctx, monkeypatch) -> None:
    """首窗就是请求的 ``limit``（没有被隐藏的命中就不多取一行——116 实测多取的每一行都要付逐条判定的钱），
    只有可读的不够才加宽。这里 ``kept`` 在该空间只有自己的 1 条命中，一次取完。
    """
    from bowhead_core.artifact_store import ArtifactStore as _ArtifactStore
    from bowhead_core.canvas_store import CanvasStore as _CanvasStore
    from bowhead_core.session_store import SessionStore as _SessionStore

    seen: dict[str, list[int]] = {"session": [], "file": [], "report": []}
    real_sessions, real_files, real_canvases = _SessionStore.list_sessions, _ArtifactStore.list_for_user, _CanvasStore.list_for_user

    async def spy_sessions(self, **kw):
        seen["session"].append(kw.get("limit"))
        return await real_sessions(self, **kw)

    async def spy_files(self, **kw):
        seen["file"].append(kw.get("limit"))
        return await real_files(self, **kw)

    async def spy_canvases(self, **kw):
        seen["report"].append(kw.get("limit"))
        return await real_canvases(self, **kw)

    monkeypatch.setattr(_SessionStore, "list_sessions", spy_sessions)
    monkeypatch.setattr(_ArtifactStore, "list_for_user", spy_files)
    monkeypatch.setattr(_CanvasStore, "list_for_user", spy_canvases)
    _mount_search(removal_ctx)

    _search_ids(removal_ctx, removal_ctx.kept, "kept", limit=3)
    # Sessions apply the Space check in SQL (b48cb9a83), so there is no per-row judging cost;
    # since 83c6d5c36 the single query peeks one extra row to decide the next-page cursor.
    assert seen["session"] == [4], f"会话取数量 {seen['session']}"
    assert seen["file"] == [3], f"文件取数量 {seen['file']}"
    assert seen["report"] == [3, 3], f"报告取数量（自己的 + 被分享的）{seen['report']}"

    for values in seen.values():
        values.clear()
    _search_ids(removal_ctx, removal_ctx.removed, "removed", limit=2)
    # Removed user's sole hit hidden, but store returned 1 < first-window 2⇒exhausted; do not widen/requery just to fill results.
    assert seen["session"] == [3], f"取尽即停；实得 {seen['session']}"


async def test_search_owner_cannot_reach_own_removed_space_report_through_the_shared_tier(removal_ctx, monkeypatch) -> None:
    """报告搜索有两档：自己的（要查空间成员资格）、被分享进来的（不靠成员资格，归批4）。

    若属主自己的报告出现在「被分享」这一档里（例如分享给了自己），而不先剔掉，它就能绕开成员校验。
    ``GET /canvas?scope=shared`` 有同一句 ``it.get("user_id") != user_id``；搜索必须同口径。
    """
    ctx = removal_ctx
    _mount_search(ctx)
    removed_session = ctx.seeded["removed"]["session_id"]

    async def _shared_to_me(_request, _user_id):
        return {removed_session}

    monkeypatch.setattr(canvas_routes, "_shared_report_session_ids", _shared_to_me)
    assert _search_ids(ctx, ctx.removed, "removed-report")["report"] == []


async def test_search_shared_tier_serves_grantees_by_grant_not_by_space_membership(removal_ctx, monkeypatch) -> None:
    """被分享人那一档按**分享授权**放行，不按空间成员资格（与 ``GET /canvas?scope=shared`` 同口径，见 tech-design §4.4）：

    - 仍是空间成员的被分享人（``kept`` 读 ``removed`` 的报告）→ 看得到（正对照：证明上一条的「自己剔除」没有把真被分享人也剔了）；
    - 不在空间的被分享人（``removed`` 读 ``kept`` 的报告）→ 看得到。
    """
    ctx = removal_ctx
    _mount_search(ctx)
    seeded = ctx.seeded

    async def _grants(_request, user_id):
        return {seeded["removed"]["session_id"]} if user_id == ctx.kept.id else {seeded["kept"]["session_id"]}

    monkeypatch.setattr(canvas_routes, "_shared_report_session_ids", _grants)
    assert _search_ids(ctx, ctx.kept, "removed-report")["report"] == [seeded["removed"]["canvas_id"]]
    assert _search_ids(ctx, ctx.removed, "kept-report")["report"] == [seeded["kept"]["canvas_id"]]


async def test_search_report_filters_also_apply_space_lifecycle(removal_ctx, monkeypatch) -> None:
    """报告搜索的两档过滤**不只**管成员资格，还管空间生命周期（``_require_canvas_lifecycle``：已归档空间对非成员不可读）。

    上面几条只造了「空间 active + 被移出」，走不到这个分支——「被分享」那一档的过滤因此没有任何用例依赖它。
    这里用**真实 grant**（``add_grant``）让 ``removed``（已不是空间成员）持有 ``kept`` 那份报告的读权限，再把空间归档：

    非成员的被分享人搜不到已归档空间里的报告；``removed`` 自己的那份同样搜不到；仍是成员的 ``kept`` 仍搜得到（归档空间对成员仍可读历史）。
    """
    from bowhead_core.access_control import AccessControlStore
    from bowhead_core.share_service import add_grant

    monkeypatch.setattr("bowhead_core.share_service._content_sharing_cache", True)  # Process cache; avoid interference from other same-process cases.
    ctx = removal_ctx
    _mount_search(ctx)
    seeded = ctx.seeded
    access = AccessControlStore(ctx.users.db_path)
    await access.initialize()
    ctx.client.app.state.access_control_store = access
    await add_grant(
        access,
        kind="report",
        resource_id=seeded["kept"]["session_id"],
        team_id=ctx.team_id,
        principal_type="user",
        principal_id=ctx.removed.id,
        actor_id=ctx.kept.id,
    )

    kept_report = seeded["kept"]["canvas_id"]
    # Positive control: active space + real grant makes report searchable to recipient (recipient path does not require membership; current design, batch 4).
    assert _search_ids(ctx, ctx.removed, "kept-report")["report"] == [kept_report], "正对照失败：真实 grant 没让被分享人搜到"

    assert await ctx.users.set_workspace_status(workspace_id=ctx.workspace_id, team_id=ctx.team_id, status="archived") is True

    as_grantee = _search_ids(ctx, ctx.removed, "kept-report")["report"]
    as_removed_owner = _search_ids(ctx, ctx.removed, "removed-report")["report"]
    as_member = _search_ids(ctx, ctx.kept, "kept-report")["report"]
    assert as_member == [kept_report], "归档空间对仍是成员的人应仍可读历史"
    assert as_grantee == []
    assert as_removed_owner == []


async def test_search_files_in_an_archived_space_stay_readable_for_members_only(removal_ctx) -> None:
    """归档空间对仍是成员的人仍可读历史（``READ_HISTORY``），对被移出的人不可读。

    判定的 operation 必须是 ``READ_HISTORY``：换成 ``WRITE``，归档空间里成员的文件会因 409 让整个文件来源变成 unavailable。
    """
    ctx = removal_ctx
    _mount_search(ctx)
    assert await ctx.users.set_workspace_status(workspace_id=ctx.workspace_id, team_id=ctx.team_id, status="archived") is True
    kept_file = ctx.seeded["kept"]["artifact_id"]

    assert _search_ids(ctx, ctx.kept, "kept-rows")["file"] == [kept_file]
    assert _search_ids(ctx, ctx.removed, "removed-rows")["file"] == []


@pytest.fixture
async def mixed_space_ctx(tmp_path, monkeypatch):
    """同一个用户：``ws_keep`` 仍是成员（2 条**较旧**的命中），``ws_gone`` 已被移出（6 条**更新**的命中）。

    三类对象（会话 / 文件 / 报告）都按「被移出那边更新」显式落时间戳——排序靠 SQL 设死，不靠写入顺序碰运气：
    ``limit=5`` 时前 5 条**全是** ``ws_gone`` 的，正是「只取 limit 条再过滤会剩 0 条」的最坏排布。
    """
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")

    db_path = tmp_path / "bowhead.db"
    canvas_db = tmp_path / "canvas.db"
    users = UserStore(db_path)
    await users.initialize()
    team = await users.create_team(name="Mixed Spaces", max_seats=10)
    user = await users.create_user(team_id=team.id, email="mixed@example.com", password="Passw0rd!", email_verified=True)
    ws_keep = await users.create_local_workspace(team_id=team.id, name="Keep", creator_user_id=user.id)
    ws_gone = await users.create_local_workspace(team_id=team.id, name="Gone", creator_user_id=user.id)

    sessions = SessionStore(db_path)
    await sessions.initialize()
    logs = TurnLogStore(str(db_path))
    await logs.initialize()
    canvases = CanvasStore(db_path=canvas_db)
    await canvases.initialize()
    backend = LocalBackend(root_dir=tmp_path / "blobs")
    artifacts = ArtifactStore(backend=backend, db_path=db_path)
    await artifacts.initialize()

    ids: dict[str, dict[str, list[str]]] = {
        "keep": {"session": [], "file": [], "report": []},
        "gone": {"session": [], "file": [], "report": []},
    }

    async def _seed(label: str, workspace_id: str, count: int, month: int) -> None:
        for i in range(count):
            session = await sessions.ensure_session(session_id=None, workspace_id=workspace_id, user_id=user.id)
            await sessions.record_turn(turn_id=f"turn_{label}_{i}", session_id=session.id, user_text=f"alpha {label} {i}")
            artifact = await artifacts.put_bytes(
                content=b"x,1\n",
                filename=f"alpha-{label}-{i}.csv",
                mime="text/csv",
                source="upload",
                created_by=user.id,
                session_id=session.id,
                kind="user_upload",
            )
            canvas = await canvases.ensure_canvas(session_id=session.id, workspace_id=workspace_id, user_id=user.id, title=f"alpha-{label}-{i}")
            await canvases.append_section(canvas_id=canvas.id, kind="artifact", payload={"artifact_id": artifact.id})
            stamp = f"2026-{month:02d}-{10 + i:02d}T00:00:00+00:00"
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "INSERT INTO artifact_upload_bindings(artifact_id, workspace_id, user_id, session_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (artifact.id, workspace_id, user.id, session.id, stamp),
                )
                await db.execute("UPDATE sessions SET last_active_at=? WHERE id=?", (stamp, session.id))
                await db.execute("UPDATE artifacts SET created_at=? WHERE id=?", (stamp, artifact.id))
                await db.commit()
            async with aiosqlite.connect(canvas_db) as db:
                await db.execute("UPDATE canvases SET updated_at=? WHERE id=?", (stamp, canvas.id))
                await db.commit()
            ids[label]["session"].append(session.id)
            ids[label]["file"].append(artifact.id)
            ids[label]["report"].append(canvas.id)

    await _seed("keep", ws_keep, 2, 1)  # 2026-01: older.
    await _seed("gone", ws_gone, 6, 6)  # 2026-06: newer.
    assert await users.remove_member(ws_gone, user.id) is True  # Sole state change: remove ws_gone membership only.
    assert await users.get_member_role(ws_keep, user.id) is not None

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(canvas_routes.router, prefix="/api/v1/canvas")
    app.include_router(turns_routes.router, prefix="/api/v1/turns")
    app.include_router(files_routes.router, prefix="/api/v1/files")
    app.state.user_store = users
    app.state.session_store = sessions
    app.state.turn_log_store = logs
    app.state.canvas_store = canvases
    app.state.artifact_store = artifacts
    app.state.artifact_backend = backend
    app.state.access_control_store = None
    app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="unused")

    return SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False),
        user=user,
        keep_ids=ids["keep"],
        gone_ids={kind: set(values) for kind, values in ids["gone"].items()},
    )
