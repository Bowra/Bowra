from __future__ import annotations

import asyncio
import gzip
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx2
import mcp.types
import pytest

from bowhead_core import openapi_tool_source as source


class _ChunkStream(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.iterated = False

    async def __aiter__(self):
        self.iterated = True
        for chunk in self._chunks:
            yield chunk


class _Session:
    def __init__(self, pages: dict[str | None, mcp.types.ListPromptsResult], *, prompt_capability: bool = True):
        self.pages = pages
        self.capabilities = SimpleNamespace(prompts=object() if prompt_capability else None)
        self.cursors: list[str | None] = []

    @property
    def server_capabilities(self):
        return self.capabilities

    async def list_prompts(self, *, params=None):
        cursor = params.cursor if params else None
        self.cursors.append(cursor)
        return self.pages[cursor]


class _EndlessSession(_Session):
    def __init__(self):
        super().__init__({})

    async def list_prompts(self, *, params=None):
        cursor = params.cursor if params else None
        self.cursors.append(cursor)
        return mcp.types.ListPromptsResult(
            prompts=[],
            next_cursor=f"page-{len(self.cursors)}",
        )


class _SlowSession(_Session):
    def __init__(self):
        super().__init__({})

    async def list_prompts(self, *, params=None):
        cursor = params.cursor if params else None
        self.cursors.append(cursor)
        await asyncio.sleep(0.05)
        return mcp.types.ListPromptsResult(prompts=[])


def _connect(session: _Session):
    @asynccontextmanager
    async def connect(*_args, **_kwargs):
        yield session

    return connect


@pytest.mark.asyncio
async def test_prompt_discovery_paginates_metadata_without_expansion(monkeypatch):
    session = _Session(
        {
            None: mcp.types.ListPromptsResult(
                prompts=[
                    mcp.types.Prompt(
                        name="profitability",
                        title="Profitability",
                        description="Explain profitability",
                        arguments=[
                            mcp.types.PromptArgument(
                                name="period",
                                description="Reporting period",
                                required=True,
                            ),
                        ],
                    ),
                ],
                next_cursor="page-2",
            ),
            "page-2": mcp.types.ListPromptsResult(
                prompts=[mcp.types.Prompt(name="cashflow")],
            ),
        },
    )
    monkeypatch.setattr(source, "connect_remote_mcp", _connect(session))

    result = await source.enumerate_remote_mcp_prompts("https://example.com/mcp")

    assert session.cursors == [None, "page-2"]
    assert result == [
        {
            "name": "profitability",
            "title": "Profitability",
            "description": "Explain profitability",
            "arguments": [
                {
                    "name": "period",
                    "description": "Reporting period",
                    "required": True,
                },
            ],
        },
        {
            "name": "cashflow",
            "title": "",
            "description": "",
            "arguments": [],
        },
    ]


@pytest.mark.asyncio
async def test_prompt_discovery_skips_list_when_server_has_no_capability(monkeypatch):
    session = _Session({}, prompt_capability=False)
    monkeypatch.setattr(source, "connect_remote_mcp", _connect(session))

    assert await source.enumerate_remote_mcp_prompts("https://example.com/mcp") == []
    assert session.cursors == []


@pytest.mark.asyncio
async def test_prompt_discovery_rejects_duplicate_names_across_pages(monkeypatch):
    session = _Session(
        {
            None: mcp.types.ListPromptsResult(
                prompts=[mcp.types.Prompt(name="same")],
                next_cursor="again",
            ),
            "again": mcp.types.ListPromptsResult(
                prompts=[mcp.types.Prompt(name="same")],
            ),
        },
    )
    monkeypatch.setattr(source, "connect_remote_mcp", _connect(session))

    with pytest.raises(ValueError, match="duplicate MCP Prompt name"):
        await source.enumerate_remote_mcp_prompts("https://example.com/mcp")


@pytest.mark.asyncio
async def test_prompt_discovery_bounds_empty_cursor_pages(monkeypatch):
    session = _EndlessSession()
    monkeypatch.setattr(source, "connect_remote_mcp", _connect(session))

    with pytest.raises(ValueError, match="MCP Prompt page limit exceeded"):
        await source.enumerate_remote_mcp_prompts("https://example.com/mcp")
    assert len(session.cursors) == source._MAX_PROMPT_PAGES


@pytest.mark.asyncio
async def test_prompt_discovery_rejects_more_than_128_arguments(monkeypatch):
    prompt = mcp.types.Prompt(
        name="too_many",
        arguments=[mcp.types.PromptArgument(name=f"argument_{index}") for index in range(129)],
    )
    session = _Session(
        {None: mcp.types.ListPromptsResult(prompts=[prompt])},
    )
    monkeypatch.setattr(source, "connect_remote_mcp", _connect(session))

    with pytest.raises(ValueError, match="argument count limit exceeded"):
        await source.enumerate_remote_mcp_prompts("https://example.com/mcp")


@pytest.mark.asyncio
async def test_prompt_discovery_has_one_absolute_deadline(monkeypatch):
    session = _SlowSession()
    monkeypatch.setattr(source, "connect_remote_mcp", _connect(session))

    with pytest.raises(TimeoutError):
        await source.enumerate_remote_mcp_prompts(
            "https://example.com/mcp",
            timeout=0.01,
        )


@pytest.mark.asyncio
async def test_prompt_response_limit_rejects_compression_before_decoding():
    limit = 1024
    compressed = gzip.compress(b'{"prompts":"' + b"x" * limit + b'"}')
    stream = _ChunkStream([compressed])

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            stream=stream,
            request=request,
        )

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        event_hooks={
            "response": [
                lambda response: source._limit_mcp_response_bytes(
                    response,
                    limit=limit,
                )
            ]
        },
    ) as client:
        with pytest.raises(source.McpResponseTooLarge):
            await client.get("https://example.com/mcp")
    assert stream.iterated is False


def test_prompt_discovery_requests_identity_encoding():
    headers = source._mcp_client_headers(
        {"accept-encoding": "gzip", "authorization": "Bearer secret"},
        response_bytes_limit=1024,
    )

    assert headers["accept-encoding"] == "identity"
    assert headers["authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_prompt_response_limit_stops_chunked_sse_event():
    limit = 128

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_ChunkStream([b"data: " + b"x" * 80, b"x" * 80 + b"\n\n"]),
            request=request,
        )

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        event_hooks={
            "response": [
                lambda response: source._limit_mcp_response_bytes(
                    response,
                    limit=limit,
                )
            ]
        },
    ) as client:
        with pytest.raises(source.McpResponseTooLarge):
            async with client.stream("GET", "https://example.com/mcp") as response:
                async for _line in response.aiter_lines():
                    pass
