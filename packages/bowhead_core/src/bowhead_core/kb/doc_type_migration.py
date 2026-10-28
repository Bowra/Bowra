"""kb-structured-tables Sprint 2 — ALTER migrations for kb_doc_types + kb_files.

Per PRD r2 + tech-design §A.2. Idempotent ALTER fns mirror Sprint 1
``extracted_record_store.migrate_v1_to_v2`` pattern (PRAGMA sniff + ADD COLUMN +
audit + degrade-on-fail).

Wired in gateway lifespan: see ``bowhead_gateway/main.py`` after the respective
store.initialize() call.

Idempotency: PRAGMA sniff guards each ALTER. Safe to call on every cold boot.

Failure mode: exception caught + audit emit + degrade-not-crash. Re-raises only
if ``BOWHEAD_KB_MIGRATION_STRICT=1`` env (test mode).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path  # noqa: TC003 (runtime-used as `str | Path` arg, kept import-time)

import aiosqlite

from bowhead_core.kb.telemetry import emit_audit

logger = logging.getLogger(__name__)


# Sprint 1 reused threshold: above N rows, ALTER may exceed startup health-check
# window. Warn-only; doesn't block.
V2_MIGRATION_ROW_COUNT_THRESHOLD = 10_000


def _strict_mode() -> bool:
    return os.environ.get("BOWHEAD_KB_MIGRATION_STRICT", "").strip() == "1"


async def migrate_kb_doc_types_add_columns(db_path: str | Path) -> None:
    """Sprint 2: ADD COLUMN to kb_doc_types (3 new cols).

    New columns (per PRD r2 + tech-design §A.5):
      - cardinality TEXT NOT NULL DEFAULT 'one' (no CHECK in ADD COLUMN;
        validation happens at write time via _validate_cardinality)
      - auto_extract_enabled INTEGER NOT NULL DEFAULT 1
      - sample_keywords_json TEXT NOT NULL DEFAULT '[]'

    NOTE: SQLite ADD COLUMN ignores CHECK constraints on the new column
    definition. Fresh DBs created via doc_type_store._SCHEMA still get the
    CHECK (CREATE TABLE supports it). For legacy DBs migrated via this fn,
    Python-side validation in doc_type_store.create()/update() enforces
    cardinality enum.

    Idempotent: PRAGMA sniff before each ALTER.
    Soft-fail: caught exceptions → audit + log (unless STRICT env).
    """
    db_path_str = str(db_path)
    t0 = time.time()
    try:
        async with aiosqlite.connect(db_path_str) as db:
            async with db.execute(
                "SELECT name FROM pragma_table_info('kb_doc_types')",
            ) as cur:
                rows = await cur.fetchall()
            existing_cols = {row[0] for row in rows}
            if not existing_cols:
                logger.info(
                    "kb_doc_types migrate: table absent, skip (init pending)",
                )
                return

            # Row count warn (Sprint 1 pattern)
            async with db.execute(
                "SELECT COUNT(*) FROM kb_doc_types WHERE soft_deleted_at IS NULL",
            ) as cur:
                row = await cur.fetchone()
            row_count = int(row[0]) if row else 0
            if row_count > V2_MIGRATION_ROW_COUNT_THRESHOLD:
                logger.warning(
                    "kb_doc_types row count=%s > %s — startup health window risk",
                    row_count,
                    V2_MIGRATION_ROW_COUNT_THRESHOLD,
                )

            added: list[str] = []
            if "cardinality" not in existing_cols:
                await db.execute(
                    "ALTER TABLE kb_doc_types ADD COLUMN cardinality TEXT NOT NULL DEFAULT 'one'",
                )
                added.append("cardinality")
            if "auto_extract_enabled" not in existing_cols:
                await db.execute(
                    "ALTER TABLE kb_doc_types ADD COLUMN auto_extract_enabled INTEGER NOT NULL DEFAULT 1",
                )
                added.append("auto_extract_enabled")
            if "sample_keywords_json" not in existing_cols:
                await db.execute(
                    "ALTER TABLE kb_doc_types ADD COLUMN sample_keywords_json TEXT NOT NULL DEFAULT '[]'",
                )
                added.append("sample_keywords_json")
            await db.commit()
        duration_ms = int((time.time() - t0) * 1000)
        emit_audit(
            "kb.migrate_doc_types_add_columns_done",
            {
                "db_path": db_path_str,
                "added": added,
                "row_count": row_count,
                "duration_ms": duration_ms,
            },
        )
        if added:
            logger.info(
                "kb_doc_types migrate done: added=%s rows=%s duration_ms=%s",
                added,
                row_count,
                duration_ms,
            )
    except Exception as exc:
        emit_audit(
            "kb.migrate_doc_types_add_columns_done",
            {
                "db_path": db_path_str,
                "error": type(exc).__name__,
                "duration_ms": int((time.time() - t0) * 1000),
            },
        )
        logger.exception("kb_doc_types migrate failed (degrading)")
        if _strict_mode():
            raise


async def migrate_kb_files_add_auto_classified(db_path: str | Path) -> None:
    """Sprint 2: ADD COLUMN kb_files.auto_classified_doc_type_id TEXT NULL.

    Rollback-safe column: 0.40.5 worker reads `file.doc_type_id` for the
    explicit-bind gate; this NEW column is read only by Sprint 2 worker hook.
    So rolling back to 0.40.5 image after this column populates leaves the
    column intact (orphaned but harmless).

    Idempotent: PRAGMA sniff before ALTER.
    Soft-fail: caught exceptions → audit + log (unless STRICT env).
    """
    db_path_str = str(db_path)
    t0 = time.time()
    try:
        async with aiosqlite.connect(db_path_str) as db:
            async with db.execute(
                "SELECT name FROM pragma_table_info('kb_files')",
            ) as cur:
                rows = await cur.fetchall()
            existing_cols = {row[0] for row in rows}
            if not existing_cols:
                logger.info(
                    "kb_files migrate: table absent, skip (init pending)",
                )
                return

            if "auto_classified_doc_type_id" in existing_cols:
                # Already present (fresh DB via _TABLE_SCHEMA, or earlier migrate run)
                return

            await db.execute(
                "ALTER TABLE kb_files ADD COLUMN auto_classified_doc_type_id TEXT",
            )
            await db.commit()
        duration_ms = int((time.time() - t0) * 1000)
        emit_audit(
            "kb.migrate_doc_types_add_columns_done",
            {
                "db_path": db_path_str,
                "added": ["kb_files.auto_classified_doc_type_id"],
                "duration_ms": duration_ms,
            },
        )
        logger.info(
            "kb_files migrate done: added=auto_classified_doc_type_id duration_ms=%s",
            duration_ms,
        )
    except Exception as exc:
        emit_audit(
            "kb.migrate_doc_types_add_columns_done",
            {
                "db_path": db_path_str,
                "error": type(exc).__name__,
                "duration_ms": int((time.time() - t0) * 1000),
            },
        )
        logger.exception("kb_files migrate failed (degrading)")
        if _strict_mode():
            raise


async def migrate_kb_doc_types_add_sprint25_columns(db_path: str | Path) -> None:
    """Sprint 25 kb-per-row-extract: ADD COLUMN to kb_doc_types (2 new cols).

    - row_count INTEGER NOT NULL DEFAULT 0
        Cache for kb_extracted_rows count per (workspace, doc_type). Worker
        `_run_per_row_extraction` calls `update_row_count(+N)` on success.
        Parallel to existing `record_count` cache (Sprint 1 file-level records).
    - dedup_key_fields_json TEXT NOT NULL DEFAULT '[]'
        Per-doc_type list of field names that form row_dedup_key_hash for the
        UNIQUE INDEX in kb_extracted_rows. Catalog defines per doc_type (e.g.
        明细账 = [日期, 凭证号, 科目编码, 借方金额, 贷方金额]).

    Idempotent: PRAGMA sniff before each ALTER.
    Soft-fail: caught exceptions → audit + log (unless STRICT env).
    """
    db_path_str = str(db_path)
    t0 = time.time()
    try:
        async with aiosqlite.connect(db_path_str) as db:
            async with db.execute(
                "SELECT name FROM pragma_table_info('kb_doc_types')",
            ) as cur:
                rows = await cur.fetchall()
            existing_cols = {row[0] for row in rows}
            if not existing_cols:
                logger.info("kb_doc_types migrate sprint25: table absent, skip")
                return

            added: list[str] = []
            if "row_count" not in existing_cols:
                await db.execute(
                    "ALTER TABLE kb_doc_types ADD COLUMN row_count INTEGER NOT NULL DEFAULT 0",
                )
                added.append("row_count")
            if "dedup_key_fields_json" not in existing_cols:
                await db.execute(
                    "ALTER TABLE kb_doc_types ADD COLUMN dedup_key_fields_json TEXT NOT NULL DEFAULT '[]'",
                )
                added.append("dedup_key_fields_json")
            await db.commit()
        duration_ms = int((time.time() - t0) * 1000)
        emit_audit(
            "kb.migrate_doc_types_add_columns_done",
            {
                "db_path": db_path_str,
                "added": added,
                "sprint": "25",
                "duration_ms": duration_ms,
            },
        )
        if added:
            logger.info(
                "kb_doc_types sprint25 migrate done: added=%s duration_ms=%s",
                added,
                duration_ms,
            )
    except Exception as exc:
        emit_audit(
            "kb.migrate_doc_types_add_columns_done",
            {
                "db_path": db_path_str,
                "error": type(exc).__name__,
                "sprint": "25",
                "duration_ms": int((time.time() - t0) * 1000),
            },
        )
        logger.exception("kb_doc_types sprint25 migrate failed (degrading)")
        if _strict_mode():
            raise


async def migrate_kb_doc_types_add_m4_columns(db_path: str | Path) -> None:
    """kb-matrix M4 FR-40: ADD COLUMN ×3 to kb_doc_types（矩阵转正血缘）。

    New columns（全 nullable，additive——旧代码读多余列自然忽略，回滚零动作）:
      - graduated_from_matrix_id TEXT   来源矩阵（doc_type→matrix 的逆向血缘是
        matrix 侧 template_doc_type_id，两者独立）
      - graduated_from_seq INTEGER      转正时矩阵 last_seq（幂等锚——同快照重试
        返回既有 doc_type；structure_seq 会让「重跑刷新→再转正」错误复用旧快照）
      - graduated_mode TEXT             'template'|'pending_snapshot'|'snapshot'
        （NULL=非转正；pending 对列表面不可见，自愈 sweep 收口崩溃窗口）

    Idempotent: PRAGMA sniff before each ALTER（照 Sprint 2/25 先例）。
    Soft-fail: caught exceptions → audit + log (unless STRICT env)。
    """
    db_path_str = str(db_path)
    t0 = time.time()
    try:
        async with aiosqlite.connect(db_path_str) as db:
            async with db.execute(
                "SELECT name FROM pragma_table_info('kb_doc_types')",
            ) as cur:
                rows = await cur.fetchall()
            existing_cols = {row[0] for row in rows}
            if not existing_cols:
                logger.info("kb_doc_types m4 migrate: table absent, skip (init pending)")
                return

            added: list[str] = []
            if "graduated_from_matrix_id" not in existing_cols:
                await db.execute(
                    "ALTER TABLE kb_doc_types ADD COLUMN graduated_from_matrix_id TEXT",
                )
                added.append("graduated_from_matrix_id")
            if "graduated_from_seq" not in existing_cols:
                await db.execute(
                    "ALTER TABLE kb_doc_types ADD COLUMN graduated_from_seq INTEGER",
                )
                added.append("graduated_from_seq")
            if "graduated_mode" not in existing_cols:
                await db.execute(
                    "ALTER TABLE kb_doc_types ADD COLUMN graduated_mode TEXT",
                )
                added.append("graduated_mode")
            await db.commit()
        duration_ms = int((time.time() - t0) * 1000)
        emit_audit(
            "kb.migrate_doc_types_add_columns_done",
            {
                "db_path": db_path_str,
                "added": added,
                "sprint": "m4-graduation",
                "duration_ms": duration_ms,
            },
        )
        if added:
            logger.info(
                "kb_doc_types m4 migrate done: added=%s duration_ms=%s",
                added,
                duration_ms,
            )
    except Exception as exc:
        emit_audit(
            "kb.migrate_doc_types_add_columns_done",
            {
                "db_path": db_path_str,
                "error": type(exc).__name__,
                "sprint": "m4-graduation",
                "duration_ms": int((time.time() - t0) * 1000),
            },
        )
        logger.exception("kb_doc_types m4 migrate failed (degrading)")
        if _strict_mode():
            raise


async def migrate_kb_doc_types_add_sharing_columns(db_path: str | Path) -> None:
    """kb-matrix-sharing: ADD COLUMN ×1 to kb_doc_types（组织模板库复制血缘）。

    New column（nullable，additive——旧代码读多余列自然忽略，回滚零动作）:
      - copied_from_team_template_id TEXT  复制来源的团队模板库条目 entry_id
        （None=非复制来源；attribution + 发现面「已复制」去重 + 审计关联）

    **无条件运行（flag 无关）**：create() 是所有 doc_type 创建的共享 INSERT，加列
    须对所有部署生效（否则 fresh-DB/legacy `create()` no-such-column）。fresh DB 经
    _SCHEMA CREATE TABLE 已带该列；本迁移只补 legacy DB。

    Idempotent: PRAGMA sniff before ALTER。Soft-fail: caught → audit + log
    (unless STRICT env)。镜像 migrate_kb_doc_types_add_m4_columns。
    """
    db_path_str = str(db_path)
    t0 = time.time()
    try:
        async with aiosqlite.connect(db_path_str) as db:
            async with db.execute(
                "SELECT name FROM pragma_table_info('kb_doc_types')",
            ) as cur:
                rows = await cur.fetchall()
            existing_cols = {row[0] for row in rows}
            if not existing_cols:
                logger.info("kb_doc_types sharing migrate: table absent, skip (init pending)")
                return

            added: list[str] = []
            if "copied_from_team_template_id" not in existing_cols:
                await db.execute(
                    "ALTER TABLE kb_doc_types ADD COLUMN copied_from_team_template_id TEXT",
                )
                added.append("copied_from_team_template_id")
            await db.commit()
        duration_ms = int((time.time() - t0) * 1000)
        emit_audit(
            "kb.migrate_doc_types_add_columns_done",
            {
                "db_path": db_path_str,
                "added": added,
                "sprint": "kb-matrix-sharing",
                "duration_ms": duration_ms,
            },
        )
        if added:
            logger.info(
                "kb_doc_types sharing migrate done: added=%s duration_ms=%s",
                added,
                duration_ms,
            )
    except Exception as exc:
        emit_audit(
            "kb.migrate_doc_types_add_columns_done",
            {
                "db_path": db_path_str,
                "error": type(exc).__name__,
                "sprint": "kb-matrix-sharing",
                "duration_ms": int((time.time() - t0) * 1000),
            },
        )
        logger.exception("kb_doc_types sharing migrate failed (degrading)")
        if _strict_mode():
            raise
