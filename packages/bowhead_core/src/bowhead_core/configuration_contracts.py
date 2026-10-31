"""Admission metadata for Configuration Agent domains, independent of stored values."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigurationDomain:
    skill: str
    scope: str
    page: str
    description: str


DOMAINS = {
    "workspace_i18n": ConfigurationDomain(
        "settings-space-preferences", "space", "/space/settings/general/", "Current Space week start and source-data language."
    ),
    "workspace_metadata": ConfigurationDomain("settings-space-metadata", "space", "/space/settings/general/", "Current Space name and description."),
    "space_instructions": ConfigurationDomain("settings-space-instructions", "space", "/space/settings/instructions/", "Current Space instructions."),
    "personal_appearance": ConfigurationDomain(
        "settings-personal-appearance", "user", "/settings/appearance/", "Personal appearance and supported scalar resets."
    ),
    "user_language": ConfigurationDomain(
        "settings-language-preferences", "user", "/settings/profile/", "Persistent AI-answer language; not interface language."
    ),
    "team_run_limits": ConfigurationDomain(
        "settings-team-administration",
        "team",
        "/team/settings/subagents/",
        "Team Agent loop limits: turn duration, model steps, tool calls and model-call time; restore Team defaults. Deployment ceilings remain effective.",
    ),
    "team_execution_limits": ConfigurationDomain(
        "settings-team-administration",
        "team",
        "/team/settings/subagents/",
        "Team sandbox execution durations and HTML size limit; restore deployment defaults.",
    ),
    "team_model_config": ConfigurationDomain(
        "settings-team-administration",
        "team",
        "/team/settings/models/",
        "Team model declarations, runtime parameters and assignments; original model Review/Apply required.",
    ),
    "team_model_retry": ConfigurationDomain("settings-team-administration", "team", "/team/settings/models/", "Team model-call retry policy."),
}

TEAM_FORMS = frozenset(name for name, domain in DOMAINS.items() if domain.scope == "team")
