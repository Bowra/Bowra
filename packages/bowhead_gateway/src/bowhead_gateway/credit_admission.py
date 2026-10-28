"""新模型工作的 credit 准入——所有会起 turn 的入口共用这一份判据。

结算（`user_store._deduct_credit_with_stable_id_in_tx`）照实扣、允许透支到负数；准入只看
`balance <= 0`。各入口按自己的面渲染拒绝（HTTP 402 / IM 文案 / 失败子 turn），但判据与
「硬拦」留痕只有这一份。

2026-09-19 前只有 chat/send、飞书新消息、定时任务三处在拦；澄清续跑（web 与飞书卡片）、
steer 续跑、Slack、Teams、workflow 蒸馏/试跑都能在额度用完后照常调模型。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

CreditDenialCode = Literal["user_credit_exhausted", "team_pool_exhausted"]


def credit_denial_code(user: Any | None, team: Any | None) -> CreditDenialCode | None:
    """判据本体（纯函数）。user/team 任一缺失不拦——与 chat preflight 一贯的语义相同。"""

    if user is None or team is None:
        return None
    if int(getattr(user, "credit_balance", 0) or 0) <= 0:
        return "user_credit_exhausted"
    if int(getattr(team, "team_credit_balance", 0) or 0) <= 0:
        return "team_pool_exhausted"
    return None


async def record_credit_exhaustion_best_effort(
    user_store: Any,
    *,
    metrics_log: Any | None = None,
    team_id: str,
    user_id: str,
    workspace_id: str | None,
    code: str,
    turn_id: str | None = None,
    user: Any | None = None,
    team: Any | None = None,
) -> dict[str, Any] | None:
    """Record credit hard-blocks without changing the caller's success/failure semantics."""

    try:
        if user is None:
            user = await user_store.get_user(user_id)
        if team is None:
            team = await user_store.get_team(team_id)
        event = await user_store.record_credit_exhaustion_event(
            team_id=team_id,
            user_id=user_id,
            workspace_id=workspace_id,
            code=code,
            credit_balance=getattr(user, "credit_balance", None),
            credit_quota=getattr(user, "credit_quota", None),
            team_credit_balance=getattr(team, "team_credit_balance", None),
            team_credit_quota=getattr(team, "team_credit_quota", None),
            turn_id=turn_id,
        )
        if metrics_log is not None:
            await metrics_log.track(
                "credit.exhausted_block",
                user_id=user_id,
                attrs={
                    "team_id": team_id,
                    "workspace_id": workspace_id,
                    "code": code,
                    "credit_balance": getattr(user, "credit_balance", None),
                    "credit_quota": getattr(user, "credit_quota", None),
                    "team_credit_balance": getattr(team, "team_credit_balance", None),
                    "team_credit_quota": getattr(team, "team_credit_quota", None),
                    "turn_id": turn_id,
                    "credit_exhaustion_event_id": event.get("id"),
                },
            )
        return event
    except Exception as e:
        logging.getLogger("bowhead_gateway.chat").warning(
            "credit.exhaustion_event_record_failed turn=%s code=%s err=%s",
            turn_id,
            code,
            str(e)[:200],
        )
        return None


async def check_credit_admission(
    user_store: Any | None,
    *,
    team_id: str | None,
    user_id: str,
    workspace_id: str | None,
    metrics_log: Any | None,
    turn_id: str | None = None,
) -> CreditDenialCode | None:
    """读余额 → 判 → 拦下时留痕。放行返回 None。

    无 user_store / 无 team（stub 调用方）不拦。查库异常照常上抛，由入口决定 fail-closed
    （HTTP 入口）还是 fail-open（后台）。
    """

    if user_store is None or not team_id:
        return None
    user = await user_store.get_user(user_id)
    team = await user_store.get_team(team_id) if user else None
    code = credit_denial_code(user, team)
    if code is not None:
        await record_credit_exhaustion_best_effort(
            user_store,
            metrics_log=metrics_log,
            team_id=team_id,
            user_id=user_id,
            workspace_id=workspace_id,
            code=code,
            turn_id=turn_id,
            user=user,
            team=team,
        )
    return code
