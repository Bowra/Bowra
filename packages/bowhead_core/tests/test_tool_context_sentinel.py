"""批2/c4 — dispatch 闸 sentinel 化 + clarification 契约扩展（PRD §3.3b）.

契约链：bi_* 无工作区 + 快照>1 → shape-2 sentinel(kind=tool_context_select,
options=(value=id,label=name)) → is_clarification_sentinel 认 →
extract_clarification_event_payload 保真透传 pairs/kind/remember。
"""

from __future__ import annotations

import pytest
from bowhead_core.agents.clarification_tool import (
    extract_clarification_event_payload,
    is_clarification_sentinel,
)
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    ToolRegistry,
    build_default_registry,
)

WS2 = ({"id": "42", "name": "零售集团"}, {"id": "7", "name": "华东大区"})


def _ctx(accessible=(), active: str = "") -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_x",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=object(),
        active_datarc_project=active,
        accessible_datarc_workspaces=accessible,
    )


@pytest.fixture(scope="module")
def reg():
    return build_default_registry()


async def test_no_project_with_multi_snapshot_yields_sentinel(reg):
    inv = await reg.execute("bi_query", {"text": "今年毛利"}, _ctx(accessible=WS2))
    assert inv.error is None
    assert is_clarification_sentinel(inv.result)
    clar = inv.result["clarification"]
    assert clar["kind"] == "tool_context_select"
    assert clar["options"] == [
        {"value": "42", "label": "零售集团"},
        {"value": "7", "label": "华东大区"},
    ]
    assert clar["remember_option"] is True


async def test_no_project_empty_snapshot_keeps_legacy_error(reg):
    """legacy 模式/缺席矩阵：快照空 → 原 DatarcUnavailableError 不变（零行为）."""
    inv = await reg.execute("bi_query", {"text": "今年毛利"}, _ctx(accessible=()))
    assert inv.result is None
    assert inv.error and "DatarcUnavailableError" in inv.error


async def test_single_snapshot_no_sentinel(reg):
    """集合=1 在 resolve 层就 auto 选中，不该走到这；若走到（异常态）保持报错不弹卡."""
    inv = await reg.execute("bi_query", {"text": "x"}, _ctx(accessible=WS2[:1]))
    assert inv.error and "DatarcUnavailableError" in inv.error


async def test_with_project_no_sentinel_handler_runs(reg):
    """有工作区 → 闸放行进 handler（handler 对假 datarc 报错也好过误弹卡）."""
    inv = await reg.execute("bi_query", {"text": "x"}, _ctx(accessible=WS2, active="42"))
    assert not is_clarification_sentinel(inv.result)


def test_extract_payload_preserves_pairs_kind_remember():
    sent = {
        "ok": False,
        "error_kind": "needs_clarification",
        "clarification": {
            "kind": "tool_context_select",
            "prompt": "选一个",
            "options": [{"value": "42", "label": "零售集团"}],
            "remember_option": True,
            "remember_default": False,
        },
        "rows": [],
    }
    p = extract_clarification_event_payload(sent)
    assert p["candidates"] == ["零售集团"]
    assert p["option_pairs"] == [{"value": "42", "label": "零售集团"}]
    assert p["clar_kind"] == "tool_context_select"
    assert p["remember_option"] is True
    assert p["remember_default"] is False
    assert p["fallback_text"] == "选一个"


def test_extract_payload_legacy_shape2_degrades():
    """旧 shape-2 生产者（bi_query 槽位歧义）：无 kind/remember，pairs 退化 value=label."""
    sent = {
        "ok": False,
        "error_kind": "needs_clarification",
        "clarification": {"prompt": "哪个指标?", "options": ["营业额", "毛利"]},
        "rows": [],
    }
    p = extract_clarification_event_payload(sent)
    assert p["candidates"] == ["营业额", "毛利"]
    assert p["option_pairs"] == [
        {"value": "营业额", "label": "营业额"},
        {"value": "毛利", "label": "毛利"},
    ]
    assert p["clar_kind"] is None
    assert p["remember_option"] is False


async def test_sentinel_payload_tool_matches_connector_table(reg: ToolRegistry) -> None:
    """T1#7 pin：sentinel payload 的判别符/prompt 显示名从 CONNECTOR_NAMESPACES/
    DISPLAY_NAMES 表取值后与 v1 字面量字节等价（tool=="datarc"、prompt 含「北极九章」）."""
    from bowhead_core.tool_registry import connector_for_tool

    inv = await reg.execute("bi_query", {"text": "今年毛利"}, _ctx(accessible=WS2))
    clar = inv.result["clarification"]
    assert clar["tool"] == connector_for_tool("bi_query") == "datarc"
    assert clar["prompt"] == "你在北极九章有 2 个可访问的工作区，这次查询用哪个？"
