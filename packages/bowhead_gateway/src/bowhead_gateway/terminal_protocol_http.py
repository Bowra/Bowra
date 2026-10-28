"""Finite HTTP projection for Terminal Protocol control-plane failures."""

from __future__ import annotations

from fastapi import HTTPException, status


class TerminalProtocolAccepted(HTTPException):
    """202 control response whose frozen payload is serialized at top level."""


def terminal_protocol_http_error(error: BaseException) -> HTTPException:
    """Map Core protocol failures to stable, non-sensitive API codes."""

    from bowhead_core.terminal_protocol_store import (
        DecisionAccessRevokedError,
        DecisionAuthorizationDeniedError,
        DecisionConflictError,
        DecisionSourceNotReadyError,
        TerminalProtocolFutureVersionError,
        TerminalProtocolMalformedError,
    )
    from bowhead_core.terminal_runtime_factory import TerminalRuntimeAdmissionError

    if isinstance(error, DecisionAuthorizationDeniedError):
        if error.reason_code in {
            "dependency_missing",
            "entitlement_denied",
        }:
            return HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "terminal_dependency_missing",
            )
        return HTTPException(status.HTTP_403_FORBIDDEN, "resume_access_revoked")
    if isinstance(error, DecisionAccessRevokedError):
        return HTTPException(status.HTTP_403_FORBIDDEN, "resume_access_revoked")
    if isinstance(error, DecisionConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, "decision_conflict")
    if isinstance(error, TerminalProtocolFutureVersionError):
        return HTTPException(status.HTTP_409_CONFLICT, "terminal_protocol_future_version")
    if isinstance(error, DecisionSourceNotReadyError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "terminal_protocol_writer_not_ready")
    if isinstance(error, TerminalRuntimeAdmissionError):
        code = error.code
        if code in {
            "terminal_access_revoked",
            "terminal_auth_fence_changed",
            "terminal_decision_access_revoked",
        }:
            return HTTPException(status.HTTP_403_FORBIDDEN, "resume_access_revoked")
        conflict_codes = {
            "decision_already_terminal": "decision_already_terminal",
            "session_recovery_locked": "session_recovery_locked",
            "session_turn_active": "session_turn_active",
            "terminal_decision_already_terminal": "decision_already_terminal",
            "terminal_decision_busy": "session_turn_active",
            "terminal_decision_recovery_locked": "session_recovery_locked",
        }
        if code in conflict_codes:
            return HTTPException(
                status.HTTP_409_CONFLICT,
                conflict_codes[code],
            )
        if code in {"terminal_decision_recorded", "terminal_admission_recovery_pending"}:
            if not error.decision_id:
                return HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "terminal_protocol_writer_not_ready",
                )
            return TerminalProtocolAccepted(
                status.HTTP_202_ACCEPTED,
                {
                    "decision_id": error.decision_id,
                    # A recorded decision has not yet durably opened N+1.
                    # Never project a speculative/preflight generation.
                    "generation": None,
                    "retryable": True,
                    "state": "decision_recorded",
                },
            )
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "terminal_protocol_writer_not_ready")
    if isinstance(error, TerminalProtocolMalformedError):
        return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "decision_source_malformed")
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "terminal_protocol_internal_error")


def terminal_post_decision_http_error(
    error: BaseException,
    *,
    decision_id: str,
) -> HTTPException:
    """Preserve a committed decision when later admission is recoverable."""

    projected = terminal_protocol_http_error(error)
    if (
        projected.status_code
        in {
            status.HTTP_403_FORBIDDEN,
            status.HTTP_409_CONFLICT,
        }
        or projected.detail == "decision_source_malformed"
    ):
        return projected
    from bowhead_core.terminal_runtime_factory import (
        TerminalRuntimeAdmissionError,
    )

    return terminal_protocol_http_error(
        TerminalRuntimeAdmissionError(
            "terminal_decision_recorded",
            decision_id=decision_id,
        )
    )


def register_terminal_protocol_http_handler(app: object) -> None:
    """Install the exact top-level serializer for accepted durable decisions."""

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    if not isinstance(app, FastAPI):
        raise TypeError("terminal protocol HTTP app is invalid")

    async def accepted_handler(
        _request: Request,
        exc: TerminalProtocolAccepted,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.detail,
            headers=exc.headers,
        )

    app.add_exception_handler(
        TerminalProtocolAccepted,
        accepted_handler,
    )
