"""Terminal Protocol v1 的 generation-scoped committed-event fanout。

这是 S2 尚未接线的并行实现。legacy ``EventBusRegistry`` 继续按 turn_id 工作；v1 bus
按 ``(turn_id, generation)`` 隔离，且只接受已经取得 ``EventTicket`` 的事件。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from enum import StrEnum
from uuid import uuid4

from bowhead_core.terminal_protocol import EventTicket, TerminalProtocolMalformedError, TerminalScope


class GenerationBusFaultPoint(StrEnum):
    FANOUT_BEFORE_APPEND = "FANOUT_BEFORE_APPEND"
    FANOUT_AFTER_PREFIX = "FANOUT_AFTER_PREFIX"
    FANOUT_AFTER_FULL = "FANOUT_AFTER_FULL"


class SubscriptionState(StrEnum):
    PAUSED = "paused"
    LIVE = "live"
    CLOSED = "closed"


class ProducerAdmissionClosedError(RuntimeError):
    """generation 已停止接收新的 runtime producer。"""


class GenerationSubscription:
    """每个 subscriber 拥有私有 cursor；paused 状态不会泄漏 live event。"""

    def __init__(
        self,
        bus: GenerationEventBus,
        *,
        subscriber_id: str,
        after_event_seq: int,
        paused: bool,
    ) -> None:
        self._bus = bus
        self.subscriber_id = subscriber_id
        self.bus_epoch = bus.bus_epoch
        self.after_event_seq = after_event_seq
        self.state = SubscriptionState.PAUSED if paused else SubscriptionState.LIVE
        self._released = False

    async def promote(self, *, after_event_seq: int | None = None) -> None:
        async with self._bus.lock:
            self._promote_locked(after_event_seq=after_event_seq)

    def _promote_locked(self, *, after_event_seq: int | None = None) -> Callable[[], None]:
        self._bus.assert_locked()
        if self.state is SubscriptionState.CLOSED:
            raise RuntimeError("terminal generation subscription is closed")
        prior_state = self.state
        prior_cursor = self.after_event_seq
        if after_event_seq is not None:
            if after_event_seq < self.after_event_seq:
                raise ValueError("subscription promotion cursor moved backwards")
            self.after_event_seq = after_event_seq
        self.state = SubscriptionState.LIVE

        def undo() -> None:
            self._bus.assert_locked()
            self.state = prior_state
            self.after_event_seq = prior_cursor

        return undo

    async def aclose(self) -> None:
        if self._released:
            return
        async with self._bus.lock:
            self.state = SubscriptionState.CLOSED
            self._released = True
            self._bus._release_subscription_locked(self.subscriber_id)

    async def events(self) -> AsyncIterator[EventTicket]:
        cursor = self.after_event_seq
        try:
            while True:
                batch: tuple[EventTicket, ...]
                async with self._bus.condition:
                    while True:
                        if self.state is SubscriptionState.CLOSED:
                            return
                        if self.state is SubscriptionState.LIVE:
                            batch = tuple(event for event in self._bus._events if event.event_seq > cursor)
                            if batch:
                                break
                            if self._bus.closed:
                                return
                        await self._bus.condition.wait()
                for event in batch:
                    cursor = event.event_seq
                    self.after_event_seq = cursor
                    yield event
        finally:
            await self.aclose()

    def __aiter__(self) -> AsyncIterator[EventTicket]:
        return self.events()


class ProducerLease:
    """begin-finalization barrier 可观察的 runtime producer identity。"""

    def __init__(self, bus: GenerationEventBus, producer_id: str, task: asyncio.Task[object] | None) -> None:
        self._bus = bus
        self.producer_id = producer_id
        self.task = task
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        async with self._bus.lock:
            self._released = True
            self._bus._producers.pop(self.producer_id, None)

    def bind_task(self, task: asyncio.Task[object]) -> None:
        """Bind a reserved lease to a child before yielding the event loop."""

        if self._released:
            raise RuntimeError("released producer lease cannot be rebound")
        if not isinstance(task, asyncio.Task):
            raise TypeError("producer lease task is invalid")
        self.task = task

    async def __aenter__(self) -> ProducerLease:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        await self.release()


class GenerationEventBus:
    """只发布 commit 后 ticket 的 per-generation append-only ring。"""

    def __init__(
        self,
        scope: TerminalScope,
        *,
        claim_epoch: int,
        bus_epoch: int,
        _fault_injector: Callable[[GenerationBusFaultPoint], None] | None = None,
    ) -> None:
        if not isinstance(scope, TerminalScope):
            raise TypeError("generation bus scope is invalid")
        if not isinstance(claim_epoch, int) or isinstance(claim_epoch, bool) or claim_epoch < 1:
            raise ValueError("generation bus claim epoch must be positive")
        if not isinstance(bus_epoch, int) or isinstance(bus_epoch, bool) or bus_epoch < 1:
            raise ValueError("generation bus epoch must be positive")
        self.scope = scope
        self.claim_epoch = claim_epoch
        self.bus_epoch = bus_epoch
        self.lock = asyncio.Lock()
        self.condition = asyncio.Condition(self.lock)
        self._events: list[EventTicket] = []
        self._events_by_id: dict[str, EventTicket] = {}
        self._subscriptions: dict[str, GenerationSubscription] = {}
        self._producers: dict[str, ProducerLease] = {}
        self._producer_admission_open = True
        self.closed = False
        self.retired = False
        self.fanout_failed = False
        self._fault_injector = _fault_injector

    def assert_locked(self) -> None:
        if not self.lock.locked():
            raise RuntimeError("generation bus lock is required")

    def _inject(self, point: GenerationBusFaultPoint) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    async def publish_batch(
        self,
        tickets: tuple[EventTicket, ...],
        *,
        claim_epoch: int,
    ) -> tuple[EventTicket, ...]:
        """原子 append committed batch；相同 event ID 重入只做 digest 校验。"""

        if not tickets:
            return ()
        async with self.condition:
            if self.closed or self.fanout_failed or claim_epoch != self.claim_epoch:
                raise TerminalProtocolMalformedError("generation bus ownership fence changed")
            self._inject(GenerationBusFaultPoint.FANOUT_BEFORE_APPEND)
            staged: list[EventTicket] = []
            staged_by_id: dict[str, EventTicket] = {}
            prior_seq = 0
            for ticket in tickets:
                if ticket.scope != self.scope:
                    raise TerminalProtocolMalformedError("generation bus ticket scope mismatch")
                if prior_seq and ticket.event_seq != prior_seq + 1:
                    raise TerminalProtocolMalformedError("generation bus batch is not contiguous")
                prior_seq = ticket.event_seq
                existing = self._events_by_id.get(ticket.event_id)
                if existing is not None:
                    if existing != ticket:
                        raise TerminalProtocolMalformedError("generation bus event identity conflict")
                    continue
                staged_existing = staged_by_id.get(ticket.event_id)
                if staged_existing is not None:
                    if staged_existing != ticket:
                        raise TerminalProtocolMalformedError("generation bus batch contains an event identity conflict")
                    continue
                prior_persisted_seq = staged[-1].event_seq if staged else (self._events[-1].event_seq if self._events else None)
                if prior_persisted_seq is not None and ticket.event_seq != prior_persisted_seq + 1:
                    raise TerminalProtocolMalformedError("generation bus event sequence is not contiguous")
                staged.append(ticket)
                staged_by_id[ticket.event_id] = ticket
            # Fault occurs in private staging; subscribers never see partial batch.
            self._inject(GenerationBusFaultPoint.FANOUT_AFTER_PREFIX)
            self._events.extend(staged)
            self._events_by_id.update((ticket.event_id, ticket) for ticket in staged)
            self.condition.notify_all()
            self._inject(GenerationBusFaultPoint.FANOUT_AFTER_FULL)
            return tuple(staged)

    async def subscribe_after(
        self,
        after_event_seq: int,
        *,
        paused: bool = True,
        subscriber_id: str | None = None,
    ) -> GenerationSubscription:
        if not isinstance(after_event_seq, int) or isinstance(after_event_seq, bool) or after_event_seq < 0:
            raise ValueError("subscriber cursor must be non-negative")
        identity = subscriber_id or f"sub_{uuid4().hex}"
        async with self.lock:
            if self.retired or self.fanout_failed:
                raise RuntimeError("terminal generation bus is retired")
            if identity in self._subscriptions:
                raise ValueError("subscriber identity already exists")
            subscription = GenerationSubscription(
                self,
                subscriber_id=identity,
                after_event_seq=after_event_seq,
                paused=paused,
            )
            self._subscriptions[identity] = subscription
            return subscription

    async def register_producer(self, *, claim_epoch: int) -> ProducerLease:
        async with self.lock:
            if self.closed or not self._producer_admission_open or claim_epoch != self.claim_epoch:
                raise ProducerAdmissionClosedError("generation producer admission is closed")
            producer_id = f"prod_{uuid4().hex}"
            current = asyncio.current_task()
            task = current if isinstance(current, asyncio.Task) else None
            lease = ProducerLease(self, producer_id, task)
            self._producers[producer_id] = lease
            return lease

    async def close_producer_admission(self, *, claim_epoch: int) -> tuple[asyncio.Task[object], ...]:
        async with self.lock:
            if claim_epoch != self.claim_epoch:
                raise TerminalProtocolMalformedError("generation producer ownership fence changed")
            self._producer_admission_open = False
            return tuple(lease.task for lease in self._producers.values() if lease.task is not None)

    async def close(self, *, claim_epoch: int) -> None:
        async with self.condition:
            if claim_epoch != self.claim_epoch:
                raise TerminalProtocolMalformedError("stale owner cannot close generation bus")
            self._producer_admission_open = False
            self.closed = True
            self.condition.notify_all()

    async def fail_fanout(self, *, claim_epoch: int) -> None:
        """fanout 失败后断开当前订阅；producer/DB writer 仍可继续持久化。"""

        async with self.condition:
            if claim_epoch != self.claim_epoch:
                raise TerminalProtocolMalformedError("stale owner cannot fail generation fanout")
            self.fanout_failed = True
            subscriptions = tuple(self._subscriptions.values())
            self._subscriptions.clear()
            for subscription in subscriptions:
                subscription.state = SubscriptionState.CLOSED
                subscription._released = True
            self.condition.notify_all()

    async def snapshot_after(self, event_seq: int) -> tuple[EventTicket, ...]:
        async with self.lock:
            return tuple(event for event in self._events if event.event_seq > event_seq)

    def promote_with_overlap_locked(
        self,
        subscription: GenerationSubscription,
        *,
        after_event_seq: int,
        through_event_seq: int,
    ) -> tuple[tuple[EventTicket, ...], Callable[[], None]]:
        """C3 在 bus lock 内验证覆盖并原子提升 paused subscriber。"""

        self.assert_locked()
        if self._subscriptions.get(subscription.subscriber_id) is not subscription:
            raise RuntimeError("terminal generation subscription is not registered")
        if subscription.state is not SubscriptionState.PAUSED or subscription.bus_epoch != self.bus_epoch or self.retired:
            raise RuntimeError("terminal generation subscription cannot be promoted")
        overlap = tuple(event for event in self._events if after_event_seq < event.event_seq <= through_event_seq)
        expected_sequences = set(range(after_event_seq + 1, through_event_seq + 1))
        if {event.event_seq for event in overlap} != expected_sequences:
            raise TerminalProtocolMalformedError("generation bus cannot cover replay overlap")
        undo = subscription._promote_locked(after_event_seq=through_event_seq)
        return overlap, undo

    @property
    def subscription_count(self) -> int:
        return len(self._subscriptions)

    def _release_subscription_locked(self, subscriber_id: str) -> None:
        self.assert_locked()
        self._subscriptions.pop(subscriber_id, None)
        self.condition.notify_all()


class GenerationEventBusRegistry:
    """按 ``(turn_id,generation)`` 保存 bus，旧代在订阅释放前保持可读。"""

    def __init__(
        self,
        *,
        _fault_injector: Callable[[GenerationBusFaultPoint], None] | None = None,
    ) -> None:
        self._buses: dict[tuple[str, int], GenerationEventBus] = {}
        self._lock = asyncio.Lock()
        self._next_bus_epoch = 1
        self._fault_injector = _fault_injector

    async def get_or_create(self, scope: TerminalScope, *, claim_epoch: int) -> GenerationEventBus:
        key = (scope.turn_id, scope.generation)
        async with self._lock:
            bus = self._buses.get(key)
            if bus is not None:
                if bus.scope != scope or bus.claim_epoch != claim_epoch:
                    raise TerminalProtocolMalformedError("generation bus registry identity conflict")
                return bus
            bus = GenerationEventBus(
                scope,
                claim_epoch=claim_epoch,
                bus_epoch=self._next_bus_epoch,
                _fault_injector=self._fault_injector,
            )
            self._next_bus_epoch += 1
            self._buses[key] = bus
            return bus

    async def get(self, scope: TerminalScope) -> GenerationEventBus | None:
        async with self._lock:
            bus = self._buses.get((scope.turn_id, scope.generation))
            return bus if bus is None or bus.scope == scope else None

    async def retire(self, scope: TerminalScope, *, claim_epoch: int) -> None:
        key = (scope.turn_id, scope.generation)
        async with self._lock:
            bus = self._buses.get(key)
            if bus is None:
                return
            if bus.scope != scope or bus.claim_epoch != claim_epoch:
                raise TerminalProtocolMalformedError("stale owner cannot retire generation bus")
            async with bus.condition:
                bus.retired = True
                subscription_count = bus.subscription_count
                bus.condition.notify_all()
            if subscription_count == 0:
                self._buses.pop(key, None)

    async def collect_retired(self) -> int:
        async with self._lock:
            removable = [key for key, bus in self._buses.items() if bus.retired and bus.subscription_count == 0]
            for key in removable:
                self._buses.pop(key, None)
            return len(removable)

    @property
    def size(self) -> int:
        return len(self._buses)
