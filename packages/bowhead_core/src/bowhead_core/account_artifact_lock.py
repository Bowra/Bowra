"""Coordinate account blob writers and erasers across SQLite instance processes."""

from __future__ import annotations

import fcntl
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from bowhead_core.account_deletion import AccountDeletionError


@asynccontextmanager
async def account_artifact_lock(db_path: str | Path, user_id: str, *, erasing: bool = False) -> AsyncIterator[None]:
    """Keep the lease through blob I/O; process death releases the OS lock.

    Hashing only makes a safe opaque filename; it grants no authorization.
    A busy eraser retries its durable operation instead of waiting on a writer.
    """
    root = Path(db_path).resolve().parent / ".account-artifact-locks"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity = hashlib.sha256(user_id.encode()).hexdigest()
    with (root / f"{identity}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, (fcntl.LOCK_EX if erasing else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AccountDeletionError("account_artifact_write_pending") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
