"""Stable server-side rollout policy for conversational Subagent authoring."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

_TEXT_ONLY_FLAG = "FEATURE_SUBAGENT_TEXT_ONLY"
_DRAFT_CHAT_EDIT_FLAG = "FEATURE_SUBAGENT_DRAFT_CHAT_EDIT"
_ROLLOUT_SALT = "subagent-authoring-v1"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class SubagentAuthoringCapabilities:
    """Resolved authoring surfaces for one team.

    ``creator`` covers purpose collection, the right-side creation pane and
    conversational AI drafting; the remaining fields are independent rollouts.
    """

    creator: bool
    text_only: bool = False
    draft_chat_edit: bool = False
    unified_draft: bool = False


def _flag_enabled(name: str, *, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").strip().lower() in _TRUE_VALUES


def _percentage(name: str, *, default: int = 0) -> int:
    raw = os.environ.get(f"{name}_PCT", str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if 0 <= value <= 100 else 0


def _team_allowlist(name: str) -> frozenset[str]:
    return frozenset(team_id.strip() for team_id in os.environ.get(f"{name}_TEAM_IDS", "").split(",") if team_id.strip())


def stable_team_bucket(feature: str, team_id: str) -> int:
    payload = f"{_ROLLOUT_SALT}\0{feature}\0{team_id}".encode()
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big") % 100


def _team_gate(feature: str, team_id: str, *, default_enabled: bool = False) -> bool:
    if not team_id or not _flag_enabled(feature, default=default_enabled):
        return False
    if team_id in _team_allowlist(feature):
        return True
    percentage = _percentage(feature, default=100 if default_enabled else 0)
    return percentage == 100 or (percentage > 0 and stable_team_bucket(feature, team_id) < percentage)


def resolve_subagent_authoring_capabilities(
    team_id: str,
) -> SubagentAuthoringCapabilities:
    creator = True
    return SubagentAuthoringCapabilities(
        creator=creator,
        text_only=creator and _team_gate(_TEXT_ONLY_FLAG, team_id),
        draft_chat_edit=creator and _team_gate(_DRAFT_CHAT_EDIT_FLAG, team_id),
        unified_draft=bool(team_id),
    )


__all__ = [
    "SubagentAuthoringCapabilities",
    "resolve_subagent_authoring_capabilities",
    "stable_team_bucket",
]
