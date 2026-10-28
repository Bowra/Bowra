"""P4.5.6 — Workspace member management.

GET    /api/v1/workspaces/{ws}/member-candidates    → list eligible Team members
POST   /api/v1/workspaces/{ws}/members              → add an existing Team member
PUT    /api/v1/workspaces/{ws}/members/{uid}/role   → admin change role
DELETE /api/v1/workspaces/{ws}/members/{uid}        → remove member
"""

from __future__ import annotations

import re
from datetime import UTC

from bowhead_core.space_governance import SpaceGovernanceError
from bowhead_core.user_store import (
    User,
    UserStore,
    WorkspaceTeamMismatchError,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

# Sprint α G9 (CTO P1-S-2 fix): email regex for InviteBody (prevent CRLF injection)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes.auth import (
    _current_user,
    _issue_invite_jwt,
    _require_space_admission,
    _require_space_governance_read,
    _require_workspace_admin,
)
from bowhead_gateway.space_assignment_notifications import resolve_space_assignment_if_ready

router = APIRouter()
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_INVITE_STATUS_ERROR_CODES = {
    "revoked": "invite_status_revoked",
    "consumed": "invite_status_consumed",
    "satisfied": "invite_status_satisfied",
    "expired": "invite_status_expired",
}


def _store(request: Request) -> UserStore:
    s = getattr(request.app.state, "user_store", None)
    if s is None:
        raise HTTPException(503, {"code": "member_service_unavailable"})
    return s


@router.get("/{workspace_id}/members")
async def list_members(
    workspace_id: str,
    user: User = Depends(_current_user),  # Sprint α G9: any ws member can list
    store: UserStore = Depends(_store),
) -> dict:
    # SP-AUTH-4 (terminology audit P1-5): match write paths (_require_workspace_role);
    # beyond membership, verify the space belongs to the caller's current team to prevent stale/cross-team roster enumeration.
    role = await store.get_member_role(workspace_id, user.id)
    if role is None or not await store.workspace_in_team(user.team_id, workspace_id):
        raise HTTPException(403, {"code": "not_workspace_member"})
    members = await store.list_members(workspace_id)
    return {"workspace_id": workspace_id, "members": members}


