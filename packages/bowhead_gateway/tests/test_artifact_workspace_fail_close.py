"""产物归属的四档判定（空间隔离批4 ③）—— `FEATURE_ARTIFACT_WORKSPACE_FAIL_CLOSE`。

| 列的值                        | 判定                                             |
| ----------------------------- | ------------------------------------------------ |
| 真实 `ws_*`                   | 走强闸按空间判（今天就是这样）                   |
| `__team__`                    | **团队级授权**，不按空间成员资格判               |
| `NULL` 且 `created_at ≥ 水位` | **fail-close**（写入点的 bug，不是历史欠账）     |
| `NULL` 且 `created_at < 水位` | **保持今天行为**（fail-open），一个字不改        |

夹具纪律（本文件大量断言形如「某某不再发生」，而「断言没发生」和「用例根本没跑到」长得一样）：

- ★★ **每条 fail-close 断言都配一条「切点前的老产物仍然读得到」的正对照**
  （`null_before` 与 `null_after` 只差 `created_at` 一个字段，其余逐字相同）。
  没有它，`null_after` 的 403 分不清是「水位判定生效」还是「夹具根本读不到任何东西」。
- **判别对照** `bound_space`：有真实 upload binding 的空间产物，**两档 flag 都**应对
  已被移出空间的人 403。它若也放行，说明夹具没造对，本文件其余结论一律作废。
- **闸关那一档必须跑**：批4 的闸关 = 逐字节今天的行为。只量闸开那一档的话，
  用例绿了也分不清是 flag 起了作用，还是这条路本来就拦得住。
- 认证走 **prod + 真 JWT**，不走 dev-fallback。第一版用 `X-Bowhead-User-Id` +
  `BOWHEAD_DEV_USER_FALLBACK=0`，结果 12 条**全部 401**——路由根本没走到空间判定，
  那种绿/红都证明不了任何事。prod+JWT 是 `test_space_removed_member_read_paths.py`
  已经在 `files.py` 上验证过的路径。
- 全部产物**故意 owner-less**（`created_by=None`）：`download` 自己有一道
  `art.created_by != caller` 的 404，混进来会让 403/200 分不清是哪道闸给的。
  owner-less 在该路由上按设计可读，于是每条断言隔离出来的正好是**空间判定**这一件事。

**写入侧谁来证**（本文件只量**读取**那一面，上表四档的输入是它给的）。源码文本级守卫对
「把写入值改成 `None`」是瞎的——批4 的 M1 突变就是这么活下来的，所以那一列必须有行为断言。
今天已有查库用例的写入口：

- `kb_presign.py` 三处 → `test_kb_presign_phase14b.py` / `test_kb_presign_bundle_phase14c.py`（2026-09-18）
- `kb.py` 三处（直传 `/api/v1/kb/files` 与 bundle）→ `test_kb_upload_workspace_attribution.py`（2026-09-19）
- `files_presign` 的 `_atomic_insert_pending` 写入 → `test_files_presign_phase14a.py`（空间头 / 遗留头 / **session** 分支，2026-09-21 起每条腿同时断言落库值）
- `brand_assets.py` → `test_brand_assets.py`

⚠ **仍然缺的那一格：`routes/files.py` 的 `POST /api/v1/files/upload`**。2026-09-19 只读探针
三档实测：无 session 无空间头 → **NULL**；带 `session_id` → `ws_*`（正对照）；
**带空间头但无 session → 仍是 NULL**——该处调用点**没有传** `workspace_id`
（⚠ 更正：`put_bytes` **是有**该形参的，`artifact_store.py:1664`，只是这个调用点没接线；
「做不到」与「还没接、接上会改变行为」是两回事），归属因此只能走 session/turn/canvas 梯子，
请求头救不了。而该路由注释自陈 chat composer 正是「不带 session 上传」。翻闸前必须先处理这一格。
（该路由的**归属落值**现有 `test_files_office_upload.py` 里的打点用例顺带钉住——无 session 落 NULL、
带空间的 session 落 session 的空间、session 的空间为空落 NULL——但那是为了让 `reason` 与落库值绑定，
不是在断言「NULL 是对的」：该缺口该落什么值尚未拍板，见 `docs/灰度功能清单.md`「产物归属打点灰度」。）
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import aiosqlite
import jwt
import pytest
from bowhead_core.artifact_store import (
    ARTIFACT_WORKSPACE_CUTOVER_TABLE,
    TEAM_SCOPE_WORKSPACE_ID,
    ArtifactStore,
    LocalBackend,
)
from bowhead_core.session_store import SessionStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.routes import files as files_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

_FLAG = "FEATURE_ARTIFACT_WORKSPACE_FAIL_CLOSE"
_SECRET = "artifact-workspace-cutover-secret-0123456789"

_BEFORE = "2026-09-01T00:00:00+00:00"
_CUTOVER = "2026-09-10T00:00:00.000Z"
_AFTER = "2026-09-15T00:00:00+00:00"


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


@pytest.fixture(params=[False, True], ids=["flag_off", "flag_on"])
def fail_close(request, monkeypatch) -> bool:
    monkeypatch.setenv(_FLAG, "1" if request.param else "0")
    return bool(request.param)


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")

    db_path = tmp_path / "bowhead.db"
    users = UserStore(db_path)
    await users.initialize()
    team = await users.create_team(name="Artifact Cutover", max_seats=10)
    keeper = await users.create_user(team_id=team.id, email="keeper@example.com", password="Passw0rd!", email_verified=True)
    removed = await users.create_user(team_id=team.id, email="removed@example.com", password="Passw0rd!", email_verified=True)
    workspace_id = await users.create_local_workspace(team_id=team.id, name="Shared", creator_user_id=keeper.id)
    await users.add_member(workspace_id=workspace_id, user_id=removed.id, role="member")

    sessions = SessionStore(db_path)
    await sessions.initialize()
    session = await sessions.ensure_session(session_id=None, workspace_id=workspace_id, user_id=keeper.id)

    backend = LocalBackend(root_dir=tmp_path / "blobs")
    artifacts = ArtifactStore(backend=backend, db_path=db_path)
    await artifacts.initialize()

    async def _put(label: str) -> str:
        art = await artifacts.put_bytes(
            content=f"{label}-bytes\n".encode(),
            filename=f"{label}.csv",
            mime="text/csv",
            source="upload",
            kind="user_upload",
            created_by=None,
        )
        return art.id

    ids = {label: await _put(label) for label in ("bound_space", "col_only", "team", "null_before", "null_after")}

    async with aiosqlite.connect(db_path) as db:
        # Discriminating control: actual space artifact resolvable via ladder ① (upload binding); both flag settings deny.
        await db.execute(
            "INSERT INTO artifact_upload_bindings(artifact_id, workspace_id, user_id, session_id, created_at) VALUES (?,?,?,?,?)",
            (ids["bound_space"], workspace_id, keeper.id, session.id, _BEFORE),
        )
        await db.execute("UPDATE artifacts SET workspace_id=?, created_at=? WHERE id=?", (workspace_id, _BEFORE, ids["bound_space"]))
        # ★ Column-only, unresolvable by ladder: proves **column first, NULL falls back to inference** actually changes order.
        await db.execute("UPDATE artifacts SET workspace_id=?, created_at=? WHERE id=?", (workspace_id, _BEFORE, ids["col_only"]))
        # Team sentinel, **deliberately after the watermark**: if unrecognized, it enters the post-cutover NULL category
        # and fails closed, so 200 actually discriminates behavior.
        await db.execute("UPDATE artifacts SET workspace_id=?, created_at=? WHERE id=?", (TEAM_SCOPE_WORKSPACE_ID, _AFTER, ids["team"]))
        # Positive control/fail-closed: rows differ only in created_at.
        await db.execute("UPDATE artifacts SET workspace_id=NULL, created_at=? WHERE id=?", (_BEFORE, ids["null_before"]))
        await db.execute("UPDATE artifacts SET workspace_id=NULL, created_at=? WHERE id=?", (_AFTER, ids["null_after"]))
        # Watermark table **intentionally excluded** from ArtifactStore.initialize() (0099 header comment);
        # store-only fixtures insert it themselves, as Alembic does in production.
        # ⚠ Schema must exactly match 0099: activated_at is **nullable** (migration creates table without stamping).
        # Fixture NOT NULL invents a happy-path-only shape, missing actual unstamped states.
        await db.execute(
            f"CREATE TABLE IF NOT EXISTS {ARTIFACT_WORKSPACE_CUTOVER_TABLE} "
            "(singleton INTEGER PRIMARY KEY CHECK(singleton = 1), activated_at TEXT, updated_at TEXT NOT NULL)"
        )
        await db.execute(
            f"INSERT INTO {ARTIFACT_WORKSPACE_CUTOVER_TABLE}(singleton, activated_at, updated_at) VALUES (1, ?, ?)",
            (_CUTOVER, _CUTOVER),
        )
        await db.commit()

    # Only state change: remove space membership; space remains active, team membership unchanged.
    assert await users.remove_member(workspace_id, removed.id) is True

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(files_routes.router, prefix="/api/v1/files")
    app.state.user_store = users
    app.state.session_store = sessions
    app.state.artifact_store = artifacts
    app.state.artifact_backend = backend
    app.state.access_control_store = None
    app.state.settings = SimpleNamespace(auth_mode="prod", dev_default_user="unused")

    return SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False),
        users=users,
        team_id=team.id,
        workspace_id=workspace_id,
        keeper=keeper,
        removed=removed,
        ids=ids,
    )


def _get(ctx, label: str, user):
    return ctx.client.get(f"/api/v1/files/{ctx.ids[label]}", cookies={"bowhead_jwt": _token(user)})


@pytest.fixture(autouse=True)
def _capture_access_logs(caplog):
    caplog.set_level(logging.INFO, logger="bowhead_gateway.routes.files")


def _assert_concealed_denial(got, caplog) -> None:
    """A Space denial answers like a missing file; only the server log keeps the real reason."""
    assert got.status_code == 404, got.text
    assert got.json()["detail"]["error"]["code"] == "file_not_found"
    assert "reason=workspace_access_denied" in caplog.text


async def test_fixture_is_the_combination_under_test(ctx) -> None:
    """夹具自证：空间仍 active、团队资格仍在、只有空间成员资格是 removed。"""
    workspace = await ctx.users.get_workspace(ctx.workspace_id)
    assert workspace is not None and workspace["status"] == "active"
    assert await ctx.users.get_member_role(ctx.workspace_id, ctx.removed.id) is None
    assert await ctx.users.get_team_role(ctx.removed.id, ctx.team_id) is not None
    assert await ctx.users.get_member_role(ctx.workspace_id, ctx.keeper.id) is not None


async def test_space_member_reads_everything_attributable(ctx, fail_close) -> None:
    """正对照：仍是空间成员的人，四条**可归属**产物全部 200 —— 证明夹具/路由/认证本身是好的。

    没有这一条，下面的 403 无法区分「闸生效」和「夹具根本跑不通」
    （第一版就是全体 401，所有断言一起失去意义）。
    ★ 闸开那一档同样要跑：它证明批4 收紧的是「推不出归属的新产物」，不是把成员一起拦了。

    ★★ `null_after` **故意不在这条里**：fail-close 是按**产物**判的，不是按**人**判的——
      推不出归属就无从校验任何人的成员资格，于是它对所有人关门（空间成员、乃至产物作者本人）。
      那一档由 `test_fail_close_is_identity_independent` 单独钉住，不塞进这条笼统的循环里。
    """
    readable = [label for label in ctx.ids if not (fail_close and label == "null_after")]
    assert len(readable) == (4 if fail_close else 5), "正对照的样本集算错了"
    for label in readable:
        got = _get(ctx, label, ctx.keeper)
        assert got.status_code == 200, f"{label}: {got.text}"


async def test_fail_close_is_identity_independent(ctx, fail_close, caplog) -> None:
    """★★ fail-close 按**产物**判，不按**人**判：切点后推不出归属的产物，连**空间成员**也读不到。

    这是翻闸最尖锐的后果，必须显式钉住、不许藏在别的用例里：推不出归属 ⇒ 无从校验任何人的
    资格 ⇒ 对所有人关门，**包括该产物的作者**。所以翻闸的前置是 C4（切点后新产物的 NULL 率）
    必须 ≈ 0 —— 真有产物落到这一档，那是写入点的 bug，不是「该拦的人被拦了」。

    正对照：同一个人、同一次夹具，读**切点前**的同形产物仍然 200 ——
    否则这条 403 分不清是水位判定生效，还是这个人本来就什么都读不到。
    """
    legacy = _get(ctx, "null_before", ctx.keeper)
    assert legacy.status_code == 200, legacy.text

    got = _get(ctx, "null_after", ctx.keeper)
    if not fail_close:
        assert got.status_code == 200, got.text
        return
    _assert_concealed_denial(got, caplog)


async def test_bound_space_artifact_is_blocked_in_both_flag_states(ctx, fail_close, caplog) -> None:
    """判别对照：有真实 binding 的空间产物，**两档都**该拦已被移出空间的人。

    它若放行，说明夹具没造对（例如 binding 没落库 ⇒ fail-open 早退），本文件其余结论作废。
    """
    got = _get(ctx, "bound_space", ctx.removed)
    _assert_concealed_denial(got, caplog)


async def test_legacy_unattributed_artifact_stays_readable(ctx, fail_close) -> None:
    """★★ 正对照（本文件最要紧的一条）：**切点前**推不出归属的老产物，两档都仍读得到。

    批4 的口径拍板是「不为历史买单，只保证 going forward」。这条就是那句话的可执行形式：
    它和 `null_after` 只差 `created_at` 一个字段，所以那条 403 只可能来自**水位判定**。
    """
    got = _get(ctx, "null_before", ctx.removed)
    assert got.status_code == 200, got.text


async def test_unattributed_artifact_created_after_cutover_fails_closed(ctx, fail_close, caplog) -> None:
    """切点后仍推不出归属 = 写入点漏了 ⇒ 闸开时拦掉；闸关时逐字节今天的行为（放行）。"""
    got = _get(ctx, "null_after", ctx.removed)
    if not fail_close:
        assert got.status_code == 200, got.text
        return
    _assert_concealed_denial(got, caplog)
    assert b"null_after-bytes" not in got.content


async def test_team_sentinel_is_authorized_at_team_level(ctx, fail_close) -> None:
    """`__team__` ⇒ 团队级授权：已被移出**空间**但仍在**团队**的人照样读得到。

    ★ 这条产物的 `created_at` 在水位**之后**：哨兵若没被认出来，它会掉进
      「NULL 且切点后」那一档而 403。所以闸开时的 200 是真判别 —— 它同时证明了
      「哨兵被识别」和「团队级授权放行」，而不是顺带被 fail-open 放过去的。
    """
    got = _get(ctx, "team", ctx.removed)
    assert got.status_code == 200, got.text


async def test_column_is_read_before_the_derivation_ladder(ctx, fail_close, caplog) -> None:
    """★ 「先读列、为 NULL 再回落推导」：只有列、梯子推不出的产物。

    闸关 = 今天：只走梯子 ⇒ 推不出 ⇒ fail-open 早退 ⇒ 200。
    闸开：列里写着 `ws_*` ⇒ 按空间判 ⇒ 已被移出的人 403。
    这条是**列真的被读了**的唯一证据 —— 其余用例里列和梯子的结论一致，替换不掉它。
    """
    got = _get(ctx, "col_only", ctx.removed)
    if not fail_close:
        assert got.status_code == 200, got.text
        return
    _assert_concealed_denial(got, caplog)
