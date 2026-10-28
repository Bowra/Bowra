from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.public_entries import PublicEntry, PublicEntryStore
from bowhead_gateway.public_entry_context import (
    COOKIE_NAME,
    PublicAccessContext,
    PublicEntryContextMiddleware,
    _cookie_header,
    bind_team_public_entry,
    current_public_access_context,
    public_access_context_for_entry,
    reset_public_access_context,
    resolve_public_access_context,
    set_public_access_context,
)
from bowhead_gateway.public_url import public_app_url
from starlette.requests import Request


@pytest.fixture
async def store(tmp_path: Path) -> PublicEntryStore:
    result = PublicEntryStore(tmp_path / "entries.db")
    await result.initialize()
    return result


async def _active_entry(
    store: PublicEntryStore,
    *,
    team_id: str = "team_a",
    base_url: str = "https://public.example.com/bowhead",
) -> PublicEntry:
    pending, token = await store.create_pending(
        team_id=team_id,
        base_url=base_url,
        actor_id="usr_owner",
    )
    return await store.verify_challenge(
        entry_id=pending.id,
        token=token,
    )


def _scope(
    *,
    client: str = "10.0.0.10",
    scheme: str = "http",
    path: str = "/settings/",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "http",
        "scheme": scheme,
        "path": path,
        "client": (client, 12345),
        "headers": headers or [],
    }


