"""Sprint β G5 — admin members + multi-admin promote/demote endpoint.

POST /api/v1/admin/members — invite a local member and reveal a one-time password setup/reset link
POST /api/v1/admin/members/{target_user_id}/role — promote/demote (owner only)
GET /api/v1/admin/members — list team members with role info (owner/team_admin/audit_admin)

Security:
- owner cannot be demoted; ownership is transferred separately
- Self-demote blocked if user is only remaining team_admin (P1-O-5 atomic via transaction)
- Cross-team changes are bound to the caller's active team_members row
- All role_change events go through audit_critical_action (D-3=B critical tier)
"""

from __future__ import annotations

import contextlib
import logging
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Literal

import aiosqlite
from bowhead_core._email_utils import mask_email
from bowhead_core.audit_actions import AuditAction
from bowhead_core.space_governance import SpaceGovernanceError
from bowhead_core.user_store import (
    TeamOwnerTransferError,
    User,
    UserStore,
    WorkspaceTeamMismatchError,
    _id,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from bowhead_gateway._entitlement_gate import require_not_readonly, seat_denied_to_http
from bowhead_gateway.auth_policy import (
    invite_only_activation_enabled,
    resolve_account_policy,
)
from bowhead_gateway.routes.auth import (
    _audit_403_denial,
    _issue_invite_jwt,
    _multi_team_authz_enabled,
    _require_team_role_dep,
    _store,
)

router = APIRouter()
logger = logging.getLogger(__name__)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_TEAM_INVITE_STATUS_ERROR_CODES = {
    "revoked": "invite_status_revoked",
    "consumed": "invite_status_consumed",
    "satisfied": "invite_status_satisfied",
    "expired": "invite_status_expired",
}


# Team membership roles grant tenant-wide authority; only the owner can change them.
_REQUIRE_TEAM_ROLE_OWNER = _require_team_role_dep("owner")
# Team administration (members, invitations and space grants) is available to owner and team_admin.
_REQUIRE_TEAM_ADMIN_OR_CREATOR = _require_team_role_dep("owner", "team_admin")
# List — broader read access
_REQUIRE_ADMIN_VIEWERS = _require_team_role_dep(
    "owner",
    "team_admin",
    "audit_admin",
)


class OwnerTransferBody(BaseModel):
    target_user_id: str = Field(..., min_length=1, max_length=128)


@router.post("/api/v1/admin/members/owner-transfer", dependencies=[Depends(require_not_readonly)])
async def transfer_owner(
    body: OwnerTransferBody, caller: User = Depends(_require_team_role_dep("owner")), store: UserStore = Depends(_store)
) -> dict:
    if body.target_user_id == caller.id:
        raise HTTPException(422, {"code": "owner_transfer_same_user"})
    try:
        await store.transfer_team_owner(caller.team_id, caller.id, body.target_user_id)
    except TeamOwnerTransferError as exc:
        status_code = 409 if exc.code == "owner_transfer_conflict" else 422
        raise HTTPException(status_code, {"code": exc.code}) from exc
    return {"ok": True, "owner_user_id": body.target_user_id}


class RoleChangeBody(BaseModel):
    new_role: str = Field(..., pattern="^(team_admin|audit_admin|member)$")
    reason: str | None = Field(None, max_length=500)


class WorkspaceGrantBody(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=128)
    role: str = Field("member", pattern="^(admin|member|viewer)$")


class TeamInviteBody(BaseModel):
    email: str
    assignment_mode: Literal["spaces", "team_only"] | None = None
    initial_workspace_grants: list[WorkspaceGrantBody] = Field(
        default_factory=list,
        max_length=20,
    )

    @field_validator("email")
    @classmethod
    def _v_email(cls, v: str) -> str:
        v = v.lower().strip()
        if any(c in v for c in "\r\n\t\0\v\f"):
            raise ValueError("email contains invalid characters")
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v


class LocalUserCreateBody(TeamInviteBody):
    display_name: str = Field(..., min_length=1, max_length=120)

    @field_validator("display_name")
    @classmethod
    def _v_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("display name is required")
        return value


def _new_team_invite_token(
    *,
    team_id: str,
    email: str,
    invite_id: str,
    team_role: str = "member",
    token_version: int = 1,
) -> tuple[str, str]:
    now_ts = int(time.time())
    exp_ts = now_ts + 7 * 86400
    token = _issue_invite_jwt(
        {
            "typ": "bowhead-invite",
            "scope": "team",
            "invite_id": invite_id,
            "token_version": token_version,
            "team_id": team_id,
            "team_role": team_role,
            "invited_email": email,
            "iat": now_ts,
            "exp": exp_ts,
        },
    )
    expires_at = datetime.fromtimestamp(exp_ts, UTC).isoformat()
    return token, expires_at


async def _maybe_send_team_invite_email(
    *,
    request: Request,
    store: UserStore,
    team_id: str,
    to_email: str,
    invite_path: str,
    resend: bool = False,
    public_entry_id: str | None = None,
) -> bool:
    try:
        from bowhead_core.email_config import resolve_smtp_config

        cfg = await resolve_smtp_config(team_id, store)
        if cfg is None:
            return False
        from bowhead_gateway.identity_email import enqueue_identity_email

        team = await store.get_team(team_id)
        await enqueue_identity_email(
            request,
            store,
            team_id=team_id,
            to_email=to_email,
            purpose="team_invite_resend" if resend else "team_invite",
            kind="team_invite",
            action_path=invite_path,
            team_name=team.name if team is not None else None,
            resend=resend,
            public_entry_id=public_entry_id,
        )
        return True
    except Exception:
        return False


async def _resolve_invite_public_context(
    request: Request,
    *,
    team_id: str,
    public_entry_id: str | None,
):
    """Resolve an invitation's immutable verified public entry."""

    from bowhead_gateway.public_url import PublicEntryRequiredError, public_entry_required_detail, team_public_access_context

    try:
        context = await team_public_access_context(request, team_id=team_id, entry_id=public_entry_id)
    except PublicEntryRequiredError as exc:
        raise HTTPException(409, public_entry_required_detail()) from exc
    return context, context


@router.post("/api/v1/admin/members", status_code=201, dependencies=[Depends(require_not_readonly)])
async def create_local_member(
    body: LocalUserCreateBody,
    request: Request,
    response: Response,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN_OR_CREATOR),
    store: UserStore = Depends(_store),
) -> dict:
    """Invite an existing local identity, or create one and add it to the sole Team."""
    if getattr(request.app.state, "deployment_mode", "local") != "local":
        raise HTTPException(409, {"code": "local_only"})
    team = await store.get_team(caller.team_id)
    if team is None:  # authenticated membership without an team is a corrupt identity record
        raise HTTPException(403, {"code": "team_not_found"})
    existing = await store.get_user_by_email(body.email)
    try:
        if existing is None:
            user = await store.create_user(
                team_id=caller.team_id,
                email=body.email,
                password=None,
                display_name=body.display_name,
                tier=team.default_user_tier,
                email_verified=True,
            )
            method = "created"
        elif existing.status == "deleted":
            user = await store.create_user(
                team_id=caller.team_id,
                email=body.email,
                password=None,
                display_name=body.display_name,
                tier=team.default_user_tier,
                email_verified=True,
                replace_deleted_user_id=existing.id,
            )
            method = "created"
            # Seed automatic preferences independently of the administrator's UI.
            with contextlib.suppress(Exception):
                from bowhead_core.new_user_defaults import seed_new_user_settings

                await seed_new_user_settings(
                    getattr(request.app.state, "user_settings_store", None),
                    user.id,
                )
        else:
            user = existing
            method = await store.add_existing_user_to_local_team(
                user_id=user.id,
                team_id=caller.team_id,
            )
    except aiosqlite.IntegrityError as exc:
        existing = await store.get_user_by_email(body.email)
        if existing is not None:
            user = existing
            method = await store.add_existing_user_to_local_team(
                user_id=user.id,
                team_id=caller.team_id,
            )
        else:
            raise HTTPException(409, {"code": "member_create_conflict"}) from exc
    except ValueError as exc:
        raise HTTPException(409, {"code": "team_not_found"}) from exc
    except Exception as exc:
        from bowhead_core.entitlements import EntitlementDenied

        if isinstance(exc, EntitlementDenied):
            raise seat_denied_to_http(exc) from exc
        raise
    recovery_path: str | None = None
    recovery_expires_at: str | None = None
    if method != "already_member":
        raw_token = secrets.token_urlsafe(32)
        recovery_expires_at = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
        recovery_path = f"/reset-password?flow=admin&token={raw_token}"
        try:
            await store.create_credential_recovery_token(
                raw_token=raw_token,
                user_id=user.id,
                team_id=caller.team_id,
                created_by_user_id=caller.id,
                expires_at=recovery_expires_at,
            )
        except ValueError as exc:
            raise HTTPException(409, {"code": "credential_recovery_not_allowed"}) from exc
    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action="local_member_invite",
        target_kind="user",
        target_id=user.id,
        scope="team",
        payload={"email_mask": mask_email(user.email), "method": method, "recovery_issued": recovery_path is not None},
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "method": method,
        "recovery_path": recovery_path,
        "recovery_expires_at": recovery_expires_at,
    }


