"""Tavily 网页工具与既有链路的接缝：来源核实、报告「数据来源」、计划执行的来源闸种子、步骤行摘要。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.types import ToolInvocation

_EXTRACT_RESULT = {
    "pages": [
        {"url": "https://a.com/x", "content": "正文", "truncated": False, "artifact_id": "art_1"},
        {"url": "https://a.com/empty", "content": "", "truncated": False, "artifact_id": "art_2"},
    ],
    "failed": [],
    "rejected_urls": [],
    "credits": 1,
}


def test_pages_read_by_web_extract_count_as_inspected_sources() -> None:
    """任务点名的网页用 web_extract 读到了正文，就不该被判「来源未核实」。"""
    from bowhead_core.subagent import _successful_web_fetch_urls

    invocations = [ToolInvocation(tool_name="web_extract", arguments={"urls": ["https://a.com/x"]}, result=_EXTRACT_RESULT)]
    assert _successful_web_fetch_urls(invocations) == frozenset({"https://a.com/x"})  # Pages whose body was not read do not count.


def test_failed_web_extract_does_not_count() -> None:
    from bowhead_core.subagent import _successful_web_fetch_urls

    invocations = [ToolInvocation(tool_name="web_extract", arguments={"urls": ["https://a.com/x"]}, result=_EXTRACT_RESULT, error="boom")]
    assert _successful_web_fetch_urls(invocations) == frozenset()


@pytest.mark.parametrize(
    ("tool", "result", "urls"),
    [
        ("web_extract", _EXTRACT_RESULT, {"https://a.com/x", "https://a.com/empty"}),
        ("web_crawl", {"pages": [{"url": "https://a.com/1", "excerpt": "e", "artifact_id": "art_3"}]}, {"https://a.com/1"}),
        ("web_deep_research", {"report": "r", "sources": [{"title": "来源", "url": "https://s.com/p", "artifact_id": "art_4"}]}, {"https://s.com/p"}),
    ],
)
def test_new_web_tools_feed_the_report_sources(tool: str, result: dict[str, Any], urls: set[str]) -> None:
    from bowhead_core.sources_resolver import TurnSourcesRegistry

    registry = TurnSourcesRegistry()
    registry.ingest_tool_invocations([ToolInvocation(tool_name=tool, arguments={}, result=result)])
    assert {entry["artifact_id"] for entry in registry.sources} == urls


def test_non_web_tool_sources_key_is_not_treated_as_web_pages() -> None:
    from bowhead_core.sources_resolver import TurnSourcesRegistry

    registry = TurnSourcesRegistry()
    registry.ingest_tool_invocations([ToolInvocation(tool_name="kb_search", arguments={}, result={"sources": [{"url": "https://x.com"}]})])
    assert all(entry.get("artifact_id") != "https://x.com" for entry in registry.sources)


def test_plan_path_backfill_reads_new_result_shapes() -> None:
    from bowhead_core.agents.agent_runner_adapter import _sources_from_web_invocations

    invocations = [
        SimpleNamespace(tool_name="web_extract", arguments={}, result=_EXTRACT_RESULT),
        SimpleNamespace(tool_name="web_deep_research", arguments={"input": "问题"}, result={"sources": [{"title": "S", "url": "https://s.com"}]}),
    ]
    entries = _sources_from_web_invocations(invocations)
    assert [e["url"] for e in entries] == ["https://a.com/x", "https://a.com/empty", "https://s.com"]
    assert entries[-1]["query"] == "问题"


def test_plan_execution_seeds_the_gate_from_the_users_own_messages() -> None:
    """计划执行轮的 text 是占位串：用户贴过的链接要靠 provenance_seed_text 进种子，否则执行步骤里读不了。"""
    from bowhead_core.agents.lead_transient import init_lead_transient
    from bowhead_core.tool_registry import ToolExecutionContext
    from bowhead_core.types import TurnRequest

    ctx = ToolExecutionContext(workspace_id="ws", member_uid="u", user_id="u", datarc=None, turn_id="t_plan")
    request = TurnRequest(
        session_id="s",
        workspace_id="ws",
        user_id="u",
        member_uid="u",
        text="(planning-mode execution)",
        provenance_seed_text="帮我分析 https://x.com/report 这份材料",
    )
    init_lead_transient(ctx, request)
    assert "x.com" in ctx._seen_url_hosts


@pytest.mark.asyncio
async def test_plan_route_collects_prior_user_texts() -> None:
    from bowhead_gateway.routes.plans import _session_user_texts

    turns = [SimpleNamespace(user_text="看看 https://x.com"), SimpleNamespace(user_text="(planning-mode execution)"), SimpleNamespace(user_text="")]

    async def _list_turns(*, session_id: str, limit: int | None = None) -> list[Any]:
        assert session_id == "s_1" and limit == 20
        return turns

    app = SimpleNamespace(state=SimpleNamespace(session_store=SimpleNamespace(list_turns=_list_turns)))
    assert await _session_user_texts(app, "s_1") == "看看 https://x.com"


@pytest.mark.asyncio
async def test_plan_route_seed_failure_is_empty_not_permissive() -> None:
    from bowhead_gateway.routes.plans import _session_user_texts

    async def _boom(**kwargs: Any) -> list[Any]:
        raise RuntimeError("db down")

    app = SimpleNamespace(state=SimpleNamespace(session_store=SimpleNamespace(list_turns=_boom)))
    assert await _session_user_texts(app, "s_1") == ""


@pytest.mark.parametrize(
    ("tool", "result", "zh"),
    [
        ("web_extract", _EXTRACT_RESULT, "读取 2 页"),
        ("web_crawl", {"pages": [{"url": "u"}] * 3}, "抓取 3 页"),
        ("web_map", {"count": 42, "urls": []}, "列出 42 个页面"),
        ("web_deep_research", {"report": "r", "sources": [{"url": "u"}] * 5}, "研究报告 · 5 来源"),
    ],
)
def test_step_row_summaries_are_human_readable(tool: str, result: dict[str, Any], zh: str) -> None:
    from bowhead_core.subagent import _prettify_output_preview

    assert _prettify_output_preview(tool, result, None, ui_language="zh-CN") == zh
