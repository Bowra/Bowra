"""Groups and resource-grant administration routes."""

from __future__ import annotations

import contextlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
from bowhead_core.audit_actions import AuditAction
from bowhead_core.resource_access import (
    SHAREABLE_KINDS,
    canonical_builtin_capability_id,
    canonical_permission,
    list_builtin_capability_ids,
)
from bowhead_core.subagent_write_boundary import (
    TeamSubagentWriteBoundaryError,
    reject_obsolete_team_subagent_write,
)
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from bowhead_core import share_service
from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.access_context import (
    access_context_to_dict,
    build_access_context_from_request,
)
from bowhead_gateway.routes.auth import _current_user, _require_team_role_dep, _store

if TYPE_CHECKING:
    from bowhead_core.access_control import AccessControlStore
    from bowhead_core.user_store import User, UserStore

router = APIRouter()

_RESOURCE_KINDS = {
    "knowledge_base",
    "kb_scope",
    "kb_file",
    "skill",
    "mcp",
    "tool",
    "team_tool_source",
    "team_tool_op",
    "builtin_capability",
    # content-permissions wave 1 shareable kinds (resource_id: osa_*/oskill_*/session_id).
    # NOTE: "skill" above = builtin catalog NAME; "team_skill" = oskill_* row id — two
    # physically separate grant spaces (tech-design §2.4).
    "team_subagent",
    "team_skill",
    "report",
}
_PERMISSIONS = {"view", "read", "use", "invoke", "edit", "write", "manage", "share", "admin"}
_EFFECTS = {"allow", "deny"}
_PRINCIPAL_TYPES = {"user", "group", "workspace", "team"}
_REQUIRE_ACCESS_ADMIN = _require_team_role_dep("owner", "team_admin")


def _reject_obsolete_team_subagent_write(resource_kind: str) -> None:
    try:
        reject_obsolete_team_subagent_write(resource_kind)
    except TeamSubagentWriteBoundaryError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "error": {
                    "code": exc.code,
                    "message": ("This Subagent grant API is obsolete; use the Subagent management API."),
                }
            },
        ) from None


class GroupCreateDTO(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    workspace_id: str | None = Field(default=None, max_length=80)


class GroupDTO(BaseModel):
    id: str
    team_id: str
    workspace_id: str | None
    name: str
    description: str
    status: str
    member_count: int
    created_by: str | None
    created_at: str
    updated_at: str


class GroupListDTO(BaseModel):
    items: list[GroupDTO]


class GroupMemberAddDTO(BaseModel):
    user_id: str = Field(min_length=1, max_length=80)
    role: str = Field(default="member", pattern="^(member|manager)$")


class GroupMemberDTO(BaseModel):
    group_id: str
    user_id: str
    role: str
    email: str | None = None
    display_name: str | None = None
    created_at: str


class GroupMemberListDTO(BaseModel):
    items: list[GroupMemberDTO]


class ResourceGrantCreateDTO(BaseModel):
    resource_kind: str = Field(min_length=1, max_length=80)
    resource_id: str = Field(min_length=1, max_length=160)
    principal_type: str = Field(pattern="^(user|group|workspace|team)$")
    principal_id: str = Field(min_length=1, max_length=160)
    permission: str = Field(pattern="^(view|read|use|invoke|edit|write|manage|share|admin)$")
    effect: str = Field(default="allow", pattern="^(allow|deny)$")
    expires_at: str | None = Field(default=None, max_length=80)


class ResourceGrantDTO(BaseModel):
    id: str
    team_id: str
    resource_kind: str
    resource_id: str
    principal_type: str
    principal_id: str
    permission: str
    effect: str
    created_by: str | None
    created_at: str
    updated_at: str
    expires_at: str | None = None
    revoked_at: str | None = None


class ResourceGrantListDTO(BaseModel):
    items: list[ResourceGrantDTO]


class PermissionExplainDTO(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=80)
    resource_kind: str = Field(min_length=1, max_length=80)
    resource_id: str = Field(min_length=1, max_length=160)
    permission: str = Field(default="read", pattern="^(view|read|use|invoke|edit|write|manage|share|admin)$")


class PermissionExplainResultDTO(BaseModel):
    allowed: bool
    reason: str
    shadow: bool = False
    grant_id: str | None = None
    access_context: dict[str, Any]


def _access_store(request: Request) -> AccessControlStore:
    store: AccessControlStore | None = getattr(request.app.state, "access_control_store", None)
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "access_control_store not configured")
    return store


