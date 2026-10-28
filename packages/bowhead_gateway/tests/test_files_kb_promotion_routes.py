"""file-management-v2 —— 「加入知识库 / 移出知识库」的**路由级**验证。

这两个端点在 code review 中从 `routes/files.py` 搬到了 `routes/kb.py`（原位置
少了一整条 KB 授权链 = 越权）。搬完之前只有 mock 层的前端 e2e 证明按钮能点，
**没有任何测试证明真能晋升成功**——本文件补上：走真 kb router + 真
`KbFilesStore` + 真 `ArtifactStore`，断言授权链的每一环。

覆盖：
  1. 晋升成功 → kb_files 里真出现一行，且**复用同一个 artifact**（零拷贝）
  2. 不是自己的 artifact → 403（越权洞的直接回归）
  3. `scope_id` 由服务端解析——客户端塞不进来
  4. mime 不可解析 → 422 + reason（前端据此说人话，不是 500）
  5. 移出 → kb_files 行走了，**artifact 本身还在**（文件不动，只是 AI 不再参考）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.session_store import SessionStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

_TEAM = "team_t"
_WS = "ws_t"
_UID = "u_alice"
_OTHER = "u_bob"
_H = {"X-Space-Id": _WS}


def _user() -> User:
    return User(id=_UID, email="alice@test.com", team_id=_TEAM, tier="ultra", email_verified=True)


class _UserStore:
    async def get_team_llm_config_with_version(self, team_id: str) -> tuple[dict, int]:
        return {}, 1

    async def get_user_team_id(self, user_id: str) -> str | None:
        return _TEAM if user_id in (_UID, _OTHER) else None

    async def get_workspace(self, workspace_id: str) -> dict[str, str] | None:
        if workspace_id != _WS:
            return None
        return {
            "id": _WS,
            "team_id": _TEAM,
            "status": "active",
        }

    async def get_workspace_team_id(self, workspace_id: str) -> str | None:
        return _TEAM if workspace_id == _WS else None

    async def get_member_role(self, workspace_id: str, user_id: str) -> str | None:
        return "member" if workspace_id == _WS and user_id in (_UID, _OTHER) else None

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        return "member" if team_id == _TEAM else None


class _NoopWorker:
    """索引是异步 worker 的事；本文件只验路由 + 授权 + 落库契约。"""

    async def parse_and_index(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
        return None


class _FakeChroma:
    """移出走 `_delete_file_core`，那里要清 chroma 里的 chunk。真 chromadb 不在
    单测范围内，用 fake 记一笔并断言确实被调到——清理漏了就是"检索还命中、原文
    已不在"的幽灵条目。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def soft_delete_file(self, file) -> None:  # noqa: ANN001
        self.deleted.append(file.file_id)


@pytest.fixture
async def ctx(initialize_migrated_kb_files_database, tmp_path: Path):
    db = tmp_path / "a.db"
    await SessionStore(db).initialize()
    artifacts = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "blob"), db_path=db)
    await artifacts.initialize()
    initialize_migrated_kb_files_database(tmp_path / "kb.db")
    files_store = KbFilesStore(tmp_path / "kb.db")
    await files_store.initialize()
    access = AccessControlStore(tmp_path / "ac.db")
    await access.initialize()

    app = FastAPI()
    app.include_router(kb.router)
    app.dependency_overrides[_current_user] = _user
    app.state.artifact_store = artifacts
    app.state.kb_files_store = files_store
    app.state.access_control_store = access
    app.state.user_store = _UserStore()
    app.state.kb_worker = _NoopWorker()
    chroma = _FakeChroma()
    app.state.kb_store = chroma
    return TestClient(app), artifacts, files_store, chroma


async def _art(store: ArtifactStore, *, owner=_UID, filename="manual.pdf", mime="application/pdf"):
    return await store.put_bytes(
        content=b"%PDF-1.4 fake",
        filename=filename,
        mime=mime,
        source="upload",
        created_by=owner,
        kind="user_upload",
    )


