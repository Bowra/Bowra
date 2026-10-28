from pathlib import Path

"""Request transport, team links and optional registry failures stay independent."""
from types import SimpleNamespace
from urllib.parse import urljoin

import httpx2
import pytest
from bowhead_core.artifact_store import LocalBackend
from bowhead_core.public_entries import PublicEntryStore
from bowhead_gateway._scheduled_runner import _deep_link, _deliver_run_result
from bowhead_gateway.auth_mutation_security import require_same_origin_json
from bowhead_gateway.identity_email import render_identity_email
from bowhead_gateway.public_entry_context import (
    PublicAccessContext,
    PublicEntryContextMiddleware,
    _cookie_header,
    bind_team_public_entry,
    current_public_access_context,
    current_request_public_access_context,
    public_access_context_for_entry,
    reset_public_access_context,
    resolve_public_access_context,
    set_public_access_context,
)
from bowhead_gateway.public_url import (
    PublicEntryRequiredError,
    oauth_return_path,
    public_app_base_url,
    public_app_url,
    restore_oauth_public_access_context,
    team_public_app_url,
)
from bowhead_gateway.routes import feishu, google_workspace, members, slack, team_tool_sources
from bowhead_gateway.routes.auth import _refresh_cookie_path
from bowhead_gateway.slack.store import SlackOAuthState
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from test_entry_outage_regressions import BrokenEntries

pytestmark = pytest.mark.asyncio


async def active(entries, team, base):
    (pending, challenge) = await entries.create_pending(team_id=team, base_url=base, actor_id="usr_owner")
    return await entries.verify_challenge(entry_id=pending.id, token=challenge)


def request_for(entries, base="https://browser.example:10010/custom"):
    return Request(
        {
            "type": "http",
            "scheme": "http",
            "method": "POST",
            "path": "/api/v1/probe",
            "headers": [
                (b"host", b"internal.example:8000"),
                (b"x-bowhead-public-base", base.encode()),
                (b"origin", base.split("/", 3)[0].encode() + b"//" + base.split("/", 3)[2].encode()),
                (b"content-type", b"application/json"),
            ],
            "app": SimpleNamespace(state=SimpleNamespace(public_entry_store=entries)),
        }
    )


@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
async def test_team_default_does_not_replace_browser_security_cookie_or_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "s" * 32)
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    await active(entries, "team_a", "https://default.example/default")
    request = request_for(entries, f"https://browser.example:10010{prefix}")
    token = set_public_access_context(await resolve_public_access_context(request.scope, entries))
    try:
        await bind_team_public_entry(request, team_id="team_a")
        require_same_origin_json(request)
        assert current_request_public_access_context().base_url == f"https://browser.example:10010{prefix}"
        assert _refresh_cookie_path() == f"{prefix}/api/v1/auth"
        assert f"Path={prefix}/" in _cookie_header(current_public_access_context())
        assert await team_public_app_url(request, team_id="team_a", path="/") == "https://default.example/default/"
    finally:
        reset_public_access_context(token)


@pytest.mark.parametrize("hint", ["invalid_browser", "stale_entry", "none"])
async def test_invalid_metadata_keeps_complete_forwarded_address(tmp_path: Path, hint: str) -> None:
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    headers = [(b"x-forwarded-proto", b"https"), (b"x-forwarded-host", b"public.example:10010"), (b"x-forwarded-prefix", b"/bowhead")]
    if hint == "invalid_browser":
        headers.append((b"x-bowhead-public-base", b"invalid"))
    if hint == "stale_entry":
        headers.append((b"x-bowhead-public-entry", b"pe_" + b"a" * 24))
    scope = {"type": "http", "scheme": "http", "path": "/callback", "headers": headers}
    context = await resolve_public_access_context(scope, entries)
    assert context.base_url == "https://public.example:10010/bowhead" and context.entry_id is None


