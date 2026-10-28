from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin

import httpx2
import pytest
import respx
from bowhead_core.public_entries import PublicEntryStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.public_entry_context import (
    PublicEntryContextMiddleware,
    bind_team_public_entry,
    public_access_context_for_entry,
    reset_public_access_context,
    set_public_access_context,
)
from bowhead_gateway.routes import oauth, team_tool_sources
from bowhead_gateway.routes.auth import LastTeamBody, set_last_team
from fastapi import FastAPI
from starlette.requests import Request


class BrokenEntries(PublicEntryStore):
    async def list_active(self):
        raise OSError("registry unavailable")

    async def list_all(self):
        raise OSError("registry unavailable")

    async def get(self, *args):
        raise OSError("registry unavailable")

    async def default_for_team(self, *args):
        raise OSError("registry unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
async def test_feishu_invalid_state_return_keeps_browser_prefix(prefix: str) -> None:
    token = set_public_access_context(None)
    try:
        response = oauth._oauth_error_redirect(provider="feishu", code="oauth_state_invalid")
    finally:
        reset_public_access_context(token)
    assert (
        urljoin(f"https://public.example{prefix}/api/v1/auth/oauth/feishu/callback", response.headers["location"])
        == f"https://public.example{prefix}/login/?oauth_error=oauth_state_invalid&oauth_provider=feishu"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
@pytest.mark.parametrize("kind", ["feishu", "mcp"])
async def test_registry_outage_does_not_gate_callback_or_recompute_saved_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str, kind: str
) -> None:
    users = UserStore(tmp_path / "users.db")
    await users.initialize()
    entries = BrokenEntries(tmp_path / "entries.db")
    app = FastAPI()
    app.state.user_store = users
    app.state.public_entry_store = entries
    app.add_middleware(PublicEntryContextMiddleware)
    app.include_router(oauth.router, prefix="/api/v1/auth/oauth")
    app.include_router(team_tool_sources.router, prefix="/api/v1/team-tool-sources")
    path = "/api/v1/auth/oauth/feishu/callback" if kind == "feishu" else "/api/v1/team-tool-sources/oauth/callback"
    uri = f"https://public.example:10010{prefix}{path}"
    if kind == "feishu":
        await oauth._store_state(users.db_path, "probe", "feishu", public_entry_id="pe_probe", redirect_uri=uri)
    else:
        await users.create_mcp_oauth_pending(
            state="probe",
            team_id="team_a",
            source_id="source_a",
            code_verifier="v" * 43,
            redirect_uri=uri,
            token_endpoint="https://provider.example/token",
            client_id="client",
            resource=None,
            created_by="usr_a",
            public_entry_id="pe_probe",
        )
    exchanged = []
    if kind == "mcp":

        async def exchange_code(_storage, **kwargs):
            exchanged.append(kwargs["redirect_uri"])

        monkeypatch.setattr("bowhead_core.mcp_oauth.exchange_mcp_oauth_code", exchange_code)
    with respx.mock:
        exchange = respx.post(oauth._PROVIDERS["feishu"]["token_url"] if kind == "feishu" else "https://provider.example/token").mock(
            return_value=httpx2.Response(400)
        )
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://internal.example:8000"
        ) as client:
            response = await client.get(path, params={"state": "probe", "code": "code"})
        assert response.status_code == 303
        if kind == "feishu":
            from urllib.parse import parse_qs

            assert parse_qs(exchange.calls[0].request.content.decode())["redirect_uri"] == [uri]
        if kind == "mcp":
            assert exchanged == [uri]
        assert response.headers["location"].startswith(f"{prefix}/login/" if kind == "feishu" else f"{prefix}/settings/tools")


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["matched", "missing", "outage"])
async def test_team_switch_uses_host_match_and_keeps_optional_entries_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_kind: str
) -> None:
    monkeypatch.setenv("FEATURE_MULTI_TEAM_AUTHZ", "1")
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()

    async def active(team: str, base: str):
        pending, challenge = await entries.create_pending(team_id=team, base_url=base, actor_id="usr_owner")
        return await entries.verify_challenge(entry_id=pending.id, token=challenge)

    source = await active("team_source", "https://current.example:10010/source")
    if target_kind == "matched":
        await active("team_target", "https://default.example/bowhead")
        await active("team_target", "https://current.example:10010/target")
    registry = BrokenEntries(tmp_path / "broken.db") if target_kind == "outage" else entries
    request = Request(
        {"type": "http", "method": "PUT", "path": "/", "headers": [], "app": SimpleNamespace(state=SimpleNamespace(public_entry_store=registry))}
    )

    class Users:
        switched = False

        async def user_has_active_team_membership(self, *args):
            return True

        async def set_active_team(self, *args):
            self.switched = True
            return True

    users = Users()
    token = set_public_access_context(public_access_context_for_entry(source))
    try:
        result = await set_last_team(LastTeamBody(team_id="team_target"), request, user=SimpleNamespace(id="usr_a"), store=users)
        if target_kind == "matched":
            assert result["redirect_url"] == "https://current.example:10010/target/"
        else:
            assert "redirect_url" not in result
    finally:
        reset_public_access_context(token)
    assert users.switched


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["hint", "proxy", "cookie", "compatible_proxy", "duplicate_cookie"])
async def test_registry_outage_retains_only_unbound_address_and_never_reissues_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    from bowhead_gateway.public_entry_context import _cookie_header, current_public_access_context

    entries = PublicEntryStore(tmp_path / "active.db")
    await entries.initialize()
    pending, challenge = await entries.create_pending(team_id="team_saved", base_url="https://public.example/bowhead", actor_id="usr_owner")
    entry = await entries.verify_challenge(entry_id=pending.id, token=challenge)
    monkeypatch.setenv("BOWHEAD_JWT_SECRETS", "current=test-key")
    cookie = _cookie_header(public_access_context_for_entry(entry)).split(";", 1)[0]
    headers = {"Cookie": cookie}
    expected = entry.base_url
    if evidence == "hint":
        headers["X-Bowhead-Public-Base"] = "https://browser.example/entry"
        expected = "https://browser.example/entry"
    elif evidence == "proxy":
        headers.update({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "proxy.example", "X-Forwarded-Prefix": "/proxy"})
        expected = "https://proxy.example/proxy"
    elif evidence == "compatible_proxy":
        headers.update({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "public.example"})
    elif evidence == "duplicate_cookie":
        headers["Cookie"] = cookie + "; " + cookie
        expected = "http://internal.example:8000"
    app = FastAPI()
    app.state.public_entry_store = BrokenEntries(tmp_path / "broken.db")
    app.add_middleware(PublicEntryContextMiddleware)

    @app.get("/context")
    async def context(request: Request):
        await bind_team_public_entry(request, team_id="team_target")
        return current_public_access_context().to_browser_dict()

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://internal.example:8000"
    ) as client:
        response = await client.get("/context", headers=headers)
    assert response.status_code == 200
    assert response.json()["baseUrl"] == expected
    assert response.json()["entryId"] is None and response.json()["teamId"] is None
    assert "set-cookie" not in response.headers
