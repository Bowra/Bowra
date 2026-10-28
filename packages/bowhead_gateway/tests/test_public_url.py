from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin

import pytest
from bowhead_core.public_entries import PublicEntry, PublicEntryStore
from bowhead_gateway.public_entry_context import (
    PublicAccessContext,
    reset_public_access_context,
    set_public_access_context,
)
from bowhead_gateway.public_url import PublicEntryRequiredError, public_app_path, public_app_url, team_public_app_url
from starlette.requests import Request
from starlette.responses import Response


@pytest.fixture
async def public_entry_store(tmp_path: Path) -> PublicEntryStore:
    store = PublicEntryStore(tmp_path / "public-entries.db")
    await store.initialize()
    return store


async def _active_entry(store: PublicEntryStore, *, team_id: str, base_url: str) -> PublicEntry:
    pending, challenge = await store.create_pending(team_id=team_id, base_url=base_url, actor_id="usr_owner")
    return await store.verify_challenge(entry_id=pending.id, token=challenge)


def _request(store: PublicEntryStore) -> Request:
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [], "app": SimpleNamespace(state=SimpleNamespace(public_entry_store=store))}
    )


@pytest.mark.asyncio
async def test_team_public_url_prefers_current_team_entry_over_default(public_entry_store: PublicEntryStore) -> None:
    await _active_entry(public_entry_store, team_id="team_a", base_url="https://default.example/bowhead")
    current = await _active_entry(public_entry_store, team_id="team_a", base_url="https://current.example:10010/bowhead")
    token = set_public_access_context(_entry_context(current.base_url, current.id))
    try:
        assert await team_public_app_url(_request(public_entry_store), team_id="team_a", path="/invite/") == (
            "https://current.example:10010/bowhead/invite/"
        )
    finally:
        reset_public_access_context(token)


@pytest.mark.asyncio
async def test_team_public_url_matches_current_host_when_upstream_port_is_wrong(public_entry_store: PublicEntryStore) -> None:
    await _active_entry(public_entry_store, team_id="team_a", base_url="https://default.example/bowhead")
    await _active_entry(public_entry_store, team_id="team_a", base_url="https://current.example:10010/bowhead")
    token = set_public_access_context(_context("http://current.example:8000"))
    try:
        assert await team_public_app_url(_request(public_entry_store), team_id="team_a", path="/invite/") == (
            "https://current.example:10010/bowhead/invite/"
        )
    finally:
        reset_public_access_context(token)


@pytest.mark.asyncio
async def test_team_public_url_falls_back_to_target_team_default(public_entry_store: PublicEntryStore) -> None:
    target = await _active_entry(public_entry_store, team_id="team_b", base_url="https://target.example/bowhead")
    other = await _active_entry(public_entry_store, team_id="team_a", base_url="https://other.example/bowhead")
    token = set_public_access_context(_entry_context(other.base_url, other.id))
    try:
        assert await team_public_app_url(_request(public_entry_store), team_id="team_b", path="/invite/") == (f"{target.base_url}/invite/")
    finally:
        reset_public_access_context(token)


@pytest.mark.asyncio
async def test_team_public_url_preserves_explicit_entry_id(public_entry_store: PublicEntryStore) -> None:
    pinned = await _active_entry(public_entry_store, team_id="team_a", base_url="https://pinned.example/bowhead")
    current = await _active_entry(public_entry_store, team_id="team_a", base_url="https://current.example/bowhead")
    token = set_public_access_context(_entry_context(current.base_url, current.id))
    try:
        assert await team_public_app_url(_request(public_entry_store), team_id="team_a", entry_id=pinned.id, path="/invite/") == (
            f"{pinned.base_url}/invite/"
        )
        with pytest.raises(PublicEntryRequiredError):
            await team_public_app_url(_request(public_entry_store), team_id="team_b", entry_id=pinned.id, path="/invite/")
    finally:
        reset_public_access_context(token)


def _context(base_url: str) -> PublicAccessContext:
    origin, _, suffix = base_url.partition("://")
    del origin
    authority, separator, path = suffix.partition("/")
    scheme = base_url.split("://", 1)[0]
    base_path = f"/{path}" if separator else ""
    return PublicAccessContext(
        base_url=base_url,
        origin=f"{scheme}://{authority}",
        base_path=base_path,
        app_root=f"{base_url}/",
        match_source="direct",
    )


