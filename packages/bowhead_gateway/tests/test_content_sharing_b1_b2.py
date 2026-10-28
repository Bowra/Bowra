"""S2 security fixes (content-permissions B1/B2) — route tests.

B1: team_subagents list/get gain creator_only visibility filtering (previously
NO filter — creator_only metadata leaked team-wide while resolve blocked it).
B2: team_skills PATCH/DELETE tighten to owner OR team_admin/creator. Subagent
management R0 is stricter: only the current active resource Owner can mutate;
team administrators receive governance metadata, never manager content.

Both are UNCONDITIONAL security fixes — no feature flag (tech-design §4).
"""

from __future__ import annotations

import asyncio
import sqlite3

from bowhead_core.access_control import AccessControlStore
from bowhead_core.team_subagent_store import TeamSubagentStore
from bowhead_core.user_store import TeamSkill, TeamSubagent, User, UserStore, _id, _now
from bowhead_gateway.routes import team_skills as oskr
from bowhead_gateway.routes import team_subagents as osa
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SA_BASE = "/api/v1/team-subagents"
_SK_BASE = "/api/v1/team-skills"


def _run(coro):
    return asyncio.run(coro)


def _store(tmp_path) -> UserStore:
    s = UserStore(str(tmp_path / "u.db"))
    _run(s.initialize())
    return s


def _client(store: UserStore, user: User) -> TestClient:
    now = _now()
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT OR IGNORE INTO teams(id,name,max_seats,status,created_at) VALUES (?,?,?,?,?)",
            (user.team_id, user.team_id, 10, "active", now),
        )
        db.execute(
            "INSERT OR IGNORE INTO users(id,team_id,email,status,created_at) VALUES (?,?,?,?,?)",
            (user.id, user.team_id, user.email, "active", now),
        )
        db.execute(
            "INSERT OR IGNORE INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?)",
            (f"tm_{user.team_id}_{user.id}", user.id, user.team_id, "member", "active", now),
        )
    subagents = TeamSubagentStore(store.db_path)
    _run(subagents.initialize())
    access = AccessControlStore(store.db_path)
    _run(access.initialize())
    app = FastAPI()
    app.include_router(osa.router, prefix=_SA_BASE)
    app.include_router(oskr.router, prefix=_SK_BASE)
    app.state.user_store = store
    app.state.team_subagent_schema_store = subagents
    app.state.access_control_store = access
    app.dependency_overrides[osa._current_user] = lambda: user
    app.dependency_overrides[oskr._current_user] = lambda: user
    return TestClient(app)


def _seed_subagent(store: UserStore, *, team_id: str, created_by: str, visibility: str, name: str) -> str:
    sid = _id("osa")
    _run(
        store.create_team_subagent(
            TeamSubagent(
                id=sid,
                team_id=team_id,
                name=name,
                system_prompt="Test operating instructions.",
                tool_refs=["bi_query"],
                visibility=visibility,
                created_by=created_by,
                created_at=_now(),
                updated_at=_now(),
            )
        )
    )
    return sid


def _seed_skill(store: UserStore, *, team_id: str, created_by: str, visibility: str, name: str) -> str:
    skid = _id("oskill")
    _run(
        store.create_team_skill(
            TeamSkill(
                id=skid,
                team_id=team_id,
                name=name,
                source_path="",
                visibility=visibility,
                created_by=created_by,
                created_at=_now(),
                updated_at=_now(),
            )
        )
    )
    return skid


# ── B1: subagent list/get visibility ────────────────────────────────────


def test_b1_creator_only_hidden_from_others_list_and_get(tmp_path):
    store = _store(tmp_path)
    hidden = _seed_subagent(store, team_id="team_a", created_by="u_alice", visibility="creator_only", name="alice-private")
    public = _seed_subagent(store, team_id="team_a", created_by="u_alice", visibility="team_wide", name="alice-public")

    bob = _client(store, User(id="u_bob", team_id="team_a", email="b@x.com"))
    names = {i["name"] for i in bob.get(_SA_BASE).json()["items"]}
    assert names == {"alice-public"}
    assert bob.get(f"{_SA_BASE}/{hidden}").status_code == 404  # Prevent enumeration: same as missing.
    assert bob.get(f"{_SA_BASE}/{public}").status_code == 200

    alice = _client(store, User(id="u_alice", team_id="team_a", email="a@x.com"))
    names = {i["name"] for i in alice.get(_SA_BASE).json()["items"]}
    assert names == {"alice-private", "alice-public"}
    assert alice.get(f"{_SA_BASE}/{hidden}").status_code == 200


# ── B2: subagent PATCH/DELETE owner check ───────────────────────────────


