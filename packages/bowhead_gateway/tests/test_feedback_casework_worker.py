"""教练办案 worker（B 线批 3a，tech-design §2.8）契约。

钉的是**接线与规则**，教练本身打桩：证据按贡献者身份求交（越权 ref 丢弃、本人 ref 可读＝正对照）、
三种结局落库、402 前置检查只写一条事件、疲劳闸到顶停、过期回执一次且惰性翻转也补、
采纳复用缓存须重算指纹一致。全部走真迁移建库。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.coach_drafting import CoachDraft, CoachResult
from bowhead_core.feedback_candidate_store import FeedbackCandidateStore
from bowhead_core.feedback_casework_store import FeedbackCaseworkStore
from bowhead_gateway.routes import feedback_candidates as fc

from bowhead_core import migrations
from bowhead_gateway import feedback_casework as fcw
from bowhead_gateway import feedback_casework_worker as worker
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


class _Turn(SimpleNamespace):
    pass


class _SessionStore:
    def __init__(self) -> None:
        self.turns = {
            "t_mine": _Turn(session_id="s_mine", user_text="以后报告先给结论", final_answer="好的"),
            "t_other": _Turn(session_id="s_other", user_text="别人的私聊", final_answer="秘密 SECRET"),
        }
        self.sessions = {
            "s_mine": SimpleNamespace(user_id="u_prop", workspace_id="ws1"),
            "s_other": SimpleNamespace(user_id="u_other", workspace_id="ws1"),
        }

    async def get_turn(self, turn_id):  # noqa: ANN001
        return self.turns.get(turn_id)

    async def get_session(self, sid):  # noqa: ANN001
        return self.sessions.get(sid)


class _UserStore:
    def __init__(self, db_path: str, *, balance: int = 100) -> None:
        self.db_path = db_path
        self.balance = balance
        self.credit_events: list[dict] = []

    async def get_team(self, team_id):  # noqa: ANN001
        return SimpleNamespace(team_credit_balance=self.balance)

    async def record_credit_exhaustion_event(self, **kw):  # noqa: ANN003
        self.credit_events.append(kw)

    async def get_workspace(self, wid):  # noqa: ANN001
        return {"team_id": "t1"} if wid == "ws1" else None

    async def workspace_in_team(self, team_id, wid):  # noqa: ANN001
        return team_id == "t1" and wid == "ws1"

    async def get_team_subagent(self, team_id, sid, *, only_enabled=False):  # noqa: ANN001
        return SimpleNamespace(id=sid, owner_user_id="u_owner", created_by="u_owner", display_name="Sample") if sid == "ag_sample" else None

    async def get_team_skill(self, team_id, sid, *, only_enabled=False):  # noqa: ANN001
        # chart-style is an existing, approved team_wide team skill ⇒ valid reroute target; all other names are missing.
        if team_id == "t1" and sid == "chart-style":
            return SimpleNamespace(
                id="chart-style", name="chart-style", display_name="图表规范", visibility="team_wide", approval="approved", created_by="u_owner"
            )
        return None

    async def get_team_tool_source(self, team_id, ref):  # noqa: ANN001
        return None  # Built-in tool names (bi_query etc.) are absent from the team tool-source table ⇒ excluded from reroute targets.

    async def is_team_admin(self, user_id, team_id):  # noqa: ANN001
        return user_id == "u_admin"

    async def get_team_role(self, user_id, team_id):  # noqa: ANN001
        return "member" if user_id in {"u_prop", "u_owner", "u_admin", "u_other"} and team_id == "t1" else None

    async def get_users_by_ids(self, ids):  # noqa: ANN001
        return {}


class _ReadStore:
    def __init__(self, db_path: str) -> None:
        pass

    async def owned_agent_ids(self, *, team_id, actor_user_id):  # noqa: ANN001
        return {"ag_sample"} if actor_user_id == "u_owner" else set()

    async def working_copy(self, *, team_id, agent_id, actor_user_id):  # noqa: ANN001
        if actor_user_id != "u_owner":
            from bowhead_core.agent_revision_store import AgentRevisionForbiddenError

            raise AgentRevisionForbiddenError("agent_not_found")
        return {
            "working_copy": {
                "row_version": 1,
                "definition": {
                    "system_prompt": "你是 Sample 的数据分析师。",
                    "skills_json": '["chart-style", "ghost-skill"]',
                    "tool_refs_json": '["bi_query"]',
                },
            }
        }


class _Notifications:
    def __init__(self) -> None:
        self.receipts: list[dict] = []

    async def emit_feedback_candidate_receipt(self, **kw):  # noqa: ANN003
        self.receipts.append(kw)
        return SimpleNamespace(id="ntf")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = _migrated_db(tmp_path)
    monkeypatch.setattr(fs, "AgentReleaseReadStore", _ReadStore)
    monkeypatch.setattr(fc, "AgentReleaseReadStore", _ReadStore)

    routers = {"t1": SimpleNamespace()}

    async def _router(app_state, team_id, **kw):  # noqa: ANN001, ANN003
        return (routers.get(team_id), "unconfigured")

    monkeypatch.setattr(fcw, "resolve_team_router", _router)
    return SimpleNamespace(
        routers=routers,
        db=db,
        cands=FeedbackCandidateStore(db, fingerprint_key="k"),
        cw=FeedbackCaseworkStore(db),
        users=_UserStore(db),
        sessions=_SessionStore(),
        ntf=_Notifications(),
        app_state=SimpleNamespace(),
    )


def _seed(env, *, refs=("t_mine", "t_other"), created_ago_s=600, sig="报告先给结论", contributor="u_prop"):
    cand, _ = _run(
        env.cands.propose(
            team_id="t1",
            target_kind="agent_def",
            target_ref="ag_sample",
            proposed={"text": "报告先给结论"},
            rubric_text="r",
            contributor_id=contributor,
            sig_text=sig,
            provenance_refs=list(refs),
        )
    )
    return cand


def _patch_draft(monkeypatch, result: CoachResult, seen: dict | None = None):
    async def _draft(router, **kw):  # noqa: ANN001, ANN003
        if seen is not None:
            seen.update(kw)
        return result

    monkeypatch.setattr(fcw, "draft_config_edit_ex", _draft)


def _tick(env, **kw):
    return _run(
        worker.casework_tick(
            env.app_state,
            candidate_store=env.cands,
            casework_store=env.cw,
            user_store=env.users,
            session_store=env.sessions,
            notification_service=env.ntf,
            daily_cap=kw.pop("daily_cap", 5),
            stale_s=900,
            min_age_s=0,
            now=kw.pop("now", None),
            team_settings_store=kw.pop("team_settings_store", None),
        )
    )


class _TeamSettings:
    def __init__(self, **values: str) -> None:
        self.values = values

    async def get(self, team_id, key):  # noqa: ANN001
        return self.values.get(key)


def test_evidence_is_read_only_for_the_contributors_own_turns(env, monkeypatch):
    cand = _seed(env)
    seen: dict = {}
    _patch_draft(monkeypatch, CoachResult(CoachDraft("edit", "新提示词", None, "ok")), seen)
    stats = _tick(env)
    assert stats["drafted"] == 1
    excerpts = seen["evidence_excerpts"]
    assert len(excerpts) == 1 and "报告先给结论" in excerpts[0]  # Positive control: their own turn was read.
    assert not any("SECRET" in x or "私聊" in x for x in excerpts)  # No text from others' turns enters the coach.
    row = _run(env.cw.get(cand.id))
    assert row.status == "done" and row.report["evidence_used"] == 1 and row.report["evidence_dropped"] == 1
    # Reroute list = assets actually accepted by the reroute endpoint: real team skills included, built-in tool names (bi_query in definition) excluded.
    # 116 live counterexample: built-in tool entered the list → UI showed '转案到「bi_schema」' → clicking always returned 404.
    assert seen["mounted_assets"] == [{"kind": "team_skill", "ref": "chart-style"}]


def test_three_outcomes_land_in_sidecar(env, monkeypatch):
    a = _seed(env, sig="A")
    _patch_draft(monkeypatch, CoachResult(CoachDraft("reroute", None, "skill：形态", "x", "team_skill", "chart-style")))
    _tick(env)
    ra = _run(env.cw.get(a.id))
    assert ra.status == "done" and ra.report["verdict"] == "reroute" and ra.report["reroute"]["target_ref"] == "chart-style" and ra.prefill is None
    b = _seed(env, sig="B")
    _patch_draft(monkeypatch, CoachResult(None, "timeout"))
    _tick(env)
    rb = _run(env.cw.get(b.id))
    assert rb.status == "failed" and rb.reason_code == "coach_unavailable" and rb.attempts == 1
    # Next tick retries failed rows (attempts<3).
    _patch_draft(monkeypatch, CoachResult(CoachDraft("edit", "v2", None, "ok")))
    _tick(env)
    assert _run(env.cw.get(b.id)).status == "done"


def test_credit_precheck_blocks_without_calling_coach_and_records_one_episode(env, monkeypatch):
    cand = _seed(env)
    env.users.balance = 0
    called = []

    async def _draft(router, **kw):  # noqa: ANN001, ANN003
        called.append(1)
        return CoachResult(None, "timeout")

    monkeypatch.setattr(fcw, "draft_config_edit_ex", _draft)
    _tick(env)
    _tick(env)
    row = _run(env.cw.get(cand.id))
    assert row.status == "blocked" and row.reason_code == "credit_blocked" and row.attempts == 0
    assert called == [] and len(env.users.credit_events) == 1  # One event per episode.
    env.users.balance = 10  # Credits restored → automatically requeue.
    _patch_draft(monkeypatch, CoachResult(CoachDraft("edit", "v", None, "ok")))
    assert _tick(env)["drafted"] == 1


def test_daily_cap_stops_drafting(env, monkeypatch):
    for i in range(3):
        _seed(env, sig=f"sig{i}")
    _patch_draft(monkeypatch, CoachResult(CoachDraft("edit", "v", None, "ok")))
    stats = _tick(env, daily_cap=2)
    assert stats["drafted"] == 2 and stats["skipped_cap"] == 1


def test_model_unconfigured_blocks_before_claim_and_never_eats_the_cap(env, monkeypatch):
    """review P1-1：没配模型的团队 blocked 每 tick 重评，不占日 cap；配好模型当天就能起草。"""

    for i in range(3):
        _seed(env, sig=f"m{i}")
    env.routers.clear()
    called = []

    async def _draft(router, **kw):  # noqa: ANN001, ANN003
        called.append(1)
        return CoachResult(CoachDraft("edit", "v", None, "ok"))

    monkeypatch.setattr(fcw, "draft_config_edit_ex", _draft)
    for _ in range(6):
        stats = _tick(env, daily_cap=2)
        assert stats["blocked"] == 3 and stats["skipped_cap"] == 0
    assert called == [] and _run(env.cw.count_started_today("t1")) == 0
    env.routers["t1"] = SimpleNamespace()  # Model configured at 10am.
    stats = _tick(env, daily_cap=2)
    assert stats["drafted"] == 2 and stats["skipped_cap"] == 1 and len(called) == 2


def test_credit_episode_is_one_event_per_team_not_per_row(env, monkeypatch):
    for i in range(3):
        _seed(env, sig=f"c{i}")
    env.users.balance = 0
    _tick(env)
    _tick(env)
    assert len(env.users.credit_events) == 1


def _age_blocked_rows(env, team_id: str, *, days: int) -> None:
    import sqlite3

    old = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    con = sqlite3.connect(env.db)
    con.execute("UPDATE feedback_casework SET updated_at=? WHERE team_id=? AND status='blocked'", (old, team_id))
    con.commit()
    con.close()


def test_new_credit_episode_is_recorded_even_when_old_blocked_rows_linger(env, monkeypatch):
    """review 二轮 P1：episode 按新近度判。撤回离池 / 被日 cap 跳过的陈旧 blocked 行不能压住下一次 402。"""

    seeds = [_seed(env, sig=f"e{i}") for i in range(3)]
    env.users.balance = 0
    _tick(env)
    assert len(env.users.credit_events) == 1
    _run(env.cands.withdraw(seeds[0].id, by="u_prop"))  # One leaves the pool: its blocked row is never reevaluated.
    _age_blocked_rows(env, "t1", days=2)  # Time passes.
    env.users.balance = 10  # Top-up: draft the other two.
    _patch_draft(monkeypatch, CoachResult(CoachDraft("edit", "v", None, "ok")))
    assert _tick(env)["drafted"] == 2
    _seed(env, sig="e_new")
    env.users.balance = 0  # Exhausted again → new episode.
    _tick(env)
    assert len(env.users.credit_events) == 2


def test_stale_expired_candidates_are_marked_but_not_notified(env):
    kw = {"team_id": "t1", "target_kind": "semantic", "target_ref": "gmv", "proposed": {"text": "x"}, "rubric_text": "r", "contributor_id": "u_a"}
    _run(env.cands.propose(sig_text="ancient", ttl_days=-30, **kw))  # Expired 30 days ago.
    _run(env.cands.propose(sig_text="recent", ttl_days=-1, **kw))
    assert _tick(env)["expired_receipts"] == 2  # Both marked.
    assert len(env.ntf.receipts) == 1  # Only the recent one actually sent.
    assert _tick(env)["expired_receipts"] == 0


def test_queued_rows_are_picked_up(env, monkeypatch):
    cand = _seed(env)
    _run(env.cw.ensure_row(cand.id, "t1", trigger="cron"))  # queued row left by a crash between ensure_row and claim.
    _patch_draft(monkeypatch, CoachResult(CoachDraft("edit", "v", None, "ok")))
    assert _tick(env)["drafted"] == 1


def test_background_result_is_dropped_when_route_replaced_cache_mid_flight(env, monkeypatch):
    """review P1-2：教练调用期间采纳路由已 replace_cache → 后台结果作废（lost），不翻 failed、不加 attempts。"""

    cand = _seed(env)

    async def _draft(router, **kw):  # noqa: ANN001, ANN003
        await env.cw.replace_cache(
            cand.id, "t1", report={"verdict": "edit", "trigger": "explicit"}, prefill={"edited_content": "路由稿"}, base={"definition_sha256": "x"}
        )
        return CoachResult(None, "timeout")

    monkeypatch.setattr(fcw, "draft_config_edit_ex", _draft)
    stats = _tick(env)
    assert stats.get("lost") == 1 and stats["failed"] == 0
    row = _run(env.cw.get(cand.id))
    assert row.status == "done" and row.prefill == {"edited_content": "路由稿"} and row.attempts == 0


def test_expired_receipts_sent_once_including_lazy_expiry(env):
    kw = {"team_id": "t1", "target_kind": "semantic", "target_ref": "gmv", "proposed": {"text": "x"}, "rubric_text": "r", "contributor_id": "u_a"}
    lazy, _ = _run(env.cands.propose(sig_text="lazy", ttl_days=-1, **kw))
    _run(env.cands.list_open("t1"))  # Lazy state transition.
    _run(env.cands.propose(sig_text="swept", ttl_days=-1, **kw))
    assert _tick(env)["expired_receipts"] == 2
    assert {r["candidate_id"] for r in env.ntf.receipts} >= {lazy.id}
    assert all(r["final_state"] == "expired" and r["reopen_resolved"] is False for r in env.ntf.receipts)
    assert _tick(env)["expired_receipts"] == 0


def test_team_switch_off_skips_background_casework_but_not_expired_receipts(env, monkeypatch):
    """§2.8.3 ③：团队关了后台教练 → 不建行、不 block、不占 cap；过期回执照发；显式采纳不受影响（路由不看这个开关）。"""

    cand = _seed(env)
    kw = {"team_id": "t1", "target_kind": "semantic", "target_ref": "gmv", "proposed": {"text": "x"}, "rubric_text": "r", "contributor_id": "u_a"}
    _run(env.cands.propose(sig_text="old", ttl_days=-1, **kw))
    called = []

    async def _draft(router, **k):  # noqa: ANN001, ANN003
        called.append(1)
        return CoachResult(CoachDraft("edit", "v", None, "ok"))

    monkeypatch.setattr(fcw, "draft_config_edit_ex", _draft)
    stats = _tick(env, team_settings_store=_TeamSettings(feedback_casework_enabled="false"))
    assert stats["skipped_team_off"] == 1 and stats["drafted"] == 0 and stats["expired_receipts"] == 1
    assert called == [] and _run(env.cw.get(cand.id)) is None
    # Missing / unrecognized value = enabled.
    assert _tick(env, team_settings_store=_TeamSettings(feedback_casework_enabled="maybe"))["drafted"] == 1


def test_exhausted_attempts_are_retried_the_next_day(env, monkeypatch):
    """116 真机（2026-09-09）：旧的 60s 超时把候选烧到 attempts 上限，之后即使超时修好也永远不再起草。

    「暂时故障」（超时/上游 402/额度）不该被记成永久损失：到顶的行隔天再试一次。
    """

    import sqlite3

    cand = _seed(env)
    _patch_draft(monkeypatch, CoachResult(None, "timeout"))
    for _ in range(3):
        _tick(env)
    row = _run(env.cw.get(cand.id))
    assert row.status == "failed" and row.attempts == 3
    assert _tick(env)["failed"] == 0  # No more spending after hitting the same-day limit.

    con = sqlite3.connect(env.db)  # Move the last failure to 25 hours ago.
    con.execute("UPDATE feedback_casework SET updated_at=? WHERE candidate_id=?", ((datetime.now(UTC) - timedelta(hours=25)).isoformat(), cand.id))
    con.commit()
    con.close()
    _patch_draft(monkeypatch, CoachResult(CoachDraft("edit", "修好超时之后的稿", None, "ok")))
    assert _tick(env)["drafted"] == 1  # Recovers next day without manual reset.
    assert _run(env.cw.get(cand.id)).status == "done"
