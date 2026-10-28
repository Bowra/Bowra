"""Edition-aware first-owner setup status and Community initialization."""

from __future__ import annotations

from bowhead_core.installation import InstallationError, read_installation, setup_community_owner
from bowhead_core.user_store import UserStore
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from bowhead_gateway.routes.auth import EMAIL_RE, _check_rate_limit, _store, _validate_password_strength

router = APIRouter()


class CommunitySetupBody(BaseModel):
    setup_token: str = Field(..., min_length=1, max_length=200)
    team_name: str = Field(..., min_length=1, max_length=120)
    email: str
    password: str = Field(..., min_length=8, max_length=200)
    display_name: str | None = Field(None, max_length=120)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalized = value.lower().strip()
        if not EMAIL_RE.match(normalized):
            raise ValueError("invalid email")
        return normalized

    @field_validator("team_name")
    @classmethod
    def validate_team_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("team name must not be blank")
        return value.strip()


@router.get("/api/v1/bootstrap/status")
async def bootstrap_status(request: Request, store: UserStore = Depends(_store)) -> dict:
    """Public setup state; never include an operator token or account identities."""
    mode = getattr(request.app.state, "deployment_mode", "local")
    edition = getattr(request.app.state, "edition", "business")
    if edition == "community":
        identity = read_installation(store.db_path)
        if identity is None or identity.current_edition != "community":
            raise HTTPException(503, {"code": "installation_unavailable"})
        return {"required": not identity.setup_complete, "deployment_mode": mode, "edition": edition, "setup_token_required": True}
    if mode != "local":
        return {"required": False, "deployment_mode": mode, "edition": edition}
    identity = read_installation(store.db_path)
    if identity is not None and identity.origin_edition == "community":
        return {
            "required": False,
            "deployment_mode": mode,
            "edition": edition,
            "existing_owner_activation_required": await store.get_single_team_license() is None,
        }
    required = await store.get_single_team_license() is None
    teams = await store.list_teams() if required else []
    return {
        "required": required,
        "deployment_mode": mode,
        "edition": edition,
        "license_team_ids": [{"id": team.id, "name": team.name} for team in teams],
    }


@router.post("/api/v1/bootstrap/community", status_code=201)
async def initialize_community(body: CommunitySetupBody, request: Request, store: UserStore = Depends(_store)) -> dict:
    if getattr(request.app.state, "edition", "business") != "community":
        raise HTTPException(404, {"code": "not_found"})
    _check_rate_limit(request)
    _validate_password_strength(body.password)
    try:
        user = await setup_community_owner(
            store.db_path,
            setup_token=body.setup_token,
            team_name=body.team_name,
            email=body.email,
            password=body.password,
            display_name=body.display_name,
        )
    except PermissionError as exc:
        raise HTTPException(403, {"code": "invalid_setup_token"}) from exc
    except InstallationError as exc:
        raise HTTPException(409, {"code": "setup_unavailable", "message": str(exc)}) from exc
    return {"ok": True, "user_id": user.id, "next_route": "/login", "max_seats": None}
