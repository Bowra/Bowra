"""Tests for bowhead_core.web_search (#86).

web_search 已团队化（镜像 image_gen）：handler 由 ``_make_handler(team_search_resolver)``
产出，每次调用按 ``ctx.team_id`` 现解析团队 active search 模型。测试用 fake resolver +
带 team_id 的 ctx 驱动，替代原先的部署级环境变量密钥入口（已删）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from bowhead_core.search_config import SearchModelRegistry, SearchModelSpec


def _spec(api_key: str = "sk-test", protocol: str = "dashscope_compat") -> SearchModelSpec:
    return SearchModelSpec(
        name="qwen-plus-search",
        capability="web_search",
        provider="aliyun",
        real_id="qwen-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key=api_key,
        timeout_sec=25,
        protocol=protocol,
        ssrf_guard=True,
    )


def _resolver(spec: SearchModelSpec | None = None):
    """team_search_resolver 替身——镜像 main.py 的 _team_search_resolver 契约。"""
    reg = None if spec is None else SearchModelRegistry(by_name={spec.name: spec}, active={"web_search": spec.name})

    async def _resolve(team_id: str) -> SearchModelRegistry | None:
        assert team_id, "resolver 不该在无 team_id 时被调用"
        return reg

    return _resolve


def _handler_for(spec: SearchModelSpec | None):
    """spec=SearchModelSpec → 团队配了该模型；spec=None → 团队未配置（resolver 返 None）。"""
    from bowhead_core.web_search import _make_handler

    return _make_handler(_resolver(spec))


def _ctx(*, team_id: str = "team_1", **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(team_id=team_id, **extra)


def _fake_client(fake_resp: Any):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        post = AsyncMock(return_value=fake_resp)

    return FakeClient


def test_search_timeout_has_safe_floor_and_honors_longer_config() -> None:
    from bowhead_core.web_search import _search_timeout_sec

    assert _search_timeout_sec(_spec()) == 45.0
    longer = SearchModelSpec(**{**_spec().__dict__, "timeout_sec": 75})
    assert _search_timeout_sec(longer) == 75.0


@pytest.mark.asyncio
async def test_handler_empty_query_returns_error() -> None:
    handler = _handler_for(_spec())
    result = await handler({}, _ctx())
    assert result["error"] == "query required"
    assert result["results"] == []


@pytest.mark.asyncio
async def test_handler_team_not_configured_returns_error() -> None:
    """团队没配 active search 模型 → 明确的「未配置」（不是静默无工具）。"""
    handler = _handler_for(None)
    result = await handler({"query": "test"}, _ctx())
    assert "not configured" in result["error"]
    assert result["results"] == []


@pytest.mark.asyncio
async def test_handler_no_team_returns_error() -> None:
    """无 team_id = 无团队配置 → 未配置。"""
    handler = _handler_for(_spec())
    result = await handler({"query": "test"}, _ctx(team_id=""))
    assert "not configured" in result["error"]


@pytest.mark.asyncio
async def test_handler_missing_key_returns_error() -> None:
    """团队配了模型但 provider 无 key（soft-disabled）→ 未配置。"""
    handler = _handler_for(_spec(api_key=""))
    result = await handler({"query": "test"}, _ctx())
    assert "not configured" in result["error"]


@pytest.mark.asyncio
async def test_handler_happy_path() -> None:
    fake_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "choices": [
                {
                    "message": {
                        "content": "Summary here [1]",
                        "citations": [
                            {"title": "Foo", "url": "http://foo.com", "snippet": "foo bar"},
                            {"title": "Bar", "url": "http://bar.com", "snippet": "bar baz"},
                        ],
                    },
                }
            ],
        },
        text="",
    )

    handler = _handler_for(_spec())
    with patch("httpx2.AsyncClient", _fake_client(fake_resp)):
        result = await handler({"query": "nvidia earnings 2026"}, _ctx())
    assert result["query"] == "nvidia earnings 2026"
    assert "Summary here" in result["summary"]
    assert len(result["results"]) == 2
    assert result["results"][0]["title"] == "Foo"
    assert result["results"][0]["url"] == "http://foo.com"


@pytest.mark.asyncio
async def test_handler_uses_team_endpoint_and_model() -> None:
    """请求打到**团队配置**的 base_url + real_id（不是写死的 dashscope 常量）。"""
    captured: dict[str, Any] = {}
    fake_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": "ok", "citations": []}}]},
        text="",
    )

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["model"] = json.get("model")
            return fake_resp

    spec = _spec(api_key="sk-team")
    spec = SearchModelSpec(**{**spec.__dict__, "base_url": "https://byo.example/v1", "real_id": "byo-model"})
    handler = _handler_for(spec)
    with patch("httpx2.AsyncClient", FakeClient):
        await handler({"query": "q"}, _ctx())
    assert captured["url"] == "https://byo.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-team"
    assert captured["model"] == "byo-model"


@pytest.mark.asyncio
async def test_handler_applies_search_timeout_floor_to_http_client() -> None:
    captured: dict[str, Any] = {}
    fake_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": "ok", "citations": []}}]},
        text="",
    )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return fake_resp

    handler = _handler_for(_spec())
    with patch("httpx2.AsyncClient", FakeClient):
        await handler({"query": "q"}, _ctx())
    assert captured["timeout"] == 45.0


@pytest.mark.asyncio
async def test_native_handler_applies_search_timeout_floor_to_http_client() -> None:
    captured: dict[str, Any] = {}
    fake_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {"output": {"text": "ok"}},
        text="",
    )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return fake_resp

    handler = _handler_for(_spec(protocol="dashscope_native"))
    with patch("httpx2.AsyncClient", FakeClient):
        result = await handler({"query": "q"}, _ctx())
    assert result["summary"] == "ok"
    assert captured["timeout"] == 45.0


def _tavily_spec() -> SearchModelSpec:
    return SearchModelSpec(
        name="tavily",
        capability="web_search",
        provider="tavily",
        real_id="tavily",
        base_url="https://api.tavily.com",
        api_key="tvly-test",
        timeout_sec=20,
        protocol="tavily",
    )


def _tavily_client(payload: dict[str, Any], captured: dict[str, Any]):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json, headers):
            captured.update(url=url, body=json, headers=headers)
            return SimpleNamespace(status_code=200, json=lambda: payload, headers={}, text="")

    return FakeClient


@pytest.mark.asyncio
async def test_tavily_handler_uses_http_api_and_normalizes_results() -> None:
    captured: dict[str, Any] = {}
    payload = {
        "answer": "A concise answer",
        "results": [{"title": "Tavily", "url": "https://tavily.com", "content": "Search result", "published_date": "2026-09-20"}],
    }
    with patch("httpx2.AsyncClient", _tavily_client(payload, captured)):
        result = await _handler_for(_tavily_spec())({"query": "latest news", "limit": 2}, _ctx())
    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["headers"]["Authorization"] == "Bearer tvly-test"
    assert captured["body"] == {"query": "latest news", "max_results": 2, "include_answer": True, "include_usage": True}
    assert result["summary"] == "A concise answer"
    assert result["results"] == [{"title": "Tavily", "url": "https://tavily.com", "snippet": "Search result", "published_date": "2026-09-20"}]


@pytest.mark.asyncio
async def test_tavily_handler_forwards_filters() -> None:
    captured: dict[str, Any] = {}
    args = {
        "query": "新能源车 政策",
        "topic": "news",
        "time_range": "week",
        "include_domains": ["gov.cn"],
        "exclude_domains": ["example.com"],
        "search_depth": "advanced",
        "country": "China",
    }
    with patch("httpx2.AsyncClient", _tavily_client({"results": [{"title": "t", "url": "https://gov.cn/a", "content": "c"}]}, captured)):
        result = await _handler_for(_tavily_spec())(args, _ctx())
    assert "error" not in result
    assert captured["body"] == {
        "query": "新能源车 政策",
        "max_results": 5,
        "include_answer": True,
        "include_usage": True,
        "topic": "news",
        "time_range": "week",
        "include_domains": ["gov.cn"],
        "exclude_domains": ["example.com"],
        "search_depth": "advanced",
        "country": "china",
    }
    assert "ignored_params" not in result


@pytest.mark.asyncio
async def test_tavily_handler_keeps_long_content_as_snippet() -> None:
    captured: dict[str, Any] = {}
    content = "x" * 1500
    with patch("httpx2.AsyncClient", _tavily_client({"results": [{"title": "t", "url": "https://a.com", "content": content}]}, captured)):
        result = await _handler_for(_tavily_spec())({"query": "q"}, _ctx())
    assert result["results"][0]["snippet"] == content  # No longer truncated to 400 characters


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"topic": "sports"}, "general / news / finance"),
        ({"time_range": "hour"}, "day / week / month / year"),
        ({"start_date": "2026/09/01"}, "YYYY-MM-DD"),
        ({"time_range": "week", "start_date": "2026-09-01"}, "二选一"),
        ({"include_domains": "gov.cn"}, "域名字符串数组"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_filters_are_rejected_with_allowed_values(args: dict[str, Any], needle: str) -> None:
    captured: dict[str, Any] = {}
    with patch("httpx2.AsyncClient", _tavily_client({"results": []}, captured)):
        result = await _handler_for(_tavily_spec())({"query": "q", **args}, _ctx())
    assert needle in result["error"]
    assert result["error_kind"] == "invalid_arguments"
    assert captured == {}  # Invalid arguments never call upstream.


@pytest.mark.asyncio
async def test_tavily_quota_error_reaches_model_with_next_step() -> None:
    class QuotaClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json, headers):
            return SimpleNamespace(status_code=432, json=dict, headers={}, text="")

    with patch("httpx2.AsyncClient", QuotaClient):
        result = await _handler_for(_tavily_spec())({"query": "q"}, _ctx(turn_id="t_quota"))
    assert result["error_kind"] == "tavily_plan_quota_exhausted"
    assert "管理员" in result["error"]


@pytest.mark.asyncio
async def test_non_tavily_backend_reports_ignored_filters() -> None:
    fake_resp = SimpleNamespace(status_code=200, json=lambda: {"choices": [{"message": {"content": "ok"}}]}, text="")
    with patch("httpx2.AsyncClient", _fake_client(fake_resp)):
        result = await _handler_for(_spec())({"query": "q", "time_range": "week", "include_domains": ["gov.cn"]}, _ctx())
    assert result["ignored_params"] == ["include_domains", "time_range"]
    assert result["note"]


@pytest.mark.asyncio
async def test_handler_limit_cap() -> None:
    fake_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "choices": [
                {
                    "message": {
                        "content": "ok",
                        "citations": [{"title": f"T{i}", "url": f"http://u{i}.com", "snippet": "s"} for i in range(20)],
                    },
                }
            ],
        },
        text="",
    )

    handler = _handler_for(_spec())
    with patch("httpx2.AsyncClient", _fake_client(fake_resp)):
        result = await handler({"query": "q", "limit": 99}, _ctx())  # User requests 99 → capped at 10
    assert len(result["results"]) == 10


@pytest.mark.asyncio
async def test_handler_registers_hosts_from_summary() -> None:
    """provenance: DashScope 常 results 空、URL 只在 summary —— 必须从 summary 抽 host 登记,
    否则 Lead 随后 web_fetch 这些搜来的 URL 会被自家 gate 误拦(dogfood Q5/Q6)。"""
    from bowhead_core.url_provenance import host_allowed

    fake_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "choices": [
                {
                    "message": {
                        # Summary contains inline URL, but citations are empty (common with DashScope).
                        "content": "见官方文档 https://docs.python.org/3.13/whatsnew/3.13.html [1]",
                        "citations": [],
                    }
                }
            ]
        },
        text="",
    )

    ctx = _ctx(_seen_url_hosts=set())
    handler = _handler_for(_spec())
    with patch("httpx2.AsyncClient", _fake_client(fake_resp)):
        await handler({"query": "python 3.13"}, ctx)
    # Summary's host is registered → web_fetch allows that URL.
    assert host_allowed(ctx, "https://docs.python.org/3.13/whatsnew/3.13.html") is True


def test_register_no_resolver_returns_false() -> None:
    from bowhead_core.tool_registry import ToolRegistry
    from bowhead_core.web_search import register_web_search_tool

    reg = ToolRegistry()
    assert register_web_search_tool(reg) is False
    assert not reg.has("web_search")


def test_register_with_resolver_registers() -> None:
    from bowhead_core.tool_registry import ToolRegistry
    from bowhead_core.web_search import register_web_search_tool

    reg = ToolRegistry()
    assert register_web_search_tool(reg, team_search_resolver=_resolver(_spec())) is True
    assert reg.has("web_search")


def test_native_url_derivation() -> None:
    """native protocol 的端点从团队 base_url 推出（compat base / bare host / native base 都对）。"""
    from bowhead_core.web_search import _native_url_for

    native = "/api/v1/services/aigc/text-generation/generation"
    assert _native_url_for("https://dashscope.aliyuncs.com/compatible-mode/v1") == f"https://dashscope.aliyuncs.com{native}"
    assert _native_url_for("https://dashscope.aliyuncs.com/api/v1") == f"https://dashscope.aliyuncs.com{native}"
    assert _native_url_for("https://byo.example") == f"https://byo.example{native}"


# ── Native requests must specify message format (bug observed on real 116) ────────────────
#
# DashScope generation returns search_info.search_results **only** for parameters.result_format == "message";
# without it, output has only {finish_reason, text}, and the entire search_info key is absent,
# making `enable_source` ineffective and web_search permanently sourceless. Downstream parser already expected
# message shape (summary falls back to output.choices[0].message.content); request and parsing were
# misaligned from the start, but team search had no configuration since 07-27 and never ran in production.


@pytest.mark.asyncio
async def test_native_request_asks_for_message_result_format() -> None:
    """不要 message 格式 = 拿不到来源。这是「请求侧」的钉子。"""
    sent: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            sent.update(kwargs.get("json") or {})
            return SimpleNamespace(status_code=200, json=lambda: {"output": {"text": "ok"}}, text="")

    handler = _handler_for(_spec(protocol="dashscope_native"))
    with patch("httpx2.AsyncClient", FakeClient):
        await handler({"query": "q"}, _ctx())

    params = sent.get("parameters") or {}
    assert params.get("result_format") == "message", sent
    # Keep both accompanying switches unchanged too.
    assert params.get("enable_search") is True
    assert (params.get("search_options") or {}).get("enable_source") is True


@pytest.mark.asyncio
async def test_native_parses_message_shape_sources() -> None:
    """真机 200 响应的形状（output.choices + output.search_info）必须解析出 summary+来源。"""
    real_shape = {
        "output": {
            "choices": [{"message": {"role": "assistant", "content": "2026 上半年社零 24.87 万亿 [ref_1]"}}],
            "search_info": {
                "search_results": [
                    {"index": 1, "title": "社会消费品零售总额(2026年1-6月)", "url": "https://mp.weixin.qq.com/s/x", "site_name": "腾讯网"},
                    {"index": 2, "title": "统计局月度报告", "url": "https://www.stats.gov.cn/y", "site_name": "国家统计局"},
                ]
            },
        }
    }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            return SimpleNamespace(status_code=200, json=lambda: real_shape, text="")

    handler = _handler_for(_spec(protocol="dashscope_native"))
    with patch("httpx2.AsyncClient", FakeClient):
        result = await handler({"query": "q"}, _ctx())

    assert "24.87" in result["summary"]
    urls = [r["url"] for r in result["results"]]
    assert urls == ["https://mp.weixin.qq.com/s/x", "https://www.stats.gov.cn/y"], result
