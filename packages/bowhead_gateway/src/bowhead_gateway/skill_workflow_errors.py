"""Finite HTTP error projection for Skills and Workflow browser APIs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from bowhead_core.published_diagnostics import (
    SKILL_WORKFLOW_FAILURE_CODES,
    publish_skill_workflow_failure,
)
from fastapi import HTTPException

_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_INTEGER_PARAMS = frozenset(
    {
        "max_count",
        "max_entries",
        "max_kb",
        "max_len",
        "max_mb",
    }
)
_PARAMS_BY_CODE: Mapping[str, frozenset[str]] = {
    "E_SKILL_DESCRIPTION_TOO_LONG": frozenset({"max_len"}),
    "E_SKILL_LIMIT_REACHED": frozenset({"max_count"}),
    "E_SKILL_MANIFEST_TOO_LARGE": frozenset({"max_kb"}),
    "E_SKILL_NAME_EXISTS": frozenset({"name"}),
    "E_SKILL_NAME_TAKEN_BY_OTHER": frozenset({"name"}),
    "E_SKILL_RESERVED_NAME": frozenset({"name"}),
    "E_SKILL_SCRIPT_TOO_LARGE": frozenset({"max_kb"}),
    "E_SKILL_STORAGE_LIMIT_REACHED": frozenset({"max_mb"}),
    "E_SKILL_TOO_LARGE": frozenset({"max_mb"}),
    "E_SKILL_ZIP_TOO_MANY_ENTRIES": frozenset({"max_entries"}),
    "resource_grant_denied": frozenset(
        {"permission", "resource_id", "resource_kind"},
    ),
}


def project_skill_workflow_error_params(
    code: str,
    params: Mapping[str, Any],
) -> dict[str, str | int]:
    """Keep only bounded catalog parameters registered for ``code``."""

    allowed = _PARAMS_BY_CODE.get(code, frozenset())
    projected: dict[str, str | int] = {}
    for key in allowed:
        value = params.get(key)
        if key in _INTEGER_PARAMS:
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000:
                projected[key] = value
            continue
        if not isinstance(value, str):
            continue
        if key == "author":
            normalized = _CONTROL_RE.sub(" ", value).strip()[:128]
            if normalized:
                projected[key] = normalized
            continue
        if _SAFE_IDENTIFIER_RE.fullmatch(value):
            projected[key] = value
    return projected


def skill_workflow_http_error(
    status_code: int,
    code: str,
    *,
    nested_error: bool = False,
    **params: Any,
) -> HTTPException:
    """Build a catalog-backed error with typed, finite diagnostics.

    Only code-specific, bounded catalog parameters are retained in the response.
    Unknown codes and parameters fail closed.
    """

    if code not in SKILL_WORKFLOW_FAILURE_CODES:
        code = "skill_bundle_invalid"
        params = {}
    error: dict[str, Any] = {
        "code": code,
        **project_skill_workflow_error_params(code, params),
    }
    published = publish_skill_workflow_failure(code)
    if published is not None:
        error["diagnostics"] = published.to_wire()
    detail = {"error": error} if nested_error else error
    return HTTPException(status_code=status_code, detail=detail)


__all__ = [
    "project_skill_workflow_error_params",
    "skill_workflow_http_error",
]
