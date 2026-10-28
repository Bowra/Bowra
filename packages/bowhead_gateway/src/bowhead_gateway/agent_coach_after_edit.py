"""The user's standing choice of what Coach does after it writes an Agent draft."""

from __future__ import annotations

from typing import Literal

KEY_AGENT_COACH_AFTER_EDIT = "agent_coach_after_edit"
AGENT_COACH_AFTER_EDIT_VALUES = frozenset({"check", "ask"})
CoachAfterEdit = Literal["check", "ask"]


def coach_after_edit(value: str | None) -> CoachAfterEdit:
    """Unset or unknown values mean the default: check after writing."""
    return "ask" if value == "ask" else "check"
