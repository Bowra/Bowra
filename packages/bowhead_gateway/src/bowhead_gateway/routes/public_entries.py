"""Team public-entry administration and inbound ownership verification."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from bowhead_core.public_entries import (
    PublicEntry,
    PublicEntryError,
    PublicEntryStore,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import HTMLResponse

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.product_identity import product_name
from bowhead_gateway.public_entry_context import current_request_public_access_context
from bowhead_gateway.public_url import public_app_url_for_base
from bowhead_gateway.routes.auth import _require_team_role_dep

if TYPE_CHECKING:
    from bowhead_core.user_store import User

router = APIRouter(tags=["public-entries"])

_REQUIRE_PUBLIC_ENTRY_ADMIN = _require_team_role_dep("owner", "team_admin")


class PublicEntryView(BaseModel):
    id: str
    team_id: str
    base_url: str
    base_path: str
    is_default: bool
    is_current: bool = False
    match_source: str | None = None
    status: str
    verification_method: str
    verification_expires_at: str | None
    verified_at: str | None
    created_at: str
    updated_at: str


class PublicEntryCreate(BaseModel):
    base_url: str = Field(min_length=1, max_length=2048)


class PublicEntryChallenge(BaseModel):
    verification_url: str
    expires_at: str | None
    entry: PublicEntryView


class PublicEntryVerify(BaseModel):
    token: str = Field(min_length=32, max_length=256)


class PublicEntryDisable(BaseModel):
    new_default_entry_id: str | None = None
    disable_last: bool = False


def _store(request: Request) -> PublicEntryStore:
    store = getattr(request.app.state, "public_entry_store", None)
    if not isinstance(store, PublicEntryStore):
        raise HTTPException(503, "public entry registry unavailable")
    return store


def _entry_view(
    entry: PublicEntry,
    *,
    is_current: bool = False,
    match_source: str | None = None,
) -> PublicEntryView:
    return PublicEntryView(
        id=entry.id,
        team_id=entry.team_id,
        base_url=entry.base_url,
        base_path=entry.base_path,
        is_default=entry.is_default,
        is_current=is_current,
        match_source=match_source if is_current else None,
        status=entry.status,
        verification_method=entry.verification_method,
        verification_expires_at=entry.verification_expires_at,
        verified_at=entry.verified_at,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _challenge_view(entry: PublicEntry, token: str) -> PublicEntryChallenge:
    path = f"/.well-known/bowhead-public-entry/{entry.id}#{token}"
    return PublicEntryChallenge(
        verification_url=public_app_url_for_base(entry.base_url, path),
        expires_at=entry.verification_expires_at,
        entry=_entry_view(entry),
    )


def _public_error(exc: PublicEntryError) -> HTTPException:
    message = str(exc)
    status = 404 if "not found" in message else 409
    return HTTPException(status, message)


@router.get(
    "/api/v1/team-settings/public-entries",
    response_model=list[PublicEntryView],
)
async def list_public_entries(
    request: Request,
    user: User = Depends(_REQUIRE_PUBLIC_ENTRY_ADMIN),
) -> list[PublicEntryView]:
    store = _store(request)
    context = current_request_public_access_context()
    result = []
    for entry in await store.list_for_team(user.team_id):
        is_current = bool(context is not None and context.prefix_known and context.base_url == entry.base_url and entry.status == "active")
        result.append(
            _entry_view(
                entry,
                is_current=is_current,
                match_source=context.match_source if context is not None else None,
            ),
        )
    return result


@router.post(
    "/api/v1/team-settings/public-entries",
    response_model=PublicEntryChallenge,
    dependencies=[Depends(require_not_readonly)],
)
async def create_public_entry(
    body: PublicEntryCreate,
    request: Request,
    user: User = Depends(_REQUIRE_PUBLIC_ENTRY_ADMIN),
) -> PublicEntryChallenge:
    try:
        entry, token = await _store(request).create_pending(
            team_id=user.team_id,
            base_url=body.base_url,
            actor_id=user.id,
        )
    except PublicEntryError as exc:
        raise _public_error(exc) from exc
    return _challenge_view(entry, token)


@router.post(
    "/api/v1/team-settings/public-entries/{entry_id}/challenge",
    response_model=PublicEntryChallenge,
    dependencies=[Depends(require_not_readonly)],
)
async def reissue_public_entry_challenge(
    entry_id: str,
    request: Request,
    user: User = Depends(_REQUIRE_PUBLIC_ENTRY_ADMIN),
) -> PublicEntryChallenge:
    try:
        entry, token = await _store(request).reissue_challenge(
            team_id=user.team_id,
            entry_id=entry_id,
            actor_id=user.id,
        )
    except PublicEntryError as exc:
        raise _public_error(exc) from exc
    return _challenge_view(entry, token)


@router.put(
    "/api/v1/team-settings/public-entries/{entry_id}/default",
    response_model=PublicEntryView,
    dependencies=[Depends(require_not_readonly)],
)
async def set_default_public_entry(
    entry_id: str,
    request: Request,
    user: User = Depends(_REQUIRE_PUBLIC_ENTRY_ADMIN),
) -> PublicEntryView:
    try:
        entry = await _store(request).set_default(
            team_id=user.team_id,
            entry_id=entry_id,
            actor_id=user.id,
        )
    except PublicEntryError as exc:
        raise _public_error(exc) from exc
    return _entry_view(entry)


@router.post(
    "/api/v1/team-settings/public-entries/{entry_id}/disable",
    response_model=PublicEntryView,
    dependencies=[Depends(require_not_readonly)],
)
async def disable_public_entry(
    entry_id: str,
    body: PublicEntryDisable,
    request: Request,
    user: User = Depends(_REQUIRE_PUBLIC_ENTRY_ADMIN),
) -> PublicEntryView:
    try:
        entry = await _store(request).disable(
            team_id=user.team_id,
            entry_id=entry_id,
            actor_id=user.id,
            new_default_entry_id=body.new_default_entry_id,
            disable_last=body.disable_last,
        )
    except PublicEntryError as exc:
        raise _public_error(exc) from exc
    return _entry_view(entry)


@router.delete(
    "/api/v1/team-settings/public-entries/{entry_id}",
    dependencies=[Depends(require_not_readonly)],
)
async def delete_public_entry(
    entry_id: str,
    request: Request,
    user: User = Depends(_REQUIRE_PUBLIC_ENTRY_ADMIN),
) -> dict[str, bool]:
    try:
        await _store(request).delete(
            team_id=user.team_id,
            entry_id=entry_id,
            actor_id=user.id,
        )
    except PublicEntryError as exc:
        raise _public_error(exc) from exc
    return {"ok": True}


@router.get("/.well-known/bowhead-public-entry/{entry_id}")
async def public_entry_challenge_page(entry_id: str, request: Request) -> HTMLResponse:
    entry = await _store(request).get(entry_id)
    if entry is None or entry.status != "pending_verification":
        raise HTTPException(404, "pending public entry not found")
    nonce = secrets.token_urlsafe(18)
    script = f"""<script nonce="{nonce}">
