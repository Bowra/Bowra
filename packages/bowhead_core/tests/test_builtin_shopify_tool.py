"""Shopify Tools preserve privacy, connection authority and complete totals."""

from __future__ import annotations

import copy
import dataclasses
import json
from types import SimpleNamespace

import httpx2
import pytest
from bowhead_core.builtin_tool_providers import active_by_provider
from bowhead_core.llm_config_crypto import encrypt_llm_config
from bowhead_core.shopify_client import ShopifyClient
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_core.user_store import TeamBuiltinTool
from cryptography.fernet import Fernet
from test_shopify_client import _SHOP, RecordingApi, _money, _order, _product

from bowhead_core import builtin_shopify_tool as module

pytestmark = pytest.mark.unit


class Store:
    def __init__(self, rows):
        self.rows = {row.id: row for row in rows}
        self.member = True
        self.logged = []

    async def user_has_active_team_membership(self, user_id, team_id):
        return self.member and user_id == "user_1" and team_id == "team_1"

    async def get_team_builtin_tool_instance(self, team_id, tool_id):
        row = self.rows.get(tool_id)
        return copy.deepcopy(row) if row and row.team_id == team_id else None

    async def log_tool_call(self, **kwargs):
        self.logged.append(kwargs)


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("FEATURE_SHOPIFY_PLUGIN", "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    scopes = "read_products,read_orders,read_all_orders,read_inventory,read_locations,read_content"
    rows = [
        TeamBuiltinTool(
            id=tool_id,
            team_id="team_1",
            provider="shopify",
            display_name=tool_id,
            endpoint="",
            status="enabled",
            auth_ref=encrypt_llm_config({"client_id": "client", "client_secret": f"secret-{tool_id}"}),
            config={"shop_domain": f"{tool_id.replace('_', '-')}.myshopify.com", "granted_scopes": scopes.split(",")},
        )
        for tool_id in ("ibt_one", "ibt_two")
    ]
    store = Store(rows)
    ctx = ToolExecutionContext(workspace_id="ws_1", member_uid="", user_id="user_1", turn_id="turn_1", datarc=None, team_id="team_1")
    return SimpleNamespace(rows=rows, store=store, ctx=ctx, scopes=scopes)


def specs(setup):
    return {
        spec.schema.name: spec
        for spec in module.build_builtin_shopify_specs(team_id="team_1", tools=setup.rows, store=setup.store, base_registry=None)
    }


def transport(monkeypatch, api):
    monkeypatch.setattr(module, "ShopifyClient", lambda credential: ShopifyClient(credential, transport=httpx2.MockTransport(api.handle)))


async def test_connection_is_exact_and_mutations_have_no_schema(setup, monkeypatch):
    api = RecordingApi([{"data": {"shop": _SHOP}}, {"data": {"shop": _SHOP}}], scopes=setup.scopes)
    transport(monkeypatch, api)
    view = specs(setup)
    for tool_id in ("ibt_one", "ibt_two"):
        result = await view[f"teammcp.{tool_id}.shopify_shop_context"].handler({}, setup.ctx)
        assert result["ok"]
        assert result["connection_id"] == tool_id
        assert f"secret-{tool_id}" not in repr(result)
    assert {request.url.host for request in api.requests} == {"ibt-one.myshopify.com", "ibt-two.myshopify.com"}
    assert all(spec.annotations.read_only and not spec.annotations.destructive for spec in view.values())
    assert all("graphql" not in spec.schema.input_schema["properties"] for spec in view.values())
    assert all("shopifyql" not in name for name in view)
    assert len(setup.store.logged) == 2
    assert all("secret" not in repr(row) and "args" not in row for row in setup.store.logged)


@pytest.mark.parametrize("boundary", ["team", "member", "disabled", "revision", "feature"])
async def test_revoked_authority_stops_before_external_call(setup, monkeypatch, boundary):
    api = RecordingApi([], scopes=setup.scopes)
    transport(monkeypatch, api)
    spec = specs(setup)["teammcp.ibt_one.shopify_shop_context"]
    if boundary == "team":
        setup.ctx.team_id = "team_other"
    elif boundary == "member":
        setup.store.member = False
    elif boundary == "disabled":
        setup.store.rows["ibt_one"].status = "disabled"
    elif boundary == "revision":
        setup.store.rows["ibt_one"].connection_revision += 1
    else:
        monkeypatch.setenv("FEATURE_SHOPIFY_PLUGIN", "0")
        assert not active_by_provider(setup.rows, None)
    assert not (await spec.handler({}, setup.ctx))["ok"]
    assert not api.requests


@pytest.mark.parametrize("boundary", ["disable", "rotate", "member", "delete"])
async def test_revocation_during_request_discards_source_data(setup, monkeypatch, boundary):
    api = RecordingApi([{"data": {"shop": _SHOP}}], scopes=setup.scopes)
    original = api.handle

    async def handle(request):
        response = await original(request)
        if request.url.path.endswith("graphql.json"):
            if boundary == "disable":
                setup.store.rows["ibt_one"].status = "disabled"
            elif boundary == "rotate":
                setup.store.rows["ibt_one"].connection_revision += 1
            elif boundary == "member":
                setup.store.member = False
            else:
                del setup.store.rows["ibt_one"]
        return response

    api.handle = handle
    transport(monkeypatch, api)
    result = await specs(setup)["teammcp.ibt_one.shopify_shop_context"].handler({}, setup.ctx)
    assert result == {"ok": False, "error_code": "shopify_connection_changed", "error_kind": "shopify_connection_changed"}
    assert "Fixture shop" not in repr(result)


async def test_continuations_bind_turn_member_connection_operation_and_filters(setup, monkeypatch):
    product = _product()
    product["variants"]["pageInfo"] = {"hasNextPage": True, "endCursor": "variant-cursor"}
    api = RecordingApi(
        [
            {"data": {"products": {"nodes": [product], "pageInfo": {"hasNextPage": True, "endCursor": "outer-cursor"}}}},
            {"data": {"product": {"id": product["id"], "variants": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}},
        ],
        scopes=setup.scopes,
    )
    transport(monkeypatch, api)
    view = specs(setup)
    first = await view["teammcp.ibt_one.shopify_list_products"].handler({}, setup.ctx)
    assert first["coverage"] == "partial"
    assert "outer-cursor" not in repr(first) and "variant-cursor" not in repr(first)
    outer, child = first["continuations"]
    for target, args, ctx in [
        ("teammcp.ibt_two.shopify_list_products", outer["arguments"], setup.ctx),
        (outer["operation"], {**outer["arguments"], "first": 1}, setup.ctx),
        (outer["operation"], outer["arguments"], dataclasses.replace(setup.ctx, turn_id="turn_other")),
        (outer["operation"], outer["arguments"], dataclasses.replace(setup.ctx, workspace_id="ws_other")),
        ("teammcp.ibt_one.shopify_list_locations", outer["arguments"], setup.ctx),
        (outer["operation"], {**outer["arguments"], "continuation": json.dumps({"cursor": "forged"})}, setup.ctx),
    ]:
        result = await view[target].handler(args, ctx)
        assert result["error_code"] == "shopify_continuation_invalid"
    assert len(api.requests) == 2
    next_page = await view[child["operation"]].handler(child["arguments"], setup.ctx)
    assert next_page["ok"]
    sent = json.loads(api.requests[-1].content)["variables"]
    assert sent["id"] == product["id"] and sent["after"] == "variant-cursor"


async def test_summary_keeps_decimal_currency_and_timezone_semantics(setup, monkeypatch):
    first = _order()
    first.update(test=False, createdAt="2026-10-01T02:00:00Z", currentTotalPriceSet=_money("0.10"))
    second = copy.deepcopy(first)
    second.update(id="gid://shopify/Order/2", currentTotalPriceSet=_money("0.20"))
    cancelled = copy.deepcopy(second)
    cancelled.update(id="gid://shopify/Order/3", cancelledAt="2026-10-02T01:00:00Z")
    api = RecordingApi(
        [
            {"data": {"shop": _SHOP}},
            {"data": {"orders": {"nodes": [first, second, cancelled], "pageInfo": {"hasNextPage": False, "endCursor": None}}}},
        ],
        scopes=setup.scopes,
    )
    transport(monkeypatch, api)
    result = await specs(setup)["teammcp.ibt_one.shopify_order_summary"].handler(
        {"created_from": "2026-09-30T00:00:00-04:00", "created_until": "2026-10-03T00:00:00-04:00"}, setup.ctx
    )
    assert result["ok"], result
    assert result["data"]["by_currency"]["USD"]["current_order_value"] == "0.30"
    assert result["data"]["by_currency"]["USD"]["average_current_order_value"] == "0.15"
    assert result["data"]["daily_order_counts"] == {"2026-09-30": 2}
    assert result["data"]["scanned_orders"] == 3
    query = json.loads(api.requests[-1].content)
    assert "test:false" in query["variables"]["filter"]
    assert "lineItems" not in query["query"]


async def test_summary_budget_never_returns_partial_totals(setup, monkeypatch):
    monkeypatch.setattr(module, "SUMMARY_PAGE_LIMIT", 1)
    order = _order()
    order["test"] = False
    api = RecordingApi(
        [{"data": {"shop": _SHOP}}, {"data": {"orders": {"nodes": [order], "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}}],
        scopes=setup.scopes,
    )
    transport(monkeypatch, api)
    result = await specs(setup)["teammcp.ibt_one.shopify_order_summary"].handler(
        {"created_from": "2026-10-01T00:00:00Z", "created_until": "2026-10-03T00:00:00Z"}, setup.ctx
    )
    assert result["error_code"] == "shopify_summary_budget_exceeded"
    assert "by_currency" not in result


@pytest.mark.parametrize("resource", ["inventory", "locations", "collections", "pages", "articles"])
async def test_object_families_use_reviewed_queries(resource):
    from bowhead_core.shopify_objects import RESOURCE_READS

    root = {"inventory": "inventoryItems"}.get(resource, resource)
    api = RecordingApi(
        [{"data": {root: {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}],
        scopes="read_inventory,read_locations,read_content,read_products",
    )
    result = await api.client().list_resource(resource)
    assert result.coverage == "complete_for_query"
    query = json.loads(api.requests[-1].content)["query"]
    assert query == RESOURCE_READS[resource][0]
    assert not any(field in query for field in ("customer", "address", "author", "comments", "unitCost"))


async def test_inventory_projection_excludes_location_address_and_unknown_stock_fields():
    api = RecordingApi(
        [
            {
                "data": {
                    "inventoryItems": {
                        "nodes": [
                            {
                                "id": "gid://shopify/InventoryItem/1",
                                "sku": "SKU",
                                "tracked": False,
                                "inventoryLevels": {
                                    "nodes": [
                                        {
                                            "id": "gid://shopify/InventoryLevel/1?inventory_item_id=1",
                                            "location": {
                                                "id": "gid://shopify/Location/1",
                                                "name": "Warehouse",
                                                "isActive": True,
                                                "address": {"phone": "private"},
                                            },
                                            "quantities": [{"name": "available", "quantity": -2, "customer": "private"}],
                                        }
                                    ],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                },
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        ],
        scopes="read_inventory",
    )
    result = await api.client().list_resource("inventory")
    assert "private" not in repr(result.data)
    assert result.data["inventoryItems"]["nodes"][0]["tracked"] is False


async def test_summary_duplicate_pages_are_rejected(setup, monkeypatch):
    order = _order()
    order["test"] = False
    api = RecordingApi(
        [
            {"data": {"shop": _SHOP}},
            *[{"data": {"orders": {"nodes": [order], "pageInfo": {"hasNextPage": True, "endCursor": str(index)}}}} for index in range(2)],
        ],
        scopes=setup.scopes,
    )
    transport(monkeypatch, api)
    result = await specs(setup)["teammcp.ibt_one.shopify_order_summary"].handler(
        {"created_from": "2026-10-01T00:00:00Z", "created_until": "2026-10-03T00:00:00Z"}, setup.ctx
    )
    assert result["error_code"] == "shopify_coverage_changed"


async def test_resource_grant_revocation_discards_inflight_result(setup, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    allowed = True

    class AccessStore:
        async def explain_decision(self, _ctx, _kind, _id, _permission):
            return {"allowed": allowed, "reason": "allowed" if allowed else "explicit_deny"}

    setup.ctx.access_control_store = AccessStore()
    setup.ctx.access_context = object()
    api = RecordingApi([{"data": {"shop": _SHOP}}], scopes=setup.scopes)
    original = api.handle

    async def handle(request):
        nonlocal allowed
        response = await original(request)
        if request.url.path.endswith("graphql.json"):
            allowed = False
        return response

    api.handle = handle
    transport(monkeypatch, api)
    result = await specs(setup)["teammcp.ibt_one.shopify_shop_context"].handler({}, setup.ctx)
    assert result["error_code"] == "shopify_permission_denied"
    assert "data" not in result


@pytest.mark.parametrize("boundary", ["store", "context"])
async def test_enforced_resource_grants_require_complete_authority(setup, monkeypatch, boundary):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    setup.ctx.access_control_store = None if boundary == "store" else object()
    setup.ctx.access_context = None if boundary == "context" else object()
    api = RecordingApi([], scopes=setup.scopes)
    transport(monkeypatch, api)
    result = await specs(setup)["teammcp.ibt_one.shopify_shop_context"].handler({}, setup.ctx)
    assert result["error_code"] == "shopify_permission_denied"
    assert not api.requests
