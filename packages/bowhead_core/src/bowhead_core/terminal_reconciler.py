"""Terminal Protocol S3 recovery scanner/processor seam."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bowhead_core.context_activity import ContextActivity
from bowhead_core.terminal_approval_contract import (
    build_approval_required_events,
)
from bowhead_core.terminal_protocol import (
    ActorScope,
    CandidateOrigin,
    CandidateOutcome,
    CommitConfirmationDisposition,
    CommittedReceipt,
    ControlGenerationRequest,
    DecisionAction,
    DecisionPreflightSnapshot,
    DecisionSourceKind,
    EffectDisclosureSnapshot,
    EffectState,
    FinalAnswerMode,
    FrozenSteerSnapshot,
    PendingDecisionRecovery,
    PhaseScope,
    RecordedDecision,
    RecoveryDiscoveryContext,
    RecoveryProcessingContext,
    RecoveryWriterFence,
    SessionVariant,
    StagedTerminalEvent,
    TerminalCandidatePlan,
    TerminalCandidateVariant,
    TerminalFlushResult,
    TerminalStatus,
    TerminalWriterLease,
    canonical_digest_v1,
    is_narrow_system_decision,
)
from bowhead_core.terminal_protocol_store import (
    TERMINAL_TASK_START_GRACE_SECONDS,
    DecisionAccessRevokedError,
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
)


@dataclass(frozen=True, slots=True)
class RecoveryBuildSnapshot:
    processing: RecoveryProcessingContext
    flush: TerminalFlushResult
    effect_disclosure: EffectDisclosureSnapshot | None
    frozen_steer: FrozenSteerSnapshot | None = None
    context_completions: tuple[ContextActivity, ...] = ()
    operation_kind: str | None = None

    def __post_init__(self) -> None:
        if self.operation_kind not in (None, "context_maintenance"):
            raise ValueError("Recovery operation kind is invalid")
        if not isinstance(self.processing, RecoveryProcessingContext) or not isinstance(self.flush, TerminalFlushResult):
            raise TypeError("recovery build snapshot is invalid")
        if self.effect_disclosure is not None and (
            not isinstance(self.effect_disclosure, EffectDisclosureSnapshot) or self.effect_disclosure.scope != self.processing.scope
        ):
            raise TypeError("recovery effect disclosure snapshot is invalid")
        if self.processing.closing_accepted_through != self.flush.closing_accepted_through:
            raise ValueError("recovery build snapshot fence mismatch")
        if self.frozen_steer is not None and self.frozen_steer.scope != self.processing.scope:
            raise ValueError("recovery frozen steer scope mismatch")


@dataclass(frozen=True, slots=True)
class TerminalRecoverySweepResult:
    processed_count: int
    eligible_remaining: int
    pending_digest: str
    fence_acquired: bool
    next_due_at: str | None = None
    max_due_at: str | None = None
    page_cursor: str | None = None
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.processed_count < 0 or self.eligible_remaining < 0:
            raise ValueError("recovery sweep counts are invalid")
        if not isinstance(self.pending_digest, str) or not self.pending_digest:
            raise ValueError("recovery sweep pending digest is invalid")
        for value in (self.next_due_at, self.max_due_at, self.page_cursor):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("recovery sweep continuation is invalid")


RecoveryCandidateBuilder = Callable[
    [RecoveryBuildSnapshot],
    TerminalCandidatePlan | Awaitable[TerminalCandidatePlan],
]
PendingExecutionRecoverer = Callable[
    [PendingDecisionRecovery, RecoveryWriterFence | TerminalWriterLease],
    bool | Awaitable[bool],
]
SettledEffectRecoverer = Callable[
    [RecoveryProcessingContext, RecoveryWriterFence | TerminalWriterLease],
    bool | Awaitable[bool],
]
DecisionEffectRecoverer = Callable[
    [str],
    int | Awaitable[int],
]
PendingDecisionRevoker = Callable[
    [
        PendingDecisionRecovery,
        RecoveryWriterFence | TerminalWriterLease,
        str,
    ],
    bool | Awaitable[bool],
]
PendingControlAuthorizer = Callable[
    [RecordedDecision, DecisionPreflightSnapshot],
    None | Awaitable[None],
]


class TerminalReconciler:
    """Never retries execution; it only converges frozen terminal projections."""

    def __init__(
        self,
        store: TerminalProtocolStore,
        *,
        fault_injector: Callable[[TerminalProtocolFaultPoint], None] | None = None,
    ) -> None:
        self._store = store
        self._fault_injector = fault_injector

    def _inject(self, point: TerminalProtocolFaultPoint) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    async def scan(
        self,
        *,
        scanner_id: str,
        limit: int = 100,
        task_start_grace_seconds: int = TERMINAL_TASK_START_GRACE_SECONDS,
    ) -> tuple[RecoveryDiscoveryContext, ...]:
        return await self._store.list_recovery_candidates(
            scanner_id=scanner_id,
            limit=limit,
            task_start_grace_seconds=task_start_grace_seconds,
        )

    async def claim(
        self,
        discovery: RecoveryDiscoveryContext,
        *,
        owner_instance_id: str,
    ) -> RecoveryProcessingContext:
        record = await self._store.discover_recovery(discovery)
        return await self._store.claim_recovery(record, owner_instance_id=owner_instance_id)

    async def converge(
        self,
        processing: RecoveryProcessingContext,
        *,
        candidate_builder: RecoveryCandidateBuilder,
        selected_variant_key: str,
        authz_snapshot_digest: str,
        writer_admission_epoch: int,
    ) -> CommittedReceipt:
        flush = await self._store.flush_recovery_through(processing)
        effect_disclosure = await self._store.read_effect_disclosure_snapshot(processing.scope)
        frozen_steer = await self._store.read_recovery_frozen_steer_snapshot(processing)
        plan = await self._store.load_candidate_for_recovery(processing)
        loaded_candidate = plan is not None
        if (
            plan is None
            and effect_disclosure is not None
            and effect_disclosure.protocol == 2
            and all(attempt.state is EffectState.SETTLED or attempt.disclosure_kind == "accounting" for attempt in effect_disclosure.attempts)
        ):
            billing = await self._store.freeze_usage_and_claim_billing_attempt(
                processing,
            )
            if billing is not None:
                await self._store.record_local_billing_settlement(
                    processing,
                    billing,
                )
            effect_disclosure = await self._store.read_effect_disclosure_snapshot(
                processing.scope,
            )
        if plan is None:
            plan = await _await_if_needed(
                candidate_builder(
                    RecoveryBuildSnapshot(
                        processing=processing,
                        flush=flush,
                        effect_disclosure=effect_disclosure,
                        frozen_steer=frozen_steer,
                        context_completions=await self._store.read_recovery_context_completions(processing),
                        operation_kind=await self._store.read_recovery_operation_kind(processing),
                    )
                )
            )
        if not isinstance(plan, TerminalCandidatePlan):
            raise TypeError("recovery candidate builder returned an invalid plan")
        if loaded_candidate:
            if plan.origin is CandidateOrigin.NORMAL:
                cancelled_primary_keys = tuple(
                    variant.variant_key
                    for variant in plan.variants
                    if variant.outcome_variant is CandidateOutcome.PRIMARY and variant.status is TerminalStatus.CANCELLED
                )
                has_v2_disclosure = any(
                    str(extension.get("kind", "")).startswith(
                        "effect_disclosure_v2",
                    )
                    for extension in plan.projection_extensions
                )
                if has_v2_disclosure and len(cancelled_primary_keys) == 1:
                    selected_variant_key = cancelled_primary_keys[0]
                else:
                    recovery_keys = tuple(
                        variant.variant_key for variant in plan.variants if variant.outcome_variant is CandidateOutcome.RECOVERY_INTERRUPT
                    )
                    if len(recovery_keys) != 1:
                        raise RuntimeError(
                            "terminal_recovery_interrupt_candidate_malformed",
                        )
                    selected_variant_key = recovery_keys[0]
            else:
                if len(plan.variants) != 1 or plan.variants[0].outcome_variant not in {
                    CandidateOutcome.PRIMARY,
                    CandidateOutcome.RECOVERY_INTERRUPT,
                }:
                    raise RuntimeError("terminal_recovery_single_candidate_malformed")
                selected_variant_key = plan.variants[0].variant_key
        prepared = await self._store.prepare_recovery_candidate(processing, plan)
        self._inject(TerminalProtocolFaultPoint.RECOVERY_AFTER_PROJECTION_BEFORE_FINALIZER)
        try:
            return await self._store.commit_recovery_candidate(
                prepared,
                plan,
                selected_variant_key=selected_variant_key,
                authz_snapshot_digest=authz_snapshot_digest,
                writer_admission_epoch=writer_admission_epoch,
            )
        except Exception:
            confirmation = await self._store.confirm_commit(
                candidate_id=plan.candidate_id,
                candidate_projection_digest=plan.candidate_projection_digest,
            )
            if confirmation.disposition is CommitConfirmationDisposition.COMMITTED:
                assert confirmation.receipt is not None
                return confirmation.receipt
            raise


class TerminalRecoveryProcessor:
    """Bounded S5 recovery: never re-enters model, tool, or provider execution."""

    def __init__(
        self,
        store: TerminalProtocolStore,
        *,
        pending_execution_recoverer: PendingExecutionRecoverer | None = None,
        settled_effect_recoverer: SettledEffectRecoverer | None = None,
        decision_effect_recoverer: DecisionEffectRecoverer | None = None,
        pending_decision_revoker: PendingDecisionRevoker | None = None,
        pending_control_authorizer: PendingControlAuthorizer | None = None,
        fault_injector: Callable[[TerminalProtocolFaultPoint], None] | None = None,
    ) -> None:
        self._store = store
        self._reconciler = TerminalReconciler(
            store,
            fault_injector=fault_injector,
        )
        self._pending_execution_recoverer = pending_execution_recoverer
        self._settled_effect_recoverer = settled_effect_recoverer
        self._decision_effect_recoverer = decision_effect_recoverer
        self._pending_decision_revoker = pending_decision_revoker
        self._pending_control_authorizer = pending_control_authorizer
        self._fault_injector = fault_injector

    def _inject(self, point: TerminalProtocolFaultPoint) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    async def run_bounded(
        self,
        *,
        owner_instance_id: str,
        rollout_config_generation: str,
        page_size: int = 25,
        max_items: int = 100,
        writer_fence: RecoveryWriterFence | TerminalWriterLease | None = None,
    ) -> TerminalRecoverySweepResult:
        if not 1 <= page_size <= 100 or not 3 <= max_items <= 1000:
            raise ValueError("recovery sweep bounds are invalid")
        self._inject(TerminalProtocolFaultPoint.RECOVERY_STARTUP_BEFORE_OLD_LEASE_EXPIRY)
        owns_fence = writer_fence is None
        fence = writer_fence
        if fence is None:
            fence = await self._store.acquire_recovery_writer_fence(
                owner_instance_id=owner_instance_id,
                rollout_config_generation=rollout_config_generation,
            )
        if fence is None:
            remaining = await self._store.count_recovery_inventory()
            return _sweep_result(
                0,
                remaining,
                False,
                (),
                next_due_at=await self._store.read_recovery_next_due_at(),
                max_due_at=await self._store.read_recovery_max_due_at(),
                page_cursor=None,
            )
        processed: list[str] = []
        errors: list[str] = []
        examined = 0
        try:
            # Reserve bounded capacity for all three independent recovery
            # inventories. A backlog of effect rows must not prevent a newly
            # due decision or generation from being examined in this sweep.
            effect_budget = max(1, max_items // 3)
            effect_decisions = await self._store.list_pending_decision_effect_ids(
                limit=min(effect_budget, 100),
            )
            for decision_id in effect_decisions:
                examined += 1
                try:
                    if self._decision_effect_recoverer is None:
                        raise RuntimeError("terminal_recovery_decision_effect_executor_required")
                    applied = await _await_if_needed(self._decision_effect_recoverer(decision_id))
                    if applied:
                        processed.append(decision_id)
                except Exception as exc:
                    code = _bounded_error_code(exc)
                    errors.append(code)
                    try:
                        await self._store.defer_decision_effect_group(
                            decision_id=decision_id,
                            code=code,
                        )
                    except Exception as defer_exc:
                        errors.append(_bounded_error_code(defer_exc))
            remaining_after_effects = max_items - examined
            decision_budget = max(1, remaining_after_effects // 2) if remaining_after_effects >= 2 else remaining_after_effects
            pending = (
                await self._store.list_pending_decisions(
                    limit=min(decision_budget, 100),
                )
                if decision_budget > 0
                else ()
            )
            for item in pending:
                examined += 1
                try:
                    recovered = await self._recover_pending_decision(item, fence)
                    if recovered:
                        processed.append(item.decision.context.decision_id)
                except Exception as exc:
                    code = _bounded_error_code(exc)
                    errors.append(code)
                    await self._store.defer_pending_decision(
                        decision_id=item.decision.context.decision_id,
                        code=code,
                    )
            while examined < max_items:
                discoveries = await self._reconciler.scan(
                    scanner_id=owner_instance_id,
                    limit=min(page_size, max_items - examined),
                )
                if not discoveries:
                    break
                for discovery in discoveries:
                    examined += 1
                    record = None
                    processing: RecoveryProcessingContext | None = None
                    try:
                        record = await self._store.discover_recovery(discovery)
                        processing = await self._store.claim_recovery(
                            record,
                            owner_instance_id=owner_instance_id,
                        )
                        loaded = await self._store.load_candidate_for_recovery(processing)
                        disclosure = await self._store.read_effect_disclosure_snapshot(processing.scope)
                        if loaded is None and disclosure is not None:
                            if disclosure.protocol == 2 and disclosure.intent_count:
                                discarded = await self._store.discard_recovery_effect_intents(processing)
                                if discarded:
                                    disclosure = await self._store.read_effect_disclosure_snapshot(processing.scope)
                            recovered = False
                            specialized_effect = not processing.cancelled_finalization and _needs_specialized_effect_recoverer(disclosure)
                            if specialized_effect:
                                if self._settled_effect_recoverer is not None:
                                    recovered = await _await_if_needed(
                                        self._settled_effect_recoverer(
                                            processing,
                                            fence,
                                        )
                                    )
                            if recovered:
                                processed.append(processing.recovery_id)
                                continue
                            if disclosure.protocol == 1 and disclosure.has_only_uncommitted_intents:
                                discarded = await self._store.discard_recovery_effect_intents(processing)
                                if discarded:
                                    disclosure = await self._store.read_effect_disclosure_snapshot(processing.scope)
                            if (
                                disclosure is not None
                                and disclosure.unsettled_count
                                and not processing.cancelled_finalization
                                and (
                                    disclosure.protocol == 1
                                    or any(
                                        attempt.state is not EffectState.SETTLED and attempt.disclosure_kind != "accounting"
                                        for attempt in disclosure.attempts
                                    )
                                )
                            ):
                                await self._store.flush_recovery_through(processing)
                                if specialized_effect:
                                    event = await self._store.record_unknown_effect_process_event(processing)
                                    processed.append(event.event_id)
                                else:
                                    processed.append(processing.recovery_id)
                                await self._store.defer_recovery(
                                    processing,
                                    code="effect_outcome_unknown",
                                    delay_seconds=30,
                                )
                                continue
                        steer = await self._store.read_recovery_frozen_steer_snapshot(processing)
                        variant_key = (
                            "recovery_discard"
                            if processing.cancelled_finalization and steer.items
                            else "recovery_deferred"
                            if steer.items
                            else "recovery_none"
                        )
                        auth_digest = await self._store.read_recovery_auth_fence_digest(processing)
                        receipt = await self._reconciler.converge(
                            processing,
                            candidate_builder=(_build_cancelled_candidate if processing.cancelled_finalization else _build_interrupt_candidate),
                            selected_variant_key=variant_key,
                            authz_snapshot_digest=auth_digest,
                            writer_admission_epoch=fence.writer_admission_epoch,
                        )
                        processed.append(receipt.receipt_id)
                    except Exception as exc:
                        errors.append(_bounded_error_code(exc))
                        if processing is not None:
                            try:
                                await self._store.defer_recovery(
                                    processing,
                                    code=errors[-1],
                                    delay_seconds=30,
                                )
                            except Exception as defer_exc:
                                errors.append(_bounded_error_code(defer_exc))
                        elif record is not None:
                            try:
                                await self._store.defer_discovered_recovery(
                                    record,
                                    code=errors[-1],
                                    delay_seconds=30,
                                )
                            except Exception as defer_exc:
                                errors.append(_bounded_error_code(defer_exc))
                        continue
                if len(discoveries) < page_size or examined >= max_items:
                    break
                if owns_fence:
                    assert isinstance(fence, RecoveryWriterFence)
                    fence = await self._store.heartbeat_recovery_writer_fence(fence)
        finally:
            if owns_fence:
                assert isinstance(fence, RecoveryWriterFence)
                await self._store.release_recovery_writer_fence(fence)
        remaining = await self._store.count_recovery_inventory()
        return _sweep_result(
            len(processed),
            remaining,
            True,
            tuple(errors),
            next_due_at=await self._store.read_recovery_next_due_at(
                include_writer_lease=False,
            ),
            max_due_at=await self._store.read_recovery_max_due_at(
                include_writer_lease=False,
            ),
            page_cursor=(processed[-1] if processed and remaining else None),
        )

    async def _recover_pending_decision(
        self,
        pending: PendingDecisionRecovery,
        fence: RecoveryWriterFence | TerminalWriterLease,
    ) -> bool:
        context = pending.decision.context
        try:
            rehydrated = await self._store.read_source_decision(
                team_id=context.scope.team_id,
                session_id=context.scope.session_id,
                turn_id=context.scope.turn_id,
                source_kind=context.source_kind,
                source_id=context.source_id,
                action=context.action,
                principals=context.principals,
            )
        except DecisionAccessRevokedError as exc:
            return await self._revoke_pending_decision(
                pending,
                fence,
                getattr(exc, "reason_code", "access_revoked"),
            )
        if rehydrated is None:
            return await self._revoke_pending_decision(
                pending,
                fence,
                "dependency_missing",
            )
        decision, preflight = rehydrated
        if (
            decision.decision_digest != pending.decision.decision_digest
            or preflight.generation_transition_epoch != pending.generation_transition_epoch
            or preflight.prior_receipt_id != pending.decision.context.prior_receipt_id
        ):
            return await self._revoke_pending_decision(
                pending,
                fence,
                "dependency_missing",
            )
        if decision.context.action not in {DecisionAction.RESUME, DecisionAction.APPROVE} and not is_narrow_system_decision(decision.context):
            if self._pending_control_authorizer is None:
                raise RuntimeError("terminal_recovery_control_authorizer_required")
            try:
                await _await_if_needed(
                    self._pending_control_authorizer(
                        decision,
                        preflight,
                    )
                )
            except DecisionAccessRevokedError as exc:
                return await self._revoke_pending_decision(
                    pending,
                    fence,
                    getattr(exc, "reason_code", "access_revoked"),
                )
        if decision.context.action in {DecisionAction.RESUME, DecisionAction.APPROVE}:
            if self._pending_execution_recoverer is None:
                raise RuntimeError("terminal_recovery_execution_attach_required")
            try:
                recovered = await _await_if_needed(self._pending_execution_recoverer(pending, fence))
            except DecisionAccessRevokedError as exc:
                return await self._revoke_pending_decision(
                    pending,
                    fence,
                    getattr(exc, "reason_code", "access_revoked"),
                )
            if not recovered:
                raise RuntimeError("terminal_recovery_execution_attach_deferred")
            return True
        status, reason_code, message_key = _CONTROL_TERMINAL_MAP[decision.context.action]
        if decision.context.source_kind is DecisionSourceKind.SYSTEM_CONTROL and decision.context.action is DecisionAction.REVOKE:
            outcome = await self._store.read_decision_control_outcome(
                decision_id=decision.context.decision_id,
            )
            if outcome in {"dependency_missing", "entitlement_denied"}:
                status = TerminalStatus.FAILED
                reason_code = "terminal_dependency_missing"
                message_key = "turn.failed"
        await self._store.commit_control_generation(
            ControlGenerationRequest(
                decision=decision,
                status=status,
                reason_code=reason_code,
                message_key=message_key,
                writer_instance_id=fence.owner_instance_id,
                writer_lease_epoch=fence.writer_lease_epoch,
                writer_admission_epoch=fence.writer_admission_epoch,
                current_authz_snapshot_digest=(preflight.authz_snapshot_digest),
            )
        )
        return True

    async def _revoke_pending_decision(
        self,
        pending: PendingDecisionRecovery,
        fence: RecoveryWriterFence | TerminalWriterLease,
        denial_reason_code: str,
    ) -> bool:
        if self._pending_decision_revoker is None:
            raise RuntimeError("terminal_recovery_revoker_required")
        revoked = await _await_if_needed(
            self._pending_decision_revoker(
                pending,
                fence,
                denial_reason_code,
            )
        )
        if not revoked:
            raise RuntimeError("terminal_recovery_revocation_deferred")
        return True


async def converge_worker_start_failure(
    store: TerminalProtocolStore,
    *,
    scope: object,
    owner_instance_id: str,
    writer_admission_epoch: int,
) -> CommittedReceipt:
    """Immediately seal a claimed-but-unstarted generation without forging start."""

    from bowhead_core.terminal_protocol import TerminalScope

    if not isinstance(scope, TerminalScope):
        raise TypeError("worker-start recovery scope is invalid")
    reconciler = TerminalReconciler(store)
    discovery = await store.read_recovery_discovery_for_scope(
        scope,
        scanner_id=owner_instance_id,
    )
    processing = await reconciler.claim(
        discovery,
        owner_instance_id=owner_instance_id,
    )
    auth_digest = await store.read_recovery_auth_fence_digest(processing)
    return await reconciler.converge(
        processing,
        candidate_builder=_build_worker_start_failed_candidate,
        selected_variant_key="recovery_none",
        authz_snapshot_digest=auth_digest,
        writer_admission_epoch=writer_admission_epoch,
    )


async def converge_settled_effect_recovery(
    store: TerminalProtocolStore,
    *,
    processing: RecoveryProcessingContext,
    fence: RecoveryWriterFence | TerminalWriterLease,
    approval_id: str,
    decision_id: str,
    ok: bool,
    result_digest: str | None,
    fault_injector: Callable[[TerminalProtocolFaultPoint], None] | None = None,
) -> CommittedReceipt:
    """Finish a provider effect whose durable projection proves its outcome."""

    if any(not isinstance(value, str) or not value for value in (approval_id, decision_id)):
        raise ValueError("settled effect recovery identity is invalid")
    auth_digest = await store.read_recovery_auth_fence_digest(processing)
    reconciler = TerminalReconciler(
        store,
        fault_injector=fault_injector,
    )

    def candidate(snapshot: RecoveryBuildSnapshot) -> TerminalCandidatePlan:
        return _build_settled_effect_candidate(
            snapshot,
            approval_id=approval_id,
            decision_id=decision_id,
            ok=ok,
            result_digest=result_digest,
        )

    session_variant = SessionVariant.DEFERRED if (await store.read_recovery_frozen_steer_snapshot(processing)).items else SessionVariant.NONE
    return await reconciler.converge(
        processing,
        candidate_builder=candidate,
        selected_variant_key=f"settled_effect_{session_variant.value}",
        authz_snapshot_digest=auth_digest,
        writer_admission_epoch=fence.writer_admission_epoch,
    )


async def continue_bounded_terminal_recovery(
    processor: TerminalRecoveryProcessor,
    initial: TerminalRecoverySweepResult,
    *,
    owner_instance_id: str,
    rollout_config_generation: str,
    processing_budget_seconds: int = 60,
    clock_skew_guard_seconds: int = 2,
    writer_fence: RecoveryWriterFence | TerminalWriterLease | None = None,
) -> TerminalRecoverySweepResult:
    """Continue the non-blocking startup sweep until durable inventory converges."""

    if processing_budget_seconds < 1 or clock_skew_guard_seconds < 0:
        raise ValueError("startup recovery timing is invalid")
    result = initial
    deadline = datetime.now(tz=UTC) + timedelta(seconds=processing_budget_seconds)
    if initial.max_due_at is not None:
        observed_max_due = datetime.fromisoformat(initial.max_due_at.replace("Z", "+00:00"))
        deadline = max(
            deadline,
            observed_max_due + timedelta(seconds=(clock_skew_guard_seconds + processing_budget_seconds)),
        )
    while result.eligible_remaining or result.next_due_at is not None or result.max_due_at is not None:
        now = datetime.now(tz=UTC)
        if result.next_due_at is not None:
            due = datetime.fromisoformat(result.next_due_at.replace("Z", "+00:00"))
            delay = max(
                0.0,
                (due - now).total_seconds() + clock_skew_guard_seconds,
            )
        else:
            delay = 1.0
        if now >= deadline:
            return result
        remaining = (deadline - now).total_seconds()
        if delay >= remaining:
            return result
        await asyncio.sleep(min(delay, remaining, 30.0))
        result = await processor.run_bounded(
            owner_instance_id=owner_instance_id,
            rollout_config_generation=rollout_config_generation,
            writer_fence=writer_fence,
        )
    return result


_CONTROL_TERMINAL_MAP = {
    DecisionAction.RESOLVE_WITHOUT_RESUME: (
        TerminalStatus.CANCELLED,
        "clarification_resolved",
        "turn.clarification_resolved_for_next_turn",
    ),
    DecisionAction.DISMISS: (
        TerminalStatus.CANCELLED,
        "clarification_dismissed",
        "turn.clarification_dismissed",
    ),
    DecisionAction.REJECT: (
        TerminalStatus.CANCELLED,
        "approval_rejected",
        "turn.approval_rejected",
    ),
    DecisionAction.CANCEL: (
        TerminalStatus.CANCELLED,
        "user_cancelled",
        "turn.cancelled",
    ),
    DecisionAction.EXPIRE: (
        TerminalStatus.ABORTED,
        "decision_expired",
        "turn.decision_expired",
    ),
    DecisionAction.REVOKE: (
        TerminalStatus.ABORTED,
        "access_revoked",
        "turn.access_revoked",
    ),
}


def _needs_specialized_effect_recoverer(
    disclosure: EffectDisclosureSnapshot,
) -> bool:
    """Route only legacy or exact approval shapes through approval recovery."""

    if disclosure.protocol == 1:
        return True
    approval_attempts = tuple(attempt for attempt in disclosure.attempts if attempt.provider_contract_key == "approval_decision/v1")
    return bool(
        len(approval_attempts) == 1 and all(attempt in approval_attempts or attempt.state is EffectState.SETTLED for attempt in disclosure.attempts)
    )


def _build_interrupt_candidate(snapshot: RecoveryBuildSnapshot) -> TerminalCandidatePlan:
    return _build_fixed_interrupt_candidate(
        snapshot,
        reason_code="terminal_projection_not_committed",
        message_key="turn.result_not_committed",
    )


def _build_cancelled_candidate(
    snapshot: RecoveryBuildSnapshot,
) -> TerminalCandidatePlan:
    return _build_fixed_interrupt_candidate(
        snapshot,
        reason_code="user_cancelled",
        message_key="turn.cancelled",
        status=TerminalStatus.CANCELLED,
        outcome_variant=CandidateOutcome.PRIMARY,
    )


def _build_worker_start_failed_candidate(snapshot: RecoveryBuildSnapshot) -> TerminalCandidatePlan:
    return _build_fixed_interrupt_candidate(
        snapshot,
        reason_code="worker_start_failed",
        message_key="turn.aborted",
    )


def _build_fixed_interrupt_candidate(
    snapshot: RecoveryBuildSnapshot,
    *,
    reason_code: str,
    message_key: str,
    status: TerminalStatus = TerminalStatus.ABORTED,
    outcome_variant: CandidateOutcome = CandidateOutcome.RECOVERY_INTERRUPT,
) -> TerminalCandidatePlan:
    scope = snapshot.processing.scope
    if (
        status is not TerminalStatus.CANCELLED
        and snapshot.effect_disclosure is not None
        and snapshot.effect_disclosure.protocol == 2
        and snapshot.effect_disclosure.unsettled_count
    ):
        raise RuntimeError("terminal_recovery_effect_outcome_unknown")
    identity = canonical_digest_v1(
        {
            "finalization_epoch": snapshot.processing.finalization_epoch,
            "recovery_id": snapshot.processing.recovery_id,
            "turn_id": scope.turn_id,
            "generation": scope.generation,
        }
    )[:32]
    candidate_id = f"tc_{identity}"
    receipt_id = f"tr_{identity}"
    done_id = f"evt_done_{identity}"
    session_variant = (
        SessionVariant.DISCARD
        if status is TerminalStatus.CANCELLED and snapshot.frozen_steer is not None and snapshot.frozen_steer.items
        else SessionVariant.DEFERRED
        if snapshot.frozen_steer is not None and snapshot.frozen_steer.items
        else SessionVariant.NONE
    )
    variant_key = f"recovery_{session_variant.value}"
    required: list[StagedTerminalEvent] = [
        StagedTerminalEvent.from_payload(
            event_id=activity.event_id(),
            event_type="agent.context",
            actor_scope=ActorScope.PEER if activity.subagent_id is not None else ActorScope.ROOT,
            phase_scope=PhaseScope.IN_GENERATION,
            payload=activity.payload(scope.turn_id),
        )
        for activity in snapshot.context_completions
    ]
    if snapshot.effect_disclosure is not None and snapshot.effect_disclosure.attempt_count > 0:
        event_type = (
            "effect_settlement"
            if snapshot.effect_disclosure.protocol == 2 and not snapshot.effect_disclosure.unsettled_count
            else "effect_disclosure"
        )
        required.append(
            StagedTerminalEvent.from_payload(
                event_id=f"evt_effect_{identity}",
                event_type=event_type,
                actor_scope=ActorScope.ROOT,
                phase_scope=PhaseScope.IN_GENERATION,
                payload=snapshot.effect_disclosure.event_payload,
            )
        )
        if snapshot.effect_disclosure.unsettled_count and status is not TerminalStatus.CANCELLED:
            required.append(
                StagedTerminalEvent.from_payload(
                    event_id=f"evt_unknown_{identity}",
                    event_type="interrupted_check_actions",
                    actor_scope=ActorScope.ROOT,
                    phase_scope=PhaseScope.IN_GENERATION,
                    payload={
                        "message_key": ("turn.interrupted_check_actions"),
                        "reason_code": "effect_outcome_unknown",
                        "unsettled_count": (snapshot.effect_disclosure.unsettled_count),
                    },
                )
            )
    manifest = [
        {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "ordinal": ordinal,
            "payload_digest": event.payload_digest,
        }
        for ordinal, event in enumerate(required, start=1)
    ]
    done = StagedTerminalEvent.from_payload(
        event_id=done_id,
        event_type="done",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={
            "candidate_id": candidate_id,
            **({"operation_kind": snapshot.operation_kind} if snapshot.operation_kind is not None else {}),
            "generation": scope.generation,
            "message_key": message_key,
            "protocol": 1,
            "reason": reason_code,
            "receipt_id": receipt_id,
            "required_manifest_template_digest": canonical_digest_v1(manifest),
            "selected_variant_key": variant_key,
            "status": status.value,
        },
    )
    variant = TerminalCandidateVariant(
        operation_kind=snapshot.operation_kind,
        variant_key=variant_key,
        outcome_variant=outcome_variant,
        session_variant=session_variant,
        status=status,
        reason_code=reason_code,
        message_key=message_key,
        final_answer_mode=FinalAnswerMode.NONE,
        final_answer_event_id=None,
        final_answer_digest=None,
        session_mutation_template={},
        events=(*required, done),
    )
    return TerminalCandidatePlan(
        scope=scope,
        candidate_id=candidate_id,
        reserved_receipt_id=receipt_id,
        reserved_terminal_event_id=done_id,
        origin=CandidateOrigin.RECOVERY,
        closing_accepted_through=snapshot.flush.closing_accepted_through,
        persisted_event_highwater=snapshot.flush.persisted_event_highwater,
        variants=(variant,),
        session_disposition_plan={
            "turn_projection": {"intent": ("cancelled" if status is TerminalStatus.CANCELLED else "recovery_interrupt")},
            "variant_templates": {variant_key: {}},
        },
        projection_extensions=(
            snapshot.effect_disclosure.projection_extensions
            if snapshot.effect_disclosure is not None and snapshot.effect_disclosure.protocol == 2
            else ()
        ),
    )


def _build_settled_effect_candidate(
    snapshot: RecoveryBuildSnapshot,
    *,
    approval_id: str,
    decision_id: str,
    ok: bool,
    result_digest: str | None,
) -> TerminalCandidatePlan:
    disclosure = snapshot.effect_disclosure
    if disclosure is None or disclosure.unsettled_count != 0:
        raise RuntimeError("terminal_recovery_effect_settlement_incomplete")
    scope = snapshot.processing.scope
    identity = canonical_digest_v1(
        {
            "approval_id": approval_id,
            "effect_snapshot_digest": disclosure.snapshot_digest,
            "recovery_id": snapshot.processing.recovery_id,
            "result_digest": result_digest,
        }
    )[:32]
    candidate_id = f"tc_{identity}"
    receipt_id = f"tr_{identity}"
    done_id = f"evt_done_{identity}"
    session_variant = SessionVariant.DEFERRED if snapshot.frozen_steer is not None and snapshot.frozen_steer.items else SessionVariant.NONE
    variant_key = f"settled_effect_{session_variant.value}"
    status = TerminalStatus.COMPLETED if ok else TerminalStatus.FAILED
    reason_code = None if ok else "terminal_dependency_missing"
    message_key = "turn.completed" if ok else "turn.failed"
    required = list(
        build_approval_required_events(
            decision_id=decision_id,
            approval_id=approval_id,
            ok=ok,
            result_digest=result_digest,
            effect_settlement_payload=disclosure.event_payload,
        )
    )
    answer_mode = FinalAnswerMode.NONE
    answer_id: str | None = None
    answer_digest: str | None = None
    if ok:
        answer = next(event for event in required if event.event_type == "final_answer")
        answer_mode = FinalAnswerMode.IN_TERMINAL_BATCH
        answer_id = answer.event_id
        answer_digest = answer.payload_digest
    manifest = [
        {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "ordinal": ordinal,
            "payload_digest": event.payload_digest,
        }
        for ordinal, event in enumerate(required, start=1)
    ]
    done = StagedTerminalEvent.from_payload(
        event_id=done_id,
        event_type="done",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={
            "candidate_id": candidate_id,
            "generation": scope.generation,
            "message_key": message_key,
            "protocol": 1,
            "reason": reason_code,
            "receipt_id": receipt_id,
            "required_manifest_template_digest": canonical_digest_v1(manifest),
            "selected_variant_key": variant_key,
            "status": status.value,
        },
    )
    variant = TerminalCandidateVariant(
        variant_key=variant_key,
        outcome_variant=CandidateOutcome.PRIMARY,
        session_variant=session_variant,
        status=status,
        reason_code=reason_code,
        message_key=message_key,
        final_answer_mode=answer_mode,
        final_answer_event_id=answer_id,
        final_answer_digest=answer_digest,
        session_mutation_template={},
        events=(*required, done),
    )
    return TerminalCandidatePlan(
        scope=scope,
        candidate_id=candidate_id,
        reserved_receipt_id=receipt_id,
        reserved_terminal_event_id=done_id,
        origin=CandidateOrigin.RECOVERY,
        closing_accepted_through=snapshot.flush.closing_accepted_through,
        persisted_event_highwater=snapshot.flush.persisted_event_highwater,
        variants=(variant,),
        session_disposition_plan={
            "turn_projection": {"intent": "settled_effect_recovery"},
            "variant_templates": {variant_key: {}},
        },
        projection_extensions=(disclosure.projection_extensions if disclosure.protocol == 2 else ()),
    )


def _bounded_error_code(error: BaseException) -> str:
    name = type(error).__name__.lower()
    return f"terminal_recovery_{name}"[:128]


def _sweep_result(
    processed_count: int,
    remaining: int,
    fence_acquired: bool,
    errors: tuple[str, ...],
    *,
    next_due_at: str | None,
    max_due_at: str | None,
    page_cursor: str | None,
) -> TerminalRecoverySweepResult:
    return TerminalRecoverySweepResult(
        processed_count=processed_count,
        eligible_remaining=remaining,
        pending_digest=canonical_digest_v1(
            {
                "eligible_remaining": remaining,
                "errors": list(errors),
                "processed_count": processed_count,
                "next_due_at": next_due_at,
                "max_due_at": max_due_at,
                "page_cursor": page_cursor,
            }
        ),
        fence_acquired=fence_acquired,
        next_due_at=next_due_at,
        max_due_at=max_due_at,
        page_cursor=page_cursor,
        errors=errors,
    )


async def _await_if_needed[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = [
    "RecoveryBuildSnapshot",
    "TerminalReconciler",
    "TerminalRecoveryProcessor",
    "TerminalRecoverySweepResult",
    "continue_bounded_terminal_recovery",
]
