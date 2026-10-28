"""M3.1 · Anthropic adapter contract 测试.

只验证 request body 构造正确性, 不打真 API. 真 API 调用 e2e 走可选 dogfood 测试 (跟
test_openai_compat.py 同 pattern, 需要 ANTHROPIC_API_KEY 才跑).

设计文档: docs/M157-routing-user-pref-design.md §4.4
"""

from __future__ import annotations

import pytest
import respx
from bowhead_router.adapters.anthropic_compat import (
    AnthropicCompatError,
    AnthropicCompatibleAdapter,
)
from bowhead_router.errors import ProviderFailureKind
from bowhead_router.types import ImageRef, LLMRequest, Message, ToolCall, ToolSchema


@pytest.fixture
def adapter() -> AnthropicCompatibleAdapter:
    return AnthropicCompatibleAdapter(
        base_url="https://api.anthropic.com",
        api_key="sk-test",
    )


def test_build_body_minimum(adapter: AnthropicCompatibleAdapter):
    req = LLMRequest(
        model="claude-opus-4-7",
        messages=[Message(role="user", content="hello")],
    )
    body = adapter._build_body(req, stream=False)
    assert body["model"] == "claude-opus-4-7"
    assert body["max_tokens"] == 32000
    assert isinstance(body["messages"], list)
    assert "stream" not in body


def test_build_body_with_system(adapter: AnthropicCompatibleAdapter):
    req = LLMRequest(
        model="claude-sonnet-4-6",
        messages=[Message(role="user", content="hi")],
        system="You are a data analyst.",
    )
    body = adapter._build_body(req, stream=False)
    assert body["system"] == "You are a data analyst."
    for m in body["messages"]:
        assert m["role"] != "system"


def test_build_body_with_temperature_top_p_stop(adapter: AnthropicCompatibleAdapter):
    req = LLMRequest(
        model="claude-haiku-4-5",
        messages=[Message(role="user", content="x")],
        temperature=0.3,
        top_p=0.9,
        stop=["END", "STOP"],
    )
    body = adapter._build_body(req, stream=False)
    assert body["temperature"] == 0.3
    assert body["top_p"] == 0.9
    assert body["stop_sequences"] == ["END", "STOP"]


def test_build_body_with_tools(adapter: AnthropicCompatibleAdapter):
    req = LLMRequest(
        model="claude-opus-4-7",
        messages=[Message(role="user", content="search GMV")],
        tools=[
            ToolSchema(
                name="bi_ask_question",
                description="Query BI for a metric",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            ),
        ],
    )
    body = adapter._build_body(req, stream=False)
    assert "tools" in body
    assert len(body["tools"]) == 1
    assert body["tools"][0]["name"] == "bi_ask_question"
    assert body["tool_choice"] == {"type": "auto"}


def test_build_body_stream_flag(adapter: AnthropicCompatibleAdapter):
    req = LLMRequest(
        model="claude-opus-4-7",
        messages=[Message(role="user", content="x")],
    )
    body = adapter._build_body(req, stream=True)
    assert body["stream"] is True


@pytest.mark.unit
@respx.mock
async def test_stream_error_event_preserves_upstream_fields(
    adapter: AnthropicCompatibleAdapter,
):
    respx.post("https://api.anthropic.com/v1/messages").respond(
        200,
        text=('event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded Authorization: Bearer raw-secret"}}\n\n'),
        headers={
            "content-type": "text/event-stream",
            "request-id": "request-456",
        },
    )

    with pytest.raises(AnthropicCompatError) as exc:
        async for _ in adapter.stream(
            LLMRequest(
                model="claude-opus-4-7",
                messages=[Message(role="user", content="x")],
            )
        ):
            pass

    assert exc.value.kind is ProviderFailureKind.UPSTREAM
    assert exc.value.retryable is True
    assert exc.value.status_code is None
    assert exc.value.upstream_error_type == "overloaded_error"
    assert exc.value.upstream_request_id == "request-456"
    assert "Overloaded" in (exc.value.upstream_message or "")
    assert "raw-secret" not in (exc.value.upstream_message or "")


def test_build_body_extended_thinking_enabled(adapter: AnthropicCompatibleAdapter):
    req = LLMRequest(
        model="claude-opus-4-7",
        messages=[Message(role="user", content="x")],
        extended_thinking=True,
        thinking_budget_tokens=4096,
    )
    body = adapter._build_body(req, stream=False)
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}


def test_build_body_max_tokens_explicit(adapter: AnthropicCompatibleAdapter):
    req = LLMRequest(
        model="claude-opus-4-7",
        messages=[Message(role="user", content="x")],
        max_output_tokens=8192,
    )
    body = adapter._build_body(req, stream=False)
    assert body["max_tokens"] == 8192


def test_convert_tool_keeps_input_schema():
    tool = ToolSchema(
        name="bi_attribute",
        description="Attribution analysis",
        input_schema={
            "type": "object",
            "properties": {"question": {"type": "string"}},
        },
    )
    out = AnthropicCompatibleAdapter(base_url="http://x", api_key="x")._convert_tool(tool)
    assert out["name"] == "bi_attribute"
    assert out["description"] == "Attribution analysis"
    assert "input_schema" in out
    assert out["input_schema"]["type"] == "object"


def test_post_init_sets_x_api_key_and_authorization(adapter: AnthropicCompatibleAdapter):
    """两个 auth 头都带, 兼容原生 + 代理."""
    headers = adapter._http.headers
    assert headers["x-api-key"] == "sk-test"
    assert headers["Authorization"] == "Bearer sk-test"
    assert headers["anthropic-version"] == "2023-06-01"


