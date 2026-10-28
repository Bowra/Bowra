"""Bounded in-memory buffer with a background batch flush, shared by Gateway recorders.

Producers run on the LLM call path, so `enqueue` must stay O(1): it never awaits,
locks or raises. A background loop writes batches; overflow drops the oldest entry
and counts it, and shutdown flushes once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque

log = logging.getLogger(__name__)


class BufferedFlushLoop[T]:
    """Subclasses implement `_write_batch`; everything else is shared."""

    #: Name used in log lines, e.g. ``llm_usage_recorder``.
    log_name = "buffered_flush"
    #: Requeue a batch whose write failed (usage metering) or drop it (diagnostics).
    requeue_on_failure = True

    def __init__(self, *, interval_s: float, max_buffer: int, flush_batch: int) -> None:
        self._interval_s = max(1.0, interval_s)
        # maxlen drops the oldest entry on overflow without manual trimming.
        self._buf: deque[T] = deque(maxlen=max(100, max_buffer))
        self._max_buffer = self._buf.maxlen or 0
        self._flush_batch = flush_batch
        self._dropped = 0
        self._written = 0
        self._task: asyncio.Task[None] | None = None

    @property
    def stats(self) -> dict[str, int]:
        return {"buffered": len(self._buf), "written": self._written, "dropped": self._dropped}

    def _enqueue(self, item: T) -> None:
        if len(self._buf) >= self._max_buffer:
            # The deque evicts the oldest entry; count it and sample the warning.
            self._dropped += 1
            self._on_drop(1)
            if self._dropped % 1000 == 1:
                log.warning("%s.buffer_full dropped_total=%d cap=%d", self.log_name, self._dropped, self._max_buffer)
        self._buf.append(item)

    def _on_drop(self, count: int) -> None:
        """Hook for a drop metric; must not raise."""

    async def _write_batch(self, batch: list[T]) -> int:
        raise NotImplementedError

    async def _before_flush(self) -> None:
        """Hook run on each tick before flushing, e.g. a retention purge."""

    async def flush_once(self) -> int:
        if not self._buf:
            return 0
        batch: list[T] = []
        while self._buf and len(batch) < self._flush_batch:
            batch.append(self._buf.popleft())
        try:
            written = await self._write_batch(batch)
        except Exception as e:  # noqa: BLE001 — a write failure must not kill the flush loop.
            if self.requeue_on_failure:
                # extendleft evicts the newest entries when the buffer filled meanwhile; count them.
                room = self._max_buffer - len(self._buf)
                if len(batch) > room:
                    self._dropped += len(batch) - room
                    self._on_drop(len(batch) - room)
                self._buf.extendleft(reversed(batch))
            else:
                self._dropped += len(batch)
                self._on_drop(len(batch))
            log.warning("%s.flush_failed n=%d err=%s", self.log_name, len(batch), e)
            return 0
        self._written += written
        return written

    async def run(self) -> None:
        """Flush on every tick; on cancellation flush what is buffered, then stop."""
        log.info("%s.started interval_s=%.1f cap=%d", self.log_name, self._interval_s, self._max_buffer)
        try:
            while True:
                await asyncio.sleep(self._interval_s)
                with contextlib.suppress(Exception):
                    await self._before_flush()
                while await self.flush_once() >= self._flush_batch:
                    pass  # Drain a backlog without waiting for the next tick.
        except asyncio.CancelledError:
            log.info("%s.cancelled buffered=%d", self.log_name, len(self._buf))
            with contextlib.suppress(Exception):
                while await self.flush_once():
                    pass
            log.info("%s.stopped written=%d dropped=%d", self.log_name, self._written, self._dropped)
            raise

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
