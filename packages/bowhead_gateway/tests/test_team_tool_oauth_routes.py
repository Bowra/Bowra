"""tools sprint WP2b-2 — remote MCP OAuth authorize / callback endpoints (TestClient).

Asserts the ENDPOINT behavior: authorize (team-admin) mints a pending record + auth URL for an MCP
source (404 for missing, 422 for non-MCP); callback (UNAUTHENTICATED — authenticity rests on the
single-use ``state``) fails closed on every bad path and completes token exchange + storage on the
good path. The OAuth mechanics (PKCE / discovery / secure remote endpoints) are covered in
bowhead_core's test_mcp_oauth; here the guarded client is monkeypatched to a MockTransport IdP.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from bowhead_core.tool_auth_registry import ToolAuthRegistry
from bowhead_core.user_store import TeamToolSource, User, UserStore, _id, _now
from bowhead_gateway.public_entry_context import (
    PublicAccessContext,
    reset_public_access_context,
    set_public_access_context,
)
from bowhead_gateway.routes import team_tool_sources as ots
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

_PUB = "https://93.184.216.34"  # public IP literal → passes the SSRF base_url guard without DNS
_IDP = "https://idp.example"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _client(store: UserStore, user: User) -> TestClient:
    from bowhead_core.public_entries import PublicEntryStore
    from bowhead_gateway.public_entry_context import public_access_context_for_entry

    public_entries = PublicEntryStore(store.db_path)
    _run(public_entries.initialize())
    entry = _run(
        public_entries.create_bootstrap_active(
            team_id=user.team_id,
            base_url="http://bowhead.example",
            actor_id=user.id,
        )
    )
    app = FastAPI()
    app.include_router(ots.router, prefix="/api/v1/team-tool-sources")
    app.state.user_store = store
    app.state.public_entry_store = public_entries
    app.state.tool_auth_registry = ToolAuthRegistry(store)
    app.dependency_overrides[ots._REQUIRE_TEAM_ADMIN] = lambda: user

    @app.middleware("http")
    async def public_entry_context(request, call_next):  # type: ignore[no-untyped-def]
        token = set_public_access_context(
            public_access_context_for_entry(entry, source="direct"),
        )
        try:
            return await call_next(request)
        finally:
            reset_public_access_context(token)

    return TestClient(app)


def _mcp_source(team_id: str = "team_a") -> TeamToolSource:
    return TeamToolSource(
        id=_id("ots"),
        team_id=team_id,
        name="acme-mcp",
        kind="mcp",
        base_url=_PUB,
        openapi_spec_json=None,
        tools_schema=[{"name": "search", "description": "d", "input_schema": {"type": "object"}, "destructive": False}],
        selected_op_ids=["search"],
        created_at=_now(),
        updated_at=_now(),
    )


def _patch_idp(monkeypatch: Any, handler: Any) -> None:
    def fake_build(*, resolver: Any = None, **kwargs: Any) -> httpx2.AsyncClient:
        kwargs.pop("auth", None)
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr("bowhead_core.mcp_oauth.build_guarded_async_client", fake_build)


def _full_idp() -> Any:
    def handler(request: httpx2.Request) -> httpx2.Response:
        path = urlparse(str(request.url)).path
        if "protected-resource" in path:
            return httpx2.Response(200, json={"resource": _PUB, "authorization_servers": [_IDP], "scopes_supported": ["repo"]})
        if path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(
                200,
                json={
                    "issuer": _IDP,
                    "authorization_endpoint": f"{_IDP}/authorize",
                    "token_endpoint": f"{_IDP}/token",
                    "registration_endpoint": f"{_IDP}/register",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                },
            )
        if path == "/register":
            return httpx2.Response(201, json={"client_id": "cid_test", "redirect_uris": ["https://x/cb"], "token_endpoint_auth_method": "none"})
        if path == "/token":
            return httpx2.Response(200, json={"access_token": "at_final", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "rt_final"})
        return httpx2.Response(404)

    return handler


# ── create (WP2b-3: OAuth-intent source may be created with NO tools) ─────────


def test_create_oauth_mcp_source_empty_tools_ok(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    client = _client(store, User(id="u", team_id="team_a", email="a@x.com"))
    r = client.post("/api/v1/team-tool-sources", json={"kind": "mcp", "name": "gh", "base_url": _PUB, "auth_type": "oauth"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["auth_type"] == "oauth" and body["tools_count"] == 0 and body["has_auth"] is False
    # auth_ref stays NULL until the authorize flow → runtime fails closed
    got = _run(store.get_team_tool_source("team_a", body["id"]))
    assert got is not None and got.auth_type == "oauth" and got.auth_ref is None and got.selected_op_ids == []


def test_create_oauth_rejected_for_openapi(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    client = _client(store, User(id="u", team_id="team_a", email="a@x.com"))
    r = client.post(
        "/api/v1/team-tool-sources", json={"kind": "openapi", "name": "x", "base_url": _PUB, "auth_type": "oauth", "openapi_spec_json": "{}"}
    )
    assert r.status_code == 422 and "only for remote MCP" in r.text


def test_create_non_oauth_mcp_still_requires_tools(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    client = _client(store, User(id="u", team_id="team_a", email="a@x.com"))
    r = client.post("/api/v1/team-tool-sources", json={"kind": "mcp", "name": "x", "base_url": _PUB, "auth_type": "none"})
    assert r.status_code == 422 and "non-empty tools_schema" in r.text


def test_patch_to_oauth_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    src = _mcp_source()
    _run(store.create_team_tool_source(src))
    client = _client(store, User(id="u", team_id="team_a", email="a@x.com"))
    r = client.patch(f"/api/v1/team-tool-sources/{src.id}", json={"auth_type": "oauth"})
    assert r.status_code == 422 and "cannot patch auth_type to oauth" in r.text


def test_preview_mcp_needs_oauth_signal(tmp_path: Any, monkeypatch: Any) -> None:
    """WP2b-3c: enumerate fails (OAuth server refuses unauthenticated tools/list) → probe detects
    OAuth → /preview returns {needs_oauth, mcp_url, provider_hint} instead of a 502."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())

    async def _raise(*a: Any, **k: Any) -> Any:
        raise RuntimeError("401 unauthorized")

    monkeypatch.setattr("bowhead_core.openapi_tool_source.enumerate_remote_mcp_tools", _raise)
    _patch_idp(monkeypatch, _full_idp())  # the probe's guarded client → PRM advertises the auth server
    client = _client(store, User(id="u", team_id="team_a", email="a@x.com"))
    r = client.post("/api/v1/team-tool-sources/preview", json={"kind": "mcp", "mcp_url": _PUB})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("needs_oauth") is True and body.get("mcp_url") == _PUB
    assert body.get("provider_hint") == "idp.example"  # PRM authorization_servers[0] host


