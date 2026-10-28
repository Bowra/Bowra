"""workspace-datarc-decouple P1 R7 — fail-closed workspace access authz (flag-gated).

The single-tenant global-datarc-client era let ``POST /chat/send`` accept ANY
``body.workspace_id`` and auto-enroll the caller as a member. Benign today (one effective
tenant, one shared datarc key), but it becomes **cross-tenant access** once ③ makes datarc
connections per-team (P2a) — a user could send another team's ws_* and run against its data.
This module gates that BEFORE per-connection keys land.

Flag ``BOWHEAD_WS_MEMBERSHIP_ENFORCE`` (default OFF):
  - OFF → exactly today's behavior (auto-enroll on first send; honor body.member_uid).
  - ON  → fail-closed: caller must be an active member of the workspace AND the workspace
          must belong to the caller's team; client-supplied member_uid is ignored.
Flip ON **after** the 1c migration (workspace_id is then a canonical ws_* with a workspaces
row carrying team_id) and **before** P2a per-connection keys. Under prod AUTH_MODE=dev the
team_id can be a stub — keep OFF until real auth, hence the flag (see PRD R7 + the focused review).
"""

from __future__ import annotations

import os

_ENFORCE_ENV = "BOWHEAD_WS_MEMBERSHIP_ENFORCE"


def ws_membership_enforce_enabled() -> bool:
    """True iff the workspace-membership authz flag is on (default off)."""
    return os.environ.get(_ENFORCE_ENV, "0").strip() == "1"


def authorize_ws_access(
    *,
    enforce: bool,
    existing_role: str | None,
    ws_team_id: str | None,
    caller_team_id: str | None,
) -> str | None:
    """Decide whether ``caller`` may send to a workspace. Returns ``None`` when allowed,
    else a short deny-reason code.

    - ``enforce=False`` → always allowed (today's behavior; enrollment happens elsewhere).
    - ``enforce=True``  → fail-closed:
        * no caller team              → ``"no_team"`` (deny; never trust an team-less caller)
        * ws_team_id known & mismatch → ``"cross_team"``
        * caller not an active member → ``"not_a_member"``
      ``ws_team_id=None`` (workspace row not found, e.g. pre-migration datarc id) skips only
      the team-equality leg — membership is still required, so it stays fail-closed.
    """
    if not enforce:
        return None
    if not caller_team_id:
        return "no_team"
    if ws_team_id is not None and ws_team_id != caller_team_id:
        return "cross_team"
    if not existing_role:
        return "not_a_member"
    return None
