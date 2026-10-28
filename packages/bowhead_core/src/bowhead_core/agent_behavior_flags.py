"""Rollout controls for revision-bound Agent behavior readers."""

from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def agent_behavior_m3_enabled() -> bool:
    """Return whether new behavior readers may affect runtime selection."""
    return os.environ.get("FEATURE_AGENT_BEHAVIOR_M3", "0").strip().lower() in _TRUTHY


__all__ = ["agent_behavior_m3_enabled"]
