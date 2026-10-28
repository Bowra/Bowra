"""Keep framework slash redirects relative to the browser's current entry."""

from __future__ import annotations

from starlette.datastructures import URL, MutableHeaders
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class PublicSlashRedirectMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_relative_redirect(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] == 307:
                headers = MutableHeaders(scope=message)
                source = URL(scope=scope)
                path = source.path
                target_path = path.rstrip("/") if path.endswith("/") else f"{path}/"
                expected = RedirectResponse(str(source.replace(path=target_path))).headers["location"]
                if target_path and headers.get("location") == expected:
                    # Only replace the framework's exact slash correction.
                    # Document-relative targets preserve even an unknown prefix
                    # stripped by a proxy, without exposing its upstream origin.
                    target = URL(expected)
                    name = target.path.rstrip("/").rsplit("/", 1)[-1]
                    relative = f"{'../' * (len(path) - len(path.rstrip('/')))}{name}" if path.endswith("/") else f"./{name}/"
                    headers["location"] = f"{relative}?{target.query}" if target.query else relative
            await send(message)

        await self.app(scope, receive, send_relative_redirect)
