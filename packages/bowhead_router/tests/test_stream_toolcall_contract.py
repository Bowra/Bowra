"""Spike adapter-stream-toolcall · contract test.

Verifies anthropic_compat + openai_compat stream() accumulate tool_call deltas
correctly per their respective SSE protocols. These adapters previously dropped
tool_call deltas; spike-2 added accumulation. Sprint κ Phase B (Lead streaming
wire) depends on this — without tool_call surfacing in stream, Lead's iterative
tool-use loop terminates after first stream chunk.

Tests use httpx2.MockTransport to inject fake SSE responses; no real network.
"""

from __future__ import annotations

import json

import httpx2
import pytest
from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter
from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter
from bowhead_router.types import LLMChunk, LLMRequest, Message, ToolSchema


def _wire_anthropic_mock(adapter: AnthropicCompatibleAdapter, sse_bytes: bytes) -> None:
    """Replace adapter's _http with one that returns the given SSE on any request."""
    adapter._http = httpx2.AsyncClient(
        base_url="https://x",
        transport=httpx2.MockTransport(
            lambda req: httpx2.Response(
                200,
                content=sse_bytes,
                headers={"content-type": "text/event-stream"},
            ),
        ),
    )


def _wire_openai_mock(adapter: OpenAICompatibleAdapter, sse_bytes: bytes) -> None:
    """Replace adapter's _http with one that returns the given SSE on any request."""
    adapter._http = httpx2.AsyncClient(
        base_url="https://x",
        transport=httpx2.MockTransport(
            lambda req: httpx2.Response(
                200,
                content=sse_bytes,
                headers={"content-type": "text/event-stream"},
            ),
        ),
    )


# ─── Anthropic SSE fixtures ──────────────────────────────────────────────────


def _anthropic_sse(events: list[tuple[str, dict]]) -> bytes:
    """Build an SSE byte stream of (event_type, payload) tuples per Anthropic spec."""
    lines: list[str] = []
    for et, payload in events:
        lines.append(f"event: {et}")
        lines.append(f"data: {json.dumps(payload)}")
        lines.append("")  # blank line terminates event
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _anthropic_handler(body: bytes) -> httpx2.Response:
    """Default mock returns text + tool_use stream."""
    sse = _anthropic_sse(
        [
            ("message_start", {"type": "message_start", "message": {"id": "msg_1"}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "I'll check the weather."}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "tool_use", "id": "toolu_abc", "name": "get_weather", "input": {}},
                },
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"location":'}},
            ),
            ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ' "SF"}'}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"input_tokens": 10, "output_tokens": 20}}),
            ("message_stop", {"type": "message_stop"}),
        ]
    )
    return httpx2.Response(200, content=sse, headers={"content-type": "text/event-stream"})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_stream_accumulates_tool_call() -> None:
    """anthropic stream emits text_delta then tool_call_delta with full args."""
    sse = _anthropic_sse(
        [
            ("message_start", {"type": "message_start", "message": {"id": "msg_1"}}),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "I'll check the weather."}}),
            ("content_block_stop", {"index": 0}),
            ("content_block_start", {"index": 1, "content_block": {"type": "tool_use", "id": "toolu_abc", "name": "get_weather"}}),
            ("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"location":'}}),
            ("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": ' "SF"}'}}),
            ("content_block_stop", {"index": 1}),
            ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"input_tokens": 10, "output_tokens": 20}}),
            ("message_stop", {}),
        ]
    )
    adapter = AnthropicCompatibleAdapter(base_url="https://x", api_key="k")
    _wire_anthropic_mock(adapter, sse)
    req = LLMRequest(
        model="claude-test",
        messages=[Message(role="user", content="weather SF")],
        tools=[ToolSchema(name="get_weather", description="", input_schema={})],
    )

    chunks: list[LLMChunk] = []
    async for c in adapter.stream(req):
        chunks.append(c)

    text = "".join(c.delta for c in chunks)
    assert "weather" in text.lower(), f"text content missing, got: {text!r}"

    tcs = [c.tool_call_delta for c in chunks if c.tool_call_delta is not None]
    assert len(tcs) == 1, f"expected 1 tool_call, got {len(tcs)}"
    assert tcs[0].name == "get_weather"
    assert tcs[0].id == "toolu_abc"
    assert tcs[0].arguments == {"location": "SF"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_stream_text_only_no_tool_call() -> None:
    """Pure text stream emits no tool_call_delta."""
    sse = _anthropic_sse(
        [
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "hello"}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "end_turn"}}),
            ("message_stop", {}),
        ]
    )
    adapter = AnthropicCompatibleAdapter(base_url="https://x", api_key="k")
    _wire_anthropic_mock(adapter, sse)
    req = LLMRequest(model="m", messages=[Message(role="user", content="hi")])

    chunks: list[LLMChunk] = []
    async for c in adapter.stream(req):
        chunks.append(c)

    assert "hello" in "".join(c.delta for c in chunks)
    tcs = [c for c in chunks if c.tool_call_delta is not None]
    assert tcs == [], "no tool_calls expected for pure text stream"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_stream_malformed_tool_args_yields_parse_error() -> None:
    """follow-up bugs 0710 ②：截断/非法 JSON 不再静默吞成 {}（那会让下游报「缺参数」
    冤枉模型）——改产 _args_parse_error + 原始预览，走既有消费通道让 LLM 自愈重发."""
    sse = _anthropic_sse(
        [
            ("content_block_start", {"index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "f"}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"a":'}}),
            # never finishes the JSON — close block
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "tool_use"}}),
            ("message_stop", {}),
        ]
    )
    adapter = AnthropicCompatibleAdapter(base_url="https://x", api_key="k")
    _wire_anthropic_mock(adapter, sse)
    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])

    chunks: list[LLMChunk] = []
    async for c in adapter.stream(req):
        chunks.append(c)

    tcs = [c.tool_call_delta for c in chunks if c.tool_call_delta is not None]
    assert len(tcs) == 1
    assert tcs[0].name == "f"
    # New contract: preserve parse errors with a marker + raw buffer preview for LLM self-repair and diagnostics.
    assert "_args_parse_error" in tcs[0].arguments
    assert tcs[0].arguments["_raw_args_preview"] == '{"a":'


