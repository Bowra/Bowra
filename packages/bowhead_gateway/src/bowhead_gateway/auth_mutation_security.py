"""Security checks for cookie-backed public auth mutations."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from bowhead_gateway.public_entry_context import current_request_public_access_context

logger = logging.getLogger(__name__)

#: scheme -> its default port. RFC 6454 defines origin as the (scheme, host, port) triple;
#: omit the port when it is the default: `https://a.com` and `https://a.com:443` are the same origin.
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def require_same_origin_json(request: Request) -> None:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, {"code": "json_content_type_required"})

    context = current_request_public_access_context()
    expected_source = context.base_url if context is not None else str(request.base_url)
    expected = _origin(expected_source)
    supplied_origin = request.headers.get("origin")
    if supplied_origin:
        if _origin(supplied_origin) != expected:
            _log_mismatch(request, expected, expected_source, supplied_origin, "origin")
            raise HTTPException(403, {"code": "cross_origin_mutation_rejected"})
        return

    referer = request.headers.get("referer")
    if not referer or _origin(referer) != expected:
        _log_mismatch(request, expected, expected_source, referer, "referer")
        raise HTTPException(403, {"code": "same_origin_required"})


def _log_mismatch(
    request: Request,
    expected: tuple[str, str, str],
    expected_source: str,
    supplied: str | None,
    header: str,
) -> None:
    """Log the expected and supplied origins with the expectation source.

    Scheme mismatches behind TLS-terminating proxies need both normalized origins
    to diagnose. Do not log request bodies or credentials.
    """
    logger.warning(
        "same_origin_rejected path=%s header=%s expected=%s expected_from=%s supplied=%s",
        request.url.path,
        header,
        expected,
        expected_source,
        _origin(supplied) if supplied else None,
    )


def _origin(value: str) -> tuple[str, str, str]:
    """把一个 URL 归一成 RFC 6454 的 origin 三元组 `(scheme, host, port)`。

    ⚠ feedback-triage-2026-08-26：原实现返回 `(scheme, netloc)`，而 `netloc` 是**裸串**
    且含端口 —— 于是 `https://a.com` 与 `https://a.com:443` 被判成不同源，尽管按 RFC
    它们是同一个。浏览器序列化 Origin 时会省掉默认端口，所以浏览器那一侧不会自己触发；
    但 `expected` 那一侧来自**我们自己的配置**（公开入口的 base_url）——入口只要被登记成
    `https://host:443`，所有真实浏览器就会全线 403。设计文档 §8.1 把「默认端口」列进
    URL 规范化的验收项，这里是实现没跟上契约。
    """
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = str(parsed.port) if parsed.port is not None else _DEFAULT_PORTS.get(scheme, "")
    return scheme, host, port
