"""Notification center storage.

Phase 1 keeps notifications small and durable: one team-scoped item plus
per-recipient delivery/read/archive state. Badge counts intentionally treat
open action items as the primary signal, even when a user has already read the
notification.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx

NotificationTab = Literal["all", "action", "updates"]
ACTION_REQUIRED_TYPES = frozenset(
    {
        "approval.required",
        "access_requested",
        # Access-request expansion: the 72h admin escalation and the per-approver digest
        # both ask someone to decide, so they count as action items like the request itself.
        "access_requested_escalated",
        "access_requests_digest",
        "scheduled.failed",
        "scheduled.paused",
        # Scheduled task paused awaiting an answer: without action membership, it is only a soft Updates notice users silently miss;
        # then the pause expires and three strikes stop delivery, defeating human-waiting entirely (scheduled-recipe-runs FR-5.6).
        "routine.needs_input",
        "memory.candidate",
        "space_assignment_requested",
        "agent.release.approval_required",
        "agent.eval.regression_failed",
    }
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_items (
    id               TEXT PRIMARY KEY,
    team_id           TEXT NOT NULL,
    category         TEXT NOT NULL,
    type             TEXT NOT NULL,
    priority         TEXT NOT NULL DEFAULT 'p2',
    title            TEXT NOT NULL,
    body             TEXT,
    subject_type     TEXT,
    subject_id       TEXT,
    actor_user_id    TEXT,
    action_url       TEXT,
    actions_json     TEXT NOT NULL DEFAULT '[]',
    metadata_json    TEXT NOT NULL DEFAULT '{}',
    dedupe_key       TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'open',
    resolved_at      TEXT,
    resolved_reason  TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE(team_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_notification_items_team_created
    ON notification_items(team_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_notification_items_subject
    ON notification_items(subject_type, subject_id);

CREATE TABLE IF NOT EXISTS notification_recipients (
    notification_id  TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    role             TEXT,
    delivered_at     TEXT NOT NULL,
    read_at          TEXT,
    archived_at      TEXT,
    PRIMARY KEY(notification_id, user_id),
    FOREIGN KEY(notification_id) REFERENCES notification_items(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_notification_recipients_user
    ON notification_recipients(user_id, archived_at, read_at);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _encode_cursor(created_at: str, notification_id: str) -> str:
    return f"{created_at}|{notification_id}"


def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor or "|" not in cursor:
        return None
    created_at, notification_id = cursor.split("|", 1)
    if not created_at or not notification_id:
        return None
    return created_at, notification_id


@dataclass
class NotificationItem:
    id: str
    team_id: str
    category: str
    type: str
    priority: str
    title: str
    body: str | None
    subject_type: str | None
    subject_id: str | None
    actor_user_id: str | None
    action_url: str | None
    actions: list[dict[str, Any]]
    metadata: dict[str, Any]
    dedupe_key: str
    state: str
    resolved_at: str | None
    resolved_reason: str | None
    created_at: str
    updated_at: str


@dataclass
class NotificationForUser(NotificationItem):
    recipient_user_id: str
    recipient_role: str | None
    delivered_at: str
    read_at: str | None
    archived_at: str | None


@dataclass
class NotificationCount:
    action_required: int
    updates_unread: int

    @property
    def total_badge(self) -> int:
        return self.action_required


@dataclass
class NotificationStore:
    db_path: str

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with open_shared_ctx(self.db_path) as db:
            columns = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(notification_items)")}
            # Development's org -> team migration predates this store's schema
            # migration. Existing notification databases retain org_id, so upgrade
            # the column before SCHEMA creates indexes that reference team_id.
            if "org_id" in columns and "team_id" not in columns:
                await db.execute("ALTER TABLE notification_items RENAME COLUMN org_id TO team_id")
            await db.executescript(SCHEMA)
            await db.commit()

    async def upsert_item(
        self,
        *,
        team_id: str,
        category: str,
        type: str,  # noqa: A002
        priority: str,
        title: str,
        body: str | None,
        subject_type: str | None,
        subject_id: str | None,
        actor_user_id: str | None,
        action_url: str | None,
        actions: list[dict[str, Any]] | None,
        metadata: dict[str, Any] | None,
        dedupe_key: str,
        reopen_resolved: bool = True,
        refresh_created_at: bool = False,
    ) -> NotificationItem:
        """按 `(team_id, dedupe_key)` upsert 一条通知。

        ★ `created_at` 何时刷新（2026-09-14 116 实测：今天学到的记忆建议在首页显示「53天前」；
        列表又按 `created_at` 排序、首页只取 action tab 前 10 条 ⇒ 这类通知不只时间错，还会沉底挑不到）：
        - **重开**已处理的通知（resolved → open）一律刷新：那是新的一次，不是上一段的延续；
        - 调用方显式 `refresh_created_at=True`：同一行承载「最新一批」的聚合型通知
          （`memory.candidate` 每批新建议都换正文）；
        - 其余保持首次时间：`scheduled.failed` 按 job 一条是**有意**把连续失败折成一条，
          未恢复期间「30 天前」＝「从 30 天前起一直在失败」，是对的。
        审批队列走 `upsert_item_with_recipients`、按 `created_at` 升序排「最早待批」，本改动不碰它。
        ⚠ SQLite 的 UPDATE SET 右侧一律读**更新前**的行，所以 CASE 里的 `notification_items.state`
        是旧状态——虽然同一条语句前面也在 SET state。重开用例钉住这一点。
        """
        nid = f"ntf_{uuid4().hex[:12]}"
        now = _now()
        reopen_flag = 1 if reopen_resolved else 0
        refresh_flag = 1 if refresh_created_at else 0
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "INSERT INTO notification_items("
                "id, team_id, category, type, priority, title, body, subject_type, subject_id, "
                "actor_user_id, action_url, actions_json, metadata_json, dedupe_key, state, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?) "
                "ON CONFLICT(team_id, dedupe_key) DO UPDATE SET "
                "category=excluded.category, type=excluded.type, priority=excluded.priority, "
                "title=excluded.title, body=excluded.body, subject_type=excluded.subject_type, "
                "subject_id=excluded.subject_id, actor_user_id=excluded.actor_user_id, "
                "action_url=excluded.action_url, actions_json=excluded.actions_json, "
                "metadata_json=excluded.metadata_json, "
                "state=CASE WHEN ? THEN 'open' ELSE notification_items.state END, "
                "resolved_at=CASE WHEN ? THEN NULL ELSE notification_items.resolved_at END, "
                "resolved_reason=CASE WHEN ? THEN NULL ELSE notification_items.resolved_reason END, "
                "created_at=CASE WHEN ? OR (? AND notification_items.state != 'open') "
                "THEN excluded.created_at ELSE notification_items.created_at END, "
                "updated_at=excluded.updated_at",
                (
                    nid,
                    team_id,
                    category,
                    type,
                    priority,
                    title,
                    body,
                    subject_type,
                    subject_id,
                    actor_user_id,
                    action_url,
                    _json_dumps(actions or []),
                    _json_dumps(metadata or {}),
                    dedupe_key,
                    now,
                    now,
                    reopen_flag,
                    reopen_flag,
                    reopen_flag,
                    refresh_flag,
                    reopen_flag,
                ),
            )
            await db.commit()
            async with db.execute(
                "SELECT * FROM notification_items WHERE team_id=? AND dedupe_key=?",
                (team_id, dedupe_key),
            ) as cur:
                row = await cur.fetchone()
            if row is not None and str(row["created_at"]) == now:
                # ★ A new occurrence resets recipients to unread and unarchives them.
                # Same semantics as `upsert_item_with_recipients`' reopening branch (explicit `read_at=NULL,archived_at=NULL`),
                # previously missing here: `memory.candidate` reuses one row per new suggestion batch,
                # leaving recipients who read the previous batch marked read (frontend `unread = !item.readAt`),
                # so new content appears already handled.
                # ⚠ Badge count is unaffected: action counts depend only on `state='open'`, not read status (`notification_count`).
                # ★ There is **one** new-occurrence criterion: whether created_at was refreshed above (reopen or explicit caller refresh).
                # A separate criterion would eventually diverge and fail silently.
                await db.execute(
                    "UPDATE notification_recipients SET read_at=NULL, archived_at=NULL WHERE notification_id=?",
                    (str(row["id"]),),
                )
                await db.commit()
        if row is None:
            raise RuntimeError("notification upsert failed")
        return _item_row(row)

    async def add_recipients(
        self,
        notification_id: str,
        recipients: list[tuple[str, str | None]],
    ) -> None:
        if not recipients:
            return
        now = _now()
        unique = {(uid, role) for uid, role in recipients if uid}
        async with open_shared_ctx(self.db_path) as db:
            await db.executemany(
                "INSERT INTO notification_recipients("
                "notification_id, user_id, role, delivered_at"
                ") VALUES (?, ?, ?, ?) "
                "ON CONFLICT(notification_id, user_id) DO UPDATE SET "
                "role=excluded.role, archived_at=NULL",
                [(notification_id, uid, role, now) for uid, role in sorted(unique)],
            )
            await db.commit()

    async def upsert_item_with_recipients(
        self,
        *,
        team_id: str,
        category: str,
        type: str,  # noqa: A002
        priority: str,
        title: str,
        body: str | None,
        subject_type: str | None,
        subject_id: str | None,
        actor_user_id: str | None,
        action_url: str | None,
        actions: list[dict[str, Any]] | None,
        metadata: dict[str, Any] | None,
        dedupe_key: str,
        recipients: list[tuple[str, str | None]],
        reopen_resolved: bool = True,
    ) -> NotificationItem:
        """Atomically write one item and all recipients.

        Repeated submissions against an already-open item are true no-ops: they
        neither refresh ``updated_at`` nor unarchive a recipient's personal
        delivery row. A later request may explicitly reopen a resolved item.
        """

        unique = sorted({(uid, role) for uid, role in recipients if uid})
        if not unique:
            raise ValueError("notification recipients are required")
        notification_id = f"ntf_{uuid4().hex[:12]}"
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM notification_items WHERE team_id=? AND dedupe_key=?",
                    (team_id, dedupe_key),
                ) as cur:
                    existing = await cur.fetchone()
                was_resolved = existing is not None and existing["state"] != "open"
                if existing is None:
                    await db.execute(
                        "INSERT INTO notification_items("
                        "id,team_id,category,type,priority,title,body,"
                        "subject_type,subject_id,actor_user_id,action_url,"
                        "actions_json,metadata_json,dedupe_key,state,"
                        "created_at,updated_at"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)",
                        (
                            notification_id,
                            team_id,
                            category,
                            type,
                            priority,
                            title,
                            body,
                            subject_type,
                            subject_id,
                            actor_user_id,
                            action_url,
                            _json_dumps(actions or []),
                            _json_dumps(metadata or {}),
                            dedupe_key,
                            now,
                            now,
                        ),
                    )
                elif was_resolved and reopen_resolved:
                    notification_id = str(existing["id"])
                    await db.execute(
                        "UPDATE notification_items SET category=?,type=?,"
                        "priority=?,title=?,body=?,subject_type=?,subject_id=?,"
                        "actor_user_id=?,action_url=?,actions_json=?,"
                        "metadata_json=?,state='open',resolved_at=NULL,"
                        "resolved_reason=NULL,updated_at=? WHERE id=?",
                        (
                            category,
                            type,
                            priority,
                            title,
                            body,
                            subject_type,
                            subject_id,
                            actor_user_id,
                            action_url,
                            _json_dumps(actions or []),
                            _json_dumps(metadata or {}),
                            now,
                            notification_id,
                        ),
                    )
                else:
                    notification_id = str(existing["id"])
                if existing is None:
                    await db.executemany(
                        "INSERT INTO notification_recipients(notification_id,user_id,role,delivered_at) VALUES (?,?,?,?)",
                        [(notification_id, uid, role, now) for uid, role in unique],
                    )
                elif was_resolved and reopen_resolved:
                    await db.executemany(
                        "INSERT INTO notification_recipients("
                        "notification_id,user_id,role,delivered_at"
                        ") VALUES (?,?,?,?) "
                        "ON CONFLICT(notification_id,user_id) DO UPDATE SET "
                        "role=excluded.role,delivered_at=excluded.delivered_at,"
                        "read_at=NULL,archived_at=NULL",
                        [(notification_id, uid, role, now) for uid, role in unique],
                    )
                else:
                    await db.executemany(
                        "INSERT INTO notification_recipients("
                        "notification_id,user_id,role,delivered_at"
                        ") VALUES (?,?,?,?) "
                        "ON CONFLICT(notification_id,user_id) DO NOTHING",
                        [(notification_id, uid, role, now) for uid, role in unique],
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            async with db.execute(
                "SELECT * FROM notification_items WHERE id=?",
                (notification_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            raise RuntimeError("notification upsert failed")
        return _item_row(row)

    async def resolve_by_dedupe(
        self,
        *,
        team_id: str,
        dedupe_key: str,
        reason: str,
    ) -> NotificationItem | None:
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            updated = await db.execute(
                "UPDATE notification_items SET state='resolved', resolved_at=?, "
                "resolved_reason=?, updated_at=? WHERE team_id=? AND dedupe_key=? "
                "AND state='open'",
                (now, reason, now, team_id, dedupe_key),
            )
            await db.commit()
            async with db.execute(
                "SELECT * FROM notification_items WHERE team_id=? AND dedupe_key=?",
                (team_id, dedupe_key),
            ) as cur:
                row = await cur.fetchone()
        if updated.rowcount <= 0:
            return None
        return _item_row(row) if row else None

    async def get_open_by_dedupe(self, *, team_id: str, dedupe_key: str) -> NotificationItem | None:
        """The open item for ``(team_id, dedupe_key)``, or None when absent/resolved."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM notification_items WHERE team_id=? AND dedupe_key=? AND state='open'",
                (team_id, dedupe_key),
            ) as cur:
                row = await cur.fetchone()
        return _item_row(row) if row else None

    async def resolve_open_by_subject(
        self,
        *,
        team_id: str,
        subject_type: str,
        subject_id: str,
        reason: str,
    ) -> int:
        """关掉某个主体名下**所有**还开着的通知，返回关掉的条数。

        `resolve_by_dedupe` 是「一件事一条」的闭环（run 离开 awaiting 关它自己那条）；
        这里是「主体没了」的闭环——定时任务被删除后，它名下的挂起待办与暂停通知都指向一个
        已不存在的 job，深链点进去无 job/run 归属。按 dedupe 逐条关做不到：run 级的 key 带
        run_id，删除时不知道有多少条。
        """
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(
                "UPDATE notification_items SET state='resolved', resolved_at=?, "
                "resolved_reason=?, updated_at=? WHERE team_id=? AND subject_type=? "
                "AND subject_id=? AND state='open'",
                (now, reason, now, team_id, subject_type, subject_id),
            )
            await db.commit()
            return int(cur.rowcount or 0)

    async def list_open_required_approval_items(
        self,
        *,
        limit: int = 500,
    ) -> list[NotificationItem]:
        limit = max(1, min(limit, 5000))
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM notification_items WHERE type='approval.required' AND state='open' ORDER BY created_at ASC, id ASC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        return [_item_row(row) for row in rows]

    async def list_for_user(
        self,
        *,
        user_id: str,
        tab: NotificationTab = "all",
        limit: int = 30,
        cursor: str | None = None,
    ) -> tuple[list[NotificationForUser], str | None]:
        limit = max(1, min(limit, 100))
        params: list[Any] = [user_id]
        where = ["r.user_id = ?", "r.archived_at IS NULL"]
        if tab == "action":
            where.append(f"i.type IN ({','.join(['?'] * len(ACTION_REQUIRED_TYPES))})")
            params.extend(sorted(ACTION_REQUIRED_TYPES))
            where.append("i.state = 'open'")
        elif tab == "updates":
            where.append(f"i.type NOT IN ({','.join(['?'] * len(ACTION_REQUIRED_TYPES))})")
            params.extend(sorted(ACTION_REQUIRED_TYPES))
        decoded = _decode_cursor(cursor)
        if decoded is not None:
            created_at, notification_id = decoded
            where.append("(i.created_at < ? OR (i.created_at = ? AND i.id < ?))")
            params.extend([created_at, created_at, notification_id])
        params.append(limit + 1)
        sql = (
            "SELECT i.*, r.user_id AS recipient_user_id, r.role AS recipient_role, "  # noqa: S608
            "r.delivered_at, r.read_at, r.archived_at "
            "FROM notification_recipients r "
            "JOIN notification_items i ON i.id = r.notification_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY i.created_at DESC, i.id DESC LIMIT ?"
        )
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        page_rows = rows[:limit]
        next_cursor = None
        if len(rows) > limit and page_rows:
            last = page_rows[-1]
            next_cursor = _encode_cursor(last["created_at"], last["id"])
        return [_user_row(row) for row in page_rows], next_cursor

    async def list_unread_by_type(
        self,
        *,
        user_id: str,
        type: str,  # noqa: A002  Same name as the analogous `upsert_item_with_recipients`
        limit: int = 5,
    ) -> list[NotificationForUser]:
        """某一类型的未读通知（最新在前）。

        给**通知中心以外**的送达面用：`list_for_user` 按 tab 分页 30 条且不按类型过滤，
        一条 p2 回执很容易被同期的 routine 通知挤到第二页——前端在一页里筛类型是不可靠的。

        送达面只需要「有没有、是哪几条」，所以默认只取 5 条：它渲染成卡片，
        多了就成了第二个收件箱（而收件箱这条路 116 实测 139 条 0 点击）。
        """
        limit = max(1, min(limit, 50))
        sql = (
            "SELECT i.*, r.user_id AS recipient_user_id, r.role AS recipient_role, "
            "r.delivered_at, r.read_at, r.archived_at "
            "FROM notification_recipients r "
            "JOIN notification_items i ON i.id = r.notification_id "
            "WHERE r.user_id = ? AND r.archived_at IS NULL AND r.read_at IS NULL "
            "AND i.type = ? "
            "ORDER BY i.created_at DESC, i.id DESC LIMIT ?"
        )
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (user_id, type, limit)) as cur:
                rows = await cur.fetchall()
        return [_user_row(row) for row in rows]

    async def notification_count(self, *, user_id: str) -> NotificationCount:
        placeholders = ",".join(["?"] * len(ACTION_REQUIRED_TYPES))
        action_params: list[Any] = [user_id, *sorted(ACTION_REQUIRED_TYPES)]
        update_params: list[Any] = [user_id, *sorted(ACTION_REQUIRED_TYPES)]
        async with open_shared_ctx(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM notification_recipients r "  # noqa: S608
                "JOIN notification_items i ON i.id = r.notification_id "
                "WHERE r.user_id=? AND r.archived_at IS NULL "
                f"AND i.type IN ({placeholders}) AND i.state='open'",
                tuple(action_params),
            ) as cur:
                action_row = await cur.fetchone()
            async with db.execute(
                "SELECT COUNT(*) FROM notification_recipients r "  # noqa: S608
                "JOIN notification_items i ON i.id = r.notification_id "
                "WHERE r.user_id=? AND r.archived_at IS NULL AND r.read_at IS NULL "
                f"AND i.type NOT IN ({placeholders})",
                tuple(update_params),
            ) as cur:
                update_row = await cur.fetchone()
        return NotificationCount(
            action_required=int(action_row[0] if action_row else 0),
            updates_unread=int(update_row[0] if update_row else 0),
        )

    async def mark_read(self, *, notification_id: str, user_id: str) -> bool:
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(
                "UPDATE notification_recipients SET read_at=COALESCE(read_at, ?) WHERE notification_id=? AND user_id=? AND archived_at IS NULL",
                (now, notification_id, user_id),
            )
            await db.commit()
        return cur.rowcount > 0

    async def mark_all_read(
        self,
        *,
        user_id: str,
        tab: NotificationTab = "all",
    ) -> int:
        now = _now()
        params: list[Any] = [now, user_id]
        where = ["user_id=?", "archived_at IS NULL", "read_at IS NULL"]
        if tab == "action":
            where.append(
                "notification_id IN ("  # noqa: S608
                "SELECT id FROM notification_items "
                f"WHERE type IN ({','.join(['?'] * len(ACTION_REQUIRED_TYPES))})"
                ")"
            )
            params.extend(sorted(ACTION_REQUIRED_TYPES))
        elif tab == "updates":
            where.append(
                "notification_id IN ("  # noqa: S608
                "SELECT id FROM notification_items "
                f"WHERE type NOT IN ({','.join(['?'] * len(ACTION_REQUIRED_TYPES))})"
                ")"
            )
            params.extend(sorted(ACTION_REQUIRED_TYPES))
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(
                f"UPDATE notification_recipients SET read_at=? WHERE {' AND '.join(where)}",  # noqa: S608
                tuple(params),
            )
            await db.commit()
        return cur.rowcount

    async def archive(self, *, notification_id: str, user_id: str) -> bool:
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(
                "UPDATE notification_recipients SET archived_at=COALESCE(archived_at, ?) WHERE notification_id=? AND user_id=?",
                (now, notification_id, user_id),
            )
            await db.commit()
        return cur.rowcount > 0

    async def prune_archived(self, *, older_than: str, limit: int = 500) -> dict[str, int]:
        """Delete archived recipient rows older than the retention cutoff.

        Only archived per-recipient state is eligible. Open action items and
        unread updates stay untouched; notification_items are removed only after
        all recipient rows for that item have been pruned.
        """
        limit = max(1, min(limit, 5000))
        async with open_shared_ctx(self.db_path) as db:
            recipients = await db.execute(
                "DELETE FROM notification_recipients "
                "WHERE rowid IN ("
                "SELECT rowid FROM notification_recipients "
                "WHERE archived_at IS NOT NULL AND archived_at < ? "
                "ORDER BY archived_at ASC LIMIT ?"
                ")",
                (older_than, limit),
            )
            items = await db.execute(
                "DELETE FROM notification_items "
                "WHERE id IN ("
                "SELECT i.id FROM notification_items i "
                "WHERE i.created_at < ? "
                "AND NOT EXISTS ("
                "SELECT 1 FROM notification_recipients r WHERE r.notification_id = i.id"
                ") "
                "LIMIT ?"
                ")",
                (older_than, limit),
            )
            await db.commit()
        return {
            "recipients_deleted": max(0, recipients.rowcount),
            "items_deleted": max(0, items.rowcount),
        }


def _item_row(row: aiosqlite.Row) -> NotificationItem:
    return NotificationItem(
        id=row["id"],
        team_id=row["team_id"],
        category=row["category"],
        type=row["type"],
        priority=row["priority"],
        title=row["title"],
        body=row["body"],
        subject_type=row["subject_type"],
        subject_id=row["subject_id"],
        actor_user_id=row["actor_user_id"],
        action_url=row["action_url"],
        actions=_json_loads(row["actions_json"], []),
        metadata=_json_loads(row["metadata_json"], {}),
        dedupe_key=row["dedupe_key"],
        state=row["state"],
        resolved_at=row["resolved_at"],
        resolved_reason=row["resolved_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _user_row(row: aiosqlite.Row) -> NotificationForUser:
    item = _item_row(row)
    return NotificationForUser(
        **item.__dict__,
        recipient_user_id=row["recipient_user_id"],
        recipient_role=row["recipient_role"],
        delivered_at=row["delivered_at"],
        read_at=row["read_at"],
        archived_at=row["archived_at"],
    )
