"""Count upstream native blocks independently of parsed calls, without content."""

import json

import httpx2
import pytest
from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter
from bowhead_router.types import LLMRequest, Message

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize(
    "reason",
    ["end_turn", "stop_sequence", "max_tokens", "tool_use", "pause_turn", "refusal", "model_context_window_exceeded", "private-provider-value", None],
)
async def test_raw_finish_reason_is_bounded_and_known_stops_are_normalized(mode, reason):
    adapter = AnthropicCompatibleAdapter(base_url="https://example.com", api_key="private-test-key")
    request = LLMRequest(model="synthetic", messages=[Message(role="user", content="Synthetic request.")])
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "private-response"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": reason}},
        {"type": "message_stop"},
    ]
    wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    await adapter._http.aclose()
    adapter._http = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda request: httpx2.Response(200, text=wire)))
    try:
        if mode == "invoke":
            response = adapter._parse_response({"content": [{"type": "text", "text": "private-response"}], "stop_reason": reason}, request)
        else:
            chunks = [chunk async for chunk in adapter.stream(request)]
            response = chunks[-1]
        fields = response.diagnostics.trace_fields()
        assert fields["mode"] == mode
        assert fields["upstream_tool_call_count"] == 0
        assert fields["upstream_finish_reason"] == ("unknown" if reason == "private-provider-value" else reason)
        expected = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}.get(reason, reason)
        assert response.finish_reason == (expected if reason else "stop" if mode == "invoke" else None)
        assert "private-" not in json.dumps(fields)
    finally:
        await adapter.close()


async def test_stream_counts_unclosed_native_blocks_without_inventing_calls():
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "first", "name": "echo"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"private":"argument"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "unclosed", "name": "echo"}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]
    wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    adapter = AnthropicCompatibleAdapter(base_url="https://example.com", api_key="private-test-key")
    await adapter._http.aclose()
    adapter._http = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda request: httpx2.Response(200, text=wire)))
    try:
        chunks = [chunk async for chunk in adapter.stream(LLMRequest(model="synthetic", messages=[]))]
        calls = [chunk.tool_call_delta for chunk in chunks if chunk.tool_call_delta is not None]
        assert len(calls) == 1 and calls[0].id == "first" and calls[0].arguments == {"private": "argument"}
        assert chunks[-1].diagnostics.trace_fields()["upstream_tool_call_count"] == 2
        assert "argument" not in json.dumps(chunks[-1].diagnostics.trace_fields())
    finally:
        await adapter.close()
