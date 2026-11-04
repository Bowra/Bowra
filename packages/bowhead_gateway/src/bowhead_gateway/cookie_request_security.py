"""Reject ambient browser credentials on unsafe cross-origin requests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from bowhead_gateway.public_entry_context import current_request_public_access_context

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            return None
        return url.scheme, url.hostname.lower(), url.port if url.port is not None else (443 if url.scheme == "https" else 80)
    except ValueError:
        return None


def cookie_request_is_same_origin(request: HTTPConnection) -> bool:
    """Use request transport evidence, never a Team's selected link entry."""
    if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
        return False
    context = current_request_public_access_context()
    transport = request.base_url
    if transport.scheme in {"ws", "wss"}:
        transport = transport.replace(scheme="https" if transport.scheme == "wss" else "http")
    expected = _origin(context.base_url if context else str(transport))
    origin = request.headers.get("origin")
    if origin is not None:
        return expected is not None and _origin(origin) == expected
    referer = request.headers.get("referer")
    if referer is not None:
        return expected is not None and _origin(referer) == expected
    return request.headers.get("sec-fetch-site") == "same-origin"


class CookieRequestSecurityMiddleware:
    """Cover async/sync auth readers and direct refresh-cookie consumers alike.

    Cookie authentication currently precedes Bearer authentication. Merely
    supplying an Authorization header cannot exempt a cookie-backed request.
    Machine integrations without sign-in cookies retain their own auth policy;
    OAuth GET callbacks retain their one-use state and browser binding checks.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        websocket = scope["type"] == "websocket"
        if websocket or (scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"}):
            request = HTTPConnection(scope)
            untrusted = (websocket and request.headers.get("origin") is None) or not cookie_request_is_same_origin(request)
            if any(request.cookies.get(name) for name in ("bowhead_jwt", "bowhead_refresh")) and untrusted:
                if websocket:
                    await send({"type": "websocket.close", "code": 1008, "reason": "Cookie-authenticated WebSockets require a matching Origin."})
                    return
                response = JSONResponse(
                    {
                        "detail": {
                            "code": "cross_origin_cookie_request_rejected",
                            "message": "Open Bowhead and submit this request from the application. Cookie-authenticated clients must supply a matching Origin header.",
                        }
                    },
                    status_code=403,
                    headers={"Cache-Control": "private, no-store"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