def _group_to_dto(g: Any) -> GroupDTO:
    return GroupDTO(
        id=g.id,
        team_id=g.team_id,
        workspace_id=g.workspace_id,
        name=g.name,
        description=g.description,
        status=g.status,
        member_count=g.member_count,
        created_by=g.created_by,
        created_at=g.created_at,
        updated_at=g.updated_at,
    )


def _member_to_dto(m: Any) -> GroupMemberDTO:
    return GroupMemberDTO(
        group_id=m.group_id,
        user_id=m.principal_id,
        role=m.role,
        email=m.email,
        display_name=m.display_name,
        created_at=m.created_at,
    )


def _grant_to_dto(g: Any) -> ResourceGrantDTO:
    return ResourceGrantDTO(
        id=g.id,
        team_id=g.team_id,
        resource_kind=g.resource_kind,
        resource_id=g.resource_id,
        principal_type=g.principal_type,
        principal_id=g.principal_id,
        permission=g.permission,
        effect=g.effect,
        created_by=g.created_by,
        created_at=g.created_at,
        updated_at=g.updated_at,
        expires_at=g.expires_at,
        revoked_at=g.revoked_at,
    )


def _validate_expires_at(value: str | None) -> str | None:
    if not value:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "expires_at must be ISO-8601") from exc
    return value


async def _audit(
    *,
    request: Request,
    store: UserStore,
    user: User,
    action: str,
    target_kind: str,
    target_id: str,
    payload: dict[str, Any],
) -> None:
    with contextlib.suppress(Exception):
        await store.write_admin_audit_log(
            actor_user_id=user.id,
            team_id=user.team_id,
            action=action,
            target_kind=target_kind,
            target_id=target_id,
            target_team_id=user.team_id,
            scope="team",
            payload=payload,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )


async def _assert_workspace_in_team(store: UserStore, team_id: str, workspace_id: str) -> None:
    ws_team = await store.get_workspace_team_id(workspace_id)
    if ws_team != team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")


async def _is_access_admin(user: User, user_store: UserStore) -> bool:
    role = await user_store.get_team_role(user.id, user.team_id)
    return role in {"owner", "team_admin"}


def _canonical_self_explain_resource(
    *,
    user: User,
    workspace_id: str,
    kind: str,
    resource_id: str,
) -> tuple[str, str]:
    resource_kind = kind.strip()
    rid = resource_id.strip()
    if resource_kind not in _RESOURCE_KINDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unsupported resource_kind")
    if resource_kind in SHAREABLE_KINDS:
        # self-explain over shareable kinds: existence is owner/visibility-scoped
        # elsewhere; here only the kind label passes through (no resource probe).
        if not rid:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "resource_id required")
        return resource_kind, rid
    if resource_kind in {"kb_scope", "knowledge_base"}:
        if rid.startswith("team:"):
            team_id = rid.split(":", 1)[1]
            if team_id != user.team_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "kb team scope not found")
            return "kb_scope", f"team:{team_id}"
        # Batch 3/c5: accept space:/workspace: input prefixes; freeze workspace: output (D2 persisted literal).
        scope_ws = rid.split(":", 1)[1] if rid.startswith(("workspace:", "space:")) else rid
        if scope_ws != workspace_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "kb workspace scope not found")
        return "kb_scope", f"workspace:{scope_ws}"
    if resource_kind == "builtin_capability":
        capability_id = canonical_builtin_capability_id(rid)
        if capability_id is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "builtin capability not found")
        return "builtin_capability", capability_id
    if resource_kind == "mcp":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "user MCP servers are personal/dev and are not M1 shared resources; use team_tool_source",
        )
    if resource_kind == "team_tool_op":
        parts = rid.split(".")
        if len(parts) < 3 or parts[0] != "teammcp":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "team_tool_op must be teammcp.<source_id>.<operation_id>",
            )
    return resource_kind, rid


def _sanitize_self_explain_reason(decision: dict[str, Any]) -> str:
    return "allowed" if decision.get("allowed") else "denied"


