"""AccessRequestStore — content-permissions wave 2 (access-request + approval)."""

from __future__ import annotations

import asyncio

from bowhead_core.access_request_store import AccessRequestStore, DecisionDetails


def _run(coro):
    return asyncio.run(coro)


def _store(tmp_path) -> AccessRequestStore:
    s = AccessRequestStore(str(tmp_path / "acr.db"))
    _run(s.initialize())
    return s


def test_enqueue_once_dedupes_pending(tmp_path):
    s = _store(tmp_path)
    a, created = _run(s.enqueue_once(team_id="o1", kind="report", resource_id="s1", requester_id="u_bob", owner_id="u_alice", message="hi"))
    assert created and a.status == "pending" and a.owner_id == "u_alice"
    # second identical request → dedupe (same row, not created)
    b, created2 = _run(s.enqueue_once(team_id="o1", kind="report", resource_id="s1", requester_id="u_bob", owner_id="u_alice", message="again"))
    assert not created2 and b.id == a.id and b.message == "hi"  # keeps original
    # different requester → new row
    _, c3 = _run(s.enqueue_once(team_id="o1", kind="report", resource_id="s1", requester_id="u_carol", owner_id="u_alice", message=None))
    assert c3


def test_set_decision_once_atomic_and_team_scoped(tmp_path):
    s = _store(tmp_path)
    a, _ = _run(s.enqueue_once(team_id="o1", kind="report", resource_id="s1", requester_id="u_bob", owner_id="u_alice", message=None))
    # cross-team decide → not transitioned (team_id guard) AND no foreign-team row leaked back
    # (the returned get() is team-scoped too — defense-in-depth at the store boundary)
    row, ok = _run(s.set_decision_once(request_id=a.id, team_id="o2", status="approved", decided_by="x"))
    assert not ok and row is None
    # correct team → transitioned
    row, ok = _run(s.set_decision_once(request_id=a.id, team_id="o1", status="approved", decided_by="u_alice"))
    assert ok and row.status == "approved" and row.decided_by == "u_alice"
    # second decide → idempotent no-op
    _, ok2 = _run(s.set_decision_once(request_id=a.id, team_id="o1", status="denied", decided_by="u_alice"))
    assert not ok2
    # after approval, the active-unique no longer blocks a re-request
    _, created = _run(s.enqueue_once(team_id="o1", kind="report", resource_id="s1", requester_id="u_bob", owner_id="u_alice", message=None))
    assert created


def test_resolve_pending_for_grant(tmp_path):
    s = _store(tmp_path)
    _run(s.enqueue_once(team_id="o1", kind="report", resource_id="s1", requester_id="u_bob", owner_id="u_alice", message=None))
    out = _run(s.resolve_pending_for_grant(team_id="o1", kind="report", resource_id="s1", requester_id="u_bob", decided_by="u_alice"))
    assert out is not None and out.status == "approved"
    # no pending → None
    assert _run(s.resolve_pending_for_grant(team_id="o1", kind="report", resource_id="s1", requester_id="u_zzz", decided_by="u_alice")) is None


def test_stale_pending_for_resource(tmp_path):
    s = _store(tmp_path)
    _run(s.enqueue_once(team_id="o1", kind="report", resource_id="s1", requester_id="u_bob", owner_id="u_alice", message=None))
    _run(s.enqueue_once(team_id="o1", kind="report", resource_id="s1", requester_id="u_carol", owner_id="u_alice", message=None))
    rows = _run(s.stale_pending_for_resource(team_id="o1", kind="report", resource_id="s1"))
    assert {r.requester_id for r in rows} == {"u_bob", "u_carol"}
    # all now stale → none pending
    assert _run(s.list_for_approver(team_id="o1", user_id="u_alice", is_admin=False, scope="pending")) == []


def test_list_and_counts_owner_vs_admin(tmp_path):
    s = _store(tmp_path)
    _run(s.enqueue_once(team_id="o1", kind="report", resource_id="s1", requester_id="u_bob", owner_id="u_alice", message=None))
    _run(s.enqueue_once(team_id="o1", kind="report", resource_id="s2", requester_id="u_bob", owner_id="u_dave", message=None))
    # owner alice sees only her resource's request
    own = _run(s.list_for_approver(team_id="o1", user_id="u_alice", is_admin=False, scope="pending"))
    assert [r.resource_id for r in own] == ["s1"] and len(own) == 1
    # admin sees all team pending
    adm = _run(s.list_for_approver(team_id="o1", user_id="u_admin", is_admin=True, scope="pending"))
    assert {r.resource_id for r in adm} == {"s1", "s2"} and len(adm) == 2
    # batch count per resource
    counts = _run(s.count_pending_for_resources(team_id="o1", kind="report", resource_ids=["s1", "s2", "s3"]))
    assert counts == {"s1": 1, "s2": 1}


