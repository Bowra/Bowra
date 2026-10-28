"""Try downstream binding when the local concept catalog lacks a literal value.

Synthetic products and organization names model substring-only candidates,
missing candidates, and exact matches without embedding customer dictionaries.
"""

from __future__ import annotations

import pytest
from bowhead_core.agents.bi_concept_resolve import _no_literal_candidate
from bowhead_core.bi_preflight import strip_product_vocabulary
from bowhead_core.concept_recall import Match

pytestmark = pytest.mark.unit


def _m(dim: str, dim_name: str, value: str, evidence: str, dist: float = 0.3) -> Match:
    return Match(type="dim_value", dim=dim, dim_name=dim_name, value=value, evidence=evidence, dist=dist)


# ── ① The term itself is absent from candidates: a missing dictionary term, not semantic ambiguity ──


def test_substring_candidates_have_no_literal_match() -> None:
    """Five synthetic candidates contain the token without matching it literally."""
    candidates = [
        _m("d15", "示例产品甲潜力", "示例产品甲潜力", "token_in_doc", 0.16),
        _m("d12", "定义市场", "示例产品甲零售市场", "token_in_doc", 0.31),
        _m("d12", "定义市场", "示例产品甲批发市场", "token_in_doc", 0.34),
        _m("d12", "定义市场", "示例产品甲零售定义市场", "token_in_doc", 0.36),
        _m("d12", "定义市场", "示例产品甲批发定义市场", "token_in_doc", 0.39),
    ]
    assert _no_literal_candidate(candidates) is True


def test_real_fork_still_asks_the_user() -> None:
    """A literal match preserves genuine ambiguity between two concepts."""
    fork = [
        _m("d11", "中文品牌", "示例产品甲", "literal", 0.0),
        _m("d13", "通用名", "示例通用产品", "near", 0.28),
    ]
    assert _no_literal_candidate(fork) is False


def test_empty_matches_counts_as_no_literal() -> None:
    assert _no_literal_candidate([]) is True


# ── ② Product-term cleanup ───────────────────────────────────────────────────────


def test_strips_our_ui_word_that_became_a_bi_filter() -> None:
    """Strip UI vocabulary while preserving the synthetic query's business terms."""
    src = "按中文品牌筛选示例产品甲，按月汇总销售数量，覆盖工作区全部可用月份，并保留中文品牌原文"
    out, hit = strip_product_vocabulary(src)
    assert "工作区" not in out, out
    assert hit == ["工作区"], hit
    # Preserve every query element without false matches.
    for keep in ("中文品牌", "示例产品甲", "销售数量", "按月"):
        assert keep in out, (keep, out)


def test_longest_variant_wins_so_no_dangling_prefix() -> None:
    out, hit = strip_product_vocabulary("覆盖当前工作区的全部月份")
    assert hit == ["当前工作区"], hit
    assert "当前" not in out and "工作区" not in out, out


def test_untouched_query_is_returned_byte_identical() -> None:
    """A query without UI vocabulary remains byte-identical."""
    src = "2026年 各品牌 营业额 每月 同比"
    out, hit = strip_product_vocabulary(src)
    assert out == src and hit == []


def test_customer_words_that_merely_look_like_ours_are_kept() -> None:
    """Do not strip general terms that may be actual customer field names."""
    src = "按口径维度看原文数量"
    out, hit = strip_product_vocabulary(src)
    assert out == src and hit == []


# ── Wiring: `_compose_result` actually determines the hint; pure-function tests cannot cover it ──
# (Tests covering only helper functions can all pass while the entire path does nothing; this repository has seen it.)


def _recall(token: str, matches: list[Match], verdict: str = "ambiguous"):
    from bowhead_core.concept_recall import TokenRecall

    return TokenRecall(token=token, verdict=verdict, matches=matches)  # type: ignore[arg-type]


def _hint(rec) -> tuple[str, object]:
    from bowhead_core.agents.bi_concept_resolve import _compose_result

    out = _compose_result(rec.token, rec, None, 30.0)
    return out["next_action_hint"], out.get("clarification_payload")


def test_wiring_missing_literal_defers_to_parser() -> None:
    hint, payload = _hint(
        _recall(
            "示例产品甲",
            [
                _m("d15", "示例产品甲潜力", "示例产品甲潜力", "token_in_doc", 0.16),
                _m("d12", "定义市场", "示例产品甲零售市场", "token_in_doc", 0.31),
            ],
        )
    )
    assert hint == "skip_token", hint
    assert payload is None, payload


def test_wiring_real_fork_still_pops_the_card() -> None:
    """A genuine fork still produces a clarification card."""
    hint, payload = _hint(
        _recall(
            "示例产品甲",
            [
                _m("d11", "中文品牌", "示例产品甲", "literal", 0.0),
                _m("d13", "通用名", "示例通用产品", "literal", 0.0),
            ],
        )
    )
    assert hint == "call_clarification_ask", hint
    assert payload is not None


def test_wiring_same_value_across_dims_unchanged() -> None:
    """The same literal value in two dimensions retains the existing exemption."""
    hint, _ = _hint(
        _recall(
            "示例供应商甲",
            [
                _m("d21", "上级机构名称", "示例供应商甲", "literal", 0.0),
                _m("d22", "客户名称", "示例供应商甲", "literal", 0.0),
            ],
        )
    )
    assert hint == "use_top_candidate", hint