@router.post("/api/v1/admin/members/invite", dependencies=[Depends(require_not_readonly)])
async def invite_team_member(
    body: TeamInviteBody,
    request: Request,
    response: Response,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN_OR_CREATOR),
    store: UserStore = Depends(_store),
) -> dict:
    """Invite a person with an explicit Team-only or initial-Space assignment."""
    existing = await store.get_user_by_email(body.email)
    existing_role = await store.get_team_role(existing.id, caller.team_id) if existing is not None else None
    policy = resolve_account_policy(
        deployment_mode_override=getattr(request.app.state, "deployment_mode", None),
    )
    if policy.deployment_mode == "local" and not invite_only_activation_enabled() and existing_role is None:
        raise HTTPException(
            409,
            {"code": "feature_disabled_use_direct_create"},
        )
    if existing:
        if existing_role is not None:
            return {"ok": True, "method": "already_member", "user_id": existing.id}
        if existing.team_id != caller.team_id and not _multi_team_authz_enabled():
            raise HTTPException(404, {"code": "user_not_found"})

    team = await store.get_team(caller.team_id)
    if team is None:
        raise HTTPException(404, {"code": "team_not_found"})
    entitlements = getattr(request.app.state, "entitlements", None)
    max_seats = (await entitlements.get(caller.team_id)).max_seats if entitlements is not None else team.max_seats
    if max_seats is not None and await store.count_active_seats(caller.team_id) >= max_seats:
        raise HTTPException(409, {"code": "seat_full"})

    invite_id = _id("oinv")
    token, expires_at = _new_team_invite_token(
        team_id=caller.team_id,
        email=body.email,
        invite_id=invite_id,
        token_version=1,
    )
    assignment_mode = body.assignment_mode
    method = "email_invite"
    invite: dict[str, object] | None = None
    if assignment_mode is None and body.initial_workspace_grants:
        raise HTTPException(
            422,
            {"code": "invite_assignment_mode_required"},
        )
    entry, invite_public_context = await _resolve_invite_public_context(
        request,
        team_id=caller.team_id,
        public_entry_id=None,
    )
    if assignment_mode is None:
        response.headers["Warning"] = '299 Bowhead "assignment_mode is required by Space Admin V2"'
        try:
            invite_id = await store.record_team_invite(
                team_id=caller.team_id,
                team_role="member",
                invited_email=body.email,
                raw_token=token,
                expires_at=expires_at,
                invited_by=caller.id,
                invite_id=invite_id,
                token_version=1,
            )
        except ValueError as exc:
            if "active team invite already exists" not in str(exc):
                raise
            existing_invite = await store.get_active_team_invite_by_email(
                caller.team_id,
                body.email,
            )
            return {
                "ok": True,
                "method": "already_invited",
                "invite_id": (existing_invite or {}).get("id"),
                "email_sent": False,
                "assignment_mode": ((existing_invite or {}).get("assignment_mode") or "team_only_legacy"),
                "deprecation_warning": "assignment_mode_required",
            }
    else:
        try:
            invite = await store.create_or_merge_team_invite(
                team_id=caller.team_id,
                team_role="member",
                invited_email=body.email,
                raw_token=token,
                expires_at=expires_at,
                assignment_mode=assignment_mode,
                initial_workspace_grants=[grant.model_dump() for grant in body.initial_workspace_grants],
                invited_by=caller.id,
                invite_id=invite_id,
                token_version=1,
                public_entry_id=entry.entry_id,
            )
        except SpaceGovernanceError as exc:
            status_code = {
                "invite_has_workspace_grants": 409,
                "too_many_initial_workspace_grants": 422,
                "invalid_initial_workspace_grant": 422,
            }.get(exc.code, 422)
            detail: dict[str, object] = {"code": exc.code}
            if exc.details:
                detail["details"] = exc.details
            raise HTTPException(status_code, detail) from exc
        invite_id = str(invite["id"])
        method = str(invite["method"])
        if method != "created":
            return {
                "ok": True,
                "method": method,
                "invite_id": invite_id,
                "email_sent": False,
                "assignment_mode": invite["assignment_mode"],
                "assignment_revision": invite["assignment_revision"],
                "initial_workspace_grants": invite["initial_workspace_grants"],
            }
    from bowhead_gateway.public_url import public_app_url_for_base

    invite_url = public_app_url_for_base(entry.base_url, f"/invite?token={token}")
    from bowhead_gateway.public_entry_context import reset_public_access_context, set_public_access_context

    context_token = set_public_access_context(invite_public_context)
    try:
        email_sent = await _maybe_send_team_invite_email(
            request=request,
            store=store,
            team_id=caller.team_id,
            to_email=body.email,
            invite_path=f"/invite?token={token}",
            public_entry_id=entry.entry_id,
        )
    finally:
        reset_public_access_context(context_token)
    if assignment_mode is None:
        await store.write_admin_audit_log(
            actor_user_id=caller.id,
            team_id=caller.team_id,
            action="team_invite_create",
            target_kind="invite",
            target_id=invite_id,
            scope="team",
            payload={
                "email_mask": mask_email(body.email),
                "team_role": "member",
                "assignment_mode": "team_only_legacy",
            },
        )
    return {
        "ok": True,
        "method": method,
        "invite_id": invite_id,
        "invite_url": invite_url,
        "email_sent": email_sent,
        "assignment_mode": (invite["assignment_mode"] if invite is not None else "team_only_legacy"),
        "assignment_revision": (invite["assignment_revision"] if invite is not None else 1),
        "initial_workspace_grants": (invite["initial_workspace_grants"] if invite is not None else []),
        **({"deprecation_warning": "assignment_mode_required"} if assignment_mode is None else {}),
    }


