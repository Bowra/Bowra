"""KB API routes — V7 knowledge-base sprint.

Tech design v3.6 §4. 8 endpoints per design:
- POST   /api/v1/kb/files                       (multipart upload, P0-R1 scope_id 验)
- GET    /api/v1/kb/files                        (list per scope, can_read filter)
- GET    /api/v1/kb/files/{file_id}              (DTO)
- GET    /api/v1/kb/files/{file_id}/download     (binary stream, can_read)
- DELETE /api/v1/kb/files/{file_id}              (soft delete)
- PATCH  /api/v1/kb/files/{file_id}/scope        (P0-O2/P0-P2 8 routing key migration + P1-N4 audit)
- POST   /api/v1/kb/search                       (4-stage retriever pipeline)
- GET    /api/v1/kb/files/{file_id}/chunks       (chunk list)

V1 simplifications:
- ScopeContext.role 走 BOWHEAD_AUTO_ADMIN_DOMAINS env (跟现 chat.py 同模式)
- get_kb_workspace_id / get_kb_team_id 走 user.team_id (real-auth ship 后切 ws_member 表)
- artifact_store 走现 artifact_store wrap

V1 backend wiring TODO (caller 在 main.py / __init__.py 启 store + worker):
    files_store = KbFilesStore(db_path)
    await files_store.initialize()
    kb_store = KbStore(persist_dir)
    embedder = Embedder(api_key)
    reranker = Reranker(api_key)
    retriever = KbRetriever(embedder, kb_store, reranker)
    worker = KbWorker(files_store, kb_store, embedder)
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import anyio
from bowhead_core.access_control import resource_grants_enforce_enabled
from bowhead_core.artifact_reconcile import probe_blob
from bowhead_core.artifact_store import _open_db
from bowhead_core.kb.errors import ScopeChangeMigrationError
from bowhead_core.kb.health import KbHealthObservation, SourceRecord, check_storage_health, health_observation_enabled, inspect_file_health
from bowhead_core.kb.parse_recovery import PARSE_ERROR_SERVICE_UNAVAILABLE
from bowhead_core.kb.permission import KbPermission, ScopeContext, drop_chunks_of_deleted_files, resolve_extra_personal_workspaces
from bowhead_core.kb.schemas import (
    KbFileDTO,
    KbFileScopePatchRequest,
    KbListScopeIn,
    KbScopeIn,
    KbSearchRequest,
    KbSearchResult,
)
from bowhead_core.kb.telemetry import emit_audit
from bowhead_core.space_governance import SpaceOperation
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_feature_licensed, require_not_readonly
from bowhead_gateway._http import content_disposition
from bowhead_gateway.access_context import (
    access_context_to_dict,
    build_access_context_from_request,
    has_workspace_access_source,
)
from bowhead_gateway.artifact_attribution import ATTRIBUTION_EVENT, decide_for_kb_scope, track_attribution
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.kb_extract_service import trigger_row_extraction
from bowhead_gateway.skill_import_landing import finish_bundle_import, inventory_bundle_for_kb

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bowhead_core.user_store import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/kb", tags=["knowledge-base"])


def _require_ingestion_admission() -> None:
    from bowhead_core.kb.ingestion import explicit_ingestion_enabled

    if not explicit_ingestion_enabled():
        raise _kb_error(
            503, "knowledge_ingestion_admission_closed", hint="Keep the original file in Files; ask an administrator to enable knowledge ingestion."
        )


def _kb_error(status_code: int, code: str, **payload: Any) -> HTTPException:
    """Return the finite KB error envelope used by routes and bulk results."""
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, **payload}},
    )


def _kb_file_not_found() -> HTTPException:
    """The single response for "no such KB file" and "a KB file you cannot read"."""
    return _kb_error(404, "kb_file_not_found")


# ─── ScopeContext / role resolution ────────────────────


def _resolve_role(user: User) -> Literal["user", "ws_admin", "team_admin"]:
    """[P1-O5] role 解析 V1 mock: BOWHEAD_AUTO_ADMIN_DOMAINS env (per memory project_p45_prod_env.md).

    real-auth ship 后走 JWT roles claim.
    """
    auto_admin_domains = os.environ.get("BOWHEAD_AUTO_ADMIN_DOMAINS", "")
    domains = [d.strip() for d in auto_admin_domains.split(",") if d.strip()]
    if domains and user.email and "@" in user.email:
        domain = user.email.split("@", 1)[1]
        if domain in domains:
            return "team_admin"
    return "user"


def _make_scope_context(user: User, request: Request) -> ScopeContext:
    """Build ScopeContext from authenticated user + request headers.

    批1/c4 (workspace-space-split): team fallback removed — a team id is NOT a workspace.
    Missing header → workspace_id="" ; personal/team scope
    operations proceed (they key on user_id/team_id), workspace-scope operations 400
    explicitly via _require_workspace_context.
    """
    # Batch 3/c2: X-Space-Id first, X-Workspace-Id fallback (old FE/script compatibility for one version).
    workspace_id = request.headers.get("X-Space-Id") or request.headers.get("X-Workspace-Id") or ""
    return ScopeContext(
        user_id=user.id,
        workspace_id=workspace_id,
        team_id=user.team_id,
        role=_resolve_role(user),
    )


async def _make_scope_context_checked(
    user: User,
    request: Request,
    *,
    workspace_id: str | None = None,
    allow_missing_workspace: bool = False,
    operation: SpaceOperation = SpaceOperation.WRITE,
) -> ScopeContext:
    raw = _make_scope_context(user, request)
    if workspace_id is not None:
        raw = ScopeContext(
            user_id=raw.user_id,
            workspace_id=workspace_id,
            team_id=raw.team_id,
            role=raw.role,
        )
    if not has_workspace_access_source(request):
        return raw
    access_ctx = await build_access_context_from_request(
        request,
        user=user,
        workspace_id=raw.workspace_id,
        allow_missing_workspace=allow_missing_workspace,
        operation=operation,
    )
    request.state.bowhead_access_context = access_context_to_dict(access_ctx)
    role: Literal["user", "ws_admin", "team_admin"] = "user"
    if access_ctx.is_team_admin:
        role = "team_admin"
    elif access_ctx.workspace_role == "ws_admin":
        role = "ws_admin"
    return ScopeContext(
        user_id=user.id,
        workspace_id=access_ctx.workspace_id,
        team_id=access_ctx.team_id,
        role=role,
    )


def _require_workspace_context(ctx: ScopeContext) -> None:
    """批1/c4: workspace-scope operations need an explicit space id — no team fallback.

    Without this, scope=workspace queries with workspace_id="" would match team files
    (whose workspace_id column is stored as '') — a cross-scope leak shape."""
    if not ctx.workspace_id:
        raise _kb_error(400, "missing_workspace_context")


def _kb_scope_resource_id(scope: str, scope_id: str, ctx: ScopeContext) -> str | None:
    # Batch 3/c1 (D2/D3 boundary): input = KB scope enum (canonical 'space', accept old spelling too);
    # output = grant resource prefix **frozen workspace:{id}** (persisted in grants/snapshot rows;
    # D2 frozen boundary: changes invalidate all existing grants). Two axes meet here; never conflate them.
    if scope in ("space", "workspace"):
        ws = scope_id or ctx.workspace_id
        if not ws:
            _require_workspace_context(ctx)  # raises 400 (batch 1/c4).
        return f"workspace:{ws}"
    if scope == "team":
        return f"team:{scope_id or ctx.team_id}"
    return None


async def _require_kb_scope_grant(
    request: Request,
    ctx: ScopeContext,
    *,
    scope: str,
    scope_id: str,
    permission: Literal["read", "write"],
    strict: bool = True,
) -> bool:
    resource_id = _kb_scope_resource_id(scope, scope_id, ctx)
    if resource_id is None:
        return True
    access_store = getattr(request.app.state, "access_control_store", None)
    access_ctx = getattr(request.state, "bowhead_access_context", None)
    if access_store is None or not access_ctx:
        return True
    decision = await access_store.explain_decision(
        access_ctx,
        "kb_scope",
        resource_id,
        permission,
    )
    if decision.get("allowed"):
        return True
    enforce = resource_grants_enforce_enabled()
    logger.info(
        "resource_grant.kb_scope_would_deny resource=%s permission=%s user=%s ws=%s reason=%s enforce=%s",
        resource_id,
        permission,
        ctx.user_id,
        ctx.workspace_id,
        decision.get("reason"),
        enforce,
    )
    if enforce and strict:
        raise _kb_error(
            403,
            "resource_grant_denied",
            resource_kind="kb_scope",
            resource_id=resource_id,
            permission=permission,
        )
    return False


def _requested_kb_search_scopes(scope_filter: list[str] | None) -> list[str]:
    # Batch 3/c1: normalize old-client workspace -> canonical space input; default all scopes.
    normalized = [{"workspace": "space"}.get(scope, scope) for scope in (scope_filter or ["personal", "space", "team"])]
    requested = [s for s in normalized if s in {"personal", "space", "team"}]
    return requested or ["personal", "space", "team"]


async def _allowed_kb_search_scope_filter(
    request: Request,
    ctx: ScopeContext,
    requested_scope_filter: list[str] | None,
) -> list[str]:
    requested = _requested_kb_search_scopes(requested_scope_filter)

    # In shadow mode, preserve legacy search breadth while still recording
    # would-deny logs for operators.
    if not resource_grants_enforce_enabled():
        if any(scope in requested for scope in ("personal", "space")):
            await _require_kb_scope_grant(
                request,
                ctx,
                scope="workspace",
                scope_id=ctx.workspace_id,
                permission="read",
                strict=False,
            )
        if "team" in requested:
            await _require_kb_scope_grant(
                request,
                ctx,
                scope="team",
                scope_id=ctx.team_id,
                permission="read",
                strict=False,
            )
        return requested

    allowed: list[str] = []
    if any(scope in requested for scope in ("personal", "space")) and await _require_kb_scope_grant(
        request,
        ctx,
        scope="workspace",
        scope_id=ctx.workspace_id,
        permission="read",
        strict=False,
    ):
        allowed.extend(scope for scope in requested if scope in ("personal", "space"))
    if "team" in requested and await _require_kb_scope_grant(
        request,
        ctx,
        scope="team",
        scope_id=ctx.team_id,
        permission="read",
        strict=False,
    ):
        allowed.append("team")
    if not allowed:
        raise _kb_error(
            403,
            "resource_grant_denied",
            resource_kind="kb_scope",
            resource_id="kb_search",
            permission="read",
        )
    return allowed


async def _require_kb_file_grant(
    request: Request,
    ctx: ScopeContext,
    file: KbFileDTO,
    *,
    permission: Literal["read", "write"],
    strict: bool = True,
) -> bool:
    access_store = getattr(request.app.state, "access_control_store", None)
    access_ctx = getattr(request.state, "bowhead_access_context", None)
    if access_store is None or not access_ctx:
        return True
    decision = await access_store.explain_decision(
        access_ctx,
        "kb_file",
        file.file_id,
        permission,
    )
    if decision.get("allowed"):
        return True
    return await _kb_file_grant_denied(request, ctx, file, decision, permission=permission, strict=strict)


async def _kb_file_grant_denied(
    request: Request,
    ctx: ScopeContext,
    file: KbFileDTO,
    decision: dict[str, Any],
    *,
    permission: Literal["read", "write"],
    strict: bool,
) -> bool:
    """Log and record a denied file grant; raise when enforced and strict.

    Returns whether the caller may still proceed (shadow mode only).
    """
    access_store = request.app.state.access_control_store
    access_ctx = request.state.bowhead_access_context
    enforce = resource_grants_enforce_enabled()
    logger.info(
        "resource_grant.kb_file_would_deny file=%s permission=%s user=%s ws=%s reason=%s enforce=%s",
        file.file_id,
        permission,
        ctx.user_id,
        ctx.workspace_id,
        decision.get("reason"),
        enforce,
    )
    await access_store.record_runtime_denied(
        access_ctx,
        resource_kind="kb_file",
        resource_id=file.file_id,
        permission=permission,
        reason=str(decision.get("reason") or "denied"),
        target_kind="kb_file",
        target_id=file.file_id,
        payload={
            "filename": file.filename,
            "scope": file.scope,
            "scope_id": file.scope_id,
            "enforce": enforce,
        },
    )
    if enforce and strict:
        raise _kb_error(
            403,
            "resource_grant_denied",
            resource_kind="kb_file",
            resource_id=file.file_id,
            permission=permission,
        )
    return not enforce


async def _filter_kb_files_by_file_grants(
    request: Request,
    ctx: ScopeContext,
    rows: list[KbFileDTO],
    *,
    scopes: list[str],
    permission: Literal["read", "write"] = "read",
) -> list[KbFileDTO]:
    """Keep the rows the caller may read: the same scope rule and file grant check as
    the per-id routes (``_kb_scope_check``), with grant decisions fetched in one batch.

    Grant denials are audited as one ``resource_list_access_filtered`` summary row per
    request (``scopes`` = the scopes this list covered), not one row per file.
    """
    readable = [row for row in rows if row.team_id == ctx.team_id and KbPermission.can_read(row, ctx)]
    access_store = getattr(request.app.state, "access_control_store", None)
    access_ctx = getattr(request.state, "bowhead_access_context", None)
    if access_store is None or not access_ctx or not readable:
        return readable
    decisions = await access_store.explain_decisions(access_ctx, "kb_file", [row.file_id for row in readable], permission)
    enforce = resource_grants_enforce_enabled()
    out: list[KbFileDTO] = []
    denied: list[tuple[str, str]] = []
    for row in readable:
        decision = decisions[row.file_id]
        if decision.get("allowed"):
            out.append(row)
            continue
        denied.append((row.file_id, str(decision.get("reason") or "denied")))
        if not enforce:
            out.append(row)
    if denied:
        logger.info(
            "resource_grant.kb_file_list_would_deny count=%d permission=%s user=%s ws=%s scopes=%s enforce=%s",
            len(denied),
            permission,
            ctx.user_id,
            ctx.workspace_id,
            ",".join(scopes),
            enforce,
        )
        try:
            await access_store.record_runtime_denial_summary(
                access_ctx,
                resource_kind="kb_file",
                permission=permission,
                denied=denied,
                target_kind="kb_file_list",
                target_id="kb.list_files",
                payload={"scopes": scopes},
            )
        except Exception as exc:
            logger.warning(
                "resource_grant.kb_file_list_audit_failed count=%d user=%s team=%s err=%s",
                len(denied),
                ctx.user_id,
                ctx.team_id,
                type(exc).__name__,
            )
    return out


async def _filter_kb_search_result_by_file_grants(
    request: Request,
    ctx: ScopeContext,
    result: KbSearchResult,
    *,
    permission: Literal["read", "write"] = "read",
) -> KbSearchResult:
    files_store = getattr(request.app.state, "kb_files_store", None)
    # Fallback: exclude chunks whose file row is soft-deleted (same criterion as conversational kb_search).
    live_chunks = await drop_chunks_of_deleted_files(files_store, result.chunks)
    if len(live_chunks) != len(result.chunks):
        result = result.model_copy(update={"chunks": live_chunks})
    access_store = getattr(request.app.state, "access_control_store", None)
    access_ctx = getattr(request.state, "bowhead_access_context", None)
    if access_store is None or not access_ctx:
        return result

    file_cache: dict[str, KbFileDTO | None] = {}
    chunks = []
    for chunk in result.chunks:
        file_id = chunk.file_id
        if not file_id:
            chunks.append(chunk)
            continue
        if file_id not in file_cache:
            try:
                file_cache[file_id] = await files_store.get(file_id) if files_store is not None else None
            except Exception as exc:
                logger.warning("kb.search.file_lookup_failed file=%s err=%s", file_id, exc)
                file_cache[file_id] = None
        file = file_cache[file_id]
        if file is not None:
            allowed = await _require_kb_file_grant(
                request,
                ctx,
                file,
                permission=permission,
                strict=False,
            )
        else:
            decision = await access_store.explain_decision(
                access_ctx,
                "kb_file",
                file_id,
                permission,
            )
            allowed = bool(decision.get("allowed"))
            if not allowed:
                enforce = resource_grants_enforce_enabled()
                logger.info(
                    "resource_grant.kb_search_file_would_deny file=%s permission=%s user=%s ws=%s reason=%s enforce=%s",
                    file_id,
                    permission,
                    ctx.user_id,
                    ctx.workspace_id,
                    decision.get("reason"),
                    enforce,
                )
                await access_store.record_runtime_denied(
                    access_ctx,
                    resource_kind="kb_file",
                    resource_id=file_id,
                    permission=permission,
                    reason=str(decision.get("reason") or "denied"),
                    target_kind="kb_file",
                    target_id=file_id,
                    payload={
                        "chunk_id": chunk.chunk_id,
                        "scope": chunk.scope,
                        "source": "kb_search",
                        "enforce": enforce,
                    },
                )
                allowed = not enforce
        if allowed:
            chunks.append(chunk)
    if len(chunks) == len(result.chunks):
        return result
    return result.model_copy(update={"chunks": chunks})


# ─── DI getters (caller injects stores in main.py) ───


def _get_files_store(request: Request):
    fs = getattr(request.app.state, "kb_files_store", None)
    if fs is None:
        raise _kb_error(503, "kb_service_unavailable")
    return fs


def _get_kb_store(request: Request):
    s = getattr(request.app.state, "kb_store", None)
    if s is None:
        raise _kb_error(503, "kb_service_unavailable")
    return s


def _get_retriever(request: Request):
    r = getattr(request.app.state, "kb_retriever", None)
    if r is None:
        raise _kb_error(503, "kb_service_unavailable")
    return r


def _get_worker(request: Request):
    w = getattr(request.app.state, "kb_worker", None)
    if w is None:
        raise _kb_error(503, "kb_service_unavailable")
    return w


async def _get_team_kb_runtime(request: Request, user: User):
    registry = getattr(request.app.state, "team_kb_runtime_registry", None)
    user_store = getattr(request.app.state, "user_store", None)
    if registry is None or user_store is None or not user.team_id:
        return None
    try:
        cfg, version = await user_store.get_team_llm_config_with_version(user.team_id)
        return await registry.get(user.team_id, cfg, version)
    except Exception as e:
        logger.warning("kb.team_runtime_unavailable team=%s err=%s", user.team_id, e)
        return None


async def _get_effective_kb_store(request: Request, user: User):
    runtime = await _get_team_kb_runtime(request, user)
    if runtime is not None and runtime.kb_store is not None:
        return runtime.kb_store
    return _get_kb_store(request)


async def _get_effective_retriever(request: Request, user: User):
    runtime = await _get_team_kb_runtime(request, user)
    if runtime is not None and runtime.retriever is not None:
        return runtime.retriever
    return _get_retriever(request)


async def _get_effective_worker(request: Request, user: User):
    runtime = await _get_team_kb_runtime(request, user)
    if runtime is not None and runtime.worker is not None:
        return runtime.worker
    worker = getattr(request.app.state, "kb_worker", None)
    if worker is not None:
        return worker
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is not None and user.team_id:
        try:
            config, _ = await user_store.get_team_llm_config_with_version(user.team_id)
        except Exception:
            logger.warning("kb.worker_config_check_failed team=%s", user.team_id, exc_info=True)
        else:
            kb_config = config.get("kb") if isinstance(config, dict) else None
            if not isinstance(kb_config, dict) or not kb_config.get("models") or not kb_config.get("active"):
                raise _kb_error(503, "kb_embedding_not_configured")
    return _get_worker(request)


def _get_artifact_store(request: Request):
    """artifact_store wrap (跟现 routes 同模式)."""
    a = getattr(request.app.state, "artifact_store", None)
    if a is None:
        raise _kb_error(503, "kb_service_unavailable")
    return a


# ─── Shared scope check (P0 cross-scope leak defense) ───


async def _kb_scope_check(
    file_id: str,
    ctx: ScopeContext,
    files_store,
    request: Request,
    *,
    include_deleted: bool = False,
) -> KbFileDTO:
    """Return the file only if the caller may read it; every other case is one 404.

    Missing, soft-deleted, another team's, outside the caller's scope and denied by a
    read grant all raise the same ``kb_file_not_found`` so the status code and body never
    reveal whether the id exists (canvas ``report_unavailable`` precedent). The real
    reason stays server-side: the audit event, the grant decision log and the runtime
    denial record are written exactly as before.
    """
    file = await files_store.get(file_id)
    if not file or (file.soft_deleted_at and not include_deleted):
        raise _kb_file_not_found()
    if file.team_id != ctx.team_id or not KbPermission.can_read(file, ctx):
        emit_audit(
            "kb.unauthorized_access_attempt",
            {
                "file_id": file_id,
                "user_id": ctx.user_id,
                "attempted_scope": file.scope,
                "caller_workspace": ctx.workspace_id,
            },
        )
        raise _kb_file_not_found()
    try:
        await _require_kb_scope_grant(
            request,
            ctx,
            scope=file.scope,
            scope_id=file.scope_id,
            permission="read",
        )
        await _require_kb_file_grant(
            request,
            ctx,
            file,
            permission="read",
        )
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        raise _kb_file_not_found() from None
    return file


def _enrich_dto(dto: KbFileDTO, ctx: ScopeContext) -> KbFileDTO:
    """Set can_delete / can_change_scope flags on DTO based on caller permission."""
    from bowhead_core.kb.generations import use_guard_enabled
    from bowhead_core.kb.maintenance import cached_source_state

    return dto.model_copy(
        update={
            "source_health": cached_source_state(dto) if use_guard_enabled() else None,
            "can_delete": KbPermission.can_delete(dto, ctx, ctx.role),
            # can_change_scope simplifies to can-delete and not current scope.
            "can_change_scope": KbPermission.can_delete(dto, ctx, ctx.role),
        }
    )


async def _with_uploader_names(request: Request, ctx: ScopeContext, dtos: list[KbFileDTO]) -> list[KbFileDTO]:
    """填 ``uploaded_by_name``（知识库体验整治 PRD FR2.3）：界面只显示成员名，不显示用户 id。

    只解析本团队成员；取不到时保持 None，由前端显示占位。名字是展示信息，查询失败不影响列表。
    """
    from bowhead_core.kb.generations import use_guard_enabled
    from bowhead_core.kb.maintenance import cached_source_state

    if use_guard_enabled():
        from bowhead_gateway.kb_maintenance import team_runtime

        try:
            runtime = await team_runtime(request.app, ctx.team_id)
            version = runtime.kb_store.index_version
        except Exception:
            version = None
        dtos = [d.model_copy(update={"source_health": cached_source_state(d, index_version=version)}) for d in dtos]
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None or not dtos:
        return dtos
    try:
        names = await user_store.get_team_member_labels(ctx.team_id, [d.uploaded_by for d in dtos])
    except Exception:
        logger.warning("kb.uploader_names_failed team_id=%s", ctx.team_id, exc_info=True)
        return dtos
    return [d.model_copy(update={"uploaded_by_name": names[d.uploaded_by]}) if d.uploaded_by in names else d for d in dtos]


# ─── upload (POST /files) ─────────────────────────────


_SINGLE_MAX_SIZE = 200 * 1024 * 1024
"""单文件上传硬闸 (200MB, 对齐前端 kb-upload-modal.tsx 的 MAX_SIZE 承诺)。

