"""建议收件箱审批面路由契约（批 2b-i，tech-design §2.3b/§2.4）。

authz 组合（admin/proposer/陌生人/跨团队）、flag 闸、决定与回执最小化、
采纳三分支（semantic 预填 / agent_def 教练 edit/reroute/降级）、来源对话
按读取者权限求交。角色判定与教练起草在各自单测里钉行为，这里全部走桩，
断言的是**端点行为**：状态码、状态迁移、响应里有什么/没有什么。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.coach_drafting import CoachDraft
from bowhead_core.feedback_candidate_store import FeedbackCandidateStore
from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import NotificationStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import feedback_candidates as fc
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bowhead_core import migrations
from bowhead_gateway import feedback_surfaces as fs

pytestmark = pytest.mark.integration


def _run(coro):
    return asyncio.run(coro)


def _migrated_db(tmp_path: Path) -> str:
    path = tmp_path / "core.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    return str(path)


_ADMIN = User(id="u_admin", team_id="t1", email="admin@x.com")
_PROPOSER = User(id="u_prop", team_id="t1", email="p@x.com")
_MEMBER = User(id="u_member", team_id="t1", email="m@x.com")
_OTHER_TEAM_ADMIN = User(id="u_admin2", team_id="t2", email="a2@x.com")

_ADMIN_IDS = {"u_admin", "u_admin2"}


class _StubUserStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path  # Real migrated DB: unstubbed ownership queries run real SQL (empty set).

    async def is_team_admin(self, user_id: str, team_id: str) -> bool:
        return user_id in _ADMIN_IDS

    audits: list[dict] = []

    async def write_admin_audit_log(  # Match UserStore.write_admin_audit_log; arbitrary kwargs in a stub would hide wrong caller arguments (review P1).
        self,
        *,
        actor_user_id: str,
        team_id: str,
        action: str,
        target_kind: str | None = None,
        target_id: str | None = None,
        target_team_id: str | None = None,
        scope: str,
        payload: dict | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        conn=None,  # noqa: ANN001
        audit_id: str | None = None,
    ) -> bool:
        self.audits.append({"actor_user_id": actor_user_id, "action": action, "target_id": target_id, "scope": scope, "payload": payload, "ip": ip})
        return True

    async def get_users_by_ids(self, user_ids: list[str]) -> dict[str, str]:
        # ⚠ Return only **requested** IDs: a stub ignoring inputs and returning all IDs would miss a caller's omitted ID
        # (review P2: `decided_by` resolution wiring therefore remained unpinned).
        known = {"u_prop": "王敏", "u_cosign": "李涛", "u_member": "赵成", "u_admin": "孙倩"}
        return {uid: known[uid] for uid in user_ids if uid in known}

    async def get_team_subagent(self, team_id: str, subagent_id: str, *, only_enabled: bool = False):
        if team_id == "t1" and subagent_id == "ag_sample":
            return SimpleNamespace(id="ag_sample", name="sample-analyst", display_name="Sample 数据分析师")
        return None

    async def get_team_skill(self, team_id: str, skill_id: str, *, only_enabled: bool = False):
        if team_id != "t1":
            return None
        if skill_id == "sk_chart":
            return SimpleNamespace(
                id="sk_chart",
                team_id="t1",
                name="chart-style",
                display_name="图表规范",
                description="d",
                source_path="x",
                visibility="team_wide",
                created_by="u_prop",
                updated_at="2026-09-09T00:00:00+00:00",
            )
        if skill_id == "sk_private":
            return SimpleNamespace(
                id="sk_private",
                team_id="t1",
                name="mine",
                display_name="我的",
                description="d",
                source_path="y",
                visibility="creator_only",
                created_by="u_other",
                updated_at="2026-09-09T00:00:00+00:00",
            )
        return None

    async def get_team_tool_source(self, team_id: str, source_id: str):
        if source_id != "ots_crm" or team_id != "t1":
            return None
        return SimpleNamespace(
            id="ots_crm",
            name="CRM",
            kind="openapi",
            status="enabled",
            auth_type="bearer",
            auth_ref="gAAAA-fernet-secret",
            updated_at="2026-09-09T00:00:00+00:00",
            base_url="https://user:pw@crm.example",
            spec_url="https://crm.example/spec?token=abc",
            openapi_spec_json="{...}",
            selected_op_ids=["listDeals"],
        )


class _CaptureNotifications:
    def __init__(self) -> None:
        self.receipts: list[dict] = []

    async def emit_feedback_candidate_receipt(self, **kwargs):  # noqa: ANN003
        self.receipts.append(kwargs)
        return SimpleNamespace(id="ntf_1")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = _migrated_db(tmp_path)
    store = FeedbackCandidateStore(db_path, fingerprint_key="test-secret")
    notifications = _CaptureNotifications()
    app = FastAPI()
    app.include_router(fc.router, prefix="/api/v1/feedback-candidates")
    app.state.feedback_candidate_store = store
    app.state.notification_service = notifications
    app.dependency_overrides[fc._store] = lambda: _StubUserStore(db_path)
    return SimpleNamespace(app=app, store=store, notifications=notifications)


def _client(env, user: User) -> TestClient:
    env.app.dependency_overrides[fc._current_user] = lambda: user
    return TestClient(env.app)


def _propose(env, **overrides) -> str:
    kwargs: dict = {
        "team_id": "t1",
        "target_kind": "semantic",
        "target_ref": "net_profit",
        "proposed": {"text": "净利润统一使用合并报表口径"},
        "rubric_text": "两套口径差 4.2 个百分点",
        "contributor_id": "u_prop",
        "sig_text": "净利润统一使用合并报表口径",
        "provenance_refs": ["t_aaa111"],
    }
    kwargs.update(overrides)
    cand, _ = _run(env.store.propose(**kwargs))
    return cand.id


def test_list_scopes_by_role(env):
    _propose(env)
    _propose(env, target_ref="report_shape", proposed={"text": "报告先给结论"}, sig_text="报告先给结论", contributor_id="u_other")
    admin_items = _client(env, _ADMIN).get("/api/v1/feedback-candidates").json()["items"]
    assert len(admin_items) == 2 and all("proposer_id" in it for it in admin_items)
    # 116 dogfood: nobody recognizes bare IDs; admin view includes display names (keep bare IDs when lookup fails).
    by_ref = {it["target_ref"]: it for it in admin_items}
    assert by_ref["net_profit"]["proposer_display"] == "王敏" and by_ref["net_profit"]["target_label"] is None
    prop_items = _client(env, _PROPOSER).get("/api/v1/feedback-candidates").json()["items"]
    assert len(prop_items) == 1 and prop_items[0]["mine"] is True
    assert _client(env, _MEMBER).get("/api/v1/feedback-candidates").json()["items"] == []


def test_detail_is_404_for_stranger_and_cross_team(env):
    cid = _propose(env)
    assert _client(env, _MEMBER).get(f"/api/v1/feedback-candidates/{cid}").status_code == 404
    assert _client(env, _OTHER_TEAM_ADMIN).get(f"/api/v1/feedback-candidates/{cid}").status_code == 404
    assert _client(env, _PROPOSER).get(f"/api/v1/feedback-candidates/{cid}").status_code == 200


def test_proposer_sees_others_evidence_as_count_only(env):
    cid = _propose(env)
    cand, outcome = _run(
        env.store.propose(
            team_id="t1",
            target_kind="semantic",
            target_ref="net_profit",
            proposed={"text": "净利润统一使用合并报表口径"},
            rubric_text="同求",
            contributor_id="u_cosign",
            sig_text="净利润统一使用合并报表口径",
        )
    )
    assert outcome == "merged" and cand.id == cid
    body = _client(env, _PROPOSER).get(f"/api/v1/feedback-candidates/{cid}").json()
    assert body["evidence"]["others_count"] == 1
    assert [e["contributor_id"] for e in body["evidence"]["entries"]] == ["u_prop"]
    assert body["proposer_display"] is None and body["contributor_displays"] == {}  # Proposer view does not resolve others' identities.
    admin_body = _client(env, _ADMIN).get(f"/api/v1/feedback-candidates/{cid}").json()
    assert len(admin_body["evidence"]["entries"]) == 2
    assert admin_body["contributor_displays"] == {"u_prop": "王敏", "u_cosign": "李涛"}


def test_decision_requires_admin_and_emits_minimal_receipt(env):
    cid = _propose(env)
    r = _client(env, _PROPOSER).post(f"/api/v1/feedback-candidates/{cid}/decision", json={"decision": "rejected"})
    assert r.status_code == 403
    r = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/decision", json={"decision": "rejected", "note": "口径委员会另议"})
    assert r.status_code == 200 and r.json()["state"] == "rejected"
    (receipt,) = env.notifications.receipts
    assert receipt["recipient_ids"] == ["u_prop"]
    assert receipt["final_state"] == "rejected"
    # Minimize receipt parameters: no proposed/rubric/decision-maker.
    assert "note" not in receipt and "rubric_text" not in receipt


def _seed_source_turns(env, turns: list[tuple[str, str, str, str]]) -> None:
    """(turn_id, workspace_id, team_id, status) → a Space, session and turn each."""
    with sqlite3.connect(env.store.db_path) as db:
        for turn_id, ws_id, team_id, status in turns:
            db.execute(
                "INSERT OR IGNORE INTO workspaces(id,team_id,name,status,created_at,updated_at) VALUES(?,?,?,?,'2026-10-16','2026-10-16')",
                (ws_id, team_id, f"Space {ws_id}", status),
            )
            db.execute(
                "INSERT INTO sessions(id,user_id,workspace_id,started_at,last_active_at) VALUES(?,'u_prop',?,'2026-10-16','2026-10-16')",
                (f"s_{turn_id}", ws_id),
            )
            db.execute(
                "INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES(?,?,1,'q','2026-10-16')",
                (turn_id, f"s_{turn_id}"),
            )


def test_adopt_semantic_prefills_space_instructions_for_the_source_spaces(env):
    from bowhead_core.space_instructions import SpaceInstructionsStore

    instructions = SpaceInstructionsStore(env.store.db_path)
    env.app.state.space_instructions_store = instructions
    _seed_source_turns(
        env,
        [
            ("t_aaa111", "ws_sales", "t1", "active"),
            ("t_bbb222", "ws_finance", "t1", "active"),
            ("t_bbb333", "ws_finance", "t1", "active"),
            ("t_ccc333", "ws_old", "t1", "archived"),
            ("t_ddd444", "ws_foreign", "t2", "active"),
        ],
    )
    cid = _propose(env, provenance_refs=["t_aaa111", "t_bbb222", "t_bbb333", "t_ccc333", "t_ddd444", "t_missing"])
    r = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "accepted"
    # Archived and other-Team Spaces cannot take the wording; unknown turns are skipped; most conversations first.
    assert body["prefill"] == {
        "kind": "space_instructions",
        "text": "净利润统一使用合并报表口径",
        "spaces": [
            {"workspace_id": "ws_finance", "name": "Space ws_finance", "conversation_count": 2, "instructions_version": 0, "added": None},
            {"workspace_id": "ws_sales", "name": "Space ws_sales", "conversation_count": 1, "instructions_version": 0, "added": None},
        ],
    }
    assert body["banner"]["evidence_count"] == 1
    r2 = _client(env, _MEMBER).post(f"/api/v1/feedback-candidates/{cid}/adopt")
    assert r2.status_code == 404  # Strangers cannot even see it.

    # Saving the wording records the suggestion, so a later prefill shows where it already landed.
    asyncio.run(instructions.save(workspace_id="ws_sales", content="v1", created_by="u_admin", expected_version=0))
    asyncio.run(instructions.save(workspace_id="ws_sales", content="v1\n净利润", created_by="u_admin", expected_version=1, source_candidate_id=cid))
    again = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/prefill").json()
    sales = next(space for space in again["prefill"]["spaces"] if space["workspace_id"] == "ws_sales")
    assert (sales["instructions_version"], sales["added"]["version"]) == (2, 2)


def test_decided_view_brings_back_adopted_wording_with_where_it_landed(env):
    from bowhead_core.space_instructions import SpaceInstructionsStore

    instructions = SpaceInstructionsStore(env.store.db_path)
    env.app.state.space_instructions_store = instructions
    _seed_source_turns(env, [("t_eee555", "ws_sales", "t1", "active")])
    cid = _propose(env, provenance_refs=["t_eee555"])
    open_id = _propose(env, target_ref="gross_margin", proposed={"text": "毛利率按含税口径"}, sig_text="毛利率")
    assert _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt").status_code == 200
    asyncio.run(
        instructions.save(
            workspace_id="ws_sales", content="净利润统一使用合并报表口径", created_by="u_admin", expected_version=0, source_candidate_id=cid
        )
    )

    pending = _client(env, _ADMIN).get("/api/v1/feedback-candidates").json()["items"]
    assert [row["id"] for row in pending] == [open_id]
    decided = _client(env, _ADMIN).get("/api/v1/feedback-candidates?view=decided").json()["items"]
    (row,) = decided
    assert (row["id"], row["state"]) == (cid, "accepted") and row["decided_at"]
    (space,) = row["space_targets"]["spaces"]
    assert (space["workspace_id"], space["added"]["version"]) == ("ws_sales", 1)
    # The proposer sees their own closed suggestion, but not the administrator's Space targets.
    mine = _client(env, _PROPOSER).get("/api/v1/feedback-candidates?view=decided").json()["items"]
    assert [r["id"] for r in mine] == [cid] and "space_targets" not in mine[0]
    every = _client(env, _ADMIN).get("/api/v1/feedback-candidates?view=all").json()["items"]
    assert {r["id"] for r in every} == {cid, open_id}


def _agent_candidate(env) -> str:
    return _propose(
        env,
        target_kind="agent_def",
        target_ref="ag_sample",
        proposed={"text": "报告固定结构：先结论，再九大区对比"},
        sig_text="报告固定结构",
    )


class _StubReadStore:
    OWNERS = {"u_member"}  # Act as ag_sample's resource owner; tests may temporarily add members.

    def __init__(self, db_path: str) -> None:  # Match the real class (route constructs with db_path).
        self.db_path = db_path

    async def owned_agent_ids(self, *, team_id: str, actor_user_id: str) -> set[str]:
        return {"ag_sample"} if actor_user_id in self.OWNERS else set()

    async def working_copy(self, *, team_id: str, agent_id: str, actor_user_id: str):
        if actor_user_id not in self.OWNERS:
            from bowhead_core.agent_revision_store import AgentRevisionForbiddenError

            raise AgentRevisionForbiddenError("agent_not_found")
        return {
            "working_copy": {"definition": {"system_prompt": "你是 Sample 的数据分析师。"}},
        }


def test_seam_stubs_match_real_signatures():
    """桩钉的是真接口的形状——真签名一变这里先红，而不是路由在生产里炸。"""
    import inspect

    from bowhead_core.agent_release_read_store import AgentReleaseReadStore
    from bowhead_gateway.routes import _deps, turns

    assert {"team_id", "agent_id", "actor_user_id"} <= set(inspect.signature(AgentReleaseReadStore.working_copy).parameters)
    assert {"team_id", "actor_user_id"} <= set(inspect.signature(AgentReleaseReadStore.owned_agent_ids).parameters)
    assert list(inspect.signature(turns._require_turn_access).parameters)[:3] == ["turn_id", "request", "user"]
    router_params = inspect.signature(_deps.team_router_or_409).parameters
    assert list(router_params)[:2] == ["app_state", "team_id"] and "site" in router_params


@pytest.fixture()
def agent_adopt(env, monkeypatch):
    # These cases cover the config-edit drafting path (draft_config_edit, patched by _patch_draft).
    # Since e8fc2b8f3 Owner version governance is on by default and routes agent_def adoption
    # through draft_agent_change_proposal instead; the config-edit path is the explicit opt-out.
    monkeypatch.setattr(fc, "AgentReleaseReadStore", _StubReadStore)
    monkeypatch.setattr(fs, "AgentReleaseReadStore", _StubReadStore)

    async def _router(app_state, team_id, **kwargs):  # noqa: ANN001
        return SimpleNamespace()

    monkeypatch.setattr(fc, "team_router_or_409", _router)
    return env


def _patch_draft(monkeypatch, result):
    async def _draft(router, **kwargs):  # noqa: ANN001, ANN003
        return result

    monkeypatch.setattr(fc, "draft_config_edit", _draft)


def test_adopt_agent_def_requires_resource_owner(agent_adopt, monkeypatch):
    cid = _agent_candidate(agent_adopt)
    _patch_draft(monkeypatch, CoachDraft("edit", "改后提示词", None, "ok"))
    # Admin is not the resource owner: ownership query returns 404 (prevent enumeration).
    assert _client(agent_adopt, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt").status_code == 404


def test_sources_share_one_resolver_and_recheck_on_open(env, monkeypatch):
    cid = _propose(env, provenance_refs=["t_ok", "t_denied"])

    async def _turn_access(turn_id, request, user=None, **kwargs):  # noqa: ANN001, ANN003
        if turn_id != "t_ok":
            raise HTTPException(status_code=404, detail="turn not found")
        return user

    monkeypatch.setattr(fc, "_require_turn_access", _turn_access)
    body = _client(env, _ADMIN).get(f"/api/v1/feedback-candidates/{cid}").json()
    assert body["sources"] == {"total": 2, "viewable": 1, "viewable_indexes": [0]}
    assert "t_ok" not in str(body["evidence"])  # No bare turn ID in detail.
    assert _client(env, _ADMIN).get(f"/api/v1/feedback-candidates/{cid}/sources/0").json()["turn_id"] == "t_ok"
    assert _client(env, _ADMIN).get(f"/api/v1/feedback-candidates/{cid}/sources/1").status_code == 404


def test_withdraw_only_while_sole_contributor(env):
    cid = _propose(env)
    r = _client(env, _PROPOSER).post(f"/api/v1/feedback-candidates/{cid}/withdraw")
    assert r.status_code == 200 and r.json()["state"] == "superseded"
    cid2 = _propose(env, target_ref="other", proposed={"text": "x"}, sig_text="另一条")
    _run(
        env.store.propose(
            team_id="t1",
            target_kind="semantic",
            target_ref="other",
            proposed={"text": "x"},
            rubric_text="同求",
            contributor_id="u_cosign",
            sig_text="另一条",
        )
    )
    r = _client(env, _PROPOSER).post(f"/api/v1/feedback-candidates/{cid2}/withdraw")
    assert r.status_code == 409


def test_admin_cannot_accept_agent_def_via_decision(env):
    # PRD §5: resource owners hold agent_def adoption rights and must use /adopt; direct admin accept
    # would close the case, send an '已采纳' receipt, and prevent owners reopening it. Admin still handles reject.
    cid = _agent_candidate(env)
    r = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/decision", json={"decision": "accepted"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "feedback_adopt_required"
    assert env.notifications.receipts == []
    r = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/decision", json={"decision": "rejected"})
    assert r.status_code == 200 and r.json()["state"] == "rejected"


def test_semantic_adopt_refuses_to_prefill_text_with_url(env):
    # §2.3 ③: persisted content is not presumed sanitized; external URLs in suggestions still allow decisions but forbid prefill.
    cid = _propose(
        env,
        proposed={"text": "口径以 https://evil.example/rules 为准"},
        sig_text="口径以外部为准",
    )
    r = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "accepted" and body["prefill"] is None
    assert body["degraded"] is True and body["degrade_reason"] == "url_in_proposed"


def test_resource_owner_gets_a_list_lane(agent_adopt):
    cid = _agent_candidate(agent_adopt)
    _propose(agent_adopt)  # A semantic candidate: exclude from the owner lane.
    items = _client(agent_adopt, _MEMBER).get("/api/v1/feedback-candidates").json()["items"]
    assert [it["id"] for it in items] == [cid]
    assert items[0]["for_my_agent"] is True and "proposer_id" in items[0]
    assert items[0]["target_label"] == "Sample 数据分析师"


def test_real_store_self_approval_is_403_through_routes(env):
    cid = _propose(env)
    _run(
        env.store.propose(
            team_id="t1",
            target_kind="semantic",
            target_ref="net_profit",
            proposed={"text": "净利润统一使用合并报表口径"},
            rubric_text="admin 也联署了",
            contributor_id="u_admin",
            sig_text="净利润统一使用合并报表口径",
        )
    )
    r = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/decision", json={"decision": "rejected"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "self_approval_forbidden"
    r = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "self_approval_forbidden"
    assert env.notifications.receipts == []


def test_adopt_after_decision_is_409(env):
    cid = _propose(env)
    assert _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt").status_code == 200
    r = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "feedback_candidate_not_open"


# ── 2b-ii four-way routing ──────────────────────────────────────────────────


def test_adopt_team_skill_runs_coach_over_skill_body(agent_adopt, monkeypatch):
    cid = _propose(agent_adopt, target_kind="team_skill", target_ref="sk_chart", proposed={"text": "排名图默认横向条形图"}, sig_text="排名图横向")
    monkeypatch.setattr(fs, "_read_body_md", lambda sk: "---\nname: chart-style\n---\n排名图用竖向柱状图。")
    seen: dict = {}

    async def _draft(router, **kwargs):  # noqa: ANN001, ANN003
        seen.update(kwargs)
        return CoachDraft("edit", "---\nname: chart-style\n---\n排名图用横向条形图。", None, "ok")

    monkeypatch.setattr(fc, "draft_config_edit", _draft)
    assert _client(agent_adopt, _MEMBER).post(f"/api/v1/feedback-candidates/{cid}/adopt").status_code == 404  # Unrelated member.
    r = _client(agent_adopt, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["prefill"]["kind"] == "team_skill_body" and "横向条形图" in body["prefill"]["edited_content"]
    import hashlib

    assert body["base"]["body_sha256"] == hashlib.sha256("---\nname: chart-style\n---\n排名图用竖向柱状图。".encode()).hexdigest()
    assert seen["target_kind"] == "team_skill" and "body_raw" in seen["config_surface"]


def test_admin_cannot_adopt_another_members_creator_only_skill(agent_adopt, monkeypatch):
    # Same policy as team_skills._visible_to: creator_only skills return 404 even for admin; coach must not disclose the body.
    cid = _propose(agent_adopt, target_kind="team_skill", target_ref="sk_private", proposed={"text": "x"}, sig_text="私有技能")
    monkeypatch.setattr(fs, "_read_body_md", lambda sk: "secret body")
    _patch_draft(monkeypatch, CoachDraft("edit", "leak", None, "x"))
    r = _client(agent_adopt, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt")
    assert r.status_code == 404 and "secret" not in r.text
    assert _run(agent_adopt.store.get(cid)).state == "open"


def test_adopt_tool_source_prefill_never_carries_secrets(env):
    cid = _propose(env, target_kind="tool_source", target_ref="ots_crm", proposed={"text": "把 getDeal 也开放给分析师"}, sig_text="开放 getDeal")
    r = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["prefill"]["kind"] == "tool_source_patch"
    current = body["prefill"]["current"]
    assert current == {
        "id": "ots_crm",
        "name": "CRM",
        "kind": "openapi",
        "status": "enabled",
        "auth_type": "bearer",
        "selected_op_ids": ["listDeals"],
        "updated_at": "2026-09-09T00:00:00+00:00",
    }
    assert "fernet" not in r.text and "user:pw" not in r.text and "token=abc" not in r.text


def test_adopt_new_asset_prefills_cca_intent_within_wizard_cap(env):
    long_text = "做一个专门跑九大区周报的 agent。" + "细节" * 1500
    cid = _propose(env, target_kind="new_asset", target_ref="new", proposed={"text": long_text}, sig_text="九大区周报 agent")
    r = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt")
    assert r.status_code == 200, r.text
    prefill = r.json()["prefill"]
    assert prefill["kind"] == "cca_intent" and len(prefill["purpose"]) == 2000
    cid2 = _propose(env, target_kind="new_asset", target_ref="new", proposed={"text": "照 https://evil.example/spec 建"}, sig_text="照外部建")
    r2 = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid2}/adopt")
    assert r2.json()["degraded"] is True and r2.json()["prefill"] is None


def test_retire_agent_is_owner_gated_and_visible_in_owner_lane(agent_adopt):
    cid = _propose(
        agent_adopt,
        target_kind="retire",
        target_ref="ag_sample",
        proposed={"asset_kind": "agent_def", "text": "已被新 agent 取代"},
        sig_text="退役 sample",
    )
    items = _client(agent_adopt, _MEMBER).get("/api/v1/feedback-candidates").json()["items"]
    assert [it["id"] for it in items] == [cid] and items[0]["for_my_agent"] is True
    # Admin is not the owner: can view (admin lane), but cannot retire it for the owner.
    assert _client(agent_adopt, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt").status_code == 403
    r = _client(agent_adopt, _MEMBER).post(f"/api/v1/feedback-candidates/{cid}/adopt")
    assert r.status_code == 200, r.text
    assert r.json()["prefill"] == {
        "kind": "retire",
        "asset_kind": "agent_def",
        "asset_ref": "ag_sample",
        "native_action": "agent_archive",
        "reason": "已被新 agent 取代",
    }


def test_retire_tool_source_is_admin_only_and_unknown_kind_is_409(env):
    cid = _propose(env, target_kind="retire", target_ref="ots_crm", proposed={"asset_kind": "tool_source", "text": "已下线"}, sig_text="退役 crm")
    r = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt")
    assert r.status_code == 200 and r.json()["prefill"]["native_action"] == "tool_source_disable"
    # Writes now reject unknown asset_kind (pinned by store tests); reads handle existing dirty rows with 409 and no decision.
    import json
    import sqlite3

    cid2 = _propose(env, target_kind="retire", target_ref="x", proposed={"asset_kind": "team_skill", "text": "?"}, sig_text="退役 x")
    with sqlite3.connect(env.store.db_path) as con:
        con.execute("UPDATE feedback_candidates SET proposed=? WHERE id=?", (json.dumps({"asset_kind": "capability", "text": "?"}), cid2))
    r2 = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid2}/adopt")
    assert r2.status_code == 409 and r2.json()["detail"]["code"] == "feedback_adopt_kind_unsupported"
    assert _run(env.store.get(cid2)).state == "open"


def test_admin_plain_accept_on_retire_of_agent_is_409(agent_adopt):
    cid = _propose(
        agent_adopt, target_kind="retire", target_ref="ag_sample", proposed={"asset_kind": "agent_def", "text": "退"}, sig_text="退 sample 2"
    )
    r = _client(agent_adopt, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/decision", json={"decision": "accepted"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "feedback_adopt_required"


def test_admin_who_owns_the_agent_can_adopt_its_retirement(agent_adopt, monkeypatch):
    monkeypatch.setattr(_StubReadStore, "OWNERS", {"u_member", "u_admin"})
    cid = _propose(
        agent_adopt, target_kind="retire", target_ref="ag_sample", proposed={"asset_kind": "agent_def", "text": "退"}, sig_text="退 sample 3"
    )
    r = _client(agent_adopt, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt")
    assert r.status_code == 200 and r.json()["prefill"]["native_action"] == "agent_archive"


def test_retire_reason_with_url_degrades(env):
    cid = _propose(
        env,
        target_kind="retire",
        target_ref="ots_crm",
        proposed={"asset_kind": "tool_source", "text": "见 https://evil.example/why"},
        sig_text="退 crm url",
    )
    r = _client(env, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt")
    assert r.status_code == 200 and r.json()["degraded"] is True and r.json()["degrade_reason"] == "url_in_proposed"


def test_oversized_skill_body_degrades_instead_of_calling_coach(agent_adopt, monkeypatch):
    cid = _propose(agent_adopt, target_kind="team_skill", target_ref="sk_chart", proposed={"text": "x"}, sig_text="超长技能")
    # Raw length 59.4k < limit, but JSON serialization (escaped newlines + indent) exceeds it; measure exactly as the coach does.
    monkeypatch.setattr(fs, "_read_body_md", lambda sk: ("x" * 10 + "\n") * 5_400)
    called = []

    async def _draft(router, **kwargs):  # noqa: ANN001, ANN003
        called.append(1)
        return

    monkeypatch.setattr(fc, "draft_config_edit", _draft)
    r = _client(agent_adopt, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt")
    assert r.status_code == 200 and r.json()["degrade_reason"] == "surface_too_large" and called == []


# ---- Batch 3a: wire casework sidecar into routes ----


@pytest.fixture()
def casework(agent_adopt):
    from bowhead_core.feedback_casework_store import FeedbackCaseworkStore

    agent_adopt.app.state.feedback_casework_store = FeedbackCaseworkStore(agent_adopt.store.db_path)
    agent_adopt.cw = agent_adopt.app.state.feedback_casework_store
    return agent_adopt


def test_redraft_on_non_coach_kind_does_not_burn_a_slot(casework, monkeypatch):
    cid = _propose(casework)  # semantic: no coach call.
    assert _client(casework, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/adopt").status_code == 200
    for _ in range(5):
        assert _client(casework, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/prefill").status_code == 200
    row = _run(casework.cw.get(cid))  # Row exists only for receipt time (mark_receipt); redraft quota/state remain untouched.
    assert row.receipt_at is not None and row.redraft_count == 0 and row.status == "queued" and row.reason_code is None


def test_proposer_never_sees_coach_prose_in_casework(casework, monkeypatch):
    cid = _agent_candidate(casework)
    _run(
        casework.cw.replace_cache(
            cid,
            "t1",
            report={"verdict": "reroute", "reroute": {"reason": "教练看着完整提示词写的理由", "target_kind": "team_skill", "target_ref": "sk"}},
            prefill={"edited_content": "秘密"},
            base={"definition_sha256": "x"},
        )
    )
    mine = _client(casework, User(id="u_prop", team_id="t1", email="p@x.com")).get(f"/api/v1/feedback-candidates/{cid}").json()["casework"]
    assert mine["status"] == "done" and mine["reroute_target"]["target_ref"] == "sk" and mine["reroute_target"]["reason"] is None
    assert "秘密" not in json.dumps(mine, ensure_ascii=False)
    owner = _client(casework, _MEMBER).get(f"/api/v1/feedback-candidates/{cid}").json()["casework"]
    assert owner["reroute_target"]["reason"] == "教练看着完整提示词写的理由" and "秘密" not in json.dumps(owner, ensure_ascii=False)


# ---- Batch 3b: reroute endpoint + team background-coach switch + usage ----


class _StubTeamSettings:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    async def get(self, team_id: str, key: str):
        return self.values.get((team_id, key))

    async def set(self, team_id: str, key: str, value: str, *, updated_by: str) -> None:
        self.values[(team_id, key)] = value


def test_reroute_supersedes_and_rehands_to_new_target_owner(agent_adopt):
    # Semantic candidate → admin decides Sample's prompt should change → reroute to agent_def.
    cid = _propose(agent_adopt)
    r = _client(agent_adopt, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/reroute", json={"target_kind": "agent_def", "target_ref": "ag_sample"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["superseded_id"] == cid and body["outcome"] == "created"
    new_id = body["candidate_id"]
    assert body["candidate"]["target_kind"] == "agent_def" and body["candidate"]["target_label"] == "Sample 数据分析师"
    assert body["candidate"]["for_my_agent"] is False  # Admin rerouting to another's agent gains no adoption rights.
    # Original row leaves the open list; new row enters the resource owner's lane.
    ids = {row["id"] for row in _client(agent_adopt, _ADMIN).get("/api/v1/feedback-candidates").json()["items"]}
    assert cid not in ids and new_id in ids
    mine = _client(agent_adopt, _MEMBER).get("/api/v1/feedback-candidates").json()["items"]
    assert [row["id"] for row in mine] == [new_id] and mine[0]["for_my_agent"] is True
    # Proposer view: the new row is still mine.
    mine_p = _client(agent_adopt, _PROPOSER).get("/api/v1/feedback-candidates").json()["items"]
    assert [row["id"] for row in mine_p] == [new_id] and mine_p[0]["mine"] is True


def test_reroute_requires_reviewer_role_and_excludes_involved(agent_adopt):
    cid = _propose(agent_adopt)
    # Proposer: lacks review role → 403 (already able to see their own candidate).
    assert (
        _client(agent_adopt, _PROPOSER)
        .post(f"/api/v1/feedback-candidates/{cid}/reroute", json={"target_kind": "agent_def", "target_ref": "ag_sample"})
        .status_code
        == 403
    )
    # Unrelated member: invisible → 404.
    assert (
        _client(agent_adopt, _MEMBER)
        .post(f"/api/v1/feedback-candidates/{cid}/reroute", json={"target_kind": "agent_def", "target_ref": "ag_sample"})
        .status_code
        == 404
    )
    # Missing target → 404; same target → 409.
    assert (
        _client(agent_adopt, _ADMIN)
        .post(f"/api/v1/feedback-candidates/{cid}/reroute", json={"target_kind": "agent_def", "target_ref": "ag_nope"})
        .status_code
        == 404
    )
    assert (
        _client(agent_adopt, _ADMIN)
        .post(f"/api/v1/feedback-candidates/{cid}/reroute", json={"target_kind": "semantic", "target_ref": "net_profit"})
        .status_code
        == 409
    )
    # creator_only skills do not exist for others: no enumeration oracle or candidates nobody can adopt.
    assert (
        _client(agent_adopt, _ADMIN)
        .post(f"/api/v1/feedback-candidates/{cid}/reroute", json={"target_kind": "team_skill", "target_ref": "sk_private"})
        .status_code
        == 404
    )
    # A cosigning admin cannot reroute their own case.
    _propose(agent_adopt, contributor_id="u_admin")
    r = _client(agent_adopt, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/reroute", json={"target_kind": "agent_def", "target_ref": "ag_sample"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "self_approval_forbidden"


def test_reroute_by_resource_owner_to_a_target_outside_his_lane(agent_adopt):
    cid = _agent_candidate(agent_adopt)
    r = _client(agent_adopt, _MEMBER).post(f"/api/v1/feedback-candidates/{cid}/reroute", json={"target_kind": "team_skill", "target_ref": "sk_chart"})
    assert r.status_code == 200, r.text
    assert r.json()["candidate"] is None and r.json()["candidate_id"] is None and r.json()["outcome"] is None  # No lane: hide merged target.
    assert _client(agent_adopt, _MEMBER).get("/api/v1/feedback-candidates").json()["items"] == []
    admin_items = _client(agent_adopt, _ADMIN).get("/api/v1/feedback-candidates").json()["items"]
    assert [row["target_kind"] for row in admin_items] == ["team_skill"]


def test_reroute_merged_returns_the_existing_candidate_with_its_casework(casework):
    from bowhead_core.feedback_casework_store import FeedbackCaseworkStore

    existing = _propose(casework, target_kind="agent_def", target_ref="ag_sample", contributor_id="u_cosign", sig_text="净利润统一使用合并报表口径")
    _run(
        FeedbackCaseworkStore(casework.store.db_path).replace_cache(
            existing, "t1", report={"verdict": "edit"}, prefill={"edited_content": "v"}, base={"definition_sha256": "x"}
        )
    )
    cid = _propose(casework)  # semantic, same statement.
    r = _client(casework, _ADMIN).post(f"/api/v1/feedback-candidates/{cid}/reroute", json={"target_kind": "agent_def", "target_ref": "ag_sample"})
    assert r.status_code == 200 and r.json()["outcome"] == "merged" and r.json()["candidate_id"] == existing
    row = r.json()["candidate"]
    assert row["evidence_count"] == 2 and row["casework"]["status"] == "done" and row["casework"]["verdict"] == "edit"


def test_casework_settings_toggle_is_admin_only_and_shows_in_list(agent_adopt, monkeypatch):
    tss = _StubTeamSettings()
    agent_adopt.app.state.team_settings_store = tss
    assert _client(agent_adopt, _MEMBER).get("/api/v1/feedback-candidates/settings/casework").status_code == 403
    assert _client(agent_adopt, _ADMIN).get("/api/v1/feedback-candidates/settings/casework").json() == {"enabled": True}  # Enabled by default.
    r = _client(agent_adopt, _ADMIN).put("/api/v1/feedback-candidates/settings/casework", json={"enabled": False})
    assert r.status_code == 200 and r.json() == {"enabled": False}
    assert tss.values[("t1", "feedback_casework_enabled")] == "false"
    audit = _StubUserStore.audits[-1]
    assert audit["target_id"] == "feedback_casework_enabled" and audit["payload"]["after"] == "false" and audit["payload"]["before"] is None
    assert "ip" in audit  # Accepted by the real signature.
    listing = _client(agent_adopt, _ADMIN).get("/api/v1/feedback-candidates").json()
    assert listing["coach"] == {"month_drafts": 0, "casework_enabled": False}
    assert "coach" not in _client(agent_adopt, _MEMBER).get("/api/v1/feedback-candidates").json()  # Usage only for admin.


def test_detail_and_list_agree_on_who_is_a_reviewer(casework, monkeypatch):
    """资源 owner 同时是提议人时，list 给全量而 detail 曾降级成 proposer——同一个人两个面两种答案。"""

    cid = _propose(
        casework, target_kind="agent_def", target_ref="ag_sample", proposed={"text": "x"}, sig_text="owner 自己提的", contributor_id="u_member"
    )
    listed = next(r for r in _client(casework, _MEMBER).get("/api/v1/feedback-candidates").json()["items"] if r["id"] == cid)
    detail = _client(casework, _MEMBER).get(f"/api/v1/feedback-candidates/{cid}").json()
    assert listed["for_my_agent"] is True
    assert detail["viewer_role"] in ("proposer", "resource_owner")
    assert detail["proposer_display"] is not None  # Still expose reviewer-view fields; also being the proposer does not degrade them.


# ── Batch 5: terminal receipt delivery (GET /receipts) ───────────────────────
# Deliver via chat inline cards rather than the notification center: 116 observed 139 notifications with 0 clicks (55 action
# notifications also had 0), whereas all 8 manual decisions in that period occurred on chat inline cards.


def _with_notifications(env, tmp_path: Path) -> NotificationStore:
    """给 env 接一个**真** NotificationStore + 真 service：回执的写与读必须同源。"""

    nstore = NotificationStore(db_path=str(tmp_path / "n.db"))
    _run(nstore.initialize())
    env.app.state.notification_store = nstore
    env.app.state.notification_service = NotificationService(store=nstore, user_store=SimpleNamespace())
    return nstore


def test_receipts_reach_the_proposer_with_a_resolvable_target_name(env, tmp_path):
    nstore = _with_notifications(env, tmp_path)
    cid = _propose(env, target_kind="agent_def", target_ref="ag_sample", proposed={"text": "先给结论"}, sig_text="先给结论")
    _run(env.store.decide(cid, decision="accepted", decided_by="u_admin"))
    _run(_emit_receipt(env, nstore, cid))

    items = _client(env, _PROPOSER).get("/api/v1/feedback-candidates/receipts").json()["items"]
    assert len(items) == 1
    got = items[0]
    assert got["candidate_id"] == cid and got["final_state"] == "accepted" and got["mine"] is True
    # Resolve display names server-side: nobody recognizes bare ag_sample (notification body minimized under §2.4 cannot provide this).
    assert got["target_label"] == "Sample 数据分析师"


def test_receipts_are_per_recipient_and_clear_on_read(env, tmp_path):
    nstore = _with_notifications(env, tmp_path)
    cid = _propose(env)
    _run(env.store.decide(cid, decision="rejected", decided_by="u_admin"))
    _run(_emit_receipt(env, nstore, cid))

    assert _client(env, _MEMBER).get("/api/v1/feedback-candidates/receipts").json()["items"] == []  # Not a recipient.
    rows = _client(env, _PROPOSER).get("/api/v1/feedback-candidates/receipts").json()["items"]
    assert len(rows) == 1
    _run(nstore.mark_read(notification_id=rows[0]["notification_id"], user_id="u_prop"))
    # Acknowledge=mark read ⇒ chat and notification center share state, preventing one alert on each surface.
    assert _client(env, _PROPOSER).get("/api/v1/feedback-candidates/receipts").json()["items"] == []


def test_receipts_path_is_not_swallowed_by_the_candidate_id_route(env, tmp_path):
    """★ `/receipts` 声明在 `/{candidate_id}` 之后就会被当成一个候选 id——404 且看不出原因。"""

    _with_notifications(env, tmp_path)
    resp = _client(env, _PROPOSER).get("/api/v1/feedback-candidates/receipts")
    assert resp.status_code == 200 and "items" in resp.json()


@pytest.mark.parametrize(("kind", "ref"), [("semantic", "net_profit"), ("new_asset", "季度复盘助手")])
def test_target_label_falls_back_to_ref_for_the_kinds_nobody_resolves(env, tmp_path, kind, ref):
    """★ `_label_for` 只认 agent_def/team_skill/tool_source，另外三种一律返 None。

    所以 `or cand.target_ref` **不是边角兜底，而是一半 kind 的正路**——而 semantic
    （业务口径）很可能是最常见的那种。少了它，用户看到的是「关于「」的这条建议」，
    空引号里什么都没有。原先只有 agent_def 一条用例，去掉兜底 47 条全绿（review 实测）。
    """

    nstore = _with_notifications(env, tmp_path)
    cid = _propose(env, target_kind=kind, target_ref=ref, proposed={"text": "x"}, sig_text=f"sig-{kind}")
    _run(env.store.decide(cid, decision="accepted", decided_by="u_admin"))
    _run(_emit_receipt(env, nstore, cid))

    items = _client(env, _PROPOSER).get("/api/v1/feedback-candidates/receipts").json()["items"]
    assert [it["target_label"] for it in items] == [ref]  # Nonempty and exactly that readable ref.


def test_retire_receipt_resolves_the_retired_assets_name(env, tmp_path):
    """Resolve retirement labels from proposed.asset_kind rather than target_kind."""

    nstore = _with_notifications(env, tmp_path)
    cid = _propose(
        env,
        target_kind="retire",
        target_ref="ag_sample",
        proposed={"text": "这个 agent 没人用了", "asset_kind": "agent_def"},
        sig_text="退役 sample",
    )
    _run(env.store.decide(cid, decision="accepted", decided_by="u_admin"))
    _run(_emit_receipt(env, nstore, cid))

    items = _client(env, _PROPOSER).get("/api/v1/feedback-candidates/receipts").json()["items"]
    assert [it["target_label"] for it in items] == ["Sample 数据分析师"]


def test_receipts_go_dark_when_the_notification_center_is_off(env, tmp_path):
    """★ 送达面必须与**签收路径**共存亡。

    `POST /notifications/{id}/read` 在 `BOWHEAD_NOTIFICATION_CENTER=0` 时直接 404
    （`routes/notifications.py::_store`）。这里若照常下发，用户拿到的是一张点「知道了」
    永远 404 的卡；前端把它当可重试失败保留 ⇒ **每轮对话都钉在那儿撕不掉**。
    """

    nstore = _with_notifications(env, tmp_path)
    cid = _propose(env)
    _run(env.store.decide(cid, decision="accepted", decided_by="u_admin"))
    _run(_emit_receipt(env, nstore, cid))
    assert len(_client(env, _PROPOSER).get("/api/v1/feedback-candidates/receipts").json()["items"]) == 1

    env.app.state.notification_center_enabled = False
    assert _client(env, _PROPOSER).get("/api/v1/feedback-candidates/receipts").json()["items"] == []


def test_receipts_carry_only_what_the_card_renders(env, tmp_path):
    """字段最小化：端点的卖点就是「通知最小化、这里才给展示名」，别顺手多发裸 ref。"""

    nstore = _with_notifications(env, tmp_path)
    cid = _propose(env, target_kind="agent_def", target_ref="ag_sample", proposed={"text": "x"}, sig_text="字段面")
    _run(env.store.decide(cid, decision="accepted", decided_by="u_admin"))
    _run(_emit_receipt(env, nstore, cid))

    item = _client(env, _PROPOSER).get("/api/v1/feedback-candidates/receipts").json()["items"][0]
    assert set(item) == {"notification_id", "candidate_id", "target_label", "final_state", "decided_at", "mine"}
    assert "decided_by" not in item  # Do not expose decision-maker identity (same policy as `_recurrence_view` for non-reviewers).


def test_receipt_addressed_to_a_non_participant_is_dropped(env, tmp_path):
    """收件人集合会漂移（转案/合并后联署人变了），通知行却留着。

    ★ 这条独立存在是因为「拿别人的回执」在上面那条测试里**测不出来**：
    `list_unread_by_type` 本来就按收件人过滤，陌生人根本没有通知行 ——
    删掉路由里的 `_involved` 判据，那条测试照样全绿（突变实测）。
    这里直接把一条回执投给一个不在 `_involved` 里的人，才碰得到那道闸。
    """

    nstore = _with_notifications(env, tmp_path)
    cid = _propose(env)
    _run(env.store.decide(cid, decision="accepted", decided_by="u_admin"))
    _run(
        nstore.upsert_item_with_recipients(
            team_id="t1",
            category="feedback",
            type="feedback_candidate_receipt",
            priority="p2",
            title="t",
            body=None,
            subject_type="feedback_candidate",
            subject_id=cid,
            actor_user_id=None,
            action_url=None,
            actions=[],
            metadata={"candidateId": cid, "finalState": "accepted"},
            dedupe_key=f"feedback.candidate.receipt:{cid}",
            recipients=[("u_member", "feedback_contributor")],  # Neither proposer nor cosigner.
        )
    )
    assert _client(env, _MEMBER).get("/api/v1/feedback-candidates/receipts").json()["items"] == []


def test_receipt_for_another_teams_candidate_is_dropped(env, tmp_path):
    """跨团队：同一个 user id 在另一个团队的候选上收到过回执，也不得读到内容。"""

    nstore = _with_notifications(env, tmp_path)
    cid = _propose(env, team_id="t2", contributor_id="u_prop")
    _run(env.store.decide(cid, decision="accepted", decided_by="u_admin2"))
    _run(_emit_receipt(env, nstore, cid))
    # u_prop is in _involved and really received the notification; only the team_id criterion blocks it.
    assert _client(env, _PROPOSER).get("/api/v1/feedback-candidates/receipts").json()["items"] == []


def test_receipt_without_a_readable_candidate_is_dropped_not_rendered_blank(env, tmp_path):
    """候选没了/跨团队：不下发半张卡。"""

    nstore = _with_notifications(env, tmp_path)
    _run(
        nstore.upsert_item_with_recipients(
            team_id="t1",
            category="feedback",
            type="feedback_candidate_receipt",
            priority="p2",
            title="t",
            body=None,
            subject_type="feedback_candidate",
            subject_id="fbc_ghost",
            actor_user_id=None,
            action_url=None,
            actions=[],
            metadata={"candidateId": "fbc_ghost", "finalState": "accepted"},
            dedupe_key="feedback.candidate.receipt:fbc_ghost",
            recipients=[("u_prop", "feedback_contributor")],
        )
    )
    assert _client(env, _PROPOSER).get("/api/v1/feedback-candidates/receipts").json()["items"] == []


async def _emit_receipt(env, nstore: NotificationStore, cid: str) -> None:
    cand = await env.store.get(cid)
    service = NotificationService(store=nstore, user_store=SimpleNamespace())
    await service.emit_feedback_candidate_receipt(
        team_id=cand.team_id,
        candidate_id=cand.id,
        target_kind=cand.target_kind,
        final_state=cand.state,
        decided_at=cand.decided_at or cand.updated_at,
        recipient_ids=sorted(fc._involved(cand)),
    )


def test_pending_count_excludes_own_proposals_and_cosignatures(env):
    _propose(env)  # Proposed by u_prop: the admin can decide it.
    _propose(env, target_ref="report_shape", proposed={"text": "报告先给结论"}, sig_text="报告先给结论", contributor_id="u_admin")
    _propose(env, target_ref="gm", proposed={"text": "毛利率口径"}, sig_text="毛利率口径", contributor_id="u_other")
    _run(
        env.store.propose(
            team_id="t1",
            target_kind="semantic",
            target_ref="gm",
            proposed={"text": "毛利率口径"},
            rubric_text="admin co-signed too",
            contributor_id="u_admin",
            sig_text="毛利率口径",
        )
    )
    # The admin's own proposal and the co-signed candidate are excluded: one candidate waits for this admin.
    assert _client(env, _ADMIN).get("/api/v1/feedback-candidates/pending-count").json() == {"count": 1}
    # The proposer cannot decide, members have nothing to decide, other teams see nothing.
    assert _client(env, _PROPOSER).get("/api/v1/feedback-candidates/pending-count").json() == {"count": 0}
    assert _client(env, _MEMBER).get("/api/v1/feedback-candidates/pending-count").json() == {"count": 0}
    assert _client(env, _OTHER_TEAM_ADMIN).get("/api/v1/feedback-candidates/pending-count").json() == {"count": 0}


def test_pending_count_gives_resource_owners_their_agent_lane(agent_adopt):
    _agent_candidate(agent_adopt)
    _propose(agent_adopt)  # Semantic: not the resource owner's decision.
    assert _client(agent_adopt, _MEMBER).get("/api/v1/feedback-candidates/pending-count").json() == {"count": 1}
    assert _client(agent_adopt, _ADMIN).get("/api/v1/feedback-candidates/pending-count").json() == {"count": 2}


def test_pending_count_route_is_not_read_as_a_candidate_id(env):
    # Declared before /{candidate_id}; a stranger gets a count, not the anti-enumeration 404.
    assert _client(env, _MEMBER).get("/api/v1/feedback-candidates/pending-count").status_code == 200
