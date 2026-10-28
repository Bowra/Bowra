"""Exercise sending policies through the final runtime and probe builders."""

from __future__ import annotations

import copy
import json

import pytest
from bowhead_router.parameter_preview import compile_request_body, parameter_preview, synthetic_request
from bowhead_router.probe import CompatibleProbeRegistry, NotTestable, ProbeRequestPlan
from bowhead_router.runtime_parameters import RuntimeParametersError, validate_runtime_parameters
from bowhead_router.transport import model_request_shape

pytestmark = pytest.mark.unit


def provider(profile="dashscope-mixed-chat-v1", defaults=None, overrides=None):
    anthropic = profile.startswith("anthropic")
    return {
        "type": "anthropic_compatible" if anthropic else "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": "test-secret",
        "models": [
            {
                "id": "arbitrary-model-id",
                "capabilities": ["chat", "tool", "vision"],
                **({"api_surface": "responses"} if "responses" in profile else {}),
                "runtime_parameters": {"profile": profile, "defaults": defaults or {}, "capability_overrides": overrides or {}},
            }
        ],
    }


@pytest.mark.parametrize("capability", ["chat", "tool", "vision"])
def test_shared_off_reaches_each_final_builder_after_caller_thinking(capability):
    definition = provider(defaults={"thinking_mode": "off"})
    request = synthetic_request("arbitrary-model-id", capability)
    request.extended_thinking = True
    request.thinking_budget_tokens = 2048
    body = compile_request_body(definition, request)
    assert body["enable_thinking"] is False


def test_anthropic_off_overrides_enabled_caller_binding():
    definition = provider("anthropic-adaptive-v1", defaults={"thinking_mode": "off"})
    request = synthetic_request("arbitrary-model-id", "chat")
    request.extended_thinking = True
    request.thinking_budget_tokens = 2048
    assert compile_request_body(definition, request)["thinking"] == {"type": "disabled"}


