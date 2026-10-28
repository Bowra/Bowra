"""Availability of Team OpenAPI automation for Owner version governance."""

from __future__ import annotations

from bowhead_core.team_openapi_flags import team_openapi_enabled


def agent_version_automation_enabled() -> bool:
    return team_openapi_enabled()
