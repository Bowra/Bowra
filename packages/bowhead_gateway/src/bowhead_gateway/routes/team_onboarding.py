"""Team owner/admin readiness endpoint."""

from __future__ import annotations

import time

import anyio
from bowhead_core.user_store import User
from fastapi import APIRouter, Depends, Request

from bowhead_core import metrics
from bowhead_gateway.routes.auth import _require_team_admin_dep, _store
from bowhead_gateway.team_onboarding import (
    TeamOnboardingReadinessService,
    TeamOnboardingStatusResponse,
)

router = APIRouter()


@router.get(
    "/api/v1/team-onboarding/status",
    response_model=TeamOnboardingStatusResponse,
)
async def team_onboarding_status(
    request: Request,
    user: User = Depends(_require_team_admin_dep),
) -> TeamOnboardingStatusResponse:
    store = _store(request)
    role = await store.get_team_role(user.id, user.team_id) or "member"
    started = time.monotonic()
    outcome = "success"
    try:
        with anyio.fail_after(4):
            result = await TeamOnboardingReadinessService(
                request,
                user_id=user.id,
                team_id=user.team_id,
                role=role,
            ).collect()
        if result.partial:
            outcome = "partial"
            metrics.inc("team_onboarding_partial_responses_total")
        if result.milestone == "core_ready":
            metrics.inc("team_onboarding_core_complete_total")
        for item in result.items:
            metrics.inc(
                "team_onboarding_item_state_total",
                labels={"item_id": item.id, "state": item.state},
            )
        return result
    except Exception:
        outcome = "error"
        raise
    finally:
        metrics.inc(
            "team_onboarding_status_requests_total",
            labels={"outcome": outcome},
        )
        metrics.observe(
            "team_onboarding_status_duration_seconds",
            time.monotonic() - started,
        )
