from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import aiosqlite
import httpx2
import jwt
import pytest
from bowhead_core.entitlements import CommunityResolver
from bowhead_core.user_store import UserStore, _now, hash_password, verify_password
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "invite-activation-test-secret-at-least-32-bytes"
ORIGIN = "http://localhost:3000"
JSON_HEADERS = {"Origin": ORIGIN, "Content-Type": "application/json"}


@pytest.fixture
async def store(tmp_path) -> UserStore:
    value = UserStore(tmp_path / "invite_activation.db")
    await value.initialize()
    from bowhead_core.public_entries import PublicEntryStore

    await PublicEntryStore(value.db_path).initialize()
    return value


@pytest.fixture
def app(store: UserStore, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "1")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.delenv("BOWHEAD_ACCOUNT_CREATION_POLICY", raising=False)
    monkeypatch.delenv("BOWHEAD_EMAIL_VERIFICATION_POLICY", raising=False)
    monkeypatch.delenv("BOWHEAD_EMAIL_VERIFY_REQUIRED", raising=False)

    from bowhead_core.public_entries import PublicEntryStore
    from bowhead_gateway.public_entry_context import (
        PublicAccessContext,
        public_access_context_for_entry,
        reset_public_access_context,
        set_public_access_context,
    )
    from bowhead_gateway.routes import (
        admin_members,
        auth,
        credential_recovery,
        invite_activation,
        oauth,
    )

    auth._RATE_BUCKET.clear()
    value = FastAPI()
    value.state.user_store = store
    registry = PublicEntryStore(store.db_path)
    value.state.public_entry_store = registry
    value.state.deployment_mode = "local"

    @value.middleware("http")
    async def _public_entry_context(request, call_next):  # type: ignore[no-untyped-def]
        entries = await registry.find_active_by_base_url(ORIGIN)
        context = (
            public_access_context_for_entry(entries[0], source="direct")
            if len(entries) == 1
            else PublicAccessContext(
                base_url=ORIGIN,
                origin=ORIGIN,
                base_path="",
                app_root=f"{ORIGIN}/",
                match_source="direct",
            )
        )
        token = set_public_access_context(context)
        try:
            return await call_next(request)
        finally:
            reset_public_access_context(token)

    value.state.entitlements = CommunityResolver(mode="saas", user_store=store)
    store.set_seat_guard(value.state.entitlements)

    class _Settings:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    value.state.settings = _Settings()
    value.include_router(auth.router, prefix="/api/v1/auth")
    value.include_router(invite_activation.router, prefix="/api/v1/auth")
    value.include_router(credential_recovery.public_router, prefix="/api/v1/auth")
    value.include_router(credential_recovery.admin_router)
    value.include_router(admin_members.router)
    value.include_router(oauth.router, prefix="/api/v1/auth/oauth")
    return value


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def team_public_access_context() -> Iterator[None]:
    from bowhead_gateway.public_entry_context import (
        PublicAccessContext,
        reset_public_access_context,
        set_public_access_context,
    )

    context_token = set_public_access_context(
        PublicAccessContext(
            base_url=ORIGIN,
            origin=ORIGIN,
            base_path="",
            app_root=f"{ORIGIN}/",
            match_source="direct",
            team_id="team_activation",
        ),
    )
    try:
        yield
    finally:
        reset_public_access_context(context_token)


async def _seed_team(store: UserStore, team_id: str = "team_activation") -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id,name,max_seats,default_user_tier,"
            "team_credit_balance,team_credit_quota,tier_credits_json,status,created_at) "
            "VALUES (?, 'Activation Team', 10, 'pro', 0, 0, "
            "'{\"pro\":5000,\"ultra\":30000}', 'active', ?)",
            (team_id, _now()),
        )
        await db.commit()
    from bowhead_core.public_entries import PublicEntryStore

    registry = PublicEntryStore(store.db_path)
    await registry.create_bootstrap_active(
        team_id=team_id,
        base_url=ORIGIN,
        actor_id="test",
    )


