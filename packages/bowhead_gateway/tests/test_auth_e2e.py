"""SP-AUTH-2 (#97) auth e2e — register / login / refresh / me / logout 真路径验.

不依赖 SMTP / OAuth provider — 只验本地 JWT + cookie + user_store path.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3
from types import SimpleNamespace

import httpx2
import jwt
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.user_store import UserStore
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import SessionStore


@pytest.fixture
async def app_ctx_auth(tmp_path, monkeypatch):
    # Set JWT secret or registration returns 500.
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test_secret_for_e2e_only_32bytes_xxx")
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_KEY",
        base64.urlsafe_b64encode(b"auth-e2e-composer-key-32-bytes!!").decode().rstrip("="),
    )
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    # Disable prod mode (secure cookies fail in tests).
    monkeypatch.delenv("BOWHEAD_PROD", raising=False)

    from bowhead_gateway.routes import auth as auth_routes

    auth_routes._RATE_BUCKET.clear()

    async def _skip_identity_email(*_args, **_kwargs) -> None:
        # Identity-email branding/entry invariants have their own integration
        # suite; this auth test intentionally covers only JWT, cookie and store paths.
        return None

    monkeypatch.setattr(auth_routes, "_enqueue_verify_email", _skip_identity_email)
    db = tmp_path / "bowhead.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db}", target="core")
    sess = SessionStore(str(db))
    await sess.initialize()
    user = UserStore(str(db))
    await user.initialize()
    from bowhead_core.public_entries import PublicEntryStore

    public_entries = PublicEntryStore(db)
    await public_entries.initialize()

    app = create_app()
    if app.state.edition == "community":
        from bowhead_core.installation import community_setup_token, initialize_installation, setup_community_owner

        monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
        identity = await initialize_installation(str(db), "community", admitted_fresh=True)
        await setup_community_owner(
            str(db),
            setup_token=community_setup_token(identity),
            team_name="Synthetic Auth Setup",
            email="setup-owner@example.test",
            password="SyntheticSetupOwner7!",
        )
    from bowhead_gateway.public_entry_context import PublicAccessContext

    from bowhead_gateway import public_entry_context as public_entry_context_module

    async def _resolved_test_entry(_scope, _store):
        return PublicAccessContext(
            base_url="http://gw",
            origin="http://gw",
            base_path="",
            app_root="http://gw/",
            match_source="direct",
        )

    monkeypatch.setattr(
        public_entry_context_module,
        "resolve_public_access_context",
        _resolved_test_entry,
    )

    @app.middleware("http")
    async def _public_entry_context(request, call_next):  # type: ignore[no-untyped-def]
        token = public_entry_context_module.set_public_access_context(
            await _resolved_test_entry(request.scope, public_entries),
        )
        try:
            return await call_next(request)
        finally:
            public_entry_context_module.reset_public_access_context(token)

    app.state.settings = Settings()
    app.state.session_store = sess
    app.state.user_store = user
    app.state.public_entry_store = public_entries
    app.state.canvas_store = None
    from bowhead_core.agent_assignment_store import AgentAssignmentStore

    assignment_store = AgentAssignmentStore(db)
    await assignment_store.backfill_existing_access(apply=True, source_build="auth-test")
    app.state.agent_assignment_store = assignment_store
    app.state.agent_marketplace_schema_ready = True
    app.state.orchestrator = None
    # This suite exercises the SaaS self-registration funnel. Account policy
    # defaults to private/local now, where self-registration is intentionally
    # disabled.
    app.state.deployment_mode = "saas"
    return app


@pytest.mark.asyncio
async def test_composer_auth_generation_store_is_best_effort_while_protocol_disabled(
    monkeypatch,
) -> None:
    from bowhead_gateway.routes.auth import _register_composer_auth_generation

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_store=None)),
    )
    monkeypatch.delenv("BOWHEAD_SUBMISSION_FINGERPRINT_KEY", raising=False)
    await _register_composer_auth_generation(
        request,
        user_id="u_test",
        generation="asg_test",
    )

    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="),
    )
    await _register_composer_auth_generation(
        request,
        user_id="u_test",
        generation="asg_test",
    )


@pytest.mark.asyncio
async def test_composer_auth_generation_is_prepared_before_protocol_enable(
    monkeypatch,
) -> None:
    from bowhead_gateway.routes.auth import _register_composer_auth_generation

    calls: list[dict[str, object]] = []

    class RecordingStore:
        async def register_chat_auth_session_generation(self, **kwargs) -> bool:
            calls.append(kwargs)
            return True

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(session_store=RecordingStore()),
        ),
    )
    monkeypatch.delenv("BOWHEAD_SUBMISSION_FINGERPRINT_KEY", raising=False)
    await _register_composer_auth_generation(
        request,
        user_id="u_test",
        generation="asg_test",
        issued_at_epoch=1.0,
    )

    assert len(calls) == 1
    assert calls[0]["user_id"] == "u_test"


@pytest.mark.integration
async def test_team_onboarding_route_is_always_mounted(app_ctx_auth) -> None:
    assert "/api/v1/team-onboarding/status" in app_ctx_auth.openapi()["paths"]


@pytest.mark.integration
async def test_register_login_me_logout_flow(
    app_ctx_auth,
    monkeypatch,
) -> None:
    """完整流: register → me (cookie 自动) → logout → me 401."""
    monkeypatch.setenv("FEATURE_COMPOSER_MODEL_PANEL_V2", "1")
    monkeypatch.setenv("FEATURE_COMPOSER_MODEL_PANEL_V2_PCT", "100")
    monkeypatch.setenv("FEATURE_THINKING_EFFORT", "1")
    monkeypatch.setenv("FEATURE_THINKING_EFFORT_PCT", "100")
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="),
    )
    transport = httpx2.ASGITransport(app=app_ctx_auth)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        # 1. register
        r = await c.post(
            "/api/v1/auth/register",
            json={"email": "alice@test.com", "password": "Passw0rd!", "name": "Alice"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user"]["email"] == "alice@test.com"
        # Cookie set: any cookie suffices (name may be bowhead_session/session_token).
        assert len(list(c.cookies.jar)) >= 1

        # 2. /me with cookie (authoring and both draft flags use defaults, independent of local env).
        monkeypatch.delenv("FEATURE_SUBAGENT_DRAFT_CHAT_EDIT", raising=False)
        monkeypatch.delenv("FEATURE_CONFIGURATION_CANVAS_V1", raising=False)
        r = await c.get("/api/v1/auth/me")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store, private"
        me = r.json()
        assert me["email"] == "alice@test.com"
        assert me["capabilities"]["team_onboarding"] is True
        assert me["capabilities"]["subagent_management_v2"] is True
        assert me["capabilities"]["configuration_agent"] is True
        assert me["capabilities"]["settings_pages_canvas"] is True
        assert me["capabilities"]["configuration_canvas"] is True
        monkeypatch.setenv("FEATURE_CONFIGURATION_CANVAS_V1", "0")
        assert (await c.get("/api/v1/auth/me")).json()["capabilities"]["configuration_canvas"] is False
        monkeypatch.setenv("FEATURE_CONFIGURATION_CANVAS_V1", "1")
        # Draft chat editing has its own rollout; unified drafts are available with a Team.
        assert me["capabilities"]["subagent_authoring"] == {
            "authoring": True,
            "draft_chat_edit": False,
            "unified_draft": True,
        }
        market_without_authoring = (await c.get("/api/v1/auth/me")).json()["capabilities"]
        assert market_without_authoring["subagent_management_v2"] is True
        assert market_without_authoring["subagent_creator"] is True
        assert market_without_authoring["configuration_canvas"] is True
        with sqlite3.connect(app_ctx_auth.state.user_store.db_path) as connection:
            marker = connection.execute(
                "SELECT assignment_cutover_complete_at FROM agent_marketplace_schema_meta WHERE singleton=1",
            ).fetchone()[0]
            connection.execute(
                "UPDATE agent_marketplace_schema_meta SET assignment_cutover_complete_at=NULL WHERE singleton=1",
            )
            connection.commit()
        assert (await c.get("/api/v1/auth/me")).json()["capabilities"]["subagent_management_v2"] is False
        with sqlite3.connect(app_ctx_auth.state.user_store.db_path) as connection:
            connection.execute(
                "UPDATE agent_marketplace_schema_meta SET assignment_cutover_complete_at=? WHERE singleton=1",
                (marker,),
            )
            connection.commit()
        monkeypatch.delenv("FEATURE_CONFIGURATION_CANVAS_V1", raising=False)
        assert me["capabilities"]["composer"] == {
            "model_panel_v2": False,
            "reference_input": False,
            # 14033edd0: manual reference Plan readiness (FEATURE_CHAT_REFERENCE_PLAN, default off).
            "reference_plan": False,
            "agent_start_use": False,
            "thinking_effort": False,
            "submission_protocol": False,
        }
        assert me["capabilities"]["planning"] is True
        assert me["chat_execution"]["write_protocol"] == "legacy_v0"
        assert me["chat_execution"]["config_generation"].startswith(
            "durable_chat_v1_",
        )
        assert me["chat_execution"]["readiness_generation"].startswith(
            "durable_chat_ready_v1_",
        )
        assert me["chat_execution"]["attachment_refs_wire_version"] == 1
        assert me["capability_revision"].startswith("cap_v1_")
        assert me["fetched_at"]
        auth_session_generation = me["auth_session_generation"]
        assert len(auth_session_generation) >= 20

        # Refresh rotates both credentials but preserves the non-credential
        # session generation used to fence late capability responses.
        r = await c.post("/api/v1/auth/refresh")
        assert r.status_code == 200, r.text
        r = await c.get("/api/v1/auth/me")
        assert r.status_code == 200
        refreshed_me = r.json()
        assert refreshed_me["auth_session_generation"] == auth_session_generation
        assert refreshed_me["capability_revision"] == me["capability_revision"]

        # 3. logout
        r = await c.post("/api/v1/auth/logout")
        assert r.status_code == 200

        # 4. /me without valid cookie → 401
        c.cookies.clear()
        r = await c.get("/api/v1/auth/me")
        assert r.status_code == 401


@pytest.mark.integration
async def test_refresh_only_logout_fences_generation_before_first_chat_claim(
    app_ctx_auth,
) -> None:
    """A refresh-only logout must fence the chain even before /me or chat claim."""
    transport = httpx2.ASGITransport(app=app_ctx_auth)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        registered = await c.post(
            "/api/v1/auth/register",
            json={"email": "refresh-only@test.com", "password": "Passw0rd!"},
        )
        assert registered.status_code == 200, registered.text
        user_id = registered.json()["user"]["id"]
        refresh = c.cookies.get("bowhead_refresh")
        assert refresh is not None
        generation = refresh.split(".", maxsplit=2)[1]
        generation_hash = hashlib.sha256(
            b"bowhead/chat-auth-session-generation/v1\0" + generation.encode(),
        ).hexdigest()

        c.cookies.clear()
        c.cookies.set("bowhead_refresh", refresh)
        logged_out = await c.post("/api/v1/auth/logout")
        assert logged_out.status_code == 200, logged_out.text

        session_store = app_ctx_auth.state.session_store
        assert not await session_store.is_chat_auth_session_generation_active(
            user_id=user_id,
            auth_session_generation_hash=generation_hash,
        )
        assert not await session_store.register_chat_auth_session_generation(
            user_id=user_id,
            auth_session_generation_hash=generation_hash,
        )


async def _register_for_refresh(app, email: str) -> tuple[str, str, str]:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as bootstrap:
        registered = await bootstrap.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "Passw0rd!"},
        )
        assert registered.status_code == 200, registered.text
        refresh = bootstrap.cookies.get("bowhead_refresh")
        assert refresh is not None
    generation = refresh.split(".", maxsplit=2)[1]
    generation_hash = hashlib.sha256(
        b"bowhead/chat-auth-session-generation/v1\0" + generation.encode(),
    ).hexdigest()
    return registered.json()["user"]["id"], refresh, generation_hash


async def _refresh_with(app, refresh: str) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        client.cookies.set("bowhead_refresh", refresh)
        return await client.post("/api/v1/auth/refresh")


def _synchronize_rotations(monkeypatch, store) -> None:
    """Hold both racers until each has passed verification, then rotate."""
    original_rotate = store.rotate_refresh_token
    both_entered = asyncio.Event()
    entrants = 0
    entrants_lock = asyncio.Lock()

    async def synchronized_rotate(**kwargs):
        nonlocal entrants
        async with entrants_lock:
            entrants += 1
            if entrants == 2:
                both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=2)
        return await original_rotate(**kwargs)

    monkeypatch.setattr(store, "rotate_refresh_token", synchronized_rotate)


def _successor_refresh(response: httpx2.Response) -> str:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith("bowhead_refresh=") and not header.startswith('bowhead_refresh=""'):
            return header.split(";", 1)[0].split("=", 1)[1]
    raise AssertionError(f"no successor refresh cookie in {response.headers.get_list('set-cookie')}")


@pytest.mark.integration
async def test_concurrent_tab_refresh_keeps_the_winning_session(
    app_ctx_auth,
    monkeypatch,
) -> None:
    """B13: two tabs refresh the same cookie concurrently; the loser must not log out everyone."""
    user_id, refresh, generation_hash = await _register_for_refresh(app_ctx_auth, "refresh-race@test.com")
    store = app_ctx_auth.state.user_store
    _synchronize_rotations(monkeypatch, store)

    responses = await asyncio.gather(
        _refresh_with(app_ctx_auth, refresh),
        _refresh_with(app_ctx_auth, refresh),
    )
    assert sorted(response.status_code for response in responses) == [200, 401]
    winner = next(response for response in responses if response.status_code == 200)
    loser = next(response for response in responses if response.status_code == 401)
    assert loser.json()["detail"]["code"] == "refresh_superseded"
    # The loser carries no credentials and no cookie deletion for the shared jar.
    assert not any(h.startswith(("bowhead_refresh=", "bowhead_jwt=")) for h in loser.headers.get_list("set-cookie"))

    session_store = app_ctx_auth.state.session_store
    assert await session_store.is_chat_auth_session_generation_active(
        user_id=user_id,
        auth_session_generation_hash=generation_hash,
    )
    successor = _successor_refresh(winner)
    assert await store.verify_refresh_token(successor) == user_id
    renewed = await _refresh_with(app_ctx_auth, successor)
    assert renewed.status_code == 200, renewed.text


@pytest.mark.integration
async def test_concurrent_refresh_outside_grace_still_revokes_all_sessions(
    app_ctx_auth,
    monkeypatch,
) -> None:
    from bowhead_gateway.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "_REFRESH_ROTATION_GRACE_SEC", 0.0)
    user_id, refresh, generation_hash = await _register_for_refresh(app_ctx_auth, "refresh-replay@test.com")
    store = app_ctx_auth.state.user_store
    _synchronize_rotations(monkeypatch, store)

    responses = await asyncio.gather(
        _refresh_with(app_ctx_auth, refresh),
        _refresh_with(app_ctx_auth, refresh),
    )
    assert sorted(response.status_code for response in responses) == [200, 401]
    loser = next(response for response in responses if response.status_code == 401)
    assert loser.json()["detail"] == "refresh race / replay detected, all sessions revoked"
    winner = next(response for response in responses if response.status_code == 200)
    assert await store.verify_refresh_token(_successor_refresh(winner)) is None
    session_store = app_ctx_auth.state.session_store
    assert not await session_store.is_chat_auth_session_generation_active(
        user_id=user_id,
        auth_session_generation_hash=generation_hash,
    )
    assert not await session_store.register_chat_auth_session_generation(
        user_id=user_id,
        auth_session_generation_hash=generation_hash,
    )


@pytest.mark.integration
async def test_sequential_refresh_with_just_rotated_predecessor_is_superseded(
    app_ctx_auth,
) -> None:
    user_id, refresh, _generation_hash = await _register_for_refresh(app_ctx_auth, "refresh-late-tab@test.com")
    store = app_ctx_auth.state.user_store

    first = await _refresh_with(app_ctx_auth, refresh)
    assert first.status_code == 200, first.text
    # A second tab's request sent with the old cookie arrives 40 ms later.
    late = await _refresh_with(app_ctx_auth, refresh)
    assert late.status_code == 401
    assert late.json()["detail"]["code"] == "refresh_superseded"
    assert "token" not in late.json()
    successor = _successor_refresh(first)
    assert await store.verify_refresh_token(successor) == user_id


@pytest.mark.integration
async def test_predecessor_replayed_after_grace_is_rejected(app_ctx_auth) -> None:
    _user_id, refresh, _generation_hash = await _register_for_refresh(app_ctx_auth, "refresh-stale@test.com")
    store = app_ctx_auth.state.user_store
    first = await _refresh_with(app_ctx_auth, refresh)
    assert first.status_code == 200, first.text

    rotated_long_ago = "2026-01-01T00:00:00+00:00"
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE refresh_tokens SET revoked_at=?, last_used_at=? WHERE token_hash=?",
            (rotated_long_ago, rotated_long_ago, hashlib.sha256(refresh.encode()).hexdigest()),
        )
        connection.commit()
    replay = await _refresh_with(app_ctx_auth, refresh)
    assert replay.status_code == 401
    assert replay.json()["detail"] == "refresh token invalid / revoked / expired"


@pytest.mark.integration
async def test_logged_out_refresh_token_is_not_treated_as_superseded(app_ctx_auth) -> None:
    _user_id, refresh, _generation_hash = await _register_for_refresh(app_ctx_auth, "refresh-logout@test.com")
    transport = httpx2.ASGITransport(app=app_ctx_auth)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        client.cookies.set("bowhead_refresh", refresh)
        assert (await client.post("/api/v1/auth/logout")).status_code == 200
    replay = await _refresh_with(app_ctx_auth, refresh)
    assert replay.status_code == 401
    assert replay.json()["detail"] == "refresh token invalid / revoked / expired"


@pytest.mark.integration
@pytest.mark.parametrize("legacy", (False, True))
async def test_bearer_only_logout_fences_current_and_legacy_access_generations(
    app_ctx_auth,
    legacy: bool,
) -> None:
    transport = httpx2.ASGITransport(app=app_ctx_auth)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        registered = await c.post(
            "/api/v1/auth/register",
            json={
                "email": f"bearer-{'legacy' if legacy else 'current'}@test.com",
                "password": "Passw0rd!",
            },
        )
        assert registered.status_code == 200, registered.text
        user_id = registered.json()["user"]["id"]
        access_token = registered.json()["token"]

        if legacy:
            from bowhead_gateway.routes import auth as auth_routes

            decoded = auth_routes._decode_jwt_multi(access_token)
            assert decoded is not None
            decoded.pop("asg", None)
            access_token = jwt.encode(
                decoded,
                "test_secret_for_e2e_only_32bytes_xxx",
                algorithm="HS256",
            )
            generation = (
                "legacy_"
                + hashlib.sha256(
                    f"bowhead-auth-session-legacy.v1\0{access_token}".encode(),
                ).hexdigest()[:32]
            )
        else:
            from bowhead_gateway.routes import auth as auth_routes

            decoded = auth_routes._decode_jwt_multi(access_token)
            assert decoded is not None
            generation = str(decoded["asg"])

        c.cookies.clear()
        headers = {"Authorization": f"Bearer {access_token}"}
        me = await c.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200, me.text

        generation_hash = hashlib.sha256(
            b"bowhead/chat-auth-session-generation/v1\0" + generation.encode(),
        ).hexdigest()
        assert await app_ctx_auth.state.session_store.is_chat_auth_session_generation_active(
            user_id=user_id,
            auth_session_generation_hash=generation_hash,
        )

        logged_out = await c.post("/api/v1/auth/logout", headers=headers)
        assert logged_out.status_code == 200, logged_out.text
        assert not await app_ctx_auth.state.session_store.is_chat_auth_session_generation_active(
            user_id=user_id,
            auth_session_generation_hash=generation_hash,
        )


@pytest.mark.integration
async def test_logout_everywhere_fences_unseen_legacy_access_token(
    app_ctx_auth,
) -> None:
    """A pre-upgrade token must not register itself after the user-wide fence."""
    transport = httpx2.ASGITransport(app=app_ctx_auth)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as owner:
        registered = await owner.post(
            "/api/v1/auth/register",
            json={"email": "legacy-fence@test.com", "password": "Passw0rd!"},
        )
        assert registered.status_code == 200, registered.text
        current_token = registered.json()["token"]
        decoded = jwt.decode(
            current_token,
            "test_secret_for_e2e_only_32bytes_xxx",
            algorithms=["HS256"],
        )
        decoded.pop("asg", None)
        unseen_legacy_token = jwt.encode(
            decoded,
            "test_secret_for_e2e_only_32bytes_xxx",
            algorithm="HS256",
        )

        logged_out = await owner.post("/api/v1/auth/logout-everywhere")
        assert logged_out.status_code == 200, logged_out.text

    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
        headers={"Authorization": f"Bearer {unseen_legacy_token}"},
    ) as legacy:
        response = await legacy.get("/api/v1/auth/me")
    assert response.status_code == 401


@pytest.mark.integration
async def test_login_with_existing_user(app_ctx_auth) -> None:
    """login 已注册用户."""
    transport = httpx2.ASGITransport(app=app_ctx_auth)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        # Register first.
        r = await c.post(
            "/api/v1/auth/register",
            json={"email": "bob@test.com", "password": "Passw0rd!", "name": "Bob"},
        )
        assert r.status_code == 200
        # Logout clears cookies.
        await c.post("/api/v1/auth/logout")
        c.cookies.clear()

        # Login with same email/password.
        r = await c.post(
            "/api/v1/auth/login",
            json={"email": "bob@test.com", "password": "Passw0rd!"},
        )
        assert r.status_code == 200, r.text
        # /me works.
        r = await c.get("/api/v1/auth/me")
        assert r.status_code == 200
        assert r.json()["email"] == "bob@test.com"


@pytest.mark.integration
async def test_login_wrong_password(app_ctx_auth) -> None:
    """错密码 401."""
    transport = httpx2.ASGITransport(app=app_ctx_auth)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        await c.post(
            "/api/v1/auth/register",
            json={"email": "carol@test.com", "password": "Passw0rd!", "name": "Carol"},
        )
        c.cookies.clear()
        r = await c.post(
            "/api/v1/auth/login",
            json={"email": "carol@test.com", "password": "wrong_password"},
        )
        assert r.status_code in (400, 401)


@pytest.mark.integration
async def test_register_duplicate_email_rejected(app_ctx_auth) -> None:
    """同 email 二次注册 → 拒绝 (409 conflict OR 429 rate-limit OR 400 bad request)."""
    transport = httpx2.ASGITransport(app=app_ctx_auth)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        await c.post(
            "/api/v1/auth/register",
            json={"email": "dup@test.com", "password": "Passw0rd!"},
        )
        c.cookies.clear()
        r = await c.post(
            "/api/v1/auth/register",
            json={"email": "dup@test.com", "password": "Other123!"},
        )
        # Any non-200 is correct rejection (rate limits are reasonable production behavior too).
        assert r.status_code != 200, f"duplicate email should not 200, got {r.status_code}"


@pytest.mark.parametrize("revoked_at", [None, "2026-10-01T00:00:00+00:00"])
async def test_external_session_requires_its_adapter_on_access_and_refresh(app_ctx_auth, revoked_at) -> None:
    from bowhead_gateway.edition_extensions import GatewayExtensions

    app_ctx_auth.state.gateway_extensions = GatewayExtensions()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app_ctx_auth), base_url="http://gw") as client:
        registered = await client.post("/api/v1/auth/register", json={"email": "external-session@test.com", "password": "Passw0rd!"})
        assert registered.status_code == 200, registered.text
        assert (await client.get("/api/v1/auth/me")).status_code == 200
        password_refresh = await client.post("/api/v1/auth/refresh")
        assert password_refresh.status_code == 200, password_refresh.text
        payload = jwt.decode(client.cookies.get("bowhead_jwt"), "test_secret_for_e2e_only_32bytes_xxx", algorithms=["HS256"])
        with sqlite3.connect(app_ctx_auth.state.user_store.db_path) as connection:
            connection.execute(
                "INSERT INTO oidc_auth_sessions(auth_session_generation,provider_id,subject,user_id,config_revision,security_epoch,require_verified_email,created_at,revoked_at) VALUES (?,?,?,?,1,1,0,'now',?)",
                (payload["asg"], "oidc_existing", "external-subject", payload["sub"], revoked_at),
            )
            connection.commit()
        denied = await client.get("/api/v1/auth/me")
        assert denied.status_code == 401, denied.text
        assert denied.json()["detail"]["code"] == "oidc_session_revoked"
        refreshed = await client.post("/api/v1/auth/refresh")
        assert refreshed.status_code == 401, refreshed.text
        assert refreshed.json()["detail"]["code"] == "oidc_session_revoked"
