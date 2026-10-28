"""Personal, assignment-cycle scoped preferences for behavior-driven Agent roles."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_behavior_contract import (
    LEAD_ROLE_IDS,
    AgentBehaviorDescriptor,
    LeadRoleId,
)
from bowhead_core.agent_behavior_flags import agent_behavior_m3_enabled
from bowhead_core.agent_capability_catalog import CapabilityCatalog, default_catalog
from bowhead_core.agent_listing_safety import scan_agent_listing
from bowhead_core.agent_role_candidates import (
    LeadRoleCandidateRoster,
    new_turn_candidate,
)
from bowhead_core.team_subagent_access import (
    TeamSubagentAccessDecision,
    TeamSubagentAccessResolver,
)

_MAX_ASSIGNMENTS = 100

RoleAvailability = Literal["ready", "no_eligible_agent", "behavior_unavailable"]


class AgentRolePreferenceError(RuntimeError):
    """Stable role-preference command failure."""

    def __init__(self, code: str, *, current_version: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.current_version = current_version


@dataclass(frozen=True, slots=True)
class AgentRolePreferenceCandidate:
    assignment_id: str
    agent_id: str
    runtime_profile_name: str
    revision_ref: str
    release_id: str
    behavior_fingerprint: str
    display_name: str
    description: str
    eligible_role_ids: tuple[LeadRoleId, ...]
    primary_role_ids: tuple[LeadRoleId, ...]

    def to_dict(self, *, role_id: LeadRoleId) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "agent_id": self.agent_id,
            "revision_ref": self.revision_ref,
            "release_id": self.release_id,
            "behavior_fingerprint": self.behavior_fingerprint,
            "display_name": self.display_name,
            "description": self.description,
            "is_primary": role_id in self.primary_role_ids,
        }


@dataclass(frozen=True, slots=True)
class AgentRolePreferenceRole:
    role_id: LeadRoleId
    row_version: int
    saved_assignment_id: str | None
    preference: AgentRolePreferenceCandidate | None
    invalid_reason: str | None
    availability: RoleAvailability
    candidates: tuple[AgentRolePreferenceCandidate, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "label_key": f"role_{self.role_id}",
            "description_key": f"role_{self.role_id}_description",
            "row_version": self.row_version,
            "saved_assignment_id": self.saved_assignment_id,
            "preference": (self.preference.to_dict(role_id=self.role_id) if self.preference is not None else None),
            "invalid_reason": self.invalid_reason,
            "availability": self.availability,
            "candidates": [item.to_dict(role_id=self.role_id) for item in self.candidates],
            "actions": {
                "set": bool(self.candidates),
                "clear": self.saved_assignment_id is not None,
            },
        }


@dataclass(frozen=True, slots=True)
class AgentRolePreferenceSnapshot:
    roles: tuple[AgentRolePreferenceRole, ...]
    candidates_truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "agent_role_preferences",
            "schema_version": 1,
            "roles": [role.to_dict() for role in self.roles],
            "candidates_truncated": self.candidates_truncated,
        }


class AgentRolePreferenceStore:
    """CAS store whose reads re-evaluate assignment, access, and behavior."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        catalog: CapabilityCatalog | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._access = TeamSubagentAccessResolver(self.db_path)
        self._catalog = catalog or default_catalog()

    async def initialize(self) -> None:
        async with open_shared_ctx(self.db_path) as db:
            async with db.execute("PRAGMA table_info(agent_role_preference)") as cursor:
                columns = {str(row[1]) for row in await cursor.fetchall()}
            required = {
                "team_id",
                "user_id",
                "role_id",
                "assignment_id",
                "assignment_cycle_started_at",
                "cleared_at",
                "row_version",
                "created_at",
                "updated_at",
            }
            if columns != required:
                raise AgentRolePreferenceError("behavior_unavailable")
            async with db.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='agent_role_preference'",
            ) as cursor:
                triggers = {str(row[0]) for row in await cursor.fetchall()}
        if (
            not {
                "trg_agent_role_preference_insert_scope",
                "trg_agent_role_preference_update_guard",
            }
            <= triggers
        ):
            raise AgentRolePreferenceError("behavior_unavailable")

    async def snapshot(
        self,
        *,
        team_id: str,
        user_id: str,
    ) -> AgentRolePreferenceSnapshot:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            try:
                result = await self._snapshot_conn(
                    db,
                    team_id=team_id,
                    user_id=user_id,
                )
                await db.commit()
                return result
            except BaseException:
                await db.rollback()
                raise

    async def set(
        self,
        *,
        team_id: str,
        user_id: str,
        role_id: str,
        assignment_id: str,
        expected_row_version: int,
    ) -> AgentRolePreferenceSnapshot:
        role = _role_id(role_id)
        if expected_row_version < 0:
            raise AgentRolePreferenceError("row_version_conflict", current_version=0)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                current = await _preference_row_conn(
                    db,
                    team_id=team_id,
                    user_id=user_id,
                    role_id=role,
                )
                current_version = int(current["row_version"]) if current is not None else 0
                if current_version != expected_row_version:
                    raise AgentRolePreferenceError(
                        "row_version_conflict",
                        current_version=current_version,
                    )
                candidate, reason = await self._candidate_for_assignment_conn(
                    db,
                    team_id=team_id,
                    user_id=user_id,
                    assignment_id=assignment_id,
                    role_id=role,
                )
                if candidate is None:
                    raise AgentRolePreferenceError(reason or "assignment_not_eligible")
                assignment = await _assignment_row_conn(db, assignment_id=assignment_id)
                if assignment is None:
                    raise AgentRolePreferenceError("assignment_not_eligible")
                cycle = str(assignment["current_cycle_started_at"])
                if current is not None and str(current["assignment_id"]) == assignment_id and str(current["assignment_cycle_started_at"]) == cycle:
                    await db.rollback()
                    return await self.snapshot(team_id=team_id, user_id=user_id)
                timestamp = _now()
                if current is None:
                    await db.execute(
                        """
                        INSERT INTO agent_role_preference(
                          team_id,user_id,role_id,assignment_id,assignment_cycle_started_at,
                          row_version,created_at,updated_at
                        ) VALUES (?,?,?,?,?,1,?,?)
                        """,
                        (team_id, user_id, role, assignment_id, cycle, timestamp, timestamp),
                    )
                else:
                    result = await db.execute(
                        """
                        UPDATE agent_role_preference
                        SET assignment_id=?,assignment_cycle_started_at=?,cleared_at=NULL,
                            row_version=row_version+1,updated_at=?
                        WHERE team_id=? AND user_id=? AND role_id=? AND row_version=?
                        """,
                        (
                            assignment_id,
                            cycle,
                            timestamp,
                            team_id,
                            user_id,
                            role,
                            expected_row_version,
                        ),
                    )
                    if result.rowcount != 1:
                        raise AgentRolePreferenceError("row_version_conflict")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.snapshot(team_id=team_id, user_id=user_id)

    async def clear(
        self,
        *,
        team_id: str,
        user_id: str,
        role_id: str,
        expected_row_version: int,
    ) -> AgentRolePreferenceSnapshot:
        role = _role_id(role_id)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                current = await _preference_row_conn(
                    db,
                    team_id=team_id,
                    user_id=user_id,
                    role_id=role,
                )
                current_version = int(current["row_version"]) if current is not None else 0
                if current is None or current_version != expected_row_version:
                    raise AgentRolePreferenceError(
                        "row_version_conflict",
                        current_version=current_version,
                    )
                if current["assignment_id"] is None:
                    await db.rollback()
                    return await self.snapshot(team_id=team_id, user_id=user_id)
                timestamp = _now()
                result = await db.execute(
                    """
                    UPDATE agent_role_preference
                    SET assignment_id=NULL,assignment_cycle_started_at=NULL,cleared_at=?,
                        row_version=row_version+1,updated_at=?
                    WHERE team_id=? AND user_id=? AND role_id=? AND row_version=?
                    """,
                    (
                        timestamp,
                        timestamp,
                        team_id,
                        user_id,
                        role,
                        expected_row_version,
                    ),
                )
                if result.rowcount != 1:
                    raise AgentRolePreferenceError("row_version_conflict")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.snapshot(team_id=team_id, user_id=user_id)

    async def effective_assignment_for_runtime(
        self,
        *,
        team_id: str,
        user_id: str,
        role_id: str,
    ) -> str | None:
        """Read a valid preference only after the behavior cutover is enabled."""
        if not agent_behavior_m3_enabled():
            return None
        role = _role_id(role_id)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            try:
                row = await _preference_row_conn(
                    db,
                    team_id=team_id,
                    user_id=user_id,
                    role_id=role,
                )
                if row is None or row["assignment_id"] is None:
                    await db.commit()
                    return None
                candidate, _reason = await self._candidate_for_assignment_conn(
                    db,
                    team_id=team_id,
                    user_id=user_id,
                    assignment_id=str(row["assignment_id"]),
                    role_id=role,
                    expected_cycle=str(row["assignment_cycle_started_at"]),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return candidate.assignment_id if candidate is not None else None

    async def lead_candidate_roster(
        self,
        *,
        team_id: str,
        user_id: str,
    ) -> LeadRoleCandidateRoster:
        """Build the exact, opaque candidate seam without selecting a holder."""
        if not agent_behavior_m3_enabled():
            return LeadRoleCandidateRoster.build({})
        snapshot = await self.snapshot(team_id=team_id, user_id=user_id)
        return LeadRoleCandidateRoster.build(
            {
                role.role_id: tuple(
                    new_turn_candidate(
                        role_id=role.role_id,
                        agent_id=candidate.agent_id,
                        runtime_profile_name=candidate.runtime_profile_name,
                        assignment_id=candidate.assignment_id,
                        revision_ref=candidate.revision_ref,
                        release_id=candidate.release_id,
                        behavior_fingerprint=candidate.behavior_fingerprint,
                        is_primary=role.role_id in candidate.primary_role_ids,
                        is_preferred=(role.preference is not None and role.preference.assignment_id == candidate.assignment_id),
                        display_name=candidate.display_name,
                        description=candidate.description,
                    )
                    for candidate in role.candidates
                )
                for role in snapshot.roles
            },
        )

    async def _snapshot_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        user_id: str,
    ) -> AgentRolePreferenceSnapshot:
        async with db.execute(
            """
            SELECT * FROM agent_role_preference
            WHERE team_id=? AND user_id=? ORDER BY role_id
            """,
            (team_id, user_id),
        ) as cursor:
            preference_rows = {str(row["role_id"]): row for row in await cursor.fetchall()}
        async with db.execute(
            """
            SELECT assignment.id
            FROM agent_assignment assignment
            WHERE assignment.team_id=? AND assignment.user_id=?
              AND assignment.agent_source_kind='team'
              AND assignment.personal_state='active'
            ORDER BY assignment.updated_at DESC,assignment.id
            LIMIT ?
            """,
            (team_id, user_id, _MAX_ASSIGNMENTS + 1),
        ) as cursor:
            assignment_ids = tuple(str(row[0]) for row in await cursor.fetchall())
        truncated = len(assignment_ids) > _MAX_ASSIGNMENTS
        bounded_ids = assignment_ids[:_MAX_ASSIGNMENTS]
        decisions = await self._access.resolve_many_in_connection(
            db,
            team_id=team_id,
            actor_user_id=user_id,
            subagent_ids=await _agent_ids_for_assignments_conn(db, bounded_ids),
        )
        candidates_by_assignment = await _candidate_inventory_conn(
            db,
            team_id=team_id,
            user_id=user_id,
            assignment_ids=bounded_ids,
            decisions=decisions,
            catalog=self._catalog,
        )
        has_reviewed_behavior = bool(candidates_by_assignment)
        roles: list[AgentRolePreferenceRole] = []
        for role in LEAD_ROLE_IDS:
            stored = preference_rows.get(role)
            preference: AgentRolePreferenceCandidate | None = None
            invalid_reason: str | None = None
            saved_assignment_id = str(stored["assignment_id"]) if stored is not None and stored["assignment_id"] is not None else None
            if stored is not None and saved_assignment_id is not None:
                preference, invalid_reason = await self._candidate_for_assignment_conn(
                    db,
                    team_id=team_id,
                    user_id=user_id,
                    assignment_id=saved_assignment_id or "",
                    role_id=role,
                    expected_cycle=str(stored["assignment_cycle_started_at"]),
                )
            candidates = tuple(
                sorted(
                    (item for item in candidates_by_assignment.values() if role in item.eligible_role_ids),
                    key=lambda item: (
                        role not in item.primary_role_ids,
                        item.display_name.casefold(),
                        item.agent_id,
                    ),
                ),
            )
            if preference is not None and all(item.assignment_id != preference.assignment_id for item in candidates):
                candidates = (preference, *candidates[: _MAX_ASSIGNMENTS - 1])
            availability: RoleAvailability = "ready" if candidates else "no_eligible_agent" if has_reviewed_behavior else "behavior_unavailable"
            roles.append(
                AgentRolePreferenceRole(
                    role_id=role,
                    row_version=int(stored["row_version"]) if stored is not None else 0,
                    saved_assignment_id=saved_assignment_id,
                    preference=preference,
                    invalid_reason=invalid_reason,
                    availability=availability,
                    candidates=candidates,
                ),
            )
        return AgentRolePreferenceSnapshot(
            roles=tuple(roles),
            candidates_truncated=truncated,
        )

    async def _candidate_for_assignment_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        user_id: str,
        assignment_id: str,
        role_id: LeadRoleId,
        expected_cycle: str | None = None,
    ) -> tuple[AgentRolePreferenceCandidate | None, str | None]:
        assignment = await _assignment_row_conn(db, assignment_id=assignment_id)
        if assignment is None:
            return None, ("preference_stale" if expected_cycle is not None else "assignment_not_eligible")
        if str(assignment["team_id"]) != team_id or str(assignment["user_id"]) != user_id:
            return None, "assignment_not_eligible"
        if expected_cycle is not None and str(assignment["current_cycle_started_at"]) != expected_cycle:
            return None, "preference_stale"
        if str(assignment["personal_state"]) != "active":
            return None, ("preference_stale" if expected_cycle is not None else "assignment_not_eligible")
        if str(assignment["agent_source_kind"]) != "team":
            return None, "behavior_unavailable"
        agent_id = str(assignment["agent_id"])
        decision = await self._access.resolve_many_in_connection(
            db,
            team_id=team_id,
            actor_user_id=user_id,
            subagent_id=agent_id,
        )
        access = decision.get(agent_id)
        if access is None or not access.resolution.can_consume:
            return None, "assignment_not_eligible"
        row = await _joined_candidate_row_conn(
            db,
            team_id=team_id,
            user_id=user_id,
            assignment_id=assignment_id,
        )
        candidate = _candidate_from_joined_row(row, access, catalog=self._catalog) if row is not None else None
        if candidate is None:
            return None, "behavior_unavailable"
        if role_id not in candidate.eligible_role_ids:
            return None, "assignment_not_eligible"
        return candidate, None