const token = location.hash.slice(1);
history.replaceState(null, "", location.pathname);
const output = document.getElementById("result");
if (!token) {{
  output.textContent = "Missing verification token.";
}} else {{
  fetch(location.pathname + "/verify", {{
    method: "POST",
    credentials: "same-origin",
    headers: {{"content-type": "application/json"}},
    body: JSON.stringify({{token}})
  }}).then(async response => {{
    const body = await response.json().catch(() => ({{}}));
    if (!response.ok) throw new Error(body.detail || "Verification failed.");
    output.textContent = "Public entry verified. You can close this page.";
  }}).catch(error => {{ output.textContent = error.message; }});
}}
</script>"""
    response = HTMLResponse(
        "<!doctype html><html><head><meta charset=utf-8>"
        f'<meta name="referrer" content="no-referrer"><title>{product_name()} public entry verification</title>'
        f"</head><body><main><h1>{product_name()} public entry verification</h1>"
        f'<p id="result">Verifying…</p>{script}</main></body></html>',
    )
    response.headers["cache-control"] = "no-store"
    response.headers["content-security-policy"] = (
        f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; "
        "style-src 'none'; img-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["referrer-policy"] = "no-referrer"
    response.headers["x-content-type-options"] = "nosniff"
    return response


@router.post(
    "/.well-known/bowhead-public-entry/{entry_id}/verify",
    response_model=PublicEntryView,
)
async def verify_public_entry(
    entry_id: str,
    body: PublicEntryVerify,
    request: Request,
) -> PublicEntryView:
    peer_key = request.client.host if request.client else "unknown"
    store = _store(request)
    if not await store.consume_challenge_attempt(
        entry_id=entry_id,
        peer_key=peer_key,
    ):
        raise HTTPException(429, "too many verification attempts")
    try:
        entry = await store.verify_challenge(
            entry_id=entry_id,
            token=body.token,
        )
    except PublicEntryError as exc:
        raise _public_error(exc) from exc
    await store.clear_challenge_attempts(entry_id)
    return _entry_view(entry)
