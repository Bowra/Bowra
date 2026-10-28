"""Durable Terminal Protocol v1 approval effect runner."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from bowhead_core.events import OrchestratorEvent
from bowhead_core.terminal_approval_contract import (
    approval_terminal_event_id,
    build_approval_required_events,
)
from bowhead_core.terminal_effect_contract import (
    effect_control_identity_digest,
)
from bowhead_core.terminal_protocol import (
    ActorScope,
    DecisionState,
    EffectClass,
    EffectSettlementContext,
    EffectSettlementOutcome,
    EffectSettlementSource,
    EffectState,
    PhaseScope,
    StagedTerminalEvent,
    canonical_digest_v1,
    stable_approval_effect_action_ref,
)
from bowhead_core.terminal_protocol_store import (
    DecisionAccessRevokedError,
    TerminalProtocolFaultPoint,
)
from bowhead_core.terminal_runtime_factory import TerminalRuntimeAdmissionError
from bowhead_core.types import TurnRequest


class TerminalApprovalRunner:
    """Open N+1, fence one provider call, settle, project, and finalize."""

    def __init__(
        self,
        *,
        factory: Any,
        approval_store: Any,
        authorizer: Any | None = None,
    ) -> None:
        self._factory = factory
        self._approval_store = approval_store
        self._authorizer = authorizer
        self._tasks: set[asyncio.Task[Any]] = set()

    async def execute(
        self,
        *,
        prepared: Any,
        approval: Any,
        legacy_executor: Any,
    ) -> Any:
        """Supervise execution outside the HTTP request task.

        Awaiting the shield keeps the existing synchronous API contract while
        request cancellation cannot become provider/generation ownership.
        """

        task = await self.spawn(
            prepared=prepared,
            approval=approval,
            legacy_executor=legacy_executor,
        )
        return await asyncio.shield(task)

    async def spawn(
        self,
        *,
        prepared: Any,
        approval: Any,
        legacy_executor: Any,
    ) -> asyncio.Task[Any]:
        """Create the process-owned worker and return without request ownership."""

        prepared = await self._factory.rehydrate_source_decision(prepared)

        if legacy_executor is None or not hasattr(
            legacy_executor,
            "execute_effect",
        ):
            error = RuntimeError("terminal_approval_execution_unsupported")
            await self._factory.trigger_recovery(
                prepared.preflight.scope,
                reason="approval_executor_unavailable",
                prior_error=error,
            )
            raise error
        if self._authorizer is not None and prepared.decision.state is DecisionState.DECISION_RECORDED:
            try:
                await self._authorizer.authorize(
                    prepared,
                    workspace_id=approval.workspace_id,
                )
            except DecisionAccessRevokedError as exc:
                await self._factory.commit_decision_authorization_denial(
                    prepared,
                    denial_reason_code=getattr(
                        exc,
                        "reason_code",
                        "access_revoked",
                    ),
                )
                raise
        runtime = await self._factory.open_decision_runtime(
            prepared,
            self._turn_request(
                prepared,
                approval,
                legacy_executor=legacy_executor,
            ),
        )

        def create() -> asyncio.Task[Any]:
            worker = self._execute_worker(
                runtime=runtime,
                prepared=prepared,
                approval=approval,
                legacy_executor=legacy_executor,
            )
            try:
                return asyncio.create_task(
                    worker,
                    name=f"terminal-approval:{approval.id}",
                )
            except BaseException:
                worker.close()
                raise

        try:
            task, _started_here = await self._factory.spawn_decision_worker(
                runtime,
                create,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if runtime.bus.receipt is None and not runtime.retired:
                try:
                    await runtime.abort_worker_start_failed()
                except BaseException as handoff_error:
                    exc.add_note(f"terminal approval pre-task recovery failed: {handoff_error!r}")
            await self._factory.trigger_recovery(
                prepared.preflight.scope,
                reason="approval_worker_task_create_failed",
                prior_error=exc,
            )
            raise
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @staticmethod
    def _turn_request(
        prepared: Any,
        approval: Any,
        *,
        legacy_executor: Any,
    ) -> TurnRequest:
        context = prepared.decision.context
        return TurnRequest(
            session_id=context.scope.session_id,
            workspace_id=approval.workspace_id,
            user_id=context.principals.effect_principal_ref,
            member_uid=(getattr(legacy_executor, "approver_member_uid", None) or ""),
            text=f"approval:{approval.id}",
            turn_id=approval.turn_id,
            team_id=context.scope.team_id,
            team_router=getattr(legacy_executor, "team_router", None),
            resume_turn_id=approval.turn_id,
        )

    async def _execute_worker(
        self,
        *,
        runtime: Any,
        prepared: Any,
        approval: Any,
        legacy_executor: Any,
    ) -> Any:
        try:
            await runtime.start_execution()
        except BaseException as exc:
            await self._handoff_after_error(
                runtime,
                reason_code="worker_start_failed",
                error=exc,
            )
            raise
        try:
            return await self._execute_started(
                runtime=runtime,
                prepared=prepared,
                approval=approval,
                legacy_executor=legacy_executor,
            )
        except BaseException as exc:
            if runtime.bus.receipt is None and not runtime.retired:
                await self._handoff_after_error(
                    runtime,
                    reason_code="worker_lost",
                    error=exc,
                )
            elif not runtime.retired:
                await _await_durable_cleanup(runtime.retire())
            raise

    async def _execute_started(
        self,
        *,
        runtime: Any,
        prepared: Any,
        approval: Any,
        legacy_executor: Any,
    ) -> Any:
        context = prepared.decision.context
        effect_user_id = context.principals.effect_principal_ref
        provider_token = context.decision_id
        action_ref = stable_approval_effect_action_ref(
            decision_id=context.decision_id,
            approval_id=approval.id,
            arguments_digest=approval.arguments_digest,
        )
        attempt_id = f"tea_{canonical_digest_v1({'action_ref': action_ref, 'kind': 'approval'})[:32]}"
        existing_attempt = await runtime.store.read_effect_attempt(
            runtime.scope,
            action_ref=action_ref,
        )
        # Semantic policy is a pre-provider gate.  Once the durable dispatch
        # boundary was crossed, replay must reconcile the provider result even
        # if the user later loses access; it must never rewrite that history as
        # a fresh authorization denial.
        if existing_attempt is None or existing_attempt.state is EffectState.INTENT:
            try:
                if self._authorizer is not None:
                    await self._authorizer.authorize(
                        prepared,
                        workspace_id=approval.workspace_id,
                    )
            except DecisionAccessRevokedError as exc:
                if existing_attempt is not None:
                    await runtime.store.discard_effect_intent(
                        runtime.execution,
                        existing_attempt,
                    )
                await self._finalize_pre_dispatch_denial(
                    runtime=runtime,
                    prepared=prepared,
                    reason_code=getattr(
                        exc,
                        "reason_code",
                        "access_revoked",
                    ),
                )
                raise
        try:
            attempt = await runtime.store.begin_effect_attempt(
                runtime.execution,
                attempt_id=attempt_id,
                effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
                provider=f"approval:{approval.tool_name}",
                action_ref=action_ref,
                idempotency_or_fence_token=provider_token,
                non_retriable=False,
            )
        except DecisionAccessRevokedError as exc:
            await self._finalize_pre_dispatch_denial(
                runtime=runtime,
                prepared=prepared,
                reason_code=getattr(
                    exc,
                    "reason_code",
                    "access_revoked",
                ),
            )
            raise
        dispatched_here = False
        if attempt.state is EffectState.INTENT:
            try:
                dispatch = await runtime.store.claim_effect_dispatch(
                    runtime.execution,
                    attempt,
                )
            except DecisionAccessRevokedError as exc:
                await self._finalize_pre_dispatch_denial(
                    runtime=runtime,
                    prepared=prepared,
                    reason_code=getattr(
                        exc,
                        "reason_code",
                        "access_revoked",
                    ),
                )
                raise
            attempt = dispatch.attempt
            dispatched_here = dispatch.dispatched_here
        projected = await self._approval_store.get(approval.id)
        if projected is None:
            error = RuntimeError("terminal_approval_projection_missing")
            await self._handoff_after_error(
                runtime,
                reason_code="terminal_projection_missing",
                error=error,
            )
            raise error

        projection_is_durable = projected.status in {"executed", "execution_failed"}
        if attempt.state in {EffectState.DISPATCHED, EffectState.UNKNOWN} and projection_is_durable:
            ok = projected.status == "executed" and projected.execution_ok is True
            result = projected.execution_result if ok else None
            error = projected.execution_error
            attempt = await self._settle_from_projection(
                runtime=runtime,
                prepared=prepared,
                approval=projected,
                attempt=attempt,
                ok=ok,
                result=result,
                error=error,
            )
        elif attempt.state is EffectState.SETTLED and projection_is_durable:
            ok = projected.status == "executed" and projected.execution_ok is True
            result = projected.execution_result if ok else None
            error = projected.execution_error
        elif attempt.state is EffectState.DISPATCHED and dispatched_here:
            try:
                ok, result, error = await legacy_executor.execute_effect(
                    approval.id,
                    effect_user_id=effect_user_id,
                    idempotency_key=provider_token,
                )
                projected = await self._approval_store.record_execution(
                    approval_id=approval.id,
                    ok=ok,
                    result=result,
                    error=error,
                )
                if projected is None:
                    raise RuntimeError("terminal_approval_projection_missing")
            except BaseException as exc:
                try:
                    await runtime.store.mark_effect_unknown(attempt)
                except BaseException as mark_error:
                    exc.add_note(f"terminal approval unknown-effect projection failed: {mark_error!r}")
                try:
                    await runtime.bus.emit_idempotent(
                        OrchestratorEvent(
                            event_type="interrupted_check_actions",
                            turn_id=approval.turn_id,
                            payload={
                                "approval_id": approval.id,
                                "attempt_id": attempt.attempt_id,
                                "message_key": ("turn.interrupted_check_actions"),
                                "reason_code": "effect_outcome_unknown",
                            },
                        ),
                        idempotency_key=(f"approval_effect_unknown:{context.decision_id}"),
                    )
                except Exception as event_error:
                    exc.add_note(f"terminal interrupted-check-actions event failed: {event_error!r}")
                await self._handoff_after_error(
                    runtime,
                    reason_code="effect_outcome_unknown",
                    error=exc,
                )
                raise
            attempt = await self._settle_from_projection(
                runtime=runtime,
                prepared=prepared,
                approval=projected,
                attempt=attempt,
                ok=ok,
                result=result,
                error=error,
            )
        elif attempt.state is EffectState.DISPATCHED:
            # Another request won the dispatch CAS and owns this shared live
            # runtime.  Do not retire/handoff its generation; the caller gets a
            # stable 202 and may reattach to the existing Turn stream.
            raise TerminalRuntimeAdmissionError(
                "terminal_admission_recovery_pending",
                decision_id=context.decision_id,
                generation=runtime.scope.generation,
            )
        else:
            error = RuntimeError("terminal_approval_effect_recovery_pending")
            await self._handoff_after_error(
                runtime,
                reason_code="effect_outcome_unknown",
                error=error,
            )
            raise error

        if attempt.state is not EffectState.SETTLED:
            error = RuntimeError("terminal_approval_effect_settlement_failed")
            await self._handoff_after_error(
                runtime,
                reason_code="terminal_projection_missing",
                error=error,
            )
            raise error
        try:
            disclosure = await runtime.store.read_effect_disclosure_snapshot(runtime.scope)
            if disclosure is None or disclosure.unsettled_count != 0:
                raise RuntimeError("terminal_approval_effect_disclosure_missing")
            runtime.bus.configure_required_terminal_events(
                build_approval_required_events(
                    decision_id=context.decision_id,
                    approval_id=approval.id,
                    ok=ok,
                    result_digest=(canonical_digest_v1(result) if ok else None),
                    effect_settlement_payload=disclosure.event_payload,
                )
            )
            await runtime.bus.emit(
                OrchestratorEvent(
                    event_type="done",
                    turn_id=approval.turn_id,
                    payload={
                        "reason_code": None if ok else "terminal_dependency_missing",
                        "status": "completed" if ok else "failed",
                        "turn_id": approval.turn_id,
                    },
                )
            )
        except BaseException as exc:
            await self._handoff_after_error(
                runtime,
                reason_code="terminal_projection_missing",
                error=exc,
            )
            raise
        await runtime.retire()
        return projected

    @staticmethod
    async def _finalize_pre_dispatch_denial(
        *,
        runtime: Any,
        prepared: Any,
        reason_code: str,
    ) -> None:
        """Commit a failed receipt only when the provider was never called."""

        context = prepared.decision.context
        terminal_reason = "terminal_dependency_missing" if reason_code in {"dependency_missing", "entitlement_denied"} else "access_revoked"
        runtime.bus.configure_required_terminal_events(
            (
                StagedTerminalEvent.from_payload(
                    event_id=approval_terminal_event_id(
                        context.decision_id,
                        "pre_dispatch_denied",
                    ),
                    event_type="turn_failed",
                    actor_scope=ActorScope.ROOT,
                    phase_scope=PhaseScope.IN_GENERATION,
                    payload={
                        "message_key": "turn.failed",
                        "reason_code": terminal_reason,
                    },
                ),
            )
        )
        await runtime.bus.emit(
            OrchestratorEvent(
                event_type="done",
                turn_id=context.scope.turn_id,
                payload={
                    "reason_code": terminal_reason,
                    "status": "failed",
                    "turn_id": context.scope.turn_id,
                },
            )
        )
        await runtime.retire()

    async def _settle_from_projection(
        self,
        *,
        runtime: Any,
        prepared: Any,
        approval: Any,
        attempt: Any,
        ok: bool,
        result: Any,
        error: str | None,
    ) -> Any:
        try:
            return await settle_approval_effect_from_projection(
                store=runtime.store,
                scope=runtime.scope,
                decision_id=prepared.decision.context.decision_id,
                authz_snapshot_digest=(runtime.identity.authz_snapshot_digest),
                approval=approval,
                attempt=attempt,
                ok=ok,
                result=result,
                error=error,
            )
        except BaseException as exc:
            await self._handoff_after_error(
                runtime,
                reason_code="terminal_projection_missing",
                error=exc,
            )
            raise

    @staticmethod
    async def _handoff_after_error(
        runtime: Any,
        *,
        reason_code: str,
        error: BaseException,
    ) -> None:
        """Retire local ownership without hiding the triggering failure."""

        try:
            await _await_durable_cleanup(runtime.handoff_to_recovery(reason_code=reason_code))
        except BaseException as handoff_error:
            error.add_note(f"terminal approval recovery handoff failed: {handoff_error!r}")


async def _await_durable_cleanup(operation: Any) -> Any:
    """Complete an ownership handoff even when the worker is cancelled."""

    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class TerminalApprovalRecovery:
    """Recover a known approval projection without invoking its provider again."""

    def __init__(
        self,
        *,
        protocol_store: Any,
        approval_store: Any,
        fault_injector: Any | None = None,
    ) -> None:
        self._protocol_store = protocol_store
        self._approval_store = approval_store
        self._fault_injector = fault_injector

    def _inject(self, point: TerminalProtocolFaultPoint) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    async def __call__(self, processing: Any, fence: Any) -> bool:
        from bowhead_core.terminal_protocol import (
            DecisionAction,
            DecisionSourceKind,
        )
        from bowhead_core.terminal_reconciler import (
            converge_settled_effect_recovery,
        )

        recovery = await self._protocol_store.read_recovery_effect_context(processing)
        if recovery.source_kind is not DecisionSourceKind.APPROVAL or recovery.action is not DecisionAction.APPROVE:
            return False
        approval = await self._approval_store.get(recovery.source_id)
        if approval is None or approval.status not in {"executed", "execution_failed"}:
            return False
        if recovery.attempt.action_ref != stable_approval_effect_action_ref(
            decision_id=recovery.decision_id,
            approval_id=approval.id,
            arguments_digest=approval.arguments_digest,
        ):
            raise RuntimeError("terminal_approval_effect_identity_changed")
        ok = approval.status == "executed" and approval.execution_ok is True
        result = approval.execution_result if ok else None
        if recovery.attempt.state in {EffectState.DISPATCHED, EffectState.UNKNOWN}:
            attempt = await settle_approval_effect_from_projection(
                store=self._protocol_store,
                scope=processing.scope,
                decision_id=recovery.decision_id,
                authz_snapshot_digest=(recovery.execution_authz_snapshot_digest),
                approval=approval,
                attempt=recovery.attempt,
                ok=ok,
                result=result,
                error=approval.execution_error,
            )
            if attempt.state is not EffectState.SETTLED:
                return False
        elif recovery.attempt.state is not EffectState.SETTLED:
            return False
        self._inject(TerminalProtocolFaultPoint.RECOVERY_AFTER_EFFECT_SETTLEMENT_BEFORE_PROJECTION)
        await converge_settled_effect_recovery(
            self._protocol_store,
            processing=processing,
            fence=fence,
            approval_id=approval.id,
            decision_id=recovery.decision_id,
            ok=ok,
            result_digest=(canonical_digest_v1(result) if ok else None),
            fault_injector=self._fault_injector,
        )
        return True


async def settle_approval_effect_from_projection(
    *,
    store: Any,
    scope: Any,
    decision_id: str,
    authz_snapshot_digest: str,
    approval: Any,
    attempt: Any,
    ok: bool,
    result: Any,
    error: str | None,
) -> Any:
    """Bind a durable ApprovalStore result to the protocol settlement ledger."""

    disclosure = await store.read_effect_disclosure_snapshot(scope)
    contract_v2 = disclosure is not None and disclosure.protocol == 2
    outcome = EffectSettlementOutcome.CONFIRMED_APPLIED if ok else EffectSettlementOutcome.CONFIRMED_NOT_APPLIED
    evidence = {
        "approval_id": approval.id,
        "decision_id": decision_id,
        "error": error,
        "ok": ok,
        "result_digest": canonical_digest_v1(result),
    }
    evidence_digest = canonical_digest_v1(evidence)
    object_version_digest = canonical_digest_v1(
        {
            "arguments_digest": approval.arguments_digest,
            "approval_id": approval.id,
            "tool_name": approval.tool_name,
        }
    )
    settlement_code = "approval_effect_applied" if ok else "approval_effect_not_applied"
    identity = (
        effect_control_identity_digest(
            attempt_id=attempt.attempt_id,
            provider_contract_key="approval_decision/v1",
            source=EffectSettlementSource.PROVIDER.value,
            outcome=outcome.value,
            settlement_code=settlement_code,
            evidence_digest=evidence_digest,
            object_version_digest=object_version_digest,
            actor_audit_ref="adapter:approval_decision_projection/v1",
            audit_object_ref=None,
            provider_external_ref_digest=None,
            provider_external_ref_key_version=None,
        )[:32]
        if contract_v2
        else evidence_digest[:32]
    )
    try:
        created_at = datetime.fromisoformat(str(approval.created_at).replace("Z", "+00:00"))
    except ValueError:
        created_at = datetime(1970, 1, 1, tzinfo=UTC)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    context = EffectSettlementContext(
        scope=scope,
        attempt_id=attempt.attempt_id,
        expected_attempt_row_version=attempt.row_version,
        settlement_id=(f"set_{identity}" if contract_v2 else f"tes_{identity}"),
        source=EffectSettlementSource.PROVIDER,
        outcome=outcome,
        settlement_code=settlement_code,
        principal_ref=("adapter:approval_decision_projection/v1" if contract_v2 else "provider:terminal-approval"),
        principal_role="provider_adapter",
        authz_snapshot_digest=authz_snapshot_digest,
        evidence_link_id=(f"evidence_{identity}" if contract_v2 else f"tel_{identity}"),
        evidence_digest=evidence_digest,
        object_version_digest=object_version_digest,
        purge_after=(None if contract_v2 else (created_at + timedelta(days=30)).astimezone(UTC).isoformat(timespec="seconds")),
    )
    if contract_v2:
        return await store.record_approval_projection_effect_settlement(context)
    return await store.record_effect_settlement(context)


__all__ = [
    "TerminalApprovalRecovery",
    "TerminalApprovalRunner",
    "settle_approval_effect_from_projection",
]
