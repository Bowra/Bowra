"""Terminal Protocol v1 production generation runtime.

The runtime is deliberately request scoped.  It adapts the existing
``TurnEventBus.emit`` producer contract to the durable v1 writer while keeping
the compatibility live subscription used by the Web gateway.  Persisted rows,
not the compatibility ring, remain the source of replay truth.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from bowhead_core.events import (
    _CANVAS_TURN_OPENERS,
    OrchestratorEvent,
    TerminalDurabilityAbort,
    TurnEventBus,
)
from bowhead_core.llm_failure import LLM_REASON_CODES
from bowhead_core.published_diagnostics import republish_chat_llm_diagnostics
from bowhead_core.terminal_effect_contract import (
    EffectExecutionContract,
    EffectIntentDisposition,
)
from bowhead_core.terminal_event_writer import TurnEventWriter
from bowhead_core.terminal_finalizer import (
    FinalizationBuildSnapshot,
    FinalizeRequest,
    TerminalFinalizer,
)
from bowhead_core.terminal_generation_bus import GenerationEventBus, ProducerLease
from bowhead_core.terminal_protocol import (
    ActorScope,
    CandidateOrigin,
    CandidateOutcome,
    CommittedReceipt,
    EventPublishMode,
    EventTicket,
    ExecutionGenerationContext,
    FinalAnswerMode,
    PhaseScope,
    RootDurability,
    SessionVariant,
    StagedTerminalEvent,
    TerminalCandidatePlan,
    TerminalCandidateVariant,
    TerminalFollowupCapability,
    TerminalProtocolError,
    TerminalProtocolMalformedError,
    TerminalScope,
    TerminalStatus,
    canonical_digest_v1,
    canonical_json_v1,
)
from bowhead_core.terminal_protocol_store import (
    TerminalProtocolEventRow,
    TerminalProtocolStore,
    TerminalReadContext,
)

_TRANSPORT_EVENT_TYPES = frozenset({"keepalive", "streaming.state"})
_STATUS_ALIASES = {
    "awaiting_approval": TerminalStatus.APPROVAL_PENDING,
    "approval_pending": TerminalStatus.APPROVAL_PENDING,
    "user_cancelled": TerminalStatus.CANCELLED,
}
_MESSAGE_KEYS = {
    TerminalStatus.COMPLETED: "turn.completed",
    TerminalStatus.FAILED: "turn.failed",
    TerminalStatus.CANCELLED: "turn.cancelled",
    TerminalStatus.ABORTED: "turn.aborted",
    TerminalStatus.CLARIFICATION_PENDING: "turn.clarification_pending",
    TerminalStatus.APPROVAL_PENDING: "turn.approval_pending",
}
_FROZEN_TERMINAL_FAILURE_REASONS = frozenset(
    {
        "worker_start_failed",
        "terminal_producer_timeout",
        "terminal_projection_missing",
        "terminal_projection_not_committed",
        "terminal_commit_outcome_unknown",
        "claim_owner_lost",
        "effect_outcome_unknown",
        "terminal_dependency_missing",
        "terminal_protocol_malformed",
        "terminal_protocol_future_version",
    }
)
# Compatibility projection from the existing finite Lead contract to the
# frozen Terminal v1 registry.  Never persist the free-form ``error`` string as
# a receipt reason: replay must remain stable when Lead wording evolves.
_CHAT_FAILURE_REASON_COMPAT = {
    "wallclock_exceeded": "terminal_producer_timeout",
    "hop_cap_exceeded": "terminal_producer_timeout",
    "peer_depth_exceeded": "terminal_producer_timeout",
    "cost_cap_exceeded": "terminal_producer_timeout",
    "token_cap_exhausted": "terminal_producer_timeout",
    "peer_dispatch_timeout": "terminal_producer_timeout",
    "canvas_section_not_found": "terminal_projection_missing",
    "canvas_payload_invalid": "terminal_projection_missing",
    "no_safe_fields_changed": "terminal_projection_missing",
    "single_write_leaf_violation": "terminal_projection_missing",
    # Native document admission rejected the request and Lead ended without a valid correction:
    # no deliverable was projected, same category as canvas_payload_invalid.
    "document_request_rejected": "terminal_projection_missing",
    "document_routing_invalid": "terminal_projection_missing",
    "document_routing_uncertain": "terminal_projection_missing",
    "document_routing_source_unavailable": "terminal_dependency_missing",
    "lead_self_no_text": "terminal_projection_missing",
    # The model still produced no answer or action after the one empty-response recovery.
    "lead_empty_answer": "terminal_projection_missing",
    "lead_output_exhausted": "terminal_projection_missing",
    # The selected @Agent primary never completed: no deliverable was produced.
    "reference_primary_incomplete": "terminal_projection_missing",
    "peer_dispatch_network_error": "worker_start_failed",
    "lead_internal_error": "worker_start_failed",
    "internal_error": "worker_start_failed",
    # Service update/restart loses dispatch owner process, same as Terminal v1 owner loss.
    "service_interrupted": "claim_owner_lost",
    "entitlement_denied": "terminal_dependency_missing",
    "session_deleted": "terminal_dependency_missing",
    "access_context_missing": "terminal_dependency_missing",
    "plan_runtime_unavailable": "terminal_dependency_missing",
    "writer_unavailable": "terminal_dependency_missing",
    "cancelled": "terminal_dependency_missing",
    "user_cancel": "terminal_dependency_missing",
    "clarification_abort": "terminal_dependency_missing",
    # Shortcut refs: selected Agent/Skill grant changed before resume, or required checkpoint/lease unavailable;
    # all nonretryable missing dependencies (access / configuration diagnostics), same category as entitlement_denied.
    "reference_policy_changed": "terminal_dependency_missing",
    "reference_continuation_unavailable": "terminal_dependency_missing",
    # Clarification resume checkpoint/lease no longer usable: nonretryable missing dependency,
    # same category as reference_continuation_unavailable.
    "clarification_resume_unavailable": "terminal_dependency_missing",
}


def _stable_id(prefix: str, scope: TerminalScope, finalization_epoch: int, kind: str) -> str:
    material = f"terminal-v1\0{scope.team_id}\0{scope.session_id}\0{scope.turn_id}\0{scope.generation}\0{finalization_epoch}\0{kind}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _status_from_done(payload: dict[str, Any]) -> TerminalStatus:
    raw = str(payload.get("status") or "failed").strip().lower()
    alias = _STATUS_ALIASES.get(raw)
    if alias is not None:
        return alias
    try:
        return TerminalStatus(raw)
    except ValueError:
        return TerminalStatus.FAILED


def _actor_scope(event: OrchestratorEvent) -> ActorScope:
    return ActorScope.PEER if event.parent_agent_id else ActorScope.ROOT


def _compat_failure_reason(payload: dict[str, Any]) -> str:
    for key in ("reason_code", "reason", "error", "error_kind"):
        candidate = payload.get(key)
        if not isinstance(candidate, str) or not candidate:
            continue
        normalized = candidate.strip().partition(":")[0].partition("=")[0]
        if normalized in _FROZEN_TERMINAL_FAILURE_REASONS:
            return normalized
        mapped = _CHAT_FAILURE_REASON_COMPAT.get(normalized)
        if mapped is not None:
            return mapped
    return "terminal_protocol_malformed"


def _safe_failure_payload(payload: dict[str, Any]) -> dict[str, object]:
    reason = _compat_failure_reason(payload)
    correlation = payload.get("occurrence_id") or payload.get("correlation_id")
    result: dict[str, object] = {
        "message_key": "turn.failed",
        "reason_code": reason[:128],
    }
    if isinstance(correlation, str) and correlation:
        result["correlation_id"] = correlation[:128]
    public_reason = payload.get("reason_code") or payload.get("reason")
    if isinstance(public_reason, str) and public_reason in LLM_REASON_CODES:
        diagnostics = republish_chat_llm_diagnostics(payload.get("diagnostics"), reason_code=public_reason)
        if diagnostics is not None:
            result["reason_code"] = public_reason
            result["diagnostics"] = diagnostics
            if payload.get("recovery_action") == "copy_diagnostics":
                result["recovery_action"] = "copy_diagnostics"
    return result


@dataclass(frozen=True, slots=True)
class TerminalRuntimeIdentity:
    """Admission-time values that must remain stable through finalization."""

    authz_snapshot_digest: str
    writer_admission_epoch: int
    owner_instance_id: str
    rollout_config_generation: str = "terminal-v1"

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.authz_snapshot_digest,
                self.owner_instance_id,
                self.rollout_config_generation,
            )
        ):
            raise ValueError("terminal runtime identity is invalid")
        if not isinstance(self.writer_admission_epoch, int) or isinstance(self.writer_admission_epoch, bool) or self.writer_admission_epoch < 1:
            raise ValueError("terminal writer admission epoch must be positive")


@dataclass(slots=True)
class TerminalTurnRuntime:
    """Immutable ownership plus the mutable, generation-local bus adapter."""

    store: TerminalProtocolStore
    execution: ExecutionGenerationContext
    generation_bus: GenerationEventBus
    identity: TerminalRuntimeIdentity
    effect_contract: EffectExecutionContract
    followup_capability: TerminalFollowupCapability | None = None
    followup_execution_intent: dict[str, object] = field(default_factory=dict)
    followup_committed_callback: (
        Callable[
            [TerminalTurnRuntime, CommittedReceipt],
            Awaitable[None] | None,
        ]
        | None
    ) = field(default=None, repr=False, compare=False)
    retire_callback: Callable[[TerminalTurnRuntime], Awaitable[None] | None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    worker_spawn_callback: (
        Callable[
            [Callable[[], asyncio.Task[object]]],
            Awaitable[tuple[asyncio.Task[object], bool]],
        ]
        | None
    ) = field(default=None, repr=False, compare=False)
    handoff_callback: (
        Callable[
            [TerminalTurnRuntime, str, BaseException | None],
            Awaitable[None] | None,
        ]
        | None
    ) = field(default=None, repr=False, compare=False)
    bus: TerminalProtocolBusAdapter = field(init=False)
    _heartbeat_task: asyncio.Task[None] | None = field(
        init=False,
        default=None,
        repr=False,
        compare=False,
    )
    _execution_started: bool = field(init=False, default=False, repr=False, compare=False)
    _worker_start_abort_started: bool = field(
        init=False,
        default=False,
        repr=False,
        compare=False,
    )
    _retired: bool = field(init=False, default=False, repr=False, compare=False)
    _execution_start_lock: asyncio.Lock = field(
        init=False,
        default_factory=asyncio.Lock,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        writer = TurnEventWriter(self.store, self.generation_bus, self.execution)
        self.bus = TerminalProtocolBusAdapter(
            store=self.store,
            execution=self.execution,
            generation_bus=self.generation_bus,
            writer=writer,
            finalizer=TerminalFinalizer(self.store),
            identity=self.identity,
            followup_capability=self.followup_capability,
            followup_execution_intent=self.followup_execution_intent,
            followup_committed_callback=(
                lambda receipt: self.followup_committed_callback(self, receipt) if self.followup_committed_callback is not None else None
            ),
        )

    @property
    def scope(self) -> TerminalScope:
        return self.execution.scope

    @property
    def retired(self) -> bool:
        return self._retired

    async def spawn_worker_once(
        self,
        spawn: Callable[[], asyncio.Task[object]],
    ) -> tuple[asyncio.Task[object], bool]:
        if self._retired:
            raise RuntimeError("terminal runtime is retired")
        if self.worker_spawn_callback is None:
            return spawn(), True
        return await self.worker_spawn_callback(spawn)

    async def start(self) -> None:
        """Start the heartbeat only after ``start_execution`` linearizes."""

        if not self._execution_started:
            raise RuntimeError("terminal execution must be durably started first")

        if self._heartbeat_task is not None:
            return
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"terminal-heartbeat:{self.scope.turn_id}:{self.scope.generation}",
        )

    async def start_execution(self) -> None:
        """Perform the worker coroutine's first durable action, then heartbeat."""

        async with self._execution_start_lock:
            if self._execution_started:
                return
            self.execution = await self.bus.start_execution()
            self._execution_started = True
            await self.start()

    async def abort_worker_start_failed(self) -> None:
        """Seal a claimed generation when task creation failed synchronously."""

        if self._worker_start_abort_started:
            return
        self._worker_start_abort_started = True
        try:
            await self.store.handoff_execution_to_recovery(
                self.execution,
                reason_code="worker_start_failed",
            )
            from bowhead_core.terminal_reconciler import converge_worker_start_failure

            receipt = await converge_worker_start_failure(
                self.store,
                scope=self.scope,
                owner_instance_id=self.identity.owner_instance_id,
                writer_admission_epoch=self.identity.writer_admission_epoch,
            )
            await self.bus.publish_recovered_receipt(receipt)
        except BaseException as exc:
            await self._notify_handoff(
                reason_code="worker_start_failed",
                error=exc,
            )
            raise
        finally:
            await self.retire()

    async def handoff_to_recovery(self, *, reason_code: str) -> None:
        """Stop local ownership and expose a durable, immediately overdue lease."""

        try:
            await self.store.handoff_execution_to_recovery(
                self.execution,
                reason_code=reason_code,
            )
        except BaseException as exc:
            await self.retire()
            await self._notify_handoff(
                reason_code=reason_code,
                error=exc,
            )
            raise
        await self.retire()
        await self._notify_handoff(
            reason_code=reason_code,
            error=None,
        )

    async def _notify_handoff(
        self,
        *,
        reason_code: str,
        error: BaseException | None,
    ) -> None:
        callback = self.handoff_callback
        if callback is None:
            return
        try:
            result = callback(self, reason_code, error)
            if inspect.isawaitable(result):
                await result
        except BaseException as callback_error:
            if error is not None:
                error.add_note(f"terminal runtime handoff supervisor failed: {callback_error!r}")

    async def heartbeat_once(self) -> None:
        """Renew immediately; public for readiness probes and fake-clock tests."""

        self.execution = await self.store.heartbeat_execution(self.execution)

    async def _heartbeat_loop(self) -> None:
        try:
            while not self.bus.closed:
                await asyncio.sleep(10)
                if self.bus.closed:
                    return
                await self.heartbeat_once()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if not self.bus.closed:
                self.bus.latch_fatal_error(exc)

    async def close(self) -> None:
        heartbeat = self._heartbeat_task
        self._heartbeat_task = None
        if heartbeat is not None and heartbeat is not asyncio.current_task():
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        await self.bus.close()

    async def retire(self) -> None:
        if self._retired:
            return
        self._retired = True
        close_error: BaseException | None = None
        try:
            await self.close()
        except BaseException as exc:
            close_error = exc
        callback_error: BaseException | None = None
        try:
            if self.retire_callback is not None:
                result = self.retire_callback(self)
                if inspect.isawaitable(result):
                    await result
        except BaseException as exc:
            callback_error = exc
        if close_error is not None:
            if callback_error is not None:
                close_error.add_note(f"terminal runtime retire callback also failed: {callback_error!r}")
            raise close_error
        if callback_error is not None:
            raise callback_error


