"""Artifacts API — 上传 + 下载 + 管理.

POST   /api/v1/files/upload                 multipart/form-data field "file"
GET    /api/v1/files/                       list (q / mime_family / source / time / sort / pagination)
GET    /api/v1/files/{artifact_id}          下载（S3 → 302 presigned; local → FileResponse）
DELETE /api/v1/files/{artifact_id}          软删（置 deleted_at; 30 天内可 restore）
POST   /api/v1/files/{artifact_id}/restore  恢复软删
GET    /api/v1/files/{artifact_id}/usages   反查引用此 artifact 的 turn 列表

ArtifactStore 负责元数据；blob I/O 委托 backend（LocalBackend / S3Backend）.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import aiosqlite
import anyio
from bowhead_core.artifact_classifier import is_legacy_category, normalize_category
from bowhead_core.artifact_store import (
    TEAM_SCOPE_WORKSPACE_ID,
    _open_db,
    artifact_created_after_cutover,
    mime_to_family,
    read_artifact_workspace_cutover_at,
)
from bowhead_core.documents.package_io import (
    MAX_OFFICE_BYTES,
    OFFICE_MIMES,
    OfficePackageError,
    defer_office_content,
    inspect_office_package,
    office_upload_mime,
)
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse

from bowhead_gateway.access_context import (
    AccessContextError,
    access_context_to_dict,
    artifact_workspace_fail_close_enabled,
    build_access_context,
    has_workspace_access_source,
)
from bowhead_gateway.artifact_representations import ArtifactRepresentationDTO, Representation, build_representation, representation_error
from bowhead_gateway.dtos import (
    FileKbStatusDTO,
    FileListDTO,
    FileListItemDTO,
    FilePreviewDTO,
    FileUploadDTO,
    FileUsageEntryDTO,
    FileUsagesDTO,
)
from bowhead_gateway.kb_promotion import is_kb_promotable
from bowhead_gateway.media_preview import INLINE_MEDIA_MIMES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from bowhead_core import ArtifactStore

from bowhead_gateway.artifact_attribution import ATTRIBUTION_EVENT, reason_for_resolved_attribution, track_attribution
from bowhead_gateway.request_access_cache import request_with_cached_access

log = logging.getLogger(__name__)

router = APIRouter()

# Sprint ι (PRD §4.B.2 / tech-r2 §2.3): upload source allowlist.
# `python_sandbox` is the runner default for sandbox outputs (origin → "compute").
# bi_fetch_rows / kb_extracted_record bypass HTTP, writing via ArtifactStore.put_bytes; exclude from this list.
_ALLOWED_UPLOAD_SOURCES: frozenset[str] = frozenset(
    {
        "upload",
        "python_sandbox",
        "sandbox_output",
        "canvas_export",
    }
)

# Sprint pptx-from-template M1: asset kinds a caller may set on /upload (web_clip
# is written by web_search directly via put_bytes, never via this route).
_VALID_UPLOAD_KINDS: frozenset[str] = frozenset(
    {
        "user_upload",
        "tool_data",
        "sandbox_output",
        "deliverable",
        "canvas_export",
    }
)

ALLOWED_MIME = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "video/mp4",
    "video/webm",
    "video/ogg",
    "application/pdf",
    "text/plain",
    "text/csv",
    "text/tab-separated-values",
    "text/markdown",
    "text/html",
    "application/xhtml+xml",
    "application/json",
    "application/x-ndjson",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
    "application/zip",
    "application/x-zip-compressed",
}
MAX_SIZE = 100 * 1024 * 1024  # 100 MB
_OFFICE_INSPECTION_LIMITER = anyio.CapacityLimiter(2)

# Extension fallback: browsers / mimetypes often report .md / .jsonl / .tsv as "" or application/octet-stream.
# Map extensions to known MIME first, then enforce ALLOWED_MIME allowlist.
_EXT_FALLBACK_MIME: dict[str, str] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "application/xhtml+xml",
    ".zip": "application/zip",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".ogg": "video/ogg",
    # Sandbox/app images (python:3.12-slim) lack MIME tables; CPython's built-in table lacks OOXML, so uploads often
    # report Office files as octet-stream. Map extensions to known MIME, then enforce ALLOWED_MIME. Template formats
    # (.potx/.dotx) use the separate Office upload channel.
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_MIME_REQUIRED_EXTENSIONS: dict[str, frozenset[str]] = {
    "text/plain": frozenset({".txt"}),
    "text/markdown": frozenset({".md"}),
    "text/csv": frozenset({".csv"}),
    "text/tab-separated-values": frozenset({".tsv"}),
    "application/json": frozenset({".json"}),
    "application/x-ndjson": frozenset({".jsonl", ".ndjson"}),
    "text/html": frozenset({".html", ".htm"}),
    "application/xhtml+xml": frozenset({".xhtml"}),
    "video/mp4": frozenset({".mp4"}),
    "video/webm": frozenset({".webm"}),
    "video/ogg": frozenset({".ogv", ".ogg"}),
}
_BROWSER_AMBIGUOUS_MIME: frozenset[str] = frozenset(
    {
        "application/octet-stream",
        "text/plain",
    }
)


def resolve_effective_upload_mime(mime: str, filename: str) -> str | None:
    """Normalize browser MIME quirks and enforce data/text extension pairing."""
    office_mime = office_upload_mime(filename, mime)
    if office_mime is not None:
        return office_mime
    normalized = mime or "application/octet-stream"
    fname = (filename or "").lower()
    ext = next((e for e in _EXT_FALLBACK_MIME if fname.endswith(e)), "")
    html_mimes = {"text/html", "application/xhtml+xml"}
    html_extensions = {".html", ".htm", ".xhtml"}
    if normalized in html_mimes:
        required = _MIME_REQUIRED_EXTENSIONS[normalized]
        return normalized if ext in required else None
    if ext in html_extensions:
        # An explicit non-HTML declaration must not be upgraded to active HTML.
        # Only the generic browser fallback may be inferred from the extension.
        if normalized != "application/octet-stream":
            return None
    browser_jsonl_fallback = normalized == "application/json" and ext in {".jsonl", ".ndjson"}
    if ext and (normalized not in ALLOWED_MIME or normalized in _BROWSER_AMBIGUOUS_MIME or browser_jsonl_fallback):
        normalized = _EXT_FALLBACK_MIME[ext]
    if normalized not in ALLOWED_MIME:
        return None
    required = _MIME_REQUIRED_EXTENSIONS.get(normalized)
    if required and ext not in required:
        return None
    return normalized


def _store(request: Request) -> ArtifactStore:
    s: ArtifactStore | None = getattr(request.app.state, "artifact_store", None)
    if s is None:
        raise _file_error(status.HTTP_503_SERVICE_UNAVAILABLE, "file_service_unavailable")
    return s


def _file_error(status_code: int, code: str, **payload: Any) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, **payload}},
    )


def _file_not_found() -> HTTPException:
    """The single response for every per-id "you cannot see this file" case.

    Missing, soft-deleted for a non-owner, owned by someone else, not shared with the
    caller, or outside a Space the caller may read all return this exact 404 so the
    status code and body never reveal whether the id exists (same rule as the canvas
    ``report_unavailable`` contract). The real reason is logged server-side only.
    """
    return _file_error(status.HTTP_404_NOT_FOUND, "file_not_found")


def _file_denied(artifact_id: str, user_id: str, reason: str) -> HTTPException:
    """Record why an existing file was refused, then answer exactly like a missing one."""
    log.info("files.access_concealed aid=%s user=%s reason=%s", artifact_id, user_id, reason)
    return _file_not_found()


@contextmanager
def _concealing_denials(artifact_id: str, user_id: str) -> Iterator[None]:
    """Turn an authorization denial (403) raised inside the block into the not-found response.

    Lifecycle (409), re-authentication (401) and service (5xx) errors pass through: they
    describe the caller's own Space or session, not the file.
    """
    try:
        yield
    except HTTPException as exc:
        if exc.status_code != status.HTTP_403_FORBIDDEN:
            raise
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        error = detail.get("error") if isinstance(detail.get("error"), dict) else {}
        raise _file_denied(artifact_id, user_id, str(error.get("code") or "forbidden")) from None


def _require_file_owner(art: Any, user_id: str) -> None:
    """Gate a per-id mutation on the exact recorded owner.

    An owner-less artifact (empty ``created_by``: legacy rows and writes with no
    accountable user) stays readable within its Space but nobody may mutate it, so
    it answers exactly like a missing id. Reads keep their own owner/share checks.
    """
    if not art.created_by:
        raise _file_denied(art.id, user_id, "owner_less")
    if art.created_by != user_id:
        raise _file_denied(art.id, user_id, "not_owner")


async def _require_artifact_visible(
    request: Request,
    store: ArtifactStore,
    art: Any,
    *,
    operation: SpaceOperation,
    user_id: str | None = None,
) -> None:
    """`_require_artifact_operation` for per-id routes: a Space denial looks like "not found"."""
    caller = user_id or _user_id(request)
    if getattr(art, "producer_kind", None) == "template_document_v1":
        from bowhead_gateway.routes.documents import published_file_identity

        await published_file_identity(request, art, caller)
    with _concealing_denials(art.id, caller):
        await _require_artifact_operation(request, store, art, operation=operation, user_id=caller)


@router.post("/upload", response_model=FileUploadDTO)
async def upload(
    file: UploadFile,
    request: Request,
    source: str = Query("upload", description="upload | python_sandbox | sandbox_output | canvas_export"),
    # Sprint pptx-from-template M1 (RD-2): optional asset-model params for
    # callers that ALREADY have a session (sandbox-output upload via
    # runner._upload_artifact, admin/KB tools). The chat composer does NOT send
    # these (it uploads at attach-time, before a session exists) — its
    # durability comes from the turn-lifecycle auto-pin, not here.
    session_id: str | None = Query(None),
    kind: str | None = Query(None),
    created_by_turn: str | None = Query(None),
    derived_from: str | None = Query(None),
    manifest_artifact_id: str | None = Query(None),
) -> FileUploadDTO:
    artifact_fetch_user_id = _artifact_fetch_identity(request)
    await _require_request_workspace_access(
        request,
        operation=SpaceOperation.WRITE,
        user_id=artifact_fetch_user_id,
    )
    try:
        mime = resolve_effective_upload_mime(file.content_type or "application/octet-stream", file.filename or "")
    except OfficePackageError as exc:
        raise _file_error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, exc.code) from None
    # Sprint ι (PRD §4.B.2 / Phase B P2 carry from ι-12 review):
    # Source resolution = first valid candidate in [header, query] order;
    # invalid candidates are skipped (not silently swallowed) so a malicious
    # / typo header can't override a legitimate query value.
    header_source = (request.headers.get("X-Bowhead-Upload-Source") or "").lower()
    query_source = (source or "").lower()
    candidates = [c for c in (header_source, query_source) if c]
    effective_source = next(
        (c for c in candidates if c in _ALLOWED_UPLOAD_SOURCES),
        "upload",
    )
    # Log every rejected candidate to surface header / query mismatches.
    for c in candidates:
        if c not in _ALLOWED_UPLOAD_SOURCES:
            log.warning(
                "files.upload.unknown_source candidate=%s effective=%s",
                c,
                effective_source,
            )

    # TeX is admitted only as an authenticated run output, never by a caller's
    # source label or by relaxing ordinary browser upload MIME rules.
    tex_output = False
    if (file.filename or "").lower().endswith(".tex"):
        from bowhead_core.artifact_store import artifact_fetch_producer_output

        authorization = request.headers.get("Authorization", "")
        secret = getattr(request.app.state, "artifact_fetch_secret", "")
        scope = artifact_fetch_producer_output(str(secret), authorization[7:]) if secret and authorization.startswith("Bearer ") else None
        store = getattr(request.app.state, "session_store", None)
        session = await store.get_session(session_id) if store is not None and session_id else None
        turn = await store.get_turn(created_by_turn) if store is not None and created_by_turn else None
        if (
            artifact_fetch_user_id
            and scope is not None
            and session is not None
            and turn is not None
            and scope == {"session_id": session_id, "turn_id": created_by_turn, "workspace_id": session.workspace_id or ""}
            and session.user_id == artifact_fetch_user_id
            and turn.session_id == session_id
            and turn.status == "running"
            and effective_source in {"python_sandbox", "sandbox_output"}
            and (file.content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
            in {"application/x-tex", "text/x-tex", "application/octet-stream", "text/plain"}
        ):
            mime = "text/x-tex"
            tex_output = True
        else:
            raise _file_error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "file_type_unsupported")
    inspect_office = mime in OFFICE_MIMES.values()
    if mime not in ALLOWED_MIME and not inspect_office and not tex_output:
        raise _file_error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "file_type_unsupported")
    limit = MAX_OFFICE_BYTES if inspect_office else MAX_SIZE
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise _file_error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file_office_package_limit" if inspect_office else "file_too_large")
    if tex_output:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise _file_error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "file_type_unsupported") from None
        if not text.strip() or "\x00" in text:
            raise _file_error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "file_type_unsupported")
    inspection_authorizer = await _require_document_inspection_upload(
        request,
        filename=file.filename or "",
        mime=mime,
        content=content,
        source=source,
        session_id=session_id,
        created_by_turn=created_by_turn,
    )
    if inspect_office:
        try:
            await anyio.to_thread.run_sync(
                partial(inspect_office_package, content, filename=file.filename or "", declared_mime=mime),
                limiter=_OFFICE_INSPECTION_LIMITER,
            )
        except OfficePackageError as exc:
            raise _file_error(413 if exc.code == "file_office_package_limit" else 415, exc.code) from None
    # multimodal-v1 SEC-P1 (defense-in-depth): declared raster MIME must match real bytes.
    # ?disposition=inline trusts art.mime; browser content_type can be forged (HTML disguised as PNG).
    # nosniff protects modern browsers; block forgeries before persistence here. Strict checks for three raster types only;
    # other MIME behavior stays unchanged.
    if mime.lower() in ("image/png", "image/jpeg", "image/webp"):
        from bowhead_core.media.image_gen import sniff_image_mime

        _actual = sniff_image_mime(content)
        if _actual != mime.lower():
            raise _file_error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "file_type_unsupported")

    # Sprint pptx-from-template M1 review (P1 IDOR): if a session_id is supplied,
    # the caller MUST own that session — otherwise any authenticated user could
    # pin an attacker-controlled artifact into a victim's session, which the
    # per-turn attachment-injection path would then feed into the victim's Lead
    # prompt (prompt poisoning). Mirror the manual-pin authz (sessions.py).
    #
    # Feishu / tool-runtime sessions do not have a browser JWT, but the sandbox
    # runner does send created_by_turn. Allow only same-turn sandbox outputs:
    # turn exists, is still running, turn.session_id equals the requested
    # session_id, and the source is a sandbox source. This is narrower than trusting
    # session_id alone and keeps cross-session pinning closed.
    upload_user_id = artifact_fetch_user_id or _user_id(request)
    created_by_user_id = upload_user_id
    upload_workspace_id = request.headers.get("X-Space-Id") or request.headers.get("X-Workspace-Id") or ""
    if session_id:
        _ss = getattr(request.app.state, "session_store", None)
        if _ss is None:
            raise _file_error(status.HTTP_403_FORBIDDEN, "file_upload_session_denied")
        try:
            _sess = await _ss.get_session(session_id)
        except Exception:
            _sess = None
        if _sess is None:
            raise _file_error(status.HTTP_403_FORBIDDEN, "file_upload_session_denied")
        session_owner = str(getattr(_sess, "user_id", "") or "")
        owns_session = session_owner == upload_user_id
        same_turn_sandbox_output = False
        if not owns_session and created_by_turn and effective_source in {"python_sandbox", "sandbox_output"}:
            try:
                _turn = await _ss.get_turn(created_by_turn)
            except Exception:
                _turn = None
            same_turn_sandbox_output = (
                _turn is not None
                and str(getattr(_turn, "session_id", "") or "") == session_id
                and str(getattr(_turn, "status", "") or "") == "running"
            )
        if not (owns_session or same_turn_sandbox_output):
            raise _file_error(status.HTTP_403_FORBIDDEN, "file_upload_session_denied")
        await _require_workspace_access(
            request,
            workspace_id=str(getattr(_sess, "workspace_id", "") or ""),
            user_id=session_owner or upload_user_id,
            operation=SpaceOperation.WRITE,
        )
        if session_owner and artifact_fetch_user_id is None:
            created_by_user_id = session_owner
        upload_workspace_id = str(getattr(_sess, "workspace_id", "") or "")
    # M1 review (P2): validate kind against the known asset kinds — an arbitrary
    # kind would let a caller force inject='full' / trust escalation via the
    # _defaults_for_kind resolver. (web_clip is written by web_search directly,
    # never via /upload.)
    if kind and kind not in _VALID_UPLOAD_KINDS:
        raise _file_error(status.HTTP_400_BAD_REQUEST, "invalid_file_upload_request")

    artifact_store = _store(request)
    if manifest_artifact_id:
        manifest = await artifact_store.get(manifest_artifact_id)
        if manifest is None or manifest.created_by != created_by_user_id:
            # A public upload may only link provenance within its own ownership
            # boundary. Treat missing and foreign IDs identically so this
            # parameter cannot become an artifact-existence oracle.
            raise _file_error(status.HTTP_400_BAD_REQUEST, "invalid_file_upload_request")

    # Persist validated upload scope and admission proof with the artifact.
    # Runtime outputs retain their existing provenance and never become uploads.
    if inspect_office and effective_source == "upload" and not any((kind, created_by_turn, derived_from, manifest_artifact_id)):
        from bowhead_gateway.routes.files_presign import confirm_multipart_office_upload

        async def authorize_upload() -> None:
            await _require_workspace_access(request, workspace_id=upload_workspace_id, user_id=created_by_user_id, operation=SpaceOperation.WRITE)

        art = await confirm_multipart_office_upload(
            artifact_store,
            content=content,
            filename=file.filename or "upload",
            mime=mime,
            user_id=created_by_user_id,
            workspace_id=upload_workspace_id,
            session_id=session_id,
            authorize=authorize_upload,
        )
    else:
        art = await artifact_store.put_bytes(
            content=content,
            filename=file.filename or "upload",
            mime=mime,
            source=effective_source,
            # cca P3 F-B: a python_sandbox runner upload carries an artifact-fetch Bearer →
            # own the output as the real caller (sub), symmetric with the fetch side so a
            # chained sandbox output→input read matches. Normal uploads → JWT/header identity.
            created_by=created_by_user_id,
            workspace_id=upload_workspace_id or None,
            bind_chat_upload=effective_source == "upload",
            session_id=(session_id or None),
            kind=(kind or None),
            created_by_turn=(created_by_turn or None),
            derived_from=(derived_from or None),
            manifest_artifact_id=(manifest_artifact_id or None),
            authorize_in_connection=inspection_authorizer,
        )

    # ★★ Instrument **after writing**, reading art.workspace_id's **actual persisted value** (2026-09-19 review).
    #    Counting session presence measures **intent**. _resolve_write_workspace_id returns None for all of:
    #    absent session row / empty sessions.workspace_id / suppressed OperationalError.
    #    A session-bearing upload with failed resolution would count as SESSION while persisting NULL,
    #    underestimating NONE and overestimating SESSION, falsely implying unresolved attribution is rare enough to enable the gate.
    # ⚠ 2026-09-19 device observation: all three frontend callers (file library / settings templates / chat attachments) **omit session**;
    #   attribution is always NULL here, accounting for 116's sole C4 NULL row.
    files_upload_reason = reason_for_resolved_attribution(getattr(art, "workspace_id", None))
    log.info("%s.files_upload reason=%s source=%s", ATTRIBUTION_EVENT, files_upload_reason, effective_source)
    # source (4 allowlisted values) must enter attrs: sandbox runner POSTs **every output file** here
    # (python_sandbox / sandbox_output). Without it, machine outputs swamp reason=none's share,
    # hiding the true user-upload proportion.
    await track_attribution(request, route="files_upload", reason=files_upload_reason, source=effective_source)

    # Fire-and-forget classification + D7 auto-indexing, without blocking upload response.
    #
    # file-management-v2 P1: kind gating. Previously classification ran on **every** /upload indiscriminately,
    # spending LLM calls on sandbox returns despite no runtime-data display value for classification results.
    # Classify only kinds that may enter the asset library / KB.
    # Batch 2-a site 6: previously gated on process-assembled router. Resolve the team router inside background task
    # via art.created_by -> team_id (/upload has no user:User=Depends(...), so cannot obtain team_id,
    # but created_by is already available and used at :341-345).
    _classify_kinds = {"user_upload", "deliverable"}
    if (getattr(art, "kind", None) or "tool_data") in _classify_kinds and not defer_office_content(filename=art.filename, mime=art.mime):
        import asyncio as _aio

        _bg = _aio.create_task(_classify_uploaded_artifact(request.app, art))
        _bg_tasks = getattr(request.app.state, "_classify_bg_tasks", None)
        if _bg_tasks is None:
            _bg_tasks = set()
            request.app.state._classify_bg_tasks = _bg_tasks
        _bg_tasks.add(_bg)
        _bg.add_done_callback(_bg_tasks.discard)

    return FileUploadDTO(
        file_id=art.id,
        filename=art.filename,
        size=art.size,
        mime=art.mime,
        url=f"/api/v1/files/{art.id}",
    )


async def _classify_uploaded_artifact(app, art) -> None:  # type: ignore[no-untyped-def]
    """Classify uploaded files for display; classification grants no knowledge action."""
    # Upload is not a template/reference declaration. Defer both LLM
    # classification and automatic indexing; explicit reads/manual promotion
    # remain separate authorized operations.
    if defer_office_content(filename=art.filename, mime=art.mime):
        return

    from bowhead_core.artifact_classifier import classify_artifact

    from bowhead_gateway.routes._deps import resolve_team_router

    store = app.state.artifact_store

    # Batch 2-a site 6: resolve in background, off the upload hot path. Missing identity returns immediately, degrading to
    # **no LLM call**, never calling a router certain to refuse. Sandbox return identity failing to resolve
    # team_id also follows this path.
    uploader_id = getattr(art, "created_by", None) or ""
    team_id = None
    if uploader_id:
        try:
            _u = await app.state.user_store.get_user(uploader_id)
            team_id = getattr(_u, "team_id", None) if _u else None
        except Exception:
            log.warning("files.classify_uploader_lookup_failed aid=%s", art.id, exc_info=True)
    router, router_miss = await resolve_team_router(app.state, team_id, agent_type="echo", site="artifact_classify")
    if router is None:
        # **Do not return early** (implementation review P1): classify_artifact's image/short-text rule branches
        # need no model; returning would remove user-visible category entirely for uploads from unconfigured teams.
        # Pass None so only its LLM branch is skipped.
        log.info("files.classify_router_unavailable aid=%s reason=%s", art.id, router_miss.value if router_miss else "?")

    from bowhead_router.usage import usage_context

    try:
        # Batch 2-a §3.6 attribution: this path never successfully called LLM in prod; after fixing it, real token spend
        # starts for the first time. Without usage_context it cannot be attributed to the team.
        with usage_context(team_id=team_id, user_id=uploader_id, reason="artifact_classify"):
            await classify_artifact(artifact=art, store=store, router=router)
    except Exception as e:  # noqa: BLE001
        log.warning("files.classify_failed aid=%s error=%s", art.id, e)
        return


def _user_id(request: Request) -> str:
    """P4.5.17: JWT > X-Bowhead-User-Id header > dev_default_user."""
    from bowhead_gateway.routes.auth import resolve_user_id_sync

    return resolve_user_id_sync(request)


async def _list_shared(
    request: Request,
    store: ArtifactStore,
    ids: list[str],
    *,
    limit: int,
    offset: int,
) -> FileListDTO:
    """P3：按 grant 到的 id 集取 artifact（owner 是别人，走不了 list_for_user）。

    分页在 id 集上做——grant 集合通常远小于全量 artifact，一次取完再切片
    比每页回查授权便宜，也避免"翻页时权限变了导致行数跳动"。
    """
    arts = await store.get_many(ids)  # Deleted/hard-deleted grant remnants are absent.
    visible_ids = await _history_visible_ids(request, store, arts, user_id=_user_id(request))
    visible = [(art.id, art) for art in arts if art.id in visible_ids]
    page = visible[offset : offset + limit]
    page_ids = [aid for aid, _art in page]
    items: list[FileListItemDTO] = []
    kb_ids, kb_status = await _kb_overlays(request, page_ids)
    try:
        origins = await store.origin_sessions(page_ids)
    except Exception:  # noqa: BLE001
        origins = {}
    trust_by_id = await _export_trust_projection(
        request,
        store,
        [art for _aid, art in page],
    )
    usage = await store.usage_counts(page_ids)
    total_bytes = 0
    for aid, art in page:
        total_bytes += art.size
        dto = _art_to_dto(
            art,
            usage.get(aid, 0),
            in_kb=aid in kb_ids,
            kb=kb_status.get(aid),
            origin_session=origins.get(aid),
            trust_override=trust_by_id.get(aid),
        )
        # Share recipient is read-only; frontend hides delete/recategorize/add-to-KB accordingly.
        dto.shared_readonly = True
        items.append(dto)
    return FileListDTO(
        total=len(visible),
        size_total_bytes=total_bytes,
        items=items,
    )


class _ShareCtx(NamedTuple):
    """分享/共享可见性判定所需的 principal 链。"""

    user_id: str
    team_id: str
    workspace_id: str
    group_ids: tuple[str, ...]


async def _share_ctx(
    request: Request,
    *,
    operation: SpaceOperation = SpaceOperation.WRITE,
) -> _ShareCtx:
    """解析 caller 的 principal 链（user / group / workspace / team）。

    **空间必须验成员资格，不能只读 header。** `X-Space-Id` 是客户端可自由填写的
    字段；空间级 grant 恰恰是按它匹配的，光信 header 等于「同团队任意成员填上某个
    空间 id 就能读到分享进该空间的全部文件」——一条团队内的越权路径。因此走
    `build_access_context`（KB 的 `_require_workspace_member` 同一把锁）：它校验
    空间归属本团队、caller 在该空间有角色，并顺带给出权威 team_id 与 group_ids。

    `allow_missing_workspace=True`：不带空间 header 也合法（只看团队级分享），
    此时 workspace principal 不入链。缺 RBAC hook 的旧测试 harness 退回轻量路径
    ——与 `_require_workspace_member` 的取舍一致。
    """
    user_id = _user_id(request)
    workspace_id = request.headers.get("X-Space-Id") or request.headers.get("X-Workspace-Id") or ""
    if user_id and has_workspace_access_source(request):
        try:
            ctx = await build_access_context(
                user_store=request.app.state.user_store,
                access_store=getattr(request.app.state, "access_control_store", None),
                user_id=user_id,
                workspace_id=workspace_id,
                allow_missing_workspace=True,
                operation=operation,
            )
        except AccessContextError as exc:
            code = "workspace_access_denied" if exc.code.startswith("ws_access_denied:") else exc.code
            raise _file_error(exc.status_code, code) from exc
        return _ShareCtx(user_id, ctx.team_id, ctx.workspace_id, tuple(ctx.group_ids))

    team_id = ""
    ustore = getattr(request.app.state, "user_store", None)
    if ustore is not None and user_id:
        try:
            u = await ustore.get_user(user_id)
            team_id = getattr(u, "team_id", "") or ""
        except Exception as e:  # noqa: BLE001 - Missing team means no sharing; never admit.
            log.warning("files.share_ctx_failed user=%s error=%s", user_id, e)
    return _ShareCtx(user_id, team_id, workspace_id, ())


async def _require_workspace_access(
    request: Request,
    *,
    workspace_id: str,
    user_id: str,
    operation: SpaceOperation,
) -> None:
    """STRONG 闸的文件侧包装: `build_access_context` = 成员校验 + 生命周期。

    ⚠ 名字里的 "access" 是认真的 —— 它**不是**只查生命周期。
    ⚠ `workspace_id` 推导不出来时下面这条早退是 fail-open(推不出归属 ⇒ 根本不进闸),
    属已知缺口, 由空间隔离 sprint 的产物归属批次收敛, 不要当成「这里没有闸」。
    """

    if not workspace_id or not user_id or not has_workspace_access_source(request):
        return
    try:
        await build_access_context(
            user_store=request.app.state.user_store,
            access_store=getattr(request.app.state, "access_control_store", None),
            user_id=user_id,
            workspace_id=workspace_id,
            operation=operation,
        )
    except AccessContextError as exc:
        code = "workspace_access_denied" if exc.code.startswith("ws_access_denied:") else exc.code
        raise _file_error(exc.status_code, code) from exc


async def _require_request_workspace_access(
    request: Request,
    *,
    operation: SpaceOperation,
    user_id: str | None = None,
) -> None:
    workspace_id = request.headers.get("X-Space-Id") or request.headers.get("X-Workspace-Id") or ""
    if not workspace_id:
        return
    await _require_workspace_access(
        request,
        workspace_id=workspace_id,
        user_id=user_id or _user_id(request),
        operation=operation,
    )


async def _artifact_workspace_id(
    request: Request,
    store: ArtifactStore,
    art: Any,
) -> str:
    """Resolve an artifact's originating Space from persisted provenance.

    ★ 批4：**先读列、为 NULL 再回落推导**。列是写入那一刻由权威上下文落的，下面的梯子
    是事后反推；两者不一致时列更可信（例如 bundle 子件：列里有 `__team__`，而梯子恒推不出）。

    ⚠ 读列这一步**挂在 flag 上**，不是无条件的。闸关时必须逐字节等于今天的行为，
    而今天这个函数**根本不看这一列** —— 无条件读列会让「只有列、梯子推不出」的产物
    在闸关时也被拦，那是偷偷收紧，不是本批的承诺。
    """

    return (await _artifact_workspace_ids(request, store, [art])).get(art.id, "")


_LOOKUP_CHUNK = 500  # Well below SQLite's parameter limit (999 in old versions).
# After two failed batch lookups, retry individually only at or below this chunk size; otherwise raise.
_PER_ITEM_RETRY_MAX = 20


async def _chunked_lookup(
    ids: list[str],
    *,
    batch: Callable[[list[str]], Awaitable[dict[str, Any]]],
    single: Callable[[str], Awaitable[Any]],
    what: str,
) -> dict[str, Any]:
    """归属梯子每一级共用的批量查法：``{id: 值}``，查不到的 id 不在字典里（单条版返回 None 同义）。

    分块 ``IN`` 查询；某一块失败先整块再试一次（瞬时 ``database is locked``）。仍失败时：
    - 块不大于 ``_PER_ITEM_RETRY_MAX`` ⇒ 逐个查单条版，一个出错只让**它自己**推不出（与单条版同义：闸关时
      「推不出」是 fail-open）——整块吞掉会把一批产物一起变成「推不出」；
    - 块更大 ⇒ **直接报错**。库本身出问题（锁死 / 句柄耗尽 / I/O）时逐个重试就是上千次开库，正是 2026-09-24
      116 事故的形状：在库最吃紧时把负载放大几百倍。这一档报错是 fail-closed（调用方整体失败，不是放行）。
    """
    out: dict[str, Any] = {}
    for start in range(0, len(ids), _LOOKUP_CHUNK):
        chunk = ids[start : start + _LOOKUP_CHUNK]
        if len(chunk) > 1:
            found: dict[str, Any] | None = None
            for attempt in (1, 2):  # Retry the entire failed batch once.
                try:
                    found = await batch(chunk)
                    break
                except Exception as exc:
                    log.warning("files.%s_batch_lookup_failed n=%d attempt=%d error=%s", what, len(chunk), attempt, type(exc).__name__)
                    if attempt == 2 and len(chunk) > _PER_ITEM_RETRY_MAX:
                        raise
            if found is not None:
                out.update(found)
                continue
        for key in chunk:
            try:
                value = await single(key)
            except Exception as exc:  # noqa: BLE001 —— Same as single lookup: only this item cannot resolve.
                log.warning("files.%s_lookup_failed id=%s error=%s", what, key, type(exc).__name__)
                continue
            if value is not None:
                out[key] = value
    return out


async def _upload_binding_workspaces(store: ArtifactStore, artifact_ids: list[str]) -> dict[str, str]:
    """梯子第一级：上传时显式登记的空间（``{artifact_id: workspace_id}``，只含非空）。表不存在（尚无该表的老开发库）照旧整级跳过。"""

    async def query(ids: list[str]) -> dict[str, str]:
        placeholders = ",".join("?" for _ in ids)
        try:
            async with (
                _open_db(store.db_path) as db,
                db.execute(
                    f"SELECT artifact_id, workspace_id FROM artifact_upload_bindings WHERE artifact_id IN ({placeholders})",  # noqa: S608 - placeholders only
                    ids,
                ) as cur,
            ):
                return {str(artifact_id): str(workspace_id) for artifact_id, workspace_id in await cur.fetchall() if workspace_id}
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc):
                return {}  # Compatibility for an old dev database before the additive table.
            raise

    async def one(artifact_id: str) -> str | None:
        return (await query([artifact_id])).get(artifact_id)

    return await _chunked_lookup(artifact_ids, batch=query, single=one, what="upload_binding")


def _session_id_of(get_row: Callable[[str], Awaitable[Any]]) -> Callable[[str], Awaitable[Any]]:
    async def one(key: str) -> Any:
        return getattr(await get_row(key), "session_id", None)

    return one


def _workspace_id_of(get_row: Callable[[str], Awaitable[Any]]) -> Callable[[str], Awaitable[Any]]:
    async def one(key: str) -> Any:
        return getattr(await get_row(key), "workspace_id", None)

    return one


async def _artifact_workspace_ids(
    request: Request,
    store: ArtifactStore,
    arts: list[Any],
) -> dict[str, str]:
    """`_artifact_workspace_id` 的批量版——**同一条梯子的唯一 Python 实现**，单条版就是它的一个特例。

    每一级的判定照旧，只是把「每个产物各查一遍」改成按**去重后的 id** 分块批量查（``_chunked_lookup``）：
    绑定表、turn、报告、pin 来源、会话→空间各一级。开库次数与产物数无关（每 500 个 id 一次）。
    列表类调用方（全局搜索、用量）一次要判几十到上万个产物；116 上逐个查一次请求 ≈4 万次开库（2026-09-24）。
    ⚠ 批量失败时的重试规则见 ``_chunked_lookup``：小块逐个重试（一个故障不放大成整批「推不出」，那一档会
      fail-open），大块直接报错（不把一次库故障放大成上千次开库）。
    """

    out: dict[str, str] = {}
    pending: list[Any] = []
    fail_close = artifact_workspace_fail_close_enabled()
    for art in arts:
        column = str(getattr(art, "workspace_id", "") or "").strip() if fail_close else ""
        if column:
            out[art.id] = column
        else:
            pending.append(art)
    if not pending:
        return out

    bindings = await _upload_binding_workspaces(store, [art.id for art in pending])
    unresolved: list[Any] = []
    for art in pending:
        if art.id in bindings:
            out[art.id] = bindings[art.id]
        else:
            unresolved.append(art)
    pending = unresolved
    if not pending:
        return out

    session_store = getattr(request.app.state, "session_store", None)
    if session_store is None:
        return {**out, **{art.id: "" for art in pending}}

    session_of: dict[str, str] = {}
    turn_ids = list(dict.fromkeys(str(art.created_by_turn) for art in pending if getattr(art, "created_by_turn", None)))
    session_of_turn = await _chunked_lookup(
        turn_ids,
        batch=session_store.session_ids_for_turns,
        single=_session_id_of(session_store.get_turn),
        what="turn_session",
    )
    for art in pending:
        if getattr(art, "created_by_turn", None):
            session_of[art.id] = str(session_of_turn.get(str(art.created_by_turn)) or "")

    canvas_store = getattr(request.app.state, "canvas_store", None)
    need_canvas = [art for art in pending if not session_of.get(art.id) and getattr(art, "canvas_id", None)]
    if need_canvas and canvas_store is not None:
        session_of_canvas = await _chunked_lookup(
            list(dict.fromkeys(str(art.canvas_id) for art in need_canvas)),
            batch=canvas_store.session_ids_for_canvases,
            single=_session_id_of(canvas_store.get_canvas),
            what="canvas_session",
        )
        for art in need_canvas:
            session_of[art.id] = str(session_of_canvas.get(str(art.canvas_id)) or "")

    need_origin = [art for art in pending if not session_of.get(art.id)]
    if need_origin:

        async def origin_batch(ids: list[str]) -> dict[str, Any]:
            return {key: (row or {}).get("session_id") for key, row in (await store.origin_sessions(ids)).items()}

        async def origin_one(artifact_id: str) -> Any:
            return (await origin_batch([artifact_id])).get(artifact_id)

        origins = await _chunked_lookup([art.id for art in need_origin], batch=origin_batch, single=origin_one, what="origin_session")
        for art in need_origin:
            session_of[art.id] = str(origins.get(art.id) or "")

    distinct_sessions = list(dict.fromkeys(sid for sid in session_of.values() if sid))
    # Absent session = single lookup's get_session returns None -> "".
    found_sessions = await _chunked_lookup(
        distinct_sessions,
        batch=session_store.workspace_ids_for_sessions,
        single=_workspace_id_of(session_store.get_session),
        what="session_workspace",
    )
    workspace_of_session = {sid: str(found_sessions.get(sid) or "") for sid in distinct_sessions}
    for art in pending:
        session_id = session_of.get(art.id, "")
        out[art.id] = workspace_of_session.get(session_id, "") if session_id else ""
    return out


class _ArtifactScope(NamedTuple):
    """产物归属的判定结果（空间隔离批4 §7.4）。

    ★ 走哪一档由 ``kind`` 决定，**不是**由 ``workspace_id`` 的字符串形状决定。
      把「团队级」编码成某个特殊字符串再让下游各自 if 一遍，就是同一判据的第 N 份副本。
    """

    kind: str  # "space" | "team" | "unattributed"
    workspace_id: str  # Meaningful only for kind == "space".
    fail_close: bool  # Meaningful only for kind == "unattributed".


async def _artifact_scope(
    request: Request,
    store: ArtifactStore,
    art: Any,
) -> _ArtifactScope:
    """§7.4 的四档判定；闸关时恒等于今天。"""

    return (await _artifact_scopes(request, store, [art]))[art.id]


async def _artifact_scopes(
    request: Request,
    store: ArtifactStore,
    arts: list[Any],
) -> dict[str, _ArtifactScope]:
    """`_artifact_scope` 的批量版（单条版就是它的一个特例）：归属走批量梯子，切点水位至多读一次。"""

    workspace_ids = await _artifact_workspace_ids(request, store, arts)
    fail_close = artifact_workspace_fail_close_enabled()
    out: dict[str, _ArtifactScope] = {}
    cutover: str | None = None
    cutover_read = False
    for art in arts:
        workspace_id = workspace_ids.get(art.id, "")
        if not fail_close:
            # Gate off preserves current behavior byte-for-byte: unresolved ownership uses _require_workspace_access's fail-open early return.
            out[art.id] = _ArtifactScope("space", workspace_id, False)
        elif workspace_id == TEAM_SCOPE_WORKSPACE_ID:
            out[art.id] = _ArtifactScope("team", "", False)
        elif workspace_id:
            out[art.id] = _ArtifactScope("space", workspace_id, False)
        else:
            # ★ Only here are the two NULL cases from §7.4: **both** column and ladder fail to resolve ownership.
            #   Resolvable ladder ownership must not fail closed; normal space authorization already works, tightening would falsely reject.
            if not cutover_read:
                cutover = await read_artifact_workspace_cutover_at(store.db_path)
                cutover_read = True
            out[art.id] = _ArtifactScope(
                "unattributed",
                "",
                artifact_created_after_cutover(getattr(art, "created_at", None), cutover),
            )
    return out


async def _require_team_scope_access(
    request: Request,
    *,
    user_id: str,
    operation: SpaceOperation,
) -> None:
    """``__team__`` 那一档：**团队级授权**，不按空间成员资格判。

    仍走 `build_access_context` 这个唯一入口（``allow_missing_workspace=True``），
    于是「用户 active + 属于某个团队」照查，只跳过空间那一段——团队级产物
    （品牌素材、团队级 KB 上传）本来就不属于任何单个空间。
    """
    if not user_id or not has_workspace_access_source(request):
        return
    try:
        await build_access_context(
            user_store=request.app.state.user_store,
            access_store=getattr(request.app.state, "access_control_store", None),
            user_id=user_id,
            workspace_id="",
            allow_missing_workspace=True,
            operation=operation,
        )
    except AccessContextError as exc:
        code = "workspace_access_denied" if exc.code.startswith("ws_access_denied:") else exc.code
        raise _file_error(exc.status_code, code) from exc


async def _require_artifact_scope_access(
    request: Request,
    scope: _ArtifactScope,
    *,
    user_id: str,
    operation: SpaceOperation,
) -> None:
    if scope.kind == "team":
        await _require_team_scope_access(request, user_id=user_id, operation=operation)
        return
    if scope.kind == "unattributed":
        if scope.fail_close:
            # Unresolved attribution after cutover = missed write site, not historical debt.
            raise _file_error(status.HTTP_403_FORBIDDEN, "workspace_access_denied")
        # Before cutover: **preserve current fail-open behavior** exactly.
        return
    await _require_workspace_access(
        request,
        workspace_id=scope.workspace_id,
        user_id=user_id,
        operation=operation,
    )


async def _require_artifact_operation(
    request: Request,
    store: ArtifactStore,
    art: Any,
    *,
    operation: SpaceOperation,
    user_id: str | None = None,
) -> None:
    scope = await _artifact_scope(request, store, art)
    await _require_artifact_scope_access(
        request,
        scope,
        user_id=user_id or _user_id(request),
        operation=operation,
    )


async def _access_request(request: Request, user_id: str) -> Any:
    """一次要判很多归属时用的请求代理：空间授权的判定数据走请求级缓存（判定逻辑不变）。

    每个归属判一次约 7 次开库；与全局搜索共用同一个缓存（``request_with_cached_access``）。
    """
    user_store = getattr(request.app.state, "user_store", None)
    if user_id and callable(getattr(user_store, "get_user_team_id", None)):
        return request_with_cached_access(request, team_id=await user_store.get_user_team_id(user_id))
    return request


async def _history_visible_ids(
    request: Request,
    store: ArtifactStore,
    arts: list[Any],
    *,
    user_id: str,
) -> set[str]:
    """混合归属的列表按「可读历史」过滤，不暴露读不到的产物（403 / 404 都当作不可见）。

    归属整批推导，同一归属只判一次。
    ★ 键与判定取自**同一个** scope 值：若按产物各自再算一遍归属，推导遇到暂态故障会被吞成「推不出」而
      fail-open，这一次的「可读」就会被缓存给同一归属的所有行。
    ★ 缓存 key 是整个 scope，不是 workspace_id：否则 `__team__` 与「推不出」都会被折叠进 `""` 这一个桶，
      两档判定就混了（而它们的判定结果可以相反）。
    """
    scopes = await _artifact_scopes(request, store, arts)
    access_request = await _access_request(request, user_id) if arts else request
    verdicts: dict[_ArtifactScope, bool] = {}
    for art in arts:
        scope = scopes[art.id]
        if scope in verdicts:
            continue
        try:
            await _require_artifact_scope_access(
                access_request,
                scope,
                user_id=user_id,
                operation=SpaceOperation.READ_HISTORY,
            )
        except HTTPException as exc:
            if exc.status_code not in {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND}:
                raise
            verdicts[scope] = False
        else:
            verdicts[scope] = True
    return {art.id for art in arts if verdicts[scopes[art.id]]}


def _artifact_fetch_identity(request: Request) -> str | None:
    """cca P3 F-B: if the request carries a valid artifact-fetch Bearer token (the
    python_sandbox host-side runner's scoped GET), return its `sub` — the REAL caller to
    run the owner-check as. None for a normal browser/user request (it then falls back to
    the JWT/header identity). Verified with the DEDICATED app.state.artifact_fetch_secret +
    aud="artifact-fetch", so a main-JWT user token (different secret/aud) never matches and
    a member can't forge one without the dedicated secret."""
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    secret = getattr(request.app.state, "artifact_fetch_secret", None)
    if not secret:
        return None
    from bowhead_core.artifact_store import verify_artifact_fetch_token

    return verify_artifact_fetch_token(str(secret), auth[7:])


async def _require_document_inspection_upload(
    request: Request,
    *,
    filename: str,
    mime: str,
    content: bytes,
    source: str,
    session_id: str | None,
    created_by_turn: str | None,
) -> Callable[[aiosqlite.Connection], Awaitable[None]] | None:
    """Honor unexpired legacy inspection-token restrictions at the original writer."""
    if _artifact_fetch_identity(request) is None:
        return None
    from bowhead_core.artifact_store import artifact_fetch_document_inspection
    from jwt import InvalidTokenError

    try:
        scope = artifact_fetch_document_inspection(str(request.app.state.artifact_fetch_secret), request.headers["Authorization"][7:])
    except (ValueError, InvalidTokenError):
        raise _file_error(status.HTTP_403_FORBIDDEN, "document_workflow_required") from None
    if scope is None:
        return None
    import re

    valid = (
        source == "python_sandbox"
        and request.headers.get("X-Bowhead-Upload-Source", "python_sandbox") == "python_sandbox"
        and request.query_params.get("kind") in (None, "tool_data", "sandbox_output")
        and not any(request.query_params.get(key) for key in ("derived_from", "manifest_artifact_id"))
        and session_id == scope["session_id"]
        and created_by_turn == scope["turn_id"]
        and all(request.headers.get(key) in (None, scope["workspace_id"]) for key in ("X-Space-Id", "X-Workspace-Id"))
        and len(content) <= 1024 * 1024
        and (
            re.fullmatch(r"sandbox-stdout(?:-data-\d+)?\.txt", filename) is not None
            and mime == "text/plain"
            or re.fullmatch(r"sandbox-result(?:-data-\d+)?\.json", filename) is not None
            and mime == "application/json"
        )
    )
    try:
        text = content.decode("utf-8", errors="strict")
        if "\x00" in text:
            valid = False
        if mime == "application/json":
            json.loads(text)
    except (UnicodeDecodeError, ValueError):
        valid = False
    if not valid:
        raise _file_error(status.HTTP_403_FORBIDDEN, "document_workflow_required")
    from bowhead_core.document_chat_continuation import DocumentTurnOwner
    from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable, require_live_worker_in_tx

    worker = DocumentTurnOwner(
        team_id=scope["team_id"],
        user_id=scope["user_id"],
        workspace_id=scope["workspace_id"],
        session_id=scope["session_id"],
        turn_id=scope["turn_id"],
        generation=scope["execution_generation"],
        owner_id=scope["execution_owner_id"],
    )

    async def authorize(db: aiosqlite.Connection) -> None:
        try:
            await require_live_worker_in_tx(db, worker=worker)
        except OrdinaryContinuationUnavailable:
            raise _file_error(status.HTTP_403_FORBIDDEN, "document_workflow_required") from None

    return authorize


# file-management-v2 P0 convergence: source/kind→origin has one implementation in artifact_origin.py.
# Previously copied manually in this file/canvas.py/sessions.py; guard tests forbid further duplication.
from bowhead_gateway.artifact_origin import origin_for as _origin_for_kind_source


def _origin_for_art(art) -> str:  # type: ignore[no-untyped-def]
    """Kind-aware origin (deck/web take priority over the legacy source map)."""
    return _origin_for_kind_source(
        getattr(art, "kind", None),
        art.source,
        producer_kind=getattr(art, "producer_kind", None),
    )


def _normalize_category_filter(category: str | None) -> str | None:
    """`?category=` 筛选值归一化成代码值；空串视为不筛。

    过渡期兼容：仍接收旧中文类别名（删除时机见 `artifact_classifier.ZH_LABEL_TO_CATEGORY`）。
    未知值原样精确匹配（结果为空），不报错。
    """
    if not category or not category.strip():
        return None
    if is_legacy_category(category):
        log.info("files.category_legacy_value route=list_files value=%s", category.strip())
    return normalize_category(category)


def _art_to_dto(  # type: ignore[no-untyped-def]
    art,
    usage_count: int,
    *,
    in_kb: bool = False,
    kb: FileKbStatusDTO | None = None,
    origin_session: dict[str, str | None] | None = None,
    trust_override: str | None = None,
) -> FileListItemDTO:
    return FileListItemDTO(
        file_id=art.id,
        filename=art.filename,
        mime=art.mime,
        mime_family=mime_to_family(art.mime),
        size=art.size,
        source=art.source,
        origin=_origin_for_art(art),  # type: ignore[arg-type]
        kind=getattr(art, "kind", None) or "tool_data",
        trust=trust_override or getattr(art, "trust", None) or "trusted",
        created_at=art.created_at,
        last_used_at=art.last_used_at,
        deleted_at=art.deleted_at,
        category=art.category,
        category_source=art.category_source,
        usage_count=usage_count,
        in_kb=in_kb,
        kb=kb,
        kb_promotable=is_kb_promotable(art),
        session_id=(origin_session or {}).get("session_id"),
        session_title=(origin_session or {}).get("session_title"),
        has_canvas_ref=bool(getattr(art, "canvas_id", None)),
        retention_hint=getattr(art, "retention_hint", None),
        url=f"/api/v1/files/{art.id}",
    )


async def _export_trust_projection(
    request: Request,
    store: ArtifactStore,
    artifacts: list[Any],
) -> dict[str, str]:
    from bowhead_gateway.export_artifact_trust import project_export_trust

    return await project_export_trust(
        artifacts,
        artifact_store=store,
        canvas_store=getattr(request.app.state, "canvas_store", None),
        viewer_id=_user_id(request),
    )


async def _in_kb_ids(request: Request, artifact_ids: list[str]) -> set[str]:
    """file-management-v2 P1: 单页一次跨 store 批查（kb_files 在独立 db，不能 JOIN）。

    KB store 缺失或查询失败 → 返回空集（degrade to「都不在 KB」）：徽章少显示是
    可接受的，而列表整页 500 不是。**删除防护不走这条路径**——那里必须实时单查。
    """
    kb_store = getattr(request.app.state, "kb_files_store", None)
    if kb_store is None or not artifact_ids:
        return set()
    try:
        return await kb_store.artifact_ids_in_kb(artifact_ids)
    except Exception as e:  # noqa: BLE001 - degraded badge beats a 500 list
        log.warning("files.in_kb_lookup_failed error=%s", e)
        return set()


async def _kb_read_context(request: Request, workspace_id: str) -> tuple[Any, dict[str, Any] | None] | None:
    """调用方在 KB 侧、针对**某个空间**（或无空间）的读上下文 + 授权快照。

    空间来自 KB 行本身而不是请求头：文件页不属于任何空间，请求不带 `X-Space-Id`。
    `build_access_context` 校验该空间属于本团队且调用方是成员；不是成员 → None（该空间的行不可见）。
    缺 RBAC hook 的旧测试 harness 退回轻量路径：只认请求头里的空间（无法验成员资格，不扩大可见面）。
    """
    from bowhead_core.kb.permission import ScopeContext

    user_id = _user_id(request)
    if has_workspace_access_source(request):
        try:
            access = await build_access_context(
                user_store=request.app.state.user_store,
                access_store=getattr(request.app.state, "access_control_store", None),
                user_id=user_id,
                workspace_id=workspace_id,
                allow_missing_workspace=not workspace_id,
                operation=SpaceOperation.READ_HISTORY,
            )
        except AccessContextError:
            return None
        ctx = ScopeContext(user_id=user_id, workspace_id=access.workspace_id, team_id=access.team_id, role="user")
        return ctx, access_context_to_dict(access)
    header_ws = request.headers.get("X-Space-Id") or request.headers.get("X-Workspace-Id") or ""
    if workspace_id and workspace_id != header_ws:
        return None
    team_id = ""
    ustore = getattr(request.app.state, "user_store", None)
    if ustore is not None and callable(getattr(ustore, "get_user", None)):
        u = await ustore.get_user(user_id)
        team_id = getattr(u, "team_id", "") or ""
    return ScopeContext(user_id=user_id, workspace_id=workspace_id, team_id=team_id, role="user"), None


def _kb_row_space(row: Any) -> str:
    """空间行归属的空间 id（`scope_id` 即空间 id，与 `KbPermission.can_read` 比较的同一列）；其余范围为空串。"""
    return (row.scope_id or "") if row.scope in ("space", "workspace") else ""


async def _kb_rows_readable(request: Request, rows: list[Any]) -> list[Any]:
    """按 KB 读权限逐行过滤——与 KB 抽屉 `_kb_scope_check` 同判据：scope 可读 + scope/file 授权。

    每行用**它自己的空间**建上下文：个人行 = 仅上传者；团队行 = 本团队；空间行 = 调用方是
    该空间成员。上下文按不同空间去重，一页最多「1（无空间）+ 页内不同空间数」次，与行数无关。
    授权只在 enforce 开启时拒绝（KB 路由在 enforce 关闭时同样放行，仅记日志）；
    这里是列表徽章，不记 runtime_denied 审计。
    """
    from bowhead_core.access_control import resource_grants_enforce_enabled
    from bowhead_core.kb.permission import KbPermission

    from bowhead_gateway.routes.kb import _kb_scope_resource_id

    contexts: dict[str, tuple[Any, dict[str, Any] | None] | None] = {}
    for ws in sorted({_kb_row_space(r) for r in rows}):
        contexts[ws] = await _kb_read_context(request, ws)
    access_store = getattr(request.app.state, "access_control_store", None)
    enforce = access_store is not None and resource_grants_enforce_enabled()
    scope_ok: dict[str, bool] = {}
    out: list[Any] = []
    for row in rows:
        built = contexts.get(_kb_row_space(row))
        if built is None:
            continue
        ctx, access_dict = built
        if not KbPermission.can_read(row, ctx):
            continue
        if enforce and access_dict:
            resource_id = _kb_scope_resource_id(row.scope, row.scope_id, ctx)
            if resource_id is not None:
                if resource_id not in scope_ok:
                    decision = await access_store.explain_decision(access_dict, "kb_scope", resource_id, "read")
                    scope_ok[resource_id] = bool(decision.get("allowed"))
                if not scope_ok[resource_id]:
                    continue
            decision = await access_store.explain_decision(access_dict, "kb_file", row.file_id, "read")
            if not decision.get("allowed"):
                continue
        out.append(row)
    return out


async def _kb_member_ids(request: Request, artifact_ids: list[str]) -> set[str]:
    """这些产物里有活跃 KB 行的（与列表 `in_kb` 徽章同一判据）。KB 不可用时视为都不在 KB。"""
    kb_store = getattr(request.app.state, "kb_files_store", None)
    if kb_store is None or not artifact_ids:
        return set()
    out: set[str] = set()
    try:
        for start in range(0, len(artifact_ids), 5000):
            out |= await kb_store.artifact_ids_in_kb(artifact_ids[start : start + 5000])
    except Exception as e:  # noqa: BLE001 - Same degradation as _kb_overlays.
        log.warning("files.in_kb_filter_failed error=%s", e)
        return set()
    return out


async def _kb_overlays(request: Request, artifact_ids: list[str]) -> tuple[set[str], dict[str, FileKbStatusDTO]]:
    """知识库体验整治 FR7：一页 artifact 的 `in_kb` 与知识库状态，KB 库只查一次。

    - `in_kb` 语义不变：有任何活跃 KB 行即为 True（删除防护、类别徽章依赖它）。
    - 状态只来自调用方按 KB 读权限**看得到**的行；看不到的行不透露状态、file_id、范围。
    - KB store 缺失或查询失败 → 同 `_in_kb_ids` 降级为「都不在 KB」；可见性判定失败 → 只丢状态。
    """
    kb_store = getattr(request.app.state, "kb_files_store", None)
    if kb_store is None or not artifact_ids:
        return set(), {}
    try:
        rows = await kb_store.active_for_artifacts(artifact_ids)
    except Exception as e:  # noqa: BLE001 - degraded badge beats a 500 list
        log.warning("files.in_kb_lookup_failed error=%s", e)
        return set(), {}
    in_kb = {r.artifact_id for r in rows}
    try:
        readable = await _kb_rows_readable(request, rows)
    except Exception as e:  # noqa: BLE001 - fail closed on status, keep the list
        log.warning("files.kb_status_visibility_failed error=%s", e)
        readable = []
    status: dict[str, FileKbStatusDTO] = {}
    for row in readable:  # Descending uploaded_at: latest addition wins among visible duplicate file rows.
        if row.artifact_id in status:
            continue
        status[row.artifact_id] = FileKbStatusDTO(
            file_id=row.file_id,
            scope="space" if row.scope == "workspace" else row.scope,
            workspace_id=_kb_row_space(row) or None,
            parse_status=row.parse_status,
            parse_progress_pct=row.parse_progress_pct,
            parse_stuck=row.parse_stuck,
            parse_error_code=row.parse_error_code,
        )
    return in_kb, status


@router.get("/", response_model=FileListDTO)
async def list_files(
    request: Request,
    q: str | None = Query(None, description="文件名模糊搜或 artifact id 精确搜"),
    mime_family: list[str] | None = Query(None, description="image / pdf / presentation / spreadsheet / document / text / other"),
    source: list[str] | None = Query(None, description="upload / canvas_export / sandbox_output"),
    kind: list[str] | None = Query(
        None,
        description=(
            "资产库过滤（file-management-v2 P1）: user_upload / deliverable / canvas_export "
            "（资产 tab）· tool_data / sandbox_output / web_clip（运行数据 tab）。与 source AND。"
        ),
    ),
    since: str | None = Query(None, description="ISO date, 起始"),
    until: str | None = Query(None, description="ISO date, 截止"),
    sort: str = Query("created_desc", regex="^(created_desc|size_desc|last_used_desc)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_deleted: int = Query(0, ge=0, le=1),
    shared_with_me: int = Query(0, ge=0, le=1, description="P3: 只看别人分享给我的"),
    in_kb: int | None = Query(None, ge=0, le=1, description="1=只看已加入知识库的，0=排除已加入的（分页前生效）"),
    category: str | None = Query(None, description="业务分类（分页前生效）"),
) -> FileListDTO:
    """列出当前用户的 artifact.

    `source` 保持既有语义（默认 ["upload"]，传 "all" 看全部）以兼容既有调用方；
    新的资产库视图用 `kind` 过滤（PRD §4.2.3）——两者 AND，通常只传其一。
    """
    store = _store(request)
    user_id = _user_id(request)
    await _require_request_workspace_access(
        request,
        operation=SpaceOperation.READ_HISTORY,
        user_id=user_id,
    )

    # P3 Shared-with-me: use authorization-engine grants, not created_by;
    # files belong to others, so list_for_user cannot retrieve them by definition.
    if shared_with_me:
        from bowhead_gateway.files_sharing import shared_artifact_ids

        ctx = await _share_ctx(
            request,
            operation=SpaceOperation.READ_HISTORY,
        )
        ids = await shared_artifact_ids(
            getattr(request.app.state, "access_control_store", None),
            team_id=ctx.team_id,
            user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            group_ids=ctx.group_ids,
        )
        return await _list_shared(request, store, sorted(ids), limit=limit, offset=offset)

    effective_sources = source if source else ["upload"]
    if effective_sources == ["all"]:
        effective_sources = None
    # Kind-only filters must not be narrowed again by default source=["upload"], leaving only uploads on the assets tab.
    if kind and not source:
        effective_sources = None

    # Category filter must precede pagination; FE previously filtered current page, leaving few rows without reducing page count.
    # In-KB uses the badge's criterion (any active KB row); KB is in another DB, so batch-check user's artifact IDs first.
    kb_member_ids: set[str] | None = None
    if in_kb is not None:
        kb_member_ids = await _kb_member_ids(request, await store.list_ids_for_user(user_id))
    items, total, size_sum = await store.list_for_user(
        user_id=user_id,
        q=q,
        include_ids=kb_member_ids if in_kb == 1 else None,
        exclude_ids=kb_member_ids if in_kb == 0 else None,
        category=_normalize_category_filter(category),
        mime_families=mime_family or None,
        sources=effective_sources,
        kinds=kind or None,
        since=since,
        until=until,
        sort=sort,
        limit=limit,
        offset=offset,
        include_deleted=bool(include_deleted),
    )
    ids = [a.id for a in items]
    kb_ids, kb_status = await _kb_overlays(request, ids)
    # Source session: same-DB JOIN, one batch per page (no N+1).
    try:
        origins = await store.origin_sessions(ids)
    except Exception as e:  # noqa: BLE001 - Missing source column must not 500 the whole page.
        log.warning("files.origin_sessions_failed error=%s", e)
        origins = {}
    out: list[FileListItemDTO] = []
    access_filtered = False
    visible_size = 0
    visible_artifacts: list[Any] = []
    visible_ids = await _history_visible_ids(request, store, items, user_id=user_id)
    for art in items:
        if art.id not in visible_ids:
            access_filtered = True
            continue
        visible_artifacts.append(art)
    trust_by_id = await _export_trust_projection(request, store, visible_artifacts)
    usage = await store.usage_counts([art.id for art in visible_artifacts])
    for art in visible_artifacts:
        out.append(
            _art_to_dto(
                art,
                usage.get(art.id, 0),
                in_kb=art.id in kb_ids,
                kb=kb_status.get(art.id),
                origin_session=origins.get(art.id),
                trust_override=trust_by_id.get(art.id),
            )
        )
        visible_size += art.size
    return FileListDTO(
        total=len(out) if access_filtered else total,
        size_total_bytes=visible_size if access_filtered else size_sum,
        items=out,
    )


@router.delete("/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def soft_delete_file(artifact_id: str, request: Request) -> Response:
    store = _store(request)
    art = await store.get(artifact_id, include_deleted=True)
    if art is None:
        raise _file_not_found()
    _require_file_owner(art, _user_id(request))
    await _require_artifact_visible(
        request,
        store,
        art,
        operation=SpaceOperation.WRITE,
    )
    # file-management-v2 P2 (D2): KB-reference defense. Deleting an artifact referenced by kb_files leaves
    # retrieval working but original downloads returning 404 (silent partial failure confirmed in discovery §3.6).
    # FE consolidated the entry into navigation, but **API needs its own guard**: bypassing UI must not
    # allow direct calls to break KB.
    #
    # Fresh single lookup, not list batch cache: user may have just removed it from KB.
    kb_store = getattr(request.app.state, "kb_files_store", None)
    if kb_store is not None:
        try:
            still_in_kb = bool(await kb_store.artifact_ids_in_kb([artifact_id]))
        except Exception as e:  # noqa: BLE001
            # Failed lookup is fail-open: permit one deletion (recoverable for 30 days) rather than
            # let KB DB instability disable the user's delete capability.
            log.warning("files.delete_kb_guard_degraded artifact=%s error=%s", artifact_id, e)
            still_in_kb = False
        if still_in_kb:
            raise _file_error(status.HTTP_409_CONFLICT, "file_in_knowledge_base")
    ok = await store.soft_delete(artifact_id, user_id=_user_id(request))
    if not ok:
        raise _file_error(status.HTTP_404_NOT_FOUND, "file_not_found")
    from bowhead_core.export_copy_contract import evict_export_copy_manifest

    evict_export_copy_manifest(artifact_id)
    evict_export_copy_manifest(getattr(art, "manifest_artifact_id", None))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{artifact_id}/restore", response_model=FileListItemDTO)
async def restore_file(artifact_id: str, request: Request) -> FileListItemDTO:
    store = _store(request)
    existing = await store.get(artifact_id, include_deleted=True)
    if existing is None:
        raise _file_not_found()
    _require_file_owner(existing, _user_id(request))
    await _require_artifact_visible(
        request,
        store,
        existing,
        operation=SpaceOperation.WRITE,
    )
    ok = await store.restore(artifact_id, user_id=_user_id(request))
    if not ok:
        raise _file_not_found()
    from bowhead_core.export_copy_contract import evict_export_copy_manifest

    evict_export_copy_manifest(artifact_id)
    evict_export_copy_manifest(getattr(existing, "manifest_artifact_id", None))
    art = await store.get(artifact_id)
    assert art is not None
    trust_by_id = await _export_trust_projection(request, store, [art])
    return _art_to_dto(
        art,
        await store.usage_count(art.id),
        trust_override=trust_by_id.get(art.id),
    )


@router.get("/{artifact_id}/usages", response_model=FileUsagesDTO)
async def file_usages(artifact_id: str, request: Request) -> FileUsagesDTO:
    store = _store(request)
    art = await store.get(artifact_id, include_deleted=True)
    if art is None:
        raise _file_not_found()
    if art.created_by and art.created_by != _user_id(request):
        raise _file_denied(artifact_id, _user_id(request), "not_owner")
    await _require_artifact_visible(
        request,
        store,
        art,
        operation=SpaceOperation.READ_HISTORY,
    )
    usages = await store.list_usages(artifact_id)
    return FileUsagesDTO(
        usages=[
            FileUsageEntryDTO(
                session_id=u.get("session_id", ""),
                session_title=u.get("session_title"),
                turn_id=u.get("turn_id", ""),
                turn_seq=u.get("turn_seq", 0),
                user_text=u.get("user_text", ""),
                used_at=u.get("used_at", ""),
            )
            for u in usages
        ]
    )


async def build_file_preview(store: ArtifactStore, art: Any, *, mode: str, max_rows: int, data: bytes | None = None) -> FilePreviewDTO:
    """Build the same preview DTO for owner-only file reads and report-scoped reads."""
    family = mime_to_family(art.mime)
    if family == "image":
        return FilePreviewDTO(family=family, text=None, note="use <img> to render")

    try:
        if data is None:
            data = await store.open_read(art)
    except Exception:
        return FilePreviewDTO(
            family=family,
            text=None,
            error=None,
            error_code="file_preview_failed",
        )

    # #100 rows mode: CSV/TSV returns structured columns + rows.
    want_rows = mode == "rows" or (mode == "auto" and art.mime in ("text/csv", "text/tab-separated-values"))
    if want_rows and art.mime in ("text/csv", "text/tab-separated-values"):
        try:
            import csv as _csv
            import io as _io

            delim = "," if art.mime == "text/csv" else "\t"
            text_data = data.decode("utf-8", errors="replace")
            reader = _csv.DictReader(_io.StringIO(text_data), delimiter=delim)
            cols = list(reader.fieldnames or [])
            rows: list[dict[str, Any]] = []
            total = 0

            def _csv_header_cell(value: object) -> str:
                return str(value if value is not None else "").strip().lstrip("\ufeff")

            for r in reader:
                row = dict(r)
                if cols and all(_csv_header_cell(row.get(c)) == _csv_header_cell(c) for c in cols):
                    continue
                total += 1
                if len(rows) < max_rows:
                    rows.append(row)
            return FilePreviewDTO(
                family=family,
                mode="rows",
                columns=cols,
                rows=rows,
                total_rows=total,
                shown_rows=len(rows),
                truncated=total > max_rows,
            )
        except Exception:
            return FilePreviewDTO(
                family=family,
                text=None,
                error=None,
                error_code="file_preview_failed",
            )

    from bowhead_core.attachments_injector import extract_attachment_bounded

    # Resource-bounded + off-loop parse (S3 follow-up): build_file_preview is an
    # async handler; a synchronous in-process parse would block the event loop.
    prep = await extract_attachment_bounded(art, data)
    if prep.error:
        return FilePreviewDTO(
            family=family,
            text=None,
            error=None,
            error_code="file_preview_failed",
        )
    text = (prep.extracted_text or "").strip()
    return FilePreviewDTO(
        family=family,
        mode="text",
        # ``extract_attachment_bounded`` already applies the shared 5k
        # untrusted-attachment bound. Do not silently crop it a second time to
        # 1.5k: PPTX/XLSX markers and useful JSON/text structure were being cut
        # before the typed Results Viewer could render them.
        text=text,
        truncated=prep.truncated,
        note=prep.note,
    )


@router.get("/{artifact_id}/representations/{representation}", response_model=ArtifactRepresentationDTO)
async def file_representation(artifact_id: str, representation: Representation, request: Request, response: Response) -> ArtifactRepresentationDTO:
    from bowhead_gateway.files_sharing import can_read_shared

    user_id = _user_id(request)  # Authentication before object lookup.
    store = _store(request)
    art = await store.get(artifact_id)
    if art is None or art.deleted_at is not None:
        raise representation_error("scope_unavailable")
    try:
        if art.created_by and art.created_by != user_id:
            ctx = await _share_ctx(request, operation=SpaceOperation.READ_HISTORY)
            if not await can_read_shared(
                getattr(request.app.state, "access_control_store", None),
                artifact_id=artifact_id,
                team_id=ctx.team_id,
                user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                group_ids=ctx.group_ids,
            ):
                raise representation_error("scope_unavailable")
        await _require_artifact_visible(request, store, art, operation=SpaceOperation.READ_HISTORY, user_id=user_id)
    except HTTPException as exc:
        if exc.status_code in {403, 404, 410}:
            raise representation_error("scope_unavailable") from exc
        raise
    response.headers["Cache-Control"] = "no-store"
    return await build_representation(store, art, representation)


@router.get("/{artifact_id}/preview", response_model=FilePreviewDTO)
async def file_preview(
    artifact_id: str,
    request: Request,
    mode: str = Query(default="auto", description="auto | text | rows | image"),
    max_rows: int = Query(default=100, ge=1, le=10_000),
) -> FilePreviewDTO:
    """按需返回有界文本预览；`mode=rows` 对 CSV/TSV 返结构化 rows.

    上限 2026-09-02 从 1000 提到 10000：前端表格上了虚拟滚动，「展开全部」不再
    受 DOM 行数限制，而 1000 这个数原本就是**为了配合它**才设的。
    116 语料实测 2057 张图 `row_count` 最大 9183、p99=235 ⇒ 10000 覆盖全部观测值。

    代价只有响应体积（每行约 126B ⇒ 1 万行约 1.2MB）：**解析成本不变**——
    `build_file_preview` 为了报准 `total_rows` 本来就要读完整份、遍历所有行，
    `max_rows` 只决定往回带几行。
    ⚠ 已知未治：CSV 那条分支是在 async handler 里**同步**解析的（text 分支才走
    off-loop 的 `extract_attachment_bounded`），大文件会占事件循环。这在提上限之前
    就存在，本次没动；真要治该把 rows 分支也挪到线程池。
    """
    from bowhead_gateway.files_sharing import can_read_shared

    store = _store(request)
    user_id = _user_id(request)
    art = await store.get(artifact_id, include_deleted=True)
    if art is None:
        raise _file_not_found()
    if art.created_by and art.created_by != user_id:
        # P3: share recipients also need preview, beyond owners; otherwise Shared-with-me opens 403 despite working downloads,
        # only half a feature. Use the same authorization as download.
        with _concealing_denials(artifact_id, user_id):
            ctx = await _share_ctx(
                request,
                operation=SpaceOperation.READ_HISTORY,
            )
        shared = art.deleted_at is None and await can_read_shared(
            getattr(request.app.state, "access_control_store", None),
            artifact_id=artifact_id,
            team_id=ctx.team_id,
            user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            group_ids=ctx.group_ids,
        )
        if not shared:
            raise _file_denied(artifact_id, user_id, "not_owner_not_shared")
    await _require_artifact_visible(
        request,
        store,
        art,
        operation=SpaceOperation.READ_HISTORY,
        user_id=user_id,
    )
    return await build_file_preview(store, art, mode=mode, max_rows=max_rows)


from pydantic import BaseModel as _BaseModel


class CategoryRequest(_BaseModel):
    category: str


@router.post("/{artifact_id}/category", response_model=FileListItemDTO)
async def set_category(
    artifact_id: str,
    body: CategoryRequest,
    request: Request,
) -> FileListItemDTO:
    """用户手工改分类. Body: {category: str}. source 自动 = 'user'.

    file-management-v2 P0-9: 返回注解曾是 dict[str, Any] 而实返 FileListItemDTO
    → FastAPI ResponseValidationError → 该端点自上线起 500（抽屉改分类从未成功）。
    """
    store = _store(request)
    art = await store.get(artifact_id, include_deleted=True)
    if art is None:
        raise _file_not_found()
    _require_file_owner(art, _user_id(request))
    await _require_artifact_visible(
        request,
        store,
        art,
        operation=SpaceOperation.WRITE,
    )
    from bowhead_core.artifact_classifier import CATEGORIES

    # Transitional compatibility: accept old Chinese category labels, normalize to codes (removal timing:
    # `artifact_classifier.ZH_LABEL_TO_CATEGORY`）。
    if is_legacy_category(body.category):
        log.info("files.category_legacy_value route=set_category value=%s", body.category.strip())
    cat = normalize_category(body.category) or ""
    if cat not in CATEGORIES:
        raise _file_error(status.HTTP_400_BAD_REQUEST, "invalid_file_category")
    await store.set_category(artifact_id, category=cat, source="user")
    art2 = await store.get(artifact_id, include_deleted=True)
    assert art2 is not None
    trust_by_id = await _export_trust_projection(request, store, [art2])
    # Drawer replaces the full list row with the response; omitting KB fields resets its KB column to not-added.
    kb_ids, kb_status = await _kb_overlays(request, [art2.id])
    return _art_to_dto(
        art2,
        await store.usage_count(art2.id),
        in_kb=art2.id in kb_ids,
        kb=kb_status.get(art2.id),
        trust_override=trust_by_id.get(art2.id),
    )


class ShareRequest(_BaseModel):
    """`subject_kind` 用 grant 的冻结拼写 `workspace`（用户面叫「空间」）。"""

    subject_kind: str = "workspace"
    subject_id: str = ""


@router.post("/{artifact_id}/share", status_code=status.HTTP_201_CREATED)
async def share_file(artifact_id: str, body: ShareRequest, request: Request) -> dict[str, Any]:
    """把一份资产分享给空间/团队（只读）。owner-only。

    P3：走既有授权引擎（`resource_kind="artifact"` 的 grant），artifacts 表
    零 schema 变更——不制造第二处权限真相。
    """
    from bowhead_gateway.files_sharing import ARTIFACT_RESOURCE_KIND, ShareRejected, assert_shareable

    store = _store(request)
    user_id = _user_id(request)
    art = await store.get(artifact_id, include_deleted=True)
    if art is None:
        raise _file_not_found()
    if art.created_by != user_id:
        # Sharing requires ownership; recipients cannot reshare.
        raise _file_denied(artifact_id, user_id, "not_owner")
    await _require_artifact_visible(
        request,
        store,
        art,
        operation=SpaceOperation.WRITE,
        user_id=user_id,
    )
    _ctx = await _share_ctx(request)
    team_id, workspace_id = _ctx.team_id, _ctx.workspace_id
    if not team_id:
        raise _file_error(status.HTTP_400_BAD_REQUEST, "file_share_context_required")

    kb_ids = await _in_kb_ids(request, [artifact_id])
    try:
        assert_shareable(art, in_kb=artifact_id in kb_ids)
    except ShareRejected as e:
        raise _file_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "file_not_shareable") from e

    subject_kind = body.subject_kind if body.subject_kind in {"workspace", "team"} else "workspace"
    subject_id = body.subject_id or (workspace_id if subject_kind == "workspace" else team_id)
    if not subject_id:
        raise _file_error(status.HTTP_400_BAD_REQUEST, "file_share_context_required")

    access_store = getattr(request.app.state, "access_control_store", None)
    if access_store is None:
        raise _file_error(status.HTTP_503_SERVICE_UNAVAILABLE, "file_access_service_unavailable")
    await access_store.create_or_update_resource_grant(
        team_id=team_id,
        resource_kind=ARTIFACT_RESOURCE_KIND,
        resource_id=artifact_id,
        principal_type=subject_kind,
        principal_id=subject_id,
        permission="read",  # Read-only: recipients cannot delete/edit/add to KB.
        created_by=user_id,
    )
    log.info("files.shared aid=%s to=%s:%s by=%s", artifact_id, subject_kind, subject_id, user_id)
    return {"artifact_id": artifact_id, "subject_kind": subject_kind, "subject_id": subject_id}


@router.delete("/{artifact_id}/share/{subject_kind}/{subject_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unshare_file(
    artifact_id: str,
    subject_kind: str,
    subject_id: str,
    request: Request,
) -> Response:
    """撤销分享。owner-only。撤销后被分享方**立即**失去列表与下载（不缓存 grant）。"""
    from bowhead_gateway.files_sharing import ARTIFACT_RESOURCE_KIND

    store = _store(request)
    user_id = _user_id(request)
    art = await store.get(artifact_id, include_deleted=True)
    if art is None:
        raise _file_not_found()
    if art.created_by != user_id:
        raise _file_denied(artifact_id, user_id, "not_owner")
    await _require_artifact_visible(
        request,
        store,
        art,
        operation=SpaceOperation.WRITE,
        user_id=user_id,
    )
    team_id = (await _share_ctx(request)).team_id
    access_store = getattr(request.app.state, "access_control_store", None)
    if access_store is None or not team_id:
        raise _file_error(status.HTTP_503_SERVICE_UNAVAILABLE, "file_access_service_unavailable")

    grants = await access_store.list_resource_grants(
        team_id,
        resource_kind=ARTIFACT_RESOURCE_KIND,
        resource_id=artifact_id,
    )
    for g in grants:
        if g.principal_type == subject_kind and g.principal_id == subject_id:
            await access_store.revoke_resource_grant(team_id, g.id)
            log.info("files.unshared aid=%s from=%s:%s", artifact_id, subject_kind, subject_id)
            break
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{artifact_id}/shares")
async def list_file_shares(artifact_id: str, request: Request) -> dict[str, Any]:
    """这份文件分享给了谁。owner-only。"""
    from bowhead_gateway.files_sharing import ARTIFACT_RESOURCE_KIND

    store = _store(request)
    user_id = _user_id(request)
    art = await store.get(artifact_id, include_deleted=True)
    if art is None:
        raise _file_not_found()
    if art.created_by != user_id:
        raise _file_denied(artifact_id, user_id, "not_owner")
    await _require_artifact_visible(
        request,
        store,
        art,
        operation=SpaceOperation.READ_HISTORY,
        user_id=user_id,
    )
    team_id = (
        await _share_ctx(
            request,
            operation=SpaceOperation.READ_HISTORY,
        )
    ).team_id
    access_store = getattr(request.app.state, "access_control_store", None)
    if access_store is None or not team_id:
        return {"shares": []}
    grants = await access_store.list_resource_grants(
        team_id,
        resource_kind=ARTIFACT_RESOURCE_KIND,
        resource_id=artifact_id,
    )
    return {
        "shares": [
            {"subject_kind": g.principal_type, "subject_id": g.principal_id}
            for g in grants
            if getattr(g, "revoked_at", None) is None and g.effect == "allow"
        ]
    }


class PurgeRequest(_BaseModel):
    artifact_ids: list[str]


@router.post("/purge")
async def purge_files(body: PurgeRequest, request: Request) -> dict[str, int]:
    """P4「永久删除」：清掉回收站里的指定文件，不等 30 天 GC。

    只作用于**自己的、已在回收站的**行（两个条件都写在 SQL 里，不靠调用方守规矩）。
    与 nightly GC 共用持久 purge claim 和清理实现；存储结果不确定时保留 claim 待重试。
    """
    store = _store(request)
    user_id = _user_id(request)
    # Check writes individually; reject whole request at its first unwritable item in request order. Batch-infer ownership and check each scope once.
    owned = [art for art in await store.get_many(list(body.artifact_ids), include_deleted=True) if art.created_by == user_id]
    scopes = await _artifact_scopes(request, store, owned)
    access_request = await _access_request(request, user_id) if owned else request
    checked: set[_ArtifactScope] = set()
    for art in owned:
        if scopes[art.id] in checked:
            continue
        await _require_artifact_scope_access(
            access_request,
            scopes[art.id],
            user_id=user_id,
            operation=SpaceOperation.WRITE,
        )
        checked.add(scopes[art.id])
    # Same D2 guard: never permanently delete KB-referenced artifacts, leaving retrieval hits but permanent original-download 404
    # without even the 30-day recovery window. Soft delete has a 409 guard; this path needs it too.
    kb_ids = await _in_kb_ids(request, list(body.artifact_ids))
    targets = [i for i in body.artifact_ids if i not in kb_ids]
    n = await store.purge_now(targets, user_id=user_id)
    log.info(
        "files.purged user=%s requested=%d purged=%d kb_blocked=%d",
        user_id,
        len(body.artifact_ids),
        n,
        len(kb_ids),
    )
    return {"purged": n, "kb_blocked": len(kb_ids)}


@router.get("/usage")
async def files_usage(request: Request) -> dict[str, int]:
    """P4 用量可见性：资产 / 运行数据分别占多少字节。

    两个数字分开给——一个总数说明不了「我的 PPT 占了 2GB」还是「agent 的中间
    CSV 占了 2GB」，而这两者的处置办法完全不同（前者用户自己清，后者等 TTL）。
    """
    store = _store(request)
    await _require_request_workspace_access(
        request,
        operation=SpaceOperation.READ_HISTORY,
    )
    user_id = _user_id(request)
    out = {"assets": 0, "run_data": 0}
    arts = await store.list_usage_artifacts_for_user(user_id)
    # ★ Batch authorization required: per-artifact ownership costs 3–4 DB opens. 116 user with 12k artifacts caused ~40k opens
    #   per request, tens of minutes of work after client 499 disconnect, saturating 4 cores and slowing the site (2026-09-24).
    visible_ids = await _history_visible_ids(request, store, arts, user_id=user_id)
    for art in arts:
        if art.id not in visible_ids:
            continue
        if art.kind in {"user_upload", "deliverable", "canvas_export"}:
            out["assets"] += art.size
        elif art.kind in {"tool_data", "sandbox_output", "web_clip"}:
            out["run_data"] += art.size
    return out


@router.get("/{artifact_id}")
async def download(artifact_id: str, request: Request):  # type: ignore[no-untyped-def]
    store = _store(request)
    # #54 long-term fix (2026-04-29): preview checked owner (l.229+), download did not, enabling cross-user
    # URL reads of others' artifacts. Add check_created_by, sharing preview's _current_user_id helper.
    # Silent 404 conceals existence.
    # cca P3 F-B: a python_sandbox runner-fetch carries an artifact-fetch Bearer → run the
    # owner-check as its scoped `sub` AND deny owner-less artifacts (close the cross-user /
    # owner-less exfil vector a member-authored sandbox script could otherwise hit). A
    # normal user/browser request → existing JWT/header identity, owner-less stays readable.
    _fetch_sub = _artifact_fetch_identity(request)
    if _fetch_sub is not None:
        user_id, _reject_null = _fetch_sub, True
    else:
        user_id, _reject_null = _user_id(request), False
    art = await store.get(artifact_id, check_created_by=user_id, reject_null_owner=_reject_null)
    if art is None and _fetch_sub is None:
        # P3: failed owner check -> freshly check sharing (no cache, so revocation takes effect immediately).
        # Still absent -> silent 404, revealing no existence.
        from bowhead_gateway.files_sharing import can_read_shared

        # A Space denial here must look like "not found": otherwise a missing id (403 from the
        # caller's own Space header) differs from an owner-less file in that Space (404).
        with _concealing_denials(artifact_id, user_id):
            ctx = await _share_ctx(
                request,
                operation=SpaceOperation.READ_HISTORY,
            )
        if await can_read_shared(
            getattr(request.app.state, "access_control_store", None),
            artifact_id=artifact_id,
            team_id=ctx.team_id,
            user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            group_ids=ctx.group_ids,
        ):
            art = await store.get(artifact_id)
    if art is None:
        raise _file_not_found()
    await _require_artifact_visible(
        request,
        store,
        art,
        operation=SpaceOperation.READ_HISTORY,
        user_id=user_id,
    )

    if art.producer_kind == "template_document_v1":
        from urllib.parse import quote

        from bowhead_core.documents.request import DocumentRequestError

        from bowhead_gateway.routes.documents import published_file_identity

        scope, job_id, name = await published_file_identity(request, art, user_id)
        try:
            content, mime = await request.app.state.document_service.delivery.read_file(scope=scope, job_id=job_id, name=name, preview=False)
        except DocumentRequestError as exc:
            raise _file_error(404 if exc.code == "document_not_found" else 409, exc.code) from None
        return Response(
            content=content,
            media_type=mime,
            headers={
                "Cache-Control": "no-store, private",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(art.filename, safe=""),
            },
        )

    # Missing original file (reconciliation marked missing) -> immediate 410, no S3 presigned URL doomed to 404.
    if art.status == "missing":
        raise _file_error(status.HTTP_410_GONE, "file_content_missing")

    if (art.mime or "").split(";", 1)[0].strip().lower() in {"text/html", "application/xhtml+xml"}:
        from bowhead_gateway.artifact_streaming import artifact_streaming_response

        # Keep the Team preview limit on the authorized response, including
        # object-store artifacts; a redirect would lose this response metadata.
        return await artifact_streaming_response(request, store, art, inline=False)

    # multimodal-v1: inline display (<img> in chat/canvas), owner already checked above.
    # Inline and attachment share identical authorization. Allowlist only safe raster types and videos
    # native media elements decode; SVG/GIF/HTML etc. still force attachment.
    _inline = request.query_params.get("disposition") == "inline" and (art.mime or "").lower() in INLINE_MEDIA_MIMES

    # S3 / MinIO -> 302 to presigned URL for direct browser access.
    url = await store.download_url(
        art,
        expires_in=3600,
        disposition="inline" if _inline else "attachment",
    )
    if url:
        return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)

    # Local backend → FileResponse
    path = Path(art.path)
    if not path.is_file():
        raise _file_error(status.HTTP_410_GONE, "file_content_missing")
    if _inline:
        # Omit filename=: FileResponse(filename=) creates an attachment header overriding inline;
        # nosniff prevents MIME-sniffing XSS (review SEC-P0-1/P0-3).
        return FileResponse(
            path=str(path),
            media_type=art.mime,
            headers={
                "Content-Disposition": "inline",
                "X-Content-Type-Options": "nosniff",
            },
        )
    return FileResponse(
        path=str(path),
        media_type=art.mime,
        filename=art.filename,
    )
