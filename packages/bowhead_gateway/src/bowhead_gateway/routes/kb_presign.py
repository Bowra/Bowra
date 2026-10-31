"""KB presigned-POST endpoints — Sprint file-upload-presign Phase 1.4b.

Single-doc presign + confirm:
  POST /api/v1/kb/presign                            — issue envelope (single doc)
  POST /api/v1/kb/presign/{file_id}/confirm         — finalize + enqueue worker

Bundle presign + confirm land in Phase 1.4c (shares the stream-switch from
Phase 1.5 _process_zip_bundle rewrite).

Tech-design §5.1b + §5.3b.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import aiosqlite
from bowhead_core.artifact_store import _open_db, _safe_filename
from bowhead_core.kb.parse_recovery import heartbeat_iso
from bowhead_core.kb.permission import KbPermission
from bowhead_core.kb.schemas import KbFileDTO, KbScopeIn
from bowhead_core.kb.telemetry import emit_audit
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_feature_licensed, require_not_readonly
from bowhead_gateway.artifact_attribution import ATTRIBUTION_EVENT, decide_for_kb_scope, track_attribution
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.files_presign import (
    _MAX_UPLOAD_BYTES,
    _PRESIGN_DEFAULT_EXPIRES_S,
    _PRESIGN_PENDING_CAP,
    _PRESIGN_RATE_LIMIT,
    _PRESIGN_RATE_WINDOW_S,
    PresignUploadEnvelope,
)
from bowhead_gateway.routes.kb import (
    _ALLOWED_MIMES,
    _BUNDLE_MAX_FILES,
    _BUNDLE_MAX_SIZE,
    _BUNDLE_MAX_UNCOMPRESSED,
    _SINGLE_MAX_SIZE,
    KbBundleSkippedItem,
    KbBundleUploadResponse,
    _bundle_skip,
    _classified_bundle_skip_code,
    _classify_zip_entry,
    _decode_zip_name,
    _enrich_dto,
    _get_artifact_store,
    _get_effective_worker,
    _get_files_store,
    _kb_error,
    _make_scope_context_checked,
    _require_ingestion_admission,
    _require_kb_scope_grant,
    _require_workspace_context,
    _resolve_kb_mime,
    _supports_kb_document_name,
    reparse_file,
)
from bowhead_gateway.skill_import_landing import finish_bundle_import, inventory_bundle_for_kb

if TYPE_CHECKING:
    from bowhead_core.user_store import User

    from bowhead_core import ArtifactStore

log = logging.getLogger(__name__)
router = APIRouter()


# ── DTOs ──────────────────────────────────────────────────────────


class KbPresignRequest(BaseModel):
    knowledge_intent: Literal["add_to_knowledge"]
    filename: str = Field(min_length=1, max_length=512)
    mime: str = Field(min_length=1, max_length=128)
    # Hand-rolled 413 in handler (matches Phase 1.4a P1-2 fix).
    size_bytes: int = Field(ge=1)
    scope: KbScopeIn
    scope_id: str = Field(min_length=1)
    doc_type_id: str | None = None
    use_llm: bool = False
    checksum_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")


class KbPresignResponse(BaseModel):
    file_id: str  # kbf_*
    artifact_id: str  # art_*
    upload: PresignUploadEnvelope
    expires_at: datetime
    confirm_url: str


class KbConfirmRequest(BaseModel):
    uploaded_size: int | None = None
    checksum_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")


# ── helpers ───────────────────────────────────────────────────────


def _backend(request: Request):
    b = getattr(request.app.state, "artifact_backend", None)
    if b is None:
        raise _kb_error(status.HTTP_503_SERVICE_UNAVAILABLE, "kb_service_unavailable")
    return b


async def _rate_limit_kb(request: Request, user_id: str) -> None:
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        return
    ok = await user_store.rate_limit_check(
        f"kb_presign:{user_id}",
        limit=_PRESIGN_RATE_LIMIT,
        window_s=_PRESIGN_RATE_WINDOW_S,
    )
    if not ok:
        raise _kb_error(429, "rate_limited")


def _new_artifact_id() -> str:
    return f"art_{uuid4().hex[:12]}"


async def _validate_doc_type(
    request: Request,
    doc_type_id: str | None,
    workspace_id: str,
) -> None:
    """Mirror existing /kb/files cross-workspace doc_type leak guard."""
    if not doc_type_id:
        return
    dt_store = getattr(request.app.state, "kb_doc_type_store", None)
    if dt_store is None:
        raise _kb_error(503, "kb_doc_type_service_unavailable")
    dt = await dt_store.get(doc_type_id)
    if dt is None:
        raise _kb_error(404, "doc_type_not_found")
    if dt.workspace_id != workspace_id:
        raise _kb_error(403, "doc_type_not_found")
    # kb-matrix M4 gate ① (wave-1 security review P2-1): presign is the third binding entry;
    # column-group templates/graduated snapshots cannot bind (same 422 as PATCH bind/upload).
    from bowhead_core.kb.doc_type_store import is_column_group_template as _is_tpl

    if _is_tpl(dt):
        raise _kb_error(422, "doc_type_not_bindable")


# ── presign (single doc) ──────────────────────────────────────────


@router.post("/api/v1/kb/presign", response_model=KbPresignResponse, dependencies=[Depends(require_not_readonly)])
async def kb_presign_single(
    body: KbPresignRequest,
    request: Request,
    user: User = Depends(_current_user),
    _feat: None = Depends(require_feature_licensed("kb")),  # S4: gate presign like direct upload POST /files
) -> KbPresignResponse:
    # Size cap hand-rolled (review P1-2 carry-forward)
    if body.size_bytes > _MAX_UPLOAD_BYTES:
        raise _kb_error(413, "kb_file_too_large")

    if not _supports_kb_document_name(body.filename) or body.mime not in _ALLOWED_MIMES:
        raise _kb_error(415, "kb_file_type_unsupported")

    _require_ingestion_admission()
    ctx = await _make_scope_context_checked(user, request)
    # ACL at presign — tech-design §5.1b
    if body.scope in ("space", "workspace"):
        _require_workspace_context(ctx)  # Batch 1/c4.
    if not KbPermission.can_write_to_scope(body.scope, body.scope_id, ctx, ctx.role):
        emit_audit(
            "kb_presign.scope_write_denied",
            {
                "user_id": user.id,
                "scope": body.scope,
                "scope_id": body.scope_id,
            },
        )
        raise _kb_error(403, "kb_scope_access_denied")
    await _require_kb_scope_grant(
        request,
        ctx,
        scope=body.scope,
        scope_id=body.scope_id,
        permission="write",
    )

    await _rate_limit_kb(request, user.id)
    await _validate_doc_type(request, body.doc_type_id, ctx.workspace_id)

    artifact_store: ArtifactStore = _get_artifact_store(request)
    backend = _backend(request)

    aid = _new_artifact_id()
    safe_name = _safe_filename(body.filename)
    presigned = await backend.presign_post(
        aid=aid,
        filename=safe_name,
        mime=body.mime,
        size_bytes=body.size_bytes,
        expires_in=_PRESIGN_DEFAULT_EXPIRES_S,
    )
    if presigned is None:
        if body.size_bytes > _SINGLE_MAX_SIZE:
            raise _kb_error(413, "kb_file_too_large")
        raise _kb_error(409, "file_multipart_required")

    files_store = _get_files_store(request)
    attribution = decide_for_kb_scope(body.scope, ctx.workspace_id)
    # ★ Retain the local name: downstream binding/kb_files consume the **original empty string**, not a sentinel (same as kb.py).
    workspace_id = attribution.scope_workspace_id
    log.info("%s.kb_presign_single reason=%s scope=%s", ATTRIBUTION_EVENT, attribution.reason, body.scope)
    now_iso = datetime.now(tz=UTC).isoformat(timespec="seconds")

    # Atomic: count pending + INSERT artifacts + INSERT kb_files in one txn.
    # mirrors files_presign._atomic_insert_pending but adds the kb_files write.
    async with _open_db(artifact_store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE created_by = ? AND status = 'pending_upload'",
                (user.id,),
            )
            row = await cur.fetchone()
            count = row[0] if row else 0
            if count >= _PRESIGN_PENDING_CAP:
                await db.execute("ROLLBACK")
                raise _kb_error(429, "file_presign_pending_limit")
            await db.execute(
                "INSERT INTO artifacts(id, filename, mime, size, source, path, "
                "canvas_id, created_by, created_at, status, expires_at, "
                "kind, scope, inject, trust, producer_kind, workspace_id) "
                "VALUES (?, ?, ?, ?, 'kb_upload', ?, NULL, ?, ?, 'pending_upload', ?, "
                "'user_upload', 'session', 'full', 'trusted', 'kb_upload', ?)",
                # Space isolation batch 4 ①: artifact_attribution.decide_for_kb_scope centrally decides attribution
                # (the sole implementation across Gateway). Empty strings **do not normalize to NULL**; store the explicit __team__ sentinel.
                # Batch 4 fails closed for NULL created after the cutover; retaining NULL would block all team-level KB uploads.
                (
                    aid,
                    safe_name,
                    body.mime,
                    body.size_bytes,
                    presigned.path,
                    user.id,
                    now_iso,
                    presigned.expires_at.isoformat(timespec="seconds"),
                    attribution.artifact_workspace_id,
                ),
            )
            await db.execute(
                "INSERT INTO artifact_upload_bindings(artifact_id, workspace_id, user_id, session_id, created_at) VALUES (?, ?, ?, NULL, ?)",
                (aid, workspace_id, user.id, now_iso),
            )
            await db.execute("COMMIT")
        except HTTPException:
            raise
        except Exception:
            await db.execute("ROLLBACK")
            raise

    # kb_files row separate — files_store has its own DB.
    # Phase 1.4b review P0 #3: create row with initial_parse_status atomically
    # so a crash between INSERT + UPDATE can't leave the row stuck in 'pending'.
    from bowhead_core.kb.ingestion import KnowledgeAction

    kbf_id, _, _ = await files_store.create_admitted(
        action=KnowledgeAction(f"presign:{aid}", "kb_ui", user.id, ctx.team_id, ctx.workspace_id),
        source_fingerprint=aid,
        artifact_id=aid,
        filename=safe_name,
        mime_type=body.mime,
        size_bytes=body.size_bytes,
        scope=body.scope,
        scope_id=body.scope_id,
        workspace_id=workspace_id,
        team_id=ctx.team_id,
        uploaded_by=user.id,
        use_llm_marker=body.use_llm,
        doc_type_id=body.doc_type_id,
        initial_parse_status="awaiting_upload",
    )

    emit_audit(
        "kb_presign.issued",
        {
            "file_id": kbf_id,
            "artifact_id": aid,
            "scope": body.scope,
            "size_bytes": body.size_bytes,
            "mime": body.mime,
        },
    )

    # Track **after success**: 429 (pending cap)/403/503/DB failures do not count (artifact_attribution module docstring ④).
    await track_attribution(request, route="kb_presign_single", reason=attribution.reason, scope=body.scope)
    return KbPresignResponse(
        file_id=kbf_id,
        artifact_id=aid,
        upload=PresignUploadEnvelope(
            url=presigned.url,
            fields=dict(presigned.fields),
        ),
        expires_at=presigned.expires_at,
        confirm_url=f"/api/v1/kb/presign/{kbf_id}/confirm",
    )


# ── confirm (single doc, CAS-guarded) ─────────────────────────────


@router.post(
    "/api/v1/kb/presign/{file_id}/confirm",
    response_model=KbFileDTO,
    dependencies=[Depends(require_not_readonly)],
)
async def kb_presign_confirm(
    file_id: str,
    body: KbConfirmRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(_current_user),
    _feat: None = Depends(require_feature_licensed("kb")),  # review-A: align confirm with presign initiator
) -> KbFileDTO:
    files_store = _get_files_store(request)

    # 1. Pull kb_files row + caller ACL re-check
    dto = await files_store.get(file_id)
    if dto is None or dto.soft_deleted_at:
        raise _kb_error(404, "kb_file_not_found")
    if dto.uploaded_by != user.id:
        # Another user's upload answers exactly like a missing one (no existence probe).
        log.info("kb_presign.confirm_concealed file=%s user=%s reason=not_uploader", file_id, user.id)
        raise _kb_error(404, "kb_file_not_found")

    ctx = await _make_scope_context_checked(
        user,
        request,
        workspace_id=dto.workspace_id if dto.scope in ("space", "workspace") else None,
    )
    # Re-check ACL — membership could have changed between presign and confirm
    if dto.scope in ("space", "workspace"):
        _require_workspace_context(ctx)  # Batch 1/c4.
    if not KbPermission.can_write_to_scope(dto.scope, dto.scope_id, ctx, ctx.role):
        emit_audit(
            "kb_presign.scope_revoked_at_confirm",
            {
                "file_id": file_id,
                "user_id": user.id,
                "scope": dto.scope,
                "scope_id": dto.scope_id,
            },
        )
        raise _kb_error(403, "kb_scope_access_denied")
    await _require_kb_scope_grant(
        request,
        ctx,
        scope=dto.scope,
        scope_id=dto.scope_id,
        permission="write",
    )

    artifact_store: ArtifactStore = _get_artifact_store(request)
    backend = _backend(request)

    # Phase 1.4b review P0 #2: doc_type re-check at confirm (defense against
    # workspace-revoke or doc_type-deletion between presign and confirm).
    await _validate_doc_type(request, dto.doc_type_id, ctx.workspace_id)

    # A failed upload can also be retried through confirm. Use the same source
    # probe, current-space permission checks and guarded enqueue as the retry API.
    if dto.parse_status == "failed":
        return await reparse_file(file_id, request, background_tasks, user)

    # Idempotent replay: kb_files row already past 'awaiting_upload' → already confirmed.
    if dto.parse_status != "awaiting_upload":
        if dto.parse_status in ("queued", "pending", "parsing", "chunking", "embedding", "done"):
            return _enrich_dto(dto, ctx)
        # Unknown state → 409
        raise _kb_error(409, "file_confirm_in_progress")

    # 2. Find linked artifact row
    artifact_id = dto.artifact_id
    async with _open_db(artifact_store.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, status, path, expires_at, created_by FROM artifacts WHERE id=?",
            (artifact_id,),
        )
        art_row = await cur.fetchone()
    if art_row is None:
        raise _kb_error(404, "kb_artifact_not_found")
    if art_row["created_by"] != user.id:
        raise _kb_error(404, "kb_artifact_not_found")

    # Expiry probe
    if art_row["expires_at"]:
        try:
            exp = datetime.fromisoformat(art_row["expires_at"])
        except ValueError:
            exp = None
        if exp and exp < datetime.now(tz=UTC):
            try:
                h = await backend.head(art_row["path"])
            except Exception as e:
                log.warning("kb_confirm.expiry_probe.head_failed art=%s err=%s", artifact_id, e)
                raise _kb_error(503, "kb_service_unavailable") from None
            if h is None:
                raise _kb_error(410, "file_presign_expired")

    if art_row["status"] == "ready":
        # Artifact already confirmed by a parallel call; just sync kb_files state
        pass
    elif art_row["status"] != "pending_upload":
        raise _kb_error(409, "file_confirm_in_progress")

    # 3. CAS UPDATE on artifacts (skip if already ready)
    if art_row["status"] == "pending_upload":
        async with _open_db(artifact_store.db_path) as db:
            cur = await db.execute(
                "UPDATE artifacts SET status='confirming' WHERE id=? AND status='pending_upload' AND created_by=?",
                (artifact_id, user.id),
            )
            rowcount = cur.rowcount
            await db.commit()
        if rowcount != 1:
            raise _kb_error(409, "file_confirm_in_progress")

        # 4. head() verify
        try:
            head = await backend.head(art_row["path"])
        except Exception as e:
            log.warning("kb_confirm.head_failed art=%s err=%s — rolling back", artifact_id, e)
            async with _open_db(artifact_store.db_path) as db:
                await db.execute(
                    "UPDATE artifacts SET status='pending_upload' WHERE id=? AND status='confirming'",
                    (artifact_id,),
                )
                await db.commit()
            raise _kb_error(503, "kb_service_unavailable") from None
        if head is None:
            async with _open_db(artifact_store.db_path) as db:
                await db.execute(
                    "UPDATE artifacts SET status='pending_upload' WHERE id=?",
                    (artifact_id,),
                )
                await db.commit()
            raise _kb_error(404, "file_upload_not_received")

        # MIME re-validation (tech-design §5.3 step 4)
        actual_mime = (head.mime or "").lower().split(";")[0].strip()
        if actual_mime and actual_mime != dto.mime_type.lower():
            async with _open_db(artifact_store.db_path) as db:
                await db.execute(
                    "UPDATE artifacts SET status='pending_upload' WHERE id=?",
                    (artifact_id,),
                )
                await db.commit()
            raise _kb_error(415, "kb_file_type_unsupported")

    # Phase 1.4b review P1 #4 + #9: resolve worker availability BEFORE
    # flipping any state (so a missing worker doesn't leave the row stuck);
    # do kb_files UPDATE BEFORE artifact UPDATE so if either fails the
    # recoverable state is "artifact 'confirming' + kb_files 'awaiting_upload'"
    # which our head() try/except can roll back, instead of "artifact 'ready' +
    # kb_files 'awaiting_upload' forever".
    try:
        worker = await _get_effective_worker(request, user)
    except HTTPException:
        # Worker missing — roll back CAS so user can retry confirm
        if art_row["status"] == "pending_upload":
            async with _open_db(artifact_store.db_path) as db:
                await db.execute(
                    "UPDATE artifacts SET status='pending_upload' WHERE id=? AND status='confirming'",
                    (artifact_id,),
                )
                await db.commit()
        raise

    # 5a. Flip kb_files.parse_status to 'queued' FIRST (recoverable failure
    # mode: artifact still in 'confirming' → next confirm CAS will succeed).
    async with aiosqlite.connect(files_store._db_path) as db:
        await db.execute(
            "UPDATE kb_files SET parse_status='queued', parse_heartbeat_at=? WHERE file_id=?",
            (heartbeat_iso(), file_id),
        )
        await db.commit()

    # 5b. Finalize artifact LAST
    if art_row["status"] == "pending_upload":
        async with _open_db(artifact_store.db_path) as db:
            await db.execute(
                "UPDATE artifacts SET status='ready', size=?, expires_at=NULL WHERE id=?",
                (head.size, artifact_id),
            )
            await db.commit()

    background_tasks.add_task(worker.parse_and_index, file_id)

    refreshed = await files_store.get(file_id)
    if refreshed is None:
        raise _kb_error(500, "kb_file_write_failed")

    emit_audit(
        "kb_presign.confirmed",
        {
            "file_id": file_id,
            "artifact_id": artifact_id,
            "scope": dto.scope,
            "size_bytes": refreshed.size_bytes,
        },
    )

    return _enrich_dto(refreshed, ctx)


# ── bundle presign (Phase 1.4c) ───────────────────────────────────


_BUNDLE_ZIP_MIMES = ("application/zip", "application/x-zip-compressed")

# Phase 1.4c review P1-1: per-entry decompressed size cap. Even when the
# aggregate cap is fine, a single entry exceeding this is likely a bomb.
_BUNDLE_MAX_ENTRY_DECOMPRESSED = 200 * 1024 * 1024  # 200 MB / entry

# Phase 1.4c review P1-2: compression ratio guard. Real-world ratios cap
# around 10:1; reject entries whose declared decompressed-to-compressed
# ratio exceeds this (likely zip-bomb).
_BUNDLE_MAX_COMPRESSION_RATIO = 100


class KbBundlePresignRequest(BaseModel):
    knowledge_intent: Literal["add_to_knowledge"]
    filename: str = Field(min_length=1, max_length=512)
    mime: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=1)
    scope: KbScopeIn
    scope_id: str = Field(min_length=1)
    use_llm: bool = False
    checksum_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")


class KbBundlePresignResponse(BaseModel):
    file_id: str  # bundle marker kbf_*
    artifact_id: str  # bundle staging art_*
    upload: PresignUploadEnvelope
    expires_at: datetime
    confirm_url: str


class KbBundleConfirmRequest(BaseModel):
    uploaded_size: int | None = None
    checksum_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")


@router.post(
    "/api/v1/kb/presign/bundle",
    response_model=KbBundlePresignResponse,
    dependencies=[Depends(require_not_readonly)],
)
async def kb_presign_bundle(
    body: KbBundlePresignRequest,
    request: Request,
    user: User = Depends(_current_user),
    _feat: None = Depends(require_feature_licensed("kb")),  # S4: gate presign like direct upload
) -> KbBundlePresignResponse:
    """Issue presigned-POST envelope for a KB zip bundle.

    Tech-design §5.1c: a "bundle marker" kb_files row is created at presign
    so the confirm step can re-derive scope/scope_id from DB (defense against
    scope-swap between presign and confirm). The marker is soft-deleted after
    confirm finishes successfully so it doesn't pollute /kb/files listings.
    """
    if body.mime not in _BUNDLE_ZIP_MIMES:
        raise _kb_error(415, "kb_bundle_required")
    if body.size_bytes > _BUNDLE_MAX_SIZE:
        raise _kb_error(413, "kb_bundle_too_large")

    _require_ingestion_admission()
    ctx = await _make_scope_context_checked(user, request)
    if body.scope in ("space", "workspace"):
        _require_workspace_context(ctx)  # Batch 1/c4.
    if not KbPermission.can_write_to_scope(body.scope, body.scope_id, ctx, ctx.role):
        emit_audit(
            "kb_presign_bundle.scope_write_denied",
            {
                "user_id": user.id,
                "scope": body.scope,
                "scope_id": body.scope_id,
            },
        )
        raise _kb_error(403, "kb_scope_access_denied")
    await _require_kb_scope_grant(
        request,
        ctx,
        scope=body.scope,
        scope_id=body.scope_id,
        permission="write",
    )

    await _rate_limit_kb(request, user.id)
    artifact_store: ArtifactStore = _get_artifact_store(request)
    files_store = _get_files_store(request)
    backend = _backend(request)

    aid = _new_artifact_id()
    safe_name = _safe_filename(body.filename)
    presigned = await backend.presign_post(
        aid=aid,
        filename=safe_name,
        mime=body.mime,
        size_bytes=body.size_bytes,
        expires_in=_PRESIGN_DEFAULT_EXPIRES_S,
    )
    if presigned is None:
        raise _kb_error(409, "file_multipart_required")

    attribution = decide_for_kb_scope(body.scope, ctx.workspace_id)
    # ★ Retain the local name (as above): binding/kb_files still consume the original empty string.
    workspace_id = attribution.scope_workspace_id
    log.info("%s.kb_presign_bundle reason=%s scope=%s", ATTRIBUTION_EVENT, attribution.reason, body.scope)
    now_iso = datetime.now(tz=UTC).isoformat(timespec="seconds")

    # Atomic INSERT (review P1-1 pattern from 1.4a) — count pending + INSERT artifact
    async with _open_db(artifact_store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE created_by = ? AND status = 'pending_upload'",
                (user.id,),
            )
            row = await cur.fetchone()
            count = row[0] if row else 0
            if count >= _PRESIGN_PENDING_CAP:
                await db.execute("ROLLBACK")
                raise _kb_error(429, "file_presign_pending_limit")
            await db.execute(
                "INSERT INTO artifacts(id, filename, mime, size, source, path, "
                "canvas_id, created_by, created_at, status, expires_at, "
                "kind, scope, inject, trust, producer_kind, workspace_id) "
                "VALUES (?, ?, ?, ?, 'kb_bundle_upload', ?, NULL, ?, ?, 'pending_upload', ?, "
                "'user_upload', 'session', 'full', 'trusted', 'kb_bundle_upload', ?)",
                # Space isolation batch 4 ①: as above, team-level bundle uploads store an explicit sentinel, not NULL.
                (
                    aid,
                    safe_name,
                    body.mime,
                    body.size_bytes,
                    presigned.path,
                    user.id,
                    now_iso,
                    presigned.expires_at.isoformat(timespec="seconds"),
                    attribution.artifact_workspace_id,
                ),
            )
            await db.execute(
                "INSERT INTO artifact_upload_bindings(artifact_id, workspace_id, user_id, session_id, created_at) VALUES (?, ?, ?, NULL, ?)",
                (aid, workspace_id, user.id, now_iso),
            )
            await db.execute("COMMIT")
        except HTTPException:
            raise
        except Exception:
            await db.execute("ROLLBACK")
            raise

    # Bundle marker kb_files row — carries scope/scope_id for confirm ACL check
    from bowhead_core.kb.ingestion import KnowledgeAction

    kbf_id, _, _ = await files_store.create_admitted(
        action=KnowledgeAction(f"presign:{aid}", "kb_ui", user.id, ctx.team_id, ctx.workspace_id),
        source_fingerprint=aid,
        artifact_id=aid,
        filename=safe_name,
        mime_type=body.mime,
        size_bytes=body.size_bytes,
        scope=body.scope,
        scope_id=body.scope_id,
        workspace_id=workspace_id,
        team_id=ctx.team_id,
        uploaded_by=user.id,
        use_llm_marker=body.use_llm,
        doc_type_id=None,
        initial_parse_status="awaiting_upload",
    )

    emit_audit(
        "kb_presign_bundle.issued",
        {
            "file_id": kbf_id,
            "artifact_id": aid,
            "scope": body.scope,
            "size_bytes": body.size_bytes,
        },
    )

    await track_attribution(request, route="kb_presign_bundle", reason=attribution.reason, scope=body.scope)
    return KbBundlePresignResponse(
        file_id=kbf_id,
        artifact_id=aid,
        upload=PresignUploadEnvelope(
            url=presigned.url,
            fields=dict(presigned.fields),
        ),
        expires_at=presigned.expires_at,
        confirm_url=f"/api/v1/kb/presign/bundle/{kbf_id}/confirm",
    )


# ── bundle confirm (Phase 1.4c) ───────────────────────────────────


@router.post(
    "/api/v1/kb/presign/bundle/{file_id}/confirm",
    response_model=KbBundleUploadResponse,
    dependencies=[Depends(require_not_readonly)],
)
async def kb_presign_bundle_confirm(
    file_id: str,
    body: KbBundleConfirmRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(_current_user),
    _feat: None = Depends(require_feature_licensed("kb")),  # review-A: align confirm with presign initiator
) -> KbBundleUploadResponse:
    """Confirm bundle upload, classify entries, fan out per-entry kb_files rows.

    Tech-design §5.3c:
    - head() verify the bundle object exists at the signed key.
    - backend.read_stream → ZipFile (no io.BytesIO heap blowup).
    - For each non-skipped entry: classify via _classify_zip_entry + MIME detect
      + zf.read(entry) → put_bytes → create child kb_files row → enqueue worker.
    - Soft-delete the bundle marker kb_files row so it doesn't show in listings.
    - Update bundle artifact → 'ready'.
    """
    import zipfile

    files_store = _get_files_store(request)

    # 1. Lookup bundle marker row + ACL recheck
    marker = await files_store.get(file_id)
    if marker is not None and marker.parse_status != "done" and not marker.ingestion_action_id:
        raise _kb_error(409, "knowledge_bundle_action_required", hint="Upload the bundle again from the Knowledge page to explicitly add its files.")
    if marker is None or marker.soft_deleted_at:
        raise _kb_error(404, "kb_file_not_found")
    if marker.uploaded_by != user.id:
        # Another user's upload answers exactly like a missing one (no existence probe).
        log.info("kb_presign.confirm_concealed file=%s user=%s reason=not_uploader", file_id, user.id)
        raise _kb_error(404, "kb_file_not_found")

    ctx = await _make_scope_context_checked(
        user,
        request,
        # Personal bundles also retain their upload Space when the browser switches.
        workspace_id=marker.workspace_id if marker.scope != "team" else None,
    )
    if not KbPermission.can_write_to_scope(marker.scope, marker.scope_id, ctx, ctx.role):
        emit_audit(
            "kb_presign_bundle.scope_revoked_at_confirm",
            {
                "file_id": file_id,
                "user_id": user.id,
                "scope": marker.scope,
                "scope_id": marker.scope_id,
            },
        )
        raise _kb_error(403, "kb_scope_access_denied")
    await _require_kb_scope_grant(
        request,
        ctx,
        scope=marker.scope,
        scope_id=marker.scope_id,
        permission="write",
    )

    artifact_store: ArtifactStore = _get_artifact_store(request)
    backend = _backend(request)

    # Idempotent replay: marker already past 'awaiting_upload' → bundle already
    # processed once. We don't retain the original response; just 409 so caller
    # knows not to double-process. (Bundle decompose is expensive — avoid races.)
    if marker.parse_status != "awaiting_upload":
        raise _kb_error(409, "file_confirm_in_progress")

    # 2. CAS bundle artifact pending_upload → confirming
    artifact_id = marker.artifact_id
    async with _open_db(artifact_store.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, status, path, expires_at, created_by FROM artifacts WHERE id=?",
            (artifact_id,),
        )
        art_row = await cur.fetchone()
    if art_row is None:
        raise _kb_error(404, "kb_artifact_not_found")
    if art_row["created_by"] != user.id:
        raise _kb_error(404, "kb_artifact_not_found")

    # Expiry probe
    if art_row["expires_at"]:
        try:
            exp = datetime.fromisoformat(art_row["expires_at"])
        except ValueError:
            exp = None
        if exp and exp < datetime.now(tz=UTC):
            try:
                h = await backend.head(art_row["path"])
            except Exception as e:
                log.warning("kb_bundle_confirm.expiry_probe.head_failed art=%s err=%s", artifact_id, e)
                raise _kb_error(503, "kb_service_unavailable") from None
            if h is None:
                raise _kb_error(410, "file_presign_expired")

    if art_row["status"] != "pending_upload":
        raise _kb_error(409, "file_confirm_in_progress")

    async with _open_db(artifact_store.db_path) as db:
        cur = await db.execute(
            "UPDATE artifacts SET status='confirming' WHERE id=? AND status='pending_upload' AND created_by=?",
            (artifact_id, user.id),
        )
        rowcount = cur.rowcount
        await db.commit()
    if rowcount != 1:
        raise _kb_error(409, "file_confirm_in_progress")

    # 3. head() verify object exists
    try:
        head = await backend.head(art_row["path"])
    except Exception as e:
        log.warning("kb_bundle_confirm.head_failed art=%s err=%s", artifact_id, e)
        async with _open_db(artifact_store.db_path) as db:
            await db.execute(
                "UPDATE artifacts SET status='pending_upload' WHERE id=? AND status='confirming'",
                (artifact_id,),
            )
            await db.commit()
        raise _kb_error(503, "kb_service_unavailable") from None
    if head is None:
        async with _open_db(artifact_store.db_path) as db:
            await db.execute(
                "UPDATE artifacts SET status='pending_upload' WHERE id=?",
                (artifact_id,),
            )
            await db.commit()
        raise _kb_error(404, "file_upload_not_received")

    # Phase 1.4c review P0-1 + P1-4: resolve worker BEFORE the read_stream
    # block. Same Phase 1.4a/b pattern — don't flip state then discover the
    # worker is missing. The artifact CAS is already set to 'confirming' by
    # the time we get here; if _get_worker raises HTTPException, roll back.
    try:
        worker = await _get_effective_worker(request, user)
    except HTTPException:
        async with _open_db(artifact_store.db_path) as db:
            await db.execute(
                "UPDATE artifacts SET status='pending_upload' WHERE id=? AND status='confirming'",
                (artifact_id,),
            )
            await db.commit()
        raise

    # Phase 1.4c review P0-4: track children we've created so a partial
    # failure can soft-delete them (prevents duplicates on retry).
    created_child_ids: list[str] = []

    # 4. backend.read_stream → ZipFile.infolist() with caps + classify pass
    try:
        async with backend.read_stream(art_row["path"]) as stream:
            try:
                zf = zipfile.ZipFile(stream, "r")
            except zipfile.BadZipFile as e:
                async with _open_db(artifact_store.db_path) as db:
                    await db.execute(
                        "UPDATE artifacts SET status='pending_upload' WHERE id=?",
                        (artifact_id,),
                    )
                    await db.commit()
                raise _kb_error(400, "kb_bundle_invalid") from e

            entries = zf.infolist()
            if len(entries) > _BUNDLE_MAX_FILES:
                async with _open_db(artifact_store.db_path) as db:
                    await db.execute(
                        "UPDATE artifacts SET status='pending_upload' WHERE id=?",
                        (artifact_id,),
                    )
                    await db.commit()
                raise _kb_error(413, "kb_bundle_too_many_entries")
            total_uncompressed = sum(e.file_size for e in entries)
            if total_uncompressed > _BUNDLE_MAX_UNCOMPRESSED:
                async with _open_db(artifact_store.db_path) as db:
                    await db.execute(
                        "UPDATE artifacts SET status='pending_upload' WHERE id=?",
                        (artifact_id,),
                    )
                    await db.commit()
                raise _kb_error(413, "kb_bundle_uncompressed_too_large")

            # MIME detection — magic optional, ext fallback always works
            try:
                import magic

                _magic_available = True
            except ImportError:
                _magic_available = False

            attribution = decide_for_kb_scope(marker.scope, ctx.workspace_id)
            # ★ Retain the local name (as above): binding/kb_files still consume the original empty string.
            workspace_id = attribution.scope_workspace_id
            log.info("%s.kb_presign_bundle_confirm reason=%s scope=%s", ATTRIBUTION_EVENT, attribution.reason, marker.scope)
            uploaded: list = []
            skipped: list[KbBundleSkippedItem] = []

            # skill-import batch 1b: inventory the whole bundle; capability components stay out of KB (decision ④). Failure ⇒ ingest as before.
            bundle_import = await inventory_bundle_for_kb(request, user, zf)

            for entry in entries:
                raw_name = entry.filename.encode("cp437") if not (entry.flag_bits & 0x800) else entry.filename.encode("utf-8")
                name = _decode_zip_name(raw_name)
                keep, reason = _classify_zip_entry(name, entry.file_size)
                if not keep:
                    if reason != "directory":
                        skipped.append(_bundle_skip(name, _classified_bundle_skip_code(reason)))
                    continue

                # Phase 1.4c review P1-1: per-entry decompressed cap. Reject
                # before reading so a bomb-claimed entry doesn't allocate.
                if entry.file_size > _BUNDLE_MAX_ENTRY_DECOMPRESSED:
                    skipped.append(_bundle_skip(name, "kb_bundle_entry_too_large"))
                    continue
                if bundle_import.is_capability_member(entry):
                    skipped.append(_bundle_skip(name, "kb_bundle_entry_capability_component"))
                    continue

                if not _supports_kb_document_name(name):
                    skipped.append(_bundle_skip(name, "kb_bundle_entry_type_unsupported"))
                    continue

                # Phase 1.4c review P1-2: compression-ratio guard.
                if entry.compress_size > 0:
                    ratio = entry.file_size / entry.compress_size
                    if ratio > _BUNDLE_MAX_COMPRESSION_RATIO:
                        skipped.append(_bundle_skip(name, "kb_bundle_entry_unsafe"))
                        continue

                # Phase 1.4c review P0-3: zf.read is sync + blocks the event
                # loop. Push to a worker thread.
                try:
                    payload = await asyncio.to_thread(zf.read, entry)
                except (zipfile.BadZipFile, RuntimeError):
                    skipped.append(_bundle_skip(name, "kb_bundle_entry_unreadable"))
                    continue
                # Hard cap actual decompressed bytes too — entry.file_size is
                # CLAIMED, not actual.
                if len(payload) > _BUNDLE_MAX_ENTRY_DECOMPRESSED:
                    skipped.append(_bundle_skip(name, "kb_bundle_entry_too_large"))
                    continue
                if len(payload) == 0:
                    skipped.append(_bundle_skip(name, "kb_bundle_entry_empty"))
                    continue

                # MIME detect: magic.from_buffer is CPU work (libmagic state
                # machine) — wrap in to_thread — then the shared ext-fallback
                # resolver (same as single-file /files + direct bundle).
                if _magic_available:
                    detected_mime = await asyncio.to_thread(
                        magic.from_buffer,
                        payload[:2048],
                        mime=True,
                    )
                else:
                    detected_mime = "application/octet-stream"
                detected_mime = _resolve_kb_mime(detected_mime, name)

                if detected_mime not in _ALLOWED_MIMES:
                    skipped.append(_bundle_skip(name, "kb_bundle_entry_type_unsupported"))
                    continue

                display_name = name.rsplit("/", 1)[-1]
                try:
                    art = await artifact_store.put_bytes(
                        content=payload,
                        filename=display_name,
                        mime=detected_mime,
                        source="kb_upload",
                        kind="user_upload",
                        producer_kind="kb_upload",
                        created_by=user.id,
                        # Space isolation batch 4 ①: this path previously **never passed** workspace_id, relying on put_bytes
                        # to infer it; bundle children have no session, turn, or canvas, so inference always fails
                        # and the **column** always stores NULL.
                        # ⚠ But post-cutover fail-closed rejection applies only to **team scope**, not all: below at :971,
                        # workspace_id is also written to artifact_upload_bindings, and read-side ladder ① uses
                        # NULLIF(b.workspace_id, ''): **space-scope** children already had resolvable attribution before this fix;
                        # team scope writes an empty string (falsy, erased by NULLIF), making it the actually unresolvable scope.
                        # marker.scope above is authoritative: pass it explicitly; team scope gets the sentinel, space scope the actual id.
                        workspace_id=attribution.artifact_workspace_id,
                    )
                    async with _open_db(artifact_store.db_path) as db:
                        await db.execute(
                            "INSERT OR IGNORE INTO artifact_upload_bindings("
                            "artifact_id,workspace_id,user_id,session_id,created_at) "
                            "VALUES (?,?,?,NULL,?)",
                            (
                                art.id,
                                workspace_id,
                                user.id,
                                datetime.now(tz=UTC).isoformat(timespec="seconds"),
                            ),
                        )
                        await db.execute(
                            "INSERT OR IGNORE INTO chat_attachment_bundle_members(bundle_artifact_id,child_artifact_id,created_at) VALUES (?,?,?)",
                            (
                                artifact_id,
                                art.id,
                                datetime.now(tz=UTC).isoformat(timespec="seconds"),
                            ),
                        )
                        await db.commit()
                    # Phase 1.4c review P0-2: children INSERT with
                    # parse_status='queued' per tech-design §5.3c step 5.
                    from bowhead_core.kb.ingestion import KnowledgeAction

                    current_ctx = await _make_scope_context_checked(
                        user, request, workspace_id=marker.workspace_id if marker.scope != "team" else None
                    )
                    if not KbPermission.can_write_to_scope(marker.scope, marker.scope_id, current_ctx, current_ctx.role):
                        raise _kb_error(403, "kb_scope_access_denied")
                    await _require_kb_scope_grant(request, current_ctx, scope=marker.scope, scope_id=marker.scope_id, permission="write")
                    child_id, _, _ = await files_store.create_admitted(
                        parent_file_id=file_id,
                        action=KnowledgeAction(f"bundle:{artifact_id}:{art.id}", "kb_ui", user.id, ctx.team_id, ctx.workspace_id),
                        source_fingerprint=art.id,
                        artifact_id=art.id,
                        filename=display_name,
                        mime_type=detected_mime,
                        size_bytes=len(payload),
                        scope=marker.scope,
                        scope_id=marker.scope_id,
                        workspace_id=workspace_id,
                        team_id=ctx.team_id,
                        uploaded_by=user.id,
                        use_llm_marker=marker.use_llm_marker,
                        doc_type_id=None,
                        initial_parse_status="queued",
                    )
                    created_child_ids.append(child_id)
                    emit_audit(
                        "kb_presign_bundle.entry_ingested",
                        {
                            "file_id": child_id,
                            "scope": marker.scope,
                            "size_bytes": len(payload),
                            "mime": detected_mime,
                            "bundle": marker.filename,
                        },
                    )
                    # NOTE: parse dispatch moved OUT of this loop — see fan-out
                    # below. Per-child add_task here would serialize all parses
                    # (FastAPI BackgroundTasks awaits sequentially).
                    child_dto = await files_store.get(child_id)
                    if child_dto:
                        uploaded.append(_enrich_dto(child_dto, ctx))
                except Exception:
                    skipped.append(_bundle_skip(name, "kb_bundle_entry_ingest_failed"))
            # skill-import batch 1b: still inside the stream scope; save skill components + inventory under the root artifact (itself fail-open).
            await finish_bundle_import(
                request,
                user,
                zf=zf,
                outcome=bundle_import,
                bundle_artifact_id=artifact_id,
                artifact_db_path=artifact_store.db_path,
                scope=marker.scope,
            )
    except HTTPException:
        # Phase 1.4c review P0-4: soft-delete children we created before the
        # failure so a retry doesn't see them as duplicates.
        for cid in created_child_ids:
            try:
                await files_store.soft_delete(cid)
            except Exception as _e:
                log.warning("kb_bundle_confirm.cleanup_failed file_id=%s err=%s", cid, _e)
        raise
    except Exception as e:
        # Stream/IO error — roll back CAS so retry is possible. Also soft-delete
        # any children we created during this partial run (P0-4).
        log.warning("kb_bundle_confirm.stream_failed art=%s err=%s", artifact_id, e)
        for cid in created_child_ids:
            try:
                await files_store.soft_delete(cid)
            except Exception as _e:
                log.warning("kb_bundle_confirm.cleanup_failed file_id=%s err=%s", cid, _e)
        async with _open_db(artifact_store.db_path) as db:
            await db.execute(
                "UPDATE artifacts SET status='pending_upload' WHERE id=?",
                (artifact_id,),
            )
            await db.commit()
        raise _kb_error(500, "kb_file_operation_failed") from None

    # 4b. Fan out parse for ALL children in ONE BackgroundTask → they run
    # CONCURRENTLY under worker._PARSE_SEMAPHORE (5-wide) instead of serially.
    # (Per-child add_task serializes: FastAPI BackgroundTasks awaits in order.)
    if created_child_ids:
        background_tasks.add_task(worker.parse_and_index_many, created_child_ids)

    # 5. Finalize: bundle artifact → ready, marker kb_files soft-deleted
    async with _open_db(artifact_store.db_path) as db:
        await db.execute(
            "UPDATE artifacts SET status='ready', size=?, expires_at=NULL WHERE id=?",
            (head.size, artifact_id),
        )
        await db.commit()
    now_iso = datetime.now(tz=UTC).isoformat(timespec="seconds")
    # Phase 1.4c review P1-5: marker is hidden from /kb/files via
    # soft_deleted_at filter. parse_layer remains None — adding 'bundle_root'
    # would require the same 3-file atomic Literal coordination (CHECK +
    # Pydantic + FE mirror) that Phase 1.3 already paid. Defer until/unless
    # an analytics query actually needs the distinction.
    async with aiosqlite.connect(files_store._db_path) as db:
        await db.execute(
            "UPDATE kb_files SET parse_status='done', soft_deleted_at=? WHERE file_id=?",
            (now_iso, file_id),
        )
        await db.commit()

    # The result UI counts uploaded + skipped files; directory entries have
    # neither state and must not inflate its progress denominator.
    total_files = sum(not entry.is_dir() for entry in entries)
    emit_audit(
        "kb_presign_bundle.confirmed",
        {
            "file_id": file_id,
            "artifact_id": artifact_id,
            "total_entries": total_files,
            "uploaded_count": len(uploaded),
            "skipped_count": len(skipped),
        },
    )

    # Confirm failures rolled back to pending_upload and retried reach here only on **final success**, preventing double counting.
    await track_attribution(request, route="kb_presign_bundle_confirm", reason=attribution.reason, scope=marker.scope)
    return KbBundleUploadResponse(
        uploaded=uploaded,
        skipped=skipped,
        total_entries=total_files,
        bundle_filename=marker.filename,
        bundle_artifact_id=artifact_id,
        bundle_mime=marker.mime_type,
        bundle_size=head.size,
        inventory=bundle_import.report_dict(),
        landed_skills=bundle_import.landed_skills,
    )