# ── perf WP-1: cache_read parsing (automatic cache hits on deepseek/doubao anthropic-compat) ──


def _req() -> LLMRequest:
    return LLMRequest(model="deepseek-v4-pro", messages=[Message(role="user", content="hi")])


def test_parse_response_captures_cache_read(adapter: AnthropicCompatibleAdapter):
    """命中缓存时 cache_read_input_tokens → usage.cached_tokens；input_tokens 不重复计."""
    data = {
        "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 83,  # On a cache hit, this already counts only new, uncached tokens.
            "output_tokens": 16,
            "cache_read_input_tokens": 6400,
            "cache_creation_input_tokens": 0,
        },
    }
    resp = adapter._parse_response(data, _req())
    assert resp.usage.cached_tokens == 6400
    assert resp.usage.input_tokens == 83  # Do not add cached tokens to input again.
    assert resp.usage.output_tokens == 16


def test_parse_response_no_cache_fields_defaults_zero(adapter: AnthropicCompatibleAdapter):
    """无缓存字段（cold call / 不支持缓存的端点）→ cached_tokens=0，回归不破."""
    data = {
        "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 6504, "output_tokens": 16},
    }
    resp = adapter._parse_response(data, _req())
    assert resp.usage.cached_tokens == 0
    assert resp.usage.input_tokens == 6504


# ─────────────────────────────────────────────────────────────
# Normalize consecutive user messages (safety prerequisite for turn-latency A).
#
# 🔍 Handle this here because Anthropic folds Bowhead's `tool` role into user, so
# a user message after a batch of tool results becomes two consecutive user messages. The official Messages API
# merges them into one turn, **but compatible endpoints do not guarantee this**. Production on 116 uses a compatible endpoint
# (`provider=deepseek_anthropic` / `deepseek-v4-flash`); if it enforces alternation,
# every call in the turn returns 400.
# ⚠ Both paths producing this shape are in **our own assembly**, independent of the provider:
#   ① hard-bail soft injection (`subagent.py` appends a user message after tool results), which **already exists**;
#   ② `BOWHEAD_REFS_IN_MESSAGES=1` (the refs list is appended to messages).
# ─────────────────────────────────────────────────────────────


def _roles(out: list[dict]) -> list[str]:
    return [m["role"] for m in out]


def test_tool_results_then_user_merge_into_one_turn():
    """★ 主判据：`[user, assistant(tool_use), tool, user]` 出线必须严格交替。

    突变：把 `_emit_user` 换回无条件 `out.append(...)` ⇒ 本条红（roles 出现 user,user）。
    """
    msgs = [
        Message(role="user", content="任务"),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="kb_search", arguments={})]),
        Message(role="tool", content="命中 3 条", tool_call_id="c1"),
        Message(role="user", content="<available_refs>…</available_refs>"),
    ]
    out = AnthropicCompatibleAdapter._convert_messages(msgs)

    assert _roles(out) == ["user", "assistant", "user"], f"出线角色未严格交替：{_roles(out)}"
    # Merging must preserve content: tool_result and the following user message must share the same turn.
    last = out[-1]["content"]
    assert isinstance(last, list)
    kinds = [b["type"] for b in last]
    assert kinds == ["tool_result", "text"], kinds
    assert last[0]["tool_use_id"] == "c1"
    assert last[1]["text"] == "<available_refs>…</available_refs>"


def test_two_plain_user_messages_merge():
    """首圈形态：`[user 任务, user refs]`（还没有任何 tool 结果）。"""
    out = AnthropicCompatibleAdapter._convert_messages(
        [
            Message(role="user", content="任务"),
            Message(role="user", content="refs"),
        ]
    )
    assert _roles(out) == ["user"]
    assert [b["text"] for b in out[-1]["content"]] == ["任务", "refs"]


def test_alternating_conversation_is_untouched():
    """★正对照：本来就交替的对话，合并逻辑**一个字节都不能动**。

    没有它，「严格交替」也可以靠把所有消息压成一条来满足。
    """
    msgs = [
        Message(role="user", content="Q1"),
        Message(role="assistant", content="A1"),
        Message(role="user", content="Q2"),
    ]
    out = AnthropicCompatibleAdapter._convert_messages(msgs)
    # ⚠ Assistant output was already a block list before this change; verify that user messages remain unchanged:
    #   preserve their count and order, and **do not convert plain string content to block lists**.
    assert out == [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": [{"type": "text", "text": "A1"}]},
        {"role": "user", "content": "Q2"},
    ]


def test_merge_preserves_images():
    """带图的 user 与随后的纯文本 user 合并后，image block 必须还在。"""
    out = AnthropicCompatibleAdapter._convert_messages(
        [
            Message(role="user", content="看图", images=[ImageRef(mime="image/png", base64="AAA")]),
            Message(role="user", content="refs"),
        ]
    )
    assert _roles(out) == ["user"]
    kinds = [b["type"] for b in out[-1]["content"]]
    assert kinds == ["text", "image", "text"], kinds


def test_build_body_tool_choice_none_keeps_tools_but_forbids_calls(adapter: AnthropicCompatibleAdapter):
    """The wrap-up call at a run limit keeps tool history valid but must not call tools."""
    req = LLMRequest(
        model="claude-opus-4-7",
        messages=[Message(role="user", content="summarize")],
        tools=[ToolSchema(name="bi_ask_question", description="Query BI", input_schema={"type": "object"})],
        tool_choice="none",
    )
    body = adapter._build_body(req, stream=False)
    assert body["tools"][0]["name"] == "bi_ask_question"
    assert body["tool_choice"] == {"type": "none"}
