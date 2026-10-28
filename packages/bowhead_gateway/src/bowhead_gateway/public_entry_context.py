"""Trusted request-to-public-entry resolution and continuity cookie."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from contextlib import suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote, unquote, urlsplit

from bowhead_core.public_entries import (
    NormalizedPublicUrl,
    PublicEntry,
    PublicEntryError,
    PublicEntryStore,
    normalize_public_base_url,
)

from bowhead_gateway.http_request_context import request_id_from_scope

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

MatchSource = Literal[
    "entry_id",
    "forwarded_url",
    "direct",
    "cookie",
    "browser_hint",
    "team_default",
    "oauth_state",
]

#: Public-address hints a proxy may send. **Any one present** means configuration supplied an address;
#: evaluate that path rather than the fallback for a proxy supplying no address information.
FORWARDED_HINT_HEADERS = (
    b"x-bowhead-public-entry",
    b"x-forwarded-proto",
    b"x-forwarded-host",
    b"x-forwarded-port",
    b"x-forwarded-prefix",
)

COOKIE_NAME = "bowhead_public_context"
COOKIE_TTL_SECONDS = 15 * 60
_COOKIE_DOMAIN = b"bowhead/public-context/v1"
_ENTRY_ID_RE = re.compile(r"pe_[a-f0-9]{24}")
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_PATH_SAFE = "/!$&'()*+,;=:@-._~"
_CONTEXT: ContextVar[PublicAccessContext | None] = ContextVar(
    "bowhead_public_access_context",
    default=None,
)
logger = logging.getLogger(__name__)


class PublicAccessError(ValueError):
    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PublicAccessContext:
    base_url: str
    origin: str
    base_path: str
    app_root: str
    match_source: MatchSource
    entry_id: str | None = None
    team_id: str | None = None
    request_base_url: str | None = None
    prefix_known: bool | None = None
    request_match_source: MatchSource | None = None

    def __post_init__(self) -> None:
        if self.prefix_known is None:
            object.__setattr__(self, "prefix_known", not (self.match_source == "direct" and self.entry_id is None and not self.base_path))

    def to_browser_dict(self) -> dict[str, str | None]:
        normalized = normalize_public_base_url(self.request_base_url or self.base_url)
        coherent = normalized.base_url == self.base_url
        return {
            "entryId": self.entry_id if coherent else None,
            "teamId": self.team_id if coherent else None,
            "baseUrl": normalized.base_url,
            "basePath": normalized.base_path,
            "matchSource": self.request_match_source or self.match_source,
        }


def current_public_access_context() -> PublicAccessContext | None:
    return _CONTEXT.get()


def current_request_public_access_context() -> PublicAccessContext | None:
    """Return address evidence without replacing it with a team's selected URL."""
    context = current_public_access_context()
    if context is None:
        return None
    if context.request_base_url is None:
        return None if context.match_source == "team_default" else context
    address = _context_from_normalized(
        normalize_public_base_url(context.request_base_url), context.request_match_source or context.match_source, entry=None
    )
    return replace(address, prefix_known=context.prefix_known)


def set_public_access_context(context: PublicAccessContext | None) -> Token[PublicAccessContext | None]:
    return _CONTEXT.set(context)


def reset_public_access_context(token: Token[PublicAccessContext | None]) -> None:
    _CONTEXT.reset(token)


async def bind_team_public_entry(
    request: Request,
    *,
    team_id: str,
) -> PublicAccessContext | None:
    """Bind optional entry metadata without gating authenticated team access."""
    try:
        return await _bind_team_public_entry(request, team_id=team_id)
    except Exception:
        logger.exception("public_entry.team_binding_unavailable team_id=%s", team_id)
        return current_public_access_context()


