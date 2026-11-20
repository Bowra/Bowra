"""Count how pooled SQLite connections are opened, reused and closed, per pool and database file.

``_db._borrow`` keeps a few idle connections per file (``open_pooled_read_ctx``: 8, ``open_shared_ctx``:
32) and at most ``sqlite_capacity.combined_idle_cap()`` idle connections in all. A borrower beyond them
opens a new connection, which loads the schema, runs its setup pragmas and starts a worker thread, and the
connection is closed on return. Nothing showed how often that happened per pool, so whether an idle limit
should grow could not be decided (read-only hot reads design, section 6). The pool calls the ``note_*``
functions here; they only update counters under a short lock and never touch SQLite.

Event, value-free:

- ``sqlite.pool_summary``: one line per pool (``read`` or ``shared``) and database file name for a window of
  :data:`SUMMARY_WINDOW_S`, with ``opened``, ``reused``, ``closed_per_file_cap``, ``closed_combined_cap``,
  ``closed_reserve`` (a completion-reserve slot is never kept), ``closed_other`` (the block failed, the
  connection was not reusable, the file was replaced or the pools were reset), that total split as
  ``other_failed_block``, ``other_not_reusable``, ``other_held_lease`` (a connection still holding a cross-file
  authority lease or read state after its release check), ``other_file_replaced`` and ``other_pools_reset``, and
  ``peak_borrowed``. A
  window is written by the first pool event after it ends, or by :func:`flush_summary` (the gateway calls it
  at shutdown); a quiet period writes nothing, and ``window_ended`` is at most :data:`SUMMARY_WINDOW_S` after
  ``window_started``.
"""

from __future__ import annotations

import contextvars
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePath

import structlog

log = structlog.get_logger(__name__)

#: Length of one summary window.
SUMMARY_WINDOW_S = 60.0

_SAFE_VERSION = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_CLOSE_REASONS = ("closed_per_file_cap", "closed_combined_cap", "closed_reserve", "closed_other")
#: Which release check closed a ``closed_other`` return.
_OTHER_REASONS = ("failed_block", "not_reusable", "held_lease", "file_replaced", "pools_reset")


@dataclass(slots=True)
class _Counts:
    opened: int = 0
    reused: int = 0
    closed_per_file_cap: int = 0
    closed_combined_cap: int = 0
    closed_reserve: int = 0
    closed_other: int = 0
    other_failed_block: int = 0
    other_not_reusable: int = 0
    other_held_lease: int = 0
    other_file_replaced: int = 0
    other_pools_reset: int = 0
    peak_borrowed: int = 0
    max_idle: int = 0


_lock = threading.Lock()
_window_started: float | None = None
_window_started_at: datetime | None = None
_counts: dict[tuple[str, str], _Counts] = {}
# Connections currently borrowed, per pool and file. Kept across windows: a window's peak starts from it.
_borrowed: dict[tuple[str, str], int] = {}


# Database file names by path. A process opens a handful of database files, so the map stays small; it
# saves building a path object on every borrow and return.
_names: dict[str, str] = {}


def _key(pool: str, path: str) -> tuple[str, str]:
    name = _names.get(path)
    if name is None:
        name = PurePath(path).name
        if len(_names) < 256:
            _names[path] = name
    return pool, name


def note_borrow(pool: str, path: str, *, opened: bool, max_idle: int) -> None:
    """A connection of ``pool`` for ``path`` was handed out: newly ``opened`` or taken from idle."""
    try:
        key = _key(pool, path)
        ready = None
        with _lock:
            ready = _roll(time.monotonic())
            counts = _counts.get(key)
            if counts is None:
                counts = _counts[key] = _Counts()
            if opened:
                counts.opened += 1
            else:
                counts.reused += 1
            counts.max_idle = max_idle
            borrowed = _borrowed.get(key, 0) + 1
            _borrowed[key] = borrowed
            counts.peak_borrowed = max(counts.peak_borrowed, borrowed)
        if ready is not None:
            _write_summary(*ready)
    except Exception:  # noqa: S110 -- Counting must never break a borrow.
        pass


