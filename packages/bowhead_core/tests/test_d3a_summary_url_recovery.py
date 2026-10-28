"""D3a — recover inlined source URLs from the web_search summary (compat-path stopgap).

DashScope's OpenAI-compat endpoint returns a summary with inline [N] markers but no
structured citation list, so results was empty. _results_from_summary_urls recovers any
FULL URLs literally present in the summary prose — never fabricating links from bare [N].
"""

from __future__ import annotations

from bowhead_core.web_search import _results_from_summary_urls


def test_extracts_full_urls_with_host_titles() -> None:
    summary = "数据见 https://www.stats.gov.cn/sj 和 https://data.worldbank.org/country/cn 。"
    out = _results_from_summary_urls(summary, limit=8)
    urls = [r["url"] for r in out]
    assert "https://www.stats.gov.cn/sj" in urls
    assert "https://data.worldbank.org/country/cn" in urls
    assert all(r["title"] for r in out)  # host used as title


def test_dedups_same_url() -> None:
    summary = "见 (https://example.com/a) 又见 https://example.com/a 。"
    out = _results_from_summary_urls(summary, 8)
    assert [r["url"] for r in out] == ["https://example.com/a"]


def test_trims_trailing_punctuation() -> None:
    out = _results_from_summary_urls("来源 https://example.com/p.", 8)
    assert out[0]["url"] == "https://example.com/p"


def test_no_urls_returns_empty() -> None:
    assert _results_from_summary_urls("纯文本，只有 [1][2] 角标，没有链接。", 8) == []


def test_drops_non_http_scheme() -> None:
    assert _results_from_summary_urls("javascript:alert(1) ftp://x.com/y", 8) == []


def test_respects_limit() -> None:
    s = " ".join(f"https://e{i}.example.com/p" for i in range(10))
    assert len(_results_from_summary_urls(s, limit=3)) == 3
