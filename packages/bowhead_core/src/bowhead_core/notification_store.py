"""Notification center storage.

Phase 1 keeps notifications small and durable: one team-scoped item plus
per-recipient delivery/read/archive state. Badge counts intentionally treat
open action items as the primary signal, even when a user has already read the
notification.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.notification_types import ACTION_REQUIRED_TYPES, Occurrence
from bowhead_core.sqlite_domains import domain_database_path, domain_layout_active, require_domain_schema

NotificationTab = Literal["all", "action", "updates"]
WriteOutcome = Literal["inserted", "unchanged", "updated", "renewed", "reopened", "resolved_unchanged"]

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


DeliveryStatus = Literal["pending", "sent", "skipped", "failed", "dead"]


@dataclass(frozen=True)
class NotificationDelivery:
    """One recorded email for one recipient of one item (``notification_deliveries``)."""

    id: str
    notification_id: str
    user_id: str
    channel: str
    status: str
    not_before: str
    attempts: int
    outcome_code: str | None
    diagnostic_ref: str | None
    created_at: str
    updated_at: str
    item_dedupe_key: str | None = None


@dataclass(frozen=True)
class NotificationWrite:
    """The result of `NotificationStore.write`.

    ``delivered_to``: who received the item in this write (every listed recipient for a new
    occurrence, only newly added people otherwise). Channels beyond the in-app list key off it.
    """

    item: NotificationItem
    outcome: WriteOutcome
    delivered_to: tuple[str, ...] = field(default_factory=tuple)

    @property
    def new_occurrence(self) -> bool:
        return self.outcome in {"inserted", "renewed", "reopened"}


@dataclass
class NotificationStore:
    db_path: str

    def __post_init__(self) -> None:
        self.db_path = str(domain_database_path(self.db_path, "notifications"))

    async def initialize(self) -> None:
        if domain_layout_active(self.db_path):
            async with open_shared_ctx(self.db_path) as db:
                await require_domain_schema(db, "notifications")
            return
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

    async def write(
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
        occurrence: Occurrence = "once",
        reopen_resolved: bool = True,
        escalate_after_s: int | None = None,
        email_delivery_to: Collection[str] = (),
    ) -> NotificationWrite:
        """Write one item and its recipients in one transaction: the only notification write.

        Keyed by ``(team_id, dedupe_key)``. What a repeat does depends on the item's state:

        - **absent**: insert the item and every recipient.
        - **open**: ``once`` changes nothing; ``update`` replaces the content but keeps
          ``created_at`` and every recipient's read/archive state; ``renew`` replaces the content,
          moves ``created_at`` to now and makes the listed recipients unread and unarchived again.
          Recipients not yet on the item are added unread in every case.
        - **resolved**, ``reopen_resolved``: a new occurrence. Reopen with the new content and a new
          ``created_at``; the listed recipients become unread and unarchived again.
        - **resolved**, not reopenable: nothing changes.

        A recipient's archive is never undone by a repeat, only by a new occurrence (``renew`` or a
        reopen). A new occurrence goes to the listed recipients: rows of people no longer listed
        (a departed administrator) are removed so a reopened item does not reach them.

        Whether this was a new occurrence, and who just received the item, are decided here, once,
        inside the transaction (`NotificationWrite`).

        Email rows (``notification_deliveries``, FEATURE_NOTIFICATION_DELIVERY) are written in the same
        transaction: ``escalate_after_s`` schedules one escalation per person who just received the
        item; ``email_delivery_to`` schedules an immediate configured email on a new occurrence.

        ⚠ The list is ordered by ``created_at``; refreshing it is what moves a new occurrence to the
        top (2026-09-14 on 116: a suggestion learned that day showed "53 days ago" and sank).
        """

        unique = _unique_recipients(recipients)
        listed = [uid for uid, _role in unique]
        notification_id = f"ntf_{uuid4().hex[:12]}"
        now = _now()
        content = (
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
        )
        outcome: WriteOutcome
        delivered_to: list[str]
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM notification_items WHERE team_id=? AND dedupe_key=?",
                    (team_id, dedupe_key),
                ) as cur:
                    existing = await cur.fetchone()
                if existing is None:
                    await db.execute(
                        "INSERT INTO notification_items("
                        "id,team_id,category,type,priority,title,body,"
                        "subject_type,subject_id,actor_user_id,action_url,"
                        "actions_json,metadata_json,dedupe_key,state,"
                        "created_at,updated_at"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)",
                        (notification_id, team_id, *content, dedupe_key, now, now),
                    )
                    await _insert_new_recipients(db, notification_id, unique, now)
                    outcome, delivered_to = "inserted", listed
                else:
                    notification_id = str(existing["id"])
                    is_open = existing["state"] == "open"
                    if is_open and occurrence in {"once", "update"}:
                        if occurrence == "update":
                            await db.execute(
                                "UPDATE notification_items SET category=?,type=?,priority=?,title=?,body=?,"
                                "subject_type=?,subject_id=?,actor_user_id=?,action_url=?,actions_json=?,"
                                "metadata_json=?,updated_at=? WHERE id=?",
                                (*content, now, notification_id),
                            )
                        delivered_to = await _insert_new_recipients(db, notification_id, unique, now)
                        outcome = "updated" if occurrence == "update" else "unchanged"
                    elif is_open or reopen_resolved:
                        # A new occurrence: `renew` of an open item, or reopening a resolved one.
                        await db.execute(
                            "UPDATE notification_items SET category=?,type=?,priority=?,title=?,body=?,"
                            "subject_type=?,subject_id=?,actor_user_id=?,action_url=?,actions_json=?,"
                            "metadata_json=?,state='open',resolved_at=NULL,resolved_reason=NULL,"
                            "created_at=?,updated_at=? WHERE id=?",
                            (*content, now, now, notification_id),
                        )
                        await _reset_recipients(db, notification_id, unique, now)
                        outcome, delivered_to = ("renewed" if is_open else "reopened"), listed
                    else:
                        outcome, delivered_to = "resolved_unchanged", []
                new_occurrence = outcome in {"inserted", "renewed", "reopened"}
                if escalate_after_s is not None and delivered_to:
                    await _schedule_deliveries(db, notification_id, delivered_to, "email_escalation", _after(now, escalate_after_s), now)
                if email_delivery_to and new_occurrence:
                    await _schedule_deliveries(db, notification_id, sorted(set(email_delivery_to)), "email_delivery", now, now)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            async with db.execute("SELECT * FROM notification_items WHERE id=?", (notification_id,)) as cur:
                row = await cur.fetchone()
        if row is None:
            raise RuntimeError("notification write failed")
        return NotificationWrite(item=_item_row(row), outcome=outcome, delivered_to=tuple(delivered_to))

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
        occurrence: Occurrence = "once",
    ) -> NotificationItem:
        """`write` for callers that only need the item (the Agent outboxes' projection step)."""
        result = await self.write(
            team_id=team_id,
            category=category,
            type=type,
            priority=priority,
            title=title,
            body=body,
            subject_type=subject_type,
            subject_id=subject_id,
            actor_user_id=actor_user_id,
            action_url=action_url,
            actions=actions,
            metadata=metadata,
            dedupe_key=dedupe_key,
            recipients=recipients,
            occurrence=occurrence,
            reopen_resolved=reopen_resolved,
        )
        return result.item

    async def add_recipients_to_open(
        self,
        *,
        team_id: str,
        dedupe_key: str,
        recipients: list[tuple[str, str | None]],
    ) -> tuple[str, ...]:
        """Deliver an existing open item to more people (an approver leaving a digest), in one transaction.

        Rows that already exist keep their read and archive state. Returns who newly received it.
        """
        unique = _unique_recipients(recipients)
        if not unique:
            return ()
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT id FROM notification_items WHERE team_id=? AND dedupe_key=? AND state='open'",
                    (team_id, dedupe_key),
                ) as cur:
                    row = await cur.fetchone()
                added = await _insert_new_recipients(db, str(row["id"]), unique, now) if row else []
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return tuple(added)

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
        types: Collection[str] | None = None,
    ) -> int:
        """关掉某个主体名下**所有**还开着的通知，返回关掉的条数。

        ``types`` narrows this to some types (a Skill decision closes the submission item, not the
        decision it is about to send).

        `resolve_by_dedupe` 是「一件事一条」的闭环（run 离开 awaiting 关它自己那条）；
        这里是「主体没了」的闭环——定时任务被删除后，它名下的挂起待办与暂停通知都指向一个
        已不存在的 job，深链点进去无 job/run 归属。按 dedupe 逐条关做不到：run 级的 key 带
        run_id，删除时不知道有多少条。
        """
        now = _now()
        sql = (
            "UPDATE notification_items SET state='resolved', resolved_at=?, "
            "resolved_reason=?, updated_at=? WHERE team_id=? AND subject_type=? "
            "AND subject_id=? AND state='open'"
        )
        params: list[Any] = [now, reason, now, team_id, subject_type, subject_id]
        if types is not None:
            type_list = sorted(set(types))
            if not type_list:
                return 0
            sql += f" AND type IN ({','.join(['?'] * len(type_list))})"
            params.extend(type_list)
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(sql, tuple(params))
            await db.commit()
            return int(cur.rowcount or 0)

    async def list_open_required_approval_items(
        self,
        *,
        limit: int = 500,
    ) -> list[NotificationItem]:
        return await self.list_open_items(types=["approval.required"], limit=limit)

    async def list_open_items(
        self,
        *,
        types: Collection[str],
        limit: int = 500,
    ) -> list[NotificationItem]:
        """Open items of the given types, oldest first: the reconciliation sweep's input."""
        type_list = sorted(set(types))
        if not type_list:
            return []
        limit = max(1, min(limit, 5000))
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM notification_items "  # noqa: S608
                f"WHERE type IN ({','.join(['?'] * len(type_list))}) AND state='open' "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                (*type_list, limit),
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

    def _browser_candidate_sql(self, types: Collection[str]) -> tuple[str, list[Any]]:
        """Unread, unarchived, open items of browser-channel types at a priority their type announces."""
        from bowhead_core.notification_types import browser_types_by_priority

        wanted = set(types)
        clauses: list[str] = []
        params: list[Any] = []
        for priority, priority_types in sorted(browser_types_by_priority().items()):
            type_list = sorted(wanted & priority_types)
            if type_list:
                clauses.append(f"(i.priority = ? AND i.type IN ({','.join(['?'] * len(type_list))}))")
                params.extend([priority, *type_list])
        where = (
            "FROM notification_recipients r JOIN notification_items i ON i.id = r.notification_id "  # noqa: S608
            "WHERE r.user_id = ? AND r.archived_at IS NULL AND r.read_at IS NULL AND i.state = 'open' "
            f"AND ({' OR '.join(clauses) or '0'})"
        )
        return where, params

    async def count_recent(self, *, type: str, subject_type: str, subject_id: str, since: str) -> int:  # noqa: A002
        """Items of one type about one subject created since ``since``, in any Team or state (rate limits)."""
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                "SELECT COUNT(*) FROM notification_items WHERE type=? AND subject_type=? AND subject_id=? AND created_at >= ?",
                (type, subject_type, subject_id, since),
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0] if row else 0)

    async def latest_browser_candidate_at(self, *, user_id: str, types: Collection[str]) -> str | None:
        """When the newest item the browser may announce was delivered; None when there is none."""
        if not types:
            return None
        where, type_params = self._browser_candidate_sql(types)
        async with open_shared_ctx(self.db_path) as db, db.execute(f"SELECT MAX(r.delivered_at) {where}", (user_id, *type_params)) as cur:
            row = await cur.fetchone()
        return str(row[0]) if row and row[0] else None

    async def list_browser_candidates(
        self,
        *,
        user_id: str,
        types: Collection[str],
        since: str | None,
        limit: int = 5,
    ) -> list[NotificationForUser]:
        """Items the browser may announce, delivered after ``since`` (newest first, at most ``limit``)."""
        if not types:
            return []
        where, type_params = self._browser_candidate_sql(types)
        params: list[Any] = [user_id, *type_params]
        if since:
            where += " AND r.delivered_at > ?"
            params.append(since)
        params.append(max(1, min(limit, 20)))
        sql = (
            "SELECT i.*, r.user_id AS recipient_user_id, r.role AS recipient_role, "
            f"r.delivered_at, r.read_at, r.archived_at {where} "
            "ORDER BY r.delivered_at DESC, i.id DESC LIMIT ?"
        )
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
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

    # ── Recorded email deliveries (FEATURE_NOTIFICATION_DELIVERY) ─────────────

    async def get_item(self, notification_id: str) -> NotificationItem | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM notification_items WHERE id=?", (notification_id,)) as cur:
                row = await cur.fetchone()
        return _item_row(row) if row else None

    async def get_recipient_state(self, notification_id: str, user_id: str) -> tuple[str | None, str | None] | None:
        """``(read_at, archived_at)`` of one recipient row, or None when the person does not hold the item."""
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                "SELECT read_at, archived_at FROM notification_recipients WHERE notification_id=? AND user_id=?",
                (notification_id, user_id),
            ) as cur,
        ):
            row = await cur.fetchone()
        return (row[0], row[1]) if row else None

    async def lease_due_deliveries(self, *, owner: str, now: str, lease_until: str, limit: int = 20) -> list[NotificationDelivery]:
        """Lease due pending rows (or rows whose lease expired) to one worker, oldest first."""
        limit = max(1, min(limit, 200))
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT id FROM notification_deliveries WHERE status='pending' AND not_before<=? "
                    "AND (lease_expires_at IS NULL OR lease_expires_at<=?) ORDER BY not_before ASC, id ASC LIMIT ?",
                    (now, now, limit),
                ) as cur:
                    ids = [str(row[0]) for row in await cur.fetchall()]
                rows: list[aiosqlite.Row] = []
                for delivery_id in ids:
                    await db.execute(
                        "UPDATE notification_deliveries SET lease_owner=?, lease_expires_at=?, updated_at=? WHERE id=?",
                        (owner, lease_until, now, delivery_id),
                    )
                    async with db.execute("SELECT * FROM notification_deliveries WHERE id=?", (delivery_id,)) as cur:
                        row = await cur.fetchone()
                    if row is not None:
                        rows.append(row)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return [_delivery_row(row) for row in rows]

    async def settle_delivery(
        self,
        *,
        delivery_id: str,
        owner: str,
        status: DeliveryStatus,
        outcome_code: str | None = None,
        diagnostic_ref: str | None = None,
        retry_at: str | None = None,
    ) -> bool:
        """Record one attempt's outcome, only while this worker still holds the lease.

        ``retry_at`` keeps the row pending for another attempt at that time.
        """
        now = _now()
        next_status = "pending" if retry_at else status
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(
                "UPDATE notification_deliveries SET status=?, attempts=attempts+?, not_before=COALESCE(?, not_before), "
                "outcome_code=?, diagnostic_ref=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE id=? AND lease_owner=? AND status='pending'",
                (next_status, 0 if status == "skipped" else 1, retry_at, outcome_code, diagnostic_ref, now, delivery_id, owner),
            )
            await db.commit()
        return int(cur.rowcount or 0) == 1

    async def deliveries_for_dedupe_keys(self, dedupe_keys: Collection[str]) -> list[NotificationDelivery]:
        """Delivery rows of the items with these keys, in any Team (a scheduled run's result and alert)."""
        keys = sorted(set(dedupe_keys))
        if not keys:
            return []
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT d.*, i.dedupe_key AS item_dedupe_key FROM notification_deliveries d "  # noqa: S608
                "JOIN notification_items i ON i.id = d.notification_id "
                f"WHERE i.dedupe_key IN ({','.join(['?'] * len(keys))}) ORDER BY d.created_at ASC, d.id ASC",
                tuple(keys),
            ) as cur:
                rows = await cur.fetchall()
        return [_delivery_row(row) for row in rows]

    async def prune_expired(self, *, older_than: str, limit: int = 500) -> dict[str, int]:
        """Delete notification history older than the retention cutoff.

        Eligible, each in batches of ``limit``:

        - recipient rows archived before the cutoff;
        - recipient rows read before the cutoff, unless their item is an open action item (it still
          asks for something even after being read);
        - items created before the cutoff that no longer have any recipient, unless the item is an
          open action item (an access request folded into a digest is open with no recipient yet).

        Unread rows and open action items are never eligible.
        """
        limit = max(1, min(limit, 5000))
        action_types = sorted(ACTION_REQUIRED_TYPES)
        action_placeholders = ",".join(["?"] * len(action_types))
        async with open_shared_ctx(self.db_path) as db:
            archived = await db.execute(
                "DELETE FROM notification_recipients "
                "WHERE rowid IN ("
                "SELECT rowid FROM notification_recipients "
                "WHERE archived_at IS NOT NULL AND archived_at < ? "
                "ORDER BY archived_at ASC LIMIT ?"
                ")",
                (older_than, limit),
            )
            read = await db.execute(
                "DELETE FROM notification_recipients "  # noqa: S608
                "WHERE rowid IN ("
                "SELECT r.rowid FROM notification_recipients r "
                "JOIN notification_items i ON i.id = r.notification_id "
                "WHERE r.archived_at IS NULL AND r.read_at IS NOT NULL AND r.read_at < ? "
                f"AND NOT (i.state = 'open' AND i.type IN ({action_placeholders})) "
                "ORDER BY r.read_at ASC LIMIT ?"
                ")",
                (older_than, *action_types, limit),
            )
            items = await db.execute(
                "DELETE FROM notification_items "  # noqa: S608
                "WHERE id IN ("
                "SELECT i.id FROM notification_items i "
                "WHERE i.created_at < ? "
                f"AND NOT (i.state = 'open' AND i.type IN ({action_placeholders})) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM notification_recipients r WHERE r.notification_id = i.id"
                ") "
                "LIMIT ?"
                ")",
                (older_than, *action_types, limit),
            )
            await db.commit()
        return {
            "recipients_deleted": max(0, archived.rowcount) + max(0, read.rowcount),
            "items_deleted": max(0, items.rowcount),
        }


