"""Deployment switches for language-context wiring and the execution paradigm.

Language-context wiring is enabled by default in every Space.
BOWHEAD_I18N_V1_KILL disables language directives at the deployment level.
When disabled, prompts and deterministic product copy use English defaults.
Source-data language remains independent and retains its Chinese fallback.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Environment variable name
ENV_KILL_SWITCH = "BOWHEAD_I18N_V1_KILL"


def is_kill_switch_active() -> bool:
    """Return whether the deployment disabled language-context wiring."""
    raw = os.environ.get(ENV_KILL_SWITCH, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


__all__ = [
    "ENV_KILL_SWITCH",
    "PARADIGM_V2_ENV_KILL",
    "PARADIGM_V2_WORKSPACE_KEY",
    "is_kill_switch_active",
    "is_paradigm_v2_enabled_for_workspace",
]


# ─── M158 paradigm v2 flag ────────────────────────────────────────
PARADIGM_V2_ENV_KILL = "BOWHEAD_PARADIGM_V2_KILL"
PARADIGM_V2_WORKSPACE_KEY = "paradigm_v2_enabled"


def is_paradigm_v2_kill_active() -> bool:
    """全局 kill switch (env). 任何 workspace 都不走 paradigm v2."""
    raw = os.environ.get(PARADIGM_V2_ENV_KILL, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def is_paradigm_v2_enabled_for_workspace(
    workspace_settings: dict[str, str] | None,
) -> bool:
    """workspace 级 paradigm v2 开关.

    - 全局 kill 优先
    - workspace_settings.paradigm_v2_enabled == "true" → True
    - 其他 → False (默认关, 渐进 rollout)
    """
    if is_paradigm_v2_kill_active():
        logger.info("paradigm_v2_flag.global_kill_active")
        return False
    if not workspace_settings:
        return False
    return (
        workspace_settings.get(
            PARADIGM_V2_WORKSPACE_KEY,
            "",
        )
        .strip()
        .lower()
        == "true"
    )
