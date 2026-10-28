"""Team-admin integration overview used by the settings landing page."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from bowhead_gateway.routes._deps import require_team_admin
from bowhead_gateway.routes.feishu import _diagnostics_payload

router = APIRouter(prefix="/api/v1/team-settings/integrations", tags=["team-settings"])
TEAM_ADMIN_DEP = Depends(require_team_admin)


class IntegrationOverviewItem(BaseModel):
    provider: str
    available: bool
    configured: bool
    enabled: bool
    healthy: bool


class IntegrationOverview(BaseModel):
    items: list[IntegrationOverviewItem]


@router.get("/overview", response_model=IntegrationOverview)
async def overview(
    request: Request,
    admin: Any = TEAM_ADMIN_DEP,
) -> IntegrationOverview:
    """Return only the state needed to sort integration cards.

    Credentials and callback URLs remain exclusively in each provider's admin
    endpoint.  Slack is intentionally unavailable outside local deployments.
    """
    feishu = _diagnostics_payload(request)

    slack_available = getattr(request.app.state, "deployment_mode", "saas") == "local"
    slack_connection = None
    if slack_available:
        slack_store = getattr(request.app.state, "slack_store", None)
        if slack_store is not None:
            slack_connection = await slack_store.by_team(str(admin.team_id))

    teams_store = getattr(request.app.state, "teams_store", None)
    teams_connection = await teams_store.get_connection_for_team(str(admin.team_id)) if teams_store is not None else None

    return IntegrationOverview(
        items=[
            IntegrationOverviewItem(
                provider="feishu",
                available=True,
                configured=not bool(feishu["missing_credentials"]),
                enabled=bool(feishu["enabled"]),
                healthy=bool(feishu["enabled"] and feishu["ok"]),
            ),
            IntegrationOverviewItem(
                provider="slack",
                available=slack_available,
                configured=slack_connection is not None,
                enabled=bool(slack_connection and slack_connection.status == "enabled"),
                healthy=bool(slack_connection and slack_connection.status == "enabled"),
            ),
            IntegrationOverviewItem(
                provider="teams",
                available=teams_store is not None,
                configured=teams_connection is not None,
                enabled=bool(teams_connection and teams_connection.status == "enabled"),
                healthy=bool(teams_connection and teams_connection.status == "enabled"),
            ),
        ],
    )
