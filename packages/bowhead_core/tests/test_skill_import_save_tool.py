"""skill-import 批1：对话保存路径（skill_save_generated 工具）过钩子；turn 内解析源装配；定时绑定的 pending 闸."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.skill_import.hook import sources_from_ctx
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.skill_save_tool import register_skill_save_tool
from bowhead_core.tool_registry import ToolRegistry
from bowhead_core.workflow_binding import ERR_LOST, _resolve_personal

_BODY = """---
name: gen-recipe
description: 对话生成
allowed-tools: {tools}
---
## 步骤
1. go
"""


def _handler(registry: ToolRegistry):
    register_skill_save_tool(registry)
    return registry.get("skill_save_generated").handler


def _ctx(store, registry):
    return SimpleNamespace(
        skill_instance_store=store, user_id="alice", turn_id="t_1", skill_catalog=None, team_skill_store=None, team_id=None, tool_view=registry
    )


@pytest.mark.asyncio
async def test_save_tool_runs_hook_and_reports_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SkillInstanceStore(str(tmp_path / "u.db"))
    await store.initialize()
    registry = ToolRegistry()
    monkeypatch.setattr(registry, "list_names", lambda: ["bi_query", "skill_save_generated"])
    handler = _handler(registry)
    ctx = _ctx(store, registry)

    out = await handler({"body_md": _BODY.format(tools="data_fetch_skill")}, ctx)
    assert out["ok"] is True and out["import_status"] == "pending"
    assert "待处理导入" in out["hint"]
    assert "/skills/" in out["hint"] and "/settings/skills" not in out["hint"]
    inst = await store.get(user_id="alice", name="gen-recipe")
    assert inst.import_pending and inst.enabled is True

    # Disable first, then overwrite: retain the enabled preference; clearing red findings yields cleared status.
    await store.set_enabled(user_id="alice", name="gen-recipe", enabled=False)
    out = await handler({"body_md": _BODY.format(tools="bi_query"), "overwrite": True}, ctx)
    assert out["ok"] is True and out["import_status"] == "cleared"
    assert "/skills/" in out["hint"] and "/settings/skills" not in out["hint"]
    inst = await store.get(user_id="alice", name="gen-recipe")
    assert inst.enabled is False and inst.import_status == "cleared"


@pytest.mark.asyncio
async def test_save_tool_reports_incomplete_inventory_and_retains_pending_review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SkillInstanceStore(str(tmp_path / "u.db"))
    await store.initialize()
    registry = ToolRegistry()
    handler = _handler(registry)
    ctx = _ctx(store, registry)
    await handler({"body_md": _BODY.format(tools="data_fetch_skill")}, ctx)
    assert (await store.get(user_id="alice", name="gen-recipe")).import_pending
    import bowhead_core.skill_save_tool as mod

    async def _boom(*a, **k):
        raise RuntimeError("sources down")

    monkeypatch.setattr(mod, "sources_from_ctx", _boom)
    out = await handler({"body_md": _BODY.format(tools="bi_query"), "overwrite": True}, ctx)
    assert out["ok"] is True and out["import_status"] == "pending"
    assert out["inventory_status"] == "not_completed"
    after = await store.get(user_id="alice", name="gen-recipe")
    assert after.import_status == "pending" and after.import_report_json is not None


@pytest.mark.asyncio
async def test_sources_from_ctx_marks_team_tools_unknown_without_overlay(tmp_path):
    registry = ToolRegistry()
    registry.list_names = lambda: ["bi_query"]  # type: ignore[method-assign]
    ctx = SimpleNamespace(
        tool_view=None,
        skill_catalog=SimpleNamespace(list_names=lambda: ["analyst-framework"]),
        skill_instance_store=None,
        user_id="alice",
        team_skill_store=None,
        team_id=None,
    )
    src = await sources_from_ctx(ctx, registry)
    assert "bi_query" in src.tool_names and "analyst-framework" in src.skill_names
    assert src.team_tools_known is False
    # Unreadable registry ⇒ skip tool rules (None), rather than treating it as empty and grading references red.
    broken = SimpleNamespace(list_names=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    src2 = await sources_from_ctx(
        SimpleNamespace(tool_view=None, skill_catalog=None, skill_instance_store=None, user_id=None, team_skill_store=None, team_id=None), broken
    )
    assert src2.tool_names is None


@pytest.mark.asyncio
async def test_workflow_binding_does_not_inject_pending_import(tmp_path):
    d = tmp_path / "wf"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: wf\ndescription: d\n---\n## steps\n", encoding="utf-8")
    rec = SimpleNamespace(name="wf", enabled=True, source_path=str(d), import_status="pending")

    class _S:
        async def get(self, *, user_id, name):
            return rec

    bound, err = await _resolve_personal("wf", user_id="alice", store=_S())
    assert bound is None and err == ERR_LOST
    rec.import_status = "cleared"
    bound, err = await _resolve_personal("wf", user_id="alice", store=_S())
    assert err is None and bound is not None


@pytest.mark.asyncio
async def test_saved_skill_receipt_includes_yellow_findings_without_trigger_guarantee(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SkillInstanceStore(str(tmp_path / "u.db"))
    await store.initialize()
    registry = ToolRegistry()
    out = await _handler(registry)(
        {"body_md": "---\nname: generic-method\ndescription: Clarify a data question\n---\nAsk only necessary questions.\n"}, _ctx(store, registry)
    )
    assert out["ok"] and out["enabled"] is True
    assert out["inspection_target_matches_saved"] is True
    assert any(f["subject"] == "trigger_examples" and f["severity"] == "yellow" for f in out["import_review"]["findings"])
    assert out["trigger_verification"] == out["trial"] == "not_run"
