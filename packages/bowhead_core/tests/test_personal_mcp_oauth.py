"""Signed-in personal MCP connections: renewal before dispatch, ended sign-ins, and signing in again.

Reuses the personal MCP fixture (real migrated core, exact Agent binding, owner consent) and turns
its connection into a signed-in one. The fixture is the pre-split core template, so the 0233 table
change is applied to it first; production databases receive it through the migration.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from importlib import import_module
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx2
import pytest
from bowhead_core.personal_mcp import PersonalMcpError, build_personal_specs, canonical, digest, personal_oauth_headers
from test_personal_mcp import _CONFIRMATIONS, _client, _wait_pending, personal  # noqa: F401 - fixture

MIGRATION = import_module("bowhead_core.migrations.versions.0233_personal_mcp_oauth")
_IDP = "https://idp.example"
_ENDPOINT = "http://localhost:9000/mcp"

pytestmark = pytest.mark.asyncio


def _widen(db_path) -> None:
    with sqlite3.connect(db_path) as db:
        MIGRATION.widen_auth_type(db.execute)


def _sign_in(personal, *, refresh_token: str = "stored-refresh") -> None:  # noqa: F811
    """Turn the fixture's bearer connection into a signed-in one, version unchanged."""
    _widen(personal.fixture.db_path)
    owner, identity = personal.ctx.user_id, personal.connection_id
    auth = {
        "oauth_client_info": {"client_id": "cid", "redirect_uris": ["https://app/cb"], "token_endpoint_auth_method": "none"},
        "oauth_tokens": {"access_token": "first-access", "token_type": "Bearer", "refresh_token": refresh_token},
        "oauth_meta": {"token_endpoint": f"{_IDP}/token", "resource": _ENDPOINT},
    }
    with sqlite3.connect(personal.fixture.db_path) as db:
        db.execute(
            "UPDATE personal_mcp_connection SET auth_type='oauth',auth_cipher=? WHERE id=?",
            (personal.registry.seal(owner, identity, "credentials", auth), identity),
        )


def _provider(monkeypatch, responses: list[tuple[int, dict]]) -> list[str]:
    """Token endpoint that answers ``responses`` in order and records the refresh tokens it saw."""
    seen: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0.01)
        assert str(request.url) == f"{_IDP}/token"
        seen.append(parse_qs(request.content.decode())["refresh_token"][0])
        status, body = responses[min(len(seen), len(responses)) - 1]
        return httpx2.Response(status, json=body)

    def fake_build(*, resolver=None, **kwargs):
        kwargs.pop("auth", None)
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr("bowhead_core.mcp_oauth.build_guarded_async_client", fake_build)
    return seen


def _remote(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    class Remote:
        async def call_tool(self, name, args):
            calls.append(args)
            return SimpleNamespace(is_error=False, structured_content={"echo": "renewed-access", "query": args["query"]}, content=[])

    @asynccontextmanager
    async def connect(endpoint, headers, **kwargs):
        assert headers == {"Authorization": "Bearer renewed-access"}
        yield Remote()

    monkeypatch.setattr("bowhead_core.personal_mcp.connect_remote_mcp", connect)
    return calls


async def _approved_call(personal, monkeypatch, call_id: str = "call_one"):  # noqa: F811
    personal.ctx.personal_mcp_call_id = call_id
    spec = (await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx))[0]
    task = asyncio.create_task(spec.handler({"query": "q"}, personal.ctx))
    pending = await _wait_pending(personal, task)
    async with await _client(personal) as client:
        response = await client.post(f"{_CONFIRMATIONS}/{pending['id']}/decision", json={"decision": "approve"})
        assert response.status_code == 200, response.text
    return await asyncio.wait_for(task, 10)


def _connection(personal) -> sqlite3.Row:  # noqa: F811
    with sqlite3.connect(personal.fixture.db_path) as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM personal_mcp_connection WHERE id=?", (personal.connection_id,)).fetchone()


