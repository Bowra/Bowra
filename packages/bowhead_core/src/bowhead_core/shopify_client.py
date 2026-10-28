"""Bounded Shopify Admin object reads and typed content updates.

Team authorization and connection revisions belong to the native Tool adapter.
Access tokens stay ephemeral and are never written back over credentials.
It accepts no caller-authored GraphQL and never returns raw provider errors.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import anyio
import httpx2
from pydantic import ValidationError

from bowhead_core.error_catalog import CodedError
from bowhead_core.shopify_objects import (
    CHILD_READS,
    FIELD_POLICY_REVISION,
    ORDER_QUERY,
    ORDER_SNAPSHOTS_QUERY,
    ORDERS_QUERY,
    PAGE_LIMIT,
    PRODUCT_QUERY,
    PRODUCTS_QUERY,
    RESOURCE_READS,
    SHOP_QUERY,
    OrderData,
    OrdersData,
    OrderSnapshotsData,
    PageContentData,
    ProductContentData,
    ProductData,
    ProductsData,
    ShopData,
    ShopifyObject,
    incomplete_pages,
)
from bowhead_core.ssrf_guard import SsrfBlocked, build_guarded_async_client

if TYPE_CHECKING:
    from collections.abc import Mapping

ADMIN_API_VERSION = "2026-10"
CALL_TIMEOUT_SECONDS = 30.0
RESPONSE_BYTE_LIMIT = 1024 * 1024
_RETRY_CODES = frozenset({"shopify_unreachable", "shopify_service_unavailable", "shopify_throttled"})
_CONTENT_FIELDS = {
    "product": "id title descriptionHtml handle status updatedAt seo { title description }",
    "page": "id title handle body isPublished createdAt updatedAt publishedAt",
}


class ShopifyClientError(CodedError):
    """Finite failure category with no raw response, request or credentials."""

    def __init__(self, code: str, *, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(code)


@dataclass(frozen=True)
class ShopifyCredential:
    """Credentials supplied only by server-side connection administration."""

    shop_domain: str
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.shop_domain, str):
            raise ShopifyClientError("shopify_shop_domain_invalid")
        domain = self.shop_domain.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}\.myshopify\.com", domain):
            raise ShopifyClientError("shopify_shop_domain_invalid")
        if not all(isinstance(value, str) and value.strip() for value in (self.client_id, self.client_secret)):
            raise ShopifyClientError("shopify_credential_unavailable")
        object.__setattr__(self, "shop_domain", domain)


@dataclass(frozen=True)
class ShopifyReadResult:
    """Projected source data with explicit selected-query coverage."""

    data: dict[str, object]
    shop_domain: str
    fetched_at: str
    api_version: str = ADMIN_API_VERSION
    field_policy_revision: str = FIELD_POLICY_REVISION
    coverage: Literal["complete_for_query", "partial", "unknown"] = "complete_for_query"
    incomplete_pages: tuple[str, ...] = ()
    granted_scopes: frozenset[str] = frozenset()
    cost: dict[str, object] = field(default_factory=dict)


class ShopifyClient:
    """Read reviewed objects using same-organization client credentials.

    The caller must bind and authorize the Team connection before each read.
    Tokens are ephemeral and coalesced within this instance only; no process-local
    lock is presented as a cross-worker credential-generation mechanism.
    """

    def __init__(self, credential: ShopifyCredential, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._credential = credential
        self._transport = transport
        self._token: str | None = None
        self._refresh_at = 0.0
        self._scopes: frozenset[str] = frozenset()
        self._token_lock = anyio.Lock()

    async def read_shop(self) -> ShopifyReadResult:
        return await self._read(SHOP_QUERY, {}, ShopData)

    async def read_content(self, resource: str, object_id: str) -> ShopifyReadResult:
        query, projection, scopes = self._content_contract(resource)
        return await self._read(query, {"id": self._gid(object_id, "Product" if resource == "product" else "Page")}, projection, scope=scopes)

    @staticmethod
    def _content_contract(resource: str) -> tuple[str, type[ShopifyObject], str | tuple[str, ...]]:
        if resource == "product":
            return (
                "query Content($id: ID!) { product(id: $id) { " + _CONTENT_FIELDS[resource] + " } }",
                ProductContentData,
                "read_products",
            )
        if resource == "page":
            return (
                "query Content($id: ID!) { page(id: $id) { " + _CONTENT_FIELDS[resource] + " } }",
                PageContentData,
                ("read_content", "read_online_store_pages"),
            )
        raise ShopifyClientError("shopify_resource_invalid")

    async def update_content(self, resource: str, object_id: str, changes: dict[str, object]) -> ShopifyReadResult:
        """Dispatch one bounded mutation. Ambiguous replies never authorize retry."""
        _, projection, _ = self._content_contract(resource)
        identifier = self._gid(object_id, "Product" if resource == "product" else "Page")
        allowed = {"title", "descriptionHtml", "seo"} if resource == "product" else {"title", "body"}
        if not changes or set(changes) - allowed:
            raise ShopifyClientError("shopify_content_arguments_invalid")
        if "seo" in changes and (not isinstance(changes["seo"], dict) or set(changes["seo"]) - {"title", "description"}):
            raise ShopifyClientError("shopify_content_arguments_invalid")
        root = "productUpdate" if resource == "product" else "pageUpdate"
        fields = _CONTENT_FIELDS[resource]
        if resource == "product":
            query = (
                "mutation Content($input: ProductUpdateInput!) { productUpdate(product: $input) { product { " + fields + " } userErrors { field } } }"
            )
            variables = {"input": {"id": identifier, **changes}}
            scopes = ("write_products",)
        else:
            query = (
                "mutation Content($id: ID!, $input: PageUpdateInput!) { pageUpdate(id: $id, page: $input) { page { "
                + fields
                + " } userErrors { field } } }"
            )
            variables = {"id": identifier, "input": changes}
            scopes = ("write_content", "write_online_store_pages")
        dispatched = False
        try:
            with anyio.fail_after(CALL_TIMEOUT_SECONDS):
                async with build_guarded_async_client(transport=self._transport, timeout=CALL_TIMEOUT_SECONDS, follow_redirects=False) as http:
                    token = await self._access_token(http)
                    if not self._scopes.intersection(scopes):
                        raise ShopifyClientError("shopify_scope_unavailable")
                    dispatched = True
                    payload, headers = await self._post(
                        http,
                        f"https://{self._credential.shop_domain}/admin/api/{ADMIN_API_VERSION}/graphql.json",
                        {"query": query, "variables": variables},
                        token=token,
                    )
                    if headers.get("X-Shopify-API-Version") != ADMIN_API_VERSION or payload.get("errors"):
                        raise ShopifyClientError("shopify_write_outcome_unknown")
                    data = payload.get("data")
                    value = data.get(root) if isinstance(data, dict) else None
                    if not isinstance(value, dict) or not isinstance(value.get("userErrors"), list):
                        raise ShopifyClientError("shopify_write_outcome_unknown")
                    if value["userErrors"]:
                        if value.get(resource) is not None:
                            raise ShopifyClientError("shopify_write_outcome_unknown")
                        raise ShopifyClientError("shopify_content_rejected")
                    selected = projection.model_validate({resource: value.get(resource)})
                    if getattr(selected, resource) is None:
                        raise ShopifyClientError("shopify_write_outcome_unknown")
                    return ShopifyReadResult(
                        data=selected.model_dump(mode="json"),
                        shop_domain=self._credential.shop_domain,
                        fetched_at=dt.datetime.now(dt.UTC).isoformat(),
                        granted_scopes=self._scopes,
                    )
        except (TimeoutError, SsrfBlocked, ValidationError, ShopifyClientError) as exc:
            if dispatched:
                if isinstance(exc, ShopifyClientError) and exc.code in {"shopify_access_denied", "shopify_throttled", "shopify_content_rejected"}:
                    raise
                raise ShopifyClientError("shopify_write_outcome_unknown") from None
            if isinstance(exc, ShopifyClientError):
                raise
            raise ShopifyClientError("shopify_timeout" if isinstance(exc, TimeoutError) else "shopify_outbound_blocked") from None

    async def list_products(self, *, first: int = PAGE_LIMIT, after: str | None = None, nested_first: int = PAGE_LIMIT) -> ShopifyReadResult:
        variables = self._page_arguments(first, after, nested_first)
        return await self._read(PRODUCTS_QUERY, variables, ProductsData, scope="read_products")

    async def read_product(self, product_id: str, *, nested_first: int = PAGE_LIMIT) -> ShopifyReadResult:
        variables = {"id": self._gid(product_id, "Product"), "nestedFirst": self._page_size(nested_first)}
        return await self._read(PRODUCT_QUERY, variables, ProductData, scope="read_products")

    async def list_orders(
        self,
        *,
        created_from: dt.datetime,
        created_until: dt.datetime,
        test: bool | None = None,
        first: int = PAGE_LIMIT,
        after: str | None = None,
        nested_first: int = PAGE_LIMIT,
    ) -> ShopifyReadResult:
        """Read a half-open creation interval, preserving snapshot semantics."""
        if any(value.tzinfo is None or value.utcoffset() is None for value in (created_from, created_until)):
            raise ShopifyClientError("shopify_date_invalid")
        if created_until <= created_from or (test is not None and type(test) is not bool):
            raise ShopifyClientError("shopify_filter_invalid")
        start, end = (value.astimezone(dt.UTC).isoformat() for value in (created_from, created_until))
        variables = self._page_arguments(first, after, nested_first)
        variables["filter"] = f'created_at:>="{start}" AND created_at:<"{end}"'
        if test is not None:
            variables["filter"] += f" AND test:{str(test).lower()}"
        return await self._read(ORDERS_QUERY, variables, OrdersData, scope="read_orders", history_start=created_from)

    async def read_order(self, order_id: str, *, nested_first: int = PAGE_LIMIT) -> ShopifyReadResult:
        variables = {"id": self._gid(order_id, "Order"), "nestedFirst": self._page_size(nested_first)}
        return await self._read(ORDER_QUERY, variables, OrderData, scope="read_orders")

    async def list_resource(self, resource: str, *, first: int = PAGE_LIMIT, after: str | None = None, nested_first: int = 5) -> ShopifyReadResult:
        if resource not in RESOURCE_READS:
            raise ShopifyClientError("shopify_resource_invalid")
        query, projection, scope = RESOURCE_READS[resource]
        variables = self._page_arguments(first, after, nested_first)
        if resource != "inventory":
            variables.pop("nestedFirst")
        return await self._read(query, variables, projection, scope=scope)

    async def list_children(
        self, resource: str, parent_id: str, *, first: int = PAGE_LIMIT, after: str | None = None, nested_first: int = 5
    ) -> ShopifyReadResult:
        if resource not in CHILD_READS:
            raise ShopifyClientError("shopify_resource_invalid")
        parent, kind, child, fields, projection, scope = CHILD_READS[resource]
        variables = self._page_arguments(first, after, nested_first)
        variables["id"] = self._gid(parent_id, kind)
        nested = ", $nestedFirst: Int!" if resource == "collection_products" else ""
        if not nested:
            variables.pop("nestedFirst")
        query = (
            "query Children($id: ID!, $first: Int!, $after: String"
            + nested
            + ") { "
            + parent
            + "(id: $id) { id "
            + child
            + "(first: $first, after: $after) { nodes { "
            + fields
            + " } pageInfo { hasNextPage endCursor } } } }"
        )
        return await self._read(query, variables, projection, scope=scope)

    async def list_order_snapshots(
        self, *, created_from: dt.datetime, created_until: dt.datetime, first: int = PAGE_LIMIT, after: str | None = None
    ) -> ShopifyReadResult:
        if any(value.tzinfo is None or value.utcoffset() is None for value in (created_from, created_until)) or created_until <= created_from:
            raise ShopifyClientError("shopify_date_invalid")
        start, end = (value.astimezone(dt.UTC).isoformat() for value in (created_from, created_until))
        variables = self._page_arguments(first, after, 1)
        variables.pop("nestedFirst")
        variables["filter"] = f'created_at:>="{start}" AND created_at:<"{end}" AND test:false'
        return await self._read(ORDER_SNAPSHOTS_QUERY, variables, OrderSnapshotsData, scope="read_orders", history_start=created_from)

    @staticmethod
    def _page_size(value: int) -> int:
        if type(value) is not int or not 1 <= value <= PAGE_LIMIT:
            raise ShopifyClientError("shopify_page_limit_invalid")
        return value

    @classmethod
    def _page_arguments(cls, first: int, after: str | None, nested_first: int) -> dict[str, object]:
        if after is not None and (not isinstance(after, str) or not after or len(after) > 2048 or any(ord(char) < 32 for char in after)):
            raise ShopifyClientError("shopify_cursor_invalid")
        return {"first": cls._page_size(first), "after": after, "nestedFirst": cls._page_size(nested_first)}

    @staticmethod
    def _gid(value: str, resource: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(rf"gid://shopify/{resource}/[0-9]+", value):
            raise ShopifyClientError("shopify_object_id_invalid")
        return value

    async def _read(
        self,
        query: str,
        variables: dict[str, object],
        projection: type[ShopifyObject],
        *,
        scope: str | tuple[str, ...] | None = None,
        history_start: dt.datetime | None = None,
    ) -> ShopifyReadResult:
        try:
            with anyio.fail_after(CALL_TIMEOUT_SECONDS):
                async with build_guarded_async_client(transport=self._transport, timeout=CALL_TIMEOUT_SECONDS, follow_redirects=False) as http:
                    token = await self._access_token(http)
                    if scope is not None and not self._scopes.intersection((scope,) if isinstance(scope, str) else scope):
                        raise ShopifyClientError("shopify_scope_unavailable")
                    if history_start is not None and "read_all_orders" not in self._scopes:
                        if history_start < dt.datetime.now(dt.UTC) - dt.timedelta(days=60):
                            raise ShopifyClientError("shopify_history_unavailable")
                    return await self._read_with_retries(http, token, query, variables, projection)
        except TimeoutError:
            raise ShopifyClientError("shopify_timeout") from None
        except SsrfBlocked:
            raise ShopifyClientError("shopify_outbound_blocked") from None

    async def _read_with_retries(
        self,
        http: httpx2.AsyncClient,
        token: str,
        query: str,
        variables: dict[str, object],
        projection: type[ShopifyObject],
    ) -> ShopifyReadResult:
        url = f"https://{self._credential.shop_domain}/admin/api/{ADMIN_API_VERSION}/graphql.json"
        for attempt in range(3):
            try:
                payload, headers = await self._post(http, url, {"query": query, "variables": variables}, token=token)
                if headers.get("X-Shopify-API-Version") != ADMIN_API_VERSION:
                    raise ShopifyClientError("shopify_api_version_unverified")
                if payload.get("errors"):
                    self._raise_graphql_error(payload["errors"])
                value = projection.model_validate(payload.get("data"))
                unread = incomplete_pages(value)
                missing_object = any(getattr(value, name) is None for name in type(value).model_fields)
                return ShopifyReadResult(
                    data=value.model_dump(mode="json"),
                    shop_domain=self._credential.shop_domain,
                    fetched_at=dt.datetime.now(dt.UTC).isoformat(),
                    coverage="unknown" if missing_object else "partial" if unread else "complete_for_query",
                    incomplete_pages=unread,
                    granted_scopes=self._scopes,
                    cost=self._safe_cost(payload.get("extensions")),
                )
            except ValidationError:
                raise ShopifyClientError("shopify_response_invalid") from None
            except ShopifyClientError as exc:
                if exc.code not in _RETRY_CODES or attempt == 2:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else 0.25 * 2**attempt
                if delay >= anyio.current_effective_deadline() - anyio.current_time():
                    raise
                await anyio.sleep(delay)
        raise ShopifyClientError("shopify_service_unavailable")

    async def _access_token(self, http: httpx2.AsyncClient) -> str:
        async with self._token_lock:
            if self._token is not None and time.monotonic() < self._refresh_at:
                return self._token
            url = f"https://{self._credential.shop_domain}/admin/oauth/access_token"
            payload, _ = await self._post(
                http,
                url,
                {
                    "grant_type": "client_credentials",
                    "client_id": self._credential.client_id,
                    "client_secret": self._credential.client_secret,
                },
            )
            token, expiry, scopes = (payload.get(key) for key in ("access_token", "expires_in", "scope"))
            if not isinstance(token, str) or not token or len(token) > 16384 or any(ord(char) < 32 for char in token):
                raise ShopifyClientError("shopify_token_response_invalid")
            if type(expiry) is not int or expiry <= 0 or not isinstance(scopes, str):
                raise ShopifyClientError("shopify_token_response_invalid")
            self._token = token
            self._scopes = frozenset(scope.strip() for scope in scopes.split(",") if re.fullmatch(r"[a-z_]+", scope.strip()))
            self._refresh_at = time.monotonic() + max(0, min(expiry, 86399) - 60)
            return token

    @staticmethod
    async def _post(
        http: httpx2.AsyncClient, url: str, body: dict[str, object], *, token: str | None = None
    ) -> tuple[dict[str, object], httpx2.Headers]:
        headers = {"X-Shopify-Access-Token": token} if token is not None else {}
        try:
            async with http.stream("POST", url, json=body, headers=headers) as response:
                if response.status_code == 429:
                    delay = response.headers.get("Retry-After", "1")
                    try:
                        seconds = float(delay)
                    except ValueError:
                        seconds = 1.0
                    seconds = seconds if math.isfinite(seconds) and seconds >= 0 else 1.0
                    raise ShopifyClientError("shopify_throttled", retry_after=seconds)
                if response.status_code in {401, 403}:
                    raise ShopifyClientError("shopify_access_denied")
                if response.status_code >= 500:
                    raise ShopifyClientError("shopify_service_unavailable")
                if response.status_code >= 300:
                    raise ShopifyClientError("shopify_request_rejected")
                raw = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(raw) + len(chunk) > RESPONSE_BYTE_LIMIT:
                        raise ShopifyClientError("shopify_response_too_large")
                    raw.extend(chunk)
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ShopifyClientError("shopify_response_invalid") from None
                if not isinstance(payload, dict):
                    raise ShopifyClientError("shopify_response_invalid")
                return payload, response.headers
        except httpx2.RequestError:
            raise ShopifyClientError("shopify_unreachable") from None

    @staticmethod
    def _raise_graphql_error(errors: object) -> None:
        if not isinstance(errors, list) or any(not isinstance(error, dict) for error in errors):
            raise ShopifyClientError("shopify_response_invalid")
        codes = {str((error.get("extensions") or {}).get("code")) for error in errors if isinstance(error.get("extensions"), dict)}
        if "ACCESS_DENIED" in codes:
            raise ShopifyClientError("shopify_access_denied")
        if "THROTTLED" in codes:
            raise ShopifyClientError("shopify_throttled", retry_after=1.0)
        raise ShopifyClientError("shopify_graphql_error")

    @staticmethod
    def _safe_cost(extensions: object) -> dict[str, object]:
        if not isinstance(extensions, dict) or not isinstance(extensions.get("cost"), dict):
            return {}
        source = extensions["cost"]
        out: dict[str, object] = {}

        def copy_numbers(values: Mapping[str, object], names: tuple[str, ...]) -> dict[str, object]:
            return {key: value for key in names if type(value := values.get(key)) in {int, float} and 0 <= value <= 2**63 - 1}

        out.update(copy_numbers(source, ("requestedQueryCost", "actualQueryCost")))
        throttle = source.get("throttleStatus")
        if isinstance(throttle, dict):
            out["throttleStatus"] = copy_numbers(throttle, ("maximumAvailable", "currentlyAvailable", "restoreRate"))
        return out
