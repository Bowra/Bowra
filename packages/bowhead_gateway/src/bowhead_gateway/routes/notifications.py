"""Notification Center HTTP API."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

from bowhead_core.notification_service import space_assignment_action_url
from bowhead_core.notification_store import ACTION_REQUIRED_TYPES
from bowhead_core.notification_types import BROWSER_TYPES, get_spec
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from bowhead_gateway.dtos import (
    NotificationActionDTO,
    NotificationCountDTO,
    NotificationItemDTO,
    NotificationListDTO,
    OkDTO,
)
from bowhead_gateway.routes.auth import _current_user, _multi_team_authz_enabled
from bowhead_gateway.routes.auth import _store as _user_store

if TYPE_CHECKING:
    from bowhead_core.notification_store import NotificationForUser, NotificationStore
    from bowhead_core.user_store import User, UserStore

router = APIRouter()

NotificationTab = Literal["all", "action", "updates"]


def normalize_notification_action_url(value: str | None) -> str | None:
    if value == "/settings/scheduled":
        return "/team/settings/scheduled/"
    if value == "/team/settings/workflows":
        return "/team/settings/workflows/"
    return value


_NOTIFICATION_PRIORITIES = frozenset({"p1", "p2", "p3"})


def normalize_notification_priority(value: str | None) -> str:
    """Map stored priorities onto the p1/p2/p3 vocabulary.

    Share and Team Skill notifications were stored as ``normal`` before they used the shared
    vocabulary; those rows read as ``p2``, like every other ordinary update.
    """
    return value if value in _NOTIFICATION_PRIORITIES else "p2"


def notification_reach_enabled() -> bool:
    """FEATURE_NOTIFICATION_REACH (default off): Team labels, panel actions, browser candidates.

    Rollout and rollback: docs/灰度功能清单.md.
    """
    return os.environ.get("FEATURE_NOTIFICATION_REACH", "0").strip().lower() in {"1", "true", "yes"}


def _api_metric(action: str, outcome: str) -> None:
    try:
        from bowhead_core import metrics as _m

        _m.inc(
            "bowhead_notification_api_total",
            labels={"action": action, "outcome": outcome},
        )
    except Exception:  # noqa: S110
        pass


def _store(request: Request) -> NotificationStore:
    if getattr(request.app.state, "notification_center_enabled", True) is False:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="notification center disabled",
        )
    store: NotificationStore | None = getattr(request.app.state, "notification_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="notification store not configured",
        )
    return store


async def _team_names(user: User, users: UserStore) -> dict[str, str]:
    """Names of the user's Teams, only when items can come from more than one of them."""
    if not notification_reach_enabled() or not _multi_team_authz_enabled():
        return {}
    teams = await users.list_active_teams_for_user(user.id)
    if len(teams) < 2:
        return {}
    return {team["id"]: team["name"] for team in teams}


def _to_dto(
    notification: NotificationForUser,
    *,
    team_names: dict[str, str] | None = None,
    current_team_id: str | None = None,
) -> NotificationItemDTO:
    requester_id = notification.metadata.get("requesterUserId") if notification.type == "space_assignment_requested" else None
    action_url = space_assignment_action_url(requester_id, notification.team_id) if isinstance(requester_id, str) and requester_id else None
    actions = [
        NotificationActionDTO(
            **{
                **action,
                "url": action_url or normalize_notification_action_url(action.get("url")),
            }
        )
        for action in notification.actions
    ]
    spec = get_spec(notification.type)
    scope = spec.scope if spec is not None else "team"
    return NotificationItemDTO(
        actionRequired=notification.type in ACTION_REQUIRED_TYPES and notification.state == "open",
        id=notification.id,
        teamId=notification.team_id,
        category=notification.category,
        type=notification.type,
        priority=normalize_notification_priority(notification.priority),
        title=notification.title,
        body=notification.body,
        subjectType=notification.subject_type,
        subjectId=notification.subject_id,
        actorUserId=notification.actor_user_id,
        actionUrl=action_url or normalize_notification_action_url(notification.action_url),
        actions=actions,
        metadata=notification.metadata,
        state=notification.state,
        recipientRole=notification.recipient_role,
        deliveredAt=notification.delivered_at,
        readAt=notification.read_at,
        archivedAt=notification.archived_at,
        createdAt=notification.created_at,
        updatedAt=notification.updated_at,
        scope=scope,
        teamName=(team_names or {}).get(notification.team_id) if scope == "team" else None,
        currentTeam=scope == "personal" or current_team_id is None or notification.team_id == current_team_id,
    )


