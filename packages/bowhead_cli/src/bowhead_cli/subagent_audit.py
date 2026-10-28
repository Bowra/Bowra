"""Read-only Stage 0A audit for team Subagent migration impact."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bowhead_core.agents._generated.role_bindings import _PARADIGM_V2_ROLE_MAP
from bowhead_core.custom_subagent_store import (
    _NAME_RE,
    DESTRUCTIVE_TOOL_PREFIXES,
    REPORT_WRITE_TOOLS,
)
from bowhead_core.openapi_tool_source import op_tool_name, parse_openapi_ops
from bowhead_core.tool_registry import build_default_registry

from bowhead_skill.catalog import builtin_skill_names

_VALID_AUDIENCE_MODES = frozenset({"private", "custom", "teamwide"})
_VALID_VALIDATION_STATUSES = frozenset({"valid", "warning", "invalid"})


@dataclass(frozen=True)
class SubagentAuditReport:
    """Safe aggregate report; never contains Subagent content or roster identities."""

    schema_version: int
    generated_at: str
    team_id: str
    schema_capabilities: dict[str, bool]
    totals: dict[str, Any]
    findings: dict[str, int | None]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SubagentMigrationImpactReport:
    """Read-only Release B dry-run; contains only non-identifying aggregates."""

    schema_version: int
    report_kind: str
    generated_at: str
    team_id: str
    dry_run: bool
    writes_performed: int
    semantic_sources: dict[str, str]
    schema_capabilities: dict[str, bool]
    completeness: dict[str, Any]
    totals: dict[str, int | None]
    actions: dict[str, Any]
    follow_up: dict[str, int | None]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_sqlite_path(db_url: str | None) -> Path:
    """Resolve the CLI DB option without accepting a non-SQLite connection."""
    default_path = Path(".data/sqlite/bowhead.db").resolve()
    if not db_url:
        import os

        db_url = os.environ.get("BOWHEAD_DB_URL")
    if not db_url:
        return default_path
    if db_url.startswith(("sqlite:///", "sqlite+aiosqlite:///")):
        raw_path = db_url.split("///", 1)[1]
        if raw_path == ":memory:":
            raise ValueError("audit-subagents requires a file-backed SQLite database")
        return Path(raw_path).expanduser().resolve()
    if "://" in db_url:
        raise ValueError("audit-subagents only supports SQLite databases")
    return Path(db_url).expanduser().resolve()


def audit_subagents(
    db_path: Path,
    *,
    team_id: str,
    now: datetime | None = None,
) -> SubagentAuditReport:
    """Audit one team's Subagent rows through a hard read-only SQLite connection."""
    if not team_id.strip():
        raise ValueError("team_id is required")
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        _require_table(connection, "team_subagent")
        _require_columns(
            connection,
            "team_subagent",
            {
                "id",
                "team_id",
                "name",
                "tool_refs_json",
                "skills_json",
                "output_schema_json",
                "visibility",
                "created_by",
                "status",
            },
        )
        if _table_exists(connection, "teams"):
            found = connection.execute("SELECT 1 FROM teams WHERE id=?", (team_id,)).fetchone()
            if found is None:
                raise ValueError(f"team not found: {team_id}")
        return _build_report(connection, team_id=team_id, now=checked_at)
    finally:
        connection.close()


def report_subagent_migration_impact(
    db_path: Path,
    *,
    team_id: str,
    now: datetime | None = None,
) -> SubagentMigrationImpactReport:
    """Preview deterministic Release B repairs without changing source rows."""
    if not team_id.strip():
        raise ValueError("team_id is required")
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        _require_table(connection, "team_subagent")
        _require_columns(
            connection,
            "team_subagent",
            {
                "id",
                "team_id",
                "name",
                "tool_refs_json",
                "skills_json",
                "output_schema_json",
                "visibility",
                "created_by",
                "status",
            },
        )
        if _table_exists(connection, "teams"):
            found = connection.execute(
                "SELECT 1 FROM teams WHERE id=?",
                (team_id,),
            ).fetchone()
            if found is None:
                raise ValueError(f"team not found: {team_id}")
        audit_report = _build_report(connection, team_id=team_id, now=checked_at)
        return _build_migration_impact_report(
            connection,
            audit_report=audit_report,
            team_id=team_id,
            now=checked_at,
        )
    finally:
        connection.close()


