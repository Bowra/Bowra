"""mcp-connect-repair F1 — remote MCP call budget + holding shape.

Regression cover for the two defects instrumented in
``docs/sprints/mcp-connect-repair-2026-09-04/discovery.md``:

  F1-a  one ``timeout`` used to be the WHOLE httpx2 client timeout, so the
        connect budget silently capped every tool call — anything slower than
        5s was severed mid-flight (116: deepwiki 3.5s tool worked, 14.9s did not).
  F1-b  the session was entered on the TURN's exit stack (one task), first-connected
        and dispatched from a SUBAGENT task, then unwound back in the turn's task →
        ``RuntimeError: Attempted to exit cancel scope in a different task``.

These are transport- and task-structure properties: a mocked ClientSession reproduces
NEITHER (the original bugs both survived a green suite). So this module talks to a REAL
Streamable-HTTP MCP server over loopback, and drives it through the production holding
shape.

What lets the SSRF guard reach 127.0.0.1 here is ``BOWHEAD_DEPLOYMENT_MODE`` defaulting
to ``local`` — NOT a per-call override. (It used to say ``allow_loopback`` did it; that
param had been inert since 22014fa9e and was removed in 2026-09. Proof the attribution
was wrong: run this module with ``BOWHEAD_DEPLOYMENT_MODE=saas`` and 3 of its 6 tests
fail — no per-call argument could have saved them.) So these tests are local-mode tests
by construction; under saas the guard blocks loopback and that is the intended policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from bowhead_core.openapi_tool_source import (
    REMOTE_MCP_CONNECT_TIMEOUT_SEC,
    RemoteMcpToolClient,
    connect_remote_mcp,
    mcp_entry,
)
from bowhead_core.team_tool_registry_view import build_team_specs
from bowhead_core.tool_registry import ToolExecutionContext
from mcp.server import MCPServer

_ORG = "team_a"
_SRC = "ots_remote1"
# Comfortably over the connect budget: the whole point is that a call may outlive it.
_SLOW_SEC = REMOTE_MCP_CONNECT_TIMEOUT_SEC + 3.0


def _build_app():
    server = MCPServer("budget-probe")

    @server.tool()
    async def echo_after(delay: float = 0.0) -> str:
        """Sleep, then answer — the delay is the whole point."""
        await asyncio.sleep(delay)
        return f"answered after {delay}"

    return server.streamable_http_app(stateless_http=True, json_response=True)


@pytest.fixture(scope="module")
def mcp_url() -> Iterator[str]:
    """A real MCP server on an ephemeral loopback port, for the duration of the module."""
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
        pytest.fail("probe MCP server did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _ctx(team_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=None,  # type: ignore[arg-type]
        team_id=team_id,
        role=None,
    )


def _entries() -> list:
    return [mcp_entry(_SRC, {"name": "echo_after", "description": "sleep then answer", "input_schema": {"type": "object"}})]


@pytest.mark.asyncio
async def test_call_may_outlive_the_connect_budget(mcp_url: str) -> None:
    """F1-a: the connect budget must NOT cap the call.

    Mutation check: collapsing the two budgets back to ``httpx2.Timeout(timeout)``
    makes this red (ReadTimeout at the connect budget)."""
    async with connect_remote_mcp(mcp_url, {}) as session:
        started = time.monotonic()
        result = await session.call_tool("echo_after", {"delay": _SLOW_SEC})
    elapsed = time.monotonic() - started
    assert result.is_error is False
    # It really did outlive the connect budget — otherwise the assertion above is vacuous.
    assert elapsed > REMOTE_MCP_CONNECT_TIMEOUT_SEC


@pytest.mark.asyncio
async def test_connect_budget_still_bounds_an_unreachable_server() -> None:
    """The split must not disarm the fast-fail: reaching a dead port stays bounded.

    (Positive control for the test above — proves ``timeout`` is still load-bearing.)"""
    started = time.monotonic()
    with pytest.raises(BaseException):  # noqa: B017,PT011 - SDK wraps in an ExceptionGroup
        async with connect_remote_mcp("http://127.0.0.1:1/mcp", {}, timeout=1.0):
            pass
    assert time.monotonic() - started < 15


@pytest.mark.asyncio
async def test_remote_mcp_dependency_logs_do_not_expose_query_credentials(
    mcp_url: str,
    caplog,
) -> None:
    caplog.set_level(logging.DEBUG)
    canary = "mcp-query-canary"

    async with connect_remote_mcp(
        f"{mcp_url}?token={canary}",
        {},
    ) as session:
        await session.list_tools()
        logging.getLogger("bowhead_core.mcp_transport_test").info("application-log-remains-visible")
    logging.getLogger("httpx2").info("httpx2-outside-mcp-remains-visible")

    assert canary not in caplog.text
    assert "application-log-remains-visible" in caplog.text
    assert "httpx2-outside-mcp-remains-visible" in caplog.text


@pytest.mark.asyncio
async def test_production_holding_shape_has_no_cross_task_violation(mcp_url: str) -> None:
    """F1-b: dispatch from another task while an exit stack is alive in this one.

    That is production (turn stack in the turn's task, dispatch in a subagent task).
    Mutation check: restoring the held-session shape makes this red with
    ``RuntimeError: Attempted to exit cancel scope in a different task``."""
    async with contextlib.AsyncExitStack():
        client = RemoteMcpToolClient(mcp_url, {})
        # the dispatching task is NOT the task owning the stack above
        result = await asyncio.create_task(client.call_tool("echo_after", {"delay": _SLOW_SEC}))
    assert result.is_error is False


@pytest.mark.asyncio
async def test_runaway_call_is_bounded_not_hung(mcp_url: str) -> None:
    """A tool slower than its budget must FAIL — the 116 symptom was a turn that never ended."""
    client = RemoteMcpToolClient(mcp_url, {}, timeout=2.0, call_timeout=1.0)
    started = time.monotonic()
    with pytest.raises(BaseException):  # noqa: B017,PT011 - TimeoutError or an SDK-wrapped group
        await client.call_tool("echo_after", {"delay": 20.0})
    assert time.monotonic() - started < 15


@pytest.mark.asyncio
async def test_handler_maps_timeout_to_a_typed_envelope() -> None:
    """The model must be told the call was too SLOW, not handed a bare 'ExceptionGroup'."""

    class _TimingOut:
        async def call_tool(self, name: str, arguments: dict) -> object:
            # the shape production raises: a timeout nested in the SDK's task-group ExceptionGroup
            raise BaseExceptionGroup("unhandled errors in a TaskGroup", [TimeoutError()])

    spec = build_team_specs(_ORG, _entries(), _TimingOut())[0]
    res = await spec.handler({}, _ctx(_ORG))
    assert res["ok"] is False
    assert res["error_kind"] == "timeout"
    assert "too slow" in res["error"]
