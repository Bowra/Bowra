"""tools sprint WP2b — OAuth 2.1 for remote MCP sources.

Remote MCP servers (GitHub / Linear / Notion / Atlassian / Slack …) almost all require OAuth.
This module backs the MCP SDK's OAuth 2.1 client with Bowhead's Fernet-encrypted, team-scoped
credential store, and builds the runtime ``httpx2.Auth`` provider.

Two pieces:
  - ``FernetTokenStorage`` — the SDK's ``TokenStorage`` protocol backed by the team tool source's
    ``auth_ref`` (Fernet ciphertext, team-scoped; the token set / dynamic-client-registration record
    are NEVER stored plaintext and NEVER logged). Reads decrypt via the store; writes re-encrypt.
  - ``build_mcp_oauth_provider`` — the SDK's ``OAuthClientProvider`` (a self-refreshing ``httpx2.Auth``).
    At RUNTIME we pass NO redirect/callback handlers, so it uses + refreshes stored tokens but CANNOT
    perform the interactive authorization dance headlessly → a never-authorized source fails closed.
    The interactive authorization (initiate + callback) is driven server-side by the gateway route.

Security notes: the access/refresh tokens live only in the Fernet store + in-memory; the guarded
httpx2 transport (SSRF pin-to-IP, follow_redirects=False) still applies to every OAuth + tool request.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlencode, urljoin, urlparse

import httpx2
from mcp.client.auth.exceptions import OAuthTokenError
from mcp.client.auth.oauth2 import OAuthClientProvider
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
)
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.shared.auth_utils import resource_url_from_server_url

from bowhead_core.ssrf_guard import build_guarded_async_client, validate_url

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

# where the token set + dynamic-client-registration record live inside the (Fernet-encrypted) auth dict
_TOKENS_KEY = "oauth_tokens"
_CLIENT_KEY = "oauth_client_info"


class _AuthStore(Protocol):
    async def get_team_tool_auth_decrypted(self, team_id: str, source_id: str) -> dict[str, Any] | None: ...
    async def update_team_tool_source_auth(self, team_id: str, source_id: str, *, auth_type: str, auth: dict[str, Any]) -> None: ...


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


def mcp_oauth_client_metadata(redirect_uri: str) -> OAuthClientMetadata:
    """Public-client (PKCE, no secret) metadata for dynamic registration against an MCP auth server."""
    return OAuthClientMetadata(
        client_name="Bowhead",
        redirect_uris=[redirect_uri],  # type: ignore[list-item]
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


class _RuntimeOAuthClientProvider(OAuthClientProvider):
    """Refresh stored credentials after discovery instead of starting browser authorization."""

    async def _perform_authorization(self) -> httpx2.Request:
        # Recreated SDK sessions do not know when the persisted access token expires.
        return await self._refresh_token()

    async def _handle_token_response(self, response: httpx2.Response) -> None:
        if not await self._handle_refresh_response(response):
            raise OAuthTokenError("MCP token refresh failed; authorize the connection again")


def build_mcp_oauth_provider(
    server_url: str,
    storage: FernetTokenStorage,
    *,
    redirect_uri: str = "http://localhost/oauth/callback",
    redirect_handler: Callable[[str], Awaitable[None]] | None = None,
    callback_handler: Callable[[], Awaitable[AuthorizationCodeResult]] | None = None,
) -> OAuthClientProvider:
    """Build the SDK's OAuthClientProvider (self-refreshing httpx2.Auth). At RUNTIME leave the handlers
    None (fail-closed if never authorized); the redirect_uri placeholder is unused during refresh."""
    provider = _RuntimeOAuthClientProvider if redirect_handler is None and callback_handler is None else OAuthClientProvider
    return provider(
        server_url=server_url,
        client_metadata=mcp_oauth_client_metadata(redirect_uri),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
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


async def probe_mcp_oauth(server_url: str, *, timeout: float = 6.0) -> tuple[bool, str | None]:
    """Side-effect-FREE probe (WP2b-3c): does this MCP server require OAuth? Returns
    ``(needs_oauth, provider_hint)``. Used by ``/preview`` to route an OAuth server into the
    先建后授权 flow instead of failing 'could not connect'. NO registration, NO storage, NO tokens —
    just an unauthenticated probe: a 401 (optionally pointing at a resource_metadata URL) CONFIRMED by
    discoverable protected-resource metadata (RFC 9728) advertising an authorization server. A bare 401
    is NOT enough — a static bearer/api_key/basic MCP server also 401s but does NOT do OAuth, so we
    require the PRM signal (review P2 — else static-auth servers get misrouted into the OAuth wizard).
    ``provider_hint`` = the authorization server's host for the UI's ``{provider}`` label. SSRF-guarded.
    ``timeout`` is an OVERALL deadline for the whole probe (GET + up to a few discovery requests), so a
    hanging server can't stack N × per-request timeouts onto an already-slow preview (review P3)."""
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
                    return (False, None)
                for url in build_protected_resource_metadata_discovery_urls(www_auth, server_url):
                    try:
                        resp = await client.send(create_oauth_metadata_request(url))
                        prm = await handle_protected_resource_response(resp)
                    except Exception:
                        prm = None
                    if prm and prm.authorization_servers:
                        return (True, urlparse(str(prm.authorization_servers[0])).hostname)
    except Exception:  # incl. asyncio.timeout's TimeoutError → best-effort, fall through to (False, None)
        return (False, None)
    # only a discoverable PRM advertising an authorization server counts as "needs OAuth".
    return (False, None)


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

    client_info = await storage.get_client_info()
    if client_info is None:
        reg_req = create_client_registration_request(asm, client_metadata, _base_url(server_url))
        reg_resp = await client.send(reg_req)
        client_info = await handle_registration_response(reg_resp)
        await storage.set_client_info(client_info)
    return asm, prm, client_info


async def begin_mcp_oauth(
    server_url: str,
    storage: FernetTokenStorage,
    redirect_uri: str,
    *,
    state: str,
    code_challenge: str,
    timeout: float = 10.0,
) -> OAuthBeginResult:
    """Discover + register (persisting client_info via ``storage``) and build the authorization
    URL for the admin's browser. ``state`` (CSRF, server-issued) and ``code_challenge`` (S256 of
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
        asm, prm, client_info = await _discover_and_register(server_url, client, storage, client_metadata, www_auth, www_auth_scope)

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
    await storage.set_tokens(token)
    return token
