"""Tests for bowhead_core.web_tavily_tools — web_extract / web_map / web_crawl / web_deep_research（Tavily 后端，FEATURE_TAVILY_TOOLS）。"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from bowhead_core.terminal_effect_contract import TOOL_EFFECT_READ_ONLY
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_core.web_tavily_tools import TavilyCredential, register_web_tavily_tools

from bowhead_core import tavily_client

_CRED = TavilyCredential(api_key="tvly-key", source_id="ibt_1")


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    tavily_client._turn_blocks.clear()
    monkeypatch.setenv("FEATURE_TAVILY_TOOLS", "1")
    monkeypatch.delenv("BOWHEAD_WEB_FETCH_PROVENANCE", raising=False)


def _resolver(cred: TavilyCredential | None = _CRED):
    async def _resolve(team_id: str) -> TavilyCredential | None:
        return cred

    return _resolve


def _registry(cred: TavilyCredential | None = _CRED, store: Any = None) -> ToolRegistry:
    registry = ToolRegistry()
    assert register_web_tavily_tools(registry, _resolver(cred), store_getter=(lambda: store) if store else None)
    return registry


class _ArtifactStore:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    async def put_bytes(self, **kwargs: Any) -> SimpleNamespace:
        self.puts.append(kwargs)
        return SimpleNamespace(id=f"art_{len(self.puts)}")


def _ctx(seen: set[str] | None = None, turn_id_override: str = "t_1", **extra: Any) -> ToolExecutionContext:
    ctx = ToolExecutionContext(workspace_id="ws_1", member_uid="u", user_id="u_1", datarc=None, team_id="team_1", turn_id=turn_id_override, **extra)
    ctx._seen_url_hosts = seen
    ctx.artifact_store = _ArtifactStore()
    return ctx


def _client(payload: dict[str, Any], calls: list[dict[str, Any]], status: int = 200):
    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls.append({"timeout": kwargs.get("timeout")})

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> SimpleNamespace:
            calls[-1].update(url=url, body=json, headers=headers)
            return SimpleNamespace(status_code=status, json=lambda: payload, headers={}, text="")

    return FakeClient


async def _call(registry: ToolRegistry, name: str, args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    return await registry.get(name).handler(args, ctx)


# ── registration ─────────────────────────────────────────────────────────────


def test_not_registered_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_TAVILY_TOOLS", "0")
    registry = ToolRegistry()
    assert register_web_tavily_tools(registry, _resolver()) is False
    assert not registry.has("web_extract") and not registry.has("web_map")


def test_flag_off_whitelisted_names_are_silently_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """回滚路径：manifest 白名单里写着这些工具、但开关关着没注册时，工具清单静默略过，不报错。"""
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.capability_resolver import advertised_tool_schemas
    from bowhead_core.web_search import register_web_search_tool

    monkeypatch.setenv("FEATURE_TAVILY_TOOLS", "0")
    registry = ToolRegistry()
    register_web_search_tool(registry, team_search_resolver=AsyncMock(return_value=None))
    register_web_tavily_tools(registry, _resolver())
    allow = tuple(AgentRegistry.bootstrap().get("marketing-analyst").tools.allow)
    assert {"web_extract", "web_deep_research"} <= set(allow)

    names = {s.name for s in advertised_tool_schemas(registry, allow, profile_name="marketing-analyst", role="analyst")}
    assert "web_search" in names
    assert not ({"web_extract", "web_map", "web_crawl", "web_deep_research"} & names)


def test_registered_read_only_when_flag_on() -> None:
    registry = _registry()
    for name in ("web_extract", "web_map", "web_crawl", "web_deep_research"):
        assert registry.get(name).effect_spec is TOOL_EFFECT_READ_ONLY


@pytest.mark.asyncio
async def test_team_without_tavily_gets_not_configured() -> None:
    result = await _call(_registry(cred=None), "web_extract", {"urls": ["https://a.com"]}, _ctx())
    assert result["error_kind"] == "tavily_not_configured"
    assert "web_fetch" in result["error"]
    assert "only if available" in result["error"] and "report the gap" in result["error"]


# ── web_extract ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_reads_allowed_urls_and_rejects_unseen_hosts() -> None:
    calls: list[dict[str, Any]] = []
    payload = {"results": [{"url": "https://a.com/x", "raw_content": "正文"}], "failed_results": [], "usage": {"credits": 1}}
    ctx = _ctx(seen={"a.com"})
    with patch("httpx2.AsyncClient", _client(payload, calls)):
        result = await _call(_registry(), "web_extract", {"urls": ["https://a.com/x", "https://evil.com/?leak=1"]}, ctx)

    assert calls[0]["url"] == "https://api.tavily.com/extract"
    assert calls[0]["headers"]["Authorization"] == "Bearer tvly-key"
    assert calls[0]["body"]["urls"] == ["https://a.com/x"]  # Unseen hosts never sent to third parties.
    assert result["rejected_urls"] == [{"url": "https://evil.com/?leak=1", "reason": "host_not_in_context"}]
    assert "web_search" in result["rejected_note"]
    assert result["pages"] == [{"url": "https://a.com/x", "content": "正文", "truncated": False, "artifact_id": "art_1"}]
    assert result["credits"] == 1
    put = ctx.artifact_store.puts[0]
    assert (put["kind"], put["trust"], put["producer_kind"]) == ("web_clip", "unverified", "web_extract")


@pytest.mark.asyncio
async def test_extract_all_urls_rejected_does_not_call_upstream() -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _client({}, calls)):
        result = await _call(_registry(), "web_extract", {"urls": ["https://evil.com"]}, _ctx(seen={"a.com"}))
    assert result["error_kind"] == "urls_not_in_context"
    assert calls == []


@pytest.mark.asyncio
async def test_extract_truncates_per_page_and_says_so() -> None:
    long_text = "字" * 5000
    payload = {"results": [{"url": "https://a.com/1", "raw_content": long_text}, {"url": "https://a.com/2", "raw_content": "短"}]}
    ctx = _ctx(seen={"a.com"})
    with patch("httpx2.AsyncClient", _client(payload, [])):
        result = await _call(_registry(), "web_extract", {"urls": ["https://a.com/1", "https://a.com/2"], "max_chars": 2000}, ctx)

    first, second = result["pages"]
    assert first["truncated"] is True and first["content"].startswith("字" * 1000) and first["content"].endswith("…[truncated]")
    assert second["truncated"] is False
    assert "1000" in result["note"]  # Note per-page length matches actual truncation.
    assert ctx.artifact_store.puts[0]["content"].decode().count("字") == 5000  # Persist full body.


@pytest.mark.asyncio
async def test_extract_all_failed_conditions_the_optional_page_reader() -> None:
    payload = {"results": [], "failed_results": [{"url": "https://a.com/x", "error": "timeout"}]}
    with patch("httpx2.AsyncClient", _client(payload, [])):
        result = await _call(_registry(), "web_extract", {"urls": ["https://a.com/x"]}, _ctx(seen={"a.com"}))
    assert result["error_kind"] == "web_extract_all_failed"
    assert result["failed"] == [{"url": "https://a.com/x", "error": "timeout"}]
    assert "web_fetch" in result["error"]
    assert "only if available" in result["error"] and "report the gap" in result["error"]


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"urls": []}, "至少"),
        ({"urls": [f"https://a.com/{i}" for i in range(21)]}, "最多 20"),
        ({"urls": ["https://a.com"], "extract_depth": "deep"}, "basic / advanced"),
        ({"urls": ["https://a.com"], "max_chars": 10}, "500–50000"),
    ],
)
@pytest.mark.asyncio
async def test_extract_invalid_args_name_the_allowed_values(args: dict[str, Any], needle: str) -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _client({}, calls)):
        result = await _call(_registry(), "web_extract", args, _ctx(seen={"a.com"}))
    assert needle in result["error"]
    assert calls == []


@pytest.mark.asyncio
async def test_extract_quota_error_is_returned_with_rejections() -> None:
    with patch("httpx2.AsyncClient", _client({}, [], status=432)):
        result = await _call(_registry(), "web_extract", {"urls": ["https://a.com", "https://b.com"]}, _ctx(seen={"a.com"}))
    assert result["error_kind"] == "tavily_plan_quota_exhausted"
    assert result["rejected_urls"][0]["url"] == "https://b.com"


# ── web_map ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_map_defaults_to_same_site_and_registers_found_hosts() -> None:
    calls: list[dict[str, Any]] = []
    payload = {"base_url": "https://a.com", "results": ["https://a.com/p1", "https://docs.a.com/p2", "javascript:alert(1)"]}
    ctx = _ctx(seen={"a.com"})
    with patch("httpx2.AsyncClient", _client(payload, calls)):
        result = await _call(_registry(), "web_map", {"url": "https://a.com"}, ctx)

    assert calls[0]["url"] == "https://api.tavily.com/map"
    assert calls[0]["body"] == {
        "url": "https://a.com",
        "max_depth": 1,
        "max_breadth": 20,
        "limit": 50,
        "allow_external": False,
        "include_usage": True,
    }
    assert calls[0]["timeout"] == 170.0
    assert result["urls"] == ["https://a.com/p1", "https://docs.a.com/p2"]
    assert "docs.a.com" in ctx._seen_url_hosts  # Later web_extract can read discovered pages.


@pytest.mark.asyncio
async def test_map_says_when_list_is_cut() -> None:
    payload = {"results": [f"https://a.com/{i}" for i in range(250)]}
    with patch("httpx2.AsyncClient", _client(payload, [])):
        result = await _call(_registry(), "web_map", {"url": "https://a.com", "limit": 300}, _ctx(seen={"a.com"}))
    assert result["count"] == 250 and len(result["urls"]) == 200
    assert "250" in result["note"]


@pytest.mark.asyncio
async def test_map_rejects_unseen_start_url() -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _client({}, calls)):
        result = await _call(_registry(), "web_map", {"url": "https://evil.com"}, _ctx(seen={"a.com"}))
    assert result["error_kind"] == "urls_not_in_context"
    assert calls == []


@pytest.mark.asyncio
async def test_map_invalid_depth_names_the_range() -> None:
    result = await _call(_registry(), "web_map", {"url": "https://a.com", "max_depth": 9}, _ctx(seen={"a.com"}))
    assert "1–5" in result["error"]


# ── web_crawl ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_crawl_defaults_and_long_timeout() -> None:
    calls: list[dict[str, Any]] = []
    payload = {"base_url": "https://a.com", "results": [{"url": "https://a.com/1", "raw_content": "一"}], "usage": {"credits": 3}}
    with patch("httpx2.AsyncClient", _client(payload, calls)):
        result = await _call(_registry(), "web_crawl", {"url": "https://a.com"}, _ctx(seen={"a.com"}))

    assert calls[0]["url"] == "https://api.tavily.com/crawl"
    assert calls[0]["timeout"] == 170.0
    assert calls[0]["body"] == {
        "url": "https://a.com",
        "max_depth": 1,
        "max_breadth": 20,
        "limit": 20,
        "allow_external": False,
        "extract_depth": "basic",
        "format": "markdown",
        "timeout": 150,
        "include_usage": True,
    }
    assert result["count"] == 1 and result["credits"] == 3


@pytest.mark.asyncio
async def test_crawl_persists_every_page_and_says_how_much_is_shown() -> None:
    payload = {"results": [{"url": f"https://a.com/{i}", "raw_content": "字" * 3000} for i in range(4)] + [{"url": "javascript:x"}]}
    ctx = _ctx(seen={"a.com"})
    with patch("httpx2.AsyncClient", _client(payload, [])):
        result = await _call(_registry(), "web_crawl", {"url": "https://a.com", "max_chars": 4000}, ctx)

    assert result["count"] == 4  # Drop non-HTTP links.
    assert [p["artifact_id"] for p in result["pages"]] == ["art_1", "art_2", "art_3", "art_4"]
    assert all(p["excerpt"].startswith("字" * 1000) and p["excerpt"].endswith("…[truncated]") for p in result["pages"])
    assert all(p["chars"] == 3000 for p in result["pages"])
    assert "共抓到 4 页" in result["note"] and "1000" in result["note"]
    assert {put["producer_kind"] for put in ctx.artifact_store.puts} == {"web_crawl"}
    assert all(put["content"].decode().count("字") == 3000 for put in ctx.artifact_store.puts)  # Persist full text.


@pytest.mark.asyncio
async def test_crawl_empty_result_suggests_next_step() -> None:
    with patch("httpx2.AsyncClient", _client({"results": []}, [])):
        result = await _call(_registry(), "web_crawl", {"url": "https://a.com"}, _ctx(seen={"a.com"}))
    assert result["error_kind"] == "web_crawl_empty"
    assert "web_map" in result["error"]
    assert "only if available" in result["error"] and "report the gap" in result["error"]


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"extract_depth": "deep"}, "basic / advanced"),
        ({"max_chars": 10}, "500–50000"),
        ({"max_depth": 0}, "1–5"),
        ({"select_paths": "/docs"}, "字符串数组"),
    ],
)
@pytest.mark.asyncio
async def test_crawl_invalid_args_do_not_call_upstream(args: dict[str, Any], needle: str) -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _client({}, calls)):
        result = await _call(_registry(), "web_crawl", {"url": "https://a.com", **args}, _ctx(seen={"a.com"}))
    assert needle in result["error"]
    assert calls == []


@pytest.mark.asyncio
async def test_crawl_rejects_unseen_start_url() -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _client({}, calls)):
        result = await _call(_registry(), "web_crawl", {"url": "https://evil.com"}, _ctx(seen={"a.com"}))
    assert result["error_kind"] == "urls_not_in_context"
    assert calls == []


# ── web_deep_research ────────────────────────────────────────────────────────


def _seq_client(responses: list[tuple[int, dict[str, Any]]], calls: list[dict[str, Any]]):
    """按顺序回放 POST / GET 响应，记录每次请求。"""
    queue = list(responses)

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> SimpleNamespace:
            calls.append({"method": "POST", "url": url, "body": json})
            status, payload = queue.pop(0)
            return SimpleNamespace(status_code=status, json=lambda: payload, headers={}, text="")

        async def get(self, url: str, *, headers: dict[str, str]) -> SimpleNamespace:
            calls.append({"method": "GET", "url": url})
            status, payload = queue.pop(0)
            return SimpleNamespace(status_code=status, json=lambda: payload, headers={}, text="")

    return FakeClient


@pytest.fixture
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from bowhead_core import web_tavily_tools

    web_tavily_tools._research_turns.clear()
    sleep = AsyncMock()
    monkeypatch.setattr("anyio.sleep", sleep)
    return sleep


_CREATED = (201, {"request_id": "req_1", "status": "pending"})
_RUNNING = (202, {"request_id": "req_1", "status": "in_progress"})


def _completed(content: str, n_sources: int = 2) -> tuple[int, dict[str, Any]]:
    sources = [{"title": f"来源{i + 1}", "url": f"https://s{i + 1}.com/p"} for i in range(n_sources)]
    return 200, {"request_id": "req_1", "status": "completed", "content": content, "sources": sources, "usage": {"credits": 12}}


@pytest.mark.asyncio
async def test_research_polls_until_done_and_rewrites_citations(_no_sleep: AsyncMock) -> None:
    calls: list[dict[str, Any]] = []
    ctx = _ctx(seen=set())
    responses = [_CREATED, _RUNNING, _completed("结论甲[1]，结论乙[2]，编号越界[9]。")]
    with patch("httpx2.AsyncClient", _seq_client(responses, calls)):
        result = await _call(_registry(), "web_deep_research", {"input": "新能源车出海趋势"}, ctx)

    assert calls[0] == {
        "method": "POST",
        "url": "https://api.tavily.com/research",
        "body": {"input": "新能源车出海趋势", "citation_format": "numbered", "model": "mini", "output_length": "standard", "include_usage": True},
    }
    assert [c["url"] for c in calls[1:]] == ["https://api.tavily.com/research/req_1?include_usage=true"] * 2
    assert _no_sleep.await_count == 2
    # Resolve this run's [n] against its source table to stored IDs; remove out-of-range numbers, never expose bare [n].
    assert result["report"] == "结论甲[data:art_1]，结论乙[data:art_2]，编号越界。"
    assert [s["artifact_id"] for s in result["sources"]] == ["art_1", "art_2"]
    assert result["report_artifact_id"] == "art_3"
    assert (result["model"], result["credits"], result["request_id"]) == ("mini", 12, "req_1")
    assert {"s1.com", "s2.com"} <= ctx._seen_url_hosts
    assert {put["producer_kind"] for put in ctx.artifact_store.puts} == {"web_deep_research"}


@pytest.mark.asyncio
async def test_research_numbers_are_translated_per_run(_no_sleep: AsyncMock) -> None:
    """两次运行都写 [1]，但指向不同来源：各自按本次来源表翻，不串号。"""
    first_run = (200, {"status": "completed", "content": "甲[1]", "sources": [{"title": "A", "url": "https://a.com"}]})
    second_run = (200, {"status": "completed", "content": "乙[1]", "sources": [{"title": "B", "url": "https://b.com"}]})
    ctx_a, ctx_b = _ctx(turn_id_override="t_a"), _ctx(turn_id_override="t_b")
    ctx_b.artifact_store = ctx_a.artifact_store  # Shared store, sequential IDs expose cross-run mixups.
    with patch("httpx2.AsyncClient", _seq_client([_CREATED, first_run, _CREATED, second_run], [])):
        one = await _call(_registry(), "web_deep_research", {"input": "问题"}, ctx_a)
        two = await _call(_registry(), "web_deep_research", {"input": "问题"}, ctx_b)

    # First: source art_1, report art_2; second: source art_3, report art_4.
    assert (one["report"], one["sources"][0]["url"]) == ("甲[data:art_1]", "https://a.com")
    assert (two["report"], two["sources"][0]["url"]) == ("乙[data:art_3]", "https://b.com")


@pytest.mark.asyncio
async def test_research_is_limited_to_once_per_turn(_no_sleep: AsyncMock) -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _seq_client([_CREATED, _completed("甲[1]", 1)], calls)):
        await _call(_registry(), "web_deep_research", {"input": "问题一"}, _ctx())
        second = await _call(_registry(), "web_deep_research", {"input": "问题二"}, _ctx())

    assert second["error_kind"] == "web_deep_research_turn_limit"
    assert "only if available" in second["error"] and "report gaps" in second["error"]
    assert "art_2" in second["error"]  # Points to this turn's existing report.
    assert len(calls) == 2  # Second call makes no upstream request.


@pytest.mark.asyncio
async def test_research_turn_slot_is_released_when_task_was_not_created(_no_sleep: AsyncMock) -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _seq_client([(500, {}), _CREATED, _completed("甲", 0)], calls)):
        first = await _call(_registry(), "web_deep_research", {"input": "问题"}, _ctx())
        second = await _call(_registry(), "web_deep_research", {"input": "问题"}, _ctx())

    assert first["error_kind"] == "tavily_unavailable"
    assert "error" not in second


@pytest.mark.asyncio
async def test_research_failed_status(_no_sleep: AsyncMock) -> None:
    with patch("httpx2.AsyncClient", _seq_client([_CREATED, (200, {"status": "failed"})], [])):
        result = await _call(_registry(), "web_deep_research", {"input": "问题"}, _ctx())
    assert (result["error_kind"], result["request_id"]) == ("web_deep_research_failed", "req_1")


@pytest.mark.asyncio
async def test_research_gives_up_at_the_wait_budget(_no_sleep: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core import web_tavily_tools

    monkeypatch.setattr(web_tavily_tools, "_RESEARCH_BUDGET_SEC", 0.0)
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _seq_client([_CREATED], calls)):
        result = await _call(_registry(), "web_deep_research", {"input": "问题"}, _ctx())
    assert (result["error_kind"], result["request_id"]) == ("web_deep_research_timeout", "req_1")
    assert "only if available" in result["error"] and "report gaps" in result["error"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_research_stops_after_repeated_poll_errors(_no_sleep: AsyncMock) -> None:
    with patch("httpx2.AsyncClient", _seq_client([_CREATED, (500, {}), (502, {}), (503, {})], [])):
        result = await _call(_registry(), "web_deep_research", {"input": "问题"}, _ctx())
    assert result["error_kind"] == "tavily_unavailable"


@pytest.mark.asyncio
async def test_research_cancellation_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from bowhead_core import web_tavily_tools

    web_tavily_tools._research_turns.clear()
    monkeypatch.setattr("anyio.sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with patch("httpx2.AsyncClient", _seq_client([_CREATED], [])), pytest.raises(asyncio.CancelledError):
        await _call(_registry(), "web_deep_research", {"input": "问题"}, _ctx())


@pytest.mark.asyncio
async def test_research_long_report_is_cut_with_a_note(_no_sleep: AsyncMock) -> None:
    with patch("httpx2.AsyncClient", _seq_client([_CREATED, _completed("字" * 15000, 0)], [])):
        result = await _call(_registry(), "web_deep_research", {"input": "问题"}, _ctx())
    assert len(result["report"]) == 12000
    assert "15000" in result["note"]


@pytest.mark.parametrize(("args", "needle"), [({"input": ""}, "input 必填"), ({"input": "q", "model": "auto"}, "mini / pro")])
@pytest.mark.asyncio
async def test_research_invalid_args(args: dict[str, Any], needle: str, _no_sleep: AsyncMock) -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _seq_client([], calls)):
        result = await _call(_registry(), "web_deep_research", args, _ctx())
    assert needle in result["error"]
    assert calls == []


# ── Review fixes: exfiltration, budgets, research polling ───────


@pytest.mark.parametrize("url", ["https://evil.com\\@a.com/", "https://a.com@evil.com/", "https://a .com/", "https://a%2ecom/"])
@pytest.mark.asyncio
async def test_ambiguous_urls_never_reach_upstream(url: str) -> None:
    """解析器之间会读出不同主机的写法一律拒掉（Python 读成 a.com 过闸、Tavily 可能去 evil.com）。"""
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _client({}, calls)):
        result = await _call(_registry(), "web_extract", {"urls": [url]}, _ctx(seen={"a.com"}))
    assert result["error_kind"] == "urls_not_in_context"
    assert result["rejected_urls"][0]["reason"] == "not_http_url"
    assert calls == []


@pytest.mark.asyncio
async def test_map_with_external_links_only_trusts_the_same_site() -> None:
    """外链是页面里的不可信内容：allow_external 列出来可以，但不能因此变成「已见」而被读取。"""
    payload = {"results": ["https://a.com/1", "https://docs.a.com/2", "https://evil.com/leak"]}
    ctx = _ctx(seen={"a.com"})
    with patch("httpx2.AsyncClient", _client(payload, [])):
        result = await _call(_registry(), "web_map", {"url": "https://a.com", "allow_external": True}, ctx)
    assert "https://evil.com/leak" in result["urls"]
    assert "docs.a.com" in ctx._seen_url_hosts
    assert "evil.com" not in ctx._seen_url_hosts


@pytest.mark.asyncio
async def test_crawl_only_trusts_the_same_site() -> None:
    payload = {"results": [{"url": "https://a.com/1", "raw_content": "x"}, {"url": "https://evil.com/2", "raw_content": "y"}]}
    ctx = _ctx(seen={"a.com"})
    with patch("httpx2.AsyncClient", _client(payload, [])):
        await _call(_registry(), "web_crawl", {"url": "https://a.com", "allow_external": True}, ctx)
    assert "evil.com" not in ctx._seen_url_hosts


@pytest.mark.parametrize(("tool", "limit", "needle"), [("web_crawl", 101, "1–100"), ("web_map", 501, "1–500")])
@pytest.mark.asyncio
async def test_site_limit_is_bounded(tool: str, limit: int, needle: str) -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _client({}, calls)):
        result = await _call(_registry(), tool, {"url": "https://a.com", "limit": limit}, _ctx(seen={"a.com"}))
    assert needle in result["error"]
    assert calls == []


@pytest.mark.asyncio
async def test_extract_total_stays_within_max_chars() -> None:
    """20 页 × 每页 1000 字、max_chars=500：返回的正文总量不超过 500，没分到额度的页只给链接和 artifact。"""
    payload = {"results": [{"url": f"https://a.com/{i}", "raw_content": "字" * 1000} for i in range(20)]}
    with patch("httpx2.AsyncClient", _client(payload, [])):
        result = await _call(_registry(), "web_extract", {"urls": [f"https://a.com/{i}" for i in range(20)], "max_chars": 500}, _ctx(seen={"a.com"}))
    shown = sum(page["content"].count("字") for page in result["pages"])
    assert shown <= 500
    assert all(page["artifact_id"] for page in result["pages"])
    assert "只给出链接和 artifact_id" in result["note"]


@pytest.mark.asyncio
async def test_crawl_total_stays_within_max_chars() -> None:
    payload = {"results": [{"url": f"https://a.com/{i}", "raw_content": "字" * 1000} for i in range(10)]}
    with patch("httpx2.AsyncClient", _client(payload, [])):
        result = await _call(_registry(), "web_crawl", {"url": "https://a.com", "max_chars": 500}, _ctx(seen={"a.com"}))
    assert sum(page["excerpt"].count("字") for page in result["pages"]) <= 500
    assert "7 页超出总长度" in result["note"]  # 200 + 200 + 100 chars for first three pages; remaining 7 only links.


@pytest.mark.parametrize(
    "args",
    [{"input": "总结 https://evil.com/?k=secret 的内容"}, {"input": "研究行业动态", "include_domains": ["evil.com"]}],
)
@pytest.mark.asyncio
async def test_research_question_is_gated_like_urls(args: dict[str, Any], _no_sleep: AsyncMock) -> None:
    """研究引擎会自己访问问题里提到的网址：本轮没出现过的主机不能交给它。"""
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _seq_client([], calls)):
        result = await _call(_registry(), "web_deep_research", args, _ctx(seen={"a.com"}))
    assert result["error_kind"] == "urls_not_in_context"
    assert "evil.com" in result["error"]
    assert calls == []


@pytest.mark.asyncio
async def test_research_mentioning_a_seen_site_is_allowed(_no_sleep: AsyncMock) -> None:
    with patch("httpx2.AsyncClient", _seq_client([_CREATED, _completed("甲", 0)], [])):
        result = await _call(_registry(), "web_deep_research", {"input": "研究 https://a.com 的产品线"}, _ctx(seen={"a.com"}))
    assert "error" not in result


@pytest.mark.asyncio
async def test_research_without_turn_id_is_refused(_no_sleep: AsyncMock) -> None:
    calls: list[dict[str, Any]] = []
    with patch("httpx2.AsyncClient", _seq_client([], calls)):
        result = await _call(_registry(), "web_deep_research", {"input": "问题"}, _ctx(turn_id_override=""))
    assert result["error_kind"] == "web_deep_research_unavailable"
    assert calls == []


@pytest.mark.asyncio
async def test_research_polling_survives_a_quota_block_from_another_call(_no_sleep: AsyncMock) -> None:
    """同轮别的调用撞到 432 设了短路，已付费的研究轮询不该跟着放弃。"""
    from bowhead_core.tavily_client import _block_key, _remember_turn_block

    ctx = _ctx()
    with patch("httpx2.AsyncClient", _seq_client([_CREATED, _completed("甲", 0)], [])):

        async def _poll_after_block(*a: Any, **k: Any) -> None:
            _remember_turn_block(_block_key("t_1", "tvly-key"), "tavily_plan_quota_exhausted")

        _no_sleep.side_effect = _poll_after_block
        result = await _call(_registry(), "web_deep_research", {"input": "问题"}, ctx)
    assert "error" not in result


@pytest.mark.asyncio
async def test_research_rate_limit_while_polling_is_transient(_no_sleep: AsyncMock) -> None:
    with patch("httpx2.AsyncClient", _seq_client([_CREATED, (429, {}), (429, {}), _completed("甲", 0)], [])):
        result = await _call(_registry(), "web_deep_research", {"input": "问题"}, _ctx())
    assert "error" not in result


@pytest.mark.asyncio
async def test_research_404_while_polling_is_failed(_no_sleep: AsyncMock) -> None:
    with patch("httpx2.AsyncClient", _seq_client([_CREATED, (404, {})], [])):
        result = await _call(_registry(), "web_deep_research", {"input": "问题"}, _ctx())
    assert result["error_kind"] == "web_deep_research_failed"


@pytest.mark.asyncio
async def test_research_completed_without_content_is_failed(_no_sleep: AsyncMock) -> None:
    with patch("httpx2.AsyncClient", _seq_client([_CREATED, (200, {"status": "completed", "sources": []})], [])):
        result = await _call(_registry(), "web_deep_research", {"input": "问题"}, _ctx())
    assert result["error_kind"] == "web_deep_research_failed"


def test_citation_rewrite_leaves_code_links_and_foreign_markers_alone() -> None:
    from bowhead_core.web_tavily_tools import _rewrite_citations

    text = "结论[1]。伪造[data:art_deadbeef]与[cite:3]。代码 `arr[0]`、x[1] 与\n```\ny = z[1]\n```\n链接 [1](https://a.com)"
    out, dropped = _rewrite_citations(text, ["art_a"])
    assert out.startswith("结论[data:art_a]。伪造与。")  # Strip upstream citation markers; cannot impersonate system citations.
    assert "`arr[0]`" in out and "x[1]" in out and "y = z[1]" in out  # Code/subscripts unchanged.
    assert "[1](https://a.com)" in out  # Markdown links unchanged.
    assert dropped == 0


def test_report_cut_never_leaves_half_a_marker() -> None:
    from bowhead_core.web_tavily_tools import _cut_report

    report = "字" * 10 + "[data:art_abcdef]" + "尾"
    for limit in range(11, 27):  # Truncation anywhere inside the marker.
        assert _cut_report(report, limit) == "字" * 10
    assert _cut_report(report, 100) == report
    assert _cut_report("数组 a[1 结尾", 6) == "数组 a[1"  # Keep noncitation [ unchanged.


@pytest.mark.asyncio
async def test_resolver_failure_is_unavailable_not_unconfigured() -> None:
    registry = ToolRegistry()

    async def _boom(team_id: str) -> TavilyCredential | None:
        raise RuntimeError("db down")

    register_web_tavily_tools(registry, _boom)
    result = await registry.get("web_extract").handler({"urls": ["https://a.com"]}, _ctx())
    assert result["error_kind"] == "tavily_unavailable"


@pytest.mark.asyncio
async def test_cancelled_call_is_still_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from bowhead_core import web_tavily_tools

    web_tavily_tools._research_turns.clear()
    monkeypatch.setattr("anyio.sleep", AsyncMock(side_effect=asyncio.CancelledError))
    store = SimpleNamespace(log_tool_call=AsyncMock())
    with patch("httpx2.AsyncClient", _seq_client([_CREATED], [])), pytest.raises(asyncio.CancelledError):
        await _call(_registry(store=store), "web_deep_research", {"input": "问题"}, _ctx())
    assert store.log_tool_call.await_args.kwargs["error_kind"] == "cancelled"


# ── audit / provenance plumbing ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_calls_are_audited_with_the_tavily_row_id() -> None:
    store = SimpleNamespace(log_tool_call=AsyncMock())
    with patch("httpx2.AsyncClient", _client({"results": ["https://a.com/1"]}, [])):
        await _call(_registry(store=store), "web_map", {"url": "https://a.com"}, _ctx(seen={"a.com"}))
    kwargs = store.log_tool_call.await_args.kwargs
    assert (kwargs["team_id"], kwargs["tool_name"], kwargs["source_id"], kwargs["status"]) == ("team_1", "web_map", "ibt_1", "ok")
    assert "https://a.com" not in str(kwargs)  # Store argument summary only, never original text.


def test_seen_hosts_survive_dataclass_replace() -> None:
    """计划步骤路径用 dataclasses.replace 造 peer ctx；已见 host 集合必须跟过去（否则来源闸放行）。"""
    ctx = _ctx(seen={"a.com"})
    peer = dataclasses.replace(ctx, workspace_id="ws_2")
    assert peer._seen_url_hosts is ctx._seen_url_hosts

    from bowhead_core.url_provenance import host_allowed

    assert host_allowed(peer, "https://evil.com") is False


@pytest.mark.asyncio
async def test_unconfigured_team_hides_tavily_tools() -> None:
    from bowhead_core.capability_resolver import resolve_unconfigured_capability_tools

    hidden = await resolve_unconfigured_capability_tools(SimpleNamespace(team_tavily_resolver=_resolver(None)), "team_1")
    shown = await resolve_unconfigured_capability_tools(SimpleNamespace(team_tavily_resolver=_resolver()), "team_1")
    assert {"web_extract", "web_map", "web_crawl", "web_deep_research"} <= hidden
    assert not ({"web_extract", "web_map", "web_crawl", "web_deep_research"} & shown)


def test_authors_can_select_the_new_tools() -> None:
    from bowhead_core.subagent_authoring_catalog import NATIVE_ALLOWLIST, classify_native_risk

    for name in ("web_extract", "web_map", "web_crawl", "web_deep_research"):
        assert name in NATIVE_ALLOWLIST
        assert classify_native_risk(name)[0] == "read"
