"""Small Feishu OpenAPI client used by the IM adapter."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx2

if TYPE_CHECKING:
    from bowhead_gateway.feishu.config import FeishuAppConfigResolver


class FeishuAPIError(RuntimeError):
    """Raised when Feishu OpenAPI returns a non-zero business code."""


class FeishuClient:
    def __init__(
        self,
        app_config_resolver: FeishuAppConfigResolver,
        *,
        base_url: str = "https://open.feishu.cn",
        timeout: float = 15.0,
        transport: httpx2.AsyncBaseTransport | None = None,
        max_retries: int = 2,
        retry_backoff_base: float = 0.25,
    ) -> None:
        self.app_config_resolver = app_config_resolver
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_base = max(0.0, float(retry_backoff_base))
        self._transport = transport
        self._tenant_tokens: dict[tuple[str, str, str], tuple[str, float]] = {}
        self._http: httpx2.AsyncClient | None = None

    def tenant_token_cache_keys(self) -> list[tuple[str, str, str]]:
        return list(self._tenant_tokens.keys())

    def clear_tenant_token_cache(self) -> None:
        self._tenant_tokens.clear()

    async def aclose(self) -> None:
        if self._http is None:
            return
        await self._http.aclose()
        self._http = None

    async def tenant_access_token(self, *, tenant_key: str | None = None) -> str:
        now = time.time()
        app_config = self.app_config_resolver.resolve(tenant_key)
        cache_key = app_config.token_cache_key(tenant_key)
        cached = self._tenant_tokens.get(cache_key)
        if cached and now < cached[1] - 300:
            return cached[0]
        if app_config.app_type != "internal":
            raise FeishuAPIError("Feishu store-app token flow is not implemented in Phase 1")
        if not (app_config.app_id and app_config.app_secret):
            raise FeishuAPIError("Feishu app_id/app_secret not configured")
        data = await self._request_raw(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": app_config.app_id,
                "app_secret": app_config.app_secret,
            },
            auth=False,
            retryable=True,
        )
        token = str(data.get("tenant_access_token") or "")
        if not token:
            raise FeishuAPIError("Feishu tenant_access_token missing in response")
        self._tenant_tokens[cache_key] = (token, now + float(data.get("expire") or 7200))
        return token

    async def send_message(
        self,
        *,
        receive_id_type: str,
        receive_id: str,
        msg_type: str,
        content: dict[str, Any],
        tenant_key: str | None = None,
        uuid: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            json={
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": _content_string(content),
                "uuid": uuid or str(uuid4()),
            },
            tenant_key=tenant_key,
            retryable=True,
        )

    async def reply_message(
        self,
        *,
        message_id: str,
        msg_type: str,
        content: dict[str, Any],
        tenant_key: str | None = None,
        uuid: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "msg_type": msg_type,
            "content": _content_string(content),
        }
        if uuid:
            body["uuid"] = uuid
        return await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            json=body,
            tenant_key=tenant_key,
            retryable=bool(uuid),
        )

    async def patch_card(
        self,
        *,
        message_id: str,
        card: dict[str, Any],
        tenant_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/open-apis/im/v1/messages/{message_id}",
            json={"content": _content_string(card)},
            tenant_key=tenant_key,
            retryable=True,
        )

    async def send_ephemeral_card(
        self,
        *,
        chat_id: str,
        open_id: str,
        card: dict[str, Any],
        tenant_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/open-apis/ephemeral/v1/send",
            json={
                "chat_id": chat_id,
                "open_id": open_id,
                "msg_type": "interactive",
                "card": card,
            },
            tenant_key=tenant_key,
        )

    async def add_reaction(
        self,
        *,
        message_id: str,
        emoji_type: str,
        tenant_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reactions",
            json={"reaction_type": {"emoji_type": emoji_type}},
            tenant_key=tenant_key,
        )

    async def get_user_email(
        self,
        *,
        open_id: str,
        tenant_key: str | None = None,
    ) -> str | None:
        data = await self._request(
            "GET",
            f"/open-apis/contact/v3/users/{open_id}?user_id_type=open_id",
            tenant_key=tenant_key,
            retryable=True,
        )
        user = data.get("user") if isinstance(data, dict) else None
        if isinstance(user, dict):
            email = user.get("email")
            return str(email).lower().strip() if email else None
        return None

    async def download_message_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        tenant_key: str | None = None,
    ) -> bytes:
        token = await self.tenant_access_token(tenant_key=tenant_key)
        resp = await self._send_with_retry(
            "GET",
            f"{self.base_url}/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
            headers={"Authorization": f"Bearer {token}"},
            retryable=True,
        )
        resp.raise_for_status()
        return resp.content

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        tenant_key: str | None = None,
        retryable: bool = False,
    ) -> dict[str, Any]:
        return await self._request_raw(
            method,
            path,
            json=json,
            auth=True,
            tenant_key=tenant_key,
            retryable=retryable,
        )

    async def _request_raw(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        auth: bool,
        tenant_key: str | None = None,
        retryable: bool = False,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {"Content-Type": "application/json; charset=utf-8"}
        if auth:
            headers["Authorization"] = f"Bearer {await self.tenant_access_token(tenant_key=tenant_key)}"
        resp = await self._send_with_retry(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            json=json,
            retryable=retryable,
        )
        resp.raise_for_status()
        data = resp.json()
        code = int(data.get("code", 0) or 0)
        if code != 0:
            raise FeishuAPIError(f"Feishu API error {code}: {data.get('msg') or data}")
        inner = data.get("data")
        return inner if isinstance(inner, dict) else data

    def _client(self) -> httpx2.AsyncClient:
        if self._http is None:
            self._http = httpx2.AsyncClient(
                timeout=self.timeout,
                transport=self._transport,
                limits=httpx2.Limits(max_keepalive_connections=20, max_connections=100),
            )
        return self._http

    async def _send_with_retry(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        retryable: bool,
    ) -> httpx2.Response:
        client = self._client()
        attempts = self.max_retries + 1 if retryable else 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=json,
                )
            except httpx2.HTTPError as exc:
                last_exc = exc
                if attempt >= attempts:
                    raise
            else:
                if not retryable or not _should_retry_response(resp) or attempt >= attempts:
                    return resp
                last_exc = httpx2.HTTPStatusError(
                    f"retryable Feishu response status {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            if self.retry_backoff_base > 0:
                await _sleep_backoff(self.retry_backoff_base, attempt)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Feishu request retry loop exited without a response")


def _content_string(content: dict[str, Any]) -> str:
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))


def _should_retry_response(resp: httpx2.Response) -> bool:
    return resp.status_code in {408, 425, 429, 500, 502, 503, 504}


async def _sleep_backoff(base: float, attempt: int) -> None:
    await asyncio.sleep(min(base * (2 ** (attempt - 1)), 2.0))
