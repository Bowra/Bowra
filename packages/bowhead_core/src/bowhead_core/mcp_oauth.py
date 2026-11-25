"""tools sprint WP2b — OAuth 2.1 for remote MCP sources.

Remote MCP servers (GitHub / Linear / Notion / Atlassian / Slack …) almost all require OAuth.
This module backs the MCP SDK's OAuth 2.1 client with Bowhead's Fernet-encrypted, team-scoped
credential store, and serves runtime access tokens through the shared single-flight broker.

Pieces:
  - ``FernetTokenStorage`` — the SDK's ``TokenStorage`` protocol backed by the team tool source's
    ``auth_ref`` (Fernet ciphertext, team-scoped; the token set / dynamic-client-registration record
    are NEVER stored plaintext and NEVER logged). Reads decrypt via the store; writes re-encrypt.
  - ``begin_mcp_oauth`` / ``exchange_mcp_oauth_code`` — the interactive authorization, driven
    server-side by the gateway route (initiate + callback).
  - ``team_oauth_headers`` — runtime access tokens through the shared single-flight
    ``OAuthTokenBroker``: one refresh per source at a time, rotated refresh tokens written over the
    version they replace, a rejected refresh recorded as an ended sign-in. A never-authorized source
    fails closed.

Security notes: refresh tokens live only in the Fernet store; access tokens in process memory. A
refresh only goes to the token endpoint recorded when the source was authorized (or, for sources
authorized before that was recorded, to the endpoint of the same issuer). The guarded httpx2
transport (SSRF pin-to-IP, follow_redirects=False) applies to every OAuth + tool request.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx2
from mcp.client.auth.exceptions import OAuthFlowError
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_client_registration_request,
    create_oauth_metadata_request,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
    get_client_metadata_scopes,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    handle_registration_response,
    handle_token_response_scopes,
    issuers_match,
    validate_metadata_issuer,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from pydantic import ValidationError

from bowhead_core._db import is_authority_busy
from bowhead_core.oauth_token_broker import OAuthGrantRejected, OAuthRefreshUnavailable, OAuthTokenBroker, RefreshedToken
from bowhead_core.ssrf_guard import build_guarded_async_client, validate_url

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

logger = logging.getLogger(__name__)

# where the token set + dynamic-client-registration record live inside the (Fernet-encrypted) auth dict
_TOKENS_KEY = "oauth_tokens"
_CLIENT_KEY = "oauth_client_info"
# token endpoint + RFC 8707 resource recorded at authorization, so a refresh needs no discovery
_META_KEY = "oauth_meta"
# set when the provider refused to renew the grant; cleared by the next successful authorization
_GRANT_STATE_KEY = "oauth_grant_state"
# a refused client registration cannot be reused for the next authorization
_CLIENT_REJECTED_REASONS = frozenset({"invalid_client", "unauthorized_client"})


class _AuthStore(Protocol):
    async def get_team_tool_auth_decrypted(self, team_id: str, source_id: str) -> dict[str, Any] | None: ...
    async def update_team_tool_source_auth(self, team_id: str, source_id: str, *, auth_type: str, auth: dict[str, Any]) -> None: ...


class _VersionedAuthStore(_AuthStore, Protocol):
    async def get_team_tool_oauth_auth_versioned(self, team_id: str, source_id: str) -> tuple[str, dict[str, Any]] | None: ...
    async def replace_team_tool_oauth_auth(self, team_id: str, source_id: str, auth: dict[str, Any], *, expected_auth_ref: str) -> bool: ...


class FernetTokenStorage:
    """SDK ``TokenStorage`` backed by the team tool source's Fernet-encrypted ``auth_ref`` (team-scoped).

    The decrypted auth dict holds the OAuth token set + the dynamic-client-registration record under
    reserved keys; get/set decrypt / re-encrypt through the store (F14 guard refuses plaintext)."""

    def __init__(self, store: _AuthStore, team_id: str, source_id: str) -> None:
        self._store = store
        self._team = team_id
        self._src = source_id
        self._cache: dict[str, Any] | None = None

    async def _load(self) -> dict[str, Any]:
        if self._cache is None:
            self._cache = await self._store.get_team_tool_auth_decrypted(self._team, self._src) or {}
        return self._cache

    async def _save(self) -> None:
        await self._store.update_team_tool_source_auth(self._team, self._src, auth_type="oauth", auth=self._cache or {})

    async def get_tokens(self) -> OAuthToken | None:
        t = (await self._load()).get(_TOKENS_KEY)
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        d = await self._load()
        d[_TOKENS_KEY] = tokens.model_dump(mode="json")
        self._cache = d
        await self._save()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        c = (await self._load()).get(_CLIENT_KEY)
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        d = await self._load()
        d[_CLIENT_KEY] = client_info.model_dump(mode="json")
        self._cache = d
        await self._save()

    async def set_grant(self, tokens: OAuthToken, meta: dict[str, Any]) -> None:
        """Store a newly authorized grant: tokens, the refresh endpoint, and no ended-sign-in mark."""
        d = await self._load()
        d[_TOKENS_KEY] = tokens.model_dump(mode="json")
        d[_META_KEY] = {key: value for key, value in meta.items() if value}
        d.pop(_GRANT_STATE_KEY, None)
        self._cache = d
        await self._save()

    async def client_rejected(self) -> bool:
        """True when the provider refused the stored client registration at the last refresh."""
        state = (await self._load()).get(_GRANT_STATE_KEY) or {}
        return isinstance(state, dict) and state.get("reason") in _CLIENT_REJECTED_REASONS


def mcp_oauth_client_metadata(redirect_uri: str) -> OAuthClientMetadata:
    """Public-client (PKCE, no secret) metadata for dynamic registration against an MCP auth server."""
    return OAuthClientMetadata(
        client_name="Bowhead",
        redirect_uris=[redirect_uri],  # type: ignore[list-item]
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


# ─────────────────────────────────────────────────────────────
# WP2b-2 — interactive server-side authorization driver.
#
# The SDK's OAuthClientProvider bundles the whole flow into ONE blocking httpx2 generator
# (redirect_handler → callback_handler), which cannot span a stateless web round-trip
# (initiate request → user's browser → a SEPARATE callback request). So we drive the flow
# ourselves, reusing the SDK's message-shaping helpers (discovery-URL builders + request /
# response parsers) but OWNING the PKCE verifier + state lifecycle (persisted server-side,
# single-use, team-scoped — the gateway route holds the pending record). Every server↔server
# request rides the SSRF-guarded transport (pin-to-IP, redirects OFF); remote MCP / IdP
# endpoints must be HTTPS (loopback allowed only in dev / tests). The Bowhead callback is a
# browser-facing public entry and may be HTTP or HTTPS; it is not a server-to-server fetch.
# ─────────────────────────────────────────────────────────────


@dataclass
class OAuthBeginResult:
    """What ``begin_mcp_oauth`` yields: the URL to send the admin's browser to, plus the
    endpoint / client_id / resource / scope the callback needs to complete the exchange
    (persisted alongside the PKCE verifier in the single-use pending record)."""

    authorization_url: str
    token_endpoint: str
    client_id: str
    resource: str | None
    scope: str | None


def _require_https(url: str) -> None:
    """Apply the deployment-mode outbound URL policy to OAuth endpoints."""
    validate_url(url)


def _require_http_callback(url: str) -> None:
    """Bowhead public callbacks may use either supported browser URL scheme."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"OAuth redirect URI must be an absolute http(s) URL: {url!r}")


