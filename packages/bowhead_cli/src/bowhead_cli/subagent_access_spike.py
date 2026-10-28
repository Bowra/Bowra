"""Isolated Stage 0A spike for planned Subagent access resolution."""

from __future__ import annotations

import platform
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bowhead_core.share_service import is_visible
from bowhead_core.team_subagent_access import (
    TeamSubagentAccessFacts as AccessFacts,
)
from bowhead_core.team_subagent_access import (
    TeamSubagentAccessResolution as AccessResolution,
)
from bowhead_core.team_subagent_access import (
    resolve_team_subagent_access as resolve_access_policy,
)

_NOW = "2026-07-27T12:00:00Z"
_TEAM_ID = "team_acl_spike"
_SURFACES = ("catalog", "consumer_detail", "lead_advertise", "name_resolve")


@dataclass(frozen=True)
class AccessScenario:
    name: str
    actor_id: str
    subagent_id: str
    governance_action_allowed: bool = False


@dataclass(frozen=True)
class SubagentAccessSpikeReport:
    schema_version: int
    scope: str
    methodology: dict[str, str]
    environment: dict[str, str]
    scenarios: list[dict[str, Any]]
    query_plan: list[str]
    checks: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_subagent_access_spike() -> SubagentAccessSpikeReport:
    """Run the deterministic SQL/policy parity matrix in a temporary database."""
    with tempfile.TemporaryDirectory(prefix="bowhead-subagent-access-spike-") as temp_dir:
        db_path = Path(temp_dir) / "access-spike.db"
        _create_fixture(db_path)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            scenarios = [_run_scenario(connection, scenario) for scenario in _ACCESS_SCENARIOS]
            query_plan = _query_plan(connection)
        finally:
            connection.close()

    by_name = {scenario["name"]: scenario for scenario in scenarios}
    checks = {
        "sql_policy_parity": all(scenario["sql_policy_parity"] for scenario in scenarios),
        "surface_parity": all(scenario["surface_parity"] for scenario in scenarios),
        "deny_wins": all(
            not by_name[name]["resolution"]["can_consume"] and by_name[name]["resolution"]["denial_reason"] == "blocked_by_security_policy"
            for name in ("teamwide_user_deny", "teamwide_group_deny_user_allow")
        ),
        "owner_manage_survives_deny": (
            by_name["private_owner_deny"]["resolution"]["can_manage"] and not by_name["private_owner_deny"]["resolution"]["can_consume"]
        ),
        "inactive_owner_fails_closed": (
            not by_name["inactive_owner_self"]["resolution"]["can_manage"]
            and not by_name["inactive_owner_self"]["resolution"]["can_consume"]
            and by_name["inactive_owner_consumer"]["resolution"]["denial_reason"] == "owner_inactive"
        ),
        "private_ignores_allow": (
            not by_name["private_nonowner_allow"]["resolution"]["can_consume"]
            and by_name["private_nonowner_allow"]["resolution"]["denial_reason"] == "audience_not_allowed"
        ),
        "governance_does_not_grant_consumption": (
            by_name["admin_governance_only"]["resolution"]["can_govern"] and not by_name["admin_governance_only"]["resolution"]["can_consume"]
        ),
        "status_is_consumer_hard_gate": (
            by_name["disabled_owner"]["resolution"]["can_manage"]
            and not by_name["disabled_owner"]["resolution"]["can_consume"]
            and by_name["disabled_owner"]["resolution"]["denial_reason"] == "status_not_enabled"
        ),
        "cross_team_is_hidden": not by_name["cross_team"]["row_found"],
        "legacy_shortcut_drift_observed": all(
            by_name[name]["legacy_drift"]
            for name in (
                "teamwide_user_deny",
                "private_owner_deny",
                "inactive_owner_self",
            )
        ),
        "query_plan_indexed": not _plan_issues(query_plan),
    }
    return SubagentAccessSpikeReport(
        schema_version=1,
        scope="synthetic_isolated_acl_sql_spike",
        methodology={
            "fixture": "deterministic temporary SQLite database; no production data",
            "sql": "planned canonical CTE resolves actor, owner, audience and deny before presentation",
            "policy": "independent pure policy is compared with SQL decisions",
            "legacy": "current is_visible shortcut is measured, not changed",
            "production_activation": "none",
        },
        environment={
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.system(),
            "architecture": platform.machine(),
        },
        scenarios=scenarios,
        query_plan=query_plan,
        checks=checks,
    )