async def _bind_team_public_entry(
    request: Request,
    *,
    team_id: str,
) -> PublicAccessContext | None:
    """Upgrade an unbound public URL to the current team's matching entry."""
    context = current_public_access_context()
    store = getattr(request.app.state, "public_entry_store", None)
    if not isinstance(store, PublicEntryStore):
        return context
    if context is None:
        entry = await store.default_for_team(team_id)
        if entry is None:
            return None
        bound = replace(_context_from_entry(entry, "team_default"), prefix_known=False)
        set_public_access_context(bound)
        request.scope.setdefault("state", {})["public_access_context"] = bound
        return bound
    if context.entry_id:
        entry = await store.get(context.entry_id)
        if entry is not None and entry.team_id == team_id and entry.status == "active":
            return context
        if context.match_source == "entry_id":
            return context
    normalized = normalize_public_base_url(context.request_base_url or context.base_url)
    entry = await _first_active_entry_for_host(store, normalized, team_id=team_id)
    if entry is None:
        entry = await store.default_for_team(team_id)
        source: MatchSource = "team_default"
    else:
        source = context.match_source
    if entry is None:
        return context
    bound = replace(
        _context_from_entry(entry, source),
        request_base_url=context.request_base_url or context.base_url,
        prefix_known=context.prefix_known,
        request_match_source=context.request_match_source or context.match_source,
    )
    set_public_access_context(bound)
    request.scope.setdefault("state", {})["public_access_context"] = bound
    return bound


async def resolve_public_access_context(
    scope: Scope,
    store: PublicEntryStore,
) -> PublicAccessContext | None:
    """Resolve registered entries from the request's public address."""
    if scope.get("type") not in {"http", "websocket"}:
        return None
    try:
        forwarded_present = any(_header_values(scope, name) for name in FORWARDED_HINT_HEADERS)
        try:
            hinted = await _resolve_browser_hint(scope, store)
        except (PublicAccessError, PublicEntryError):
            hinted = None
        if forwarded_present:
            try:
                forwarded = await _resolve_forwarded(scope, store)
            except PublicAccessError:
                # Some proxies preserve the public host but omit their path
                # prefix. A browser request carries its canonical public base
                # explicitly; use that registered entry rather than dropping
                # the context altogether.
                forwarded = None
            if forwarded is not None and forwarded.entry_id is not None:
                address = hinted
                if address is None and not _header_values(scope, b"x-forwarded-prefix"):
                    restored = await _restore_cookie(scope, store)
                    if restored is not None and restored.origin == normalize_public_base_url(forwarded.request_base_url or forwarded.base_url).origin:
                        address = restored
                if address is not None and not _header_values(scope, b"x-forwarded-prefix"):
                    # Host matching selects the Team's external-link entry,
                    # but cannot identify a proxy-stripped browser prefix.
                    return replace(
                        forwarded,
                        request_base_url=address.request_base_url or address.base_url,
                        request_match_source=address.request_match_source or address.match_source,
                        prefix_known=address.prefix_known,
                    )
                return forwarded
            if hinted is not None:
                return hinted
            if forwarded is not None:
                if not forwarded.prefix_known:
                    restored = await _restore_cookie(scope, store)
                    if restored is not None and restored.origin == normalize_public_base_url(forwarded.request_base_url or forwarded.base_url).origin:
                        return restored
                return forwarded

            # A path-stripping proxy cannot describe the browser-visible
            # prefix on a top-level navigation. Reuse the short-lived signed
            # browser context set by the preceding page/API response before
            # falling back to the proxy's internal direct address.
            restored = await _restore_cookie(scope, store)
            if restored is not None:
                return restored

        if hinted is not None:
            return hinted

        if _header_values(scope, b"host"):
            direct = await _resolve_direct(scope, store)
            if direct is not None and direct.entry_id is not None:
                restored = await _restore_cookie(scope, store)
                # A host-only match cannot prove which prefix a proxy stripped.
                # A visible registered path still takes precedence over cookies.
                if (
                    restored is not None
                    and restored.origin == direct.origin
                    and (not direct.base_path or not _path_matches(_canonical_request_path(scope), direct.base_path))
                ):
                    return replace(
                        direct,
                        request_base_url=restored.base_url,
                        request_match_source=restored.match_source,
                        prefix_known=restored.prefix_known,
                    )
                return direct
            restored = await _restore_cookie(scope, store)
            return restored or direct
        return await _restore_cookie(scope, store)
    except (PublicAccessError, PublicEntryError):
        # Public entry resolution is optional. Invalid or stale browser/proxy
        # metadata must not turn an otherwise valid application request into a
        # 4xx response; downstream URL producers use the team's default entry.
        return None


class PublicEntryContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        app = scope.get("app")
        store = getattr(getattr(app, "state", None), "public_entry_store", None)
        registry_available = isinstance(store, PublicEntryStore)
        try:
            context = await resolve_public_access_context(scope, store) if registry_available else _unbound_request_context(scope)
        except (PublicAccessError, PublicEntryError):
            # Public entries are optional routing metadata. A malformed, stale,
            # or unavailable entry candidate must never reject application
            # traffic; authenticated routes bind the team's default entry only
            # when they need to generate an absolute URL.
            context = _unbound_request_context(scope)
        except Exception:
            # Public-entry data only improves generated external URLs. A
            # registry outage must never become an application availability
            # gate, so continue without entry metadata.
            logger.exception(
                "public_entry.store_unavailable http_request_id=%s path=%s peer=%s",
                request_id_from_scope(scope),
                scope.get("path"),
                _peer_ip(scope),
            )
            registry_available = False
            context = _unbound_request_context(scope)

        state = scope.setdefault("state", {})
        state["public_access_context"] = context
        if (
            context is not None
            and context.match_source == "direct"
            and context.base_path
            and _path_matches(_canonical_request_path(scope), context.base_path)
        ):
            scope = dict(scope)
            public_path = _canonical_request_path(scope)
            internal_raw_path = public_path[len(context.base_path) :] or "/"
            scope["path"] = unquote(internal_raw_path)
            scope["raw_path"] = internal_raw_path.encode("ascii")
            scope["state"] = state
        token = set_public_access_context(context)
        try:
            if scope.get("type") != "http" or context is None or not registry_available:
                await self.app(scope, receive, send)
                return

            async def send_with_cookie(message: Message) -> None:
                if message["type"] == "http.response.start":
                    response_context = current_public_access_context() or context
                    cookie = _cookie_header(response_context)
                    if cookie is not None:
                        headers = list(message.get("headers", []))
                        headers.append((b"set-cookie", cookie.encode("latin-1")))
                        message["headers"] = headers
                await send(message)

            await self.app(scope, receive, send_with_cookie)
        finally:
            reset_public_access_context(token)


def _unbound_request_context(scope: Scope) -> PublicAccessContext | None:
    """Retain canonical address evidence during an outage, never entry/team binding."""
    try:
        hint = _single_header(scope, b"x-bowhead-public-base", required=False)
        if hint:
            return _context_from_normalized(normalize_public_base_url(hint), "browser_hint", entry=None)
    except (PublicAccessError, PublicEntryError):
        pass
    cookie_context = None
    cookies = _cookie_values(scope, COOKIE_NAME)
    if len(cookies) == 1:
        payload = _decode_cookie(cookies[0])
        if payload is not None:
            with suppress(PublicEntryError):
                cookie_context = _context_from_normalized(normalize_public_base_url(str(payload.get("base_url") or "")), "cookie", entry=None)
    try:
        forwarded = _forwarded_base_url(
            proto=_single_header(scope, b"x-forwarded-proto", required=True),
            host_header=_single_header(scope, b"x-forwarded-host", required=True),
            port_header=_single_header(scope, b"x-forwarded-port", required=False),
            prefix=_single_header(scope, b"x-forwarded-prefix", required=False),
        )
        if cookie_context is not None and cookie_context.origin == forwarded.origin and not _header_values(scope, b"x-forwarded-prefix"):
            return cookie_context
        return replace(
            _context_from_normalized(forwarded, "forwarded_url", entry=None), prefix_known=bool(_header_values(scope, b"x-forwarded-prefix"))
        )
    except PublicAccessError:
        pass
    if cookie_context is not None:
        return cookie_context
    try:
        direct = _forwarded_base_url(
            proto=str(scope.get("scheme") or ""),
            host_header=_single_header(scope, b"host", required=True),
            port_header="",
            prefix="",
        )
        return replace(_context_from_normalized(direct, "direct", entry=None), prefix_known=False)
    except PublicAccessError:
        return None


