"""GET /scheduled/bindable-workflows —— 可绑定固化流程的服务端聚合（S1 / tech-design §1.6）.

为什么是服务端聚合而不是前端串三个端点：可绑性是**授权语义**（enabled / visibility /
是不是固化产物），前端抄一份迟早跟 fire 时的解析漂移；个人 skill 那侧还没有「这是流程」
的判别字段，得读 source_type。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app(monkeypatch: pytest.MonkeyPatch, **state: object) -> TestClient:
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")

    from bowhead_gateway.routes import scheduled

    app = FastAPI()
    app.state.settings = SimpleNamespace(auth_mode="dev", dev_default_user="user_1")
    for key, value in state.items():
        setattr(app.state, key, value)
    app.include_router(scheduled.router, prefix="/api/v1/scheduled")
    return TestClient(app)


def _skill(name: str, *, source_type: str = "user_generated", enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(name=name, source_type=source_type, enabled=enabled, description=f"{name} 说明")


class _SkillStore:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    async def list_for_user(self, *, user_id: str) -> list[SimpleNamespace]:
        return self._rows


class _UserStore:
    def __init__(self, skills: list[SimpleNamespace] | None = None, subagents: list[SimpleNamespace] | None = None):
        self._skills = skills or []
        self._subagents = subagents or []

    async def get_user_team_id(self, _user_id: str) -> str:
        return "team_1"

    async def get_user(self, user_id: str):
        return SimpleNamespace(display_name="张三", email=f"{user_id}@x.com") if user_id == "user_2" else None

    async def list_team_skills(self, team_id: str, *, only_enabled: bool = False, only_approved: bool = True):
        return self._skills

    async def list_team_subagents(self, team_id: str, *, only_enabled: bool = False):
        return self._subagents

    # ── The following are used only by the list route (_require_workspace_access) ──
    async def get_workspace_team_id(self, _workspace_id: str) -> str:
        return "team_1"

    async def get_member_role(self, _workspace_id: str, _user_id: str) -> str:
        return "member"

    async def get_team_role(self, _user_id: str, _team_id: str) -> str:
        return "member"

    async def get_workspace(self, workspace_id: str) -> dict[str, str]:
        return {"id": workspace_id, "status": "active"}


def _team_skill(sid: str, *, visibility: str = "team_wide", created_by: str = "user_2") -> SimpleNamespace:
    return SimpleNamespace(
        id=sid,
        name=sid,
        display_name=f"{sid} 显示名",
        description="团队流程",
        visibility=visibility,
        created_by=created_by,
    )


def test_personal_lists_only_conversationally_solidified_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """上传的普通 skill 不是「流程」——只有对话固化产物（user_generated）能绑。"""
    client = _app(
        monkeypatch,
        skill_instance_store=_SkillStore(
            [
                _skill("weekly-report"),
                _skill("bowhead-slides", source_type="user_upload"),
                _skill("disabled-flow", enabled=False),
            ]
        ),
    )

    items = client.get("/api/v1/scheduled/bindable-workflows").json()["items"]

    assert [i["id_or_name"] for i in items] == ["weekly-report"]
    assert items[0]["form"] == "personal_skill"
    assert items[0]["is_own"] is True


def test_team_creator_only_hidden_from_non_creator(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(
        monkeypatch,
        user_store=_UserStore(
            skills=[
                _team_skill("oskill_open"),
                _team_skill("oskill_private", visibility="creator_only", created_by="user_2"),
                _team_skill("oskill_mine", visibility="creator_only", created_by="user_1"),
            ]
        ),
    )

    items = client.get("/api/v1/scheduled/bindable-workflows").json()["items"]

    ids = {i["id_or_name"] for i in items}
    assert ids == {"oskill_open", "oskill_mine"}
    assert next(i for i in items if i["id_or_name"] == "oskill_mine")["is_own"] is True
    assert next(i for i in items if i["id_or_name"] == "oskill_open")["author"] == "user_2"
    # UI must not show u_xxx; resolve author ID to a readable name.
    assert next(i for i in items if i["id_or_name"] == "oskill_open")["author_name"] == "张三"


def test_subagents_included_with_team_form(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.agents import plan_admission

    async def _allowed(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(plan_admission, "team_agent_runtime_allowed", _allowed)
    client = _app(
        monkeypatch,
        user_store=_UserStore(
            subagents=[
                SimpleNamespace(id="sub_1", name="monthly", display_name="月度复盘", description="", visibility="team_wide", created_by="user_2")
            ]
        ),
    )

    items = client.get("/api/v1/scheduled/bindable-workflows").json()["items"]

    assert items == [
        {
            "form": "team_subagent",
            "id_or_name": "sub_1",
            "display_name": "月度复盘",
            "description": "",
            "author": "user_2",
            "author_name": "张三",
            "is_own": False,
        }
    ]


def test_unassigned_subagent_is_not_bindable_after_runtime_cutover(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.agents import plan_admission

    async def _denied(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(plan_admission, "team_agent_runtime_allowed", _denied)
    client = _app(
        monkeypatch,
        user_store=_UserStore(
            subagents=[
                SimpleNamespace(
                    id="sub_1",
                    name="monthly",
                    display_name="月度复盘",
                    description="",
                    visibility="team_wide",
                    created_by="user_2",
                ),
            ],
        ),
    )

    assert client.get("/api/v1/scheduled/bindable-workflows").json()["items"] == []


def test_one_broken_source_does_not_sink_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个源挂掉不该让整个选择器空掉——用户至少还能绑到别的流程。"""

    class _Broken:
        async def list_for_user(self, *, user_id: str):
            raise RuntimeError("boom")

    client = _app(
        monkeypatch,
        skill_instance_store=_Broken(),
        user_store=_UserStore(skills=[_team_skill("oskill_open")]),
    )

    items = client.get("/api/v1/scheduled/bindable-workflows").json()["items"]

    assert [i["id_or_name"] for i in items] == ["oskill_open"]


