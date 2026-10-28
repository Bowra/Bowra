"""cca ④f — credential scrubbing at the tool_registry.execute() chokepoint.

The engine (④a–④e) scrubs its own expected-failure dicts, but ANY tool (native /
MCP / team-created) can RAISE an exception whose text carries a credential; that text
funnels through execute()'s except blocks into ``invocation.error`` → the LLM. ④f
re-applies the shared scrubber there (belt-and-suspenders) + at the subagent fallback
paths. These tests drive the chokepoint directly and assert the cred never survives.
"""

from __future__ import annotations

import pytest
from bowhead_core.scrub import scrub
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
)
from bowhead_router.types import ToolSchema

from bowhead_bi_bridge import DatarcError


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=None,  # type: ignore[arg-type]
    )


def _registry_with_raising_tool(name: str, exc: Exception) -> ToolRegistry:
    async def _handler(arguments: dict, ctx: ToolExecutionContext):  # type: ignore[no-untyped-def]
        raise exc

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            schema=ToolSchema(name=name, description="", input_schema={"type": "object"}),
            handler=_handler,
        )
    )
    return reg


@pytest.mark.unit
def test_scrub_unit_redacts_each_shape() -> None:
    assert "tok123abc" not in scrub("Authorization: Bearer tok123abc")
    assert "dXNlcjpwYXNz" not in scrub("Authorization: Basic dXNlcjpwYXNz")
    assert "proj-abcdef123456" not in scrub("(sk-proj-abcdef123456)")
    assert "[redacted]" in scrub("api_key=topsecret&x=1")
    assert scrub("plain message").strip() == "plain message"


@pytest.mark.unit
def test_scrub_redacts_datarc_auth_key_field() -> None:
    # R7.5 (ws-datarc-decouple): the datarc connection secret is named `auth_key` — the
    # opaque key has no prefix, so we key off the field name. Covers both the query-string
    # and the dict-repr shapes (a connection dict logged by accident must not leak the key).
    assert "de0c-secret-abcd" not in scrub("auth_key=de0c-secret-abcd&base_url=x")
    assert "de0c-secret-abcd" not in scrub("{'auth_key': 'de0c-secret-abcd', 'name': 'prod'}")
    assert "de0c-secret-abcd" not in scrub('{"auth_key": "de0c-secret-abcd"}')
    # `authorization` (the existing branch) still works and isn't broken by the new alternation
    assert "tok-xyz" not in scrub("authorization: tok-xyz")


@pytest.mark.asyncio
async def test_execute_scrubs_generic_handler_exception() -> None:
    # :1288 unexpected_error path — a raised exception reflecting an injected auth header.
    reg = _registry_with_raising_tool(
        "boom",
        RuntimeError("call to https://api.x failed; sent Authorization: Bearer supersecret123"),
    )
    inv = await reg.execute("boom", {}, _ctx())
    assert inv.error is not None
    assert "supersecret123" not in inv.error
    assert "[redacted]" in inv.error  # both the bearer + keyword patterns fire (over-redact is safe)
    assert inv.error.startswith("unexpected_error:")  # format preserved around the scrub


@pytest.mark.asyncio
async def test_execute_scrubs_datarc_error() -> None:
    # :1282 DatarcError path — sk- key in the exception text must not survive.
    reg = _registry_with_raising_tool(
        "boom2",
        DatarcError("upstream rejected key sk-ant-api03-LEAKED12345abcd"),
    )
    inv = await reg.execute("boom2", {}, _ctx())
    assert inv.error is not None
    assert "ant-api03-LEAKED12345abcd" not in inv.error
    assert "[redacted-key]" in inv.error


@pytest.mark.unit
def test_extract_result_error_scrubs_result_dict_error_message() -> None:
    # ④f CTO-review: the result-dict error → SSE step_done.error chokepoint. A tool that
    # returns {ok:False, error_message: <cred>} must not surface the cred to the FE.
    from bowhead_core.subagent import _extract_result_error

    kind, msg = _extract_result_error(
        {"ok": False, "error_kind": "datarc_error", "error_message": "auth failed: Authorization: Bearer leaked-tok-998877"},
    )
    assert kind == "datarc_error"
    assert msg is not None
    assert "leaked-tok-998877" not in msg
