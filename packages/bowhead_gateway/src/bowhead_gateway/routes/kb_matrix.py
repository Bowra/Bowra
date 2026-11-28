"""文档矩阵端点族（kb-matrix M2 tech design §7）。

所有端点：`require_feature_licensed("kb")`
（读写都挂，S4 教训）+ `_require_workspace_member_strict`（**无 legacy fallback**，
kb_records docstring 的 M2 警示落地）；mutation 另挂 `require_not_readonly`
（**cancel 除外**——冻结 team 止损动作，归 ALLOW_ROUTES，review P1）+
`_require_member_rank`；matrix_id 反查 → `matrix.workspace_id != ctx.workspace_id
→ 404`（kb_admin get_extract_rows_status 枚举防护范式）。

owner-or-admin 是显式 helper 不是散写（review P1）：member-rank 对「删矩阵/删列/
取消他人 run」是必要非充分，漏掉 per-handler 判定 = 任意 member 越权。

Only-me matrices (document matrix step 1, S8): a private matrix answers every route like a missing one
unless the caller created it, whatever their role (``_get_matrix_checked`` and ``cancel_run``). It may
hold its creator's personal files; the per-file and batched file checks accept them only there.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

import aiosqlite
import anyio
from bowhead_core.access_control import workspace_role_satisfies
from bowhead_core.kb import matrix_usage
from bowhead_core.kb.doc_type_store import MAX_FIELDS_PER_SCHEMA, is_column_group_template
from bowhead_core.kb.graduation import (
    GRADUATION_EXTRACTOR_MODEL,
    GraduationError,
    build_graduation_entries,
    build_graduation_fields,
    compose_description,
    validate_graduation_columns,
)
from bowhead_core.kb.knowledge_base_access import named_knowledge_base_id
from bowhead_core.kb.matrix_engine import (
    ETA_SECONDS_PER_CELL,
    MAX_SCOPE_FILE_IDS,
    MatrixInsufficientCredits,
    MatrixQuotaExceeded,
    MatrixRateLimited,
    MatrixRunConflict,
    MatrixRunError,
    cell_concurrency,
    global_concurrency,
)
from bowhead_core.kb.matrix_store import (
    CELL_EVENT_LIMIT,
    MATRIX_COLUMN_TYPES,
    MatrixColumnBusyError,
    MatrixColumnNameError,
    MatrixFileLimitError,
    MatrixVisibilityError,
    cell_is_stale,
    is_raw_string_sentinel,
    new_column_id,
    private_matrices_enabled,
    validate_options,
)
from bowhead_core.kb.permission import KbPermission, ScopeContext
from bowhead_core.kb.telemetry import emit_audit
from bowhead_core.secret_redaction import extract_secrets, wrap_untrusted
from bowhead_core.space_governance import SpaceOperation
from bowhead_router.usage import usage_context
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_feature_licensed, require_not_readonly
from bowhead_gateway.access_context import access_context_to_dict, build_access_context_from_request, has_workspace_access_source
from bowhead_gateway.kb_authorization import kb_file_decisions, knowledge_levels_for
from bowhead_gateway.routes.auth import _current_user

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bowhead_core.access_control import AccessContext
    from bowhead_core.kb.doc_type_store import KbDocTypeStore
    from bowhead_core.kb.extracted_record_store import KbExtractedRecordStore
    from bowhead_core.kb.files_store import KbFilesStore
    from bowhead_core.kb.matrix_engine import KbMatrixEngine
    from bowhead_core.kb.matrix_store import KbMatrixStore, MatrixCellDTO, MatrixCellEventDTO, MatrixDTO, MatrixRunDTO
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
    if isinstance(exc, MatrixColumnNameError):
        return _matrix_error(422, exc.code, **exc.payload)
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


def _env_cap(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def _max_matrix_columns() -> int:
    return _env_cap("BOWHEAD_KB_MATRIX_MAX_COLUMNS", 20)


def _max_matrix_files() -> int:
    return _env_cap("BOWHEAD_KB_MATRIX_MAX_FILES", 500)


def _check_matrix_caps(*, columns: tuple[int, int] | None = None, files: tuple[int, int] | None = None) -> None:
    """Size caps on writes that grow a matrix; each pair is (count now, count after the write).

    A write is refused only when it grows the count past the cap, so matrices already larger
    than the cap stay readable, runnable and shrinkable. ``count`` is 0 for a new matrix.
    """
    for code, pair, limit in (
        ("matrix_column_limit", columns, _max_matrix_columns()),
        ("matrix_file_limit", files, _max_matrix_files()),
    ):
        if pair is None:
            continue
        current, requested = pair
        if requested > limit and requested > current:
            raise _matrix_error(422, code, limit=limit, count=current, requested=requested)


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
    # S8: who can see the matrix. Omitted or null = Space members; ignored (and reported) while the switch is off.
    visibility: Literal["space", "private"] | None = None


class _FromTemplateIn(BaseModel):
    workspace_id: str
    doc_type_id: str


class _GraduateIn(BaseModel):
    # M4 FR-40/41: snapshot = graduate as a template (fields + records, queryable in chat); template = save fields only.
    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    mode: Literal["snapshot", "template"] = "snapshot"


class _MatrixPatchIn(BaseModel):
    # Emptiness is checked after trimming in the route, so "" and "   " get the same localizable code.
    name: str | None = Field(None, max_length=_MAX_NAME_LEN)
    # S8: the creator changes who can see the matrix (switch on only).
    visibility: Literal["space", "private"] | None = None


class _FilesReplaceIn(BaseModel):
    file_ids: list[str]


# Rows added or removed per request (document matrix step 1, S2).
_MAX_FILES_PER_CHANGE = 100


class _FilesChangeIn(BaseModel):
    file_ids: list[str]


class _ColumnPatchIn(BaseModel):
    navigation_role: Literal["none", "description", "topics"] | None = None
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
    # Step 1 §3.1: failed reruns failed cells only; single_cell prices or runs one cell (file_id + column_id).
    kind: Literal["all_pending", "failed", "column", "force_all", "force_column", "stale", "stale_column", "single_cell"]
    column_id: str | None = None
    file_id: str | None = None  # single_cell only.
    file_ids: list[str] | None = None  # Trial or selection: at most MAX_SCOPE_FILE_IDS rows of this matrix.
    keep_verified: bool | None = None  # Force scopes keep verified cells unless this is false (omitted or null = true).


class _CellPatchIn(BaseModel):
    # revert_edited: M3 clears the edit flag (FR-9 r5); set_verified accepts false from M3 (FR-12 r5).
    op: Literal["edit_value", "keep_as_text", "set_verified", "revert_edited"]
    value: Any = None
    verified: bool | None = None


class _CellsVerifyIn(BaseModel):
    # Step 1 §4.2 bulk verify: one column, some rows, or both; neither means every cell of the matrix.
    column_id: str | None = None
    file_ids: list[str] | None = None
    # Required; checked in the route so a missing value gets the same code as the single-cell action.
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


def _is_owner_or_admin(owner_user_id: str, ctx: AccessContext) -> bool:
    return owner_user_id == ctx.user_id or ctx.is_team_admin or workspace_role_satisfies(ctx.workspace_role, "ws_admin")


def _require_owner_or_admin(owner_user_id: str, ctx: AccessContext) -> None:
    """删矩阵/删列/取消他人 run（FR-34）：member-rank 必要非充分（review P1）。"""
    if _is_owner_or_admin(owner_user_id, ctx):
        return
    raise _matrix_error(403, "owner_or_admin_required")


def _visible_to(matrix: MatrixDTO, ctx: AccessContext) -> bool:
    """Whether the caller may see this matrix at all: a private matrix only by its creator (S8). Roles grant
    nothing here, as for personal files (``KbPermission.can_read``)."""
    return matrix.visibility != "private" or matrix.created_by == ctx.user_id


def _owner_personal(matrix: MatrixDTO, ctx: AccessContext) -> bool:
    """Whether file checks accept the caller's own personal files: only in their own private matrix (S8)."""
    return matrix.visibility == "private" and matrix.created_by == ctx.user_id


