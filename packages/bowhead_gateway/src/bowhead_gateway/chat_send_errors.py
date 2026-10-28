"""Closed projection for dynamic ``POST /chat/send`` failures.

Only codes in this module may cross a runtime expression or a persisted
submission receipt.  Static route branches continue to construct literal
code-only errors, while this boundary prevents database or exception payloads
from becoming arbitrary user-visible detail.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bowhead_core.document_revision_target import DOCUMENT_REVISION_ADMISSION_ERRORS
from fastapi import HTTPException, status

CHAT_SEND_DYNAMIC_ERROR_STATUS = {
    **dict.fromkeys(DOCUMENT_REVISION_ADMISSION_ERRORS, status.HTTP_409_CONFLICT),
    "coach_canvas_target_unavailable": status.HTTP_409_CONFLICT,
    "unsupported_entry": status.HTTP_409_CONFLICT,
    "auth_session_generation_inactive": status.HTTP_401_UNAUTHORIZED,
    "plan_already_running": status.HTTP_409_CONFLICT,
    "plan_not_approvable": status.HTTP_409_CONFLICT,
    "plan_not_found": status.HTTP_404_NOT_FOUND,
    "planning_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "steer_parent_not_active": status.HTTP_409_CONFLICT,
    "submission_abandoned": status.HTTP_409_CONFLICT,
    "submission_contract_inactive": status.HTTP_409_CONFLICT,
    "submission_id_conflict": status.HTTP_409_CONFLICT,
    "submission_kind_unavailable": status.HTTP_409_CONFLICT,
    "submission_not_found": status.HTTP_404_NOT_FOUND,
    "submission_pending": status.HTTP_409_CONFLICT,
    "submission_rejected": status.HTTP_409_CONFLICT,
    "turn_became_active": status.HTTP_409_CONFLICT,
}
CHAT_SEND_DYNAMIC_ERROR_CODES = frozenset(CHAT_SEND_DYNAMIC_ERROR_STATUS)


def chat_send_http_error(code: str) -> HTTPException:
    """Return a code-only error, rejecting values outside the closed map."""

    safe_code = code if code in CHAT_SEND_DYNAMIC_ERROR_CODES else "submission_rejected"
    return HTTPException(
        status_code=CHAT_SEND_DYNAMIC_ERROR_STATUS[safe_code],
        detail={"error": {"code": safe_code}},
    )


def chat_submission_receipt_http_error(
    response: Mapping[str, Any],
    *,
    fallback_code: str,
) -> HTTPException:
    """Project a persisted rejection without trusting its status or envelope."""

    detail = response.get("detail")
    error = detail.get("error") if isinstance(detail, Mapping) else None
    code = error.get("code") if isinstance(error, Mapping) else None
    if not isinstance(code, str) or code not in CHAT_SEND_DYNAMIC_ERROR_CODES:
        code = fallback_code
    return chat_send_http_error(code)


__all__ = [
    "CHAT_SEND_DYNAMIC_ERROR_CODES",
    "CHAT_SEND_DYNAMIC_ERROR_STATUS",
    "chat_send_http_error",
    "chat_submission_receipt_http_error",
]
