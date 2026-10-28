"""KbExtractedRowsStore — per-row records store for Sprint 25 kb-per-row-extract.

Independent SQLite db `kb_extracted_rows.db` (NOT `kb_extracted_records.db`).
Per tech-design r2 §5 + PRD r3 §5.1 — independent table for cardinality=many
doc_type (序时账 / 明细账 / 总账 / 发生额表) row-level records. Rollback safe:
0.43.0 image revert doesn't open this file, kb_extracted_records.db schema unchanged.

Key differences from kb_extracted_records:
- `row_seq` top-level column (1-indexed in source file) for SQL filter
- `row_dedup_key_hash INTEGER` (SHA1[:8] signed int64) for UNIQUE INDEX
- `extraction_attempt_id` for force re-trigger soft-delete + new attempt
- `create_many(rows)` batch INSERT 1 commit / batch (NOT per-row INSERT)
- No pre-SELECT dedup_check (UNIQUE INDEX integrity error 兜底)
- WAL + busy_timeout via bowhead_core._db.bootstrap_shared_db / open_shared_ctx
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import bootstrap_shared_db, open_shared_ctx
from bowhead_core.kb.extracted_record_store import coalesce_field_expr as _coalesce_field_expr
from bowhead_core.kb.telemetry import emit_audit

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ─── Sprint 25 caps (PRD r3 §4.3 aligned Perf-R2-4) ───────────
MAX_ROWS_PER_FILE = 10_000  # per file (single PDF), revert from r2 50K
MAX_ROWS_PER_DOC_TYPE = 200_000  # per (workspace, doc_type), accumulates across files

VERIFY_STATUS_VALUES = ("ready", "needs_review", "approved", "extract_failed")


# ─── Exceptions (tech-design §3.1 typed exception parallel to Sprint 2) ──


# ─── Schema ──────────────────────────────────────────────────


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_extracted_rows (
    row_id                  TEXT PRIMARY KEY,
    doc_type_id             TEXT NOT NULL,
    file_id                 TEXT NOT NULL,
    workspace_id            TEXT NOT NULL,
    row_seq                 INTEGER NOT NULL,
    page                    INTEGER,
    row_dedup_key_hash      INTEGER NOT NULL,
    fields_json             TEXT NOT NULL DEFAULT '{}',
    verify_status           TEXT NOT NULL DEFAULT 'ready',
    edited_by_user_json     TEXT NOT NULL DEFAULT '[]',
    extractor_model         TEXT NOT NULL DEFAULT 'qwen3.6-flash-row',
    extraction_attempt_id   INTEGER NOT NULL DEFAULT 1,
    extracted_at            TEXT NOT NULL,
    last_modified_at        TEXT NOT NULL,
    last_modified_by        TEXT NOT NULL,
    soft_deleted_at         TEXT,
    schema_version          INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_rows_dedup
    ON kb_extracted_rows (file_id, doc_type_id, row_dedup_key_hash)
    WHERE soft_deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_kb_rows_dt
    ON kb_extracted_rows (doc_type_id, workspace_id, row_seq)
    WHERE soft_deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_kb_rows_file
    ON kb_extracted_rows (file_id, row_seq)
    WHERE soft_deleted_at IS NULL;
"""


# ─── SQL query helpers (reuse extracted_record_store pattern) ─────


# Whitelist op (prevent SQL injection)
QUERY_OPS = ("=", "!=", ">", ">=", "<", "<=", "LIKE", "IS NULL", "IS NOT NULL")
SORT_DIRS = ("ASC", "DESC")
AGGREGATE_OPS = ("SUM", "COUNT", "AVG", "MIN", "MAX")


def _validate_field_name(name: str) -> None:
    """字段名必须是 alnum / 中文 / _- (防 json path 注入).

    Sprint 25 P0-TD-SEC-2 fix: 拒 `-` 单独, alias 不可含 SQL meta. 实际白名单跟
    extracted_record_store._validate_field_name 一致 — alnum + 中文 + `_-`.
    """
    if not name or len(name) > 100:
        raise ValueError(f"field name 长度 1-100, got: {name!r}")
    for ch in name:
        if ch.isalnum() or ch in "_-" or "一" <= ch <= "鿿":
            continue
        raise ValueError(f"field name 含非法字符 {ch!r}: {name!r}")


# M4 FR-42 convergence: this file's former _coalesce_field_expr was byte-identical to the
# extracted_record_store version (extracted_rows uses same v2 wrap shape as extracted_records)—
# now imports the single authority above (arm semantics/new .choice/.flag in its docstring).


# Top-level filter keys (NOT json_extract — direct SQL column access).
# Per PRD r3 §3.4 D9 + tech-design §3.1 query_rows top_level_filters.
TOP_LEVEL_FILTER_KEYS = frozenset({"row_seq", "row_seq_range", "page"})


