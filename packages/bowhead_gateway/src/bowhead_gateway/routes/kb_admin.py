"""kb-structured-tables Sprint 2 Phase B — admin backfill endpoint.

Re-run classifier + extract on existing parsed files (the ones uploaded BEFORE
Sprint 2 Phase B shipped, hence no auto_classified_doc_type_id). Used by:
  - Dogfood after ship to populate kb_extracted_records on existing corpus.
  - Recovery after a doc_type catalog change.

Endpoints:
  POST  /api/v1/admin/kb/files/backfill         — queue task
  GET   /api/v1/admin/kb/files/backfill/{task_id}  — poll status
  POST  /api/v1/admin/kb/files/backfill/{task_id}/cancel

RBAC (P0-SEC-3):
  - Caller must be workspace_admin (via _require_workspace_admin).
  - body.workspace_id MUST equal caller's user.workspace_id (no cross-ws backfill
    via this route — cross-ws bulk seed uses the BOWHEAD_ADMIN_TOKEN-gated
    `scripts/seed_kb_doc_type_catalog.py`).

State (per P2-TD-2 dual-dict):
  - app.state.kb_backfill_active_ws: dict[ws_id, task_id]    (per-ws lock)
  - app.state.kb_backfill_tasks_by_id: dict[task_id, Task]   (cancel lookup)
  - app.state.kb_backfill_status: dict[task_id, BackfillStatus]  (poll DTO)

Concurrency:
  - 1 task per workspace_id (returns 409 if already running).
  - Strong-ref via `app.state` prevents GC of background task (per
    [[feedback_no_reactive_patch_system_primitives]]).
  - Cursor pagination (max 200 files / call).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from bowhead_core.kb.extracted_record_store import RecordAlreadyExistsError
from bowhead_core.kb.telemetry import emit_audit
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_feature_licensed, require_not_readonly
from bowhead_gateway.access_context import build_access_context_from_request, has_workspace_access_source
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.kb import (
    _get_effective_worker,
    _kb_error,
    _kb_file_not_found,
    _kb_scope_check,
    _make_scope_context_checked,
    _require_workspace_context,
)
from bowhead_gateway.routes.kb_extract_service import trigger_row_extraction

if TYPE_CHECKING:
    from bowhead_core.user_store import User

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


async def _resolve_ws_id(
    user: User,
    request: Request,
    workspace_id: str | None = None,
    *,
    operation: SpaceOperation = SpaceOperation.WRITE,
) -> str:
    """Resolve the target workspace for an admin KB op — explicit only.

    批1/c4 (workspace-space-split): the `or user.team_id` fallback is removed — an team id
    is NOT a workspace (it bled team_* values into KB workspace_id columns). Admin KB ops
    are inherently per-workspace: missing context → 400."""
    raw = workspace_id or request.headers.get("X-Space-Id") or request.headers.get("X-Workspace-Id") or ""  # Batch 3/c2 dual read.
    if not raw:
        raise _kb_error(400, "missing_workspace_context")
    if not has_workspace_access_source(request):
        return raw
    access_ctx = await build_access_context_from_request(
        request,
        user=user,
        workspace_id=raw,
        operation=operation,
    )
    if not access_ctx.is_team_admin and access_ctx.workspace_role != "ws_admin":
        raise _kb_error(403, "workspace_admin_required")
    return access_ctx.workspace_id


async def _readable_file_for_admin_op(
    file_id: str,
    user: User,
    request: Request,
    *,
    operation: SpaceOperation,
) -> tuple[Any, str]:
    """Resolve a per-file admin op target without revealing whether the id exists.

    The caller's Space context is built first (it depends only on the caller and the
    header), then the file goes through the same read check as every KB file route:
    missing, unreadable (including another member's personal file) and outside the
    current Space all answer ``404 kb_file_not_found``. Only for a file the caller can
    read does the admin requirement apply.
    """
    ctx = await _make_scope_context_checked(user, request, operation=operation)
    _require_workspace_context(ctx)
    file = await _kb_scope_check(file_id, ctx, request.app.state.kb_files_store, request)
    if file.workspace_id != ctx.workspace_id:
        raise _kb_file_not_found()
    if has_workspace_access_source(request) and ctx.role not in ("ws_admin", "team_admin"):
        raise _kb_error(403, "workspace_admin_required")
    return file, ctx.workspace_id


# ─── DTO ──────────────────────────────────────────────────


class BackfillRequest(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    file_ids: list[str] | None = None  # None → all done files w/o doc_type_id
    limit: int = Field(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT)
    cursor: str | None = None  # last file_id processed, for pagination


@dataclass
class BackfillStatus:
    task_id: str
    workspace_id: str
    state: str  # queued | running | completed | failed | cancelled
    requested_by: str
    requested_at: str
    started_at: str | None = None
    completed_at: str | None = None
    processed: int = 0
    succeeded: int = 0
    skipped_no_match: int = 0
    skipped_disabled: int = 0
    skipped_per_row: int = 0
    skipped_duplicate: int = 0
    failed: int = 0
    last_processed_file_id: str | None = None
    next_cursor: str | None = None
    error_code: str | None = None
    # Deprecated compatibility field. Internal exception names stay in logs.
    error_message: str | None = None
    file_ids_subset: list[str] = field(default_factory=list)  # echo of input

    def to_public_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── State helpers ────────────────────────────────────────


def _state(request: Request) -> Any:
    return request.app.state


def _ensure_state(request: Request) -> None:
    """Initialize state dicts if absent. main.py lifespan does this on boot;
    this is a defensive idempotent fallback.
    """
    s = _state(request)
    if not hasattr(s, "kb_backfill_active_ws"):
        s.kb_backfill_active_ws = {}
    if not hasattr(s, "kb_backfill_tasks_by_id"):
        s.kb_backfill_tasks_by_id = {}
    if not hasattr(s, "kb_backfill_status"):
        s.kb_backfill_status = {}


# ─── Endpoints ────────────────────────────────────────────


@router.post("/api/v1/admin/kb/files/backfill", dependencies=[Depends(require_not_readonly)])
async def trigger_backfill(
    body: BackfillRequest,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """Start an async backfill task. RBAC: workspace_admin only.

    Returns {task_id, queued, total_estimate, batch_size}. Use GET .../backfill/{task_id}
    to poll progress.
    """
    # RBAC: body.workspace_id must equal caller's workspace_id (no cross-ws via this route)
    caller_ws = await _resolve_ws_id(
        user,
        request,
        operation=SpaceOperation.EXECUTE,
    )
    if body.workspace_id != caller_ws:
        raise _kb_error(403, "cross_ws_backfill_forbidden")
    await _resolve_ws_id(
        user,
        request,
        body.workspace_id,
        operation=SpaceOperation.EXECUTE,
    )

    _ensure_state(request)
    s = _state(request)

    # Per-ws lock
    if body.workspace_id in s.kb_backfill_active_ws:
        existing_task_id = s.kb_backfill_active_ws[body.workspace_id]
        raise _kb_error(
            409,
            "backfill_already_running",
            task_id=existing_task_id,
        )

    task_id = f"bf_{uuid4().hex[:12]}"
    status = BackfillStatus(
        task_id=task_id,
        workspace_id=body.workspace_id,
        state="queued",
        requested_by=user.id,
        requested_at=_now(),
        file_ids_subset=list(body.file_ids) if body.file_ids else [],
    )
    s.kb_backfill_status[task_id] = status

    # Build the runner closure with explicit refs to stores (frozen at call time).
    files_store = getattr(s, "kb_files_store", None)
    try:
        kb_worker = await _get_effective_worker(request, user)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        kb_worker = None
    if files_store is None or kb_worker is None:
        raise _kb_error(503, "kb_not_ready")

    task = asyncio.create_task(
        _run_backfill(task_id, body, files_store, kb_worker, status),
    )

    s.kb_backfill_active_ws[body.workspace_id] = task_id
    s.kb_backfill_tasks_by_id[task_id] = task

    def _cleanup(t: asyncio.Task) -> None:
        # Drop locks; leave status DTO for poll-after-done.
        s.kb_backfill_active_ws.pop(body.workspace_id, None)
        s.kb_backfill_tasks_by_id.pop(task_id, None)
        # Reflect terminal state into status if not already set by runner
        cur_status = s.kb_backfill_status.get(task_id)
        if cur_status and cur_status.state in ("queued", "running"):
            if t.cancelled():
                cur_status.state = "cancelled"
            elif t.exception():
                cur_status.state = "failed"
                cur_status.error_code = "kb_backfill_failed"
            else:
                cur_status.state = "completed"
            cur_status.completed_at = _now()

    task.add_done_callback(_cleanup)

    emit_audit(
        "kb.backfill_admin_invoked",
        {
            "task_id": task_id,
            "actor_user_id": user.id,
            "target_ws_id": body.workspace_id,
            "limit": body.limit,
            "explicit_file_count": len(body.file_ids) if body.file_ids else 0,
        },
    )

    return {
        "task_id": task_id,
        "queued": True,
        "batch_size": body.limit,
    }


@router.get("/api/v1/admin/kb/files/backfill/{task_id}")
async def get_backfill_status(
    task_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    _ensure_state(request)
    s = _state(request)
    status = s.kb_backfill_status.get(task_id)
    if status is None:
        raise _kb_error(404, "backfill_task_not_found")
    # Caller must be admin in same ws as task
    caller_ws = await _resolve_ws_id(
        user,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    if status.workspace_id != caller_ws:
        raise _kb_error(403, "cross_ws_backfill_status_forbidden")
    await _resolve_ws_id(
        user,
        request,
        status.workspace_id,
        operation=SpaceOperation.READ_HISTORY,
    )
    return status.to_public_dict()


@router.post("/api/v1/admin/kb/files/backfill/{task_id}/cancel", dependencies=[Depends(require_not_readonly)])
async def cancel_backfill(
    task_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    _ensure_state(request)
    s = _state(request)
    status = s.kb_backfill_status.get(task_id)
    if status is None:
        raise _kb_error(404, "backfill_task_not_found")
    caller_ws = await _resolve_ws_id(
        user,
        request,
        operation=SpaceOperation.EXECUTE,
    )
    if status.workspace_id != caller_ws:
        raise _kb_error(403, "cross_ws_backfill_cancel_forbidden")
    await _resolve_ws_id(
        user,
        request,
        status.workspace_id,
        operation=SpaceOperation.EXECUTE,
    )
    task = s.kb_backfill_tasks_by_id.get(task_id)
    if task is None or task.done():
        return {
            "cancelled": False,
            "reason": "backfill_task_already_done",
            "task_id": task_id,
        }
    task.cancel()
    return {"cancelled": True, "task_id": task_id}


# ─── Sprint 25 kb-per-row-extract endpoints ──────────────────


class ExtractRowsTriggerRequest(BaseModel):
    """POST /extract_rows body — force=True triggers fresh attempt (P0-SEC-7
    soft-deletes old rows + new attempt_id)."""

    force: bool = False


@router.post("/api/v1/kb/files/{file_id}/extract_rows", dependencies=[Depends(require_not_readonly)])
async def trigger_extract_rows(
    file_id: str,
    body: ExtractRowsTriggerRequest,
    request: Request,
    user: User = Depends(_current_user),
    _feat: None = Depends(require_feature_licensed("kb")),  # §5.1 entitlement
) -> dict[str, Any]:
    """Sprint 25 P0-ENG-1 force re-trigger endpoint.

    Workspace_admin only. force=True cancels active task + soft-deletes prior
    attempt rows + spawns new attempt. Rate limited (5/file/hr, 50/day/ws, 5s
    cooldown via per_row_jobs heartbeat_at delta).

    Per PRD r3 §3.4 D2 + I10. Status 404 cross-ws (enumeration-resist).
    """
    s = request.app.state
    file, ws_id = await _readable_file_for_admin_op(file_id, user, request, operation=SpaceOperation.EXECUTE)

    rows_store = getattr(s, "kb_extracted_rows_store", None)
    jobs_store = getattr(s, "kb_per_row_jobs_store", None)
    worker = await _get_effective_worker(request, user)
    active_tasks = getattr(s, "kb_per_row_tasks_by_file_id", None)
    user_store = getattr(s, "user_store", None)
    if rows_store is None or jobs_store is None or worker is None:
        raise _kb_error(503, "per_row_extract_unavailable")

    # kb-management D5: shared rate-limited trigger (also used by doc_type bind
    # auto-extract). Maps the service status dict back to this endpoint's HTTP
    # contract; per-row scaffolding (cancel/soft-delete/audit) lives in the service.
    res = await trigger_row_extraction(
        file=file,
        ws_id=ws_id,
        force=body.force,
        rows_store=rows_store,
        jobs_store=jobs_store,
        user_store=user_store,
        worker=worker,
        active_tasks=active_tasks,
        actor_user_id=user.id,
        dt_store=getattr(request.app.state, "kb_doc_type_store", None),
    )
    status = res.get("status")
    if status == "no_doc_type":
        raise _kb_error(400, "file_has_no_doc_type")
    if status == "extract_disabled":
        # kb-matrix M4 gate③: frozen snapshots/column-group templates reject reextraction (attempt cannot soft-delete snapshot rows).
        raise _kb_error(422, "doc_type_extract_disabled")
    if status == "rate_limited_file":
        raise _kb_error(429, "rate_limit_per_file")
    if status == "rate_limited_ws":
        raise _kb_error(429, "rate_limit_per_ws_daily")
    return res


# NOTE: this literal-path route MUST be declared BEFORE the parameterized
# /{file_id}/extract_rows_status route below — Starlette matches in declaration
# order, and /{file_id} would otherwise capture "_batch" as a file_id (→ 404).
@router.get("/api/v1/kb/files/_batch/extract_rows_status")
async def get_extract_rows_status_batch(
    request: Request,
    user: User = Depends(_current_user),
    file_ids: str = "",
) -> dict[str, Any]:
    """Sprint 25 batch poll for FE single-page polling (P1-ENG-8).

    Single setInterval polls N file_ids at once, returns dict
    {file_id: {status, ...}}. Cap 50 file_ids/call (defensive).
    Cross-ws filter at SQL layer (workspace_id = ?).
    """
    ws_id = await _resolve_ws_id(
        user,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    jobs_store = getattr(request.app.state, "kb_per_row_jobs_store", None)
    if jobs_store is None:
        return {"jobs": {}}
    fids = [s.strip() for s in (file_ids or "").split(",") if s.strip()][:50]
    if not fids:
        return {"jobs": {}}
    jobs = await jobs_store.list_for_file_ids(file_ids=fids, workspace_id=ws_id)
    return {
        "jobs": {
            j.file_id: {
                "job_id": j.job_id,
                "status": j.status,
                "attempt_id": j.attempt_id,
                "total_batches": j.total_batches,
                "completed_batches": j.completed_batches,
                "rows_extracted": j.rows_extracted,
                "error_code": j.error_code,
                "error_hint": j.error_hint,
            }
            for j in jobs
        },
    }


@router.get("/api/v1/kb/files/{file_id}/extract_rows_status")
async def get_extract_rows_status(
    file_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """Sprint 25 per-row extraction status poll endpoint.

    Returns latest job for a file_id. Cross-ws guard via 404 (P0-TD-SEC-2
    enumeration-resist: don't expose existence of file in other ws via 403).
    """
    file, _ = await _readable_file_for_admin_op(file_id, user, request, operation=SpaceOperation.READ_HISTORY)

    jobs_store = getattr(request.app.state, "kb_per_row_jobs_store", None)
    if jobs_store is None:
        return {"status": "na", "job_id": None, "note": "per_row_jobs_store unavailable"}

    job = await jobs_store.get_latest_for_file(file_id)
    if job is None:
        return {
            "status": "na",
            "job_id": None,
            "extracted_rows_status": file.extracted_rows_status,
        }
    return {
        "job_id": job.job_id,
        "status": job.status,
        "attempt_id": job.attempt_id,
        "total_batches": job.total_batches,
        "completed_batches": job.completed_batches,
        "rows_extracted": job.rows_extracted,
        "rows_skipped_dup": job.rows_skipped_dup,
        "error_code": job.error_code,
        "error_hint": job.error_hint,
        "started_at": job.started_at,
        "heartbeat_at": job.heartbeat_at,
        "finished_at": job.finished_at,
        "extracted_rows_status": file.extracted_rows_status,
    }


@router.get("/api/v1/kb/admin/llm_cost_summary")
async def get_llm_cost_summary(
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """Sprint 26 PRD §3.4 D4 + CTO P1-PERF-1: per-ws monthly LLM token usage
    + cap summary. Workspace_admin only — admin sees own workspace data."""
    ws_id = await _resolve_ws_id(
        user,
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    quotas_store = getattr(request.app.state, "kb_workspace_quotas_store", None)
    if quotas_store is None:
        return {"workspace_id": ws_id, "note": "quotas store unavailable"}
    summary = await quotas_store.get_summary(ws_id)
    if summary is None:
        return {
            "workspace_id": ws_id,
            "tokens_used": 0,
            "tokens_cap": 50_000_000,
            "month_yyyymm": None,
            "note": "no usage yet this month",
        }
    estimated_cost_usd = (summary.tokens_used * 0.000020) / 1_000_000  # qwen-flash output approx
    return {
        "workspace_id": summary.workspace_id,
        "month_yyyymm": summary.month_yyyymm,
        "tokens_used": summary.tokens_used,
        "tokens_cap": summary.tokens_cap,
        "estimated_cost_usd": round(estimated_cost_usd, 4),
        "last_used_at": summary.last_used_at,
        "headroom_pct": round((summary.tokens_cap - summary.tokens_used) / summary.tokens_cap * 100, 1),
    }


# ─── Runner ───────────────────────────────────────────────


async def _run_backfill(
    task_id: str,
    body: BackfillRequest,
    files_store: Any,
    kb_worker: Any,
    status: BackfillStatus,
) -> None:
    """Iterate done-files-without-doc_type, invoke worker's auto-classify path.

    Soft-fail per file; aggregate counters into status DTO.
    """
    status.state = "running"
    status.started_at = _now()
    try:
        # Resolve target file_ids
        if body.file_ids:
            file_ids = list(body.file_ids)
        else:
            # Pull all done files in ws without explicit doc_type_id AND without
            # auto_classified_doc_type_id (i.e. not yet attempted).

            # files_store may be a real KbFilesStore or a stub for tests.
            file_ids = await _select_backfill_file_ids(
                files_store,
                body.workspace_id,
                body.cursor,
                body.limit,
            )

        # Cap at body.limit even when caller supplies explicit ids (defense).
        file_ids = file_ids[: body.limit]

        from bowhead_core.kb.loader.dispatch import get_loader

        for file_id in file_ids:
            # Cancellation point
            if asyncio.current_task().cancelled():  # type: ignore[union-attr]
                break

            status.processed += 1
            status.last_processed_file_id = file_id
            try:
                file_obj = await files_store.get(file_id)
                if file_obj is None or file_obj.workspace_id != body.workspace_id:
                    status.failed += 1
                    continue
                # Skip if already classified or explicit-bound
                if file_obj.doc_type_id:
                    status.skipped_duplicate += 1
                    continue
                if file_obj.auto_classified_doc_type_id:
                    status.skipped_duplicate += 1
                    continue

                # Re-run loader to get text. Reuse worker._fetch_artifact_path +
                # loader dispatch. For Sprint 2 V1 we assume parse_status='done'
                # (kb_chunks already exist); the loader.load returns the same
                # documents the worker used originally.
                try:
                    file_path = await kb_worker._fetch_artifact_path(file_obj)
                    loader = get_loader(
                        file_obj.mime_type,
                        vision_client=kb_worker._vision_client,
                    )
                    load_result = await loader.load(
                        file_path,
                        use_llm=file_obj.use_llm_marker,
                    )
                except Exception as load_exc:
                    logger.warning(
                        "backfill load failed file_id=%s err=%s",
                        file_id,
                        type(load_exc).__name__,
                    )
                    status.failed += 1
                    continue

                # Snapshot outcome counters before/after to attribute deltas
                # (worker emits audits but doesn't return; we infer outcome by
                # post-state inspect).
                await kb_worker._run_auto_classify_and_extract(file_obj, load_result)

                # Inspect post-state to bucket outcome.
                refreshed = await files_store.get(file_id)
                if refreshed is None or refreshed.auto_classified_doc_type_id is None:
                    status.skipped_no_match += 1
                    continue
                status.succeeded += 1
            except RecordAlreadyExistsError:
                status.skipped_duplicate += 1
            except asyncio.CancelledError:
                raise  # propagate cancel
            except Exception as e:
                logger.warning(
                    "backfill per-file error file_id=%s err=%s",
                    file_id,
                    type(e).__name__,
                )
                status.failed += 1

        status.next_cursor = status.last_processed_file_id if len(file_ids) >= body.limit else None
        status.state = "completed"
        status.completed_at = _now()
        emit_audit(
            "kb.backfill_completed",
            {
                "task_id": task_id,
                "processed": status.processed,
                "succeeded": status.succeeded,
                "skipped_no_match": status.skipped_no_match,
                "skipped_disabled": status.skipped_disabled,
                "skipped_per_row": status.skipped_per_row,
                "skipped_duplicate": status.skipped_duplicate,
                "failed": status.failed,
            },
        )
    except asyncio.CancelledError:
        status.state = "cancelled"
        status.completed_at = _now()
        raise
    except Exception as exc:
        status.state = "failed"
        status.error_code = "kb_backfill_failed"
        status.completed_at = _now()
        emit_audit(
            "kb.backfill_failed",
            {
                "task_id": task_id,
                "error_code": type(exc).__name__,
            },
        )


async def _select_backfill_file_ids(
    files_store: Any,
    workspace_id: str,
    cursor: str | None,
    limit: int,
) -> list[str]:
    """Lightweight wrapper: list done files in ws missing both doc_type_id +
    auto_classified_doc_type_id, ordered by uploaded_at asc, with optional
    cursor pagination.

    Implemented inline here (rather than as a new KbFilesStore method) to
    keep Sprint 2 Phase B scope contained. Sprint 3+ can promote to store
    if other callers need it.
    """
    import aiosqlite

    db_path = getattr(files_store, "_db_path", None)
    if db_path is None:
        # Tests may inject a stub; fall back to a public list() if available.
        if hasattr(files_store, "list_backfill_candidates"):
            return await files_store.list_backfill_candidates(
                workspace_id=workspace_id,
                cursor=cursor,
                limit=limit,
            )
        return []

    sql = (
        "SELECT file_id FROM kb_files "
        "WHERE workspace_id = ? "
        "AND parse_status = 'done' "
        "AND doc_type_id IS NULL "
        "AND (auto_classified_doc_type_id IS NULL OR auto_classified_doc_type_id = '') "
        "AND soft_deleted_at IS NULL "
    )
    params: list[Any] = [workspace_id]
    if cursor:
        sql += "AND file_id > ? "
        params.append(cursor)
    sql += "ORDER BY file_id ASC LIMIT ?"
    params.append(limit)
    async with aiosqlite.connect(db_path) as db, db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [r[0] for r in rows]
