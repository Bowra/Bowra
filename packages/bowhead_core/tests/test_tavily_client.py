"""Tests for bowhead_core.tavily_client — Tavily 出站的鉴权、错误分类与轮级短路。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from bowhead_core.tavily_client import tavily_post

from bowhead_core import tavily_client


def _resp(status: int, payload: Any = None, headers: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(status_code=status, json=lambda: payload if payload is not None else {}, headers=headers or {}, text="")


def _client(responses: list[SimpleNamespace], calls: list[dict[str, Any]]):
    queue = list(responses)

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> SimpleNamespace:
            calls.append({"url": url, "json": json, "headers": headers})
            return queue.pop(0)

    return FakeClient


@pytest.fixture(autouse=True)
def _clear_turn_blocks() -> None:
    tavily_client._turn_blocks.clear()


@pytest.mark.asyncio
async def test_post_uses_bearer_header_and_requests_usage() -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _client([_resp(200, {"results": [], "usage": {"credits": 1}})], calls)):
        data, error = await tavily_post("tvly-key", "/search", {"query": "q"}, timeout=5)

    assert error is None
    assert data == {"results": [], "usage": {"credits": 1}}
    assert calls == [
        {
            "url": "https://api.tavily.com/search",
            "json": {"query": "q", "include_usage": True},
            "headers": {"Authorization": "Bearer tvly-key", "Content-Type": "application/json"},
        }
    ]


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "tavily_key_invalid"),
        (432, "tavily_plan_quota_exhausted"),
        (433, "tavily_spend_limit_reached"),
        (400, "tavily_bad_request"),
        (500, "tavily_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_status_codes_map_to_error_kinds_with_next_step(status: int, kind: str) -> None:
    with patch("httpx2.AsyncClient", _client([_resp(status)], [])):
        data, error = await tavily_post("k", "/search", {"query": "q"}, timeout=5)

    assert data is None
    assert error is not None and error.kind == kind
    result = error.to_tool_result()
    assert result["error_kind"] == kind
    assert result["results"] == []
    assert result["error"]  # Every failure needs nonempty next-step guidance.


@pytest.mark.asyncio
async def test_bad_request_names_fields_without_upstream_prose() -> None:
    payload = {"detail": [{"loc": ["body", "time_range"], "msg": "long upstream prose", "type": "enum"}]}
    with patch("httpx2.AsyncClient", _client([_resp(422, payload)], [])):
        _data, error = await tavily_post("k", "/search", {"query": "q"}, timeout=5)

    message = error.to_tool_result()["error"]
    assert "time_range" in message
    assert "long upstream prose" not in message


@pytest.mark.asyncio
async def test_config_state_error_short_circuits_rest_of_turn() -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _client([_resp(432)], calls)):
        _first, first_error = await tavily_post("k", "/search", {"query": "a"}, timeout=5, turn_id="t_1")
        _second, second_error = await tavily_post("k", "/search", {"query": "b"}, timeout=5, turn_id="t_1")

    assert first_error.kind == second_error.kind == "tavily_plan_quota_exhausted"
    assert len(calls) == 1  # The second call in the same turn must not hit upstream again.


@pytest.mark.asyncio
async def test_transient_error_does_not_block_the_turn() -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _client([_resp(500), _resp(200, {"results": []})], calls)):
        await tavily_post("k", "/search", {"query": "a"}, timeout=5, turn_id="t_2")
        data, error = await tavily_post("k", "/search", {"query": "b"}, timeout=5, turn_id="t_2")

    assert error is None and data == {"results": []}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_rate_limit_retries_once_when_retry_after_is_short() -> None:
    calls: list[dict[str, Any]] = []
    responses = [_resp(429, headers={"retry-after": "0"}), _resp(200, {"results": []})]
    with patch("httpx2.AsyncClient", _client(responses, calls)):
        data, error = await tavily_post("k", "/search", {"query": "q"}, timeout=5)

    assert error is None and data == {"results": []}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_rate_limit_gives_up_when_retry_after_is_long() -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _client([_resp(429, headers={"retry-after": "60"})], calls)):
        _data, error = await tavily_post("k", "/search", {"query": "q"}, timeout=5)

    assert error.kind == "tavily_rate_limited"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_network_error_is_unavailable_without_status() -> None:
    class Boom:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Boom:
            raise OSError("connect failed")

        async def __aexit__(self, *args: Any) -> None:
            return None

    with patch("httpx2.AsyncClient", Boom):
        _data, error = await tavily_post("k", "/search", {"query": "q"}, timeout=5)

    assert error.kind == "tavily_unavailable"
    assert error.status is None


@pytest.mark.parametrize(
    ("path", "kind"), [("/extract", "tavily_url_not_supported"), ("/crawl", "tavily_url_not_supported"), ("/search", "tavily_key_invalid")]
)
@pytest.mark.asyncio
async def test_403_means_unsupported_site_only_on_url_endpoints(path: str, kind: str) -> None:
    with patch("httpx2.AsyncClient", _client([_resp(403)], [])):
        _data, error = await tavily_post("k", path, {}, timeout=5)
    assert error.kind == kind
    if kind == "tavily_url_not_supported":
        guidance = error.to_tool_result()["error"]
        assert "web_fetch" in guidance and "only if available" in guidance and "report the gap" in guidance


@pytest.mark.asyncio
async def test_non_ascii_key_is_invalid_not_unreachable() -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _client([], calls)):
        _data, error = await tavily_post("tvly-ｋey", "/search", {"query": "q"}, timeout=5)
    assert error.kind == "tavily_key_invalid"
    assert calls == []


@pytest.mark.asyncio
async def test_malformed_validation_body_keeps_the_real_status() -> None:
    payload = {"detail": [{"loc": {"weird": 1}, "msg": "x"}]}
    with patch("httpx2.AsyncClient", _client([_resp(422, payload)], [])):
        _data, error = await tavily_post("k", "/search", {"query": "q"}, timeout=5)
    assert (error.kind, error.status) == ("tavily_bad_request", 422)


@pytest.mark.asyncio
async def test_turn_block_does_not_hold_a_replaced_key() -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _client([_resp(401), _resp(200, {"results": []})], calls)):
        await tavily_post("old-key", "/search", {"query": "a"}, timeout=5, turn_id="t_9")
        data, error = await tavily_post("new-key", "/search", {"query": "b"}, timeout=5, turn_id="t_9")
    assert error is None and data == {"results": []}
    assert len(calls) == 2
