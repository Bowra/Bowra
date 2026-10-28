"""KbMatrixStore — 文档矩阵三表一库（kb_matrix.db）。

kb-matrix M2 tech design §4。三表：
- ``kb_matrices``：矩阵实体。``files_json``（有序 [{file_id, char_count}]，行序 SSOT）
  + ``columns_json``（列定义 SSOT，含 prompt_version）。
- ``kb_matrix_cells``：每格一行，lazy 建行——**empty 态 = 无行**（快照组装由
  files×columns 网格补 empty）。status 无 "empty" 无 "stale"（stale 纯派生，决策 3）。
- ``kb_matrix_runs``：run 台账 + 恢复锚点（对账唯一真相 = credit_ledger，本表是派生
  台账，run 收敛时由引擎从 ledger 反查回填）。

seq 协议（§4.4）：``kb_matrices.last_seq`` 为权威；任何 cell 行写入必须在同一事务里
``UPDATE kb_matrices SET last_seq = last_seq + 1`` 并把新值写进 cell.seq——本类内部
唯一 cell 写原语 = ``_bump_seq`` + ``_upsert_cell_row``，全部写路径走它。结构性变更
（列/文件集/改名/转正/删除）额外把 ``structure_seq`` 置为新 last_seq；快照请求
``since_seq < structure_seq`` 时返回全量。单进程安全依据 = chi_flags 的
WEB_CONCURRENCY=1 boot 强制；DB 事务为重启兜底。

连接走 ``bootstrap_shared_db``/``open_shared_ctx``（Sprint 25 范式；不抄
extracted_record_store 的裸 aiosqlite.connect busy_timeout=0 历史欠账）。
S1 谓词纵深：读路径 clauses 恒含 ``soft_deleted_at IS NULL``，按需追加
``workspace_id = ?``；mutate 带可选 ws 追加谓词。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import bootstrap_shared_db, open_shared_ctx

# M4 FR-42: options validation/constants and three new type normalizers moved to shared layer—redundant-alias
# re-export here (PEP 484 style) preserves existing caller/tests' `matrix_store.validate_options`
# / `_split_multi_value` etc. references unchanged.
from bowhead_core.kb.doc_type_store import (
    FIELD_TYPES as FIELD_TYPES,
)
from bowhead_core.kb.doc_type_store import (
    MAX_OPTION_LEN as MAX_OPTION_LEN,
)
from bowhead_core.kb.doc_type_store import (
    MAX_OPTIONS as MAX_OPTIONS,
)
from bowhead_core.kb.doc_type_store import (
    OPTIONS_REQUIRED_TYPES as OPTIONS_REQUIRED_TYPES,
)
from bowhead_core.kb.doc_type_store import (
    validate_options as validate_options,
)
from bowhead_core.kb.extracted_record_store import (
    _match_option as _match_option,
)
from bowhead_core.kb.extracted_record_store import (
    _split_multi_value as _split_multi_value,
)
from bowhead_core.kb.extracted_record_store import (
    normalize_field_value as normalize_field_value,
)
from bowhead_core.kb.extracted_record_store import (
    v1_value_to_normalized as v1_value_to_normalized,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ─── enums（§4.2 / §4.3）──────────────────────────────────────

CELL_STATUS_VALUES = ("queued", "running", "done", "failed", "needs_review")

# Pre-enqueue state (cancel rollback); force rerun enqueues done/needs_review (review P1 four-value enum).
QUEUED_FROM_VALUES = ("empty", "failed", "done", "needs_review")

FAIL_REASON_VALUES = (
    "llm_invoke_failed",
    "llm_parse_failed",
    "timeout",
    "insufficient_credits",
    "quota_exceeded",
    "system_interrupted",
    "source_unavailable",
)

RUN_STATUS_VALUES = ("running", "paused", "succeeded", "failed", "cancelling", "cancelled")
RUN_ACTIVE_STATUSES = ("running", "paused", "cancelling")

# columns_json type values = kernel FIELD_TYPES (single SSOT since M4 FR-42; do not duplicate)
# + question (matrix concept, degraded to string before kernel; execution degradation in matrix_engine).
MATRIX_COLUMN_TYPES = (*FIELD_TYPES, "question")

# Column graduation (in-matrix promote, §4.5): question → any kernel type = FIELD_TYPES itself.
PROMOTABLE_TYPES = FIELD_TYPES

# options constants/validate_options: moved to kernel (doc_type_store) in M4; redundant-alias
# re-export above preserves existing callers/tests.


# ─── schema (§4.1-4.3 DDL level) ────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_matrices (
    matrix_id            TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    workspace_id         TEXT NOT NULL,
    team_id               TEXT NOT NULL,
    files_json           TEXT NOT NULL DEFAULT '[]',
    columns_json         TEXT NOT NULL DEFAULT '[]',
    template_doc_type_id TEXT,
    last_seq             INTEGER NOT NULL DEFAULT 0,
    structure_seq        INTEGER NOT NULL DEFAULT 0,
    created_by           TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    last_run_at          TEXT,
    soft_deleted_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_kb_matrices_ws
    ON kb_matrices (workspace_id, updated_at DESC) WHERE soft_deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS kb_matrix_cells (
    matrix_id       TEXT NOT NULL,
    file_id         TEXT NOT NULL,
    column_id       TEXT NOT NULL,
    workspace_id    TEXT NOT NULL,
    team_id          TEXT NOT NULL,
    status          TEXT NOT NULL,
    queued_from     TEXT,
    variants_json   TEXT NOT NULL DEFAULT '[]',
    fail_reason     TEXT,
    fail_hint       TEXT,
    edited_by_user  INTEGER NOT NULL DEFAULT 0,
    verified        INTEGER NOT NULL DEFAULT 0,
    cached          INTEGER NOT NULL DEFAULT 0,
    prompt_version  INTEGER NOT NULL,
    file_version    INTEGER NOT NULL DEFAULT 1,
    content_hash    TEXT,
    cache_key       TEXT,
    idempotency_key TEXT,
    run_id          TEXT,
    seq             INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    soft_deleted_at TEXT,
    PRIMARY KEY (matrix_id, file_id, column_id)
);

CREATE INDEX IF NOT EXISTS idx_kb_matrix_cells_seq
    ON kb_matrix_cells (matrix_id, seq) WHERE soft_deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_kb_matrix_cells_col
    ON kb_matrix_cells (matrix_id, column_id) WHERE soft_deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_kb_matrix_cells_cache
    ON kb_matrix_cells (team_id, cache_key) WHERE soft_deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_kb_matrix_cells_run
    ON kb_matrix_cells (run_id, status) WHERE soft_deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS kb_matrix_runs (
    run_id            TEXT PRIMARY KEY,
    matrix_id         TEXT NOT NULL,
    workspace_id      TEXT NOT NULL,
    team_id            TEXT NOT NULL,
    scope_json        TEXT NOT NULL,
    status            TEXT NOT NULL,
    cells_total       INTEGER NOT NULL DEFAULT 0,
    cells_done        INTEGER NOT NULL DEFAULT 0,
    cells_failed      INTEGER NOT NULL DEFAULT 0,
    cells_cached      INTEGER NOT NULL DEFAULT 0,
    skipped_edited    INTEGER NOT NULL DEFAULT 0,
    credits_estimated INTEGER NOT NULL DEFAULT 0,
    credits_charged   INTEGER NOT NULL DEFAULT 0,
    tokens_input      INTEGER NOT NULL DEFAULT 0,
    tokens_output     INTEGER NOT NULL DEFAULT 0,
    error_code        TEXT,
    initiated_by      TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    heartbeat_at      TEXT NOT NULL,
    finished_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_kb_matrix_runs_matrix
    ON kb_matrix_runs (matrix_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_kb_matrix_runs_status
    ON kb_matrix_runs (status, heartbeat_at);
"""


# ─── DTO ─────────────────────────────────────────────────────


@dataclass
class MatrixDTO:
    matrix_id: str
    name: str
    workspace_id: str
    team_id: str
    files: list[dict[str, Any]]
    columns: list[dict[str, Any]]
    template_doc_type_id: str | None
    last_seq: int
    structure_seq: int
    created_by: str
    created_at: str
    updated_at: str
    last_run_at: str | None
    soft_deleted_at: str | None


