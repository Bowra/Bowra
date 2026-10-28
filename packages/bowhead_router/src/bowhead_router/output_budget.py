"""Resolve model output allowances after all explicit request policies apply."""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

from bowhead_router.input_admission import LLMInputAdmissionError
from bowhead_router.model_capabilities import AUTO_OUTPUT_CEILING, OUTPUT_BUDGET_VERSION, TokenCapacity, model_token_capacity
from bowhead_router.outbound import resolve_outbound_params
from bowhead_router.runtime_notes import OUTPUT_RECOVERY_STATE_NAME
from bowhead_router.runtime_parameters import effective_outbound_params, effective_policy, model_spec
from bowhead_router.token_estimator import estimate_tokens

if TYPE_CHECKING:
    from bowhead_router.outbound import ProtocolFamily
    from bowhead_router.types import LLMRequest

FRAMING_MARGIN = 1024
MIN_PROGRESS_OUTPUT = 1024
OUTPUT_FIELDS = ("max_tokens", "max_output_tokens", "max_completion_tokens")


def _recovery_allowance_note(request: LLMRequest, body: dict[str, Any], family: ProtocolFamily):
    """Append one owned text block, never search/replace user-authored content."""
    if not any(
        message.role == "user" and message.name == OUTPUT_RECOVERY_STATE_NAME and json.loads(message.content).get("attempts")
        for message in request.messages
    ):
        return lambda _: None
    messages = body.get("input" if family == "openai_responses" else "messages")
    if not isinstance(messages, list) or not messages or messages[-1].get("role") != "user":
        raise OutputBudgetError("Output recovery requires a final user instruction in the rendered transport")
    message = messages[-1]
    original = message.get("content")
    block = {"type": "input_text" if family == "openai_responses" else "text", "text": ""}
    if isinstance(original, list):
        message["content"] = [*original, block]

    def update(allowance: int | None) -> None:
        note = (
            f"This physical request allows at most {allowance} output tokens. Keep the response smaller than this maximum."
            if allowance is not None
            else "This physical request omits the output limit. The server's actual limit is unknown; keep the response small."
        )
        if isinstance(original, list):
            block["text"] = note
        elif isinstance(original, str):
            message["content"] = original + "\n\n" + note
        else:
            raise OutputBudgetError("Output recovery instruction cannot be rendered by this transport")

    return update


class OutputBudgetError(LLMInputAdmissionError):
    """An impossible local allowance must never be retried as a provider error."""


def initial_output_allowance(request: LLMRequest, provider: Any) -> int:
    """Supply automatic intent before applying fixed/cap/omit policies."""
    if request.max_output_tokens is not None:
        if type(request.max_output_tokens) is not int or request.max_output_tokens <= 0:
            raise OutputBudgetError("max_output_tokens must be a positive integer or automatic")
        return request.max_output_tokens
    return min(model_token_capacity(provider, request.model).value, AUTO_OUTPUT_CEILING)


def output_reservation(provider: Any, model: str, caller: int | None, *, capability: str, family: ProtocolFamily, thinking_budget: int = 0) -> int:
    """Reserve required output only; desired automatic capacity must not evict input."""
    rule = effective_outbound_params(provider, model_id=model, capability=capability, family=family).rule("max_tokens")
    policy = effective_policy(model_spec(provider, model), capability)
    budget = 0 if policy.get("thinking_mode") in {"off", "omit", "adaptive"} else policy.get("thinking_budget", thinking_budget)
    minimum = max(MIN_PROGRESS_OUTPUT, budget + MIN_PROGRESS_OUTPUT if type(budget) is int and budget > 0 else 0)
    if rule.mode == "fixed":
        return int(rule.value)
    if rule.mode == "omit":
        return minimum
    raw = (model_spec(provider, model).get("request_body_overrides") or {}).get(capability) or {}
    raw_value = next((raw[key] for key in OUTPUT_FIELDS if type(raw.get(key)) is int and raw[key] > 0), None)
    value = caller if caller is not None else raw_value if raw_value is not None else minimum
    legacy = resolve_outbound_params(provider, model_id=model, family=family).rule("max_tokens")
    if legacy.mode == "fixed":
        value = int(legacy.value)
    elif legacy.mode == "cap":
        value = min(value, int(legacy.value))
    return min(value, int(rule.value)) if rule.mode == "cap" else value