async def test_signed_in_call_renews_before_dispatch_and_keeps_the_version(personal, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "1")
    _sign_in(personal)
    seen = _provider(monkeypatch, [(200, {"access_token": "renewed-access", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "rotated"})])
    calls = _remote(monkeypatch)
    version = _connection(personal)["version"]
    result = await _approved_call(personal, monkeypatch)
    assert result == {"ok": True, "data": {"echo": "[redacted]", "query": "q"}}
    assert calls == [{"query": "q"}] and seen == ["stored-refresh"]
    row = _connection(personal)
    assert row["version"] == version and row["status"] == "ready"  # a renewal is not a configuration change
    stored = personal.registry.unseal(personal.ctx.user_id, personal.connection_id, "credentials", row["auth_cipher"])
    assert stored["oauth_tokens"]["refresh_token"] == "rotated"
    assert "rotated" not in row["auth_cipher"]


async def test_ended_sign_in_is_not_sent_and_asks_to_sign_in_again(personal, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "1")
    _sign_in(personal)
    _provider(monkeypatch, [(400, {"error": "invalid_grant", "error_description": "private-provider-detail"})])
    calls = _remote(monkeypatch)
    version = _connection(personal)["version"]
    result = await _approved_call(personal, monkeypatch)
    assert result["ok"] is False and result["error_kind"] == "personal_mcp_sign_in_ended"
    assert result["error"].startswith("Not run") and "My Connections" in result["error"] and "private-provider-detail" not in result["error"]
    assert calls == []
    row = _connection(personal)
    assert row["status"] == "needs_reauth" and row["version"] == version + 1
    invocations = await personal.registry.rows("SELECT status FROM personal_mcp_invocation")
    assert [item["status"] for item in invocations] == ["denied"]  # never "unknown": nothing was sent


async def test_unanswered_renewal_keeps_the_sign_in(personal, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "1")
    _sign_in(personal)
    _provider(monkeypatch, [(503, {})])
    calls = _remote(monkeypatch)
    result = await _approved_call(personal, monkeypatch)
    assert result["error_kind"] == "personal_mcp_sign_in_renewal_unavailable" and "still valid" in result["error"]
    assert calls == []
    assert _connection(personal)["status"] == "ready"


async def test_signed_in_connection_does_not_run_while_the_feature_is_off(personal, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "0")
    _sign_in(personal)
    seen = _provider(monkeypatch, [(200, {"access_token": "renewed-access", "token_type": "Bearer", "expires_in": 3600})])
    calls = _remote(monkeypatch)
    result = await _approved_call(personal, monkeypatch)
    assert result["error_kind"] == "personal_mcp_oauth_disabled" and calls == [] and seen == []


async def test_concurrent_calls_share_one_renewal(personal, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "1")
    _sign_in(personal)
    seen = _provider(monkeypatch, [(200, {"access_token": "renewed-access", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "rotated"})])
    owner, identity = personal.ctx.user_id, personal.connection_id
    headers = await asyncio.gather(*(personal_oauth_headers(personal.registry, owner, identity, _ENDPOINT) for _ in range(6)))
    assert headers == [{"Authorization": "Bearer renewed-access"}] * 6 and seen == ["stored-refresh"]


def _tool(name: str, schema: dict) -> dict:
    return {"remote_name": name, "description": name, "schema_json": canonical(schema), "schema_digest": digest(schema), "risk": "unknown"}


