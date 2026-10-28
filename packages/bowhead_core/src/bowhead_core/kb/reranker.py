"""Async text-rerank client — OpenAI-compatible (httpx2).

Sprint kb-model-config-2026-05-26: replaces the DashScope SDK direct
binding (`dashscope.TextReRank.call`) with a vendor-agnostic httpx2 client
driven by `ModelSpec` (from `kb.config_loader`).

Two body shapes supported per `ModelSpec.capability`:
- ``rerank``: OpenAI-style flat body `{model, query, documents, top_n}`
  Used by qwen3-rerank, gte-rerank-v2 (via DashScope OpenAI-compat path).
- ``rerank-multimodal`` (future): DashScope-style nested
  `{model, input: {query, documents}, top_n}` for qwen3-vl-rerank.

Response shape (both): `{results: [{index, relevance_score}, ...]}`.

Key behaviors:
- P1-R3 (round 6): 5s timeout + fallback cosine (linearly decreasing)
- non-200 / network / timeout → fallback cosine (graceful degrade —
  retrieval still returns vector-rank ordered slice)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx2

from bowhead_core.kb.telemetry import emit_audit
from bowhead_core.ssrf_guard import build_guarded_async_client

if TYPE_CHECKING:
    from bowhead_core.kb.config_loader import ModelSpec

logger = logging.getLogger(__name__)


# Back-compat: tests import this constant. Production code reads
# `reranker.model_id` to track yaml-driven active model.
RERANK_MODEL = "qwen/qwen3-rerank"
# P1-R3 round 6: 5s timeout
RERANK_TIMEOUT_S = 5.0
# Character cap per rerank document. Normal chunk ~800 tokens (<3000 Chinese characters), far below this;
# old splitting stored long headingless .txt/.md files as one chunk; DashScope rejects the entire
# request with 400 ("should not be larger than 300000") for oversized documents, reverting the entire query to vector order
# and disabling the relevance floor. Truncation affects only these long chunks; model reads only first few thousand tokens anyway.
RERANK_DOC_MAX_CHARS = 8000


class FallbackRerankPairs(list[tuple[int, float]]):
    """Marker type for ``Reranker._fallback_cosine`` output.

    The scores inside are synthetic rank positions (1.0, 0.99, ...), NOT
    cross-encoder relevance. Consumers that interpret score as relevance
    (e.g. the page search relevance floor) must detect this type and skip.
    """


def _parse_rerank_pairs(data: object, doc_count: int) -> list[tuple[int, float]]:
    """``[(index, relevance_score)]`` from a rerank response; malformed rows are skipped."""
    if not isinstance(data, dict):
        return []
    output = data.get("output")
    results = data.get("results") or (output.get("results") if isinstance(output, dict) else None)
    if not isinstance(results, list):
        return []
    pairs: list[tuple[int, float]] = []
    for r in results:
        if not isinstance(r, dict) or "index" not in r or "relevance_score" not in r:
            continue
        try:
            idx, score = int(r["index"]), float(r["relevance_score"])
        except (TypeError, ValueError):
            continue
        if 0 <= idx < doc_count:
            pairs.append((idx, score))
    return pairs


class Reranker:
    """OpenAI-compatible text rerank cross-encoder.

    Construct from `ModelSpec` carrying base_url + api_key + real_id +
    capability. Same `rerank` API as before.
    """

    def __init__(self, spec: ModelSpec):
        if spec.capability not in ("rerank", "rerank-multimodal"):  # type: ignore[comparison-overlap]
            raise ValueError(
                f"Reranker requires capability rerank or rerank-multimodal, got {spec.capability!r}",
            )
        self._spec = spec
        self._base_url = spec.base_url.rstrip("/")
        self._timeout_sec = float(spec.timeout_sec or RERANK_TIMEOUT_S)
        self._ssrf_guard = spec.ssrf_guard

    @property
    def model_id(self) -> str:
        return self._spec.real_id

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 5,
    ) -> list[tuple[int, float]]:
        """Cross-encoder rerank. Returns [(original_index, relevance_score)] sorted desc.

        [P1-R3] timeout + fallback cosine (linearly decreasing score)
        如果 top_k > len(documents), 返 min(top_k, len(documents)) 条.
        """
        if not documents:
            return []

        actual_top_k = min(top_k, len(documents))
        documents = [d[:RERANK_DOC_MAX_CHARS] for d in documents]
        payload = self._build_payload(query, documents, actual_top_k)
        headers = {
            "Authorization": f"Bearer {self._spec.api_key}",
            "Content-Type": "application/json",
        }
        # 2026-06-05 de-edu: official DashScope has no OpenAI-style /rerank—native
        # protocol uses full services/rerank path (base_url must be .../api/v1).
        if getattr(self._spec, "protocol", "openai_compat") == "dashscope_native":
            url = f"{self._base_url}/services/rerank/text-rerank/text-rerank"
        else:
            url = f"{self._base_url}/rerank"
        try:
            async with self._client() as client:
                resp = await client.post(
                    url,
                    json=payload,
                    headers=headers,
                )
        except (TimeoutError, httpx2.TimeoutException):
            logger.warning("rerank timeout %ss, fallback cosine", self._timeout_sec)
            emit_audit(
                "kb.rerank_timeout",
                {
                    "query_len": len(query),
                    "doc_count": len(documents),
                    "timeout_s": self._timeout_sec,
                },
            )
            return self._fallback_cosine(actual_top_k)
        except httpx2.HTTPError as exc:
            logger.warning("rerank network error, fallback cosine: %s", exc)
            return self._fallback_cosine(actual_top_k)
        except Exception as exc:
            logger.warning("rerank exception, fallback cosine: %s", exc)
            return self._fallback_cosine(actual_top_k)

        if resp.status_code != 200:
            try:
                err = resp.json()
                msg = (err.get("error") or {}).get("message") or err.get("message") or resp.text[:200]
            except Exception:
                msg = resp.text[:200]
            logger.warning("rerank failed [%s]: %s, fallback cosine", resp.status_code, msg)
            return self._fallback_cosine(actual_top_k)

        try:
            data = resp.json()
        except Exception as exc:
            logger.warning("rerank bad json, fallback cosine: %s", exc)
            return self._fallback_cosine(actual_top_k)

        # Result shape: flat `{results: [...]}` (qwen3-rerank, qwen3-vl-rerank
        # wraps results inside `output:` block per DashScope convention).
        pairs = _parse_rerank_pairs(data, len(documents))
        if not pairs:
            # 200 without usable results (missing results/relevance_score or out-of-bounds index): treat as rerank
            # failure, fall back to vector order. Returning [] makes retrieval return 0 with rerank_used=True,
            # causing chat to say "知识库里没有".
            logger.warning("rerank 200 with no usable results (doc_count=%d), fallback cosine", len(documents))
            emit_audit("kb.rerank_malformed_response", {"query_len": len(query), "doc_count": len(documents)})
            return self._fallback_cosine(actual_top_k)
        return pairs

    def _client(self) -> httpx2.AsyncClient:
        if not self._ssrf_guard:
            return httpx2.AsyncClient(timeout=self._timeout_sec)
        return build_guarded_async_client(
            timeout=self._timeout_sec,
        )

    def _build_payload(self, query: str, documents: list[str], top_n: int) -> dict:
        """Per-capability / per-protocol request body shape."""
        if getattr(self._spec, "protocol", "openai_compat") == "dashscope_native":
            # Native DashScope (verified 2026-06-05: gte-rerank-v2 200; see sprint spike):
            # input wrapper + parameters.top_n; response output.results (parser below already compatible).
            return {
                "model": self._spec.real_id,
                "input": {"query": query, "documents": documents},
                "parameters": {"top_n": top_n, "return_documents": False},
            }
        if self._spec.capability == "rerank-multimodal":
            # DashScope-style: input wrapper (qwen3-vl-rerank etc.)
            return {
                "model": self._spec.real_id,
                "input": {"query": query, "documents": documents},
                "top_n": top_n,
            }
        # default: OpenAI-style flat
        return {
            "model": self._spec.real_id,
            "query": query,
            "documents": documents,
            "top_n": top_n,
        }

    @staticmethod
    def _fallback_cosine(top_k: int) -> list[tuple[int, float]]:
        """Linearly decreasing fallback (V1 接受质量降级). Scores are rank positions, not relevance."""
        return FallbackRerankPairs((i, max(0.0, 1.0 - i * 0.01)) for i in range(top_k))
