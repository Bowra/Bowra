"""Durable Agent change proposals and Owner-controlled three-way merge."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_automation_context import AutomationAuthority
from bowhead_core.agent_behavior_authoring import default_agent_behavior_authoring_service
from bowhead_core.agent_eval_store import EvalCaseDraft, append_eval_suite_cases_conn
from bowhead_core.agent_revision_store import (
    AgentAudience,
    AgentPublicationBlockedError,
    AgentRevisionConflictError,
    AgentRevisionForbiddenError,
    AgentRevisionStore,
    _canonical_document,
    _canonical_json,
    _names_from_listing,
    _working_copy_from_row,
    _working_copy_hash,
    working_copy_content_hash,
)
from bowhead_core.resource_dependency_projection import invalidate_working_copy_projection_conn

if TYPE_CHECKING:
    from bowhead_core.agent_coach_generation import CoachGenerationClaim

ProposalSource = Literal["coach", "feedback", "regression", "dependency", "upstream", "git"]

_MAX_PATCH_BYTES = 512 * 1024
_MAX_GROUP_BYTES = 256 * 1024
_MAX_CASE_BYTES = 256 * 1024
_ALLOWED_EXACT_PATHS = {
    "/definition/system_prompt",
    "/definition/tool_refs",
    "/definition/skill_grants",
    "/definition/capability_ids",
    "/definition/builtin_plugin_ids",
    "/definition/model",
    "/definition/max_iterations",
    "/definition/output_schema",
    "/definition/runtime_extensions",
    "/definition/description",
    "/definition/model_policy",
    "/definition/tool_refs_json",
    "/definition/skill_refs_json",
    "/definition/capability_refs_json",
    "/definition/output_contract",
    "/listing/display_name",
    "/listing/name",
    "/listing/description",
    "/listing/category",
    "/listing/tags",
    "/audience/effective_mode",
    "/audience/user_ids",
    "/audience/group_ids",
}
_ALLOWED_PREFIXES = (
    "/definition/model_policy/",
    "/definition/output_contract/",
)
# Public names for the Team API reference, which documents exactly what `_path_allowed` accepts.
PROPOSAL_EXACT_PATHS = frozenset(_ALLOWED_EXACT_PATHS)
PROPOSAL_PATH_PREFIXES = _ALLOWED_PREFIXES


class AgentChangeProposalError(RuntimeError):
    """`details` names the failed rule and the offending item (safe to show the author or Coach)."""

    def __init__(self, code: str, *, conflicts: tuple[dict[str, Any], ...] = (), details: Mapping[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.conflicts = conflicts
        self.details = dict(details or {})


_GROUP_SHAPE = 'Each atomic group is {"id": "<unique name>", "operation_indexes": [<0-based operation index>, ...], "depends_on": [<group id>, ...]}.'


def _groups_invalid(rule: str, message: str, **facts: Any) -> AgentChangeProposalError:
    """`agent_change_groups_invalid` that says which rule failed, on which group, and how to fix it."""

    return AgentChangeProposalError("agent_change_groups_invalid", details={"rule": rule, **facts, "message": f"{message} {_GROUP_SHAPE}"})


_CASE_SHAPE = (
    'Each suggested case is {"id": "<unique id, 1-120 chars>", "task": "<non-empty, at most 8000 chars>", '
    '"failure_mode": "<non-empty, at most 2000 chars>", "case_spec": {"case_key", "expected_contract", "scorer_config", ...}}.'
)


def suggested_case_invalid(rule: str, message: str, **facts: Any) -> AgentChangeProposalError:
    """`agent_change_suggested_case_invalid` naming the failed rule, the offending case, and the expected shape."""

    return AgentChangeProposalError(
        "agent_change_suggested_case_invalid",
        details={"rule": rule, **{key: value for key, value in facts.items() if value is not None}, "message": f"{message} {_CASE_SHAPE}"},
    )


def case_text_problem(case: Mapping[str, Any]) -> tuple[str, str] | None:
    """The first invalid `task` / `failure_mode` field of one suggested case, as (field, reason)."""

    for field, limit in (("task", 8000), ("failure_mode", 2000)):
        value = case.get(field)
        if not isinstance(value, str):
            return field, "must be a string"
        if not value.strip():
            return field, "must not be empty"
        if len(value) > limit:
            return field, f"is {len(value)} characters; the limit is {limit}"
    return None


@dataclass(frozen=True, slots=True)
class AgentProposalBaseline:
    working_copy_id: str
    working_copy_row_version: int
    base_release_id: str | None


@dataclass(frozen=True, slots=True)
class AgentChangeProposal:
    id: str
    team_id: str
    agent_id: str
    source_type: str
    source_ref: str
    base_release_id: str | None
    base_working_copy_id: str | None
    base_working_copy_version: int | None
    base_document: dict[str, Any]
    patch: tuple[dict[str, Any], ...]
    atomic_groups: tuple[dict[str, Any], ...]
    suggested_cases: tuple[dict[str, Any], ...]
    rationale: str
    provenance: dict[str, Any]
    status: str
    row_version: int
    created_by_kind: str
    created_by_id: str
    created_at: str
    decided_at: str | None
    initial_suite_name: str | None = None
    base_suite_revision_id: str | None = None
    case_target_suite_revision_id: str | None = None
    # Where a CI proposal came from (repository, ref, commit, grant, key name); None for other sources.
    origin: dict[str, Any] | None = None


# Proposal reads join the Team OpenAPI key that created a CI proposal, so the Owner sees which key
# sent it. A deleted key yields NULL. Callers qualify their WHERE columns with `proposal.`.
_PROPOSAL_SELECT = """SELECT proposal.*,origin_key.name AS origin_key_name
FROM agent_change_proposal proposal
LEFT JOIN team_openapi_keys origin_key
  ON proposal.source_type='git' AND origin_key.id=proposal.created_by_id
  AND origin_key.team_id=proposal.team_id AND origin_key.status<>'deleted'"""


class AgentChangeProposalStore:
    def __init__(self, db_path: str, *, revision_store: AgentRevisionStore | None = None) -> None:
        self.db_path = db_path
        self._revision_store = revision_store
        self._behavior_authoring = revision_store._behavior_authoring if revision_store is not None else default_agent_behavior_authoring_service()

    async def create(
        self,
        *,
        team_id: str,
        agent_id: str,
        effective_owner_user_id: str,
        source_type: ProposalSource,
        source_ref: str,
        operations: Sequence[Mapping[str, Any]],
        atomic_groups: Sequence[Mapping[str, Any]],
        suggested_cases: Sequence[Mapping[str, Any]] = (),
        rationale: str = "",
        created_by_kind: Literal["user", "automation", "system"] = "user",
        created_by_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        expected_baseline: AgentProposalBaseline | None = None,
        automation: AutomationAuthority | None = None,
        strict_replay: bool = False,
        authorize: Callable[[aiosqlite.Connection], Awaitable[None]] | None = None,
        generation: CoachGenerationClaim | None = None,
        validate_candidate: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AgentChangeProposal:
        patch, groups, cases = validate_proposal_payload(
            operations,
            atomic_groups,
            suggested_cases,
            allow_document_roots=source_type == "regression",
        )
        source_ref = source_ref.strip()
        if not source_ref or len(source_ref) > 240 or len(rationale) > 4_000:
            raise AgentChangeProposalError("agent_change_proposal_invalid")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                proposal, _ = await self._create_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    effective_owner_user_id=effective_owner_user_id,
                    source_type=source_type,
                    source_ref=source_ref,
                    rationale=rationale,
                    created_by_kind=created_by_kind,
                    created_by_id=created_by_id,
                    provenance=provenance,
                    expected_baseline=expected_baseline,
                    automation=automation,
                    strict_replay=strict_replay,
                    authorize=authorize,
                    generation=generation,
                    validate_candidate=validate_candidate,
                    patch=patch,
                    groups=groups,
                    cases=cases,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return proposal

    async def _create_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        effective_owner_user_id: str,
        source_type: ProposalSource,
        source_ref: str,
        rationale: str,
        created_by_kind: Literal["user", "automation", "system"],
        created_by_id: str | None,
        provenance: Mapping[str, Any] | None,
        expected_baseline: AgentProposalBaseline | None,
        automation: AutomationAuthority | None,
        strict_replay: bool,
        authorize: Callable[[aiosqlite.Connection], Awaitable[None]] | None,
        generation: CoachGenerationClaim | None,
        validate_candidate: Callable[[dict[str, Any]], Awaitable[None]] | None,
        patch: tuple[dict[str, Any], ...],
        groups: tuple[dict[str, Any], ...],
        cases: tuple[dict[str, Any], ...],
    ) -> tuple[AgentChangeProposal, bool]:
        timestamp = _now()
        if authorize is not None:
            await authorize(db)
        if generation is not None:
            if (
                not strict_replay
                or source_type != "coach"
                or created_by_kind != "user"
                or (team_id, agent_id, effective_owner_user_id, source_ref, expected_baseline)
                != (generation.team_id, generation.agent_id, generation.owner_user_id, generation.source_ref, generation.baseline)
                or (provenance or {}).get("generation_request_id") != generation.id
            ):
                raise AgentChangeProposalError("agent_coach_request_conflict")
            await generation.require_conn(db)
        if strict_replay:
            if source_type != "coach" or expected_baseline is None or automation is not None:
                raise AgentChangeProposalError("agent_coach_proposal_context_required")
            await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=effective_owner_user_id)
            existing = await _proposal_by_source_conn(db, team_id=team_id, agent_id=agent_id, source_type=source_type, source_ref=source_ref)
            if existing is not None:
                if (
                    existing["patch_json"] != _canonical_json(patch)
                    or existing["atomic_groups_json"] != _canonical_json(groups)
                    or existing["suggested_cases_json"] != _canonical_json(cases)
                    or existing["rationale"] != rationale
                    or existing["provenance_json"] != _canonical_json(dict(provenance or {}))
                    or existing["created_by_kind"] != created_by_kind
                    or existing["created_by_id"] != (created_by_id or effective_owner_user_id)
                    or (existing["base_working_copy_id"], existing["base_working_copy_version"], existing["base_release_id"])
                    != (expected_baseline.working_copy_id, expected_baseline.working_copy_row_version, expected_baseline.base_release_id)
                ):
                    raise AgentChangeProposalError("agent_change_proposal_idempotency_conflict")
                result = await _proposal_with_case_decisions_conn(db, existing)
                if generation is not None:
                    await generation.complete_conn(db, result.id)
                return result, False
        if automation is not None:
            if (
                source_type != "git"
                or created_by_kind != "automation"
                or created_by_id != automation.principal_id
                or effective_owner_user_id != automation.effective_owner_user_id
                or (provenance or {}).get("automation_grant_id") != automation.grant_id
            ):
                raise AgentChangeProposalError("agent_automation_origin_mismatch")
            await automation.require_conn(db, team_id=team_id, agent_id=agent_id, scope="proposal:create")
            existing = await _proposal_by_source_conn(db, team_id=team_id, agent_id=agent_id, source_type=source_type, source_ref=source_ref)
            if existing is not None:
                stored = json.loads(existing["provenance_json"])
                if (
                    existing["created_by_id"] != created_by_id
                    or stored != dict(provenance or {})
                    or existing["patch_json"] != _canonical_json(patch)
                    or existing["atomic_groups_json"] != _canonical_json(groups)
                    or existing["suggested_cases_json"] != _canonical_json(cases)
                    or existing["rationale"] != rationale
                ):
                    raise AgentChangeProposalError("agent_change_proposal_idempotency_conflict")
                return _proposal_from_row(existing), False
        try:
            working, owner_id = await _owned_working_conn(
                db,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=effective_owner_user_id,
            )
        except AgentRevisionForbiddenError:
            if source_type != "regression":
                raise
            working, owner_id = await _create_neutral_working_copy_conn(
                db,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=effective_owner_user_id,
                timestamp=timestamp,
            )
        if expected_baseline is not None and (working["id"], working["row_version"], working["base_release_id"]) != (
            expected_baseline.working_copy_id,
            expected_baseline.working_copy_row_version,
            expected_baseline.base_release_id,
        ):
            raise AgentChangeProposalError("agent_working_copy_version_conflict")
        base = _document_from_working(working)
        result = apply_operations(base, patch)
        from bowhead_core.agent_runtime_extensions import require_extension_policy_preserved

        require_extension_policy_preserved(base["definition"], result["definition"])
        await self._validate_document_conn(db, team_id=team_id, document=result)
        if validate_candidate is not None:
            await validate_candidate(result)
        base_json = _canonical_json(base)
        patch_json = _canonical_json(patch)
        groups_json = _canonical_json(groups)
        cases_json = _canonical_json(cases)
        provenance_json = _canonical_json(dict(provenance or {}))
        proposal_id = f"acp_{uuid4().hex}"
        try:
            await db.execute(
                """
                INSERT INTO agent_change_proposal(
                  id,team_id,agent_id,source_type,source_ref,base_release_id,
                  base_working_copy_id,base_working_copy_version,base_document_json,
                  base_document_hash,patch_json,patch_hash,atomic_groups_json,
                  suggested_cases_json,rationale,provenance_json,status,row_version,
                  created_by_kind,created_by_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open',1,?,?,?)
                """,
                (
                    proposal_id,
                    team_id,
                    agent_id,
                    source_type,
                    source_ref,
                    str(working["base_release_id"] or "") or None,
                    str(working["id"]),
                    int(working["row_version"]),
                    base_json,
                    _hash_json(base_json),
                    patch_json,
                    _hash_json(patch_json),
                    groups_json,
                    cases_json,
                    rationale,
                    provenance_json,
                    created_by_kind,
                    created_by_id or owner_id,
                    timestamp,
                ),
            )
        except aiosqlite.IntegrityError:
            row = await _proposal_by_identity_conn(
                db,
                team_id=team_id,
                agent_id=agent_id,
                source_type=source_type,
                source_ref=source_ref,
                patch_hash=_hash_json(patch_json),
            )
            if row is None:
                if source_type == "git":
                    row = await _proposal_by_source_conn(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        source_type=source_type,
                        source_ref=source_ref,
                    )
                    if row is not None:
                        raise AgentChangeProposalError("agent_change_proposal_idempotency_conflict")
                raise
            return _proposal_from_row(row), False
        row = await _proposal_conn(db, team_id=team_id, agent_id=agent_id, proposal_id=proposal_id)
        assert row is not None
        # Coach proposals answer the Owner's own request; regression proposals already
        # carry their own regression notification.
        if source_type not in {"coach", "regression"} and str(row["created_by_id"]) != owner_id:
            await _enqueue_proposal_notice_conn(
                db,
                team_id=team_id,
                agent_id=agent_id,
                owner_user_id=owner_id,
                proposal_id=proposal_id,
                source_type=source_type,
                group_count=len(groups),
                timestamp=timestamp,
            )
        if generation is not None:
            await generation.complete_conn(db, proposal_id)
        return _proposal_from_row(row), True

    async def preview(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        expected_baseline: AgentProposalBaseline,
        operations: Sequence[Mapping[str, Any]],
        atomic_groups: Sequence[Mapping[str, Any]],
        suggested_cases: Sequence[Mapping[str, Any]] = (),
        authorize: Callable[[aiosqlite.Connection], Awaitable[None]] | None = None,
        include_candidate: bool = False,
    ) -> dict[str, Any]:
        """Validate proposal structure against an exact base, without saving or running it."""
        patch, _, _ = validate_proposal_payload(operations, atomic_groups, suggested_cases)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            if authorize is not None:
                await authorize(db)
            working, _ = await _owned_working_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            if (working["id"], working["row_version"], working["base_release_id"]) != (
                expected_baseline.working_copy_id,
                expected_baseline.working_copy_row_version,
                expected_baseline.base_release_id,
            ):
                raise AgentChangeProposalError("agent_working_copy_version_conflict")
            base = _document_from_working(working)
            result = apply_operations(base, patch)
            from bowhead_core.agent_runtime_extensions import require_extension_policy_preserved

            require_extension_policy_preserved(base["definition"], result["definition"])
            await self._validate_document_conn(db, team_id=team_id, document=result)
            changed = sorted({str(item["path"]) for item in patch if _value_at(base, str(item["path"])) != _value_at(result, str(item["path"]))})
        return {
            "validation_kind": "proposal_structure",
            "changed_paths": changed,
            "persisted": False,
            "executed": False,
            **({"candidate_document": result} if include_candidate else {}),
        }

    async def create_restore_proposal(
        self,
        *,
        team_id: str,
        agent_id: str,
        failed_release_id: str,
        effective_owner_user_id: str,
        regression_run_id: str,
        reason_code: str,
    ) -> AgentChangeProposal | None:
        """Propose restoring the most recent evidenced predecessor; never activate it."""

        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT agent.live_release_id,failed.predecessor_release_id,
                       previous_revision.definition_json,previous_revision.listing_json,
                       evidence.readiness
                FROM team_subagent agent
                JOIN team_subagent_release failed
                  ON failed.team_id=agent.team_id AND failed.subagent_id=agent.id AND failed.id=?
                JOIN team_subagent_release previous
                  ON previous.team_id=failed.team_id AND previous.subagent_id=failed.subagent_id
                 AND previous.id=failed.predecessor_release_id
                JOIN team_subagent_revision previous_revision ON previous_revision.id=previous.revision_id
                LEFT JOIN agent_release_eval_evidence evidence ON evidence.release_id=previous.id
                WHERE agent.team_id=? AND agent.id=? AND agent.lifecycle_state='active'
                  AND COALESCE(agent.owner_user_id,agent.created_by)=?
                """,
                (failed_release_id, team_id, agent_id, effective_owner_user_id),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None or str(row["live_release_id"] or "") != failed_release_id:
            return None
        if str(row["readiness"] or "") not in {"ready", "ready_with_risks"}:
            return None
        previous_release_id = str(row["predecessor_release_id"])
        return await self.create(
            team_id=team_id,
            agent_id=agent_id,
            effective_owner_user_id=effective_owner_user_id,
            source_type="regression",
            source_ref=f"regression:{regression_run_id}",
            operations=(
                {"op": "replace", "path": "/definition", "value": json.loads(str(row["definition_json"]))},
                {"op": "replace", "path": "/listing", "value": json.loads(str(row["listing_json"]))},
            ),
            atomic_groups=({"id": "restore_release", "operation_indexes": [0, 1], "depends_on": []},),
            rationale="检测到已发布版本回归；建议恢复到最近一个有完整版本证据的发布。",
            created_by_kind="system",
            created_by_id="system:agent-regression",
            provenance={
                "failed_release_id": failed_release_id,
                "previous_release_id": previous_release_id,
                "reason_code": reason_code,
                "regression_run_id": regression_run_id,
            },
        )

    async def list(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        status: str | None = None,
    ) -> tuple[AgentChangeProposal, ...]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            query = _PROPOSAL_SELECT + " WHERE proposal.team_id=? AND proposal.agent_id=?"
            params: list[Any] = [team_id, agent_id]
            if status is not None:
                query += " AND proposal.status=?"
                params.append(status)
            query += " ORDER BY proposal.created_at DESC,proposal.id DESC"
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
            proposals = tuple([await _proposal_with_case_decisions_conn(db, row) for row in rows])
        return proposals

    async def get(self, *, team_id: str, agent_id: str, proposal_id: str, actor_user_id: str) -> AgentChangeProposal:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            row = await _proposal_conn(db, team_id=team_id, agent_id=agent_id, proposal_id=proposal_id)
        if row is None:
            raise AgentChangeProposalError("agent_change_proposal_not_found")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await _proposal_with_case_decisions_conn(db, row)

    async def apply(
        self,
        *,
        team_id: str,
        agent_id: str,
        proposal_id: str,
        actor_user_id: str,
        selected_group_ids: Sequence[str],
        expected_proposal_row_version: int,
        expected_working_copy_row_version: int,
    ) -> dict[str, Any]:
        selected = tuple(dict.fromkeys(selected_group_ids))
        if not selected:
            raise AgentChangeProposalError("agent_change_groups_required")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                result = await self._apply_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    proposal_id=proposal_id,
                    actor_user_id=actor_user_id,
                    selected=selected,
                    expected_proposal_row_version=expected_proposal_row_version,
                    expected_working_copy_row_version=expected_working_copy_row_version,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return result

    async def _apply_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        proposal_id: str,
        actor_user_id: str,
        selected: tuple[str, ...],
        expected_proposal_row_version: int,
        expected_working_copy_row_version: int,
    ) -> dict[str, Any]:
        timestamp = _now()
        working, _ = await _owned_working_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
        if int(working["row_version"]) != expected_working_copy_row_version:
            raise AgentRevisionConflictError("agent_working_copy_version_conflict")
        row = await _proposal_conn(db, team_id=team_id, agent_id=agent_id, proposal_id=proposal_id)
        if row is None:
            raise AgentChangeProposalError("agent_change_proposal_not_found")
        proposal = _proposal_from_row(row)
        if proposal.status != "open" or proposal.row_version != expected_proposal_row_version:
            raise AgentChangeProposalError("agent_change_proposal_stale")
        operation_indexes = _selected_operation_indexes(proposal.atomic_groups, selected)
        selected_operations = tuple(proposal.patch[index] for index in operation_indexes)
        ours = _document_from_working(working)
        conflicts = _merge_conflicts(
            proposal.base_document,
            ours,
            proposal.patch,
            proposal.atomic_groups,
            operation_indexes,
        )
        if conflicts:
            raise AgentChangeProposalError("agent_change_merge_conflict", conflicts=conflicts)
        merged = apply_operations(ours, selected_operations)
        from bowhead_core.agent_runtime_extensions import require_extension_policy_preserved

        require_extension_policy_preserved(ours["definition"], merged["definition"])
        await self._validate_document_conn(db, team_id=team_id, document=merged)
        await self._write_working_conn(db, working=working, document=merged, actor_user_id=actor_user_id, timestamp=timestamp)
        changed = await db.execute(
            """
            UPDATE agent_change_proposal
            SET status='accepted',row_version=row_version+1,decided_at=?
            WHERE id=? AND team_id=? AND agent_id=? AND status='open' AND row_version=?
            """,
            (timestamp, proposal_id, team_id, agent_id, expected_proposal_row_version),
        )
        if changed.rowcount != 1:
            raise AgentChangeProposalError("agent_change_proposal_stale")
        decision_id, result_copy, result_hash = await _record_patch_decision_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            proposal_id=proposal_id,
            decision_kind="patch_accept",
            selected=selected,
            actor_user_id=actor_user_id,
            timestamp=timestamp,
        )
        return {
            "proposal_id": proposal_id,
            "status": "accepted",
            "decision_id": decision_id,
            "working_copy": result_copy,
            "working_copy_hash": result_hash,
        }

    async def _write_working_conn(
        self,
        db: aiosqlite.Connection,
        *,
        working: aiosqlite.Row,
        document: Mapping[str, Any],
        actor_user_id: str,
        timestamp: str,
    ) -> None:
        definition_json, _ = _canonical_document(document["definition"], name="definition")
        listing_json, _ = _canonical_document(document["listing"], name="listing")
        audience = AgentAudience.from_mapping(document["audience"])
        audience_json = _canonical_json(audience.to_dict())
        validation_json = _canonical_json(self._behavior_authoring.validate_definition_intent(document["definition"]).working_copy_validation())
        next_version = int(working["row_version"]) + 1
        updated = await db.execute(
            """
            UPDATE team_subagent_working_copy
            SET definition_json=?,listing_json=?,audience_json=?,validation_json=?,
                row_version=?,updated_by=?,updated_at=?
            WHERE id=? AND row_version=?
            """,
            (
                definition_json,
                listing_json,
                audience_json,
                validation_json,
                next_version,
                actor_user_id,
                timestamp,
                str(working["id"]),
                int(working["row_version"]),
            ),
        )
        if updated.rowcount != 1:
            raise AgentRevisionConflictError("agent_working_copy_version_conflict")
        await invalidate_working_copy_projection_conn(db, team_id=str(working["team_id"]), working_copy_id=str(working["id"]))

    async def create_applied(
        self,
        *,
        team_id: str,
        agent_id: str,
        effective_owner_user_id: str,
        source_ref: str,
        operations: Sequence[Mapping[str, Any]],
        atomic_groups: Sequence[Mapping[str, Any]],
        expected_baseline: AgentProposalBaseline,
        suggested_cases: Sequence[Mapping[str, Any]] = (),
        rationale: str = "",
        provenance: Mapping[str, Any] | None = None,
        authorize: Callable[[aiosqlite.Connection], Awaitable[None]] | None = None,
        validate_candidate: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> tuple[AgentChangeProposal, int, bool]:
        """Create a Coach proposal and write all of its groups into the draft atomically.

        Returns the proposal, the working-copy version holding its content and whether this
        call wrote it. A replay of the same Coach request returns the original proposal
        without writing again.
        """
        patch, groups, cases = validate_proposal_payload(operations, atomic_groups, suggested_cases)
        source_ref = source_ref.strip()
        if not source_ref or len(source_ref) > 240 or len(rationale) > 4_000:
            raise AgentChangeProposalError("agent_change_proposal_invalid")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                proposal, _ = await self._create_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    effective_owner_user_id=effective_owner_user_id,
                    source_type="coach",
                    source_ref=source_ref,
                    rationale=rationale,
                    created_by_kind="user",
                    created_by_id=None,
                    provenance=provenance,
                    expected_baseline=expected_baseline,
                    automation=None,
                    strict_replay=True,
                    authorize=authorize,
                    generation=None,
                    validate_candidate=validate_candidate,
                    patch=patch,
                    groups=groups,
                    cases=cases,
                )
                written = proposal.status == "open"
                if written:
                    applied = await self._apply_conn(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        proposal_id=proposal.id,
                        actor_user_id=effective_owner_user_id,
                        selected=tuple(str(group["id"]) for group in proposal.atomic_groups),
                        expected_proposal_row_version=proposal.row_version,
                        expected_working_copy_row_version=expected_baseline.working_copy_row_version,
                    )
                    version = int(applied["working_copy"].row_version)
                    row = await _proposal_conn(db, team_id=team_id, agent_id=agent_id, proposal_id=proposal.id)
                    assert row is not None
                    proposal = await _proposal_with_case_decisions_conn(db, row)
                else:
                    version = await _accepted_working_copy_version_conn(db, proposal_id=proposal.id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return proposal, version, written

    async def draft_changes(self, *, team_id: str, agent_id: str, actor_user_id: str) -> tuple[dict[str, Any], ...]:
        """Each draft field that differs from the live release, or that Coach changed, with who changed it."""
        from bowhead_core.agent_release_read_store import _changed_keys

        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            try:
                working, _ = await _owned_working_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            except AgentRevisionForbiddenError:
                return ()
            live = await _live_document_conn(db, team_id=team_id, agent_id=agent_id)
            history = await _coach_field_history_conn(db, team_id=team_id, agent_id=agent_id, working=working)
            writes = await _draft_field_writes_conn(db, team_id=team_id, agent_id=agent_id, working=working)
        current = _document_from_working(working)
        live = live or {"definition": {}, "listing": {}, "audience": {}}
        paths = {f"/definition/{key}" for key in set(live["definition"]) | set(current["definition"])}
        paths |= {f"/listing/{key}" for key in _changed_keys(live["listing"], current["listing"])}
        paths |= {f"/audience/{key}" for key in (set(live["audience"]) | set(current["audience"])) - {"schema_version"}}
        changed = {path for path in paths if not _same_field_value(_value_at(live, path), _value_at(current, path))} | set(history)
        rows = []
        for path in sorted(changed):
            value = _value_at(current, path)
            source = next((kind for kind, written in reversed(writes.get(path, ())) if written == value), "manual")
            row: dict[str, Any] = {"path": path, "live": _draft_value(_value_at(live, path)), "draft": _draft_value(value), "source": source}
            entries = history.get(path, ())
            applied = _undo_chain(entries)
            chain = applied or _reapply_chain(entries)
            if chain:
                expected = chain[-1].coach if applied else chain[0].before
                row["coach"] = {
                    "state": "applied" if applied else "undone",
                    "proposal_id": chain[-1].proposal_id,
                    "group_ids": list(chain[-1].group_ids),
                    "before": _draft_value(chain[0].before),
                    "value": _draft_value(chain[-1].coach),
                    "conflict": value != expected,
                }
            rows.append(row)
        return tuple(rows)

    async def undo_fields(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        fields: Sequence[str],
        expected_working_copy_row_version: int,
    ) -> dict[str, Any]:
        """Restore each field to its value before Coach's consecutive writes in this draft."""
        return await self._revert_fields(
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            fields=fields,
            expected_working_copy_row_version=expected_working_copy_row_version,
            decision_kind="patch_undo",
        )

    async def reapply_fields(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        fields: Sequence[str],
        expected_working_copy_row_version: int,
    ) -> dict[str, Any]:
        """Write Coach's value back into fields that were undone and not changed since."""
        return await self._revert_fields(
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            fields=fields,
            expected_working_copy_row_version=expected_working_copy_row_version,
            decision_kind="patch_reapply",
        )

    async def keep_fields(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        fields: Sequence[str],
        expected_working_copy_row_version: int,
        restore: bool = False,
    ) -> dict[str, Any]:
        """Resolve undo conflicts in the Owner's favor and take the fields out of Coach's undo history.

        Without `restore` the Owner's current values stay and the draft is unchanged. With `restore` the
        fields go back to their value before Coach in the same transaction: an ordinary edit that replaces
        the Owner's later change, which the Owner confirmed.
        """
        selected = tuple(dict.fromkeys(fields))
        if not selected or len(selected) > 64:
            raise AgentChangeProposalError("agent_coach_change_fields_required")
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                working, _ = await _owned_working_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
                if int(working["row_version"]) != expected_working_copy_row_version:
                    raise AgentRevisionConflictError("agent_working_copy_version_conflict")
                history = await _coach_field_history_conn(db, team_id=team_id, agent_id=agent_id, working=working)
                current = _document_from_working(working)
                document = copy.deepcopy(current)
                touched: dict[str, tuple[set[str], list[str]]] = {}
                batch_id = f"acpb_{uuid4().hex}"
                for field in selected:
                    chain = _undo_chain(history.get(field, ()))
                    if not chain:
                        raise AgentChangeProposalError("agent_coach_change_not_found")
                    if _value_at(current, field) == chain[-1].coach:
                        raise AgentChangeProposalError("agent_coach_change_not_in_conflict")
                    if restore:
                        _set_value(document, field, chain[0].before)
                    for entry in chain:
                        groups, paths = touched.setdefault(entry.proposal_id, (set(), []))
                        groups.update(entry.group_ids)
                        paths.append(field)
                if restore:
                    from bowhead_core.agent_runtime_extensions import require_extension_policy_preserved

                    require_extension_policy_preserved(current["definition"], document["definition"])
                    await self._validate_document_conn(db, team_id=team_id, document=document)
                    await self._write_working_conn(db, working=working, document=document, actor_user_id=actor_user_id, timestamp=timestamp)
                    async with db.execute(
                        "SELECT * FROM team_subagent_working_copy WHERE team_id=? AND subagent_id=?", (team_id, agent_id)
                    ) as cursor:
                        refreshed = await cursor.fetchone()
                    assert refreshed is not None
                    working = refreshed
                decision_ids = []
                for proposal_id, (groups, paths) in touched.items():
                    decision_id = f"acpd_{uuid4().hex}"
                    await db.execute(
                        """INSERT INTO agent_change_proposal_decision(
                        id,proposal_id,decision_kind,selected_group_ids_json,selected_case_ids_json,
                        actor_user_id,created_at,decision_reason_json) VALUES(?,?,'patch_dismiss',?,'[]',?,?,?)""",
                        (
                            decision_id,
                            proposal_id,
                            _canonical_json(sorted(groups)),
                            actor_user_id,
                            timestamp,
                            _canonical_json({"fields": paths, "batch_id": batch_id, **({"restored": True} if restore else {})}),
                        ),
                    )
                    decision_ids.append(decision_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {"fields": list(selected), "decision_ids": decision_ids, "working_copy": _working_copy_from_row(working)}

    async def _revert_fields(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        fields: Sequence[str],
        expected_working_copy_row_version: int,
        decision_kind: Literal["patch_undo", "patch_reapply"],
    ) -> dict[str, Any]:
        selected = tuple(dict.fromkeys(fields))
        if not selected or len(selected) > 64:
            raise AgentChangeProposalError("agent_coach_change_fields_required")
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                working, _ = await _owned_working_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
                if int(working["row_version"]) != expected_working_copy_row_version:
                    raise AgentRevisionConflictError("agent_working_copy_version_conflict")
                history = await _coach_field_history_conn(db, team_id=team_id, agent_id=agent_id, working=working)
                current = _document_from_working(working)
                document = copy.deepcopy(current)
                conflicts: list[dict[str, Any]] = []
                touched: dict[str, tuple[set[str], list[str]]] = {}
                batch_id = f"acpb_{uuid4().hex}"
                for field in selected:
                    entries = history.get(field, ())
                    chain = _undo_chain(entries) if decision_kind == "patch_undo" else _reapply_chain(entries)
                    if not chain:
                        raise AgentChangeProposalError("agent_coach_change_not_found")
                    before, coach = chain[0].before, chain[-1].coach
                    expected, target = (coach, before) if decision_kind == "patch_undo" else (before, coach)
                    observed = _value_at(current, field)
                    if observed != expected:
                        conflicts.append(
                            {
                                "path": field,
                                "before": _draft_value(before),
                                "coach": _draft_value(coach),
                                "current": _draft_value(observed),
                            }
                        )
                        continue
                    _set_value(document, field, target)
                    for entry in chain:
                        groups, paths = touched.setdefault(entry.proposal_id, (set(), []))
                        groups.update(entry.group_ids)
                        paths.append(field)
                if conflicts:
                    raise AgentChangeProposalError("agent_change_undo_conflict", conflicts=tuple(conflicts))
                from bowhead_core.agent_runtime_extensions import require_extension_policy_preserved

                require_extension_policy_preserved(current["definition"], document["definition"])
                await self._validate_document_conn(db, team_id=team_id, document=document)
                await self._write_working_conn(db, working=working, document=document, actor_user_id=actor_user_id, timestamp=timestamp)
                decision_ids = []
                result_copy: Any = None
                for proposal_id, (groups, paths) in touched.items():
                    decision_id, result_copy, _ = await _record_patch_decision_conn(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        proposal_id=proposal_id,
                        decision_kind=decision_kind,
                        selected=sorted(groups),
                        actor_user_id=actor_user_id,
                        timestamp=timestamp,
                        reason={"fields": paths, "batch_id": batch_id},
                    )
                    decision_ids.append(decision_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {"fields": list(selected), "decision_ids": decision_ids, "working_copy": result_copy}

    async def accept_suggested_cases(
        self,
        *,
        team_id: str,
        agent_id: str,
        proposal_id: str,
        actor_user_id: str,
        expected_proposal_row_version: int,
        expected_suite_revision_id: str | None,
        cases: Sequence[tuple[str, EvalCaseDraft]],
    ) -> dict[str, Any]:
        if not cases:
            raise suggested_case_invalid("no_cases_selected", "Select at least one suggested case to accept.")
        selected_ids = [item[0] for item in cases]
        if len(set(selected_ids)) != len(selected_ids):
            duplicates = sorted({item for item in selected_ids if selected_ids.count(item) > 1})
            raise suggested_case_invalid(
                "duplicate_case_id", f"Suggested cases {duplicates[:16]} are selected more than once.", case_ids=duplicates[:16]
            )
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
                row = await _proposal_conn(db, team_id=team_id, agent_id=agent_id, proposal_id=proposal_id)
                if row is None:
                    raise AgentChangeProposalError("agent_change_proposal_not_found")
                proposal = _proposal_from_row(row)
                if proposal.row_version != expected_proposal_row_version:
                    raise AgentChangeProposalError("agent_change_proposal_stale")
                known = {str(item["id"]) for item in proposal.suggested_cases}
                selected = tuple(item[0] for item in cases)
                decided = await _decided_case_ids_conn(db, proposal_id=proposal_id)
                if any(case_id not in known or case_id in decided for case_id in selected):
                    raise AgentChangeProposalError("agent_change_suggested_case_conflict")
                if (proposal.base_suite_revision_id is not None or proposal.initial_suite_name is not None) and await _case_target_suite_conn(
                    db, proposal
                ) != expected_suite_revision_id:
                    raise AgentChangeProposalError("agent_eval_suite_version_conflict")
                if expected_suite_revision_id is None and proposal.initial_suite_name is None:
                    raise AgentChangeProposalError("agent_eval_suite_version_required")
                suggestions = {str(item["id"]): item for item in proposal.suggested_cases}
                replacements = {}
                for identity, candidate in cases:
                    edit = suggestions[identity].get("edit")
                    if edit is not None:
                        if candidate.case_key != edit["case_key"]:
                            raise suggested_case_invalid("edit_identity", "An edited case must retain its original identity.", case_id=identity)
                        replacements[candidate.case_key] = edit["case_hash"]
                revision = await append_eval_suite_cases_conn(
                    db,
                    initial_suite_name=proposal.initial_suite_name if expected_suite_revision_id is None else None,
                    team_id=team_id,
                    agent_id=agent_id,
                    expected_suite_revision_id=expected_suite_revision_id,
                    cases=tuple(item[1] for item in cases),
                    actor_user_id=actor_user_id,
                    replacements=replacements,
                )
                decision_id = f"acpd_{uuid4().hex}"
                await db.execute(
                    """
                    INSERT INTO agent_change_proposal_decision(
                      id,proposal_id,decision_kind,selected_group_ids_json,selected_case_ids_json,
                      result_suite_revision_id,actor_user_id,created_at
                    ) VALUES(?,?,'cases_accept','[]',?,?,?,?)
                    """,
                    (decision_id, proposal_id, _canonical_json(selected), revision.suite_revision_id, actor_user_id, timestamp),
                )
                await _settle_case_only_proposal_conn(db, proposal, timestamp)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "decision_id": decision_id,
            "proposal_id": proposal_id,
            "suite_revision_id": revision.suite_revision_id,
        }

    async def dismiss_suggested_cases(
        self,
        *,
        team_id: str,
        agent_id: str,
        proposal_id: str,
        actor_user_id: str,
        expected_proposal_row_version: int,
        suggested_case_ids: Sequence[str],
        reason: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected = tuple(dict.fromkeys(suggested_case_ids))
        if not selected:
            raise suggested_case_invalid("no_cases_selected", "Select at least one suggested case.")
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
                row = await _proposal_conn(db, team_id=team_id, agent_id=agent_id, proposal_id=proposal_id)
                if row is None:
                    raise AgentChangeProposalError("agent_change_proposal_not_found")
                proposal = _proposal_from_row(row)
                if proposal.row_version != expected_proposal_row_version:
                    raise AgentChangeProposalError("agent_change_proposal_stale")
                known = {str(item["id"]) for item in proposal.suggested_cases}
                decided = await _decided_case_ids_conn(db, proposal_id=proposal_id)
                if any(case_id not in known or case_id in decided for case_id in selected):
                    raise AgentChangeProposalError("agent_change_suggested_case_conflict")
                decision_id = f"acpd_{uuid4().hex}"
                await db.execute(
                    """
                    INSERT INTO agent_change_proposal_decision(
                      id,proposal_id,decision_kind,selected_group_ids_json,selected_case_ids_json,decision_reason_json,
                      actor_user_id,created_at
                    ) VALUES(?,?,'cases_dismiss','[]',?,?,?,?)
                    """,
                    (decision_id, proposal_id, _canonical_json(selected), _canonical_json(dict(reason or {})), actor_user_id, timestamp),
                )
                await _settle_case_only_proposal_conn(db, proposal, timestamp)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {"decision_id": decision_id, "proposal_id": proposal_id}

    async def dismiss(
        self,
        *,
        team_id: str,
        agent_id: str,
        proposal_id: str,
        actor_user_id: str,
        expected_proposal_row_version: int,
    ) -> AgentChangeProposal:
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
                changed = await db.execute(
                    """UPDATE agent_change_proposal SET status='dismissed',row_version=row_version+1,decided_at=?
                    WHERE id=? AND team_id=? AND agent_id=? AND status='open' AND row_version=? AND base_suite_revision_id IS NULL AND initial_suite_name IS NULL""",
                    (timestamp, proposal_id, team_id, agent_id, expected_proposal_row_version),
                )
                if changed.rowcount != 1:
                    raise AgentChangeProposalError("agent_change_proposal_stale")
                await db.execute(
                    """INSERT INTO agent_change_proposal_decision(
                    id,proposal_id,decision_kind,selected_group_ids_json,selected_case_ids_json,
                    actor_user_id,created_at) VALUES(?,?,'patch_dismiss','[]','[]',?,?)""",
                    (f"acpd_{uuid4().hex}", proposal_id, actor_user_id, timestamp),
                )
                row = await _proposal_conn(db, team_id=team_id, agent_id=agent_id, proposal_id=proposal_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        assert row is not None
        return _proposal_from_row(row)

    async def _validate_document_conn(self, db: aiosqlite.Connection, *, team_id: str, document: Mapping[str, Any]) -> None:
        if set(document) != {"definition", "listing", "audience"}:
            raise AgentChangeProposalError("agent_change_proposal_invalid_document")
        _, definition = _canonical_document(document["definition"], name="definition")
        _, listing = _canonical_document(document["listing"], name="listing")
        _names_from_listing(listing)
        audience = AgentAudience.from_mapping(document["audience"])
        self._behavior_authoring.validate_definition_intent(definition)
        _validate_builtin_plugin_selection(definition)
        from bowhead_core.agent_runtime_extension_store import require_extension_policy_write

        await require_extension_policy_write(db, definition, audience_mode=audience.mode)
        await _validate_audience_conn(db, team_id=team_id, audience=audience)


def _validate_builtin_plugin_selection(definition: Mapping[str, Any]) -> None:
    """Refuse proposals with built-in Plugin selections that publication would refuse."""
    if not definition.get("builtin_plugin_ids"):
        return
    from bowhead_core.system_plugins import BuiltinPluginSelectionError, selected_builtin_plugin_ids

    try:
        selected_builtin_plugin_ids(definition)
    except BuiltinPluginSelectionError as exc:
        raise AgentChangeProposalError(exc.code) from exc


def validate_proposal_payload(
    operations: Sequence[Mapping[str, Any]],
    atomic_groups: Sequence[Mapping[str, Any]],
    suggested_cases: Sequence[Mapping[str, Any]],
    *,
    allow_document_roots: bool = False,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    patch = tuple(dict(item) for item in operations)
    groups = tuple(dict(item) for item in atomic_groups)
    cases = tuple(dict(item) for item in suggested_cases)
    if not patch or len(_canonical_json(patch).encode()) > _MAX_PATCH_BYTES:
        raise AgentChangeProposalError("agent_change_proposal_invalid_patch")
    if len(_canonical_json(groups).encode()) > _MAX_GROUP_BYTES or len(_canonical_json(cases).encode()) > _MAX_CASE_BYTES:
        raise AgentChangeProposalError("agent_change_proposal_too_large")
    for operation in patch:
        if operation.get("op") not in {"add", "replace", "remove"} or not _path_allowed(
            operation.get("path"),
            allow_document_roots=allow_document_roots,
        ):
            raise AgentChangeProposalError("agent_change_proposal_path_forbidden")
        if operation["op"] != "remove" and "value" not in operation:
            raise AgentChangeProposalError("agent_change_proposal_invalid_patch")
        if operation["op"] == "remove" and "value" in operation:
            raise AgentChangeProposalError("agent_change_proposal_invalid_patch")
    group_ids: set[str] = set()
    covered: list[int] = []
    dependencies: dict[str, tuple[str, ...]] = {}
    for group_index, group in enumerate(groups):
        group_id = group.get("id")
        indexes = group.get("operation_indexes")
        depends_on = group.get("depends_on", [])
        if not isinstance(group_id, str) or not group_id:
            raise _groups_invalid("group_id_required", f"Group {group_index} has no string id.", group_index=group_index)
        if group_id in group_ids:
            raise _groups_invalid(
                "group_id_duplicate",
                f"Group {group_index} repeats the id {group_id!r}; give each group its own id.",
                group_index=group_index,
                group_id=group_id,
            )
        if not isinstance(indexes, list) or not indexes or any(not isinstance(i, int) or i < 0 or i >= len(patch) for i in indexes):
            raise _groups_invalid(
                "operation_indexes_invalid",
                f"Group {group_id!r} needs a non-empty operation_indexes list of integers from 0 to {len(patch) - 1}"
                f" ({len(patch)} operations were sent).",
                group_index=group_index,
                group_id=group_id,
                operation_count=len(patch),
                invalid_indexes=[i for i in indexes if not isinstance(i, int) or i < 0 or i >= len(patch)][:16]
                if isinstance(indexes, list)
                else None,
            )
        if not isinstance(depends_on, list) or any(not isinstance(item, str) for item in depends_on):
            raise _groups_invalid(
                "depends_on_invalid",
                f"Group {group_id!r} has a depends_on that is not a list of group ids.",
                group_index=group_index,
                group_id=group_id,
            )
        group_ids.add(group_id)
        covered.extend(indexes)
        dependencies[group_id] = tuple(depends_on)
    missing = sorted(set(range(len(patch))) - set(covered))
    if missing:
        raise _groups_invalid(
            "operation_not_grouped",
            f"Operations {missing[:16]} belong to no group; every operation must be in exactly one group.",
            operation_indexes=missing[:16],
        )
    repeated = sorted({index for index in covered if covered.count(index) > 1})
    if repeated:
        raise _groups_invalid(
            "operation_in_several_groups",
            f"Operations {repeated[:16]} are listed in more than one group; every operation must be in exactly one group.",
            operation_indexes=repeated[:16],
        )
    for group_id, deps in dependencies.items():
        for dep in deps:
            if dep == group_id:
                raise _groups_invalid("depends_on_self", f"Group {group_id!r} depends on itself.", group_id=group_id)
            if dep not in group_ids:
                raise _groups_invalid(
                    "depends_on_unknown_group",
                    f"Group {group_id!r} depends on {dep!r}, which is not a group in this change.",
                    group_id=group_id,
                    depends_on=dep,
                )
    _require_acyclic(dependencies)
    case_ids: set[str] = set()
    case_keys: set[str] = set()
    for index, case in enumerate(cases):
        covers = case.get("covers_group_ids")
        label = case.get("id") if isinstance(case.get("id"), str) else None
        problem = case_text_problem(case)
        if problem is not None:
            raise suggested_case_invalid(
                f"{problem[0]}_invalid",
                f"Suggested case {index} `{problem[0]}` {problem[1]}.",
                case_index=index,
                case_id=label,
                field=problem[0],
            )
        if not isinstance(covers, list) or not covers:
            raise suggested_case_invalid(
                "covers_group_ids_required",
                f"Suggested case {index} must list the atomic group ids it covers in `covers_group_ids`.",
                case_index=index,
                case_id=label,
                field="covers_group_ids",
            )
        unknown = [item for item in covers if item not in group_ids]
        if unknown:
            raise suggested_case_invalid(
                "covers_unknown_group",
                f"Suggested case {index} covers {unknown[:16]!r}, which are not group ids in this change.",
                case_index=index,
                case_id=label,
                field="covers_group_ids",
                unknown_group_ids=[str(item) for item in unknown[:16]],
            )
        case.setdefault("id", f"case_{index + 1}")
        identity = case["id"]
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 120:
            raise suggested_case_invalid(
                "id_invalid",
                f"Suggested case {index} `id` must be a non-empty string of at most 120 characters.",
                case_index=index,
                field="id",
            )
        if identity in case_ids:
            raise suggested_case_invalid(
                "duplicate_case_id",
                f"Suggested case id {identity!r} is used more than once.",
                case_index=index,
                case_id=identity,
                field="id",
            )
        case_ids.add(identity)
        if "case_spec" in case:
            try:
                draft = EvalCaseDraft(input={"task": case["task"]}, **case["case_spec"])
            except (TypeError, ValueError) as exc:
                raise suggested_case_invalid(
                    "case_spec_invalid",
                    f"Suggested case {identity!r} `case_spec` is invalid: {str(exc)[:300]}",
                    case_index=index,
                    case_id=identity,
                    field="case_spec",
                ) from exc
            if "held_out" in draft.expected_contract:
                # Only the platform (alignment loop) or the Owner marks a case held out (A6).
                raise suggested_case_invalid(
                    "held_out_not_allowed",
                    f"Suggested case {identity!r} sets `expected_contract.held_out`; only the platform or the Owner marks a case held out.",
                    case_index=index,
                    case_id=identity,
                    field="case_spec.expected_contract.held_out",
                )
            if draft.case_key in case_keys:
                raise AgentChangeProposalError("agent_eval_case_conflict")
            case_keys.add(draft.case_key)
    return patch, groups, cases


def apply_operations(document: Mapping[str, Any], operations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result = copy.deepcopy(dict(document))
    for operation in operations:
        parent, key = _resolve_parent(result, str(operation["path"]), create=operation["op"] == "add")
        if operation["op"] == "remove":
            if key not in parent:
                raise AgentChangeProposalError("agent_change_proposal_invalid_patch")
            del parent[key]
        else:
            if operation["op"] == "replace" and key not in parent:
                raise AgentChangeProposalError("agent_change_proposal_invalid_patch")
            parent[key] = copy.deepcopy(operation["value"])
    return result


def _merge_conflicts(
    base: Mapping[str, Any],
    ours: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]],
    selected_indexes: Sequence[int],
) -> tuple[dict[str, Any], ...]:
    group_by_index = {index: str(group["id"]) for group in groups for index in group["operation_indexes"]}
    conflicts: list[dict[str, Any]] = []
    for original_index in selected_indexes:
        operation = operations[original_index]
        path = str(operation["path"])
        base_value = _value_at(base, path)
        ours_value = _value_at(ours, path)
        proposed_value = None if operation["op"] == "remove" else operation.get("value")
        if ours_value != base_value and ours_value != proposed_value:
            conflicts.append(
                {
                    "group_id": group_by_index.get(original_index, "unknown"),
                    "field_path": path,
                    "field_label": path.rsplit("/", 1)[-1].replace("_", " "),
                    "base_diff": _safe_value(base_value),
                    "current_draft_diff": _safe_value(ours_value),
                    "proposed_diff": _safe_value(proposed_value),
                    "conflict_reason": "both_changed",
                    "allowed_resolutions": ["keep_current", "use_proposed", "regenerate"],
                }
            )
    return tuple(conflicts)


async def _record_patch_decision_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    proposal_id: str,
    decision_kind: Literal["patch_accept", "patch_undo", "patch_reapply"],
    selected: Sequence[str],
    actor_user_id: str,
    timestamp: str,
    reason: Mapping[str, Any] | None = None,
) -> tuple[str, Any, str]:
    async with db.execute(
        "SELECT * FROM team_subagent_working_copy WHERE team_id=? AND subagent_id=?",
        (team_id, agent_id),
    ) as cursor:
        refreshed = await cursor.fetchone()
    assert refreshed is not None
    result_copy = _working_copy_from_row(refreshed)
    result_hash = _working_copy_hash(
        working=refreshed,
        definition=result_copy.definition,
        listing=result_copy.listing,
        audience=result_copy.audience,
    )
    decision_id = f"acpd_{uuid4().hex}"
    await db.execute(
        """
        INSERT INTO agent_change_proposal_decision(
          id,proposal_id,decision_kind,selected_group_ids_json,selected_case_ids_json,
          result_working_copy_id,result_working_copy_version,result_working_copy_hash,
          result_suite_revision_id,actor_user_id,created_at,result_content_hash,decision_reason_json
        ) VALUES(?,?,?,?,'[]',?,?,?,?,?,?,?,?)
        """,
        (
            decision_id,
            proposal_id,
            decision_kind,
            _canonical_json(list(selected)),
            result_copy.id,
            result_copy.row_version,
            result_hash,
            None,
            actor_user_id,
            timestamp,
            working_copy_content_hash(refreshed),
            _canonical_json(dict(reason or {})),
        ),
    )
    return decision_id, result_copy, result_hash


@dataclass(frozen=True, slots=True)
class _CoachFieldWrite:
    proposal_id: str
    group_ids: tuple[str, ...]
    before: Any
    coach: Any
    state: Literal["applied", "undone"]
    undo_batch_id: str | None


def _field_of(path: str) -> str:
    parts = _pointer_parts(path)[:2]
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


async def _coach_field_history_conn(
    db: aiosqlite.Connection, *, team_id: str, agent_id: str, working: aiosqlite.Row
) -> dict[str, tuple[_CoachFieldWrite, ...]]:
    """Coach writes per field in this draft, oldest first, with their latest undo state."""
    async with db.execute(
        """SELECT * FROM agent_change_proposal
        WHERE team_id=? AND agent_id=? AND source_type='coach' AND status='accepted'
          AND base_working_copy_id=? AND base_release_id IS ?
        ORDER BY created_at,id""",
        (team_id, agent_id, str(working["id"]), working["base_release_id"]),
    ) as cursor:
        rows = await cursor.fetchall()
    history: dict[str, list[_CoachFieldWrite]] = {}
    for row in rows:
        proposal = _proposal_from_row(row)
        async with db.execute(
            """SELECT decision_kind,selected_group_ids_json,decision_reason_json FROM agent_change_proposal_decision
            WHERE proposal_id=? AND decision_kind IN ('patch_accept','patch_undo','patch_reapply','patch_dismiss')
            ORDER BY created_at,id""",
            (proposal.id,),
        ) as cursor:
            decisions = await cursor.fetchall()
        accepted_groups: set[str] = set()
        states: dict[str, tuple[Literal["applied", "undone", "kept"], str | None]] = {}
        for decision in decisions:
            kind = str(decision["decision_kind"])
            if kind == "patch_accept":
                accepted_groups.update(json.loads(str(decision["selected_group_ids_json"])))
                continue
            reason = json.loads(str(decision["decision_reason_json"]))
            for field in reason.get("fields", []):
                if kind == "patch_dismiss":
                    # The Owner kept their own value over this write; it leaves undo history for good.
                    states[str(field)] = ("kept", None)
                elif kind == "patch_undo":
                    states[str(field)] = ("undone", str(reason.get("batch_id")))
                else:
                    states[str(field)] = ("applied", None)
        by_field: dict[str, set[str]] = {}
        for group in proposal.atomic_groups:
            if str(group["id"]) not in accepted_groups:
                continue
            for index in group["operation_indexes"]:
                by_field.setdefault(_field_of(str(proposal.patch[index]["path"])), set()).add(str(group["id"]))
        selected = tuple(index for group in proposal.atomic_groups if str(group["id"]) in accepted_groups for index in group["operation_indexes"])
        result = apply_operations(proposal.base_document, tuple(proposal.patch[index] for index in selected))
        for field, group_ids in by_field.items():
            before = _value_at(proposal.base_document, field)
            coach = _value_at(result, field)
            if before == coach:
                continue
            state, batch_id = states.get(field, ("applied", None))
            if state == "kept":
                continue
            history.setdefault(field, []).append(_CoachFieldWrite(proposal.id, tuple(sorted(group_ids)), before, coach, state, batch_id))
    return {field: tuple(entries) for field, entries in history.items()}


def _current_run(entries: Sequence[_CoachFieldWrite]) -> Sequence[_CoachFieldWrite]:
    """Coach writes since the last change made between two of them; earlier writes are settled.

    A write whose `before` differs from the previous write's value started from someone else's edit (or
    from an undo), so undoing or reapplying an older write would overwrite that edit.
    """
    start = 0
    for index in range(1, len(entries)):
        if entries[index - 1].coach != entries[index].before:
            start = index
    return entries[start:]


def _undo_chain(entries: Sequence[_CoachFieldWrite]) -> tuple[_CoachFieldWrite, ...]:
    """The latest applied write and the consecutive applied writes it built on."""
    applied = [entry for entry in _current_run(entries) if entry.state == "applied"]
    if not applied:
        return ()
    chain = [applied[-1]]
    for entry in reversed(applied[:-1]):
        if entry.coach != chain[0].before:
            break
        chain.insert(0, entry)
    return tuple(chain)


def _reapply_chain(entries: Sequence[_CoachFieldWrite]) -> tuple[_CoachFieldWrite, ...]:
    """The writes undone together most recently, when no later write is applied."""
    run = _current_run(entries)
    if not run or run[-1].state != "undone":
        return ()
    batch_id = run[-1].undo_batch_id
    return tuple(entry for entry in run if entry.state == "undone" and entry.undo_batch_id == batch_id)


def _set_value(document: dict[str, Any], path: str, value: Any) -> None:
    if value is _MISSING:
        parent, key = _resolve_parent(document, path, create=False)
        parent.pop(key, None)
        return
    parent, key = _resolve_parent(document, path, create=True)
    parent[key] = copy.deepcopy(value)


async def _accepted_working_copy_version_conn(db: aiosqlite.Connection, *, proposal_id: str) -> int:
    async with db.execute(
        """SELECT result_working_copy_version FROM agent_change_proposal_decision
        WHERE proposal_id=? AND decision_kind='patch_accept' ORDER BY created_at,id LIMIT 1""",
        (proposal_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or row[0] is None:
        raise AgentChangeProposalError("agent_change_proposal_stale")
    return int(row[0])


_SOURCE_LABELS = {"git": "ci"}


async def _live_document_conn(db: aiosqlite.Connection, *, team_id: str, agent_id: str) -> dict[str, Any] | None:
    async with db.execute(
        """SELECT revision.definition_json,revision.listing_json,release.audience_snapshot_json
        FROM team_subagent agent
        JOIN team_subagent_release release ON release.id=agent.live_release_id
        JOIN team_subagent_revision revision ON revision.id=release.revision_id
        WHERE agent.team_id=? AND agent.id=?""",
        (team_id, agent_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return {"definition": json.loads(str(row[0])), "listing": json.loads(str(row[1])), "audience": json.loads(str(row[2]))}


async def _draft_field_writes_conn(
    db: aiosqlite.Connection, *, team_id: str, agent_id: str, working: aiosqlite.Row
) -> dict[str, tuple[tuple[str, Any], ...]]:
    """Per field, the value each accepted proposal in this draft wrote, oldest first, with its source."""
    async with db.execute(
        """SELECT proposal.*,MAX(decision.created_at) AS decided
        FROM agent_change_proposal proposal
        JOIN agent_change_proposal_decision decision ON decision.proposal_id=proposal.id AND decision.decision_kind='patch_accept'
        WHERE proposal.team_id=? AND proposal.agent_id=? AND proposal.status='accepted'
          AND proposal.base_working_copy_id=? AND proposal.base_release_id IS ?
        GROUP BY proposal.id ORDER BY decided,proposal.id""",
        (team_id, agent_id, str(working["id"]), working["base_release_id"]),
    ) as cursor:
        rows = await cursor.fetchall()
    writes: dict[str, list[tuple[str, Any]]] = {}
    for row in rows:
        proposal = _proposal_from_row(row)
        async with db.execute(
            "SELECT selected_group_ids_json FROM agent_change_proposal_decision WHERE proposal_id=? AND decision_kind='patch_accept'",
            (proposal.id,),
        ) as cursor:
            selected = {str(group) for decision in await cursor.fetchall() for group in json.loads(str(decision[0]))}
        indexes = tuple(index for group in proposal.atomic_groups if str(group["id"]) in selected for index in group["operation_indexes"])
        result = apply_operations(proposal.base_document, tuple(proposal.patch[index] for index in indexes))
        source = _SOURCE_LABELS.get(proposal.source_type, proposal.source_type)
        for field in {_field_of(str(proposal.patch[index]["path"])) for index in indexes}:
            writes.setdefault(field, []).append((source, _value_at(result, field)))
    return {field: tuple(items) for field, items in writes.items()}


async def _owned_working_conn(db: aiosqlite.Connection, *, team_id: str, agent_id: str, actor_user_id: str) -> tuple[aiosqlite.Row, str]:
    async with db.execute(
        """
        SELECT working.*,COALESCE(agent.owner_user_id,agent.created_by) AS owner_user_id
        FROM team_subagent_working_copy working
        JOIN team_subagent agent ON agent.team_id=working.team_id AND agent.id=working.subagent_id
        JOIN teams team ON team.id=working.team_id AND team.status='active'
        JOIN users actor ON actor.id=? AND actor.status='active'
        JOIN team_members member ON member.team_id=working.team_id AND member.user_id=actor.id AND member.status='active'
        WHERE working.team_id=? AND working.subagent_id=? AND agent.lifecycle_state='active'
          AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
        """,
        (actor_user_id, team_id, agent_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentRevisionForbiddenError("agent_owner_required")
    return row, str(row["owner_user_id"])


async def _enqueue_proposal_notice_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    owner_user_id: str,
    proposal_id: str,
    source_type: str,
    group_count: int,
    timestamp: str,
) -> None:
    """Tell the Owner a proposal they did not write is waiting, in the proposal's own transaction."""

    await db.execute(
        """
        INSERT INTO agent_version_notification_outbox(
          id,team_id,agent_id,recipient_user_id,event_type,candidate_id,plan_id,
          snapshot_id,release_id,dedupe_key,safe_payload_json,status,
          lease_generation,attempt_count,available_at,created_at
        ) VALUES(?,?,?,?,'proposal.created',NULL,NULL,NULL,NULL,?,?,'pending',0,0,?,?)
        """,
        (
            f"aevn_{uuid4().hex}",
            team_id,
            agent_id,
            owner_user_id,
            f"proposal.created:{proposal_id}",
            _canonical_json(
                {
                    "group_count": group_count,
                    "proposal_id": proposal_id,
                    "schema_version": 1,
                    "source_type": source_type,
                },
            ),
            timestamp,
            timestamp,
        ),
    )


async def _require_owner_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
) -> str:
    async with db.execute(
        """
        SELECT COALESCE(agent.owner_user_id,agent.created_by) AS owner_user_id
        FROM team_subagent agent
        JOIN teams team ON team.id=agent.team_id AND team.status='active'
        JOIN users actor ON actor.id=? AND actor.status='active'
        JOIN team_members member ON member.team_id=agent.team_id AND member.user_id=actor.id AND member.status='active'
        WHERE agent.team_id=? AND agent.id=? AND agent.lifecycle_state='active'
          AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
        """,
        (actor_user_id, team_id, agent_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentRevisionForbiddenError("agent_owner_required")
    return str(row["owner_user_id"])


async def _validate_audience_conn(db: aiosqlite.Connection, *, team_id: str, audience: AgentAudience) -> None:
    if audience.user_ids:
        placeholders = ",".join("?" for _ in audience.user_ids)
        async with db.execute(
            f"""SELECT users.id FROM users
            JOIN team_members member ON member.user_id=users.id AND member.team_id=? AND member.status='active'
            WHERE users.status='active' AND users.id IN ({placeholders})""",  # noqa: S608
            (team_id, *audience.user_ids),
        ) as cursor:
            valid = {str(row[0]) for row in await cursor.fetchall()}
        if valid != set(audience.user_ids):
            raise AgentPublicationBlockedError("agent_audience_user_invalid")
    if audience.group_ids:
        placeholders = ",".join("?" for _ in audience.group_ids)
        async with db.execute(
            f"SELECT id FROM groups WHERE team_id=? AND status='active' AND id IN ({placeholders})",  # noqa: S608
            (team_id, *audience.group_ids),
        ) as cursor:
            valid = {str(row[0]) for row in await cursor.fetchall()}
        if valid != set(audience.group_ids):
            raise AgentPublicationBlockedError("agent_audience_group_invalid")


async def _create_neutral_working_copy_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
    timestamp: str,
) -> tuple[aiosqlite.Row, str]:
    async with db.execute(
        """
        SELECT agent.live_release_id,COALESCE(agent.owner_user_id,agent.created_by) AS owner_user_id,
               revision.definition_json,revision.listing_json,release.audience_snapshot_json
        FROM team_subagent agent
        JOIN teams team ON team.id=agent.team_id AND team.status='active'
        JOIN users actor ON actor.id=? AND actor.status='active'
        JOIN team_members member ON member.team_id=agent.team_id AND member.user_id=actor.id AND member.status='active'
        JOIN team_subagent_release release ON release.id=agent.live_release_id
        JOIN team_subagent_revision revision ON revision.id=release.revision_id
        WHERE agent.team_id=? AND agent.id=? AND agent.lifecycle_state='active'
          AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
        """,
        (actor_user_id, team_id, agent_id),
    ) as cursor:
        source = await cursor.fetchone()
    if source is None:
        raise AgentRevisionForbiddenError("agent_owner_required")
    working_id = f"awc_{uuid4().hex}"
    await db.execute(
        """
        INSERT INTO team_subagent_working_copy(
          id,team_id,subagent_id,base_release_id,definition_json,listing_json,audience_json,
          validation_json,row_version,updated_by,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,'{"status":"not_validated"}',1,?,?,?)
        """,
        (
            working_id,
            team_id,
            agent_id,
            str(source["live_release_id"]),
            str(source["definition_json"]),
            str(source["listing_json"]),
            str(source["audience_snapshot_json"]),
            actor_user_id,
            timestamp,
            timestamp,
        ),
    )
    async with db.execute("SELECT * FROM team_subagent_working_copy WHERE id=?", (working_id,)) as cursor:
        working = await cursor.fetchone()
    if working is None:
        raise AgentChangeProposalError("agent_working_copy_create_failed")
    return working, str(source["owner_user_id"])


def _document_from_working(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "definition": json.loads(str(row["definition_json"])),
        "listing": json.loads(str(row["listing_json"])),
        "audience": json.loads(str(row["audience_json"])),
    }


async def _proposal_conn(db: aiosqlite.Connection, *, team_id: str, agent_id: str, proposal_id: str) -> aiosqlite.Row | None:
    async with db.execute(
        _PROPOSAL_SELECT + " WHERE proposal.team_id=? AND proposal.agent_id=? AND proposal.id=?",
        (team_id, agent_id, proposal_id),
    ) as cursor:
        return await cursor.fetchone()


async def _proposal_by_identity_conn(
    db: aiosqlite.Connection, *, team_id: str, agent_id: str, source_type: str, source_ref: str, patch_hash: str
) -> aiosqlite.Row | None:
    async with db.execute(
        _PROPOSAL_SELECT
        + """
        WHERE proposal.team_id=? AND proposal.agent_id=? AND proposal.source_type=?
          AND proposal.source_ref=? AND proposal.patch_hash=?""",
        (team_id, agent_id, source_type, source_ref, patch_hash),
    ) as cursor:
        return await cursor.fetchone()


async def _proposal_by_source_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    source_type: ProposalSource,
    source_ref: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        _PROPOSAL_SELECT
        + """
        WHERE proposal.team_id=? AND proposal.agent_id=? AND proposal.source_type=? AND proposal.source_ref=?
        ORDER BY proposal.created_at,proposal.id LIMIT 1""",
        (team_id, agent_id, source_type, source_ref),
    ) as cursor:
        return await cursor.fetchone()


async def _decided_case_ids_conn(db: aiosqlite.Connection, *, proposal_id: str) -> dict[str, tuple[str, str | None]]:
    decisions: dict[str, tuple[str, str | None]] = {}
    async with db.execute(
        """SELECT decision_kind,selected_case_ids_json,result_suite_revision_id
        FROM agent_change_proposal_decision
        WHERE proposal_id=? AND decision_kind IN ('cases_accept','cases_dismiss')
        ORDER BY created_at,id""",
        (proposal_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    for row in rows:
        status = "accepted" if str(row["decision_kind"]) == "cases_accept" else "dismissed"
        revision_id = str(row["result_suite_revision_id"]) if row["result_suite_revision_id"] else None
        for case_id in json.loads(str(row["selected_case_ids_json"])):
            decisions[str(case_id)] = (status, revision_id)
    return decisions


async def _case_target_suite_conn(db: aiosqlite.Connection, proposal: AgentChangeProposal) -> str | None:
    if proposal.base_suite_revision_id is None and proposal.initial_suite_name is None:
        return None
    # Only this proposal's prior accepted append can advance its original target.
    # The suite writer independently rejects any intervening external revision.
    cursor = await db.execute(
        """SELECT r.id FROM agent_change_proposal_decision d
           JOIN agent_eval_suite_revision r ON r.id=d.result_suite_revision_id
           WHERE d.proposal_id=? AND d.decision_kind='cases_accept'
           ORDER BY r.sequence DESC LIMIT 1""",
        (proposal.id,),
    )
    row = await cursor.fetchone()
    return str(row["id"]) if row else proposal.base_suite_revision_id


async def _settle_case_only_proposal_conn(db: aiosqlite.Connection, proposal: AgentChangeProposal, timestamp: str) -> None:
    if (proposal.base_suite_revision_id is None and proposal.initial_suite_name is None) or proposal.status != "open":
        return
    decisions = await _decided_case_ids_conn(db, proposal_id=proposal.id)
    complete = all(str(case["id"]) in decisions for case in proposal.suggested_cases)
    if not complete:
        return
    status = "accepted" if any(item[0] == "accepted" for item in decisions.values()) else "dismissed"
    await db.execute(
        "UPDATE agent_change_proposal SET status=?,row_version=row_version+1,decided_at=? WHERE id=? AND status='open'",
        (status, timestamp, proposal.id),
    )


async def _proposal_with_case_decisions_conn(db: aiosqlite.Connection, row: aiosqlite.Row) -> AgentChangeProposal:
    proposal = _proposal_from_row(row)
    decisions = await _decided_case_ids_conn(db, proposal_id=proposal.id)
    cases = []
    for item in proposal.suggested_cases:
        case = dict(item)
        status, revision_id = decisions.get(str(case["id"]), ("pending", None))
        case["decision_status"] = status
        if revision_id is not None:
            case["result_suite_revision_id"] = revision_id
        cases.append(case)
    return replace(proposal, suggested_cases=tuple(cases), case_target_suite_revision_id=await _case_target_suite_conn(db, proposal))


def _proposal_from_row(row: aiosqlite.Row) -> AgentChangeProposal:
    return AgentChangeProposal(
        id=str(row["id"]),
        team_id=str(row["team_id"]),
        agent_id=str(row["agent_id"]),
        source_type=str(row["source_type"]),
        source_ref=str(row["source_ref"]),
        base_release_id=str(row["base_release_id"]) if row["base_release_id"] else None,
        base_working_copy_id=str(row["base_working_copy_id"]) if row["base_working_copy_id"] else None,
        base_working_copy_version=int(row["base_working_copy_version"]) if row["base_working_copy_version"] is not None else None,
        initial_suite_name=str(row["initial_suite_name"]) if row["initial_suite_name"] else None,
        base_suite_revision_id=str(row["base_suite_revision_id"]) if row["base_suite_revision_id"] else None,
        base_document=json.loads(str(row["base_document_json"])),
        patch=tuple(json.loads(str(row["patch_json"]))),
        atomic_groups=tuple(json.loads(str(row["atomic_groups_json"]))),
        suggested_cases=tuple(json.loads(str(row["suggested_cases_json"]))),
        rationale=str(row["rationale"]),
        provenance=json.loads(str(row["provenance_json"])),
        status=str(row["status"]),
        row_version=int(row["row_version"]),
        created_by_kind=str(row["created_by_kind"]),
        created_by_id=str(row["created_by_id"]),
        created_at=str(row["created_at"]),
        decided_at=str(row["decided_at"]) if row["decided_at"] else None,
        origin=_proposal_origin(row),
    )


def _proposal_origin(row: aiosqlite.Row) -> dict[str, Any] | None:
    """Content-free CI origin of a `git` proposal; the key name is NULL once the key is deleted."""
    if str(row["source_type"]) != "git":
        return None
    provenance = json.loads(str(row["provenance_json"]))
    # sqlite3.Row `in` tests values, not column names, so the column check needs keys().
    key_name = row["origin_key_name"] if "origin_key_name" in row.keys() else None  # noqa: SIM118
    return {
        "repository": provenance.get("repository"),
        "ref": provenance.get("ref"),
        "commit_sha": provenance.get("commit_sha"),
        "grant_id": provenance.get("automation_grant_id"),
        "key_name": str(key_name) if key_name is not None else None,
    }


def _path_allowed(path: Any, *, allow_document_roots: bool = False) -> bool:
    return isinstance(path, str) and (
        path in _ALLOWED_EXACT_PATHS or path.startswith(_ALLOWED_PREFIXES) or (allow_document_roots and path in {"/definition", "/listing"})
    )


def _pointer_parts(path: str) -> tuple[str, ...]:
    if not path.startswith("/") or path.endswith("/"):
        raise AgentChangeProposalError("agent_change_proposal_invalid_patch")
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/"))


def _resolve_parent(document: dict[str, Any], path: str, *, create: bool) -> tuple[dict[str, Any], str]:
    parts = _pointer_parts(path)
    current: Any = document
    for part in parts[:-1]:
        if not isinstance(current, dict):
            raise AgentChangeProposalError("agent_change_proposal_invalid_patch")
        if part not in current:
            if not create:
                raise AgentChangeProposalError("agent_change_proposal_invalid_patch")
            current[part] = {}
        current = current[part]
    if not isinstance(current, dict):
        raise AgentChangeProposalError("agent_change_proposal_invalid_patch")
    return current, parts[-1]


_MISSING = object()


def _value_at(document: Mapping[str, Any], path: str) -> Any:
    current: Any = document
    for part in _pointer_parts(path):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _same_field_value(left: Any, right: Any) -> bool:
    """Whether a draft field holds what live holds. An absent field and an empty one (None, "", [] or {})
    mean the same thing, so saving an untouched empty field from Configure is not counted as a change."""
    return left == right or (_empty_field_value(left) and _empty_field_value(right))


def _empty_field_value(value: Any) -> bool:
    return value is _MISSING or value is None or (isinstance(value, (str, list, dict)) and not value)


def _selected_operation_indexes(groups: Sequence[Mapping[str, Any]], selected: Sequence[str]) -> tuple[int, ...]:
    selected_set = set(selected)
    by_id = {str(group["id"]): group for group in groups}
    if unknown := sorted(selected_set - set(by_id)):
        raise _groups_invalid(
            "selected_group_unknown",
            f"Groups {unknown[:16]} are not groups of this proposal.",
            group_ids=unknown[:16],
            known_group_ids=sorted(by_id)[:64],
        )
    for group_id in selected_set:
        if set(by_id[group_id].get("depends_on", [])) - selected_set:
            raise AgentChangeProposalError("agent_change_group_dependency_missing")
    return tuple(index for group in groups if str(group["id"]) in selected_set for index in group["operation_indexes"])


def _require_acyclic(graph: Mapping[str, Sequence[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise _groups_invalid(
                "depends_on_cycle", f"Group {node!r} depends on itself through depends_on; remove one dependency of the cycle.", group_id=node
            )
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


def _safe_value(value: Any, *, limit: int = 2_000) -> Any:
    if value is _MISSING:
        return {"state": "missing"}
    text = _canonical_json(value)
    if len(text) > limit:
        return {"type": type(value).__name__, "hash": _hash_json(text), "size": len(text)}
    return value


# The Owner's own draft values go back whole, so the change list and undo conflicts can show and restore
# a long prompt; the bound only keeps a pathological field from bloating the workbench.
_DRAFT_VALUE_LIMIT = 200_000


def _draft_value(value: Any) -> Any:
    return _safe_value(value, limit=_DRAFT_VALUE_LIMIT)


def _hash_json(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()
