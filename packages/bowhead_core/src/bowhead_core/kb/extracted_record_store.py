"""KbExtractedRecord store — workspace 级 抽取记录 (PDF→fields_json) CRUD + SQL query.

kb-knowledge-tables sprint v1.0 §1.2 (tech design).

每条 record = 1 file × 1 doc_type 抽出来的 1 行 "虚拟表格" (合同金额=500w / 起始日=2024-01-15 / ...).
fields_json shape (per Spike 4 验证):
  {
    "合同金额": {"value": 5000000, "source_snippet": "本协议合同总金额为人民币...",
                "source_page": 1, "confidence": 0.95},
    "起始日": {"value": "2024-01-15", "source_snippet": "...", ...},
    ...
  }

V1 cap (P1-6-2): records / doc_type ≤ 1000 (sqlite json_extract perf 实测 < 1ms with index, 但留余量).

P0-R1 (cross-ws leak fix): list_for_doc_type / query_by_field 强制 workspace_id 过滤.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiosqlite

from bowhead_core.kb.telemetry import emit_audit

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# kb-knowledge-tables v2 sprint: row-count threshold — above this, ALTER TABLE
# may block startup beyond health-check window. tech-design §A.5 pre-ship gate.
V2_MIGRATION_ROW_COUNT_THRESHOLD = 10_000

# V1 cap: 1000 records / doc_type (json_extract perf, prevent sqlite overload)
MAX_RECORDS_PER_DOC_TYPE = 1000

VERIFY_STATUS_VALUES = ("ready", "needs_review", "approved", "extract_failed")


class RecordAlreadyExistsError(Exception):
    """Sprint 2 kb-structured-tables Phase B (I-B4): raised by `create()` when
    ``dedup_check=True`` (default) and a non-soft-deleted record already exists
    for the (file_id, doc_type_id) tuple.

    Phase B worker `_run_auto_classify_and_extract` catches this + emits
    `kb.extract_duplicate_skipped` audit + skips re-insert. Backfill admin
    endpoint does the same.
    """

    def __init__(self, file_id: str, doc_type_id: str, existing_record_id: str | None = None) -> None:
        self.file_id = file_id
        self.doc_type_id = doc_type_id
        self.existing_record_id = existing_record_id
        super().__init__(
            f"record exists for file_id={file_id} doc_type_id={doc_type_id}"
            + (f" (existing record_id={existing_record_id})" if existing_record_id else ""),
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_extracted_records (
    record_id                   TEXT PRIMARY KEY,
    doc_type_id                 TEXT NOT NULL,
    file_id                     TEXT NOT NULL,
    workspace_id                TEXT NOT NULL,
    -- v2: {field_name: [variant, ...]}  (variant array)
    -- variant = {raw, normalized, effective_at, is_primary, extracted_by,
    --           confidence, source_excerpt, chunk_id, page, edited_by_user?}
    fields_json                 TEXT NOT NULL DEFAULT '{}',
    -- 'ready' / 'needs_review' / 'approved' / 'extract_failed' (Sprint 2 D.1 adds last)
    verify_status               TEXT NOT NULL DEFAULT 'ready',
    -- 哪些字段被用户手动改过 (edited_by_user = ["合同金额", "起始日"]).
    -- LLM re-extract 时该字段保留用户值不覆盖.
    edited_by_user_json         TEXT NOT NULL DEFAULT '[]',
    extractor_model             TEXT NOT NULL,
    extraction_duration_ms      INTEGER,
    extracted_at                TEXT NOT NULL,
    last_modified_at            TEXT NOT NULL,
    last_modified_by            TEXT NOT NULL,
    soft_deleted_at             TEXT,
    -- v2: schema version (1 = V1 single-value legacy, 2 = v2 variant array)
    schema_version              INTEGER NOT NULL DEFAULT 2,
    -- v2: backup of V1 fields_json on lazy-migrate (30-day retention)
    fields_json_legacy          TEXT
);
CREATE INDEX IF NOT EXISTS idx_kb_records_dt
    ON kb_extracted_records(doc_type_id, workspace_id) WHERE soft_deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_kb_records_file
    ON kb_extracted_records(file_id) WHERE soft_deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_kb_records_ws_extracted
    ON kb_extracted_records(workspace_id, extracted_at) WHERE soft_deleted_at IS NULL;
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


# ─── kb-knowledge-tables v2 sprint: V1 → v2 wrappers (tech-design §A.2 + §A.7) ──

# Currency / number markup chars stripped by structured_extractor._coerce_value.
# Mirror its regex so I-A6 guard below catches partial-numeric strings
# ("约100元" type LLM mid-emit) — _coerce would happily return 100, but the val
# contains "约" residual which means semantic uncertainty.
_CURRENCY_MARKERS_RE = re.compile(r"[¥￥,，\s元万RMB]", re.IGNORECASE)
_PURE_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _v1_value_to_normalized(val: Any, type_hint: str) -> dict[str, Any] | None:
    """V1 single-value → v2 ``normalized`` dict per tech-design §A.2 step 5.

    Reuses ``structured_extractor._coerce_value`` for Chinese-currency parsing
    ("RMB五百万" / "5,000,000" / "万元" / "万").

    Contract:
      - None → None
      - For currency/number type_hint: try numeric coerce.
        - On success (int/float) **AND** the original string is purely
          numeric+markup (no residual non-numeric chars like "约" / "大约"):
          {"amount": "<as_str>", "currency": None}
        - On parse failure OR residual non-numeric content: {"raw_string": val}
          per I-A6 (no destructive normalize — LLM mid-emit like "约100元" must
          NOT become a confirmed 100 value).
      - For date / string / fallback type_hint: {"value": str(val)}

    Known _coerce_value limitations (Sprint 2 fix candidates): scientific
    notation ("1.5e6"), suffix abbreviations ("100K"), USD/EUR currency suffix
    not captured into normalized.currency. Current code returns raw_string
    fallback for these via the I-A6 residual guard below.
    """
    if val is None:
        return None

    if type_hint in ("currency", "number"):
        # Lazy import to avoid circular dependency on structured_extractor module
        # at import time (extractor imports from doc_type_store which is a sibling).
        from bowhead_core.kb.structured_extractor import (
            _coerce_value as _coerce,
        )

        coerced = _coerce(val, type_hint)
        if isinstance(coerced, (int, float)):
            # I-A6 residual guard: only accept numeric coerce when the original
            # string (after stripping currency markup) is purely a number. This
            # blocks "约100元" / "大约 5000" / "100 (估算)" from becoming
            # confirmed amounts.
            if isinstance(val, str):
                residual = _CURRENCY_MARKERS_RE.sub("", val).strip()
                if not _PURE_NUMBER_RE.fullmatch(residual):
                    return {"raw_string": val}
            return {"amount": str(coerced), "currency": None}
        # Parse failed — raw_string fallback (I-A6: no destructive normalize).
        return {"raw_string": str(val)}

    # date / string / unknown → keep ISO/text under .value (matches §A.6 COALESCE
    # second arm json_extract(...[0].normalized.value)).
    return {"value": str(val)}


# kb-matrix M2 §8⑦: public re-export—matrix store column graduation re-coercion reuses the same
# normalization contract (including I-A6 residual guard), avoiding cross-module private imports.
v1_value_to_normalized = _v1_value_to_normalized


# ─── kb-matrix M4 FR-42: shared normalization for three new types (select/multi_select/boolean) ──
# One implementation, two consumers (doc_type extraction wrap + matrix normalize_matrix_value delegation)—
# moved unchanged from matrix_store (M3); matrix retains compatible re-export; zero semantic changes.

# Boolean normalization vocabulary (matched after casefold).
_BOOL_TRUE_TOKENS = frozenset({"是", "true", "yes", "y", "有", "1"})
_BOOL_FALSE_TOKENS = frozenset({"否", "false", "no", "n", "无", "0"})


def _fold_option(text: str) -> str:
    """选项匹配折叠：casefold + NFKC 全半角 + 空白压缩（次级匹配）。"""
    import unicodedata

    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _match_option(value: Any, options: list[str]) -> str | None:
    """select 值匹配：精确 → 折叠匹配；返回 options 中的原文（值域收敛到定义）。"""
    text = str(value).strip()
    if not text:
        return None
    for opt in options:
        if text == opt:
            return opt
    folded = _fold_option(text)
    for opt in options:
        if folded == _fold_option(opt):
            return opt
    return None


def _split_multi_value(value: Any) -> list[str]:
    """multi_select 输入容错：真 list / list 字面量串 / 顿号逗号分号分隔串 / 单值。

    真栈 dogfood 逮出（M3）：multi_select 降解为 string 后，extractor 的
    `_coerce_value(list, "string")` 会把模型返的 JSON 数组 `str()` 成 **Python repr
    单引号串** `['星舰 Pro', '星舰 Air']`——只认 JSON（双引号）会漏解落哨兵。故 `[` 分支
    JSON 解析后再 `ast.literal_eval` 兜底（安全，仅解字面量），双引号/单引号数组都收。"""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        import ast

        for parse in (json.loads, ast.literal_eval):
            try:
                parsed = parse(text)
            except (TypeError, ValueError, SyntaxError):
                continue
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
    split_re = re.compile(r"[、，,;；/]")
    return [p.strip() for p in split_re.split(text) if p.strip()]


def normalize_field_value(
    value: Any,
    type_: str,
    options: list[str] | None = None,
) -> dict[str, Any] | None:
    """字段值归一化单一权威（M4 FR-42）：select/multi_select/boolean 三型自持，
    question 降解 string，存量四型委托 ``_v1_value_to_normalized``（含 I-A6
    residual guard，行为逐字节不变）。不匹配 → ``{"raw_string": …}`` 哨兵。"""
    if value is None:
        return None
    if type_ == "select":
        matched = _match_option(value, options or [])
        return {"choice": matched} if matched is not None else {"raw_string": value}
    if type_ == "multi_select":
        opts = list(options or [])
        items = _split_multi_value(value)
        if not items:
            return {"raw_string": value}
        matched_items = [_match_option(item, opts) for item in items]
        if any(m is None for m in matched_items):
            return {"raw_string": value}
        # Deduplicate in options definition order (chip display order SSOT = options order).
        chosen = {m for m in matched_items if m is not None}
        return {"choices": [o for o in opts if o in chosen]}
    if type_ == "boolean":
        token = str(value).strip().casefold()
        if token in _BOOL_TRUE_TOKENS:
            return {"flag": True}
        if token in _BOOL_FALSE_TOKENS:
            return {"flag": False}
        if isinstance(value, bool):
            return {"flag": value}
        return {"raw_string": value}
    eff_type = type_ if type_ != "question" else "string"
    return _v1_value_to_normalized(value, eff_type)


def _build_field_type_map(doc_type_fields: Any) -> dict[str, str]:
    """Extract {field_name: type} from a list of ExtractField-like objects
    (dataclass OR dict). Default type 'string'.

    ⚠ 形状冻结 {name: type_str}——除两处 wrap 外还有索引重建/query cast 消费者
    （M4 曾误扩形状把索引重建打哑，UT 逮回）。需要 options 的用
    ``_build_field_def_map``。"""
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


def _build_field_def_map(doc_type_fields: Any) -> dict[str, dict[str, Any]]:
    """M4 FR-42：{field_name: {"type": str, "options": list|None}}——仅两处 wrap
    消费（select/multi_select 归一化需要 options）；`_build_field_type_map`
    形状冻结不动（见其 docstring）。"""
    out: dict[str, dict[str, Any]] = {}
    if not doc_type_fields:
        return out
    for f in doc_type_fields:
        if isinstance(f, dict):
            name = f.get("name")
            type_ = f.get("type") or "string"
            options = f.get("options")
        else:
            name = getattr(f, "name", None)
            type_ = getattr(f, "type", "string") or "string"
            options = getattr(f, "options", None)
        if name:
            out[name] = {
                "type": type_,
                "options": list(options) if isinstance(options, list) and options else None,
            }
    return out


def _wrap_v1_to_v2(
    v1_fields: dict[str, dict[str, Any]],
    doc_type_fields: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    """V1 single-value ``fields_json`` → v2 variant array (lazy migrate read path).

    Per tech-design §A.2. Preserves V1 ``alternative_value`` as 2nd variant
    (is_primary=false). Primary variant carries extracted_by="tier1_legacy" so
    downstream can distinguish legacy-wrap from fresh tier1 extract.
    """
    type_by_name = _build_field_def_map(doc_type_fields)
    out: dict[str, list[dict[str, Any]]] = {}
    for fname, fdata in v1_fields.items():
        if not isinstance(fdata, dict):
            continue
        fdef = type_by_name.get(fname) or {"type": "string", "options": None}
        type_hint = str(fdef["type"])
        primary = {
            "raw": None,
            "normalized": normalize_field_value(fdata.get("value"), type_hint, fdef["options"]),
            "effective_at": None,
            "is_primary": True,
            "extracted_by": "tier1_legacy",
            "confidence": fdata.get("confidence"),
            "source_excerpt": fdata.get("source_snippet"),
            "chunk_id": None,  # §A.3 deferred — schema field-pos only
            "page": fdata.get("source_page"),
        }
        variants = [primary]
        alt = fdata.get("alternative_value")
        if alt is not None and alt != fdata.get("value"):
            variants.append(
                {
                    "raw": None,
                    "normalized": normalize_field_value(alt, type_hint, fdef["options"]),
                    "effective_at": None,
                    "is_primary": False,
                    "extracted_by": "tier1_legacy_alt",
                    "confidence": None,
                    "source_excerpt": None,
                    "chunk_id": None,
                    "page": None,
                }
            )
        out[fname] = variants
    return out


def wrap_extractor_result_to_variants(
    fields: dict[str, dict[str, Any]],
    doc_type_fields: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    """Worker-boundary wrapper for fresh extractor output → v2 1-variant array.

    Per tech-design §A.7. Always emits exactly 1 variant per field, is_primary=true,
    extracted_by="tier1" (NOT _legacy — this is a fresh LLM extract).

    Provenance path B (kb-optimization): chunk_id is read from ``fdata`` when the
    worker's backfill (structured_extractor.backfill_chunk_ids) located an origin
    chunk; stays None when there's no source_snippet match (unchanged behavior).
    """
    type_by_name = _build_field_def_map(doc_type_fields)
    out: dict[str, list[dict[str, Any]]] = {}
    for fname, fdata in fields.items():
        if not isinstance(fdata, dict):
            continue
        fdef = type_by_name.get(fname) or {"type": "string", "options": None}
        type_hint = str(fdef["type"])
        variant = {
            "raw": None,
            "normalized": normalize_field_value(fdata.get("value"), type_hint, fdef["options"]),
            "effective_at": None,
            "is_primary": True,
            "extracted_by": "tier1",
            "confidence": fdata.get("confidence"),
            "source_excerpt": fdata.get("source_snippet"),
            "chunk_id": fdata.get("chunk_id"),  # provenance path B: backfilled by worker, else None
            "page": fdata.get("source_page"),
        }
        # Carry cross-verify evidence (kb-matrix M3 FR-39, additive): agree was previously dropped at this boundary;
        # reduced confidence was the only cv evidence surviving in persisted variants. Records also benefit
        # (doc_type cv production enabled); a dormant key when there are no consumers.
        if "self_consistency_agree" in fdata:
            variant["self_consistency_agree"] = fdata.get("self_consistency_agree")
        out[fname] = [variant]
    return out


def _safe_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


# ─── kb-knowledge-tables v2 sprint: per-field expression indexes (§A.6) ─────


_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9]+")
# Field types that benefit from numeric expression index — see
# ``doc_type_store.FIELD_TYPES`` (identify by symbol, line numbers drift; M4 includes select/multi_select/
# boolean, all nonnumeric and excluded from this table). Only currency + number get the numeric
# CAST(... AS REAL) hot path.
_NUMERIC_INDEX_TYPES = ("currency", "number")


def _hash_id(s: str) -> str:
    """8-char SHA1 hex prefix — stable identifier for doc_type_id / field name."""
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


def _hash_field_name(name: str) -> str:
    """ASCII whitelist prefix (≤16) + SHA1[:8] suffix for SQL identifier safety.

    Examples:
      "amount" → "amount_<8hex>"
      "合同金额" → "_<8hex>" (all-Unicode stripped)
      "金额-RMB" → "_RMB_<8hex>"
      "金额 RMB" → "_RMB_<diff-8hex>"  (hyphen vs space yields distinct hash)
    """
    ascii_part = _SAFE_NAME_RE.sub("_", name).strip("_")[:16]
    hash_part = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{ascii_part}_{hash_part}" if ascii_part else f"_{hash_part}"


async def _rebuild_field_indexes(
    db_path: str | Path,
    doc_type_id: str,
    fields: Any = None,
) -> None:
    """DROP old + CREATE new expression indexes for a doc_type's numeric fields.

    Per tech-design §A.6: each currency/number field gets an expression index on
    ``CAST(json_extract(fields_json, '$.<name>[0].normalized.amount') AS REAL)``
    so cast_numeric query_by_field SQL hits the index instead of a full scan.

    Args:
      db_path: path to ``kb_extracted_records.db`` (records DB, not doc_types DB).
      doc_type_id: scope. All indexes named ``idx_kb_records_fld_<dt_hash>_*``.
      fields: list of ExtractField-like (must have .name, .type). None or empty
        list → drop-only path (used by soft_delete).

    Failure handling: wraps in try/except + audit emit ``kb.field_index_failed``
    so doc_type CRUD doesn't crash if the records DB is unavailable / locked.
    """
    db_path_str = str(db_path)
    dt_hash = _hash_id(doc_type_id)
    try:
        async with aiosqlite.connect(db_path_str) as db:
            # IntelliVault audit learning (Step 5 review): wrap DROP+CREATE in
            # BEGIN IMMEDIATE so partial fail (DROP succeeded, CREATE crashed)
            # doesn't leave the field un-indexed mid-transaction.
            await db.execute("BEGIN IMMEDIATE")
            # 1. DROP all indexes for this doc_type (whether dropping all or
            #    rebuilding — easier than diff-by-name).
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE ?",
                (f"idx_kb_records_fld_{dt_hash}_%",),
            ) as cur:
                old_names = [row[0] for row in await cur.fetchall()]
            for old in old_names:
                await db.execute(f"DROP INDEX IF EXISTS {old}")

            # 2. CREATE new indexes per numeric field (skip if fields=None/empty).
            if fields:
                type_map = _build_field_type_map(fields)
                for fname, ftype in type_map.items():
                    if ftype not in _NUMERIC_INDEX_TYPES:
                        continue
                    _validate_field_name(fname)  # blocks SQL/JSON-path injection
                    fname_hash = _hash_field_name(fname)
                    idx_name = f"idx_kb_records_fld_{dt_hash}_{fname_hash}_v2"
                    # Field name is whitelist-validated (see _validate_field_name)
                    # so direct interpolation into json path is safe. SQL-string
                    # escape just-in-case (single-quote-escape).
                    path = fname.replace("'", "''")
                    await db.execute(
                        f"CREATE INDEX IF NOT EXISTS {idx_name} ON "
                        f"kb_extracted_records ("
                        f"  workspace_id, doc_type_id, "
                        f"  CAST(json_extract(fields_json, "
                        f"      '$.{path}[0].normalized.amount') AS REAL)"
                        f") WHERE soft_deleted_at IS NULL"
                    )
            await db.commit()
    except Exception as exc:
        logger.exception(
            "kb _rebuild_field_indexes failed (db=%s, doc_type=%s): %s",
            db_path_str,
            doc_type_id,
            exc,
        )
        emit_audit(
            "kb.field_index_failed",
            {
                "db_path": db_path_str,
                "doc_type_id": doc_type_id,
                "error": str(exc),
            },
        )
        # Don't re-raise: doc_type CRUD path must not crash on index hook failure.
        # COALESCE SQL still works without the expression index (just slower).


def _coalesce_field_expr(field_name: str) -> str:
    """Build a SQL ``COALESCE`` expression that reads the field value from a
    v2 variant array (primary variant), falling back to V1 single-value shape.

    5-arm chain（M4 FR-42 追加 ③④，additive——存量列在 ①② 即命中，新 arm 永不触发，
    逐字节零回归；``.choices`` 数组**不进** COALESCE——聚合走 json_each 或降级）：
      1. v2 currency/number: ``$.{f}[0].normalized.amount``
      2. v2 date/string:     ``$.{f}[0].normalized.value``
      3. v2 select:          ``$.{f}[0].normalized.choice``
      4. v2 boolean:         ``$.{f}[0].normalized.flag``（json_extract → 1/0）
      5. V1 fallback:        ``$.{f}.value``

    本函数是 records/rows 两库的单一权威（M4 收敛——两份此前逐字节相同）；
    rows 侧经公开别名 ``coalesce_field_expr`` 引用。Caller already validates
    ``field_name`` against ``_validate_field_name`` — no SQL-injection risk.
    """
    return (
        "COALESCE("
        f"json_extract(fields_json, '$.{field_name}[0].normalized.amount'), "
        f"json_extract(fields_json, '$.{field_name}[0].normalized.value'), "
        f"json_extract(fields_json, '$.{field_name}[0].normalized.choice'), "
        f"json_extract(fields_json, '$.{field_name}[0].normalized.flag'), "
        f"json_extract(fields_json, '$.{field_name}.value')"
        ")"
    )


# M4 convergence: rows store reuses the same expression (previous copies byte-identical); public alias avoids private imports.
coalesce_field_expr = _coalesce_field_expr


@dataclass
class KbExtractedRecordDTO:
    """单条 record (一份 PDF 抽出来的虚拟表格行).

    v2 shape (per tech-design §A.1): fields[name] is a variant array.
    Each variant = {raw, normalized, effective_at, is_primary, extracted_by,
                    confidence, source_excerpt, chunk_id, page, edited_by_user?}
    """

    record_id: str
    doc_type_id: str
    file_id: str
    workspace_id: str
    # v2: {field_name: [variant, ...]}.
    fields: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    verify_status: str = "ready"
    # Field names the user has manually edited (LLM re-extract preserves these).
    edited_by_user: list[str] = field(default_factory=list)
    extractor_model: str = "qwen-plus"
    extraction_duration_ms: int | None = None
    extracted_at: str = ""
    last_modified_at: str = ""
    last_modified_by: str = ""
    # v2 metadata (defaults safe for fresh code reading legacy stored objects).
    schema_version: int = 2

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "doc_type_id": self.doc_type_id,
            "file_id": self.file_id,
            "workspace_id": self.workspace_id,
            "fields": self.fields,
            "verify_status": self.verify_status,
            "edited_by_user": list(self.edited_by_user),
            "extractor_model": self.extractor_model,
            "extraction_duration_ms": self.extraction_duration_ms,
            "extracted_at": self.extracted_at,
            "last_modified_at": self.last_modified_at,
            "last_modified_by": self.last_modified_by,
            "schema_version": self.schema_version,
        }


def _row_to_dto(
    row: aiosqlite.Row,
    doc_type_fields: Any = None,
) -> KbExtractedRecordDTO:
    """Convert SQLite row → DTO with v2 shape.

    Per tech-design §A.2 step 7 (r3 contract): caller MAY pass ``doc_type_fields``
    so V1 lazy-migrate gets correct type_hint per field (currency → .normalized.amount,
    not .normalized.value). When None, V1 rows degrade to string shape — COALESCE
    SQL (§A.6) still recovers value via V1 fallback arm; only loses semantic type.

    Defensive reads: pre-migration rows may lack ``schema_version`` /
    ``edited_by_user_json`` cols (test fixtures bypass lifespan migration).
    """
    row_keys = set(row.keys()) if hasattr(row, "keys") else set()

    fields_data = _safe_loads(row["fields_json"], {})
    if not isinstance(fields_data, dict):
        fields_data = {}

    # schema_version dispatch — defensive: col may be absent in old test fixtures.
    sv = row["schema_version"] if "schema_version" in row_keys else None
    if sv == 2:
        # v2 row: fields_json already variant array shape.
        fields_v2 = fields_data
    else:
        # V1 row (or unmarked): lazy wrap to v2 shape. Type-hint via doc_type_fields
        # (None → string default; COALESCE SQL fallback arm handles).
        fields_v2 = _wrap_v1_to_v2(fields_data, doc_type_fields)

    # edited_by_user_json with fallback to legacy user_corrected_fields_json
    # (pre-migration test rows lack the renamed col).
    if "edited_by_user_json" in row_keys:
        corrected_raw = row["edited_by_user_json"]
    elif "user_corrected_fields_json" in row_keys:
        corrected_raw = row["user_corrected_fields_json"]
    else:
        corrected_raw = None
    corrected = _safe_loads(corrected_raw, [])
    if not isinstance(corrected, list):
        corrected = []

    return KbExtractedRecordDTO(
        record_id=row["record_id"],
        doc_type_id=row["doc_type_id"],
        file_id=row["file_id"],
        workspace_id=row["workspace_id"],
        fields=fields_v2,
        verify_status=row["verify_status"] or "ready",
        edited_by_user=[str(x) for x in corrected],
        extractor_model=row["extractor_model"] or "qwen-plus",
        extraction_duration_ms=row["extraction_duration_ms"],
        extracted_at=row["extracted_at"],
        last_modified_at=row["last_modified_at"],
        last_modified_by=row["last_modified_by"],
        schema_version=int(sv) if sv is not None else 1,
    )


# ─── SQL helpers for kb_query_records (Day 3) ─────────

# Supported op (whitelist, prevent SQL injection).
QUERY_OPS = ("=", "!=", ">", ">=", "<", "<=", "LIKE", "IS NULL", "IS NOT NULL")
SORT_DIRS = ("ASC", "DESC")


def _validate_field_name(name: str) -> None:
    """字段名必须是 ascii 或中文 + 数字 + 下划线 (防 json path 注入).

    json_extract '$.<name>' 里 name 包 \" 或 \\ 会逃 path. 简单白名单替.
    """
    if not name or len(name) > 100:
        raise ValueError(f"field name 长度 1-100, got: {name!r}")
    for ch in name:
        if ch.isalnum() or ch in "_-" or "\u4e00" <= ch <= "\u9fff":
            continue
        raise ValueError(f"field name 含非法字符 {ch!r}: {name!r}")


class KbExtractedRecordStore:
    """SQLite-backed kb_extracted_records store.

    Workspace-scoped — list/query 必传 workspace_id (防 cross-ws leak).
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

    # ─── CRUD ─────────

    async def create(
        self,
        *,
        workspace_id: str,
        doc_type_id: str,
        file_id: str,
        fields: dict[str, Any],
        extractor_model: str = "qwen-plus",
        extraction_duration_ms: int | None = None,
        verify_status: str = "ready",
        created_by: str = "system",
        doc_type_fields: Any = None,
        dedup_check: bool = True,
    ) -> str:
        """Create a record. ``fields`` may be EITHER:
          - v2 shape: ``dict[str, list[dict]]`` (variant array, preferred)
          - V1 shape: ``dict[str, dict]`` (single-value, auto-wrapped via
            ``wrap_extractor_result_to_variants``)

        ``doc_type_fields`` (optional, kb-v2 §A.7): list[ExtractField]-like
        for type_hint propagation during auto-wrap. When provided, V1-shape
        currency/number values get ``{amount, currency}`` shape; without it,
        they degrade to ``{value: str}`` (COALESCE SQL still recovers).
        Always writes v2 shape (schema_version=2) to disk.

        Sprint 2 Phase B (I-B4): ``dedup_check=True`` (default) pre-SELECTs
        any non-soft-deleted record with the same (file_id, doc_type_id) and
        raises ``RecordAlreadyExistsError``. Set False only for explicit
        admin re-extract paths that have already cleared the prior row.

        Sprint 2 (P2-SEC-1): ``verify_status="extract_failed"`` forces
        ``fields={}`` to prevent partial-extract leak through records API.
        """
        if verify_status not in VERIFY_STATUS_VALUES:
            raise ValueError(
                f"verify_status 必须是 {VERIFY_STATUS_VALUES} 之一",
            )
        # Sprint 2 P2-SEC-1: extract_failed rows MUST NOT carry partial fields.
        if verify_status == "extract_failed":
            fields = {}
        # Sprint 2 I-B4: pre-INSERT dedup check
        if dedup_check:
            async with aiosqlite.connect(self._db_path) as db:
                cur = await db.execute(
                    "SELECT record_id FROM kb_extracted_records WHERE file_id = ? AND doc_type_id = ? AND soft_deleted_at IS NULL LIMIT 1",
                    (file_id, doc_type_id),
                )
                existing = await cur.fetchone()
                if existing:
                    raise RecordAlreadyExistsError(
                        file_id=file_id,
                        doc_type_id=doc_type_id,
                        existing_record_id=existing[0],
                    )
        # Detect V1 shape (any value is a non-list dict) → auto-wrap to v2.
        # Worker may pass doc_type.fields snapshot so type_hint propagates.
        if (
            fields
            and any(isinstance(v, dict) and not isinstance(v, list) for v in fields.values())
            and not any(isinstance(v, list) for v in fields.values())
        ):
            fields = wrap_extractor_result_to_variants(fields, doc_type_fields)
        record_id = f"rec_{uuid4().hex[:14]}"
        now = _now()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO kb_extracted_records "
                "(record_id, doc_type_id, file_id, workspace_id, fields_json, "
                " verify_status, edited_by_user_json, extractor_model, "
                " extraction_duration_ms, extracted_at, last_modified_at, "
                " last_modified_by, schema_version) "
                "VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, 2)",
                (
                    record_id,
                    doc_type_id,
                    file_id,
                    workspace_id,
                    json.dumps(fields, ensure_ascii=False),
                    verify_status,
                    extractor_model,
                    extraction_duration_ms,
                    now,
                    now,
                    created_by,
                ),
            )
            await db.commit()
        return record_id

    async def get(
        self,
        record_id: str,
        *,
        workspace_id: str | None = None,
        doc_type_fields: Any = None,
    ) -> KbExtractedRecordDTO | None:
        """Fetch a single record. ``doc_type_fields`` optional — when None, V1
        rows degrade to string-shape normalized (COALESCE SQL still recovers
        value). See tech-design §A.2 step 7.

        ``workspace_id`` (optional): when provided, scopes the query with an extra
        ``AND workspace_id = ?`` predicate as defense-in-depth. HTTP callers that
        already enforce membership pass the authoritative ws here so a record in
        another tenant is never even loaded (kb-security-quickfix P0)."""
        sql = "SELECT * FROM kb_extracted_records WHERE record_id = ? AND soft_deleted_at IS NULL"
        params: tuple[Any, ...] = (record_id,)
        if workspace_id is not None:
            sql += " AND workspace_id = ?"
            params = (record_id, workspace_id)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
        return _row_to_dto(row, doc_type_fields=doc_type_fields) if row else None

    async def list_for_doc_type(
        self,
        *,
        workspace_id: str,
        doc_type_id: str,
        limit: int = 100,
        offset: int = 0,
        doc_type_fields: Any = None,
    ) -> list[KbExtractedRecordDTO]:
        """List records for doc_type. P0-R1: 强制 workspace_id 过滤防 cross-ws leak.

        ``doc_type_fields`` (optional): list[ExtractField] for lazy-migrate type_hint
        per tech-design §A.2 step 7. Callers with batch context fetch once outside
        the loop + pass down.
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_extracted_records "
                "WHERE doc_type_id = ? AND workspace_id = ? "
                "AND soft_deleted_at IS NULL "
                "ORDER BY extracted_at DESC LIMIT ? OFFSET ?",
                (doc_type_id, workspace_id, limit, offset),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r, doc_type_fields=doc_type_fields) for r in rows]

    async def list_for_file(
        self,
        file_id: str,
        *,
        doc_type_fields: Any = None,
    ) -> list[KbExtractedRecordDTO]:
        """单 file 抽出的所有 records (一份 PDF 长 → chunk → merge 后通常 1 record)."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_extracted_records WHERE file_id = ? AND soft_deleted_at IS NULL ORDER BY extracted_at DESC",
                (file_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r, doc_type_fields=doc_type_fields) for r in rows]

    async def count_for_doc_type(
        self,
        *,
        workspace_id: str,
        doc_type_id: str,
        allowed_file_ids: tuple[str, ...] | None = None,
    ) -> int:
        from bowhead_core.kb.source_filter import append_source_filter

        clauses = ["workspace_id = ?", "doc_type_id = ?", "soft_deleted_at IS NULL"]
        params: list[Any] = [workspace_id, doc_type_id]
        append_source_filter(clauses, params, allowed_file_ids)
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT COUNT(*) FROM kb_extracted_records WHERE " + " AND ".join(clauses),
                params,
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def update_field(
        self,
        *,
        record_id: str,
        field_name: str,
        new_value: Any,
        modified_by: str,
        workspace_id: str | None = None,
        doc_type_fields: Any = None,
    ) -> bool:
        """User 手动改 1 个字段值. 该字段进入 edited_by_user, 后续 re-extract 不覆盖.

        Two-branch dispatch per tech-design §A.2 step 8:
          - V1 row (schema_version=1 or unmarked): lazy-migrate to v2 shape
            in same UPDATE + write ``fields_json_legacy`` backup +
            emit ``kb.record_v1_to_v2_migrate`` audit
          - v2 row (schema_version=2): regular v2→v2 update,
            ``fields_json_legacy`` untouched

        Sprint 1 invariant: edits always target the ``is_primary=true`` variant;
        multi-variant edit (Sprint 2 B1 UI) is out of Phase A scope.
        """
        _validate_field_name(field_name)
        # workspace_id (optional) scopes the pre-fetch so a cross-tenant record is
        # never loaded → the UPDATE below can only ever touch an in-scope row.
        existing = await self.get(record_id, workspace_id=workspace_id, doc_type_fields=doc_type_fields)
        if existing is None:
            return False
        is_v1_path = existing.schema_version != 2

        # Build v2 fields_json (already wrapped by _row_to_dto regardless of
        # source). Target the is_primary=true variant; create one if absent.
        new_fields: dict[str, list[dict[str, Any]]] = {k: [dict(v) for v in (vs or [])] for k, vs in existing.fields.items()}
        variants = new_fields.setdefault(field_name, [])
        primary_idx = next(
            (i for i, v in enumerate(variants) if v.get("is_primary")),
            -1,
        )
        # Build normalized via type_hint from doc_type_fields (if provided).
        type_map = _build_field_type_map(doc_type_fields)
        type_hint = type_map.get(field_name, "string")
        normalized = _v1_value_to_normalized(new_value, type_hint)
        if primary_idx < 0:
            # Field had no primary variant (orphan / never extracted) — create one.
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

        new_corrected = list(existing.edited_by_user)
        if field_name not in new_corrected:
            new_corrected.append(field_name)
        now = _now()
        t0 = time.time()
        async with aiosqlite.connect(self._db_path) as db:
            if is_v1_path:
                # Lazy-migrate write: bump schema_version=2, stash V1 backup,
                # all in one atomic UPDATE. WHERE clause guards re-migration.
                # Read V1 fields_json_raw straight from disk to preserve byte-exact
                # backup (existing.fields is already v2-wrapped via _row_to_dto).
                async with db.execute(
                    "SELECT fields_json FROM kb_extracted_records WHERE record_id = ?",
                    (record_id,),
                ) as cur:
                    legacy_row = await cur.fetchone()
                legacy_json = legacy_row[0] if legacy_row else "{}"
                cur = await db.execute(
                    "UPDATE kb_extracted_records SET "
                    "fields_json = ?, edited_by_user_json = ?, "
                    "fields_json_legacy = ?, schema_version = 2, "
                    "last_modified_at = ?, last_modified_by = ? "
                    "WHERE record_id = ? AND soft_deleted_at IS NULL "
                    "AND (schema_version = 1 OR schema_version IS NULL)",
                    (
                        json.dumps(new_fields, ensure_ascii=False),
                        json.dumps(new_corrected, ensure_ascii=False),
                        legacy_json,
                        now,
                        modified_by,
                        record_id,
                    ),
                )
                rowcount = cur.rowcount
                await db.commit()
                if rowcount > 0:
                    emit_audit(
                        "kb.record_v1_to_v2_migrate",
                        {
                            "record_id": record_id,
                            "workspace_id": existing.workspace_id,
                            "duration_ms": int((time.time() - t0) * 1000),
                        },
                    )
                return rowcount > 0
            # v2 row: regular update, leave fields_json_legacy untouched.
            cur = await db.execute(
                "UPDATE kb_extracted_records SET "
                "fields_json = ?, edited_by_user_json = ?, "
                "last_modified_at = ?, last_modified_by = ? "
                "WHERE record_id = ? AND soft_deleted_at IS NULL "
                "AND schema_version = 2",
                (json.dumps(new_fields, ensure_ascii=False), json.dumps(new_corrected, ensure_ascii=False), now, modified_by, record_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def update_verify_status(
        self,
        record_id: str,
        new_status: str,
        *,
        modified_by: str,
        workspace_id: str | None = None,
    ) -> bool:
        if new_status not in VERIFY_STATUS_VALUES:
            raise ValueError(f"verify_status 必须是 {VERIFY_STATUS_VALUES} 之一")
        now = _now()
        # workspace_id (optional): scope the UPDATE so a cross-tenant record cannot be
        # mutated even if the record_id is known (kb-security-quickfix P0 defense-in-depth).
        sql = (
            "UPDATE kb_extracted_records SET "
            "verify_status = ?, last_modified_at = ?, last_modified_by = ? "
            "WHERE record_id = ? AND soft_deleted_at IS NULL"
        )
        params: tuple[Any, ...] = (new_status, now, modified_by, record_id)
        if workspace_id is not None:
            sql += " AND workspace_id = ?"
            params = (new_status, now, modified_by, record_id, workspace_id)
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(sql, params)
            await db.commit()
            return cur.rowcount > 0

    async def soft_delete(self, record_id: str) -> bool:
        now = _now()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE kb_extracted_records SET soft_deleted_at = ?, last_modified_at = ? WHERE record_id = ? AND soft_deleted_at IS NULL",
                (now, now, record_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def soft_delete_for_file(self, file_id: str) -> int:
        """File 删时级联软删 records. 返删除条数."""
        now = _now()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE kb_extracted_records SET soft_deleted_at = ?, last_modified_at = ? WHERE file_id = ? AND soft_deleted_at IS NULL",
                (now, now, file_id),
            )
            await db.commit()
            return cur.rowcount

    # ─── SQL query (for Day 3 kb_query_records) ─────────

    async def query_by_field(
        self,
        *,
        workspace_id: str,
        doc_type_id: str,
        where: list[dict] | None = None,
        sort_by: str | None = None,
        sort_dir: str = "DESC",
        limit: int = 50,
        cast_numeric: list[str] | None = None,
        allowed_file_ids: tuple[str, ...] | None = None,
    ) -> list[KbExtractedRecordDTO]:
        """SQL filter + sort by fields_json[field].value via json_extract.

        Args:
            workspace_id: 强制 ws 过滤 (P0-R1).
            doc_type_id: 限单 doc_type.
            where: list of {field, op, value}, 多条件 AND 拼.
                op ∈ QUERY_OPS, field 走 _validate_field_name 白名单.
            sort_by: 字段名 sort, 默认按 extracted_at DESC.
            sort_dir: ASC | DESC (whitelist).
            limit: ≤ 1000.
            cast_numeric: 哪些字段要 CAST AS INTEGER (currency / number 用 sort/compare).

        Returns: matching records.

        SQL:
            SELECT * FROM kb_extracted_records
            WHERE workspace_id = ? AND doc_type_id = ?
              AND soft_deleted_at IS NULL
              AND json_extract(fields_json, '$.<f>.value') <op> ?
              AND ...
            ORDER BY <sort_expr> <dir>
            LIMIT ?
        """
        if sort_dir.upper() not in SORT_DIRS:
            raise ValueError(f"sort_dir 必须是 {SORT_DIRS} 之一")
        sort_dir = sort_dir.upper()
        limit = min(max(int(limit), 1), 1000)
        cast_numeric = cast_numeric or []

        for f in cast_numeric:
            _validate_field_name(f)

        clauses: list[str] = [
            "workspace_id = ?",
            "doc_type_id = ?",
            "soft_deleted_at IS NULL",
        ]
        params: list[Any] = [workspace_id, doc_type_id]
        from bowhead_core.kb.source_filter import append_source_filter

        append_source_filter(clauses, params, allowed_file_ids)

        for cond in where or []:
            f = cond.get("field", "")
            op = (cond.get("op") or "=").upper()
            val = cond.get("value")
            _validate_field_name(f)
            if op not in QUERY_OPS:
                raise ValueError(f"op 必须是 {QUERY_OPS} 之一, got {op}")
            extract_expr = _coalesce_field_expr(f)
            if f in cast_numeric:
                extract_expr = f"CAST({extract_expr} AS REAL)"
            if op in ("IS NULL", "IS NOT NULL"):
                clauses.append(f"{extract_expr} {op}")
            else:
                clauses.append(f"{extract_expr} {op} ?")
                params.append(val)

        # Sort expression — same 3-arm COALESCE for v2 (primary variant) + V1 fallback.
        if sort_by:
            _validate_field_name(sort_by)
            sort_expr = _coalesce_field_expr(sort_by)
            if sort_by in cast_numeric:
                sort_expr = f"CAST({sort_expr} AS REAL)"
        else:
            sort_expr = "extracted_at"

        sql = f"SELECT * FROM kb_extracted_records WHERE {' AND '.join(clauses)} ORDER BY {sort_expr} {sort_dir} LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]


# ─── kb-knowledge-tables v2 sprint: schema v1 → v2 migration ─────────


async def migrate_v1_to_v2(db_path: str | Path) -> None:
    """Idempotent v1→v2 schema migration. Re-run = no-op.

    Per tech-design §A.5: adds ``schema_version`` + ``fields_json_legacy`` cols,
    backfills ``schema_version=1`` for existing rows, renames
    ``user_corrected_fields_json`` → ``edited_by_user_json``.

    Safety net (read-only DB / disk-full / partial corruption): exception caught +
    audit emit + degrade. Re-raise only if ``BOWHEAD_KB_MIGRATION_STRICT=1``.

    Idempotency: PRAGMA sniff guards each ALTER. Safe to call on every lifespan
    start.
    """
    db_path_str = str(db_path)
    t0 = time.time()
    try:
        async with aiosqlite.connect(db_path_str) as db:
            # Step 1: single PRAGMA sniff to get all existing column names.
            # NOTE: existing_cols is intentionally a pre-ALTER snapshot — subsequent
            # `in existing_cols` checks must reflect the start state (idempotency).
            async with db.execute("SELECT name FROM pragma_table_info('kb_extracted_records')") as cur:
                rows = await cur.fetchall()
            existing_cols = {row[0] for row in rows}
            if not existing_cols:
                # Table doesn't exist — initialize() hasn't run. Nothing to migrate.
                logger.info("kb migrate: table absent, skip (init pending)")
                return

            # Step 1a: row-count pre-ship gate (warn-only — startup must not block).
            async with db.execute("SELECT COUNT(*) FROM kb_extracted_records WHERE soft_deleted_at IS NULL") as cur:
                row = await cur.fetchone()
            row_count = int(row[0]) if row else 0
            if row_count > V2_MIGRATION_ROW_COUNT_THRESHOLD:
                logger.warning(
                    "kb_extracted_records row count=%s exceeds %s threshold — "
                    "ALTER TABLE may exceed startup health check window. Consider "
                    "scheduling maintenance migration off the lifespan path.",
                    row_count,
                    V2_MIGRATION_ROW_COUNT_THRESHOLD,
                )

            # Step 2: ADD COLUMN schema_version + fields_json_legacy (idempotent).
            if "schema_version" not in existing_cols:
                await db.execute("ALTER TABLE kb_extracted_records ADD COLUMN schema_version INTEGER")
            if "fields_json_legacy" not in existing_cols:
                await db.execute("ALTER TABLE kb_extracted_records ADD COLUMN fields_json_legacy TEXT")

            # Step 3: backfill V1 marker (idempotent — re-run is no-op).
            await db.execute("UPDATE kb_extracted_records SET schema_version = 1 WHERE schema_version IS NULL")

            # Step 4: rename user_corrected_fields_json → edited_by_user_json
            # (only if old name still present + new not yet present).
            if "user_corrected_fields_json" in existing_cols and "edited_by_user_json" not in existing_cols:
                await db.execute("ALTER TABLE kb_extracted_records RENAME COLUMN user_corrected_fields_json TO edited_by_user_json")

            await db.commit()
            emit_audit(
                "kb.migrate_v1_to_v2_done",
                {
                    "db_path": db_path_str,
                    "row_count": row_count,
                    "duration_ms": int((time.time() - t0) * 1000),
                },
            )
    except Exception as exc:
        # Read-only mount / disk-full / partial corruption: don't crash lifespan.
        logger.exception("kb v1→v2 migration failed (db=%s): %s", db_path_str, exc)
        emit_audit(
            "kb.migrate_v1_to_v2_failed",
            {
                "db_path": db_path_str,
                "error": str(exc),
                "duration_ms": int((time.time() - t0) * 1000),
            },
        )
        if os.environ.get("BOWHEAD_KB_MIGRATION_STRICT") == "1":
            raise
