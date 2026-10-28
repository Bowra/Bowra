from __future__ import annotations

import pytest
from bowhead_core.agent_version_flags import (
    agent_version_automation_enabled,
)
from bowhead_core.team_openapi_flags import team_openapi_enabled

pytestmark = pytest.mark.unit


def test_local_defaults_enable_agent_automation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    assert agent_version_automation_enabled()


def test_saas_automation_follows_the_team_openapi_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_TEAM_OPENAPI_SAAS", raising=False)
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    assert agent_version_automation_enabled()

    monkeypatch.setenv("FEATURE_TEAM_OPENAPI_SAAS", "0")
    assert not agent_version_automation_enabled()


def test_team_openapi_surface_is_served_on_private_and_default_on_saas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Private deployments keep the surface unconditionally; SaaS serves it unless explicitly disabled."""
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    monkeypatch.setenv("FEATURE_TEAM_OPENAPI_SAAS", "0")
    assert team_openapi_enabled()

    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.delenv("FEATURE_TEAM_OPENAPI_SAAS", raising=False)
    assert team_openapi_enabled()

    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("FEATURE_TEAM_OPENAPI_SAAS", truthy)
        assert team_openapi_enabled()

    monkeypatch.setenv("FEATURE_TEAM_OPENAPI_SAAS", "0")
    assert not team_openapi_enabled()
