"""Versioned credit pricing for historical v1 and classified model usage in v2.

V1 rounds combined input and output tokens. V2 weights disjoint input, cached
input, and output tokens per model, then rounds once at the settlement boundary.
USD estimates on turns are separate from credit billing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from types import MappingProxyType

#: Current pricing-rule version. Bump when changing formulas; historical ledger rows use it to interpret the original algorithm.
RATE_CARD_VERSION = "v2"

#: v1: 1 credit per 1000 tokens (input + output combined), rounded up.
_TOKENS_PER_CREDIT_BY_VERSION = MappingProxyType({"v1": 1000, "v2": 1000})
SUPPORTED_RATE_CARD_VERSIONS = frozenset(_TOKENS_PER_CREDIT_BY_VERSION)


@dataclass(frozen=True, slots=True)
class ModelCreditRates:
    input: Decimal = Decimal(1)
    cached_input: Decimal = Decimal(1)
    output: Decimal = Decimal(1)

    @classmethod
    def from_config(cls, value: object) -> ModelCreditRates:
        if value is None:
            return cls()
        if not isinstance(value, dict) or set(value) != {"input", "cached_input", "output"}:
            raise ValueError("model credit rates require input, cached_input, and output")
        parsed = []
        for key in ("input", "cached_input", "output"):
            raw = value[key]
            if isinstance(raw, bool) or not isinstance(raw, (int, float, str, Decimal)):
                raise ValueError(f"model credit rate {key} must be a number")
            try:
                number = Decimal(str(raw))
            except InvalidOperation as exc:
                raise ValueError(f"model credit rate {key} must be a number") from exc
            if not number.is_finite() or number < 0 or number > 1000 or number.as_tuple().exponent < -6:
                raise ValueError(f"model credit rate {key} must be between 0 and 1000 with at most six decimals")
            parsed.append(number)
        return cls(*parsed)

    def as_json(self) -> dict[str, str]:
        return {"input": str(self.input), "cached_input": str(self.cached_input), "output": str(self.output)}


@dataclass(frozen=True, slots=True)
class ModelTokenUsage:
    input: int
    cached_input: int
    output: int
    rates: ModelCreditRates = ModelCreditRates()

    @classmethod
    def from_provider_counts(
        cls,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        *,
        cached_in_input: bool,
        rates: ModelCreditRates,
    ) -> ModelTokenUsage:
        if any(type(count) is not int or count < 0 for count in (input_tokens, cached_tokens, output_tokens)):
            raise ValueError("provider token counts must be non-negative integers")
        if cached_in_input and cached_tokens > input_tokens:
            raise ValueError("cached token count exceeds input token count")
        return cls(input_tokens - cached_tokens if cached_in_input else input_tokens, cached_tokens, output_tokens, rates)

    def weighted_tokens(self) -> Decimal:
        if any(type(count) is not int or count < 0 for count in (self.input, self.cached_input, self.output)):
            raise ValueError("model token counts must be non-negative integers")
        return self.input * self.rates.input + self.cached_input * self.rates.cached_input + self.output * self.rates.output


@dataclass(frozen=True, slots=True)
class ModelUsageLine:
    provider: str | None
    model: str | None
    usage: ModelTokenUsage

    def as_json(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input": self.usage.input,
            "cached_input": self.usage.cached_input,
            "output": self.usage.output,
            "rates": self.usage.rates.as_json(),
        }

    @classmethod
    def from_json(cls, value: object) -> ModelUsageLine:
        if not isinstance(value, dict):
            raise ValueError("model usage line must be an object")
        if "rates" not in value or value["rates"] is None:
            raise ValueError("model usage line requires frozen credit rates")
        provider, model = value.get("provider"), value.get("model")
        if provider is not None and (not isinstance(provider, str) or not provider):
            raise ValueError("model usage provider is invalid")
        if model is not None and (not isinstance(model, str) or not model):
            raise ValueError("model usage model is invalid")
        usage = ModelTokenUsage(value.get("input"), value.get("cached_input"), value.get("output"), ModelCreditRates.from_config(value["rates"]))
        usage.weighted_tokens()
        return cls(provider, model, usage)


def credits_for_model_lines(lines: list[ModelUsageLine] | tuple[ModelUsageLine, ...]) -> int:
    return credits_for_model_usage([line.usage for line in lines])


def credits_for_model_usage(usages: list[ModelTokenUsage] | tuple[ModelTokenUsage, ...]) -> int:
    """Sum all model usage before rounding once at the settlement boundary."""
    weighted = sum((usage.weighted_tokens() for usage in usages), Decimal(0))
    return int((weighted / 1000).to_integral_value(rounding=ROUND_CEILING)) if weighted > 0 else 0


def credits_for_tokens_at_version(
    version: str,
    tokens_input: int | None,
    tokens_output: int | None,
    *,
    model_usage: list[ModelTokenUsage] | tuple[ModelTokenUsage, ...] | None = None,
) -> int:
    """按持久化 rate-card 版本计价，供历史任务恢复与账本回放。"""

    try:
        tokens_per_credit = _TOKENS_PER_CREDIT_BY_VERSION[version]
    except (KeyError, TypeError) as exc:
        raise ValueError("unknown rate card version") from exc
    if version == "v2":
        if model_usage is None:
            raise ValueError("v2 rate card requires classified model usage")
        return credits_for_model_usage(model_usage)
    total = int(tokens_input or 0) + int(tokens_output or 0)
    if total <= 0:
        return 0
    return math.ceil(total / tokens_per_credit)


def credits_for_tokens(tokens_input: int | None, tokens_output: int | None) -> int:
    """Preserve the v1 two-counter calculation for historical and unclassified usage."""
    return credits_for_tokens_at_version("v1", tokens_input, tokens_output)


def explain_cost(tokens_input: int | None, tokens_output: int | None) -> str:
    """一句人类可读的计价说明，给 billing admin 回答「为什么这次花了这么多」。"""
    tin, tout = int(tokens_input or 0), int(tokens_output or 0)
    cost = credits_for_tokens(tin, tout)
    tokens_per_credit = _TOKENS_PER_CREDIT_BY_VERSION["v1"]
    return f"{tin} + {tout} = {tin + tout} tokens / {tokens_per_credit} → {cost} credits (rate card v1)"