@pytest.mark.parametrize("changed", [False, True])
async def test_signing_in_again_keeps_unchanged_tools_and_their_bindings(personal, monkeypatch, changed):  # noqa: F811
    """Decision Q1: no re-selecting or re-binding after a new sign-in, unless the tool itself changed."""
    _sign_in(personal)
    owner, identity = personal.ctx.user_id, personal.connection_id
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}
    if changed:
        schema = {**schema, "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}}
    await personal.registry.save_connection(
        owner,
        "My CRM",
        _ENDPOINT,
        "oauth",
        None,
        [_tool("search", schema), _tool("lookup", {"type": "object"})],
        connection_id=identity,
        expected_version=_connection(personal)["version"],
        keep_unchanged=True,
    )
    tools = {row["remote_name"]: row for row in await personal.registry.rows("SELECT * FROM personal_mcp_tool WHERE connection_id=?", (identity,))}
    bindings = await personal.registry.rows("SELECT tool_ids_json FROM personal_mcp_binding")
    bound = json.loads(bindings[0]["tool_ids_json"])
    assert tools["lookup"]["selected"] == 0  # a new tool always waits for the owner
    if changed:
        assert tools["search"]["selected"] == 0 and personal.tool_id not in bound
    else:
        assert tools["search"]["selected"] == 1 and bound == [personal.tool_id]
    row = _connection(personal)
    assert row["auth_type"] == "oauth" and row["status"] == "ready"
    # the stored grant was kept: no credential was written
    assert personal.registry.unseal(owner, identity, "credentials", row["auth_cipher"])["oauth_tokens"]["refresh_token"] == "stored-refresh"


async def test_explicit_refresh_still_resets_every_selection(personal):  # noqa: F811
    owner, identity = personal.ctx.user_id, personal.connection_id
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}
    await personal.registry.save_connection(
        owner,
        "My CRM",
        _ENDPOINT,
        "bearer",
        {"value": "sensitive-personal-token"},
        [_tool("search", schema)],
        connection_id=identity,
        expected_version=_connection(personal)["version"],
    )
    tools = await personal.registry.rows("SELECT selected FROM personal_mcp_tool WHERE connection_id=?", (identity,))
    assert [row["selected"] for row in tools] == [0]
    assert json.loads((await personal.registry.rows("SELECT tool_ids_json FROM personal_mcp_binding"))[0]["tool_ids_json"]) == []


# ── Sign-in routes ────────────────────────────────────────────────────────────

_PRM = {"resource": _ENDPOINT, "authorization_servers": [_IDP]}
_ASM = {
    "issuer": _IDP,
    "authorization_endpoint": f"{_IDP}/authorize",
    "token_endpoint": f"{_IDP}/token",
    "registration_endpoint": f"{_IDP}/register",
    "response_types_supported": ["code"],
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["none"],
}


def _identity_provider(monkeypatch, *, registration: bool = True) -> list[str]:
    """Discovery, registration and code exchange; records the paths it served."""
    served: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        served.append(path)
        if path == "/mcp":
            return httpx2.Response(401, headers={"WWW-Authenticate": "Bearer"})
        if "oauth-protected-resource" in path:
            return httpx2.Response(200, json=_PRM)
        if path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json=_ASM if registration else {key: value for key, value in _ASM.items() if key != "registration_endpoint"})
        if path == "/register":
            body = json.loads(request.content)
            return httpx2.Response(201, json={"client_id": "cid", "redirect_uris": body["redirect_uris"], "token_endpoint_auth_method": "none"})
        if path == "/token":
            assert parse_qs(request.content.decode())["grant_type"] == ["authorization_code"]
            return httpx2.Response(
                200, json={"access_token": "signed-in-access", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "fresh"}
            )
        return httpx2.Response(404)

    def fake_build(*, resolver=None, **kwargs):
        kwargs.pop("auth", None)
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr("bowhead_core.mcp_oauth.build_guarded_async_client", fake_build)
    return served


async def _oauth_client(personal, prefix: str = ""):  # noqa: F811
    from bowhead_core.public_entries import PublicEntryStore
    from bowhead_gateway.public_entry_context import public_access_context_for_entry, reset_public_access_context, set_public_access_context
    from bowhead_gateway.routes import personal_mcp as routes
    from bowhead_gateway.routes.auth import _current_user, _store
    from fastapi import FastAPI

    entries = PublicEntryStore(personal.fixture.db_path)
    await entries.initialize()
    user = await personal.fixture.users.get_user(personal.ctx.user_id)
    entry = await entries.create_bootstrap_active(team_id=user.team_id, base_url=f"http://bowhead.example{prefix}", actor_id=user.id)
    app = FastAPI()
    app.state = personal.license_app.state
    app.state.user_store = personal.fixture.users
    app.state.public_entry_store = entries
    app.include_router(routes.router, prefix="/api/v1/personal-mcp")
    app.dependency_overrides[_current_user] = lambda: user
    app.dependency_overrides[_store] = lambda: personal.fixture.users
    app.dependency_overrides[routes._registry] = lambda: personal.registry

    @app.middleware("http")
    async def public_entry(request, call_next):
        token = set_public_access_context(public_access_context_for_entry(entry, source="direct"))
        try:
            return await call_next(request)
        finally:
            reset_public_access_context(token)

    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://bowhead.example")


