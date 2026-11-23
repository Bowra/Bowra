"""Team MCP calls name the upstream HTTP status instead of a bare ``ExceptionGroup``.

On 2026-11-22 an n8n instance-level MCP server rate-limited Bowhead (HTTP 429 with
``Retry-After``). The handler reported ``protocol`` / ``ExceptionGroup``, so the model kept
calling every tool of the source. The first test drives the real MCP SDK against a loopback
server that answers 429, because the shape the SDK raises is what the mapping depends on.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx2
import pytest
import uvicorn
from bowhead_core.openapi_tool_source import RemoteMcpToolClient, mcp_entry
from bowhead_core.team_tool_registry_view import build_team_specs, is_upstream_auth_error
from bowhead_core.tool_registry import ToolExecutionContext

_TEAM = "team_a"
_SRC = "ots_remote1"


async def _refusing_server(scope, receive, send) -> None:  # noqa: ANN001 - raw ASGI app
    """Answer every request with the status named by the path: /429 rate-limits, /401 rejects."""
    if scope["type"] != "http":
        return
    status = 401 if scope["path"].startswith("/401") else 429
    headers = [(b"content-type", b"application/json")]
    if status == 429:
        headers.append((b"retry-after", b"13"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": b'{"message":"refused"}'})


@pytest.fixture(scope="module")
def refusing_base() -> Iterator[str]:
    server = uvicorn.Server(uvicorn.Config(_refusing_server, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if server.started and server.servers and server.servers[0].sockets:
            break
        time.sleep(0.05)
    else:  # pragma: no cover - only on a wedged CI box
        server.should_exit = True
        pytest.fail("rate-limited probe server did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=None,  # type: ignore[arg-type]
        team_id=_TEAM,
        role=None,
    )


def _spec(client: object):
    entry = mcp_entry(_SRC, {"name": "get_workflow_execution", "description": "poll", "input_schema": {"type": "object"}})
    return build_team_specs(_TEAM, [entry], client)[0]  # type: ignore[arg-type]


def _status_error(status: int, headers: dict[str, str] | None = None) -> httpx2.HTTPStatusError:
    request = httpx2.Request("POST", "https://mcp.example/mcp")
    response = httpx2.Response(status, headers=headers or {}, request=request)
    return httpx2.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class _Raising:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def call_tool(self, name: str, arguments: dict) -> object:
        raise self._exc


@pytest.mark.asyncio
async def test_real_sdk_429_becomes_rate_limited_with_retry_after(refusing_base: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="bowhead_core.team_tool_registry_view")

    res = await _spec(RemoteMcpToolClient(f"{refusing_base}/429/mcp", {}, timeout=5.0, call_timeout=5.0)).handler({}, _ctx())

    assert res["ok"] is False
    assert res["error_kind"] == "rate_limited"
    assert res["retry_after_seconds"] == 13
    assert "HTTP 429" in res["error"]
    assert "13 seconds" in res["error"]
    assert "ExceptionGroup" not in res["error"]
    assert "error_kind=rate_limited http_status=429" in caplog.text


@pytest.mark.asyncio
async def test_real_sdk_401_is_attributed_to_the_team_credential(refusing_base: str) -> None:
    res = await _spec(RemoteMcpToolClient(f"{refusing_base}/401/mcp", {}, timeout=5.0, call_timeout=5.0)).handler({}, _ctx())

    assert res["error_kind"] == "upstream_auth"
    assert _SRC in res["error"]


@pytest.mark.asyncio
async def test_retry_after_http_date_is_converted_to_seconds() -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=90), usegmt=True)
    exc = BaseExceptionGroup("unhandled errors in a TaskGroup", [_status_error(429, {"Retry-After": when})])

    res = await _spec(_Raising(exc)).handler({}, _ctx())

    assert res["error_kind"] == "rate_limited"
    assert 80 <= res["retry_after_seconds"] <= 90


@pytest.mark.asyncio
async def test_429_without_a_usable_hint_says_so() -> None:
    exc = BaseExceptionGroup("g", [_status_error(429, {"Retry-After": "soon"})])

    res = await _spec(_Raising(exc)).handler({}, _ctx())

    assert res["error_kind"] == "rate_limited"
    assert "retry_after_seconds" not in res
    assert "did not say how long to wait" in res["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "kind", "phrase"), [(404, "upstream_4xx", "refused"), (502, "upstream_5xx", "unknown")])
async def test_other_http_statuses_are_named(status: int, kind: str, phrase: str) -> None:
    exc = BaseExceptionGroup("g", [_status_error(status)])

    res = await _spec(_Raising(exc)).handler({}, _ctx())

    assert res["error_kind"] == kind
    assert f"HTTP {status}" in res["error"]
    assert phrase in res["error"]


@pytest.mark.asyncio
async def test_auth_and_plain_protocol_failures_keep_their_mapping() -> None:
    auth = await _spec(_Raising(BaseExceptionGroup("g", [_status_error(401)]))).handler({}, _ctx())
    other = await _spec(_Raising(BaseExceptionGroup("g", [ValueError("bad frame")]))).handler({}, _ctx())

    assert auth["error_kind"] == "upstream_auth"
    assert other == {"ok": False, "error_kind": "protocol", "error": "ExceptionGroup"}


def test_auth_detection_walks_groups_and_causes() -> None:
    chained = RuntimeError("wrapper")
    chained.__cause__ = _status_error(403)

    assert is_upstream_auth_error(BaseExceptionGroup("g", [ValueError("x"), chained]))
    assert not is_upstream_auth_error(BaseExceptionGroup("g", [_status_error(429)]))