def _build_report(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    now: datetime,
) -> SubagentAuditReport:
    subagent_columns = _columns(connection, "team_subagent")
    owner_semantics_active, audience_semantics_active = _semantic_activation(connection)
    capabilities = {
        "owner_user_id": "owner_user_id" in subagent_columns,
        "audience_mode": "audience_mode" in subagent_columns,
        "owner_semantics_active": owner_semantics_active,
        "audience_semantics_active": audience_semantics_active,
        "owner_membership": _has_columns(connection, "users", {"id", "status"})
        and _has_columns(connection, "team_members", {"user_id", "team_id", "status"}),
        "audience_principals": _has_columns(connection, "users", {"id", "status"})
        and _has_columns(connection, "team_members", {"user_id", "team_id", "status"})
        and _has_columns(connection, "groups", {"id", "team_id", "status"}),
        "resource_grants": _has_columns(
            connection,
            "resource_grants",
            {
                "team_id",
                "resource_kind",
                "resource_id",
                "principal_type",
                "principal_id",
                "effect",
                "expires_at",
                "revoked_at",
            },
        ),
        "team_tool_source": _has_columns(
            connection,
            "team_tool_source",
            {
                "id",
                "team_id",
                "kind",
                "status",
                "openapi_spec_json",
                "selected_op_ids_json",
                "tools_schema_json",
            },
        ),
        "team_skill": _has_columns(
            connection,
            "team_skill",
            {"team_id", "name", "status", "visibility"},
        ),
        "validation": _has_columns(
            connection,
            "team_subagent_validation",
            {"team_id", "subagent_id", "status"},
        ),
    }
    subagents = list(
        connection.execute(
            "SELECT * FROM team_subagent WHERE team_id=? ORDER BY id",
            (team_id,),
        )
    )
    grants, malformed_expiry_count = _active_allow_grants(
        connection,
        team_id=team_id,
        now=now,
        available=capabilities["resource_grants"],
    )
    owner_states = _owner_states(
        connection,
        team_id=team_id,
        available=capabilities["owner_membership"],
    )
    valid_users, valid_groups = _valid_audience_principals(
        connection,
        team_id=team_id,
        available=capabilities["audience_principals"],
    )
    active_tool_names, inactive_tool_names = _team_tool_names(
        connection,
        team_id=team_id,
        available=capabilities["team_tool_source"],
    )
    skill_states = _team_skill_states(
        connection,
        team_id=team_id,
        available=capabilities["team_skill"],
    )
    native_tool_names = _native_tool_allowlist()
    builtins = builtin_skill_names()

    audience_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    owner_state_counts: Counter[str] = Counter()
    names: Counter[str] = Counter()
    affected: dict[str, set[str]] = defaultdict(set)
    private_allow_grants = 0
    expiring_allow_grants = 0
    invalid_principal_grants = 0

    for row in subagents:
        subagent_id = str(row["id"])
        row_grants = grants.get(subagent_id, [])
        mode = _audience_mode(
            row,
            row_grants,
            capabilities["audience_mode"] and capabilities["audience_semantics_active"],
            capabilities["resource_grants"],
        )
        audience_counts[mode] += 1
        status = str(row["status"] or "unknown")
        status_counts[status] += 1
        owner_id = row["owner_user_id"] if capabilities["owner_user_id"] and capabilities["owner_semantics_active"] else row["created_by"]
        owner_state = _owner_state(
            owner_id,
            owner_states,
            available=capabilities["owner_membership"],
        )
        owner_state_counts[owner_state] += 1
        name = str(row["name"] or "")
        names[name] += 1

        if not _NAME_RE.fullmatch(name):
            affected["invalid_name_subagents"].add(subagent_id)
        if capabilities["owner_user_id"] and capabilities["owner_semantics_active"] and owner_id != row["created_by"]:
            affected["creator_owner_mismatch_subagents"].add(subagent_id)
        if status == "enabled" and mode in {"custom", "teamwide"} and owner_state != "active":
            affected["enabled_shared_invalid_owner_subagents"].add(subagent_id)

        principal_types = {str(grant["principal_type"]) for grant in row_grants}
        roster = {(str(grant["principal_type"]), str(grant["principal_id"])) for grant in row_grants if grant["principal_type"] in {"user", "group"}}
        invalid_principals = [
            grant
            for grant in row_grants
            if (grant["principal_type"] == "user" and str(grant["principal_id"]) not in valid_users)
            or (grant["principal_type"] == "group" and str(grant["principal_id"]) not in valid_groups)
        ]
        if invalid_principals:
            affected["invalid_audience_principal_subagents"].add(subagent_id)
            invalid_principal_grants += len(invalid_principals)
        if mode == "private" and row_grants:
            affected["private_active_allow_subagents"].add(subagent_id)
            private_allow_grants += len(row_grants)
        if mode == "teamwide" and row_grants:
            affected["teamwide_hidden_allow_subagents"].add(subagent_id)
        if mode == "custom" and len(roster) > 200:
            affected["custom_roster_over_limit_subagents"].add(subagent_id)
        if any(grant["expires_at"] is not None for grant in row_grants):
            affected["expiring_active_allow_subagents"].add(subagent_id)
            expiring_allow_grants += sum(grant["expires_at"] is not None for grant in row_grants)
        if _audience_mismatch(row, mode, row_grants, principal_types):
            affected["visibility_grant_mismatch_subagents"].add(subagent_id)

        tool_refs, tools_valid = _json_string_list(row["tool_refs_json"])
        skill_refs, skills_valid = _json_string_list(row["skills_json"])
        output_schema_valid = _json_object_or_null(row["output_schema_json"])
        if not tools_valid or not skills_valid or not output_schema_valid:
            affected["malformed_definition_json_subagents"].add(subagent_id)
        if tools_valid:
            _audit_tool_refs(
                affected,
                subagent_id=subagent_id,
                tool_refs=tool_refs,
                native_tool_names=native_tool_names,
                active_tool_names=active_tool_names,
                inactive_tool_names=inactive_tool_names,
                source_catalog_available=capabilities["team_tool_source"],
            )
        if skills_valid:
            _audit_skill_refs(
                affected,
                subagent_id=subagent_id,
                skill_refs=skill_refs,
                builtin_names=builtins,
                team_skill_states=skill_states,
                team_skill_catalog_available=capabilities["team_skill"],
            )

    for name, count in names.items():
        if count > 1:
            for row in subagents:
                if row["name"] == name:
                    affected["duplicate_name_subagents"].add(str(row["id"]))

    validation_pending = _validation_pending_count(
        connection,
        team_id=team_id,
        subagent_ids={str(row["id"]) for row in subagents},
        available=capabilities["validation"],
    )
    findings: dict[str, int | None] = {
        "enabled_shared_invalid_owner_subagents": (
            len(affected["enabled_shared_invalid_owner_subagents"])
            if capabilities["owner_membership"]
            and ((capabilities["audience_mode"] and capabilities["audience_semantics_active"]) or capabilities["resource_grants"])
            else None
        ),
        "private_active_allow_subagents": (len(affected["private_active_allow_subagents"]) if capabilities["resource_grants"] else None),
        "private_active_allow_grants": private_allow_grants if capabilities["resource_grants"] else None,
        "teamwide_hidden_allow_subagents": (len(affected["teamwide_hidden_allow_subagents"]) if capabilities["resource_grants"] else None),
        "visibility_grant_mismatch_subagents": (len(affected["visibility_grant_mismatch_subagents"]) if capabilities["resource_grants"] else None),
        "expiring_active_allow_subagents": (len(affected["expiring_active_allow_subagents"]) if capabilities["resource_grants"] else None),
        "expiring_active_allow_grants": (expiring_allow_grants if capabilities["resource_grants"] else None),
        "malformed_allow_expiry_grants": (malformed_expiry_count if capabilities["resource_grants"] else None),
        "custom_roster_over_limit_subagents": (len(affected["custom_roster_over_limit_subagents"]) if capabilities["resource_grants"] else None),
        "invalid_audience_principal_subagents": (
            len(affected["invalid_audience_principal_subagents"]) if capabilities["resource_grants"] and capabilities["audience_principals"] else None
        ),
        "invalid_audience_principal_grants": (
            invalid_principal_grants if capabilities["resource_grants"] and capabilities["audience_principals"] else None
        ),
        "creator_owner_mismatch_subagents": (
            len(affected["creator_owner_mismatch_subagents"]) if capabilities["owner_user_id"] and capabilities["owner_semantics_active"] else None
        ),
        "invalid_name_subagents": len(affected["invalid_name_subagents"]),
        "duplicate_name_subagents": len(affected["duplicate_name_subagents"]),
        "malformed_definition_json_subagents": len(affected["malformed_definition_json_subagents"]),
        "missing_or_disallowed_tool_ref_subagents": (
            len(affected["missing_or_disallowed_tool_ref_subagents"]) if capabilities["team_tool_source"] else None
        ),
        "disabled_tool_ref_subagents": (len(affected["disabled_tool_ref_subagents"]) if capabilities["team_tool_source"] else None),
        "missing_skill_ref_subagents": (len(affected["missing_skill_ref_subagents"]) if capabilities["team_skill"] else None),
        "disabled_skill_ref_subagents": (len(affected["disabled_skill_ref_subagents"]) if capabilities["team_skill"] else None),
        "unapproved_or_unattachable_skill_ref_subagents": (
            len(affected["unapproved_or_unattachable_skill_ref_subagents"]) if capabilities["team_skill"] else None
        ),
        "validation_pending_subagents": validation_pending,
    }
    totals: dict[str, Any] = {
        "subagents": len(subagents),
        "status": {
            "enabled": status_counts["enabled"],
            "disabled": status_counts["disabled"],
            "other": len(subagents) - status_counts["enabled"] - status_counts["disabled"],
        },
        "audience": {
            "private": audience_counts["private"],
            "custom": audience_counts["custom"],
            "teamwide": audience_counts["teamwide"],
            "unknown": audience_counts["unknown"],
        },
        "owner_state": {
            "active": owner_state_counts["active"],
            "inactive": owner_state_counts["inactive"],
            "null": owner_state_counts["null"],
            "unknown": owner_state_counts["unknown"],
        },
    }
    return SubagentAuditReport(
        schema_version=1,
        generated_at=now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        team_id=team_id,
        schema_capabilities=capabilities,
        totals=totals,
        findings=findings,
    )


