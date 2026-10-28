"""Shared user-Memory mutation boundary used by every storage package."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite


class MemoryWriteBlockedError(RuntimeError):
    """A write began before or during a personal-Memory deletion boundary."""

    def __init__(self, operation_id: str | None = None) -> None:
        super().__init__("personal Memory writes are temporarily blocked")
        self.operation_id = operation_id


@dataclass(frozen=True)
class MemoryMutationContext:
    started_at: str
    privacy_epoch: int | None = None


_MEMORY_MUTATION_CONTEXT: contextvars.ContextVar[MemoryMutationContext | None] = contextvars.ContextVar(
    "memory_mutation_context",
    default=None,
)


def start_memory_mutation_context() -> contextvars.Token[MemoryMutationContext | None]:
    """Capture the outer request/task boundary; child tasks inherit it."""

    return _MEMORY_MUTATION_CONTEXT.set(
        MemoryMutationContext(started_at=_now()),
    )


def reset_memory_mutation_context(
    token: contextvars.Token[MemoryMutationContext | None],
) -> None:
    _MEMORY_MUTATION_CONTEXT.reset(token)


def current_memory_mutation_context() -> MemoryMutationContext:
    return _MEMORY_MUTATION_CONTEXT.get() or MemoryMutationContext(
        started_at=_now(),
    )


async def ensure_memory_write_allowed(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    context: MemoryMutationContext | None = None,
) -> int:
    """Check barrier and late-write epoch inside the caller's write transaction."""

    if not await _table_exists(db, "memory_privacy_state"):
        return 0
    mutation = context or current_memory_mutation_context()
    async with db.execute(
        """
        SELECT current_epoch, barrier_status, barrier_operation_id,
               last_completed_delete_at
        FROM memory_privacy_state
        WHERE user_id=?
        """,
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return 0
    current_epoch = int(row[0])
    if str(row[1]) != "idle":
        raise MemoryWriteBlockedError(str(row[2]) if row[2] else None)
    if mutation.privacy_epoch is not None and mutation.privacy_epoch != current_epoch:
        raise MemoryWriteBlockedError
    completed_at = str(row[3]) if row[3] else None
    if completed_at and _as_datetime(mutation.started_at) <= _as_datetime(completed_at):
        raise MemoryWriteBlockedError
    return current_epoch


async def is_memory_read_hidden(
    db: aiosqlite.Connection,
    *,
    user_id: str,
) -> bool:
    """Fail closed while a full-user destructive operation owns the barrier."""

    if not await _table_exists(db, "memory_privacy_state"):
        return False
    async with db.execute(
        """
        SELECT barrier_status
        FROM memory_privacy_state
        WHERE user_id=?
        """,
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row is not None and str(row[0]) != "idle"


async def _table_exists(
    db: aiosqlite.Connection,
    table: str,
) -> bool:
    async with db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ) as cursor:
        return await cursor.fetchone() is not None


def _as_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="microseconds")
