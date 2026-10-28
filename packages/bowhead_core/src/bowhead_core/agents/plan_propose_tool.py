"""`plan_propose` 工具 —— Auto 模式下「这事要不要出计划」由 Lead 自己判。

★2026-08-14。此前这个判断的历史是一路把语义判断交给非语义手段：

  ①17 个关键词的子串匹配（判「用户用了什么词」而非「任务是什么结构」）——
    116 生产实测最近 60 个真实计划里 44 个(73%)触发不了，全靠用户点 UI 开关；
  ②改成「先无脑付一次 `author_plan`，拆不出多步就丢掉」—— 判据对了，但**每个
    ≥10 字的问题都要先等一次 8k-token 调用**，非项目请求白等几秒。

现在的形态：**Auto 下 Lead 自己判**。Lead 本来就要跑、本来就有工具循环、而且它比
`author_plan` 多看得到工作区与对话上下文。它认为该出计划时才调这个工具，于是
非项目请求**零额外开销**。

机制沿用 `clarification_ask`（同目录）：`tool_registry.execute` 吞异常，所以不能靠
抛错中断，而是返回**哨兵 dict**，由 lead_loop 在 run 后扫 `tool_invocations` 捕获。

两种显式模式：
  ·`planning_mode == "planning"` —— 用户已经拍板，走 loop 前的直通路，不经过本工具；
  ·`planning_mode == "off"` —— 本工具**仍在白名单里**（按模式裁剪 tool view 要深拷
    spec，代价不划算），模型仍可能调它；把关在 `_maybe_propose_plan` 的**出卡咽喉点**
    ——卡在哪儿创建就在哪儿拦，免得将来多一条调用路就多一个漏口。fail-closed。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bowhead_router.types import ToolSchema

from bowhead_core.tool_registry import ToolSpec

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

logger = logging.getLogger(__name__)

# Sentinel key: lead_loop scans every tool result for it.
SENTINEL_PLAN_PROPOSE = "_plan_propose_pending"


def is_plan_propose_sentinel(tool_result: Any) -> bool:
    """lead_loop 用它判「Lead 要求出计划」。True → 中止本轮, 走出卡路径。"""
    return isinstance(tool_result, dict) and tool_result.get(SENTINEL_PLAN_PROPOSE) is True


# Number of **distinct** peers dispatched in one turn that counts as multiple experts. 2 is the
# machine-readable form of the design criterion "intend to dispatch ≥2 distinct peers".
MULTI_PEER_THRESHOLD = 2

_SPAWN_TOOLS = ("subagent_spawn",)


def multi_peer_sentinel(tool_calls: Sequence[Any]) -> dict[str, Any] | None:
    """确定性兜底：这一轮要**同时**派 ≥2 个不同 peer 吗？是 → 返回与工具同款的哨兵。

    ★为什么要这条兜底（116 三轮 dogfood 的结论）：`plan_propose` 这个工具
    **模型就是不调**。分层证伪三次，前两层每次都过——指令在 prompt 里（Lead 能
    逐字复述）、判断也对（问它「该不该调」它答「该」并正确引用「≥2 个不同专家」），
    但真跑就是不调；跨两个模型（deepseek-v4-flash / qwen3.6-flash）、三种 prompt
    位置（不在 / 决策树旁 / 树内 Step 0）全部复现，连用户当场打字「请立刻调用
    plan_propose」都不调。LLM-first 这条路试到头了，按既有纪律转 backend 兜底。

    判据用的是**服务端可观测量**而不是又一张关键词表：模型自己列出的这一轮
    tool_calls 里有几个不同的 `subagent_spawn` profile。真机实证 turn
    `t_182294fe3b5d` 的 `peer_dispatched` 恰好是 3 个不同 profile
    （financial / business / compliance-analyst）——正是该出计划却没出的那次。

    ⚠ **只拦「一轮批量派」这一种形状**：此刻一个活都还没跑，中止零副作用。
    模型也会**串行**派（真机见过「Handed off from …」），但那时第一个 peer 已经跑完，
    再弹卡就违反工具描述里那条「已经跑了一半再弹卡，用户既等了又要再批一次」——
    那一支先不拦（留埋点观测占比），不要为了覆盖率去做用户体验更差的事。
    """
    profiles: list[str] = []
    for tc in tool_calls:
        if getattr(tc, "name", "") not in _SPAWN_TOOLS:
            continue
        args = getattr(tc, "arguments", None) or {}
        prof = str(args.get("profile") or "").strip().lower() if isinstance(args, dict) else ""
        if prof and prof not in profiles:
            profiles.append(prof)
    if len(profiles) < MULTI_PEER_THRESHOLD:
        return None
    logger.info("plan_propose.multi_peer_gate profiles=%s n=%d", profiles, len(profiles))
    return {
        SENTINEL_PLAN_PROPOSE: True,
        "reason": f"本轮要并行派 {len(profiles)} 个不同专家（{'、'.join(profiles)}），按多专家判据先出计划",
        "source": "multi_peer_gate",
    }


def _plan_propose_schema() -> ToolSchema:
    return ToolSchema(
        name="plan_propose",
        description=(
            "把当前请求变成一份**可编辑的执行计划**交给用户过目，批准后才执行。\n"
            "\n"
            "什么时候用\n"
            "这个请求需要**多个互相依赖的专家步骤**才能完成时。判据是任务的**结构**，"
            "不是用户的措辞 —— 用户不会说「请出计划」，他会直接描述一件多步的事。\n"
            "- 需要先查 A、再查 B，然后**基于两者**得出结论\n"
            "- 需要多个不同领域的专家分别产出，再汇总成一份材料\n"
            "- 用户明确列了多个子任务（①②③ / (1)(2)(3) / 分三路），且它们**各自都是**"
            "  一个真正的分析步，不是「查一下然后总结一句」\n"
            "\n"
            "什么时候不用\n"
            "- 一次查询能答的（哪怕问句很长、哪怕用户写了「全面」「整体」这类词）\n"
            "- 拆出来只有一步 —— 那不是计划，直接答\n"
            "- 「查个数然后一句话总结」：总结不是独立的专家步，这是一步\n"
            "\n"
            "⚠ **要用就在派任何活之前用**。计划卡的意义是让用户先看后批；"
            "已经跑了一半再弹卡，用户既等了又要再批一次。\n"
            "\n"
            "调用后本轮立即结束并把计划卡交给用户，你不需要再写任何回答。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "一句话说明为什么这个请求需要多步计划（进日志，便于事后校准，不展示给用户）。",
                },
            },
            "required": ["reason"],
        },
    )


async def _plan_propose(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    reason = str(args.get("reason") or "")[:300]
    logger.info("plan_propose.requested turn=%s reason=%s", getattr(ctx, "turn_id", None), reason)
    return {SENTINEL_PLAN_PROPOSE: True, "reason": reason}


def register_plan_propose_tool(registry: ToolRegistry) -> None:
    """注册 plan_propose。Lead manifest 的 tools.allow 里要有它才可见。"""
    registry.register(ToolSpec(schema=_plan_propose_schema(), handler=_plan_propose))