def _rows_where_clauses(
    workspace_id: str,
    doc_type_id: str,
    where: list[dict] | None,
    top_level_filters: dict | None,
) -> tuple[list[str], list[Any]]:
    """WHERE 构建单一实现（M4 FR-44：aggregate 与 aggregate_coverage 的分母/过滤
    必须逐字节同源——原 inline 于 aggregate，语义原样提取）。"""
    clauses = ["workspace_id = ?", "doc_type_id = ?", "soft_deleted_at IS NULL"]
    params: list[Any] = [workspace_id, doc_type_id]

    if top_level_filters:
        for key, val in top_level_filters.items():
            if key not in TOP_LEVEL_FILTER_KEYS:
                raise ValueError(f"top_level_filter key must be in {TOP_LEVEL_FILTER_KEYS}")
            if key == "row_seq":
                clauses.append("row_seq = ?")
                params.append(int(val))
            elif key == "row_seq_range":
                clauses.append("row_seq BETWEEN ? AND ?")
                params.extend([int(val[0]), int(val[1])])
            elif key == "page":
                clauses.append("page = ?")
                params.append(int(val))

    for cond in where or []:
        f = cond.get("field", "")
        op_w = (cond.get("op") or "=").upper()
        val = cond.get("value")
        _validate_field_name(f)
        if op_w not in QUERY_OPS:
            raise ValueError(f"where op must be one of {QUERY_OPS}")
        expr = _coalesce_field_expr(f)
        if op_w in ("IS NULL", "IS NOT NULL"):
            clauses.append(f"{expr} {op_w}")
        else:
            clauses.append(f"{expr} {op_w} ?")
            params.append(val)
    return clauses, params


# ─── DTO ─────────────────────────────────────────────────────


@dataclass
class RowInput:
    """Per-row input from RowExtractor to create_many.

    fields shape: {field_name: {value, confidence, source_cell_text, ...}}
    (LLM extractor output before v2 wrap — store wraps to v2 variant array via
    wrap_extractor_result_to_variants on INSERT).
    """

    row_seq: int  # 1-indexed in source file
    page: int | None  # source page in PDF (None if unknown)
    fields: dict[str, Any]  # field_name → {value, confidence, source_cell_text}
    dedup_key_hash: int  # pre-computed SHA1[:8] signed int64


@dataclass
class CreateManyResult:
    inserted_count: int
    skipped_dup_count: int


@dataclass
class KbExtractedRowDTO:
    """Row-level record DTO. Parallel to KbExtractedRecordDTO but with row_seq."""

    row_id: str
    doc_type_id: str
    file_id: str
    workspace_id: str
    row_seq: int
    page: int | None
    fields: dict[str, list[dict[str, Any]]]  # v2 variant array shape
    verify_status: str
    edited_by_user: list[str]
    extractor_model: str
    extraction_attempt_id: int
    extracted_at: str
    last_modified_at: str
    last_modified_by: str
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id,
            "doc_type_id": self.doc_type_id,
            "file_id": self.file_id,
            "workspace_id": self.workspace_id,
            "row_seq": self.row_seq,
            "page": self.page,
            "fields": self.fields,
            "verify_status": self.verify_status,
            "edited_by_user": list(self.edited_by_user),
            "extractor_model": self.extractor_model,
            "extraction_attempt_id": self.extraction_attempt_id,
            "extracted_at": self.extracted_at,
            "last_modified_at": self.last_modified_at,
            "last_modified_by": self.last_modified_by,
            "schema_version": self.schema_version,
        }


@dataclass
class AggregateResult:
    """Aggregate query result row. group_keys is {} for non-grouped queries."""

    group_keys: dict[str, Any]
    value: float | int | None


# ─── helpers ─────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _safe_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _row_to_dto(row: aiosqlite.Row) -> KbExtractedRowDTO:
    """Convert SQLite row → KbExtractedRowDTO. Sprint 25 always writes v2 shape."""
    fields_data = _safe_loads(row["fields_json"], {})
    if not isinstance(fields_data, dict):
        fields_data = {}
    edited = _safe_loads(row["edited_by_user_json"], [])
    if not isinstance(edited, list):
        edited = []
    return KbExtractedRowDTO(
        row_id=row["row_id"],
        doc_type_id=row["doc_type_id"],
        file_id=row["file_id"],
        workspace_id=row["workspace_id"],
        row_seq=int(row["row_seq"]),
        page=row["page"] if row["page"] is not None else None,
        fields=fields_data,
        verify_status=row["verify_status"] or "ready",
        edited_by_user=[str(x) for x in edited],
        extractor_model=row["extractor_model"] or "qwen3.6-flash-row",
        extraction_attempt_id=int(row["extraction_attempt_id"] or 1),
        extracted_at=row["extracted_at"],
        last_modified_at=row["last_modified_at"],
        last_modified_by=row["last_modified_by"],
        schema_version=int(row["schema_version"] or 1),
    )


