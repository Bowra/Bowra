"""Approval store - 写操作审批队列.

production:
    - approvals(id, turn_id, tool_name, arguments_json, reason, workspace_id, member_uid,
                user_id, status, decided_by, decided_at, execution_ok, execution_result,
                execution_error, created_at, updated_at)
    - status: pending | approved | rejected | executed | execution_failed | expired
    - 审批完 → 真正执行 → 写 execution_* 字段
    - 所有字段用 JSON 序列化(arguments/result)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id                TEXT PRIMARY KEY,
    turn_id           TEXT NOT NULL,
    session_id        TEXT,
    workspace_id      TEXT NOT NULL,
    member_uid        TEXT NOT NULL,
    -- 批2/c9 (PRD §3.9): 请求时点的北极九章工作区快照——重放 pin 这个，
    -- 不用审批人当前偏好（B 批 A 的报告绝不悄悄换数据域）。NULL=非 BI/pre-批2 行。
    datarc_workspace_id TEXT,
    user_id           TEXT NOT NULL,
    tool_name         TEXT NOT NULL,
    arguments_json    TEXT NOT NULL,
    request_key       TEXT,
    arguments_digest  TEXT,
    reason            TEXT,
    risk_level        TEXT NOT NULL DEFAULT 'medium',
    status            TEXT NOT NULL DEFAULT 'pending',
    decided_by        TEXT,
    decided_at        TEXT,
    execution_ok      INTEGER,                   -- 0/1 NULL=未执行
    execution_result  TEXT,                      -- JSON
    execution_error   TEXT,
    executed_at       TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appr_status ON approvals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_appr_turn ON approvals(turn_id);
-- NOTE: idx_appr_request_key is intentionally NOT created here. It is created in
-- _migrate(), AFTER the request_key column is ensured. On a pre-existing approvals
-- table (created before the request_key column existed), executescript(SCHEMA) runs
-- this block before _migrate() can ADD COLUMN, so creating the index here raises
-- "no such column: request_key" and crashes gateway startup.
"""


ApprovalStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "executed",
    "execution_failed",
    "expired",
]


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _arguments_digest(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(arguments).encode("utf-8")).hexdigest()


