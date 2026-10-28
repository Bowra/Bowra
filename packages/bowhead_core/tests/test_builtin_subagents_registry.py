"""followup-context-deck #12 — 内置 subagent (manifest peers) 数据源.

GET /api/v1/team-subagents/builtins 用 AgentRegistry.bootstrap() 列系统内置子代理给 UI
只读展示. 测数据源: 期望的 manifest peers 在、各有 name/model/description, lead 可被排除.
"""

from __future__ import annotations

from bowhead_core.agents.registry import AgentRegistry


def test_bootstrap_lists_system_manifest_peers():
    reg = AgentRegistry.bootstrap()
    names = reg.list_all()
    # Built-in subagents visible in user traces (manifest system peers).
    for expected in ("consultant", "financial-analyst", "kb-analyst"):
        assert expected in names, names
    # lead exists (the endpoint explicitly excludes it: the orchestrator itself, not a dispatched subagent).
    assert "lead" in names


def test_each_builtin_has_name_model_description():
    reg = AgentRegistry.bootstrap()
    for name in reg.list_all():
        if name == "lead":
            continue
        m = reg.get(name)
        assert m.name == name
        assert str(m.model), f"{name} missing model"
        assert m.description and len(m.description) >= 1, f"{name} missing description"
