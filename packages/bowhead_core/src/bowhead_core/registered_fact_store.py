"""RegisteredFact persistence — Sprint δ.

Stores user-corrected / learned facts that override LLM standard knowledge:
- data_correction: 用户改具体数字 (e.g., "Q1 不是 12 亿是 13")
- formula_definition: 用户给公式 (e.g., "EBITDA 我们不算外包")
- concept_definition: 用户给业务概念 (e.g., "高潜客户 = ...")
- preference: 行为偏好 (e.g., "默认按产品线分")

Schema (per tech-design §1.3 + round 2 fixes):
- registered_facts: current state, UNIQUE per (workspace, project, user, fact_key)
- registered_facts_history: append-only audit (UPSERT 时 INSERT 老 row)

UPSERT atomicity: BEGIN → archive old → UPDATE/INSERT new → COMMIT (single transaction).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class RegisteredFact:
    """A user-corrected / learned fact persisted across sessions."""

    fact_id: str
    workspace_id: str
    fact_key: str
    fact_type: str  # data_correction / formula_definition / concept_definition / preference
    value_json: str
    source: str  # user_directive / kb / hitl_resolution / auto_learner
    is_user_corrected: bool = False
    project_id: str | None = None
    user_id: str | None = None
    confidence: int | None = None
    created_at: str = ""
    updated_at: str = ""
    source_session_id: str | None = None
    source_turn_id: str | None = None


def _now_iso() -> str:
    """ISO 8601 UTC timestamp (matches session_store pattern)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id() -> str:
    return f"fact_{uuid.uuid4().hex[:16]}"


def _new_history_id() -> str:
    return f"fhist_{uuid.uuid4().hex[:16]}"


# Hard cap on persisted value_json (Sec P1-11): an LLM-emitted value_json could
# arbitrarily expand under prompt injection. Storage is unbounded by schema, but
# we cap at 4000 chars at write boundary to prevent DB bloat + adversary-chosen
# retention. Render-time truncation (provenance_prompts._FACT_VALUE_TRUNC_CHARS=200)
# is separate; storage cap enforces the absolute limit.
MAX_VALUE_JSON_CHARS = 4000


def _cap_value_json(value_json: str) -> str:
    """Truncate value_json to MAX_VALUE_JSON_CHARS — bounded persistence."""
    if len(value_json) <= MAX_VALUE_JSON_CHARS:
        return value_json
    logger.warning(
        "delta.fact_value_truncated original_len=%d cap=%d",
        len(value_json),
        MAX_VALUE_JSON_CHARS,
    )
    return value_json[:MAX_VALUE_JSON_CHARS]


