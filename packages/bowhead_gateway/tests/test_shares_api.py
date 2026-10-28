"""S3 — Share API route tests (content-permissions wave 1, tech-design §3).

Covers: ownership adapters (3 kinds incl. report team containment), can_manage
gate, DTO extra=forbid / schema-level "不收" rejects, audience write-through
round-trip via API, info boundary (recipient sees only own grant), grant
CRUD + audit rows, per-kind creation-flag gating, admin team-grant convergence.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.subagent_write_boundary import (
    _issue_team_subagent_transaction_token,
)
from bowhead_core.team_subagent_store import TeamSubagentStore
from bowhead_core.user_store import TeamSubagent, User, UserStore, _id, _now
from bowhead_gateway.routes import access_control as ac
from bowhead_gateway.routes import shares
from fastapi import FastAPI
from fastapi.testclient import TestClient

_BASE = "/api/v1/shares"
_SUBAGENT_TRANSACTION_TOKEN = _issue_team_subagent_transaction_token()


@pytest.fixture(autouse=True)
def _enable_kind_flags(monkeypatch):
    """cca 创建类 flag 在测试环境默认 dark — share 适配器从属 kind 生命周期,
    这里点亮以测分享本体;flag-gating 用例自行覆盖回 False。"""
    monkeypatch.setattr(shares, "subagent_creator_enabled", lambda: True)
    monkeypatch.setattr(shares, "workflow_creator_enabled", lambda: True)


def _run(coro):
    return asyncio.run(coro)


class _Env:
    def __init__(self, tmp_path):
        from bowhead_core.access_request_store import AccessRequestStore

        db = str(tmp_path / "s.db")
        self.users = UserStore(db)
        _run(self.users.initialize())
        self.access = AccessControlStore(db)
        _run(self.access.initialize())
        self.subagents = TeamSubagentStore(db)
        _run(self.subagents.initialize())
        self.canvas = CanvasStore(db_path=str(tmp_path / "c.db"))
        _run(self.canvas.initialize())
        self.access_requests = AccessRequestStore(db)
        _run(self.access_requests.initialize())

    def client(self, user: User, *, admin: bool = False) -> TestClient:
        app = FastAPI()
        app.include_router(shares.router, prefix=_BASE)
        app.include_router(ac.router, prefix="/api/v1")
        app.state.user_store = self.users
        app.state.access_control_store = self.access
        app.state.canvas_store = self.canvas
        app.state.access_request_store = self.access_requests
        app.dependency_overrides[shares._current_user] = lambda: user
        if admin:
            app.dependency_overrides[ac._REQUIRE_ACCESS_ADMIN] = lambda: user
        return TestClient(app)

    def seed_subagent(self, *, team_id: str, created_by: str, visibility: str = "creator_only", name: str = "agent-x") -> str:
        sid = _id("osa")
        _run(
            self.users.create_team_subagent(
                TeamSubagent(
                    id=sid,
                    team_id=team_id,
                    name=name,
                    tool_refs=["bi_query"],
                    visibility=visibility,
                    created_by=created_by,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        )
        return sid

    def seed_subagent_grant(
        self,
        *,
        team_id: str,
        subagent_id: str,
        principal_type: str,
        principal_id: str,
    ):
        return _run(
            self.access.create_or_update_resource_grant(
                team_id=team_id,
                resource_kind="team_subagent",
                resource_id=subagent_id,
                principal_type=principal_type,
                principal_id=principal_id,
                permission="use",
                subagent_transaction_token=_SUBAGENT_TRANSACTION_TOKEN,
            )
        )

    def set_subagent_audience(
        self,
        *,
        team_id: str,
        subagent_id: str,
        target: str,
    ):
        return _run(
            self.access.set_audience_tx(
                team_id=team_id,
                resource_kind="team_subagent",
                resource_id=subagent_id,
                target=target,
                permission="use",
                visibility_table="team_subagent",
                subagent_transaction_token=_SUBAGENT_TRANSACTION_TOKEN,
            )
        )


def _team_users(env: _Env):
    team = _run(env.users.create_team(name="Acme", max_seats=10))
    alice = _run(env.users.create_user(team_id=team.id, email="a@x.com", password="Passw0rd!"))
    bob = _run(env.users.create_user(team_id=team.id, email="b@x.com", password="Passw0rd!"))
    return team, alice, bob


# ── adapters + can_manage + grant CRUD ──────────────────────────────────


def test_subagent_share_mutations_return_stable_obsolete_error(tmp_path):
    env = _Env(tmp_path)
    team, alice, bob = _team_users(env)
    sid = env.seed_subagent(team_id=team.id, created_by=alice.id)

    a = env.client(alice)
    # Initial audience = private.
    r = a.get(f"{_BASE}/team_subagent/{sid}")
    assert r.status_code == 200 and r.json()["audience"] == "private"
    assert r.json()["can_manage"] is True and r.json()["grants"] == []

    for response in (
        a.post(
            f"{_BASE}/team_subagent/{sid}/grants",
            json={"principal_type": "user", "principal_id": bob.id},
        ),
        a.put(
            f"{_BASE}/team_subagent/{sid}/audience",
            json={"audience": "team_wide"},
        ),
        a.delete(f"{_BASE}/team_subagent/{sid}/grants/grant_missing"),
    ):
        assert response.status_code == 422
        assert response.json()["detail"]["error"]["code"] == "subagent_write_api_obsolete"

    # Authorization still runs before the obsolete-API response.
    b = env.client(bob)
    assert (
        b.post(
            f"{_BASE}/team_subagent/{sid}/grants",
            json={"principal_type": "user", "principal_id": alice.id},
        ).status_code
        == 403
    )
    grants = _run(
        env.access.list_resource_grants(
            team.id,
            resource_kind="team_subagent",
            resource_id=sid,
        )
    )
    assert grants == []


def test_get_shares_third_party_404_but_team_wide_visible(tmp_path):
    """信息边界(B1 反枚举):第三方(非 owner/管理者/被分享人)GET 资源 share 态 → 404,
    不泄露 label/owner/audience/存在性;切 team_wide 后全员可见 → 200。"""
    env = _Env(tmp_path)
    team, alice, bob = _team_users(env)
    carol = _run(env.users.create_user(team_id=team.id, email="c@x.com", password="Passw0rd!"))
    sid = env.seed_subagent(team_id=team.id, created_by=alice.id)

    # private + carol without grant → 404.
    assert env.client(carol).get(f"{_BASE}/team_subagent/{sid}").status_code == 404

    # Release A mixed-version fixture seeds a historical custom grant directly;
    # generic HTTP writes are tested as rejected above.
    env.seed_subagent_grant(
        team_id=team.id,
        subagent_id=sid,
        principal_type="user",
        principal_id=bob.id,
    )
    assert env.client(carol).get(f"{_BASE}/team_subagent/{sid}").status_code == 404
    assert env.client(bob).get(f"{_BASE}/team_subagent/{sid}").status_code == 200

    env.set_subagent_audience(
        team_id=team.id,
        subagent_id=sid,
        target="team_wide",
    )
    r = env.client(carol).get(f"{_BASE}/team_subagent/{sid}")
    assert r.status_code == 200 and r.json()["audience"] == "team_wide"


def test_obsolete_team_grant_revoke_does_not_change_visibility(tmp_path):
    env = _Env(tmp_path)
    team, alice, bob = _team_users(env)
    sid = env.seed_subagent(team_id=team.id, created_by=alice.id)
    a = env.client(alice)
    env.seed_subagent_grant(
        team_id=team.id,
        subagent_id=sid,
        principal_type="user",
        principal_id=bob.id,
    )
    result = env.set_subagent_audience(
        team_id=team.id,
        subagent_id=sid,
        target="team_wide",
    )
    assert _run(env.users.get_team_subagent(team.id, sid)).visibility == "team_wide"

    response = a.delete(
        f"{_BASE}/team_subagent/{sid}/grants/{result['team_grant_id']}",
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "subagent_write_api_obsolete"
    assert _run(env.users.get_team_subagent(team.id, sid)).visibility == "team_wide"
    active_ids = {
        grant.id
        for grant in _run(
            env.access.list_resource_grants(
                team.id,
                resource_kind="team_subagent",
                resource_id=sid,
            )
        )
    }
    assert result["team_grant_id"] in active_ids


def test_subagent_audience_api_rejects_every_legacy_target(tmp_path):
    env = _Env(tmp_path)
    team, alice, _bob = _team_users(env)
    sid = env.seed_subagent(team_id=team.id, created_by=alice.id)
    a = env.client(alice)
    for target in ("private", "custom", "team_wide"):
        response = a.put(
            f"{_BASE}/team_subagent/{sid}/audience",
            json={"audience": target},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["error"]["code"] == "subagent_write_api_obsolete"
    assert _run(env.users.get_team_subagent(team.id, sid)).visibility == "creator_only"


def test_dto_forbid_and_principal_validation(tmp_path):
    env = _Env(tmp_path)
    team, alice, _bob = _team_users(env)
    sid = env.seed_subagent(team_id=team.id, created_by=alice.id)
    a = env.client(alice)

    # Extra fields → 422 (schema actively rejects the three unsupported shapes).
    for bad in (
        {"principal_type": "user", "principal_id": alice.id, "effect": "deny"},
        {"principal_type": "user", "principal_id": alice.id, "permission": "admin"},
        {"principal_type": "team", "principal_id": team.id},  # Literal rejects team.
    ):
        assert a.post(f"{_BASE}/team_subagent/{sid}/grants", json=bad).status_code == 422
    # Missing / foreign-team / inactive principal → 422.
    assert a.post(f"{_BASE}/team_subagent/{sid}/grants", json={"principal_type": "user", "principal_id": "u_ghost"}).status_code == 422
    assert a.post(f"{_BASE}/team_subagent/{sid}/grants", json={"principal_type": "group", "principal_id": "grp_ghost"}).status_code == 422
    # Unknown kind / unknown audience.
    assert a.get(f"{_BASE}/kb_file/abc").status_code == 422
    assert a.put(f"{_BASE}/team_subagent/{sid}/audience", json={"audience": "everyone"}).status_code == 422


def test_report_adapter_team_containment(tmp_path):
    env = _Env(tmp_path)
    team, alice, bob = _team_users(env)
    # Team-owned space (Bowhead ID) + canvas in an orphaned space.
    ws_id = _run(env.users.sync_workspace_from_datarc(team_id=team.id, datarc_workspace_id="336", name="WS"))
    # Synced spaces lack member rows; share management requires owner membership (production rows come from initial join or member-add).
    _run(env.users.add_member(workspace_id=ws_id, user_id=alice.id, role="admin"))
    _run(env.canvas.ensure_canvas(session_id="ses_ok", workspace_id=ws_id, user_id=alice.id))
    _run(env.canvas.ensure_canvas(session_id="ses_datarc", workspace_id="336", user_id=alice.id))
    _run(env.canvas.ensure_canvas(session_id="ses_orphan", workspace_id="ws_nowhere", user_id=alice.id))

    a = env.client(alice)
    # Bowhead and datarc identifiers both resolve to this team → shareable.
    r = a.post(f"{_BASE}/report/ses_ok/grants", json={"principal_type": "user", "principal_id": bob.id})
    assert r.status_code == 201 and r.json()["permission"] == "read"
    assert a.get(f"{_BASE}/report/ses_datarc").status_code == 200
    # Orphan space: explicit 409 for owner, 404 for others.
    assert a.get(f"{_BASE}/report/ses_orphan").status_code == 409
    assert env.client(bob).get(f"{_BASE}/report/ses_orphan").status_code == 404
    # Missing report → 404.
    assert a.get(f"{_BASE}/report/ses_ghost").status_code == 404
    # Report audience derived solely from grants.
    assert a.get(f"{_BASE}/report/ses_ok").json()["audience"] == "custom"


def test_archived_report_share_history_requires_membership_and_blocks_mutations(
    tmp_path,
):
    env = _Env(tmp_path)
    team, alice, bob = _team_users(env)
    carol = _run(
        env.users.create_user(
            team_id=team.id,
            email="c@x.com",
            password="Passw0rd!",
        )
    )
    workspace_id = _run(
        env.users.create_local_workspace(
            team_id=team.id,
            name="Archived Report",
            creator_user_id=alice.id,
        )
    )
    _run(
        env.users.add_member(
            workspace_id=workspace_id,
            user_id=bob.id,
            role="member",
            invited_by=alice.id,
        )
    )
    _run(
        env.canvas.ensure_canvas(
            session_id="ses_archived",
            workspace_id=workspace_id,
            user_id=alice.id,
        )
    )
    alice_client = env.client(alice)
    grant = alice_client.post(
        f"{_BASE}/report/ses_archived/grants",
        json={"principal_type": "user", "principal_id": bob.id},
    )
    assert grant.status_code == 201, grant.text

    async def _archive() -> None:
        async with aiosqlite.connect(env.users.db_path) as db:
            await db.execute(
                "UPDATE workspaces SET status='archived' WHERE id=?",
                (workspace_id,),
            )
            await db.commit()

    _run(_archive())

    assert alice_client.get(f"{_BASE}/report/ses_archived").status_code == 200
    assert env.client(bob).get(f"{_BASE}/report/ses_archived").status_code == 200
    removed_member_read = env.client(carol).get(f"{_BASE}/report/ses_archived")
    assert removed_member_read.status_code == 403

    blocked = [
        alice_client.post(
            f"{_BASE}/report/ses_archived/grants",
            json={"principal_type": "user", "principal_id": carol.id},
        ),
        alice_client.put(
            f"{_BASE}/report/ses_archived/audience",
            json={"audience": "team_wide"},
        ),
        alice_client.delete(f"{_BASE}/report/ses_archived/grants/{grant.json()['id']}"),
    ]
    for response in blocked:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"]["code"] == "workspace_archived"


def test_active_space_report_share_read_requires_space_membership(tmp_path):
    """**active** 空间也判空间成员资格（批2）。

    只判空间生命周期时，空间 active 就对「已被移出该空间、但仍在团队」的报告**属主**完全不设防；
    上面那条 archived 用例量的是归档空间，覆盖不到这一格。

    - **正对照**：属主仍是空间成员 → 200（证明不是把所有人都拦了）；
    - **主断言**：属主被移出空间（空间仍 active）→ 403。
    """
    env = _Env(tmp_path)
    team, alice, bob = _team_users(env)
    workspace_id = _run(
        env.users.create_local_workspace(
            team_id=team.id,
            name="Active Report",
            creator_user_id=alice.id,
        )
    )
    _run(
        env.users.add_member(
            workspace_id=workspace_id,
            user_id=bob.id,
            role="member",
            invited_by=alice.id,
        )
    )
    _run(
        env.canvas.ensure_canvas(
            session_id="ses_active",
            workspace_id=workspace_id,
            user_id=alice.id,
        )
    )
    # Validate fixture: alice is neither team owner nor team_admin, otherwise allow_team_admin_override
    # allows her (existing PRD §5.5-A semantics), bypassing the space-membership gate under test.
    assert _run(env.users.get_team_role(alice.id, team.id)) not in {"owner", "team_admin"}
    alice_client = env.client(alice)
    assert alice_client.get(f"{_BASE}/report/ses_active").status_code == 200

    # ★ Sole state change: remove only space membership; space stays active, team membership unchanged.
    assert _run(env.users.remove_member(workspace_id, alice.id)) is True
    assert _run(env.users.get_member_role(workspace_id, alice.id)) is None
    assert _run(env.users.get_team_role(alice.id, team.id)) is not None

    denied = alice_client.get(f"{_BASE}/report/ses_active")
    assert denied.status_code == 403, denied.text
    # D3: shares keeps its existing code, same family as the current archived branch.
    assert denied.json()["detail"]["error"]["code"] == "ws_access_denied:not_a_member"


def test_archived_report_share_gives_no_team_admin_override(tmp_path):
    """★ 补审 P1-1：**archived 那一档不给团队管理员豁免**。

    `_require_share_lifecycle` 的成员校验分支命中后直接 ``return``，跳过下面那段不带 override 的
    archived 补校验。若这里无条件给管理员豁免，「本团队管理员 ∧ 已被移出该空间 ∧ 空间已归档」
    会从 403 变成 **200**。``turns.py`` 的同一条不变量另有用例钉着。

    - **正对照**：仍是空间成员时照读 200；
    - **主断言**：移出 + 归档后 403。
    """
    env = _Env(tmp_path)
    team, alice, _bob = _team_users(env)
    workspace_id = _run(
        env.users.create_local_workspace(
            team_id=team.id,
            name="Archived Admin Report",
            creator_user_id=alice.id,
        )
    )
    _run(
        env.canvas.ensure_canvas(
            session_id="ses_arch_admin",
            workspace_id=workspace_id,
            user_id=alice.id,
        )
    )

    async def _make_team_admin() -> None:
        async with aiosqlite.connect(env.users.db_path) as db:
            await db.execute(
                "UPDATE team_members SET role='team_admin' WHERE team_id=? AND user_id=?",
                (team.id, alice.id),
            )
            await db.commit()

    _run(_make_team_admin())
    # Validate fixture: without this, override never triggers and the relaxed path is not measured.
    assert _run(env.users.get_team_role(alice.id, team.id)) == "team_admin"

    alice_client = env.client(alice)
    # Positive control: currently still a space member ⇒ read normally.
    assert alice_client.get(f"{_BASE}/report/ses_arch_admin").status_code == 200

    assert _run(env.users.remove_member(workspace_id, alice.id)) is True
    assert _run(env.users.get_member_role(workspace_id, alice.id)) is None
    # Team membership unchanged; otherwise not measuring the space-membership gate.
    assert _run(env.users.get_team_role(alice.id, team.id)) == "team_admin"

    async def _archive() -> None:
        async with aiosqlite.connect(env.users.db_path) as db:
            await db.execute(
                "UPDATE workspaces SET status='archived' WHERE id=?",
                (workspace_id,),
            )
            await db.commit()

    _run(_archive())

    denied = alice_client.get(f"{_BASE}/report/ses_arch_admin")
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"]["error"]["code"] == "ws_access_denied:not_a_member"


def test_kind_creation_flag_gates_share(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    team, alice, _bob = _team_users(env)
    sid = env.seed_subagent(team_id=team.id, created_by=alice.id)
    monkeypatch.setattr(shares, "subagent_creator_enabled", lambda: False)
    a = env.client(alice)
    assert a.get(f"{_BASE}/team_subagent/{sid}").status_code == 404  # kind dark → share dark
    monkeypatch.setattr(shares, "workflow_creator_enabled", lambda: False)
    assert a.get(f"{_BASE}/team_skill/oskill_x").status_code == 404


# ── S7: manager-view pending access-request count ───────────────────────────


def test_get_shares_pending_request_count_manager_only(tmp_path):
    """wave 2: 管理者 GET 资源 share 态时带上 open 访问申请数(分享弹层"N 待处理"提示);
    被分享人视角不泄露该计数(信息边界)。"""
    env = _Env(tmp_path)
    team, alice, bob = _team_users(env)
    sid = env.seed_subagent(team_id=team.id, created_by=alice.id)
    a = env.client(alice)

    # No requests → 0.
    assert a.get(f"{_BASE}/team_subagent/{sid}").json()["pending_requests"] == 0

    # bob submits one pending access request → manager alice sees 1.
    _run(
        env.access_requests.enqueue_once(
            team_id=team.id,
            kind="team_subagent",
            resource_id=sid,
            requester_id=bob.id,
            owner_id=alice.id,
            message="求授权",
        )
    )
    body = a.get(f"{_BASE}/team_subagent/{sid}").json()
    assert body["can_manage"] is True and body["pending_requests"] == 1

    # Recipient carol view: even when shared, excludes pending_requests.
    carol = _run(env.users.create_user(team_id=team.id, email="c3@x.com", password="Passw0rd!"))
    env.seed_subagent_grant(
        team_id=team.id,
        subagent_id=sid,
        principal_type="user",
        principal_id=carol.id,
    )
    cbody = env.client(carol).get(f"{_BASE}/team_subagent/{sid}").json()
    assert cbody["can_manage"] is False and "pending_requests" not in cbody


# ── Admin consolidation (single write-through point) ───────────────────────


def test_admin_subagent_grant_api_is_stably_rejected(tmp_path):
    env = _Env(tmp_path)
    team, alice, _bob = _team_users(env)
    creator = _run(env.users.create_user(team_id=team.id, email="c@x.com", password="Passw0rd!"))
    _run(env.users.set_team_creator(team.id, creator.id))
    sid = env.seed_subagent(team_id=team.id, created_by=alice.id)

    admin_c = env.client(creator, admin=True)
    for principal_type, principal_id in (
        ("team", team.id),
        ("user", alice.id),
    ):
        response = admin_c.post(
            "/api/v1/resource-grants",
            json={
                "resource_kind": "team_subagent",
                "resource_id": sid,
                "principal_type": principal_type,
                "principal_id": principal_id,
                "permission": "use",
            },
        )
        assert response.status_code == 422
        assert response.json()["detail"]["error"]["code"] == "subagent_write_api_obsolete"

    grant = env.seed_subagent_grant(
        team_id=team.id,
        subagent_id=sid,
        principal_type="user",
        principal_id=alice.id,
    )
    response = admin_c.delete(f"/api/v1/resource-grants/{grant.id}")
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "subagent_write_api_obsolete"
    assert _run(env.access.get_resource_grant(team.id, grant.id)).revoked_at is None


# ── S4: list partition fields + deletion cascade (via team_subagents route) ──


def test_s4_list_partition_fields_and_delete_cascade(tmp_path, monkeypatch):
    from bowhead_gateway.routes import team_subagents as osa

    env = _Env(tmp_path)
    team, alice, bob = _team_users(env)
    sid = env.seed_subagent(team_id=team.id, created_by=alice.id, visibility="creator_only", name="alice-private")

    def _osa_client(user):
        app = FastAPI()
        app.include_router(osa.router, prefix="/api/v1/team-subagents")
        app.state.user_store = env.users
        app.state.access_control_store = env.access
        app.state.team_subagent_schema_store = env.subagents
        app.dependency_overrides[osa._current_user] = lambda: user
        return app, TestClient(app)

    # Before sharing: bob list empty.
    _, bob_c = _osa_client(bob)
    assert bob_c.get("/api/v1/team-subagents").json()["items"] == []

    # Mixed-version fixture: an old active grant remains readable in Release A.
    env.seed_subagent_grant(
        team_id=team.id,
        subagent_id=sid,
        principal_type="user",
        principal_id=bob.id,
    )

    # Appears in bob's list with only safe Owner summary + audience; no old shared_by/raw definition.
    items = bob_c.get("/api/v1/team-subagents").json()["items"]
    assert [i["name"] for i in items] == ["alice-private"]
    assert items[0]["owner"]["id"] == alice.id and items[0]["audience"] == "custom"
    assert "shared_by" not in items[0] and "system_prompt" not in items[0]
    _, alice_c = _osa_client(alice)
    mine = alice_c.get("/api/v1/team-subagents").json()["items"]
    assert mine[0]["owner"]["id"] == alice.id and mine[0]["audience"] == "custom"
    # bob GET visible (grant path), but PATCH/DELETE 403 (visible, not editable).
    assert bob_c.get(f"/api/v1/team-subagents/{sid}").status_code == 200
    assert bob_c.patch(f"/api/v1/team-subagents/{sid}", json={"description": "x"}).status_code == 403

    # Owner deletes → grants cascade revoked.
    assert (
        alice_c.delete(
            f"/api/v1/team-subagents/{sid}",
            headers={"If-Match": mine[0]["etag"]},
        ).status_code
        == 204
    )
    remaining = _run(env.access.list_resource_grants(team.id, resource_kind="team_subagent", resource_id=sid))
    assert remaining == []
    assert bob_c.get("/api/v1/team-subagents").json()["items"] == []


# ── S6: sharing notifications + people-picker directory ─────────────────────


def test_s6_share_notification_and_directory(tmp_path):
    from bowhead_core.notification_store import NotificationStore
    from bowhead_gateway.routes import shares as shares_mod

    env = _Env(tmp_path)
    team, alice, bob = _team_users(env)
    ns = NotificationStore(db_path=str(tmp_path / "n.db"))
    _run(ns.initialize())
    sid = env.seed_subagent(team_id=team.id, created_by=alice.id)

    app = FastAPI()
    app.include_router(shares_mod.router, prefix=_BASE)
    app.include_router(shares_mod.directory_router, prefix="/api/v1/team")
    app.state.user_store = env.users
    app.state.access_control_store = env.access
    app.state.canvas_store = env.canvas
    app.state.notification_store = ns
    app.dependency_overrides[shares_mod._current_user] = lambda: alice
    a = TestClient(app)

    # Obsolete Subagent share writes are rejected before notification side effects.
    r = a.post(f"{_BASE}/team_subagent/{sid}/grants", json={"principal_type": "user", "principal_id": bob.id})
    assert r.status_code == 422
    bob_items, _ = _run(ns.list_for_user(user_id=bob.id))
    assert bob_items == []
    alice_items, _ = _run(ns.list_for_user(user_id=alice.id))
    assert alice_items == []

    # Group targets are rejected at the same boundary and also emit nothing.
    grp = _run(env.access.create_group(team_id=team.id, name="g1", created_by=alice.id))
    carol = _run(env.users.create_user(team_id=team.id, email="c2@x.com", password="Passw0rd!"))
    _run(env.access.add_group_member(team_id=team.id, group_id=grp.id, user_id=carol.id))
    r = a.post(f"{_BASE}/team_subagent/{sid}/grants", json={"principal_type": "group", "principal_id": grp.id})
    assert r.status_code == 422
    carol_items, _ = _run(ns.list_for_user(user_id=carol.id))
    assert carol_items == []

    # Picker: minimal directory fields + masked email; group directory readable by members.
    d = a.get("/api/v1/team/directory").json()
    emails = {i["email_masked"] for i in d["items"]}
    assert "a***@x.com" in emails and all("@x.com" in e for e in emails)
    assert all(set(i) == {"id", "display_name", "email_masked"} for i in d["items"])
    g = a.get("/api/v1/team/groups-directory").json()
    assert [x["name"] for x in g["items"]] == ["g1"]
    assert set(g["items"][0]) == {"id", "name", "member_count"}
