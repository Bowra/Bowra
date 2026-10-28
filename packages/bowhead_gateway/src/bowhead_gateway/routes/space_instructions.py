"""/api/v1/space-instructions — administrator-written instructions for every conversation in a Space.

GET  /api/v1/space-instructions/{workspace_id}           current version (Space member or Team manager)
PUT  /api/v1/space-instructions/{workspace_id}           append a version (Space admin or Team manager)
GET  /api/v1/space-instructions/{workspace_id}/versions  version history (Space admin or Team manager)

The Space must belong to the caller's current Team (404 otherwise). Team owners and team admins manage
instructions without joining the Space: they already appoint Space admins, and the suggestion inbox they
review routes adopted wording here. Writes and history require an active Space.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bowhead_core.access_control import normalize_workspace_role
from bowhead_core.space_instructions import (
    MAX_CONTENT_CHARS,
    SpaceInstructionsError,
    SpaceInstructionsStore,
    SpaceInstructionsVersion,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.auth import _store as _user_store_dep
from bowhead_gateway.routes.feedback_candidates import _source_spaces

if TYPE_CHECKING:
    from bowhead_core.user_store import User, UserStore

router = APIRouter(prefix="/api/v1/space-instructions", tags=["space-instructions"])

_ERROR_STATUS = {"too_long": 422, "unchanged": 409, "stale_version": 409, "restore_source_missing": 404}


def _store(request: Request) -> SpaceInstructionsStore:
    store = getattr(request.app.state, "space_instructions_store", None)
    if store is None:
        raise HTTPException(503, {"code": "space_instructions_unavailable"})
    return store


class SpaceInstructionsVersionDTO(BaseModel):
    version: int
    content: str
    created_by: str
    created_by_name: str
    created_at: str
    restored_from: int | None = None
    source_candidate_id: str | None = None


class SpaceInstructionsDTO(BaseModel):
    workspace_id: str
    #: 0 when the Space has never had instructions.
    version: int
    content: str
    updated_by_name: str | None = None
    updated_at: str | None = None
    max_chars: int = MAX_CONTENT_CHARS
    can_edit: bool
    #: Archived Spaces keep their instructions readable but reject changes until restored.
    archived: bool = False
    #: Display names of the Space administrators, so readers know whom to ask for a change.
    administrators: list[str] = Field(default_factory=list)


class SpaceInstructionsVersionsDTO(BaseModel):
    versions: list[SpaceInstructionsVersionDTO]


class SaveSpaceInstructions(BaseModel):
    content: str = Field(..., max_length=MAX_CONTENT_CHARS * 2)
    expected_version: int = Field(..., ge=0)
    restored_from: int | None = Field(None, ge=1)
    #: The accepted suggestion inbox candidate whose wording this save adds.
    source_candidate_id: str | None = Field(None, max_length=64)


_TEAM_MANAGER_ROLES = frozenset({"owner", "team_admin"})


def _denied(user: User, workspace_id: str, what: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={"error": {"code": "E_PERMISSION_DENIED", "message": f"user '{user.id}' is not {what} for '{workspace_id}'"}},
    )


async def _access(user_store: UserStore, workspace_id: str, user: User) -> tuple[bool, bool, bool]:
    """(can_read, can_manage, archived) for the caller; 404 for a Space outside the current Team."""
    workspace = await user_store.get_workspace(workspace_id)
    if workspace is None or not user.team_id or workspace["team_id"] != user.team_id:
        raise HTTPException(status_code=404, detail="workspace not found")
    archived = workspace["status"] != "active"
    member_role = normalize_workspace_role(await user_store.get_member_role(workspace_id, user.id))
    team_manager = await user_store.get_team_role(user.id, user.team_id) in _TEAM_MANAGER_ROLES
    can_manage = member_role == "ws_admin" or team_manager
    return member_role is not None or team_manager, can_manage, archived


async def _require_manager(user_store: UserStore, workspace_id: str, user: User) -> None:
    _can_read, can_manage, archived = await _access(user_store, workspace_id, user)
    if archived:
        raise HTTPException(409, {"code": "workspace_archived"})
    if not can_manage:
        raise _denied(user, workspace_id, "a workspace admin")


def _version_dto(item: SpaceInstructionsVersion, names: dict[str, str]) -> SpaceInstructionsVersionDTO:
    return SpaceInstructionsVersionDTO(
        version=item.version,
        content=item.content,
        created_by=item.created_by,
        created_by_name=names.get(item.created_by, item.created_by),
        created_at=item.created_at,
        restored_from=item.restored_from,
        source_candidate_id=item.source_candidate_id,
    )


async def _current_dto(
    store: SpaceInstructionsStore,
    user_store: UserStore,
    workspace_id: str,
    *,
    can_edit: bool,
    archived: bool = False,
) -> SpaceInstructionsDTO:
    current = await store.current(workspace_id)
    administrators = await _administrator_names(user_store, workspace_id)
    if current is None:
        return SpaceInstructionsDTO(
            workspace_id=workspace_id, version=0, content="", can_edit=can_edit, archived=archived, administrators=administrators
        )
    names = await user_store.get_users_by_ids([current.created_by])
    return SpaceInstructionsDTO(
        workspace_id=workspace_id,
        version=current.version,
        content=current.content,
        updated_by_name=names.get(current.created_by, current.created_by),
        updated_at=current.created_at,
        can_edit=can_edit,
        archived=archived,
        administrators=administrators,
    )


async def _administrator_names(user_store: UserStore, workspace_id: str) -> list[str]:
    members = await user_store.list_members(workspace_id)
    names = [member["display_name"] or member["email"] for member in members if member["role"] == "admin"]
    return sorted(name for name in names if name)


async def _require_adoptable_candidate(request: Request, user_store: UserStore, user: User, candidate_id: str, workspace_id: str) -> None:
    """The source must be an accepted Business-definition suggestion of the caller's Team raised from this Space."""

    candidates = getattr(request.app.state, "feedback_candidate_store", None)
    candidate = await candidates.get(candidate_id) if candidates is not None else None
    if candidate is None or candidate.team_id != user.team_id:
        raise HTTPException(404, {"code": "source_candidate_missing", "candidate_id": candidate_id})
    if candidate.target_kind != "semantic" or candidate.state != "accepted":
        raise HTTPException(409, {"code": "source_candidate_not_adopted", "candidate_id": candidate_id})
    if workspace_id not in {space["workspace_id"] for space in await _source_spaces(user_store, candidate)}:
        raise HTTPException(409, {"code": "source_candidate_other_space", "candidate_id": candidate_id})


