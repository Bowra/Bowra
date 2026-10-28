"""Gateway projections for protocol-aware Session lifecycle operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bowhead_core.session_lifecycle import (
    SESSION_LIFECYCLE_ERROR_CODES,
    SessionLifecycleError,
)
from bowhead_core.session_store import SessionProtocolManagedError
from fastapi import HTTPException, status


@dataclass(frozen=True, slots=True)
class SessionLifecyclePublicError:
    status_code: int
    code: str


@dataclass(frozen=True, slots=True)
class IMSessionReviveResult:
    changed: bool
    error_code: str | None = None


SESSION_LIFECYCLE_PUBLIC_CODES = frozenset(
    {
        "session_transitioning",
        "session_delete_active_turn",
        "terminal_dependency_recovering",
        "terminal_protocol_admission_rejected",
        "terminal_protocol_future_version",
        "terminal_protocol_malformed",
        "terminal_protocol_writer_not_ready",
    }
)
_SESSION_LIFECYCLE_PROJECTION: dict[str, SessionLifecyclePublicError] = {
    "session_active_turn": SessionLifecyclePublicError(
        status.HTTP_409_CONFLICT,
        "session_delete_active_turn",
    ),
    "canvas_lifecycle_store_missing": SessionLifecyclePublicError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "terminal_protocol_writer_not_ready",
    ),
    "linked_canvas_not_found": SessionLifecyclePublicError(
        status.HTTP_409_CONFLICT,
        "terminal_protocol_admission_rejected",
    ),
    "session_generation_not_fenced": SessionLifecyclePublicError(
        status.HTTP_409_CONFLICT,
        "terminal_dependency_recovering",
    ),
    "session_lifecycle_commit_conflict": SessionLifecyclePublicError(
        status.HTTP_409_CONFLICT,
        "session_transitioning",
    ),
    "session_lifecycle_conflict": SessionLifecyclePublicError(
        status.HTTP_409_CONFLICT,
        "session_transitioning",
    ),
    "session_lifecycle_operation_missing": SessionLifecyclePublicError(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "terminal_protocol_malformed",
    ),
    "session_lifecycle_precondition_changed": SessionLifecyclePublicError(
        status.HTTP_409_CONFLICT,
        "session_transitioning",
    ),
    "session_not_found": SessionLifecyclePublicError(
        status.HTTP_409_CONFLICT,
        "terminal_protocol_admission_rejected",
    ),
    "session_owner_mismatch": SessionLifecyclePublicError(
        status.HTTP_409_CONFLICT,
        "terminal_protocol_admission_rejected",
    ),
    "session_revive_blocked": SessionLifecyclePublicError(
        status.HTTP_409_CONFLICT,
        "terminal_dependency_recovering",
    ),
    "session_visibility_epoch_changed": SessionLifecyclePublicError(
        status.HTTP_409_CONFLICT,
        "session_transitioning",
    ),
    "session_visibility_epoch_missing": SessionLifecyclePublicError(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "terminal_protocol_malformed",
    ),
    "terminal_protocol_future_version": SessionLifecyclePublicError(
        status.HTTP_409_CONFLICT,
        "terminal_protocol_future_version",
    ),
    "terminal_protocol_malformed": SessionLifecyclePublicError(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "terminal_protocol_malformed",
    ),
}
assert _SESSION_LIFECYCLE_PROJECTION.keys() == SESSION_LIFECYCLE_ERROR_CODES


def project_session_lifecycle_error(
    exc: SessionLifecycleError,
) -> SessionLifecyclePublicError:
    """Map the finite Core registry without forwarding arbitrary exception text."""

    projected = _SESSION_LIFECYCLE_PROJECTION.get(
        exc.code,
        SessionLifecyclePublicError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "terminal_protocol_malformed",
        ),
    )
    assert projected.code in SESSION_LIFECYCLE_PUBLIC_CODES
    return projected


def session_lifecycle_http_error(exc: SessionLifecycleError) -> HTTPException:
    projected = project_session_lifecycle_error(exc)
    return HTTPException(
        status_code=projected.status_code,
        detail={"error": {"code": projected.code}},
    )


async def revive_session_for_im(
    app: Any,
    orchestrator: Any,
    *,
    session_id: str,
    user_id: str,
    request_id: str,
) -> IMSessionReviveResult:
    """Revive an IM-bound Session and return only a finite public failure."""

    coordinator = getattr(app.state, "session_lifecycle_coordinator", None)
    if coordinator is not None:
        try:
            result = await coordinator.revive(
                session_id=session_id,
                actor_audit_ref=user_id,
                request_id=request_id,
                expected_user_id=user_id,
            )
        except SessionLifecycleError as exc:
            return IMSessionReviveResult(
                changed=False,
                error_code=project_session_lifecycle_error(exc).code,
            )
        return IMSessionReviveResult(changed=result.changed)
    if getattr(app.state, "legacy_session_lifecycle_allowed", None) is not True:
        return IMSessionReviveResult(
            changed=False,
            error_code="terminal_protocol_writer_not_ready",
        )
    store = getattr(orchestrator, "session_store", None)
    if store is None:
        return IMSessionReviveResult(
            changed=False,
            error_code="terminal_protocol_writer_not_ready",
        )
    try:
        changed = await store.revive_session(session_id, user_id=user_id)
    except SessionProtocolManagedError:
        return IMSessionReviveResult(
            changed=False,
            error_code="terminal_protocol_writer_not_ready",
        )
    return IMSessionReviveResult(changed=changed)


__all__ = [
    "IMSessionReviveResult",
    "SESSION_LIFECYCLE_PUBLIC_CODES",
    "SessionLifecyclePublicError",
    "project_session_lifecycle_error",
    "revive_session_for_im",
    "session_lifecycle_http_error",
]
