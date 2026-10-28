"""ASGI boundary for late personal-Memory write detection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bowhead_memory.privacy_guard import (
    reset_memory_mutation_context,
    start_memory_mutation_context,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


class MemoryMutationContextMiddleware:
    """Capture request start once so inherited background tasks keep its boundary."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        token = start_memory_mutation_context()
        try:
            await self.app(scope, receive, send)
        finally:
            reset_memory_mutation_context(token)