# ─── PATCH binding path ────────────────────────────────────


class _PatchStore:
    """记录 patch 收到的 kwargs —— 用来钉住「DTO 收了但服务端没往下传」这类静默丢弃。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.job = SimpleNamespace(
            id="job_1",
            user_id="user_1",
            workspace_id="ws_1",
            name="D",
            cron="0 9 * * *",
            tz="Asia/Shanghai",
            query="q",
            session_strategy="fresh",
            anchor_session_id=None,
            notify_channels={},
            status="paused",
            failure_count=0,
            next_run_at=None,
            last_run_at=None,
            last_turn_id=None,
            created_at="t",
            updated_at="t",
            public_entry_id=None,
            pause_reason="scheduled_recipe_lost",
            recipe_ref=None,
        )

    async def get(self, _job_id: str):
        return self.job

    async def patch(self, job_id: str, **fields):
        self.calls.append(fields)
        return True

    async def count_active(self, _user_id: str) -> int:
        return 0

    async def set_next_run_at(self, _job_id: str, _next_run_at: str | None) -> bool:
        return True


class _OneSkillStore:
    def __init__(self, name: str, source_path: str) -> None:
        self._name, self._path = name, source_path

    async def get(self, *, user_id: str, name: str):
        if name != self._name:
            return None
        return SimpleNamespace(source_path=self._path, description="周报", enabled=True)


def _skill_on_disk(tmp_path, name: str = "weekly-report") -> str:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 周报\nversion: 1.0.0\n---\n\n1. 取数\n2. 出报告\n",
        encoding="utf-8",
    )
    return str(d)


def test_patch_forwards_binding_to_store(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """★改绑必须真的落到 store。

    FE 能提交、路由 DTO 也收了，但若 handler 不往 store.patch 传 recipe_ref，编辑面的
    改绑/解绑/重新确认全部**静默丢弃**——而前端看起来还是成功的。
    """
    store = _PatchStore()
    client = _app(
        monkeypatch,
        scheduled_store=store,
        # user_store=None bypasses space authorization (existing design); this case only checks binding persistence.
        skill_instance_store=_OneSkillStore("weekly-report", _skill_on_disk(tmp_path)),
    )

    resp = client.patch(
        "/api/v1/scheduled/jobs/job_1",
        json={"recipe_ref": {"form": "personal_skill", "id_or_name": "weekly-report"}},
    )

    assert resp.status_code == 200, resp.text
    assert store.calls, "patch 必须被调用"
    assert "recipe_ref" in store.calls[0], "recipe_ref 没往下传 = 改绑被静默丢弃"
    assert "weekly-report" in (store.calls[0]["recipe_ref"] or "")


def test_patch_unbind_clears_the_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _PatchStore()
    client = _app(monkeypatch, scheduled_store=store)

    resp = client.patch("/api/v1/scheduled/jobs/job_1", json={"recipe_ref": {"unbind": True}})

    assert resp.status_code == 200, resp.text
    assert store.calls[0]["recipe_ref"] == "", "解绑要落成空值，store 层再转 NULL"


def test_patch_without_recipe_ref_leaves_binding_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """只改名字时不得把已有绑定顺手解掉（None = 不动）。"""
    store = _PatchStore()
    client = _app(monkeypatch, scheduled_store=store)

    resp = client.patch("/api/v1/scheduled/jobs/job_1", json={"name": "改个名"})

    assert resp.status_code == 200, resp.text
    assert store.calls[0]["recipe_ref"] is None


def test_reenable_team_agent_refreshes_legacy_ref_and_exact_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activation is the explicit reconfirm action for a drift-paused Agent."""
    from bowhead_gateway.routes import scheduled

    store = _PatchStore()
    store.job.recipe_ref = json.dumps(
        {
            "form": "team_subagent",
            "id_or_name": "osa_monthly",
            "bound_sha": "sha256:old",
        },
    )
    store.job.pause_reason = "scheduled_recipe_changed"
    rebound_ref = json.dumps(
        {
            "form": "team_subagent",
            "id_or_name": "osa_monthly",
            "bound_sha": "sha256:new",
        },
    )
    binding_seed = object()
    bind_calls: list[object] = []

    async def fake_bind(_request, raw, *, user_id):  # noqa: ANN001, ANN202
        bind_calls.append(raw)
        return None if raw is None else rebound_ref

    async def fake_prepare(_request, recipe_ref, *, user_id):  # noqa: ANN001, ANN202
        assert recipe_ref == rebound_ref
        return binding_seed

    async def fake_check(_request, _job, recipe_ref=None):  # noqa: ANN001, ANN202
        assert recipe_ref == rebound_ref
        return None, None

    monkeypatch.setattr(scheduled, "_bind_recipe_ref", fake_bind)
    monkeypatch.setattr(scheduled, "_prepare_agent_binding", fake_prepare)
    monkeypatch.setattr(scheduled, "_stored_recipe_check", fake_check)
    client = _app(monkeypatch, scheduled_store=store)

    response = client.patch(
        "/api/v1/scheduled/jobs/job_1",
        json={"status": "active"},
    )

    assert response.status_code == 200, response.text
    assert bind_calls == [None, {"form": "team_subagent", "id_or_name": "osa_monthly"}]
    assert store.calls[0]["recipe_ref"] == rebound_ref
    assert store.calls[0]["agent_binding"] is binding_seed