**这是安全控制, 不是 UX 提示** —— 前端那个 200MB 是客户端限制, curl 一行就绕过。
此前本路由**完全没有服务端 size 闸**(`len(raw)` 只用来记录 size_bytes, 从不检查),
而同文件的 bundle 路(`_BUNDLE_MAX_SIZE`)和 presign 路(`_MAX_UPLOAD_BYTES`)都有 ——
是漏了, 不是设计取舍。worker 跑在网关进程内(`background_tasks.add_task`),
无界 `await file.read()` = 任意登录用户可 OOM 整个网关 → 全租户下线。

⚠ 必须在 `.read()` **之前**判 `file.size`: 读完再判是马后炮, 内存那时已经吃进去了
(bundle 路的 `len(raw) > _BUNDLE_MAX_SIZE` 正是这个毛病, 见 §_read_bundle)。
"""

_ALLOWED_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/markdown",
    "text/x-markdown",
    "text/plain",
    # image-extraction sprint v1.0 §2.6: jpg/png/webp via vision LLM
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    # kb-doctype-config sprint v1.0 §2.6: xlsx indexing.
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    # kb-doctype-config sprint: financial due-diligence statements often use legacy .xls.
    "application/vnd.ms-excel",
    # KB experience cleanup §10 Q5: CSV uses CsvLoader (table chunking matches xlsx).
    "text/csv",
    # pptx-loader sprint: PptxLoader dispatch integration allows pptx ingestion into KB.
    # ⚠ Permit .pptx only, never legacy binary .ppt: python-pptx cannot read .ppt;
    #   admitting it only yields parse_failed after upload.
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

# ZIP is a container, not an ingestable child document. Keep this list aligned
# with the frontend's KB_UPLOAD_FORMATS (excluding .zip). MIME sniffing alone
# can classify arbitrary text-like .bin files as text/plain and index them.
_KB_DOCUMENT_SUFFIXES = frozenset({".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".csv", ".md", ".markdown", ".txt", ".jpg", ".jpeg", ".png", ".webp"})


def _supports_kb_document_name(filename: str) -> bool:
    return Path(filename).suffix.lower() in _KB_DOCUMENT_SUFFIXES


# OOXML/OLE2 containers and short/CJK text routinely sniff as application/zip
# or application/octet-stream; recover the real type by extension. Shared by
# the single-file upload, bundle, and presign-bundle ingest paths so they never
# diverge (a .txt accepted by one route but rejected by another).
_OOXML_ZIP_MIMES = ("application/zip", "application/octet-stream", "application/x-zip-compressed")


def _resolve_kb_mime(detected_mime: str, filename: str) -> str:
    """Normalize a sniffed MIME to a KB-supported type via filename extension."""
    lower = (filename or "").lower()
    m = detected_mime
    if lower.endswith((".md", ".markdown")) and m in ("text/plain", "application/octet-stream"):
        return "text/markdown"
    if lower.endswith(".txt") and m == "application/octet-stream":
        return "text/plain"
    # CSV often sniffs as text/plain (Chinese/short files) or aliases; normalize by extension to text/csv.
    if lower.endswith(".csv") and m in (
        "text/plain",
        "text/csv",
        "application/csv",
        "text/x-csv",
        "application/vnd.ms-excel",
        "application/octet-stream",
    ):
        return "text/csv"
    if lower.endswith(".docx") and m in _OOXML_ZIP_MIMES:
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if lower.endswith(".xlsx") and m in _OOXML_ZIP_MIMES:
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if lower.endswith(".pptx") and m in _OOXML_ZIP_MIMES:
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if lower.endswith(".xls") and m in (
        "application/octet-stream",
        "application/x-ole-storage",
        "application/cdfv2",
        "application/cdfv2-corrupt",
    ):
        return "application/vnd.ms-excel"
    if lower.endswith(".pdf") and m in ("application/x-pdf", "application/octet-stream"):
        return "application/pdf"
    if lower.endswith((".jpg", ".jpeg")) and m in ("application/octet-stream", "image/x-jpeg"):
        return "image/jpeg"
    if lower.endswith(".png") and m == "application/octet-stream":
        return "image/png"
    if lower.endswith(".webp") and m == "application/octet-stream":
        return "image/webp"
    return m


@router.post("/files", response_model=KbFileDTO, dependencies=[Depends(require_not_readonly)])
async def upload_file(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    knowledge_intent: Literal["add_to_knowledge"] = Form(...),
    scope: KbScopeIn = Form(...),
    scope_id: str = Form(...),
    use_llm: bool = Form(False),
    # kb-knowledge-tables v1.0: optional doc_type_id binds file to a workspace doc_type
    # → worker runs StructuredExtractor and saves a KbExtractedRecord with extracted fields.
    doc_type_id: str | None = Form(None),
    user: User = Depends(_current_user),
    _feat: None = Depends(require_feature_licensed("kb")),  # §5.1 entitlement (additive to scope authz, INV-2)
) -> KbFileDTO:
    """Upload file → save artifact → create kb_files row → enqueue parse worker.

    P0-R1: 验 scope_id 跟 caller context 匹配 (防跨 ws/team/user 写 leak).
    P1-O12: server-side mime 检测 (python-magic) + filename ext fallback (P1-S1).
    """
    _require_ingestion_admission()
    ctx = await authorize_kb_scope_write(request, user, scope=scope, scope_id=scope_id)

    # [V3 P0-V3-1 fix] cross-workspace doc_type_id leak defense:
    # If supplied, doc_type_id must belong to caller's space, preventing foreign doc_type scope bypass.
    if doc_type_id:
        dt_store = getattr(request.app.state, "kb_doc_type_store", None)
        if dt_store is None:
            raise _kb_error(503, "kb_doc_type_service_unavailable")
        dt = await dt_store.get(doc_type_id)
        if dt is None:
            raise _kb_error(404, "doc_type_not_found")
        if dt.workspace_id != ctx.workspace_id:
            raise _kb_error(404, "doc_type_not_found")
        # kb-matrix M4 gate① (wave-1 security review P2-1): upload is one of three binding entries;
        # guarding only PATCH bind leaves frozen invariants solely on gate③. Never bind column-group templates/graduated snapshots.
        from bowhead_core.kb.doc_type_store import is_column_group_template as _is_tpl_upload

        if _is_tpl_upload(dt):
            raise _kb_error(422, "doc_type_not_bindable")

    # ── Hard size gate (before read; see _SINGLE_MAX_SIZE docstring) ──
    # Starlette multipart parser supplies file.size (body spooled to disk, not memory);
    # None means unavailable from parser; fall back to checking after read, never silently admit.
    if file.size is not None and file.size > _SINGLE_MAX_SIZE:
        raise _kb_error(413, "kb_file_too_large")
    if not _supports_kb_document_name(file.filename or ""):  # reject before reading the body
        raise _kb_error(415, "kb_file_type_unsupported")
    raw = await file.read()
    return await ingest_kb_file_bytes(
        request,
        background_tasks,
        user,
        ctx,
        raw=raw,
        filename=file.filename or "",
        client_mime=file.content_type,
        scope=scope,
        scope_id=scope_id,
        use_llm=use_llm,
        doc_type_id=doc_type_id,
        route="kb_single_upload",
    )


async def authorize_kb_scope_write(request: Request, user: User, *, scope: str, scope_id: str) -> Any:
    """Scope-context, write-permission and resource-grant checks shared by every KB file entry."""
    ctx = await _make_scope_context_checked(user, request)
    if scope in ("space", "workspace"):
        _require_workspace_context(ctx)  # Batch 1/c4: prevent scope_id=""+ws="" empty-to-empty writes.
    if not KbPermission.can_write_to_scope(scope, scope_id, ctx, ctx.role):
        raise _kb_error(403, "kb_scope_access_denied")
    await _require_kb_scope_grant(
        request,
        ctx,
        scope=scope,
        scope_id=scope_id,
        permission="write",
    )
    return ctx


async def ingest_kb_file_bytes(
    request: Request,
    background_tasks: BackgroundTasks,
    user: User,
    ctx: Any,
    *,
    raw: bytes,
    filename: str,
    client_mime: str | None,
    scope: str,
    scope_id: str,
    use_llm: bool = False,
    doc_type_id: str | None = None,
    route: str,
) -> KbFileDTO:
    """Store one already-authorized file and enqueue parsing — the single KB ingestion path.

    Used by the multipart upload and by imports that fetch bytes elsewhere (Google Drive);
    callers must have run :func:`authorize_kb_scope_write` for the same scope.
    """
    if len(raw) > _SINGLE_MAX_SIZE:
        raise _kb_error(413, "kb_file_too_large")
    if not _supports_kb_document_name(filename):
        raise _kb_error(415, "kb_file_type_unsupported")

    # ── server-side mime detection (P1-O12) ──
    detected_mime: str
    try:
        import magic

        detected_mime = magic.from_buffer(raw[:2048], mime=True)
    except ImportError:
        # python-magic absent -> client-supplied fallback (accepted by V1).
        detected_mime = client_mime or "application/octet-stream"

    # Normalize sniffed MIME → KB-supported type by extension (markdown/txt as
    # text, OOXML zip→docx/xlsx, OLE2→xls, pdf, images). Shared resolver so the
    # single-file, bundle, and presign-bundle paths never diverge.
    detected_mime = _resolve_kb_mime(detected_mime, filename)

    if detected_mime not in _ALLOWED_MIMES:
        raise _kb_error(415, "kb_file_type_unsupported")

    # ── Store artifact (current API: put_bytes returns Artifact object) ──
    artifact_store = _get_artifact_store(request)
    # Space isolation batch 4 ①: previously supplied **neither workspace_id nor session_id** and **never wrote**
    # artifact_upload_bindings; column and ladder cannot infer ownership, always NULL, all 403 after enabling gate.
    # ★ Undetected because this file has no INSERT INTO artifacts; old guard collected writes by
    #   SQL presence, **structurally missing kb.py**. Now scan put_bytes( call sites
    #   (see test_artifact_workspace_id_migration.py).
    # Ownership is knowable here; compute before put_bytes and pass down: team sentinel, real space ID.
    attribution = decide_for_kb_scope(scope, ctx.workspace_id)
    # ★ Retain local name workspace_id: downstream artifact_upload_bindings and files_store.create
    #   consume **original empty-string** semantics, never sentinel. Preserve exactly.
    workspace_id = attribution.scope_workspace_id
    logger.info("%s.%s reason=%s scope=%s", ATTRIBUTION_EVENT, route, attribution.reason, scope)
    artifact = await artifact_store.put_bytes(
        content=raw,
        filename=filename or "unnamed",
        mime=detected_mime,
        source="kb_upload",
        kind="user_upload",
        producer_kind="kb_upload",
        created_by=user.id,
        workspace_id=attribution.artifact_workspace_id,
    )
    artifact_id = artifact.id

    # ── create kb_files row ──
    files_store = _get_files_store(request)
    from bowhead_core.kb.ingestion import KnowledgeAction

    file_id, _, _ = await files_store.create_admitted(
        action=KnowledgeAction(f"upload:{artifact.id}", "kb_ui", user.id, ctx.team_id, ctx.workspace_id),
        source_fingerprint=artifact.id,
        artifact_id=artifact_id,
        filename=filename or "unnamed",
        mime_type=detected_mime,
        size_bytes=len(raw),
        scope=scope,
        scope_id=scope_id,
        workspace_id=workspace_id,
        team_id=ctx.team_id,
        uploaded_by=user.id,
        use_llm_marker=use_llm,
        doc_type_id=doc_type_id,
    )
    emit_audit(
        "kb.uploaded",
        {
            "file_id": file_id,
            "scope": scope,
            "size_bytes": len(raw),
            "mime": detected_mime,
        },
    )

    # ── enqueue parse worker (P0-O1: only file_id) ──
    worker = await _get_effective_worker(request, user)
    background_tasks.add_task(worker.parse_and_index, file_id)

    dto = await files_store.get(file_id)
    if dto is None:
        raise _kb_error(500, "kb_file_write_failed")
    # Instrument **after success**: no 4xx/5xx rejected requests counted (artifact_attribution module docstring ④).
    await track_attribution(request, route=route, reason=attribution.reason, scope=scope)
    return _enrich_dto(dto, ctx)


# ─── zip-bundle upload (POST /files/bundle) ───────────
# 0.24.4 (kb-zip-bundle sprint): user uploads .zip of mixed docs (PDF/DOCX/MD/etc),
# server-side unzip, per-file ingest into KB. Skips __MACOSX/, .DS_Store, dirs,
# unsupported MIMEs (logs skip list in response).


_BUNDLE_MAX_SIZE = 500 * 1024 * 1024  # 500 MB raw zip (uncompressed budget separate)
_BUNDLE_MAX_UNCOMPRESSED = 2 * 1024 * 1024 * 1024  # 2 GB zip-bomb guard
_BUNDLE_MAX_FILES = 1000


class KbBundleSkippedItem(BaseModel):
    filename: str
    # Kept for one compatibility cycle. User-facing clients must use
    # ``reason_code`` and must never render backend exception prose.
    reason: None = None
    reason_code: str


class KbBundleUploadResponse(BaseModel):
    """Summary response for zip-bundle upload.

    `uploaded`: each successfully ingested file's DTO (with file_id).
    `skipped`: filename + finite reason_code. ``reason`` is always null.
    `total_entries`: file entries in zip (pre-filter; directories are not files).
    """

    uploaded: list[KbFileDTO]
    skipped: list[KbBundleSkippedItem]
    total_entries: int
    bundle_filename: str
    bundle_artifact_id: str
    bundle_mime: str
    bundle_size: int
    # skill-import batch 1b: bundle inventory (BundleInventory.to_dict; plain document/data bundles also have it, but no capability-shaped components)
    # and components landed as personal Skills (red -> import_status=pending). Inventory failure leaves both empty, without affecting KB ingestion.
    inventory: dict[str, Any] | None = None
    landed_skills: list[dict[str, Any]] = Field(default_factory=list)


def _decode_zip_name(raw_bytes: bytes) -> str:
    """zipfile may yield filenames as latin-1 (when no UTF-8 flag set on entry).
    Try utf-8 → gbk → fall back to original. Mac-zipped Chinese files commonly
    use GBK encoding; this lets users see readable names instead of 涔�?�宸�?�."""
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw_bytes.decode("gbk")
    except UnicodeDecodeError:
        pass
    return raw_bytes.decode("latin-1", errors="replace")


def _classify_zip_entry(name: str, size: int) -> tuple[bool, str]:
    """Returns (keep, skip_reason). skip_reason '' when keep=True.
    0.24.5: check macOS/system metadata + directory BEFORE size==0 so
    "__MACOSX/foo" (size=0) reports macos_metadata not empty_or_directory."""
    if name.endswith("/"):
        return False, "directory"
    if name.startswith("__MACOSX/") or "/__MACOSX/" in name:
        return False, "macos_metadata"
    base = name.rsplit("/", 1)[-1]
    if base.startswith(("._", "~$")) or base == ".DS_Store" or base in ("Thumbs.db", "desktop.ini"):
        return False, "system_metadata"
    if size == 0:
        return False, "empty_file"
    return True, ""


def _bundle_skip(filename: str, reason_code: str) -> KbBundleSkippedItem:
    return KbBundleSkippedItem(
        filename=filename,
        reason=None,
        reason_code=reason_code,
    )


def _classified_bundle_skip_code(reason: str) -> str:
    if reason in {"macos_metadata", "system_metadata"}:
        return "kb_bundle_entry_metadata"
    return "kb_bundle_entry_empty"


async def _process_zip_bundle(
    raw: bytes,
    bundle_name: str,
    *,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User,
    ctx: ScopeContext,
    scope: str,
    scope_id: str,
    use_llm: bool,
    attribution_route: str = "kb_bundle_upload",
) -> KbBundleUploadResponse:
    """Core bundle ingest — caller responsible for ACL + raw byte source.

    `attribution_route`：归属打点用的路由名。`/admin/import` 传 `kb_admin_import`，
    以免管理员服务端导入混进用户 bundle 上传的分布里。

    Shared by /files/bundle (HTTP multipart) and /admin/import (server-side
    file path). Raises HTTPException on caps / corrupt zip.
    """
    import io
    import zipfile

    _require_ingestion_admission()
    if len(raw) > _BUNDLE_MAX_SIZE:
        raise _kb_error(413, "kb_bundle_too_large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw), "r")
    except zipfile.BadZipFile as e:
        raise _kb_error(400, "kb_bundle_invalid") from e

    entries = zf.infolist()
    if len(entries) > _BUNDLE_MAX_FILES:
        raise _kb_error(413, "kb_bundle_too_many_entries")
    total_uncompressed = sum(e.file_size for e in entries)
    if total_uncompressed > _BUNDLE_MAX_UNCOMPRESSED:
        raise _kb_error(413, "kb_bundle_uncompressed_too_large")

    artifact_store = _get_artifact_store(request)
    files_store = _get_files_store(request)
    worker = await _get_effective_worker(request, user)
    attribution = decide_for_kb_scope(scope, ctx.workspace_id)
    # ★ As above: keep local name; downstream bindings / kb_files retain original empty-string semantics.
    workspace_id = attribution.scope_workspace_id
    logger.info("%s.kb_bundle_upload reason=%s scope=%s", ATTRIBUTION_EVENT, attribution.reason, scope)

    # Preserve a stable, server-owned bundle root for Composer grouping and
    # replay.  Children remain the runtime inputs, while this artifact proves
    # the display metadata and group identity across F5/hydrate.
    bundle_artifact = await artifact_store.put_bytes(
        content=raw,
        filename=bundle_name,
        mime="application/zip",
        source="kb_bundle_upload",
        kind="user_upload",
        producer_kind="kb_bundle_upload",
        created_by=user.id,
        # Space isolation batch 4 ①: formerly relied on the binding below, but **team binding stores empty string**;
        # ladder normalizes it to NULL, leaving team bundles unattributable. Explicitly persist sentinel.
        workspace_id=attribution.artifact_workspace_id,
    )
    async with _open_db(artifact_store.db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO artifact_upload_bindings(artifact_id,workspace_id,user_id,session_id,created_at) VALUES (?,?,?,NULL,?)",
            (
                bundle_artifact.id,
                workspace_id,
                user.id,
                datetime.now(tz=UTC).isoformat(timespec="seconds"),
            ),
        )
        await db.commit()

    uploaded: list[KbFileDTO] = []
    skipped: list[KbBundleSkippedItem] = []
    child_ids: list[str] = []

    # skill-import batch 1b: inventory entire bundle; capability components (SKILL.md / agents / openapi / mcp) never enter KB (decision ④).
    bundle_import = await inventory_bundle_for_kb(request, user, zf)

    # python-magic for MIME detection (same as single-file path).
    try:
        import magic

        _magic_available = True
    except ImportError:
        _magic_available = False

    for entry in entries:
        # zipfile names: when UTF-8 flag not set, ZipFile decodes as cp437
        # (latin-1-ish). Re-decode from raw bytes for non-ASCII filenames.
        raw_name = entry.filename.encode("cp437") if not (entry.flag_bits & 0x800) else entry.filename.encode("utf-8")
        name = _decode_zip_name(raw_name)

        keep, reason = _classify_zip_entry(name, entry.file_size)
        if not keep:
            # P2 fix (0.24.5): record garbage entries in skipped list so user
            # sees __MACOSX/, .DS_Store, etc were filtered (not silently
            # vanished). Only true directories ('foo/') silently dropped since
            # they're not "files" by user mental model.
            if reason != "directory":
                skipped.append(_bundle_skip(name, _classified_bundle_skip_code(reason)))
            continue

        if bundle_import.is_capability_member(entry):
            skipped.append(_bundle_skip(name, "kb_bundle_entry_capability_component"))
            continue

        if not _supports_kb_document_name(name):
            skipped.append(_bundle_skip(name, "kb_bundle_entry_type_unsupported"))
            continue

        try:
            payload = zf.read(entry)
        except (zipfile.BadZipFile, RuntimeError):
            skipped.append(_bundle_skip(name, "kb_bundle_entry_unreadable"))
            continue

        if len(payload) == 0:
            skipped.append(_bundle_skip(name, "kb_bundle_entry_empty"))
            continue

        # MIME detect (magic + shared ext fallback, same as single-file /files).
        detected_mime = magic.from_buffer(payload[:2048], mime=True) if _magic_available else "application/octet-stream"
        detected_mime = _resolve_kb_mime(detected_mime, name)

        if detected_mime not in _ALLOWED_MIMES:
            skipped.append(_bundle_skip(name, "kb_bundle_entry_type_unsupported"))
            continue

        # Use just the basename (zip path-prefix is irrelevant for KB display).
        display_name = name.rsplit("/", 1)[-1]
        try:
            artifact = await artifact_store.put_bytes(
                content=payload,
                filename=display_name,
                mime=detected_mime,
                source="kb_upload",
                kind="user_upload",
                producer_kind="kb_upload",
                created_by=user.id,
                # Space isolation batch 4 ①: same as above, team binding empty string normalizes to NULL in ladder.
                workspace_id=attribution.artifact_workspace_id,
            )
            async with _open_db(artifact_store.db_path) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO artifact_upload_bindings(artifact_id,workspace_id,user_id,session_id,created_at) VALUES (?,?,?,NULL,?)",
                    (
                        artifact.id,
                        workspace_id,
                        user.id,
                        datetime.now(tz=UTC).isoformat(timespec="seconds"),
                    ),
                )
                await db.execute(
                    "INSERT OR IGNORE INTO chat_attachment_bundle_members(bundle_artifact_id,child_artifact_id,created_at) VALUES (?,?,?)",
                    (
                        bundle_artifact.id,
                        artifact.id,
                        datetime.now(tz=UTC).isoformat(timespec="seconds"),
                    ),
                )
                await db.commit()
            from bowhead_core.kb.ingestion import KnowledgeAction

            ctx = await authorize_kb_scope_write(request, user, scope=scope, scope_id=scope_id)
            file_id, _, _ = await files_store.create_admitted(
                action=KnowledgeAction(
                    f"bundle:{bundle_artifact.id}:{artifact.id}",
                    "admin_import" if attribution_route == "kb_admin_import" else "kb_ui",
                    user.id,
                    ctx.team_id,
                    ctx.workspace_id,
                ),
                source_fingerprint=artifact.id,
                artifact_id=artifact.id,
                filename=display_name,
                mime_type=detected_mime,
                size_bytes=len(payload),
                scope=scope,
                scope_id=scope_id,
                workspace_id=workspace_id,
                team_id=ctx.team_id,
                uploaded_by=user.id,
                use_llm_marker=use_llm,
                doc_type_id=None,
            )
            emit_audit(
                "kb.uploaded.bundle_entry",
                {
                    "file_id": file_id,
                    "scope": scope,
                    "size_bytes": len(payload),
                    "mime": detected_mime,
                    "bundle": bundle_name,
                },
            )
            child_ids.append(file_id)
            dto = await files_store.get(file_id)
            if dto:
                uploaded.append(_enrich_dto(dto, ctx))
        except Exception as e:
            logger.exception("kb.bundle.ingest_failed name=%s err=%s", display_name, e)
            skipped.append(_bundle_skip(name, "kb_bundle_entry_ingest_failed"))

    # Fan out ALL children in ONE background task so they parse concurrently
    # under worker._PARSE_SEMAPHORE(5). Per-file background_tasks.add_task is
    # serially awaited by FastAPI → a single slow scanned PDF blocks the whole
    # bundle (Semaphore(5) never exploited). Mirrors the presign-bundle confirm
    # path (kb_presign.py) which already uses parse_and_index_many.
    if child_ids:
        background_tasks.add_task(worker.parse_and_index_many, child_ids)

    await finish_bundle_import(
        request, user, zf=zf, outcome=bundle_import, bundle_artifact_id=bundle_artifact.id, artifact_db_path=artifact_store.db_path, scope=scope
    )

    # Explicit ZIP directory entries never create a child or skipped item, so
    # counting them leaves the client progress stuck below 100%.
    total_files = sum(not entry.is_dir() for entry in entries)
    emit_audit(
        "kb.uploaded.bundle",
        {
            "bundle": bundle_name,
            "total": total_files,
            "uploaded": len(uploaded),
            "skipped": len(skipped),
        },
    )
    await track_attribution(request, route=attribution_route, reason=attribution.reason, scope=scope)
    return KbBundleUploadResponse(
        uploaded=uploaded,
        skipped=skipped,
        total_entries=total_files,
        bundle_filename=bundle_name,
        bundle_artifact_id=bundle_artifact.id,
        bundle_mime=bundle_artifact.mime,
        bundle_size=bundle_artifact.size,
        inventory=bundle_import.report_dict(),
        landed_skills=bundle_import.landed_skills,
    )


# ─── HTTP route: multipart bundle upload ───────────────


@router.post("/files/bundle", response_model=KbBundleUploadResponse, dependencies=[Depends(require_not_readonly)])
async def upload_bundle(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    knowledge_intent: Literal["add_to_knowledge"] = Form(...),
    scope: KbScopeIn = Form(...),
    scope_id: str = Form(...),
    use_llm: bool = Form(False),
    user: User = Depends(_current_user),
    _feat: None = Depends(require_feature_licensed("kb")),  # §5.1 entitlement
) -> KbBundleUploadResponse:
    """Upload a .zip → unzip → per-file ingest into KB (browser multipart path)."""
    ctx = await _make_scope_context_checked(user, request)
    if scope in ("space", "workspace"):
        _require_workspace_context(ctx)  # Batch 1/c4: prevent scope_id=""+ws="" empty-to-empty writes.
    if not KbPermission.can_write_to_scope(scope, scope_id, ctx, ctx.role):
        raise _kb_error(403, "kb_scope_access_denied")
    await _require_kb_scope_grant(
        request,
        ctx,
        scope=scope,
        scope_id=scope_id,
        permission="write",
    )

    _require_ingestion_admission()
    bundle_name = file.filename or "bundle.zip"
    if not (
        bundle_name.lower().endswith(".zip")
        or (file.content_type or "").lower()
        in (
            "application/zip",
            "application/x-zip-compressed",
        )
    ):
        raise _kb_error(415, "kb_bundle_required")
    raw = await file.read()
    return await _process_zip_bundle(
        raw,
        bundle_name,
        request=request,
        background_tasks=background_tasks,
        user=user,
        ctx=ctx,
        scope=scope,
        scope_id=scope_id,
        use_llm=use_llm,
    )


# ─── HTTP route: server-side admin import (large file path) ───
# 0.24.7: bypass browser HTTP upload for huge zips (e.g. 119MB cross-WAN).
# Workflow:
#   scp /tmp/big.zip ubuntu@server:/var/lib/bowhead/uploads/big.zip
#   curl -X POST /api/v1/kb/admin/import -d '{"server_path":".../big.zip", ...}'
# Faster + reliable for files where browser fetch hits connection timeout.

_ADMIN_IMPORT_ALLOWED_PREFIXES = (
    "/var/lib/bowhead/uploads/",
    "/tmp/",  # convenience for ssh + quick test; admin must clean up
)


class KbAdminImportRequest(BaseModel):
    server_path: str
    scope: KbScopeIn
    scope_id: str
    use_llm: bool = False


@router.post("/admin/import", response_model=KbBundleUploadResponse, dependencies=[Depends(require_not_readonly)])
async def admin_import_bundle(
    request: Request,
    background_tasks: BackgroundTasks,
    body: KbAdminImportRequest,
    user: User = Depends(_current_user),
    _feat: None = Depends(require_feature_licensed("kb")),  # §5.1 entitlement
) -> KbBundleUploadResponse:
    """Admin-only: read .zip from server-side disk path → bundle ingest pipeline.

    Use for huge zips where browser HTTP upload times out (119MB+ across slow
    WAN). User scp's file to whitelist prefix first, then triggers ingest.

    Path whitelist + admin-only check prevents arbitrary file read.
    """
    ctx = await _make_scope_context_checked(user, request)
    if ctx.role not in ("ws_admin", "team_admin"):
        raise _kb_error(403, "workspace_admin_required")
    if body.scope in ("space", "workspace"):
        _require_workspace_context(ctx)  # Batch 1/c4.
    if not KbPermission.can_write_to_scope(body.scope, body.scope_id, ctx, ctx.role):
        raise _kb_error(403, "kb_scope_access_denied")
    await _require_kb_scope_grant(
        request,
        ctx,
        scope=body.scope,
        scope_id=body.scope_id,
        permission="write",
    )

    path = body.server_path
    # Path safety: must be within allowlisted prefix (prevent ../, /etc/, etc.).
    abs_path = str(Path(path).resolve())
    if not any(abs_path.startswith(p) for p in _ADMIN_IMPORT_ALLOWED_PREFIXES):
        raise _kb_error(403, "kb_import_path_forbidden")
    if not Path(abs_path).is_file():
        raise _kb_error(404, "kb_import_file_not_found")
    if not abs_path.lower().endswith(".zip"):
        raise _kb_error(415, "kb_bundle_required")

    with Path(abs_path).open("rb") as f:
        raw = f.read()
    bundle_name = Path(abs_path).name
    return await _process_zip_bundle(
        raw,
        bundle_name,
        request=request,
        background_tasks=background_tasks,
        user=user,
        ctx=ctx,
        scope=body.scope,
        scope_id=body.scope_id,
        use_llm=body.use_llm,
        attribution_route="kb_admin_import",
    )


# ─── list (GET /files) ────────────────────────────────


class KbFileListResponse(BaseModel):
    files: list[KbFileDTO]
    total: int  # post-filter full count (NOT page size) — kb-management REQ-1.1
    has_more: bool = False  # more rows beyond this page (offset+limit < total)
    # Opaque keyset cursor for the row after this page; None on the last page.
    next_cursor: str | None = None


_LIST_ORDER_COLUMNS = ("uploaded_at", "filename", "size_bytes")


def _list_order(order_by: str, order_dir: str) -> tuple[str, str]:
    """Normalize sort params exactly like ``KbFilesStore.list_for_scope_filtered``."""
    column = order_by if order_by in _LIST_ORDER_COLUMNS else "uploaded_at"
    direction = "asc" if str(order_dir).lower() == "asc" else "desc"
    return column, direction


def _list_sort_key(row: KbFileDTO, column: str) -> tuple[Any, str]:
    return getattr(row, column), row.file_id


def _encode_list_cursor(row: KbFileDTO, column: str, direction: str) -> str:
    payload = json.dumps({"c": column, "d": direction, "v": getattr(row, column), "id": row.file_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_list_cursor(cursor: str, column: str, direction: str) -> tuple[Any, str]:
    """Return the (sort value, file_id) key the cursor points after.

    A cursor is only valid for the sort it was issued with; anything else is a 400
    rather than a silently restarted list.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        value, file_id = payload["v"], payload["id"]
        valid = payload["c"] == column and payload["d"] == direction and isinstance(file_id, str)
        valid = valid and (isinstance(value, int) if column == "size_bytes" else isinstance(value, str))
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        raise _kb_error(400, "invalid_kb_pagination")
    return value, file_id


