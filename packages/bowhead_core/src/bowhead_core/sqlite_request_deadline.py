"""The 30-second total deadline of a save a person triggers.

``repeat_when_authority_busy(kind="request")`` opens a deadline for the decorated method. Every
connection the method opens reads it: ``diagnostic_connect`` passes the time left as its busy
timeout and ``open_shared_ctx`` lowers a borrowed pooled connection to it, so the writer wait
inside an attempt ends with the deadline too, not only the authority-lease repeats. Background and
Turn-internal writes open no deadline and keep their connections' own waits. See
``docs/sprints/request-write-deadline-2026-11-17/tech-design.md``.

This module sits below ``_db`` and ``sqlite_domain_runtime`` so both can import it.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import time
from collections.abc import Iterator

# (owning task, monotonic deadline). A task spawned inside a request write copies the context; the
# owner check keeps it from inheriting the deadline, which would be spent once the write returns.
_DEADLINE: contextvars.ContextVar[tuple[asyncio.Task | None, float] | None] = contextvars.ContextVar("bowhead_request_write_deadline", default=None)


def _current_task() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


@contextlib.contextmanager
def request_write_deadline(seconds: float) -> Iterator[float]:
    """Open a deadline ``seconds`` from now; a nested request write keeps the earlier one."""
    task = _current_task()
    deadline = time.monotonic() + seconds
    current = _DEADLINE.get()
    if current is not None and current[0] is task:
        deadline = min(deadline, current[1])
    token = _DEADLINE.set((task, deadline))
    try:
        yield deadline
    finally:
        _DEADLINE.reset(token)


def remaining_ms() -> int | None:
    """Milliseconds left of this task's request-write deadline, or ``None`` outside one."""
    current = _DEADLINE.get()
    if current is None or current[0] is not _current_task():
        return None
    return max(0, int((current[1] - time.monotonic()) * 1000))
