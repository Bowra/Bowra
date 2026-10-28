"""Google Workspace connection: admin setup, per-member OAuth, isolation and token lifecycle."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from bowhead_core.builtin_tool_grants import BuiltinToolGrantStore
from bowhead_core.google_workspace import GoogleWorkspaceError, scopes_for, token_broker
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import google_workspace as routes
from cryptography.fernet import Fernet
from fastapi import HTTPException

from bowhead_core import google_workspace

pytestmark = pytest.mark.anyio

_DOMAIN = "acme.example"


def _id_token(*, email: str, hd: str = _DOMAIN, aud: str = "client-1", sub: str = "sub-1") -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    claims = {
        "iss": "https://accounts.google.com",
        "aud": aud,
        "exp": time.time() + 600,
        "hd": hd,
        "email": email,
        "email_verified": True,
        "sub": sub,
    }
    return f"{part({'alg': 'RS256'})}.{part(claims)}.sig"


class _Google:
    """Fake token endpoint; each test queues the next responses."""

    def __init__(self) -> None:
        self.token_responses: list[tuple[int, dict]] = []
        self.token_requests: list[dict[str, list[str]]] = []
        self.revoked: list[str] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        form = parse_qs(request.content.decode())
        if request.url.path == "/revoke":
            self.revoked.append(form["token"][0])
            return httpx2.Response(200)
        self.token_requests.append(form)
        status, body = self.token_responses.pop(0)
        return httpx2.Response(status, json=body)


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> _Google:
    fake = _Google()
    google_workspace.set_http_client_factory(lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(fake.handler)))
    yield fake
    google_workspace.set_http_client_factory(None)
    google_workspace._brokers.clear()


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initialize_migrated_core_database, google: _Google) -> SimpleNamespace:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FEATURE_GOOGLE_WORKSPACE", "1")

    async def public_url(_request: object, *, team_id: str, path: str) -> str:
        return f"https://bowhead.example{path}"

    monkeypatch.setattr(routes, "team_public_app_url", public_url)
    db_path = tmp_path / "bowhead.db"
    initialize_migrated_core_database(db_path)
    store = UserStore(str(db_path))
    team = await store.create_team(name="Acme", max_seats=10)
    admin = await store.create_user(team_id=team.id, email=f"admin@{_DOMAIN}")
    member = await store.create_user(team_id=team.id, email=f"member@{_DOMAIN}")
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        for user, role in ((admin, "team_admin"), (member, "member")):
            await db.execute("UPDATE team_members SET role=?, status='active' WHERE user_id=? AND team_id=?", (role, user.id, team.id))
        await db.commit()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(user_store=store)))
    return SimpleNamespace(store=store, team=team, admin=admin, member=member, request=request, google=google, db_path=str(db_path))


async def _configure(env: SimpleNamespace, **overrides) -> dict:
    body = {"client_id": "client-1", "client_secret": "secret-1", "domain": _DOMAIN, "picker_api_key": "pk", "picker_project_number": "123"}
    body.update(overrides)
    return (await routes.save_connection(routes.ConnectionBody(**body), env.request, env.admin))["connection"]


async def _authorize(env: SimpleNamespace, user, *, token_body: dict) -> str:
    url = (await routes.connect(env.request, user))["authorize_url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    env.google.token_responses.append((200, token_body))
    response = await routes.oauth_callback(env.request, code="code-1", state=state)
    return response.headers["location"]


def _tokens(email: str, *, refresh: str | None = "refresh-1", scopes: tuple[str, ...] | None = None) -> dict:
    body = {
        "access_token": "access-1",
        "expires_in": 3600,
        "scope": " ".join(scopes or scopes_for({"base", "read_by_link", "edit_docs", "edit_sheets"})),
        "id_token": _id_token(email=email),
    }
    if refresh:
        body["refresh_token"] = refresh
    return body


async def test_feature_flag_off_hides_every_route(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_GOOGLE_WORKSPACE", "0")
    with pytest.raises(HTTPException) as exc:
        await routes._available()
    assert exc.value.status_code == 404
    response = await routes.oauth_callback(env.request, code="c", state="s")
    assert parse_qs(urlsplit(response.headers["location"]).query)["google_reason"] == ["connection_unavailable"]


async def test_no_bundle_ever_requests_a_restricted_scope() -> None:
    every = set(scopes_for(set(google_workspace.BUNDLES)))
    api = "https://www.googleapis.com/auth/"
    restricted = {"https://mail.google.com/"} | {
        api + name
        for name in (
            "gmail.readonly",
            "gmail.modify",
            "gmail.compose",
            "gmail.metadata",
            "drive",
            "drive.readonly",
            "drive.metadata",
            "drive.metadata.readonly",
        )
    }
    assert every.isdisjoint(restricted)
    assert not any("gmail" in scope for scope in every)


async def test_admin_setup_keeps_secrets_write_only_and_requires_own_grant_to_enable(env: SimpleNamespace) -> None:
    connection = await _configure(env)

    assert connection["status"] == "disabled"
    assert connection["client_secret_configured"] is True and connection["picker_api_key_configured"] is True
    assert "secret-1" not in json.dumps(connection) and connection.get("picker_api_key") != "pk"
    assert connection["redirect_uri"] == "https://bowhead.example/api/v1/google/oauth/callback"
    assert {b["id"]: b["enabled"] for b in connection["bundles"]} == {
        "base": True,
        "read_by_link": True,
        "edit_docs": True,
        "edit_sheets": True,
        "calendar": False,
    }
    with pytest.raises(HTTPException) as exc:
        await routes.update_status(routes.StatusBody(enabled=True), env.request, env.admin)
    assert exc.value.detail == {"code": "google_admin_authorization_required"}


async def test_admin_authorizes_then_enables_and_member_connects_with_own_grant(env: SimpleNamespace) -> None:
    await _configure(env)
    # Members cannot see or authorize a connection that is still being set up.
    with pytest.raises(HTTPException):
        await routes.connect(env.request, env.member)
    assert (await routes.my_authorization(env.request, env.member))["available"] is False

    url = (await routes.connect(env.request, env.admin))["authorize_url"]
    query = parse_qs(urlsplit(url).query)
    assert query["hd"] == [_DOMAIN] and query["prompt"] == ["consent"] and query["access_type"] == ["offline"]
    assert query["login_hint"] == [f"admin@{_DOMAIN}"] and query["code_challenge_method"] == ["S256"]
    env.google.token_responses.append((200, _tokens(f"admin@{_DOMAIN}")))
    response = await routes.oauth_callback(env.request, code="code-1", state=query["state"][0])
    assert response.headers["location"] == "/team/settings/tools/?google=connected"
    exchange = env.google.token_requests[-1]
    assert exchange["redirect_uri"] == ["https://bowhead.example/api/v1/google/oauth/callback"]
    assert exchange["code_verifier"][0]

    enabled = (await routes.update_status(routes.StatusBody(enabled=True), env.request, env.admin))["connection"]
    assert enabled["status"] == "enabled" and enabled["authorized_members"] == 1

    view = await routes.my_authorization(env.request, env.member)
    assert view["available"] is True and view["authorization"]["status"] == "not_connected"
    with pytest.raises(HTTPException) as exc:
        await routes.picker_config(env.request, env.member)
    assert exc.value.detail == {"code": "google_not_connected"}

    location = await _authorize(env, env.member, token_body=_tokens(f"member@{_DOMAIN}", refresh="refresh-member"))
    assert location == "/connections/?google=connected"
    grants = BuiltinToolGrantStore(env.db_path)
    tool_id = enabled["id"]
    member_grant = await grants.get(tool_id, env.member.id)
    assert member_grant is not None and member_grant.provider_email == f"member@{_DOMAIN}"
    assert await grants.refresh_token(member_grant) == "refresh-member"
    assert await routes.picker_config(env.request, env.member) == {
        "client_id": "client-1",
        "api_key": "pk",
        "project_number": "123",
        "login_hint": f"member@{_DOMAIN}",
        "hosted_domain": _DOMAIN,
    }


@pytest.mark.parametrize(
    ("claims", "reason"),
    [({"email": f"someone-else@{_DOMAIN}"}, "email_mismatch"), ({"email": f"admin@{_DOMAIN}", "hd": "other.example"}, "domain_mismatch")],
)
async def test_callback_refuses_foreign_identity_and_stores_nothing(env: SimpleNamespace, claims: dict, reason: str) -> None:
    await _configure(env)
    body = _tokens(f"admin@{_DOMAIN}")
    body["id_token"] = _id_token(**claims)
    location = await _authorize(env, env.admin, token_body=body)

    assert parse_qs(urlsplit(location).query)["google_reason"] == [reason]
    tool = await env.store.get_team_builtin_tool(env.team.id, "google_workspace")
    assert await BuiltinToolGrantStore(env.db_path).get(tool.id, env.admin.id) is None


async def test_state_is_single_use(env: SimpleNamespace) -> None:
    await _configure(env)
    url = (await routes.connect(env.request, env.admin))["authorize_url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    env.google.token_responses.append((200, _tokens(f"admin@{_DOMAIN}")))
    await routes.oauth_callback(env.request, code="code-1", state=state)

    replay = await routes.oauth_callback(env.request, code="code-1", state=state)
    assert parse_qs(urlsplit(replay.headers["location"]).query)["google_reason"] == ["session_expired"]


async def test_incremental_consent_without_refresh_token_keeps_the_old_one(env: SimpleNamespace) -> None:
    connection = await _configure(env, bundles={"calendar": False})
    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}", refresh="refresh-1"))
    await _configure(env, bundles={"calendar": True})

    url = (await routes.connect(env.request, env.admin))["authorize_url"]
    assert "prompt" not in parse_qs(urlsplit(url).query)  # active grant ⇒ incremental, no forced consent
    state = parse_qs(urlsplit(url).query)["state"][0]
    env.google.token_responses.append((200, _tokens(f"admin@{_DOMAIN}", refresh=None, scopes=scopes_for(set(google_workspace.BUNDLES)))))
    await routes.oauth_callback(env.request, code="code-2", state=state)

    grants = BuiltinToolGrantStore(env.db_path)
    grant = await grants.get(connection["id"], env.admin.id)
    assert await grants.refresh_token(grant) == "refresh-1"
    assert "https://www.googleapis.com/auth/calendar.events.readonly" in grant.granted_scopes


async def test_broker_refreshes_once_and_marks_invalid_grant_for_reauth(env: SimpleNamespace) -> None:
    connection = await _configure(env)
    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}"))
    tool = await env.store.get_team_builtin_tool(env.team.id, "google_workspace")
    auth = await env.store.get_team_builtin_tool_instance_auth_decrypted(env.team.id, tool.id)
    broker = token_broker(env.db_path)

    env.google.token_responses.append((200, {"access_token": "fresh", "expires_in": 3600}))
    token, _ = await broker.access_token(tool=tool, auth=auth, user_id=env.admin.id, required_bundles=frozenset({"base"}))
    again, _ = await broker.access_token(tool=tool, auth=auth, user_id=env.admin.id, required_bundles=frozenset({"base"}))
    assert token == again == "fresh"
    assert env.google.token_requests[-1]["grant_type"] == ["refresh_token"]

    with pytest.raises(GoogleWorkspaceError) as missing:
        await broker.access_token(tool=tool, auth=auth, user_id=env.admin.id, required_bundles=frozenset({"calendar"}))
    assert missing.value.code == "google_scope_missing"
    with pytest.raises(GoogleWorkspaceError) as other:
        await broker.access_token(tool=tool, auth=auth, user_id=env.member.id, required_bundles=frozenset({"base"}))
    assert other.value.code == "google_not_connected"

    broker.forget((await BuiltinToolGrantStore(env.db_path).get(connection["id"], env.admin.id)).id)
    env.google.token_responses.append((400, {"error": "invalid_grant"}))
    with pytest.raises(GoogleWorkspaceError) as reauth:
        await broker.access_token(tool=tool, auth=auth, user_id=env.admin.id, required_bundles=frozenset({"base"}))
    assert reauth.value.code == "google_reauth_required"
    assert (await BuiltinToolGrantStore(env.db_path).get(connection["id"], env.admin.id)).status == "needs_reauth"
    url = (await routes.connect(env.request, env.admin))["authorize_url"]
    assert parse_qs(urlsplit(url).query)["prompt"] == ["consent"]


async def test_changing_the_client_revokes_and_drops_every_grant(env: SimpleNamespace) -> None:
    connection = await _configure(env)
    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}", refresh="refresh-old"))
    await routes.update_status(routes.StatusBody(enabled=True), env.request, env.admin)

    updated = await _configure(env, client_id="client-2")

    assert updated["status"] == "disabled" and updated["authorized_members"] == 0
    assert "refresh-old" in env.google.revoked
    assert await BuiltinToolGrantStore(env.db_path).get(connection["id"], env.admin.id) is None


async def test_disconnect_and_delete_connection_revoke_tokens(env: SimpleNamespace) -> None:
    connection = await _configure(env)
    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}", refresh="refresh-a"))
    await routes.disconnect(env.request, env.admin)
    assert "refresh-a" in env.google.revoked
    assert await BuiltinToolGrantStore(env.db_path).get(connection["id"], env.admin.id) is None

    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}", refresh="refresh-b"))
    await routes.delete_connection(env.request, env.admin)
    assert "refresh-b" in env.google.revoked
    assert await env.store.get_team_builtin_tool(env.team.id, "google_workspace") is None


async def test_me_capability_tracks_feature_and_enabled_connection(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_gateway.routes.auth import _google_workspace_available

    assert await _google_workspace_available(env.request, env.team.id) is False  # no connection yet
    await _configure(env)
    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}"))
    assert await _google_workspace_available(env.request, env.team.id) is False  # configured, not enabled
    await routes.update_status(routes.StatusBody(enabled=True), env.request, env.admin)
    assert await _google_workspace_available(env.request, env.team.id) is True
    monkeypatch.setenv("FEATURE_GOOGLE_WORKSPACE", "0")
    assert await _google_workspace_available(env.request, env.team.id) is False


def test_google_catalog_entries_carry_every_locale() -> None:
    from bowhead_core.builtin_tool_providers import providers

    google = next(provider for provider in providers() if provider.name == "google_workspace")
    for operation in google.catalog.values():
        for field in ("display_name_en", "description_en", "display_name_de", "description_de", "display_name_fr", "description_fr"):
            assert getattr(operation, field), field


@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
@pytest.mark.parametrize("denied", [False, True])
async def test_oauth_return_restores_saved_prefix_without_proxy_headers(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, prefix: str, denied: bool
) -> None:
    from bowhead_core.public_entries import normalize_public_base_url
    from bowhead_gateway.public_entry_context import _context_from_normalized, reset_public_access_context, set_public_access_context

    await _configure(env)
    await _authorize(env, env.admin, token_body=_tokens(env.admin.email))
    await routes.update_status(routes.StatusBody(enabled=True), env.request, env.admin)

    async def public_url(_request: object, *, team_id: str, path: str) -> str:
        return f"https://public.example:10010{prefix}{path}"

    monkeypatch.setattr(routes, "team_public_app_url", public_url)
    start = await routes.connect(env.request, env.member)
    query = parse_qs(urlsplit(start["authorize_url"]).query)
    token = set_public_access_context(_context_from_normalized(normalize_public_base_url("http://internal.example:8000"), "direct", entry=None))
    try:
        if not denied:
            env.google.token_responses.append((200, _tokens(env.member.email)))
        response = await routes.oauth_callback(
            env.request, code="code" if not denied else "", state=query["state"][0], error="access_denied" if denied else ""
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith(f"{prefix}/connections/?google=")
        if not denied:
            assert env.google.token_requests[-1]["redirect_uri"] == query["redirect_uri"]
    finally:
        reset_public_access_context(token)