async def _kb_scope_listable(request: Request, ctx: ScopeContext, *, scope: str, scope_id: str) -> bool:
    """Whether a scope-level read grant lets the single-scope list of ``scope`` succeed.

    Mirrors the strict check in the single-scope branches without raising: an
    enforced denial removes that scope from the union, shadow mode keeps it.
    """
    if await _require_kb_scope_grant(request, ctx, scope=scope, scope_id=scope_id, permission="read", strict=False):
        return True
    return not resource_grants_enforce_enabled()


@router.get("/files", response_model=KbFileListResponse)
async def list_files(
    request: Request,
    scope: KbListScopeIn = "personal",
    limit: int = 200,
    offset: int = 0,
    cursor: str | None = None,
    since: str | None = None,
    file_ids: list[str] | None = Query(default=None, max_length=100),
    status: str | None = None,
    mime: str | None = None,
    doc_type_id: str | None = None,
    uploaded_after: str | None = None,
    uploaded_before: str | None = None,
    q: str | None = None,
    order_by: str = "uploaded_at",
    order_dir: str = "desc",
    user: User = Depends(_current_user),
) -> KbFileListResponse:
    """List files in scope. Returns DTOs with can_delete / can_change_scope flags.

    Sprint file-upload-presign Phase 3-followup review P0-2: accept `since` +
    `limit` to bound the response so the composer's 3s polling for bundle
    indexing progress doesn't issue unbounded full-table scans. `since` is an
    ISO8601 string compared against `uploaded_at`. `limit` clamped 1..1000.

    kb-management REQ-1.1/1.2: page-number pagination (`offset`/`limit`) +
    server-side metadata filters (`status` / `mime` / `doc_type_id` /
    `uploaded_after` / `uploaded_before` / `q` filename substring) + sort
    (`order_by` ∈ {uploaded_at,filename,size_bytes}, `order_dir` ∈ {asc,desc}).
    The resource-grant filter runs in Python after the SQL query (grants are in
    a different DB), so `total` is the post-grant-filter full count and the page
    is sliced offset/limit afterwards. `total` is NOT the page size (was a latent
    bug); `has_more` signals more rows beyond this page. All new params are
    optional → omitting them preserves prior behaviour (modulo the workspace tab
    no longer being implicitly capped at list_for_ws's 50-row default).

    ``scope=all`` lists every file the caller can read: their personal files, the
    current space's files (only with a space header, after the same membership
    check) and team files. It is by construction the union of the three
    single-scope lists (a scope whose single list would be refused contributes
    nothing), ordered globally by the sort column then ``file_id``; each item
    carries its canonical ``scope``. ``cursor`` (from ``next_cursor``) pages by
    keyset over that order and replaces ``offset``.
    """
    # personal/team/all listings do not depend on a space. Build a team-level
    # production RBAC snapshot when no space is selected; space listings keep
    # the default fail-closed workspace requirement.
    ctx = await _make_scope_context_checked(
        user,
        request,
        allow_missing_workspace=scope in ("personal", "team", "all"),
        operation=SpaceOperation.READ_HISTORY,
    )
    files_store = _get_files_store(request)
    safe_limit = max(1, min(int(limit), 1000))
    safe_offset = max(0, int(offset))
    statuses = [s.strip() for s in status.split(",") if s.strip()] if status else None
    mime_prefixes = [m.strip() for m in mime.split(",") if m.strip()] if mime else None
    sort_column, sort_direction = _list_order(order_by, order_dir)
    if cursor is not None and safe_offset:
        raise _kb_error(400, "invalid_kb_pagination")
    cursor_key = _decode_list_cursor(cursor, sort_column, sort_direction) if cursor is not None else None
    filters: dict[str, Any] = {
        "statuses": statuses,
        "mime_prefixes": mime_prefixes,
        "doc_type_id": doc_type_id,
        "uploaded_after": uploaded_after,
        "uploaded_before": uploaded_before,
        "q": q,
        "order_by": sort_column,
        "order_dir": sort_direction,
    }

    if scope == "all":
        space_id = (
            ctx.workspace_id if ctx.workspace_id and await _kb_scope_listable(request, ctx, scope="workspace", scope_id=ctx.workspace_id) else None
        )
        team_scope_id = ctx.team_id if await _kb_scope_listable(request, ctx, scope="team", scope_id=ctx.team_id) else None
        listed_scopes = ["personal", *(["space"] if space_id else []), *(["team"] if team_scope_id else [])]
        rows = await files_store.list_for_scopes_filtered(
            team_id=ctx.team_id,
            personal_owner_id=user.id,
            space_id=space_id,
            team_scope_id=team_scope_id,
            **filters,
        )
    else:
        if scope == "personal":
            scope_id = user.id
        elif scope in ("space", "workspace"):
            # Batch 1/c4: without space context, workspace-scope WHERE workspace_id="" includes team files
            # (team rows store workspace_id=''). Return explicit 400, never guess.
            _require_workspace_context(ctx)
            await _require_kb_scope_grant(
                request,
                ctx,
                scope="workspace",
                scope_id=ctx.workspace_id,
                permission="read",
            )
            scope_id = ctx.workspace_id
        else:  # team
            await _require_kb_scope_grant(
                request,
                ctx,
                scope="team",
                scope_id=ctx.team_id,
                permission="read",
            )
            scope_id = ctx.team_id
        listed_scopes = [scope]
        rows = await files_store.list_for_scope_filtered(scope, scope_id, **filters)

    if file_ids is not None:
        requested_ids = set(file_ids)
        rows = [row for row in rows if row.file_id in requested_ids]
    rows = await _filter_kb_files_by_file_grants(request, ctx, rows, scopes=listed_scopes)
    if since:
        rows = [r for r in rows if (r.uploaded_at or "") >= since]

    total = len(rows)
    if cursor_key is not None:
        # Rows are already in (sort column, file_id) order; resume strictly after the cursor.
        if sort_direction == "asc":
            start = next((i for i, r in enumerate(rows) if _list_sort_key(r, sort_column) > cursor_key), total)
        else:
            start = next((i for i, r in enumerate(rows) if _list_sort_key(r, sort_column) < cursor_key), total)
    else:
        start = safe_offset
    page = rows[start : start + safe_limit]
    has_more = start + len(page) < total
    next_cursor = _encode_list_cursor(page[-1], sort_column, sort_direction) if has_more and page else None

    enriched = await _with_uploader_names(request, ctx, [_enrich_dto(r, ctx) for r in page])
    return KbFileListResponse(files=enriched, total=total, has_more=has_more, next_cursor=next_cursor)