def test_off_exception_does_not_inherit_a_manual_budget():
    definition = provider(
        "anthropic-manual-v1",
        defaults={"thinking_mode": "on", "thinking_budget": 2048, "outbound_params": {"temperature": {"mode": "omit"}}},
        overrides={"tool": {"thinking_mode": "off"}},
    )
    validate_runtime_parameters(definition, definition["models"][0])
    assert compile_request_body(definition, synthetic_request("arbitrary-model-id", "tool"))["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("advanced", [{"access_token": "secret"}, {"max_output_tokens": False}, {"parallel_tool_calls": 0}])
def test_advanced_credentials_and_invalid_known_value_types_are_rejected(advanced):
    definition = provider(defaults={"advanced": advanced})
    with pytest.raises(ValueError):
        validate_runtime_parameters(definition, definition["models"][0])


def test_output_alias_collision_requires_explicit_resolution():
    definition = provider(defaults={"outbound_params": {"max_tokens": {"mode": "fixed", "value": 2000}}, "advanced": {"max_completion_tokens": 3000}})
    with pytest.raises(RuntimeParametersError, match="overlapping"):
        validate_runtime_parameters(definition, definition["models"][0])


def test_advanced_output_aliases_require_one_wire_field():
    definition = provider(defaults={"advanced": {"max_tokens": 2000, "max_completion_tokens": 3000}})
    with pytest.raises(RuntimeParametersError, match="one output-limit"):
        validate_runtime_parameters(definition, definition["models"][0])


@pytest.mark.parametrize("alias", ["max_tokens", "max_output_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("profile", ["dashscope-mixed-chat-v1", "openai-reasoning-responses-v1"])
def test_reviewed_advanced_output_aliases_compile_to_one_protocol_field(alias, profile):
    definition = provider(profile, defaults={"advanced": {alias: 2000}})
    validate_runtime_parameters(definition, definition["models"][0])
    body = compile_request_body(definition, synthetic_request("arbitrary-model-id", "chat"))
    output = {key: body[key] for key in ("max_tokens", "max_output_tokens", "max_completion_tokens") if key in body}
    assert output == {"max_output_tokens" if "responses" in profile else "max_tokens": 2000}
    preview = parameter_preview(definition, "arbitrary-model-id", "chat")
    rows = [row for row in preview["parameters"] if row["field"] in {"max_tokens", "max_output_tokens", "max_completion_tokens"}]
    assert len(rows) == 1
    assert rows[0]["value"] == 2000
    assert rows[0]["source"] == "advanced"


def test_azure_output_alias_preserves_the_declared_value_after_final_rename():
    definition = provider(defaults={"advanced": {"max_completion_tokens": 2000}})
    definition["type"] = "azure_openai"
    definition["models"][0]["id"] = "gpt-5"
    definition["models"][0]["runtime_parameters"].pop("profile")
    validate_runtime_parameters(definition, definition["models"][0])
    body = compile_request_body(definition, synthetic_request("gpt-5", "chat"))
    assert body["max_completion_tokens"] == 2000
    assert "max_tokens" not in body
    assert "max_output_tokens" not in body


def test_advanced_thinking_budget_cannot_bypass_paid_probe_limits():
    definition = provider(defaults={"advanced": {"enable_thinking": True, "thinking_budget": 50000}})
    definition["models"][0]["runtime_parameters"].pop("profile")
    validate_runtime_parameters(definition, definition["models"][0])
    plan = CompatibleProbeRegistry().plan(
        definition, provider_id="llmp_example", provider_display_name="Example", model_id="arbitrary-model-id", capability="chat"
    )
    assert isinstance(plan, NotTestable)
    assert plan.safe_code == "parameter_policy_exceeds_probe_limits"
    assert plan.provider_request_count == 0


def test_preview_never_echoes_arbitrary_legacy_reasoning_values():
    definition = provider()
    del definition["models"][0]["runtime_parameters"]
    definition["models"][0]["request_body_overrides"] = {"chat": {"reasoning_effort": "private-value", "thinking_budget": "private-value"}}
    assert "private-value" not in json.dumps(parameter_preview(definition, "arbitrary-model-id", "chat"))


def test_only_explicit_fields_override_defaults_and_removal_restores_inheritance():
    definition = provider(
        defaults={"thinking_mode": "off", "outbound_params": {"temperature": {"mode": "fixed", "value": 0.4}}},
        overrides={"tool": {"thinking_mode": "on"}},
    )
    tool = compile_request_body(definition, synthetic_request("arbitrary-model-id", "tool"))
    chat = compile_request_body(definition, synthetic_request("arbitrary-model-id", "chat"))
    assert tool["enable_thinking"] is True
    assert chat["enable_thinking"] is False
    assert tool["temperature"] == chat["temperature"] == 0.4
    del definition["models"][0]["runtime_parameters"]["capability_overrides"]["tool"]
    assert compile_request_body(definition, synthetic_request("arbitrary-model-id", "tool"))["enable_thinking"] is False


@pytest.mark.parametrize("profile", ["dashscope-mixed-chat-v1", "anthropic-adaptive-v1"])
def test_false_parallel_control_is_a_boolean_in_both_protocols(profile):
    definition = provider(profile, defaults={"thinking_mode": "off", "outbound_params": {"parallel_tool_calls": {"mode": "fixed", "value": False}}})
    body = compile_request_body(definition, synthetic_request("arbitrary-model-id", "tool"))
    if profile.startswith("anthropic"):
        assert body["tool_choice"]["disable_parallel_tool_use"] is True
        assert body["thinking"] == {"type": "disabled"}
    else:
        assert body["parallel_tool_calls"] is False


def test_responses_effort_mapping_and_output_cap():
    definition = provider(
        "openai-reasoning-responses-v1",
        defaults={"reasoning_effort": "low", "outbound_params": {"temperature": {"mode": "omit"}, "max_tokens": {"mode": "cap", "value": 2048}}},
    )
    body = compile_request_body(definition, synthetic_request("arbitrary-model-id", "chat"))
    assert body["reasoning"] == {"effort": "low"}
    assert body["max_output_tokens"] == 2048
    assert "temperature" not in body
    assert "max_tokens" not in body
    assert body["store"] is False


@pytest.mark.parametrize(
    "defaults",
    [
        {"thinking_mode": "off", "thinking_budget": 1000},
        {"thinking_mode": "on", "thinking_budget": True},
        {"thinking_mode": "on", "thinking_budget": 0},
        {"reasoning_effort": "max"},
        {"advanced": {"reasoning.effort": "low"}},
        {"advanced": {"store": True}},
        {"advanced": {"vendor_option": {"$bowhead": "messages"}}},
    ],
)
def test_invalid_policies_fail_before_sending(defaults):
    definition = provider(defaults=defaults)
    with pytest.raises(ValueError):
        validate_runtime_parameters(definition, definition["models"][0])


def test_always_on_profile_has_no_off_and_profile_switch_preserves_incompatible_values():
    definition = provider("openai-reasoning-responses-v1", defaults={"thinking_mode": "off"})
    with pytest.raises(RuntimeParametersError, match="thinking_mode"):
        validate_runtime_parameters(definition, definition["models"][0])
    assert definition["models"][0]["runtime_parameters"]["defaults"]["thinking_mode"] == "off"


def test_manual_budget_requires_output_headroom_and_sampling_compatibility():
    definition = provider(
        "anthropic-manual-v1",
        defaults={
            "thinking_mode": "on",
            "thinking_budget": 2048,
            "outbound_params": {"temperature": {"mode": "omit"}, "max_tokens": {"mode": "fixed", "value": 2048}},
        },
    )
    with pytest.raises(RuntimeParametersError, match="headroom"):
        validate_runtime_parameters(definition, definition["models"][0])
    definition["models"][0]["runtime_parameters"]["defaults"]["outbound_params"]["max_tokens"]["value"] = 4096
    validate_runtime_parameters(definition, definition["models"][0])


@pytest.mark.parametrize("capacity_scope", ["model", "provider", "fallback"])
def test_manual_thinking_validation_uses_automatic_model_output(capacity_scope):
    definition = provider(
        "anthropic-manual-v1",
        defaults={"thinking_mode": "on", "thinking_budget": 16_000, "outbound_params": {"temperature": {"mode": "omit"}}},
    )
    model = definition["models"][0]
    if capacity_scope != "fallback":
        (model if capacity_scope == "model" else definition)["max_output_tokens"] = 64_000
    validate_runtime_parameters(definition, model)
    body = compile_request_body(definition, synthetic_request(model["id"], "tool", max_output_tokens=None))
    assert body["max_tokens"] == (32_000 if capacity_scope == "fallback" else 64_000)
    assert body["thinking"]["budget_tokens"] == 16_000
    model["runtime_parameters"]["defaults"]["outbound_params"]["max_tokens"] = {"mode": "cap", "value": 4000}
    with pytest.raises(RuntimeParametersError, match="headroom"):
        validate_runtime_parameters(definition, model)


def test_legacy_scope_and_precedence_survive_without_new_policy():
    definition = provider()
    model = definition["models"][0]
    del model["runtime_parameters"]
    model["request_body_overrides"] = {"chat": {"enable_thinking": False, "temperature": 0.4}}
    model["outbound_params"] = {"temperature": {"mode": "fixed", "value": 0.8}}
    chat = compile_request_body(definition, synthetic_request("arbitrary-model-id", "chat"))
    tool = compile_request_body(definition, synthetic_request("arbitrary-model-id", "tool"))
    assert chat["enable_thinking"] is False
    assert "enable_thinking" not in tool
    assert chat["temperature"] == 0.8
    assert "runtime_parameters" not in model_request_shape(definition, model_id=model["id"], capability="chat", adapter_type=definition["type"])


def test_new_typed_policy_cannot_shadow_legacy_raw_reasoning():
    definition = provider(defaults={"thinking_mode": "off"})
    definition["models"][0]["request_body_overrides"] = {"chat": {"enable_thinking": True}}
    with pytest.raises(RuntimeParametersError, match="overlapping"):
        validate_runtime_parameters(definition, definition["models"][0])


def test_only_effective_capability_policies_change_evidence_shape():
    definition = provider(defaults={"thinking_mode": "off"}, overrides={"tool": {"thinking_mode": "on"}})
    changed = copy.deepcopy(definition)
    changed["models"][0]["runtime_parameters"]["defaults"]["thinking_mode"] = "on"

    def shape(p, c):
        return model_request_shape(p, model_id="arbitrary-model-id", capability=c, adapter_type=p["type"])

    assert shape(definition, "tool") == shape(changed, "tool")
    assert shape(definition, "chat") != shape(changed, "chat")


def test_parameter_preview_redacts_unknown_values_and_never_constructs_an_http_client(monkeypatch):
    monkeypatch.setattr("httpx2.AsyncClient", lambda **kwargs: pytest.fail("Preview must not construct a client"))
    definition = provider(defaults={"thinking_mode": "off", "advanced": {"vendor_option": {"credential": "sensitive-custom-value"}}})
    projection = parameter_preview(definition, "arbitrary-model-id", "chat")
    assert "sensitive-custom-value" not in json.dumps(projection)
    assert "test-secret" not in json.dumps(projection)
    assert projection["provider_request_count"] == projection["configuration_writes"] == 0
    assert next(row for row in projection["parameters"] if row["field"] == "enable_thinking")["value"] is False


def test_verification_uses_final_builder_and_never_reduces_explicit_fixed_limits():
    definition = provider(
        defaults={
            "thinking_mode": "off",
            "outbound_params": {"parallel_tool_calls": {"mode": "fixed", "value": False}, "max_tokens": {"mode": "fixed", "value": 3000}},
        }
    )
    plan = CompatibleProbeRegistry().plan(
        definition, provider_id="llmp_" + "0" * 32, provider_display_name="Demo", model_id="arbitrary-model-id", capability="tool"
    )
    assert isinstance(plan, ProbeRequestPlan)
    assert plan.body_shape["max_tokens"] == 3000
    assert plan.body_shape["parallel_tool_calls"] is False
    assert plan.body_shape == compile_request_body(definition, synthetic_request("arbitrary-model-id", "tool", max_output_tokens=128))
    definition["models"][0]["runtime_parameters"]["defaults"]["outbound_params"]["max_tokens"]["value"] = 100000
    plan = CompatibleProbeRegistry().plan(
        definition, provider_id="llmp_" + "0" * 32, provider_display_name="Demo", model_id="arbitrary-model-id", capability="tool"
    )
    assert isinstance(plan, NotTestable)
    assert plan.provider_request_count == 0
    assert plan.safe_code == "parameter_policy_exceeds_probe_limits"
