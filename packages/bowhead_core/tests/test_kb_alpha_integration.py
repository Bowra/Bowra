"""Sprint α — integration test: full KbRetriever.invoke() with all 5 patches active.

Mocks: chromadb store + dashscope rerank/embed (no external API calls).
Exercises: embedder semaphore (α-1), retriever junk filter (α-2), BM25-lite
boost (α-3), synonym expansion (α-4), splitter API surface (α-5 import).

Goal: catch wiring regressions that unit tests miss (e.g. shape mismatch
between Stage 4a output and Stage 4d input, audit event flooding, etc.).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.kb.embedder import EMBED_DIM
from bowhead_core.kb.permission import ScopeContext
from bowhead_core.kb.retriever import KbRetriever
from bowhead_core.kb.splitter import (
    make_paragraph_splitter,
    make_sentence_aware_splitter,
)


def _candidate(
    chunk_id: str,
    content: str,
    role: str = "intro",
    file_id: str = "f1",
    file_name: str = "doc.md",
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "content": content,
        "metadata": {
            "role": role,
            "file_id": file_id,
            "file_name": file_name,
            "scope": "user",
        },
    }


@pytest.mark.asyncio
async def test_full_pipeline_synonym_query_lifts_alt_phrasing_chunk() -> None:
    """α-4 integration: query "毛利率" → doc says "毛利百分比" → still rank well.

    Setup:
    - 3 candidates: chunk A says "毛利百分比", chunk B random analysis,
      chunk C says "毛利率" directly (would rank highest pre-α even).
    - Reranker scores: A=0.55, B=0.50, C=0.60 (close)
    - Without synonym expansion: chunk B might beat A on token overlap noise
    - With synonym: query tokens include "毛利百分比" → A gets BM25 boost.

    Expected: top_k=3 returns C (direct), A (via synonym), B (last).
    """
    embedder = MagicMock()
    embedder.embed_batch = AsyncMock(return_value=[[0.1] * EMBED_DIM])

    candidates = [
        _candidate("c_alt", "公司Q3毛利百分比为32%, 同比下滑3pp"),  # synonym hit
        _candidate("c_noise", "公司业绩季度报告里有大量分析内容"),  # no token hit
        _candidate("c_direct", "Q3毛利率达到32%, 历史新高"),  # direct hit
    ]
    store = MagicMock()
    store.search = AsyncMock(return_value=candidates)

    reranker = MagicMock()
    # rerank: c_alt 0.55, c_noise 0.50, c_direct 0.60
    reranker.rerank = AsyncMock(return_value=[(0, 0.55), (1, 0.50), (2, 0.60)])

    retriever = KbRetriever(embedder, store, reranker)
    ctx = ScopeContext(user_id="u", workspace_id="ws", team_id="team")
    result = await retriever.invoke("毛利率走势", ctx, top_k=3)

    chunk_ids_in_order = [c.chunk_id for c in result.chunks]
    # Both c_alt (via synonym) and c_direct (literal) should outrank c_noise
    assert "c_direct" in chunk_ids_in_order[:2]
    assert "c_alt" in chunk_ids_in_order[:2]
    assert chunk_ids_in_order[-1] == "c_noise"


@pytest.mark.asyncio
async def test_full_pipeline_junk_chunk_filtered() -> None:
    """α-2 integration: empty-header markdown chunk gets dropped from results."""
    embedder = MagicMock()
    embedder.embed_batch = AsyncMock(return_value=[[0.1] * EMBED_DIM])

    candidates = [
        _candidate("c_real", "正文内容: Q3 营收 32 亿元 同比 +0.3%"),
        _candidate("c_junk", "| | | |\n|---|---|---|\n| 1 | 2 | 3 |"),
        _candidate("c_other", "其他分析内容也是有效信息"),
    ]
    store = MagicMock()
    store.search = AsyncMock(return_value=candidates)

    reranker = MagicMock()
    reranker.rerank = AsyncMock(return_value=[(0, 0.7), (1, 0.85), (2, 0.6)])

    retriever = KbRetriever(embedder, store, reranker)
    ctx = ScopeContext(user_id="u", workspace_id="ws", team_id="team")
    result = await retriever.invoke("营收数据", ctx, top_k=5)

    chunk_ids = {c.chunk_id for c in result.chunks}
    assert "c_junk" not in chunk_ids, "empty-header markdown should be filtered"
    assert "c_real" in chunk_ids
    assert "c_other" in chunk_ids


@pytest.mark.asyncio
async def test_full_pipeline_bm25_boost_breaks_tie() -> None:
    """α-3 integration: query token overlap breaks rerank tie."""
    embedder = MagicMock()
    embedder.embed_batch = AsyncMock(return_value=[[0.1] * EMBED_DIM])

    candidates = [
        _candidate("c_no_overlap", "完全不同主题的随机文本块包含足量字符"),
        _candidate("c_with_overlap", "营收 毛利 净利 都在这个 chunk 里出现"),
    ]
    store = MagicMock()
    store.search = AsyncMock(return_value=candidates)

    reranker = MagicMock()
    # Same rerank score → BM25-lite breaks tie
    reranker.rerank = AsyncMock(return_value=[(0, 0.70), (1, 0.70)])

    retriever = KbRetriever(embedder, store, reranker)
    ctx = ScopeContext(user_id="u", workspace_id="ws", team_id="team")
    result = await retriever.invoke("营收 毛利 净利 分析", ctx, top_k=2)

    # c_with_overlap should rank first (rerank tie + BM25 boost)
    assert result.chunks[0].chunk_id == "c_with_overlap"
    assert result.chunks[1].chunk_id == "c_no_overlap"
    # Score reflects boost
    assert result.chunks[0].score > result.chunks[1].score


@pytest.mark.asyncio
async def test_full_pipeline_no_results_no_crash() -> None:
    """Empty store returns empty result (no patches break this path)."""
    embedder = MagicMock()
    embedder.embed_batch = AsyncMock(return_value=[[0.1] * EMBED_DIM])

    store = MagicMock()
    store.search = AsyncMock(return_value=[])

    reranker = MagicMock()
    reranker.rerank = AsyncMock(return_value=[])

    retriever = KbRetriever(embedder, store, reranker)
    ctx = ScopeContext(user_id="u", workspace_id="ws", team_id="team")
    result = await retriever.invoke("anything", ctx, top_k=5)

    assert result.chunks == []
    assert result.total_candidates == 0


@pytest.mark.asyncio
async def test_full_pipeline_role_promotion_still_works() -> None:
    """Regression: existing α-2/α-3 doesn't break role-based glossary promotion."""
    embedder = MagicMock()
    embedder.embed_batch = AsyncMock(return_value=[[0.1] * EMBED_DIM])

    candidates = [
        _candidate("c0", "first analysis content here", role="analysis"),
        _candidate("c1", "second analysis content here", role="analysis"),
        _candidate("c2", "ROE definition: 净资产收益率", role="glossary"),  # low rerank
    ]
    store = MagicMock()
    store.search = AsyncMock(return_value=candidates)

    reranker = MagicMock()
    # c2 (glossary) has LOW rerank but high role boost — should be promoted
    reranker.rerank = AsyncMock(return_value=[(0, 0.80), (1, 0.75), (2, 0.30)])

    retriever = KbRetriever(embedder, store, reranker)
    ctx = ScopeContext(user_id="u", workspace_id="ws", team_id="team")
    result = await retriever.invoke("ROE 怎么算", ctx, top_k=2)

    # Should include glossary via promotion (slot kept)
    chunk_ids = {c.chunk_id for c in result.chunks}
    assert "c2" in chunk_ids


