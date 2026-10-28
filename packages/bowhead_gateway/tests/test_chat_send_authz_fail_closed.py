"""ws-datarc-decouple [review P1-a / P1-c] — POST /chat/send fails CLOSED under membership enforce.

P1-a: the earlier code wrapped the whole authz block in ``if resolved_team_id:``, so under
BOWHEAD_WS_MEMBERSHIP_ENFORCE=1 an team-less caller (no JWT → dev stub, team_id None) and a
missing membership store both SKIPPED authz and proceeded into the datarc/turn work — a fail-OPEN
gap. P1-c: the call passed ws_team_id=None, so authorize_ws_access's cross_team leg was dead and a
stale/cross-team member row (get_member_role keys only on (ws, user)) leaked access — now the real
workspace team is resolved so cross_team fires.

These drive the real endpoint (lightweight: only the chat router mounted, like test_g2_ws_endpoint)
and assert every enforce path ends in allow-or-deny BEFORE any turn work. Positive + enforce-OFF
mirrors prove the gate — not some unrelated error — is what produces (or withholds) the 4xx.
"""

from __future__ import annotations

from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import auth as auth_routes
from bowhead_gateway.routes import chat as chat_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app(*, user_store) -> FastAPI:
    """Minimal app: only the chat router + the app.state the send() authz preamble reads.

    orchestrator is a bare stub — _get_orchestrator only needs it non-None, and it is never
    dereferenced because we POST without a session_id (the steer-intercept block is skipped).
    """
    a = FastAPI()
    # auth_mode="dev": no-JWT must still yield the team-less dev stub so the membership-enforce gate
    # (the SUT here) is what produces allow/deny. Under a non-dev auth_mode resolve_user_or_stub now
    # fail-closes to 401 BEFORE the gate (covered separately by test_anon_auth_bypass).
    a.state.settings = SimpleNamespace(dev_default_user="dev_user", auth_mode="dev")
    a.state.user_store = user_store
    a.state.orchestrator = SimpleNamespace()
    a.include_router(chat_routes.router, prefix="/api/v1/chat")
    return a


def _send(client: TestClient):
    # no session_id → steer block skipped; no JWT cookie → team-less dev stub (team_id None)
    return client.post("/api/v1/chat/send", json={"workspace_id": "ws_x", "message": "hi"})


def test_enforce_teamless_caller_denied_403(monkeypatch) -> None:
    """enforce ON + team-less caller → 403 no_team. Store present but get_member_role is NEVER
    called for an team-less caller (resolved_team_id None → existing_role None), so a bare stub
    store is enough — the deny comes purely from authorize_ws_access rejecting caller_team_id=None."""
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")
    client = TestClient(_app(user_store=SimpleNamespace()))  # non-None store, never queried
    r = _send(client)
    assert r.status_code == 403, r.text
    err = r.json()["detail"]["error"]
    assert err["code"] == "ws_access_denied"
    assert err["reason"] == "no_team"


def test_enforce_missing_store_fails_closed_503(monkeypatch) -> None:
    """enforce ON + no membership store wired → 503 authz_unavailable (cannot verify → deny),
    NOT a silent pass-through into the turn."""
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")
    client = TestClient(_app(user_store=None))
    r = _send(client)
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["error"]["code"] == "authz_unavailable"


def test_enforce_lookup_failure_fails_closed_503(monkeypatch) -> None:
    """enforce ON + a caller WITH an team but the authz lookup raises → 503 authz_lookup_failed.
    A JWT-less request can't carry an team, so we stub resolve_user_or_stub to yield one and a store
    whose ownership/membership read raises — asserting the except-arm fails closed (503), not open."""
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")

    async def _stub_resolve(_request):
        return ("u_boom", "team_boom")  # caller HAS an team → lookup is attempted

    # send() does a lazy `from bowhead_gateway.routes.auth import resolve_user_or_stub`, so the
    # name is resolved on the auth module at call time — patch it there, not on chat_routes.
    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", _stub_resolve)

    class _BoomStore:
        async def get_workspace_team_id(self, ws_id):
            raise RuntimeError("db down")

        async def get_member_role(self, ws_id, user_id):
            raise RuntimeError("db down")

    client = TestClient(_app(user_store=_BoomStore()))
    r = _send(client)
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["error"]["code"] == "authz_lookup_failed"


