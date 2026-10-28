"""Terminal Protocol v1 durable accept → materialize → live publish runtime。

S2 只提供未接线 seam；生产 Orchestrator 仍使用 legacy bus/log sink，writer readiness 保持 OFF。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress

from bowhead_core.terminal_generation_bus import GenerationEventBus
from bowhead_core.terminal_protocol import (
    AcceptedEventProposal,
    ActorScope,
    EventPublishMode,
    EventTicket,
    ExecutionGenerationContext,
    PhaseScope,
    RootDurability,
)
from bowhead_core.terminal_protocol_store import TerminalProtocolStore


class AcceptedEventHandle:
    """durable proposal identity + commit 后才 resolve 的 ticket future。"""

    def __init__(self, proposal: AcceptedEventProposal) -> None:
        self.proposal = proposal
        self._future: asyncio.Future[tuple[EventTicket, tuple[EventTicket, ...]]] = asyncio.get_running_loop().create_future()

    @property
    def proposal_id(self) -> str:
        return self.proposal.proposal_id

    @property
    def event_id(self) -> str:
        return self.proposal.event_id

    @property
    def accept_ordinal(self) -> int:
        return self.proposal.accept_ordinal

    async def persisted(self) -> EventTicket:
        ticket, _prefix = await asyncio.shield(self._future)
        return ticket

    async def persisted_with_prefix(
        self,
    ) -> tuple[EventTicket, tuple[EventTicket, ...]]:
        return await asyncio.shield(self._future)

    def _resolve(
        self,
        ticket: EventTicket,
        prefix: tuple[EventTicket, ...],
    ) -> None:
        if not self._future.done():
            self._future.set_result((ticket, prefix))

    def _reject(self, error: BaseException) -> None:
        if not self._future.done():
            self._future.set_exception(error)


class TurnEventWriter:
    """一个 running generation 的短 batch writer。

    queue 只是降延迟设施，不是 correctness truth；``flush_through`` 始终从 durable proposal
    prefix 重建，因此 accept commit 后进程死亡也能由后续 S3 takeover 收敛。
    """

    def __init__(
        self,
        store: TerminalProtocolStore,
        bus: GenerationEventBus,
        context: ExecutionGenerationContext,
        *,
        batch_delay_seconds: float = 0.010,
        batch_size: int = 32,
    ) -> None:
        if bus.scope != context.scope or bus.claim_epoch != context.claim_epoch:
            raise ValueError("event writer bus and execution context must share identity")
        if batch_delay_seconds <= 0:
            raise ValueError("event writer batch delay must be positive")
        if batch_size < 1:
            raise ValueError("event writer batch size must be positive")
        self._store = store
        self._bus = bus
        self._context = context
        self._batch_delay_seconds = batch_delay_seconds
        self._batch_size = batch_size
        self._pending: dict[int, AcceptedEventHandle] = {}
        self._accept_lock = asyncio.Lock()
        self._pending_lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        self._background_error: BaseException | None = None
        self._fanout_error: BaseException | None = None

    @property
    def fanout_error(self) -> BaseException | None:
        return self._fanout_error

    async def accept_event(
        self,
        *,
        event_type: str,
        actor_scope: ActorScope,
        phase_scope: PhaseScope,
        payload: Mapping[str, object],
        publish_mode: EventPublishMode = EventPublishMode.NORMAL,
        root_durability: RootDurability = RootDurability.COMMITTED,
        idempotency_key: str | None = None,
    ) -> AcceptedEventHandle:
        async with self._accept_lock:
            if self._closed:
                raise RuntimeError("terminal event writer is closed")
            if self._background_error is not None:
                raise RuntimeError("terminal event writer background flush failed") from self._background_error
            proposal = await self._store.accept_event(
                self._context,
                event_type=event_type,
                actor_scope=actor_scope,
                phase_scope=phase_scope,
                payload=payload,
                publish_mode=publish_mode,
                root_durability=root_durability,
                idempotency_key=idempotency_key,
            )
            handle = AcceptedEventHandle(proposal)
            async with self._pending_lock:
                self._pending[proposal.accept_ordinal] = handle
                self._ensure_worker_locked()
                if publish_mode is EventPublishMode.BEFORE_LIVE or len(self._pending) >= self._batch_size:
                    self._wake.set()
            return handle

    async def flush_through(self, accepted_through: int) -> tuple[EventTicket, ...]:
        async with self._flush_lock:
            tickets = await self._store.flush_through(
                self._context,
                accepted_through=accepted_through,
            )
            # Ticket linearizes at DB commit: resolve first, then try live fanout.
            async with self._pending_lock:
                for ticket in tickets:
                    handle = self._pending.get(ticket.accept_ordinal)
                    if handle is not None:
                        handle._resolve(ticket, tickets)
                for ordinal in tuple(self._pending):
                    if ordinal <= accepted_through and self._pending[ordinal]._future.done():
                        self._pending.pop(ordinal, None)
            try:
                await self._bus.publish_batch(tickets, claim_epoch=self._context.claim_epoch)
            except asyncio.CancelledError as exc:
                self._fanout_error = exc
                await asyncio.shield(self._bus.fail_fanout(claim_epoch=self._context.claim_epoch))
                raise
            except Exception as exc:
                # DB ticket committed; live fanout failure only interrupts stream for client replay,
                # never rolls back durable business event or aborts subsequent execution.
                self._fanout_error = exc
                await self._bus.fail_fanout(claim_epoch=self._context.claim_epoch)
            return tickets

    async def drain(self) -> tuple[EventTicket, ...]:
        async with self._pending_lock:
            accepted_through = max(self._pending, default=0)
        if accepted_through == 0:
            return ()
        return await self.flush_through(accepted_through)

    async def close(self) -> None:
        async with self._accept_lock:
            self._closed = True
            self._wake.set()
        worker = self._worker
        try:
            await self.drain()
        finally:
            if worker is not None and worker is not asyncio.current_task():
                await asyncio.gather(worker, return_exceptions=True)

    def _ensure_worker_locked(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run(),
                name=f"terminal-event-writer:{self._context.scope.turn_id}:{self._context.scope.generation}",
            )

    async def _run(self) -> None:
        try:
            while not self._closed:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self._batch_delay_seconds)
                self._wake.clear()
                async with self._pending_lock:
                    batch = sorted(self._pending)[: self._batch_size]
                    accepted_through = batch[-1] if batch else 0
                if accepted_through:
                    await self.flush_through(accepted_through)
                    async with self._pending_lock:
                        if self._pending:
                            self._wake.set()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._background_error = exc
            async with self._pending_lock:
                handles = tuple(self._pending.values())
            for handle in handles:
                handle._reject(exc)


__all__ = ["AcceptedEventHandle", "TurnEventWriter"]