async def _resolve_forwarded(
    scope: Scope,
    store: PublicEntryStore,
) -> PublicAccessContext:
    proto = _single_header(scope, b"x-forwarded-proto", required=True).lower()
    host_header = _single_header(scope, b"x-forwarded-host", required=True)
    prefix = _single_header(scope, b"x-forwarded-prefix", required=False)
    port_header = _single_header(scope, b"x-forwarded-port", required=False)
    entry_id = _single_header(scope, b"x-bowhead-public-entry", required=False)
    if proto not in {"http", "https"}:
        raise PublicAccessError(400, "invalid forwarded proto")
    normalized = _forwarded_base_url(
        proto=proto,
        host_header=host_header,
        port_header=port_header,
        prefix=prefix,
    )
    if entry_id and _ENTRY_ID_RE.fullmatch(entry_id):
        entry = await store.get(entry_id)
        if entry is not None and entry.status == "active" and entry.base_url == normalized.base_url:
            return replace(_context_from_entry(entry, "entry_id"), request_base_url=normalized.base_url)
    entry = await _first_active_entry_for_host(store, normalized)
    explicit_prefix = bool(_header_values(scope, b"x-forwarded-prefix"))
    if entry is None:
        return replace(_context_from_normalized(normalized, "forwarded_url", entry=None), prefix_known=explicit_prefix)
    address = normalized.base_url if explicit_prefix else f"{normalized.origin}{entry.base_path}"
    return replace(_context_from_entry(entry, "forwarded_url"), request_base_url=address, prefix_known=True)


async def _resolve_direct(
    scope: Scope,
    store: PublicEntryStore,
) -> PublicAccessContext | None:
    scheme = str(scope.get("scheme") or "").lower()
    if scheme not in {"http", "https"}:
        return None
    host_header = _single_header(scope, b"host", required=True)
    origin = _forwarded_base_url(
        proto=scheme,
        host_header=host_header,
        port_header="",
        prefix="/",
    ).origin
    normalized_origin = normalize_public_base_url(origin)
    request_path = _canonical_request_path(scope)
    for entry in await store.list_all():
        if entry.status != "pending_verification":
            continue
        normalized = normalize_public_base_url(entry.base_url)
        challenge_path = f"{entry.base_path}/.well-known/bowhead-public-entry/{entry.id}"
        if normalized.origin == origin and request_path in {challenge_path, f"{challenge_path}/verify"}:
            return _context_from_normalized(normalized, "direct", entry=None)
    entry = await _first_active_entry_for_host(store, normalized_origin)
    if entry is None:
        return replace(_context_from_normalized(normalized_origin, "direct", entry=None), prefix_known=False)
    return _context_from_entry(entry, "direct")


async def _restore_cookie(
    scope: Scope,
    store: PublicEntryStore,
) -> PublicAccessContext | None:
    values = _cookie_values(scope, COOKIE_NAME)
    if len(values) != 1:
        return None
    payload = _decode_cookie(values[0])
    if payload is None:
        return None
    try:
        normalized = normalize_public_base_url(str(payload["base_url"]))
    except (KeyError, PublicEntryError):
        return None
    entry_id = payload.get("entry_id")
    if entry_id:
        entry = await store.get(str(entry_id))
        if entry is None or entry.status != "active" or entry.base_url != normalized.base_url or payload.get("team_id") != entry.team_id:
            return _context_from_normalized(normalized, "cookie", entry=None)
        return _context_from_entry(entry, "cookie")
    return _context_from_normalized(normalized, "cookie", entry=None)