async def _validate_principal(
    *,
    user: User,
    user_store: UserStore,
    access_store: AccessControlStore,
    principal_type: str,
    principal_id: str,
) -> None:
    if principal_type not in _PRINCIPAL_TYPES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid principal_type")
    if principal_type == "team":
        if principal_id != user.team_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "principal team must be caller team")
        return
    if principal_type == "workspace":
        await _assert_workspace_in_team(user_store, user.team_id, principal_id)
        return
    if principal_type == "group":
        if await access_store.get_group(user.team_id, principal_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "group not found")
        return
    target = await user_store.get_user(principal_id)
    if target is None or target.team_id != user.team_id or target.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")


async def _canonical_resource(
    *,
    request: Request,
    user: User,
    user_store: UserStore,
    kind: str,
    resource_id: str,
) -> tuple[str, str]:
    resource_kind = kind.strip()
    rid = resource_id.strip()
    if resource_kind not in _RESOURCE_KINDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unsupported resource_kind")
    if resource_kind in {"kb_scope", "knowledge_base"}:
        if rid.startswith("team:"):
            team_id = rid.split(":", 1)[1]
            if team_id != user.team_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "kb team scope not found")
            return "kb_scope", f"team:{team_id}"
        # Batch 3/c5: accept both input prefixes; freeze workspace: output.
        workspace_id = rid.split(":", 1)[1] if rid.startswith(("workspace:", "space:")) else rid
        await _assert_workspace_in_team(user_store, user.team_id, workspace_id)
        return "kb_scope", f"workspace:{workspace_id}"
    if resource_kind == "kb_file":
        files_store = getattr(request.app.state, "kb_files_store", None)
        if files_store is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "kb files store not configured")
        file = await files_store.get(rid)
        if file is None or getattr(file, "soft_deleted_at", None):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "kb file not found")
        if getattr(file, "team_id", None) != user.team_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "kb file not found")
        return "kb_file", rid
    if resource_kind == "skill":
        catalog = getattr(request.app.state, "skill_catalog", None)
        if catalog is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "skill catalog not configured")
        spec = catalog.get(rid)
        if spec is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "skill not found")
        try:
            from bowhead_skill.catalog import builtin_catalog_dir

            source_dir = Path(str(spec.source_dir)).resolve()
            source_dir.relative_to(builtin_catalog_dir().resolve())
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "user-generated skill sharing requires published skill storage",
            ) from exc
        except Exception:  # noqa: S110
            pass
        return "skill", rid
    if resource_kind == "tool":
        registry = getattr(request.app.state, "tool_registry", None)
        if registry is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "tool registry not configured")
        if not callable(getattr(registry, "has", None)) or not registry.has(rid):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "tool not found")
        return "tool", rid
    if resource_kind == "builtin_capability":
        capability_id = canonical_builtin_capability_id(rid)
        if capability_id is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"builtin capability not found; valid ids: {', '.join(list_builtin_capability_ids())}",
            )
        return "builtin_capability", capability_id
    if resource_kind == "mcp":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "user MCP servers are personal/dev and are not M1 shared resources; use team_tool_source",
        )
    if resource_kind == "team_tool_source":
        if await user_store.get_team_tool_source(user.team_id, rid) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "team tool source not found")
        return "team_tool_source", rid
    if resource_kind == "team_tool_op":
        parts = rid.split(".")
        if len(parts) >= 3 and parts[0] == "teammcp":
            source_id = parts[1]
            if await user_store.get_team_tool_source(user.team_id, source_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "team tool source not found")
            try:
                from bowhead_core.team_tool_loader import load_team_tool_entries

                entries, _by_id = await load_team_tool_entries(user.team_id, user_store)
                valid = {e.advertised_name for e in entries if e.source_id == source_id}
                if rid not in valid:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "team tool op not found")
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "team tool op not found") from exc
        elif not rid:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "team_tool_op id required")
        else:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "team_tool_op must be teammcp.<source_id>.<operation_id>",
            )
        return "team_tool_op", rid
    if resource_kind == "team_subagent":
        if await user_store.get_team_subagent(user.team_id, rid) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "team subagent not found")
        return "team_subagent", rid
    if resource_kind == "team_skill":
        if await user_store.get_team_skill(user.team_id, rid) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "team skill not found")
        return "team_skill", rid
    if resource_kind == "report":
        canvas_store = getattr(request.app.state, "canvas_store", None)
        if canvas_store is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "canvas store not configured")
        canvas = await canvas_store.get_canvas_by_session(rid)
        if canvas is None or not await user_store.workspace_in_team(
            user.team_id,
            canvas.workspace_id or "",
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
        return "report", rid
    return resource_kind, rid


@router.post(
    "/groups",
    response_model=GroupDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_not_readonly)],
)
async def create_group(
    body: GroupCreateDTO,
    request: Request,
    user: User = Depends(_REQUIRE_ACCESS_ADMIN),
    access_store: AccessControlStore = Depends(_access_store),
    user_store: UserStore = Depends(_store),
) -> GroupDTO:
    try:
        group = await access_store.create_group(
            team_id=user.team_id,
            name=body.name,
            workspace_id=body.workspace_id,
            description=body.description,
            created_by=user.id,
        )
    except aiosqlite.IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "group already exists") from exc
    except ValueError as exc:
        code = str(exc)
        if code == "workspace_not_in_team":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found") from exc
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, code or "invalid group") from exc
    await _audit(
        request=request,
        store=user_store,
        user=user,
        action=AuditAction.ACCESS_GROUP_CREATED,
        target_kind="group",
        target_id=group.id,
        payload={"workspace_id": group.workspace_id, "name": group.name},
    )
    return _group_to_dto(group)


