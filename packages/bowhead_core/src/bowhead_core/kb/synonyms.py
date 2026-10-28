"""Finance/DD synonym map for KB retrieval (sprint α PR-#4).

Used by retriever's BM25-lite token expansion: when the user types a common
finance term, expand the query token set to include known synonyms so docs
written with the alternate phrasing still hit.

Scope: ONLY high-confidence 1:N maps where alternate phrasings refer to the
same concept (no semantic drift). Don't add fuzzy / generic synonyms — they
hurt precision more than help recall.

Source: prod incident reports + IntelliVault DD scenario library
(intellivault_extended.py 50 queries) + 客户访谈.

CTO-review fix (P0): English keys ≤ 3 chars use word-boundary regex match
to avoid substring false-positives ("pe" in "people" / "ps" in "tips"). CJK
keys keep substring match (Chinese has natural word segmentation).
"""

from __future__ import annotations

import re

# Canonical term (lowercased / CJK-clean) → synonyms list.
# All entries lowercased so they collide with the BM25-lite tokenizer output
# (which folds case + emits CJK bigrams).
#
# CJK terms are stored as their original form; the consumer should ALSO emit
# bigrams from synonyms so the lookup matches token patterns.
FINANCE_SYNONYMS: dict[str, list[str]] = {
    # Profit margins / returns.
    "毛利率": ["毛利百分比", "gross margin", "gpm"],
    "净利率": ["净利润率", "净利百分比", "net margin", "npm"],
    "营业利润率": ["operating margin", "opm"],
    # ROx family.
    "roe": ["净资产收益率", "股东权益回报率", "return on equity"],
    "roa": ["总资产收益率", "return on assets"],
    "roic": ["投资资本回报率", "return on invested capital"],
    "roi": ["投资回报率", "return on investment"],
    # Operations / cash flow.
    "ebitda": ["息税折旧摊销前利润"],
    "ebit": ["息税前利润", "operating income"],
    "fcf": ["自由现金流", "free cash flow"],
    "ocf": ["经营现金流", "operating cash flow"],
    # SaaS metrics.
    "arr": ["年度经常性收入", "annualized recurring revenue"],
    "mrr": ["月度经常性收入", "monthly recurring revenue"],
    "nrr": ["净留存率", "net dollar retention", "ndr"],
    "ltv": ["客户生命周期价值", "lifetime value"],
    "cac": ["客户获取成本", "customer acquisition cost"],
    # Growth / periods.
    "yoy": ["同比", "year over year", "year-on-year"],
    "qoq": ["环比", "quarter over quarter"],
    "mom": ["月环比", "month over month"],
    # Finance.
    "gmv": ["商品交易总额", "gross merchandise value"],
    "应收账款": ["应收", "receivables", "ar"],
    "存货": ["库存", "inventory"],
    "周转": ["周转率", "turnover"],
    # Valuation.
    "pe": ["市盈率", "price to earnings"],
    "pb": ["市净率", "price to book"],
    "ps": ["市销率", "price to sales"],
}


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _is_cjk_key(s: str) -> bool:
    """Canonical key is CJK if it contains any CJK character."""
    return bool(_CJK_RE.search(s))


def _key_matches_query(canonical: str, q_lower: str) -> bool:
    """CTO P0 fix: word-boundary match for short English keys, substring for CJK.

    - CJK keys: substring (natural Chinese segmentation makes substr safe)
    - English keys ≥ 4 chars: substring (rare false positive, "ebitda" / "fcf"
      / "growth" don't collide with normal text)
    - English keys ≤ 3 chars (pe/pb/ps/roe/roa/roi/yoy/qoq/mom/arr/mrr/nrr/
      ltv/cac/gmv): word-boundary regex required
    """
    if _is_cjk_key(canonical):
        return canonical in q_lower
    if len(canonical) <= 3:
        # Word-boundary: must be surrounded by non-alphanumeric (or string boundary)
        pattern = r"(?<![a-z0-9])" + re.escape(canonical) + r"(?![a-z0-9])"
        return bool(re.search(pattern, q_lower))
    return canonical in q_lower


def expand_synonyms_for_query(query: str) -> set[str]:
    """Detect known finance terms in raw query, return their synonym tokens.

    Operates on the raw query string (CJK-aware) before bigram tokenization.
    Match policy (P0 fix):
    - CJK canonical keys: substring match
    - English canonical keys: word-boundary if ≤ 3 chars, substring if ≥ 4

    Returns: set of synonym tokens to UNION with the BM25-lite token set.
    Each synonym is also bigram-expanded for CJK so it overlaps with doc tokens.

    Examples:
    - query = "ROE 趋势"        → {"roe", "净资产收益率", ...}
    - query = "毛利率怎么算"     → {"毛利率", "毛利百分比", ...}
    - query = "people prefer"   → {} (no false-positive "pe" hit)
    - query = "completely unrelated"  → {}
    """
    if not query:
        return set()
    q_lower = query.lower()
    out: set[str] = set()
    for canonical, synonyms in FINANCE_SYNONYMS.items():
        if not _key_matches_query(canonical, q_lower):
            continue
        # Add all synonyms + the canonical itself; bigram expand CJK ones
        for syn in (canonical, *synonyms):
            syn_lower = syn.lower()
            out.add(syn_lower)
            out.update(_cjk_bigrams(syn_lower))
            # Also add English token splits (so "gross margin" → "gross", "margin")
            out.update(_en_words(syn_lower))
    return out


# -- Helpers (deliberate small dupes to avoid retriever import cycle) --


def _cjk_bigrams(text: str) -> set[str]:
    """Bigrams over CJK runs; matches retriever tokenizer behavior."""
    out: set[str] = set()
    cjk_run = ""
    for ch in text + " ":
        if "\u4e00" <= ch <= "\u9fff":
            cjk_run += ch
        else:
            if len(cjk_run) >= 2:
                for i in range(len(cjk_run) - 1):
                    out.add(cjk_run[i : i + 2])
            cjk_run = ""
    return out


def _en_words(text: str) -> set[str]:
    """English word split; matches retriever tokenizer behavior."""
    out: set[str] = set()
    word = ""
    for ch in text + " ":
        if ch.isalnum() and ord(ch) < 128:
            word += ch
        else:
            if len(word) >= 2:
                out.add(word)
            word = ""
    return out
