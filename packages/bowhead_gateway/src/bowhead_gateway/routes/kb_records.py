"""KB extracted_record CRUD routes (kb-knowledge-tables sprint v1.0).

Endpoints:
- GET    /api/v1/kb/doc_types/{doc_type_id}/records?workspace_id=&limit=&offset=
- GET    /api/v1/kb/records/{record_id}?workspace_id=
- PATCH  /api/v1/kb/records/{record_id}/field   {field_name, new_value}
- PATCH  /api/v1/kb/records/{record_id}/verify_status   {status}

P0-R1 cross-ws leak 防御: GET 强制 workspace_id 过滤; PATCH 验 record.workspace_id ==
caller's workspace before mutating.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.access_control import workspace_role_satisfies
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.access_context import build_access_context_from_request, has_workspace_access_source
from bowhead_gateway.routes.auth import _current_user

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bowhead_core.access_control import AccessContext
    from bowhead_core.kb.doc_type_store import ExtractField, KbDocTypeStore
    from bowhead_core.kb.extracted_record_store import KbExtractedRecordStore
    from bowhead_core.user_store import User

router = APIRouter(tags=["knowledge-base"])


def _get_record_store(request: Request) -> KbExtractedRecordStore:
    s = getattr(request.app.state, "kb_extracted_record_store", None)
    if s is None:
        raise _record_error(503, "kb_record_service_unavailable")
    return s


def _record_error(status_code: int, code: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code}},
    )


def _ws_from_request(request: Request, fallback: str) -> str:
    """Caller's workspace from header (set by frontend), fallback to query param."""
    hdr = request.headers.get("X-Space-Id") or request.headers.get("X-Workspace-Id")  # Batch 3/c2 dual read.
    return hdr or fallback


async def _require_workspace_member(
    request: Request,
    user: User,
    workspace_id: str,
    *,
    operation: SpaceOperation = SpaceOperation.WRITE,
) -> tuple[str, AccessContext | None]:
    """Verify caller is a member of ``workspace_id`` (fail-closed) and return
    ``(authoritative_ws, access_ctx | None)``.

    P0 (kb-security-quickfix): every other KB router builds an access context to
    enforce workspace membership, but records routes historically trusted the
    client-supplied ``workspace_id`` with the sole ``rec.workspace_id != ws``
    guard — and ``ws`` itself was attacker-controlled. Any authed user who knew a
    ``record_id``/``doc_type_id`` + a target ``workspace_id`` could read or mutate
    another tenant's records. Mirror ``kb_doc_types._require_workspace_member`` so
    records enforces membership too, and return ``access_ctx.workspace_id`` so
    callers scope the store with a server-derived value rather than the raw input.

    Falls back to the raw ``workspace_id`` (ctx=None) only when the app state lacks
    the RBAC user-store hooks (legacy route-test harnesses), matching the doc_types
    router. kb-matrix M1: write routes additionally enforce a role floor via
    ``_require_member_rank`` — membership alone is NOT enough (viewer is a member).
    """
    if not has_workspace_access_source(request):
        return workspace_id, None
    ctx = await build_access_context_from_request(
        request,
        user=user,
        workspace_id=workspace_id,
        operation=operation,
    )
    return ctx.workspace_id, ctx


def _require_member_rank(ctx: AccessContext | None) -> None:
    """kb-matrix M1 (PRD FR-34): mutations require role rank ≥ member.

    ``viewer`` is a legitimate workspace member (rank 1) but read-only — membership
    check alone let viewers PATCH records. team_admin passes (S2-style gate) but only
    after membership already held (ctx exists ⇔ membership verified upstream).
    ctx=None (legacy no-hooks harness) keeps the historical fallback semantics.
    """
    if ctx is None:
        return
    if ctx.is_team_admin or workspace_role_satisfies(ctx.workspace_role, "member"):
        return
    raise _record_error(403, "workspace_member_role_required")


async def _doc_type_fields(request: Request, doc_type_id: str | None, workspace_id: str) -> list[ExtractField] | None:
    """Best-effort fields lookup so V1-legacy rows lazy-wrap with real type hints.

    Without this, ``list_for_doc_type``/``get`` degrade V1 rows to string-shape
    normalized and the ``raw_string`` residual sentinel (needs_review 态) can never
    surface (kb-matrix M1 tech design §4.5). Store missing / doc_type gone → None,
    preserving the historical degrade path.

    ws-scope guard (impl review P2-2): ``KbDocTypeStore.get`` has no workspace
    predicate and the list route's ``doc_type_id`` is caller-supplied — a foreign
    tenant's doc_type must never influence this tenant's responses (unexploitable
    today because no record in the caller's ws can carry a foreign doc_type_id,
    but M2's matrix routes will clone this helper — keep it closed by shape).
    """
    if not doc_type_id:
        return None
    store: KbDocTypeStore | None = getattr(request.app.state, "kb_doc_type_store", None)
    if store is None:
        return None
    try:
        dt = await store.get(doc_type_id)
    except Exception:  # noqa: BLE001 - reads must degrade, not break (logged for ops)
        logger.warning("kb_records: doc_type fields lookup failed doc_type_id=%s", doc_type_id, exc_info=True)
        return None
    if dt is None or dt.workspace_id != workspace_id:
        return None
    return dt.fields


