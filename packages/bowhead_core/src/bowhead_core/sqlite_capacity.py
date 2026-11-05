"""Process-wide admission for physical SQLite connections."""

from __future__ import annotations

import asyncio
import logging
import resource
import threading
import time
import traceback
import weakref
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from contextvars import Context, ContextVar
from pathlib import Path
from typing import Any

import anyio

_log = logging.getLogger(__name__)
_critical = ContextVar("sqlite_completion_capacity", default=False)
_lock = threading.Lock()
_used = 0
_holders: dict[int, ConnectionSlot] = {}
_last_holder_report = 0.0


def await_locations(task: asyncio.Task | None) -> list[str]:
    """Read bounded suspended code locations, never task names, locals or values."""
    if task is None:
        return []
    try:
        current = asyncio.current_task()
    except RuntimeError:
        return []
    if task is current:
        return [f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in traceback.extract_stack(limit=32)]
    locations = []
    pending = task.get_coro()
    for _ in range(32):
        frame = getattr(pending, "cr_frame", None) or getattr(pending, "gi_frame", None) or getattr(pending, "ag_frame", None)
        if frame is not None:
            locations.append(f"{Path(frame.f_code.co_filename).name}:{frame.f_lineno}:{frame.f_code.co_name}")
        pending = getattr(pending, "cr_await", None) or getattr(pending, "gi_yieldfrom", None) or getattr(pending, "ag_await", None)
        if pending is None:
            break
    return locations


def _connection_limit() -> int:
    # One main and up to five attached databases each hold a DB and WAL descriptor.
    # A connection can also hold one gate per registered domain. Shared WAL indexes,
    # cached Unix file descriptors, sockets and other users draw from the headroom.
    from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES

    connection_cost = 2 * 6 + len(DOMAIN_TARGET_FILES)
    try:
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        soft = 256
    if soft == resource.RLIM_INFINITY:
        return 128
    return max(0, min(128, (soft - max(64, soft // 4)) // connection_cost))


LIMIT = _connection_limit()
RESERVE = min(4, LIMIT // 4)


class SQLiteCapacityError(RuntimeError):
    """No physical connection slot is available for this operation."""


@asynccontextmanager
async def completion_capacity() -> AsyncIterator[None]:
    token = _critical.set(True)
    try:
        yield
    finally:
        _critical.reset(token)


def copy_completion_capacity(context: Context) -> None:
    context.run(_critical.set, _critical.get())


class ConnectionSlot:
    """One reservation, transferred to the native connection until close."""

    def __init__(self) -> None:
        self.held = False
        self.reserve_only = False
        self.phase = "opening"
        self.since = 0.0
        self.owner: weakref.ReferenceType[asyncio.Task] | None = None

    def track(self, phase: str) -> None:
        """Observe ownership without retaining the borrower or its connection."""
        self.phase = phase
        self.since = time.monotonic()
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        self.owner = weakref.ref(task) if task is not None and phase != "idle" else None

    def acquire(self, *, report: bool = True) -> None:
        global _used, _last_holder_report
        refused = False
        holders = []
        with _lock:
            ordinary = LIMIT - RESERVE
            if _used >= (LIMIT if _critical.get() else ordinary):
                if report:
                    _log.warning("SQLite connection capacity exhausted (used=%d, limit=%d)", _used, LIMIT)
                refused = True
                now = time.monotonic()
                if report and now - _last_holder_report >= 5:
                    _last_holder_report = now
                    holders = [(id(slot), slot.phase, slot.since, slot.owner) for slot in _holders.values()]
            else:
                self.reserve_only = _used >= ordinary
                _used += 1
                self.held = True
                self.track("opening")
                _holders[id(self)] = self
        if refused:
            # Task frames are inspected outside the admission lock, on the event loop.
            for identity, phase, since, owner in holders:
                task = owner() if owner is not None else None
                _log.warning(
                    "sqlite.capacity_holder slot=%x phase=%s age_ms=%d awaiting=%s",
                    identity,
                    phase,
                    round((time.monotonic() - since) * 1000),
                    await_locations(task) if task is not None else [],
                )
            raise SQLiteCapacityError("SQLite connection capacity exhausted. Check the saved operation status before retrying.")

    async def acquire_async(self) -> None:
        """Wait for short connection turnover before opening, never inside owned work."""
        deadline = time.monotonic() + 5
        task = asyncio.current_task()
        while True:
            try:
                self.acquire(report=False)
                return
            except SQLiteCapacityError:
                with _lock:
                    nested = any(slot.owner is not None and slot.owner() is task for slot in _holders.values())
                # Waiting while holding a connection could deadlock nested writers.
                if nested or time.monotonic() >= deadline:
                    self.acquire()
                    return
                await asyncio.sleep(0.01)

    def release(self) -> None:
        global _used
        with _lock:
            if self.held:
                self.held = False
                _used -= 1
                _holders.pop(id(self), None)


async def finish_cleanup[T](operation: Coroutine[Any, Any, T]) -> T:
    """Finish owned cleanup before propagating a deadline or repeated task cancellation."""
    task = asyncio.create_task(operation)
    cancelled = None
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc
    result = task.result()
    if cancelled is not None:
        raise cancelled
    await anyio.lowlevel.checkpoint_if_cancelled()
    return result