def estimate_body_input(body: dict[str, Any]) -> tuple[int, str]:
    """Count the rendered input with the shared estimator, excluding binary payloads."""
    media = 0

    def project(value: Any) -> Any:
        nonlocal media
        if isinstance(value, dict):
            if isinstance(value.get("type"), str) and value["type"] in {"image", "image_url", "input_image", "input_file", "document"}:
                media += 1
                return {"type": value["type"]}
            return {key: project(item) for key, item in value.items()}
        if isinstance(value, list):
            return [project(item) for item in value]
        return value

    content = {key: project(body[key]) for key in ("messages", "input", "instructions", "system", "tools", "response_format", "text") if key in body}
    # Media has provider-specific token costs. Keep coverage explicit, never count
    # base64 as text or report this estimate as a measured provider token count.
    return estimate_tokens(
        json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    ) + media * 4096, "media_estimated" if media else "text_estimated"


def finalize_output_allowance(
    request: LLMRequest, body: dict[str, Any], provider: Any, *, capability: str, family: ProtocolFamily, constant_output: bool = False
) -> None:
    """Fit only automatic output; preserve explicit choices and legal omissions."""
    fields = [key for key in OUTPUT_FIELDS if key in body]
    if len(fields) > 1:
        raise OutputBudgetError("Conflicting output token fields; keep one output parameter in the model settings")
    capacity = model_token_capacity(provider, request.model)
    window = model_token_capacity(provider, request.model, field="context_window")
    if type(request.input_admission_window) is int and request.input_admission_window > 0 and request.input_admission_window != window.value:
        window = TokenCapacity(request.input_admission_window, "resolved_target")
    rule = effective_outbound_params(provider, model_id=request.model, capability=capability, family=family).rule("max_tokens")
    diagnostics: dict[str, Any] = {
        "version": OUTPUT_BUDGET_VERSION,
        "configuration_version": request.configuration_version,
        "mode": rule.mode,
        "capacity": capacity.value,
        "capacity_source": capacity.source,
        "automatic_ceiling": AUTO_OUTPUT_CEILING,
        "context_window": window.value,
        "context_source": window.source,
        "caller_allowance": request.max_output_tokens,
        "wire_allowance": None,
    }
    request.output_budget = diagnostics
    recovery_note = _recovery_allowance_note(request, body, family)
    if not fields:
        if family == "anthropic":
            raise OutputBudgetError("Anthropic requires a positive max_tokens parameter")
        recovery_note(None)
        return
    key = fields[0]
    selected = body[key]
    if type(selected) is not int or selected <= 0:
        raise OutputBudgetError("The output token allowance must be a positive integer")
    overrides = (model_spec(provider, request.model).get("request_body_overrides") or {}).get(capability) or {}
    advanced = effective_policy(model_spec(provider, request.model), capability).get("advanced") or {}
    automatic = (
        request.max_output_tokens is None
        and rule.mode != "fixed"
        and resolve_outbound_params(provider, model_id=request.model, family=family).rule("max_tokens").mode != "fixed"
        and not constant_output
        and not any(field in overrides or field in advanced for field in OUTPUT_FIELDS)
    )
    diagnostics["automatic"] = automatic
    allowance = selected
    for _ in range(4):
        recovery_note(allowance)
        estimated, coverage = estimate_body_input(body)
        room = window.value - math.ceil(estimated * 1.15) - FRAMING_MARGIN
        input_output_room = getattr(request.input_admission, "output_room", None)
        if callable(input_output_room):
            room = min(room, input_output_room(request, body, family=family))
        fitted = min(allowance, room) if automatic else allowance
        if fitted == allowance:
            break
        allowance = fitted
    else:
        raise OutputBudgetError("Output recovery allowance could not stabilize; reduce optional input")
    if allowance <= 0 or (automatic and allowance < min(MIN_PROGRESS_OUTPUT, selected)):
        raise OutputBudgetError("Input leaves insufficient output space; reduce optional context or correct the model context window")
    if not automatic and selected > room:
        if callable(input_output_room):
            # Preserve the protected-input gate's finite failure code when it
            # is the constraint; it remains authoritative for required sources.
            request.input_admission.validate_outbound(request, body, family=family)
        raise OutputBudgetError("Configured output allowance does not fit with the input; lower the output limit or correct the context window")
    if capacity.source != "fallback" and allowance > capacity.value:
        raise OutputBudgetError("Output allowance exceeds model capacity; lower the request limit or correct the declared model output capacity")
    thinking = body.get("thinking")
    budget = thinking.get("budget_tokens") if isinstance(thinking, dict) and thinking.get("type") == "enabled" else body.get("thinking_budget")
    if type(budget) is int and budget >= allowance:
        raise OutputBudgetError("Thinking budget leaves no output headroom; lower thinking budget or increase available output space")
    body[key] = allowance
    diagnostics.update(
        wire_field=key, wire_allowance=allowance, input_estimate=estimated, estimate_coverage=coverage, context_reduced=allowance < selected
    )
