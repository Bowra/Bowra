"""Reranker dashscope_native 协议分支（de-edu 2026-06-05）。

官方 DashScope compatible-mode 无 /rerank（实测 404）；native 路径
/services/rerank/text-rerank/text-rerank + input/parameters 包装 +
output.results 响应（实测 200，见 sprint spike-image-gen.md 同目录记录）。
"""

from __future__ import annotations

import json

import httpx2
import pytest
import respx
from bowhead_core.kb.config_loader import ModelSpec
from bowhead_core.kb.reranker import RERANK_DOC_MAX_CHARS, FallbackRerankPairs, Reranker

_BASE = "https://dashscope.aliyuncs.com/api/v1"


def _native_spec() -> ModelSpec:
    return ModelSpec(
        name="gte-rerank-v2",
        capability="rerank",
        provider="aliyun_dashscope_native",
        real_id="gte-rerank-v2",
        base_url=_BASE,
        api_key="sk-t",
        timeout_sec=5,
        protocol="dashscope_native",
    )


@respx.mock
async def test_native_url_body_and_parse():
    route = respx.post(f"{_BASE}/services/rerank/text-rerank/text-rerank").mock(
        return_value=httpx2.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": 1, "relevance_score": 0.91},
                        {"index": 0, "relevance_score": 0.12},
                    ]
                },
                "usage": {"total_tokens": 23},
            },
        ),
    )
    out = await Reranker(_native_spec()).rerank("q", ["a", "b"], top_k=2)
    assert out == [(1, 0.91), (0, 0.12)]
    body = json.loads(route.calls[0].request.content)
    assert body["input"] == {"query": "q", "documents": ["a", "b"]}
    assert body["parameters"]["top_n"] == 2
    assert "query" not in body  # Not flat OpenAI shape.


@respx.mock
async def test_native_404_falls_back_cosine():
    respx.post(f"{_BASE}/services/rerank/text-rerank/text-rerank").mock(
        return_value=httpx2.Response(404, json={"message": "no such api"}),
    )
    out = await Reranker(_native_spec()).rerank("q", ["a", "b", "c"], top_k=2)
    assert len(out) == 2  # Cosine fallback does not crash.


@respx.mock
async def test_oversized_document_is_truncated_instead_of_failing_the_whole_query():
    """116 实测：一个 3MB 的旧 chunk 让 DashScope 整次 400，同范围所有查询退回向量顺序。"""
    route = respx.post(f"{_BASE}/services/rerank/text-rerank/text-rerank").mock(
        return_value=httpx2.Response(200, json={"output": {"results": [{"index": 0, "relevance_score": 0.8}]}}),
    )
    huge = "会员复购率" * 100_000

    out = await Reranker(_native_spec()).rerank("q", [huge, "b"], top_k=1)

    assert out == [(0, 0.8)]
    sent = json.loads(route.calls[0].request.content)["input"]["documents"]
    assert len(sent[0]) == RERANK_DOC_MAX_CHARS
    assert sent[1] == "b"


@pytest.mark.parametrize(
    "payload",
    [
        {"output": {}},  # Missing results.
        {"usage": {"total_tokens": 3}},  # Even output absent.
        {"output": {"results": [{"index": 0}, {"index": 1}]}},  # Rows lack relevance_score.
        {"output": {"results": [{"index": 9, "relevance_score": 0.9}]}},  # Index out of bounds.
    ],
)
@respx.mock
async def test_200_without_usable_results_falls_back_instead_of_returning_nothing(payload):
    """200 但没有可用结果：按 rerank 失败退回向量顺序，不能交出空列表让检索整体 0 条。"""
    respx.post(f"{_BASE}/services/rerank/text-rerank/text-rerank").mock(return_value=httpx2.Response(200, json=payload))

    out = await Reranker(_native_spec()).rerank("q", ["a", "b", "c"], top_k=2)

    assert isinstance(out, FallbackRerankPairs)
    assert [idx for idx, _ in out] == [0, 1]
