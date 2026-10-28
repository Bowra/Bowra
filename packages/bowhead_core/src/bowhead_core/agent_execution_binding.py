"""M3-B exact Agent admission and durable execution identity.

This module does not execute an Agent. It rechecks the current assignment/access
fence, fixes an immutable revision, and records the execution before the existing
``SubagentRunner`` starts a peer LLM loop. Team and system admission support standalone
transactions and composition inside the caller's Turn admission transaction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_behavior_contract import AgentBehaviorDescriptor
from bowhead_core.agent_behavior_flags import agent_behavior_m3_enabled
from bowhead_core.agent_behavior_materializer import AgentBehaviorMaterializer, default_agent_behavior_materializer
from bowhead_core.agent_listing_safety import scan_agent_listing
from bowhead_core.resource_dependency_projection import (
    replace_subject_projection_conn,
    stable_edges_from_definition,
)
from bowhead_core.team_plugin_store import TeamPluginError, expand_plugins_conn, selected_plugin_ids
from bowhead_core.team_subagent_access import TeamSubagentAccessResolver
from bowhead_core.team_subagent_store import release_semantics_active_conn

if TYPE_CHECKING:
    from bowhead_core.chat_reference_scope import ReferenceRequestScope
    from bowhead_core.chat_references import AgentResourceRef
    from bowhead_core.scheduled_agent_binding import ScheduledAgentBinding

_TEAM_AGENT_DISPATCHABLE_COLUMNS = (
    "status='enabled'",
    "lifecycle_state='active'",
    "definition_mode='revisioned'",
)


def team_agent_dispatchable_state_sql(alias: str = "") -> str:
    """The ONE definition-state predicate that lets a team_subagent row dispatch.

    Judgement 6 (§5.6.1 sibling criterion): advertising surfaces build their roster
    from this same predicate via :func:`dispatchable_team_agent_ids`, so the Lead is
    never told about an Agent that admission will refuse.  Two predicates means the
    Lead advertises Agents it cannot run, which reaches the user as a silent
    substitution by another Agent -- or as a raw error code in the final answer.
    """
    prefix = f"{alias}." if alias else ""
    return " AND ".join(f"{prefix}{column}" for column in _TEAM_AGENT_DISPATCHABLE_COLUMNS)


AgentSourceKind = Literal["team", "system"]
AgentDispatchSource = Literal["explicit", "lead", "plan", "system"]
AgentBindingSource = Literal[
    "marketplace",
    "detail",
    "hr",
    "mention",
    "schedule",
    "plan",
    "workflow_dry_run",
]
AgentExecutionStatus = Literal[
    "admitted",
    "running",
    "completed",
    "failed",
    "blocked",
    "cancelled",
]


class AgentExecutionBindingError(RuntimeError):
    """Finite, publication-safe M3-B admission failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ExactAgentExecutionBinding:
    execution_id: str
    team_id: str
    turn_id: str
    agent_id: str
    agent_source_kind: AgentSourceKind
    revision_ref: str
    release_id: str | None
    runtime_profile_name: str
    capability_fingerprint: str
    policy_fingerprint: str
    definition: dict[str, Any]
    listing: dict[str, Any]
    status: AgentExecutionStatus
    behavior_descriptor: AgentBehaviorDescriptor | None = None
    # Team Agents with Plugins: the authored (unexpanded) definition, so a run can tell directly
    # selected resources from Plugin contributions. Not part of the frozen, compared definition.
    authored_definition: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ExactAgentBindingExpectation:
    """Exact roster identity that admission must prove before inserting a run."""

    agent_id: str
    assignment_id: str | None
    revision_ref: str
    release_id: str | None
    behavior_fingerprint: str


@dataclass(frozen=True, slots=True)
class ProjectedTeamAgentReference:
    """Current server candidate, without a Turn, reservation or execution right."""

    profile_name: str
    expected: ExactAgentBindingExpectation
    policy_epoch: int
    policy_fingerprint: str
    definition_json: str = field(repr=False)
    behavior_descriptor: AgentBehaviorDescriptor = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProjectedSystemAgentRevision:
    """Prepared projection of the immutable system revision admission will create."""

    agent_id: str
    revision_ref: str
    definition: dict[str, Any]
    definition_hash: str
    capability_fingerprint: str
    behavior_descriptor: AgentBehaviorDescriptor


@dataclass(frozen=True, slots=True)
class TurnAgentBindingInput:
    """One typed exact-binding input shared by every Turn entry point.

    It is an admission input, not an authorization grant.  Each consumer must
    still recheck its current access and safety fences before execution.
    Schedule-only fields are kept inside this envelope so ``TurnRequest`` does
    not grow a second set of binding primitives for each channel.
    """

    binding_source: AgentBindingSource
    agent_id: str
    agent_source_kind: AgentSourceKind
    revision_ref: str
    release_id: str | None
    capability_fingerprint: str
    dependency_fingerprint: str
    assignment_id: str | None = None
    schedule_job_id: str | None = None
    schedule_binding_row_version: int | None = None

    def __post_init__(self) -> None:
        if self.binding_source not in {
            "marketplace",
            "detail",
            "hr",
            "mention",
            "schedule",
            "plan",
            "workflow_dry_run",
        }:
            raise ValueError("Turn Agent binding source is invalid")
        if self.agent_source_kind not in {"team", "system"}:
            raise ValueError("Turn Agent source kind is invalid")
        values = (self.agent_id, self.revision_ref)
        if any(not value or len(value) > 200 for value in values):
            raise ValueError("Turn Agent binding identity is invalid")
        optional_identities = (
            self.release_id,
            self.assignment_id,
            self.schedule_job_id,
        )
        if any(value is not None and (not value or len(value) > 200) for value in optional_identities):
            raise ValueError("Turn Agent binding optional identity is invalid")
        if not self.revision_ref.startswith("arv_"):
            raise ValueError("Turn Agent binding revision is invalid")
        if self.agent_source_kind == "team" and self.release_id is None:
            raise ValueError("Team Agent binding requires an exact release")
        if self.agent_source_kind == "team" and self.assignment_id is None:
            raise ValueError("Team Agent binding requires an assignment")
        if self.agent_source_kind == "system" and self.release_id is not None:
            raise ValueError("System Agent binding cannot carry a release")
        for fingerprint in (
            self.capability_fingerprint,
            self.dependency_fingerprint,
        ):
            if not fingerprint.startswith("sha256:") or len(fingerprint) < 8:
                raise ValueError("Turn Agent binding fingerprint is invalid")
        schedule_fields = (
            self.schedule_job_id,
            self.schedule_binding_row_version,
        )
        if self.binding_source == "schedule":
            if (
                not self.schedule_job_id
                or isinstance(self.schedule_binding_row_version, bool)
                or not isinstance(self.schedule_binding_row_version, int)
                or self.schedule_binding_row_version < 1
            ):
                raise ValueError("Scheduled Turn Agent binding is incomplete")
        elif any(value is not None for value in schedule_fields):
            raise ValueError("Non-scheduled Turn Agent binding has schedule fields")

    @classmethod
    def from_scheduled(cls, binding: ScheduledAgentBinding) -> TurnAgentBindingInput:
        return cls(
            binding_source="schedule",
            agent_id=str(binding.agent_id),
            agent_source_kind=binding.agent_source_kind,
            revision_ref=str(binding.revision_ref),
            release_id=(str(binding.release_id) if binding.release_id is not None else None),
            capability_fingerprint=str(binding.capability_fingerprint),
            dependency_fingerprint=str(binding.dependency_fingerprint),
            assignment_id=(str(binding.assignment_id) if binding.assignment_id is not None else None),
            schedule_job_id=str(binding.job_id),
            schedule_binding_row_version=int(binding.row_version),
        )


def project_system_agent_revision(
    *,
    team_id: str,
    profile_name: str,
    definition: Mapping[str, Any],
    behavior_materializer: AgentBehaviorMaterializer | None = None,
) -> ProjectedSystemAgentRevision:
    """Project the exact system revision identity without admitting execution.

    Lead uses this value for its closed Turn roster. Prepare it outside a DB
    transaction: initializing the default materializer may read catalog files.
    Admission verifies the roster identity before inserting an
    execution, so an override or Catalog race becomes ``agent_candidate_stale``
    instead of silently selecting the new definition.
    """
    if not team_id or not profile_name:
        raise AgentExecutionBindingError("agent_turn_scope_invalid")
    (
        agent_id,
        definition_value,
        definition_hash,
        revision_ref,
        capability_fingerprint,
    ) = _system_revision_projection_base(
        team_id=team_id,
        profile_name=profile_name,
        definition=definition,
    )
    materializer = behavior_materializer or default_agent_behavior_materializer()
    descriptor = materializer.compile_system_revision(
        revision_id=revision_ref,
        definition=definition_value,
        source_definition_hash=definition_hash,
    )
    return ProjectedSystemAgentRevision(
        agent_id=agent_id,
        revision_ref=revision_ref,
        definition=definition_value,
        definition_hash=definition_hash,
        capability_fingerprint=capability_fingerprint,
        behavior_descriptor=descriptor,
    )


