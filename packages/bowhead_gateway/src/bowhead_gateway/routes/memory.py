"""User Memory HTTP API.

V3 PRD-B 改造: backend for unified memory page (4 sub-stores aggregated).

Existing (kept for backward compat):
  GET    /api/v1/memory/user                 → list active memory_entries (legacy)
  POST   /api/v1/memory/user/remember        → upsert memory entry
  DELETE /api/v1/memory/user/{entry_id}      → soft-delete memory entry

V3 PRD-B new:
  GET    /api/v1/memory/snapshot             → unified DTO (entries + stats + dreaming state)
  GET    /api/v1/memory/trash                → trashed entries (memory)
  POST   /api/v1/memory/{entry_id}/restore   → restore from trash
  POST   /api/v1/memory/reset                → soft-delete all unlocked + save snapshot
  GET    /api/v1/memory/export               → streaming JSON of all user memory
  GET    /api/v1/memory/dreaming             → paused state
  POST   /api/v1/memory/dreaming/pause       → set paused=true
  POST   /api/v1/memory/dreaming/resume      → set paused=false
"""

from __future__ import annotations

import base64
import json
import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from bowhead_core._db import open_shared_ctx
from bowhead_core.memory_kind_policy import resolve_memory_policy
from bowhead_core.memory_notes import (
    MemoryNotesStore,
    NotesCharLimitError,
    NotesEditTokenError,
    NotesLockedError,
    NotesVersionConflictError,
    parse_notes_lines,
)
from bowhead_core.memory_privacy import (
    MemoryDestructiveOperationConflictError,
    MemoryDestructiveOperationStore,
    MemoryDestructiveTargetNotFoundError,
    MemoryIdempotencyConflictError,
    PrivacyDeletionNotReadyError,
)
from bowhead_core.user_memory import (
    MemoryAboutYouNotFoundError,
    MemoryAboutYouVersionConflictError,
    MemoryEntryLockedError,
    MemoryEntryNotFoundError,
    MemoryEntrySchemaError,
    MemoryEntryStateError,
    MemoryResetBlockedError,
    MemoryResetSnapshotNotFoundError,
    MemoryScopeConflictError,
    MemoryVersionConflictError,
)
from bowhead_memory.candidates import (
    SETTINGS_KEY_DREAMING_PAUSED,
    MemoryCandidateStore,
    PauseStateCache,
)
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from bowhead_gateway.dtos import (
    CanonicalMemoryCreatedFromDTO,
    CanonicalMemoryDisplaySchemaDTO,
    CanonicalMemoryItemCapabilitiesDTO,
    CanonicalMemoryItemDTO,
    CanonicalMemoryItemsPageDTO,
    CanonicalMemoryLastModifiedDTO,
    CanonicalMemoryProvenanceDTO,
    CanonicalMemoryScopeDTO,
    CanonicalMemorySupersededByDTO,
    DeletedDTO,
    DreamingStateDTO,
    MemoryAboutYouDeleteDTO,
    MemoryAboutYouEditSessionDTO,
    MemoryAboutYouV2DTO,
    MemoryCapabilitiesDTO,
    MemoryCapabilityActionsDTO,
    MemoryDestructiveOperationDTO,
    MemoryEntryDTO,
    MemoryLearningV2DTO,
    MemoryListDTO,
    MemoryNoteDTO,
    MemoryOverviewStatsDTO,
    MemoryOverviewV2DTO,
    MemoryResetDetailDTO,
    MemoryResetListDTO,
    MemoryResetPreviewDTO,
    MemorySnapshotDTO,
    MemoryStatsDTO,
    MemoryUpsertResultDTO,
    ResetMemoryResultDTO,
    RestoreResultDTO,
    TrashListDTO,
    TurnMemoryExplicitRequestDTO,
    TurnMemoryReceiptDTO,
    TurnMemoryReceiptEntryDTO,
    UndoResetResultDTO,
    UnifiedMemoryItemDTO,
)
from bowhead_gateway.routes.memory_candidates import (
    _reason_memory_texts,
    accessible_candidate_spaces,
    candidate_to_dto,
)

if TYPE_CHECKING:
    from bowhead_core.metrics_log import MetricsLog
    from bowhead_core.user_settings_store import UserSettingsStore

    from bowhead_core import UserMemoryStore

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── store accessors ──────────────────────────────────────


def _user_memory_store(request: Request) -> UserMemoryStore:
    store = getattr(request.app.state, "user_memory_store", None)
    if store is None:
        raise HTTPException(503, "user memory not configured")
    return store


def _notes_store(request: Request) -> MemoryNotesStore | None:
    return getattr(request.app.state, "memory_notes_store", None)


def _candidate_store(request: Request) -> MemoryCandidateStore | None:
    return getattr(request.app.state, "memory_candidate_store", None)


def _settings_store(request: Request) -> UserSettingsStore | None:
    return getattr(request.app.state, "user_settings_store", None)


def _destructive_operation_store(
    request: Request,
) -> MemoryDestructiveOperationStore | None:
    return getattr(
        request.app.state,
        "memory_destructive_operation_store",
        None,
    )


def _metrics_log(request: Request) -> MetricsLog | None:
    return getattr(request.app.state, "metrics_log", None)


def _pause_cache(request: Request) -> PauseStateCache | None:
    return getattr(request.app.state, "pause_state_cache", None)


def _user_id(request: Request) -> str:
    from bowhead_gateway.routes.auth import resolve_user_id_sync

    return resolve_user_id_sync(request)


