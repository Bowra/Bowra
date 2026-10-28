"""Local presigned upload receiver — Sprint file-upload-presign Phase 1.2.

DEV-ONLY route. Mounted by main.py lifespan ONLY when
``settings.allow_local_receiver=True``. Prod startup asserts the flag is False
(prod uses S3Backend, no local-receiver path exists). See tech-design §6.3.

**Wire shape diverges from S3 path-style ``generate_presigned_post``**:
S3 receives multipart/form-data with policy+signature in form fields. Doing
the same in dev would require parsing multipart on the receiver — which
forces full-body buffering and defeats the streaming-write goal.

Instead, LocalBackend's ``presign_post()`` returns:
- ``url``: gateway receiver endpoint
- ``fields``: dict that the FE client uses as **HTTP HEADERS** on the POST
  (not multipart form fields). Includes ``Authorization: Bearer <jwt>``.
- Body: **raw file bytes** (no multipart wrapper).

FE upload helper branches on URL prefix: if ``/api/v1/files/local-receiver/``,
treat ``fields`` as headers + raw body; else FormData. This keeps S3 prod
path unchanged and gives dev a heap-flat upload route.

Flow:
  1. POST /api/v1/files/presign returns envelope w/ ``url``+``fields``.
  2. FE POSTs raw body to receiver with Authorization header.
  3. Receiver verifies JWT (aud=local-presign, aid match, exp), resolves
     target path with traversal guard, streams chunks via to_thread to a
     ``.partial`` with running byte counter, atomic ``os.replace`` on done.

Stays inside the gateway process BUT bypasses ``await file.read()`` — chunks
are written via ``asyncio.to_thread(open, ...)`` with no aiofiles dep
(closes review P0-D-2). Heap stays flat at chunk size (~64 KB).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import jwt
from bowhead_core.artifact_store import _open_db
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, HTTPException, Request, Response

from bowhead_gateway.routes.files import (
    _file_error,
    _require_workspace_access,
)

if TYPE_CHECKING:
    from bowhead_core.artifact_store import LocalBackend

log = logging.getLogger(__name__)

router = APIRouter()

_AID_RE = re.compile(r"^art_[a-f0-9]{12}$")
_JWT_AUD = "local-presign"


def _resolve_secret(request: Request) -> str:
    """Pull HS256 secret from app.state.local_presign_secret (set at lifespan)."""
    sec = getattr(request.app.state, "local_presign_secret", None)
    if not sec:
        log.error("local-receiver: secret unset; refusing")
        raise _file_error(503, "file_presign_unavailable")
    return str(sec)


def _resolve_root(request: Request) -> Path:
    """Pull LocalBackend.root_dir from app.state.artifact_backend."""
    backend: LocalBackend | None = getattr(request.app.state, "artifact_backend", None)
    if backend is None or getattr(backend, "name", None) != "local":
        raise _file_error(503, "file_presign_unavailable")
    return Path(backend.root_dir).resolve()


def _safe_filename_local(name: str, max_len: int = 200) -> str:
    """Mirror of artifact_store._safe_filename — JWT-bound, but defensive 2nd pass."""
    cleaned = re.sub(r"[/\\\x00-\x1f\x7f]+", "_", (name or "").strip()) or "upload"
    cleaned = cleaned.strip(".")
    if not cleaned:
        cleaned = "upload"
    return cleaned[:max_len]


async def _require_persisted_upload_lifecycle(
    request: Request,
    aid: str,
) -> None:
    artifact_store = getattr(request.app.state, "artifact_store", None)
    if artifact_store is None:
        return
    try:
        async with _open_db(artifact_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT workspace_id, user_id FROM artifact_upload_bindings WHERE artifact_id=?",
                (aid,),
            ) as cur:
                binding = await cur.fetchone()
    except aiosqlite.OperationalError:
        # Compatibility for an old dev database before the additive table.
        return
    if binding is None or not binding["workspace_id"]:
        return
    await _require_workspace_access(
        request,
        workspace_id=str(binding["workspace_id"]),
        user_id=str(binding["user_id"]),
        operation=SpaceOperation.WRITE,
    )


@router.post("/api/v1/files/local-receiver/{aid}")
async def local_receiver(aid: str, request: Request) -> Response:
    # 1. aid shape
    if not _AID_RE.fullmatch(aid):
        raise _file_error(403, "file_upload_authorization_failed")

    # 2. Pull token from Authorization header (header-only — calling
    #    request.form() would consume the streaming body before our chunk
    #    loop can run). FE upload helper sends header explicitly.
    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    if not token:
        raise _file_error(401, "file_upload_authorization_failed")

    # 3. JWT verify
    secret = _resolve_secret(request)
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience=_JWT_AUD,
        )
    except jwt.ExpiredSignatureError:
        # Per RFC 6750 + tech-design §5.5: expired token is 401 with
        # WWW-Authenticate hint, NOT 410. 410 would imply "gone forever";
        # the FE should re-presign and retry.
        error = _file_error(401, "file_presign_expired")
        error.headers = {
            "WWW-Authenticate": 'Bearer error="invalid_token", error_description="expired"',
        }
        raise error from None
    except jwt.InvalidTokenError:
        error = _file_error(401, "file_upload_authorization_failed")
        error.headers = {"WWW-Authenticate": 'Bearer error="invalid_token"'}
        raise error from None
    if payload.get("aid") != aid:
        raise _file_error(403, "file_upload_authorization_failed")

    filename = _safe_filename_local(str(payload.get("filename", "upload")))
    size_max = int(payload.get("size_max", 0))
    if size_max <= 0:
        raise _file_error(400, "invalid_file_upload_request")

    # Phase 1.4a review P1-3: enforce Content-Type binding so LocalBackend
    # matches the S3 `generate_presigned_post` Conditions security model.
    # Without this, a browser could presign for image/png + POST text/html and
    # the dev confirm path (where LocalBackend.head() returns mime=None) would
    # never detect the swap.
    expected_mime = str(payload.get("mime", ""))
    actual_mime = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if expected_mime and actual_mime and actual_mime != expected_mime.lower():
        raise _file_error(415, "file_type_unsupported")

    await _require_persisted_upload_lifecycle(request, aid)

    # 4. Resolve target with traversal guard
    root = _resolve_root(request)
    target = (root / f"{aid}_{filename}").resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise _file_error(403, "file_upload_authorization_failed") from None

    partial = target.with_suffix(target.suffix + ".partial")

    # 4b. Replay / clobber guard (review P1-S-1): refuse second-POST of a still-
    # valid JWT if the artifact (or an in-flight .partial) already exists.
    # 409 lets the FE distinguish "already uploaded, fetch metadata" from auth.
    if target.exists():
        raise _file_error(409, "file_upload_in_progress")
    if partial.exists():
        raise _file_error(409, "file_upload_in_progress")

    # 5. Stream chunks → .partial with running byte counter.
    # NOTE: this signature MUST NOT add File()/Form()/Body() params — FastAPI
    # would parse the body upfront and the stream loop would see nothing.
    # Auth stays header-only Bearer for the same reason (review P1-S-3).
    total = 0
    try:
        # Open in a worker thread (sync open() — no aiofiles dep)
        f = await asyncio.to_thread(partial.open, "wb")
        try:
            async for chunk in request.stream():
                # Starlette emits empty terminal chunk; skip
                if not chunk:
                    continue
                total += len(chunk)
                if total > size_max:
                    await asyncio.to_thread(f.close)
                    await asyncio.to_thread(partial.unlink, missing_ok=True)
                    raise _file_error(413, "file_too_large")
                await asyncio.to_thread(f.write, chunk)
        finally:
            await asyncio.to_thread(f.close)
    except HTTPException:
        # Cleanup before re-raising (size-overflow already cleaned up inline)
        await asyncio.to_thread(partial.unlink, missing_ok=True)
        raise
    except Exception as e:
        # Includes ClientDisconnect (mid-stream client drop): unlink partial
        # so a retry doesn't see ".partial exists" → 409 stale.
        await asyncio.to_thread(partial.unlink, missing_ok=True)
        log.error("local-receiver write failed aid=%s err=%s", aid, e)
        raise _file_error(500, "file_upload_write_failed") from None

    if total == 0:
        await asyncio.to_thread(partial.unlink, missing_ok=True)
        raise _file_error(400, "file_upload_empty")

    # 6. Atomic rename. partial + target share root_dir (same filesystem) so
    # POSIX os.replace is atomic.
    await asyncio.to_thread(os.replace, str(partial), str(target))

    return Response(status_code=204)
