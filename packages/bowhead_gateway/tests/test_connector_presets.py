"""external-capability-onboarding §3.1 — curated connector-preset catalog + GET /presets route.

The catalog is hand-maintained; these lock the invariants the FE + create flow rely on (legal field
combos, entitlement filter, route-order collision with /{source_id}).
"""

from __future__ import annotations

import asyncio

from bowhead_core.tool_auth_registry import ToolAuthRegistry
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.connector_presets import CURATED_PRESETS, presets_for
from bowhead_gateway.routes import team_tool_sources as ots
from fastapi import FastAPI
from fastapi.testclient import TestClient

_LEGAL_AUTH = {"none", "bearer", "api_key", "basic", "oauth"}


def _run(coro):
    return asyncio.run(coro)


def _client(store: UserStore, user: User) -> TestClient:
    app = FastAPI()
    app.include_router(ots.router, prefix="/api/v1/team-tool-sources")
    app.state.user_store = store
    app.state.tool_auth_registry = ToolAuthRegistry(store)
    app.dependency_overrides[ots._REQUIRE_TEAM_ADMIN] = lambda: user
    return TestClient(app)


def test_curated_presets_all_legal():
    assert len(CURATED_PRESETS) >= 1
    ids = [p.id for p in CURATED_PRESETS]
    assert len(ids) == len(set(ids)), "preset ids must be unique"
    for p in CURATED_PRESETS:
        assert p.kind in {"openapi", "mcp"}
        assert p.auth in _LEGAL_AUTH
        # create enforces oauth ⇒ kind=mcp — the catalog must never ship an illegal combo
        if p.auth == "oauth":
            assert p.kind == "mcp", f"{p.id}: oauth requires kind=mcp"
        # a usable source needs an address on the matching field
        if p.kind == "mcp":
            assert p.mcp_url, f"{p.id}: mcp preset needs mcp_url"
        else:
            assert p.spec_url, f"{p.id}: openapi preset needs spec_url"


def test_presets_for_filters_on_license():
    licensed = presets_for(mcp_licensed=True)
    assert licensed, "a licensed team must still see the usable presets"
    # unlicensed team can't create → advertise nothing (P2-11)
    assert presets_for(mcp_licensed=False) == []
    # public dict drops None fields, keeps the FE contract
    dw = next(p for p in licensed if p["id"] == "deepwiki")
    assert set(dw.keys()) <= {"id", "name", "icon", "kind", "auth", "blurb", "spec_url", "mcp_url"}
    assert "spec_url" not in dw and dw["mcp_url"]  # mcp preset → no spec_url key
    # only catalog fields reach the FE
    assert all(set(p.keys()) <= {"id", "name", "icon", "kind", "auth", "blurb", "spec_url", "mcp_url"} for p in licensed)


def test_http_deployment_advertises_no_oauth_preset():
    """advertise-execute-gap B2: OAuth 2.1 needs an HTTPS redirect_uri, so on a plain-HTTP
    deployment (116 today) every OAuth connector's click ends in OAuthRegistrationError."""
    items = presets_for(mcp_licensed=True, oauth_callback_https=False)
    assert items, "non-OAuth presets stay usable — they need no callback"
    assert all(p["auth"] != "oauth" for p in items)
    assert {p["id"] for p in items} == {p.id for p in CURATED_PRESETS if p.auth != "oauth"}


def test_https_deployment_restores_the_oauth_presets():
    """Positive control for the filter above: HTTPS brings the OAuth entries back."""
    ids = {p["id"] for p in presets_for(mcp_licensed=True, oauth_callback_https=True)}
    assert {"notion", "linear", "stripe"} <= ids


def test_github_is_a_static_credential_preset_so_http_deployments_keep_it():
    """GitHub's hosted MCP takes a PAT as a Bearer — no callback, so no HTTPS dependency, and no
    dependence on dynamic client registration (its authorization server has none). Cataloguing it
    as ``oauth`` is what made it permanently unusable."""
    gh = next(p for p in CURATED_PRESETS if p.id == "github")
    assert gh.auth == "bearer"
    assert "github" in {p["id"] for p in presets_for(mcp_licensed=True, oauth_callback_https=False)}


def test_get_presets_route(tmp_path):
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    client = _client(store, User(id="u_a", team_id="team_a", email="a@x.com"))

    r = client.get("/api/v1/team-tool-sources/presets")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    # licensed (test mode is not "local") → catalog served, NOT a 404 fall-through to /{source_id}
    assert items
    dw = next(i for i in items if i["id"] == "deepwiki")
    assert dw["kind"] == "mcp" and dw["auth"] == "none" and dw["mcp_url"].startswith("https://")
    # no secrets / team data leak — only catalog fields
    for i in items:
        assert set(i.keys()) <= {"id", "name", "icon", "kind", "auth", "blurb", "spec_url", "mcp_url"}


def test_catalog_has_non_oauth_preset():
    # Keep at least one source that can complete via direct preview/enumerate without provider OAuth.
    no_auth = [p for p in CURATED_PRESETS if p.auth == "none"]
    assert no_auth, "catalog must offer at least one non-OAuth preset"
    for p in no_auth:
        # a no-auth preset enumerates unauthenticated → must be MCP with an endpoint (not oauth/openapi-only)
        assert p.kind == "mcp" and p.mcp_url, f"{p.id}: no-auth preset needs an mcp_url to enumerate"


def test_presets_route_survives_an_unresolvable_public_entry(tmp_path):
    """The catalog is read-only — it must not 500 when no public entry resolves in this context.

    ``_oauth_redirect_uri`` → ``public_app_url`` raises there, and this route started depending on
    it in advertise-execute-gap B2. Fail closed (no OAuth entries), never fail loud."""
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    client = _client(store, User(id="u_a", team_id="team_a", email="a@x.com"))

    r = client.get("/api/v1/team-tool-sources/presets")
    assert r.status_code == 200, r.text
    assert all(i["auth"] != "oauth" for i in r.json()["items"])
