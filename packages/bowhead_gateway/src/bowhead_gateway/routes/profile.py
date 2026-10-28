"""P4.5.5 — User profile + API tokens + GDPR endpoints.

GET    /api/v1/profile/me                 → user info (跟 /auth/me 接近)
PATCH  /api/v1/profile/me                 → update display_name/locale/timezone/preferences
GET    /api/v1/profile/api-tokens         → list user's tokens
POST   /api/v1/profile/api-tokens         → issue new token (raw shown once)
DELETE /api/v1/profile/api-tokens/{id}    → revoke
GET    /api/v1/profile/export             → GDPR data export (JSON dump)
DELETE /api/v1/profile                    → soft-delete user (30d grace)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bowhead_gateway.routes.auth import _current_user, _store

if TYPE_CHECKING:
    from bowhead_core.user_store import User, UserStore

router = APIRouter()


@router.get("/me")
async def get_profile(user: User = Depends(_current_user)) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "tier": user.tier,
        "team_id": user.team_id,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "locale": user.locale,
        "timezone": user.timezone,
        "preferences": user.preferences,
        "credit_balance": user.credit_balance,
        "credit_quota": user.credit_quota,
        "email_verified": user.email_verified,
    }


class UpdateProfileBody(BaseModel):
    display_name: str | None = None
    avatar_url: str | None = None
    locale: str | None = None
    timezone: str | None = None
    preferences: dict[str, Any] | None = None


@router.patch("/me")
async def update_profile(
    body: UpdateProfileBody,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    import aiosqlite

    fields = []
    params: list[Any] = []
    if body.display_name is not None:
        fields.append("display_name = ?")
        params.append(body.display_name)
    if body.avatar_url is not None:
        fields.append("avatar_url = ?")
        params.append(body.avatar_url)
    if body.locale is not None:
        fields.append("locale = ?")
        params.append(body.locale)
    if body.timezone is not None:
        fields.append("timezone = ?")
        params.append(body.timezone)
    if body.preferences is not None:
        fields.append("preferences_json = ?")
        params.append(json.dumps(body.preferences, ensure_ascii=False))
    if not fields:
        return {"ok": True, "no_change": True}
    params.append(user.id)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        await db.commit()
    return {"ok": True}


# ─── API tokens ──────────────────────────────────────────


@router.get("/api-tokens")
async def list_tokens(
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    return {"tokens": await store.list_api_tokens(user.id)}


class IssueTokenBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    workspace_id: str | None = None
    expires_at: str | None = None


@router.post("/api-tokens")
async def issue_token(
    body: IssueTokenBody,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    """Snapshot user role at issue time (12.9 fix)."""
    role_snapshot = "member"
    if body.workspace_id:
        r = await store.get_member_role(body.workspace_id, user.id)
        role_snapshot = r or "member"
    raw, tid = await store.issue_api_token(
        user_id=user.id,
        name=body.name,
        role_snapshot=role_snapshot,
        workspace_id=body.workspace_id,
        expires_at=body.expires_at,
    )
    await store.log_audit(
        user_id=user.id,
        team_id=user.team_id,
        event_type="api_token_issue",
        payload={"token_id": tid, "name": body.name},
    )
    return {"id": tid, "name": body.name, "token": raw, "role_snapshot": role_snapshot, "warning": "Save this token now — it won't be shown again."}


@router.delete("/api-tokens/{token_id}")
async def revoke_token(
    token_id: str,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    ok = await store.revoke_api_token(token_id, user.id)
    if not ok:
        raise HTTPException(404, "token not found")
    return {"ok": True, "revoked": token_id}


# ─── GDPR ────────────────────────────────────────────────


@router.get("/export")
async def export_data(
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> JSONResponse:
    data = await store.export_user_data(user.id)
    await store.log_audit(
        user_id=user.id,
        team_id=user.team_id,
        event_type="gdpr_export",
    )
    return JSONResponse(
        content=data,
        headers={
            "Content-Disposition": f"attachment; filename=bowhead-export-{user.id}.json",
        },
    )


class DeleteAccountBody(BaseModel):
    confirm_email: str
    password: str | None = None


@router.delete("")
async def delete_account(
    request: Request,
    response: Response,
    body: DeleteAccountBody | None = None,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    """Retire the identity and expose its exact retained-data deadline."""
    from bowhead_core._email_utils import normalize_email
    from bowhead_core.account_deletion import AccountDeletionError
    from bowhead_core.user_store import verify_password

    from bowhead_gateway.routes.auth import _clear_refresh_cookie, _extract_jwt_payload

    if body is None or normalize_email(body.confirm_email) != normalize_email(user.email):
        raise HTTPException(422, {"code": "account_email_confirmation_required"})
    if user.password_hash:
        if not body.password or not verify_password(body.password, user.password_hash):
            raise HTTPException(403, {"code": "account_password_confirmation_failed"})
    else:
        payload = _extract_jwt_payload(request) or {}
        auth_time = payload.get("auth_time")
        age = datetime.now(UTC).timestamp() - auth_time if type(auth_time) is int else None
        if payload.get("sub") != user.id or age is None or not 0 <= age <= 300:
            raise HTTPException(403, {"code": "account_recent_login_required"})
    service = getattr(request.app.state, "account_deletion_service", None)
    if service is None:
        raise HTTPException(503, {"code": "account_deletion_not_ready"})
    try:
        result = await service.request(user.id, expected_pwd_version=user.pwd_version)
    except AccountDeletionError as exc:
        teams = []
        for team_id in exc.team_ids:
            team = await store.get_team(team_id)
            teams.append({"id": team_id, "name": team.name if team is not None else team_id})
        raise HTTPException(409, {"code": exc.code, "teams": teams}) from exc
    response.delete_cookie(key="bowhead_jwt")
    _clear_refresh_cookie(response)
    # Admission is already fenced. Slow external cleanup is always resumable.
    service.wake()
    return {
        "ok": True,
        "user_id": user.id,
        "operation_id": result.operation_id,
        "requested_at": result.requested_at,
        "purge_after": result.purge_after,
    }