@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
@pytest.mark.parametrize("scheme,port", [("https", "443"), ("http", "10010")])
@pytest.mark.parametrize("evidence", ["browser", "cookie"])
@pytest.mark.integration
async def test_matched_proxy_without_prefix_preserves_explicit_browser_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str, scheme: str, port: str, evidence: str
) -> None:
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "s" * 32)
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    selected = await active(entries, "team_a", f"{scheme}://browser.example:{port}/registered")
    origin = f"{scheme}://browser.example" + (f":{port}" if port != "443" else "")
    request = request_for(entries, f"{origin}{prefix}")
    if evidence == "cookie":
        request.scope["headers"] = [(key, value) for key, value in request.scope["headers"] if key != b"x-bowhead-public-base"]
        saved = PublicAccessContext(f"{origin}{prefix}", origin, prefix, f"{origin}{prefix}/", "browser_hint")
        request.scope["headers"].append((b"cookie", _cookie_header(saved).split(";", 1)[0].encode()))
    request.scope["headers"].extend([(b"x-forwarded-proto", scheme.encode()), (b"x-forwarded-host", f"browser.example:{port}".encode())])
    context = await resolve_public_access_context(request.scope, entries)
    token = set_public_access_context(context)
    try:
        await bind_team_public_entry(request, team_id="team_a")
        require_same_origin_json(request)
        assert current_public_access_context().entry_id == selected.id
        assert current_request_public_access_context().base_url == f"{origin}{prefix}"
        assert _refresh_cookie_path() == f"{prefix}/api/v1/auth"
        assert f"Path={prefix}/" in _cookie_header(current_public_access_context())
        assert oauth_return_path("/login/", callback_path="/api/v1/auth/oidc/callback") == f"{prefix}/login/"
        assert await team_public_app_url(request, team_id="team_a", path="/") == f"{selected.base_url}/"
    finally:
        reset_public_access_context(token)


@pytest.mark.parametrize("registry", ["empty", "unavailable"])
@pytest.mark.parametrize("address", ["direct", "partial_proxy"])
@pytest.mark.integration
async def test_unknown_request_address_cannot_be_promoted_to_public_links(tmp_path: Path, registry: str, address: str) -> None:
    entries = BrokenEntries(tmp_path / "entries.db") if registry == "unavailable" else PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    request = request_for(entries)
    request.scope["headers"] = [(b"host", b"internal.example:8000")]
    if address == "partial_proxy":
        request.scope["headers"].extend([(b"x-forwarded-proto", b"https"), (b"x-forwarded-host", b"public.example")])
    app = FastAPI()
    app.state.public_entry_store = entries
    app.add_middleware(PublicEntryContextMiddleware)

    @app.get("/probe")
    async def probe() -> dict[str, bool]:
        assert not current_request_public_access_context().prefix_known
        with pytest.raises(PublicEntryRequiredError):
            await team_public_app_url(request, team_id="team_a", path="/invite/")
        with pytest.raises(PublicEntryRequiredError):
            public_app_url("/api/v1/auth/oauth/feishu/callback")
        with pytest.raises(PublicEntryRequiredError):
            public_app_base_url()
        return {"ok": True}

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://internal.example:8000") as client:
        response = await client.get("/probe", headers={key.decode(): value.decode() for key, value in request.scope["headers"]})
    assert response.json() == {"ok": True}


@pytest.mark.parametrize("registry", ["empty", "unavailable"])
@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
@pytest.mark.parametrize("evidence", ["browser", "proxy", "cookie"])
@pytest.mark.integration
async def test_complete_address_can_generate_links_without_registry_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: str, prefix: str, evidence: str
) -> None:
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "s" * 32)
    entries = BrokenEntries(tmp_path / "entries.db") if registry == "unavailable" else PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    base = f"http://public.example:8000{prefix}"
    headers = {"Host": "internal.example:8000"}
    if evidence == "browser":
        headers["X-Bowhead-Public-Base"] = base
    elif evidence == "proxy":
        headers.update({"X-Forwarded-Proto": "http", "X-Forwarded-Host": "public.example:8000", "X-Forwarded-Prefix": prefix or "/"})
    else:
        context = PublicAccessContext(base, "http://public.example:8000", prefix, f"{base}/", "browser_hint")
        headers["Cookie"] = _cookie_header(context).split(";", 1)[0]
    app = FastAPI()
    app.state.public_entry_store = entries
    app.add_middleware(PublicEntryContextMiddleware)

    @app.get("/probe")
    async def probe(request: Request) -> dict[str, str]:
        assert current_request_public_access_context().prefix_known
        assert public_app_url("/login/") == f"{base}/login/"
        assert public_app_base_url() == base
        return {"link": await team_public_app_url(request, team_id="team_a", path="/invite/")}

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://internal.example:8000") as client:
        response = await client.get("/probe", headers=headers)
    assert response.json() == {"link": f"{base}/invite/"}


