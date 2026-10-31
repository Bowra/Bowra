from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.main import create_app
from bowhead_gateway.results_contracts import (
    ResultsEffectiveCapabilityV1,
    ResultsGroupCountsV1,
    ResultsNewResultsV1,
    ResultsProjectionStatusV1,
    ResultsQueryResponseV1,
)
from bowhead_gateway.routes import results as results_routes
from bowhead_gateway.routes import sessions as sessions_routes
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


def _app(user: SimpleNamespace | None = None) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.state.settings = SimpleNamespace(auth_mode="prod")
    app.include_router(results_routes.router, prefix="/api/v1/results")
    app.include_router(results_routes.owner_router, prefix="/api/v1")
    if user is not None:
        app.dependency_overrides[results_routes._current_user] = lambda: user
    return app


@pytest.mark.unit
def test_results_capability_route_is_authenticated() -> None:
    response = TestClient(_app()).get("/api/v1/results/capabilities")

    assert response.status_code == 401


@pytest.mark.unit
def test_results_capability_route_returns_standard_results_capabilities(monkeypatch) -> None:
    monkeypatch.delenv("FEATURE_INTERACTIVE_HTML_PREVIEW", raising=False)
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    user = SimpleNamespace(id="user_a", team_id="team_a")

    response = TestClient(_app(user)).get("/api/v1/results/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "dto_version": 1,
        "workspace": True,
        "complete_list": True,
        "scale_nav": True,
        "interactive_html": True,
        "max_page_size": 50,
    }
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie"


@pytest.mark.unit
def test_results_capability_route_fails_subfeatures_closed_without_cursor_key(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "invalid")
    user = SimpleNamespace(id="user_a", team_id="team_a")

    response = TestClient(_app(user)).get("/api/v1/results/capabilities")

    assert response.status_code == 200
    assert response.json()["workspace"] is True
    assert response.json()["complete_list"] is False
    assert response.json()["scale_nav"] is False


@pytest.mark.unit
def test_results_capability_route_is_mounted_in_main_app() -> None:
    paths = create_app().openapi()["paths"]

    assert "/api/v1/results/capabilities" in paths
    assert "/api/v1/sessions/{session_id}/results/query" in paths
    assert "/api/v1/canvas/by-session/{session_id}/results/query" in paths


@pytest.mark.unit
def test_owner_results_route_enforces_owned_scope(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    user = SimpleNamespace(id="user_a", team_id="team_a")
    app = _app(user)

    async def _missing_session(_session_id: str):  # type: ignore[no-untyped-def]
        return None

    app.state.session_store = SimpleNamespace(get_session=_missing_session)
    app.state.canvas_store = object()
    app.state.artifact_store = object()

    disabled = TestClient(app).post(
        "/api/v1/sessions/session_a/results/query",
        json={"dto_version": 1, "surface": "explorer"},
    )

    # The 404 conceals the absent/non-owned session without entering the query
    # service.
    assert disabled.status_code == 404
    assert disabled.json()["detail"]["error"]["code"] == "results_scope_unavailable"
    assert disabled.headers["cache-control"] == "private, no-store"


@pytest.mark.unit
def test_owner_results_route_maps_scope_store_failure_to_finite_503(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "BOWHEAD_FERNET_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    user = SimpleNamespace(id="user_a", team_id="team_a")
    app = _app(user)

    async def _broken_session(_session_id: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("database detail must not escape")

    app.state.session_store = SimpleNamespace(get_session=_broken_session)
    app.state.canvas_store = object()
    app.state.artifact_store = object()

    response = TestClient(app).post(
        "/api/v1/sessions/session_a/results/query",
        json={"dto_version": 1, "surface": "explorer"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["code"] == ("results_read_model_unavailable")
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.unit
def test_owner_results_route_returns_no_store_contract(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    user = SimpleNamespace(id="user_a", team_id="team_a")
    app = _app(user)

    async def _session(_session_id: str):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            id="session_a",
            user_id="user_a",
            workspace_id="workspace_a",
        )

    async def _allow_lifecycle(*_args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        if kwargs.get("operation") is results_routes.SpaceOperation.WRITE:
            raise HTTPException(409, "workspace_archived")

    captured: dict[str, object] = {}

    async def _query(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return ResultsQueryResponseV1(
            capability=ResultsEffectiveCapabilityV1(scale_nav=True),
            snapshot_token="snapshot",
            items=[],
            authorized_total=0,
            matched_total=0,
            group_counts=ResultsGroupCountsV1(
                deliverables=0,
                evidence=0,
                data=0,
                code=0,
                input=0,
            ),
            loaded_count=0,
            page_start=0,
            next_cursor=None,
            previous_cursor=None,
            has_more=False,
            projection_status=ResultsProjectionStatusV1(
                report_relations="unavailable",
                execution_relations="unavailable",
                export_relations="degraded",
            ),
            projection_revision="revision",
            new_results=ResultsNewResultsV1(count=0, projection_changed=False),
            resolved_current_key=None,
        )

    app.state.session_store = SimpleNamespace(get_session=_session)
    app.state.canvas_store = object()
    app.state.artifact_store = object()
    monkeypatch.setattr(sessions_routes, "_require_workspace_access", _allow_lifecycle)
    monkeypatch.setattr(results_routes, "query_owner_results", _query)

    response = TestClient(app).post(
        "/api/v1/sessions/session_a/results/query",
        json={"dto_version": 1, "surface": "explorer"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie"
    assert captured["writable"] is False
    assert captured["scale_nav"] is True
    assert response.json()["capability"] == {
        "complete_list": True,
        "scale_nav": True,
    }


@pytest.mark.unit
def test_owner_results_route_maps_invalid_body_to_finite_400(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    user = SimpleNamespace(id="user_a", team_id="team_a")

    response = TestClient(_app(user)).post(
        "/api/v1/sessions/session_a/results/query",
        json={"dto_version": 1, "surface": "explorer", "recent_keys": ["x" * 257]},
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": {"error": {"code": "results_query_invalid"}},
    }
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.unit
@pytest.mark.parametrize("value,expected", [(None, True), ("0", False), ("", False), ("true", False), ("1", True)])
def test_interactive_html_default_and_explicit_override(monkeypatch, value, expected) -> None:
    if value is None:
        monkeypatch.delenv("FEATURE_INTERACTIVE_HTML_PREVIEW", raising=False)
    else:
        monkeypatch.setenv("FEATURE_INTERACTIVE_HTML_PREVIEW", value)
    user = SimpleNamespace(id="user_a", team_id="team_a")
    response = TestClient(_app(user)).get("/api/v1/results/capabilities")
    assert response.status_code == 200
    assert response.json()["interactive_html"] is expected
