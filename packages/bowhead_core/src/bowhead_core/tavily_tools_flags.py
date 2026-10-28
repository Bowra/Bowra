"""Rollout switch for the Tavily-backed web tools (web_extract / web_map, later web_crawl / web_deep_research)."""

from __future__ import annotations

import os
from typing import Final

ENV_TAVILY_TOOLS: Final = "FEATURE_TAVILY_TOOLS"
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})


def tavily_tools_enabled() -> bool:
    """Whether the Tavily web tools are registered at startup.

    Off by default: each call spends the team's own Tavily credits, and the tools
    need a gray period before release (docs/灰度功能清单.md). Read only at the
    registration point, so flipping it takes effect on restart.
    """

    return os.environ.get(ENV_TAVILY_TOOLS, "0").strip().lower() in _TRUTHY


__all__ = [
    "ENV_TAVILY_TOOLS",
    "tavily_tools_enabled",
]
