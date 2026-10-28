"""Test outbound declaration semantics, protocol mapping, and adaptation boundaries."""

from __future__ import annotations

import pytest
from bowhead_router.outbound import (
    EMPTY_PARAMS,
    OutboundParams,
    OutboundParamsError,
    ParamRule,
    apply_learned_constraints,
    apply_outbound_params,
    learn_constraint,
    outbound_params_digest,
    parse_outbound_params,
    rejected_param,
    reset_learned_for_test,
    resolve_outbound_params,
)


@pytest.fixture(autouse=True)
def _clean_learned():
    reset_learned_for_test()
    yield
    reset_learned_for_test()


def _params(**rules: ParamRule) -> OutboundParams:
    return OutboundParams(rules=dict(rules))


@pytest.mark.unit
def test_follow_does_not_touch_the_body() -> None:
    """Follow must preserve values already supplied by model body overrides."""
    body = {"model": "m", "temperature": 1, "max_tokens": 4000}

    assert apply_outbound_params(dict(body), EMPTY_PARAMS, family="openai") == body


@pytest.mark.unit
def test_fixed_and_omit_beat_whatever_the_caller_asked_for() -> None:
    """约束落在出站层：调用方传什么都不改变结论（全仓 39 处调用点各设各的 temperature）。"""
    fixed = apply_outbound_params(
        {"temperature": 0.7, "top_p": 0.9},
        _params(temperature=ParamRule("fixed", 1)),
        family="openai",
    )
    assert fixed["temperature"] == 1

    omitted = apply_outbound_params(
        {"temperature": 0.7, "top_p": 0.9},
        _params(temperature=ParamRule("omit")),
        family="openai",
    )
    assert "temperature" not in omitted
    assert omitted["top_p"] == 0.9, "只动被声明的参数"


@pytest.mark.unit
def test_cap_lowers_but_never_raises() -> None:
    """负对照：cap 是上限不是设定值 —— 调用方要得少就按少的来。"""
    rule = _params(max_tokens=ParamRule("cap", 4096))

    assert apply_outbound_params({"max_tokens": 8000}, rule, family="openai")["max_tokens"] == 4096
    assert apply_outbound_params({"max_tokens": 512}, rule, family="openai")["max_tokens"] == 512


@pytest.mark.unit
def test_anthropic_parallel_maps_to_the_inverted_opt_out() -> None:
    """协议映射：anthropic 没有 parallel_tool_calls，等价控制是**取反**的 opt-out。"""
    on = apply_outbound_params(
        {"tool_choice": {"type": "auto"}},
        _params(parallel_tool_calls=ParamRule("fixed", True)),
        family="anthropic",
    )
    assert on["tool_choice"]["disable_parallel_tool_use"] is False
    assert "parallel_tool_calls" not in on, "不得把 OpenAI-only 字段漏给 anthropic"

    off = apply_outbound_params(
        {"tool_choice": {"type": "auto"}},
        _params(parallel_tool_calls=ParamRule("fixed", False)),
        family="anthropic",
    )
    assert off["tool_choice"]["disable_parallel_tool_use"] is True


@pytest.mark.unit
def test_anthropic_max_tokens_cannot_be_omitted() -> None:
    """协议差异在**校验期**炸，不留到运行期：anthropic 的 max_tokens 必填。"""
    with pytest.raises(OutboundParamsError, match="requires it"):
        parse_outbound_params({"max_tokens": {"mode": "omit"}}, family="anthropic", where="p")

    # Positive control: the same declaration is valid on the openai surface.
    assert parse_outbound_params({"max_tokens": {"mode": "omit"}}, family="openai", where="p").rule("max_tokens").mode == "omit"


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        {"messages": {"mode": "omit"}},  # Not allowlisted: changes semantics rather than shape.
        {"temperature": {"mode": "clamp"}},  # Unsupported mode.
        {"temperature": {"mode": "fixed"}},  # fixed requires a value.
        {"temperature": {"mode": "omit", "value": 1}},  # omit must not include a value.
        {"temperature": {"mode": "cap", "value": 1}},  # Only max_tokens supports cap.
        {"temperature": "1"},  # Not an object.
    ],
)
def test_illegal_declarations_are_rejected_at_parse_time(raw: dict) -> None:
    with pytest.raises(OutboundParamsError):
        parse_outbound_params(raw, family="openai", where="p")


@pytest.mark.unit
def test_model_level_declaration_beats_provider_level() -> None:
    """同一个网关可以同时挂拒 temperature 的 GPT-5 与接受它的 GPT-4 —— 粒度必须到模型。"""
    provider = {
        "outbound_params": {"temperature": {"mode": "fixed", "value": 1}},
        "models": [
            {"id": "gpt5", "outbound_params": {"temperature": {"mode": "omit"}}},
            {"id": "gpt4"},
        ],
    }

    assert resolve_outbound_params(provider, model_id="gpt5", family="openai").rule("temperature").mode == "omit"
    assert resolve_outbound_params(provider, model_id="gpt4", family="openai").rule("temperature").mode == "fixed"


@pytest.mark.unit
def test_digest_changes_with_the_declaration_and_ignores_follow() -> None:
    """声明变了 ⇒ config_signature 变 ⇒ 旧证据 stale。全 follow 时必须与「没配」同签名。"""
    assert outbound_params_digest(EMPTY_PARAMS) == ""
    assert outbound_params_digest(_params(temperature=ParamRule("follow"))) == ""
    assert outbound_params_digest(_params(temperature=ParamRule("fixed", 1))) != outbound_params_digest(_params(temperature=ParamRule("omit")))