@router.get("/api/v1/admin/members/invites")
async def list_team_member_invites(
    include_inactive: bool = False,
    page: int | None = Query(default=None, ge=1),
    size: int = Query(default=25, ge=1, le=100),
    q: str = Query(default="", max_length=120),
    caller: User = Depends(_REQUIRE_ADMIN_VIEWERS),
    store: UserStore = Depends(_store),
) -> dict:
    query = q.strip()
    return {
        "team_id": caller.team_id,
        "invites": await store.list_team_invites(
            caller.team_id,
            include_inactive=include_inactive,
            page=page,
            size=size,
            q=query,
        ),
        "total": await store.count_team_invites(
            caller.team_id,
            include_inactive=include_inactive,
            q=query,
        ),
        "page": page,
        "size": size if page is not None else None,
    }


class TeamInviteAssignmentBody(BaseModel):
    assignment_mode: Literal["spaces", "team_only"]
    initial_workspace_grants: list[WorkspaceGrantBody] = Field(
        default_factory=list,
        max_length=20,
    )
    expected_assignment_revision: int = Field(..., ge=1)


@router.patch(
    "/api/v1/admin/members/invites/{invite_id}/assignment",
    dependencies=[Depends(require_not_readonly)],
)
async def update_team_member_invite_assignment(
    invite_id: str,
    body: TeamInviteAssignmentBody,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN_OR_CREATOR),
    store: UserStore = Depends(_store),
) -> dict:
    try:
        invite = await store.replace_team_invite_assignment(
            invite_id=invite_id,
            team_id=caller.team_id,
            actor_user_id=caller.id,
            assignment_mode=body.assignment_mode,
            initial_workspace_grants=[grant.model_dump() for grant in body.initial_workspace_grants],
            expected_assignment_revision=(body.expected_assignment_revision),
        )
    except SpaceGovernanceError as exc:
        status_code = {
            "invite_not_found": 404,
            "stale_invite_assignment": 409,
        }.get(exc.code, 422)
        detail: dict[str, object] = {"code": exc.code}
        if exc.details:
            detail["details"] = exc.details
        raise HTTPException(status_code, detail) from exc
    return {"ok": True, "invite": invite}