class AgentExecutionBindingStore:
    """Admission/read store for exact root and dynamic Agent bindings."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        behavior_materializer: AgentBehaviorMaterializer | None = None,
    ) -> None:
        self.db_path = str(db_path)
        # Keep existing-revision reads independent of the shadow compiler and
        # its catalog. Only a newly inserted immutable revision requires the
        # sidecar, in the same transaction.
        self._behavior_materializer = behavior_materializer

    async def admit_eval_execution(
        self,
        *,
        eval_run_id: str,
        user_id: str,
        turn_id: str,
        dispatch_key: str,
    ) -> ExactAgentExecutionBinding:
        """Materialize the exact revision pinned by a running Agent Eval run.

        Eval managers need not hire the Agent.  Authorization comes from the
        already-authorized run plus current team membership and current Safety
        evidence; the scratch Turn remains scoped to that run's creator/team.
        """

        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT * FROM agent_eval_run
                    WHERE id=? AND created_by=? AND status='running'
                    """,
                    (eval_run_id, user_id),
                ) as cursor:
                    run = await cursor.fetchone()
                if run is None:
                    raise AgentExecutionBindingError("agent_eval_run_not_runnable")
                await _require_turn_scope(
                    db,
                    team_id=str(run["team_id"]),
                    user_id=user_id,
                    turn_id=turn_id,
                )
                await _require_eval_scratch_turn(
                    db,
                    eval_run_id=eval_run_id,
                    turn_id=turn_id,
                )
                source_kind = str(run["agent_source_kind"])
                if source_kind == "team":
                    snapshot = await _eval_team_snapshot(db, run)
                    _validate_team_snapshot(snapshot)
                    listing = _decode_object(snapshot["listing_json"], "Agent listing")
                    profile_name = str(listing.get("name") or snapshot["runtime_profile_name"])
                elif source_kind == "system":
                    snapshot = await _eval_system_snapshot(db, run)
                    _validate_system_snapshot(snapshot)
                    definition = _decode_object(snapshot["definition_json"], "system Agent definition")
                    profile_name = str(definition.get("name") or str(run["agent_id"]).removeprefix("system:"))
                    listing = None
                else:  # pragma: no cover - DB enum invariant
                    raise AgentExecutionBindingError("agent_eval_subject_invalid")
                if not profile_name:
                    raise AgentExecutionBindingError("agent_snapshot_malformed")
                existing = await _execution_by_dispatch_key(
                    db,
                    turn_id=turn_id,
                    dispatch_source="explicit",
                    dispatch_key=dispatch_key,
                )
                if existing is not None:
                    if (
                        str(existing["agent_id"]) != str(run["agent_id"])
                        or str(existing["revision_ref"]) != str(run["revision_ref"])
                        or str(existing["release_id"] or "") != str(run["release_id"] or "")
                    ):
                        raise AgentExecutionBindingError("agent_dispatch_identity_conflict")
                    _require_resumable_execution(existing)
                    await db.commit()
                    return _binding_from_rows(existing, snapshot, listing=listing)
                execution_id = f"aex_{uuid4().hex}"
                policy_fingerprint = _policy_fingerprint(
                    {
                        "eval_run_id": eval_run_id,
                        "source": "agent_eval",
                        "subject_fingerprint": str(run["subject_fingerprint"]),
                    }
                )
                resolved_definition_json = (
                    await _resolved_team_definition_json(db, team_id=str(run["team_id"]), snapshot=snapshot) if source_kind == "team" else None
                )
                await db.execute(
                    """
                    INSERT INTO turn_agent_execution(
                      id,team_id,turn_id,parent_execution_id,agent_id,agent_source_kind,
                      revision_ref,release_id,dispatch_source,dispatch_key,
                      runtime_profile_name,capability_fingerprint,policy_fingerprint,
                      status,created_at,resolved_definition_json
                    ) VALUES (?,?,?,NULL,?,?,?,?, 'explicit',?,?,?,?, 'admitted',?,?)
                    """,
                    (
                        execution_id,
                        run["team_id"],
                        turn_id,
                        run["agent_id"],
                        source_kind,
                        run["revision_ref"],
                        run["release_id"],
                        dispatch_key,
                        profile_name,
                        run["capability_fingerprint"],
                        policy_fingerprint,
                        timestamp,
                        resolved_definition_json,
                    ),
                )
                execution = await _execution_by_id(db, execution_id)
                if execution is None:
                    raise AgentExecutionBindingError("agent_execution_persist_failed")
                await db.commit()
                return _binding_from_rows(execution, snapshot, listing=listing)
            except BaseException:
                await db.rollback()
                raise

    async def project_reference_team_agent_in_connection(
        self,
        db: aiosqlite.Connection,
        *,
        scope: ReferenceRequestScope,
        resource: AgentResourceRef,
    ) -> ProjectedTeamAgentReference:
        """Inspect a selected current Team Agent using the admission predicates.

        No placeholder Turn, name lookup, descriptor compilation or writes. The
        caller owns the transaction; a later admission still checks every fact.
        """
        from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
        from bowhead_core.chat_references import AgentResourceRef

        if not db.in_transaction:
            raise AgentExecutionBindingError("agent_admission_transaction_required")
        scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python"))
        resource = AgentResourceRef.model_validate(resource.model_dump(mode="python"))
        if resource.source_kind != "team":
            raise AgentExecutionBindingError("reference_start_unsupported")
        previous_factory = db.row_factory
        db.row_factory = aiosqlite.Row
        try:
            epoch = await require_reference_request_scope(db, scope)
            if not await release_semantics_active_conn(db):
                raise AgentExecutionBindingError("reference_binding_unavailable")
            async with db.execute(
                f"SELECT * FROM team_subagent WHERE team_id=? AND id=? AND {team_agent_dispatchable_state_sql()}",
                (scope.team_id, resource.id),
            ) as cursor:
                aggregate = await cursor.fetchone()
            if aggregate is None:
                raise AgentExecutionBindingError("agent_not_runnable")
            assignment = await _require_assignment(db, team_id=scope.team_id, user_id=scope.user_id, agent_id=resource.id, source_kind="team")
            access_reason, snapshot = await _authorized_current_team_snapshot(
                db, db_path=self.db_path, team_id=scope.team_id, user_id=scope.user_id, aggregate=aggregate
            )
            descriptor = _behavior_descriptor_from_snapshot(snapshot)
            if descriptor is None or descriptor.verification_state == "blocked":
                raise AgentExecutionBindingError("agent_behavior_descriptor_unavailable")
            expected = ExactAgentBindingExpectation(
                agent_id=resource.id,
                assignment_id=resource.assignment_id,
                revision_ref=resource.version,
                release_id=resource.release_id,
                behavior_fingerprint=descriptor.behavior_fingerprint,
            )
            _require_expected_candidate(
                expected,
                agent_id=str(aggregate["id"]),
                assignment_id=str(assignment["id"]),
                revision_ref=str(snapshot["revision_id"]),
                release_id=str(snapshot["release_id"]),
                snapshot=snapshot,
            )
            return ProjectedTeamAgentReference(
                profile_name=str(aggregate["name"]),
                expected=expected,
                policy_epoch=epoch,
                policy_fingerprint=_policy_fingerprint(
                    {
                        "access_reason": access_reason,
                        "agent_row_version": int(aggregate["row_version"]),
                        "assignment_id": str(assignment["id"]),
                        "assignment_row_version": int(assignment["row_version"]),
                        "source": "team",
                    }
                ),
                definition_json=str(snapshot["definition_json"]),
                behavior_descriptor=descriptor,
            )
        finally:
            db.row_factory = previous_factory

    async def admit_team_execution(
        self,
        *,
        team_id: str,
        user_id: str,
        turn_id: str,
        profile_name: str,
        dispatch_key: str,
        agent_id: str | None = None,
        dispatch_source: AgentDispatchSource = "lead",
        parent_execution_id: str | None = None,
        expected: ExactAgentBindingExpectation | None = None,
    ) -> ExactAgentExecutionBinding:
        """Bind a Team Agent using the shared admission transaction kernel."""
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                binding = await self.admit_team_execution_in_connection(
                    db,
                    team_id=team_id,
                    user_id=user_id,
                    turn_id=turn_id,
                    profile_name=profile_name,
                    dispatch_key=dispatch_key,
                    agent_id=agent_id,
                    dispatch_source=dispatch_source,
                    parent_execution_id=parent_execution_id,
                    expected=expected,
                )
                await db.commit()
                return binding
            except BaseException:
                await db.rollback()
                raise

    async def admit_team_execution_in_connection(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        user_id: str,
        turn_id: str,
        profile_name: str,
        dispatch_key: str,
        agent_id: str | None = None,
        dispatch_source: AgentDispatchSource = "lead",
        parent_execution_id: str | None = None,
        expected: ExactAgentBindingExpectation | None = None,
    ) -> ExactAgentExecutionBinding:
        """Reserve an exact Team Agent execution inside the caller's transaction.

        The caller owns BEGIN, commit and rollback, including on cancellation.
        All access, assignment, release and dispatch-identity checks use this
        connection, so an uncommitted Turn and its execution become visible
        together. No connection is opened and no worker is started here.
        """
        if not db.in_transaction:
            raise AgentExecutionBindingError("agent_admission_transaction_required")
        timestamp = _now()
        previous_factory = db.row_factory
        db.row_factory = aiosqlite.Row
        try:
            await _require_turn_scope(
                db,
                team_id=team_id,
                user_id=user_id,
                turn_id=turn_id,
            )
            aggregate = await _team_agent_row(
                db,
                team_id=team_id,
                profile_name=profile_name,
                agent_id=agent_id,
            )
            stable_agent_id = str(aggregate["id"])
            assignment = await _require_assignment(
                db,
                team_id=team_id,
                user_id=user_id,
                agent_id=stable_agent_id,
                source_kind="team",
            )
            access_reason, current_snapshot = await _authorized_current_team_snapshot(
                db, db_path=self.db_path, team_id=team_id, user_id=user_id, aggregate=aggregate
            )

            existing = await _execution_by_dispatch_key(
                db,
                turn_id=turn_id,
                dispatch_source=dispatch_source,
                dispatch_key=dispatch_key,
            )
            if existing is not None:
                if (
                    str(existing["agent_source_kind"]) != "team"
                    or str(existing["agent_id"]) != stable_agent_id
                    or str(existing["runtime_profile_name"]) != profile_name
                ):
                    raise AgentExecutionBindingError("agent_dispatch_identity_conflict")
                _require_resumable_execution(existing)
                snapshot = await _team_snapshot_by_execution(db, existing)
                _validate_team_snapshot(snapshot)
                _require_expected_candidate(
                    expected,
                    agent_id=stable_agent_id,
                    assignment_id=str(assignment["id"]),
                    revision_ref=str(existing["revision_ref"]),
                    release_id=str(existing["release_id"]),
                    snapshot=snapshot,
                )
                return _binding_from_rows(existing, snapshot)

            _require_expected_candidate(
                expected,
                agent_id=stable_agent_id,
                assignment_id=str(assignment["id"]),
                revision_ref=str(current_snapshot["revision_id"]),
                release_id=str(current_snapshot["release_id"]),
                snapshot=current_snapshot,
            )

            policy_fingerprint = _policy_fingerprint(
                {
                    "access_reason": access_reason,
                    "agent_row_version": int(aggregate["row_version"]),
                    "assignment_id": str(assignment["id"]),
                    "assignment_row_version": int(assignment["row_version"]),
                    "source": "team",
                }
            )
            resolved_definition_json = await _resolved_team_definition_json(db, team_id=team_id, snapshot=current_snapshot)
            execution_id = f"aex_{uuid4().hex}"
            await db.execute(
                """
                INSERT INTO turn_agent_execution(
                  id,team_id,turn_id,parent_execution_id,agent_id,agent_source_kind,
                  revision_ref,release_id,dispatch_source,dispatch_key,
                  runtime_profile_name,capability_fingerprint,policy_fingerprint,
                  status,created_at,resolved_definition_json
                ) VALUES (?,?,?,?,?,'team',?,?,?,?,?,?,?,'admitted',?,?)
                """,
                (
                    execution_id,
                    team_id,
                    turn_id,
                    parent_execution_id,
                    stable_agent_id,
                    str(current_snapshot["revision_id"]),
                    str(current_snapshot["release_id"]),
                    dispatch_source,
                    dispatch_key,
                    profile_name,
                    str(current_snapshot["capability_fingerprint"]),
                    policy_fingerprint,
                    timestamp,
                    resolved_definition_json,
                ),
            )
            row = await _execution_by_id(db, execution_id)
            if row is None:
                raise AgentExecutionBindingError("agent_execution_persist_failed")
            return _binding_from_rows(row, current_snapshot)
        finally:
            db.row_factory = previous_factory

    async def admit_system_execution(
        self,
        *,
        team_id: str,
        user_id: str,
        turn_id: str,
        profile_name: str,
        dispatch_key: str,
        definition: Mapping[str, Any],
        manifest_fingerprint: str,
        override_version: int | None,
        system_required: bool = False,
        enforce_current_override: bool = True,
        dispatch_source: AgentDispatchSource = "lead",
        parent_execution_id: str | None = None,
        expected: ExactAgentBindingExpectation | None = None,
    ) -> ExactAgentExecutionBinding:
        """Bind a system Agent, preparing a missing revision outside the lock."""
        definition_value = _decode_object(_canonical_json(definition), "system Agent definition")
        prepared_revision = None
        while True:
            try:
                async with open_shared_ctx(self.db_path) as db:
                    await db.execute("BEGIN IMMEDIATE")
                    try:
                        binding = await self.admit_system_execution_in_connection(
                            db,
                            team_id=team_id,
                            user_id=user_id,
                            turn_id=turn_id,
                            profile_name=profile_name,
                            dispatch_key=dispatch_key,
                            definition=definition_value,
                            manifest_fingerprint=manifest_fingerprint,
                            override_version=override_version,
                            system_required=system_required,
                            enforce_current_override=enforce_current_override,
                            dispatch_source=dispatch_source,
                            parent_execution_id=parent_execution_id,
                            expected=expected,
                            prepared_revision=prepared_revision,
                        )
                        await db.commit()
                        return binding
                    except BaseException:
                        await db.rollback()
                        raise
            except AgentExecutionBindingError as exc:
                if exc.code != "agent_system_revision_preparation_required" or prepared_revision is not None:
                    raise
            # Only a missing revision gets one preparation/retry. The kernel
            # rechecks current authority after the transaction has been released.
            prepared_revision = project_system_agent_revision(
                team_id=team_id,
                profile_name=profile_name,
                definition=definition_value,
                behavior_materializer=self._behavior_materializer,
            )

    async def admit_system_execution_in_connection(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        user_id: str,
        turn_id: str,
        profile_name: str,
        dispatch_key: str,
        definition: Mapping[str, Any],
        manifest_fingerprint: str,
        override_version: int | None,
        system_required: bool = False,
        enforce_current_override: bool = True,
        dispatch_source: AgentDispatchSource = "lead",
        parent_execution_id: str | None = None,
        expected: ExactAgentBindingExpectation | None = None,
        prepared_revision: ProjectedSystemAgentRevision | None = None,
    ) -> ExactAgentExecutionBinding:
        """Reserve a system execution in the caller's transaction, without I/O preparation.

        The caller owns BEGIN/commit/rollback, including on cancellation. A new
        revision requires project_system_agent_revision prepared before BEGIN;
        this value is not an authorization grant. Existing executions/snapshots
        reuse their immutable sidecars and need no preparation. All current
        scope, assignment, override and candidate checks run on this connection.
        """
        if not db.in_transaction:
            raise AgentExecutionBindingError("agent_admission_transaction_required")
        (
            agent_id,
            definition_value,
            definition_hash,
            revision_id,
            capability_fingerprint,
        ) = _system_revision_projection_base(
            team_id=team_id,
            profile_name=profile_name,
            definition=definition,
        )
        definition_json = _canonical_json(definition_value)
        if prepared_revision is not None:
            if (
                prepared_revision.agent_id != agent_id
                or prepared_revision.revision_ref != revision_id
                or prepared_revision.definition_hash != definition_hash
                or _canonical_json(prepared_revision.definition) != definition_json
                or prepared_revision.capability_fingerprint != capability_fingerprint
                or prepared_revision.behavior_descriptor.source_definition_hash != definition_hash
            ):
                raise AgentExecutionBindingError("agent_candidate_stale")
            # Revalidate even a server-side model_copy before its first await.
            try:
                behavior = AgentBehaviorDescriptor.model_validate_json(prepared_revision.behavior_descriptor.canonical_json())
            except ValueError as exc:
                raise AgentExecutionBindingError("agent_behavior_descriptor_invalid") from exc
        timestamp = _now()
        previous_factory = db.row_factory
        db.row_factory = aiosqlite.Row
        try:
            if definition_value.get("schema_version") in (2, 3):
                from bowhead_core.system_skill_snapshot import MIGRATION_TRIGGER, OUTPUT_BUDGET_TRIGGER, decode_system_skill_snapshot

                decode_system_skill_snapshot(definition_value)
                triggers = [OUTPUT_BUDGET_TRIGGER] if definition_value["schema_version"] == 3 else []
                if definition_value["schema_version"] == 2 or "skill_runtime_scope" in definition_value:
                    triggers.append(MIGRATION_TRIGGER)
                for trigger in triggers:
                    async with db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)) as cursor:
                        if await cursor.fetchone() is None:
                            raise AgentExecutionBindingError("agent_system_skill_snapshot_migration_required")
            await _require_turn_scope(
                db,
                team_id=team_id,
                user_id=user_id,
                turn_id=turn_id,
            )
            if enforce_current_override:
                await _require_current_system_override(
                    db,
                    team_id=team_id,
                    profile_name=profile_name,
                    expected_version=override_version,
                )
            assignment = None
            if not system_required:
                assignment = await _require_assignment(
                    db,
                    team_id=team_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    source_kind="system",
                )
            existing = await _execution_by_dispatch_key(
                db,
                turn_id=turn_id,
                dispatch_source=dispatch_source,
                dispatch_key=dispatch_key,
            )
            if existing is not None:
                if (
                    str(existing["agent_source_kind"]) != "system"
                    or str(existing["agent_id"]) != agent_id
                    or str(existing["runtime_profile_name"]) != profile_name
                ):
                    raise AgentExecutionBindingError("agent_dispatch_identity_conflict")
                _require_resumable_execution(existing)
                snapshot = await _system_snapshot_by_execution(db, existing)
                _validate_system_snapshot(snapshot)
                _require_expected_candidate(
                    expected,
                    agent_id=agent_id,
                    assignment_id=None,
                    revision_ref=str(existing["revision_ref"]),
                    release_id=None,
                    snapshot=snapshot,
                )
                return _binding_from_rows(existing, snapshot)

            if prepared_revision is None:
                async with db.execute(
                    "SELECT 1 FROM system_agent_effective_revision WHERE team_id=? AND agent_id=? AND definition_hash=?",
                    (team_id, agent_id, definition_hash),
                ) as cursor:
                    if await cursor.fetchone() is None:
                        raise AgentExecutionBindingError("agent_system_revision_preparation_required")

            inserted_revision = await db.execute(
                """
                INSERT INTO system_agent_effective_revision(
                  id,team_id,agent_id,manifest_fingerprint,override_version,
                  definition_json,definition_hash,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(team_id,agent_id,definition_hash) DO NOTHING
                """,
                (
                    revision_id,
                    team_id,
                    agent_id,
                    manifest_fingerprint,
                    override_version,
                    definition_json,
                    definition_hash,
                    timestamp,
                ),
            )
            if inserted_revision.rowcount == 1:
                assert prepared_revision is not None
                await AgentBehaviorMaterializer.persist_system_revision_conn(
                    db,
                    revision_id=revision_id,
                    team_id=team_id,
                    agent_id=agent_id,
                    descriptor=behavior,
                    created_at=timestamp,
                )
            async with db.execute(
                """
                SELECT revision.*,
                       behavior.descriptor_json AS behavior_descriptor_json
                FROM system_agent_effective_revision revision
                LEFT JOIN system_agent_revision_behavior behavior
                  ON behavior.team_id=revision.team_id
                 AND behavior.agent_id=revision.agent_id
                 AND behavior.revision_id=revision.id
                WHERE revision.team_id=? AND revision.agent_id=?
                  AND revision.definition_hash=?
                """,
                (team_id, agent_id, definition_hash),
            ) as cursor:
                snapshot = await cursor.fetchone()
            if snapshot is None:
                raise AgentExecutionBindingError("agent_system_snapshot_unavailable")
            _validate_system_snapshot(snapshot)
            _require_expected_candidate(
                expected,
                agent_id=agent_id,
                assignment_id=None,
                revision_ref=str(snapshot["id"]),
                release_id=None,
                snapshot=snapshot,
            )
            projection, edges = stable_edges_from_definition(
                team_id=team_id,
                subject_kind="system_agent_revision",
                subject_ref=str(snapshot["id"]),
                agent_id=agent_id,
                subject_fingerprint=definition_hash,
                definition=definition_value,
            )
            await replace_subject_projection_conn(
                db,
                projection=projection,
                edges=edges,
                projected_at=timestamp,
            )
            policy_fingerprint = _system_policy_fingerprint(
                assignment, override_version, system_required=system_required, enforce_current_override=enforce_current_override
            )
            execution_id = f"aex_{uuid4().hex}"
            await db.execute(
                """
                INSERT INTO turn_agent_execution(
                  id,team_id,turn_id,parent_execution_id,agent_id,agent_source_kind,
                  revision_ref,release_id,dispatch_source,dispatch_key,
                  runtime_profile_name,capability_fingerprint,policy_fingerprint,
                  status,created_at
                ) VALUES (?,?,?,?,?,'system',?,NULL,?,?,?,?,?,'admitted',?)
                """,
                (
                    execution_id,
                    team_id,
                    turn_id,
                    parent_execution_id,
                    agent_id,
                    str(snapshot["id"]),
                    dispatch_source,
                    dispatch_key,
                    profile_name,
                    capability_fingerprint,
                    policy_fingerprint,
                    timestamp,
                ),
            )
            row = await _execution_by_id(db, execution_id)
            if row is None:
                raise AgentExecutionBindingError("agent_execution_persist_failed")
            listing = {
                "display_name": str(definition_value.get("display_name") or profile_name),
                "description": str(definition_value.get("description") or ""),
                "name": profile_name,
            }
            return _binding_from_rows(row, snapshot, listing=listing)
        finally:
            db.row_factory = previous_factory

    async def admit_scheduled_team_execution(
        self,
        *,
        team_id: str,
        user_id: str,
        turn_id: str,
        profile_name: str,
        dispatch_key: str,
        job_id: str,
        binding_row_version: int,
        dispatch_source: AgentDispatchSource = "lead",
        parent_execution_id: str | None = None,
        expected: ExactAgentBindingExpectation | None = None,
    ) -> ExactAgentExecutionBinding:
        """Admit the exact Team Agent revision confirmed by a schedule pin."""

        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_turn_scope(
                    db,
                    team_id=team_id,
                    user_id=user_id,
                    turn_id=turn_id,
                )
                pin = await _scheduled_team_pin(
                    db,
                    team_id=team_id,
                    user_id=user_id,
                    turn_id=turn_id,
                    job_id=job_id,
                    binding_row_version=binding_row_version,
                )
                aggregate = await _team_agent_row(
                    db,
                    team_id=team_id,
                    profile_name=profile_name,
                    agent_id=str(pin["agent_id"]),
                )
                assignment = await _require_assignment(
                    db,
                    team_id=team_id,
                    user_id=user_id,
                    agent_id=str(pin["agent_id"]),
                    source_kind="team",
                )
                if str(assignment["id"]) != str(pin["assignment_id"]):
                    raise AgentExecutionBindingError(
                        "agent_assignment_inactive",
                    )
                decisions = await TeamSubagentAccessResolver(
                    self.db_path,
                ).resolve_many_in_connection(
                    db,
                    team_id=team_id,
                    actor_user_id=user_id,
                    subagent_id=str(pin["agent_id"]),
                    now=datetime.now(UTC),
                )
                access = decisions.get(str(pin["agent_id"]))
                if access is None or not access.resolution.can_consume:
                    raise AgentExecutionBindingError("agent_access_denied")
                snapshot = await _team_snapshot_by_pin(db, pin)
                _validate_team_snapshot(snapshot)
                _require_expected_candidate(
                    expected,
                    agent_id=str(pin["agent_id"]),
                    assignment_id=str(pin["assignment_id"]),
                    revision_ref=str(pin["revision_ref"]),
                    release_id=str(pin["release_id"]),
                    snapshot=snapshot,
                )

                existing = await _execution_by_dispatch_key(
                    db,
                    turn_id=turn_id,
                    dispatch_source=dispatch_source,
                    dispatch_key=dispatch_key,
                )
                if existing is not None:
                    if (
                        str(existing["agent_source_kind"]) != "team"
                        or str(existing["agent_id"]) != str(pin["agent_id"])
                        or str(existing["runtime_profile_name"]) != profile_name
                        or str(existing["revision_ref"]) != str(pin["revision_ref"])
                        or str(existing["release_id"]) != str(pin["release_id"])
                    ):
                        raise AgentExecutionBindingError(
                            "agent_dispatch_identity_conflict",
                        )
                    _require_resumable_execution(existing)
                    await db.commit()
                    return _binding_from_rows(existing, snapshot)

                policy_fingerprint = _policy_fingerprint(
                    {
                        "access_reason": access.resolution.access_reason,
                        "agent_row_version": int(aggregate["row_version"]),
                        "assignment_id": str(assignment["id"]),
                        "assignment_row_version": int(assignment["row_version"]),
                        "schedule_binding_row_version": binding_row_version,
                        "scheduled_job_id": job_id,
                        "source": "schedule",
                    }
                )
                resolved_definition_json = await _resolved_team_definition_json(db, team_id=team_id, snapshot=snapshot)
                execution_id = f"aex_{uuid4().hex}"
                await db.execute(
                    """
                    INSERT INTO turn_agent_execution(
                      id,team_id,turn_id,parent_execution_id,agent_id,agent_source_kind,
                      revision_ref,release_id,dispatch_source,dispatch_key,
                      runtime_profile_name,capability_fingerprint,policy_fingerprint,
                      status,created_at,resolved_definition_json
                    ) VALUES (?,?,?,?,?,'team',?,?,?,?,?,?,?,'admitted',?,?)
                    """,
                    (
                        execution_id,
                        team_id,
                        turn_id,
                        parent_execution_id,
                        str(pin["agent_id"]),
                        str(pin["revision_ref"]),
                        str(pin["release_id"]),
                        dispatch_source,
                        dispatch_key,
                        profile_name,
                        str(pin["capability_fingerprint"]),
                        policy_fingerprint,
                        timestamp,
                        resolved_definition_json,
                    ),
                )
                execution = await _execution_by_id(db, execution_id)
                if execution is None:
                    raise AgentExecutionBindingError(
                        "agent_execution_persist_failed",
                    )
                await db.commit()
                return _binding_from_rows(execution, snapshot)
            except BaseException:
                await db.rollback()
                raise

    async def read_reference_primary_in_connection(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        user_id: str,
        turn_id: str,
        resource: AgentResourceRef,
        policy_fingerprint: str,
        binding_source: Literal["mention", "lead"] = "mention",
        read_output: bool = False,
        retained_plan_id: str | None = None,
    ) -> ExactAgentExecutionBinding:
        """Read and reauthorize the already reserved primary; never create a run.

        The reference reader supplies the validated source/plan identity. This
        method uses only the caller's transaction and retains its row factory.
        A returned running binding is not permission to start another worker.
        """
        from bowhead_core.chat_references import AgentResourceRef

        if not db.in_transaction:
            raise AgentExecutionBindingError("agent_admission_transaction_required")
        if binding_source not in {"mention", "lead"}:
            raise AgentExecutionBindingError("reference_primary_unavailable")
        resource = AgentResourceRef.model_validate(resource.model_dump(mode="python", exclude_none=True))
        previous_factory = db.row_factory
        db.row_factory = aiosqlite.Row
        try:
            live_turn_id = turn_id
            if retained_plan_id is not None:
                async with db.execute(
                    "SELECT p.execution_turn_id FROM plans p JOIN turns original ON original.id=? "
                    "JOIN sessions session ON session.id=original.session_id "
                    "WHERE p.id=? AND p.session_id=original.session_id AND p.user_id=? "
                    "AND p.workspace_id=session.workspace_id AND session.user_id=p.user_id "
                    "AND p.status='running' AND original.status='clarification_pending' "
                    "AND json_extract(CASE WHEN json_valid(p.spec_json) THEN p.spec_json ELSE '{}' END,'$.reference.source_turn_id')=original.id "
                    "AND json_extract(p.spec_json,'$.reference.contract')='reference-plan.v1'",
                    (turn_id, retained_plan_id, user_id),
                ) as cursor:
                    plan_owner = await cursor.fetchone()
                if plan_owner is None:
                    raise AgentExecutionBindingError("reference_plan_source_unavailable")
                live_turn_id = plan_owner["execution_turn_id"]
            await _require_turn_scope(db, team_id=team_id, user_id=user_id, turn_id=live_turn_id)
            async with db.execute(
                "SELECT * FROM turn_agent_binding WHERE turn_id=? AND team_id=? AND user_id=?",
                (turn_id, team_id, user_id),
            ) as cursor:
                root = await cursor.fetchone()
            if root is None or root["binding_source"] != binding_source or not root["primary_execution_id"]:
                raise AgentExecutionBindingError("reference_primary_unavailable")
            if (
                root["requested_agent_id"],
                root["agent_source_kind"],
                root["revision_ref"],
                root["release_id"],
                root["assignment_id"],
                root["policy_fingerprint"],
            ) != (resource.id, resource.source_kind, resource.version, resource.release_id, resource.assignment_id, policy_fingerprint):
                raise AgentExecutionBindingError("reference_primary_mismatch")
            execution = await _execution_by_id(db, str(root["primary_execution_id"]))
            if execution is None or (
                execution["turn_id"],
                execution["team_id"],
                execution["agent_id"],
                execution["agent_source_kind"],
                execution["revision_ref"],
                execution["release_id"],
                execution["capability_fingerprint"],
                execution["policy_fingerprint"],
                execution["parent_execution_id"],
                execution["dispatch_source"],
                execution["dispatch_key"],
            ) != (
                turn_id,
                team_id,
                resource.id,
                resource.source_kind,
                resource.version,
                resource.release_id,
                root["capability_fingerprint"],
                policy_fingerprint,
                None,
                "explicit" if binding_source == "mention" else "lead",
                "reference:primary",
            ):
                raise AgentExecutionBindingError("reference_primary_mismatch")
            if read_output and execution["status"] in {"completed", "failed"}:
                async with db.execute(
                    "SELECT 1 FROM chat_reference_plans p JOIN chat_reference_evidence o ON o.turn_id=p.turn_id "
                    "WHERE p.turn_id=? AND json_extract(p.plan_json,'$.orchestration_contract')='lead-delegation.v1' "
                    "AND o.execution_id=? AND o.kind='output_received' AND o.retention_state='available' "
                    "AND julianday(o.expires_at)>julianday('now')",
                    (turn_id, execution["id"]),
                ) as cursor:
                    if await cursor.fetchone() is None:
                        raise AgentExecutionBindingError("reference_output_unavailable")
            else:
                _require_resumable_execution(execution)
            if resource.source_kind == "system":
                snapshot = await _authorized_system_execution_snapshot(db, execution, user_id=user_id)
            else:
                assignment = await _require_execution_assignment(db, execution, user_id=user_id)
                snapshot = await _team_snapshot_by_execution(db, execution)
                # Reference retains its pinned revision while checking current access.
                async with db.execute(
                    f"SELECT * FROM team_subagent WHERE team_id=? AND id=? AND {team_agent_dispatchable_state_sql()}",
                    (team_id, resource.id),
                ) as cursor:
                    aggregate = await cursor.fetchone()
                if aggregate is None:
                    raise AgentExecutionBindingError("agent_not_runnable")
                await _authorized_current_team_snapshot(db, db_path=self.db_path, team_id=team_id, user_id=user_id, aggregate=aggregate)
                _validate_team_snapshot(snapshot)
                if str(assignment["id"]) != resource.assignment_id:
                    raise AgentExecutionBindingError("agent_candidate_stale")
            if _behavior_descriptor_from_snapshot(snapshot) is None:
                raise AgentExecutionBindingError("agent_behavior_descriptor_unavailable")
            binding = _binding_from_rows(execution, snapshot)
            if binding.execution_id != root["primary_execution_id"]:
                raise AgentExecutionBindingError("reference_primary_mismatch")
            return binding
        finally:
            db.row_factory = previous_factory

    async def read_completed_helper_in_connection(
        self,
        db: aiosqlite.Connection,
        *,
        scope: ReferenceRequestScope,
        turn_id: str,
        execution_id: str,
        dispatch_key: str,
    ) -> ExactAgentExecutionBinding:
        """Preserve completed-Team-only authority for legacy ordinary envelopes."""
        return await self.read_settled_helper_in_connection(
            db,
            scope=scope,
            turn_id=turn_id,
            execution_id=execution_id,
            dispatch_key=dispatch_key,
            status="completed",
            failure_code=None,
            required_source="team",
        )

    async def read_settled_helper_in_connection(
        self,
        db: aiosqlite.Connection,
        *,
        scope: ReferenceRequestScope,
        turn_id: str,
        execution_id: str,
        dispatch_key: str,
        status: Literal["completed", "failed"],
        failure_code: str | None,
        system_profile_resolver: Callable[..., Any] | None = None,
        required_source: AgentSourceKind | None = None,
    ) -> ExactAgentExecutionBinding:
        """Reauthorize original settled output without admitting a new execution.

        The caller separately proves native outcome and effect settlement. This
        reader checks current authority and the exact durable terminal outcome
        in the caller's transaction; terminal status alone grants no continuation.
        """
        from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope

        if not db.in_transaction:
            raise AgentExecutionBindingError("agent_admission_transaction_required")
        scope = ReferenceRequestScope.model_validate(scope.model_dump(mode="python"))
        if (
            scope.session_id is None
            or status not in {"completed", "failed"}
            or (status == "completed" and failure_code is not None)
            or (status == "failed" and (type(failure_code) is not str or not failure_code))
            or any(type(value) is not str or not 1 <= len(value) <= 512 for value in (turn_id, execution_id))
            or type(dispatch_key) is not str
            or not dispatch_key.startswith("call:")
            or not 5 < len(dispatch_key) <= 517
        ):
            raise AgentExecutionBindingError("agent_completed_helper_unavailable")
        previous_factory = db.row_factory
        db.row_factory = aiosqlite.Row
        try:
            try:
                await require_reference_request_scope(db, scope)
            except ValueError as exc:
                raise AgentExecutionBindingError("agent_turn_scope_invalid") from exc
            async with db.execute(
                "SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id "
                "WHERE t.id=? AND s.id=? AND s.user_id=? AND s.workspace_id=? AND s.deleted_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM turn_agent_binding b WHERE b.turn_id=t.id)",
                (turn_id, scope.session_id, scope.user_id, scope.workspace_id),
            ) as cursor:
                if await cursor.fetchone() is None:
                    raise AgentExecutionBindingError("agent_completed_helper_unavailable")
            execution = await _execution_by_id(db, execution_id)
            if (
                execution is None
                or (
                    execution["team_id"],
                    execution["turn_id"],
                    execution["dispatch_source"],
                    execution["dispatch_key"],
                    execution["parent_execution_id"],
                    execution["status"],
                )
                != (scope.team_id, turn_id, "lead", dispatch_key, None, status)
                or execution["agent_source_kind"] not in {"team", "system"}
                or (required_source is not None and execution["agent_source_kind"] != required_source)
                or (execution["started_at"] is None or execution["ended_at"] is None or execution["failure_code"] != failure_code)
            ):
                raise AgentExecutionBindingError("agent_completed_helper_unavailable")
            if execution["agent_source_kind"] == "system":
                if system_profile_resolver is None:
                    raise AgentExecutionBindingError("agent_completed_helper_unavailable")
                snapshot = await _authorized_system_execution_snapshot(
                    db,
                    execution,
                    user_id=scope.user_id,
                    system_profile_resolver=system_profile_resolver,
                )
                return _binding_from_rows(execution, snapshot)
            if not await release_semantics_active_conn(db):
                raise AgentExecutionBindingError("agent_completed_helper_unavailable")
            assignment = await _require_execution_assignment(db, execution, user_id=scope.user_id)
            async with db.execute(
                f"SELECT * FROM team_subagent WHERE team_id=? AND id=? AND {team_agent_dispatchable_state_sql()}",
                (scope.team_id, execution["agent_id"]),
            ) as cursor:
                aggregate = await cursor.fetchone()
            if aggregate is None:
                raise AgentExecutionBindingError("agent_not_runnable")
            access_reason, current = await _authorized_current_team_snapshot(
                db, db_path=self.db_path, team_id=scope.team_id, user_id=scope.user_id, aggregate=aggregate
            )
            policy_fingerprint = _policy_fingerprint(
                {
                    "access_reason": access_reason,
                    "agent_row_version": int(aggregate["row_version"]),
                    "assignment_id": str(assignment["id"]),
                    "assignment_row_version": int(assignment["row_version"]),
                    "source": "team",
                }
            )
            if (current["revision_id"], current["release_id"], current["capability_fingerprint"], policy_fingerprint) != (
                execution["revision_ref"],
                execution["release_id"],
                execution["capability_fingerprint"],
                execution["policy_fingerprint"],
            ):
                raise AgentExecutionBindingError("agent_completed_helper_changed")
            snapshot = await _team_snapshot_by_execution(db, execution)
            _validate_team_snapshot(snapshot)
            if await _resolved_team_definition_json(db, team_id=scope.team_id, snapshot=current) != execution["resolved_definition_json"]:
                raise AgentExecutionBindingError("agent_completed_helper_changed")
            return _binding_from_rows(execution, snapshot)
        finally:
            db.row_factory = previous_factory

    async def mark_running(self, execution_id: str) -> None:
        await self._transition(execution_id, to_status="running")

    async def read_turn_binding_detail(
        self,
        *,
        team_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        """Return the credential-free binding ledger for a completed or live Turn."""

        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT requested_agent_id AS agent_id,agent_source_kind,
                       revision_ref AS agent_revision_id,
                       release_id AS agent_release_id,binding_source,
                       capability_fingerprint,created_at
                FROM turn_agent_binding
                WHERE team_id=? AND turn_id=?
                """,
                (team_id, turn_id),
            ) as cursor:
                root = await cursor.fetchone()
            async with db.execute(
                """
                SELECT execution.id AS agent_execution_id,
                       execution.parent_execution_id,
                       execution.agent_id,execution.agent_source_kind,
                       execution.revision_ref AS agent_revision_id,
                       execution.release_id AS agent_release_id,
                       execution.dispatch_source,execution.dispatch_key,
                       execution.runtime_profile_name,
                       execution.capability_fingerprint,execution.status,
                       execution.created_at,execution.started_at,
                       execution.ended_at,execution.failure_code,
                       COALESCE(
                         json_extract(team_revision.listing_json,'$.display_name'),
                         json_extract(system_revision.definition_json,'$.display_name'),
                         execution.runtime_profile_name
                       ) AS display_name_snapshot
                FROM turn_agent_execution execution
                LEFT JOIN team_subagent_revision team_revision
                  ON execution.agent_source_kind='team'
                 AND team_revision.team_id=execution.team_id
                 AND team_revision.subagent_id=execution.agent_id
                 AND team_revision.id=execution.revision_ref
                LEFT JOIN system_agent_effective_revision system_revision
                  ON execution.agent_source_kind='system'
                 AND system_revision.team_id=execution.team_id
                 AND system_revision.agent_id=execution.agent_id
                 AND system_revision.id=execution.revision_ref
                WHERE execution.team_id=? AND execution.turn_id=?
                ORDER BY execution.created_at,execution.id
                """,
                (team_id, turn_id),
            ) as cursor:
                executions = await cursor.fetchall()
        return {
            "turn_id": turn_id,
            "root_binding": dict(root) if root is not None else None,
            "executions": [dict(row) for row in executions],
        }

    async def mark_terminal(
        self,
        execution_id: str,
        *,
        status: Literal["completed", "failed", "blocked", "cancelled"],
        failure_code: str | None = None,
    ) -> None:
        if status not in {"failed", "blocked"}:
            failure_code = None
        await self._transition(
            execution_id,
            to_status=status,
            failure_code=failure_code,
        )

    async def _transition(
        self,
        execution_id: str,
        *,
        to_status: AgentExecutionStatus,
        failure_code: str | None = None,
    ) -> None:
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            try:
                if to_status == "running":
                    updated = await db.execute(
                        """
                        UPDATE turn_agent_execution
                        SET status='running',started_at=COALESCE(started_at,?)
                        WHERE id=? AND status='admitted'
                        """,
                        (timestamp, execution_id),
                    )
                else:
                    if to_status == "completed":
                        updated = await db.execute(
                            """
                            UPDATE turn_agent_execution
                            SET status='completed',ended_at=COALESCE(ended_at,?),failure_code=NULL
                            WHERE id=? AND status='running'
                            """,
                            (timestamp, execution_id),
                        )
                    else:
                        updated = await db.execute(
                            """
                            UPDATE turn_agent_execution
                            SET status=?,ended_at=COALESCE(ended_at,?),failure_code=?
                            WHERE id=? AND status IN ('admitted','running')
                            """,
                            (to_status, timestamp, failure_code, execution_id),
                        )
                if updated.rowcount == 0:
                    async with db.execute(
                        "SELECT status FROM turn_agent_execution WHERE id=?",
                        (execution_id,),
                    ) as cursor:
                        row = await cursor.fetchone()
                    if row is None or str(row[0]) != to_status:
                        raise AgentExecutionBindingError(
                            "agent_execution_transition_conflict",
                        )
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                if "active Capability executions" in str(exc):
                    raise AgentExecutionBindingError(
                        "agent_capability_execution_active",
                    ) from exc
                if "unresolved collaboration obligations" in str(exc):
                    raise AgentExecutionBindingError("agent_collaboration_unresolved") from exc
                raise

    @staticmethod
    async def insert_root_binding_in_connection(
        db: aiosqlite.Connection,
        *,
        binding: ExactAgentExecutionBinding,
        user_id: str,
        assignment_id: str | None,
        binding_source: Literal["marketplace", "detail", "hr", "mention", "schedule", "plan", "lead"],
        bind_primary_execution: bool = False,
    ) -> None:
        """Write a root binding inside the caller's Turn admission transaction."""
        extra_column = ",primary_execution_id" if bind_primary_execution else ""
        extra_value = ",?" if bind_primary_execution else ""
        await db.execute(
            f"""
            INSERT INTO turn_agent_binding(
              turn_id,team_id,user_id,assignment_id,requested_agent_id,
              agent_source_kind,revision_ref,release_id,binding_source,
              capability_fingerprint,policy_fingerprint,created_at{extra_column}
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?{extra_value})
            """,  # noqa: S608 -- fixed server-owned column/placeholder fragments.
            (
                binding.turn_id,
                binding.team_id,
                user_id,
                assignment_id,
                binding.agent_id,
                binding.agent_source_kind,
                binding.revision_ref,
                binding.release_id,
                binding_source,
                binding.capability_fingerprint,
                binding.policy_fingerprint,
                _now(),
                *((binding.execution_id,) if bind_primary_execution else ()),
            ),
        )


async def _require_turn_scope(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    user_id: str,
    turn_id: str,
) -> None:
    async with db.execute(
        """
        SELECT 1
        FROM turns turn_row
        JOIN sessions session_row ON session_row.id=turn_row.session_id
        JOIN workspaces workspace_row
          ON workspace_row.id=session_row.workspace_id AND workspace_row.status='active'
        JOIN users actor ON actor.id=session_row.user_id AND actor.status='active'
        JOIN workspace_members space_member
          ON space_member.workspace_id=workspace_row.id AND space_member.user_id=actor.id
         AND space_member.status='active'
        JOIN team_members member
          ON member.team_id=workspace_row.team_id AND member.user_id=actor.id
         AND member.status='active'
        JOIN teams team ON team.id=workspace_row.team_id AND team.status='active'
        WHERE turn_row.id=? AND turn_row.status='running'
          AND session_row.user_id=? AND workspace_row.team_id=?
        """,
        (turn_id, user_id, team_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentExecutionBindingError("agent_turn_scope_invalid")


async def _require_eval_scratch_turn(
    db: aiosqlite.Connection,
    *,
    eval_run_id: str,
    turn_id: str,
) -> None:
    async with db.execute(
        """
        SELECT 1
        FROM agent_eval_scratch_turn scratch
        JOIN agent_eval_case_job job
          ON job.id=scratch.job_id
         AND job.run_id=scratch.run_id
         AND job.case_revision_id=scratch.case_revision_id
         AND job.attempt=scratch.attempt
         AND job.lease_generation=scratch.lease_generation
        JOIN agent_eval_run_execution execution ON execution.run_id=scratch.run_id
        JOIN turns turn_row ON turn_row.id=scratch.turn_id
        JOIN sessions session_row
          ON session_row.id=scratch.session_id
         AND session_row.id=turn_row.session_id
         AND session_row.workspace_id=execution.workspace_id
         AND session_row.origin='agent_eval'
        WHERE scratch.run_id=? AND scratch.turn_id=?
          AND job.status='running' AND job.lease_expires_at>?
          AND execution.cancel_requested_at IS NULL
        """,
        (eval_run_id, turn_id, _now()),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentExecutionBindingError("agent_eval_turn_scope_invalid")


async def _team_agent_row(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    profile_name: str,
    agent_id: str | None,
) -> aiosqlite.Row:
    where = "team_id=? AND id=? AND name=?" if agent_id else "team_id=? AND name=?"
    params = (team_id, agent_id, profile_name) if agent_id else (team_id, profile_name)
    async with db.execute(
        f"""
        SELECT * FROM team_subagent
        WHERE {where} AND {team_agent_dispatchable_state_sql()}
        """,  # noqa: S608 -- fixed local fragments only
        params,
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentExecutionBindingError("agent_not_runnable")
    return row


async def dispatchable_team_agent_ids(
    db_path: str | Path,
    *,
    team_id: str,
) -> frozenset[str]:
    """Team Agent ids whose durable definition would pass execution admission.

    Judgement 6.  Advertising surfaces must intersect their roster with this set.

    Access, assignment and Audience are deliberately NOT re-implemented here -- the
    roster already resolves those, and duplicating them would recreate the very
    divergence this function exists to close.
    """
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""
            SELECT agent.id AS agent_id
            FROM team_subagent agent
            JOIN team_subagent_release release
              ON release.team_id=agent.team_id AND release.subagent_id=agent.id
             AND release.id=agent.live_release_id
             AND release.revision_id=agent.live_revision_id
            JOIN team_subagent_revision revision
              ON revision.team_id=release.team_id
             AND revision.subagent_id=release.subagent_id
             AND revision.id=release.revision_id
             AND revision.capability_fingerprint=release.capability_fingerprint
             AND revision.dependency_fingerprint=release.dependency_fingerprint
            JOIN agent_safety_report safety
              ON safety.team_id=release.team_id
             AND safety.agent_id=release.subagent_id
             AND safety.id=release.safety_report_id
             AND safety.subject_kind='revision'
             AND safety.subject_ref=revision.id
             AND safety.dependency_fingerprint=revision.dependency_fingerprint
            WHERE agent.team_id=? AND {team_agent_dispatchable_state_sql("agent")}
              AND safety.status NOT IN ('blocked','unknown')
            """,  # noqa: S608 -- fixed local fragment only
            (team_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return frozenset(str(row["agent_id"]) for row in rows)


async def team_agent_dispatch_state(
    db_path: str | Path,
    *,
    team_id: str,
    agent_id: str,
) -> str | None:
    """Durable state deciding compatibility vs exact binding for one Agent.

    ``"revisioned"`` -- it carries an immutable revision, so bind it exactly even
    when the deployment marker is still unset.  ``apply`` writes this column per
    batch and only sets ``release_semantics_activated_at`` after the whole
    inventory reconciles, so an interrupted import leaves revisioned Agents on an
    un-marked deployment; running those through the compatibility path would
    execute the mutable row instead of the frozen revision.

    ``"legacy"`` -- no immutable revision yet, so runtime dispatch refuses it.

    ``None`` -- the row is not dispatchable at all (disabled, archived or
    quarantined).  The routing-name lookup only filters ``status``, so without
    this an archived Agent could still reach the compatibility path.
    """
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT definition_mode FROM team_subagent
            WHERE team_id=? AND id=? AND status='enabled' AND lifecycle_state='active'
            """,
            (team_id, agent_id),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return "revisioned" if str(row["definition_mode"]) == "revisioned" else "legacy"


async def _require_assignment(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    user_id: str,
    agent_id: str,
    source_kind: AgentSourceKind,
) -> aiosqlite.Row:
    async with db.execute(
        """
        SELECT * FROM agent_assignment
        WHERE team_id=? AND user_id=? AND agent_id=? AND agent_source_kind=?
          AND personal_state='active' AND access_state='granted'
        """,
        (team_id, user_id, agent_id, source_kind),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentExecutionBindingError("agent_assignment_inactive")
    return row


async def _require_execution_assignment(
    db: aiosqlite.Connection,
    execution: Mapping[str, Any],
    *,
    user_id: str,
) -> aiosqlite.Row:
    assignment = await _require_assignment(
        db,
        team_id=str(execution["team_id"]),
        user_id=user_id,
        agent_id=str(execution["agent_id"]),
        source_kind=execution["agent_source_kind"],
    )
    # Rehire reuses the assignment ID but cannot revive a previous hiring cycle.
    try:
        cycle = datetime.fromisoformat(str(assignment["current_cycle_started_at"]).replace("Z", "+00:00"))
        admitted = datetime.fromisoformat(str(execution["created_at"]).replace("Z", "+00:00"))
        valid_cycle = cycle.tzinfo is not None and admitted.tzinfo is not None and cycle <= admitted
    except ValueError:
        valid_cycle = False
    if not valid_cycle:
        raise AgentExecutionBindingError("agent_assignment_cycle_changed")
    return assignment


async def _authorized_system_execution_snapshot(
    db: aiosqlite.Connection,
    execution: Mapping[str, Any],
    *,
    user_id: str,
    system_profile_resolver: Callable[..., Any] | None = None,
) -> aiosqlite.Row:
    """Share current system authority without changing entry-point purpose guards.

    Ordinary output continuation additionally compares today's effective profile.
    Reference retains its existing pinned-definition semantics.
    """
    team_id, profile_name = str(execution["team_id"]), str(execution["runtime_profile_name"])
    async with db.execute(
        "SELECT * FROM team_builtin_subagent_override WHERE team_id=? AND name=?",
        (team_id, profile_name),
    ) as cursor:
        override = await cursor.fetchone()
    override_version = int(override["row_version"]) if override is not None else None
    await _require_current_system_override(db, team_id=team_id, profile_name=profile_name, expected_version=override_version)
    system_required = execution["policy_fingerprint"] == _system_policy_fingerprint(
        None,
        override_version,
        system_required=True,
        enforce_current_override=True,
    )
    if system_required:
        from bowhead_core.subagent import RETIRED_SUBAGENT_NAMES
        from bowhead_core.system_agent_marketplace import system_manifest_names

        # Unlisting the Agent from the marketplace doesn't stop an admitted
        # execution, and neither does a profile check here: only a profile that
        # is no longer a system manifest stops it. A Team override still does:
        # ``_require_current_system_override`` above stops the execution when the
        # override is turned off (enabled=0) or its row_version changed.
        if profile_name not in system_manifest_names() or profile_name in RETIRED_SUBAGENT_NAMES:
            raise AgentExecutionBindingError("agent_not_runnable")
    assignment = None if system_required else await _require_execution_assignment(db, execution, user_id=user_id)
    if (
        _system_policy_fingerprint(
            assignment,
            override_version,
            system_required=system_required,
            enforce_current_override=True,
        )
        != execution["policy_fingerprint"]
    ):
        raise AgentExecutionBindingError("agent_system_definition_changed")
    snapshot = await _system_snapshot_by_execution(db, execution)
    _validate_system_snapshot(snapshot)
    if _behavior_descriptor_from_snapshot(snapshot) is None:
        raise AgentExecutionBindingError("agent_behavior_descriptor_unavailable")
    definition = _decode_object(snapshot["definition_json"], "system Agent definition")
    projected = _system_revision_projection_base(team_id=team_id, profile_name=profile_name, definition=definition)
    if projected[3] != execution["revision_ref"] or projected[4] != execution["capability_fingerprint"]:
        raise AgentExecutionBindingError("agent_bound_revision_corrupt")
    if system_profile_resolver is not None:
        from bowhead_core.builtin_subagent_override_store import _row_to_override
        from bowhead_core.subagent import _system_profile_snapshot

        known, current = system_profile_resolver(
            profile_name,
            team_id=team_id,
            builtin_overrides={profile_name: _row_to_override(override)} if override is not None else {},
        )
        if not known or current is None:
            raise AgentExecutionBindingError("agent_not_runnable")
        if _system_profile_snapshot(current) != definition:
            raise AgentExecutionBindingError("agent_completed_helper_changed")
    return snapshot


async def _require_current_system_override(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    profile_name: str,
    expected_version: int | None,
) -> None:
    async with db.execute(
        """
        SELECT enabled,row_version FROM team_builtin_subagent_override
        WHERE team_id=? AND name=?
        """,
        (team_id, profile_name),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        if expected_version is not None:
            raise AgentExecutionBindingError("agent_system_definition_changed")
        return
    if not bool(row["enabled"]):
        raise AgentExecutionBindingError("agent_not_runnable")
    if expected_version is None or int(row["row_version"]) != expected_version:
        raise AgentExecutionBindingError("agent_system_definition_changed")


async def _require_team_extension_snapshot(db: aiosqlite.Connection, snapshot: Mapping[str, Any]) -> None:
    """Historical opaque policies never become permission by a schema upgrade."""
    from bowhead_core.agent_runtime_extension_store import read_published_extension_policy_in_connection
    from bowhead_core.agent_runtime_extensions import AgentRuntimeExtensionError, read_agent_runtime_extensions

    try:
        definition = json.loads(str(snapshot["definition_json"]))
        if not isinstance(definition, dict):
            raise ValueError()
        if read_agent_runtime_extensions(definition) is None:
            return
        async with db.execute(
            "SELECT team_id,subagent_id FROM team_subagent_revision WHERE id=? AND definition_hash=?",
            (snapshot["revision_id"], snapshot["definition_hash"]),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentRuntimeExtensionError("agent_runtime_extension_unproven")
        await read_published_extension_policy_in_connection(
            db, team_id=str(row[0]), agent_id=str(row[1]), revision_ref=str(snapshot["revision_id"]), definition=definition
        )
    except (AgentRuntimeExtensionError, ValueError, TypeError):
        raise AgentExecutionBindingError("agent_runtime_extension_unproven") from None


async def _authorized_current_team_snapshot(
    db: aiosqlite.Connection, *, db_path: str, team_id: str, user_id: str, aggregate: Mapping[str, Any]
) -> tuple[str, aiosqlite.Row]:
    decisions = await TeamSubagentAccessResolver(db_path).resolve_many_in_connection(
        db, team_id=team_id, actor_user_id=user_id, subagent_id=str(aggregate["id"]), now=datetime.now(UTC)
    )
    access = decisions.get(str(aggregate["id"]))
    if access is None or not access.resolution.can_consume:
        raise AgentExecutionBindingError("agent_access_denied")
    snapshot = await _current_team_snapshot(db, aggregate)
    _validate_team_snapshot(snapshot)
    return access.resolution.access_reason, snapshot


async def _current_team_snapshot(
    db: aiosqlite.Connection,
    aggregate: Mapping[str, Any],
) -> aiosqlite.Row:
    if not aggregate["live_release_id"] or not aggregate["live_revision_id"]:
        raise AgentExecutionBindingError("agent_live_release_missing")
    async with db.execute(
        """
        SELECT release.id AS release_id,release.revision_id,
               release.capability_fingerprint,release.dependency_fingerprint,
               revision.definition_json,revision.listing_json,
               revision.definition_hash,revision.risk_json,
               behavior.descriptor_json AS behavior_descriptor_json,
               safety.status AS safety_status,safety.content_hash AS safety_content_hash
        FROM team_subagent_release release
        JOIN team_subagent_revision revision
          ON revision.team_id=release.team_id
         AND revision.subagent_id=release.subagent_id
         AND revision.id=release.revision_id
        LEFT JOIN team_subagent_revision_behavior behavior
          ON behavior.team_id=revision.team_id
         AND behavior.subagent_id=revision.subagent_id
         AND behavior.revision_id=revision.id
        JOIN agent_safety_report safety
          ON safety.team_id=release.team_id
         AND safety.agent_id=release.subagent_id
         AND safety.id=release.safety_report_id
         AND safety.subject_kind='revision'
         AND safety.subject_ref=revision.id
         AND safety.dependency_fingerprint=revision.dependency_fingerprint
        WHERE release.team_id=? AND release.subagent_id=? AND release.id=?
          AND release.revision_id=?
          AND release.capability_fingerprint=revision.capability_fingerprint
          AND release.dependency_fingerprint=revision.dependency_fingerprint
        """,
        (
            aggregate["team_id"],
            aggregate["id"],
            aggregate["live_release_id"],
            aggregate["live_revision_id"],
        ),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentExecutionBindingError("agent_live_release_inconsistent")
    await _require_team_extension_snapshot(db, row)
    return row


async def _eval_team_snapshot(
    db: aiosqlite.Connection,
    run: Mapping[str, Any],
) -> aiosqlite.Row:
    """Load the run-pinned release or frozen candidate with current Safety evidence."""

    try:
        candidate_id = run["candidate_id"]
    except (IndexError, KeyError):
        candidate_id = None
    if candidate_id is not None:
        async with db.execute(
            """
            SELECT NULL AS release_id,candidate.revision_id,
                   candidate.capability_fingerprint,candidate.dependency_fingerprint,
                   revision.definition_json,revision.listing_json,
                   revision.definition_hash,revision.risk_json,
                   behavior.descriptor_json AS behavior_descriptor_json,
                   safety.status AS safety_status,safety.content_hash AS safety_content_hash,agent.name AS runtime_profile_name
            FROM team_subagent_release_candidate candidate
            JOIN team_subagent_revision revision
              ON revision.team_id=candidate.team_id
             AND revision.subagent_id=candidate.subagent_id
             AND revision.id=candidate.revision_id
            LEFT JOIN team_subagent_revision_behavior behavior
              ON behavior.team_id=revision.team_id
             AND behavior.subagent_id=revision.subagent_id
             AND behavior.revision_id=revision.id
            JOIN team_subagent agent
              ON agent.team_id=candidate.team_id AND agent.id=candidate.subagent_id
            JOIN agent_safety_report safety
              ON safety.team_id=candidate.team_id
             AND safety.agent_id=candidate.subagent_id
             AND safety.id=candidate.safety_report_id
             AND safety.subject_kind='revision'
             AND safety.subject_ref=revision.id
             AND safety.dependency_fingerprint=revision.dependency_fingerprint
            WHERE candidate.team_id=? AND candidate.subagent_id=?
              -- `prepared` is where the Owner comparison runs. 0073 widened the matching run-scope
              -- and turn-execution triggers to it; this lookup was missed, which refused every
              -- comparison attempt before the Agent ran.
              AND candidate.id=? AND candidate.state IN ('prepared','pending','approved')
              AND candidate.revision_id=?
              AND candidate.capability_fingerprint=?
              AND candidate.dependency_fingerprint=?
              AND revision.capability_fingerprint=candidate.capability_fingerprint
              AND revision.dependency_fingerprint=candidate.dependency_fingerprint
            """,
            (
                run["team_id"],
                run["agent_id"],
                candidate_id,
                run["revision_ref"],
                run["capability_fingerprint"],
                run["dependency_fingerprint"],
            ),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentExecutionBindingError("agent_eval_bound_candidate_unavailable")
        await _require_team_extension_snapshot(db, row)
        return row

    async with db.execute(
        """
        SELECT release.id AS release_id,release.revision_id,
               release.capability_fingerprint,release.dependency_fingerprint,
               revision.definition_json,revision.listing_json,
               revision.definition_hash,revision.risk_json,
               behavior.descriptor_json AS behavior_descriptor_json,
               safety.status AS safety_status,safety.content_hash AS safety_content_hash,agent.name AS runtime_profile_name
        FROM team_subagent_release release
        JOIN team_subagent_revision revision
          ON revision.team_id=release.team_id
         AND revision.subagent_id=release.subagent_id
         AND revision.id=release.revision_id
        LEFT JOIN team_subagent_revision_behavior behavior
          ON behavior.team_id=revision.team_id
         AND behavior.subagent_id=revision.subagent_id
         AND behavior.revision_id=revision.id
        JOIN team_subagent agent
          ON agent.team_id=release.team_id AND agent.id=release.subagent_id
        JOIN agent_safety_report safety
          ON safety.team_id=release.team_id
         AND safety.agent_id=release.subagent_id
         AND safety.subject_kind='revision'
         AND safety.subject_ref=revision.id
         AND safety.dependency_fingerprint=revision.dependency_fingerprint
        WHERE release.team_id=? AND release.subagent_id=?
          AND release.id=? AND release.revision_id=?
          AND release.capability_fingerprint=?
          AND release.dependency_fingerprint=?
          AND revision.capability_fingerprint=release.capability_fingerprint
          AND revision.dependency_fingerprint=release.dependency_fingerprint
          AND safety.id=(
            SELECT latest.id FROM agent_safety_report latest
            WHERE latest.team_id=release.team_id
              AND latest.agent_id=release.subagent_id
              AND latest.subject_kind='revision'
              AND latest.subject_ref=revision.id
              AND latest.dependency_fingerprint=revision.dependency_fingerprint
            ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1
          )
        """,
        (
            run["team_id"],
            run["agent_id"],
            run["release_id"],
            run["revision_ref"],
            run["capability_fingerprint"],
            run["dependency_fingerprint"],
        ),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentExecutionBindingError("agent_eval_bound_revision_unavailable")
    await _require_team_extension_snapshot(db, row)
    return row


async def _eval_system_snapshot(
    db: aiosqlite.Connection,
    run: Mapping[str, Any],
) -> aiosqlite.Row:
    async with db.execute(
        """
        SELECT revision.*,
               behavior.descriptor_json AS behavior_descriptor_json
        FROM system_agent_effective_revision revision
        LEFT JOIN system_agent_revision_behavior behavior
          ON behavior.team_id=revision.team_id
         AND behavior.agent_id=revision.agent_id
         AND behavior.revision_id=revision.id
        WHERE revision.team_id=? AND revision.agent_id=? AND revision.id=?
        """,
        (run["team_id"], run["agent_id"], run["revision_ref"]),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentExecutionBindingError("agent_eval_bound_revision_unavailable")
    return row


async def _team_snapshot_by_execution(
    db: aiosqlite.Connection,
    execution: Mapping[str, Any],
) -> aiosqlite.Row:
    async with db.execute(
        """
        SELECT release.id AS release_id,release.revision_id,
               release.capability_fingerprint,release.dependency_fingerprint,
               revision.definition_json,revision.listing_json,
               revision.definition_hash,revision.risk_json,
               behavior.descriptor_json AS behavior_descriptor_json,
               safety.status AS safety_status,safety.content_hash AS safety_content_hash
        FROM team_subagent_release release
        JOIN team_subagent_revision revision
          ON revision.team_id=release.team_id
         AND revision.subagent_id=release.subagent_id
         AND revision.id=release.revision_id
        LEFT JOIN team_subagent_revision_behavior behavior
          ON behavior.team_id=revision.team_id
         AND behavior.subagent_id=revision.subagent_id
         AND behavior.revision_id=revision.id
        JOIN agent_safety_report safety
          ON safety.team_id=release.team_id
         AND safety.agent_id=release.subagent_id
         AND safety.id=release.safety_report_id
         AND safety.subject_kind='revision'
         AND safety.subject_ref=revision.id
         AND safety.dependency_fingerprint=revision.dependency_fingerprint
        WHERE release.team_id=? AND release.subagent_id=?
          AND release.id=? AND release.revision_id=?
          AND release.capability_fingerprint=?
          AND release.capability_fingerprint=revision.capability_fingerprint
          AND release.dependency_fingerprint=revision.dependency_fingerprint
        """,
        (
            execution["team_id"],
            execution["agent_id"],
            execution["release_id"],
            execution["revision_ref"],
            execution["capability_fingerprint"],
        ),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentExecutionBindingError("agent_bound_revision_unavailable")
    await _require_team_extension_snapshot(db, row)
    return row


async def _scheduled_team_pin(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    user_id: str,
    turn_id: str,
    job_id: str,
    binding_row_version: int,
) -> aiosqlite.Row:
    async with db.execute(
        """
        SELECT binding.*
        FROM scheduled_job_agent_binding binding
        JOIN scheduled_jobs job ON job.id=binding.job_id
        JOIN workspaces job_workspace
          ON job_workspace.id=job.workspace_id
         AND job_workspace.team_id=? AND job_workspace.status='active'
        JOIN turns turn_row ON turn_row.id=? AND turn_row.status='running'
        JOIN sessions session_row
          ON session_row.id=turn_row.session_id
         AND session_row.workspace_id=job.workspace_id
         AND session_row.user_id=job.user_id
        WHERE binding.job_id=? AND binding.row_version=?
          AND binding.state='ready' AND binding.follow_mode='pinned'
          AND binding.agent_source_kind='team'
          AND job.user_id=?
        """,
        (team_id, turn_id, job_id, binding_row_version, user_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentExecutionBindingError("agent_schedule_binding_stale")
    return row


async def _team_snapshot_by_pin(
    db: aiosqlite.Connection,
    pin: Mapping[str, Any],
) -> aiosqlite.Row:
    async with db.execute(
        """
        SELECT release.id AS release_id,release.revision_id,
               release.capability_fingerprint,release.dependency_fingerprint,
               revision.definition_json,revision.listing_json,
               revision.definition_hash,revision.risk_json,
               behavior.descriptor_json AS behavior_descriptor_json,
               safety.status AS safety_status,safety.content_hash AS safety_content_hash
        FROM scheduled_job_agent_binding binding
        JOIN scheduled_jobs job ON job.id=binding.job_id
        JOIN workspaces workspace ON workspace.id=job.workspace_id
        JOIN team_subagent_release release
          ON release.team_id=workspace.team_id
         AND release.subagent_id=binding.agent_id
         AND release.id=binding.release_id
         AND release.revision_id=binding.revision_ref
         AND release.capability_fingerprint=binding.capability_fingerprint
         AND release.dependency_fingerprint=binding.dependency_fingerprint
        JOIN team_subagent_revision revision
          ON revision.team_id=release.team_id
         AND revision.subagent_id=release.subagent_id
         AND revision.id=release.revision_id
         AND revision.capability_fingerprint=release.capability_fingerprint
         AND revision.dependency_fingerprint=release.dependency_fingerprint
        LEFT JOIN team_subagent_revision_behavior behavior
          ON behavior.team_id=revision.team_id
         AND behavior.subagent_id=revision.subagent_id
         AND behavior.revision_id=revision.id
        JOIN agent_safety_report safety
          ON safety.team_id=release.team_id
         AND safety.agent_id=release.subagent_id
         AND safety.id=release.safety_report_id
         AND safety.subject_kind='revision'
         AND safety.subject_ref=revision.id
         AND safety.dependency_fingerprint=revision.dependency_fingerprint
        WHERE binding.job_id=? AND binding.agent_id=?
          AND binding.revision_ref=? AND binding.release_id=?
        """,
        (
            pin["job_id"],
            pin["agent_id"],
            pin["revision_ref"],
            pin["release_id"],
        ),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentExecutionBindingError("agent_bound_revision_unavailable")
    await _require_team_extension_snapshot(db, row)
    return row


async def _system_snapshot_by_execution(
    db: aiosqlite.Connection,
    execution: Mapping[str, Any],
) -> aiosqlite.Row:
    async with db.execute(
        """
        SELECT revision.*,
               behavior.descriptor_json AS behavior_descriptor_json
        FROM system_agent_effective_revision revision
        LEFT JOIN system_agent_revision_behavior behavior
          ON behavior.team_id=revision.team_id
         AND behavior.agent_id=revision.agent_id
         AND behavior.revision_id=revision.id
        WHERE revision.team_id=? AND revision.agent_id=? AND revision.id=?
        """,
        (
            execution["team_id"],
            execution["agent_id"],
            execution["revision_ref"],
        ),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentExecutionBindingError("agent_bound_revision_unavailable")
    return row


def _validate_team_snapshot(snapshot: Mapping[str, Any]) -> None:
    if str(snapshot["safety_status"]) in {"blocked", "unknown"}:
        raise AgentExecutionBindingError("agent_safety_blocked")
    if _hash_text(str(snapshot["definition_json"])) != str(
        snapshot["definition_hash"],
    ):
        raise AgentExecutionBindingError("agent_bound_revision_corrupt")
    definition = _decode_object(snapshot["definition_json"], "Agent definition")
    listing = _decode_object(snapshot["listing_json"], "Agent listing")
    # "This safety report is about exactly this content" used to be a SQL join
    # condition comparing safety.content_hash to revision.definition_hash -- two
    # digests over different inputs, so it could never hold for anything a writer
    # actually produced and no team Agent could ever bind.  The check itself is
    # worth keeping, so it moves here where the writer's formula
    # (hash of {definition, listing}) can be reproduced.
    expected_content_hash = _hash_text(_canonical_json({"definition": definition, "listing": listing}))
    if str(snapshot["safety_content_hash"]) != expected_content_hash:
        raise AgentExecutionBindingError("agent_live_release_inconsistent")
    prompt = definition.get("system_prompt", "")
    if not isinstance(prompt, str):
        raise AgentExecutionBindingError("agent_definition_invalid")
    from bowhead_core.agents.exceptions import ManifestSecurityError
    from bowhead_core.agents.manifest_security import lint_user_append

    try:
        lint_user_append(prompt)
    except ManifestSecurityError as exc:
        raise AgentExecutionBindingError("agent_prompt_blocked") from exc
    listing_result = scan_agent_listing(
        name=listing.get("name", ""),
        display_name=listing.get("display_name", ""),
        description=listing.get("description", listing.get("summary", "")),
    )
    if listing_result.blocked:
        raise AgentExecutionBindingError("agent_listing_blocked")


def _validate_system_snapshot(snapshot: Mapping[str, Any]) -> None:
    if _hash_text(str(snapshot["definition_json"])) != str(
        snapshot["definition_hash"],
    ):
        raise AgentExecutionBindingError("agent_bound_revision_corrupt")
    _decode_object(snapshot["definition_json"], "system Agent definition")


async def _execution_by_dispatch_key(
    db: aiosqlite.Connection,
    *,
    turn_id: str,
    dispatch_source: AgentDispatchSource,
    dispatch_key: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        """
        SELECT * FROM turn_agent_execution
        WHERE turn_id=? AND dispatch_source=? AND dispatch_key=?
        """,
        (turn_id, dispatch_source, dispatch_key),
    ) as cursor:
        return await cursor.fetchone()


async def _execution_by_id(
    db: aiosqlite.Connection,
    execution_id: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM turn_agent_execution WHERE id=?",
        (execution_id,),
    ) as cursor:
        return await cursor.fetchone()


async def _resolved_team_definition_json(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    snapshot: Mapping[str, Any],
) -> str | None:
    from bowhead_core.system_plugins import BuiltinPluginSelectionError, expand_builtin_plugins

    definition = _decode_object(snapshot["definition_json"], "Agent definition")
    builtin_selected = bool(definition.get("builtin_plugin_ids"))
    try:
        team_selected = bool(selected_plugin_ids(definition))
        if not team_selected and not builtin_selected:
            return None
        resolved = dict(definition)
        if team_selected:
            resolved = await expand_plugins_conn(db, team_id=team_id, definition=definition, ignore_deleted=True, allow_inactive=True)
        if builtin_selected:
            resolved = expand_builtin_plugins(resolved, strict=False)
    except TeamPluginError as exc:
        raise AgentExecutionBindingError(exc.code) from exc
    except BuiltinPluginSelectionError as exc:
        raise AgentExecutionBindingError(exc.code) from exc
    return _canonical_json(resolved)


def _binding_from_rows(
    execution: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    listing: dict[str, Any] | None = None,
) -> ExactAgentExecutionBinding:
    source_kind = str(execution["agent_source_kind"])
    if source_kind == "team":
        # resolved_definition_json was added in 0123; old-schema rows lack it. Fall back to the snapshot definition
        # so admission reaches its *_migration_required checks instead of raising IndexError here.
        # (sqlite Row `in` compares values, not column names; call keys() first.)
        columns = execution.keys()
        resolved = execution["resolved_definition_json"] if "resolved_definition_json" in columns else None
        definition = _decode_object(resolved if resolved is not None else snapshot["definition_json"], "Agent definition")
        authored = _decode_object(snapshot["definition_json"], "Agent definition") if resolved is not None else None
        listing_value = _decode_object(snapshot["listing_json"], "Agent listing")
    else:
        authored = None
        definition = _decode_object(snapshot["definition_json"], "system Agent definition")
        listing_value = listing or {
            "display_name": str(definition.get("display_name") or execution["runtime_profile_name"]),
            "description": str(definition.get("description") or ""),
            "name": str(execution["runtime_profile_name"]),
        }
    return ExactAgentExecutionBinding(
        execution_id=str(execution["id"]),
        team_id=str(execution["team_id"]),
        turn_id=str(execution["turn_id"]),
        agent_id=str(execution["agent_id"]),
        agent_source_kind=source_kind,  # type: ignore[arg-type]
        revision_ref=str(execution["revision_ref"]),
        release_id=(str(execution["release_id"]) if execution["release_id"] else None),
        runtime_profile_name=str(execution["runtime_profile_name"]),
        capability_fingerprint=str(execution["capability_fingerprint"]),
        policy_fingerprint=str(execution["policy_fingerprint"]),
        definition=definition,
        listing=listing_value,
        status=str(execution["status"]),  # type: ignore[arg-type]
        behavior_descriptor=_behavior_descriptor_from_snapshot(snapshot),
        authored_definition=authored,
    )


def _behavior_descriptor_from_snapshot(
    snapshot: Mapping[str, Any],
) -> AgentBehaviorDescriptor | None:
    keys = snapshot.keys()
    if "behavior_descriptor_json" not in keys:
        return None
    raw = snapshot["behavior_descriptor_json"]
    if raw is None:
        return None
    try:
        descriptor = AgentBehaviorDescriptor.model_validate_json(str(raw))
    except ValueError as exc:
        if agent_behavior_m3_enabled():
            raise AgentExecutionBindingError("agent_behavior_descriptor_invalid") from exc
        return None
    if descriptor.source_definition_hash != str(snapshot["definition_hash"]):
        if agent_behavior_m3_enabled():
            raise AgentExecutionBindingError("agent_behavior_descriptor_invalid")
        return None
    return descriptor


def _require_expected_candidate(
    expected: ExactAgentBindingExpectation | None,
    *,
    agent_id: str,
    assignment_id: str | None,
    revision_ref: str,
    release_id: str | None,
    snapshot: Mapping[str, Any],
) -> None:
    if expected is None:
        return
    descriptor = _behavior_descriptor_from_snapshot(snapshot)
    if (
        descriptor is None
        or expected.agent_id != agent_id
        or expected.assignment_id != assignment_id
        or expected.revision_ref != revision_ref
        or expected.release_id != release_id
        or expected.behavior_fingerprint != descriptor.behavior_fingerprint
    ):
        raise AgentExecutionBindingError("agent_candidate_stale")


def _require_resumable_execution(execution: Mapping[str, Any]) -> None:
    if str(execution["status"]) not in {"admitted", "running"}:
        raise AgentExecutionBindingError("agent_execution_already_terminal")


def _decode_object(value: object, name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentExecutionBindingError("agent_snapshot_malformed") from exc
    if not isinstance(parsed, dict):
        raise AgentExecutionBindingError("agent_snapshot_malformed")
    return parsed


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise AgentExecutionBindingError("agent_snapshot_malformed") from exc


def _hash_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _system_policy_fingerprint(
    assignment: Mapping[str, Any] | None, override_version: int | None, *, system_required: bool, enforce_current_override: bool
) -> str:
    return _policy_fingerprint(
        {
            "assignment_id": str(assignment["id"]) if assignment is not None else None,
            "assignment_row_version": int(assignment["row_version"]) if assignment is not None else None,
            "override_version": override_version,
            "source": "system",
            "system_required": system_required,
            "override_enforced": enforce_current_override,
        }
    )


def _policy_fingerprint(value: Mapping[str, Any]) -> str:
    return _hash_text(_canonical_json(value))


def _system_revision_projection_base(
    *,
    team_id: str,
    profile_name: str,
    definition: Mapping[str, Any],
) -> tuple[str, dict[str, Any], str, str, str]:
    agent_id = f"system:{profile_name}"
    definition_json = _canonical_json(definition)
    definition_value = _decode_object(definition_json, "system Agent definition")
    definition_hash = _hash_text(definition_json)
    revision_ref = _system_revision_id(
        team_id=team_id,
        agent_id=agent_id,
        definition_hash=definition_hash,
    )
    capability_fingerprint = _policy_fingerprint(
        {
            "definition_hash": definition_hash,
            "kind": "legacy_system_profile",
        },
    )
    return (
        agent_id,
        definition_value,
        definition_hash,
        revision_ref,
        capability_fingerprint,
    )


def _system_revision_id(*, team_id: str, agent_id: str, definition_hash: str) -> str:
    digest = hashlib.sha256(f"{team_id}\0{agent_id}\0{definition_hash}".encode()).hexdigest()
    return f"arv_{digest[:32]}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "AgentExecutionBindingError",
    "AgentExecutionBindingStore",
    "ExactAgentBindingExpectation",
    "ExactAgentExecutionBinding",
    "ProjectedSystemAgentRevision",
    "TurnAgentBindingInput",
    "project_system_agent_revision",
]