async def _seed_user(
    store: UserStore,
    *,
    user_id: str,
    email: str,
    team_id: str = "team_activation",
    owner: bool = False,
) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO users(id,team_id,email,email_verified,password_hash,tier,"
            "credit_balance,credit_quota,status,created_at) "
            "VALUES (?,?,?,1,?,'pro',0,0,'active',?)",
            (user_id, team_id, email, hash_password("OldPassw0rd!"), _now()),
        )
        await db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,'member','active',?)",
            (f"om_{user_id}_{team_id}", user_id, team_id, _now()),
        )
        if owner:
            await db.execute(
                "UPDATE teams SET created_by_user_id=? WHERE id=?",
                (user_id, team_id),
            )
        await db.commit()


def _user_jwt(user_id: str, email: str, team_id: str = "team_activation") -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user_id,
            "email": email,
            "team_id": team_id,
            "pwd_v": 0,
            "iat": now,
            "exp": now + 3600,
        },
        SECRET,
        algorithm="HS256",
    )


async def _team_invite(
    store: UserStore,
    *,
    email: str,
    invite_id: str = "oinv_activation",
    token_version: int = 1,
    invited_by: str | None = None,
) -> str:
    now_ts = int(time.time())
    exp_ts = now_ts + 7 * 86400
    token = jwt.encode(
        {
            "typ": "bowhead-invite",
            "scope": "team",
            "invite_id": invite_id,
            "token_version": token_version,
            "team_id": "team_activation",
            "team_role": "member",
            "invited_email": email,
            "iat": now_ts,
            "exp": exp_ts,
        },
        SECRET,
        algorithm="HS256",
    )
    await store.record_team_invite(
        team_id="team_activation",
        team_role="member",
        invited_email=email,
        raw_token=token,
        expires_at=datetime.fromtimestamp(exp_ts, UTC).isoformat(),
        invite_id=invite_id,
        token_version=token_version,
        invited_by=invited_by,
    )
    return token


