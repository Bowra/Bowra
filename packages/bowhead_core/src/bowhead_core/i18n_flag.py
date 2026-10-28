"""Gate language-context wiring with Space settings and a global kill switch.

Precedence: BOWHEAD_I18N_V1_KILL disables the wiring globally; an explicit
Space i18n_v1=false disables it locally; otherwise it is enabled. The gate
controls language directives and does not change interface preferences.
When disabled, prompts and deterministic product copy use English defaults.
Source-data language remains independent and retains its Chinese fallback.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Environment variable name
ENV_KILL_SWITCH = "BOWHEAD_I18N_V1_KILL"

# workspace_settings key (matches workspace_settings_store.KEY_I18N_V1_ENABLED).
WORKSPACE_FLAG_KEY = "i18n_v1"


def is_kill_switch_active() -> bool:
    """全局 kill switch 状态. 环境变量为 truthy 值（'1', 'true' 不区分大小写）."""
    raw = os.environ.get(ENV_KILL_SWITCH, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def is_i18n_enabled_for_workspace(
    workspace_settings: dict[str, str] | None,
) -> bool:
    """workspace 级开关检查（**默认开**，2026-08-18 起）.

    - 全局 kill 优先: 任何情况下 kill 开 → return False
    - workspace_settings.i18n_v1 == "false" → False（显式关掉这一个 workspace）
    - 其他（缺失 / "true" / 任意值）→ True
    """
    if is_kill_switch_active():
        logger.info("i18n_flag.global_kill_active")
        return False
    if not workspace_settings:
        return True
    return workspace_settings.get(WORKSPACE_FLAG_KEY, "").strip().lower() != "false"


__all__ = [
    "ENV_KILL_SWITCH",
    "PARADIGM_V2_ENV_KILL",
    "PARADIGM_V2_WORKSPACE_KEY",
    "WORKSPACE_FLAG_KEY",
    "is_i18n_enabled_for_workspace",
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
