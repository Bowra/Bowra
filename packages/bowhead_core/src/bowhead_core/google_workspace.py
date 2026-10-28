"""Google Workspace connection: capability bundles, OAuth web-server flow, token broker.

Design: ``docs/design/Google Workspace 连接-技术设计-2026-09-24.md``.

* The OAuth client is always customer-owned (Internal app) and lives on a
  ``team_builtin_tool`` row (``provider='google_workspace'``); there is no
  deployment-level Google credential or environment fallback.
* No restricted scope is ever requested (no Gmail read, no Drive-wide scopes).
* Refresh tokens are per member (:mod:`bowhead_core.builtin_tool_grants`);
  access tokens only live in this process' memory.
* Requests go to a fixed set of Google hosts through the shared outbound client.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import anyio
import httpx2

from bowhead_core.builtin_tool_grants import BuiltinToolGrant, BuiltinToolGrantError, BuiltinToolGrantStore
from bowhead_core.ssrf_guard import build_guarded_async_client

logger = logging.getLogger(__name__)

PROVIDER = "google_workspace"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALLBACK_PATH = "/api/v1/google/oauth/callback"
_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
_API = "https://www.googleapis.com/auth/"

BASE_BUNDLE = "base"
# Ordered: display order in settings, and the only bundles that exist.
BUNDLES: dict[str, tuple[str, ...]] = {
    BASE_BUNDLE: ("openid", "email", "profile", _API + "drive.file"),
    "read_by_link": (_API + "documents.readonly", _API + "spreadsheets.readonly", _API + "presentations.readonly"),
    "edit_docs": (_API + "documents", _API + "presentations"),
    "edit_sheets": (_API + "spreadsheets",),
    "calendar": (_API + "calendar.events.readonly",),
}
DEFAULT_BUNDLES: dict[str, bool] = {BASE_BUNDLE: True, "read_by_link": True, "edit_docs": True, "edit_sheets": True, "calendar": False}
# Google may echo the short aliases for the identity scopes.
_SCOPE_ALIASES = {
    "https://www.googleapis.com/auth/userinfo.email": "email",
    "https://www.googleapis.com/auth/userinfo.profile": "profile",
}


def feature_enabled() -> bool:
    return os.environ.get("FEATURE_GOOGLE_WORKSPACE", "0") == "1"


class GoogleWorkspaceError(Exception):
    """Stable, secret-free failure code (also the tool ``error_kind``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    expires_in: int
    refresh_token: str | None
    scopes: tuple[str, ...]
    id_token: str | None


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str


# --------------------------------------------------------------------------- config


def normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Canonical non-secret connection settings; unknown keys are dropped."""
    raw = raw or {}
    domain = str(raw.get("domain") or "").strip().lower()
    requested = raw.get("bundles") if isinstance(raw.get("bundles"), dict) else {}
    bundles = {name: bool(requested.get(name, default)) for name, default in DEFAULT_BUNDLES.items()}
    bundles[BASE_BUNDLE] = True
    project_number = str(raw.get("picker_project_number") or "").strip()
    return {"domain": domain, "bundles": bundles, "picker_project_number": project_number}


def valid_domain(domain: str) -> bool:
    labels = domain.split(".")
    return len(labels) >= 2 and all(label and len(label) <= 63 and label.replace("-", "").isalnum() for label in labels) and len(domain) <= 253


def enabled_bundles(config: dict[str, Any]) -> frozenset[str]:
    bundles = normalize_config(config)["bundles"]
    return frozenset(name for name, on in bundles.items() if on)


def scopes_for(bundles: frozenset[str] | set[str]) -> tuple[str, ...]:
    out: list[str] = []
    for name, scopes in BUNDLES.items():
        if name in bundles:
            out.extend(scope for scope in scopes if scope not in out)
    return tuple(out)


def normalize_scopes(raw: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    items = raw.split() if isinstance(raw, str) else list(raw or ())
    return tuple(sorted({_SCOPE_ALIASES.get(item, item) for item in items if item}))


def bundle_granted(bundle: str, granted: tuple[str, ...] | frozenset[str]) -> bool:
    have = set(granted)
    return all(scope in have for scope in BUNDLES[bundle])


# --------------------------------------------------------------------------- OAuth


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: tuple[str, ...],
    login_hint: str,
    domain: str,
    code_challenge: str,
    force_consent: bool,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "state": state,
        "login_hint": login_hint,
        "hd": domain,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if force_consent:
        params["prompt"] = "consent"
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


_client_factory: Callable[[], httpx2.AsyncClient] = lambda: build_guarded_async_client(timeout=20.0, follow_redirects=False)  # noqa: E731


def set_http_client_factory(factory: Callable[[], httpx2.AsyncClient] | None) -> None:
    """Test seam; ``None`` restores the guarded client."""
    global _client_factory
    _client_factory = factory or (lambda: build_guarded_async_client(timeout=20.0, follow_redirects=False))


def http_client() -> httpx2.AsyncClient:
    return _client_factory()


def _token_response(payload: dict[str, Any]) -> TokenResponse:
    access = str(payload.get("access_token") or "")
    if not access:
        raise GoogleWorkspaceError("google_token_exchange_failed")
    return TokenResponse(
        access_token=access,
        expires_in=int(payload.get("expires_in") or 3600),
        refresh_token=str(payload["refresh_token"]) if payload.get("refresh_token") else None,
        scopes=normalize_scopes(payload.get("scope")),
        id_token=str(payload["id_token"]) if payload.get("id_token") else None,
    )


async def _post_token(form: dict[str, str]) -> dict[str, Any]:
    try:
        async with http_client() as client:
            response = await client.post(TOKEN_URL, data=form, headers={"Accept": "application/json"})
    except httpx2.HTTPError:
        raise GoogleWorkspaceError("google_unreachable") from None
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 500:
        raise GoogleWorkspaceError("google_unreachable")
    if not response.is_success:
        if payload.get("error") == "invalid_grant":
            raise GoogleWorkspaceError("google_reauth_required")
        raise GoogleWorkspaceError("google_token_exchange_failed")
    return payload if isinstance(payload, dict) else {}


async def exchange_code(*, client_id: str, client_secret: str, code: str, redirect_uri: str, code_verifier: str) -> TokenResponse:
    payload = await _post_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
    )
    return _token_response(payload)


async def refresh_access_token(*, client_id: str, client_secret: str, refresh_token: str) -> TokenResponse:
    payload = await _post_token(
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id, "client_secret": client_secret}
    )
    return _token_response(payload)


async def revoke(token: str) -> None:
    """Best effort; failures are logged, never raised."""
    if not token:
        return
    try:
        async with http_client() as client:
            await client.post(REVOKE_URL, data={"token": token})
    except Exception as exc:  # pragma: no cover - network best effort
        logger.info("google_workspace.revoke_failed err=%s", type(exc).__name__)


def verify_identity(id_token: str | None, *, client_id: str, domain: str, expected_email: str) -> GoogleIdentity:
    """Check the ID token received directly from Google's token endpoint over TLS.

    Per Google's OpenID Connect guidance, a token obtained straight from the token
    endpoint need not have its signature verified; the claims still must match.
    ``hd`` in the authorization URL is only a UI hint — the claim is authoritative.
    """
    if not id_token:
        raise GoogleWorkspaceError("google_identity_invalid")
    try:
        body = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        raise GoogleWorkspaceError("google_identity_invalid") from None
    if not isinstance(claims, dict) or claims.get("iss") not in _ISSUERS or claims.get("aud") != client_id:
        raise GoogleWorkspaceError("google_identity_invalid")
    if float(claims.get("exp") or 0) < time.time():
        raise GoogleWorkspaceError("google_identity_invalid")
    if str(claims.get("hd") or "").lower() != domain.lower():
        raise GoogleWorkspaceError("google_domain_mismatch")
    email = str(claims.get("email") or "")
    if claims.get("email_verified") not in (True, "true") or not email:
        raise GoogleWorkspaceError("google_identity_invalid")
    if email.casefold() != expected_email.strip().casefold():
        raise GoogleWorkspaceError("google_email_mismatch")
    subject = str(claims.get("sub") or "")
    if not subject:
        raise GoogleWorkspaceError("google_identity_invalid")
    return GoogleIdentity(subject=subject, email=email)


# --------------------------------------------------------------------------- broker


@dataclass
class _CachedToken:
    token: str
    expires_at: float


class GoogleTokenBroker:
    """Resolve a member's access token for one connection; refresh single-flight.

    Access tokens are cached only in memory and dropped 60 s before expiry.
    ``invalid_grant`` marks the grant ``needs_reauth`` — it is never retried as
    a transient failure.
    """

    def __init__(self, grants: BuiltinToolGrantStore) -> None:
        self.grants = grants
        self._cache: dict[str, _CachedToken] = {}
        self._locks: dict[str, anyio.Lock] = {}
        # Bumped by forget(): a refresh that started before a reconnect must not cache its token.
        self._generation: dict[str, int] = {}

    def forget(self, grant_id: str) -> None:
        self._cache.pop(grant_id, None)
        self._generation[grant_id] = self._generation.get(grant_id, 0) + 1

    async def access_token(self, *, tool: Any, auth: dict[str, Any], user_id: str, required_bundles: frozenset[str]) -> tuple[str, BuiltinToolGrant]:
        grant = await self.grants.get(tool.id, user_id)
        if grant is None:
            raise GoogleWorkspaceError("google_not_connected")
        if grant.status != "active":
            raise GoogleWorkspaceError("google_reauth_required")
        if not all(bundle_granted(bundle, grant.granted_scopes) for bundle in required_bundles):
            raise GoogleWorkspaceError("google_scope_missing")
        cached = self._cache.get(grant.id)
        if cached and cached.expires_at > time.monotonic():
            return cached.token, grant
        lock = self._locks.setdefault(grant.id, anyio.Lock())
        async with lock:
            cached = self._cache.get(grant.id)
            if cached and cached.expires_at > time.monotonic():
                return cached.token, grant
            generation = self._generation.get(grant.id, 0)
            try:
                refresh, version = await self.grants.refresh_token_versioned(grant)
            except BuiltinToolGrantError:
                await self.grants.mark_needs_reauth(tool.id, user_id)
                raise GoogleWorkspaceError("google_reauth_required") from None
            try:
                response = await refresh_access_token(
                    client_id=str(auth.get("client_id") or ""), client_secret=str(auth.get("client_secret") or ""), refresh_token=refresh
                )
            except GoogleWorkspaceError as exc:
                if exc.code == "google_reauth_required":
                    await self.grants.mark_needs_reauth(tool.id, user_id, expected_cipher=version)
                    self.forget(grant.id)
                raise
            if self._generation.get(grant.id, 0) == generation:
                self._cache[grant.id] = _CachedToken(response.access_token, time.monotonic() + max(response.expires_in - 60, 0))
            await self.grants.touch(tool.id, user_id)
            return response.access_token, grant


_brokers: dict[str, GoogleTokenBroker] = {}


def token_broker(db_path: str | os.PathLike[str]) -> GoogleTokenBroker:
    """Process-wide broker per database, so the access-token cache and refresh locks are shared."""
    key = str(db_path)
    broker = _brokers.get(key)
    if broker is None:
        broker = _brokers[key] = GoogleTokenBroker(BuiltinToolGrantStore(key))
    return broker
