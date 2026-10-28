"""知识库文件路由：个人文件归属按 scope_id、范围变更迁移失败回滚、单条分块读取。

真 KbFilesStore + 真 KbStore（chromadb 临时目录）；只替身当前用户与原件存储。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from bowhead_core.kb.document import ChunkMetadata, KbChunk, make_chunk_id
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.store import KbStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

TEAM = "team_1"
WS = "ws_1"
ADMIN = User(id="u_admin", email="boss@admin.test", team_id=TEAM, tier="ultra", email_verified=True)
UPLOADER = User(id="u_x", email="x@member.test", team_id=TEAM, tier="ultra", email_verified=True)
_DIM = 8


class _ArtifactStore:
    def __init__(self) -> None:
        self.backend = SimpleNamespace(read=self._read)

    async def get(self, artifact_id: str):
        return SimpleNamespace(path=f"/blob/{artifact_id}")

    async def _read(self, path: str) -> bytes:
        return b"original bytes"


def _chunk(file_id: str, idx: int, content: str, *, scope: str, scope_owner: str, workspace_id: str) -> KbChunk:
    return KbChunk(
        chunk_id=make_chunk_id(file_id, idx, content),
        file_id=file_id,
        content=content,
        embedding=[1.0] * _DIM,
        metadata=ChunkMetadata(
            file_id=file_id,
            file_name="doc.md",
            scope=scope,  # type: ignore[arg-type]
            scope_owner=scope_owner,
            workspace_id=workspace_id,
            team_id=TEAM,
            chunk_index=idx,
        ),
    )


@pytest.fixture
async def env(initialize_migrated_kb_files_database, tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "admin.test")
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    files = KbFilesStore(tmp_path / "kb_files.db")
    await files.initialize()
    store = KbStore(str(tmp_path / f"chroma_{uuid.uuid4().hex}"))
    app = FastAPI()
    app.include_router(kb.router)
    current = {"user": ADMIN}
    app.dependency_overrides[_current_user] = lambda: current["user"]
    app.state.kb_files_store = files
    app.state.kb_store = store
    app.state.artifact_store = _ArtifactStore()

    async def index(*, scope: str, scope_id: str, uploaded_by: str, contents: list[str]) -> tuple[str, list[str]]:
        file_id = await files.create(
            artifact_id=f"art_{uuid.uuid4().hex[:6]}",
            filename="doc.md",
            mime_type="text/markdown",
            size_bytes=10,
            scope=scope,
            scope_id=scope_id,
            workspace_id=WS,
            team_id=TEAM,
            uploaded_by=uploaded_by,
            initial_parse_status="done",
        )
        dto = await files.get(file_id)
        assert dto is not None
        chunks = [_chunk(file_id, i, c, scope=scope, scope_owner=scope_id, workspace_id=WS) for i, c in enumerate(contents, start=1)]
        await store.add_chunks(dto, chunks)
        return file_id, [c.chunk_id for c in chunks]

    def as_user(user: User) -> None:
        current["user"] = user

    return SimpleNamespace(client=TestClient(app, raise_server_exceptions=False), files=files, store=store, index=index, as_user=as_user)


def _code(resp) -> str:
    return resp.json()["detail"]["error"]["code"]


# ─── Personal file ownership = scope_id ─────────────────────


async def test_admin_owns_a_file_moved_into_their_personal_scope(env) -> None:
    """管理员把 X 的文件改进自己的个人范围：管理员能打开/看分块/下载，原上传者不能再下载。"""
    file_id, _ = await env.index(scope="personal", scope_id=ADMIN.id, uploaded_by=UPLOADER.id, contents=["第一段"])

    for path in (f"/api/v1/kb/files/{file_id}", f"/api/v1/kb/files/{file_id}/chunks", f"/api/v1/kb/files/{file_id}/download"):
        assert env.client.get(path).status_code == 200, path

    env.as_user(UPLOADER)
    for path in (f"/api/v1/kb/files/{file_id}", f"/api/v1/kb/files/{file_id}/download"):
        resp = env.client.get(path)
        assert resp.status_code == 404, path
        assert _code(resp) == "kb_file_not_found"


# ─── Scope change: migration failure rolls back SQL ─────────


class _FailingDeleteColl:
    def __init__(self, inner) -> None:
        self._inner = inner

    def delete(self, *_a, **_kw):
        raise RuntimeError("vector store down")

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


async def test_scope_change_rolls_back_when_vector_store_fails_midway(env, monkeypatch) -> None:
    """空间→团队跨集合迁移时旧集合删除失败：结构化错误，SQL 范围回到空间，分块全部仍在空间集合。"""
    file_id, chunk_ids = await env.index(scope="space", scope_id=WS, uploaded_by=ADMIN.id, contents=["甲", "乙"])
    real_routing = env.store._routing_collection

    async def _routing(scope, workspace_id, team_id, *, create):
        coll = await real_routing(scope, workspace_id, team_id, create=create)
        return _FailingDeleteColl(coll) if coll is not None and scope != "team" else coll

    monkeypatch.setattr(env.store, "_routing_collection", _routing)

    resp = env.client.patch(
        f"/api/v1/kb/files/{file_id}/scope",
        json={"new_scope": "team", "new_scope_id": TEAM},
        headers={"X-Space-Id": WS},
    )

    assert resp.status_code == 500
    assert _code(resp) == "kb_scope_change_failed"
    monkeypatch.setattr(env.store, "_routing_collection", real_routing)
    row = await env.files.get(file_id)
    assert row is not None and (row.scope, row.scope_id, row.workspace_id) == ("space", WS, WS)
    assert sorted(cid for cid, _, _ in await env.store.list_active_chunks(row)) == sorted(chunk_ids)
    team_view = row.model_copy(update={"scope": "team", "scope_id": TEAM, "workspace_id": ""})
    assert await env.store.list_active_chunks(team_view) == []


async def test_scope_change_rolls_back_on_unexpected_vector_store_error(env, monkeypatch) -> None:
    """迁移抛出非迁移类异常（如向量库连接错误）也回滚并返回结构化错误，而不是裸 500。"""
    file_id, _ = await env.index(scope="space", scope_id=WS, uploaded_by=ADMIN.id, contents=["甲"])

    async def _boom(**_kw):
        raise ConnectionError("chroma unreachable")

    monkeypatch.setattr(env.store, "update_chunks_scope", _boom)

    resp = env.client.patch(
        f"/api/v1/kb/files/{file_id}/scope",
        json={"new_scope": "team", "new_scope_id": TEAM},
        headers={"X-Space-Id": WS},
    )

    assert resp.status_code == 500
    assert _code(resp) == "kb_scope_change_failed"
    row = await env.files.get(file_id)
    assert row is not None and (row.scope, row.scope_id) == ("space", WS)


# ─── GET /files/{id}/chunks?chunk_id= ──────────────────


async def test_chunks_route_returns_only_the_requested_chunk(env) -> None:
    file_id, chunk_ids = await env.index(scope="personal", scope_id=ADMIN.id, uploaded_by=ADMIN.id, contents=["第一段", "第二段", "第三段"])

    whole = env.client.get(f"/api/v1/kb/files/{file_id}/chunks")
    one = env.client.get(f"/api/v1/kb/files/{file_id}/chunks", params={"chunk_id": chunk_ids[1]})

    assert whole.status_code == 200 and whole.json()["total"] == 3
    assert one.status_code == 200
    assert one.json() == {"chunks": [whole.json()["chunks"][1]], "total": 1}


async def test_chunks_route_404s_for_a_chunk_not_in_this_file(env) -> None:
    file_id, _ = await env.index(scope="personal", scope_id=ADMIN.id, uploaded_by=ADMIN.id, contents=["本文件"])
    _, other_chunks = await env.index(scope="personal", scope_id=ADMIN.id, uploaded_by=ADMIN.id, contents=["别的文件"])

    for chunk_id in (other_chunks[0], "chunk_missing"):
        resp = env.client.get(f"/api/v1/kb/files/{file_id}/chunks", params={"chunk_id": chunk_id})
        assert resp.status_code == 404
        assert _code(resp) == "kb_chunk_not_found"


async def test_single_chunk_read_keeps_the_file_permission_check(env) -> None:
    file_id, chunk_ids = await env.index(scope="personal", scope_id=ADMIN.id, uploaded_by=ADMIN.id, contents=["私人内容"])
    env.as_user(UPLOADER)

    resp = env.client.get(f"/api/v1/kb/files/{file_id}/chunks", params={"chunk_id": chunk_ids[0]})

    assert resp.status_code == 404
