"""V4 PRD-D-4-4 · Memory Pack Snapshot Replay routes.

User-self path:
  GET /api/v1/memory/turns/{turn_id}/memory-pack-snapshot
  Returns the snapshot row for the caller's own turn. Cross-user attempts
  return 404 (opaque — never reveal that the turn exists for another user).

Team-admin path:
  GET /api/v1/admin/turns/{turn_id}/memory-pack-snapshot
  Returns the snapshot row if turn's team_id matches the caller's team_id.
  Returns 204 for a visible turn with no saved snapshot.
  NULL team_id rows (orphan / dev / scheduled turns) are NOT in any admin
  scope (returns 404 even if turn exists).
  Side effect: writes audit log row 'admin.replay.viewed' (auditing the auditor).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from bowhead_gateway.routes._deps import require_team_admin
from bowhead_gateway.routes.turns import _require_turn_access

if TYPE_CHECKING:
    from bowhead_core.memory_pack_snapshots import (
        MemoryPackSnapshot,
        MemoryPackSnapshotsStore,
    )
    from bowhead_core.user_store import User

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── DTO ────────────────────────────────────────────────


class MemoryPackSnapshotDTO(BaseModel):
    turn_id: str
    user_id: str
    team_id: str | None = None
    workspace_id: str | None = None
    char_count: int
    sections_breakdown: dict[str, int]
    content: str
    pack_version: str
    created_at: str


def _to_dto(snap: MemoryPackSnapshot) -> MemoryPackSnapshotDTO:
    return MemoryPackSnapshotDTO(
        turn_id=snap.turn_id,
        user_id=snap.user_id,
        team_id=snap.team_id,
        workspace_id=snap.workspace_id,
        char_count=snap.char_count,
        sections_breakdown=snap.sections_breakdown,
        content=snap.content,
        pack_version=snap.pack_version,
        created_at=snap.created_at,
    )


# ─── store accessor ─────────────────────────────────────


def _snapshots_store(request: Request) -> MemoryPackSnapshotsStore:
    store = getattr(request.app.state, "memory_pack_snapshots_store", None)
    if store is None:
        raise HTTPException(503, "memory replay not configured")
    return store


def _user_id(request: Request) -> str:
    from bowhead_gateway.routes.auth import resolve_user_id_sync

    return resolve_user_id_sync(request)


async def _audit(
    request: Request,
    *,
    user_id: str,
    team_id: str | None,
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
            team_id=team_id,
            event_type=event_type,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            payload=payload,
        )
    except Exception as e:
        logger.debug("memory_replay.audit_failed event=%s err=%s", event_type, e)


# ─── routes ─────────────────────────────────────────────


@router.get("/api/v1/memory/turns/{turn_id}/memory-pack-snapshot")
async def get_user_turn_snapshot(
    turn_id: str,
    request: Request,
    store: MemoryPackSnapshotsStore = Depends(_snapshots_store),
) -> MemoryPackSnapshotDTO:
    """User-self replay. 404 if turn not found OR belongs to another user."""
    uid = _user_id(request)
    snap = await store.get_by_turn_for_user(turn_id=turn_id, user_id=uid)
    if snap is None:
        raise HTTPException(404, "snapshot not found")
    return _to_dto(snap)


@router.get(
    "/api/v1/admin/turns/{turn_id}/memory-pack-snapshot",
    response_model=MemoryPackSnapshotDTO,
)
async def get_admin_turn_snapshot(
    turn_id: str,
    request: Request,
    admin: User = Depends(require_team_admin),
    store: MemoryPackSnapshotsStore = Depends(_snapshots_store),
) -> MemoryPackSnapshotDTO | Response:
    """Return 204 for an accessible turn without a snapshot, 404 outside scope."""
    if not admin.team_id:
        # Defensive: team-admin without team_id can't be scoped.
        raise HTTPException(404, "snapshot not found")
    snap = await store.get_by_turn_for_admin(
        turn_id=turn_id,
        admin_team_id=admin.team_id,
    )
    if snap is None:
        session_store = getattr(request.app.state, "session_store", None)
        user_store = getattr(request.app.state, "user_store", None)
        if session_store is None or user_store is None:
            raise HTTPException(503, "turn scope not configured")
        turn = await session_store.get_turn(turn_id)
        session = await session_store.get_session(turn.session_id) if turn else None
        workspace = await user_store.get_workspace(session.workspace_id) if session else None
        if workspace is None or workspace.get("team_id") != admin.team_id:
            raise HTTPException(404, "snapshot not found")
        await _require_turn_access(turn_id, request, admin)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    # auditing the auditor (PRD-D-4-4 §5.4)
    await _audit(
        request,
        user_id=admin.id,
        team_id=admin.team_id,
        event_type="admin.replay.viewed",
        payload={"turn_id": turn_id, "target_user_id": snap.user_id},
    )
    return _to_dto(snap)
