from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from bowhead_core.builtin_datarc_scope import (
    datarc_scope_is_expired,
    normalize_datarc_scope,
    supported_datarc_operations,
)


def test_scope_is_sanitized_and_permission_matrix_is_minimal() -> None:
    scope = normalize_datarc_scope(
        {
            "id": "key-1",
            "name": "只读",
            "desc": "finance",
            "permissions": ["workspace", "account", "unknown"],
            "workspaces": [{"id": 7, "name": "财务"}],
            "secret": "must-not-persist",
        }
    )
    tool = SimpleNamespace(scope_state="verified", scope=scope)

    assert "secret" not in scope
    assert supported_datarc_operations(tool) == frozenset({"datarc_workspaces", "datarc_schema"})


def test_empty_permissions_mean_all_known_operations() -> None:
    scope = normalize_datarc_scope({"id": "key-1", "permissions": [], "workspaces": []})
    assert supported_datarc_operations(SimpleNamespace(scope_state="verified", scope=scope)) == frozenset(
        {"datarc_workspaces", "datarc_schema", "datarc_query"}
    )


def test_unknown_or_expired_scope_fails_closed() -> None:
    scope = normalize_datarc_scope({"id": "key-1", "expired_at": "2025-01-01T00:00:00Z", "permissions": [], "workspaces": []})
    assert datarc_scope_is_expired(scope, now=datetime(2026, 1, 1, tzinfo=UTC))
    assert not supported_datarc_operations(SimpleNamespace(scope_state="unknown", scope=scope))
    assert not supported_datarc_operations(SimpleNamespace(scope_state="verified", scope=scope))


def test_malformed_scope_is_rejected() -> None:
    with pytest.raises(ValueError, match="datarc_scope_invalid"):
        normalize_datarc_scope({"id": "key-1", "permissions": None, "workspaces": []})