def note_return(pool: str, path: str, *, closed_reason: str | None, other_reason: str | None = None) -> None:
    """A borrowed connection came back; ``closed_reason`` says why it was closed, or None when kept idle.

    For ``closed_other``, ``other_reason`` names the failed release check (one of ``_OTHER_REASONS``).
    """
    try:
        key = _key(pool, path)
        ready = None
        with _lock:
            ready = _roll(time.monotonic())
            counts = _counts.get(key)
            if counts is None:
                counts = _counts[key] = _Counts()
            _borrowed[key] = max(0, _borrowed.get(key, 0) - 1)
            if closed_reason in _CLOSE_REASONS:
                setattr(counts, closed_reason, getattr(counts, closed_reason) + 1)
            if closed_reason == "closed_other" and other_reason in _OTHER_REASONS:
                field = f"other_{other_reason}"
                setattr(counts, field, getattr(counts, field) + 1)
        if ready is not None:
            _write_summary(*ready)
    except Exception:  # noqa: S110 -- Counting must never break a release.
        pass


def _roll(now: float) -> tuple[datetime, float, dict[tuple[str, str], _Counts]] | None:
    """Start a window if none is open and detach the current one once it has ended; the caller holds ``_lock``."""
    global _window_started, _window_started_at
    ready = None
    if _window_started is not None and now - _window_started >= SUMMARY_WINDOW_S:
        ready = _take_window(now)
    if _window_started is None:
        _window_started = now
        _window_started_at = datetime.now(UTC)
    return ready


def _take_window(now: float) -> tuple[datetime, float, dict[tuple[str, str], _Counts]] | None:
    """Detach the current window; the caller holds ``_lock``."""
    global _window_started, _window_started_at, _counts
    if _window_started is None or _window_started_at is None:
        return None
    # A window closed by an event long after it ended still covers only its own span, not the quiet gap.
    window = (_window_started_at, min(now, _window_started + SUMMARY_WINDOW_S) - _window_started, _counts)
    _window_started, _window_started_at, _counts = None, None, {}
    return window


def flush_summary() -> None:
    """Write the current window now, for shutdown and tests."""
    with _lock:
        ready = _take_window(time.monotonic())
    if ready is not None:
        _write_summary(*ready)


def _write_summary(started_at: datetime, seconds: float, counts: dict[tuple[str, str], _Counts]) -> None:
    try:
        from bowhead_core import sqlite_capacity

        window = {
            "window_started": started_at.isoformat(),
            "window_ended": (started_at + timedelta(seconds=seconds)).isoformat(),
            "window_seconds": round(seconds, 3),
            "combined_idle_cap": sqlite_capacity.combined_idle_cap(),
            "app_version": _app_version(),
        }
        for (pool, database), value in sorted(counts.items()):
            contextvars.Context().run(
                log.info,
                "sqlite.pool_summary",
                pool=pool,
                database=database,
                opened=value.opened,
                reused=value.reused,
                closed_per_file_cap=value.closed_per_file_cap,
                closed_combined_cap=value.closed_combined_cap,
                closed_reserve=value.closed_reserve,
                closed_other=value.closed_other,
                other_failed_block=value.other_failed_block,
                other_not_reusable=value.other_not_reusable,
                other_held_lease=value.other_held_lease,
                other_file_replaced=value.other_file_replaced,
                other_pools_reset=value.other_pools_reset,
                peak_borrowed=value.peak_borrowed,
                max_idle=value.max_idle,
                **window,
            )
    except Exception:  # noqa: S110 -- A failing log sink must not break the pool.
        pass


def _reset_for_test() -> None:
    global _window_started, _window_started_at, _counts
    with _lock:
        _window_started, _window_started_at, _counts = None, None, {}
        _borrowed.clear()


def _app_version() -> str:
    version = os.environ.get("BOWHEAD_VERSION", "")
    return version if _SAFE_VERSION.fullmatch(version) else "unavailable"
