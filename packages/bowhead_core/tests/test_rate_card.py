"""credit-billing gap 8 —— 计价 rate card 的数值等价与调用点收敛。

`ceil((tokens_in + tokens_out) / 1000)` 原本在四处各写一遍（chat 扣费 / 定时任务扣费 /
KB 矩阵扣费 / KB 矩阵跑前预估）。model §5 早写明「不要把定价逻辑散落在各处」，Slice D 是
该设计里唯一零实现的切片。

本文件钉两件事:
1. **v1 与改前公式逐字节等价** —— 引入中心 helper 不能改变任何扣费金额
2. **调用点真的收敛了** —— 扫源码确认那四处不再各自算（否则「收敛」只是说法）
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from pathlib import Path

import pytest
from bowhead_core.rate_card import (
    RATE_CARD_VERSION,
    SUPPORTED_RATE_CARD_VERSIONS,
    ModelCreditRates,
    ModelTokenUsage,
    ModelUsageLine,
    credits_for_model_lines,
    credits_for_tokens,
    credits_for_tokens_at_version,
    explain_cost,
)
from bowhead_core.terminal_effect_contract import (
    SUPPORTED_BILLING_RATE_CARD_VERSIONS,
    effect_provider_contract_keys,
)

_ROOT = Path(__file__).resolve().parents[3]


def _legacy(tokens_input: int, tokens_output: int) -> int:
    """改前的原式，逐字节照抄用于 A/B。"""
    return math.ceil((tokens_input + tokens_output) / 1000)


def test_v1_is_numerically_identical_to_the_legacy_formula() -> None:
    """★ 引入 helper 不改任何金额 —— 边界 + 随机大范围双向比对。"""
    cases = [
        (0, 1),
        (1, 0),
        (1, 1),
        (999, 0),
        (1000, 0),
        (1001, 0),
        (500, 500),
        (500, 501),
        (1_000_000, 234_567),
    ]
    for tin, tout in cases:
        assert credits_for_tokens(tin, tout) == _legacy(tin, tout), f"{tin}+{tout}"
    for tin in range(0, 5000, 137):
        for tout in range(0, 3000, 91):
            if tin + tout == 0:
                continue  # 0 tokens are a special case; see next test.
            assert credits_for_tokens(tin, tout) == _legacy(tin, tout), f"{tin}+{tout}"


def test_zero_and_none_are_zero_cost() -> None:
    """0 token 不该被 ceil 成 1 credit；None 视作 0（调用点常传可空的 result.tokens_*）。"""
    assert credits_for_tokens(0, 0) == 0
    assert credits_for_tokens(None, None) == 0
    assert credits_for_tokens(None, 0) == 0


def test_negative_totals_do_not_produce_negative_credits() -> None:
    """脏数据不该变成「反向充值」。"""
    assert credits_for_tokens(-5, 0) == 0
    assert credits_for_tokens(-5000, 100) == 0


def test_rounding_is_up_not_nearest() -> None:
    assert credits_for_tokens(1, 0) == 1
    assert credits_for_tokens(1000, 0) == 1
    assert credits_for_tokens(1001, 0) == 2


def test_historical_rate_card_registry_is_the_terminal_reader_registry() -> None:
    assert SUPPORTED_BILLING_RATE_CARD_VERSIONS == SUPPORTED_RATE_CARD_VERSIONS
    assert {
        key.removeprefix("credit_ledger/") for key in effect_provider_contract_keys() if key.startswith("credit_ledger/")
    } == SUPPORTED_RATE_CARD_VERSIONS
    assert credits_for_tokens_at_version("v1", 1200, 300) == 2
    assert RATE_CARD_VERSION == "v2"
    assert credits_for_tokens(1200, 300) == credits_for_tokens_at_version("v1", 1200, 300)
    with pytest.raises(ValueError, match="unknown rate card version"):
        credits_for_tokens_at_version("future", 1200, 300)


def test_explain_cost_states_inputs_result_and_version() -> None:
    """给 billing admin 的解释必须自足: 两个 token 数、结果、以及按哪版规则算的。"""
    text = explain_cost(1200, 300)
    assert "1200" in text
    assert "300" in text
    assert "1500" in text
    assert "2 credits" in text
    assert "v1" in text


def test_v2_classifies_cache_per_provider_and_rounds_after_all_models() -> None:
    rates = ModelCreditRates(Decimal("2"), Decimal("0.25"), Decimal("4"))
    openai = ModelTokenUsage.from_provider_counts(800, 600, 100, cached_in_input=True, rates=rates)
    anthropic = ModelTokenUsage.from_provider_counts(200, 600, 100, cached_in_input=False, rates=rates)
    assert (openai.input, openai.cached_input, openai.output) == (200, 600, 100)
    assert (anthropic.input, anthropic.cached_input, anthropic.output) == (200, 600, 100)
    lines = [ModelUsageLine("openai", "a", openai), ModelUsageLine("anthropic", "b", anthropic)]
    assert credits_for_model_lines(lines) == 2
    assert credits_for_tokens_at_version("v2", 1000, 200, model_usage=[openai, anthropic]) == 2


def test_v2_default_rates_match_disjoint_token_total() -> None:
    rates = ModelCreditRates.from_config(None)
    usage = ModelTokenUsage.from_provider_counts(1000, 300, 500, cached_in_input=True, rates=rates)
    assert credits_for_model_lines([ModelUsageLine(None, "model", usage)]) == 2
    with pytest.raises(ValueError, match="classified model usage"):
        credits_for_tokens_at_version("v2", 1000, 500)
    with pytest.raises(ValueError, match="cached token count exceeds"):
        ModelTokenUsage.from_provider_counts(1, 2, 0, cached_in_input=True, rates=rates)


def test_v2_replay_requires_the_frozen_rates_even_when_the_defaults_are_one() -> None:
    line = ModelUsageLine("provider", "model", ModelTokenUsage(100, 50, 20))
    assert ModelUsageLine.from_json(line.as_json()) == line
    without_rates = line.as_json()
    del without_rates["rates"]
    with pytest.raises(ValueError, match="frozen credit rates"):
        ModelUsageLine.from_json(without_rates)


def test_no_call_site_computes_the_formula_itself() -> None:
    """★ 守卫: 扣费/预估的四处不得再各自写 ceil(.../1000)。

    扫真实源码而非信任本文件的说法 —— 否则「已收敛」只是一句注释。
    """
    watched = [
        "packages/bowhead_gateway/src/bowhead_gateway/routes/chat.py",
        "packages/bowhead_gateway/src/bowhead_gateway/_scheduled_runner.py",
        "packages/bowhead_core/src/bowhead_core/kb/matrix_engine.py",
    ]
    # Match `ceil(<anything>/ 1000)`: detect only the credit conversion denominator 1000, not other ceil uses like CHUNK_CHARS.
    pattern = re.compile(r"ceil\([^)]*/\s*1000\s*\)")
    offenders: list[str] = []
    for rel in watched:
        text = (_ROOT / rel).read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "这些行应改用 rate_card.credits_for_tokens:\n" + "\n".join(offenders)


def test_watched_call_sites_import_the_helper() -> None:
    """反向守卫: 真正扣费的 owner 必须使用中心 helper。

    scheduled runner 已复用 chat worker cleanup，不再单独扣费或 import helper。
    """
    for rel in (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/chat.py",
        "packages/bowhead_core/src/bowhead_core/kb/matrix_engine.py",
    ):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        assert "credits_for_tokens" in text, rel