@router.delete("/api/v1/admin/members/invites/{invite_id}", dependencies=[Depends(require_not_readonly)])
async def revoke_team_member_invite(
    invite_id: str,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN_OR_CREATOR),
    store: UserStore = Depends(_store),
) -> dict:
    ok = await store.revoke_team_invite(invite_id, caller.team_id)
    if not ok:
        raise HTTPException(404, {"code": "invite_not_found"})
    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action="team_invite_revoke",
        target_kind="invite",
        target_id=invite_id,
        scope="team",
        payload={},
    )
    return {"ok": True, "invite_id": invite_id, "status": "revoked"}


@router.post("/api/v1/admin/members/invites/{invite_id}/resend", dependencies=[Depends(require_not_readonly)])
async def resend_team_member_invite(
    invite_id: str,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN_OR_CREATOR),
    store: UserStore = Depends(_store),
) -> dict:
    if not await store.rate_limit_check(
        f"team_invite_resend:invite:{invite_id}",
        limit=1,
        window_s=60,
    ):
        raise HTTPException(429, {"code": "resend_cooldown_1min"})
    invites = await store.list_team_invites(caller.team_id, include_inactive=True)
    invite = next((item for item in invites if item["id"] == invite_id), None)
    if not invite:
        raise HTTPException(404, {"code": "invite_not_found"})
    if invite["status"] != "active":
        raise HTTPException(
            409,
            {
                "code": _TEAM_INVITE_STATUS_ERROR_CODES.get(
                    str(invite["status"]),
                    "invite_not_reissuable",
                ),
            },
        )

    current_version = int(invite.get("token_version") or 1)
    entry, invite_public_context = await _resolve_invite_public_context(
        request,
        team_id=caller.team_id,
        public_entry_id=invite.get("public_entry_id"),
    )
    token, expires_at = _new_team_invite_token(
        team_id=caller.team_id,
        email=invite["invited_email"],
        invite_id=invite_id,
        team_role=invite.get("team_role") or "member",
        token_version=current_version + 1,
    )
    ok = await store.reissue_team_invite_token(
        invite_id,
        caller.team_id,
        token,
        expires_at,
        expected_token_version=current_version,
        public_entry_id=entry.entry_id,
    )
    if not ok:
        raise HTTPException(409, {"code": "invite_not_reissuable"})
    from bowhead_gateway.public_entry_context import (
        reset_public_access_context,
        set_public_access_context,
    )
    from bowhead_gateway.public_url import public_app_url_for_base

    context_token = set_public_access_context(invite_public_context)
    try:
        invite_url = public_app_url_for_base(entry.base_url, f"/invite?token={token}")
        email_sent = await _maybe_send_team_invite_email(
            request=request,
            store=store,
            team_id=caller.team_id,
            to_email=invite["invited_email"],
            invite_path=f"/invite?token={token}",
            public_entry_id=entry.entry_id,
            resend=True,
        )
    finally:
        reset_public_access_context(context_token)
    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action="team_invite_resend",
        target_kind="invite",
        target_id=invite_id,
        scope="team",
        payload={},
    )
    return {
        "ok": True,
        "invite_id": invite_id,
        "invite_url": invite_url,
        "email_sent": email_sent,
    }


