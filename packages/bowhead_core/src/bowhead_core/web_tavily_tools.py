"""Tavily 后端的原生网页工具：``web_extract``（批量读网页正文）、``web_map``（列出站点页面）、``web_crawl``（抓取站点多页正文）与
``web_deep_research``（异步深度研究，工具内轮询，每轮最多 1 次）。

设计：docs/sprints/tavily-tools-2026-09-24/tech-design.md §3、§5。
- 名字与供应商无关；Tavily 是当前唯一后端，出站统一经 ``tavily_client``（Bearer、错误分类、同轮短路）。
- 只在 ``FEATURE_TAVILY_TOOLS`` 开启时注册；团队未启用 Tavily 时由
  ``capability_resolver.resolve_unconfigured_capability_tools`` 从模型工具清单里隐藏，handler 每次调用仍重读启用状态。
- 所有 URL 入参先过与 web_fetch 相同的来源闸：只能用本轮已出现过的 host（用户消息、web_search 结果、本模块工具返回的同站链接），
  且必须是解析无歧义的 http(s) URL；深度研究的问题文本与 include_domains 里出现的主机同样过闸。
- map / crawl 只把与起始网址同站的 host 登记为「已见」：页面里的外链是不可信内容，不能借此变成可信来源。
- 正文存成 ``web_clip``（trust=unverified），返回给模型的正文按 ``max_chars`` 截断并明说，完整正文在 artifact 里。
- 失败不回落到其他来源。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import anyio
from bowhead_router.types import ToolSchema

from bowhead_core.tavily_client import tavily_get, tavily_post
from bowhead_core.terminal_effect_contract import TOOL_EFFECT_READ_ONLY
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

TAVILY_TOOL_NAMES: tuple[str, ...] = ("web_extract", "web_map", "web_crawl", "web_deep_research")


@dataclass(frozen=True)
class TavilyCredential:
    api_key: str
    source_id: str  # team_builtin_tool row ID for auditing.


TeamTavilyResolver = Callable[[str], Awaitable[TavilyCredential | None]]

_EXTRACT_MAX_URLS = 20
_DEFAULT_MAX_CHARS = 12000
_HARD_MAX_CHARS = 50000
_EXTRACT_DEPTHS = ("basic", "advanced")
# Upstream default timeouts: basic 10s, advanced 30s; httpx2 adds 15s.
_EXTRACT_HTTP_TIMEOUT = {"basic": 25.0, "advanced": 45.0}
# map upstream timeout defaults to and caps at 150s; httpx2 adds 20s.
_MAP_HTTP_TIMEOUT = 170.0
_CRAWL_HTTP_TIMEOUT = 170.0
_MAP_DEFAULT_LIMIT = 50
# crawl fetches every page body (map + extract costs); default is below upstream 50, but model may explicitly raise it.
_CRAWL_DEFAULT_LIMIT = 20
# Limit caps: map lists links; crawl fetches, stores, and bills each page body.
_MAP_MAX_LIMIT = 500
_CRAWL_MAX_LIMIT = 100
_MAP_RETURN_URLS = 200

_UNAVAILABLE = {
    "error": "暂时读不到团队的 Tavily 配置，请稍后再试；不要改用其他来源顶替。",
    "error_kind": "tavily_unavailable",
}

_NOT_CONFIGURED = {
    "error": (
        "Tavily is not enabled for this team. Ask a team admin to enable it. "
        "Use web_fetch for a known page only if available; otherwise ask for the page content or report the gap."
    ),
    "error_kind": "tavily_not_configured",
}

_SOURCE_HINT = "可用来源：用户消息里的链接，以及 web_search、web_map、web_crawl、web_deep_research 返回的链接；不要自己拼接或猜测网址。"

_RESEARCH_RECOVERY = (
    "Continue from verified information and report gaps. "
    "Use web_search or web_extract to fill gaps only if available and appropriate for the original task."
)


def _http_url(url: str) -> bool:
    from bowhead_core.url_provenance import is_unambiguous_http_url

    return is_unambiguous_http_url(url)


def _same_site_urls(urls: list[str], start_url: str) -> list[str]:
    """只留与起始网址同站（同 host 或其子域）的链接，用于登记「已见」。"""
    from bowhead_core.url_provenance import host_of

    start = host_of(start_url)
    base = start[4:] if start.startswith("www.") else start
    if not base:
        return []
    return [u for u in urls if (h := host_of(u)) == start or h == base or h.endswith("." + base)]


def _split_by_provenance(ctx: ToolExecutionContext, urls: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    from bowhead_core.url_provenance import host_allowed, provenance_gate_enabled

    allowed: list[str] = []
    rejected: list[dict[str, str]] = []
    gate_on = provenance_gate_enabled()
    for url in urls:
        if not _http_url(url):
            rejected.append({"url": url, "reason": "not_http_url"})
        elif gate_on and host_allowed(ctx, url) is False:
            rejected.append({"url": url, "reason": "host_not_in_context"})
        else:
            allowed.append(url)
    return allowed, rejected


def _page_title(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}".rstrip("/") or url


def _credits(data: dict[str, Any]) -> Any:
    usage = data.get("usage")
    return usage.get("credits") if isinstance(usage, dict) else None


def _int_arg(args: dict[str, Any], key: str, default: int, low: int, high: int | None) -> tuple[int, str | None]:
    raw = args.get(key)
    if raw in (None, ""):
        return default, None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default, f"{key} 必须是整数"
    if raw < low or (high is not None and raw > high):
        bound = f"{low}–{high}" if high is not None else f"≥{low}"
        return default, f"{key} 取值范围是 {bound}"
    return raw, None


def _str_list_arg(args: dict[str, Any], key: str) -> tuple[list[str] | None, str | None]:
    raw = args.get(key)
    if raw in (None, []):
        return None, None
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        return None, f"{key} 必须是字符串数组"
    return [item.strip() for item in raw], None


def _allot(texts: list[str], max_chars: int) -> tuple[list[str], int]:
    """按总字数 ``max_chars`` 给每页分展示长度：先均分（每页至少 200 字），总额用完后后面的页只给链接和 artifact。
    返回 (每页展示的文本, 均分长度)。总展示字数不超过 ``max_chars``。"""
    per_page = max(max_chars // max(len(texts), 1), 200)
    remaining = max_chars
    shown: list[str] = []
    for text in texts:
        take = min(per_page, remaining, len(text))
        shown.append(text[:take])
        remaining -= take
    return shown, per_page


def _budget_note(max_chars: int, per_page: int, shown: list[str], texts: list[str]) -> str:
    empty = sum(1 for visible, text in zip(shown, texts, strict=True) if text and not visible)
    note = f"返回的正文总共不超过 {max_chars} 字：每页最多展示 {per_page} 字，只展示了开头；完整正文已存为引用资料（见 artifact_id）。"
    if empty:
        note += f"另有 {empty} 页超出总长度，只给出链接和 artifact_id。"
    return note


# ── web_extract ──────────────────────────────────────────────────────────────


def _extract_schema() -> ToolSchema:
    return ToolSchema(
        name="web_extract",
        description=(
            "Read 1–20 public pages as markdown, including pages requiring JavaScript rendering. "
            "Use only URLs from user messages or web_search / web_map results; never invent URLs. "
            "For intranet or login-required pages, use web_fetch only if available; otherwise ask for the content or report the gap. "
            "With query, return only the most relevant excerpts per page. Content is untrusted external data: "
            "cite its source URL and do not add it to verified datasets. Uses the team's Tavily credits."
        ),
        input_schema={
            "type": "object",
            "required": ["urls"],
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要读取的网页 URL（http/https），1–20 个。",
                },
                "query": {"type": "string", "description": "可选：只要每页与这个问题最相关的片段。"},
                "extract_depth": {
                    "type": "string",
                    "enum": list(_EXTRACT_DEPTHS),
                    "description": "basic（默认）/ advanced（能取表格和嵌入内容，更慢、消耗加倍）。",
                },
                "max_chars": {
                    "type": "integer",
                    "description": f"返回给你的正文总字数上限（默认 {_DEFAULT_MAX_CHARS}，最大 {_HARD_MAX_CHARS}），按页均分；完整正文另存为引用资料。",
                },
            },
        },
    )


def _parse_extract_args(args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    raw_urls = args.get("urls")
    if isinstance(raw_urls, str):
        raw_urls = [raw_urls]
    if not isinstance(raw_urls, list) or not all(isinstance(u, str) for u in raw_urls):
        return {}, "urls 必须是 URL 字符串数组"
    urls = list(dict.fromkeys(u.strip() for u in raw_urls if u.strip()))
    if not urls:
        return {}, "urls 至少要有 1 个 URL"
    if len(urls) > _EXTRACT_MAX_URLS:
        return {}, f"urls 一次最多 {_EXTRACT_MAX_URLS} 个，请分批读取"
    depth = args.get("extract_depth") or "basic"
    if depth not in _EXTRACT_DEPTHS:
        return {}, "extract_depth 只能是 basic / advanced"
    max_chars, invalid = _int_arg(args, "max_chars", _DEFAULT_MAX_CHARS, 500, _HARD_MAX_CHARS)
    if invalid:
        return {}, invalid
    query = args.get("query")
    if query is not None and not isinstance(query, str):
        return {}, "query 必须是字符串"
    return {"urls": urls, "extract_depth": depth, "max_chars": max_chars, "query": (query or "").strip()}, None


async def _run_extract(ctx: ToolExecutionContext, cred: TavilyCredential, args: dict[str, Any]) -> dict[str, Any]:
    parsed, invalid = _parse_extract_args(args)
    if invalid:
        return {"error": f"参数不合法：{invalid}", "error_kind": "invalid_arguments"}
    allowed, rejected = _split_by_provenance(ctx, parsed["urls"])
    if not allowed:
        return {"error": f"这些网址都不能读取。{_SOURCE_HINT}", "error_kind": "urls_not_in_context", "rejected_urls": rejected}

    body: dict[str, Any] = {"urls": allowed, "extract_depth": parsed["extract_depth"], "format": "markdown"}
    if parsed["query"]:
        body["query"] = parsed["query"]
    data, error = await tavily_post(
        cred.api_key,
        "/extract",
        body,
        timeout=_EXTRACT_HTTP_TIMEOUT[parsed["extract_depth"]],
        turn_id=getattr(ctx, "turn_id", None) or None,
    )
    if error is not None:
        return error.to_tool_result() | {"rejected_urls": rejected}
    assert data is not None

    from bowhead_core.web_search import _strip_ctrl, persist_web_clip

    failed = [
        {"url": _strip_ctrl(str(row.get("url") or ""))[:500], "error": _strip_ctrl(str(row.get("error") or ""))[:200]}
        for row in data.get("failed_results") or []
        if isinstance(row, dict)
    ]
    rows = [row for row in data.get("results") or [] if isinstance(row, dict) and _http_url(str(row.get("url") or ""))]
    if not rows:
        return {
            "error": (
                "No page content could be extracted. For intranet or login-required pages, use web_fetch only if available; "
                "otherwise ask for the content or report the gap."
            ),
            "error_kind": "web_extract_all_failed",
            "failed": failed,
            "rejected_urls": rejected,
        }

    texts = [_strip_ctrl(str(row.get("raw_content") or "")) for row in rows]
    shown, per_page = _allot(texts, parsed["max_chars"])
    pages: list[dict[str, Any]] = []
    for row, text, visible in zip(rows, texts, shown, strict=True):
        url = str(row["url"])
        artifact_id = await persist_web_clip(ctx, title=_page_title(url), url=url, text=text, producer="web_extract")
        truncated = len(visible) < len(text)
        pages.append(
            {
                "url": url,
                "content": visible.rstrip() + ("\n\n…[truncated]" if truncated else ""),
                "truncated": truncated,
                "artifact_id": artifact_id,
            }
        )
    result: dict[str, Any] = {"pages": pages, "failed": failed, "rejected_urls": rejected, "credits": _credits(data)}
    if any(page["truncated"] for page in pages):
        result["note"] = _budget_note(parsed["max_chars"], per_page, shown, texts)
    if rejected:
        result["rejected_note"] = f"有 {len(rejected)} 个网址未读取。{_SOURCE_HINT}"
    return result


# ── web_map ──────────────────────────────────────────────────────────────────


def _map_schema() -> ToolSchema:
    return ToolSchema(
        name="web_map",
        description=(
            "List links within a public website without reading page content. Inspect the structure, then use web_extract "
            "for selected pages only if available. The starting URL must come from user messages or web_search / web_map results. "
            "Defaults to the same site, depth 1, and at most 50 pages. Uses the team's Tavily credits "
            "(about 1 per 10 pages, doubled when instructions are provided)."
        ),
        input_schema={"type": "object", "required": ["url"], "properties": _site_properties(_MAP_DEFAULT_LIMIT, _MAP_MAX_LIMIT)},
    )


def _site_properties(default_limit: int, max_limit: int) -> dict[str, Any]:
    return {
        "url": {"type": "string", "description": "站点起始网址（http/https）。"},
        "instructions": {"type": "string", "description": "可选：用自然语言说明要找哪类页面，例如「产品价格相关页面」。"},
        "max_depth": {"type": "integer", "description": "从起始页往下走几层链接，1–5，默认 1。"},
        "max_breadth": {"type": "integer", "description": "每页最多跟多少个链接，1–500，默认 20。"},
        "limit": {"type": "integer", "description": f"总共最多处理多少个页面，默认 {default_limit}，最大 {max_limit}。"},
        "select_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": '只要路径匹配这些正则的页面，例如 ["/docs/.*"]。',
        },
        "exclude_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "排除路径匹配这些正则的页面。",
        },
        "allow_external": {
            "type": "boolean",
            "description": "是否包含其他域名的链接，默认 false（只看本站）。外站链接不会被当作已出现过的来源，之后不能直接读取。",
        },
    }


def _parse_site_args(args: dict[str, Any], *, default_limit: int, max_limit: int) -> tuple[dict[str, Any], str | None]:
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return {}, "url 必填"
    body: dict[str, Any] = {"url": url.strip()}
    for key, default, low, high in (("max_depth", 1, 1, 5), ("max_breadth", 20, 1, 500), ("limit", default_limit, 1, max_limit)):
        value, invalid = _int_arg(args, key, default, low, high)
        if invalid:
            return {}, invalid
        body[key] = value
    for key in ("select_paths", "exclude_paths"):
        value, invalid = _str_list_arg(args, key)
        if invalid:
            return {}, invalid
        if value:
            body[key] = value
    instructions = args.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        return {}, "instructions 必须是字符串"
    if instructions and instructions.strip():
        body["instructions"] = instructions.strip()
    allow_external = args.get("allow_external", False)
    if not isinstance(allow_external, bool):
        return {}, "allow_external 必须是 true / false"
    body["allow_external"] = allow_external
    return body, None


async def _run_map(ctx: ToolExecutionContext, cred: TavilyCredential, args: dict[str, Any]) -> dict[str, Any]:
    body, invalid = _parse_site_args(args, default_limit=_MAP_DEFAULT_LIMIT, max_limit=_MAP_MAX_LIMIT)
    if invalid:
        return {"error": f"参数不合法：{invalid}", "error_kind": "invalid_arguments"}
    allowed, rejected = _split_by_provenance(ctx, [body["url"]])
    if not allowed:
        return {"error": f"这个起始网址不能使用。{_SOURCE_HINT}", "error_kind": "urls_not_in_context", "rejected_urls": rejected}
    data, error = await tavily_post(cred.api_key, "/map", body, timeout=_MAP_HTTP_TIMEOUT, turn_id=getattr(ctx, "turn_id", None) or None)
    if error is not None:
        return error.to_tool_result()
    assert data is not None

    from bowhead_core.url_provenance import register_seen_hosts
    from bowhead_core.web_search import _strip_ctrl

    urls = [_strip_ctrl(u) for u in data.get("results") or [] if isinstance(u, str) and _http_url(u)]
    register_seen_hosts(ctx, _same_site_urls(urls, body["url"]))
    result: dict[str, Any] = {
        "base_url": _strip_ctrl(str(data.get("base_url") or body["url"]))[:500],
        "count": len(urls),
        "urls": urls[:_MAP_RETURN_URLS],
        "credits": _credits(data),
    }
    if len(urls) > _MAP_RETURN_URLS:
        result["note"] = f"共找到 {len(urls)} 个页面，这里只列出前 {_MAP_RETURN_URLS} 个；需要其余部分请用 select_paths 缩小范围再查。"
    elif not urls:
        result["note"] = "没有找到页面。可以加大 max_depth，或去掉 select_paths / exclude_paths 再试。"
    return result


# ── web_crawl ────────────────────────────────────────────────────────────────


def _crawl_schema() -> ToolSchema:
    properties = _site_properties(_CRAWL_DEFAULT_LIMIT, _CRAWL_MAX_LIMIT) | {
        "extract_depth": {
            "type": "string",
            "enum": list(_EXTRACT_DEPTHS),
            "description": "basic（默认）/ advanced（能取表格和嵌入内容，更慢、消耗加倍）。",
        },
        "max_chars": {
            "type": "integer",
            "description": f"返回给你的正文总字数上限（默认 {_DEFAULT_MAX_CHARS}，最大 {_HARD_MAX_CHARS}），按页均分；每页完整正文另存为引用资料。",
        },
    }
    return ToolSchema(
        name="web_crawl",
        description=(
            "Follow links from a public website's starting page and read multiple pages (up to about 2.5 minutes per call). "
            "Useful for reading a site's documentation or product pages. For a few known pages, prefer web_extract only if available. "
            "The starting URL must come from user messages or web_search / web_map results. "
            f"Defaults to the same site, depth 1, and at most {_CRAWL_DEFAULT_LIMIT} pages; narrow with select_paths / instructions. "
            "Full page content is saved as reference material; the response shows excerpts. Uses the team's Tavily credits "
            "(about 3 per 10 pages, or 5 with advanced extraction)."
        ),
        input_schema={"type": "object", "required": ["url"], "properties": properties},
    )


async def _run_crawl(ctx: ToolExecutionContext, cred: TavilyCredential, args: dict[str, Any]) -> dict[str, Any]:
    body, invalid = _parse_site_args(args, default_limit=_CRAWL_DEFAULT_LIMIT, max_limit=_CRAWL_MAX_LIMIT)
    if not invalid:
        depth = args.get("extract_depth") or "basic"
        if depth not in _EXTRACT_DEPTHS:
            invalid = "extract_depth 只能是 basic / advanced"
        body["extract_depth"] = depth
    max_chars = _DEFAULT_MAX_CHARS
    if not invalid:
        max_chars, invalid = _int_arg(args, "max_chars", _DEFAULT_MAX_CHARS, 500, _HARD_MAX_CHARS)
    if invalid:
        return {"error": f"参数不合法：{invalid}", "error_kind": "invalid_arguments"}
    allowed, rejected = _split_by_provenance(ctx, [body["url"]])
    if not allowed:
        return {"error": f"这个起始网址不能使用。{_SOURCE_HINT}", "error_kind": "urls_not_in_context", "rejected_urls": rejected}
    body |= {"format": "markdown", "timeout": 150}
    data, error = await tavily_post(cred.api_key, "/crawl", body, timeout=_CRAWL_HTTP_TIMEOUT, turn_id=getattr(ctx, "turn_id", None) or None)
    if error is not None:
        return error.to_tool_result()
    assert data is not None

    from bowhead_core.url_provenance import register_seen_hosts
    from bowhead_core.web_search import _strip_ctrl, persist_web_clip

    rows = [row for row in data.get("results") or [] if isinstance(row, dict) and _http_url(str(row.get("url") or ""))]
    if not rows:
        return {
            "error": (
                "No pages were crawled. Try increasing max_depth or removing select_paths / exclude_paths. "
                "Use web_map to inspect the site structure only if available; otherwise report the gap."
            ),
            "error_kind": "web_crawl_empty",
        }
    texts = [_strip_ctrl(str(row.get("raw_content") or "")) for row in rows]
    shown, per_page = _allot(texts, max_chars)
    pages: list[dict[str, Any]] = []
    for row, text, visible in zip(rows, texts, shown, strict=True):
        url = str(row["url"])
        artifact_id = await persist_web_clip(ctx, title=_page_title(url), url=url, text=text, producer="web_crawl")
        excerpt = visible.rstrip() + ("\n\n…[truncated]" if len(visible) < len(text) else "")
        pages.append({"url": url, "excerpt": excerpt, "chars": len(text), "artifact_id": artifact_id})
    register_seen_hosts(ctx, _same_site_urls([page["url"] for page in pages], body["url"]))
    empty = sum(1 for visible, text in zip(shown, texts, strict=True) if text and not visible)
    note = f"共抓到 {len(pages)} 页，每页正文都已存为引用资料（见 artifact_id）；返回的正文总共不超过 {max_chars} 字，每页最多展示 {per_page} 字，chars 是该页全文长度。"
    if empty:
        note += f"其中 {empty} 页超出总长度，只给出链接和 artifact_id。"
    return {
        "base_url": _strip_ctrl(str(data.get("base_url") or body["url"]))[:500],
        "count": len(pages),
        "pages": pages,
        "credits": _credits(data),
        # No silent truncation: disclose page count, displayed length per page, and full-text location.
        "note": note,
    }


# ── web_deep_research ────────────────────────────────────────────────────────

_RESEARCH_MODELS = ("mini", "pro")
_RESEARCH_LENGTHS = ("short", "standard", "long")
_RESEARCH_MAX_DOMAINS = 20
# Wait budget: peer wall-clock defaults to 900s, checked only at iteration start; 480s reserves room for surrounding iterations (tech-design §5.5).
_RESEARCH_BUDGET_SEC = 480.0
_RESEARCH_HTTP_TIMEOUT = 30.0
_RESEARCH_MAX_POLL_ERRORS = 3
_RESEARCH_REPORT_CHARS = 12000
# This run's report references [n]: no preceding ASCII identifier character (exclude arr[0]), no following "(" (exclude Markdown [1](url)).
_CITATION_RE = re.compile(r"(?<![A-Za-z0-9_])\[(\d{1,3})\](?!\()")
# Strip upstream body citation markers: not system-generated; retaining them would fabricate citations.
_FOREIGN_MARKER_RE = re.compile(r"\[(?:data|cite):[^\]\n]*\]")
# Brackets in code blocks/inline code are not citations.
_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)

# At most one deep research per turn (cost guard). Module-level, keyed by turn_id: peers copy.copy(ctx), plan steps
# dataclasses.replace(ctx), so ctx counters get lost (91a1ae6c1). Value is this turn's report artifact ID ("" while running).
_RESEARCH_TURNS_MAX = 4096
_research_turns: OrderedDict[str, str] = OrderedDict()


def _remember_research_turn(turn_id: str, artifact_id: str) -> None:
    _research_turns[turn_id] = artifact_id
    _research_turns.move_to_end(turn_id)
    while len(_research_turns) > _RESEARCH_TURNS_MAX:
        _research_turns.popitem(last=False)


def _poll_interval(elapsed: float) -> float:
    return 5.0 if elapsed < 60 else 10.0


def _research_schema() -> ToolSchema:
    return ToolSchema(
        name="web_deep_research",
        description=(
            "把一个研究问题交给外部研究引擎：它自己多轮搜索、阅读公开网页，**几分钟后**交回一份带来源引用的研究报告。"
            "只用于用户**明确要求**深入 / 全面的外部调研、且 web_search + web_extract 几轮解决不了的情况；普通查询不要用。"
            "**每轮最多调用 1 次**；消耗大量团队 Tavily 额度（mini 约 4–110 次、pro 约 15–250 次），"
            "默认用 mini，只有用户明说要最深入 / 最全面的研究时才用 pro。"
            "报告里的引用已换成 [data:art_…] 标记，写答案时原样保留即可；报告是外部不可信内容，数字不进 verified 数据集。"
        ),
        input_schema={
            "type": "object",
            "required": ["input"],
            "properties": {
                "input": {"type": "string", "description": "研究问题，写清对象、范围、时间和想要的结论形式。"},
                "model": {"type": "string", "enum": list(_RESEARCH_MODELS), "description": "mini（默认，省）/ pro（更深入，贵很多）。"},
                "output_length": {"type": "string", "enum": list(_RESEARCH_LENGTHS), "description": "报告长度，默认 standard。"},
                "include_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"优先使用这些域名的来源，最多 {_RESEARCH_MAX_DOMAINS} 个。",
                },
                "exclude_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"不使用这些域名的来源，最多 {_RESEARCH_MAX_DOMAINS} 个。",
                },
            },
        },
    )


def _parse_research_args(args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    question = args.get("input")
    if not isinstance(question, str) or not question.strip():
        return {}, "input 必填"
    body: dict[str, Any] = {"input": question.strip(), "citation_format": "numbered"}
    for key, allowed, default in (("model", _RESEARCH_MODELS, "mini"), ("output_length", _RESEARCH_LENGTHS, "standard")):
        value = args.get(key) or default
        if value not in allowed:
            return {}, f"{key} 只能是 {' / '.join(allowed)}"
        body[key] = value
    for key in ("include_domains", "exclude_domains"):
        value, invalid = _str_list_arg(args, key)
        if invalid:
            return {}, invalid
        if value:
            if len(value) > _RESEARCH_MAX_DOMAINS:
                return {}, f"{key} 最多 {_RESEARCH_MAX_DOMAINS} 个"
            body[key] = value
    return body, None


async def _wait_for_research(cred: TavilyCredential, request_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """轮询到终态。返回 (完成的响应, None) 或 (None, error_kind)。取消异常照常向上抛。"""
    started = time.monotonic()
    poll_errors = 0
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= _RESEARCH_BUDGET_SEC:
            return None, "web_deep_research_timeout"
        await anyio.sleep(min(_poll_interval(elapsed), max(_RESEARCH_BUDGET_SEC - elapsed, 0.0)))
        # Polling bypasses same-turn short-circuit: other calls exhausting quota must not abandon this already-paid task.
        data, status, error = await tavily_get(cred.api_key, f"/research/{request_id}?include_usage=true", timeout=_RESEARCH_HTTP_TIMEOUT)
        if error is not None:
            if error.kind not in {"tavily_unavailable", "tavily_rate_limited"} or status == 404:
                return None, error.kind if status != 404 else "web_deep_research_failed"
            poll_errors += 1
            if poll_errors >= _RESEARCH_MAX_POLL_ERRORS:
                return None, "tavily_unavailable"
            continue
        poll_errors = 0
        state = str((data or {}).get("status") or "")
        if status == 202 or state in {"pending", "in_progress"}:
            continue
        if state == "completed":
            return data, None
        return None, "web_deep_research_failed"


def _rewrite_citations(text: str, source_ids: list[str | None]) -> tuple[str, int]:
    """把本次运行自己的 [n] 编号翻成 [data:art_…]；翻不了的去掉并计数。

    必须用本次运行的来源表翻：原样交出去会与本轮其他引用串号，写成字面 [cite:N] 会被当孤儿剥掉，
    留裸 [n] 画布又会当成自己的编号。"""
    dropped = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal dropped
        index = int(match.group(1)) - 1
        artifact_id = source_ids[index] if 0 <= index < len(source_ids) else None
        if artifact_id and artifact_id.startswith("art_"):
            return f"[data:{artifact_id}]"
        dropped += 1
        return ""

    out: list[str] = []
    cursor = 0
    for code in _CODE_SPAN_RE.finditer(text):
        out.append(_CITATION_RE.sub(_sub, _FOREIGN_MARKER_RE.sub("", text[cursor : code.start()])))
        out.append(code.group(0))
        cursor = code.end()
    out.append(_CITATION_RE.sub(_sub, _FOREIGN_MARKER_RE.sub("", text[cursor:])))
    return "".join(out), dropped


def _cut_report(report: str, limit: int) -> str:
    """截到 ``limit`` 字，但不把 [data:art_…] 标记切成半截。"""
    if len(report) <= limit:
        return report
    head = report[:limit]
    tail_start = head.rfind("[")
    if tail_start != -1 and "]" not in head[tail_start:]:
        tail = head[tail_start:]
        # If ending with [data:art_… prefix (including just "[" / "[da"), remove it entirely, leaving no partial marker.
        if "[data:".startswith(tail) or tail.startswith("[data:"):
            head = head[:tail_start]
    return head


async def _run_research(ctx: ToolExecutionContext, cred: TavilyCredential, args: dict[str, Any]) -> dict[str, Any]:
    body, invalid = _parse_research_args(args)
    if invalid:
        return {"error": f"参数不合法：{invalid}", "error_kind": "invalid_arguments"}
    from bowhead_core.url_provenance import extract_hosts, provenance_gate_enabled, unseen_hosts

    if provenance_gate_enabled():
        # Research visits URLs mentioned in questions: gate question text and include_domains equally to prevent exfiltration via data-bearing URLs.
        mentioned = [*sorted(extract_hosts(body["input"])), *body.get("include_domains", [])]
        blocked = unseen_hosts(ctx, mentioned)
        if blocked:
            return {
                "error": f"研究问题或 include_domains 里有本轮没出现过的网址/域名：{', '.join(blocked[:10])}。去掉它们再调用。{_SOURCE_HINT}",
                "error_kind": "urls_not_in_context",
            }
    turn_id = getattr(ctx, "turn_id", None) or None
    if not turn_id:
        # Without turn identity, "at most 1 per turn" cannot be enforced: decline to run.
        return {
            "error": "Deep research requires a conversation turn identity, which is missing. " + _RESEARCH_RECOVERY,
            "error_kind": "web_deep_research_unavailable",
        }
    if turn_id in _research_turns:
        previous = _research_turns[turn_id]
        where = f"report artifact {previous}" if previous else "the previous request is still running or failed"
        return {
            "error": f"Deep research is limited to once per turn ({where}). " + _RESEARCH_RECOVERY,
            "error_kind": "web_deep_research_turn_limit",
        }
    _remember_research_turn(turn_id, "")

    created, error = await tavily_post(cred.api_key, "/research", body, timeout=_RESEARCH_HTTP_TIMEOUT, turn_id=turn_id)
    if error is not None:
        _research_turns.pop(turn_id, None)  # No task or cost incurred: refund this turn's slot.
        return error.to_tool_result()
    assert created is not None
    request_id = str(created.get("request_id") or "")
    if not request_id:
        return {"error": "The research task was not created. " + _RESEARCH_RECOVERY, "error_kind": "web_deep_research_failed"}
    logger.info("web_deep_research.created request_id=%s model=%s team=%s", request_id, body["model"], getattr(ctx, "team_id", ""))
    started = time.monotonic()
    try:
        done, failure = await _wait_for_research(cred, request_id)
    except anyio.get_cancelled_exc_class():
        # Tavily has no cancellation API; started tasks continue and incur costs, so record truthfully.
        logger.info("web_deep_research.cancelled request_id=%s elapsed_s=%d", request_id, int(time.monotonic() - started))
        raise
    elapsed_s = int(time.monotonic() - started)
    if failure == "web_deep_research_timeout":
        return {
            "error": f"The research task did not finish within {elapsed_s} seconds; this turn will stop waiting. " + _RESEARCH_RECOVERY,
            "error_kind": failure,
            "request_id": request_id,
        }
    if failure == "web_deep_research_failed":
        return {"error": "The research task failed. " + _RESEARCH_RECOVERY, "error_kind": failure, "request_id": request_id}
    if failure is not None:
        from bowhead_core.tavily_client import TavilyError

        return TavilyError(failure, None).to_tool_result() | {"request_id": request_id}
    assert done is not None

    from bowhead_core.url_provenance import register_seen_hosts
    from bowhead_core.web_search import _strip_ctrl, persist_web_clip

    sources: list[dict[str, Any]] = []
    for row in done.get("sources") or []:
        if not isinstance(row, dict) or not _http_url(str(row.get("url") or "")):
            sources.append({"title": "", "url": "", "artifact_id": None})  # Placeholder keeps numbering aligned.
            continue
        url = str(row["url"])
        title = _strip_ctrl(str(row.get("title") or ""))[:200] or _page_title(url)
        artifact_id = await persist_web_clip(ctx, title=title, url=url, text="", producer="web_deep_research")
        sources.append({"title": title, "url": url, "artifact_id": artifact_id})
    register_seen_hosts(ctx, [src["url"] for src in sources if src["url"]])

    content = done.get("content")
    if content in (None, "", {}, []):
        return {
            "error": "The research task finished without a report. " + _RESEARCH_RECOVERY,
            "error_kind": "web_deep_research_failed",
            "request_id": request_id,
        }
    raw_report = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    report, dropped = _rewrite_citations(_strip_ctrl(raw_report), [src["artifact_id"] for src in sources])
    if dropped:
        logger.info("web_deep_research.citations_dropped request_id=%s dropped=%d", request_id, dropped)
    report_id = await persist_web_clip(ctx, title=f"深度研究：{body['input'][:60]}", url="", text=report, producer="web_deep_research")
    _remember_research_turn(turn_id, report_id or "")
    result: dict[str, Any] = {
        "report": _cut_report(report, _RESEARCH_REPORT_CHARS),
        "report_artifact_id": report_id,
        "sources": [src for src in sources if src["url"]],
        "model": body["model"],
        "credits": _credits(done),
        "request_id": request_id,
        "elapsed_s": elapsed_s,
    }
    if len(report) > _RESEARCH_REPORT_CHARS:
        result["note"] = f"报告共 {len(report)} 字，这里只展示前 {_RESEARCH_REPORT_CHARS} 字；完整报告已存为引用资料（report_artifact_id）。"
    return result


# ── registration ─────────────────────────────────────────────────────────────

_RUNNERS: dict[str, Callable[[ToolExecutionContext, TavilyCredential, dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "web_extract": _run_extract,
    "web_map": _run_map,
    "web_crawl": _run_crawl,
    "web_deep_research": _run_research,
}
_SCHEMAS: dict[str, Callable[[], ToolSchema]] = {
    "web_extract": _extract_schema,
    "web_map": _map_schema,
    "web_crawl": _crawl_schema,
    "web_deep_research": _research_schema,
}


def _make_handler(tool_name: str, resolver: TeamTavilyResolver, store_getter: Callable[[], Any | None] | None):
    runner = _RUNNERS[tool_name]

    async def _handler(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        team_id = getattr(ctx, "team_id", "") or ""
        cred: TavilyCredential | None = None
        if team_id:
            try:
                cred = await resolver(team_id)
            except Exception:
                logger.warning("%s.tavily_resolve_failed team=%s", tool_name, team_id, exc_info=True)
                return dict(_UNAVAILABLE)
        if cred is None:
            return dict(_NOT_CONFIGURED)
        started = time.perf_counter()
        result: dict[str, Any] = {"error": "internal", "error_kind": "internal_error"}
        try:
            result = await runner(ctx, cred, args)
            return result
        except anyio.get_cancelled_exc_class():
            result = {"error": "cancelled", "error_kind": "cancelled"}
            raise
        finally:
            # Persist audit even on cancellation: deep research already cost money. Shield cancellation + short timeout so audit cannot delay cleanup.
            with anyio.CancelScope(shield=True), anyio.move_on_after(5):
                await _audit(store_getter() if store_getter else None, tool_name, ctx, cred, args, result, started)

    return _handler


async def _audit(
    store: Any | None,
    tool_name: str,
    ctx: ToolExecutionContext,
    cred: TavilyCredential,
    args: dict[str, Any],
    result: dict[str, Any],
    started: float,
) -> None:
    if store is None:
        return
    digest = hashlib.sha256(json.dumps(args, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    try:
        await store.log_tool_call(
            team_id=ctx.team_id,
            user_id=getattr(ctx, "user_id", None) or None,
            source_id=cred.source_id,
            tool_name=tool_name,
            args_digest=digest,
            status="error" if "error" in result else "ok",
            error_kind=result.get("error_kind"),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except Exception as exc:
        logger.warning("%s.audit_failed team=%s err=%s", tool_name, ctx.team_id, type(exc).__name__)


def register_web_tavily_tools(
    registry: ToolRegistry,
    resolver: TeamTavilyResolver | None,
    *,
    store_getter: Callable[[], Any | None] | None = None,
) -> bool:
    """注册 Tavily 网页工具；开关关闭或没有团队解析器时不注册（与 web_search 一样不接受部署级密钥）。

    ``store_getter`` 在调用时取审计用的 UserStore——gateway 注册工具时 store 尚未建好。"""
    from bowhead_core.tavily_tools_flags import tavily_tools_enabled

    if resolver is None or not tavily_tools_enabled():
        logger.info("web_tavily_tools: not registered (flag=%s resolver=%s)", tavily_tools_enabled(), resolver is not None)
        return False
    for tool_name in TAVILY_TOOL_NAMES:
        registry.register(
            ToolSpec(
                schema=_SCHEMAS[tool_name](),
                handler=_make_handler(tool_name, resolver, store_getter),
                effect_spec=TOOL_EFFECT_READ_ONLY,
            )
        )
    logger.info("web_tavily_tools registered tools=%s", ",".join(TAVILY_TOOL_NAMES))
    return True