def _build_migration_impact_report(
    connection: sqlite3.Connection,
    *,
    audit_report: SubagentAuditReport,
    team_id: str,
    now: datetime,
) -> SubagentMigrationImpactReport:
    capabilities = audit_report.schema_capabilities
    subagents = list(
        connection.execute(
            "SELECT * FROM team_subagent WHERE team_id=? ORDER BY id",
            (team_id,),
        )
    )
    owner_actions_available = capabilities["owner_membership"]
    audience_actions_available = capabilities["resource_grants"] and capabilities["audience_principals"]

    owner_states = _owner_states(
        connection,
        team_id=team_id,
        available=owner_actions_available,
    )
    owner_disabled: set[str] = set()
    owner_disable_states: Counter[str] = Counter()
    if owner_actions_available:
        for row in subagents:
            owner_id = row["owner_user_id"] if capabilities["owner_user_id"] and capabilities["owner_semantics_active"] else row["created_by"]
            owner_state = _owner_state(owner_id, owner_states, available=True)
            if row["status"] == "enabled" and owner_state != "active":
                owner_disabled.add(str(row["id"]))
                owner_disable_states[owner_state] += 1

    dispositions = {
        "unchanged_private_subagents": 0,
        "unchanged_custom_subagents": 0,
        "unchanged_teamwide_subagents": 0,
        "private_revoke_allow_subagents": 0,
        "teamwide_clear_hidden_allow_subagents": 0,
        "custom_prune_keep_custom_subagents": 0,
        "custom_prune_to_private_subagents": 0,
        "custom_roster_over_limit_needs_attention_subagents": 0,
        "fail_closed_to_private_subagents": 0,
        "projection_repair_only_subagents": 0,
    }
    audience_changed: set[str] = set()
    audience_narrowed: set[str] = set()
    audience_to_private: set[str] = set()
    audience_needs_attention: set[str] = set()
    revoke_allow_grants = 0

    if audience_actions_available:
        all_grants = _unrevoked_allow_grants(connection, team_id=team_id)
        valid_users, valid_groups = _valid_audience_principals(
            connection,
            team_id=team_id,
            available=True,
        )
        explicit_mode = capabilities["audience_mode"] and capabilities["audience_semantics_active"]
        for row in subagents:
            subagent_id = str(row["id"])
            row_grants = all_grants.get(subagent_id, [])
            active_grants = [grant for grant in row_grants if _grant_is_active(grant["expires_at"], now)[0]]
            mode = _audience_mode(
                row,
                active_grants,
                explicit_mode,
                has_grant_data=True,
            )
            disposition, revoked, changed, narrowed, to_private = _migration_audience_disposition(
                row,
                mode=mode,
                grants=row_grants,
                valid_users=valid_users,
                valid_groups=valid_groups,
            )
            dispositions[disposition] += 1
            revoke_allow_grants += revoked
            if changed:
                audience_changed.add(subagent_id)
            if narrowed:
                audience_narrowed.add(subagent_id)
            if to_private:
                audience_to_private.add(subagent_id)
            if disposition == ("custom_roster_over_limit_needs_attention_subagents"):
                audience_needs_attention.add(subagent_id)

    unavailable_sections: list[str] = []
    if not owner_actions_available:
        unavailable_sections.append("owner_actions")
    if not audience_actions_available:
        unavailable_sections.append("audience_actions")
    if not capabilities["team_tool_source"]:
        unavailable_sections.append("tool_dependency_checks")
    if not capabilities["team_skill"]:
        unavailable_sections.append("skill_dependency_checks")
    if not capabilities["validation"]:
        unavailable_sections.append("validation_state")

    access_or_lifecycle_changed = len(audience_changed | owner_disabled) if audience_actions_available and owner_actions_available else None
    audience_actions: dict[str, Any]
    if audience_actions_available:
        audience_actions = {
            "dispositions": dispositions,
            "automatic_change_subagents": len(audience_changed),
            "audience_contraction_subagents": len(audience_narrowed),
            "to_private_subagents": len(audience_to_private),
            "needs_attention_subagents": len(audience_needs_attention),
            "revoke_allow_grants": revoke_allow_grants,
            "deny_grants_changed": 0,
        }
    else:
        audience_actions = {
            "dispositions": dict.fromkeys(dispositions),
            "automatic_change_subagents": None,
            "audience_contraction_subagents": None,
            "to_private_subagents": None,
            "needs_attention_subagents": None,
            "revoke_allow_grants": None,
            "deny_grants_changed": 0,
        }

    findings = audit_report.findings
    return SubagentMigrationImpactReport(
        schema_version=1,
        report_kind="subagent_release_b_migration_impact",
        generated_at=now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        team_id=team_id,
        dry_run=True,
        writes_performed=0,
        semantic_sources={
            "owner": ("owner_user_id" if capabilities["owner_user_id"] and capabilities["owner_semantics_active"] else "created_by_compatibility"),
            "audience": (
                "audience_mode"
                if capabilities["audience_mode"] and capabilities["audience_semantics_active"]
                else "legacy_visibility_grants_compatibility"
            ),
        },
        schema_capabilities=capabilities,
        completeness={
            "automatic_repairs_computable": (owner_actions_available and audience_actions_available),
            "dependency_checks_computable": (capabilities["team_tool_source"] and capabilities["team_skill"]),
            "unavailable_sections": unavailable_sections,
        },
        totals={
            "subagents": len(subagents),
            "access_or_lifecycle_change_subagents": access_or_lifecycle_changed,
        },
        actions={
            "owner": {
                "disable_invalid_owner_subagents": (len(owner_disabled) if owner_actions_available else None),
                "inactive_owner_subagents": (owner_disable_states["inactive"] if owner_actions_available else None),
                "null_owner_subagents": (owner_disable_states["null"] if owner_actions_available else None),
                "owner_transfers": 0,
            },
            "audience": audience_actions,
        },
        follow_up={
            "invalid_name_subagents": findings["invalid_name_subagents"],
            "duplicate_name_subagents": findings["duplicate_name_subagents"],
            "malformed_definition_json_subagents": findings["malformed_definition_json_subagents"],
            "missing_or_disallowed_tool_ref_subagents": findings["missing_or_disallowed_tool_ref_subagents"],
            "disabled_tool_ref_subagents": findings["disabled_tool_ref_subagents"],
            "missing_skill_ref_subagents": findings["missing_skill_ref_subagents"],
            "disabled_skill_ref_subagents": findings["disabled_skill_ref_subagents"],
            "unapproved_or_unattachable_skill_ref_subagents": findings["unapproved_or_unattachable_skill_ref_subagents"],
            "validation_pending_subagents": findings["validation_pending_subagents"],
        },
    )


