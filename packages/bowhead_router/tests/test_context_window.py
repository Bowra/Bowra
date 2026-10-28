"""上下文工程 sprint 批 4 —— 模型目录 `context_window` 与超窗失败分类（router 侧）。"""

from __future__ import annotations

import pytest
from bowhead_router.errors import (
    ProviderCallError,
    ProviderFailureKind,
    context_overflow_from_message,
    failure_kind_for_error_payload,
    refine_failure_kind,
    stream_failure_kind_allows_fallback,
)
from bowhead_router.model_capabilities import DEFAULT_CONTEXT_WINDOW, catalog_context_window, effective_context_window
from bowhead_router.router import UserModelPref

from bowhead_router import AgentBinding, ProviderConfig, ProviderRegistry, Router

_PROVIDERS = {
    "p_rich": {"models": [{"id": "big", "context_window": 1_000_000}, {"id": "nowin"}, {"id": "bad", "context_window": True}]},
    "p_default": {"context_window": 128_000, "models": ["legacy", {"id": "override", "context_window": 32_000}]},
    "p_none": {"models": ["m"]},
}


# ── 4-a Catalog lookup ──


@pytest.mark.unit
def test_rich_model_field_is_read() -> None:
    assert effective_context_window(_PROVIDERS, "p_rich", "big") == 1_000_000


@pytest.mark.unit
def test_provider_default_applies_to_legacy_string_models() -> None:
    assert effective_context_window(_PROVIDERS, "p_default", "legacy") == 128_000


@pytest.mark.unit
def test_model_field_overrides_provider_default() -> None:
    assert effective_context_window(_PROVIDERS, "p_default", "override") == 32_000


@pytest.mark.unit
def test_undeclared_model_uses_the_catalog_then_the_default() -> None:
    assert effective_context_window(_PROVIDERS, "p_rich", "nowin") == DEFAULT_CONTEXT_WINDOW == 131_072
    assert effective_context_window(_PROVIDERS, "p_none", "m") == 131_072
    assert effective_context_window(None, "p_rich", "big") == 131_072
    # Documented windows: "1M" is recorded as 1,000,000; max input wins over the full window.
    assert effective_context_window({"ds": {"models": ["deepseek-flash"]}}, "ds", "deepseek-flash") == 1_000_000
    assert effective_context_window({"q": {"models": [{"id": "qwen3.7-plus-us"}]}}, "q", "qwen3.7-plus-us") == 991_808
    assert catalog_context_window("MiniMax-M3") == 1_000_000
    assert catalog_context_window("my-private-llm") is None


@pytest.mark.unit
def test_a_declared_window_wins_over_the_catalog() -> None:
    assert effective_context_window({"ds": {"models": [{"id": "deepseek-flash", "context_window": 64_000}]}}, "ds", "deepseek-flash") == 64_000


@pytest.mark.unit
def test_bool_is_not_a_window() -> None:
    assert effective_context_window(_PROVIDERS, "p_rich", "bad") == 131_072


# ── 4-a Router: minimum context window across the chain ──


def _router() -> Router:
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="a", type="mock"), ProviderConfig(name="b", type="mock")])
    r = Router(reg)
    r.bind(AgentBinding(agent_type="lead", primary=("a", "m1"), fallback=[("b", "m2")]))
    r.bind(AgentBinding(agent_type="echo", primary=("b", "m3")))
    return r


@pytest.mark.unit
def test_router_uses_the_primary_window_not_the_chain_minimum() -> None:
    """review 修：1M 主模型配 32k 备胎时按最小值算 ⇒ 每轮正常流量都被裁、前缀每圈作废。备胎窗口是备胎的事。"""
    r = _router()
    r.set_context_windows({("a", "m1"): 1_000_000, ("b", "m2"): 32_000})
    assert r.context_window_for("lead") == 1_000_000


@pytest.mark.unit
def test_router_unregistered_primary_uses_the_default_not_the_fallback() -> None:
    r = _router()
    r.set_context_windows({("b", "m2"): 128_000})
    assert r.context_window_for("lead") == 131_072
    assert r.context_window_for("echo") == 131_072


@pytest.mark.unit
def test_router_user_pref_replaces_primary_in_the_lookup() -> None:
    r = _router()
    r.set_context_windows({("a", "m1"): 1_000_000, ("b", "m2"): 128_000, ("b", "small"): 32_000})
    assert r.context_window_for("lead", user_pref=UserModelPref(provider="b", model="small")) == 32_000


@pytest.mark.unit
def test_router_no_binding_is_none_not_an_exception() -> None:
    assert _router().context_window_for("unbound") is None


@pytest.mark.unit
def test_router_rejects_garbage_windows() -> None:
    r = _router()
    r.set_context_windows({("a", "m1"): 0, ("b", "m2"): True, ("b", "m3"): -5})  # type: ignore[dict-item]
    assert r.context_window_for("lead") == 131_072
    assert r.context_window_for("echo") == 131_072


# ── 4-c Context overflow: anchored message templates (two observed on 2026-09-05 + Anthropic documentation) ──

