"""Pure model-directory diagnostics shared by routing and readiness.

This leaf module deliberately depends on neither ``bowhead_core`` nor router
runtime classes. Team config validation, tier expansion, and read-only status
projection can therefore share one capability interpretation without creating
an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CAPABILITY_VOCAB = frozenset({"chat", "tool", "vision", "embed", "rerank", "think"})


@dataclass(frozen=True, slots=True)
class ModelRefDiagnostics:
    provider_exists: bool
    provider_has_secret: bool
    model_declared: bool
    capabilities: frozenset[str]


def inferred_provider_capabilities(supports: Any) -> frozenset[str]:
    """Capabilities inherited only by legacy string model entries."""
    supports = supports if isinstance(supports, dict) else {}
    capabilities = {"chat"}
    if supports.get("tool_use") or supports.get("tools"):
        capabilities.add("tool")
    if supports.get("vision"):
        capabilities.add("vision")
    if supports.get("extended_thinking") or supports.get("thinking"):
        capabilities.add("think")
    return frozenset(capabilities)


def provider_has_team_secret(providers: Any, provider_name: str) -> bool:
    if not isinstance(providers, dict):
        return False
    provider = providers.get(provider_name)
    if not isinstance(provider, dict):
        return False
    auth = provider.get("auth")
    if isinstance(auth, dict) and auth.get("type") == "none":
        return True
    if isinstance(auth, dict) and isinstance(auth.get("secret"), str) and bool(auth["secret"].strip()):
        return True
    return any(isinstance(provider.get(field), str) and bool(provider[field].strip()) for field in ("api_key", "secret_key"))


def effective_model_capabilities(
    providers: Any,
    provider_name: str,
    model_id: str,
) -> frozenset[str] | None:
    """Return effective capabilities, or ``None`` when the model is undeclared.

    Legacy string models inherit provider ``supports``. Rich object models use
    their own explicit ``capabilities``; an omitted/empty list stays empty.
    """
    if not isinstance(providers, dict):
        return None
    provider = providers.get(provider_name)
    if not isinstance(provider, dict):
        return None
    models = provider.get("models")
    if not isinstance(models, list):
        return None
    for model in models:
        if isinstance(model, str) and model == model_id:
            return inferred_provider_capabilities(provider.get("supports"))
        if isinstance(model, dict) and str(model.get("id") or "") == model_id:
            capabilities = model.get("capabilities")
            if not isinstance(capabilities, list):
                return frozenset()
            return frozenset(capability for capability in capabilities if isinstance(capability, str) and capability in CAPABILITY_VOCAB)
    return None


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


# Context window (tokens) used for a model whose connection does not declare one (design:
# agent-release-beginner-experience §8). 128K; an administrator can enter the exact value.
DEFAULT_CONTEXT_WINDOW = 131_072

# Reviewed catalog of documented windows, verified against provider documentation on 2026-10-20
# (sources in docs/design/agent-release-beginner-experience.md §8.2). Rule: the documented maximum
# input when docs give one separately from the full window, the exact number when docs give one, and
# 1,000,000 / 128,000 / 200,000 when docs state only "1M" / "128K" / "200K". Keys are model ids as
# provider documentation spells them, compared case-insensitively.
KNOWN_CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-flash": 1_000_000,
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    "qwen3.8-max": 1_000_000,
    "qwen3.7-plus": 1_000_000,
    "qwen3.8-flash": 1_000_000,
    "qwen3.6-plus": 1_000_000,
    "qwen-plus": 1_000_000,
    "qwen-flash": 1_000_000,
    "qwen3.7-plus-us": 991_808,
    "qwen-max": 128_000,
    "gpt-6-astra": 1_050_000,
    "gpt-5": 272_000,
    "gpt-5-mini": 272_000,
    "claude-opus-5-5": 1_000_000,
    "claude-sonnet-5-5": 1_000_000,
    "gemini-3.1-pro-preview": 1_048_576,
    "gemini-3.6-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "kimi-k3": 1_048_576,
    "kimi-k2.6": 262_144,
    "glm-5.3": 1_000_000,
    "glm-4.6": 200_000,
    "minimax-m3": 1_000_000,
}


def catalog_context_window(model_id: str) -> int | None:
    """The reviewed catalog window for a model id, or ``None`` when the catalog does not know it."""
    return KNOWN_CONTEXT_WINDOWS.get(str(model_id or "").strip().casefold())


AUTO_OUTPUT_CEILING = 256_000
UNKNOWN_OUTPUT_CAPACITY = 32_000
OUTPUT_BUDGET_VERSION = "model-output-budget.v1"
# Exact official IDs reviewed on 2026-10-27; deployment aliases require declarations.
KNOWN_OUTPUT_CAPACITIES: dict[str, int] = {
    "deepseek-flash": 393_216,
    "deepseek-v4-flash": 393_216,
    "deepseek-v4-pro": 393_216,
    "gpt-6.1-sol": 128_000,
    "claude-opus-5-5": 128_000,
    "claude-sonnet-5-5": 128_000,
}
OUTPUT_CAPACITY_SOURCES = {
    "deepseek": "https://api-docs.deepseek.com/api/create-chat-completion/",
    "openai": "https://developers.openai.com/api/docs/models/gpt-6.1-sol",
    "anthropic": "https://platform.claude.com/docs/en/models/overview",
}


@dataclass(frozen=True, slots=True)
class TokenCapacity:
    value: int
    source: str


def model_token_capacity(provider: Any, model_id: str, *, field: str = "max_output_tokens") -> TokenCapacity:
    """Resolve an exact connection's capacity without treating a fallback as evidence."""
    provider = provider if isinstance(provider, dict) else {}
    for model in provider.get("models") or []:
        if isinstance(model, dict) and model.get("id") == model_id:
            value = _positive_int(model.get(field))
            if value is not None:
                return TokenCapacity(value, "model")
            break
    value = _positive_int(provider.get(field))
    if value is not None:
        return TokenCapacity(value, "provider")
    catalog = KNOWN_CONTEXT_WINDOWS if field == "context_window" else KNOWN_OUTPUT_CAPACITIES
    value = catalog.get(str(model_id).strip().casefold())
    if value is not None:
        return TokenCapacity(value, "catalog")
    return TokenCapacity(DEFAULT_CONTEXT_WINDOW if field == "context_window" else UNKNOWN_OUTPUT_CAPACITY, "fallback")