def _migration_audience_disposition(
    row: sqlite3.Row,
    *,
    mode: str,
    grants: list[sqlite3.Row],
    valid_users: frozenset[str],
    valid_groups: frozenset[str],
) -> tuple[str, int, bool, bool, bool]:
    visibility = str(row["visibility"] or "")
    if mode == "unknown":
        return (
            "fail_closed_to_private_subagents",
            len(grants),
            True,
            True,
            True,
        )
    if mode == "private":
        if grants:
            return (
                "private_revoke_allow_subagents",
                len(grants),
                True,
                True,
                False,
            )
        if visibility != "creator_only":
            return "projection_repair_only_subagents", 0, True, False, False
        return "unchanged_private_subagents", 0, False, False, False
    if mode == "teamwide":
        if grants:
            return (
                "teamwide_clear_hidden_allow_subagents",
                len(grants),
                True,
                False,
                False,
            )
        if visibility != "team_wide":
            return "projection_repair_only_subagents", 0, True, False, False
        return "unchanged_teamwide_subagents", 0, False, False, False

    eligible_grants = [
        grant
        for grant in grants
        if grant["expires_at"] is None
        and (
            (grant["principal_type"] == "user" and str(grant["principal_id"]) in valid_users)
            or (grant["principal_type"] == "group" and str(grant["principal_id"]) in valid_groups)
        )
    ]
    eligible_roster = {(str(grant["principal_type"]), str(grant["principal_id"])) for grant in eligible_grants}
    ineligible_grants = len(grants) - len(eligible_grants)
    if len(eligible_roster) > 200:
        return (
            "custom_roster_over_limit_needs_attention_subagents",
            ineligible_grants,
            ineligible_grants > 0 or visibility != "creator_only",
            ineligible_grants > 0,
            False,
        )
    if not eligible_roster:
        return (
            "custom_prune_to_private_subagents",
            len(grants),
            True,
            True,
            True,
        )
    if ineligible_grants:
        return (
            "custom_prune_keep_custom_subagents",
            ineligible_grants,
            True,
            True,
            False,
        )
    if visibility != "creator_only":
        return "projection_repair_only_subagents", 0, True, False, False
    return "unchanged_custom_subagents", 0, False, False, False


