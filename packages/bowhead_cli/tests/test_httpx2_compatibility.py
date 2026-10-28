"""Verify SDK and mock boundaries use the migrated HTTP client types."""

import subprocess
import sys

import httpx2
import pytest
import respx


@pytest.mark.parametrize("entrypoint", ["bowhead_core", "bowhead_gateway"])
def test_application_startup_shares_sdk_http_types(entrypoint):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"""
import {entrypoint}
import importlib
import httpx2
from mcp.client.auth.oauth2 import OAuthClientProvider
from openai import AsyncOpenAI

assert issubclass(OAuthClientProvider, httpx2.Auth)
assert importlib.import_module('httpx._models').Response is httpx2.Response
assert importlib.import_module('httpcore').ConnectError is importlib.import_module('httpcore2').ConnectError
client = AsyncOpenAI(api_key='test', http_client=httpx2.AsyncClient())
import asyncio
asyncio.run(client.close())
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@respx.mock
def test_respx_intercepts_httpx2_sync_client():
    route = respx.get("https://example.test/resource").respond(200, json={"ok": True})
    with httpx2.Client() as client:
        response = client.get("https://example.test/resource")
    assert response.json() == {"ok": True}
    assert isinstance(route.calls.last.request, httpx2.Request)


@pytest.mark.asyncio
@respx.mock
async def test_respx_preserves_httpx2_streams_and_exceptions():
    respx.get("https://example.test/stream").mock(return_value=httpx2.Response(200, content=b"data: done\n\n"))
    respx.get("https://example.test/timeout").mock(side_effect=httpx2.ReadTimeout("timed out"))
    async with httpx2.AsyncClient() as client:
        async with client.stream("GET", "https://example.test/stream") as response:
            assert [line async for line in response.aiter_lines()] == ["data: done", ""]
        with pytest.raises(httpx2.ReadTimeout, match="timed out"):
            await client.get("https://example.test/timeout")
