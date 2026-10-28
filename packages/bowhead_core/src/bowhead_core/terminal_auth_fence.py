"""Terminal Protocol v1 单实例 auth revoke / frame enqueue 线性化 seam。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from bowhead_core.terminal_protocol import (
    AuthFenceFrameContext,
    AuthFenceResolveContext,
    AuthFenceScope,
    FrameSendCertificate,
    canonical_digest_v1,
    canonical_json_v1,
)
from bowhead_core.terminal_protocol_store import TerminalProtocolStore, TerminalReadContext


class AuthFenceFaultPoint(StrEnum):
    TRANSITION_WATCH_AFTER_AUTH_CHECK = "TRANSITION_WATCH_AFTER_AUTH_CHECK"
    AUTH_FENCE_BEFORE_FRAME_ENQUEUE = "AUTH_FENCE_BEFORE_FRAME_ENQUEUE"
    AUTH_FENCE_AFTER_REVOKE_COMMIT = "AUTH_FENCE_AFTER_REVOKE_COMMIT"


@dataclass(frozen=True, slots=True)
class AuthorizedFrame:
    payload_json: str
    send_certificate: FrameSendCertificate


@dataclass(slots=True)
class _ScopeLockRecord:
    lock: asyncio.Lock
    users: int = 0


class TerminalAuthFenceCoordinator:
    """scope locks → durable vector/F → full auth → bounded enqueue。"""

    def __init__(
        self,
        store: TerminalProtocolStore,
        *,
        _clock: Callable[[], datetime] | None = None,
        _fault_injector: Callable[[AuthFenceFaultPoint], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._clock = _clock or (lambda: datetime.now(UTC))
        self._fault_injector = _fault_injector
        self._scope_locks: dict[AuthFenceScope, _ScopeLockRecord] = {}
        self._scope_locks_guard = asyncio.Lock()

    async def authorize_and_enqueue_frame(
        self,
        context: AuthFenceFrameContext,
        *,
        resolve_context: AuthFenceResolveContext,
        frame_payload: Mapping[str, object],
        outbound_queue: asyncio.Queue[AuthorizedFrame],
        authorize: Callable[[AuthFenceResolveContext], Awaitable[bool]],
        close_subscriber: Callable[[], None],
    ) -> FrameSendCertificate | None:
        if outbound_queue.maxsize <= 0:
            raise ValueError("terminal outbound queue must be bounded")
        if (
            context.scope.team_id != resolve_context.team_id
            or context.scope.session_id != resolve_context.session_id
            or context.scope.turn_id != resolve_context.resource_ref
        ):
            raise ValueError("auth frame and resolver identity mismatch")
        expected_scopes = tuple(entry.scope for entry in context.expected_auth_fence_vector.entries)
        if set(expected_scopes) != set(resolve_context.required_scopes):
            raise ValueError("auth frame and resolver scope vector mismatch")
        payload_json = canonical_json_v1(frame_payload)
        if canonical_digest_v1(frame_payload) != context.frame_digest:
            raise ValueError("auth frame digest does not match payload")
        close_requested = False

        def close_once() -> None:
            nonlocal close_requested
            if close_requested:
                return
            close_requested = True
            close_subscriber()

        locks = await self._acquire_scope_locks(expected_scopes)
        try:
            current_vector = await self._store.read_auth_fence_vector(resolve_context)
            read_context = TerminalReadContext(
                team_id=context.scope.team_id,
                session_id=context.scope.session_id,
                turn_id=context.scope.turn_id,
                viewer=resolve_context.viewer,
                auth_fence_vector_digest=current_vector.auth_fence_vector_digest,
            )
            pointer = (await self._store.read_generation_pointers((read_context,)))[context.scope.turn_id]
            authorized = await authorize(resolve_context)
            if not isinstance(authorized, bool):
                raise TypeError("auth frame authorization result must be boolean")
            await self._inject(AuthFenceFaultPoint.TRANSITION_WATCH_AFTER_AUTH_CHECK)
            if (
                not authorized
                or current_vector != context.expected_auth_fence_vector
                or pointer.generation != context.scope.generation
                or pointer.delivery_fence_epoch != context.expected_delivery_fence_epoch
            ):
                close_once()
                return None
            await self._inject(AuthFenceFaultPoint.AUTH_FENCE_BEFORE_FRAME_ENQUEUE)
            certificate: FrameSendCertificate | None = None

            def enqueue() -> bool:
                nonlocal certificate
                candidate = FrameSendCertificate(
                    linearized_at=self._now_text(),
                    auth_fence_vector_digest=current_vector.auth_fence_vector_digest,
                    delivery_fence_epoch=pointer.delivery_fence_epoch,
                    frame_digest=context.frame_digest,
                )
                try:
                    outbound_queue.put_nowait(
                        AuthorizedFrame(
                            payload_json=payload_json,
                            send_certificate=candidate,
                        )
                    )
                except asyncio.QueueFull:
                    return False
                certificate = candidate
                return True

            enqueued = await self._store.linearize_frame_enqueue(
                read_context,
                resolve_context=resolve_context,
                expected_pointer=pointer,
                expected_auth_fence_vector=current_vector,
                enqueue=enqueue,
            )
            if not enqueued or certificate is None:
                close_once()
                return None
            return certificate
        except BaseException:
            close_once()
            raise
        finally:
            await self._release_scope_locks(locks)

    async def run_auth_mutation(
        self,
        scopes: tuple[AuthFenceScope, ...],
        mutation: Callable[[], Awaitable[None]],
    ) -> None:
        """S4 mutation adapter 的锁边界；callback 必须原子提交 ACL + epoch bump。"""

        locks = await self._acquire_scope_locks(scopes)
        try:
            await mutation()
            await self._inject(AuthFenceFaultPoint.AUTH_FENCE_AFTER_REVOKE_COMMIT)
        finally:
            await self._release_scope_locks(locks)

    async def _acquire_scope_locks(
        self,
        scopes: tuple[AuthFenceScope, ...],
    ) -> tuple[tuple[AuthFenceScope, _ScopeLockRecord], ...]:
        if not scopes or any(not isinstance(scope, AuthFenceScope) for scope in scopes):
            raise ValueError("auth fence coordinator requires typed scopes")
        ordered = tuple(sorted(set(scopes), key=AuthFenceScope.sort_key))
        async with self._scope_locks_guard:
            records: list[tuple[AuthFenceScope, _ScopeLockRecord]] = []
            for scope in ordered:
                record = self._scope_locks.setdefault(scope, _ScopeLockRecord(asyncio.Lock()))
                record.users += 1
                records.append((scope, record))
        acquired: list[tuple[AuthFenceScope, _ScopeLockRecord]] = []
        try:
            for item in records:
                await item[1].lock.acquire()
                acquired.append(item)
        except BaseException:
            for _scope, record in reversed(acquired):
                record.lock.release()
            await self._drop_scope_lock_reservations(tuple(records))
            raise
        return tuple(records)

    async def _release_scope_locks(
        self,
        records: tuple[tuple[AuthFenceScope, _ScopeLockRecord], ...],
    ) -> None:
        for _scope, record in reversed(records):
            record.lock.release()
        await self._drop_scope_lock_reservations(records)

    async def _drop_scope_lock_reservations(
        self,
        records: tuple[tuple[AuthFenceScope, _ScopeLockRecord], ...],
    ) -> None:
        async with self._scope_locks_guard:
            for scope, record in records:
                record.users -= 1
                if record.users == 0 and self._scope_locks.get(scope) is record:
                    self._scope_locks.pop(scope, None)

    async def _inject(self, point: AuthFenceFaultPoint) -> None:
        if self._fault_injector is not None:
            await self._fault_injector(point)

    def _now_text(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("auth fence clock must be timezone-aware")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["AuthFenceFaultPoint", "AuthorizedFrame", "TerminalAuthFenceCoordinator"]
