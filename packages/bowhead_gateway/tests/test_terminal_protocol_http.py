from __future__ import annotations

import pytest
from bowhead_core.terminal_runtime_factory import TerminalRuntimeAdmissionError
from bowhead_gateway.terminal_protocol_http import (
    register_terminal_protocol_http_handler,
    terminal_protocol_http_error,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    ("code", "status_code", "detail"),
    (
        ("terminal_decision_access_revoked", 403, "resume_access_revoked"),
        ("terminal_decision_busy", 409, "session_turn_active"),
        (
            "terminal_decision_recovery_locked",
            409,
            "session_recovery_locked",
        ),
        (
            "terminal_decision_already_terminal",
            409,
            "decision_already_terminal",
        ),
        (
            "terminal_admission_recovery_pending",
            503,
            "terminal_protocol_writer_not_ready",
        ),
        (
            "terminal_admission_replay_pending",
            503,
            "terminal_protocol_writer_not_ready",
        ),
    ),
)
def test_terminal_control_http_projection_is_finite(
    code: str,
    status_code: int,
    detail: str,
) -> None:
    projected = terminal_protocol_http_error(TerminalRuntimeAdmissionError(code))

    assert projected.status_code == status_code
    assert projected.detail == detail


def test_terminal_control_http_projection_hides_unknown_internal_errors() -> None:
    projected = terminal_protocol_http_error(RuntimeError("secret provider detail"))

    assert projected.status_code == 500
    assert projected.detail == "terminal_protocol_internal_error"


def test_terminal_accepted_response_has_frozen_top_level_shape() -> None:
    app = FastAPI()
    register_terminal_protocol_http_handler(app)

    @app.get("/accepted")
    async def accepted() -> None:
        raise terminal_protocol_http_error(
            TerminalRuntimeAdmissionError(
                "terminal_admission_recovery_pending",
                decision_id="td_123",
                generation=2,
            )
        )

    response = TestClient(app).get("/accepted")

    assert response.status_code == 202
    assert response.json() == {
        "decision_id": "td_123",
        "generation": None,
        "retryable": True,
        "state": "decision_recorded",
    }
