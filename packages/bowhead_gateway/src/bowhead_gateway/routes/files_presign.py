"""Chat-attachment presigned-POST endpoints — Sprint file-upload-presign Phase 1.4a.

POST /api/v1/files/presign                            — issue signed envelope
POST /api/v1/files/presign/{file_id}/confirm         — finalize after browser PUT

KB single + bundle live in routes/kb.py (they share _current_user + scope ACL
pipelines that aren't relevant for chat attachments). See tech-design §5.1a +
§5.3a.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import aiosqlite
from bowhead_core.artifact_store import _open_db, _safe_filename
from bowhead_core.documents.package_io import MAX_OFFICE_BYTES, OFFICE_MIMES, OfficePackageError
from bowhead_core.documents.upload_confirmation import (
    UploadConfirmationError,
    confirm_upload,
    get_confirmation,
    has_confirmation_schema,
    register_confirmation,
)
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from bowhead_gateway.dtos import FileUploadDTO
from bowhead_gateway.routes.files import (
    MAX_SIZE,
    _file_denied,
    _file_error,
    _file_not_found,
    _require_request_workspace_access,
    _require_workspace_access,
    _user_id,
    resolve_effective_upload_mime,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bowhead_core.artifact_store import Artifact, ArtifactBackend

    from bowhead_core import ArtifactStore

from bowhead_gateway.artifact_attribution import ATTRIBUTION_EVENT, reason_for_resolved_attribution, track_attribution

log = logging.getLogger(__name__)

router = APIRouter()

# Sprint file-upload-presign Phase 1.4a constants.
_PRESIGN_RATE_LIMIT = 10  # per user per window
_PRESIGN_RATE_WINDOW_S = 60
_PRESIGN_PENDING_CAP = 20  # per-user pending_upload + confirming rows
_PRESIGN_DEFAULT_EXPIRES_S = 3600
_MAX_UPLOAD_BYTES = 2 * 1024**3  # 2 GB hard cap (tech-design §1)


# ── DTOs ──────────────────────────────────────────────────────────


class FilesPresignRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    mime: str = Field(min_length=1, max_length=128)
    # Phase 1.4a review P1-2: drop Pydantic `le=` so an oversize body returns
    # a hand-rolled HTTP 413 (matching tech-design §5.5 retry table) instead
    # of Pydantic's default 422. The handler enforces _MAX_UPLOAD_BYTES below.
    size_bytes: int = Field(ge=1)
    session_id: str | None = None
    # Phase 1.4a review P1-6: SHA-256 hex is conventionally case-insensitive.
    # Accept either case; handler normalizes to lowercase.
    checksum_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")


class PresignUploadEnvelope(BaseModel):
    """Wire shape mirrors `boto3.generate_presigned_post`.

    For LocalBackend dev, `fields` is consumed as HTTP HEADERS not multipart
    form fields (see routes/files_local_receiver.py for the receiver protocol).
    `method` always 'POST'.
    """

    url: str
    fields: dict[str, str]
    method: str = "POST"


class FilesPresignResponse(BaseModel):
    file_id: str
    artifact_id: str
    upload: PresignUploadEnvelope
    expires_at: datetime
    confirm_url: str


class FilesConfirmRequest(BaseModel):
    session_id: str | None = None
    uploaded_size: int | None = None
    # Phase 1.4a review P1-6: case-insensitive hex (normalize to lower in handler).
    checksum_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")


class FilesConfirmResponse(FileUploadDTO):
    """Confirm response extends FileUploadDTO with idempotent-replay flag."""

    status: str = "ready"
    idempotent_replay: bool = False


# ── helpers ───────────────────────────────────────────────────────


def _store(request: Request) -> ArtifactStore:
    s: ArtifactStore | None = getattr(request.app.state, "artifact_store", None)
    if s is None:
        raise _file_error(status.HTTP_503_SERVICE_UNAVAILABLE, "file_service_unavailable")
    return s


def _backend(request: Request):
    b = getattr(request.app.state, "artifact_backend", None)
    if b is None:
        raise _file_error(status.HTTP_503_SERVICE_UNAVAILABLE, "file_service_unavailable")
    return b


def _resolve_effective_mime(mime: str, filename: str) -> str | None:
    """Apply the same MIME normalization as /files/upload, return None if
    still not in ALLOWED_MIME after fallback."""
    return resolve_effective_upload_mime(mime, filename)


async def _rate_limit(request: Request, user_id: str) -> None:
    """Throw 429 if user exceeds presign rate. Uses user_store.rate_limit_check."""
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        # Dev / test path where user_store isn't set up — skip rate limit
        return
    ok = await user_store.rate_limit_check(
        f"presign:{user_id}",
        limit=_PRESIGN_RATE_LIMIT,
        window_s=_PRESIGN_RATE_WINDOW_S,
    )
    if not ok:
        raise _file_error(429, "file_presign_rate_limited")


async def _require_upload_space(
    request: Request,
    *,
    user_id: str,
    session_id: str | None,
) -> tuple[str, str | None]:
    """Resolve an optional persisted Session before admitting an upload write."""

    session_store = getattr(request.app.state, "session_store", None)
    if session_id:
        if session_store is None:
            raise _file_error(status.HTTP_403_FORBIDDEN, "file_upload_session_denied")
        try:
            session = await session_store.get_session(session_id)
        except Exception:
            session = None
        if session is None or str(getattr(session, "user_id", "") or "") != user_id:
            raise _file_error(status.HTTP_403_FORBIDDEN, "file_upload_session_denied")
        await _require_workspace_access(
            request,
            workspace_id=str(getattr(session, "workspace_id", "") or ""),
            user_id=user_id,
            operation=SpaceOperation.WRITE,
        )
        return str(getattr(session, "workspace_id", "") or ""), session_id
    await _require_request_workspace_access(
        request,
        operation=SpaceOperation.WRITE,
        user_id=user_id,
    )
    workspace_id = request.headers.get("X-Space-Id") or request.headers.get("X-Workspace-Id") or ""
    return workspace_id, session_id


async def _atomic_insert_pending(
    db_path: str,
    *,
    aid: str,
    filename: str,
    mime: str,
    size_bytes: int,
    backend_path: str,
    user_id: str,
    workspace_id: str,
    session_id: str | None,
    created_at_iso: str,
    expires_at_iso: str,
    confirmation_backend: ArtifactBackend | None = None,
    checksum_sha256: str | None = None,
) -> bool:
    """Phase 1.4a review P1-1: atomic SELECT-COUNT + INSERT inside
    BEGIN IMMEDIATE. Returns True on insert success, False if user is at
    _PRESIGN_PENDING_CAP. Eliminates the race where N concurrent presign
    calls all read count=cap-N and all INSERT.
    """
    async with _open_db(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE created_by = ? AND status IN ('pending_upload','confirming')",
                (user_id,),
            )
            row = await cur.fetchone()
            count = row[0] if row else 0
            if count >= _PRESIGN_PENDING_CAP:
                await db.execute("ROLLBACK")
                return False
            await db.execute(
                "INSERT INTO artifacts(id, filename, mime, size, source, path, "
                "canvas_id, created_by, created_at, status, expires_at, "
                "kind, scope, inject, trust, producer_kind, workspace_id) "
                "VALUES (?, ?, ?, ?, 'upload', ?, NULL, ?, ?, 'pending_upload', ?, "
                "'user_upload', 'session', 'full', 'trusted', 'upload', ?)",
                # Space isolation batch 3: write route's workspace_id (request-header space or session's own space),
                # also stored in artifact_upload_bindings in this transaction; no later backfill inference needed.
                # ⚠ 2026-09-21 correction: **both absent -> empty string -> NULL**. Earlier claim of an authoritative value
                #   contradicted implementation: frontend issueFilesPresign omits space header. Empty-string normalization to NULL means
                #   no space ownership, never ownership by space ''.
                (aid, filename, mime, size_bytes, backend_path, user_id, created_at_iso, expires_at_iso, workspace_id or None),
            )
            await db.execute(
                "INSERT INTO artifact_upload_bindings(artifact_id, workspace_id, user_id, session_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    aid,
                    workspace_id,
                    user_id,
                    session_id,
                    created_at_iso,
                ),
            )
            if confirmation_backend is not None:
                await register_confirmation(
                    db,
                    confirmation_backend,
                    artifact_id=aid,
                    quarantine_path=backend_path,
                    filename=filename,
                    mime=mime,
                    declared_size=size_bytes,
                    expected_sha256=checksum_sha256,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    expires_at=datetime.fromisoformat(expires_at_iso).timestamp(),
                )
            async with db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='account_owned_artifact_writes'") as cur:
                has_owned_writes = await cur.fetchone() is not None
            if has_owned_writes:
                await db.execute("DELETE FROM account_owned_artifact_writes WHERE write_id=?", (aid,))
            await db.execute("COMMIT")
            return True
        except Exception:
            await db.execute("ROLLBACK")
            raise


def _new_artifact_id() -> str:
    """Same shape as ArtifactStore.put_bytes: `art_{uuid4().hex[:12]}`."""
    return f"art_{uuid4().hex[:12]}"


async def confirm_multipart_office_upload(
    store: ArtifactStore,
    *,
    content: bytes,
    filename: str,
    mime: str,
    user_id: str,
    workspace_id: str,
    session_id: str | None,
    authorize: Callable[[], Awaitable[None]],
) -> Artifact:
    """Use the existing immutable confirmation pipeline for a multipart body."""
    artifact_id = _new_artifact_id()
    digest = hashlib.sha256(content).hexdigest()
    now = datetime.now(UTC)
    path = await store.put_account_owned_bytes(created_by=user_id, aid=artifact_id, content=content, filename=_safe_filename(filename), mime=mime)
    inserted = await _atomic_insert_pending(
        store.db_path,
        aid=artifact_id,
        filename=filename,
        mime=mime,
        size_bytes=len(content),
        backend_path=path,
        user_id=user_id,
        workspace_id=workspace_id,
        session_id=session_id,
        created_at_iso=now.isoformat(),
        expires_at_iso=(now + timedelta(seconds=_PRESIGN_DEFAULT_EXPIRES_S)).isoformat(),
        confirmation_backend=store.backend,
        checksum_sha256=digest,
    )
    if not inserted:
        await store.backend.delete(path)
        raise _file_error(status.HTTP_429_TOO_MANY_REQUESTS, "file_upload_pending_limit")
    try:
        await confirm_upload(
            store,
            artifact_id=artifact_id,
            user_id=user_id,
            session_id=session_id,
            checksum_sha256=digest,
            authorize=authorize,
        )
    except UploadConfirmationError as exc:
        raise _file_error(exc.status, exc.code) from exc
    artifact = await store.get(artifact_id)
    if artifact is None:
        raise _file_error(status.HTTP_503_SERVICE_UNAVAILABLE, "file_service_unavailable")
    return artifact


# ── presign ───────────────────────────────────────────────────────


@router.post("/api/v1/files/presign", response_model=FilesPresignResponse)
async def files_presign(
    body: FilesPresignRequest,
    request: Request,
) -> FilesPresignResponse:
    user_id = _user_id(request)
    workspace_id, session_id = await _require_upload_space(
        request,
        user_id=user_id,
        session_id=body.session_id,
    )
    await _rate_limit(request, user_id)

    # Phase 1.4a review P1-2: hand-rolled 413 (Pydantic field has no `le=`
    # so we can return spec'd 413 instead of generic 422).
    if body.size_bytes > _MAX_UPLOAD_BYTES:
        raise _file_error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file_too_large")

    # Normalize + whitelist MIME (mirrors /files/upload pipeline)
    try:
        effective_mime = _resolve_effective_mime(body.mime, body.filename)
    except OfficePackageError as exc:
        raise _file_error(415, exc.code) from None
    if effective_mime is None:
        raise _file_error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "file_type_unsupported")
    is_office = effective_mime in OFFICE_MIMES.values()
    if is_office:
        if body.size_bytes > MAX_OFFICE_BYTES:
            raise _file_error(413, "file_office_package_limit")
        async with _open_db(_store(request).db_path) as db:
            if not await has_confirmation_schema(db):
                raise _file_error(503, "file_service_unavailable")

    # Phase 1.4a review P1-5: sanitize filename for DB column too (path side
    # is sanitized by backend; DB column was previously stored raw, allowing
    # control chars / log-injection vectors).
    safe_name = _safe_filename(body.filename)

    store = _store(request)
    backend = _backend(request)
    aid = _new_artifact_id()
    presigned = await backend.presign_post(
        aid=aid,
        filename=safe_name,
        mime=effective_mime,
        size_bytes=body.size_bytes,
        expires_in=_PRESIGN_DEFAULT_EXPIRES_S,
    )
    if presigned is None:
        # No pending write exists: negotiate the supported authenticated transport.
        if body.size_bytes > MAX_SIZE:
            raise _file_error(413, "file_too_large")
        raise _file_error(409, "file_multipart_required")

    # Phase 1.4a review P1-1: atomic SELECT-COUNT + INSERT in BEGIN IMMEDIATE.
    # size=body.size_bytes is the CLAIMED size; confirm replaces with head().size.
    now_iso = datetime.now(tz=UTC).isoformat(timespec="microseconds")
    # The same resolved context supplies the artifact and its immutable upload binding.
    via_session_branch = bool(session_id)
    files_presign_reason = reason_for_resolved_attribution(workspace_id, via_explicit_space=not via_session_branch)
    log.info("%s.files_presign reason=%s", ATTRIBUTION_EVENT, files_presign_reason)
    inserted = await _atomic_insert_pending(
        store.db_path,
        aid=aid,
        filename=safe_name,
        mime=effective_mime,
        size_bytes=body.size_bytes,
        backend_path=presigned.path,
        user_id=user_id,
        workspace_id=workspace_id,
        session_id=session_id,
        created_at_iso=now_iso,
        expires_at_iso=presigned.expires_at.isoformat(timespec="seconds"),
        confirmation_backend=backend if is_office else None,
        checksum_sha256=body.checksum_sha256,
    )
    if not inserted:
        # P1-2 review note: signed envelope wasted; 1h TTL self-cleans. Log so
        # ops can correlate "presigned but capped" telemetry.
        log.info("files.presign.pending_cap_reject user=%s aid=%s", user_id, aid)
        raise _file_error(429, "file_presign_pending_limit")

    # Instrument **after successful insertion**: exclude 413/415/429/503 (artifact_attribution module docstring ④).
    await track_attribution(request, route="files_presign", reason=files_presign_reason)
    return FilesPresignResponse(
        file_id=aid,
        artifact_id=aid,
        upload=PresignUploadEnvelope(
            url=presigned.url,
            fields=dict(presigned.fields),
        ),
        expires_at=presigned.expires_at,
        confirm_url=f"/api/v1/files/presign/{aid}/confirm",
    )


# ── confirm (CAS-guarded) ─────────────────────────────────────────


@router.post(
    "/api/v1/files/presign/{file_id}/confirm",
    response_model=FilesConfirmResponse,
)
async def files_presign_confirm(
    file_id: str,
    body: FilesConfirmRequest,
    request: Request,
) -> FilesConfirmResponse:
    user_id = _user_id(request)
    store = _store(request)

    # 1. Fetch row + creator check before CAS so we can return idempotent_replay
    async with _open_db(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT a.id, a.filename, a.mime, a.size, a.path, a.status, "
            "a.created_by, a.expires_at, a.deleted_at, "
            "b.workspace_id AS binding_workspace_id, "
            "b.user_id AS binding_user_id, "
            "b.session_id AS binding_session_id "
            "FROM artifacts a "
            "LEFT JOIN artifact_upload_bindings b ON b.artifact_id = a.id "
            "WHERE a.id = ?",
            (file_id,),
        )
        row = await cur.fetchone()
    if row is None or row["deleted_at"] is not None:
        raise _file_not_found()
    if row["created_by"] != user_id:
        raise _file_denied(file_id, user_id, "not_owner")
    if row["binding_user_id"] and row["binding_user_id"] != user_id:
        raise _file_denied(file_id, user_id, "upload_binding_owner_mismatch")
    binding_session_id = row["binding_session_id"]
    if binding_session_id and body.session_id and binding_session_id != body.session_id:
        raise _file_error(409, "file_upload_session_conflict")
    final_session_id = binding_session_id or body.session_id
    binding_workspace_id = row["binding_workspace_id"] or ""
    request_workspace_id = request.headers.get("X-Space-Id") or request.headers.get("X-Workspace-Id") or ""
    if binding_workspace_id and request_workspace_id and request_workspace_id != binding_workspace_id:
        raise _file_error(409, "file_upload_session_conflict")
    # Recheck even a previously bound session. A late confirmation cannot pin to
    # another owner's session or use a new session to change the upload's Space.
    if final_session_id:
        session_workspace_id, _ = await _require_upload_space(request, user_id=user_id, session_id=final_session_id)
        if binding_workspace_id and session_workspace_id != binding_workspace_id:
            raise _file_error(409, "file_upload_session_conflict")
    if binding_workspace_id:
        await _require_workspace_access(
            request,
            workspace_id=binding_workspace_id,
            user_id=user_id,
            operation=SpaceOperation.WRITE,
        )
    else:
        await _require_request_workspace_access(request, operation=SpaceOperation.WRITE, user_id=user_id)
    backend = _backend(request)

    receipt = await get_confirmation(store.db_path, file_id)
    # Already admitted confirmations stay immutable even after flag rollback.
    # Historical ready artifacts remain ordinary files, without retroactive proof.
    pending_mime = None
    if row["status"] != "ready":
        try:
            pending_mime = _resolve_effective_mime(row["mime"], row["filename"])
        except OfficePackageError as exc:
            raise _file_error(415, exc.code) from None
    is_pending_office = pending_mime in OFFICE_MIMES.values()
    if receipt is not None or is_pending_office:

        async def authorize_office() -> None:
            resolved_workspace, _ = await _require_upload_space(request, user_id=user_id, session_id=final_session_id)
            if binding_workspace_id and resolved_workspace != binding_workspace_id:
                raise _file_error(403, "file_upload_session_denied")

        await authorize_office()
        if receipt is None:
            # Freeze old pending envelopes using the same primitive, never HEAD.
            async with _open_db(store.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                if not await has_confirmation_schema(db):
                    raise _file_error(503, "file_service_unavailable")
                cursor = await db.execute("SELECT status, path FROM artifacts WHERE id=? AND deleted_at IS NULL", (file_id,))
                current = await cursor.fetchone()
                if current is None or current[0] != "pending_upload" or current[1] != row["path"]:
                    raise _file_error(409, "file_confirm_in_progress")
                cursor = await db.execute("SELECT 1 FROM artifact_upload_confirmations WHERE artifact_id=?", (file_id,))
                if await cursor.fetchone() is None:
                    await register_confirmation(
                        db,
                        backend,
                        artifact_id=file_id,
                        quarantine_path=row["path"],
                        filename=row["filename"],
                        mime=pending_mime,
                        declared_size=row["size"],
                        expected_sha256=body.checksum_sha256,
                        user_id=user_id,
                        workspace_id=binding_workspace_id,
                        session_id=final_session_id,
                        expires_at=datetime.fromisoformat(row["expires_at"]).timestamp(),
                    )
                await db.commit()
        try:
            receipt, replay = await confirm_upload(
                store,
                artifact_id=file_id,
                user_id=user_id,
                session_id=final_session_id,
                checksum_sha256=body.checksum_sha256,
                authorize=authorize_office,
            )
        except UploadConfirmationError as exc:
            raise _file_error(exc.status, exc.code) from None
        except HTTPException:
            raise
        except Exception as exc:
            # Do not include package content, raw object keys, or provider errors.
            log.warning("files.office_confirmation_failed aid=%s error_type=%s", file_id, type(exc).__name__)
            raise _file_error(503, "file_service_unavailable") from None
        return FilesConfirmResponse(
            file_id=file_id,
            filename=receipt["filename"],
            size=receipt["actual_size"],
            mime=receipt["mime"],
            url=f"/api/v1/files/{file_id}",
            status="ready",
            idempotent_replay=replay,
        )

    # Idempotent replay: row already at 'ready' → return existing
    if row["status"] == "ready":
        return FilesConfirmResponse(
            file_id=row["id"],
            filename=row["filename"],
            size=row["size"],
            mime=row["mime"],
            url=f"/api/v1/files/{row['id']}",
            status="ready",
            idempotent_replay=True,
        )
    # status='confirming' means another concurrent confirm is in-flight.
    # Return 409 with retry hint; FE retry-policy table §5.5 reads this as
    # "wait then re-call confirm".
    if row["status"] != "pending_upload":
        raise _file_error(409, "file_confirm_in_progress")

    # Expiry guard (tech-design §5.3a step 6: 410 if expired and not yet uploaded)
    if row["expires_at"]:
        try:
            exp = datetime.fromisoformat(row["expires_at"])
        except ValueError:
            exp = None
        if exp and exp < datetime.now(tz=UTC):
            # Phase 1.4a review P0-1: wrap head() in try/except. If the backend
            # is unreachable here we cannot tell apart "no upload" from
            # "transient backend error", so surface 503 (retryable) not 410.
            try:
                h = await backend.head(row["path"])
            except Exception as e:
                log.warning("confirm.expiry_probe.head_failed aid=%s err=%s", file_id, e)
                raise _file_error(503, "file_presign_unavailable") from None
            if h is None:
                raise _file_error(410, "file_presign_expired")

    # 2. CAS UPDATE: only one concurrent confirm wins
    async with _open_db(store.db_path) as db:
        cur = await db.execute(
            "UPDATE artifacts SET status = 'confirming' WHERE id = ? AND status = 'pending_upload' AND created_by = ? AND deleted_at IS NULL",
            (file_id, user_id),
        )
        rowcount = cur.rowcount
        await db.commit()
    if rowcount != 1:
        # Lost the race — re-fetch and return idempotent_replay if now ready
        async with _open_db(store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, filename, mime, size, status FROM artifacts WHERE id = ? AND deleted_at IS NULL",
                (file_id,),
            )
            r2 = await cur.fetchone()
        if r2 and r2["status"] == "ready":
            return FilesConfirmResponse(
                file_id=r2["id"],
                filename=r2["filename"],
                size=r2["size"],
                mime=r2["mime"],
                url=f"/api/v1/files/{r2['id']}",
                status="ready",
                idempotent_replay=True,
            )
        raise _file_error(409, "file_confirm_in_progress")

    # 3. head() verify the object exists at the expected key.
    # Phase 1.4a review P0-1: any non-404 head() failure (auth, network,
    # timeout) would leave the CAS row stuck in 'confirming' forever (sweeper
    # only matches 'pending_upload'). Roll back CAS + surface 503 (retryable).
    try:
        head = await backend.head(row["path"])
    except Exception as e:
        log.warning("confirm.head_failed aid=%s err=%s — rolling CAS back", file_id, e)
        async with _open_db(store.db_path) as db:
            await db.execute(
                "UPDATE artifacts SET status='pending_upload' WHERE id=? AND status='confirming'",
                (file_id,),
            )
            await db.commit()
        raise _file_error(503, "file_presign_unavailable") from None
    if head is None:
        # Roll back CAS so a retry can succeed
        async with _open_db(store.db_path) as db:
            await db.execute(
                "UPDATE artifacts SET status = 'pending_upload' WHERE id = ?",
                (file_id,),
            )
            await db.commit()
        raise _file_error(404, "file_upload_not_received")

    # 4. MIME re-validation (tech-design §5.3a step 4: catch Content-Type swap)
    actual_mime = (head.mime or "").lower().split(";")[0].strip()
    presign_mime = row["mime"].lower()
    if actual_mime and actual_mime != presign_mime:
        # Roll back so user can presign again with the correct mime
        async with _open_db(store.db_path) as db:
            await db.execute(
                "UPDATE artifacts SET status = 'pending_upload' WHERE id = ?",
                (file_id,),
            )
            await db.commit()
        raise _file_error(415, "file_type_unsupported")

    # 5. Finalize: status='ready', actual size from head (object store truth).
    # Sprint pptx-from-template M1: a presigned upload IS a user file → type it
    # user_upload/full (inject) + producer_kind so it's richly available and
    # surfaces correctly in Assets. Columns guaranteed present by the ALTER-raise
    # migration (artifact_store.initialize).
    actual_size = head.size
    ready_at = datetime.now(tz=UTC).isoformat(timespec="microseconds")
    async with _open_db(store.db_path) as db:
        cursor = await db.execute(
            "UPDATE artifacts SET status = 'ready', size = ?, expires_at = NULL, "
            "kind = 'user_upload', inject = 'full', producer_kind = 'upload', created_at = ? "
            "WHERE id = ? AND status = 'confirming' AND deleted_at IS NULL",
            (actual_size, ready_at, file_id),
        )
        if cursor.rowcount != 1:
            raise _file_error(409, "file_confirm_in_progress")
        # Pin to session_attachments if session_id provided
        if final_session_id:
            try:
                await db.execute(
                    "INSERT INTO session_attachments(session_id, artifact_id, pinned_at, detached_at) "
                    "VALUES (?, ?, ?, NULL) "
                    "ON CONFLICT(session_id, artifact_id) DO UPDATE SET detached_at=NULL",
                    (
                        final_session_id,
                        file_id,
                        ready_at,
                    ),
                )
            except Exception as _e:
                log.warning(
                    "files_presign_confirm: session_attachments insert failed session=%s art=%s err=%s",
                    final_session_id,
                    file_id,
                    _e,
                )
        await db.commit()

    return FilesConfirmResponse(
        file_id=file_id,
        filename=row["filename"],
        size=actual_size,
        mime=row["mime"],
        url=f"/api/v1/files/{file_id}",
        status="ready",
        idempotent_replay=False,
    )
