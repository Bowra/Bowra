"""workflow-solidify S2 — the conversational ``workflow_creator`` TRIGGER.

The Lead calls this when a member is **satisfied with a workflow that just ran and wants to
solidify / reuse it**. It does NOT distill or create anything itself: it emits a
``cca.workflow_solidify_open`` SSE event (mirroring ``cca.subagent_creator_open`` /
``cca.tool_create_open``) so the FE opens the inline 固化 card. The card then calls
``POST /api/v1/workflow-solidify/distill`` (S1) on the target turn and walks the user
through review → save (SKILL.md draft or team subagent).

Registration is flag-gated (``BOWHEAD_CCA_WORKFLOW_CREATOR`` — the SAME single switch that
mounts the distill route, default OFF) and advertised only to the Lead role. This module is
the canonical home of ``workflow_creator_enabled()`` (route mount, trigger register, and
future store-wire / chip all gate on it — one flag, no drift).

Payload contract: ``{turn_id, hint}``. ``turn_id`` is the **distill target** — ``None`` on
the conversational entry (the FE resolves which past turn from the conversation), passed
through verbatim on the S4 suggestions-chip entry. ``hint`` (≤200) is a one-line reuse
intent prefill, never structured state.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Final

from bowhead_router.types import ToolSchema

from bowhead_core.events import make_event
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

TOOL_NAME = "workflow_creator"
_EVENT: Final = "cca.workflow_solidify_open"  # Final → type checker infers the Literal (valid EventType).
_FLAG = "BOWHEAD_CCA_WORKFLOW_CREATOR"
_MAX_HINT = 200  # hint only prefills the card; bound before the SSE stream.


def workflow_creator_enabled() -> bool:
    """Single rollout switch for the固化工作流 + 团队 skill 治理全家（trigger + distill/dry-run
    route + team-skills 路由 + Lead 广告段团队腿 + FE 团队组/审批区 as one unit, no drift）.

    默认 **ON**（skill-team-governance 收官,用户拍板默认开）；``BOWHEAD_CCA_WORKFLOW_CREATOR=0``
    显式关 = kill-switch（存量 pending/approved 惰化,开回原样恢复）。⚠部署前置:固化 distill
    路依赖反向代理 ``proxy_read_timeout≥120s``（LLM 慢调用,见 deploy.md），翻不翻默认都需满足。"""
    return os.environ.get(_FLAG, "1").strip().lower() not in {"0", "false", "no", "off"}


_ENTRY_FLAG = "BOWHEAD_WORKFLOW_SOLIDIFY_ENTRY"


def workflow_solidify_entry_enabled() -> bool:
    """「固化此流程」**入口**的窄开关。默认 **OFF**（2026-09-01，用户拍板先隐藏）。

    只管两个入口：Lead 的 ``workflow_creator`` 触发工具、以及 FE 聊天面的
    「固化此流程」chip（单轮与整段共用 `capabilities.workflow_solidify`）。

    ⚠ **为什么不直接关 `BOWHEAD_CCA_WORKFLOW_CREATOR`**：那是**一整族**的总开关
    （固化 + 团队 skill 治理：team-skills 路由 / Lead 广告段团队腿 / FE 团队组与
    审批区 / `/settings/workflows` 管理页）。关它会连带干掉团队 skill 治理，
    而本次只要求隐藏固化。已固化出来的东西仍要能在 `/settings/workflows` 管理，
    所以那一页**不动**。

    ⇒ 生效条件 = 族开关 ON **且** 本开关 ON。恢复 = ``BOWHEAD_WORKFLOW_SOLIDIFY_ENTRY=1``。
    """
    return os.environ.get(_ENTRY_FLAG, "0").strip().lower() in {"1", "true", "yes", "on"}


def _schema() -> ToolSchema:
    return ToolSchema(
        name=TOOL_NAME,
        description=(
            "打开「固化工作流」卡片：当用户**对刚刚跑完的某一轮分析流程表示满意、想把它固化/保存/以后"
            "复用**时调用本工具，把那一轮的执行轨迹蒸馏成可复用 recipe（SKILL.md 草稿或专属子代理）。"
            "**触发信号（三者俱备最该用本工具）**：① 指代过去某一轮（『刚才那个 / 上一个分析 / 这个流程』）"
            "+ ② 满意或复用语气（『挺好的 / 就这样 / 以后都这么做 / 固化下来 / 存成模板 / 下次还这样』）"
            "+ ③ 当前会话确有已完成的轮次。"
            "⚠️ 区分三条路：用户从零描述要一个**新 skill / SOP 文档**（**没有**指代过去一轮）→ 走 skill 创建，"
            "不是本工具；用户要一个**专属 agent / 子代理**（带工具束+人设、可被路由点名）→ 走 subagent_creator，"
            "不是本工具；仅当意图是『把**已经发生过**的好流程固化下来复用』才用本工具。歧义时（既像固化又像"
            "新建）优先本工具。本工具只打开卡片，不直接创建——蒸馏与确认在卡片里完成。"
            "若用户说了一句复用意图，把它放进 `purpose` 预填。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "purpose": {
                    "type": "string",
                    "description": "可选：用户表达的复用意图一句话（如『以后核渠道毛利都这么做』），用于预填卡片；没有就留空。",
                },
            },
            "required": [],
        },
    )


async def _handler(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    raw = args.get("purpose")
    hint = raw.strip()[:_MAX_HINT] if isinstance(raw, str) and raw.strip() else None
    bus = getattr(ctx, "parent_bus", None)
    turn_id = getattr(ctx, "turn_id", "") or ""
    if bus is None:
        return {"ok": False, "opened": False, "error": "无法打开固化卡片（当前会话不支持事件）。"}
    try:
        # payload.turn_id = target turn: conversation entry uses None (frontend resolves which turn to solidify); chip entry passes through.
        # hint reuses intent in one sentence only, never contains structured state.
        await bus.emit(make_event(_EVENT, turn_id, {"turn_id": None, "hint": hint}))
    except Exception as e:
        logger.warning("workflow_creator.emit_failed turn=%s err=%s", turn_id, e)
        return {"ok": False, "opened": False, "error": "打开固化卡片失败。"}
    return {
        "ok": True,
        "opened": True,
        "message": (
            "已打开「固化工作流」卡片。卡片会把你刚才满意的那一轮蒸馏成可复用 recipe（草稿），你确认触发词与步骤后即可保存，下次同类问题直接复用。"
        ),
    }


def register_workflow_creator_trigger(registry: ToolRegistry) -> bool:
    """Flag-gated registration of the ``workflow_creator`` TRIGGER (Lead-only).

    Returns True if registered (flag ON), False if skipped (flag OFF)."""
    if not workflow_creator_enabled():
        return False
    if not workflow_solidify_entry_enabled():
        # Entry hidden by default: Lead lacks the tool, so conversation cannot open the solidification card.
        # Keep the distill route mounted (unused, harmless); team skill governance is unaffected.
        logger.info("workflow_creator_trigger.skipped (%s off)", _ENTRY_FLAG)
        return False
    registry.register(
        ToolSpec(
            schema=_schema(),
            handler=_handler,
            needs_approval=False,
            risk_level="low",
            allowed_for_roles=frozenset({"lead"}),
            visible_to_llm=True,
        )
    )
    logger.info("workflow_creator_trigger.registered (flag on)")
    return True


__all__ = ["TOOL_NAME", "register_workflow_creator_trigger", "workflow_creator_enabled", "workflow_solidify_entry_enabled"]
