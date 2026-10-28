"""Compile synthetic requests with actual adapter builders, without clients."""

from __future__ import annotations

import copy
import math
from dataclasses import MISSING, fields
from typing import Any

from bowhead_router.runtime_parameters import effective_policy, family_for, model_spec, policy_origin
from bowhead_router.types import ImageRef, LLMRequest, Message, ToolSchema

_SYNTHETIC_IMAGE = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jLpUAAAAASUVORK5CYII="


def synthetic_request(model_id: str, capability: str, *, max_output_tokens: int | None = 4000) -> LLMRequest:
    message = Message(role="user", content="Reply with OK.")
    if capability == "vision":
        message.images = [ImageRef(base64=_SYNTHETIC_IMAGE, mime="image/png")]
    tools = (
        [ToolSchema(name="bowhead_connection_probe", description="Connection probe", input_schema={"type": "object", "properties": {}})]
        if capability == "tool"
        else []
    )
    if tools:
        message.content = "Call the bowhead_connection_probe tool now."
    return LLMRequest(model=model_id, messages=[message], tools=tools, max_output_tokens=max_output_tokens)


def compile_request_body(provider: dict[str, Any], request: LLMRequest, *, stream: bool = True) -> dict[str, Any]:
    """Use the adapter's final builder without running client initialization.

    The builders are synchronous and consume only dataclass configuration. This
    detached compiler has no HTTP client, so preview cannot dispatch a request.
    Runtime still initializes and owns its ordinary adapter clients.
    """
    from bowhead_router.adapters.anthropic_compat import AnthropicCompatibleAdapter
    from bowhead_router.adapters.openai_compat import OpenAICompatibleAdapter

    cls = AnthropicCompatibleAdapter if provider.get("type") == "anthropic_compatible" else OpenAICompatibleAdapter
    adapter = cls.__new__(cls)
    for field in fields(cls):
        if not field.init:
            continue
        if field.default is not MISSING:
            setattr(adapter, field.name, field.default)
        elif field.default_factory is not MISSING:
            setattr(adapter, field.name, field.default_factory())
    adapter.base_url = str(provider.get("base_url") or provider.get("endpoint") or "")
    adapter.api_key = ""
    adapter.provider_name = "parameter_preview"
    adapter.provider_options = copy.deepcopy(provider)
    if isinstance(adapter, OpenAICompatibleAdapter):
        adapter.adapter_type = str(provider.get("type") or "openai_compatible")
    return adapter._build_body(request, stream=stream)


def parameter_preview(provider: dict[str, Any], model_id: str, capability: str) -> dict[str, Any]:
    """Return only reviewed parameter projections, never arbitrary draft values."""
    request = synthetic_request(model_id, capability, max_output_tokens=None)
    body = compile_request_body(provider, request)
    model = model_spec(provider, model_id)
    policy = effective_policy(model, capability)
    family = family_for(provider, model_id)
    raw = (model.get("request_body_overrides") or {}).get(capability) or {}
    rows: list[dict[str, Any]] = []
    wire_keys = {
        "temperature": "temperature",
        "top_p": "top_p",
        "max_tokens": "max_output_tokens"
        if family == "openai_responses"
        else "max_completion_tokens"
        if "max_completion_tokens" in body
        else "max_tokens",
        "parallel_tool_calls": "parallel_tool_calls",
    }
    for key, wire_key in wire_keys.items():
        value = body.get(wire_key)
        if key == "parallel_tool_calls" and family == "anthropic":
            disabled = (body.get("tool_choice") or {}).get("disable_parallel_tool_use")
            value = None if disabled is None else not disabled
            wire_key = "tool_choice.disable_parallel_tool_use"
        source = "runtime_caller"
        rule = (policy.get("outbound_params") or {}).get(key)
        advanced_names = {key, "max_output_tokens", "max_completion_tokens"} if key == "max_tokens" else {key}
        if rule:
            source = policy_origin(model, capability, "outbound_params", key)
        elif advanced_names & set(policy.get("advanced") or {}):
            source = "advanced"
        elif key in (model.get("outbound_params") or {}):
            source = "legacy_model_setting"
        elif key in (provider.get("outbound_params") or {}):
            source = "provider_setting"
        elif key in raw:
            source = "legacy_advanced"
        if key == "max_tokens" and source == "runtime_caller" and (request.output_budget or {}).get("automatic"):
            source = "automatic_model_budget"
        if value is None and not rule:
            continue
        if value is not None and not (
            isinstance(value, bool)
            if key == "parallel_tool_calls"
            else isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        ):
            value = "[redacted legacy value]"
        rows.append(
            {
                "field": wire_key,
                "value": value,
                "omitted": value is None,
                "source": source,
                "variable": source == "runtime_caller" or (rule or {}).get("mode") == "cap",
            }
        )
    reasoning_fields = [
        ("thinking_mode", "enable_thinking" if family == "openai" else "thinking.type" if family == "anthropic" else "reasoning.effort"),
        (
            "reasoning_effort",
            "reasoning.effort" if family == "openai_responses" else "output_config.effort" if family == "anthropic" else "reasoning_effort",
        ),
        ("thinking_budget", "thinking_budget" if family == "openai" else "thinking.budget_tokens"),
    ]
    projected: set[str] = set()
    for key, wire in reasoning_fields:
        if wire in projected:
            continue
        root, _, child = wire.partition(".")
        nested = body.get(root)
        value = nested.get(child) if child and isinstance(nested, dict) else nested if not child else None
        if value is None and key not in policy:
            continue
        # A shared wire root uses the most specific declared semantic field.
        semantic_key = "reasoning_effort" if wire == "reasoning.effort" and "reasoning_effort" in policy else key
        source = (
            policy_origin(model, capability, semantic_key)
            if semantic_key in policy
            else "advanced"
            if root in (policy.get("advanced") or {})
            else "legacy_advanced"
            if root in raw
            else "adapter_requirement"
        )
        valid = (
            isinstance(value, bool)
            if wire == "enable_thinking"
            else isinstance(value, int) and not isinstance(value, bool)
            if key == "thinking_budget"
            else isinstance(value, str) and value in {"enabled", "disabled", "adaptive"}
            if wire == "thinking.type"
            else isinstance(value, str) and value in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
        )
        rows.append(
            {
                "field": wire,
                "value": value if valid else None,
                "omitted": value is None,
                "redacted": value is not None and not valid,
                "source": source,
                "variable": semantic_key not in policy,
            }
        )
        projected.add(wire)
    recognized_roots = {wire.partition(".")[0] for _, wire in reasoning_fields}
    for key in set(policy.get("advanced") or {}) - set(wire_keys) - {"max_output_tokens", "max_completion_tokens"} - recognized_roots:
        rows.append({"field": key, "redacted": True, "source": "advanced", "acceptance": "unverified"})
    return {
        "capability": capability,
        "profile": (model.get("runtime_parameters") or {}).get("profile"),
        "parameters": rows,
        "synthetic": True,
        "provider_request_count": 0,
        "configuration_writes": 0,
        "output_budget": request.output_budget,
    }
