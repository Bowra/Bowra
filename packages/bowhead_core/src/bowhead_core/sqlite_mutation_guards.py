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
# Columns whose change can never revoke access, reassign scope, delete, or race a retention pin or
# purge claim. ``workspaces.revision`` is the Space metadata edit counter (optimistic concurrency and
# same-file scope snapshots; no cross-file predicate reads it) and ``builtin_tool_user_grant.last_used_at``
# a usage timestamp; revision ``0230_unfence_routine_metadata`` applies both to deployed databases.
_UNPROTECTED_UPDATES = {
    "users": {"display_name", "avatar_url", "preferences_json", "locale", "timezone", "active_team_id"},
    "workspaces": {"name", "name_key", "description", "last_synced_at", "updated_at", "revision"},
    "builtin_tool_user_grant": {"last_used_at"},
    "workspace_members": {"last_active_at", "cached_name"},
    "teams": {"name", "report_timezone"},
    "sessions": {"origin"},
    "turns": {"status"},
    "team_subagent_app_compatibility": set(_AUTHORITY["core"]["team_subagent_app_compatibility"]),
    "artifacts": {"filename"},
}
# Key/value tables whose rows are authority only under some keys: a row is fenced only when its key,
# before or after the change, is listed. No cross-file trigger or view reads ``user_settings`` and its
# readers are core-file or single-statement snapshots, so a preference such as the knowledge-base scope
# filter or the interface language cannot race a revocation; a running Turn's shared core lease starved
# such a save for 9.7 s on 116 (1.7.59). The kept keys pause memory consolidation (privacy) and disable
# Plugins for the person (access), so a later cross-file admission reading them stays protected.
# Revision ``0231_user_settings_key_fence`` applies this to deployed databases.
_FENCED_KEYS = {
    "user_settings": ("key", ("builtin_plugins_disabled", "memory_dreaming_paused", "team_plugins_disabled")),
}


def _fenced_key(table: str, row: str) -> str | None:
    """The condition limiting a key/value table's fence to its authority keys, for the ``OLD`` or ``NEW`` row."""
    scope = _FENCED_KEYS.get(table)
    if scope is None:
        return None
    column, keys = scope
    listed = ",".join("'" + key + "'" for key in keys)
    return f'{row}."{column}" IN ({listed})'


def mutation_triggers(domain: str) -> tuple[str, ...]:
    """Install versioned fences without changing historical UDF semantics."""
    statements = []
    for table, original_columns in sorted(_AUTHORITY.get(domain, {}).items()):
        old_key, new_key = _fenced_key(table, "OLD"), _fenced_key(table, "NEW")
        operations = [("delete", "DELETE", f" WHEN {old_key}" if old_key else "")]
        if table not in _ORDINARY_INSERTS:
            operations.append(("insert", "INSERT", f" WHEN {new_key}" if new_key else ""))
        columns = [column for column in original_columns if column not in _UNPROTECTED_UPDATES.get(table, set())]
        if columns:
            names = ",".join(f'"{column}"' for column in columns)
            changed = " OR ".join(f'OLD."{column}" IS NOT NEW."{column}"' for column in columns)
            if old_key:
                changed = f"({old_key} OR {new_key}) AND ({changed})"
            operations.append(("update", f"UPDATE OF {names}", f" WHEN {changed}"))
        for suffix, operation, condition in operations:
            statements.append(
                f'CREATE TRIGGER "trg_domain_mutation_{table}_{suffix}" BEFORE {operation} ON "{table}"{condition} '
                f"BEGIN SELECT bowhead_domain_mutation_v2('{table}','{suffix}'); END"
            )
    return tuple(statements)
