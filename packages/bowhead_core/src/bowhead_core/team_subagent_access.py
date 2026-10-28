"""Canonical, read-only access resolution for team-managed Subagents.

Release A keeps the legacy ``created_by`` / ``visibility`` columns authoritative
until the explicit Release B semantic activation.  This resolver therefore
projects those legacy columns before activation and switches to
``owner_user_id`` / ``audience_mode`` / ``lifecycle_state`` only after their
individual activation timestamps are present.

The pure policy and the SQL fact resolver are intentionally separate:

- SQL resolves membership, Owner, audience grants, and security denies;
- the pure policy maps those facts to manage / consume / govern decisions.

Catalog, manager, governance, and runtime surfaces must reuse these primitives
instead of rebuilding role or visibility shortcuts.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import aiosqlite

from bowhead_core._db import open_shared_ctx

SubagentRequestSurface = Literal[
    "catalog",
    "consumer_detail",
    "manager",
    "governance",
    "runtime",
]
SubagentDetailLevel = Literal[
    "hidden",
    "summary",
    "consumer",
    "owner_manager",
    "governance_metadata",
]


@dataclass(frozen=True, slots=True)
class TeamSubagentAccessFacts:
    """Database facts consumed by the pure access policy."""

    actor_active: bool
    is_owner: bool
    owner_active: bool
    is_team_governor: bool
    governance_action_allowed: bool
    audience_mode: str
    has_user_allow: bool
    has_group_allow: bool
    has_security_deny: bool
    lifecycle_state: str
    status: str


@dataclass(frozen=True, slots=True)
class TeamSubagentAccessResolution:
    """Canonical manage / consume / govern decision for one actor and Agent."""

    can_manage: bool
    can_consume: bool
    can_govern: bool
    access_reason: str | None
    denial_reason: str | None

    @property
    def catalog_visible(self) -> bool:
        """Return whether the Agent belongs in the actor's ordinary catalog."""
        return self.can_manage or self.can_consume

    def detail_level(
        self,
        request_surface: SubagentRequestSurface,
    ) -> SubagentDetailLevel:
        """Return the maximum safe DTO level for the requested surface."""
        if request_surface == "catalog":
            return "summary" if self.catalog_visible else "hidden"
        if request_surface == "manager":
            return "owner_manager" if self.can_manage else "hidden"
        if request_surface == "governance":
            return "governance_metadata" if self.can_govern else "hidden"
        if request_surface in {"consumer_detail", "runtime"}:
            return "consumer" if self.can_consume else "hidden"
        return "hidden"


@dataclass(frozen=True, slots=True)
class TeamSubagentAccessDecision:
    """Resolved facts and decision for one team-scoped Subagent."""

    subagent_id: str
    owner_user_id: str | None
    facts: TeamSubagentAccessFacts
    resolution: TeamSubagentAccessResolution
    row_version: int = 1


def resolve_team_subagent_access(
    facts: TeamSubagentAccessFacts,
) -> TeamSubagentAccessResolution:
    """Resolve access without database or network I/O."""
    audience_reason = _audience_reason(facts)
    can_manage = facts.actor_active and facts.is_owner and facts.lifecycle_state == "active"
    can_consume = (
        facts.actor_active
        and facts.lifecycle_state == "active"
        and facts.status == "enabled"
        and facts.owner_active
        and audience_reason is not None
        and not facts.has_security_deny
    )
    can_govern = facts.actor_active and facts.is_team_governor and facts.governance_action_allowed
    return TeamSubagentAccessResolution(
        can_manage=can_manage,
        can_consume=can_consume,
        can_govern=can_govern,
        access_reason=audience_reason if can_consume else None,
        denial_reason=(None if can_consume else _consumer_denial_reason(facts)),
    )


