"""Empty output keeps useful evidence without exposing credentials or changing verification."""

from __future__ import annotations

import json

import bowhead_router.probe as probe_module
import httpx2
import pytest
from bowhead_router.probe import CompatibleProbeRegistry
from bowhead_router.probe_diagnostics import empty_completion_diagnostics


@pytest.mark.unit
@pytest.mark.parametrize("stream", [False, True], ids=["anthropic-json", "openai-sse"])
async def test_empty_output_collects_final_request_and_response(monkeypatch, stream: bool) -> None:
    payload = {
        "model": "returned-model",
        "content": [{"type": "thinking", "thinking": "Still considering"}],
        "stop_reason": "max_tokens",
        "usage": {"input_tokens": 10, "output_tokens": 8},
        "api_key": "upstream-private-key",
        "debug": "provider-private-key query-private-key custom-value cookie-value",
    }
    if stream:
        frames = [
            {"model": "returned-model", "choices": [{"delta": {"reasoning_content": "Still considering"}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}], "usage": {"completion_tokens": 8}},
        ]
        raw = "event: message\n" + "\n".join("data: " + json.dumps(frame) for frame in frames) + "\ndata: [DONE]\n"
    else:
        raw = json.dumps(payload)

    def factory(**kwargs):
        def handler(request: httpx2.Request) -> httpx2.Response:
            assert json.loads(request.content)["model"] == "configured-model"
            return httpx2.Response(200, text=raw)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(probe_module, "build_guarded_async_client", factory)
    registry = CompatibleProbeRegistry()
    plan = registry.plan(
        {
            "type": "openai_compatible" if stream else "anthropic_compatible",
            "base_url": "https://models.example/v1?sig=query-private-key",
            "api_key": "provider-private-key",
            "default_headers": {"X-Custom": "Custom custom-value", "Cookie": "session=cookie-value"},
        },
        provider_id="llmp_1",
        provider_display_name="Test",
        model_id="configured-model",
        capability="chat",
    )
    empty_raw = raw
    result = await registry.execute(plan, timeout=10)
    assert result.code == "empty_completion"
    assert result.safe_http_status == 200
    details = result.diagnostics
    assert details is not None
    assert details["request"]["model"] == "configured-model"
    assert details["response_summary"]["model"] == "returned-model"
    assert details["response_summary"]["stop_reason"] == ("length" if stream else "max_tokens")
    assert details["response_summary"]["usage"] == ({"completion_tokens": 8} if stream else payload["usage"])
    assert details["response_summary"]["content_types"] == (["reasoning"] if stream else ["thinking"])
    assert "Still considering" in details["response_body"]
    assert not details["response_truncated"]
    if stream:
        assert "event: message\ndata:" in details["response_body"]
        assert "data: [DONE]" in details["response_body"]
    else:
        assert details["request"]["max_tokens"] == 8
        assert details["request"]["thinking"] == {"type": "disabled"}
        serialized = json.dumps(details)
        for secret in ("upstream-private-key", "provider-private-key", "query-private-key", "custom-value", "cookie-value"):
            assert secret not in serialized

    raw = (
        'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\ndata: [DONE]\n'
        if stream
        else '{"content":[{"type":"text","text":"OK"}]}'
    )
    successful = await registry.execute(plan, timeout=10)
    assert successful.code == "ok"
    assert successful.diagnostics is None
    raw = "not JSON"
    invalid = await registry.execute(plan, timeout=10)
    assert invalid.code == "invalid_response"
    assert invalid.diagnostics is None
    import bowhead_router.probe_diagnostics as diagnostics_module

    raw = empty_raw
    monkeypatch.setattr(diagnostics_module, "empty_completion_diagnostics", lambda **kwargs: None)
    without_details = await registry.execute(plan, timeout=10)
    assert without_details.code == "empty_completion"
    assert without_details.diagnostics is None


@pytest.mark.unit
def test_nested_credentials_and_echoed_request_secrets_are_screened_before_truncation() -> None:
    payload = {
        "content": [],
        "metadata": {
            "Authorization": "Bearer private",
            "Set-Cookie": "session=private",
            "accessToken": "private-token",
            "client_secret": "private-secret",
        },
        "echo": "known-request-secret",
        "tail": "汉" * 30000,
    }
    details = empty_completion_diagnostics(
        body={"model": "test", "api_key": "known-request-secret"},
        adapter_type="anthropic_compatible",
        raw=json.dumps(payload).encode(),
        secret_values=(),
    )
    assert details is not None
    assert details["response_truncated"]
    assert len(details["response_body"].encode("utf-8")) <= 64 * 1024
    assert "[REDACTED]" in details["response_body"]
    assert "private" not in details["response_body"]
    assert "known-request-secret" not in details["response_body"]
    assert "api_key" not in details["request"]


