"""Unit tests for the team settings integration overview."""

from types import SimpleNamespace

import pytest
from bowhead_gateway.feishu.config import FeishuAppConfig
from bowhead_gateway.routes import integration_overview
from bowhead_gateway.routes._deps import require_team_admin
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _SlackStore:
    async def by_team(self, team_id: str) -> SimpleNamespace:
        assert team_id == "team_a"
        return SimpleNamespace(status="enabled")


class _TeamsStore:
    async def get_connection_for_team(self, team_id: str) -> SimpleNamespace:
        assert team_id == "team_a"
        return SimpleNamespace(status="draft")


@pytest.mark.unit
async def test_overview_only_marks_enabled_healthy_connections_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        integration_overview,
        "_diagnostics_payload",
        lambda _request: {
            "missing_credentials": [],
            "enabled": True,
            "ok": True,
        },
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                deployment_mode="local",
                slack_store=_SlackStore(),
                teams_store=_TeamsStore(),
            )
        )
    )

    result = await integration_overview.overview(request, SimpleNamespace(team_id="team_a"))

    assert [item.model_dump() for item in result.items] == [
        {"provider": "feishu", "available": True, "configured": True, "enabled": True, "healthy": True},
        {"provider": "slack", "available": True, "configured": True, "enabled": True, "healthy": True},
        {"provider": "teams", "available": True, "configured": True, "enabled": False, "healthy": False},
    ]


class _StaticFeishuResolver:
    def resolve(self, _tenant_key: str) -> FeishuAppConfig:
        return FeishuAppConfig(app_id="cli_a", app_secret="secret", bot_open_id="ou_bot", enabled=False)


@pytest.mark.unit
def test_overview_route_uses_real_feishu_diagnostics_signature() -> None:
    """Regression: the route must call the real Feishu diagnostics helper with its current signature."""
    app = FastAPI()
    app.include_router(integration_overview.router)
    app.state.deployment_mode = "saas"
    app.state.settings = SimpleNamespace(auth_mode="oidc", env="prod")
    app.state.feishu_app_config_resolver = _StaticFeishuResolver()
    app.dependency_overrides[require_team_admin] = lambda: SimpleNamespace(team_id="team_a")

    response = TestClient(app).get("/api/v1/team-settings/integrations/overview")

    assert response.status_code == 200, response.text
    items = {item["provider"]: item for item in response.json()["items"]}
    assert items["feishu"] == {
        "provider": "feishu",
        "available": True,
        "configured": False,
        "enabled": False,
        "healthy": False,
    }
    assert items["slack"]["available"] is False
    assert items["teams"]["available"] is False