def _unrevoked_allow_grants(
    connection: sqlite3.Connection,
    *,
    team_id: str,
) -> dict[str, list[sqlite3.Row]]:
    grants: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
        "SELECT resource_id,principal_type,principal_id,expires_at "
        "FROM resource_grants "
        "WHERE team_id=? AND resource_kind='team_subagent' "
        "AND effect='allow' AND revoked_at IS NULL "
        "ORDER BY resource_id,principal_type,principal_id",
        (team_id,),
    ):
        grants[str(row["resource_id"])].append(row)
    return dict(grants)


def _active_allow_grants(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    now: datetime,
    available: bool,
) -> tuple[dict[str, list[sqlite3.Row]], int]:
    if not available:
        return {}, 0
    rows = connection.execute(
        "SELECT resource_id,principal_type,principal_id,expires_at "
        "FROM resource_grants "
        "WHERE team_id=? AND resource_kind='team_subagent' "
        "AND effect='allow' AND revoked_at IS NULL",
        (team_id,),
    )
    grants: dict[str, list[sqlite3.Row]] = defaultdict(list)
    malformed_expiry_count = 0
    for row in rows:
        is_active, malformed = _grant_is_active(row["expires_at"], now)
        if malformed:
            malformed_expiry_count += 1
        if is_active:
            grants[str(row["resource_id"])].append(row)
    return dict(grants), malformed_expiry_count