def test_public_app_url_uses_current_entry_and_preserves_prefix() -> None:
    token = set_public_access_context(_context("https://domain.example/bowhead"))
    try:
        assert public_app_url("/invite?token=x") == "https://domain.example/bowhead/invite?token=x"
        assert public_app_path("/api/v1/auth/refresh") == "/bowhead/api/v1/auth/refresh"
    finally:
        reset_public_access_context(token)


def test_public_app_url_preserves_http_scheme_and_port() -> None:
    token = set_public_access_context(_context("http://domain.example:10010/bowhead"))
    try:
        assert public_app_url("/login/") == "http://domain.example:10010/bowhead/login/"
    finally:
        reset_public_access_context(token)


def test_absolute_public_url_requires_resolved_entry() -> None:
    token = set_public_access_context(None)
    try:
        with pytest.raises(RuntimeError, match="resolved public entry"):
            public_app_url("/invite")
    finally:
        reset_public_access_context(token)


def test_refresh_cookie_uses_browser_visible_auth_path() -> None:
    from bowhead_gateway.routes.auth import _set_refresh_cookie

    token = set_public_access_context(_context("https://domain.example/bowhead"))
    try:
        response = Response()
        _set_refresh_cookie(response, "refresh-token")
        # Changing Path also sends cleanup headers; scan all set-cookie headers for the one actually containing a value.
        header = next(h for h in _set_cookie_headers(response) if "refresh-token" in h)
        assert "Path=/bowhead/api/v1/auth" in header
    finally:
        reset_public_access_context(token)


# A TLS-terminating proxy can prevent public-entry matching and leave the request
# prefix unknown. A guessed narrow cookie path would then omit refresh cookies
# from prefixed requests. These cases verify the trusted-prefix fallback.


def _origin_only_context(origin: str) -> PublicAccessContext:
    """没匹配到登记入口时的兜底上下文：只有 origin，前缀不可知。"""
    return PublicAccessContext(
        base_url=origin,
        origin=origin,
        base_path="",
        app_root=f"{origin}/",
        match_source="direct",
    )


def _entry_context(base_url: str, entry_id: str) -> PublicAccessContext:
    context = _context(base_url)
    return PublicAccessContext(
        base_url=context.base_url,
        origin=context.origin,
        base_path=context.base_path,
        app_root=context.app_root,
        match_source="direct",
        entry_id=entry_id,
        team_id="team_x",
    )


def _set_cookie_headers(response: Response) -> list[str]:
    return [value.decode("latin-1") for key, value in response.raw_headers if key == b"set-cookie"]


def test_refresh_cookie_falls_back_to_root_when_prefix_unknown() -> None:
    """★ 前缀不可知时必须退回 `/`，不能猜一个必然收不到的窄 Path。"""
    from bowhead_gateway.routes.auth import _set_refresh_cookie

    token = set_public_access_context(_origin_only_context("http://chatbi-prd.example"))
    try:
        response = Response()
        _set_refresh_cookie(response, "refresh-token")
        header = next(h for h in _set_cookie_headers(response) if "bowhead_refresh=refresh-token" in h)
        assert "Path=/;" in header or header.endswith("Path=/")
        # Exactly the original implementation's value, never sent back in prefixed deployments.
        assert "Path=/api/v1/auth" not in header
    finally:
        reset_public_access_context(token)


def test_refresh_cookie_narrows_when_entry_says_there_is_no_prefix() -> None:
    """确实解析到登记入口、且该部署本来就没前缀（116 那种）→ 仍然收窄。"""
    from bowhead_gateway.routes.auth import _set_refresh_cookie

    token = set_public_access_context(_entry_context("http://116.example", "pe_abc"))
    try:
        response = Response()
        _set_refresh_cookie(response, "refresh-token")
        header = next(h for h in _set_cookie_headers(response) if "bowhead_refresh=refresh-token" in h)
        assert "Path=/api/v1/auth" in header
    finally:
        reset_public_access_context(token)