@pytest.mark.asyncio
async def test_forwarded_url_resolves_registered_entry(store: PublicEntryStore) -> None:
    entry = await _active_entry(store)
    context = await resolve_public_access_context(
        _scope(
            headers=[
                (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-host", b"public.example.com"),
                (b"x-forwarded-port", b"443"),
                (b"x-forwarded-prefix", b"/bowhead"),
            ],
        ),
        store,
    )
    assert context is not None
    assert context.entry_id == entry.id
    assert context.base_url == entry.base_url
    assert context.match_source == "forwarded_url"


@pytest.mark.asyncio
async def test_forwarded_headers_are_resolved_without_network_configuration(
    store: PublicEntryStore,
) -> None:
    entry = await _active_entry(store, base_url="https://attacker.example/")
    context = await resolve_public_access_context(
        _scope(
            client="203.0.113.8",
            headers=[
                (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-host", b"attacker.example"),
                (b"x-forwarded-prefix", b"/"),
            ],
        ),
        store,
    )
    assert context is not None
    assert context.entry_id == entry.id


@pytest.mark.asyncio
async def test_browser_public_base_hint_selects_registered_active_entry(
    store: PublicEntryStore,
) -> None:
    entry = await _active_entry(store)

    context = await resolve_public_access_context(
        _scope(
            client="172.19.0.1",
            headers=[
                (
                    b"x-bowhead-public-base",
                    b"https://public.example.com/bowhead",
                ),
            ],
        ),
        store,
    )

    assert context is not None
    assert context.entry_id == entry.id
    assert context.base_url == entry.base_url
    assert context.match_source == "browser_hint"


@pytest.mark.asyncio
async def test_browser_hint_recovers_from_partial_forwarded_headers(
    store: PublicEntryStore,
) -> None:
    entry = await _active_entry(store)

    context = await resolve_public_access_context(
        _scope(
            headers=[
                (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-host", b"internal-proxy.example"),
                (b"x-bowhead-public-base", entry.base_url.encode()),
            ],
        ),
        store,
    )

    assert context is not None
    assert context.entry_id == entry.id
    assert context.match_source == "browser_hint"


@pytest.mark.asyncio
async def test_browser_public_base_hint_allows_unregistered_url(
    store: PublicEntryStore,
) -> None:
    await _active_entry(store)

    context = await resolve_public_access_context(
        _scope(headers=[(b"x-bowhead-public-base", b"https://attacker.example/bowhead")]),
        store,
    )

    assert context is not None
    assert context.base_url == "https://attacker.example/bowhead"
    assert context.entry_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/bootstrap/status",
        "/api/v1/bootstrap/activate",
    ],
)
async def test_empty_instance_accepts_unregistered_browser_hint(
    store: PublicEntryStore,
    path: str,
) -> None:

    context = await resolve_public_access_context(
        _scope(
            path=path,
            headers=[
                (
                    b"x-bowhead-public-base",
                    b"https://first.example.com/bowhead",
                ),
            ],
        ),
        store,
    )

    assert context is not None
    assert context.base_url == "https://first.example.com/bowhead"
    assert context.entry_id is None
    assert context.match_source == "browser_hint"


@pytest.mark.asyncio
async def test_empty_instance_allows_unregistered_browser_hint_on_normal_api(
    store: PublicEntryStore,
) -> None:

    context = await resolve_public_access_context(
        _scope(
            path="/api/v1/auth/me",
            headers=[(b"x-bowhead-public-base", b"https://first.example.com/bowhead")],
        ),
        store,
    )

    assert context is not None
    assert context.base_url == "https://first.example.com/bowhead"
    assert context.match_source == "browser_hint"


@pytest.mark.asyncio
async def test_browser_public_base_hint_uses_first_host_match(
    store: PublicEntryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = await _active_entry(
        store,
        base_url="https://public.example.com",
    )
    current = await _active_entry(
        store,
        team_id="team_b",
        base_url="https://public.example.com/bowhead",
    )
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "cookie-secret")
    cookie_header = _cookie_header(public_access_context_for_entry(stale))
    assert cookie_header is not None
    cookie_value = cookie_header.split(";", 1)[0]

    context = await resolve_public_access_context(
        _scope(
            headers=[
                (b"cookie", cookie_value.encode()),
                (
                    b"x-bowhead-public-base",
                    current.base_url.encode(),
                ),
            ],
        ),
        store,
    )

    assert context is not None
    assert context.entry_id == stale.id
    assert context.base_url == stale.base_url
    assert context.match_source == "browser_hint"


@pytest.mark.asyncio
async def test_duplicate_forwarded_header_leaves_context_unresolved(store: PublicEntryStore) -> None:
    context = await resolve_public_access_context(
        _scope(
            headers=[
                (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-proto", b"http"),
                (b"x-forwarded-host", b"public.example.com"),
                (b"x-forwarded-prefix", b"/bowhead"),
            ],
        ),
        store,
    )
    assert context is None


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["public.example.com/path", "public.example.com?next=evil"])
async def test_invalid_forwarded_host_leaves_context_unresolved(
    store: PublicEntryStore,
    host: str,
) -> None:
    context = await resolve_public_access_context(
        _scope(
            headers=[
                (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-host", host.encode()),
                (b"x-forwarded-prefix", b"/bowhead"),
            ],
        ),
        store,
    )
    assert context is None


@pytest.mark.asyncio
async def test_mismatched_explicit_entry_id_retains_unbound_address(
    store: PublicEntryStore,
) -> None:
    entry = await _active_entry(store)
    context = await resolve_public_access_context(
        _scope(
            headers=[
                (b"x-bowhead-public-entry", entry.id.encode()),
                (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-host", b"other.example.com"),
                (b"x-forwarded-prefix", b"/bowhead"),
            ],
        ),
        store,
    )
    assert context is not None
    assert context.entry_id is None and context.team_id is None
    assert context.base_url == "https://other.example.com/bowhead"


@pytest.mark.asyncio
async def test_empty_instance_accepts_forwarded_bootstrap_candidate(store: PublicEntryStore) -> None:
    context = await resolve_public_access_context(
        _scope(
            path="/api/v1/bootstrap/activate",
            headers=[
                (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-host", b"first.example.com"),
                (b"x-forwarded-prefix", b"/bowhead"),
            ],
        ),
        store,
    )
    assert context is not None
    assert context.base_url == "https://first.example.com/bowhead"
    assert context.entry_id is None


@pytest.mark.asyncio
async def test_empty_instance_uses_candidate_for_activation_assets(
    store: PublicEntryStore,
) -> None:
    context = await resolve_public_access_context(
        _scope(
            path="/_next/static/chunks/app/activate.js",
            headers=[
                (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-host", b"first.example.com"),
                (b"x-forwarded-prefix", b"/bowhead"),
            ],
        ),
        store,
    )
    assert context is not None
    assert context.base_url == "https://first.example.com/bowhead"
    assert context.entry_id is None


@pytest.mark.asyncio
async def test_direct_entry_uses_first_host_and_port_match(
    store: PublicEntryStore,
) -> None:
    first = await _active_entry(store, base_url="http://localhost:8080/a")
    await _active_entry(store, base_url="http://localhost:8080/apple")
    context = await resolve_public_access_context(
        _scope(
            client="127.0.0.1",
            scheme="http",
            path="/apple/settings/",
            headers=[(b"host", b"localhost:8080")],
        ),
        store,
    )
    assert context is not None
    assert context.entry_id == first.id


@pytest.mark.asyncio
async def test_unregistered_direct_url_uses_current_request_base(
    store: PublicEntryStore,
) -> None:
    await _active_entry(store, base_url="http://registered.example:8080")
    context = await resolve_public_access_context(
        _scope(
            client="127.0.0.1",
            scheme="http",
            path="/settings/",
            headers=[(b"host", b"unregistered.example:8080")],
        ),
        store,
    )
    assert context is not None
    assert context.base_url == "http://unregistered.example:8080"
    assert context.entry_id is None


@pytest.mark.asyncio
async def test_direct_match_uses_host_and_port_even_when_tls_and_prefix_are_stripped(
    store: PublicEntryStore,
) -> None:
    entry = await _active_entry(store, base_url="https://public.example.com/bowhead")
    context = await resolve_public_access_context(
        _scope(scheme="http", path="/login/", headers=[(b"host", b"public.example.com")]),
        store,
    )
    assert context is not None
    assert context.match_source == "direct"
    assert context.entry_id == entry.id
    assert context.base_url == entry.base_url


@pytest.mark.asyncio
async def test_host_only_match_follows_a_port_miss(store: PublicEntryStore) -> None:
    entry = await _active_entry(store, base_url="https://public.example.com:8443/bowhead")
    context = await resolve_public_access_context(
        _scope(scheme="http", path="/login/", headers=[(b"host", b"public.example.com")]),
        store,
    )
    assert context is not None
    assert context.entry_id == entry.id


@pytest.mark.asyncio
async def test_host_match_uses_the_first_stably_ordered_active_entry(store: PublicEntryStore) -> None:
    first = await _active_entry(store, team_id="team_a", base_url="https://public.example.com/bowhead")
    await _active_entry(store, team_id="team_b", base_url="https://public.example.com/other")
    context = await resolve_public_access_context(
        _scope(scheme="http", path="/login/", headers=[(b"host", b"public.example.com")]),
        store,
    )
    assert context is not None
    assert context.entry_id == first.id


@pytest.mark.asyncio
async def test_forwarded_match_does_not_require_a_prefix(store: PublicEntryStore) -> None:
    entry = await _active_entry(store)
    context = await resolve_public_access_context(
        _scope(
            headers=[
                (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-host", b"public.example.com"),
            ],
        ),
        store,
    )
    assert context is not None
    assert context.entry_id == entry.id
    assert context.match_source == "forwarded_url"


@pytest.mark.asyncio
async def test_direct_entry_can_resolve_pending_challenge(
    store: PublicEntryStore,
) -> None:
    entry, _token = await store.create_pending(
        team_id="team_a",
        base_url="http://localhost:8080/bowhead",
        actor_id="usr_owner",
    )
    context = await resolve_public_access_context(
        _scope(
            client="127.0.0.1",
            scheme="http",
            path=f"/bowhead/.well-known/bowhead-public-entry/{entry.id}",
            headers=[(b"host", b"localhost:8080")],
        ),
        store,
    )
    assert context is not None
    assert context.base_url == entry.base_url
    assert context.entry_id is None


@pytest.mark.asyncio
async def test_direct_entry_accepts_pending_intranet_challenge(store: PublicEntryStore) -> None:
    entry, _token = await store.create_pending(
        team_id="team_a",
        base_url="http://10.10.0.5:10010",
        actor_id="usr_owner",
    )
    context = await resolve_public_access_context(
        _scope(
            client="10.10.0.133",
            scheme="http",
            path=f"/.well-known/bowhead-public-entry/{entry.id}",
            headers=[(b"host", b"10.10.0.5:10010")],
        ),
        store,
    )

    assert context is not None
    assert context.base_url == entry.base_url
    assert context.match_source == "direct"


@pytest.mark.asyncio
async def test_invalid_direct_host_leaves_context_unresolved(
    store: PublicEntryStore,
) -> None:
    context = await resolve_public_access_context(
        _scope(
            client="127.0.0.1",
            scheme="http",
            path="/api/v1/bootstrap/status",
            headers=[(b"host", b"localhost:8080/path")],
        ),
        store,
    )
    assert context is None


@pytest.mark.asyncio
async def test_direct_entry_strips_registered_prefix_before_routing(
    store: PublicEntryStore,
) -> None:
    await _active_entry(
        store,
        base_url="http://localhost:8080/bowhead",
    )
    seen_path = ""

    async def inner(scope, receive, send):
        nonlocal seen_path
        seen_path = scope["path"]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = PublicEntryContextMiddleware(inner)
    scope = _scope(
        client="127.0.0.1",
        scheme="http",
        path="/bowhead/settings/",
        headers=[(b"host", b"localhost:8080")],
    )
    scope["app"] = SimpleNamespace(
        state=SimpleNamespace(public_entry_store=store),
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    await middleware(scope, receive, send)
    assert seen_path == "/settings/"


@pytest.mark.asyncio
async def test_direct_entry_matches_and_strips_percent_encoded_prefix(
    store: PublicEntryStore,
) -> None:
    await _active_entry(
        store,
        base_url="http://localhost:8080/caf%C3%A9",
    )
    seen_path = ""
    seen_raw_path = b""

    async def inner(scope, receive, send):
        nonlocal seen_path, seen_raw_path
        seen_path = scope["path"]
        seen_raw_path = scope["raw_path"]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = PublicEntryContextMiddleware(inner)
    scope = _scope(
        client="127.0.0.1",
        scheme="http",
        path="/café/settings/",
        headers=[(b"host", b"localhost:8080")],
    )
    scope["raw_path"] = b"/caf%c3%a9/settings/"
    scope["app"] = SimpleNamespace(
        state=SimpleNamespace(public_entry_store=store),
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    await middleware(scope, receive, send)
    assert seen_path == "/settings/"
    assert seen_raw_path == b"/settings/"


@pytest.mark.asyncio
async def test_context_drives_public_url_without_environment(
    store: PublicEntryStore,
) -> None:
    entry = await _active_entry(store)
    context = await resolve_public_access_context(
        _scope(
            headers=[
                (b"x-bowhead-public-entry", entry.id.encode()),
                (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-host", b"public.example.com"),
                (b"x-forwarded-prefix", b"/bowhead"),
            ],
        ),
        store,
    )
    token = set_public_access_context(context)
    try:
        assert public_app_url("/login/") == "https://public.example.com/bowhead/login/"
    finally:
        reset_public_access_context(token)


@pytest.mark.asyncio
async def test_signed_context_cookie_supports_key_rotation_and_entry_lifecycle(
    store: PublicEntryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _active_entry(store)
    monkeypatch.setenv("BOWHEAD_JWT_SECRETS", "old=old-secret")
    cookie_header = _cookie_header(public_access_context_for_entry(entry))
    assert cookie_header is not None
    assert "HttpOnly" in cookie_header
    assert "SameSite=Lax" in cookie_header
    assert "Secure" in cookie_header
    assert "Path=/bowhead/" in cookie_header
    cookie_value = cookie_header.split(";", 1)[0]

    monkeypatch.setenv("BOWHEAD_JWT_SECRETS", "new=new-secret,old=old-secret")
    restored = await resolve_public_access_context(
        _scope(headers=[(b"cookie", cookie_value.encode())]),
        store,
    )
    assert restored is not None
    assert restored.entry_id == entry.id
    assert restored.match_source == "cookie"

    await store.disable(
        team_id=entry.team_id,
        entry_id=entry.id,
        actor_id="usr_owner",
        disable_last=True,
    )
    restored = await resolve_public_access_context(_scope(headers=[(b"cookie", cookie_value.encode())]), store)
    assert restored is not None and restored.base_url == entry.base_url
    assert restored.entry_id is None and restored.team_id is None


@pytest.mark.asyncio
async def test_signed_context_cookie_recovers_prefix_for_top_level_navigation(
    store: PublicEntryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _active_entry(store)
    monkeypatch.setenv("BOWHEAD_JWT_SECRETS", "current=secret")
    cookie_header = _cookie_header(public_access_context_for_entry(entry))
    assert cookie_header is not None
    cookie_value = cookie_header.split(";", 1)[0]

    context = await resolve_public_access_context(
        _scope(
            headers=[(b"cookie", cookie_value.encode())],
        ),
        store,
    )

    assert context is not None
    assert context.base_url == entry.base_url
    assert context.match_source == "cookie"


@pytest.mark.asyncio
@pytest.mark.parametrize("address_source", ["internal_host", "registered_host", "forwarded", "browser_hint"])
async def test_cookie_recovers_internal_host_without_overriding_identified_addresses(
    store: PublicEntryStore,
    monkeypatch: pytest.MonkeyPatch,
    address_source: str,
) -> None:
    saved = await _active_entry(store)
    current = await _active_entry(store, base_url="https://current.example/other")
    monkeypatch.setenv("BOWHEAD_JWT_SECRETS", "current=secret")
    cookie = _cookie_header(public_access_context_for_entry(saved))
    assert cookie is not None
    headers = [(b"cookie", cookie.split(";", 1)[0].encode()), (b"host", b"internal.example:8000")]
    if address_source == "registered_host":
        headers[-1] = (b"host", b"current.example:8000")
    elif address_source == "forwarded":
        headers.extend([(b"x-forwarded-proto", b"https"), (b"x-forwarded-host", b"unregistered.example")])
    elif address_source == "browser_hint":
        headers.append((b"x-bowhead-public-base", b"https://unregistered.example/bowhead"))
    context = await resolve_public_access_context(_scope(headers=headers), store)
    assert context is not None
    if address_source == "internal_host":
        assert context.entry_id == saved.id
        assert context.base_url == saved.base_url
    elif address_source == "registered_host":
        assert context.entry_id == current.id
    else:
        assert context.entry_id is None
        assert context.origin == "https://unregistered.example"


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_prefix", ["", "/bowhead"])
async def test_unbound_cookie_cannot_acquire_another_entry_path_or_team(
    store: PublicEntryStore,
    monkeypatch: pytest.MonkeyPatch,
    saved_prefix: str,
) -> None:
    from bowhead_core.public_entries import normalize_public_base_url
    from bowhead_gateway.public_entry_context import _context_from_normalized

    await _active_entry(store, base_url="https://public.example/bowhead")
    monkeypatch.setenv("BOWHEAD_JWT_SECRETS", "current=secret")
    saved = _context_from_normalized(normalize_public_base_url(f"https://public.example{saved_prefix}"), "oauth_state", entry=None)
    cookie = _cookie_header(saved)
    assert cookie is not None
    context = await resolve_public_access_context(_scope(headers=[(b"cookie", cookie.split(";", 1)[0].encode())]), store)
    assert context is not None
    assert context.base_url == saved.base_url
    assert context.entry_id is None and context.team_id is None


def test_unmatched_direct_host_does_not_issue_a_competing_root_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.public_entries import normalize_public_base_url
    from bowhead_gateway.public_entry_context import _context_from_normalized

    monkeypatch.setenv("BOWHEAD_JWT_SECRETS", "current=secret")
    context = _context_from_normalized(normalize_public_base_url("http://internal.example:8000"), "direct", entry=None)
    assert _cookie_header(context) is None


@pytest.mark.asyncio
async def test_context_cookie_rejects_tampering_and_duplicates(
    store: PublicEntryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _active_entry(store, base_url="http://public.example.com")
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "cookie-secret")
    cookie_header = _cookie_header(public_access_context_for_entry(entry))
    assert cookie_header is not None
    assert "Secure" not in cookie_header
    cookie_value = cookie_header.split(";", 1)[0]
    name, value = cookie_value.split("=", 1)
    assert name == COOKIE_NAME

    assert (
        await resolve_public_access_context(
            _scope(headers=[(b"cookie", f"{name}={value}x".encode())]),
            store,
        )
        is None
    )
    assert (
        await resolve_public_access_context(
            _scope(
                headers=[
                    (
                        b"cookie",
                        f"{name}={value}; {name}={value}".encode(),
                    ),
                ],
            ),
            store,
        )
        is None
    )


@pytest.mark.asyncio
async def test_team_binding_selects_team_entry_for_shared_url(
    store: PublicEntryStore,
) -> None:
    first = await _active_entry(store, team_id="team_a")
    second = await _active_entry(store, team_id="team_b")
    unbound = PublicAccessContext(
        base_url=first.base_url,
        origin="https://public.example.com",
        base_path="/bowhead",
        app_root=f"{first.base_url}/",
        match_source="forwarded_url",
    )
    token = set_public_access_context(unbound)
    try:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/auth/me",
                "headers": [],
                "app": SimpleNamespace(
                    state=SimpleNamespace(public_entry_store=store),
                ),
            },
        )
        bound = await bind_team_public_entry(request, team_id="team_b")
        assert bound is not None
        assert bound.entry_id == second.id
        assert current_public_access_context() == bound
    finally:
        reset_public_access_context(token)


def _local_scope(**kwargs: Any) -> dict[str, Any]:
    """A request scope on a private deployment (``deployment_mode == "local"``)."""
    scope = _scope(**kwargs)
    scope["app"] = SimpleNamespace(state=SimpleNamespace(deployment_mode="local"))
    return scope


@pytest.mark.asyncio
async def test_unregistered_hint_on_private_deployment_uses_current_request_base(
    store: PublicEntryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale registry remains usable without a repair-only fallback path."""
    await _active_entry(store, base_url="https://old-name.example/bowhead")
    scope = _local_scope(
        headers=[(b"x-bowhead-public-base", b"https://new-name.example/bowhead")],
    )

    context = await resolve_public_access_context(scope, store)

    assert context is not None
    assert context.base_url == "https://new-name.example/bowhead"
    assert context.entry_id is None


@pytest.mark.asyncio
async def test_unregistered_hint_on_saas_uses_current_request_base(
    store: PublicEntryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unregistered hint never blocks ordinary application access."""
    await _active_entry(store)
    scope = _scope(headers=[(b"x-bowhead-public-base", b"https://attacker.example/bowhead")])
    scope["app"] = SimpleNamespace(state=SimpleNamespace(deployment_mode="saas"))

    context = await resolve_public_access_context(scope, store)

    assert context is not None
    assert context.base_url == "https://attacker.example/bowhead"
    assert context.entry_id is None


@pytest.mark.asyncio
async def test_team_binding_uses_default_without_request_context(
    store: PublicEntryStore,
) -> None:
    expected = await _active_entry(
        store,
        team_id="team_a",
        base_url="https://default.example/bowhead",
    )
    token = set_public_access_context(None)
    try:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/auth/me",
                "headers": [],
                "app": SimpleNamespace(
                    state=SimpleNamespace(public_entry_store=store),
                ),
            },
        )

        bound = await bind_team_public_entry(
            request,
            team_id="team_a",
        )

        assert bound is not None
        assert bound.entry_id == expected.id
        assert bound.base_url == expected.base_url
        assert bound.match_source == "team_default"
    finally:
        reset_public_access_context(token)


@pytest.mark.asyncio
async def test_explicit_entry_id_is_not_rebound_to_another_team(
    store: PublicEntryStore,
) -> None:
    first = await _active_entry(store, team_id="team_a")
    await _active_entry(store, team_id="team_b")
    explicit = PublicAccessContext(
        base_url=first.base_url,
        origin="https://public.example.com",
        base_path="/bowhead",
        app_root=f"{first.base_url}/",
        match_source="entry_id",
        entry_id=first.id,
        team_id=first.team_id,
    )
    token = set_public_access_context(explicit)
    try:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/auth/me",
                "headers": [],
                "app": SimpleNamespace(
                    state=SimpleNamespace(public_entry_store=store),
                ),
            },
        )
        bound = await bind_team_public_entry(request, team_id="team_b")
        assert bound == explicit
        assert current_public_access_context() == explicit
    finally:
        reset_public_access_context(token)


@pytest.mark.asyncio
async def test_team_switch_returns_target_default_navigation(
    store: PublicEntryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_gateway.routes.auth import LastTeamBody, set_last_team

    source = await _active_entry(
        store,
        team_id="team_a",
        base_url="https://a.example.com/bowhead",
    )
    target = await _active_entry(
        store,
        team_id="team_b",
        base_url="https://b.example.com/app",
    )

    class _Users:
        async def user_has_active_team_membership(self, user_id, team_id):
            return user_id == "usr_a" and team_id == "team_b"

        async def set_active_team(self, user_id, team_id):
            return user_id == "usr_a" and team_id == "team_b"

    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/v1/auth/me/last-team",
            "headers": [],
            "app": SimpleNamespace(
                state=SimpleNamespace(public_entry_store=store),
            ),
        },
    )
    token = set_public_access_context(public_access_context_for_entry(source))
    monkeypatch.setenv("FEATURE_MULTI_TEAM_AUTHZ", "1")
    try:
        result = await set_last_team(
            LastTeamBody(team_id="team_b"),
            request,
            user=SimpleNamespace(id="usr_a"),
            store=_Users(),
        )
        assert result["redirect_url"] == f"{target.base_url}/"
    finally:
        reset_public_access_context(token)


class _BrokenStore(PublicEntryStore):
    """Store 在读时抛非 PublicEntryError —— 模拟 DB 锁 / IO / ENOSPC。

    覆盖解析路径实际会调到的全部读方法；漏一个测试就会静默退化成"没匹配上"，
    而不是在测被解析异常炸掉的那条路。
    """

    async def find_active_by_base_url(self, *args: Any, **kwargs: Any) -> Any:
        raise OSError("database or disk is full")

    async def list_active(self, *args: Any, **kwargs: Any) -> Any:
        raise OSError("database or disk is full")

    async def list_all(self, *args: Any, **kwargs: Any) -> Any:
        raise OSError("database or disk is full")

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise OSError("database or disk is full")

    async def default_for_team(self, *args: Any, **kwargs: Any) -> Any:
        raise OSError("database or disk is full")


@pytest.fixture
async def broken_store(tmp_path: Path) -> PublicEntryStore:
    result = _BrokenStore(tmp_path / "broken.db")
    await result.initialize()
    return result


async def _drive_middleware(
    store: PublicEntryStore,
    scope: dict[str, Any],
) -> tuple[int | None, dict[bytes, bytes], bool]:
    """跑一遍中间件，返回 (status, headers, 是否透传到了 inner app)。"""
    reached_inner = False

    async def inner(_scope, _receive, send):
        nonlocal reached_inner
        reached_inner = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope["app"] = SimpleNamespace(state=SimpleNamespace(public_entry_store=store))
    status: int | None = None
    headers: dict[bytes, bytes] = {}

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            headers.update(dict(message.get("headers", [])))

    await PublicEntryContextMiddleware(inner)(scope, receive, send)
    return status, headers, reached_inner


@pytest.mark.asyncio
async def test_store_failure_does_not_block_application_request(
    broken_store: PublicEntryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-07-29 116 事故回归：store 不可用时不得把每个请求变成 500。

    该解析跑在每个 HTTP 请求上，所以一次磁盘写满曾把静态页 /reports/ 一起打成 500。
    """
    status, headers, reached_inner = await _drive_middleware(
        broken_store,
        _scope(client="127.0.0.1", path="/reports/", headers=[(b"host", b"localhost:8080")]),
    )

    assert status == 204
    assert b"retry-after" not in headers
    assert reached_inner


@pytest.mark.asyncio
async def test_store_failure_keeps_operational_surface_alive(
    broken_store: PublicEntryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/health 与普通应用请求一样不依赖公开入口注册表。"""
    status, _headers, reached_inner = await _drive_middleware(
        broken_store,
        _scope(client="127.0.0.1", path="/health", headers=[(b"host", b"localhost:8080")]),
    )

    assert status == 204
    assert reached_inner


@pytest.mark.asyncio
async def test_readiness_probe_is_operationally_exempt(
    broken_store: PublicEntryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose probes /readyz and must not require a registered browser entry."""
    status, _headers, reached_inner = await _drive_middleware(
        broken_store,
        _scope(client="127.0.0.1", path="/readyz", headers=[(b"host", b"localhost:8080")]),
    )

    assert status == 204
    assert reached_inner


@pytest.mark.asyncio
async def test_store_failure_does_not_close_websocket(
    broken_store: PublicEntryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公开入口注册表不可用时，WebSocket 仍交由应用处理。"""
    closed: list[dict[str, Any]] = []

    async def inner(_scope, _receive, _send):
        closed.append({"type": "inner.reached"})

    scope = _scope(client="127.0.0.1", path="/ws", headers=[(b"host", b"localhost:8080")])
    scope["type"] = "websocket"
    scope["app"] = SimpleNamespace(state=SimpleNamespace(public_entry_store=broken_store))

    async def receive():
        return {"type": "websocket.disconnect"}

    async def send(message):
        closed.append(message)

    await PublicEntryContextMiddleware(inner)(scope, receive, send)
    assert closed == [{"type": "inner.reached"}]
