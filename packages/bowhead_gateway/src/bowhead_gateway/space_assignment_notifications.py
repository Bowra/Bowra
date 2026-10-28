"""Best-effort convergence for no-Space assignment notifications."""

from __future__ import annotations

import logging

from bowhead_core.user_store import UserStore
from fastapi import Request

logger = logging.getLogger(__name__)


async def resolve_space_assignment_if_ready(
    request: Request,
    *,
    store: UserStore,
    team_id: str,
    user_id: str,
) -> None:
    """Resolve an open request once the user has any active Space."""

    try:
        if not await store.list_workspaces_for_user(
            user_id,
            team_id=team_id,
        ):
            return
        service = getattr(request.app.state, "notification_service", None)
        if service is None:
            return
        await service.resolve_space_assignment_request(
            team_id=team_id,
            requester_user_id=user_id,
        )
    except Exception:
        logger.exception(
            "space_assignment_notification.resolve_failed team_id=%s user_id=%s",
            team_id,
            user_id,
        )
