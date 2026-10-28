"""Full-byte source checks with bounded outstanding I/O, including timed-out work."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

import anyio

from bowhead_core.artifact_store import LocalBackend

if TYPE_CHECKING:
    from bowhead_core.artifact_store import ArtifactBackend, ObjectHead

# Keep the lease until the actual backend call closes its resources. Cancelling a
# request must not release capacity while a filesystem/boto thread still runs.
_PENDING: WeakKeyDictionary = WeakKeyDictionary()
MAX_SOURCE_IO = 4


async def bounded_source_io[T](operation: Callable[[], Awaitable[T]]) -> T:
    loop = asyncio.get_running_loop()
    pending = _PENDING.setdefault(loop, set())
    if len(pending) >= MAX_SOURCE_IO:
        raise TimeoutError("source I/O capacity exhausted")
    task = loop.create_task(operation())
    pending.add(task)

    def completed(done):
        pending.discard(done)
        if not done.cancelled():
            done.exception()  # Consume failures even after the caller timed out.

    task.add_done_callback(completed)
    return await asyncio.shield(task)


def hash_path(path: str | Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def source_head(backend: ArtifactBackend, path: str) -> tuple[str, ObjectHead | None]:
    async def inspect():
        head = await backend.head(path)
        if head is None and isinstance(backend, LocalBackend) and not Path(path).is_absolute():
            alternate = str(Path(".data") / path)
            other = await backend.head(alternate)
            if other is not None:
                return alternate, other
        return path, head

    return await bounded_source_io(inspect)


async def full_source_hash(backend: ArtifactBackend, path: str) -> str:
    async def read():
        async with backend.read_stream(path) as stream:
            return await anyio.to_thread.run_sync(lambda: hashlib.file_digest(stream, "sha256").hexdigest())

    return await bounded_source_io(read)
