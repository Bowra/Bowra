"""scheduled_job_create tool (routines ⑥ — flag-gated conversational create)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bowhead_core.scheduled_job_tool import (
    _handler,
    _human_schedule,
    register_scheduled_job_tool,
    scheduled_job_tool_enabled,
)
from bowhead_core.scheduled_store import ScheduledJobStore


def _run(coro):
    return asyncio.run(coro)


def _store(tmp_path):
    s = ScheduledJobStore(str(tmp_path / "s.db"))
    _run(s.initialize())
    return s


def _migrated_store(tmp_path):
    """建一个**迁移后形态**的库。

    `recipe_ref` 由 alembic 0011 追加（`ScheduledJobStore.SCHEMA` 是 1.0.0 过渡 DDL，按
    docs/design/数据库迁移.md 不得再扩），所以只 `initialize()` 的库没有这列。迁移本身由
    gateway 侧 test_alembic_scheduled_recipe_runs.py 验；这里只需要迁移后的形状。
    """
    import sqlite3

    s = _store(tmp_path)
    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        conn.execute("ALTER TABLE scheduled_jobs ADD COLUMN recipe_ref TEXT")
        conn.commit()
    return s


def _ctx(store, uid="u1", ws="w1"):
    return SimpleNamespace(scheduled_store=store, user_id=uid, workspace_id=ws)


def test_human_schedule():
    assert _human_schedule("0 9 * * *", "Asia/Shanghai") == "每天 09:00"
    assert _human_schedule("30 8 * * 1-5", "Asia/Shanghai") == "工作日 08:30"
    assert _human_schedule("0 9 * * 1", "Asia/Shanghai") == "每周一 09:00"
    assert _human_schedule("0 * * * *", "Asia/Shanghai") == "每小时"
    assert _human_schedule("@manual", "Asia/Shanghai").startswith("手动")


def test_create_ok(tmp_path):
    store = _store(tmp_path)
    r = _run(_handler({"name": "每日GMV", "query": "昨日GMV", "cron": "0 9 * * *", "notify_emails": ["a@x.com"]}, _ctx(store)))
    assert r["ok"] is True and r["job_id"].startswith("sj_")
    assert r["human_schedule"] == "每天 09:00" and r["next_run_at"] is not None
    jobs = _run(store.list_for_user(user_id="u1"))
    assert len(jobs) == 1 and jobs[0].name == "每日GMV"
    assert jobs[0].notify_channels == {"email": ["a@x.com"], "deliver_on_success": True}  # dict, not array


def test_subhourly_rejected(tmp_path):
    store = _store(tmp_path)
    r = _run(_handler({"name": "x", "query": "q", "cron": "* * * * *"}, _ctx(store)))
    assert r["ok"] is False and "sub-hourly" in r["error"]
    assert len(_run(store.list_for_user(user_id="u1"))) == 0  # not created


def test_manual_no_next_run(tmp_path):
    store = _store(tmp_path)
    r = _run(_handler({"name": "x", "query": "q", "cron": "@manual"}, _ctx(store)))
    assert r["ok"] is True and r["next_run_at"] is None and "手动" in r["human_schedule"]


def test_missing_store_and_fields(tmp_path):
    bad = _run(_handler({"name": "x", "query": "q", "cron": "0 9 * * *"}, SimpleNamespace(scheduled_store=None, user_id="u1", workspace_id="w1")))
    assert bad["ok"] is False and "scheduled_store" in bad["error"]
    store = _store(tmp_path)
    no_name = _run(_handler({"name": "", "query": "q", "cron": "0 9 * * *"}, _ctx(store)))
    assert no_name["ok"] is False


def test_flag_gate(monkeypatch):
    monkeypatch.delenv("BOWHEAD_SCHEDULED_JOB_TOOL", raising=False)
    assert scheduled_job_tool_enabled() is False

    class _Reg:
        def __init__(self) -> None:
            self.n = 0

        def register(self, _spec) -> None:
            self.n += 1

    reg = _Reg()
    assert register_scheduled_job_tool(reg) is False and reg.n == 0  # OFF → not registered
    monkeypatch.setenv("BOWHEAD_SCHEDULED_JOB_TOOL", "1")
    assert scheduled_job_tool_enabled() is True
    assert register_scheduled_job_tool(reg) is True and reg.n == 1  # ON → registered


# ─── scheduled-recipe-runs S1: bind solidified workflows ──────────────────


def _skill_dir(tmp_path, name: str):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 周报流程\nversion: 1.0.0\n---\n\n1. 取数\n2. 出报告\n",
        encoding="utf-8",
    )
    return d


class _PersonalSkills:
    def __init__(self, rows):
        self._rows = rows

    async def get(self, *, user_id: str, name: str):
        return self._rows.get((user_id, name))


def _ctx_with_skills(store, skills, uid="u1", ws="w1"):
    ctx = _ctx(store, uid=uid, ws=ws)
    ctx.skill_instance_store = skills
    ctx.team_id = "t1"
    ctx.team_skill_store = None
    ctx.team_subagent_store = None
    return ctx


def test_create_with_binding_persists_ref(tmp_path):
    store = _migrated_store(tmp_path)
    d = _skill_dir(tmp_path, "weekly-report")
    skills = _PersonalSkills({("u1", "weekly-report"): SimpleNamespace(source_path=str(d), description="周报", enabled=True)})

    r = _run(
        _handler(
            {
                "name": "周报",
                "query": "出周报",
                "cron": "0 20 * * 5",
                "recipe_ref": {"form": "personal_skill", "id_or_name": "weekly-report"},
            },
            _ctx_with_skills(store, skills),
        )
    )

    assert r["ok"] is True
    assert r["bound_workflow"] is True
    assert "固化流程" in r["message"], "绑了流程必须回显给用户确认，不能默默绑上"
    job = _run(store.get(r["job_id"]))
    assert job.recipe_ref and "weekly-report" in job.recipe_ref


def test_made_up_workflow_name_refuses_to_create(tmp_path):
    """★模型编了个不存在的流程名 → 整个建任务失败，而不是建成一个「绑了空气」的任务。

    绑错流程 = 无人值守时按别人的纪律跑在 owner 身份上，宁可不建。
    """
    store = _store(tmp_path)

    r = _run(
        _handler(
            {
                "name": "周报",
                "query": "出周报",
                "cron": "0 20 * * 5",
                "recipe_ref": {"form": "personal_skill", "id_or_name": "我猜有这么个流程"},
            },
            _ctx_with_skills(store, _PersonalSkills({})),
        )
    )

    assert r["ok"] is False
    assert "固化流程" in r["error"]
    assert _run(store.list_for_user(user_id="u1")) == [], "拒绝时不得留下半成品任务"


def test_unbound_create_unchanged(tmp_path):
    store = _store(tmp_path)
    r = _run(_handler({"name": "普通", "query": "q", "cron": "0 9 * * *"}, _ctx(store)))
    assert r["ok"] is True and r.get("bound_workflow") is False
    assert _run(store.get(r["job_id"])).recipe_ref is None


# ── default zone: the asking viewer's zone when the user names none ────────────


def _created_tz(store, args, ctx):
    r = _run(_handler({"name": "每日GMV", "query": "昨日GMV", "cron": "0 9 * * *", **args}, ctx))
    assert r["ok"] is True
    return _run(store.get(r["job_id"])).tz, r["message"]


def test_unspecified_zone_defaults_to_the_turn_viewer_zone(tmp_path):
    from bowhead_core.language_context import LanguageContext

    store = _store(tmp_path)
    ctx = _ctx(store)
    ctx.language_context = LanguageContext().with_time_zone("Europe/Berlin")
    tz, message = _created_tz(store, {}, ctx)
    assert tz == "Europe/Berlin"
    assert "（Europe/Berlin）" in message  # the confirmation read back to the user names it


def test_unspecified_zone_reads_the_bound_turn_clock(tmp_path):
    from bowhead_core.model_clock import viewer_clock_scope

    store = _store(tmp_path)
    with viewer_clock_scope("America/Los_Angeles"):
        tz, _ = _created_tz(store, {}, _ctx(store))
    assert tz == "America/Los_Angeles"


def test_explicit_zone_wins_over_the_viewer_zone(tmp_path):
    from bowhead_core.language_context import LanguageContext

    store = _store(tmp_path)
    ctx = _ctx(store)
    ctx.language_context = LanguageContext().with_time_zone("Europe/Berlin")
    assert _created_tz(store, {"tz": "Asia/Tokyo"}, ctx)[0] == "Asia/Tokyo"


def test_without_a_viewer_zone_the_server_default_stays(tmp_path):
    """IM, scheduled and older-client turns admit no zone."""
    store = _store(tmp_path)
    assert _created_tz(store, {}, _ctx(store))[0] == "Asia/Shanghai"
