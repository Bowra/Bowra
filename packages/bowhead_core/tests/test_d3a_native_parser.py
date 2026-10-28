"""D3a-native — parse the native DashScope generation response.

The native endpoint (flag BOWHEAD_WEB_SEARCH_NATIVE, default OFF) returns structured
output.search_info.search_results[] with real URLs, unlike the OpenAI-compat path. This tests
the parser against the documented shape; the live request switch is dogfood-gated.
"""

from __future__ import annotations

from bowhead_core.web_search import _parse_native_search_response


def test_parses_search_info_results() -> None:
    data = {
        "output": {
            "text": "中国GDP约18万亿美元 [ref_1]。",
            "search_info": {
                "search_results": [
                    {"index": 1, "title": "统计局", "url": "https://www.stats.gov.cn/x", "site_name": "s"},
                    {"index": 2, "title": "World Bank", "url": "https://data.worldbank.org/cn"},
                ]
            },
        }
    }
    summary, results = _parse_native_search_response(data, 8)
    assert "18万亿" in summary
    assert [r["url"] for r in results] == ["https://www.stats.gov.cn/x", "https://data.worldbank.org/cn"]
    assert results[0]["title"] == "统计局"


def test_summary_falls_back_to_choices_content() -> None:
    data = {"output": {"choices": [{"message": {"content": "fallback prose"}}], "search_info": {"search_results": []}}}
    summary, results = _parse_native_search_response(data, 8)
    assert summary == "fallback prose"
    assert results == []


def test_title_falls_back_to_site_name() -> None:
    data = {"output": {"text": "x", "search_info": {"search_results": [{"url": "https://e.com/p", "site_name": "Example"}]}}}
    _, results = _parse_native_search_response(data, 8)
    assert results[0]["title"] == "Example"


def test_drops_non_http_url() -> None:
    data = {"output": {"text": "x", "search_info": {"search_results": [{"title": "bad", "url": "javascript:alert(1)"}]}}}
    _, results = _parse_native_search_response(data, 8)
    assert results[0]["url"] == ""


def test_respects_limit() -> None:
    data = {"output": {"text": "x", "search_info": {"search_results": [{"title": str(i), "url": f"https://e{i}.example.com"} for i in range(10)]}}}
    _, results = _parse_native_search_response(data, 3)
    assert len(results) == 3


def test_malformed_response_empty() -> None:
    assert _parse_native_search_response({}, 8) == ("", [])
    assert _parse_native_search_response({"output": "notdict"}, 8) == ("", [])
    assert _parse_native_search_response({"output": {"search_info": "x"}}, 8) == ("", [])
