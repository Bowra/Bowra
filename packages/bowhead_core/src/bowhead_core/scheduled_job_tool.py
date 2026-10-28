"""scheduled_job_create — conversational routine creation (Lead tool, flag-gated, default OFF).

The Lead calls this when the user clearly expresses a recurring schedule ("每天早上跑…"). It creates a
scheduled job via the existing engine and returns a human-readable schedule the Lead MUST echo back for
the user to confirm. Create-only; managing routines is the settings UI. Reuses the same cron validation
as the create route. JTBD/design: docs/routines-conversational-tool-jtbd-2026-06-22.md.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC
from typing import Any

from bowhead_router.types import ToolSchema

from bowhead_core.scheduler_loop import MANUAL_CRON, cron_validation_error, next_run_for_job
from bowhead_core.tool_registry import ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

_FLAG = "BOWHEAD_SCHEDULED_JOB_TOOL"
_MAX_EMAILS = 5
_MAX_QUERY_CHARS = 2000

# python weekday order (Mon..Sun) — cron dow W maps to (W-1)%7
_WEEKDAYS_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
# Used when the user names no zone and the turn has no viewer zone (IM, scheduled, older clients).
_FALLBACK_TZ = "Asia/Shanghai"


def scheduled_job_tool_enabled() -> bool:
    return os.environ.get(_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def _human_schedule(cron: str, tz: str) -> str:
    """cron → 中文人话（与前端 cronToHuman 对齐），用于回显确认。"""
    c = cron.strip()
    if c == MANUAL_CRON:
        return "手动（仅手动触发）"
    parts = c.split()
    if len(parts) != 5:
        return c
    minute, hour, _dom, _mon, dow = parts
    if minute == "*" and hour == "*":
        return "每分钟"
    if minute.startswith("*/") and hour == "*":
        return f"每 {minute[2:]} 分钟"
    if minute.isdigit() and hour == "*":
        return "每小时" if int(minute) == 0 else f"每小时第 {int(minute)} 分"
    if not (hour.isdigit() and minute.isdigit()):
        return c
    hhmm = f"{int(hour):02d}:{int(minute):02d}"
    if dow == "*":
        return f"每天 {hhmm}"
    if dow == "1-5":
        return f"工作日 {hhmm}"
    if dow.isdigit():
        return f"每{_WEEKDAYS_CN[(int(dow) - 1) % 7]} {hhmm}"
    return f"{hhmm}（{dow}）"


async def _handler(args: dict[str, Any], ctx: Any) -> Any:
    store = getattr(ctx, "scheduled_store", None)
    if store is None:
        return {"ok": False, "error": "scheduled_store not configured on context"}
    uid = getattr(ctx, "user_id", None) or ""
    ws = getattr(ctx, "workspace_id", None) or ""
    if not uid or not ws:
        return {"ok": False, "error": "no user_id / workspace_id in context"}

    name = str(args.get("name") or "").strip()
    cron = str(args.get("cron") or "").strip()
    query = str(args.get("query") or "").strip()
    if not (name and cron and query):
        return {"ok": False, "error": "name, cron, query are all required"}
    if len(name) > 60:
        return {"ok": False, "error": "name too long (max 60)"}
    if len(query) > _MAX_QUERY_CHARS:
        return {"ok": False, "error": f"query too long (max {_MAX_QUERY_CHARS} chars)"}
    cron_err = cron_validation_error(cron)
    if cron_err:
        return {"ok": False, "error": cron_err, "hint": "改用预设级别：每小时/每天/每周/工作日，或 @manual（最短间隔 1 小时）"}

    # Unspecified zone: the zone of the viewer who asked in this turn (admitted with the turn),
    # else the long-standing server default. An explicit zone from the user always wins.
    from bowhead_core.model_clock import request_time_zone_name, turn_time_zone_name

    tz = str(args.get("tz") or request_time_zone_name(ctx) or turn_time_zone_name() or _FALLBACK_TZ)
    strategy = str(args.get("session_strategy") or "fresh")
    # Basic format sanity only; cross-team recipient allowlisting is a pre-enable requirement (see
    # JTBD doc) — the summary is delivered to these addresses, so don't ship external delivery broadly
    # without verifying recipients against team membership.
    emails = [e for e in (str(x).strip() for x in (args.get("notify_emails") or [])) if "@" in e and "." in e.rsplit("@", 1)[-1]][:_MAX_EMAILS]
    notify = {"email": emails, "deliver_on_success": True} if emails else {}

    # scheduled-recipe-runs S1: bind solidified workflows. turn_runner injects the three stores into ctx;
    # validate_and_bind owns validation/flags/bindability (routes and copilot drafts share this entry point).
    # ⚠ **Never accept model-invented names**: ref must have {form, id_or_name} shape; unresolved refs
    # reject the entire task creation, since a wrong workflow applies someone else's rules unattended under the owner's identity.
    recipe_ref_json: str | None = None
    agent_binding = None
    raw_ref = args.get("recipe_ref")
    if raw_ref:
        from bowhead_core.workflow_binding import WorkflowBindingError, validate_and_bind

        try:
            recipe_ref_json = await validate_and_bind(
                raw_ref,
                user_id=uid,
                team_id=getattr(ctx, "team_id", None),
                skill_instance_store=getattr(ctx, "skill_instance_store", None),
                team_skill_store=getattr(ctx, "team_skill_store", None),
                team_subagent_store=getattr(ctx, "team_subagent_store", None),
            )
        except WorkflowBindingError as exc:
            return {
                "ok": False,
                "error": f"绑定的固化流程不可用：{exc}",
                "hint": "让用户在定时任务设置页里选择流程，或先确认流程名/可见性",
            }
        from bowhead_core.workflow_binding import FORM_TEAM_SUBAGENT, parse_ref

        parsed_ref = parse_ref(recipe_ref_json)
        if parsed_ref is not None and parsed_ref["form"] == FORM_TEAM_SUBAGENT:
            team_id = str(getattr(ctx, "team_id", "") or "")
            db_path = str(getattr(store, "db_path", "") or "")
            if not team_id or not db_path:
                return {
                    "ok": False,
                    "error": "绑定的 Agent 无法固定版本，请在定时任务设置页重新选择",
                }
            try:
                from bowhead_core.scheduled_agent_binding import (
                    ScheduledAgentBindingStore,
                )

                agent_binding = await ScheduledAgentBindingStore(
                    db_path,
                ).prepare_current_team_binding(
                    team_id=team_id,
                    user_id=uid,
                    agent_id=parsed_ref["id_or_name"],
                    confirmed_by=uid,
                )
            except Exception as exc:
                logger.warning(
                    "scheduled_job_create.agent_binding_failed agent=%s err=%s",
                    parsed_ref["id_or_name"],
                    exc,
                )
                return {
                    "ok": False,
                    "error": "绑定的 Agent 当前不可用或版本证据不完整，请在设置页重新选择",
                }

    try:
        job = await store.create(
            user_id=uid,
            workspace_id=ws,
            name=name,
            cron=cron,
            query=query,
            tz=tz,
            session_strategy=strategy,
            notify_channels=notify,
            recipe_ref=recipe_ref_json,
            agent_binding=agent_binding,
        )
    except Exception as exc:
        logger.warning("scheduled_job_create.failed err=%s", exc)
        return {"ok": False, "error": f"create failed: {exc}"}

    next_iso = None
    try:
        from datetime import datetime

        nxt = next_run_for_job(cron, tz, datetime.now(tz=UTC))
        if nxt is not None:
            await store.set_next_run_at(job.id, nxt.isoformat())
            next_iso = nxt.isoformat()
    except Exception as exc:
        logger.warning("scheduled_job_create.next_run_failed job=%s err=%s", job.id, exc)

    human = _human_schedule(cron, tz)
    return {
        "ok": True,
        "job_id": job.id,
        "name": name,
        "human_schedule": human,
        "next_run_at": next_iso,
        "bound_workflow": bool(recipe_ref_json),
        "message": (
            f"已创建定时任务「{name}」，{human}（{tz}）" + ("，并绑定了指定的固化流程" if recipe_ref_json else "") + "。请把这个时间读给用户确认。"
        ),
    }


def register_scheduled_job_tool(tool_registry: ToolRegistry) -> bool:
    """Register scheduled_job_create (flag-gated, default OFF). Returns True if registered."""
    if not scheduled_job_tool_enabled():
        return False
    tool_registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="scheduled_job_create",
                description=(
                    "把用户的分析需求保存为一个【定时任务/routine】，到点在云端自动跑并把结果送达。\n"
                    "仅在用户【明确表达周期 + 时间】时调用（如“每天早上9点跑昨日GMV发我邮箱”）；"
                    "含糊（“以后还要看”）不要调，先 clarification_ask 问清时间/范围/邮箱。\n"
                    "cron 由你从自然语言生成：每小时=`0 * * * *`；每天HH:MM=`MM HH * * *`；"
                    "工作日HH:MM=`MM HH * * 1-5`；每周W HH:MM=`MM HH * * W`(周日=0..周六=6)；手动=`@manual`。"
                    "最短间隔 1 小时（sub-hourly 会被拒）。\n"
                    "返回 {ok, job_id, human_schedule, next_run_at, message}；失败 ok=false+error。\n"
                    "成功后【必须】把 human_schedule + 下次运行时间读给用户确认，不要默默建完。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "任务名（≤60 字）"},
                        "query": {"type": "string", "description": "要定时执行的自然语言分析查询"},
                        "cron": {"type": "string", "description": "CRON 表达式或 @manual（见上映射）"},
                        "tz": {
                            "type": "string",
                            "description": "IANA 时区；仅在用户明确指定时填写，省略则使用本轮提问用户的浏览器时区（取不到时 Asia/Shanghai）",
                        },
                        "session_strategy": {
                            "type": "string",
                            "enum": ["fresh", "anchor"],
                            "description": "fresh=每次独立报告(默认)；anchor=续同一会话",
                        },
                        "notify_emails": {"type": "array", "items": {"type": "string"}, "description": "跑完投递摘要+链接的邮箱（可选，≤5）"},
                        "recipe_ref": {
                            "type": "object",
                            "description": (
                                "绑定一份【已固化的流程】，到点按它的步骤执行（可选）。"
                                "只有当用户明确指名要用某个已存在的流程时才填，"
                                "且必须是你在本轮里真实看到过的流程（如 skill_list 的结果）——"
                                "**不要凭印象编名字**；不确定就不填，让用户去设置页里选。"
                            ),
                            "properties": {
                                "form": {"type": "string", "enum": ["personal_skill", "team_skill", "team_subagent"]},
                                "id_or_name": {"type": "string"},
                            },
                            "required": ["form", "id_or_name"],
                        },
                    },
                    "required": ["name", "query", "cron"],
                },
            ),
            handler=_handler,
            risk_level="low",
            needs_approval=False,
            allowed_for_roles=frozenset({"lead"}),
        )
    )
    logger.info("scheduled_job_create.registered (flag on)")
    return True
