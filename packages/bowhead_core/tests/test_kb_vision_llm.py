"""image-extraction Day 1: VisionLLMClient unit tests (mock dashscope).

Tech design v1.0 §3.1 — 4 cases:
1. happy path: mock 200 response → markdown text
2. 5xx → tenacity retry → success
3. 429 (rate limit) → retry exhausted → VisionLLMError
4. 400 (bad image) → VisionLLMNonRetryableError immediately (no retry)
"""

from __future__ import annotations

import base64

import httpx2
import pytest
import respx
from bowhead_core.kb.vision_llm import (
    VisionLLMClient,
    VisionLLMError,
    VisionLLMNonRetryableError,
)


def _client() -> VisionLLMClient:
    return VisionLLMClient(api_key="sk-test-fake")


def _b64() -> str:
    return base64.b64encode(b"fake_png_bytes").decode("ascii")


@respx.mock
async def test_extract_text_happy_path() -> None:
    """200 → markdown content from choices[0].message.content."""
    respx.post("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions").mock(
        return_value=httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "# 身份证\n姓名: 张三"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )
    )
    result = await _client().extract_text(_b64(), mime="image/png")
    assert "# 身份证" in result
    assert "张三" in result


@respx.mock
async def test_5xx_retries_then_succeeds() -> None:
    """5xx → tenacity retry → eventually 200."""
    route = respx.post("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions").mock(
        side_effect=[
            httpx2.Response(503, json={"error": {"message": "server busy"}}),
            httpx2.Response(503, json={"error": {"message": "server busy"}}),
            httpx2.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "RECOVERED"}}],
                },
            ),
        ]
    )
    result = await _client().extract_text(_b64())
    assert result == "RECOVERED"
    assert route.call_count == 3


@respx.mock
async def test_429_retries_exhausted() -> None:
    """429 (rate limit) → 4 retries exhausted → VisionLLMError."""
    respx.post("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions").mock(
        return_value=httpx2.Response(
            429,
            json={"error": {"message": "rate limit"}},
        )
    )
    with pytest.raises(VisionLLMError):
        await _client().extract_text(_b64())


@respx.mock
async def test_400_bad_image_not_retried() -> None:
    """400 → VisionLLMNonRetryableError raised immediately (no retry, single call)."""
    route = respx.post("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions").mock(
        return_value=httpx2.Response(
            400,
            json={"error": {"message": "invalid image format"}},
        )
    )
    with pytest.raises(VisionLLMNonRetryableError) as exc_info:
        await _client().extract_text(_b64())
    assert exc_info.value.status_code == 400
    assert route.call_count == 1  # No retry.


async def test_empty_image_returns_empty_string() -> None:
    """Empty b64 → graceful empty (no API call)."""
    result = await _client().extract_text("", mime="image/png")
    assert result == ""


def test_constructor_requires_api_key() -> None:
    """Empty api_key → ValueError."""
    with pytest.raises(ValueError):
        VisionLLMClient(api_key="")
