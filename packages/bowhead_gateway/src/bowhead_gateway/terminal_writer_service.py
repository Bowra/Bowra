"""Single-active Terminal Protocol writer lease service."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from bowhead_core.terminal_protocol import RecoveryWriterFence, TerminalWriterLease
from bowhead_core.terminal_runtime_factory import TerminalWriterSnapshot


class TerminalWriterLeaseService:
    """Hold and renew one open or recovery-only lease until shutdown."""

    def __init__(
        self,
        *,
        store: Any,
        owner_instance_id: str,
        rollout_config_generation: str,
        fresh_admission_enabled: bool,
        active_runtime_count: Callable[[], int] | None = None,
        heartbeat_interval_seconds: float = 10.0,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("terminal writer heartbeat interval is invalid")
        self._store = store
        self._owner = owner_instance_id
        self._rollout = rollout_config_generation
        self._fresh_admission_enabled = fresh_admission_enabled
        self._active_runtime_count = active_runtime_count
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._fence: RecoveryWriterFence | TerminalWriterLease | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._lost_error: Exception | None = None
        self._worker_tasks: set[asyncio.Task[object]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._startup_recovery_ready = False
        self._draining = False

    @property
    def fence(self) -> RecoveryWriterFence | TerminalWriterLease | None:
        return self._fence

    @property
    def ready(self) -> bool:
        return self._lost_error is None and self._fence_is_unexpired()

    @property
    def startup_recovery_ready(self) -> bool:
        return self._startup_recovery_ready

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def fresh_admission_ready(self) -> bool:
        fence = self._fence
        return bool(
            self._fresh_admission_enabled
            and self._startup_recovery_ready
            and not self._draining
            and self._lost_error is None
            and isinstance(fence, TerminalWriterLease)
            and fence.admission_state == "open"
            and self._fence_is_unexpired()
        )

    def mark_recovery_pending(self) -> None:
        self._startup_recovery_ready = False

    def mark_recovery_ready(self) -> bool:
        fence = self._fence
        ready = bool(
            self._fresh_admission_enabled
            and not self._draining
            and self._lost_error is None
            and isinstance(fence, TerminalWriterLease)
            and fence.admission_state == "open"
            and self._fence_is_unexpired()
        )
        self._startup_recovery_ready = ready
        return ready

    async def start(self) -> bool:
        async with self._lifecycle_lock:
            if self._draining:
                return False
            if self._fence is not None:
                if self._lost_error is None and self._fence_is_unexpired():
                    return True
                if self._lost_error is None:
                    self._lost_error = RuntimeError("terminal writer lease expired")
                await self._abandon_current_fence()
            self._startup_recovery_ready = False
            self._lost_error = None
            if self._fresh_admission_enabled:
                self._fence = await self._store.acquire_terminal_writer_lease(
                    owner_instance_id=self._owner,
                    rollout_config_generation=self._rollout,
                )
            else:
                self._fence = await self._store.acquire_recovery_writer_fence(
                    owner_instance_id=self._owner,
                    rollout_config_generation=self._rollout,
                )
            if self._fence is None:
                return False
            if not self._fence_is_unexpired():
                self._lost_error = RuntimeError("terminal writer lease expired")
                await self._abandon_current_fence()
                return False
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(),
                name="terminal-writer-heartbeat",
            )
            return True

    async def ensure_writer_ready(self) -> bool:
        """Keep existing/control work available while a valid lease drains."""

        if self.ready:
            return True
        return await self.start()

    async def _abandon_current_fence(self) -> None:
        """Drop a locally lost capability without pretending release succeeded."""

        heartbeat = self._heartbeat_task
        self._heartbeat_task = None
        if heartbeat is not None and heartbeat is not asyncio.current_task():
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        self._fence = None
        self._startup_recovery_ready = False

    def observe_worker_task(self, task: asyncio.Task[object]) -> None:
        """Keep a recovery-only fence alive until its last spawned worker ends."""

        self._worker_tasks.add(task)

        def done(completed: asyncio.Task[object]) -> None:
            self._worker_tasks.discard(completed)
            if not self._fresh_admission_enabled:
                asyncio.create_task(
                    self.release_recovery_if_idle(),
                    name="terminal-recovery-writer-release",
                )

        task.add_done_callback(done)

    async def release_recovery_if_idle(self) -> bool:
        """Release OFF-mode ownership once no local recovery worker needs it."""

        if self._fresh_admission_enabled or self._worker_tasks or self._has_active_runtime():
            return False
        async with self._lifecycle_lock:
            if self._worker_tasks or self._has_active_runtime() or self._fence is None:
                return False
            await self._release_current_fence()
            return True

    def _has_active_runtime(self) -> bool:
        if self._active_runtime_count is None:
            return False
        try:
            return self._active_runtime_count() > 0
        except Exception:
            return True

    def writer_snapshot(self) -> TerminalWriterSnapshot:
        fence = self._fence
        if fence is None or self._lost_error is not None or not self._fence_is_unexpired():
            raise RuntimeError("terminal_protocol_writer_not_ready")
        return TerminalWriterSnapshot(
            instance_id=fence.owner_instance_id,
            lease_epoch=fence.writer_lease_epoch,
            admission_epoch=fence.writer_admission_epoch,
            rollout_config_generation=fence.rollout_config_generation,
            recovery_only=isinstance(fence, RecoveryWriterFence),
        )

    async def _heartbeat_loop(self) -> None:
        try:
            while self._fence is not None:
                await asyncio.sleep(self._heartbeat_interval_seconds)
                async with self._lifecycle_lock:
                    fence = self._fence
                    if fence is None:
                        return
                    if isinstance(fence, RecoveryWriterFence):
                        self._fence = await self._store.heartbeat_recovery_writer_fence(fence)
                    else:
                        self._fence = await self._store.heartbeat_terminal_writer_lease(fence)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._lost_error = exc
            self._startup_recovery_ready = False

    def _fence_is_unexpired(self) -> bool:
        fence = self._fence
        if fence is None:
            return False
        try:
            expires_at = datetime.fromisoformat(
                fence.lease_expires_at.replace("Z", "+00:00"),
            )
        except (AttributeError, ValueError):
            return False
        return bool(expires_at.tzinfo is not None and expires_at.astimezone(UTC) > datetime.now(tz=UTC))

    async def begin_drain(self) -> None:
        """Stop fresh admission before any other shutdown cleanup begins."""

        async with self._lifecycle_lock:
            self._draining = True
            self._startup_recovery_ready = False
            fence = self._fence
            if isinstance(fence, TerminalWriterLease) and fence.admission_state == "open" and self._lost_error is None:
                try:
                    self._fence = await self._store.drain_terminal_writer_lease(fence)
                except Exception as exc:
                    self._lost_error = exc
                    raise

    async def wait_for_workers(self, *, timeout_seconds: float = 30.0) -> bool:
        """Wait boundedly for all workers observed before or during drain."""

        if timeout_seconds < 0:
            raise ValueError("terminal worker drain timeout is invalid")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            pending = tuple(task for task in self._worker_tasks if not task.done())
            remaining = deadline - loop.time()
            if not pending:
                return True
            if remaining <= 0:
                return False
            await asyncio.wait(pending, timeout=remaining)

    async def close(self, *, worker_timeout_seconds: float = 30.0) -> None:
        if worker_timeout_seconds < 0:
            raise ValueError("terminal worker drain timeout is invalid")
        await self.begin_drain()
        await self.wait_for_workers(timeout_seconds=worker_timeout_seconds)
        async with self._lifecycle_lock:
            await self._release_current_fence()

    async def _release_current_fence(self) -> None:
        heartbeat = self._heartbeat_task
        self._heartbeat_task = None
        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        fence = self._fence
        self._fence = None
        if fence is None:
            return
        if isinstance(fence, RecoveryWriterFence):
            await self._store.release_recovery_writer_fence(fence)
            return
        if fence.admission_state == "open":
            fence = await self._store.drain_terminal_writer_lease(fence)
        await self._store.release_terminal_writer_lease(fence)


__all__ = ["TerminalWriterLeaseService"]
