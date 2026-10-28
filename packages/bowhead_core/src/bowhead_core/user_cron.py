"""Maintain daily usage aggregates and catch up monthly credit resets.

Account retirement and erasure run exclusively through the durable account
service; this scheduler never guesses deletion deadlines from activity time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite

from bowhead_core.user_store import _id

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


# ─── LLM usage rollup + retention-pruning cron ─────────────────────

DEFAULT_LLM_USAGE_RETENTION_DAYS = 90


def llm_usage_retention_days() -> int:
    """`BOWHEAD_LLM_USAGE_RETENTION_DAYS` —— 原始逐次用量行的保留天数（默认 90）。

    设 0 或负数 = 不裁剪（只汇总）。日级汇总永久保留, 不受此值影响。
    """
    raw = os.environ.get("BOWHEAD_LLM_USAGE_RETENTION_DAYS", "").strip()
    if not raw:
        return DEFAULT_LLM_USAGE_RETENTION_DAYS
    try:
        return int(raw)
    except ValueError:
        logger.warning("cron.bad_llm_usage_retention value=%r -> default %d", raw, DEFAULT_LLM_USAGE_RETENTION_DAYS)
        return DEFAULT_LLM_USAGE_RETENTION_DAYS


async def rollup_and_prune_llm_usage(db_path: str, *, retention_days: int | None = None) -> dict[str, int]:
    """先把原始用量汇总进日级表, 再裁掉超过保留窗口的原始行。

    **顺序不可颠倒**: 汇总在前、裁剪在后, 且 `prune_llm_usage_events` 自身还会逐天核对
    「该天已进汇总」才删 —— 双重保险, 因为顺序错会造成不可逆的数据丢失。

    为什么要裁: `llm_usage_events` 逐次调用一行, 实测每行 368 bytes(含索引)。300 人常规用量
    (每人每工作日约 10 turn × 每 turn 约 15 次调用) ≈ 137 万行/月 = 5.6 GiB/年; 整库单文件
    SQLite 且备份按整文件复制, 留 3 份即 ×4 占盘。日级汇总实测压缩比 75:1(300 人一年约
    77 MiB)可永久保留, 所以裁原始行**不损失分析能力**。

    开销: 汇总是对**保留窗口内**的原始行做一次 GROUP BY —— 稳态下窗口固定(默认 90 天),
    所以成本有界而非随时间增长。实测 300 人 × 90 天(约 405 万行)汇总约 3s、裁剪约 4s,
    每日 04:00 跑一次, 无需改成增量窗口。
    """
    from bowhead_core.user_store import UserStore

    store = UserStore(db_path)
    days = llm_usage_retention_days() if retention_days is None else retention_days
    # Full rollup (idempotent: ON CONFLICT overwrites the whole interval, never accumulates; reruns agree).
    rolled = await store.rollup_llm_usage_daily()
    pruned = 0
    if days > 0:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        pruned = await store.prune_llm_usage_events(older_than=cutoff)
    if rolled or pruned:
        logger.info("llm_usage.rollup_and_prune rolled=%d pruned=%d retention_days=%d", rolled, pruned, days)
    return {"rolled": rolled, "pruned": pruned, "retention_days": days}


# ─── monthly credit reset cron ─────────────────────────


def _utc_month_key(ts: str | None) -> str | None:
    """取 ISO 时间串的 UTC 年月 `YYYY-MM`; 空值或解析不了返回 None。

    库里的 `*_period_start` 由 `datetime.now(UTC).isoformat()` 写入(带 `+00:00`), 但历史行
    可能是 `Z` 结尾或不带时区, 所以这里容错解析而不是直接切字符串: 判「这个月刷没刷」不能
    因为一个格式差异就误判成没刷(会重复发额度)或刷过(会整月不发)。
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m")