@router.get("/groups", response_model=GroupListDTO)
async def list_groups(
    workspace_id: str | None = None,
    user: User = Depends(_REQUIRE_ACCESS_ADMIN),
    access_store: AccessControlStore = Depends(_access_store),
) -> GroupListDTO:
    groups = await access_store.list_groups(user.team_id, workspace_id=workspace_id)
    return GroupListDTO(items=[_group_to_dto(g) for g in groups])


@router.get("/groups/{group_id}", response_model=GroupDTO)
async def get_group(
    group_id: str,
    user: User = Depends(_REQUIRE_ACCESS_ADMIN),
    access_store: AccessControlStore = Depends(_access_store),
) -> GroupDTO:
    group = await access_store.get_group(user.team_id, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "group not found")
    return _group_to_dto(group)


@router.get("/groups/{group_id}/members", response_model=GroupMemberListDTO)
async def list_group_members(
    group_id: str,
    user: User = Depends(_REQUIRE_ACCESS_ADMIN),
    access_store: AccessControlStore = Depends(_access_store),
) -> GroupMemberListDTO:
    if await access_store.get_group(user.team_id, group_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "group not found")
    members = await access_store.list_group_members(user.team_id, group_id)
    return GroupMemberListDTO(items=[_member_to_dto(m) for m in members])


@router.post(
    "/groups/{group_id}/members",
    response_model=GroupMemberDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_not_readonly)],
)
async def add_group_member(
    group_id: str,
    body: GroupMemberAddDTO,
    request: Request,
    user: User = Depends(_REQUIRE_ACCESS_ADMIN),
    access_store: AccessControlStore = Depends(_access_store),
    user_store: UserStore = Depends(_store),
) -> GroupMemberDTO:
    try:
        member = await access_store.add_group_member(
            team_id=user.team_id,
            group_id=group_id,
            user_id=body.user_id,
            role=body.role,
            created_by=user.id,
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "group not found") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await _audit(
        request=request,
        store=user_store,
        user=user,
        action=AuditAction.ACCESS_GROUP_MEMBER_ADDED,
        target_kind="group",
        target_id=group_id,
        payload={"user_id": body.user_id, "role": body.role},
    )
    return _member_to_dto(member)


@router.delete(
    "/groups/{group_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_not_readonly)],
)
async def remove_group_member(
    group_id: str,
    user_id: str,
    request: Request,
    user: User = Depends(_REQUIRE_ACCESS_ADMIN),
    access_store: AccessControlStore = Depends(_access_store),
    user_store: UserStore = Depends(_store),
) -> None:
    removed = await access_store.remove_group_member(user.team_id, group_id, user_id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "group member not found")
    await _audit(
        request=request,
        store=user_store,
        user=user,
        action=AuditAction.ACCESS_GROUP_MEMBER_REMOVED,
        target_kind="group",
        target_id=group_id,
        payload={"user_id": user_id},
    )