# ─── DTOs ───────────────────────────


class _FieldEditIn(BaseModel):
    field_name: str = Field(..., min_length=1, max_length=100)
    new_value: Any = None


class _VerifyStatusIn(BaseModel):
    # kb-v2 §A.8: extract_failed added for Sprint 2 D.1 review queue.
    status: Literal["ready", "needs_review", "approved", "extract_failed"]


# ─── routes ─────────────────────────


@router.get("/api/v1/kb/doc_types/{doc_type_id}/records")
async def list_records_for_doc_type(
    doc_type_id: str,
    request: Request,
    workspace_id: str,
    limit: int = 100,
    offset: int = 0,
    user: User = Depends(_current_user),
) -> dict:
    store = _get_record_store(request)
    ws = _ws_from_request(request, workspace_id)
    ws, _ctx = await _require_workspace_member(
        request,
        user,
        ws,
        operation=SpaceOperation.READ_HISTORY,
    )
    items = await store.list_for_doc_type(
        workspace_id=ws,
        doc_type_id=doc_type_id,
        limit=min(max(int(limit), 1), 1000),
        offset=max(int(offset), 0),
        doc_type_fields=await _doc_type_fields(request, doc_type_id, ws),
    )
    total = await store.count_for_doc_type(
        workspace_id=ws,
        doc_type_id=doc_type_id,
    )
    return {
        "items": [r.to_dict() for r in items],
        "total": total,
        "doc_type_id": doc_type_id,
        "workspace_id": ws,
    }


@router.get("/api/v1/kb/records/{record_id}")
async def get_record(
    record_id: str,
    request: Request,
    workspace_id: str,
    user: User = Depends(_current_user),
) -> dict:
    store = _get_record_store(request)
    ws = _ws_from_request(request, workspace_id)
    ws, _ctx = await _require_workspace_member(
        request,
        user,
        ws,
        operation=SpaceOperation.READ_HISTORY,
    )
    rec = await store.get(record_id, workspace_id=ws)
    if rec is None:
        # Missing records or records outside the caller's membership-verified ws both return 404, concealing existence.
        raise _record_error(404, "kb_record_not_found")
    # v2 rows ignore doc_type_fields entirely (_row_to_dto) — skip the second read.
    if rec.schema_version != 2:
        fields = await _doc_type_fields(request, rec.doc_type_id, ws)
        if fields is not None:
            rec = await store.get(record_id, workspace_id=ws, doc_type_fields=fields) or rec
    return rec.to_dict()


@router.patch("/api/v1/kb/records/{record_id}/field", dependencies=[Depends(require_not_readonly)])
async def edit_record_field(
    record_id: str,
    body: _FieldEditIn,
    request: Request,
    workspace_id: str,
    user: User = Depends(_current_user),
) -> dict:
    store = _get_record_store(request)
    ws = _ws_from_request(request, workspace_id)
    ws, ctx = await _require_workspace_member(request, user, ws)
    _require_member_rank(ctx)
    # P0: caller's ws membership is verified; fetch the record in authoritative ws scope to prevent cross-ws edits.
    rec = await store.get(record_id, workspace_id=ws)
    if rec is None:
        raise _record_error(404, "kb_record_not_found")
    fields = await _doc_type_fields(request, rec.doc_type_id, ws)
    try:
        ok = await store.update_field(
            record_id=record_id,
            field_name=body.field_name,
            new_value=body.new_value,
            modified_by=user.id,
            workspace_id=ws,
            doc_type_fields=fields,
        )
    except ValueError as e:
        raise _record_error(400, "invalid_kb_record_value") from e
    if not ok:
        raise _record_error(404, "kb_record_not_found")
    updated = await store.get(record_id, workspace_id=ws, doc_type_fields=fields)
    return updated.to_dict() if updated else {"record_id": record_id}


@router.patch("/api/v1/kb/records/{record_id}/verify_status", dependencies=[Depends(require_not_readonly)])
async def update_record_verify_status(
    record_id: str,
    body: _VerifyStatusIn,
    request: Request,
    workspace_id: str,
    user: User = Depends(_current_user),
) -> dict:
    store = _get_record_store(request)
    ws = _ws_from_request(request, workspace_id)
    ws, ctx = await _require_workspace_member(request, user, ws)
    _require_member_rank(ctx)
    rec = await store.get(record_id, workspace_id=ws)
    if rec is None:
        raise _record_error(404, "kb_record_not_found")
    ok = await store.update_verify_status(
        record_id,
        body.status,
        modified_by=user.id,
        workspace_id=ws,
    )
    if not ok:
        raise _record_error(404, "kb_record_not_found")
    fields = await _doc_type_fields(request, rec.doc_type_id, ws) if rec.schema_version != 2 else None
    updated = await store.get(record_id, workspace_id=ws, doc_type_fields=fields)
    return updated.to_dict() if updated else {"record_id": record_id}