class TerminalProtocolBusAdapter:
    """Persist-before-live adapter for one v1 execution generation."""

    terminal_protocol_version = 1

    def __init__(
        self,
        *,
        store: TerminalProtocolStore,
        execution: ExecutionGenerationContext,
        generation_bus: GenerationEventBus,
        writer: TurnEventWriter,
        finalizer: TerminalFinalizer,
        identity: TerminalRuntimeIdentity,
        followup_capability: TerminalFollowupCapability | None,
        followup_execution_intent: dict[str, object],
        followup_committed_callback: Callable[
            [CommittedReceipt],
            Awaitable[None] | None,
        ],
    ) -> None:
        self._store = store
        self._execution = execution
        self._generation_bus = generation_bus
        self._writer = writer
        self._finalizer = finalizer
        self._identity = identity
        self._followup_capability = followup_capability
        self._followup_execution_intent = dict(followup_execution_intent)
        self._followup_committed_callback = followup_committed_callback
        self._live = TurnEventBus()
        self._emit_lock = asyncio.Lock()
        self._closed = False
        self._terminal_delivery_finalized = False
        self._receipt: CommittedReceipt | None = None
        self._receipt_ready = asyncio.Event()
        self._fatal_error: BaseException | None = None
        self._last_answer: EventTicket | None = None
        self._last_partial_text = ""
        self._compat_published_event_ids: set[str] = set()
        self._terminal_snapshot: dict[str, object] = {}
        self._producer_tasks: tuple[asyncio.Task[object], ...] = ()
        self._required_terminal_events: tuple[StagedTerminalEvent, ...] = ()
        # report-streaming P1: same TurnEventBus rule, independently implemented on **this adapter**;
        # this path persists before publish; relying only on self._live injection puts synthetic frame
        # in live ring but not turn_events: live != persist, F5 loses
        # currentTurnId. See emit().
        self._canvas_turn_streaming_sent = False

    @property
    def scope(self) -> TerminalScope:
        return self._execution.scope

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def receipt(self) -> CommittedReceipt | None:
        return self._receipt

    async def wait_for_receipt(self) -> CommittedReceipt:
        await self._receipt_ready.wait()
        assert self._receipt is not None
        return self._receipt

    async def publish_recovered_receipt(self, receipt: CommittedReceipt) -> None:
        """Publish a recovery-committed terminal batch to attached live readers."""

        if self._receipt is not None:
            if self._receipt != receipt:
                raise RuntimeError("terminal runtime receipt changed")
            if not self._terminal_delivery_finalized:
                await self._close_terminal_delivery_for_replay()
            return
        self._receipt = receipt
        self._receipt_ready.set()
        await self._deliver_committed_receipt(receipt)

    def latch_fatal_error(self, error: BaseException) -> None:
        """Fail future writes after an asynchronous ownership failure."""

        if self._fatal_error is None:
            self._fatal_error = error

    def raise_if_failed(self) -> None:
        """Fail-stop seam used by legacy best-effort event wrappers."""

        if self._fatal_error is not None:
            raise TerminalDurabilityAbort("terminal runtime has a prior durable write failure") from self._fatal_error

    async def start_execution(self) -> ExecutionGenerationContext:
        """Advance claimed -> started before the first event/model/tool action."""

        async with self._emit_lock:
            execution = await self._store.mark_execution_started(self._execution)
            self._execution = execution
            self._writer = TurnEventWriter(self._store, self._generation_bus, execution)
            accepted_through = await self._store.read_execution_accepted_highwater(
                execution,
            )
            await self._materialize_accepted_prefix_locked(accepted_through)
            return execution

    async def register_producer(self) -> ProducerLease:
        """Register a detached business-event producer before it is spawned."""

        return await self._generation_bus.register_producer(
            claim_epoch=self._execution.claim_epoch,
        )

    async def materialize_accepted_prefix(
        self,
        accepted_through: int,
    ) -> tuple[EventTicket, ...]:
        """Wake an already-started writer without accepting another proposal."""

        async with self._emit_lock:
            return await self._materialize_accepted_prefix_locked(
                accepted_through,
            )

    async def _materialize_accepted_prefix_locked(
        self,
        accepted_through: int,
    ) -> tuple[EventTicket, ...]:
        tickets = await self._writer.flush_through(accepted_through)
        await self._publish_compat_prefix(tickets)
        return tickets

    async def _publish_compat_prefix(
        self,
        tickets: tuple[EventTicket, ...],
    ) -> None:
        for ticket in tickets:
            await self._publish_compat(ticket)

    async def emit_idempotent(
        self,
        event: OrchestratorEvent,
        *,
        idempotency_key: str,
    ) -> EventTicket:
        """Persist one non-terminal root event with a caller-owned replay key."""

        if not isinstance(event, OrchestratorEvent) or event.turn_id != self.scope.turn_id:
            raise ValueError("idempotent terminal event scope is invalid")
        if event.event_type == "done" or not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotent terminal event identity is invalid")
        async with self._emit_lock:
            payload = dict(event.payload)
            if event.parent_agent_id is not None:
                payload["parent_agent_id"] = event.parent_agent_id
            handle = await self._writer.accept_event(
                event_type=event.event_type,
                actor_scope=_actor_scope(event),
                phase_scope=PhaseScope.IN_GENERATION,
                payload=payload,
                publish_mode=(EventPublishMode.BEFORE_LIVE if event.persist_before_delivery else EventPublishMode.NORMAL),
                root_durability=RootDurability.COMMITTED,
                idempotency_key=idempotency_key,
            )
            ticket, prefix = await handle.persisted_with_prefix()
            await self._publish_compat_prefix(prefix)
            return ticket

    async def accept_llm_invocation_ticket(
        self,
        *,
        actor_scope: ActorScope,
        invocation_digest: str,
        mode: str,
    ) -> EventTicket:
        """Persist one content-free physical-invocation marker before dispatch."""

        if (
            not isinstance(invocation_digest, str)
            or len(invocation_digest) != 64
            or any(char not in "0123456789abcdef" for char in invocation_digest)
            or mode not in {"invoke", "stream"}
            or not isinstance(actor_scope, ActorScope)
        ):
            raise ValueError("LLM invocation marker identity is invalid")
        async with self._emit_lock:
            if self._closed or self._fatal_error is not None:
                self.raise_if_failed()
                raise TerminalDurabilityAbort("terminal invocation marker admission is closed")
            handle = await self._writer.accept_event(
                event_type="turn.llm_invocation",
                actor_scope=actor_scope,
                phase_scope=PhaseScope.IN_GENERATION,
                payload={
                    "invocation_digest": invocation_digest,
                    "mode": mode,
                    "protocol": 2,
                },
                publish_mode=EventPublishMode.BEFORE_LIVE,
                root_durability=RootDurability.COMMITTED,
            )
            ticket, prefix = await handle.persisted_with_prefix()
            await self._publish_compat_prefix(prefix)
            return ticket

    async def emit_tool_step_started(self, event: OrchestratorEvent) -> EventTicket:
        """Persist and publish the existing tool step marker, returning its ticket."""

        if not isinstance(event, OrchestratorEvent) or event.turn_id != self.scope.turn_id or event.event_type != "step_started":
            raise ValueError("tool invocation ticket requires a scoped step_started event")
        async with self._emit_lock:
            if self._closed or self._fatal_error is not None:
                self.raise_if_failed()
                raise TerminalDurabilityAbort("terminal tool marker admission is closed")
            try:
                ticket, prefix = await self._persist_event(event)
                await self._publish_compat_prefix(prefix)
                return ticket
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                if isinstance(exc, TerminalDurabilityAbort):
                    raise
                self._fatal_error = exc
                raise TerminalDurabilityAbort("terminal durable tool marker write failed") from exc

    def configure_required_terminal_events(
        self,
        events: tuple[StagedTerminalEvent, ...],
    ) -> None:
        """Freeze domain-required events for the finalizer's atomic batch."""

        if self._closed or self._receipt is not None:
            raise RuntimeError("terminal required events cannot change after close")
        if any(
            not isinstance(event, StagedTerminalEvent) or event.actor_scope is not ActorScope.ROOT or event.event_type == "done" for event in events
        ):
            raise ValueError("terminal required event set is invalid")
        if len({event.event_id for event in events}) != len(events):
            raise ValueError("terminal required event identity is duplicated")
        if self._required_terminal_events and self._required_terminal_events != events:
            raise RuntimeError("terminal required event set changed")
        self._required_terminal_events = events

    async def emit(self, event: OrchestratorEvent) -> bool:
        if not isinstance(event, OrchestratorEvent):
            raise TypeError("terminal runtime only accepts OrchestratorEvent")
        if event.turn_id != self.scope.turn_id:
            raise ValueError("terminal runtime event turn identity mismatch")
        if event.event_type == "done":
            return await self._emit_done(event)
        # report-streaming P1: inject canvas.turn_streaming with exact TurnEventBus.emit rule
        # (sole writer of frontend currentTurnId; missing means D2 waiting strip never renders,
        # D3 banner phase decisions all idle).
        #
        # Duplicate here rather than reuse underlying bus: adapter first _persist_event,
        # then _publish_compat → self._live.emit. Underlying injection occurs after publish,
        # putting frame only in live ring, **not turn_events**; reconnect/F5 replay misses it,
        # live != persist violates chat invariant (test_runtime_live_and_durable_replay_share_stable_canvas_cursor
        # caught it). Injection here uses same persistence+publish path as other events.
        #
        # ⚠ Before **_emit_lock acquisition**: asyncio.Lock nonreentrant, recursive emit
        # acquires it itself. Depth always 1 (inner canvas.turn_streaming directly sets flag).
        if event.event_type == "canvas.turn_streaming":
            if self._canvas_turn_streaming_sent:
                return True
            self._canvas_turn_streaming_sent = True
        elif not self._canvas_turn_streaming_sent and event.event_type in _CANVAS_TURN_OPENERS:
            await self.emit(
                OrchestratorEvent(
                    event_type="canvas.turn_streaming",
                    turn_id=event.turn_id,
                    payload={"turn_id": event.turn_id},
                )
            )
        async with self._emit_lock:
            if self._fatal_error is not None:
                self.raise_if_failed()
            try:
                if self._closed:
                    return False
                if event.event_type in _TRANSPORT_EVENT_TYPES:
                    return await self._live.emit(event)
                if event.event_type in {"error", "turn_failed"} and _actor_scope(event) is ActorScope.ROOT:
                    event = OrchestratorEvent(
                        event_type="turn_failed",
                        turn_id=event.turn_id,
                        payload=_safe_failure_payload(event.payload),
                        occurred_at=event.occurred_at,
                        parent_agent_id=event.parent_agent_id,
                        persist_before_delivery=event.persist_before_delivery,
                    )
                if event.event_type == "partial_answer":
                    text = event.payload.get("text")
                    if isinstance(text, str) and text:
                        self._last_partial_text = text
                if event.event_type == "step_done":
                    self._step_count = int(getattr(self, "_step_count", 0)) + 1
                ticket, prefix = await self._persist_event(event)
                if event.event_type == "final_answer" and ticket.actor_scope is ActorScope.ROOT:
                    self._last_answer = ticket
                await self._publish_compat_prefix(prefix)
                return True
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                if isinstance(exc, TerminalDurabilityAbort):
                    raise
                self._fatal_error = exc
                raise TerminalDurabilityAbort("terminal durable event write failed") from exc

    async def _emit_done(self, event: OrchestratorEvent) -> bool:
        """Drain admitted producers before taking the terminal emit lock."""

        try:
            await self._close_producer_admission(self._execution)
            await self._await_producer_barrier(self._execution)
            async with self._emit_lock:
                if self._receipt is not None:
                    # The receipt may have committed before live fanout failed.
                    # Force subscribers to reconnect to durable replay; never
                    # claim a duplicate done succeeded while leaving them hung.
                    await self._close_terminal_delivery_for_replay()
                    return True
                if self._fatal_error is not None:
                    self.raise_if_failed()
                return await self._finalize(event)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fatal_error = exc
            if isinstance(exc, TerminalDurabilityAbort):
                raise
            raise TerminalDurabilityAbort("terminal durable finalization failed") from exc

    async def commit_cancelled(self) -> CommittedReceipt:
        """Commit an exact active generation as cancelled before process signaling.

        Stop cannot wait for admitted producer tasks: those tasks may need the
        process-local cancel signal in order to finish.  Closing producer
        admission and entering finalization under the generation's emit lock
        establishes the durable write fence first; the caller may signal the
        worker only after this method returns a committed receipt.
        """

        event = OrchestratorEvent(
            "done",
            self.scope.turn_id,
            {"status": TerminalStatus.CANCELLED.value},
        )
        try:
            await self._close_producer_admission(self._execution)
            async with self._emit_lock:
                if self._receipt is not None:
                    if not self._terminal_delivery_finalized:
                        await self._close_terminal_delivery_for_replay()
                    return self._receipt
                if self._fatal_error is not None:
                    self.raise_if_failed()
                existing_types = {required.event_type for required in self._required_terminal_events}
                cancellation_events: list[StagedTerminalEvent] = []
                if "turn_cancelled" not in existing_types:
                    cancellation_events.append(
                        StagedTerminalEvent.from_payload(
                            event_id=_stable_id(
                                "evt",
                                self.scope,
                                self._execution.claim_epoch,
                                "web_cancelled",
                            ),
                            event_type="turn_cancelled",
                            actor_scope=ActorScope.ROOT,
                            phase_scope=PhaseScope.IN_GENERATION,
                            payload={"turn_id": self.scope.turn_id},
                        )
                    )
                if "step_list_card_finalized" not in existing_types:
                    cancellation_events.append(
                        StagedTerminalEvent.from_payload(
                            event_id=_stable_id(
                                "evt",
                                self.scope,
                                self._execution.claim_epoch,
                                "cancelled_card",
                            ),
                            event_type="step_list_card_finalized",
                            actor_scope=ActorScope.ROOT,
                            phase_scope=PhaseScope.IN_GENERATION,
                            payload={
                                "card_id": self.scope.turn_id,
                                "state": "cancelled",
                                "step_count": int(getattr(self, "_step_count", 0) or 0),
                            },
                        )
                    )
                self._required_terminal_events = (
                    *self._required_terminal_events,
                    *cancellation_events,
                )
                self._card_finalized_emitted = True
                await self._finalize(event)
                assert self._receipt is not None
                return self._receipt
        except asyncio.CancelledError:
            # Finalization may have committed and assigned the receipt before
            # cancellation interrupted compatibility fanout.  Durable truth
            # wins; the Stop route can still signal/recover the exact worker.
            if self._receipt is not None:
                await self._shielded_close_terminal_delivery_for_replay()
                return self._receipt
            raise
        except BaseException as exc:
            # Finalization may have committed before compatibility fanout
            # failed.  Preserve that authoritative outcome for the Stop route.
            if self._receipt is not None:
                await self._close_terminal_delivery_for_replay()
                return self._receipt
            self._fatal_error = exc
            if isinstance(exc, TerminalDurabilityAbort):
                raise
            raise TerminalDurabilityAbort("terminal durable cancellation failed") from exc

    async def _persist_event(
        self,
        event: OrchestratorEvent,
    ) -> tuple[EventTicket, tuple[EventTicket, ...]]:
        payload = dict(event.payload)
        if event.parent_agent_id is not None:
            # The durable row has a coarse peer actor scope.  Preserve the exact
            # parent identity in the payload so live, replay and IM folding see
            # the same peer lineage after process restart.
            payload["parent_agent_id"] = event.parent_agent_id
        handle = await self._writer.accept_event(
            event_type=event.event_type,
            actor_scope=_actor_scope(event),
            phase_scope=PhaseScope.IN_GENERATION,
            payload=payload,
            publish_mode=(EventPublishMode.BEFORE_LIVE if event.persist_before_delivery else EventPublishMode.NORMAL),
            root_durability=RootDurability.COMMITTED,
        )
        return await handle.persisted_with_prefix()

    async def _publish_compat(
        self,
        ticket: EventTicket,
    ) -> None:
        if ticket.event_id in self._compat_published_event_ids:
            return
        payload = ticket.payload()
        persisted_parent_agent_id = payload.get("parent_agent_id")
        event = OrchestratorEvent(
            event_type=ticket.event_type,  # type: ignore[arg-type]
            turn_id=ticket.scope.turn_id,
            payload=payload,
            occurred_at=ticket.occurred_at,
            seq=ticket.event_seq,
            parent_agent_id=(
                persisted_parent_agent_id
                if isinstance(persisted_parent_agent_id, str) and persisted_parent_agent_id and ticket.actor_scope is ActorScope.PEER
                else ("peer" if ticket.actor_scope is ActorScope.PEER else None)
            ),
        )
        event.payload["event_id"] = ticket.event_id
        event.payload["event_seq"] = ticket.event_seq
        event.payload["generation"] = ticket.scope.generation
        self._live.seed_seq_counter(ticket.event_seq)
        emit_task = asyncio.create_task(self._live.emit(event))
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                emitted = await asyncio.shield(emit_task)
                break
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
                if emit_task.done():
                    if emit_task.cancelled():
                        raise
                    emitted = emit_task.result()
                    break
        if emitted:
            self._compat_published_event_ids.add(ticket.event_id)
        if cancellation is not None:
            raise cancellation

    async def _finalize(self, event: OrchestratorEvent) -> bool:
        self._terminal_snapshot = self._merged_terminal_payload(event.payload)
        status = _status_from_done(event.payload)
        contract = await self._store.read_effect_execution_contract(
            self._execution.scope,
        )
        if contract.disposition is EffectIntentDisposition.RECOVERY_ONLY:
            if status is not TerminalStatus.CANCELLED:
                input_tokens = _exact_nonnegative_int(
                    self._terminal_snapshot.get("tokens_input"),
                )
                output_tokens = _exact_nonnegative_int(
                    self._terminal_snapshot.get("tokens_output"),
                )
                # Terminal mode intentionally skips the legacy persist
                # chokepoint that used to add cost_usd to done. Derive it from
                # the same single estimator used by every physical settlement.
                from bowhead_core.turn_runner import _estimate_cost_usd

                cost_usd = _estimate_cost_usd(input_tokens, output_tokens)
                self._terminal_snapshot["cost_usd"] = cost_usd
                await self._store.verify_execution_usage(
                    self._execution,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_amount=cost_usd,
                )
        else:
            await self._store.record_execution_usage(
                self._execution,
                input_tokens=_nonnegative_int(
                    self._terminal_snapshot.get("tokens_input"),
                ),
                output_tokens=_nonnegative_int(
                    self._terminal_snapshot.get("tokens_output"),
                ),
                cost_amount=_nonnegative_float(
                    self._terminal_snapshot.get("cost_usd"),
                ),
            )
        request = FinalizeRequest(
            execution=self._execution,
            finalization_owner_instance_id=self._identity.owner_instance_id,
            closing_reason=f"root_done:{status.value}",
            selected_variant_key=None,
            authz_snapshot_digest=self._identity.authz_snapshot_digest,
            writer_admission_epoch=self._identity.writer_admission_epoch,
            candidate_builder=self._build_candidate,
        )
        receipt = await (self._finalizer.finalize_cancelled(request) if status is TerminalStatus.CANCELLED else self._finalizer.finalize(request))
        self._receipt = receipt
        self._receipt_ready.set()
        await self._deliver_committed_receipt(receipt)
        if receipt.selected_session_variant is SessionVariant.FOLLOWUP and self._followup_committed_callback is not None:
            callback = self._followup_committed_callback(receipt)
            if inspect.isawaitable(callback):
                await callback
        return True

    async def _deliver_committed_receipt(self, receipt: CommittedReceipt) -> None:
        """Publish one durable receipt or cut live readers over to replay."""

        try:
            await self._publish_terminal_rows(receipt)
            self._closed = True
            await self._generation_bus.close(
                claim_epoch=self._execution.claim_epoch,
            )
            self._terminal_delivery_finalized = True
        except asyncio.CancelledError:
            await self._shielded_close_terminal_delivery_for_replay()
            raise
        except BaseException:
            await self._close_terminal_delivery_for_replay()
            raise

    async def _shielded_close_terminal_delivery_for_replay(self) -> None:
        """Finish post-commit stream teardown despite caller cancellation."""

        cleanup = asyncio.create_task(self._close_terminal_delivery_for_replay())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # The durable receipt is already authoritative.  A repeated
                # disconnect cannot leave subscribers waiting on a dead live
                # fanout; finish teardown before returning that receipt.
                continue
        await cleanup

    async def _close_terminal_delivery_for_replay(self) -> None:
        """Fence writers and wake every live reader after committed fanout loss."""

        if self._terminal_delivery_finalized:
            return
        self._closed = True
        cleanup_error: BaseException | None = None
        try:
            await self._generation_bus.fail_fanout(
                claim_epoch=self._execution.claim_epoch,
            )
        except BaseException as exc:
            cleanup_error = exc
        try:
            await self._live.close()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
            else:
                cleanup_error.add_note(f"terminal compatibility stream close also failed: {exc!r}")
        if cleanup_error is not None:
            raise cleanup_error
        self._terminal_delivery_finalized = True

    async def _close_producer_admission(
        self,
        execution: ExecutionGenerationContext,
    ) -> None:
        if execution != self._execution:
            raise TerminalProtocolError("terminal producer execution fence changed")
        self._producer_tasks = await self._generation_bus.close_producer_admission(
            claim_epoch=self._execution.claim_epoch,
        )

    async def _await_producer_barrier(
        self,
        execution: ExecutionGenerationContext,
    ) -> None:
        if execution != self._execution:
            raise TerminalProtocolError("terminal producer execution fence changed")
        current = asyncio.current_task()
        pending = tuple(task for task in self._producer_tasks if task is not current and not task.done())
        if pending:
            await asyncio.gather(*pending)

    def _merged_terminal_payload(self, payload: dict[str, Any]) -> dict[str, object]:
        merged: dict[str, object] = {}
        state = getattr(self, "_pi_turn_state", None)
        if isinstance(state, dict):
            merged.update(state)
        merged.update(payload)
        return merged

    async def _build_candidate(self, snapshot: FinalizationBuildSnapshot) -> TerminalCandidatePlan:
        scope = snapshot.finalization.scope
        epoch = snapshot.finalization.finalization_epoch
        status = _status_from_done(self._terminal_snapshot)
        steer = await self._store.read_frozen_steer_snapshot(
            scope,
            claim_epoch=snapshot.finalization.execution_claim_epoch,
        )
        followup_certified = bool(
            steer.items
            and status is TerminalStatus.COMPLETED
            and self._followup_capability is not None
            and self._followup_capability.certified
            and self._followup_capability.adapter_generation == self._identity.rollout_config_generation
        )
        primary_session = (
            SessionVariant.NONE
            if not steer.items
            else SessionVariant.DISCARD
            if status is TerminalStatus.CANCELLED
            else SessionVariant.FOLLOWUP
            if followup_certified
            else SessionVariant.DEFERRED
        )
        recovery_session = SessionVariant.DEFERRED if primary_session is SessionVariant.FOLLOWUP else primary_session
        primary_key = f"primary_{primary_session.value}"
        recovery_key = f"recovery_{recovery_session.value}"
        primary_reason = (
            "user_cancelled"
            if status is TerminalStatus.CANCELLED
            else _compat_failure_reason(self._terminal_snapshot)
            if status in {TerminalStatus.FAILED, TerminalStatus.ABORTED}
            else None
        )
        candidate_id = _stable_id("tc", scope, epoch, "candidate")
        receipt_id = _stable_id("tr", scope, epoch, "receipt")
        done_id = _stable_id("evt", scope, epoch, "done")
        primary_events: list[StagedTerminalEvent] = list(self._required_terminal_events)
        answer_mode = FinalAnswerMode.NONE
        answer_id: str | None = None
        answer_digest: str | None = None

        staged_answers = [item for item in primary_events if item.event_type == "final_answer"]
        if len(staged_answers) > 1:
            raise RuntimeError("terminal candidate has multiple required answers")
        if staged_answers:
            answer_event = staged_answers[0]
            answer_mode = FinalAnswerMode.IN_TERMINAL_BATCH
            answer_id = answer_event.event_id
            answer_digest = answer_event.payload_digest

        if status is TerminalStatus.COMPLETED and not staged_answers:
            answer = self._last_answer
            if answer is not None and answer.event_seq <= snapshot.flush.persisted_event_highwater:
                answer_mode = FinalAnswerMode.PRECOMMITTED
                answer_id = answer.event_id
                answer_digest = answer.payload_digest
            else:
                text = self._candidate_answer_text()
                answer_event = StagedTerminalEvent.from_payload(
                    event_id=_stable_id("evt", scope, epoch, "final_answer"),
                    event_type="final_answer",
                    actor_scope=ActorScope.ROOT,
                    phase_scope=PhaseScope.IN_GENERATION,
                    payload={"text": text},
                )
                primary_events.append(answer_event)
                answer_mode = FinalAnswerMode.IN_TERMINAL_BATCH
                answer_id = answer_event.event_id
                answer_digest = answer_event.payload_digest
        elif status is TerminalStatus.FAILED and not staged_answers and self._last_answer is not None:
            answer = self._last_answer
            if answer.event_seq <= snapshot.flush.persisted_event_highwater:
                answer_mode = FinalAnswerMode.PRECOMMITTED
                answer_id = answer.event_id
                answer_digest = answer.payload_digest

        effect_disclosure = snapshot.effect_disclosure
        if effect_disclosure is not None and effect_disclosure.protocol == 2 and effect_disclosure.attempt_count:
            primary_events.append(
                StagedTerminalEvent.from_payload(
                    event_id=_stable_id(
                        "evt",
                        scope,
                        epoch,
                        "effect_primary",
                    ),
                    event_type=("effect_disclosure" if effect_disclosure.unsettled_count else "effect_settlement"),
                    actor_scope=ActorScope.ROOT,
                    phase_scope=PhaseScope.IN_GENERATION,
                    payload=effect_disclosure.event_payload,
                )
            )

        primary_template: dict[str, object] = {}
        if primary_session is SessionVariant.FOLLOWUP:
            followup_turn_id = _stable_id(
                "t",
                scope,
                epoch,
                f"followup:{steer.buffer_identity_digest}",
            )
            followup_request_id = _stable_id(
                "req",
                scope,
                epoch,
                f"followup-request:{steer.buffer_identity_digest}",
            )
            execution_intent = dict(self._followup_execution_intent)
            execution_intent["text"] = steer.merged_text
            execution_intent["attachments"] = list(steer.merged_attachments)
            execution_intent["planning_mode"] = steer.planning_mode
            execution_intent["workspace_id"] = steer.workspace_id
            execution_intent["source_authz_snapshot_digest"] = self._identity.authz_snapshot_digest
            execution_intent["source_steer_ids"] = [item.steer_id for item in steer.items]
            execution_intent["source_steer_request_digests"] = [item.request_digest for item in steer.items]
            canonical_intent = {
                "execution_intent": execution_intent,
                "request_id": followup_request_id,
                "session_id": scope.session_id,
                "team_id": scope.team_id,
                "turn_id": followup_turn_id,
                "user_text": steer.merged_text,
            }
            next_turn = {
                "execution_intent": execution_intent,
                "execution_intent_digest": canonical_digest_v1(canonical_intent),
                "request_id": followup_request_id,
                "turn_id": followup_turn_id,
                "user_text": steer.merged_text,
            }
            primary_template = {
                "next_turn": next_turn,
                "writer_admission_epoch": (self._identity.writer_admission_epoch),
            }
        primary_done = self._done_event(
            event_id=done_id,
            candidate_id=candidate_id,
            receipt_id=receipt_id,
            variant_key=primary_key,
            status=status,
            reason_code=primary_reason,
            message_key=_MESSAGE_KEYS[status],
            answer_mode=answer_mode,
            answer_id=answer_id,
            answer_digest=answer_digest,
            required_events=tuple(primary_events),
        )
        primary = TerminalCandidateVariant(
            variant_key=primary_key,
            outcome_variant=CandidateOutcome.PRIMARY,
            session_variant=primary_session,
            status=status,
            reason_code=primary_reason,
            message_key=_MESSAGE_KEYS[status],
            final_answer_mode=answer_mode,
            final_answer_event_id=answer_id,
            final_answer_digest=answer_digest,
            session_mutation_template=primary_template,
            events=(*primary_events, primary_done),
        )

        recovery_template: dict[str, object] = {}
        recovery_events: list[StagedTerminalEvent] = []
        if effect_disclosure is not None and (effect_disclosure.protocol == 1 or effect_disclosure.attempt_count):
            recovery_events.append(
                StagedTerminalEvent.from_payload(
                    event_id=_stable_id(
                        "evt",
                        scope,
                        epoch,
                        ("effect_disclosure" if effect_disclosure.protocol == 1 else "effect_recovery"),
                    ),
                    event_type=("effect_disclosure" if effect_disclosure.protocol == 1 or effect_disclosure.unsettled_count else "effect_settlement"),
                    actor_scope=ActorScope.ROOT,
                    phase_scope=PhaseScope.IN_GENERATION,
                    payload=effect_disclosure.event_payload,
                )
            )
        recovery_done = self._done_event(
            event_id=done_id,
            candidate_id=candidate_id,
            receipt_id=receipt_id,
            variant_key=recovery_key,
            status=TerminalStatus.ABORTED,
            reason_code="terminal_projection_not_committed",
            message_key="turn.result_not_committed",
            answer_mode=FinalAnswerMode.NONE,
            answer_id=None,
            answer_digest=None,
            required_events=tuple(recovery_events),
        )
        recovery = TerminalCandidateVariant(
            variant_key=recovery_key,
            outcome_variant=CandidateOutcome.RECOVERY_INTERRUPT,
            session_variant=recovery_session,
            status=TerminalStatus.ABORTED,
            reason_code="terminal_projection_not_committed",
            message_key="turn.result_not_committed",
            final_answer_mode=FinalAnswerMode.NONE,
            final_answer_event_id=None,
            final_answer_digest=None,
            session_mutation_template=recovery_template,
            events=(*recovery_events, recovery_done),
        )
        return TerminalCandidatePlan(
            scope=scope,
            candidate_id=candidate_id,
            reserved_receipt_id=receipt_id,
            reserved_terminal_event_id=done_id,
            origin=CandidateOrigin.NORMAL,
            closing_accepted_through=snapshot.flush.closing_accepted_through,
            persisted_event_highwater=snapshot.flush.persisted_event_highwater,
            variants=(primary, recovery),
            session_disposition_plan={
                "turn_projection": {
                    "error": _optional_text(self._terminal_snapshot.get("error")),
                    "error_kind": _optional_text(self._terminal_snapshot.get("error_kind")),
                    "intent": _optional_text(self._terminal_snapshot.get("intent")),
                },
                "variant_templates": {
                    primary_key: primary_template,
                    recovery_key: recovery_template,
                },
                "writer_admission_epoch": (self._identity.writer_admission_epoch if primary_session is SessionVariant.FOLLOWUP else None),
            },
            projection_extensions=(
                effect_disclosure.projection_extensions if effect_disclosure is not None and effect_disclosure.protocol == 2 else ()
            ),
        )

    def _candidate_answer_text(self) -> str:
        for value in (
            self._terminal_snapshot.get("final_answer"),
            self._last_partial_text,
        ):
            if isinstance(value, str) and value.strip():
                return value
        return "本次没有产出可展示的内容。请补充更具体的问题后重试。"

    def _done_event(
        self,
        *,
        event_id: str,
        candidate_id: str,
        receipt_id: str,
        variant_key: str,
        status: TerminalStatus,
        reason_code: str | None,
        message_key: str,
        answer_mode: FinalAnswerMode,
        answer_id: str | None,
        answer_digest: str | None,
        required_events: tuple[StagedTerminalEvent, ...],
    ) -> StagedTerminalEvent:
        manifest: list[dict[str, object]] = []
        if answer_mode is FinalAnswerMode.PRECOMMITTED:
            assert answer_id is not None
            assert answer_digest is not None
            manifest.append(
                {
                    "event_id": answer_id,
                    "event_type": "final_answer",
                    "ordinal": 1,
                    "payload_digest": answer_digest,
                }
            )
        offset = len(manifest)
        manifest.extend(
            {
                "event_id": item.event_id,
                "event_type": item.event_type,
                "ordinal": index + offset,
                "payload_digest": item.payload_digest,
            }
            for index, item in enumerate(required_events, start=1)
        )
        return StagedTerminalEvent.from_payload(
            event_id=event_id,
            event_type="done",
            actor_scope=ActorScope.ROOT,
            phase_scope=PhaseScope.IN_GENERATION,
            payload={
                "candidate_id": candidate_id,
                "generation": self.scope.generation,
                "message_key": message_key,
                "protocol": 1,
                "reason": reason_code,
                "receipt_id": receipt_id,
                "required_manifest_template_digest": canonical_digest_v1(manifest),
                "selected_variant_key": variant_key,
                "status": status.value,
            },
        )

    async def _publish_terminal_rows(self, receipt: CommittedReceipt) -> None:
        rows = await self._store.list_protocol_events(
            TerminalReadContext(
                team_id=self.scope.team_id,
                session_id=self.scope.session_id,
                turn_id=self.scope.turn_id,
                viewer=self._identity.owner_instance_id,
                auth_fence_vector_digest=self._identity.authz_snapshot_digest,
            ),
            after_event_seq=receipt.first_event_seq - 1,
            up_to_event_seq=receipt.last_event_seq,
        )
        tickets = tuple(_terminal_row_ticket(self.scope, receipt, row) for row in rows)
        await self._generation_bus.publish_batch(
            tickets,
            claim_epoch=self._execution.claim_epoch,
        )
        for ticket in tickets:
            await self._publish_compat(ticket)

    async def subscribe(self):  # type annotation mirrors legacy async iterator at runtime
        async for event in self._live.subscribe():
            yield event

    def snapshot(self) -> list[OrchestratorEvent]:
        return self._live.snapshot()

    async def close(self) -> None:
        cleanup_error: BaseException | None = None
        try:
            await self._writer.close()
        except BaseException as exc:
            cleanup_error = exc
        if not self._closed:
            self._closed = True
            try:
                await self._generation_bus.fail_fanout(claim_epoch=self._execution.claim_epoch)
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                else:
                    cleanup_error.add_note(f"terminal generation fanout close also failed: {exc!r}")
        try:
            await self._live.close()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
            else:
                cleanup_error.add_note(f"terminal compatibility stream close also failed: {exc!r}")
        if cleanup_error is not None:
            raise cleanup_error


