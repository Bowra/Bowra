"""Sprint α-1 — Embedder semaphore unit tests.

Verify class-level semaphore limits concurrent embed_batch calls so the
embed endpoint doesn't 429 us. Uses asyncio events to assert at most N
batches run in parallel.

kb-model-config-2026-05-26: Embedder ctor now takes ModelSpec; tests use
a fixture spec rather than the old `api_key="x"` direct-key signature.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

import httpx2
import pytest
from bowhead_core.kb.config_loader import ModelSpec
from bowhead_core.kb.embedder import Embedder


def _spec(dim: int = 1024) -> ModelSpec:
    return ModelSpec(
        name="test-embed",
        capability="embed",
        provider="test",
        real_id="test/embed-model",
        base_url="https://test.invalid/v1",
        api_key="test-key",
        timeout_sec=30,
        dim=dim,
    )


@pytest.fixture(autouse=True)
def _reset_semaphore() -> None:
    """Drop class-level cached semaphore before each test so env reads fresh."""
    Embedder._reset_semaphore_for_test()
    yield
    Embedder._reset_semaphore_for_test()


@pytest.mark.asyncio
async def test_semaphore_caps_concurrent_batches() -> None:
    """N concurrent embed_batch calls → only `BOWHEAD_EMBED_CONCURRENCY` run at once."""
    os.environ["BOWHEAD_EMBED_CONCURRENCY"] = "2"

    e = Embedder(_spec())
    in_flight = 0
    max_seen = 0
    lock = asyncio.Lock()

    async def fake_call_with_retry(texts: list[str]) -> list[list[float]]:
        nonlocal in_flight, max_seen
        async with lock:
            in_flight += 1
            max_seen = max(max_seen, in_flight)
        await asyncio.sleep(0.05)  # hold the slot to surface concurrency
        async with lock:
            in_flight -= 1
        return [[0.1] * 1536 for _ in texts]

    with patch.object(e, "_call_with_retry", side_effect=fake_call_with_retry):
        # 6 concurrent batches, semaphore=2 → max_seen should be exactly 2
        await asyncio.gather(*[e.embed_batch(["t"]) for _ in range(6)])

    assert max_seen == 2, f"expected ≤ 2 concurrent, saw {max_seen}"
    del os.environ["BOWHEAD_EMBED_CONCURRENCY"]


@pytest.mark.asyncio
async def test_default_concurrency_is_4() -> None:
    """No env → default 4."""
    os.environ.pop("BOWHEAD_EMBED_CONCURRENCY", None)
    e = Embedder(_spec())
    sem = e._get_semaphore()
    # Semaphore reflects bound = 4 (initial _value)
    assert sem._value == 4


@pytest.mark.asyncio
async def test_env_clamp_to_min_1() -> None:
    """Env=0 or negative → clamped to 1."""
    os.environ["BOWHEAD_EMBED_CONCURRENCY"] = "0"
    e = Embedder(_spec())
    sem = e._get_semaphore()
    assert sem._value == 1
    del os.environ["BOWHEAD_EMBED_CONCURRENCY"]


@pytest.mark.asyncio
async def test_env_clamp_to_max_32() -> None:
    """Env=999 → clamped to 32 (sanity ceiling)."""
    os.environ["BOWHEAD_EMBED_CONCURRENCY"] = "999"
    e = Embedder(_spec())
    sem = e._get_semaphore()
    assert sem._value == 32
    del os.environ["BOWHEAD_EMBED_CONCURRENCY"]


@pytest.mark.asyncio
async def test_env_invalid_falls_back_to_default() -> None:
    """Env=garbage → default 4."""
    os.environ["BOWHEAD_EMBED_CONCURRENCY"] = "abc"
    e = Embedder(_spec())
    sem = e._get_semaphore()
    assert sem._value == 4
    del os.environ["BOWHEAD_EMBED_CONCURRENCY"]


@pytest.mark.asyncio
async def test_split_batch_does_not_re_acquire() -> None:
    """400/422 split must reuse parent slot, not deadlock when sem=1."""
    os.environ["BOWHEAD_EMBED_CONCURRENCY"] = "1"

    e = Embedder(_spec())
    call_count = 0

    from bowhead_core.kb.embedder import _NonRetryableError

    async def fake_call_with_retry(texts: list[str]) -> list[list[float]]:
        nonlocal call_count
        call_count += 1
        # First call (size 2) raises NonRetryable to force split; halves succeed
        if len(texts) > 1:
            raise _NonRetryableError(400, "simulated")
        return [[0.1] * 1536 for _ in texts]

    with patch.object(e, "_call_with_retry", side_effect=fake_call_with_retry):
        # Tight timeout: deadlock would block, success returns < 1s.
        result = await asyncio.wait_for(e.embed_batch(["a", "b"]), timeout=2.0)

    assert len(result) == 2
    # Parent (size 2) + 2 children (size 1) = 3 calls
    assert call_count == 3
    del os.environ["BOWHEAD_EMBED_CONCURRENCY"]


@pytest.mark.asyncio
async def test_empty_input_skips_semaphore() -> None:
    """embed_batch([]) returns immediately, doesn't acquire (would block sem=0)."""
    e = Embedder(_spec())
    result = await asyncio.wait_for(e.embed_batch([]), timeout=0.5)
    assert result == []


@pytest.mark.asyncio
async def test_azure_embedding_uses_deployment_endpoint() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={"data": [{"embedding": [0.1, 0.2]}]},
        )

    spec = ModelSpec(
        name="azure-embed",
        capability="embed",
        provider="uptimize",
        real_id="text-embedding-3-small",
        base_url="https://api.example.com",
        api_key="secret",
        timeout_sec=30,
        dim=2,
        provider_type="azure_openai",
        provider_options={
            "endpoint": "https://api.example.com",
            "api_version": "2024-10-21",
            "auth": {"type": "bearer"},
            "models": [
                {
                    "id": "text-embedding-3-small",
                    "deployment": "embed-prod",
                    "capabilities": ["embed"],
                }
            ],
        },
    )
    embedder = Embedder(spec)

    def client() -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))

    with patch.object(embedder, "_client", side_effect=client):
        result = await embedder.embed_batch(["hello"])

    assert result == [[0.1, 0.2]]
    assert seen[0].url == ("https://api.example.com/openai/deployments/embed-prod/embeddings?api-version=2024-10-21")
    assert seen[0].headers["authorization"] == "Bearer secret"
    assert json.loads(seen[0].content) == {
        "model": "text-embedding-3-small",
        "input": ["hello"],
    }
