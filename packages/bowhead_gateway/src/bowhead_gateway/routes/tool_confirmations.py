"""One owner-only view of every tool call waiting for its requester (``FEATURE_INLINE_TOOL_CONFIRMATION``).

GET  /api/v1/tool-confirmations                 — summaries of the caller's pending items, all kinds
GET  /api/v1/tool-confirmations/{id}            — one item with its kind-specific preview
POST /api/v1/tool-confirmations/{id}/decision   — approve once, approve for the session, or reject

The id prefix selects the existing queue (``pmi_`` personal MCP, ``esc_`` email, ``scc_``
Shopify, ``ttc_`` Team MCP) and each kind keeps its own checks: personal MCP license and
revalidation, the Shopify feature switches, team scope and read-only guard. The kind
endpoints stay until Release so a client with stale capabilities keeps working. Only the
member who started the task sees or decides; there is no administrator bypass.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any, Literal

from bowhead_core._db import open_pooled_read_ctx
from bowhead_core.builtin_shopify_tool import feature_enabled as shopify_enabled
from bowhead_core.email_send_confirmation import default_email_send_confirmations
from bowhead_core.personal_mcp import PersonalMcpError, PersonalMcpStore
from bowhead_core.shopify_content import content_writes_enabled, default_shopify_content_confirmations
from bowhead_core.team_tool_confirmation import default_team_tool_confirmations
from bowhead_core.user_store import User, UserStore
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from bowhead_gateway._entitlement_gate import assert_not_readonly, feature_license_checker
from bowhead_gateway.routes import personal_mcp as personal_mcp_routes
from bowhead_gateway.routes.auth import _current_user, _store

router = APIRouter()
logger = logging.getLogger(__name__)

Kind = Literal["personal_mcp", "email_send", "shopify_content", "team_mcp"]
_KINDS: dict[str, Kind] = {"pmi_": "personal_mcp", "esc_": "email_send", "scc_": "shopify_content", "ttc_": "team_mcp"}
_CHOICES: dict[Kind, list[str]] = {
    "personal_mcp": ["reject", "approve", "approve_session"],
    "email_send": ["reject", "approve"],
    "shopify_content": ["reject", "approve"],
    "team_mcp": ["reject", "approve", "approve_session"],
}
_NOT_FOUND = {"code": "tool_confirmation_not_found"}
_UNAVAILABLE = {"code": "tool_confirmation_unavailable"}


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "approve_session", "reject"]


def _kind(confirmation_id: str) -> Kind:
    kind = _KINDS.get(confirmation_id[:4])
    if kind is None:
        raise HTTPException(404, _NOT_FOUND)
    return kind


def _summary(kind: Kind, item: dict[str, Any]) -> dict[str, Any]:
    if kind == "personal_mcp":
        title, source, agent, category = item.get("tool_name"), item.get("connection_name"), item.get("agent_name"), "unknown"
    elif kind == "team_mcp":
        title, source, agent, category = item.get("tool_name"), item.get("source_name"), None, item.get("category") or "unknown"
    elif kind == "email_send":
        title, source, agent, category = item.get("subject"), item.get("to"), None, None
    else:
        title, source, agent, category = item.get("title") or item.get("object_id"), item.get("shop_domain"), None, None
    try:
        remaining = max(0, round((datetime.fromisoformat(str(item["expires_at"])) - datetime.now(UTC)).total_seconds()))
    except (KeyError, ValueError):
        remaining = 0
    return {
        "id": item["id"],
        "kind": kind,
        "session_id": item.get("session_id") or None,
        "session_title": None,
        "turn_id": item.get("turn_id") or None,
        "step_id": None,
        "title": str(title or "")[:120],
        "source_label": str(source)[:120] if source else None,
        "agent_name": agent or None,
        "expires_at": item.get("expires_at"),
        "remaining_seconds": remaining,
        "category": category,
        "choices": _CHOICES[kind],
    }


async def _items(kind: Kind, request: Request, user: User, store: UserStore) -> list[dict[str, Any]]:
    """The caller's pending items of one kind; an unavailable kind has none."""
    if kind == "team_mcp":
        return default_team_tool_confirmations().pending(user.id)
    if kind == "email_send":
        return default_email_send_confirmations().pending(user.id)
    if kind == "shopify_content":
        if not shopify_enabled() or not content_writes_enabled():
            return []
        return [item for item in default_shopify_content_confirmations().pending(user.id) if item["team_id"] == user.team_id]
    if not await feature_license_checker(request.app, "mcp")(user.team_id):
        return []
    try:
        return await PersonalMcpStore(store.db_path, license_check=feature_license_checker(request.app, "mcp")).pending(user.id)
    except PersonalMcpError:
        return []