@router.get("", response_model=NotificationListDTO)
async def list_notifications(
    store: NotificationStore = Depends(_store),
    user: User = Depends(_current_user),
    users: UserStore = Depends(_user_store),
    tab: NotificationTab = Query(default="all"),
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = None,
) -> NotificationListDTO:
    items, next_cursor = await store.list_for_user(
        user_id=user.id,
        tab=tab,
        limit=limit,
        cursor=cursor,
    )
    requesters_by_team: dict[str, list[str]] = {}
    for item in items:
        requester_id = item.metadata.get("requesterUserId")
        if item.type == "space_assignment_requested" and isinstance(requester_id, str) and requester_id:
            requesters_by_team.setdefault(item.team_id, []).append(requester_id)
    for team_id, requester_ids in requesters_by_team.items():
        labels = await users.get_team_member_labels(team_id, requester_ids)
        for item in items:
            if item.type == "space_assignment_requested" and item.team_id == team_id:
                requester_id = item.metadata.get("requesterUserId")
                if isinstance(requester_id, str):
                    item.metadata = {**item.metadata, "requesterName": labels.get(requester_id, "")}
    _api_metric("list", "success")
    team_names = await _team_names(user, users)
    return NotificationListDTO(
        items=[_to_dto(item, team_names=team_names, current_team_id=user.team_id) for item in items],
        nextCursor=next_cursor,
    )


@router.get("/unread-count", response_model=NotificationCountDTO)
async def unread_count(
    store: NotificationStore = Depends(_store),
    user: User = Depends(_current_user),
) -> NotificationCountDTO:
    counts = await store.notification_count(user_id=user.id)
    latest = await store.latest_browser_candidate_at(user_id=user.id, types=BROWSER_TYPES) if notification_reach_enabled() else None
    _api_metric("unread_count", "success")
    return NotificationCountDTO(
        actionRequired=counts.action_required,
        updatesUnread=counts.updates_unread,
        totalBadge=counts.total_badge,
        latestBrowserCandidateAt=latest,
    )


@router.get("/browser-candidates", response_model=NotificationListDTO)
async def browser_candidates(
    store: NotificationStore = Depends(_store),
    user: User = Depends(_current_user),
    users: UserStore = Depends(_user_store),
    since: str | None = Query(default=None, max_length=64),
) -> NotificationListDTO:
    """Unread urgent items delivered after ``since`` that the open app may announce (at most 5).

    Only what the localized copy needs; the same authentication and switches as the list.
    """
    if not notification_reach_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification reach disabled")
    items = await store.list_browser_candidates(user_id=user.id, types=BROWSER_TYPES, since=since, limit=5)
    _api_metric("browser_candidates", "success")
    team_names = await _team_names(user, users)
    return NotificationListDTO(items=[_to_dto(item, team_names=team_names, current_team_id=user.team_id) for item in items])


class MarkAllReadBody(BaseModel):
    tab: NotificationTab = "all"


@router.post("/read-all", response_model=OkDTO)
async def mark_all_read(
    body: MarkAllReadBody,
    store: NotificationStore = Depends(_store),
    user: User = Depends(_current_user),
) -> OkDTO:
    await store.mark_all_read(user_id=user.id, tab=body.tab)
    _api_metric("read_all", "success")
    return OkDTO()


@router.post("/{notification_id}/read", response_model=OkDTO)
async def mark_read(
    notification_id: str,
    store: NotificationStore = Depends(_store),
    user: User = Depends(_current_user),
) -> OkDTO:
    ok = await store.mark_read(notification_id=notification_id, user_id=user.id)
    if not ok:
        _api_metric("read", "not_found")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found")
    _api_metric("read", "success")
    return OkDTO()


@router.post("/{notification_id}/archive", response_model=OkDTO)
async def archive(
    notification_id: str,
    store: NotificationStore = Depends(_store),
    user: User = Depends(_current_user),
) -> OkDTO:
    ok = await store.archive(notification_id=notification_id, user_id=user.id)
    if not ok:
        _api_metric("archive", "not_found")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found")
    _api_metric("archive", "success")
    return OkDTO()