# ─── OpenAI SSE fixtures ─────────────────────────────────────────────────────


def _openai_sse(frames: list[dict]) -> bytes:
    """Build OpenAI-style SSE byte stream."""
    lines: list[str] = []
    for f in frames:
        lines.append(f"data: {json.dumps(f)}")
        lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_stream_accumulates_tool_call() -> None:
    """OpenAI stream accumulates per-index tool_call deltas + emits at end."""
    sse = _openai_sse(
        [
            {"choices": [{"delta": {"content": "Let me check..."}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_xyz", "function": {"name": "get_weather", "arguments": ""}},
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"location":'}},
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": ' "SF"}'}},
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    adapter = OpenAICompatibleAdapter(base_url="https://x", api_key="k")
    _wire_openai_mock(adapter, sse)
    req = LLMRequest(
        model="gpt-test",
        messages=[Message(role="user", content="weather")],
        tools=[ToolSchema(name="get_weather", description="", input_schema={})],
    )

    chunks: list[LLMChunk] = []
    async for c in adapter.stream(req):
        chunks.append(c)

    text = "".join(c.delta for c in chunks)
    assert "check" in text

    tcs = [c.tool_call_delta for c in chunks if c.tool_call_delta is not None]
    assert len(tcs) == 1
    assert tcs[0].name == "get_weather"
    assert tcs[0].id == "call_xyz"
    assert tcs[0].arguments == {"location": "SF"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_stream_multiple_parallel_tool_calls() -> None:
    """OpenAI parallel tool_calls (different indices) each emit own ToolCall."""
    sse = _openai_sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "c0", "function": {"name": "a", "arguments": "{}"}},
                                {"index": 1, "id": "c1", "function": {"name": "b", "arguments": '{"k":1}'}},
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    adapter = OpenAICompatibleAdapter(base_url="https://x", api_key="k")
    _wire_openai_mock(adapter, sse)
    req = LLMRequest(
        model="m",
        messages=[Message(role="user", content="x")],
        tools=[
            ToolSchema(name="a", description="", input_schema={}),
            ToolSchema(name="b", description="", input_schema={}),
        ],
    )

    chunks: list[LLMChunk] = []
    async for c in adapter.stream(req):
        chunks.append(c)

    tcs = [c.tool_call_delta for c in chunks if c.tool_call_delta is not None]
    assert len(tcs) == 2
    names = sorted(t.name for t in tcs)
    assert names == ["a", "b"]
    by_name = {t.name: t for t in tcs}
    assert by_name["a"].arguments == {}
    assert by_name["b"].arguments == {"k": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_stream_text_only_no_tool_call() -> None:
    """Pure text stream emits no tool_call_delta."""
    sse = _openai_sse(
        [
            {"choices": [{"delta": {"content": "hello world"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
    )
    adapter = OpenAICompatibleAdapter(base_url="https://x", api_key="k")
    _wire_openai_mock(adapter, sse)
    req = LLMRequest(model="m", messages=[Message(role="user", content="hi")])

    chunks: list[LLMChunk] = []
    async for c in adapter.stream(req):
        chunks.append(c)

    assert "hello" in "".join(c.delta for c in chunks)
    tcs = [c for c in chunks if c.tool_call_delta is not None]
    assert tcs == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_stream_malformed_tool_args_yields_parse_error() -> None:
    """follow-up bugs 0710 ②：同 anthropic——malformed/截断 args 产 _args_parse_error
    （含 finish_reason 供判断是否 max-tokens 截断），不再静默 {}。"""
    sse = _openai_sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "c0", "function": {"name": "f", "arguments": '{"a":'}},
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    adapter = OpenAICompatibleAdapter(base_url="https://x", api_key="k")
    _wire_openai_mock(adapter, sse)
    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])

    chunks: list[LLMChunk] = []
    async for c in adapter.stream(req):
        chunks.append(c)

    tcs = [c.tool_call_delta for c in chunks if c.tool_call_delta is not None]
    assert len(tcs) == 1
    assert tcs[0].name == "f"
    # New contract: preserve errors with finish_reason (tool_calls/length distinguishes truncation causes).
    assert "finish_reason=tool_calls" in tcs[0].arguments["_args_parse_error"]
    assert tcs[0].arguments["_raw_args_preview"] == '{"a":'


# ─── 2026-09-25: extra closing brackets after a complete object no longer trigger a full-turn retry ──────────
# A scan of 278 turns on 116 found this shape in all 41 `Extra data` errors, in large subagent completion-tool payloads.

_INTENDED = {"payload": {"title": "复盘", "sources": [{"kind": "computation_sandbox", "query": "q"}]}}
_EXTRA_BRACE = json.dumps(_INTENDED, ensure_ascii=False) + "}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_stream_accepts_extra_closing_brace_after_complete_args() -> None:
    sse = _anthropic_sse(
        [
            ("content_block_start", {"index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "subagent_return"}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": _EXTRA_BRACE[:40]}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": _EXTRA_BRACE[40:]}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "tool_use"}}),
            ("message_stop", {}),
        ]
    )
    adapter = AnthropicCompatibleAdapter(base_url="https://x", api_key="k")
    _wire_anthropic_mock(adapter, sse)
    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])

    tcs = [c.tool_call_delta async for c in adapter.stream(req) if c.tool_call_delta is not None]

    assert len(tcs) == 1
    assert tcs[0].arguments == _INTENDED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_stream_accepts_extra_closing_brace_but_not_two_concatenated_objects() -> None:
    def _frames(args: str) -> bytes:
        return _openai_sse(
            [
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c0", "function": {"name": "subagent_return", "arguments": args}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )

    async def _args(sse: bytes) -> dict:
        adapter = OpenAICompatibleAdapter(base_url="https://x", api_key="k")
        _wire_openai_mock(adapter, sse)
        req = LLMRequest(model="m", messages=[Message(role="user", content="x")])
        tcs = [c.tool_call_delta async for c in adapter.stream(req) if c.tool_call_delta is not None]
        assert len(tcs) == 1
        return tcs[0].arguments

    assert await _args(_frames(_EXTRA_BRACE)) == _INTENDED
    # Two concatenated objects still fail parsing: they may represent two calls, so never silently discard the second.
    assert "_args_parse_error" in await _args(_frames('{"a": 1}{"b": 2}'))


@pytest.mark.unit
def test_openai_non_stream_accepts_extra_closing_brace() -> None:
    adapter = OpenAICompatibleAdapter(base_url="https://x", api_key="k")
    assert adapter._parse_tool_call_arguments(_EXTRA_BRACE, "subagent_return") == _INTENDED


# ─── Sprint chat-redesign-2026-05-21 Phase 1.1 (Spike 1) ─────────────────────
# `tool_call_partial` incremental emit — drives `lead_thinking_writing` step
# UX on FE without waiting for the full tool_use block to close.


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_stream_emits_partial_per_input_json_delta() -> None:
    """Each Anthropic `input_json_delta` chunk yields a tool_call_partial."""
    sse = _anthropic_sse(
        [
            ("content_block_start", {"index": 0, "content_block": {"type": "tool_use", "id": "toolu_xyz", "name": "bi_query"}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"sql":'}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": ' "SELECT'}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": ' 1"}'}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "tool_use"}}),
            ("message_stop", {}),
        ]
    )
    adapter = AnthropicCompatibleAdapter(base_url="https://x", api_key="k")
    _wire_anthropic_mock(adapter, sse)
    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])

    chunks: list[LLMChunk] = []
    async for c in adapter.stream(req):
        chunks.append(c)

    partials = [c.tool_call_partial for c in chunks if c.tool_call_partial is not None]
    assert len(partials) == 3, f"expected 3 partials, got {len(partials)}: {partials}"
    # All carry the same tool_name + tool_call_id
    assert {p["tool_name"] for p in partials} == {"bi_query"}
    assert {p["tool_call_id"] for p in partials} == {"toolu_xyz"}
    # Concatenated partials reconstruct the full args JSON
    concat = "".join(p["partial_args_json"] for p in partials)
    assert concat == '{"sql": "SELECT 1"}'
    # Final tool_call_delta still emitted at content_block_stop
    finals = [c.tool_call_delta for c in chunks if c.tool_call_delta is not None]
    assert len(finals) == 1
    assert finals[0].name == "bi_query"
    assert finals[0].arguments == {"sql": "SELECT 1"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anthropic_stream_text_only_no_partial() -> None:
    """Pure text stream emits no tool_call_partial."""
    sse = _anthropic_sse(
        [
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "hi"}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "end_turn"}}),
            ("message_stop", {}),
        ]
    )
    adapter = AnthropicCompatibleAdapter(base_url="https://x", api_key="k")
    _wire_anthropic_mock(adapter, sse)
    req = LLMRequest(model="m", messages=[Message(role="user", content="hi")])

    chunks: list[LLMChunk] = []
    async for c in adapter.stream(req):
        chunks.append(c)

    partials = [c for c in chunks if c.tool_call_partial is not None]
    assert partials == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_stream_emits_partial_per_arguments_chunk() -> None:
    """Each OpenAI arguments chunk yields a tool_call_partial (after name+id known)."""
    sse = _openai_sse(
        [
            # First chunk: id + name (no arguments yet)
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_abc", "function": {"name": "python_sandbox", "arguments": ""}},
                            ]
                        }
                    }
                ]
            },
            # 3 arguments chunks
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"code":'}},
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": ' "import'}},
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": ' os"}'}},
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    adapter = OpenAICompatibleAdapter(base_url="https://x", api_key="k")
    _wire_openai_mock(adapter, sse)
    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])

    chunks: list[LLMChunk] = []
    async for c in adapter.stream(req):
        chunks.append(c)

    partials = [c.tool_call_partial for c in chunks if c.tool_call_partial is not None]
    assert len(partials) == 3, f"expected 3 partials, got {len(partials)}: {partials}"
    assert {p["tool_name"] for p in partials} == {"python_sandbox"}
    assert {p["tool_call_id"] for p in partials} == {"call_abc"}
    concat = "".join(p["partial_args_json"] for p in partials)
    assert concat == '{"code": "import os"}'
    # Final tool_call_delta still emitted at stream end
    finals = [c.tool_call_delta for c in chunks if c.tool_call_delta is not None]
    assert len(finals) == 1
    assert finals[0].name == "python_sandbox"
    assert finals[0].arguments == {"code": "import os"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_stream_partial_skipped_when_name_not_yet_known() -> None:
    """Defensive: if arguments stream before name (rare), partial is skipped to avoid
    emitting a partial with empty tool_name (which would confuse downstream gate).
    """
    sse = _openai_sse(
        [
            # Arguments arrive first WITHOUT name (rare OpenAI behavior)
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_x", "function": {"arguments": '{"a":'}},
                            ]
                        }
                    }
                ]
            },
            # Then name arrives + more arguments
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"name": "f", "arguments": " 1}"}},
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    adapter = OpenAICompatibleAdapter(base_url="https://x", api_key="k")
    _wire_openai_mock(adapter, sse)
    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])

    chunks: list[LLMChunk] = []
    async for c in adapter.stream(req):
        chunks.append(c)

    partials = [c.tool_call_partial for c in chunks if c.tool_call_partial is not None]
    # Only the 2nd args chunk (after name was set) emits partial
    assert len(partials) == 1
    assert partials[0]["tool_name"] == "f"
    assert partials[0]["partial_args_json"] == " 1}"
    # Final tool_call_delta has the full args (both arg chunks accumulated)
    finals = [c.tool_call_delta for c in chunks if c.tool_call_delta is not None]
    assert len(finals) == 1
    assert finals[0].arguments == {"a": 1}