def test_enforce_cross_team_member_row_denied_403(monkeypatch) -> None:
    """[review P1-c] caller in team_B holding an ACTIVE member row in a workspace OWNED by team_A
    must be denied (cross_team), NOT allowed by the stale row. get_member_role keys only on
    (ws, user), so without resolving the workspace's real team the cross_team leg was dead and the
    stale row leaked access. Proves ws_team_id is now resolved and the cross_team leg is live."""
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")

    async def _stub_resolve(_request):
        return ("u1", "team_B")  # caller belongs to team_B

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", _stub_resolve)

    class _CrossOrgStore:
        async def get_workspace_team_id(self, ws_id):
            return "team_A"  # workspace belongs to a DIFFERENT team

        async def get_member_role(self, ws_id, user_id):
            return "member"  # stale/cross-team active member row exists

    client = TestClient(_app(user_store=_CrossOrgStore()))
    r = _send(client)
    assert r.status_code == 403, r.text
    err = r.json()["detail"]["error"]
    assert err["code"] == "ws_access_denied"
    assert err["reason"] == "cross_team"


def test_enforce_same_team_member_not_denied_by_authz(monkeypatch) -> None:
    """Positive: a legit same-team member (caller team == workspace team, active role) is NOT denied
    by the authz preamble — proves the P1-c ws_team_id resolution didn't break valid access. The
    request proceeds past the gate into downstream turn machinery the stub lacks, so we only assert
    it is NOT a ws_access_denied / authz_* response from the gate."""
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")

    async def _stub_resolve(_request):
        return ("u1", "team_A")  # caller's team == workspace's team

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", _stub_resolve)

    class _SameOrgStore:
        async def get_workspace_team_id(self, ws_id):
            return "team_A"

        async def get_member_role(self, ws_id, user_id):
            return "member"

    client = TestClient(_app(user_store=_SameOrgStore()), raise_server_exceptions=False)
    r = _send(client)
    if r.status_code in (403, 503):
        code = r.json().get("detail", {}).get("error", {}).get("code")
        assert code not in {"ws_access_denied", "authz_unavailable", "authz_lookup_failed"}, (
            f"a legit same-team member must pass the gate, got {code}"
        )


def test_enforce_off_teamless_caller_not_denied_by_authz(monkeypatch) -> None:
    """Mirror: enforce OFF → the SAME team-less request is NOT rejected by the authz gate. It
    proceeds past the preamble (and only then hits downstream turn machinery the stub lacks), so
    we just assert it is NOT the 403 ws_access_denied / 503 authz_* the gate would emit."""
    monkeypatch.delenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", raising=False)
    client = TestClient(_app(user_store=None), raise_server_exceptions=False)
    r = _send(client)
    # not gated by membership authz: never a deny/authz-unavailable from the enforce preamble
    if r.status_code in (403, 503):
        code = r.json().get("detail", {}).get("error", {}).get("code")
        assert code not in {"ws_access_denied", "authz_unavailable", "authz_lookup_failed"}, (
            f"enforce OFF must not trip the membership gate, got {code}"
        )


@pytest.mark.asyncio
async def test_archived_space_rejected_before_turn_allocation(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")
    store = UserStore(tmp_path / "chat-lifecycle.db")
    await store.initialize()
    team = await store.create_team(name="Chat Lifecycle")
    owner = await store.create_user(
        team_id=team.id,
        email="owner@example.com",
        password="StrongPass123!",
    )
    await store.set_team_creator(team.id, owner.id)
    workspace_id = await store.create_local_workspace(
        team_id=team.id,
        name="Archived Chat",
        creator_user_id=owner.id,
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id=?",
            (workspace_id,),
        )
        await db.commit()

    async def _resolve(_request):
        return owner.id, team.id

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", _resolve)
    response = TestClient(_app(user_store=store)).post(
        "/api/v1/chat/send",
        json={"workspace_id": workspace_id, "message": "hi"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "workspace_archived"