def _terminal_row_ticket(
    scope: TerminalScope,
    receipt: CommittedReceipt,
    row: TerminalProtocolEventRow,
) -> EventTicket:
    return EventTicket(
        scope=scope,
        proposal_id=f"terminal:{receipt.receipt_id}:{row.event_seq}",
        event_id=row.event_id,
        accept_ordinal=row.event_seq,
        event_seq=row.event_seq,
        legacy_seq=row.event_seq,
        event_type=row.event_type,
        actor_scope=ActorScope(row.actor_scope),
        phase_scope=PhaseScope(row.phase_scope),
        payload_json=_canonical_payload(row.payload),
        payload_digest=row.payload_digest,
        root_durability=RootDurability.COMMITTED,
        occurred_at=row.occurred_at,
        terminal_receipt_id=row.terminal_receipt_id,
    )


def _canonical_payload(payload: dict[str, object]) -> str:

    return canonical_json_v1(payload)


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _nonnegative_int(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _exact_nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise TerminalProtocolMalformedError(
            "contract-v2 terminal usage tokens are invalid",
        )
    return value


def _nonnegative_float(value: object) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if parsed != parsed or parsed < 0 or parsed == float("inf"):
        return 0.0
    return parsed


__all__ = [
    "_CHAT_FAILURE_REASON_COMPAT",
    "_compat_failure_reason",
    "TerminalDurabilityAbort",
    "TerminalProtocolBusAdapter",
    "TerminalRuntimeIdentity",
    "TerminalTurnRuntime",
]