@dataclass(frozen=True)
class McpOAuthProbe:
    """What an unauthenticated look at an MCP server says about signing in to it."""

    needs_oauth: bool
    provider_host: str | None = None
    #: False when the authorization server publishes metadata without a registration endpoint, so
    #: only an administrator-registered app can sign in; None when that could not be determined.
    registration_supported: bool | None = None


async def probe_mcp_oauth(server_url: str, *, timeout: float = 6.0) -> tuple[bool, str | None]:
    """Side-effect-FREE probe (WP2b-3c): does this MCP server require OAuth? Returns
    ``(needs_oauth, provider_hint)``. Used by ``/preview`` to route an OAuth server into the
    先建后授权 flow instead of failing 'could not connect'. See ``probe_mcp_oauth_details``."""
    probe = await probe_mcp_oauth_details(server_url, timeout=timeout, check_registration=False)
    return probe.needs_oauth, probe.provider_host


async def probe_mcp_oauth_details(server_url: str, *, timeout: float = 6.0, check_registration: bool = True) -> McpOAuthProbe:
    """NO registration, NO storage, NO tokens — just an unauthenticated probe: a 401 (optionally pointing
    at a resource_metadata URL) CONFIRMED by discoverable protected-resource metadata (RFC 9728)
    advertising an authorization server. A bare 401 is NOT enough — a static bearer/api_key/basic MCP
    server also 401s but does NOT do OAuth, so we require the PRM signal (review P2 — else static-auth
    servers get misrouted into the OAuth wizard). ``provider_host`` = the authorization server's host for
    the UI's ``{provider}`` label. With ``check_registration`` it also reads the authorization-server
    metadata (RFC 8414) for a registration endpoint. SSRF-guarded. ``timeout`` is an OVERALL deadline for
    the whole probe, so a hanging server can't stack N × per-request timeouts (review P3)."""
    per_req = httpx2.Timeout(min(timeout, 4.0))
    try:
        async with asyncio.timeout(timeout):  # overall deadline across all probe requests
            async with build_guarded_async_client(follow_redirects=False, timeout=per_req) as client:
                www_auth: str | None = None
                try:
                    probe = await client.get(server_url)
                    if probe.status_code == 401:
                        www_auth = extract_resource_metadata_from_www_auth(probe)
                except Exception:
                    return McpOAuthProbe(False)
                for url in build_protected_resource_metadata_discovery_urls(www_auth, server_url):
                    try:
                        resp = await client.send(create_oauth_metadata_request(url))
                        prm = await handle_protected_resource_response(resp)
                    except Exception:
                        prm = None
                    if prm and prm.authorization_servers:
                        auth_server = str(prm.authorization_servers[0])
                        host = urlparse(auth_server).hostname
                        if not check_registration:
                            return McpOAuthProbe(True, host)
                        return McpOAuthProbe(True, host, await _registration_supported(client, auth_server, server_url))
    except Exception:  # incl. asyncio.timeout's TimeoutError → best-effort, fall through to "no OAuth"
        return McpOAuthProbe(False)
    # only a discoverable PRM advertising an authorization server counts as "needs OAuth".
    return McpOAuthProbe(False)