def _role_id(value: str) -> LeadRoleId:
    if value not in LEAD_ROLE_IDS:
        raise AgentRolePreferenceError("invalid_role")
    return cast(LeadRoleId, value)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _preference_row_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    user_id: str,
    role_id: LeadRoleId,
) -> aiosqlite.Row | None:
    async with db.execute(
        """
        SELECT * FROM agent_role_preference
        WHERE team_id=? AND user_id=? AND role_id=?
        """,
        (team_id, user_id, role_id),
    ) as cursor:
        return await cursor.fetchone()


async def _assignment_row_conn(
    db: aiosqlite.Connection,
    *,
    assignment_id: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM agent_assignment WHERE id=?",
        (assignment_id,),
    ) as cursor:
        return await cursor.fetchone()


async def _agent_ids_for_assignments_conn(
    db: aiosqlite.Connection,
    assignment_ids: tuple[str, ...],
) -> tuple[str, ...]:
    if not assignment_ids:
        return ()
    placeholders = ",".join("?" for _ in assignment_ids)
    async with db.execute(
        f"SELECT agent_id FROM agent_assignment WHERE id IN ({placeholders})",  # noqa: S608 -- placeholders only
        assignment_ids,
    ) as cursor:
        return tuple(str(row[0]) for row in await cursor.fetchall())


