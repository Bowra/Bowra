"""bi-workspace-list — tool_list_workspaces 只读名单原语契约.

switch 的读侧姊妹工具：名单来自 ctx 可及集合快照（server 注入，LLM 无法扩集合）；
只读，不改 ctx 任何字段。空可及 → 结构化拒绝（与 switch 的 no_accessible 同语义）。
"""

from __future__ import annotations

import pytest
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

WS3 = (
    {"id": "1", "name": "商超零售场景"},
    {"id": "234", "name": "北极九章DEMO"},
    {"id": "235", "name": "北极九章TEST"},
)


def _ctx(accessible=WS3, active_name="商超零售场景") -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_x",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=object(),
        active_datarc_project="1",
        active_datarc_workspace_name=active_name,
        accessible_datarc_workspaces=accessible,
    )


@pytest.fixture(scope="module")
def reg():
    return build_default_registry()


async def test_lists_full_roster_with_current(reg):
    ctx = _ctx()
    inv = await reg.execute("tool_list_workspaces", {}, ctx)
    assert inv.error is None and inv.result["ok"] is True
    assert inv.result["total"] == 3
    assert inv.result["workspaces"] == ["商超零售场景", "北极九章DEMO", "北极九章TEST"]
    assert inv.result["current"] == "商超零售场景"
    # Return routing-discipline hints with results (observed model compliance: no indiscriminate switching, switch back after inspection).
    assert "切回" in inv.result["hint"]
    assert "一次只发起一个切换" in inv.result["hint"]  # Defense against blind parallel switching across 31 workspaces observed on 116.
    # bi-answer-correctness B2: ask the user when a question seems to target another Datarc workspace; no silent model-decided switch (review c4).
    assert "clarification_ask" in inv.result["hint"] and "不要自己切换" in inv.result["hint"]
    assert "不确定就先问" not in inv.result["hint"]
    # Small list (≤ soft cap): allow inspection in one turn without pushing batches/plan.
    assert "plan_propose" not in inv.result["hint"]
    # Read-only: ctx remains unchanged.
    assert ctx.active_datarc_project == "1" and ctx.member_uid == "m"


async def test_large_roster_hint_redirects_to_batching_or_plan(reg):
    """方向①（116 真机 t_619ab802b174 撞 cap 实证）：名单超过单轮迭代预算时，
    hint 引导改计划形状——报量级+挑重点/分批/plan_propose，而不是放行全量巡检。"""
    roster = tuple({"id": str(i), "name": f"工作区{i:02d}"} for i in range(12))
    inv = await reg.execute("tool_list_workspaces", {}, _ctx(accessible=roster, active_name="工作区00"))
    hint = inv.result["hint"]
    assert "12 个" in hint and "不要在本轮逐个盘点" in hint
    assert "plan_propose" in hint and "分批" in hint
    assert "切回" in hint  # Inspection discipline remains after scope selection.


async def test_empty_accessible_refuses_structurally(reg):
    """空可及=答案本身：error_kind 使其按预期内 outcome 渲染，不出红色报错步骤。

    review c1：断言穿过真实豁免 seam（subagent._is_non_failure_result），
    不是只查字段在——那正是 feedback-0713 修 switch not_accessible 的同族坑。
    """
    from bowhead_core.subagent import _is_non_failure_result

    inv = await reg.execute("tool_list_workspaces", {}, _ctx(accessible=()))
    assert inv.result["ok"] is False
    assert inv.result["error"] == "no_accessible_workspaces"
    assert inv.result["error_kind"] == "no_accessible_workspaces"
    assert inv.result["total"] == 0 and inv.result["workspaces"] == []
    assert _is_non_failure_result(inv.result) is True
    # review r2#1 negative check: a magic kind string without structural evidence of an empty list (third-party MCP forging
    # an actual failure shape) must not be exempted; it still counts as failure.
    assert _is_non_failure_result({"ok": False, "error_kind": "no_accessible_workspaces"}) is False


async def test_hint_never_names_a_lead_denied_tool(reg):
    """review c5：hint 教 Lead 的路由（bi_schema 盘点/spawn 取数）必须与
    _LEAD_TOOL_DENY 一致——上一次漂移就是 hint 教 Lead 调它没有的 bi_query。
    两个量级变体都查（方向①后 hint 按名单大小二选一）。"""
    from bowhead_core.agents.lead_orchestrator import _LEAD_TOOL_DENY

    big = tuple({"id": str(i), "name": f"ws{i}"} for i in range(12))
    for accessible in (WS3, big):
        inv = await reg.execute("tool_list_workspaces", {}, _ctx(accessible=accessible, active_name=""))
        hint = inv.result["hint"]
        assert all(denied not in hint for denied in _LEAD_TOOL_DENY), hint


async def test_current_falls_back_to_id_lookup_when_name_missing(reg):
    """review c7：active name 与 project id 分开走 6 跳，name 漏接时按 id 回查
    名单——不许把「有工作区但名字没接到」渲成「当前未连接」。"""
    inv = await reg.execute("tool_list_workspaces", {}, _ctx(active_name=""))
    assert inv.result["current"] == "商超零售场景"  # Looked up via active_datarc_project="1".


async def test_current_stays_empty_when_active_id_not_in_roster(reg):
    """review r2#8：active id 已被从可及集合收权（desync）→ current 留空如实，
    不把一个名单里不存在的裸 id 当「当前工作区」报给模型。"""
    ctx = _ctx(active_name="")
    ctx.active_datarc_project = "99"
    inv = await reg.execute("tool_list_workspaces", {}, ctx)
    assert inv.result["current"] == ""


async def test_unsupported_binding_refused(reg):
    inv = await reg.execute("tool_list_workspaces", {"tool": "nosuch"}, _ctx())
    assert inv.result["ok"] is False and inv.result["error"] == "unsupported_tool"