@pytest.mark.asyncio
async def test_local_invite_password_activation_uses_server_intent(
    client: TestClient,
    store: UserStore,
) -> None:
    await _seed_team(store)
    token = await _team_invite(store, email="new.user@example.com")

    exchange = client.post(
        "/api/v1/auth/invites/intent",
        json={"token": token},
        headers=JSON_HEADERS,
    )
    assert exchange.status_code == 200, exchange.text
    assert exchange.json()["status"] == "ready"
    assert exchange.json()["invited_email_mask"] == "n***@example.com"
    assert token not in exchange.text
    assert client.cookies.get("bowhead_invite_intent")

    next_action = client.post(
        "/api/v1/auth/invites/intent/continue",
        json={},
        headers=JSON_HEADERS,
    )
    assert next_action.status_code == 200, next_action.text
    assert next_action.json()["next_action"] == "activate_password"

    activated = client.post(
        "/api/v1/auth/invites/activate",
        json={"display_name": "New User", "password": "Passw0rd!"},
        headers=JSON_HEADERS,
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["result"] == "activated"
    assert activated.json()["invite_result"]["invite_type"] == "team"
    assert client.cookies.get("bowhead_jwt")
    assert client.cookies.get("bowhead_invite_intent") is None

    user = await store.get_user_by_email("new.user@example.com")
    assert user is not None
    assert user.display_name == "New User"
    assert user.password_hash
    assert await store.user_has_active_team_membership(user.id, "team_activation")
    invite = await store.get_team_invite_by_id("oinv_activation")
    assert invite and invite["status"] == "consumed"
    assert invite["consumed_by_user_id"] == user.id


@pytest.mark.asyncio
async def test_intent_preview_names_the_inviter_by_display_name_only(
    client: TestClient,
    store: UserStore,
) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="u_owner", email="owner@example.com", owner=True)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET display_name = ? WHERE id = ?", ("Li Hua", "u_owner"))
        await db.commit()
    token = await _team_invite(store, email="new.user@example.com", invited_by="u_owner")

    exchange = client.post(
        "/api/v1/auth/invites/intent",
        json={"token": token},
        headers=JSON_HEADERS,
    )
    assert exchange.status_code == 200, exchange.text
    assert exchange.json()["inviter_display_name"] == "Li Hua"
    assert "owner@example.com" not in exchange.text

    resumed = client.post(
        "/api/v1/auth/invites/intent/continue",
        json={},
        headers=JSON_HEADERS,
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["inviter_display_name"] == "Li Hua"
    assert "owner@example.com" not in resumed.text


@pytest.mark.asyncio
async def test_activation_body_cannot_override_invite_authority(
    client: TestClient,
    store: UserStore,
) -> None:
    await _seed_team(store)
    token = await _team_invite(store, email="authority@example.com")
    assert (
        client.post(
            "/api/v1/auth/invites/intent",
            json={"token": token},
            headers=JSON_HEADERS,
        ).status_code
        == 200
    )
    response = client.post(
        "/api/v1/auth/invites/activate",
        json={
            "display_name": "Authority",
            "password": "Passw0rd!",
            "email": "attacker@example.com",
            "team_id": "team_other",
        },
        headers=JSON_HEADERS,
    )
    assert response.status_code == 422
    assert await store.get_user_by_email("authority@example.com") is None
    assert await store.get_user_by_email("attacker@example.com") is None


@pytest.mark.asyncio
async def test_intent_exchange_rejects_cross_origin(
    client: TestClient,
    store: UserStore,
) -> None:
    await _seed_team(store)
    token = await _team_invite(store, email="origin@example.com")
    response = client.post(
        "/api/v1/auth/invites/intent",
        json={"token": token},
        headers={"Origin": "https://attacker.example", "Content-Type": "application/json"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "cross_origin_mutation_rejected"


@pytest.mark.asyncio
async def test_rotate_invalidates_previous_version(
    client: TestClient,
    store: UserStore,
) -> None:
    await _seed_team(store)
    old_token = await _team_invite(store, email="rotate@example.com")
    invite = await store.get_team_invite_by_id("oinv_activation")
    assert invite is not None
    current_version = int(invite["token_version"])
    new_version = current_version + 1
    now_ts = int(time.time())
    exp_ts = now_ts + 7 * 86400
    new_token = jwt.encode(
        {
            "typ": "bowhead-invite",
            "scope": "team",
            "invite_id": "oinv_activation",
            "token_version": new_version,
            "team_id": "team_activation",
            "team_role": "member",
            "invited_email": "rotate@example.com",
            "iat": now_ts,
            "exp": exp_ts,
        },
        SECRET,
        algorithm="HS256",
    )
    assert await store.reissue_team_invite_token(
        "oinv_activation",
        "team_activation",
        new_token,
        datetime.fromtimestamp(exp_ts, UTC).isoformat(),
        expected_token_version=current_version,
    )

    old_exchange = client.post(
        "/api/v1/auth/invites/intent",
        json={"token": old_token},
        headers=JSON_HEADERS,
    )
    assert old_exchange.status_code == 401
    new_exchange = client.post(
        "/api/v1/auth/invites/intent",
        json={"token": new_token},
        headers=JSON_HEADERS,
    )
    assert new_exchange.status_code == 200, new_exchange.text


def test_capabilities_expose_derived_local_policy(client: TestClient) -> None:
    response = client.get("/api/v1/auth/capabilities")
    assert response.status_code == 200
    assert response.json() == {
        "deployment_mode": "local",
        "account_creation_policy": "invite_only",
        "email_verification_policy": "not_required",
        "auth_methods": ["password"],
        "public_registration": False,
        "bootstrap_required": False,
        "invite_activation": True,
        "team_openapi": True,
    }


@pytest.mark.asyncio
async def test_invite_entry_binding_ignores_another_team(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_gateway.public_entry_context import PublicAccessContext
    from bowhead_gateway.routes.invite_activation import bind_invite_public_entry

    from bowhead_gateway import public_entry_context

    unbound = PublicAccessContext(
        base_url=ORIGIN,
        origin=ORIGIN,
        base_path="",
        app_root=f"{ORIGIN}/",
        match_source="direct",
    )
    wrong_team = PublicAccessContext(
        base_url=ORIGIN,
        origin=ORIGIN,
        base_path="",
        app_root=f"{ORIGIN}/",
        match_source="direct",
        entry_id="pe_wrong",
        team_id="team_other",
    )

    async def _bind(_request, *, team_id: str) -> PublicAccessContext:
        assert team_id == "team_activation"
        return wrong_team

    monkeypatch.setattr(public_entry_context, "bind_team_public_entry", _bind)
    context_token = public_entry_context.set_public_access_context(unbound)
    try:
        await bind_invite_public_entry(
            object(),  # type: ignore[arg-type]
            "team_activation",
        )
    finally:
        public_entry_context.reset_public_access_context(context_token)


class _OAuthClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def post(self, *args, **kwargs) -> httpx2.Response:
        return httpx2.Response(200, json={"access_token": "oauth-access"})

    async def get(self, *args, **kwargs) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "sub": "feishu-subject-1",
                "email": "oauth.invite@example.com",
                "email_verified": True,
                "name": "OAuth Invite",
            },
        )


@pytest.mark.asyncio
async def test_matching_trusted_oauth_activates_same_invitation_transaction(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_team(store)
    token = await _team_invite(store, email="oauth.invite@example.com")
    assert (
        client.post(
            "/api/v1/auth/invites/intent",
            json={"token": token},
            headers=JSON_HEADERS,
        ).status_code
        == 200
    )
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_ID", "client")
    monkeypatch.setenv("BOWHEAD_OAUTH_FEISHU_APP_SECRET", "secret")
    from bowhead_gateway.routes import oauth

    monkeypatch.setattr(oauth.httpx2, "AsyncClient", _OAuthClient)
    login = client.get("/api/v1/auth/oauth/feishu/login?flow=invite")
    assert login.status_code == 200, login.text
    state = parse_qs(urlparse(login.json()["redirect_url"]).query)["state"][0]
    callback = client.get(
        f"/api/v1/auth/oauth/feishu/callback?code=ok&state={state}",
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    assert callback.headers["location"].startswith(
        "/onboarding/profile?source=invite",
    )
    user = await store.get_user_by_email("oauth.invite@example.com")
    assert user is not None
    assert user.password_hash is None
    assert user.email_verified
    assert (
        await store.get_user_by_federated_identity(
            "feishu",
            "feishu-subject-1",
        )
        == user
    )


@pytest.mark.asyncio
async def test_self_service_reset_uses_explicit_flow_with_invite_activation_enabled(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_team(store)
    await _seed_user(
        store,
        user_id="usr_self_service",
        email="self-service@example.com",
    )
    before = await store.get_user("usr_self_service")
    assert before is not None
    action_paths: list[str] = []

    async def _smtp_config(_team_id: str, _store: UserStore) -> object:
        return object()

    async def _render(_request, **kwargs) -> SimpleNamespace:  # type: ignore[no-untyped-def]
        action_paths.append(str(kwargs["action_path"]))
        return SimpleNamespace(subject="Reset", text_body="reset body", html_body="<p>reset</p>")

    async def _deliver(*_args, **_kwargs) -> tuple[bool, None]:  # type: ignore[no-untyped-def]
        return True, None

    monkeypatch.setattr(
        "bowhead_core.email_config.resolve_smtp_config",
        _smtp_config,
    )
    monkeypatch.setattr(
        "bowhead_gateway.identity_email.render_identity_email",
        _render,
    )
    monkeypatch.setattr(
        "bowhead_core.email_config.deliver_email",
        _deliver,
    )

    requested = client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "self-service@example.com"},
        headers=JSON_HEADERS,
    )
    assert requested.status_code == 200, requested.text
    assert len(action_paths) == 1
    query = parse_qs(urlparse(action_paths[0]).query)
    assert query["flow"] == ["self_service"]
    raw_token = query["token"][0]

    completed = client.post(
        "/api/v1/auth/reset-password",
        json={"token": raw_token, "new_password": "NewPassw0rd!"},
        headers=JSON_HEADERS,
    )
    assert completed.status_code == 200, completed.text
    user = await store.get_user("usr_self_service")
    assert user is not None
    assert verify_password("NewPassw0rd!", user.password_hash)
    assert user.pwd_version == before.pwd_version + 1
    assert user.session_token_version == before.session_token_version + 1
    assert completed.json()["token"]
    assert client.cookies.get("bowhead_jwt")
    assert client.cookies.get("bowhead_refresh")


@pytest.mark.asyncio
async def test_self_service_reset_delivers_with_configured_smtp_without_public_entry(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_team(store)
    await _seed_user(
        store,
        user_id="usr_reset_without_entry",
        email="reset-without-entry@example.com",
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("DELETE FROM public_entries WHERE team_id = ?", ("team_activation",))
        await db.commit()
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "smtp.example.com")
    deliveries: list[tuple[str, str]] = []

    async def _deliver(_to, _subject, body, **_kwargs) -> tuple[bool, None]:  # type: ignore[no-untyped-def]
        deliveries.append((_to, body))
        return True, None

    monkeypatch.setattr("bowhead_core.email_config.deliver_email", _deliver)

    response = client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "reset-without-entry@example.com"},
        headers=JSON_HEADERS,
    )

    assert response.status_code == 200, response.text
    assert len(deliveries) == 1
    assert deliveries[0][0] == "reset-without-entry@example.com"
    assert "http://localhost:3000/reset-password?flow=self_service&token=" in deliveries[0][1]


@pytest.mark.asyncio
async def test_self_service_reset_reports_unconfigured_email_service(
    client: TestClient,
    store: UserStore,
) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="usr_no_smtp", email="no-smtp@example.com")

    response = client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "no-smtp@example.com"},
        headers=JSON_HEADERS,
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == {"code": "email_not_configured"}


