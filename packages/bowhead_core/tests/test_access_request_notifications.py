"""access-request notifications (content-permissions wave 2, S2)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import ACTION_REQUIRED_TYPES, NotificationStore


def _run(coro):
    return asyncio.run(coro)


class _Users:
    async def list_team_users_by_roles(self, team_id, roles, *, include_creator=True):
        return ["u_admin"]

    async def get_user(self, uid):
        return SimpleNamespace(display_name={"u_bob": "Bob", "u_admin": "Adm"}.get(uid, uid), email=f"{uid}@x.com")


def _svc(tmp_path):
    store = NotificationStore(str(tmp_path / "n.db"))
    _run(store.initialize())
    return NotificationService(store=store, user_store=_Users()), store


def _req(rid="acr_1", status="pending", decided_by=None):
    return SimpleNamespace(
        id=rid, team_id="o1", kind="report", resource_id="s1", requester_id="u_bob", owner_id="u_alice", status=status, decided_by=decided_by
    )


def _res():
    return SimpleNamespace(kind="report", resource_id="s1", owner_id="u_alice", label="Q4 报告")


def test_access_requested_reaches_routed_approvers_as_action_required(tmp_path):
    svc, store = _svc(tmp_path)
    item = _run(svc.emit_access_requested(_req(), _res(), recipients={"u_alice", "u_admin"}))
    assert item is not None and item.type == "access_requested"
    assert "access_requested" in ACTION_REQUIRED_TYPES  # Drives the badge + "待处理" tab.
    assert "Q4 报告" in (item.body or "")  # owner/admin side: label OK
    # every routed approver gets an action-required item
    assert _run(store.notification_count(user_id="u_alice")).action_required >= 1
    assert _run(store.notification_count(user_id="u_admin")).action_required >= 1
    # requester is NOT a recipient of the request notification
    assert _run(store.notification_count(user_id="u_bob")).action_required == 0


def test_access_decided_requester_no_label(tmp_path):
    svc, store = _svc(tmp_path)
    item = _run(svc.emit_access_decided(_req(status="approved", decided_by="u_alice"), approved=True, action_url="/?session=s1"))
    assert item.type == "access_request_decided"
    assert item.body is None  # anti-fishing: no resource label
    assert item.action_url == "/?session=s1"  # direct link in
    # decided notification is NOT action-required (informational)
    assert _run(store.notification_count(user_id="u_bob")).action_required == 0


def test_resolve_access_requested_clears_owner(tmp_path):
    svc, store = _svc(tmp_path)
    _run(svc.emit_access_requested(_req("acr_3"), _res(), recipients={"u_alice"}))
    assert _run(store.notification_count(user_id="u_alice")).action_required == 1
    resolved = _run(svc.resolve_access_requested("o1", "acr_3", reason="approved"))
    assert resolved is not None
    assert _run(store.notification_count(user_id="u_alice")).action_required == 0


def test_admin_decided_notifies_owner(tmp_path):
    svc, store = _svc(tmp_path)
    # admin (not owner) approved → owner gets a heads-up (informational → updates_unread)
    _run(svc.emit_access_admin_decided(_req(status="approved", decided_by="u_admin"), "u_alice", "Q4 报告"))
    items, _cursor = _run(store.list_for_user(user_id="u_alice", tab="all"))
    assert any(i.type == "access_request_admin_decided" for i in items)
    # if the decider IS the owner, no admin-decided notice
    before = _run(store.notification_count(user_id="u_alice")).updates_unread
    _run(svc.emit_access_admin_decided(_req("acr_9", status="approved", decided_by="u_alice"), "u_alice", "X"))
    assert _run(store.notification_count(user_id="u_alice")).updates_unread == before
