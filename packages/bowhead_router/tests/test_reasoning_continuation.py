"""Thinking-mode tool continuation: capture, provenance-bound emission and record compatibility."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

import httpx2
import pytest
from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter
from bowhead_router.reasoning import message_record, record_reasoning, valid_record_keys
from bowhead_router.types import LLMChunk, LLMRequest, Message, ToolCall, ToolSchema

PROVIDER = "deepseek_anthropic"
MODEL = "deepseek-flash"
THINKING = {"type": "thinking", "thinking": "Compare both brands first.", "signature": "sig-1"}
REDACTED = {"type": "redacted_thinking", "data": "opaque"}


def _adapter() -> AnthropicCompatibleAdapter:
    return AnthropicCompatibleAdapter(base_url="https://x", api_key="k", provider_name=PROVIDER)


def _envelope(*blocks: dict, provider: str = PROVIDER, model: str = MODEL) -> dict:
    return {"version": 1, "protocol": "anthropic_messages", "provider": provider, "model": model, "blocks": list(blocks)}


def _loop(reasoning: dict | None) -> list[Message]:
    call = ToolCall(id="toolu_1", name="python_exec", arguments={"code": "print(1)"})
    return [
        Message(role="user", content="Fill the deck."),
        Message(role="assistant", content="Computing.", tool_calls=[call], provider_reasoning=reasoning),
        Message(role="tool", content='{"ok":true}', name="python_exec", tool_call_id="toolu_1"),
    ]


def _request(messages: list[Message], *, thinking: bool = True) -> LLMRequest:
    return LLMRequest(
        model=MODEL,
        messages=messages,
        tools=[ToolSchema(name="python_exec", description="", input_schema={"type": "object"})],
        extended_thinking=thinking,
    )


def test_invoke_response_keeps_exact_blocks_with_provenance() -> None:
    adapter = _adapter()
    data = {
        "content": [
            THINKING,
            REDACTED,
            {"type": "text", "text": "Computing."},
            {"type": "tool_use", "id": "toolu_1", "name": "python_exec", "input": {}},
        ],
        "stop_reason": "tool_use",
        "model": "deepseek-v4-flash",
    }
    response = adapter._parse_response(data, _request(_loop(None)[:1]))
    assert response.thinking == "Compare both brands first."
    # Provenance uses the configured request model, which the next request must match.
    assert response.provider_reasoning == _envelope(THINKING, REDACTED)
    plain = adapter._parse_response({"content": [{"type": "text", "text": "Done."}], "stop_reason": "end_turn"}, _request(_loop(None)[:1]))
    assert plain.provider_reasoning is None


@pytest.mark.asyncio
async def test_stream_assembles_thinking_signature_and_redacted_blocks() -> None:
    events = [
        ("message_start", {"type": "message_start", "message": {"id": "msg_1"}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "thinking_delta", "thinking": "Compare both "}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "thinking_delta", "thinking": "brands first."}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "signature_delta", "signature": "sig-1"}}),
        ("content_block_stop", {"index": 0}),
        ("content_block_start", {"index": 1, "content_block": REDACTED}),
        ("content_block_stop", {"index": 1}),
        ("content_block_start", {"index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "python_exec"}}),
        ("content_block_delta", {"index": 2, "delta": {"type": "input_json_delta", "partial_json": "{}"}}),
        ("content_block_stop", {"index": 2}),
        ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"input_tokens": 3, "output_tokens": 4}}),
        ("message_stop", {}),
    ]
    sse = "".join(f"event: {name}\ndata: {json.dumps({'type': name, **payload})}\n\n" for name, payload in events).encode()
    adapter = _adapter()
    adapter._http = httpx2.AsyncClient(
        base_url="https://x",
        transport=httpx2.MockTransport(lambda _req: httpx2.Response(200, content=sse, headers={"content-type": "text/event-stream"})),
    )
    chunks: list[LLMChunk] = [chunk async for chunk in adapter.stream(_request(_loop(None)[:1]))]
    finals = [chunk.provider_reasoning for chunk in chunks if chunk.provider_reasoning is not None]
    assert finals == [_envelope(THINKING, REDACTED)]
    assert chunks[-1].provider_reasoning == finals[0]


def test_matching_blocks_precede_text_and_tool_use() -> None:
    body = _adapter()._build_body(_request(_loop(_envelope(THINKING, REDACTED))), stream=False)
    assert body["thinking"]["type"] == "enabled"
    assistant = body["messages"][1]
    assert [block["type"] for block in assistant["content"]] == ["thinking", "redacted_thinking", "text", "tool_use"]
    assert assistant["content"][:2] == [THINKING, REDACTED]


@pytest.mark.parametrize(
    "reasoning",
    [None, _envelope(THINKING, provider="aliyun_qwen"), _envelope(THINKING, model="deepseek-v4-pro")],
    ids=["missing", "other_connection", "other_model"],
)
def test_unmatched_tool_loop_continues_without_thinking(reasoning: dict | None) -> None:
    body = _adapter()._build_body(_request(_loop(reasoning)), stream=False)
    assert body["thinking"] == {"type": "disabled"}
    assert all(block["type"] not in {"thinking", "redacted_thinking"} for block in body["messages"][1]["content"])


def test_closed_earlier_turns_do_not_block_thinking() -> None:
    earlier = _loop(None)
    messages = [*earlier, Message(role="assistant", content="Filled."), Message(role="user", content="Now the summary page.")]
    body = _adapter()._build_body(_request(messages), stream=False)
    assert body["thinking"]["type"] == "enabled"


def test_thinking_off_request_is_unchanged_by_stored_reasoning() -> None:
    adapter = _adapter()
    with_reasoning = adapter._build_body(_request(_loop(_envelope(THINKING)), thinking=False), stream=False)
    without = adapter._build_body(_request(_loop(None), thinking=False), stream=False)
    assert with_reasoning == without
    assert with_reasoning["thinking"] == {"type": "disabled"}


def test_absent_reasoning_keeps_existing_record_and_digest() -> None:
    message = _loop(None)[1]
    legacy = {key: value for key, value in asdict(message).items() if key != "provider_reasoning"}
    record = message_record(message)
    assert record == legacy and valid_record_keys(record)

    def digest(value: object) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    assert digest(record) == digest(legacy)
    kept = message_record(_loop(_envelope(THINKING))[1])
    assert valid_record_keys(kept) and record_reasoning(kept) == _envelope(THINKING)
    assert "provider_reasoning" not in message_record(_loop(_envelope(THINKING))[1], keep_reasoning=False)
    assert record_reasoning({**record, "provider_reasoning": None}) is None
    with pytest.raises(ValueError):
        record_reasoning({**record, "provider_reasoning": {"blocks": []}})
