"""Shopify object reads, projection and bounded external-service failures."""

from __future__ import annotations

import copy
import datetime as dt
import json
from typing import TYPE_CHECKING

import anyio
import bowhead_core.shopify_client as module
import httpx2
import pytest
from bowhead_core.shopify_client import ADMIN_API_VERSION, ShopifyClient, ShopifyClientError, ShopifyCredential

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.unit

_SCOPES = "read_products,read_orders,read_all_orders,read_customers,read_reports"
_SHOP = {"id": "gid://shopify/Shop/1", "name": "Fixture shop", "currencyCode": "USD", "ianaTimezone": "America/New_York"}


def _money(amount: str) -> dict[str, object]:
    return {"shopMoney": {"amount": amount, "currencyCode": "USD"}, "presentmentMoney": {"amount": amount, "currencyCode": "USD"}}


def _order() -> dict[str, object]:
    return {
        "id": "gid://shopify/Order/1",
        "name": "#1001",
        "createdAt": "2026-10-01T10:00:00Z",
        "updatedAt": "2026-10-02T10:00:00Z",
        "processedAt": "2026-10-01T10:00:00Z",
        "test": True,
        "cancelledAt": None,
        "displayFinancialStatus": "PARTIALLY_REFUNDED",
        "displayFulfillmentStatus": "UNFULFILLED",
        "currencyCode": "USD",
        "currentTotalPriceSet": _money("111.10"),
        "currentTotalDiscountsSet": _money("12.30"),
        "totalRefundedSet": _money("7.00"),
        "lineItems": {
            "nodes": [
                {
                    "id": "gid://shopify/LineItem/1",
                    "sku": "FIXTURE-SKU",
                    "quantity": 2,
                    "currentQuantity": 1,
                    "product": {"id": "gid://shopify/Product/1"},
                    "variant": {"id": "gid://shopify/ProductVariant/1"},
                }
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    }


def _product() -> dict[str, object]:
    return {
        "id": "gid://shopify/Product/1",
        "title": "Fixture product",
        "handle": "fixture-product",
        "status": "ACTIVE",
        "productType": "Fixture",
        "vendor": "Fixture vendor",
        "descriptionHtml": "<p>Fixture description</p>",
        "createdAt": "2026-10-01T10:00:00Z",
        "updatedAt": "2026-10-02T10:00:00Z",
        "variants": {
            "nodes": [{"id": "gid://shopify/ProductVariant/1", "title": "Default", "sku": None, "price": "12.30"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    }


class RecordingApi:
    """Mock only the external HTTP boundary, preserving real client behavior."""

    def __init__(self, payloads: list[dict[str, object]], *, scopes: str = _SCOPES, version: str = ADMIN_API_VERSION) -> None:
        self.payloads = list(payloads)
        self.scopes = scopes
        self.version = version
        self.requests: list[httpx2.Request] = []
        self.token_calls = 0

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path.endswith("/access_token"):
            self.token_calls += 1
            await anyio.lowlevel.checkpoint()
            return httpx2.Response(200, json={"access_token": "fixture-access", "expires_in": 86399, "scope": self.scopes})
        return httpx2.Response(200, headers={"X-Shopify-API-Version": self.version}, json=self.payloads.pop(0))

    def client(self) -> ShopifyClient:
        return ShopifyClient(
            ShopifyCredential("fixture-shop.myshopify.com", "fixture-client", "fixture-secret"),
            transport=httpx2.MockTransport(self.handle),
        )


@pytest.fixture(autouse=True)
def _local_transport_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")


async def test_order_output_excludes_customer_data_at_every_nested_level() -> None:
    expected = _order()
    unsafe = copy.deepcopy(expected)
    unsafe.update(
        customer={"id": "gid://shopify/Customer/7", "email": "buyer@example.test"},
        email="buyer@example.test",
        phone="fixture-phone",
        shippingAddress={"name": "Fixture buyer"},
        note="Fixture customer note",
        tags=["Fixture customer tag"],
        customAttributes=[{"key": "delivery", "value": "Fixture customer text"}],
    )
    unsafe["lineItems"]["nodes"][0]["customAttributes"] = [{"value": "Fixture personalization"}]
    unsafe["lineItems"]["nodes"][0]["product"]["customer"] = {"id": "gid://shopify/Customer/7"}
    unsafe["currentTotalPriceSet"]["shopMoney"]["email"] = "buyer@example.test"
    api = RecordingApi([{"data": {"order": unsafe, "customer": {"id": "gid://shopify/Customer/7"}}}])
    result = await api.client().read_order("gid://shopify/Order/1")
    assert result.data == {"order": expected}
    assert result.coverage == "complete_for_query"
    assert "read_customers" in result.granted_scopes
    assert "buyer@example.test" not in repr(result)
    request = api.requests[-1]
    assert request.url.host == "fixture-shop.myshopify.com"
    assert request.headers["X-Shopify-Access-Token"] == "fixture-access"
    query = json.loads(request.content)["query"]
    assert "customer" not in query and "shippingAddress" not in query


async def test_nested_order_pagination_cannot_be_reported_as_complete() -> None:
    order = _order()
    order["lineItems"]["pageInfo"] = {"hasNextPage": True, "endCursor": "line-cursor"}
    api = RecordingApi([{"data": {"orders": {"nodes": [order], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}])
    result = await api.client().list_orders(
        created_from=dt.datetime(2026, 10, 1, tzinfo=dt.UTC), created_until=dt.datetime(2026, 10, 2, tzinfo=dt.UTC), test=True
    )
    assert result.coverage == "partial"
    assert result.incomplete_pages == ("orders.nodes[0].lineItems",)
    variables = json.loads(api.requests[-1].content)["variables"]
    assert variables["filter"] == 'created_at:>="2026-10-01T00:00:00+00:00" AND created_at:<"2026-10-02T00:00:00+00:00" AND test:true'
    assert result.data["orders"]["nodes"][0]["currentTotalPriceSet"]["shopMoney"]["amount"] == "111.10"


async def test_product_content_and_nullable_sku_survive_projection() -> None:
    product = _product()
    product["variants"]["pageInfo"] = {"hasNextPage": True, "endCursor": "variant-cursor"}
    api = RecordingApi([{"data": {"products": {"nodes": [product], "pageInfo": {"hasNextPage": True, "endCursor": "product-cursor"}}}}])
    result = await api.client().list_products(first=1, nested_first=1)
    assert result.data["products"]["nodes"][0] == product
    assert result.incomplete_pages == ("products", "products.nodes[0].variants")
    assert json.loads(api.requests[-1].content)["variables"] == {"first": 1, "after": None, "nestedFirst": 1}


async def test_missing_recent_history_does_not_become_an_empty_result() -> None:
    api = RecordingApi([], scopes="read_orders")
    with pytest.raises(ShopifyClientError, match="shopify_history_unavailable"):
        await api.client().list_orders(created_from=dt.datetime(2020, 1, 1, tzinfo=dt.UTC), created_until=dt.datetime(2020, 2, 1, tzinfo=dt.UTC))
    assert len(api.requests) == 1


async def test_missing_operation_scope_prevents_graphql_request() -> None:
    api = RecordingApi([], scopes="read_reports,read_customers")
    with pytest.raises(ShopifyClientError, match="shopify_scope_unavailable"):
        await api.client().list_products()
    assert len(api.requests) == 1


async def test_successful_empty_page_is_distinct_from_denied_access() -> None:
    api = RecordingApi([{"data": {"products": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}])
    result = await api.client().list_products()
    assert result.coverage == "complete_for_query"
    assert result.data["products"]["nodes"] == []


async def test_null_order_does_not_prove_absence_of_an_older_order() -> None:
    api = RecordingApi([{"data": {"order": None}}], scopes="read_orders")
    result = await api.client().read_order("gid://shopify/Order/1")
    assert result.data == {"order": None}
    assert result.coverage == "unknown"


@pytest.mark.parametrize("count", [0, 51, True])
async def test_invalid_page_size_fails_before_authentication(count: int) -> None:
    api = RecordingApi([])
    with pytest.raises(ShopifyClientError, match="shopify_page_limit_invalid"):
        await api.client().list_products(first=count)
    assert api.requests == []


@pytest.mark.parametrize(
    "domain", ["https://fixture-shop.myshopify.com", "fixture-shop.myshopify.com.attacker.test", "fixture-shop.myshopify.com/path"]
)
def test_shop_endpoint_cannot_be_overridden(domain: str) -> None:
    with pytest.raises(ShopifyClientError, match="shopify_shop_domain_invalid"):
        ShopifyCredential(domain, "fixture-client", "fixture-secret")


async def test_wrong_resource_id_cannot_select_a_customer() -> None:
    api = RecordingApi([])
    with pytest.raises(ShopifyClientError, match="shopify_object_id_invalid"):
        await api.client().read_order("gid://shopify/Customer/7")
    assert api.requests == []


async def test_graphql_denial_never_returns_partial_data_or_raw_error_text() -> None:
    api = RecordingApi(
        [{"data": {"order": _order()}, "errors": [{"message": "fixture-secret buyer@example.test", "extensions": {"code": "ACCESS_DENIED"}}]}]
    )
    with pytest.raises(ShopifyClientError, match="shopify_access_denied") as caught:
        await api.client().read_order("gid://shopify/Order/1")
    assert caught.value.params == {}
    assert "fixture-secret" not in repr(caught.value)
    assert "buyer@example.test" not in repr(caught.value)
    assert len(api.requests) == 2


async def test_unknown_api_version_cannot_be_marked_ready() -> None:
    api = RecordingApi([{"data": {"shop": _SHOP}}], version="2027-01")
    with pytest.raises(ShopifyClientError, match="shopify_api_version_unverified"):
        await api.client().read_shop()


async def test_cost_observations_are_projected_without_provider_details() -> None:
    api = RecordingApi(
        [
            {
                "data": {"shop": _SHOP},
                "extensions": {
                    "cost": {
                        "requestedQueryCost": 10,
                        "actualQueryCost": 2,
                        "email": "buyer@example.test",
                        "throttleStatus": {"maximumAvailable": 1000, "currentlyAvailable": 2**1000, "restoreRate": True, "customer": "Fixture buyer"},
                    }
                },
            }
        ]
    )
    result = await api.client().read_shop()
    assert result.cost == {"requestedQueryCost": 10, "actualQueryCost": 2, "throttleStatus": {"maximumAvailable": 1000}}


async def test_bad_money_and_excess_rows_fail_safely() -> None:
    order = _order()
    order["currentTotalPriceSet"]["shopMoney"]["amount"] = "NaN"
    api = RecordingApi(
        [{"data": {"order": order}}, {"data": {"products": {"nodes": [_product()] * 51, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}]
    )
    client = api.client()
    with pytest.raises(ShopifyClientError, match="shopify_response_invalid"):
        await client.read_order("gid://shopify/Order/1")
    with pytest.raises(ShopifyClientError, match="shopify_response_invalid"):
        await client.list_products()


async def test_token_is_cached_coalesced_and_renewed_after_expiry() -> None:
    api = RecordingApi([{"data": {"shop": _SHOP}}] * 4)
    client = api.client()
    async with anyio.create_task_group() as tasks:
        for _ in range(3):
            tasks.start_soon(client.read_shop)
    assert api.token_calls == 1
    assert json.loads(api.requests[0].content) == {
        "grant_type": "client_credentials",
        "client_id": "fixture-client",
        "client_secret": "fixture-secret",
    }
    client._refresh_at = 0
    await client.read_shop()
    assert api.token_calls == 2
    assert "fixture-secret" not in repr(client._credential)


async def test_throttled_graphql_reads_have_only_two_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    api = RecordingApi([{"errors": [{"extensions": {"code": "THROTTLED"}}]}] * 3)
    waits: list[float] = []

    async def record_wait(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(module.anyio, "sleep", record_wait)
    with pytest.raises(ShopifyClientError, match="shopify_throttled"):
        await api.client().read_shop()
    assert waits == [1.0, 1.0]
    assert len(api.requests) == 4


async def test_stream_limit_stops_before_materializing_an_oversized_response() -> None:
    class OversizedStream(httpx2.AsyncByteStream):
        chunks_read = 0

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(50):
                self.chunks_read += 1
                yield b"x" * 65536

    stream = OversizedStream()

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/access_token"):
            return httpx2.Response(200, json={"access_token": "fixture-access", "expires_in": 86399, "scope": "read_products"})
        return httpx2.Response(200, headers={"X-Shopify-API-Version": ADMIN_API_VERSION}, stream=stream)

    client = ShopifyClient(
        ShopifyCredential("fixture-shop.myshopify.com", "fixture-client", "fixture-secret"), transport=httpx2.MockTransport(handle)
    )
    with pytest.raises(ShopifyClientError, match="shopify_response_too_large"):
        await client.list_products()
    assert stream.chunks_read == 17


async def test_deadline_includes_token_exchange_and_the_response_read(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handle(request: httpx2.Request) -> httpx2.Response:
        await anyio.sleep_forever()
        raise AssertionError("The token exchange should have timed out.")

    monkeypatch.setattr(module, "CALL_TIMEOUT_SECONDS", 0.01)
    client = ShopifyClient(
        ShopifyCredential("fixture-shop.myshopify.com", "fixture-client", "fixture-secret"), transport=httpx2.MockTransport(handle)
    )
    with pytest.raises(ShopifyClientError, match="shopify_timeout"):
        await client.read_shop()


async def test_cancellation_reaches_the_external_request() -> None:
    entered = anyio.Event()
    cancelled = False

    async def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/access_token"):
            return httpx2.Response(200, json={"access_token": "fixture-access", "expires_in": 86399, "scope": "read_products"})
        entered.set()
        await anyio.sleep_forever()
        raise AssertionError("The external request should have been cancelled.")

    client = ShopifyClient(
        ShopifyCredential("fixture-shop.myshopify.com", "fixture-client", "fixture-secret"), transport=httpx2.MockTransport(handle)
    )

    async def read() -> None:
        nonlocal cancelled
        try:
            await client.list_products()
        except anyio.get_cancelled_exc_class():
            cancelled = True
            raise

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(read)
        await entered.wait()
        tasks.cancel_scope.cancel()
    assert cancelled
