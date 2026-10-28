"""Shared per-row extraction trigger (rate-limited enqueue).

Extracted from kb_admin.trigger_extract_rows so two callers share ONE
rate-limited path without a circular import (kb_admin → kb already; this module
imports neither, so both kb_admin and kb can import it):
  - kb_admin POST /kb/files/{id}/extract_rows (force re-trigger)
  - kb._bind_doc_type_core (kb-management D5: auto-extract on doc_type bind)

Returns a status dict; callers map it to HTTP (kb_admin) or per-item results
(bulk bind). No exceptions for expected outcomes (no_doc_type / rate_limited_* /
already_*). Concurrency is bounded by the worker's global parse semaphore.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from bowhead_core.kb.telemetry import emit_audit


async def trigger_row_extraction(
    *,
    file: Any,
    ws_id: str,
    force: bool,
    rows_store: Any,
    jobs_store: Any,
    user_store: Any,
    worker: Any,
    active_tasks: dict[str, Any] | None,
    actor_user_id: str,
    dt_store: Any = None,
) -> dict[str, Any]:
    """Trigger per-row extraction for ``file``. Caller must have resolved
    ``ws_id`` + done its own auth/scope checks and confirmed rows_store/jobs_store/
    worker are non-None. Behaviour is identical to the original
    kb_admin.trigger_extract_rows body from the dt_id check onward.
    """
    dt_id = getattr(file, "auto_classified_doc_type_id", None) or getattr(file, "doc_type_id", None)
    if not dt_id:
        return {"status": "no_doc_type"}
    # kb-matrix M4 gate③ (decision 12): auto_extract=off admits no new extraction into this table;
    # frozen snapshots/column-group templates cannot be reextracted (force soft_delete_for_attempt silently
    # deletes frozen rows, hollowing snapshots). Worker entry has a matching second defense.
    if dt_store is not None:
        _dt = await dt_store.get(dt_id)
        if _dt is not None and not getattr(_dt, "auto_extract_enabled", True):
            return {"status": "extract_disabled", "doc_type_id": dt_id}
    file_id = file.file_id

    # Idempotency FIRST — a no-op (non-force) trigger on an already in-progress /
    # succeeded job must NOT consume rate-limit quota. Otherwise a bulk doc_type
    # re-bind of an already-extracted type would burn the per-ws 50/day cap doing
    # no work (self-inflicted quota exhaustion).
    latest_job = await jobs_store.get_latest_for_file(file_id)
    if not force:
        if latest_job and latest_job.status in ("queued", "running"):
            return {"status": "already_in_progress", "job_id": latest_job.job_id}
        if latest_job and latest_job.status == "succeeded":
            return {"status": "already_succeeded", "job_id": latest_job.job_id, "hint": "set force=true to re-extract"}

    # Rate limit (per-file 5/hour, per-ws 50/day) — only when we will actually
    # enqueue work. Audit emitted here so both callers get the same trail.
    if user_store is not None:
        if not await user_store.rate_limit_check(f"kb_row_extract:{file_id}", limit=5, window_s=3600):
            emit_audit(
                "kb.row_extract_force_rate_limit",
                {
                    "file_id": file_id,
                    "workspace_id": ws_id,
                    "scope": "per_file_5_per_hour",
                },
            )
            return {"status": "rate_limited_file"}
        if not await user_store.rate_limit_check(f"kb_row_extract_ws:{ws_id}", limit=50, window_s=86400):
            emit_audit(
                "kb.row_extract_force_rate_limit",
                {
                    "workspace_id": ws_id,
                    "scope": "per_ws_50_per_day",
                },
            )
            return {"status": "rate_limited_ws"}

    # Force path: cancel old task + soft-delete old attempt + new attempt
    new_attempt = (latest_job.attempt_id + 1) if latest_job else 1
    cancelled = False
    if active_tasks and file_id in active_tasks:
        old_task = active_tasks[file_id]
        if not old_task.done():
            old_task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(old_task, timeout=10.0)
            cancelled = True

    if latest_job:
        with contextlib.suppress(Exception):
            await jobs_store.mark_status(latest_job.job_id, "cancelled")

    # P0-SEC-7: soft-delete old attempt rows (NOT hard delete — preserves audit trail)
    deleted_count = await rows_store.soft_delete_for_attempt(
        file_id=file_id,
        doc_type_id=dt_id,
        workspace_id=ws_id,
        attempt_id_lt=new_attempt,
    )
    emit_audit(
        "kb.row_extract_force_trigger",
        {
            "file_id": file_id,
            "doc_type_id": dt_id,
            "workspace_id": ws_id,
            "actor_user_id": actor_user_id,
            "soft_deleted_count": deleted_count,
            "cancelled_old_task": cancelled,
            "new_attempt_id": new_attempt,
        },
    )

    # Re-parse → classifier → extract (heavier than needed; see Sprint 26 note in
    # kb_admin). Bounded by the worker's global parse semaphore.
    asyncio.create_task(
        worker.parse_and_index(file_id, allow_terminal=True),
        name=f"kb_row_extract_force:{file_id}",
    )
    return {
        "status": "queued",
        "attempt_id": new_attempt,
        "soft_deleted_old_rows": deleted_count,
        "cancelled_old_task": cancelled,
        "estimated_minutes": 5,
        "estimated_cost_usd": 0.013,
    }
