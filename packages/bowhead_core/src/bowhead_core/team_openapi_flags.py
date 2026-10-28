"""Whether this deployment serves the team-level OpenAPI surface.

The surface mints long-lived, team-scoped API credentials (`team_openapi_keys`). Private
deployments have always served it. SaaS serves it by default since 2026-10-14; an operator who
must not issue long-lived team credentials in a multi-tenant deployment sets
`FEATURE_TEAM_OPENAPI_SAAS=0`. This remains a per-deployment decision — not something a feature
flag belonging to one consumer of that surface may turn on by itself.

This is the single source of truth for "is the transport there". Both the router mount and the
Agent-version automation dependency check read it, so a deployment can never end up with the
automation flag accepted at startup while the credential surface it needs is unmounted.
"""

from __future__ import annotations

import os

from bowhead_core.entitlements import deployment_mode_from_env

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def team_openapi_enabled() -> bool:
    if deployment_mode_from_env() == "local":
        return True
    return os.environ.get("FEATURE_TEAM_OPENAPI_SAAS", "1").strip().lower() in _TRUTHY
