"""Regression: anonymous (no-JWT) requests must fail closed (401) on non-dev deployments.

Root cause of the 116 '免密登录' bug: ``resolve_user_id_sync`` / ``resolve_user_or_stub`` fell back
to ``settings.dev_default_user`` for ANY no-JWT request, regardless of auth_mode. On the 116 SaaS
box (auth_mode='prod') that served every store route (/sessions, /user-settings, /memory, /files,
/skills, ...) as dev_default_user WITHOUT login — opening a private browser window straight onto the
app showed a logged-in session. The /auth/me chokepoint (``_current_user``) was correctly gated;
these sync resolvers were not.

Fix (``_is_dev_auth_mode``): both resolvers — and the legacy X-Bowhead-User-Id header path — now
attribute an unauthenticated request to a stub identity ONLY when auth_mode == 'dev'. Any other mode
(prod / oidc / jwt / missing) → 401.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_gateway.routes import auth as auth_routes
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient


def _req(*, auth_mode: str, headers: dict | None = None, cookies: dict | None = None):
    """Minimal Request stand-in — the resolvers only touch .cookies/.headers/.app.state.settings."""
    settings = SimpleNamespace(dev_default_user="u_dev_alice", auth_mode=auth_mode)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, deployment_mode="saas"))
    return SimpleNamespace(cookies=cookies or {}, headers=headers or {}, app=app)


# ── _is_dev_auth_mode predicate ──────────────────────────────────────────────


def test_predicate_dev_only():
    assert auth_routes._is_dev_auth_mode(SimpleNamespace(auth_mode="dev")) is True
    assert auth_routes._is_dev_auth_mode(SimpleNamespace(auth_mode="prod")) is False
    assert auth_routes._is_dev_auth_mode(SimpleNamespace(auth_mode="jwt")) is False
    # missing attr → fail-closed (non-dev)
    assert auth_routes._is_dev_auth_mode(SimpleNamespace()) is False


# ── resolve_user_id_sync ─────────────────────────────────────────────────────


def test_sync_dev_anon_returns_stub():
    assert auth_routes.resolve_user_id_sync(_req(auth_mode="dev")) == "u_dev_alice"


def test_sync_prod_anon_raises_401():
    with pytest.raises(HTTPException) as ei:
        auth_routes.resolve_user_id_sync(_req(auth_mode="prod"))
    assert ei.value.status_code == 401


def test_sync_prod_header_spoof_rejected():
    """X-Bowhead-User-Id no longer lets a caller assert an identity on a non-dev box — 401 (the
    closed spoof vector). Header lockdown is also covered in tests/test_entitlement_lockdown.py."""
    with pytest.raises(HTTPException) as ei:
        auth_routes.resolve_user_id_sync(_req(auth_mode="prod", headers={"x-bowhead-user-id": "u_victim"}))
    assert ei.value.status_code == 401


def test_sync_dev_header_still_honored():
    """dev auth_mode: the legacy iframe-loader header path stays intact."""
    assert auth_routes.resolve_user_id_sync(_req(auth_mode="dev", headers={"x-bowhead-user-id": "u_iframe"})) == "u_iframe"


# ── resolve_user_or_stub ─────────────────────────────────────────────────────


async def test_stub_dev_anon_returns_teamless_stub():
    uid, team = await auth_routes.resolve_user_or_stub(_req(auth_mode="dev"))
    assert (uid, team) == ("u_dev_alice", None)


async def test_stub_prod_anon_raises_401():
    with pytest.raises(HTTPException) as ei:
        await auth_routes.resolve_user_or_stub(_req(auth_mode="prod"))
    assert ei.value.status_code == 401
    assert ei.value.detail == {"code": "auth_required"}


# ── end-to-end: a store route resolved via _user_id 401s anonymously on prod ──


def _mini_app(auth_mode: str) -> FastAPI:
    a = FastAPI()
    a.state.settings = SimpleNamespace(dev_default_user="u_dev_alice", auth_mode=auth_mode)
    a.state.deployment_mode = "saas"

    @a.get("/api/v1/probe")
    def _probe(request: Request):
        return {"uid": auth_routes.resolve_user_id_sync(request)}

    return a


def test_e2e_prod_store_route_anonymous_401():
    """The actual reported bug: no cookie on a prod box → 401, not a dev_default_user session."""
    r = TestClient(_mini_app("prod")).get("/api/v1/probe")
    assert r.status_code == 401, r.text


def test_e2e_dev_store_route_anonymous_ok():
    r = TestClient(_mini_app("dev")).get("/api/v1/probe")
    assert r.status_code == 200
    assert r.json()["uid"] == "u_dev_alice"


# ── resolve_ws_user_id (WebSocket fail-closed) — task_plan WS authz ───────────


class _FakeWS:
    def __init__(self, *, auth_mode, cookies=None, headers=None):
        self.cookies = cookies or {}
        self.headers = headers or {}
        settings = SimpleNamespace(dev_default_user="u_dev_alice", auth_mode=auth_mode)
        self.app = SimpleNamespace(state=SimpleNamespace(settings=settings))


def test_ws_user_dev_anon_returns_stub():
    assert auth_routes.resolve_ws_user_id(_FakeWS(auth_mode="dev")) == "u_dev_alice"


def test_ws_user_prod_anon_none():
    # non-dev WS with no JWT → None → caller closes the socket (was: anyone could connect).
    assert auth_routes.resolve_ws_user_id(_FakeWS(auth_mode="prod")) is None


# ── assert_turn_access (SSE turn ownership) — /chat/stream + /turns/replay ────


class _FakeTurnStore:
    """turn_id → session_id → user_id, for the turn-ownership chain."""

    def __init__(self, turn_to_session, session_to_owner):
        self._ts, self._so = turn_to_session, session_to_owner

    async def get_turn(self, turn_id):
        sid = self._ts.get(turn_id)
        return SimpleNamespace(session_id=sid) if sid else None

    async def get_session(self, sid):
        owner = self._so.get(sid)
        return SimpleNamespace(user_id=owner) if owner else None


async def test_turn_access_prod_anon_401():
    with pytest.raises(HTTPException) as ei:
        await auth_routes.assert_turn_access(_req(auth_mode="prod"), "t1", None)
    assert ei.value.status_code == 401


async def test_turn_access_cross_user_403():
    # dev stub caller = u_dev_alice; turn t1's session is owned by someone else → IDOR blocked.
    store = _FakeTurnStore({"t1": "s1"}, {"s1": "u_other"})
    with pytest.raises(HTTPException) as ei:
        await auth_routes.assert_turn_access(_req(auth_mode="dev"), "t1", store)
    assert ei.value.status_code == 403


async def test_turn_access_owner_ok():
    store = _FakeTurnStore({"t1": "s1"}, {"s1": "u_dev_alice"})
    uid = await auth_routes.assert_turn_access(_req(auth_mode="dev"), "t1", store)
    assert uid == "u_dev_alice"