async def _candidate_inventory_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    user_id: str,
    assignment_ids: tuple[str, ...],
    decisions: Mapping[str, TeamSubagentAccessDecision],
    catalog: CapabilityCatalog | None,
) -> dict[str, AgentRolePreferenceCandidate]:
    if not assignment_ids:
        return {}
    placeholders = ",".join("?" for _ in assignment_ids)
    async with db.execute(
        _JOINED_CANDIDATE_SQL + f" AND assignment.id IN ({placeholders}) ORDER BY assignment.id",  # noqa: S608 -- placeholders only
        (team_id, user_id, *assignment_ids),
    ) as cursor:
        rows = await cursor.fetchall()
    result: dict[str, AgentRolePreferenceCandidate] = {}
    for row in rows:
        agent_id = str(row["agent_id"])
        access = decisions.get(agent_id)
        candidate = _candidate_from_joined_row(row, access, catalog=catalog) if access is not None else None
        if candidate is not None:
            result[candidate.assignment_id] = candidate
    return result


async def _joined_candidate_row_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    user_id: str,
    assignment_id: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        _JOINED_CANDIDATE_SQL + " AND assignment.id=?",
        (team_id, user_id, assignment_id),
    ) as cursor:
        return await cursor.fetchone()


_JOINED_CANDIDATE_SQL = """
SELECT assignment.id AS assignment_id,assignment.agent_id,
       assignment.current_cycle_started_at,agent.name AS routing_name,
       agent.live_revision_id AS revision_ref,agent.live_release_id AS release_id,
       revision.definition_json,revision.definition_hash,revision.listing_json,
       revision.capability_fingerprint,revision.dependency_fingerprint,
       behavior.source_definition_hash AS behavior_source_definition_hash,
       behavior.descriptor_json,behavior.behavior_fingerprint AS stored_behavior_fingerprint,
       behavior.compiler_version AS stored_compiler_version,
       safety.content_hash AS safety_content_hash
FROM agent_assignment assignment
JOIN team_subagent agent
  ON agent.team_id=assignment.team_id AND agent.id=assignment.agent_id
 AND agent.status='enabled' AND agent.lifecycle_state='active'
 AND agent.definition_mode='revisioned'
JOIN team_subagent_release release
  ON release.team_id=agent.team_id AND release.subagent_id=agent.id
 AND release.id=agent.live_release_id AND release.revision_id=agent.live_revision_id
JOIN team_subagent_revision revision
  ON revision.team_id=release.team_id AND revision.subagent_id=release.subagent_id
 AND revision.id=release.revision_id
 AND revision.capability_fingerprint=release.capability_fingerprint
 AND revision.dependency_fingerprint=release.dependency_fingerprint
JOIN agent_safety_report safety
  ON safety.team_id=release.team_id AND safety.agent_id=release.subagent_id
 AND safety.id=release.safety_report_id
 AND safety.subject_kind='revision' AND safety.subject_ref=revision.id
 AND safety.dependency_fingerprint=revision.dependency_fingerprint
 AND safety.status NOT IN ('blocked','unknown')
JOIN team_subagent_revision_behavior behavior
  ON behavior.team_id=revision.team_id AND behavior.subagent_id=revision.subagent_id
 AND behavior.revision_id=revision.id
WHERE assignment.team_id=? AND assignment.user_id=?
  AND assignment.agent_source_kind='team' AND assignment.personal_state='active'
"""


