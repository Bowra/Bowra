"""Read-only team operations report and its reporting time zone."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bowhead_core.team_operations_report import get_team_operations_report, set_team_report_timezone
from bowhead_core.user_store import User, UserStore
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from bowhead_gateway.routes.auth import _current_user, _store

router = APIRouter()


async def _require_admin(user: User, store: UserStore) -> str:
    if not await store.is_team_admin(user.id, user.team_id):
        raise HTTPException(403, {"code": "team_operations_forbidden"})
    return user.team_id


class TimezoneUpdate(BaseModel):
    timezone: str = Field(min_length=1, max_length=64)


@router.get("")
async def read_team_operations(
    days: int = 7,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    team_id = await _require_admin(user, store)
    if days not in {1, 7, 30}:
        raise HTTPException(400, {"code": "team_operations_invalid_range"})
    return await get_team_operations_report(store.db_path, team_id=team_id, days=days)


@router.put("/timezone")
async def update_team_operations_timezone(
    body: TimezoneUpdate,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    team_id = await _require_admin(user, store)
    try:
        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(400, {"code": "team_operations_invalid_timezone"}) from None
    await set_team_report_timezone(store.db_path, team_id=team_id, timezone=body.timezone)
    return {"timezone": body.timezone}