def compute_row_dedup_key_hash(
    fields_normalized: dict[str, Any],
    dedup_key_fields: list[str],
    row_seq: int,
) -> int:
    """SHA1[:8] → signed int64. Sprint 25 Phase A Step 3 verified zero collision
    on 50K synthetic 序时账 row.

    Per tech-design §5.4. row_seq tie-breaker for multi-entry same logical row
    (e.g. 同凭证号借/贷两分录).

    Args:
        fields_normalized: dict {field_name: {value, ...}} OR {field_name: {normalized: {value/amount}}}
        dedup_key_fields: ordered list of fields to include in hash (from catalog)
        row_seq: 1-indexed row in source file, tie-breaker

    Returns:
        signed int64 (sqlite INTEGER 64-bit), suitable for UNIQUE INDEX
    """
    parts = []
    for name in dedup_key_fields:
        v = fields_normalized.get(name, {})
        if isinstance(v, dict):
            # Try normalized.amount (currency), then normalized.value (date/string), then value (raw)
            normalized = v.get("normalized", {}) if isinstance(v.get("normalized"), dict) else {}
            val = normalized.get("amount") or normalized.get("value") or v.get("value")
            if val is None:
                val = "NULL"
        else:
            val = str(v) if v is not None else "NULL"
        parts.append(f"{name}={val}")
    parts.append(f"row_seq={row_seq}")  # tie-breaker (P1-ENG-3)
    payload = "|".join(parts).encode("utf-8")
    digest = hashlib.sha1(payload).digest()[:8]  # noqa: S324 — non-crypto, dedup only
    return int.from_bytes(digest, byteorder="big", signed=True)


# ─── Store class ─────────────────────────────────────────────


# ─── Expression index builder (P0-PERF-1: aggregate SUM perf) ──────────


# Numeric types that benefit from CAST AS REAL expression index for aggregate SUM/AVG/MIN/MAX.
_NUMERIC_INDEX_TYPES = ("currency", "number")
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9]+")


def _hash_id(s: str) -> str:
    """8-char SHA1 hex prefix — stable identifier for doc_type_id / field name."""
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]  # noqa: S324 — non-crypto, identifier


def _hash_field_name(name: str) -> str:
    """ASCII whitelist prefix (≤16) + SHA1[:8] suffix for SQL identifier safety."""
    ascii_part = _SAFE_NAME_RE.sub("_", name).strip("_")[:16]
    hash_part = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]  # noqa: S324 — identifier
    return f"{ascii_part}_{hash_part}" if ascii_part else f"_{hash_part}"


def _build_field_type_map(doc_type_fields: Any) -> dict[str, str]:
    """{field_name: type} from list of ExtractField-like (dataclass OR dict)."""
    out: dict[str, str] = {}
    if not doc_type_fields:
        return out
    for f in doc_type_fields:
        if isinstance(f, dict):
            name = f.get("name")
            type_ = f.get("type") or "string"
        else:
            name = getattr(f, "name", None)
            type_ = getattr(f, "type", "string") or "string"
        if name:
            out[name] = type_
    return out


async def _rebuild_row_field_indexes(
    db_path: str | Path,
    doc_type_id: str,
    fields: Any = None,
) -> None:
    """Sprint 25 P0-PERF-1: DROP + CREATE expression indexes for kb_extracted_rows
    numeric fields. Parallel to extracted_record_store._rebuild_field_indexes.

    Per tech-design §3.1 + CTO P0-PERF-1: aggregate SUM/AVG/MIN/MAX on 50K rows
    without expression index = 2-8s full json_extract scan (CPU-bound). With
    index = ~150-400ms. Required for PRD §12.5 perf gate.

    Args:
      db_path: kb_extracted_rows.db path
      doc_type_id: scope. Indexes named idx_kb_rows_fld_<dt_hash>_<f_hash>_v2.
      fields: list[ExtractField-like]. None/empty → drop-only path.

    Failure handling: wraps in try/except + audit + degrade (COALESCE SQL still
    works without index, just slower).
    """
    db_path_str = str(db_path)
    dt_hash = _hash_id(doc_type_id)
    try:
        async with open_shared_ctx(db_path_str) as db:
            await db.execute("BEGIN IMMEDIATE")
            # 1. DROP all indexes for this doc_type (atomic with CREATE in same txn)
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE ?",
                (f"idx_kb_rows_fld_{dt_hash}_%",),
            ) as cur:
                old_names = [row[0] for row in await cur.fetchall()]
            for old in old_names:
                await db.execute(f"DROP INDEX IF EXISTS {old}")

            # 2. CREATE new indexes per numeric field (skip if fields=None/empty)
            if fields:
                type_map = _build_field_type_map(fields)
                for fname, ftype in type_map.items():
                    if ftype not in _NUMERIC_INDEX_TYPES:
                        continue
                    _validate_field_name(fname)  # SQL/JSON-path injection guard
                    fname_hash = _hash_field_name(fname)
                    idx_name = f"idx_kb_rows_fld_{dt_hash}_{fname_hash}_v2"
                    path = fname.replace("'", "''")
                    await db.execute(
                        f"CREATE INDEX IF NOT EXISTS {idx_name} ON "
                        f"kb_extracted_rows ("
                        f"  workspace_id, doc_type_id, "
                        f"  CAST(json_extract(fields_json, "
                        f"      '$.{path}[0].normalized.amount') AS REAL)"
                        f") WHERE soft_deleted_at IS NULL"
                    )
            await db.commit()
    except Exception as exc:
        logger.exception(
            "kb _rebuild_row_field_indexes failed (db=%s, doc_type=%s)",
            db_path_str,
            doc_type_id,
        )
        emit_audit(
            "kb.row_field_index_failed",
            {
                "db_path": db_path_str,
                "doc_type_id": doc_type_id,
                "error_code": type(exc).__name__,
            },
        )


