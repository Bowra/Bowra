"""Canonical Datarc key-scope projection for built-in team tools."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

DATARC_KNOWN_PERMISSIONS = frozenset({"auth", "account", "workspace", "data", "task", "embed"})
DATARC_OPERATION_REQUIREMENTS: dict[str, frozenset[str]] = {
    "datarc_workspaces": frozenset({"account", "workspace"}),
    "datarc_schema": frozenset({"account", "workspace"}),
    "datarc_query": frozenset({"account", "workspace", "data", "task"}),
}


def _bounded_workspaces(items: list[Any]) -> list[dict[str, str]]:
    workspaces: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        workspace_id = str(item.get("id") or "").strip()
        if not workspace_id or workspace_id in seen_ids:
            continue
        seen_ids.add(workspace_id)
        workspaces.append(
            {
                "id": workspace_id[:128],
                "name": str(item.get("name") or workspace_id).strip()[:160],
                "desc": str(item.get("desc") or "").strip()[:500],
            }
        )
        if len(workspaces) >= 1000:
            break
    return workspaces


def normalize_datarc_scope(raw: object) -> dict[str, Any]:
    """Whitelist and bound the persisted subset of ``GET /openapi/v1/scopes/``."""

    if not isinstance(raw, dict):
        raise ValueError("datarc_scope_invalid")
    permissions_raw = raw.get("permissions")
    workspaces_raw = raw.get("workspaces")
    if not isinstance(permissions_raw, list) or not isinstance(workspaces_raw, list):
        raise ValueError("datarc_scope_invalid")
    key_id = str(raw.get("id") or "").strip()
    if not key_id:
        raise ValueError("datarc_scope_invalid")
    permissions = sorted({str(item).strip().lower()[:64] for item in permissions_raw if str(item).strip()})
    if len(permissions) > 32:
        raise ValueError("datarc_scope_too_large")
    workspaces = _bounded_workspaces(workspaces_raw)
    expired_at_raw = raw.get("expired_at")
    return {
        "schema_version": 1,
        "key_id": key_id[:128],
        "key_name": str(raw.get("name") or "").strip()[:160],
        "key_description": str(raw.get("desc") or "").strip()[:500],
        "expired_at": str(expired_at_raw).strip()[:64] if expired_at_raw else None,
        "permissions": permissions,
        "workspaces": workspaces,
    }


def datarc_scope_from_listed_workspaces(workspaces: list[dict[str, Any]]) -> dict[str, Any]:
    """Scope snapshot for Datarc instances that do not serve ``GET /openapi/v1/scopes/``.

    Such instances cannot declare the key's interface permissions. The snapshot keeps the
    existing "no declared permissions" semantics of :func:`supported_datarc_operations`
    (all read-only operations; Datarc still enforces the key server-side) and limits
    workspaces to what the same key lists via ``GET /openapi/v1/workspaces/`` — the
    contract every connection had before key scopes existed. ``source`` records that the
    snapshot was derived rather than declared.
    """

    return {
        "schema_version": 1,
        "source": "workspaces",
        "key_id": "",
        "key_name": "",
        "key_description": "",
        "expired_at": None,
        "permissions": [],
        "workspaces": _bounded_workspaces(workspaces),
    }


def datarc_scope_fingerprint(scope: dict[str, Any]) -> str:
    payload = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def datarc_scope_is_expired(scope: dict[str, Any], *, now: datetime | None = None) -> bool:
    value = scope.get("expired_at")
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed <= (now or datetime.now(UTC))


def supported_datarc_operations(tool: Any) -> frozenset[str]:
    if getattr(tool, "scope_state", "unknown") != "verified":
        return frozenset()
    scope = getattr(tool, "scope", None)
    if not isinstance(scope, dict) or datarc_scope_is_expired(scope):
        return frozenset()
    configured = {str(item) for item in scope.get("permissions") or []}
    effective = DATARC_KNOWN_PERMISSIONS if not configured else configured.intersection(DATARC_KNOWN_PERMISSIONS)
    return frozenset(operation for operation, required in DATARC_OPERATION_REQUIREMENTS.items() if required.issubset(effective))


__all__ = [
    "DATARC_KNOWN_PERMISSIONS",
    "DATARC_OPERATION_REQUIREMENTS",
    "datarc_scope_fingerprint",
    "datarc_scope_from_listed_workspaces",
    "datarc_scope_is_expired",
    "normalize_datarc_scope",
    "supported_datarc_operations",
]
