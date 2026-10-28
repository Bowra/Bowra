"""KB document module unit tests.

Covers:
- P0-A: chunk_id idempotent across re-parse (same file_id + chunk_index + content → same id)
- P1-O8: chunk_id is 24 hex char (96 bit)
- ChunkMetadata.to_dict serialization (chromadb 不接受 None)
"""

from __future__ import annotations

from bowhead_core.kb.document import (
    ChunkMetadata,
    KbChunk,
    KbDocument,
    make_chunk_id,
)

# ─── chunk_id ─────────────────────────────────────────


def test_chunk_id_idempotent_across_reparse() -> None:
    """P0-A: 重 parse 时相同 (file_id, chunk_index, content) → 相同 chunk_id.

    chromadb upsert 同 id 不重复, 旧 chunks 自动覆盖.
    """
    file_id = "kbf_test_001"
    content = "GMV (Gross Merchandise Volume) 是商品交易总额, 不扣退款"
    chunk_index = 5

    id1 = make_chunk_id(file_id, chunk_index, content)
    id2 = make_chunk_id(file_id, chunk_index, content)

    assert id1 == id2


def test_chunk_id_changes_when_content_changes() -> None:
    """content 变 (e.g. 用户改文档重 parse) → id 变, version+1 语义自然 work."""
    file_id = "kbf_test_001"
    chunk_index = 5

    id_v1 = make_chunk_id(file_id, chunk_index, "old content version")
    id_v2 = make_chunk_id(file_id, chunk_index, "new content version")

    assert id_v1 != id_v2


def test_chunk_id_changes_when_chunk_index_changes() -> None:
    """同 file 不同 chunk index → 不同 id (file 内 chunks 间 unique)."""
    file_id = "kbf_test_001"
    content = "same content"

    id1 = make_chunk_id(file_id, 1, content)
    id2 = make_chunk_id(file_id, 2, content)

    assert id1 != id2


def test_chunk_id_changes_when_file_id_changes() -> None:
    """跨 file 不同 file_id → 不同 id (不同 file 不冲突)."""
    content = "same content"
    chunk_index = 1

    id_a = make_chunk_id("kbf_a", chunk_index, content)
    id_b = make_chunk_id("kbf_b", chunk_index, content)

    assert id_a != id_b


def test_chunk_id_format_24_hex_chars() -> None:
    """P1-O8: chunk_id 是 24 hex char (96 bit), prefix 'chunk_'."""
    cid = make_chunk_id("kbf_x", 1, "any content")

    assert cid.startswith("chunk_")
    hex_part = cid[len("chunk_") :]
    assert len(hex_part) == 24
    # all chars are valid hex
    int(hex_part, 16)  # raises ValueError if not hex


def test_chunk_id_only_uses_content_first_200_chars() -> None:
    """make_chunk_id 用 content[:200] 避免长内容 hash 性能问题.

    长文档 content 头 200 char 相同 → 同 id (rare collision risk).
    Tail 改但 head 同 → 同 id (acceptable for V1, content addressable on prefix).
    """
    common_prefix = "X" * 200
    id1 = make_chunk_id("kbf_a", 1, common_prefix + "tail A")
    id2 = make_chunk_id("kbf_a", 1, common_prefix + "tail B")

    # V1 design tradeoff: same first 200 chars → same ID.
    assert id1 == id2


# ─── KbDocument ───────────────────────────────────────


def test_kb_document_construction() -> None:
    doc = KbDocument(page_content="hello", metadata={"page": 1})
    assert doc.page_content == "hello"
    assert doc.metadata["page"] == 1
    assert doc.id is None


def test_kb_document_default_metadata_empty_dict() -> None:
    """Default metadata is empty dict, not None (Python dataclass mutable default trap)."""
    doc1 = KbDocument(page_content="a")
    doc2 = KbDocument(page_content="b")
    doc1.metadata["key"] = "val"

    # Default factory must not share mutable defaults.
    assert "key" not in doc2.metadata


# ─── ChunkMetadata.to_dict ────────────────────────────


def test_chunk_metadata_to_dict_no_none() -> None:
    """P0-O3: chromadb where filter 不接受 None.
    to_dict 必须把 None 转 sentinel (page=-1 / section_title="" / char_offset=-1).
    """
    meta = ChunkMetadata(
        file_id="kbf_x",
        file_name="test.pdf",
        scope="personal",
        scope_owner="user_a",
        workspace_id="ws_42",
        team_id="team_5",
        chunk_index=1,
        page=None,  # → -1
        section_title=None,  # → ""
        char_offset=None,  # → -1
    )
    d = meta.to_dict()
    assert d["page"] == -1
    assert d["section_title"] == ""
    assert d["char_offset"] == -1
    # All dict values chromadb-compatible str/int/float/bool, no None.
    for k, v in d.items():
        assert v is not None, f"{k} 不该是 None (chromadb where filter 不接受 None)"
        assert isinstance(v, (str, int, float, bool)), f"{k}={v!r} 不是 chromadb 接受类型"


def test_chunk_metadata_active_default_true() -> None:
    """P0-O3: is_active 默 True, deleted_at_iso 默 ''."""
    meta = ChunkMetadata(
        file_id="kbf_x",
        file_name="t",
        scope="workspace",
        scope_owner="ws_42",
        workspace_id="ws_42",
        team_id="team_1",
        chunk_index=1,
    )
    assert meta.is_active is True
    assert meta.deleted_at_iso == ""

    d = meta.to_dict()
    assert d["is_active"] is True
    assert d["deleted_at_iso"] == ""


def test_chunk_metadata_soft_deleted_serializes() -> None:
    """soft delete 后 metadata: is_active=False, deleted_at_iso 含 ISO timestamp."""
    meta = ChunkMetadata(
        file_id="kbf_x",
        file_name="t",
        scope="workspace",
        scope_owner="ws_42",
        workspace_id="ws_42",
        team_id="team_1",
        chunk_index=1,
        is_active=False,
        deleted_at_iso="2026-05-04T10:00:00+00:00",
    )
    d = meta.to_dict()
    assert d["is_active"] is False
    assert d["deleted_at_iso"].startswith("2026-")


# ─── KbChunk ──────────────────────────────────────────


def test_kb_chunk_holds_embedding() -> None:
    meta = ChunkMetadata(
        file_id="f",
        file_name="t",
        scope="personal",
        scope_owner="u",
        workspace_id="w",
        team_id="o",
        chunk_index=1,
    )
    chunk = KbChunk(
        chunk_id="chunk_abc",
        file_id="f",
        content="hello",
        embedding=[0.1] * 1536,
        metadata=meta,
    )
    assert chunk.chunk_id == "chunk_abc"
    assert len(chunk.embedding) == 1536
