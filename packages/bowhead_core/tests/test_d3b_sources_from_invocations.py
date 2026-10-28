"""D3b-sources — chat lookup short-path populates lead.final.payload.sources from the turn's
tool invocations (web_search {title,url} + bi/kb artifacts) instead of hardcoded [].
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from bowhead_core.agents.lead_orchestrator import _sources_from_invocations


def _inv(tool: str, result: Any) -> SimpleNamespace:
    return SimpleNamespace(tool_name=tool, arguments={}, result=result)


def test_web_search_results_become_sources() -> None:
    inv = _inv(
        "web_search",
        {
            "results": [
                {"title": "统计局", "url": "https://stats.gov.cn/x"},
                {"title": "WB", "url": "https://data.worldbank.org/cn"},
            ]
        },
    )
    srcs = _sources_from_invocations([inv])
    urls = [s["artifact_id"] for s in srcs]  # web source content-id = the URL
    assert "https://stats.gov.cn/x" in urls
    assert "https://data.worldbank.org/cn" in urls
    assert any(s["label"] == "统计局" for s in srcs)


def test_empty_invocations_yield_empty_sources() -> None:
    assert _sources_from_invocations([]) == []
    assert _sources_from_invocations(None) == []


def test_bi_artifact_becomes_source() -> None:
    inv = _inv("bi_query", {"results": [{"artifact_id": "art_abc", "query_text": "营业额"}]})
    srcs = _sources_from_invocations([inv])
    assert any(s["artifact_id"] == "art_abc" for s in srcs)
