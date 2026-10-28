"""Analysis-depth tuning — one user-facing knob (fast / standard / thorough)
that maps to a PROFILE of internal agent budgets, instead of exposing raw
iteration numbers to admins.

Rationale (2026-06-17): the peer no-commit bail caps were raw env vars; as models
get stronger the "right" thoroughness shifts, and re-teaching admins new numbers
is bad UX. An admin instead picks an intent ("how thorough should agents be") and
the system owns the internals — re-map the profiles centrally when models change.

Resolution layering (most specific wins):
  explicit ops env override (BOWHEAD_PEER_HARD_BAIL_ITER / _FORCE_BAIL_ITER)
    → the turn's depth profile (from the team/workspace setting, via ContextVar)
      → the standard-profile default.

The depth is a turn-scoped ContextVar (set at turn start from the team setting),
so it propagates to the copied contexts of spawned peers like the other
per-turn plumbing in `_lead_context`.
"""

from __future__ import annotations

import contextvars
import os
from typing import Literal

AnalysisDepth = Literal["fast", "standard", "thorough"]

# team_settings_store key (generic k/v) — the admin-set depth for the team.
KEY_TEAM_ANALYSIS_DEPTH = "analysis_depth"

# depth → internal budget profile. Keep values here; this is the ONE place that
# changes when model capability shifts. `peer_hard_bail` = soft "commit now"
# inject iter; `peer_force_bail` = hard force-stop iter (subagent.py).
_PROFILES: dict[str, dict[str, int]] = {
    "fast": {"peer_hard_bail": 14, "peer_force_bail": 20},
    "standard": {"peer_hard_bail": 24, "peer_force_bail": 32},
    "thorough": {"peer_hard_bail": 34, "peer_force_bail": 46},
}
_DEFAULT_DEPTH = "standard"

# display labels for the admin UI (key is the stored value).
DEPTH_LABELS: dict[str, str] = {
    "fast": "快速",
    "standard": "标准",
    "thorough": "深度",
}


def normalize_depth(value: str | None) -> AnalysisDepth:
    """Coerce any stored/env value to a valid depth key; unknown → standard."""
    v = (value or "").strip().lower()
    return v if v in _PROFILES else _DEFAULT_DEPTH  # type: ignore[return-value]


# turn-scoped current depth. Default from env so a deploy can still pin it
# globally; the team setting overrides it per turn via set_analysis_depth.
_ANALYSIS_DEPTH_CONTEXTVAR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "analysis_depth",
    default=normalize_depth(os.environ.get("BOWHEAD_ANALYSIS_DEPTH")),  # noqa: B039
)


def set_analysis_depth(depth: str | None) -> contextvars.Token:
    """Set the turn's depth (orchestrator/gateway sets this from the team setting)."""
    return _ANALYSIS_DEPTH_CONTEXTVAR.set(normalize_depth(depth))


def get_analysis_depth() -> AnalysisDepth:
    return normalize_depth(_ANALYSIS_DEPTH_CONTEXTVAR.get())


def reset_analysis_depth(token: contextvars.Token) -> None:
    _ANALYSIS_DEPTH_CONTEXTVAR.reset(token)


def resolve_peer_bail_iters() -> tuple[int, int]:
    """(soft hard-bail iter, hard force-bail iter) for the current turn.

    Explicit env vars still win (ops escape hatch); otherwise the depth profile;
    default = standard. Mirrors the old `os.environ.get(..., "24"/"32")` reads but
    depth-aware."""
    prof = _PROFILES[get_analysis_depth()]
    hard = int(os.environ.get("BOWHEAD_PEER_HARD_BAIL_ITER", prof["peer_hard_bail"]))
    force = int(os.environ.get("BOWHEAD_PEER_FORCE_BAIL_ITER", prof["peer_force_bail"]))
    return hard, force