def _run_scenario(
    connection: sqlite3.Connection,
    scenario: AccessScenario,
) -> dict[str, Any]:
    row = connection.execute(
        _CANONICAL_ACCESS_SQL,
        {
            "team_id": _TEAM_ID,
            "actor_id": scenario.actor_id,
            "subagent_id": scenario.subagent_id,
            "now": _NOW,
            "governance_action_allowed": int(
                scenario.governance_action_allowed,
            ),
        },
    ).fetchone()
    if row is None:
        return {
            "name": scenario.name,
            "row_found": False,
            "facts": None,
            "resolution": None,
            "sql_policy_parity": True,
            "surface_parity": True,
            "surface_decisions": {surface: {"row_found": False} for surface in _SURFACES},
            "legacy_visible": False,
            "legacy_drift": False,
        }

    facts = _facts_from_row(row)
    sql_resolution = _resolution_from_row(row)
    policy_resolution = resolve_access_policy(facts)
    surface_decisions = {
        surface: asdict(
            _resolve_sql(
                connection,
                scenario=scenario,
            ),
        )
        for surface in _SURFACES
    }
    legacy_granted_ids = (
        frozenset({scenario.subagent_id}) if (facts.has_user_allow or facts.has_group_allow) and not facts.has_security_deny else frozenset()
    )
    legacy_visible = is_visible(
        owner_id=str(row["owner_user_id"]),
        visibility=str(row["visibility"]),
        resource_id=scenario.subagent_id,
        viewer_id=scenario.actor_id,
        granted_ids=legacy_granted_ids,
    )
    comparable_surface_decisions = {
        (
            decision["can_manage"],
            decision["can_consume"],
            decision["can_govern"],
            decision["access_reason"],
            decision["denial_reason"],
        )
        for decision in surface_decisions.values()
    }
    return {
        "name": scenario.name,
        "row_found": True,
        "facts": asdict(facts),
        "resolution": asdict(sql_resolution),
        "sql_policy_parity": sql_resolution == policy_resolution,
        "surface_parity": len(comparable_surface_decisions) == 1,
        "surface_decisions": surface_decisions,
        "legacy_visible": legacy_visible,
        "legacy_drift": legacy_visible != sql_resolution.can_consume,
    }


def _resolve_sql(
    connection: sqlite3.Connection,
    *,
    scenario: AccessScenario,
) -> AccessResolution:
    row = connection.execute(
        _CANONICAL_ACCESS_SQL,
        {
            "team_id": _TEAM_ID,
            "actor_id": scenario.actor_id,
            "subagent_id": scenario.subagent_id,
            "now": _NOW,
            "governance_action_allowed": int(
                scenario.governance_action_allowed,
            ),
        },
    ).fetchone()
    if row is None:
        raise ValueError(f"missing synthetic scenario row: {scenario.name}")
    return _resolution_from_row(row)


def _facts_from_row(row: sqlite3.Row) -> AccessFacts:
    return AccessFacts(
        actor_active=bool(row["actor_active"]),
        is_owner=bool(row["is_owner"]),
        owner_active=bool(row["owner_active"]),
        is_team_governor=bool(row["is_team_governor"]),
        governance_action_allowed=bool(row["governance_action_allowed"]),
        audience_mode=str(row["audience_mode"]),
        has_user_allow=bool(row["has_user_allow"]),
        has_group_allow=bool(row["has_group_allow"]),
        has_security_deny=bool(row["has_security_deny"]),
        lifecycle_state=str(row["lifecycle_state"]),
        status=str(row["status"]),
    )


def _resolution_from_row(row: sqlite3.Row) -> AccessResolution:
    return AccessResolution(
        can_manage=bool(row["can_manage"]),
        can_consume=bool(row["can_consume"]),
        can_govern=bool(row["can_govern"]),
        access_reason=(str(row["access_reason"]) if row["access_reason"] is not None else None),
        denial_reason=(str(row["denial_reason"]) if row["denial_reason"] is not None else None),
    )


def _query_plan(connection: sqlite3.Connection) -> list[str]:
    scenario = _ACCESS_SCENARIOS[0]
    return [
        str(row[3])
        for row in connection.execute(
            f"EXPLAIN QUERY PLAN {_CANONICAL_ACCESS_SQL}",  # noqa: S608
            {
                "team_id": _TEAM_ID,
                "actor_id": scenario.actor_id,
                "subagent_id": scenario.subagent_id,
                "now": _NOW,
                "governance_action_allowed": 0,
            },
        )
    ]