@pytest.mark.unit
@pytest.mark.parametrize("raw", [b"not-json", b"\xff", b"{}" * 2000])
def test_diagnostic_collection_failure_is_optional(raw: bytes) -> None:
    assert empty_completion_diagnostics(body={}, adapter_type="anthropic_compatible", raw=raw, secret_values=()) is None


@pytest.mark.unit
def test_responses_reasoning_only_keeps_incomplete_reason_and_usage() -> None:
    payload = {
        "model": "reasoner",
        "output": [{"type": "reasoning", "summary": []}],
        "incomplete_details": {"reason": "max_output_tokens"},
        "usage": {"output_tokens": 128, "output_tokens_details": {"reasoning_tokens": 128}},
    }
    details = empty_completion_diagnostics(
        body={"max_output_tokens": 128, "reasoning": {"effort": "high"}},
        adapter_type="openai_compatible",
        raw=json.dumps(payload).encode(),
        secret_values=(),
    )
    assert details is not None
    assert details["response_summary"]["stop_reason"] == "max_output_tokens"
    assert details["response_summary"]["content_types"] == ["reasoning"]
    assert details["response_summary"]["usage"] == payload["usage"]


@pytest.mark.unit
@pytest.mark.parametrize("payload", [None, False, 0, "unexpected response"])
def test_valid_json_scalars_remain_visible_for_response_mapping_diagnosis(payload: object) -> None:
    details = empty_completion_diagnostics(body={}, adapter_type="anthropic_compatible", raw=json.dumps(payload).encode(), secret_values=())
    assert details is not None
    assert json.loads(details["response_body"]) == payload
    assert details["response_summary"]["stop_reason"] is None


@pytest.mark.unit
def test_request_cookie_values_and_custom_auth_prefixes_are_hidden_when_echoed() -> None:
    details = empty_completion_diagnostics(
        body={},
        adapter_type="custom_http",
        raw=b'{"debug": "cookie-value custom-value credential%2F%2B%3D%3D", "content": []}',
        secret_values=("session=cookie-value; preference=private-value", "Custom custom-value", "credential/+=="),
    )
    assert details is not None
    assert "cookie-value" not in details["response_body"]
    assert "custom-value" not in details["response_body"]
    assert "credential%2F%2B%3D%3D" not in details["response_body"]


@pytest.mark.unit
def test_empty_text_blocks_are_not_reported_as_returned_text() -> None:
    raw = b'{"content": [{"type": "text", "text": "   "}], "stop_reason": "end_turn"}'
    details = empty_completion_diagnostics(body={}, adapter_type="anthropic_compatible", raw=raw, secret_values=())
    assert details is not None
    assert details["response_summary"]["content_types"] == []
    assert details["response_summary"]["stop_reason"] == "end_turn"


@pytest.mark.unit
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse-later-credential-frame"])
def test_response_credentials_are_hidden_in_other_fields_and_earlier_frames(stream: bool) -> None:
    frames = [
        {"debug": "remote-private-value cookie-private-value remote%2Fcredential%3D private-array-value opaque-jwt-value", "padding": "x" * 70000},
        {
            "content": [],
            "jwt": "opaque-jwt-value",
            "credentials": {
                "api_key": "remote-private-value",
                "Cookie": "sid=cookie-private-value",
                "access_token": "remote/credential=",
                "extra": ["private-array-value"],
            },
        },
    ]
    raw = (
        ("\n".join("data: " + json.dumps(frame) for frame in frames) + "\ndata: [DONE]")
        if stream
        else json.dumps({"content": [], "debug": frames[0]["debug"], "padding": frames[0]["padding"], "metadata": frames[1]})
    )
    details = empty_completion_diagnostics(body={}, adapter_type="anthropic_compatible", raw=raw.encode(), secret_values=())
    assert details is not None
    for secret in ("remote-private-value", "cookie-private-value", "remote%2Fcredential%3D", "private-array-value", "opaque-jwt-value"):
        assert secret not in json.dumps(details)
    assert details["response_truncated"]
    assert len(details["response_body"].encode()) <= 64 * 1024


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"model": "actual-model", "stop_reason": "end_turn", "content": [], "usage": {"output_tokens": 0}},
        {
            "model": "actual-model",
            "choices": [
                {"finish_reason": "end_turn", "message": {"content": ""}},
                {"finish_reason": "max_tokens", "message": {"content": "other choice"}},
            ],
            "usage": {"output_tokens": 0},
        },
        {
            "type": "response.completed",
            "response": {"model": "actual-model", "output": [], "usage": {"output_tokens": 0}, "incomplete_details": {"reason": "end_turn"}},
        },
    ],
    ids=["anthropic", "first-chat-choice", "responses-envelope"],
)
def test_summary_uses_protocol_fields_instead_of_nested_metadata(payload: dict) -> None:
    payload["metadata"] = {
        "model": "metadata-model",
        "stop_reason": "max_tokens",
        "usage": {"output_tokens": 999},
        "content": "metadata text",
        "type": "tool_use",
    }
    details = empty_completion_diagnostics(body={}, adapter_type="openai_compatible", raw=json.dumps(payload).encode(), secret_values=())
    assert details is not None
    assert details["response_summary"] == {"model": "actual-model", "stop_reason": "end_turn", "usage": {"output_tokens": 0}, "content_types": []}
    assert "metadata-model" in details["response_body"]


