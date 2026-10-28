"""POST /api/v1/kb/search — 页面深搜与对话检索同源 + 相关度下限（知识库体验整治 PRD FR6.1–6.3）。

真 KbStore（chromadb 临时目录）+ 真 KbRetriever + 真 KbFilesStore；只替身 embedder / reranker。
"""

from __future__ import annotations

import uuid

import pytest
from bowhead_core.kb.document import ChunkMetadata, KbChunk, make_chunk_id
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.reranker import Reranker
from bowhead_core.kb.retriever import KbRetriever
from bowhead_core.kb.schemas import KbFileDTO
from bowhead_core.kb.store import KbStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import kb
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

_DIM = 8


class _Embedder:
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * _DIM for _ in texts]


class _KeywordReranker:
    """Cross-encoder 替身：文档含查询词 → 给定分；否则 0.08（gte-rerank-v2 在 116 真链路上完全无关查询的典型分）。"""

    def __init__(self, hits: dict[str, float] | None = None, model_id: str = "gte-rerank-v2") -> None:
        self.hits = hits or {}
        self.model_id = model_id

    async def rerank(self, query: str, documents: list[str], top_k: int = 5) -> list[tuple[int, float]]:
        scored = []
        for i, doc in enumerate(documents):
            score = 0.08
            for marker, value in self.hits.items():
                if marker in doc:
                    score = value
            if query in doc:
                score = max(score, 0.8)
            scored.append((i, score))
        scored.sort(key=lambda p: p[1], reverse=True)
        return scored[:top_k]


class _FallbackReranker:
    """rerank 服务不可用：走真 Reranker 的回退打分（合成的名次分，不含相关度）。"""

    async def rerank(self, query: str, documents: list[str], top_k: int = 5) -> list[tuple[int, float]]:
        return Reranker._fallback_cosine(min(top_k, len(documents)))


def _chunk(
    file_id: str,
    content: str,
    *,
    scope: str,
    scope_owner: str,
    workspace_id: str,
    team_id: str,
    role: str = "data",
) -> KbChunk:
    return KbChunk(
        chunk_id=make_chunk_id(file_id, 1, content),
        file_id=file_id,
        content=content,
        embedding=[1.0] * _DIM,
        metadata=ChunkMetadata(
            file_id=file_id,
            file_name=f"{file_id}.xlsx",
            scope=scope,  # type: ignore[arg-type]
            scope_owner=scope_owner,
            workspace_id=workspace_id,
            team_id=team_id,
            chunk_index=1,
            role=role,  # type: ignore[arg-type]
        ),
    )


async def _index(
    store: KbStore,
    files: KbFilesStore,
    *,
    name: str,
    content: str,
    scope: str,
    scope_id: str,
    workspace_id: str,
    team_id: str,
    uploaded_by: str,
    role: str = "data",
) -> str:
    file_id = await files.create(
        artifact_id=f"art_{name}",
        filename=f"{name}.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=100,
        scope=scope,
        scope_id=scope_id,
        workspace_id=workspace_id,
        team_id=team_id,
        uploaded_by=uploaded_by,
        initial_parse_status="done",
    )
    dto = KbFileDTO.model_validate(
        {
            "file_id": file_id,
            "artifact_id": f"art_{name}",
            "filename": f"{name}.xlsx",
            "mime_type": "application/octet-stream",
            "size_bytes": 100,
            "scope": scope,
            "scope_id": scope_id,
            "workspace_id": workspace_id,
            "team_id": team_id,
            "uploaded_by": uploaded_by,
            "uploaded_at": "2026-09-24T00:00:00+00:00",
            "parse_status": "done",
        }
    )
    await store.add_chunks(
        dto,
        [
            _chunk(
                name,
                content,
                scope=scope,
                scope_owner=scope_id,
                workspace_id=workspace_id,
                team_id=team_id,
                role=role,
            )
        ],
    )
    return file_id


@pytest.fixture
async def search_env(initialize_migrated_kb_files_database, tmp_path):
    users = UserStore(tmp_path / "bowhead.db")
    await users.initialize()
    team = await users.create_team(name="KB Search", max_seats=10)
    owner = await users.create_user(team_id=team.id, email="owner@example.com", password="Passw0rd!")
    other = await users.create_user(team_id=team.id, email="other@example.com", password="Passw0rd!")
    ws_a = await users.create_local_workspace(team_id=team.id, name="A", creator_user_id=owner.id)
    ws_b = await users.create_local_workspace(team_id=team.id, name="B", creator_user_id=owner.id)

    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    files = KbFilesStore(tmp_path / "kb_files.db")
    await files.initialize()
    # chromadb caches process clients by path; pytest with retention=failed may reuse a just-deleted temp directory name;
    # random suffix avoids cached connections to deleted directories (readonly database).
    store = KbStore(str(tmp_path / f"chroma_{uuid.uuid4().hex}"))
    common = {"team_id": team.id}
    # Owner's personal files: one with empty space (historical upload path), one uploaded in space B.
    await _index(
        store,
        files,
        name="legacy_personal",
        content="福州 legacy 个人",
        scope="personal",
        scope_id=owner.id,
        workspace_id="",
        uploaded_by=owner.id,
        **common,
    )
    await _index(
        store, files, name="b_personal", content="福州 B 个人", scope="personal", scope_id=owner.id, workspace_id=ws_b, uploaded_by=owner.id, **common
    )
    # Current space A's space file; space B's space file (must not cross boundaries).
    await _index(store, files, name="a_space", content="福州 A 空间", scope="space", scope_id=ws_a, workspace_id=ws_a, uploaded_by=owner.id, **common)
    await _index(store, files, name="b_space", content="福州 B 空间", scope="space", scope_id=ws_b, workspace_id=ws_b, uploaded_by=owner.id, **common)
    # Another user's personal file in the same collection that contains owner's personal files.
    await _index(
        store,
        files,
        name="other_personal",
        content="福州 别人 个人",
        scope="personal",
        scope_id=other.id,
        workspace_id="",
        uploaded_by=other.id,
        **common,
    )
    await _index(store, files, name="team_doc", content="福州 团队", scope="team", scope_id=team.id, workspace_id="", uploaded_by=owner.id, **common)

    app = FastAPI()
    app.include_router(kb.router)
    app.dependency_overrides[_current_user] = lambda: owner
    app.state.user_store = users
    app.state.kb_files_store = files
    app.state.kb_store = store

    def use_reranker(reranker) -> None:
        app.state.kb_retriever = KbRetriever(embedder=_Embedder(), store=store, reranker=reranker)  # type: ignore[arg-type]

    use_reranker(_KeywordReranker())
    client = TestClient(app, raise_server_exceptions=False)
    return client, {"X-Space-Id": ws_a}, use_reranker


