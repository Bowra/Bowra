"""KbWorker 写分块时的文件身份：解析期间删除/改范围、强制重解析替换旧分块、失败不留可检索分块。

真 chromadb（临时目录）+ 真 kb_files 库；嵌入器用固定向量替身，在「嵌入」这一步模拟并发操作。
"""

from __future__ import annotations

import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.kb.document import ChunkMetadata, KbChunk, make_chunk_id
from bowhead_core.kb.embedder import EMBED_DIM
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.permission import ScopeContext
from bowhead_core.kb.store import KbStore
from bowhead_core.kb.worker import KbWorker

_VEC = [0.1] * EMBED_DIM


@pytest.fixture
async def files_store(initialize_migrated_kb_files_database, tmp_path):
    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    s = KbFilesStore(tmp_path / "kb_files.db")
    await s.initialize()
    return s


@pytest.fixture
def kb_store():
    persist_dir = tempfile.mkdtemp(prefix="kb_test_worker_identity_")
    try:
        yield KbStore(persist_dir)
    finally:
        shutil.rmtree(persist_dir, ignore_errors=True)


async def _space_file(files_store: KbFilesStore, tmp_path) -> str:
    md = tmp_path / "doc.md"
    md.write_text("# 标题\n\n机密内容：季度预算三百万。\n", encoding="utf-8")
    return await files_store.create(
        artifact_id=str(md),
        filename="doc.md",
        mime_type="text/markdown",
        size_bytes=md.stat().st_size,
        scope="space",
        scope_id="ws_42",
        workspace_id="ws_42",
        team_id="team_5",
        uploaded_by="user_a",
    )


def _embedder(on_embed=None) -> MagicMock:
    async def _embed(texts):
        if on_embed is not None:
            await on_embed()
        return [_VEC for _ in texts]

    emb = MagicMock()
    emb.embed_batch = AsyncMock(side_effect=_embed)
    return emb


async def _stored(kb_store: KbStore, files_store: KbFilesStore, file_id: str) -> dict:
    file = await files_store.get(file_id)
    assert file is not None
    return kb_store._coll_for_file(file).get(where={"file_id": file_id}, include=["metadatas"])


async def _search(kb_store: KbStore, user_id: str) -> list[dict]:
    return await kb_store.search(ScopeContext(user_id=user_id, workspace_id="ws_42", team_id="team_5"), _VEC)


@pytest.mark.asyncio
async def test_file_deleted_during_parse_is_not_indexed(files_store, kb_store, tmp_path) -> None:
    file_id = await _space_file(files_store, tmp_path)

    async def _delete() -> None:
        await files_store.soft_delete(file_id)

    worker = KbWorker(files_store=files_store, store=kb_store, embedder=_embedder(_delete))
    await worker.parse_and_index(file_id)

    assert (await _stored(kb_store, files_store, file_id))["ids"] == []
    assert await _search(kb_store, "user_b") == []
    row = await files_store.get(file_id)
    assert row is not None and row.parse_status == "failed"
    assert "deleted" in (row.parse_error or "")


@pytest.mark.asyncio
async def test_space_to_personal_during_parse_writes_personal_chunks(files_store, kb_store, tmp_path) -> None:
    file_id = await _space_file(files_store, tmp_path)

    async def _make_personal() -> None:
        await files_store.update_scope(file_id, "personal", "user_a", workspace_id="ws_42", team_id="team_5")

    worker = KbWorker(files_store=files_store, store=kb_store, embedder=_embedder(_make_personal))
    await worker.parse_and_index(file_id)

    row = await files_store.get(file_id)
    assert row is not None and row.parse_status == "done"
    metas = (await _stored(kb_store, files_store, file_id))["metadatas"]
    assert metas and all(m["scope"] == "personal" and m["scope_owner"] == "user_a" for m in metas)
    # Other space members cannot retrieve; owner still can.
    assert await _search(kb_store, "user_b") == []
    assert {r["metadata"]["file_id"] for r in await _search(kb_store, "user_a")} == {file_id}


@pytest.mark.asyncio
async def test_file_deleted_in_write_window_chunks_are_removed(files_store, kb_store, tmp_path) -> None:
    file_id = await _space_file(files_store, tmp_path)
    real_add = kb_store.add_chunks

    async def _add_then_delete(file, chunks) -> None:
        await real_add(file, chunks)
        await files_store.soft_delete(file_id)

    kb_store.add_chunks = _add_then_delete  # type: ignore[method-assign]
    worker = KbWorker(files_store=files_store, store=kb_store, embedder=_embedder())
    await worker.parse_and_index(file_id)

    assert (await _stored(kb_store, files_store, file_id))["ids"] == []
    assert await _search(kb_store, "user_b") == []


@pytest.mark.asyncio
async def test_forced_reparse_replaces_stale_chunks(files_store, kb_store, tmp_path) -> None:
    file_id = await _space_file(files_store, tmp_path)
    file = await files_store.get(file_id)
    assert file is not None
    stale = KbChunk(
        chunk_id=make_chunk_id(file_id, 1, "旧解析的内容"),
        file_id=file_id,
        content="旧解析的内容",
        embedding=_VEC,
        metadata=ChunkMetadata(
            file_id=file_id,
            file_name="doc.md",
            scope="space",
            scope_owner="ws_42",
            workspace_id="ws_42",
            team_id="team_5",
            chunk_index=1,
        ),
    )
    await kb_store.add_chunks(file, [stale])
    await files_store.update(file_id, parse_status="done", chunks_count=1)

    worker = KbWorker(files_store=files_store, store=kb_store, embedder=_embedder())
    await worker.parse_and_index(file_id, allow_terminal=True)

    row = await files_store.get(file_id)
    assert row is not None and row.parse_status == "done"
    stored = await _stored(kb_store, files_store, file_id)
    assert stale.chunk_id not in stored["ids"]
    assert len(stored["ids"]) == row.chunks_count


@pytest.mark.asyncio
async def test_count_mismatch_failure_leaves_no_searchable_chunks(files_store, kb_store, tmp_path) -> None:
    file_id = await _space_file(files_store, tmp_path)
    kb_store.count_chunks = AsyncMock(return_value=99)  # type: ignore[method-assign]

    worker = KbWorker(files_store=files_store, store=kb_store, embedder=_embedder())
    await worker.parse_and_index(file_id)

    row = await files_store.get(file_id)
    assert row is not None and row.parse_status == "failed"
    assert (await _stored(kb_store, files_store, file_id))["ids"] == []
    assert await _search(kb_store, "user_a") == []