def _owner_states(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    available: bool,
) -> dict[str, tuple[str | None, str | None]]:
    if not available:
        return {}
    return {
        str(row["id"]): (row["user_status"], row["membership_status"])
        for row in connection.execute(
            "SELECT u.id,u.status AS user_status,tm.status AS membership_status "
            "FROM users u "
            "LEFT JOIN team_members tm ON tm.user_id=u.id AND tm.team_id=?",
            (team_id,),
        )
    }


def _valid_audience_principals(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    available: bool,
) -> tuple[frozenset[str], frozenset[str]]:
    if not available:
        return frozenset(), frozenset()
    users = frozenset(
        str(row["id"])
        for row in connection.execute(
            "SELECT u.id FROM users u JOIN team_members tm ON tm.user_id=u.id AND tm.team_id=? WHERE u.status='active' AND tm.status='active'",
            (team_id,),
        )
    )
    groups = frozenset(
        str(row["id"])
        for row in connection.execute(
            "SELECT id FROM groups WHERE team_id=? AND status='active'",
            (team_id,),
        )
    )
    return users, groups


def _team_tool_names(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    available: bool,
) -> tuple[frozenset[str], frozenset[str]]:
    if not available:
        return frozenset(), frozenset()
    active: set[str] = set()
    inactive: set[str] = set()
    for row in connection.execute(
        "SELECT id,kind,status,openapi_spec_json,selected_op_ids_json,tools_schema_json FROM team_tool_source WHERE team_id=?",
        (team_id,),
    ):
        names = _source_tool_names(row)
        (active if row["status"] == "enabled" else inactive).update(names)
    inactive.difference_update(active)
    return frozenset(active), frozenset(inactive)


