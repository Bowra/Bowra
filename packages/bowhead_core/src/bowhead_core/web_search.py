"""`web_search` tool — 外部互联网搜索（#86，团队化整体镜像 image_gen）.

web_search 由团队在「模型」设置里绑定一个 provider + 模型（``search:`` 段）才可用：
- 注册 key-gated：无团队解析器 / 团队无 active search 模型 / 缺 key → 不注册（handler
  返「未配置」），Lead/peer 优雅降级看不到该工具。绝不通过环境变量/部署级密钥绕过。
- handler 在调用时按 ``ctx.team_id`` 解析 SearchModelRegistry，取 base_url/real_id/key。
- ``protocol`` 决定端点：``dashscope_compat``（OpenAI-兼容，默认）/ ``dashscope_native``
  （原生 generation，结构化 search_results）——此前的 ``BOWHEAD_WEB_SEARCH_NATIVE`` 环境
  开关已删。另有 ``tavily``：团队在「工具」里启用 Tavily 时优先使用，经 ``tavily_client`` 出站，
  支持 topic / 时间 / 域名 / 深度过滤；其他协议收到这些过滤参数时在返回里标 ``ignored_params``。

返 shape:
{
  "query": "...",
  "summary": "一段 AI 聚合的摘要（含内联 citation 编号）",
  "results": [{"title": "...", "url": "...", "snippet": "..."}, ...]
}

设计要点：
- 外部数字 **不** 加入 authorized set（number_verifier 只信 tool 返值、不信 web）.
- 每次调用成本和延迟不可忽视（LLM 跑一圈），Lead prompt 要明确**仅用于**外部实时数据.
- 失败静默降级返 `{"error": "<reason>", ...}`，不 raise（Lead 可回退）.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from bowhead_router.types import ToolSchema

from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from bowhead_core.search_config import SearchModelRegistry, SearchModelSpec

# Resolve team search registry asynchronously by team_id; absent config disables that team's web_search.
TeamSearchResolver = Callable[[str], Awaitable["SearchModelRegistry | None"]]

logger = logging.getLogger(__name__)

# DashScope web search has to retrieve and synthesize external sources before
# returning a completion. Real 116 dogfood measured healthy requests near the
# old 25-second ceiling, which made both the runtime tool and its automatic retry
# fail even though the same key and request succeeded upstream. Keep an explicit
# floor aligned with provider verification while still honoring longer
# per-model configuration.
_MIN_SEARCH_TIMEOUT_SEC = 45.0


def _search_timeout_sec(spec: SearchModelSpec) -> float:
    return max(float(spec.timeout_sec), _MIN_SEARCH_TIMEOUT_SEC)


# ── M1 (RD §7) web_clip sanitization ─────────────────────────────────────────
# web_search results are third-party (DashScope) strings → sanitize before they
# are persisted as a citeable, re-injectable asset.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _strip_ctrl(s: str) -> str:
    return _CTRL_RE.sub("", s or "")


def _scheme_ok(url: str) -> bool:
    """Allow only http/https — drop javascript:/data:/etc. at persist time so a
    malicious result URL can never become a live link (XSS)."""
    return (url or "").strip().lower().startswith(("http://", "https://"))


def _slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^\w一-鿿-]+", "_", _strip_ctrl(s).strip()).strip("_")
    return (s or "web")[:n]


def _render_clip_md(title: str, url: str, snippet: str) -> bytes:
    parts = [f"# {title}".strip() or "# web result"]
    if url:
        parts.append(f"\n<{url}>")
    if snippet:
        parts.append(f"\n\n{snippet}")
    return "\n".join(parts).encode("utf-8")


def _results_from_summary_urls(summary: str, limit: int) -> list[dict[str, Any]]:
    """D3a stopgap: recover FULL URLs DashScope inlined into the summary prose when it returns
    no structured citation list (the OpenAI-compat path). Lets the answer surface real
    clickable sources instead of fabricating links from bare [N] markers — only URLs literally
    present in the text are surfaced, never invented."""
    from bowhead_core.url_provenance import _URL_RE, host_of

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for m in _URL_RE.finditer(summary or ""):
        u = _strip_ctrl(m.group(0)).rstrip(".,;)]）】")
        if not _scheme_ok(u) or u in seen:
            continue
        seen.add(u)
        out.append({"title": host_of(u) or u[:60], "url": u, "snippet": ""})
        if len(out) >= limit:
            break
    return out


def _schema() -> ToolSchema:
    return ToolSchema(
        name="web_search",
        description=(
            "在互联网搜索**外部 / 实时**信息（行业均值、竞品、新闻、最新政策、"
            "workspace 数据外的任何东西）. 返 summary + 多条 {title, url, snippet} 结果. "
            "**禁止**用于 workspace 内数据 —— 那些走 bi.*. "
            "**引用规则**：final_answer 提到 web_search 返的数字/结论必须 inline 写 "
            "来源 URL（形如 `(来源: https://...)`），不加入 verified 数据集. "
            "**何时用**：用户明确说'上网查 / 最新 / 行业 / 外部'，或 bi.* 覆盖不到的范畴."
        ),
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "自然语言搜索词；越具体越好（含时间 / 实体 / 关键指标）.",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回结果条数（默认 5，最大 10）.",
                },
                "topic": {
                    "type": "string",
                    "enum": list(_TOPICS),
                    "description": "搜索类别：general（默认）/ news（新闻，带发布日期）/ finance.",
                },
                "time_range": {
                    "type": "string",
                    "enum": list(_TIME_RANGES),
                    "description": "只要最近一段时间内的结果；与 start_date/end_date 二选一.",
                },
                "start_date": {"type": "string", "description": "结果发布日期下限，YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "结果发布日期上限，YYYY-MM-DD."},
                "include_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": '只在这些域名里搜（如 ["gov.cn"]）.',
                },
                "exclude_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "排除这些域名.",
                },
                "search_depth": {
                    "type": "string",
                    "enum": list(_DEPTHS),
                    "description": "basic（默认）/ advanced（更相关、更慢、消耗加倍）.",
                },
                "country": {
                    "type": "string",
                    "description": "优先该国结果，英文小写国名（如 china、united states）；仅 topic=general 生效.",
                },
            },
        },
    )


_TOPICS = ("general", "news", "finance")
_TIME_RANGES = ("day", "week", "month", "year")
_DEPTHS = ("basic", "advanced")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_INCLUDE_DOMAINS = 300
_MAX_EXCLUDE_DOMAINS = 150
# Tavily content is at most 3 blocks × 500 characters per result; retain that scale, also stored identically in web_clip.
_TAVILY_SNIPPET_CHARS = 1600


def _parse_search_options(args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """把可选过滤参数校验成上游请求字段；不合法时返回带合法取值的错误说明（让模型一次改对）。"""
    options: dict[str, Any] = {}
    for key, allowed in (("topic", _TOPICS), ("time_range", _TIME_RANGES), ("search_depth", _DEPTHS)):
        value = args.get(key)
        if value in (None, ""):
            continue
        if value not in allowed:
            return {}, f"{key} 只能是 {' / '.join(allowed)}"
        options[key] = value
    for key in ("start_date", "end_date"):
        value = args.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str) or not _DATE_RE.match(value):
            return {}, f"{key} 格式必须是 YYYY-MM-DD"
        options[key] = value
    if "time_range" in options and ("start_date" in options or "end_date" in options):
        return {}, "time_range 与 start_date/end_date 只能二选一"
    for key, cap in (("include_domains", _MAX_INCLUDE_DOMAINS), ("exclude_domains", _MAX_EXCLUDE_DOMAINS)):
        value = args.get(key)
        if value in (None, []):
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            return {}, f"{key} 必须是域名字符串数组"
        if len(value) > cap:
            return {}, f"{key} 最多 {cap} 个"
        options[key] = [item.strip() for item in value]
    country = args.get("country")
    if country not in (None, ""):
        if not isinstance(country, str):
            return {}, "country 必须是英文小写国名"
        options["country"] = country.strip().lower()
    return options, None


async def _resolve_team_search_spec(
    ctx: ToolExecutionContext,
    team_search_resolver: TeamSearchResolver | None,
) -> SearchModelSpec | None:
    """按 ctx.team_id 现解析团队 active web_search 模型 spec；无团队/无配置/无 key → None.

    每次调用现解析（不在注册期缓存），团队改配置/撤密钥下一次调用即生效（镜像
    image_gen_tool._make_handler）。"""
    team_id = getattr(ctx, "team_id", "") or ""
    if team_search_resolver is None or not team_id:
        return None
    try:
        reg = await team_search_resolver(team_id)
    except Exception:
        logger.warning("web_search: team search resolver failed team=%s", team_id, exc_info=True)
        return None
    if reg is None:
        return None
    spec = reg.get_active("web_search")
    if spec is None or not spec.api_key:
        return None
    return spec


def _make_handler(team_search_resolver: TeamSearchResolver | None = None):
    """每次调用只解析团队 search registry；不使用全局或环境变量 Provider。"""

    async def _handler(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        query = str(args.get("query") or "").strip()
        # observability: there was NO log when web_search ran, which made it
        # impossible to tell (from logs) whether a "market research" turn actually
        # searched the web vs. answered from model knowledge. Now it's visible.
        logger.info("web_search.invoked query=%r", query[:100])
        if not query:
            return {"error": "query required", "results": []}
        spec = await _resolve_team_search_spec(ctx, team_search_resolver)
        if spec is None:
            return {
                "error": "web search is not configured for this team",
                "results": [],
            }
        try:
            limit = int(args.get("limit") or 5)
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(10, limit))
        options, invalid = _parse_search_options(args)
        if invalid:
            return {"error": f"参数不合法：{invalid}", "error_kind": "invalid_arguments", "results": []}
        return await _run_search(ctx, spec, query, limit, options)

    return _handler


async def _run_search(
    ctx: ToolExecutionContext,
    spec: SearchModelSpec,
    query: str,
    limit: int,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    options = options or {}
    if spec.protocol == "tavily":
        summary, results, error = await _fetch_tavily(ctx, spec, query, limit, options)
        if error is not None:
            return error
        await _persist_web_clips_and_provenance(ctx, summary, results)
        return {"query": query, "summary": summary, "results": results}

    result = await _run_search_llm_backend(ctx, spec, query, limit)
    if options and "error" not in result:
        # DashScope does not support these filters; tell the model honestly so it cannot assume filtered results.
        result["ignored_params"] = sorted(options)
        result["note"] = "当前团队的搜索后端不支持这些过滤参数，结果未按它们过滤。"
    return result


async def _run_search_llm_backend(
    ctx: ToolExecutionContext,
    spec: SearchModelSpec,
    query: str,
    limit: int,
) -> dict[str, Any]:
    try:
        import httpx2
    except ImportError:
        return {"error": "httpx2 not available", "results": []}

    # protocol determines endpoint: native generation (structured search_results), otherwise OpenAI-compatible
    # chat/completions + summary-URL fallback. Previously switched by BOWHEAD_WEB_SEARCH_NATIVE env flag.
    if spec.protocol == "dashscope_native":
        summary, results, _err = await _fetch_native(httpx2, spec, query, limit)
        if _err is not None:
            return _err
        await _persist_web_clips_and_provenance(ctx, summary, results)
        return {"query": query, "summary": summary, "results": results}

    url = f"{spec.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {spec.api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": spec.real_id,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a factual research assistant. Use web search to answer. "
                    "Return a concise summary paragraph with inline citations as [1] [2] etc. "
                    "Do NOT add disclaimers or filler."
                ),
            },
            {"role": "user", "content": query},
        ],
        "enable_search": True,
        "search_options": {"return_citations": True},
    }

    try:
        async with httpx2.AsyncClient(timeout=_search_timeout_sec(spec)) as client:
            resp = await client.post(url, headers=headers, json=body)
    except Exception as e:
        return {"error": f"http error: {type(e).__name__}: {e}", "results": []}
    if resp.status_code >= 400:
        return {"error": f"dashscope {resp.status_code}: {resp.text[:200]}", "results": []}
    try:
        data = resp.json()
    except Exception as e:
        return {"error": f"parse json failed: {e}", "results": []}

    choices = data.get("choices") or []
    if not choices:
        return {"error": "no choices returned", "results": []}
    first = choices[0] or {}
    msg = first.get("message") or {}
    summary = (msg.get("content") or "").strip()

    # DashScope citations usually live in message.citations or top-level search_results.
    raw_cits = msg.get("citations") or data.get("search_results") or data.get("citations") or []
    results: list[dict[str, Any]] = []
    for c in raw_cits[:limit]:
        if not isinstance(c, dict):
            continue
        _raw_url = str(c.get("url") or c.get("link") or "")
        results.append(
            {
                "title": _strip_ctrl(str(c.get("title") or c.get("source") or "")[:200]),
                # Drop non-http(s) schemes (javascript:/data:) at persist time.
                "url": _raw_url if _scheme_ok(_raw_url) else "",
                "snippet": _strip_ctrl(
                    str(
                        c.get("snippet") or c.get("excerpt") or c.get("summary") or "",
                    )[:400]
                ),
            }
        )

    if not results and summary:
        # DashScope qwen-plus (OpenAI-compat path) returns a summary with inline [N] markers
        # but no structured citation list (message.citations / search_results / citations),
        # so `results` is empty even on a successful search. D3a stopgap: recover any FULL
        # URLs DashScope inlined into the summary prose so the answer surfaces real clickable
        # sources instead of fabricating links from bare [N]. (The native DashScope endpoint
        # returns proper search_results — see STATUS.md D3a, dogfood-gated.)
        results = _results_from_summary_urls(summary, limit)
        logger.info(
            "web_search.no_citation_urls query=%r summary_len=%d recovered_urls=%d",
            query[:80],
            len(summary),
            len(results),
        )

    await _persist_web_clips_and_provenance(ctx, summary, results)
    return {"query": query, "summary": summary, "results": results}


async def _fetch_tavily(
    ctx: ToolExecutionContext,
    spec: SearchModelSpec,
    query: str,
    limit: int,
    options: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
    from bowhead_core.tavily_client import tavily_post

    body = {"query": query, "max_results": limit, "include_answer": True, **options}
    turn_id = getattr(ctx, "turn_id", None) or None
    data, error = await tavily_post(spec.api_key, "/search", body, timeout=_search_timeout_sec(spec), turn_id=turn_id)
    if error is not None:
        return "", [], error.to_tool_result()
    assert data is not None
    raw_results = data.get("results") if isinstance(data.get("results"), list) else []
    results = [
        {
            "title": _strip_ctrl(str(row.get("title") or "")[:200]),
            "url": str(row.get("url") or "") if _scheme_ok(str(row.get("url") or "")) else "",
            "snippet": _strip_ctrl(str(row.get("content") or "")[:_TAVILY_SNIPPET_CHARS]),
            "published_date": row.get("published_date") if isinstance(row.get("published_date"), str) else None,
        }
        for row in raw_results[:limit]
        if isinstance(row, dict)
    ]
    summary = _strip_ctrl(str(data.get("answer") or ""))
    if not summary and not results:
        return "", [], {"error": "没有搜到结果。换更宽的关键词或去掉过滤参数再试。", "results": []}
    return summary, results, None


async def persist_web_clip(ctx: ToolExecutionContext, *, title: str, url: str, text: str, producer: str) -> str | None:
    """把一段第三方网页内容存成 `web_clip`（可引用，trust='unverified'，inject='stub'），返回 artifact id.
    FAIL-SOFT：存储失败只记日志返回 None，不打断工具。web_search 与 Tavily 网页工具共用。"""
    astore = getattr(ctx, "artifact_store", None)
    if astore is None:
        return None
    try:
        art = await astore.put_bytes(
            content=_render_clip_md(title, url, text),
            filename=f"{_slug(title)}.md",
            mime="text/markdown",
            # source must be literal (statically registered in artifact_store._SOURCE_TO_KIND, pinned by test_pptx_m1_asset_model).
            source=(
                "web_extract"
                if producer == "web_extract"
                else "web_crawl"
                if producer == "web_crawl"
                else "web_deep_research"
                if producer == "web_deep_research"
                else "web_search"
            ),
            created_by=getattr(ctx, "user_id", None) or None,
            session_id=getattr(ctx, "session_id", None) or None,
            kind="web_clip",
            inject="stub",
            trust="unverified",
            producer_kind=producer,
            created_by_turn=getattr(ctx, "turn_id", None),
        )
    except Exception as exc:
        logger.warning("%s.persist_clip_failed err=%s", producer, exc)
        return None
    return art.id


async def _persist_web_clips_and_provenance(
    ctx: ToolExecutionContext,
    summary: str,
    results: list[dict[str, Any]],
) -> None:
    """Shared web_search tail (compat + native): persist each result as a `web_clip` asset
    (citeable via [cite:N], trust='unverified', inject='stub'; stamps result['artifact_id'])
    and register result + summary hosts into the per-turn seen set so a later web_fetch of
    those URLs passes the exfil gate. FAIL-SOFT: a store/provenance error never breaks search."""
    for r in results:
        artifact_id = await persist_web_clip(ctx, title=r["title"], url=r["url"], text=r["snippet"], producer="web_search")
        if artifact_id is not None:
            r["artifact_id"] = artifact_id
    try:
        from bowhead_core.url_provenance import (
            extract_hosts,
            register_seen_hosts,
        )

        register_seen_hosts(ctx, [r.get("url") for r in results])
        _seen = getattr(ctx, "_seen_url_hosts", None)
        if _seen is not None and summary:
            _seen |= extract_hosts(summary)
    except Exception as _pe:
        logger.warning("web_search.provenance_register_failed err=%s", _pe)


_DASHSCOPE_NATIVE_PATH = "/api/v1/services/aigc/text-generation/generation"


def _native_url_for(base_url: str) -> str:
    """从团队配置的 provider ``base_url`` 推出原生 generation 端点（团队 BYO endpoint 也生效）。

    compat 与 native 是同一 DashScope host 下的不同路径：compat base 常配成
    ``.../compatible-mode/v1``、native 走 ``.../api/v1/services/...``。把 base_url 归一到
    API root 再拼原生路径，团队只配一个 host 就能两种 protocol 通用。"""
    root = (base_url or "").rstrip("/")
    for suffix in ("/compatible-mode/v1", "/api/v1"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
            break
    return f"{root}{_DASHSCOPE_NATIVE_PATH}"


def _parse_native_search_response(
    data: dict[str, Any],
    limit: int,
) -> tuple[str, list[dict[str, Any]]]:
    """D3a: parse the NATIVE DashScope generation response → (summary, results).

    Native shape: ``output.text`` (or ``output.choices[0].message.content``) is the prose; the
    structured sources are ``output.search_info.search_results[]`` = {index, title, url,
    site_name, ...}. URLs are http(s)-sanitised + control-stripped (same as the compat path).
    """
    out = data.get("output") if isinstance(data.get("output"), dict) else {}
    summary = str(out.get("text") or "").strip()
    if not summary:
        ch = out.get("choices") if isinstance(out.get("choices"), list) else []
        if ch and isinstance(ch[0], dict):
            summary = str((ch[0].get("message") or {}).get("content") or "").strip()
    info = out.get("search_info") if isinstance(out.get("search_info"), dict) else {}
    raw = info.get("search_results") if isinstance(info.get("search_results"), list) else []
    results: list[dict[str, Any]] = []
    for c in raw[:limit]:
        if not isinstance(c, dict):
            continue
        u = str(c.get("url") or "")
        results.append(
            {
                "title": _strip_ctrl(str(c.get("title") or c.get("site_name") or "")[:200]),
                "url": u if _scheme_ok(u) else "",
                "snippet": _strip_ctrl(str(c.get("snippet") or "")[:400]),
            }
        )
    return summary, results


async def _fetch_native(
    httpx2_mod: Any,
    spec: SearchModelSpec,
    query: str,
    limit: int,
) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
    """D3a native path: POST the DashScope generation endpoint with
    parameters.search_options.enable_source so the response carries real search_results[].
    Returns (summary, results, error_dict_or_None)."""
    body = {
        "model": spec.real_id,
        "input": {
            "messages": [
                {
                    "role": "system",
                    "content": "You are a factual research assistant. Use web "
                    "search to answer. Return a concise summary; do NOT add disclaimers or filler.",
                },
                {"role": "user", "content": query},
            ]
        },
        "parameters": {
            "enable_search": True,
            # ★ Explicitly request message format or **no sources are returned** (observed on 116 with a real
            # DashScope key on 2026-08-08): without result_format, output contains only
            # {finish_reason, text}; **the entire `search_info` key is absent**, making `enable_source`
            # ineffective. With it, output becomes {choices, search_info}, with 10 search_results
            # (all containing title/url/site_name).
            # Previously unnoticed because team search had no configuration since 07-27, so this path
            # never ran in production ([[feedback_capability_off_path_untested]]: the disabled path
            # was untested). Downstream `_parse_native_search_response` **already** expected message
            # shape (summary falls back to output.choices[0].message.content);
            # request and parsing were misaligned from the start, but nobody reached this path.
            "result_format": "message",
            "search_options": {"enable_source": True, "enable_citation": True, "citation_format": "[ref_<number>]"},
        },
    }
    headers = {"Authorization": f"Bearer {spec.api_key}", "Content-Type": "application/json"}
    try:
        async with httpx2_mod.AsyncClient(timeout=_search_timeout_sec(spec)) as client:
            resp = await client.post(_native_url_for(spec.base_url), headers=headers, json=body)
    except Exception as e:
        return "", [], {"error": f"http error: {type(e).__name__}: {e}", "results": []}
    if resp.status_code >= 400:
        return "", [], {"error": f"dashscope-native {resp.status_code}: {resp.text[:200]}", "results": []}
    try:
        data = resp.json()
    except Exception as e:
        return "", [], {"error": f"parse json failed: {e}", "results": []}
    summary, results = _parse_native_search_response(data, limit)
    if not summary and not results:
        return "", [], {"error": "native search returned no output", "results": []}
    return summary, results, None


def register_web_search_tool(
    registry: ToolRegistry,
    team_search_resolver: TeamSearchResolver | None = None,
) -> bool:
    """注册团队级 `web_search`；无团队解析器时不得注册（镜像 register_image_gen_tool）。

    工具「可见性」= 有没有团队解析器；「可用性」= 团队是否配了 active search 模型 + key
    （后者在 handler 调用期判定，返「未配置」）。绝不作为部署级/环境变量工具注册。"""
    if team_search_resolver is None:
        logger.info("web_search: no team search resolver — tool not registered")
        return False
    registry.register(ToolSpec(schema=_schema(), handler=_make_handler(team_search_resolver)))
    logger.info("web_search registered (per_team=True)")
    return True