class RegisteredFactStore:
    """SQLite-backed CRUD + UPSERT + history archive for RegisteredFact.

    Schema migration handled inline in SessionStore.initialize() per η pattern.
    Tests rely on caller calling SessionStore.initialize() before instantiating
    this store.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def upsert(
        self,
        *,
        workspace_id: str,
        fact_key: str,
        fact_type: str,
        value_json: str,
        source: str,
        is_user_corrected: bool = False,
        project_id: str | None = None,
        user_id: str | None = None,
        confidence: int | None = None,
        source_session_id: str | None = None,
        source_turn_id: str | None = None,
    ) -> RegisteredFact:
        """Upsert atomic — archive old (if any) → write new in single transaction.

        UPSERT 必跨表原子: 老 row INSERT 进 history → DELETE/UPDATE current row →
        新 row insert. 跑在 single BEGIN IMMEDIATE/COMMIT.

        BEGIN IMMEDIATE (review P0-2): default deferred BEGIN takes a read lock
        only; two concurrent upserts on same key both see ``old_row=None`` and
        race on INSERT — second hits UNIQUE constraint and rolls back the
        wrong half. IMMEDIATE acquires write lock up front so the second
        coroutine waits until the first commits.
        """
        # Bounded value_json (Sec P1-11) — prevent adversary-chosen storage.
        value_json = _cap_value_json(value_json)
        now = _now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # δ-#8.5 round-2 backend P1: aiosqlite default busy_timeout=0
            # means the second concurrent BEGIN IMMEDIATE on the same workspace
            # raises "database is locked" instead of waiting. 5s is generous —
            # any wait longer indicates a deeper problem (caller times out).
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "SELECT * FROM registered_facts WHERE workspace_id=? AND "
                    "COALESCE(project_id,'')=COALESCE(?,'') AND "
                    "COALESCE(user_id,'')=COALESCE(?,'') AND fact_key=?",
                    (workspace_id, project_id, user_id, fact_key),
                )
                old_row = await cur.fetchone()

                if old_row is not None:
                    # Archive to history
                    await db.execute(
                        "INSERT INTO registered_facts_history "
                        "(history_id, fact_id, workspace_id, fact_key, fact_type, "
                        " value_json, source, superseded_at, "
                        " superseded_by_session_id, superseded_by_turn_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            _new_history_id(),
                            old_row["fact_id"],
                            old_row["workspace_id"],
                            old_row["fact_key"],
                            old_row["fact_type"],
                            old_row["value_json"],
                            old_row["source"],
                            now,
                            source_session_id,
                            source_turn_id,
                        ),
                    )
                    fact_id = old_row["fact_id"]
                    await db.execute(
                        "UPDATE registered_facts SET fact_type=?, value_json=?, "
                        "source=?, is_user_corrected=?, confidence=?, updated_at=?, "
                        "source_session_id=?, source_turn_id=? "
                        "WHERE fact_id=?",
                        (
                            fact_type,
                            value_json,
                            source,
                            1 if is_user_corrected else 0,
                            confidence,
                            now,
                            source_session_id,
                            source_turn_id,
                            fact_id,
                        ),
                    )
                    created_at = old_row["created_at"]
                else:
                    fact_id = _new_id()
                    created_at = now
                    await db.execute(
                        "INSERT INTO registered_facts "
                        "(fact_id, workspace_id, project_id, user_id, fact_key, "
                        " fact_type, value_json, source, is_user_corrected, confidence, "
                        " created_at, updated_at, source_session_id, source_turn_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            fact_id,
                            workspace_id,
                            project_id,
                            user_id,
                            fact_key,
                            fact_type,
                            value_json,
                            source,
                            1 if is_user_corrected else 0,
                            confidence,
                            created_at,
                            now,
                            source_session_id,
                            source_turn_id,
                        ),
                    )

                await db.commit()
            except Exception:
                await db.rollback()
                raise

        logger.info(
            "delta.fact_upsert workspace=%s key=%s type=%s corrected=%s",
            workspace_id,
            fact_key,
            fact_type,
            is_user_corrected,
        )
        return RegisteredFact(
            fact_id=fact_id,
            workspace_id=workspace_id,
            project_id=project_id,
            user_id=user_id,
            fact_key=fact_key,
            fact_type=fact_type,
            value_json=value_json,
            source=source,
            is_user_corrected=is_user_corrected,
            confidence=confidence,
            created_at=created_at,
            updated_at=now,
            source_session_id=source_session_id,
            source_turn_id=source_turn_id,
        )

    async def get(
        self,
        *,
        workspace_id: str,
        fact_key: str,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> RegisteredFact | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM registered_facts WHERE workspace_id=? AND "
                "COALESCE(project_id,'')=COALESCE(?,'') AND "
                "COALESCE(user_id,'')=COALESCE(?,'') AND fact_key=?",
                (workspace_id, project_id, user_id, fact_key),
            )
            row = await cur.fetchone()
        return _row_to_fact(row) if row else None

    async def list_active(
        self,
        *,
        workspace_id: str,
        project_id: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[RegisteredFact]:
        """Facts visible to ``(workspace, project, user)``.

        Visibility = {workspace-shared facts} ∪ {my-project facts} ∪ {my facts}.
        Workspace-shared = ``project_id IS NULL AND user_id IS NULL``.
        Never returns *other users'* personal facts or *other projects'* facts —
        that would leak preferences across tenants and let A's prompt-injected
        fact land in B's system prompt (review P0-fix).
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql, params = self._scope_filter(workspace_id, project_id, user_id)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            cur = await db.execute(sql, tuple(params))
            rows = await cur.fetchall()
        return [_row_to_fact(r) for r in rows]

    async def list_by_key_keywords(
        self,
        *,
        workspace_id: str,
        keywords: list[str],
        project_id: str | None = None,
        user_id: str | None = None,
        limit: int = 10,
    ) -> list[RegisteredFact]:
        """Keyword match on fact_key (lowercase + substring) for pre-LLM injection.

        Per round 2 CTO-P0-4: pre-LLM fact retrieval keyed by query terms,
        not just recency. Same scope-leak guard as ``list_active`` — caller
        gets shared+own facts only.
        """
        if not keywords:
            return []
        normalized = [k.lower().strip() for k in keywords if k.strip()]
        if not normalized:
            return []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            base_sql, params = self._scope_filter(workspace_id, project_id, user_id)
            like_clauses = " OR ".join(["LOWER(fact_key) LIKE ?"] * len(normalized))
            sql = base_sql + f" AND ({like_clauses})"
            for kw in normalized:
                params.append(f"%{kw}%")
            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            cur = await db.execute(sql, tuple(params))
            rows = await cur.fetchall()
        return [_row_to_fact(r) for r in rows]

    @staticmethod
    def _scope_filter(
        workspace_id: str,
        project_id: str | None,
        user_id: str | None,
    ) -> tuple[str, list[Any]]:
        """Build SELECT + params with scope visibility semantics.

        - If both scopes None: return only workspace-shared facts (both NULL).
        - project_id given, user_id None: workspace-shared + my-project (project_id match,
          user_id IS NULL).
        - both given: + my-user (user_id match) overlay.

        Caller appends ORDER / LIMIT.
        """
        # workspace-shared facts (ALWAYS visible)
        clauses = ["(project_id IS NULL AND user_id IS NULL)"]
        params: list[Any] = []
        if project_id is not None:
            clauses.append("(project_id = ? AND user_id IS NULL)")
            params.append(project_id)
        if user_id is not None:
            # User-personal facts apply per-user; project_id may be NULL or match
            clauses.append("(user_id = ?)")
            params.append(user_id)
        sql = "SELECT * FROM registered_facts WHERE workspace_id=? AND (" + " OR ".join(clauses) + ")"
        return sql, [workspace_id, *params]

    async def delete(
        self,
        *,
        workspace_id: str,
        fact_key: str,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM registered_facts WHERE workspace_id=? AND "
                "COALESCE(project_id,'')=COALESCE(?,'') AND "
                "COALESCE(user_id,'')=COALESCE(?,'') AND fact_key=?",
                (workspace_id, project_id, user_id, fact_key),
            )
            await db.commit()
            return cur.rowcount > 0

    async def list_history(
        self,
        *,
        workspace_id: str,
        fact_key: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return audit trail for given fact_key (oldest-first)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM registered_facts_history WHERE workspace_id=? AND fact_key=? ORDER BY superseded_at ASC LIMIT ?",
                (workspace_id, fact_key, limit),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]


def _row_to_fact(row: aiosqlite.Row) -> RegisteredFact:
    return RegisteredFact(
        fact_id=row["fact_id"],
        workspace_id=row["workspace_id"],
        project_id=row["project_id"],
        user_id=row["user_id"],
        fact_key=row["fact_key"],
        fact_type=row["fact_type"],
        value_json=row["value_json"],
        source=row["source"],
        is_user_corrected=bool(row["is_user_corrected"]),
        confidence=row["confidence"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        source_session_id=row["source_session_id"],
        source_turn_id=row["source_turn_id"],
    )