def _source_tool_names(row: sqlite3.Row) -> set[str]:
    selected, selected_valid = _json_string_list(row["selected_op_ids_json"])
    if not selected_valid:
        return set()
    selected_names = set(selected)
    source_id = str(row["id"])
    if row["kind"] == "mcp":
        try:
            tools = json.loads(row["tools_schema_json"] or "[]")
        except (TypeError, ValueError):
            return set()
        if not isinstance(tools, list):
            return set()
        raw_names = {str(item.get("name")) for item in tools if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("name")}
        if selected_names:
            raw_names.intersection_update(selected_names)
        return {op_tool_name(source_id, name) for name in raw_names}
    if row["kind"] != "openapi" or not row["openapi_spec_json"]:
        return set()
    try:
        spec = json.loads(row["openapi_spec_json"])
        operations = parse_openapi_ops(spec)
    except (TypeError, ValueError):
        return set()
    return {
        op_tool_name(source_id, operation.operation_id) for operation in operations if not selected_names or operation.operation_id in selected_names
    }


def _team_skill_states(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    available: bool,
) -> dict[str, str]:
    if not available:
        return {}
    columns = _columns(connection, "team_skill")
    approval_expr = "approval" if "approval" in columns else "'approved'"
    states: dict[str, str] = {}
    for row in connection.execute(
        f"SELECT name,status,visibility,{approval_expr} AS approval FROM team_skill WHERE team_id=?",  # noqa: S608
        (team_id,),
    ):
        name = str(row["name"])
        if row["status"] != "enabled":
            states[name] = "disabled"
        elif row["approval"] != "approved" or row["visibility"] != "team_wide":
            states[name] = "unapproved_or_unattachable"
        else:
            states[name] = "active"
    return states


def _native_tool_allowlist() -> frozenset[str]:
    registry = build_default_registry()
    hidden = frozenset(registry.list_names()) - frozenset(schema.name for schema in registry.to_schemas())
    return frozenset(
        name
        for name, roles in _PARADIGM_V2_ROLE_MAP.items()
        if "analyst" in roles
        and name != "subagent_return"
        and not any(name.startswith(prefix) for prefix in DESTRUCTIVE_TOOL_PREFIXES)
        and name not in REPORT_WRITE_TOOLS
        and name not in hidden
    ) | frozenset({"python_sandbox"})


def _audit_tool_refs(
    affected: dict[str, set[str]],
    *,
    subagent_id: str,
    tool_refs: list[str],
    native_tool_names: frozenset[str],
    active_tool_names: frozenset[str],
    inactive_tool_names: frozenset[str],
    source_catalog_available: bool,
) -> None:
    for ref in tool_refs:
        if ref.startswith("teammcp."):
            if not source_catalog_available:
                continue
            if ref in active_tool_names:
                continue
            finding = "disabled_tool_ref_subagents" if ref in inactive_tool_names else "missing_or_disallowed_tool_ref_subagents"
            affected[finding].add(subagent_id)
        elif ref not in native_tool_names:
            affected["missing_or_disallowed_tool_ref_subagents"].add(subagent_id)


