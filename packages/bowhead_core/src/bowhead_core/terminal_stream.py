"""Terminal Protocol v1 coherent checkpoint、replay→live 与 transition watch seam。"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum

from bowhead_core.terminal_generation_bus import (
    GenerationEventBusRegistry,
    GenerationSubscription,
    SubscriptionState,
)
from bowhead_core.terminal_protocol import (
    AuthFenceResolveContext,
    AuthFenceVector,
    EventTicket,
    GenerationPointer,
    GenerationState,
    PromoteCertificate,
    SubscriberFenceToken,
    TerminalProtocolMalformedError,
    TerminalScope,
)
from bowhead_core.terminal_protocol_store import (
    TerminalCheckpoint,
    TerminalProtocolEventRow,
    TerminalProtocolStore,
    TerminalReadContext,
)


class ReplayHandoffFaultPoint(StrEnum):
    REPLAY_AFTER_C1 = "REPLAY_AFTER_C1"
    REPLAY_AFTER_SUBSCRIBE = "REPLAY_AFTER_SUBSCRIBE"
    REPLAY_AFTER_C2 = "REPLAY_AFTER_C2"
    REPLAY_BEFORE_PROMOTE = "REPLAY_BEFORE_PROMOTE"
    REPLAY_AFTER_PROMOTE = "REPLAY_AFTER_PROMOTE"


class ReplayHandoffRetry(RuntimeError):
    """coherent handoff fence 已变化；caller 应从 C1 自动重试。"""


@dataclass(frozen=True, slots=True)
class AuthorizedFence:
    resolve_context: AuthFenceResolveContext
    vector: AuthFenceVector

    def __post_init__(self) -> None:
        if not isinstance(self.resolve_context, AuthFenceResolveContext) or not isinstance(self.vector, AuthFenceVector):
            raise TypeError("authorized fence proof is invalid")


@dataclass(frozen=True, slots=True)
class ReplayHandoffResult:
    checkpoint: TerminalCheckpoint
    replay_events: tuple[TerminalProtocolEventRow, ...]
    overlap_events: tuple[EventTicket, ...]
    subscription: GenerationSubscription | None
    promote_certificate: PromoteCertificate | None

    @property
    def live(self) -> bool:
        return self.subscription is not None and self.promote_certificate is not None


class TerminalReplayHandoff:
    """C1 → paused subscribe → C2 replay → bus-lock/C3 promotion。"""

    def __init__(
        self,
        store: TerminalProtocolStore,
        buses: GenerationEventBusRegistry,
        *,
        _fault_injector: Callable[[ReplayHandoffFaultPoint], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._buses = buses
        self._fault_injector = _fault_injector

    async def connect(
        self,
        *,
        team_id: str,
        session_id: str,
        turn_id: str,
        client_cursor: int,
        authorize: Callable[[], Awaitable[AuthorizedFence]],
        max_retries: int = 4,
    ) -> ReplayHandoffResult:
        if client_cursor < 0:
            raise ValueError("client cursor must be non-negative")
        if max_retries < 1:
            raise ValueError("replay handoff retries must be positive")
        last_error: BaseException | None = None
        for _attempt in range(max_retries):
            subscription: GenerationSubscription | None = None
            try:
                first_auth = await self._authorize_current(
                    authorize,
                    team_id=team_id,
                    session_id=session_id,
                    turn_id=turn_id,
                )
                read_context = TerminalReadContext(
                    team_id=team_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    viewer=first_auth.resolve_context.viewer,
                    auth_fence_vector_digest=first_auth.vector.auth_fence_vector_digest,
                )
                c1 = await self._store.read_checkpoint(read_context)
                if client_cursor > c1.persistent_event_highwater:
                    raise TerminalProtocolMalformedError("client cursor exceeds persistent event highwater")
                await self._inject(ReplayHandoffFaultPoint.REPLAY_AFTER_C1)
                scope = TerminalScope(
                    team_id=team_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    generation=c1.current_generation,
                )
                bus = await self._buses.get(scope)
                if bus is None or bus.fanout_failed or c1.current_generation_state is not GenerationState.RUNNING:
                    second_auth = await self._authorize_current(
                        authorize,
                        team_id=team_id,
                        session_id=session_id,
                        turn_id=turn_id,
                    )
                    c2_context = TerminalReadContext(
                        team_id=team_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        viewer=second_auth.resolve_context.viewer,
                        auth_fence_vector_digest=second_auth.vector.auth_fence_vector_digest,
                    )
                    c2 = await self._store.read_checkpoint(c2_context)
                    await self._inject(ReplayHandoffFaultPoint.REPLAY_AFTER_C2)
                    if (
                        c2.subscriber_fence_token != c1.subscriber_fence_token
                        or second_auth.resolve_context != first_auth.resolve_context
                        or second_auth.vector != first_auth.vector
                    ):
                        raise ReplayHandoffRetry("checkpoint or authorization changed during replay-only handoff")
                    replay = await self._store.list_protocol_events(
                        c2_context,
                        after_event_seq=client_cursor,
                        up_to_event_seq=c2.persistent_event_highwater,
                    )
                    return ReplayHandoffResult(
                        checkpoint=c2,
                        replay_events=tuple(replay),
                        overlap_events=(),
                        subscription=None,
                        promote_certificate=None,
                    )
                subscription = await bus.subscribe_after(client_cursor, paused=True)
                subscribed_bus_epoch = subscription.bus_epoch
                await self._inject(ReplayHandoffFaultPoint.REPLAY_AFTER_SUBSCRIBE)

                second_auth = await self._authorize_current(
                    authorize,
                    team_id=team_id,
                    session_id=session_id,
                    turn_id=turn_id,
                )
                c2_context = TerminalReadContext(
                    team_id=team_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    viewer=second_auth.resolve_context.viewer,
                    auth_fence_vector_digest=second_auth.vector.auth_fence_vector_digest,
                )
                c2 = await self._store.read_checkpoint(c2_context)
                await self._inject(ReplayHandoffFaultPoint.REPLAY_AFTER_C2)
                if (
                    c2.subscriber_fence_token != c1.subscriber_fence_token
                    or second_auth.resolve_context != first_auth.resolve_context
                    or second_auth.vector != first_auth.vector
                    or bus.bus_epoch != subscribed_bus_epoch
                ):
                    raise ReplayHandoffRetry("checkpoint or authorization changed between C1 and C2")
                replay = tuple(
                    await self._store.list_protocol_events(
                        c2_context,
                        after_event_seq=client_cursor,
                        up_to_event_seq=c2.persistent_event_highwater,
                    )
                )
                await self._inject(ReplayHandoffFaultPoint.REPLAY_BEFORE_PROMOTE)
                async with bus.lock:
                    if subscription.state is not SubscriptionState.PAUSED or subscription.bus_epoch != subscribed_bus_epoch or bus.retired:
                        raise ReplayHandoffRetry("generation bus changed before promotion")
                    overlap: tuple[EventTicket, ...] = ()

                    def promote(
                        c3: TerminalCheckpoint,
                        *,
                        auth_digest: str = second_auth.vector.auth_fence_vector_digest,
                        bus_for_promote=bus,
                        c2_highwater: int = c2.persistent_event_highwater,
                        subscription_for_promote=subscription,
                    ) -> Callable[[], None]:
                        nonlocal overlap
                        if c3.auth_fence_vector_digest != auth_digest:
                            raise ReplayHandoffRetry("authorization digest changed before promotion")
                        try:
                            overlap, undo = bus_for_promote.promote_with_overlap_locked(
                                subscription_for_promote,
                                after_event_seq=c2_highwater,
                                through_event_seq=c3.persistent_event_highwater,
                            )
                        except (RuntimeError, TerminalProtocolMalformedError) as exc:
                            raise ReplayHandoffRetry("generation bus cannot cover C2 to C3 overlap") from exc
                        return undo

                    c3 = await self._store.linearize_subscription_promotion(
                        c2_context,
                        expected_pointer=c2.generation_pointer,
                        resolve_context=second_auth.resolve_context,
                        expected_auth_fence_vector=second_auth.vector,
                        promote=promote,
                    )
                    if c3 is None:
                        raise ReplayHandoffRetry("generation pointer changed during C3")
                    certificate = PromoteCertificate(
                        subscriber_fence_token=c3.subscriber_fence_token,
                        bus_epoch=subscribed_bus_epoch,
                        checkpoint_token=c3.checkpoint_token,
                        persistent_event_highwater=c3.persistent_event_highwater,
                    )
                await self._inject(ReplayHandoffFaultPoint.REPLAY_AFTER_PROMOTE)
                return ReplayHandoffResult(
                    checkpoint=c3,
                    replay_events=replay,
                    overlap_events=overlap,
                    subscription=subscription,
                    promote_certificate=certificate,
                )
            except ReplayHandoffRetry as exc:
                last_error = exc
                if subscription is not None:
                    await subscription.aclose()
                continue
            except BaseException:
                if subscription is not None:
                    await subscription.aclose()
                raise
        raise ReplayHandoffRetry("replay handoff retry budget exhausted") from last_error

    @staticmethod
    def _validate_authorized_fence(
        authorized: AuthorizedFence,
        *,
        team_id: str,
        session_id: str,
        turn_id: str,
    ) -> None:
        if not isinstance(authorized, AuthorizedFence):
            raise TerminalProtocolMalformedError("authorization resolver returned a malformed proof")
        if (
            authorized.resolve_context.team_id != team_id
            or authorized.resolve_context.session_id != session_id
            or authorized.resolve_context.resource_ref != turn_id
        ):
            raise TerminalProtocolMalformedError("authorization resolver returned the wrong resource identity")
        if tuple(entry.scope for entry in authorized.vector.entries) != tuple(
            sorted(authorized.resolve_context.required_scopes, key=lambda scope: scope.sort_key())
        ):
            raise TerminalProtocolMalformedError("authorization resolver returned an incomplete scope vector")

    async def _authorize_current(
        self,
        authorize: Callable[[], Awaitable[AuthorizedFence]],
        *,
        team_id: str,
        session_id: str,
        turn_id: str,
    ) -> AuthorizedFence:
        authorized = await authorize()
        self._validate_authorized_fence(
            authorized,
            team_id=team_id,
            session_id=session_id,
            turn_id=turn_id,
        )
        current = await self._store.read_auth_fence_vector(authorized.resolve_context)
        if current != authorized.vector:
            raise ReplayHandoffRetry("authorization vector changed during resolution")
        return authorized

    async def _inject(self, point: ReplayHandoffFaultPoint) -> None:
        if self._fault_injector is not None:
            await self._fault_injector(point)


@dataclass(slots=True)
class TransitionWatchSubscription:
    subscriber_id: str
    read_context: TerminalReadContext
    resolve_context: AuthFenceResolveContext
    expected_token: SubscriberFenceToken
    authorize: Callable[[AuthFenceResolveContext], Awaitable[bool]]
    on_generation_changed: Callable[[GenerationPointer], None]
    close: Callable[[], None]

    def __post_init__(self) -> None:
        if not isinstance(self.subscriber_id, str) or not self.subscriber_id:
            raise ValueError("transition watch subscriber identity is invalid")
        if not isinstance(self.read_context, TerminalReadContext) or not isinstance(
            self.resolve_context,
            AuthFenceResolveContext,
        ):
            raise TypeError("transition watch read/auth context is invalid")
        if not isinstance(self.expected_token, SubscriberFenceToken):
            raise TypeError("transition watch expected token is invalid")
        if not all(callable(callback) for callback in (self.authorize, self.on_generation_changed, self.close)):
            raise TypeError("transition watch callbacks are invalid")


class TransitionWatchMultiplexer:
    """按 Turn 复用 pointer batch read，authorization 只按相同 proof 合并。"""

    MAX_TURNS_PER_BATCH = 500
    POLL_INTERVAL_SECONDS = 1.0
    POLL_DEADLINE_SECONDS = 0.5

    def __init__(
        self,
        store: TerminalProtocolStore,
        *,
        _jitter: Callable[[], float] | None = None,
        _on_poll_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._store = store
        self._subscriptions: dict[str, TransitionWatchSubscription] = {}
        self._jitter = _jitter or (lambda: random.uniform(-0.2, 0.2))
        self._on_poll_error = _on_poll_error
        self.last_unique_turn_reads = 0
        self.last_authorization_checks = 0
        self.last_poll_error: BaseException | None = None
        self.last_close_errors: tuple[Exception, ...] = ()

    def register(self, subscription: TransitionWatchSubscription) -> None:
        if subscription.subscriber_id in self._subscriptions:
            raise ValueError("transition watch subscriber identity already exists")
        if (
            subscription.read_context.team_id != subscription.resolve_context.team_id
            or subscription.read_context.session_id != subscription.resolve_context.session_id
            or subscription.read_context.turn_id != subscription.resolve_context.resource_ref
            or subscription.read_context.viewer != subscription.resolve_context.viewer
        ):
            raise ValueError("transition watch authorization identity mismatch")
        if subscription.read_context.auth_fence_vector_digest != subscription.expected_token.auth_fence_vector_digest:
            raise ValueError("transition watch token is not viewer-bound to read context")
        self._subscriptions[subscription.subscriber_id] = subscription

    def unregister(self, subscriber_id: str) -> None:
        self._subscriptions.pop(subscriber_id, None)

    async def run(self, stop_event: asyncio.Event) -> None:
        """按 1s±20% 执行500ms有界扫描；超时/失败对当前订阅 fail closed。"""

        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            started_at = loop.time()
            try:
                await asyncio.wait_for(self.poll_once(), timeout=self.POLL_DEADLINE_SECONDS)
                self.last_poll_error = None
            except asyncio.CancelledError:
                self.close_all()
                raise
            except BaseException as exc:
                self.last_poll_error = exc
                self.close_all()
                if self._on_poll_error is not None:
                    self._on_poll_error(exc)
            elapsed = loop.time() - started_at
            jittered_interval = self.POLL_INTERVAL_SECONDS * (1 + self._jitter())
            delay = max(0.0, jittered_interval - elapsed)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=delay)

    def close_all(self) -> None:
        subscriptions = tuple(self._subscriptions.values())
        self._subscriptions.clear()
        errors: list[Exception] = []
        for subscription in subscriptions:
            try:
                subscription.close()
            except Exception as exc:
                errors.append(exc)
        self.last_close_errors = tuple(errors)

    async def poll_once(self) -> None:
        subscriptions = tuple(self._subscriptions.values())
        by_turn: dict[str, TerminalReadContext] = {}
        for subscription in subscriptions:
            prior = by_turn.setdefault(subscription.read_context.turn_id, subscription.read_context)
            if prior.team_id != subscription.read_context.team_id or prior.session_id != subscription.read_context.session_id:
                raise TerminalProtocolMalformedError("transition watch turn identity conflict")
        self.last_unique_turn_reads = len(by_turn)
        pointers: dict[str, GenerationPointer] = {}
        contexts = tuple(by_turn.values())
        for start in range(0, len(contexts), self.MAX_TURNS_PER_BATCH):
            pointers.update(await self._store.read_generation_pointers(contexts[start : start + self.MAX_TURNS_PER_BATCH]))

        auth_cache: dict[tuple[object, ...], tuple[bool, AuthFenceVector]] = {}
        self.last_authorization_checks = 0
        for subscription in subscriptions:
            if self._subscriptions.get(subscription.subscriber_id) is not subscription:
                continue
            scopes = tuple(sorted(subscription.resolve_context.required_scopes, key=lambda scope: scope.sort_key()))
            key = (
                subscription.resolve_context.team_id,
                subscription.resolve_context.viewer,
                subscription.resolve_context.resource_ref,
                subscription.resolve_context.session_id,
                scopes,
                id(subscription.authorize),
            )
            result = auth_cache.get(key)
            if result is None:
                vector = await self._store.read_auth_fence_vector(subscription.resolve_context)
                allowed = await subscription.authorize(subscription.resolve_context)
                if not isinstance(allowed, bool):
                    raise TypeError("transition watch authorization result must be boolean")
                result = (allowed, vector)
                auth_cache[key] = result
                self.last_authorization_checks += 1
            allowed, vector = result
            pointer = pointers[subscription.read_context.turn_id]
            if (
                not allowed
                or vector.auth_fence_vector_digest != subscription.expected_token.auth_fence_vector_digest
                or pointer.delivery_fence_epoch != subscription.expected_token.delivery_fence_epoch
            ):
                self._close_subscription(subscription)
                continue
            if pointer != subscription.expected_token.pointer:
                try:
                    subscription.on_generation_changed(pointer)
                finally:
                    self._close_subscription(subscription)

    def _close_subscription(self, subscription: TransitionWatchSubscription) -> None:
        self.unregister(subscription.subscriber_id)
        try:
            subscription.close()
        except Exception as exc:
            self.last_close_errors = (*self.last_close_errors, exc)


__all__ = [
    "AuthorizedFence",
    "ReplayHandoffFaultPoint",
    "ReplayHandoffResult",
    "ReplayHandoffRetry",
    "TerminalReplayHandoff",
    "TransitionWatchMultiplexer",
    "TransitionWatchSubscription",
]
