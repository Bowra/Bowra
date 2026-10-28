"""M3-10 durable Agent Eval execution service.

The service owns orchestration, budgets, scratch Turn lifecycle, deterministic
scoring, optional Judge handling, and evidence projection.  Product HTTP/UI
producers and automatic change-trigger outboxes remain M3-11.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from bowhead_router.invocation import enclosing_physical_guard_scope, physical_invocation_scope
from bowhead_router.usage import usage_context

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_automation_execution import AutomationExecutionGuard
from bowhead_core.agent_eval_attempt_failure import (
    CANCELLED_CODE,
    JUDGE_NOT_CONFIGURED_CODE,
    AttemptFailure,
    CancelCause,
    classify_attempt_exception,
)
from bowhead_core.agent_eval_effects import (
    EvalEffectAdapterRegistry,
    EvalEffectRouter,
    EvalFixtureResolver,
    EvalTraceCollector,
)
from bowhead_core.agent_eval_job_store import (
    AgentEvalJobStore,
    AgentEvalJobStoreError,
    ClaimedEvalCaseJob,
    EvalRunExecution,
)
from bowhead_core.agent_eval_judge import EvalPointsGrading, TeamRouterEvalJudge
from bowhead_core.agent_eval_point_calibration import AgentEvalPointStore
from bowhead_core.agent_eval_points import (
    graded_point_rows,
    has_model_graded_points,
    model_graded_points,
    points_evidence,
    settle_points,
)
from bowhead_core.agent_eval_scorers import (
    MANDATORY_SCORERS,
    build_mandatory_scorers,
    deterministic_result,
    required_tool_groups,
    required_tools_not_run_in_eval,
    tools_not_run_in_eval,
    validate_core_scoring_contract,
)
from bowhead_core.agent_eval_store import (
    AgentEvalStore,
    AgentEvalStoreError,
    EvalCaseRevision,
    EvalRun,
)
from bowhead_core.agent_eval_usage import AGENT_EVAL_USAGE_REASON, EvalAttemptUsageCounter
from bowhead_core.agent_version_automation import AgentVersionAutomationError
from bowhead_core.evalkit.cases import EvalCase
from bowhead_core.evalkit.runner import EvalRunner
from bowhead_core.evalkit.scorers import Scorer
from bowhead_core.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


class AgentEvalServiceError(RuntimeError):
    """Finite execution-service failure safe to expose as an error code."""

    def __init__(self, code: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        # Safe facts that name the failing condition (for example the model role and why it has no model).
        self.details: dict[str, Any] = dict(details or {})


class _EvalCancellationRequested(RuntimeError):
    """The attempt must stop: its comparison was stopped, or the run lost its authority."""

    def __init__(self, cause: CancelCause = "stopped") -> None:
        super().__init__(cause)
        self.cause: CancelCause = cause


class _EvalDurationExceeded(RuntimeError):
    """The run's wall-clock budget ran out while this attempt was in flight."""


class _EvalAttemptTimeLimit(RuntimeError):
    """This attempt alone ran longer than its own time limit."""


class _EvalDatarcStopped(RuntimeError):
    """The live Datarc route ended the attempt (an environment cause, or too little time for a call)."""


# Failure codes of an attempt cut by a time limit. They settle as ``infra_failed`` (the result
# says nothing about quality) but are reported as a time limit, never as an environment fault.
ATTEMPT_TIME_LIMIT_CODE = "agent_eval_attempt_timeout"
RUN_TIME_LIMIT_CODE = "agent_eval_duration_budget_exceeded"
TIME_LIMIT_FAILURE_CODES = frozenset({ATTEMPT_TIME_LIMIT_CODE, RUN_TIME_LIMIT_CODE})
# The Agent called a Tool the case requires, but the evaluation had no adapter to run it.
REQUIRED_TOOL_NOT_RUN_CODE = "agent_eval_required_tool_not_run"
# The answer depended on a Tool the evaluation can't run yet (for example a Datarc query): the case
# can't be tested in evaluation, which is neither a pass nor the Agent's failure.
TOOL_NOT_RUN_CODE = "agent_eval_tool_not_run_in_eval"
# Slack for the evalkit runner's own timeout so the claim monitor, which records which limit was
# hit and how long the attempt ran, is the one that ends an over-long attempt.
_RUNNER_TIMEOUT_SLACK_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class EvalExecutionLimits:
    attempts: int = 1
    max_concurrency: int = 1
    max_duration_ms: int = 120_000
    max_cost_microusd: int = 5_000_000
    lease_seconds: int = 120

    def __post_init__(self) -> None:
        if not 1 <= self.attempts <= 5:
            raise ValueError("Agent Eval attempts must be between 1 and 5")
        if not 1 <= self.max_concurrency <= 8:
            raise ValueError("Agent Eval concurrency must be between 1 and 8")
        if not 1_000 <= self.max_duration_ms <= 3_600_000:
            raise ValueError("Agent Eval duration budget is invalid")
        if not 0 <= self.max_cost_microusd <= 1_000_000_000:
            raise ValueError("Agent Eval cost budget is invalid")
        if not 10 <= self.lease_seconds <= 3_600:
            raise ValueError("Agent Eval lease duration is invalid")


@dataclass(frozen=True, slots=True)
class EvalAttemptRequest:
    execution_mode: Literal["eval"]
    run: EvalRun
    case: EvalCaseRevision
    attempt: int
    workspace_id: str
    session_id: str
    turn_id: str
    safe_tool_registry: ToolRegistry


@dataclass(frozen=True, slots=True)
class EvalAttemptOutput:
    output: Any
    terminal_status: str
    duration_ms: int
    cost_microusd: int
    tokens_input: int = 0
    tokens_output: int = 0
    trace: Mapping[str, Any] = field(default_factory=dict)


class ExactAgentEvalExecutor(Protocol):
    """Runtime adapter that must materialize ``request.run``'s exact revision."""

    async def execute(self, request: EvalAttemptRequest) -> EvalAttemptOutput: ...


class EvalToolViewProvider(Protocol):
    """Open the current team-scoped registry; MCP resources may need a context."""

    def open_tool_view(
        self,
        *,
        run: EvalRun,
        workspace_id: str,
        actor_user_id: str,
    ) -> contextlib.AbstractAsyncContextManager[ToolRegistry]: ...


class EvalSafetyGate(Protocol):
    async def require_current(self, run: EvalRun) -> None: ...


class EvalModelConfigGate(Protocol):
    """Verify that Router resolution still matches the run-pinned model hash."""

    async def require_current(self, run: EvalRun) -> None: ...


class EvalAdmissionGate(Protocol):
    async def authorize(
        self,
        *,
        team_id: str,
        user_id: str,
        case_count: int,
        attempts: int,
    ) -> bool: ...


