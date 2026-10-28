from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx2
import pytest
import respx
from bowhead_core.llm_config_crypto import encrypt_llm_config
from bowhead_core.public_entries import PublicEntry, PublicEntryStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.public_entry_context import public_access_context_for_entry, reset_public_access_context, set_public_access_context
from bowhead_gateway.routes import personal_mcp, slack
from bowhead_gateway.slack.store import SlackStore
from cryptography.fernet import Fernet
from starlette.requests import Request


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initialize_migrated_core_database: Callable[[Path], None]) -> SimpleNamespace:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FEATURE_PERSONAL_SLACK_MCP", "1")
    path = tmp_path / "users.db"
    initialize_migrated_core_database(path)
    users = UserStore(path)
    await users.initialize()
    team = await users.create_team(name="Team")
    user = await users.create_user(team_id=team.id, email="owner@example.com")
    entries = PublicEntryStore(users.db_path)
    await entries.initialize()

    async def active(base: str) -> PublicEntry:
        pending, token = await entries.create_pending(team_id=team.id, base_url=base, actor_id=user.id)
        return await entries.verify_challenge(entry_id=pending.id, token=token)

    default = await active("https://default.example/bowhead")
    current = await active("https://current.example:10010/ai/apps/bowhead")
    store = SlackStore(users.db_path)
    connection = await store.upsert(
        team_id=team.id,
        actor_id=user.id,
        values={
            "client_id": "client",
            "client_secret_ref": encrypt_llm_config({"value": "secret"}),
            "signing_secret_ref": "signing",
            "status": "enabled",
        },
    )
    app = SimpleNamespace(state=SimpleNamespace(user_store=users, slack_store=store, public_entry_store=entries, deployment_mode="local"))
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})
    return SimpleNamespace(store=store, connection=connection, user=user, request=request, default=default, current=current)


@pytest.mark.asyncio
@pytest.mark.parametrize("personal", [False, True])
async def test_slack_callback_reuses_exact_authorization_uri(env: SimpleNamespace, personal: bool) -> None:
    token = set_public_access_context(public_access_context_for_entry(env.current))
    try:
        result = await personal_mcp.connect_slack(env.request, env.user) if personal else await slack.begin_install(env.request, env.user)
        query = parse_qs(urlsplit(result["authorize_url" if personal else "install_url"]).query)
    finally:
        reset_public_access_context(token)
    uri = query["redirect_uri"][0]
    assert uri.startswith(env.current.base_url)
    token = set_public_access_context(public_access_context_for_entry(env.default))
    try:
        endpoint = "https://slack.com/api/oauth.v2.user.access" if personal else "https://slack.com/api/oauth.v2.access"
        with respx.mock:
            exchange = respx.post(endpoint).mock(return_value=httpx2.Response(200, json={"ok": False}))
            callback = personal_mcp.slack_oauth_callback if personal else slack.oauth_callback
            response = await callback(env.request, code="code", state=query["state"][0])
            assert response.status_code == 303
            assert parse_qs(exchange.calls[0].request.content.decode())["redirect_uri"] == [uri]
            await callback(env.request, code="code", state=query["state"][0])
            assert exchange.call_count == 1
    finally:
        reset_public_access_context(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("personal", [False, True])
async def test_slack_state_is_atomic_single_use_and_reads_legacy_rows(env: SimpleNamespace, personal: bool) -> None:
    create = env.store.create_personal_oauth_state if personal else env.store.create_oauth_state
    args = (env.connection.id, env.user.id, env.user.team_id) if personal else (env.connection.id,)
    consume = env.store.consume_personal_oauth_state if personal else env.store.consume_oauth_state
    state = await create(*args, redirect_uri=f"{env.current.base_url}/callback")
    results = []

    async def take() -> None:
        results.append(await consume(state))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(take)
        tasks.start_soon(take)
    accepted = [result for result in results if result is not None]
    assert len(accepted) == 1
    assert accepted[0].redirect_uri == f"{env.current.base_url}/callback"
    legacy = await create(*args)
    assert (await consume(legacy)).redirect_uri is None
    assert await consume(legacy) is None
    assert await consume("unknown") is None