def test_b2_subagent_member_cannot_mutate_others(tmp_path):
    store = _store(tmp_path)
    public = _seed_subagent(store, team_id="team_a", created_by="u_alice", visibility="team_wide", name="alice-public")
    hidden = _seed_subagent(store, team_id="team_a", created_by="u_alice", visibility="creator_only", name="alice-private")

    bob = _client(store, User(id="u_bob", team_id="team_a", email="b@x.com"))
    # Visible but not editable → 403.
    assert bob.patch(f"{_SA_BASE}/{public}", json={"description": "hax"}).status_code == 403
    assert bob.delete(f"{_SA_BASE}/{public}").status_code == 403
    # Invisible → 404 (hide existence).
    assert bob.patch(f"{_SA_BASE}/{hidden}", json={"description": "hax"}).status_code == 404
    assert bob.delete(f"{_SA_BASE}/{hidden}").status_code == 404

    alice = _client(store, User(id="u_alice", team_id="team_a", email="a@x.com"))
    etag = alice.get(f"{_SA_BASE}/{public}").json()["etag"]
    updated = alice.patch(
        f"{_SA_BASE}/{public}",
        json={"description": "ok"},
        headers={"If-Match": etag},
    )
    assert updated.status_code == 200
    assert (
        alice.delete(
            f"{_SA_BASE}/{public}",
            headers={"If-Match": updated.json()["etag"]},
        ).status_code
        == 204
    )


def test_b2_only_subagent_owner_can_mutate_not_team_admin_or_team_creator(tmp_path):
    store = _store(tmp_path)
    team = _run(store.create_team(name="Acme", max_seats=10))
    owner = _run(store.create_user(team_id=team.id, email="o@x.com", password="Passw0rd!"))
    creator = _run(store.create_user(team_id=team.id, email="c@x.com", password="Passw0rd!"))
    admin = _run(store.create_user(team_id=team.id, email="ad@x.com", password="Passw0rd!"))
    _run(store.set_team_creator(team.id, creator.id))
    _run(store.promote_team_role(user_id=admin.id, team_id=team.id, new_role="team_admin", promoted_by=creator.id))

    sid1 = _seed_subagent(store, team_id=team.id, created_by=owner.id, visibility="team_wide", name="owner-agent-1")
    sid2 = _seed_subagent(store, team_id=team.id, created_by=owner.id, visibility="team_wide", name="owner-agent-2")

    admin_c = _client(store, admin)
    assert admin_c.patch(f"{_SA_BASE}/{sid1}", json={"description": "by admin"}).status_code == 403
    creator_c = _client(store, creator)
    assert creator_c.delete(f"{_SA_BASE}/{sid2}").status_code == 403
    owner_c = _client(store, owner)
    sid1_etag = owner_c.get(f"{_SA_BASE}/{sid1}").json()["etag"]
    assert (
        owner_c.patch(
            f"{_SA_BASE}/{sid1}",
            json={"description": "by owner"},
            headers={"If-Match": sid1_etag},
        ).status_code
        == 200
    )
    sid2_etag = owner_c.get(f"{_SA_BASE}/{sid2}").json()["etag"]
    assert (
        owner_c.delete(
            f"{_SA_BASE}/{sid2}",
            headers={"If-Match": sid2_etag},
        ).status_code
        == 204
    )


# ── B2: team_skill PATCH/DELETE owner check ──────────────────────────────


def test_b2_skill_member_cannot_mutate_others(tmp_path):
    store = _store(tmp_path)
    public = _seed_skill(store, team_id="team_a", created_by="u_alice", visibility="team_wide", name="alice-skill")
    hidden = _seed_skill(store, team_id="team_a", created_by="u_alice", visibility="creator_only", name="alice-secret-skill")

    bob = _client(store, User(id="u_bob", team_id="team_a", email="b@x.com"))
    assert bob.patch(f"{_SK_BASE}/{public}", json={"description": "hax"}).status_code == 403
    assert bob.delete(f"{_SK_BASE}/{public}").status_code == 403
    assert bob.patch(f"{_SK_BASE}/{hidden}", json={"description": "hax"}).status_code == 404
    assert bob.delete(f"{_SK_BASE}/{hidden}").status_code == 404

    alice = _client(store, User(id="u_alice", team_id="team_a", email="a@x.com"))
    # skill-team-governance P1b (PRD §3.4/review P2-14): approved team_wide description
    # feeds Lead routing. After approval, a non-admin author cannot change it unilaterally → 403; own creator_only rows remain editable.
    assert alice.patch(f"{_SK_BASE}/{public}", json={"description": "ok"}).status_code == 403
    assert alice.patch(f"{_SK_BASE}/{hidden}", json={"description": "ok"}).status_code == 200
    assert alice.delete(f"{_SK_BASE}/{public}").status_code == 204
