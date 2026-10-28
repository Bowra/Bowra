"""KbDocType store — workspace 级 文档表 (Document Tables) 配置.

kb-knowledge-tables sprint v1.0 (替代 kb-doctype-config V1+V2+V3 的语义错误).

文档表 = 用户从一类文档 (合同/回单/简历) 抽结构化字段, 形成虚拟表格. 用户:
1. 创文档表 + 描述要抽什么字段 (合同金额/起止日/对方/付款条件)
2. 上传 N 份 PDF 选该文档表
3. LLM 自动抽 → 形成 N 行 records
4. chat 走 SQL aggregate 查 ("金额最大的 5 笔")

Per `docs/kb-knowledge-tables-prd.md` v3.2 + `docs/kb-knowledge-tables-tech-design.md` v1.0.

V1+V2+V3 的 chunk_strategy / columns_json 语义砍 (那条路是装饰). dev 阶段 DROP+CREATE
schema, 老 dt_xxx 测试数据无成本砍.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiosqlite

if TYPE_CHECKING:
    from pathlib import Path

# V1 cap: schema fields ≤20 (P1-6-2 fix), prevent oversized fields_json overloading sqlite TEXT
MAX_FIELDS_PER_SCHEMA = 20

# V1 cap: per-field description / extraction_hint length limits
MAX_FIELD_NAME_LEN = 100
MAX_FIELD_DESC_LEN = 500

# Sprint 2: sample_keywords validation caps (per I-B8)
MAX_SAMPLE_KEYWORDS = 30
MAX_SAMPLE_KEYWORD_LEN = 32

# Sprint 2: cardinality enum values
CARDINALITY_VALUES = ("one", "many")

# kb-matrix M4 FR-40: graduation lineage graduated_mode values (NULL = not graduated).
# 'pending_snapshot' invisible in all lists (list_for_ws/kb_list_tables/classification candidates)—
# flip to 'snapshot' after rows persist + row_count writeback; self-healing sweep closes crash window.
GRADUATED_MODE_VALUES = ("template", "pending_snapshot", "snapshot")

# kb-matrix M4 FR-41: seed column-template created_by constant (basis for derived kind=seed;
# independent of Sprint-25 system_catalog_seed—catalog is extraction doc_type, not column template).
SYSTEM_TEMPLATE_SEED_USER = "system_template_seed"


def derive_doc_type_kind(dto: KbDocTypeDTO) -> str:
    """模板管理页/工具面 kind 派生（M4 决策 12，零额外列）：
    graduated_mode=snapshot → 'graduated'（冻结生态 gate 生效）；
    =template → 'template'；created_by=seed 常量 → 'seed'；其余 'manual'。
    pending_snapshot 不应出现在任何列表面（list 已滤），防御性归 'graduated'。
    getattr 防御（review P3-1）：kb_list_tables 对每 item 调本函数——duck-typed
    测试桩缺新字段不得 500 掉整个工具。"""
    mode = getattr(dto, "graduated_mode", None)
    if mode in ("snapshot", "pending_snapshot"):
        return "graduated"
    if mode == "template":
        return "template"
    if getattr(dto, "created_by", None) == SYSTEM_TEMPLATE_SEED_USER:
        return "seed"
    return "manual"


def is_frozen_snapshot(dto: KbDocTypeDTO) -> bool:
    """冻结快照判定（M4 决策 12 五点生态 gate 的单一谓词）。"""
    return getattr(dto, "graduated_mode", None) in ("snapshot", "pending_snapshot")


def is_column_group_template(dto: KbDocTypeDTO) -> bool:
    """列组模板判定（graduated/template/seed 任一）——不是抽取绑定/分类目标，
    seed/template 且零数据者不进 chat kb_list_tables。"""
    return getattr(dto, "graduated_mode", None) is not None or getattr(dto, "created_by", None) == SYSTEM_TEMPLATE_SEED_USER


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_doc_types (
    doc_type_id            TEXT PRIMARY KEY,
    workspace_id           TEXT NOT NULL,
    name                   TEXT NOT NULL,
    description            TEXT NOT NULL DEFAULT '',
    -- V1.0 新语义: 字段抽取 schema (replace V3 columns_json + chunk_strategy)
    -- {"fields": [{name, type, description, required, cross_verify, extraction_hint}, ...]}
    extract_schema_json    TEXT NOT NULL DEFAULT '{"fields": []}',
    -- Cache: records 计数 (避 SELECT COUNT 频繁)
    record_count           INTEGER NOT NULL DEFAULT 0,
    -- Extractor 配置
    extractor_model        TEXT NOT NULL DEFAULT 'qwen-plus',
    cross_verify_enabled   INTEGER NOT NULL DEFAULT 1,
    -- Sprint 2: classifier hints + cardinality (added via doc_type_migration on lifespan)
    cardinality            TEXT NOT NULL DEFAULT 'one' CHECK (cardinality IN ('one', 'many')),
    auto_extract_enabled   INTEGER NOT NULL DEFAULT 1,
    sample_keywords_json   TEXT NOT NULL DEFAULT '[]',
    -- Sprint 25 kb-per-row-extract: per-doc_type row-level cache + dedup config
    -- (added via doc_type_migration.migrate_kb_doc_types_add_sprint25_columns).
    row_count              INTEGER NOT NULL DEFAULT 0,
    dedup_key_fields_json  TEXT NOT NULL DEFAULT '[]',
    -- kb-matrix M4 FR-40: 矩阵转正血缘（fresh DB；legacy 经
    -- doc_type_migration.migrate_kb_doc_types_add_m4_columns ADD COLUMN）。
    -- graduated_mode: NULL=非转正 | 'template'(存为模板,仅字段) |
    --   'pending_snapshot'(rows 未落/未确认——对一切列表面不可见,自愈 sweep 收口) |
    --   'snapshot'(转正定格,冻结生态 gate 生效)。
    graduated_from_matrix_id TEXT,
    graduated_from_seq       INTEGER,
    graduated_mode           TEXT,
    -- kb-matrix-sharing: 团队模板库复制血缘（None=非复制来源；legacy 经
    -- doc_type_migration.migrate_kb_doc_types_add_sharing_columns ADD COLUMN）。
    copied_from_team_template_id TEXT,
    --
    created_by             TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    soft_deleted_at        TEXT,
    UNIQUE(workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_kb_doc_types_ws
    ON kb_doc_types(workspace_id) WHERE soft_deleted_at IS NULL;
"""


