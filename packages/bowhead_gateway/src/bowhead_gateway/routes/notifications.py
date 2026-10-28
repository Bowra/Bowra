"""Notification Center HTTP API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

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

if TYPE_CHECKING:
    from bowhead_core.notification_store import NotificationForUser, NotificationStore
    from bowhead_core.user_store import User

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
    actions = [
        NotificationActionDTO(
            **{
                **action,
                "url": normalize_notification_action_url(action.get("url")),
            }
        )
        for action in notification.actions
    ]
    return NotificationItemDTO(
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
        actionUrl=normalize_notification_action_url(notification.action_url),
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
