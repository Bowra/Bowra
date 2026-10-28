"""Sprint α-3 — BM25-lite tokenizer + boost unit tests."""

from __future__ import annotations

from bowhead_core.kb.retriever import (
    _bm25_lite_boost,
    _tokenize_cjk_bigram_en,
)

# -- Tokenizer --


def test_tokenize_english_words() -> None:
    out = _tokenize_cjk_bigram_en("ROE growth rate")
    assert "roe" in out
    assert "growth" in out
    assert "rate" in out


def test_tokenize_lowercase_folding() -> None:
    """ROE / RoE / roe collapse to same token."""
    a = _tokenize_cjk_bigram_en("ROE")
    b = _tokenize_cjk_bigram_en("roe")
    c = _tokenize_cjk_bigram_en("RoE")
    assert a == b == c == {"roe"}


def test_tokenize_cjk_bigram() -> None:
    out = _tokenize_cjk_bigram_en("毛利率")
    # 3 chars → 2 bigrams: "毛利", "利率"
    assert "毛利" in out
    assert "利率" in out


def test_tokenize_mixed_cjk_en() -> None:
    out = _tokenize_cjk_bigram_en("毛利率 ROE 走势")
    assert "毛利" in out
    assert "利率" in out
    assert "roe" in out
    assert "走势" in out


def test_tokenize_skips_short_english() -> None:
    """Single-char English (a, b, c) skipped to prevent stopword noise."""
    out = _tokenize_cjk_bigram_en("a b c is good")
    assert "a" not in out
    assert "is" in out  # 2 chars OK
    assert "good" in out


def test_tokenize_id_pattern_digit_led() -> None:
    """0.40.2 #2: structured ids like 付-0004, GR202211000250 produce tokens.
    Phase 4 dogfood Q5 ("2022年付-0004业务招待费") missed pre-0.40.2 because
    the EN-only regex required `[A-Za-z]` start. With ID regex added, the
    literal dash-bearing id appears in the lexical token set.
    """
    out = _tokenize_cjk_bigram_en("付-0004 业务招待费")
    assert "付-0004" in out or "0004" in out  # at least one form captured
    # Pure digits "2022" alone NOT included (would over-boost common years)
    out2 = _tokenize_cjk_bigram_en("2022")
    assert "2022" not in out2


def test_tokenize_id_pattern_alnum() -> None:
    """Alphanumeric ids GR202211000250 / kbf_abcd_1234 captured."""
    out = _tokenize_cjk_bigram_en("证书编号 GR202211000250")
    assert "gr202211000250" in out
    out2 = _tokenize_cjk_bigram_en("kbf_d0d08f279bbc")
    assert "kbf_d0d08f279bbc" in out2


def test_tokenize_skips_isolated_cjk() -> None:
    """Single CJK char (e.g. trailing 个) doesn't become a bigram."""
    out = _tokenize_cjk_bigram_en("营 收")
    # 营 and 收 are separated by space — no bigram emitted
    assert "营收" not in out


def test_tokenize_handles_punctuation() -> None:
    """Punctuation breaks runs, doesn't appear in tokens."""
    out = _tokenize_cjk_bigram_en("营收, 毛利率.")
    assert "营收" in out
    assert "," not in out
    assert "." not in out


# -- BM25-lite boost --


def test_no_query_tokens_no_boost() -> None:
    assert _bm25_lite_boost(set(), "any content") == 0.0


def test_no_overlap_no_boost() -> None:
    q_tokens = _tokenize_cjk_bigram_en("毛利率")
    boost = _bm25_lite_boost(q_tokens, "完全不相关内容 content here")
    assert boost == 0.0


def test_partial_overlap_proportional() -> None:
    """1 overlap = 0.005, 2 overlaps = 0.010, capped at 0.05."""
    q_tokens = _tokenize_cjk_bigram_en("ROE 毛利率")
    # ROE bigram missing in chunk; only "ROE" overlap
    boost_1 = _bm25_lite_boost(q_tokens, "ROE 数据")
    assert 0.004 < boost_1 < 0.011  # at least one English token match


def test_full_overlap_caps_at_005() -> None:
    """Many tokens in common — capped at 0.05."""
    # 12 tokens → 12 × 0.005 = 0.06 > 0.05 cap
    q_tokens = _tokenize_cjk_bigram_en("毛利率 ROE NRR LTV CAC 走势 营收 净利润 应收账款 存货 周转 现金流")
    big_chunk = "毛利率 ROE NRR LTV CAC 走势 营收 净利润 应收账款 存货 周转 现金流"
    boost = _bm25_lite_boost(q_tokens, big_chunk)
    assert boost == 0.05  # capped


def test_empty_content_no_boost() -> None:
    q_tokens = _tokenize_cjk_bigram_en("anything")
    assert _bm25_lite_boost(q_tokens, "") == 0.0
