"""Feature flags for editable design (multimodal V2A)."""

from __future__ import annotations

import os

_FALSY = {"0", "false", "no", "off"}
_FLAG = "BOWHEAD_DESIGN_V2A"


def is_design_v2a_enabled() -> bool:
    """Production-on switch for creating/updating/flattening editable designs.

    V2A is now the default product path. Keep an explicit env kill-switch for
    emergency rollback, but do not require a rollout flag for normal launch.
    """

    return os.environ.get(_FLAG, "1").strip().lower() not in _FALSY


__all__ = ["is_design_v2a_enabled"]