def _discovered(monkeypatch, tools: list[dict]) -> None:
    async def discover(endpoint, headers):
        assert headers == {"Authorization": "Bearer signed-in-access"}
        return tools

    monkeypatch.setattr("bowhead_gateway.routes.personal_mcp.discover", discover)


def _state(authorize_url: str) -> str:
    from urllib.parse import urlsplit

    return parse_qs(urlsplit(authorize_url).query)["state"][0]


@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
async def test_first_sign_in_creates_a_ready_connection_with_unselected_tools(personal, monkeypatch, prefix):  # noqa: F811
    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "1")
    _widen(personal.fixture.db_path)
    served = _identity_provider(monkeypatch)
    _discovered(monkeypatch, [_tool("notes", {"type": "object"})])
    async with await _oauth_client(personal, prefix) as client:
        started = await client.post("/api/v1/personal-mcp/oauth/authorize", json={"name": "Notes", "endpoint": _ENDPOINT})
        assert started.status_code == 200, started.text
        identity = started.json()["connection_id"]
        # the provider returns the browser to the entry the member started from, prefix included
        callback = f"http://bowhead.example{prefix}/api/v1/personal-mcp/oauth/callback"
        assert parse_qs(started.json()["authorize_url"].split("?", 1)[1])["redirect_uri"] == [callback]
        assert started.json()["authorize_url"].startswith(f"{_IDP}/authorize?")
        with sqlite3.connect(personal.fixture.db_path) as db:
            assert db.execute("SELECT status,auth_type FROM personal_mcp_connection WHERE id=?", (identity,)).fetchone() == ("authorizing", "oauth")
        state = _state(started.json()["authorize_url"])
        back = await client.get("/api/v1/personal-mcp/oauth/callback", params={"code": "c", "state": state})
        assert back.status_code == 303
        # back to My Connections of the entry the member started from
        assert back.headers["location"] == f"{prefix}/connections/?mcp_oauth=connected&mcp_oauth_reason=signed_in&connection={identity}"
        replay = await client.get("/api/v1/personal-mcp/oauth/callback", params={"code": "c", "state": state})
        assert "mcp_oauth_reason=session_expired" in replay.headers["location"]
        listed = (await client.get("/api/v1/personal-mcp")).json()["connections"]
    connection = next(item for item in listed if item["id"] == identity)
    assert connection["status"] == "ready" and connection["provider_host"] == "idp.example"
    assert [tool["selected"] for tool in connection["tools"]] == [0]
    assert "signed-in-access" not in json.dumps(listed) and "fresh" not in json.dumps(listed)
    # runs use the new grant at once: no renewal right after signing in
    assert await personal_oauth_headers(personal.registry, personal.ctx.user_id, identity, _ENDPOINT) == {"Authorization": "Bearer signed-in-access"}
    assert served.count("/token") == 1


async def test_signing_in_again_reports_it_and_keeps_the_selected_tool(personal, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "1")
    _sign_in(personal)
    _identity_provider(monkeypatch)
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}
    _discovered(monkeypatch, [_tool("search", schema)])
    async with await _oauth_client(personal) as client:
        version = _connection(personal)["version"]
        started = await client.post("/api/v1/personal-mcp/oauth/authorize", json={"connection_id": personal.connection_id, "version": version})
        assert started.status_code == 200, started.text
        back = await client.get("/api/v1/personal-mcp/oauth/callback", params={"code": "c", "state": _state(started.json()["authorize_url"])})
    assert "mcp_oauth_reason=signed_in_again" in back.headers["location"]
    tools = await personal.registry.rows("SELECT selected FROM personal_mcp_tool WHERE id=?", (personal.tool_id,))
    assert tools[0]["selected"] == 1