@dataclass
class MatrixCellDTO:
    matrix_id: str
    file_id: str
    column_id: str
    workspace_id: str
    team_id: str
    status: str
    queued_from: str | None
    variants: list[dict[str, Any]]
    fail_reason: str | None
    fail_hint: str | None
    edited_by_user: bool
    verified: bool
    cached: bool
    prompt_version: int
    file_version: int
    content_hash: str | None
    cache_key: str | None
    idempotency_key: str | None
    run_id: str | None
    seq: int
    created_at: str
    updated_at: str
    kb_generation_id: str | None = None
    kb_source_check_state: str | None = None


@dataclass
class MatrixRunDTO:
    run_id: str
    matrix_id: str
    workspace_id: str
    team_id: str
    scope: dict[str, Any]
    status: str
    cells_total: int
    cells_done: int
    cells_failed: int
    cells_cached: int
    skipped_edited: int
    credits_estimated: int
    credits_charged: int
    tokens_input: int
    tokens_output: int
    error_code: str | None
    initiated_by: str
    started_at: str
    heartbeat_at: str
    finished_at: str | None
    billing_rate_card_version: str = "v1"


# ─── module helpers ──────────────────────────────────────────


def new_matrix_id() -> str:
    return f"mx_{uuid4().hex[:12]}"


def new_column_id() -> str:
    return f"mxcol_{uuid4().hex[:12]}"


def new_run_id() -> str:
    return f"mxrun_{uuid4().hex[:14]}"


def normalize_column(col: dict[str, Any]) -> dict[str, Any]:
    """列定义归一到 columns_json 全形状（FR-2 r5 + depends_on 留位）。"""
    type_ = str(col.get("type") or "question")
    if type_ not in MATRIX_COLUMN_TYPES:
        raise ValueError(f"column type must be one of {MATRIX_COLUMN_TYPES}")
    cross_verify = bool(col.get("cross_verify"))
    if cross_verify and type_ == "question":
        # FR-39: two samples of free long text almost always differ, pure noise—disabled for question columns.
        raise ValueError("cross_verify_not_applicable")
    return {
        "column_id": str(col.get("column_id") or new_column_id()),
        "name": str(col.get("name") or "").strip(),
        "type": type_,
        "question": str(col.get("question") or ""),
        "extraction_hint": str(col.get("extraction_hint") or ""),
        "options": validate_options(type_, col.get("options")),
        "depends_on": col.get("depends_on"),
        "cross_verify": cross_verify,
        "prompt_version": int(col.get("prompt_version") or 1),
    }


def cell_is_stale(
    status: str,
    cell_prompt_version: int,
    col_prompt_version: int,
    cell_file_version: int,
    file_version_now: int | None,
) -> bool:
    """stale 单一权威谓词（tech-design-m3 决策 2）：快照组装（routes._cell_payload）
    与「重跑已过期」圈选（engine.expand_targets）双消费——两处公式漂移 =
    「显示已过期但圈不进重跑」整类 bug，UT 钉一致性。"""
    return status in ("done", "needs_review") and (
        cell_prompt_version < col_prompt_version or (file_version_now is not None and cell_file_version < file_version_now)
    )


def normalize_matrix_value(value: Any, col: dict[str, Any]) -> dict[str, Any] | None:
    """矩阵层归一化（M3 决策 8 → M4 FR-42 收敛）：实现整体迁至
    ``extracted_record_store.normalize_field_value``（doc_type 抽取路 wrap 同源
    消费，一份归一化两处用），本函数保签名成薄壳——M3 语义/UT 原样。
    不匹配 → {"raw_string": …} 哨兵（needs_review 机制与 M2 同源零改）。"""
    type_ = str(col.get("type") or "string")
    return normalize_field_value(value, type_, col.get("options"))


def is_raw_string_sentinel(normalized: dict[str, Any] | None) -> bool:
    """needs_review 哨兵判定（与 M1 同源：primary normalized 为 {"raw_string": …}）。"""
    return isinstance(normalized, dict) and "raw_string" in normalized


# _fold_option/_match_option/_split_multi_value: moved to extracted_record_store in M4
# (normalize_field_value is self-contained); imports above retain `_match_option/_split_multi_value`
# for existing module/test references ([Python repr fallback etc. M3 semantics migrated unchanged]).


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _matrix_row_to_dto(row: aiosqlite.Row) -> MatrixDTO:
    return MatrixDTO(
        matrix_id=row["matrix_id"],
        name=row["name"],
        workspace_id=row["workspace_id"],
        team_id=row["team_id"],
        files=_loads(row["files_json"], []),
        columns=_loads(row["columns_json"], []),
        template_doc_type_id=row["template_doc_type_id"],
        last_seq=int(row["last_seq"]),
        structure_seq=int(row["structure_seq"]),
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_run_at=row["last_run_at"],
        soft_deleted_at=row["soft_deleted_at"],
    )