async def _registration_supported(client: httpx2.AsyncClient, auth_server: str, server_url: str) -> bool | None:
    for url in build_oauth_authorization_server_metadata_discovery_urls(auth_server, server_url):
        try:
            ok, asm = await handle_auth_metadata_response(await client.send(create_oauth_metadata_request(url)))
        except Exception:
            return None
        if not ok:
            return None
        if asm is not None:
            return asm.registration_endpoint is not None
    return None


class McpOAuthRegistrationRefused(ValueError):
    """The authorization server refused Bowhead's client registration (RFC 7591 §3.2.2)."""

    def __init__(self, code: str) -> None:
        super().__init__(f"client registration refused: {code}")
        self.code = code


def _base_url(server_url: str) -> str:
    p = urlparse(server_url)
    return f"{p.scheme}://{p.netloc}"


async def _discover_and_register(
    server_url: str,
    client: httpx2.AsyncClient,
    storage: FernetTokenStorage,
    client_metadata: OAuthClientMetadata,
    www_auth: str | None,
    www_auth_scope: str | None,
    *,
    reuse_client: bool = True,
) -> tuple[OAuthMetadata | None, ProtectedResourceMetadata | None, OAuthClientInformationFull]:
    """Straight-line version of the SDK's async_auth_flow discovery+registration (steps 1-4):
    protected-resource metadata (RFC 9728) → auth-server metadata (RFC 8414, with fallbacks) →
    scope selection → dynamic client registration (RFC 7591) if we have no client_info yet. Reuses
    the SDK's URL builders + response parsers so the fallback semantics match exactly."""
    prm: ProtectedResourceMetadata | None = None
    auth_server_url: str | None = None
    for url in build_protected_resource_metadata_discovery_urls(www_auth, server_url):
        resp = await client.send(create_oauth_metadata_request(url))
        cand = await handle_protected_resource_response(resp)
        if cand and cand.authorization_servers:
            prm = cand
            auth_server_url = str(cand.authorization_servers[0])
            break

    asm: OAuthMetadata | None = None
    for url in build_oauth_authorization_server_metadata_discovery_urls(auth_server_url, server_url):
        resp = await client.send(create_oauth_metadata_request(url))
        ok, cand_asm = await handle_auth_metadata_response(resp)
        if not ok:
            break
        if cand_asm:
            asm = cand_asm
            break

    # review P2: select scope BEFORE registration (mirror SDK ordering — its DCR body carries the
    # scope). Registering first with no scope makes a scope-strict AS issue tokens missing the scope
    # we then request in the authorize URL → later tool calls fail insufficient_scope.
    client_metadata.scope = get_client_metadata_scopes(www_auth_scope, prm, asm)

    client_info = await storage.get_client_info() if reuse_client else None
    if client_info is not None and not await _client_info_reusable(storage, client_info, client_metadata):
        client_info = None
    if client_info is None:
        reg_req = create_client_registration_request(asm, client_metadata, _base_url(server_url))
        reg_resp = await client.send(reg_req)
        if reg_resp.status_code >= 400:
            # Name the refusal (for example invalid_redirect_uri); provider text never leaves here.
            await reg_resp.aread()
            try:
                code = reg_resp.json().get("error")
            except (ValueError, AttributeError):
                code = None
            raise McpOAuthRegistrationRefused(code if isinstance(code, str) and code.isidentifier() else f"http_{reg_resp.status_code}")
        client_info = await handle_registration_response(reg_resp)
        if asm is not None and asm.issuer and client_info.issuer is None:
            # Bind the registration to the server that issued it (SEP-2352), as the SDK does.
            client_info.issuer = str(asm.issuer)
        await storage.set_client_info(client_info)
    return asm, prm, client_info