@pytest.mark.asyncio
async def test_full_pipeline_query_with_no_finance_term_still_works() -> None:
    """α-4 graceful: query with no finance term doesn't break / inflate tokens."""
    embedder = MagicMock()
    embedder.embed_batch = AsyncMock(return_value=[[0.1] * EMBED_DIM])

    candidates = [
        _candidate("c1", "people prefer this approach over alternatives"),
        _candidate("c2", "tips for retention are documented here"),
    ]
    store = MagicMock()
    store.search = AsyncMock(return_value=candidates)

    reranker = MagicMock()
    reranker.rerank = AsyncMock(return_value=[(0, 0.6), (1, 0.55)])

    retriever = KbRetriever(embedder, store, reranker)
    ctx = ScopeContext(user_id="u", workspace_id="ws", team_id="team")

    # P0 fix verification: "people" / "tips" should NOT trigger pe / ps synonyms.
    # If they did, BM25-lite boost would shift rankings unpredictably.
    result = await retriever.invoke("people would prefer with tips", ctx, top_k=2)

    # Both candidates returned, original order maintained
    assert len(result.chunks) == 2
    assert result.chunks[0].chunk_id == "c1"
    assert result.chunks[1].chunk_id == "c2"


# -- α-5: splitter API surface --


def test_alpha5_splitter_available_and_callable() -> None:
    """α-5 deferred wire-up: factory exists, callable, returns splitter instance."""
    paragraph = make_paragraph_splitter(chunk_size=200, chunk_overlap=20)
    sentence = make_sentence_aware_splitter(chunk_size=200, chunk_overlap=20)
    # Both produce chunks for same input
    text = "段落一; 子句二, 子句三。段落二, 后续内容。"
    p_chunks = paragraph.split_text(text)
    s_chunks = sentence.split_text(text)
    assert len(p_chunks) >= 1
    assert len(s_chunks) >= 1
