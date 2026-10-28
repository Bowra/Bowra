"""Bounded concurrent checks that fail like the sequential loop they replace."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable


async def run_bounded(
    items: Iterable[str],
    check: Callable[[str], Awaitable[None]],
    *,
    limit: int,
) -> None:
    """Run ``check(item)`` for every item, at most ``limit`` at a time.

    The first exception any check raises is re-raised as itself after all
    checks settle. Letting it escape inside the task group would wrap it in an
    ``ExceptionGroup``, which no FastAPI handler maps: an ``HTTPException(503)``
    or ``(401)`` would reach the client as a bare 500.
    """

    limiter = anyio.CapacityLimiter(limit)
    failures: list[Exception] = []

    async def _run(item: str) -> None:
        async with limiter:
            if failures:
                return
            try:
                await check(item)
            except Exception as exc:  # noqa: BLE001 - re-raised unwrapped below
                failures.append(exc)

    async with anyio.create_task_group() as tg:
        for item in items:
            tg.start_soon(_run, item)
    if failures:
        raise failures[0]
