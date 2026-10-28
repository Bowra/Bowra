"""KB Pydantic DTOs (frontend ↔ backend shared types).

Tech design v3.6 §2.4. Round 4 P0-P1: KbFileDTO 加 workspace_id / team_id.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field


def _normalize_scope(v: str) -> str:
    """批3/c1 (D3): API 边界双收——旧客户端/脚本发 'workspace' 归一化为 canonical
    'space'（一版兼容；FE 本批已改发 'space'）。

    实现注记：用 **AfterValidator**（Literal 四值先过、再归一）而非 Before——
    FastAPI 的 Form 参数不跑 BeforeValidator（dogfood 422 实测），After 对
    Form/JSON 两路都生效。运行时产出恒为 canonical 三值。"""
    return "space" if v == "workspace" else v


# Canonical scope input type: personal | space | team (also accept and normalize 'workspace')
KbScopeIn = Annotated[
    Literal["personal", "space", "workspace", "team"],
    AfterValidator(_normalize_scope),
]

# File-list scope filter: the three scopes plus ``all`` (every scope the caller can read).
# Only GET /kb/files accepts ``all``; writes keep KbScopeIn.
KbListScopeIn = Annotated[
    Literal["personal", "space", "workspace", "team", "all"],
    AfterValidator(_normalize_scope),
]


class KbTruncatedRows(BaseModel):
    """表格文件超出索引行数上限：只索引了前 ``indexed`` 行，文件共 ``total`` 行。"""

    indexed: int
    total: int


class KbFileDTO(BaseModel):
    """KB 文件 metadata DTO. SQL kb_files row → API response."""

    file_id: str
    artifact_id: str
    filename: str
    mime_type: str
    size_bytes: int

    scope: KbScopeIn
    scope_id: str  # SQL field, same meaning as chromadb metadata.scope_owner [P1-P6]
    workspace_id: str  # [P0-P1] physical ws collection routing (team scope uses '')
    team_id: str  # [P0-P1] cross-scope defense + audit

    uploaded_by: str
    uploaded_by_name: str | None = None
    uploaded_at: str

    parse_status: Literal[
        # Sprint file-upload-presign Phase 1.3: 'awaiting_upload' and 'queued'
        # added for the presigned upload flow. 'awaiting_upload' is set at
        # presign-issue time before browser PUT; 'queued' is set after confirm
        # while the KbWorker hasn't started yet. CHECK constraint in
        # files_store.py:34 expanded via copy-migrate in _migrate_parse_status_check.
        "pending",
        "parsing",
        "chunking",
        "embedding",
        "done",
        "failed",
        "awaiting_upload",
        "queued",
    ] = "pending"
    parse_progress_pct: int | None = None  # 0-100, P1-Q6 state map
    parse_layer: Literal["L0", "L1", "L2"] | None = None
    parse_error: str | None = None
    parse_duration_ms: int | None = None
    # KB experience cleanup FR1: user-facing failure categories (parse_recovery.parse_error_code);
    # frontend maps codes to copy, hides raw parse_error; parse_stuck = in progress with heartbeat timeout,
    # and supports retry like failed.
    parse_error_code: (
        Literal[
            "unsupported_format",
            "empty_content",
            "interrupted",
            "timeout",
            "service_unavailable",
            "source_missing",
            "parse_failed",
        ]
        | None
    ) = None
    parse_stuck: bool = False

    chunks_count: int = 0
    cite_count: int = 0
    last_cited_at: str | None = None

    use_llm_marker: bool = False
    doc_type_id: str | None = None  # Unused in V1; enabled by kb-doctype-config sprint
    # Sprint 2 kb-structured-tables: classifier writes here (NOT doc_type_id),
    # so rollback to 0.40.5 is safe (0.40.5 doesn't read this column).
    auto_classified_doc_type_id: str | None = None
    # Sprint 25 kb-per-row-extract: cardinality=many doc_type extraction state.
    # 'na' = not applicable (cardinality=one or no doc_type) — default.
    # 'queued' / 'running' = per-row extract in progress.
    # 'succeeded' / 'failed' / 'cancelled' = terminal.
    # Lead chat prompt dispatch reads this field (Sprint 25 §9.2).
    extracted_rows_status: Literal[
        "na",
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    ] = "na"
    summary_json: str | None = None  # Parse summary (JSON object); currently only truncated_rows
    # Truncation info when Excel/CSV exceeds indexed row cap (derived from summary_json, read-only):
    # {"indexed": indexed row count, "total": file total rows}; None if not truncated.
    truncated_rows: KbTruncatedRows | None = None

    version: int = 1
    active_generation_id: str | None = None
    source_observation_json: str | None = Field(default=None, exclude=True)
    source_health: dict[str, Any] | None = None
    source_binding_json: str | None = Field(default=None, exclude=True)
    source_absent: bool = False
    parse_attempts: int = Field(default=0, exclude=True)
    soft_deleted_at: str | None = None

    # Caller-visible permissions (backend computes DTO values)
    can_delete: bool = False
    can_change_scope: bool = False


class KbFileScopePatchRequest(BaseModel):
    """PATCH /api/v1/kb/files/{file_id}/scope body."""

    new_scope: KbScopeIn
    new_scope_id: str
    reason: str | None = None  # P1-N4 admin override requires ≥10 chars


class KbSearchRequest(BaseModel):
    """POST /api/v1/kb/search body."""

    query: str = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=50, strict=True)
    scope_filter: list[KbScopeIn] | None = None


class KbSearchResultChunk(BaseModel):
    """Single chunk in search result."""

    chunk_id: str
    content: str
    file_id: str
    file_name: str
    page: int | None = None
    section_title: str | None = None
    role: str
    score: float  # rerank score (post role-boost)
    scope: str
    # 1-based paragraph index within file; page deep search labels "第 N 段" without page/section. None for old chunks missing it.
    chunk_index: int | None = None
    # Cross-encoder relevance BEFORE role/lexical boosts; None when the reranker
    # fell back to synthetic rank scores (no relevance signal). Page search uses
    # it for the relevance floor (PRD FR6.3); the chat tool does not read it.
    rerank_score: float | None = None
    generation_id: str | None = None
    source_warning: str | None = None


class KbSearchResult(BaseModel):
    """KbSearch 整体结果."""

    chunks: list[KbSearchResultChunk]
    total_candidates: int  # stage 2 raw chromadb candidates (before diversification/filtering; fixed after flooding fix)
    rerank_used: bool
    latency_ms: int
    # Actual model id on rerank success (page deep search uses its relevance floor); None on fallback or no rerank.
    rerank_model: str | None = None
    # Some search scopes (collections) skipped due to timeout/error: incomplete results cannot establish no relevant KB content.
    partial: bool = False
