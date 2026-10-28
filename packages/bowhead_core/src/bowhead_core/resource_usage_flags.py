"""Rollout switch for resource impact confirmation."""

from __future__ import annotations

import os
from typing import Final

ENV_RESOURCE_IMPACT_GUARD: Final = "FEATURE_RESOURCE_IMPACT_GUARD"
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})


def resource_impact_guard_enabled() -> bool:
    """Whether protected Skill / Tool mutations require a fresh impact token.

    The guard stays opt-in until preview, atomic mutation and dispatch-time
    revocation have completed dogfood together.  Keeping this separate from
    the released visibility behavior prevents a UI rollout from silently
    changing mutation semantics.
    """

    return os.environ.get(ENV_RESOURCE_IMPACT_GUARD, "0").strip().lower() in _TRUTHY


__all__ = [
    "ENV_RESOURCE_IMPACT_GUARD",
    "resource_impact_guard_enabled",
]