async def _get_matrix_checked(request: Request, ctx: AccessContext, matrix_id: str) -> MatrixDTO:
    """matrix_id 反查 + 跨空间 404（枚举防护——不 403 不暴露存在性）。

    A private matrix (S8) gets the same 404 unless the caller created it, whatever their role, with the
    switch on or off.
    """
    m = await _get_matrix_store(request).get_matrix(matrix_id)
    if m is None or m.workspace_id != ctx.workspace_id or m.team_id != ctx.team_id or not _visible_to(m, ctx):
        raise _matrix_error(404, "matrix_not_found")
    return m


async def _validate_file_refs(request: Request, ctx: AccessContext, file_ids: list[str], *, personal_owner: bool = False) -> list[KbFileDTO]:
    """FR-32/FR-35 资源逐个校验（建矩阵/改文件集共用）：任一不存在 → 404 整单拒；
    personal → 422；跨空间/跨 team 探测统一 404 不 403（枚举防护）。

    ``personal_owner`` (S8, private matrices only): the caller's own personal files pass the same scope and
    grant checks as other files instead of the 422. Someone else's personal file is always a 404.
    Graduation keeps the strict mode.
    """
    files_store = _get_files_store(request)
    out: list[KbFileDTO] = []
    # The caller's knowledge base levels, resolved once and only for files in named knowledge bases.
    levels: list[Any] = []
    for file_id in dict.fromkeys(file_ids):
        f = await files_store.get(file_id)
        if f is None or f.soft_deleted_at or f.team_id != ctx.team_id:
            raise _matrix_error(404, "matrix_source_file_unavailable")
        if f.scope == "personal" and KbPermission.owner_of(f) != ctx.user_id:
            raise _matrix_error(404, "matrix_source_file_unavailable")
        if f.scope == "personal" and not personal_owner:
            raise _matrix_error(422, "personal_files_not_allowed")
        if not levels and named_knowledge_base_id(f):
            levels.append(
                await knowledge_levels_for(
                    ctx,
                    files_store=files_store,
                    user_store=getattr(request.app.state, "user_store", None),
                    access_store=getattr(request.app.state, "access_control_store", None),
                )
            )
        knowledge_levels = levels[0] if levels else None
        if f.scope == "personal":
            pass  # The caller's own (checked above); the scope check below applies the owner rule and grants.
        elif f.scope in ("space", "workspace"):
            if f.workspace_id != ctx.workspace_id:
                raise _matrix_error(404, "matrix_source_file_unavailable")
        elif f.scope == "team":
            scope_ctx = ScopeContext(
                user_id=ctx.user_id, workspace_id=ctx.workspace_id, team_id=ctx.team_id, role="user", knowledge_levels=knowledge_levels
            )
            if f.team_id != ctx.team_id or not KbPermission.can_read(f, scope_ctx):
                raise _matrix_error(404, "matrix_source_file_unavailable")
        else:
            raise _matrix_error(404, "matrix_source_file_unavailable")
        from bowhead_gateway.routes.kb import _kb_scope_check

        scope_ctx = ScopeContext(user_id=ctx.user_id, workspace_id=ctx.workspace_id, team_id=ctx.team_id, knowledge_levels=knowledge_levels)
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


def _source_in_matrix_context(file: KbFileDTO, ctx: AccessContext, *, personal_owner: bool = False) -> bool:
    """The structural part of ``_validate_file_refs``: an active file of the caller's Team that is a Space
    file of the current Space or a Team file. Personal files qualify only with ``personal_owner`` (the
    caller's own private matrix, S8) and only the caller's own."""
    if file.soft_deleted_at or file.team_id != ctx.team_id:
        return False
    if file.scope in ("space", "workspace"):
        return file.workspace_id == ctx.workspace_id
    if file.scope == "personal":
        return personal_owner and KbPermission.owner_of(file) == ctx.user_id
    return file.scope == "team"


async def _readable_sources(request: Request, ctx: AccessContext, file_ids: list[str], *, personal_owner: bool = False) -> dict[str, KbFileDTO]:
    """The files of these rows the caller may read now, by file ID: the batched form of ``_validate_file_refs``.

    A file is returned exactly when ``_validate_file_refs`` would accept it: active, in the caller's Team, not
    personal (or, with ``personal_owner``, the caller's own personal file), a Space file of the current Space
    or a Team file, readable under the scope rule and the
    caller's knowledge base levels, and allowed by its file and scope grants when grants are enforced. The
    whole call reads the files in pages of 256 IDs, resolves the levels at most once and asks for the grant
    decisions in one batch (``kb_file_decisions``, the check behind ``kb_file_authorizer(...).batch``) with
    the request's access context. Two differences, as in the engine's check: a hidden row writes no audit
    line, would-deny log or runtime-denied record (matrix pages poll every few seconds), and enforced grants
    without an access-control store (never the case in a deployment) refuse every file.
    """
    files_store = _get_files_store(request)
    ids = list(dict.fromkeys(file_id for file_id in file_ids if file_id))
    found: dict[str, KbFileDTO] = {}
    for offset in range(0, len(ids), 256):
        found.update({f.file_id: f for f in await files_store.get_many(ids[offset : offset + 256])})
    candidates = [f for f in found.values() if _source_in_matrix_context(f, ctx, personal_owner=personal_owner)]
    if not candidates:
        return {}
    decisions = await kb_file_decisions(
        candidates,
        ctx,
        getattr(request.app.state, "access_control_store", None),
        files_store=files_store,
        user_store=getattr(request.app.state, "user_store", None),
    )
    return {f.file_id: f for f in candidates if decisions.get(f.file_id)}


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
    # Space matrices and the caller's own private ones (S8); admins get no exception.
    matrices = await store.list_matrices(ctx.workspace_id, viewer_id=ctx.user_id)
    creator_names = await _member_labels(request, ctx.team_id, [m.created_by for m in matrices])
    items: list[dict[str, Any]] = []
    for m in matrices:
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
                "created_by_name": creator_names.get(m.created_by),
                "created_at": m.created_at,
                "visibility": m.visibility,
            }
        )
    return {"items": items, "workspace_id": ctx.workspace_id}


