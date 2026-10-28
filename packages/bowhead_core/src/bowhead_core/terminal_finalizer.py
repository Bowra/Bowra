"""Terminal Protocol S3 finalization orchestration.

This module owns process-local ordering only. SQLite truth remains exclusively in
``TerminalProtocolStore`` and production lifecycle wiring remains an S4 concern.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anyio

from bowhead_core.terminal_effect_contract import EffectIntentDisposition
from bowhead_core.terminal_protocol import (
    CommitConfirmationDisposition,
    CommittedReceipt,
    EffectDisclosureSnapshot,
    ExecutionGenerationContext,
    FinalizationContext,
    TerminalCandidatePlan,
    TerminalFlushResult,
)
from bowhead_core.terminal_protocol_store import TerminalProtocolFaultPoint, TerminalProtocolStore


@dataclass(frozen=True, slots=True)
class FinalizationBuildSnapshot:
    finalization: FinalizationContext
    flush: TerminalFlushResult
    effect_disclosure: EffectDisclosureSnapshot | None

    def __post_init__(self) -> None:
        if not isinstance(self.finalization, FinalizationContext) or not isinstance(self.flush, TerminalFlushResult):
            raise TypeError("finalization build snapshot is invalid")
        if self.effect_disclosure is not None and (
            not isinstance(self.effect_disclosure, EffectDisclosureSnapshot) or self.effect_disclosure.scope != self.finalization.scope
        ):
            raise TypeError("finalization effect disclosure snapshot is invalid")
        if self.finalization.closing_accepted_through != self.flush.closing_accepted_through:
            raise ValueError("finalization build snapshot fence mismatch")


CandidateBuilder = Callable[[FinalizationBuildSnapshot], TerminalCandidatePlan | Awaitable[TerminalCandidatePlan]]
ProducerAdmissionCloser = Callable[[ExecutionGenerationContext], None | Awaitable[None]]
ProducerBarrier = Callable[[ExecutionGenerationContext], None | Awaitable[None]]
FinalizerFaultInjector = Callable[[TerminalProtocolFaultPoint], None]


@dataclass(frozen=True, slots=True)
class FinalizeRequest:
    execution: ExecutionGenerationContext
    finalization_owner_instance_id: str
    closing_reason: str
    selected_variant_key: str | None
    authz_snapshot_digest: str
    writer_admission_epoch: int
    candidate_builder: CandidateBuilder
    close_producer_admission: ProducerAdmissionCloser | None = None
    producer_barrier: ProducerBarrier | None = None
    producer_barrier_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.execution, ExecutionGenerationContext):
            raise TypeError("finalize execution context is invalid")
        strings = (
            self.finalization_owner_instance_id,
            self.closing_reason,
            self.authz_snapshot_digest,
        )
        if any(not isinstance(value, str) or not value for value in strings):
            raise ValueError("finalize request identity is invalid")
        if self.selected_variant_key is not None and (not isinstance(self.selected_variant_key, str) or not self.selected_variant_key):
            raise ValueError("finalize selected variant is invalid")
        if not isinstance(self.writer_admission_epoch, int) or isinstance(self.writer_admission_epoch, bool) or self.writer_admission_epoch < 0:
            raise ValueError("finalize writer admission epoch is invalid")
        if self.producer_barrier_timeout_seconds <= 0:
            raise ValueError("producer barrier timeout must be positive")


class TerminalFinalizer:
    """Close producers, flush the durable fence, then atomically commit terminal truth."""

    def __init__(
        self,
        store: TerminalProtocolStore,
        *,
        _fault_injector: FinalizerFaultInjector | None = None,
    ) -> None:
        self._store = store
        self._fault_injector = _fault_injector

    async def finalize(self, request: FinalizeRequest) -> CommittedReceipt:
        if not isinstance(request, FinalizeRequest):
            raise TypeError("finalize request is invalid")
        if request.close_producer_admission is not None:
            await _await_if_needed(request.close_producer_admission(request.execution))
        if request.producer_barrier is not None:
            with anyio.fail_after(request.producer_barrier_timeout_seconds):
                await _await_if_needed(request.producer_barrier(request.execution))
        contract = await self._store.read_effect_execution_contract(
            request.execution.scope,
        )
        if contract.disposition is EffectIntentDisposition.RECOVERY_ONLY:
            billing = await self._store.freeze_usage_and_claim_billing_attempt(
                request.execution,
            )
            if billing is not None:
                await self._store.record_local_billing_settlement(
                    request.execution,
                    billing,
                )
        # Only freeze accepted_through after every producer admitted before
        # closure has finished accepting/persisting its business events.
        finalization = await self._store.begin_finalization(
            request.execution,
            finalization_owner_instance_id=request.finalization_owner_instance_id,
            closing_reason=request.closing_reason,
        )
        return await self._commit_finalization(request, finalization)

    async def finalize_cancelled(
        self,
        request: FinalizeRequest,
    ) -> CommittedReceipt:
        """Use the atomic Stop seam for contract-v2; preserve historical v1."""

        if not isinstance(request, FinalizeRequest):
            raise TypeError("finalize request is invalid")
        if request.close_producer_admission is not None:
            await _await_if_needed(
                request.close_producer_admission(request.execution),
            )
        contract = await self._store.read_effect_execution_contract(
            request.execution.scope,
        )
        if contract.disposition is EffectIntentDisposition.RECOVERY_ONLY:
            finalization = await self._store.begin_cancelled_finalization_with_local_billing(
                request.execution,
                finalization_owner_instance_id=(request.finalization_owner_instance_id),
            )
            assert isinstance(finalization, FinalizationContext)
        else:
            finalization = await self._store.begin_finalization(
                request.execution,
                finalization_owner_instance_id=(request.finalization_owner_instance_id),
                closing_reason=request.closing_reason,
            )
        return await self._commit_finalization(request, finalization)

    async def _commit_finalization(
        self,
        request: FinalizeRequest,
        finalization: FinalizationContext,
    ) -> CommittedReceipt:
        self._inject(TerminalProtocolFaultPoint.FINALIZE_AFTER_BARRIER)
        flush = await self._store.flush_finalization_through(finalization)
        self._inject(TerminalProtocolFaultPoint.FINALIZE_AFTER_FLUSH)
        effect_disclosure = await self._store.read_effect_disclosure_snapshot(finalization.scope)
        plan = await _await_if_needed(
            request.candidate_builder(
                FinalizationBuildSnapshot(
                    finalization=finalization,
                    flush=flush,
                    effect_disclosure=effect_disclosure,
                )
            )
        )
        if not isinstance(plan, TerminalCandidatePlan):
            raise TypeError("candidate builder returned an invalid plan")
        prepared = await self._store.prepare_candidate(finalization, plan)
        try:
            return await self._store.commit_candidate(
                prepared,
                plan,
                selected_variant_key=request.selected_variant_key,
                authz_snapshot_digest=request.authz_snapshot_digest,
                writer_admission_epoch=request.writer_admission_epoch,
            )
        except asyncio.CancelledError:
            # Cancellation can arrive after SQLite committed but before the
            # commit acknowledgement returned.  Confirm under a shield so an
            # HTTP disconnect cannot turn durable terminal truth into an
            # unknown outcome.  A genuinely absent commit keeps cancellation
            # semantics; the runtime factory then hands the scope to recovery.
            confirmation_task = asyncio.create_task(
                self._store.confirm_commit(
                    candidate_id=plan.candidate_id,
                    candidate_projection_digest=plan.candidate_projection_digest,
                )
            )
            confirmation = await asyncio.shield(confirmation_task)
            if confirmation.disposition is CommitConfirmationDisposition.COMMITTED:
                assert confirmation.receipt is not None
                return confirmation.receipt
            raise
        except Exception:
            confirmation = await self._store.confirm_commit(
                candidate_id=plan.candidate_id,
                candidate_projection_digest=plan.candidate_projection_digest,
            )
            if confirmation.disposition is CommitConfirmationDisposition.COMMITTED:
                assert confirmation.receipt is not None
                return confirmation.receipt
            raise

    def _inject(self, point: TerminalProtocolFaultPoint) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)


async def _await_if_needed[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = ["FinalizationBuildSnapshot", "FinalizeRequest", "TerminalFinalizer"]