async def _resolve_browser_hint(
    scope: Scope,
    store: PublicEntryStore,
) -> PublicAccessContext | None:
    """Resolve the browser-visible base, preferring a matching active entry."""
    base_url = _single_header(
        scope,
        b"x-bowhead-public-base",
        required=False,
    )
    if not base_url:
        return None
    try:
        normalized = normalize_public_base_url(base_url)
    except PublicEntryError as exc:
        raise PublicAccessError(400, str(exc)) from exc
    entry = await _first_active_entry_for_host(store, normalized)
    if entry is None:
        return _context_from_normalized(normalized, "browser_hint", entry=None)
    return replace(_context_from_entry(entry, "browser_hint"), request_base_url=normalized.base_url)


async def _first_active_entry_for_host(
    store: PublicEntryStore,
    request_url: NormalizedPublicUrl,
    *,
    team_id: str | None = None,
) -> PublicEntry | None:
    """Return the first active entry matching host+port, then host.

    ``PublicEntryStore.list_active`` provides the stable ordering that defines
    "first".  The entry's complete base URL remains authoritative for all URLs
    emitted after selection.
    """
    entries = await store.list_active()
    if team_id is not None:
        entries = [entry for entry in entries if entry.team_id == team_id]
    normalized_entries = [(entry, normalize_public_base_url(entry.base_url)) for entry in entries]
    for entry, normalized in normalized_entries:
        if normalized.host == request_url.host and normalized.port == request_url.port:
            return entry
    for entry, normalized in normalized_entries:
        if normalized.host == request_url.host:
            return entry
    return None


def _forwarded_base_url(
    *,
    proto: str,
    host_header: str,
    port_header: str,
    prefix: str,
):
    if any(value.strip() != value or "," in value for value in (host_header, prefix, port_header)):
        raise PublicAccessError(400, "forwarded headers must be canonical single values")
    probe = urlsplit(f"{proto}://{host_header}")
    if not probe.hostname or probe.username is not None or probe.password is not None or probe.path or probe.query or probe.fragment:
        raise PublicAccessError(400, "invalid forwarded host")
    try:
        host_port = probe.port
    except ValueError as exc:
        raise PublicAccessError(400, "invalid forwarded host port") from exc
    explicit_port: int | None = None
    if port_header:
        if not port_header.isdigit():
            raise PublicAccessError(400, "invalid forwarded port")
        explicit_port = int(port_header)
        if not 1 <= explicit_port <= 65535:
            raise PublicAccessError(400, "invalid forwarded port")
    if host_port is not None and explicit_port is not None and host_port != explicit_port:
        raise PublicAccessError(400, "forwarded host and port disagree")
    port = explicit_port if explicit_port is not None else host_port
    host = probe.hostname
    rendered_host = f"[{host}]" if ":" in host else host
    rendered_port = f":{port}" if port is not None else ""
    normalized_prefix = "" if prefix in {"", "/"} else prefix
    try:
        return normalize_public_base_url(
            f"{proto}://{rendered_host}{rendered_port}{normalized_prefix}",
        )
    except PublicEntryError as exc:
        raise PublicAccessError(400, str(exc)) from exc


def _single_header(scope: Scope, name: bytes, *, required: bool) -> str:
    values = _header_values(scope, name)
    if len(values) > 1:
        raise PublicAccessError(400, f"duplicate {name.decode('ascii')} header")
    if not values:
        if required:
            raise PublicAccessError(400, f"missing {name.decode('ascii')} header")
        return ""
    value = values[0]
    if not value or "," in value or "\r" in value or "\n" in value:
        raise PublicAccessError(400, f"invalid {name.decode('ascii')} header")
    return value


def _header_values(scope: Scope, name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == name]


def _peer_ip(scope: Scope) -> str | None:
    client = scope.get("client")
    if not client or not client[0]:
        return None
    return str(client[0])


def _path_matches(request_path: str, base_path: str) -> bool:
    if not base_path:
        return request_path.startswith("/")
    return request_path == base_path or request_path.startswith(f"{base_path}/")


def _canonical_request_path(scope: Scope) -> str:
    """Render the ASGI request path in the registry's percent-encoded form."""
    raw_path = scope.get("raw_path")
    if isinstance(raw_path, bytes):
        try:
            path = raw_path.decode("ascii")
        except UnicodeDecodeError as exc:
            raise PublicAccessError(400, "request path must use ASCII percent encoding") from exc
    else:
        path = quote(str(scope.get("path") or "/"), safe=_PATH_SAFE)
    return _PERCENT_ESCAPE_RE.sub(
        lambda match: match.group(0).upper(),
        path,
    )


