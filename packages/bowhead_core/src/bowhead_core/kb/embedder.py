"""Async text-embedding client — OpenAI-compatible (httpx2).

Sprint kb-model-config-2026-05-26: replaces the DashScope SDK direct
binding with a vendor-agnostic httpx2 client driven by `ModelSpec` (from
`kb.config_loader`). Same retry / split / truncate semantics, but base
URL + model + api_key + dim now flow from yaml. Supports DashScope
(`https://dashscope.aliyuncs.com/compatible-mode/v1`), Aliyun Model
Router (`https://model-router.example.com/v1`), or any other
OpenAI-style `POST /embeddings` endpoint.

Key behaviors:
- P1-L: tenacity retry 3x exponential backoff (5xx / 429 / network / timeout)
- P1-M: text > 8000 token 截 + warn (chunk content 全文存 chromadb, 仅 embedding 用截)
- P1-O7: 400/422 (non-retryable) → split batch 找单坏 chunk + skip (零 embedding 不阻塞 worker)
- batch limit 25 (DashScope text-embedding-v3 cap; Model Router v4 lower limit may apply — caller can pre-split)
- α-1: class-level semaphore 限并发 batch (BOWHEAD_EMBED_CONCURRENCY, default 4) 防 429
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

import httpx2
from bowhead_router.transport import (
    build_provider_transport_plan,
    materialize_transport_headers,
    render_custom_http_body,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bowhead_core.kb.errors import EmbedError
from bowhead_core.kb.splitter.paragraph_aware import count_tokens, truncate_to_tokens
from bowhead_core.kb.telemetry import emit_audit
from bowhead_core.ssrf_guard import build_guarded_async_client

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bowhead_core.kb.config_loader import ModelSpec

logger = logging.getLogger(__name__)


# DashScope text-embedding-v3 had 25-item batch cap. Other models may
# tolerate larger batches, but we keep 25 as a safe default. Override
# via env if a vendor confirms higher cap is safe.
EMBED_MAX_INPUT_TOKENS = 8000  # P1-M chunk truncation threshold (below 8192 limit)
EMBED_BATCH_SIZE = int(os.environ.get("BOWHEAD_KB_EMBED_BATCH_SIZE", "25"))

# Back-compat: tests still import EMBED_DIM for fake vector sizing. New
# default = 1024 (qwen text-embedding-v4); old v3 was 1536. Runtime dim
# comes from `Embedder(spec).dim` driven by yaml `kb.models.<name>.dim`.
# This constant is only a TEST scaffold — production code must read
# `embedder.dim` so it tracks the active model.
EMBED_DIM = 1024
_DEFAULT_CONCURRENCY = 4
_DEFAULT_TIMEOUT_SEC = 60


def _read_concurrency_from_env() -> int:
    raw = os.environ.get("BOWHEAD_EMBED_CONCURRENCY", "")
    if not raw:
        return _DEFAULT_CONCURRENCY
    try:
        n = int(raw)
        return max(1, min(n, 32))  # clamp [1, 32]
    except ValueError:
        return _DEFAULT_CONCURRENCY


class _NonRetryableError(Exception):
    """400/422 等 non-retryable, 直接 raise (caller P1-O7 处理 split)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"[{status_code}] {message}")
        self.status_code = status_code