def test_refresh_cookie_clears_other_candidate_paths() -> None:
    """★ 换 Path 时必须清掉另一份，否则两份并存、服务端只读到其中一个。"""
    from bowhead_gateway.routes.auth import _set_refresh_cookie

    token = set_public_access_context(_context("https://domain.example/bowhead"))
    try:
        response = Response()
        _set_refresh_cookie(response, "refresh-token")
        headers = [h for h in _set_cookie_headers(response) if "bowhead_refresh=" in h]
        assert any("Path=/bowhead/api/v1/auth" in h and "refresh-token" in h for h in headers)
        cleared = [h for h in headers if "refresh-token" not in h]
        assert cleared, "换 Path 时没有清理其它候选 Path"
        assert any("Path=/;" in h or h.endswith("Path=/") for h in cleared)
    finally:
        reset_public_access_context(token)


def test_auth_cookies_are_secure_when_public_entry_is_https(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browser-visible HTTPS requires Secure on both authentication cookies."""
    from bowhead_gateway.routes.auth import _set_refresh_cookie

    monkeypatch.delenv("BOWHEAD_PROD", raising=False)
    token = set_public_access_context(_context("https://domain.example/bowhead"))
    try:
        response = Response()
        _set_refresh_cookie(response, "refresh-token")
        header = next(h for h in _set_cookie_headers(response) if "refresh-token" in h)
        assert "Secure" in header
    finally:
        reset_public_access_context(token)


def test_auth_cookies_stay_plain_on_http_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """反向对照：入口是 http（116 就是纯 HTTP）时不得强加 Secure，否则 cookie 直接不落地。"""
    from bowhead_gateway.routes.auth import _set_refresh_cookie

    monkeypatch.delenv("BOWHEAD_PROD", raising=False)
    token = set_public_access_context(_context("http://116.example"))
    try:
        response = Response()
        _set_refresh_cookie(response, "refresh-token")
        header = next(h for h in _set_cookie_headers(response) if "refresh-token" in h)
        assert "Secure" not in header
    finally:
        reset_public_access_context(token)


@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
def test_consumed_oauth_address_restores_prefix_without_binding_permissions(prefix: str) -> None:
    from bowhead_gateway.public_entry_context import current_public_access_context
    from bowhead_gateway.public_url import oauth_return_path, restore_oauth_public_access_context

    token = set_public_access_context(_context("http://internal.example:8000"))
    callback = "/api/v1/google/oauth/callback"
    try:
        restore_oauth_public_access_context(f"https://public.example:10010{prefix}{callback}", callback_path=callback)
        context = current_public_access_context()
        assert context is not None
        assert context.entry_id is None and context.team_id is None
        assert oauth_return_path("/connections/?google=connected", callback_path=callback) == f"{prefix}/connections/?google=connected"
    finally:
        reset_public_access_context(token)


@pytest.mark.parametrize("source", ["browser_hint", "forwarded_url", "entry_id"])
def test_oauth_state_does_not_override_identified_request_address(source: str) -> None:
    from dataclasses import replace

    from bowhead_gateway.public_url import oauth_return_path, restore_oauth_public_access_context

    context = replace(_context("https://current.example/current"), match_source=source, entry_id="pe_known" if source == "entry_id" else None)
    token = set_public_access_context(context)
    try:
        callback = "/api/v1/google/oauth/callback"
        restore_oauth_public_access_context(f"https://other.example/old{callback}", callback_path=callback)
        assert oauth_return_path("/connections/", callback_path=callback) == "/current/connections/"
    finally:
        reset_public_access_context(token)


@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
def test_invalid_state_return_keeps_unknown_browser_prefix(prefix: str) -> None:
    from bowhead_gateway.public_url import oauth_return_path

    token = set_public_access_context(_context("http://internal.example:8000"))
    callback = "/api/v1/team-tool-sources/oauth/callback"
    try:
        target = oauth_return_path("/settings/tools?oauth=error", callback_path=callback)
        assert (
            urljoin(f"https://public.example{prefix}{callback}?state=invalid", target) == f"https://public.example{prefix}/settings/tools?oauth=error"
        )
    finally:
        reset_public_access_context(token)