# ── List-row binding echo (S1 FR-1.5 / FR-3.2; 116 dogfood P2-1 coverage) ─────


class _JobStore:
    """只实现 list_jobs 用到的那一个方法。"""

    def __init__(self, jobs: list[SimpleNamespace]) -> None:
        self._jobs = jobs

    async def list_for_user(self, _user_id: str) -> list[SimpleNamespace]:
        return self._jobs


def _job(job_id: str, recipe_ref: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=job_id,
        user_id="user_1",
        workspace_id="ws_1",
        name=job_id,
        cron="0 9 * * *",
        tz="Asia/Shanghai",
        query="q",
        session_strategy="fresh",
        anchor_session_id=None,
        notify_channels={},
        status="active",
        failure_count=0,
        next_run_at=None,
        last_run_at=None,
        last_turn_id=None,
        created_at="2026-08-05T00:00:00+00:00",
        updated_at="2026-08-05T00:00:00+00:00",
        public_entry_id=None,
        pause_reason=None,
        recipe_ref=recipe_ref,
    )


def test_list_enriches_bound_recipe_name_and_foreign_author(monkeypatch: pytest.MonkeyPatch) -> None:
    """绑定他人团队流程 → 行上给得出人话名 + 作者。

    `recipe_ref` 里存的是 `oskill_*` 这种不可读 id，不 enrich 的话前端渲染不出「绑了哪份
    流程、谁写的」，US-6 的借权可见性在列表层就是空的。
    """
    client = _app(
        monkeypatch,
        scheduled_store=_JobStore([_job("sj_1", '{"form": "team_skill", "id_or_name": "oskill_open"}')]),
        user_store=_UserStore(skills=[_team_skill("oskill_open", created_by="user_2")]),
    )

    items = client.get("/api/v1/scheduled/jobs").json()["items"]

    assert items[0]["bound_display_name"] == "oskill_open 显示名"
    assert items[0]["bound_author"] == "张三"