# ── authorize ───────────────────────────────────────────────────────────────


def test_authorize_missing_source_404(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    client = _client(store, User(id="u", team_id="team_a", email="a@x.com"))
    assert client.post("/api/v1/team-tool-sources/ots_nope/oauth/authorize").status_code == 404


def test_authorize_rejects_non_mcp_source_422(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    src = TeamToolSource(id=_id("ots"), team_id="team_a", name="rest", base_url=_PUB, openapi_spec_json="{}", created_at=_now(), updated_at=_now())
    _run(store.create_team_tool_source(src))
    client = _client(store, User(id="u", team_id="team_a", email="a@x.com"))
    assert client.post(f"/api/v1/team-tool-sources/{src.id}/oauth/authorize").status_code == 422


def test_authorize_rejects_static_auth_source_409(tmp_path: Any, monkeypatch: Any) -> None:
    """review P2: authorizing a bearer/api_key source would clobber its working creds → reject."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    src = _mcp_source()
    src.auth_type = "bearer"
    _run(store.create_team_tool_source(src))
    client = _client(store, User(id="u", team_id="team_a", email="a@x.com"))
    r = client.post(f"/api/v1/team-tool-sources/{src.id}/oauth/authorize")
    assert r.status_code == 409 and "bearer" in r.text


def test_authorize_allows_http_public_callback_in_prod(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    src = _mcp_source()
    _run(store.create_team_tool_source(src))
    _patch_idp(monkeypatch, _full_idp())
    client = _client(store, User(id="u", team_id="team_a", email="a@x.com"))
    r = client.post(f"/api/v1/team-tool-sources/{src.id}/oauth/authorize")
    assert r.status_code == 200, r.text
    query = parse_qs(urlparse(r.json()["authorization_url"]).query)
    assert query["redirect_uri"] == ["http://bowhead.example/api/v1/team-tool-sources/oauth/callback"]


def test_authorize_happy_path_mints_pending_and_url(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    src = _mcp_source()
    _run(store.create_team_tool_source(src))
    _patch_idp(monkeypatch, _full_idp())
    client = _client(store, User(id="u_admin", team_id="team_a", email="a@x.com"))

    r = client.post(f"/api/v1/team-tool-sources/{src.id}/oauth/authorize")
    assert r.status_code == 200, r.text
    url = r.json()["authorization_url"]
    q = parse_qs(urlparse(url).query)
    assert urlparse(url).path == "/authorize"
    assert q["client_id"][0] == "cid_test" and q["code_challenge_method"][0] == "S256"
    state = q["state"][0]
    # a single-use pending record now exists for that state, bound to this team/source
    rec = _run(store.take_mcp_oauth_pending(state))
    assert rec is not None and rec["team_id"] == "team_a" and rec["source_id"] == src.id
    assert rec["code_verifier"] and rec["token_endpoint"] == f"{_IDP}/token"
    # client_info was persisted (registration) → source became oauth
    got = _run(store.get_team_tool_source("team_a", src.id))
    assert got is not None and got.auth_type == "oauth"


# ── callback (unauthenticated; state IS the auth) ────────────────────────────


def _cb_client(store: UserStore) -> TestClient:
    from bowhead_core.public_entries import PublicEntryStore
    from bowhead_gateway.public_entry_context import public_access_context_for_entry

    app = FastAPI()
    app.include_router(ots.router, prefix="/api/v1/team-tool-sources")
    app.state.user_store = store
    registry = PublicEntryStore(store.db_path)
    _run(registry.initialize())
    app.state.public_entry_store = registry

    @app.middleware("http")
    async def public_entry_context(request, call_next):  # type: ignore[no-untyped-def]
        entries = await registry.find_active_by_base_url("http://bowhead.example")
        context = (
            public_access_context_for_entry(entries[0], source="direct")
            if len(entries) == 1
            else PublicAccessContext(
                base_url="http://bowhead.example",
                origin="http://bowhead.example",
                base_path="",
                app_root="http://bowhead.example/",
                match_source="direct",
            )
        )
        token = set_public_access_context(context)
        try:
            return await call_next(request)
        finally:
            reset_public_access_context(token)

    return TestClient(app)


_CB = "/api/v1/team-tool-sources/oauth/callback"


def test_callback_fails_closed_on_bad_state(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    client = _cb_client(store)
    for params in ({}, {"code": "x", "state": "unknown"}, {"error": "access_denied", "state": "x"}):
        r = client.get(_CB, params=params, follow_redirects=False)
        assert r.status_code == 303 and "oauth=error" in r.headers["location"], (params, r.headers.get("location"))


def test_callback_happy_path_and_single_use(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    src = _mcp_source()
    _run(store.create_team_tool_source(src))
    _run(
        store.create_mcp_oauth_pending(
            state="good",
            team_id="team_a",
            source_id=src.id,
            code_verifier="v" * 43,
            redirect_uri="https://app/cb",
            token_endpoint=f"{_IDP}/token",
            client_id="cid_test",
            resource=None,
            created_by="u_admin",
        )
    )
    _patch_idp(monkeypatch, _full_idp())
    client = _cb_client(store)

    r = client.get(_CB, params={"code": "authcode", "state": "good"}, follow_redirects=False)
    assert r.status_code == 303 and "oauth=success" in r.headers["location"], r.headers.get("location")
    # tokens are now stored (Fernet) for the source
    from bowhead_core.mcp_oauth import FernetTokenStorage

    tok = _run(FernetTokenStorage(store, "team_a", src.id).get_tokens())
    assert tok is not None and tok.access_token == "at_final"

    # replay the consumed state → error (single-use at the HTTP layer)
    r2 = client.get(_CB, params={"code": "authcode", "state": "good"}, follow_redirects=False)
    assert r2.status_code == 303 and "oauth=error" in r2.headers["location"]


def test_callback_keeps_tool_refresh_when_optional_prompt_discovery_fails(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP_PROMPT", "1")
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    src = _mcp_source()
    src.auth_type = "oauth"
    _run(store.create_team_tool_source(src))
    _run(
        store.create_mcp_oauth_pending(
            state="split-refresh",
            team_id="team_a",
            source_id=src.id,
            code_verifier="v" * 43,
            redirect_uri="https://app/cb",
            token_endpoint=f"{_IDP}/token",
            client_id="cid_test",
            resource=None,
            created_by="u_admin",
        )
    )

    async def _tools(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "name": "search",
                "description": "fresh after OAuth",
                "input_schema": {"type": "object"},
                "destructive": False,
            }
        ]

    async def _prompts(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("optional prompt endpoint failed")

    monkeypatch.setattr(
        "bowhead_core.openapi_tool_source.enumerate_remote_mcp_tools",
        _tools,
    )
    monkeypatch.setattr(
        "bowhead_core.openapi_tool_source.enumerate_remote_mcp_prompts",
        _prompts,
    )
    _patch_idp(monkeypatch, _full_idp())

    response = _cb_client(store).get(
        _CB,
        params={"code": "authcode", "state": "split-refresh"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "oauth=success" in response.headers["location"]
    current = _run(store.get_team_tool_source("team_a", src.id))
    assert current is not None
    assert current.tools_schema[0]["description"] == "fresh after OAuth"


def test_callback_prompt_refresh_does_not_overwrite_concurrent_admin_refresh(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP_PROMPT", "1")
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "ALTER TABLE team_tool_source ADD COLUMN prompts_schema_json TEXT NOT NULL DEFAULT '[]'",
        )
    src = _mcp_source()
    src.auth_type = "oauth"
    _run(store.create_team_tool_source(src))
    _run(
        store.create_mcp_oauth_pending(
            state="concurrent-refresh",
            team_id="team_a",
            source_id=src.id,
            code_verifier="v" * 43,
            redirect_uri="https://app/cb",
            token_endpoint=f"{_IDP}/token",
            client_id="cid_test",
            resource=None,
            created_by="u_admin",
        )
    )

    prompt_a = {
        "name": "callback",
        "title": "",
        "description": "",
        "arguments": [],
    }
    prompt_b = {
        "name": "admin",
        "title": "",
        "description": "",
        "arguments": [],
    }

    async def _tools(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"name": "search", "description": "fresh"}]

    async def _prompts(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [prompt_a]

    original_update = store.update_team_tool_source_meta

    async def interleaved_update(*args: Any, **kwargs: Any) -> str | None:
        revision = await original_update(*args, **kwargs)
        if kwargs.get("tools_schema") is not None:
            # Simulate refresh-apply committing after the callback's Tool CAS
            # and before its later prompts/list response is persisted.
            await original_update(
                "team_a",
                src.id,
                prompts_schema=[prompt_b],
            )
        return revision

    monkeypatch.setattr(store, "update_team_tool_source_meta", interleaved_update)
    monkeypatch.setattr(
        "bowhead_core.openapi_tool_source.enumerate_remote_mcp_tools",
        _tools,
    )
    monkeypatch.setattr(
        "bowhead_core.openapi_tool_source.enumerate_remote_mcp_prompts",
        _prompts,
    )
    _patch_idp(monkeypatch, _full_idp())

    response = _cb_client(store).get(
        _CB,
        params={"code": "authcode", "state": "concurrent-refresh"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    current = _run(store.get_team_tool_source("team_a", src.id))
    assert current is not None
    assert current.prompts_schema == [prompt_b]


def test_full_loop_create_authorize_callback(tmp_path: Any, monkeypatch: Any) -> None:
    """The complete loop the FE drives: create an oauth source (empty tools) → authorize (mints the
    pending + URL) → callback (exchange + store). Proves the three HTTP endpoints compose end-to-end."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    _patch_idp(monkeypatch, _full_idp())
    admin = _client(store, User(id="u_admin", team_id="team_a", email="a@x.com"))

    # 1) create oauth source, no tools yet
    src_id = admin.post("/api/v1/team-tool-sources", json={"kind": "mcp", "name": "gh", "base_url": _PUB, "auth_type": "oauth"}).json()["id"]

    # 2) authorize → auth URL + a single-use pending record
    q = parse_qs(urlparse(admin.post(f"/api/v1/team-tool-sources/{src_id}/oauth/authorize").json()["authorization_url"]).query)
    state = q["state"][0]

    # 3) callback (unauthenticated; state IS the auth) → exchange + store tokens → success redirect
    r = _cb_client(store).get(_CB, params={"code": "authcode", "state": state}, follow_redirects=False)
    assert r.status_code == 303 and "oauth=success" in r.headers["location"], r.headers.get("location")
    from bowhead_core.mcp_oauth import FernetTokenStorage

    tok = _run(FernetTokenStorage(store, "team_a", src_id).get_tokens())
    assert tok is not None and tok.access_token == "at_final"


@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
@pytest.mark.parametrize("denied", [False, True])
def test_mcp_return_restores_saved_prefix_and_consumes_denied_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str, denied: bool
) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    store = UserStore(tmp_path / "users.db")
    _run(store.initialize())
    source = _mcp_source()
    _run(store.create_team_tool_source(source))
    uri = f"https://public.example:10010{prefix}{_CB}"
    _run(
        store.create_mcp_oauth_pending(
            state="prefix-state",
            team_id="team_a",
            source_id=source.id,
            code_verifier="v" * 43,
            redirect_uri=uri,
            token_endpoint=f"{_IDP}/token",
            client_id="cid_test",
            resource=None,
            created_by="u_admin",
        )
    )
    _patch_idp(monkeypatch, _full_idp())
    client = _cb_client(store)
    params = {"state": "prefix-state", "error": "access_denied"} if denied else {"state": "prefix-state", "code": "code"}
    response = client.get(_CB, params=params, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"{prefix}/settings/tools?oauth={'error' if denied else 'success'}")
    assert _run(store.take_mcp_oauth_pending("prefix-state")) is None