class KbExtractedRowsStore:
    """Per-row records store. Independent SQLite db file.

    Workspace-scoped — all queries enforce workspace_id = ? (P0-R1 parallel
    to extracted_record_store). PRAGMA WAL + busy_timeout via _db.bootstrap_shared_db.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def initialize(self) -> None:
        """Bootstrap WAL + create tables + idempotent indexes.

        Calls bootstrap_shared_db (per-path cached in _db._bootstrap_done) so
        WAL is set persistent. All subsequent CRUD goes through open_shared_ctx
        which applies busy_timeout=5000 per-connection.
        """
        try:
            await bootstrap_shared_db(self._db_path)
            async with open_shared_ctx(self._db_path) as db:
                for stmt in _SCHEMA.split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        await db.execute(stmt)
                await db.commit()
            emit_audit(
                "kb.migrate_kb_extracted_rows_done",
                {
                    "db_path": self._db_path,
                },
            )
        except Exception as exc:
            logger.exception("kb_extracted_rows initialize failed: %s", exc)
            emit_audit(
                "kb.migrate_kb_extracted_rows_failed",
                {
                    "db_path": self._db_path,
                    "error": type(exc).__name__,
                },
            )
            import os

            if os.environ.get("BOWHEAD_KB_MIGRATION_STRICT") == "1":
                raise

    # ─── Write path ──

    async def create_many(
        self,
        *,
        rows: list[RowInput],
        file_id: str,
        doc_type_id: str,
        workspace_id: str,
        attempt_id: int = 1,
        extractor_model: str = "qwen3.6-flash-row",
        created_by: str = "system",
        doc_type_fields: Any = None,
        verify_status: str = "ready",
    ) -> CreateManyResult:
        """Batch INSERT N row in single transaction (1 commit / batch).

        UNIQUE INDEX (file_id, doc_type_id, row_dedup_key_hash) enforces dedup,
        IntegrityError caught + counted (NOT raised). Audit emitted once at
        batch end with summary count (NOT per-row).

        Sprint 25 Perf-R2-2: 1 commit / batch. fsync overhead amortized.
        Sprint 25 P0-ENG-5: NO pre-SELECT dedup_check (relies on UNIQUE INDEX).

        Args:
            rows: list of RowInput (each pre-computed dedup_key_hash)
            file_id / doc_type_id / workspace_id: scope
            attempt_id: extraction attempt (incremented on force re-trigger)
            extractor_model: catalog name (e.g. qwen3.6-flash-row)
            doc_type_fields: passed to wrap_extractor_result_to_variants for type_hint
            verify_status: 'ready' / 'needs_review' / 'approved' / 'extract_failed'

        Returns:
            CreateManyResult(inserted_count, skipped_dup_count)
        """
        if verify_status not in VERIFY_STATUS_VALUES:
            raise ValueError(f"verify_status must be one of {VERIFY_STATUS_VALUES}")

        if not rows:
            return CreateManyResult(inserted_count=0, skipped_dup_count=0)

        # Lazy import to avoid circular dep with extracted_record_store
        from bowhead_core.kb.extracted_record_store import (
            wrap_extractor_result_to_variants,
        )

        inserted = 0
        skipped = 0
        now = _now()

        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    # v2 wrap: convert {field_name: {value, conf, source}} →
                    # {field_name: [{normalized: {...}, is_primary: true, ...}]}
                    fields_v2 = wrap_extractor_result_to_variants(row.fields, doc_type_fields)

                    # P1-TD-SEC-2: server-side echo sanitize source_cell_text
                    # Prevent LLM injection rebound. Per cell field, lift the
                    # source_cell_text -> source_excerpt for v2 variant.
                    # (wrap_extractor_result_to_variants reads source_snippet;
                    # caller passes source_cell_text in same shape).

                    row_id = f"row_{uuid4().hex[:14]}"
                    try:
                        await db.execute(
                            """INSERT INTO kb_extracted_rows
                               (row_id, doc_type_id, file_id, workspace_id,
                                row_seq, page, row_dedup_key_hash, fields_json,
                                verify_status, edited_by_user_json,
                                extractor_model, extraction_attempt_id,
                                extracted_at, last_modified_at, last_modified_by,
                                schema_version)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, 1)""",
                            (
                                row_id,
                                doc_type_id,
                                file_id,
                                workspace_id,
                                row.row_seq,
                                row.page,
                                row.dedup_key_hash,
                                json.dumps(fields_v2, ensure_ascii=False),
                                verify_status,
                                extractor_model,
                                attempt_id,
                                now,
                                now,
                                created_by,
                            ),
                        )
                        inserted += 1
                    except aiosqlite.IntegrityError:
                        # UNIQUE INDEX violation — dedup hit, count silently
                        skipped += 1
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        # Audit summary (not per-row, P0-SEC-5 + telemetry banned-key filter)
        if skipped > 0 or inserted > 0:
            emit_audit(
                "kb.row_extract_duplicate_skipped_summary",
                {
                    "file_id": file_id,
                    "doc_type_id": doc_type_id,
                    "workspace_id": workspace_id,
                    "attempt_id": attempt_id,
                    "inserted_count": inserted,
                    "skipped_dup_count": skipped,
                },
            )
        return CreateManyResult(inserted_count=inserted, skipped_dup_count=skipped)

    async def create_many_prewrapped(
        self,
        entries: list[dict[str, Any]],
        *,
        doc_type_id: str,
        workspace_id: str,
        created_by: str,
        extractor_model: str = "matrix_graduation",
        batch_size: int = 500,
    ) -> CreateManyResult:
        """kb-matrix M4 FR-40 转正直存：每 entry = 一个文件的**已包裹 v2 变体**行。

        为什么不走 ``create_many``（r2/r3 review P0）：它无条件
        ``wrap_extractor_result_to_variants``（:pre-wrap dict 期望），而矩阵
        cell.variants 是**已包裹 list**——wrapper ``isinstance(dict)`` 跳过 →
        字段全丢空行。本方法直插 v2 ``fields_json``（provenance/多来源/edited
        variant 保真），**sanitize 由调用方（graduation 组装）负责**——
        ``row_extractor.sanitize_cell_text`` 已在组装时对 source_excerpt 等跑过。

        entry 形状::

            {"file_id": str,
             "fields_v2": {field_name: [variant, ...]},   # 已包裹 v2
             "edited_fields": [str, ...],                  # cell.edited → 行级映射（F10）
             "page": int | None}

        ``row_seq`` 恒 1（每文件恰一行）；dedup hash 恒定值——跨文件唯一性由
        UNIQUE INDEX (file_id, doc_type_id, hash) 的 file_id 列承载（r3 决策 1，
        不改 ``compute_row_dedup_key_hash``）。分批 commit（batch_size）避单
        ``BEGIN IMMEDIATE`` 长锁 rows DB（DoS 收口，r3 风险⑥）。
        """
        if not entries:
            return CreateManyResult(inserted_count=0, skipped_dup_count=0)

        dedup_hash = compute_row_dedup_key_hash(
            fields_normalized={},
            dedup_key_fields=[],
            row_seq=1,
        )
        inserted = 0
        skipped = 0
        now = _now()
        for start in range(0, len(entries), max(1, batch_size)):
            batch = entries[start : start + max(1, batch_size)]
            async with open_shared_ctx(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    for entry in batch:
                        row_id = f"row_{uuid4().hex[:14]}"
                        try:
                            await db.execute(
                                """INSERT INTO kb_extracted_rows
                                   (row_id, doc_type_id, file_id, workspace_id,
                                    row_seq, page, row_dedup_key_hash, fields_json,
                                    verify_status, edited_by_user_json,
                                    extractor_model, extraction_attempt_id,
                                    extracted_at, last_modified_at, last_modified_by,
                                    schema_version)
                                   VALUES (?, ?, ?, ?, 1, ?, ?, ?, 'ready', ?, ?, 1, ?, ?, ?, 1)""",
                                (
                                    row_id,
                                    doc_type_id,
                                    str(entry.get("file_id") or ""),
                                    workspace_id,
                                    entry.get("page"),
                                    dedup_hash,
                                    json.dumps(entry.get("fields_v2") or {}, ensure_ascii=False),
                                    json.dumps(list(entry.get("edited_fields") or []), ensure_ascii=False),
                                    extractor_model,
                                    now,
                                    now,
                                    created_by,
                                ),
                            )
                            inserted += 1
                        except aiosqlite.IntegrityError:
                            skipped += 1  # Idempotent same-file replay → UNIQUE hit counted silently
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

        emit_audit(
            "kb.row_extract_duplicate_skipped_summary",
            {
                "doc_type_id": doc_type_id,
                "workspace_id": workspace_id,
                "attempt_id": 1,
                "inserted_count": inserted,
                "skipped_dup_count": skipped,
                "source": "matrix_graduation",
            },
        )
        return CreateManyResult(inserted_count=inserted, skipped_dup_count=skipped)

    # ─── Read path ──

    async def get(self, row_id: str) -> KbExtractedRowDTO | None:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_extracted_rows WHERE row_id = ? AND soft_deleted_at IS NULL",
                (row_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_dto(row) if row else None

    async def list_for_file(self, file_id: str) -> list[KbExtractedRowDTO]:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_extracted_rows WHERE file_id = ? AND soft_deleted_at IS NULL ORDER BY row_seq",
                (file_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    async def count_for_doc_type(
        self,
        *,
        workspace_id: str,
        doc_type_id: str,
        needs_review_only: bool = False,
    ) -> int:
        """COUNT(*) for Records page filter chip + cap check.

        Sprint 25 P0-TD-PERF-2: 100K row COUNT via partial expression index leaves
        实测 ~50-100ms (acceptable). FilterChips total + needs_review_count.
        """
        clauses = ["workspace_id = ?", "doc_type_id = ?", "soft_deleted_at IS NULL"]
        params: list[Any] = [workspace_id, doc_type_id]
        if needs_review_only:
            clauses.append("verify_status = 'needs_review'")
        sql = f"SELECT COUNT(*) FROM kb_extracted_rows WHERE {' AND '.join(clauses)}"
        async with open_shared_ctx(self._db_path) as db, db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def query_rows(
        self,
        *,
        workspace_id: str,
        doc_type_id: str,
        where: list[dict] | None = None,
        top_level_filters: dict | None = None,
        sort_by: str | None = None,
        sort_dir: str = "ASC",
        limit: int = 50,
        offset: int = 0,
        cast_numeric: list[str] | None = None,
    ) -> list[KbExtractedRowDTO]:
        """SQL filter + sort + paginate. workspace_id 强制过滤 (P0-R1).

        Sprint 25 NEW vs Sprint 1 query_by_field:
        - `top_level_filters`: dict with row_seq / row_seq_range / page (走 SQL 顶层列, NOT json_extract)
        - `offset`: server-side paginate (PRD §3.4 D8 Records page)

        Args:
            workspace_id: 必传, cross-ws leak 防
            doc_type_id: 必传, 限单 doc_type
            where: list of {field, op, value} for fields_json filter
            top_level_filters: {row_seq: N | row_seq_range: [lo, hi] | page: N}
            sort_by: field name (走 fields_json) OR 'row_seq'/'page'/'extracted_at' (top-level)
            sort_dir: ASC | DESC
            limit: ≤ 1000
            offset: ≥ 0 (Records page paginate)
            cast_numeric: list of field names to CAST AS REAL for sort/compare
        """
        sort_dir = sort_dir.upper()
        if sort_dir not in SORT_DIRS:
            raise ValueError(f"sort_dir must be one of {SORT_DIRS}")
        limit = min(max(int(limit), 1), 1000)
        offset = max(int(offset), 0)
        cast_numeric = cast_numeric or []
        for f in cast_numeric:
            _validate_field_name(f)

        clauses = ["workspace_id = ?", "doc_type_id = ?", "soft_deleted_at IS NULL"]
        params: list[Any] = [workspace_id, doc_type_id]

        # Top-level column filters (SAFE — int cast)
        if top_level_filters:
            for key, val in top_level_filters.items():
                if key not in TOP_LEVEL_FILTER_KEYS:
                    raise ValueError(f"top_level_filter key must be in {TOP_LEVEL_FILTER_KEYS}")
                if key == "row_seq":
                    clauses.append("row_seq = ?")
                    params.append(int(val))
                elif key == "row_seq_range":
                    if not isinstance(val, (list, tuple)) or len(val) != 2:
                        raise ValueError("row_seq_range must be [lo, hi]")
                    clauses.append("row_seq BETWEEN ? AND ?")
                    params.extend([int(val[0]), int(val[1])])
                elif key == "page":
                    clauses.append("page = ?")
                    params.append(int(val))

        # fields_json WHERE
        for cond in where or []:
            f = cond.get("field", "")
            op = (cond.get("op") or "=").upper()
            val = cond.get("value")
            _validate_field_name(f)
            if op not in QUERY_OPS:
                raise ValueError(f"op must be one of {QUERY_OPS}")
            expr = _coalesce_field_expr(f)
            if f in cast_numeric:
                expr = f"CAST({expr} AS REAL)"
            if op in ("IS NULL", "IS NOT NULL"):
                clauses.append(f"{expr} {op}")
            else:
                clauses.append(f"{expr} {op} ?")
                params.append(val)

        # Sort
        if sort_by in ("row_seq", "page", "extracted_at"):
            sort_expr = sort_by
        elif sort_by:
            _validate_field_name(sort_by)
            sort_expr = _coalesce_field_expr(sort_by)
            if sort_by in cast_numeric:
                sort_expr = f"CAST({sort_expr} AS REAL)"
        else:
            sort_expr = "row_seq"

        sql = f"SELECT * FROM kb_extracted_rows WHERE {' AND '.join(clauses)} ORDER BY {sort_expr} {sort_dir} LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    async def aggregate(
        self,
        *,
        workspace_id: str,
        doc_type_id: str,
        field: str,
        op: str,
        group_by: list[str] | None = None,
        where: list[dict] | None = None,
        top_level_filters: dict | None = None,
        raw_compare: bool = False,
    ) -> list[AggregateResult]:
        """SQL aggregate (SUM/COUNT/AVG/MIN/MAX) with optional group_by.

        Sprint 25 P0-TD-SEC-2: alias uses positional grp_{i} (NOT grp_{field_name})
        to avoid SQL identifier issues with Chinese/special chars in field names.

        kb-matrix M4（wave-1 review P1）：``raw_compare=True`` 让 MIN/MAX 走**文本序**
        比较（不 CAST AS REAL）——date 列转正时已归一 ISO，字典序=日期序才正确；
        CAST 对 '2026-04-12' 只得 2026.0（年粒度垃圾）。仅 MIN/MAX 合法。

        Returns:
            list of AggregateResult(group_keys={...}, value=...).
            For non-grouped query, returns single result with group_keys={}.
        """
        op = op.upper()
        if op not in AGGREGATE_OPS:
            raise ValueError(f"aggregate op must be one of {AGGREGATE_OPS}")
        if raw_compare and op not in ("MIN", "MAX"):
            raise ValueError("raw_compare only valid for MIN/MAX")
        _validate_field_name(field)
        group_by = group_by or []
        for g in group_by:
            _validate_field_name(g)

        # Build SELECT
        # P0-TD-SEC-2: positional grp_{i} for alias (not grp_{Chinese_field})
        if op == "COUNT":
            # COUNT(*) or COUNT(field) — both supported via expression
            agg_expr = f"COUNT({_coalesce_field_expr(field)})"
        elif raw_compare:
            agg_expr = f"{op}({_coalesce_field_expr(field)})"
        else:
            field_expr = f"CAST({_coalesce_field_expr(field)} AS REAL)"
            agg_expr = f"{op}({field_expr})"

        select_parts = [f"{agg_expr} AS agg_value"]
        for i, g in enumerate(group_by):
            grp_sql = g if g in ("row_seq", "page") else _coalesce_field_expr(g)
            select_parts.append(f"{grp_sql} AS grp_{i}")

        # WHERE clauses (same as query_rows)—M4 extracted a helper shared with aggregate_coverage
        # (denominator/filters must have byte-identical origin, or coverage will not match the aggregate).
        clauses, params = _rows_where_clauses(workspace_id, doc_type_id, where, top_level_filters)

        group_clause = ""
        if group_by:
            group_clause = "GROUP BY " + ", ".join(f"grp_{i}" for i in range(len(group_by)))

        sql = f"SELECT {', '.join(select_parts)} FROM kb_extracted_rows WHERE {' AND '.join(clauses)} {group_clause}"

        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()

        results = []
        for r in rows:
            group_keys = {group_by[i]: r[f"grp_{i}"] for i in range(len(group_by))}
            value = r["agg_value"]
            results.append(AggregateResult(group_keys=group_keys, value=value))
        return results

    async def aggregate_coverage(
        self,
        *,
        workspace_id: str,
        doc_type_id: str,
        field: str,
        where: list[dict] | None = None,
        top_level_filters: dict | None = None,
        sample_limit: int = 5,
        numeric: bool = True,
    ) -> dict[str, Any]:
        """M4 FR-44 聚合诚实：数值聚合的**覆盖**——included（真被计入聚合的行数）/
        total（同过滤全行数）/ excluded_samples（被排除的 raw_string 哨兵原话 ≤N 条，
        residual guard 挡下的「$300/month/面议」们）。

        分母/过滤与 ``aggregate`` 走同一 ``_rows_where_clauses``（逐字节同源）。
        ``numeric=True``（SUM/AVG 数值路）：included = COUNT(CAST(expr AS REAL))
        （忽略 NULL，SQL 标准）；``numeric=False``（date MIN/MAX 文本序路）：
        included = COUNT(expr)（非 NULL 文本值即计入）。
        """
        _validate_field_name(field)
        clauses, params = _rows_where_clauses(workspace_id, doc_type_id, where, top_level_filters)
        expr = _coalesce_field_expr(field)
        where_sql = " AND ".join(clauses)
        included_expr = f"COUNT(CAST({expr} AS REAL))" if numeric else f"COUNT({expr})"
        excluded_pred = f"CAST({expr} AS REAL) IS NULL" if numeric else f"{expr} IS NULL"
        raw_expr = f"json_extract(fields_json, '$.{field}[0].normalized.raw_string')"
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT COUNT(*) AS total, {included_expr} AS included "  # noqa: S608 — expr/clauses all whitelist-built
                f"FROM kb_extracted_rows WHERE {where_sql}",
                params,
            ) as cur:
                row = await cur.fetchone()
            total = int(row["total"]) if row else 0
            included = int(row["included"]) if row else 0
            samples: list[str] = []
            if included < total and sample_limit > 0:
                async with db.execute(
                    f"SELECT {raw_expr} AS raw FROM kb_extracted_rows "  # noqa: S608
                    f"WHERE {where_sql} AND {excluded_pred} AND {raw_expr} IS NOT NULL "
                    f"LIMIT ?",
                    [*params, int(sample_limit)],
                ) as cur:
                    sample_rows = await cur.fetchall()
                samples = [str(r["raw"])[:80] for r in sample_rows]
        return {"included": included, "total": total, "excluded_samples": samples}

    # ─── Update / Delete ──

    async def soft_delete_for_attempt(
        self,
        *,
        file_id: str,
        doc_type_id: str,
        workspace_id: str,
        attempt_id_lt: int,
    ) -> int:
        """Force re-trigger: soft-delete all rows with attempt_id < N (P0-SEC-7).

        Cross-ws guard via workspace_id in WHERE (defense in depth).
        Returns rowcount soft-deleted.
        """
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "UPDATE kb_extracted_rows "
                "SET soft_deleted_at = ?, last_modified_at = ? "
                "WHERE file_id = ? AND doc_type_id = ? AND workspace_id = ? "
                "AND extraction_attempt_id < ? AND soft_deleted_at IS NULL",
                (now, now, file_id, doc_type_id, workspace_id, attempt_id_lt),
            )
            await db.commit()
            return cur.rowcount

    async def soft_delete_for_file(self, file_id: str, *, exclude_doc_type_ids: list[str] | None = None) -> int:
        """File 删时级联软删 rows (parallel to extracted_record_store.soft_delete_for_file).

        kb-matrix M4 gate④：``exclude_doc_type_ids`` 豁免 graduated 快照行
        （时点冻结不随文件删除改写；调用方按 is_frozen_snapshot 算集合）。
        """
        now = _now()
        sql = "UPDATE kb_extracted_rows SET soft_deleted_at = ?, last_modified_at = ? WHERE file_id = ? AND soft_deleted_at IS NULL"
        params: list[Any] = [now, now, file_id]
        if exclude_doc_type_ids:
            placeholders = ",".join("?" for _ in exclude_doc_type_ids)
            sql += f" AND doc_type_id NOT IN ({placeholders})"
            params.extend(exclude_doc_type_ids)
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(sql, params)
            await db.commit()
            return cur.rowcount

    async def list_doc_type_ids_for_file(self, file_id: str) -> list[str]:
        """该文件现存 rows 涉及的 doc_type_id 去重集（M4 gate④ 豁免计算用）。"""
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                "SELECT DISTINCT doc_type_id FROM kb_extracted_rows WHERE file_id = ? AND soft_deleted_at IS NULL",
                (file_id,),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return [str(r[0]) for r in rows]

    async def soft_delete_for_doc_type(self, doc_type_id: str, *, workspace_id: str) -> int:
        """删 graduated doc_type 时级联软删其 rows（M4 决策 12——否则行带摘录
        永久滞留 = 存储/合规债；chat 不可达但擦除合规要真删）。"""
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "UPDATE kb_extracted_rows SET soft_deleted_at = ?, last_modified_at = ? "
                "WHERE doc_type_id = ? AND workspace_id = ? AND soft_deleted_at IS NULL",
                (now, now, doc_type_id, workspace_id),
            )
            await db.commit()
            return cur.rowcount

    async def purge_provenance_for_file(self, file_id: str) -> int:
        """FR-7 dormant 原语（M4 gate④ 配套，镜像 matrix_store 同名语义）：剔除该
        file 全部 rows 的 variant ``source_excerpt``（摘录原文）；normalized 值保留。
        调用点留给未来 90 天 grace 硬删 cron 接线（合规债台账，与矩阵同一登记）。
        """
        n = 0
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT row_id, fields_json FROM kb_extracted_rows WHERE file_id = ?",
                (file_id,),
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                try:
                    fields = json.loads(row["fields_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                changed = False
                if isinstance(fields, dict):
                    for variants in fields.values():
                        if not isinstance(variants, list):
                            continue
                        for v in variants:
                            if isinstance(v, dict) and v.get("source_excerpt") is not None:
                                v["source_excerpt"] = None
                                changed = True
                if changed:
                    await db.execute(
                        "UPDATE kb_extracted_rows SET fields_json = ?, last_modified_at = ? WHERE row_id = ?",
                        (json.dumps(fields, ensure_ascii=False), now, row["row_id"]),
                    )
                    n += 1
            await db.commit()
        return n

    async def update_verify_status(
        self,
        row_id: str,
        new_status: str,
        *,
        modified_by: str,
    ) -> bool:
        if new_status not in VERIFY_STATUS_VALUES:
            raise ValueError(f"verify_status must be one of {VERIFY_STATUS_VALUES}")
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "UPDATE kb_extracted_rows "
                "SET verify_status = ?, last_modified_at = ?, last_modified_by = ? "
                "WHERE row_id = ? AND soft_deleted_at IS NULL",
                (new_status, now, modified_by, row_id),
            )
            await db.commit()
            return cur.rowcount > 0