# Sprint 2: sample_keywords validator (per I-B8 — block regex meta + RTL + control chars)
_CONTROL_CHAR_RE = re.compile(
    r"[\x00-\x1f\x7f‪-‮⁦-⁩​-‍]",
)
_REGEX_META_RE = re.compile(r"[.*+?\[\]{}()|^$\\]")


def _validate_sample_keywords(keywords: list[str]) -> None:
    """Sprint 2: Reject regex DoS / RTL / control char injection in keywords.

    Per I-B8 (PRD r2 §4 + tech-design §A.5). Keywords used as literal substring
    match in classifier fast path — NO regex semantics — so reject meta-chars
    upfront to avoid surprise.
    """
    if not isinstance(keywords, list):
        raise ValueError("sample_keywords must be a list")
    if len(keywords) > MAX_SAMPLE_KEYWORDS:
        raise ValueError(
            f"sample_keywords ≤ {MAX_SAMPLE_KEYWORDS} (got {len(keywords)})",
        )
    for kw in keywords:
        if not isinstance(kw, str):
            raise ValueError(f"sample_keywords 必须 str: {kw!r}")
        if not (1 <= len(kw) <= MAX_SAMPLE_KEYWORD_LEN):
            raise ValueError(
                f"keyword 长度 1-{MAX_SAMPLE_KEYWORD_LEN}: {kw!r}",
            )
        if _CONTROL_CHAR_RE.search(kw):
            raise ValueError(f"keyword 含控制字符 / RTL / zero-width: {kw!r}")
        if _REGEX_META_RE.search(kw):
            raise ValueError(f"keyword 含 regex meta-char: {kw!r}")


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


# Single authority for field types (kb-matrix M4 FR-42: SSOT—matrix MATRIX_COLUMN_TYPES/PROMOTABLE_TYPES,
# route _FieldIn Literal, draft whitelist/system prompts derive here or equality is pinned by UT; do not duplicate).
# M4 adds select/multi_select/boolean (converges with matrix M3 column types; zero change to existing 4 types).
FIELD_TYPES = ("string", "number", "currency", "date", "select", "multi_select", "boolean")

# options constraints (kernel authority since M4; matrix re-exports same implementation—FR-2 r5 semantics unchanged):
# select/multi_select required (strip nonempty, deduplicate, each ≤64, 1..100); forbidden for boolean/legacy four types.
OPTIONS_REQUIRED_TYPES = ("select", "multi_select")
MAX_OPTIONS = 100
MAX_OPTION_LEN = 64


def validate_options(type_: str, options: list[str] | None) -> list[str] | None:
    """options 单一权威校验（原 matrix_store 实现 M4 上移内核，矩阵侧 re-export；
    四路写入口共用：建矩阵/加列/编辑列 PATCH/转正 + doc_type create/update）。
    返回归一化后的 options（strip 去重保序）。"""
    if type_ in OPTIONS_REQUIRED_TYPES:
        if not options:
            raise ValueError("options_required")
        seen: set[str] = set()
        norm: list[str] = []
        for opt in options:
            item = str(opt).strip()
            if not item:
                raise ValueError("option_empty")
            if len(item) > MAX_OPTION_LEN:
                raise ValueError("option_too_long")
            if item in seen:
                raise ValueError("option_duplicate")
            seen.add(item)
            norm.append(item)
        if len(norm) > MAX_OPTIONS:
            raise ValueError("options_too_many")
        return norm
    if options:
        raise ValueError("options_not_applicable")
    return None