@router.post(
    "/resource-grants",
    response_model=ResourceGrantDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_not_readonly)],
)
async def upsert_resource_grant(
    body: ResourceGrantCreateDTO,
    request: Request,
    user: User = Depends(_REQUIRE_ACCESS_ADMIN),
    access_store: AccessControlStore = Depends(_access_store),
    user_store: UserStore = Depends(_store),
) -> ResourceGrantDTO:
    resource_kind, resource_id = await _canonical_resource(
        request=request,
        user=user,
        user_store=user_store,
        kind=body.resource_kind,
        resource_id=body.resource_id,
    )
    _reject_obsolete_team_subagent_write(resource_kind)
    permission = canonical_permission(body.permission, resource_kind=resource_kind)
    if permission is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid permission")
    await _validate_principal(
        user=user,
        user_store=user_store,
        access_store=access_store,
        principal_type=body.principal_type,
        principal_id=body.principal_id,
    )
    # content-permissions write-through convergence (tech-design §2.3): an admin
    # creating the team-principal ALLOW grant of a shareable kind == publishing it
    # team_wide — route through set_audience so visibility stays in sync (single
    # write entry; permission is forced to the kind's wave-1 level).
    if resource_kind in SHAREABLE_KINDS and permission == SHAREABLE_KINDS[resource_kind] and body.principal_type == "team" and body.effect == "allow":
        try:
            result = await share_service.set_audience(
                access_store,
                kind=resource_kind,
                resource_id=resource_id,
                team_id=user.team_id,
                target="team_wide",
                actor_id=user.id,
                # skill-team-governance fourth gate: publishing team_wide from the admin grants console also uses
                # set_audience's approval gate; pending/rejected team Skills -> 422.
                user_store=user_store,
            )
        except share_service.SkillApprovalRequiredError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {"error": {"code": share_service.SkillApprovalRequiredError.code, "message": "team skill not approved"}},
            ) from None
        grant = await access_store.get_resource_grant(user.team_id, result["team_grant_id"])
        if grant is None:  # pragma: no cover — set_audience just created it
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "grant upsert failed")
        await _audit(
            request=request,
            store=user_store,
            user=user,
            action=AuditAction.RESOURCE_GRANT_UPSERTED,
            target_kind="resource_grant",
            target_id=grant.id,
            payload={
                "resource_kind": resource_kind,
                "resource_id": resource_id,
                "principal_type": "team",
                "principal_id": body.principal_id,
                "permission": grant.permission,
                "requested_permission": body.permission,
                "effect": "allow",
                "via": "admin_api",
                "audience_transition": "team_wide",
            },
        )
        # wave 2: admin published team_wide → stale all pending access requests (report-only).
        if resource_kind == "report":
            from bowhead_gateway.routes.access_requests import cascade_requests_for_resource

            await cascade_requests_for_resource(
                request.app, team_id=user.team_id, kind=resource_kind, resource_id=resource_id, decided_by=user.id, gone=False
            )
        return _grant_to_dto(grant)
    # skill-team-governance: admin-console direct-store user/group ALLOW paths also gate pending/rejected
    # team Skills (same checkpoint as share_service.add_grant; deny rows tighten access and are allowed).
    if resource_kind == "team_skill" and body.effect == "allow":
        try:
            await share_service.ensure_team_skill_grantable(user_store, team_id=user.team_id, resource_id=resource_id)
        except share_service.SkillApprovalRequiredError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {"error": {"code": share_service.SkillApprovalRequiredError.code, "message": "team skill not approved"}},
            ) from None
    grant = await access_store.create_or_update_resource_grant(
        team_id=user.team_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        principal_type=body.principal_type,
        principal_id=body.principal_id,
        permission=permission,
        effect=body.effect,
        created_by=user.id,
        expires_at=_validate_expires_at(body.expires_at),
    )
    await _audit(
        request=request,
        store=user_store,
        user=user,
        action=AuditAction.RESOURCE_GRANT_UPSERTED,
        target_kind="resource_grant",
        target_id=grant.id,
        payload={
            "resource_kind": resource_kind,
            "resource_id": resource_id,
            "principal_type": body.principal_type,
            "principal_id": body.principal_id,
            "permission": permission,
            "requested_permission": body.permission,
            "effect": body.effect,
        },
    )
    # wave 2: an admin granting a user report-read auto-resolves their pending request.
    if resource_kind == "report" and body.principal_type == "user" and body.effect == "allow":
        from bowhead_gateway.routes.access_requests import resolve_request_after_grant

        await resolve_request_after_grant(
            request.app, team_id=user.team_id, kind=resource_kind, resource_id=resource_id, requester_id=body.principal_id, decided_by=user.id
        )
    return _grant_to_dto(grant)