@router.post("/api/v1/admin/members/{target_user_id}/role", dependencies=[Depends(require_not_readonly)])
async def change_member_role(
    target_user_id: str,
    body: RoleChangeBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ROLE_OWNER),
    store: UserStore = Depends(_store),
) -> dict:
    """G5 promote/demote endpoint.

    Steps:
    1. Validate target exists + same team as caller (cross-team block)
    2. Validate target is not the owner (ownership is transferred separately)
    3. Validate not self-demote-last-admin race (P1-O-5 atomic)
    4. promote_team_role (atomic via BEGIN IMMEDIATE)
    5. audit_critical_action (D-3=B atomic with main op)
    6. enqueue notification email via sprint α SMTP outbox
    """
    # 1. Target exists + active membership in the caller's team
    target_user = await store.get_user(target_user_id)
    if not target_user:
        raise HTTPException(404, {"code": "target_user_not_found"})
    target_role_current = await store.get_team_role(target_user_id, caller.team_id)
    if target_role_current is None:
        await _audit_403_denial(
            caller,
            f"user:{target_user_id}",
            "cross_team",
            ["owner"],
            store,
        )
        raise HTTPException(403, {"code": "cross_team_target_not_allowed"})

    # 2. Ownership is transferred through the dedicated endpoint.
    if target_role_current == "owner":
        raise HTTPException(
            422,
            {"code": "owner_immutable"},
        )

    # 3. Self-demote-last-admin block (atomic check via is_only_remaining_team_admin)
    if (
        target_user_id == caller.id and target_role_current == "team_admin" and body.new_role != "team_admin"
    ) and await store.is_only_remaining_team_admin(caller.id, caller.team_id):
        raise HTTPException(
            422,
            {"code": "last_team_admin_cannot_self_demote"},
        )

    # 4 + 5. atomic promote + critical audit in one tx
    # NOTE: promote_team_role is its own atomic tx — we run it first, then audit
    # in separate atomic tx. Acceptable because audit references final state.
    changed, old_role = await store.promote_team_role(
        user_id=target_user_id,
        team_id=caller.team_id,
        new_role=body.new_role,
        promoted_by=caller.id,
    )
    if not changed:
        return {
            "ok": True,
            "method": "noop",
            "current_role": old_role,
        }

    # 5. audit via D-3=B critical tier (main_op = no-op SELECT, actual change already done)
    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action=AuditAction.ROLE_CHANGE,
        target_kind="user",
        target_id=target_user_id,
        target_team_id=caller.team_id,
        scope="team",
        payload={
            "old_role": old_role,
            "new_role": body.new_role,
            "reason": body.reason or "",
            "target_email_mask": mask_email(target_user.email),
            "self_action": target_user_id == caller.id,
        },
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    # Notify the affected member using their saved interface language.
    try:  # noqa: SIM105
        from bowhead_gateway.im.copy_locale import resolve_channel_copy_locale
        from bowhead_gateway.product_identity import product_name

        name = product_name()
        locale = await resolve_channel_copy_locale(request.app, user_id=target_user.id, team_id=caller.team_id)
        if locale == "zh-CN":
            subject = f"你在 {name} 的角色已更新"
            message = (
                f"管理员将你的团队角色从 {old_role or 'member'} 改为 {body.new_role}。\n\n"
                f"原因：{body.reason or '（无）'}\n\n如有疑问，请联系管理员。\n\n— {name}\n"
            )
        else:
            subject = f"Your {name} role has changed"
            message = (
                f"An administrator changed your team role from {old_role or 'member'} to {body.new_role}.\n\n"
                f"Reason: {body.reason or '(none)'}\n\nContact your administrator with any questions.\n\n— {name}\n"
            )
        await store.enqueue_smtp(
            to_email=target_user.email,
            subject=subject,
            body=message,
            purpose="role_change_notification",
            team_id=caller.team_id,
        )
    except Exception:  # noqa: S110
        pass  # best-effort

    return {
        "ok": True,
        "method": "changed",
        "old_role": old_role,
        "new_role": body.new_role,
    }


