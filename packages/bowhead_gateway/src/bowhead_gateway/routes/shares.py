"""Share API — user-initiated content sharing (content-permissions wave 1).

Behavior: docs/sprints/content-permissions/prd.md §5.1; implementation:
tech-design.md §3. Owner-facing share management for the SHAREABLE_KINDS
(team_subagent / team_skill / report) — a thin HTTP layer over
``bowhead_core.share_service``; admin's ``/api/v1/resource-grants`` console
stays separate but writes the same ``resource_grants`` table.

Mounted in main.py under ``content_sharing_enabled()`` (default ON). Per-kind
the capability assets additionally follow their creation flag (dark kind →
404, same as its CRUD routes). DTOs are ``extra="forbid"`` and carry NO
``effect`` / ``permission`` / ``expires_at`` fields: user-side shares are
allow-only at the kind's fixed permission; team_wide goes through the audience
endpoint ONLY (write-through invariant, §2.3).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.audit_actions import AuditAction
from bowhead_core.resource_access import SHAREABLE_KINDS
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.subagent_creator_trigger import subagent_creator_enabled
from bowhead_core.subagent_write_boundary import (
    TeamSubagentWriteBoundaryError,
    reject_obsolete_team_subagent_write,
)
from bowhead_core.workflow_creator_trigger import workflow_creator_enabled
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from bowhead_core import share_service
from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.access_context import (
    AccessContextError,
    build_access_context_from_request,
    require_space_lifecycle_without_authorization,
)
from bowhead_gateway.routes.auth import _current_user, _store

if TYPE_CHECKING:
    from bowhead_core.access_control import AccessControlStore, ResourceGrantRecord
    from bowhead_core.share_service import ResolvedShareable
    from bowhead_core.user_store import User, UserStore

router = APIRouter()


def _access_store(request: Request) -> AccessControlStore:
    store = getattr(request.app.state, "access_control_store", None)
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "access control store not configured")
    return store


def _skill_approval_422(request: Request) -> HTTPException:
    """skill-team-governance：share_service 审批闸 raise → 422 coded（批 A 顶层 error 形状）."""
    code = share_service.SkillApprovalRequiredError.code
    msg = code
    catalog = getattr(request.app.state, "error_catalog", None)
    if catalog is not None:
        with contextlib.suppress(Exception):
            from bowhead_core.error_catalog import DEFAULT_LANGUAGE

            msg = catalog.render(code, DEFAULT_LANGUAGE)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"error": {"code": code, "message": msg}})


def _reject_obsolete_team_subagent_write(resource_kind: str) -> None:
    try:
        reject_obsolete_team_subagent_write(resource_kind)
    except TeamSubagentWriteBoundaryError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "error": {
                    "code": exc.code,
                    "message": ("This Subagent sharing API is obsolete; use the Subagent management API."),
                }
            },
        ) from None


async def _is_team_admin(store: UserStore, user: User) -> bool:
    """get_team_role ∈ {owner, team_admin}（不用 can_manage_shares 的 owner 短路）——
    team_skill 的下架/降级是审批状态机的 admin-only 转移，不能由作者经 shares API 绕过。"""
    if not user.team_id:
        return False
    try:
        role = await store.get_team_role(user.id, user.team_id)
    except Exception:
        return False
    return role in ("owner", "team_admin")


async def _guard_team_skill_downgrade(store: UserStore, user: User, res: ResolvedShareable, target: str | None) -> None:
    """skill-team-governance（安全 review P2-C）：已批 team_wide 的 team_skill 下架/降级
    = admin-only（PRD §3.6 转移表「作者不得下架已批资产」）。shares API 的
    put_audience(custom/private) 与 revoke team-grant 是 team_skills.py PATCH 之外的第二/
    第三条降级路,owner 短路会让非 admin 作者绕过——此处收紧。creator_only 行的点名分享
    （visibility!=team_wide）不受影响（PRD residual 保留点名路）;elevation 由 set_audience 闸另管。"""
    if res.kind != "team_skill":
        return
    if (res.visibility or "") != "team_wide":
        return  # Not broadcast (creator_only/named recipients): authors manage shares freely.
    if target == "team_wide":
        return  # Separate gates handle idempotency/upgrades.
    if not await _is_team_admin(store, user):
        from bowhead_gateway.skill_workflow_errors import skill_workflow_http_error

        raise skill_workflow_http_error(status.HTTP_403_FORBIDDEN, "team_skill_audience_admin_required")


# ── DTOs (extra=forbid: the three excluded inputs are schema-level rejections) ──


class AudiencePutDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audience: Literal["private", "custom", "team_wide"]


class GrantCreateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principal_type: Literal["user", "group"]
    principal_id: str = Field(min_length=1, max_length=160)


# ── per-kind ownership adapters (tech-design §3 table) ──────────────────


async def _resolve_shareable(
    request: Request,
    user: User,
    store: UserStore,
    kind: str,
    rid: str,
) -> ResolvedShareable:
    """kind whitelist + existence + ownership + team containment. 404 hides
    cross-team/unknown resources (anti-enumeration); 409 = exists but unshareable.

    Thin wrapper over the non-raising ``share_service.resolve_shareable`` (shared
    with wave-2 access-requests). Resolution lives in the service; the caller-
    dependent 422/404/409/503 HTTP mapping stays here (we hold the request ``user``).
    """
    kind = (kind or "").strip()
    rid = (rid or "").strip()
    if kind not in SHAREABLE_KINDS or not rid:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unsupported kind")
    canvas_store = getattr(request.app.state, "canvas_store", None)
    if kind == "report" and canvas_store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "canvas store not configured")

    res = await share_service.resolve_shareable(
        kind=kind,
        resource_id=rid,
        team_id=user.team_id,
        user_store=store,
        canvas_store=canvas_store,
        subagent_enabled=subagent_creator_enabled(),
        workflow_enabled=workflow_creator_enabled(),
    )
    if res is not None:
        return res

    # None → reconstruct the wave-1 error code. report owner with an unresolvable
    # workspace gets explicit 409 (PRD: this report cannot be shared); all others 404 (anti-enumeration).
    if kind == "report":
        canvas = await canvas_store.get_canvas_by_session(rid)
        if canvas is not None and canvas.user_id == user.id and not await store.workspace_in_team(user.team_id, canvas.workspace_id or ""):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "report workspace cannot be resolved to your team; sharing unavailable",
            )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


async def _require_can_manage(store: UserStore, user: User, res: ResolvedShareable) -> None:
    if not await share_service.can_manage_shares(store, user, res.owner_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "only the owner or an team admin can manage shares for this resource",
        )


async def _require_share_lifecycle(
    request: Request,
    store: UserStore,
    user: User,
    res: ResolvedShareable,
    *,
    operation: SpaceOperation,
    enforce_space_membership: bool = True,
) -> None:
    """Reports are Space-bound; capability assets remain Team-bound.

    ``enforce_space_membership=False``：调用方不是靠空间成员资格进来的（被分享人 /
    ``team_wide`` 可见的团队成员，见 ``get_shares`` 的 recipient 分支）。那一档归**批4**
    （tech-design §4.4「分享按空间级」+ PRD §5.2/§5.6），批2 不顺手收紧。
    """

    if res.kind != "report" or not res.workspace_id:
        return
    if enforce_space_membership:
        # Batch 2 (§4.2): check space membership for active spaces too.
        # ★ allow_team_admin_override preserves an existing path, without adding privileges:
        # share_service.can_manage_shares (owner ∨ same-team owner/team_admin) lets admins manage
        # team resources' shares without space membership, matching existing PRD §5.5 option A semantics.
        # ★ Reject via the unified entry's 403 ws_access_denied:* codes, same family as the archived branch below
        # (D3: shares retains existing codes).
        #
        # ★★ **No archived exemptions** (2026-09-17 additional review P1-1): this branch returns directly,
        # skipping archived validation below, which **has no override**. Unconditional exemptions here would change
        # same-team admins removed from an archived space from 403 to **200**.
        # turns.py avoids this because archived rejection runs **before** membership validation;
        # allow_team_admin_override=not archived enforces the same invariant here:
        # **Archiving can only narrow access.**
        workspace = await store.get_workspace(res.workspace_id)
        archived = workspace is not None and str(workspace.get("status") or "") == "archived"
        await build_access_context_from_request(
            request,
            user=user,
            workspace_id=res.workspace_id,
            operation=operation,
            allow_team_admin_override=not archived,
        )
        return
    try:
        await require_space_lifecycle_without_authorization(
            user_store=store,
            workspace_id=res.workspace_id,
            operation=operation,
        )
    except AccessContextError as exc:
        raise HTTPException(
            exc.status_code,
            {"error": {"code": exc.code}},
        ) from exc

    if operation is SpaceOperation.READ_HISTORY:
        workspace = await store.get_workspace(res.workspace_id)
        if workspace is not None and str(workspace.get("status") or "") == "archived":
            await build_access_context_from_request(
                request,
                user=user,
                workspace_id=res.workspace_id,
                operation=operation,
            )


def _grant_dto(g: ResourceGrantRecord, name: str | None = None) -> dict[str, Any]:
    return {
        "id": g.id,
        "principal_type": g.principal_type,
        "principal_id": g.principal_id,
        # 0708 ⑤: display name (user/group name). None → FE falls back to raw principal_id.
        "principal_name": name,
        "permission": g.permission,
        "created_by": g.created_by,
        "created_at": g.created_at,
    }


async def _principal_name_map(
    store: UserStore,
    access_store: AccessControlStore,
    team_id: str,
    grants: list[ResourceGrantRecord],
    *extra_user_ids: str,
) -> dict[str, str]:
    """principal_id → display name for the share roster (0708 ⑤): users resolved via
    user_store, groups via access_store.list_groups, plus any extra user ids (e.g.
    owner). best-effort — unresolved ids are omitted so the FE falls back to the raw
    id, and enrichment never fails the roster."""
    names: dict[str, str] = {}
    user_ids = [g.principal_id for g in grants if g.principal_type == "user"]
    user_ids.extend(u for u in extra_user_ids if u)
    if user_ids:
        with contextlib.suppress(Exception):
            names.update(await store.get_users_by_ids(user_ids))
    if any(g.principal_type == "group" for g in grants):
        try:
            for grp in await access_store.list_groups(team_id):
                if getattr(grp, "name", None):
                    names[grp.id] = grp.name
        except Exception:  # noqa: S110
            pass
    return names


def _audience_of(res: ResolvedShareable, grants: list[ResourceGrantRecord], team_id: str) -> str:
    if res.visibility is not None:  # capability asset: visibility column is the cache
        if res.visibility == "team_wide":
            return "team_wide"
        return "custom" if any(g.effect == "allow" and g.principal_type in ("user", "group") for g in grants) else "private"
    return share_service.derive_audience(grants, team_id=team_id)


async def _audit(
    request: Request,
    store: UserStore,
    user: User,
    action: str,
    res: ResolvedShareable,
    payload: dict[str, Any],
) -> None:
    """Best-effort admin-audit row (never blocks the share op)."""
    try:
        await store.write_admin_audit_log(
            actor_user_id=user.id,
            team_id=user.team_id,
            action=action,
            target_kind=res.kind,
            target_id=res.resource_id,
            target_team_id=user.team_id,
            scope="team",
            payload={"via": "share_api", "label": res.label, **payload},
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:  # audit must not block the operation
        return


async def _pending_request_count(request: Request, user: User, res: ResolvedShareable) -> int:
    """Open access-requests for this resource (manager 'N 待处理' nudge). Best-effort:
    flag-gated, and any store hiccup degrades to 0 rather than failing the roster."""
    from bowhead_core.access_request_flags import access_requests_enabled

    if not access_requests_enabled():
        return 0
    acr_store = getattr(request.app.state, "access_request_store", None)
    if acr_store is None:
        return 0
    try:
        counts = await acr_store.count_pending_for_resources(
            team_id=user.team_id,
            kind=res.kind,
            resource_ids=[res.resource_id],
        )
    except Exception:
        return 0
    return counts.get(res.resource_id, 0)


# ── endpoints ───────────────────────────────────────────────────────────


@router.get("/{kind}/{rid}")
async def get_shares(
    kind: str,
    rid: str,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
    access_store: AccessControlStore = Depends(_access_store),
) -> dict[str, Any]:
    """Share state of one resource. Managers see the full roster; a recipient
    sees only the audience level + their own grant (info boundary, PRD §5.1)."""
    res = await _resolve_shareable(request, user, store, kind, rid)
    await _require_share_lifecycle(
        request,
        store,
        user,
        res,
        operation=SpaceOperation.READ_HISTORY,
        # Non-owner readers here may be recipients or team members with team_wide visibility (recipient branch below);
        # they do not enter through space membership, so belong to batch 4 (§4.4).
        # Owners managing their own shares after removal from the space is exactly the path PRD §5.1 must close.
        enforce_space_membership=res.owner_id == user.id,
    )
    grants = await access_store.list_resource_grants(
        user.team_id,
        resource_kind=res.kind,
        resource_id=res.resource_id,
    )
    allow_grants = [g for g in grants if g.effect == "allow"]
    audience = _audience_of(res, allow_grants, user.team_id)
    can_manage = await share_service.can_manage_shares(store, user, res.owner_id)
    base: dict[str, Any] = {
        "kind": res.kind,
        "resource_id": res.resource_id,
        "label": res.label,
        "audience": audience,
        "owner_id": res.owner_id,
        "can_manage": can_manage,
    }
    if can_manage:
        # 0708 ⑤: enrich roster + owner with display names (was raw usr_*/group ids).
        names = await _principal_name_map(store, access_store, user.team_id, allow_grants, res.owner_id)
        base["grants"] = [_grant_dto(g, names.get(g.principal_id)) for g in allow_grants]
        base["owner_name"] = names.get(res.owner_id)
        # wave 2: surface the count of open access-requests so the manager view can
        # nudge "N 个访问申请待处理 →". Best-effort; never block the share roster.
        base["pending_requests"] = await _pending_request_count(request, user, res)
        return base
    # 0708 ⑤: recipient's read-only view shows "由 X 分享" — resolve owner name too.
    try:
        _owner_names = await store.get_users_by_ids([res.owner_id]) if res.owner_id else {}
    except Exception:
        _owner_names = {}
    base["owner_name"] = _owner_names.get(res.owner_id)
    group_ids = set(await access_store.principal_group_ids(user.team_id, user.id))
    mine = next(
        (
            g
            for g in allow_grants
            if (g.principal_type == "user" and g.principal_id == user.id) or (g.principal_type == "group" and g.principal_id in group_ids)
        ),
        None,
    )
    # skill-team-governance (security review P2-B): pending/rejected team_skill rows retain team_wide visibility
    # (write-through waits until approval). _audience_of checks visibility alone, so recipients falsely infer
    # universal visibility, leaking labels/owners. Fail closed for members reaching unapproved team_skill via recipients:
    # 404 (same zero-trace contract as /team-skills GET); managers (owner/admin) already returned above.
    if res.kind == "team_skill":
        try:
            await share_service.ensure_team_skill_grantable(store, team_id=user.team_id, resource_id=res.resource_id)
        except share_service.SkillApprovalRequiredError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found") from None
    # B1 anti-enumeration: neither manager nor recipient, without team_wide visibility → 404,
    # exposing no label/owner/audience/existence (same 404 contract as GET /team-subagents/{id}).
    if mine is None and audience != "team_wide":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    base["my_grant"] = _grant_dto(mine) if mine else None
    return base


@router.put("/{kind}/{rid}/audience", dependencies=[Depends(require_not_readonly)])
async def put_audience(
    kind: str,
    rid: str,
    body: AudiencePutDTO,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
    access_store: AccessControlStore = Depends(_access_store),
) -> dict[str, Any]:
    """Atomic audience transition per the §4.3 matrix (set_audience_tx)."""
    res = await _resolve_shareable(request, user, store, kind, rid)
    await _require_can_manage(store, user, res)
    await _require_share_lifecycle(
        request,
        store,
        user,
        res,
        operation=SpaceOperation.WRITE,
    )
    _reject_obsolete_team_subagent_write(res.kind)
    await _guard_team_skill_downgrade(store, user, res, body.audience)  # P2-C: approved team_wide downgrades are admin-only.
    try:
        result = await share_service.set_audience(
            access_store,
            kind=res.kind,
            resource_id=res.resource_id,
            team_id=user.team_id,
            target=body.audience,
            actor_id=user.id,
            # skill-team-governance gate 3: kind=team_skill ∧ target=team_wide triggers service-level
            # approval revalidation (inside set_audience); unapproved → 422.
            user_store=store,
        )
    except share_service.SkillApprovalRequiredError:
        raise _skill_approval_422(request) from None
    for revoked_id in result["revoked_grant_ids"]:
        await _audit(request, store, user, AuditAction.RESOURCE_GRANT_REVOKED, res, {"grant_id": revoked_id, "audience_transition": body.audience})
    if result["team_grant_id"]:
        await _audit(
            request,
            store,
            user,
            AuditAction.RESOURCE_GRANT_UPSERTED,
            res,
            {"grant_id": result["team_grant_id"], "principal_type": "team", "audience_transition": body.audience},
        )
    # wave 2: going team_wide grants everyone → stale all pending access requests.
    if body.audience == "team_wide":
        from bowhead_gateway.routes.access_requests import cascade_requests_for_resource

        await cascade_requests_for_resource(
            request.app, team_id=user.team_id, kind=res.kind, resource_id=res.resource_id, decided_by=user.id, gone=False
        )
    return {"kind": res.kind, "resource_id": res.resource_id, "audience": body.audience, "revoked": len(result["revoked_grant_ids"])}


@router.post("/{kind}/{rid}/grants", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_not_readonly)])
async def create_share_grant(
    kind: str,
    rid: str,
    body: GrantCreateDTO,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
    access_store: AccessControlStore = Depends(_access_store),
) -> dict[str, Any]:
    """Direct share to one user/group at the kind's fixed permission."""
    res = await _resolve_shareable(request, user, store, kind, rid)
    await _require_can_manage(store, user, res)
    await _require_share_lifecycle(
        request,
        store,
        user,
        res,
        operation=SpaceOperation.WRITE,
    )
    _reject_obsolete_team_subagent_write(res.kind)
    pid = body.principal_id.strip()
    if body.principal_type == "user":
        target = await store.get_user(pid)
        if target is None or target.team_id != user.team_id or target.status != "active":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "user not found in your team")
    else:
        group = await access_store.get_group(user.team_id, pid)
        if group is None or group.status != "active":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "group not found in your team")
    try:
        grant = await share_service.add_grant(
            access_store,
            kind=res.kind,
            resource_id=res.resource_id,
            team_id=user.team_id,
            principal_type=body.principal_type,
            principal_id=pid,
            actor_id=user.id,
            # skill-team-governance: deny named shares of pending/rejected team skills (gate inside add_grant).
            user_store=store,
        )
    except share_service.SkillApprovalRequiredError:
        raise _skill_approval_422(request) from None
    await _audit(
        request,
        store,
        user,
        AuditAction.RESOURCE_GRANT_UPSERTED,
        res,
        {"grant_id": grant.id, "principal_type": body.principal_type, "principal_id": pid, "permission": grant.permission},
    )
    await _notify_share(request, user=user, store=store, access_store=access_store, res=res, principal_type=body.principal_type, principal_id=pid)
    # wave 2: a direct user share auto-resolves that user's pending access request.
    if body.principal_type == "user":
        from bowhead_gateway.routes.access_requests import resolve_request_after_grant

        await resolve_request_after_grant(
            request.app, team_id=user.team_id, kind=res.kind, resource_id=res.resource_id, requester_id=pid, decided_by=user.id
        )
    return _grant_dto(grant)


