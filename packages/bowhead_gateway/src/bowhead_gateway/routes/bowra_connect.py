"""Import Bowra credentials into the original model Review/Apply editor."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import secrets
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx2
from bowhead_core.bowra_connect import BowraConnectStore
from bowhead_core.ssrf_guard import SsrfBlocked, build_guarded_async_client, validate_url
from bowhead_core.user_store import User
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import JSONResponse, RedirectResponse

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.product_identity import product_name
from bowhead_gateway.public_entry_context import current_request_public_access_context
from bowhead_gateway.public_url import oauth_return_path, public_app_url_for_base, restore_oauth_public_access_context
from bowhead_gateway.routes.auth import _require_team_role_dep

_REQUIRE_TEAM_ADMIN = _require_team_role_dep("owner", "team_admin")

router = APIRouter(prefix="/api/v1/admin/llm-config/bowra")
CALLBACK_PATH = "/api/v1/admin/llm-config/bowra/callback"
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def available() -> bool:
    return bool(os.environ.get("BOWRA_URL", "").strip())


def _service_url() -> str:
    value = os.environ.get("BOWRA_URL", "").strip().rstrip("/")
    if not value:
        raise HTTPException(404, {"code": "bowra_unavailable"})
    try:
        validate_url(value)
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError
    except (ValueError, SsrfBlocked):
        raise HTTPException(422, {"code": "bowra_service_url_invalid"}) from None
    return value


def _validate_callback(value: str) -> None:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP callback")
    if parsed.scheme != "https" or parsed.port not in (None, 443) or "." not in host or host.endswith(".localhost") or host == "localhost":
        raise ValueError("HTTPS domain callback required")


def _store(request: Request) -> BowraConnectStore:
    return BowraConnectStore(request.app.state.user_store.db_path)


def _return(**query: str) -> RedirectResponse:
    return RedirectResponse(
        oauth_return_path(f"/team/settings/models/?{urlencode(query)}", callback_path=CALLBACK_PATH), status_code=303, headers=HEADERS
    )


@router.post("/start", dependencies=[Depends(require_not_readonly)])
async def start(request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> JSONResponse:
    service_url = _service_url()
    try:
        address = current_request_public_access_context()
        if address is None or not address.prefix_known:
            raise ValueError("browser address unavailable")
        redirect_uri = public_app_url_for_base(address.base_url, CALLBACK_PATH)
        _validate_callback(redirect_uri)
    except ValueError:
        raise HTTPException(422, {"code": "bowra_https_callback_required"}) from None
    verifier = secrets.token_urlsafe(32)
    name = product_name()
    try:
        state = await _store(request).put(
            team_id=user.team_id,
            user_id=user.id,
            kind="authorization",
            payload={"verifier": verifier, "redirect_uri": redirect_uri, "service_url": service_url, "app_name": name},
        )
    except ValueError:
        raise HTTPException(503, {"code": "bowra_encryption_required"}) from None
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    return JSONResponse(
        {
            "authorize_url": f"{service_url}/connect/authorize?{urlencode({'app_name': name, 'redirect_uri': redirect_uri, 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})}"
        },
        headers=HEADERS,
    )


def _validated_result(payload: Any, name: str) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("api_key"), str)
        or not payload["api_key"].strip()
        or len(payload["api_key"]) > 500
        or not isinstance(payload.get("base_url"), str)
        or len(payload["base_url"]) > 500
        or payload.get("name") != name
        or "expires_at" not in payload
        or payload["expires_at"] is not None
        or "quota_limit" not in payload
        or payload["quota_limit"] is not None
        or not isinstance(payload.get("models"), list)
        or len(payload["models"]) > 1000
        or any(not isinstance(model, str) or not model.strip() for model in payload["models"])
        or len(set(payload["models"])) != len(payload["models"])
    ):
        raise ValueError("invalid response")
    validate_url(payload["base_url"])
    parsed = urlsplit(payload["base_url"])
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid endpoint")
    base_url = payload["base_url"].rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    if len(base_url) > 500:
        raise ValueError("invalid endpoint")
    return {"base_url": base_url, "api_key": payload["api_key"], "models": payload["models"]}


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    query = request.query_params
    # Access logs emitted while sending the response must not retain the code.
    request.scope["query_string"] = b""
    try:
        user = await _REQUIRE_TEAM_ADMIN(request, request.app.state.user_store)
        await require_not_readonly(request)
    except HTTPException:
        return _return(bowra_error="permission_changed")
    if any(len(query.getlist(key)) > 1 for key in ("state", "code", "error")) or not query.get("state"):
        return _return(bowra_error="invalid_callback")
    pending = await _store(request).take(query["state"], team_id=user.team_id, user_id=user.id, kind="authorization")
    if pending is None:
        return _return(bowra_error="invalid_callback")
    restore_oauth_public_access_context(pending["redirect_uri"], callback_path=CALLBACK_PATH)
    if not available() or pending["service_url"] != os.environ.get("BOWRA_URL", "").strip().rstrip("/"):
        return _return(bowra_error="unavailable")
    if ("code" in query) == ("error" in query):
        return _return(bowra_error="invalid_callback")
    if "error" in query:
        return _return(bowra_error="cancelled" if query["error"] == "access_denied" else "invalid_callback")
    if not query["code"]:
        return _return(bowra_error="invalid_callback")
    try:
        # No automatic retry: a lost response may already have consumed the code.
        async with build_guarded_async_client(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = await client.post(
                f"{pending['service_url']}/connect/exchange",
                data={"code": query["code"], "code_verifier": pending["verifier"], "redirect_uri": pending["redirect_uri"]},
                headers={"Accept": "application/json"},
            )
        if response.status_code != 200:
            failure = response.json()
            error = failure.get("error") if isinstance(failure, dict) else None
            reason = {
                (429, "rate_limited"): "rate_limited",
                (503, "service_unavailable"): "service_unavailable",
                (400, "invalid_grant"): "exchange_rejected",
            }.get((response.status_code, error if isinstance(error, str) else None), "exchange_uncertain")
            return _return(bowra_error=reason)
        if response.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise ValueError("invalid content type")
        result = _validated_result(response.json(), pending["app_name"])
        if not result["models"]:
            return _return(bowra_error="no_models")
        token = await _store(request).put(team_id=user.team_id, user_id=user.id, kind="import", payload=result)
    except (httpx2.HTTPError, ValueError, SsrfBlocked):
        return _return(bowra_error="exchange_uncertain")
    return _return(bowra_import=token)


@router.post("/imports/{token}", dependencies=[Depends(require_not_readonly)])
async def claim_import(token: str, request: Request, user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> JSONResponse:
    if not available():
        raise HTTPException(404, {"code": "bowra_unavailable"})
    result = await _store(request).take(token, team_id=user.team_id, user_id=user.id, kind="import")
    if result is None:
        raise HTTPException(410, {"code": "bowra_import_expired"})
    return JSONResponse(result, headers=HEADERS)