@dataclass
class ExtractField:
    """文档表的一个字段定义."""

    name: str  # e.g. "合同金额"
    type: str = "string"  # one of FIELD_TYPES
    description: str = ""  # e.g. "合同总金额, RMB"
    required: bool = False  # Required (empty → mark "缺失")
    cross_verify: bool = False  # N=2 self-consistency (high sensitivity)
    extraction_hint: str = ""  # Hint where LLM should extract field
    # M4 FR-42: select/multi_select options constraints (all other types must use None).
    options: list[str] | None = None
    # M5 FR-43 reserved numeric-dimension seam (unused in M4, round trip only; additive).
    dimension: dict[str, Any] | None = None

    def validate(self) -> None:
        if not self.name or len(self.name) > MAX_FIELD_NAME_LEN:
            raise ValueError(f"field name 长度 1-{MAX_FIELD_NAME_LEN}")
        if self.type not in FIELD_TYPES:
            raise ValueError(f"field type 必须是 {FIELD_TYPES} 之一")
        if len(self.description) > MAX_FIELD_DESC_LEN:
            raise ValueError(f"field description 长度 ≤ {MAX_FIELD_DESC_LEN}")
        # Single execution point: create/update both call validate()—normalize and write back options here
        # (strip/deduplicate; reject missing select/multi_select options or options on other types).
        self.options = validate_options(self.type, self.options)
        if self.dimension is not None and not isinstance(self.dimension, dict):
            raise ValueError("dimension 必须是 dict（M5 留位，M4 不消费）")


@dataclass(frozen=True)
class DocTypeStats:
    """Sprint 2 Phase C: aggregate stats for a single doc_type.

    Returned by ``KbDocTypeStore.list_with_stats(ws_id)`` keyed by doc_type_id.
    Computed in a single sweep over kb_extracted_records (per ws filter).
    """

    total: int = 0
    needs_review_count: int = 0
    last_extracted_at: str | None = None  # ISO datetime or None


@dataclass
class KbDocTypeDTO:
    """文档表 DTO (公共 API DTO)."""

    doc_type_id: str
    workspace_id: str
    name: str
    description: str
    fields: list[ExtractField]  # Field definitions (V1 main attribute)
    record_count: int  # Cache may lag slightly
    extractor_model: str
    cross_verify_enabled: bool
    created_by: str
    created_at: str
    updated_at: str
    # Sprint 2: classifier hints + cardinality (defaults preserve V1 behavior)
    cardinality: str = "one"  # "one" | "many"
    auto_extract_enabled: bool = True
    sample_keywords: list[str] = field(default_factory=list)
    # Sprint 25 kb-per-row-extract: per-doc_type row-level cache + dedup config
    row_count: int = 0  # Cache: kb_extracted_rows count for this (ws, dt)
    dedup_key_fields: list[str] = field(default_factory=list)  # Catalog: which fields form row_dedup_key_hash
    # kb-matrix M4 FR-40: matrix graduation lineage (None = not graduated; kind derived by derive_doc_type_kind)
    graduated_from_matrix_id: str | None = None
    graduated_from_seq: int | None = None
    graduated_mode: str | None = None
    # kb-matrix-sharing: organization template library copy lineage (None = no copy source; additive round trip)
    copied_from_team_template_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "doc_type_id": self.doc_type_id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "description": self.description,
            "fields": [_field_to_dict(f) for f in self.fields],
            "record_count": self.record_count,
            "extractor_model": self.extractor_model,
            "cross_verify_enabled": self.cross_verify_enabled,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            # Sprint 2
            "cardinality": self.cardinality,
            "auto_extract_enabled": self.auto_extract_enabled,
            "sample_keywords": list(self.sample_keywords),
            # Sprint 25
            "row_count": self.row_count,
            "dedup_key_fields": list(self.dedup_key_fields),
            # kb-matrix M4: lineage + kind (additive; FE template management page grouping/gates consume)
            "graduated_from_matrix_id": self.graduated_from_matrix_id,
            "graduated_mode": self.graduated_mode,
            "copied_from_team_template_id": self.copied_from_team_template_id,
            "kind": derive_doc_type_kind(self),
        }


