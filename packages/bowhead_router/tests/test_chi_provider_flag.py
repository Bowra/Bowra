"""Sprint χ-1 — provider parallel_tool_calls 透传 + sniff drop tests."""

from __future__ import annotations

import pytest
import respx
from bowhead_router.adapters import _chi_sniff
from bowhead_router.outbound import reset_learned_for_test

from bowhead_router import LLMRequest, Message, OpenAICompatibleAdapter, ToolSchema

BASE = "https://api.example.com/v1"


def _tool() -> ToolSchema:
    return ToolSchema(
        name="echo",
        description="echo",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )


def _ok_body(content: str = "hi") -> dict:
    return {
        "id": "x",
        "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


@pytest.fixture
async def adapter():
    a = OpenAICompatibleAdapter(base_url=BASE, api_key="sk-test", provider_name="test_provider")
    try:
        yield a
    finally:
        await a.close()


@pytest.fixture(autouse=True)
def _reset_sniff() -> None:
    _chi_sniff._reset_for_test()
    reset_learned_for_test()
    yield
    _chi_sniff._reset_for_test()
    # Reset outbound's process-wide _learned state now that it tracks sniff-drop constraints;
    # otherwise constraints stored for ("openai_compatible","api.example.com","test-model") leak into
    # later tests (review R3-P2-13: delayed failure).
    reset_learned_for_test()


@pytest.mark.unit
@respx.mock
async def test_flag_disabled_no_param(adapter: OpenAICompatibleAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit flag OFF → body 不含 parallel_tool_calls."""
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "0")
    route = respx.post(f"{BASE}/chat/completions").respond(200, json=_ok_body())
    await adapter.invoke(LLMRequest(model="test-model", messages=[Message(role="user", content="hi")], tools=[_tool()]))
    sent = route.calls[0].request.content.decode()
    assert "parallel_tool_calls" not in sent


@pytest.mark.unit
@respx.mock
async def test_flag_on_sends_param(adapter: OpenAICompatibleAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag ON + clean provider → body 含 parallel_tool_calls=true."""
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    route = respx.post(f"{BASE}/chat/completions").respond(200, json=_ok_body())
    await adapter.invoke(LLMRequest(model="test-model", messages=[Message(role="user", content="hi")], tools=[_tool()]))
    sent = route.calls[0].request.content.decode()
    assert '"parallel_tool_calls": true' in sent or '"parallel_tool_calls":true' in sent


@pytest.mark.unit
@respx.mock
async def test_flag_on_no_tools_no_param(adapter: OpenAICompatibleAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag ON but req.tools 空 → 不发 parallel_tool_calls (无意义)."""
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    route = respx.post(f"{BASE}/chat/completions").respond(200, json=_ok_body())
    await adapter.invoke(LLMRequest(model="test-model", messages=[Message(role="user", content="hi")]))
    sent = route.calls[0].request.content.decode()
    assert "parallel_tool_calls" not in sent


@pytest.mark.unit
@respx.mock
async def test_400_sniff_match_retries_without_flag(adapter: OpenAICompatibleAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag ON, provider 返 400 含 'parallel' → drop flag + retry succeeds."""
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    err_body = {"error": {"message": "Unknown parameter: parallel_tool_calls"}}
    route = respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[
            httpx2.Response(400, json=err_body),  # type: ignore[name-defined]
            httpx2.Response(200, json=_ok_body("retry-ok")),  # type: ignore[name-defined]
        ]
    )
    resp = await adapter.invoke(LLMRequest(model="test-model", messages=[Message(role="user", content="hi")], tools=[_tool()]))
    assert resp.content == "retry-ok"
    # First request had flag, second didn't
    first = route.calls[0].request.content.decode()
    second = route.calls[1].request.content.decode()
    assert "parallel_tool_calls" in first
    assert "parallel_tool_calls" not in second
    # The constraint is remembered in outbound parameters, rather than `_chi_sniff._dropped` (keyed by provider name).
    # It is keyed by **transport identity** (protocol + host + model); provider names have no team namespace.
    from bowhead_router.outbound import learned_constraints

    assert learned_constraints(
        adapter_type="openai_compatible",
        base_url=BASE,
        model="test-model",
    ) == frozenset({"parallel_tool_calls"})


@pytest.mark.unit
@respx.mock
async def test_400_non_sniff_does_not_retry(adapter: OpenAICompatibleAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    """400 with unrelated message → no retry, raises original."""
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    err_body = {"error": {"message": "bad model id"}}
    respx.post(f"{BASE}/chat/completions").respond(400, json=err_body)
    from bowhead_router.adapters.openai_compat import OpenAICompatError

    with pytest.raises(OpenAICompatError):
        await adapter.invoke(LLMRequest(model="test-model", messages=[Message(role="user", content="hi")], tools=[_tool()]))
    assert not _chi_sniff.is_dropped("test_provider")


@pytest.mark.unit
@respx.mock
async def test_dropped_provider_skips_param(adapter: OpenAICompatibleAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider already dropped → next request skips flag without round-trip."""
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EMIT", "1")
    _chi_sniff.mark_dropped("test_provider")
    route = respx.post(f"{BASE}/chat/completions").respond(200, json=_ok_body())
    await adapter.invoke(LLMRequest(model="test-model", messages=[Message(role="user", content="hi")], tools=[_tool()]))
    sent = route.calls[0].request.content.decode()
    assert "parallel_tool_calls" not in sent


@pytest.mark.unit
def test_sniff_matches_param_unknown() -> None:
    """Tightened regex (χ-1.1) — requires param name OR proximity to unknown/unsupported."""
    assert _chi_sniff.matches_param_unknown("Unknown parameter: parallel_tool_calls")
    assert _chi_sniff.matches_param_unknown("PARALLEL_TOOL_CALLS not supported")
    assert _chi_sniff.matches_param_unknown("parallel_tool_calls is invalid")
    assert _chi_sniff.matches_param_unknown("unrecognized field 'parallel_tool_calls'")
    # Tightening: bare "parallel x" no longer triggers (was false positive)
    assert not _chi_sniff.matches_param_unknown('{"error":{"message":"parallel x"}}')
    # Tightening: rate limit / capacity messages don't false-positive
    assert not _chi_sniff.matches_param_unknown("rate limit on parallel requests")
    assert not _chi_sniff.matches_param_unknown("max parallel sessions exceeded")
    assert not _chi_sniff.matches_param_unknown("invalid model id")
    assert not _chi_sniff.matches_param_unknown("")


@pytest.mark.unit
def test_sniff_mark_warn_once(caplog: pytest.LogCaptureFixture) -> None:
    """mark_dropped logs WARN once per provider."""
    import logging

    with caplog.at_level(logging.WARNING):
        _chi_sniff.mark_dropped("foo")
        _chi_sniff.mark_dropped("foo")  # 2nd call — no extra warn
        _chi_sniff.mark_dropped("bar")
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    # Expect 2 warns: foo + bar (foo's 2nd call dedup'd)
    assert sum(1 for r in warns if "foo" in r.getMessage()) == 1
    assert sum(1 for r in warns if "bar" in r.getMessage()) == 1


# httpx2 imported lazily — declare at module level for type annotation
import httpx2  # noqa: E402
