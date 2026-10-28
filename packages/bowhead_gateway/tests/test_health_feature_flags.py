"""Public `/health` capability contract."""

from __future__ import annotations

import pytest
from bowhead_gateway.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.mark.unit
def test_health_only_exposes_browser_consumed_capabilities(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEATURE_ERROR_DIAGNOSTICS", "1")

    flags = client.get("/health").json()["feature_flags"]

    assert flags == {
        "session_archive": True,
        "error_diagnostics": True,
        "results_workspace": True,
    }


@pytest.mark.unit
def test_error_diagnostics_defaults_on_and_explicit_zero_disables(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FEATURE_ERROR_DIAGNOSTICS", raising=False)
    assert client.get("/health").json()["feature_flags"]["error_diagnostics"] is True

    monkeypatch.setenv("FEATURE_ERROR_DIAGNOSTICS", "0")
    assert client.get("/health").json()["feature_flags"]["error_diagnostics"] is False


@pytest.mark.unit
def test_health_preserves_frontend_reload_contract(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_FRONTEND_MIN", "1.2.3")
    monkeypatch.setenv("BOWHEAD_FORCE_FRONTEND_RELOAD", "1")

    body = client.get("/health").json()

    assert body["frontend_min"] == "1.2.3"
    assert body["force_reload"] is True
