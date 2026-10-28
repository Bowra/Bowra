"""Which deployments serve the team-level OpenAPI surface.

The surface mints long-lived team-scoped credentials. A private deployment always serves it; a
SaaS one serves it by default (since 2026-10-14) unless the operator sets the switch to 0. Asserted at the route level rather than only on the flag helper: the
mount decision and the per-endpoint guard are two separate reads of the same predicate, and a
regression in either one is what would actually reach a user.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.team_openapi_flags import team_openapi_enabled
from bowhead_gateway.routes.team_openapi import _local
from fastapi import HTTPException

pytestmark = pytest.mark.unit

_ANY_REQUEST = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


def test_private_deployment_serves_the_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    monkeypatch.delenv("FEATURE_TEAM_OPENAPI_SAAS", raising=False)

    assert team_openapi_enabled()
    _local(_ANY_REQUEST)  # does not raise


def test_saas_serves_the_surface_unless_the_operator_disables_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.delenv("FEATURE_TEAM_OPENAPI_SAAS", raising=False)

    assert team_openapi_enabled()
    _local(_ANY_REQUEST)  # served by default

    monkeypatch.setenv("FEATURE_TEAM_OPENAPI_SAAS", "0")
    assert not team_openapi_enabled()
    with pytest.raises(HTTPException) as denied:
        _local(_ANY_REQUEST)
    assert denied.value.status_code == 404


def test_the_guard_does_not_read_the_deployment_mode_off_app_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale `app.state.deployment_mode` must not be able to answer this question.

    The guard used to read that attribute, which made the surface's availability a property of
    request state rather than of what the deployment actually serves.
    """
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.setenv("FEATURE_TEAM_OPENAPI_SAAS", "0")
    lying_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(deployment_mode="local")))

    with pytest.raises(HTTPException):
        _local(lying_request)
