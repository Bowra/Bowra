"""Stable connector selection prompts and supported refusal contracts.

Switch outcomes are tested as facts in test_tool_switch_context and test_datarc_selection.
"""

from __future__ import annotations

import pytest
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    ToolRegistry,
    build_default_registry,
)

WS2 = ({"id": "42", "name": "零售集团"}, {"id": "7", "name": "华东大区"})
WS3 = (
    {"id": "1", "name": "商超零售场景"},
    {"id": "234", "name": "北极九章DEMO"},
    {"id": "235", "name": "北极九章TEST"},
)


class _Switcher:
    def __init__(self, fail: bool = False, persisted: bool = True) -> None:
        self.fail = fail
        self.persisted = persisted

    async def __call__(self, ws_id: str, ws_name: str = "", *, remember: bool = False) -> tuple[str, bool]:
        if self.fail:
            raise RuntimeError("resolver down")
        return f"member_of_{ws_id}", self.persisted


def _ctx(accessible: tuple = WS3, switcher: object = None, active: str = "1", member: str = "m_old") -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_x",
        member_uid=member,
        user_id="u",
        turn_id="t",
        datarc=object(),
        active_datarc_project=active,
        accessible_datarc_workspaces=accessible,
        datarc_context_switcher=switcher,
    )


@pytest.fixture(scope="module")
def reg() -> ToolRegistry:
    return build_default_registry()


async def test_golden_sentinel_prompt(reg: ToolRegistry) -> None:
    ctx = _ctx(accessible=WS2, switcher=None, active="")
    inv = await reg.execute("bi_query", {"text": "今年毛利"}, ctx)
    assert inv.result["clarification"]["prompt"] == "你在北极九章有 2 个可访问的工作区，这次查询用哪个？"


async def test_golden_no_project_error(reg: ToolRegistry) -> None:
    """快照 ≤1 → DatarcUnavailableError 裸错（LLM 可见，前缀+正文逐字节冻结）."""
    ctx = _ctx(accessible=(), switcher=None, active="")
    inv = await reg.execute("bi_query", {"text": "x"}, ctx)
    assert inv.error == (
        "DatarcUnavailableError: tool='bi_query' needs a datarc project, but this "
        "workspace isn't linked to one. hint: answer from the knowledge base "
        "(kb_search / kb_query_records), or select a datarc project for this workspace."
    )


async def test_golden_channel_not_supported_hint(reg: ToolRegistry) -> None:
    ctx = _ctx(switcher=None)
    inv = await reg.execute("tool_switch_context", {"name": "北极九章DEMO"}, ctx)
    assert inv.result["error"] == "channel_not_supported"
    assert inv.result["hint"] == "本通道暂不支持切换北极九章工作区，请到网页端会话中说「换 XX」切换。"


async def test_golden_no_accessible_hint(reg: ToolRegistry) -> None:
    ctx = _ctx(accessible=(), switcher=_Switcher())
    inv = await reg.execute("tool_switch_context", {"name": "任意"}, ctx)
    assert inv.result["error"] == "no_accessible_workspaces"
    assert inv.result["hint"] == "当前通道无可切换的工作区（无身份/无账号/通道不支持）。"


async def test_golden_ambiguity_clarification_prompt(reg: ToolRegistry) -> None:
    """「北极九章」substring 命中 2 → 确认卡 prompt 逐字节冻结."""
    ctx = _ctx(switcher=_Switcher())
    inv = await reg.execute("tool_switch_context", {"name": "北极九章"}, ctx)
    clar = inv.result["clarification"]
    assert clar["prompt"] == "「北极九章」匹配到 2 个工作区，请确认用哪个："
    assert clar["tool"] == "datarc"


async def test_golden_schema_enum(reg: ToolRegistry) -> None:
    spec = reg.get("tool_switch_context")
    assert spec.schema.input_schema["properties"]["tool"]["enum"] == ["datarc"]


async def test_golden_unsupported_tool_refusal(reg: ToolRegistry) -> None:
    """非 datarc 输入 → unsupported_tool（表驱动后 binding miss 必须同形状）."""
    ctx = _ctx(switcher=_Switcher())
    inv = await reg.execute("tool_switch_context", {"tool": "pbi", "name": "x"}, ctx)
    assert inv.result == {"ok": False, "error": "unsupported_tool", "tool": "pbi"}