# ── Adaptive behavior ───────────────────────────────────────────────────


@pytest.mark.unit
def test_structured_param_is_preferred_over_prose() -> None:
    """Read the structured param even when the prose lacks parameter keywords."""
    rejection = (
        b'{"error":{"message":"litellm.BadRequestError: AzureException BadRequestError - '
        b"Unsupported value: 'temperature' does not support 0.7 with this model. "
        b'Only the default (1) value is supported.","type":"invalid_request_error",'
        b'"param":"temperature","code":"unsupported_value"}}'
    )

    assert rejected_param(rejection) == "temperature"


@pytest.mark.unit
def test_prose_fallback_covers_providers_without_structured_param() -> None:
    assert rejected_param(b'{"error":{"message":"parallel_tool_calls is not supported"}}') == "parallel_tool_calls"
    assert rejected_param(b"400: unsupported temperature for this model") == "temperature"


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        b'{"error":{"message":"too many parallel requests, slow down","type":"rate_limit_error"}}',
        b'{"error":{"message":"invalid messages: role must be user","param":"messages"}}',
        b'{"error":{"message":"model not found","param":"model"}}',
        b"upstream timeout",
        b"",
        None,
    ],
)
def test_never_names_a_parameter_we_must_not_touch(body: bytes | None) -> None:
    """★ 上游正文是不可信输入。

    它点名 `messages`/`model` 时绝不能被我们照做 —— 那是改请求的**语义**。
    「parallel requests 限流」这类也不得误触发（否则会把好端端的 parallel_tool_calls 丢掉）。
    """
    assert rejected_param(body) is None


@pytest.mark.unit
def test_learning_is_per_model_and_only_announced_once() -> None:
    assert learn_constraint(adapter_type="openai_compatible", base_url="https://gw.example.com/v1", model="gpt5", param="temperature") is True
    assert learn_constraint(adapter_type="openai_compatible", base_url="https://gw.example.com/v1", model="gpt5", param="temperature") is False, (
        "重复观测不该刷屏"
    )

    body = apply_learned_constraints(
        {"temperature": 0.7},
        adapter_type="openai_compatible",
        base_url="https://gw.example.com/v1",
        model="gpt5",
        params=EMPTY_PARAMS,
        family="openai",
    )
    assert "temperature" not in body

    other = apply_learned_constraints(
        {"temperature": 0.7},
        adapter_type="openai_compatible",
        base_url="https://gw.example.com/v1",
        model="gpt4",
        params=EMPTY_PARAMS,
        family="openai",
    )
    assert other["temperature"] == 0.7, "同一个 provider 的另一个模型不受连累"


@pytest.mark.unit
def test_machine_never_overrides_an_explicit_declaration() -> None:
    """★ 人的声明 > 机器的观测：管理员表过态的参数，自适应不许再插手。"""
    learn_constraint(adapter_type="openai_compatible", base_url="https://gw.example.com/v1", model="m", param="temperature")

    body = apply_learned_constraints(
        {"temperature": 0.2},
        adapter_type="openai_compatible",
        base_url="https://gw.example.com/v1",
        model="m",
        params=_params(temperature=ParamRule("fixed", 0.2)),
        family="openai",
    )

    assert body["temperature"] == 0.2


@pytest.mark.unit
def test_learning_refuses_parameters_outside_the_whitelist() -> None:
    with pytest.raises(OutboundParamsError):
        learn_constraint(adapter_type="openai_compatible", base_url="https://gw.example.com/v1", model="m", param="messages")


@pytest.mark.unit
def test_two_teams_with_the_same_provider_name_do_not_share_constraints() -> None:
    """Transport identity must isolate gateways even when teams choose the same provider name."""
    learn_constraint(
        adapter_type="openai_compatible",
        base_url="https://provider.example.com/v1",
        model="shared-model-name",
        param="temperature",
    )

    other_gateway = apply_learned_constraints(
        {"temperature": 0.7},
        adapter_type="openai_compatible",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="shared-model-name",
        params=EMPTY_PARAMS,
        family="openai",
    )
    assert other_gateway["temperature"] == 0.7, "另一个网关的同名模型不得被连累"

    same_gateway = apply_learned_constraints(
        {"temperature": 0.7},
        adapter_type="openai_compatible",
        base_url="https://provider.example.com/v1",
        model="shared-model-name",
        params=EMPTY_PARAMS,
        family="openai",
    )
    assert "temperature" not in same_gateway, "同一个网关的同一个模型，约束本来就该共享"


@pytest.mark.unit
def test_adaptations_expose_only_the_parameter_and_the_time() -> None:
    """设置页要读它 —— 只给参数名与时间，不给地址、凭据、provider 的报错正文。"""
    from bowhead_router.outbound import learned_adaptations

    learn_constraint(
        adapter_type="openai_compatible",
        base_url="https://gw.example.com/v1",
        model="m",
        param="temperature",
    )

    found = learned_adaptations(adapter_type="openai_compatible", base_url="https://gw.example.com/v1", model="m")

    assert [item.param for item in found] == ["temperature"]
    assert found[0].since.tzinfo is not None, "时间必须带时区（页面要显示「起生效」）"
    assert set(vars(type(found[0])).get("__slots__", ())) == {"param", "since"}, "不得泄漏第三个字段"
