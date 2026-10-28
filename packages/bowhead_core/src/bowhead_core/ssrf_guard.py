"""SSRF guard — thin re-export of the repo-wide single implementation.

实现已于 2026-07 收敛到 `bowhead_router.ssrf_guard`（router 是更底层的包：
core 依赖 router，反向不成立）。本模块仅保留 import 路径向后兼容——
KB embed/rerank、media、web_fetch/web_search、openapi_tool_source、mcp_oauth、
email/search/llm team config 及 bi_bridge/memory 的懒加载均沿用
`bowhead_core.ssrf_guard`。policy 改动请去 `bowhead_router.ssrf_guard`，
两包共享同一份，不再各自演化。
"""

from __future__ import annotations

from bowhead_router.ssrf_guard import (
    SMTP_PORTS,
    Resolver,
    SmtpPortBlocked,
    SsrfBlocked,
    SsrfGuardedAsyncTransport,
    ValidatedTarget,
    build_guarded_async_client,
    resolve_and_validate,
    validate_smtp_target,
    validate_url,
)

__all__ = [
    "SMTP_PORTS",
    "Resolver",
    "SmtpPortBlocked",
    "SsrfBlocked",
    "SsrfGuardedAsyncTransport",
    "ValidatedTarget",
    "build_guarded_async_client",
    "resolve_and_validate",
    "validate_smtp_target",
    "validate_url",
]
