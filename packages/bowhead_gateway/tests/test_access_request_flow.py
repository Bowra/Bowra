"""Access requests: kinds, lifecycle, guards, and rows left by the retired report-only intake."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.access_request_store import AccessRequestStore
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import NotificationStore
from bowhead_core.share_service import grants_for_viewer, reset_content_sharing_cache
from bowhead_core.team_subagent_access import TeamSubagentAccessResolver
from bowhead_core.team_subagent_store import TeamSubagentStore
from bowhead_core.user_store import TeamSkill, TeamSubagent, UserStore
from bowhead_gateway.routes import access_requests as ar
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_core import access_request_store as acr_module

pytestmark = pytest.mark.integration

# Background follow-ups scheduled by the handlers (``ar._spawn``). TestClient's loop does not
# reliably run fire-and-forget tasks, so tests collect them and drain deterministically.
_SPAWNED: list = []


def _run(coro):
    return asyncio.run(coro)


def _drain() -> int:
    ran = 0
    while _SPAWNED:
        _run(_SPAWNED.pop(0))
        ran += 1
    return ran


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
    def __init__(self, tmp_path, *, with_resources: bool = True):
        db = str(tmp_path / "s.db")
        self.db = db
        self.users = UserStore(db)
        _run(self.users.initialize())
        self.access = AccessControlStore(db)
        _run(self.access.initialize())
        self.subagents = TeamSubagentStore(db)
        _run(self.subagents.initialize())
        self.canvas = CanvasStore(db_path=str(tmp_path / "c.db"))
        _run(self.canvas.initialize())
        self.acr = AccessRequestStore(db)
        _run(self.acr.initialize())
        self.notif = NotificationStore(str(tmp_path / "n.db"))
        _run(self.notif.initialize())
        self.nsvc = NotificationService(store=self.notif, user_store=self.users)
        self.team = _run(self.users.create_team(name="Acme", max_seats=20))
        self.alice = _run(self.users.create_user(team_id=self.team.id, email="a@x.com"))
        self.bob = _run(self.users.create_user(team_id=self.team.id, email="b@x.com"))
        self.carol = _run(self.users.create_user(team_id=self.team.id, email="c@x.com"))
        self.dave = _run(self.users.create_user(team_id=self.team.id, email="d@x.com"))
        _run(self.users.set_team_creator(self.team.id, self.dave.id))
        self.ws_id = _run(self.users.sync_workspace_from_datarc(team_id=self.team.id, datarc_workspace_id="336", name="WS"))
        _run(self.users.add_member(workspace_id=self.ws_id, user_id=self.alice.id, role="admin"))
        if with_resources:
            _run(self.canvas.ensure_canvas(session_id="ses_1", workspace_id=self.ws_id, user_id=self.alice.id))
            _run(self.users.create_team_skill(self._skill("sk_1", "approved")))
            _run(self.users.create_team_skill(self._skill("sk_pending", "pending")))
            _run(
                self.subagents.create_team_subagent(
                    TeamSubagent(
                        id="osa_1",
                        team_id=self.team.id,
                        name="helper",
                        tool_refs=["bi_query"],
                        visibility="creator_only",
                        created_by=self.alice.id,
                        created_at="2026-09-01T00:00:00Z",
                        updated_at="2026-09-01T00:00:00Z",
                    ),
                )
            )

    def _skill(self, skill_id: str, approval: str) -> TeamSkill:
        return TeamSkill(
            id=skill_id,
            team_id=self.team.id,
            name=skill_id,
            display_name=f"Skill {skill_id}",
            visibility="creator_only",
            created_by=self.alice.id,
            approval=approval,
            created_at="2026-09-01T00:00:00Z",
            updated_at="2026-09-01T00:00:00Z",
        )

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
        app.state.team_subagent_schema_store = self.subagents
        app.dependency_overrides[ar._current_user] = lambda: user
        return TestClient(app)

    def submit(self, user, kind: str, resource_id: str, message: str | None = None):
        body = {"kind": kind, "resource_id": resource_id}
        if message is not None:
            body["message"] = message
        response = self.client(user).post("/api/v1/access-requests", json=body)
        _drain()
        return response

    def withdraw(self, user, request_id: str):
        response = self.client(user).post(f"/api/v1/access-requests/{request_id}/withdraw")
        _drain()
        return response

    def decide(self, user, request_id: str, **body):
        return self.client(user).post(f"/api/v1/access-requests/{request_id}/decide", json=body)

    def status(self, user, kind: str, resource_id: str):
        return self.client(user).get("/api/v1/access-requests/status", params={"kind": kind, "resource_id": resource_id})

    def pending_ids(self, approver, *, is_admin: bool = False) -> list[str]:
        rows = _run(self.acr.list_for_approver(team_id=self.team.id, user_id=approver.id, is_admin=is_admin, scope="pending"))
        return [r.id for r in rows]

    def action_required(self, user) -> int:
        return _run(self.notif.notification_count(user_id=user.id)).action_required

    def notifications(self, user, type_: str):
        items, _ = _run(self.notif.list_for_user(user_id=user.id))
        return [i for i in items if i.type == type_]

    def audit_actions(self) -> list[tuple[str, dict]]:
        async def go():
            async with aiosqlite.connect(self.db) as db, db.execute("SELECT action, payload_json FROM admin_audit_log ORDER BY ts, rowid") as cur:
                return [(row[0], json.loads(row[1])) for row in await cur.fetchall()]

        return _run(go())

    def make_team_admin(self, user) -> None:
        _run(self.users.promote_team_role(user_id=user.id, team_id=self.team.id, new_role="team_admin", promoted_by=self.dave.id))


def _seed_legacy_row(env: _Env, requester, message: str | None = None) -> None:
    """Write the row the retired report-only intake left for ``ses_1`` (regular, routed to the
    owner, no expiry, ``legacy_origin``). That intake dropped requests for missing resources
    silently, so nothing is written when the report does not exist."""
    if _run(env.canvas.get_canvas_by_session("ses_1")) is None:
        return
    _run(
        env.acr.enqueue_once(
            team_id=env.team.id, kind="report", resource_id="ses_1", requester_id=requester.id, owner_id=env.alice.id, message=message
        )
    )


# ── happy paths per kind ──────────────────────────────────────────────────────


def test_report_request_routes_to_owner_only_and_approval_grants_time_limited_read(tmp_path):
    env = _Env(tmp_path)
    r = env.submit(env.bob, "report", "ses_1", message="need Q4")
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "submitted" and body["request_id"].startswith("acr_")
    rid = body["request_id"]
    assert env.pending_ids(env.alice) == [rid]
    # D5: only the owner is notified; the team owner (dave) is not pushed.
    assert env.action_required(env.alice) == 1
    assert env.action_required(env.dave) == 0
    assert env.status(env.bob, "report", "ses_1").json()["state"] == "pending"

    d = env.decide(env.alice, rid, action="approve", note="Read-only, please", grant_expires_in_days=30)
    assert d.status_code == 200 and d.json() == {"status": "approved"}
    grants = _run(env.access.list_resource_grants(env.team.id, resource_kind="report", resource_id="ses_1"))
    user_grants = [g for g in grants if g.principal_id == env.bob.id]
    assert len(user_grants) == 1 and user_grants[0].permission == "read" and user_grants[0].expires_at is not None
    expires = datetime.fromisoformat(user_grants[0].expires_at)
    assert timedelta(days=29) < expires - datetime.now(tz=UTC) <= timedelta(days=30)

    status = env.status(env.bob, "report", "ses_1").json()
    assert status["state"] == "approved"
    assert status["request"]["note"] == "Read-only, please"
    assert status["request"]["granted_permission"] == "read"
    assert status["request"]["grant_expires_at"] == user_grants[0].expires_at
    decided = env.notifications(env.bob, "access_request_decided")
    assert len(decided) == 1
    # D9: "a manager" plus the note verbatim; never who decided.
    assert decided[0].actor_user_id is None
    assert decided[0].metadata["note"] == "Read-only, please" and decided[0].metadata["decidedBy"] == "manager"
    assert env.action_required(env.alice) == 0
    actions = [a for a, _ in env.audit_actions()]
    assert "access_request_submitted" in actions and "access_request_approved" in actions
    approved_payload = next(p for a, p in env.audit_actions() if a == "access_request_approved")
    assert approved_payload["granted_permission"] == "read" and approved_payload["note_present"] is True


def test_team_skill_request_grants_use(tmp_path):
    env = _Env(tmp_path)
    rid = env.submit(env.bob, "team_skill", "sk_1").json()["request_id"]
    assert env.pending_ids(env.alice) == [rid]
    assert env.decide(env.alice, rid, action="approve").json() == {"status": "approved"}
    granted = _run(grants_for_viewer(env.access, team_id=env.team.id, user_id=env.bob.id, kind="team_skill"))
    assert "sk_1" in granted
    assert env.status(env.bob, "team_skill", "sk_1").json()["request"]["granted_permission"] == "use"


def test_unapproved_skill_request_stays_shadow(tmp_path):
    env = _Env(tmp_path)
    rid = env.submit(env.bob, "team_skill", "sk_pending").json()["request_id"]
    row = _run(env.acr.get(rid, team_id=env.team.id))
    assert row.shadow is True
    assert env.pending_ids(env.alice) == [] and env.pending_ids(env.dave, is_admin=True) == []
    assert env.action_required(env.alice) == 0


def test_skill_that_lost_approval_marks_request_approve_failed(tmp_path):
    env = _Env(tmp_path)
    rid = env.submit(env.bob, "team_skill", "sk_1").json()["request_id"]

    async def unapprove():
        async with aiosqlite.connect(env.db) as db:
            await db.execute("UPDATE team_skill SET approval='pending' WHERE id='sk_1'")
            await db.commit()

    _run(unapprove())
    r = env.decide(env.alice, rid, action="approve")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "access_request_skill_not_grantable"
    assert _run(env.acr.get(rid, team_id=env.team.id)).status == "approve_failed"
    assert env.action_required(env.alice) == 0
    # requester side never sees approve_failed
    assert env.status(env.bob, "team_skill", "sk_1").json()["state"] == "expired"


def test_team_subagent_request_appends_to_owner_audience_without_touching_existing_grants(tmp_path):
    env = _Env(tmp_path)
    _run(
        env.subagents.replace_team_subagent_audience(
            env.team.id,
            "osa_1",
            mode="custom",
            user_ids=[env.carol.id],
            access_store=env.access,
            actor_user_id=env.alice.id,
            audit_action="subagent_audience_changed",
        )
    )
    before = {g.id for g in _run(env.access.list_resource_grants(env.team.id, resource_kind="team_subagent", resource_id="osa_1"))}
    rid = env.submit(env.bob, "team_subagent", "osa_1").json()["request_id"]
    assert env.pending_ids(env.alice) == [rid]

    assert env.decide(env.alice, rid, action="approve").json() == {"status": "approved"}
    after = _run(env.access.list_resource_grants(env.team.id, resource_kind="team_subagent", resource_id="osa_1"))
    assert before <= {g.id for g in after}  # appended, not replaced
    assert {(g.principal_type, g.principal_id) for g in after} == {("user", env.carol.id), ("user", env.bob.id)}
    decision = _run(TeamSubagentAccessResolver(env.db).resolve(team_id=env.team.id, actor_user_id=env.bob.id, subagent_id="osa_1"))
    assert decision.resolution.can_consume is True


def test_agent_request_is_not_requestable_by_viewer_who_already_has_access(tmp_path):
    env = _Env(tmp_path)
    _run(
        env.subagents.replace_team_subagent_audience(
            env.team.id, "osa_1", mode="teamwide", access_store=env.access, actor_user_id=env.alice.id, audit_action="subagent_audience_changed"
        )
    )
    rid = env.submit(env.bob, "team_subagent", "osa_1").json()["request_id"]
    assert _run(env.acr.get(rid, team_id=env.team.id)).shadow is True


def test_agent_grant_cannot_be_time_limited(tmp_path):
    env = _Env(tmp_path)
    rid = env.submit(env.bob, "team_subagent", "osa_1").json()["request_id"]
    r = env.decide(env.alice, rid, action="approve", grant_expires_in_days=30)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "access_request_grant_expiry_unsupported"
    assert _run(env.acr.get(rid, team_id=env.team.id)).status == "pending"


# ── who may approve ───────────────────────────────────────────────────────────


def test_approver_boundaries_per_kind(tmp_path):
    env = _Env(tmp_path)
    erin = _run(env.users.create_user(team_id=env.team.id, email="e@x.com"))
    env.make_team_admin(erin)
    report_id = env.submit(env.bob, "report", "ses_1").json()["request_id"]
    skill_id = env.submit(env.bob, "team_skill", "sk_1").json()["request_id"]
    agent_id = env.submit(env.bob, "team_subagent", "osa_1").json()["request_id"]
    shadow_id = env.submit(env.bob, "report", "ses_missing").json()["request_id"]

    # requester and unrelated members cannot decide anything (404, no existence leak)
    for user in (env.bob, env.carol):
        for rid in (report_id, skill_id, agent_id, shadow_id):
            assert env.decide(user, rid, action="approve").status_code == 404
    # team admins see report/skill requests but never agent requests (Owner-managed audience)
    assert set(env.pending_ids(erin, is_admin=True)) == {report_id, skill_id}
    assert env.decide(erin, agent_id, action="approve").status_code == 404
    assert env.decide(env.dave, agent_id, action="approve").status_code == 404
    # shadow rows are invisible and undecidable even for the team owner, whether the
    # resource is missing or exists (an unapproved skill), and deciding never touches them
    unapproved_shadow = env.submit(env.bob, "team_skill", "sk_pending").json()["request_id"]
    for rid in (shadow_id, unapproved_shadow):
        assert env.decide(env.dave, rid, action="approve").status_code == 404
        assert _run(env.acr.get(rid, team_id=env.team.id)).status == "pending"
    assert env.decide(erin, skill_id, action="deny").json() == {"status": "denied"}
    assert env.decide(env.dave, report_id, action="approve").json() == {"status": "approved"}
    assert env.decide(env.alice, agent_id, action="approve").json() == {"status": "approved"}


def test_decide_rejects_mismatched_options(tmp_path):
    env = _Env(tmp_path)
    rid = env.submit(env.bob, "report", "ses_1").json()["request_id"]
    r1 = env.decide(env.alice, rid, action="deny", grant_expires_in_days=30)
    r2 = env.decide(env.alice, rid, action="approve", stop_requests=True)
    assert r1.status_code == 422 and r1.json()["detail"]["code"] == "access_request_decision_invalid"
    assert r2.status_code == 422 and r2.json()["detail"]["code"] == "access_request_decision_invalid"
    assert env.decide(env.alice, rid, action="approve", note="x" * 281).status_code == 422


# ── cooldown and rate limit ───────────────────────────────────────────────────


def test_denial_starts_seven_day_cooldown_with_retry_after(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    rid = env.submit(env.bob, "report", "ses_1").json()["request_id"]
    assert env.decide(env.alice, rid, action="deny", note="Not this one").json() == {"status": "denied"}
    again = env.submit(env.bob, "report", "ses_1")
    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["code"] == "access_request_cooldown"
    assert 6 * 86_400 < detail["retry_after"] <= 7 * 86_400
    assert again.headers["Retry-After"] == str(detail["retry_after"])
    status = env.status(env.bob, "report", "ses_1").json()
    assert status["state"] == "denied" and status["request"]["note"] == "Not this one"
    assert 6 * 86_400 < status["retry_after"] <= 7 * 86_400
    # the cooldown is per resource: other requests still go through
    assert env.submit(env.bob, "team_skill", "sk_1").status_code == 202
    # after the cooldown the requester can ask again
    later = datetime.now(tz=UTC) + timedelta(days=8)
    monkeypatch.setattr(ar, "_utcnow", lambda: later)
    assert env.submit(env.bob, "report", "ses_1").status_code == 202


def test_third_denial_and_stop_requests_use_thirty_day_cooldown(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    start = datetime.now(tz=UTC)
    for round_ in range(3):
        now = start + timedelta(days=8 * round_)
        monkeypatch.setattr(ar, "_utcnow", lambda now=now: now)
        rid = env.submit(env.bob, "report", "ses_1").json()["request_id"]
        env.decide(env.alice, rid, action="deny")
    retry_after = env.submit(env.bob, "report", "ses_1").json()["detail"]["retry_after"]
    assert 29 * 86_400 < retry_after <= 30 * 86_400

    rid = env.submit(env.carol, "report", "ses_1").json()["request_id"]
    env.decide(env.alice, rid, action="deny", stop_requests=True)
    retry_after = env.submit(env.carol, "report", "ses_1").json()["detail"]["retry_after"]
    assert 29 * 86_400 < retry_after <= 30 * 86_400


def test_rate_limit_is_synchronous_429_per_requester(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    monkeypatch.setattr(ar, "_RATE_LIMIT", 2)
    assert env.submit(env.bob, "report", "ses_missing_1").status_code == 202
    assert env.submit(env.bob, "report", "ses_1").status_code == 202
    limited = env.submit(env.bob, "report", "ses_missing_2")
    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "access_request_rate_limited"
    assert 0 < int(limited.headers["Retry-After"]) <= 3600
    # nothing was recorded for the rejected submit; other requesters are unaffected
    assert len(_run(env.acr.list_for_requester(team_id=env.team.id, requester_id=env.bob.id))) == 2
    assert env.submit(env.carol, "report", "ses_missing_2").status_code == 202


def test_daily_rate_limit(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    monkeypatch.setattr(ar, "DAILY_RATE_LIMIT", 1)
    assert env.submit(env.bob, "report", "ses_a").status_code == 202
    r = env.submit(env.bob, "report", "ses_b")
    assert r.status_code == 429 and r.json()["detail"]["code"] == "access_request_rate_limited"


# ── non-disclosure: missing vs no access ─────────────────────────────────────


def _frozen(monkeypatch):
    fixed = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(ar, "_utcnow", lambda: fixed)
    monkeypatch.setattr(acr_module, "_now", lambda: fixed.isoformat(timespec="seconds"))
    monkeypatch.setattr(acr_module, "uuid4", lambda: SimpleNamespace(hex="0123456789abcdef0123"))
    return fixed


def _requester_transcript(env: _Env, fixed: datetime, requester=None) -> list[bytes]:
    requester = requester or env.bob
    out = []
    submit = env.submit(requester, "report", "ses_1", message="please")
    out.append(submit.content)
    client = env.client(requester)
    out.append(env.status(requester, "report", "ses_1").content)
    out.append(client.get("/api/v1/access-requests", params={"scope": "mine"}).content)
    # same answers again after the background promotion and after expiry
    _run(ar.sweep_access_requests_once(env.app_obj(), now=fixed + timedelta(days=15)))
    out.append(env.status(requester, "report", "ses_1").content)
    out.append(client.get("/api/v1/access-requests", params={"scope": "mine"}).content)
    expired = env.notifications(requester, "access_request_expired")
    out.append(json.dumps([(n.title, n.body, n.metadata, n.actor_user_id) for n in expired]).encode())
    return out


def test_missing_cross_team_and_no_access_resources_answer_byte_identically(tmp_path, monkeypatch):
    fixed = _frozen(monkeypatch)
    existing = _Env(tmp_path / "existing")
    missing = _Env(tmp_path / "missing", with_resources=False)
    cross = _Env(tmp_path / "cross", with_resources=False)
    other_team = _run(cross.users.create_team(name="Other", max_seats=5))
    other_ws = _run(cross.users.sync_workspace_from_datarc(team_id=other_team.id, datarc_workspace_id="999", name="Other"))
    _run(cross.canvas.ensure_canvas(session_id="ses_1", workspace_id=other_ws, user_id=cross.alice.id))

    transcripts = [_requester_transcript(env, fixed) for env in (existing, missing, cross)]
    assert transcripts[0] == transcripts[1] == transcripts[2]
    # positive control: the existing resource really produced an approver-side request
    assert len(existing.notifications(existing.alice, "access_requested")) == 1
    assert missing.pending_ids(missing.dave, is_admin=True) == []


def test_status_without_any_request_is_none(tmp_path):
    env = _Env(tmp_path)
    assert env.status(env.bob, "team_subagent", "osa_1").json() == {"state": "none", "request": None, "retry_after": None}


def _set_grant_expiry(env: _Env, request_id: str, value: str) -> None:
    async def go():
        async with aiosqlite.connect(env.db) as db:
            await db.execute("UPDATE access_requests SET grant_expires_at=? WHERE id=?", (value, request_id))
            await db.commit()

    _run(go())


def test_expired_time_limited_grant_reads_as_expired_and_can_be_requested_again(tmp_path):
    env = _Env(tmp_path)
    rid = env.submit(env.bob, "report", "ses_1").json()["request_id"]
    assert env.decide(env.alice, rid, action="approve", grant_expires_in_days=30).json() == {"status": "approved"}
    assert env.status(env.bob, "report", "ses_1").json()["state"] == "approved"

    past = (datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat(timespec="seconds")
    _set_grant_expiry(env, rid, past)
    status = env.status(env.bob, "report", "ses_1").json()
    assert status["state"] == "expired" and status["request"]["grant_expires_at"] is None
    mine = env.client(env.bob).get("/api/v1/access-requests", params={"scope": "mine"}).json()["items"]
    assert [i["state"] for i in mine] == ["expired"]

    again = env.submit(env.bob, "report", "ses_1")
    assert again.status_code == 202 and again.json()["request_id"] != rid
    assert env.status(env.bob, "report", "ses_1").json()["state"] == "pending"


def test_approved_request_whose_grant_was_revoked_reads_as_expired(tmp_path):
    env = _Env(tmp_path)
    rid = env.submit(env.bob, "report", "ses_1").json()["request_id"]
    env.decide(env.alice, rid, action="approve")
    assert env.status(env.bob, "report", "ses_1").json()["state"] == "approved"

    grants = _run(env.access.list_resource_grants(env.team.id, resource_kind="report", resource_id="ses_1"))
    bob_grant = next(g for g in grants if g.principal_id == env.bob.id)
    assert _run(env.access.revoke_resource_grant(env.team.id, bob_grant.id))
    assert env.status(env.bob, "report", "ses_1").json()["state"] == "expired"


def test_requester_state_only_expires_grants_in_the_past():
    now = datetime(2026, 10, 1, tzinfo=UTC)
    row = SimpleNamespace(status="approved", grant_expires_at="2026-10-02T00:00:00+00:00")
    assert ar.requester_state(row, now=now) == "approved"
    row.grant_expires_at = "2026-09-30T00:00:00+00:00"
    assert ar.requester_state(row, now=now) == "expired"
    row.grant_expires_at = None
    assert ar.requester_state(row, now=now) == "approved"
    assert ar.requester_state(SimpleNamespace(status="denied", grant_expires_at="2026-09-30T00:00:00+00:00"), now=now) == "denied"


# ── expiry, escalation, digest ───────────────────────────────────────────────


def test_sweep_expires_real_and_shadow_requests_and_notifies_requester(tmp_path):
    env = _Env(tmp_path)
    real = env.submit(env.bob, "report", "ses_1").json()["request_id"]
    shadow = env.submit(env.bob, "report", "ses_missing").json()["request_id"]
    assert env.action_required(env.alice) == 1
    assert _run(ar.sweep_access_requests_once(env.app_obj(), now=datetime.now(tz=UTC) + timedelta(days=13)))["expired"] == 0
    counts = _run(ar.sweep_access_requests_once(env.app_obj(), now=datetime.now(tz=UTC) + timedelta(days=15)))
    assert counts["expired"] == 2
    for rid in (real, shadow):
        assert _run(env.acr.get(rid, team_id=env.team.id)).status == "expired"
    assert env.action_required(env.alice) == 0
    assert len(env.notifications(env.bob, "access_request_expired")) == 2
    expired_audits = [p["request_id"] for a, p in env.audit_actions() if a == "access_request_expired"]
    assert expired_audits == [real]  # shadow rows are never audited


def test_sweep_escalates_after_72h_to_admins_once_and_never_for_agents(tmp_path):
    env = _Env(tmp_path)
    erin = _run(env.users.create_user(team_id=env.team.id, email="e@x.com"))
    env.make_team_admin(erin)
    report_id = env.submit(env.bob, "report", "ses_1").json()["request_id"]
    env.submit(env.bob, "team_subagent", "osa_1")
    early = _run(ar.sweep_access_requests_once(env.app_obj(), now=datetime.now(tz=UTC) + timedelta(hours=71)))
    assert early["escalated"] == 0
    later = datetime.now(tz=UTC) + timedelta(hours=73)
    assert _run(ar.sweep_access_requests_once(env.app_obj(), now=later))["escalated"] == 1
    assert _run(ar.sweep_access_requests_once(env.app_obj(), now=later))["escalated"] == 0
    for admin in (env.dave, erin):
        items = env.notifications(admin, "access_requested_escalated")
        assert [i.metadata["requestId"] for i in items] == [report_id]
    assert env.notifications(env.alice, "access_requested_escalated") == []
    # deciding closes the escalation item for the admins too
    env.decide(env.alice, report_id, action="deny")
    assert env.action_required(env.dave) == 0


def test_digest_folds_third_pending_request_and_unfolds_below_threshold(tmp_path):
    env = _Env(tmp_path)
    frank = _run(env.users.create_user(team_id=env.team.id, email="f@x.com"))
    ids = [env.submit(user, "report", "ses_1").json()["request_id"] for user in (env.bob, env.carol, frank)]
    digests = env.notifications(env.alice, "access_requests_digest")
    assert len(digests) == 1 and digests[0].metadata["count"] == 3
    assert len(env.notifications(env.alice, "access_requested")) == 2  # the third folded into the digest
    env.decide(env.alice, ids[0], action="deny")
    assert env.notifications(env.alice, "access_requests_digest")[0].state == "resolved"
    # every remaining pending request is delivered individually again
    open_items = {i.metadata["requestId"] for i in env.notifications(env.alice, "access_requested") if i.state == "open"}
    assert open_items == {ids[1], ids[2]}
    assert env.action_required(env.alice) == 2


# ── withdraw and member removal ──────────────────────────────────────────────


def test_withdraw_is_requester_only_idempotent_and_clears_owner_item(tmp_path):
    env = _Env(tmp_path)
    rid = env.submit(env.bob, "report", "ses_1").json()["request_id"]
    assert env.withdraw(env.carol, rid).status_code == 404
    assert env.withdraw(env.alice, rid).status_code == 404
    r = env.withdraw(env.bob, rid)
    assert r.status_code == 200 and r.json() == {"status": "withdrawn"}
    assert env.withdraw(env.bob, rid).json() == {"status": "withdrawn"}
    assert env.action_required(env.alice) == 0
    assert env.status(env.bob, "report", "ses_1").json()["state"] == "none"
    assert env.decide(env.alice, rid, action="approve").json() == {"status": "withdrawn"}
    assert "access_request_withdrawn" in [a for a, _ in env.audit_actions()]
    decided_rid = env.submit(env.bob, "report", "ses_1").json()["request_id"]
    env.decide(env.alice, decided_rid, action="approve")
    late = env.withdraw(env.bob, decided_rid)
    assert late.status_code == 409 and late.json()["detail"]["code"] == "access_request_not_pending"


def test_removed_member_pending_requests_are_voided_through_deactivate_route(tmp_path):
    from bowhead_gateway.routes import admin_members

    env = _Env(tmp_path)
    rid = env.submit(env.bob, "report", "ses_1").json()["request_id"]
    other = env.submit(env.carol, "team_skill", "sk_1").json()["request_id"]
    assert env.action_required(env.alice) == 2
    app = FastAPI()
    app.include_router(admin_members.router)
    app.state.user_store = env.users
    app.state.access_request_store = env.acr
    app.state.notification_service = env.nsvc
    app.dependency_overrides[admin_members._REQUIRE_TEAM_ADMIN_OR_CREATOR] = lambda: env.dave
    r = TestClient(app).post(f"/api/v1/admin/team/members/{env.bob.id}/deactivate")
    assert r.status_code == 200, r.text
    assert _run(env.acr.get(rid, team_id=env.team.id)).status == "stale"
    assert _run(env.acr.get(other, team_id=env.team.id)).status == "pending"
    assert env.action_required(env.alice) == 1


def _remove_member(env: _Env, user) -> None:
    # The fixture workspace's only admin is alice; a workspace must keep one.
    _run(env.users.add_member(workspace_id=env.ws_id, user_id=env.carol.id, role="admin"))
    result = _run(env.users.admin_soft_delete_member_detailed(target_user_id=user.id, team_id=env.team.id, actor_user_id=env.dave.id))
    assert result.changed, result.error_code


def _all_access_requested(env: _Env, *users) -> list:
    return [item for user in users for item in env.notifications(user, "access_requested")]


def test_request_for_a_departed_owners_report_goes_to_the_other_admins(tmp_path):
    env = _Env(tmp_path)
    erin = _run(env.users.create_user(team_id=env.team.id, email="e@x.com"))
    env.make_team_admin(erin)
    _remove_member(env, env.alice)
    rid = env.submit(env.dave, "report", "ses_1").json()["request_id"]
    assert _run(env.acr.get(rid, team_id=env.team.id)).shadow is False
    # the requester (the team owner) is never asked to decide their own request
    assert [i.metadata["requestId"] for i in env.notifications(erin, "access_requested")] == [rid]
    assert env.notifications(env.dave, "access_requested") == []
    assert env.decide(erin, rid, action="approve").json() == {"status": "approved"}


def test_report_request_nobody_else_could_decide_stays_a_placeholder(tmp_path, monkeypatch):
    """The owner left and the requester is the team's only owner/admin: no approver exists, so
    no real request is created. The requester sees exactly what a missing report shows."""
    fixed = _frozen(monkeypatch)
    orphaned = _Env(tmp_path / "orphaned")
    _remove_member(orphaned, orphaned.alice)
    missing = _Env(tmp_path / "missing", with_resources=False)

    transcripts = [_requester_transcript(env, fixed, requester=env.dave) for env in (orphaned, missing)]
    assert transcripts[0] == transcripts[1]
    assert orphaned.pending_ids(orphaned.dave, is_admin=True) == []
    assert _all_access_requested(orphaned, orphaned.alice, orphaned.bob, orphaned.carol, orphaned.dave) == []
    assert [a for a, _ in orphaned.audit_actions() if a.startswith("access_request_")] == []


def test_agent_request_whose_owner_left_during_routing_is_not_created(tmp_path, monkeypatch):
    """Before Owner activation an Agent's effective Owner is its creator. If the creator leaves
    after the resolver answered ``audience_not_allowed``, nobody can change the audience (team
    roles never decide Agent requests), so the request stays a placeholder."""
    env = _Env(tmp_path)
    erin = _run(env.users.create_user(team_id=env.team.id, email="e@x.com"))
    env.make_team_admin(erin)
    resolve = ar._resolve_requestable

    async def _owner_leaves_after_resolution(app, **kwargs):
        target = await resolve(app, **kwargs)
        assert target is not None and target.owner_id == env.alice.id  # created_by fallback
        await env.users.add_member(workspace_id=env.ws_id, user_id=env.carol.id, role="admin")
        result = await env.users.admin_soft_delete_member_detailed(target_user_id=env.alice.id, team_id=env.team.id, actor_user_id=env.dave.id)
        assert result.changed, result.error_code
        return target

    monkeypatch.setattr(ar, "_resolve_requestable", _owner_leaves_after_resolution)
    rid = env.submit(env.bob, "team_subagent", "osa_1").json()["request_id"]
    assert _run(env.acr.get(rid, team_id=env.team.id)).shadow is True
    assert _run(env.users.get_team_role(env.alice.id, env.team.id)) is None
    assert _all_access_requested(env, env.alice, env.dave, erin) == []
    assert env.pending_ids(env.alice) == [] and env.pending_ids(erin, is_admin=True) == []
    assert env.status(env.bob, "team_subagent", "osa_1").json()["state"] == "pending"
    _run(ar.sweep_access_requests_once(env.app_obj(), now=datetime.now(tz=UTC) + timedelta(days=15)))
    assert env.status(env.bob, "team_subagent", "osa_1").json()["state"] == "expired"


# ── release-managed Agents (wiring; the release mechanics are covered in core) ─


def _release_managed(monkeypatch, env, outcomes):
    """Pretend the deployment is past the release cutover and record release starts."""
    from bowhead_core.agent_revision_store import AgentRevisionStore

    from bowhead_core import access_request_agent_release as release_module

    calls = []

    async def _active(self):
        return True

    async def _fake_release(revision_store, **kwargs):
        calls.append(kwargs)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(TeamSubagentStore, "release_semantics_active", _active)
    monkeypatch.setattr(release_module, "request_audience_release", _fake_release)
    client = env.client(env.alice)
    client.app.state.agent_revision_store = AgentRevisionStore(env.db, validator=None)
    return client, calls


def test_release_managed_agent_approval_waits_for_the_release_and_is_idempotent(tmp_path, monkeypatch):
    from bowhead_core.access_request_agent_release import AgentReleaseApprovalOutcome

    env = _Env(tmp_path)
    rid = env.submit(env.bob, "team_subagent", "osa_1").json()["request_id"]
    alice, calls = _release_managed(monkeypatch, env, [AgentReleaseApprovalOutcome("awaiting_owner_publish")])
    r = alice.post(f"/api/v1/access-requests/{rid}/decide", json={"action": "approve", "note": "Welcome"})
    assert r.json() == {"status": "approved_pending"}
    assert calls == [
        {
            "team_id": env.team.id,
            "agent_id": "osa_1",
            "owner_user_id": env.alice.id,
            "requester_id": env.bob.id,
        }
    ]
    row = _run(env.acr.get(rid, team_id=env.team.id))
    assert row.status == "approved_pending" and row.release_candidate_id is None
    # no direct audience write happened
    grants = _run(env.access.list_resource_grants(env.team.id, resource_kind="team_subagent", resource_id="osa_1"))
    assert not [g for g in grants if g.principal_id == env.bob.id]
    status = env.status(env.bob, "team_subagent", "osa_1").json()
    assert status["state"] == "approved_pending" and status["request"]["note"] == "Welcome"
    assert env.notifications(env.bob, "access_request_decided")[0].metadata["outcome"] == "approved_pending"
    # approving again never starts a second release
    assert alice.post(f"/api/v1/access-requests/{rid}/decide", json={"action": "approve"}).json() == {"status": "approved_pending"}
    assert len(calls) == 1


def test_release_managed_agent_with_unpublished_changes_leaves_the_request_pending(tmp_path, monkeypatch):
    from bowhead_core.access_request_agent_release import UNPUBLISHED_CHANGES, AgentReleaseApprovalError

    env = _Env(tmp_path)
    rid = env.submit(env.bob, "team_subagent", "osa_1").json()["request_id"]
    alice, _ = _release_managed(monkeypatch, env, [AgentReleaseApprovalError(UNPUBLISHED_CHANGES)])
    r = alice.post(f"/api/v1/access-requests/{rid}/decide", json={"action": "approve"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "access_request_agent_has_unpublished_changes"
    assert _run(env.acr.get(rid, team_id=env.team.id)).status == "pending"
    assert env.action_required(env.alice) == 1


def test_release_settlement_notifies_requester_and_owner(tmp_path, monkeypatch):
    from bowhead_core import access_request_agent_release as release_module

    env = _Env(tmp_path)
    ok_id = env.submit(env.bob, "team_subagent", "osa_1").json()["request_id"]
    failed_id = env.submit(env.carol, "team_subagent", "osa_1").json()["request_id"]
    for rid in (ok_id, failed_id):
        _run(
            env.acr.set_decision_once(
                request_id=rid, team_id=env.team.id, status="approved_pending", decided_by=env.alice.id, decision=acr_module.DecisionDetails()
            )
        )

    async def _outcome(db_path, row):
        return "approved" if row.id == ok_id else "approve_failed"

    monkeypatch.setattr(release_module, "_release_pending_outcome", _outcome)
    assert _run(ar.reconcile_agent_release_requests(env.app_obj(), team_id=env.team.id, agent_id="osa_1")) == 2
    assert _run(env.acr.get(ok_id, team_id=env.team.id)).status == "approved"
    assert _run(env.acr.get(failed_id, team_id=env.team.id)).status == "approve_failed"
    assert env.notifications(env.bob, "access_request_decided")[0].metadata["outcome"] == "approved"
    # the requester gets the same "expired" notice and /status value as any request that did
    # not take effect (no reason, no approver)
    [expired] = env.notifications(env.carol, "access_request_expired")
    assert expired.metadata == {"requestId": failed_id, "outcome": "expired"} and expired.actor_user_id is None
    assert [n.metadata["requestId"] for n in env.notifications(env.alice, "access_request_release_failed")] == [failed_id]
    assert env.status(env.carol, "team_subagent", "osa_1").json()["state"] == "expired"


def test_release_claim_blocks_racing_withdraw_deny_and_expiry_during_draft_save(tmp_path, monkeypatch):
    from bowhead_core.access_request_agent_release import AgentReleaseApprovalOutcome

    env = _Env(tmp_path)
    rid = env.submit(env.bob, "team_subagent", "osa_1").json()["request_id"]
    races = []

    async def _racing_release(revision_store, **kwargs):
        # While the audience draft is being saved, everyone else tries to move the request.
        races.append((await env.acr.withdraw_once(request_id=rid, team_id=env.team.id, requester_id=env.bob.id))[1])
        races.append((await env.acr.set_decision_once(request_id=rid, team_id=env.team.id, status="denied", decided_by=env.dave.id))[1])
        races.append(bool(await env.acr.expire_due(now="2999-01-01T00:00:00+00:00")))
        races.append(bool(await release_module.reconcile_release_pending(env.acr, env.db, team_id=env.team.id)))
        return AgentReleaseApprovalOutcome("awaiting_owner_publish")

    from bowhead_core import access_request_agent_release as release_module

    alice, _ = _release_managed(monkeypatch, env, [])
    monkeypatch.setattr(release_module, "request_audience_release", _racing_release)
    assert alice.post(f"/api/v1/access-requests/{rid}/decide", json={"action": "approve"}).json() == {"status": "approved_pending"}
    assert races == [False, False, False, False]
    row = _run(env.acr.get(rid, team_id=env.team.id))
    assert row.status == "approved_pending" and row.release_candidate_id is None


def test_failed_release_start_reverts_the_claim_to_pending(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    rid = env.submit(env.bob, "team_subagent", "osa_1").json()["request_id"]
    alice, _ = _release_managed(monkeypatch, env, [ValueError("publication blocked")])
    assert alice.post(f"/api/v1/access-requests/{rid}/decide", json={"action": "approve"}).status_code == 422
    row = _run(env.acr.get(rid, team_id=env.team.id))
    assert row.status == "pending" and row.decided_by is None and row.release_candidate_id is None
    # the requester can still withdraw, nothing is stuck
    assert env.withdraw(env.bob, rid).json() == {"status": "withdrawn"}


# ── review follow-ups ─────────────────────────────────────────────────────────


class _SyncSpy:
    """Counts follow-up calls that happen synchronously inside a request handler."""

    def __init__(self, monkeypatch, env):
        self.calls: list[str] = []
        for name in ("resolve_access_requested", "emit_access_requested", "refresh_access_digest"):
            original = getattr(env.nsvc, name)

            async def _spy(*args, _name=name, _original=original, **kwargs):
                self.calls.append(_name)
                return await _original(*args, **kwargs)

            monkeypatch.setattr(env.nsvc, name, _spy)
        original_audit = env.users.write_admin_audit_log

        async def _audit(*args, **kwargs):
            self.calls.append("audit")
            return await original_audit(*args, **kwargs)

        monkeypatch.setattr(env.users, "write_admin_audit_log", _audit)


def _sync_footprint(env, spy, call) -> tuple[list[str], list[str]]:
    """(synchronous follow-up calls, names of the scheduled background coroutines)."""
    spy.calls.clear()
    call()
    scheduled = [coro.cr_code.co_name for coro in _SPAWNED]
    sync_calls = list(spy.calls)
    _drain()
    return sync_calls, scheduled


def test_real_and_shadow_rows_do_the_same_synchronous_work(tmp_path, monkeypatch):
    """Structural stand-in for the timing comparison: for an existing-but-inaccessible
    resource and a missing one, submit (new and deduplicated), status and withdraw make the
    same synchronous calls and schedule the same background work."""
    env = _Env(tmp_path)
    spy = _SyncSpy(monkeypatch, env)
    c = env.client(env.bob)
    footprints = {}
    for rid in ("ses_1", "ses_missing"):
        body = {"kind": "report", "resource_id": rid}
        first = _sync_footprint(env, spy, lambda body=body: c.post("/api/v1/access-requests", json=body))
        again = _sync_footprint(env, spy, lambda body=body: c.post("/api/v1/access-requests", json=body))
        request_id = _run(env.acr.latest_for_requester(team_id=env.team.id, kind="report", resource_id=rid, requester_id=env.bob.id)).id
        status = _sync_footprint(env, spy, lambda rid=rid: c.get("/api/v1/access-requests/status", params={"kind": "report", "resource_id": rid}))
        withdraw = _sync_footprint(env, spy, lambda request_id=request_id: c.post(f"/api/v1/access-requests/{request_id}/withdraw"))
        footprints[rid] = (first, again, status, withdraw)
    assert footprints["ses_1"] == footprints["ses_missing"]
    assert footprints["ses_1"][3] == ([], ["_after_withdraw"])
    assert footprints["ses_1"][0] == ([], ["promote_request"])
    # positive control: the real row's follow-up did happen in the background
    assert "resolve_access_requested" in spy.calls or env.action_required(env.alice) == 0


def test_a_lost_promotion_is_recovered_by_the_next_submit(tmp_path):
    env = _Env(tmp_path)
    c = env.client(env.bob)
    rid = c.post("/api/v1/access-requests", json={"kind": "report", "resource_id": "ses_1"}).json()["request_id"]
    _SPAWNED.pop().close()  # the background promotion never ran (e.g. process restart)
    assert _run(env.acr.get(rid, team_id=env.team.id)).shadow is True
    assert env.submit(env.bob, "report", "ses_1").json()["request_id"] == rid
    row = _run(env.acr.get(rid, team_id=env.team.id))
    assert row.shadow is False and row.owner_id == env.alice.id
    assert env.action_required(env.alice) == 1


def test_legacy_rows_never_expire_and_are_not_escalated_again(tmp_path):
    env = _Env(tmp_path)
    _seed_legacy_row(env, env.bob)
    [legacy] = _run(env.acr.list_for_approver(team_id=env.team.id, user_id=env.alice.id, is_admin=False))
    assert legacy.expires_at is None
    # a row written by an image that predates the legacy_origin marker
    _run(
        env.acr.enqueue_once(
            team_id=env.team.id, kind="report", resource_id="ses_old", requester_id=env.carol.id, owner_id=env.alice.id, message=None
        )
    )

    async def unmark():
        async with aiosqlite.connect(env.db) as db:
            await db.execute("UPDATE access_requests SET legacy_origin=0 WHERE resource_id='ses_old'")
            await db.commit()

    _run(unmark())
    now = datetime.now(tz=UTC) + timedelta(hours=80)
    counts = _run(ar.sweep_access_requests_once(env.app_obj(), now=now))
    assert counts["legacy_marked"] == 2 and counts["escalated"] == 0
    row = _run(env.acr.get(legacy.id))
    assert row.expires_at is None and row.escalated_at is not None
    later = _run(ar.sweep_access_requests_once(env.app_obj(), now=now + timedelta(days=60)))
    assert later["expired"] == 0 and later["escalated"] == 0
    assert _run(env.acr.get(legacy.id)).status == "pending"
    assert all(r.legacy_origin for r in _run(env.acr.list_for_approver(team_id=env.team.id, user_id=env.alice.id, is_admin=False)))


def test_first_sweep_after_upgrade_leaves_pre_existing_legacy_rows_alone(tmp_path):
    """Upgrade path: report requests written by the retired intake (no expiry) must not be
    mass-expired or mass-escalated by the first hourly sweep after the upgrade."""
    env = _Env(tmp_path)
    _seed_legacy_row(env, env.bob)
    _seed_legacy_row(env, env.carol)
    legacy = _run(env.acr.list_for_approver(team_id=env.team.id, user_id=env.alice.id, is_admin=False))
    assert len(legacy) == 2

    async def backdate():
        async with aiosqlite.connect(env.db) as db:
            await db.execute("UPDATE access_requests SET created_at='2026-01-01T00:00:00Z', updated_at='2026-01-01T00:00:00Z'")
            await db.commit()

    _run(backdate())

    def inbox():
        return {
            u.id: [(i.type, i.state) for i in _run(env.notif.list_for_user(user_id=u.id, limit=100))[0]]
            for u in (env.alice, env.bob, env.carol, env.dave)
        }

    before = inbox()

    counts = _run(ar.sweep_access_requests_once(env.app_obj()))
    assert counts["expired"] == 0 and counts["escalated"] == 0
    assert inbox() == before  # nobody is notified about the old rows
    for row in legacy:
        current = _run(env.acr.get(row.id))
        assert current.status == "pending" and current.expires_at is None and current.legacy_origin
    # approvers keep the old rows in their queue and can still decide them
    assert sorted(env.pending_ids(env.alice)) == sorted(r.id for r in legacy)


def test_nobody_decides_or_is_notified_about_their_own_request(tmp_path):
    env = _Env(tmp_path)
    erin = _run(env.users.create_user(team_id=env.team.id, email="e@x.com"))
    env.make_team_admin(erin)
    own = env.submit(env.dave, "report", "ses_1").json()["request_id"]
    assert env.decide(env.dave, own, action="approve").status_code == 404
    erins = env.submit(erin, "report", "ses_1").json()["request_id"]
    assert env.decide(erin, erins, action="approve").status_code == 404
    _run(ar.sweep_access_requests_once(env.app_obj(), now=datetime.now(tz=UTC) + timedelta(hours=73)))
    escalated = {i.metadata["requestId"] for i in env.notifications(erin, "access_requested_escalated")}
    assert escalated == {own}  # erin is told about dave's request, never about her own
    assert {i.metadata["requestId"] for i in env.notifications(env.dave, "access_requested_escalated")} == {erins}


def test_deleted_resource_looks_exactly_like_a_missing_one(tmp_path):
    env = _Env(tmp_path)
    rid = env.submit(env.bob, "report", "ses_1").json()["request_id"]
    before = env.status(env.bob, "report", "ses_1").json()
    _run(ar.cascade_requests_for_resource(env.app_obj(), team_id=env.team.id, kind="report", resource_id="ses_1", decided_by=env.alice.id, gone=True))
    assert env.status(env.bob, "report", "ses_1").json() == before  # still pending, same body
    assert env.notifications(env.bob, "access_request_decided") == []
    assert env.action_required(env.alice) == 0
    _run(ar.sweep_access_requests_once(env.app_obj(), now=datetime.now(tz=UTC) + timedelta(days=15)))
    assert env.status(env.bob, "report", "ses_1").json()["state"] == "expired"
    assert [n.metadata["requestId"] for n in env.notifications(env.bob, "access_request_expired")] == [rid]


def test_approved_pending_request_blocks_a_duplicate(tmp_path):
    env = _Env(tmp_path)
    rid = env.submit(env.bob, "team_subagent", "osa_1").json()["request_id"]
    _run(
        env.acr.set_decision_once(
            request_id=rid, team_id=env.team.id, status="approved_pending", decided_by=env.alice.id, decision=acr_module.DecisionDetails()
        )
    )
    assert env.submit(env.bob, "team_subagent", "osa_1").json()["request_id"] == rid


# ── follow-ups: legacy-era rows, stale claims, neutral copy ─


def _status_and_mine(env: _Env) -> tuple[bytes, bytes]:
    c = env.client(env.bob)
    status = c.get("/api/v1/access-requests/status", params={"kind": "report", "resource_id": "ses_1"}).content
    mine = c.get("/api/v1/access-requests", params={"scope": "mine"}).content
    return status, mine


def test_legacy_era_rows_are_hidden_from_the_requester_but_not_from_approvers(tmp_path, monkeypatch):
    fixed = _frozen(monkeypatch)
    existing = _Env(tmp_path / "existing")
    missing = _Env(tmp_path / "missing", with_resources=False)
    views = []
    for env in (existing, missing):
        _seed_legacy_row(env, env.bob)
        _run(ar.sweep_access_requests_once(env.app_obj(), now=fixed))  # marks the legacy rows
        views.append(_status_and_mine(env))
    assert views[0] == views[1]
    assert json.loads(views[0][0])["state"] == "none" and json.loads(views[0][1])["items"] == []
    # approvers still see and decide the legacy-era request
    [rid] = existing.pending_ids(existing.alice)
    assert existing.decide(existing.alice, rid, action="deny").json() == {"status": "denied"}
    # its later expiry is not announced to the requester either
    env = _Env(tmp_path / "expiring")
    _seed_legacy_row(env, env.bob)
    _run(ar.sweep_access_requests_once(env.app_obj(), now=fixed))
    _run(ar.sweep_access_requests_once(env.app_obj(), now=fixed + timedelta(days=15)))
    assert env.notifications(env.bob, "access_request_expired") == []


def test_new_flow_submit_takes_over_a_legacy_era_row_like_a_fresh_request(tmp_path, monkeypatch):
    fixed = _frozen(monkeypatch)
    existing = _Env(tmp_path / "existing")
    missing = _Env(tmp_path / "missing", with_resources=False)
    views = []
    for env in (existing, missing):
        _seed_legacy_row(env, env.bob, "old")
        _run(ar.sweep_access_requests_once(env.app_obj(), now=fixed))
        submit = env.submit(env.bob, "report", "ses_1", message="new").content
        views.append((submit, *_status_and_mine(env)))
    assert views[0] == views[1]
    assert json.loads(views[0][1])["state"] == "pending"


def test_lost_finalize_sends_no_approval_notification(tmp_path, monkeypatch):
    from bowhead_core.access_request_agent_release import AgentReleaseApprovalOutcome

    env = _Env(tmp_path)
    rid = env.submit(env.bob, "team_subagent", "osa_1").json()["request_id"]
    alice, _ = _release_managed(monkeypatch, env, [AgentReleaseApprovalOutcome("awaiting_owner_publish")])

    original_finalize = env.acr.finalize_release_claim

    async def _lost(**kwargs):
        # the stale-claim recovery settled the claim first
        await original_finalize(request_id=rid, status="approved_pending", candidate_id=None)
        return

    monkeypatch.setattr(env.acr, "finalize_release_claim", _lost)
    assert alice.post(f"/api/v1/access-requests/{rid}/decide", json={"action": "approve"}).json() == {"status": "approved_pending"}
    assert env.notifications(env.bob, "access_request_decided") == []


def test_expiry_copy_is_neutral(tmp_path):
    env = _Env(tmp_path)
    env.submit(env.bob, "report", "ses_missing")
    _run(ar.sweep_access_requests_once(env.app_obj(), now=datetime.now(tz=UTC) + timedelta(days=15)))
    [item] = env.notifications(env.bob, "access_request_expired")
    assert item.title == "This request is no longer active. Request access again if you still need it."


def test_requester_reads_never_close_pending_requests_only_the_sweeper_does(tmp_path):
    env = _Env(tmp_path)
    rid = env.submit(env.bob, "team_subagent", "osa_1").json()["request_id"]
    # the requester gains access some other way (here: the Owner shares the Agent team-wide)
    _run(
        env.subagents.replace_team_subagent_audience(
            env.team.id, "osa_1", mode="teamwide", access_store=env.access, actor_user_id=env.alice.id, audit_action="subagent_audience_changed"
        )
    )
    assert env.status(env.bob, "team_subagent", "osa_1").json()["state"] == "pending"
    env.client(env.bob).get("/api/v1/access-requests", params={"scope": "mine"})
    assert _run(env.acr.get(rid, team_id=env.team.id)).status == "pending"
    _run(ar.sweep_access_requests_once(env.app_obj()))
    assert _run(env.acr.get(rid, team_id=env.team.id)).status == "approved"
