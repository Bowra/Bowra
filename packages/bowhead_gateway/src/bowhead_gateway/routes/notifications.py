"""Notification Center HTTP API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from bowhead_core.notification_service import space_assignment_action_url
from bowhead_core.notification_store import ACTION_REQUIRED_TYPES
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from bowhead_gateway.dtos import (
    NotificationActionDTO,
    NotificationCountDTO,
    NotificationItemDTO,
    NotificationListDTO,
    OkDTO,
)
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.auth import _store as _user_store

if TYPE_CHECKING:
    from bowhead_core.notification_store import NotificationForUser, NotificationStore
    from bowhead_core.user_store import User, UserStore

router = APIRouter()

NotificationTab = Literal["all", "action", "updates"]


def normalize_notification_action_url(value: str | None) -> str | None:
    if value == "/settings/scheduled":
        return "/team/settings/scheduled/"
    return value


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


def _to_dto(notification: NotificationForUser) -> NotificationItemDTO:
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
    return NotificationItemDTO(
        actionRequired=notification.type in ACTION_REQUIRED_TYPES and notification.state == "open",
        id=notification.id,
        teamId=notification.team_id,
        category=notification.category,
        type=notification.type,
        priority=notification.priority,
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
    return NotificationListDTO(
        items=[_to_dto(item) for item in items],
        nextCursor=next_cursor,
    )


@router.get("/unread-count", response_model=NotificationCountDTO)
async def unread_count(
    store: NotificationStore = Depends(_store),
    user: User = Depends(_current_user),
) -> NotificationCountDTO:
    counts = await store.notification_count(user_id=user.id)
    _api_metric("unread_count", "success")
    return NotificationCountDTO(
        actionRequired=counts.action_required,
        updatesUnread=counts.updates_unread,
        totalBadge=counts.total_badge,
    )


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