# ─── get single file ──────────────────────────────────


@router.get("/files/{file_id}", response_model=KbFileDTO)
async def get_file(
    file_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> KbFileDTO:
    ctx = await _make_scope_context_checked(
        user,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    files_store = _get_files_store(request)
    file = await _kb_scope_check(file_id, ctx, files_store, request)
    (named,) = await _with_uploader_names(request, ctx, [_enrich_dto(file, ctx)])
    return named


# ─── download (binary stream) ─────────────────────────


@router.get("/files/{file_id}/health", response_model=KbHealthObservation)
async def get_file_health(file_id: str, request: Request, response: Response, user: User = Depends(_current_user)) -> KbHealthObservation:
    """On-demand facts only. No list-wide probes, repair writes or eligibility changes."""
    if not health_observation_enabled():
        raise _kb_error(404, "kb_file_not_found")
    response.headers["Cache-Control"] = "no-store"
    ctx = await _make_scope_context_checked(user, request, operation=SpaceOperation.READ_HISTORY, allow_missing_workspace=True)
    files = _get_files_store(request)
    file = await _kb_scope_check(file_id, ctx, files, request)
    limiter = getattr(request.app.state, "kb_health_limiter", None)
    if limiter is None:
        limiter = request.app.state.kb_health_limiter = anyio.CapacityLimiter(4)
    try:
        limiter.acquire_nowait()
    except anyio.WouldBlock:
        raise _kb_error(429, "kb_service_unavailable") from None
    try:
        artifacts = _get_artifact_store(request)
        art = await artifacts.get(file.artifact_id)

        async def storage_health() -> bool:
            recent = await artifacts.list_recent_ready_paths(limit=20)
            return await check_storage_health(artifacts.backend, [path for _, path in recent])

        try:
            with anyio.fail_after(3):
                registry = getattr(request.app.state, "team_kb_runtime_registry", None)
                if registry is None:
                    index = _get_kb_store(request)
                else:
                    # A cold/stale team runtime is unknown. Do not construct model
                    # clients or fall back to another index namespace to diagnose it.
                    _, config_version = await request.app.state.user_store.get_team_llm_config_with_version(ctx.team_id)
                    index = registry.observation_store(ctx.team_id, config_version)
        except Exception:  # noqa: BLE001 - diagnostics must survive an unavailable index runtime
            index = None
        result = await inspect_file_health(
            file,
            source=SourceRecord(art.path, art.status) if art else None,
            backend=artifacts.backend,
            storage_health=storage_health,
            index_reader=index,
        )
        # Permissions/routing can change during remote I/O; never return stale facts.
        ctx = await _make_scope_context_checked(user, request, operation=SpaceOperation.READ_HISTORY, allow_missing_workspace=True)
        current = await _kb_scope_check(file_id, ctx, files, request)
        keys = ("artifact_id", "version", "scope", "scope_id", "workspace_id", "team_id", "parse_status", "chunks_count")
        if any(getattr(file, key) != getattr(current, key) for key in keys):
            raise _kb_error(409, "kb_health_changed")
        return result
    finally:
        limiter.release()


@router.get("/files/{file_id}/preview")
async def preview_file(
    file_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> StreamingResponse:
    """Inline preview stream — browser native viewer (PDF / md text).

    Day 15: Content-Disposition `inline` 让 browser 内嵌渲 (vs download 的 attachment).
    PDF → iframe / <object> 自渲, md → text/plain 浏览器显. DOCX V1 placeholder (V2 mammoth).
    """
    ctx = await _make_scope_context_checked(
        user,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    files_store = _get_files_store(request)
    file = await _kb_scope_check(file_id, ctx, files_store, request)

    artifact_store = _get_artifact_store(request)
    art = await artifact_store.get(file.artifact_id)
    if art is None:
        raise _kb_error(404, "kb_artifact_not_found")
    try:
        raw = await artifact_store.backend.read(art.path)
    except FileNotFoundError:
        raise _kb_error(404, "kb_artifact_not_found") from None
    except Exception:
        raise _kb_error(503, "kb_service_unavailable") from None
    ctx = await _make_scope_context_checked(user, request, operation=SpaceOperation.READ_HISTORY)
    current = await _kb_scope_check(file_id, ctx, files_store, request)
    if any(getattr(current, key) != getattr(file, key) for key in ("artifact_id", "version", "scope", "scope_id", "workspace_id", "team_id")):
        raise _kb_error(409, "kb_health_changed")

    emit_audit(
        "kb.previewed",
        {
            "file_id": file_id,
            "scope": file.scope,
            "source": "drawer",
        },
    )

    def _iter():
        yield raw

    return StreamingResponse(
        _iter(),
        media_type=file.mime_type,
        headers={"Content-Disposition": content_disposition("inline", file.filename)},
    )


@router.get("/files/{file_id}/download")
async def download_file(
    file_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> StreamingResponse:
    ctx = await _make_scope_context_checked(
        user,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    files_store = _get_files_store(request)
    file = await _kb_scope_check(file_id, ctx, files_store, request)

    artifact_store = _get_artifact_store(request)
    art = await artifact_store.get(file.artifact_id)
    if art is None:
        raise _kb_error(404, "kb_artifact_not_found")
    try:
        raw = await artifact_store.backend.read(art.path)
    except FileNotFoundError:
        raise _kb_error(404, "kb_artifact_not_found") from None
    except Exception:
        raise _kb_error(503, "kb_service_unavailable") from None
    ctx = await _make_scope_context_checked(user, request, operation=SpaceOperation.READ_HISTORY)
    current = await _kb_scope_check(file_id, ctx, files_store, request)
    if any(getattr(current, key) != getattr(file, key) for key in ("artifact_id", "version", "scope", "scope_id", "workspace_id", "team_id")):
        raise _kb_error(409, "kb_health_changed")

    emit_audit(
        "kb.downloaded",
        {
            "file_id": file_id,
            "scope": file.scope,
            "size_bytes": file.size_bytes,
        },
    )

    def _iter():
        yield raw

    return StreamingResponse(
        _iter(),
        media_type=file.mime_type,
        headers={"Content-Disposition": content_disposition("attachment", file.filename)},
    )


# ─── delete (soft) ────────────────────────────────────


async def _delete_file_core(
    file_id: str,
    ctx: Any,
    request: Request,
    user: User,
    *,
    action=None,
) -> None:
    """Per-file delete (scope-check + perms + soft-delete + cascade + audit).

    Shared by the single DELETE endpoint and bulk_delete. Raises HTTPException
    on a permission/not-found failure so bulk can capture it per item.
    kb-management REQ-1.4a.
    """
    files_store = _get_files_store(request)
    file = await _kb_scope_check(file_id, ctx, files_store, request, include_deleted=True)

    if not KbPermission.can_delete(file, ctx, ctx.role):
        raise _kb_error(403, "kb_file_access_denied")
    await _require_kb_scope_grant(
        request,
        ctx,
        scope=file.scope,
        scope_id=file.scope_id,
        permission="write",
    )
    await _require_kb_file_grant(
        request,
        ctx,
        file,
        permission="write",
    )

    # P0-B: soft delete chunks (chromadb metadata.is_active=False) + SQL row soft_deleted_at
    kb_store = await _get_effective_kb_store(request, user)
    from bowhead_core.kb.ingestion import KnowledgeAction

    action = action or KnowledgeAction(f"remove:{file_id}", "kb_ui", user.id, ctx.team_id, ctx.workspace_id)
    await files_store.remove_admitted(file_id, action=action)
    await kb_store.soft_delete_file(file)
    # kb-knowledge-tables sprint v1.0: cascade soft-delete records + decrement doc_type.record_count
    record_store = getattr(request.app.state, "kb_extracted_record_store", None)
    dt_store = getattr(request.app.state, "kb_doc_type_store", None)
    if record_store is not None and file.doc_type_id:
        cascaded = await record_store.soft_delete_for_file(file_id)
        if cascaded > 0 and dt_store is not None:
            await dt_store.update_record_count(file.doc_type_id, -cascaded)
    # Sprint 25 kb-per-row-extract P0-ENG-4: cascade soft-delete row-level records
    # (cardinality=many doc_types). Without this, deleted file's rows remain
    # visible in chat kb_query_records aggregate → privacy/correctness bug.
    rows_store = getattr(request.app.state, "kb_extracted_rows_store", None)
    dt_id_for_rows = file.auto_classified_doc_type_id or file.doc_type_id
    if rows_store is not None:
        # kb-matrix M4 gate④ (decision 12): **graduated snapshots exempt from deletion cascade**; snapshots freeze
        # a point in time. File deletion must not rewrite history (same as matrix FR-7: keep rows, purge excerpts
        # at 90-day hard deletion), or chat aggregation shrinks while coverage denominator shrinks too, hiding lost rows.
        # Failed exemption computation falls back to old full-cascade behavior, without blocking deletion.
        frozen_ids: list[str] = []
        exemption_ok = True
        if dt_store is not None:
            try:
                from bowhead_core.kb.doc_type_store import is_frozen_snapshot as _is_frozen

                for _row_dt_id in await rows_store.list_doc_type_ids_for_file(file_id):
                    _row_dt = await dt_store.get(_row_dt_id)
                    if _row_dt is not None and _is_frozen(_row_dt):
                        frozen_ids.append(_row_dt_id)
            except Exception:
                # wave-1 security review P2-3: failed exemption computation must **fail closed**, skipping
                # the entire rows cascade (snapshot immutability outranks immediate cascade; ordinary rows for this file
                # can be cleaned by later deletion/sweep). Never delete snapshot rows with an unknown exemption set,
                # the exact silent row-loss/coverage-masking that gate④ prevents.
                logger.warning("kb.delete_frozen_exemption_failed file=%s — skip rows cascade (fail-closed)", file_id, exc_info=True)
                exemption_ok = False
        if exemption_ok:
            rows_cascaded = await rows_store.soft_delete_for_file(file_id, exclude_doc_type_ids=frozen_ids)
            if rows_cascaded > 0 and dt_store is not None and dt_id_for_rows and dt_id_for_rows not in frozen_ids:
                # row_count cache decrement (parallel to record_count)
                await dt_store.update_row_count(dt_id_for_rows, -rows_cascaded)
    emit_audit("kb.deleted", {"file_id": file_id, "scope": file.scope})


@router.delete("/files/{file_id}", status_code=204, dependencies=[Depends(require_not_readonly)])
async def delete_file(
    file_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> None:
    ctx = await _make_scope_context_checked(user, request)
    await _delete_file_core(file_id, ctx, request, user)


# ─── file-management-v2 P2.5: promote existing artifact to KB file ───
# KB route placement rather than /files is intentional: the **complete KB-write authorization chain**
# （scope context → workspace guard → can_write_to_scope → scope grant →
# entitlement) lives here. /files would require cross-module imports of private helpers or duplicate authorization;
# duplicated truth eventually drifts into privilege escalation.


class PromoteArtifactRequest(BaseModel):
    """`artifact_id` 指向一份**已存在**的文件（零拷贝晋升）。"""

    artifact_id: str
    scope: KbScopeIn = "personal"
    knowledge_intent: Literal["add_to_knowledge"]
    action_id: str = Field(min_length=1, max_length=128)


@router.post(
    "/files/from-artifact",
    response_model=KbFileDTO,
    status_code=201,
    dependencies=[Depends(require_not_readonly)],
)
async def promote_artifact(
    request: Request,
    body: PromoteArtifactRequest,
    user: User = Depends(_current_user),
    _feat: None = Depends(require_feature_licensed("kb")),
) -> KbFileDTO:
    """把一份已有的 artifact 加入知识库——**复用同一个 blob**，不产生第二份字节。

    授权与 `upload_file` 完全同链：调用方必须既拥有这份 artifact，又有权写入
    目标 scope。`scope_id` **由服务端按 scope 解析**，不接受客户端传入
    （否则可以拿别的 workspace/team id 越权写）。
    """
    from bowhead_core.kb.ingestion import KnowledgeAction

    action = KnowledgeAction(body.action_id, "kb_ui", user.id, user.team_id, _make_scope_context(user, request).workspace_id)
    return await _promote_with_action(request, body=body, user=user, action=action)


async def _promote_with_action(request: Request, *, body: PromoteArtifactRequest, user: User, action) -> KbFileDTO:
    from bowhead_core.kb.ingestion import KnowledgeWriteError

    try:
        action.require(user_id=user.id, team_id=user.team_id)
    except KnowledgeWriteError as exc:
        raise _kb_error(503, exc.code, hint="Use Files to keep the original file; ask an administrator to enable knowledge ingestion.") from exc
    artifact_store = _get_artifact_store(request)
    art = await artifact_store.get(body.artifact_id)
    if art is None:
        raise _kb_error(404, "kb_artifact_not_found")
    if art.created_by != user.id:
        # Someone else's file, or an owner-less one (nobody may promote it), answers
        # exactly like a missing one (no existence probe).
        reason = "not_owner" if art.created_by else "owner_less"
        logger.info("kb.promote_concealed artifact=%s user=%s reason=%s", body.artifact_id, user.id, reason)
        raise _kb_error(404, "kb_artifact_not_found")

    # ⚠ allow_missing_workspace=True: **main caller is /files**, a personal asset library with no space
    # context and no X-Space-Id header. Strict mode gives even scope=personal (scope_id=user.id, requiring
    # no space) 400 workspace_required. After 0.87.0 release, all file-page Add-to-KB calls failed,
    # caught by prod dogfood. The space/workspace branch below still has a hard _require_workspace_context
    # gate; supplied headers still trigger build_access_context's membership check,
    # so the defense is not relaxed.
    ctx = await _make_scope_context_checked(user, request, allow_missing_workspace=True)
    from bowhead_gateway.routes.files import _require_artifact_visible

    await _require_artifact_visible(request, artifact_store, art, operation=SpaceOperation.READ_HISTORY, user_id=user.id)
    scope = body.scope
    # Resolve scope_id server-side (same as list endpoint 1194-1215).
    if scope == "personal":
        scope_id = user.id
    elif scope in ("space", "workspace"):
        _require_workspace_context(ctx)  # Batch 1/c4: prevent scope_id=""+ws="" empty-to-empty writes.
        scope_id = ctx.workspace_id
    else:
        scope_id = ctx.team_id

    if not KbPermission.can_write_to_scope(scope, scope_id, ctx, ctx.role):
        raise _kb_error(403, "kb_scope_access_denied")
    await _require_kb_scope_grant(request, ctx, scope=scope, scope_id=scope_id, permission="write")

    files_store = _get_files_store(request)
    from bowhead_gateway.kb_promotion import PromotionSkipped, promote_artifact_to_kb

    try:
        file_id = await promote_artifact_to_kb(
            artifact=art,
            files_store=files_store,
            scope=scope,
            scope_id=scope_id,
            # Sixth and final manual-copy consolidation: consumer is the **kb_files row** (promotion creates no artifact),
            # so use scope_workspace_id (original empty string = frozen team/no-space semantics),
            # **never** artifact_workspace_id; feeding sentinel to kb_files changes scope filtering.
            workspace_id=decide_for_kb_scope(scope, ctx.workspace_id).scope_workspace_id,
            team_id=ctx.team_id,
            user_id=user.id,
            trigger="manual",
            action=action,
            worker=await _get_effective_worker(request, user),
        )
    except KnowledgeWriteError as exc:
        raise _kb_error(
            409, exc.code, hint="Check the file's current knowledge membership and destination; use a new explicit action after removal."
        ) from exc
    except PromotionSkipped as e:
        # Unsupported by rules (unparseable MIME etc.), not a failure: 422 for readable frontend messaging.
        raise _kb_error(422, "kb_promotion_not_supported") from e

    emit_audit("kb.promoted_from_artifact", {"file_id": file_id, "artifact_id": art.id, "scope": scope})
    dto = await files_store.get(file_id)
    if dto is None:
        raise _kb_error(500, "kb_file_write_failed")
    return _enrich_dto(dto, ctx)


@router.delete(
    "/files/by-artifact/{artifact_id}",
    status_code=204,
    dependencies=[Depends(require_not_readonly)],
)
async def remove_artifact_from_kb(
    artifact_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> None:
    """按 artifact 反查并移出知识库——**文件本身不动**，只是 AI 不再自动参考。

    走 `_delete_file_core` 而不是直接 soft_delete：那里带完整的 scope 权限判定
    （space/team 文件不是谁上传谁就能移出）+ chroma 清理 + 审计。
    """
    # Like promote: /files sends space header when current space exists, allowing ordinary ACL-based removal for space scope;
    # without current space, personal scope still works. Shared scope without context fails closed in
    # _delete_file_core; never infer scope_id from empty-to-empty comparisons.
    # The caller context is built before the lookup so header errors cannot reveal the id.
    ctx = await _make_scope_context_checked(user, request, allow_missing_workspace=True)
    files_store = _get_files_store(request)
    file_id = await files_store.find_active_by_artifact(artifact_id)
    if file_id is None:
        raise _kb_error(404, "kb_file_not_in_knowledge_base")
    try:
        await _delete_file_core(file_id, ctx, request, user)
    except HTTPException as exc:
        # A KB entry the caller cannot read must look like "not in the knowledge base".
        if exc.status_code != 404:
            raise
        raise _kb_error(404, "kb_file_not_in_knowledge_base") from None


# ─── PATCH /scope (P0-O2 + P0-P2 + P1-N4) ─────────────


async def _change_file_scope_core(
    file_id: str,
    body: KbFileScopePatchRequest,
    ctx: Any,
    request: Request,
    user: User,
) -> KbFileDTO:
    """Per-file scope change (perms + SQL update + chromadb migration + audit).

    Shared by the single PATCH endpoint and bulk_scope. Raises HTTPException on
    failure so bulk can capture it per item. kb-management REQ-1.4a.
    """
    files_store = _get_files_store(request)
    file = await _kb_scope_check(file_id, ctx, files_store, request)

    if not KbPermission.can_change_scope(file, ctx, body.new_scope, body.new_scope_id):
        raise _kb_error(403, "kb_file_access_denied")
    await _require_kb_scope_grant(
        request,
        ctx,
        scope=file.scope,
        scope_id=file.scope_id,
        permission="write",
    )
    await _require_kb_file_grant(
        request,
        ctx,
        file,
        permission="write",
    )
    await _require_kb_scope_grant(
        request,
        ctx,
        scope=body.new_scope,
        scope_id=body.new_scope_id,
        permission="write",
    )

    # P1-N4: admin override requires reason ≥10 characters.
    is_admin_override = ctx.user_id != KbPermission.owner_of(file)
    if is_admin_override and (not body.reason or len(body.reason) < 10):
        raise _kb_error(400, "kb_scope_change_reason_required")

    # Compute new routing keys.
    old_scope = file.scope
    old_scope_id = file.scope_id
    old_workspace_id = file.workspace_id
    old_team_id = file.team_id

    new_workspace_id = body.new_scope_id if body.new_scope in ("space", "workspace") else (old_workspace_id if body.new_scope == "personal" else "")
    new_team_id = body.new_scope_id if body.new_scope == "team" else old_team_id

    # SQL update
    await files_store.update_scope(
        file_id,
        body.new_scope,
        body.new_scope_id,
        workspace_id=new_workspace_id,
        team_id=new_team_id,
    )

    # chromadb cross-coll migration (P0-O2 + P0-P2)
    kb_store = await _get_effective_kb_store(request, user)
    try:
        await kb_store.update_chunks_scope(
            file_id=file_id,
            old_scope=old_scope,
            old_scope_id=old_scope_id,
            old_workspace_id=old_workspace_id,
            old_team_id=old_team_id,
            new_scope=body.new_scope,
            new_scope_id=body.new_scope_id,
            new_workspace_id=new_workspace_id,
            new_team_id=new_team_id,
        )
    except Exception as exc:
        # SQL changed first; any chunk-migration failure (reading old collection, updating metadata in place, vector-store outage)
        # restores old SQL scope (store already tried restoring chunks), returning structured error instead of raw 500.
        # Entire file remains in old scope; no half-migrated new-SQL/old-chunks state.
        stage = exc.stage if isinstance(exc, ScopeChangeMigrationError) else "vector_store"
        logger.exception("scope change migration failed for file_id=%s stage=%s", file_id, stage)
        try:
            await files_store.update_scope(
                file_id,
                old_scope,
                old_scope_id,
                workspace_id=old_workspace_id,
                team_id=old_team_id,
            )
        except Exception:
            logger.exception("scope change SQL rollback failed for file_id=%s", file_id)
            emit_audit("kb.scope_change_rollback_failed", {"file_id": file_id, "stage": stage})
        raise _kb_error(500, "kb_scope_change_failed") from exc

    # P1-Q2: refresh chunks_count after cross-collection move.
    refreshed_file = await files_store.get(file_id)
    if refreshed_file is None:
        raise _kb_error(500, "kb_scope_change_failed")
    new_actual = await kb_store.count_chunks(refreshed_file)
    await files_store.update_chunks_count(file_id, new_actual)

    emit_audit(
        "kb.scope_changed",
        {
            "file_id": file_id,
            "old_scope": old_scope,
            "old_scope_id": old_scope_id,
            "new_scope": body.new_scope,
            "new_scope_id": body.new_scope_id,
            "changed_by": user.id,
            "changed_by_role": ctx.role,
            "original_uploader": file.uploaded_by,
            "is_admin_override": is_admin_override,
            "reason": body.reason,
        },
    )

    final = await files_store.get(file_id)
    if final is None:
        raise _kb_error(500, "kb_scope_change_failed")
    return _enrich_dto(final, ctx)


@router.patch("/files/{file_id}/scope", response_model=KbFileDTO, dependencies=[Depends(require_not_readonly)])
async def patch_file_scope(
    file_id: str,
    body: KbFileScopePatchRequest,
    request: Request,
    user: User = Depends(_current_user),
) -> KbFileDTO:
    """Scope change with cross-collection physical migration if needed."""
    ctx = await _make_scope_context_checked(user, request)
    return await _change_file_scope_core(file_id, body, ctx, request, user)


# ─── bulk file ops (kb-management REQ-1.4) ────────────


_BULK_MAX = 500


class KbBulkItemResult(BaseModel):
    file_id: str
    ok: bool
    # Deprecated compatibility field. Never place exception text here: this is
    # a HTTP 200 partial-result envelope, so the global error handler cannot
    # localize or sanitize it.
    error: str | None = None
    error_code: str | None = None


class KbBulkResponse(BaseModel):
    results: list[KbBulkItemResult]
    succeeded: int
    failed: int


class KbBulkDeleteRequest(BaseModel):
    file_ids: list[str]


class KbBulkScopeRequest(BaseModel):
    file_ids: list[str]
    new_scope: KbScopeIn
    new_scope_id: str
    reason: str | None = None


def _dedupe_ids(file_ids: list[str]) -> list[str]:
    """Order-preserving dedupe + validation for bulk file_ids."""
    seen: set[str] = set()
    out: list[str] = []
    for fid in file_ids:
        if fid and fid not in seen:
            seen.add(fid)
            out.append(fid)
    if not out:
        raise _kb_error(400, "invalid_kb_bulk_request")
    if len(out) > _BULK_MAX:
        raise _kb_error(400, "invalid_kb_bulk_request")
    return out


async def _run_bulk(
    file_ids: list[str],
    per_item: Callable[[str], Awaitable[None]],
) -> KbBulkResponse:
    """Run `per_item` over each id, capturing per-item success/failure.

    Partial-failure semantics: one item's HTTPException (perm/not-found) or
    unexpected error becomes `ok=false` and does NOT abort the rest.
    """
    results: list[KbBulkItemResult] = []
    for fid in file_ids:
        try:
            await per_item(fid)
            results.append(KbBulkItemResult(file_id=fid, ok=True))
        except HTTPException as exc:
            detail = exc.detail
            code: str | None = None
            if isinstance(detail, dict):
                error = detail.get("error")
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    code = error["code"]
                elif isinstance(detail.get("code"), str):
                    code = detail["code"]
            results.append(
                KbBulkItemResult(
                    file_id=fid,
                    ok=False,
                    error_code=code or "kb_file_operation_failed",
                )
            )
        except Exception:
            logger.exception("bulk op failed for file_id=%s", fid)
            results.append(
                KbBulkItemResult(
                    file_id=fid,
                    ok=False,
                    error_code="kb_file_operation_failed",
                )
            )
    succeeded = sum(1 for r in results if r.ok)
    return KbBulkResponse(results=results, succeeded=succeeded, failed=len(results) - succeeded)


@router.post("/files/bulk_delete", response_model=KbBulkResponse, dependencies=[Depends(require_not_readonly)])
async def bulk_delete_files(
    body: KbBulkDeleteRequest,
    request: Request,
    user: User = Depends(_current_user),
) -> KbBulkResponse:
    ctx = await _make_scope_context_checked(user, request)
    ids = _dedupe_ids(body.file_ids)
    return await _run_bulk(ids, lambda fid: _delete_file_core(fid, ctx, request, user))


@router.post("/files/bulk_scope", response_model=KbBulkResponse, dependencies=[Depends(require_not_readonly)])
async def bulk_change_scope(
    body: KbBulkScopeRequest,
    request: Request,
    user: User = Depends(_current_user),
) -> KbBulkResponse:
    ctx = await _make_scope_context_checked(user, request)
    ids = _dedupe_ids(body.file_ids)
    patch = KbFileScopePatchRequest(
        new_scope=body.new_scope,
        new_scope_id=body.new_scope_id,
        reason=body.reason,
    )

    async def _one(fid: str) -> None:
        await _change_file_scope_core(fid, patch, ctx, request, user)

    return await _run_bulk(ids, _one)


class KbDocTypePatchRequest(BaseModel):
    doc_type_id: str | None = None  # None unbinds


class KbBulkDocTypeRequest(BaseModel):
    file_ids: list[str]
    doc_type_id: str | None = None


async def _maybe_autoextract_on_bind(
    file: KbFileDTO,
    request: Request,
    user: User,
    *,
    force: bool,
) -> None:
    """[kb-management D5] Fire rate-limited extraction after a doc_type bind.

    Best-effort: the bind already committed, so any failure here is swallowed.
    Skips when the per-row subsystem isn't configured. Reuses the shared
    rate-limited trigger so bulk binds can't bypass the per-ws daily cap.
    """
    s = request.app.state
    rows_store = getattr(s, "kb_extracted_rows_store", None)
    jobs_store = getattr(s, "kb_per_row_jobs_store", None)
    if rows_store is None or jobs_store is None:
        return
    try:
        worker = await _get_effective_worker(request, user)
    except HTTPException:
        return
    try:
        await trigger_row_extraction(
            file=file,
            ws_id=file.workspace_id,
            force=force,
            rows_store=rows_store,
            jobs_store=jobs_store,
            user_store=getattr(s, "user_store", None),
            worker=worker,
            active_tasks=getattr(s, "kb_per_row_tasks_by_file_id", None),
            actor_user_id=user.id,
            dt_store=getattr(s, "kb_doc_type_store", None),
        )
    except Exception:
        logger.exception("doc_type auto-extract failed for file_id=%s", file.file_id)


async def _bind_doc_type_core(
    file_id: str,
    doc_type_id: str | None,
    ctx: Any,
    request: Request,
    user: User,
) -> KbFileDTO:
    """Per-file doc_type binding (set/unset the authoritative doc_type_id).

    kb-management REQ-1.4b + D5: binding sets doc_type_id and then (when binding,
    not unbinding) triggers extraction via the shared rate-limited service —
    matching the upload-time "assign type → records appear" model. Shared by the
    single PATCH endpoint and bulk_doc_type.
    """
    files_store = _get_files_store(request)
    file = await _kb_scope_check(file_id, ctx, files_store, request)

    if not KbPermission.can_delete(file, ctx, ctx.role):
        raise _kb_error(403, "kb_file_access_denied")
    await _require_kb_scope_grant(
        request,
        ctx,
        scope=file.scope,
        scope_id=file.scope_id,
        permission="write",
    )
    await _require_kb_file_grant(request, ctx, file, permission="write")

    # doc_type must belong to the caller's current workspace (cross-ws leak guard).
    # S5 (kb-security-quickfix): check against ctx.workspace_id — NOT file.workspace_id —
    # to match the upload path (kb.py upload_file / kb_presign._validate_doc_type both use
    # ctx.workspace_id). team-scope files store workspace_id="" so file.workspace_id would
    # never equal a real doc_type ws → team-scope files could be bound at upload but never
    # re-bound afterwards (a permanent 403 over-block). The caller is already verified as a
    # member of ctx.workspace_id, so a doc_type in that ws is in-scope to bind.
    if doc_type_id:
        dt_store = getattr(request.app.state, "kb_doc_type_store", None)
        if dt_store is None:
            raise _kb_error(503, "kb_doc_type_service_unavailable")
        dt = await dt_store.get(doc_type_id)
        if dt is None:
            raise _kb_error(404, "doc_type_not_found")
        if dt.workspace_id != ctx.workspace_id:
            raise _kb_error(404, "doc_type_not_found")
        # kb-matrix M4 gate① (decision 12): graduated snapshots/column-group templates are not binding targets.
        # Extraction after binding writes phantom records (template-record tab shows data split from rows).
        from bowhead_core.kb.doc_type_store import is_column_group_template as _is_tpl

        if _is_tpl(dt):
            raise _kb_error(422, "doc_type_not_bindable")

    old_dt = file.doc_type_id
    updated = await files_store.update_doc_type(
        file_id,
        doc_type_id,
        expected_workspace_id=file.workspace_id,
    )
    if not updated:
        raise _kb_error(404, "kb_file_not_found")
    emit_audit(
        "kb.doc_type_bound",
        {
            "file_id": file_id,
            "doc_type_id": doc_type_id,
            "changed_by": user.id,
        },
    )
    final = await files_store.get(file_id)
    if final is None:
        raise _kb_error(500, "kb_file_write_failed")
    # kb-management D5: binding (not unbind) defaults to triggering extraction —
    # async + rate-limited via the shared service; idempotent unless the type
    # changed (force). Best-effort: the bind already committed, extraction status
    # surfaces via the list pill; skips silently if the per-row subsystem is off.
    if doc_type_id:
        await _maybe_autoextract_on_bind(final, request, user, force=old_dt != doc_type_id)
    return _enrich_dto(final, ctx)


@router.patch("/files/{file_id}/doc_type", response_model=KbFileDTO, dependencies=[Depends(require_not_readonly)])
async def patch_file_doc_type(
    file_id: str,
    body: KbDocTypePatchRequest,
    request: Request,
    user: User = Depends(_current_user),
) -> KbFileDTO:
    ctx = await _make_scope_context_checked(user, request)
    return await _bind_doc_type_core(file_id, body.doc_type_id, ctx, request, user)


@router.post("/files/bulk_doc_type", response_model=KbBulkResponse, dependencies=[Depends(require_not_readonly)])
async def bulk_bind_doc_type(
    body: KbBulkDocTypeRequest,
    request: Request,
    user: User = Depends(_current_user),
) -> KbBulkResponse:
    ctx = await _make_scope_context_checked(user, request)
    ids = _dedupe_ids(body.file_ids)

    async def _one(fid: str) -> None:
        await _bind_doc_type_core(fid, body.doc_type_id, ctx, request, user)

    return await _run_bulk(ids, _one)


# ─── reparse (retry failed / stuck parse) — kb-management REQ-2.1 + KB UX FR1 ──


async def _reparse_failed(
    file_id: str,
    files_store: Any,
    request: Request,
    user: User,
    background_tasks: BackgroundTasks,
    *,
    expected_file: KbFileDTO | None = None,
) -> bool:
    """Flip a *failed* or *stuck* file back to queued + re-enqueue the worker.

    Race-safe: the store flip is one guarded UPDATE that also refreshes the
    heartbeat, so of two concurrent clicks only one wins (FR1 / AC03); the
    worker's own claim additionally refuses a file that is already running.
    Rolls back to 'failed' if the worker turns out to be unavailable. Returns
    True iff a reparse was actually kicked off. Shared by the presign-confirm
    retry path and POST /kb/files/{id}/reparse so the two never diverge.
    """
    flipped = await files_store.requeue_for_reparse(file_id, expected_file=expected_file)
    if not flipped:
        return False
    try:
        worker = await _get_effective_worker(request, user)
    except HTTPException:
        # Worker unavailable → undo the flip so a later retry can succeed.
        await files_store.mark_parse_failed(file_id, f"{PARSE_ERROR_SERVICE_UNAVAILABLE}: kb worker unavailable")
        raise
    background_tasks.add_task(worker.parse_and_index, file_id)
    return True


@router.post(
    "/files/{file_id}/reparse", response_model=KbFileDTO, dependencies=[Depends(require_not_readonly), Depends(require_feature_licensed("kb"))]
)
async def reparse_file(
    file_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(_current_user),
) -> KbFileDTO:
    """Retry parsing a file whose parse failed or is stuck (``parse_stuck``).

    Requires write perm (same gate as delete). Only acts when parse_status is
    'failed' or the file is stuck in progress past the heartbeat threshold; any
    other state is idempotent and returns the current DTO, so a double-click
    or a concurrent worker pickup never 500s.
    """
    ctx = await _make_scope_context_checked(
        user,
        request,
        operation=SpaceOperation.EXECUTE,
    )
    files_store = _get_files_store(request)
    file = await _kb_scope_check(file_id, ctx, files_store, request)

    if not KbPermission.can_delete(file, ctx, ctx.role):
        raise _kb_error(403, "kb_file_access_denied")
    await _require_kb_scope_grant(
        request,
        ctx,
        scope=file.scope,
        scope_id=file.scope_id,
        permission="write",
    )
    await _require_kb_file_grant(request, ctx, file, permission="write")

    expected_file = None
    if file.parse_status == "failed" and (file.parse_error or "").startswith("source_missing:"):
        artifacts = _get_artifact_store(request)
        artifact = await artifacts.get(file.artifact_id)
        state = await probe_blob(artifacts.backend, artifact.path) if artifact is not None else "absent"
        # A storage probe can take remote I/O time. Recheck both membership and
        # resource grants before changing the artifact or scheduling work.
        ctx = await _make_scope_context_checked(user, request, operation=SpaceOperation.EXECUTE)
        current = await _kb_scope_check(file_id, ctx, files_store, request)
        if not KbPermission.can_delete(current, ctx, ctx.role):
            raise _kb_error(403, "kb_file_access_denied")
        await _require_kb_scope_grant(request, ctx, scope=current.scope, scope_id=current.scope_id, permission="write")
        await _require_kb_file_grant(request, ctx, current, permission="write")
        if current != file:
            raise _kb_error(409, "kb_health_changed")
        if state == "unknown":
            raise _kb_error(503, "kb_service_unavailable")
        if state == "absent":
            raise _kb_error(409, "kb_source_missing")
        if artifact is not None and artifact.status == artifacts.MISSING_STATUS:
            await artifacts.set_blob_missing([artifact.id], missing=False)
        expected_file = file

    await _reparse_failed(file_id, files_store, request, user, background_tasks, expected_file=expected_file)
    refreshed = await files_store.get(file_id)
    if refreshed is None:
        raise _kb_error(500, "kb_file_write_failed")
    return _enrich_dto(refreshed, ctx)


@router.post("/files/{file_id}/verify", response_model=KbHealthObservation)
async def verify_file_source(file_id: str, request: Request, response: Response, user: User = Depends(_current_user)) -> KbHealthObservation:
    """Explicit full binding check. Ordinary list/health reads remain observation-only."""
    from bowhead_core.kb.eligibility import KbUseEligibility
    from bowhead_core.kb.generations import use_guard_enabled

    from bowhead_gateway.kb_authorization import kb_file_authorizer

    if not use_guard_enabled():
        raise _kb_error(404, "kb_file_not_found")
    observed = await get_file_health(file_id, request, response, user)
    ctx = await _make_scope_context_checked(user, request, operation=SpaceOperation.READ_HISTORY, allow_missing_workspace=True)
    files = _get_files_store(request)
    await _kb_scope_check(file_id, ctx, files, request)
    retriever = await _get_effective_retriever(request, user)
    authorizer = kb_file_authorizer(request.app.state.user_store, getattr(request.app.state, "access_control_store", None))

    async def authorize(file: KbFileDTO) -> bool:
        return await authorizer(file, ctx.user_id, ctx.team_id, ctx.workspace_id)

    check = await KbUseEligibility(files, _get_artifact_store(request), retriever._store).check(file_id, ctx, authorize)
    ctx = await _make_scope_context_checked(user, request, operation=SpaceOperation.READ_HISTORY, allow_missing_workspace=True)
    current = await _kb_scope_check(file_id, ctx, files, request)
    can_manage = False
    try:
        await require_not_readonly(request)
        await _require_source_write(file_id, request, user)
        can_manage = True
    except HTTPException:
        pass
    from bowhead_core.kb.maintenance import KbMaintenanceStore

    await KbMaintenanceStore(files._db_path).observe(current, check, index_version=retriever._store.index_version)
    action = "check"
    if can_manage and check.reason in {"source_missing", "source_changed"}:
        action = "restore"
    elif (
        can_manage
        and current.parse_status in {"done", "failed"}
        and observed.source_health == "present"
        and check.reason in {"generation_unverified", "index_unavailable"}
    ):
        action = "rebuild"
    return observed.model_copy(
        update={
            "file_version": current.version,
            "checked_at": check.checked_at,
            "binding_state": "verified" if check.allowed else "unverified",
            "eligibility_enforced": True,
            "use_state": check.state,
            "use_reason": check.reason,
            "generation_id": check.generation_id,
            "primary_action": action,
        }
    )


async def _require_source_write(file_id: str, request: Request, user: User) -> tuple[ScopeContext, KbFileDTO]:
    ctx = await _make_scope_context_checked(user, request, operation=SpaceOperation.EXECUTE)
    file = await _kb_scope_check(file_id, ctx, _get_files_store(request), request)
    if not KbPermission.can_delete(file, ctx, ctx.role):
        raise _kb_error(403, "kb_file_access_denied")
    await _require_kb_scope_grant(request, ctx, scope=file.scope, scope_id=file.scope_id, permission="write")
    await _require_kb_file_grant(request, ctx, file, permission="write")
    return ctx, file


@router.post("/files/{file_id}/restore-source", dependencies=[Depends(require_not_readonly), Depends(require_feature_licensed("kb"))])
async def restore_file_source(file_id: str, request: Request, upload: UploadFile = File(...), user: User = Depends(_current_user)) -> dict[str, str]:
    from hashlib import sha256

    from bowhead_core.kb.generations import KbGenerationStore, use_guard_enabled

    if not use_guard_enabled():
        raise _kb_error(404, "kb_file_not_found")
    ctx, file = await _require_source_write(file_id, request, user)
    generations = KbGenerationStore(_get_files_store(request)._db_path)
    generation = await generations.get(file.active_generation_id)
    if not generation or generation["state"] != "published":
        raise _kb_error(409, "kb_generation_unverified")
    if upload.size is not None and upload.size > _SINGLE_MAX_SIZE:
        raise _kb_error(413, "kb_file_too_large")
    raw = await upload.read(_SINGLE_MAX_SIZE + 1)
    if len(raw) > _SINGLE_MAX_SIZE:
        raise _kb_error(413, "kb_file_too_large")
    if sha256(raw).hexdigest() != generation["source_sha256"]:
        return {"outcome": "different_content_requires_new_file"}
    ctx, current = await _require_source_write(file_id, request, user)
    if current.active_generation_id != file.active_generation_id or current.version != file.version:
        raise _kb_error(409, "kb_health_changed")
    attribution = decide_for_kb_scope(file.scope, file.workspace_id)
    artifact = await _get_artifact_store(request).put_bytes(
        content=raw,
        filename=file.filename,
        mime=file.mime_type,
        source="kb_upload",
        kind="user_upload",
        producer_kind="kb_upload",
        created_by=user.id,
        workspace_id=attribution.artifact_workspace_id,
    )
    # The immutable upload and the file pointer are separate commits. A loser
    # leaves an unreferenced artifact for ordinary artifact retention, never overwrites.
    await _require_source_write(file_id, request, user)
    if not await generations.restore_source(current, artifact.id):
        raise _kb_error(409, "kb_health_changed")
    return {"outcome": "same_content_restored"}


@router.post(
    "/files/{file_id}/rebuild", response_model=KbFileDTO, dependencies=[Depends(require_not_readonly), Depends(require_feature_licensed("kb"))]
)
async def rebuild_file_source(file_id: str, request: Request, user: User = Depends(_current_user)) -> KbFileDTO:
    # Single-file repair uses the same durable, reauthorized executor. No generic
    # parse recovery may silently resume paid work after a restart.
    await create_rebuild_batch(
        KbRebuildBatchRequest(file_ids=[file_id], apply=True, accept_model_cost=True, max_bytes=500 * 1024 * 1024), request, user
    )
    ctx, file = await _require_source_write(file_id, request, user)
    return _enrich_dto(file, ctx)


# ─── search (4-stage retrieval pipeline) ──────────────

# Page deep-search relevance floor (KB experience cleanup PRD FR6.3), this route only, not chat kb_search.
# Compare raw cross-encoder rerank_score (no role / lexical bonuses). Calibrate per model;
# no floor for models absent from the table.
# gte-rerank-v2 (2026-09-25, 116 real path, three rounds, 21 simulated business docs, 124 answerable / 82 unanswerable queries;
# third round independently authored holdout): highest wholly unrelated scores 0.06–0.124; answerable correct-document top scores
# ≥0.121. At 0.15: false removal 2/124 answerable; wholly unrelated 100% no hits; same-domain unanswerable 68% no hits;
# unrelated returned files for answerable queries 75%→20%. Larger corpora slightly raise unrelated scores (15→25 files, 0.119→0.124).
# Artificial chunks on 09-24 gave unrelated scores always 0.006, which does not hold on realistically shaped corpora.
# qwen3-rerank's unrelated/relevant scores overlap; any absolute threshold removes true results, so no floor.
# Rerank fallback (timeout/non-200) has no relevance score, so no floor applies.
KB_PAGE_SEARCH_MIN_RERANK_SCORE_BY_MODEL: dict[str, float] = {"gte-rerank-v2": 0.15}


def _apply_page_search_relevance_floor(result: KbSearchResult) -> KbSearchResult:
    floor = KB_PAGE_SEARCH_MIN_RERANK_SCORE_BY_MODEL.get(result.rerank_model or "")
    if floor is None:
        return result
    kept = [c for c in result.chunks if c.rerank_score is None or c.rerank_score >= floor]
    if len(kept) == len(result.chunks):
        return result
    return result.model_copy(update={"chunks": kept})


@router.post("/search", response_model=KbSearchResult)
async def search(
    body: KbSearchRequest,
    request: Request,
    user: User = Depends(_current_user),
) -> KbSearchResult:
    """KB search via 4-stage pipeline: embed → chromadb → rerank → post-process.

    ``scope_filter``: ``["personal"]`` / ``["space"]`` / ``["team"]``（可组合；缺省 = 三档全查）。
    检索范围与对话 kb_search 同源（PRD FR6.1）：个人档额外覆盖 caller 在其他空间或空串空间
    上传的个人文件（``resolve_extra_personal_workspaces``）。
    """
    ctx = await _make_scope_context_checked(
        user,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    _require_workspace_context(ctx)  # Batch 1/c4: search collection routing needs space context.
    scope_filter = await _allowed_kb_search_scope_filter(request, ctx, body.scope_filter)
    search_ctx = dataclasses.replace(
        ctx,
        extra_personal_workspaces=await resolve_extra_personal_workspaces(
            getattr(request.app.state, "kb_files_store", None),
            ctx.user_id,
        ),
    )
    retriever = await _get_effective_retriever(request, user)
    from bowhead_core.kb.generations import use_guard_enabled

    from bowhead_gateway.kb_authorization import kb_file_authorizer

    authorizer = kb_file_authorizer(getattr(request.app.state, "user_store", None), getattr(request.app.state, "access_control_store", None))

    async def authorize(file: KbFileDTO) -> bool:
        return await authorizer(file, ctx.user_id, ctx.team_id, ctx.workspace_id)

    guard_args = {"authorize_file": authorize} if use_guard_enabled() else {}
    result = await retriever.invoke(
        query=body.query,
        scope_ctx=search_ctx,
        top_k=body.top_k,
        scope_filter=scope_filter,
        **guard_args,
    )
    result = _apply_page_search_relevance_floor(result)
    return await _filter_kb_search_result_by_file_grants(request, ctx, result)


# ─── GET /files/{file_id}/chunks (P1-12-2 frontend drawer) ──


class KbChunkSummary(BaseModel):
    """Lightweight chunk for drawer preview list (no embedding)."""

    chunk_id: str
    content: str
    page: int | None = None
    section_title: str | None = None
    role: str
    chunk_index: int
    generation_id: str | None = None
    historical_generation: bool = False
    current_source_state: str | None = None


class KbChunksListResponse(BaseModel):
    chunks: list[KbChunkSummary]
    total: int


@router.get("/files/{file_id}/chunks", response_model=KbChunksListResponse)
async def list_file_chunks(
    file_id: str,
    request: Request,
    chunk_id: str | None = None,
    user: User = Depends(_current_user),
) -> KbChunksListResponse:
    """List chunks for a file (drawer preview). Active chunks only (P0-O3 is_active filter).

    ``chunk_id``（可选）：只返回这一个分块（同样的权限检查、同样的响应结构，``total`` = 1），
    供单条引用卡片使用；该分块不属于此文件、已删除或所属批次未发布 → 404
    ``kb_chunk_not_found``。不带参数时行为不变（抽屉列出全部分块）。
    """
    ctx = await _make_scope_context_checked(
        user,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    files_store = _get_files_store(request)
    file = await _kb_scope_check(file_id, ctx, files_store, request)

    kb_store = await _get_effective_kb_store(request, user)
    source_state = None
    from bowhead_core.kb.generations import use_guard_enabled

    if use_guard_enabled():
        checked_identity = (file.artifact_id, file.version, file.active_generation_id)
        from bowhead_core.kb.eligibility import KbUseEligibility

        from bowhead_gateway.kb_authorization import kb_file_authorizer

        authorizer = kb_file_authorizer(request.app.state.user_store, getattr(request.app.state, "access_control_store", None))

        async def authorize(current):
            return await authorizer(current, ctx.user_id, ctx.team_id, ctx.workspace_id)

        source_state = "unchecked"
        with anyio.move_on_after(10):
            check = await KbUseEligibility(files_store, _get_artifact_store(request), kb_store).check(file_id, ctx, authorize)
            source_state = check.state
        ctx = await _make_scope_context_checked(user, request, operation=SpaceOperation.READ_HISTORY)
        file = await _kb_scope_check(file_id, ctx, files_store, request)
        if (file.artifact_id, file.version, file.active_generation_id) != checked_identity:
            source_state = "unchecked"
    rows = await kb_store.list_active_chunks(file, chunk_id=chunk_id)
    if chunk_id is not None and rows:
        from bowhead_core.kb.generations import KbGenerationStore

        generation_id = rows[0][2].get("generation_id")
        if generation_id:
            generation = await KbGenerationStore(files_store._db_path).get(generation_id)
            if not generation or generation["file_id"] != file_id or generation["state"] != "published":
                rows = []
    if chunk_id is not None and not rows:
        raise _kb_error(404, "kb_chunk_not_found")

    # Index I/O can overlap revocation or a scope move. Reauthorize before
    # returning passage text, even for a previously published generation.
    ctx = await _make_scope_context_checked(user, request, operation=SpaceOperation.READ_HISTORY)
    current = await _kb_scope_check(file_id, ctx, files_store, request)
    if (current.artifact_id, current.active_generation_id, current.version) != (file.artifact_id, file.active_generation_id, file.version):
        source_state = "unchecked"
    file = current
    # Sort by chunk_index for stable display
    items: list[KbChunkSummary] = []
    for i, (row_chunk_id, content, meta) in enumerate(rows):
        page = meta.get("page", -1)
        items.append(
            KbChunkSummary(
                chunk_id=row_chunk_id,
                content=content,
                page=None if page == -1 else page,
                section_title=meta.get("section_title") or None,
                role=meta.get("role", "intro"),
                chunk_index=meta.get("chunk_index", i + 1),
                generation_id=meta.get("generation_id") or None,
                historical_generation=(meta.get("generation_id") or None) != file.active_generation_id,
                current_source_state=source_state,
            )
        )
    items.sort(key=lambda c: c.chunk_index)
    return KbChunksListResponse(chunks=items, total=len(items))


class KbRebuildBatchRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=20)
    max_bytes: int = Field(default=100 * 1024 * 1024, gt=0, le=500 * 1024 * 1024)
    apply: bool = False
    accept_model_cost: bool = False


@router.post("/maintenance/rebuilds", dependencies=[Depends(require_not_readonly), Depends(require_feature_licensed("kb"))])
async def create_rebuild_batch(body: KbRebuildBatchRequest, request: Request, user: User = Depends(_current_user)):
    from bowhead_core.kb.generations import use_guard_enabled
    from bowhead_core.kb.maintenance import KbMaintenanceStore

    from bowhead_gateway.kb_maintenance import start_rebuild_job

    if not use_guard_enabled():
        raise _kb_error(404, "kb_file_not_found")
    selected = []
    for fid in dict.fromkeys(body.file_ids):
        ctx, file = await _require_source_write(fid, request, user)
        if file.parse_status not in {"done", "failed"}:
            raise _kb_error(409, "kb_health_changed")
        selected.append(file)
    size = sum(file.size_bytes for file in selected)
    if size > body.max_bytes:
        raise _kb_error(413, "kb_file_too_large")
    preview = {"files": len(selected), "size_bytes": size, "model_cost": "usage_based", "concurrency": 1}
    if not body.apply:
        return preview
    if not body.accept_model_cost:
        raise _kb_error(409, "kb_rebuild_cost_confirmation_required")
    keys = ("file_id", "version", "active_generation_id", "team_id", "workspace_id", "scope", "scope_id", "artifact_id")
    items = [{key: getattr(file, key) for key in keys} for file in selected]
    try:
        job_id = await KbMaintenanceStore(_get_files_store(request)._db_path).create_job(
            team_id=ctx.team_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id, items=items
        )
    except ValueError:
        raise _kb_error(409, "kb_rebuild_busy") from None
    start_rebuild_job(request.app, job_id)
    return {**preview, "job_id": job_id, "state": "running"}


async def _rebuild_job_checked(request, user, job_id):
    from bowhead_core.kb.generations import use_guard_enabled
    from bowhead_core.kb.maintenance import KbMaintenanceStore

    if not use_guard_enabled():
        raise _kb_error(404, "kb_file_not_found")
    ctx = await _make_scope_context_checked(user, request, operation=SpaceOperation.READ_HISTORY)
    jobs = KbMaintenanceStore(_get_files_store(request)._db_path)
    job = await jobs.get_job(job_id)
    if not job or (job["team_id"], job["workspace_id"], job["user_id"]) != (ctx.team_id, ctx.workspace_id, ctx.user_id):
        raise _kb_error(404, "kb_file_not_found")
    return jobs, job


@router.get("/maintenance/rebuilds")
async def latest_rebuild_batch(request: Request, user: User = Depends(_current_user)):
    from bowhead_core.kb.generations import use_guard_enabled
    from bowhead_core.kb.maintenance import KbMaintenanceStore

    if not use_guard_enabled():
        raise _kb_error(404, "kb_file_not_found")
    ctx = await _make_scope_context_checked(user, request, operation=SpaceOperation.READ_HISTORY)
    jobs = KbMaintenanceStore(_get_files_store(request)._db_path)
    return {"job_id": await jobs.latest_job_id(team_id=ctx.team_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id)}


@router.get("/maintenance/rebuilds/{job_id}")
async def get_rebuild_batch(job_id: str, request: Request, user: User = Depends(_current_user)):
    _, job = await _rebuild_job_checked(request, user, job_id)
    # Progress belongs to the initiating user; never serialize the frozen source
    # manifest (paths/routing/file identities can become inaccessible mid-job).
    return {
        "job_id": job_id,
        "state": job["state"],
        "total": len(job["items"]),
        "processed": job["cursor"],
        "failed": sum(item["outcome"] == "failed" for item in job["outcomes"]),
        "changed": sum(item["outcome"] == "changed" for item in job["outcomes"]),
    }


@router.post("/maintenance/rebuilds/{job_id}/cancel", dependencies=[Depends(require_not_readonly)])
async def cancel_rebuild_batch(job_id: str, request: Request, user: User = Depends(_current_user)):
    jobs, job = await _rebuild_job_checked(request, user, job_id)
    if job["state"] not in {"running", "paused"}:
        return {"state": job["state"]}
    target = "cancelling" if job["state"] == "running" else "cancelled"
    await jobs.state(job_id, target, expected=job["state"])
    return {"state": target}


@router.post("/maintenance/rebuilds/{job_id}/resume", dependencies=[Depends(require_not_readonly), Depends(require_feature_licensed("kb"))])
async def resume_rebuild_batch(job_id: str, request: Request, user: User = Depends(_current_user)):
    from bowhead_gateway.kb_maintenance import start_rebuild_job

    jobs, job = await _rebuild_job_checked(request, user, job_id)
    for item in job["items"][job["cursor"] :]:
        await _require_source_write(item["file_id"], request, user)
    if not await jobs.resume(job_id):
        raise _kb_error(409, "kb_rebuild_busy")
    start_rebuild_job(request.app, job_id)
    return {"state": "running"}
