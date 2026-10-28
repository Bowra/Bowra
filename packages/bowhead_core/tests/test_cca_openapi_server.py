"""cca block ④b — in-process MCP server + ClientSession round-trip.

Asserts the spike-proven engine (tech-design §2/§3.4): build_team_mcp_server builds
a low-level mcp.server.Server whose list_tools advertises the team's types.Tool and
whose call_tool routes to the injected executor; connect_team_mcp opens a real
in-memory ClientSession (no wire/subprocess). Failure modes (unknown tool, executor
raise, invalid args) surface as CallToolResult.is_error=True (SDK-native), which the
④e view handler will map to Bowhead's {ok/error_kind/error}.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from bowhead_core.openapi_tool_source import (
    OpenApiOp,
    TeamToolEntry,
    build_team_mcp_server,
    connect_team_mcp,
    op_tool_name,
    openapi_entry,
    parse_openapi_ops,
)

_SRC = "ots_datarc1"


def _spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "datarc", "version": "1"},
        "paths": {
            "/metrics/{mid}": {
                "get": {
                    "operationId": "getMetric",
                    "summary": "Fetch metric",
                    "parameters": [
                        {"name": "mid", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "tenant", "in": "header", "required": True, "schema": {"type": "string"}},
                        {"name": "window", "in": "query", "required": False, "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/query": {
                "post": {
                    "operationId": "runQuery",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": {"type": "object", "required": ["metric"], "properties": {"metric": {"type": "string"}}}}
                        },
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }


def _entries() -> list[TeamToolEntry]:
    return [openapi_entry(_SRC, op) for op in parse_openapi_ops(_spec())]


def _text(res: Any) -> str:
    return res.content[0].text  # first content block is our TextContent


@pytest.mark.asyncio
async def test_list_tools_advertises_team_tools() -> None:
    calls: list = []

    async def _exec(sid: str, op: OpenApiOp, args: dict) -> Any:
        calls.append((sid, op.operation_id, args))
        return {"echo": args, "op": op.operation_id, "src": sid}

    server = build_team_mcp_server(_entries(), _exec)
    async with connect_team_mcp(server) as client:
        listed = await client.list_tools()
    names = {t.name for t in listed.tools}
    assert names == {op_tool_name(_SRC, "getMetric"), op_tool_name(_SRC, "runQuery")}
    # inputSchema reaches the client intact
    gm = next(t for t in listed.tools if t.name == op_tool_name(_SRC, "getMetric"))
    assert set(gm.input_schema["properties"]) == {"mid", "tenant", "window"}


@pytest.mark.asyncio
async def test_call_tool_routes_to_executor_and_returns_result() -> None:
    calls: list = []

    async def _exec(sid: str, op: OpenApiOp, args: dict) -> Any:
        calls.append((sid, op.operation_id, args))
        return {"echo": args, "op": op.operation_id, "src": sid}

    server = build_team_mcp_server(_entries(), _exec)
    async with connect_team_mcp(server) as client:
        res = await client.call_tool(op_tool_name(_SRC, "getMetric"), {"mid": "m1", "tenant": "t1"})
    assert res.is_error is False
    payload = json.loads(_text(res))
    assert payload == {"echo": {"mid": "m1", "tenant": "t1"}, "op": "getMetric", "src": _SRC}
    # executor actually invoked with the routed op + args
    assert calls == [(_SRC, "getMetric", {"mid": "m1", "tenant": "t1"})]


@pytest.mark.asyncio
async def test_unknown_tool_is_error() -> None:
    async def _exec(sid: str, op: OpenApiOp, args: dict) -> Any:
        return {}

    server = build_team_mcp_server(_entries(), _exec)
    async with connect_team_mcp(server) as client:
        res = await client.call_tool("teammcp.ots_datarc1.doesNotExist", {})
    assert res.is_error is True


@pytest.mark.asyncio
async def test_executor_raise_surfaces_as_is_error() -> None:
    async def _exec(sid: str, op: OpenApiOp, args: dict) -> Any:
        raise RuntimeError("upstream boom")

    server = build_team_mcp_server(_entries(), _exec)
    async with connect_team_mcp(server) as client:
        res = await client.call_tool(op_tool_name(_SRC, "runQuery"), {"metric": "rev"})
    assert res.is_error is True


@pytest.mark.asyncio
async def test_invalid_args_rejected_before_dispatch() -> None:
    """validate_input=True: missing required path/header param → isError, executor
    never reached."""
    reached = []

    async def _exec(sid: str, op: OpenApiOp, args: dict) -> Any:
        reached.append(args)
        return {}

    server = build_team_mcp_server(_entries(), _exec)
    async with connect_team_mcp(server) as client:
        res = await client.call_tool(op_tool_name(_SRC, "getMetric"), {"tenant": "t1"})  # missing required "mid"
    assert res.is_error is True
    assert reached == []  # dispatch blocked by input validation