class TeamSubagentAccessResolver:
    """Resolve canonical access facts from the shared SQLite database."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    async def resolve(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        subagent_id: str,
        governance_action_allowed: bool = False,
        now: datetime | None = None,
    ) -> TeamSubagentAccessDecision | None:
        """Resolve one team-bound Agent, returning ``None`` for cross-team/missing IDs."""
        decisions = await self.resolve_many(
            team_id=team_id,
            actor_user_id=actor_user_id,
            subagent_id=subagent_id,
            governance_action_allowed=governance_action_allowed,
            now=now,
        )
        return decisions.get(subagent_id)

    async def resolve_many(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        subagent_id: str | None = None,
        governance_action_allowed: bool = False,
        now: datetime | None = None,
    ) -> dict[str, TeamSubagentAccessDecision]:
        """Resolve all requested rows with one indexed SQL statement."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await self.resolve_many_in_connection(
                db,
                team_id=team_id,
                actor_user_id=actor_user_id,
                subagent_id=subagent_id,
                governance_action_allowed=governance_action_allowed,
                now=now,
            )

    async def resolve_subset(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        subagent_ids: Collection[str],
        governance_action_allowed: bool = False,
        now: datetime | None = None,
    ) -> dict[str, TeamSubagentAccessDecision]:
        """Resolve a bounded caller-supplied subset with the canonical policy."""
        normalized = tuple(dict.fromkeys(str(value) for value in subagent_ids if value))
        if not normalized:
            return {}
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await self.resolve_many_in_connection(
                db,
                team_id=team_id,
                actor_user_id=actor_user_id,
                subagent_ids=normalized,
                governance_action_allowed=governance_action_allowed,
                now=now,
            )

    async def resolve_many_in_connection(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        actor_user_id: str,
        subagent_id: str | None = None,
        subagent_ids: Collection[str] | None = None,
        governance_action_allowed: bool = False,
        now: datetime | None = None,
    ) -> dict[str, TeamSubagentAccessDecision]:
        """Resolve canonical access inside the caller's admission transaction."""
        if subagent_id is not None and subagent_ids is not None:
            raise ValueError("subagent_id and subagent_ids are mutually exclusive")
        exact_ids = (
            tuple(dict.fromkeys(str(value) for value in subagent_ids if value))
            if subagent_ids is not None
            else ((subagent_id,) if subagent_id is not None else None)
        )
        if exact_ids == ():
            return {}
        timestamp = _utc_timestamp(now or datetime.now(UTC))
        params: dict[str, str | int | None] = {
            "team_id": team_id,
            "actor_id": actor_user_id,
            "subagent_ids_json": json.dumps(exact_ids) if exact_ids is not None else None,
            "now": timestamp,
            "governance_action_allowed": int(governance_action_allowed),
        }
        async with db.execute(_CANONICAL_ACCESS_FACTS_SQL, params) as cursor:
            rows = await cursor.fetchall()
        return {str(row["subagent_id"]): _decision_from_row(row) for row in rows}


def _decision_from_row(row: aiosqlite.Row) -> TeamSubagentAccessDecision:
    facts = TeamSubagentAccessFacts(
        actor_active=bool(row["actor_active"]),
        is_owner=bool(row["is_owner"]),
        owner_active=bool(row["owner_active"]),
        is_team_governor=bool(row["is_team_governor"]),
        governance_action_allowed=bool(row["governance_action_allowed"]),
        audience_mode=str(row["effective_audience_mode"]),
        has_user_allow=bool(row["has_user_allow"]),
        has_group_allow=bool(row["has_group_allow"]),
        has_security_deny=bool(row["has_security_deny"]),
        lifecycle_state=str(row["effective_lifecycle_state"]),
        status=str(row["status"]),
    )
    return TeamSubagentAccessDecision(
        subagent_id=str(row["subagent_id"]),
        owner_user_id=(str(row["effective_owner_user_id"]) if row["effective_owner_user_id"] is not None else None),
        facts=facts,
        resolution=resolve_team_subagent_access(facts),
        row_version=int(row["row_version"]),
    )


def _audience_reason(facts: TeamSubagentAccessFacts) -> str | None:
    if facts.is_owner:
        return "owner"
    if facts.audience_mode == "teamwide":
        return "teamwide"
    if facts.audience_mode != "custom":
        return None
    if facts.has_user_allow:
        return "user_grant"
    if facts.has_group_allow:
        return "group_grant"
    return None


def _consumer_denial_reason(facts: TeamSubagentAccessFacts) -> str:
    if not facts.actor_active:
        return "actor_inactive"
    if facts.lifecycle_state != "active":
        return "lifecycle_not_active"
    if facts.status != "enabled":
        return "status_not_enabled"
    if not facts.owner_active:
        return "owner_inactive"
    if _audience_reason(facts) is None:
        return "audience_not_allowed"
    if facts.has_security_deny:
        return "blocked_by_security_policy"
    return "access_indeterminate"


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("team_subagent_access_now_must_be_timezone_aware")
    return value.astimezone(UTC).isoformat()