_SHARE_ACTION_URL = {
    "report": "/?session={rid}",
    "team_subagent": "/settings/subagents",
    "team_skill": "/skills/",
}


async def _notify_share(
    request: Request,
    *,
    user: User,
    store: UserStore,
    access_store: AccessControlStore,
    res: ResolvedShareable,
    principal_type: str,
    principal_id: str,
) -> None:
    """direct 分享通知（PRD §5.4：仅 direct;team_wide 切换不推送;撤销不推送）。
    group → 展开当前 active 成员（双过滤:membership active ∧ user active）;
    后入组不补发。best-effort,失败吞掉（与审计同策略）。"""
    try:
        ns = getattr(request.app.state, "notification_store", None)
        if ns is None:
            return
        if principal_type == "user":
            recipients = [principal_id]
        else:
            members = await access_store.list_group_members(user.team_id, principal_id)
            recipients = []
            for m in members:
                if getattr(m, "status", "") != "active":
                    continue
                u = await store.get_user(m.principal_id)
                if u is not None and u.status == "active":
                    recipients.append(u.id)
        recipients = [r for r in recipients if r != user.id]
        if not recipients:
            return
        item = await ns.upsert_item(
            team_id=user.team_id,
            category="share",
            type="content_shared",
            priority="normal",
            title=f"{user.display_name or user.email} 分享了「{res.label}」",
            body=None,
            subject_type=res.kind,
            subject_id=res.resource_id,
            actor_user_id=user.id,
            action_url=_SHARE_ACTION_URL[res.kind].format(rid=res.resource_id),
            actions=None,
            metadata={
                "kind": res.kind,
                "resource_id": res.resource_id,
                "actorName": user.display_name or user.email,
                "resourceLabel": res.label,
            },
            dedupe_key=f"share:{res.kind}:{res.resource_id}",
        )
        await ns.add_recipients(item.id, [(r, None) for r in recipients])
    except Exception:  # Notification failures do not block sharing.
        return


