"""Gateway-owned HTTP request correlation and shared CORS policy."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

HTTP_REQUEST_ID_STATE_KEY = "http_request_id"
REQUEST_ID_HEADER = "X-Request-ID"
_CORS_ALLOW_ORIGINS = ("*",)
_CORS_ALLOW_CREDENTIALS = False
_CORS_ALLOW_METHODS = ("*",)
_CORS_ALLOW_HEADERS = ("*",)
_CORS_EXPOSE_HEADERS = (REQUEST_ID_HEADER,)

logger = structlog.get_logger(__name__)


def cors_middleware_options() -> dict[str, Any]:
    """Return the single CORS policy used by normal and outer 500 responses."""

    return {
        "allow_origins": list(_CORS_ALLOW_ORIGINS),
        "allow_credentials": _CORS_ALLOW_CREDENTIALS,
        "allow_methods": list(_CORS_ALLOW_METHODS),
        "allow_headers": list(_CORS_ALLOW_HEADERS),
        "expose_headers": list(_CORS_EXPOSE_HEADERS),
    }


def cors_response_headers(scope: Scope) -> dict[str, str]:
    """Project the shared wildcard policy onto a response outside CORS middleware."""

    origin = Headers(scope=scope).get("origin")
    if not origin:
        return {}
    # Current policy is deliberately wildcard + no credentials. Keep this
    # branch explicit so a future policy change cannot accidentally reflect an
    # unverified Origin from an outer error handler.
    if _CORS_ALLOW_ORIGINS == ("*",) and not _CORS_ALLOW_CREDENTIALS:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": ", ".join(_CORS_EXPOSE_HEADERS),
        }
    return {}


def request_id_from_scope(scope: Scope) -> str | None:
    state = scope.get("state")
    if not isinstance(state, dict):
        return None
    value = state.get(HTTP_REQUEST_ID_STATE_KEY)
    return value if isinstance(value, str) and value else None


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path and len(path) <= 256:
        return path
    return "unmatched"


class RequestIdContextMiddleware:
    """Own a fresh request ID for every HTTP scope and expose it on all responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4().hex
        state = scope.setdefault("state", {})
        if not isinstance(state, dict):
            state = {}
            scope["state"] = state
        state[HTTP_REQUEST_ID_STATE_KEY] = request_id
        context_tokens = structlog.contextvars.bind_contextvars(
            http_request_id=request_id,
        )
        started_at = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            method = str(scope.get("method") or "UNKNOWN").upper()[:16]
            try:
                logger.info(
                    "gateway.http_request_completed",
                    http_method=method,
                    route_template=_route_template(scope),
                    status_code=status_code,
                    duration_ms=duration_ms,
                )
            finally:
                structlog.contextvars.reset_contextvars(**context_tokens)
