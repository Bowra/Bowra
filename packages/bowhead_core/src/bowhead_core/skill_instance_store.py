"""Store per-user Skill enablement, overrides and ownership.

Built-in Skills are enabled without a row until a user overrides that state.
Uploaded and generated Skills require a row recording their provenance and size.
The unique user/name key prevents conflicting names within one user's catalog.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiosqlite

from bowhead_core.access_control import _require_read_transaction
from bowhead_core.skill_bundle_lock import skill_bundle_lock, skill_write_conflict

SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_instances (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    source_type     TEXT NOT NULL,            -- builtin | user_upload | user_generated
    source_path     TEXT NOT NULL,            -- 绝对路径（builtin 指向 catalog；user 指 .data/user_skills/）
    version         TEXT,
    description     TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    source_turn_id  TEXT,
    size_bytes      INTEGER DEFAULT 0,
    verdict         TEXT,                     -- workflow-solidify S3 (§4.3): dry-run judge 结论 pass|fail|skipped
    verdict_json    TEXT,                     -- judge 结构化结果（checkpoint 逐条）
    risk_report_json TEXT,                    -- skill-multiruntime S1 (G5): 上传时 risk report(语言/关键词/shebang)
    import_status   TEXT,                     -- skill-import 批1: NULL|pending|cleared|overridden（tech-design r3 §2）
    import_report_json TEXT,                  -- skill-import 批1: 包清点结果（BundleInventory.to_dict，≤64KB）
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_skill_user
    ON skill_instances(user_id, enabled, source_type);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@dataclass
class SkillInstance:
    id: str
    user_id: str
    name: str
    source_type: str
    source_path: str
    version: str | None
    description: str | None
    enabled: bool
    source_turn_id: str | None
    size_bytes: int
    created_at: str
    updated_at: str
    # workflow-solidify S3 (§4.3): personal recipe dry-run verdict (None = not yet run).
    verdict: str | None = None
    verdict_json: str | None = None
    # skill-multiruntime S1 (G5): upload-time risk report JSON (None = not produced/OFF).
    risk_report_json: str | None = None
    # skill-import batch 1 (tech-design r3 §2): pending import status and inventory report.
    # `effective = enabled ∧ import_status != "pending"`; enabled retains user-preference semantics,
    # pending is independent; effective.py / tools.py / agent authoring / promote each enforce activation.
    import_status: str | None = None
    import_report_json: str | None = None

    @property
    def import_pending(self) -> bool:
        return self.import_status == "pending"


class SkillInstanceStore:
    """SQLite-backed per-user skill registry.

    - Dev / prod 用同一个 bowhead.db
    - 调用方保证 user_id / skill name 合法
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    @property
    def db_path(self) -> str:
        return self._db_path

    async def initialize(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(SCHEMA)
            # workflow-solidify S3 (§4.3): incremental verdict-column migration. This store originally had **no** migration block;
            # CREATE IF NOT EXISTS does not add columns to existing tables, so PRAGMA-guarded ALTER is required
            # (otherwise missing columns make set_verdict writes fail).
            cur = await db.execute("PRAGMA table_info(skill_instances)")
            cols = {row[1] for row in await cur.fetchall()}
            if "verdict" not in cols:
                await db.execute("ALTER TABLE skill_instances ADD COLUMN verdict TEXT")
            if "verdict_json" not in cols:
                await db.execute("ALTER TABLE skill_instances ADD COLUMN verdict_json TEXT")
            if "risk_report_json" not in cols:
                await db.execute("ALTER TABLE skill_instances ADD COLUMN risk_report_json TEXT")
            # skill-import batch 1: same PRAGMA-guarded column additions; outside Alembic's chain (boundary documented in 《数据库迁移》).
            if "import_status" not in cols:
                await db.execute("ALTER TABLE skill_instances ADD COLUMN import_status TEXT")
            if "import_report_json" not in cols:
                await db.execute("ALTER TABLE skill_instances ADD COLUMN import_report_json TEXT")
            await db.commit()

    # ─── Writes ────────────────────────────────────────────────────────
    async def upsert(
        self,
        *,
        user_id: str,
        name: str,
        source_type: str,
        source_path: str,
        version: str | None = None,
        description: str | None = None,
        enabled: bool = True,
        source_turn_id: str | None = None,
        size_bytes: int = 0,
        risk_report_json: str | None = None,
        reset_verdict: bool = False,
        expected_content_hash: str | None = None,
        expected_instance_id: str | None = None,
        connection: aiosqlite.Connection | None = None,
    ) -> SkillInstance:
        """Save metadata for the validated bytes, refusing a newer filesystem write.

        A supplied connection and package lock remain owned by the caller.
        """
        if connection is not None and not connection.in_transaction:
            raise ValueError("skill_write_transaction_required")
        now = _now()
        async with nullcontext(connection) if connection is not None else aiosqlite.connect(self._db_path) as db:
            if connection is None:
                await db.execute("BEGIN IMMEDIATE")
            with skill_bundle_lock(Path(source_path)) if expected_content_hash is not None and connection is None else nullcontext():
                if expected_content_hash is not None:
                    from bowhead_core.skill_upload import bundle_sha256

                    if not Path(source_path).is_dir() or "sha256:" + bundle_sha256(Path(source_path)) != expected_content_hash:
                        raise skill_write_conflict()
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT id FROM skill_instances WHERE user_id=? AND name=?",
                    (user_id, name),
                ) as cur:
                    existing = await cur.fetchone()

                if expected_instance_id is not None and (existing is None or existing["id"] != expected_instance_id):
                    raise skill_write_conflict()

                if existing:
                    await db.execute(
                        """
                        UPDATE skill_instances SET
                            source_type=?, source_path=?, version=?, description=?,
                            enabled=?, source_turn_id=?, size_bytes=?, risk_report_json=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            source_type,
                            source_path,
                            version,
                            description,
                            1 if enabled else 0,
                            source_turn_id,
                            size_bytes,
                            risk_report_json,
                            now,
                            existing["id"],
                        ),
                    )
                    sid = existing["id"]
                else:
                    sid = f"sk_{uuid4().hex[:12]}"
                    await db.execute(
                        """
                        INSERT INTO skill_instances(
                            id, user_id, name, source_type, source_path,
                            version, description, enabled, source_turn_id,
                            size_bytes, risk_report_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sid,
                            user_id,
                            name,
                            source_type,
                            source_path,
                            version,
                            description,
                            1 if enabled else 0,
                            source_turn_id,
                            size_bytes,
                            risk_report_json,
                            now,
                            now,
                        ),
                    )
                if reset_verdict:
                    await db.execute("UPDATE skill_instances SET verdict=NULL, verdict_json=NULL WHERE id=?", (sid,))
                if connection is None:
                    await db.commit()

                async with db.execute(
                    "SELECT * FROM skill_instances WHERE id=?",
                    (sid,),
                ) as cur:
                    row = await cur.fetchone()
                assert row is not None
                return _row_to_instance(row)

    async def set_enabled(
        self,
        *,
        user_id: str,
        name: str,
        enabled: bool,
    ) -> bool:
        """更新 enabled 字段. 不存在则创建 (仅针对 built-in 的禁用 override).

        调用方：当 built-in skill 被用户第一次 disable 时，传入 source_type='builtin',
        source_path=<catalog 路径> 以完整 upsert；此方法要求 row 已存在.
        不存在时返回 False，让调用方走 upsert 路径.
        """
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE skill_instances SET enabled=?, updated_at=? WHERE user_id=? AND name=?",
                (1 if enabled else 0, _now(), user_id, name),
            )
            await db.commit()
            return cur.rowcount > 0

    async def set_verdict(
        self,
        *,
        user_id: str,
        name: str,
        verdict: str,
        verdict_json: str | None = None,
    ) -> bool:
        """workflow-solidify S3 (§4.3): 写 personal recipe 的 dry-run judge 结论.

        verdict ∈ {pass, fail, skipped}；verdict_json = judge 结构化结果（checkpoint 逐条）.
        要求 row 已存在（recipe 已保存为 skill_instance）；返回是否命中一行."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE skill_instances SET verdict=?, verdict_json=?, updated_at=? WHERE user_id=? AND name=?",
                (verdict, verdict_json, _now(), user_id, name),
            )
            await db.commit()
            return cur.rowcount > 0

    async def set_import_status(
        self,
        *,
        user_id: str,
        name: str,
        import_status: str | None,
        import_report_json: str | None,
    ) -> bool:
        """skill-import 批1：写清点结果与待处理导入状态（独立 setter，不经 upsert——
        upsert 只写显式列，且报告要合并不覆盖）。要求 row 已存在；返回是否命中一行."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE skill_instances SET import_status=?, import_report_json=?, updated_at=? WHERE user_id=? AND name=?",
                (import_status, import_report_json, _now(), user_id, name),
            )
            await db.commit()
            return cur.rowcount > 0

    async def set_import_inventory(
        self,
        *,
        expected: SkillInstance,
        content_hash: str,
        import_status: str | None,
        import_report_json: str | None,
    ) -> bool:
        """Set derived inventory only for the original bytes and review state.

        The inventory caller holds the package lock throughout reading and
        settlement. A user decision during inspection is never overwritten.
        """
        from bowhead_core.skill_upload import bundle_sha256

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            source = Path(expected.source_path)
            if not source.is_dir() or "sha256:" + bundle_sha256(source) != content_hash:
                return False
            cursor = await db.execute(
                """UPDATE skill_instances SET import_status=?,import_report_json=?,updated_at=?
                   WHERE id=? AND user_id=? AND name=? AND source_path=? AND source_type=?
                     AND import_status IS ? AND import_report_json IS ?""",
                (
                    import_status,
                    import_report_json,
                    _now(),
                    expected.id,
                    expected.user_id,
                    expected.name,
                    expected.source_path,
                    expected.source_type,
                    expected.import_status,
                    expected.import_report_json,
                ),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def delete(self, *, user_id: str, name: str) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM skill_instances WHERE user_id=? AND name=?",
                (user_id, name),
            )
            await db.commit()
            return cur.rowcount > 0

    async def delete_by_id(self, *, user_id: str, skill_id: str) -> bool:
        """按持久化 ID 删除，避免把用户自定义名称放入资源 URL。"""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM skill_instances WHERE user_id=? AND id=?",
                (user_id, skill_id),
            )
            await db.commit()
            return cur.rowcount > 0

    # ─── Reads ────────────────────────────────────────────────────────
    async def get(
        self,
        *,
        user_id: str,
        name: str,
        connection: aiosqlite.Connection | None = None,
    ) -> SkillInstance | None:
        """Read a preference/instance; an explicit connection stays caller-owned."""
        _require_read_transaction(connection)
        async with (
            nullcontext(connection) if connection is not None else aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT * FROM skill_instances WHERE user_id=? AND name=?",
                (user_id, name),
            ) as cur,
        ):
            cur.row_factory = aiosqlite.Row
            row = await cur.fetchone()
        return _row_to_instance(row) if row else None

    async def get_by_id(
        self,
        *,
        user_id: str,
        skill_id: str,
        connection: aiosqlite.Connection | None = None,
    ) -> SkillInstance | None:
        """读取当前用户拥有的 Skill 实例；跨用户 ID 不泄露。"""
        _require_read_transaction(connection)
        async with (
            nullcontext(connection) if connection is not None else aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT * FROM skill_instances WHERE user_id=? AND id=?",
                (user_id, skill_id),
            ) as cur,
        ):
            cur.row_factory = aiosqlite.Row
            row = await cur.fetchone()
        return _row_to_instance(row) if row else None

    async def list_for_user(
        self,
        *,
        user_id: str,
    ) -> list[SkillInstance]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM skill_instances WHERE user_id=? ORDER BY source_type, name",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_instance(r) for r in rows]

    async def is_enabled(
        self,
        *,
        user_id: str,
        name: str,
        default: bool = True,
    ) -> bool:
        """判当前 user 是否启用某 skill.

        - DB 里无 row → 返回 default（built-in 默认 True；user skill 通常已 upsert）
        - DB 里有 row → 返该 row 的 enabled 字段
        """
        row = await self.get(user_id=user_id, name=name)
        if row is None:
            return default
        return row.enabled


def _row_to_instance(row: aiosqlite.Row) -> SkillInstance:
    # verdict / risk_report columns may be absent (pre-migration connection / old db); read defensively.
    verdict: str | None = None
    verdict_json: str | None = None
    risk_report_json: str | None = None
    import_status: str | None = None
    import_report_json: str | None = None
    try:
        verdict = row["verdict"]
        verdict_json = row["verdict_json"]
        risk_report_json = row["risk_report_json"]
    except (KeyError, IndexError):
        pass
    try:
        import_status = row["import_status"]
        import_report_json = row["import_report_json"]
    except (KeyError, IndexError):
        pass
    return SkillInstance(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        source_type=row["source_type"],
        source_path=row["source_path"],
        version=row["version"],
        description=row["description"],
        enabled=bool(row["enabled"]),
        source_turn_id=row["source_turn_id"],
        size_bytes=int(row["size_bytes"] or 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        verdict=verdict,
        verdict_json=verdict_json,
        risk_report_json=risk_report_json,
        import_status=import_status,
        import_report_json=import_report_json,
    )
