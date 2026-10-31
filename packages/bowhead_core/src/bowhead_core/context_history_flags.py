"""Legacy history controls for callers without owned context management.

Qualified executions use cursor-based history and the model's context policy.
These environment settings apply only to the compatibility assembly path.
Its 40,000-token default drops complete old Turns with 0.75 hysteresis; the
Lead caller additionally bounds it by a quarter of the declared model window.
Settings are read during Turn assembly, never midway through the model loop.
"""

from __future__ import annotations

import os

_ENV = "BOWHEAD_CTX_TURN_HISTORY"
_ENV_TOKENS = "BOWHEAD_CTX_TURN_HISTORY_TOKENS"
DEFAULT_HISTORY_TOKENS = 40_000
HISTORY_HYSTERESIS = 0.75


def turn_history_enabled() -> bool:
    """Enable legacy Turn history by default; an explicit zero selects digest mode."""
    return os.environ.get(_ENV, "1").strip() == "1"


def turn_history_token_budget() -> int:
    """Return the legacy token budget, using the default for invalid values."""
    raw = os.environ.get(_ENV_TOKENS, "").strip()
    try:
        v = int(raw) if raw else DEFAULT_HISTORY_TOKENS
    except ValueError:
        return DEFAULT_HISTORY_TOKENS
    return v if v > 0 else DEFAULT_HISTORY_TOKENS


__all__ = ["DEFAULT_HISTORY_TOKENS", "HISTORY_HYSTERESIS", "turn_history_enabled", "turn_history_token_budget"]
