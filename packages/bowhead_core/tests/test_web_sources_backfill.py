"""S6 假阴修复(F-D2 根因 a): web invocation → sources[] 确定性回填."""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.agents.agent_runner_adapter import _sources_from_web_invocations


def _inv(tool: str, results: list | None, query: str = "零售行业新闻") -> SimpleNamespace:
    return SimpleNamespace(tool_name=tool, arguments={"query": query}, result={"query": query, "summary": "s", "results": results or []})


def test_backfill_from_web_search_results() -> None:
    invs = [
        _inv(
            "web_search",
            [
                {"title": "国家统计局:2025社零数据", "url": "https://stats.gov.cn/a", "snippet": "…"},
                {"title": "商务部306号文", "url": "https://mofcom.gov.cn/b", "snippet": "…"},
            ],
        )
    ]
    out = _sources_from_web_invocations(invs)
    assert [s["label"] for s in out] == ["国家统计局:2025社零数据", "商务部306号文"]
    assert all(s["tool"] == "web_search" and s["query"] for s in out)
    assert out[0]["url"] == "https://stats.gov.cn/a"


def test_backfill_dedupes_and_caps() -> None:
    results = [{"title": f"t{i}", "url": f"https://x/{i % 3}"} for i in range(20)]  # 3 unique URLs.
    out = _sources_from_web_invocations([_inv("web_search", results)])
    assert len(out) == 3
    many = [{"title": f"t{i}", "url": f"https://x/{i}"} for i in range(20)]
    assert len(_sources_from_web_invocations([_inv("web_search", many)])) == 10  # cap


def test_backfill_ignores_non_web_and_malformed() -> None:
    invs = [
        _inv("bi_query", [{"title": "x", "url": "https://x"}]),
        SimpleNamespace(tool_name="web_search", arguments={}, result="not a dict"),
        SimpleNamespace(tool_name="web_search", arguments=None, result={"results": [{"title": "ok", "url": "https://ok"}]}),
        _inv("web_search", ["not-a-dict", {"title": "", "url": ""}]),
    ]
    out = _sources_from_web_invocations(invs)
    assert [s["label"] for s in out] == ["ok"]
    assert "query" not in out[0]  # arguments=None → no query key.


def test_backfill_empty_when_no_web() -> None:
    assert _sources_from_web_invocations([]) == []