async def test_denied_sign_in_and_states_of_the_other_kind(personal, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "1")
    _widen(personal.fixture.db_path)
    _identity_provider(monkeypatch)
    async with await _oauth_client(personal) as client:
        started = await client.post("/api/v1/personal-mcp/oauth/authorize", json={"name": "Notes", "endpoint": _ENDPOINT})
        denied = await client.get(
            "/api/v1/personal-mcp/oauth/callback", params={"error": "access_denied", "state": _state(started.json()["authorize_url"])}
        )
        assert "mcp_oauth=failed&mcp_oauth_reason=authorization_denied" in denied.headers["location"]
        user = await personal.fixture.users.get_user(personal.ctx.user_id)
        await personal.fixture.users.create_mcp_oauth_pending(
            state="team-state",
            team_id=user.team_id,
            source_id="ots_team",
            code_verifier="v" * 43,
            redirect_uri="http://bowhead.example/cb",
            token_endpoint=f"{_IDP}/token",
            client_id="cid",
            resource=None,
            created_by=user.id,
        )
        foreign = await client.get("/api/v1/personal-mcp/oauth/callback", params={"code": "c", "state": "team-state"})
    assert "mcp_oauth_reason=session_expired" in foreign.headers["location"]
    # the team source's pending authorization is untouched by the personal callback
    assert await personal.fixture.users.take_mcp_oauth_pending("team-state") is not None


async def test_sign_in_is_offered_only_without_a_credential(personal, monkeypatch):  # noqa: F811
    from bowhead_core.personal_mcp import PersonalMcpError

    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "1")
    served = _identity_provider(monkeypatch)

    async def unreachable(endpoint, headers):
        raise PersonalMcpError("personal_mcp_discovery_failed")

    monkeypatch.setattr("bowhead_gateway.routes.personal_mcp.discover", unreachable)
    async with await _oauth_client(personal) as client:
        offered = await client.post("/api/v1/personal-mcp", json={"name": "Notes", "endpoint": _ENDPOINT})
        assert offered.status_code == 409 and offered.json()["detail"] == {"code": "personal_mcp_needs_oauth", "provider_host": "idp.example"}
        served.clear()
        with_token = await client.post("/api/v1/personal-mcp", json={"name": "Notes", "endpoint": _ENDPOINT, "auth_type": "bearer", "value": "wrong"})
        assert with_token.status_code == 400 and with_token.json()["detail"] == "personal_mcp_discovery_failed"
        assert served == []  # a refused credential is not a reason to sign in
        _identity_provider(monkeypatch, registration=False)
        admin_only = await client.post("/api/v1/personal-mcp", json={"name": "Notes", "endpoint": _ENDPOINT})
        assert admin_only.status_code == 422 and admin_only.json()["detail"]["code"] == "personal_mcp_admin_app_required"


async def test_sign_in_routes_are_closed_while_the_feature_is_off(personal, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "0")
    async with await _oauth_client(personal) as client:
        started = await client.post("/api/v1/personal-mcp/oauth/authorize", json={"name": "Notes", "endpoint": _ENDPOINT})
        assert started.status_code == 404 and started.json()["detail"] == {"code": "personal_mcp_oauth_unavailable"}
        assert (await client.get("/api/v1/personal-mcp/capabilities")).json()["oauth_connectable"] is False


# ── P1: a bound connection that needs the owner (decided 2026-11-25: keep the tool, answer on call) ──


def _needs_owner(personal) -> None:  # noqa: F811
    with sqlite3.connect(personal.fixture.db_path) as db:
        db.execute("UPDATE personal_mcp_connection SET status='needs_reauth',version=version+1 WHERE id=?", (personal.connection_id,))


