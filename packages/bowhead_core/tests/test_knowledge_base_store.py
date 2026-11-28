"""Knowledge base read rule, chunk key and index identity (phase 1 slice 1: only default knowledge bases)."""

from __future__ import annotations

import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.kb.document import ChunkMetadata, KbChunk, KbDocument, make_chunk_id
from bowhead_core.kb.knowledge_base_store import (
    KnowledgeBaseHomeRow,
    default_for_scope,
    effective_knowledge_base_id,
    knowledge_base_key,
)
from bowhead_core.kb.schemas import KbFileDTO
from bowhead_core.kb.store import KbStore
from bowhead_core.kb.worker import KbWorker, _index_identity

SPACE_DEFAULT = default_for_scope("space", "ws_1", "team_a")
NAMED = "kbase_0123456789abcdef01234567"


def _file(**overrides) -> KbFileDTO:
    values = {
        "file_id": "kbf_1",
        "artifact_id": "art_1",
        "filename": "a.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 1,
        "scope": "space",
        "scope_id": "ws_1",
        "workspace_id": "ws_1",
        "team_id": "team_a",
        "uploaded_by": "u_1",
        "uploaded_at": "2026-11-27T00:00:00+00:00",
    }
    return KbFileDTO(**{**values, **overrides})


@pytest.mark.unit
def test_read_rule_maps_null_unknown_and_mismatched_values_to_the_scope_default():
    named_here = KnowledgeBaseHomeRow(NAMED, "team_a", "space", "ws_1")
    assert effective_knowledge_base_id(_file()) == SPACE_DEFAULT
    assert effective_knowledge_base_id(_file(knowledge_base_id=SPACE_DEFAULT)) == SPACE_DEFAULT
    assert effective_knowledge_base_id(_file(knowledge_base_id=NAMED), named_here) == NAMED
    # Unknown ID, another Space, another Team, or a row for a different ID: the default of the current scope.
    assert effective_knowledge_base_id(_file(knowledge_base_id=NAMED)) == SPACE_DEFAULT
    assert effective_knowledge_base_id(_file(knowledge_base_id=NAMED), KnowledgeBaseHomeRow(NAMED, "team_a", "space", "ws_2")) == SPACE_DEFAULT
    assert effective_knowledge_base_id(_file(knowledge_base_id=NAMED), KnowledgeBaseHomeRow(NAMED, "team_b", "space", "ws_1")) == SPACE_DEFAULT
    assert (
        effective_knowledge_base_id(_file(knowledge_base_id=NAMED), KnowledgeBaseHomeRow("kbase_other", "team_a", "space", "ws_1")) == SPACE_DEFAULT
    )
    # A file moved to My knowledge keeps an ID from its old home until a writer rewrites it.
    moved = _file(scope="personal", scope_id="u_1", knowledge_base_id=SPACE_DEFAULT)
    assert effective_knowledge_base_id(moved, KnowledgeBaseHomeRow(SPACE_DEFAULT, "team_a", "space", "ws_1")) == default_for_scope(
        "personal", "u_1", "team_a"
    )
    # The legacy scope spelling shares the Space default.
    assert effective_knowledge_base_id(_file(scope="workspace")) == SPACE_DEFAULT


@pytest.mark.unit
def test_chunk_key_and_index_identity_include_the_knowledge_base():
    assert knowledge_base_key(_file()) == SPACE_DEFAULT
    assert knowledge_base_key(_file(knowledge_base_id=NAMED)) == NAMED
    assert knowledge_base_key(MagicMock(spec=["scope", "scope_id", "team_id"], scope="org", scope_id="x", team_id="t")) == ""
    # NULL and the stored default are the same identity; another knowledge base is not.
    assert _index_identity(_file()) == _index_identity(_file(knowledge_base_id=SPACE_DEFAULT))
    assert _index_identity(_file()) != _index_identity(_file(knowledge_base_id=NAMED))
    assert _index_identity(_file())[:4] == ("space", "ws_1", "ws_1", "team_a")
    base = {"file_id": "kbf_1", "file_name": "a", "scope": "space", "scope_owner": "ws_1", "workspace_id": "ws_1", "team_id": "t", "chunk_index": 1}
    # An unknown key is left out, like chunks written before the key existed.
    assert "knowledge_base_id" not in ChunkMetadata(**base).to_dict()
    assert ChunkMetadata(**base, knowledge_base_id=SPACE_DEFAULT).to_dict()["knowledge_base_id"] == SPACE_DEFAULT


