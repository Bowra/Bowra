"""Actor-scoped reverse usage view for Skills and Team Tool sources.

The durable edge projection answers dependency identity.  This service adds
the current/draft/running/history relation, applies the canonical Agent access
resolver, and emits only bounded metadata suitable for management UIs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.team_subagent_access import TeamSubagentAccessDecision, TeamSubagentAccessResolver

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

UsageResourceKind = Literal["skill", "tool_source", "tool_operation"]
UsageGroup = Literal["current", "running", "draft", "history"]
Readiness = Literal["ready", "degraded", "unavailable", "unknown"]


class ResourceUsageUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResourceUsageSelector:
    key: str
    kind: UsageResourceKind
    resource_id: str
    display_name: str
    resource_ref: str | None = None
    parent_ref: str | None = None
    revision: str | None = None
    available: bool = True

    def matches(self, row: Mapping[str, Any]) -> bool:
        if self.kind == "skill":
            return row["resource_kind"] == "skill" and row["resource_ref"] == self.resource_ref
        if self.kind == "tool_operation":
            return (
                row["resource_kind"] == "tool_operation"
                and row["resource_ref"] == self.resource_ref
                and row["resource_parent_ref"] == self.parent_ref
            )
        return row["resource_kind"] == "tool_operation" and row["resource_parent_ref"] == self.parent_ref


@dataclass(frozen=True, slots=True)
class ResourceUsageSummary:
    current_agent_count: int = 0
    running_task_count: int = 0
    draft_agent_count: int = 0
    historical_agent_count: int = 0
    scheduled_binding_count: int = 0
    restricted_count: int = 0
    completeness: Literal["complete", "partial"] = "complete"
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reason_codes"] = list(self.reason_codes)
        return value


@dataclass(frozen=True, slots=True)
class ResourceUsageItem:
    kind: Literal["agent", "task"]
    id: str
    agent_id: str
    display_name: str
    relation: UsageGroup
    readiness: Readiness
    subject_ref: str
    evidence_level: str
    resource_revision: str | None
    href: str | None
    status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResourceUsageView:
    resource: ResourceUsageSelector
    summary: ResourceUsageSummary
    groups: Mapping[UsageGroup, tuple[ResourceUsageItem, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "resource_usage",
            "schema_version": 1,
            "resource": {
                "kind": self.resource.kind,
                "id": self.resource.resource_id,
                "display_name": self.resource.display_name,
                "revision": self.resource.revision,
            },
            "summary": self.summary.to_dict(),
            "actual_usage_availability": "unavailable",
            "completeness": self.summary.completeness,
            "reason_codes": list(self.summary.reason_codes),
            "groups": {name: [item.to_dict() for item in self.groups.get(name, ())] for name in ("current", "running", "draft", "history")},
            "next_cursors": {},
        }


class ResourceUsageProjectionService:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)

    async def usage(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        selector: ResourceUsageSelector,
        governance_action_allowed: bool = False,
    ) -> ResourceUsageView:
        views = await self.usage_many(
            team_id=team_id,
            actor_user_id=actor_user_id,
            selectors=(selector,),
            governance_action_allowed=governance_action_allowed,
            include_groups=True,
        )
        return views[selector.key]

    async def summaries(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        selectors: Sequence[ResourceUsageSelector],
        governance_action_allowed: bool = False,
    ) -> dict[str, ResourceUsageSummary]:
        views = await self.usage_many(
            team_id=team_id,
            actor_user_id=actor_user_id,
            selectors=selectors,
            governance_action_allowed=governance_action_allowed,
            include_groups=False,
        )
        return {key: view.summary for key, view in views.items()}

    async def usage_many(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        selectors: Sequence[ResourceUsageSelector],
        governance_action_allowed: bool,
        include_groups: bool,
    ) -> dict[str, ResourceUsageView]:
        if not team_id or not actor_user_id:
            raise ResourceUsageUnavailableError("resource_usage_scope_missing")
        unique = {selector.key: selector for selector in selectors}
        if not unique:
            return {}
        try:
            async with open_shared_ctx(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                return await self.usage_many_in_connection(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    selectors=tuple(unique.values()),
                    governance_action_allowed=governance_action_allowed,
                    include_groups=include_groups,
                )
        except (aiosqlite.Error, OSError) as exc:
            raise ResourceUsageUnavailableError("resource_usage_unavailable") from exc

    async def usage_many_in_connection(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        actor_user_id: str,
        selectors: Sequence[ResourceUsageSelector],
        governance_action_allowed: bool,
        include_groups: bool,
    ) -> dict[str, ResourceUsageView]:
        """Build the projection using a caller-owned admission transaction."""

        if not team_id or not actor_user_id:
            raise ResourceUsageUnavailableError("resource_usage_scope_missing")
        # Caller-owned connections default to tuple rows. Every projection query and the
        # canonical Agent access resolver consume named columns, so normalize the connection
        # here instead of relying on each impact-preview caller to remember this hidden
        # precondition. Empty-team tests previously masked the bug because no row was indexed.
        db.row_factory = aiosqlite.Row
        unique = {selector.key: selector for selector in selectors}
        if not unique:
            return {}
        try:
            edges = await _team_edges(db, team_id=team_id)
            executions = await _team_executions(db, team_id=team_id)
            completeness_reasons = await _projection_completeness_reasons(
                db,
                team_id=team_id,
            )
            system_assignments = await _system_assignments(
                db,
                team_id=team_id,
                actor_user_id=actor_user_id,
            )
            actor_is_governor = await _actor_is_governor(
                db,
                team_id=team_id,
                actor_user_id=actor_user_id,
            )
            decisions = await TeamSubagentAccessResolver(self.db_path).resolve_many_in_connection(
                db,
                team_id=team_id,
                actor_user_id=actor_user_id,
                governance_action_allowed=governance_action_allowed,
                now=datetime.now(UTC),
            )
        except (aiosqlite.Error, OSError) as exc:
            raise ResourceUsageUnavailableError("resource_usage_unavailable") from exc

        result: dict[str, ResourceUsageView] = {}
        for key, selector in unique.items():
            matched = [row for row in edges if selector.matches(row)]
            view = _build_view(
                selector=selector,
                rows=matched,
                executions=executions,
                decisions=decisions,
                system_assignments=system_assignments,
                actor_user_id=actor_user_id,
                actor_is_governor=actor_is_governor and governance_action_allowed,
                base_completeness_reasons=completeness_reasons,
                include_groups=include_groups,
            )
            result[key] = view
        return result


def _build_view(
    *,
    selector: ResourceUsageSelector,
    rows: Sequence[Mapping[str, Any]],
    executions: Sequence[Mapping[str, Any]],
    decisions: Mapping[str, TeamSubagentAccessDecision],
    system_assignments: frozenset[str],
    actor_user_id: str,
    actor_is_governor: bool,
    base_completeness_reasons: set[str],
    include_groups: bool,
) -> ResourceUsageView:
    groups: dict[UsageGroup, dict[tuple[str, str], ResourceUsageItem]] = {
        "current": {},
        "running": {},
        "draft": {},
        "history": {},
    }
    restricted_agents: set[str] = set()
    visible_subjects: dict[tuple[str, str], Mapping[str, Any]] = {}
    reasons = set(base_completeness_reasons)
    for row in rows:
        agent_id = str(row["agent_id"])
        visible, can_consume = _agent_visibility(
            agent_id=agent_id,
            decisions=decisions,
            system_assignments=system_assignments,
            actor_is_governor=actor_is_governor,
        )
        if not visible:
            restricted_agents.add(agent_id)
            continue
        relation = _relation(row)
        if row["projection_status"] == "partial":
            reasons.add(str(row["projection_reason"] or "resource_dependency_projection_partial"))
        readiness = _readiness(
            row,
            relation=relation,
            can_consume=can_consume,
            resource_available=selector.available,
        )
        item = ResourceUsageItem(
            kind="agent",
            id=agent_id,
            agent_id=agent_id,
            display_name=_display_name(row),
            relation=relation,
            readiness=readiness,
            subject_ref=str(row["subject_ref"]),
            evidence_level=str(row["evidence_level"]),
            resource_revision=(str(row["resource_revision"]) if row["resource_revision"] is not None else None),
            href=(f"/agents/detail/?id={agent_id}" if not agent_id.startswith("system:") else None),
            status=(str(row["agent_status"]) if row["agent_status"] is not None else None),
        )
        groups[relation][(agent_id, str(row["subject_ref"]))] = item
        visible_subjects[(str(row["subject_kind"]), str(row["subject_ref"]))] = row

    scheduled_count = 0
    scheduled_subjects: set[tuple[str, str]] = set()
    for row in rows:
        subject_key = (str(row["agent_id"]), str(row["subject_ref"]))
        if row["scheduled_binding_count"] is not None and subject_key not in scheduled_subjects:
            visible, _can_consume = _agent_visibility(
                agent_id=str(row["agent_id"]),
                decisions=decisions,
                system_assignments=system_assignments,
                actor_is_governor=actor_is_governor,
            )
            if visible:
                scheduled_count += int(row["scheduled_binding_count"])
                scheduled_subjects.add(subject_key)

    for execution in executions:
        subject_key = (
            "system_agent_revision" if str(execution["agent_source_kind"]) == "system" else "agent_revision",
            str(execution["revision_ref"]),
        )
        source = visible_subjects.get(subject_key)
        if source is None:
            continue
        if str(execution["session_user_id"] or "") != actor_user_id:
            continue
        execution_group: UsageGroup = "running" if str(execution["status"]) in {"admitted", "running"} else "history"
        groups[execution_group][(str(execution["id"]), str(execution["revision_ref"]))] = ResourceUsageItem(
            kind="task",
            id=str(execution["turn_id"]),
            agent_id=str(execution["agent_id"]),
            display_name=_display_name(source),
            relation=execution_group,
            readiness=("unknown" if source["projection_status"] == "partial" else "ready"),
            subject_ref=str(execution["revision_ref"]),
            evidence_level=str(source["evidence_level"]),
            resource_revision=(str(source["resource_revision"]) if source["resource_revision"] is not None else None),
            href=f"/chat/?turn={execution['turn_id']}",
            status=str(execution["status"]),
        )

    summary = ResourceUsageSummary(
        current_agent_count=len({item.agent_id for item in groups["current"].values()}),
        running_task_count=len(groups["running"]),
        draft_agent_count=len({item.agent_id for item in groups["draft"].values()}),
        historical_agent_count=len({item.agent_id for item in groups["history"].values()}),
        scheduled_binding_count=scheduled_count,
        restricted_count=len(restricted_agents),
        completeness="partial" if reasons else "complete",
        reason_codes=tuple(sorted(reasons)[:8]),
    )
    frozen_groups = {
        name: tuple(
            sorted(
                values.values(),
                key=lambda item: (item.display_name.casefold(), item.agent_id, item.subject_ref),
            )[:20]
        )
        for name, values in groups.items()
    }
    if not include_groups:
        frozen_groups = dict.fromkeys(frozen_groups, ())
    return ResourceUsageView(resource=selector, summary=summary, groups=frozen_groups)


def _agent_visibility(
    *,
    agent_id: str,
    decisions: Mapping[str, TeamSubagentAccessDecision],
    system_assignments: frozenset[str],
    actor_is_governor: bool,
) -> tuple[bool, bool]:
    if agent_id.startswith("system:"):
        assigned = agent_id in system_assignments
        return assigned or actor_is_governor, assigned
    decision = decisions.get(agent_id)
    if decision is None:
        return False, False
    resolution = decision.resolution
    return resolution.catalog_visible or (actor_is_governor and resolution.can_govern), resolution.can_consume


def _relation(row: Mapping[str, Any]) -> UsageGroup:
    if row["subject_kind"] == "agent_working_copy" or bool(row["is_pending_candidate"]):
        return "draft"
    if row["subject_kind"] == "system_agent_revision":
        return "current" if bool(row["is_latest_system_revision"]) else "history"
    return "current" if row["live_revision_id"] == row["subject_ref"] else "history"


def _readiness(
    row: Mapping[str, Any],
    *,
    relation: UsageGroup,
    can_consume: bool,
    resource_available: bool,
) -> Readiness:
    if row["projection_status"] == "partial" or row["evidence_level"] != "exact":
        return "unknown"
    if relation == "history" or not resource_available or not can_consume:
        return "unavailable"
    if row["agent_status"] not in {None, "enabled"} or row["lifecycle_state"] not in {None, "active"}:
        return "unavailable"
    return "ready"


def _display_name(row: Mapping[str, Any]) -> str:
    display = str(row["display_name"] or row["routing_name"] or "")
    if display:
        return display
    return str(row["agent_id"]).removeprefix("system:")


async def _team_edges(db: aiosqlite.Connection, *, team_id: str) -> list[aiosqlite.Row]:
    async with db.execute(
        """
        SELECT edge.*,
               projection.status AS projection_status,
               projection.reason_code AS projection_reason,
               agent.display_name,agent.name AS routing_name,
               agent.status AS agent_status,agent.lifecycle_state,
               agent.live_revision_id,
               EXISTS(
                 SELECT 1 FROM team_subagent_release_candidate candidate
                 WHERE candidate.team_id=edge.team_id
                   AND candidate.subagent_id=edge.agent_id
                   AND candidate.revision_id=edge.subject_ref
                   AND candidate.state='pending'
               ) AS is_pending_candidate,
               CASE WHEN edge.subject_kind='system_agent_revision' THEN
                 edge.subject_ref=(
                   SELECT system_revision.id
                   FROM system_agent_effective_revision system_revision
                   WHERE system_revision.team_id=edge.team_id
                     AND system_revision.agent_id=edge.agent_id
                   ORDER BY system_revision.created_at DESC,system_revision.id DESC
                   LIMIT 1
                 )
               ELSE 0 END AS is_latest_system_revision,
               (
                 SELECT COUNT(*)
                 FROM scheduled_job_agent_binding binding
                 JOIN scheduled_jobs job ON job.id=binding.job_id
                 WHERE binding.agent_id=edge.agent_id
                   AND binding.revision_ref=edge.subject_ref
                   AND binding.state='ready'
                   AND job.status='active'
               ) AS scheduled_binding_count
        FROM resource_dependency_edge edge
        JOIN resource_dependency_subject_projection projection
          ON projection.team_id=edge.team_id
         AND projection.subject_kind=edge.subject_kind
         AND projection.subject_ref=edge.subject_ref
        LEFT JOIN team_subagent agent
          ON agent.team_id=edge.team_id AND agent.id=edge.agent_id
        WHERE edge.team_id=?
        """,
        (team_id,),
    ) as cursor:
        return await cursor.fetchall()


async def _team_executions(db: aiosqlite.Connection, *, team_id: str) -> list[aiosqlite.Row]:
    async with db.execute(
        """
        SELECT execution.*,session.user_id AS session_user_id
        FROM turn_agent_execution execution
        JOIN turns turn_row ON turn_row.id=execution.turn_id
        JOIN sessions session ON session.id=turn_row.session_id
        WHERE execution.team_id=?
        """,
        (team_id,),
    ) as cursor:
        return await cursor.fetchall()


async def _system_assignments(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    actor_user_id: str,
) -> frozenset[str]:
    async with db.execute(
        """
        SELECT agent_id FROM agent_assignment
        WHERE team_id=? AND user_id=? AND agent_source_kind='system'
          AND personal_state='active' AND access_state='available'
        """,
        (team_id, actor_user_id),
    ) as cursor:
        return frozenset(str(row[0]) for row in await cursor.fetchall())


async def _actor_is_governor(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    actor_user_id: str,
) -> bool:
    async with db.execute(
        """
        SELECT 1
        FROM team_members member
        LEFT JOIN teams team ON team.id=member.team_id
        WHERE member.team_id=? AND member.user_id=? AND member.status='active'
          AND (member.role='team_admin' OR team.created_by_user_id=member.user_id)
        LIMIT 1
        """,
        (team_id, actor_user_id),
    ) as cursor:
        return await cursor.fetchone() is not None


async def _projection_completeness_reasons(
    db: aiosqlite.Connection,
    *,
    team_id: str,
) -> set[str]:
    async with db.execute(
        """
        SELECT
          EXISTS(
            SELECT 1 FROM team_subagent_working_copy subject
            LEFT JOIN resource_dependency_subject_projection projection
              ON projection.team_id=subject.team_id
             AND projection.subject_kind='agent_working_copy'
             AND projection.subject_ref=subject.id
            WHERE subject.team_id=? AND projection.subject_ref IS NULL
          ) OR EXISTS(
            SELECT 1 FROM team_subagent_revision subject
            LEFT JOIN resource_dependency_subject_projection projection
              ON projection.team_id=subject.team_id
             AND projection.subject_kind='agent_revision'
             AND projection.subject_ref=subject.id
             AND projection.subject_fingerprint=subject.definition_hash
            WHERE subject.team_id=? AND projection.subject_ref IS NULL
          ) OR EXISTS(
            SELECT 1 FROM system_agent_effective_revision subject
            LEFT JOIN resource_dependency_subject_projection projection
              ON projection.team_id=subject.team_id
             AND projection.subject_kind='system_agent_revision'
             AND projection.subject_ref=subject.id
             AND projection.subject_fingerprint=subject.definition_hash
            WHERE subject.team_id=? AND projection.subject_ref IS NULL
          ) AS incomplete,
          -- advertise-execute-gap: an ENABLED team Agent that is not `revisioned` cannot be
          -- projected at all — the three subjects above are working copies / revisions / system
          -- revisions, and the backfill's `_subject_page` scans exactly those. A pre-cutover
          -- deployment keeps its real Agents in `team_subagent` with definition_mode
          -- 'legacy_mutable', so NOTHING in this pipeline ever reads what tools they use.
          --
          -- Without this reason the surface reports zero matches as `complete`, and the UI turns
          -- "may be incomplete" into "not used by any Agent" — an assertion nobody verified.
          -- 116 measured: 6 legacy Agents, 0 dependency edges referencing any team tool source.
          -- The "incomplete" flag above happens to mask it today, but that flag CLEARS ITSELF
          -- (dispatch projects system Agents), so the false claim would appear on its own.
          --
          -- Self-correcting: once M2 cutover marks these Agents `revisioned`, their revisions
          -- enter the subject universe and this reason stops firing with no cleanup needed.
          EXISTS(
            SELECT 1 FROM team_subagent
            WHERE team_id=? AND status='enabled' AND definition_mode<>'revisioned'
          ) AS unprojectable
        """,
        (team_id, team_id, team_id, team_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return set()
    reasons: set[str] = set()
    if bool(row[0]):
        reasons.add("resource_dependency_projection_incomplete")
    if bool(row[1]):
        reasons.add("team_agents_not_projectable")
    return reasons


__all__ = [
    "ResourceUsageProjectionService",
    "ResourceUsageSelector",
    "ResourceUsageSummary",
    "ResourceUsageUnavailableError",
    "ResourceUsageView",
]
