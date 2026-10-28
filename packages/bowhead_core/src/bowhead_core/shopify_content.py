"""Bounded content proposals and owner-confirmed Shopify updates."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bowhead_core.delivery_confirmation import DeliveryConfirmations
from bowhead_core.llm_config_crypto import decrypt_llm_config, encrypt_llm_config
from bowhead_core.shopify_client import ShopifyClient, ShopifyClientError

PREVIEW_TTL_SECONDS = 900
CONTENT_OPERATIONS = frozenset({"shopify_read_content", "shopify_preview_content_update", "shopify_apply_content_update"})


def content_writes_enabled() -> bool:
    import os

    return os.environ.get("FEATURE_SHOPIFY_CONTENT_WRITES", "0").strip().lower() in {"1", "true", "yes", "on"}


class _StaticHTML(HTMLParser):
    """Reject active content instead of silently changing the proposed markup."""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {
            "p",
            "div",
            "span",
            "br",
            "hr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "strong",
            "b",
            "em",
            "i",
            "u",
            "s",
            "ul",
            "ol",
            "li",
            "blockquote",
            "a",
            "img",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
            "caption",
            "sup",
            "sub",
        }:
            raise ValueError("Content must use supported static HTML elements.")
        for name, value in attrs:
            if name not in {"href", "src", "alt", "title", "width", "height", "colspan", "rowspan", "class"}:
                raise ValueError("Content contains an unsupported HTML attribute.")
            if name in {"href", "src"}:
                address = re.sub(r"[\x00-\x20]", "", value or "")
                if urlsplit(address).scheme.lower() not in {"", "http", "https", "mailto", "tel"}:
                    raise ValueError("Content contains an active URL scheme.")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_decl(self, decl: str) -> None:
        raise ValueError("Content declarations are not supported.")


class ContentTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    resource: Literal["product", "page"]
    object_id: str = Field(max_length=160)

    @model_validator(mode="after")
    def validate_identity(self) -> ContentTarget:
        kind = "Product" if self.resource == "product" else "Page"
        if not re.fullmatch(rf"gid://shopify/{kind}/[0-9]+", self.object_id):
            raise ValueError("The Shopify object ID must match the content resource.")
        return self


class ContentChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description_html: str | None = Field(default=None, max_length=50000)
    body_html: str | None = Field(default=None, max_length=50000)
    seo_title: str | None = Field(default=None, max_length=255)
    seo_description: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def validate_values(self) -> ContentChanges:
        if not self.model_fields_set or any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Provide at least one content change; explicit null is not supported.")
        for name in self.model_fields_set:
            value = getattr(self, name)
            if "\x00" in value or len(value.encode("utf-8")) > (50000 if name.endswith("_html") else 20000):
                raise ValueError("Content exceeds the supported UTF-8 byte limit.")
            if name.endswith("_html"):
                parser = _StaticHTML(convert_charrefs=True)
                parser.feed(value)
                parser.close()
            elif name == "title" and (not value.strip() or "<" in value or "\n" in value or "\r" in value):
                raise ValueError("The title must be nonempty plain text.")
        return self

    def values(self) -> dict[str, str]:
        return self.model_dump(exclude_unset=True)


class ContentPreview(ContentTarget):
    changes: ContentChanges

    @model_validator(mode="after")
    def validate_fields(self) -> ContentPreview:
        allowed = {"title", "description_html", "seo_title", "seo_description"} if self.resource == "product" else {"title", "body_html"}
        if self.changes.model_fields_set - allowed:
            raise ValueError("The selected content fields do not belong to this resource.")
        return self


class ContentApply(ContentPreview):
    review_token: str = Field(min_length=1, max_length=8192)


class ShopifyContentConfirmations(DeliveryConfirmations):
    def __init__(self, *, timeout_seconds: float = 180) -> None:
        super().__init__(id_prefix="scc", timeout_seconds=timeout_seconds)
        self._claimed: dict[str, int] = {}

    def claim(self, token: str, expires_at: int) -> None:
        """Reject same-process proposal replay; this is not a durable ledger."""
        now = time.time()
        self._claimed = {key: expiry for key, expiry in self._claimed.items() if expiry > now}
        key = hashlib.sha256(token.encode()).hexdigest()
        if key in self._claimed or len(self._claimed) >= 2048:
            raise ShopifyClientError("shopify_review_consumed")
        self._claimed[key] = expires_at


_default = ShopifyContentConfirmations()


def default_shopify_content_confirmations() -> ShopifyContentConfirmations:
    return _default


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _editable(obj: dict[str, Any], resource: str) -> dict[str, Any]:
    if resource == "page":
        return {"title": obj["title"], "body_html": obj["body"]}
    return {
        "title": obj["title"],
        "description_html": obj["descriptionHtml"],
        "seo_title": obj["seo"]["title"],
        "seo_description": obj["seo"]["description"],
    }


def _mutation(changes: dict[str, str]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in changes.items():
        if key.startswith("seo_"):
            result.setdefault("seo", {})[key.removeprefix("seo_")] = value
        else:
            result[{"description_html": "descriptionHtml", "body_html": "body"}.get(key, key)] = value
    return result


async def execute_content(
    client: ShopifyClient,
    operation: str,
    args: ContentTarget,
    *,
    binding: dict[str, Any],
    ctx: Any,
    connection: Any,
    reauthorize: Callable[[], Awaitable[None]],
) -> dict[str, Any]:
    result = await client.read_content(args.resource, args.object_id)
    obj = result.data[args.resource]
    await reauthorize()
    if obj is None:
        raise ShopifyClientError("shopify_content_unavailable")
    if obj["id"] != args.object_id:
        raise ShopifyClientError("shopify_content_unavailable")
    envelope = {
        "ok": True,
        "connection_id": connection.id,
        "connection_revision": connection.connection_revision,
        "shop_domain": result.shop_domain,
        "api_version": result.api_version,
        "fetched_at": result.fetched_at,
        "source_content_untrusted": True,
        "coverage": result.coverage,
    }
    if operation == "shopify_read_content":
        return {**envelope, "data": obj}
    assert isinstance(args, ContentPreview)
    changes = args.changes.values()
    before = _editable(obj, args.resource)
    if len(json.dumps(before, ensure_ascii=False).encode()) > 150000:
        raise ShopifyClientError("shopify_content_review_too_large")
    edits = [{"field": key, "before": before[key], "after": value} for key, value in changes.items() if before[key] != value]
    if not edits:
        raise ShopifyClientError("shopify_content_unchanged")
    snapshot = {
        "team_id": ctx.team_id,
        "connection_id": connection.id,
        "shop_domain": result.shop_domain,
        "object_id": args.object_id,
        "resource": args.resource,
        "title": obj["title"],
        "handle": obj["handle"],
        "state": obj.get("status", "published" if obj.get("isPublished") else "unpublished"),
        "baseline_updated_at": obj["updatedAt"],
        "edits": edits,
    }
    if operation == "shopify_preview_content_update":
        token = encrypt_llm_config({"binding": binding, "baseline": _digest(obj), "expires_at": int(time.time()) + PREVIEW_TTL_SECONDS})
        if not token.startswith("FERNET:"):
            raise ShopifyClientError("shopify_credential_unavailable")
        return {**envelope, "preview": snapshot, "review_token": token, "status": "review_ready", "write_performed": False}
    assert isinstance(args, ContentApply)
    if not ctx.config_lite_supported or not ctx.session_id:
        raise ShopifyClientError("shopify_content_web_only")
    try:
        if not args.review_token.startswith("FERNET:"):
            raise ValueError("Invalid review token")
        review = decrypt_llm_config(args.review_token)
        if review.get("binding") != binding or type(review.get("expires_at")) is not int or review["expires_at"] <= time.time():
            raise ValueError("Review expired or changed")
    except (ValueError, TypeError, KeyError):
        raise ShopifyClientError("shopify_review_invalid") from None
    if review.get("baseline") != _digest(obj):
        raise ShopifyClientError("shopify_content_changed")
    queue = default_shopify_content_confirmations()
    queue.claim(args.review_token, review["expires_at"])
    decision = await queue.request_snapshot(
        owner_user_id=ctx.user_id,
        turn_id=ctx.turn_id,
        session_id=ctx.session_id,
        payload=snapshot,
        timeout_seconds=review["expires_at"] - time.time(),
    )
    if decision != "approved":
        raise ShopifyClientError(f"shopify_content_confirmation_{decision}")
    await reauthorize()
    if review["expires_at"] <= time.time():
        raise ShopifyClientError("shopify_review_invalid")
    fresh = await client.read_content(args.resource, args.object_id)
    if fresh.data[args.resource] != obj:
        raise ShopifyClientError("shopify_content_changed")
    await reauthorize()
    # Shopify's content mutations have no revision precondition. This narrows the
    # race window; it cannot make the provider update compare-and-swap atomic.
    updated = await client.update_content(args.resource, args.object_id, _mutation(changes))
    try:
        verified = await client.read_content(args.resource, args.object_id)
        await reauthorize()
    except Exception:
        return {
            "ok": False,
            "error_code": "shopify_write_outcome_unknown",
            "error_kind": "shopify_write_outcome_unknown",
            "write_dispatched": True,
            "retry_allowed": False,
        }
    expected = {**before, **changes}
    actual = verified.data[args.resource]
    retained = ("id", "handle", "status") if args.resource == "product" else ("id", "handle", "isPublished", "publishedAt")
    matched = actual is not None and _editable(actual, args.resource) == expected and all(actual[key] == obj[key] for key in retained)
    if not matched or updated.data[args.resource]["id"] != args.object_id:
        return {
            "ok": False,
            "error_code": "shopify_content_verification_failed",
            "error_kind": "shopify_content_verification_failed",
            "write_dispatched": True,
            "retry_allowed": False,
        }
    return {
        **envelope,
        "fetched_at": verified.fetched_at,
        "status": "updated_and_verified",
        "write_dispatched": True,
        "retry_allowed": False,
        "data": actual,
    }
