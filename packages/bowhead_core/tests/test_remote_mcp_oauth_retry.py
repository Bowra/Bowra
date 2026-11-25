"""A team OAuth call whose access token the server refuses recovers once, before the call is sent.

The broker caches access tokens in memory; a provider can revoke one before it expires. When the
MCP server refuses the token during ``initialize`` the tool call has not been sent yet, so the
client asks the header source once more with the refused token and retries. A refusal of
``tools/call`` itself may follow partial processing on some servers, so it is reported instead.
Driven through the real MCP SDK against a loopback server behind a token gate.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
import uvicorn
from bowhead_core.openapi_tool_source import RemoteMcpToolClient, refused_credential
from mcp.server import MCPServer

_SEEN: list[tuple[str, str]] = []


def _build_app() -> Any:
    server = MCPServer("oauth-retry-probe")

    @server.tool()
    async def ping() -> str:
        """Answer pong."""
        return "pong"

    inner = server.streamable_http_app(stateless_http=True, json_response=True)

    async def gate(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await inner(scope, receive, send)
            return
        messages: list[dict[str, Any]] = []
        body = b""
        while True:
            message = await receive()
            messages.append(message)
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        try:
            method = str(json.loads(body or b"{}").get("method") or "")
        except ValueError:
            method = ""
        token = dict(scope["headers"]).get(b"authorization", b"").decode()
        _SEEN.append((method, token))
        # "init-only" passes the handshake but not a tool call; "good" passes everything.
        if token != "Bearer good" and not (token == "Bearer init-only" and method != "tools/call"):
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error":"invalid_token"}'})
            return
        pending = iter(messages)

        async def replay() -> dict[str, Any]:
            try:
                return next(pending)
            except StopIteration:
                return await receive()

        await inner(scope, replay, send)

    return gate


@pytest.fixture(scope="module")
def gated_url() -> Iterator[str]:
    config = uvicorn.Config(_build_app(), host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if server.started and server.servers and server.servers[0].sockets:
            break
        time.sleep(0.05)
    else:  # pragma: no cover - only on a wedged CI box
        server.should_exit = True
        pytest.fail("gated MCP server did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class _Source:
    def __init__(self, *tokens: str) -> None:
        self.tokens = list(tokens)
        self.calls: list[str | None] = []

    async def __call__(self, rejected: str | None) -> dict[str, str]:
        self.calls.append(rejected)
        return {"Authorization": f"Bearer {self.tokens.pop(0)}"}


@pytest.mark.asyncio
async def test_token_refused_at_initialize_is_renewed_once(gated_url: str) -> None:
    source = _Source("stale", "good")
    result = await RemoteMcpToolClient(gated_url, {}, timeout=5.0, call_timeout=5.0, header_source=source).call_tool("ping", {})
    assert not result.is_error
    assert source.calls == [None, "stale"]


@pytest.mark.asyncio
async def test_token_refused_for_the_call_itself_is_not_retried(gated_url: str) -> None:
    _SEEN.clear()
    source = _Source("init-only", "good")
    with pytest.raises(Exception) as caught:
        await RemoteMcpToolClient(gated_url, {}, timeout=5.0, call_timeout=5.0, header_source=source).call_tool("ping", {})
    assert refused_credential(caught.value)
    assert source.calls == [None]
    assert [method for method, _ in _SEEN].count("tools/call") == 1


def test_refused_credential_only_matches_401() -> None:
    request = httpx2.Request("POST", "https://mcp.example/mcp")

    def status(code: int) -> httpx2.HTTPStatusError:
        return httpx2.HTTPStatusError("x", request=request, response=httpx2.Response(code, request=request))

    assert refused_credential(BaseExceptionGroup("g", [ValueError("x"), status(401)]))
    assert not refused_credential(status(403)) and not refused_credential(status(429))
