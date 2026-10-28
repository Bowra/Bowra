"""Emergency rendering control for published team branding."""

from __future__ import annotations

import os
from typing import Final

ENV_TEAM_BRANDING_RENDER_SAFE_MODE: Final = "BOWHEAD_TEAM_BRANDING_RENDER_SAFE_MODE"

_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})


def is_team_branding_render_safe_mode() -> bool:
    """Return whether custom active branding must fall back during rendering."""

    return os.environ.get(ENV_TEAM_BRANDING_RENDER_SAFE_MODE, "0").strip().lower() in _TRUTHY


__all__ = [
    "ENV_TEAM_BRANDING_RENDER_SAFE_MODE",
    "is_team_branding_render_safe_mode",
]