def test_prior_denied_count(tmp_path):
    s = _store(tmp_path)
    a, _ = _run(s.enqueue_once(team_id="o1", kind="report", resource_id="s1", requester_id="u_bob", owner_id="u_alice", message=None))
    _run(s.set_decision_once(request_id=a.id, team_id="o1", status="denied", decided_by="u_alice"))
    assert _run(s.prior_denied_count(team_id="o1", kind="report", resource_id="s1", requester_id="u_bob")) == 1
    assert _run(s.prior_denied_count(team_id="o1", kind="report", resource_id="s1", requester_id="u_x")) == 0


# ── lifecycle ────────────────────────────────────────────────────────────────


def _enqueue(s, requester="u_bob", rid="s1", *, kind="report", owner="u_alice", shadow=False, expires_at="2026-10-14T00:00:00+00:00"):
    row, _ = _run(
        s.enqueue_once(
            team_id="o1", kind=kind, resource_id=rid, requester_id=requester, owner_id=owner, message=None, shadow=shadow, expires_at=expires_at
        )
    )
    return row


def test_approver_reads_exclude_shadow_rows_and_admins_do_not_see_agent_requests(tmp_path):
    s = _store(tmp_path)
    _enqueue(s, rid="s1")
    _enqueue(s, requester="u_carol", rid="s1", owner=None, shadow=True)
    _enqueue(s, rid="osa_1", kind="team_subagent")
    owner_rows = _run(s.list_for_approver(team_id="o1", user_id="u_alice", is_admin=False, scope="pending"))
    assert {(r.kind, r.resource_id) for r in owner_rows} == {("report", "s1"), ("team_subagent", "osa_1")}
    admin_rows = _run(s.list_for_approver(team_id="o1", user_id="u_admin", is_admin=True, scope="pending"))
    assert [(r.kind, r.requester_id) for r in admin_rows] == [("report", "u_bob")]
    assert _run(s.count_pending_for_resources(team_id="o1", kind="report", resource_ids=["s1"])) == {"s1": 1}


def test_withdraw_once_is_requester_scoped(tmp_path):
    s = _store(tmp_path)
    a = _enqueue(s)
    assert _run(s.withdraw_once(request_id=a.id, team_id="o1", requester_id="u_carol")) == (None, False)
    row, ok = _run(s.withdraw_once(request_id=a.id, team_id="o1", requester_id="u_bob"))
    assert ok and row.status == "withdrawn" and row.withdrawn_at
    assert _run(s.withdraw_once(request_id=a.id, team_id="o1", requester_id="u_bob"))[1] is False


def test_expire_due_only_touches_pending_rows_with_an_expiry(tmp_path):
    s = _store(tmp_path)
    due = _enqueue(s, rid="s1", expires_at="2026-10-01T00:00:00+00:00")
    later = _enqueue(s, rid="s2", expires_at="2026-11-01T00:00:00+00:00")
    legacy, _ = _run(s.enqueue_once(team_id="o1", kind="report", resource_id="s3", requester_id="u_bob", owner_id="u_alice", message=None))
    expired = _run(s.expire_due(now="2026-10-15T00:00:00+00:00"))
    assert [r.id for r in expired] == [due.id]
    assert _run(s.get(later.id)).status == "pending" and _run(s.get(legacy.id)).status == "pending"


def test_cooldown_comes_from_the_latest_real_denial(tmp_path):
    s = _store(tmp_path)
    a = _enqueue(s)
    details = DecisionDetails(note="no", cooldown_until="2026-10-07T00:00:00+00:00")
    _run(s.set_decision_once(request_id=a.id, team_id="o1", status="denied", decided_by="u_alice", decision=details))
    kwargs = {"team_id": "o1", "kind": "report", "resource_id": "s1", "requester_id": "u_bob"}
    assert _run(s.active_cooldown_until(**kwargs, now="2026-10-01T00:00:00+00:00")) == "2026-10-07T00:00:00+00:00"
    assert _run(s.active_cooldown_until(**kwargs, now="2026-10-08T00:00:00+00:00")) is None
    assert _run(s.get(a.id)).decision_note == "no"


def test_shadow_rows_cannot_be_decided_or_resolved_out_of_band(tmp_path):
    s = _store(tmp_path)
    shadow = _enqueue(s, owner=None, shadow=True)
    _, ok = _run(s.set_decision_once(request_id=shadow.id, team_id="o1", status="approved", decided_by="x", decision=DecisionDetails()))
    assert not ok
    assert _run(s.resolve_pending_for_grant(team_id="o1", kind="report", resource_id="s1", requester_id="u_bob", decided_by="x")) is None
    assert _run(s.stale_pending_for_resource(team_id="o1", kind="report", resource_id="s1")) == []
    assert _run(s.get(shadow.id)).status == "pending"


def test_void_pending_for_requester_is_team_scoped(tmp_path):
    s = _store(tmp_path)
    mine = _enqueue(s)
    other = _enqueue(s, requester="u_carol")
    assert _run(s.void_pending_for_requester(requester_id="u_bob", team_id="o2")) == []
    voided = _run(s.void_pending_for_requester(requester_id="u_bob", team_id="o1"))
    assert [r.id for r in voided] == [mine.id]
    assert _run(s.get(other.id)).status == "pending"
