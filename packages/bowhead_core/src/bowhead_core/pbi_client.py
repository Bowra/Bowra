"""Power BI Service Principal REST client.

All credentials are supplied by the team connection store and are used only on
the server. Access tokens stay in this process and are never persisted.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx2

_AUTHORITY = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
PBI_API = "https://api.powerbi.com/v1.0/myorg"
PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
_ROW_CAP = 100_000
_TOKEN_CACHE_TTL_S = 45 * 60
_QUERY_TIMEOUT_S = 30.0
_QUERY_RETRY_COUNT = 3


@dataclass
class PbiResult:
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    error_code: str | None = None


class PbiClientError(Exception):
    """Finite failure code only; provider response text never crosses this boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PbiTokenProvider:
    """Client-credentials token cache, shared by one team's Power BI connection."""

    def __init__(self, credential: dict[str, Any]) -> None:
        self._credential = dict(credential)
        self._access: str | None = None
        self._refresh_at = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self, *, http: httpx2.AsyncClient) -> str:
        if self._access and time.monotonic() < self._refresh_at:
            return self._access
        async with self._lock:
            if self._access and time.monotonic() < self._refresh_at:
                return self._access
            await self._refresh(http)
            assert self._access is not None
            return self._access

    async def _refresh(self, http: httpx2.AsyncClient) -> None:
        tenant_id = self._credential.get("tenant_id")
        client_id = self._credential.get("client_id")
        client_secret = self._credential.get("client_secret")
        if not all(isinstance(value, str) and value for value in (tenant_id, client_id, client_secret)):
            raise PbiClientError("pbi_connection_credential_unavailable")
        try:
            response = await http.post(
                _AUTHORITY.format(tenant=tenant_id),
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": PBI_SCOPE,
                },
            )
        except (httpx2.TimeoutException, httpx2.RequestError) as exc:
            raise PbiClientError("pbi_token_refresh_failed") from exc
        if response.status_code in {400, 401, 403}:
            raise PbiClientError("pbi_token_refresh_rejected")
        if response.status_code >= 400:
            raise PbiClientError("pbi_token_refresh_failed")
        try:
            body = response.json()
            access_token = body["access_token"]
            expires_in = int(body.get("expires_in", 3600))
        except (KeyError, TypeError, ValueError) as exc:
            raise PbiClientError("pbi_token_refresh_failed") from exc
        if not isinstance(access_token, str) or not access_token or expires_in <= 0:
            raise PbiClientError("pbi_token_refresh_failed")
        self._access = access_token
        self._refresh_at = time.monotonic() + min(_TOKEN_CACHE_TTL_S, max(1, expires_in - 300))


class PbiClient:
    """Read-only executeQueries client for a configured semantic model."""

    def __init__(
        self,
        *,
        workspace_id: str,
        dataset_id: str,
        token_provider: PbiTokenProvider,
        query_limiter: asyncio.Semaphore | None = None,
    ) -> None:
        self._workspace_id = workspace_id
        self._dataset_id = dataset_id
        self._tokens = token_provider
        self._query_limiter = query_limiter

    async def execute_dax(self, dax: str) -> PbiResult:
        if not self._workspace_id or not self._dataset_id:
            return PbiResult(error_code="pbi_query_not_found")
        if self._query_limiter is None:
            return await self._execute_dax(dax)
        async with self._query_limiter:
            return await self._execute_dax(dax)

    async def _execute_dax(self, dax: str) -> PbiResult:
        url = f"{PBI_API}/groups/{self._workspace_id}/datasets/{self._dataset_id}/executeQueries"
        body = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
        try:
            async with httpx2.AsyncClient(timeout=_QUERY_TIMEOUT_S) as http:
                token = await self._tokens.get_token(http=http)
                for attempt in range(_QUERY_RETRY_COUNT):
                    response = await http.post(
                        url,
                        json=body,
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    )
                    if response.status_code != 429 or attempt == _QUERY_RETRY_COUNT - 1:
                        break
                    await asyncio.sleep(0.25 * (2**attempt))
        except PbiClientError as exc:
            return PbiResult(error_code=exc.code)
        except (httpx2.TimeoutException, httpx2.RequestError):
            return PbiResult(error_code="pbi_query_unreachable")
        if response.status_code == 400:
            return PbiResult(error_code="pbi_query_invalid_request")
        if response.status_code == 401:
            return PbiResult(error_code="pbi_query_auth_rejected")
        if response.status_code == 403:
            return PbiResult(error_code="pbi_query_access_denied")
        if response.status_code == 404:
            return PbiResult(error_code="pbi_query_not_found")
        if response.status_code == 429:
            return PbiResult(error_code="pbi_query_rate_limited")
        if response.status_code >= 400:
            return PbiResult(error_code="pbi_query_failed")
        try:
            return self._parse(response.json())
        except ValueError:
            return PbiResult(error_code="pbi_query_response_invalid")

    @staticmethod
    def _parse(payload: object) -> PbiResult:
        if not isinstance(payload, dict):
            return PbiResult(error_code="pbi_query_response_invalid")
        results = payload.get("results")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            return PbiResult(error_code="pbi_query_response_invalid")
        result = results[0]
        tables = result.get("tables")
        if not isinstance(tables, list) or len(tables) != 1 or not isinstance(tables[0], dict):
            return PbiResult(error_code="pbi_query_response_invalid")
        table = tables[0]
        rows = table.get("rows") or []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return PbiResult(error_code="pbi_query_response_invalid")
        if result.get("error") or table.get("error") or len(rows) >= _ROW_CAP:
            return PbiResult(truncated=True, error_code="pbi_query_result_incomplete")
        return PbiResult(columns=list(rows[0]) if rows else [], rows=rows, row_count=len(rows))


@dataclass
class PbiTurnHandle:
    client: PbiClient
    catalog: dict[str, Any] | None = None


@dataclass(frozen=True)
class PbiUnavailableHandle:
    error_code: str


class PbiTeamRuntime:
    """In-memory state for one team connection: a token cache and five query slots."""

    def __init__(self, credential: dict[str, Any]) -> None:
        self._tokens = PbiTokenProvider(credential)
        self._queries = asyncio.Semaphore(5)

    def handle(self, *, workspace_id: str, dataset_id: str, catalog: dict[str, Any] | None = None) -> PbiTurnHandle:
        return PbiTurnHandle(
            client=PbiClient(
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                token_provider=self._tokens,
                query_limiter=self._queries,
            ),
            catalog=catalog,
        )


def build_pbi_turn_handle(
    *,
    credential: dict[str, Any],
    workspace_id: str,
    dataset_id: str,
    catalog: dict[str, Any] | None = None,
    persist_rotated: Any = None,
) -> PbiTurnHandle:
    """Compatibility constructor for callers that only need a non-shared handle.

    Service Principal tokens have no refresh-token persistence or user impersonation.
    """
    del persist_rotated
    return PbiTeamRuntime(credential).handle(
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        catalog=catalog,
    )