@pytest.mark.integration
async def test_explicit_proxy_prefix_keeps_priority_over_conflicting_browser_hint(tmp_path: Path) -> None:
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    selected = await active(entries, "team_a", "https://browser.example/registered")
    request = request_for(entries, "https://browser.example/other")
    request.scope["headers"].extend(
        [(b"x-forwarded-proto", b"https"), (b"x-forwarded-host", b"browser.example"), (b"x-forwarded-prefix", b"/registered")]
    )
    context = await resolve_public_access_context(request.scope, entries)
    assert context.entry_id == selected.id
    assert context.request_base_url == selected.base_url


@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
@pytest.mark.integration
async def test_host_matched_direct_request_preserves_signed_browser_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "s" * 32)
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    selected = await active(entries, "team_a", "https://browser.example/registered")
    saved = PublicAccessContext(
        f"https://browser.example{prefix}", "https://browser.example", prefix, f"https://browser.example{prefix}/", "browser_hint"
    )
    scope = {
        "type": "http",
        "scheme": "http",
        "path": "/login/",
        "headers": [(b"host", b"browser.example:8000"), (b"cookie", _cookie_header(saved).split(";", 1)[0].encode())],
    }
    context = await resolve_public_access_context(scope, entries)
    assert context.entry_id == selected.id
    assert context.to_browser_dict()["baseUrl"] == saved.base_url
    scope["path"] = "/registered/login/"
    identified = await resolve_public_access_context(scope, entries)
    assert identified.base_url == selected.base_url
    assert identified.to_browser_dict()["baseUrl"] == selected.base_url


@pytest.mark.integration
async def test_unknown_address_keeps_diagnostics_readable_and_link_actions_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from bowhead_gateway.public_entry_context import _unbound_request_context

    entries = BrokenEntries(tmp_path / "entries.db")
    request = request_for(entries)
    request.scope["headers"] = [(b"host", b"internal.example:8000")]
    token = set_public_access_context(_unbound_request_context(request.scope))
    monkeypatch.setattr(feishu, "_diagnostics_payload", lambda _: {"ok": True})
    user = SimpleNamespace(team_id="team_a")
    source = SimpleNamespace(kind="mcp", auth_type="none")
    store = SimpleNamespace(get_team_tool_source=AsyncMock(return_value=source))
    try:
        diagnostics = await feishu.admin_diagnostics(request, user)
        assert diagnostics == {"ok": True, "event_subscription_url": "", "card_callback_url": ""}
        with pytest.raises(HTTPException) as setup_error:
            await feishu._setup_session_url(request, SimpleNamespace(id="setup_a", team_id="team_a"))
        with pytest.raises(HTTPException) as authorization_error:
            await team_tool_sources.oauth_authorize("source_a", request, user, store, None)
        for error in [setup_error.value, authorization_error.value]:
            assert error.status_code == 409
            assert error.detail["code"] == "public_entry_required"
            assert "application URL" in error.detail["message"]
            assert "internal.example" not in str(error.detail)
    finally:
        reset_public_access_context(token)


@pytest.mark.parametrize("prefix", ["", "/bowhead", "/ai/apps/bowhead"])
async def test_partial_proxy_uses_consumed_callback_prefix_without_binding(tmp_path: Path, prefix: str) -> None:
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    scope = {
        "type": "http",
        "scheme": "http",
        "path": "/callback",
        "headers": [(b"x-forwarded-proto", b"https"), (b"x-forwarded-host", b"public.example:10010")],
    }
    context = await resolve_public_access_context(scope, entries)
    token = set_public_access_context(context)
    try:
        callback = "/api/v1/auth/oauth/feishu/callback"
        restore_oauth_public_access_context(f"https://public.example:10010{prefix}{callback}", callback_path=callback)
        assert current_request_public_access_context().entry_id is None
        assert _refresh_cookie_path() == f"{prefix}/api/v1/auth"
        target = oauth_return_path("/login/", callback_path=callback)
        assert urljoin(f"https://public.example:10010{prefix}{callback}", target) == f"https://public.example:10010{prefix}/login/"
    finally:
        reset_public_access_context(token)


