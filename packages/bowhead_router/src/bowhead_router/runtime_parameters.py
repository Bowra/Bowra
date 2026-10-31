"""Versioned, model-owned request policies shared by runtime and verification.

Profiles are explicit contracts, not model-name heuristics or upstream evidence.
Legacy overrides and outbound declarations keep their order; this optional layer
is applied afterwards, so an explicit Off cannot be lost during binding.
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any

from bowhead_router.outbound import (
    OutboundParams,
    ParamRule,
    ProtocolFamily,
    apply_outbound_params,
    parse_outbound_params,
    resolve_outbound_params,
    wire_param_name,
)

CONTRACT_VERSION = "model-runtime-parameters-v1"
CONVERSATIONAL_CAPABILITIES = frozenset({"chat", "tool", "vision"})
PROTECTED_FIELDS = frozenset(
    {
        "model",
        "messages",
        "input",
        "instructions",
        "system",
        "tools",
        "tool_choice",
        "response_format",
        "text",
        "stream",
        "stream_options",
        "n",
        "store",
        "background",
        "stop",
        "stop_sequences",
        "native_search",
        "enable_search",
        "search_options",
        "url",
        "base_url",
        "endpoint",
        "headers",
        "auth",
        "api_key",
        "extra_body",
        "previous_response_id",
    }
)

# A selected profile declares this documented wire dialect, not compatibility of
# every model behind an arbitrary gateway. IDs pin reviewed mappings forever.
PROFILES: dict[str, dict[str, Any]] = {
    "dashscope-mixed-chat-v1": {
        "id": "dashscope-mixed-chat-v1",
        "label": "DashScope mixed thinking · Chat",
        "family": "openai",
        "provider_types": ["openai_compatible"],
        "thinking_modes": ["off", "on", "omit"],
        "efforts": [],
        "budget_min": 1,
        "budget_max": 32768,
        "source": "https://help.aliyun.com/zh/model-studio/deep-thinking",
        "temperature_max": 2,
    },
    "openai-reasoning-responses-v1": {
        "id": "openai-reasoning-responses-v1",
        "label": "OpenAI reasoning · Responses (low–high)",
        "family": "openai_responses",
        "provider_types": ["openai_compatible"],
        "thinking_modes": ["on", "omit"],
        "efforts": ["low", "medium", "high"],
        "source": "https://developers.openai.com/api/docs/guides/reasoning",
        "temperature_max": 2,
    },
    "anthropic-manual-v1": {
        "id": "anthropic-manual-v1",
        "label": "Anthropic manual thinking · Messages (legacy)",
        "family": "anthropic",
        "provider_types": ["anthropic_compatible"],
        "thinking_modes": ["off", "on", "omit"],
        "efforts": [],
        "budget_min": 1024,
        "budget_max": 32768,
        "source": "https://platform.claude.com/docs/en/build-with-claude/extended-thinking",
        "temperature_max": 1,
    },
    "anthropic-adaptive-v1": {
        "id": "anthropic-adaptive-v1",
        "label": "Anthropic adaptive thinking · Messages (low–high)",
        "family": "anthropic",
        "provider_types": ["anthropic_compatible"],
        "thinking_modes": ["off", "adaptive", "omit"],
        "efforts": ["low", "medium", "high"],
        "source": "https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost",
        "temperature_max": 1,
    },
}


class RuntimeParametersError(ValueError):
    """A safe field-specific configuration error, never including raw values."""


def model_spec(provider: Any, model_id: str) -> dict[str, Any]:
    models = provider.get("models", []) if isinstance(provider, dict) else getattr(provider, "models", [])
    return next((model for model in models or [] if isinstance(model, dict) and model.get("id") == model_id), {})


def family_for(provider: Any, model_id: str) -> ProtocolFamily:
    provider_type = provider.get("type") if isinstance(provider, dict) else getattr(provider, "type", "")
    if provider_type == "anthropic_compatible":
        return "anthropic"
    return (
        "openai_responses" if provider_type == "openai_compatible" and model_spec(provider, model_id).get("api_surface") == "responses" else "openai"
    )


def effective_policy(model: dict[str, Any], capability: str) -> dict[str, Any]:
    if capability not in CONVERSATIONAL_CAPABILITIES:
        return {}
    policy = model.get("runtime_parameters") or {}
    defaults = policy.get("defaults") or {}
    override = (policy.get("capability_overrides") or {}).get(capability) or {}
    merged = {**defaults, **override}
    for key in ("outbound_params", "advanced"):
        merged[key] = {**(defaults.get(key) or {}), **(override.get(key) or {})}
    if override.get("thinking_mode") in {"off", "omit"}:
        for key in ("thinking_budget", "reasoning_effort"):
            if key not in override:
                merged.pop(key, None)
    if capability != "tool":
        merged["outbound_params"].pop("parallel_tool_calls", None)
    return {key: value for key, value in merged.items() if value not in ({}, None)}


def policy_origin(model: dict[str, Any], capability: str, key: str, child: str | None = None) -> str:
    override = ((model.get("runtime_parameters") or {}).get("capability_overrides") or {}).get(capability) or {}
    return "capability_override" if (child in (override.get(key) or {}) if child else key in override) else "model_default"


def _wire_roots(policy: dict[str, Any], profile: dict[str, Any] | None, family: ProtocolFamily) -> set[str]:
    roots = set(policy.get("outbound_params") or {})
    if "max_tokens" in roots:
        roots.update({"max_output_tokens", "max_completion_tokens"})
    if "thinking_mode" in policy or "thinking_budget" in policy:
        roots.update(
            {"enable_thinking", "thinking_budget"}
            if profile and profile["family"] == "openai"
            else {"reasoning"}
            if family == "openai_responses"
            else {"thinking"}
        )
    if "reasoning_effort" in policy:
        roots.add("reasoning" if family == "openai_responses" else "output_config" if family == "anthropic" else "reasoning_effort")
    return roots


def _validate_advanced(raw: Any, where: str) -> None:
    from bowhead_router.transport import is_sensitive_query_name

    if not isinstance(raw, dict) or len(raw) > 32:
        raise RuntimeParametersError(f"{where}: use a JSON object with at most 32 fields")
    try:
        encoded = json.dumps(raw, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise RuntimeParametersError(f"{where}: values must be finite static JSON") from exc
    if len(encoded.encode()) > 16384:
        raise RuntimeParametersError(f"{where}: JSON must not exceed 16 KiB")

    def check(value: Any, depth: int) -> None:
        if depth > 8:
            raise RuntimeParametersError(f"{where}: JSON nesting must not exceed 8 levels")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or key.startswith("$bowhead"):
                    raise RuntimeParametersError(f"{where}: executable templates are not allowed")
                check(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                check(child, depth + 1)

    for key, value in raw.items():
        reviewed_token_fields = {"max_tokens", "max_output_tokens", "max_completion_tokens"}
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 80
            or "." in key
            or key.lower() in PROTECTED_FIELDS
            or (is_sensitive_query_name(key) and key not in reviewed_token_fields)
        ):
            raise RuntimeParametersError(f"{where}: use an unprotected exact top-level wire field name (no dotted keys)")
        check(value, 0)


def runtime_parameters_schema() -> dict[str, Any]:
    """Describe the same versioned fields accepted by runtime and configuration validation."""
    from bowhead_router.outbound import outbound_parameters_schema

    outbound = outbound_parameters_schema()
    policy = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "thinking_mode": {
                "type": "string",
                "enum": sorted({mode for profile in PROFILES.values() for mode in profile["thinking_modes"]}),
                "description": "Use a thinking mode supported by the selected profile.",
            },
            "reasoning_effort": {
                "type": "string",
                "enum": sorted({"omit", *(effort for profile in PROFILES.values() for effort in profile["efforts"])}),
                "description": "Profile-supported reasoning effort, or omit to remove the wire field.",
            },
            "thinking_budget": {
                "type": "integer",
                "minimum": 1,
                "x-bowhead-field": {"unit": "tokens"},
                "description": "Manual thinking tokens; the selected profile's bounds and model output headroom also apply.",
            },
            "outbound_params": {**outbound, "description": "Typed sampling, output-token and parallel-tool controls."},
            "advanced": {
                "type": "object",
                "maxProperties": 32,
                "description": "Static JSON only: maximum 16 KiB and 8 levels; protected request fields and credentials are forbidden. Original validation checks collisions.",
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "profile": {
                "type": ["string", "null"],
                "enum": [None, *PROFILES],
                "description": "Versioned wire profile matching the provider and API surface; use returned profile metadata.",
            },
            "defaults": {**policy, "description": "Shared model defaults for declared Chat, Tool and Vision capabilities."},
            "capability_overrides": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    cap: {
                        **policy,
                        "description": "Override the shared model policy for this declared capability; omitted fields inherit shared defaults.",
                    }
                    for cap in sorted(CONVERSATIONAL_CAPABILITIES)
                },
                "description": "Field-level exceptions for capabilities declared by this model.",
            },
        },
    }


def validate_runtime_parameters(provider: dict[str, Any], model: dict[str, Any]) -> None:
    raw = model.get("runtime_parameters")
    if raw is None:
        return
    where = f"models[{model.get('id')}].runtime_parameters"
    schema = runtime_parameters_schema()
    if not isinstance(raw, dict) or set(raw) - set(schema["properties"]):
        raise RuntimeParametersError(f"{where}: allowed keys are profile, defaults and capability_overrides")
    profile_id = raw.get("profile")
    profile = PROFILES.get(profile_id) if isinstance(profile_id, str) else None
    if profile_id is not None and profile is None:
        raise RuntimeParametersError(f"{where}.profile: select a registered versioned profile")
    family = family_for(provider, str(model.get("id")))
    if profile and (profile["family"] != family or provider.get("type") not in profile["provider_types"]):
        raise RuntimeParametersError(f"{where}.profile: select a profile matching the provider and API surface")
    caps = set(model.get("capabilities") or []) & CONVERSATIONAL_CAPABILITIES
    if not caps:
        raise RuntimeParametersError(f"{where}: declare Chat, Tool or Vision before configuring request parameters")
    defaults = raw.get("defaults", {})
    overrides = raw.get("capability_overrides", {})
    if not isinstance(defaults, dict) or not isinstance(overrides, dict) or set(overrides) - caps:
        raise RuntimeParametersError(f"{where}.capability_overrides: use declared Chat, Tool or Vision capabilities")
    for scope, policy in [("defaults", defaults), *overrides.items()]:
        scope_where = f"{where}.{scope}"
        if not isinstance(policy, dict) or set(policy) - set(schema["properties"]["defaults"]["properties"]):
            raise RuntimeParametersError(f"{scope_where}: unsupported parameter policy field")
        mode = policy.get("thinking_mode")
        if mode is not None and (not profile or mode not in profile["thinking_modes"]):
            raise RuntimeParametersError(f"{scope_where}.thinking_mode: choose a mode supported by the selected profile")
        effort = policy.get("reasoning_effort")
        if effort is not None and (not profile or not profile["efforts"] or effort not in [*profile["efforts"], "omit"]):
            raise RuntimeParametersError(f"{scope_where}.reasoning_effort: choose a supported effort")
        budget = policy.get("thinking_budget")
        if budget is not None and (
            not profile
            or "budget_min" not in profile
            or isinstance(budget, bool)
            or not isinstance(budget, int)
            or not profile["budget_min"] <= budget <= profile["budget_max"]
        ):
            bounds = f"{profile['budget_min']}–{profile['budget_max']}" if profile and "budget_min" in profile else "a profile with budget support"
            raise RuntimeParametersError(f"{scope_where}.thinking_budget: use {bounds} integer tokens")
        parse_outbound_params(policy.get("outbound_params"), family=family, where=f"{scope_where}.outbound_params", strict_values=True)
        if "parallel_tool_calls" in (policy.get("outbound_params") or {}) and (scope not in {"defaults", "tool"} or "tool" not in caps):
            raise RuntimeParametersError(f"{scope_where}.parallel_tool_calls: use a declared Tool capability")
        advanced = policy.get("advanced", {})
        _validate_advanced(advanced, f"{scope_where}.advanced")
        if advanced and provider.get("type") == "anthropic_compatible":
            raise RuntimeParametersError(f"{scope_where}.advanced: this adapter does not consume arbitrary JSON fields")
    for capability in caps:
        policy = effective_policy(model, capability)
        mode = policy.get("thinking_mode")
        if mode in {"off", "omit"} and ("thinking_budget" in policy or policy.get("reasoning_effort") not in {None, "omit"}):
            raise RuntimeParametersError(f"{where}.{capability}: remove effort/budget overrides before choosing Off or provider omission")
        if "thinking_budget" in policy and mode != "on":
            raise RuntimeParametersError(f"{where}.{capability}.thinking_budget: explicitly enable manual thinking")
        advanced = policy.get("advanced") or {}
        legacy = (model.get("request_body_overrides") or {}).get(capability) or {}
        output_aliases = {"max_tokens", "max_output_tokens", "max_completion_tokens"}
        advanced_output = set(advanced) & output_aliases
        if len(advanced_output) > 1:
            raise RuntimeParametersError(f"{where}.{capability}.advanced: use one output-limit field or the typed max output control")
        collisions = _wire_roots(policy, profile, family) & (set(advanced) | set(legacy))
        if advanced_output:
            collisions.update(output_aliases & set(legacy))
        # Legacy-only typed/raw overlaps keep their old precedence. A new layer
        # must be explicit about ownership and cannot silently choose a winner.
        if collisions or set(advanced) & set(legacy):
            raise RuntimeParametersError(f"{where}.{capability}.advanced: remove overlapping typed/legacy fields before saving")
        recognized = {"enable_thinking", "thinking_budget", "thinking", "reasoning", "reasoning_effort", "output_config"} & set(advanced)
        if recognized and profile:
            raise RuntimeParametersError(f"{where}.{capability}.advanced: use the profile-gated reasoning controls for recognized fields")
        generation = {
            key: {"mode": "fixed", "value": value}
            for key, value in advanced.items()
            if key in {"temperature", "top_p", "max_tokens", "parallel_tool_calls"}
        }
        for alias in ("max_output_tokens", "max_completion_tokens"):
            if alias in advanced:
                generation["max_tokens"] = {"mode": "fixed", "value": advanced[alias]}
        parse_outbound_params(generation, family=family, where=f"{where}.{capability}.advanced", strict_values=True)
        if "parallel_tool_calls" in advanced and capability != "tool":
            raise RuntimeParametersError(f"{where}.{capability}.advanced: parallel_tool_calls requires Tool scope")
        if "enable_thinking" in advanced and not isinstance(advanced["enable_thinking"], bool):
            raise RuntimeParametersError(f"{where}.{capability}.advanced: enable_thinking must be a JSON boolean")
        if provider.get("type") == "custom_http" and (advanced or policy.get("outbound_params")):
            from bowhead_router.transport import render_custom_http_body

            candidate = copy.deepcopy(provider)
            target = model_spec(candidate, str(model["id"]))
            constants = dict(advanced)
            for key, rule in (policy.get("outbound_params") or {}).items():
                if rule.get("mode") != "fixed":
                    raise RuntimeParametersError(
                        f"{where}.{capability}.outbound_params: Custom HTTP supports only explicit fixed transport constants in this layer"
                    )
                constants[key] = rule["value"]
            target.setdefault("request_body_overrides", {}).setdefault(capability, {}).update(constants)
            render_custom_http_body(
                candidate,
                model_id=str(model["id"]),
                capability=capability,
                variables={
                    "model": str(model["id"]),
                    "deployment": str(model.get("deployment") or model["id"]),
                    "messages": [],
                    "tools": [],
                    "tool_choice": None,
                    "stream": False,
                    "max_output_tokens": 4000,
                    "input": [],
                },
            )
        if provider.get("type") != "custom_http":
            from bowhead_router.output_budget import initial_output_allowance
            from bowhead_router.types import LLMRequest

            baseline = {
                "temperature": 0.7,
                "max_tokens": initial_output_allowance(LLMRequest(model=str(model["id"]), messages=[]), provider),
                "thinking": {"type": "disabled"},
            }
            if family == "openai_responses":
                baseline["max_output_tokens"] = baseline.pop("max_tokens")
            if provider.get("type") != "anthropic_compatible":
                baseline.update(legacy)
            apply_outbound_params(baseline, resolve_outbound_params(provider, model_id=str(model["id"]), family=family), family=family)
            apply_runtime_parameters(baseline, provider, model_id=str(model["id"]), capability=capability, family=family)


def effective_outbound_params(provider: Any, *, model_id: str, capability: str, family: ProtocolFamily) -> OutboundParams:
    baseline = resolve_outbound_params(provider, model_id=model_id, family=family)
    policy = effective_policy(model_spec(provider, model_id), capability)
    rules = dict(baseline.rules)
    advanced = policy.get("advanced") or {}
    rules.update(
        {
            key: ParamRule(mode="fixed", value=advanced[key])
            for key in ("temperature", "top_p", "max_tokens", "parallel_tool_calls")
            if key in advanced
        }
    )
    for alias in ("max_output_tokens", "max_completion_tokens"):
        if alias in advanced:
            rules["max_tokens"] = ParamRule(mode="fixed", value=advanced[alias])
    rules.update(
        parse_outbound_params(policy.get("outbound_params"), family=family, where="runtime_parameters.outbound_params", strict_values=True).rules
    )
    return OutboundParams(rules=rules)


def apply_runtime_parameters(body: dict[str, Any], provider: Any, *, model_id: str, capability: str, family: ProtocolFamily) -> dict[str, Any]:
    """Apply this layer after the legacy baseline, before bounded adaptations."""
    model = model_spec(provider, model_id)
    policy = effective_policy(model, capability)
    if not policy:
        return body
    provider_type = provider.get("type") if isinstance(provider, dict) else getattr(provider, "type", "")
    advanced = copy.deepcopy(policy.get("advanced") or {})
    if provider_type != "custom_http":
        # Reviewed output aliases have one semantic owner. Normalize them before
        # merging so Responses/Azure cannot receive two competing output keys.
        for alias in ("max_output_tokens", "max_completion_tokens"):
            if alias in advanced:
                advanced["max_tokens"] = advanced.pop(alias)
        if "max_tokens" in advanced:
            advanced[wire_param_name("max_tokens", family)] = advanced.pop("max_tokens")
    body.update(advanced)
    params = (
        parse_outbound_params(policy.get("outbound_params"), family=family, where="runtime_parameters.outbound_params", strict_values=True)
        if provider_type == "custom_http"
        else effective_outbound_params(provider, model_id=model_id, capability=capability, family=family)
    )
    output_key = "max_output_tokens" if family == "openai_responses" else "max_tokens"
    cap = (policy.get("outbound_params") or {}).get("max_tokens") or {}
    if cap.get("mode") == "cap" and body.get(output_key) is None:
        body[output_key] = cap["value"]
    apply_outbound_params(body, params, family=family)
    profile = PROFILES.get((model.get("runtime_parameters") or {}).get("profile"))
    mode = policy.get("thinking_mode")
    budget = policy.get("thinking_budget")
    if profile and profile["family"] == "openai":
        if mode == "omit":
            body.pop("enable_thinking", None)
            body.pop("thinking_budget", None)
        elif mode is not None:
            body["enable_thinking"] = mode == "on"
        if budget is not None:
            body["thinking_budget"] = budget
    elif family == "anthropic":
        if mode == "omit":
            body.pop("thinking", None)
        elif mode is not None:
            body["thinking"] = {"type": {"off": "disabled", "on": "enabled", "adaptive": "adaptive"}[mode]}
            if budget is not None:
                body["thinking"]["budget_tokens"] = budget
        if mode == "on":
            effective_budget = (body.get("thinking") or {}).get("budget_tokens")
            if not isinstance(effective_budget, int) or effective_budget < 1024:
                raise RuntimeParametersError("thinking_budget: manual thinking requires at least 1024 tokens; set an explicit budget")
            if body.get("temperature") not in {None, 1} or "top_p" in body:
                raise RuntimeParametersError("temperature/top_p: manual thinking requires temperature 1 or omission, and top_p omission")
    effort = policy.get("reasoning_effort")
    if effort is not None:
        root = "reasoning" if family == "openai_responses" else "output_config"
        config = dict(body.get(root) or {})
        if effort == "omit":
            config.pop("effort", None)
        else:
            config["effort"] = effort
        if config:
            body[root] = config
        else:
            body.pop(root, None)
    if family == "openai_responses" and mode == "omit":
        config = dict(body.get("reasoning") or {})
        config.pop("effort", None)
        if config:
            body["reasoning"] = config
        else:
            body.pop("reasoning", None)
    output = body.get("max_output_tokens" if family == "openai_responses" else "max_tokens")
    if family == "anthropic" and budget is not None and (not isinstance(output, int) or budget >= output):
        raise RuntimeParametersError("thinking_budget: leave output headroom by increasing max output or lowering the thinking budget")
    if profile:
        temperature = body.get("temperature")
        if temperature is not None and (
            isinstance(temperature, bool)
            or not isinstance(temperature, int | float)
            or not math.isfinite(temperature)
            or not 0 <= temperature <= profile["temperature_max"]
        ):
            raise RuntimeParametersError("temperature: use a finite value within the selected profile's range")
    return body


def runtime_policy_shape(provider: Any, model_id: str, capability: str) -> dict[str, Any]:
    """Only effective rules enter capability evidence, not shadowed defaults."""
    if capability not in CONVERSATIONAL_CAPABILITIES:
        return {}
    model = model_spec(provider, model_id)
    raw = model.get("runtime_parameters") or {}
    if not raw:
        return {}
    return {"contract": CONTRACT_VERSION, "profile": raw.get("profile"), "policy": effective_policy(model, capability)}