async def _audit(
    request: Request,
    *,
    user_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Best-effort audit log via UserStore.log_audit."""
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
        logger.debug("memory.audit_failed event=%s err=%s", event_type, e)


async def _track(
    request: Request,
    *,
    name: str,
    user_id: str | None = None,
    attrs: dict[str, Any] | None = None,
) -> None:
    log = _metrics_log(request)
    if log is None:
        return
    await log.track(name, user_id=user_id, attrs=attrs)


def _memory_to_unified(e: Any) -> UnifiedMemoryItemDTO:
    return UnifiedMemoryItemDTO(
        id=e.id,
        origin="memory",
        kind=e.kind,
        key=e.key,
        value=e.parsed_value(),
        locked=e.locked,
        lock_reason=e.lock_reason,
        source=e.source,
        created_at=e.created_at,
        updated_at=e.updated_at,
        deleted_at=e.deleted_at,
    )


# ─── legacy endpoints (backward compat) ──────────────────


_MAX_VALUE_BYTES = 16 * 1024  # 16 KB after JSON serialization (per-entry cap)


class RememberRequest(BaseModel):
    kind: str = Field(
        default="preference",
        pattern="^(preference|constraint|datarc_binding)$",
    )
    key: str = Field(min_length=1, max_length=128)
    value: Any

    def value_size_ok(self) -> bool:
        try:
            return len(json.dumps(self.value, ensure_ascii=False, default=str)) <= _MAX_VALUE_BYTES
        except (TypeError, ValueError):
            return False


@router.get("/user", response_model=MemoryListDTO)
async def list_memory(
    request: Request,
    store: UserMemoryStore = Depends(_user_memory_store),
) -> MemoryListDTO:
    uid = _user_id(request)
    entries = await store.list_for_user(uid)
    return MemoryListDTO(
        user_id=uid,
        entries=[
            MemoryEntryDTO(
                id=e.id,
                kind=e.kind,
                key=e.key,
                value=e.parsed_value(),
                locked=e.locked,
                lock_reason=e.lock_reason,
                source=e.source,
                created_at=e.created_at,
                updated_at=e.updated_at,
                deleted_at=e.deleted_at,
            )
            for e in entries
        ],
    )


@router.post("/user/remember", response_model=MemoryUpsertResultDTO)
async def remember(
    body: RememberRequest,
    request: Request,
    store: UserMemoryStore = Depends(_user_memory_store),
) -> MemoryUpsertResultDTO:
    if not body.value_size_ok():
        raise HTTPException(
            413,
            f"value exceeds {_MAX_VALUE_BYTES} bytes",
        )
    uid = _user_id(request)

    # Memory redesign batch 2: manual creation on the memory page also passes the write gate.
    # ★ check()'s docstring explicitly names this route (no turn_id, so criterion ② must recognize
    # the UI actor), but previously **no call anywhere passed actor_type='user'**. The parameter
    # and its tests expressed intent with no runtime implementation: the same api_key=sk-... text
    # was rejected through the agent but saved through the memory page.
    from bowhead_core.memory_write_gate import check as _gate_check
    from bowhead_core.memory_write_policy import comparable_text as _comparable

    verdict = _gate_check(
        # ★ Same normalization as the other two write paths: comparable_text(json.dumps(...)).
        # Previously str(body.value): dict values' Python repr (single quotes)
        # evaded secret scanning, the third copy of the pitfall documented by combined_text.
        value_text=_comparable(json.dumps(body.value, ensure_ascii=False)),
        has_turn_id=False,
        actor_type="user",
        key=body.key,
    )
    if not verdict.allowed:
        raise HTTPException(
            422,
            {"code": "memory_write_gate_refused", "reason": verdict.reason, "message": verdict.message},
        )

    try:
        entry = await store.upsert_product_memory(
            user_id=uid,
            kind=body.kind,
            key=body.key,
            value=body.value,
            source="user_explicit",
            scope_specified=True,
        )
    except MemoryScopeConflictError as exc:
        raise HTTPException(
            409,
            {
                "code": "memory_scope_conflict",
                "entry_id": exc.entry_id,
                "existing_scope": exc.existing_workspace_id or "global",
                "requested_scope": "global",
            },
        ) from None
    except MemoryEntryLockedError:
        raise HTTPException(423, "memory entry is locked") from None
    await _audit(
        request,
        user_id=uid,
        event_type="memory.entry.create",
        payload={"id": entry.id, "kind": entry.kind},
    )
    await _track(
        request,
        name="memory.entry.create",
        user_id=uid,
        attrs={"kind": entry.kind, "source": "user_explicit"},
    )
    return MemoryUpsertResultDTO(
        id=entry.id,
        kind=entry.kind,
        key=entry.key,
        value=entry.parsed_value(),
    )


@router.delete("/user/{entry_id}", response_model=DeletedDTO)
async def delete_entry(
    entry_id: str,
    request: Request,
    store: UserMemoryStore = Depends(_user_memory_store),
) -> DeletedDTO:
    """Legacy delete — preserved for old clients. New clients should use
    DELETE /api/v1/memory/{entry_id} which routes through the unified handler."""
    uid = _user_id(request)
    ok = await store.soft_delete(user_id=uid, entry_id=entry_id)
    if not ok:
        raise HTTPException(404, "entry not found or locked")
    await _audit(
        request,
        user_id=uid,
        event_type="memory.entry.delete",
        payload={"id": entry_id, "origin": "memory"},
    )
    await _track(
        request,
        name="memory.entry.delete",
        user_id=uid,
        attrs={"origin": "memory"},
    )
    return DeletedDTO(deleted=entry_id)


# ─── V3 PRD-B unified endpoints ──────────────────────────


@router.get("/turn-receipt", response_model=TurnMemoryReceiptDTO)
async def turn_memory_receipt(
    request: Request,
    response: Response,
    turn_id: str = Query(..., min_length=1, max_length=128),
    store: UserMemoryStore = Depends(_user_memory_store),
) -> TurnMemoryReceiptDTO:
    """The per-turn memory receipt: explicit request, pending candidates and created entries.

    A REST projection polled after `done`, read only from persisted state (`memory_turn_requests`,
    `memory_candidates`, `memory_entries`), so the live view, a refresh and a replay agree. It adds no
    turn event. Entry `state` is derived at read time: trashed → `undone`, restored → `remembered`,
    purged → omitted.

    Only the owner of the turn's session may read it. Any other caller gets 404, so the endpoint
    does not reveal whether another user's turn holds a memory request.
    """
    from bowhead_core.memory_auto_capture import CAPTURE_SOURCE
    from bowhead_core.memory_turn_requests import (
        SOURCE_REMEMBER_TOOL,
        MemoryTurnRequestStore,
        public_reason,
        select_explicit_request,
    )

    from bowhead_gateway.routes.memory_candidates import _supersedes_item_map

    response.headers["Cache-Control"] = "no-store, private"
    uid = _user_id(request)
    session_store: Any | None = getattr(request.app.state, "session_store", None)
    if session_store is None:
        raise HTTPException(503, "session store not configured")
    turn = await session_store.get_turn(turn_id)
    session = await session_store.get_session(getattr(turn, "session_id", "") or "") if turn is not None else None
    if session is None or getattr(session, "user_id", None) != uid:
        raise HTTPException(404, "turn not found")

    requests = await MemoryTurnRequestStore(store.db_path).list_for_turn(user_id=uid, turn_id=turn_id)
    pause_cache = _pause_cache(request)
    if pause_cache is not None and await pause_cache.is_paused(uid):
        # Paused learning shows no automatic-path request; an explicit `remember` call still reports.
        requests = [r for r in requests if r.source == SOURCE_REMEMBER_TOOL]
    selected = select_explicit_request(requests)

    pending: list[Any] = []
    candidate_store = _candidate_store(request)
    if candidate_store is not None:
        candidates = await candidate_store.list_for_user(user_id=uid, status="pending", limit=10, source_turn_id=turn_id)
        accessible_spaces = await accessible_candidate_spaces(request, uid)
        supersedes_items = await _supersedes_item_map(store, uid, candidates)
        memory_texts = await _reason_memory_texts(store, uid, candidates)
        pending = [
            candidate_to_dto(c, accessible_spaces=accessible_spaces, supersedes_items=supersedes_items, memory_texts=memory_texts) for c in candidates
        ]

    entries: list[TurnMemoryReceiptEntryDTO] = []
    for entry in await store.list_created_in_turn(uid, turn_id=turn_id):
        if entry.created_source_candidate_id:
            origin = "candidate"
        elif entry.source == CAPTURE_SOURCE:
            origin = "capture"
        elif entry.source == "user_directive":
            origin = "remember_tool"
        else:
            # Unknown writers are not claimed as this turn's memory.
            continue
        undone = entry.deleted_at is not None
        entries.append(
            TurnMemoryReceiptEntryDTO(
                id=entry.id,
                kind=entry.kind,
                key=entry.key,
                # Same display rule as the memory page: non-strings are JSON with real characters.
                display_value=_display_memory_value(entry.parsed_value()),
                origin=origin,
                state="undone" if undone else "remembered",
                undoable=not undone and not entry.locked,
                candidate_id=entry.created_source_candidate_id,
            )
        )

    return TurnMemoryReceiptDTO(
        turn_id=turn_id,
        explicit_request=(
            TurnMemoryExplicitRequestDTO(
                source=selected.source,
                outcome=selected.outcome,
                reason=public_reason(selected.reason),
            )
            if selected is not None
            else None
        ),
        pending_candidates=pending,
        entries=entries,
    )


@router.get("/capabilities", response_model=MemoryCapabilitiesDTO)
async def memory_capabilities(
    request: Request,
    response: Response,
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> MemoryCapabilitiesDTO:
    response.headers["Cache-Control"] = "no-store, private"
    uid = _user_id(request)
    return await _build_memory_capabilities(
        request,
        user_id=uid,
        user_mem=user_mem,
    )


async def _build_memory_capabilities(
    request: Request,
    *,
    user_id: str,
    user_mem: UserMemoryStore,
) -> MemoryCapabilitiesDTO:
    ready, reason = await user_mem.v2_readiness()

    required_stores = (
        "personal_memory_governance_store",
        "memory_candidate_store",
        "memory_notes_store",
    )
    if ready and any(getattr(request.app.state, name, None) is None for name in required_stores):
        ready = False
        reason = "memory_governance_store_unavailable"

    candidate_store = _candidate_store(request)
    if ready and candidate_store is not None:
        pending = await candidate_store.list_for_user(
            user_id=user_id,
            status="pending",
            limit=500,
        )
        seen: set[tuple[str, str, str | None]] = set()
        for candidate in pending:
            fingerprint = (
                candidate.kind,
                json.dumps(
                    candidate.value,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                candidate.workspace_id,
            )
            if fingerprint in seen:
                ready = False
                reason = "memory_candidate_deduplication_incomplete"
                break
            seen.add(fingerprint)

    permanent_delete_ready = False
    permanent_delete_reason = "memory_destructive_store_unavailable"
    destructive_store = _destructive_operation_store(request)
    if destructive_store is not None:
        permanent_delete_ready, permanent_delete_reason = await destructive_store.readiness()
    return MemoryCapabilitiesDTO(
        v2_enabled=True,
        v2_ready=ready,
        presentation="v2",
        actions=MemoryCapabilityActionsDTO(
            edit=ready,
            permanent_delete=(ready and permanent_delete_ready),
            recent_resets=ready,
            destructive_operations=(ready and permanent_delete_ready),
        ),
        permanent_delete_ready=permanent_delete_ready,
        permanent_delete_reason=permanent_delete_reason,
        reason=reason,
    )


def _require_memory_ready(capabilities: MemoryCapabilitiesDTO) -> None:
    if not capabilities.v2_ready:
        raise HTTPException(
            503,
            {
                "code": "memory_v2_not_ready",
                "reason": capabilities.reason,
            },
        )


def _require_permanent_delete(
    capabilities: MemoryCapabilitiesDTO,
) -> None:
    _require_memory_ready(capabilities)
    if not capabilities.permanent_delete_ready:
        raise HTTPException(
            503,
            {
                "code": "memory_permanent_delete_not_ready",
                "reason": capabilities.permanent_delete_reason,
            },
        )


@router.get("/overview", response_model=MemoryOverviewV2DTO)
async def memory_overview(
    request: Request,
    response: Response,
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> MemoryOverviewV2DTO:
    response.headers["Cache-Control"] = "no-store, private"
    uid = _user_id(request)
    capabilities = await _build_memory_capabilities(
        request,
        user_id=uid,
        user_mem=user_mem,
    )
    _require_memory_ready(capabilities)

    active_entries = await user_mem.list_product_for_user(uid)
    all_product_entries = await user_mem.list_product_for_user(
        uid,
        include_trashed=True,
    )
    trashed_entries = [entry for entry in all_product_entries if entry.deleted_at is not None]
    about_you_recoveries = await user_mem.list_about_you_recoveries(
        user_id=uid,
    )

    note = None
    note_version = 0
    note_locked = False
    notes_store = _notes_store(request)
    if notes_store is not None:
        note = await notes_store.get(user_id=uid)
        version_note = note
        if version_note is None:
            version_note = await notes_store.get(
                user_id=uid,
                include_trashed=True,
            )
        note_version = version_note.version if version_note else 0
        note_locked = await notes_store.is_lock_active(user_id=uid)

    candidate_store = _candidate_store(request)
    pending = (
        await candidate_store.list_for_user(
            user_id=uid,
            status="pending",
            limit=500,
        )
        if candidate_store is not None
        else []
    )
    accessible_spaces = await accessible_candidate_spaces(request, uid)
    # ★ Batch 1c: **raise the limit from 3 to the full pending length**.
    # Old pending[:3] previewed 3 items; users clicked View all N items to open another
    # 380px modal drawer with **different components** (new layout for the same suggestions, text instead of icon actions,
    # batch actions available only there), independently requesting listCandidates("pending", 50).
    # Two lists on one page could disagree. Now there is **one list**; FE decides how many to show initially,
    # expands in place, without switching UI or fetching again.
    # ⚠ Upstream list_for_user still has limit=500; this is not unbounded.
    memory_texts = await _reason_memory_texts(user_mem, uid, pending)
    suggestions = [
        candidate_to_dto(
            candidate,
            accessible_spaces=accessible_spaces,
            memory_texts=memory_texts,
        )
        for candidate in pending
    ]

    paused = False
    pause_cache = _pause_cache(request)
    if pause_cache is not None:
        paused = await pause_cache.is_paused(uid)

    return MemoryOverviewV2DTO(
        about_you=_about_you_to_dto(
            note,
            version=note_version,
            is_locked=note_locked,
            can_edit=capabilities.actions.edit,
        ),
        suggestions=suggestions,
        stats=MemoryOverviewStatsDTO(
            notes_configured=bool(note and note.content),
            saved_entries=len(active_entries),
            pending_candidates=len(pending),
            spaces_represented=len(
                {entry.workspace_id for entry in active_entries if entry.workspace_id},
            ),
            trash_count=(len(trashed_entries) + len(about_you_recoveries)),
            recoverable_resets=await user_mem.count_recent_reset_snapshots(
                user_id=uid,
            ),
        ),
        learning=MemoryLearningV2DTO(
            automatic_learning="paused" if paused else "active",
        ),
        capabilities=capabilities,
    )


class MemoryAboutYouEditSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: str = Field(pattern=r"^[0-9]+$")
    edit_token: str | None = Field(default=None, min_length=32, max_length=256)


class MemoryAboutYouPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(max_length=3000)
    version: str = Field(pattern=r"^[0-9]+$")
    edit_token: str = Field(min_length=32, max_length=256)


@router.post(
    "/about-you/edit-session",
    response_model=MemoryAboutYouEditSessionDTO,
)
async def begin_about_you_edit_session(
    body: MemoryAboutYouEditSessionRequest,
    request: Request,
    response: Response,
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> MemoryAboutYouEditSessionDTO:
    response.headers["Cache-Control"] = "no-store, private"
    uid = _user_id(request)
    capabilities = await _build_memory_capabilities(
        request,
        user_id=uid,
        user_mem=user_mem,
    )
    _require_memory_ready(capabilities)
    notes_store = _notes_store(request)
    assert notes_store is not None
    try:
        lease = await notes_store.begin_edit_session(
            user_id=uid,
            expected_version=int(body.expected_version),
            edit_token=body.edit_token,
            ttl_seconds=300,
        )
    except NotesLockedError as exc:
        raise HTTPException(
            423,
            {
                "code": "memory_about_you_editing",
                "lock_expires_at": exc.lock_expires_at,
            },
        ) from None
    except NotesVersionConflictError as exc:
        raise HTTPException(
            409,
            {
                "code": "memory_version_conflict",
                "retryable": True,
                "current_version": str(exc.current_version),
            },
        ) from None
    return MemoryAboutYouEditSessionDTO(
        edit_token=lease.edit_token,
        version=str(lease.version),
        expires_at=lease.expires_at,
    )


@router.delete("/about-you/edit-session", status_code=204)
async def release_about_you_edit_session(
    request: Request,
    response: Response,
    edit_token: str = Header(..., alias="X-Memory-Edit-Token"),
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> Response:
    response.status_code = 204
    response.headers["Cache-Control"] = "no-store, private"
    uid = _user_id(request)
    capabilities = await _build_memory_capabilities(
        request,
        user_id=uid,
        user_mem=user_mem,
    )
    _require_memory_ready(capabilities)
    notes_store = _notes_store(request)
    assert notes_store is not None
    await notes_store.release_lock(
        user_id=uid,
        edit_token=edit_token,
    )
    return response


@router.patch("/about-you", response_model=MemoryAboutYouV2DTO)
async def patch_about_you(
    body: MemoryAboutYouPatchRequest,
    request: Request,
    response: Response,
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> MemoryAboutYouV2DTO:
    response.headers["Cache-Control"] = "no-store, private"
    uid = _user_id(request)
    capabilities = await _build_memory_capabilities(
        request,
        user_id=uid,
        user_mem=user_mem,
    )
    _require_memory_ready(capabilities)
    notes_store = _notes_store(request)
    assert notes_store is not None
    try:
        note = await notes_store.commit_edit(
            user_id=uid,
            content=body.content,
            expected_version=int(body.version),
            edit_token=body.edit_token,
        )
    except NotesCharLimitError as exc:
        raise HTTPException(413, str(exc)) from None
    except NotesEditTokenError:
        raise HTTPException(
            423,
            {"code": "memory_about_you_edit_session_invalid"},
        ) from None
    except NotesVersionConflictError as exc:
        raise HTTPException(
            409,
            {
                "code": "memory_version_conflict",
                "retryable": True,
                "current_version": str(exc.current_version),
            },
        ) from None

    await _audit(
        request,
        user_id=uid,
        event_type="memory.about_you.edit",
        payload={"char_count": len(body.content)},
    )
    await _track(
        request,
        name="memory.about_you.edit",
        user_id=uid,
        attrs={"char_count": len(body.content)},
    )
    return _about_you_to_dto(
        note,
        version=note.version,
        is_locked=False,
        can_edit=True,
    )


@router.delete(
    "/about-you",
    response_model=MemoryAboutYouDeleteDTO,
)
async def delete_about_you(
    request: Request,
    response: Response,
    if_match: str = Header(..., alias="If-Match"),
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> MemoryAboutYouDeleteDTO:
    response.headers["Cache-Control"] = "no-store, private"
    uid = _user_id(request)
    capabilities = await _build_memory_capabilities(
        request,
        user_id=uid,
        user_mem=user_mem,
    )
    _require_memory_ready(capabilities)
    expected_version = _parse_if_match(if_match)
    assert expected_version is not None
    try:
        result = await user_mem.delete_about_you_v3(
            user_id=uid,
            expected_version=expected_version,
        )
    except MemoryAboutYouNotFoundError:
        raise HTTPException(404, "About you not found") from None
    except MemoryAboutYouVersionConflictError as exc:
        raise HTTPException(
            409,
            {
                "code": "memory_version_conflict",
                "retryable": True,
                "current_version": str(exc.current_version),
            },
        ) from None
    except MemoryResetBlockedError as exc:
        raise HTTPException(
            423,
            {
                "code": "memory_about_you_editing",
                "lock_expires_at": exc.lock_expires_at,
            },
        ) from None
    await _audit(
        request,
        user_id=uid,
        event_type="memory.about_you.delete",
        payload={"recovery_id": result["recovery_id"]},
    )
    return MemoryAboutYouDeleteDTO.model_validate(result)


def _about_you_to_dto(
    note: Any,
    *,
    version: int,
    is_locked: bool,
    can_edit: bool,
) -> MemoryAboutYouV2DTO:
    return MemoryAboutYouV2DTO(
        content=note.content if note else "",
        scope=_global_memory_scope(),
        version=str(version),
        updated_at=note.updated_at if note else None,
        is_locked=is_locked,
        capabilities=CanonicalMemoryItemCapabilitiesDTO(
            edit=can_edit and not is_locked,
            soft_delete=bool(note and note.content and not is_locked),
        ),
    )


@router.get("/items", response_model=CanonicalMemoryItemsPageDTO)
async def memory_items(
    request: Request,
    response: Response,
    state: str = Query("active", pattern="^(active|trashed)$"),
    query: str = Query("", max_length=200),
    # Source-line keys whose label, in the client's interface language, contains `query`. The client renders the
    # source line from `source_label_key` in its own language, so only it can tell which labels the user's search
    # words match. When present (even empty), it replaces matching on the Chinese `source_label`; older clients
    # omit it and keep the legacy Chinese-label match.
    source_label_keys: str | None = Query(None, max_length=512),
    display_type: str | None = Query(None, max_length=32),
    scope_type: str | None = Query(
        None,
        pattern="^(global|space)$",
    ),
    space_id: str | None = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> CanonicalMemoryItemsPageDTO:
    response.headers["Cache-Control"] = "no-store, private"
    uid = _user_id(request)
    capabilities = await _build_memory_capabilities(
        request,
        user_id=uid,
        user_mem=user_mem,
    )
    _require_memory_ready(capabilities)

    accessible_spaces = await accessible_candidate_spaces(request, uid)
    entries = await user_mem.list_product_for_user(
        uid,
        include_trashed=state == "trashed",
    )
    entries = [entry for entry in entries if (entry.deleted_at is not None) == (state == "trashed")]
    about_you_recoveries = await user_mem.list_about_you_recoveries(user_id=uid) if state == "trashed" else []

    after = _decode_memory_cursor(cursor) if cursor else None
    canonical: list[CanonicalMemoryItemDTO] = []
    normalized_query = query.strip().casefold()
    matched_label_keys = None if source_label_keys is None else {key.strip() for key in source_label_keys.split(",") if key.strip()}
    superseded_keys = await _superseded_key_map(user_mem, uid, entries)
    source_items = [
        _entry_to_canonical(
            entry,
            accessible_spaces=accessible_spaces,
            superseded_keys=superseded_keys,
        )
        for entry in entries
    ]
    source_items.extend(_about_you_recovery_to_canonical(recovery) for recovery in about_you_recoveries)
    if state == "trashed" and capabilities.actions.permanent_delete:
        for item in source_items:
            item.capabilities.permanent_delete = True
    source_items.sort(
        key=lambda item: (item.updated_at, item.id),
        reverse=True,
    )
    for item in source_items:
        if (
            after is not None
            and (
                item.updated_at,
                item.id,
            )
            >= after
        ):
            continue
        if display_type and item.display_type != display_type:
            continue
        if scope_type and item.scope.type != scope_type:
            continue
        if space_id and (item.scope.type != "space" or item.scope.space_id != space_id):
            continue
        if normalized_query and not _matches_memory_query(item, normalized_query, matched_label_keys):
            continue
        canonical.append(item)
        if len(canonical) > limit:
            break

    page_items = canonical[:limit]
    next_cursor = _encode_memory_cursor(page_items[-1]) if len(canonical) > limit and page_items else None
    return CanonicalMemoryItemsPageDTO(
        items=page_items,
        next_cursor=next_cursor,
    )


def _matches_memory_query(
    item: CanonicalMemoryItemDTO,
    normalized_query: str,
    matched_label_keys: set[str] | None,
) -> bool:
    """Match the search against what the user sees: content, Space name and the localized source line.

    `matched_label_keys` is None for older clients, which keep matching the Chinese `source_label`.
    """
    created_from = item.provenance.created_from
    text_fields = [item.display_value, item.scope.space_name]
    if matched_label_keys is None:
        text_fields.append(created_from.source_label)
    elif created_from.source_label_key in matched_label_keys:
        return True
    searchable = " ".join(field for field in text_fields if field).casefold()
    return normalized_query in searchable


class MemoryItemPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edit_schema: str = Field(alias="schema", min_length=1, max_length=64)
    patch: dict[str, Any]
    version: str = Field(pattern=r"^[0-9]+$")


@router.patch("/items/{entry_id}", response_model=CanonicalMemoryItemDTO)
async def patch_memory_item(
    entry_id: str,
    body: MemoryItemPatchRequest,
    request: Request,
    response: Response,
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> CanonicalMemoryItemDTO:
    response.headers["Cache-Control"] = "no-store, private"
    uid = _user_id(request)
    capabilities = await _build_memory_capabilities(
        request,
        user_id=uid,
        user_mem=user_mem,
    )
    _require_memory_ready(capabilities)
    value = _typed_patch_value(body.edit_schema, body.patch)
    accessible_spaces = await accessible_candidate_spaces(request, uid)

    try:
        updated = await user_mem.patch_product_value(
            user_id=uid,
            entry_id=entry_id,
            expected_version=int(body.version),
            edit_schema=body.edit_schema,
            value=value,
        )
    except MemoryEntryNotFoundError:
        raise HTTPException(404, "memory item not found") from None
    except MemoryEntryLockedError:
        raise HTTPException(
            423,
            {"code": "memory_item_locked"},
        ) from None
    except MemoryEntryStateError:
        raise HTTPException(
            409,
            {"code": "memory_item_not_editable"},
        ) from None
    except MemoryEntrySchemaError:
        raise HTTPException(
            422,
            {"code": "memory_edit_schema_mismatch"},
        ) from None
    except MemoryVersionConflictError as exc:
        current_item = _safe_canonical_item(
            exc.current,
            accessible_spaces=accessible_spaces,
        )
        raise HTTPException(
            409,
            {
                "code": "memory_version_conflict",
                "retryable": True,
                "current_version": str(exc.current.version),
                "current_item": (current_item.model_dump() if current_item is not None else None),
            },
        ) from None

    await _audit(
        request,
        user_id=uid,
        event_type="memory.entry.edit",
        payload={"id": entry_id, "display_schema": body.edit_schema},
    )
    await _track(
        request,
        name="memory.entry.edit",
        user_id=uid,
        attrs={"display_schema": body.edit_schema},
    )
    return _entry_to_canonical(
        updated,
        accessible_spaces=accessible_spaces,
    )


def _entry_to_canonical(
    entry: Any,
    *,
    accessible_spaces: dict[str, str],
    superseded_keys: dict[str, str] | None = None,
) -> CanonicalMemoryItemDTO:
    """`superseded_keys` 是 id→key 的**预解析**结果（`_superseded_key_map` 一次批量查出来）。

    ⚠ 故意做成参数而不是在这里现查：本函数在列表页会被逐条调用，就地查库等于 N+1。
    传 None 时 `superseded_by.key` 为空，前端走兜底文案 —— 这是**降级**不是错误，
    单条 PATCH 之类不值得为它多查一次的路径可以这么用。
    """
    value = entry.parsed_value()
    policy = resolve_memory_policy(
        memory_role=entry.memory_role,
        memory_policy_version=entry.memory_policy_version,
        kind=entry.kind,
        value_schema=entry.value_schema,
        value=value,
    )
    if not policy.user_visible or not policy.display_type:
        raise ValueError("entry has no user-visible Memory policy")
    return CanonicalMemoryItemDTO(
        id=entry.id,
        display_type=policy.display_type,
        display_value=_display_memory_value(value),
        display_schema=CanonicalMemoryDisplaySchemaDTO(
            name=policy.display_schema or "text_v1",
            payload=_display_schema_payload(
                policy.display_schema,
                value,
            ),
            editable_fields=_editable_fields(policy.edit_schema),
        ),
        scope=_canonical_entry_scope(
            workspace_id=entry.workspace_id,
            accessible_spaces=accessible_spaces,
        ),
        provenance=CanonicalMemoryProvenanceDTO(
            created_from=CanonicalMemoryCreatedFromDTO(
                type=_created_from_type(entry.created_source_type),
                source_label=_created_from_label(
                    entry.created_source_type,
                    entry.source,
                ),
                source_label_key=_created_from_label_key(entry.created_source_type, entry.source),
                source_available=False,
            ),
            last_modified=CanonicalMemoryLastModifiedDTO(
                actor_type=entry.last_modified_actor_type,
                actor_id=entry.last_modified_actor_id,
                modified_at=entry.last_modified_at,
            ),
        ),
        capabilities=CanonicalMemoryItemCapabilitiesDTO(
            edit=bool(policy.edit_schema and not entry.locked and entry.deleted_at is None),
            soft_delete=not entry.locked and entry.deleted_at is None,
            restore=not entry.locked and entry.deleted_at is not None,
            permanent_delete=False,
        ),
        locked=entry.locked,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        deleted_at=entry.deleted_at,
        purge_at=_purge_at(entry.deleted_at),
        version=str(entry.version),
        confidence=getattr(entry, "confidence", None),
        memory_type=getattr(entry, "memory_type", None),
        previous_value=_display_previous_value(getattr(entry, "previous_value", None)),
        previous_value_at=getattr(entry, "previous_value_at", None),
        superseded_by=_superseded_by_dto(
            getattr(entry, "superseded_by", None),
            superseded_keys,
        ),
    )


def _display_previous_value(raw: str | None) -> str | None:
    """`previous_value` 与 `value` 一样是 JSON 串，展示前要按同一条路解码。

    ⚠ 直接把原始串塞给前端的话，用户会在「上一版」里看到 `\"Top 10\"` 带引号 ——
    与同一张卡上的当前值渲染不一致。解不动就原样回退（老行可能存的是裸文本）。
    """
    if raw is None:
        return None
    try:
        return _display_memory_value(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return str(raw)


def _superseded_by_dto(
    entry_id: str | None,
    superseded_keys: dict[str, str] | None,
) -> CanonicalMemorySupersededByDTO | None:
    if not entry_id:
        return None
    return CanonicalMemorySupersededByDTO(
        id=entry_id,
        key=(superseded_keys or {}).get(entry_id),
    )


async def _superseded_key_map(
    user_mem: Any,
    user_id: str,
    entries: Sequence[Any],
) -> dict[str, str]:
    """一页条目里所有 `superseded_by` 的 id，一次查完（避免逐条 N+1）。"""
    ids = {sid for e in entries if (sid := getattr(e, "superseded_by", None))}
    if not ids:
        return {}
    resolve = getattr(user_mem, "resolve_entry_keys", None)
    if resolve is None:  # Older stores (test doubles) lack this method: degrade without breaking the list page.
        return {}
    return await resolve(user_id, sorted(ids))


def _about_you_recovery_to_canonical(
    recovery: dict[str, Any],
) -> CanonicalMemoryItemDTO:
    payload = recovery["payload"]
    return CanonicalMemoryItemDTO(
        id=recovery["recovery_id"],
        resource_type="memory_note_recovery",
        display_type="background",
        display_value=payload["content"],
        display_schema=CanonicalMemoryDisplaySchemaDTO(
            name="text_v1",
            payload={"content": payload["content"]},
            editable_fields=[],
        ),
        scope=_global_memory_scope(),
        provenance=CanonicalMemoryProvenanceDTO(
            created_from=CanonicalMemoryCreatedFromDTO(
                type=_created_from_type(payload.get("created_source_type")),
                source_label=_created_from_label(
                    payload.get("created_source_type"),
                    payload.get("source") or "legacy_unknown",
                ),
                source_label_key=_created_from_label_key(
                    payload.get("created_source_type"),
                    payload.get("source") or "legacy_unknown",
                ),
                source_available=False,
            ),
            last_modified=CanonicalMemoryLastModifiedDTO(
                actor_type=payload.get("last_modified_actor_type"),
                actor_id=payload.get("last_modified_actor_id"),
                modified_at=payload.get("last_modified_at"),
            ),
        ),
        capabilities=CanonicalMemoryItemCapabilitiesDTO(restore=True),
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
        deleted_at=recovery["created_at"],
        purge_at=recovery["expires_at"],
        version=recovery["deleted_version"],
    )


def _global_memory_scope() -> CanonicalMemoryScopeDTO:
    return CanonicalMemoryScopeDTO(
        type="global",
        label="仅供你使用 · 所有空间",
    )


def _canonical_entry_scope(
    *,
    workspace_id: str | None,
    accessible_spaces: dict[str, str],
) -> CanonicalMemoryScopeDTO:
    if workspace_id is None:
        return _global_memory_scope()
    name = accessible_spaces.get(workspace_id)
    if name:
        return CanonicalMemoryScopeDTO(
            type="space",
            space_id=workspace_id,
            space_name=name,
            availability="available",
            label=f"仅供你使用 · {name}",
        )
    return CanonicalMemoryScopeDTO(
        type="space",
        availability="unavailable",
        label="仅供你使用 · 不可用的空间",
    )


def _display_memory_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _display_schema_payload(
    schema: str | None,
    value: Any,
) -> dict[str, Any]:
    if schema in {"text_v1", "note_text_v1"}:
        return {"content": value}
    return {"value": value}


def _editable_fields(edit_schema: str | None) -> list[str]:
    if edit_schema == "text_v1":
        return ["content"]
    if edit_schema in {"boolean_v1", "number_v1"}:
        return ["value"]
    return []


def _typed_patch_value(schema: str, patch: dict[str, Any]) -> object:
    if schema == "text_v1":
        if set(patch) != {"content"} or not isinstance(patch["content"], str):
            raise HTTPException(
                422,
                {
                    "code": "memory_edit_payload_invalid",
                    "expected": {"content": "string"},
                },
            )
        if len(patch["content"].encode("utf-8")) > _MAX_VALUE_BYTES:
            raise HTTPException(413, "memory content exceeds 16 KB")
        return patch["content"]
    if schema == "boolean_v1":
        if set(patch) != {"value"} or not isinstance(patch["value"], bool):
            raise HTTPException(
                422,
                {
                    "code": "memory_edit_payload_invalid",
                    "expected": {"value": "boolean"},
                },
            )
        return patch["value"]
    if schema == "number_v1":
        value = patch.get("value")
        if set(patch) != {"value"} or not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise HTTPException(
                422,
                {
                    "code": "memory_edit_payload_invalid",
                    "expected": {"value": "number"},
                },
            )
        return value
    raise HTTPException(
        422,
        {"code": "memory_edit_schema_unsupported"},
    )


def _safe_canonical_item(
    entry: Any,
    *,
    accessible_spaces: dict[str, str],
    superseded_keys: dict[str, str] | None = None,
) -> CanonicalMemoryItemDTO | None:
    try:
        return _entry_to_canonical(
            entry,
            accessible_spaces=accessible_spaces,
            superseded_keys=superseded_keys,
        )
    except ValueError:
        return None


def _safe_export_item(
    entry: Any,
    *,
    accessible_spaces: dict[str, str],
    superseded_keys: dict[str, str] | None = None,
) -> CanonicalMemoryItemDTO | None:
    try:
        value = entry.parsed_value()
        policy = resolve_memory_policy(
            memory_role=entry.memory_role,
            memory_policy_version=entry.memory_policy_version,
            kind=entry.kind,
            value_schema=entry.value_schema,
            value=value,
        )
        if not policy.memory_export:
            return None
        return _entry_to_canonical(
            entry,
            accessible_spaces=accessible_spaces,
            superseded_keys=superseded_keys,
        )
    except ValueError:
        return None


def _parse_if_match(value: str | None) -> int | None:
    if value is None:
        return None
    token = value.strip()
    if token.startswith('"') and token.endswith('"') and len(token) >= 2:
        token = token[1:-1]
    if not token.isdecimal():
        raise HTTPException(
            400,
            {"code": "memory_version_invalid"},
        )
    return int(token)


from bowhead_core.memory_auto_capture import CAPTURE_SOURCE

#: Source identifier for zero-click direct writes (memory_auto_capture.CAPTURE_SOURCE).
#: ⚠ Import it rather than duplicating the literal: this sprint has repeatedly fixed independent implementations
#: of the same concept; a rename must propagate here.
#: Internal source identifier → **public** type. ★ This is a **closed mapping**: unregistered sources fall back to
#: legacy_unknown; never expose internal identifiers unchanged through the API.
#: Previously the final line was if created_source_type: return created_source_type,
#: exposing user_directive (the remember tool's source) directly as a public type.
#:
#: ⚠ Align this table with **actual production values**, not assumed names:
#: the read side compared user_explicit, which production never emits (116 measurements:
#: 8 user_directive rows, 0 user_explicit rows): a criterion guarded a nonexistent production configuration.
_PUBLIC_SOURCE_TYPE = {
    "candidate": "candidate_approved",
    "user_explicit": "explicit_instruction",
    "reset_undo": "explicit_instruction",
    # memory_tools._REMEMBER_SOURCE: user explicitly requests remembering something in chat.
    "user_directive": "explicit_instruction",
    "legacy_notes_migration": "legacy_notes_migration",
    "import": "import",
    CAPTURE_SOURCE: "auto_captured",
    # Operational rows (tool_context / datarc_pref), currently excluded from user-visible lists.
    # Register them so **internal identifiers do not leak even if included**.
    "tool_runtime": "operational",
    "bi_runtime": "operational",
    "cron_aggregator": "operational",
}


def _created_from_type(created_source_type: str | None) -> str:
    if created_source_type == "candidate":
        return "candidate_approved"
    if created_source_type in {"user_explicit", "reset_undo"}:
        return "explicit_instruction"
    # Track B batch 1a zero-click direct writes. ★ Separate from candidate_approved: users **clicked** for that type,
    # but never clicked for this one; conflating them on reads directs undo guidance to the wrong place.
    if created_source_type == CAPTURE_SOURCE:
        return "auto_captured"
    mapped = _PUBLIC_SOURCE_TYPE.get(created_source_type or "")
    if mapped:
        return mapped
    # ★ Never return created_source_type directly: that leaks internal identifiers through the API.
    return "legacy_unknown"


def _created_from_label_key(
    created_source_type: str | None,
    source: str,
) -> str:
    # ⚠ **Evaluation order determines correctness**: created_source_type is explicitly registered provenance;
    # branches below infer it from source. Check explicit provenance first, or inferred branches intercept it.
    # Verified: migrated entries with source='user_explicit' were classified below as
    # '你在对话中告诉 Bowhead', though actually migrated from '关于你'.
    # The design also separates responsibilities: source determines confidence; created_source_type is provenance.
    if created_source_type == "legacy_notes_migration":
        # Copy comes from memory-item-detail-design.md's source_legacy.
        # ⚠ Writes were fixed on 2026-09-03, but reads lagged; after migration on 116,
        # all 5 actual entries showed '最初来源不可用', exposing the problem.
        return "legacy_migration"
    # Zero-click direct writes: **users never confirmed them**, so copy cannot claim user confirmation.
    # Copy also omits undo: that is a detail-page button; the source line only explains where this entry came from.
    # ⓘ Disjoint from the candidate branch below (automatically captured entries' source/type are
    # feedback_confirmed, neither containing dreaming nor equal to candidate); ordering here improves readability,
    # not correctness — moving it later was verified to preserve behavior. Do not extrapolate the migration branch's
    # evaluation-order requirement: that branch really can be intercepted by inferred-source branches.
    if created_source_type == CAPTURE_SOURCE or source == CAPTURE_SOURCE:
        return "auto_captured"
    if created_source_type == "candidate" or "dreaming" in source:
        return "confirmed_suggestion"
    # ⚠ user_directive is the remember tool's source (memory_tools._REMEMBER_SOURCE),
    # **the main production category**; previously this compared only user_explicit, a value production never emits.
    # 116 measurements (2026-09-10): 3 of 10 user-visible entries fell back to default copy, all this type.
    # Another recorded case of criteria guarding a nonexistent production configuration.
    if created_source_type in {"user_explicit", "user_directive"} or source in {"user_explicit", "user_directive"}:
        return "told_in_chat"
    # ⚠ Recognize operational rows in **both columns**: the most common measured shape on 116 is
    # created_source_type=NULL, source='tool_runtime' (8 rows);
    # comparing only created_source_type still sends it to fallback.
    # The first dataset was manually copied from a partial query, missing that NULL combination (found during dogfood).
    _OPERATIONAL = {"tool_runtime", "bi_runtime", "cron_aggregator"}
    # ws-autosync / ws-sync-datarc-<date>: workspace rows synchronized from the data source.
    # ★ Match prefixes because of date variants (both exist on 116). These are all system-written;
    # system-maintained is accurate without overclaiming who synchronized them or when.
    if source.startswith(("system", "ws-")) or created_source_type in _OPERATIONAL or source in _OPERATIONAL:
        return "system_maintained"
    return "unavailable"


# Chinese source-line copy kept for `source_label` (older clients' display and search). Current clients render the
# localized copy from `source_label_key` and search it through `source_label_keys`, so other interface languages
# never show or match this Chinese text.
_SOURCE_LABEL_ZH = {
    "legacy_migration": "迁移自旧版记忆",
    "auto_captured": "根据你在对话中的要求自动记下",
    "confirmed_suggestion": "你确认过的一条建议",
    "told_in_chat": "你在对话中告诉 Bowhead",
    "system_maintained": "由系统维护",
    "unavailable": "最初来源不可用",
}


def _created_from_label(
    created_source_type: str | None,
    source: str,
) -> str:
    return _SOURCE_LABEL_ZH[_created_from_label_key(created_source_type, source)]


def _purge_at(deleted_at: str | None) -> str | None:
    if deleted_at is None:
        return None
    try:
        parsed = datetime.fromisoformat(deleted_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (parsed + timedelta(days=30)).isoformat(timespec="seconds")


def _encode_memory_cursor(item: CanonicalMemoryItemDTO) -> str:
    raw = json.dumps(
        {
            "updated_at": item.updated_at,
            "id": item.id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_memory_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * ((4 - len(cursor) % 4) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii")),
        )
        updated_at = payload["updated_at"]
        item_id = payload["id"]
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        base64.binascii.Error,
    ) as exc:
        raise HTTPException(400, "invalid memory cursor") from exc
    if not isinstance(updated_at, str) or not isinstance(item_id, str):
        raise HTTPException(400, "invalid memory cursor")
    return updated_at, item_id


@router.get("/snapshot", response_model=MemorySnapshotDTO)
async def snapshot(
    request: Request,
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> MemorySnapshotDTO:
    """Unified DTO for the user memory page.

    Aggregates active memory entries, statistics and dreaming pause state.
    """
    uid = _user_id(request)
    items: list[UnifiedMemoryItemDTO] = []

    # memory_entries (active)
    mem_entries = await user_mem.list_for_user(uid)
    items.extend(_memory_to_unified(e) for e in mem_entries)

    # stats
    trash_mem = await user_mem.count_trashed(uid)
    pending_n = 0
    candidate_store = _candidate_store(request)
    if candidate_store is not None:
        pending = await candidate_store.list_for_user(
            user_id=uid,
            status="pending",
            limit=500,
        )
        pending_n = len(pending)

    stats = MemoryStatsDTO(
        active_count=len(items),
        trash_count=trash_mem,
        pending_candidates=pending_n,
    )

    # dreaming pause state
    paused = False
    cache = _pause_cache(request)
    if cache is not None:
        paused = await cache.is_paused(uid)

    # V3.5 PRD-C: notes section
    notes_dto: MemoryNoteDTO | None = None
    notes_store = _notes_store(request)
    if notes_store is not None:
        try:
            note = await notes_store.get(user_id=uid)
            version_note = note
            if version_note is None:
                version_note = await notes_store.get(
                    user_id=uid,
                    include_trashed=True,
                )
            is_locked = await notes_store.is_lock_active(user_id=uid)
            lock_expires = await notes_store.get_lock_expiry(user_id=uid) if is_locked else None
            counts = parse_notes_lines(note.content if note else "")
            notes_dto = MemoryNoteDTO(
                user_id=uid,
                version=str(version_note.version if version_note else 0),
                content=note.content if note else "",
                char_count=counts["char_count"],
                pinned_count=counts["pinned_count"],
                bullet_count=counts["bullet_count"],
                free_text_lines=counts["free_text_lines"],
                last_edited_at=note.updated_at if note else None,
                last_edited_source=note.source if note else "",
                is_locked=is_locked,
                lock_expires_at=lock_expires,
            )
        except Exception as e:
            logger.warning("memory.snapshot.notes_load_failed user=%s err=%s", uid, e)

    return MemorySnapshotDTO(
        user_id=uid,
        items=items,
        stats=stats,
        dreaming=DreamingStateDTO(paused=paused),
        notes=notes_dto,
    )


@router.get("/trash", response_model=TrashListDTO)
async def trash(
    request: Request,
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> TrashListDTO:
    uid = _user_id(request)
    items: list[UnifiedMemoryItemDTO] = []

    for e in await user_mem.list_trashed(uid):
        items.append(_memory_to_unified(e))

    return TrashListDTO(user_id=uid, items=items)


@router.post("/{entry_id}/restore", response_model=RestoreResultDTO)
async def restore(
    entry_id: str,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> RestoreResultDTO:
    uid = _user_id(request)
    ok = False
    origin = "memory"
    if entry_id.startswith("snap_"):
        accessible_spaces = await accessible_candidate_spaces(request, uid)
        try:
            recovery = await user_mem.restore_reset_v3(
                snapshot_id=entry_id,
                user_id=uid,
                accessible_space_ids=set(accessible_spaces),
            )
        except MemoryResetSnapshotNotFoundError:
            raise HTTPException(404, "recovery item not found") from None
        if recovery.status != "restored":
            raise HTTPException(
                409,
                {
                    "code": "memory_restore_conflict",
                    "conflicts": recovery.conflicts,
                    "remaining_recoverable": (recovery.remaining_recoverable),
                },
            )
        ok = True
        origin = "about_you_recovery"
    if ok:
        pass
    else:
        try:
            ok = await user_mem.restore(
                user_id=uid,
                entry_id=entry_id,
                expected_version=_parse_if_match(if_match),
            )
        except MemoryVersionConflictError as exc:
            raise HTTPException(
                409,
                {
                    "code": "memory_version_conflict",
                    "retryable": True,
                    "current_version": str(exc.current.version),
                },
            ) from None

    if not ok:
        raise HTTPException(404, "entry not found in trash or already restored")
    await _audit(
        request,
        user_id=uid,
        event_type="memory.entry.restore",
        payload={"id": entry_id, "origin": origin},
    )
    await _track(
        request,
        name="memory.entry.restore",
        user_id=uid,
        attrs={"origin": origin},
    )
    return RestoreResultDTO(id=entry_id, restored=True)


@router.delete("/{entry_id}", response_model=DeletedDTO)
async def unified_soft_delete(
    entry_id: str,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> DeletedDTO:
    """Soft-delete a personal memory entry. Locked entries return 404."""
    uid = _user_id(request)
    try:
        ok = await user_mem.soft_delete(
            user_id=uid,
            entry_id=entry_id,
            expected_version=_parse_if_match(if_match),
        )
    except MemoryVersionConflictError as exc:
        raise HTTPException(
            409,
            {
                "code": "memory_version_conflict",
                "retryable": True,
                "current_version": str(exc.current.version),
            },
        ) from None
    origin = "memory"

    if not ok:
        raise HTTPException(404, "entry not found or locked")
    await _audit(
        request,
        user_id=uid,
        event_type="memory.entry.delete",
        payload={"id": entry_id, "origin": origin},
    )
    await _track(
        request,
        name="memory.entry.delete",
        user_id=uid,
        attrs={"origin": origin},
    )
    return DeletedDTO(deleted=entry_id)


@router.get("/reset-preview", response_model=MemoryResetPreviewDTO)
async def reset_memory_preview(
    request: Request,
    response: Response,
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> MemoryResetPreviewDTO:
    response.headers["Cache-Control"] = "no-store, private"
    uid = _user_id(request)
    capabilities = await _build_memory_capabilities(
        request,
        user_id=uid,
        user_mem=user_mem,
    )
    _require_memory_ready(capabilities)
    return MemoryResetPreviewDTO(
        counts=await user_mem.preview_reset_v3(user_id=uid),
    )


@router.get("/resets", response_model=MemoryResetListDTO)
async def list_memory_resets(
    request: Request,
    response: Response,
    limit: int = Query(20, ge=1, le=50),
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> MemoryResetListDTO:
    response.headers["Cache-Control"] = "no-store, private"
    uid = _user_id(request)
    capabilities = await _build_memory_capabilities(
        request,
        user_id=uid,
        user_mem=user_mem,
    )
    _require_memory_ready(capabilities)
    return MemoryResetListDTO(
        items=await user_mem.list_reset_v3_summaries(
            user_id=uid,
            limit=limit,
        ),
    )


@router.get(
    "/resets/{snapshot_id}",
    response_model=MemoryResetDetailDTO,
)
async def get_memory_reset(
    snapshot_id: str,
    request: Request,
    response: Response,
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> MemoryResetDetailDTO:
    response.headers["Cache-Control"] = "no-store, private"
    uid = _user_id(request)
    capabilities = await _build_memory_capabilities(
        request,
        user_id=uid,
        user_mem=user_mem,
    )
    _require_memory_ready(capabilities)
    detail = await user_mem.get_reset_v3_detail(
        snapshot_id=snapshot_id,
        user_id=uid,
    )
    if detail is None:
        raise HTTPException(404, "reset snapshot not found")
    return MemoryResetDetailDTO.model_validate(detail)


@router.post(
    "/resets/{snapshot_id}/restore",
    response_model=UndoResetResultDTO,
)
async def restore_memory_reset(
    snapshot_id: str,
    request: Request,
    response: Response,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
    ),
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> UndoResetResultDTO:
    response.headers["Cache-Control"] = "no-store, private"
    uid = _user_id(request)
    capabilities = await _build_memory_capabilities(
        request,
        user_id=uid,
        user_mem=user_mem,
    )
    _require_memory_ready(capabilities)
    accessible_spaces = await accessible_candidate_spaces(request, uid)
    try:
        result = await user_mem.restore_reset_v3(
            snapshot_id=snapshot_id,
            user_id=uid,
            accessible_space_ids=set(accessible_spaces),
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(
            400,
            {
                "code": "memory_idempotency_key_invalid",
                "message": str(exc),
            },
        ) from None
    except MemoryIdempotencyConflictError:
        raise HTTPException(
            409,
            {"code": "memory_idempotency_key_reused"},
        ) from None
    except MemoryResetSnapshotNotFoundError:
        raise HTTPException(404, "reset snapshot not found") from None
    await _audit(
        request,
        user_id=uid,
        event_type="memory.undo_reset",
        payload={
            "snapshot_id": snapshot_id,
            "status": result.status,
            "restored_counts": result.restored_counts,
            "remaining_recoverable": result.remaining_recoverable,
        },
    )
    return UndoResetResultDTO(
        snapshot_id=snapshot_id,
        restored_memory=result.restored_counts["entries"],
        restored_notes=result.restored_counts["notes"],
        status=result.status,
        restored_counts=result.restored_counts,
        conflicts=result.conflicts,
        unavailable_counts=result.unavailable_counts,
        remaining_recoverable=result.remaining_recoverable,
    )


@router.post("/reset", response_model=ResetMemoryResultDTO)
async def reset_memory(
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(
        None,
        alias="Idempotency-Key",
    ),
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> ResetMemoryResultDTO:
    """Soft-delete all unlocked memory entries for user.
    Saves a snapshot first for 30-day undo window.
    """
    uid = _user_id(request)
    response.headers["Cache-Control"] = "no-store, private"
    capabilities = await _build_memory_capabilities(
        request,
        user_id=uid,
        user_mem=user_mem,
    )
    if capabilities.v2_ready:
        return await _reset_memory_v3(
            request,
            user_mem=user_mem,
            user_id=uid,
            idempotency_key=idempotency_key,
        )
    return await _reset_memory_legacy(
        request,
        user_mem=user_mem,
        user_id=uid,
    )


async def _reset_memory_v3(
    request: Request,
    *,
    user_mem: UserMemoryStore,
    user_id: str,
    idempotency_key: str | None,
) -> ResetMemoryResultDTO:
    if idempotency_key is None:
        raise HTTPException(
            400,
            {"code": "memory_idempotency_key_required"},
        )
    try:
        result = await user_mem.reset_v3(
            user_id=user_id,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(
            400,
            {
                "code": "memory_idempotency_key_invalid",
                "message": str(exc),
            },
        ) from None
    except MemoryIdempotencyConflictError:
        raise HTTPException(
            409,
            {"code": "memory_idempotency_key_reused"},
        ) from None
    except MemoryResetBlockedError as exc:
        raise HTTPException(
            423,
            {
                "code": "memory_about_you_editing",
                "lock_expires_at": exc.lock_expires_at,
            },
        ) from None
    await _audit(
        request,
        user_id=user_id,
        event_type="memory.reset",
        payload={
            "snapshot_id": result.snapshot_id,
            "counts": result.counts,
        },
    )
    await _track(
        request,
        name="memory.reset",
        user_id=user_id,
        attrs=result.counts,
    )
    return ResetMemoryResultDTO(
        snapshot_id=result.snapshot_id,
        affected_memory=result.counts["entries"],
        affected_notes=result.counts["notes"],
        counts=result.counts,
        expires_at=result.expires_at,
        undo_available=any(
            result.counts[key] > 0
            for key in (
                "entries",
                "notes",
                "anchors",
            )
        ),
    )


async def _reset_memory_legacy(
    request: Request,
    *,
    user_mem: UserMemoryStore,
    user_id: str,
) -> ResetMemoryResultDTO:
    notes_store = _notes_store(request)

    snap_payload: dict[str, Any] = {
        "user_id": user_id,
        "_schema_version": 2,
        "memory_entries": [
            {
                "id": entry.id,
                "kind": entry.kind,
                "key": entry.key,
                "value": entry.parsed_value(),
                "source": entry.source,
                "locked": entry.locked,
                "lock_reason": entry.lock_reason,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
            }
            for entry in await user_mem.list_for_user(user_id)
        ],
        "notes": None,
    }
    if notes_store is not None:
        note = await notes_store.get(user_id=user_id)
        if note and note.content:
            snap_payload["notes"] = {
                "content": note.content,
                "source": note.source,
                "source_turn_id": note.source_turn_id,
                "created_at": note.created_at,
                "updated_at": note.updated_at,
            }
    snapshot_id = await user_mem.save_reset_snapshot(
        user_id=user_id,
        snapshot=snap_payload,
    )

    affected_memory = await user_mem.soft_delete_all_for_user(user_id)
    affected_notes = 0
    if notes_store is not None and await notes_store.soft_delete(user_id=user_id):
        affected_notes = 1
    payload = {
        "snapshot_id": snapshot_id,
        "affected_memory": affected_memory,
        "affected_notes": affected_notes,
    }
    await _audit(
        request,
        user_id=user_id,
        event_type="memory.reset",
        payload=payload,
    )
    await _track(
        request,
        name="memory.reset",
        user_id=user_id,
        attrs=payload,
    )
    return ResetMemoryResultDTO(
        snapshot_id=snapshot_id,
        affected_memory=affected_memory,
        affected_notes=affected_notes,
    )


class UndoResetRequest(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=64)


@router.post("/undo-reset", response_model=UndoResetResultDTO)
async def undo_reset(
    body: UndoResetRequest,
    request: Request,
    response: Response,
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> UndoResetResultDTO:
    """V3 PRD-B: restore from a /memory/reset snapshot (within 30-day window).

    Restores memory entries by clearing their deletion state.
    Snapshot row is deleted on success to make undo non-replayable.
    """
    uid = _user_id(request)
    response.headers["Cache-Control"] = "no-store, private"
    accessible_spaces = await accessible_candidate_spaces(request, uid)
    try:
        result = await user_mem.restore_reset_v3(
            snapshot_id=body.snapshot_id,
            user_id=uid,
            accessible_space_ids=set(accessible_spaces),
        )
    except MemoryResetSnapshotNotFoundError:
        result = None
    if result is not None:
        await _audit(
            request,
            user_id=uid,
            event_type="memory.undo_reset",
            payload={
                "snapshot_id": body.snapshot_id,
                "status": result.status,
                "restored_counts": result.restored_counts,
                "remaining_recoverable": result.remaining_recoverable,
            },
        )
        await _track(
            request,
            name="memory.undo_reset",
            user_id=uid,
            attrs={
                "status": result.status,
                "remaining_recoverable": result.remaining_recoverable,
            },
        )
        return UndoResetResultDTO(
            snapshot_id=body.snapshot_id,
            restored_memory=result.restored_counts["entries"],
            restored_notes=result.restored_counts["notes"],
            status=result.status,
            restored_counts=result.restored_counts,
            conflicts=result.conflicts,
            unavailable_counts=result.unavailable_counts,
            remaining_recoverable=result.remaining_recoverable,
        )

    snapshot = await user_mem.get_reset_snapshot(
        snapshot_id=body.snapshot_id,
        user_id=uid,
    )
    if snapshot is None:
        raise HTTPException(404, "snapshot not found or expired")

    # Restore memory entries: re-upsert each (entries already in DB will be
    # updated; deleted_at will be cleared via ON CONFLICT DO UPDATE).
    restored_mem = 0
    for e in snapshot.get("memory_entries", []):
        try:
            await user_mem.upsert(
                user_id=uid,
                kind=e["kind"],
                key=e["key"],
                value=e["value"],
                source=e.get("source") or "reset_undo",
                locked=bool(e.get("locked", False)),
                lock_reason=e.get("lock_reason"),
            )
            restored_mem += 1
        except Exception as exc:
            logger.warning("undo_reset.memory_entry_failed key=%s err=%s", e.get("key"), exc)

    # V3.5 PRD-C: restore notes from snapshot if schema_version >= 2
    restored_notes = 0
    if snapshot.get("_schema_version", 1) >= 2 and snapshot.get("notes"):
        notes_store = _notes_store(request)
        if notes_store is not None:
            try:
                ns = snapshot["notes"]
                await notes_store.upsert_content(
                    user_id=uid,
                    content=ns.get("content", ""),
                    source=ns.get("source") or "reset_undo",
                    source_turn_id=ns.get("source_turn_id"),
                )
                restored_notes = 1
            except Exception as exc:
                logger.warning("undo_reset.notes_failed err=%s", exc)

    # Delete snapshot so undo is single-use
    await user_mem.delete_reset_snapshot(snapshot_id=body.snapshot_id, user_id=uid)

    await _audit(
        request,
        user_id=uid,
        event_type="memory.undo_reset",
        payload={
            "snapshot_id": body.snapshot_id,
            "restored_memory": restored_mem,
            "restored_notes": restored_notes,
        },
    )
    await _track(
        request,
        name="memory.undo_reset",
        user_id=uid,
        attrs={
            "snapshot_id": body.snapshot_id,
            "restored_memory": restored_mem,
            "restored_notes": restored_notes,
        },
    )
    return UndoResetResultDTO(
        snapshot_id=body.snapshot_id,
        restored_memory=restored_mem,
        restored_notes=restored_notes,
    )


@router.post(
    "/items/{item_id}/permanent-delete",
    response_model=MemoryDestructiveOperationDTO,
)
async def permanent_delete_memory_item(
    item_id: str,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> MemoryDestructiveOperationDTO:
    return await _run_permanent_delete(
        request=request,
        response=response,
        user_mem=user_mem,
        idempotency_key=idempotency_key,
        action="permanent_delete_item",
        target_id=item_id,
    )


@router.post(
    "/trash/permanent-delete",
    response_model=MemoryDestructiveOperationDTO,
)
async def permanent_delete_memory_trash(
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> MemoryDestructiveOperationDTO:
    return await _run_permanent_delete(
        request=request,
        response=response,
        user_mem=user_mem,
        idempotency_key=idempotency_key,
        action="permanent_delete_trash",
        target_id=None,
    )


@router.post(
    "/resets/{snapshot_id}/permanent-delete",
    response_model=MemoryDestructiveOperationDTO,
)
async def permanent_delete_memory_reset(
    snapshot_id: str,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> MemoryDestructiveOperationDTO:
    return await _run_permanent_delete(
        request=request,
        response=response,
        user_mem=user_mem,
        idempotency_key=idempotency_key,
        action="permanent_delete_reset",
        target_id=snapshot_id,
    )


@router.post(
    "/permanent-delete-all",
    response_model=MemoryDestructiveOperationDTO,
)
async def permanent_delete_all_memory(
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> MemoryDestructiveOperationDTO:
    return await _run_permanent_delete(
        request=request,
        response=response,
        user_mem=user_mem,
        idempotency_key=idempotency_key,
        action="permanent_delete_all",
        target_id=None,
    )


async def _run_permanent_delete(
    *,
    request: Request,
    response: Response,
    user_mem: UserMemoryStore,
    idempotency_key: str,
    action: str,
    target_id: str | None,
) -> MemoryDestructiveOperationDTO:
    response.headers["Cache-Control"] = "no-store, private"
    uid = _user_id(request)
    capabilities = await _build_memory_capabilities(
        request,
        user_id=uid,
        user_mem=user_mem,
    )
    _require_permanent_delete(capabilities)
    store = _destructive_operation_store(request)
    assert store is not None
    try:
        if action == "permanent_delete_all":
            operation = await store.permanent_delete_all(
                user_id=uid,
                idempotency_key=idempotency_key,
            )
        elif action == "permanent_delete_item":
            assert target_id is not None
            operation = await store.permanent_delete_item(
                user_id=uid,
                item_id=target_id,
                idempotency_key=idempotency_key,
            )
        elif action == "permanent_delete_trash":
            operation = await store.permanent_delete_trash(
                user_id=uid,
                idempotency_key=idempotency_key,
            )
        elif action == "permanent_delete_reset":
            assert target_id is not None
            operation = await store.permanent_delete_reset(
                user_id=uid,
                snapshot_id=target_id,
                idempotency_key=idempotency_key,
            )
        else:
            raise RuntimeError("unsupported permanent delete action")
    except ValueError as exc:
        raise HTTPException(
            400,
            {
                "code": "memory_idempotency_key_invalid",
                "message": str(exc),
            },
        ) from None
    except MemoryIdempotencyConflictError:
        raise HTTPException(
            409,
            {"code": "memory_idempotency_key_reused"},
        ) from None
    except MemoryDestructiveOperationConflictError as exc:
        raise HTTPException(
            409,
            {
                "code": "memory_destructive_operation_active",
                "operation_id": exc.operation_id,
            },
        ) from None
    except MemoryDestructiveTargetNotFoundError:
        raise HTTPException(
            404,
            {"code": "memory_destructive_target_not_found"},
        ) from None
    except PrivacyDeletionNotReadyError as exc:
        raise HTTPException(
            503,
            {
                "code": "memory_permanent_delete_not_ready",
                "reason": str(exc),
            },
        ) from None
    pause_cache = _pause_cache(request)
    if pause_cache is not None:
        pause_cache.invalidate(uid)
    await _audit(
        request,
        user_id=uid,
        event_type=f"memory.{action}",
        payload={
            "operation_id": operation["operation_id"],
            "result": operation["status"],
        },
    )
    await _track(
        request,
        name="memory.permanent_delete.completed",
        user_id=uid,
        attrs={
            "action": action,
            "outcome": operation["status"],
        },
    )
    return MemoryDestructiveOperationDTO.model_validate(operation)


@router.get(
    "/operations/{operation_id}",
    response_model=MemoryDestructiveOperationDTO,
)
async def get_memory_destructive_operation(
    operation_id: str,
    request: Request,
    response: Response,
) -> MemoryDestructiveOperationDTO:
    response.headers["Cache-Control"] = "no-store, private"
    store = _destructive_operation_store(request)
    if store is None:
        raise HTTPException(404, "memory operation not found")
    operation = await store.get_operation(
        user_id=_user_id(request),
        operation_id=operation_id,
    )
    if operation is None:
        raise HTTPException(404, "memory operation not found")
    return MemoryDestructiveOperationDTO.model_validate(operation)


@router.get("/export")
async def export_memory(
    request: Request,
    user_mem: UserMemoryStore = Depends(_user_memory_store),
) -> StreamingResponse:
    """Stream a canonical, product-only export without operational rows."""

    uid = _user_id(request)
    notes_store = _notes_store(request)
    accessible_spaces = await accessible_candidate_spaces(request, uid)

    async def _stream_payload():
        reset_count = await user_mem.count_recent_reset_snapshots(
            user_id=uid,
        )
        about_you_recoveries = await user_mem.list_about_you_recoveries(
            user_id=uid,
        )
        yield "{\n"
        yield f'  "user_id": {json.dumps(uid)},\n'
        yield '  "_schema_version": 3,\n'
        exported_at = datetime.now(tz=UTC).isoformat(timespec="seconds")
        yield f'  "exported_at": {json.dumps(exported_at)},\n'
        yield '  "items": [\n'
        first = True
        async for e in user_mem.iter_for_user(uid, include_trashed=True):
            # Export **intentionally omits** superseded_keys: it streams, whereas bulk id→key resolution would first
            # materialize all entries in memory. A display-only field does not justify abandoning streaming.
            # Exports already contain every entry; consumers can join by id themselves.
            canonical = _safe_export_item(
                e,
                accessible_spaces=accessible_spaces,
            )
            if canonical is None:
                continue
            obj = canonical.model_dump(mode="json")
            yield ("    " if first else ",\n    ") + json.dumps(obj, ensure_ascii=False)
            first = False
        yield "\n  ],\n"
        about_you = None
        if notes_store is not None:
            try:
                note = await notes_store.get(user_id=uid)
                if note:
                    about_you = {
                        "content": note.content,
                        "scope": {
                            "type": "global",
                            "label": "仅供你使用 · 所有空间",
                        },
                        "state": "active",
                        "version": str(note.version),
                        "created_at": note.created_at,
                        "updated_at": note.updated_at,
                        "deleted_at": note.deleted_at,
                        "provenance": {
                            "created_source_type": note.created_source_type,
                            "created_source_turn_id": note.created_source_turn_id,
                            "created_source_candidate_id": (note.created_source_candidate_id),
                            "created_source_space_id": (note.created_source_workspace_id),
                            "last_modified_actor_type": (note.last_modified_actor_type),
                            "last_modified_at": note.last_modified_at,
                        },
                    }
            except Exception as e:
                logger.warning("memory.export.notes_failed err=%s", e)
        yield '  "about_you": ' + json.dumps(about_you, ensure_ascii=False) + ",\n"
        recoverable_about_you = []
        for recovery in about_you_recoveries:
            item = _about_you_recovery_to_canonical(recovery)
            recoverable_about_you.append(
                {
                    "recovery_id": recovery["recovery_id"],
                    "content": item.display_value,
                    "state": "trashed",
                    "expires_at": recovery["expires_at"],
                    "provenance": item.provenance.model_dump(mode="json"),
                },
            )
        yield ('  "recoverable_about_you": ' + json.dumps(recoverable_about_you, ensure_ascii=False) + ",\n")
        yield (
            '  "recovery": '
            + json.dumps(
                {
                    "recoverable_reset_count": reset_count,
                    "reset_payloads_included": False,
                },
            )
            + "\n"
        )
        yield "}\n"

    async def _stream():
        # Hold one shared-DB read snapshot for the whole response. A concurrent
        # permanent delete may scrub logically, but its WAL truncate cannot
        # complete (and therefore cannot report success) until the final export
        # byte has left this generator.
        async with open_shared_ctx(user_mem.db_path) as export_guard:
            await export_guard.execute("BEGIN")
            async with export_guard.execute(
                """
                SELECT id
                FROM memory_entries
                WHERE subject_type='user' AND subject_id=?
                LIMIT 1
                """,
                (uid,),
            ) as cursor:
                await cursor.fetchone()
            try:
                async for chunk in _stream_payload():
                    yield chunk
            finally:
                await export_guard.rollback()

    await _audit(request, user_id=uid, event_type="memory.export")
    await _track(request, name="memory.export.requested", user_id=uid)
    return StreamingResponse(
        _stream(),
        media_type="application/json",
        headers={
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": (f'attachment; filename="bowhead-memory-{datetime.now(tz=UTC).date().isoformat()}.json"'),
        },
    )


# ─── dreaming pause toggle ───────────────────────────────


@router.get("/dreaming", response_model=DreamingStateDTO)
async def dreaming_state(request: Request) -> DreamingStateDTO:
    uid = _user_id(request)
    cache = _pause_cache(request)
    if cache is None:
        return DreamingStateDTO(paused=False)
    return DreamingStateDTO(paused=await cache.is_paused(uid))


@router.post("/dreaming/pause", response_model=DreamingStateDTO)
async def dreaming_pause(request: Request) -> DreamingStateDTO:
    uid = _user_id(request)
    settings = _settings_store(request)
    if settings is None:
        raise HTTPException(503, "user_settings_store not configured")
    await settings.set(uid, SETTINGS_KEY_DREAMING_PAUSED, "true")
    cache = _pause_cache(request)
    if cache is not None:
        cache.invalidate(uid)
    await _audit(request, user_id=uid, event_type="memory.dreaming.pause")
    await _track(request, name="memory.dreaming.paused", user_id=uid)
    return DreamingStateDTO(paused=True)


@router.post("/dreaming/resume", response_model=DreamingStateDTO)
async def dreaming_resume(request: Request) -> DreamingStateDTO:
    uid = _user_id(request)
    settings = _settings_store(request)
    if settings is None:
        raise HTTPException(503, "user_settings_store not configured")
    await settings.set(uid, SETTINGS_KEY_DREAMING_PAUSED, "false")
    cache = _pause_cache(request)
    if cache is not None:
        cache.invalidate(uid)
    await _audit(request, user_id=uid, event_type="memory.dreaming.resume")
    await _track(request, name="memory.dreaming.resumed", user_id=uid)
    return DreamingStateDTO(paused=False)