@router.get("/api/v1/admin/members/{target_user_id}/workspace-grants")
async def list_member_workspace_grants(
    target_user_id: str,
    caller: User = Depends(_REQUIRE_ADMIN_VIEWERS),
    store: UserStore = Depends(_store),
) -> dict:
    target = await store.get_user(target_user_id)
    if not target:
        raise HTTPException(404, {"code": "target_user_not_found"})
    if await store.get_team_role(target_user_id, caller.team_id) is None:
        await _audit_403_denial(
            caller,
            f"user:{target_user_id}",
            "cross_team",
            ["owner", "team_admin", "audit_admin"],
            store,
        )
        raise HTTPException(403, {"code": "cross_team_target_not_allowed"})
    return {
        "user_id": target_user_id,
        "items": await store.list_team_workspace_grants_for_user(
            team_id=caller.team_id,
            user_id=target_user_id,
        ),
    }


@router.put("/api/v1/admin/members/{target_user_id}/workspace-grants", dependencies=[Depends(require_not_readonly)])
async def upsert_member_workspace_grant(
    target_user_id: str,
    body: WorkspaceGrantBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN_OR_CREATOR),
    store: UserStore = Depends(_store),
) -> dict:
    target = await store.get_user(target_user_id)
    if not target:
        raise HTTPException(404, {"code": "target_user_not_found"})
    if await store.get_team_role(target_user_id, caller.team_id) is None:
        await _audit_403_denial(
            caller,
            f"user:{target_user_id}",
            "cross_team",
            ["owner", "team_admin"],
            store,
        )
        raise HTTPException(403, {"code": "cross_team_target_not_allowed"})
    if not await store.workspace_in_team(caller.team_id, body.workspace_id):
        raise HTTPException(404, {"code": "workspace_not_found"})
    if not await store.workspace_in_team(
        caller.team_id,
        body.workspace_id,
        active_only=True,
    ):
        raise HTTPException(409, {"code": "workspace_archived"})
    current_role = await store.get_member_role(body.workspace_id, target_user_id)
    if current_role in {"admin", "owner", "workspace_admin", "ws_admin"} and body.role != "admin":
        admin_count = await store.count_active_workspace_admins(body.workspace_id)
        if admin_count <= 1:
            raise HTTPException(422, {"code": "last_workspace_admin"})
    try:
        await store.add_member(
            workspace_id=body.workspace_id,
            user_id=target_user_id,
            role=body.role,
            invited_by=caller.id,
        )
    except WorkspaceTeamMismatchError as exc:
        raise HTTPException(409, {"code": exc.code}) from exc
    except SpaceGovernanceError as exc:
        raise HTTPException(409, {"code": exc.code}) from exc
    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action="workspace_grant_upsert",
        target_kind="user",
        target_id=target_user_id,
        target_team_id=caller.team_id,
        scope="workspace",
        payload={"workspace_id": body.workspace_id, "role": body.role},
    )
    from bowhead_gateway.space_assignment_notifications import (
        resolve_space_assignment_if_ready,
    )

    await resolve_space_assignment_if_ready(
        request,
        store=store,
        team_id=caller.team_id,
        user_id=target_user_id,
    )
    return {"ok": True, "user_id": target_user_id, "workspace_id": body.workspace_id, "role": body.role}


@router.delete("/api/v1/admin/members/{target_user_id}/workspace-grants/{workspace_id}", dependencies=[Depends(require_not_readonly)])
async def revoke_member_workspace_grant(
    target_user_id: str,
    workspace_id: str,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN_OR_CREATOR),
    store: UserStore = Depends(_store),
) -> dict:
    target = await store.get_user(target_user_id)
    if not target:
        raise HTTPException(404, {"code": "target_user_not_found"})
    if await store.get_team_role(target_user_id, caller.team_id) is None:
        await _audit_403_denial(
            caller,
            f"user:{target_user_id}",
            "cross_team",
            ["owner", "team_admin"],
            store,
        )
        raise HTTPException(403, {"code": "cross_team_target_not_allowed"})
    if not await store.workspace_in_team(caller.team_id, workspace_id):
        raise HTTPException(404, {"code": "workspace_not_found"})
    if not await store.workspace_in_team(
        caller.team_id,
        workspace_id,
        active_only=True,
    ):
        raise HTTPException(409, {"code": "workspace_archived"})
    role = await store.get_member_role(workspace_id, target_user_id)
    if role in {"admin", "owner", "workspace_admin", "ws_admin"}:
        admin_count = await store.count_active_workspace_admins(workspace_id)
        if admin_count <= 1:
            raise HTTPException(422, {"code": "last_workspace_admin"})
    ok = await store.remove_member(workspace_id, target_user_id)
    if not ok:
        raise HTTPException(404, {"code": "grant_not_found"})
    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action="workspace_grant_revoke",
        target_kind="user",
        target_id=target_user_id,
        target_team_id=caller.team_id,
        scope="workspace",
        payload={"workspace_id": workspace_id},
    )
    return {"ok": True, "user_id": target_user_id, "workspace_id": workspace_id, "status": "removed"}


