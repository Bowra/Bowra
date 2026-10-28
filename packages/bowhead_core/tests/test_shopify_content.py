"""Shopify content changes require exact review and verified single dispatch."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json

import anyio
import httpx2
import pytest
import test_builtin_shopify_tool as read_tool_tests
from pydantic import ValidationError
from test_builtin_shopify_tool import specs, transport
from test_shopify_client import RecordingApi as ReadRecordingApi

from bowhead_core import shopify_content as content

pytestmark = pytest.mark.unit
read_setup = read_tool_tests.setup


class RecordingApi(ReadRecordingApi):
    async def handle(self, request):
        if not request.url.path.endswith("/access_token") and isinstance(self.payloads[0], tuple):
            self.requests.append(request)
            status, payload = self.payloads.pop(0)
            return httpx2.Response(status, headers={"X-Shopify-API-Version": self.version}, json=payload)
        return await super().handle(request)


@pytest.fixture
def setup(read_setup, monkeypatch):
    monkeypatch.setenv("FEATURE_SHOPIFY_CONTENT_WRITES", "1")
    read_setup.scopes += ",write_products,write_content"
    for row in read_setup.rows:
        row.config["granted_scopes"] = read_setup.scopes.split(",")
    read_setup.ctx.config_lite_supported = True
    read_setup.ctx.session_id = "s_1"
    read_setup.store.space_role = "member"
    read_setup.store.space = {"team_id": "team_1", "status": "active"}

    async def get_workspace(_workspace_id):
        return read_setup.store.space

    async def get_member_role(_workspace_id, _user_id):
        return read_setup.store.space_role

    read_setup.store.get_workspace = get_workspace
    read_setup.store.get_member_role = get_member_role
    monkeypatch.setattr(content, "_default", content.ShopifyContentConfirmations(timeout_seconds=0.5))
    return read_setup


def obj(resource="product"):
    base = {
        "id": f"gid://shopify/{'Product' if resource == 'product' else 'Page'}/1",
        "title": "Original",
        "handle": "original",
        "updatedAt": "2026-10-09T10:00:00Z",
    }
    if resource == "product":
        base.update(descriptionHtml="<p>Original copy</p>", status="ACTIVE", seo={"title": None, "description": "Original metadata"})
    else:
        base.update(body="<p>Original copy</p>", isPublished=False, createdAt="2026-10-01T00:00:00Z", publishedAt=None)
    return base


def args(resource="product"):
    return {
        "resource": resource,
        "object_id": obj(resource)["id"],
        "changes": {"description_html" if resource == "product" else "body_html": "<p>Improved copy</p>"},
    }


def updated(resource="product"):
    value = obj(resource)
    value.update({"descriptionHtml" if resource == "product" else "body": "<p>Improved copy</p>", "updatedAt": "2026-10-09T10:01:00Z"})
    return value


def reply(resource, value):
    return {"data": {resource: copy.deepcopy(value)}}


def mutation_reply(resource, value):
    return {"data": {resource + "Update": {resource: copy.deepcopy(value), "userErrors": []}}}


async def wait_pending():
    with anyio.fail_after(2):
        while not (items := content.default_shopify_content_confirmations().pending("user_1")):
            await anyio.sleep(0.001)
    return items[0]


def mutation_requests(api):
    return [request for request in api.requests if json.loads(request.content).get("query", "").startswith("mutation")]


async def preview(setup, view, resource="product"):
    result = await view["teammcp.ibt_one.shopify_preview_content_update"].handler(args(resource), setup.ctx)
    assert result["ok"], result
    assert result["write_performed"] is False
    return {**args(resource), "review_token": result["review_token"]}


@pytest.mark.parametrize("resource", ["product", "page"])
async def test_review_then_exact_partial_update_and_readback_preserve_other_fields(setup, monkeypatch, resource):
    original, changed = obj(resource), updated(resource)
    api = RecordingApi([reply(resource, original)] * 3 + [mutation_reply(resource, changed), reply(resource, changed)], scopes=setup.scopes)
    transport(monkeypatch, api)
    view = specs(setup)
    change = await preview(setup, view, resource)
    waiting = asyncio.create_task(view["teammcp.ibt_one.shopify_apply_content_update"].handler(change, setup.ctx))
    item = await wait_pending()
    assert not mutation_requests(api)
    assert item["shop_domain"] == "ibt-one.myshopify.com"
    assert item["edits"] == [{"field": next(iter(args(resource)["changes"])), "before": "<p>Original copy</p>", "after": "<p>Improved copy</p>"}]
    assert not content.default_shopify_content_confirmations().decide("other_user", item["id"], True)
    assert content.default_shopify_content_confirmations().decide("user_1", item["id"], True)
    result = await waiting
    assert result["status"] == "updated_and_verified", result
    assert result["data"] == changed
    assert len(mutation_requests(api)) == 1
    variables = json.loads(mutation_requests(api)[0].content)["variables"]
    assert variables["input"] == (
        {"id": original["id"], "descriptionHtml": "<p>Improved copy</p>"} if resource == "product" else {"body": "<p>Improved copy</p>"}
    )
    assert not view["teammcp.ibt_one.shopify_apply_content_update"].annotations.read_only
    assert view["teammcp.ibt_one.shopify_preview_content_update"].annotations.read_only


@pytest.mark.parametrize("decision", ["reject", "expire", "cancel"])
async def test_unapproved_change_never_dispatches(setup, monkeypatch, decision):
    api = RecordingApi([reply("product", obj())] * 2, scopes=setup.scopes)
    transport(monkeypatch, api)
    view = specs(setup)
    change = await preview(setup, view)
    waiting = asyncio.create_task(view["teammcp.ibt_one.shopify_apply_content_update"].handler(change, setup.ctx))
    item = await wait_pending()
    if decision == "cancel":
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    else:
        if decision == "reject":
            assert content.default_shopify_content_confirmations().decide("user_1", item["id"], False)
        result = await waiting
        assert result["error_code"] == "shopify_content_confirmation_" + ("rejected" if decision == "reject" else "expired")
    assert not mutation_requests(api)
    assert content.default_shopify_content_confirmations().pending("user_1") == []


@pytest.mark.parametrize(
    "boundary", ["changed", "rotation", "membership", "feature", "grants", "space_removed", "space_viewer", "space_team", "space_archived"]
)
async def test_authority_and_baseline_rechecked_after_confirmation(setup, monkeypatch, boundary):
    newer = obj()
    newer["updatedAt"] = "2026-10-09T10:00:01Z"
    api = RecordingApi([reply("product", obj())] * 2 + [reply("product", newer if boundary == "changed" else obj())], scopes=setup.scopes)
    transport(monkeypatch, api)
    allowed = True
    if boundary == "grants":
        monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")

        class AccessStore:
            async def explain_decision(self, *_args):
                return {"allowed": allowed, "reason": "fixture"}

        setup.ctx.access_control_store = AccessStore()
        setup.ctx.access_context = object()
    view = specs(setup)
    change = await preview(setup, view)
    waiting = asyncio.create_task(view["teammcp.ibt_one.shopify_apply_content_update"].handler(change, setup.ctx))
    item = await wait_pending()
    if boundary == "rotation":
        setup.store.rows["ibt_one"].connection_revision += 1
    elif boundary == "membership":
        setup.store.member = False
    elif boundary == "feature":
        monkeypatch.setenv("FEATURE_SHOPIFY_CONTENT_WRITES", "0")
    elif boundary == "grants":
        allowed = False
    elif boundary == "space_removed":
        setup.store.space_role = None
    elif boundary == "space_viewer":
        setup.store.space_role = "viewer"
    elif boundary == "space_team":
        setup.store.space["team_id"] = "other_team"
    elif boundary == "space_archived":
        setup.store.space["status"] = "archived"
    content.default_shopify_content_confirmations().decide("user_1", item["id"], True)
    result = await waiting
    assert not result["ok"]
    assert not mutation_requests(api)


@pytest.mark.parametrize("boundary", ["changes", "turn", "space", "shop", "forged", "expired"])
async def test_review_cannot_be_transferred_or_rewritten(setup, monkeypatch, boundary):
    from bowhead_core.llm_config_crypto import decrypt_llm_config, encrypt_llm_config

    api = RecordingApi([reply("product", obj())] * 2, scopes=setup.scopes)
    transport(monkeypatch, api)
    view = specs(setup)
    change = await preview(setup, view)
    ctx = setup.ctx
    ref = "teammcp.ibt_one.shopify_apply_content_update"
    if boundary == "changes":
        change["changes"] = {"title": "Unreviewed title"}
    elif boundary == "turn":
        ctx = dataclasses.replace(ctx, turn_id="t_other")
    elif boundary == "space":
        ctx = dataclasses.replace(ctx, workspace_id="ws_other")
    elif boundary == "shop":
        ref = "teammcp.ibt_two.shopify_apply_content_update"
    elif boundary == "forged":
        change["review_token"] = "approved"
    else:
        decoded = decrypt_llm_config(change["review_token"])
        decoded["expires_at"] = 0
        change["review_token"] = encrypt_llm_config(decoded)
    result = await view[ref].handler(change, ctx)
    assert result["error_code"] == "shopify_review_invalid"
    assert not mutation_requests(api)
    assert not content.default_shopify_content_confirmations().pending("user_1")


async def test_unknown_write_is_not_retried_and_review_cannot_be_replayed(setup, monkeypatch):
    api = RecordingApi(
        [reply("product", obj())] * 3 + [(503, {"message": "private upstream details"})] + [reply("product", obj())], scopes=setup.scopes
    )
    transport(monkeypatch, api)
    view = specs(setup)
    change = await preview(setup, view)
    apply = view["teammcp.ibt_one.shopify_apply_content_update"]
    waiting = asyncio.create_task(apply.handler(change, setup.ctx))
    item = await wait_pending()
    content.default_shopify_content_confirmations().decide("user_1", item["id"], True)
    result = await waiting
    assert result["error_code"] == "shopify_write_outcome_unknown"
    assert result["retry_allowed"] is False
    assert "private" not in repr(result)
    assert len(mutation_requests(api)) == 1
    replay = await apply.handler(change, setup.ctx)
    assert replay["error_code"] == "shopify_review_consumed"
    assert len(mutation_requests(api)) == 1


async def test_post_write_access_failure_remains_unknown_not_a_zero_write(setup, monkeypatch):
    api = RecordingApi([reply("product", obj())] * 3 + [mutation_reply("product", updated()), (403, {})], scopes=setup.scopes)
    transport(monkeypatch, api)
    view = specs(setup)
    change = await preview(setup, view)
    waiting = asyncio.create_task(view["teammcp.ibt_one.shopify_apply_content_update"].handler(change, setup.ctx))
    item = await wait_pending()
    content.default_shopify_content_confirmations().decide("user_1", item["id"], True)
    result = await waiting
    assert result["error_code"] == "shopify_write_outcome_unknown"
    assert result["write_dispatched"] is True
    assert len(mutation_requests(api)) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"price": "1"},
        {"body_html": "x"},
        {"title": None},
        {"title": ""},
        {"description_html": '<img src="x" onerror="alert(1)">'},
        {"description_html": '<a href="java&#9;script:alert(1)">x</a>'},
        {"description_html": "<script>alert(1)</script>"},
        {"description_html": "界" * 20000},
    ],
)
def test_only_bounded_resource_specific_static_content_is_accepted(change):
    with pytest.raises(ValidationError):
        content.ContentPreview.model_validate({"resource": "product", "object_id": obj()["id"], "changes": change})


async def test_nonweb_and_missing_write_scope_cannot_start_confirmation(setup, monkeypatch):
    api = RecordingApi([], scopes=setup.scopes)
    transport(monkeypatch, api)
    spec = specs(setup)["teammcp.ibt_one.shopify_apply_content_update"]
    result = await spec.handler({**args(), "review_token": "opaque"}, dataclasses.replace(setup.ctx, config_lite_supported=False))
    assert result["error_code"] == "shopify_content_web_only"
    setup.store.rows["ibt_one"].config["granted_scopes"].remove("write_products")
    result = await spec.handler({**args(), "review_token": "opaque"}, setup.ctx)
    assert result["error_code"] == "shopify_scope_unavailable"
    assert not api.requests


@pytest.mark.parametrize("outcome", ["user_error", "readback_mismatch", "publication_changed", "seo_clear"])
async def test_provider_outcomes_and_exact_seo_fields_are_not_overstated(setup, monkeypatch, outcome):
    original, changed = obj(), updated()
    resource = "page" if outcome == "publication_changed" else "product"
    if resource == "page":
        original, changed = obj(resource), updated(resource)
        observed = copy.deepcopy(changed)
        observed["isPublished"] = True
    elif outcome == "seo_clear":
        changed = copy.deepcopy(original)
        changed["seo"]["description"] = ""
        changed["updatedAt"] = "2026-10-09T10:01:00Z"
        observed = changed
    else:
        observed = original if outcome == "readback_mismatch" else changed
    mutation = (
        {"data": {resource + "Update": {resource: None, "userErrors": [{"field": ["title"], "message": "untrusted-provider-details"}]}}}
        if outcome == "user_error"
        else mutation_reply(resource, changed)
    )
    api = RecordingApi([reply(resource, original)] * 3 + [mutation, reply(resource, observed)], scopes=setup.scopes)
    transport(monkeypatch, api)
    view = specs(setup)
    parameters = args(resource)
    if outcome == "seo_clear":
        parameters["changes"] = {"seo_description": ""}
    proposal = await view["teammcp.ibt_one.shopify_preview_content_update"].handler(parameters, setup.ctx)
    waiting = asyncio.create_task(
        view["teammcp.ibt_one.shopify_apply_content_update"].handler({**parameters, "review_token": proposal["review_token"]}, setup.ctx)
    )
    item = await wait_pending()
    content.default_shopify_content_confirmations().decide("user_1", item["id"], True)
    result = await waiting
    assert len(mutation_requests(api)) == 1
    assert "untrusted-provider-details" not in repr(result)
    if outcome == "user_error":
        assert result["error_code"] == "shopify_content_rejected"
    elif outcome == "seo_clear":
        assert result["status"] == "updated_and_verified"
        assert result["data"]["seo"]["title"] is None
        assert json.loads(mutation_requests(api)[0].content)["variables"]["input"] == {"id": original["id"], "seo": {"description": ""}}
    else:
        assert result["error_code"] == "shopify_content_verification_failed"
        assert result["write_dispatched"] is True and result["retry_allowed"] is False


async def test_mismatched_provider_object_cannot_be_reviewed_or_updated(setup, monkeypatch):
    wrong = obj()
    wrong["id"] = "gid://shopify/Product/999"
    api = RecordingApi([reply("product", wrong)], scopes=setup.scopes)
    transport(monkeypatch, api)
    result = await specs(setup)["teammcp.ibt_one.shopify_preview_content_update"].handler(args(), setup.ctx)
    assert result["error_code"] == "shopify_content_unavailable"
    assert not mutation_requests(api)