def _candidate_from_joined_row(
    row: aiosqlite.Row,
    access: TeamSubagentAccessDecision | None,
    *,
    catalog: CapabilityCatalog | None,
) -> AgentRolePreferenceCandidate | None:
    if access is None or not access.resolution.can_consume:
        return None
    try:
        raw = str(row["descriptor_json"])
        descriptor = AgentBehaviorDescriptor.model_validate_json(raw)
        definition = json.loads(str(row["definition_json"]))
        listing = json.loads(str(row["listing_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(definition, dict) or not isinstance(listing, dict):
        return None
    definition_json = _canonical_json(definition)
    listing_json = _canonical_json(listing)
    if (
        descriptor.canonical_json() != raw
        or definition_json != str(row["definition_json"])
        or listing_json != str(row["listing_json"])
        or _hash_text(definition_json) != str(row["definition_hash"])
        or _hash_text(
            _canonical_json({"definition": definition, "listing": listing}),
        )
        != str(row["safety_content_hash"])
        or descriptor.verification_state != "reviewed"
        or descriptor.source_definition_hash != str(row["definition_hash"])
        or descriptor.source_definition_hash != str(row["behavior_source_definition_hash"])
        or descriptor.behavior_fingerprint != str(row["stored_behavior_fingerprint"])
        or descriptor.compiler_version != str(row["stored_compiler_version"])
        or descriptor.capability_fingerprint != str(row["capability_fingerprint"])
        or descriptor.dependency_fingerprint != str(row["dependency_fingerprint"])
    ):
        return None
    current_roles = _current_catalog_roles(descriptor, catalog)
    if current_roles is None or current_roles != descriptor.eligible_lead_role_ids:
        return None
    display_name = str(listing.get("display_name") or "").strip()
    description = str(listing.get("description") or "").strip()
    if not display_name:
        return None
    listing_safety = scan_agent_listing(
        name=listing.get("name") or row["routing_name"],
        display_name=display_name,
        description=description,
    )
    if listing_safety.blocked:
        return None
    return AgentRolePreferenceCandidate(
        assignment_id=str(row["assignment_id"]),
        agent_id=str(row["agent_id"]),
        runtime_profile_name=str(row["routing_name"]),
        revision_ref=str(row["revision_ref"]),
        release_id=str(row["release_id"]),
        behavior_fingerprint=descriptor.behavior_fingerprint,
        display_name=display_name[:160],
        description=listing_safety.normalized_description[:500],
        eligible_role_ids=descriptor.eligible_lead_role_ids,
        primary_role_ids=_primary_role_ids(descriptor, catalog),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _hash_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _current_catalog_roles(
    descriptor: AgentBehaviorDescriptor,
    catalog: CapabilityCatalog | None,
) -> tuple[LeadRoleId, ...] | None:
    if catalog is None:
        return None
    roles: set[LeadRoleId] = set()
    capability_refs: set[str] = set()
    for exact_ref in descriptor.implementation_refs:
        try:
            implementation_id, version = exact_ref.rsplit("@", 1)
            implementation = catalog.get_implementation(implementation_id, int(version))
            definition = catalog.get_definition(
                implementation.capability.id,
                implementation.capability.version,
            )
        except (KeyError, TypeError, ValueError):
            return None
        if (
            implementation.lifecycle != "active"
            or implementation.review_state != "reviewed"
            or definition.lifecycle != "active"
            or implementation.capability.exact_ref not in descriptor.effective_capability_refs
        ):
            return None
        capability_refs.add(implementation.capability.exact_ref)
        roles.update(implementation.lead_role_ids)
    if tuple(sorted(capability_refs)) != descriptor.effective_capability_refs:
        return None
    return tuple(sorted(roles))


def _primary_role_ids(
    descriptor: AgentBehaviorDescriptor,
    catalog: CapabilityCatalog | None,
) -> tuple[LeadRoleId, ...]:
    if descriptor.primary_capability_ref is None:
        return ()
    if catalog is None:
        return ()
    roles: set[LeadRoleId] = set()
    for exact_ref in descriptor.implementation_refs:
        try:
            implementation_id, version = exact_ref.rsplit("@", 1)
            implementation = catalog.get_implementation(implementation_id, int(version))
        except (KeyError, TypeError, ValueError):
            return ()
        if implementation.capability.exact_ref == descriptor.primary_capability_ref:
            roles.update(implementation.lead_role_ids)
    return tuple(role for role in LEAD_ROLE_IDS if role in roles)


__all__ = [
    "AgentRolePreferenceCandidate",
    "AgentRolePreferenceError",
    "AgentRolePreferenceRole",
    "AgentRolePreferenceSnapshot",
    "AgentRolePreferenceStore",
]
