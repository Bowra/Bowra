from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from bowhead_gateway.edition_extensions import GatewayExtensions
from bowhead_gateway.post_login_hooks import schedule_post_login_hooks
from fastapi import BackgroundTasks


class _ConnectionStore:
    def __init__(self, connection=None, *, auth_key: str | None = "secret") -> None:
        self.connection = connection
        self.auth_key = auth_key

    async def list_connections(self, team_id: str):
        assert team_id == "team_1"
        return [self.connection] if self.connection is not None else []

    async def get_connection(self, connection_id: str, team_id: str):
        assert (connection_id, team_id) == ("dc_1", "team_1")
        return self.connection

    async def get_connection_auth(self, connection_id: str, team_id: str):
        assert (connection_id, team_id) == ("dc_1", "team_1")
        return self.auth_key


class _UserStore:
    def __init__(self) -> None:
        self.user = SimpleNamespace(
            id="u_1",
            team_id="team_1",
            active_team_id=None,
            email="USER@example.com",
            display_name="Ada Lovelace",
            status="active",
        )

    async def get_user(self, user_id: str):
        assert user_id == "u_1"
        return self.user

    async def get_team_role(self, user_id: str, team_id: str):
        return "member" if (user_id, team_id) == ("u_1", "team_1") else None


class _Client:
    def __init__(self, *, existing=None, failure: Exception | None = None) -> None:
        self.existing = existing
        self.failure = failure
        self.created: list[dict] = []

    async def fetch_account(self, email: str):
        if self.failure is not None:
            raise self.failure
        assert email == "user@example.com"
        return self.existing

    async def create_account(self, **payload):
        self.created.append(payload)
        return {"uid": payload["uid"]}


class _MetadataAdapter:
    def __init__(self, userinfo=None) -> None:
        self.userinfo = userinfo if userinfo is not None else {"sub": "subject", "groups": ["analysts"]}

    async def post_login_metadata(self, app, team_id: str, user_id: str):
        assert (team_id, user_id) == ("team_1", "u_1")
        return self.userinfo


def _app(*, connection=None, client=None, identity=None):
    invalidated: list[str] = []
    state = SimpleNamespace(
        datarc_connection_store=_ConnectionStore(connection),
        datarc_client_factory=SimpleNamespace(
            get_client=lambda base_url, auth_key: client,
        ),
        user_store=_UserStore(),
        gateway_extensions=GatewayExtensions(identity=identity),
        tool_context_service=SimpleNamespace(
            invalidate_user=invalidated.append,
        ),
    )
    return SimpleNamespace(state=state), state.user_store.user, invalidated


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connection",
    [
        None,
        SimpleNamespace(
            id="dc_1",
            status="archived",
            base_url="https://datarc.example",
            key_hint="cret",
        ),
        SimpleNamespace(
            id="dc_1",
            status="active",
            base_url="https://datarc.example",
            key_hint="",
        ),
    ],
)
async def test_datarc_hook_is_not_scheduled_without_valid_active_connection(connection):
    app, user, _ = _app(connection=connection, client=_Client())
    background = BackgroundTasks()

    await schedule_post_login_hooks(
        background,
        SimpleNamespace(app=app),
        user=user,
        team_id="team_1",
        auth_method="password",
    )

    assert background.tasks == []


@pytest.mark.asyncio
async def test_datarc_hook_prepare_failure_does_not_change_login_result(caplog):
    app, user, _ = _app(client=_Client())

    async def fail_list_connections(team_id: str):
        raise RuntimeError(f"connection store unavailable for {team_id}")

    app.state.datarc_connection_store.list_connections = fail_list_connections
    background = BackgroundTasks()

    await schedule_post_login_hooks(
        background,
        SimpleNamespace(app=app),
        user=user,
        team_id="team_1",
        auth_method="password",
    )

    assert background.tasks == []
    assert "post_login_hook.prepare_failed" in caplog.text


@pytest.mark.asyncio
async def test_datarc_hook_creates_user_with_identity_metadata():
    connection = SimpleNamespace(
        id="dc_1",
        status="active",
        base_url="https://datarc.example",
        key_hint="cret",
    )
    client = _Client()
    app, user, invalidated = _app(
        connection=connection,
        client=client,
        identity=_MetadataAdapter(),
    )
    background = BackgroundTasks()

    await schedule_post_login_hooks(
        background,
        SimpleNamespace(app=app),
        user=user,
        team_id="team_1",
        auth_method="oidc",
    )
    assert len(background.tasks) == 1
    await background()

    assert len(client.created) == 1
    payload = client.created[0]
    assert UUID(payload["uid"]).version == 4
    assert payload == {
        "uid": payload["uid"],
        "email": "user@example.com",
        "nickname": "Ada Lovelace",
        "metadata": {"sub": "subject", "groups": ["analysts"]},
    }
    assert invalidated == ["u_1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("userinfo", "expected_nickname"),
    [
        ({"name": "Grace Hopper"}, "Grace Hopper"),
        ({"nickname": "Amazing Grace"}, "Amazing Grace"),
        ({"name": "Grace Hopper", "nickname": "Amazing Grace"}, "Amazing Grace"),
        ({"name": "  "}, "Ada Lovelace"),
        ({"name": "  ", "nickname": "Amazing Grace"}, "Amazing Grace"),
    ],
)
async def test_datarc_hook_prefers_identity_metadata_for_nickname(userinfo, expected_nickname):
    connection = SimpleNamespace(
        id="dc_1",
        status="active",
        base_url="https://datarc.example",
        key_hint="cret",
    )
    client = _Client()
    app, user, _ = _app(
        connection=connection,
        client=client,
        identity=_MetadataAdapter(userinfo),
    )
    background = BackgroundTasks()

    await schedule_post_login_hooks(
        background,
        SimpleNamespace(app=app),
        user=user,
        team_id="team_1",
        auth_method="oidc",
    )
    await background()

    assert client.created[0]["nickname"] == expected_nickname


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_rejected", [False, True], ids=["upstream-outage", "expired-key"])
async def test_datarc_hook_failure_is_logged_and_never_escapes(caplog, credential_rejected):
    from bowhead_bi_bridge import BIPermissionError

    connection = SimpleNamespace(
        id="dc_1",
        status="active",
        base_url="https://datarc.example",
        key_hint="cret",
    )
    app, user, _ = _app(
        connection=connection,
        client=_Client(
            failure=(BIPermissionError("access rejected", source="http") if credential_rejected else RuntimeError("upstream unavailable"))
        ),
    )
    background = BackgroundTasks()
    await schedule_post_login_hooks(
        background,
        SimpleNamespace(app=app),
        user=user,
        team_id="team_1",
        auth_method="password",
    )

    await background()

    assert "post_login_hook.failed" in caplog.text
    assert ("BIPermissionError" if credential_rejected else "RuntimeError") in caplog.text


@pytest.mark.asyncio
async def test_datarc_hook_does_not_create_an_existing_user():
    connection = SimpleNamespace(
        id="dc_1",
        status="active",
        base_url="https://datarc.example",
        key_hint="cret",
    )
    client = _Client(existing={"uid": "existing"})
    app, user, invalidated = _app(connection=connection, client=client)
    background = BackgroundTasks()
    await schedule_post_login_hooks(
        background,
        SimpleNamespace(app=app),
        user=user,
        team_id="team_1",
        auth_method="password",
    )

    await background()

    assert client.created == []
    assert invalidated == ["u_1"]
