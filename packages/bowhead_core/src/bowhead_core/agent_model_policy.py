"""Single source of truth for user-authored Team Agent model roles."""

from __future__ import annotations

TEAM_SUBAGENT_ALLOWED_MODEL_ROLES: frozenset[str] = frozenset({"analyst"})
TEAM_SUBAGENT_DEFAULT_MODEL_ROLE = "analyst"
TEAM_SUBAGENT_MODEL_POLICY_VERSION = "team-agent-model-policy-v1"

if TEAM_SUBAGENT_DEFAULT_MODEL_ROLE not in TEAM_SUBAGENT_ALLOWED_MODEL_ROLES:
    raise RuntimeError("default Team Agent model role must be allowed")


def team_agent_model_role(model: object) -> str:
    """The model role a Team Agent actually runs as, from its stored ``model``.

    The runtime clamps anything outside the allowlist to the default role, so every check of
    "can this Agent get a model" (evaluation preflight, model hashes) must ask for this role too,
    not the raw stored value.
    """

    return model if isinstance(model, str) and model in TEAM_SUBAGENT_ALLOWED_MODEL_ROLES else TEAM_SUBAGENT_DEFAULT_MODEL_ROLE


__all__ = [
    "TEAM_SUBAGENT_ALLOWED_MODEL_ROLES",
    "TEAM_SUBAGENT_DEFAULT_MODEL_ROLE",
    "TEAM_SUBAGENT_MODEL_POLICY_VERSION",
    "team_agent_model_role",
]
