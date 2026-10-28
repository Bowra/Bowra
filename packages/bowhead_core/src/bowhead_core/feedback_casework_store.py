"""教练办案 sidecar store（B 线批 3a，tech-design §2.8.2/§2.8.5）。

一候选一行。**互斥在行上**：`claim` 用 `UPDATE … WHERE status IN (queued/failed/blocked)` 的
rowcount 判定，rowcount=1 才起草——`try_acquire_gc_lock` 只是时间租约，不是互斥。
`blocked` 不是终态（每 tick 重评、不计 attempts）；`failed` attempts ≤ 3。
疲劳闸按 `started_at` 落在 UTC 自然日内的行计数（与候选配额同口径）。
表由 migration 0062 建；本 store 无 initialize DDL。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from bowhead_core._db import open_shared_ctx

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"

MAX_ATTEMPTS = 3
REDRAFTS_PER_DAY = 3


def _utcnow() -> datetime:
    return datetime.now(UTC)


def iso_utc(dt: datetime) -> str:
    """与本表所有时间列同一份 ISO 口径——跨模块做字符串比较必须用同一个（worker 的隔天重试判据要用）。"""

    return _iso(dt)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


@dataclass(frozen=True)
class FeedbackCasework:
    candidate_id: str
    team_id: str
    trigger: str
    status: str
    reason_code: str | None
    report: dict[str, Any]
    prefill: dict[str, Any] | None
    base: dict[str, Any]
    attempts: int
    redraft_count: int
    redraft_day: str
    receipt_at: str | None
    started_at: str | None
    finished_at: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> FeedbackCasework:
        return cls(
            candidate_id=row["candidate_id"],
            team_id=row["team_id"],
            trigger=row["trigger"],
            status=row["status"],
            reason_code=row["reason_code"],
            report=json.loads(row["report_json"] or "{}"),
            prefill=json.loads(row["prefill_json"]) if row["prefill_json"] else None,
            base=json.loads(row["base_json"] or "{}"),
            attempts=int(row["attempts"]),
            redraft_count=int(row["redraft_count"]),
            redraft_day=row["redraft_day"] or "",
            receipt_at=row["receipt_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def public(self, *, reviewer: bool = False) -> dict[str, Any]:
        """读面投影：不带 prefill 正文（那是采纳时才给的），不带模型自报的展示名。

        ``reviewer=False``（提议人视角）连改道理由也不给——那是教练看着完整配置面写的散文，
        提议人本来看不到那份配置面（§2.4「自己的份」）。
        """

        reroute = self.report.get("reroute") if isinstance(self.report, dict) else None
        return {
            "status": self.status,
            "verdict": self.report.get("verdict") if isinstance(self.report, dict) else None,
            "reason_code": self.reason_code,
            "reroute_target": (
                {
                    "target_kind": reroute.get("target_kind"),
                    "target_ref": reroute.get("target_ref"),
                    "reason": reroute.get("reason") if reviewer else None,
                }
                if isinstance(reroute, dict)
                else None
            ),
            "evidence_used": self.report.get("evidence_used") if isinstance(self.report, dict) else None,
            "drafted_at": self.finished_at,
        }


@dataclass
class FeedbackCaseworkStore:
    db_path: str

    async def get(self, candidate_id: str) -> FeedbackCasework | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM feedback_casework WHERE candidate_id=?", (candidate_id,))
            row = await cursor.fetchone()
            return FeedbackCasework.from_row(row) if row is not None else None

    async def get_many(self, candidate_ids: list[str]) -> dict[str, FeedbackCasework]:
        ids = [c for c in dict.fromkeys(candidate_ids) if c]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM feedback_casework WHERE candidate_id IN ({placeholders})",  # noqa: S608 -- placeholders only
                ids,
            )
            return {r["candidate_id"]: FeedbackCasework.from_row(r) for r in await cursor.fetchall()}

    async def ensure_row(self, candidate_id: str, team_id: str, *, trigger: str) -> None:
        now = _iso(_utcnow())
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO feedback_casework
                  (candidate_id, team_id, trigger, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (candidate_id, team_id, trigger, STATUS_QUEUED, now, now),
            )
            await db.commit()

    async def claim(self, candidate_id: str, team_id: str, *, trigger: str, stale_after_s: int) -> bool:
        """行级互斥：queued/failed/blocked（或 running 超时）→ running。rowcount=1 才起草。

        超时判据用 ``updated_at``（claim 时刻）；``started_at`` 只在真正调教练时盖（`mark_coaching`），
        否则日 cap 会被根本没到模型的 claim 吃掉（review P1-1）。
        """

        now = _utcnow()
        await self.ensure_row(candidate_id, team_id, trigger=trigger)
        stale_before = _iso(datetime.fromtimestamp(now.timestamp() - stale_after_s, tz=UTC))
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE feedback_casework
                SET status=?, trigger=?, updated_at=?, reason_code=NULL
                WHERE candidate_id=? AND (
                    status IN (?, ?, ?)
                    OR (status=? AND updated_at < ?)
                )
                """,
                (
                    STATUS_RUNNING,
                    trigger,
                    _iso(now),
                    candidate_id,
                    STATUS_QUEUED,
                    STATUS_FAILED,
                    STATUS_BLOCKED,
                    STATUS_RUNNING,
                    stale_before,
                ),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def mark_coaching(self, candidate_id: str) -> None:
        """真正调教练前盖 ``started_at``——日 cap 数的就是这一枚戳（只数到模型的调用）。"""

        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "UPDATE feedback_casework SET started_at=? WHERE candidate_id=? AND status=?",
                (_iso(_utcnow()), candidate_id, STATUS_RUNNING),
            )
            await db.commit()

    async def finish(
        self,
        candidate_id: str,
        *,
        report: dict[str, Any],
        prefill: dict[str, Any] | None,
        base: dict[str, Any],
        running_only: bool = False,
    ) -> bool:
        """``running_only=True``（worker 路径）只写自己还持有的 running 行：采纳路由已 `replace_cache`
        覆盖的行不再被后台结果盖掉（review P1-2）。返回是否写到了行。"""

        now = _iso(_utcnow())
        guard = " AND status=?" if running_only else ""
        params: list[Any] = [
            STATUS_DONE,
            json.dumps(report, ensure_ascii=False),
            json.dumps(prefill, ensure_ascii=False) if prefill is not None else None,
            json.dumps(base, ensure_ascii=False),
            now,
            now,
            candidate_id,
        ]
        if running_only:
            params.append(STATUS_RUNNING)
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE feedback_casework SET status=?, reason_code=NULL, report_json=?, prefill_json=?, base_json=?, "
                "finished_at=?, updated_at=? WHERE candidate_id=?" + guard,  # noqa: S608 -- guard is a literal
                params,
            )
            await db.commit()
            return cursor.rowcount == 1

    async def fail(self, candidate_id: str, *, reason_code: str, report: dict[str, Any] | None = None, running_only: bool = False) -> bool:
        """失败计 attempts；到顶后仍是 failed（worker 不再捞）。``running_only`` 语义同 `finish`。"""

        now = _iso(_utcnow())
        guard = " AND status=?" if running_only else ""
        params: list[Any] = [STATUS_FAILED, reason_code, json.dumps(report or {}, ensure_ascii=False), now, now, candidate_id]
        if running_only:
            params.append(STATUS_RUNNING)
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE feedback_casework SET status=?, reason_code=?, attempts=attempts+1, report_json=?, finished_at=?, "
                "updated_at=? WHERE candidate_id=?" + guard,  # noqa: S608 -- guard is a literal
                params,
            )
            await db.commit()
            return cursor.rowcount == 1

    async def invalidate(self, candidate_id: str) -> None:
        """显式 redraft：只作废 done 缓存（不计 attempts、不碰 running 行）。"""

        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "UPDATE feedback_casework SET status=?, reason_code=?, prefill_json=NULL, base_json='{}', report_json='{}', "
                "finished_at=NULL, updated_at=? WHERE candidate_id=? AND status=?",
                (STATUS_QUEUED, "redraft_requested", _iso(_utcnow()), candidate_id, STATUS_DONE),
            )
            await db.commit()

    async def block(self, candidate_id: str, *, reason_code: str) -> bool:
        """阻塞不计 attempts、每 tick 重评；不碰别人正持有的 running 行。
        返回是否为「从非 blocked 翻到 blocked」（402 事件只在这时写一条）。"""

        now = _iso(_utcnow())
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT status, reason_code FROM feedback_casework WHERE candidate_id=?", (candidate_id,))
            row = await cursor.fetchone()
            was_blocked = row is not None and row["status"] == STATUS_BLOCKED and row["reason_code"] == reason_code
            cursor = await db.execute(
                "UPDATE feedback_casework SET status=?, reason_code=?, finished_at=NULL, updated_at=? WHERE candidate_id=? AND status<>?",
                (STATUS_BLOCKED, reason_code, now, candidate_id, STATUS_RUNNING),
            )
            await db.commit()
            return cursor.rowcount == 1 and not was_blocked

    async def has_blocked(self, team_id: str, *, reason_code: str, since: datetime) -> bool:
        """团队里是否有**最近**（``updated_at >= since``）被同因 block 过的行——402 事件按团队 episode 一条。

        按新近度而不是存在性判：撤回/过期离池的行、被日 cap 跳过没重盖的行都会永远留在 blocked，
        若只看存在性，一个团队一辈子只能记一次 402（review 二轮 P1）。`block()` 每 tick 都重盖 `updated_at`，
        所以「上个 tick 还在 block」＝episode 未断，「陈旧的 blocked」＝新 episode。
        """

        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM feedback_casework WHERE team_id=? AND status=? AND reason_code=? AND updated_at>=? LIMIT 1",
                (team_id, STATUS_BLOCKED, reason_code, _iso(since)),
            )
            return await cursor.fetchone() is not None

    async def count_started_today(self, team_id: str, now: datetime | None = None) -> int:
        """今日（UTC）真正调过教练的次数（`mark_coaching` 盖的戳）。"""

        day = _day(now or _utcnow())
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                "SELECT count(*) FROM feedback_casework WHERE team_id=? AND started_at IS NOT NULL AND substr(started_at,1,10)=?",
                (team_id, day),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def count_started_this_month(self, team_id: str, now: datetime | None = None) -> int:
        """本月（UTC）教练起草次数——收件箱页角落「本月教练起草 N 次」（§2.8.3 ③ owner 可见用量）。"""

        month = _day(now or _utcnow())[:7]
        async with open_shared_ctx(self.db_path) as db:
            cursor = await db.execute(
                "SELECT count(*) FROM feedback_casework WHERE team_id=? AND started_at IS NOT NULL AND substr(started_at,1,7)=?",
                (team_id, month),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def take_redraft_slot(self, candidate_id: str, team_id: str, now: datetime | None = None) -> bool:
        """每候选每 UTC 日 redraft ≤ REDRAFTS_PER_DAY；无行先 upsert（trigger=explicit）。"""

        now = now or _utcnow()
        day = _day(now)
        await self.ensure_row(candidate_id, team_id, trigger="explicit")
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "UPDATE feedback_casework SET redraft_count=0, redraft_day=? WHERE candidate_id=? AND redraft_day<>?",
                (day, candidate_id, day),
            )
            cursor = await db.execute(
                "UPDATE feedback_casework SET redraft_count=redraft_count+1, updated_at=? WHERE candidate_id=? AND redraft_count<?",
                (_iso(now), candidate_id, REDRAFTS_PER_DAY),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def release_redraft_slot(self, candidate_id: str, now: datetime | None = None) -> None:
        """退回一个当日 redraft 槽（教练根本没产出时：超时/解析失败/上游全败）。只在同一 UTC 日且计数>0 时退。"""

        now = now or _utcnow()
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "UPDATE feedback_casework SET redraft_count=redraft_count-1, updated_at=? WHERE candidate_id=? AND redraft_day=? AND redraft_count>0",
                (_iso(now), candidate_id, _day(now)),
            )
            await db.commit()

    async def replace_cache(
        self, candidate_id: str, team_id: str, *, report: dict[str, Any], prefill: dict[str, Any] | None, base: dict[str, Any]
    ) -> None:
        """显式 redraft 后覆盖缓存（status 直接 done）。"""

        await self.ensure_row(candidate_id, team_id, trigger="explicit")
        await self.finish(candidate_id, report=report, prefill=prefill, base=base)