def _schema_json_to_fields(schema_raw: str | None) -> list[ExtractField]:
    """Parse an ``extract_schema_json`` blob → ``list[ExtractField]``.

    Standalone so the team-template-library publish/copy/discovery paths can turn a
    stored schema snapshot into fields without a full DTO row (``_row_to_dto``
    delegates here). Read-path defensive: missing keys (legacy rows) → defaults;
    malformed shapes don't raise (``validate()`` only runs on the write path).
    """
    try:
        schema_data = json.loads(schema_raw or '{"fields": []}')
    except (TypeError, json.JSONDecodeError):
        schema_data = {"fields": []}
    fields_data = schema_data.get("fields", []) if isinstance(schema_data, dict) else []
    return [
        ExtractField(
            name=f.get("name", ""),
            type=f.get("type", "string"),
            description=f.get("description", ""),
            required=bool(f.get("required", False)),
            cross_verify=bool(f.get("cross_verify", False)),
            extraction_hint=f.get("extraction_hint", ""),
            # Defensive reads—missing key (legacy row) → None; invalid shape does not crash (validate runs only on writes).
            options=(lambda o: [str(x) for x in o] if isinstance(o, list) and o else None)(f.get("options")),
            dimension=(f.get("dimension") if isinstance(f.get("dimension"), dict) else None),
        )
        for f in fields_data
        if isinstance(f, dict)
    ]