async def _client_info_reusable(storage: FernetTokenStorage, client_info: OAuthClientInformationFull, metadata: OAuthClientMetadata) -> bool:
    """A stored registration is reused only for the same callback and while the provider accepts it.

    A changed public entry changes the callback, which a strict server refuses for the old client;
    a client the provider rejected at refresh (``invalid_client``) stays rejected (spike 2026-11-24)."""
    registered = {str(uri) for uri in (client_info.redirect_uris or [])}
    wanted = {str(uri) for uri in (metadata.redirect_uris or [])}
    if registered and not wanted <= registered:
        return False
    return not await storage.client_rejected()


async def begin_mcp_oauth(
    server_url: str,
    storage: FernetTokenStorage,
    redirect_uri: str,
    *,
    state: str,
    code_challenge: str,
    timeout: float = 10.0,
    reuse_client: bool = True,
) -> OAuthBeginResult:
    """Discover + register (persisting client_info via ``storage``) and build the authorization
    URL for the admin's browser. ``reuse_client=False`` registers a new client even when one is
    stored: a provider may delete the registration when access is revoked there (n8n does), and an
    authorization URL with a deleted client strands the browser on the provider's error page. ``state`` (CSRF, server-issued) and ``code_challenge`` (S256 of
    the caller-held verifier) are the caller's — this NEVER generates or stores the verifier, so it
    cannot leak into the browser leg. Returns the token_endpoint / client_id / resource / scope the
    callback must reuse verbatim (RFC 8707 resource binding, exact redirect_uri)."""
    _require_https(server_url)
    _require_http_callback(redirect_uri)
    client_metadata = mcp_oauth_client_metadata(redirect_uri)
    async with build_guarded_async_client(follow_redirects=False, timeout=httpx2.Timeout(timeout)) as client:
        www_auth: str | None = None
        www_auth_scope: str | None = None
        try:  # best-effort: a 401 from the MCP endpoint may point at the resource metadata + scope
            probe = await client.get(server_url)
            if probe.status_code == 401:
                www_auth = extract_resource_metadata_from_www_auth(probe)
                www_auth_scope = extract_scope_from_www_auth(probe)
        except Exception:
            www_auth = None
        asm, prm, client_info = await _discover_and_register(
            server_url, client, storage, client_metadata, www_auth, www_auth_scope, reuse_client=reuse_client
        )

    scope = client_metadata.scope  # set by _discover_and_register (used for both DCR + the authorize URL)
    base = _base_url(server_url)
    auth_endpoint = str(asm.authorization_endpoint) if (asm and asm.authorization_endpoint) else urljoin(base, "/authorize")
    token_endpoint = str(asm.token_endpoint) if (asm and asm.token_endpoint) else urljoin(base, "/token")
    _require_https(auth_endpoint)
    _require_https(token_endpoint)
    # review P2: RFC 8707 resource must match what the SDK's RUNTIME refresh sends. At refresh the
    # provider has no PRM loaded, so it uses the canonical resource_url_from_server_url(server_url)
    # and always includes it (protocol ≥ 2025-06-18). Mirror that here (canonical, always) so the
    # grant audience == the refresh audience — else a resource-strict AS kills the token at first
    # refresh. (A server that demands the PRM resource verbatim is an SDK-refresh limitation, noted.)
    resource: str | None = resource_url_from_server_url(server_url)
    client_id = client_info.client_id
    if not client_id:
        raise ValueError("client registration returned no client_id")

    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scope:
        params["scope"] = scope
    if resource:
        params["resource"] = resource
    authorization_url = f"{auth_endpoint}?{urlencode(params)}"
    return OAuthBeginResult(authorization_url, token_endpoint, client_id, resource, scope)