async def _with_session_titles(store: UserStore, user: User, summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = sorted({item["session_id"] for item in summaries if item["session_id"]})
    if not ids:
        return summaries
    marks = ",".join("?" for _ in ids)
    query = f"SELECT id, title FROM sessions WHERE user_id=? AND id IN ({marks})"  # noqa: S608 - placeholders only
    try:
        async with open_pooled_read_ctx(store.db_path) as db, db.execute(query, (user.id, *ids)) as cursor:
            titles = {row[0]: row[1] for row in await cursor.fetchall()}
    except sqlite3.Error:
        # The title only labels the notice; the confirmation itself must still be listed.
        logger.warning("tool_confirmations.session_titles_unavailable", exc_info=True)
        titles = {}
    for item in summaries:
        item["session_title"] = titles.get(item["session_id"]) or None
    return summaries


async def _find(confirmation_id: str, request: Request, user: User, store: UserStore) -> tuple[Kind, dict[str, Any]]:
    kind = _kind(confirmation_id)
    item = next((item for item in await _items(kind, request, user, store) if item["id"] == confirmation_id), None)
    if item is None:
        raise HTTPException(404, _NOT_FOUND)
    return kind, item


@router.get("")
async def pending(request: Request, response: Response, user: User = Depends(_current_user), store: UserStore = Depends(_store)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    summaries: list[dict[str, Any]] = []
    for kind in ("personal_mcp", "email_send", "shopify_content", "team_mcp"):
        summaries.extend(_summary(kind, item) for item in await _items(kind, request, user, store))
    summaries.sort(key=lambda item: str(item["expires_at"]))
    for item in summaries:
        item.pop("category")
        item.pop("choices")
    return {"items": await _with_session_titles(store, user, summaries)}


@router.get("/{confirmation_id}")
async def detail(
    confirmation_id: str, request: Request, response: Response, user: User = Depends(_current_user), store: UserStore = Depends(_store)
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    kind, item = await _find(confirmation_id, request, user, store)
    (summary,) = await _with_session_titles(store, user, [_summary(kind, item)])
    return {**summary, "item": item}


@router.post("/{confirmation_id}/decision")
async def decide(
    confirmation_id: str, body: DecisionInput, request: Request, user: User = Depends(_current_user), store: UserStore = Depends(_store)
) -> dict[str, Any]:
    kind, item = await _find(confirmation_id, request, user, store)
    if body.decision not in _CHOICES[kind]:
        raise HTTPException(422, {"code": "tool_confirmation_choice_unavailable"})
    if kind == "personal_mcp":
        registry = PersonalMcpStore(store.db_path, license_check=feature_license_checker(request.app, "mcp"))
        try:
            await personal_mcp_routes.decide(confirmation_id, personal_mcp_routes.DecisionInput(decision=body.decision), user=user, registry=registry)
        except HTTPException as exc:
            if exc.status_code in {404, 409}:
                raise HTTPException(exc.status_code, _NOT_FOUND if exc.status_code == 404 else _UNAVAILABLE) from None
            raise
        return {"ok": True}
    if kind == "shopify_content":
        await assert_not_readonly(request)
        if datetime.fromisoformat(str(item["expires_at"])) <= datetime.now(UTC):
            raise HTTPException(409, _UNAVAILABLE)
        recorded = default_shopify_content_confirmations().decide(user.id, confirmation_id, body.decision == "approve")
    elif kind == "email_send":
        recorded = default_email_send_confirmations().decide(user.id, confirmation_id, body.decision == "approve")
    elif body.decision == "approve_session":
        recorded = default_team_tool_confirmations().approve_for_session(user.id, confirmation_id)
    else:
        recorded = default_team_tool_confirmations().decide(user.id, confirmation_id, body.decision == "approve")
    if not recorded:
        raise HTTPException(409, _UNAVAILABLE)
    return {"ok": True}