async def test_promote_creates_kb_row_reusing_the_same_artifact(ctx) -> None:
    """零拷贝晋升：kb_files 里出现一行，指回**同一个** artifact_id。"""
    client, artifacts, files_store, chroma = ctx
    art = await _art(artifacts)

    r = client.post(
        "/api/v1/kb/files/from-artifact",
        json={"artifact_id": art.id, "scope": "personal"},
        headers=_H,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["artifact_id"] == art.id, "必须复用原 blob，不产生第二份字节"

    file_id = await files_store.find_active_by_artifact(art.id)
    assert file_id is not None and file_id == body["file_id"]
    assert art.id in await files_store.artifact_ids_in_kb([art.id]), "in_kb 反查也要认得它"


async def test_promote_without_embedding_model_explains_why_no_kb_row_was_created(ctx) -> None:
    client, artifacts, files_store, _ = ctx
    art = await _art(artifacts)
    client.app.state.kb_worker = None

    response = client.post(
        "/api/v1/kb/files/from-artifact",
        json={"artifact_id": art.id, "scope": "space"},
        headers=_H,
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {"error": {"code": "kb_embedding_not_configured"}}
    assert await files_store.find_active_by_artifact(art.id) is None


async def test_promoting_someone_elses_artifact_is_forbidden(ctx) -> None:
    """**越权回归**：端点搬家前就漏在这——任何人拿到 artifact_id 就能把别人的
    文件塞进自己的知识库。"""
    client, artifacts, *_ = ctx
    art = await _art(artifacts, owner=_OTHER, filename="theirs.pdf")
    r = client.post(
        "/api/v1/kb/files/from-artifact",
        json={"artifact_id": art.id, "scope": "personal"},
        headers=_H,
    )
    missing = client.post(
        "/api/v1/kb/files/from-artifact",
        json={"artifact_id": "art_missing", "scope": "personal"},
        headers=_H,
    )
    # Someone else's file is refused exactly like a missing one.
    assert r.status_code == 404
    assert (r.status_code, r.content) == (missing.status_code, missing.content)


async def test_scope_id_is_server_resolved_not_client_supplied(ctx) -> None:
    """请求体里没有 scope_id 这一项——服务端按 scope 自己解析。
    客户端多塞的字段被忽略，不能借此写进别的 workspace/team。"""
    client, artifacts, files_store, chroma = ctx
    art = await _art(artifacts)
    r = client.post(
        "/api/v1/kb/files/from-artifact",
        json={"artifact_id": art.id, "scope": "space", "scope_id": "ws_someone_else"},
        headers=_H,
    )
    assert r.status_code == 201, r.text
    file_id = await files_store.find_active_by_artifact(art.id)
    row = await files_store.get(file_id)
    assert row.scope_id == _WS, "scope_id 必须是服务端解析出的当前空间"


async def test_unparsable_mime_is_code_only_422_not_500(ctx) -> None:
    """规则不允许 ≠ 故障；页面按稳定 code 本地化，不读取动态 MIME reason。

    注意选旧版 .ppt 而不是 png/csv：**png 是 KB 支持的**（vision LLM 抽取，`_ALLOWED_MIMES`
    里有）。本测试第一版拿 png 当反例，真跑起来返回 201——正好说明"凭印象猜
    支持范围"会写出假的守卫。csv 也在 98f05509a 起可入库；.ppt 是白名单刻意排除的。"""
    client, artifacts, *_ = ctx
    art = await _art(artifacts, filename="deck.ppt", mime="application/vnd.ms-powerpoint")
    r = client.post(
        "/api/v1/kb/files/from-artifact",
        json={"artifact_id": art.id, "scope": "personal"},
        headers=_H,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"error": {"code": "kb_promotion_not_supported"}}


async def test_remove_drops_kb_row_but_keeps_the_file(ctx) -> None:
    """移出知识库 = AI 不再自动参考；文件本身**不能**被删掉。"""
    client, artifacts, files_store, chroma = ctx
    art = await _art(artifacts)
    assert (
        client.post(
            "/api/v1/kb/files/from-artifact",
            json={"artifact_id": art.id, "scope": "personal"},
            headers=_H,
        ).status_code
        == 201
    )

    r = client.delete(f"/api/v1/kb/files/by-artifact/{art.id}", headers=_H)
    assert r.status_code == 204, r.text
    assert await files_store.find_active_by_artifact(art.id) is None
    assert (await artifacts.get(art.id)) is not None, "文件本身必须还在"
    assert chroma.deleted, "chroma 里的 chunk 必须一并清掉，否则检索命中幽灵条目"


async def test_space_remove_uses_the_active_space_context(ctx) -> None:
    """Files 客户端必须把当前空间带给 DELETE，否则共享条目会 fail-closed 成 403。"""
    client, artifacts, files_store, chroma = ctx
    art = await _art(artifacts, filename="space.pdf")
    assert (
        client.post(
            "/api/v1/kb/files/from-artifact",
            json={"artifact_id": art.id, "scope": "space"},
            headers=_H,
        ).status_code
        == 201
    )

    r = client.delete(f"/api/v1/kb/files/by-artifact/{art.id}", headers=_H)
    assert r.status_code == 204, r.text
    assert await files_store.find_active_by_artifact(art.id) is None
    assert (await artifacts.get(art.id)) is not None
    assert chroma.deleted


async def test_remove_unknown_artifact_is_404(ctx) -> None:
    client, artifacts, *_ = ctx
    art = await _art(artifacts)
    assert client.delete(f"/api/v1/kb/files/by-artifact/{art.id}", headers=_H).status_code == 404


# ── No space context: actual /files page shape ──────────────


async def test_personal_promote_works_without_a_space_header(ctx) -> None:
    """**prod 事故回归（0.87.0 dogfood）**：`/files` 是个人资产库，**不发
    `X-Space-Id`**。端点原本用严格版 `_make_scope_context_checked`，于是
    scope="personal"（scope_id = user.id，压根不需要空间）也吃 400
    `workspace_required` —— 文件页「加入知识库」在 prod 100% 失败。

    本文件此前每个请求都硬塞了 `_H = {"X-Space-Id": ...}`，等于**测试替真实
    调用方补齐了它根本不会发的东西**，所以全绿却挡不住这个洞。这条不带 header。
    """
    client, artifacts, files_store, _ = ctx
    art = await _art(artifacts)
    r = client.post(
        "/api/v1/kb/files/from-artifact",
        json={"artifact_id": art.id, "scope": "personal"},
    )
    assert r.status_code == 201, r.text
    assert await files_store.find_active_by_artifact(art.id) is not None


async def test_personal_remove_works_without_a_space_header(ctx) -> None:
    """移出走同一把锁，同一个洞。"""
    client, artifacts, files_store, _ = ctx
    art = await _art(artifacts)
    assert (
        client.post(
            "/api/v1/kb/files/from-artifact",
            json={"artifact_id": art.id, "scope": "personal"},
        ).status_code
        == 201
    )
    assert client.delete(f"/api/v1/kb/files/by-artifact/{art.id}").status_code == 204
    assert await files_store.find_active_by_artifact(art.id) is None


async def test_space_scope_still_requires_a_space(ctx) -> None:
    """放松只针对 personal —— 要写进空间仍必须有空间上下文，不能空对空。"""
    client, artifacts, *_ = ctx
    art = await _art(artifacts)
    r = client.post(
        "/api/v1/kb/files/from-artifact",
        json={"artifact_id": art.id, "scope": "space"},
    )
    assert r.status_code == 400, r.text