async def test_bound_tool_needing_sign_in_stays_listed_and_never_sends(personal, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "1")
    _sign_in(personal)
    _needs_owner(personal)
    calls = _remote(monkeypatch)
    specs = await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx)
    assert [spec.personal_mcp_label for spec in specs] == ["My CRM · search"]
    assert "Unavailable in this run" in specs[0].schema.description
    result = await specs[0].handler({"query": "q"}, personal.ctx)
    assert result["error_kind"] == "personal_mcp_sign_in_ended" and "My Connections" in result["error"]
    assert calls == []
    assert await personal.registry.rows("SELECT * FROM personal_mcp_invocation") == []  # no consent card, nothing queued


async def test_rejected_token_asks_to_update_the_credential(personal, monkeypatch):  # noqa: F811
    _needs_owner(personal)  # the fixture's Bearer connection after a 401
    calls = _remote(monkeypatch)
    specs = await build_personal_specs(personal.registry, personal.ctx.user_id, personal.binding, personal.ctx)
    result = await specs[0].handler({"query": "q"}, personal.ctx)
    assert result["error_kind"] == "personal_mcp_credential_rejected" and "update the credential" in result["error"]
    assert calls == []


async def test_other_bound_tools_still_run_and_other_states_still_fail_closed(personal, monkeypatch):  # noqa: F811
    owner = personal.ctx.user_id
    second = await personal.registry.save_connection(
        owner, "Docs", "http://localhost:9001/mcp", "bearer", {"value": "docs-token"}, [_tool("lookup", {"type": "object"})]
    )
    lookup = next(item for item in await personal.registry.connections(owner) if item["id"] == second)["tools"][0]["id"]
    await personal.registry.configure(owner, second, 1, "ready", [lookup])
    await personal.registry.bind(owner, personal.fixture.team_id, "osa_exact", personal.binding.revision_ref, [personal.tool_id, lookup], 1)
    _needs_owner(personal)
    specs = await build_personal_specs(personal.registry, owner, personal.binding, personal.ctx)
    runnable = [spec.personal_mcp_label for spec in specs if "Unavailable in this run" not in spec.schema.description]
    assert sorted(spec.personal_mcp_label for spec in specs) == ["Docs · lookup", "My CRM · search"]
    assert runnable == ["Docs · lookup"]
    # A connection the owner turned off is a binding change, not a sign-in: unchanged, it fails closed.
    with sqlite3.connect(personal.fixture.db_path) as db:
        db.execute("UPDATE personal_mcp_connection SET status='disabled' WHERE id=?", (second,))
        db.execute("DELETE FROM personal_mcp_manifest")
    with pytest.raises(PersonalMcpError, match="binding_unavailable"):
        await build_personal_specs(personal.registry, owner, personal.binding, personal.ctx)


async def test_sign_in_is_on_unless_the_deployment_turns_it_off(monkeypatch):
    from bowhead_core.personal_mcp import personal_mcp_oauth_enabled

    monkeypatch.delenv("FEATURE_PERSONAL_MCP_OAUTH", raising=False)
    assert personal_mcp_oauth_enabled()
    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "0")
    assert not personal_mcp_oauth_enabled()


# ── A refused remembered token (116 acceptance, 2026-11-25) ──────────────────


def _refusing_remote(monkeypatch, refused: set[str]) -> list[str]:
    """Remote that answers HTTP 401 for the tokens in ``refused`` and runs the tool for any other."""
    seen: list[str] = []

    class Remote:
        async def call_tool(self, name, args):
            return SimpleNamespace(is_error=False, structured_content={"query": args["query"]}, content=[])

    @asynccontextmanager
    async def connect(endpoint, headers, **kwargs):
        token = headers["Authorization"].removeprefix("Bearer ")
        seen.append(token)
        if token in refused:
            request = httpx2.Request("POST", endpoint)
            raise httpx2.HTTPStatusError("refused", request=request, response=httpx2.Response(401, request=request))
        yield Remote()

    monkeypatch.setattr("bowhead_core.personal_mcp.connect_remote_mcp", connect)
    return seen