@dataclass(slots=True)
class AgentEvalService:
    eval_store: AgentEvalStore
    job_store: AgentEvalJobStore
    session_store: Any
    executor: ExactAgentEvalExecutor
    tool_view_provider: EvalToolViewProvider
    fixture_resolver: EvalFixtureResolver | None
    adapter_registry: EvalEffectAdapterRegistry
    safety_gate: EvalSafetyGate
    model_config_gate: EvalModelConfigGate
    admission_gate: EvalAdmissionGate
    judge: TeamRouterEvalJudge | None = None
    schema_registry: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    scorer_bundle_factories: Mapping[str, Callable[[], list[Scorer]]] = field(default_factory=dict)
    runtime_authority_check: Callable[[str], Awaitable[None]] | None = None
    # FEATURE_AGENT_EVAL_DATARC_READ_ONLY: the run's live read-only Datarc scope (agent_eval_datarc.EvalDatarcLiveRun).
    # None (switch off, system Agents) keeps every Datarc call denied as not run.
    datarc_live: Any | None = None

    def _authority_guard(self, run_id: str, *, claim: ClaimedEvalCaseJob | None = None) -> AutomationExecutionGuard:
        return AutomationExecutionGuard(
            self.eval_store.db_path,
            run_id,
            job_id=claim.job.job_id if claim else None,
            lease_generation=claim.job.lease_generation if claim else None,
            runtime_check=self.runtime_authority_check,
        )

    async def prepare_run(
        self,
        *,
        run_id: str,
        workspace_id: str,
        limits: EvalExecutionLimits = EvalExecutionLimits(),
    ) -> EvalRun:
        """Persist the complete execution plan before background work is acknowledged."""

        run = await self.eval_store.read_run(run_id)
        if run is None:
            raise AgentEvalServiceError("agent_eval_run_not_found")
        if not await self._authority_guard(run_id).allowed():
            stopped = await self.eval_store.read_run(run_id)
            if stopped is None:
                raise AgentEvalServiceError("agent_eval_run_not_found")
            return stopped
        revision = await self.eval_store.read_suite_revision(run.suite_revision_id)
        if revision is None:
            raise AgentEvalServiceError("agent_eval_suite_revision_not_found")
        if revision.case_set_hash != run.case_set_hash:
            raise AgentEvalServiceError("agent_eval_case_set_changed")
        if run.status not in {"queued", "running"}:
            return run
        suite = await self.eval_store.read_suite(revision.suite_id)
        if suite is None:
            raise AgentEvalServiceError("agent_eval_suite_not_found")
        try:
            cases = {case.case_revision_id: case for case in revision.cases}
            for case in cases.values():
                _task_text(case)
                _validate_case_execution_contract(case)
                if run.scorer_bundle_ref == "evalkit.core@1":
                    try:
                        validate_core_scoring_contract(case.expected_contract, case.scorer_config)
                    except ValueError as exc:
                        raise AgentEvalServiceError("agent_eval_scoring_contract_invalid") from exc
            rubric_ref = revision.judge_policy.get("rubric_ref")
            # A pinned judge needs something to judge: a v1 rubric, or expected points to grade.
            judges_points = any(has_model_graded_points(case.expected_contract) for case in cases.values())
            if run.judge_role_ref is not None and (run.judge_role_ref != "judge" or not (isinstance(rubric_ref, str) or judges_points)):
                raise AgentEvalServiceError("agent_eval_judge_policy_invalid")
            # Resolve once before admission so an unavailable or malformed exact
            # bundle fails the whole run without consuming an execution job.  Each
            # attempt creates fresh instances below to avoid state leaking between
            # concurrently scored cases.
            self._scorers_for_bundle(run.scorer_bundle_ref)
        except AgentEvalServiceError as exc:
            return await self._stop_preflight(
                run=run,
                suite_id=suite.suite_id,
                failure_code=exc.code,
                critical_failure=False,
            )
        try:
            await self.safety_gate.require_current(run)
        except Exception as exc:
            failure_code = str(getattr(exc, "code", "") or "agent_eval_safety_gate_failed")
            return await self._stop_preflight(
                run=run,
                suite_id=suite.suite_id,
                failure_code=failure_code,
                critical_failure=failure_code in {"agent_safety_blocked", "agent_prompt_blocked", "agent_listing_blocked"},
            )
        try:
            await self.model_config_gate.require_current(run)
        except Exception as exc:
            failure_code = str(getattr(exc, "code", "") or "agent_eval_model_config_changed")
            logger.warning(
                "agent_eval.run_stopped_before_attempts run=%s failure_code=%s details=%s",
                run.run_id,
                failure_code,
                getattr(exc, "details", None) or {},
            )
            return await self._stop_preflight(
                run=run,
                suite_id=suite.suite_id,
                failure_code=failure_code,
                critical_failure=False,
                details=getattr(exc, "details", None),
            )
        if run.status == "queued":
            allowed = await self.admission_gate.authorize(
                team_id=run.subject.team_id,
                user_id=run.created_by,
                case_count=revision.case_count,
                attempts=limits.attempts,
            )
            if not allowed:
                return await self.eval_store.mark_run_terminal(
                    run.run_id,
                    status="cancelled",
                    summary={
                        "critical_failure": False,
                        "failure_code": "agent_eval_rate_limited",
                        "schema_version": 1,
                    },
                )
            await self.job_store.materialize_jobs(
                run_id=run.run_id,
                workspace_id=workspace_id,
                attempts=limits.attempts,
                max_concurrency=limits.max_concurrency,
                max_duration_ms=limits.max_duration_ms,
                max_cost_microusd=limits.max_cost_microusd,
                case_revision_ids=await _comparison_case_selection(self.eval_store.db_path, run.run_id),
            )
            run = await self.eval_store.mark_run_running(run.run_id)
        await self._project_evidence(
            run=run,
            suite_id=suite.suite_id,
            state="running",
        )
        return run

    async def execute_run(
        self,
        *,
        run_id: str,
        workspace_id: str,
        worker_id: str,
        limits: EvalExecutionLimits = EvalExecutionLimits(),
    ) -> EvalRun:
        run = await self.prepare_run(
            run_id=run_id,
            workspace_id=workspace_id,
            limits=limits,
        )
        if run.status != "running":
            return run
        revision = await self.eval_store.read_suite_revision(run.suite_revision_id)
        if revision is None:
            raise AgentEvalServiceError("agent_eval_suite_revision_not_found")
        cases = {case.case_revision_id: case for case in revision.cases}
        rubric_ref = revision.judge_policy.get("rubric_ref")
        plan = await self.job_store.read_execution(run.run_id)
        if plan is None:
            raise AgentEvalServiceError("agent_eval_execution_not_found")
        self._require_matching_limits(plan, workspace_id=workspace_id, limits=limits)
        started = time.perf_counter() - _elapsed_since_seconds(plan.created_at)
        existing_results = await self.eval_store.list_case_results(run.run_id)
        budget = _SharedBudget(
            cost_microusd=sum(item.cost_microusd or 0 for item in existing_results),
            duration_ms=sum(item.duration_ms or 0 for item in existing_results),
        )

        async def _worker(index: int) -> None:
            while True:
                if await self._should_stop(plan, started=started, budget=budget):
                    return
                claim = await self.job_store.claim_next_job(
                    run_id=run.run_id,
                    worker_id=f"{worker_id}.{index}",
                    lease_seconds=limits.lease_seconds,
                )
                if claim is None:
                    jobs = await self.job_store.list_jobs(run.run_id)
                    if any(job.status in {"queued", "leased", "running"} for job in jobs):
                        await asyncio.sleep(min(1.0, limits.lease_seconds / 3))
                        continue
                    return
                case = cases.get(claim.job.case_revision_id)
                if case is None:
                    raise AgentEvalServiceError("agent_eval_case_not_found")
                await self._execute_claim(
                    claim=claim,
                    run=run,
                    case=case,
                    plan=plan,
                    started=started,
                    budget=budget,
                    lease_seconds=limits.lease_seconds,
                    rubric_ref=rubric_ref if isinstance(rubric_ref, str) else None,
                )

        await asyncio.gather(*(_worker(index) for index in range(plan.max_concurrency)))
        return await self._finish_run(run=run, revision_case_count=revision.case_count, plan=plan)

    async def _execute_claim(
        self,
        *,
        claim: ClaimedEvalCaseJob,
        run: EvalRun,
        case: EvalCaseRevision,
        plan: EvalRunExecution,
        started: float,
        budget: _SharedBudget,
        lease_seconds: int,
        rubric_ref: str | None,
    ) -> None:
        session_id = f"s_eval_{claim.job.job_id.removeprefix('aej_')[:24]}_{claim.job.lease_generation}"
        turn_id = f"t_eval_{claim.job.job_id.removeprefix('aej_')[:24]}_{claim.job.lease_generation}"
        usage = EvalAttemptUsageCounter()
        # Observe subject and Judge usage for this attempt without the durable physical-send
        # contract; it also attributes Eval rows in `llm_usage_events` to the scratch Turn.
        with usage_context(
            team_id=run.subject.team_id,
            user_id=run.created_by,
            workspace_id=plan.workspace_id,
            turn_id=turn_id,
            reason=AGENT_EVAL_USAGE_REASON,
            turn_usage_sink=usage.observe,
        ):
            await self._execute_attempt(
                claim=claim,
                run=run,
                case=case,
                plan=plan,
                started=started,
                budget=budget,
                lease_seconds=lease_seconds,
                rubric_ref=rubric_ref,
                session_id=session_id,
                turn_id=turn_id,
                usage=usage,
            )

    async def _execute_attempt(
        self,
        *,
        claim: ClaimedEvalCaseJob,
        run: EvalRun,
        case: EvalCaseRevision,
        plan: EvalRunExecution,
        started: float,
        budget: _SharedBudget,
        lease_seconds: int,
        rubric_ref: str | None,
        session_id: str,
        turn_id: str,
        usage: EvalAttemptUsageCounter,
    ) -> None:
        task = _task_text(case)
        await self.job_store.start_scratch_turn(
            job_id=claim.job.job_id,
            lease_token=claim.lease_token,
            session_id=session_id,
            turn_id=turn_id,
            user_text=task,
        )
        trace_collector = EvalTraceCollector(
            max_tool_calls=_case_limit(
                case,
                "max_tool_calls",
                default=200,
                minimum=0,
                maximum=200,
            ),
        )
        from bowhead_core.number_verifier import extract_numbers

        # Owner-supplied case input is a numeric source; expected answers and
        # model output must never authorize their own figures.
        extract_numbers(task, trace_collector.authorized_numbers)
        trace_collector.numeric_input = bool(trace_collector.authorized_numbers)
        # The per-attempt limit bounds the whole attempt (the Agent, then its grading); the run's
        # own budget is a separate, larger wall-clock bound checked alongside it.
        attempt_limit_ms = _case_limit(
            case,
            "max_duration_ms",
            default=120_000,
            minimum=1_000,
            maximum=3_600_000,
        )
        policy_attempt_limit_ms = await _comparison_attempt_limit_ms(self.eval_store.db_path, run.run_id)
        if policy_attempt_limit_ms is not None:
            attempt_limit_ms = min(attempt_limit_ms, policy_attempt_limit_ms)
        deadline = _AttemptDeadline(
            run_started=started,
            run_limit_ms=plan.max_duration_ms,
            attempt_started=time.perf_counter(),
            attempt_limit_ms=attempt_limit_ms,
        )
        # Waiting for a Datarc slot and every Datarc call count against this attempt's deadline.
        datarc = (
            self.datarc_live.open_attempt(reference=claim.job.job_id, remaining_ms=deadline.remaining_ms) if self.datarc_live is not None else None
        )
        router = EvalEffectRouter(
            effect_policy=case.effect_policy,
            fixture_ref=case.fixture_ref,
            fixture_resolver=self.fixture_resolver,
            adapter_registry=self.adapter_registry,
            trace=trace_collector,
            authority_check=self._authority_guard(run.run_id).require,
            datarc_live=datarc,
        )
        case_timeout = max(0.001, deadline.remaining_ms() / 1000) + _RUNNER_TIMEOUT_SLACK_SECONDS
        output: EvalAttemptOutput | None = None
        # The runner keeps only the exception's text; the object says which cause it was.
        agent_error: BaseException | None = None
        try:
            scorers = self._scorers_for_bundle(run.scorer_bundle_ref)
            async with self.tool_view_provider.open_tool_view(
                run=run,
                workspace_id=plan.workspace_id,
                actor_user_id=run.created_by,
            ) as live_view:
                safe_view = router.build_safe_view(live_view)
                request = EvalAttemptRequest(
                    execution_mode="eval",
                    run=run,
                    case=case,
                    attempt=claim.job.attempt,
                    workspace_id=plan.workspace_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    safe_tool_registry=safe_view,
                )

                async def _agent(_: EvalCase) -> tuple[Any, dict[str, Any]]:
                    nonlocal output, agent_error
                    try:
                        output = await self.executor.execute(request)
                    except Exception as exc:
                        agent_error = exc
                        raise
                    trace = dict(output.trace)
                    trace.update(trace_collector.to_scorer_trace())
                    trace.update(
                        {
                            "cost_microusd": output.cost_microusd,
                            "duration_ms": output.duration_ms,
                            "terminal_status": output.terminal_status,
                        }
                    )
                    return output.output, trace

                eval_case = EvalCase(
                    id=case.case_revision_id,
                    input=case.input,
                    expected=case.expected_contract,
                    tags=[],
                    meta={"scorer_config": case.scorer_config},
                )
                report = await self._run_with_claim_controls(
                    operation=EvalRunner(
                        scorers=scorers,
                        concurrency=1,
                        per_case_timeout_sec=case_timeout,
                        dataset_name=run.suite_revision_id,
                    ).run([eval_case], _agent),
                    claim=claim,
                    run_id=run.run_id,
                    lease_seconds=lease_seconds,
                    deadline=deadline,
                    stop_event=datarc.stop_event if datarc is not None else None,
                )
            result = report.case_results[0]
            if datarc is not None and datarc.stop is not None:
                # The Agent's run finished before the route's stop reached it; the stop still decides.
                raise _EvalDatarcStopped
            if result.error is not None and "timed out" in result.error:
                await self._settle_time_limit(claim=claim, run=run, turn_id=turn_id, deadline=deadline, scope=deadline.binding_scope(), usage=usage)
                return
            if result.error is not None or output is None:
                await self._settle_attempt_exception(
                    claim=claim,
                    run=run,
                    turn_id=turn_id,
                    exc=agent_error,
                    failed_tool=trace_collector.failed_tool,
                    usage=usage,
                    datarc=datarc,
                )
                return
            denied_tool = (
                datarc.owner_permission_denied(required_tool_groups(scorer_config=case.scorer_config, expected=case.expected_contract))
                if datarc is not None
                else None
            )
            if datarc is not None and denied_tool is not None:
                # Every call to a Datarc Tool the case requires failed for lack of the Owner's Datarc permission.
                from bowhead_core.agent_eval_datarc import DATARC_UNAVAILABLE_CODE, OWNER_PERMISSION_DENIED, failure_document

                await self._settle_infra_failure(
                    claim=claim,
                    run=run,
                    turn_id=turn_id,
                    failure_code=DATARC_UNAVAILABLE_CODE,
                    usage=usage,
                    evidence={
                        **failure_document(OWNER_PERMISSION_DENIED, reference=claim.job.job_id, tool=denied_tool),
                        **datarc.evidence(),
                    },
                    duration_ms=max(0, output.duration_ms),
                )
                return
            not_run = required_tools_not_run_in_eval(
                scorer_config=case.scorer_config,
                expected=case.expected_contract,
                tool_calls=trace_collector.tool_calls,
            )
            if not_run:
                # The answer was produced without the work the case requires, so it can't pass, and
                # the Agent did call the Tool, so it isn't the Agent's failure either.
                await self._settle_infra_failure(
                    claim=claim,
                    run=run,
                    turn_id=turn_id,
                    failure_code=REQUIRED_TOOL_NOT_RUN_CODE,
                    usage=usage,
                    evidence={"required_tools_not_run": not_run, **(datarc.evidence() if datarc is not None else {})},
                    duration_ms=max(0, output.duration_ms),
                )
                return
            depended_on = tools_not_run_in_eval(
                tool_calls=trace_collector.tool_calls,
                terminal_status=output.terminal_status,
                output=output.output,
            )
            deterministic = deterministic_result(
                result.scores,
                scorer_config=case.scorer_config,
            )
            if datarc is not None:
                # Counts, times, outcomes and two keyed digests per row query; never rows or query text (§6.1).
                deterministic = {**deterministic, **datarc.evidence()}
            judge_evidence: dict[str, Any] | None = None
            settlement: Literal["passed", "failed", "judge_unknown"] = "passed" if deterministic["passed"] else "failed"
            failure_code: str | None = None
            judge_cost_microusd = 0
            judge_duration_ms = 0
            if deterministic["passed"] and run.judge_role_ref is not None and rubric_ref is not None:
                if self.judge is None or run.judge_model_fingerprint is None:
                    # No evaluation model is set up for this run: testing again can't help until one is.
                    settlement = "judge_unknown"
                    failure_code = JUDGE_NOT_CONFIGURED_CODE
                    judge_evidence = {"failure_code": failure_code}
                else:
                    judged = await self._run_with_claim_controls(
                        operation=self.judge.judge(
                            task=task,
                            output=output.output,
                            rubric_ref=rubric_ref,
                            role_ref=run.judge_role_ref,
                            expected_model_fingerprint=run.judge_model_fingerprint,
                        ),
                        claim=claim,
                        run_id=run.run_id,
                        lease_seconds=lease_seconds,
                        deadline=deadline,
                    )
                    judge_evidence = judged.evidence
                    failure_code = judged.failure_code
                    settlement = "judge_unknown" if judged.status == "unknown" else ("passed" if judged.status == "passed" else "failed")
                    judge_cost_microusd = judged.cost_microusd
                    judge_duration_ms = judged.duration_ms
            points = model_graded_points(case.expected_contract)
            if points:
                grading: EvalPointsGrading | None = None
                if self.judge is not None and run.judge_role_ref is not None and run.judge_model_fingerprint is not None:
                    grading = await self._run_with_claim_controls(
                        operation=self.judge.grade_points(
                            task=task,
                            output=output.output,
                            points=points,
                            role_ref=run.judge_role_ref,
                            expected_model_fingerprint=run.judge_model_fingerprint,
                        ),
                        claim=claim,
                        run_id=run.run_id,
                        lease_seconds=lease_seconds,
                        deadline=deadline,
                    )
                    judge_cost_microusd += grading.cost_microusd
                    judge_duration_ms += grading.duration_ms
                # A point calibrated under another prompt or model is informational for this run; the
                # Owner's eval view schedules its recalibration (technical design §7.2).
                calibrated = await AgentEvalPointStore(self.eval_store.db_path).calibrated_point_ids(
                    case_revision_id=case.case_revision_id,
                    points=points,
                    model_fingerprint=run.judge_model_fingerprint,
                )
                rows = graded_point_rows(
                    points,
                    grading,
                    data_read=trace_collector.data_read,
                    authorized_numbers=trace_collector.authorized_numbers,
                    calibrated_point_ids=calibrated,
                )
                document = points_evidence(rows, grading, model_fingerprint=run.judge_model_fingerprint)
                # Judge evidence exists only for a run with a pinned judge (`trg_agent_eval_case_result_scope`);
                # without one, every graded point reads as not graded from the run itself.
                if run.judge_role_ref is not None and judge_evidence is None:
                    judge_evidence = document
                elif run.judge_role_ref is not None and judge_evidence is not None:
                    # A suite with both a v1 rubric and points keeps the rubric evidence at the top level.
                    judge_evidence = {
                        **judge_evidence,
                        "points": document["points"],
                        "points_passed": document["points_passed"],
                        "points_grading": {key: value for key, value in document.items() if key not in {"points", "points_passed"}},
                    }
                if settlement == "passed":
                    settlement = settle_points(rule_passed=True, graded=rows)
            total_cost_microusd = max(0, output.cost_microusd) + judge_cost_microusd
            total_duration_ms = max(0, output.duration_ms) + judge_duration_ms
            # The answer was produced without a Tool the evaluation can't run yet (a data query, say): not
            # the Agent's failure and not a verdict, and a pass without the data proves nothing either. The
            # platform's safety case checks only that the Agent stays within limits, which a denied data
            # query doesn't change, so there a pass keeps its pass.
            if depended_on and (settlement != "passed" or not await _platform_contract_case(self.eval_store.db_path, run.suite_revision_id)):
                await self._settle_infra_failure(
                    claim=claim,
                    run=run,
                    turn_id=turn_id,
                    failure_code=TOOL_NOT_RUN_CODE,
                    usage=usage,
                    evidence={"tools_not_run": depended_on, **(datarc.evidence() if datarc is not None else {})},
                    duration_ms=total_duration_ms,
                    cost_microusd=total_cost_microusd,
                )
                await budget.add(cost_microusd=total_cost_microusd, duration_ms=total_duration_ms)
                return
            await self.session_store.mark_completed(
                turn_id=turn_id,
                intent="agent_eval",
                final_answer=_render_output(output.output),
                tokens_input=max(0, output.tokens_input),
                tokens_output=max(0, output.tokens_output),
                cost_usd=max(0, output.cost_microusd) / 1_000_000,
            )
            await self.session_store.finalize_turn(
                session_id=session_id,
                current_turn_id=turn_id,
                next_turn_id=None,
            )
            await self.job_store.settle_job(
                job_id=claim.job.job_id,
                lease_token=claim.lease_token,
                status=settlement,
                deterministic=deterministic,
                judge=judge_evidence,
                trace_evidence_ref=f"turn:{turn_id}",
                duration_ms=total_duration_ms,
                cost_microusd=total_cost_microusd,
                failure_code=failure_code,
                usage=usage.snapshot(),
                **_attempt_identity(run),
            )
            await budget.add(
                cost_microusd=total_cost_microusd,
                duration_ms=total_duration_ms,
            )
        except AgentVersionAutomationError as exc:
            if exc.code != "agent_automation_authority_lost":
                raise
            await self._settle_cancelled(claim=claim, run=run, turn_id=turn_id, usage=usage, cause="authority_lost")
        except _EvalCancellationRequested as exc:
            await self._settle_cancelled(claim=claim, run=run, turn_id=turn_id, usage=usage, cause=exc.cause)
        except _EvalDurationExceeded:
            await self._settle_time_limit(claim=claim, run=run, turn_id=turn_id, deadline=deadline, scope="run", usage=usage, datarc=datarc)
        except _EvalAttemptTimeLimit:
            await self._settle_time_limit(claim=claim, run=run, turn_id=turn_id, deadline=deadline, scope="attempt", usage=usage, datarc=datarc)
        except _EvalDatarcStopped:
            await self._settle_datarc_stop(claim=claim, run=run, turn_id=turn_id, deadline=deadline, usage=usage, datarc=datarc)
        except asyncio.CancelledError:
            # The worker itself is stopping (a restart or shutdown), not the Owner.
            await self._settle_cancelled(claim=claim, run=run, turn_id=turn_id, usage=usage, cause="restart")
            raise
        except (AgentEvalJobStoreError, AgentEvalServiceError):
            raise
        except Exception as exc:
            await self._settle_attempt_exception(
                claim=claim,
                run=run,
                turn_id=turn_id,
                exc=exc,
                failed_tool=trace_collector.failed_tool,
                usage=usage,
                datarc=datarc,
            )
        finally:
            if datarc is not None:
                # Rows exist only in the attempt store; it and the Tool's Turn caches end with the attempt.
                datarc.close()

    async def _settle_datarc_stop(
        self,
        *,
        claim: ClaimedEvalCaseJob,
        run: EvalRun,
        turn_id: str,
        deadline: _AttemptDeadline,
        usage: EvalAttemptUsageCounter,
        datarc: Any,
    ) -> None:
        """Settle an attempt the live Datarc route stopped: its environment cause, or the time limit."""

        from bowhead_core.agent_eval_datarc import DATARC_UNAVAILABLE_CODE

        failure = datarc.failure_evidence() if datarc is not None else None
        if failure is None:
            # Too little time was left to start or wait for a Datarc call: the attempt's time limit.
            await self._settle_time_limit(
                claim=claim, run=run, turn_id=turn_id, deadline=deadline, scope=deadline.binding_scope(), usage=usage, datarc=datarc
            )
            return
        logger.warning(
            "agent_eval.attempt_datarc_unavailable reference=%s run=%s case=%s attempt=%s cause=%s tool=%s",
            claim.job.job_id,
            run.run_id,
            claim.job.case_revision_id,
            claim.job.attempt,
            failure["failure"].get("cause"),
            failure["failure"].get("tool") or "-",
        )
        await self._settle_infra_failure(
            claim=claim,
            run=run,
            turn_id=turn_id,
            failure_code=DATARC_UNAVAILABLE_CODE,
            usage=usage,
            evidence={**failure, **datarc.evidence()},
            duration_ms=deadline.attempt_elapsed_ms(),
        )

    async def _run_with_claim_controls(
        self,
        *,
        operation: Awaitable[Any],
        claim: ClaimedEvalCaseJob,
        run_id: str,
        lease_seconds: int,
        deadline: _AttemptDeadline,
        stop_event: asyncio.Event | None = None,
    ) -> Any:
        guard = self._authority_guard(run_id, claim=claim)

        async def guarded_operation() -> Any:
            try:
                await guard.require()
            except BaseException:
                if hasattr(operation, "close"):
                    operation.close()
                raise
            if await guard.requires_physical_guard():
                if await guard.internal_delegation():
                    with enclosing_physical_guard_scope(guard):
                        return await operation
                with physical_invocation_scope(guard):
                    return await operation
            return await operation

        operation_task = asyncio.create_task(guarded_operation())
        monitor_task = asyncio.create_task(
            self._monitor_claim(
                claim=claim,
                run_id=run_id,
                lease_seconds=lease_seconds,
                deadline=deadline,
            )
        )
        # The live Datarc route ends the attempt at once on an environment cause, not at the next poll.
        stop_task = asyncio.create_task(stop_event.wait()) if stop_event is not None else None
        try:
            done, _pending = await asyncio.wait(
                {operation_task, monitor_task, *((stop_task,) if stop_task is not None else ())},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation_task in done:
                return await operation_task
            if stop_task is not None and stop_task in done:
                operation_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await operation_task
                raise _EvalDatarcStopped
            stop_reason = await monitor_task
            operation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await operation_task
            if stop_reason == "duration":
                raise _EvalDurationExceeded
            if stop_reason == "attempt_duration":
                raise _EvalAttemptTimeLimit
            raise _EvalCancellationRequested("authority_lost" if stop_reason == "authority" else "stopped")
        finally:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
            if stop_task is not None:
                stop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_task
            if not operation_task.done():
                operation_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await operation_task

    async def _monitor_claim(
        self,
        *,
        claim: ClaimedEvalCaseJob,
        run_id: str,
        lease_seconds: int,
        deadline: _AttemptDeadline,
    ) -> Literal["cancelled", "authority", "duration", "attempt_duration"]:
        interval = min(1.0, lease_seconds / 3)
        while True:
            if not await self._authority_guard(run_id).allowed():
                return "authority"
            if deadline.remaining_ms() <= 0:
                return "duration" if deadline.binding_scope() == "run" else "attempt_duration"
            execution = await self.job_store.read_execution(run_id)
            if execution is None:
                raise AgentEvalServiceError("agent_eval_execution_not_found")
            if execution.cancel_requested_at is not None:
                return "cancelled"
            await asyncio.sleep(min(interval, max(0.0, deadline.remaining_ms() / 1000)))
            if deadline.remaining_ms() <= 0:
                return "duration" if deadline.binding_scope() == "run" else "attempt_duration"
            await self.job_store.renew_lease(
                job_id=claim.job.job_id,
                lease_token=claim.lease_token,
                lease_seconds=lease_seconds,
            )

    async def _settle_cancelled(
        self,
        *,
        claim: ClaimedEvalCaseJob,
        run: EvalRun,
        turn_id: str,
        usage: EvalAttemptUsageCounter | None = None,
        cause: CancelCause = "stopped",
    ) -> None:
        turn = await self.session_store.get_turn(turn_id)
        if turn is not None:
            await self.session_store.mark_cancelled(turn_id=turn_id, error_kind="agent_eval_cancelled")
            await self.session_store.finalize_turn(
                session_id=turn.session_id,
                current_turn_id=turn_id,
                next_turn_id=None,
            )
        await self.job_store.settle_job(
            job_id=claim.job.job_id,
            lease_token=claim.lease_token,
            status="cancelled",
            deterministic={
                "critical_failure": False,
                "passed": False,
                "scorers": [],
                "failure": {"category": "cancelled", "cause": cause, "reference": claim.job.job_id},
            },
            trace_evidence_ref=f"turn:{turn_id}",
            failure_code=CANCELLED_CODE,
            # Spent usage is real even though the result is not.
            usage=usage.snapshot() if usage is not None else None,
            **_attempt_identity(run),
        )

    async def _settle_attempt_exception(
        self,
        *,
        claim: ClaimedEvalCaseJob,
        run: EvalRun,
        turn_id: str,
        exc: BaseException | None,
        failed_tool: str | None,
        usage: EvalAttemptUsageCounter | None = None,
        datarc: Any = None,
    ) -> None:
        """Settle an attempt the Agent's run ended without an answer, keeping what caused it.

        The Owner sees the category and the attempt's reference; the exception itself stays in this log
        line under the same reference, so an admin can look it up.
        """

        failure = (
            classify_attempt_exception(exc, failed_tool=failed_tool) if exc is not None else AttemptFailure("agent_eval_attempt_failed", "platform")
        )
        reference = claim.job.job_id
        logger.error(
            "agent_eval.attempt_failed reference=%s run=%s case=%s attempt=%s failure_code=%s category=%s cause=%s tool=%s err_type=%s",
            reference,
            run.run_id,
            claim.job.case_revision_id,
            claim.job.attempt,
            failure.failure_code,
            failure.category,
            failure.cause or "-",
            failure.tool or "-",
            type(exc).__name__ if exc is not None else "-",
            exc_info=(type(exc), exc, exc.__traceback__) if exc is not None else None,
        )
        await self._settle_infra_failure(
            claim=claim,
            run=run,
            turn_id=turn_id,
            failure_code=failure.failure_code,
            usage=usage,
            evidence={**failure.evidence(reference=reference), **(datarc.evidence() if datarc is not None else {})},
        )

    async def _settle_infra_failure(
        self,
        *,
        claim: ClaimedEvalCaseJob,
        run: EvalRun,
        turn_id: str,
        failure_code: str,
        usage: EvalAttemptUsageCounter | None = None,
        evidence: Mapping[str, Any] | None = None,
        duration_ms: int | None = None,
        cost_microusd: int | None = None,
    ) -> None:
        turn = await self.session_store.get_turn(turn_id)
        if turn is not None:
            await self.session_store.mark_failed(
                turn_id=turn_id,
                error=failure_code,
                error_kind="agent_eval_infrastructure",
            )
            await self.session_store.finalize_turn(
                session_id=turn.session_id,
                current_turn_id=turn_id,
                next_turn_id=None,
            )
        await self.job_store.settle_job(
            job_id=claim.job.job_id,
            lease_token=claim.lease_token,
            status="infra_failed",
            deterministic={"critical_failure": False, "passed": False, "scorers": [], **(evidence or {})},
            trace_evidence_ref=f"turn:{turn_id}",
            duration_ms=duration_ms,
            cost_microusd=cost_microusd,
            failure_code=failure_code,
            usage=usage.snapshot() if usage is not None else None,
            **_attempt_identity(run),
        )

    async def _settle_time_limit(
        self,
        *,
        claim: ClaimedEvalCaseJob,
        run: EvalRun,
        turn_id: str,
        deadline: _AttemptDeadline,
        scope: Literal["attempt", "run"],
        usage: EvalAttemptUsageCounter | None = None,
        datarc: Any = None,
    ) -> None:
        """Record a time-limit cut with the limit that was hit and how long the attempt ran.

        It settles as ``infra_failed`` because the attempt has no verdict, but its failure code and
        ``time_limit`` evidence say what happened, so it is never shown as an environment fault.
        """

        observed_ms = deadline.attempt_elapsed_ms()
        failure_code = RUN_TIME_LIMIT_CODE if scope == "run" else ATTEMPT_TIME_LIMIT_CODE
        limit_ms = deadline.run_limit_ms if scope == "run" else deadline.attempt_limit_ms
        turn = await self.session_store.get_turn(turn_id)
        if turn is not None:
            await self.session_store.mark_failed(
                turn_id=turn_id,
                error=failure_code,
                error_kind="agent_eval_time_limit",
            )
            await self.session_store.finalize_turn(
                session_id=turn.session_id,
                current_turn_id=turn_id,
                next_turn_id=None,
            )
        await self.job_store.settle_job(
            job_id=claim.job.job_id,
            lease_token=claim.lease_token,
            status="infra_failed",
            deterministic={
                "critical_failure": False,
                "passed": False,
                "scorers": [],
                "time_limit": {"limit_ms": limit_ms, "observed_ms": observed_ms, "scope": scope},
                # Datarc calls made before the cut (§4.5: the time-limit copy names a data case).
                **(datarc.evidence() if datarc is not None else {}),
            },
            trace_evidence_ref=f"turn:{turn_id}",
            duration_ms=observed_ms,
            failure_code=failure_code,
            usage=usage.snapshot() if usage is not None else None,
            **_attempt_identity(run),
        )

    async def _should_stop(
        self,
        plan: EvalRunExecution,
        *,
        started: float,
        budget: _SharedBudget,
    ) -> bool:
        current = await self.job_store.read_execution(plan.run_id)
        if current is None or current.cancel_requested_at is not None:
            return True
        run = await self.eval_store.read_run(plan.run_id)
        if run is None or run.status != "running":
            return True
        cost, _duration = await budget.snapshot()
        durable_results = await self.eval_store.list_case_results(plan.run_id)
        cost = max(cost, sum(item.cost_microusd or 0 for item in durable_results))
        return cost >= plan.max_cost_microusd or int((time.perf_counter() - started) * 1000) >= plan.max_duration_ms

    async def _finish_run(
        self,
        *,
        run: EvalRun,
        revision_case_count: int,
        plan: EvalRunExecution,
    ) -> EvalRun:
        current = await self.eval_store.read_run(run.run_id)
        if current is not None and current.status in {"passed", "failed", "unknown", "incomplete", "cancelled"}:
            return current
        jobs = await self.job_store.list_jobs(run.run_id)
        results = await self.eval_store.list_case_results(run.run_id)
        by_case: dict[str, list[Any]] = {}
        for result in results:
            by_case.setdefault(result.case_revision_id, []).append(result)
        pass_at_k = sum(any(item.status == "passed" for item in values) for values in by_case.values())
        pass_power_k = sum(len(values) == plan.requested_attempts and all(item.status == "passed" for item in values) for values in by_case.values())
        flaky = sum(
            any(item.status == "passed" for item in values) and any(item.status != "passed" for item in values) for values in by_case.values()
        )
        critical_failure = any(bool(item.deterministic.get("critical_failure")) for item in results)
        completed_all = len(results) == revision_case_count * plan.requested_attempts
        has_quality_failure = any(item.status == "failed" for item in results)
        has_unknown = any(item.status == "judge_unknown" for item in results)
        has_infra = any(item.status in {"infra_failed", "cancelled"} for item in results)
        execution = await self.job_store.read_execution(run.run_id)
        cancelled = execution is not None and execution.cancel_requested_at is not None
        if cancelled:
            terminal = "cancelled"
        elif not completed_all or has_infra or any(job.status not in {"completed", "failed", "cancelled"} for job in jobs):
            terminal = "incomplete"
        elif has_quality_failure:
            terminal = "failed"
        elif has_unknown:
            terminal = "unknown"
        else:
            terminal = "passed"
        summary = {
            "attempts": plan.requested_attempts,
            "case_count": revision_case_count,
            "completed_results": len(results),
            "cost_microusd": sum(item.cost_microusd or 0 for item in results),
            "critical_failure": critical_failure,
            "duration_ms": sum(item.duration_ms or 0 for item in results),
            "flaky_cases": flaky,
            "infra_failures": sum(item.status == "infra_failed" and item.failure_code not in TIME_LIMIT_FAILURE_CODES for item in results),
            "time_limit_failures": sum(item.status == "infra_failed" and item.failure_code in TIME_LIMIT_FAILURE_CODES for item in results),
            "judge_unknown": sum(item.status == "judge_unknown" for item in results),
            "pass@k_cases": pass_at_k,
            "pass^k_cases": pass_power_k,
            "schema_version": 1,
        }
        total_cost = int(summary["cost_microusd"])
        elapsed_ms = int(_elapsed_since_seconds(plan.created_at) * 1000)
        if not completed_all and total_cost >= plan.max_cost_microusd:
            summary["stop_reason"] = "agent_eval_cost_budget_exceeded"
        elif any(item.failure_code == RUN_TIME_LIMIT_CODE for item in results) or (not completed_all and elapsed_ms >= plan.max_duration_ms):
            summary["stop_reason"] = RUN_TIME_LIMIT_CODE
        finished = await self.eval_store.mark_run_terminal(
            run.run_id,
            status=terminal,  # type: ignore[arg-type]
            summary=summary,
        )
        state = {
            "passed": "valid",
            "failed": "blocked" if critical_failure else "failed",
            "unknown": "unknown",
            "incomplete": "unknown",
            "cancelled": "unknown",
        }[terminal]
        suite_revision = await self.eval_store.read_suite_revision(run.suite_revision_id)
        if suite_revision is None:
            return finished
        await self._project_evidence(
            run=run,
            suite_id=suite_revision.suite_id,
            state=state,
        )
        return finished

    async def _project_evidence(
        self,
        *,
        run: EvalRun,
        suite_id: str,
        state: str,
    ) -> None:
        async with (
            open_shared_ctx(self.eval_store.db_path) as db,
            db.execute(
                "SELECT 1 FROM agent_eval_comparison_run WHERE run_id=?",
                (run.run_id,),
            ) as cursor,
        ):
            if await cursor.fetchone() is not None:
                return
        current = await self.eval_store.read_evidence(
            team_id=run.subject.team_id,
            agent_source_kind=run.subject.agent_source_kind,
            agent_id=run.subject.agent_id,
            suite_id=suite_id,
        )
        if (
            current is not None
            and current.current_run_id == run.run_id
            and current.subject_fingerprint == run.subject_fingerprint
            and current.state == state
        ):
            return
        try:
            await self.eval_store.project_evidence(
                team_id=run.subject.team_id,
                agent_source_kind=run.subject.agent_source_kind,
                agent_id=run.subject.agent_id,
                suite_id=suite_id,
                subject_fingerprint=run.subject_fingerprint,
                state=state,  # type: ignore[arg-type]
                current_run_id=run.run_id,
                expected_row_version=(current.row_version if current is not None else None),
            )
        except AgentEvalStoreError as exc:
            if exc.code != "agent_eval_evidence_version_conflict":
                raise
            refreshed = await self.eval_store.read_evidence(
                team_id=run.subject.team_id,
                agent_source_kind=run.subject.agent_source_kind,
                agent_id=run.subject.agent_id,
                suite_id=suite_id,
            )
            if (
                refreshed is not None
                and refreshed.current_run_id == run.run_id
                and refreshed.subject_fingerprint == run.subject_fingerprint
                and refreshed.state == state
            ):
                return
            raise

    async def _stop_preflight(
        self,
        *,
        run: EvalRun,
        suite_id: str,
        failure_code: str,
        critical_failure: bool,
        details: Mapping[str, Any] | None = None,
    ) -> EvalRun:
        stopped = await self.eval_store.mark_run_terminal(
            run.run_id,
            status="cancelled",
            summary={
                "critical_failure": critical_failure,
                "failure_code": failure_code,
                **({"failure_details": dict(details)} if details else {}),
                "schema_version": 1,
            },
        )
        await self._project_evidence(
            run=run,
            suite_id=suite_id,
            state="unknown",
        )
        return stopped

    @staticmethod
    def _require_matching_limits(
        plan: EvalRunExecution,
        *,
        workspace_id: str,
        limits: EvalExecutionLimits,
    ) -> None:
        if (
            plan.workspace_id,
            plan.requested_attempts,
            plan.max_concurrency,
            plan.max_duration_ms,
            plan.max_cost_microusd,
        ) != (
            workspace_id,
            limits.attempts,
            limits.max_concurrency,
            limits.max_duration_ms,
            limits.max_cost_microusd,
        ):
            raise AgentEvalServiceError("agent_eval_execution_plan_conflict")

    def _scorers_for_bundle(self, exact_ref: str) -> list[Scorer]:
        scorers = build_mandatory_scorers(schema_registry=self.schema_registry)
        if exact_ref == "evalkit.core@1":
            return scorers
        factory = self.scorer_bundle_factories.get(exact_ref)
        if factory is None:
            raise AgentEvalServiceError("agent_eval_scorer_bundle_unavailable")
        try:
            additional = factory()
        except Exception as exc:
            raise AgentEvalServiceError("agent_eval_scorer_bundle_unavailable") from exc
        if not isinstance(additional, list):
            raise AgentEvalServiceError("agent_eval_scorer_bundle_invalid")
        names = [str(getattr(scorer, "name", "")) for scorer in additional]
        if (
            any(not name or len(name) > 200 for name in names)
            or any(not callable(getattr(scorer, "score", None)) for scorer in additional)
            or len(names) != len(set(names))
        ):
            raise AgentEvalServiceError("agent_eval_scorer_bundle_invalid")
        if set(names) & set(MANDATORY_SCORERS):
            raise AgentEvalServiceError("agent_eval_scorer_bundle_overrides_mandatory")
        return [*scorers, *additional]


@dataclass(frozen=True, slots=True)
class _AttemptDeadline:
    """The two wall-clock bounds an attempt runs under: its own limit and the run's budget."""

    run_started: float
    run_limit_ms: int
    attempt_started: float
    attempt_limit_ms: int

    def attempt_elapsed_ms(self) -> int:
        return max(0, int((time.perf_counter() - self.attempt_started) * 1000))

    def _run_remaining_ms(self) -> int:
        return self.run_limit_ms - int((time.perf_counter() - self.run_started) * 1000)

    def _attempt_remaining_ms(self) -> int:
        return self.attempt_limit_ms - self.attempt_elapsed_ms()

    def remaining_ms(self) -> int:
        return min(self._run_remaining_ms(), self._attempt_remaining_ms())

    def binding_scope(self) -> Literal["attempt", "run"]:
        """Which limit ends first; the run's budget wins a tie because it stops the whole run."""

        return "run" if self._run_remaining_ms() <= self._attempt_remaining_ms() else "attempt"


@dataclass(slots=True)
class _SharedBudget:
    cost_microusd: int = 0
    duration_ms: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def add(self, *, cost_microusd: int, duration_ms: int) -> None:
        async with self._lock:
            self.cost_microusd += cost_microusd
            self.duration_ms += duration_ms

    async def snapshot(self) -> tuple[int, int]:
        async with self._lock:
            return self.cost_microusd, self.duration_ms


def _task_text(case: EvalCaseRevision) -> str:
    value = case.input.get("task")
    if not isinstance(value, str) or not value.strip():
        raise AgentEvalServiceError("agent_eval_case_task_missing")
    return value.strip()


def _validate_case_execution_contract(case: EvalCaseRevision) -> None:
    _case_limit(
        case,
        "max_tool_calls",
        default=200,
        minimum=0,
        maximum=200,
    )
    _case_limit(
        case,
        "max_duration_ms",
        default=120_000,
        minimum=1_000,
        maximum=3_600_000,
    )
    _case_limit(
        case,
        "max_cost_microusd",
        default=5_000_000,
        minimum=0,
        maximum=1_000_000_000,
    )
    if case.valid_until is None:
        return
    try:
        valid_until = datetime.fromisoformat(case.valid_until.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AgentEvalServiceError("agent_eval_fixture_expiry_invalid") from exc
    if valid_until.tzinfo is None:
        raise AgentEvalServiceError("agent_eval_fixture_expiry_invalid")
    if valid_until.astimezone(UTC) <= datetime.now(UTC):
        raise AgentEvalServiceError("agent_eval_fixture_expired")


def _case_limit(
    case: EvalCaseRevision,
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = case.scorer_config.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
        raise AgentEvalServiceError("agent_eval_case_scorer_config_invalid")
    return raw


def _render_output(output: Any) -> str:
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(output)


def _attempt_identity(run: EvalRun) -> dict[str, str | None]:
    """Persist the provider identities already pinned and rechecked for this attempt."""

    return {
        "subject_provider_ref": "model-config",
        "subject_provider_hash": run.subject.model_config_hash,
        "judge_provider_ref": run.judge_role_ref,
        "judge_provider_hash": run.judge_model_fingerprint,
        "reproducibility": "exact",
    }


async def _comparison_case_selection(db_path: str, run_id: str) -> tuple[str, ...] | None:
    async with (
        open_shared_ctx(db_path) as db,
        db.execute(
            """
        SELECT execution.run_kind,execution.selection_json,run.suite_revision_id
        FROM agent_eval_comparison_run link
        JOIN agent_eval_comparison_execution execution ON execution.id=link.execution_id
        JOIN agent_eval_run run ON run.id=link.run_id
        WHERE link.run_id=?
        """,
            (run_id,),
        ) as cursor,
    ):
        row = await cursor.fetchone()
    if row is None or str(row[0]) != "diagnostic":
        return None
    try:
        selection = json.loads(str(row[1]))
    except (TypeError, ValueError) as exc:
        raise AgentEvalServiceError("agent_eval_diagnostic_selection_invalid") from exc
    by_suite = selection.get("cases_by_suite") if isinstance(selection, dict) else None
    case_ids = by_suite.get(str(row[2])) if isinstance(by_suite, dict) else None
    if not isinstance(case_ids, list) or not case_ids or any(not isinstance(item, str) for item in case_ids):
        raise AgentEvalServiceError("agent_eval_diagnostic_selection_invalid")
    return tuple(case_ids)


async def _platform_contract_case(db_path: str, suite_revision_id: str) -> bool:
    """Whether a case belongs to the platform-managed safety contract suite, not the Owner's own cases."""

    async with (
        open_shared_ctx(db_path) as db,
        db.execute(
            """
        SELECT suite.suite_kind
        FROM agent_eval_suite_revision revision
        JOIN agent_eval_suite suite ON suite.id=revision.suite_id
        WHERE revision.id=?
        """,
            (suite_revision_id,),
        ) as cursor,
    ):
        row = await cursor.fetchone()
    return row is not None and str(row[0]) == "platform_contract"


async def _comparison_attempt_limit_ms(db_path: str, run_id: str) -> int | None:
    """The comparison policy's per-attempt time limit for a comparison run; None for other runs."""

    async with (
        open_shared_ctx(db_path) as db,
        db.execute(
            """
        SELECT policy.policy_json
        FROM agent_eval_comparison_run link
        JOIN agent_eval_comparison_execution execution ON execution.id=link.execution_id
        JOIN agent_eval_plan plan ON plan.id=execution.plan_id
        JOIN agent_eval_policy_revision policy ON policy.id=plan.policy_revision_id
        WHERE link.run_id=?
        """,
            (run_id,),
        ) as cursor,
    ):
        row = await cursor.fetchone()
    if row is None:
        return None
    try:
        document = json.loads(str(row[0]))
    except (TypeError, ValueError):
        return None
    limits = document.get("cost_and_latency_limits") if isinstance(document, dict) else None
    value = limits.get("max_attempt_duration_ms") if isinstance(limits, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1_000:
        return None
    return value


def _elapsed_since_seconds(timestamp: str) -> float:
    try:
        started = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AgentEvalServiceError("agent_eval_execution_timestamp_invalid") from exc
    if started.tzinfo is None:
        raise AgentEvalServiceError("agent_eval_execution_timestamp_invalid")
    return max(0.0, (datetime.now(UTC) - started.astimezone(UTC)).total_seconds())