@router.get("/resource-grants", response_model=ResourceGrantListDTO)
async def list_resource_grants(
    resource_kind: str | None = None,
    resource_id: str | None = None,
    user: User = Depends(_REQUIRE_ACCESS_ADMIN),
    access_store: AccessControlStore = Depends(_access_store),
) -> ResourceGrantListDTO:
    grants = await access_store.list_resource_grants(
        user.team_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )
    return ResourceGrantListDTO(items=[_grant_to_dto(g) for g in grants])


@router.delete(
    "/resource-grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_not_readonly)],
)
async def revoke_resource_grant(
    grant_id: str,
    request: Request,
    user: User = Depends(_REQUIRE_ACCESS_ADMIN),
    access_store: AccessControlStore = Depends(_access_store),
    user_store: UserStore = Depends(_store),
) -> None:
    # content-permissions write-through convergence (tech-design §2.3): revoking the
    # team-principal ALLOW grant of a shareable kind == leaving team_wide — route it
    # through set_audience so the visibility cache can never drift into an unrevokable state.
    existing = await access_store.get_resource_grant(user.team_id, grant_id)
    if existing is not None:
        _reject_obsolete_team_subagent_write(existing.resource_kind)
    if (
        existing is not None
        and existing.revoked_at is None
        and existing.resource_kind in SHAREABLE_KINDS
        and existing.principal_type == "team"
        and existing.effect == "allow"
    ):
        result = await share_service.set_audience(
            access_store,
            kind=existing.resource_kind,
            resource_id=existing.resource_id,
            team_id=user.team_id,
            target="custom",
            actor_id=user.id,
        )
        for rid_ in result["revoked_grant_ids"]:
            await _audit(
                request=request,
                store=user_store,
                user=user,
                action=AuditAction.RESOURCE_GRANT_REVOKED,
                target_kind="resource_grant",
                target_id=rid_,
                payload={"via": "admin_api", "audience_transition": "custom"},
            )
        return
    revoked = await access_store.revoke_resource_grant(user.team_id, grant_id)
    if not revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "grant not found")
    await _audit(
        request=request,
        store=user_store,
        user=user,
        action=AuditAction.RESOURCE_GRANT_REVOKED,
        target_kind="resource_grant",
        target_id=grant_id,
        payload={},
    )


@router.post("/permissions/explain", response_model=PermissionExplainResultDTO)
async def explain_permission(
    body: PermissionExplainDTO,
    request: Request,
    user: User = Depends(_current_user),
    access_store: AccessControlStore = Depends(_access_store),
    user_store: UserStore = Depends(_store),
) -> PermissionExplainResultDTO:
    is_admin = await _is_access_admin(user, user_store)
    if is_admin:
        resource_kind, resource_id = await _canonical_resource(
            request=request,
            user=user,
            user_store=user_store,
            kind=body.resource_kind,
            resource_id=body.resource_id,
        )
    else:
        resource_kind, resource_id = _canonical_self_explain_resource(
            user=user,
            workspace_id=body.workspace_id,
            kind=body.resource_kind,
            resource_id=body.resource_id,
        )
    permission = canonical_permission(body.permission, resource_kind=resource_kind)
    if permission is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid permission")
    ctx = await build_access_context_from_request(
        request,
        user=user,
        workspace_id=body.workspace_id,
    )
    decision = await access_store.explain_decision(
        ctx,
        resource_kind,
        resource_id,
        permission,
    )
    if not is_admin:
        return PermissionExplainResultDTO(
            allowed=bool(decision["allowed"]),
            reason=_sanitize_self_explain_reason(decision),
            shadow=bool(decision.get("shadow", False)),
            grant_id=None,
            access_context={
                "user_id": ctx.user_id,
                "workspace_id": ctx.workspace_id,
                "workspace_role": ctx.workspace_role,
                "is_team_admin": ctx.is_team_admin,
            },
        )
    return PermissionExplainResultDTO(
        allowed=bool(decision["allowed"]),
        reason=str(decision["reason"]),
        shadow=bool(decision.get("shadow", False)),
        grant_id=decision.get("grant_id"),
        access_context=access_context_to_dict(ctx) or {},
    )