def output_budget_defaults() -> dict[str, Any]:
    """Read-only defaults for settings; callers never save this projection as policy."""
    return {
        "version": OUTPUT_BUDGET_VERSION,
        "automatic_ceiling": AUTO_OUTPUT_CEILING,
        "unknown_capacity": UNKNOWN_OUTPUT_CAPACITY,
        "models": dict(KNOWN_OUTPUT_CAPACITIES),
    }


def effective_context_window(
    providers: Any,
    provider_name: str,
    model_id: str,
) -> int:
    """Context window (tokens) for a model: declared, else the catalog value, else the default.

    Rich object models may carry ``context_window``; a provider-level ``context_window`` is the
    default for every model under it (legacy string models can only be declared that way).
    Model-level wins. An undeclared window falls back to the reviewed catalog and then to
    ``DEFAULT_CONTEXT_WINDOW`` (owner decision 2026-10-20: unknown models are bounded at 131,072
    until an administrator enters the exact value), so a window always exists.
    """
    declared: int | None = None
    provider = providers.get(provider_name) if isinstance(providers, dict) else None
    if isinstance(provider, dict):
        provider_default = _positive_int(provider.get("context_window"))
        for model in provider.get("models") if isinstance(provider.get("models"), list) else []:
            if isinstance(model, str) and model == model_id:
                declared = provider_default
                break
            if isinstance(model, dict) and str(model.get("id") or "") == model_id:
                declared = _positive_int(model.get("context_window")) or provider_default
                break
    return declared or catalog_context_window(model_id) or DEFAULT_CONTEXT_WINDOW


def diagnose_model_ref(
    providers: Any,
    provider_name: str,
    model_id: str,
) -> ModelRefDiagnostics:
    provider_exists = isinstance(providers, dict) and isinstance(
        providers.get(provider_name),
        dict,
    )
    capabilities = effective_model_capabilities(providers, provider_name, model_id)
    return ModelRefDiagnostics(
        provider_exists=provider_exists,
        provider_has_secret=provider_has_team_secret(providers, provider_name),
        model_declared=capabilities is not None,
        capabilities=capabilities or frozenset(),
    )