@router.get("/{workspace_id}", response_model=SpaceInstructionsDTO)
async def get_space_instructions(
    workspace_id: str,
    store: SpaceInstructionsStore = Depends(_store),
    user: User = Depends(_current_user),
    user_store: UserStore = Depends(_user_store_dep),
) -> SpaceInstructionsDTO:
    can_read, can_manage, archived = await _access(user_store, workspace_id, user)
    if not can_read:
        raise _denied(user, workspace_id, "a workspace member")
    return await _current_dto(store, user_store, workspace_id, can_edit=can_manage and not archived, archived=archived)


@router.put(
    "/{workspace_id}",
    response_model=SpaceInstructionsDTO,
    dependencies=[Depends(require_not_readonly)],
)
async def save_space_instructions(
    workspace_id: str,
    body: SaveSpaceInstructions,
    request: Request,
    store: SpaceInstructionsStore = Depends(_store),
    user: User = Depends(_current_user),
    user_store: UserStore = Depends(_user_store_dep),
) -> SpaceInstructionsDTO:
    await _require_manager(user_store, workspace_id, user)
    if body.source_candidate_id is not None:
        await _require_adoptable_candidate(request, user_store, user, body.source_candidate_id, workspace_id)
    try:
        await store.save(
            workspace_id=workspace_id,
            content=body.content,
            created_by=user.id,
            expected_version=body.expected_version,
            restored_from=body.restored_from,
            source_candidate_id=body.source_candidate_id,
        )
    except SpaceInstructionsError as exc:
        raise HTTPException(_ERROR_STATUS.get(exc.code, 422), {"code": exc.code, **exc.details}) from exc
    return await _current_dto(store, user_store, workspace_id, can_edit=True)


@router.get("/{workspace_id}/versions", response_model=SpaceInstructionsVersionsDTO)
async def list_space_instruction_versions(
    workspace_id: str,
    store: SpaceInstructionsStore = Depends(_store),
    user: User = Depends(_current_user),
    user_store: UserStore = Depends(_user_store_dep),
) -> SpaceInstructionsVersionsDTO:
    await _require_manager(user_store, workspace_id, user)
    versions = await store.list_versions(workspace_id)
    names = await user_store.get_users_by_ids([item.created_by for item in versions])
    return SpaceInstructionsVersionsDTO(versions=[_version_dto(item, names) for item in versions])