def _row_to_dto(row: aiosqlite.Row) -> KbDocTypeDTO:
    fields = _schema_json_to_fields(row["extract_schema_json"])
    # Sprint 2 (I-A5): defensive col-missing read — NEW image reading pre-ALTER
    # rows (test fixtures / pre-migration sqlite) must not crash on KeyError.
    # aiosqlite.Row supports `.keys()` listing.
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    cardinality = row["cardinality"] if "cardinality" in keys else "one"
    auto_extract_enabled = bool(row["auto_extract_enabled"]) if "auto_extract_enabled" in keys else True
    sample_keywords_raw = row["sample_keywords_json"] if "sample_keywords_json" in keys else "[]"
    try:
        sample_keywords = json.loads(sample_keywords_raw or "[]")
        if not isinstance(sample_keywords, list):
            sample_keywords = []
    except (TypeError, json.JSONDecodeError):
        sample_keywords = []
    # Sprint 25 defensive read
    row_count = int(row["row_count"]) if "row_count" in keys and row["row_count"] is not None else 0
    dedup_key_fields_raw = row["dedup_key_fields_json"] if "dedup_key_fields_json" in keys else "[]"
    try:
        dedup_key_fields = json.loads(dedup_key_fields_raw or "[]")
        if not isinstance(dedup_key_fields, list):
            dedup_key_fields = []
    except (TypeError, json.JSONDecodeError):
        dedup_key_fields = []
    # kb-matrix M4 defensive read (pre-migration rows/old fixtures with missing columns do not crash)
    grad_matrix = row["graduated_from_matrix_id"] if "graduated_from_matrix_id" in keys else None
    grad_seq_raw = row["graduated_from_seq"] if "graduated_from_seq" in keys else None
    grad_seq = int(grad_seq_raw) if grad_seq_raw is not None else None
    grad_mode = row["graduated_mode"] if "graduated_mode" in keys else None
    copied_from = row["copied_from_team_template_id"] if "copied_from_team_template_id" in keys else None
    return KbDocTypeDTO(
        doc_type_id=row["doc_type_id"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        description=row["description"] or "",
        fields=fields,
        record_count=row["record_count"] or 0,
        extractor_model=row["extractor_model"] or "qwen-plus",
        cross_verify_enabled=bool(row["cross_verify_enabled"]),
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        cardinality=cardinality,
        auto_extract_enabled=auto_extract_enabled,
        sample_keywords=sample_keywords,
        row_count=row_count,
        dedup_key_fields=[str(x) for x in dedup_key_fields],
        graduated_from_matrix_id=grad_matrix,
        graduated_from_seq=grad_seq,
        graduated_mode=grad_mode,
        copied_from_team_template_id=copied_from,
    )


def _field_to_dict(f: ExtractField) -> dict[str, Any]:
    """字段序列化单一形状（M4 FR-42：to_dict / _fields_to_schema_json 同源——
    r2 review P1：漏落库序列化器 = options 静默丢）。options/dimension **条件式**
    发射：存量 4 型字段的序列化输出逐字节不变（零回归硬约束）。"""
    out: dict[str, Any] = {
        "name": f.name,
        "type": f.type,
        "description": f.description,
        "required": f.required,
        "cross_verify": f.cross_verify,
        "extraction_hint": f.extraction_hint,
    }
    if f.options is not None:
        out["options"] = list(f.options)
    if f.dimension is not None:
        out["dimension"] = dict(f.dimension)
    return out


def _fields_to_schema_json(fields: list[ExtractField]) -> str:
    return json.dumps({"fields": [_field_to_dict(f) for f in fields]}, ensure_ascii=False)


async def _maybe_rebuild_field_indexes(
    records_db_path: str | Path | None,
    doc_type_id: str,
    fields: list[ExtractField] | None,
) -> None:
    """Side-effect hook: rebuild expression indexes on kb_extracted_records when
    records_db_path is configured. When None (legacy test fixtures), skip.

    Tech-design §A.6: numeric fields (currency/number) get expression indexes
    so cast_numeric query_by_field SQL hits index instead of full table scan.
    Failure inside _rebuild_field_indexes is logged + audited but never raises.
    """
    if records_db_path is None:
        return
    # Lazy import to avoid module-cycle (extracted_record_store imports nothing
    # from this module — but keep it scoped to defer until first call).
    from bowhead_core.kb.extracted_record_store import (
        _rebuild_field_indexes,
    )

    await _rebuild_field_indexes(records_db_path, doc_type_id, fields)


async def _maybe_rebuild_row_field_indexes(
    rows_db_path: str | Path | None,
    doc_type_id: str,
    fields: Any = None,
) -> None:
    """Sprint 25 P0-PERF-1 hook: rebuild expression indexes on kb_extracted_rows
    when rows_db_path configured. Parallel to _maybe_rebuild_field_indexes for
    file-level records. Called on doc_type CRUD (create + update fields)."""
    if rows_db_path is None:
        return
    from bowhead_core.kb.extracted_rows_store import (
        _rebuild_row_field_indexes,
    )

    await _rebuild_row_field_indexes(rows_db_path, doc_type_id, fields)


class KbDocTypeStore:
    """SQLite-backed kb_doc_types store. Workspace-scoped."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        records_db_path: str | Path | None = None,
        rows_db_path: str | Path | None = None,
    ) -> None:
        """``records_db_path``: when set, CRUD ops trigger
        ``_rebuild_field_indexes`` on the records DB (kb-v2 sprint §A.6).
        ``rows_db_path``: Sprint 25 — when set, CRUD ops also trigger
        ``_rebuild_row_field_indexes`` on kb_extracted_rows.db for expression
        indexes (aggregate SUM perf, P0-PERF-1).
        Tests / standalone use can pass None to skip."""
        self._db_path = str(db_path)
        self._records_db_path = str(records_db_path) if records_db_path is not None else None
        self._rows_db_path = str(rows_db_path) if rows_db_path is not None else None

    async def initialize(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            for stmt in _SCHEMA.split(";"):
                stmt = stmt.strip()
                if stmt:
                    await db.execute(stmt)
            await db.commit()

    async def create(
        self,
        *,
        workspace_id: str,
        name: str,
        created_by: str,
        description: str = "",
        fields: list[ExtractField] | None = None,
        extractor_model: str = "qwen-plus",
        cross_verify_enabled: bool = True,
        # Sprint 2: classifier hints + cardinality
        cardinality: str = "one",
        auto_extract_enabled: bool = True,
        sample_keywords: list[str] | None = None,
        # Sprint 25 kb-per-row-extract: dedup_key_fields for row UNIQUE INDEX
        dedup_key_fields: list[str] | None = None,
        # kb-matrix M4 FR-40: matrix graduation lineage (pass all three columns together or all None)
        graduated_from_matrix_id: str | None = None,
        graduated_from_seq: int | None = None,
        graduated_mode: str | None = None,
        # kb-matrix-sharing: organization template library copy lineage (None = no copy source; additive)
        copied_from_team_template_id: str | None = None,
    ) -> str:
        fields = fields or []
        if len(fields) > MAX_FIELDS_PER_SCHEMA:
            raise ValueError(
                f"V1 限单文档表字段 ≤ {MAX_FIELDS_PER_SCHEMA} (你配了 {len(fields)})",
            )
        for f in fields:
            f.validate()
        # M4 review P1-1: duplicate field names = silent last-wins loss in fields_json dict
        # + ambiguous field_def lookup by name (chat type-gate may read wrong type and allow invalid SUM).
        _names = [f.name for f in fields]
        if len(set(_names)) != len(_names):
            raise ValueError("duplicate_field_names")
        # Sprint 2 validation
        if cardinality not in CARDINALITY_VALUES:
            raise ValueError(
                f"cardinality 必须 ∈ {CARDINALITY_VALUES} (got {cardinality!r})",
            )
        sample_keywords = sample_keywords or []
        _validate_sample_keywords(sample_keywords)
        # Sprint 25: dedup_key_fields validation — must be subset of field names
        dedup_key_fields = list(dedup_key_fields or [])
        field_names = {f.name for f in fields}
        for dkf in dedup_key_fields:
            if dkf not in field_names:
                raise ValueError(
                    f"dedup_key_field {dkf!r} not in fields {sorted(field_names)}",
                )
        if graduated_mode is not None and graduated_mode not in GRADUATED_MODE_VALUES:
            raise ValueError(f"graduated_mode 必须 ∈ {GRADUATED_MODE_VALUES} (got {graduated_mode!r})")
        doc_type_id = f"dt_{uuid4().hex[:12]}"
        now = _now()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO kb_doc_types "
                "(doc_type_id, workspace_id, name, description, extract_schema_json, "
                " record_count, extractor_model, cross_verify_enabled, "
                " cardinality, auto_extract_enabled, sample_keywords_json, "
                " row_count, dedup_key_fields_json, "
                " graduated_from_matrix_id, graduated_from_seq, graduated_mode, "
                " copied_from_team_template_id, "
                " created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc_type_id,
                    workspace_id,
                    name,
                    description,
                    _fields_to_schema_json(fields),
                    extractor_model,
                    1 if cross_verify_enabled else 0,
                    cardinality,
                    1 if auto_extract_enabled else 0,
                    json.dumps(sample_keywords, ensure_ascii=False),
                    json.dumps(dedup_key_fields, ensure_ascii=False),
                    graduated_from_matrix_id,
                    graduated_from_seq,
                    graduated_mode,
                    copied_from_team_template_id,
                    created_by,
                    now,
                    now,
                ),
            )
            await db.commit()
        # kb-v2 §A.6: rebuild per-field expression indexes on records DB
        # (no-op when records_db_path=None — test fixtures / legacy callers).
        await _maybe_rebuild_field_indexes(
            self._records_db_path,
            doc_type_id,
            fields,
        )
        # Sprint 25 P0-PERF-1: also rebuild on kb_extracted_rows.db (cardinality=many
        # aggregate SUM perf). No-op when rows_db_path=None.
        if cardinality == "many":
            await _maybe_rebuild_row_field_indexes(
                self._rows_db_path,
                doc_type_id,
                fields,
            )
        return doc_type_id

    async def get(self, doc_type_id: str) -> KbDocTypeDTO | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_doc_types WHERE doc_type_id = ? AND soft_deleted_at IS NULL",
                (doc_type_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_dto(row) if row else None

    async def list_with_stats(
        self,
        workspace_id: str,
    ) -> tuple[list[KbDocTypeDTO], dict[str, DocTypeStats]]:
        """Sprint 2 Phase C (per tech-design §C.7 r2 fix): list ws doc_types +
        aggregate stats from records DB in a single cross-DB call.

        Reuses ``self._records_db_path`` already plumbed in ``__init__`` (same
        pattern as ``_maybe_rebuild_field_indexes``). No ATTACH, no Python merge
        helper module — keeps Sprint 2 footprint contained.

        Returns ``(doc_types, stats_map)``. ``stats_map`` keyed by
        ``doc_type_id``; missing keys (zero records) default to empty
        ``DocTypeStats``.
        """
        doc_types = await self.list_for_ws(workspace_id)
        if not doc_types or self._records_db_path is None:
            return doc_types, {}

        dt_ids = [dt.doc_type_id for dt in doc_types]
        placeholders = ",".join("?" * len(dt_ids))
        stats_map: dict[str, DocTypeStats] = {}
        try:
            async with aiosqlite.connect(self._records_db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    f"SELECT doc_type_id, "  # noqa: S608 — placeholders bound, ids from list
                    f"  COUNT(*) AS total, "
                    f"  SUM(CASE WHEN verify_status='needs_review' THEN 1 ELSE 0 END) "
                    f"    AS needs_review_count, "
                    f"  MAX(extracted_at) AS last_extracted_at "
                    f"FROM kb_extracted_records "
                    f"WHERE workspace_id = ? AND doc_type_id IN ({placeholders}) "
                    f"  AND soft_deleted_at IS NULL "
                    f"GROUP BY doc_type_id",
                    (workspace_id, *dt_ids),
                ) as cur:
                    async for row in cur:
                        stats_map[row["doc_type_id"]] = DocTypeStats(
                            total=row["total"] or 0,
                            needs_review_count=row["needs_review_count"] or 0,
                            last_extracted_at=row["last_extracted_at"],
                        )
        except aiosqlite.Error:
            # Records DB missing or unreadable → return doc_types with empty stats
            # (Sprint 1 / Phase A boot may legitimately have no records DB yet).
            return doc_types, {}
        return doc_types, stats_map

    async def list_for_ws(self, workspace_id: str) -> list[KbDocTypeDTO]:
        # M4: pending_snapshot (unfinished graduation table, rows unconfirmed) invisible to all lists
        # (kb_list_tables/template page/classification candidates all use this); self-healing sweep closes crash window.
        # ⚠ Filter in Python, not SQL—WHERE on new column would cause OperationalError in
        # pre-migration databases (defensive-col-missing UT caught it; _row_to_dto missing column→None always passes).
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_doc_types WHERE workspace_id = ? AND soft_deleted_at IS NULL ORDER BY name",
                (workspace_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [d for d in (_row_to_dto(r) for r in rows) if d.graduated_mode != "pending_snapshot"]

    async def has_many_auto_extract_for_workspaces(
        self,
        workspace_ids: list[str],
    ) -> bool:
        """Whether any active many-cardinality type requires row extraction."""
        if not workspace_ids:
            return False
        for offset in range(0, len(workspace_ids), 400):
            batch = workspace_ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            async with (
                aiosqlite.connect(self._db_path) as db,
                db.execute(
                    "SELECT 1 FROM kb_doc_types "
                    f"WHERE workspace_id IN ({placeholders}) "
                    "AND cardinality = 'many' AND auto_extract_enabled = 1 "
                    "AND soft_deleted_at IS NULL LIMIT 1",
                    tuple(batch),
                ) as cur,
            ):
                if await cur.fetchone():
                    return True
        return False

    async def update(
        self,
        doc_type_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        fields: list[ExtractField] | None = None,
        extractor_model: str | None = None,
        cross_verify_enabled: bool | None = None,
        # Sprint 2: classifier hints + cardinality
        cardinality: str | None = None,
        auto_extract_enabled: bool | None = None,
        sample_keywords: list[str] | None = None,
    ) -> bool:
        sets: list[str] = []
        vals: list = []
        if name is not None:
            sets.append("name = ?")
            vals.append(name)
        if description is not None:
            sets.append("description = ?")
            vals.append(description)
        if fields is not None:
            if len(fields) > MAX_FIELDS_PER_SCHEMA:
                raise ValueError(
                    f"V1 限单文档表字段 ≤ {MAX_FIELDS_PER_SCHEMA}",
                )
            for f in fields:
                f.validate()
            _upd_names = [f.name for f in fields]
            if len(set(_upd_names)) != len(_upd_names):
                raise ValueError("duplicate_field_names")
            sets.append("extract_schema_json = ?")
            vals.append(_fields_to_schema_json(fields))
        if extractor_model is not None:
            sets.append("extractor_model = ?")
            vals.append(extractor_model)
        if cross_verify_enabled is not None:
            sets.append("cross_verify_enabled = ?")
            vals.append(1 if cross_verify_enabled else 0)
        # Sprint 2 fields
        if cardinality is not None:
            if cardinality not in CARDINALITY_VALUES:
                raise ValueError(
                    f"cardinality 必须 ∈ {CARDINALITY_VALUES}",
                )
            sets.append("cardinality = ?")
            vals.append(cardinality)
        if auto_extract_enabled is not None:
            sets.append("auto_extract_enabled = ?")
            vals.append(1 if auto_extract_enabled else 0)
        if sample_keywords is not None:
            _validate_sample_keywords(sample_keywords)
            sets.append("sample_keywords_json = ?")
            vals.append(json.dumps(sample_keywords, ensure_ascii=False))
        if not sets:
            return False
        sets.append("updated_at = ?")
        vals.append(_now())
        vals.append(doc_type_id)
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                f"UPDATE kb_doc_types SET {', '.join(sets)} WHERE doc_type_id = ? AND soft_deleted_at IS NULL",
                vals,
            )
            await db.commit()
            rowcount = cur.rowcount
        # kb-v2 §A.6: rebuild indexes only when fields actually changed + the
        # UPDATE matched (no-op for rename-only / desc-only updates).
        if rowcount > 0 and fields is not None:
            await _maybe_rebuild_field_indexes(
                self._records_db_path,
                doc_type_id,
                fields,
            )
            # kb-matrix M4 accompanying fix (pre-existing gap, journey review F5③): update previously
            # rebuilt only records indexes, not rows indexes—many-type field changes drifted aggregate expression indexes.
            # create() already builds both for many; align update.
            dto_after = await self.get(doc_type_id)
            if dto_after is not None and dto_after.cardinality == "many":
                await _maybe_rebuild_row_field_indexes(
                    self._rows_db_path,
                    doc_type_id,
                    fields,
                )
        return rowcount > 0

    # ─── kb-matrix M4 FR-40: graduation lineage primitives ──────────────────

    async def set_graduated_mode(self, doc_type_id: str, mode: str) -> bool:
        """graduated_mode 直写（一般用途）；pending→snapshot 的 flip 必须走
        ``flip_graduated_snapshot``（winner-only + 绝对计数）。"""
        if mode not in GRADUATED_MODE_VALUES:
            raise ValueError(f"graduated_mode 必须 ∈ {GRADUATED_MODE_VALUES}")
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE kb_doc_types SET graduated_mode = ?, updated_at = ? WHERE doc_type_id = ? AND soft_deleted_at IS NULL",
                (mode, _now(), doc_type_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def flip_graduated_snapshot(self, doc_type_id: str, *, row_count: int) -> bool:
        """pending_snapshot → snapshot 的**唯一**合法 flip（wave-1 review P2-4）：
        `WHERE graduated_mode='pending_snapshot'` winner-only（并发自愈两路同 flip
        → loser no-op 不重复计数）+ row_count **绝对写入**（相对 delta 基于 stale
        读会永久翻倍；flip 时刻的 rows 真 COUNT 就是权威）。返回是否赢得 flip。"""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE kb_doc_types SET graduated_mode = 'snapshot', row_count = ?, updated_at = ? "
                "WHERE doc_type_id = ? AND graduated_mode = 'pending_snapshot' AND soft_deleted_at IS NULL",
                (int(row_count), _now(), doc_type_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def find_graduated_for_matrix(
        self,
        workspace_id: str,
        matrix_id: str,
        seq: int | None = None,
    ) -> list[KbDocTypeDTO]:
        """矩阵的转正产物反查：幂等键 (matrix_id, seq) 命中复用（seq 给定），
        或全量列（确认对话框「已转正 N 次」提示，seq=None）。含 pending
        （幂等重试要自愈崩溃残留），不含软删。"""
        sql = "SELECT * FROM kb_doc_types WHERE workspace_id = ? AND graduated_from_matrix_id = ? AND soft_deleted_at IS NULL"
        params: list[Any] = [workspace_id, matrix_id]
        if seq is not None:
            sql += " AND graduated_from_seq = ?"
            params.append(seq)
        sql += " ORDER BY created_at DESC"
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    async def name_exists(self, workspace_id: str, name: str) -> bool:
        """转正/重命名撞名前置查（UNIQUE(ws,name) 兜底 IntegrityError 仍要 catch——
        本查询只为给出结构化 409 + 建议名，非并发安全依据）。"""
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT 1 FROM kb_doc_types WHERE workspace_id = ? AND name = ? AND soft_deleted_at IS NULL LIMIT 1",
                (workspace_id, name),
            ) as cur,
        ):
            row = await cur.fetchone()
        return row is not None

    async def list_pending_graduations(self, older_than_iso: str) -> list[KbDocTypeDTO]:
        """自愈 sweep 候选：pending_snapshot 且 updated_at 早于阈值（全 ws——boot
        级任务）。谓词绝不触碰 template/snapshot 态（r3 review：r2 的 row_count=0
        谓词会误删全部合法 template）。"""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_doc_types WHERE graduated_mode = 'pending_snapshot' AND soft_deleted_at IS NULL AND updated_at < ?",
                (older_than_iso,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    async def soft_delete(self, doc_type_id: str) -> bool:
        # M4 FR-40 (name-collision journey): soft delete **renames tombstone to free name**—UNIQUE(workspace_id, name)
        # is a table constraint including soft-deleted rows (cannot use partial index without table rebuild, violating additive discipline);
        # retaining name loops through deleted-but-duplicate-name errors + guarantees regraduation collision. Deliberate global behavior change:
        # get() may show tombstone name (lists already exclude soft-deleted rows).
        async with aiosqlite.connect(self._db_path) as db:
            # Suffix uses doc_type_id (unique by construction), not timestamp—delete→create same name→delete again
            # within a second would hit UNIQUE 500 (wave-1 review P3-3).
            cur = await db.execute(
                "UPDATE kb_doc_types SET soft_deleted_at = ?, updated_at = ?, "
                "name = name || '·del·' || doc_type_id "
                "WHERE doc_type_id = ? AND soft_deleted_at IS NULL",
                (_now(), _now(), doc_type_id),
            )
            await db.commit()
            rowcount = cur.rowcount
        # kb-v2 §A.6: drop all indexes for this doc_type (no rebuild — schema gone).
        if rowcount > 0:
            await _maybe_rebuild_field_indexes(
                self._records_db_path,
                doc_type_id,
                None,
            )
        return rowcount > 0

    async def update_record_count(self, doc_type_id: str, delta: int) -> None:
        """+/- record_count cache. Called by KbExtractedRecordStore on insert/delete."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE kb_doc_types SET record_count = record_count + ? WHERE doc_type_id = ?",
                (delta, doc_type_id),
            )
            await db.commit()

    async def update_row_count(self, doc_type_id: str, delta: int) -> None:
        """Sprint 25 kb-per-row-extract: +/- row_count cache.

        Called by KbWorker._run_per_row_extraction after successful create_many
        batch (worker passes total inserted_count - skipped_dup_count = net new rows).
        Parallel to update_record_count for file-level path (cardinality=one).

        Per P0-TD-ENG-R2-1 (Eng r2 review): admin UI displays both counts
        consistently.
        """
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE kb_doc_types SET row_count = row_count + ? WHERE doc_type_id = ?",
                (delta, doc_type_id),
            )
            await db.commit()
