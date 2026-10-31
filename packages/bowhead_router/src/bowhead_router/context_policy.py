"""Typed model context preferences shared by configuration and execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bowhead_router.output_budget import RenderedRequestBudget
from bowhead_router.runtime_parameters import model_spec

CONTEXT_POLICY_VERSION = "context-policy.v1"
MAX_SAFE_TOKEN_INTEGER = 9_007_199_254_740_991


class ContextPolicyError(ValueError):
    """An invalid saved preference must not silently turn compaction on."""


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    enabled: bool = True
    trigger_input_tokens: int | None = None
    keep_recent_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TargetContextBudget:
    """Read-only facts for one configured physical target, without prompt data."""

    provider: str
    model: str
    configuration_version: int | None
    policy: ContextPolicy
    budget: RenderedRequestBudget
    policy_version: str = CONTEXT_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class ResolvedContextPolicy:
    trigger_input_tokens: int
    keep_recent_tokens: int
    growth_tokens: int
    summary_output_tokens: int
    limiting_reasons: tuple[str, ...]
    version: str = CONTEXT_POLICY_VERSION


def summary_output_target(context_window: int) -> int:
    """Bound summary intent across small and large windows before rendering."""
    return min(8192, max(1024, context_window // 64))


def resolve_context_policy(
    policy: ContextPolicy,
    *,
    ordinary: RenderedRequestBudget,
    summary: RenderedRequestBudget,
    fixed_input_tokens: int,
    max_growth_tokens: int,
) -> ResolvedContextPolicy:
    """Leave room for both rendered calls and a bounded ingestion increment.

    The owner supplies the measured fixed prefix and the Tool path's ingestion
    bound. Growth reservation is capped at one eighth of usable input: a single
    unusually large result still needs actual pre-send admission. Tail selection
    preserves complete required transactions even when they exceed this target.
    Final candidate rendering remains authoritative.
    """
    if type(fixed_input_tokens) is not int or fixed_input_tokens < 0 or type(max_growth_tokens) is not int or max_growth_tokens < 0:
        raise ContextPolicyError("context_policy_accounting_invalid")
    if ordinary.context_window != summary.context_window:
        raise ContextPolicyError("context_policy_target_mismatch")
    summary_overhead = max(0, summary.input_estimate - ordinary.input_estimate)
    safe_limit = max(0, min(ordinary.input_limit, summary.input_limit - summary_overhead))
    usable = max(0, safe_limit - fixed_input_tokens)
    growth = min(max_growth_tokens, usable // 8)
    automatic_trigger = max(0, safe_limit - growth)
    trigger = min(automatic_trigger, policy.trigger_input_tokens) if policy.trigger_input_tokens is not None else automatic_trigger
    recent_limit = max(0, trigger - growth - fixed_input_tokens - summary.required_output)
    automatic_recent = min(usable // 3, 4 * summary.required_output + growth, recent_limit)
    recent = min(policy.keep_recent_tokens, recent_limit) if policy.keep_recent_tokens is not None else automatic_recent
    reasons = []
    if policy.trigger_input_tokens is not None and trigger != policy.trigger_input_tokens:
        reasons.append("trigger_limited_by_request_capacity")
    if policy.keep_recent_tokens is not None and recent != policy.keep_recent_tokens:
        reasons.append("recent_limited_by_request_capacity")
    if fixed_input_tokens >= trigger:
        reasons.append("fixed_input_reaches_trigger")
    return ResolvedContextPolicy(trigger, recent, growth, summary.required_output, tuple(reasons))


def parse_context_policy(value: Any, *, path: str = "context_policy") -> ContextPolicy:
    """Resolve omission/reset while keeping explicit false and typed numbers."""
    if value is None:
        return ContextPolicy()
    if not isinstance(value, dict):
        raise ContextPolicyError(f"{path} must be an object or null")
    allowed = {"enabled", "trigger_input_tokens", "keep_recent_tokens"}
    if set(value) - allowed:
        raise ContextPolicyError(f"{path} contains unsupported fields")
    enabled = value.get("enabled", True)
    if type(enabled) is not bool:
        raise ContextPolicyError(f"{path}.enabled must be a boolean")
    numbers: dict[str, int | None] = {}
    for name in ("trigger_input_tokens", "keep_recent_tokens"):
        number = value.get(name)
        if number is not None and (type(number) is not int or not 1 <= number <= MAX_SAFE_TOKEN_INTEGER):
            raise ContextPolicyError(f"{path}.{name} must be a positive safe JSON integer or null")
        numbers[name] = number
    trigger, recent = numbers["trigger_input_tokens"], numbers["keep_recent_tokens"]
    if trigger is not None and recent is not None and recent >= trigger:
        raise ContextPolicyError(f"{path}.keep_recent_tokens must be smaller than trigger_input_tokens")
    return ContextPolicy(enabled=enabled, trigger_input_tokens=trigger, keep_recent_tokens=recent)


def model_context_policy(provider: Any, model_id: str) -> ContextPolicy:
    """Read only the exact model's declaration from the admitted configuration."""
    return parse_context_policy(model_spec(provider, model_id).get("context_policy"))


def fallback_context_policy(admitted: ContextPolicy, destination: ContextPolicy) -> ContextPolicy:
    """Either explicit off disables automatic summarization on this physical path."""
    return ContextPolicy(
        enabled=admitted.enabled and destination.enabled,
        trigger_input_tokens=destination.trigger_input_tokens,
        keep_recent_tokens=destination.keep_recent_tokens,
    )