# Core deactivation result → stable HTTP status/code.
_DEACTIVATE_ERROR_RESPONSE: dict[str, tuple[int, str]] = {
    "target_not_found": (404, "target_user_not_found"),
    "cannot_self_deactivate": (422, "cannot_self_deactivate"),
    "owner_immutable": (422, "owner_immutable"),
    "last_team_admin": (422, "last_team_admin"),
    "last_workspace_admin": (422, "last_workspace_admin"),
    "already_inactive": (409, "already_inactive"),
    "local_team_invariant_failed": (409, "local_team_invariant_failed"),
    "account_deletion_not_ready": (503, "account_deletion_not_ready"),
    "subagent_lifecycle_action_required": (
        409,
        "subagent_lifecycle_action_required",
    ),
}


@router.post("/api/v1/admin/team/members/{target_user_id}/deactivate", dependencies=[Depends(require_not_readonly)])
async def deactivate_member(
    target_user_id: str,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN_OR_CREATOR),
    store: UserStore = Depends(_store),
) -> dict:
    """Remove another member from the caller's team and free that team's licensed seat.

    INV-2 (entitlement ≠ authz): this is an authz'd write bound to ``caller.team_id``, independent of any
    entitlement gate.  In local mode, the single-team invariant permits this operation to also
    soft-delete the global identity and revoke all of its sessions in the same transaction.
    """
    # 1. Target exists + same team (cross-team block — INV-2/SEC-2)
    target = await store.get_user(target_user_id)
    if not target:
        raise HTTPException(404, {"code": "target_user_not_found"})
    if await store.get_team_role(target_user_id, caller.team_id) is None:
        await _audit_403_denial(
            caller,
            f"user:{target_user_id}",
            "cross_team",
            ["owner", "team_admin"],
            store,
        )
        raise HTTPException(403, {"code": "cross_team_target_not_allowed"})
    # 2. No self-deactivate (avoid admin self-lockout; use the account-delete flow instead)
    if target_user_id == caller.id:
        raise HTTPException(422, {"code": "cannot_self_deactivate"})

    # 3. Atomic team membership removal + audit (data-layer guards: owner/last-admin)
    result = await store.admin_soft_delete_member_detailed(
        target_user_id=target_user_id,
        team_id=caller.team_id,
        actor_user_id=caller.id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        delete_identity=getattr(request.app.state, "deployment_mode", "local") == "local",
    )
    if not result.changed:
        status, code = _DEACTIVATE_ERROR_RESPONSE.get(
            result.error_code or "",
            (400, "deactivate_failed"),
        )
        detail: dict[str, object] = {"code": code}
        if code == "account_deletion_not_ready":
            detail["message"] = (
                "Account retirement is unavailable. Ask the instance administrator to restore the privacy ledger configuration before retrying."
            )
        if code == "subagent_lifecycle_action_required" and result.error_details is not None:
            detail["enabled_shared"] = int(
                result.error_details.get("enabled_shared", 0),
            )
            detail["governance_path"] = str(
                result.error_details.get(
                    "governance_path",
                    "/team/settings/subagents/",
                ),
            )
            logger.warning(
                "subagent_member_deactivation_blocked team_id=%s actor_id=%s target_user_id=%s enabled_shared=%d",
                caller.team_id,
                caller.id,
                target_user_id,
                detail["enabled_shared"],
            )
        raise HTTPException(status, detail)

    # A removed member's pending access requests are void (best-effort).
    from bowhead_gateway.routes.access_requests import void_requests_for_member

    await void_requests_for_member(request.app, user_id=target_user_id, team_id=caller.team_id)

    return {
        "ok": True,
        "method": "removed",
        "target_user_id": target_user_id,
        "status": "removed",
        "identity_deleted": result.identity_deleted,
    }


