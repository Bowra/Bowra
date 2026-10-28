"""kb-optimization 可信度纵深 — retrieval-layer parse-failure placeholder filter.

The loaders write a stub chunk when a file fails to parse (scanned PDF with no
vision L2, corrupt file, …). Those stubs are ingest artifacts, never real content.
This filter (ALWAYS-ON, not α-gated) drops them at retrieval so a
"(PDF parse failed…" chunk is never returned to the LLM and cited as content.

Surfaced by the KB retrieval eval MVP-0 (62% of a local dogfood corpus was
placeholder chunks). See docs/sprints/kb-optimization/eval/mvp0-findings.md.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.kb.embedder import EMBED_DIM
from bowhead_core.kb.permission import ScopeContext
from bowhead_core.kb.retriever import KbRetriever, _is_parse_failure_placeholder


def test_predicate_matches_all_loader_variants() -> None:
    assert _is_parse_failure_placeholder(
        "(PDF parse failed — 扫描件或损坏文件, 配置 ALIYUN_DASHSCOPE_API_KEY 启用 vision LLM L2, 或上传 markdown / docx 替代)"
    )
    assert _is_parse_failure_placeholder("(图片解析失败: vision_client_unconfigured)")
    assert _is_parse_failure_placeholder("(xlsx parse failed: bad zip)")
    assert _is_parse_failure_placeholder("(DOCX parse failed)")
    # Empty-table placeholder (same xlsx/CSV wording).
    assert _is_parse_failure_placeholder("(xlsx empty — 没找到数据 island)")
    # leading whitespace tolerated
    assert _is_parse_failure_placeholder("  \n(PDF parse failed — …)")


def test_predicate_does_not_match_real_content() -> None:
    assert not _is_parse_failure_placeholder("")
    assert not _is_parse_failure_placeholder("2025Q3 营收同比下滑 8%, 净利率退到 9.5%.")
    # a doc that merely mentions the words is not a stub (must be the leading stub)
    assert not _is_parse_failure_placeholder("本节讨论 PDF parse failed 的处理策略。")


def _candidate(chunk_id: str, content: str, file_name: str = "f.pdf") -> dict:
    return {
        "chunk_id": chunk_id,
        "content": content,
        "metadata": {"file_id": "kbf_x", "file_name": file_name, "scope": "workspace", "role": "intro", "page": 1, "section_title": None},
        "distance": 0.1,
    }


@pytest.mark.asyncio
async def test_placeholder_chunk_filtered_from_results() -> None:
    """A parse-failure stub in the candidate pool is dropped; real chunk survives."""
    embedder = MagicMock()
    embedder.embed_batch = AsyncMock(return_value=[[0.1] * EMBED_DIM])
    store = MagicMock()
    store.search = AsyncMock(
        return_value=[
            _candidate(
                "c_stub",
                "(PDF parse failed — 扫描件或损坏文件, 配置 ALIYUN_DASHSCOPE_API_KEY 启用 vision LLM L2, 或上传 markdown / docx 替代)",
                "scanned.pdf",
            ),
            _candidate("c_real", "本公司 2022 年度营业收入 3.2 亿元，同比增长 12%。", "report.pdf"),
        ]
    )
    reranker = MagicMock()
    # rerank ranks the stub FIRST — the filter must still drop it.
    reranker.rerank = AsyncMock(return_value=[(0, 0.95), (1, 0.90)])
    retriever = KbRetriever(embedder, store, reranker)
    ctx = ScopeContext(user_id="u", workspace_id="w", team_id="o")
    result = await retriever.invoke("2022 年营业收入", ctx, top_k=5)
    ids = [c.chunk_id for c in result.chunks]
    assert "c_stub" not in ids, "parse-failure placeholder must not be returned"
    assert "c_real" in ids


@pytest.mark.asyncio
async def test_all_placeholder_pool_returns_empty() -> None:
    """If every candidate is a stub, retrieval returns nothing (not stubs)."""
    embedder = MagicMock()
    embedder.embed_batch = AsyncMock(return_value=[[0.1] * EMBED_DIM])
    store = MagicMock()
    store.search = AsyncMock(
        return_value=[
            _candidate("c1", "(PDF parse failed — …)", "a.pdf"),
            _candidate("c2", "(图片解析失败: x)", "b.png"),
        ]
    )
    reranker = MagicMock()
    reranker.rerank = AsyncMock(return_value=[(0, 0.9), (1, 0.8)])
    retriever = KbRetriever(embedder, store, reranker)
    ctx = ScopeContext(user_id="u", workspace_id="w", team_id="o")
    result = await retriever.invoke("查询", ctx, top_k=5)
    assert result.chunks == []
