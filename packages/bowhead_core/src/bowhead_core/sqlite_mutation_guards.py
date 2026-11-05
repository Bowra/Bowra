"""Version-two fences for destructive mutations, not ordinary object creation.

The version-one registry remains frozen for historical migration and readers.
These persistent row triggers request protection only when a protected value
actually changes; statement preparation and no-op updates never take a writer gate.
"""

from __future__ import annotations

from bowhead_core.sqlite_domain_runtime import _AUTHORITY

# New identities and immutable revisions cannot revoke an existing capability.
# Retention pins and deletion claims remain protected in both race directions.
_ORDINARY_INSERTS = frozenset(
    {
        "sessions",
        "turns",
        "artifacts",
        "system_agent_effective_revision",
        "team_subagent_app_compatibility",
        "team_subagent_revision",
        "team_subagent_revision_extensions",
        "team_subagent_revision_behavior",
        "system_agent_revision_behavior",
        "team_capability_revision",
    }
)
_UNPROTECTED_UPDATES = {
    "users": {"display_name", "avatar_url", "preferences_json", "locale", "timezone", "active_team_id"},
    "workspaces": {"name", "name_key", "description", "last_synced_at", "updated_at"},
    "workspace_members": {"last_active_at", "cached_name"},
    "teams": {"name", "report_timezone"},
    "sessions": {"origin"},
    "turns": {"status"},
    "team_subagent_app_compatibility": set(_AUTHORITY["core"]["team_subagent_app_compatibility"]),
    "artifacts": {"filename"},
}


def mutation_triggers(domain: str) -> tuple[str, ...]:
    """Install versioned fences without changing historical UDF semantics."""
    statements = []
    for table, original_columns in sorted(_AUTHORITY.get(domain, {}).items()):
        operations = [("delete", "DELETE", "")]
        if table not in _ORDINARY_INSERTS:
            operations.append(("insert", "INSERT", ""))
        columns = [column for column in original_columns if column not in _UNPROTECTED_UPDATES.get(table, set())]
        if columns:
            names = ",".join(f'"{column}"' for column in columns)
            changed = " OR ".join(f'OLD."{column}" IS NOT NEW."{column}"' for column in columns)
            operations.append(("update", f"UPDATE OF {names}", f" WHEN {changed}"))
        for suffix, operation, condition in operations:
            statements.append(
                f'CREATE TRIGGER "trg_domain_mutation_{table}_{suffix}" BEFORE {operation} ON "{table}"{condition} '
                f"BEGIN SELECT bowhead_domain_mutation_v2('{table}','{suffix}'); END"
            )
    return tuple(statements)
