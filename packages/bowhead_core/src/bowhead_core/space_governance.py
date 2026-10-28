"""Space governance domain primitives.

The persisted protocol keeps the historical ``workspace_*`` spelling.  This
module owns the user-facing Space rules that must not drift between routes and
store transactions.
"""

from __future__ import annotations

import os
import unicodedata
from enum import StrEnum
from typing import Any


class SpaceOperation(StrEnum):
    """Lifecycle-sensitive operation classes for Space-scoped requests."""

    READ_HISTORY = "read_history"
    WRITE = "write"
    EXECUTE = "execute"


class SpaceGovernanceError(ValueError):
    """Stable domain failure safe to map to the public API."""

    def __init__(
        self,
        code: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.details = details
        super().__init__(code)


def normalize_space_name(value: str) -> tuple[str, str]:
    """Return the canonical display name and case-insensitive uniqueness key."""

    display_name = unicodedata.normalize("NFKC", value).strip()
    if not display_name:
        raise SpaceGovernanceError("workspace_name_required")
    return display_name, display_name.casefold()


def normalize_space_role(value: str | None) -> str | None:
    """Normalize supported historical role aliases to the canonical role."""

    normalized = str(value or "").strip().lower()
    if normalized in {"admin", "ws_admin", "owner", "workspace_admin"}:
        return "admin"
    if normalized in {"member", "viewer"}:
        return normalized
    return None


def space_admin_v2_enabled() -> bool:
    """Runtime M1/M2 UI capability; secure M0 contracts never depend on it."""

    return os.environ.get("FEATURE_SPACE_ADMIN_V2", "1").strip().lower() in {
        "1",
        "true",
        "yes",
    }