async def _has_protocol_projection_tables(
    db: aiosqlite.Connection,
) -> bool:
    async with db.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE type='table' AND name IN ('turns','turn_terminal_receipts')
        """
    ) as cursor:
        row = await cursor.fetchone()
    return bool(row is not None and int(row[0]) == 2)


@dataclass
class Approval:
    id: str
    turn_id: str
    session_id: str | None
    workspace_id: str
    member_uid: str
    user_id: str
    tool_name: str
    arguments: dict[str, Any]
    request_key: str | None
    arguments_digest: str | None
    reason: str | None
    risk_level: str
    status: str
    decided_by: str | None
    decided_at: str | None
    execution_ok: bool | None
    execution_result: Any
    execution_error: str | None
    executed_at: str | None
    created_at: str
    updated_at: str
    # Batch 2/c9: request-time Datarc workspace snapshot (pinned for replay; None=non-BI/existing row).
    datarc_workspace_id: str | None = None


@dataclass
class ApprovalStore:
    db_path: str

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with open_shared_ctx(self.db_path) as db:
            await db.executescript(SCHEMA)
            await self._migrate(db)
            await db.commit()

    async def list_v1_expiry_candidates(
        self,
        *,
        ttl_hours: float = 24.0,
        limit: int = 100,
    ) -> list[Approval]:
        """Read due v1 approvals without bypassing the decision protocol."""

        if ttl_hours <= 0 or not 1 <= limit <= 100:
            raise ValueError("approval expiry bounds are invalid")
        cutoff = (datetime.now(tz=UTC) - timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT a.* FROM approvals a
                JOIN turns t ON t.id=a.turn_id
                JOIN turn_terminal_receipts r
                  ON r.receipt_id=t.current_terminal_receipt_id
                WHERE a.status='pending' AND a.created_at<?
                  AND t.terminal_protocol_version=1
                  AND r.status='approval_pending'
                ORDER BY a.created_at,a.id LIMIT ?
                """,
                (cutoff, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row(row) for row in rows]

    async def list_v1_pending_candidates(
        self,
        *,
        limit: int = 100,
        after_id: str | None = None,
    ) -> list[Approval]:
        """Read current paused approvals for authorization revocation."""

        if not 1 <= limit <= 100 or (after_id is not None and (not isinstance(after_id, str) or not after_id)):
            raise ValueError("approval pending bounds are invalid")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT a.* FROM approvals a
                JOIN turns t ON t.id=a.turn_id
                JOIN turn_terminal_receipts r
                  ON r.receipt_id=t.current_terminal_receipt_id
                WHERE a.status='pending'
                  AND t.terminal_protocol_version=1
                  AND r.status='approval_pending'
                  AND (? IS NULL OR a.id>?)
                ORDER BY a.id LIMIT ?
                """,
                (after_id, after_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row(row) for row in rows]

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        async with db.execute("PRAGMA table_info(approvals)") as cur:
            rows = await cur.fetchall()
        cols = {row[1] for row in rows}
        if "request_key" not in cols:
            await db.execute("ALTER TABLE approvals ADD COLUMN request_key TEXT")
        if "arguments_digest" not in cols:
            await db.execute("ALTER TABLE approvals ADD COLUMN arguments_digest TEXT")
        if "datarc_workspace_id" not in cols:
            # Batch 2/c9: workspace snapshot column (pinned for replay).
            await db.execute("ALTER TABLE approvals ADD COLUMN datarc_workspace_id TEXT")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_appr_request_key ON approvals(request_key) WHERE request_key IS NOT NULL")

    async def enqueue(
        self,
        *,
        turn_id: str,
        session_id: str | None,
        workspace_id: str,
        member_uid: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        reason: str | None,
        risk_level: str = "medium",
        datarc_workspace_id: str | None = None,
    ) -> Approval:
        return await self._insert(
            request_key=None,
            turn_id=turn_id,
            session_id=session_id,
            workspace_id=workspace_id,
            member_uid=member_uid,
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
            reason=reason,
            risk_level=risk_level,
            datarc_workspace_id=datarc_workspace_id,
        )

    async def enqueue_once(
        self,
        *,
        request_key: str,
        turn_id: str,
        session_id: str | None,
        workspace_id: str,
        member_uid: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        reason: str | None,
        risk_level: str = "medium",
        datarc_workspace_id: str | None = None,
    ) -> tuple[Approval, bool]:
        existing = await self.get_by_request_key(request_key)
        if existing is not None:
            return existing, False
        try:
            approval = await self._insert(
                request_key=request_key,
                turn_id=turn_id,
                session_id=session_id,
                workspace_id=workspace_id,
                member_uid=member_uid,
                user_id=user_id,
                tool_name=tool_name,
                arguments=arguments,
                reason=reason,
                risk_level=risk_level,
                datarc_workspace_id=datarc_workspace_id,
            )
            return approval, True
        except aiosqlite.IntegrityError:
            existing = await self.get_by_request_key(request_key)
            if existing is not None:
                return existing, False
            raise

    async def _insert(
        self,
        *,
        request_key: str | None,
        turn_id: str,
        session_id: str | None,
        workspace_id: str,
        member_uid: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        reason: str | None,
        risk_level: str,
        datarc_workspace_id: str | None = None,
    ) -> Approval:
        aid = f"apr_{uuid4().hex[:12]}"
        now = _now()
        arg_digest = _arguments_digest(arguments)
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "INSERT INTO approvals(id, turn_id, session_id, workspace_id, member_uid, user_id, "
                "tool_name, arguments_json, request_key, arguments_digest, reason, "
                "risk_level, datarc_workspace_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    aid,
                    turn_id,
                    session_id,
                    workspace_id,
                    member_uid,
                    user_id,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False),
                    request_key,
                    arg_digest,
                    reason,
                    risk_level,
                    datarc_workspace_id,
                    now,
                    now,
                ),
            )
            await db.commit()
        return Approval(
            id=aid,
            turn_id=turn_id,
            session_id=session_id,
            workspace_id=workspace_id,
            member_uid=member_uid,
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
            request_key=request_key,
            arguments_digest=arg_digest,
            reason=reason,
            risk_level=risk_level,
            status="pending",
            decided_by=None,
            decided_at=None,
            execution_ok=None,
            execution_result=None,
            execution_error=None,
            executed_at=None,
            created_at=now,
            updated_at=now,
            datarc_workspace_id=datarc_workspace_id,
        )

    async def get(self, approval_id: str) -> Approval | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM approvals WHERE id=?",
                (approval_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row(row) if row else None

    async def get_by_request_key(self, request_key: str) -> Approval | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM approvals WHERE request_key=?",
                (request_key,),
            ) as cur:
                row = await cur.fetchone()
        return _row(row) if row else None

    async def list_pending(
        self,
        *,
        workspace_id: str | None = None,
        limit: int = 50,
    ) -> list[Approval]:
        """Pending administrator approvals."""
        params: list = []
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            protocol_tables = await _has_protocol_projection_tables(db)
            sql = (
                """
                SELECT a.* FROM approvals a
                LEFT JOIN turns t ON t.id=a.turn_id
                LEFT JOIN turn_terminal_receipts r
                  ON r.receipt_id=t.current_terminal_receipt_id
                WHERE a.status='pending'
                  AND (
                    COALESCE(t.terminal_protocol_version,0)<>1
                    OR r.status='approval_pending'
                  )
                """
                if protocol_tables
                else "SELECT a.* FROM approvals a WHERE a.status='pending'"
            )
            if workspace_id:
                sql += " AND a.workspace_id=?"
                params.append(workspace_id)
            sql += " ORDER BY a.created_at DESC LIMIT ?"
            params.append(limit)
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        return [_row(r) for r in rows]

    async def list_recent(self, *, limit: int = 50) -> list[Approval]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row(r) for r in rows]

    async def set_decision(
        self,
        *,
        approval_id: str,
        status: ApprovalStatus,
        decided_by: str,
    ) -> Approval | None:
        approval, _transitioned = await self.set_decision_once(
            approval_id=approval_id,
            status=status,
            decided_by=decided_by,
        )
        return approval

    async def set_decision_once(
        self,
        *,
        approval_id: str,
        status: ApprovalStatus,
        decided_by: str,
    ) -> tuple[Approval | None, bool]:
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(
                "UPDATE approvals SET status=?, decided_by=?, decided_at=?, updated_at=? WHERE id=? AND status='pending'",
                (status, decided_by, now, now, approval_id),
            )
            await db.commit()
        approval = await self.get(approval_id)
        return approval, cur.rowcount == 1

    async def record_execution(
        self,
        *,
        approval_id: str,
        ok: bool,
        result: Any,
        error: str | None,
    ) -> Approval | None:
        now = _now()
        status: ApprovalStatus = "executed" if ok else "execution_failed"
        result_json = _canonical_json(result) if ok else None
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                """
                UPDATE approvals
                SET status=?,execution_ok=?,execution_result=?,execution_error=?,
                    executed_at=?,updated_at=?
                WHERE id=? AND status='approved' AND execution_ok IS NULL
                """,
                (status, 1 if ok else 0, result_json, error, now, now, approval_id),
            )
            if cur.rowcount != 1:
                async with db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)) as existing_cur:
                    existing = await existing_cur.fetchone()
                if existing is None:
                    await db.rollback()
                    return None
                same = (
                    existing["status"] == status
                    and existing["execution_ok"] == (1 if ok else 0)
                    and existing["execution_result"] == result_json
                    and existing["execution_error"] == error
                )
                if not same:
                    await db.rollback()
                    raise RuntimeError("approval execution projection conflict")
            await db.commit()
        return await self.get(approval_id)


def _row(row: aiosqlite.Row) -> Approval:
    columns = set(row.keys())
    return Approval(
        id=row["id"],
        turn_id=row["turn_id"],
        session_id=row["session_id"],
        workspace_id=row["workspace_id"],
        member_uid=row["member_uid"],
        user_id=row["user_id"],
        tool_name=row["tool_name"],
        arguments=json.loads(row["arguments_json"] or "{}"),
        request_key=row["request_key"] if "request_key" in columns else None,
        arguments_digest=row["arguments_digest"] if "arguments_digest" in columns else None,
        reason=row["reason"],
        risk_level=row["risk_level"],
        status=row["status"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        execution_ok=(None if row["execution_ok"] is None else bool(row["execution_ok"])),
        execution_result=(json.loads(row["execution_result"]) if row["execution_result"] else None),
        execution_error=row["execution_error"],
        executed_at=row["executed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        datarc_workspace_id=(row["datarc_workspace_id"] if "datarc_workspace_id" in columns else None),
    )