@pytest.mark.asyncio
async def test_self_service_reset_delivers_for_user_without_a_password(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="usr_without_password", email="without-password@example.com")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET password_hash = NULL WHERE id = ?", ("usr_without_password",))
        await db.commit()
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "smtp.example.com")
    delivered_to: list[str] = []

    async def _deliver(to_email, *_args, **_kwargs) -> tuple[bool, None]:  # type: ignore[no-untyped-def]
        delivered_to.append(to_email)
        return True, None

    monkeypatch.setattr("bowhead_core.email_config.deliver_email", _deliver)

    response = client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "without-password@example.com"},
        headers=JSON_HEADERS,
    )

    assert response.status_code == 200, response.text
    assert delivered_to == ["without-password@example.com"]


@pytest.mark.asyncio
async def test_self_service_reset_reports_unavailable_email_service(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_team(store)
    await _seed_user(store, user_id="usr_busy_smtp", email="busy-smtp@example.com")
    monkeypatch.setenv("BOWHEAD_SMTP_HOST", "smtp.example.com")

    async def _deliver(*_args, **_kwargs) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
        return False, "smtp temporarily unavailable"

    monkeypatch.setattr("bowhead_core.email_config.deliver_email", _deliver)
    response = client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "busy-smtp@example.com"},
        headers=JSON_HEADERS,
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == {"code": "email_delivery_unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("has_password", [True, False])
async def test_admin_recovery_is_one_shot_and_revokes_old_sessions(
    has_password: bool,
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
    team_public_access_context: None,
) -> None:
    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "0")
    await _seed_team(store)
    await _seed_user(
        store,
        user_id="usr_owner",
        email="owner@example.com",
        owner=True,
    )
    await _seed_user(
        store,
        user_id="usr_target",
        email="target@example.com",
    )
    if not has_password:
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute("UPDATE users SET password_hash=NULL WHERE id='usr_target'")
            await db.commit()
    # A pre-upgrade account may have one active link of each historical type.
    old_tokens = ["old-recovery-password_reset", "old-recovery-password_setup"]
    async with aiosqlite.connect(store.db_path) as db:
        for token, purpose in zip(old_tokens, ["password_reset", "password_setup"], strict=True):
            await db.execute(
                "INSERT INTO credential_recovery_tokens "
                "(token_hash,user_id,team_id,purpose,status,created_by_user_id,created_at,expires_at) "
                "VALUES (?,'usr_target','team_activation',?,'active','usr_owner',?,'2999-01-01')",
                (hashlib.sha256(token.encode()).hexdigest(), purpose, _now()),
            )
        await db.commit()
    client.cookies.set(
        "bowhead_jwt",
        _user_jwt("usr_owner", "owner@example.com"),
    )
    created = client.post(
        "/api/v1/admin/members/usr_target/credential-recovery",
        json={},
        headers=JSON_HEADERS,
    )
    assert created.status_code == 200, created.text
    for token in old_tokens:
        old_intent = client.post(
            "/api/v1/auth/credential-recovery/intent",
            json={"token": token},
            headers=JSON_HEADERS,
        )
        assert old_intent.status_code == 410
    issued_target = await store.get_user("usr_target")
    assert issued_target is not None
    assert issued_target.pwd_version == 1
    assert issued_target.session_token_version == 1
    recovery_url = created.json()["recovery_url"]
    recovery_query = parse_qs(urlparse(recovery_url).query)
    assert recovery_query["flow"] == ["admin"]
    raw_token = recovery_query["token"][0]

    intent = client.post(
        "/api/v1/auth/credential-recovery/intent",
        json={"token": raw_token},
        headers=JSON_HEADERS,
    )
    assert intent.status_code == 200, intent.text
    assert raw_token not in intent.text
    completed = client.post(
        "/api/v1/auth/credential-recovery/complete",
        json={"new_password": "NewPassw0rd!"},
        headers=JSON_HEADERS,
    )
    assert completed.status_code == 200, completed.text
    target = await store.get_user("usr_target")
    assert target is not None
    assert not verify_password("OldPassw0rd!", target.password_hash)
    assert verify_password("NewPassw0rd!", target.password_hash)
    assert target.pwd_version == 2

    replay = client.post(
        "/api/v1/auth/credential-recovery/intent",
        json={"token": raw_token},
        headers=JSON_HEADERS,
    )
    assert replay.status_code == 410


