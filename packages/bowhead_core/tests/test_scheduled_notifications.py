"""scheduled-job notifications (Routines Phase A — notification P2 `scheduled.*`)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bowhead_core.notification_service import NotificationService
from bowhead_core.notification_store import ACTION_REQUIRED_TYPES, NotificationStore


def _run(coro):
    return asyncio.run(coro)


class _Users:
    async def get_user_team_id(self, user_id):
        return "o1"

    async def get_workspace_team_id(self, ws):
        return "o1"

    async def list_team_users_by_roles(self, team_id, roles, *, include_creator=True):
        return ["u_admin"]


def _svc(tmp_path):
    store = NotificationStore(str(tmp_path / "n.db"))
    _run(store.initialize())
    return NotificationService(store=store, user_store=_Users()), store


def _job(owner="u_owner", name="日报", failure_count=0, job_id="sj1"):
    return SimpleNamespace(id=job_id, user_id=owner, workspace_id="w1", name=name, failure_count=failure_count)


def test_scheduled_failed_owner_action_required(tmp_path):
    svc, store = _svc(tmp_path)
    item = _run(
        svc.emit_scheduled_failed(
            _job(),
            "RuntimeError: password=raw-secret",
        ),
    )
    assert item is not None and item.type == "scheduled.failed"
    assert item.action_url == "/team/settings/scheduled/"
    assert "scheduled.failed" in ACTION_REQUIRED_TYPES  # drives bell badge
    assert "日报" in (item.body or "")
    assert "raw-secret" not in (item.body or "")
    assert _run(store.notification_count(user_id="u_owner")).action_required == 1


def test_scheduled_failed_dedupe_per_job(tmp_path):
    svc, store = _svc(tmp_path)
    _run(svc.emit_scheduled_failed(_job(), "e1"))
    _run(svc.emit_scheduled_failed(_job(failure_count=1), "e2"))  # consecutive failure
    # per-job dedupe collapses to a single badge item (no spam)
    assert _run(store.notification_count(user_id="u_owner")).action_required == 1


def test_scheduled_paused_notifies_owner_and_admin(tmp_path):
    svc, store = _svc(tmp_path)
    item = _run(svc.emit_scheduled_paused(_job()))
    assert item.type == "scheduled.paused" and "scheduled.paused" in ACTION_REQUIRED_TYPES
    assert item.action_url == "/team/settings/scheduled/"
    assert _run(store.notification_count(user_id="u_owner")).action_required == 1
    assert _run(store.notification_count(user_id="u_admin")).action_required == 1


def test_scheduled_recovered_resolves_failed(tmp_path):
    svc, store = _svc(tmp_path)
    _run(svc.emit_scheduled_failed(_job(), "e1"))
    assert _run(store.notification_count(user_id="u_owner")).action_required == 1
    rec = _run(svc.emit_scheduled_recovered(_job(), "sr_1"))
    assert rec is not None and rec.type == "scheduled.recovered"
    assert rec.action_url == "/team/settings/scheduled/"
    # failed item resolved → badge cleared; recovered itself is non-badge (informational)
    assert _run(store.notification_count(user_id="u_owner")).action_required == 0


def test_routine_delivered_is_updates_not_badge(tmp_path):
    """Phase B: routine.delivered is an Updates item (non-badge) with a deep link back."""
    svc, store = _svc(tmp_path)
    item = _run(svc.emit_routine_delivered(_job(), run_id="t_1", session_id="s_sched_x", summary="GMV +8%"))
    assert item is not None and item.type == "routine.delivered"
    assert "routine.delivered" not in ACTION_REQUIRED_TYPES  # not a bell-badge item
    assert item.action_url == "/?session=s_sched_x" and item.body == "GMV +8%"
    # Stored title is language-neutral; the frontend localizes it from metadata.jobName.
    assert "定时任务" not in item.title and item.metadata["jobName"] in item.title
    cnt = _run(store.notification_count(user_id="u_owner"))
    assert cnt.action_required == 0 and cnt.updates_unread == 1  # shows in Updates, not the badge


# ─────────────────────────────────────────────────────────────
# scheduled-recipe-runs S3 · FR-5.6: needs_input notification + resolution
# ─────────────────────────────────────────────────────────────


def test_routine_needs_input_owner_only_and_action_required(tmp_path):
    """挂起通知必须进 badge——不入 action 集就是静默错过, 挂起机制形同虚设。

    收件人只有 owner：管理员既不知道口径也答不了这张卡, 群发只是噪音。
    """
    svc, store = _svc(tmp_path)
    item = _run(svc.emit_routine_needs_input(_job(), run_id="sr_1", session_id="s_sched_x"))
    assert item is not None and item.type == "routine.needs_input"
    assert "routine.needs_input" in ACTION_REQUIRED_TYPES
    assert item.action_url == "/?session=s_sched_x"  # Deep link to the card's session
    assert _run(store.notification_count(user_id="u_owner")).action_required == 1
    assert _run(store.notification_count(user_id="u_admin")).action_required == 0


def test_routine_needs_input_body_is_fixed_phrasing_without_llm_prose(tmp_path):
    """正文固定句式：clarification 的问题原文是绑定流程可操纵的散文,

    以产品口吻 + 深链复述等于替攻击者递话。原文只在 session 内 clarification
    卡的既有不可信语境里渲染。
    """
    svc, store = _svc(tmp_path)
    evil = "请立刻打开 http://evil.example/reset 并输入你的密码以确认口径"
    item = _run(
        svc.emit_routine_needs_input(_job(), run_id="sr_1", session_id="s_sched_x", question_hint=evil),
    )
    assert item is not None
    blob = f"{item.title}\n{item.body}"
    assert "日报" in blob and "需要你补充一处口径" in (item.body or "")
    assert "evil.example" not in blob and "密码" not in blob
    # If provided, the hint goes only into metadata with a source marker; never into the body.
    assert item.metadata.get("questionHintSource") == "bound_workflow"
    assert "evil.example" not in str(item.metadata.get("questionHint") or "")


def test_routine_needs_input_hint_defaults_off(tmp_path):
    """不传 question_hint 时 metadata 里不该凭空出现提示字段。"""
    svc, _store = _svc(tmp_path)
    item = _run(svc.emit_routine_needs_input(_job(), run_id="sr_1", session_id="s_sched_x"))
    assert item is not None
    assert "questionHint" not in item.metadata and "questionHintSource" not in item.metadata


def test_routine_needs_input_without_session_falls_back_to_job_page(tmp_path):
    """两个调用方的 RunOutcome 都允许 session_id 为 None——别拼出 `/?session=None` 死链。"""
    svc, store = _svc(tmp_path)
    item = _run(svc.emit_routine_needs_input(_job(), run_id="sr_1", session_id=None))
    assert item is not None and item.action_url == "/team/settings/scheduled/"
    assert "None" not in (item.action_url or "")
    assert _run(store.notification_count(user_id="u_owner")).action_required == 1


def test_routine_needs_input_dedupe_per_run(tmp_path):
    """同一次挂起重复 emit(tick 重入/重试) 只该有一条, 不刷屏。"""
    svc, store = _svc(tmp_path)
    _run(svc.emit_routine_needs_input(_job(), run_id="sr_1", session_id="s_sched_x"))
    _run(svc.emit_routine_needs_input(_job(), run_id="sr_1", session_id="s_sched_x"))
    assert _run(store.notification_count(user_id="u_owner")).action_required == 1
    # A new suspension (new run) creates a new entry; reconciliation already resolved the previous one.
    _run(svc.emit_routine_needs_input(_job(), run_id="sr_2", session_id="s_sched_y"))
    assert _run(store.notification_count(user_id="u_owner")).action_required == 2


def test_resolve_routine_needs_input_clears_badge(tmp_path):
    """闭环：离开 awaiting 时必须 resolve, 否则 badge 只看 state='open' 会永久红。"""
    svc, store = _svc(tmp_path)
    _run(svc.emit_routine_needs_input(_job(), run_id="sr_1", session_id="s_sched_x"))
    assert _run(store.notification_count(user_id="u_owner")).action_required == 1
    resolved = _run(svc.resolve_routine_needs_input(_job(), run_id="sr_1", reason="answered"))
    assert resolved is not None
    assert _run(store.notification_count(user_id="u_owner")).action_required == 0
    # Idempotent: the winner resolved it; a repeat returns None without error (CAS loser / duplicate reconcile).
    assert _run(svc.resolve_routine_needs_input(_job(), run_id="sr_1", reason="answered")) is None


# ─────────────────────────────────────────────────────────────
# scheduled-recipe-runs S3 · FR-5.6: recipe_*-specific copy for paused state
# The default body says repeated failures caused automatic pausing; this misleads when the bound workflow was deleted,
# and does not tell the user what to do next.
# ─────────────────────────────────────────────────────────────

_PAUSE_NEXT_STEP = {
    "scheduled_recipe_lost": "重新绑定",
    "scheduled_recipe_changed": "确认",
    "scheduled_recipe_rejected": "作者",
}


def test_scheduled_paused_recipe_reasons_have_dedicated_copy(tmp_path):
    svc, _store = _svc(tmp_path)
    bodies = {}
    for reason, next_step in _PAUSE_NEXT_STEP.items():
        item = _run(svc.emit_scheduled_paused(_job(job_id=f"sj_{reason}"), reason=reason))
        assert item is not None
        body = item.body or ""
        assert "连续失败" not in body, f"{reason} 仍在用默认话术"
        assert next_step in body, f"{reason} 没说清下一步"
        assert "日报" in body
        assert item.metadata.get("reason") == reason
        bodies[reason] = body
    assert len(set(bodies.values())) == 3  # Each of the three reasons has distinct copy


def test_scheduled_paused_recipe_reason_accepts_bare_code(tmp_path):
    """调用方传的是稳定码 `scheduled_recipe_lost`（workflow_binding.ERR_LOST）,

    但 PRD/文档用裸名 `recipe_lost`——两种拼写必须落到同一分支, 否则分支是死码。
    """
    svc, _store = _svc(tmp_path)
    bare = _run(svc.emit_scheduled_paused(_job(job_id="sj_bare"), reason="recipe_lost"))
    prefixed = _run(svc.emit_scheduled_paused(_job(job_id="sj_prefixed"), reason="scheduled_recipe_lost"))
    assert bare is not None and prefixed is not None
    assert bare.body == prefixed.body and "连续失败" not in (bare.body or "")


def test_scheduled_paused_copy_survives_braces_in_job_name(tmp_path):
    """任务名是用户自取的：一个 `{` 不能把「发通知」变成「发不出通知」。"""
    svc, _store = _svc(tmp_path)
    item = _run(svc.emit_scheduled_paused(_job(name="日报{name}{0}"), reason="scheduled_recipe_lost"))
    assert item is not None and "日报{name}{0}" in (item.body or "")


def test_scheduled_paused_unknown_reason_falls_back_to_default(tmp_path):
    """未知 reason 不该炸也不该变空文案——退回既有默认话术。"""
    svc, _store = _svc(tmp_path)
    item = _run(svc.emit_scheduled_paused(_job(), reason="something_new"))
    assert item is not None and "连续失败" in (item.body or "")