async def _member_labels(request: Request, team_id: str, user_ids: list[str]) -> dict[str, str]:
    """Display names of this Team's members (the list shows a name, never a user id).

    A name is display information: when it cannot be resolved the field stays empty and the
    list still loads.
    """
    lookup = getattr(getattr(request.app.state, "user_store", None), "get_team_member_labels", None)
    ids = list(dict.fromkeys(uid for uid in user_ids if uid))
    if not callable(lookup) or not ids:
        return {}
    try:
        return dict(await lookup(team_id, ids))
    except Exception:
        logger.warning("kb_matrix.creator_names_failed team_id=%s", team_id, exc_info=True)
        return {}


@router.post("/matrices", dependencies=[Depends(require_not_readonly)])
async def create_matrix(
    body: _MatrixCreateIn,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    ws = _ws_from_request(request, body.workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    # Before the per-file checks, so an oversized request is refused cheaply.
    _check_matrix_caps(columns=(0, len(body.columns)), files=(0, len(dict.fromkeys(body.file_ids))))
    # S8: only-me matrices exist while the switch is on; otherwise the request field is ignored and reported.
    requested_visibility = body.visibility or "space"
    visibility = requested_visibility if private_matrices_enabled() else "space"
    files = await _validate_file_refs(request, ctx, body.file_ids, personal_owner=visibility == "private")
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
            visibility=visibility,
        )
    except ValueError as e:
        raise _column_validation_error(e) from e
    emit_audit(
        "kb.matrix_created",
        {"matrix_id": m.matrix_id, "files_count": len(entries), "columns_count": len(m.columns), "visibility": m.visibility},
    )
    await matrix_usage.track_matrix_created(getattr(request.app.state, "metrics_log", None), ctx, matrix_id=m.matrix_id)
    out: dict[str, Any] = {"matrix_id": m.matrix_id, "columns": m.columns, "visibility": m.visibility}
    if requested_visibility != visibility:
        # The matrix was created visible to Space members: only-me matrices are not enabled here.
        out["ignored_fields"] = ["visibility"]
    return out


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
    # files the caller cannot read (the shared KB read check, resource grants included) or from personal files, which a
    # matrix never holds; otherwise a single from_template exposes restricted files' extracted values + names to all space members.
    # Also calculate actual char_count (B-P2-5: zero systematically deflates estimates, distorting the FR-14 hard gate).
    files_store = _get_files_store(request)
    record_file_ids = list(dict.fromkeys(rec.file_id for rec in records))
    candidates: dict[str, KbFileDTO] = {}
    for offset in range(0, len(record_file_ids), 256):
        candidates.update({f.file_id: f for f in await files_store.get_many(record_file_ids[offset : offset + 256])})
    decisions = await kb_file_decisions(
        list(candidates.values()),
        ctx,
        getattr(request.app.state, "access_control_store", None),
        files_store=files_store,
        user_store=getattr(request.app.state, "user_store", None),
    )
    allowed_files: dict[str, KbFileDTO] = {
        file_id: candidates[file_id]
        for file_id in record_file_ids
        if file_id in candidates and decisions.get(file_id) and candidates[file_id].scope != "personal"
    }
    skipped_files = len(record_file_ids) - len(allowed_files)
    file_versions = {file_id: int(getattr(dto, "version", 1) or 1) for file_id, dto in allowed_files.items()}
    file_ids = list(allowed_files.keys())
    entries = await _files_with_char_counts(request, user, list(allowed_files.values()))

    store = _get_matrix_store(request)
    try:
        m = await store.create_matrix(
            name=f"{dt.name}（矩阵）",
            workspace_id=ctx.workspace_id,
            team_id=ctx.team_id,
            created_by=user.id,
            files=entries,
            columns=columns,
            template_doc_type_id=body.doc_type_id,
        )
    except ValueError as e:
        raise _column_validation_error(e) from e

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
    await matrix_usage.track_matrix_created(getattr(request.app.state, "metrics_log", None), ctx, matrix_id=m.matrix_id)
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
    if matrix.visibility == "private":
        # A snapshot is queryable by the whole Space, so personal files stay out (S8): name the caller's own.
        own_personal, _hidden = await _personal_rows(request, ctx, matrix_file_ids)
        if own_personal:
            raise _matrix_error(422, "matrix_graduation_personal_files", **_named_files(own_personal))
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


_NAMED_FILES_LIMIT = 20


def _named_files(files: list[KbFileDTO]) -> dict[str, Any]:
    """An error payload naming the caller's own files: the first ``_NAMED_FILES_LIMIT`` and the count."""
    return {"files": [{"file_id": f.file_id, "name": f.filename} for f in files[:_NAMED_FILES_LIMIT]], "count": len(files)}


async def _personal_rows(request: Request, ctx: AccessContext, file_ids: list[str]) -> tuple[list[KbFileDTO], int]:
    """The rows that are personal files: the caller's own (in row order, deleted ones included) and how many
    belong to someone else (counted, never named)."""
    files_store = _get_files_store(request)
    ids = list(dict.fromkeys(file_id for file_id in file_ids if file_id))
    found: dict[str, KbFileDTO] = {}
    for offset in range(0, len(ids), 256):
        found.update({f.file_id: f for f in await files_store.get_many(ids[offset : offset + 256])})
    own: list[KbFileDTO] = []
    others = 0
    for file_id in ids:
        f = found.get(file_id)
        if f is None or f.scope != "personal":
            continue
        if KbPermission.owner_of(f) == ctx.user_id and f.team_id == ctx.team_id:
            own.append(f)
        else:
            others += 1
    return own, others


async def _change_visibility(request: Request, ctx: AccessContext, matrix: MatrixDTO, visibility: str) -> MatrixDTO:
    """Change who can see the matrix (S8): the creator only, while the switch is on.

    - To ``space``: refused while the matrix holds personal files; the error names the caller's own (other
      people's are only counted) and says to remove them first.
    - To ``private``: refused while other members' runs are active (they could no longer see or cancel them)
      or while a column has a navigation role. Other members' edits, verifications and runs stay in the
      matrix; they lose access to it, as everyone but the creator does.
    """
    if visibility == matrix.visibility:
        return matrix
    if not private_matrices_enabled():
        raise _matrix_error(422, "matrix_visibility_unavailable")
    if matrix.created_by != ctx.user_id:
        raise _matrix_error(403, "matrix_creator_required")
    store = _get_matrix_store(request)

    async def check(current: MatrixDTO) -> None:
        if visibility == "space":
            own, hidden = await _personal_rows(request, ctx, [str(f.get("file_id") or "") for f in current.files])
            if own or hidden:
                raise MatrixVisibilityError("matrix_personal_files_block_space", **_named_files(own), hidden=hidden)
            return
        others = [r for r in await store.list_active_runs_for_matrix(current.matrix_id) if r.initiated_by != current.created_by]
        if others:
            raise MatrixVisibilityError("matrix_visibility_runs_active", count=len(others))

    try:
        changed = await store.set_visibility(matrix.matrix_id, visibility, workspace_id=ctx.workspace_id, check=check)
    except MatrixVisibilityError as e:
        status = 409 if e.code == "matrix_visibility_runs_active" else 422
        raise _matrix_error(status, e.code, **e.payload) from e
    if changed is None:
        raise _matrix_error(404, "matrix_not_found")
    updated, did_change = changed
    if did_change:
        emit_audit("kb.matrix_visibility_changed", {"matrix_id": matrix.matrix_id, "visibility": visibility})
    return updated


@router.patch("/matrices/{matrix_id}", dependencies=[Depends(require_not_readonly)])
async def update_matrix(
    matrix_id: str,
    body: _MatrixPatchIn,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """Rename the matrix (member rank) and/or change who can see it (its creator, S8)."""
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    name = body.name.strip() if body.name is not None else None
    if body.visibility is None and not name:
        # A missing name answers as an empty one, as it did before the visibility field existed.
        raise _matrix_error(422, "matrix_name_invalid")
    if body.name is not None and not name:
        raise _matrix_error(422, "matrix_name_invalid")
    if body.visibility is not None:
        matrix = await _change_visibility(request, ctx, matrix, body.visibility)
    if name is not None:
        await _get_matrix_store(request).rename_matrix(matrix.matrix_id, name, workspace_id=ctx.workspace_id)
        emit_audit("kb.matrix_renamed", {"matrix_id": matrix_id})
    out: dict[str, Any] = {"matrix_id": matrix_id, "name": name if name is not None else matrix.name}
    if body.visibility is not None:
        out["visibility"] = matrix.visibility
    return out


@router.get("/matrices/{matrix_id}/visibility")
async def get_matrix_visibility(
    matrix_id: str,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """What changing who can see the matrix would do (S8), for the creator's confirmation.

    ``contributors`` names the other people who changed a cell or started a run here (they lose access when
    the matrix becomes private; their edits and verifications stay). ``personal_files`` names the caller's
    personal files that keep it from becoming visible to the Space (``hidden_personal_files`` counts other
    people's), and ``active_runs_by_others`` the runs that keep it from becoming private.
    """
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws, operation=SpaceOperation.READ_HISTORY)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    if matrix.created_by != ctx.user_id:
        raise _matrix_error(403, "matrix_creator_required")
    store = _get_matrix_store(request)
    contributor_ids = [uid for uid in await store.list_contributors(matrix.matrix_id) if uid != matrix.created_by]
    names = await _member_labels(request, ctx.team_id, contributor_ids)
    own, hidden = await _personal_rows(request, ctx, [str(f.get("file_id") or "") for f in matrix.files])
    others_active = [r for r in await store.list_active_runs_for_matrix(matrix.matrix_id) if r.initiated_by != matrix.created_by]
    return {
        "matrix_id": matrix.matrix_id,
        "visibility": matrix.visibility,
        "can_change": private_matrices_enabled() and (ctx.is_team_admin or workspace_role_satisfies(ctx.workspace_role, "member")),
        # People whose names cannot be resolved (for example after account deletion) are counted, not named.
        "contributors": [names[uid] for uid in contributor_ids if names.get(uid)],
        "contributors_count": len(contributor_ids),
        "personal_files": _named_files(own)["files"],
        "personal_files_count": len(own),
        "hidden_personal_files": hidden,
        "active_runs_by_others": len(others_active),
    }


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
    store = _get_matrix_store(request)
    # Stop spending first: every active run goes through the normal cancel CAS
    # (running|paused → cancelling); in-flight calls finish and their writes are discarded.
    cancelled = 0
    active = await store.list_active_runs_for_matrix(matrix.matrix_id)
    if active:
        engine = _get_engine(request)
        for run in active:
            if run.status in ("running", "paused") and await engine.cancel_run(run.run_id):
                cancelled += 1
    await store.soft_delete_matrix(matrix.matrix_id, workspace_id=ctx.workspace_id)
    emit_audit("kb.matrix_deleted", {"matrix_id": matrix_id, "runs_cancelled": cancelled})
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
    _check_matrix_caps(files=(len(matrix.files), len(dict.fromkeys(body.file_ids))))
    files = await _validate_file_refs(request, ctx, body.file_ids, personal_owner=_owner_personal(matrix, ctx))
    entries = await _files_with_char_counts(request, user, files)
    # Removed files' queued cells leave active runs in the same transaction (store). Kept for API
    # compatibility (the UI adds and removes named rows); dropping rows with edited or verified
    # cells follows the same creator-or-admin rule as POST …/files/remove.
    try:
        change = await _get_matrix_store(request).replace_files(
            matrix.matrix_id,
            entries,
            workspace_id=ctx.workspace_id,
            allow_reviewed=_is_owner_or_admin(matrix.created_by, ctx),
            require_private=any(f.scope == "personal" for f in files),
        )
    except MatrixVisibilityError as e:
        raise _matrix_error(409, e.code, **e.payload) from e
    if change is not None and change.refused:
        raise _matrix_error(403, "matrix_remove_requires_owner", edited=change.edited, verified=change.verified)
    emit_audit("kb.matrix_files_changed", {"matrix_id": matrix_id, "files_count": len(entries)})
    return {"matrix_id": matrix_id, "files_count": len(entries)}


def _change_file_ids(body: _FilesChangeIn) -> list[str]:
    """The named rows of an add or remove, de-duplicated in request order and capped per request."""
    file_ids = list(dict.fromkeys(fid for fid in body.file_ids if fid))
    if len(file_ids) > _MAX_FILES_PER_CHANGE:
        raise _matrix_error(422, "matrix_files_batch_limit", limit=_MAX_FILES_PER_CHANGE, requested=len(file_ids))
    return file_ids


@router.post("/matrices/{matrix_id}/files", dependencies=[Depends(require_not_readonly)])
async def add_matrix_files(
    matrix_id: str,
    body: _FilesChangeIn,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """Append rows (document matrix step 1, S2): only the named files are touched.

    Files already in the matrix are skipped; the others go through the same per-file checks as
    creation, and only they get a character count (one chunk read each).
    """
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    requested = _change_file_ids(body)
    present = {str(f.get("file_id") or "") for f in matrix.files}
    new_ids = [fid for fid in requested if fid not in present]
    store = _get_matrix_store(request)
    if new_ids:
        # Before the per-file checks, so an oversized request is refused cheaply; the store checks again under its lock.
        _check_matrix_caps(files=(len(matrix.files), len(matrix.files) + len(new_ids)))
        files = await _validate_file_refs(request, ctx, new_ids, personal_owner=_owner_personal(matrix, ctx))
        entries = await _files_with_char_counts(request, user, files)
    else:
        files, entries = [], []
    try:
        change = await store.add_files(
            matrix.matrix_id,
            entries,
            workspace_id=ctx.workspace_id,
            max_files=_max_matrix_files(),
            require_private=any(f.scope == "personal" for f in files),
        )
    except MatrixFileLimitError as e:
        raise _matrix_error(422, e.code, **e.payload) from e
    except MatrixVisibilityError as e:
        # The matrix stopped being private while the request held personal files.
        raise _matrix_error(409, e.code, **e.payload) from e
    if change is None:
        raise _matrix_error(404, "matrix_not_found")
    if change.changed:
        emit_audit("kb.matrix_files_added", {"matrix_id": matrix_id, "added": len(change.changed), "files_count": change.files_count})
    return {
        "matrix_id": matrix_id,
        "added": change.changed,
        "skipped": len(requested) - len(change.changed),
        "files_count": change.files_count,
    }


@router.post("/matrices/{matrix_id}/files/remove", dependencies=[Depends(require_not_readonly)])
async def remove_matrix_files(
    matrix_id: str,
    body: _FilesChangeIn,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """Remove the named rows only (S2), so rows hidden from the caller are never dropped (spike bug D).

    Members may remove rows; rows holding edited or verified cells need the matrix creator or a
    Space/Team admin. No cell records who edited or verified it yet, so every such cell counts.
    """
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    requested = _change_file_ids(body)
    change = await _get_matrix_store(request).remove_files(
        matrix.matrix_id,
        requested,
        workspace_id=ctx.workspace_id,
        allow_reviewed=_is_owner_or_admin(matrix.created_by, ctx),
    )
    if change is None:
        raise _matrix_error(404, "matrix_not_found")
    if change.refused:
        raise _matrix_error(403, "matrix_remove_requires_owner", edited=change.edited, verified=change.verified)
    if change.changed:
        emit_audit(
            "kb.matrix_files_removed",
            {
                "matrix_id": matrix_id,
                "removed": len(change.changed),
                "files_count": change.files_count,
                "edited": change.edited,
                "verified": change.verified,
            },
        )
    return {
        "matrix_id": matrix_id,
        "removed": change.changed,
        "files_count": change.files_count,
        "edited": change.edited,
        "verified": change.verified,
    }


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
    _check_matrix_caps(columns=(len(matrix.columns), len(matrix.columns) + 1))
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
    if body.navigation_role not in (None, "none") and matrix.visibility == "private":
        # Navigation roles publish cells as Space-wide source descriptions (S8); the store checks again under its lock.
        raise _matrix_error(422, "matrix_navigation_role_private")
    store = _get_matrix_store(request)
    if body.type is not None:
        # Refine promotion as the sole change (FR-5 r5): M3 allows {type, options} together
        # (select/multi_select promotion needs options), still not alongside name/question/hint/
        # cross_verify changes (impl-diff review P3-2: promote silently drops cross_verify sent together;
        # return explicit 422 instead of silently dropping it; change cv via a separate PATCH).
        if (
            body.name is not None
            or body.question is not None
            or body.extraction_hint is not None
            or body.cross_verify is not None
            or body.navigation_role is not None
        ):
            raise _matrix_error(422, "promote_must_be_sole_change")
        try:
            promoted = await store.promote_column(matrix.matrix_id, column_id, body.type, workspace_id=ctx.workspace_id, options=body.options)
        except MatrixColumnBusyError as e:
            raise _matrix_error(409, e.code, **e.payload) from e
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
            navigation_role=body.navigation_role,
        )
    except MatrixVisibilityError as e:
        raise _matrix_error(422, e.code, **e.payload) from e
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


def _run_scope(body: _RunScopeIn, matrix: MatrixDTO) -> dict[str, Any]:
    """Normalize a run request into the engine scope (step 1 §3.1), rejecting row subsets the engine cannot honor.

    - ``single_cell`` keeps only its cell; ``file_ids`` and ``keep_verified`` do not apply to it.
    - Force scopes store ``keep_verified`` (true unless explicitly false).
    - ``file_ids`` is deduplicated in order and must name 1–MAX_SCOPE_FILE_IDS rows of this matrix.
    """
    scope: dict[str, Any] = {"kind": body.kind}
    if body.column_id is not None:
        scope["column_id"] = body.column_id
    if body.kind == "single_cell":
        if body.file_id is not None:
            scope["file_id"] = body.file_id
        return scope
    if body.kind in ("force_all", "force_column"):
        scope["keep_verified"] = body.keep_verified is not False
    if body.file_ids is not None:
        file_ids = list(dict.fromkeys(body.file_ids))
        if not file_ids:
            raise _matrix_error(422, "matrix_run_files_empty")
        if len(file_ids) > MAX_SCOPE_FILE_IDS:
            raise _matrix_error(422, "matrix_run_files_limit", limit=MAX_SCOPE_FILE_IDS, count=len(file_ids))
        rows = {str(f.get("file_id") or "") for f in matrix.files}
        unknown = sum(1 for file_id in file_ids if file_id not in rows)
        if unknown:
            raise _matrix_error(422, "matrix_run_files_not_in_matrix", count=unknown)
        scope["file_ids"] = file_ids
    return scope


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
    scope = _run_scope(body, matrix)
    try:
        return await _get_engine(request).estimate_run(matrix, scope, user_id=user.id)
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
    scope = _run_scope(body, matrix)
    try:
        run = await _get_engine(request).start_run(matrix, scope, initiated_by=user.id)
    except MatrixRunError as e:
        raise _run_error_to_http(e) from e
    except ValueError as e:
        raise _matrix_error(422, "invalid_matrix_run_scope") from e
    trial = bool(scope.get("file_ids"))  # A run limited to a few rows, as the snapshot's ``trial``.
    await matrix_usage.track_matrix_run_started(
        getattr(request.app.state, "metrics_log", None), ctx, matrix_id=matrix.matrix_id, scope_kind=body.kind, cells=run.cells_total, trial=trial
    )
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
    动作，gating 它 = 冻结 team 无法停止消耗（readonly-gate「remediation」类目同理由）。

    Runs of a soft-deleted matrix stay cancellable (same Space and Team checks), so a
    cancel that races a matrix delete converges instead of answering 404."""
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(
        request,
        user,
        ws,
        operation=SpaceOperation.EXECUTE,
    )
    store = _get_matrix_store(request)
    matrix = await store.get_matrix(matrix_id, include_deleted=True)
    if matrix is None or matrix.workspace_id != ctx.workspace_id or matrix.team_id != ctx.team_id or not _visible_to(matrix, ctx):
        raise _matrix_error(404, "matrix_not_found")
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
    await matrix_usage.track_matrix_run_started(
        getattr(request.app.state, "metrics_log", None), ctx, matrix_id=matrix.matrix_id, scope_kind="single_cell", cells=run.cells_total, trial=False
    )
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
    await _validate_file_refs(request, ctx, [file_id], personal_owner=_owner_personal(matrix, ctx))
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
            actor_user_id=user.id,
        )
        if cell is None:
            raise _matrix_error(404, "matrix_cell_not_found")
        emit_audit("kb.matrix_cell_edited", {"matrix_id": matrix_id, "file_id": file_id, "column_id": column_id})
    elif body.op == "keep_as_text":
        cell = await store.keep_cell_as_text(matrix.matrix_id, file_id, column_id, workspace_id=ctx.workspace_id, actor_user_id=user.id)
        if cell is None:
            raise _matrix_error(404, "matrix_cell_not_found")
        emit_audit("kb.matrix_cell_kept_as_text", {"matrix_id": matrix_id, "file_id": file_id, "column_id": column_id})
    elif body.op == "revert_edited":
        # M3 clears the edit flag (FR-9 r5): clear edited + cache_key/cached; retain the manual value as the current value,
        # now overwritable by batches (no superseded preservation). Unedited cells → 422 (helps FE catch stale UI).
        try:
            cell = await store.revert_cell_edited(matrix.matrix_id, file_id, column_id, workspace_id=ctx.workspace_id, actor_user_id=user.id)
        except ValueError as e:
            raise _matrix_error(422, "not_edited") from e
        if cell is None:
            raise _matrix_error(404, "matrix_cell_not_found")
        emit_audit("kb.matrix_cell_edit_reverted", {"matrix_id": matrix_id, "file_id": file_id, "column_id": column_id})
    else:  # set_verified (M3 accepts false to undo verification, FR-12 r5).
        if body.verified is None:
            raise _matrix_error(422, "verified_required")
        try:
            cell, changed = await store.set_cell_verified(
                matrix.matrix_id, file_id, column_id, workspace_id=ctx.workspace_id, verified=body.verified, actor_user_id=user.id
            )
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
            if body.verified:
                await matrix_usage.track_matrix_cells_verified(
                    getattr(request.app.state, "metrics_log", None), ctx, matrix_id=matrix.matrix_id, count=1, bulk=False
                )
    if cell is None:  # All three branches already return 404; defensive check (python -O strips asserts, S-P2-4).
        raise _matrix_error(404, "matrix_cell_not_found")
    latest = await store.list_cell_events(matrix.matrix_id, file_id, column_id, limit=1)
    names = await _member_labels(request, ctx.team_id, [e.actor_user_id or "" for e in latest])
    return {"cell": {**_cell_payload(cell, matrix), "last_event": _last_event_payload(latest[0], names) if latest else None}}


# ─── review (step 1 §4.2): bulk verify and cell history ─────


def _last_event_payload(event: MatrixCellEventDTO, names: dict[str, str]) -> dict[str, Any]:
    """What the side panel shows of a cell's latest change. ``actor_name`` is None for the AI and for people
    whose name cannot be resolved (for example after account deletion)."""
    return {"kind": event.kind, "actor_name": names.get(event.actor_user_id) if event.actor_user_id else None, "at": event.created_at}


async def _readable_rows(request: Request, ctx: AccessContext, matrix: MatrixDTO, file_ids: list[str]) -> dict[str, int]:
    """The named rows the caller can read now, with each file's current version (the snapshot's row check)."""
    readable = await _readable_sources(request, ctx, file_ids, personal_owner=_owner_personal(matrix, ctx))
    return {file_id: int(dto.version or 1) for file_id, dto in readable.items()}


@router.post("/matrices/{matrix_id}/cells/verify", dependencies=[Depends(require_not_readonly)])
async def verify_cells(
    matrix_id: str,
    body: _CellsVerifyIn,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """Mark many cells verified, or undo it: a column, named rows, or both (member rank).

    Verifying takes cells that are done and not stale, edited ones included; undoing takes every verified
    cell, as the single-cell actions do. Rows the caller cannot read are left untouched and counted as
    ``unavailable``; every changed cell gets one history event, all with the same ``batch_id``.
    """
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws)
    _require_member_rank(ctx)
    if body.verified is None:
        raise _matrix_error(422, "verified_required")
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    columns = [_column_or_404(matrix, body.column_id)] if body.column_id is not None else list(matrix.columns)
    rows = [str(f.get("file_id") or "") for f in matrix.files]
    if body.file_ids is not None:
        file_ids = list(dict.fromkeys(body.file_ids))
        if not file_ids:
            raise _matrix_error(422, "matrix_verify_files_empty")
        present = set(rows)
        unknown = sum(1 for file_id in file_ids if file_id not in present)
        if unknown:
            raise _matrix_error(422, "matrix_verify_files_not_in_matrix", count=unknown)
        rows = file_ids
    readable = await _readable_rows(request, ctx, matrix, rows)
    keys = [(file_id, str(c.get("column_id") or "")) for file_id in rows if file_id in readable for c in columns]
    outcome = await _get_matrix_store(request).verify_cells(
        matrix.matrix_id,
        keys,
        verified=body.verified,
        workspace_id=ctx.workspace_id,
        actor_user_id=user.id,
        column_versions={str(c.get("column_id") or ""): int(c.get("prompt_version") or 1) for c in columns},
        file_versions=readable,
    )
    skipped = {**outcome.skipped, "unavailable": (len(rows) - len(readable)) * len(columns)}
    emit_audit(
        "kb.matrix_cells_bulk_verified",
        {
            "matrix_id": matrix.matrix_id,
            "column_id": body.column_id,
            "verified": body.verified,
            "changed": len(outcome.changed),
            "batch_id": outcome.batch_id,
            **{f"skipped_{reason}": count for reason, count in skipped.items()},
        },
    )
    if body.verified:
        await matrix_usage.track_matrix_cells_verified(
            getattr(request.app.state, "metrics_log", None), ctx, matrix_id=matrix.matrix_id, count=len(outcome.changed), bulk=True
        )
    return {"changed": len(outcome.changed), "skipped": skipped, "batch_id": outcome.batch_id}


@router.get("/matrices/{matrix_id}/cells/{file_id}/{column_id}/events")
async def list_cell_events(
    matrix_id: str,
    file_id: str,
    column_id: str,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """A cell's history, newest first, at most ``CELL_EVENT_LIMIT`` events (``more`` says older ones exist).

    Any Space member who can read the row may read it; a row the caller cannot read answers like a missing one.
    """
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws, operation=SpaceOperation.READ_HISTORY)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    _column_or_404(matrix, column_id)
    _file_in_matrix_or_404(matrix, file_id)
    await _validate_file_refs(request, ctx, [file_id], personal_owner=_owner_personal(matrix, ctx))
    events = await _get_matrix_store(request).list_cell_events(matrix.matrix_id, file_id, column_id, limit=CELL_EVENT_LIMIT + 1)
    shown = events[:CELL_EVENT_LIMIT]
    names = await _member_labels(request, ctx.team_id, [e.actor_user_id or "" for e in shown])
    return {
        "items": [{**_last_event_payload(e, names), "event_id": e.event_id, "run_id": e.run_id, "batch_id": e.batch_id} for e in shown],
        "more": len(events) > CELL_EVENT_LIMIT,
    }


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
    file_ids = run.scope.get("file_ids")
    trial_files = len(file_ids) if isinstance(file_ids, list) and file_ids else None
    return {
        "run_id": run.run_id,
        "status": run.status,
        "scope": {"kind": run.scope.get("kind"), "column_id": run.scope.get("column_id")},
        # Step 1 §3.1: a run limited to a few rows is a trial; the banner says so and how many files it covers.
        "trial": trial_files is not None,
        "trial_files": trial_files,
        "cells_total": run.cells_total,
        "cells_done": run.cells_done,
        "cells_failed": run.cells_failed,
        "skipped_edited": run.skipped_edited,
        "error_code": run.error_code,
        "eta_s": math.ceil(ETA_SECONDS_PER_CELL * remaining / concurrency),
        "initiated_by": run.initiated_by,
        "can_cancel": can_cancel,
    }


# A cell in a row the caller cannot read: no value, source, history or write position, so it stays the
# same while the row stays hidden and a delta never needs to carry it.
_HIDDEN_CELL: dict[str, Any] = {
    "status": "failed",
    "variants": [],
    "fail_reason": "source_unavailable",
    "edited": False,
    "verified": False,
    "stale": False,
    "cached": False,
    "prompt_version": 0,
    "seq": 0,
}


@dataclass(frozen=True)
class _ViewerRows:
    """A matrix's rows as one caller may see them now, and how each cell is shown to that caller.

    ``readable`` holds the files of the rows the caller can read; every other row keeps its place with no
    name (``missing``) and hidden cells. ``sources`` is each readable row's display-only source state (only
    while ``FEATURE_KB_USE_GUARD`` is on).
    """

    matrix: MatrixDTO
    readable: dict[str, KbFileDTO]
    sources: dict[str, Any]

    @property
    def columns(self) -> list[dict[str, Any]]:
        return [
            {
                "column_id": c.get("column_id"),
                "name": c.get("name"),
                "type": c.get("type"),
                "question": c.get("question"),
                "extraction_hint": c.get("extraction_hint"),
                "prompt_version": c.get("prompt_version"),
                "options": c.get("options"),
                "cross_verify": bool(c.get("cross_verify")),
                "navigation_role": c.get("navigation_role", "none"),
            }
            for c in self.matrix.columns
        ]

    @property
    def rows(self) -> list[dict[str, Any]]:
        rows = []
        for f in self.matrix.files:
            file_id = str(f.get("file_id") or "")
            dto = self.readable.get(file_id)
            rows.append(
                {
                    "file_id": file_id,
                    "file_name": dto.filename if dto else "",
                    "missing": dto is None,
                    "file_version": int(dto.version or 1) if dto else 1,
                }
            )
        return rows

    def actor_ids(self, last_events: dict[tuple[str, str], MatrixCellEventDTO]) -> list[str]:
        """The people behind the latest change of cells in readable rows; only their names are resolved."""
        return list(dict.fromkeys(e.actor_user_id for (file_id, _col), e in last_events.items() if e.actor_user_id and file_id in self.readable))

    def cell(self, cell: MatrixCellDTO, last_events: dict[tuple[str, str], MatrixCellEventDTO], names: dict[str, str]) -> dict[str, Any]:
        dto = self.readable.get(cell.file_id)
        if dto is None:
            return {**_HIDDEN_CELL, "variants": []}
        latest = last_events.get((cell.file_id, cell.column_id))
        return {
            **_cell_payload(cell, self.matrix, int(dto.version or 1)),
            "current_source": self.sources.get(cell.file_id),
            # Step 1 §4.2: the side panel's latest change; hidden rows above never carry it.
            "last_event": _last_event_payload(latest, names) if latest else None,
        }

    def digest(
        self,
        header: dict[str, Any],
        cell_keys: Iterable[tuple[str, str]],
        last_events: dict[tuple[str, str], MatrixCellEventDTO],
        names: dict[str, str],
    ) -> str:
        """``projection_digest`` (step 1 §6): every part of this caller's full projection a delta cannot carry.

        A delta carries the cells written after ``since_seq`` and the runs. Everything else the full
        projection shows can change without a cell write, so it is hashed here: the header except ``seq``
        (name, structure, the caller's ``can_edit`` and ``can_manage``), the columns, each row's name,
        hidden state and file version (permission changes, removals, renames and new versions that change
        ``stale``), the rows' source states, the set of cell keys (removing a cell row moves no ``seq``) and
        the names of the people behind readable latest changes.
        """
        material = {
            "matrix": {key: value for key, value in header.items() if key != "seq"},
            "columns": self.columns,
            "rows": self.rows,
            "sources": self.sources,
            "cells": sorted(f"{file_id}:{column_id}" for file_id, column_id in cell_keys),
            "actors": sorted((actor, names.get(actor)) for actor in self.actor_ids(last_events)),
        }
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


async def _viewer_rows(request: Request, ctx: AccessContext, matrix: MatrixDTO) -> _ViewerRows:
    """Check every row's file against the caller's current permissions, in one batch (step 1 §6)."""
    from bowhead_core.kb.generations import use_guard_enabled
    from bowhead_core.kb.maintenance import cached_source_state

    readable = await _readable_sources(request, ctx, [str(f.get("file_id") or "") for f in matrix.files], personal_owner=_owner_personal(matrix, ctx))
    sources: dict[str, Any] = {}
    if use_guard_enabled():
        from bowhead_gateway.kb_maintenance import team_runtime

        try:
            runtime = await team_runtime(request.app, ctx.team_id)
            index_version = runtime.kb_store.index_version
        except Exception:
            index_version = None
        sources = {file_id: cached_source_state(dto, index_version=index_version) for file_id, dto in readable.items()}
    return _ViewerRows(matrix=matrix, readable=readable, sources=sources)


async def _viewer_projection(
    request: Request,
    ctx: AccessContext,
    matrix: MatrixDTO,
    *,
    header: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """The matrix as this caller may see it now: ``columns``, ``rows`` and ``cells`` of the snapshot.

    Every row's file is checked against the caller's current permissions; a row the caller cannot read
    keeps its place with no name (``missing=True``) and its cells carry no value, source or history. The
    snapshot and the Excel export both read this projection, so an export never shows more than the grid.
    The snapshot passes its ``header``: the optional source-navigation cache is warmed and the projection
    digest returned. The export passes none and gets None.
    """
    store = _get_matrix_store(request)
    # Cells are read before the row check, so the permissions applied are never older than the cells shown.
    all_cells = await store.list_cells(matrix.matrix_id)
    last_events = await store.last_cell_events(matrix.matrix_id)
    view = await _viewer_rows(request, ctx, matrix)
    if header is not None:
        from bowhead_gateway.kb_source_navigation import refresh_matrix_navigation

        await refresh_matrix_navigation(request.app, ctx, matrix, view.readable, all_cells)
    names = await _member_labels(request, ctx.team_id, view.actor_ids(last_events))
    projection = {
        "columns": view.columns,
        "rows": view.rows,
        "cells": {f"{c.file_id}:{c.column_id}": view.cell(c, last_events, names) for c in all_cells},
    }
    if header is None:
        return projection, None
    return projection, view.digest(header, [(c.file_id, c.column_id) for c in all_cells], last_events, names)


async def _viewer_delta(
    request: Request, ctx: AccessContext, matrix: MatrixDTO, header: dict[str, Any], since_seq: int, digest: str
) -> dict[str, Any] | None:
    """The cells written after ``since_seq`` in rows the caller can read, or None when ``digest`` is stale.

    The cell keys are read after the changed cells, so a cell removed before then already changes the digest.
    """
    store = _get_matrix_store(request)
    changed = await store.list_cells_since(matrix.matrix_id, since_seq)
    last_events = await store.last_cell_events(matrix.matrix_id)
    keys = await store.list_cell_keys(matrix.matrix_id)
    view = await _viewer_rows(request, ctx, matrix)
    names = await _member_labels(request, ctx.team_id, view.actor_ids(last_events))
    current = view.digest(header, keys, last_events, names)
    if current != digest:
        return None
    visible = [c for c in changed if c.file_id in view.readable]
    from bowhead_gateway.kb_source_navigation import refresh_matrix_navigation

    await refresh_matrix_navigation(request.app, ctx, matrix, view.readable, visible)
    return {
        "kind": "delta",
        "seq": header["seq"],
        "structure_seq": header["structure_seq"],
        "projection_digest": current,
        "cells": {f"{c.file_id}:{c.column_id}": view.cell(c, last_events, names) for c in visible},
    }


def _snapshot_header(matrix: MatrixDTO, ctx: AccessContext) -> dict[str, Any]:
    can_edit = ctx.is_team_admin or workspace_role_satisfies(ctx.workspace_role, "member")
    return {
        "matrix_id": matrix.matrix_id,
        "name": matrix.name,
        "can_edit": can_edit,  # e2e-plan P2-1 delivered: FE shadow role checks retired.
        # Creator or Space/Team admin: may delete the matrix and remove rows with edited or verified cells.
        "can_manage": can_edit and _is_owner_or_admin(matrix.created_by, ctx),
        "seq": matrix.last_seq,
        "structure_seq": matrix.structure_seq,
        "poll_ms": _poll_ms(),
        "template_doc_type_id": matrix.template_doc_type_id,
        # S8: who can see the matrix, and whether this caller (its creator, switch on) may change that.
        "visibility": matrix.visibility,
        "can_change_visibility": can_edit and private_matrices_enabled() and matrix.created_by == ctx.user_id,
    }


@router.get("/matrices/{matrix_id}/snapshot")
async def get_snapshot(
    matrix_id: str,
    request: Request,
    workspace_id: str = "",
    since_seq: int | None = None,
    digest: str | None = None,
    opened: bool = False,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """The matrix as the caller may see it now (``kind: "full"``), or only what changed (``kind: "delta"``).

    Every answer re-checks each row against the caller's current permissions. A full answer carries
    ``projection_digest``. A poll that sends ``since_seq`` with the digest of its last answer gets a delta
    while the digest still matches: the cells written after ``since_seq`` in rows the caller can read, the
    new ``seq`` and the runs. When anything else changed (see ``_ViewerRows.digest``), and for clients that
    send no digest, the answer is full.

    ``opened`` marks the page's first request (step 1 S9): it records one ``kb.matrix.opened``, and only
    on a request without ``since_seq``, so polls and later reloads never count.
    """
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
    header = _snapshot_header(matrix, ctx)

    if digest and since_seq is not None and matrix.structure_seq <= since_seq <= matrix.last_seq:
        delta = await _viewer_delta(request, ctx, matrix, header, since_seq, digest)
        if delta is not None:
            return {**delta, "runs": runs_payload, "run": run_compat}

    projection, projection_digest = await _viewer_projection(request, ctx, matrix, header=header)
    if opened and since_seq is None:
        await matrix_usage.track_matrix_opened(getattr(request.app.state, "metrics_log", None), ctx, matrix_id=matrix.matrix_id)
    return {
        "kind": "full",
        "matrix": header,
        **projection,
        "projection_digest": projection_digest,
        "runs": runs_payload,
        "run": run_compat,  # Deprecated: FE reads runs[] from M3.
    }


# ─── export (step 1 §5) ─────────────────────────────────────


@router.get("/matrices/{matrix_id}/export.xlsx")
async def export_matrix(
    matrix_id: str,
    request: Request,
    workspace_id: str = "",
    user: User = Depends(_current_user),
) -> Response:
    """The matrix as an Excel workbook (Answers, Sources, About) in the caller's interface language.

    A read: every Space role that may open the snapshot may export it. The workbook is built only from
    the snapshot's per-viewer projection, so rows the caller cannot read are exported without values.
    """
    ws = _ws_from_request(request, workspace_id)
    _ws, ctx = await _require_workspace_member_strict(request, user, ws, operation=SpaceOperation.READ_HISTORY)
    matrix = await _get_matrix_checked(request, ctx, matrix_id)
    projection, _digest = await _viewer_projection(request, ctx, matrix)

    from bowhead_core.kb.matrix_export import XLSX_MIME, build_matrix_workbook, export_filename

    from bowhead_gateway.error_handler import _resolve_ui_language

    language = await _resolve_ui_language(request)
    exported = await anyio.to_thread.run_sync(
        functools.partial(
            build_matrix_workbook,
            matrix_name=matrix.name,
            columns=projection["columns"],
            rows=projection["rows"],
            cells=projection["cells"],
            language=language,
            exporter_name=user.display_name or (user.email or "").partition("@")[0],
            exported_at=datetime.now(tz=UTC),
        )
    )
    emit_audit(
        "kb.matrix_exported",
        {
            "matrix_id": matrix.matrix_id,
            "files": exported.files,
            "columns": exported.columns,
            "hidden_cells": exported.hidden_cells,
            "language": language,
        },
    )
    await matrix_usage.track_matrix_exported(getattr(request.app.state, "metrics_log", None), ctx, matrix_id=matrix.matrix_id)
    headers = {
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(export_filename(matrix.name), safe=""),
        "Cache-Control": "no-store, private",
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=exported.content, media_type=XLSX_MIME, headers=headers)