@pytest.mark.unit
async def test_worker_chunks_carry_the_default_without_a_read_and_check_a_stored_named_id():
    files = MagicMock()
    files.effective_knowledge_base_ids = AsyncMock(return_value={"kbf_1": SPACE_DEFAULT})
    worker = KbWorker(files_store=files, store=MagicMock(), embedder=MagicMock())
    docs = [KbDocument(page_content="alpha", metadata={}), KbDocument(page_content="beta", metadata={})]
    chunks = worker._build_kb_chunks(_file(), docs, [[0.1], [0.2]], await worker._chunk_knowledge_base_id(_file()))
    assert {chunk.metadata.to_dict()["knowledge_base_id"] for chunk in chunks} == {SPACE_DEFAULT}
    files.effective_knowledge_base_ids.assert_not_awaited()
    # A stored named ID goes through the read rule (here: unknown, so the default).
    assert await worker._chunk_knowledge_base_id(_file(knowledge_base_id=NAMED)) == SPACE_DEFAULT
    files.effective_knowledge_base_ids.assert_awaited_once()


@pytest.fixture
def kb_store():
    directory = tempfile.mkdtemp(prefix="kb_test_knowledge_base_")
    try:
        yield KbStore(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _chunk(file: KbFileDTO, index: int, **metadata) -> KbChunk:
    return KbChunk(
        chunk_id=make_chunk_id(file.file_id, index, f"text {index}"),
        file_id=file.file_id,
        content=f"text {index}",
        embedding=[0.1 * index, 0.2, 0.3],
        metadata=ChunkMetadata(
            file_id=file.file_id,
            file_name=file.filename,
            scope=file.scope,  # type: ignore[arg-type]
            scope_owner=file.scope_id,
            workspace_id=file.workspace_id,
            team_id=file.team_id,
            chunk_index=index,
            **metadata,
        ),
    )


@pytest.mark.integration
async def test_scope_change_moves_chunks_to_the_target_default(kb_store):
    file = _file()
    await kb_store.add_chunks(file, [_chunk(file, 1, knowledge_base_id=SPACE_DEFAULT), _chunk(file, 2)])
    # In place (same Space collection) to My knowledge.
    await kb_store.update_chunks_scope("kbf_1", "space", "ws_1", "ws_1", "team_a", "personal", "u_1", "ws_1", "team_a")
    mine = _file(scope="personal", scope_id="u_1")
    metas = (await kb_store._file_collection(mine)).get(where={"file_id": "kbf_1"}, include=["metadatas"])["metadatas"]
    assert {meta["knowledge_base_id"] for meta in metas} == {default_for_scope("personal", "u_1", "team_a")}
    # Across collections to the Team.
    await kb_store.update_chunks_scope("kbf_1", "personal", "u_1", "ws_1", "team_a", "team", "team_a", "", "team_a")
    team = _file(scope="team", scope_id="team_a", workspace_id="")
    metas = (await kb_store._file_collection(team)).get(where={"file_id": "kbf_1"}, include=["metadatas", "documents"])
    assert {meta["knowledge_base_id"] for meta in metas["metadatas"]} == {default_for_scope("team", "team_a", "team_a")}
    assert sorted(metas["documents"]) == ["text 1", "text 2"]


@pytest.mark.integration
async def test_setting_the_chunk_key_touches_only_stale_chunks_and_keeps_the_rest(kb_store):
    file = _file()
    await kb_store.add_chunks(file, [_chunk(file, 1, knowledge_base_id=SPACE_DEFAULT), _chunk(file, 2), _chunk(file, 3, knowledge_base_id=NAMED)])
    assert await kb_store.set_chunks_knowledge_base(file, SPACE_DEFAULT) == 2
    assert await kb_store.set_chunks_knowledge_base(file, SPACE_DEFAULT) == 0
    got = (await kb_store._file_collection(file)).get(where={"file_id": "kbf_1"}, include=["metadatas", "documents", "embeddings"])
    assert {meta["knowledge_base_id"] for meta in got["metadatas"]} == {SPACE_DEFAULT}
    assert {meta["scope_owner"] for meta in got["metadatas"]} == {"ws_1"}
    assert sorted(got["documents"]) == ["text 1", "text 2", "text 3"]
    assert len(got["embeddings"]) == 3
    # No collection is created for a file that has never been indexed.
    assert await kb_store.set_chunks_knowledge_base(_file(file_id="kbf_2", workspace_id="ws_9", scope_id="ws_9"), SPACE_DEFAULT) == 0
    assert "kb_ws_ws_9" not in {getattr(item, "name", item) for item in kb_store._client.list_collections()}