# ── Principal picker (tech-design §5): minimal directory readable by members ──
# Separate router, mounted at /api/v1/team in main.py (same content_sharing flag as shares).
# Admin GET /groups (access_control.py, admin-gated) stays unchanged.

directory_router = APIRouter()


@directory_router.get("/directory")
async def team_directory(
    q: str | None = None,
    cursor: int = 0,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """同 team active 用户最小目录(id/display_name/email 掩码),分页 50。"""
    if not user.team_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no team")
    items = await store.list_team_directory(user.team_id, q=q, limit=50, offset=max(0, cursor))
    return {"items": items, "next_cursor": (cursor + 50) if len(items) == 50 else None}


@directory_router.get("/groups-directory")
async def team_groups_directory(
    request: Request,
    user: User = Depends(_current_user),
    access_store: AccessControlStore = Depends(_access_store),
) -> dict[str, Any]:
    """active groups 的 {id, name, member_count}(member 可读;成员由 admin 维护,
    分享给组=信任其后续成员变化,share 弹层明示 — PRD §5.2)。"""
    if not user.team_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no team")
    groups = await access_store.list_groups(user.team_id)
    return {"items": [{"id": g.id, "name": g.name, "member_count": g.member_count} for g in groups if g.status == "active"]}


@router.delete("/{kind}/{rid}/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_not_readonly)])
async def revoke_share_grant(
    kind: str,
    rid: str,
    grant_id: str,
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
    access_store: AccessControlStore = Depends(_access_store),
) -> None:
    res = await _resolve_shareable(request, user, store, kind, rid)
    await _require_can_manage(store, user, res)
    await _require_share_lifecycle(
        request,
        store,
        user,
        res,
        operation=SpaceOperation.WRITE,
    )
    _reject_obsolete_team_subagent_write(res.kind)
    grant = await access_store.get_resource_grant(user.team_id, grant_id)
    if grant is None or grant.resource_kind != res.kind or grant.resource_id != res.resource_id or grant.revoked_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "grant not found")
    if grant.principal_type == "team":
        # P2-C: deleting a team-principal grant unpublishes an approved team_wide skill (set_audience(custom)
        # writes through visibility). team_skill unpublishing is admin-only, preventing author bypass via owner short-circuiting.
        await _guard_team_skill_downgrade(store, user, res, "custom")
        # Never delete team_wide's team-principal grant directly (leaves dirty team_wide visibility);
        # use set_audience(custom) to remove team grant + write through visibility consistently in one transaction.
        await share_service.set_audience(
            access_store,
            kind=res.kind,
            resource_id=res.resource_id,
            team_id=user.team_id,
            target="custom",
            actor_id=user.id,
        )
    else:
        await share_service.remove_grant(
            access_store,
            kind=res.kind,
            team_id=user.team_id,
            grant_id=grant_id,
        )
    await _audit(
        request,
        store,
        user,
        AuditAction.RESOURCE_GRANT_REVOKED,
        res,
        {"grant_id": grant_id, "principal_type": grant.principal_type, "principal_id": grant.principal_id},
    )
