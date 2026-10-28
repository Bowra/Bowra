"""qwen3-vl-plus vision LLM client (image → markdown text).

image-extraction sprint tech design v1.0 §2.1.

Used by:
- ImageLoader (jpg/png/jpeg/webp): full image OCR/structure
- PdfLoader L2 (page-by-page render): scanned/image-only PDF fallback

API: dashscope OpenAI-compatible chat/completions endpoint (qwen3-vl-plus).
Sprint 0 spike 4 已实证 model 可用.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx2
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bowhead_core.ssrf_guard import build_guarded_async_client

logger = logging.getLogger(__name__)


# Default OCR + structure prompt. Caller can override via VisionLLMClient.extract_text(prompt=...)
DEFAULT_OCR_PROMPT = (
    "你是 OCR + 文档结构化助手. 输入是文档/图片. 你的输出:\n"
    "1. 按从左到右、从上到下的阅读顺序抽 ALL 文字\n"
    "2. 表格 → markdown table\n"
    "3. 标题 / 段落保持层级 (h1/h2 用 # / ##)\n"
    "4. 不解释, 不润色, 不添加内容\n"
    "5. 图章 / 签字 也尽量抽 (例: '北京谊慧信息技术有限公司公章')\n"
    "如果图片只是装饰 (logo / 边框 / 空白), 返一个空字符串."
)

DEFAULT_MODEL = "qwen3-vl-plus"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_TIMEOUT_SEC = 120


class VisionLLMError(Exception):
    """Vision LLM call failed (retryable: 5xx / 429 / timeout / network)."""


class VisionLLMNonRetryableError(Exception):
    """Vision LLM call failed permanently (4xx besides 429: bad image / auth / quota)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"[{status_code}] {message}")
        self.status_code = status_code


class VisionLLMClient:
    """qwen3-vl-plus client for OCR + structured extraction.

    Usage:
        client = VisionLLMClient(api_key="<configured team provider key>")
        text = await client.extract_text(image_b64, mime="image/png")

    image_b64: base64-encoded image bytes (no data URI prefix; we add it).
    mime: e.g. "image/png", "image/jpeg".
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        max_output_tokens: int = 4000,
        ssrf_guard: bool = False,
    ) -> None:
        if not api_key:
            raise ValueError("VisionLLMClient requires non-empty api_key")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._max_output_tokens = max_output_tokens
        self._ssrf_guard = ssrf_guard

    async def extract_text(
        self,
        image_b64: str,
        *,
        mime: str = "image/png",
        prompt: str = DEFAULT_OCR_PROMPT,
    ) -> str:
        """Vision LLM → markdown text. Returns empty string if image is decorative.

        - Tenacity 4 retries exponential (1s/2s/4s/8s) on 5xx / 429 / timeout / network.
        - 4xx except 429 → VisionLLMNonRetryableError raised (caller should not retry).
        - All other → VisionLLMError raised after retries exhausted.
        """
        if not image_b64:
            return ""
        data_uri = f"data:{mime};base64,{image_b64}"
        try:
            return await self._call_with_retry(prompt=prompt, data_uri=data_uri)
        except VisionLLMNonRetryableError as exc:
            logger.warning("vision_llm.non_retryable error=%s", exc)
            raise
        except VisionLLMError as exc:
            logger.warning("vision_llm.failed error=%s", exc)
            raise

    async def _call_with_retry(self, *, prompt: str, data_uri: str) -> str:

        @retry(
            stop=stop_after_attempt(4),
            wait=wait_exponential(min=1, max=8),
            retry=retry_if_exception_type(VisionLLMError),
            reraise=True,
        )
        async def _call() -> str:
            return await self._call_once(prompt=prompt, data_uri=data_uri)

        return await _call()

    async def _call_once(self, *, prompt: str, data_uri: str) -> str:
        """Single API call. 4xx (≠429) → VisionLLMNonRetryableError. Else → VisionLLMError."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "max_tokens": self._max_output_tokens,
            "temperature": 0.0,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
        except httpx2.TimeoutException as exc:
            raise VisionLLMError(f"timeout: {exc}") from exc
        except httpx2.HTTPError as exc:
            raise VisionLLMError(f"network: {exc}") from exc

        status = resp.status_code
        if status == 200:
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                raise VisionLLMError(f"no choices in response: {data}")
            content = (choices[0].get("message") or {}).get("content") or ""
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            return str(content).strip()

        # error path
        try:
            err_msg = resp.json().get("error", {}).get("message", "")
        except Exception:
            err_msg = resp.text[:300]

        if 400 <= status < 500 and status != 429:
            raise VisionLLMNonRetryableError(status, err_msg)
        raise VisionLLMError(f"dashscope vision_llm [{status}]: {err_msg}")

    def _client(self) -> httpx2.AsyncClient:
        if not self._ssrf_guard:
            return httpx2.AsyncClient(timeout=self._timeout_sec)
        return build_guarded_async_client(
            timeout=self._timeout_sec,
        )
