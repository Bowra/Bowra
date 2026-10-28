"""Runtime gate for tools that require admin approval."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bowhead_core.events import OrchestratorEvent
from bowhead_core.types import ToolInvocation, utc_now_iso

if TYPE_CHECKING:
    from bowhead_core.approval_store import ApprovalStore
    from bowhead_core.notification_service import NotificationService
    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

logger = logging.getLogger(__name__)

# Native write tools whose operations approval_executor can actually execute.
# Empty since 2026-07-21: all four former BI write tools (bi_create/update/delete_measures +
# bi_update_dimensions) were removed; datarc client has no write methods, so even approval only reached
# an always-raising stub (M3 never implemented). The 116 production approvals table had 0 rows.
# When executable write tools are implemented, add their names here; advertising follows automatically,
# requiring no advertising-side code changes (see approval_executable's docstring).
SUPPORTED_NATIVE_WRITE_TOOLS: frozenset[str] = frozenset()


def approval_write_enabled() -> bool:
    """本部署是否启用写操作审批。**每次读 env**，不在 import 期定型。

    ⚠ 生效方式：改 `.env` 后需要重建 app 容器。`psi_flags` 的 docstring 说
    「/admin/flags POST 改完不必重启」——**那个端点并不存在**（`chi_flags` 写的是
    "a future /admin/flags endpoint"，更诚实）。每次读 env 的真实收益是不在
    import 期定型：同进程内谁改了 os.environ 就立刻生效（测试、将来真有端点）。
    对运维而言，下线 = 改配置 + 重启容器（分钟级、不改代码不发版），不是秒级。

    默认 ON：它是**紧急下线开关**，不是启用开关。默认 OFF 会制造「实装了却忘了
    翻开关」那一类静默失效。真正决定能不能用的是 `approval_executable`。
    """
    return os.environ.get("BOWHEAD_APPROVAL_WRITE_ENABLED", "1") == "1"


def approval_executable(tool_name: str) -> bool:
    """这个工具**今天**能不能真的走完「入队 → 批准 → 执行」。

    ★ 广告面与本模块的闸共用的**唯一**判据（advertise-execute-gap 判据 4：
    同一判据只允许一份实现，广告侧调执行侧那一份）。消费者：
      - `capability_resolver.advertised_tool_schemas`（模型可见的工具 schema）
      - `lead_orchestrator._gather_team_tool_entries`（`<team_tools>` 提示词块）
      - 本模块 `ApprovalGate.maybe_gate`（执行准入）

    广告面每轮现算、不缓存，于是三个方向都不用改广告侧代码：
      - 工具被纳入 `SUPPORTED_NATIVE_WRITE_TOOLS` ⇒ 下一轮回到模型面前
      - `BOWHEAD_APPROVAL_WRITE_ENABLED=0` ⇒ 下一轮全部消失
      - 团队工具接通（executor 拿得到 per-turn overlay）⇒ 把这里的名单判据换成
        executor registry 的 `has()`，广告面仍然零改动

    今天恒 False（集合为空）——那是**当前可用性**，不是永久事实。
    """
    if not approval_write_enabled():
        return False
    return tool_name in SUPPORTED_NATIVE_WRITE_TOOLS


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:16]


def _request_key(
    *,
    turn_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    tool_call_id: str | None,
) -> str:
    if tool_call_id:
        return f"tool:{turn_id}:{tool_call_id}:{tool_name}"
    return f"tool:{turn_id}:{tool_name}:{_digest(arguments)}"


@dataclass
class ApprovalGate:
    approval_store: ApprovalStore
    notification_service: NotificationService | None = None
    enabled: bool = True

    async def maybe_gate(
        self,
        *,
        registry: ToolRegistry,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext,
        tool_call_id: str | None = None,
        bus: Any | None = None,
    ) -> ToolInvocation | None:
        if not self.enabled or not registry.has(tool_name):
            return None
        spec = registry.get(tool_name)
        if not spec.needs_approval:
            return None

        started_at = utc_now_iso()
        ended_at = utc_now_iso()
        # Same predicate as advertising (approval_executable). Normally the model never sees this tool;
        # reaching here means a direct name call (cached schema / hardcoded custom-subagent allowlist). This gate is the fallback.
        if not approval_executable(tool_name):
            logger.warning("approval_gate.unsupported_write_tool tool=%s", tool_name)
            return ToolInvocation(
                tool_name=tool_name,
                arguments={},
                result={
                    "ok": False,
                    "status": "approval_unsupported",
                    # Model-facing text (not user-facing; no i18n needed). Old wording was
                    # "not supported ... yet"; "yet" suggests a retry might work,
                    # but this is **configuration state**, not a transient failure. Follow 7afec446a P1-4's shape:
                    # state configuration, forbid retries, and do not expose internal identifiers.
                    "message": (
                        "Write-operation approval is not enabled on this deployment, "
                        "so this tool cannot run. This is a configuration state, not a "
                        "transient failure: do not retry, do not try different arguments, "
                        "and do not surface this status text to the user. "
                        "Continue with read-only tools, or tell the user this action "
                        "needs to be done outside the assistant."
                    ),
                },
                error="approval_unsupported",
                started_at=started_at,
                ended_at=ended_at,
            )

        reason = arguments.get("reason") if isinstance(arguments, dict) else None
        if not isinstance(reason, str) or not reason.strip():
            reason = "写操作需要管理员审批"
        request_key = _request_key(
            turn_id=ctx.turn_id,
            tool_name=tool_name,
            arguments=arguments,
            tool_call_id=tool_call_id,
        )
        approval, created = await self.approval_store.enqueue_once(
            request_key=request_key,
            turn_id=ctx.turn_id,
            session_id=ctx.session_id or None,
            workspace_id=ctx.workspace_id,
            member_uid=ctx.member_uid or "",
            user_id=ctx.user_id,
            tool_name=tool_name,
            arguments=arguments,
            reason=reason,
            risk_level=spec.risk_level or "medium",
            # Batch 2/c9 (PRD §3.9): request-time Datarc workspace snapshot, pinned for replay
            datarc_workspace_id=((_rp() or None) if callable(_rp := getattr(ctx, "resolve_datarc_project", None)) else None),
        )
        if created and self.notification_service is not None:
            try:
                await self.notification_service.emit_approval_required(approval)
            except Exception as exc:
                logger.warning(
                    "approval_gate.notification_emit_failed approval=%s err=%s",
                    approval.id,
                    exc,
                )
        if bus is not None:
            try:
                await bus.emit(
                    OrchestratorEvent(
                        event_type="approval.required",  # type: ignore[arg-type]
                        turn_id=ctx.turn_id,
                        payload={
                            "card_id": ctx.turn_id,
                            "approval_id": approval.id,
                            "tool_name": tool_name,
                            "risk_level": approval.risk_level,
                            "created": created,
                        },
                    )
                )
            except Exception as exc:
                logger.debug("approval_gate.bus_emit_failed approval=%s err=%s", approval.id, exc)

        return ToolInvocation(
            tool_name=tool_name,
            arguments={},
            result={
                "ok": True,
                "status": "approval_pending",
                "approval_id": approval.id,
                "created": created,
                "message": "Approval request created; an team admin must approve before execution.",
            },
            started_at=started_at,
            ended_at=ended_at,
        )