@pytest.mark.unit
def test_usage_summary_cannot_bypass_response_excerpt_limit() -> None:
    payload = {
        "content": [],
        "usage": {"output_tokens": 8, "output_tokens_details": {"reasoning_tokens": 8, "metadata": "x" * 100000}, "debug": "y" * 100000},
    }
    details = empty_completion_diagnostics(body={}, adapter_type="anthropic_compatible", raw=json.dumps(payload).encode(), secret_values=())
    assert details is not None
    assert details["response_summary"]["usage"] == {"output_tokens": 8, "output_tokens_details": {"reasoning_tokens": 8}}
    assert len(json.dumps(details["response_summary"]).encode()) < 2048
    assert details["response_truncated"]


@pytest.mark.unit
def test_response_formatting_is_preserved_inside_json_strings_and_sse_events() -> None:
    value = " first\n  second\t "
    raw = json.dumps({"content": [], "debug": value}).encode()
    details = empty_completion_diagnostics(body={}, adapter_type="anthropic_compatible", raw=raw, secret_values=())
    assert details is not None
    assert json.loads(details["response_body"])["debug"] == value


@pytest.mark.unit
def test_stream_usage_keeps_earlier_input_counts_and_later_output_counts() -> None:
    frames = [
        {
            "model": "actual",
            "choices": [{"delta": {"reasoning_content": "thinking"}}],
            "usage": {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 5}},
        },
        {
            "choices": [{"delta": {}, "finish_reason": "length"}],
            "usage": {"completion_tokens": 8, "completion_tokens_details": {"reasoning_tokens": 8}},
        },
    ]
    raw = "event: response\n" + "\n".join("data: " + json.dumps(frame) for frame in frames) + "\ndata: [DONE]"
    details = empty_completion_diagnostics(body={}, adapter_type="openai_compatible", raw=raw.encode(), secret_values=())
    assert details is not None
    assert details["response_summary"]["usage"] == {**frames[0]["usage"], **frames[1]["usage"]}
    assert details["response_body"].startswith("event: response\ndata:")
    assert details["response_body"].endswith("data: [DONE]")


@pytest.mark.unit
def test_invalid_token_counts_are_not_displayed_as_valid_usage() -> None:
    payload = {
        "content": [],
        "usage": {"input_tokens": True, "output_tokens": -1, "total_tokens": 2**100, "prompt_tokens": "10", "completion_tokens": 1.5},
    }
    details = empty_completion_diagnostics(body={}, adapter_type="anthropic_compatible", raw=json.dumps(payload).encode(), secret_values=())
    assert details is not None
    assert details["response_summary"]["usage"] is None
    assert json.loads(details["response_body"])["usage"] == payload["usage"]


@pytest.mark.unit
@pytest.mark.parametrize("request_credential", [False, True], ids=["response-number", "known-request-number"])
def test_numeric_credentials_are_hidden_when_echoed_as_json_numbers(request_credential: bool) -> None:
    payload = {"content": [], "debug": 1234}
    if not request_credential:
        payload["api_key"] = 1234
    details = empty_completion_diagnostics(
        body={"api_key": "1234"} if request_credential else {},
        adapter_type="anthropic_compatible",
        raw=json.dumps(payload).encode(),
        secret_values=(),
    )
    assert details is not None
    assert "1234" not in json.dumps(details)