async def test_signed_unregistered_address_survives_navigation_without_team_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "s" * 32)
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    request = request_for(entries)
    context = await resolve_public_access_context(request.scope, entries)
    cookie = _cookie_header(context).split(";", 1)[0]
    scope = {**request.scope, "headers": [(b"host", b"internal.example:8000"), (b"cookie", cookie.encode())]}
    restored = await resolve_public_access_context(scope, entries)
    assert restored.base_url == context.base_url and restored.entry_id is None and (restored.team_id is None)


@pytest.mark.parametrize("kind", ["verify", "password_reset", "workspace_invite"])
async def test_mail_registry_outage_uses_request_snapshot(tmp_path: Path, kind: str) -> None:
    entries = BrokenEntries(tmp_path / "broken.db")
    request = request_for(entries)
    token = set_public_access_context(
        PublicAccessContext(
            "https://browser.example:10010/custom",
            "https://browser.example:10010",
            "/custom",
            "https://browser.example:10010/custom/",
            "browser_hint",
        )
    )
    try:
        mail = await render_identity_email(request, team_id="team_a", kind=kind, action_path="/action?token=opaque", locale="en-US")
        assert mail.action_url == "https://browser.example:10010/custom/action?token=opaque"
        assert mail.snapshot.public_entry_id is None
        with pytest.raises(PublicEntryRequiredError):
            await render_identity_email(request, team_id="team_a", kind=kind, action_path="/action", locale="en-US", public_entry_id="pe_fixed")
    finally:
        reset_public_access_context(token)


async def test_invitation_missing_fixed_entry_is_checked_before_token_rotation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "s" * 32)
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()

    class Invites:
        rotated = False

        async def rate_limit_check(self, *args, **kwargs):
            return True

        async def list_invites_for_workspace(self, *args, **kwargs):
            return [
                {
                    "id": "invite_a",
                    "status": "active",
                    "public_entry_id": "pe_missing",
                    "token_version": 1,
                    "role": "member",
                    "invited_email": "member@example.com",
                }
            ]

        async def reissue_invite_token(self, *args, **kwargs):
            self.rotated = True
            return True

    invites = Invites()
    with pytest.raises(HTTPException) as error:
        await members.resend_invite("ws_a", "invite_a", request_for(entries), user=SimpleNamespace(team_id="team_a"), store=invites)
    assert error.value.status_code == 409 and (not invites.rotated)


async def test_invitation_response_and_email_use_identical_fixed_entry(tmp_path: Path) -> None:
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    pinned = await active(entries, "team_a", "https://pinned.example/bowhead")
    current = await active(entries, "team_a", "https://current.example/custom")
    request = request_for(entries)
    token = set_public_access_context(public_access_context_for_entry(current))
    try:
        url = await team_public_app_url(request, team_id="team_a", entry_id=pinned.id, path="/invite?token=opaque")
        mail = await render_identity_email(
            request, team_id="team_a", kind="workspace_invite", action_path="/invite?token=opaque", public_entry_id=pinned.id, locale="en-US"
        )
        assert mail.action_url == url == "https://pinned.example/bowhead/invite?token=opaque"
    finally:
        reset_public_access_context(token)


async def test_feishu_setup_selects_its_team_even_when_transport_belongs_to_another(tmp_path: Path) -> None:
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    await active(entries, "team_a", "https://team-a.example/bowhead")
    other = await active(entries, "team_b", "https://team-b.example/other")
    token = set_public_access_context(public_access_context_for_entry(other))
    try:
        url = await feishu._setup_session_url(request_for(entries), SimpleNamespace(id="setup_a", team_id="team_a"))
        assert url == "https://team-a.example/bowhead/team/settings/integrations/feishu/?stage=02&setup=setup_a"
        from bowhead_gateway.routes import team_tool_sources

        assert (
            await team_tool_sources._oauth_redirect_uri(request_for(entries), "team_a")
            == "https://team-a.example/bowhead/api/v1/team-tool-sources/oauth/callback"
        )
    finally:
        reset_public_access_context(token)


