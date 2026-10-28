"""`/preview` 的 `max_rows` 上限（chart-config-ux 批5）。

前端表格上虚拟滚动后，「展开全部」按 `TABLE_FULL_ROWS_LIMIT` 取全量，那个常量和
这里的 `le` **必须同数**：喊得比服务端上限大，用户点「展开全部」直接吃 422。
跨语言钉不到一起，两边各钉一半——前端那半在 `lib/chart/view-transforms.test.ts`
（"表格上限必须与后端 /preview 的 le=10_000 同数"）。

这里**走真路由**而不是读函数签名：签名里那个 `Query(le=...)` 是不是真被执行，
取决于 FastAPI 的校验链，看得见 ≠ 跑得到。判据因此是
「10000 拿得到 10000 行、10001 吃 422」。
"""

from __future__ import annotations

import csv
import io
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.session_store import SessionStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import files
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Frontend `TABLE_FULL_ROWS_LIMIT`; update both limits together or Expand all receives 422.
FRONTEND_TABLE_FULL_ROWS_LIMIT = 10_000

# CSV longer than the limit: only total rows > limit can prove actual truncation;
# otherwise receiving all rows could mean either an effective limit or a short file (controls indistinguishable).
_TOTAL_ROWS = FRONTEND_TABLE_FULL_ROWS_LIMIT + 137


def _big_csv() -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["门店", "营业额"])
    for i in range(_TOTAL_ROWS):
        writer.writerow([f"S{i}", _TOTAL_ROWS - i])
    return buf.getvalue().encode("utf-8")


@pytest.fixture
async def preview_ctx(tmp_path):
    db_path = tmp_path / "bowhead.db"
    users = UserStore(db_path)
    await users.initialize()
    team = await users.create_team(name="Preview Cap", max_seats=10)
    user = await users.create_user(
        team_id=team.id,
        email="preview-cap@example.com",
        password="Passw0rd!",
    )
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="Preview Cap",
        creator_user_id=user.id,
    )
    sessions = SessionStore(db_path)
    await sessions.initialize()
    session = await sessions.ensure_session(
        session_id=None,
        workspace_id=workspace_id,
        user_id=user.id,
    )
    backend = LocalBackend(root_dir=tmp_path / "files")
    artifacts = ArtifactStore(backend=backend, db_path=db_path)
    await artifacts.initialize()
    artifact = await artifacts.put_bytes(
        content=_big_csv(),
        filename="shops.csv",
        mime="text/csv",
        source="upload",
        created_by=user.id,
        session_id=session.id,
        kind="user_upload",
    )
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO artifact_upload_bindings(artifact_id, workspace_id, user_id, session_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (artifact.id, workspace_id, user.id, session.id, "2026-09-02T00:00:00+00:00"),
        )
        await db.commit()

    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1/files")
    app.state.artifact_store = artifacts
    app.state.session_store = sessions
    app.state.user_store = users
    app.state.settings = SimpleNamespace(dev_default_user=user.id, auth_mode="dev")
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Bowhead-User-Id": user.id, "X-Space-Id": workspace_id}
    return client, artifact.id, headers


def _preview(ctx, max_rows: int):
    client, artifact_id, headers = ctx
    return client.get(
        f"/api/v1/files/{artifact_id}/preview",
        params={"mode": "rows", "max_rows": max_rows},
        headers=headers,
    )


async def test_at_cap_returns_all_requested_rows(preview_ctx) -> None:
    """★ 上限值本身必须拿得到——前端「展开全部」就是按这个数请求的。"""
    resp = _preview(preview_ctx, FRONTEND_TABLE_FULL_ROWS_LIMIT)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["rows"]) == FRONTEND_TABLE_FULL_ROWS_LIMIT
    # Report actual total (footer source); truncation must not shrink total_rows too.
    assert body["total_rows"] == _TOTAL_ROWS


async def test_above_cap_is_rejected(preview_ctx) -> None:
    """负对照：上限确实在校验链上跑到了，而不只是写在签名里。"""
    resp = _preview(preview_ctx, FRONTEND_TABLE_FULL_ROWS_LIMIT + 1)
    assert resp.status_code == 422, resp.text


async def test_default_stays_conservative(preview_ctx) -> None:
    """默认值不跟着上限走：绝大多数调用方（缩略预览）只要头几行。

    116 语料 p99=235 行，默认 100 覆盖典型用途；把默认抬到上限会让每个预览拖 1.2MB。
    """
    client, artifact_id, headers = preview_ctx
    resp = client.get(
        f"/api/v1/files/{artifact_id}/preview",
        params={"mode": "rows"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["rows"]) == 100


def test_report_scoped_preview_shares_the_same_cap() -> None:
    """两条 `/preview`（文件库 / 报告内 artifact）必须同上限：前端不区分走哪条。

    canvas 那条要整套 report 闭包才跑得起来，这里只比对两处声明——
    它的行为由 canvas 自己的用例覆盖，本条只防「一处改、一处忘」。
    """
    import inspect

    from annotated_types import Le
    from bowhead_gateway.routes.canvas import get_report_artifact_preview

    def _le(handler) -> int | None:
        query = inspect.signature(handler).parameters["max_rows"].default
        return next((m.le for m in query.metadata if isinstance(m, Le)), None)

    assert _le(get_report_artifact_preview) == FRONTEND_TABLE_FULL_ROWS_LIMIT
    assert _le(files.file_preview) == FRONTEND_TABLE_FULL_ROWS_LIMIT