class AddMemberBody(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    role: str = Field("member", pattern="^(admin|member|viewer)$")


@router.get("/{workspace_id}/member-candidates")
async def list_member_candidates(
    workspace_id: str,
    caller: User = Depends(_require_space_admission),
    store: UserStore = Depends(_store),
) -> dict:
    return {
        "workspace_id": workspace_id,
        "items": await store.list_workspace_member_candidates(caller.team_id, workspace_id),
    }


@router.post("/{workspace_id}/members", dependencies=[Depends(require_not_readonly)])
async def add_member(
    workspace_id: str,
    body: AddMemberBody,
    request: Request,
    caller: User = Depends(_require_space_admission),
    store: UserStore = Depends(_store),
) -> dict:
    """Grant a Space role to an existing active Team member only."""
    try:
        await store.add_member(workspace_id=workspace_id, user_id=body.user_id, role=body.role)
    except WorkspaceTeamMismatchError as exc:
        raise HTTPException(409, {"code": exc.code}) from exc
    except SpaceGovernanceError as exc:
        raise HTTPException(409, {"code": exc.code}) from exc
    await resolve_space_assignment_if_ready(
        request,
        store=store,
        team_id=caller.team_id,
        user_id=body.user_id,
    )
    return {"ok": True, "user_id": body.user_id, "role": body.role}


@router.get("/{workspace_id}/invites")
async def list_invites(
    workspace_id: str,
    include_inactive: bool = False,
    user: User = Depends(_require_space_governance_read),
    store: UserStore = Depends(_store),
) -> dict:
    """P4.5.18: list active (or all) invite tokens for revoke UI."""
    items = await store.list_invites_for_workspace(
        workspace_id,
        include_inactive=include_inactive,
    )
    return {"workspace_id": workspace_id, "invites": items}


@router.delete("/{workspace_id}/invites/{invite_id}", dependencies=[Depends(require_not_readonly)])
async def revoke_invite(
    workspace_id: str,
    invite_id: str,
    user: User = Depends(_require_space_admission),  # SP-AUTH-5
    store: UserStore = Depends(_store),
) -> dict:
    """P4.5.18: revoke pending invite. 已 consumed / expired 不影响."""
    ok = await store.revoke_invite(invite_id)
    if not ok:
        raise HTTPException(404, {"code": "invite_not_found"})
    return {"ok": True, "invite_id": invite_id, "status": "revoked"}


# ─── Sprint β G1 — invites/check (7-state) + invites/{id}/resend ──


@router.post("/{workspace_id}/invites/{invite_id}/resend", dependencies=[Depends(require_not_readonly)])
async def resend_invite(
    workspace_id: str,
    invite_id: str,
    request: Request,
    user: User = Depends(_require_space_admission),  # SP-AUTH-5
    store: UserStore = Depends(_store),
) -> dict:
    """G1 resend invite. 1/min/invite cooldown via sprint α rate_limit_check.

    Re-enqueues via SMTP outbox (NOT BackgroundTasks per P1-T-12).
    """
    if not await store.rate_limit_check(
        f"invite_resend:invite:{invite_id}",
        limit=1,
        window_s=60,
    ):
        raise HTTPException(429, {"code": "resend_cooldown_1min"})

    # Lookup invite — we use list_invites_for_workspace to find by id
    invites = await store.list_invites_for_workspace(workspace_id, include_inactive=True)
    invite = next((i for i in invites if i["id"] == invite_id), None)
    if not invite:
        raise HTTPException(404, {"code": "invite_not_found"})
    if invite["status"] != "active":
        code = _INVITE_STATUS_ERROR_CODES.get(
            invite["status"],
            "invite_not_reissuable",
        )
        raise HTTPException(409, {"code": code})

    # email-config sprint (PRD P0-1): old resend URLs used ?invite_email= without a token or consumers on either side,
    # so invitees registered without joining the workspace. Remint a token + /invite?token=<jwt>
    # (same as creation). New JWTs carry invite_id, so resend rotates the stored hash
    # without making already-delivered, still-signed links fail before revoke/consume/expiry.
    import time as _time
    from datetime import datetime as _dt

    now_ts = int(_time.time())
    exp_ts = now_ts + 7 * 86400
    current_version = int(invite.get("token_version") or 1)
    new_token = _issue_invite_jwt(
        {
            "typ": "bowhead-invite",
            "scope": "workspace",
            "invite_id": invite_id,
            "token_version": current_version + 1,
            "workspace_id": workspace_id,
            "role": invite["role"],
            "invited_email": invite["invited_email"],
            "iat": now_ts,
            "exp": exp_ts,
        },
    )
    expires_at_iso = _dt.fromtimestamp(exp_ts, UTC).isoformat()
    from bowhead_gateway.public_url import (
        PublicEntryRequiredError,
        public_entry_required_detail,
        team_public_app_url,
    )

    try:
        invite_url = await team_public_app_url(
            request,
            team_id=user.team_id,
            entry_id=str(invite.get("public_entry_id") or "") or None,
            path=f"/invite?token={new_token}",
        )
    except PublicEntryRequiredError as exc:
        raise HTTPException(409, public_entry_required_detail()) from exc
    # review-fix (P1): enqueue only with SMTP configured; otherwise outbox entries are doomed, yet FE falsely shows Sent,
    # so admins skip copying links and invitees receive nothing. No config → email_enqueued=false → FE guides link copying.
    from bowhead_core.email_config import resolve_smtp_config

    cfg = await resolve_smtp_config(user.team_id, store)
    email_enqueued = cfg is not None
    if email_enqueued:
        from bowhead_gateway.identity_email import enqueue_rendered_identity_email, render_identity_email

        rendered = await render_identity_email(
            request,
            team_id=user.team_id,
            kind="workspace_invite",
            action_path=f"/invite?token={new_token}",
            inviter_name=user.display_name,
            resend=True,
            public_entry_id=str(invite.get("public_entry_id") or "") or None,
        )

    ok = await store.reissue_invite_token(
        invite_id,
        user.team_id,
        new_token,
        expires_at_iso,
        expected_token_version=current_version,
    )
    if not ok:
        raise HTTPException(409, {"code": "invite_not_reissuable"})

    if email_enqueued:
        await enqueue_rendered_identity_email(store, rendered, to_email=invite["invited_email"], purpose="invite_resend")

    await store.write_admin_audit_log(
        actor_user_id=user.id,
        team_id=user.team_id,
        action="invite_resend",
        target_kind="invite",
        target_id=invite_id,
        scope="workspace",
        payload={"workspace_id": workspace_id},
    )

    # Return invite_url for FE to display/copy immediately (mail may fail or enter spam; M3 completes the flow).
    return {"ok": True, "invite_id": invite_id, "resent": True, "invite_url": invite_url, "email_enqueued": email_enqueued}


@router.get("/{workspace_id}/check-invite-eligibility")
async def check_invite_eligibility(
    workspace_id: str,
    email: str,
    user: User = Depends(_require_space_admission),  # SP-AUTH-5
    store: UserStore = Depends(_store),
) -> dict:
    """G1 v9.2 F-6 7-state check.

    States:
    - empty: input empty
    - invalid: format invalid
    - member: email already in this workspace (with role)
    - invited: pending invite exists (with remaining_hours)
    - domain: domain in BOWHEAD_AUTO_MEMBER_DOMAINS whitelist — no invite needed
    - ok: clean, can invite (same-domain user not in ws)
    - ok-foreign: different domain — invite OK with warning
    """
    # Rate limit 60/min/user (P1-S-1 tightened)
    if not await store.rate_limit_check(
        f"invite_check:uid:{user.id}",
        limit=60,
        window_s=60,
    ):
        raise HTTPException(429, {"code": "too_many_checks"})

    if not email:
        return {"state": "empty"}
    email = email.lower().strip()
    if not _EMAIL_RE.match(email):
        return {"state": "invalid"}

    # Already member of THIS workspace?
    existing_user = await store.get_user_by_email(email)
    if existing_user:
        role = await store.get_member_role(workspace_id, existing_user.id)
        if role:
            return {"state": "member", "role": role}

    # Pending invite for THIS workspace + email?
    invites = await store.list_invites_for_workspace(workspace_id, include_inactive=False)
    for inv in invites:
        if inv["invited_email"] == email and inv["status"] == "active":
            from datetime import datetime

            try:
                expires = datetime.fromisoformat(inv["expires_at"])
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
                remaining_hours = int((expires - datetime.now(UTC)).total_seconds() / 3600)
            except (ValueError, TypeError):
                remaining_hours = None
            return {
                "state": "invited",
                "invite_id": inv["id"],
                "remaining_hours": remaining_hours,
            }

    # Whitelist domain (G6 interaction) — domain user will auto-join on register
    from bowhead_gateway.routes.auth import is_auto_member_email

    if is_auto_member_email(email):
        return {"state": "domain"}

    # Different domain than ws's team domain? (ok-foreign)
    # Fetch ws team and compare domain
    async with __import__("aiosqlite").connect(store.db_path) as db:
        db.row_factory = __import__("aiosqlite").Row
        async with db.execute(
            "SELECT o.domain FROM workspaces w JOIN teams o ON o.id = w.team_id WHERE w.id = ?",
            (workspace_id,),
        ) as cur:
            row = await cur.fetchone()
    team_domain = row["domain"] if row else None
    if team_domain:
        email_domain = email.rsplit("@", 1)[-1].lower()
        if email_domain != team_domain.lower():
            return {"state": "ok-foreign", "ws_domain": team_domain}

    return {"state": "ok"}


class RoleBody(BaseModel):
    role: str = Field(..., pattern="^(admin|member|viewer)$")


@router.put("/{workspace_id}/members/{user_id}/role", dependencies=[Depends(require_not_readonly)])
async def update_role(
    workspace_id: str,
    user_id: str,
    body: RoleBody,
    caller: User = Depends(_require_workspace_admin),  # Sprint α G9 SEC P0
    store: UserStore = Depends(_store),
) -> dict:
    # followup-context-deck #2: last-admin protection. Target must be a current member (None→404, also rejecting
    # user_ids outside this ws); never demote the last admin, leaving the workspace with zero admins and
    # no management access (also blocks the sole admin's self-demotion).
    target_role = await store.get_member_role(workspace_id, user_id)
    if target_role is None:
        raise HTTPException(404, {"code": "workspace_member_not_found"})
    if target_role == "admin" and body.role != "admin" and await store.count_active_workspace_admins(workspace_id) <= 1:
        raise HTTPException(422, {"code": "last_workspace_admin"})
    ok = await store.update_member_role(workspace_id, user_id, body.role)
    if not ok:
        raise HTTPException(404, {"code": "workspace_member_not_found"})
    return {"ok": True, "user_id": user_id, "new_role": body.role}


@router.delete("/{workspace_id}/members/{user_id}", dependencies=[Depends(require_not_readonly)])
async def remove_member(
    workspace_id: str,
    user_id: str,
    caller: User = Depends(_require_workspace_admin),  # Sprint α G9 SEC P0
    store: UserStore = Depends(_store),
) -> dict:
    # followup-context-deck #2: last-admin protection. Target must be a current member (None→404); never remove
    # the last admin, leaving zero admins and no management access (also blocks the sole admin's self-removal).
    target_role = await store.get_member_role(workspace_id, user_id)
    if target_role is None:
        raise HTTPException(404, {"code": "workspace_member_not_found"})
    if target_role == "admin" and await store.count_active_workspace_admins(workspace_id) <= 1:
        raise HTTPException(422, {"code": "last_workspace_admin"})
    ok = await store.remove_member(workspace_id, user_id)
    if not ok:
        raise HTTPException(404, {"code": "workspace_member_not_found"})
    return {"ok": True, "user_id": user_id}
