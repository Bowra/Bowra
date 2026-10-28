"""kb-matrix-sharing — 组织模板库注册表 store（`kb_team_templates`）。

一个空间里的 `kind=template` 列组模板可**发布**到团队库，同团队其他空间成员
**发现 + 复制**一份进自己空间（copy 语义，定格快照，数据永不跨空间）。

设计权威 = `docs/sprints/kb-matrix-sharing/tech-design.md` §3。形状镜像
`user_store` 的 `team_skill`「team 表 + CRUD」范式（team_id 键、`BEGIN IMMEDIATE`
建、纯 team_id list、IDOR-safe by construction），但作为独立 KB sibling DB
（`kb_team_templates.db`）、`team_id` 是普通索引列（跨 DB 无法 FK teams）。

不变量（Q2 铁律）：条目 `schema_json` 只含 fields/name/description——**永无**
records/file_ids/cell/出处/edited_by_user。发布端在路由层硬拒 `kind != template`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_team_templates (
    entry_id             TEXT PRIMARY KEY,          -- ot_<uuid12>
    team_id               TEXT NOT NULL,             -- 团队作用域（普通索引列，非 FK）
    source_doc_type_id   TEXT NOT NULL,             -- 幂等 republish + 源删级联的锚
    source_workspace_id  TEXT NOT NULL,             -- attribution（源空间名读时解析）
    name                 TEXT NOT NULL,             -- 发布时刻的模板名快照
    description          TEXT NOT NULL DEFAULT '',
    schema_json          TEXT NOT NULL DEFAULT '{"fields": []}',  -- 定格 fields 快照
    field_count          INTEGER NOT NULL DEFAULT 0,
    published_by         TEXT NOT NULL,
    published_at         TEXT NOT NULL,
    updated_at           TEXT NOT NULL,             -- republish 刷新（published_at 保原署名）
    revoked_at           TEXT                       -- NULL=live；置值=已撤销（软撤销，保幂等锚+血缘）
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_kot_source ON kb_team_templates(source_doc_type_id);
CREATE INDEX IF NOT EXISTS idx_kot_team_live ON kb_team_templates(team_id) WHERE revoked_at IS NULL;
"""


@dataclass
class TeamTemplateRow:
    """一条组织模板库条目（定格 schema 快照）。"""

    entry_id: str
    team_id: str
    source_doc_type_id: str
    source_workspace_id: str
    name: str
    description: str
    schema_json: str
    field_count: int
    published_by: str
    published_at: str
    updated_at: str
    revoked_at: str | None = None


class KbTeamTemplateStore:
    """SQLite-backed `kb_team_templates` store. Team-scoped.

    所有 list/get/revoke 方法**store 层 team_id-keyed**（IDOR-safe by construction，
    对齐 `user_store.get_team_skill`/`list_team_skills`）。`revoke_by_source` 是源删
    级联专用（不带 team，由 `delete_doc_type` 钩子对已授权的删除调用）。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def initialize(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            for stmt in _SCHEMA.split(";"):
                stmt = stmt.strip()
                if stmt:
                    await db.execute(stmt)
            await db.commit()

    async def publish(
        self,
        *,
        team_id: str,
        source_doc_type_id: str,
        source_workspace_id: str,
        name: str,
        description: str,
        schema_json: str,
        field_count: int,
        published_by: str,
    ) -> tuple[TeamTemplateRow, bool]:
        """发布/重发布一个模板到组织库（幂等 per source_doc_type_id）。

        返回 (条目, was_republish)。`BEGIN IMMEDIATE` 让存在性判定 + upsert 对并发
        原子（镜像 `create_team_skill`）。重发布刷新 schema/name/desc/field_count/
        updated_at 并 revoked_at=NULL（复活软撤销行），**保留原 published_by/
        published_at**（归属=首次发布者，tech-design §3.3）。
        """
        now = _now()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT entry_id FROM kb_team_templates WHERE source_doc_type_id=?",
                (source_doc_type_id,),
            ) as cur:
                existing = await cur.fetchone()
            if existing is not None:
                entry_id = existing["entry_id"]
                await db.execute(
                    "UPDATE kb_team_templates SET name=?, description=?, schema_json=?, "
                    "field_count=?, updated_at=?, revoked_at=NULL WHERE source_doc_type_id=?",
                    (name, description, schema_json, field_count, now, source_doc_type_id),
                )
                was_republish = True
            else:
                entry_id = f"ot_{uuid4().hex[:12]}"
                await db.execute(
                    "INSERT INTO kb_team_templates"
                    "(entry_id, team_id, source_doc_type_id, source_workspace_id, name, "
                    " description, schema_json, field_count, published_by, published_at, "
                    " updated_at, revoked_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        entry_id,
                        team_id,
                        source_doc_type_id,
                        source_workspace_id,
                        name,
                        description,
                        schema_json,
                        field_count,
                        published_by,
                        now,
                        now,
                    ),
                )
                was_republish = False
            await db.commit()
            async with db.execute(
                "SELECT * FROM kb_team_templates WHERE entry_id=?",
                (entry_id,),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None  # noqa: S101 — just inserted/updated within the txn
        return _row_to_team_template(row), was_republish

    async def list_live(self, team_id: str) -> list[TeamTemplateRow]:
        """列出组织内所有未撤销条目（纯 team_id 过滤，IDOR-safe）。"""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_team_templates WHERE team_id=? AND revoked_at IS NULL ORDER BY published_at DESC",
                (team_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_team_template(r) for r in rows]

    async def get(self, entry_id: str, *, team_id: str) -> TeamTemplateRow | None:
        """team-keyed 取单条（跨组织 entry → None → 调用方统一 404，堵枚举 oracle）。"""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_team_templates WHERE entry_id=? AND team_id=?",
                (entry_id, team_id),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_team_template(row) if row else None

    async def revoke(self, entry_id: str, *, team_id: str) -> bool:
        """软撤销（team-keyed）。已撤销的 no-op（rowcount 0）。"""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE kb_team_templates SET revoked_at=? WHERE entry_id=? AND team_id=? AND revoked_at IS NULL",
                (_now(), entry_id, team_id),
            )
            await db.commit()
            changed = cur.rowcount
        return changed > 0

    async def revoke_by_source(self, source_doc_type_id: str) -> bool:
        """源删级联专用：撤销某源 doc_type 的库条目（best-effort，无 team 参数——
        由 `delete_doc_type` 钩子对已授权的删除调用，见 tech-design §5.3）。"""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE kb_team_templates SET revoked_at=? WHERE source_doc_type_id=? AND revoked_at IS NULL",
                (_now(), source_doc_type_id),
            )
            await db.commit()
            changed = cur.rowcount
        return changed > 0


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _row_to_team_template(row: aiosqlite.Row) -> TeamTemplateRow:
    return TeamTemplateRow(
        entry_id=row["entry_id"],
        team_id=row["team_id"],
        source_doc_type_id=row["source_doc_type_id"],
        source_workspace_id=row["source_workspace_id"],
        name=row["name"],
        description=row["description"] or "",
        schema_json=row["schema_json"] or '{"fields": []}',
        field_count=int(row["field_count"] or 0),
        published_by=row["published_by"],
        published_at=row["published_at"],
        updated_at=row["updated_at"],
        revoked_at=row["revoked_at"],
    )