def _after(now: str, seconds: int) -> str:
    return (datetime.fromisoformat(now) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


async def _schedule_deliveries(
    db: aiosqlite.Connection,
    notification_id: str,
    user_ids: list[str],
    channel: str,
    not_before: str,
    now: str,
) -> None:
    """Schedule one email per person; a new occurrence of an earlier one starts over as pending."""
    await db.executemany(
        "INSERT INTO notification_deliveries(id,notification_id,user_id,channel,status,not_before,attempts,created_at,updated_at) "
        "VALUES (?,?,?,?,'pending',?,0,?,?) "
        "ON CONFLICT(notification_id,user_id,channel) DO UPDATE SET status='pending', not_before=excluded.not_before, "
        "attempts=0, outcome_code=NULL, diagnostic_ref=NULL, lease_owner=NULL, lease_expires_at=NULL, updated_at=excluded.updated_at",
        [(f"ndl_{uuid4().hex[:16]}", notification_id, uid, channel, not_before, now, now) for uid in user_ids],
    )


def _delivery_row(row: aiosqlite.Row) -> NotificationDelivery:
    keys = row.keys()
    return NotificationDelivery(
        id=row["id"],
        notification_id=row["notification_id"],
        user_id=row["user_id"],
        channel=row["channel"],
        status=row["status"],
        not_before=row["not_before"],
        attempts=int(row["attempts"]),
        outcome_code=row["outcome_code"],
        diagnostic_ref=row["diagnostic_ref"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        item_dedupe_key=row["item_dedupe_key"] if "item_dedupe_key" in keys else None,
    )


def _unique_recipients(recipients: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    """One row per user (the first role listed wins), in a stable order."""
    by_user: dict[str, str | None] = {}
    for uid, role in recipients:
        if uid and uid not in by_user:
            by_user[uid] = role
    return sorted(by_user.items(), key=lambda pair: pair[0])


async def _insert_new_recipients(
    db: aiosqlite.Connection,
    notification_id: str,
    recipients: list[tuple[str, str | None]],
    now: str,
) -> list[str]:
    """Add recipients not yet on the item, unread. Existing rows are untouched. Returns the added user ids."""
    if not recipients:
        return []
    async with db.execute(
        "SELECT user_id FROM notification_recipients WHERE notification_id=?",
        (notification_id,),
    ) as cur:
        present = {str(row[0]) for row in await cur.fetchall()}
    new = [(uid, role) for uid, role in recipients if uid not in present]
    if new:
        await db.executemany(
            "INSERT INTO notification_recipients(notification_id,user_id,role,delivered_at) VALUES (?,?,?,?)",
            [(notification_id, uid, role, now) for uid, role in new],
        )
    return [uid for uid, _role in new]


async def _reset_recipients(
    db: aiosqlite.Connection,
    notification_id: str,
    recipients: list[tuple[str, str | None]],
    now: str,
) -> None:
    """A new occurrence: exactly the listed recipients hold the item, unread and unarchived."""
    if not recipients:
        return
    listed = [uid for uid, _role in recipients]
    await db.execute(
        f"DELETE FROM notification_recipients WHERE notification_id=? AND user_id NOT IN ({','.join(['?'] * len(listed))})",  # noqa: S608
        (notification_id, *listed),
    )
    await db.executemany(
        "INSERT INTO notification_recipients(notification_id,user_id,role,delivered_at) VALUES (?,?,?,?) "
        "ON CONFLICT(notification_id,user_id) DO UPDATE SET "
        "role=excluded.role,delivered_at=excluded.delivered_at,read_at=NULL,archived_at=NULL",
        [(notification_id, uid, role, now) for uid, role in recipients],
    )


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
