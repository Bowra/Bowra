"""One-shot, administrator-assisted credential recovery."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from bowhead_core.user_store import User, UserStore, hash_password
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.auth_mutation_security import require_same_origin_json
from bowhead_gateway.public_url import (
    PublicEntryRequiredError,
    public_app_path,
    public_entry_required_detail,
    team_public_app_url,
)
from bowhead_gateway.routes.auth import (
    _bind_current_team_public_entry,
    _cookie_samesite,
    _cookie_secure,
    _require_team_role_dep,
    _store,
    _validate_password_strength,
)

public_router = APIRouter()
admin_router = APIRouter()

_RECOVERY_COOKIE = "bowhead_recovery_intent"
_RECOVERY_TTL_SECONDS = 15 * 60
_REQUIRE_RECOVERY_ADMIN = _require_team_role_dep("owner", "team_admin")


class RecoveryIntentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(..., min_length=20, max_length=500)


class RecoveryCompleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_password: str = Field(..., min_length=8, max_length=200)


@admin_router.post(
    "/api/v1/admin/members/{target_user_id}/credential-recovery",
    dependencies=[Depends(require_not_readonly)],
)
async def create_credential_recovery(
    target_user_id: str,
    request: Request,
    caller: User = Depends(_REQUIRE_RECOVERY_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    require_same_origin_json(request)
    if caller.id == target_user_id:
        raise HTTPException(422, {"code": "self_recovery_not_allowed"})
    target = await store.get_user(target_user_id)
    if target is None or target.status != "active":
        raise HTTPException(404, {"code": "member_not_found"})
    memberships = await store.list_team_memberships_for_user(target_user_id)
    caller_membership = next(
        (item for item in memberships if item["team_id"] == caller.team_id),
        None,
    )
    if caller_membership is None or caller_membership["status"] != "active":
        raise HTTPException(404, {"code": "member_not_found"})
    if len({str(item["team_id"]) for item in memberships}) != 1:
        raise HTTPException(
            409,
            {"code": "shared_identity_recovery_requires_self_service"},
        )
    caller_role = await store.get_team_role(caller.id, caller.team_id)
    target_role = await store.get_team_role(target_user_id, caller.team_id)
    if target_role == "owner" and caller_role != "owner":
        raise HTTPException(403, {"code": "owner_recovery_requires_owner"})
    if not await store.rate_limit_check(
        f"credential_recovery:{caller.id}:{target_user_id}",
        limit=3,
        window_s=3600,
    ):
        raise HTTPException(429, {"code": "rate_limited"})

    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(seconds=_RECOVERY_TTL_SECONDS)
    try:
        reset_url = await team_public_app_url(
            request,
            team_id=caller.team_id,
            path=f"/reset-password?flow=admin&token={raw_token}",
        )
    except PublicEntryRequiredError as exc:
        raise HTTPException(409, public_entry_required_detail()) from exc
    try:
        await store.create_credential_recovery_token(
            raw_token=raw_token,
            user_id=target_user_id,
            team_id=caller.team_id,
            created_by_user_id=caller.id,
            expires_at=expires_at.isoformat(),
        )
    except ValueError as exc:
        raise HTTPException(
            409,
            {"code": "credential_recovery_not_allowed"},
        ) from exc
    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action="credential_recovery_create",
        target_kind="user",
        target_id=target_user_id,
        scope="team",
        payload={"expires_in_seconds": _RECOVERY_TTL_SECONDS},
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {
        "ok": True,
        "delivery": "manual",
        "recovery_url": reset_url,
        "expires_at": expires_at.isoformat(),
    }


@admin_router.post(
    "/api/v1/admin/members/{target_user_id}/credential-recovery/revoke",
    dependencies=[Depends(require_not_readonly)],
)
async def revoke_credential_recovery(
    target_user_id: str,
    request: Request,
    caller: User = Depends(_REQUIRE_RECOVERY_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    require_same_origin_json(request)
    target = await store.get_user(target_user_id)
    if target is None:
        raise HTTPException(404, {"code": "member_not_found"})
    memberships = await store.list_team_memberships_for_user(target_user_id)
    if not any(item["team_id"] == caller.team_id and item["status"] == "active" for item in memberships):
        raise HTTPException(404, {"code": "member_not_found"})
    count = await store.revoke_credential_recovery_token(
        user_id=target_user_id,
        team_id=caller.team_id,
    )
    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action="credential_recovery_revoke",
        target_kind="user",
        target_id=target_user_id,
        scope="team",
        payload={"revoked_count": count},
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {"ok": True, "revoked_count": count}


@public_router.post("/credential-recovery/intent")
async def create_recovery_intent(
    body: RecoveryIntentBody,
    request: Request,
    response: Response,
    store: UserStore = Depends(_store),
) -> dict:
    require_same_origin_json(request)
    ip = request.client.host if request.client else "unknown"
    if not await store.rate_limit_check(
        f"credential_recovery_intent:ip:{ip}",
        limit=30,
        window_s=60,
    ):
        raise HTTPException(429, {"code": "rate_limited"})
    row = await store.get_credential_recovery_token(body.token)
    if row is None or row["status"] != "active" or row["expires_at"] < datetime.now(UTC).isoformat():
        raise HTTPException(410, {"code": "recovery_invalid_or_expired"})
    await _bind_current_team_public_entry(request, str(row["team_id"]))
    _set_recovery_cookie(response, body.token)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return {"ok": True, "status": "ready"}


@public_router.post("/credential-recovery/complete")
async def complete_recovery(
    body: RecoveryCompleteBody,
    request: Request,
    response: Response,
    store: UserStore = Depends(_store),
) -> dict:
    require_same_origin_json(request)
    raw_token = request.cookies.get(_RECOVERY_COOKIE)
    if not raw_token:
        raise HTTPException(401, {"code": "recovery_intent_missing"})
    ip = request.client.host if request.client else "unknown"
    token_bucket = hashlib.sha256(raw_token.encode()).hexdigest()[:24]
    if not await store.rate_limit_check(
        f"credential_recovery_complete:{ip}:{token_bucket}",
        limit=5,
        window_s=_RECOVERY_TTL_SECONDS,
    ):
        raise HTTPException(429, {"code": "rate_limited"})
    _validate_password_strength(body.new_password)
    try:
        user_id = await store.complete_credential_recovery(
            raw_token=raw_token,
            password_hash=hash_password(body.new_password),
        )
    except ValueError as exc:
        _clear_recovery_cookie(response)
        raise HTTPException(410, {"code": "recovery_invalid_or_expired"}) from exc
    _clear_recovery_cookie(response)
    await store.log_audit(
        user_id=user_id,
        team_id=None,
        event_type="credential_recovery_consumed",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        payload={},
    )
    return {"ok": True, "login_required": True}


def _set_recovery_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=_RECOVERY_COOKIE,
        value=raw_token,
        httponly=True,
        secure=_cookie_secure(),
        samesite=_cookie_samesite(),  # type: ignore[arg-type]
        max_age=_RECOVERY_TTL_SECONDS,
        path=public_app_path("/"),
    )


def _clear_recovery_cookie(response: Response) -> None:
    response.delete_cookie(
        key=_RECOVERY_COOKIE,
        path=public_app_path("/"),
        secure=_cookie_secure(),
        samesite=_cookie_samesite(),  # type: ignore[arg-type]
    )
