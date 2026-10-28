"""δ feature flags — master + 3 sub-flags.

Per tech-design §5.2 round 2 CTO-P1 split: master flag for canary rollback,
sub-flags so individual subsystems can be toggled (e.g. disable fact lookup
alone if RegisteredFactStore degrades while leaving tagging in place).

Master default ON since 0.8.5 (after impl review 17-P0 fix-up landed pre-ship).
Operational canary still recommends prod env explicit OFF for first week.

- ``BOWHEAD_PROVENANCE_HITL_ENABLED`` (master, default true)
- ``BOWHEAD_PROVENANCE_TAGS_ENABLED`` (default true) — tagging in prompt
- ``BOWHEAD_HITL_CONFIRM_ENABLED`` (default true) — Confidence Gate trigger
- ``BOWHEAD_FACT_LOOKUP_ENABLED`` (default true) — pre/post-LLM fact retrieval

Master=false → 全部 disabled. Master=true 时 sub-flags 各自走 env.
"""

from __future__ import annotations

import os

_FALSEY = ("false", "0", "no", "off")
_TRUTHY = ("true", "1", "yes", "on")


def _flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in _FALSEY:
        return False
    if raw in _TRUTHY:
        return True
    return default


def is_provenance_hitl_enabled() -> bool:
    """δ master flag — gates the entire sprint behind one env var for canary."""
    return _flag("BOWHEAD_PROVENANCE_HITL_ENABLED", default=True)


def is_feature_enabled(sub_flag_name: str) -> bool:
    """Master + sub-flag check.

    Pass the sub-flag suffix (e.g. ``"FACT_LOOKUP"``) — function expands to
    ``BOWHEAD_FACT_LOOKUP_ENABLED``. Sub-flags default ON when master is ON.
    """
    if not is_provenance_hitl_enabled():
        return False
    return _flag(f"BOWHEAD_{sub_flag_name}_ENABLED", default=True)
