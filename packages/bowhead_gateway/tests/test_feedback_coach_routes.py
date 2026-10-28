"""教练直连面（批 3d）契约：只到本人且 pending/overridden、整份 SKILL.md 起草、三道结构闸（name 不变 / 不许编工具名 /
零 diff 不给预填）、日限 3 且空跑退槽、清点结果按红黄走消毒通道（truncated 只说至少）、可用名单进配置面。
教练打桩；skill 实例与 bundle 目录是真的；节流店是真迁移库。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.coach_drafting import CoachDraft, CoachResult
from bowhead_core.feedback_casework_store import FeedbackCaseworkStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import feedback_coach as fcr
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bowhead_core import migrations
from bowhead_gateway import feedback_surfaces as fs

BODY = "---\nname: sales-brief\ndescription: d\nallowed-tools: [nonexistent_tool]\n---\n\n先查 https://docs.internal/spec 再写。\n"
URL = "/api/v1/feedback-coach/user-skills/sales-brief/draft"


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


_REPORT = {
    "version": 1,
    "grade": "red",
    "red_count": 1,
    "truncated": True,
    "findings": [
        {
            "id": "f1",
            "code": "tool_ref_unresolved",
            "severity": "red",
            "path": "SKILL.md",
            "line": 4,
            "subject": "nonexistent_tool",
            "message_zh": "工具不存在",
        },
        {"id": "f2", "code": "frontmatter_gap", "severity": "yellow", "subject": "description", "message_zh": "描述太短"},
    ],
}


class _Instances:
    def __init__(self, skill_dir: Path) -> None:
        def row(name: str, user: str, status: str, report: str | None) -> SimpleNamespace:
            return SimpleNamespace(name=name, user_id=user, source_path=str(skill_dir), import_status=status, import_report_json=report, version="1")

        self.rows = {
            ("u_me", "sales-brief"): row("sales-brief", "u_me", "pending", json.dumps(_REPORT)),
            ("u_me", "fine"): row("fine", "u_me", "cleared", None),
            ("u_me", "sales-brief-live"): row("sales-brief", "u_me", "overridden", "not json{"),
            ("u_other", "theirs"): row("theirs", "u_other", "pending", None),
        }

    async def get(self, *, user_id: str, name: str):
        return self.rows.get((user_id, name))


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    skill_dir = tmp_path / "sales-brief"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(BODY, encoding="utf-8")
    (skill_dir / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
    app = FastAPI()
    app.include_router(fcr.router, prefix="/api/v1/feedback-coach")
    app.state.skill_instance_store = _Instances(skill_dir)
    app.state.feedback_casework_store = FeedbackCaseworkStore(_migrated_db(tmp_path))
    app.dependency_overrides[fcr._current_user] = lambda: User(id="u_me", team_id="t1", email="me@x.com")

    async def _router(app_state, team_id, **kw):  # noqa: ANN001, ANN003
        return SimpleNamespace()

    monkeypatch.setattr(fcr, "team_router_or_409", _router)

    async def _sources(request, user):  # noqa: ANN001
        return SimpleNamespace(tool_names=frozenset({"bi_query", "bi_schema"}), skill_names=frozenset({"chart-style"}))

    monkeypatch.setattr(fcr, "_resolution_sources", _sources)
    seen: dict = {}
    state = {
        "result": CoachResult(
            CoachDraft(
                "edit",
                BODY.replace("[nonexistent_tool]", "[bi_query]"),
                None,
                "我改了什么：…\n我没改什么：…\n你还需要做什么：…",
                findings_addressed=("f1", "f_not_in_report"),
            )
        )
    }

    async def _draft(router, **kw):  # noqa: ANN001, ANN003
        seen.update(kw)
        return state["result"]

    monkeypatch.setattr(fcr, "draft_config_edit_ex", _draft)
    return SimpleNamespace(client=TestClient(app), seen=seen, state=state, skill_dir=skill_dir, cw=app.state.feedback_casework_store)


def test_only_own_pending_or_overridden_skill_is_coachable(env):
    assert env.client.post("/api/v1/feedback-coach/user-skills/theirs/draft").status_code == 404  # Someone else's.
    assert env.client.post("/api/v1/feedback-coach/user-skills/fine/draft").status_code == 404  # Already cleared: nothing to change.
    assert env.client.post("/api/v1/feedback-coach/user-skills/nope/draft").status_code == 404
    r = env.client.post(URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["degraded"] is False and body["prefill"]["kind"] == "user_skill_body" and "[bi_query]" in body["prefill"]["edited_content"]
    assert body["base"]["body_sha256"] == fs.sha256_text(BODY) and body["base"]["import_status"] == "pending"
    assert body["report"] == {"red_count": 1, "yellow_count": 1, "truncated": True, "findings_addressed": ["f1"]}  # Unknown IDs rejected.
    assert body["banner"]["text"] == "此内容由教练起草，来源不可信，保存前逐行审阅"


def test_coach_sees_body_available_names_and_findings_by_severity(env):
    env.client.post(URL)
    assert (
        env.seen["target_kind"] == "user_skill" and env.seen["mounted_assets"] == [] and env.seen["timeout_sec"] == fcr.USER_SKILL_DRAFT_TIMEOUT_SEC
    )
    surface = env.seen["config_surface"]
    assert surface["body_raw"] == BODY and surface["files"] == [{"path": "scripts/run.py", "size": 9}]
    assert surface["available"] == {"tools": ["bi_query", "bi_schema"], "skills": ["chart-style"]}  # Coach renaming must choose from here.
    assert "findings" not in surface["import_report"] and surface["import_report"]["finding_ids"] == ["f1", "f2"]  # findings via sanitizer.
    excerpts = "\n".join(env.seen["evidence_excerpts"])
    assert "红色阻断项（至少 1 条" in excerpts and "[f1] [tool_ref_unresolved] nonexistent_tool SKILL.md:4 — 工具不存在" in excerpts
    assert "黄色提示（至少 1 条" in excerpts and "[f2] [frontmatter_gap] description  — 描述太短" in excerpts  # No path: do not render :None.


def test_overridden_is_coachable_and_malformed_report_does_not_crash(env):
    r = env.client.post("/api/v1/feedback-coach/user-skills/sales-brief-live/draft")
    assert r.status_code == 200 and r.json()["base"]["import_status"] == "overridden"
    assert env.seen["config_surface"]["import_report"]["finding_ids"] == [] and env.seen["config_surface"]["skill"]["import_status"] == "overridden"


def test_renamed_unchanged_and_failed_drafts_degrade_instead_of_prefilling(env):
    env.state["result"] = CoachResult(CoachDraft("edit", BODY.replace("name: sales-brief", "name: sales-brief-v2"), None, "改名了"))
    r = env.client.post(URL).json()
    assert r["degraded"] is True and r["degrade_reason"] == "name_changed" and r["prefill"] is None
    env.state["result"] = CoachResult(CoachDraft("edit", BODY, None, "改不了：stdio MCP 需要部署侧开关"))
    r = env.client.post(URL).json()
    assert r["degraded"] is True and r["degrade_reason"] == "no_change" and "部署侧" in r["rationale"]
    env.state["result"] = CoachResult(None, "url_guard")
    r = env.client.post(URL).json()
    assert r["degraded"] is True and r["degrade_reason"] == "url_guard"  # Output leaks a URL: consumes a slot (not an empty run).
    env.state["result"] = CoachResult(CoachDraft("reroute", None, "x", "y"))
    assert env.client.post("/api/v1/feedback-coach/user-skills/sales-brief-live/draft").json()["degrade_reason"] == "coach_rerouted"


def test_invented_tool_missing_trailing_newline_and_crlf_are_structural_gates(env):
    env.state["result"] = CoachResult(CoachDraft("edit", BODY.replace("[nonexistent_tool]", "[bi_query, made_up_tool]"), None, "x"))
    r = env.client.post(URL).json()
    assert r["degraded"] is True and r["degrade_reason"] == "invented_tool" and r["invented"] == ["made_up_tool"]
    # Save-path frontmatter validation does not require a trailing newline; do not falsely report name_changed here either.
    env.state["result"] = CoachResult(CoachDraft("edit", "---\nname: sales-brief\ndescription: d\nallowed-tools: [bi_query]\n---\n改了", None, "x"))
    assert env.client.post(URL).json()["degraded"] is False
    # Normalize CRLF output: no whole-file diff from line endings, and do not miss a zero diff.
    env.state["result"] = CoachResult(CoachDraft("edit", BODY.replace("\n", "\r\n"), None, "x"))
    r = env.client.post(URL).json()
    assert r["degraded"] is True and r["degrade_reason"] == "no_change"


def test_daily_throttle_refunds_zero_product_and_is_not_taken_before_the_call(env, monkeypatch):
    env.state["result"] = CoachResult(None, "timeout")
    for _ in range(5):
        assert env.client.post(URL).json()["degrade_reason"] == "coach_unavailable"  # Empty runs do not lock users out.
    env.state["result"] = CoachResult(CoachDraft("edit", BODY.replace("[nonexistent_tool]", "[bi_query]"), None, "x"))
    for _ in range(3):
        assert env.client.post(URL).status_code == 200
    r = env.client.post(URL)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "feedback_redraft_throttled"
    # Check oversized bodies before consuming a slot; unconfigured-model 409 also consumes none.
    (env.skill_dir / "SKILL.md").write_text(BODY + "x" * 20_000, encoding="utf-8")
    assert env.client.post(URL).json()["degrade_reason"] == "surface_too_large"
    (env.skill_dir / "SKILL.md").write_text(BODY, encoding="utf-8")

    async def _no_router(app_state, team_id, **kw):  # noqa: ANN001, ANN003
        raise HTTPException(status_code=409, detail={"code": "team_model_unconfigured"})

    monkeypatch.setattr(fcr, "team_router_or_409", _no_router)
    assert env.client.post(URL).status_code == 409
    assert _run(env.cw.get(fcr._throttle_key("u_me", "sales-brief"))).redraft_count == 3