_CANONICAL_ACCESS_FACTS_SQL = """
WITH semantics AS (
  SELECT
    COALESCE(
      MAX(owner_semantics_activated_at IS NOT NULL),
      0
    ) AS owner_activated,
    COALESCE(
      MAX(lifecycle_semantics_activated_at IS NOT NULL),
      0
    ) AS lifecycle_activated,
    COALESCE(
      MAX(audience_semantics_activated_at IS NOT NULL),
      0
    ) AS audience_activated
  FROM team_subagent_schema_meta
  WHERE singleton = 1
),
targets AS MATERIALIZED (
  SELECT
    sa.id AS subagent_id,
    sa.team_id,
    sa.status,
    sa.row_version,
    CASE
      WHEN semantics.owner_activated
        THEN sa.owner_user_id
      ELSE sa.created_by
    END AS effective_owner_user_id,
    CASE
      WHEN semantics.lifecycle_activated
        THEN sa.lifecycle_state
      ELSE 'active'
    END AS effective_lifecycle_state,
    CASE
      WHEN semantics.audience_activated
        THEN sa.audience_mode
      WHEN sa.visibility = 'team_wide'
        THEN 'teamwide'
      WHEN EXISTS (
        SELECT 1
        FROM resource_grants legacy_allow
        WHERE legacy_allow.team_id = sa.team_id
          AND legacy_allow.resource_kind = 'team_subagent'
          AND legacy_allow.resource_id = sa.id
          AND legacy_allow.principal_type IN ('user', 'group')
          AND legacy_allow.permission IN ('use', 'write', 'admin')
          AND legacy_allow.effect = 'allow'
          AND legacy_allow.revoked_at IS NULL
          AND (
            legacy_allow.expires_at IS NULL
            OR legacy_allow.expires_at > :now
          )
      )
        THEN 'custom'
      ELSE 'private'
    END AS effective_audience_mode
  FROM team_subagent sa
  CROSS JOIN semantics
  WHERE sa.team_id = :team_id
    AND (
      :subagent_ids_json IS NULL
      OR sa.id IN (SELECT value FROM json_each(:subagent_ids_json))
    )
),
facts AS (
  SELECT
    targets.*,
    EXISTS (
      SELECT 1
      FROM users actor
      JOIN team_members actor_member
        ON actor_member.user_id = actor.id
       AND actor_member.team_id = targets.team_id
       AND actor_member.status = 'active'
      WHERE actor.id = :actor_id
        AND actor.status = 'active'
    ) AS actor_active,
    targets.effective_owner_user_id = :actor_id AS is_owner,
    EXISTS (
      SELECT 1
      FROM users owner
      JOIN team_members owner_member
        ON owner_member.user_id = owner.id
       AND owner_member.team_id = targets.team_id
       AND owner_member.status = 'active'
      WHERE owner.id = targets.effective_owner_user_id
        AND owner.status = 'active'
    ) AS owner_active,
    (
      EXISTS (
        SELECT 1
        FROM teams
        WHERE teams.id = targets.team_id
          AND teams.created_by_user_id = :actor_id
      )
      OR EXISTS (
        SELECT 1
        FROM team_members governor_member
        WHERE governor_member.team_id = targets.team_id
          AND governor_member.user_id = :actor_id
          AND governor_member.role = 'team_admin'
          AND governor_member.status = 'active'
      )
    ) AS is_team_governor,
    :governance_action_allowed AS governance_action_allowed,
    EXISTS (
      SELECT 1
      FROM resource_grants user_allow
      WHERE user_allow.team_id = targets.team_id
        AND user_allow.resource_kind = 'team_subagent'
        AND user_allow.resource_id = targets.subagent_id
        AND user_allow.principal_type = 'user'
        AND user_allow.principal_id = :actor_id
        AND user_allow.permission IN ('use', 'write', 'admin')
        AND user_allow.effect = 'allow'
        AND user_allow.revoked_at IS NULL
        AND (
          user_allow.expires_at IS NULL
          OR user_allow.expires_at > :now
        )
    ) AS has_user_allow,
    EXISTS (
      SELECT 1
      FROM resource_grants group_allow
      WHERE group_allow.team_id = targets.team_id
        AND group_allow.resource_kind = 'team_subagent'
        AND group_allow.resource_id = targets.subagent_id
        AND group_allow.principal_type = 'group'
        AND group_allow.permission IN ('use', 'write', 'admin')
        AND group_allow.effect = 'allow'
        AND group_allow.revoked_at IS NULL
        AND (
          group_allow.expires_at IS NULL
          OR group_allow.expires_at > :now
        )
        AND EXISTS (
          SELECT 1
          FROM group_memberships allow_membership
          JOIN groups allow_group
            ON allow_group.id = allow_membership.group_id
           AND allow_group.team_id = allow_membership.team_id
           AND allow_group.status = 'active'
          WHERE allow_membership.team_id = targets.team_id
            AND allow_membership.group_id = group_allow.principal_id
            AND allow_membership.principal_type = 'user'
            AND allow_membership.principal_id = :actor_id
            AND allow_membership.status = 'active'
        )
    ) AS has_group_allow,
    EXISTS (
      SELECT 1
      FROM resource_grants security_deny
      WHERE security_deny.team_id = targets.team_id
        AND security_deny.resource_kind = 'team_subagent'
        AND security_deny.resource_id = targets.subagent_id
        AND security_deny.permission IN ('use', 'write', 'admin')
        AND security_deny.effect = 'deny'
        AND security_deny.revoked_at IS NULL
        AND (
          security_deny.expires_at IS NULL
          OR security_deny.expires_at > :now
        )
        AND (
          (
            security_deny.principal_type = 'user'
            AND security_deny.principal_id = :actor_id
          )
          OR (
            security_deny.principal_type = 'group'
            AND EXISTS (
              SELECT 1
              FROM group_memberships deny_membership
              JOIN groups deny_group
                ON deny_group.id = deny_membership.group_id
               AND deny_group.team_id = deny_membership.team_id
               AND deny_group.status = 'active'
              WHERE deny_membership.team_id = targets.team_id
                AND deny_membership.group_id = security_deny.principal_id
                AND deny_membership.principal_type = 'user'
                AND deny_membership.principal_id = :actor_id
                AND deny_membership.status = 'active'
            )
          )
        )
    ) AS has_security_deny
  FROM targets
)
SELECT *
FROM facts
ORDER BY subagent_id
"""
