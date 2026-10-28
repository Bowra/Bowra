"""文档矩阵端点族（kb-matrix M2 tech design §7）。

所有端点：`require_feature_licensed("kb")`
（读写都挂，S4 教训）+ `_require_workspace_member_strict`（**无 legacy fallback**，
kb_records docstring 的 M2 警示落地）；mutation 另挂 `require_not_readonly`
（**cancel 除外**——冻结 team 止损动作，归 ALLOW_ROUTES，review P1）+
`_require_member_rank`；matrix_id 反查 → `matrix.workspace_id != ctx.workspace_id
→ 404`（kb_admin get_extract_rows_status 枚举防护范式）。

owner-or-admin 是显式 helper 不是散写（review P1）：member-rank 对「删矩阵/删列/
取消他人 run」是必要非充分，漏掉 per-handler 判定 = 任意 member 越权。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from typing import TYPE_CHECKING, Any, Literal

import aiosqlite
import anyio
from bowhead_core.access_control import workspace_role_satisfies
from bowhead_core.kb.doc_type_store import MAX_FIELDS_PER_SCHEMA, is_column_group_template
from bowhead_core.kb.graduation import (
    GRADUATION_EXTRACTOR_MODEL,
    GraduationError,
    build_graduation_entries,
    build_graduation_fields,
    compose_description,
    validate_graduation_columns,
)
from bowhead_core.kb.matrix_engine import (
    ETA_SECONDS_PER_CELL,
    MatrixInsufficientCredits,
    MatrixQuotaExceeded,
    MatrixRateLimited,
    MatrixRunConflict,
    MatrixRunError,
    cell_concurrency,
    global_concurrency,
)
from bowhead_core.kb.matrix_store import (
    MATRIX_COLUMN_TYPES,
    cell_is_stale,
    is_raw_string_sentinel,
    new_column_id,
    validate_options,
)
from bowhead_core.kb.permission import KbPermission, ScopeContext
from bowhead_core.kb.telemetry import emit_audit
from bowhead_core.secret_redaction import extract_secrets, wrap_untrusted
from bowhead_core.space_governance import SpaceOperation
from bowhead_router.usage import usage_context
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_feature_licensed, require_not_readonly
from bowhead_gateway.access_context import access_context_to_dict, build_access_context_from_request, has_workspace_access_source
from bowhead_gateway.routes.auth import _current_user

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bowhead_core.access_control import AccessContext
    from bowhead_core.kb.doc_type_store import KbDocTypeStore
    from bowhead_core.kb.extracted_record_store import KbExtractedRecordStore
    from bowhead_core.kb.files_store import KbFilesStore
    from bowhead_core.kb.matrix_engine import KbMatrixEngine
    from bowhead_core.kb.matrix_store import KbMatrixStore, MatrixCellDTO, MatrixDTO, MatrixRunDTO
    from bowhead_core.kb.schemas import KbFileDTO
    from bowhead_core.user_store import User


def _matrix_error(status_code: int, code: str, **payload: Any) -> HTTPException:
    """Return the stable, localizable error envelope used by matrix routes."""
    return HTTPException(status_code, {"error": {"code": code, **payload}})


_COLUMN_VALIDATION_CODES = frozenset(
    {
        "cross_verify_not_applicable",
        "option_duplicate",
        "option_empty",
        "option_too_long",
        "options_not_applicable",
        "options_required",
        "options_too_many",
    }
)


def _column_validation_error(exc: ValueError, *, fallback: str = "invalid_matrix_request") -> HTTPException:
    """Map store validation failures to the finite matrix mutation contract."""
    code = str(exc)
    if code in _COLUMN_VALIDATION_CODES:
        return _matrix_error(422, code)
    if code.startswith("column type must be one of"):
        return _matrix_error(422, "invalid_column_type")
    return _matrix_error(422, fallback)


router = APIRouter(
    prefix="/api/v1/kb",
    tags=["knowledge-base"],
    dependencies=[Depends(require_feature_licensed("kb"))],
)

_MAX_NAME_LEN = 200
_MAX_QUESTION_LEN = 2000
_RECORDS_PAGE = 1000
# Soft guard on JSON-serialized cell values (S-P2-4; generous enough for normal manual input).
_MAX_CELL_VALUE_JSON_LEN = 50_000


# ─── DTOs ───────────────────────────────────────────────────


class _ColumnIn(BaseModel):
    name: str = Field("", max_length=_MAX_NAME_LEN)
    type: str = "question"
    question: str = Field("", max_length=_MAX_QUESTION_LEN)
    extraction_hint: str = Field("", max_length=500)
    # M3 (FR-2 r5): select/multi_select require options; boolean/the four existing types with options → 422.
    # (Shared validate_options validation lives in the store, used by all four write entries.) See FR-39 for cross_verify.
    options: list[str] | None = None
    cross_verify: bool = False


class _MatrixCreateIn(BaseModel):
    workspace_id: str
    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    file_ids: list[str] = Field(default_factory=list)
    columns: list[_ColumnIn] = Field(default_factory=list)


class _FromTemplateIn(BaseModel):
    workspace_id: str
    doc_type_id: str


class _GraduateIn(BaseModel):
    # M4 FR-40/41: snapshot = graduate as a template (fields + records, queryable in chat); template = save fields only.
    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    mode: Literal["snapshot", "template"] = "snapshot"


class _MatrixRenameIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)


class _FilesReplaceIn(BaseModel):
    file_ids: list[str]


class _ColumnPatchIn(BaseModel):
    name: str | None = Field(None, max_length=_MAX_NAME_LEN)
    question: str | None = Field(None, max_length=_MAX_QUESTION_LEN)
    extraction_hint: str | None = Field(None, max_length=500)
    type: str | None = None  # Graduation (§4.5): M3 allows options alongside type (FR-5 r5).
    options: list[str] | None = None
    cross_verify: bool | None = None


class _ColumnDraftIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=_MAX_QUESTION_LEN)


class _RunScopeIn(BaseModel):
    # stale/stale_column: M3 reruns stale cells (FR-12b rows; selection is a derived predicate, includes verified, excludes edited).
    kind: Literal["all_pending", "column", "force_all", "force_column", "stale", "stale_column"]
    column_id: str | None = None


class _CellPatchIn(BaseModel):
    # revert_edited: M3 clears the edit flag (FR-9 r5); set_verified accepts false from M3 (FR-12 r5).
    op: Literal["edit_value", "keep_as_text", "set_verified", "revert_edited"]
    value: Any = None
    verified: bool | None = None


# ─── DI getters ─────────────────────────────────────────────


def _get_matrix_store(request: Request) -> KbMatrixStore:
    s = getattr(request.app.state, "kb_matrix_store", None)
    if s is None:
        raise _matrix_error(503, "matrix_service_unavailable")
    return s


def _get_engine(request: Request) -> KbMatrixEngine:
    e = getattr(request.app.state, "kb_matrix_engine", None)
    if e is None:
        raise _matrix_error(503, "matrix_service_unavailable")
    return e


def _get_files_store(request: Request) -> KbFilesStore:
    fs = getattr(request.app.state, "kb_files_store", None)
    if fs is None:
        raise _matrix_error(503, "matrix_data_service_unavailable")
    return fs


def _ws_from_request(request: Request, fallback: str = "") -> str:
    hdr = request.headers.get("X-Space-Id") or request.headers.get("X-Workspace-Id")
    return hdr or fallback


def _today_str() -> str:
    from datetime import UTC, datetime

    return datetime.now(tz=UTC).date().isoformat()


# ─── authz helpers（§7.3/§7.4）──────────────────────────────


async def _require_workspace_member_strict(
    request: Request,
    user: User,
    workspace_id: str,
    *,
    operation: SpaceOperation = SpaceOperation.WRITE,
) -> tuple[str, AccessContext]:
    """strict 版（§7.4）：RBAC hooks 缺失直接 503——**无 legacy fallback**（矩阵是
    M2 净新面，没有旧 harness 兼容包袱；fail-open = 跨租户面）。"""
    if not has_workspace_access_source(request):
        raise _matrix_error(503, "access_control_unavailable")
    try:
        ctx = await build_access_context_from_request(
            request,
            user=user,
            workspace_id=workspace_id,
            operation=operation,
        )
    except HTTPException as exc:
        detail = exc.detail
        error = detail.get("error") if isinstance(detail, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, str) and code.startswith("ws_access_denied:"):
            raise _matrix_error(403, "workspace_access_denied", workspace_id=workspace_id) from exc
        raise
    request.state.bowhead_access_context = access_context_to_dict(ctx)
    return ctx.workspace_id, ctx


def _require_member_rank(ctx: AccessContext) -> None:
    """mutation 角色下限（FR-34）：rank ≥ member；viewer 是合法成员但只读。"""
    if ctx.is_team_admin or workspace_role_satisfies(ctx.workspace_role, "member"):
        return
    raise _matrix_error(403, "workspace_member_role_required")


def _require_owner_or_admin(owner_user_id: str, ctx: AccessContext) -> None:
    """删矩阵/删列/取消他人 run（FR-34）：member-rank 必要非充分（review P1）。"""
    if owner_user_id == ctx.user_id or ctx.is_team_admin or workspace_role_satisfies(ctx.workspace_role, "ws_admin"):
        return
    raise _matrix_error(403, "owner_or_admin_required")


async def _get_matrix_checked(request: Request, ctx: AccessContext, matrix_id: str) -> MatrixDTO:
    """matrix_id 反查 + 跨空间 404（枚举防护——不 403 不暴露存在性）。"""
    m = await _get_matrix_store(request).get_matrix(matrix_id)
    if m is None or m.workspace_id != ctx.workspace_id or m.team_id != ctx.team_id:
        raise _matrix_error(404, "matrix_not_found")
    return m


async def _validate_file_refs(request: Request, ctx: AccessContext, file_ids: list[str]) -> list[KbFileDTO]:
    """FR-32/FR-35 资源逐个校验（建矩阵/改文件集共用）：任一不存在 → 404 整单拒；
    personal → 422；跨空间/跨 team 探测统一 404 不 403（枚举防护）。"""
    files_store = _get_files_store(request)
    out: list[KbFileDTO] = []
    for file_id in dict.fromkeys(file_ids):
        f = await files_store.get(file_id)
        if f is None or f.soft_deleted_at or f.team_id != ctx.team_id:
            raise _matrix_error(404, "matrix_source_file_unavailable")
        if f.scope == "personal" and KbPermission.owner_of(f) != ctx.user_id:
            raise _matrix_error(404, "matrix_source_file_unavailable")
        if f.scope == "personal":
            raise _matrix_error(422, "personal_files_not_allowed")
        if f.scope in ("space", "workspace"):
            if f.workspace_id != ctx.workspace_id:
                raise _matrix_error(404, "matrix_source_file_unavailable")
        elif f.scope == "team":
            scope_ctx = ScopeContext(user_id=ctx.user_id, workspace_id=ctx.workspace_id, team_id=ctx.team_id, role="user")
            if f.team_id != ctx.team_id or not KbPermission.can_read(f, scope_ctx):
                raise _matrix_error(404, "matrix_source_file_unavailable")
        else:
            raise _matrix_error(404, "matrix_source_file_unavailable")
        from bowhead_gateway.routes.kb import _kb_scope_check

        scope_ctx = ScopeContext(user_id=ctx.user_id, workspace_id=ctx.workspace_id, team_id=ctx.team_id)
        try:
            current = await _kb_scope_check(f.file_id, scope_ctx, files_store, request)
        except HTTPException as exc:
            # A file the caller cannot read gets the same answer as a missing one.
            if exc.status_code != 404:
                raise
            raise _matrix_error(404, "matrix_source_file_unavailable") from None
        if any(getattr(current, key) != getattr(f, key) for key in ("team_id", "workspace_id", "scope", "scope_id", "version")):
            raise _matrix_error(404, "matrix_source_file_unavailable")
        out.append(current)
    return out


async def _files_with_char_counts(request: Request, user: User, files: list[KbFileDTO]) -> list[dict[str, Any]]:
    """files_json 条目（§4.1）：char_count 在文件加入矩阵时算一次（Σ len(content)，
    char≈token 仓库口径）——预估端点纯算术零 chroma 查询。取数失败落 0（只影响预估）。"""
    from bowhead_gateway.routes.kb import _get_effective_kb_store

    kb_store = await _get_effective_kb_store(request, user)
    entries: list[dict[str, Any]] = []
    for f in files:
        char_count = 0
        try:
            chunks = await kb_store.get_ordered_chunks_for_file(f)
            char_count = sum(len(content) for _, content, _ in chunks)
        except Exception:
            logger.warning("kb_matrix.char_count_failed file=%s", f.file_id, exc_info=True)
        entries.append({"file_id": f.file_id, "char_count": char_count})
    return entries


def _column_or_404(matrix: MatrixDTO, column_id: str) -> dict[str, Any]:
    col = next((c for c in matrix.columns if c.get("column_id") == column_id), None)
    if col is None:
        raise _matrix_error(404, "matrix_column_not_found")
    return col


def _file_in_matrix_or_404(matrix: MatrixDTO, file_id: str) -> None:
    if file_id not in {str(f.get("file_id") or "") for f in matrix.files}:
        raise _matrix_error(404, "matrix_file_not_found")


# ─── matrices CRUD ──────────────────────────────────────────


@router.get("/matrices")
async def list_matrices(
    request: Request,
    workspace_id: str,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(
        request,
        user,
        ws,
        operation=SpaceOperation.READ_HISTORY,
    )
    store = _get_matrix_store(request)
    items: list[dict[str, Any]] = []
    for m in await store.list_matrices(ctx.workspace_id):
        summary = await store.status_summary_for_matrix(m.matrix_id)
        items.append(
            {
                "matrix_id": m.matrix_id,
                "name": m.name,
                "template_doc_type_id": m.template_doc_type_id,
                "files_count": len(m.files),
                "columns_count": len(m.columns),
                "status_summary": summary,
                "last_run_at": m.last_run_at,
                "created_by": m.created_by,
                "created_at": m.created_at,
            }
        )
    return {"items": items, "workspace_id": ctx.workspace_id}


@router.post("/matrices", dependencies=[Depends(require_not_readonly)])
async def create_matrix(
    body: _MatrixCreateIn,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    ws = _ws_from_request(request, body.workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    files = await _validate_file_refs(request, ctx, body.file_ids)
    columns = [c.model_dump() for c in body.columns]
    for c in columns:
        if c["type"] not in MATRIX_COLUMN_TYPES:
            raise _matrix_error(422, "invalid_column_type")
    entries = await _files_with_char_counts(request, user, files)
    try:
        m = await _get_matrix_store(request).create_matrix(
            name=body.name,
            workspace_id=ctx.workspace_id,
            team_id=ctx.team_id,
            created_by=user.id,
            files=entries,
            columns=columns,
        )
    except ValueError as e:
        raise _column_validation_error(e) from e
    emit_audit("kb.matrix_created", {"matrix_id": m.matrix_id, "files_count": len(entries), "columns_count": len(m.columns)})
    return {"matrix_id": m.matrix_id, "columns": m.columns}


@router.post("/matrices/from_template", dependencies=[Depends(require_not_readonly)])
async def create_matrix_from_template(
    body: _FromTemplateIn,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """另存为矩阵物化（§7.6）。doc_type_id 过资源校验（review P0——跨空间 404）；
    records 查询恒用 ctx.workspace_id（绝不用 dt.workspace_id 当 scope）。"""
    ws = _ws_from_request(request, body.workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    dt_store: KbDocTypeStore | None = getattr(request.app.state, "kb_doc_type_store", None)
    rec_store: KbExtractedRecordStore | None = getattr(request.app.state, "kb_extracted_record_store", None)
    if dt_store is None or rec_store is None:
        raise _matrix_error(503, "matrix_data_service_unavailable")
    dt = await dt_store.get(body.doc_type_id)
    if dt is None or dt.workspace_id != ctx.workspace_id:
        raise _matrix_error(404, "matrix_template_not_found")
    # M4 (review P3-7): column-group templates (seed/saved templates/graduated snapshots) have no records. This endpoint
    # materializes only records, so these yield dead-end matrices with zero files/cells. Apply column-group templates via
    # the new-matrix flow (prefilled columns + file selection); this endpoint serves record-based template materialization (§7.6).
    if is_column_group_template(dt):
        raise _matrix_error(422, "template_use_new_matrix_flow")

    # Generate columns from dt.fields: question = f.description, prompt_version=1.
    columns: list[dict[str, Any]] = []
    col_id_by_field: dict[str, str] = {}
    for f in dt.fields:
        col_id = new_column_id()
        col_id_by_field[f.name] = col_id
        columns.append(
            {
                "column_id": col_id,
                "name": f.name,
                "type": f.type,
                "question": f.description or "",
                "extraction_hint": f.extraction_hint or "",
                "prompt_version": 1,
            }
        )

    # All records (direct store reads, no HTTP; scope is always ctx.workspace_id).
    records = []
    offset = 0
    while True:
        page = await rec_store.list_for_doc_type(
            workspace_id=ctx.workspace_id,
            doc_type_id=body.doc_type_id,
            limit=_RECORDS_PAGE,
            offset=offset,
            doc_type_fields=dt.fields,
        )
        records.extend(page)
        if len(page) < _RECORDS_PAGE:
            break
        offset += _RECORDS_PAGE

    # File filtering (security review S-P1-2): materialization uses the same gate as creation. Never materialize records from
    # personal files (visible only to the uploader) or files that fail team ACL checks; otherwise a single from_template
    # exposes restricted files' extracted values + names to all space members.
    # Also calculate actual char_count (B-P2-5: zero systematically deflates estimates, distorting the FR-14 hard gate).
    files_store = _get_files_store(request)
    allowed_files: dict[str, Any] = {}
    file_versions: dict[str, int] = {}
    skipped_files = 0
    scope_ctx = ScopeContext(user_id=ctx.user_id, workspace_id=ctx.workspace_id, team_id=ctx.team_id, role="user")
    for rec in records:
        if rec.file_id in file_versions or rec.file_id in allowed_files:
            continue
        dto = await files_store.get(rec.file_id)
        ok = (
            dto is not None
            and not dto.soft_deleted_at
            and dto.scope != "personal"
            and (
                (dto.scope in ("space", "workspace") and dto.workspace_id == ctx.workspace_id)
                or (dto.scope == "team" and dto.team_id == ctx.team_id and KbPermission.can_read(dto, scope_ctx))
            )
        )
        if not ok:
            skipped_files += 1
            file_versions[rec.file_id] = -1  # Sentinel: already determined unavailable.
            continue
        allowed_files[rec.file_id] = dto
        file_versions[rec.file_id] = int(getattr(dto, "version", 1) or 1)
    file_ids = list(allowed_files.keys())
    entries = await _files_with_char_counts(request, user, list(allowed_files.values()))

    store = _get_matrix_store(request)
    m = await store.create_matrix(
        name=f"{dt.name}（矩阵）",
        workspace_id=ctx.workspace_id,
        team_id=ctx.team_id,
        created_by=user.id,
        files=entries,
        columns=columns,
        template_doc_type_id=body.doc_type_id,
    )

    # Materialize cells per record/field (§7.6): copy variants unchanged to preserve multiple sources;
    # cell prompt_version=1 matches columns (review P2 enforces NOT NULL); extract_failed rows produce no cells.
    cells: list[dict[str, Any]] = []
    for rec in records:
        if rec.file_id not in allowed_files:
            continue  # Skip entire records from restricted files (S-P1-2).
        if rec.verify_status == "extract_failed":
            continue
        edited_fields = set(rec.edited_by_user or [])
        for fname, variants in (rec.fields or {}).items():
            col_id = col_id_by_field.get(fname)
            if col_id is None or not variants:
                continue
            primary = next((v for v in variants if isinstance(v, dict) and v.get("is_primary")), None)
            norm = primary.get("normalized") if isinstance(primary, dict) else None
            status = "needs_review" if is_raw_string_sentinel(norm if isinstance(norm, dict) else None) else "done"
            edited = fname in edited_fields
            verified = rec.verify_status == "approved" and status == "done"
            cells.append(
                {
                    "file_id": rec.file_id,
                    "column_id": col_id,
                    "workspace_id": ctx.workspace_id,
                    "team_id": ctx.team_id,
                    "status": status,
                    "variants": variants,
                    "edited_by_user": edited,
                    "verified": verified,
                    "prompt_version": 1,
                    "file_version": file_versions.get(rec.file_id, 1),
                }
            )
    inserted = await store.insert_cells_bulk(m.matrix_id, cells)
    emit_audit(
        "kb.matrix_materialized",
        {
            "matrix_id": m.matrix_id,
            "doc_type_id": body.doc_type_id,
            "records": len(records),
            "cells": inserted,
            "skipped_files": skipped_files,
        },
    )
    return {"matrix_id": m.matrix_id, "files_count": len(file_ids), "cells": inserted}


@router.post("/matrices/{matrix_id}/graduate", dependencies=[Depends(require_not_readonly)])
async def graduate_matrix(
    matrix_id: str,
    body: _GraduateIn,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """矩阵转正（M4 FR-40）：整矩阵固化为 doc_type——mode=snapshot 含每文件一行
    rows（chat 经既有 kb_query_records 查询，零新工具）；mode=template 仅字段
    （FR-41 存为模板）。冻结快照 auto_extract=off + graduated_mode 生态 gate。

    两库非原子（kb_doc_types.db / kb_extracted_rows.db 分离）→ pending-flip：
    先建 pending_snapshot（对一切列表面不可见）→ rows 直存 + row_count 回写 →
    flip snapshot；rows 失败补偿软删（墓碑让名）；崩溃窗口由 boot 自愈 sweep 收口。
    幂等锚 = matrix.last_seq（同快照重试复用既有 doc_type；重跑刷新后产新快照）。
    """
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    # Best-effort throttling (graduation uses no LLM, but heavy IO with no credit backpressure — r3 risk ⑥).
    rl = getattr(getattr(request.app.state, "user_store", None), "rate_limit_check", None)
    if callable(rl):
        grad_limit = int(os.environ.get("BOWHEAD_KB_MATRIX_RL_GRADUATE", "") or 20)
        if not await rl(f"kb_matrix_graduate:{user.id}", limit=grad_limit, window_s=3600):
            raise _matrix_error(429, "rate_limited")

    dt_store: KbDocTypeStore | None = getattr(request.app.state, "kb_doc_type_store", None)
    rows_store = getattr(request.app.state, "kb_extracted_rows_store", None)
    if dt_store is None or rows_store is None:
        raise _matrix_error(503, "matrix_data_service_unavailable")

    # ① Hard column cap (doc_type MAX_FIELDS_PER_SCHEMA=20, a user-visible product limit — FR-40).
    if len(matrix.columns) > MAX_FIELDS_PER_SCHEMA:
        raise _matrix_error(
            422,
            "too_many_columns_to_graduate",
            max=MAX_FIELDS_PER_SCHEMA,
            got=len(matrix.columns),
        )
    if not matrix.columns:
        raise _matrix_error(422, "empty_matrix")
    if body.mode == "snapshot" and not matrix.files:
        # Zero-file snapshots pollute kb_list_tables with visible empty tables (review P3-8); fields-only templates are exempt.
        raise _matrix_error(422, "empty_matrix")
    # Prevalidate column names (review P1-1/P1-2/P2-1: duplicates silently lose columns + yield confidently wrong SUMs;
    # characters outside the rows allowlist make graduated tables unqueryable; names >100 are rejected by core but leak raw Chinese).
    try:
        validate_graduation_columns(matrix.columns)
    except GraduationError as ge:
        raise _matrix_error(422, ge.code, **ge.detail) from ge
    # Row cap (security review P3-2: graduation has no credit backpressure; prevent large-matrix storage amplification/long transactions).
    grad_max_files = int(os.environ.get("BOWHEAD_KB_MATRIX_GRADUATE_MAX_FILES", "") or 2000)
    if len(matrix.files) > grad_max_files:
        raise _matrix_error(
            422,
            "too_many_files_to_graduate",
            max=grad_max_files,
            got=len(matrix.files),
        )

    # ② Revalidate scope (FR-40: file ACLs can tighten after cells materialize, exposing data). Reuse
    # _validate_file_refs = re-fetch + personal 422 + can_read/cross-space 404;
    # any failure **fails the entire request closed**, including soft-deleted files: v1 is strict.
    matrix_file_ids = [str(f.get("file_id") or "") for f in matrix.files if f.get("file_id")]
    await _validate_file_refs(request, ctx, matrix_file_ids)

    # ③ Idempotency (reuse the same (matrix, last_seq, mode); pending matches self-heal crash residue in place).
    want_modes = ("template",) if body.mode == "template" else ("pending_snapshot", "snapshot")
    for prior in await dt_store.find_graduated_for_matrix(ctx.workspace_id, matrix_id, seq=matrix.last_seq):
        if prior.graduated_mode not in want_modes:
            continue
        if prior.graduated_mode == "pending_snapshot":
            # Self-healing = **rebuild missing rows, then flip** (review P2-1: a multibatch crash may leave only some rows;
            # flipping on count>0 returns an incomplete snapshot as complete. Matching last_seq means cells have not
            # changed; rerunning create_many_prewrapped is idempotent: UNIQUE skips existing rows, inserts missing ones).
            # Flip uses an absolute count, winner-only (review P2-4: relative deltas permanently double counts under concurrency).
            cells = await _get_matrix_store(request).list_cells(matrix_id)
            entries, _stats = build_graduation_entries(matrix.files, matrix.columns, cells)
            try:
                await rows_store.create_many_prewrapped(
                    entries,
                    doc_type_id=prior.doc_type_id,
                    workspace_id=ctx.workspace_id,
                    created_by=user.id,
                    extractor_model=GRADUATION_EXTRACTOR_MODEL,
                )
            except Exception:
                logger.exception("kb_matrix.graduate_selfheal_rows_failed dt=%s", prior.doc_type_id)
                raise _matrix_error(502, "graduation_rows_failed") from None
            true_count = await rows_store.count_for_doc_type(workspace_id=ctx.workspace_id, doc_type_id=prior.doc_type_id)
            await dt_store.flip_graduated_snapshot(prior.doc_type_id, row_count=true_count)
            emit_audit(
                "kb.matrix_graduated",
                {
                    "matrix_id": matrix_id,
                    "doc_type_id": prior.doc_type_id,
                    "columns": len(matrix.columns),
                    "records": true_count,
                    "mode": "snapshot",
                    "outcome": "selfhealed",
                },
            )
            return {
                "doc_type_id": prior.doc_type_id,
                "mode": "snapshot",
                "records": true_count,
                "columns": len(matrix.columns),
                "name": prior.name,
                "reused": True,
            }
        return {
            "doc_type_id": prior.doc_type_id,
            "mode": body.mode,
            "records": prior.row_count,
            "columns": len(prior.fields),
            "name": prior.name,
            "reused": True,
        }

    # ④ Precheck name collisions (structured 409 + suggested name; UNIQUE handles races). Probe suggestions in a loop
    # (review P3-2: a fixed '· 2' makes the third graduation's suggested name collide itself).
    if await dt_store.name_exists(ctx.workspace_id, body.name):
        suggestion = f"{body.name} · {_today_str()}"
        for i in range(2, 10):
            cand = f"{body.name} · {i}"
            if not await dt_store.name_exists(ctx.workspace_id, cand):
                suggestion = cand
                break
        raise _matrix_error(409, "doc_type_name_taken", suggestion=suggestion)

    # ⑤ Create doc_type (frozen: auto_extract=off; snapshots start pending and invisible).
    fields = build_graduation_fields(matrix.columns)
    grad_mode = "template" if body.mode == "template" else "pending_snapshot"
    try:
        dt_id = await dt_store.create(
            workspace_id=ctx.workspace_id,
            name=body.name,
            created_by=user.id,
            description=compose_description(matrix.name, len(matrix.files), len(matrix.columns), _today_str()),
            fields=fields,
            cardinality="many",
            auto_extract_enabled=False,
            graduated_from_matrix_id=matrix_id,
            graduated_from_seq=matrix.last_seq,
            graduated_mode=grad_mode,
        )
    except ValueError as e:
        raise _matrix_error(422, "invalid_graduation_request") from e
    except aiosqlite.IntegrityError as e:
        raise _matrix_error(409, "doc_type_name_taken", suggestion=f"{body.name} · 2") from e

    records = 0
    if body.mode == "snapshot":
        # ⑥ Store rows directly (bypass create_many re-wrapping, which loses fields — review P0) + flip with an absolute count.
        cells = await _get_matrix_store(request).list_cells(matrix_id)
        entries, stats = build_graduation_entries(matrix.files, matrix.columns, cells)
        try:
            result = await rows_store.create_many_prewrapped(
                entries,
                doc_type_id=dt_id,
                workspace_id=ctx.workspace_id,
                created_by=user.id,
                extractor_model=GRADUATION_EXTRACTOR_MODEL,
            )
            records = result.inserted_count
            # Flip with an absolute count, winner-only (review P2-4: update_row_count relative deltas double counts
            # under concurrency; inserted at flip time is authoritative).
            await dt_store.flip_graduated_snapshot(dt_id, row_count=records)
        except Exception:
            # ⑦ Compensation: row failure → soft-delete pending (tombstone frees the name) + cascade-delete committed batch rows
            # (review P2-3: partially committed rows with excerpts left under a tombstoned doc_type create compliance debt);
            # compensation failure never masks the original error (self-healing sweep handles the residue window).
            logger.exception("kb_matrix.graduate_rows_failed matrix=%s dt=%s", matrix_id, dt_id)
            try:
                await dt_store.soft_delete(dt_id)
                await rows_store.soft_delete_for_doc_type(dt_id, workspace_id=ctx.workspace_id)
            except Exception:
                logger.exception("kb_matrix.graduate_compensation_failed dt=%s", dt_id)
            emit_audit(
                "kb.matrix_graduated",
                {
                    "matrix_id": matrix_id,
                    "doc_type_id": dt_id,
                    "columns": len(matrix.columns),
                    "records": 0,
                    "mode": "snapshot",
                    "outcome": "rows_failed",
                },
            )
            raise _matrix_error(502, "graduation_rows_failed") from None
        emit_audit(
            "kb.matrix_graduated",
            {
                "matrix_id": matrix_id,
                "doc_type_id": dt_id,
                "columns": len(matrix.columns),
                "records": records,
                "cells_absent": stats["cells_absent"],
                "mode": "snapshot",
            },
        )
    else:
        emit_audit(
            "kb.matrix_graduated",
            {"matrix_id": matrix_id, "doc_type_id": dt_id, "columns": len(matrix.columns), "records": 0, "mode": "template"},
        )
    return {
        "doc_type_id": dt_id,
        "mode": body.mode,
        "records": records,
        "columns": len(matrix.columns),
        "name": body.name,
        "reused": False,
    }


@router.patch("/matrices/{matrix_id}", dependencies=[Depends(require_not_readonly)])
async def rename_matrix(
    matrix_id: str,
    body: _MatrixRenameIn,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    await _get_matrix_store(request).rename_matrix(matrix.matrix_id, body.name, workspace_id=ctx.workspace_id)
    return {"matrix_id": matrix_id, "name": body.name}


@router.delete("/matrices/{matrix_id}", dependencies=[Depends(require_not_readonly)])
async def delete_matrix(
    matrix_id: str,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    _require_owner_or_admin(matrix.created_by, ctx)  # FR-34 row-deletion permission.
    await _get_matrix_store(request).soft_delete_matrix(matrix.matrix_id, workspace_id=ctx.workspace_id)
    emit_audit("kb.matrix_deleted", {"matrix_id": matrix_id})
    return {"matrix_id": matrix_id, "deleted": True}


@router.put("/matrices/{matrix_id}/files", dependencies=[Depends(require_not_readonly)])
async def replace_matrix_files(
    matrix_id: str,
    body: _FilesReplaceIn,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    files = await _validate_file_refs(request, ctx, body.file_ids)
    entries = await _files_with_char_counts(request, user, files)
    await _get_matrix_store(request).replace_files(matrix.matrix_id, entries, workspace_id=ctx.workspace_id)
    emit_audit("kb.matrix_files_changed", {"matrix_id": matrix_id, "files_count": len(entries)})
    return {"matrix_id": matrix_id, "files_count": len(entries)}


# ─── columns ────────────────────────────────────────────────


@router.post("/matrices/{matrix_id}/columns", dependencies=[Depends(require_not_readonly)])
async def add_column(
    matrix_id: str,
    body: _ColumnIn,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    try:
        column_id = await _get_matrix_store(request).add_column(matrix.matrix_id, body.model_dump(), workspace_id=ctx.workspace_id)
    except ValueError as e:
        raise _column_validation_error(e) from e
    emit_audit("kb.matrix_column_added", {"matrix_id": matrix_id, "column_id": column_id or ""})
    return {"matrix_id": matrix_id, "column_id": column_id}


# Batch 2-a: this call site previously had no timeout. Column drafts are single fast-tier calls of ≤300 tokens;
# 30s accommodates slow providers without leaving users waiting indefinitely on a spinner.
_DRAFT_TIMEOUT_S = 30.0


@router.post("/matrices/{matrix_id}/columns/draft", dependencies=[Depends(require_not_readonly)])
async def draft_column(
    matrix_id: str,
    body: _ColumnDraftIn,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """AI 建议列定义（FR-3）：{question} → {name, type, extraction_hint}。
    lead binding + json_object + sanitize（draft_extract_schema 范式，不复用端点
    ——多字段形状 + 无 question 型）。起草失败 200 + fallback: true 不阻塞手填。"""
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(
        request,
        user,
        ws,
        operation=SpaceOperation.EXECUTE,
    )
    _require_member_rank(ctx)
    await _get_matrix_checked(request, ctx, matrix_id)
    # Best-effort throttling (security review S-P2-3): this endpoint calls the LLM directly without billing; without a bucket,
    # scripts can consume platform quota for free. Skip fake user_store instances (test harness) without this method.
    rl = getattr(getattr(request.app.state, "user_store", None), "rate_limit_check", None)
    if callable(rl):
        draft_limit = int(os.environ.get("BOWHEAD_KB_MATRIX_RL_DRAFT", "") or 60)
        if not await rl(f"kb_matrix_draft:{user.id}", limit=draft_limit, window_s=3600):
            raise _matrix_error(429, "rate_limited")
    fallback = {"name": body.question[:20], "type": "question", "extraction_hint": "", "options": None, "fallback": True}
    from bowhead_gateway.turn_service import resolve_team_model_runtime

    team_runtime = await resolve_team_model_runtime(
        request.app.state,
        ctx.team_id,
        entrypoint="tool_continuation",
    )
    if not team_runtime.available:
        return {**fallback, "reason": team_runtime.reason}
    router_obj = team_runtime.router
    from bowhead_router import LLMRequest, Message

    sys_prompt = (
        "你是文档矩阵的列定义助手. 用户给出一个想对每份文档提的问题, 你输出该列的定义草稿.\n\n"
        'Schema: {"name": str, "type": "string|number|currency|date|question|select|multi_select|boolean", '
        '"extraction_hint": str, "options": [str] | null}\n\n'
        "规则:\n"
        "1. name = 问题的简短列名 (≤10 字)\n"
        "2. type: 答案恒为金额→currency, 日期→date, 数量→number, 短文本字段→string, 开放性问题→question;\n"
        "   分类/标签且选项可枚举→select (单选) 或 multi_select (可多个), 是非判断→boolean\n"
        "3. select/multi_select 必须给 options (2-10 个短选项); 其他类型 options=null\n"
        "4. extraction_hint 提示从文档哪里找 (没把握留空)\n"
        "5. 仅输出 JSON, 不解释\n"
    )
    # Batch 2-a §3.5: question is free-form user text, previously interpolated directly into the prompt without protection.
    # Wrap + redact; **do not run lint_user_append**: it raises on a hit and targets the manifest's
    # system.append instruction slot; applying it to one-off questions falsely rejects inputs such as '合同里写忽略之前的规则
    # 那条怎么理解' and violates invariant 3.
    redacted_q, _hits = extract_secrets(body.question)
    user_prompt = wrap_untrusted(redacted_q, tag="question")
    try:
        # Batch 2-a §3.6: attribution + hard timeout (previously none at this call site).
        with usage_context(team_id=user.team_id, user_id=user.id, reason="kb_matrix_column_draft"), anyio.fail_after(_DRAFT_TIMEOUT_S):
            resp = await router_obj.invoke(
                "echo",  # Tiering: editable column drafts (≤300 tokens) → fast tier.
                LLMRequest(
                    model="",
                    messages=[Message(role="user", content=user_prompt)],
                    system=sys_prompt,
                    max_output_tokens=300,
                    temperature=0.2,
                    response_format={"type": "json_object"},
                ),
            )
        raw = (resp.content or "").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
    except Exception:
        logger.warning("kb_matrix.draft_column_failed matrix=%s", matrix_id, exc_info=True)
        return fallback
    if not isinstance(data, dict):
        return fallback
    type_ = str(data.get("type", "question")).lower().strip()
    if type_ not in MATRIX_COLUMN_TYPES:
        type_ = "question"
    # Sanitize options (uniform fallback, review PRD-P3-2): invalid AI options →
    # fall back to a bare question column (drafts are suggestions, not submissions: no 422, consistent with M2 type sanitization).
    options: list[str] | None = None
    raw_options = data.get("options")
    if type_ in ("select", "multi_select"):
        try:
            options = validate_options(type_, [str(o) for o in raw_options] if isinstance(raw_options, list) else None)
        except ValueError:
            type_, options = "question", None
    name = str(data.get("name", "")).strip()[:100]
    if not name:
        return fallback
    return {
        "name": name,
        "type": type_,
        "extraction_hint": str(data.get("extraction_hint", ""))[:300],
        "options": options,
        "fallback": False,
    }


@router.patch("/matrices/{matrix_id}/columns/{column_id}", dependencies=[Depends(require_not_readonly)])
async def update_column(
    matrix_id: str,
    column_id: str,
    body: _ColumnPatchIn,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    _column_or_404(matrix, column_id)
    store = _get_matrix_store(request)
    if body.type is not None:
        # Refine promotion as the sole change (FR-5 r5): M3 allows {type, options} together
        # (select/multi_select promotion needs options), still not alongside name/question/hint/
        # cross_verify changes (impl-diff review P3-2: promote silently drops cross_verify sent together;
        # return explicit 422 instead of silently dropping it; change cv via a separate PATCH).
        if body.name is not None or body.question is not None or body.extraction_hint is not None or body.cross_verify is not None:
            raise _matrix_error(422, "promote_must_be_sole_change")
        try:
            promoted = await store.promote_column(matrix.matrix_id, column_id, body.type, workspace_id=ctx.workspace_id, options=body.options)
        except ValueError as e:
            raise _column_validation_error(e, fallback="invalid_promotion") from e
        emit_audit("kb.matrix_column_promoted", {"matrix_id": matrix_id, "column_id": column_id, "new_type": body.type})
        return {"matrix_id": matrix_id, "column": promoted}
    try:
        updated = await store.update_column(
            matrix.matrix_id,
            column_id,
            workspace_id=ctx.workspace_id,
            name=body.name,
            question=body.question,
            extraction_hint=body.extraction_hint,
            options=body.options,
            cross_verify=body.cross_verify,
        )
    except ValueError as e:
        raise _column_validation_error(e) from e
    if updated is None:
        raise _matrix_error(404, "matrix_column_not_found")
    emit_audit("kb.matrix_column_edited", {"matrix_id": matrix_id, "column_id": column_id, "prompt_version": updated.get("prompt_version")})
    return {"matrix_id": matrix_id, "column": updated}


@router.delete("/matrices/{matrix_id}/columns/{column_id}", dependencies=[Depends(require_not_readonly)])
async def delete_column(
    matrix_id: str,
    column_id: str,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    _column_or_404(matrix, column_id)
    _require_owner_or_admin(matrix.created_by, ctx)
    await _get_matrix_store(request).delete_column(matrix.matrix_id, column_id, workspace_id=ctx.workspace_id)
    emit_audit("kb.matrix_column_deleted", {"matrix_id": matrix_id, "column_id": column_id})
    return {"matrix_id": matrix_id, "deleted": True}


# ─── runs ───────────────────────────────────────────────────


def _run_error_to_http(e: MatrixRunError) -> HTTPException:
    detail = {"error": {"code": e.code, **e.payload}}
    if isinstance(e, MatrixRunConflict):
        return HTTPException(409, detail)
    if isinstance(e, MatrixInsufficientCredits):
        return HTTPException(402, detail)
    if isinstance(e, (MatrixQuotaExceeded, MatrixRateLimited)):
        return HTTPException(429, detail)
    return HTTPException(422, detail)


@router.post("/matrices/{matrix_id}/runs/estimate", dependencies=[Depends(require_not_readonly)])
async def estimate_run(
    matrix_id: str,
    body: _RunScopeIn,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """§6.1 预估：只读但含余额，挂 require_not_readonly（冻结 team 本就不能跑，一致性优先）。"""
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(
        request,
        user,
        ws,
        operation=SpaceOperation.EXECUTE,
    )
    _require_member_rank(ctx)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    try:
        return await _get_engine(request).estimate_run(matrix, body.model_dump(exclude_none=True), user_id=user.id)
    except ValueError as e:
        raise _matrix_error(422, "invalid_matrix_run_scope") from e


@router.post("/matrices/{matrix_id}/runs", status_code=202, dependencies=[Depends(require_not_readonly)])
async def start_run(
    matrix_id: str,
    body: _RunScopeIn,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(
        request,
        user,
        ws,
        operation=SpaceOperation.EXECUTE,
    )
    _require_member_rank(ctx)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    try:
        run = await _get_engine(request).start_run(matrix, body.model_dump(exclude_none=True), initiated_by=user.id)
    except MatrixRunError as e:
        raise _run_error_to_http(e) from e
    except ValueError as e:
        raise _matrix_error(422, "invalid_matrix_run_scope") from e
    return {
        "run_id": run.run_id,
        "cells_total": run.cells_total,
        "skipped_edited": run.skipped_edited,
        "credits_estimated": run.credits_estimated,
    }


@router.post("/matrices/{matrix_id}/runs/{run_id}/cancel")
async def cancel_run(
    matrix_id: str,
    run_id: str,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """取消（FR-19/FR-34：发起者或 admin）。**不挂 require_not_readonly，归
    ALLOW_ROUTES**（review P1）：team 冻结时在飞 run 仍在烧 credits，cancel 是止损
    动作，gating 它 = 冻结 team 无法停止消耗（readonly-gate「remediation」类目同理由）。"""
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(
        request,
        user,
        ws,
        operation=SpaceOperation.EXECUTE,
    )
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    store = _get_matrix_store(request)
    run = await store.get_run(run_id)
    if run is None or run.matrix_id != matrix.matrix_id:
        raise _matrix_error(404, "matrix_run_not_found")
    _require_owner_or_admin(run.initiated_by, ctx)  # initiated_by comes from the runs table and cannot be forged.
    if not await _get_engine(request).cancel_run(run_id):
        raise _matrix_error(409, "run_not_cancellable")
    return {"run_id": run_id, "status": "cancelling"}


@router.post("/matrices/{matrix_id}/cells/{file_id}/{column_id}/rerun", dependencies=[Depends(require_not_readonly)])
async def rerun_cell(
    matrix_id: str,
    file_id: str,
    column_id: str,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """单格重跑（FR-12b：任意态可发起含 edited，就地执行不弹窗；独立 rate limit 桶）。"""
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(
        request,
        user,
        ws,
        operation=SpaceOperation.EXECUTE,
    )
    _require_member_rank(ctx)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    _column_or_404(matrix, column_id)
    _file_in_matrix_or_404(matrix, file_id)
    scope = {"kind": "single_cell", "file_id": file_id, "column_id": column_id}
    try:
        run = await _get_engine(request).start_run(matrix, scope, initiated_by=user.id)
    except MatrixRunError as e:
        raise _run_error_to_http(e) from e
    except ValueError as e:
        raise _matrix_error(422, "invalid_matrix_run_scope") from e
    return {"run_id": run.run_id, "cells_total": run.cells_total}


# ─── cells (three user-write ops) ────────────────────────────


@router.patch("/matrices/{matrix_id}/cells/{file_id}/{column_id}", dependencies=[Depends(require_not_readonly)])
async def patch_cell(
    matrix_id: str,
    file_id: str,
    column_id: str,
    body: _CellPatchIn,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    col = _column_or_404(matrix, column_id)
    _file_in_matrix_or_404(matrix, file_id)
    await _validate_file_refs(request, ctx, [file_id])
    store = _get_matrix_store(request)

    if body.op == "edit_value":
        # Cell-value limit (security review S-P2-4): unbounded value: Any lets MB-sized payloads land directly in
        # variants_json and be amplified by full-snapshot replay. A generous soft guard does not reject normal input outright.
        try:
            if len(json.dumps(body.value, ensure_ascii=False)) > _MAX_CELL_VALUE_JSON_LEN:
                raise _matrix_error(422, "cell_value_too_large")
        except (TypeError, ValueError) as e:
            raise _matrix_error(422, "cell_value_not_serializable") from e
        cell = await store.edit_cell_value(
            matrix.matrix_id,
            file_id,
            column_id,
            value=body.value,
            column=col,
            workspace_id=ctx.workspace_id,
            team_id=ctx.team_id,
        )
        if cell is None:
            raise _matrix_error(404, "matrix_cell_not_found")
        emit_audit("kb.matrix_cell_edited", {"matrix_id": matrix_id, "file_id": file_id, "column_id": column_id})
    elif body.op == "keep_as_text":
        cell = await store.keep_cell_as_text(matrix.matrix_id, file_id, column_id, workspace_id=ctx.workspace_id)
        if cell is None:
            raise _matrix_error(404, "matrix_cell_not_found")
        emit_audit("kb.matrix_cell_kept_as_text", {"matrix_id": matrix_id, "file_id": file_id, "column_id": column_id})
    elif body.op == "revert_edited":
        # M3 clears the edit flag (FR-9 r5): clear edited + cache_key/cached; retain the manual value as the current value,
        # now overwritable by batches (no superseded preservation). Unedited cells → 422 (helps FE catch stale UI).
        try:
            cell = await store.revert_cell_edited(matrix.matrix_id, file_id, column_id, workspace_id=ctx.workspace_id)
        except ValueError as e:
            raise _matrix_error(422, "not_edited") from e
        if cell is None:
            raise _matrix_error(404, "matrix_cell_not_found")
        emit_audit("kb.matrix_cell_edit_reverted", {"matrix_id": matrix_id, "file_id": file_id, "column_id": column_id})
    else:  # set_verified (M3 accepts false to undo verification, FR-12 r5).
        if body.verified is None:
            raise _matrix_error(422, "verified_required")
        try:
            cell, changed = await store.set_cell_verified(matrix.matrix_id, file_id, column_id, workspace_id=ctx.workspace_id, verified=body.verified)
        except ValueError as e:
            raise _matrix_error(422, "verify_requires_done") from e
        if cell is None:
            raise _matrix_error(404, "matrix_cell_not_found")
        # No-ops (e.g. repeated undo during a polling race) generate no false audit entries (tech-design-m3 §4.3).
        if changed:
            emit_audit(
                "kb.matrix_cell_verified" if body.verified else "kb.matrix_cell_verify_reverted",
                {"matrix_id": matrix_id, "file_id": file_id, "column_id": column_id},
            )
    if cell is None:  # All three branches already return 404; defensive check (python -O strips asserts, S-P2-4).
        raise _matrix_error(404, "matrix_cell_not_found")
    return {"cell": _cell_payload(cell, matrix)}


# ─── snapshot（§7.5）────────────────────────────────────────


def _poll_ms() -> int:
    try:
        return int(os.environ.get("BOWHEAD_KB_MATRIX_POLL_MS", "") or 3000)
    except ValueError:
        return 3000


def _cell_payload(cell: MatrixCellDTO, matrix: MatrixDTO, file_version_now: int | None = None) -> dict[str, Any]:
    """快照 cell 形状。stale 纯派生（决策 3）——M3 起走 `cell_is_stale` 单一权威谓词
    （与 engine.expand_targets 的「重跑已过期」圈选双消费，公式漂移即 UT 红）。"""
    col = next((c for c in matrix.columns if c.get("column_id") == cell.column_id), None)
    col_pv = int(col.get("prompt_version") or 1) if col else 1
    stale = cell_is_stale(cell.status, cell.prompt_version, col_pv, cell.file_version, file_version_now)
    return {
        "status": cell.status,
        "variants": cell.variants,
        "fail_reason": cell.fail_reason,
        "edited": cell.edited_by_user,
        "verified": cell.verified,
        "stale": stale,
        "cached": cell.cached,
        "kb_generation_id": cell.kb_generation_id,
        "kb_source_check_state": cell.kb_source_check_state,
        "prompt_version": cell.prompt_version,
        "seq": cell.seq,
    }


def _run_payload(run: MatrixRunDTO | None, ctx: AccessContext | None = None, user_id: str = "") -> dict[str, Any] | None:
    if run is None:
        return None
    remaining = max(0, run.cells_total - run.cells_done - run.cells_failed)
    concurrency = max(1, min(cell_concurrency(), global_concurrency()))
    # Server-authoritative can_cancel (M3 FR-34/FR-21 r5: members may cancel only their own runs;
    # FE hides cancel accordingly, without shadow role checks — same pattern as can_edit).
    can_cancel = False
    if ctx is not None:
        can_cancel = run.initiated_by == user_id or ctx.is_team_admin or workspace_role_satisfies(ctx.workspace_role, "ws_admin")
    return {
        "run_id": run.run_id,
        "status": run.status,
        "scope": {"kind": run.scope.get("kind"), "column_id": run.scope.get("column_id")},
        "cells_total": run.cells_total,
        "cells_done": run.cells_done,
        "cells_failed": run.cells_failed,
        "skipped_edited": run.skipped_edited,
        "error_code": run.error_code,
        "eta_s": math.ceil(ETA_SECONDS_PER_CELL * remaining / concurrency),
        "initiated_by": run.initiated_by,
        "can_cancel": can_cancel,
    }


@router.get("/matrices/{matrix_id}/snapshot")
async def get_snapshot(
    matrix_id: str,
    request: Request,
    workspace_id: str = "",
    since_seq: int | None = None,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(
        request,
        user,
        ws,
        operation=SpaceOperation.READ_HISTORY,
    )
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    store = _get_matrix_store(request)
    # M3 decision 5: runs[] = active + latest 5 terminal runs (excluding single_cell), globally ordered by started_at DESC;
    # compatibility run = runs[0] (deprecated). Five terminal runs prevent later completions from displacing earlier
    # undismissed failure banners (review PRD-P1-1).
    runs = await store.list_runs_for_snapshot(matrix.matrix_id)
    runs_payload = [_run_payload(r, ctx, user.id) for r in runs]
    run_compat = runs_payload[0] if runs_payload else None

    all_cells = await store.list_cells(matrix.matrix_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws, operation=SpaceOperation.READ_HISTORY)
    # Project every source through the current viewer permissions.
    from bowhead_core.kb.generations import use_guard_enabled
    from bowhead_core.kb.maintenance import cached_source_state

    index_version = None
    if use_guard_enabled():
        from bowhead_gateway.kb_maintenance import team_runtime

        try:
            runtime = await team_runtime(request.app, ctx.team_id)
            index_version = runtime.kb_store.index_version
        except Exception:
            index_version = None
    file_meta: dict[str, tuple[str, bool, int]] = {}
    source_states = {}
    for f in matrix.files:
        fid = str(f.get("file_id") or "")
        try:
            dto = (await _validate_file_refs(request, ctx, [fid]))[0]
        except HTTPException:
            dto = None
        if dto is None or dto.soft_deleted_at:
            file_meta[fid] = ("", True, 1)
        else:
            file_meta[fid] = (dto.filename, False, int(dto.version or 1))
            if use_guard_enabled():
                source_states[fid] = cached_source_state(dto, index_version=index_version)

    def visible_cell(cell):
        meta = file_meta.get(cell.file_id, ("", True, 1))
        if meta[1]:
            return {
                "status": "failed",
                "variants": [],
                "fail_reason": "source_unavailable",
                "edited": False,
                "verified": False,
                "stale": False,
                "cached": False,
                "prompt_version": cell.prompt_version,
                "seq": cell.seq,
            }
        return {**_cell_payload(cell, matrix, meta[2]), "current_source": source_states.get(cell.file_id)}

    # Source access can change without matrix seq moving. Return a full projection
    # on polls as well, so revoked names/old variants cannot survive a delta merge.
    can_edit = ctx.is_team_admin or workspace_role_satisfies(ctx.workspace_role, "member")
    return {
        "matrix": {
            "matrix_id": matrix.matrix_id,
            "name": matrix.name,
            "can_edit": can_edit,  # e2e-plan P2-1 delivered: FE shadow role checks retired.
            "seq": matrix.last_seq,
            "structure_seq": matrix.structure_seq,
            "poll_ms": _poll_ms(),
            "template_doc_type_id": matrix.template_doc_type_id,
        },
        "columns": [
            {
                "column_id": c.get("column_id"),
                "name": c.get("name"),
                "type": c.get("type"),
                "question": c.get("question"),
                "extraction_hint": c.get("extraction_hint"),
                "prompt_version": c.get("prompt_version"),
                "options": c.get("options"),
                "cross_verify": bool(c.get("cross_verify")),
            }
            for c in matrix.columns
        ],
        "rows": [
            {
                "file_id": str(f.get("file_id") or ""),
                "file_name": file_meta.get(str(f.get("file_id") or ""), ("", True, 1))[0],
                "missing": file_meta.get(str(f.get("file_id") or ""), ("", True, 1))[1],
                "file_version": file_meta.get(str(f.get("file_id") or ""), ("", True, 1))[2],
            }
            for f in matrix.files
        ],
        "cells": {f"{c.file_id}:{c.column_id}": visible_cell(c) for c in all_cells},
        "runs": runs_payload,
        "run": run_compat,  # Deprecated: FE reads runs[] from M3.
    }