class Embedder:
    """OpenAI-compatible text embedding, vendor-agnostic.

    Construct from `ModelSpec` (read from yaml catalog) — ModelSpec carries
    base_url + api_key + real_id + dim. Same `embed_batch` API as before.

    α-1: class-level semaphore 限并发 batch (BOWHEAD_EMBED_CONCURRENCY, default 4)
    防 429. 多 ingest worker 共享同 process semaphore.
    """

    # α-1: lazy semaphore init (asyncio.Semaphore ctor safe within event loop).
    _sem: asyncio.Semaphore | None = None

    def __init__(self, spec: ModelSpec):
        if spec.capability != "embed":
            raise ValueError(f"Embedder requires capability=embed, got {spec.capability!r}")
        if not spec.dim or spec.dim <= 0:
            raise ValueError(f"Embedder requires positive dim in ModelSpec, got {spec.dim!r}")
        self._spec = spec
        self._base_url = spec.base_url.rstrip("/")
        self._timeout_sec = spec.timeout_sec or _DEFAULT_TIMEOUT_SEC
        self._ssrf_guard = spec.ssrf_guard

    @property
    def dim(self) -> int:
        """Vector dimension — used by chromadb collection creation."""
        return self._spec.dim  # type: ignore[return-value]  # validated non-None in ctor

    @property
    def model_id(self) -> str:
        return self._spec.real_id

    @classmethod
    def _get_semaphore(cls) -> asyncio.Semaphore:
        if cls._sem is None:
            cls._sem = asyncio.Semaphore(_read_concurrency_from_env())
        return cls._sem

    @classmethod
    def _reset_semaphore_for_test(cls) -> None:
        """Test-only: drop cached semaphore so next embed_batch reads env again."""
        cls._sem = None

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Batch embed up to EMBED_BATCH_SIZE texts. Returns list of embeddings.

        - P1-M: text > 8000 token 截 + audit
        - P1-L: retry 3x on 5xx / 429
        - P1-O7: 400/422 → recursively split batch, 单坏 chunk skip (zero embedding)
        """
        if not texts:
            return []

        # P1-M: truncate for embedding only; full chunk content stored in chromadb.
        truncated_texts = self._truncate_to_max_tokens(list(texts))

        # α-1: concurrency limit, class-level shared across instances to prevent 429.
        async with self._get_semaphore():
            return await self._embed_with_retry_or_split(truncated_texts)

    async def _embed_with_retry_or_split(
        self,
        truncated_texts: list[str],
    ) -> list[list[float]]:
        """Inner: retry / split / skip 逻辑 (从 embed_batch 抽离, semaphore 不递归)."""
        try:
            return await self._call_with_retry(truncated_texts)
        except _NonRetryableError as exc:
            # P1-O7: 400/422 → split batch to locate bad chunk.
            # α-1: reuse parent semaphore slot (_embed_with_retry_or_split, no reacquire);
            # parent holds 1 slot, sequential binary children share it.
            if len(truncated_texts) > 1:
                mid = len(truncated_texts) // 2
                left = await self._embed_with_retry_or_split(truncated_texts[:mid])
                right = await self._embed_with_retry_or_split(truncated_texts[mid:])
                return left + right
            # Single chunk still fails →skip (zero embedding never retrieved).
            emit_audit(
                "kb.embed_chunk_skipped",
                {
                    "reason": str(exc),
                    "content_preview": truncated_texts[0][:100] if truncated_texts else "",
                },
            )
            return [[0.0] * self.dim]
        except EmbedError as exc:
            emit_audit(
                "kb.embed_failed",
                {
                    "error": str(exc),
                    "batch_size": len(truncated_texts),
                },
            )
            raise

    def _truncate_to_max_tokens(self, texts: list[str]) -> list[str]:
        """[P1-M] text > 8000 token 截 + audit."""
        out: list[str] = []
        for t in texts:
            tok_count = count_tokens(t)
            if tok_count > EMBED_MAX_INPUT_TOKENS:
                emit_audit(
                    "kb.embed_text_truncated",
                    {
                        "original_tokens": tok_count,
                        "max": EMBED_MAX_INPUT_TOKENS,
                    },
                )
                out.append(truncate_to_tokens(t, EMBED_MAX_INPUT_TOKENS))
            else:
                out.append(t)
        return out

    async def _call_with_retry(self, texts: list[str]) -> list[list[float]]:
        """[P1-L] tenacity retry 3x exponential. retry on 5xx / 429 / network / timeout."""

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(min=1, max=8),
            retry=retry_if_exception_type(EmbedError),
            reraise=True,
        )
        async def _call() -> list[list[float]]:
            return await self._call_once(texts)

        return await _call()

    async def _call_once(self, texts: list[str]) -> list[list[float]]:
        """Single API call. 400/422 → _NonRetryableError. 5xx/429/network → EmbedError."""
        provider = {
            **self._spec.provider_options,
            "type": self._spec.provider_type,
            "base_url": self._spec.base_url,
            "api_key": self._spec.api_key,
            "timeout_sec": self._spec.timeout_sec,
        }
        try:
            transport = build_provider_transport_plan(
                provider,
                purpose="runtime_embed",
                model_id=self._spec.real_id,
                capability="embed",
            )
        except ValueError as exc:
            raise _NonRetryableError(400, str(exc)) from exc
        payload: dict = {
            "model": self._spec.real_id,
            "input": texts,
            # encoding_format omitted — both DashScope & Model Router default to float
        }
        if self._spec.provider_type == "custom_http":
            try:
                payload = render_custom_http_body(
                    provider,
                    model_id=self._spec.real_id,
                    capability="embed",
                    variables={
                        "model": self._spec.real_id,
                        "deployment": _deployment_for(provider, self._spec.real_id),
                        "input": texts,
                    },
                )
            except ValueError as exc:
                raise _NonRetryableError(400, str(exc)) from exc
        # Preserve HTTP's case-insensitive lookup semantics even when a test or
        # instrumentation hook inspects the argument before httpx2 normalizes it.
        headers = httpx2.Headers(materialize_transport_headers(transport))
        try:
            async with self._client() as client:
                resp = await client.post(
                    transport.request_url,
                    json=payload,
                    headers=headers,
                )
        except httpx2.TimeoutException as exc:
            raise EmbedError(f"timeout: {exc}") from exc
        except httpx2.HTTPError as exc:
            raise EmbedError(f"network: {exc}") from exc

        status = resp.status_code
        if status == 200:
            data = resp.json()
            items = data.get("data") or []
            embeddings = [item["embedding"] for item in items if "embedding" in item]
            if len(embeddings) != len(texts):
                logger.warning(
                    "embed response count mismatch: requested=%d got=%d",
                    len(texts),
                    len(embeddings),
                )
            return embeddings

        # error path
        try:
            err_body = resp.json()
            err_msg = (err_body.get("error") or {}).get("message") or err_body.get("message") or resp.text[:300]
        except Exception:
            err_msg = resp.text[:300]

        if status in (400, 422):
            raise _NonRetryableError(status, err_msg)
        # 5xx / 429 / others → retryable.
        raise EmbedError(f"embed [{status}] {self._spec.real_id}: {err_msg}")

    def _client(self) -> httpx2.AsyncClient:
        if not self._ssrf_guard:
            return httpx2.AsyncClient(timeout=self._timeout_sec)
        return build_guarded_async_client(
            timeout=self._timeout_sec,
        )


def _deployment_for(provider: dict, model_id: str) -> str:
    for model in provider.get("models") or []:
        if isinstance(model, dict) and str(model.get("id") or "") == model_id:
            return str(model.get("deployment") or model_id)
    return model_id
