"""skill-team-governance P1b — 审批状态机穿闸测试（tech-design §1 转移表 + PRD 三扇门）.

覆盖：L1/L2 转移表逐行（含 L2 撤回不下架 live、L2 拒后 live 照跑）、三扇门+第四扇
（admin grants 控制台经 set_audience 同一闸拒）、CAS 双层 409、crash 三窗口恢复
（盘状态直构，不打桩）、自批 403 / admin 直批、``only_approved`` 默认参守卫
（store / picker / effective=Lead 广告段 / GET list）、``skill_invoke.audit``
origin=team、通知分键+收件人表、审计三动作、pending 点名分享拒。

真 UserStore / AccessControlStore / NotificationStore 共用同一 sqlite 文件——
``set_audience_tx`` 的 visibility 写穿要求 access store 与 team_skill 表同库。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.notification_store import NotificationStore
from bowhead_core.skill_upload import bundle_sha256
from bowhead_core.user_store import TeamSkillStoreAdapter, User, UserStore, _now
from bowhead_gateway.routes import team_skills as oskr
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_core import share_service


def _run(coro):
    return asyncio.run(coro)


OWNER = User(id="u_owner", team_id="team_a", email="owner@x.com")
ADMIN = User(id="u_admin", team_id="team_a", email="admin@x.com")
AUTHOR = User(id="u_author", team_id="team_a", email="author@x.com")
MEMBER = User(id="u_member", team_id="team_a", email="member@x.com")

_BASE = "/api/v1/team-skills"


def _skill_md(name="channel-gm-recipe", desc="渠道毛利核对 触发：渠道毛利/口径", body="## 执行步骤\n1. 取数\n2. 算毛利\n") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n{body}"


async def _seed_team(db_path: str) -> None:
    """teams+users+team_members：owner=team creator、admin=team_admin、author/member=member."""
    now = _now()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, created_by_user_id, created_at) VALUES ('team_a','A','u_owner',?)",
            (now,),
        )
        for u in (OWNER, ADMIN, AUTHOR, MEMBER):
            await db.execute(
                "INSERT INTO users(id, team_id, email, created_at) VALUES (?,?,?,?)",
                (u.id, "team_a", u.email, now),
            )
        for uid, role in (("u_admin", "team_admin"), ("u_author", "member"), ("u_member", "member")):
            await db.execute(
                "INSERT INTO team_members(id, user_id, team_id, role, status, created_at) VALUES (?,?,?,?,'active',?)",
                (f"tm_{uid}", uid, "team_a", role, now),
            )
        await db.commit()


def _store(tmp_path) -> UserStore:
    s = UserStore(str(tmp_path / "u.db"))
    _run(s.initialize())
    _run(_seed_team(str(tmp_path / "u.db")))
    return s


async def _set_risk_report(db_path: str, skill_id: str, risk_json: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE team_skill SET risk_report_json=? WHERE id=?", (risk_json, skill_id))
        await db.commit()


def _app(store: UserStore, monkeypatch, tmp_path, *, access=False, notify=False, personal=False) -> FastAPI:
    monkeypatch.setattr(oskr, "_TEAM_SKILLS_ROOT", tmp_path / "team_skills_data")
    monkeypatch.setattr(oskr, "_USER_SKILLS_ROOT", tmp_path / ".data" / "user_skills")
    app = FastAPI()
    app.include_router(oskr.router, prefix=_BASE)
    app.state.user_store = store
    if access:
        acs = AccessControlStore(str(tmp_path / "u.db"))  # Same DB: visibility writes through to team_skill table.
        _run(acs.initialize())
        app.state.access_control_store = acs
    if notify:
        ns = NotificationStore(str(tmp_path / "u.db"))
        _run(ns.initialize())
        app.state.notification_store = ns
    if personal:
        # promote/copy need personal surface: skills router + skill_instance_store + empty catalog.
        # Personal bundle validation uses CWD-relative .data → callers must monkeypatch.chdir(tmp_path).
        from bowhead_core.skill_instance_store import SkillInstanceStore
        from bowhead_gateway.routes import skills as skr

        sis = SkillInstanceStore(str(tmp_path / "u.db"))
        _run(sis.initialize())
        app.state.skill_instance_store = sis
        app.state.skill_catalog = SimpleNamespace(list_names=list, list_specs=list, get=lambda name: None, refresh=lambda: None)
        app.include_router(skr.router, prefix="/api/v1/skills")
    return app


def _client(app: FastAPI, user: User) -> TestClient:
    app.dependency_overrides[oskr._current_user] = lambda: user
    return TestClient(app)


def _create_pending(app: FastAPI, author: User = AUTHOR, name="channel-gm-recipe") -> dict:
    d = _client(app, author).post(_BASE, json={"body_md": _skill_md(name=name)}).json()
    assert d["approval"] == "pending", d
    return d


def _approved_skill(app: FastAPI, store: UserStore) -> dict:
    d = _create_pending(app)
    r = _client(app, ADMIN).post(f"{_BASE}/{d['id']}/approve", json={"source_sha256": d["source_sha256"]})
    assert r.status_code == 200, r.text
    return r.json()


def _submit_candidate(store: UserStore, rid: str, *, marker="v2") -> tuple[Path, str, str]:
    """构造候选目录（copy source + 改 body）并走 store L2 提交转移；返回 (dir, sha, ts)."""
    row = _run(store.get_team_skill("team_a", rid))
    src = Path(row.source_path)
    cand = src.parent / f"{row.name}__candidate"
    if cand.exists():
        shutil.rmtree(cand)
    shutil.copytree(src, cand)
    (cand / "SKILL.md").write_text(_skill_md(name=row.name, body=f"## 执行步骤 {marker}\n1. 新流程\n"), encoding="utf-8")
    sha = bundle_sha256(cand)
    ts = _run(store.submit_team_skill_candidate("team_a", rid, candidate_path=str(cand), candidate_sha256=sha, submitted_by="u_author"))
    assert ts
    return cand, sha, ts


# ── Gate 1: create + only_approved default-argument guard (five consumers consolidated) ──


def test_member_create_team_wide_lands_pending_and_hidden(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path)
    r = _client(app, AUTHOR).post(_BASE, json={"body_md": _skill_md()})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["approval"] == "pending" and d["visibility"] == "team_wide"
    assert d["submitted_by"] == "u_author" and d["source_sha256"]
    rid = d["id"]
    # Store default-argument guard closes resolve/effective/home_proposals (same list only_enabled=True call shape).
    assert _run(store.get_team_skill_by_name("team_a", "channel-gm-recipe")) is None
    assert _run(store.list_team_skills("team_a")) == []
    assert _run(store.list_team_skills("team_a", only_enabled=True)) == []  # home_proposals call shape.
    assert _run(store.get_team_skill_by_name("team_a", "channel-gm-recipe", only_approved=False)) is not None
    # GET list/detail: invisible to members (404 against enumeration); author/admin see approval fields.
    assert _client(app, MEMBER).get(_BASE).json()["items"] == []
    assert _client(app, MEMBER).get(f"{_BASE}/{rid}").status_code == 404
    mine = _client(app, AUTHOR).get(_BASE).json()["items"]
    assert [i["approval"] for i in mine] == ["pending"]
    admin_view = _client(app, OWNER).get(f"{_BASE}/{rid}").json()
    assert admin_view["approval"] == "pending" and admin_view["source_sha256"]
    # Collapsed approval card risk chips visible (review P2): risk_report included in list (inline column, zero disk reads).
    _run(_set_risk_report(str(tmp_path / "u.db"), rid, '{"languages": {"py": 2}, "keywords": ["curl"]}'))
    admin_list = {i["id"]: i for i in _client(app, OWNER).get(_BASE).json()["items"]}
    assert admin_list[rid]["risk_report"] == {"languages": {"py": 2}, "keywords": ["curl"]}
    member_view_items = _client(app, MEMBER).get(_BASE).json()["items"]
    assert member_view_items == []  # Members see no row (preserve zero traces).


def test_pending_hidden_from_picker_and_lead_view(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path)
    _create_pending(app)
    # Subagent picker (team_subagents._team_skill_catalog directly calls store).
    from bowhead_gateway.routes.team_subagents import _team_skill_catalog

    assert _run(_team_skill_catalog(store, "team_a")) == []
    # Lead advertisement: effective unified view's team path uses TeamSkillStoreAdapter (default arguments close the gate).
    from bowhead_skill.effective import list_effective_skills

    catalog = SimpleNamespace(list_names=list, list_specs=list, get=lambda name: None)
    ctx = SimpleNamespace(
        user_id="u_member",
        team_id="team_a",
        skill_instance_store=None,
        team_skill_store=TeamSkillStoreAdapter(store),
        granted_team_skill_ids=None,
    )
    assert _run(list_effective_skills(ctx, catalog, include_catalog=False)) == []


def test_admin_create_is_direct_approved_with_writethrough(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path, access=True)
    d = _client(app, ADMIN).post(_BASE, json={"body_md": _skill_md()}).json()
    assert d["approval"] == "approved" and d["approved_by"] == "u_admin" and d["approved_sha256"]
    assert _run(store.get_team_skill_by_name("team_a", "channel-gm-recipe")) is not None
    items = _client(app, MEMBER).get(_BASE).json()["items"]
    assert [i["name"] for i in items] == ["channel-gm-recipe"]
    # Member view has no approval/candidate trace fields (include_review=False).
    assert "submitted_by" not in items[0] and "candidate_state" not in items[0]
    grants = _run(app.state.access_control_store.list_resource_grants("team_a", resource_kind="team_skill"))
    assert any(g.principal_type == "team" and g.effect == "allow" for g in grants)


# ── L1: approve/reject/withdraw/resubmit/reclaim ─────────────────────────────


def test_l1_approve_cas_and_grant(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path, access=True)
    d = _create_pending(app)
    rid, sha = d["id"], d["source_sha256"]
    admin = _client(app, ADMIN)
    r = admin.post(f"{_BASE}/{rid}/approve", json={"source_sha256": "deadbeef"})
    assert r.status_code == 409 and r.json()["detail"]["error"]["code"] == "E_SKILL_CANDIDATE_STALE"
    r = admin.post(f"{_BASE}/{rid}/approve", json={"source_sha256": sha})
    assert r.status_code == 200, r.text
    assert r.json()["approval"] == "approved" and r.json()["approved_sha256"] == sha
    assert _run(store.get_team_skill_by_name("team_a", "channel-gm-recipe")) is not None
    # Defer team grant write-through until approve (no team grant during pending).
    grants = _run(app.state.access_control_store.list_resource_grants("team_a", resource_kind="team_skill"))
    assert any(g.principal_type == "team" and g.effect == "allow" for g in grants)


def test_l1_self_approve_403_member_404(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path)
    rid = _create_pending(app)["id"]
    # Self-approval denied: visible author → 403; invisible member → 404 against enumeration.
    assert _client(app, AUTHOR).post(f"{_BASE}/{rid}/approve", json={}).status_code == 403
    assert _client(app, MEMBER).post(f"{_BASE}/{rid}/approve", json={}).status_code == 404


def test_l1_reject_resubmit_and_reclaim(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path)
    d = _create_pending(app)
    rid, sha = d["id"], d["source_sha256"]
    admin = _client(app, OWNER)
    assert admin.post(f"{_BASE}/{rid}/reject", json={"source_sha256": sha}).status_code == 422  # Reason required.
    r = admin.post(f"{_BASE}/{rid}/reject", json={"source_sha256": sha, "reason": "缺回滚步骤"})
    assert r.status_code == 200 and r.json()["approval"] == "rejected"
    assert r.json()["rejected_reason"] == "缺回滚步骤"
    # L1 rejection keeps visibility team_wide (review P3: FE's 8-state derivation depends on this invariant; if reject flips to
    # creator_only, drawer wrongly displays '未提交' instead of '被拒'; one assertion pins this).
    assert _run(store.get_team_skill("team_a", rid)).visibility == "team_wide"
    # rejected visible only to author+admin.
    assert _client(app, MEMBER).get(f"{_BASE}/{rid}").status_code == 404
    assert _client(app, AUTHOR).get(f"{_BASE}/{rid}").json()["rejected_reason"] == "缺回滚步骤"
    # L1 resubmit (store transition; promote route in P2) → pending, rejection reason cleared.
    assert _run(store.resubmit_team_skill("team_a", rid, submitted_by="u_author"))
    row = _run(store.get_team_skill("team_a", rid))
    assert row.approval == "pending" and row.rejected_reason is None
    # After another rejection, author reclaims (PATCH→creator_only, second-round P2-7) → personal use.
    admin.post(f"{_BASE}/{rid}/reject", json={"source_sha256": sha, "reason": "again"})
    r = _client(app, AUTHOR).patch(f"{_BASE}/{rid}", json={"visibility": "creator_only"})
    assert r.status_code == 200, r.text
    row = _run(store.get_team_skill("team_a", rid))
    assert row.visibility == "creator_only" and row.approval == "approved" and row.rejected_reason is None


def test_l1_withdraw(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path)
    rid = _create_pending(app)["id"]
    assert _client(app, ADMIN).post(f"{_BASE}/{rid}/withdraw").status_code == 403  # Not author.
    assert _client(app, MEMBER).post(f"{_BASE}/{rid}/withdraw").status_code == 404  # Invisible.
    r = _client(app, AUTHOR).post(f"{_BASE}/{rid}/withdraw")
    assert r.status_code == 200, r.text
    row = _run(store.get_team_skill("team_a", rid))
    assert row.visibility == "creator_only" and row.approval == "approved" and row.submitted_by is None


# ── Gate 2: PATCH visibility transitions ────────────────────────────────────


def test_patch_gate2_submit_and_admin_direct(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path, access=True)
    # creator_only bypasses approval, immediately usable personally (default approved).
    d = _client(app, AUTHOR).post(_BASE, json={"body_md": _skill_md(), "visibility": "creator_only"}).json()
    assert d["approval"] == "approved"
    rid = d["id"]
    assert _run(store.get_team_skill_by_name("team_a", "channel-gm-recipe")) is not None
    # Gate 2: author PATCH → team_wide = L1 submission.
    r = _client(app, AUTHOR).patch(f"{_BASE}/{rid}", json={"visibility": "team_wide"})
    assert r.status_code == 200 and r.json()["approval"] == "pending"
    assert _run(store.get_team_skill_by_name("team_a", "channel-gm-recipe")) is None  # Invisible on team surface.
    # Admin creator_only row PATCH → team_wide = direct approval + write-through.
    d2 = (
        _client(app, ADMIN)
        .post(_BASE, json={"body_md": _skill_md(name="admin-recipe", desc="口径审查 触发：口径"), "visibility": "creator_only"})
        .json()
    )
    r = _client(app, ADMIN).patch(f"{_BASE}/{d2['id']}", json={"visibility": "team_wide"})
    assert r.status_code == 200, r.text
    assert r.json()["approval"] == "approved"
    grants = _run(app.state.access_control_store.list_resource_grants("team_a", resource_kind="team_skill"))
    assert any(g.principal_type == "team" and g.resource_id == d2["id"] for g in grants)


def test_patch_approved_metadata_and_unpublish_admin_only(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path, access=True)
    ap = _approved_skill(app, store)
    rid = ap["id"]
    # Approved team_wide metadata (feeds Lead routing): author → 403; admin → 200 (review P2-14).
    assert _client(app, AUTHOR).patch(f"{_BASE}/{rid}", json={"display_name": "新名"}).status_code == 403
    assert _client(app, ADMIN).patch(f"{_BASE}/{rid}", json={"display_name": "新名"}).status_code == 200
    # Delist: author → 403 (cannot delist approved assets); admin → set_audience(custom) writes through creator_only.
    assert _client(app, AUTHOR).patch(f"{_BASE}/{rid}", json={"visibility": "creator_only"}).status_code == 403
    r = _client(app, ADMIN).patch(f"{_BASE}/{rid}", json={"visibility": "creator_only"})
    assert r.status_code == 200, r.text
    row = _run(store.get_team_skill("team_a", rid))
    assert row.visibility == "creator_only" and row.approval == "approved"
    grants = _run(app.state.access_control_store.list_resource_grants("team_a", resource_kind="team_skill", resource_id=rid))
    assert not any(g.principal_type == "team" and g.revoked_at is None for g in grants)


# ── Gate 3 + fourth entry: lower set_audience/add_grant gates ────────────────


def test_gate3_set_audience_service_level(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path, access=True)
    acs = app.state.access_control_store
    rid = _create_pending(app)["id"]
    # pending row → raise (put_audience / fourth admin-grants-console entry share this gate).
    with pytest.raises(share_service.SkillApprovalRequiredError):
        _run(
            share_service.set_audience(
                acs, kind="team_skill", resource_id=rid, team_id="team_a", target="team_wide", actor_id="u_owner", user_store=store
            )
        )
    # Also reject broadcast for creator_only rows (always approved for personal use); set_audience is not a transition entry.
    d2 = (
        _client(app, AUTHOR)
        .post(_BASE, json={"body_md": _skill_md(name="own-recipe", desc="私有流程 触发：私有"), "visibility": "creator_only"})
        .json()
    )
    with pytest.raises(share_service.SkillApprovalRequiredError):
        _run(
            share_service.set_audience(
                acs, kind="team_skill", resource_id=d2["id"], team_id="team_a", target="team_wide", actor_id="u_author", user_store=store
            )
        )
    # Missing user_store also raises (fail-closed structural guard).
    with pytest.raises(share_service.SkillApprovalRequiredError):
        _run(share_service.set_audience(acs, kind="team_skill", resource_id=rid, team_id="team_a", target="team_wide", actor_id="u_owner"))


def _mount_shares(app, monkeypatch):
    from bowhead_gateway.routes import shares as shr

    monkeypatch.setattr(shr, "workflow_creator_enabled", lambda: True)
    app.include_router(shr.router, prefix="/api/v1/shares")


def test_gate3_put_audience_route_422(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path, access=True)
    _mount_shares(app, monkeypatch)
    rid = _create_pending(app)["id"]
    r = _client(app, AUTHOR).put(f"/api/v1/shares/team_skill/{rid}/audience", json={"audience": "team_wide"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"]["code"] == "E_SKILL_APPROVAL_REQUIRED"


def test_shares_recipient_view_hides_pending_from_member(tmp_path, monkeypatch):
    """安全 review P2-B：pending team_skill 的 visibility 列仍是 team_wide,shares recipient
    视图 _audience_of 只看 visibility → 曾对任意成员返 200 泄露 name+owner。"""
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path, access=True)
    _mount_shares(app, monkeypatch)
    rid = _create_pending(app)["id"]
    # Member GET shares → 404, zero traces (no label/owner leak).
    assert _client(app, MEMBER).get(f"/api/v1/shares/team_skill/{rid}").status_code == 404
    # owner/admin (manager) still visible.
    assert _client(app, OWNER).get(f"/api/v1/shares/team_skill/{rid}").status_code == 200
    # Visible to members after approval (broadcast).
    sha = _run(store.get_team_skill("team_a", rid)).approved_sha256 or _client(app, OWNER).get(f"{_BASE}/{rid}").json()["source_sha256"]
    _client(app, ADMIN).post(f"{_BASE}/{rid}/approve", json={"source_sha256": sha})
    assert _client(app, MEMBER).get(f"/api/v1/shares/team_skill/{rid}").status_code == 200


def test_shares_downgrade_of_approved_team_wide_is_admin_only(tmp_path, monkeypatch):
    """安全 review P2-C：已批 team_wide 下架/降级 admin-only——挡住 owner 短路让非 admin
    作者经 put_audience(custom/private) 绕过 team_skills PATCH 的下架门。"""
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path, access=True)
    _mount_shares(app, monkeypatch)
    rid = _approved_skill(app, store)["id"]  # AUTHOR submits, admin approves → team_wide+approved.
    # Author (skill owner, not team admin) downgrades via shares → 403.
    for audience in ("custom", "private"):
        r = _client(app, AUTHOR).put(f"/api/v1/shares/team_skill/{rid}/audience", json={"audience": audience})
        assert r.status_code == 403
        # A registered, localized code rather than an English sentence the UI would echo verbatim.
        assert r.json()["detail"]["code"] == "team_skill_audience_admin_required", r.text
    assert _run(store.get_team_skill("team_a", rid)).visibility == "team_wide"  # Not delisted.
    # Team admin may downgrade.
    assert _client(app, ADMIN).put(f"/api/v1/shares/team_skill/{rid}/audience", json={"audience": "custom"}).status_code == 200
    assert _run(store.get_team_skill("team_a", rid)).visibility == "creator_only"


def test_create_disk_write_serialized_with_promote_lock(tmp_path, monkeypatch):
    """正确性 review P2：create 盘写取 per-skill 锁,与同名 promote 的 copy_validated_bundle
    (overwrite=True) 互斥。并发 clobber 难在单测复现,此处锁定关键机制:peek 出与 promote
    一致的 name → 同 (team,name) 取到同一把锁 → 两者串行不会互删盘。"""
    from bowhead_core.skill_upload import peek_skill_name
    from bowhead_gateway._team_skill_locks import team_skill_lock

    assert peek_skill_name(_skill_md(name="channel-gm-recipe")) == "channel-gm-recipe"
    assert peek_skill_name("no frontmatter at all") is None  # → nullcontext, no disk write.
    # Same key, same lock (create/promote share import); different name/team, different locks.
    assert team_skill_lock("team_a", "foo") is team_skill_lock("team_a", "foo")
    assert team_skill_lock("team_a", "foo") is not team_skill_lock("team_a", "bar")
    # Same-name promote after create (not the actor's same-name row) takes name-conflict path, not clobber (no regression).
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    _client(app, AUTHOR).post(_BASE, json={"body_md": _skill_md(), "visibility": "creator_only"})  # Create creator_only team row.
    _personal_skill(tmp_path, app)  # Author's same-name personal skill.
    r = _promote(app, AUTHOR)  # Own creator_only → L1 submission path.
    assert r.status_code == 200 and r.json()["approval"] == "pending"


def test_pending_named_share_rejected(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path, access=True)
    acs = app.state.access_control_store
    rid = _create_pending(app)["id"]
    with pytest.raises(share_service.SkillApprovalRequiredError):
        _run(
            share_service.add_grant(
                acs,
                kind="team_skill",
                resource_id=rid,
                team_id="team_a",
                principal_type="user",
                principal_id="u_member",
                actor_id="u_author",
                user_store=store,
            )
        )
    # Shared checkpoint for direct admin-console calls.
    with pytest.raises(share_service.SkillApprovalRequiredError):
        _run(share_service.ensure_team_skill_grantable(store, team_id="team_a", resource_id=rid))
    # creator_only approved rows still allow explicit grants (PRD residual: retain explicit path, audit covered).
    d2 = (
        _client(app, AUTHOR)
        .post(_BASE, json={"body_md": _skill_md(name="own-recipe", desc="私有流程 触发：私有"), "visibility": "creator_only"})
        .json()
    )
    g = _run(
        share_service.add_grant(
            acs,
            kind="team_skill",
            resource_id=d2["id"],
            team_id="team_a",
            principal_type="user",
            principal_id="u_member",
            actor_id="u_author",
            user_store=store,
        )
    )
    assert g.effect == "allow"


# ── L2: full candidate-slot lifecycle ──────────────────────────────────────


def test_l2_submit_reject_resubmit_withdraw_live_untouched(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path, access=True)
    rid = _approved_skill(app, store)["id"]
    cand, sha, ts = _submit_candidate(store, rid)
    row = _run(store.get_team_skill("team_a", rid))
    assert row.approval == "approved" and row.candidate_state == "pending"
    # Core invariant: pending updates do not hide live (only_approved surface still returns it).
    assert _run(store.get_team_skill_by_name("team_a", row.name)) is not None
    items = _client(app, MEMBER).get(_BASE).json()["items"]
    assert len(items) == 1 and "candidate_state" not in items[0]  # No candidate traces for members.
    # L2 reject: candidate_state=rejected + reason; live unchanged, still runs.
    r = _client(app, ADMIN).post(f"{_BASE}/{rid}/reject", json={"candidate_submitted_at": ts, "candidate_sha256": sha, "reason": "改口径"})
    assert r.status_code == 200, r.text
    row = _run(store.get_team_skill("team_a", rid))
    assert row.candidate_state == "rejected" and row.candidate_rejected_reason == "改口径"
    assert row.approval == "approved" and _run(store.get_team_skill_by_name("team_a", row.name)) is not None
    # L2 resubmit (overwrite candidate): pending again, candidate rejection reason cleared.
    cand, sha, ts = _submit_candidate(store, rid, marker="v3")
    row = _run(store.get_team_skill("team_a", rid))
    assert row.candidate_state == "pending" and row.candidate_rejected_reason is None
    # L2 withdraw: clear candidate slot only, leaving live/visibility unchanged (must not delist approved assets).
    r = _client(app, AUTHOR).post(f"{_BASE}/{rid}/withdraw")
    assert r.status_code == 200, r.text
    row = _run(store.get_team_skill("team_a", rid))
    assert row.candidate_path is None and row.visibility == "team_wide" and row.approval == "approved"
    assert not cand.exists()  # Candidate directory cleaned.


def test_l2_approve_swaps_disk_with_double_cas(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path)
    rid = _approved_skill(app, store)["id"]
    cand, sha, ts = _submit_candidate(store, rid, marker="v2")
    admin = _client(app, ADMIN)
    # In-memory CAS: old key → 409 (author overwrites candidate during admin review).
    r = admin.post(f"{_BASE}/{rid}/approve", json={"candidate_submitted_at": "1999-01-01T00:00:00", "candidate_sha256": sha})
    assert r.status_code == 409 and r.json()["detail"]["error"]["code"] == "E_SKILL_CANDIDATE_STALE"
    # Direct DB CAS test (WHERE both candidate keys; 0 rows → False).
    assert not _run(
        store.approve_team_skill(
            "team_a", rid, approved_by="u_admin", approved_sha256=sha, expected_candidate_submitted_at="1999", expected_candidate_sha256=sha
        )
    )
    # Correct keys → disk replacement + empty slot + approved_sha256=candidate hash.
    r = admin.post(f"{_BASE}/{rid}/approve", json={"candidate_submitted_at": ts, "candidate_sha256": sha})
    assert r.status_code == 200, r.text
    row = _run(store.get_team_skill("team_a", rid))
    assert row.approved_sha256 == sha and row.candidate_path is None and row.candidate_state is None
    src = Path(row.source_path)
    assert "v2" in (src / "SKILL.md").read_text(encoding="utf-8")
    assert not cand.exists() and not (src.parent / f"{row.name}__old").exists()
    assert bundle_sha256(src) == sha


def test_l2_rejected_candidate_cannot_be_approved(tmp_path, monkeypatch):
    """CTO review 修复回归：reject 后 CAS 键不变——缺 candidate_state 守卫时 admin 可用旧键把被拒候选转正."""
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path)
    rid = _approved_skill(app, store)["id"]
    cand, sha, ts = _submit_candidate(store, rid)
    admin = _client(app, ADMIN)
    r = admin.post(f"{_BASE}/{rid}/reject", json={"candidate_submitted_at": ts, "candidate_sha256": sha, "reason": "no"})
    assert r.status_code == 200
    # Route dispatch guard: approve rejected candidate → 409 (no disk replacement).
    r = admin.post(f"{_BASE}/{rid}/approve", json={"candidate_submitted_at": ts, "candidate_sha256": sha})
    assert r.status_code == 409
    # DB guard: WHERE candidate_state='pending' → 0 rows.
    assert not _run(
        store.approve_team_skill(
            "team_a", rid, approved_by="u_admin", approved_sha256=sha, expected_candidate_submitted_at=ts, expected_candidate_sha256=sha
        )
    )
    row = _run(store.get_team_skill("team_a", rid))
    assert row.candidate_state == "rejected"  # Retain candidate for author viewing/resubmission.


def test_metadata_frozen_while_pending(tmp_path, monkeypatch):
    """安全 review 修复回归：pending 窗口元数据冻结——CAS 不盖 description，否则作者可在
    admin 打开审批卡后调包路由文案（bait-and-switch）."""
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path)
    d = _create_pending(app)
    rid, sha = d["id"], d["source_sha256"]
    assert _client(app, AUTHOR).patch(f"{_BASE}/{rid}", json={"description": "换个说法 触发：全部"}).status_code == 403
    assert _client(app, ADMIN).patch(f"{_BASE}/{rid}", json={"description": "admin 修订 触发：口径"}).status_code == 200
    # Author can edit while rejected (admin sees again after resubmission).
    _client(app, OWNER).post(f"{_BASE}/{rid}/reject", json={"source_sha256": sha, "reason": "r"})
    assert _client(app, AUTHOR).patch(f"{_BASE}/{rid}", json={"description": "改后重提 触发：口径"}).status_code == 200


def test_admin_unpublish_with_candidate_cleans_slot(tmp_path, monkeypatch):
    """正确性 review 修复回归：带候选的已批行被 admin 下架 → 候选槽+目录一并清（无孤儿）."""
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path, access=True)
    rid = _approved_skill(app, store)["id"]
    cand, _sha, _ts = _submit_candidate(store, rid)
    r = _client(app, ADMIN).patch(f"{_BASE}/{rid}", json={"visibility": "creator_only"})
    assert r.status_code == 200, r.text
    row = _run(store.get_team_skill("team_a", rid))
    assert row.visibility == "creator_only" and row.candidate_path is None
    assert not cand.exists()


def test_l2_candidate_integrity_gate(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path)
    rid = _approved_skill(app, store)["id"]
    cand, sha, ts = _submit_candidate(store, rid)
    (cand / "SKILL.md").unlink()  # Disk damage: candidate lacks SKILL.md.
    r = _client(app, ADMIN).post(f"{_BASE}/{rid}/approve", json={"candidate_submitted_at": ts, "candidate_sha256": sha})
    assert r.status_code == 409  # Prevalidation → 409 '候选不完整', not 500.
    # Assert specific wording, not only status_code (review P2: bypassing completeness into stale-CAS would otherwise also pass).
    assert "incomplete" in str(r.json().get("detail", "")).lower()


# ── Three crash windows (construct disk state, verify precleanup recovery) ──


def test_crash_windows_recovery(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path)
    rid = _approved_skill(app, store)["id"]
    row = _run(store.get_team_skill("team_a", rid))
    src = Path(row.source_path)
    old = src.parent / f"{row.name}__old"
    admin = _client(app, ADMIN)

    # Window A: crash halfway through disk replacement (approved→__old renamed, candidate not promoted).
    # __old is sole live copy; precleanup must restore before cleanup.
    cand, sha, ts = _submit_candidate(store, rid, marker="v2")
    src.rename(old)
    r = admin.post(f"{_BASE}/{rid}/approve", json={"candidate_submitted_at": ts, "candidate_sha256": sha})
    assert r.status_code == 200, r.text
    assert "v2" in (src / "SKILL.md").read_text(encoding="utf-8") and not old.exists()

    # Window B: disk replacement done, crash before DB update → replay approve idempotently completes DB.
    cand, sha, ts = _submit_candidate(store, rid, marker="v3")
    src.rename(old)
    cand.rename(src)
    shutil.rmtree(old)
    r = admin.post(f"{_BASE}/{rid}/approve", json={"candidate_submitted_at": ts, "candidate_sha256": sha})
    assert r.status_code == 200, r.text
    row = _run(store.get_team_skill("team_a", rid))
    assert row.candidate_path is None and row.approved_sha256 == sha
    assert "v3" in (src / "SKILL.md").read_text(encoding="utf-8")

    # Window C: only __old remains → next approve precleanup removes it.
    cand, sha, ts = _submit_candidate(store, rid, marker="v4")
    old.mkdir()
    (old / "junk.txt").write_text("stale", encoding="utf-8")
    r = admin.post(f"{_BASE}/{rid}/approve", json={"candidate_submitted_at": ts, "candidate_sha256": sha})
    assert r.status_code == 200, r.text
    assert not old.exists() and "v4" in (src / "SKILL.md").read_text(encoding="utf-8")


# ── DELETE cleans candidates/residue ───────────────────────────────────────


def test_delete_cleans_candidate_and_old_dirs(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path)
    rid = _approved_skill(app, store)["id"]
    cand, _sha, _ts = _submit_candidate(store, rid)
    row = _run(store.get_team_skill("team_a", rid))
    src = Path(row.source_path)
    old = src.parent / f"{row.name}__old"
    old.mkdir()
    assert _client(app, AUTHOR).delete(f"{_BASE}/{rid}").status_code == 204
    assert not src.exists() and not cand.exists() and not old.exists()


# ── Store transition prior-state guards (§1 transition-table negatives: WHERE is CAS) ──


def test_transition_state_guards_store_level(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path)
    rid = _create_pending(app)["id"]
    # L1 resubmit awaiting approval: pending stays pending (CAS key=disk hash refreshes naturally, round 2 P2-7).
    assert _run(store.resubmit_team_skill("team_a", rid, submitted_by="u_author"))
    assert _run(store.get_team_skill("team_a", rid)).approval == "pending"
    # Prior-state guard: pending cannot submit L2 candidate (candidate path only for approved rows).
    assert _run(store.submit_team_skill_candidate("team_a", rid, candidate_path="/x", candidate_sha256="s", submitted_by="u")) is None
    # submit_team_skill accepts only creator_only+approved prior state.
    assert not _run(store.submit_team_skill("team_a", rid, submitted_by="u_author"))
    # After L1 approve: repeated approve / reject / L1 resubmit all blocked by prior state.
    sha = oskr._source_sha256(_run(store.get_team_skill("team_a", rid)))
    assert _run(store.approve_team_skill("team_a", rid, approved_by="u_admin", approved_sha256=sha))
    assert not _run(store.approve_team_skill("team_a", rid, approved_by="u_admin", approved_sha256=sha))
    assert not _run(store.reject_team_skill("team_a", rid, reason="r"))
    assert not _run(store.resubmit_team_skill("team_a", rid, submitted_by="u_author"))
    # Approved with no candidate, withdraw → None (nothing to withdraw).
    assert _run(store.withdraw_team_skill("team_a", rid)) is None
    # creator_only cannot create candidate slots (visibility guard prevents creator_only+candidate anomalies).
    d2 = (
        _client(app, AUTHOR)
        .post(_BASE, json={"body_md": _skill_md(name="own-recipe", desc="私有流程 触发：私有"), "visibility": "creator_only"})
        .json()
    )
    assert _run(store.submit_team_skill_candidate("team_a", d2["id"], candidate_path="/x", candidate_sha256="s", submitted_by="u")) is None


# ── skill_invoke.audit origin=team (PRD acceptance 9) ───────────────────────


def test_skill_invoke_audit_origin_team(tmp_path, monkeypatch, caplog):
    # Classify origin by source_dir location (.data/team_skills); chdir+default relative root yields production-shaped paths.
    monkeypatch.chdir(tmp_path)
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path)
    monkeypatch.setattr(oskr, "_TEAM_SKILLS_ROOT", Path(".data") / "team_skills")
    rid = _approved_skill(app, store)["id"]
    row = _run(store.get_team_skill("team_a", rid))
    from bowhead_skill.tools import build_skill_tools

    monkeypatch.delenv("BOWHEAD_SKILL_MULTIRUNTIME", raising=False)  # off → fail-closed execution denial.
    catalog = SimpleNamespace(list_names=list, list_specs=list, get=lambda name: None)
    tools = {t.name: t for t in build_skill_tools(catalog, runtime=SimpleNamespace())}
    ctx = SimpleNamespace(
        user_id="u_member",
        team_id="team_a",
        workspace_id="ws_1",
        skill_instance_store=None,
        team_skill_store=TeamSkillStoreAdapter(store),
        granted_team_skill_ids=None,
    )
    with caplog.at_level(logging.INFO, logger="bowhead_skill.tools"):
        out = _run(tools["skill_invoke"].handler({"name": row.name, "script": "x.py"}, ctx))
    assert "error" in out  # multiruntime off → deny (audit also recorded on denial).
    audit_lines = [rec.getMessage() for rec in caplog.records if "skill_invoke.audit" in rec.getMessage()]
    assert audit_lines and "origin=team" in audit_lines[-1]


# ── Separate notification keys + recipient table ───────────────────────────


def test_notifications_split_keys_and_recipients(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path, notify=True)
    d = _create_pending(app)
    rid, sha = d["id"], d["source_sha256"]
    db = str(tmp_path / "u.db")

    def _items():
        async def q():
            async with aiosqlite.connect(db) as conn:
                conn.row_factory = aiosqlite.Row
                rows = await conn.execute_fetchall(
                    "SELECT i.dedupe_key AS k, i.type AS t, r.user_id AS u, i.action_url AS url"
                    " FROM notification_items i JOIN notification_recipients r ON r.notification_id = i.id"
                )
                return [(r["k"], r["t"], r["u"], r["url"]) for r in rows]

        return _run(q())

    submitted = [x for x in _items() if x[1] == "skill_approval_submitted"]
    assert {x[2] for x in submitted} == {"u_owner", "u_admin"}  # → admins; no author/member.
    assert all(x[0].startswith(f"skill_approval:submitted:{rid}:") for x in submitted)
    assert {x[3] for x in submitted} == {"/team/settings/workflows"}
    _client(app, ADMIN).post(f"{_BASE}/{rid}/approve", json={"source_sha256": sha})
    decided = [x for x in _items() if x[1] == "skill_approval_decided"]
    assert {x[2] for x in decided} == {"u_author"}  # → author.
    assert decided[0][0].startswith(f"skill_approval:decided:{rid}:")
    assert decided[0][3] == "/skills/"  # The author's Skills page, not the legacy /settings/skills redirect.
    assert len({x[0] for x in _items()}) == 2  # Separate keys: decision never overwrites admin's pending item.


def test_reject_notification_carries_reason(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path, notify=True)
    d = _create_pending(app)
    _client(app, OWNER).post(f"{_BASE}/{d['id']}/reject", json={"source_sha256": d["source_sha256"], "reason": "脚本未做超时防护"})

    async def q():
        async with aiosqlite.connect(str(tmp_path / "u.db")) as conn:
            conn.row_factory = aiosqlite.Row
            rows = await conn.execute_fetchall("SELECT title FROM notification_items WHERE type='skill_approval_decided'")
            return [r["title"] for r in rows]

    titles = _run(q())
    assert titles and "被拒绝" in titles[0] and "脚本未做超时防护" in titles[0]


def test_direct_team_skill_share_notification_links_to_skills_page(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path, access=True, notify=True)
    _mount_shares(app, monkeypatch)
    skill = (
        _client(app, AUTHOR)
        .post(_BASE, json={"body_md": _skill_md(name="own-recipe", desc="Private recipe for shared channel checks"), "visibility": "creator_only"})
        .json()
    )

    r = _client(app, AUTHOR).post(f"/api/v1/shares/team_skill/{skill['id']}/grants", json={"principal_type": "user", "principal_id": MEMBER.id})
    assert r.status_code == 201, r.text

    async def q():
        async with aiosqlite.connect(str(tmp_path / "u.db")) as conn:
            rows = await conn.execute_fetchall("SELECT action_url FROM notification_items WHERE type='content_shared'")
            return [row[0] for row in rows]

    # The recipient opens the shared skill on the Skills page, not the legacy redirect.
    assert _run(q()) == ["/skills/"]


# ── P2: three-plus promote branches / copy-to-personal ──────────────────────


def _personal_skill(tmp_path, app, uid="u_author", name="channel-gm-recipe", marker="v1", with_ref=True):
    """建真实个人 bundle（含 references/，P1-5 关键回归面）+ skill_instances 行."""
    d = tmp_path / ".data" / "user_skills" / uid / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_skill_md(name=name, body=f"## 执行步骤 {marker}\n1. 个人版\n"), encoding="utf-8")
    if with_ref:
        (d / "references").mkdir(exist_ok=True)
        (d / "references" / "notes.md").write_text("口径备注", encoding="utf-8")
    sis = app.state.skill_instance_store
    _run(
        sis.upsert(
            user_id=uid,
            name=name,
            source_type="user_generated",
            source_path=str(d),
            description=f"个人描述 {marker} 触发：毛利",
            enabled=True,
        )
    )
    return d


def _promote(app: FastAPI, user: User, name="channel-gm-recipe"):
    """Submit exactly what the skill drawer submits: title/body_md from GET /skills/{name}.

    Building the payload with the server's own loader hid a parser mismatch (blank line after
    the closing ``---``) that made every such promote fail with skill_promote_content_stale.
    """
    client = _client(app, user)
    detail = client.get(f"/api/v1/skills/{name}")
    assert detail.status_code == 200, detail.text
    d = detail.json()
    return client.post(
        "/api/v1/skills/promote",
        json={"name": name, "title": d["title"] or name, "content": d["body_md"]},
    )


def _promote_app(tmp_path, monkeypatch, store, **kw):
    monkeypatch.chdir(tmp_path)  # Personal bundle validation uses CWD-relative .data.
    from bowhead_gateway.routes import skills as skill_routes

    async def _no_subagents(*_args, **_kwargs):
        return []

    # Skill detail (read by _promote like the drawer) projects subagent usage, which needs a resource-usage service.
    monkeypatch.setattr(skill_routes, "_used_by_subagents", _no_subagents)
    monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", "1")
    app = _app(store, monkeypatch, tmp_path, personal=True, **kw)
    # Real error catalog: also test coded 422/409 message rendering (including masked author text).
    from bowhead_core.error_catalog import load_error_catalog

    repo_root = Path(__file__).resolve().parents[3]
    with contextlib.suppress(Exception):
        app.state.error_catalog = load_error_catalog(repo_root / "config" / "i18n" / "errors")
    return app


def test_promote_flag_off_404(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", "0")  # Default now ON; explicitly 0 to test flag-off 404.
    app = _app(store, monkeypatch, tmp_path, personal=True)
    _personal_skill(tmp_path, app)
    assert _promote(app, AUTHOR).status_code == 404


def test_promote_l1_first_submission_preserves_references(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    _personal_skill(tmp_path, app)
    r = _promote(app, AUTHOR)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["approval"] == "pending" and d["visibility"] == "team_wide" and d["submitted_by"] == "u_author"
    row = _run(store.get_team_skill("team_a", d["id"]))
    # Whole-directory snapshot preserves references/ (reason validate_and_save_generated dest semantics forbidden, review P1-5).
    assert (Path(row.source_path) / "references" / "notes.md").read_text(encoding="utf-8") == "口径备注"
    assert row.description.startswith("个人描述 v1")  # Personal DB override feeds team row.
    # Hidden on team surface (default argument).
    assert _run(store.get_team_skill_by_name("team_a", "channel-gm-recipe")) is None


def test_promote_accepts_blank_line_after_frontmatter(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    d = _personal_skill(tmp_path, app)
    (d / "SKILL.md").write_text(_skill_md(body="\n## 执行步骤\n1. 取数\n"), encoding="utf-8")
    r = _promote(app, AUTHOR)
    assert r.status_code == 200, r.text
    assert r.json()["approval"] == "pending"


def test_resubmission_and_update_refresh_title_and_risk_for_reviewers(tmp_path, monkeypatch):
    """Review evidence (risk chips, title) follows the content under review, not the first submission."""
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", "1")  # Risk reports are produced only with multi-runtime validation.
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    d = _personal_skill(tmp_path, app)
    manifest = d / "SKILL.md"

    def write(title: str, body: str) -> None:
        manifest.write_text(f"---\nname: channel-gm-recipe\ndescription: d\ntitle: {title}\n---\n{body}", encoding="utf-8")

    write("Old Title", "## Steps\n1. read\n")
    rid = _promote(app, AUTHOR).json()["id"]
    write("New Title", "## Steps\n1. rm -rf /tmp/x\n")
    r = _promote(app, AUTHOR)  # L1 resubmit while pending.
    assert r.status_code == 200, r.text
    row = _run(store.get_team_skill("team_a", rid))
    assert row.display_name == "New Title"
    assert "rm -rf" in json.loads(row.risk_report_json)["keywords"]

    sha = _client(app, ADMIN).get(f"{_BASE}/{rid}").json()["source_sha256"]
    assert _client(app, ADMIN).post(f"{_BASE}/{rid}/approve", json={"source_sha256": sha}).status_code == 200
    write("Newer Title", "## Steps\n1. sudo reboot\n")
    assert _promote(app, AUTHOR).status_code == 200  # L2 update candidate.
    detail = _client(app, ADMIN).get(f"{_BASE}/{rid}").json()
    assert "sudo " in detail["candidate_risk_report"]["keywords"]
    assert "sudo " not in detail["risk_report"]["keywords"]  # Live row still describes the approved content.
    r = _client(app, ADMIN).post(
        f"{_BASE}/{rid}/approve", json={"candidate_submitted_at": detail["candidate_submitted_at"], "candidate_sha256": detail["candidate_sha256"]}
    )
    assert r.status_code == 200, r.text
    row = _run(store.get_team_skill("team_a", rid))
    assert row.display_name == "Newer Title"
    assert json.loads(row.risk_report_json)["keywords"] == ["sudo "]


def test_update_approval_keeps_an_admin_renamed_display_name(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    d = _personal_skill(tmp_path, app)
    manifest = d / "SKILL.md"
    manifest.write_text("---\nname: channel-gm-recipe\ndescription: d\ntitle: First\n---\nbody\n", encoding="utf-8")
    rid = _promote(app, AUTHOR).json()["id"]
    sha = _client(app, ADMIN).get(f"{_BASE}/{rid}").json()["source_sha256"]
    assert _client(app, ADMIN).post(f"{_BASE}/{rid}/approve", json={"source_sha256": sha}).status_code == 200
    _run(store.update_team_skill_meta("team_a", rid, display_name="Team naming"))
    manifest.write_text("---\nname: channel-gm-recipe\ndescription: d\ntitle: Second\n---\nbody v2\n", encoding="utf-8")
    assert _promote(app, AUTHOR).status_code == 200
    detail = _client(app, ADMIN).get(f"{_BASE}/{rid}").json()
    r = _client(app, ADMIN).post(
        f"{_BASE}/{rid}/approve", json={"candidate_submitted_at": detail["candidate_submitted_at"], "candidate_sha256": detail["candidate_sha256"]}
    )
    assert r.status_code == 200, r.text
    assert _run(store.get_team_skill("team_a", rid)).display_name == "Team naming"


def test_delete_personal_skill_uses_persistent_id(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    _personal_skill(tmp_path, app)
    inst = _run(app.state.skill_instance_store.get(user_id=AUTHOR.id, name="channel-gm-recipe"))
    assert inst is not None

    # Name no longer identifies deletion URL resources; only current user's persisted instance ID may be deleted.
    assert _client(app, AUTHOR).delete("/api/v1/skills/channel-gm-recipe").status_code == 404
    response = _client(app, AUTHOR).delete(f"/api/v1/skills/{inst.id}")
    assert response.status_code == 204, response.text
    assert _run(app.state.skill_instance_store.get_by_id(user_id=AUTHOR.id, skill_id=inst.id)) is None


def test_promote_uses_fixed_path_and_rejects_stale_body(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    _personal_skill(tmp_path, app)

    # Old dynamic URL no longer creates/submits.
    assert _client(app, AUTHOR).post("/api/v1/skills/channel-gm-recipe/promote").status_code == 404

    response = _client(app, AUTHOR).post(
        "/api/v1/skills/promote",
        json={"name": "channel-gm-recipe", "title": "channel-gm-recipe", "content": "## 执行步骤\n1. 旧内容\n"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "skill_promote_content_stale"


def test_promote_name_taken_by_other_422(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    _create_pending(app, author=MEMBER)  # Same-name team row created_by=u_member (invisible to u_author).
    _personal_skill(tmp_path, app)  # u_author's same-name personal skill.
    r = _promote(app, AUTHOR)
    assert r.status_code == 422, r.text
    err = r.json()["detail"]["error"]
    assert err["code"] == "E_SKILL_NAME_TAKEN_BY_OTHER"
    # Mask author ownership (security review P2): conflicting row invisible to requester (pending)→do not leak author name.
    assert "u_member" not in str(err) and "member@x.com" not in str(err)
    # No author parameter at all: a hard-coded localized placeholder used to leak into every locale.
    assert err["name"] == "channel-gm-recipe" and "author" not in err, err
    assert err["diagnostics"]["diagnostic_code"] == "skill_workflow_failure"


def test_promote_l1_resubmit_after_reject(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    _personal_skill(tmp_path, app)
    d = _promote(app, AUTHOR).json()
    rid, sha = d["id"], d["source_sha256"]
    _client(app, OWNER).post(f"{_BASE}/{rid}/reject", json={"source_sha256": sha, "reason": "补充回滚"})
    # Author edits personal version, then promote=L1 resubmit: pending, reason cleared, content overwritten, old CAS key invalid.
    _personal_skill(tmp_path, app, marker="v2")
    r = _promote(app, AUTHOR)
    assert r.status_code == 200, r.text
    row = _run(store.get_team_skill("team_a", rid))
    assert row.approval == "pending" and row.rejected_reason is None
    assert "v2" in (Path(row.source_path) / "SKILL.md").read_text(encoding="utf-8")
    assert r.json()["source_sha256"] != sha  # Pending/resubmission refresh CAS key → old admin key 409.


def test_promote_creator_only_leg_is_l1_submit(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    # Author first creates creator_only team row (solidification directly selecting team/create), then promotes same-name personal version.
    _client(app, AUTHOR).post(_BASE, json={"body_md": _skill_md(), "visibility": "creator_only"})
    _personal_skill(tmp_path, app, marker="v9")
    r = _promote(app, AUTHOR)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["approval"] == "pending" and d["visibility"] == "team_wide"
    row = _run(store.get_team_skill("team_a", d["id"]))
    assert row.candidate_path is None  # No candidate slot (r3 P1-2: no creator_only+candidate).
    assert "v9" in (Path(row.source_path) / "SKILL.md").read_text(encoding="utf-8")


def test_promote_l2_candidate_and_admin_direct(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    _personal_skill(tmp_path, app)
    d = _promote(app, AUTHOR).json()
    rid = d["id"]
    _client(app, ADMIN).post(f"{_BASE}/{rid}/approve", json={"source_sha256": d["source_sha256"]})
    # L2: author edits personal version → promote submits candidate; live unchanged.
    _personal_skill(tmp_path, app, marker="v2")
    r = _promote(app, AUTHOR)
    assert r.status_code == 200, r.text
    d2 = r.json()
    assert d2["approval"] == "approved" and d2["candidate_state"] == "pending"
    row = _run(store.get_team_skill("team_a", rid))
    assert "v1" in (Path(row.source_path) / "SKILL.md").read_text(encoding="utf-8")  # live still old version.
    # Admin approves using response candidate keys → disk replacement + references retained + description refreshed on approval.
    r = _client(app, ADMIN).post(
        f"{_BASE}/{rid}/approve",
        json={"candidate_submitted_at": d2["candidate_submitted_at"], "candidate_sha256": d2["candidate_sha256"]},
    )
    assert r.status_code == 200, r.text
    row = _run(store.get_team_skill("team_a", rid))
    assert "v2" in (Path(row.source_path) / "SKILL.md").read_text(encoding="utf-8")
    assert (Path(row.source_path) / "references" / "notes.md").is_file()
    assert row.candidate_path is None
    # Admin's own L2 promote = direct approval (submission replaces live immediately).
    _personal_skill(tmp_path, app, uid="u_admin", name="admin-recipe", marker="a1")
    da = _promote(app, ADMIN, "admin-recipe").json()
    assert da["approval"] == "approved" and da["approved_by"] == "u_admin"
    _personal_skill(tmp_path, app, uid="u_admin", name="admin-recipe", marker="a2")
    da2 = _promote(app, ADMIN, "admin-recipe").json()
    assert da2["approval"] == "approved" and da2["candidate_state"] is None
    row = _run(store.get_team_skill("team_a", da["id"]))
    assert "a2" in (Path(row.source_path) / "SKILL.md").read_text(encoding="utf-8")


def test_promote_l2_never_touches_live_description(tmp_path, monkeypatch):
    """CTO review P1 回归：L2 首候选（rec.candidate_path 尚为 None）不得把未审批的个人
    描述覆写进 live 行——Lead 路由面 bait-and-switch，且 reject 不回滚。"""
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    _personal_skill(tmp_path, app)
    d = _promote(app, AUTHOR).json()
    rid = d["id"]
    _client(app, ADMIN).post(f"{_BASE}/{rid}/approve", json={"source_sha256": d["source_sha256"]})
    live_desc = _run(store.get_team_skill("team_a", rid)).description
    # Non-admin first L2 candidate submission: live description unchanged.
    _personal_skill(tmp_path, app, marker="v2")  # Personal description becomes v2.
    d2 = _promote(app, AUTHOR).json()
    assert d2["candidate_state"] == "pending"
    assert _run(store.get_team_skill("team_a", rid)).description == live_desc
    # Still unchanged after reject.
    _client(app, ADMIN).post(
        f"{_BASE}/{rid}/reject",
        json={"candidate_submitted_at": d2["candidate_submitted_at"], "candidate_sha256": d2["candidate_sha256"], "reason": "no"},
    )
    assert _run(store.get_team_skill("team_a", rid)).description == live_desc
    # After resubmission+approval: description follows approved candidate frontmatter (approve 4b).
    _personal_skill(tmp_path, app, marker="v3")
    d3 = _promote(app, AUTHOR).json()
    r = _client(app, ADMIN).post(
        f"{_BASE}/{rid}/approve",
        json={"candidate_submitted_at": d3["candidate_submitted_at"], "candidate_sha256": d3["candidate_sha256"]},
    )
    assert r.status_code == 200, r.text
    assert _run(store.get_team_skill("team_a", rid)).description == "渠道毛利核对 触发：渠道毛利/口径"  # Candidate frontmatter.


def test_copy_validated_bundle_rejects_symlinks(tmp_path):
    """安全 review P2 回归：转移函数自身拒符号链（不依赖 multiruntime flag）——
    copytree 解引用会把链指向的任意盘上文件拷成普通文件（读旁路原语）。"""
    from bowhead_core.skill_upload import SkillUploadError as _E
    from bowhead_core.skill_upload import copy_validated_bundle as _copy

    src = tmp_path / "bundle"
    src.mkdir()
    (src / "SKILL.md").write_text(_skill_md(name="linked-recipe"), encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret", encoding="utf-8")
    (src / "references").mkdir()
    (src / "references" / "leak.md").symlink_to(secret)
    with pytest.raises(_E) as exc_info:
        _copy(src, tmp_path / "out" / "linked-recipe", expected_name="linked-recipe")
    assert exc_info.value.code == "E_SKILL_SYMLINK_NOT_ALLOWED"
    assert not (tmp_path / "out" / "linked-recipe").exists()


def test_copy_to_personal_snapshots_live_version(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    rid = _approved_skill(app, store)["id"]
    _submit_candidate(store, rid, marker="draft")  # Pending candidate exists → still copy live version.
    r = _client(app, MEMBER).post(f"{_BASE}/{rid}/copy-to-personal", json={})
    assert r.status_code == 200, r.text
    assert r.json()["source_type"] == "user_upload"
    p = tmp_path / ".data" / "user_skills" / "u_member" / "channel-gm-recipe"
    assert (p / "SKILL.md").is_file() and "draft" not in (p / "SKILL.md").read_text(encoding="utf-8")
    sis = app.state.skill_instance_store
    inst = _run(sis.get(user_id="u_member", name="channel-gm-recipe"))
    assert inst is not None and inst.source_type == "user_upload"
    # Copy same name again → 422 E_SKILL_NAME_EXISTS; with overwrite → 200.
    r = _client(app, MEMBER).post(f"{_BASE}/{rid}/copy-to-personal", json={})
    assert r.status_code == 422 and r.json()["detail"]["error"]["code"] == "E_SKILL_NAME_EXISTS"
    assert _client(app, MEMBER).post(f"{_BASE}/{rid}/copy-to-personal", json={"overwrite": True}).status_code == 200


def test_copy_to_personal_gate_and_author_exception(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    rid = _create_pending(app)["id"]
    # Member cannot see → 404; admin sees unapproved → 403 (approve before copying).
    assert _client(app, MEMBER).post(f"{_BASE}/{rid}/copy-to-personal", json={}).status_code == 404
    assert _client(app, ADMIN).post(f"{_BASE}/{rid}/copy-to-personal", json={}).status_code == 403
    # Gate exception (round 2 P2-8): author's own pending allowed; direct team solidification rejection leaves no personal copy, so copy starts editing.
    r = _client(app, AUTHOR).post(f"{_BASE}/{rid}/copy-to-personal", json={})
    assert r.status_code == 200, r.text
    assert (tmp_path / ".data" / "user_skills" / "u_author" / "channel-gm-recipe" / "SKILL.md").is_file()


# ── P3 enabler: per-user team skill enable/disable writes + sync fingerprint ──


def test_team_skill_per_user_toggle_and_sync_fingerprint(tmp_path, monkeypatch):
    """PRD 验收 8：settings 团队组 per-user 开关只影响自己——写 team_shared 行，
    effective/invoke 面按 enabled 过滤（批A #17 对称过滤的写路径补全）。"""
    store = _store(tmp_path)
    app = _promote_app(tmp_path, monkeypatch, store)
    _personal_skill(tmp_path, app)
    d = _promote(app, AUTHOR).json()
    rid = d["id"]
    _client(app, ADMIN).post(f"{_BASE}/{rid}/approve", json={"source_sha256": d["source_sha256"]})
    # Member disables per-user → team_shared row (not personal group, does not occupy the name).
    r = _client(app, MEMBER).patch("/api/v1/skills/update", json={"name": "channel-gm-recipe", "enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["source_type"] == "team_shared" and r.json()["enabled"] is False
    from bowhead_skill.effective import list_effective_skills

    catalog = SimpleNamespace(list_names=list, list_specs=list, get=lambda name: None)
    ctx = SimpleNamespace(
        user_id="u_member",
        team_id="team_a",
        skill_instance_store=app.state.skill_instance_store,
        team_skill_store=TeamSkillStoreAdapter(store),
        granted_team_skill_ids=None,
    )
    assert _run(list_effective_skills(ctx, catalog, include_catalog=False)) == []  # No advertisement when disabled.
    _client(app, MEMBER).patch("/api/v1/skills/update", json={"name": "channel-gm-recipe", "enabled": True})
    assert [e.name for e in _run(list_effective_skills(ctx, catalog, include_catalog=False))] == ["channel-gm-recipe"]
    # description through personal route → 422 (team metadata governed by /team-skills approval gate).
    assert _client(app, MEMBER).patch("/api/v1/skills/update", json={"name": "channel-gm-recipe", "description": "x 触发：y"}).status_code == 422
    # Author's personal detail content_sha256 == team's approved_sha256 (basis for drawer's '已同步' state).
    det = _client(app, AUTHOR).get("/api/v1/skills/channel-gm-recipe").json()
    row = _run(store.get_team_skill("team_a", rid))
    assert det["content_sha256"] and det["content_sha256"] == row.approved_sha256


# ── Three audit actions ────────────────────────────────────────────────────


def test_audit_three_actions(tmp_path, monkeypatch):
    store = _store(tmp_path)
    app = _app(store, monkeypatch, tmp_path)
    d = _create_pending(app)
    rid, sha = d["id"], d["source_sha256"]
    admin = _client(app, OWNER)
    admin.post(f"{_BASE}/{rid}/reject", json={"source_sha256": sha, "reason": "r1"})
    _run(store.resubmit_team_skill("team_a", rid, submitted_by="u_author"))
    admin.post(f"{_BASE}/{rid}/approve", json={"source_sha256": sha})

    async def q():
        async with aiosqlite.connect(str(tmp_path / "u.db")) as conn:
            conn.row_factory = aiosqlite.Row
            rows = await conn.execute_fetchall("SELECT action, actor_user_id FROM admin_audit_log WHERE target_id=?", (rid,))
            return [(r["action"], r["actor_user_id"]) for r in rows]

    acts = _run(q())
    assert ("team_skill_submitted", "u_author") in acts
    assert ("team_skill_rejected", "u_owner") in acts
    assert ("team_skill_approved", "u_owner") in acts


@pytest.fixture(autouse=True)
def _prepare_usage_projection_databases(tmp_path, initialize_migrated_core_database):
    for filename in ("u.db",):
        initialize_migrated_core_database(tmp_path / filename)