_DEEPSEEK_400 = (
    "This model's maximum context length is 1048576 tokens. However, you requested 3600023 tokens "
    "(3600007 in the messages, 16 in the completion). Please reduce the length of the messages or completion."
)
_DASHSCOPE_400 = "<400> InternalError.Algo.InvalidParameter: Range of input length should be [1, 30720]"
_ANTHROPIC_400 = "prompt is too long: 210000 tokens > 200000 maximum"


@pytest.mark.unit
def test_real_deepseek_overflow_body_is_classified_with_numbers() -> None:
    """116 生产路径（DeepSeek anthropic 面）实测：code 是 invalid_request_error，只有消息模板说了超窗。"""
    e = ProviderCallError(
        kind=ProviderFailureKind.REQUEST_REJECTED, status_code=400, upstream_error_code="invalid_request_error", upstream_message=_DEEPSEEK_400
    )
    assert e.kind is ProviderFailureKind.CONTEXT_OVERFLOW
    assert context_overflow_from_message(e.upstream_message) == (1_048_576, 3_600_023)


@pytest.mark.unit
def test_real_dashscope_overflow_body_is_classified_with_maximum_only() -> None:
    e = ProviderCallError(
        kind=ProviderFailureKind.REQUEST_REJECTED, status_code=400, upstream_error_code="invalid_parameter_error", upstream_message=_DASHSCOPE_400
    )
    assert e.kind is ProviderFailureKind.CONTEXT_OVERFLOW
    assert context_overflow_from_message(e.upstream_message) == (30_720, None)


@pytest.mark.unit
def test_anthropic_documented_template_is_classified() -> None:
    e = ProviderCallError(
        kind=ProviderFailureKind.REQUEST_REJECTED, status_code=400, upstream_error_type="invalid_request_error", upstream_message=_ANTHROPIC_400
    )
    assert e.kind is ProviderFailureKind.CONTEXT_OVERFLOW
    assert context_overflow_from_message(e.upstream_message) == (200_000, 210_000)


@pytest.mark.unit
def test_unrelated_400_prose_is_not_overflow() -> None:
    for msg in ("temperature must be between 0 and 2", "Invalid parameter: messages[0].content", "context is important"):
        e = ProviderCallError(
            kind=ProviderFailureKind.REQUEST_REJECTED, status_code=400, upstream_error_code="invalid_request_error", upstream_message=msg
        )
        assert e.kind is ProviderFailureKind.REQUEST_REJECTED, msg
    assert context_overflow_from_message(None) is None and context_overflow_from_message("") is None


# ── 4-c Context overflow classification: accept only structured codes ──


@pytest.mark.unit
def test_constructor_refines_400_with_structured_overflow_code() -> None:
    e = ProviderCallError(kind=ProviderFailureKind.REQUEST_REJECTED, status_code=400, upstream_error_code="context_length_exceeded")
    assert e.kind is ProviderFailureKind.CONTEXT_OVERFLOW


@pytest.mark.unit
def test_constructor_leaves_other_400s_alone() -> None:
    e = ProviderCallError(kind=ProviderFailureKind.REQUEST_REJECTED, status_code=400, upstream_error_code="unsupported_value")
    assert e.kind is ProviderFailureKind.REQUEST_REJECTED


@pytest.mark.unit
def test_refine_only_touches_request_rejected() -> None:
    assert refine_failure_kind(ProviderFailureKind.AUTH, upstream_error_code="context_length_exceeded") is ProviderFailureKind.AUTH
    assert (
        refine_failure_kind(ProviderFailureKind.REQUEST_REJECTED, upstream_error_code="Context-Length-Exceeded")
        is ProviderFailureKind.CONTEXT_OVERFLOW
    )


@pytest.mark.unit
def test_in_band_payload_classifies_overflow_with_and_without_status() -> None:
    assert (
        failure_kind_for_error_payload({"error": {"type": "invalid_request_error", "code": "context_length_exceeded"}})
        is ProviderFailureKind.CONTEXT_OVERFLOW
    )
    assert failure_kind_for_error_payload({"error": {"code": "context_length_exceeded", "status": 400}}) is ProviderFailureKind.CONTEXT_OVERFLOW
    assert failure_kind_for_error_payload({"error": {"code": "bad_request", "status": 400}}) is ProviderFailureKind.REQUEST_REJECTED


@pytest.mark.unit
def test_overflow_is_not_stream_fallback_eligible() -> None:
    assert not stream_failure_kind_allows_fallback(ProviderFailureKind.CONTEXT_OVERFLOW)


@pytest.mark.unit
def test_openai_second_template_without_max_tokens_is_classified() -> None:
    msg = "This model's maximum context length is 128000 tokens. However, your messages resulted in 130123 tokens. Please reduce the length of the messages."
    e = ProviderCallError(
        kind=ProviderFailureKind.REQUEST_REJECTED, status_code=400, upstream_error_code="context_length_exceeded", upstream_message=msg
    )
    assert e.kind is ProviderFailureKind.CONTEXT_OVERFLOW
    assert context_overflow_from_message(msg) == (128_000, 130_123)