async def exchange_mcp_oauth_code(
    storage: FernetTokenStorage,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    token_endpoint: str,
    client_id: str,
    resource: str | None,
    timeout: float = 10.0,
) -> OAuthToken:
    """Exchange the authorization code (+ PKCE verifier) for tokens at the persisted token_endpoint
    and store them via ``storage`` (Fernet). ``redirect_uri`` must equal the one used at authorize
    (RFC 6749 §4.1.3); the guarded transport still applies (server↔server). Raises on non-200."""
    _require_https(token_endpoint)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if resource:
        data["resource"] = resource
    async with build_guarded_async_client(follow_redirects=False, timeout=httpx2.Timeout(timeout)) as client:
        resp = await client.post(token_endpoint, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if resp.status_code != 200:
            await resp.aread()
            raise ValueError(f"token exchange failed: HTTP {resp.status_code}")
        token = await handle_token_response_scopes(resp)
    await storage.set_grant(token, {"token_endpoint": token_endpoint, "resource": resource})
    return token


# ─────────────────────────────────────────────────────────────
# Runtime access tokens for team OAuth sources (shared single-flight broker).
#
# Every call connects anew, so the SDK's self-refreshing provider used to refresh once per
# connection: simultaneous calls at expiry refreshed with the same refresh token, and providers
# that rotate refresh tokens (n8n, Notion, Monday, …) refused all but one — or revoked the grant.
# The broker refreshes once per source and hands the same access token to every caller.
# ─────────────────────────────────────────────────────────────

_REFRESH_TIMEOUT_SECONDS = 10.0
# RFC 6749 §5.2 codes that mean the grant or the client can no longer be renewed.
_REJECTION_CODES = frozenset({"invalid_grant", "invalid_client", "unauthorized_client", "invalid_scope", "invalid_request", "unsupported_grant_type"})
_brokers: dict[str, OAuthTokenBroker] = {}
# Token endpoint + resource found by discovery for grants stored before ``oauth_meta`` existed,
# per grant scope, server and client (written to ``oauth_meta`` with the next rotated refresh token).
_discovered_meta: dict[tuple[str, str, str], dict[str, str]] = {}


def _retry_after_seconds(response: httpx2.Response) -> int | None:
    value = (response.headers.get("retry-after") or "").strip()
    return min(int(value), 3600) if value.isdigit() else None


def _token_request_auth(client_info: OAuthClientInformationFull, data: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Client authentication at the token endpoint (RFC 6749 §2.3.1), as the SDK's provider does."""
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    method = client_info.token_endpoint_auth_method
    secret = client_info.client_secret
    if method == "client_secret_basic" and secret:
        pair = f"{quote(str(client_info.client_id), safe='')}:{quote(secret, safe='')}"
        headers["Authorization"] = f"Basic {base64.b64encode(pair.encode()).decode()}"
    elif method == "client_secret_post" and secret:
        data["client_secret"] = secret
    return data, headers


async def refresh_mcp_oauth_token(
    *,
    token_endpoint: str,
    client_info: OAuthClientInformationFull,
    refresh_token: str,
    resource: str | None,
    timeout: float = _REFRESH_TIMEOUT_SECONDS,
) -> RefreshedToken:
    """One refresh-token grant. Raises ``OAuthGrantRejected`` when the provider will not renew the
    grant (4xx with an RFC 6749 code, 401/403) and ``OAuthRefreshUnavailable`` for network errors,
    5xx, 429 and unreadable responses. Provider error text never leaves this function."""
    _require_https(token_endpoint)
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": str(client_info.client_id)}
    if resource:
        data["resource"] = resource  # RFC 8707: the same audience as at authorization
    data, headers = _token_request_auth(client_info, data)
    try:
        async with build_guarded_async_client(follow_redirects=False, timeout=httpx2.Timeout(timeout)) as client:
            resp = await client.post(token_endpoint, data=data, headers=headers)
            await resp.aread()
    except (httpx2.HTTPError, OSError, TimeoutError) as exc:
        raise OAuthRefreshUnavailable(f"network:{type(exc).__name__}") from None
    status = resp.status_code
    if status == 429:
        raise OAuthRefreshUnavailable("rate_limited", retry_after_seconds=_retry_after_seconds(resp))
    if status >= 500:
        raise OAuthRefreshUnavailable(f"http_{status}")
    if status in (400, 401, 403):
        try:
            code = resp.json().get("error")
        except (ValueError, AttributeError):
            code = None
        raise OAuthGrantRejected(code if code in _REJECTION_CODES else f"http_{status}")
    if status != 200:
        raise OAuthRefreshUnavailable(f"http_{status}")
    try:
        token = OAuthToken.model_validate_json(resp.content)
    except ValidationError:
        raise OAuthRefreshUnavailable("invalid_response") from None
    return RefreshedToken(access_token=token.access_token, expires_in=token.expires_in, refresh_token=token.refresh_token)


async def _discover_refresh_meta(
    server_url: str, client_info: OAuthClientInformationFull, *, timeout: float = _REFRESH_TIMEOUT_SECONDS
) -> dict[str, str]:
    """Token endpoint for a source authorized before ``oauth_meta`` was recorded.

    Mirrors the SDK's checks so a refresh token never goes to another server: the resource must match,
    the authorization-server metadata must name the expected issuer, and a registration bound to an
    issuer is only used with that issuer."""
    try:
        async with build_guarded_async_client(follow_redirects=False, timeout=httpx2.Timeout(timeout)) as client:
            www_auth: str | None = None
            with contextlib.suppress(httpx2.HTTPError, OSError, TimeoutError):
                probe = await client.get(server_url)
                if probe.status_code == 401:
                    www_auth = extract_resource_metadata_from_www_auth(probe)
            prm: ProtectedResourceMetadata | None = None
            for url in build_protected_resource_metadata_discovery_urls(www_auth, server_url):
                candidate = await handle_protected_resource_response(await client.send(create_oauth_metadata_request(url)))
                if candidate and candidate.authorization_servers:
                    prm = candidate
                    break
            resource = resource_url_from_server_url(server_url)
            if prm is not None and prm.resource and not check_resource_allowed(requested_resource=resource, configured_resource=str(prm.resource)):
                raise OAuthGrantRejected("resource_mismatch")
            auth_server_url = str(prm.authorization_servers[0]) if prm is not None else None
            expected_issuer = auth_server_url or _base_url(server_url)
            if client_info.issuer is not None and not issuers_match(client_info.issuer, expected_issuer):
                raise OAuthGrantRejected("authorization_server_changed")
            asm: OAuthMetadata | None = None
            for url in build_oauth_authorization_server_metadata_discovery_urls(auth_server_url, server_url):
                ok, candidate_asm = await handle_auth_metadata_response(await client.send(create_oauth_metadata_request(url)))
                if not ok:
                    break
                if candidate_asm:
                    if auth_server_url is None and issuers_match(str(candidate_asm.issuer), expected_issuer):
                        expected_issuer = str(candidate_asm.issuer)
                    validate_metadata_issuer(candidate_asm, expected_issuer)
                    asm = candidate_asm
                    break
    except OAuthFlowError:
        raise OAuthGrantRejected("authorization_server_changed") from None
    except (httpx2.HTTPError, OSError, TimeoutError, ValidationError) as exc:
        raise OAuthRefreshUnavailable(f"discovery:{type(exc).__name__}") from None
    token_endpoint = str(asm.token_endpoint) if asm and asm.token_endpoint else urljoin(_base_url(server_url), "/token")
    return {"token_endpoint": token_endpoint, "resource": resource}


class OAuthGrantRecord(Protocol):
    """Where one stored MCP OAuth grant lives: a team source's ``auth_ref`` or a personal connection."""

    #: Distinguishes stores in process-wide caches (database path plus the grant's identity).
    cache_scope: str

    async def read_versioned(self) -> tuple[str, dict[str, Any]] | None:
        """(stored ciphertext, decrypted auth); the ciphertext is the grant version. None when nothing is stored."""
        ...

    async def replace(self, auth: dict[str, Any], *, expected_version: str) -> bool:
        """Write ``auth`` only over ``expected_version``; False when the grant changed meanwhile."""
        ...

    async def end(self, auth: dict[str, Any], *, expected_version: str) -> None:
        """Record that the provider will not renew the grant (``auth`` carries the ended mark)."""
        ...


class McpOAuthGrant:
    """A stored MCP OAuth grant for the broker; the version is the stored ciphertext."""

    def __init__(self, record: OAuthGrantRecord, server_url: str) -> None:
        self._record = record
        self._server_url = server_url
        self._auth: dict[str, Any] = {}
        self._meta: dict[str, str] | None = None

    async def read(self) -> tuple[str, str]:
        row = await self._record.read_versioned()
        if row is None:
            raise OAuthGrantRejected("not_authorized")
        version, auth = row
        state = auth.get(_GRANT_STATE_KEY)
        if isinstance(state, dict) and state.get("ended"):
            # The provider already refused this grant; asking again cannot succeed.
            raise OAuthGrantRejected(str(state.get("reason") or "ended"))
        refresh_token = (auth.get(_TOKENS_KEY) or {}).get("refresh_token")
        if not refresh_token or not auth.get(_CLIENT_KEY):
            raise OAuthGrantRejected("not_authorized")
        self._auth = auth
        return str(refresh_token), version

    async def refresh(self, refresh_token: str) -> RefreshedToken:
        try:
            client_info = OAuthClientInformationFull.model_validate(self._auth[_CLIENT_KEY])
        except ValidationError:
            raise OAuthGrantRejected("client_unreadable") from None
        meta = self._auth.get(_META_KEY) or {}
        if not meta.get("token_endpoint"):
            key = (self._record.cache_scope, self._server_url, str(client_info.client_id))
            meta = _discovered_meta.get(key) or await _discover_refresh_meta(self._server_url, client_info)
            _discovered_meta[key] = meta
            self._meta = meta
        return await refresh_mcp_oauth_token(
            token_endpoint=str(meta["token_endpoint"]), client_info=client_info, refresh_token=refresh_token, resource=meta.get("resource")
        )

    async def store_rotated(self, refreshed: RefreshedToken, expected_version: str) -> bool:
        auth = dict(self._auth)
        tokens = dict(auth.get(_TOKENS_KEY) or {})
        tokens.update(access_token=refreshed.access_token, refresh_token=refreshed.refresh_token, expires_in=refreshed.expires_in)
        auth[_TOKENS_KEY] = tokens
        if self._meta is not None:
            auth[_META_KEY] = self._meta
        return await self._record.replace(auth, expected_version=expected_version)

    async def mark_rejected(self, reason: str, expected_version: str | None) -> None:
        if expected_version is None:
            return  # nothing readable to mark: never authorized, or already marked
        auth = dict(self._auth)
        auth[_GRANT_STATE_KEY] = {"ended": True, "reason": reason}
        try:
            await self._record.end(auth, expected_version=expected_version)
        except sqlite3.OperationalError as error:
            if not is_authority_busy(error):
                raise
            logger.warning("mcp_oauth.sign_in_end_unrecorded scope=%s", self._record.cache_scope)

    async def used(self) -> None:
        return None


class _TeamSourceGrant:
    """A team source's grant in ``team_tool_source.auth_ref``; an ended sign-in is a mark in the auth."""

    def __init__(self, store: _VersionedAuthStore, team_id: str, source_id: str) -> None:
        self._store = store
        self._team = team_id
        self._src = source_id
        self.cache_scope = f"{_store_key(store)}:{_grant_key(team_id, source_id)}"

    async def read_versioned(self) -> tuple[str, dict[str, Any]] | None:
        return await self._store.get_team_tool_oauth_auth_versioned(self._team, self._src)

    async def replace(self, auth: dict[str, Any], *, expected_version: str) -> bool:
        return await self._store.replace_team_tool_oauth_auth(self._team, self._src, auth, expected_auth_ref=expected_version)

    async def end(self, auth: dict[str, Any], *, expected_version: str) -> None:
        await self._store.replace_team_tool_oauth_auth(self._team, self._src, auth, expected_auth_ref=expected_version)


def _store_key(store: Any) -> str:
    return str(getattr(store, "db_path", "") or id(store))


def oauth_broker(store: Any) -> OAuthTokenBroker:
    """One broker per database, so the access-token cache and refresh locks are shared in the process."""
    key = _store_key(store)
    broker = _brokers.get(key)
    if broker is None:
        broker = _brokers[key] = OAuthTokenBroker()
    return broker


def _grant_key(team_id: str, source_id: str) -> str:
    return f"team-mcp:{team_id}:{source_id}"


async def oauth_grant_headers(
    broker: OAuthTokenBroker, key: str, record: OAuthGrantRecord, server_url: str, *, rejected_token: str | None = None
) -> dict[str, str]:
    """Bearer header for a stored MCP grant. ``rejected_token`` is the access token the MCP server just
    refused; it is dropped (unless another caller already replaced it) and a fresh one is fetched."""
    if rejected_token is not None:
        broker.invalidate(key, rejected_token)
    token = await broker.access_token(key, McpOAuthGrant(record, server_url))
    return {"Authorization": f"Bearer {token}"}


async def team_oauth_headers(
    store: _VersionedAuthStore, team_id: str, source_id: str, server_url: str, *, rejected_token: str | None = None
) -> dict[str, str]:
    """Bearer header for a team OAuth source (see ``oauth_grant_headers``)."""
    return await oauth_grant_headers(
        oauth_broker(store), _grant_key(team_id, source_id), _TeamSourceGrant(store, team_id, source_id), server_url, rejected_token=rejected_token
    )


def team_oauth_header_source(
    store: _VersionedAuthStore, team_id: str, source_id: str, server_url: str
) -> Callable[[str | None], Awaitable[dict[str, str]]]:
    """Header source for ``RemoteMcpToolClient``: called before each connection, and once more with the
    refused token when the server rejects it during ``initialize``."""

    async def headers(rejected_token: str | None) -> dict[str, str]:
        return await team_oauth_headers(store, team_id, source_id, server_url, rejected_token=rejected_token)

    return headers


def prime_team_oauth(store: Any, team_id: str, source_id: str, token: OAuthToken) -> None:
    """Use the access token of a source that was just authorized instead of refreshing at once."""
    oauth_broker(store).prime(_grant_key(team_id, source_id), token.access_token, token.expires_in)


def oauth_sign_in_ended(auth: dict[str, Any] | None) -> bool:
    """True when the provider refused to renew the stored grant and an administrator must authorize again."""
    state = (auth or {}).get(_GRANT_STATE_KEY)
    return isinstance(state, dict) and bool(state.get("ended"))
