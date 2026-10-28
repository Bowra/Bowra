"""Dreaming REM + Deep — 周期性汇总 + 报告.

REM：
    从 skill_observations + turn_events 聚合出（历史注：写入方 SkillDistiller 已删除
    ——见 docs/skills-tools-ssot.md §5-G8；旧库有表则照常聚合，新库无表返回空 digest）
    "最近 N 天高频查询模式"，生成简短摘要字符串，可由 PromptBuilder 注入到
    Lead system prompt 的 "Your users frequently ask ..." 段落。

Deep：
    统计 token 使用 / 工具失败率 / 平均延迟，产出运维级别的报告 dict。
    供 CLI `bowhead dream digest` 展示或 cron 上报。

不做调度器（cron）；上层调用方负责定时触发。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class RemDigest:
    top_patterns: list[dict[str, Any]]  # [{intent, tool_chain, sections, count}, …]
    window_days: int
    total_turns: int

    def as_prompt_block(self) -> str:
        """供 PromptBuilder 注入的 Markdown 片段."""
        if not self.top_patterns:
            return ""
        lines = [f"## Your users' recent patterns (last {self.window_days}d)"]
        for p in self.top_patterns[:8]:
            lines.append(
                f"- **{p['intent']}**: tools=`{p['tool_chain']}` → sections=`{p['sections']}` ({p['count']}×)",
            )
        return "\n".join(lines) + "\n"


@dataclass
class DeepDigest:
    window_hours: int
    total_turns: int
    by_event_type: dict[str, int]  # event_type → count
    tool_usage: dict[str, int]  # tool_name → count
    tool_failure_rate: dict[str, float]  # tool_name → fail rate (0..1)
    avg_duration_ms: float | None
    errors: list[str]  # recent error messages (deduped)


@dataclass
class DreamingEngine:
    db_path: str  # SQLite path (shared with TurnLogStore; legacy skill_observations uses the same database).

    # ─── REM ──────────────────────────────────────────────
    async def rem_digest(
        self,
        *,
        window_days: int = 14,
        top_k: int = 20,
    ) -> RemDigest:
        since_iso = (datetime.now(tz=UTC) - timedelta(days=window_days)).isoformat(timespec="seconds")

        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row

                # total turns in window
                async with db.execute(
                    "SELECT COUNT(DISTINCT turn_id) FROM skill_observations WHERE created_at >= ?",
                    (since_iso,),
                ) as cur:
                    row = await cur.fetchone()
                total = int(row[0]) if row else 0

                async with db.execute(
                    """
                    SELECT intent, tool_chain_sig, section_kinds_sig,
                           COUNT(*) AS n
                    FROM skill_observations
                    WHERE created_at >= ?
                    GROUP BY intent, tool_chain_sig, section_kinds_sig
                    ORDER BY n DESC
                    LIMIT ?
                    """,
                    (since_iso, top_k),
                ) as cur:
                    rows = await cur.fetchall()
        except sqlite3.OperationalError as exc:
            # The skill_observations writer (SkillDistiller) was removed; new databases no longer have this table.
            # Old databases can still aggregate historical data; a missing table returns an empty digest instead of a 500.
            if "no such table" not in str(exc).lower():
                raise
            logger.info("dreaming.rem_digest skill_observations absent — empty digest")
            return RemDigest(top_patterns=[], window_days=window_days, total_turns=0)

        patterns = [
            {
                "intent": r["intent"],
                "tool_chain": r["tool_chain_sig"],
                "sections": r["section_kinds_sig"],
                "count": int(r["n"]),
            }
            for r in rows
        ]
        logger.info(
            "dreaming.rem_digest window=%dd total=%d patterns=%d",
            window_days,
            total,
            len(patterns),
        )
        return RemDigest(
            top_patterns=patterns,
            window_days=window_days,
            total_turns=total,
        )

    # ─── Deep ─────────────────────────────────────────────
    async def deep_digest(
        self,
        *,
        window_hours: int = 24,
        error_sample_limit: int = 10,
    ) -> DeepDigest:
        since_iso = (datetime.now(tz=UTC) - timedelta(hours=window_hours)).isoformat(timespec="seconds")

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            async with db.execute(
                "SELECT COUNT(DISTINCT turn_id) FROM turn_events WHERE occurred_at >= ?",
                (since_iso,),
            ) as cur:
                row = await cur.fetchone()
            total = int(row[0]) if row else 0

            async with db.execute(
                "SELECT event_type, COUNT(*) FROM turn_events WHERE occurred_at >= ? GROUP BY event_type",
                (since_iso,),
            ) as cur:
                by_type = {r[0]: int(r[1]) for r in await cur.fetchall()}

            # Parse tool usage + failures from tool_result event payloads.
            async with db.execute(
                "SELECT payload_json FROM turn_events WHERE event_type='tool_result' AND occurred_at >= ?",
                (since_iso,),
            ) as cur:
                tool_rows = await cur.fetchall()

            tool_usage: dict[str, int] = {}
            tool_fails: dict[str, int] = {}
            durations: list[int] = []
            for tr in tool_rows:
                try:
                    p = json.loads(tr[0])
                except (json.JSONDecodeError, TypeError):
                    continue
                name = p.get("tool_name", "?")
                tool_usage[name] = tool_usage.get(name, 0) + 1
                if p.get("ok") is False:
                    tool_fails[name] = tool_fails.get(name, 0) + 1
                d = p.get("duration_ms")
                if isinstance(d, (int, float)):
                    durations.append(int(d))

            failure_rate = {n: round(tool_fails.get(n, 0) / tool_usage[n], 4) for n in tool_usage}

            # Error samples (the most recent N entries).
            async with db.execute(
                "SELECT payload_json FROM turn_events WHERE event_type='error' AND occurred_at >= ? ORDER BY occurred_at DESC LIMIT ?",
                (since_iso, error_sample_limit),
            ) as cur:
                err_rows = await cur.fetchall()
            errors: list[str] = []
            seen: set[str] = set()
            for er in err_rows:
                try:
                    p = json.loads(er[0])
                    msg = str(p.get("message") or "")[:200]
                    if msg and msg not in seen:
                        errors.append(msg)
                        seen.add(msg)
                except (json.JSONDecodeError, TypeError):
                    continue

        avg = sum(durations) / len(durations) if durations else None
        logger.info(
            "dreaming.deep_digest window=%dh turns=%d tools=%d err=%d",
            window_hours,
            total,
            len(tool_usage),
            len(errors),
        )
        return DeepDigest(
            window_hours=window_hours,
            total_turns=total,
            by_event_type=by_type,
            tool_usage=tool_usage,
            tool_failure_rate=failure_rate,
            avg_duration_ms=avg,
            errors=errors,
        )