async def test_slack_denied_callback_and_configuration_survive_registry_outage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connection = SimpleNamespace(
        id="slc_a",
        team_id="team_a",
        client_secret_ref="configured",
        client_id="client",
        slack_team_id="",
        slack_app_id="",
        bot_user_id="",
        status="draft",
        channel_answers_enabled=False,
        signing_secret_ref=None,
        bot_token_ref=None,
        updated_at="2026-10-14",
    )

    class Store:
        async def consume_oauth_state(self, state):
            return SlackOAuthState("slc_a", "https://public.example/bowhead/api/v1/integrations/slack/oauth/callback")

        async def by_id(self, identity):
            return connection

    monkeypatch.setattr(slack, "_store", lambda request: Store())
    app = FastAPI()
    app.state.public_entry_store = BrokenEntries(tmp_path / "broken.db")
    app.add_middleware(PublicEntryContextMiddleware)

    @app.get("/callback")
    async def callback(request: Request):
        return await slack.oauth_callback(request, state="valid", error="access_denied")

    @app.get("/config")
    async def config(request: Request):
        return {"slack": await slack._config_payload(request, connection), "google": await google_workspace._redirect_uri_or_none(request, "team_a")}

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://internal.example:8000") as client:
        response = await client.get("/callback")
        assert response.status_code == 303 and response.headers["location"] == "/bowhead/team/settings/integrations/slack/?install=failed"
        response = await client.get("/config", headers={"X-Bowhead-Public-Base": "https://public.example/bowhead"})
        assert response.status_code == 200 and response.json()["google"].startswith("https://public.example/bowhead/")


async def test_scheduled_registry_failure_delivers_summary_and_never_replaces_fixed_entry(tmp_path: Path) -> None:

    class Notifications:
        calls = 0

        async def emit_routine_delivered(self, *args, **kwargs):
            self.calls += 1

    notifications = Notifications()
    app = SimpleNamespace(state=SimpleNamespace(public_entry_store=BrokenEntries(tmp_path / "broken.db"), notification_service=notifications))
    job = SimpleNamespace(public_entry_id=None, notify_channels={"deliver_on_success": True}, id="job_a", name="Job")
    await _deliver_run_result(app, job, summary="Completed", session_id="s_a", run_id="run_a", team_id="team_a", locale="en-US")
    assert notifications.calls == 1
    entries = PublicEntryStore(tmp_path / "active.db")
    await entries.initialize()
    await active(entries, "team_a", "https://default.example/bowhead")
    app.state.public_entry_store = entries
    assert await _deep_link(app, SimpleNamespace(public_entry_id="pe_missing"), "s_a", "team_a") is None


async def test_local_uploads_use_current_origin(tmp_path: Path) -> None:
    backend = LocalBackend(
        tmp_path / "files", gateway_base_url="http://internal.example:8000", allow_local_receiver=True, local_presign_secret="s" * 32
    )
    pending = await backend.presign_post(aid="art_" + "a" * 12, filename="probe.txt", mime="text/plain", size_bytes=100)
    assert pending.url == "/api/v1/files/local-receiver/art_aaaaaaaaaaaa"


async def test_known_http_browser_cookie_does_not_inherit_https_team_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_gateway.routes.auth import _cookie_secure

    monkeypatch.setenv("BOWHEAD_PROD", "1")
    monkeypatch.setenv("BOWHEAD_COOKIE_SAMESITE", "lax")
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    await active(entries, "team_a", "https://default.example/bowhead")
    request = request_for(entries, "http://browser.example:10010/custom")
    token = set_public_access_context(await resolve_public_access_context(request.scope, entries))
    try:
        await bind_team_public_entry(request, team_id="team_a")
        assert not _cookie_secure()
        assert _refresh_cookie_path() == "/custom/api/v1/auth"
        require_same_origin_json(request)
    finally:
        reset_public_access_context(token)