@pytest.mark.asyncio
async def test_recovery_intent_binds_token_team_public_entry(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
    team_public_access_context: None,
) -> None:
    await _seed_team(store)
    await _seed_user(
        store,
        user_id="usr_owner",
        email="owner@example.com",
        owner=True,
    )
    await _seed_user(
        store,
        user_id="usr_target",
        email="target@example.com",
    )
    client.cookies.set(
        "bowhead_jwt",
        _user_jwt("usr_owner", "owner@example.com"),
    )
    created = client.post(
        "/api/v1/admin/members/usr_target/credential-recovery",
        json={},
        headers=JSON_HEADERS,
    )
    recovery_query = parse_qs(urlparse(created.json()["recovery_url"]).query)
    assert recovery_query["flow"] == ["admin"]
    raw_token = recovery_query["token"][0]

    # An unexpired setup link issued before unification still works after upgrade.
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE credential_recovery_tokens SET purpose='password_setup' WHERE user_id='usr_target'")
        await db.commit()

    from bowhead_gateway.routes import credential_recovery

    async def _ignore_wrong_entry(_request, team_id: str) -> None:
        assert team_id == "team_activation"
        return

    monkeypatch.setattr(
        credential_recovery,
        "_bind_current_team_public_entry",
        _ignore_wrong_entry,
    )
    intent = client.post(
        "/api/v1/auth/credential-recovery/intent",
        json={"token": raw_token},
        headers=JSON_HEADERS,
    )
    assert intent.status_code == 200, intent.text
    assert client.cookies.get("bowhead_recovery_intent") is not None

    completed = client.post(
        "/api/v1/auth/credential-recovery/complete",
        json={"new_password": "NewPassw0rd!"},
        headers=JSON_HEADERS,
    )
    assert completed.status_code == 200, completed.text
    target = await store.get_user("usr_target")
    assert target is not None and verify_password("NewPassw0rd!", target.password_hash)


