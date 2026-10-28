"""Public browser URL helpers for deployments below a reverse-proxy path."""

from __future__ import annotations

import logging
import posixpath
from urllib.parse import urlsplit

from bowhead_core.public_entries import PublicEntry, normalize_public_base_url
from starlette.requests import Request

from bowhead_gateway.public_entry_context import (
    PublicAccessContext,
    _context_from_normalized,
    _first_active_entry_for_host,
    _unbound_request_context,
    current_public_access_context,
    current_request_public_access_context,
    set_public_access_context,
)


def restore_oauth_public_access_context(redirect_uri: str, *, callback_path: str) -> None:
    """Recover return paths from consumed server-side state, without granting entry binding."""
    context = current_request_public_access_context()
    if context is not None and context.prefix_known:
        return
    try:
        if not redirect_uri.endswith(callback_path):
            raise ValueError("OAuth callback path mismatch")
        normalized = normalize_public_base_url(redirect_uri.removesuffix(callback_path))
        if context is not None and context.match_source != "direct" and context.origin != normalized.origin:
            return
    except ValueError:
        logging.getLogger(__name__).warning("oauth.return_context_invalid")
        return
    set_public_access_context(_context_from_normalized(normalized, "oauth_state", entry=None))


def oauth_return_path(path: str, *, callback_path: str) -> str:
    """Keep even invalid-state returns on an unknown browser entry prefix."""
    public_path = public_app_path(path)
    context = current_request_public_access_context()
    if context is not None and context.prefix_known:
        return public_path
    target = urlsplit(path)
    relative = posixpath.relpath(target.path, start=posixpath.dirname(callback_path))
    if target.path.endswith("/"):
        relative += "/"
    return f"{relative}?{target.query}" if target.query else relative


class PublicEntryRequiredError(RuntimeError):
    """An external link needs a selected entry or an identified public address."""


def public_entry_required_detail() -> dict[str, str]:
    return {
        "code": "public_entry_required",
        "message": "Cannot create an external link because the public address is unavailable. Open the team's application URL and try again, or ask the team owner to verify its public entry.",
        "action": "configure_public_entry",
    }


async def team_public_entry(
    request: Request,
    *,
    team_id: str,
    entry_id: str | None = None,
) -> PublicEntry:
    """Use a pinned entry, the current team's matching entry, or its default."""
    from bowhead_core.public_entries import PublicEntryStore

    store = getattr(request.app.state, "public_entry_store", None)
    if store is None:
        user_store = getattr(request.app.state, "user_store", None)
        db_path = getattr(user_store, "db_path", None)
        if not db_path:
            raise PublicEntryRequiredError
        store = PublicEntryStore(db_path)
        await store.initialize()
        request.app.state.public_entry_store = store
    if entry_id is not None:
        entry = await store.get_for_team(team_id, entry_id)
    else:
        entry = None
        context = current_public_access_context()
        if context is not None:
            if context.entry_id and context.team_id == team_id:
                entry = await store.get_for_team(team_id, context.entry_id)
                if entry is not None and entry.status != "active":
                    entry = None
            if entry is None:
                address = current_request_public_access_context() or context
                entry = await _first_active_entry_for_host(store, normalize_public_base_url(address.base_url), team_id=team_id)
        if entry is None:
            entry = await store.default_for_team(team_id)
    if entry is None or entry.status != "active":
        raise PublicEntryRequiredError
    return entry


async def team_public_app_url(request: Request, *, team_id: str, path: str, entry_id: str | None = None) -> str:
    context = await team_public_access_context(request, team_id=team_id, entry_id=entry_id)
    return public_app_url_for_base(context.base_url, path)


async def team_public_access_context(request: Request, *, team_id: str, entry_id: str | None = None) -> PublicAccessContext:
    """Select a target-team external URL; request fallback never grants binding."""
    from bowhead_gateway.public_entry_context import public_access_context_for_entry

    try:
        entry = await team_public_entry(request, team_id=team_id, entry_id=entry_id)
        return public_access_context_for_entry(entry)
    except PublicEntryRequiredError:
        if entry_id is not None:
            raise
    except Exception as exc:
        logging.getLogger(__name__).exception("public_entry.link_metadata_unavailable team_id=%s", team_id)
        if entry_id is not None:
            raise PublicEntryRequiredError from exc
    context = current_request_public_access_context()
    if context is None and hasattr(request, "scope"):
        context = _unbound_request_context(request.scope)
    if context is None or not context.prefix_known:
        raise PublicEntryRequiredError
    return _context_from_normalized(normalize_public_base_url(context.base_url), "browser_hint", entry=None)


def public_app_path(path: str) -> str:
    """Return a browser-visible app path without changing internal route matching.

    Reverse proxies commonly strip a registered entry's path prefix before
    forwarding a request to the gateway. Gateway routes therefore continue to
    use ``/api/...`` internally, while browser-visible paths restore the
    current entry prefix.
    """
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("public app path must start with one slash")
    prefix = _base_path()
    if not prefix or path == prefix or path.startswith((f"{prefix}/", f"{prefix}?")):
        return path
    return f"{prefix}/" if path == "/" else f"{prefix}{path}"


def public_app_base_url() -> str:
    """Return the canonical public Bowhead URL without a trailing slash."""
    context = current_public_access_context()
    if context is None or (context.entry_id is None and not context.prefix_known):
        raise PublicEntryRequiredError("a resolved public entry or complete browser address is required to generate an absolute URL")
    return context.base_url


def public_app_url(path: str) -> str:
    """Build a public URL while preserving any configured URI prefix."""
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("public app path must start with one slash")
    return public_app_url_for_base(public_app_base_url(), path)


def public_app_url_for_base(base_url: str, path: str) -> str:
    """Build an internal path below an explicit registered public base URL."""
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("public app path must start with one slash")
    normalized = normalize_public_base_url(base_url)
    return f"{normalized.base_url}{path}"


def _base_path() -> str:
    context = current_request_public_access_context()
    return context.base_path if context is not None and context.prefix_known else ""