def test_own_recipe_shows_no_author(monkeypatch: pytest.MonkeyPatch) -> None:
    """自己的流程不标作者——标自己是噪声；作者那半句只在借他人流程时才有意义。"""
    client = _app(
        monkeypatch,
        scheduled_store=_JobStore([_job("sj_1", '{"form": "personal_skill", "id_or_name": "weekly-report"}')]),
        skill_instance_store=_SkillStore([_skill("weekly-report")]),
    )

    items = client.get("/api/v1/scheduled/jobs").json()["items"]

    assert items[0]["bound_display_name"] == "weekly-report"
    assert items[0]["bound_author"] is None


def test_lost_binding_enriches_to_null_not_a_stale_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """流程已删/失权 → 两个字段都 None（失效由 pause_reason 说，别让列表撒谎说还绑着）。"""
    client = _app(
        monkeypatch,
        scheduled_store=_JobStore([_job("sj_1", '{"form": "personal_skill", "id_or_name": "已删掉的流程"}')]),
        skill_instance_store=_SkillStore([]),
    )

    items = client.get("/api/v1/scheduled/jobs").json()["items"]

    assert items[0]["bound_display_name"] is None
    assert items[0]["bound_author"] is None


def test_unbound_job_skips_the_aggregation_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有任何绑定时不去聚合可绑集——一次多余 IO 都不做。"""
    calls: list[str] = []

    class _CountingSkillStore(_SkillStore):
        async def list_for_user(self, *, user_id: str):
            calls.append(user_id)
            return []

    client = _app(
        monkeypatch,
        scheduled_store=_JobStore([_job("sj_1", None)]),
        skill_instance_store=_CountingSkillStore([]),
    )

    items = client.get("/api/v1/scheduled/jobs").json()["items"]

    assert items[0]["bound_display_name"] is None
    assert calls == []


def test_enrichment_failure_does_not_break_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """附加信息取不到就不显示，列表页不能因此整页红。"""

    class _BoomSkillStore:
        async def list_for_user(self, *, user_id: str):
            raise RuntimeError("skill store down")

    client = _app(
        monkeypatch,
        scheduled_store=_JobStore([_job("sj_1", '{"form": "personal_skill", "id_or_name": "weekly-report"}')]),
        skill_instance_store=_BoomSkillStore(),
    )

    resp = client.get("/api/v1/scheduled/jobs")

    assert resp.status_code == 200
    assert resp.json()["items"][0]["bound_display_name"] is None