def _cell_row_to_dto(row: aiosqlite.Row) -> MatrixCellDTO:
    return MatrixCellDTO(
        matrix_id=row["matrix_id"],
        file_id=row["file_id"],
        column_id=row["column_id"],
        workspace_id=row["workspace_id"],
        team_id=row["team_id"],
        status=row["status"],
        queued_from=row["queued_from"],
        variants=_loads(row["variants_json"], []),
        fail_reason=row["fail_reason"],
        fail_hint=row["fail_hint"],
        edited_by_user=bool(row["edited_by_user"]),
        verified=bool(row["verified"]),
        cached=bool(row["cached"]),
        prompt_version=int(row["prompt_version"]),
        file_version=int(row["file_version"]),
        content_hash=row["content_hash"],
        cache_key=row["cache_key"],
        idempotency_key=row["idempotency_key"],
        run_id=row["run_id"],
        seq=int(row["seq"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        kb_generation_id=row["kb_generation_id"] if "kb_generation_id" in dict(row) else None,
        kb_source_check_state=row["kb_source_check_state"] if "kb_source_check_state" in dict(row) else None,
    )


def _run_row_to_dto(row: aiosqlite.Row) -> MatrixRunDTO:
    return MatrixRunDTO(
        run_id=row["run_id"],
        matrix_id=row["matrix_id"],
        workspace_id=row["workspace_id"],
        team_id=row["team_id"],
        scope=_loads(row["scope_json"], {}),
        status=row["status"],
        cells_total=int(row["cells_total"]),
        cells_done=int(row["cells_done"]),
        cells_failed=int(row["cells_failed"]),
        cells_cached=int(row["cells_cached"]),
        skipped_edited=int(row["skipped_edited"]),
        credits_estimated=int(row["credits_estimated"]),
        credits_charged=int(row["credits_charged"]),
        tokens_input=int(row["tokens_input"]),
        tokens_output=int(row["tokens_output"]),
        error_code=row["error_code"],
        initiated_by=row["initiated_by"],
        started_at=row["started_at"],
        heartbeat_at=row["heartbeat_at"],
        finished_at=row["finished_at"],
        billing_rate_card_version=(row["billing_rate_card_version"] if "billing_rate_card_version" in set(row.keys()) else None) or "v1",
    )


# ─── store ───────────────────────────────────────────────────


class KbMatrixStore:
    """三表单 store——cell 写 + last_seq 递增 + run 计数必须同事务（跨 sqlite 文件无事务）。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        # Structural changes (columns/files) read-modify-write: get_matrix → modify JSON in memory → full UPDATE;
        # concurrent awaits lose updates (impl review B-P2-8)—serialize infrequent operations with one lock.
        self._structure_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await bootstrap_shared_db(self._db_path)
        async with open_shared_ctx(self._db_path) as db:
            for stmt in _SCHEMA.split(";"):
                stmt = stmt.strip()
                if stmt:
                    await db.execute(stmt)
            await db.commit()

    # ─── matrices ───────────────────────────────────────────

    async def create_matrix(
        self,
        *,
        name: str,
        workspace_id: str,
        team_id: str,
        created_by: str,
        files: list[dict[str, Any]],
        columns: list[dict[str, Any]],
        template_doc_type_id: str | None = None,
    ) -> MatrixDTO:
        matrix_id = new_matrix_id()
        now = _now()
        norm_cols = [normalize_column(c) for c in columns]
        norm_files = [{"file_id": str(f["file_id"]), "char_count": int(f.get("char_count") or 0)} for f in files]
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "INSERT INTO kb_matrices (matrix_id, name, workspace_id, team_id, files_json,"
                " columns_json, template_doc_type_id, last_seq, structure_seq, created_by,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)",
                (
                    matrix_id,
                    name,
                    workspace_id,
                    team_id,
                    json.dumps(norm_files, ensure_ascii=False),
                    json.dumps(norm_cols, ensure_ascii=False),
                    template_doc_type_id,
                    created_by,
                    now,
                    now,
                ),
            )
            await db.commit()
        got = await self.get_matrix(matrix_id)
        assert got is not None
        return got

    async def get_matrix(self, matrix_id: str, *, workspace_id: str | None = None) -> MatrixDTO | None:
        clauses = ["matrix_id = ?", "soft_deleted_at IS NULL"]
        params: list[Any] = [matrix_id]
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM kb_matrices WHERE {' AND '.join(clauses)}",
                params,
            ) as cur:
                row = await cur.fetchone()
        return _matrix_row_to_dto(row) if row else None

    async def list_matrices(self, workspace_id: str) -> list[MatrixDTO]:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_matrices WHERE workspace_id = ? AND soft_deleted_at IS NULL ORDER BY updated_at DESC",
                (workspace_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_matrix_row_to_dto(r) for r in rows]

    async def rename_matrix(self, matrix_id: str, name: str, *, workspace_id: str) -> bool:
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "UPDATE kb_matrices SET name = ? WHERE matrix_id = ? AND workspace_id = ? AND soft_deleted_at IS NULL",
                (name, matrix_id, workspace_id),
            )
            if cur.rowcount == 0:
                return False
            await self._bump_seq(db, matrix_id, structure=True)
            await db.commit()
        return True

    async def soft_delete_matrix(self, matrix_id: str, *, workspace_id: str) -> bool:
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            cur = await db.execute(
                "UPDATE kb_matrices SET soft_deleted_at = ?, updated_at = ? WHERE matrix_id = ? AND workspace_id = ? AND soft_deleted_at IS NULL",
                (now, now, matrix_id, workspace_id),
            )
            if cur.rowcount == 0:
                return False
            await db.execute(
                "UPDATE kb_matrix_cells SET soft_deleted_at = ?, updated_at = ? WHERE matrix_id = ? AND soft_deleted_at IS NULL",
                (now, now, matrix_id),
            )
            await db.commit()
        return True

    async def touch_last_run_at(self, matrix_id: str) -> None:
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "UPDATE kb_matrices SET last_run_at = ?, updated_at = ? WHERE matrix_id = ?",
                (now, now, matrix_id),
            )
            await db.commit()

    # ─── files（§4.6）───────────────────────────────────────

    async def replace_files(self, matrix_id: str, files: list[dict[str, Any]], *, workspace_id: str) -> bool:
        """全量替换 files_json；移除的文件级联软删其 cells（structure bump → 全量回退）。"""
        async with self._structure_lock:
            matrix = await self.get_matrix(matrix_id, workspace_id=workspace_id)
            if matrix is None:
                return False
            norm_files = [{"file_id": str(f["file_id"]), "char_count": int(f.get("char_count") or 0)} for f in files]
            new_ids = {f["file_id"] for f in norm_files}
            removed = [f["file_id"] for f in matrix.files if f.get("file_id") not in new_ids]
            now = _now()
            async with open_shared_ctx(self._db_path) as db:
                await db.execute(
                    "UPDATE kb_matrices SET files_json = ? WHERE matrix_id = ?",
                    (json.dumps(norm_files, ensure_ascii=False), matrix_id),
                )
                if removed:
                    placeholders = ",".join("?" for _ in removed)
                    await db.execute(
                        f"UPDATE kb_matrix_cells SET soft_deleted_at = ?, updated_at = ? WHERE matrix_id = ? AND file_id IN ({placeholders}) AND soft_deleted_at IS NULL",
                        (now, now, matrix_id, *removed),
                    )
                await self._bump_seq(db, matrix_id, structure=True)
                await db.commit()
            return True

    # ─── columns（§4.5）─────────────────────────────────────

    async def add_column(self, matrix_id: str, column: dict[str, Any], *, workspace_id: str) -> str | None:
        async with self._structure_lock:
            matrix = await self.get_matrix(matrix_id, workspace_id=workspace_id)
            if matrix is None:
                return None
            col = normalize_column(column)
            cols = [*matrix.columns, col]
            async with open_shared_ctx(self._db_path) as db:
                await db.execute(
                    "UPDATE kb_matrices SET columns_json = ? WHERE matrix_id = ?",
                    (json.dumps(cols, ensure_ascii=False), matrix_id),
                )
                await self._bump_seq(db, matrix_id, structure=True)
                await db.commit()
            return str(col["column_id"])

    async def update_column(
        self,
        matrix_id: str,
        column_id: str,
        *,
        workspace_id: str,
        name: str | None = None,
        question: str | None = None,
        extraction_hint: str | None = None,
        options: list[str] | None = None,
        cross_verify: bool | None = None,
    ) -> dict[str, Any] | None:
        """改 name 只改 JSON；改 question/hint/**options/cross_verify**（FR-4 r5）
        → prompt_version += 1（该列已完成 cells 派生 stale，决策 3——无需 UPDATE cells）。
        options 过 validate_options 同源校验（本方法不经 normalize_column，必须显式挂，
        tech-design-m3 §0.3）。返回更新后的列定义。"""
        async with self._structure_lock:
            matrix = await self.get_matrix(matrix_id, workspace_id=workspace_id)
            if matrix is None:
                return None
            cols = [dict(c) for c in matrix.columns]
            target = next((c for c in cols if c.get("column_id") == column_id), None)
            if target is None:
                return None
            prompt_changed = False
            if name is not None and name != target.get("name"):
                target["name"] = name
            if question is not None and question != target.get("question"):
                target["question"] = question
                prompt_changed = True
            if extraction_hint is not None and extraction_hint != target.get("extraction_hint"):
                target["extraction_hint"] = extraction_hint
                prompt_changed = True
            if options is not None:
                norm_options = validate_options(str(target.get("type") or ""), options)
                existing = target.get("options") or None
                # Order-independent set comparison (explicit FR-4, impl-diff review P2-1): only **set** changes
                # bump prompt_version (avoid display-only reorder falsely triggering stale + rerun dialog);
                # pure reorder still persists in columns_json (structure_seq bump refreshes FE order).
                if norm_options != existing:
                    target["options"] = norm_options
                if set(norm_options or []) != set(existing or []):
                    prompt_changed = True
            if cross_verify is not None and cross_verify != bool(target.get("cross_verify")):
                if cross_verify and target.get("type") == "question":
                    raise ValueError("cross_verify_not_applicable")
                target["cross_verify"] = cross_verify
                prompt_changed = True
            if prompt_changed:
                target["prompt_version"] = int(target.get("prompt_version") or 1) + 1
            async with open_shared_ctx(self._db_path) as db:
                await db.execute(
                    "UPDATE kb_matrices SET columns_json = ? WHERE matrix_id = ?",
                    (json.dumps(cols, ensure_ascii=False), matrix_id),
                )
                await self._bump_seq(db, matrix_id, structure=True)
                await db.commit()
            return target

    async def promote_column(
        self,
        matrix_id: str,
        column_id: str,
        new_type: str,
        *,
        workspace_id: str,
        options: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """转正（question → 结构化类型；M3 含 select/multi_select/boolean，
        {type, options} 可同 present——FR-5 r5）：列 type 改写 + 全列 done/needs_review
        cells 就地 re-coercion（单事务逐格递增 seq）。不递增 prompt_version、不产生
        stale（FR-4 例外）。edited 格同样 re-coercion（类型再解释非 AI 覆盖，
        edited/verified 标记保留）。取值路径（review P2）：优先 normalized.value，
        缺失取 raw，两者皆无 → 跳过该格保持现状。

        毒缓存收口路③（tech-design-m3 §4.3 / review S-P1-2）：全列 cells 同事务
        ``cache_key=NULL, cached=0``——re-coercion 后形状与旧（question 型）指纹脱钩，
        同文本列命中会拿到错形状值。"""
        async with self._structure_lock:
            if new_type not in PROMOTABLE_TYPES:
                raise ValueError(f"promote target type must be one of {PROMOTABLE_TYPES}")
            matrix = await self.get_matrix(matrix_id, workspace_id=workspace_id)
            if matrix is None:
                return None
            cols = [dict(c) for c in matrix.columns]
            target = next((c for c in cols if c.get("column_id") == column_id), None)
            if target is None:
                return None
            if target.get("type") != "question":
                raise ValueError("only question columns can be promoted")
            target["type"] = new_type
            target["options"] = validate_options(new_type, options)
            now = _now()
            async with open_shared_ctx(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute(
                    "UPDATE kb_matrices SET columns_json = ? WHERE matrix_id = ?",
                    (json.dumps(cols, ensure_ascii=False), matrix_id),
                )
                await db.execute(
                    "UPDATE kb_matrix_cells SET cache_key = NULL, cached = 0 WHERE matrix_id = ? AND column_id = ? AND soft_deleted_at IS NULL",
                    (matrix_id, column_id),
                )
                async with db.execute(
                    "SELECT * FROM kb_matrix_cells WHERE matrix_id = ? AND column_id = ?"
                    " AND status IN ('done', 'needs_review') AND soft_deleted_at IS NULL",
                    (matrix_id, column_id),
                ) as cur:
                    rows = await cur.fetchall()
                for row in rows:
                    variants = _loads(row["variants_json"], [])
                    primary_idx = next(
                        (i for i, v in enumerate(variants) if isinstance(v, dict) and v.get("is_primary")),
                        -1,
                    )
                    if primary_idx < 0:
                        continue
                    primary = dict(variants[primary_idx])
                    norm = primary.get("normalized")
                    value: Any = None
                    if isinstance(norm, dict) and norm.get("value") is not None:
                        value = norm.get("value")
                    elif isinstance(norm, dict) and norm.get("amount") is not None:
                        value = norm.get("amount")
                    elif isinstance(norm, dict) and norm.get("raw_string") is not None:
                        value = norm.get("raw_string")
                    elif primary.get("raw") is not None:
                        value = primary.get("raw")
                    if value is None:
                        continue
                    new_norm = normalize_matrix_value(value, target)
                    primary["normalized"] = new_norm
                    variants[primary_idx] = primary
                    new_status = "needs_review" if is_raw_string_sentinel(new_norm) else "done"
                    seq = await self._bump_seq(db, matrix_id)
                    await db.execute(
                        "UPDATE kb_matrix_cells SET variants_json = ?, status = ?, seq = ?, updated_at = ?"
                        " WHERE matrix_id = ? AND file_id = ? AND column_id = ?",
                        (
                            json.dumps(variants, ensure_ascii=False),
                            new_status,
                            seq,
                            now,
                            matrix_id,
                            row["file_id"],
                            column_id,
                        ),
                    )
                await self._bump_seq(db, matrix_id, structure=True)
                await db.commit()
            return target

    async def delete_column(self, matrix_id: str, column_id: str, *, workspace_id: str) -> bool:
        async with self._structure_lock:
            matrix = await self.get_matrix(matrix_id, workspace_id=workspace_id)
            if matrix is None:
                return False
            cols = [c for c in matrix.columns if c.get("column_id") != column_id]
            if len(cols) == len(matrix.columns):
                return False
            now = _now()
            async with open_shared_ctx(self._db_path) as db:
                await db.execute(
                    "UPDATE kb_matrices SET columns_json = ? WHERE matrix_id = ?",
                    (json.dumps(cols, ensure_ascii=False), matrix_id),
                )
                await db.execute(
                    "UPDATE kb_matrix_cells SET soft_deleted_at = ?, updated_at = ? WHERE matrix_id = ? AND column_id = ? AND soft_deleted_at IS NULL",
                    (now, now, matrix_id, column_id),
                )
                await self._bump_seq(db, matrix_id, structure=True)
                await db.commit()
            return True

    # ─── cells: engine write paths ──────────────────────────

    async def enqueue_cells(
        self,
        matrix_id: str,
        targets: list[tuple[str, str]],
        *,
        run_id: str,
        workspace_id: str,
        team_id: str,
        column_versions: dict[str, int],
        file_versions: dict[str, int],
    ) -> int:
        """目标格入队（单事务）。幂等：已 queued/running 的格跳过（idempotency_key
        + UPSERT WHERE status NOT IN 语义）。存量行不覆盖 prompt_version/file_version
        （取消回退要还原旧值语义；执行版本由 finalize_cell 落）。返回实际入队数。"""
        if not targets:
            return 0
        async with open_shared_ctx(self._db_path) as db:
            enqueued = await self._enqueue_cells_on_db(
                db,
                matrix_id,
                targets,
                run_id=run_id,
                workspace_id=workspace_id,
                team_id=team_id,
                column_versions=column_versions,
                file_versions=file_versions,
            )
            await db.commit()
        return enqueued

    async def mark_cells_running(self, matrix_id: str, keys: list[tuple[str, str]]) -> None:
        if not keys:
            return
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            for file_id, column_id in keys:
                seq = await self._bump_seq(db, matrix_id)
                await db.execute(
                    "UPDATE kb_matrix_cells SET status = 'running', seq = ?, updated_at = ?"
                    " WHERE matrix_id = ? AND file_id = ? AND column_id = ? AND status = 'queued' AND soft_deleted_at IS NULL",
                    (seq, now, matrix_id, file_id, column_id),
                )
            await db.commit()

    async def finalize_cell(
        self,
        matrix_id: str,
        file_id: str,
        column_id: str,
        *,
        status: str,
        variants: list[dict[str, Any]] | None = None,
        fail_reason: str | None = None,
        fail_hint: str | None = None,
        cached: bool = False,
        prompt_version: int | None = None,
        file_version: int | None = None,
        content_hash: str | None = None,
        cache_key: str | None = None,
        run_id: str | None = None,
        require_unedited: bool = False,
        kb_generation_id: str | None = None,
        kb_source_check_state: str | None = None,
        generation_db_path: str | None = None,
    ) -> bool:
        """AI 落态唯一出口（run 落态 / 单格 rerun / force 重跑 / 缓存命中复制）。

        落态不变量（review P1，Verified Intelligence 命脉）：**任何 AI 写新值
        （variants 非 None）必同事务置 verified = 0 与 edited_by_user = 0**——人工
        确认的是旧值；重跑 edited 格是显式意图（FR-12b），旧人工值由引擎降级保留为
        非 primary variant 后传入。failed 落态未写新值 → 旧值仍是人工确认过的，
        edited/verified 原样保留。

        ``require_unedited``（impl review B-P1-3）：批量 run 落态传 True——WHERE 追加
        ``edited_by_user = 0``，原子闭掉「enqueue 后、落态前用户编辑」的整个窗口
        （含 engine 读检查与 UPDATE 之间的竞态毫秒），FR-9「批量永不覆盖人工值」
        才在 run 全时长成立。返回是否真正落态（False = 被编辑守卫跳过）。
        """
        if status not in ("done", "needs_review", "failed"):
            raise ValueError("finalize status must be done/needs_review/failed")
        if fail_reason is not None and fail_reason not in FAIL_REASON_VALUES:
            raise ValueError(f"fail_reason must be one of {FAIL_REASON_VALUES}")
        now = _now()
        sets = [
            "status = ?",
            "queued_from = NULL",
            "fail_reason = ?",
            "fail_hint = ?",
            "seq = ?",
            "updated_at = ?",
        ]
        async with open_shared_ctx(self._db_path) as db:
            if kb_generation_id:
                if not generation_db_path:
                    raise ValueError("generation database required")
                await db.execute("ATTACH DATABASE ? AS kb_generation_guard", (str(generation_db_path),))
            seq = await self._bump_seq(db, matrix_id)
            params: list[Any] = [status, fail_reason, fail_hint, seq, now]
            if variants is not None:
                sets.extend(
                    [
                        "variants_json = ?",
                        "edited_by_user = 0",
                        "verified = 0",
                        "cached = ?",
                    ]
                )
                params.append(json.dumps(variants, ensure_ascii=False))
                params.append(1 if cached else 0)
            if prompt_version is not None:
                sets.append("prompt_version = ?")
                params.append(int(prompt_version))
            if file_version is not None:
                sets.append("file_version = ?")
                params.append(int(file_version))
            if content_hash is not None:
                sets.append("content_hash = ?")
                params.append(content_hash)
            if cache_key is not None:
                sets.append("cache_key = ?")
                params.append(cache_key)
            if run_id is not None:
                sets.append("run_id = ?")
                params.append(run_id)
            if kb_generation_id:
                sets.extend(["kb_generation_id = ?", "kb_source_check_state = ?"])
                params.extend([kb_generation_id, kb_source_check_state])
            params.extend([matrix_id, file_id, column_id])
            guard = " AND edited_by_user = 0" if require_unedited else ""
            if kb_generation_id:
                guard += (
                    " AND EXISTS (SELECT 1 FROM kb_generation_guard.kb_files f WHERE f.file_id=kb_matrix_cells.file_id "
                    "AND f.active_generation_id=? AND f.version=? AND f.source_absent=0 AND f.soft_deleted_at IS NULL)"
                )
                params.extend([kb_generation_id, file_version])
            cur = await db.execute(
                f"UPDATE kb_matrix_cells SET {', '.join(sets)} WHERE matrix_id = ? AND file_id = ? AND column_id = ? AND soft_deleted_at IS NULL{guard}",
                params,
            )
            await db.commit()
            if kb_generation_id:
                await db.execute("DETACH DATABASE kb_generation_guard")
            return cur.rowcount > 0

    async def revert_queued_cells_for_run(self, run_id: str) -> int:
        """取消回退（§5.5）：queued cells 一次事务回退到 queued_from——empty=删行，
        failed/done/needs_review=恢复原态（variants 未被入队触碰，原样保留）。"""
        reverted = 0
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT matrix_id, file_id, column_id, queued_from FROM kb_matrix_cells"
                " WHERE run_id = ? AND status = 'queued' AND soft_deleted_at IS NULL",
                (run_id,),
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                await self._revert_queued_cell_on_db(db, row["matrix_id"], row["file_id"], row["column_id"], row["queued_from"], now)
                reverted += 1
            await db.commit()
        return reverted

    async def revert_queued_cells(self, matrix_id: str, keys: list[tuple[str, str]]) -> int:
        """按键回退 queued cells（引擎文件不可用跳行路径，§4.6：不算 failed）。"""
        if not keys:
            return 0
        reverted = 0
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            for file_id, column_id in keys:
                async with db.execute(
                    "SELECT queued_from FROM kb_matrix_cells WHERE matrix_id = ? AND file_id = ? AND column_id = ?"
                    " AND status = 'queued' AND soft_deleted_at IS NULL",
                    (matrix_id, file_id, column_id),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    continue
                await self._revert_queued_cell_on_db(db, matrix_id, file_id, column_id, row["queued_from"], now)
                reverted += 1
            await db.commit()
        return reverted

    async def reset_running_cells_to_queued(self, run_id: str) -> int:
        """boot resume：进程死于 mid-call 的 running cells 回 queued（done 格不重跑
        ——cell 状态本身就是 checkpoint，决策 5）。"""
        n = 0
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT matrix_id, file_id, column_id FROM kb_matrix_cells WHERE run_id = ? AND status = 'running' AND soft_deleted_at IS NULL",
                (run_id,),
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                seq = await self._bump_seq(db, row["matrix_id"])
                await db.execute(
                    "UPDATE kb_matrix_cells SET status = 'queued', seq = ?, updated_at = ? WHERE matrix_id = ? AND file_id = ? AND column_id = ?",
                    (seq, now, row["matrix_id"], row["file_id"], row["column_id"]),
                )
                n += 1
            await db.commit()
        return n

    async def fail_pending_cells_for_run(
        self,
        run_id: str,
        *,
        fail_reason: str,
        fail_hint: str | None = None,
    ) -> int:
        """systemic_stop / zombie：该 run 的 queued+running cells → failed（可重试）。
        未写新值 → edited/verified/variants 原样保留（同 finalize_cell failed 语义）。"""
        if fail_reason not in FAIL_REASON_VALUES:
            raise ValueError(f"fail_reason must be one of {FAIL_REASON_VALUES}")
        n = 0
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT matrix_id, file_id, column_id FROM kb_matrix_cells"
                " WHERE run_id = ? AND status IN ('queued', 'running') AND soft_deleted_at IS NULL",
                (run_id,),
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                seq = await self._bump_seq(db, row["matrix_id"])
                await db.execute(
                    "UPDATE kb_matrix_cells SET status = 'failed', fail_reason = ?, fail_hint = ?,"
                    " queued_from = NULL, seq = ?, updated_at = ?"
                    " WHERE matrix_id = ? AND file_id = ? AND column_id = ?",
                    (fail_reason, fail_hint, seq, now, row["matrix_id"], row["file_id"], row["column_id"]),
                )
                n += 1
            await db.commit()
        return n

    async def fail_cells(
        self,
        matrix_id: str,
        keys: list[tuple[str, str]],
        *,
        fail_reason: str,
        fail_hint: str | None = None,
    ) -> int:
        """按键置 failed（timeout/invoke 异常路径）；status 守卫 queued/running。"""
        if fail_reason not in FAIL_REASON_VALUES:
            raise ValueError(f"fail_reason must be one of {FAIL_REASON_VALUES}")
        if not keys:
            return 0
        n = 0
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            for file_id, column_id in keys:
                seq = await self._bump_seq(db, matrix_id)
                cur = await db.execute(
                    "UPDATE kb_matrix_cells SET status = 'failed', fail_reason = ?, fail_hint = ?,"
                    " queued_from = NULL, seq = ?, updated_at = ?"
                    " WHERE matrix_id = ? AND file_id = ? AND column_id = ?"
                    " AND status IN ('queued', 'running') AND soft_deleted_at IS NULL",
                    (fail_reason, fail_hint, seq, now, matrix_id, file_id, column_id),
                )
                if cur.rowcount > 0:
                    n += 1
            await db.commit()
        return n

    # ─── cells: user write paths (§7.2 PATCH three ops) ───────

    async def edit_cell_value(
        self,
        matrix_id: str,
        file_id: str,
        column_id: str,
        *,
        value: Any,
        column: dict[str, Any],
        workspace_id: str,
        team_id: str,
        file_version: int = 1,
    ) -> MatrixCellDTO | None:
        """人工编辑格值：primary variant normalized 按列定义 wrap（M3 起收整列 def
        ——select/multi_select 归一化需要 options，review G-P2-7）+ edited=1；
        needs_review 清除（edit 后 status=done）；verified 清零（新值未经确认）。
        空格（无行）编辑 → lazy 建行。毒缓存收口路①（防御性，今天被 edited=1 掩住）：
        同事务 cache_key=NULL, cached=0。"""
        cell = await self.get_cell(matrix_id, file_id, column_id)
        if cell is not None and cell.workspace_id != workspace_id:
            return None  # S1 defense in depth (B-P2-9), same as keep/verify
        normalized = normalize_matrix_value(value, column)
        column_prompt_version = int(column.get("prompt_version") or 1)
        now = _now()
        if cell is None:
            variants: list[dict[str, Any]] = [
                {
                    "raw": None,
                    "normalized": normalized,
                    "effective_at": None,
                    "is_primary": True,
                    "extracted_by": "user_edit",
                    "confidence": None,
                    "source_excerpt": None,
                    "chunk_id": None,
                    "page": None,
                    "edited_by_user": True,
                }
            ]
            async with open_shared_ctx(self._db_path) as db:
                seq = await self._bump_seq(db, matrix_id)
                await db.execute(
                    "INSERT INTO kb_matrix_cells (matrix_id, file_id, column_id, workspace_id,"
                    " team_id, status, variants_json, edited_by_user, verified, prompt_version,"
                    " file_version, seq, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 'done', ?, 1, 0, ?, ?, ?, ?, ?)",
                    (
                        matrix_id,
                        file_id,
                        column_id,
                        workspace_id,
                        team_id,
                        json.dumps(variants, ensure_ascii=False),
                        int(column_prompt_version),
                        int(file_version),
                        seq,
                        now,
                        now,
                    ),
                )
                await db.commit()
        else:
            variants = [dict(v) for v in cell.variants]
            primary_idx = next((i for i, v in enumerate(variants) if v.get("is_primary")), -1)
            if primary_idx < 0:
                variants.insert(
                    0,
                    {
                        "raw": None,
                        "normalized": normalized,
                        "effective_at": None,
                        "is_primary": True,
                        "extracted_by": "user_edit",
                        "confidence": None,
                        "source_excerpt": None,
                        "chunk_id": None,
                        "page": None,
                        "edited_by_user": True,
                    },
                )
            else:
                variants[primary_idx]["normalized"] = normalized
                variants[primary_idx]["edited_by_user"] = True
            async with open_shared_ctx(self._db_path) as db:
                seq = await self._bump_seq(db, matrix_id)
                await db.execute(
                    "UPDATE kb_matrix_cells SET status = 'done', variants_json = ?, edited_by_user = 1,"
                    " verified = 0, cache_key = NULL, cached = 0, fail_reason = NULL, fail_hint = NULL, seq = ?, updated_at = ?"
                    " WHERE matrix_id = ? AND file_id = ? AND column_id = ? AND workspace_id = ? AND soft_deleted_at IS NULL",
                    (json.dumps(variants, ensure_ascii=False), seq, now, matrix_id, file_id, column_id, workspace_id),
                )
                await db.commit()
        return await self.get_cell(matrix_id, file_id, column_id)

    async def keep_cell_as_text(
        self,
        matrix_id: str,
        file_id: str,
        column_id: str,
        *,
        workspace_id: str,
    ) -> MatrixCellDTO | None:
        """保留为文本（FR-10）：needs_review 清除 + normalized=None、raw 保留原话
        （raw_string 哨兵值搬进 raw）；视同人工编辑（edited=1，PRD 拍板）。
        毒缓存收口路①（同 edit_cell_value）：同事务 cache_key=NULL, cached=0。"""
        cell = await self.get_cell(matrix_id, file_id, column_id)
        if cell is None or cell.workspace_id != workspace_id:
            return None
        variants = [dict(v) for v in cell.variants]
        primary_idx = next((i for i, v in enumerate(variants) if v.get("is_primary")), -1)
        if primary_idx < 0:
            return None
        primary = variants[primary_idx]
        norm = primary.get("normalized")
        if isinstance(norm, dict) and norm.get("raw_string") is not None:
            primary["raw"] = norm.get("raw_string")
        primary["normalized"] = None
        primary["edited_by_user"] = True
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            seq = await self._bump_seq(db, matrix_id)
            await db.execute(
                "UPDATE kb_matrix_cells SET status = 'done', variants_json = ?, edited_by_user = 1,"
                " verified = 0, cache_key = NULL, cached = 0, seq = ?, updated_at = ?"
                " WHERE matrix_id = ? AND file_id = ? AND column_id = ? AND soft_deleted_at IS NULL",
                (json.dumps(variants, ensure_ascii=False), seq, now, matrix_id, file_id, column_id),
            )
            await db.commit()
        return await self.get_cell(matrix_id, file_id, column_id)

    async def set_cell_verified(
        self,
        matrix_id: str,
        file_id: str,
        column_id: str,
        *,
        workspace_id: str,
        verified: bool = True,
    ) -> tuple[MatrixCellDTO | None, bool]:
        """标记已核 / 撤销已核（FR-12 r5）。true 仅 done 格；**false 不要求 done**
        ——force/stale 重跑圈进 verified 格、requeue 不触 verified、失败后
        failed+verified=1 真实可达，必须可撤（review G-P2-3 修正）。
        返回 (cell, changed)：changed=False 的 no-op（如对未核格 set false）
        不落审计（轮询竞态常见，虚审计更糟——tech-design-m3 §4.3）。"""
        cell = await self.get_cell(matrix_id, file_id, column_id)
        if cell is None or cell.workspace_id != workspace_id:
            return None, False
        if verified and cell.status != "done":
            raise ValueError("only done cells can be verified")
        if cell.verified == verified:
            return cell, False
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            seq = await self._bump_seq(db, matrix_id)
            await db.execute(
                "UPDATE kb_matrix_cells SET verified = ?, seq = ?, updated_at = ?"
                " WHERE matrix_id = ? AND file_id = ? AND column_id = ? AND soft_deleted_at IS NULL",
                (1 if verified else 0, seq, now, matrix_id, file_id, column_id),
            )
            await db.commit()
        return await self.get_cell(matrix_id, file_id, column_id), True

    async def revert_cell_edited(
        self,
        matrix_id: str,
        file_id: str,
        column_id: str,
        *,
        workspace_id: str,
    ) -> MatrixCellDTO | None:
        """撤销编辑标记（FR-9 r5）：清 edited + **cache_key/cached**（毒缓存收口路②
        ——撤销后 cell 重新满足缓存池条件 status=done & edited=0，cache_key 残留 =
        人工值顶着 AI 缓存键被其他矩阵命中复制）。variants/status/verified 不动：
        当前人工值保留为格值但不再受保护（此后批量可覆盖、不再 superseded 降级）；
        variant 级 edited_by_user 键保留为历史记录。未编辑格 → ValueError("not_edited")
        （route 映射 422，帮 FE 揪 stale UI）。"""
        cell = await self.get_cell(matrix_id, file_id, column_id)
        if cell is None or cell.workspace_id != workspace_id:
            return None
        if not cell.edited_by_user:
            raise ValueError("not_edited")
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            seq = await self._bump_seq(db, matrix_id)
            await db.execute(
                "UPDATE kb_matrix_cells SET edited_by_user = 0, cache_key = NULL, cached = 0,"
                " seq = ?, updated_at = ?"
                " WHERE matrix_id = ? AND file_id = ? AND column_id = ? AND soft_deleted_at IS NULL",
                (seq, now, matrix_id, file_id, column_id),
            )
            await db.commit()
        return await self.get_cell(matrix_id, file_id, column_id)

    # ─── cells: materialization writes (from_template §7.6) ───

    async def insert_cells_bulk(self, matrix_id: str, cells: list[dict[str, Any]]) -> int:
        """另存为矩阵物化专用：逐 cell INSERT（单事务逐格递增 seq）。
        caller 负责构造完整 cell dict（status/variants/edited/verified/prompt_version=1
        与列一致/file_version 取当前 kb_files.version；content_hash/cache_key 置 NULL）。"""
        if not cells:
            return 0
        now = _now()
        n = 0
        async with open_shared_ctx(self._db_path) as db:
            for c in cells:
                status = str(c["status"])
                if status not in CELL_STATUS_VALUES:
                    raise ValueError(f"cell status must be one of {CELL_STATUS_VALUES}")
                seq = await self._bump_seq(db, matrix_id)
                await db.execute(
                    "INSERT OR REPLACE INTO kb_matrix_cells (matrix_id, file_id, column_id,"
                    " workspace_id, team_id, status, variants_json, edited_by_user, verified,"
                    " prompt_version, file_version, seq, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        matrix_id,
                        str(c["file_id"]),
                        str(c["column_id"]),
                        str(c["workspace_id"]),
                        str(c["team_id"]),
                        status,
                        json.dumps(c.get("variants") or [], ensure_ascii=False),
                        1 if c.get("edited_by_user") else 0,
                        1 if c.get("verified") else 0,
                        int(c.get("prompt_version") or 1),
                        int(c.get("file_version") or 1),
                        seq,
                        now,
                        now,
                    ),
                )
                n += 1
            await db.commit()
        return n

    # ─── cells: read paths ──────────────────────────────────

    async def get_cell(self, matrix_id: str, file_id: str, column_id: str) -> MatrixCellDTO | None:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_matrix_cells WHERE matrix_id = ? AND file_id = ? AND column_id = ? AND soft_deleted_at IS NULL",
                (matrix_id, file_id, column_id),
            ) as cur:
                row = await cur.fetchone()
        return _cell_row_to_dto(row) if row else None

    async def list_cells(self, matrix_id: str) -> list[MatrixCellDTO]:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_matrix_cells WHERE matrix_id = ? AND soft_deleted_at IS NULL",
                (matrix_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_cell_row_to_dto(r) for r in rows]

    async def list_cells_since(self, matrix_id: str, since_seq: int) -> list[MatrixCellDTO]:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_matrix_cells WHERE matrix_id = ? AND seq > ? AND soft_deleted_at IS NULL",
                (matrix_id, int(since_seq)),
            ) as cur:
                rows = await cur.fetchall()
        return [_cell_row_to_dto(r) for r in rows]

    async def list_cells_for_run(self, run_id: str, *, statuses: tuple[str, ...] | None = None) -> list[MatrixCellDTO]:
        clauses = ["run_id = ?", "soft_deleted_at IS NULL"]
        params: list[Any] = [run_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM kb_matrix_cells WHERE {' AND '.join(clauses)}",
                params,
            ) as cur:
                rows = await cur.fetchall()
        return [_cell_row_to_dto(r) for r in rows]

    async def find_cached_cell(self, team_id: str, cache_key: str) -> MatrixCellDTO | None:
        """缓存命中（§5.6）：同 team 任意矩阵、done 且 edited=0（人改过的值不进缓存池）。"""
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_matrix_cells WHERE team_id = ? AND cache_key = ?"
                " AND status = 'done' AND edited_by_user = 0 AND soft_deleted_at IS NULL"
                " ORDER BY updated_at DESC LIMIT 1",
                (team_id, cache_key),
            ) as cur:
                row = await cur.fetchone()
        return _cell_row_to_dto(row) if row else None

    async def status_summary_for_matrix(self, matrix_id: str) -> dict[str, int]:
        """GET /matrices 列表的状态摘要（一条 GROUP BY 聚合，FR-1）。"""
        out = {"done": 0, "failed": 0, "needs_review": 0, "running": 0, "queued": 0}
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                "SELECT status, COUNT(*) FROM kb_matrix_cells WHERE matrix_id = ? AND soft_deleted_at IS NULL GROUP BY status",
                (matrix_id,),
            ) as cur,
        ):
            rows = await cur.fetchall()
        for status, count in rows:
            if status in out:
                out[str(status)] = int(count)
        return out

    async def purge_provenance_for_file(self, file_id: str) -> int:
        """FR-7 dormant 原语（§4.6）：剔除该 file 全部 cells 的 source_excerpt
        （含非 primary variant）与 fail_hint；normalized/value 保留。调用点留给
        未来 grace cron 接线（§0.3 合规债显式登记）；不 bump seq（cron 场景无
        live 客户端一致性诉求，如实登记）。"""
        n = 0
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT matrix_id, file_id, column_id, variants_json FROM kb_matrix_cells WHERE file_id = ?",
                (file_id,),
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                variants = _loads(row["variants_json"], [])
                changed = False
                for v in variants:
                    if isinstance(v, dict) and v.get("source_excerpt") is not None:
                        v["source_excerpt"] = None
                        changed = True
                    if isinstance(v, dict) and v.get("source_snippet_unverified") is not None:
                        v["source_snippet_unverified"] = None
                        changed = True
                await db.execute(
                    "UPDATE kb_matrix_cells SET variants_json = ?, fail_hint = NULL, updated_at = ?"
                    " WHERE matrix_id = ? AND file_id = ? AND column_id = ?",
                    (
                        json.dumps(variants, ensure_ascii=False) if changed else row["variants_json"],
                        now,
                        row["matrix_id"],
                        row["file_id"],
                        row["column_id"],
                    ),
                )
                n += 1
            await db.commit()
        return n

    # ─── runs（§4.3）────────────────────────────────────────

    async def create_run(
        self,
        *,
        matrix_id: str,
        workspace_id: str,
        team_id: str,
        scope: dict[str, Any],
        initiated_by: str,
        cells_total: int,
        skipped_edited: int,
        credits_estimated: int,
    ) -> MatrixRunDTO:
        run_id = new_run_id()
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "INSERT INTO kb_matrix_runs (run_id, matrix_id, workspace_id, team_id, scope_json,"
                " status, cells_total, skipped_edited, credits_estimated, initiated_by,"
                " started_at, heartbeat_at) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    matrix_id,
                    workspace_id,
                    team_id,
                    json.dumps(scope, ensure_ascii=False),
                    int(cells_total),
                    int(skipped_edited),
                    int(credits_estimated),
                    initiated_by,
                    now,
                    now,
                ),
            )
            await db.commit()
        got = await self.get_run(run_id)
        assert got is not None
        return got

    async def create_run_and_enqueue(
        self,
        *,
        matrix_id: str,
        workspace_id: str,
        team_id: str,
        scope: dict[str, Any],
        initiated_by: str,
        targets: list[tuple[str, str]],
        skipped_edited: int,
        credits_estimated: int,
        column_versions: dict[str, int],
        file_versions: dict[str, int],
    ) -> MatrixRunDTO | None:
        """§5.1 创建事务：INSERT run(running) + 目标 cells UPSERT(queued) 同事务
        （崩在中间不会留下无 cells 锚点的 run 或无 run 的 queued cells）。
        M3（tech-design-m3 决策 4）：cells_total = **实际入队数**（防御性收口——
        单进程锁内展开下与 len(targets) 恒等）；实际入队 0 → 整单回滚返回 None
        （空 run 横幅不闪现；engine 侧抛 no_pending_cells）。"""
        run_id = new_run_id()
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "INSERT INTO kb_matrix_runs (run_id, matrix_id, workspace_id, team_id, scope_json,"
                " status, cells_total, skipped_edited, credits_estimated, initiated_by,"
                " started_at, heartbeat_at) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    matrix_id,
                    workspace_id,
                    team_id,
                    json.dumps(scope, ensure_ascii=False),
                    len(targets),
                    int(skipped_edited),
                    int(credits_estimated),
                    initiated_by,
                    now,
                    now,
                ),
            )
            enqueued = await self._enqueue_cells_on_db(
                db,
                matrix_id,
                targets,
                run_id=run_id,
                workspace_id=workspace_id,
                team_id=team_id,
                column_versions=column_versions,
                file_versions=file_versions,
            )
            if enqueued == 0:
                await db.rollback()
                return None
            if enqueued != len(targets):
                await db.execute(
                    "UPDATE kb_matrix_runs SET cells_total = ? WHERE run_id = ?",
                    (int(enqueued), run_id),
                )
            await db.commit()
        got = await self.get_run(run_id)
        assert got is not None
        return got

    async def get_run(self, run_id: str) -> MatrixRunDTO | None:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_matrix_runs WHERE run_id = ?",
                (run_id,),
            ) as cur:
                row = await cur.fetchone()
        return _run_row_to_dto(row) if row else None

    # Batch run predicate (tech-design-m3 decision 7): single_cell exempt from per-matrix/ws caps,
    # absent from snapshot runs[]. scope_json is TEXT; filter via JSON1 json_extract (zero new columns/DDL);
    # COALESCE handles missing kind (treat as batch, count toward cap fail-closed).
    _BATCH_RUN_PREDICATE = "COALESCE(json_extract(scope_json, '$.kind'), '') != 'single_cell'"

    async def count_active_batch_runs_for_matrix(self, matrix_id: str) -> int:
        """per-matrix 批量 run 并发帽的计数（M3 替代单 run 409 闸；single_cell 豁免）。"""
        placeholders = ",".join("?" for _ in RUN_ACTIVE_STATUSES)
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                f"SELECT COUNT(*) FROM kb_matrix_runs WHERE matrix_id = ? AND status IN ({placeholders}) AND {self._BATCH_RUN_PREDICATE}",
                (matrix_id, *RUN_ACTIVE_STATUSES),
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_active_runs_for_workspace(self, workspace_id: str) -> int:
        """空间级活跃 run 帽计数（M3 起只计批量 run——single_cell 豁免，决策 7）。"""
        placeholders = ",".join("?" for _ in RUN_ACTIVE_STATUSES)
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                f"SELECT COUNT(*) FROM kb_matrix_runs WHERE workspace_id = ? AND status IN ({placeholders}) AND {self._BATCH_RUN_PREDICATE}",
                (workspace_id, *RUN_ACTIVE_STATUSES),
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def list_runs_for_snapshot(self, matrix_id: str, *, terminal_limit: int = 5) -> list[MatrixRunDTO]:
        """快照 runs[]（tech-design-m3 决策 5）：活跃 runs + 最近 ``terminal_limit`` 条
        终态 runs（多 run 间隔完成时，后完成者不挤掉前者未 dismiss 的失败横幅——
        review PRD-P1-1），single_cell 排除（决策 7：不进横幅栈），全局按
        started_at DESC, run_id DESC 排序（``run`` 兼容字段 = runs[0]）。"""
        active_ph = ",".join("?" for _ in RUN_ACTIVE_STATUSES)
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM kb_matrix_runs WHERE matrix_id = ? AND {self._BATCH_RUN_PREDICATE}"
                f" AND (status IN ({active_ph}) OR run_id IN ("
                f"   SELECT run_id FROM kb_matrix_runs WHERE matrix_id = ?"
                f"   AND status IN ('succeeded', 'failed', 'cancelled') AND {self._BATCH_RUN_PREDICATE}"
                f"   ORDER BY started_at DESC, run_id DESC LIMIT ?))"
                " ORDER BY started_at DESC, run_id DESC",
                (matrix_id, *RUN_ACTIVE_STATUSES, matrix_id, int(terminal_limit)),
            ) as cur:
                rows = await cur.fetchall()
        return [_run_row_to_dto(r) for r in rows]

    async def touch_run_heartbeat(self, run_id: str) -> None:
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "UPDATE kb_matrix_runs SET heartbeat_at = ? WHERE run_id = ?",
                (_now(), run_id),
            )
            await db.commit()

    async def increment_run_counters(
        self,
        run_id: str,
        *,
        done: int = 0,
        failed: int = 0,
        cached: int = 0,
        tokens_input: int = 0,
        tokens_output: int = 0,
        credits_delta: int = 0,
    ) -> None:
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "UPDATE kb_matrix_runs SET cells_done = cells_done + ?, cells_failed = cells_failed + ?,"
                " cells_cached = cells_cached + ?, tokens_input = tokens_input + ?,"
                " tokens_output = tokens_output + ?, credits_charged = credits_charged + ?,"
                " heartbeat_at = ? WHERE run_id = ?",
                (int(done), int(failed), int(cached), int(tokens_input), int(tokens_output), int(credits_delta), _now(), run_id),
            )
            await db.commit()

    async def adjust_run_cells_total(self, run_id: str, delta: int) -> None:
        """投递前复查文件不可用 → 跳过该行（不算 failed，从 cells_total 剔除，§4.6）。"""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "UPDATE kb_matrix_runs SET cells_total = cells_total + ? WHERE run_id = ?",
                (int(delta), run_id),
            )
            await db.commit()

    async def update_run_billing(
        self,
        run_id: str,
        *,
        credits_charged: int,
        tokens_input: int,
        tokens_output: int,
    ) -> None:
        """run 收敛时从 ledger 反查回填（§6.2，对账唯一真相 = ledger；覆盖 in-loop 累加）。"""
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                "UPDATE kb_matrix_runs SET credits_charged = ?, tokens_input = ?, tokens_output = ? WHERE run_id = ?",
                (int(credits_charged), int(tokens_input), int(tokens_output), run_id),
            )
            await db.commit()

    async def set_run_status(
        self,
        run_id: str,
        status: str,
        *,
        error_code: str | None = None,
        expected_from: tuple[str, ...] | None = None,
    ) -> bool:
        """状态迁移（§4.3 状态机）。``expected_from`` 提供原子 CAS 守卫——
        freeze 不得覆盖 cancelling（review P1）由 caller 传 expected_from=("running",)
        兜死：已是 cancelling 时 rowcount=0 返 False，引擎走取消收敛。"""
        if status not in RUN_STATUS_VALUES:
            raise ValueError(f"run status must be one of {RUN_STATUS_VALUES}")
        now = _now()
        finished_at = now if status in ("succeeded", "failed", "cancelled") else None
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if expected_from is not None:
            placeholders = ",".join("?" for _ in expected_from)
            clauses.append(f"status IN ({placeholders})")
            params.extend(expected_from)
        async with open_shared_ctx(self._db_path) as db:
            if finished_at is not None:
                cur = await db.execute(
                    f"UPDATE kb_matrix_runs SET status = ?, error_code = COALESCE(?, error_code),"
                    f" finished_at = ?, heartbeat_at = ? WHERE {' AND '.join(clauses)}",
                    (status, error_code, finished_at, now, *params),
                )
            else:
                cur = await db.execute(
                    f"UPDATE kb_matrix_runs SET status = ?, error_code = COALESCE(?, error_code), heartbeat_at = ? WHERE {' AND '.join(clauses)}",
                    (status, error_code, now, *params),
                )
            await db.commit()
            return cur.rowcount > 0

    async def claim_resumable_runs(self) -> list[MatrixRunDTO]:
        """boot resume 第一步**原子认领**（review P1）：单条 UPDATE 把全部待恢复
        run 的 heartbeat 顶到当前——否则 resume 慢时 zombie scanner 会把正在被
        恢复的 run 判死，双方对同一 run 竞写。随后返回待恢复清单。"""
        now = _now()
        placeholders = ",".join("?" for _ in RUN_ACTIVE_STATUSES)
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                f"UPDATE kb_matrix_runs SET heartbeat_at = ? WHERE status IN ({placeholders})",
                (now, *RUN_ACTIVE_STATUSES),
            )
            await db.commit()
            async with db.execute(
                f"SELECT * FROM kb_matrix_runs WHERE status IN ({placeholders}) ORDER BY started_at",
                RUN_ACTIVE_STATUSES,
            ) as cur:
                rows = await cur.fetchall()
        return [_run_row_to_dto(r) for r in rows]

    async def list_zombie_candidate_runs(self, threshold_s: int) -> list[MatrixRunDTO]:
        """status='running' 且心跳超阈值的 run（scanner 侧再排除有活 task 的 run）。"""
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM kb_matrix_runs WHERE status = 'running' AND datetime(heartbeat_at) < datetime('now', '-{int(threshold_s)} seconds')",
            ) as cur:
                rows = await cur.fetchall()
        return [_run_row_to_dto(r) for r in rows]

    # ─── internal (seq transaction primitives, §4.4) ─────────

    async def _enqueue_cells_on_db(
        self,
        db: aiosqlite.Connection,
        matrix_id: str,
        targets: list[tuple[str, str]],
        *,
        run_id: str,
        workspace_id: str,
        team_id: str,
        column_versions: dict[str, int],
        file_versions: dict[str, int],
    ) -> int:
        """入队核心（不 commit——caller 决定事务边界）。幂等：queued/running 同键
        跳过；存量行不覆盖 prompt_version/file_version（取消回退要还原旧值语义，
        执行版本由 finalize_cell 落）。"""
        db.row_factory = aiosqlite.Row
        now = _now()
        enqueued = 0
        for file_id, column_id in targets:
            async with db.execute(
                "SELECT status, soft_deleted_at FROM kb_matrix_cells WHERE matrix_id = ? AND file_id = ? AND column_id = ?",
                (matrix_id, file_id, column_id),
            ) as cur:
                row = await cur.fetchone()
            idem = f"{file_id}:{column_id}:{int(column_versions.get(column_id, 1))}"
            if row is None:
                seq = await self._bump_seq(db, matrix_id)
                await db.execute(
                    "INSERT INTO kb_matrix_cells (matrix_id, file_id, column_id, workspace_id,"
                    " team_id, status, queued_from, variants_json, prompt_version, file_version,"
                    " idempotency_key, run_id, seq, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 'queued', 'empty', '[]', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        matrix_id,
                        file_id,
                        column_id,
                        workspace_id,
                        team_id,
                        int(column_versions.get(column_id, 1)),
                        int(file_versions.get(file_id, 1)),
                        idem,
                        run_id,
                        seq,
                        now,
                        now,
                    ),
                )
                enqueued += 1
                continue
            if row["soft_deleted_at"] is None and row["status"] in ("queued", "running"):
                continue  # Idempotent: skip in-flight key
            # Reviving soft-deleted row is empty (cancel → delete row); live row keeps old state for cancel rollback.
            queued_from = "empty" if row["soft_deleted_at"] is not None else str(row["status"])
            if queued_from not in QUEUED_FROM_VALUES:
                queued_from = "empty"
            variants_reset = ", variants_json = '[]', edited_by_user = 0, verified = 0, cached = 0" if row["soft_deleted_at"] is not None else ""
            seq = await self._bump_seq(db, matrix_id)
            await db.execute(
                "UPDATE kb_matrix_cells SET status = 'queued', queued_from = ?, run_id = ?,"
                f" idempotency_key = ?, soft_deleted_at = NULL, seq = ?, updated_at = ?{variants_reset}"
                " WHERE matrix_id = ? AND file_id = ? AND column_id = ?",
                (queued_from, run_id, idem, seq, now, matrix_id, file_id, column_id),
            )
            enqueued += 1
        return enqueued

    async def _revert_queued_cell_on_db(
        self,
        db: aiosqlite.Connection,
        matrix_id: str,
        file_id: str,
        column_id: str,
        queued_from: str | None,
        now: str,
    ) -> None:
        """单格回退核心（不 commit）。empty=删行（lazy 建的行；行删除靠增量协议
        无法表达，seq 仍推进 → 客户端拿到 run 终态后全量收尾），其余恢复原态
        （variants 未被入队触碰，原样保留）。"""
        origin = queued_from or "empty"
        if origin == "empty":
            await self._bump_seq(db, matrix_id)
            await db.execute(
                "DELETE FROM kb_matrix_cells WHERE matrix_id = ? AND file_id = ? AND column_id = ?",
                (matrix_id, file_id, column_id),
            )
        else:
            seq = await self._bump_seq(db, matrix_id)
            await db.execute(
                "UPDATE kb_matrix_cells SET status = ?, queued_from = NULL, run_id = NULL, seq = ?, updated_at = ?"
                " WHERE matrix_id = ? AND file_id = ? AND column_id = ?",
                (origin, seq, now, matrix_id, file_id, column_id),
            )

    async def _bump_seq(self, db: aiosqlite.Connection, matrix_id: str, *, structure: bool = False) -> int:
        """唯一 seq 递增原语：调用方保证与 cell 写在同一连接/事务内。"""
        now = _now()
        await db.execute(
            "UPDATE kb_matrices SET last_seq = last_seq + 1, updated_at = ? WHERE matrix_id = ?",
            (now, matrix_id),
        )
        async with db.execute(
            "SELECT last_seq FROM kb_matrices WHERE matrix_id = ?",
            (matrix_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise ValueError(f"matrix not found: {matrix_id}")
        seq = int(row[0])
        if structure:
            await db.execute(
                "UPDATE kb_matrices SET structure_seq = last_seq WHERE matrix_id = ?",
                (matrix_id,),
            )
        return seq
