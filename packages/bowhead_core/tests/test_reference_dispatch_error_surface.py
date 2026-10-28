"""Reference dispatch refusals retain model guidance without leaking it to the user."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.subagent import _public_tool_result, register_subagent_tool
from bowhead_core.tool_registry import ToolRegistry


class _RefusingRuntime:
    def __init__(self, code: str) -> None:
        self.code = code

    async def binding_for_delegation(self, **_kwargs: object) -> None:
        raise ValueError(self.code)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_primary_role_is_self_correctable_without_raw_exception() -> None:
    registry = ToolRegistry()
    register_subagent_tool(registry)
    bus = SimpleNamespace()
    ctx = SimpleNamespace(subagent_runner=object(), parent_bus=bus, _reference_lead_runtime=_RefusingRuntime("reference_delegation_role_required"))

    result = await registry.get("subagent_spawn").handler({"profile": "compute", "task": "分析"}, ctx)

    assert result["error_kind"] == "invalid_args"
    assert result["ok"] is False
    assert "delegation_role=helper" in result["error"]
    assert "exact selected Agent profile or agent_id" in result["error"]
    public, _ = _public_tool_result("subagent_spawn", result)
    assert public["error"] == "The dispatch arguments need correction."
    assert "reference_delegation_role_required" not in repr(result)
    assert not getattr(bus, "_reference_continuation_unavailable", False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_second_primary_dispatch_is_permanent_and_public_result_is_safe() -> None:
    registry = ToolRegistry()
    register_subagent_tool(registry)
    bus = SimpleNamespace()
    ctx = SimpleNamespace(subagent_runner=object(), parent_bus=bus, _reference_lead_runtime=_RefusingRuntime("reference_primary_already_dispatched"))

    result = await registry.get("subagent_spawn").handler({"profile": "compute", "task": "分析", "delegation_role": "primary"}, ctx)
    public, _ = _public_tool_result("subagent_spawn", result)

    assert result["error_kind"] == "reference_dispatch_blocked"
    assert getattr(bus, "_reference_continuation_unavailable", False)
    assert public["error_kind"] == "reference_dispatch_blocked"
    assert "reference_primary_already_dispatched" not in repr(public)
    # Public copy follows the UI language; without language context it is English
    # (docs/design/language-policy.md, "AI answers": deterministic product copy falls back to English).
    assert "cannot be dispatched again" in public["error"]
    from bowhead_core.agents._lead_context import reset_current_ui_language, set_current_ui_language

    token = set_current_ui_language("zh-CN")
    try:
        public_zh, _ = _public_tool_result("subagent_spawn", result)
    finally:
        reset_current_ui_language(token)
    assert "不能重复" in public_zh["error"]
    assert "reference_primary_already_dispatched" not in repr(public_zh)
