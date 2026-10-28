from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
import respx
from bowhead_core.public_entries import PublicEntryStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.public_entry_context import (
    PublicAccessContext,
    PublicEntryContextMiddleware,
    reset_public_access_context,
    set_public_access_context,
)
from bowhead_gateway.routes import oauth
from bowhead_gateway.routes.oauth import (
    _consume_state,
    _oauth_error_redirect,
    _store_state,
)
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_oauth_state_persists_public_entry_and_is_single_use(tmp_path) -> None:
    store = UserStore(str(tmp_path / "oauth.db"))
    await store.initialize()

    await _store_state(
        store.db_path,
        "state_a",
        "feishu",
        "/invite?token=opaque",
        "pe_example",
        flow_kind="invite",
        invite_intent_hash="intent_hash",
    )

    assert await _consume_state(store.db_path, "state_a", "feishu") == (
        True,
        "/invite?token=opaque",
        "pe_example",
        "invite",
        "intent_hash",
    )
    assert await _consume_state(store.db_path, "state_a", "feishu") == (
        False,
        None,
        None,
        "normal",
        None,
    )


def test_oauth_error_redirect_preserves_verified_prefix_and_invite_resume() -> None:
    context = PublicAccessContext(
        base_url="https://public.example.com/bowhead",
        origin="https://public.example.com",
        base_path="/bowhead",
        app_root="https://public.example.com/bowhead/",
        match_source="entry_id",
        entry_id="pe_example",
        team_id="team_example",
    )
    token = set_public_access_context(context)
    try:
        response = _oauth_error_redirect(
            provider="feishu",
            code="invite_activation_failed",
            flow_kind="invite",
        )
    finally:
        reset_public_access_context(token)

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/bowhead/login/?oauth_error=invite_activation_failed&oauth_provider=feishu&flow=invite&next=%2Finvite%2F%3Fresume%3D1"
    )


@pytest.mark.parametrize(
    "request_host,expected_base",
    [
        ("internal.example:8000", "https://default.example/bowhead"),
        ("current.example:8000", "https://current.example:10010/ai/apps/bowhead"),
    ],
)
@pytest.mark.asyncio
async def test_normal_oauth_uses_team_entry_and_pins_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_host: str,
    expected_base: str,
) -> None:
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_ID", "test-client")
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_SECRET", "test-secret")
    store = UserStore(str(tmp_path / "oauth.db"))
    await store.initialize()
    team = await store.create_team(name="Local team")
    entries = PublicEntryStore(store.db_path)
    await entries.initialize()
    for base in ("https://default.example/bowhead", "https://current.example:10010/ai/apps/bowhead"):
        pending, token = await entries.create_pending(team_id=team.id, base_url=base, actor_id="usr_owner")
        await entries.verify_challenge(entry_id=pending.id, token=token)
    app = FastAPI()
    app.state.user_store = store
    app.state.public_entry_store = entries
    app.state.deployment_mode = "local"
    app.add_middleware(PublicEntryContextMiddleware)
    app.include_router(oauth.router, prefix="/api/v1/auth/oauth")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=f"http://{request_host}") as client:
        response = await client.get("/api/v1/auth/oauth/feishu/login")
        assert response.status_code == 200
        query = parse_qs(urlsplit(response.json()["redirect_url"]).query)
        expected_callback = f"{expected_base}/api/v1/auth/oauth/feishu/callback"
        assert query["redirect_uri"] == [expected_callback]
        state = query["state"][0]
        with respx.mock:
            exchange = respx.post(oauth._PROVIDERS["feishu"]["token_url"]).mock(return_value=httpx2.Response(400))
            callback = await client.get(
                "/api/v1/auth/oauth/feishu/callback",
                params={"state": state, "code": "test-code"},
                # Callback transport can differ from the address used at login.
                headers={"Host": "default.example:8000"},
            )
        assert callback.status_code == 303
        assert parse_qs(exchange.calls[0].request.content.decode())["redirect_uri"] == [expected_callback]
        assert "8000" not in callback.headers["location"]
        assert (await _consume_state(store.db_path, state, "feishu"))[0] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_binding_failure", [False, True])
async def test_normal_oauth_without_registered_entries_remains_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_binding_failure: bool,
) -> None:
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_ID", "test-client")
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_SECRET", "test-secret")
    store = UserStore(str(tmp_path / "oauth.db"))
    await store.initialize()
    await store.create_team(name="Local team")
    entries = PublicEntryStore(store.db_path)
    await entries.initialize()
    if entry_binding_failure:

        async def unavailable(_team_id: str) -> None:
            raise OSError("entry registry temporarily unavailable")

        monkeypatch.setattr(entries, "default_for_team", unavailable)
    app = FastAPI()
    app.state.user_store = store
    app.state.public_entry_store = entries
    app.state.deployment_mode = "local"
    app.add_middleware(PublicEntryContextMiddleware)
    app.include_router(oauth.router, prefix="/api/v1/auth/oauth")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="https://unregistered.example") as client:
        response = await client.get("/api/v1/auth/oauth/feishu/login", headers={"X-Bowhead-Public-Base": "https://unregistered.example/bowhead"})
    assert response.status_code == 200
    assert parse_qs(urlsplit(response.json()["redirect_url"]).query)["redirect_uri"] == [
        "https://unregistered.example/bowhead/api/v1/auth/oauth/feishu/callback"
    ]
    assert await entries.list_all() == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_oauth_unknown_address_returns_recovery_action_before_creating_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_ID", "test-client")
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_SECRET", "test-secret")
    store = UserStore(str(tmp_path / "oauth.db"))
    await store.initialize()
    entries = PublicEntryStore(store.db_path)
    await entries.initialize()
    write_state = AsyncMock()
    monkeypatch.setattr(oauth, "_store_state", write_state)
    app = FastAPI()
    app.state.user_store = store
    app.state.public_entry_store = entries
    app.state.deployment_mode = "local"
    app.add_middleware(PublicEntryContextMiddleware)
    app.include_router(oauth.router, prefix="/api/v1/auth/oauth")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://internal.example:8000") as client:
        response = await client.get("/api/v1/auth/oauth/feishu/login")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "public_entry_required"
        assert "application URL" in response.json()["detail"]["message"]
        assert "internal.example" not in response.text
        write_state.assert_not_awaited()
        recovered = await client.get("/api/v1/auth/oauth/feishu/login", headers={"X-Bowhead-Public-Base": "https://public.example/ai/apps"})
    assert recovered.status_code == 200
    assert parse_qs(urlsplit(recovered.json()["redirect_url"]).query)["redirect_uri"] == [
        "https://public.example/ai/apps/api/v1/auth/oauth/feishu/callback"
    ]
    write_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_snapshot_is_atomic_and_independent_of_entry_lifecycle(tmp_path: Path) -> None:
    import anyio

    store = UserStore(tmp_path / "states.db")
    await store.initialize()
    uri = "https://public.example:10010/bowhead/api/v1/auth/oauth/feishu/callback"
    await _store_state(store.db_path, "single", "feishu", public_entry_id="pe_disabled", redirect_uri=uri)
    results = []

    async def take() -> None:
        results.append(await oauth._consume_state_snapshot(store.db_path, "single", "feishu"))

    async with anyio.create_task_group() as group:
        group.start_soon(take)
        group.start_soon(take)
    valid = [result for result in results if result[0]]
    assert len(valid) == 1 and valid[0][-1] == uri
    await _store_state(store.db_path, "legacy", "feishu")
    assert (await oauth._consume_state_snapshot(store.db_path, "legacy", "feishu"))[-1] is None