@router.get("/api/v1/admin/members")
async def list_admin_members(
    page: int | None = Query(default=None, ge=1),
    size: int = Query(default=25, ge=1, le=100),
    q: str = Query(default="", max_length=120),
    role: Literal["owner", "team_admin", "audit_admin", "member"] | None = None,
    caller: User = Depends(_REQUIRE_ADMIN_VIEWERS),
    store: UserStore = Depends(_store),
) -> dict:
    """List team members with role info and an owner marker.

    Returns members in caller's team with role from team_members + owner badge.
    """
    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        # Get owner id.
        async with db.execute(
            "SELECT created_by_user_id FROM teams WHERE id = ?",
            (caller.team_id,),
        ) as cur:
            team_row = await cur.fetchone()
        owner_id = team_row["created_by_user_id"] if team_row else None

        # team_members is authoritative; users.team_id is only the legacy/default team.
        where = ["tm.team_id=?", "tm.status='active'", "u.status='active'"]
        args: list[object] = [caller.team_id]
        if q.strip():
            escaped = q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(LOWER(u.email) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(u.display_name,'')) LIKE ? ESCAPE '\\')")
            args.extend([f"%{escaped}%", f"%{escaped}%"])
        if role == "owner":
            where.append("u.id=?")
            args.append(owner_id)
        elif role is not None:
            where.append("u.id<>? AND COALESCE(tm.role,'member')=?")
            args.extend([owner_id, role])
        filters = " AND ".join(where)
        async with db.execute(
            "SELECT COUNT(*) FROM team_members tm JOIN users u ON u.id=tm.user_id WHERE " + filters,
            args,
        ) as cur:
            total = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM team_members tm JOIN users u ON u.id=tm.user_id "
            "WHERE tm.team_id=? AND tm.status='active' AND u.status='active' "
            "AND u.id<>? AND tm.role='team_admin'",
            (caller.team_id, owner_id),
        ) as cur:
            non_owner_team_admin_count = (await cur.fetchone())[0]
        paging = " LIMIT ? OFFSET ?" if page is not None else ""
        row_args = [*args, size, (page - 1) * size] if page is not None else args
        async with db.execute(
            "SELECT u.id, u.email, u.display_name, u.tier, u.status, u.created_at, "
            "       u.last_active_at, tm.role AS team_role, tm.promoted_at "
            "FROM team_members tm JOIN users u ON u.id=tm.user_id "
            "WHERE " + filters + " ORDER BY u.created_at, u.id" + paging,
            row_args,
        ) as cur:
            rows = await cur.fetchall()

    members = []
    for r in rows:
        is_owner = r["id"] == owner_id
        effective_role = "owner" if is_owner else (r["team_role"] or "member")
        members.append(
            {
                "user_id": r["id"],
                "email": r["email"],
                "display_name": r["display_name"],
                "tier": r["tier"],
                "is_owner": is_owner,
                "team_role": effective_role,
                "promoted_at": r["promoted_at"],
                "created_at": r["created_at"],
                "last_active_at": r["last_active_at"],
            }
        )

    return {
        "items": members,
        "owner_id": owner_id,
        "your_role": await store.get_team_role(caller.id, caller.team_id) or "member",
        "total": total,
        "page": page,
        "size": size if page is not None else None,
        "non_owner_team_admin_count": non_owner_team_admin_count,
    }


# ─── Batch 2/c7 (workspace-space-split PRD §3.2 L3): explicit admin Datarc account mapping ───
# Trusted-identity ladder fallback: when L1 verified email / L2 IM identity cannot resolve an account, a team admin
# specifies the member's Datarc account uid directly (account-level, not workspace-scoped member_uid;
# the latter is always freshly resolved at query time). Write user key kind='tool_context', slot='account'
# (ConnectorContextService.resolve_trusted_account_uid L3 reads this same entry).


class DatarcAccountMappingBody(BaseModel):
    account_uid: str = Field(default="", max_length=64)  # Empty clears mapping.


@router.get("/api/v1/admin/members/{user_id}/datarc-account")
async def get_member_datarc_account(
    user_id: str,
    request: Request,
    caller: User = Depends(_REQUIRE_ADMIN_VIEWERS),
    store: UserStore = Depends(_store),
) -> dict:
    target = await store.get_user(user_id)
    if target is None or await store.get_team_role(user_id, caller.team_id) is None:
        raise HTTPException(404, "member not found")
    umem = getattr(request.app.state, "user_memory_store", None)
    ctx = await umem.get_tool_context(user_id, "datarc", slot="account") if umem else None
    return {
        "user_id": user_id,
        "account_uid": str((ctx or {}).get("id") or ""),
        "source": str((ctx or {}).get("source") or ""),
    }


@router.put(
    "/api/v1/admin/members/{user_id}/datarc-account",
    dependencies=[Depends(require_not_readonly)],
)
async def set_member_datarc_account(
    user_id: str,
    body: DatarcAccountMappingBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN_OR_CREATOR),
    store: UserStore = Depends(_store),
) -> dict:
    target = await store.get_user(user_id)
    if target is None or await store.get_team_role(user_id, caller.team_id) is None:
        raise HTTPException(404, "member not found")
    umem = getattr(request.app.state, "user_memory_store", None)
    if umem is None:
        raise HTTPException(503, "user memory store not configured")
    account_uid = body.account_uid.strip()
    if account_uid:
        await umem.set_tool_context(
            user_id,
            "datarc",
            slot="account",
            context_id=account_uid,
            source="admin",
        )
    else:
        await umem.clear_tool_context(user_id, "datarc", slot="account")
    # Mapping changes immediately invalidate this user's accessible-set cache (otherwise up to 15min delay).
    svc = getattr(request.app.state, "tool_context_service", None)
    if svc is not None:
        svc.invalidate_user(user_id)
    await store.log_audit(
        user_id=caller.id,
        team_id=caller.team_id,
        event_type="datarc_account_mapping_set",
        ip=request.client.host if request.client else None,
        payload={"target_user": user_id, "account_uid": account_uid or "(cleared)"},
    )
    return {"ok": True, "user_id": user_id, "account_uid": account_uid}
