"""Team Space governance API.

These routes expose governance metadata only.  Content access continues to
require an explicit active ``workspace_members`` row.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any, Literal

from bowhead_core.space_governance import (
    SpaceGovernanceError,
    normalize_space_name,
)
from bowhead_core.user_store import User, UserStore
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes.auth import _current_user, _store

router = APIRouter(prefix="/api/v1/admin/spaces")
logger = logging.getLogger(__name__)

_ERROR_HTTP_STATUS = {
    "workspace_not_found": 404,
    "not_team_admin": 403,
    "not_workspace_admin": 403,
    "workspace_archived": 409,
    "workspace_not_archived": 409,
    "last_active_space": 409,
    "restore_admin_required": 409,
    "recovery_admin_not_active": 422,
    "duplicate_workspace_name": 409,
    "stale_workspace_update": 409,
    "workspace_name_required": 422,
    "workspace_name_too_long": 422,
    "invalid_workspace_status": 422,
}


def _raise_space_error(exc: SpaceGovernanceError) -> None:
    detail: dict[str, Any] = {"code": exc.code}
    if exc.details:
        detail["details"] = exc.details
    raise HTTPException(_ERROR_HTTP_STATUS.get(exc.code, 409), detail) from exc


async def pause_archived_space_jobs(
    app: Any,
    *,
    workspace_id: str,
) -> None:
    """Converge scheduled jobs after archive without weakening the archive commit.

    Every archive entry (Team governance, legacy Space status, OpenAPI) calls this
    so restore can resume exactly the jobs archiving paused.
    """

    scheduled_store = getattr(app.state, "scheduled_store", None)
    pause_jobs = getattr(
        scheduled_store,
        "pause_active_for_workspace",
        None,
    )
    if not callable(pause_jobs):
        return
    try:
        paused = await pause_jobs(
            workspace_id,
            reason="workspace_archived",
        )
        from bowhead_core import metrics

        metrics.inc(
            "space_archive_scheduled_jobs_paused_total",
            value=float(paused),
        )
    except Exception as exc:
        logger.exception(
            "space_archive.scheduled_pause_failed workspace=%s err=%s",
            workspace_id,
            exc,
        )
        from bowhead_core import metrics

        metrics.inc("space_archive_scheduled_pause_failures_total")


async def resume_restored_space_jobs(
    app: Any,
    *,
    workspace_id: str,
) -> None:
    """Resume the jobs archiving paused, after the restore commit.

    Only jobs paused with the archive reason come back; a user's own pause and
    other system pauses stay.  Like the archive-side pause this converges after
    the Space commit and never fails the restore: jobs left paused can still be
    re-enabled one by one.
    """

    scheduled_store = getattr(app.state, "scheduled_store", None)
    resume_jobs = getattr(
        scheduled_store,
        "resume_archived_for_workspace",
        None,
    )
    if not callable(resume_jobs):
        return
    from bowhead_core import metrics

    try:
        result = await resume_jobs(workspace_id)
    except Exception as exc:
        logger.exception(
            "space_restore.scheduled_resume_failed workspace=%s err=%s",
            workspace_id,
            exc,
        )
        metrics.inc("space_restore_scheduled_resume_failures_total")
        return
    metrics.inc(
        "space_restore_scheduled_jobs_resumed_total",
        value=float(len(result.resumed)),
    )
    if result.over_limit:
        metrics.inc(
            "space_restore_scheduled_jobs_over_limit_total",
            value=float(len(result.over_limit)),
        )


def _query_hash(status: str, q: str | None) -> str:
    return hashlib.sha256(f"{status}\0{q or ''}".encode()).hexdigest()[:16]


def _encode_cursor(
    *,
    status: str,
    q: str | None,
    name_key: str,
    workspace_id: str,
) -> str:
    raw = json.dumps(
        {
            "filter": _query_hash(status, q),
            "name_key": name_key,
            "workspace_id": workspace_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(
    cursor: str | None,
    *,
    status: str,
    q: str | None,
) -> tuple[str | None, str | None]:
    if not cursor:
        return None, None
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, {"code": "invalid_cursor"}) from exc
    if payload.get("filter") != _query_hash(status, q):
        raise HTTPException(422, {"code": "cursor_filter_mismatch"})
    name_key = payload.get("name_key")
    workspace_id = payload.get("workspace_id")
    if not isinstance(name_key, str) or not isinstance(workspace_id, str):
        raise HTTPException(422, {"code": "invalid_cursor"})
    return name_key, workspace_id


class CreateSpaceBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    set_as_current: bool = False


class RevisionBody(BaseModel):
    expected_revision: int = Field(..., ge=1)


class RestoreSpaceBody(RevisionBody):
    recovery_admin_user_id: str | None = Field(None, min_length=1, max_length=128)


class SpaceAdminBody(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)


@router.get("")
async def list_team_spaces(
    status: Literal["active", "archived", "all"] = "active",
    q: str | None = Query(None, max_length=120),
    page_size: int = Query(50, ge=1, le=100),
    cursor: str | None = None,
    caller: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    after_name_key, after_workspace_id = _decode_cursor(
        cursor,
        status=status,
        q=q,
    )
    try:
        items, has_more = await store.list_team_workspaces_governance(
            team_id=caller.team_id,
            caller_user_id=caller.id,
            status=status,
            q=q,
            page_size=page_size,
            after_name_key=after_name_key,
            after_workspace_id=after_workspace_id,
        )
    except SpaceGovernanceError as exc:
        _raise_space_error(exc)
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = _encode_cursor(
            status=status,
            q=q,
            name_key=normalize_space_name(str(last["name"]))[1],
            workspace_id=str(last["workspace_id"]),
        )
    return {"items": items, "next_cursor": next_cursor}


@router.post(
    "",
    status_code=201,
    dependencies=[Depends(require_not_readonly)],
)
async def create_team_space(
    body: CreateSpaceBody,
    request: Request,
    caller: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    try:
        workspace = await store.create_governed_workspace(
            team_id=caller.team_id,
            actor_user_id=caller.id,
            name=body.name,
            description=body.description,
            set_as_current=body.set_as_current,
        )
    except SpaceGovernanceError as exc:
        _raise_space_error(exc)
    from bowhead_gateway.space_assignment_notifications import (
        resolve_space_assignment_if_ready,
    )

    await resolve_space_assignment_if_ready(
        request,
        store=store,
        team_id=caller.team_id,
        user_id=caller.id,
    )
    return workspace


@router.get("/{workspace_id}")
async def get_team_space(
    workspace_id: str,
    caller: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    try:
        return await store.get_team_workspace_governance(
            team_id=caller.team_id,
            caller_user_id=caller.id,
            workspace_id=workspace_id,
        )
    except SpaceGovernanceError as exc:
        _raise_space_error(exc)


@router.get("/{workspace_id}/admins")
async def list_team_space_admins(
    workspace_id: str,
    caller: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """List assignable Team members and current Space administrators.

    This is governance data only; a caller does not need to be a Space member.
    """
    try:
        await store.get_team_workspace_governance(
            team_id=caller.team_id,
            caller_user_id=caller.id,
            workspace_id=workspace_id,
        )
    except SpaceGovernanceError as exc:
        _raise_space_error(exc)
    members = await store.list_members(workspace_id)
    return {
        "admins": [member for member in members if member["role"] == "admin"],
        "team_members": await store.list_active_team_members_for_governance(caller.team_id),
    }


@router.put(
    "/{workspace_id}/admins",
    dependencies=[Depends(require_not_readonly)],
)
async def grant_team_space_admin(
    workspace_id: str,
    body: SpaceAdminBody,
    caller: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    try:
        await store.get_team_workspace_governance(
            team_id=caller.team_id,
            caller_user_id=caller.id,
            workspace_id=workspace_id,
        )
        team_members = await store.list_active_team_members_for_governance(caller.team_id)
        if body.user_id not in {str(member["user_id"]) for member in team_members}:
            raise HTTPException(422, {"code": "space_admin_not_active_team_member"})
        role = await store.get_member_role(workspace_id, body.user_id)
        if role is None:
            await store.add_member(
                workspace_id=workspace_id,
                user_id=body.user_id,
                role="admin",
                invited_by=caller.id,
            )
        elif role != "admin":
            await store.update_member_role(workspace_id, body.user_id, "admin")
    except SpaceGovernanceError as exc:
        _raise_space_error(exc)
    return {"ok": True, "workspace_id": workspace_id, "user_id": body.user_id}


@router.delete(
    "/{workspace_id}/admins/{user_id}",
    dependencies=[Depends(require_not_readonly)],
)
async def revoke_team_space_admin(
    workspace_id: str,
    user_id: str,
    caller: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    try:
        await store.get_team_workspace_governance(
            team_id=caller.team_id,
            caller_user_id=caller.id,
            workspace_id=workspace_id,
        )
    except SpaceGovernanceError as exc:
        _raise_space_error(exc)
    if await store.get_member_role(workspace_id, user_id) != "admin":
        raise HTTPException(404, {"code": "space_admin_not_found"})
    if await store.count_active_workspace_admins(workspace_id) <= 1:
        raise HTTPException(409, {"code": "last_space_admin"})
    await store.update_member_role(workspace_id, user_id, "member")
    return {"ok": True, "workspace_id": workspace_id, "user_id": user_id}


@router.post(
    "/{workspace_id}/archive",
    dependencies=[Depends(require_not_readonly)],
)
async def archive_team_space(
    workspace_id: str,
    body: RevisionBody,
    request: Request,
    caller: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    try:
        workspace = await store.archive_governed_workspace(
            team_id=caller.team_id,
            actor_user_id=caller.id,
            workspace_id=workspace_id,
            expected_revision=body.expected_revision,
        )
    except SpaceGovernanceError as exc:
        _raise_space_error(exc)
    await pause_archived_space_jobs(
        request.app,
        workspace_id=workspace_id,
    )
    return workspace


@router.post(
    "/{workspace_id}/restore",
    dependencies=[Depends(require_not_readonly)],
)
async def restore_team_space(
    workspace_id: str,
    body: RestoreSpaceBody,
    request: Request,
    caller: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    try:
        workspace = await store.restore_governed_workspace(
            team_id=caller.team_id,
            actor_user_id=caller.id,
            workspace_id=workspace_id,
            expected_revision=body.expected_revision,
            recovery_admin_user_id=body.recovery_admin_user_id,
        )
    except SpaceGovernanceError as exc:
        _raise_space_error(exc)
    await resume_restored_space_jobs(
        request.app,
        workspace_id=workspace_id,
    )
    return workspace