def _is_challenge_path(path: str, entry_id: str) -> bool:
    return path == f"/.well-known/bowhead-public-entry/{entry_id}" or path == (f"/.well-known/bowhead-public-entry/{entry_id}/verify")


def _context_from_entry(entry: PublicEntry, source: MatchSource) -> PublicAccessContext:
    normalized = normalize_public_base_url(entry.base_url)
    return _context_from_normalized(normalized, source, entry=entry)


def public_access_context_for_entry(
    entry: PublicEntry,
    *,
    source: MatchSource = "entry_id",
) -> PublicAccessContext:
    """Build a canonical context without reconstructing IPv6 authorities by hand."""
    return _context_from_entry(entry, source)


def _context_from_normalized(
    normalized: NormalizedPublicUrl,
    source: MatchSource,
    *,
    entry: PublicEntry | None,
) -> PublicAccessContext:
    return PublicAccessContext(
        base_url=normalized.base_url,
        origin=normalized.origin,
        base_path=normalized.base_path,
        app_root=normalized.app_root,
        match_source=source,
        entry_id=entry.id if entry else None,
        team_id=entry.team_id if entry else None,
    )


def _cookie_values(scope: Scope, name: str) -> list[str]:
    values: list[str] = []
    for raw_cookie in _header_values(scope, b"cookie"):
        for part in raw_cookie.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key == name:
                values.append(value)
    return values


def _cookie_header(context: PublicAccessContext) -> str | None:
    if not context.prefix_known:
        return None
    if context.request_base_url is not None and context.request_base_url != context.base_url:
        context = _context_from_normalized(normalize_public_base_url(context.request_base_url), "browser_hint", entry=None)
    # An unmatched direct Host can be an upstream address. Signing it at Path=/
    # would compete with the browser's later verified prefixed entry cookie.
    if context.match_source == "direct" and context.entry_id is None:
        return None
    encoded = _encode_cookie(context)
    if encoded is None:
        return None
    cookie = SimpleCookie()
    cookie[COOKIE_NAME] = encoded
    morsel = cookie[COOKIE_NAME]
    morsel["path"] = f"{context.base_path}/" if context.base_path else "/"
    morsel["max-age"] = str(COOKIE_TTL_SECONDS)
    morsel["httponly"] = True
    morsel["samesite"] = "Lax"
    if context.origin.startswith("https://"):
        morsel["secure"] = True
    return morsel.OutputString()


def _encode_cookie(context: PublicAccessContext) -> str | None:
    keys = _jwt_keys()
    if not keys:
        return None
    now = int(time.time())
    payload = {
        "version": 1,
        "base_url": context.base_url,
        "entry_id": context.entry_id,
        "team_id": context.team_id,
        "iat": now,
        "exp": now + COOKIE_TTL_SECONDS,
    }
    body = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
    )
    signature = _b64encode(hmac.new(_derived_key(keys[0]), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def _decode_cookie(value: str) -> dict[str, Any] | None:
    if not value.isascii():
        return None
    body, separator, signature = value.partition(".")
    if not separator:
        return None
    for key in _jwt_keys():
        expected = _b64encode(hmac.new(_derived_key(key), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            continue
        try:
            payload = json.loads(_b64decode(body))
        except (ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("exp"), int)
            or payload["exp"] < int(time.time())
        ):
            return None
        return payload
    return None


def _jwt_keys() -> list[bytes]:
    try:
        from bowhead_gateway.routes.auth import _jwt_secret_keys

        return [secret.encode("utf-8") for _kid, secret in _jwt_secret_keys()]
    except Exception:
        return []


def _derived_key(jwt_key: bytes) -> bytes:
    return hmac.new(jwt_key, _COOKIE_DOMAIN, hashlib.sha256).digest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}").decode("utf-8")
