"""Feature-gated M3-11 Agent Eval runtime and durable worker."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import aiosqlite
import structlog
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_change_proposal_store import AgentChangeProposalStore
from bowhead_core.agent_eval_comparison import AgentEvalComparisonStore
from bowhead_core.agent_eval_effects import EvalEffectAdapterRegistry
from bowhead_core.agent_eval_job_store import AgentEvalJobStore, EvalRunExecution
from bowhead_core.agent_eval_judge import TeamRouterEvalJudge
from bowhead_core.agent_eval_point_calibration import AgentEvalPointStore, PointCalibrator, TeamRouterCalibrationWriter
from bowhead_core.agent_eval_product import AgentEvalProductStore, model_route_hash
from bowhead_core.agent_eval_service import (
    AgentEvalService,
    AgentEvalServiceError,
    EvalExecutionLimits,
)
from bowhead_core.agent_eval_store import AgentEvalStore, EvalRun
from bowhead_core.agent_eval_subagent_executor import ExactSubagentEvalExecutor
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.agent_version_notifications import AgentVersionNotificationStore
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_router.usage import usage_context

from bowhead_gateway.access_context import build_access_context
from bowhead_gateway.routes._deps import resolve_team_router

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class _ToolViewProvider:
    registry: Any

    @contextlib.asynccontextmanager
    async def open_tool_view(self, **_kwargs: Any):
        # AgentEvalService wraps this registry before every attempt; production
        # handlers never cross the EvalEffectRouter boundary.
        yield self.registry


@dataclass(slots=True)
class _ContextFactory:
    user_store: Any
    access_store: Any

    async def build(self, request: Any) -> ToolExecutionContext:
        access = await build_access_context(
            user_store=self.user_store,
            access_store=self.access_store,
            user_id=request.run.created_by,
            workspace_id=request.workspace_id,
            operation=SpaceOperation.EXECUTE,
            expected_team_id=request.run.subject.team_id,
        )
        return ToolExecutionContext(
            workspace_id=request.workspace_id,
            member_uid="",
            user_id=request.run.created_by,
            turn_id=request.turn_id,
            datarc=object(),  # replaced by the executor's inert connector sentinel
            pbi=object(),
            session_id=request.session_id,
            user_text=str(request.case.input.get("task") or ""),
            role="analyst",
            team_id=request.run.subject.team_id,
            access_context=access,
            access_control_store=self.access_store,
            active_datarc_project="eval_fixture",
        )


@dataclass(slots=True)
class _SafetyGate:
    db_path: str

    async def require_current(self, run: EvalRun) -> None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if run.subject.agent_source_kind == "system":
                async with db.execute(
                    """
                    SELECT 1 FROM system_agent_effective_revision
                    WHERE team_id=? AND agent_id=? AND id=?
                    """,
                    (
                        run.subject.team_id,
                        run.subject.agent_id,
                        run.subject.revision_ref,
                    ),
                ) as cursor:
                    if await cursor.fetchone() is None:
                        raise AgentEvalServiceError("agent_eval_subject_changed")
                return
            if run.subject.candidate_id is not None:
                sql = """
                    SELECT safety.status
                    FROM team_subagent_release_candidate candidate
                    JOIN agent_safety_report safety
                      ON safety.id=candidate.safety_report_id
                     AND safety.team_id=candidate.team_id
                     AND safety.agent_id=candidate.subagent_id
                    WHERE candidate.team_id=? AND candidate.subagent_id=?
                      AND candidate.id=? AND candidate.state IN ('prepared','pending','approved')
                      AND candidate.revision_id=?
                      AND candidate.capability_fingerprint=?
                      AND candidate.dependency_fingerprint=?
                """
                args = (
                    run.subject.team_id,
                    run.subject.agent_id,
                    run.subject.candidate_id,
                    run.subject.revision_ref,
                    run.subject.capability_fingerprint,
                    run.subject.dependency_fingerprint,
                )
            else:
                sql = """
                    SELECT safety.status
                    FROM team_subagent_release release
                    JOIN agent_safety_report safety
                      ON safety.id=release.safety_report_id
                     AND safety.team_id=release.team_id
                     AND safety.agent_id=release.subagent_id
                    WHERE release.team_id=? AND release.subagent_id=?
                      AND release.id=? AND release.revision_id=?
                      AND release.capability_fingerprint=?
                      AND release.dependency_fingerprint=?
                """
                args = (
                    run.subject.team_id,
                    run.subject.agent_id,
                    run.subject.release_id,
                    run.subject.revision_ref,
                    run.subject.capability_fingerprint,
                    run.subject.dependency_fingerprint,
                )
            async with db.execute(sql, args) as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise AgentEvalServiceError("agent_eval_subject_changed")
        if str(row["status"]) in {"blocked", "unknown"}:
            raise AgentEvalServiceError("agent_safety_blocked")


async def resolve_eval_model(state: Any, *, team_id: str, role: str, site: str) -> tuple[Any, str]:
    """The Team router and model route hash an evaluation of ``role`` would use.

    The one resolver for every evaluation model check: starting a test or comparison, the run's
    preflight gate and the executor's router. A missing model raises ``agent_eval_model_unavailable``
    with the role and a safe reason (``unconfigured``, ``legacy_config``, ``build_failed``,
    ``resolve_error`` or ``role_unassigned``), never a generic failure.
    """

    router, miss = await resolve_team_router(state, team_id, agent_type=role, site=site)
    if router is None:
        reason = str(getattr(miss, "value", miss) or "unconfigured").lower()
        raise AgentEvalServiceError("agent_eval_model_unavailable", details={"model_role": role, "model_reason": reason})
    try:
        binding = router.resolve(role)
    except KeyError as exc:
        raise AgentEvalServiceError("agent_eval_model_unavailable", details={"model_role": role, "model_reason": "role_unassigned"}) from exc
    return router, model_route_hash(role=role, binding=binding)


@dataclass(slots=True)
class _NoModelRouter:
    """Stand-in Router for a run whose model is gone; the model gate stops the run before any call."""

    role: str

    def resolve(self, agent_type: str, *_args: Any, **_kwargs: Any) -> Any:
        from bowhead_router.router import NoBindingError

        raise NoBindingError(agent_type)

    async def invoke(self, agent_type: str, *_args: Any, **_kwargs: Any) -> Any:
        from bowhead_router.router import NoBindingError

        raise NoBindingError(agent_type)

    def stream(self, agent_type: str, *_args: Any, **_kwargs: Any) -> Any:
        from bowhead_router.router import NoBindingError

        raise NoBindingError(agent_type)


@dataclass(slots=True)
class _ModelGate:
    state: Any
    role: str

    async def require_current(self, run: EvalRun) -> None:
        _router, current = await resolve_eval_model(self.state, team_id=run.subject.team_id, role=self.role, site="agent_eval_model_gate")
        if current != run.subject.model_config_hash:
            raise AgentEvalServiceError("agent_eval_model_config_changed", details={"model_role": self.role})


@dataclass(slots=True)
class _AdmissionGate:
    db_path: str
    max_user_running: int = 4
    max_team_running: int = 20

    async def authorize(
        self,
        *,
        team_id: str,
        user_id: str,
        case_count: int,
        attempts: int,
    ) -> bool:
        if case_count * attempts > 1_000:
            return False
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                """
                SELECT
                  SUM(CASE WHEN created_by=? THEN 1 ELSE 0 END),COUNT(*)
                FROM agent_eval_run WHERE team_id=? AND status='running'
                """,
                (user_id, team_id),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return bool(row is not None and int(row[0] or 0) < self.max_user_running and int(row[1] or 0) < self.max_team_running)


def _create_detached_task(coro: Any, *, name: str) -> asyncio.Task[None]:
    """Run background evaluation work in an empty context, as the worker path does.

    ``asyncio.create_task`` copies the caller's contextvars. When Coach starts Agent CI
    from a chat Turn, that copy carries the Turn's physical usage observer and usage
    attribution; the observer then refuses every evaluation model call
    (``physical_observation_unavailable``) and usage would be billed to the Coach Turn.
    Evaluation execution owns its scope: each attempt binds its scratch Turn itself.
    """

    return asyncio.create_task(coro, name=name, context=contextvars.Context())


@dataclass(slots=True)
class AgentEvalRuntime:
    db_path: str
    state: Any
    notification_store: Any | None = None
    _tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)

    async def model_hash(self, *, team_id: str, role: str) -> str:
        _router, value = await resolve_eval_model(self.state, team_id=team_id, role=role, site="agent_eval_create")
        return value

    async def prepare_and_schedule(
        self,
        *,
        run_id: str,
        workspace_id: str,
        limits: EvalExecutionLimits,
    ) -> EvalRun:
        service = await self._service(run_id)
        run = await service.prepare_run(
            run_id=run_id,
            workspace_id=workspace_id,
            limits=limits,
        )
        if run.status == "running":
            comparison_id = await _comparison_execution_id(self.db_path, run_id)
            if comparison_id is not None:
                await AgentEvalComparisonStore(self.db_path).mark_running(comparison_id)
            self.schedule_execution(run_id=run_id)
        return run

    def schedule_execution(self, *, run_id: str) -> None:
        current = self._tasks.get(run_id)
        if current is not None and not current.done():
            return
        task = _create_detached_task(self._execute(run_id), name=f"agent-eval:{run_id}")
        self._tasks[run_id] = task

        def _remove_finished(finished: asyncio.Task[None]) -> None:
            if self._tasks.get(run_id) is finished:
                self._tasks.pop(run_id, None)

        task.add_done_callback(_remove_finished)

    async def judge_model_hash(self, *, team_id: str) -> str | None:
        """The Team evaluation model's fingerprint, or None when no evaluation model resolves."""

        try:
            return await self.model_hash(team_id=team_id, role="judge")
        except AgentEvalServiceError:
            return None

    def schedule_point_calibration(self, *, team_id: str, agent_id: str) -> None:
        """Calibrate the Agent's confirmed model-graded points in the background (technical design §7.2)."""

        key = f"calibration:{team_id}:{agent_id}"
        current = self._tasks.get(key)
        if current is not None and not current.done():
            return
        task = _create_detached_task(self._calibrate_points(team_id=team_id, agent_id=agent_id), name=f"agent-eval:{key}")
        self._tasks[key] = task

        def _remove_finished(finished: asyncio.Task[None]) -> None:
            if self._tasks.get(key) is finished:
                self._tasks.pop(key, None)

        task.add_done_callback(_remove_finished)

    async def _calibrate_points(self, *, team_id: str, agent_id: str) -> None:
        try:
            model_fingerprint = await self.judge_model_hash(team_id=team_id)
            store = AgentEvalPointStore(self.db_path)
            tasks = await store.calibration_tasks(team_id=team_id, agent_id=agent_id, model_fingerprint=model_fingerprint)
            if not tasks or model_fingerprint is None:
                return
            judge_router, _judge_miss = await resolve_team_router(self.state, team_id, agent_type="judge", site="agent_eval_calibration")
            writer_router, _writer_miss = await resolve_team_router(self.state, team_id, agent_type="echo", site="agent_eval_calibration")
            if judge_router is None or writer_router is None:
                return
            calibrator = PointCalibrator(
                store=store,
                judge=TeamRouterEvalJudge(router=judge_router),
                writer=TeamRouterCalibrationWriter(router=writer_router),
            )
            for task in tasks:
                # Calibration calls show up in usage reporting under their own reason.
                with usage_context(team_id=team_id, user_id=task.owner_user_id, reason="agent_eval_calibration"):
                    outcome = await calibrator.calibrate(task, model_fingerprint=model_fingerprint)
                log.info(
                    "agent_eval.point_calibrated",
                    agent_id=agent_id,
                    suite_id=task.suite_id,
                    point_id=task.point["id"],
                    state=outcome.state,
                    failure_reason=outcome.failure_reason,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("agent_eval.point_calibration_failed", agent_id=agent_id, exc_info=True)

    async def recover_running(self) -> None:
        for plan in await AgentEvalJobStore(self.db_path).list_runnable_executions(
            limit=200,
        ):
            self.schedule_execution(run_id=plan.run_id)
        comparison_store = AgentEvalComparisonStore(self.db_path, runtime_check=self.require_coach_runtime)
        for execution_id in await comparison_store.list_finalizable(limit=50):
            await comparison_store.finalize(execution_id)

    async def process_one_regression(self, *, worker_id: str) -> bool:
        product = AgentEvalProductStore(self.db_path)
        claim = await product.claim_regression(worker_id=worker_id)
        if claim is None:
            return False
        event = claim.event
        if event.agent_source_kind != "team":
            await product.settle_regression(
                claim,
                status="skipped",
                outcome_code="system_eval_requires_governance",
            )
            return True
        try:
            scheduled = await self._schedule_regression_event(event, product=product)
        except Exception as exc:
            code = str(getattr(exc, "code", "agent_eval_regression_failed"))[:160]
            log.warning(
                "agent_eval.regression_failed",
                event_id=event.event_id,
                code=code,
            )
            if claim.event.attempt_count < 8:
                await product.retry_regression(
                    claim,
                    outcome_code=code,
                    delay_seconds=min(300, 2**claim.event.attempt_count),
                )
            else:
                await product.settle_regression(
                    claim,
                    status="skipped",
                    outcome_code=code,
                )
            return True
        await product.settle_regression(
            claim,
            status="delivered" if scheduled else "skipped",
            outcome_code="runs_scheduled" if scheduled else "manual_baseline_required",
        )
        return True

    async def process_one_failure_notification(self, *, worker_id: str) -> bool:
        if self.notification_store is None:
            return False
        product = AgentEvalProductStore(self.db_path)
        claim = await product.claim_failure_notification(worker_id=worker_id)
        if claim is None:
            return False
        try:
            await product.project_failure_notification(
                claim,
                notification_store=self.notification_store,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "agent_eval_notification_projection_failed"))[:160]
            if claim.notification.attempt_count < 8:
                await product.retry_failure_notification(
                    claim,
                    outcome_code=code,
                    delay_seconds=min(300, 2**claim.notification.attempt_count),
                )
            else:
                await product.settle_failure_notification(
                    claim,
                    status="failed",
                    outcome_code=code,
                )
            log.warning(
                "agent_eval.notification_failed",
                run_id=claim.notification.run_id,
                code=code,
            )
            return True
        if claim.notification.critical_failure and claim.notification.trigger_kind != "manual":
            try:
                await self._propose_restore_for_notification(claim.notification)
            except Exception:
                log.exception(
                    "agent_eval.restore_proposal_failed",
                    run_id=claim.notification.run_id,
                    agent_id=claim.notification.agent_id,
                )
        await product.settle_failure_notification(
            claim,
            status="delivered",
            outcome_code="notification_projected",
        )
        return True

    async def process_one_version_notification(self, *, worker_id: str) -> bool:
        """Deliver one Owner version notification (proposal, candidate or result) to the inbox."""

        if self.notification_store is None:
            return False
        store = AgentVersionNotificationStore(self.db_path)
        claim = await store.claim_version_notification(worker_id=worker_id)
        if claim is None:
            return False
        try:
            outcome = await store.project_version_notification(
                claim,
                notification_store=self.notification_store,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "agent_version_notification_projection_failed"))[:160]
            log.warning(
                "agent_version.notification_failed",
                outbox_id=claim.notice.outbox_id,
                event_type=claim.notice.event_type,
                attempt=claim.notice.attempt_count,
                code=code,
            )
            await store.retry_version_notification(claim, outcome_code=code)
            return True
        await store.settle_version_notification(
            claim,
            status=outcome.status,
            outcome_code=outcome.outcome_code,
        )
        return True

    async def _propose_restore_for_notification(self, notification: Any) -> None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT release_id FROM agent_eval_run WHERE id=? AND team_id=? AND agent_id=?",
                (notification.run_id, notification.team_id, notification.agent_id),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None or not row["release_id"]:
            return
        await AgentChangeProposalStore(self.db_path).create_restore_proposal(
            team_id=notification.team_id,
            agent_id=notification.agent_id,
            failed_release_id=str(row["release_id"]),
            effective_owner_user_id=notification.recipient_user_id,
            regression_run_id=notification.run_id,
            reason_code=f"regression_{notification.run_status}",
        )

    async def _schedule_regression_event(
        self,
        event: Any,
        *,
        product: AgentEvalProductStore,
    ) -> bool:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT suite.id,revision.id AS revision_id
                FROM agent_eval_suite suite
                JOIN agent_eval_suite_revision revision ON revision.id=(
                  SELECT latest.id FROM agent_eval_suite_revision latest
                  WHERE latest.suite_id=suite.id ORDER BY latest.sequence DESC LIMIT 1
                )
                JOIN agent_eval_trigger_policy policy
                  ON policy.team_id=suite.team_id
                 AND policy.agent_source_kind=suite.agent_source_kind
                 AND policy.agent_id=suite.agent_id
                WHERE suite.team_id=? AND suite.agent_source_kind='team'
                  AND suite.agent_id=? AND suite.state='active' AND policy.enabled=1
                ORDER BY suite.id
                """,
                (event.team_id, event.agent_id),
            ) as cursor:
                suites = await cursor.fetchall()
        scheduled = False
        for suite in suites:
            baseline = await product.latest_execution_for_suite(
                team_id=event.team_id,
                agent_id=event.agent_id,
                suite_id=str(suite["id"]),
            )
            if baseline is None:
                continue
            workspace_id, actor_user_id, values = baseline
            preliminary = await product.resolve_team_subject(
                team_id=event.team_id,
                agent_id=event.agent_id,
                model_config_hash="sha256:" + "0" * 64,
                target="live",
            )
            model_hash = await self.model_hash(
                team_id=event.team_id,
                role=preliminary.model_role,
            )
            target = await product.resolve_team_subject(
                team_id=event.team_id,
                agent_id=event.agent_id,
                model_config_hash=model_hash,
                target="live",
            )
            run = await product.eval_store.create_run(
                subject=target.subject,
                suite_revision_id=str(suite["revision_id"]),
                trigger_kind=event.change_kind,
                idempotency_key=(f"auto:{event.event_id}:{suite['id']}:{target.subject.fingerprint}")[:200],
                actor_user_id=actor_user_id,
            )
            limits = EvalExecutionLimits(**values)
            await self.prepare_and_schedule(
                run_id=run.run_id,
                workspace_id=workspace_id,
                limits=limits,
            )
            scheduled = True
        return scheduled

    async def _execute(self, run_id: str) -> None:
        try:
            plan = await AgentEvalJobStore(self.db_path).read_execution(run_id)
            if plan is None:
                return
            service = await self._service(run_id)
            run = await service.execute_run(
                run_id=run_id,
                workspace_id=plan.workspace_id,
                worker_id=f"gateway:{uuid4().hex}",
                limits=_limits_from_plan(plan),
            )
            if run.status in {"passed", "failed", "unknown", "incomplete", "cancelled"}:
                comparison_id = await _comparison_execution_id(self.db_path, run_id)
                if comparison_id is not None:
                    await AgentEvalComparisonStore(self.db_path, runtime_check=self.require_coach_runtime).finalize(comparison_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "agent_eval.execution_failed",
                run_id=run_id,
                code=str(getattr(exc, "code", "agent_eval_execution_failed")),
            )

    async def require_coach_runtime(self, run_id: str) -> None:
        """Re-read runtime admission before internal CI dispatch and finalization."""
        from bowhead_core.entitlements import EntitlementDenied

        from bowhead_gateway._entitlement_gate import _requires_resolver

        run = await AgentEvalStore(self.db_path).read_run(run_id)
        if run is None:
            raise AgentEvalServiceError("agent_eval_run_not_found")
        resolver = getattr(self.state, "entitlements", None)
        if resolver is None:
            if _requires_resolver(self.state):
                raise AgentEvalServiceError("entitlement_unavailable")
        else:
            try:
                await resolver.assert_can_run_turn(run.subject.team_id)
            except EntitlementDenied as exc:
                raise AgentEvalServiceError("coach_ci_entitlement_denied") from exc
        await _SafetyGate(self.db_path).require_current(run)
        await _ModelGate(self.state, await _model_role(self.db_path, run)).require_current(run)

    async def _service(self, run_id: str) -> AgentEvalService:
        run = await AgentEvalStore(self.db_path).read_run(run_id)
        if run is None:
            raise AgentEvalServiceError("agent_eval_run_not_found")
        role = await _model_role(self.db_path, run)
        router, _miss = await resolve_team_router(
            self.state,
            run.subject.team_id,
            agent_type=role,
            site="agent_eval_execute",
        )
        if router is None:
            # The model went away after the run was scheduled. Building the service anyway lets the run's own
            # model gate stop it with agent_eval_model_unavailable (it never reaches an attempt); failing here
            # retried the run on every poll and the test showed "Testing" forever.
            router = _NoModelRouter(role)
        runner = SubagentRunner(
            router=router,
            tool_registry=self.state.tool_registry,
            team_subagent_store=self.state.user_store,
        )
        eval_store = AgentEvalStore(self.db_path)
        job_store = AgentEvalJobStore(self.db_path)
        judge: TeamRouterEvalJudge | None = None
        if run.judge_role_ref is not None:
            # The Team evaluation model grades expected points (technical design §5.4). If it can't be
            # resolved now, graded points report "not graded" and the rules decide (PRD R17).
            judge_router, _judge_miss = await resolve_team_router(
                self.state,
                run.subject.team_id,
                agent_type=run.judge_role_ref,
                site="agent_eval_judge",
            )
            if judge_router is not None:
                judge = TeamRouterEvalJudge(router=judge_router)
        return AgentEvalService(
            datarc_live=_datarc_live_scope(self.db_path, self.state, run),
            eval_store=eval_store,
            job_store=job_store,
            session_store=self.state.session_store,
            executor=ExactSubagentEvalExecutor(
                binding_store=AgentExecutionBindingStore(self.db_path),
                subagent_runner=runner,
                context_factory=_ContextFactory(
                    self.state.user_store,
                    self.state.access_control_store,
                ),
                ordered_turn_log_sink=self.state.ordered_turn_log_sink,
                team_settings_store=getattr(self.state, "team_settings_store", None),
            ),
            tool_view_provider=_ToolViewProvider(self.state.tool_registry),
            fixture_resolver=None,
            adapter_registry=EvalEffectAdapterRegistry(),
            safety_gate=_SafetyGate(self.db_path),
            model_config_gate=_ModelGate(self.state, role),
            admission_gate=_AdmissionGate(self.db_path),
            judge=judge,
            runtime_authority_check=self.require_coach_runtime,
        )

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def run_agent_eval_worker(
    runtime: AgentEvalRuntime,
    *,
    poll_interval: float = 2.0,
) -> None:
    worker_id = f"gateway:{uuid4().hex}"
    while True:
        try:
            await runtime.recover_running()
            regression_consumed = await runtime.process_one_regression(
                worker_id=worker_id,
            )
            notification_consumed = await runtime.process_one_failure_notification(
                worker_id=worker_id,
            )
            version_notice_consumed = await runtime.process_one_version_notification(
                worker_id=worker_id,
            )
            consumed = regression_consumed or notification_consumed or version_notice_consumed
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("agent_eval.worker_tick_failed", exc_info=True)
            consumed = False
        await asyncio.sleep(0 if consumed else poll_interval)


def _datarc_live_scope(db_path: str, state: Any, run: EvalRun) -> Any:
    """The run's live read-only Datarc scope (FEATURE_AGENT_EVAL_DATARC_READ_ONLY), Team Agents only.

    None with the switch off or for a system Agent: every Datarc call then stays eval_effect_adapter_missing.
    """

    import os

    from bowhead_core.agent_eval_datarc import EvalDatarcLiveRun, datarc_live_enabled, derive_evidence_key

    if not datarc_live_enabled() or run.subject.agent_source_kind != "team":
        return None
    from bowhead_gateway.agent_eval_datarc import TeamOwnerDatarcResolver

    return EvalDatarcLiveRun(
        resolver=TeamOwnerDatarcResolver(db_path=db_path, state=state),
        run=run,
        # The §6.1 digest key, derived for its fixed purpose from the instance root secret.
        digest_key=derive_evidence_key(os.environ.get("BOWHEAD_FERNET_KEY", "")),
    )


async def _model_role(db_path: str, run: EvalRun) -> str:
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        if run.subject.agent_source_kind == "system":
            sql = """
                SELECT definition_json FROM system_agent_effective_revision
                WHERE team_id=? AND agent_id=? AND id=?
            """
            args = (
                run.subject.team_id,
                run.subject.agent_id,
                run.subject.revision_ref,
            )
        else:
            sql = """
                SELECT definition_json FROM team_subagent_revision
                WHERE team_id=? AND subagent_id=? AND id=?
            """
            args = (
                run.subject.team_id,
                run.subject.agent_id,
                run.subject.revision_ref,
            )
        async with db.execute(sql, args) as cursor:
            row = await cursor.fetchone()
    if row is None:
        raise AgentEvalServiceError("agent_eval_subject_changed")
    import json

    definition = json.loads(str(row["definition_json"]))
    if run.subject.agent_source_kind == "system" and isinstance(definition, dict):
        role = definition.get("llm_agent_type")
        return str(role) if isinstance(role, str) and role else "analyst"
    # The role the runtime runs a Team Agent as (the allowlist clamp), not the raw stored value.
    from bowhead_core.agent_model_policy import team_agent_model_role

    return team_agent_model_role(definition.get("model") if isinstance(definition, dict) else None)


async def _comparison_execution_id(db_path: str, run_id: str) -> str | None:
    async with (
        open_shared_ctx(db_path) as db,
        db.execute(
            "SELECT execution_id FROM agent_eval_comparison_run WHERE run_id=?",
            (run_id,),
        ) as cursor,
    ):
        row = await cursor.fetchone()
    return str(row[0]) if row is not None else None


def _limits_from_plan(plan: EvalRunExecution) -> EvalExecutionLimits:
    return EvalExecutionLimits(
        attempts=plan.requested_attempts,
        max_concurrency=plan.max_concurrency,
        max_duration_ms=plan.max_duration_ms,
        max_cost_microusd=plan.max_cost_microusd,
    )


__all__ = ["AgentEvalRuntime", "run_agent_eval_worker"]
