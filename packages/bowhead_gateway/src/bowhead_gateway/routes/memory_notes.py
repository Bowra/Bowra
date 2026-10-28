"""V3.5 PRD-C · /api/v1/memory/notes routes.

Endpoints:
  GET    /notes                          → MemoryNoteDTO (computed counts + lock state)
  PATCH  /notes                          → MemoryNoteDTO (after PATCH)
  POST   /notes/append                   → MemoryNoteDTO (after append)
  DELETE /notes/bullet?bullet_index=N    → MemoryNoteDTO (after delete)
  POST   /notes/lock                     → NotesLockResultDTO
  POST   /notes/unlock                   → {ok: true}

All mutating endpoints emit audit + telemetry like PRD-B (see _audit, _track helpers
in routes/memory.py — duplicated here for 模块独立; consolidate later).

Cross-user enforcement: _user_id() returns auth-resolved uid; all queries scope to
that uid via store API. There's no path-level user_id, so cross-user attempts are
naturally blocked.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bowhead_core.memory_notes import (
    MAX_CONTENT_CHARS,
    MemoryNotesStore,
    NotesCharLimitError,
    NotesEditTokenError,
    NotesLockedError,
    NotesVersionConflictError,
    parse_notes_lines,
)
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from bowhead_gateway.dtos import (
    MemoryNoteDTO,
    NoteAppendRequest,
    NotesLockResultDTO,
    NotesPatchRequest,
)

if TYPE_CHECKING:
    from bowhead_core.metrics_log import MetricsLog

logger = logging.getLogger(__name__)
router = APIRouter()


def _store(request: Request) -> MemoryNotesStore:
    s = getattr(request.app.state, "memory_notes_store", None)
    if s is None:
        raise HTTPException(503, "memory notes store not configured")
    return s


def _user_id(request: Request) -> str:
    from bowhead_gateway.routes.auth import resolve_user_id_sync

    return resolve_user_id_sync(request)


def _parse_version_header(value: str) -> int:
    token = value.strip()
    if token.startswith('"') and token.endswith('"') and len(token) >= 2:
        token = token[1:-1]
    if not token.isdecimal():
        raise HTTPException(
            400,
            {"code": "memory_version_invalid"},
        )
    return int(token)


async def _audit(
    request: Request,
    *,
    user_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    user_store = getattr(request.app.state, "user_store", None)
    if user_store is None:
        return
    try:
        await user_store.log_audit(
            user_id=user_id,
            team_id=None,
            event_type=event_type,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            payload=payload,
        )
    except Exception as e:
        logger.debug("memory_notes.audit_failed event=%s err=%s", event_type, e)


async def _track(
    request: Request,
    *,
    name: str,
    user_id: str | None = None,
    attrs: dict[str, Any] | None = None,
) -> None:
    log: MetricsLog | None = getattr(request.app.state, "metrics_log", None)
    if log is None:
        return
    await log.track(name, user_id=user_id, attrs=attrs)


async def _build_bullet_anchors_dto(
    *,
    request: Request,
    user_id: str,
) -> list:
    """V4 Sprint 2 PRD-D-4-1: per-bullet anchor list with read-time recompute fallback.

    Returns [] when:
      - kill switch off (BOWHEAD_ANCHOR_INVALIDATION_ENABLED=0)
      - anchors store not configured
      - user has no anchors
    """
    if not getattr(request.app.state, "anchor_invalidation_enabled", False):
        return []
    anchors_store = getattr(request.app.state, "memory_note_anchors_store", None)
    if anchors_store is None:
        return []
    anchors = await anchors_store.list_for_user(user_id)
    if not anchors:
        return []

    # Read-time recompute fallback (cascade gap defense). Cache per canvas_id.
    canvas_store = getattr(request.app.state, "canvas_store", None)
    section_cache: dict[str, list] = {}
    out = []
    from dataclasses import replace

    for a in anchors:
        if a.status == "valid" and canvas_store is not None:
            try:
                if a.canvas_id not in section_cache:
                    section_cache[a.canvas_id] = await canvas_store.list_sections(
                        a.canvas_id,
                    )
                sections = section_cache[a.canvas_id]
                section = next((s for s in sections if s.id == a.section_id), None)
                if section is None:
                    a = replace(
                        a,
                        status="stale",
                        invalidation_reason="canvas_deleted_runtime",
                    )
                else:
                    new_hash = canonical_payload_hash(section.payload)
                    if new_hash != a.verifier_result_hash:
                        a = replace(
                            a,
                            status="stale",
                            invalidation_reason="payload_changed_runtime",
                        )
            except Exception as e:
                logger.debug("anchor.read_recompute_skip user=%s err=%s", user_id, e)
        out.append(
            {
                "bullet_text": a.bullet_text,
                "canvas_id": a.canvas_id,
                "section_id": a.section_id,
                "verifier_result_hash": a.verifier_result_hash,
                "saved_at": a.saved_at,
                "status": a.status,
                "invalidated_at": a.invalidated_at,
                "invalidation_reason": a.invalidation_reason,
            }
        )
    return out


async def _to_dto(
    store: MemoryNotesStore,
    user_id: str,
    *,
    request: Request | None = None,
) -> MemoryNoteDTO:
    note = await store.get(user_id=user_id)
    version_note = note
    if version_note is None:
        version_note = await store.get(
            user_id=user_id,
            include_trashed=True,
        )
    is_locked = await store.is_lock_active(user_id=user_id)
    lock_expires = await store.get_lock_expiry(user_id=user_id) if is_locked else None
    bullet_anchors = await _build_bullet_anchors_dto(request=request, user_id=user_id) if request is not None else []
    if note is None:
        return MemoryNoteDTO(
            user_id=user_id,
            version=str(version_note.version if version_note else 0),
            content="",
            char_count=0,
            char_limit=MAX_CONTENT_CHARS,
            pinned_count=0,
            bullet_count=0,
            free_text_lines=0,
            is_locked=is_locked,
            lock_expires_at=lock_expires,
            bullet_anchors=bullet_anchors,
        )
    counts = parse_notes_lines(note.content)
    return MemoryNoteDTO(
        user_id=user_id,
        version=str(note.version),
        content=note.content,
        char_count=counts["char_count"],
        char_limit=MAX_CONTENT_CHARS,
        pinned_count=counts["pinned_count"],
        bullet_count=counts["bullet_count"],
        free_text_lines=counts["free_text_lines"],
        last_edited_at=note.updated_at,
        last_edited_source=note.source,
        is_locked=is_locked,
        lock_expires_at=lock_expires,
        bullet_anchors=bullet_anchors,
        # Sprint 1 deprecated row-level fields kept for backward compatibility (always None/'valid'
        # in Sprint 2 since save-from-canvas switched to sidecars).
        verification_anchor=note.verification_anchor,
        verification_status=note.verification_status,
    )


# ─── endpoints ────────────────────────────────────────────


@router.get("/notes", response_model=MemoryNoteDTO)
async def get_notes(
    request: Request,
    store: MemoryNotesStore = Depends(_store),
) -> MemoryNoteDTO:
    return await _to_dto(store, _user_id(request), request=request)


@router.patch("/notes", response_model=MemoryNoteDTO)
async def patch_notes(
    body: NotesPatchRequest,
    request: Request,
    store: MemoryNotesStore = Depends(_store),
) -> MemoryNoteDTO:
    """Save About-you content using the owner lease and version CAS."""

    uid = _user_id(request)
    if len(body.content) > MAX_CONTENT_CHARS:
        raise HTTPException(413, f"content > {MAX_CONTENT_CHARS} char")
    try:
        await store.commit_edit(
            user_id=uid,
            content=body.content,
            expected_version=int(body.version),
            edit_token=body.edit_token,
        )
    except NotesCharLimitError as e:
        raise HTTPException(413, str(e)) from None
    except NotesEditTokenError:
        raise HTTPException(
            423,
            {"code": "memory_about_you_edit_session_invalid"},
        ) from None
    except NotesVersionConflictError as e:
        raise HTTPException(
            409,
            {
                "code": "memory_version_conflict",
                "retryable": True,
                "current_version": str(e.current_version),
            },
        ) from None

    await _audit(
        request,
        user_id=uid,
        event_type="memory.notes.patch",
        payload={"char_count": len(body.content)},
    )
    await _track(
        request,
        name="memory.notes.patch",
        user_id=uid,
        attrs={"char_count": len(body.content)},
    )
    return await _to_dto(store, uid, request=request)


@router.post("/notes/append", response_model=MemoryNoteDTO)
async def append_bullet(
    body: NoteAppendRequest,
    request: Request,
    store: MemoryNotesStore = Depends(_store),
) -> MemoryNoteDTO:
    uid = _user_id(request)
    try:
        await store.append(
            user_id=uid,
            bullet=body.bullet,
            pinned=body.pinned,
            source=body.source,
            source_turn_id=body.source_turn_id,
            ignore_lock=body.ignore_lock,
        )
    except NotesLockedError as e:
        raise HTTPException(
            status_code=423,
            detail={
                "code": "memory_about_you_editing",
                "lock_expires_at": e.lock_expires_at,
            },
        ) from None
    except NotesCharLimitError as e:
        raise HTTPException(413, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None

    await _audit(
        request,
        user_id=uid,
        event_type="memory.notes.append",
        payload={"pinned": body.pinned, "source": body.source},
    )
    await _track(
        request,
        name="memory.notes.append",
        user_id=uid,
        attrs={"pinned": body.pinned, "source": body.source},
    )
    return await _to_dto(store, uid, request=request)


@router.delete("/notes/bullet", response_model=MemoryNoteDTO)
async def delete_bullet(
    request: Request,
    bullet_index: int = Query(..., ge=0),
    ignore_lock: bool = Query(False),
    if_match: str = Header(..., alias="If-Match"),
    edit_token: str = Header(..., alias="X-Memory-Edit-Token"),
    store: MemoryNotesStore = Depends(_store),
) -> MemoryNoteDTO:
    """Delete a bullet through the same owner lease and CAS as full edits."""

    uid = _user_id(request)
    _ = ignore_lock

    # V4 Sprint 2 PRD-D-4-1: capture bullet text BEFORE delete, so we can
    # cascade-remove the sidecar anchor row by bullet_text match.
    from bowhead_core.memory_notes import parse_notes_to_lines

    deleted_bullet_text: str | None = None
    note_pre = await store.get(user_id=uid)
    if note_pre and note_pre.content:
        lines = parse_notes_to_lines(note_pre.content)
        bullet_lines = [ln for ln in lines if ln["kind"] in ("pinned", "bullet")]
        if 0 <= bullet_index < len(bullet_lines):
            deleted_bullet_text = bullet_lines[bullet_index]["text"]

    try:
        await store.delete_bullet_with_edit(
            user_id=uid,
            bullet_index=bullet_index,
            expected_version=_parse_version_header(if_match),
            edit_token=edit_token,
        )
    except IndexError as e:
        raise HTTPException(400, str(e)) from None
    except NotesEditTokenError:
        raise HTTPException(
            423,
            {"code": "memory_about_you_edit_session_invalid"},
        ) from None
    except NotesVersionConflictError as e:
        raise HTTPException(
            409,
            {
                "code": "memory_version_conflict",
                "retryable": True,
                "current_version": str(e.current_version),
            },
        ) from None

    # cascade delete sidecar anchor (best-effort, post-bullet-delete)
    anchors_store = getattr(request.app.state, "memory_note_anchors_store", None)
    if anchors_store is not None and deleted_bullet_text:
        try:
            await anchors_store.delete_by_bullet(
                user_id=uid,
                bullet_text=deleted_bullet_text,
            )
        except Exception as e:
            logger.warning("anchor.delete_cascade_failed user=%s err=%s", uid, e)

    await _audit(
        request,
        user_id=uid,
        event_type="memory.notes.delete_bullet",
        payload={"bullet_index": bullet_index},
    )
    await _track(
        request,
        name="memory.notes.delete_bullet",
        user_id=uid,
        attrs={"bullet_index": bullet_index},
    )
    return await _to_dto(store, uid, request=request)


class _LockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: str | None = Field(
        default=None,
        pattern=r"^[0-9]+$",
    )
    edit_token: str | None = Field(default=None, min_length=32, max_length=256)


@router.post("/notes/lock", response_model=NotesLockResultDTO)
async def lock_notes(
    request: Request,
    body: _LockRequest | None = None,
    store: MemoryNotesStore = Depends(_store),
) -> NotesLockResultDTO:
    uid = _user_id(request)
    current = await store.get(user_id=uid, include_trashed=True)
    expected_version = int(body.expected_version) if body and body.expected_version is not None else current.version if current else 0
    try:
        lease = await store.begin_edit_session(
            user_id=uid,
            expected_version=expected_version,
            edit_token=body.edit_token if body else None,
            ttl_seconds=300,
        )
    except NotesLockedError as e:
        raise HTTPException(
            status_code=423,
            detail={
                "code": "memory_about_you_editing",
                "lock_expires_at": e.lock_expires_at,
            },
        ) from None
    except NotesVersionConflictError as e:
        raise HTTPException(
            409,
            {
                "code": "memory_version_conflict",
                "retryable": True,
                "current_version": str(e.current_version),
            },
        ) from None
    return NotesLockResultDTO(
        locked_until=lease.expires_at,
        edit_token=lease.edit_token,
        version=str(lease.version),
    )


class _UnlockResp(BaseModel):
    ok: bool = True


class _UnlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edit_token: str | None = Field(default=None, min_length=32, max_length=256)


@router.post("/notes/unlock", response_model=_UnlockResp)
async def unlock_notes(
    request: Request,
    body: _UnlockRequest | None = None,
    store: MemoryNotesStore = Depends(_store),
) -> _UnlockResp:
    await store.release_lock(
        user_id=_user_id(request),
        edit_token=body.edit_token if body else None,
    )
    return _UnlockResp(ok=True)


# ─── V4 PRD-D-5 save-from-canvas ──────────────────────────


# Verified-fact whitelist per Day 0 spike (real canvas section.kind values).
# narrative / insight_card / chart_placeholder / sql_block / sources are NOT
# verified-fact (LLM-text or metadata). See V4-sprint-1-spike-report.md §1.2.
_VERIFIED_FACT_KINDS = {"kpi_row", "chart", "data_table"}


# Re-export from core (Sprint 2 PRD-D-4-1 — single source of truth).
from bowhead_core.memory_note_anchors import canonical_payload_hash  # noqa: E402

_canonical_payload_hash = canonical_payload_hash  # backward-compatible alias


class SaveFromCanvasRequest(BaseModel):
    canvas_id: str
    section_id: str
    summary_text: str  # 1..200 char (validated server-side)
    # memory-revive S1 volume guard: unpinned by default. Pinned notes always lead budget truncation order (pinned>recent>old);
    # default pinning canvas entries permanently monopolizes the memory profile's front rows (discovery §5.2 main risk).
    pinned: bool = False


@router.post("/notes/save-from-canvas", response_model=MemoryNoteDTO)
async def save_from_canvas(
    body: SaveFromCanvasRequest,
    request: Request,
    store: MemoryNotesStore = Depends(_store),
) -> MemoryNoteDTO:
    """V4 PRD-D-5: append a verified-fact bullet from a canvas section.

    Validates canvas/section ownership and verified-fact kind whitelist before
    appending, then writes verification_anchor (canvas_id / section_id /
    sha256 of payload) to the notes row.

    404 errors are intentionally opaque (canvas not found / not yours / section
    not found / wrong kind all map to 404 — never reveal cross-user existence).
    """
    # V4 PRD-D-5 master kill-switch (independent of replay snapshot).
    if not getattr(request.app.state, "memory_from_canvas_enabled", False):
        raise HTTPException(503, "memory↔canvas writes disabled")
    uid = _user_id(request)
    if not body.summary_text or not body.summary_text.strip():
        raise HTTPException(400, "summary_text cannot be empty")
    text = body.summary_text.strip()
    if len(text) > 200:
        raise HTTPException(400, "summary_text > 200 char")

    canvas_store = getattr(request.app.state, "canvas_store", None)
    if canvas_store is None:
        raise HTTPException(503, "canvas store not configured")
    canvas = await canvas_store.get_canvas(body.canvas_id)
    if canvas is None or canvas.user_id != uid:
        # opaque 404: cross-user attempts indistinguishable from missing
        raise HTTPException(404, "canvas not found")
    sections = await canvas_store.list_sections(body.canvas_id)
    section = next((s for s in sections if s.id == body.section_id), None)
    if section is None:
        raise HTTPException(404, "section not found")
    if section.kind not in _VERIFIED_FACT_KINDS:
        raise HTTPException(
            400,
            f"section kind '{section.kind}' is not a verified-fact (whitelist: {sorted(_VERIFIED_FACT_KINDS)})",
        )

    verifier_hash = canonical_payload_hash(section.payload)

    # Step 1: append bullet to memory_notes content (Sprint 2: skip row-level
    # anchor write — that path is deprecated/dead).
    try:
        await store.append(
            user_id=uid,
            bullet=text,
            pinned=body.pinned,
            source="verified_in_report",
            source_turn_id=None,
            ignore_lock=True,
        )
    except NotesCharLimitError as e:
        raise HTTPException(413, str(e)) from None
    except NotesLockedError as e:
        raise HTTPException(
            423,
            {
                "code": "memory_about_you_editing",
                "lock_expires_at": e.lock_expires_at,
            },
        ) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None

    # Step 2: write per-bullet anchor to sidecar (V4 Sprint 2 path).
    # Failure here does NOT roll back the bullet — bullet is the durable state,
    # anchor is recoverable via re-click. Audit the partial failure.
    from bowhead_core.memory_note_anchors import AnchorCapExceeded

    anchors_store = getattr(request.app.state, "memory_note_anchors_store", None)
    if anchors_store is not None:
        try:
            await anchors_store.insert_or_revive(
                user_id=uid,
                bullet_text=text,
                canvas_id=body.canvas_id,
                section_id=body.section_id,
                verifier_result_hash=verifier_hash,
            )
        except AnchorCapExceeded:
            # Cap hit — bullet already added, but anchor count exceeded. Surface to user.
            raise HTTPException(
                413,
                "anchor count exceeds 50; delete some pinned bullets first",
            ) from None
        except Exception as e:
            logger.warning(
                "anchor.save_partial_failure user=%s err=%s",
                uid,
                e,
            )
            await _audit(
                request,
                user_id=uid,
                event_type="memory.anchor.save_partial_failure",
                payload={
                    "canvas_id": body.canvas_id,
                    "section_id": body.section_id,
                    "error": str(e),
                },
            )
            # fall through — return 200 with bullet present, anchor missing

    await _audit(
        request,
        user_id=uid,
        event_type="memory.notes.save_from_canvas",
        payload={
            "canvas_id": body.canvas_id,
            "section_id": body.section_id,
            "section_kind": section.kind,
            "verifier_result_hash": verifier_hash,
        },
    )
    await _track(
        request,
        name="memory.notes.save_from_canvas",
        user_id=uid,
        attrs={"section_kind": section.kind},
    )

    return await _to_dto(store, uid, request=request)
