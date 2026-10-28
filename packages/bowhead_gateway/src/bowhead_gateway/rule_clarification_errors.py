"""Finite HTTP/semantic error projection for Clarifications."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from bowhead_core.published_diagnostics import (
    RULE_CLARIFICATION_FAILURE_CODES,
    publish_rule_clarification_failure,
)
from fastapi import HTTPException, status

_ACCESS_CODES = frozenset(
    {
        "authz_lookup_failed",
        "authz_unavailable",
        "user_not_active",
        "workspace_archived",
        "workspace_required",
        "workspace_role_unknown",
        "workspace_unavailable",
    }
)
_FALLBACK_CODE = "clarification_operation_failed"


def normalize_rule_clarification_access_code(code: str) -> str:
    """Collapse dynamic authorization reasons without exposing membership detail."""

    if code.startswith("ws_access_denied:"):
        return "ws_access_denied"
    if code in _ACCESS_CODES:
        return code
    return "clarification_access_denied"


def rule_clarification_error_payload(code: str, *, clarification_stage: Literal["clarification_resume_preflight"] | None = None) -> dict[str, Any]:
    """Build a code-only, catalog-backed payload with typed diagnostics."""

    safe_code = code if code in RULE_CLARIFICATION_FAILURE_CODES else _FALLBACK_CODE
    payload: dict[str, Any] = {"code": safe_code}
    published = publish_rule_clarification_failure(safe_code, clarification_stage=clarification_stage)
    if published is not None:
        payload["diagnostics"] = published.to_wire()
    return payload


def rule_clarification_http_error(
    status_code: int,
    code: str,
    *,
    nested_error: bool = False,
    clarification_stage: Literal["clarification_resume_preflight"] | None = None,
    headers: Mapping[str, str] | None = None,
) -> HTTPException:
    """Build a localized HTTPException without accepting free-form detail."""

    payload = rule_clarification_error_payload(code, clarification_stage=clarification_stage)
    detail = {"error": payload} if nested_error else payload
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers=dict(headers) if headers is not None else None,
    )


def is_resume_probe_unavailable(exc: BaseException) -> bool:
    """Classify a read-only resume preflight failure as "answer unavailable".

    Material errors (``ValueError``) and ownership/generation conflicts (409)
    mean the answered card can no longer resume; anything else must propagate.
    Routes call this instead of importing ``HTTPException`` themselves.
    """

    if isinstance(exc, ValueError):
        return True
    return isinstance(exc, HTTPException) and exc.status_code == status.HTTP_409_CONFLICT


__all__ = [
    "normalize_rule_clarification_access_code",
    "rule_clarification_error_payload",
    "rule_clarification_http_error",
]
