"""Report access requests end to end: approval effects, hooks wired through the real share
and canvas routes, and failure recovery. Kinds, lifecycle and anti-enumeration guards are
covered in ``test_access_request_flow.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.access_request_store import AccessRequestStore
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import NotificationStore
from bowhead_core.share_service import reset_content_sharing_cache
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import access_requests as ar
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Background follow-ups scheduled by the handlers (``ar._spawn``), drained deterministically.
_SPAWNED: list = []


def _run(coro):
    return asyncio.run(coro)


def _drain() -> None:
    while _SPAWNED:
        _run(_SPAWNED.pop(0))


@pytest.fixture(autouse=True)
def _env_flags(monkeypatch):
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    monkeypatch.setenv("BOWHEAD_CONTENT_SHARING", "1")
    reset_content_sharing_cache()
    _SPAWNED.clear()
    monkeypatch.setattr(ar, "_spawn", _SPAWNED.append)
    yield
    for coro in _SPAWNED:
        coro.close()
    _SPAWNED.clear()
    reset_content_sharing_cache()


class _Env:
    def __init__(self, tmp_path):
        db = str(tmp_path / "s.db")
        self.users = UserStore(db)
        _run(self.users.initialize())
        self.access = AccessControlStore(db)
        _run(self.access.initialize())
        self.canvas = CanvasStore(db_path=str(tmp_path / "c.db"))
        _run(self.canvas.initialize())
        self.acr = AccessRequestStore(str(tmp_path / "acr.db"))
        _run(self.acr.initialize())
        self.notif = NotificationStore(str(tmp_path / "n.db"))
        _run(self.notif.initialize())
        self.nsvc = NotificationService(store=self.notif, user_store=self.users)
        self.team = _run(self.users.create_team(name="Acme", max_seats=10))
        self.alice = _run(self.users.create_user(team_id=self.team.id, email="a@x.com", password="Passw0rd!"))
        self.bob = _run(self.users.create_user(team_id=self.team.id, email="b@x.com", password="Passw0rd!"))
        self.ws_id = _run(self.users.sync_workspace_from_datarc(team_id=self.team.id, datarc_workspace_id="336", name="WS"))
        # Synced spaces lack member rows; production owners get them on initial join or member-add. Owners need membership for report reads/management; Bob stays a nonmember recipient.
        _run(self.users.add_member(workspace_id=self.ws_id, user_id=self.alice.id, role="admin"))
        _run(self.canvas.ensure_canvas(session_id="ses_1", workspace_id=self.ws_id, user_id=self.alice.id))

    def app_obj(self):
        return SimpleNamespace(
            state=SimpleNamespace(
                user_store=self.users,
                access_control_store=self.access,
                canvas_store=self.canvas,
                access_request_store=self.acr,
                notification_service=self.nsvc,
            )
        )

    def client(self, user) -> TestClient:
        app = FastAPI()
        app.include_router(ar.router, prefix="/api/v1/access-requests")
        app.state.user_store = self.users
        app.state.access_control_store = self.access
        app.state.canvas_store = self.canvas
        app.state.access_request_store = self.acr
        app.state.notification_service = self.nsvc
        app.dependency_overrides[ar._current_user] = lambda: user
        return TestClient(app)

    def _enqueue(self, requester, rid, msg="please"):
        response = self.client(requester).post("/api/v1/access-requests", json={"kind": "report", "resource_id": rid, "message": msg})
        assert response.status_code == 202
        _drain()
        return response

    def _pending(self, approver, is_admin=False):
        return len(_run(self.acr.list_for_approver(team_id=self.team.id, user_id=approver.id, is_admin=is_admin, scope="pending")))


def _first_pending_id(env, approver):
    items = env.client(approver).get("/api/v1/access-requests?scope=pending").json()["items"]
    return items, (items[0]["id"] if items else None)


def test_list_full_identity_and_approve_grants_access(tmp_path):
    from bowhead_core.share_service import grants_for_viewer

    env = _Env(tmp_path)
    env._enqueue(env.bob, "ses_1", msg="想看 Q4")
    a = env.client(env.alice)
    items, rid = _first_pending_id(env, env.alice)
    assert len(items) == 1
    # approver sees FULL identity + resource label + the message (no masking on this side)
    assert items[0]["requester_email"] == "b@x.com" and items[0]["label"] and items[0]["message"] == "想看 Q4"
    # approve → grant created, requester notified, owner pending cleared
    r = a.post(f"/api/v1/access-requests/{rid}/decide", json={"action": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    granted = _run(grants_for_viewer(env.access, team_id=env.team.id, user_id=env.bob.id, kind="report"))
    assert "ses_1" in granted
    assert _run(env.notif.notification_count(user_id=env.bob.id)).updates_unread >= 1
    assert env._pending(env.alice) == 0
    # idempotent: re-decide on the now-approved row is a no-op
    assert a.post(f"/api/v1/access-requests/{rid}/decide", json={"action": "deny"}).json()["status"] == "approved"


def test_decide_deny_no_grant(tmp_path):
    from bowhead_core.share_service import grants_for_viewer

    env = _Env(tmp_path)
    env._enqueue(env.bob, "ses_1")
    _items, rid = _first_pending_id(env, env.alice)
    r = env.client(env.alice).post(f"/api/v1/access-requests/{rid}/decide", json={"action": "deny"})
    assert r.status_code == 200 and r.json()["status"] == "denied"
    granted = _run(grants_for_viewer(env.access, team_id=env.team.id, user_id=env.bob.id, kind="report"))
    assert "ses_1" not in granted
    # denied → bob is in the cooldown; nothing new reaches the owner
    again = env.client(env.bob).post("/api/v1/access-requests", json={"kind": "report", "resource_id": "ses_1"})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "access_request_cooldown"
    assert env._pending(env.alice) == 0


def test_non_approver_and_admin_fallback(tmp_path):
    env = _Env(tmp_path)
    env._enqueue(env.bob, "ses_1")
    _items, rid = _first_pending_id(env, env.alice)
    # requester (bob) cannot decide their own request → 404 (no existence leak)
    assert env.client(env.bob).post(f"/api/v1/access-requests/{rid}/decide", json={"action": "approve"}).status_code == 404
    # random member (carol, not owner/admin) → 404
    carol = _run(env.users.create_user(team_id=env.team.id, email="c@x.com", password="Passw0rd!"))
    assert env.client(carol).post(f"/api/v1/access-requests/{rid}/decide", json={"action": "approve"}).status_code == 404
    # carol sees nothing in her own approver list
    assert env.client(carol).get("/api/v1/access-requests?scope=pending").json()["items"] == []


def test_out_of_band_grant_resolves_pending(tmp_path):
    env = _Env(tmp_path)
    env._enqueue(env.bob, "ses_1")
    assert env._pending(env.alice) == 1
    assert _run(env.notif.notification_count(user_id=env.alice.id)).action_required == 1
    # a grant arrives out-of-band (direct share / admin console) → hook resolves it
    _run(
        ar.resolve_request_after_grant(
            env.app_obj(), team_id=env.team.id, kind="report", resource_id="ses_1", requester_id=env.bob.id, decided_by=env.alice.id
        )
    )
    assert env._pending(env.alice) == 0
    assert _run(env.notif.notification_count(user_id=env.alice.id)).action_required == 0  # owner cleared
    assert _run(env.notif.notification_count(user_id=env.bob.id)).updates_unread >= 1  # Requester notified access is available.


def test_approve_grant_failure_clears_owner_nudge_and_allows_rerequest(tmp_path, monkeypatch):
    """P1 partial-failure: add_grant raises AFTER the atomic status flip. Recovery must
    (a) mark the row approve_failed, (b) clear the owner's open '待处理' nudge (else it
    dangles on a now-terminal row pointing at an empty queue), (c) 500 the approver,
    (d) leave the requester free to re-request."""
    env = _Env(tmp_path)
    env._enqueue(env.bob, "ses_1")
    assert env._pending(env.alice) == 1
    assert _run(env.notif.notification_count(user_id=env.alice.id)).action_required == 1
    _items, rid = _first_pending_id(env, env.alice)

    def _boom(*a, **k):
        raise RuntimeError("grant store down")

    real_add_grant = ar.share_service.add_grant
    monkeypatch.setattr(ar.share_service, "add_grant", _boom)
    r = env.client(env.alice).post(f"/api/v1/access-requests/{rid}/decide", json={"action": "approve"})
    assert r.status_code == 500
    # row is terminal approve_failed → gone from the pending list, and the owner nudge cleared
    assert _run(env.acr.get(rid, team_id=env.team.id)).status == "approve_failed"
    assert env._pending(env.alice) == 0
    assert _run(env.notif.notification_count(user_id=env.alice.id)).action_required == 0
    # requester is not wedged: re-request works (active-unique only covers pending)
    monkeypatch.setattr(ar.share_service, "add_grant", real_add_grant)
    env._enqueue(env.bob, "ses_1")
    assert env._pending(env.alice) == 1


def test_full_walkthrough_canvas_403_then_request_approve_then_200(tmp_path):
    """端到端用户可见链路(非 route 内部断言):被分享人打不开报告 → 申请 → owner 批 →
    报告真正可打开。把 grant 接到真实读端点(GET /canvas/by-session 的 report_unavailable
    占位契约),证明"批准"产生用户可感知的效果,而不仅是一行 grant。"""
    from bowhead_gateway.routes import canvas as cv

    env = _Env(tmp_path)

    # Actual read-endpoint app: canvas resolves viewers from X-Bowhead-User-Id (dev fallback).
    capp = FastAPI()
    capp.include_router(cv.router, prefix="/api/v1/canvas")
    capp.state.canvas_store = env.canvas
    capp.state.access_control_store = env.access
    capp.state.user_store = env.users
    cc = TestClient(capp)

    def _open_report(user):
        return cc.get("/api/v1/canvas/by-session/ses_1", headers={"X-Bowhead-User-Id": user.id})

    # Owner Alice always reads; Bob initially cannot → 403 placeholder contract {code: report_unavailable}.
    assert _open_report(env.alice).status_code == 200
    denied = _open_report(env.bob)
    assert denied.status_code == 403
    assert denied.json()["detail"] == {"code": "report_unavailable"}

    # Bob requests access: uniform 202; the background promotion is drained deterministically.
    rc = env._enqueue(env.bob, "ses_1", msg="想看下")
    assert rc.json()["status"] == "submitted" and rc.json()["request_id"]
    items, rid = _first_pending_id(env, env.alice)
    assert len(items) == 1 and items[0]["requester_email"] == "b@x.com"

    # Bob still gets 403 before approval (no side-effect leak).
    assert _open_report(env.bob).status_code == 403

    # Owner approves → Bob actually opens report (200 with sections payload).
    assert env.client(env.alice).post(f"/api/v1/access-requests/{rid}/decide", json={"action": "approve"}).json()["status"] == "approved"
    ok = _open_report(env.bob)
    assert ok.status_code == 200 and ok.json()["canvas"]["sessionId"] == "ses_1"
    # Bob receives approval notification; owner's pending count clears.
    assert _run(env.notif.notification_count(user_id=env.bob.id)).updates_unread >= 1
    assert env._pending(env.alice) == 0


def test_out_of_band_grant_via_real_share_route_resolves_pending(tmp_path):
    """Wiring regression guard: the auto-resolve hooks must fire through the REAL grant
    routes (not only when the hook fn is called directly). Drive POST /shares/.../grants
    and PUT .../audience=team_wide and assert the pending request actually flips."""
    from bowhead_gateway.routes import shares

    env = _Env(tmp_path)
    app = FastAPI()
    app.include_router(shares.router, prefix="/api/v1/shares")
    app.state.user_store = env.users
    app.state.access_control_store = env.access
    app.state.canvas_store = env.canvas
    app.state.access_request_store = env.acr
    app.state.notification_service = env.nsvc
    app.dependency_overrides[shares._current_user] = lambda: env.alice
    sc = TestClient(app)

    # direct user-grant path (shares.py create_share_grant → resolve_request_after_grant)
    env._enqueue(env.bob, "ses_1")
    assert env._pending(env.alice) == 1
    r = sc.post("/api/v1/shares/report/ses_1/grants", json={"principal_type": "user", "principal_id": env.bob.id})
    assert r.status_code == 201
    assert env._pending(env.alice) == 0  # pending auto-resolved out-of-band
    assert _run(env.notif.notification_count(user_id=env.bob.id)).updates_unread >= 1

    # team_wide cascade path (shares.py put_audience → cascade_requests_for_resource)
    carol = _run(env.users.create_user(team_id=env.team.id, email="c4@x.com", password="Passw0rd!"))
    env._enqueue(carol, "ses_1")
    assert env._pending(env.alice) == 1
    assert sc.put("/api/v1/shares/report/ses_1/audience", json={"audience": "team_wide"}).status_code == 200
    assert env._pending(env.alice) == 0  # cascade staled/approved every pending for the resource


def test_admin_deny_does_not_notify_owner_but_approve_does(tmp_path):
    """approve-only 代审通知 (PRD §4.2): an admin (≠owner) DENY must NOT emit
    access_request_admin_decided to the owner; an admin APPROVE must."""
    env = _Env(tmp_path)
    # dave = team creator → can_manage_shares any resource, and ≠ owner(alice) so the
    # admin-decided self-guard (decided_by == owner_id) doesn't short-circuit.
    dave = _run(env.users.create_user(team_id=env.team.id, email="d@x.com", password="Passw0rd!"))
    _run(env.users.set_team_creator(env.team.id, dave.id))
    dave_c = env.client(dave)

    def _owner_has_admin_decided() -> bool:
        items, _ = _run(env.notif.list_for_user(user_id=env.alice.id))
        return any(i.type == "access_request_admin_decided" for i in items)

    # Bob requests Alice's ses_1 → Dave (admin) denies → owner gets NO delegated-review notice.
    env._enqueue(env.bob, "ses_1")
    _items, rid = _first_pending_id(env, env.alice)
    assert dave_c.post(f"/api/v1/access-requests/{rid}/decide", json={"action": "deny"}).json()["status"] == "denied"
    assert not _owner_has_admin_decided()

    # Carol requests → Dave approves → NOW owner gets the delegated-review heads-up.
    carol = _run(env.users.create_user(team_id=env.team.id, email="c@x.com", password="Passw0rd!"))
    env._enqueue(carol, "ses_1")
    _items2, rid2 = _first_pending_id(env, env.alice)
    assert dave_c.post(f"/api/v1/access-requests/{rid2}/decide", json={"action": "approve"}).json()["status"] == "approved"
    assert _owner_has_admin_decided()


def test_decide_when_resource_gone_hides_request_and_clears_owner_nudge(tmp_path):
    """decide on a request whose resource has since vanished → 404 and the request becomes a
    shadow row (the requester sees what a missing resource shows), AND the owner's pending
    nudge is cleared here too (the best-effort delete cascade may have been swallowed)."""
    env = _Env(tmp_path)
    env._enqueue(env.bob, "ses_1")
    assert env._pending(env.alice) == 1
    assert _run(env.notif.notification_count(user_id=env.alice.id)).action_required == 1
    _items, rid = _first_pending_id(env, env.alice)

    # resource vanishes out from under the pending request (cascade assumed to have missed it)
    canvas = _run(env.canvas.get_canvas_by_session("ses_1"))
    _run(env.canvas.delete_canvas(canvas_id=canvas.id))

    r = env.client(env.alice).post(f"/api/v1/access-requests/{rid}/decide", json={"action": "approve"})
    assert r.status_code == 404
    row = _run(env.acr.get(rid, team_id=env.team.id))
    assert row.status == "pending" and row.shadow is True
    assert env._pending(env.alice) == 0
    assert _run(env.notif.notification_count(user_id=env.alice.id)).action_required == 0
