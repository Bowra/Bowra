"""Durable contract-v2 hook for each physical LLM provider dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

from bowhead_router.errors import (
    InvocationOutcomeUnknownError,
    InvocationTargetsExhaustedError,
    LLMTargetFailure,
    StreamingInterruptedError,
    StreamingTargetsExhaustedError,
)
from bowhead_router.invocation import (
    PhysicalInvocationAttempt,
    PhysicalInvocationHook,
    PhysicalInvocationSpec,
    fail_physical_invocation,
    physical_invocation_scope,
)
from bowhead_router.types import TokenUsage
from bowhead_router.usage import detached_usage_context

from bowhead_core.events import OrchestratorEvent, TerminalDurabilityAbort
from bowhead_core.llm_failure import project_llm_failure
from bowhead_core.published_diagnostics import build_chat_llm_failure
from bowhead_core.terminal_effect_contract import (
    ClassifiedToolEffectResult,
    EffectIntentDisposition,
    EffectProbeEvidence,
    ToolEffectIdempotency,
    ToolEffectSpec,
    effect_control_identity_digest,
    get_effect_provider_contract,
)
from bowhead_core.terminal_protocol import (
    ActorScope,
    EffectAttemptContext,
    EffectClass,
    EffectSettlementContext,
    EffectSettlementOutcome,
    EffectSettlementSource,
    EffectState,
    EventTicket,
    TerminalProtocolMalformedError,
    canonical_digest_v1,
)
from bowhead_core.terminal_protocol_store import TerminalReadContext

_LLM_PROVIDER_CONTRACT = "llm_provider/v1"
_LLM_ADAPTER_PRINCIPAL = "adapter:llm_provider_receipt/v1"
_RECEIPT_WAIT_TIMEOUT_SECONDS = 2.0
_RECEIPT_PROBE_INTERVAL_SECONDS = 0.05

_CURRENT_DURABLE_HOOK: ContextVar[TerminalLLMInvocationHook | None] = ContextVar(
    "bowhead_terminal_effect_hook",
    default=None,
)
_CURRENT_TOOL_TICKET: ContextVar[EventTicket | None] = ContextVar(
    "bowhead_terminal_tool_ticket",
    default=None,
)


@dataclass(slots=True)
class _InvocationState:
    lock: asyncio.Lock
    poisoned: bool = False


class TerminalLLMInvocationAttempt(PhysicalInvocationAttempt):
    def __init__(
        self,
        *,
        hook: TerminalLLMInvocationHook,
        spec: PhysicalInvocationSpec,
        attempt: EffectAttemptContext,
    ) -> None:
        self._hook = hook
        self._spec = spec
        self._attempt = attempt

    async def confirmed_not_applied(
        self,
        *,
        evidence_code: str,
        evidence_digest: str,
    ) -> bool:
        return await self._hook.settle(
            spec=self._spec,
            attempt=self._attempt,
            outcome=EffectSettlementOutcome.CONFIRMED_NOT_APPLIED,
            settlement_code=evidence_code,
            usage=TokenUsage(),
            response_digest=evidence_digest,
        )

    async def complete(self, *, usage: TokenUsage, response_digest: str) -> bool:
        return await self._hook.settle(
            spec=self._spec,
            attempt=self._attempt,
            outcome=EffectSettlementOutcome.CONFIRMED_APPLIED,
            settlement_code="llm_provider_completed/v1",
            usage=usage,
            response_digest=response_digest,
        )

    async def unknown(self, *, reason_code: str) -> None:
        await self._hook.mark_unknown(
            attempt=self._attempt,
            reason_code=reason_code,
            scanner_prefix="llm_transport",
        )


@dataclass(frozen=True, slots=True)
class TerminalToolInvocationAttempt:
    hook: TerminalLLMInvocationHook
    contract_key: str
    provider_token: str
    attempt: EffectAttemptContext

    async def complete(self, evidence: EffectProbeEvidence) -> bool:
        return await self.hook.settle_tool(
            contract_key=self.contract_key,
            attempt=self.attempt,
            evidence=evidence,
        )

    async def unknown(self, *, reason_code: str) -> None:
        await self.hook.mark_unknown(
            attempt=self.attempt,
            reason_code=reason_code,
            scanner_prefix="tool_handler",
        )


class TerminalLLMInvocationHook(PhysicalInvocationHook):
    """One generation-scoped admission/poison latch shared by copied tasks."""

    def __init__(
        self,
        runtime: Any,
        *,
        cost_estimator: Callable[[int, int], float],
    ) -> None:
        self._runtime = runtime
        self._store = runtime.store
        self._execution = runtime.execution
        self._cost_estimator = cost_estimator
        self._state = _InvocationState(lock=asyncio.Lock())

    async def begin(self, spec: PhysicalInvocationSpec) -> PhysicalInvocationAttempt:
        async with self._state.lock:
            if self._state.poisoned:
                raise InvocationOutcomeUnknownError("generation_invocation_scope_poisoned")
            invocation_digest = canonical_digest_v1(
                {
                    "adapter_contract_key": spec.adapter_contract_key,
                    "mode": spec.mode,
                    "model": spec.model,
                    "provider": spec.provider,
                    "request_digest": spec.request_digest,
                }
            )
            from bowhead_core.agents._lead_context import (
                get_current_subagent_id,
            )

            ticket = await self._runtime.bus.accept_llm_invocation_ticket(
                actor_scope=(ActorScope.PEER if get_current_subagent_id() else ActorScope.ROOT),
                invocation_digest=invocation_digest,
                mode=spec.mode,
            )
            attempt, _provider_token = await self._claim_effect(
                ticket=ticket,
                provider_contract_key=_LLM_PROVIDER_CONTRACT,
                semantic_fact={
                    "adapter_contract_key": spec.adapter_contract_key,
                    "model": spec.model,
                    "provider": spec.provider,
                    "request_digest": spec.request_digest,
                },
                non_retriable=True,
                scanner_prefix="llm_claim",
            )
            return TerminalLLMInvocationAttempt(
                hook=self,
                spec=spec,
                attempt=attempt,
            )

    async def begin_tool(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        effect_spec: ToolEffectSpec,
        ticket: EventTicket,
    ) -> TerminalToolInvocationAttempt:
        if ticket.scope != self._execution.scope or ticket.event_type != "step_started":
            raise TerminalDurabilityAbort("durable tool ticket identity is invalid")
        if effect_spec.effect_class is not EffectClass.SIDE_EFFECT_POSSIBLE:
            raise ValueError("durable tool attempt requires a side-effect contract")
        contract_key = effect_spec.effect_contract_key
        if not isinstance(contract_key, str):
            raise ValueError("durable tool provider contract is missing")
        if contract_key != "tool_external/v1":
            raise TerminalDurabilityAbort(
                "durable tool contract is not admitted by the generic dispatcher",
            )
        contract = get_effect_provider_contract(contract_key)
        if contract.usage_evidence.value != "unsupported":
            raise TerminalDurabilityAbort("durable tool contract requires a dedicated settlement seam")
        arguments_digest = canonical_digest_v1(dict(arguments))
        async with self._state.lock:
            if self._state.poisoned:
                raise InvocationOutcomeUnknownError("generation_invocation_scope_poisoned")
            attempt, provider_token = await self._claim_effect(
                ticket=ticket,
                provider_contract_key=contract_key,
                semantic_fact={
                    "arguments_digest": arguments_digest,
                    "tool_name": tool_name,
                },
                non_retriable=(effect_spec.idempotency is ToolEffectIdempotency.NON_RETRIABLE),
                scanner_prefix="tool_claim",
            )
        return TerminalToolInvocationAttempt(
            hook=self,
            contract_key=contract_key,
            provider_token=provider_token,
            attempt=attempt,
        )

    async def _claim_effect(
        self,
        *,
        ticket: EventTicket,
        provider_contract_key: str,
        semantic_fact: Mapping[str, object],
        non_retriable: bool,
        scanner_prefix: str,
    ) -> tuple[EffectAttemptContext, str]:
        ticket_identity = {
            "accept_ordinal": ticket.accept_ordinal,
            "event_id": ticket.event_id,
            "event_seq": ticket.event_seq,
            "proposal_id": ticket.proposal_id,
        }
        slot = canonical_digest_v1(
            {
                "generation": self._execution.scope.generation,
                "identity": ticket_identity,
                "kind": "effect-slot/v1",
                "turn_id": self._execution.scope.turn_id,
            }
        )
        fact = canonical_digest_v1(
            {
                "actor_scope": ticket.actor_scope.value,
                "kind": "effect-fact/v1",
                "provider_contract_key": provider_contract_key,
                "semantic_fact": dict(semantic_fact),
                "slot": slot,
            }
        )
        action_ref = f"invocation:{slot}"
        provider_token = canonical_digest_v1(
            {
                "kind": "effect-provider/v1",
                "slot": slot,
                "team_id": self._execution.scope.team_id,
            }
        )
        try:
            intent = await self._store.begin_effect_attempt(
                self._execution,
                attempt_id=f"tea_fx_{fact[:32]}",
                effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
                provider=provider_contract_key,
                action_ref=action_ref,
                idempotency_or_fence_token=provider_token,
                non_retriable=non_retriable,
            )
            claim = await self._store.claim_effect_dispatch(
                self._execution,
                intent,
            )
        except BaseException as exc:
            try:
                current = await self._store.read_effect_attempt(
                    self._execution.scope,
                    action_ref=action_ref,
                )
            except BaseException:
                self._state.poisoned = True
                raise InvocationOutcomeUnknownError(
                    "physical_dispatch_claim_unconfirmed",
                ) from exc
            if current is None:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise TerminalDurabilityAbort(
                    "durable effect claim rejected before provider dispatch",
                ) from exc
            if current is not None and current.state is not EffectState.INTENT:
                self._state.poisoned = True
                if current.state is EffectState.DISPATCHED:
                    with suppress(BaseException):
                        await self._store.mark_effect_unknown_and_discover_recovery(
                            self._execution,
                            current,
                            scanner_id=f"{scanner_prefix}:{current.attempt_id}",
                        )
                raise InvocationOutcomeUnknownError(
                    "physical_dispatch_claim_unconfirmed",
                ) from exc
            if current is not None:
                try:
                    discarded = await self._store.discard_effect_intent(
                        self._execution,
                        current,
                    )
                except BaseException:
                    self._state.poisoned = True
                    raise InvocationOutcomeUnknownError(
                        "physical_dispatch_claim_unconfirmed",
                    ) from exc
                if not discarded:
                    self._state.poisoned = True
                    raise InvocationOutcomeUnknownError(
                        "physical_dispatch_claim_unconfirmed",
                    ) from exc
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise TerminalDurabilityAbort(
                "durable effect claim rejected before provider dispatch",
            ) from exc
        if not claim.dispatched_here:
            self._state.poisoned = True
            if claim.attempt.state is EffectState.DISPATCHED:
                with suppress(BaseException):
                    await self._store.mark_effect_unknown_and_discover_recovery(
                        self._execution,
                        claim.attempt,
                        scanner_id=f"{scanner_prefix}:{claim.attempt.attempt_id}",
                    )
            raise InvocationOutcomeUnknownError("physical_dispatch_claim_replayed")
        return claim.attempt, provider_token

    @asynccontextmanager
    async def _settlement_guard(self) -> AsyncIterator[None]:
        async with self._state.lock:
            try:
                yield
            except BaseException:
                # Release only after the generation-local one-way poison is
                # visible. A queued sibling must never claim between an
                # uncertain settlement and the outer UNKNOWN handoff.
                self._state.poisoned = True
                raise

    async def _record_completion_with_replay(
        self,
        *,
        attempt: EffectAttemptContext,
        context: EffectSettlementContext,
        commit: Callable[[EffectSettlementContext], Awaitable[Any]],
    ) -> Any:
        while True:
            try:
                return await commit(context)
            except TerminalProtocolMalformedError as exc:
                if str(exc) == "cancelled effect settlement waits for terminal receipt":
                    await self._wait_for_cancelled_receipt()
                    continue
                if str(exc) == "effect settlement attempt fence changed":
                    persisted = await self._store.read_effect_attempt(
                        attempt.scope,
                        action_ref=attempt.action_ref,
                    )
                    if persisted is None or persisted.attempt_id != attempt.attempt_id:
                        raise
                    if persisted.state is EffectState.UNKNOWN:
                        attempt = persisted
                        context = replace(
                            context,
                            expected_attempt_row_version=persisted.row_version,
                        )
                        continue
                    if persisted.state is EffectState.SETTLED:
                        context = replace(
                            context,
                            expected_attempt_row_version=persisted.row_version - 1,
                        )
                        continue
                raise
            except Exception:
                # A commit may have won even when its acknowledgement was lost.
                # Only an exact persisted settlement permits replay.
                persisted = await self._store.read_effect_attempt(
                    attempt.scope,
                    action_ref=attempt.action_ref,
                )
                if persisted is None or persisted.attempt_id != attempt.attempt_id or persisted.state is not EffectState.SETTLED:
                    raise
                context = replace(
                    context,
                    expected_attempt_row_version=persisted.row_version - 1,
                )
                return await commit(context)

    async def _wait_for_cancelled_receipt(self) -> None:
        """Observe a Stop receipt through shared storage, not a process-local Event."""

        scope = self._execution.scope
        read_context = TerminalReadContext(
            team_id=scope.team_id,
            session_id=scope.session_id,
            turn_id=scope.turn_id,
            viewer=self._runtime.identity.owner_instance_id,
            auth_fence_vector_digest=self._runtime.identity.authz_snapshot_digest,
        )

        async def _observe() -> None:
            while True:
                summary = await self._store.read_protocol_summary(read_context)
                receipt = summary.current_receipt
                if summary.current_generation == scope.generation and receipt is not None and receipt.generation == scope.generation:
                    return
                try:
                    await asyncio.wait_for(
                        self._runtime.bus.wait_for_receipt(),
                        timeout=_RECEIPT_PROBE_INTERVAL_SECONDS,
                    )
                    return
                except TimeoutError:
                    continue

        try:
            async with asyncio.timeout(_RECEIPT_WAIT_TIMEOUT_SECONDS):
                await _observe()
        except TimeoutError as exc:
            self._state.poisoned = True
            raise InvocationOutcomeUnknownError(
                "terminal_receipt_not_observed",
            ) from exc

    async def _handoff_settled_completion_ack_loss(
        self,
        *,
        scanner_prefix: str,
    ) -> None:
        """Fence a committed response whose continuation acknowledgement was lost."""

        handed_off = await self._store.handoff_execution_to_recovery(
            self._execution,
            reason_code=f"{scanner_prefix}_completion_ack_unknown",
        )
        if handed_off:
            discovery = await self._store.read_recovery_discovery_for_scope(
                self._execution.scope,
                scanner_id=(f"{scanner_prefix}_completion_ack:{self._execution.scope.turn_id}:{self._execution.scope.generation}"),
            )
            await self._store.discover_recovery(discovery)
            return
        scope = self._execution.scope
        summary = await self._store.read_protocol_summary(
            TerminalReadContext(
                team_id=scope.team_id,
                session_id=scope.session_id,
                turn_id=scope.turn_id,
                viewer=self._runtime.identity.owner_instance_id,
                auth_fence_vector_digest=(self._runtime.identity.authz_snapshot_digest),
            )
        )
        if summary.current_generation == scope.generation and (
            summary.current_generation_state is not None and summary.current_generation_state.value in {"closing", "recovery_pending", "closed"}
        ):
            return
        raise InvocationOutcomeUnknownError(
            "settled_completion_recovery_handoff_failed",
        )

    async def settle(
        self,
        *,
        spec: PhysicalInvocationSpec,
        attempt: EffectAttemptContext,
        outcome: EffectSettlementOutcome,
        settlement_code: str,
        usage: TokenUsage,
        response_digest: str,
    ) -> bool:
        async with self._settlement_guard():
            current = await self._store.read_effect_attempt(
                attempt.scope,
                action_ref=attempt.action_ref,
            )
            if current is not None and current.attempt_id == attempt.attempt_id and current.state is EffectState.UNKNOWN:
                attempt = current
            usage_fact = {
                "cached_tokens": usage.cached_tokens,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }
            evidence_digest = canonical_digest_v1(
                {
                    "adapter_contract_key": spec.adapter_contract_key,
                    "outcome": outcome.value,
                    "request_digest": spec.request_digest,
                    "response_digest": response_digest,
                    "usage": usage_fact,
                }
            )
            auth_digest = effect_control_identity_digest(
                attempt_id=attempt.attempt_id,
                provider_contract_key=_LLM_PROVIDER_CONTRACT,
                source=EffectSettlementSource.PROVIDER.value,
                outcome=outcome.value,
                settlement_code=settlement_code,
                evidence_digest=evidence_digest,
                object_version_digest=response_digest,
                actor_audit_ref=_LLM_ADAPTER_PRINCIPAL,
                audit_object_ref=attempt.action_ref,
                provider_external_ref_digest=None,
                provider_external_ref_key_version=None,
            )
            suffix = auth_digest[:32]
            context = EffectSettlementContext(
                scope=attempt.scope,
                attempt_id=attempt.attempt_id,
                expected_attempt_row_version=attempt.row_version,
                settlement_id=f"set_{suffix}",
                source=EffectSettlementSource.PROVIDER,
                outcome=outcome,
                settlement_code=settlement_code,
                principal_ref=_LLM_ADAPTER_PRINCIPAL,
                principal_role="provider_adapter",
                authz_snapshot_digest=auth_digest,
                evidence_link_id=f"evidence_{suffix}",
                evidence_digest=evidence_digest,
                object_version_digest=response_digest,
                purge_after=None,
                audit_object_ref=attempt.action_ref,
            )
            cost = self._cost_estimator(usage.input_tokens, usage.output_tokens) if outcome is EffectSettlementOutcome.CONFIRMED_APPLIED else 0.0

            async def _commit(candidate: EffectSettlementContext) -> Any:
                return await self._store.record_llm_effect_settlement_with_usage(
                    self._execution,
                    candidate,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_amount=cost,
                )

            result = await self._record_completion_with_replay(
                attempt=attempt,
                context=context,
                commit=_commit,
            )
            if not result.continue_execution:
                self._state.poisoned = True
            return result.continue_execution

    async def settle_tool(
        self,
        *,
        contract_key: str,
        attempt: EffectAttemptContext,
        evidence: EffectProbeEvidence,
    ) -> bool:
        async with self._settlement_guard():
            contract = get_effect_provider_contract(contract_key)
            if not isinstance(evidence, EffectProbeEvidence) or not evidence.settlement_code.startswith(
                f"{contract.outcome_classifier_key}:",
            ):
                raise TerminalDurabilityAbort("tool result lacks trusted classifier evidence")
            principal = f"adapter:{contract.outcome_classifier_key}"
            auth_digest = effect_control_identity_digest(
                attempt_id=attempt.attempt_id,
                provider_contract_key=contract_key,
                source=EffectSettlementSource.PROVIDER.value,
                outcome=evidence.outcome.value,
                settlement_code=evidence.settlement_code,
                evidence_digest=evidence.evidence_digest,
                object_version_digest=evidence.object_version_digest,
                actor_audit_ref=principal,
                audit_object_ref=evidence.audit_object_ref,
                provider_external_ref_digest=evidence.provider_external_ref_digest,
                provider_external_ref_key_version=evidence.provider_external_ref_key_version,
            )
            suffix = auth_digest[:32]
            context = EffectSettlementContext(
                scope=attempt.scope,
                attempt_id=attempt.attempt_id,
                expected_attempt_row_version=attempt.row_version,
                settlement_id=f"set_{suffix}",
                source=EffectSettlementSource.PROVIDER,
                outcome=evidence.outcome,
                settlement_code=evidence.settlement_code,
                principal_ref=principal,
                principal_role="provider_adapter",
                authz_snapshot_digest=auth_digest,
                evidence_link_id=f"evidence_{suffix}",
                evidence_digest=evidence.evidence_digest,
                object_version_digest=evidence.object_version_digest,
                purge_after=None,
                audit_object_ref=evidence.audit_object_ref,
                provider_external_ref_digest=evidence.provider_external_ref_digest,
                provider_external_ref_key_version=evidence.provider_external_ref_key_version,
            )

            async def _commit(candidate: EffectSettlementContext) -> Any:
                return await self._store.record_tool_effect_settlement(
                    self._execution,
                    candidate,
                )

            completion = await self._record_completion_with_replay(
                attempt=attempt,
                context=context,
                commit=_commit,
            )
            if not completion.continue_execution:
                self._state.poisoned = True
            return completion.continue_execution

    async def publish_failure(self, failure: LLMTargetFailure) -> None:
        """Persist the observed failure before UNKNOWN fences further event writes."""
        async with self._state.lock:
            if self._state.poisoned:
                return
            self._state.poisoned = True
            error_type = (
                StreamingInterruptedError
                if failure.first_visible_chunk_emitted
                else StreamingTargetsExhaustedError
                if failure.mode == "stream"
                else InvocationTargetsExhaustedError
            )
            projection = project_llm_failure(error_type([failure]))
            assert projection is not None
            published = build_chat_llm_failure(
                projection=replace(projection, retryable=False, outcome_unknown=True),
                turn_id=self._execution.scope.turn_id,
            )
            await self._runtime.bus.emit(
                OrchestratorEvent(
                    event_type="turn_failed",
                    turn_id=self._execution.scope.turn_id,
                    payload={
                        "reason_code": published.reason_code,
                        "diagnostics": published.diagnostics,
                        "occurrence_id": published.occurrence_id,
                        "recovery_action": "copy_diagnostics",
                    },
                )
            )

    async def mark_unknown(
        self,
        *,
        attempt: EffectAttemptContext,
        reason_code: str,
        scanner_prefix: str,
    ) -> None:
        del reason_code
        async with self._state.lock:
            self._state.poisoned = True
            try:
                await self._store.mark_effect_unknown_and_discover_recovery(
                    self._execution,
                    attempt,
                    scanner_id=f"{scanner_prefix}:{attempt.attempt_id}",
                )
            except BaseException:
                current = await self._store.read_effect_attempt(
                    attempt.scope,
                    action_ref=attempt.action_ref,
                )
                if current is not None and current.state is EffectState.SETTLED:
                    await self._handoff_settled_completion_ack_loss(
                        scanner_prefix=scanner_prefix,
                    )
                    return
                if current is None or current.state is not EffectState.UNKNOWN:
                    raise


@contextmanager
def tool_invocation_ticket_scope(ticket: EventTicket | None) -> Iterator[None]:
    """Bind the already-persisted step_started identity to one handler call."""

    if ticket is not None and not isinstance(ticket, EventTicket):
        raise TypeError("tool invocation ticket is invalid")
    token = _CURRENT_TOOL_TICKET.set(ticket)
    try:
        yield
    finally:
        _CURRENT_TOOL_TICKET.reset(token)


async def emit_tool_step_started_ticket(
    bus: Any,
    event: OrchestratorEvent,
) -> EventTicket | None:
    """Return a persisted v2 ticket; preserve legacy best-effort emit semantics."""

    emit_durable = getattr(bus, "emit_tool_step_started", None)
    if callable(emit_durable):
        ticket = await emit_durable(event)
        if not isinstance(ticket, EventTicket):
            raise TerminalDurabilityAbort(
                "terminal durable tool marker did not return a persisted ticket",
            )
        return ticket
    await bus.emit(event)
    return None


async def begin_durable_tool_effect(
    *,
    tool_name: str,
    arguments: Mapping[str, object],
    effect_spec: ToolEffectSpec | None,
    approval_bound: bool,
) -> TerminalToolInvocationAttempt | None:
    """Validate a trusted ToolSpec and claim before any mutation handler runs."""

    hook = _CURRENT_DURABLE_HOOK.get()
    if hook is None:
        return None
    ticket = _CURRENT_TOOL_TICKET.get()
    if ticket is None:
        raise TerminalDurabilityAbort("durable tool dispatch is missing a persisted step ticket")
    if approval_bound:
        raise TerminalDurabilityAbort("approval-bound tool cannot use the generic durable dispatcher")
    if effect_spec is None:
        raise TerminalDurabilityAbort("tool has no trusted durable effect contract")
    if effect_spec.effect_class in {EffectClass.NONE, EffectClass.READ_ONLY}:
        return None
    return await hook.begin_tool(
        tool_name=tool_name,
        arguments=arguments,
        effect_spec=effect_spec,
        ticket=ticket,
    )


async def complete_durable_tool_effect(
    attempt: TerminalToolInvocationAttempt | None,
    *,
    result: object,
) -> object:
    display_result = result.result if isinstance(result, ClassifiedToolEffectResult) else result
    if attempt is None:
        return display_result
    if not isinstance(result, ClassifiedToolEffectResult):
        await fail_physical_invocation(
            attempt,
            reason_code="tool_result_outcome_unclassified",
        )
    if not await attempt.complete(result.evidence):
        raise InvocationOutcomeUnknownError("durable_invocation_fenced")
    return display_result


def durable_tool_provider_token(
    attempt: TerminalToolInvocationAttempt | None,
    effect_spec: ToolEffectSpec | None,
) -> str | None:
    if attempt is None or effect_spec is None or effect_spec.idempotency is not ToolEffectIdempotency.STABLE_TOKEN:
        return None
    return attempt.provider_token


def reject_unclassified_direct_mutation(ingress: str) -> None:
    """Keep audited composite/direct writers dormant until an atomic wrapper exists."""

    if _CURRENT_DURABLE_HOOK.get() is not None:
        raise TerminalDurabilityAbort(
            f"durable direct mutation is not eligible: {ingress}",
        )


@contextmanager
def detached_auxiliary_scope(*, reason: str) -> Iterator[None]:
    """Run non-root best-effort work without inherited effect or billing authority."""

    hook_token = _CURRENT_DURABLE_HOOK.set(None)
    ticket_token = _CURRENT_TOOL_TICKET.set(None)
    try:
        with (
            physical_invocation_scope(None),
            detached_usage_context(
                reason=reason,
            ),
        ):
            yield
    finally:
        _CURRENT_TOOL_TICKET.reset(ticket_token)
        _CURRENT_DURABLE_HOOK.reset(hook_token)


def create_detached_auxiliary_task[T](
    coroutine: Coroutine[Any, Any, T],
    *,
    reason: str,
    name: str | None = None,
) -> asyncio.Task[T]:
    """Create one auxiliary task after stripping root durable ContextVars."""

    with detached_auxiliary_scope(reason=reason):
        return asyncio.create_task(coroutine, name=name)


@asynccontextmanager
async def durable_llm_invocation_scope(
    runtime: Any | None,
    *,
    cost_estimator: Callable[[int, int], float],
) -> AsyncIterator[None]:
    """Install the hook only for a persisted contract-v2 generation."""

    if runtime is None:
        yield
        return
    contract = runtime.effect_contract
    if contract.disposition is EffectIntentDisposition.LEGACY:
        yield
        return
    hook = TerminalLLMInvocationHook(
        runtime,
        cost_estimator=cost_estimator,
    )
    token = _CURRENT_DURABLE_HOOK.set(hook)
    try:
        with physical_invocation_scope(hook, failure_observer=hook.publish_failure):
            yield
    finally:
        _CURRENT_DURABLE_HOOK.reset(token)
