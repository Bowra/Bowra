"""Immutable Evaluation Plans and the mutable candidate-control projection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_policy_store import (
    AgentEvalPolicyError,
    AgentEvalPolicyRevision,
    AgentEvalPolicyStore,
)
from bowhead_core.agent_eval_store import AgentEvalStoreError, verify_eval_suite_revision_conn

AGENT_EVAL_PLAN_COMPILER_VERSION = "agent-eval-plan-v1"
EvalReadiness = Literal[
    "missing",
    "running",
    "ready",
    "ready_with_risks",
    "blocked",
    "unknown",
    "stale",
]
_MAX_PLAN_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class AgentPreparationActor:
    kind: Literal["user", "automation"]
    ref: str
    user_id: str
    automation_grant_id: str | None = None

    def __post_init__(self) -> None:
        if not self.ref or not self.user_id or self.kind not in {"user", "automation"}:
            raise ValueError("Preparation actor is invalid")
        if (self.kind == "automation") != bool(self.automation_grant_id):
            raise ValueError("Preparation actor grant is invalid")
        if self.kind == "user" and self.ref != self.user_id:
            raise ValueError("Preparation user identity is invalid")


class AgentEvalPlanError(RuntimeError):
    """Stable plan/control failure for orchestration and HTTP adapters."""

    def __init__(self, code: str, *, details: Mapping[str, str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        # Safe values the caller needs to retry correctly, such as the current policy revision.
        self.details = dict(details or {})


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class AgentEvalPlanSuite:
    suite_revision_id: str
    suite_kind: str
    execution_scope: str
    required: bool
    tier: str
    attempts: int
    fixture_bundle: dict[str, Any]
    fixture_bundle_hash: str
    scorer_bundle_ref: str
    scorer_bundle_hash: str
    judge_policy_hash: str
    order_index: int


@dataclass(frozen=True, slots=True)
class AgentEvalPlan:
    id: str
    team_id: str
    agent_id: str
    candidate_id: str
    sequence: int
    policy_revision_id: str
    baseline_release_id: str | None
    execution_workspace_id: str
    execution_workspace_scope_hash: str
    candidate_hash: str
    plan: dict[str, Any]
    plan_hash: str
    compiler_version: str
    suites: tuple[AgentEvalPlanSuite, ...]
    created_by: str
    created_at: str


@dataclass(frozen=True, slots=True)
class AgentEvalCandidateControl:
    candidate_id: str
    team_id: str
    agent_id: str
    source_working_copy_id: str
    source_working_copy_version: int
    source_working_copy_hash: str
    active_plan_id: str | None
    readiness: EvalReadiness
    stale_subject: str | None
    reason_codes: tuple[str, ...]
    active_execution_id: str | None
    active_snapshot_id: str | None
    active_override_id: str | None
    row_version: int


class AgentEvalPlanStore:
    """Compile plans from exact candidate, policy, suite, fixture, and Space facts."""

    def __init__(self, db_path: str | Path, *, clock: _Clock | None = None) -> None:
        self.db_path = str(db_path)
        self._clock = clock or _SystemClock()
        self._policy_store = AgentEvalPolicyStore(self.db_path)

    async def refresh_behind_plan_conn(
        self,
        db: aiosqlite.Connection,
        *,
        candidate: aiosqlite.Row,
        control: aiosqlite.Row,
        plan: aiosqlite.Row,
        actor_user_id: str,
        created_at: str,
    ) -> tuple[AgentEvalPlan, int] | None:
        """Recompile a prepared candidate's plan from the current policy when it is behind, else None.

        The policy follows the latest saved test cases while a plan keeps the policy it was compiled
        from. The Owner's comparison start calls this in its own transaction, so a behind plan is never
        used: the run gets a new plan row (next `sequence`, `created_by` the Owner) in the same Space,
        the control row moves to it, and the comparison audit names the plan it replaced. Returns the
        new plan and the control row version after the switch.
        """
        candidate_id = str(candidate["id"])
        team_id = str(candidate["team_id"])
        agent_id = str(candidate["subagent_id"])
        current_policy = await _current_policy_id(db, team_id=team_id, agent_id=agent_id)
        if current_policy is None or current_policy == str(plan["policy_revision_id"]):
            return None
        if str(candidate["state"]) != "prepared":
            code = "agent_eval_plan_frozen_for_review" if str(candidate["state"]) in {"pending", "approved"} else "agent_eval_candidate_not_prepared"
            raise AgentEvalPlanError(code)
        if await _has_active_execution(db, candidate_id=candidate_id):
            raise AgentEvalPlanError("agent_eval_comparison_active")
        try:
            policy = await self._policy_store.get_conn(db, team_id=team_id, agent_id=agent_id, revision_id=current_policy)
        except AgentEvalPolicyError as exc:
            raise AgentEvalPlanError(exc.code) from exc
        refreshed = await self.create_plan_conn(
            db,
            candidate=candidate,
            policy=policy,
            actor_user_id=actor_user_id,
            workspace_id=str(plan["execution_workspace_id"]),
            created_at=created_at,
        )
        readiness = "stale" if control["active_snapshot_id"] is not None else "missing"
        stale_subject = "evidence" if readiness == "stale" else None
        reasons = ("evaluation_plan_changed",) if readiness == "stale" else ()
        expected_row_version = int(control["row_version"])
        updated = await db.execute(
            """
            UPDATE agent_eval_candidate_control
            SET active_plan_id=?,readiness=?,stale_subject=?,reason_codes_json=?,
                active_execution_id=NULL,active_snapshot_id=NULL,active_override_id=NULL,
                row_version=row_version+1,updated_at=?
            WHERE candidate_id=? AND row_version=?
            """,
            (
                refreshed.id,
                readiness,
                stale_subject,
                _canonical_json(list(reasons)),
                created_at,
                candidate_id,
                expected_row_version,
            ),
        )
        if updated.rowcount != 1:
            raise AgentEvalPlanError("agent_eval_control_version_conflict")
        return refreshed, expected_row_version + 1

    async def create_initial_plan_conn(
        self,
        db: aiosqlite.Connection,
        *,
        candidate: aiosqlite.Row,
        policy: AgentEvalPolicyRevision,
        actor_user_id: str,
        workspace_id: str,
        source_working_copy_id: str,
        source_working_copy_version: int,
        source_working_copy_hash: str,
        created_at: str,
        preparation_actor: AgentPreparationActor | None = None,
    ) -> tuple[AgentEvalPlan, AgentEvalCandidateControl]:
        actor = preparation_actor or AgentPreparationActor("user", actor_user_id, actor_user_id)
        if actor.user_id != actor_user_id:
            raise AgentEvalPlanError("agent_eval_preparation_actor_mismatch")
        if str(candidate["state"]) != "prepared":
            raise AgentEvalPlanError("agent_eval_candidate_not_prepared")
        if await _control(db, candidate_id=str(candidate["id"])) is not None:
            raise AgentEvalPlanError("agent_eval_control_already_exists")
        plan = await self.create_plan_conn(
            db,
            candidate=candidate,
            policy=policy,
            actor_user_id=actor_user_id,
            workspace_id=workspace_id,
            created_at=created_at,
        )
        await db.execute(
            """
            INSERT INTO agent_eval_candidate_control(
              candidate_id,team_id,agent_id,source_working_copy_id,
              source_working_copy_version,source_working_copy_hash,active_plan_id,
              readiness,stale_subject,reason_codes_json,active_execution_id,
              active_snapshot_id,active_override_id,prepared_actor_kind,
              prepared_actor_ref,prepared_user_id,prepared_automation_grant_id,
              row_version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,'missing',NULL,'[]',NULL,NULL,NULL,
                     ?,?,?,?,1,?,?)
            """,
            (
                str(candidate["id"]),
                str(candidate["team_id"]),
                str(candidate["subagent_id"]),
                source_working_copy_id,
                source_working_copy_version,
                source_working_copy_hash,
                plan.id,
                actor.kind,
                actor.ref,
                actor.user_id,
                actor.automation_grant_id,
                created_at,
                created_at,
            ),
        )
        control = await _control(db, candidate_id=str(candidate["id"]))
        if control is None:
            raise AgentEvalPlanError("agent_eval_control_create_failed")
        return plan, _control_from_row(control)

    async def create_plan_conn(
        self,
        db: aiosqlite.Connection,
        *,
        candidate: aiosqlite.Row,
        policy: AgentEvalPolicyRevision,
        actor_user_id: str,
        workspace_id: str,
        created_at: str,
    ) -> AgentEvalPlan:
        db.row_factory = aiosqlite.Row
        team_id = str(candidate["team_id"])
        agent_id = str(candidate["subagent_id"])
        if policy.team_id != team_id or policy.agent_id != agent_id:
            raise AgentEvalPlanError("agent_eval_policy_scope_mismatch")
        workspace_scope = await _workspace_scope(
            db,
            team_id=team_id,
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
        )
        if workspace_scope is None:
            raise AgentEvalPlanError("agent_eval_workspace_execute_denied")
        suites = await _compile_suites(
            db,
            team_id=team_id,
            agent_id=agent_id,
            policy=policy,
        )
        if candidate["base_live_release_id"] is None:
            # With no Live release there is nothing to pair the platform contract with; it still
            # runs on the candidate, and its result reads as "not run on Live".
            suites = [replace(suite, execution_scope="candidate_only") if suite.suite_kind == "platform_contract" else suite for suite in suites]
        baseline = await _baseline(
            db,
            team_id=team_id,
            agent_id=agent_id,
            release_id=candidate["base_live_release_id"],
        )
        candidate_revision = await _revision_definition(
            db,
            team_id=team_id,
            agent_id=agent_id,
            revision_id=str(candidate["revision_id"]),
        )
        baseline_definition = (
            await _revision_definition(
                db,
                team_id=team_id,
                agent_id=agent_id,
                revision_id=str(baseline["revision_id"]),
            )
            if baseline is not None
            else None
        )
        scope_hash = _hash_document(workspace_scope)
        suite_identities = [
            {
                "attempts": suite.attempts,
                "case_set_hash": suite.fixture_bundle["case_set_hash"],
                "execution_scope": suite.execution_scope,
                "fixture_bundle_hash": suite.fixture_bundle_hash,
                "judge_policy_hash": suite.judge_policy_hash,
                "required": suite.required,
                "scorer_bundle_ref": suite.scorer_bundle_ref,
                "suite_revision_id": suite.suite_revision_id,
                "tier": suite.tier,
            }
            for suite in suites
        ]
        plan_document = {
            "allowed_tested_delta_manifest": _allowed_delta_manifest(
                baseline_definition=baseline_definition,
                candidate_definition=candidate_revision,
                baseline_audience=(str(baseline["audience_snapshot_json"]) if baseline is not None else None),
                candidate_audience=str(candidate["target_audience_json"]),
            ),
            "audience_execution_projection_hash": _hash_document(
                {
                    "audience_hash": str(candidate["target_audience_hash"]),
                    "workspace_scope_hash": scope_hash,
                },
            ),
            "baseline_release_id": (str(candidate["base_live_release_id"]) if candidate["base_live_release_id"] is not None else None),
            "candidate_hash": str(candidate["candidate_hash"]),
            "candidate_id": str(candidate["id"]),
            "execution_workspace_id": workspace_id,
            "execution_workspace_scope_hash": scope_hash,
            "ordered_suite_items": suite_identities,
            "plan_compiler_version": AGENT_EVAL_PLAN_COMPILER_VERSION,
            "policy_hash": policy.policy_hash,
            "policy_revision_id": policy.id,
            "schema_version": 1,
        }
        plan_json = _canonical_json(plan_document)
        if len(plan_json.encode()) > _MAX_PLAN_BYTES:
            raise AgentEvalPlanError("agent_eval_plan_too_large")
        plan_hash = _hash_text(plan_json)
        async with db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM agent_eval_plan WHERE candidate_id=?",
            (candidate["id"],),
        ) as cursor:
            sequence_row = await cursor.fetchone()
        sequence = int(sequence_row[0])
        plan_id = f"aep_{uuid4().hex}"
        await db.execute(
            """
            INSERT INTO agent_eval_plan(
              id,team_id,agent_id,candidate_id,sequence,policy_revision_id,
              baseline_release_id,execution_workspace_id,execution_workspace_scope_hash,
              candidate_hash,plan_json,plan_hash,compiler_version,created_by,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                plan_id,
                team_id,
                agent_id,
                str(candidate["id"]),
                sequence,
                policy.id,
                candidate["base_live_release_id"],
                workspace_id,
                scope_hash,
                str(candidate["candidate_hash"]),
                plan_json,
                plan_hash,
                AGENT_EVAL_PLAN_COMPILER_VERSION,
                actor_user_id,
                created_at,
            ),
        )
        for suite in suites:
            await db.execute(
                """
                INSERT INTO agent_eval_plan_suite(
                  plan_id,suite_revision_id,suite_kind,execution_scope,required,tier,
                  attempts,fixture_bundle_json,fixture_bundle_hash,scorer_bundle_ref,
                  scorer_bundle_hash,judge_policy_hash,order_index
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    plan_id,
                    suite.suite_revision_id,
                    suite.suite_kind,
                    suite.execution_scope,
                    int(suite.required),
                    suite.tier,
                    suite.attempts,
                    _canonical_json(suite.fixture_bundle),
                    suite.fixture_bundle_hash,
                    suite.scorer_bundle_ref,
                    suite.scorer_bundle_hash,
                    suite.judge_policy_hash,
                    suite.order_index,
                ),
            )
        return AgentEvalPlan(
            id=plan_id,
            team_id=team_id,
            agent_id=agent_id,
            candidate_id=str(candidate["id"]),
            sequence=sequence,
            policy_revision_id=policy.id,
            baseline_release_id=(str(candidate["base_live_release_id"]) if candidate["base_live_release_id"] is not None else None),
            execution_workspace_id=workspace_id,
            execution_workspace_scope_hash=scope_hash,
            candidate_hash=str(candidate["candidate_hash"]),
            plan=plan_document,
            plan_hash=plan_hash,
            compiler_version=AGENT_EVAL_PLAN_COMPILER_VERSION,
            suites=suites,
            created_by=actor_user_id,
            created_at=created_at,
        )

    async def get(
        self,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
        plan_id: str,
        actor_user_id: str,
    ) -> AgentEvalPlan:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _require_owner(
                db,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
            )
            async with db.execute(
                """
                SELECT * FROM agent_eval_plan
                WHERE team_id=? AND agent_id=? AND candidate_id=? AND id=?
                """,
                (team_id, agent_id, candidate_id, plan_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise AgentEvalPlanError("agent_eval_plan_not_found")
            suites = await _plan_suites(db, plan_id=plan_id)
        return _plan_from_row(row, suites=suites)


async def _compile_suites(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    policy: AgentEvalPolicyRevision,
) -> tuple[AgentEvalPlanSuite, ...]:
    raw_items = policy.policy.get("suite_items")
    if not isinstance(raw_items, list) or not raw_items:
        raise AgentEvalPlanError("agent_eval_policy_malformed")
    result: list[AgentEvalPlanSuite] = []
    for order_index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            raise AgentEvalPlanError("agent_eval_policy_malformed")
        suite_revision_id = item.get("suite_revision_id")
        if not isinstance(suite_revision_id, str):
            raise AgentEvalPlanError("agent_eval_policy_malformed")
        async with db.execute(
            """
            SELECT revision.*,suite.team_id,suite.agent_id,suite.agent_source_kind,
                   suite.suite_kind,suite.state
            FROM agent_eval_suite_revision revision
            JOIN agent_eval_suite suite ON suite.id=revision.suite_id
            WHERE revision.id=?
            """,
            (suite_revision_id,),
        ) as cursor:
            revision = await cursor.fetchone()
        if (
            revision is None
            or str(revision["team_id"]) != team_id
            or str(revision["agent_id"]) != agent_id
            or str(revision["agent_source_kind"]) != "team"
            or str(revision["state"]) != "active"
            or str(revision["suite_kind"]) != item.get("suite_kind")
        ):
            raise AgentEvalPlanError("agent_eval_plan_suite_scope_mismatch")
        try:
            await verify_eval_suite_revision_conn(
                db,
                suite_revision_id=suite_revision_id,
            )
        except AgentEvalStoreError as exc:
            raise AgentEvalPlanError(exc.code, details=exc.details) from exc
        async with db.execute(
            """
            SELECT id,case_hash,fixture_ref_json,valid_until
            FROM agent_eval_case_revision
            WHERE suite_revision_id=? ORDER BY case_key,id
            """,
            (suite_revision_id,),
        ) as cursor:
            cases = await cursor.fetchall()
        if len(cases) != int(revision["case_count"]):
            raise AgentEvalPlanError("agent_eval_plan_suite_incomplete")
        fixture_bundle = {
            "case_set_hash": str(revision["case_set_hash"]),
            "cases": [
                {
                    "case_hash": str(case["case_hash"]),
                    "case_revision_id": str(case["id"]),
                    "fixture_ref": _object(case["fixture_ref_json"], code="agent_eval_fixture_malformed"),
                    "valid_until": (str(case["valid_until"]) if case["valid_until"] is not None else None),
                }
                for case in cases
            ],
            "schema_version": 1,
        }
        fixture_json = _canonical_json(fixture_bundle)
        execution_scope = item.get("execution_scope")
        tier = item.get("tier")
        attempts = item.get("attempts")
        required = item.get("required")
        if (
            execution_scope not in {"paired", "candidate_only"}
            or tier not in {"critical", "standard", "diagnostic"}
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or not 1 <= attempts <= 5
            or not isinstance(required, bool)
        ):
            raise AgentEvalPlanError("agent_eval_policy_malformed")
        judge_policy = _object(revision["judge_policy_json"], code="agent_eval_judge_policy_malformed")
        scorer_ref = str(revision["scorer_bundle_ref"])
        result.append(
            AgentEvalPlanSuite(
                suite_revision_id=suite_revision_id,
                suite_kind=str(revision["suite_kind"]),
                execution_scope=execution_scope,
                required=required,
                tier=tier,
                attempts=attempts,
                fixture_bundle=fixture_bundle,
                fixture_bundle_hash=_hash_text(fixture_json),
                scorer_bundle_ref=scorer_ref,
                scorer_bundle_hash=_hash_document({"scorer_bundle_ref": scorer_ref}),
                judge_policy_hash=_hash_document(judge_policy),
                order_index=order_index,
            ),
        )
    return tuple(result)


async def _plan_suites(
    db: aiosqlite.Connection,
    *,
    plan_id: str,
) -> tuple[AgentEvalPlanSuite, ...]:
    async with db.execute(
        "SELECT * FROM agent_eval_plan_suite WHERE plan_id=? ORDER BY order_index",
        (plan_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return tuple(
        AgentEvalPlanSuite(
            suite_revision_id=str(row["suite_revision_id"]),
            suite_kind=str(row["suite_kind"]),
            execution_scope=str(row["execution_scope"]),
            required=bool(row["required"]),
            tier=str(row["tier"]),
            attempts=int(row["attempts"]),
            fixture_bundle=_object(row["fixture_bundle_json"], code="agent_eval_fixture_malformed"),
            fixture_bundle_hash=str(row["fixture_bundle_hash"]),
            scorer_bundle_ref=str(row["scorer_bundle_ref"]),
            scorer_bundle_hash=str(row["scorer_bundle_hash"]),
            judge_policy_hash=str(row["judge_policy_hash"]),
            order_index=int(row["order_index"]),
        )
        for row in rows
    )


async def _require_owner(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
) -> None:
    async with db.execute(
        """
        SELECT 1
        FROM team_subagent agent
        JOIN teams team ON team.id=agent.team_id AND team.status='active'
        JOIN users actor ON actor.id=? AND actor.team_id=agent.team_id AND actor.status='active'
        JOIN team_members member
          ON member.team_id=agent.team_id AND member.user_id=actor.id AND member.status='active'
        WHERE agent.team_id=? AND agent.id=? AND agent.lifecycle_state='active'
          AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
        """,
        (actor_user_id, team_id, agent_id),
    ) as cursor:
        if await cursor.fetchone() is None:
            raise AgentEvalPlanError("agent_eval_plan_forbidden")


async def _workspace_scope(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    workspace_id: str,
    actor_user_id: str,
) -> dict[str, Any] | None:
    async with db.execute(
        """
        SELECT workspace.id,workspace.revision,member.role,
               COALESCE(policy.epoch,0) AS policy_epoch
        FROM workspaces workspace
        JOIN users actor ON actor.id=? AND actor.team_id=workspace.team_id AND actor.status='active'
        JOIN team_members team_member
          ON team_member.team_id=workspace.team_id AND team_member.user_id=actor.id
         AND team_member.status='active'
        JOIN workspace_members member
          ON member.workspace_id=workspace.id AND member.user_id=actor.id AND member.status='active'
        LEFT JOIN access_policy_state policy ON policy.team_id=workspace.team_id
        WHERE workspace.id=? AND workspace.team_id=? AND workspace.status='active'
        """,
        (actor_user_id, workspace_id, team_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "policy_epoch": int(row["policy_epoch"]),
        "schema_version": 1,
        "workspace_id": str(row["id"]),
        "workspace_revision": int(row["revision"] or 1),
        "workspace_role": str(row["role"]),
    }


async def _candidate(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    candidate_id: str,
) -> aiosqlite.Row:
    async with db.execute(
        """
        SELECT * FROM team_subagent_release_candidate
        WHERE team_id=? AND subagent_id=? AND id=?
        """,
        (team_id, agent_id, candidate_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentEvalPlanError("agent_release_candidate_not_found")
    return row


async def _control(db: aiosqlite.Connection, *, candidate_id: str) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM agent_eval_candidate_control WHERE candidate_id=?",
        (candidate_id,),
    ) as cursor:
        return await cursor.fetchone()


async def _has_active_execution(db: aiosqlite.Connection, *, candidate_id: str) -> bool:
    async with db.execute(
        """
        SELECT 1 FROM agent_eval_comparison_execution
        WHERE candidate_id=? AND status IN ('queued','running','finalizing') LIMIT 1
        """,
        (candidate_id,),
    ) as cursor:
        return await cursor.fetchone() is not None


async def plan_behind_details_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    candidate_id: str,
    plan: Mapping[str, Any],
) -> dict[str, str] | None:
    """Safe details when a candidate's plan predates the current evaluation policy, else None.

    The policy follows the latest saved suites, while a plan keeps the policy revision it was compiled
    from. Same rule as Coach checks (`coach_ci_plan_changed`) and the workbench's `evaluation_plan_behind`.
    Releasing, submitting for review or running a comparison on such a plan would rely on results without
    the newer cases, so those commands refuse with `agent_eval_plan_behind` and these details. The Owner's
    own comparison start recompiles the plan first (`AgentEvalPlanStore.refresh_behind_plan_conn`).
    """
    current = await _current_policy_id(db, team_id=team_id, agent_id=agent_id)
    planned = str(plan["policy_revision_id"])
    if current is None or planned == current:
        return None
    return {
        "candidate_id": candidate_id,
        "current_policy_revision_id": current,
        "plan_id": str(plan["id"]),
        "plan_policy_revision_id": planned,
    }


async def _current_policy_id(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
) -> str | None:
    async with db.execute(
        """
        SELECT id FROM agent_eval_policy_revision
        WHERE team_id=? AND agent_id=? ORDER BY sequence DESC LIMIT 1
        """,
        (team_id, agent_id),
    ) as cursor:
        row = await cursor.fetchone()
    return str(row["id"]) if row is not None else None


async def _baseline(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    release_id: object,
) -> aiosqlite.Row | None:
    if release_id is None:
        return None
    async with db.execute(
        """
        SELECT * FROM team_subagent_release
        WHERE team_id=? AND subagent_id=? AND id=?
        """,
        (team_id, agent_id, release_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentEvalPlanError("agent_eval_baseline_release_not_found")
    return row


async def _revision_definition(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    revision_id: str,
) -> dict[str, Any]:
    async with db.execute(
        """
        SELECT definition_json FROM team_subagent_revision
        WHERE team_id=? AND subagent_id=? AND id=?
        """,
        (team_id, agent_id, revision_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentEvalPlanError("agent_eval_revision_not_found")
    return _object(row["definition_json"], code="agent_eval_revision_malformed")


def _allowed_delta_manifest(
    *,
    baseline_definition: Mapping[str, Any] | None,
    candidate_definition: Mapping[str, Any],
    baseline_audience: str | None,
    candidate_audience: str,
) -> dict[str, Any]:
    if baseline_definition is None:
        changed = sorted(candidate_definition)
    else:
        changed = sorted(
            key for key in set(baseline_definition) | set(candidate_definition) if baseline_definition.get(key) != candidate_definition.get(key)
        )
    return {
        "audience_changed": baseline_audience != candidate_audience,
        "definition_fields": changed,
        "schema_version": 1,
    }


def _plan_from_row(
    row: Mapping[str, Any],
    *,
    suites: tuple[AgentEvalPlanSuite, ...],
) -> AgentEvalPlan:
    plan = _object(row["plan_json"], code="agent_eval_plan_malformed")
    if _hash_document(plan) != str(row["plan_hash"]):
        raise AgentEvalPlanError("agent_eval_plan_hash_mismatch")
    return AgentEvalPlan(
        id=str(row["id"]),
        team_id=str(row["team_id"]),
        agent_id=str(row["agent_id"]),
        candidate_id=str(row["candidate_id"]),
        sequence=int(row["sequence"]),
        policy_revision_id=str(row["policy_revision_id"]),
        baseline_release_id=(str(row["baseline_release_id"]) if row["baseline_release_id"] else None),
        execution_workspace_id=str(row["execution_workspace_id"]),
        execution_workspace_scope_hash=str(row["execution_workspace_scope_hash"]),
        candidate_hash=str(row["candidate_hash"]),
        plan=plan,
        plan_hash=str(row["plan_hash"]),
        compiler_version=str(row["compiler_version"]),
        suites=suites,
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
    )


def _control_from_row(row: Mapping[str, Any]) -> AgentEvalCandidateControl:
    reasons = json.loads(str(row["reason_codes_json"]))
    if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
        raise AgentEvalPlanError("agent_eval_control_malformed")
    readiness = str(row["readiness"])
    if readiness not in {"missing", "running", "ready", "ready_with_risks", "blocked", "unknown", "stale"}:
        raise AgentEvalPlanError("agent_eval_control_malformed")
    return AgentEvalCandidateControl(
        candidate_id=str(row["candidate_id"]),
        team_id=str(row["team_id"]),
        agent_id=str(row["agent_id"]),
        source_working_copy_id=str(row["source_working_copy_id"]),
        source_working_copy_version=int(row["source_working_copy_version"]),
        source_working_copy_hash=str(row["source_working_copy_hash"]),
        active_plan_id=(str(row["active_plan_id"]) if row["active_plan_id"] else None),
        readiness=readiness,
        stale_subject=(str(row["stale_subject"]) if row["stale_subject"] else None),
        reason_codes=tuple(reasons),
        active_execution_id=(str(row["active_execution_id"]) if row["active_execution_id"] else None),
        active_snapshot_id=(str(row["active_snapshot_id"]) if row["active_snapshot_id"] else None),
        active_override_id=(str(row["active_override_id"]) if row["active_override_id"] else None),
        row_version=int(row["row_version"]),
    )


def _object(value: object, *, code: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise AgentEvalPlanError(code) from exc
    if not isinstance(decoded, dict):
        raise AgentEvalPlanError(code)
    return decoded


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _hash_document(value: object) -> str:
    return _hash_text(_canonical_json(value))


__all__ = [
    "AGENT_EVAL_PLAN_COMPILER_VERSION",
    "AgentEvalCandidateControl",
    "AgentEvalPlan",
    "AgentEvalPlanError",
    "AgentEvalPlanStore",
    "AgentEvalPlanSuite",
]