def _names(resp) -> set[str]:
    assert resp.status_code == 200, resp.text
    return {c["file_name"].removesuffix(".xlsx") for c in resp.json()["chunks"]}


async def test_page_search_finds_own_personal_files_across_spaces(search_env) -> None:
    client, headers, _ = search_env

    resp = client.post("/api/v1/kb/search", json={"query": "福州", "top_k": 10}, headers=headers)

    # Find personal files with empty space or uploaded in another space; exclude others' personal files and other spaces' space files.
    assert _names(resp) == {"legacy_personal", "b_personal", "a_space", "team_doc"}


@pytest.mark.parametrize(
    ("scope_filter", "expected"),
    [
        (["personal"], {"legacy_personal", "b_personal"}),
        (["space"], {"a_space"}),
        (["team"], {"team_doc"}),
    ],
)
async def test_page_search_respects_scope_filter(search_env, scope_filter, expected) -> None:
    client, headers, _ = search_env

    resp = client.post(
        "/api/v1/kb/search",
        json={"query": "福州", "top_k": 10, "scope_filter": scope_filter},
        headers=headers,
    )

    assert _names(resp) == expected


async def test_page_search_unrelated_query_returns_no_hits(search_env) -> None:
    client, headers, _ = search_env

    resp = client.post("/api/v1/kb/search", json={"query": "火星殖民计划", "top_k": 10}, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["chunks"] == []


async def test_page_search_floor_boundary_uses_raw_rerank_score(search_env) -> None:
    client, headers, use_reranker = search_env
    floor = kb.KB_PAGE_SEARCH_MIN_RERANK_SCORE_BY_MODEL["gte-rerank-v2"]
    use_reranker(_KeywordReranker({"legacy": floor + 0.001, "B 个人": floor - 0.001, "团队": floor}))

    resp = client.post("/api/v1/kb/search", json={"query": "查询", "top_k": 10}, headers=headers)

    # Keep scores equal to the floor, drop below it; data-role +0.01 boost must not recover below-floor chunks.
    assert _names(resp) == {"legacy_personal", "team_doc"}


async def test_page_search_rerank_fallback_does_not_apply_floor(search_env) -> None:
    client, headers, use_reranker = search_env
    use_reranker(_FallbackReranker())

    resp = client.post("/api/v1/kb/search", json={"query": "火星殖民计划", "top_k": 10}, headers=headers)

    body = resp.json()
    assert resp.status_code == 200
    assert _names(resp) == {"legacy_personal", "b_personal", "a_space", "team_doc"}
    assert all(c["rerank_score"] is None for c in body["chunks"])


async def test_page_search_model_without_calibrated_floor_keeps_all_results(search_env) -> None:
    """未标定的 rerank 模型（qwen3-rerank 无关与相关分数重叠）不套下限，不能误删真结果。"""
    client, headers, use_reranker = search_env
    use_reranker(_KeywordReranker(model_id="qwen3-rerank"))

    resp = client.post("/api/v1/kb/search", json={"query": "火星殖民计划", "top_k": 10}, headers=headers)

    assert _names(resp) == {"legacy_personal", "b_personal", "a_space", "team_doc"}


async def test_page_search_drops_chunks_of_soft_deleted_file(search_env) -> None:
    """兜底：分块仍是 active、但文件行已软删（删除与解析写入交错漏网）时不出现在深搜结果里。"""
    client, headers, _ = search_env
    files: KbFilesStore = client.app.state.kb_files_store
    store: KbStore = client.app.state.kb_store
    ws_a = headers["X-Space-Id"]
    live = await files.get((await files.list_for_ws(ws_a))[0].file_id)
    assert live is not None
    file_id = await files.create(
        artifact_id="art_deleted",
        filename="deleted_space.xlsx",
        mime_type="application/octet-stream",
        size_bytes=1,
        scope="space",
        scope_id=ws_a,
        workspace_id=ws_a,
        team_id=live.team_id,
        uploaded_by=live.uploaded_by,
        initial_parse_status="done",
    )
    dto = await files.get(file_id)
    assert dto is not None
    chunk = _chunk(file_id, "福州 已删除", scope="space", scope_owner=ws_a, workspace_id=ws_a, team_id=live.team_id)
    chunk.metadata.file_name = "deleted_space.xlsx"
    await store.add_chunks(dto, [chunk])
    await files.soft_delete(file_id)

    resp = client.post("/api/v1/kb/search", json={"query": "福州", "top_k": 10}, headers=headers)

    assert "deleted_space" not in _names(resp)
    assert "a_space" in _names(resp)