def next_monthly_credit_reset(now: datetime | None = None) -> datetime:
    """Return the next scheduled monthly credit reset boundary: the start of the next UTC month.

    ``run_cron_loop`` resets every active team whose last reset happened in an earlier UTC
    month, so the period always ends at the next UTC month boundary (the hourly tick applies it
    within an hour). User-facing surfaces read the reset date from here instead of re-deriving
    the schedule.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if current.month == 12:
        return datetime(current.year + 1, 1, 1, tzinfo=UTC)
    return datetime(current.year, current.month + 1, 1, tzinfo=UTC)


async def teams_due_for_monthly_reset(db_path: str, now: datetime | None = None) -> list[str]:
    """返回本月还欠一次月度重置的 active team id。

    判据放在**库里**(`teams.team_credit_period_start`)而不是进程内存: 只要某个 team 上次重置
    发生在更早的月份(或从来没重置过), 它就是 due。因此任意一次 tick 都能补上, 进程重启、
    部署重建、宕机都不会让某个月的额度永久丢失。

    用 `<` 而不是 `!=` 比较年月: 时钟回拨或写入了未来时间戳时, 宁可不重复发额度。
    """
    now = now or datetime.now(UTC)
    current = now.astimezone(UTC).strftime("%Y-%m")
    due: list[str] = []
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, team_credit_period_start FROM teams WHERE status = 'active'",
        ) as cur:
            rows = await cur.fetchall()
    for r in rows:
        key = _utc_month_key(r["team_credit_period_start"])
        # key is None = never reset (or invalid timestamp) → due, ensuring the team receives quota at least once.
        if key is None or key < current:
            due.append(r["id"])
    return due


async def monthly_credit_reset(db_path: str, *, team_ids: Sequence[str] | None = None) -> dict[str, int]:
    """Reset all team + user credit_balance to quota.

    `team_ids=None` 重置全部 active team(既有调用方语义不变); 传了就只重置这些 team ——
    cron 用它只重置真正 due 的 team, 避免把本月已经重置过的 team 的用量一并抹掉。

    User credit_quota 从 team.tier_credits_json[user.tier] 重算 (admin 改了 tier_credits 也跟).

    **安全约束**: `tier_credits` 缺 `user.tier` 键时**保留该用户现有 quota**, 绝不写 0 ——
    与 `UserStore.admin_period_reset` 的 `balance_and_quota` 分支同口径 (那里用的是
    `tier_credits.get(tier, before["quota"])`)。曾用的 `get(tier, 0)` 会在新增/改名 tier
    档位、或 `tier_credits_json` 变成 `{}` (如 license 漏签 tier_credits) 时,于月初把**全员**
    quota+balance 一起归零 → 此后每轮 chat 撞 402。缺键是配置异常, 不是「该用户额度为零」。

    `tier_key_missing` 计缺键用户数并额外打 WARNING —— 它是需要人工修 `tier_credits_json`
    的配置错误。`quota_changed` 计「写入值 != 原值」的用户数, 让本函数改动 quota 这件事不再
    完全静默 (设计要求 quota 覆盖必须可见, 见 `docs/credit-usage-activation-model.md` §6)。
    ⚠ 该计数**区分不了**「admin 定制的 quota 被按 tier 重写」与「admin 改了 tier 默认额度后
    正常传播」——两者在数据上都只是 `旧值 != 新值`。要真正满足设计里「定制必须存活」的要求,
    需要显式的 `credit_quota_custom` 标记列; 靠值比较反推会在 admin **下调** tier 默认额度时
    把所有仍持旧默认值的用户误判成定制而免于下调, 方向正好相反。
    """
    now = datetime.now(UTC).isoformat()
    teams_reset = 0
    users_reset = 0
    tier_key_missing = 0
    quota_changed = 0
    ledger_rows = 0
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        # All active teams (if team_ids is supplied, only those listed).
        if team_ids is None:
            async with db.execute(
                "SELECT id, team_credit_quota, tier_credits_json FROM teams WHERE status = 'active'",
            ) as cur:
                teams = await cur.fetchall()
        elif not team_ids:
            teams = []
        else:
            placeholders = ",".join("?" * len(team_ids))
            async with db.execute(
                f"SELECT id, team_credit_quota, tier_credits_json FROM teams WHERE status = 'active' AND id IN ({placeholders})",
                tuple(team_ids),
            ) as cur:
                teams = await cur.fetchall()
        for team in teams:
            await db.execute(
                "UPDATE teams SET team_credit_balance = ?, team_credit_period_start = ? WHERE id = ?",
                (int(team["team_credit_quota"]), now, team["id"]),
            )
            teams_reset += 1
            tier_credits = json.loads(team["tier_credits_json"] or '{"pro":5000,"ultra":30000}')
            # All active users in this team.
            async with db.execute(
                "SELECT id, tier, credit_quota, credit_balance FROM users WHERE team_id = ? AND status = 'active'",
                (team["id"],),
            ) as ucur:
                users = await ucur.fetchall()
            for u in users:
                current_quota = int(u["credit_quota"] or 0)
                if u["tier"] in tier_credits:
                    quota = int(tier_credits[u["tier"]])
                else:
                    # Invalid config → preserve current state (fail safe), warn for manual tier_credits_json correction.
                    quota = current_quota
                    tier_key_missing += 1
                    logger.warning(
                        "monthly_credit_reset.tier_key_missing team=%s user=%s tier=%r tier_credits_keys=%s -> keeping quota=%d (NOT zeroing)",
                        team["id"],
                        u["id"],
                        u["tier"],
                        sorted(tier_credits),
                        quota,
                    )
                if quota != current_quota:
                    quota_changed += 1
                    logger.info(
                        "monthly_credit_reset.quota_rewritten team=%s user=%s tier=%r %d -> %d",
                        team["id"],
                        u["id"],
                        u["tier"],
                        current_quota,
                        quota,
                    )
                await db.execute(
                    "UPDATE users SET credit_quota = ?, credit_balance = ?, credit_period_start = ? WHERE id = ?",
                    (quota, quota, now, u["id"]),
                )
                # Ledger rows match `UserStore.admin_period_reset`: same columns/reason,
                # delta = balance change. Without them, monthly resets are invisible in the ledger: 116 had
                # zero reset records; whether this month reset could only be inferred from users.credit_period_start,
                # while reconciliation/usage analysis reads credit_ledger.
                delta = quota - int(u["credit_balance"] or 0)
                if delta != 0:
                    await db.execute(
                        "INSERT INTO credit_ledger(id,user_id,team_id,workspace_id,"
                        "turn_id,delta,reason,tokens_input,tokens_output,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (_id("led"), u["id"], team["id"], None, None, delta, "period_reset", 0, 0, now),
                    )
                    ledger_rows += 1
                users_reset += 1
        await db.commit()
    logger.info(
        "monthly_credit_reset.done teams=%d users=%d tier_key_missing=%d quota_changed=%d ledger_rows=%d",
        teams_reset,
        users_reset,
        tier_key_missing,
        quota_changed,
        ledger_rows,
    )
    return {
        "teams_reset": teams_reset,
        "users_reset": users_reset,
        "tier_key_missing": tier_key_missing,
        "quota_changed": quota_changed,
        "ledger_rows": ledger_rows,
    }


# ─── cron loop ────────────────────────────────────────


async def run_cron_loop(db_path: str) -> None:
    """主 loop: 每 1h tick, 跑日 / 月任务. lifespan 启动 + 进程退出取消."""
    last_usage_run: str | None = None
    try:
        while True:
            now = datetime.now(UTC)
            today = now.strftime("%Y-%m-%d")
            # Daily usage rollup; account erasure has its own durable worker.
            if now.hour == 4 and last_usage_run != today:
                try:
                    await rollup_and_prune_llm_usage(db_path)
                except Exception as e:
                    logger.warning("cron.llm_usage_rollup_failed: %s", e)
                last_usage_run = today
            # Monthly credit reset: each tick asks which teams still lack this month's quota; reset only due teams.
            # ⚠ Never restore `day == 1 and hour == 0` + in-process last_monthly_run: if the process is absent
            # for the whole first-day 00:00–00:59 UTC window (e.g. deployment rebuild), the month silently
            # skips with no catch-up, leaving everyone without quota all month and no ledger evidence.
            try:
                due = await teams_due_for_monthly_reset(db_path, now)
                if due:
                    logger.info("cron.monthly_reset_due teams=%d", len(due))
                    await monthly_credit_reset(db_path, team_ids=due)
            except Exception as e:
                logger.warning("cron.monthly_reset_failed: %s", e)
            # sleep 1h
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