def _remember(personal, token: str = "first-access") -> None:  # noqa: F811
    from bowhead_core.personal_mcp import prime_personal_oauth

    prime_personal_oauth(personal.registry, personal.ctx.user_id, personal.connection_id, token, 3600)


async def test_revoked_access_is_reported_as_not_run_and_asks_to_sign_in_again(personal, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "1")
    _sign_in(personal)
    _remember(personal)
    # n8n deletes the client registration when access is revoked there.
    seen = _provider(monkeypatch, [(401, {"error": "invalid_client"})])
    sent = _refusing_remote(monkeypatch, {"first-access"})
    version = _connection(personal)["version"]
    result = await _approved_call(personal, monkeypatch)
    assert result["ok"] is False and result["error_kind"] == "personal_mcp_sign_in_ended" and result["error"].startswith("Not run")
    assert sent == ["first-access"] and seen == ["stored-refresh"]
    row = _connection(personal)
    assert row["status"] == "needs_reauth" and row["version"] == version + 1
    stored = personal.registry.unseal(personal.ctx.user_id, personal.connection_id, "credentials", row["auth_cipher"])
    assert stored["oauth_grant_state"]["reason"] == "invalid_client"  # the next sign-in must not reuse that client
    invocations = await personal.registry.rows("SELECT status FROM personal_mcp_invocation")
    assert [item["status"] for item in invocations] == ["denied"]  # refused, so never "unknown"


async def test_refused_remembered_token_is_renewed_and_the_call_runs_once(personal, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "1")
    _sign_in(personal)
    _remember(personal)
    seen = _provider(monkeypatch, [(200, {"access_token": "renewed-access", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "rotated"})])
    sent = _refusing_remote(monkeypatch, {"first-access"})
    result = await _approved_call(personal, monkeypatch)
    assert result == {"ok": True, "data": {"query": "q"}}
    assert sent == ["first-access", "renewed-access"] and seen == ["stored-refresh"]
    assert _connection(personal)["status"] == "ready"
    invocations = await personal.registry.rows("SELECT status FROM personal_mcp_invocation")
    assert [item["status"] for item in invocations] == ["ok"]


async def test_refused_static_credential_is_reported_as_not_run(personal, monkeypatch):  # noqa: F811
    _refusing_remote(monkeypatch, {"sensitive-personal-token"})
    result = await _approved_call(personal, monkeypatch)
    assert result["ok"] is False and result["error_kind"] == "personal_mcp_credential_rejected"
    assert _connection(personal)["status"] == "needs_reauth"
    invocations = await personal.registry.rows("SELECT status FROM personal_mcp_invocation")
    assert [item["status"] for item in invocations] == ["denied"]


async def test_signing_in_again_registers_a_new_client(personal, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FEATURE_PERSONAL_MCP_OAUTH", "1")
    _sign_in(personal)
    owner, identity = personal.ctx.user_id, personal.connection_id
    row = _connection(personal)
    stored = personal.registry.unseal(owner, identity, "credentials", row["auth_cipher"])
    # A stored client that matches the callback would be reused unless each sign-in registers afresh.
    stored["oauth_client_info"] = {
        "client_id": "deleted-at-the-service",
        "redirect_uris": ["http://bowhead.example/api/v1/personal-mcp/oauth/callback"],
        "token_endpoint_auth_method": "none",
    }
    with sqlite3.connect(personal.fixture.db_path) as db:
        db.execute(
            "UPDATE personal_mcp_connection SET auth_cipher=? WHERE id=?", (personal.registry.seal(owner, identity, "credentials", stored), identity)
        )
    served = _identity_provider(monkeypatch)
    async with await _oauth_client(personal) as client:
        started = await client.post("/api/v1/personal-mcp/oauth/authorize", json={"connection_id": identity, "version": row["version"]})
    assert started.status_code == 200, started.text
    from urllib.parse import urlsplit

    assert parse_qs(urlsplit(started.json()["authorize_url"]).query)["client_id"] == ["cid"]
    assert "/register" in served
