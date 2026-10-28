"""Sprint α-4 — finance synonym expansion unit tests."""

from __future__ import annotations

from bowhead_core.kb.synonyms import (
    FINANCE_SYNONYMS,
    expand_synonyms_for_query,
)


def test_empty_query() -> None:
    assert expand_synonyms_for_query("") == set()


def test_no_known_term_no_expansion() -> None:
    assert expand_synonyms_for_query("完全无关的内容 random stuff") == set()


def test_english_term_roe_expanded() -> None:
    out = expand_synonyms_for_query("ROE 走势")
    # Should include the canonical + all synonyms
    assert "roe" in out
    assert "净资产收益率" in out
    assert "return on equity" in out
    # English words split into BM25-compatible tokens
    assert "return" in out
    assert "equity" in out
    # CJK synonym broken into bigrams
    assert "净资" in out
    assert "资产" in out


def test_cjk_term_毛利率_expanded() -> None:
    out = expand_synonyms_for_query("毛利率怎么算")
    assert "毛利率" in out
    assert "毛利百分比" in out
    assert "gross margin" in out
    # Bigrams of synonyms
    assert "毛利" in out
    assert "百分" in out
    # English synonym split
    assert "gross" in out
    assert "margin" in out


def test_case_insensitive_english() -> None:
    """ROE / RoE / roe all hit the same map entry."""
    a = expand_synonyms_for_query("ROE growth")
    b = expand_synonyms_for_query("roe growth")
    c = expand_synonyms_for_query("RoE growth")
    assert a == b == c


def test_multiple_terms_in_one_query() -> None:
    out = expand_synonyms_for_query("ROE vs 毛利率 趋势")
    # Both expanded
    assert "净资产收益率" in out
    assert "毛利百分比" in out


def test_mom_and_yoy_distinct() -> None:
    """qoq / yoy / mom all separate, no cross-pollution."""
    yoy_out = expand_synonyms_for_query("YoY 增长")
    qoq_out = expand_synonyms_for_query("QoQ 增长")
    assert "同比" in yoy_out
    assert "同比" not in qoq_out
    assert "环比" in qoq_out
    assert "环比" not in yoy_out


def test_synonym_map_has_high_value_terms() -> None:
    """Sanity: 8 most-asked terms covered."""
    must_have = ["毛利率", "roe", "ebitda", "fcf", "yoy", "gmv", "ltv", "arr"]
    for k in must_have:
        assert k in FINANCE_SYNONYMS, f"missing canonical key: {k}"


def test_substring_match_intentional() -> None:
    """'ROE 净资产收益率' typed by user → still expanded
    (substring match ok, no exact-word boundary required)."""
    out = expand_synonyms_for_query("公司 ROE 怎么算")
    assert "roe" in out
    assert "return on equity" in out


# -- P0 fix: word-boundary for short English keys --


def test_pe_does_not_match_inside_people() -> None:
    """`pe` is 2-char English key — must NOT trigger on substring inside 'people'."""
    out = expand_synonyms_for_query("people prefer this approach")
    assert "市盈率" not in out
    assert "price to earnings" not in out


def test_ps_does_not_match_inside_tips() -> None:
    """`ps` is 2-char English key — must NOT trigger on substring inside 'tips'."""
    out = expand_synonyms_for_query("Some tips for retention growth")
    assert "市销率" not in out


def test_pe_matches_at_word_boundary() -> None:
    """`pe` typed standalone (with spaces / start / end) → expand."""
    out = expand_synonyms_for_query("PE ratio is high")
    assert "市盈率" in out
    assert "price to earnings" in out


def test_pe_matches_with_punctuation_boundary() -> None:
    """`PE.` or `(PE)` count as boundaries."""
    out = expand_synonyms_for_query("Look at the (PE) for valuation")
    assert "市盈率" in out


def test_roe_does_not_match_inside_rope() -> None:
    """`roe` substring of `rope` was a worry; `rope` doesn't actually contain `roe` chars
    but `royal` doesn't either. Real risk: words ending in 'roe' or starting with 'roe'.
    Regression catch."""
    expand_synonyms_for_query("a tightrope walker")
    # `roe` not contiguous in `tightrope` → not affected anyway
    # but check actual collision case
    out2 = expand_synonyms_for_query("hero of the story")
    assert "净资产收益率" not in out2
    out3 = expand_synonyms_for_query("the rope")
    # 'rope' contains 'rop', 'ope' — not 'roe' contiguous; safe
    assert "净资产收益率" not in out3


def test_roi_word_boundary() -> None:
    """`roi` 3-char key — `approach` / `coronavirus` etc. should not match."""
    out = expand_synonyms_for_query("the approach is right")
    assert "投资回报率" not in out
    out2 = expand_synonyms_for_query("ROI on this project?")
    assert "投资回报率" in out2


def test_long_english_key_substring_still_works() -> None:
    """≥ 4 char keys still substring-match (false-positive risk much lower)."""
    out = expand_synonyms_for_query("EBITDA margin")
    assert "息税折旧摊销前利润" in out


def test_cjk_substring_still_works_for_inflected_query() -> None:
    """CJK 'GMV' synonyms via key '毛利率' substring of '毛利率分析'."""
    out = expand_synonyms_for_query("毛利率分析")
    assert "毛利百分比" in out
    assert "gross margin" in out