def _audit_skill_refs(
    affected: dict[str, set[str]],
    *,
    subagent_id: str,
    skill_refs: list[str],
    builtin_names: frozenset[str],
    team_skill_states: dict[str, str],
    team_skill_catalog_available: bool,
) -> None:
    for name in skill_refs:
        if name in builtin_names or team_skill_states.get(name) == "active":
            continue
        state = team_skill_states.get(name)
        if not team_skill_catalog_available:
            continue
        if state == "disabled":
            affected["disabled_skill_ref_subagents"].add(subagent_id)
        elif state == "unapproved_or_unattachable":
            affected["unapproved_or_unattachable_skill_ref_subagents"].add(subagent_id)
        else:
            affected["missing_skill_ref_subagents"].add(subagent_id)


def _audience_mode(
    row: sqlite3.Row,
    grants: list[sqlite3.Row],
    has_explicit_mode: bool,
    has_grant_data: bool,
) -> str:
    if has_explicit_mode:
        mode = str(row["audience_mode"] or "")
        return mode if mode in _VALID_AUDIENCE_MODES else "unknown"
    visibility = str(row["visibility"] or "")
    if visibility == "team_wide":
        return "teamwide"
    if visibility != "creator_only":
        return "unknown"
    if not has_grant_data:
        return "unknown"
    principal_types = {str(grant["principal_type"]) for grant in grants}
    if "team" in principal_types or principal_types - {"user", "group"}:
        return "unknown"
    return "custom" if principal_types else "private"


def _audience_mismatch(
    row: sqlite3.Row,
    mode: str,
    grants: list[sqlite3.Row],
    principal_types: set[str],
) -> bool:
    expected_visibility = "team_wide" if mode == "teamwide" else "creator_only"
    if mode == "unknown" or row["visibility"] != expected_visibility:
        return True
    if mode in {"private", "teamwide"}:
        return bool(grants)
    if mode == "custom":
        return not grants or bool(principal_types - {"user", "group"})
    return True


def _validation_pending_count(
    connection: sqlite3.Connection,
    *,
    team_id: str,
    subagent_ids: set[str],
    available: bool,
) -> int | None:
    if not available:
        return None
    current = {
        str(row["subagent_id"])
        for row in connection.execute(
            "SELECT subagent_id,status FROM team_subagent_validation WHERE team_id=?",
            (team_id,),
        )
        if row["status"] in _VALID_VALIDATION_STATUSES
    }
    return len(subagent_ids - current)


def _grant_is_active(raw_expiry: Any, now: datetime) -> tuple[bool, bool]:
    if raw_expiry is None:
        return True, False
    try:
        expiry = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            return True, True
    except ValueError:
        return True, True
    return expiry > now, False


def _owner_state(
    owner_id: Any,
    states: dict[str, tuple[str | None, str | None]],
    *,
    available: bool,
) -> str:
    if not owner_id:
        return "null"
    if not available:
        return "unknown"
    user_status, membership_status = states.get(str(owner_id), (None, None))
    return "active" if user_status == "active" and membership_status == "active" else "inactive"


def _json_string_list(raw: Any) -> tuple[list[str], bool]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return [], False
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return [], False
    return value, True


def _json_object_or_null(raw: Any) -> bool:
    if raw is None:
        return True
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict)


def _semantic_activation(connection: sqlite3.Connection) -> tuple[bool, bool]:
    required = {
        "singleton",
        "owner_semantics_activated_at",
        "audience_semantics_activated_at",
    }
    if not _has_columns(connection, "team_subagent_schema_meta", required):
        return False, False
    row = connection.execute(
        "SELECT owner_semantics_activated_at,audience_semantics_activated_at FROM team_subagent_schema_meta WHERE singleton=1",
    ).fetchone()
    if row is None:
        return False, False
    return row["owner_semantics_activated_at"] is not None, row["audience_semantics_activated_at"] is not None


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _require_table(connection: sqlite3.Connection, table: str) -> None:
    if not _table_exists(connection, table):
        raise ValueError(f"database is missing required table: {table}")


def _has_columns(
    connection: sqlite3.Connection,
    table: str,
    required: set[str],
) -> bool:
    return _table_exists(connection, table) and required <= _columns(connection, table)


def _require_columns(
    connection: sqlite3.Connection,
    table: str,
    required: set[str],
) -> None:
    missing = required - _columns(connection, table)
    if missing:
        raise ValueError(f"table {table} is missing required audit columns: {', '.join(sorted(missing))}")


def _columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")'))