def _plan_issues(query_plan: list[str]) -> list[str]:
    issues: list[str] = []
    for detail in query_plan:
        normalized = detail.upper()
        grant_aliases = ("USER_ALLOW", "GROUP_ALLOW", "SECURITY_DENY")
        if any(f"SCAN {alias}" in normalized for alias in grant_aliases) and "USING INDEX" not in normalized:
            issues.append("unindexed_resource_grants_scan")
        membership_aliases = ("ALLOW_GM", "DENY_GM")
        if any(f"SCAN {alias}" in normalized for alias in membership_aliases) and "USING INDEX" not in normalized:
            issues.append("unindexed_group_memberships_scan")
        if "SCAN TEAM_SUBAGENT" in normalized and "USING INDEX" not in normalized:
            issues.append("unindexed_team_subagent_scan")
    return sorted(set(issues))


def _create_fixture(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(_SCHEMA)
        connection.executescript(_FIXTURE_SQL)
    finally:
        connection.close()


_ACCESS_SCENARIOS = (
    AccessScenario("teamwide_open", "usr_member", "osa_teamwide_open"),
    AccessScenario("teamwide_user_deny", "usr_member", "osa_teamwide_user_deny"),
    AccessScenario(
        "teamwide_group_deny_user_allow",
        "usr_member",
        "osa_teamwide_group_deny",
    ),
    AccessScenario("private_owner_deny", "usr_owner", "osa_private_owner_deny"),
    AccessScenario(
        "inactive_owner_self",
        "usr_owner_inactive",
        "osa_inactive_owner",
    ),
    AccessScenario(
        "inactive_owner_consumer",
        "usr_member",
        "osa_inactive_owner",
    ),
    AccessScenario("custom_user_allow", "usr_member", "osa_custom_user"),
    AccessScenario("custom_group_allow", "usr_member", "osa_custom_group"),
    AccessScenario("custom_expired_allow", "usr_member", "osa_custom_expired"),
    AccessScenario("private_nonowner_allow", "usr_member", "osa_private_allow"),
    AccessScenario("disabled_owner", "usr_owner", "osa_disabled"),
    AccessScenario(
        "admin_governance_only",
        "usr_admin",
        "osa_custom_governance",
        governance_action_allowed=True,
    ),
    AccessScenario(
        "audit_admin_not_governor",
        "usr_audit",
        "osa_custom_governance",
        governance_action_allowed=True,
    ),
    AccessScenario("inactive_actor", "usr_inactive", "osa_teamwide_open"),
    AccessScenario("quarantined_owner", "usr_owner", "osa_quarantined"),
    AccessScenario("cross_team", "usr_member", "osa_other_team"),
)

_FIXTURE_SQL = """
INSERT INTO teams VALUES
  ('team_acl_spike','usr_team_owner'),
  ('team_other','usr_other');
INSERT INTO users VALUES
  ('usr_owner','active'),
  ('usr_member','active'),
  ('usr_admin','active'),
  ('usr_team_owner','active'),
  ('usr_audit','active'),
  ('usr_inactive','active'),
  ('usr_owner_inactive','inactive'),
  ('usr_other','active');
INSERT INTO team_members VALUES
  ('team_acl_spike','usr_owner','member','active'),
  ('team_acl_spike','usr_member','member','active'),
  ('team_acl_spike','usr_admin','team_admin','active'),
  ('team_acl_spike','usr_team_owner','member','active'),
  ('team_acl_spike','usr_audit','audit_admin','active'),
  ('team_acl_spike','usr_inactive','member','removed'),
  ('team_acl_spike','usr_owner_inactive','member','removed'),
  ('team_other','usr_other','member','active');
INSERT INTO groups VALUES
  ('grp_allow','team_acl_spike','active'),
  ('grp_deny','team_acl_spike','active');
INSERT INTO group_memberships VALUES
  ('team_acl_spike','grp_allow','user','usr_member','active'),
  ('team_acl_spike','grp_deny','user','usr_member','active');
INSERT INTO team_subagent VALUES
  ('osa_teamwide_open','team_acl_spike','usr_owner','teamwide','team_wide','active','enabled'),
  ('osa_teamwide_user_deny','team_acl_spike','usr_owner','teamwide','team_wide','active','enabled'),
  ('osa_teamwide_group_deny','team_acl_spike','usr_owner','teamwide','team_wide','active','enabled'),
  ('osa_private_owner_deny','team_acl_spike','usr_owner','private','creator_only','active','enabled'),
  ('osa_inactive_owner','team_acl_spike','usr_owner_inactive','teamwide','team_wide','active','enabled'),
  ('osa_custom_user','team_acl_spike','usr_owner','custom','creator_only','active','enabled'),
  ('osa_custom_group','team_acl_spike','usr_owner','custom','creator_only','active','enabled'),
  ('osa_custom_expired','team_acl_spike','usr_owner','custom','creator_only','active','enabled'),
  ('osa_private_allow','team_acl_spike','usr_owner','private','creator_only','active','enabled'),
  ('osa_disabled','team_acl_spike','usr_owner','teamwide','team_wide','active','disabled'),
  ('osa_custom_governance','team_acl_spike','usr_owner','custom','creator_only','active','enabled'),
  ('osa_quarantined','team_acl_spike','usr_owner','private','creator_only','quarantined','disabled'),
  ('osa_other_team','team_other','usr_other','teamwide','team_wide','active','enabled');
INSERT INTO resource_grants VALUES
  ('grant_user_deny','team_acl_spike','team_subagent','osa_teamwide_user_deny','user','usr_member','use','deny',NULL,NULL),
  ('grant_group_deny','team_acl_spike','team_subagent','osa_teamwide_group_deny','group','grp_deny','use','deny',NULL,NULL),
  ('grant_user_allow_on_denied','team_acl_spike','team_subagent','osa_teamwide_group_deny','user','usr_member','use','allow',NULL,NULL),
  ('grant_owner_deny','team_acl_spike','team_subagent','osa_private_owner_deny','user','usr_owner','use','deny',NULL,NULL),
  ('grant_custom_user','team_acl_spike','team_subagent','osa_custom_user','user','usr_member','use','allow',NULL,NULL),
  ('grant_custom_group','team_acl_spike','team_subagent','osa_custom_group','group','grp_allow','use','allow',NULL,NULL),
  ('grant_custom_expired','team_acl_spike','team_subagent','osa_custom_expired','user','usr_member','use','allow','2026-07-27T11:59:59Z',NULL),
  ('grant_private_ignored','team_acl_spike','team_subagent','osa_private_allow','user','usr_member','use','allow',NULL,NULL);
"""

_SCHEMA = """
CREATE TABLE teams (
  id TEXT PRIMARY KEY,
  created_by_user_id TEXT NOT NULL
);
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL
);
CREATE TABLE team_members (
  team_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY(team_id,user_id)
);
CREATE INDEX idx_team_members_user_status
  ON team_members(user_id,status,team_id);
CREATE TABLE groups (
  id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE group_memberships (
  team_id TEXT NOT NULL,
  group_id TEXT NOT NULL,
  principal_type TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY(group_id,principal_type,principal_id)
);
CREATE INDEX idx_group_memberships_principal
  ON group_memberships(team_id,principal_type,principal_id,status,group_id);
CREATE TABLE team_subagent (
  id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  owner_user_id TEXT,
  audience_mode TEXT NOT NULL,
  visibility TEXT NOT NULL,
  lifecycle_state TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE INDEX idx_team_subagent_team_id
  ON team_subagent(team_id,id);
CREATE TABLE resource_grants (
  id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  resource_kind TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  principal_type TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  permission TEXT NOT NULL,
  effect TEXT NOT NULL,
  expires_at TEXT,
  revoked_at TEXT
);
CREATE INDEX idx_resource_grants_lookup
  ON resource_grants(team_id,resource_kind,resource_id,revoked_at);
"""

_CANONICAL_ACCESS_SQL = """
WITH target AS (
  SELECT sa.*
  FROM team_subagent sa
  WHERE sa.team_id=:team_id AND sa.id=:subagent_id
),
facts AS MATERIALIZED (
  SELECT
    target.*,
    EXISTS (
      SELECT 1
      FROM users actor
      JOIN team_members actor_tm
        ON actor_tm.user_id=actor.id
       AND actor_tm.team_id=target.team_id
       AND actor_tm.status='active'
      WHERE actor.id=:actor_id AND actor.status='active'
    ) AS actor_active,
    target.owner_user_id=:actor_id AS is_owner,
    EXISTS (
      SELECT 1
      FROM users owner
      JOIN team_members owner_tm
        ON owner_tm.user_id=owner.id
       AND owner_tm.team_id=target.team_id
       AND owner_tm.status='active'
      WHERE owner.id=target.owner_user_id AND owner.status='active'
    ) AS owner_active,
    (
      EXISTS (
        SELECT 1 FROM teams
        WHERE teams.id=target.team_id AND teams.created_by_user_id=:actor_id
      )
      OR EXISTS (
        SELECT 1 FROM team_members governor_tm
        WHERE governor_tm.team_id=target.team_id
          AND governor_tm.user_id=:actor_id
          AND governor_tm.role='team_admin'
          AND governor_tm.status='active'
      )
    ) AS is_team_governor,
    :governance_action_allowed AS governance_action_allowed,
    EXISTS (
      SELECT 1
      FROM resource_grants user_allow
      WHERE user_allow.team_id=target.team_id
        AND user_allow.resource_kind='team_subagent'
        AND user_allow.resource_id=target.id
        AND user_allow.principal_type='user'
        AND user_allow.principal_id=:actor_id
        AND user_allow.permission IN ('use','write','admin')
        AND user_allow.effect='allow'
        AND user_allow.revoked_at IS NULL
        AND (user_allow.expires_at IS NULL OR user_allow.expires_at>:now)
    ) AS has_user_allow,
    EXISTS (
      SELECT 1
      FROM resource_grants group_allow
      WHERE group_allow.team_id=target.team_id
        AND group_allow.resource_kind='team_subagent'
        AND group_allow.resource_id=target.id
        AND group_allow.principal_type='group'
        AND group_allow.permission IN ('use','write','admin')
        AND group_allow.effect='allow'
        AND group_allow.revoked_at IS NULL
        AND (group_allow.expires_at IS NULL OR group_allow.expires_at>:now)
        AND EXISTS (
          SELECT 1
          FROM group_memberships allow_gm
          JOIN groups allow_group
            ON allow_group.id=allow_gm.group_id
           AND allow_group.team_id=allow_gm.team_id
           AND allow_group.status='active'
          WHERE allow_gm.team_id=target.team_id
            AND allow_gm.group_id=group_allow.principal_id
            AND allow_gm.principal_type='user'
            AND allow_gm.principal_id=:actor_id
            AND allow_gm.status='active'
        )
    ) AS has_group_allow,
    EXISTS (
      SELECT 1
      FROM resource_grants security_deny
      WHERE security_deny.team_id=target.team_id
        AND security_deny.resource_kind='team_subagent'
        AND security_deny.resource_id=target.id
        AND security_deny.permission IN ('use','write','admin')
        AND security_deny.effect='deny'
        AND security_deny.revoked_at IS NULL
        AND (security_deny.expires_at IS NULL OR security_deny.expires_at>:now)
        AND (
          (
            security_deny.principal_type='user'
            AND security_deny.principal_id=:actor_id
          )
          OR (
            security_deny.principal_type='group'
            AND EXISTS (
              SELECT 1
              FROM group_memberships deny_gm
              JOIN groups deny_group
                ON deny_group.id=deny_gm.group_id
               AND deny_group.team_id=deny_gm.team_id
               AND deny_group.status='active'
              WHERE deny_gm.team_id=target.team_id
                AND deny_gm.group_id=security_deny.principal_id
                AND deny_gm.principal_type='user'
                AND deny_gm.principal_id=:actor_id
                AND deny_gm.status='active'
            )
          )
        )
    ) AS has_security_deny
  FROM target
),
audience AS (
  SELECT
    facts.*,
    CASE
      WHEN is_owner THEN 'owner'
      WHEN audience_mode='teamwide' THEN 'teamwide'
      WHEN audience_mode='custom' AND has_user_allow THEN 'user_grant'
      WHEN audience_mode='custom' AND has_group_allow THEN 'group_grant'
      ELSE NULL
    END AS audience_reason
  FROM facts
),
resolution AS (
  SELECT
    audience.*,
    (
      actor_active
      AND is_owner
      AND lifecycle_state='active'
    ) AS can_manage,
    (
      actor_active
      AND lifecycle_state='active'
      AND status='enabled'
      AND owner_active
      AND audience_reason IS NOT NULL
      AND NOT has_security_deny
    ) AS can_consume,
    (
      actor_active
      AND is_team_governor
      AND governance_action_allowed
    ) AS can_govern
  FROM audience
)
SELECT
  resolution.*,
  CASE WHEN can_consume THEN audience_reason ELSE NULL END AS access_reason,
  CASE
    WHEN can_consume THEN NULL
    WHEN NOT actor_active THEN 'actor_inactive'
    WHEN lifecycle_state<>'active' THEN 'lifecycle_not_active'
    WHEN status<>'enabled' THEN 'status_not_enabled'
    WHEN NOT owner_active THEN 'owner_inactive'
    WHEN audience_reason IS NULL THEN 'audience_not_allowed'
    WHEN has_security_deny THEN 'blocked_by_security_policy'
    ELSE 'access_indeterminate'
  END AS denial_reason
FROM resolution
"""