@pytest.mark.asyncio
async def test_team_admin_cannot_reset_shared_identity(
    client: TestClient,
    store: UserStore,
) -> None:
    await _seed_team(store)
    await _seed_team(store, team_id="team_second")
    await _seed_user(
        store,
        user_id="usr_owner",
        email="owner@example.com",
        owner=True,
    )
    await _seed_user(
        store,
        user_id="usr_shared",
        email="shared@example.com",
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) "
            "VALUES ('om_shared_second','usr_shared','team_second','member','removed',?)",
            (_now(),),
        )
        await db.commit()
    before = await store.get_user("usr_shared")
    assert before is not None
    client.cookies.set(
        "bowhead_jwt",
        _user_jwt("usr_owner", "owner@example.com"),
    )
    response = client.post(
        "/api/v1/admin/members/usr_shared/credential-recovery",
        json={},
        headers=JSON_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "shared_identity_recovery_requires_self_service"
    after = await store.get_user("usr_shared")
    assert after is not None
    assert after.password_hash == before.password_hash
    assert after.pwd_version == before.pwd_version


@pytest.mark.asyncio
async def test_credential_recovery_store_error_does_not_leak_internal_reason(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_team(store)
    await _seed_user(
        store,
        user_id="usr_owner",
        email="owner@example.com",
        owner=True,
    )
    await _seed_user(
        store,
        user_id="usr_target",
        email="target@example.com",
    )

    async def _fail_create(**_kwargs) -> None:
        raise ValueError("sensitive internal recovery state")

    monkeypatch.setattr(store, "create_credential_recovery_token", _fail_create)
    client.cookies.set(
        "bowhead_jwt",
        _user_jwt("usr_owner", "owner@example.com"),
    )

    response = client.post(
        "/api/v1/admin/members/usr_target/credential-recovery",
        json={},
        headers=JSON_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "credential_recovery_not_allowed",
    }
    assert "sensitive" not in response.text


def test_legacy_reset_rejects_invalid_token_with_structured_code(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/auth/reset-password",
        json={
            "token": "invalid-reset-token",
            "new_password": "NewPassw0rd!",
        },
        headers=JSON_HEADERS,
    )

    assert response.status_code == 410
    assert response.json()["detail"] == {
        "code": "recovery_invalid_or_expired",
    }


@pytest.mark.asyncio
async def test_removed_identity_keeps_session_but_product_routes_require_membership(
    client: TestClient,
    store: UserStore,
) -> None:
    await _seed_team(store)
    await _seed_user(
        store,
        user_id="usr_removed",
        email="removed@example.com",
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE team_members SET status='removed' WHERE user_id='usr_removed' AND team_id='team_activation'",
        )
        await db.commit()
    client.cookies.set(
        "bowhead_jwt",
        _user_jwt("usr_removed", "removed@example.com"),
    )

    identity = client.get("/api/v1/auth/me")
    assert identity.status_code == 200
    assert identity.json()["membership_required"] is True
    product = client.get("/api/v1/auth/me/workspaces")
    assert product.status_code == 403
    assert product.json()["detail"]["code"] == "team_membership_required"


@pytest.mark.asyncio
async def test_multi_team_off_blocks_matching_cross_team_identity_before_accept(
    client: TestClient,
    store: UserStore,
) -> None:
    await _seed_team(store)
    await _seed_team(store, team_id="team_second")
    await _seed_user(
        store,
        user_id="usr_cross_team",
        email="cross-team@example.com",
        team_id="team_second",
    )
    token = await _team_invite(store, email="cross-team@example.com")
    client.cookies.set(
        "bowhead_jwt",
        _user_jwt(
            "usr_cross_team",
            "cross-team@example.com",
            team_id="team_second",
        ),
    )
    assert (
        client.post(
            "/api/v1/auth/invites/intent",
            json={"token": token},
            headers=JSON_HEADERS,
        ).status_code
        == 200
    )

    branch = client.post(
        "/api/v1/auth/invites/intent/continue",
        json={},
        headers=JSON_HEADERS,
    )

    assert branch.status_code == 200
    assert branch.json()["next_action"] == "policy_blocked"
    assert await store.get_team_role("usr_cross_team", "team_activation") is None


@pytest.mark.asyncio
async def test_multi_team_accept_preserves_active_team_until_ready_cta(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEATURE_MULTI_TEAM_AUTHZ", "1")
    await _seed_team(store)
    await _seed_team(store, team_id="team_second")
    await _seed_user(
        store,
        user_id="usr_shared",
        email="shared@example.com",
        team_id="team_second",
    )
    token = await _team_invite(store, email="shared@example.com")
    client.cookies.set(
        "bowhead_jwt",
        _user_jwt("usr_shared", "shared@example.com", team_id="team_second"),
    )

    assert (
        client.post(
            "/api/v1/auth/invites/intent",
            json={"token": token},
            headers=JSON_HEADERS,
        ).status_code
        == 200
    )
    accepted = client.post(
        "/api/v1/auth/invites/accept",
        json={},
        headers=JSON_HEADERS,
    )

    assert accepted.status_code == 200, accepted.text
    assert await store.get_team_role("usr_shared", "team_activation") == "member"
    user = await store.get_user("usr_shared")
    assert user is not None
    assert user.team_id == "team_second"
    assert user.active_team_id is None


@pytest.mark.asyncio
async def test_local_flag_off_refuses_invite_that_cannot_be_activated(
    client: TestClient,
    store: UserStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEATURE_INVITE_ONLY_ACTIVATION", "0")
    await _seed_team(store)
    await _seed_user(
        store,
        user_id="usr_owner",
        email="owner@example.com",
        owner=True,
    )
    client.cookies.set(
        "bowhead_jwt",
        _user_jwt("usr_owner", "owner@example.com"),
    )

    response = client.post(
        "/api/v1/admin/members/invite",
        json={"email": "cannot-activate@example.com"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "feature_disabled_use_direct_create"
    assert await store.list_team_invites("team_activation") == []
