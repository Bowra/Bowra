"""Tavily HTTP 出站的唯一入口（设计：docs/sprints/tavily-tools-2026-09-24/tech-design.md §4）。

web_search 的 tavily 后端、设置页的密钥验证，以及后续的 extract / map / crawl / research 工具都经这里发请求：
- 鉴权只用 ``Authorization: Bearer``（当前 OpenAPI 只声明这一种；请求体 ``api_key`` 已不在文档里）。
- 上游状态码统一分类成 ``error_kind``，并附一句「给模型的话」——必须带下一步该做什么，只说「错了」模型会反复重试。
- 401 / 432 / 433 是配置态：同一轮第一次命中后，本轮后续调用直接短路，不再打上游。轮级状态放模块级、按 turn_id
  为键——peer 派发用 ``copy.copy(ctx)``、计划步骤用 ``dataclasses.replace(ctx)``，挂在 ctx 上的计数会丢（见 91a1ae6c1）。
- 失败不回落到其他搜索源。
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import anyio
import httpx2

logger = logging.getLogger(__name__)

TAVILY_BASE_URL = "https://api.tavily.com"

# Retry 429 once only if the upstream-requested delay is at most this many seconds; longer waits return to the model.
_MAX_RETRY_AFTER_SEC = 10.0

# Configuration errors: invalid key / exhausted plan quota / pay-as-you-go cap. Retrying is pointless.
_CONFIG_STATE_KINDS = frozenset({"tavily_key_invalid", "tavily_plan_quota_exhausted", "tavily_spend_limit_reached"})

_TURN_BLOCKS_MAX = 4096
# Include the key fingerprint: an admin key change in the same turn (e.g. approval continuation) must not inherit the old short circuit.
_turn_blocks: OrderedDict[tuple[str, str], str] = OrderedDict()

# 403 means unsupported site fetching only on these endpoints; on search / research it means key or plan rejection.
_URL_PATHS = ("/extract", "/crawl", "/map")

_TELL_USER_ADMIN = "如实告诉用户：需要团队管理员在「团队设置 → 工具」处理 Tavily 配置。本轮不要再调用任何网络搜索或网页读取工具。"

_MODEL_HINTS: dict[str, str] = {
    "tavily_key_invalid": "Tavily 密钥已失效。" + _TELL_USER_ADMIN,
    "tavily_plan_quota_exhausted": "Tavily 套餐额度已用完。" + _TELL_USER_ADMIN,
    "tavily_spend_limit_reached": "Tavily 按量付费已达到上限。" + _TELL_USER_ADMIN,
    "tavily_rate_limited": "Tavily 请求过于频繁。减少本轮的并发搜索，稍后再试一次；不要连续重复同一请求。",
    "tavily_bad_request": "请求参数不被 Tavily 接受。按工具说明里的取值范围修改参数后再试。",
    "tavily_url_not_supported": (
        "Tavily cannot fetch this site. Use web_fetch for a known page only if available; otherwise ask for the page content or report the gap."
    ),
    "tavily_unavailable": "Tavily 暂时不可用。不要改用其他搜索源；如实告诉用户网络搜索暂不可用。",
}


@dataclass(frozen=True)
class TavilyError:
    kind: str
    status: int | None
    detail: str = ""

    def to_tool_result(self) -> dict[str, Any]:
        """工具返回给模型的失败形状；detail 只含上游字段名等短信息，不含第三方正文。"""
        message = _MODEL_HINTS.get(self.kind, "Tavily 调用失败。")
        if self.detail:
            message = f"{message}（{self.detail}）"
        return {"error": message, "error_kind": self.kind, "results": []}


def _block_key(turn_id: str, api_key: str) -> tuple[str, str]:
    return turn_id, hashlib.sha256(api_key.encode("utf-8", "replace")).hexdigest()[:12]


def _remember_turn_block(key: tuple[str, str], kind: str) -> None:
    _turn_blocks[key] = kind
    _turn_blocks.move_to_end(key)
    while len(_turn_blocks) > _TURN_BLOCKS_MAX:
        _turn_blocks.popitem(last=False)


def _classify(status: int, payload: Any, path: str) -> TavilyError:
    if status == 401:
        return TavilyError("tavily_key_invalid", status)
    if status == 432:
        return TavilyError("tavily_plan_quota_exhausted", status)
    if status == 433:
        return TavilyError("tavily_spend_limit_reached", status)
    if status == 429:
        return TavilyError("tavily_rate_limited", status)
    if status == 403:
        if path.startswith(_URL_PATHS):
            return TavilyError("tavily_url_not_supported", status)
        return TavilyError("tavily_key_invalid", status)
    if status in {400, 422}:
        return TavilyError("tavily_bad_request", status, _bad_request_fields(payload))
    return TavilyError("tavily_unavailable", status)


def _bad_request_fields(payload: Any) -> str:
    """只取出错字段名交给模型，不转述第三方正文。"""
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, list):
        fields = sorted(
            {str(item["loc"][-1]) for item in detail if isinstance(item, dict) and isinstance(item.get("loc"), list | tuple) and item["loc"]}
        )
        return "字段：" + "、".join(fields) if fields else ""
    return ""


def _retry_after_sec(response: Any) -> float | None:
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _json_or_none(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return None


async def tavily_post(
    api_key: str,
    path: str,
    body: dict[str, Any],
    *,
    timeout: float,
    turn_id: str | None = None,
) -> tuple[dict[str, Any] | None, TavilyError | None]:
    """POST ``TAVILY_BASE_URL + path``；返回 (响应 JSON, None) 或 (None, TavilyError)。"""
    data, _status, error = await _request("POST", api_key, path, {**body, "include_usage": True}, timeout=timeout, turn_id=turn_id)
    return data, error


async def tavily_get(
    api_key: str,
    path: str,
    *,
    timeout: float,
    turn_id: str | None = None,
) -> tuple[dict[str, Any] | None, int | None, TavilyError | None]:
    """GET（研究任务轮询用）；多返回 HTTP 状态码——202 表示任务仍在进行。"""
    return await _request("GET", api_key, path, None, timeout=timeout, turn_id=turn_id)


async def _request(
    method: str,
    api_key: str,
    path: str,
    payload: dict[str, Any] | None,
    *,
    timeout: float,
    turn_id: str | None,
) -> tuple[dict[str, Any] | None, int | None, TavilyError | None]:
    block_key = _block_key(turn_id, api_key) if turn_id else None
    if block_key and block_key in _turn_blocks:
        return None, None, TavilyError(_turn_blocks[block_key], None)
    if not api_key.isascii() or not api_key.isprintable():
        # Pasted full-width characters, for example, raise UnicodeEncodeError in headers; do not classify as connection failure.
        return None, None, TavilyError("tavily_key_invalid", None, "密钥含非法字符")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    started = time.perf_counter()
    status: int | None = None
    data: Any = None
    error: TavilyError | None = None
    response: Any = None
    try:
        async with httpx2.AsyncClient(timeout=timeout) as client:
            for attempt in range(2):
                if method == "GET":
                    response = await client.get(f"{TAVILY_BASE_URL}{path}", headers=headers)
                else:
                    response = await client.post(f"{TAVILY_BASE_URL}{path}", json=payload, headers=headers)
                status = response.status_code
                if status != 429 or attempt:
                    break
                wait = _retry_after_sec(response)
                if wait is None or wait > _MAX_RETRY_AFTER_SEC:
                    break
                await anyio.sleep(max(wait, 0.0))
    except Exception as exc:
        error = TavilyError("tavily_unavailable", None, type(exc).__name__)
    if error is None:
        # Classify outside the network try: parsing/classification exceptions must not be reported as connection failures.
        data = _json_or_none(response)
        if status is not None and status >= 400:
            error = _classify(status, data, path)
        elif not isinstance(data, dict):
            error = TavilyError("tavily_unavailable", status, "响应不是 JSON")
    usage = data.get("usage") if isinstance(data, dict) else None
    logger.info(
        "tavily.call method=%s path=%s status=%s error_kind=%s credits=%s request_id=%s ms=%d",
        method,
        path,
        status,
        error.kind if error else None,
        usage.get("credits") if isinstance(usage, dict) else None,
        data.get("request_id") if isinstance(data, dict) else None,
        int((time.perf_counter() - started) * 1000),
    )
    if error is not None:
        if block_key and error.kind in _CONFIG_STATE_KINDS:
            _remember_turn_block(block_key, error.kind)
        return None, status, error
    return data, status, None
