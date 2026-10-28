"""Recoverable filesystem boundary for protected Skill deletion.

The caller verifies the impact token first, then moves every bundle into a
same-volume quarantine while its SQLite write transaction is still open.  A
failed transaction restores the bundles; a committed transaction records a
durable cleanup receipt before deleting the quarantined copies.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


@dataclass(frozen=True, slots=True)
class QuarantinedPath:
    cleanup_id: str
    original_path: Path
    quarantine_path: Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def quarantine_paths(
    paths: list[Path],
    *,
    root: Path,
    command_identity: str,
) -> list[QuarantinedPath]:
    """Move existing paths under ``root`` into a hidden same-volume directory."""

    resolved_root = root.resolve()
    quarantine_root = resolved_root / ".bowhead-quarantine"
    moved: list[QuarantinedPath] = []
    try:
        for index, raw_path in enumerate(paths):
            path = raw_path.resolve()
            if not path.exists():
                continue
            if path == resolved_root or not path.is_relative_to(resolved_root):
                raise ValueError("resource bundle path is outside its owner root")
            digest = hashlib.sha256(
                f"{command_identity}:{index}:{path}".encode(),
            ).hexdigest()[:24]
            cleanup_id = f"rfc_{digest}"
            destination = quarantine_root / f"{digest}-{path.name}"
            if destination.exists():
                raise FileExistsError("resource quarantine destination already exists")
            quarantine_root.mkdir(parents=True, exist_ok=True)
            path.rename(destination)
            moved.append(
                QuarantinedPath(
                    cleanup_id=cleanup_id,
                    original_path=path,
                    quarantine_path=destination,
                )
            )
    except Exception:
        restore_quarantined(moved)
        raise
    return moved


def restore_quarantined(entries: list[QuarantinedPath]) -> None:
    """Restore in reverse order so multi-directory transitions unwind safely."""

    errors: list[Exception] = []
    for entry in reversed(entries):
        if not entry.quarantine_path.exists():
            continue
        try:
            entry.original_path.parent.mkdir(parents=True, exist_ok=True)
            if entry.original_path.exists():
                raise FileExistsError("resource restore destination already exists")
            entry.quarantine_path.rename(entry.original_path)
        except Exception as exc:  # recovery must try every path before surfacing
            errors.append(exc)
    if errors:
        raise RuntimeError("resource bundle restore failed") from errors[0]


async def write_cleanup_receipts(
    db: aiosqlite.Connection,
    *,
    entries: list[QuarantinedPath],
    team_id: str,
    actor_user_id: str,
    idempotency_key: str,
    resource_kind: str,
    resource_id: str,
) -> None:
    now = _now()
    await db.executemany(
        "INSERT INTO resource_file_cleanup_receipt("
        "cleanup_id,team_id,actor_user_id,idempotency_key,resource_kind,resource_id,"
        "original_path,quarantine_path,state,error_code,created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                entry.cleanup_id,
                team_id,
                actor_user_id,
                idempotency_key,
                resource_kind,
                resource_id,
                str(entry.original_path),
                str(entry.quarantine_path),
                "pending",
                None,
                now,
                now,
            )
            for entry in entries
        ],
    )


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


async def finalize_cleanup(db_path: str, entries: list[QuarantinedPath]) -> None:
    """Best-effort post-commit cleanup; failures remain durable and retryable."""

    if not entries:
        return
    from bowhead_core._db import open_shared_ctx

    for entry in entries:
        error_code: str | None = None
        try:
            _remove_path(entry.quarantine_path)
        except OSError as exc:
            error_code = type(exc).__name__
        async with open_shared_ctx(db_path) as db:
            await db.execute(
                "UPDATE resource_file_cleanup_receipt SET state=?,error_code=?,updated_at=? WHERE cleanup_id=? AND state='pending'",
                (
                    "completed" if error_code is None else "pending",
                    error_code,
                    _now(),
                    entry.cleanup_id,
                ),
            )
            await db.commit()


async def retry_pending_cleanup(db_path: str, *, limit: int = 20) -> None:
    """Retry abandoned post-commit cleanup and prune completed receipts after 30 days."""

    from bowhead_core._db import open_shared_ctx

    async with open_shared_ctx(db_path) as db:
        async with db.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='resource_file_cleanup_receipt'",
        ) as cursor:
            if await cursor.fetchone() is None:
                return
        async with db.execute(
            "SELECT cleanup_id,original_path,quarantine_path FROM resource_file_cleanup_receipt WHERE state='pending' ORDER BY created_at LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    entries = [
        QuarantinedPath(
            cleanup_id=str(row[0]),
            original_path=Path(str(row[1])),
            quarantine_path=Path(str(row[2])),
        )
        for row in rows
    ]
    await finalize_cleanup(db_path, entries)
    cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    async with open_shared_ctx(db_path) as db:
        await db.execute(
            "DELETE FROM resource_change_receipt "
            "WHERE created_at<? AND NOT EXISTS("
            "SELECT 1 FROM resource_file_cleanup_receipt cleanup "
            "WHERE cleanup.team_id=resource_change_receipt.team_id "
            "AND cleanup.actor_user_id=resource_change_receipt.actor_user_id "
            "AND cleanup.idempotency_key=resource_change_receipt.idempotency_key "
            "AND cleanup.state='pending')",
            (cutoff,),
        )
        await db.commit()


__all__ = [
    "QuarantinedPath",
    "finalize_cleanup",
    "quarantine_paths",
    "restore_quarantined",
    "retry_pending_cleanup",
    "write_cleanup_receipts",
]
