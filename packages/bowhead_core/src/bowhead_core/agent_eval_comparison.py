"""Atomic full-version Eval comparison, cancellation, and Snapshot finalization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_automation_context import (
    AutomationAuthority,
    canonical,
    comparison_intent_hash,
    comparison_origin_conn,
)
from bowhead_core.agent_candidate_name_store import AgentCandidateNameStore
from bowhead_core.agent_eval_job_store import AgentEvalJobStore, AgentEvalJobStoreError
from bowhead_core.agent_eval_plan_store import AgentEvalPlanError, AgentEvalPlanStore, plan_behind_details_conn
from bowhead_core.agent_eval_points import suite_revision_grades_points_sql, unmet_blocking_point_ids
from bowhead_core.agent_eval_policy_store import adopt_default_verdict_rules_conn
from bowhead_core.agent_eval_store import AgentEvalStore, AgentEvalStoreError, EvalRunSubject
from bowhead_core.agent_version_automation import AgentVersionAutomationError
from bowhead_core.agent_version_eval import AttemptEvidence, ComparisonCase, ReadinessDecision, project_readiness
from bowhead_core.audit_actions import AuditAction
from bowhead_core.user_store import UserStore

ComparisonStatus = Literal["queued", "running", "finalizing", "completed", "failed", "cancelled"]
_TERMINAL_RUNS = {"passed", "failed", "unknown", "incomplete", "cancelled"}
_MAX_SELECTION_BYTES = 256 * 1024
# Claiming, scratch-Turn setup and settlement between attempts; a run's budget allows this on top
# of its attempts' own limits so the run bound never fires before an attempt's own limit does.
RUN_SCHEDULING_SLACK_MS = 60_000
# The job store's bounds on one run's plan.
_RUN_MAX_DURATION_MS = 3_600_000
_RUN_MAX_COST_MICROUSD = 1_000_000_000
_RUN_MAX_CONCURRENCY = 8


def comparison_run_budget(*, case_count: int, attempts: int, limits: Mapping[str, Any]) -> tuple[int, int]:
    """One comparison run's (wall-clock ms, cost microusd) budget from the policy limits.

    ``max_attempt_*`` limit each attempt; a run of ``case_count × attempts`` attempts on
    ``max_concurrency`` workers needs ``ceil(jobs / concurrency)`` attempt slots. The runs of a
    comparison start together, so each run is also capped by the comparison-wide limits.
    """

    jobs = max(1, case_count * attempts)
    concurrency = max(1, min(_RUN_MAX_CONCURRENCY, int(limits["max_concurrency"])))
    waves = -(-jobs // concurrency)
    attempt_duration = int(limits["max_attempt_duration_ms"])
    duration = min(
        int(limits["max_comparison_duration_ms"]),
        waves * attempt_duration + RUN_SCHEDULING_SLACK_MS,
        _RUN_MAX_DURATION_MS,
    )
    cost = min(int(limits["max_comparison_cost_microusd"]), jobs * int(limits["max_attempt_cost_microusd"]), _RUN_MAX_COST_MICROUSD)
    return max(1_000, duration), max(0, cost)


def coach_comparison_intent_hash(
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
    delegation_id: str,
    candidate_id: str,
    candidate_hash: str,
    candidate_row_version: int,
    plan_id: str,
    plan_hash: str,
    control_row_version: int,
    run_kind: str,
    selection: tuple[str, ...] = (),
) -> str:
    """Preserve the v1 internal request identity independently of live routes."""
    return _hash_document(
        {
            "team_id": team_id,
            "agent_id": agent_id,
            "actor_kind": "user",
            "actor_ref": actor_user_id,
            "automation_grant_id": None,
            "coach_delegation_id": delegation_id,
            "candidate_id": candidate_id,
            "candidate_hash": candidate_hash,
            "candidate_row_version": candidate_row_version,
            "plan_id": plan_id,
            "plan_hash": plan_hash,
            "control_row_version": control_row_version,
            "run_kind": run_kind,
            "selected_case_revision_ids": sorted(selection),
            "schema_version": 1,
        }
    )


class AgentEvalComparisonError(RuntimeError):
    def __init__(self, code: str, *, details: Mapping[str, str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        # Safe identifiers the caller needs to act on the refusal, such as the candidate whose plan is behind.
        self.details = dict(details or {})


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ComparisonRunLink:
    suite_revision_id: str
    subject_side: Literal["baseline", "candidate"]
    run_id: str


@dataclass(frozen=True, slots=True)
class ComparisonExecution:
    id: str
    team_id: str
    agent_id: str
    candidate_id: str
    plan_id: str
    run_kind: Literal["release", "diagnostic"]
    status: ComparisonStatus
    selection_hash: str
    request_hash: str
    actor_kind: Literal["user", "automation"]
    actor_ref: str
    generation: int
    row_version: int
    requested_at: str
    finished_at: str | None
    failure_code: str | None
    runs: tuple[ComparisonRunLink, ...]
    client_intent_hash: str | None = None
    automation_context: dict[str, Any] | None = None
    coach_delegation_id: str | None = None

    @property
    def can_cancel(self) -> bool:
        return self.status in {"queued", "running"}


@dataclass(frozen=True, slots=True)
class EvalSnapshot:
    id: str
    candidate_id: str
    plan_id: str
    execution_id: str
    readiness: str
    identity_hash: str
    reviewer_projection_hash: str
    snapshot_hash: str
    created_at: str


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    execution: ComparisonExecution
    snapshot: EvalSnapshot | None
    decision: ReadinessDecision | None


class AgentEvalComparisonStore:
    """Own the comparison aggregate; Agent execution remains in the existing worker."""

    def __init__(self, db_path: str | Path, *, clock: _Clock | None = None, runtime_check: Callable[[str], Awaitable[None]] | None = None) -> None:
        self.db_path = str(db_path)
        self.runtime_check = runtime_check
        self._clock = clock or _SystemClock()
        self._eval_store = AgentEvalStore(self.db_path)
        self._job_store = AgentEvalJobStore(self.db_path)

    async def replay_coach(
        self, *, team_id: str, agent_id: str, actor_user_id: str, delegation_id: str, idempotency_key: str, client_intent_hash: str
    ) -> ComparisonExecution | None:
        """Read original intent without resolving a new route or admitting work."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            await _require_owner(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            row = await _execution_by_idempotency(
                db, team_id=team_id, actor_kind="user", actor_ref=actor_user_id, idempotency_hash=_hash_text(idempotency_key)
            )
            if row is None:
                return None
            if row["coach_delegation_id"] != delegation_id or row["coach_client_intent_hash"] != client_intent_hash or row["agent_id"] != agent_id:
                raise AgentEvalComparisonError("agent_eval_comparison_idempotency_conflict")
            return await _execution_from_row(db, row)

    async def start_release(
        self,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
        actor_user_id: str,
        candidate_row_version: int,
        expected_control_row_version: int,
        candidate_hash: str,
        plan_id: str,
        plan_hash: str,
        model_config_hashes: Mapping[str, str],
        idempotency_key: str,
        judge_model_fingerprint: str | None = None,
        actor_kind: Literal["user", "automation"] = "user",
        actor_ref: str | None = None,
        automation_grant_id: str | None = None,
        automation: AutomationAuthority | None = None,
        coach_delegation_id: str | None = None,
        refresh_behind_plan: bool = False,
    ) -> ComparisonExecution:
        """Start the release comparison. `refresh_behind_plan` is the Owner's own run only: a plan
        behind the latest saved test cases is recompiled in the same transaction instead of refusing."""
        if refresh_behind_plan and (actor_kind != "user" or automation is not None or coach_delegation_id is not None):
            raise AgentVersionAutomationError("agent_automation_origin_mismatch")
        return await self._start(
            team_id=team_id,
            agent_id=agent_id,
            candidate_id=candidate_id,
            actor_user_id=actor_user_id,
            candidate_row_version=candidate_row_version,
            expected_control_row_version=expected_control_row_version,
            candidate_hash=candidate_hash,
            plan_id=plan_id,
            plan_hash=plan_hash,
            model_config_hashes=model_config_hashes,
            idempotency_key=idempotency_key,
            judge_model_fingerprint=judge_model_fingerprint,
            run_kind="release",
            selected_case_revision_ids=(),
            actor_kind=actor_kind,
            actor_ref=actor_ref or actor_user_id,
            automation_grant_id=automation_grant_id,
            automation=automation,
            coach_delegation_id=coach_delegation_id,
            refresh_behind_plan=refresh_behind_plan,
        )

    async def start_diagnostic(
        self,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
        actor_user_id: str,
        candidate_row_version: int,
        expected_control_row_version: int,
        candidate_hash: str,
        plan_id: str,
        plan_hash: str,
        model_config_hashes: Mapping[str, str],
        selected_case_revision_ids: tuple[str, ...],
        idempotency_key: str,
        judge_model_fingerprint: str | None = None,
        actor_kind: Literal["user", "automation"] = "user",
        actor_ref: str | None = None,
        automation_grant_id: str | None = None,
        automation: AutomationAuthority | None = None,
        coach_delegation_id: str | None = None,
    ) -> ComparisonExecution:
        if not selected_case_revision_ids or len(set(selected_case_revision_ids)) != len(selected_case_revision_ids):
            raise ValueError("Agent Eval diagnostic selection is invalid")
        return await self._start(
            team_id=team_id,
            agent_id=agent_id,
            candidate_id=candidate_id,
            actor_user_id=actor_user_id,
            candidate_row_version=candidate_row_version,
            expected_control_row_version=expected_control_row_version,
            candidate_hash=candidate_hash,
            plan_id=plan_id,
            plan_hash=plan_hash,
            model_config_hashes=model_config_hashes,
            idempotency_key=idempotency_key,
            judge_model_fingerprint=judge_model_fingerprint,
            run_kind="diagnostic",
            selected_case_revision_ids=selected_case_revision_ids,
            actor_kind=actor_kind,
            actor_ref=actor_ref or actor_user_id,
            automation_grant_id=automation_grant_id,
            automation=automation,
            coach_delegation_id=coach_delegation_id,
            refresh_behind_plan=False,
        )

    async def _start(
        self,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
        actor_user_id: str,
        candidate_row_version: int,
        expected_control_row_version: int,
        candidate_hash: str,
        plan_id: str,
        plan_hash: str,
        model_config_hashes: Mapping[str, str],
        idempotency_key: str,
        judge_model_fingerprint: str | None,
        run_kind: Literal["release", "diagnostic"],
        selected_case_revision_ids: tuple[str, ...],
        actor_kind: Literal["user", "automation"],
        actor_ref: str,
        automation_grant_id: str | None,
        automation: AutomationAuthority | None,
        coach_delegation_id: str | None,
        refresh_behind_plan: bool,
    ) -> ComparisonExecution:
        if not idempotency_key or len(idempotency_key.encode()) > 200:
            raise ValueError("Agent Eval comparison idempotency key is invalid")
        request = {
            **({"coach_delegation_id": coach_delegation_id} if coach_delegation_id is not None else {}),
            "candidate_hash": candidate_hash,
            "candidate_id": candidate_id,
            "candidate_row_version": candidate_row_version,
            "control_row_version": expected_control_row_version,
            "model_config_hashes": dict(sorted(model_config_hashes.items())),
            "plan_hash": plan_hash,
            "plan_id": plan_id,
            "run_kind": run_kind,
            "schema_version": 1,
            "selected_case_revision_ids": sorted(selected_case_revision_ids),
            "actor_kind": actor_kind,
            "actor_ref": actor_ref,
            "automation_grant_id": automation_grant_id,
            **({"refresh_behind_plan": True} if refresh_behind_plan else {}),
        }
        if coach_delegation_id is not None and (
            actor_kind != "user" or automation is not None or automation_grant_id is not None or actor_ref != actor_user_id
        ):
            from bowhead_core.coach_ci_delegation import CoachCIError

            raise CoachCIError("coach_ci_origin_mismatch")
        if actor_kind != "automation" and automation is not None:
            raise AgentVersionAutomationError("agent_automation_origin_mismatch")
        if actor_kind == "automation" and (
            automation is None
            or (actor_ref, automation_grant_id, actor_user_id) != (automation.principal_id, automation.grant_id, automation.effective_owner_user_id)
        ):
            raise AgentVersionAutomationError("agent_automation_context_required")
        client_intent = (
            comparison_intent_hash(
                team_id=team_id,
                agent_id=agent_id,
                authority=automation,
                candidate_id=candidate_id,
                candidate_hash=candidate_hash,
                candidate_row_version=candidate_row_version,
                plan_id=plan_id,
                plan_hash=plan_hash,
                expected_control_row_version=expected_control_row_version,
                run_kind=run_kind,
                selected_case_revision_ids=selected_case_revision_ids,
            )
            if automation is not None
            else None
        )
        coach_client_intent = None
        if coach_delegation_id is not None:
            # Route hashes are resolved by the server. Replaying the original
            # client intent recovers the original execution after a route change.
            coach_client_intent = coach_comparison_intent_hash(
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
                delegation_id=coach_delegation_id,
                candidate_id=candidate_id,
                candidate_hash=candidate_hash,
                candidate_row_version=candidate_row_version,
                plan_id=plan_id,
                plan_hash=plan_hash,
                control_row_version=expected_control_row_version,
                run_kind=run_kind,
                selection=selected_case_revision_ids,
            )
        request_hash = _hash_document(request)
        idempotency_hash = _hash_text(idempotency_key)
        timestamp = self._timestamp()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_owner(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
                grant = await automation.require_conn(db, team_id=team_id, agent_id=agent_id, scope="eval:run") if automation is not None else None
                existing = await _execution_by_idempotency(
                    db,
                    team_id=team_id,
                    actor_kind=actor_kind,
                    actor_ref=actor_ref,
                    idempotency_hash=idempotency_hash,
                )
                if coach_delegation_id is not None:
                    from bowhead_core.coach_ci_delegation import check_comparison_conn

                    await check_comparison_conn(
                        db,
                        coach_delegation_id,
                        team_id=team_id,
                        agent_id=agent_id,
                        actor_user_id=actor_user_id,
                        candidate_id=candidate_id,
                        plan_id=plan_id,
                        new=existing is None,
                    )
                if existing is not None:
                    if automation is not None:
                        await comparison_origin_conn(db, execution=existing, authority=automation, scope="eval:run")
                    if (
                        existing["candidate_id"] != candidate_id
                        # A refreshing run executes on the plan it compiled; its request hash still names the one it replaced.
                        or (existing["plan_id"] != plan_id and not refresh_behind_plan)
                        or existing["coach_delegation_id"] != coach_delegation_id
                        or (
                            existing["coach_client_intent_hash"] != coach_client_intent
                            if coach_delegation_id is not None
                            else existing["client_intent_hash"] != client_intent
                            if existing["client_intent_hash"] is not None
                            else str(existing["request_hash"]) != request_hash
                        )
                    ):
                        raise AgentEvalComparisonError("agent_eval_comparison_idempotency_conflict")
                    result = await _execution_from_row(db, existing)
                    await db.commit()
                    return result
                candidate, control, plan, policy = await _start_context(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    candidate_id=candidate_id,
                    plan_id=plan_id,
                )
                if (
                    grant is not None
                    and grant["evaluation_workspace_id"] is not None
                    and grant["evaluation_workspace_id"] != plan["execution_workspace_id"]
                ):
                    raise AgentVersionAutomationError("agent_automation_workspace_mismatch")
                if grant is not None and grant["evaluation_workspace_id"] is None:
                    # Grants created before protocol v2 can't start evaluations; the Owner replaces them.
                    raise AgentVersionAutomationError("agent_automation_workspace_required")
                if str(candidate["state"]) != "prepared":
                    raise AgentEvalComparisonError("agent_eval_candidate_not_prepared")
                if int(candidate["row_version"]) != candidate_row_version:
                    raise AgentEvalComparisonError("agent_release_candidate_version_conflict")
                if str(candidate["candidate_hash"]) != candidate_hash:
                    raise AgentEvalComparisonError("agent_eval_candidate_hash_mismatch")
                if int(control["row_version"]) != expected_control_row_version:
                    raise AgentEvalComparisonError("agent_eval_control_version_conflict")
                if str(control["active_plan_id"] or "") != plan_id or str(plan["plan_hash"]) != plan_hash:
                    raise AgentEvalComparisonError("agent_eval_plan_version_conflict")
                # A plan compiled before the latest saved test cases would run without them. CI (eval:run) and
                # Coach are refused; the Owner's own run recompiles the plan here, so the run, the new plan
                # row and the audit record all show the refresh.
                replaced_plan_id: str | None = None
                if refresh_behind_plan:
                    # The Owner's own test is where an older policy adopts the current verdict rules: the
                    # new revision makes this plan behind, and the refresh below recompiles it. Nothing else
                    # moves, so other plans and their evidence stay current until the Owner tests again.
                    await adopt_default_verdict_rules_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id, created_at=timestamp)
                    try:
                        refreshed = await AgentEvalPlanStore(self.db_path).refresh_behind_plan_conn(
                            db, candidate=candidate, control=control, plan=plan, actor_user_id=actor_user_id, created_at=timestamp
                        )
                    except AgentEvalPlanError as exc:
                        raise AgentEvalComparisonError(exc.code, details=exc.details) from exc
                    if refreshed is not None:
                        replaced_plan_id = plan_id
                        plan_id = refreshed[0].id
                        expected_control_row_version = refreshed[1]
                        candidate, control, plan, policy = await _start_context(
                            db, team_id=team_id, agent_id=agent_id, candidate_id=candidate_id, plan_id=plan_id
                        )
                behind = await plan_behind_details_conn(db, team_id=team_id, agent_id=agent_id, candidate_id=candidate_id, plan=plan)
                if behind is not None:
                    raise AgentEvalComparisonError("agent_eval_plan_behind", details=behind)
                current_scope_hash = await _workspace_scope_hash(
                    db,
                    team_id=team_id,
                    workspace_id=str(plan["execution_workspace_id"]),
                    actor_user_id=actor_user_id,
                )
                if current_scope_hash is None:
                    raise AgentEvalComparisonError("agent_eval_workspace_execute_denied")
                if current_scope_hash != str(plan["execution_workspace_scope_hash"]):
                    raise AgentEvalComparisonError("agent_eval_plan_stale")
                suites = await _plan_suites(db, plan_id=plan_id)
                if not suites:
                    raise AgentEvalComparisonError("agent_eval_plan_suite_incomplete")
                selected_by_suite = await _selected_cases(
                    db,
                    suites=suites,
                    run_kind=run_kind,
                    selected_case_revision_ids=selected_case_revision_ids,
                )
                selected_suites = [row for row in suites if str(row["suite_revision_id"]) in selected_by_suite]
                selection = {
                    "run_kind": run_kind,
                    "schema_version": 1,
                    "suite_revision_ids": [str(row["suite_revision_id"]) for row in selected_suites],
                    "case_revision_ids": sorted(selected_case_revision_ids),
                    "cases_by_suite": {suite_id: list(case_ids) for suite_id, case_ids in sorted(selected_by_suite.items())},
                }
                selection_json = _canonical_json(selection)
                if len(selection_json.encode()) > _MAX_SELECTION_BYTES:
                    raise AgentEvalComparisonError("agent_eval_comparison_selection_too_large")
                execution_id = f"aex_{uuid4().hex}"
                await db.execute(
                    """
                    INSERT INTO agent_eval_comparison_execution(
                      id,team_id,agent_id,candidate_id,plan_id,run_kind,selection_json,
                      selection_hash,status,idempotency_hash,request_hash,actor_kind,
                      actor_ref,automation_grant_id,generation,row_version,requested_at,client_intent_hash,automation_context_json,coach_delegation_id,coach_client_intent_hash
                    ) VALUES(?,?,?,?,?,?,?,?,'queued',?,?,?,?,?,1,1,?,?,?,?,?)
                    """,
                    (
                        execution_id,
                        team_id,
                        agent_id,
                        candidate_id,
                        plan_id,
                        run_kind,
                        selection_json,
                        _hash_text(selection_json),
                        idempotency_hash,
                        request_hash,
                        actor_kind,
                        actor_ref,
                        automation_grant_id,
                        timestamp,
                        client_intent,
                        canonical(
                            {
                                "schema_version": 1,
                                "protocol_version": 2,
                                "repository": automation.repository,
                                "ref": automation.ref,
                                "effective_owner_user_id": automation.effective_owner_user_id,
                                "evaluation_workspace_id": grant["evaluation_workspace_id"],
                            }
                        )
                        if automation is not None
                        else None,
                        coach_delegation_id,
                        coach_client_intent,
                    ),
                )
                limits = _object(policy["policy_json"], code="agent_eval_policy_malformed").get("cost_and_latency_limits")
                if not isinstance(limits, dict):
                    raise AgentEvalComparisonError("agent_eval_policy_malformed")
                for suite in selected_suites:
                    run_duration_ms, run_cost_microusd = comparison_run_budget(
                        case_count=len(selected_by_suite[str(suite["suite_revision_id"])]),
                        attempts=int(suite["attempts"]),
                        limits=limits,
                    )
                    for side in _subject_sides(suite):
                        subject = await _subject(
                            db,
                            candidate=candidate,
                            side=side,
                            model_config_hashes=model_config_hashes,
                        )
                        judge_role, judge_hash = await _judge_identity(
                            db,
                            suite_revision_id=str(suite["suite_revision_id"]),
                            judge_model_fingerprint=judge_model_fingerprint,
                        )
                        run = await self._eval_store.create_run_conn(
                            db,
                            subject=subject,
                            suite_revision_id=str(suite["suite_revision_id"]),
                            trigger_kind="manual",
                            idempotency_key=f"cmp:{execution_id}:{suite['order_index']}:{side}",
                            actor_user_id=actor_user_id,
                            judge_role_ref=judge_role,
                            judge_model_fingerprint=judge_hash,
                            created_at=timestamp,
                        )
                        await self._job_store.materialize_jobs_conn(
                            db,
                            run_id=run.run_id,
                            workspace_id=str(plan["execution_workspace_id"]),
                            attempts=int(suite["attempts"]),
                            max_concurrency=min(_RUN_MAX_CONCURRENCY, int(limits["max_concurrency"])),
                            # Run-wide bounds; each attempt keeps max_attempt_duration_ms (read by the service).
                            max_duration_ms=run_duration_ms,
                            max_cost_microusd=run_cost_microusd,
                            created_at=timestamp,
                            case_revision_ids=selected_by_suite[str(suite["suite_revision_id"])],
                        )
                        await db.execute(
                            """
                            INSERT INTO agent_eval_comparison_run(
                              execution_id,suite_revision_id,subject_side,run_id
                            ) VALUES(?,?,?,?)
                            """,
                            (execution_id, suite["suite_revision_id"], side, run.run_id),
                        )
                if run_kind == "release":
                    control_update = """
                        UPDATE agent_eval_candidate_control
                        SET readiness='running',stale_subject=NULL,reason_codes_json='[]',
                            active_execution_id=?,row_version=row_version+1,updated_at=?
                        WHERE candidate_id=? AND row_version=?
                    """
                else:
                    control_update = """
                        UPDATE agent_eval_candidate_control
                        SET active_execution_id=?,row_version=row_version+1,updated_at=?
                        WHERE candidate_id=? AND row_version=?
                    """
                updated = await db.execute(
                    control_update,
                    (execution_id, timestamp, candidate_id, expected_control_row_version),
                )
                if updated.rowcount != 1:
                    raise AgentEvalComparisonError("agent_eval_control_version_conflict")
                await _audit(
                    db,
                    db_path=self.db_path,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                    action=AuditAction.AGENT_EVAL_COMPARISON_STARTED,
                    payload={
                        "candidate_id": candidate_id,
                        "execution_id": execution_id,
                        "plan_id": plan_id,
                        **({"replaced_plan_id": replaced_plan_id} if replaced_plan_id is not None else {}),
                        "run_kind": run_kind,
                    },
                )
                row = await _execution(db, execution_id)
                if row is None:
                    raise AgentEvalComparisonError("agent_eval_comparison_create_failed")
                result = await _execution_from_row(db, row)
                await db.commit()
                return result
            except (AgentEvalStoreError, AgentEvalJobStoreError) as exc:
                await db.rollback()
                raise AgentEvalComparisonError(exc.code) from exc
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise AgentEvalComparisonError("agent_eval_comparison_integrity_error") from exc
            except BaseException:
                await db.rollback()
                raise

    async def replay_automation(
        self,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
        automation: AutomationAuthority,
        idempotency_key: str,
        client_intent_hash: str,
    ) -> ComparisonExecution | None:
        """Recover the original execution before resolving current model routes."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            try:
                await automation.require_conn(db, team_id=team_id, agent_id=agent_id, scope="eval:run")
                row = await _execution_by_idempotency(
                    db, team_id=team_id, actor_kind="automation", actor_ref=automation.principal_id, idempotency_hash=_hash_text(idempotency_key)
                )
                if row is None:
                    return None
                if row["agent_id"] != agent_id or row["candidate_id"] != candidate_id:
                    raise AgentEvalComparisonError("agent_eval_comparison_idempotency_conflict")
                await comparison_origin_conn(db, execution=row, authority=automation, scope="eval:run")
                if row["client_intent_hash"] is None:
                    return None
                if row["client_intent_hash"] != client_intent_hash:
                    raise AgentEvalComparisonError("agent_eval_comparison_idempotency_conflict")
                return await _execution_from_row(db, row)
            finally:
                await db.rollback()

    async def replay_owner(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        idempotency_key: str,
    ) -> ComparisonExecution | None:
        """The Owner's execution started under this exact idempotency key, or None.

        A "Test this change" retry resends its key after the plan may have been refreshed and the
        control row moved, so the original request can't be rebuilt; the key alone recovers the run.
        """
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            try:
                row = await _execution_by_idempotency(
                    db, team_id=team_id, actor_kind="user", actor_ref=actor_user_id, idempotency_hash=_hash_text(idempotency_key)
                )
                if row is None:
                    return None
                if row["agent_id"] != agent_id or row["coach_delegation_id"] is not None:
                    raise AgentEvalComparisonError("agent_eval_comparison_idempotency_conflict")
                return await _execution_from_row(db, row)
            finally:
                await db.rollback()

    async def mark_running(self, execution_id: str) -> ComparisonExecution:
        timestamp = self._timestamp()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await _require_execution(db, execution_id)
                if str(row["status"]) == "queued":
                    await db.execute(
                        """
                        UPDATE agent_eval_comparison_execution
                        SET status='running',started_at=?,row_version=row_version+1
                        WHERE id=? AND status='queued'
                        """,
                        (timestamp, execution_id),
                    )
                elif str(row["status"]) != "running":
                    raise AgentEvalComparisonError("agent_eval_comparison_not_startable")
                updated = await _require_execution(db, execution_id)
                result = await _execution_from_row(db, updated)
                await db.commit()
                return result
            except BaseException:
                await db.rollback()
                raise

    async def cancel(
        self,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str,
        execution_id: str,
        actor_user_id: str,
        expected_execution_row_version: int,
        actor_kind: Literal["user", "automation"] = "user",
        automation_grant_id: str | None = None,
        automation: AutomationAuthority | None = None,
    ) -> ComparisonExecution:
        timestamp = self._timestamp()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_owner(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
                row = await _require_execution(db, execution_id)
                if (str(row["team_id"]), str(row["agent_id"]), str(row["candidate_id"])) != (
                    team_id,
                    agent_id,
                    candidate_id,
                ):
                    raise AgentEvalComparisonError("agent_eval_comparison_not_found")
                if actor_kind == "automation":
                    if automation is None or automation.effective_owner_user_id != actor_user_id:
                        raise AgentVersionAutomationError("agent_automation_context_required")
                    await comparison_origin_conn(db, execution=row, authority=automation, scope="eval:run")
                if actor_kind == "automation" and (
                    str(row["actor_kind"]) != "automation" or str(row["automation_grant_id"] or "") != str(automation_grant_id or "")
                ):
                    raise AgentEvalComparisonError("agent_eval_comparison_not_cancellable")
                if str(row["status"]) == "cancelled":
                    await db.commit()
                    return await _execution_from_row(db, row)
                if int(row["row_version"]) != expected_execution_row_version:
                    raise AgentEvalComparisonError("agent_eval_comparison_version_conflict")
                if str(row["status"]) not in {"queued", "running"}:
                    raise AgentEvalComparisonError("agent_eval_comparison_not_cancellable")
                links = await _run_links(db, execution_id)
                for link in links:
                    async with db.execute(
                        "SELECT * FROM agent_eval_run WHERE id=?",
                        (link.run_id,),
                    ) as cursor:
                        run = await cursor.fetchone()
                    if run is None or str(run["status"]) in _TERMINAL_RUNS:
                        continue
                    await db.execute(
                        """
                        UPDATE agent_eval_run_execution
                        SET cancel_requested_at=COALESCE(cancel_requested_at,?)
                        WHERE run_id=?
                        """,
                        (timestamp, link.run_id),
                    )
                    await _cancel_run_conn(db, run=run, timestamp=timestamp)
                await db.execute(
                    """
                    UPDATE agent_eval_comparison_execution
                    SET status='cancelled',cancelled_at=?,row_version=row_version+1
                    WHERE id=? AND row_version=?
                    """,
                    (timestamp, execution_id, expected_execution_row_version),
                )
                readiness, reasons = await _prior_readiness(db, candidate_id=candidate_id)
                await db.execute(
                    """
                    UPDATE agent_eval_candidate_control
                    SET readiness=?,stale_subject=NULL,reason_codes_json=?,active_execution_id=NULL,
                        row_version=row_version+1,updated_at=?
                    WHERE candidate_id=? AND active_execution_id=?
                    """,
                    (readiness, _canonical_json(reasons), timestamp, candidate_id, execution_id),
                )
                await _audit(
                    db,
                    db_path=self.db_path,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                    action=AuditAction.AGENT_EVAL_COMPARISON_CANCELLED,
                    payload={"candidate_id": candidate_id, "execution_id": execution_id},
                )
                updated = await _require_execution(db, execution_id)
                result = await _execution_from_row(db, updated)
                await db.commit()
                return result
            except BaseException:
                await db.rollback()
                raise

    async def finalize(self, execution_id: str) -> FinalizeResult:
        current = await self.get(execution_id)
        if current.coach_delegation_id and current.status not in {"completed", "failed", "cancelled"}:
            from bowhead_core.agent_automation_execution import AutomationExecutionGuard
            from bowhead_core.coach_ci_delegation import CoachCIError

            if self.runtime_check is None:
                raise CoachCIError("coach_ci_runtime_gate_required")
            for run in current.runs:
                if not await AutomationExecutionGuard(self.db_path, run.run_id, runtime_check=self.runtime_check).allowed():
                    break
        timestamp = self._timestamp()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                execution = await _require_execution(db, execution_id)
                if str(execution["status"]) in {"completed", "failed", "cancelled"}:
                    snapshot = await _snapshot_for_execution(db, execution_id)
                    await db.commit()
                    return FinalizeResult(
                        execution=await _execution_from_row(db, execution),
                        snapshot=_snapshot_from_row(snapshot) if snapshot is not None else None,
                        decision=None,
                    )
                from bowhead_core.agent_automation_execution import guard_comparison_conn

                if not await guard_comparison_conn(db, execution=execution, timestamp=timestamp):
                    updated = await _require_execution(db, execution_id)
                    result = FinalizeResult(execution=await _execution_from_row(db, updated), snapshot=None, decision=None)
                    await db.commit()
                    return result
                run_rows = await _comparison_runs(db, execution_id)
                if not run_rows or any(str(row["status"]) not in _TERMINAL_RUNS for row in run_rows):
                    await db.commit()
                    return FinalizeResult(execution=await _execution_from_row(db, execution), snapshot=None, decision=None)
                if str(execution["status"]) == "queued":
                    await db.execute(
                        """
                        UPDATE agent_eval_comparison_execution
                        SET status='running',started_at=?,row_version=row_version+1 WHERE id=?
                        """,
                        (timestamp, execution_id),
                    )
                    execution = await _require_execution(db, execution_id)
                await db.execute(
                    """
                    UPDATE agent_eval_comparison_execution
                    SET status='finalizing',finalizing_at=?,row_version=row_version+1
                    WHERE id=? AND status='running'
                    """,
                    (timestamp, execution_id),
                )
                execution = await _require_execution(db, execution_id)
                if str(execution["run_kind"]) == "diagnostic":
                    await db.execute(
                        """
                        UPDATE agent_eval_comparison_execution
                        SET status='completed',finished_at=?,row_version=row_version+1
                        WHERE id=? AND status='finalizing'
                        """,
                        (timestamp, execution_id),
                    )
                    await db.execute(
                        """
                        UPDATE agent_eval_candidate_control
                        SET active_execution_id=NULL,row_version=row_version+1,updated_at=?
                        WHERE candidate_id=? AND active_execution_id=?
                        """,
                        (timestamp, execution["candidate_id"], execution_id),
                    )
                    updated = await _require_execution(db, execution_id)
                    await db.commit()
                    return FinalizeResult(execution=await _execution_from_row(db, updated), snapshot=None, decision=None)
                context = await _finalize_context(db, execution=execution)
                stale = await _stale_reason(db, execution=execution, context=context)
                if stale is not None:
                    stale_subject, reason_code = stale
                    await _finalize_stale(
                        db,
                        db_path=self.db_path,
                        execution=execution,
                        context=context,
                        stale_subject=stale_subject,
                        reason_code=reason_code,
                        timestamp=timestamp,
                    )
                    updated = await _require_execution(db, execution_id)
                    result = FinalizeResult(execution=await _execution_from_row(db, updated), snapshot=None, decision=None)
                    await db.commit()
                    return result
                decision = await _decision(db, execution=execution, context=context, run_rows=run_rows)
                await db.execute(
                    """
                    UPDATE agent_eval_comparison_execution
                    SET status='completed',finished_at=?,row_version=row_version+1
                    WHERE id=? AND status='finalizing'
                    """,
                    (timestamp, execution_id),
                )
                snapshot_id = f"aevs_{uuid4().hex}"
                identity_json = _canonical_json(decision.identity_document)
                summary = {
                    "aggregate_metrics": decision.aggregate_metrics,
                    "case_comparisons": [asdict(item) for item in decision.case_comparisons],
                    "coverage_gaps": list(decision.coverage_gaps),
                    "reason_codes": list(decision.reason_codes),
                    "schema_version": 1,
                }
                summary_json = _canonical_json(summary)
                reviewer_json = _canonical_json(decision.reviewer_projection)
                await db.execute(
                    """
                    INSERT INTO agent_eval_snapshot(
                      id,team_id,agent_id,candidate_id,plan_id,execution_id,readiness,
                      identity_json,identity_hash,summary_json,reviewer_projection_json,
                      reviewer_projection_hash,snapshot_hash,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        snapshot_id,
                        execution["team_id"],
                        execution["agent_id"],
                        execution["candidate_id"],
                        execution["plan_id"],
                        execution_id,
                        decision.state,
                        identity_json,
                        _hash_text(identity_json),
                        summary_json,
                        reviewer_json,
                        _hash_text(reviewer_json),
                        decision.snapshot_hash,
                        timestamp,
                    ),
                )
                await db.execute(
                    """
                    UPDATE agent_eval_candidate_control
                    SET readiness=?,stale_subject=NULL,reason_codes_json=?,active_execution_id=NULL,
                        active_snapshot_id=?,active_override_id=NULL,row_version=row_version+1,updated_at=?
                    WHERE candidate_id=? AND active_plan_id=? AND active_execution_id=?
                    """,
                    (
                        decision.state,
                        _canonical_json(list(decision.reason_codes)),
                        snapshot_id,
                        timestamp,
                        execution["candidate_id"],
                        execution["plan_id"],
                        execution_id,
                    ),
                )
                await _notify(
                    db,
                    execution=execution,
                    snapshot_id=snapshot_id,
                    readiness=decision.state,
                    timestamp=timestamp,
                )
                await _audit(
                    db,
                    db_path=self.db_path,
                    team_id=str(execution["team_id"]),
                    agent_id=str(execution["agent_id"]),
                    actor_user_id=str(context["owner_user_id"]),
                    action=AuditAction.AGENT_EVAL_COMPARISON_FINALIZED,
                    payload={
                        "candidate_id": str(execution["candidate_id"]),
                        "execution_id": execution_id,
                        "readiness": decision.state,
                        "snapshot_id": snapshot_id,
                    },
                )
                updated = await _require_execution(db, execution_id)
                snapshot = await _snapshot_for_execution(db, execution_id)
                if snapshot is None:
                    raise AgentEvalComparisonError("agent_eval_snapshot_create_failed")
                result = FinalizeResult(
                    execution=await _execution_from_row(db, updated),
                    snapshot=_snapshot_from_row(snapshot),
                    decision=decision,
                )
                await db.commit()
                return result
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise AgentEvalComparisonError("agent_eval_comparison_finalize_conflict") from exc
            except BaseException:
                await db.rollback()
                raise

    async def get(self, execution_id: str) -> ComparisonExecution:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await _execution_from_row(db, await _require_execution(db, execution_id))

    async def list_finalizable(self, *, limit: int = 50) -> tuple[str, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("Agent Eval comparison recovery limit is invalid")
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                """
                SELECT execution.id
                FROM agent_eval_comparison_execution execution
                WHERE execution.status IN ('queued','running','finalizing')
                  AND EXISTS (
                    SELECT 1 FROM agent_eval_comparison_run link
                    WHERE link.execution_id=execution.id
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM agent_eval_comparison_run link
                    JOIN agent_eval_run run ON run.id=link.run_id
                    WHERE link.execution_id=execution.id
                      AND run.status NOT IN ('passed','failed','unknown','incomplete','cancelled')
                  )
                ORDER BY execution.requested_at,execution.id LIMIT ?
                """,
                (limit,),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return tuple(str(row[0]) for row in rows)

    def _timestamp(self) -> str:
        return self._clock.now().astimezone(UTC).isoformat()


async def _start_context(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    candidate_id: str,
    plan_id: str,
) -> tuple[aiosqlite.Row, aiosqlite.Row, aiosqlite.Row, aiosqlite.Row]:
    async with db.execute(
        """
        SELECT * FROM team_subagent_release_candidate
        WHERE team_id=? AND subagent_id=? AND id=?
        """,
        (team_id, agent_id, candidate_id),
    ) as cursor:
        candidate = await cursor.fetchone()
    async with db.execute("SELECT * FROM agent_eval_candidate_control WHERE candidate_id=?", (candidate_id,)) as cursor:
        control = await cursor.fetchone()
    async with db.execute(
        "SELECT * FROM agent_eval_plan WHERE team_id=? AND agent_id=? AND candidate_id=? AND id=?",
        (team_id, agent_id, candidate_id, plan_id),
    ) as cursor:
        plan = await cursor.fetchone()
    if candidate is None or control is None or plan is None:
        raise AgentEvalComparisonError("agent_eval_comparison_context_not_found")
    async with db.execute(
        "SELECT * FROM agent_eval_policy_revision WHERE id=?",
        (plan["policy_revision_id"],),
    ) as cursor:
        policy = await cursor.fetchone()
    if policy is None:
        raise AgentEvalComparisonError("agent_eval_policy_not_found")
    return candidate, control, plan, policy


async def _subject(
    db: aiosqlite.Connection,
    *,
    candidate: aiosqlite.Row,
    side: Literal["baseline", "candidate"],
    model_config_hashes: Mapping[str, str],
) -> EvalRunSubject:
    if side == "candidate":
        revision_ref = str(candidate["revision_id"])
        release_id = None
        candidate_id = str(candidate["id"])
        capability = str(candidate["capability_fingerprint"])
        dependency = str(candidate["dependency_fingerprint"])
    else:
        release_id = str(candidate["base_live_release_id"] or "")
        if not release_id:
            raise AgentEvalComparisonError("agent_eval_baseline_release_not_found")
        async with db.execute(
            """
            SELECT * FROM team_subagent_release
            WHERE team_id=? AND subagent_id=? AND id=?
            """,
            (candidate["team_id"], candidate["subagent_id"], release_id),
        ) as cursor:
            release = await cursor.fetchone()
        if release is None:
            raise AgentEvalComparisonError("agent_eval_baseline_release_not_found")
        revision_ref = str(release["revision_id"])
        candidate_id = None
        capability = str(release["capability_fingerprint"])
        dependency = str(release["dependency_fingerprint"])
    model_hash = model_config_hashes.get(revision_ref)
    if not isinstance(model_hash, str) or not model_hash.startswith("sha256:"):
        raise AgentEvalComparisonError("agent_eval_model_config_unavailable")
    return EvalRunSubject(
        team_id=str(candidate["team_id"]),
        agent_id=str(candidate["subagent_id"]),
        agent_source_kind="team",
        revision_ref=revision_ref,
        release_id=release_id,
        candidate_id=candidate_id,
        capability_fingerprint=capability,
        dependency_fingerprint=dependency,
        model_config_hash=model_hash,
    )


async def _decision(
    db: aiosqlite.Connection,
    *,
    execution: aiosqlite.Row,
    context: dict[str, Any],
    run_rows: list[aiosqlite.Row],
) -> ReadinessDecision:
    plan_document = _object(context["plan_json"], code="agent_eval_plan_malformed")
    policy = _object(context["policy_json"], code="agent_eval_policy_malformed")
    allow_subject_delta = "model" in (
        plan_document.get("allowed_tested_delta_manifest", {}).get("definition_fields", [])
        if isinstance(plan_document.get("allowed_tested_delta_manifest"), dict)
        else []
    )
    runs = {(str(row["suite_revision_id"]), str(row["subject_side"])): row for row in run_rows}
    cases: list[ComparisonCase] = []
    for suite in await _plan_suites(db, plan_id=str(execution["plan_id"])):
        async with db.execute(
            "SELECT id FROM agent_eval_case_revision WHERE suite_revision_id=? ORDER BY case_key,id",
            (suite["suite_revision_id"],),
        ) as cursor:
            case_rows = await cursor.fetchall()
        for case_row in case_rows:
            case_id = str(case_row["id"])
            baseline = ()
            if str(suite["execution_scope"]) == "paired":
                baseline = await _attempts(db, runs[(str(suite["suite_revision_id"]), "baseline")], case_id)
            candidate = await _attempts(db, runs[(str(suite["suite_revision_id"]), "candidate")], case_id)
            cases.append(
                ComparisonCase(
                    suite_revision_id=str(suite["suite_revision_id"]),
                    case_revision_id=case_id,
                    tier=cast(Any, str(suite["tier"])),
                    required=bool(suite["required"]),
                    attempts=int(suite["attempts"]),
                    execution_scope=cast(Any, str(suite["execution_scope"])),
                    baseline=baseline,
                    candidate=candidate,
                    allow_subject_provider_delta=allow_subject_delta,
                ),
            )
    limits = policy.get("cost_and_latency_limits")
    if not isinstance(limits, dict):
        raise AgentEvalComparisonError("agent_eval_policy_malformed")
    return project_readiness(
        tuple(cases),
        run_stop_reasons=await _run_stop_reasons(db, run_rows),
        pass_thresholds=cast(dict[str, float], policy.get("pass_thresholds", {})),
        minimum_delta=float(cast(dict[str, float], policy.get("minimum_deltas", {})).get("pass_rate", 0.05)),
        flaky_limits=cast(dict[str, float], policy.get("flaky_limits", {})),
        # A policy written before verdict rules has none: its plans keep the threshold rule they were made with.
        verdict_rules=cast(dict[str, str], policy.get("verdict_rules", {})),
        safety_status=str(context["safety_status"]),
        compatibility_verdict=str(context["compatibility_verdict"]),
        max_comparison_cost_microusd=int(limits["max_comparison_cost_microusd"]),
        execution_identity={
            "candidate_hash": str(context["candidate_hash"]),
            "candidate_id": str(execution["candidate_id"]),
            "execution_id": str(execution["id"]),
            "plan_hash": str(context["plan_hash"]),
            "plan_id": str(execution["plan_id"]),
            "runs": [
                {
                    "run_id": str(row["run_id"]),
                    "subject_fingerprint": str(row["subject_fingerprint"]),
                    "subject_side": str(row["subject_side"]),
                    "suite_revision_id": str(row["suite_revision_id"]),
                }
                for row in run_rows
            ],
            "schema_version": 1,
        },
    )


# A run stopped before any attempt (its preflight) for a reason the Owner acts on; without this the cases
# would only read "attempts missing".
_RUN_STOP_REASONS = {
    "agent_eval_model_unavailable": "run_model_unavailable",
    "team_model_unconfigured": "run_model_unavailable",
    "agent_eval_model_config_changed": "run_model_config_changed",
}


async def _run_stop_reasons(db: aiosqlite.Connection, run_rows: list[aiosqlite.Row]) -> tuple[str, ...]:
    reasons: set[str] = set()
    for row in run_rows:
        async with db.execute("SELECT status,summary_json FROM agent_eval_run WHERE id=?", (row["run_id"],)) as cursor:
            run = await cursor.fetchone()
        if run is None or run["summary_json"] is None or str(run["status"]) not in {"cancelled", "failed", "unknown", "incomplete"}:
            continue
        try:
            summary = json.loads(str(run["summary_json"]))
        except ValueError:
            continue
        reason = _RUN_STOP_REASONS.get(str(summary.get("failure_code") or "")) if isinstance(summary, dict) else None
        if reason is not None:
            reasons.add(reason)
    return tuple(sorted(reasons))


async def _attempts(db: aiosqlite.Connection, run: aiosqlite.Row, case_id: str) -> tuple[AttemptEvidence, ...]:
    from bowhead_core.agent_eval_datarc import datarc_live_enabled, datarc_query_digests

    # With the switch off, recorded Datarc evidence stays readable but is ignored (rollback).
    datarc_live = datarc_live_enabled()
    async with db.execute(
        """
        SELECT result.status,result.duration_ms,result.cost_microusd,result.failure_code,
               result.deterministic_json,result.judge_json,
               identity.subject_provider_ref,identity.subject_provider_hash,
               identity.judge_provider_ref,identity.judge_provider_hash,identity.reproducibility
        FROM agent_eval_case_result result
        LEFT JOIN agent_eval_attempt_identity identity
          ON identity.run_id=result.run_id
         AND identity.case_revision_id=result.case_revision_id
         AND identity.attempt=result.attempt
        WHERE result.run_id=? AND result.case_revision_id=? ORDER BY result.attempt
        """,
        (run["run_id"], case_id),
    ) as cursor:
        rows = await cursor.fetchall()
    return tuple(
        AttemptEvidence(
            status=cast(Any, str(row["status"])),
            subject_provider_ref=(str(row["subject_provider_ref"]) if row["subject_provider_ref"] is not None else None),
            subject_provider_hash=(str(row["subject_provider_hash"]) if row["subject_provider_hash"] is not None else None),
            judge_provider_ref=(str(row["judge_provider_ref"]) if row["judge_provider_ref"] is not None else None),
            judge_provider_hash=(str(row["judge_provider_hash"]) if row["judge_provider_hash"] is not None else None),
            reproducibility=cast(Any, str(row["reproducibility"] or "unknown")),
            duration_ms=(int(row["duration_ms"]) if row["duration_ms"] is not None else None),
            cost_microusd=(int(row["cost_microusd"]) if row["cost_microusd"] is not None else None),
            rule_passed=_rule_passed(row["deterministic_json"]),
            rubric_passed=_rubric_passed(_optional_object(row["judge_json"])),
            points_passed=_points_passed(_optional_object(row["judge_json"])),
            unmet_point_ids=unmet_blocking_point_ids(_optional_object(row["judge_json"])),
            failure_code=(str(row["failure_code"]) if row["failure_code"] is not None else None),
            datarc_digests=datarc_query_digests(_optional_object(row["deterministic_json"])) if datarc_live else (),
            failure_cause=_failure_cause(row["deterministic_json"]),
        )
        for row in rows
    )


def _failure_cause(value: object) -> str | None:
    document = _optional_object(value)
    failure = document.get("failure") if document is not None else None
    cause = failure.get("cause") if isinstance(failure, dict) else None
    return cause if isinstance(cause, str) and cause else None


def _rule_passed(value: object) -> bool | None:
    document = _optional_object(value)
    passed = document.get("passed") if document is not None else None
    return passed if isinstance(passed, bool) else None


def _rubric_passed(judge: Mapping[str, Any] | None) -> bool | None:
    # v1 evidence names its rubric; points-only evidence has none, so there is no rubric verdict.
    if judge is None or "rubric_ref" not in judge:
        return None
    return judge.get("passed") is True


def _points_passed(judge: Mapping[str, Any] | None) -> bool | None:
    passed = judge.get("points_passed") if judge is not None else None
    return passed if isinstance(passed, bool) else None


def _optional_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


async def _finalize_context(db: aiosqlite.Connection, *, execution: aiosqlite.Row) -> dict[str, Any]:
    async with db.execute(
        """
        SELECT candidate.candidate_hash,candidate.state,plan.candidate_hash AS plan_candidate_hash,
               plan.plan_hash,plan.plan_json,
               policy.policy_json,safety.status AS safety_status,compatibility.verdict AS compatibility_verdict,
               COALESCE(agent.owner_user_id,agent.created_by) AS owner_user_id,
               agent.live_release_id,candidate.base_live_release_id,plan.execution_workspace_id,
               plan.execution_workspace_scope_hash,candidate.row_version AS candidate_row_version,
               control.active_plan_id,control.active_execution_id
        FROM agent_eval_comparison_execution execution
        JOIN team_subagent_release_candidate candidate ON candidate.id=execution.candidate_id
        JOIN agent_eval_plan plan ON plan.id=execution.plan_id
        JOIN agent_eval_policy_revision policy ON policy.id=plan.policy_revision_id
        JOIN agent_safety_report safety ON safety.id=candidate.safety_report_id
        JOIN agent_revision_compatibility compatibility ON compatibility.revision_id=candidate.revision_id
        JOIN team_subagent agent ON agent.id=execution.agent_id AND agent.team_id=execution.team_id
        JOIN agent_eval_candidate_control control ON control.candidate_id=candidate.id
        WHERE execution.id=?
        """,
        (execution["id"],),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentEvalComparisonError("agent_eval_comparison_context_not_found")
    return dict(row)


async def _stale_reason(
    db: aiosqlite.Connection,
    *,
    execution: aiosqlite.Row,
    context: dict[str, Any],
) -> tuple[Literal["candidate", "evidence"], str] | None:
    if (
        str(context["state"]) != "prepared"
        or str(context["candidate_hash"]) != str(context["plan_candidate_hash"])
        or context["live_release_id"] != context["base_live_release_id"]
    ):
        return "candidate", "agent_eval_candidate_stale"
    if str(context["active_plan_id"] or "") != str(execution["plan_id"]) or str(context["active_execution_id"] or "") != str(execution["id"]):
        return "evidence", "agent_eval_plan_stale"
    current_scope_hash = await _workspace_scope_hash(
        db,
        team_id=str(execution["team_id"]),
        workspace_id=str(context["execution_workspace_id"]),
        actor_user_id=str(context["owner_user_id"]),
    )
    if current_scope_hash != str(context["execution_workspace_scope_hash"]):
        return "evidence", "agent_eval_workspace_scope_changed"
    return None


async def _finalize_stale(
    db: aiosqlite.Connection,
    *,
    db_path: str,
    execution: aiosqlite.Row,
    context: dict[str, Any],
    stale_subject: Literal["candidate", "evidence"],
    reason_code: str,
    timestamp: str,
) -> None:
    await db.execute(
        """
        UPDATE agent_eval_comparison_execution
        SET status='failed',finished_at=?,failure_code=?,row_version=row_version+1
        WHERE id=? AND status='finalizing'
        """,
        (timestamp, reason_code, execution["id"]),
    )
    if stale_subject == "candidate" and str(context["state"]) == "prepared":
        await db.execute(
            """
            UPDATE team_subagent_release_candidate
            SET state='stale',row_version=row_version+1,decided_by=?,decision_reason_code=?,
                self_review=1,decided_at=?,updated_at=?
            WHERE id=? AND state='prepared' AND row_version=?
            """,
            (
                context["owner_user_id"],
                reason_code,
                timestamp,
                timestamp,
                execution["candidate_id"],
                context["candidate_row_version"],
            ),
        )
        await AgentCandidateNameStore(db_path).release_names_conn(
            db,
            team_id=str(execution["team_id"]),
            agent_id=str(execution["agent_id"]),
            candidate_id=str(execution["candidate_id"]),
            timestamp=timestamp,
        )
    await db.execute(
        """
        UPDATE agent_eval_candidate_control
        SET readiness='stale',stale_subject=?,reason_codes_json=?,active_execution_id=NULL,
            row_version=row_version+1,updated_at=?
        WHERE candidate_id=? AND active_execution_id=?
        """,
        (
            stale_subject,
            _canonical_json([reason_code]),
            timestamp,
            execution["candidate_id"],
            execution["id"],
        ),
    )
    await _notify_stale(db, execution=execution, reason_code=reason_code, timestamp=timestamp)


async def _notify_stale(
    db: aiosqlite.Connection,
    *,
    execution: aiosqlite.Row,
    reason_code: str,
    timestamp: str,
) -> None:
    async with db.execute(
        "SELECT COALESCE(owner_user_id,created_by) FROM team_subagent WHERE team_id=? AND id=?",
        (execution["team_id"], execution["agent_id"]),
    ) as cursor:
        owner = await cursor.fetchone()
    if owner is None:
        return
    await db.execute(
        """
        INSERT INTO agent_version_notification_outbox(
          id,team_id,agent_id,recipient_user_id,event_type,candidate_id,plan_id,
          snapshot_id,release_id,dedupe_key,safe_payload_json,status,
          lease_generation,attempt_count,available_at,created_at
        ) VALUES(?,?,?,?,? ,?,?,NULL,NULL,?,?,'pending',0,0,?,?)
        """,
        (
            f"aevn_{uuid4().hex}",
            execution["team_id"],
            execution["agent_id"],
            owner[0],
            "candidate.stale",
            execution["candidate_id"],
            execution["plan_id"],
            f"agent-version:{execution['id']}:candidate.stale",
            _canonical_json(
                {
                    "candidate_id": str(execution["candidate_id"]),
                    "execution_id": str(execution["id"]),
                    "reason_code": reason_code,
                    "schema_version": 1,
                },
            ),
            timestamp,
            timestamp,
        ),
    )


async def _comparison_runs(db: aiosqlite.Connection, execution_id: str) -> list[aiosqlite.Row]:
    async with db.execute(
        """
        SELECT link.execution_id,link.suite_revision_id,link.subject_side,link.run_id,
               run.status,run.subject_fingerprint
        FROM agent_eval_comparison_run link
        JOIN agent_eval_run run ON run.id=link.run_id
        WHERE link.execution_id=? ORDER BY link.suite_revision_id,link.subject_side
        """,
        (execution_id,),
    ) as cursor:
        return list(await cursor.fetchall())


async def _cancel_run_conn(db: aiosqlite.Connection, *, run: aiosqlite.Row, timestamp: str) -> None:
    if str(run["status"]) == "queued":
        await db.execute(
            "UPDATE agent_eval_run SET status='running',started_at=? WHERE id=? AND status='queued'",
            (timestamp, run["id"]),
        )
    async with db.execute(
        "SELECT * FROM agent_eval_case_job WHERE run_id=? AND status NOT IN ('completed','failed','cancelled')",
        (run["id"],),
    ) as cursor:
        jobs = await cursor.fetchall()
    for job in jobs:
        if str(job["status"]) == "queued":
            await db.execute(
                """
                UPDATE agent_eval_case_job
                SET status='leased',lease_generation=1,lease_owner='comparison-cancel',
                    lease_token_hash=?,lease_expires_at=?
                WHERE id=? AND status='queued'
                """,
                (_hash_text(f"cancel:{job['id']}"), timestamp, job["id"]),
            )
        await db.execute(
            """
            INSERT OR IGNORE INTO agent_eval_case_result(
              run_id,case_revision_id,attempt,status,deterministic_json,judge_json,
              trace_evidence_ref,duration_ms,cost_microusd,failure_code,created_at
            ) VALUES(?,?,?,'cancelled',?,NULL,NULL,NULL,NULL,
                     'agent_eval_comparison_cancelled',?)
            """,
            (
                run["id"],
                job["case_revision_id"],
                job["attempt"],
                _canonical_json({"critical_failure": False, "passed": False, "scorers": []}),
                timestamp,
            ),
        )
        await db.execute(
            """
            INSERT OR IGNORE INTO agent_eval_attempt_identity(
              run_id,case_revision_id,attempt,subject_provider_ref,subject_provider_hash,
              judge_provider_ref,judge_provider_hash,reproducibility,created_at
            ) VALUES(?,?,?,'model-config',?,?,?,'exact',?)
            """,
            (
                run["id"],
                job["case_revision_id"],
                job["attempt"],
                run["model_config_hash"],
                run["judge_role_ref"],
                run["judge_model_fingerprint"],
                timestamp,
            ),
        )
        await db.execute(
            """
            UPDATE agent_eval_case_job
            SET status='cancelled',ended_at=?,failure_code='agent_eval_comparison_cancelled'
            WHERE id=? AND status IN ('leased','running')
            """,
            (timestamp, job["id"]),
        )
    await db.execute(
        """
        UPDATE agent_eval_run
        SET status='cancelled',summary_json=?,finished_at=?
        WHERE id=? AND status IN ('queued','running')
        """,
        (
            _canonical_json({"failure_code": "agent_eval_comparison_cancelled", "schema_version": 1}),
            timestamp,
            run["id"],
        ),
    )


async def _judge_identity(
    db: aiosqlite.Connection,
    *,
    suite_revision_id: str,
    judge_model_fingerprint: str | None,
) -> tuple[str | None, str | None]:
    async with db.execute(
        "SELECT judge_policy_json FROM agent_eval_suite_revision WHERE id=?",
        (suite_revision_id,),
    ) as cursor:
        row = await cursor.fetchone()
    policy = _object(row["judge_policy_json"], code="agent_eval_judge_policy_invalid") if row is not None else {}
    if policy.get("enabled"):
        if judge_model_fingerprint is None:
            raise AgentEvalComparisonError("agent_eval_judge_identity_unavailable")
        return "judge", judge_model_fingerprint
    # Expected points are graded when the Team has an evaluation model; without one they report
    # "not graded" and the rules decide (PRD R17), so a missing model never stops the comparison.
    if judge_model_fingerprint is None:
        return None, None
    async with db.execute(f"SELECT {suite_revision_grades_points_sql('?')} AS grades_points", (suite_revision_id,)) as cursor:
        points = await cursor.fetchone()
    return ("judge", judge_model_fingerprint) if points is not None and bool(points["grades_points"]) else (None, None)


def _subject_sides(suite: Mapping[str, Any]) -> tuple[Literal["baseline", "candidate"], ...]:
    return ("baseline", "candidate") if str(suite["execution_scope"]) == "paired" else ("candidate",)


async def _workspace_scope_hash(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    workspace_id: str,
    actor_user_id: str,
) -> str | None:
    async with db.execute(
        """
        SELECT workspace.id,workspace.revision,member.role,COALESCE(policy.epoch,0) AS policy_epoch
        FROM workspaces workspace
        JOIN users actor ON actor.id=? AND actor.team_id=workspace.team_id AND actor.status='active'
        JOIN team_members team_member ON team_member.team_id=workspace.team_id
          AND team_member.user_id=actor.id AND team_member.status='active'
        JOIN workspace_members member ON member.workspace_id=workspace.id
          AND member.user_id=actor.id AND member.status='active'
        LEFT JOIN access_policy_state policy ON policy.team_id=workspace.team_id
        WHERE workspace.id=? AND workspace.team_id=? AND workspace.status='active'
        """,
        (actor_user_id, workspace_id, team_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return _hash_document(
        {
            "policy_epoch": int(row["policy_epoch"]),
            "schema_version": 1,
            "workspace_id": str(row["id"]),
            "workspace_revision": int(row["revision"] or 1),
            "workspace_role": str(row["role"]),
        },
    )


async def _require_owner(db: aiosqlite.Connection, *, team_id: str, agent_id: str, actor_user_id: str) -> None:
    async with db.execute(
        """
        SELECT 1 FROM team_subagent agent
        JOIN teams team ON team.id=agent.team_id AND team.status='active'
        JOIN users actor ON actor.id=? AND actor.team_id=agent.team_id AND actor.status='active'
        JOIN team_members member ON member.team_id=agent.team_id
          AND member.user_id=actor.id AND member.status='active'
        WHERE agent.team_id=? AND agent.id=? AND agent.lifecycle_state='active'
          AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
        """,
        (actor_user_id, team_id, agent_id),
    ) as cursor:
        if await cursor.fetchone() is None:
            raise AgentEvalComparisonError("agent_eval_comparison_forbidden")


async def _plan_suites(db: aiosqlite.Connection, *, plan_id: str) -> list[aiosqlite.Row]:
    async with db.execute(
        "SELECT * FROM agent_eval_plan_suite WHERE plan_id=? ORDER BY order_index",
        (plan_id,),
    ) as cursor:
        return list(await cursor.fetchall())


async def _selected_cases(
    db: aiosqlite.Connection,
    *,
    suites: list[aiosqlite.Row],
    run_kind: Literal["release", "diagnostic"],
    selected_case_revision_ids: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    selected = set(selected_case_revision_ids)
    if run_kind == "release" and selected:
        raise AgentEvalComparisonError("agent_eval_release_selection_forbidden")
    result: dict[str, tuple[str, ...]] = {}
    known: set[str] = set()
    for suite in suites:
        suite_id = str(suite["suite_revision_id"])
        async with db.execute(
            "SELECT id FROM agent_eval_case_revision WHERE suite_revision_id=? ORDER BY case_key,id",
            (suite_id,),
        ) as cursor:
            all_cases = tuple(str(row[0]) for row in await cursor.fetchall())
        known.update(all_cases)
        chosen = all_cases if run_kind == "release" else tuple(case_id for case_id in all_cases if case_id in selected)
        if chosen:
            result[suite_id] = chosen
    if run_kind == "diagnostic" and (not result or selected - known):
        raise AgentEvalComparisonError("agent_eval_diagnostic_selection_invalid")
    return result


async def _execution(db: aiosqlite.Connection, execution_id: str) -> aiosqlite.Row | None:
    async with db.execute("SELECT * FROM agent_eval_comparison_execution WHERE id=?", (execution_id,)) as cursor:
        return await cursor.fetchone()


async def _require_execution(db: aiosqlite.Connection, execution_id: str) -> aiosqlite.Row:
    row = await _execution(db, execution_id)
    if row is None:
        raise AgentEvalComparisonError("agent_eval_comparison_not_found")
    return row


async def _execution_by_idempotency(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    actor_kind: Literal["user", "automation"],
    actor_ref: str,
    idempotency_hash: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        """
        SELECT * FROM agent_eval_comparison_execution
        WHERE team_id=? AND actor_kind=? AND actor_ref=? AND idempotency_hash=?
        """,
        (team_id, actor_kind, actor_ref, idempotency_hash),
    ) as cursor:
        return await cursor.fetchone()


async def _run_links(db: aiosqlite.Connection, execution_id: str) -> tuple[ComparisonRunLink, ...]:
    async with db.execute(
        "SELECT * FROM agent_eval_comparison_run WHERE execution_id=? ORDER BY suite_revision_id,subject_side",
        (execution_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return tuple(
        ComparisonRunLink(
            suite_revision_id=str(row["suite_revision_id"]),
            subject_side=cast(Any, str(row["subject_side"])),
            run_id=str(row["run_id"]),
        )
        for row in rows
    )


async def _execution_from_row(db: aiosqlite.Connection, row: aiosqlite.Row) -> ComparisonExecution:
    return ComparisonExecution(
        id=str(row["id"]),
        team_id=str(row["team_id"]),
        agent_id=str(row["agent_id"]),
        candidate_id=str(row["candidate_id"]),
        plan_id=str(row["plan_id"]),
        run_kind=cast(Any, str(row["run_kind"])),
        status=cast(Any, str(row["status"])),
        selection_hash=str(row["selection_hash"]),
        request_hash=str(row["request_hash"]),
        actor_kind=cast(Any, str(row["actor_kind"])),
        actor_ref=str(row["actor_ref"]),
        generation=int(row["generation"]),
        row_version=int(row["row_version"]),
        requested_at=str(row["requested_at"]),
        finished_at=(str(row["finished_at"]) if row["finished_at"] is not None else None),
        failure_code=(str(row["failure_code"]) if row["failure_code"] is not None else None),
        runs=await _run_links(db, str(row["id"])),
        client_intent_hash=row["client_intent_hash"],
        automation_context=json.loads(row["automation_context_json"]) if row["automation_context_json"] is not None else None,
        coach_delegation_id=row["coach_delegation_id"],
    )


async def _snapshot_for_execution(db: aiosqlite.Connection, execution_id: str) -> aiosqlite.Row | None:
    async with db.execute("SELECT * FROM agent_eval_snapshot WHERE execution_id=?", (execution_id,)) as cursor:
        return await cursor.fetchone()


def _snapshot_from_row(row: Mapping[str, Any]) -> EvalSnapshot:
    return EvalSnapshot(
        id=str(row["id"]),
        candidate_id=str(row["candidate_id"]),
        plan_id=str(row["plan_id"]),
        execution_id=str(row["execution_id"]),
        readiness=str(row["readiness"]),
        identity_hash=str(row["identity_hash"]),
        reviewer_projection_hash=str(row["reviewer_projection_hash"]),
        snapshot_hash=str(row["snapshot_hash"]),
        created_at=str(row["created_at"]),
    )


async def _prior_readiness(db: aiosqlite.Connection, *, candidate_id: str) -> tuple[str, list[str]]:
    async with db.execute(
        """
        SELECT snapshot.readiness,snapshot.summary_json
        FROM agent_eval_candidate_control control
        LEFT JOIN agent_eval_snapshot snapshot ON snapshot.id=control.active_snapshot_id
        WHERE control.candidate_id=?
        """,
        (candidate_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or row["readiness"] is None:
        return "missing", []
    summary = _object(row["summary_json"], code="agent_eval_snapshot_malformed")
    reasons = summary.get("reason_codes", [])
    return str(row["readiness"]), [str(item) for item in reasons] if isinstance(reasons, list) else []


async def _notify(
    db: aiosqlite.Connection,
    *,
    execution: aiosqlite.Row,
    snapshot_id: str,
    readiness: str,
    timestamp: str,
) -> None:
    event_type = {
        "ready": "candidate.ready",
        "ready_with_risks": "candidate.ready",
        "blocked": "candidate.blocked",
        "unknown": "candidate.unknown",
    }[readiness]
    async with db.execute(
        "SELECT COALESCE(owner_user_id,created_by) FROM team_subagent WHERE team_id=? AND id=?",
        (execution["team_id"], execution["agent_id"]),
    ) as cursor:
        owner = await cursor.fetchone()
    if owner is None:
        raise AgentEvalComparisonError("agent_eval_owner_not_found")
    for kind in ("comparison.completed", event_type):
        await db.execute(
            """
            INSERT INTO agent_version_notification_outbox(
              id,team_id,agent_id,recipient_user_id,event_type,candidate_id,plan_id,
              snapshot_id,release_id,dedupe_key,safe_payload_json,status,
              lease_generation,attempt_count,available_at,created_at
            ) VALUES(?,?,?,?,?,?,?,?,NULL,?,?,'pending',0,0,?,?)
            """,
            (
                f"aevn_{uuid4().hex}",
                execution["team_id"],
                execution["agent_id"],
                owner[0],
                kind,
                execution["candidate_id"],
                execution["plan_id"],
                snapshot_id,
                f"agent-version:{execution['id']}:{kind}",
                _canonical_json(
                    {
                        "candidate_id": str(execution["candidate_id"]),
                        "execution_id": str(execution["id"]),
                        "readiness": readiness,
                        "schema_version": 1,
                        "snapshot_id": snapshot_id,
                    },
                ),
                timestamp,
                timestamp,
            ),
        )


async def _audit(
    db: aiosqlite.Connection,
    *,
    db_path: str,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
    action: str,
    payload: dict[str, Any],
) -> None:
    await UserStore(db_path).write_admin_audit_log(
        actor_user_id=actor_user_id,
        team_id=team_id,
        action=action,
        target_kind="team_subagent",
        target_id=agent_id,
        target_team_id=team_id,
        scope="team",
        payload=payload,
        conn=db,
        audit_id=f"aud_{uuid4().hex}",
    )


def _object(value: object, *, code: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise AgentEvalComparisonError(code) from exc
    if not isinstance(decoded, dict):
        raise AgentEvalComparisonError(code)
    return decoded


def _canonical_json(value: object) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _hash_document(value: object) -> str:
    return _hash_text(_canonical_json(value))


__all__ = [
    "AgentEvalComparisonError",
    "AgentEvalComparisonStore",
    "ComparisonExecution",
    "ComparisonRunLink",
    "EvalSnapshot",
    "FinalizeResult",
]
