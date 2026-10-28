"""Preserve bounded structured provider errors through the diagnostic boundary.

OpenAI-compatible type, code, and param fields have a restricted identifier
grammar. Free-form messages may contain links or credentials and cannot pass
this boundary. Invalid identifier values become None, never fabricated evidence."""

from __future__ import annotations

import pytest
import respx
from bowhead_router.errors import (
    ProviderFailureKind,
    safe_upstream_param,
    upstream_error_fields_from_payload,
)


@pytest.mark.unit
def test_openai_style_rejection_names_the_offending_parameter() -> None:
    """真实形状：拒 `temperature` 的 400 正文（照 OpenAI 兼容网关的写法）。"""

    fields = upstream_error_fields_from_payload(
        {
            "error": {
                "message": "Unsupported value: 'temperature' does not support 0.7 with this model. Only the default (1) value is supported.",
                "type": "invalid_request_error",
                "param": "temperature",
                "code": "unsupported_value",
            }
        },
        headers={"x-request-id": "req_abc123"},
    )

    assert fields["upstream_error_type"] == "invalid_request_error"
    assert fields["upstream_error_code"] == "unsupported_value"
    assert fields["upstream_error_param"] == "temperature"
    assert fields["upstream_request_id"] == "req_abc123"
    # Prose still goes only into log fields, with credentials and control characters removed.
    assert fields["upstream_message"].startswith("Unsupported value:")


@pytest.mark.unit
def test_missing_param_and_code_stay_absent_not_invented() -> None:
    """负对照：provider 没指认就是没指认，不得编造。

    anthropic 兼容的错误体没有 `param`/`code`，此时两者必须是 None ——
    诊断里因此不会出现 `upstream_param=` 这一段（「没这个字段」＝「没指认」）。
    """

    fields = upstream_error_fields_from_payload(
        {"type": "error", "error": {"type": "invalid_request_error", "message": "bad request"}},
    )

    assert fields["upstream_error_type"] == "invalid_request_error"
    assert fields["upstream_error_code"] is None
    assert fields["upstream_error_param"] is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example.com/leak",  # Reject the entire URL at the diagnostics boundary.
        "<script>alert(1)</script>",  # markup
        "/etc/passwd",  # Absolute path.
        "temperature or top_p",  # Prose with spaces, rather than a parameter name.
        "x" * 200,  # Too long.
        'param"; DROP TABLE',  # Quotes / injection-shaped input.
        "",
        None,
        123,
    ],
)
def test_param_grammar_rejects_anything_that_is_not_a_parameter_name(hostile: object) -> None:
    """文法是白名单：不像参数名的一律 None。

    这条守的是「provider 说什么我们就外发什么」这个诱惑 —— 上游正文是不可信输入，
    它同时决定了下游诊断能不能过界（见 published_diagnostics 的内容检查）。
    """

    assert safe_upstream_param(hostile) is None


@pytest.mark.unit
def test_param_grammar_accepts_indexed_paths() -> None:
    """正对照：provider 常用下标指认嵌套字段，这类要留住，否则等于白提取。"""

    assert safe_upstream_param("messages[0].content") == "messages[0].content"
    assert safe_upstream_param("max_tokens") == "max_tokens"
    assert safe_upstream_param("parallel_tool_calls") == "parallel_tool_calls"


@pytest.mark.unit
def test_provider_call_error_carries_the_identifiers_through() -> None:
    """adapter 把整份 fields 直接 splat 进异常构造器 —— 键加了、参数没加就会 TypeError。

    这条同时守住那个装配点：三处 adapter（openai invoke / openai stream / anthropic）
    都是 `**upstream_error_fields(resp)`。
    """

    from bowhead_router.errors import ProviderCallError, target_failure

    exc = ProviderCallError(
        kind=ProviderFailureKind.REQUEST_REJECTED,
        status_code=400,
        provider_family="openai_compatible",
        **upstream_error_fields_from_payload(
            {"error": {"message": "nope", "type": "invalid_request_error", "param": "max_tokens", "code": "unknown_parameter"}},
        ),
    )
    failure = target_failure(
        exc,
        adapter=object(),
        provider="p",
        model="m",
        mode="invoke",
        target_index=0,
        attempt=1,
    )

    assert failure.upstream_error_param == "max_tokens"
    assert failure.upstream_error_code == "unknown_parameter"


@pytest.mark.unit
@respx.mock
async def test_real_adapter_400_reaches_the_projection_with_the_param_name() -> None:
    """Carry a mocked non-streaming 400 parameter through adapter, exception, and target failure projection."""
    from bowhead_router.errors import target_failure

    from bowhead_router import LLMRequest, Message, OpenAICompatibleAdapter

    base = "https://api.marketplace.example.com/v1"
    respx.post(f"{base}/chat/completions").respond(
        400,
        json={
            "error": {
                "message": "Unsupported value: 'temperature' does not support 0.7 with this model.",
                "type": "invalid_request_error",
                "param": "temperature",
                "code": "unsupported_value",
            }
        },
    )
    adapter = OpenAICompatibleAdapter(base_url=base, api_key="sk-test", provider_name="acme-genai")
    try:
        with pytest.raises(Exception) as caught:  # noqa: PT011 — The criterion is the projected facts.
            await adapter.invoke(
                LLMRequest(model="openai_gpt56_luna", messages=[Message(role="user", content="hi")]),
            )
    finally:
        await adapter.close()

    failure = target_failure(
        caught.value,
        adapter=adapter,
        provider="acme-genai",
        model="openai_gpt56_luna",
        mode="invoke",
        target_index=0,
        attempt=1,
    )

    assert failure.kind is ProviderFailureKind.REQUEST_REJECTED
    assert failure.status_code == 400
    assert failure.upstream_error_param == "temperature"
    assert failure.upstream_error_code == "unsupported_value"
    assert failure.upstream_error_type == "invalid_request_error"