async def test_workspace_resend_materializes_email_before_rotation_and_uses_fixed_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    import bowhead_core.email_config

    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "s" * 32)
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    pinned = await active(entries, "team_a", "https://pinned.example/bowhead")
    current = await active(entries, "team_a", "https://current.example/custom")
    events = []

    class Invites:
        async def rate_limit_check(self, *args, **kwargs):
            return True

        async def list_invites_for_workspace(self, *args, **kwargs):
            return [
                {
                    "id": "invite_a",
                    "status": "active",
                    "public_entry_id": pinned.id,
                    "token_version": 1,
                    "role": "member",
                    "invited_email": "member@example.com",
                }
            ]

        async def reissue_invite_token(self, *args, **kwargs):
            events.append("rotated")
            return True

        async def enqueue_smtp(self, **kwargs):
            events.append(kwargs)

        async def write_admin_audit_log(self, **kwargs):
            pass

    async def smtp(*args):
        return object()

    monkeypatch.setattr(bowhead_core.email_config, "resolve_smtp_config", smtp)
    token = set_public_access_context(public_access_context_for_entry(current))
    try:
        result = await members.resend_invite(
            "ws_a", "invite_a", request_for(entries), user=SimpleNamespace(team_id="team_a", id="usr_a", display_name="Owner"), store=Invites()
        )
        assert result["invite_url"] in events[1]["body"]
        assert json.loads(events[1]["branding_snapshot"])["public_entry_id"] == pinned.id
        assert events[0] == "rotated"
    finally:
        reset_public_access_context(token)


async def test_cross_team_link_matches_original_browser_after_default_binding(tmp_path: Path) -> None:
    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    await active(entries, "team_a", "https://default-a.example/bowhead")
    await active(entries, "team_b", "https://default-b.example/bowhead")
    await active(entries, "team_b", "https://browser.example:10010/team-b")
    request = request_for(entries)
    token = set_public_access_context(await resolve_public_access_context(request.scope, entries))
    try:
        await bind_team_public_entry(request, team_id="team_a")
        assert await team_public_app_url(request, team_id="team_b", path="/") == "https://browser.example:10010/team-b/"
    finally:
        reset_public_access_context(token)


async def test_legacy_slack_return_without_address_or_registry_never_exposes_upstream(tmp_path: Path) -> None:
    request = request_for(BrokenEntries(tmp_path / "broken.db"))
    token = set_public_access_context(
        PublicAccessContext("http://internal.example:8000", "http://internal.example:8000", "", "http://internal.example:8000/", "direct")
    )
    try:
        response = await slack._install_result_redirect(request, team_id="team_a", result="failed")
        location = response.headers["location"]
        assert ":8000" not in location and not location.startswith("/")
        assert (
            urljoin("https://public.example/bowhead/api/v1/integrations/slack/oauth/callback", location)
            == "https://public.example/bowhead/team/settings/integrations/slack/?install=failed"
        )
    finally:
        reset_public_access_context(token)


async def test_default_binding_keeps_original_direct_confidence_and_browser_marker(tmp_path: Path) -> None:
    from bowhead_gateway.routes.public_entries import list_public_entries

    entries = PublicEntryStore(tmp_path / "entries.db")
    await entries.initialize()
    await active(entries, "team_a", "https://default.example/bowhead")
    request = request_for(entries)
    token = set_public_access_context(
        PublicAccessContext("http://internal.example:8000", "http://internal.example:8000", "", "http://internal.example:8000/", "direct")
    )
    try:
        await bind_team_public_entry(request, team_id="team_a")
        assert current_request_public_access_context().match_source == "direct"
        restore_oauth_public_access_context(
            "https://browser.example/custom/api/v1/auth/oauth/feishu/callback", callback_path="/api/v1/auth/oauth/feishu/callback"
        )
        assert _refresh_cookie_path() == "/custom/api/v1/auth"
    finally:
        reset_public_access_context(token)
    token = set_public_access_context(await resolve_public_access_context(request.scope, entries))
    try:
        await bind_team_public_entry(request, team_id="team_a")
        browser = current_public_access_context().to_browser_dict()
        assert browser["baseUrl"] == "https://browser.example:10010/custom" and browser["entryId"] is None
        rows = await list_public_entries(request, user=SimpleNamespace(team_id="team_a"))
        assert not any(row.is_current for row in rows)
    finally:
        reset_public_access_context(token)
