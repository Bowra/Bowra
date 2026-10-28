"""Privacy boundary and durable destructive operations for personal Memory."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import aiosqlite
from bowhead_memory.privacy_guard import (
    MemoryMutationContext,
    MemoryWriteBlockedError,
    current_memory_mutation_context,
    ensure_memory_write_allowed,
    is_memory_read_hidden,
    reset_memory_mutation_context,
    start_memory_mutation_context,
)

from bowhead_core._db import open_shared_ctx

logger = logging.getLogger(__name__)

_SHARED_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_privacy_state (
    user_id                    TEXT PRIMARY KEY,
    current_epoch              INTEGER NOT NULL DEFAULT 0,
    barrier_status             TEXT NOT NULL DEFAULT 'idle',
    barrier_operation_id       TEXT,
    barrier_started_at         TEXT,
    last_completed_delete_at   TEXT,
    updated_at                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_resource_deletion_tombstones (
    user_id                  TEXT NOT NULL,
    resource_type            TEXT NOT NULL,
    resource_id              TEXT NOT NULL,
    deleted_through_version  INTEGER NOT NULL,
    deleted_at               TEXT NOT NULL,
    PRIMARY KEY(user_id, resource_type, resource_id)
);

CREATE TABLE IF NOT EXISTS memory_destructive_operations (
    id                    TEXT PRIMARY KEY,
    user_id               TEXT NOT NULL,
    idempotency_key       TEXT NOT NULL,
    request_fingerprint   TEXT NOT NULL,
    operation_type        TEXT NOT NULL,
    target_id             TEXT,
    cutoff_at             TEXT NOT NULL,
    privacy_epoch         INTEGER,
    status                TEXT NOT NULL,
    phase                 TEXT NOT NULL,
    progress_current      INTEGER NOT NULL DEFAULT 0,
    progress_total        INTEGER NOT NULL DEFAULT 0,
    attempt_count         INTEGER NOT NULL DEFAULT 0,
    ledger_prepared_at    TEXT,
    last_error_code       TEXT,
    result_json           TEXT NOT NULL DEFAULT '{}',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    completed_at          TEXT,
    UNIQUE(user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS memory_destructive_operation_items (
    operation_id          TEXT NOT NULL,
    group_id              TEXT NOT NULL,
    group_order           INTEGER NOT NULL,
    resource_type         TEXT NOT NULL,
    resource_id           TEXT NOT NULL,
    expected_version      INTEGER,
    expected_state        TEXT,
    action_state          TEXT NOT NULL DEFAULT 'pending',
    error_code            TEXT,
    PRIMARY KEY(operation_id, resource_type, resource_id)
);

CREATE TABLE IF NOT EXISTS memory_applied_deletion_receipts (
    receipt_id   TEXT PRIMARY KEY,
    applied_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_migration_state (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_tombstone_time
    ON memory_resource_deletion_tombstones(deleted_at);
CREATE INDEX IF NOT EXISTS idx_memory_operation_user
    ON memory_destructive_operations(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_operation_status
    ON memory_destructive_operations(status, updated_at);
"""

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS privacy_ledger_metadata (
    singleton_id     INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    key_version      TEXT NOT NULL,
    key_fingerprint  TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS privacy_deletion_receipts (
    receipt_id               TEXT PRIMARY KEY,
    operation_id             TEXT NOT NULL,
    manifest_digest          TEXT NOT NULL,
    receipt_set_size         INTEGER NOT NULL,
    subject_key_version      TEXT NOT NULL,
    subject_hmac             TEXT NOT NULL,
    delete_scope             TEXT NOT NULL,
    resource_type            TEXT,
    resource_id              TEXT,
    deleted_through_version  INTEGER,
    cutoff_at                TEXT NOT NULL,
    privacy_epoch            INTEGER,
    status                   TEXT NOT NULL,
    committed_at             TEXT,
    retain_until             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_privacy_receipt_operation
    ON privacy_deletion_receipts(operation_id);
CREATE INDEX IF NOT EXISTS idx_privacy_receipt_retention
    ON privacy_deletion_receipts(retain_until);
"""


class MemoryIdempotencyConflictError(RuntimeError):
    """An idempotency key was reused for a different personal-Memory request."""


class MemoryDestructiveOperationConflictError(RuntimeError):
    """Another destructive operation currently owns the user's barrier."""

    def __init__(self, operation_id: str) -> None:
        super().__init__("another personal Memory operation is active")
        self.operation_id = operation_id


class PrivacyDeletionNotReadyError(RuntimeError):
    """The external deletion ledger or physical-erasure gate is not ready."""


class MemoryDestructiveTargetNotFoundError(LookupError):
    """A destructive target is absent, not owned, or not eligible."""


class PrivacyDeletionLedger:
    """Content-free deletion receipts stored outside the restorable shared DB."""

    def __init__(
        self,
        path: str | Path,
        *,
        hmac_key: str | bytes,
        key_version: str = "v1",
    ) -> None:
        self._path = str(path)
        self._key = hmac_key.encode() if isinstance(hmac_key, str) else hmac_key
        self._key_version = key_version

    async def initialize(self) -> None:
        ledger_dir = Path(self._path).parent
        ledger_dir.mkdir(parents=True, exist_ok=True)
        ledger_dir.chmod(0o700)
        if await self._initialize_once():
            return

        archived_path = self._archive_mismatched_ledger()
        logger.warning(
            "privacy_ledger.key_mismatch_archived old_path=%s archived_path=%s",
            self._path,
            archived_path,
        )
        if not await self._initialize_once():  # pragma: no cover - impossible for a new ledger
            raise PrivacyDeletionNotReadyError("privacy deletion ledger key mismatch")

    async def _initialize_once(self) -> bool:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA secure_delete=ON")
            await db.executescript(_LEDGER_SCHEMA)
            key_fingerprint = hmac.new(
                self._key,
                b"bowhead/privacy-ledger/key-check",
                hashlib.sha256,
            ).hexdigest()
            await db.execute(
                """
                INSERT INTO privacy_ledger_metadata(
                    singleton_id, key_version, key_fingerprint, created_at
                )
                VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton_id) DO NOTHING
                """,
                (self._key_version, key_fingerprint, _now()),
            )
            async with db.execute(
                """
                SELECT key_version, key_fingerprint
                FROM privacy_ledger_metadata
                WHERE singleton_id=1
                """,
            ) as cursor:
                metadata = await cursor.fetchone()
            if metadata != (
                self._key_version,
                key_fingerprint,
            ):
                return False
            await db.commit()
        Path(self._path).chmod(0o600)
        return True

    def _archive_mismatched_ledger(self) -> Path:
        ledger_path = Path(self._path)
        archived_path = ledger_path.with_name(
            f"{ledger_path.name}.key-mismatch-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:8]}",
        )
        ledger_path.rename(archived_path)
        for suffix in ("-wal", "-shm"):
            sidecar = ledger_path.with_name(f"{ledger_path.name}{suffix}")
            if sidecar.exists():
                sidecar.rename(archived_path.with_name(f"{archived_path.name}{suffix}"))
        return archived_path

    async def healthcheck(self) -> bool:
        try:
            async with (
                aiosqlite.connect(self._path) as db,
                db.execute(
                    "SELECT COUNT(*) FROM privacy_deletion_receipts",
                ) as cursor,
            ):
                await cursor.fetchone()
            return True
        except aiosqlite.Error:
            return False

    async def prepare_full_user_receipt(
        self,
        *,
        operation_id: str,
        user_id: str,
        manifest_digest: str,
        cutoff_at: str,
        privacy_epoch: int,
    ) -> str:
        receipt_id = _receipt_id(operation_id, "full_user")
        retain_until = (datetime.now(tz=UTC) + timedelta(days=210)).isoformat(timespec="seconds")
        subject_hmac = self.subject_hmac(user_id)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA secure_delete=ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    INSERT INTO privacy_deletion_receipts(
                        receipt_id, operation_id, manifest_digest,
                        receipt_set_size, subject_key_version, subject_hmac,
                        delete_scope, resource_type, resource_id,
                        deleted_through_version, cutoff_at, privacy_epoch,
                        status, committed_at, retain_until
                    )
                    VALUES (?, ?, ?, 1, ?, ?, 'full_user', NULL, NULL,
                            NULL, ?, ?, 'prepared', NULL, ?)
                    ON CONFLICT(receipt_id) DO NOTHING
                    """,
                    (
                        receipt_id,
                        operation_id,
                        manifest_digest,
                        self._key_version,
                        subject_hmac,
                        cutoff_at,
                        privacy_epoch,
                        retain_until,
                    ),
                )
                async with db.execute(
                    """
                    SELECT operation_id, manifest_digest, receipt_set_size,
                           subject_key_version, subject_hmac, delete_scope
                    FROM privacy_deletion_receipts
                    WHERE receipt_id=?
                    """,
                    (receipt_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                expected = (
                    operation_id,
                    manifest_digest,
                    1,
                    self._key_version,
                    subject_hmac,
                    "full_user",
                )
                if row is None or tuple(row) != expected:
                    raise PrivacyDeletionNotReadyError(
                        "deletion ledger receipt mismatch",
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return receipt_id

    async def prepare_resource_receipts(
        self,
        *,
        operation_id: str,
        user_id: str,
        manifest_digest: str,
        manifest: list[dict[str, Any]],
        cutoff_at: str,
        privacy_epoch: int,
    ) -> list[str]:
        """Persist the complete content-free receipt set before any scrub."""

        if not manifest:
            raise PrivacyDeletionNotReadyError(
                "destructive manifest must not be empty",
            )
        retain_until = (datetime.now(tz=UTC) + timedelta(days=210)).isoformat(timespec="seconds")
        subject_hmac = self.subject_hmac(user_id)
        receipt_ids = [
            _receipt_id(
                operation_id,
                f"{item['resource_type']}:{item['resource_id']}",
            )
            for item in manifest
        ]
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA secure_delete=ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                for receipt_id, item in zip(
                    receipt_ids,
                    manifest,
                    strict=True,
                ):
                    await db.execute(
                        """
                        INSERT INTO privacy_deletion_receipts(
                            receipt_id, operation_id, manifest_digest,
                            receipt_set_size, subject_key_version, subject_hmac,
                            delete_scope, resource_type, resource_id,
                            deleted_through_version, cutoff_at, privacy_epoch,
                            status, committed_at, retain_until
                        )
                        VALUES (?, ?, ?, ?, ?, ?, 'resource', ?, ?, ?,
                                ?, ?, 'prepared', NULL, ?)
                        ON CONFLICT(receipt_id) DO NOTHING
                        """,
                        (
                            receipt_id,
                            operation_id,
                            manifest_digest,
                            len(manifest),
                            self._key_version,
                            subject_hmac,
                            item["resource_type"],
                            item["resource_id"],
                            item["expected_version"],
                            cutoff_at,
                            privacy_epoch,
                            retain_until,
                        ),
                    )
                async with db.execute(
                    """
                    SELECT receipt_id, manifest_digest, receipt_set_size,
                           subject_key_version, subject_hmac, delete_scope
                    FROM privacy_deletion_receipts
                    WHERE operation_id=?
                    ORDER BY receipt_id
                    """,
                    (operation_id,),
                ) as cursor:
                    rows = await cursor.fetchall()
                expected_ids = sorted(receipt_ids)
                if (
                    [str(row[0]) for row in rows] != expected_ids
                    or any(str(row[1]) != manifest_digest for row in rows)
                    or any(int(row[2]) != len(manifest) for row in rows)
                    or any(str(row[3]) != self._key_version for row in rows)
                    or any(str(row[4]) != subject_hmac for row in rows)
                    or any(str(row[5]) != "resource" for row in rows)
                ):
                    raise PrivacyDeletionNotReadyError(
                        "deletion ledger receipt set mismatch",
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return expected_ids

    async def commit_operation(
        self,
        *,
        operation_id: str,
        manifest_digest: str,
    ) -> list[str]:
        now = _now()
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA secure_delete=ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT receipt_id, manifest_digest, receipt_set_size
                    FROM privacy_deletion_receipts
                    WHERE operation_id=?
                    ORDER BY receipt_id
                    """,
                    (operation_id,),
                ) as cursor:
                    rows = await cursor.fetchall()
                if not rows or any(str(row[1]) != manifest_digest for row in rows):
                    raise PrivacyDeletionNotReadyError(
                        "deletion ledger manifest mismatch",
                    )
                expected_size = int(rows[0][2])
                if expected_size != len(rows):
                    raise PrivacyDeletionNotReadyError(
                        "deletion ledger receipt count mismatch",
                    )
                await db.execute(
                    """
                    UPDATE privacy_deletion_receipts
                    SET status='committed', committed_at=?
                    WHERE operation_id=?
                    """,
                    (now, operation_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return [str(row[0]) for row in rows]

    async def list_committed_receipts(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT *
                FROM privacy_deletion_receipts
                WHERE status='committed'
                ORDER BY committed_at, receipt_id
                """,
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    def subject_hmac(self, user_id: str) -> str:
        return hmac.new(
            self._key,
            f"bowhead/privacy-subject/{user_id}".encode(),
            hashlib.sha256,
        ).hexdigest()


class MemoryDestructiveOperationStore:
    """Durable full-user deletion with barrier, ledger and residue verification."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        ledger: PrivacyDeletionLedger | None,
        fingerprint_key: str | bytes,
        require_secure_delete_baseline: bool = True,
    ) -> None:
        self._db_path = str(db_path)
        self._ledger = ledger
        self._fingerprint_key = fingerprint_key.encode() if isinstance(fingerprint_key, str) else fingerprint_key
        self._require_secure_delete_baseline = require_secure_delete_baseline
        self._operation_locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        async with open_shared_ctx(self._db_path) as db:
            await db.executescript(_SHARED_SCHEMA)
            await db.commit()

    async def readiness(self) -> tuple[bool, str | None]:
        if self._ledger is None or not await self._ledger.healthcheck():
            return False, "privacy_deletion_ledger_unavailable"
        if self._require_secure_delete_baseline:
            async with open_shared_ctx(self._db_path) as db:
                if not await _table_exists(db, "memory_migration_state"):
                    return False, "secure_delete_baseline_incomplete"
                async with db.execute(
                    """
                    SELECT value
                    FROM memory_migration_state
                    WHERE key='secure_delete_baseline_complete'
                    """,
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None or str(row[0]) != "1":
                    return False, "secure_delete_baseline_incomplete"
                async with db.execute(
                    """
                    SELECT value
                    FROM memory_migration_state
                    WHERE key='privacy_ledger_replay_complete'
                    """,
                ) as cursor:
                    replay = await cursor.fetchone()
                if replay is None or str(replay[0]) != "1":
                    return False, "privacy_ledger_replay_incomplete"
        return True, None

    async def run_secure_delete_baseline(self) -> None:
        """Offline maintenance: rewrite historical free pages before enablement."""

        async with open_shared_ctx(self._db_path) as db:
            async with db.execute("PRAGMA wal_checkpoint(TRUNCATE)") as cursor:
                checkpoint = await cursor.fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise PrivacyDeletionNotReadyError(
                    "shared DB WAL checkpoint is busy",
                )
            await db.execute("VACUUM")
        await self.mark_secure_delete_baseline_complete()

    async def mark_secure_delete_baseline_complete(self) -> None:
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO memory_migration_state(key, value, updated_at)
                VALUES ('secure_delete_baseline_complete', '1', ?)
                ON CONFLICT(key) DO UPDATE SET value='1', updated_at=excluded.updated_at
                """,
                (now,),
            )
            await db.commit()

    async def replay_committed_deletions(self) -> dict[str, int]:
        """Reapply external receipts after any shared-DB backup restore."""

        if self._ledger is None:
            raise PrivacyDeletionNotReadyError(
                "privacy deletion ledger is unavailable",
            )
        receipts = await self._ledger.list_committed_receipts()
        scrubbed = 0
        applied = 0
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT receipt_id
                FROM memory_applied_deletion_receipts
                """,
            ) as cursor:
                already_applied = {str(row[0]) for row in await cursor.fetchall()}
            pending_receipts = [receipt for receipt in receipts if str(receipt["receipt_id"]) not in already_applied]
            user_ids = await _all_personal_memory_user_ids(db)
            user_by_hmac = {self._ledger.subject_hmac(user_id): user_id for user_id in user_ids}
            by_subject: dict[str, list[dict[str, Any]]] = {}
            for receipt in pending_receipts:
                by_subject.setdefault(
                    str(receipt["subject_hmac"]),
                    [],
                ).append(receipt)
            await db.execute("BEGIN IMMEDIATE")
            try:
                for subject_hmac, subject_receipts in by_subject.items():
                    user_id = user_by_hmac.get(subject_hmac)
                    if user_id is not None:
                        if any(receipt["delete_scope"] == "full_user" for receipt in subject_receipts):
                            scrubbed += await _replay_full_user_delete(
                                db,
                                user_id=user_id,
                            )
                        else:
                            for receipt in subject_receipts:
                                scrubbed += await _replay_resource_delete(
                                    db,
                                    user_id=user_id,
                                    receipt=receipt,
                                )
                        max_epoch = max(int(receipt["privacy_epoch"] or 0) for receipt in subject_receipts)
                        completed_at = max(
                            str(
                                receipt["committed_at"] or receipt["cutoff_at"],
                            )
                            for receipt in subject_receipts
                        )
                        await db.execute(
                            """
                            INSERT INTO memory_privacy_state(
                                user_id, current_epoch, barrier_status,
                                last_completed_delete_at, updated_at
                            )
                            VALUES (?, ?, 'idle', ?, ?)
                            ON CONFLICT(user_id) DO UPDATE SET
                                current_epoch=MAX(
                                    current_epoch,
                                    excluded.current_epoch
                                ),
                                barrier_status='idle',
                                barrier_operation_id=NULL,
                                barrier_started_at=NULL,
                                last_completed_delete_at=MAX(
                                    COALESCE(last_completed_delete_at, ''),
                                    excluded.last_completed_delete_at
                                ),
                                updated_at=excluded.updated_at
                            """,
                            (
                                user_id,
                                max_epoch,
                                completed_at,
                                _now(),
                            ),
                        )
                    for receipt in subject_receipts:
                        cursor = await db.execute(
                            """
                            INSERT OR IGNORE INTO
                                memory_applied_deletion_receipts(
                                    receipt_id, applied_at
                                )
                            VALUES (?, ?)
                            """,
                            (receipt["receipt_id"], _now()),
                        )
                        applied += cursor.rowcount or 0
                await db.execute(
                    """
                    INSERT INTO memory_migration_state(key, value, updated_at)
                    VALUES ('privacy_ledger_replay_complete', '1', ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value='1', updated_at=excluded.updated_at
                    """,
                    (_now(),),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if scrubbed:
            await self._physical_purge()
        return {
            "receipts_seen": len(receipts),
            "receipts_applied": applied,
            "rows_scrubbed": scrubbed,
        }

    async def resume_incomplete_operations(self) -> dict[str, int]:
        """Finish operations that were interrupted before finalization."""

        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT *
                FROM memory_destructive_operations
                WHERE status IN ('running', 'failed', 'partially_failed')
                ORDER BY created_at, id
                """,
            ) as cursor:
                rows = await cursor.fetchall()
        if not rows:
            return {"operations_seen": 0, "operations_completed": 0}
        ready, reason = await self.readiness()
        if not ready:
            raise PrivacyDeletionNotReadyError(
                reason or "privacy deletion recovery is not ready",
            )
        completed = 0
        for row in rows:
            operation = _operation_to_dict(row)
            action = str(operation["action"])
            user_id = str(row["user_id"])
            target_id = operation["target_id"]
            idempotency_key = str(operation["idempotency_key"])
            if action == "permanent_delete_all":
                await self.permanent_delete_all(
                    user_id=user_id,
                    idempotency_key=idempotency_key,
                )
            else:
                await self._permanent_delete_target(
                    user_id=user_id,
                    operation_type=action,
                    target_id=(str(target_id) if target_id is not None else None),
                    idempotency_key=idempotency_key,
                )
            completed += 1
        return {
            "operations_seen": len(rows),
            "operations_completed": completed,
        }

    async def get_operation(
        self,
        *,
        user_id: str,
        operation_id: str,
    ) -> dict[str, Any] | None:
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT *
                FROM memory_destructive_operations
                WHERE id=? AND user_id=?
                """,
                (operation_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()
        return _operation_to_dict(row) if row is not None else None

    async def is_full_delete_active(self, *, user_id: str) -> bool:
        """Hide all personal Memory while a full-user privacy barrier is held."""

        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                """
            SELECT barrier_status
            FROM memory_privacy_state
            WHERE user_id=?
            """,
                (user_id,),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return row is not None and str(row[0]) != "idle"

    async def permanent_delete_all(
        self,
        *,
        user_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _validate_idempotency_key(idempotency_key)
        ready, reason = await self.readiness()
        if not ready:
            raise PrivacyDeletionNotReadyError(reason or "not_ready")
        assert self._ledger is not None

        operation = await self._prepare_full_delete(
            user_id=user_id,
            idempotency_key=idempotency_key,
        )
        if operation["status"] == "completed":
            return operation
        operation_id = str(operation["operation_id"])
        async with self._operation_lock(operation_id):
            current = await self.get_operation(
                user_id=user_id,
                operation_id=operation_id,
            )
            assert current is not None
            if current["status"] == "completed":
                return current
            manifest_digest = str(current["manifest_digest"])
            try:
                receipt_id = await self._ledger.prepare_full_user_receipt(
                    operation_id=operation_id,
                    user_id=user_id,
                    manifest_digest=manifest_digest,
                    cutoff_at=str(current["cutoff_at"]),
                    privacy_epoch=int(current["privacy_epoch"]),
                )
                await self._mark_ledger_prepared(
                    operation_id=operation_id,
                )
                counts = await self._scrub_full_user(
                    operation_id=operation_id,
                    user_id=user_id,
                    cutoff_at=str(current["cutoff_at"]),
                    privacy_epoch=int(current["privacy_epoch"]),
                    manifest_digest=manifest_digest,
                )
                await self._assert_full_user_residue_zero(
                    user_id=user_id,
                )
                await self._physical_purge()
                receipt_ids = await self._ledger.commit_operation(
                    operation_id=operation_id,
                    manifest_digest=manifest_digest,
                )
                if receipt_ids != [receipt_id]:
                    raise PrivacyDeletionNotReadyError(
                        "unexpected deletion receipt set",
                    )
                await self._finalize_full_delete(
                    operation_id=operation_id,
                    user_id=user_id,
                    receipt_ids=receipt_ids,
                    counts=counts,
                )
            except BaseException as exc:
                await self._mark_failed(
                    operation_id=operation_id,
                    error_code=type(exc).__name__,
                )
                raise
            completed = await self.get_operation(
                user_id=user_id,
                operation_id=operation_id,
            )
            assert completed is not None
            return completed

    async def permanent_delete_item(
        self,
        *,
        user_id: str,
        item_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._permanent_delete_target(
            user_id=user_id,
            operation_type="permanent_delete_item",
            target_id=item_id,
            idempotency_key=idempotency_key,
        )

    async def permanent_delete_trash(
        self,
        *,
        user_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._permanent_delete_target(
            user_id=user_id,
            operation_type="permanent_delete_trash",
            target_id=None,
            idempotency_key=idempotency_key,
        )

    async def permanent_delete_reset(
        self,
        *,
        user_id: str,
        snapshot_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._permanent_delete_target(
            user_id=user_id,
            operation_type="permanent_delete_reset",
            target_id=snapshot_id,
            idempotency_key=idempotency_key,
        )

    async def gc_expired_trash(
        self,
        *,
        max_age_days: int = 30,
    ) -> dict[str, int]:
        """Run 30-day retention through the same durable delete protocol."""

        if max_age_days < 1:
            raise ValueError("max_age_days must be positive")
        cutoff = (
            (datetime.now(tz=UTC) - timedelta(days=max_age_days))
            .replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            .isoformat()
        )
        async with open_shared_ctx(self._db_path) as db:
            user_ids = await _users_with_expired_trash(
                db,
                cutoff=cutoff,
            )
        stats = {
            "users_processed": 0,
            "items_permanently_deleted": 0,
            "entries": 0,
            "notes": 0,
            "reset_snapshots": 0,
        }
        for user_id in sorted(user_ids):
            idempotency_key = str(
                uuid5(
                    NAMESPACE_URL,
                    (f"bowhead:memory:expired-trash:{user_id}:{cutoff}"),
                ),
            )
            try:
                result = await self._permanent_delete_target(
                    user_id=user_id,
                    operation_type="permanent_delete_expired_trash",
                    target_id=cutoff,
                    idempotency_key=idempotency_key,
                )
            except MemoryDestructiveTargetNotFoundError:
                continue
            stats["users_processed"] += 1
            counts = result.get("counts", {})
            stats["items_permanently_deleted"] += sum(int(value) for value in counts.values() if isinstance(value, int))
            for key in (
                "entries",
                "notes",
                "reset_snapshots",
            ):
                stats[key] += int(counts.get(key, 0))
        return stats

    async def gc_expired_auxiliary(
        self,
        *,
        max_age_days: int = 180,
    ) -> dict[str, int]:
        """Purge expired pack snapshots and invalidated anchors durably."""

        if max_age_days < 1:
            raise ValueError("max_age_days must be positive")
        cutoff = (
            (datetime.now(tz=UTC) - timedelta(days=max_age_days))
            .replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            .isoformat()
        )
        async with open_shared_ctx(self._db_path) as db:
            user_ids = await _users_with_expired_auxiliary(
                db,
                cutoff=cutoff,
            )
        stats = {
            "users_processed": 0,
            "pack_snapshots": 0,
            "anchors": 0,
        }
        for user_id in sorted(user_ids):
            idempotency_key = str(
                uuid5(
                    NAMESPACE_URL,
                    (f"bowhead:memory:expired-auxiliary:{user_id}:{cutoff}"),
                ),
            )
            try:
                result = await self._permanent_delete_target(
                    user_id=user_id,
                    operation_type=("permanent_delete_expired_auxiliary"),
                    target_id=cutoff,
                    idempotency_key=idempotency_key,
                )
            except MemoryDestructiveTargetNotFoundError:
                continue
            stats["users_processed"] += 1
            counts = result.get("counts", {})
            stats["pack_snapshots"] += int(
                counts.get("pack_snapshots", 0),
            )
            stats["anchors"] += int(counts.get("anchors", 0))
        return stats

    async def _permanent_delete_target(
        self,
        *,
        user_id: str,
        operation_type: str,
        target_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _validate_idempotency_key(idempotency_key)
        ready, reason = await self.readiness()
        if not ready:
            raise PrivacyDeletionNotReadyError(reason or "not_ready")
        assert self._ledger is not None

        operation = await self._prepare_target_delete(
            user_id=user_id,
            operation_type=operation_type,
            target_id=target_id,
            idempotency_key=idempotency_key,
        )
        if operation["status"] == "completed":
            return operation
        operation_id = str(operation["operation_id"])
        async with self._operation_lock(operation_id):
            current = await self.get_operation(
                user_id=user_id,
                operation_id=operation_id,
            )
            assert current is not None
            if current["status"] == "completed":
                return current
            manifest = await self._get_operation_manifest(
                operation_id=operation_id,
            )
            manifest_digest = str(current["manifest_digest"])
            try:
                expected_receipts = await self._ledger.prepare_resource_receipts(
                    operation_id=operation_id,
                    user_id=user_id,
                    manifest_digest=manifest_digest,
                    manifest=manifest,
                    cutoff_at=str(current["cutoff_at"]),
                    privacy_epoch=int(current["privacy_epoch"]),
                )
                await self._mark_ledger_prepared(
                    operation_id=operation_id,
                )
                counts = await self._scrub_target_manifest(
                    operation_id=operation_id,
                    user_id=user_id,
                    privacy_epoch=int(current["privacy_epoch"]),
                    manifest=manifest,
                    manifest_digest=manifest_digest,
                )
                await self._assert_target_residue_zero(
                    user_id=user_id,
                    manifest=manifest,
                )
                await self._physical_purge()
                receipt_ids = await self._ledger.commit_operation(
                    operation_id=operation_id,
                    manifest_digest=manifest_digest,
                )
                if receipt_ids != expected_receipts:
                    raise PrivacyDeletionNotReadyError(
                        "unexpected deletion receipt set",
                    )
                await self._finalize_target_delete(
                    operation_id=operation_id,
                    user_id=user_id,
                    receipt_ids=receipt_ids,
                    counts=counts,
                )
            except BaseException as exc:
                await self._mark_failed(
                    operation_id=operation_id,
                    error_code=type(exc).__name__,
                )
                raise
            completed = await self.get_operation(
                user_id=user_id,
                operation_id=operation_id,
            )
            assert completed is not None
            return completed

    async def _prepare_target_delete(
        self,
        *,
        user_id: str,
        operation_type: str,
        target_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if operation_type not in {
            "permanent_delete_item",
            "permanent_delete_trash",
            "permanent_delete_reset",
            "permanent_delete_expired_trash",
            "permanent_delete_expired_auxiliary",
        }:
            raise ValueError("unsupported destructive operation")
        now = _now()
        fingerprint = self._request_fingerprint(
            operation_type=operation_type,
            target_id=target_id,
        )
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT *
                    FROM memory_destructive_operations
                    WHERE user_id=? AND idempotency_key=?
                    """,
                    (user_id, idempotency_key),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is not None:
                    if str(existing["request_fingerprint"]) != fingerprint:
                        raise MemoryIdempotencyConflictError
                    await db.rollback()
                    return _operation_to_dict(existing)

                async with db.execute(
                    """
                    SELECT barrier_status, barrier_operation_id, current_epoch
                    FROM memory_privacy_state
                    WHERE user_id=?
                    """,
                    (user_id,),
                ) as cursor:
                    privacy = await cursor.fetchone()
                if privacy is not None and str(privacy["barrier_status"]) != "idle":
                    raise MemoryDestructiveOperationConflictError(
                        str(privacy["barrier_operation_id"]),
                    )
                current_epoch = int(privacy["current_epoch"]) if privacy else 0
                privacy_epoch = current_epoch + 1
                operation_id = f"mop_{uuid4().hex}"
                manifest = await _collect_target_delete_manifest(
                    db,
                    user_id=user_id,
                    operation_type=operation_type,
                    target_id=target_id,
                )
                if not manifest:
                    raise MemoryDestructiveTargetNotFoundError
                manifest_digest = _manifest_digest(manifest)
                await db.execute(
                    """
                    INSERT INTO memory_destructive_operations(
                        id, user_id, idempotency_key, request_fingerprint,
                        operation_type, target_id, cutoff_at, privacy_epoch,
                        status, phase, progress_current, progress_total,
                        attempt_count, result_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running',
                            'ledger_prepare', 0, ?, 1, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        user_id,
                        idempotency_key,
                        fingerprint,
                        operation_type,
                        target_id,
                        now,
                        privacy_epoch,
                        len(manifest),
                        json.dumps(
                            {"manifest_digest": manifest_digest},
                            sort_keys=True,
                        ),
                        now,
                        now,
                    ),
                )
                await _insert_operation_manifest(
                    db,
                    operation_id=operation_id,
                    manifest=manifest,
                )
                await db.execute(
                    """
                    INSERT INTO memory_privacy_state(
                        user_id, current_epoch, barrier_status,
                        barrier_operation_id, barrier_started_at, updated_at
                    )
                    VALUES (?, ?, 'deleting', ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        current_epoch=excluded.current_epoch,
                        barrier_status='deleting',
                        barrier_operation_id=excluded.barrier_operation_id,
                        barrier_started_at=excluded.barrier_started_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        user_id,
                        privacy_epoch,
                        operation_id,
                        now,
                        now,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "operation_id": operation_id,
            "idempotency_key": idempotency_key,
            "action": operation_type,
            "target_id": target_id,
            "status": "running",
            "phase": "ledger_prepare",
            "cutoff_at": now,
            "privacy_epoch": privacy_epoch,
            "manifest_digest": manifest_digest,
            "progress_current": 0,
            "progress_total": len(manifest),
        }

    async def _get_operation_manifest(
        self,
        *,
        operation_id: str,
    ) -> list[dict[str, Any]]:
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                """
            SELECT resource_type, resource_id, expected_version,
                   expected_state
            FROM memory_destructive_operation_items
            WHERE operation_id=?
            ORDER BY group_order, resource_type, resource_id
            """,
                (operation_id,),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return [
            {
                "resource_type": str(row[0]),
                "resource_id": str(row[1]),
                "expected_version": (int(row[2]) if row[2] is not None else None),
                "expected_state": str(row[3]) if row[3] else None,
            }
            for row in rows
        ]

    async def _prepare_full_delete(
        self,
        *,
        user_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        now = _now()
        fingerprint = self._request_fingerprint(
            operation_type="permanent_delete_all",
            target_id=None,
        )
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT *
                    FROM memory_destructive_operations
                    WHERE user_id=? AND idempotency_key=?
                    """,
                    (user_id, idempotency_key),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is not None:
                    if str(existing["request_fingerprint"]) != fingerprint:
                        raise MemoryIdempotencyConflictError
                    await db.rollback()
                    return _operation_to_dict(existing)

                async with db.execute(
                    """
                    SELECT barrier_status, barrier_operation_id, current_epoch
                    FROM memory_privacy_state
                    WHERE user_id=?
                    """,
                    (user_id,),
                ) as cursor:
                    privacy = await cursor.fetchone()
                if privacy is not None and str(privacy["barrier_status"]) != "idle":
                    raise MemoryDestructiveOperationConflictError(
                        str(privacy["barrier_operation_id"]),
                    )
                current_epoch = int(privacy["current_epoch"]) if privacy else 0
                privacy_epoch = current_epoch + 1
                operation_id = f"mop_{uuid4().hex}"
                manifest = await _collect_full_delete_manifest(
                    db,
                    user_id=user_id,
                )
                manifest_digest = _manifest_digest(manifest)
                await db.execute(
                    """
                    INSERT INTO memory_destructive_operations(
                        id, user_id, idempotency_key, request_fingerprint,
                        operation_type, target_id, cutoff_at, privacy_epoch,
                        status, phase, progress_current, progress_total,
                        attempt_count, result_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 'permanent_delete_all', NULL, ?, ?,
                            'running', 'ledger_prepare', 0, ?, 1, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        user_id,
                        idempotency_key,
                        fingerprint,
                        now,
                        privacy_epoch,
                        len(manifest),
                        json.dumps(
                            {"manifest_digest": manifest_digest},
                            sort_keys=True,
                        ),
                        now,
                        now,
                    ),
                )
                for order, item in enumerate(manifest):
                    await db.execute(
                        """
                        INSERT INTO memory_destructive_operation_items(
                            operation_id, group_id, group_order,
                            resource_type, resource_id, expected_version,
                            expected_state
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            operation_id,
                            f"mrg_{uuid4().hex}",
                            order,
                            item["resource_type"],
                            item["resource_id"],
                            item["expected_version"],
                            item["expected_state"],
                        ),
                    )
                await db.execute(
                    """
                    INSERT INTO memory_privacy_state(
                        user_id, current_epoch, barrier_status,
                        barrier_operation_id, barrier_started_at, updated_at
                    )
                    VALUES (?, ?, 'deleting', ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        current_epoch=excluded.current_epoch,
                        barrier_status='deleting',
                        barrier_operation_id=excluded.barrier_operation_id,
                        barrier_started_at=excluded.barrier_started_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        user_id,
                        privacy_epoch,
                        operation_id,
                        now,
                        now,
                    ),
                )
                if await _table_exists(db, "user_settings"):
                    await db.execute(
                        """
                        INSERT INTO user_settings(user_id, key, value, updated_at)
                        VALUES (?, 'memory_dreaming_paused', 'true', ?)
                        ON CONFLICT(user_id, key) DO UPDATE SET
                            value='true', updated_at=excluded.updated_at
                        """,
                        (user_id, now),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "operation_id": operation_id,
            "idempotency_key": idempotency_key,
            "status": "running",
            "phase": "ledger_prepare",
            "cutoff_at": now,
            "privacy_epoch": privacy_epoch,
            "manifest_digest": manifest_digest,
            "progress_current": 0,
            "progress_total": len(manifest),
        }

    async def _mark_ledger_prepared(self, *, operation_id: str) -> None:
        now = _now()
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                """
                UPDATE memory_destructive_operations
                SET phase='logical_scrub', ledger_prepared_at=?, updated_at=?
                WHERE id=? AND status='running'
                """,
                (now, now, operation_id),
            )
            await db.commit()

    async def _scrub_full_user(
        self,
        *,
        operation_id: str,
        user_id: str,
        cutoff_at: str,
        privacy_epoch: int,
        manifest_digest: str,
    ) -> dict[str, int]:
        del cutoff_at
        now = _now()
        counts: dict[str, int] = {}
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _assert_destructive_owner(
                    db,
                    operation_id=operation_id,
                    user_id=user_id,
                    privacy_epoch=privacy_epoch,
                )
                counts["active_entries"], counts["trashed_entries"] = await _count_memory_entries(
                    db,
                    user_id=user_id,
                )
                counts["entries"] = await _delete_where(
                    db,
                    "memory_entries",
                    "subject_type='user' AND subject_id=?",
                    (user_id,),
                )
                counts["notes"] = await _delete_where_if_exists(
                    db,
                    "memory_notes",
                    "subject_type='user' AND subject_id=?",
                    (user_id,),
                )
                counts["anchors"] = await _delete_where_if_exists(
                    db,
                    "memory_note_anchors",
                    "user_id=?",
                    (user_id,),
                )
                counts["candidates_redacted"] = await _redact_candidates(
                    db,
                    user_id=user_id,
                    now=now,
                )
                await _delete_where_if_exists(
                    db,
                    "memory_candidate_suppressions",
                    "user_id=?",
                    (user_id,),
                )
                await _delete_where_if_exists(
                    db,
                    "memory_mutation_idempotency",
                    "user_id=?",
                    (user_id,),
                )
                counts["reset_snapshots"] = await _delete_reset_snapshots(
                    db,
                    user_id=user_id,
                )
                counts["pack_snapshots"] = await _delete_pack_snapshots(
                    db,
                    user_id=user_id,
                )
                # Per-turn memory receipt facts (codes and memory IDs) leave with the user's memory.
                await _delete_where_if_exists(
                    db,
                    "memory_turn_requests",
                    "user_id=?",
                    (user_id,),
                )
                counts["audit_payloads_scrubbed"] = await _scrub_memory_audit(
                    db,
                    user_id=user_id,
                )
                await _resolve_memory_notifications(
                    db,
                    user_id=user_id,
                    now=now,
                )
                await db.execute(
                    """
                    UPDATE memory_destructive_operation_items
                    SET action_state='completed', error_code=NULL
                    WHERE operation_id=?
                    """,
                    (operation_id,),
                )
                await db.execute(
                    """
                    UPDATE memory_destructive_operations
                    SET phase='residue_check',
                        progress_current=progress_total,
                        result_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        json.dumps(
                            {
                                "manifest_digest": manifest_digest,
                                "counts": counts,
                                "dreaming_paused": True,
                                "logical_scrub_completed": True,
                                "physical_purge_completed": False,
                                "deletion_receipt_committed": False,
                            },
                            sort_keys=True,
                        ),
                        now,
                        operation_id,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return counts

    async def _assert_full_user_residue_zero(self, *, user_id: str) -> None:
        async with open_shared_ctx(self._db_path) as db:
            checks = (
                ("memory_entries", "subject_type='user' AND subject_id=?"),
                ("memory_notes", "subject_type='user' AND subject_id=?"),
                ("memory_note_anchors", "user_id=?"),
                ("memory_reset_snapshots", "user_id=?"),
                ("memory_pack_snapshots", "user_id=?"),
                ("memory_turn_requests", "user_id=?"),
            )
            for table, where in checks:
                if not await _table_exists(db, table):
                    continue
                async with db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}",
                    (user_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row and int(row[0]) != 0:
                    raise PrivacyDeletionNotReadyError(
                        f"personal Memory residue remains in {table}",
                    )
            if await _table_exists(db, "memory_candidates"):
                async with db.execute(
                    """
                    SELECT COUNT(*)
                    FROM memory_candidates
                    WHERE user_id=? AND (
                        key<>'' OR value_json<>'null' OR reason IS NOT NULL
                        OR source_turn_id IS NOT NULL OR workspace_id IS NOT NULL
                    )
                    """,
                    (user_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row and int(row[0]) != 0:
                    raise PrivacyDeletionNotReadyError(
                        "candidate content residue remains",
                    )

    async def _scrub_target_manifest(
        self,
        *,
        operation_id: str,
        user_id: str,
        privacy_epoch: int,
        manifest: list[dict[str, Any]],
        manifest_digest: str,
    ) -> dict[str, int]:
        now = _now()
        counts: dict[str, int] = {
            "entries": 0,
            "notes": 0,
            "anchors": 0,
            "reset_snapshots": 0,
            "snapshot_copies": 0,
            "pack_snapshots": 0,
            "candidates_redacted": 0,
        }
        auxiliary_only = all(str(item["resource_type"]) in {"memory_pack_snapshot", "note_anchor"} for item in manifest)
        async with open_shared_ctx(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _assert_destructive_owner(
                    db,
                    operation_id=operation_id,
                    user_id=user_id,
                    privacy_epoch=privacy_epoch,
                )
                linked_candidate_ids: set[str] = set()
                for item in manifest:
                    resource_type = str(item["resource_type"])
                    resource_id = str(item["resource_id"])
                    expected_version = item["expected_version"]
                    if resource_type == "memory_entry":
                        linked = await _linked_candidate_id(
                            db,
                            table="memory_entries",
                            id_column="id",
                            resource_id=resource_id,
                            user_column="subject_id",
                            user_id=user_id,
                        )
                        if linked:
                            linked_candidate_ids.add(linked)
                        cursor = await db.execute(
                            """
                            DELETE FROM memory_entries
                            WHERE id=? AND subject_type='user' AND subject_id=?
                              AND deleted_at IS NOT NULL AND version=?
                            """,
                            (resource_id, user_id, expected_version),
                        )
                        counts["entries"] += cursor.rowcount or 0
                        counts["snapshot_copies"] += await _delete_snapshot_copies(
                            db,
                            user_id=user_id,
                            resource_type=resource_type,
                            resource_id=resource_id,
                        )
                    elif resource_type == "memory_note":
                        cursor = await db.execute(
                            """
                            DELETE FROM memory_notes
                            WHERE id=? AND subject_type='user' AND subject_id=?
                              AND deleted_at IS NOT NULL AND version=?
                            """,
                            (resource_id, user_id, expected_version),
                        )
                        counts["notes"] += cursor.rowcount or 0
                        counts["anchors"] += await _delete_where_if_exists(
                            db,
                            "memory_note_anchors",
                            "user_id=?",
                            (user_id,),
                        )
                        counts["snapshot_copies"] += await _delete_snapshot_copies(
                            db,
                            user_id=user_id,
                            resource_type=resource_type,
                            resource_id=resource_id,
                        )
                    elif resource_type in {
                        "item_delete_snapshot",
                        "reset_snapshot",
                    }:
                        counts["snapshot_copies"] += await _delete_where_if_exists(
                            db,
                            "memory_reset_snapshot_items",
                            "snapshot_id=?",
                            (resource_id,),
                        )
                        cursor = await db.execute(
                            """
                            DELETE FROM memory_reset_snapshots
                            WHERE id=? AND user_id=?
                            """,
                            (resource_id, user_id),
                        )
                        counts["reset_snapshots"] += cursor.rowcount or 0
                    elif resource_type == "memory_pack_snapshot":
                        cursor = await db.execute(
                            """
                            DELETE FROM memory_pack_snapshots
                            WHERE turn_id=? AND user_id=?
                            """,
                            (resource_id, user_id),
                        )
                        counts["pack_snapshots"] += cursor.rowcount or 0
                    elif resource_type == "note_anchor":
                        cursor = await db.execute(
                            """
                            DELETE FROM memory_note_anchors
                            WHERE id=? AND user_id=?
                              AND invalidated_at IS NOT NULL
                            """,
                            (resource_id, user_id),
                        )
                        counts["anchors"] += cursor.rowcount or 0
                    await db.execute(
                        """
                        INSERT INTO memory_resource_deletion_tombstones(
                            user_id, resource_type, resource_id,
                            deleted_through_version, deleted_at
                        )
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(user_id, resource_type, resource_id)
                        DO UPDATE SET
                            deleted_through_version=MAX(
                                deleted_through_version,
                                excluded.deleted_through_version
                            ),
                            deleted_at=excluded.deleted_at
                        """,
                        (
                            user_id,
                            resource_type,
                            resource_id,
                            int(expected_version or 0),
                            now,
                        ),
                    )

                if not auxiliary_only and await _table_exists(
                    db,
                    "memory_reset_snapshots",
                ):
                    counts["reset_snapshots"] += await _delete_empty_snapshots(
                        db,
                        user_id=user_id,
                    )
                    counts["reset_snapshots"] += await _delete_legacy_snapshots(
                        db,
                        user_id=user_id,
                    )
                if not auxiliary_only:
                    counts["pack_snapshots"] += await _delete_pack_snapshots(
                        db,
                        user_id=user_id,
                    )
                if not auxiliary_only:
                    counts["candidates_redacted"] += await _redact_candidate_ids(
                        db,
                        user_id=user_id,
                        candidate_ids=linked_candidate_ids,
                        now=now,
                    )
                    counts["audit_payloads_scrubbed"] = await _scrub_memory_audit(
                        db,
                        user_id=user_id,
                    )
                    await _resolve_memory_notifications(
                        db,
                        user_id=user_id,
                        now=now,
                    )
                await db.execute(
                    """
                    UPDATE memory_destructive_operation_items
                    SET action_state='completed', error_code=NULL
                    WHERE operation_id=?
                    """,
                    (operation_id,),
                )
                await db.execute(
                    """
                    UPDATE memory_destructive_operations
                    SET phase='residue_check',
                        progress_current=progress_total,
                        result_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        json.dumps(
                            {
                                "manifest_digest": manifest_digest,
                                "counts": counts,
                                "logical_scrub_completed": True,
                                "physical_purge_completed": False,
                                "deletion_receipt_committed": False,
                            },
                            sort_keys=True,
                        ),
                        now,
                        operation_id,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return counts

    async def _assert_target_residue_zero(
        self,
        *,
        user_id: str,
        manifest: list[dict[str, Any]],
    ) -> None:
        async with open_shared_ctx(self._db_path) as db:
            for item in manifest:
                resource_type = str(item["resource_type"])
                resource_id = str(item["resource_id"])
                expected_version = int(item["expected_version"] or 0)
                if resource_type == "memory_entry":
                    row = await _count_rows(
                        db,
                        "memory_entries",
                        ("id=? AND subject_type='user' AND subject_id=? AND deleted_at IS NOT NULL AND version<=?"),
                        (resource_id, user_id, expected_version),
                    )
                elif resource_type == "memory_note":
                    row = await _count_rows(
                        db,
                        "memory_notes",
                        ("id=? AND subject_type='user' AND subject_id=? AND deleted_at IS NOT NULL AND version<=?"),
                        (resource_id, user_id, expected_version),
                    )
                elif resource_type == "memory_pack_snapshot":
                    row = await _count_rows(
                        db,
                        "memory_pack_snapshots",
                        "turn_id=? AND user_id=?",
                        (resource_id, user_id),
                    )
                elif resource_type == "note_anchor":
                    row = await _count_rows(
                        db,
                        "memory_note_anchors",
                        "id=? AND user_id=?",
                        (resource_id, user_id),
                    )
                else:
                    row = await _count_rows(
                        db,
                        "memory_reset_snapshots",
                        "id=? AND user_id=?",
                        (resource_id, user_id),
                    )
                if row:
                    raise PrivacyDeletionNotReadyError(
                        f"{resource_type} residue remains",
                    )

                if resource_type in {
                    "memory_entry",
                    "memory_note",
                }:
                    copies = await _count_rows(
                        db,
                        "memory_reset_snapshot_items",
                        "resource_type=? AND resource_id=?",
                        (resource_type, resource_id),
                    )
                    if copies:
                        raise PrivacyDeletionNotReadyError(
                            f"{resource_type} snapshot residue remains",
                        )

    async def _finalize_target_delete(
        self,
        *,
        operation_id: str,
        user_id: str,
        receipt_ids: list[str],
        counts: dict[str, int],
    ) -> None:
        now = _now()
        result = {
            "counts": counts,
            "skipped_locked": 0,
            "dreaming_paused": False,
            "partial_failures": [],
            "logical_scrub_completed": True,
            "physical_purge_completed": True,
            "deletion_receipt_committed": True,
        }
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                for receipt_id in receipt_ids:
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO memory_applied_deletion_receipts(
                            receipt_id, applied_at
                        )
                        VALUES (?, ?)
                        """,
                        (receipt_id, now),
                    )
                await db.execute(
                    """
                    UPDATE memory_destructive_operations
                    SET status='completed', phase='finalize',
                        result_json=?, updated_at=?, completed_at=?,
                        last_error_code=NULL
                    WHERE id=? AND user_id=?
                    """,
                    (
                        json.dumps(result, sort_keys=True),
                        now,
                        now,
                        operation_id,
                        user_id,
                    ),
                )
                await db.execute(
                    """
                    UPDATE memory_privacy_state
                    SET barrier_status='idle', barrier_operation_id=NULL,
                        barrier_started_at=NULL,
                        last_completed_delete_at=?, updated_at=?
                    WHERE user_id=? AND barrier_operation_id=?
                    """,
                    (now, now, user_id, operation_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def _physical_purge(self) -> None:
        async with (
            open_shared_ctx(self._db_path) as db,
            db.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)",
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None or int(row[0]) != 0:
            raise PrivacyDeletionNotReadyError(
                "shared DB WAL checkpoint is busy",
            )
        wal_path = Path(f"{self._db_path}-wal")
        if wal_path.exists() and wal_path.stat().st_size != 0:
            raise PrivacyDeletionNotReadyError(
                "shared DB WAL was not truncated",
            )

    async def _finalize_full_delete(
        self,
        *,
        operation_id: str,
        user_id: str,
        receipt_ids: list[str],
        counts: dict[str, int],
    ) -> None:
        now = _now()
        result = {
            "counts": counts,
            "skipped_locked": 0,
            "dreaming_paused": True,
            "partial_failures": [],
            "logical_scrub_completed": True,
            "physical_purge_completed": True,
            "deletion_receipt_committed": True,
        }
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                for receipt_id in receipt_ids:
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO memory_applied_deletion_receipts(
                            receipt_id, applied_at
                        )
                        VALUES (?, ?)
                        """,
                        (receipt_id, now),
                    )
                await db.execute(
                    """
                    UPDATE memory_destructive_operations
                    SET status='completed', phase='finalize',
                        result_json=?, updated_at=?, completed_at=?,
                        last_error_code=NULL
                    WHERE id=? AND user_id=?
                    """,
                    (
                        json.dumps(result, sort_keys=True),
                        now,
                        now,
                        operation_id,
                        user_id,
                    ),
                )
                await db.execute(
                    """
                    UPDATE memory_privacy_state
                    SET barrier_status='idle', barrier_operation_id=NULL,
                        barrier_started_at=NULL,
                        last_completed_delete_at=?, updated_at=?
                    WHERE user_id=? AND barrier_operation_id=?
                    """,
                    (now, now, user_id, operation_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def _mark_failed(
        self,
        *,
        operation_id: str,
        error_code: str,
    ) -> None:
        async with open_shared_ctx(self._db_path) as db:
            await db.execute(
                """
                UPDATE memory_destructive_operations
                SET status='failed', last_error_code=?, updated_at=?
                WHERE id=? AND status<>'completed'
                """,
                (error_code, _now(), operation_id),
            )
            await db.commit()

    def _operation_lock(self, operation_id: str) -> asyncio.Lock:
        return self._operation_locks.setdefault(
            operation_id,
            asyncio.Lock(),
        )

    def _request_fingerprint(
        self,
        *,
        operation_type: str,
        target_id: str | None,
    ) -> str:
        canonical = json.dumps(
            {
                "operation_type": operation_type,
                "target_id": target_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hmac.new(
            self._fingerprint_key,
            canonical.encode(),
            hashlib.sha256,
        ).hexdigest()


async def _collect_full_delete_manifest(
    db: aiosqlite.Connection,
    *,
    user_id: str,
) -> list[dict[str, Any]]:
    specs = (
        (
            "memory_entries",
            "memory_entry",
            "id",
            "version",
            "subject_type='user' AND subject_id=?",
        ),
        (
            "memory_notes",
            "memory_note",
            "id",
            "version",
            "subject_type='user' AND subject_id=?",
        ),
        (
            "memory_note_anchors",
            "note_anchor",
            "id",
            "NULL",
            "user_id=?",
        ),
        (
            "memory_candidates",
            "memory_candidate",
            "id",
            "version",
            "user_id=?",
        ),
        (
            "memory_reset_snapshots",
            "memory_reset_snapshot",
            "id",
            "schema_version",
            "user_id=?",
        ),
        (
            "memory_pack_snapshots",
            "memory_pack_snapshot",
            "turn_id",
            "NULL",
            "user_id=?",
        ),
    )
    manifest = []
    for table, resource_type, id_column, version_column, where in specs:
        if not await _table_exists(db, table):
            continue
        async with db.execute(
            f"""
            SELECT {id_column}, {version_column}
            FROM {table}
            WHERE {where}
            ORDER BY {id_column}
            """,
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        manifest.extend(
            {
                "resource_type": resource_type,
                "resource_id": str(row[0]),
                "expected_version": int(row[1]) if row[1] is not None else None,
                "expected_state": "present",
            }
            for row in rows
            if row[0]
        )
    manifest.sort(
        key=lambda item: (
            str(item["resource_type"]),
            str(item["resource_id"]),
        ),
    )
    return manifest


async def _collect_target_delete_manifest(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    operation_type: str,
    target_id: str | None,
) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    if operation_type in {
        "permanent_delete_item",
        "permanent_delete_trash",
        "permanent_delete_expired_trash",
    }:
        entry_where = "subject_type='user' AND subject_id=? AND deleted_at IS NOT NULL"
        if operation_type != "permanent_delete_expired_trash":
            entry_where += " AND memory_role='product_memory' AND memory_policy_version=1"
        entry_params: tuple[Any, ...] = (user_id,)
        if operation_type == "permanent_delete_item":
            if target_id is None:
                return []
            entry_where += " AND id=?"
            entry_params = (user_id, target_id)
        elif operation_type == "permanent_delete_expired_trash":
            if target_id is None:
                return []
            entry_where += " AND datetime(deleted_at)<datetime(?)"
            entry_params = (user_id, target_id)
        manifest.extend(
            await _collect_rows_for_manifest(
                db,
                table="memory_entries",
                resource_type="memory_entry",
                where=entry_where,
                params=entry_params,
                version_column="version",
                expected_state="trashed",
            ),
        )
        if operation_type != "permanent_delete_item":
            note_where = "subject_type='user' AND subject_id=? AND deleted_at IS NOT NULL"
            note_params: tuple[Any, ...] = (user_id,)
            if operation_type == "permanent_delete_expired_trash":
                note_where += " AND datetime(deleted_at)<datetime(?)"
                note_params = (user_id, target_id)
            manifest.extend(
                await _collect_rows_for_manifest(
                    db,
                    table="memory_notes",
                    resource_type="memory_note",
                    where=note_where,
                    params=note_params,
                    version_column="version",
                    expected_state="trashed",
                ),
            )

        snapshot_where = "user_id=? AND schema_version=3 AND snapshot_type='item_delete' AND consumed_at IS NULL"
        snapshot_params: tuple[Any, ...] = (user_id,)
        if operation_type == "permanent_delete_item":
            snapshot_where += " AND id=?"
            snapshot_params = (user_id, target_id)
        elif operation_type == "permanent_delete_expired_trash":
            snapshot_where += " AND expires_at IS NOT NULL AND datetime(expires_at)<=datetime(?, '+30 days')"
            snapshot_params = (user_id, target_id)
        snapshot_rows = await _collect_rows_for_manifest(
            db,
            table="memory_reset_snapshots",
            resource_type="item_delete_snapshot",
            where=snapshot_where,
            params=snapshot_params,
            version_column="schema_version",
            expected_state="recoverable",
        )
        manifest.extend(snapshot_rows)
        if snapshot_rows and await _table_exists(
            db,
            "memory_reset_snapshot_items",
        ):
            placeholders = ",".join("?" for _ in snapshot_rows)
            async with db.execute(
                f"""
                SELECT resource_id, deleted_version
                FROM memory_reset_snapshot_items
                WHERE resource_type='memory_note'
                  AND snapshot_id IN ({placeholders})
                """,
                tuple(item["resource_id"] for item in snapshot_rows),
            ) as cursor:
                manifest.extend(
                    {
                        "resource_type": "memory_note",
                        "resource_id": str(row[0]),
                        "expected_version": int(row[1] or 0),
                        "expected_state": "trashed",
                    }
                    for row in await cursor.fetchall()
                )
        if operation_type == "permanent_delete_expired_trash":
            manifest.extend(
                await _collect_rows_for_manifest(
                    db,
                    table="memory_reset_snapshots",
                    resource_type="reset_snapshot",
                    where=(
                        "user_id=? AND schema_version=3 "
                        "AND snapshot_type='reset' AND consumed_at IS NULL "
                        "AND expires_at IS NOT NULL "
                        "AND datetime(expires_at)<=datetime(?, '+30 days')"
                    ),
                    params=(user_id, target_id),
                    version_column="schema_version",
                    expected_state="expired",
                ),
            )
            manifest.extend(
                await _collect_rows_for_manifest(
                    db,
                    table="memory_reset_snapshots",
                    resource_type="reset_snapshot",
                    where=("user_id=? AND schema_version<3 AND datetime(created_at)<datetime(?)"),
                    params=(user_id, target_id),
                    version_column="schema_version",
                    expected_state="expired",
                ),
            )
    elif operation_type == "permanent_delete_reset":
        if target_id is None:
            return []
        manifest.extend(
            await _collect_rows_for_manifest(
                db,
                table="memory_reset_snapshots",
                resource_type="reset_snapshot",
                where=("user_id=? AND id=? AND schema_version=3 AND snapshot_type='reset' AND consumed_at IS NULL"),
                params=(user_id, target_id),
                version_column="schema_version",
                expected_state="recoverable",
            ),
        )
    elif operation_type == "permanent_delete_expired_auxiliary":
        if target_id is None:
            return []
        manifest.extend(
            await _collect_rows_for_manifest(
                db,
                table="memory_pack_snapshots",
                resource_type="memory_pack_snapshot",
                id_column="turn_id",
                where=("user_id=? AND datetime(created_at)<datetime(?)"),
                params=(user_id, target_id),
                version_column="NULL",
                expected_state="expired",
            ),
        )
        manifest.extend(
            await _collect_rows_for_manifest(
                db,
                table="memory_note_anchors",
                resource_type="note_anchor",
                where=("user_id=? AND invalidated_at IS NOT NULL AND datetime(invalidated_at)<datetime(?)"),
                params=(user_id, target_id),
                version_column="NULL",
                expected_state="expired",
            ),
        )

    deduped = {(str(item["resource_type"]), str(item["resource_id"])): item for item in manifest}
    return sorted(
        deduped.values(),
        key=lambda item: (
            str(item["resource_type"]),
            str(item["resource_id"]),
        ),
    )


async def _collect_rows_for_manifest(
    db: aiosqlite.Connection,
    *,
    table: str,
    resource_type: str,
    id_column: str = "id",
    where: str,
    params: tuple[Any, ...],
    version_column: str,
    expected_state: str,
) -> list[dict[str, Any]]:
    if not await _table_exists(db, table):
        return []
    async with db.execute(
        f"""
        SELECT {id_column}, {version_column}
        FROM {table}
        WHERE {where}
        ORDER BY {id_column}
        """,
        params,
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        {
            "resource_type": resource_type,
            "resource_id": str(row[0]),
            "expected_version": (int(row[1]) if row[1] is not None else None),
            "expected_state": expected_state,
        }
        for row in rows
    ]


async def _insert_operation_manifest(
    db: aiosqlite.Connection,
    *,
    operation_id: str,
    manifest: list[dict[str, Any]],
) -> None:
    for order, item in enumerate(manifest):
        await db.execute(
            """
            INSERT INTO memory_destructive_operation_items(
                operation_id, group_id, group_order,
                resource_type, resource_id, expected_version,
                expected_state
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                f"mrg_{uuid4().hex}",
                order,
                item["resource_type"],
                item["resource_id"],
                item["expected_version"],
                item["expected_state"],
            ),
        )


async def _linked_candidate_id(
    db: aiosqlite.Connection,
    *,
    table: str,
    id_column: str,
    resource_id: str,
    user_column: str,
    user_id: str,
) -> str | None:
    if not await _table_exists(db, table):
        return None
    columns = await _column_names(db, table)
    if "created_source_candidate_id" not in columns:
        return None
    async with db.execute(
        f"""
        SELECT created_source_candidate_id
        FROM {table}
        WHERE {id_column}=? AND {user_column}=?
        """,
        (resource_id, user_id),
    ) as cursor:
        row = await cursor.fetchone()
    return str(row[0]) if row and row[0] else None


async def _delete_snapshot_copies(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    resource_type: str,
    resource_id: str,
) -> int:
    if not await _table_exists(db, "memory_reset_snapshot_items"):
        return 0
    cursor = await db.execute(
        """
        DELETE FROM memory_reset_snapshot_items
        WHERE resource_type=? AND resource_id=?
          AND snapshot_id IN (
              SELECT id FROM memory_reset_snapshots WHERE user_id=?
          )
        """,
        (resource_type, resource_id, user_id),
    )
    return cursor.rowcount or 0


async def _delete_empty_snapshots(
    db: aiosqlite.Connection,
    *,
    user_id: str,
) -> int:
    cursor = await db.execute(
        """
        DELETE FROM memory_reset_snapshots
        WHERE user_id=? AND schema_version=3
          AND NOT EXISTS (
              SELECT 1
              FROM memory_reset_snapshot_items
              WHERE snapshot_id=memory_reset_snapshots.id
                AND payload_json<>'{}'
          )
        """,
        (user_id,),
    )
    return cursor.rowcount or 0


async def _delete_legacy_snapshots(
    db: aiosqlite.Connection,
    *,
    user_id: str,
) -> int:
    return await _delete_where(
        db,
        "memory_reset_snapshots",
        "user_id=? AND schema_version<3",
        (user_id,),
    )


async def _redact_candidate_ids(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    candidate_ids: set[str],
    now: str,
) -> int:
    if not candidate_ids or not await _table_exists(db, "memory_candidates"):
        return 0
    columns = await _column_names(db, "memory_candidates")
    assignments = [
        "status='rejected'",
        "key=''",
        "value_json='null'",
        "reason=NULL",
        "source_turn_id=NULL",
        "workspace_id=NULL",
        "updated_at=?",
    ]
    params: list[Any] = [now]
    optional_assignments = {
        "decision_kind": "'permanent_delete'",
        "target_scope_type": "NULL",
        "target_workspace_id": "NULL",
        "approved_resource_type": "NULL",
        "approved_resource_id": "NULL",
        "duplicate_of_candidate_id": "NULL",
        "content_fingerprint": "NULL",
        "content_redacted_at": "?",
    }
    for column, expression in optional_assignments.items():
        if column in columns:
            assignments.append(f"{column}={expression}")
            if expression == "?":
                params.append(now)
    if "version" in columns:
        assignments.append("version=version+1")
    placeholders = ",".join("?" for _ in candidate_ids)
    params.extend(sorted(candidate_ids))
    params.append(user_id)
    cursor = await db.execute(
        f"""
        UPDATE memory_candidates
        SET {", ".join(assignments)}
        WHERE id IN ({placeholders}) AND user_id=?
        """,
        tuple(params),
    )
    return cursor.rowcount or 0


async def _all_personal_memory_user_ids(
    db: aiosqlite.Connection,
) -> set[str]:
    specs = (
        ("memory_entries", "subject_id", "subject_type='user'"),
        ("memory_notes", "subject_id", "subject_type='user'"),
        ("memory_note_anchors", "user_id", "1=1"),
        ("memory_candidates", "user_id", "1=1"),
        ("memory_reset_snapshots", "user_id", "1=1"),
        ("memory_pack_snapshots", "user_id", "1=1"),
        ("memory_turn_requests", "user_id", "1=1"),
        ("memory_privacy_state", "user_id", "1=1"),
    )
    user_ids: set[str] = set()
    for table, column, where in specs:
        if not await _table_exists(db, table):
            continue
        async with db.execute(
            f"""
            SELECT DISTINCT {column}
            FROM {table}
            WHERE {where} AND {column}<>''
            """,
        ) as cursor:
            user_ids.update(str(row[0]) for row in await cursor.fetchall() if row[0])
    return user_ids


async def _users_with_expired_trash(
    db: aiosqlite.Connection,
    *,
    cutoff: str,
) -> set[str]:
    specs = (
        (
            "memory_entries",
            "subject_id",
            ("subject_type='user' AND deleted_at IS NOT NULL AND datetime(deleted_at)<datetime(?)"),
        ),
        (
            "memory_notes",
            "subject_id",
            ("subject_type='user' AND deleted_at IS NOT NULL AND datetime(deleted_at)<datetime(?)"),
        ),
        (
            "memory_reset_snapshots",
            "user_id",
            (
                "schema_version=3 AND snapshot_type='item_delete' "
                "AND consumed_at IS NULL AND expires_at IS NOT NULL "
                "AND datetime(expires_at)<=datetime(?, '+30 days')"
            ),
        ),
        (
            "memory_reset_snapshots",
            "user_id",
            ("schema_version<3 AND datetime(created_at)<datetime(?)"),
        ),
        (
            "memory_reset_snapshots",
            "user_id",
            (
                "schema_version=3 AND snapshot_type='reset' "
                "AND consumed_at IS NULL AND expires_at IS NOT NULL "
                "AND datetime(expires_at)<=datetime(?, '+30 days')"
            ),
        ),
    )
    user_ids: set[str] = set()
    for table, column, where in specs:
        if not await _table_exists(db, table):
            continue
        async with db.execute(
            f"""
            SELECT DISTINCT {column}
            FROM {table}
            WHERE {where}
            """,
            (cutoff,),
        ) as cursor:
            user_ids.update(str(row[0]) for row in await cursor.fetchall() if row[0])
    return user_ids


async def _users_with_expired_auxiliary(
    db: aiosqlite.Connection,
    *,
    cutoff: str,
) -> set[str]:
    specs = (
        (
            "memory_pack_snapshots",
            "user_id",
            "datetime(created_at)<datetime(?)",
        ),
        (
            "memory_note_anchors",
            "user_id",
            ("invalidated_at IS NOT NULL AND datetime(invalidated_at)<datetime(?)"),
        ),
    )
    user_ids: set[str] = set()
    for table, column, where in specs:
        if not await _table_exists(db, table):
            continue
        async with db.execute(
            f"""
            SELECT DISTINCT {column}
            FROM {table}
            WHERE {where}
            """,
            (cutoff,),
        ) as cursor:
            user_ids.update(str(row[0]) for row in await cursor.fetchall() if row[0])
    return user_ids


async def _replay_full_user_delete(
    db: aiosqlite.Connection,
    *,
    user_id: str,
) -> int:
    now = _now()
    total = 0
    total += await _delete_where_if_exists(
        db,
        "memory_entries",
        "subject_type='user' AND subject_id=?",
        (user_id,),
    )
    total += await _delete_where_if_exists(
        db,
        "memory_notes",
        "subject_type='user' AND subject_id=?",
        (user_id,),
    )
    total += await _delete_where_if_exists(
        db,
        "memory_note_anchors",
        "user_id=?",
        (user_id,),
    )
    total += await _redact_candidates(
        db,
        user_id=user_id,
        now=now,
    )
    total += await _delete_where_if_exists(
        db,
        "memory_candidate_suppressions",
        "user_id=?",
        (user_id,),
    )
    total += await _delete_where_if_exists(
        db,
        "memory_mutation_idempotency",
        "user_id=?",
        (user_id,),
    )
    total += await _delete_reset_snapshots(db, user_id=user_id)
    total += await _delete_pack_snapshots(db, user_id=user_id)
    total += await _delete_where_if_exists(
        db,
        "memory_turn_requests",
        "user_id=?",
        (user_id,),
    )
    total += await _scrub_memory_audit(db, user_id=user_id)
    await _resolve_memory_notifications(
        db,
        user_id=user_id,
        now=now,
    )
    if await _table_exists(db, "user_settings"):
        await db.execute(
            """
            INSERT INTO user_settings(user_id, key, value, updated_at)
            VALUES (?, 'memory_dreaming_paused', 'true', ?)
            ON CONFLICT(user_id, key) DO UPDATE SET
                value='true', updated_at=excluded.updated_at
            """,
            (user_id, now),
        )
    return total


async def _replay_resource_delete(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    receipt: dict[str, Any],
) -> int:
    resource_type = str(receipt["resource_type"] or "")
    resource_id = str(receipt["resource_id"] or "")
    deleted_through_version = int(
        receipt["deleted_through_version"] or 0,
    )
    now = _now()
    total = 0
    linked_candidate_ids: set[str] = set()
    if resource_type == "memory_entry":
        linked = await _linked_candidate_id(
            db,
            table="memory_entries",
            id_column="id",
            resource_id=resource_id,
            user_column="subject_id",
            user_id=user_id,
        )
        if linked:
            linked_candidate_ids.add(linked)
        total += await _delete_where_if_exists(
            db,
            "memory_entries",
            ("id=? AND subject_type='user' AND subject_id=? AND version<=?"),
            (resource_id, user_id, deleted_through_version),
        )
        total += await _delete_snapshot_copies(
            db,
            user_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
    elif resource_type == "memory_note":
        total += await _delete_where_if_exists(
            db,
            "memory_notes",
            ("id=? AND subject_type='user' AND subject_id=? AND version<=?"),
            (resource_id, user_id, deleted_through_version),
        )
        total += await _delete_where_if_exists(
            db,
            "memory_note_anchors",
            "user_id=?",
            (user_id,),
        )
        total += await _delete_snapshot_copies(
            db,
            user_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
    elif resource_type in {
        "item_delete_snapshot",
        "reset_snapshot",
    }:
        total += await _delete_where_if_exists(
            db,
            "memory_reset_snapshot_items",
            "snapshot_id=?",
            (resource_id,),
        )
        total += await _delete_where_if_exists(
            db,
            "memory_reset_snapshots",
            "id=? AND user_id=?",
            (resource_id, user_id),
        )
    elif resource_type == "memory_pack_snapshot":
        total += await _delete_where_if_exists(
            db,
            "memory_pack_snapshots",
            "turn_id=? AND user_id=?",
            (resource_id, user_id),
        )
    elif resource_type == "note_anchor":
        total += await _delete_where_if_exists(
            db,
            "memory_note_anchors",
            "id=? AND user_id=?",
            (resource_id, user_id),
        )
    total += await _redact_candidate_ids(
        db,
        user_id=user_id,
        candidate_ids=linked_candidate_ids,
        now=now,
    )
    if resource_type not in {
        "memory_pack_snapshot",
        "note_anchor",
    }:
        total += await _delete_pack_snapshots(db, user_id=user_id)
        total += await _scrub_memory_audit(db, user_id=user_id)
        await _resolve_memory_notifications(
            db,
            user_id=user_id,
            now=now,
        )
    return total


async def _assert_destructive_owner(
    db: aiosqlite.Connection,
    *,
    operation_id: str,
    user_id: str,
    privacy_epoch: int,
) -> None:
    async with db.execute(
        """
        SELECT current_epoch, barrier_status, barrier_operation_id
        FROM memory_privacy_state
        WHERE user_id=?
        """,
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or int(row[0]) != privacy_epoch or str(row[1]) != "deleting" or str(row[2]) != operation_id:
        raise PrivacyDeletionNotReadyError(
            "destructive operation lost its privacy barrier",
        )


async def _count_memory_entries(
    db: aiosqlite.Connection,
    *,
    user_id: str,
) -> tuple[int, int]:
    async with db.execute(
        """
        SELECT
            SUM(CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END)
        FROM memory_entries
        WHERE subject_type='user' AND subject_id=?
        """,
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return (
        int(row[0] or 0) if row else 0,
        int(row[1] or 0) if row else 0,
    )


async def _redact_candidates(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    now: str,
) -> int:
    if not await _table_exists(db, "memory_candidates"):
        return 0
    columns = await _column_names(db, "memory_candidates")
    assignments = [
        "status='rejected'",
        "key=''",
        "value_json='null'",
        "reason=NULL",
        "source_turn_id=NULL",
        "workspace_id=NULL",
        "updated_at=?",
    ]
    params: list[Any] = [now]
    optional_assignments = {
        "decision_kind": "'permanent_delete'",
        "target_scope_type": "NULL",
        "target_workspace_id": "NULL",
        "approved_resource_type": "NULL",
        "approved_resource_id": "NULL",
        "duplicate_of_candidate_id": "NULL",
        "content_fingerprint": "NULL",
        "content_redacted_at": "?",
    }
    for column, expression in optional_assignments.items():
        if column in columns:
            assignments.append(f"{column}={expression}")
            if expression == "?":
                params.append(now)
    if "version" in columns:
        assignments.append("version=version+1")
    params.append(user_id)
    cursor = await db.execute(
        f"""
        UPDATE memory_candidates
        SET {", ".join(assignments)}
        WHERE user_id=?
        """,
        tuple(params),
    )
    return cursor.rowcount or 0


async def _delete_reset_snapshots(
    db: aiosqlite.Connection,
    *,
    user_id: str,
) -> int:
    if not await _table_exists(db, "memory_reset_snapshots"):
        return 0
    if await _table_exists(db, "memory_reset_snapshot_items"):
        await db.execute(
            """
            DELETE FROM memory_reset_snapshot_items
            WHERE snapshot_id IN (
                SELECT id FROM memory_reset_snapshots WHERE user_id=?
            )
            """,
            (user_id,),
        )
    return await _delete_where(
        db,
        "memory_reset_snapshots",
        "user_id=?",
        (user_id,),
    )


async def _delete_pack_snapshots(
    db: aiosqlite.Connection,
    *,
    user_id: str,
) -> int:
    if not await _table_exists(db, "memory_pack_snapshots"):
        return 0
    if await _table_exists(db, "memory_pack_snapshot_refs"):
        await db.execute(
            "DELETE FROM memory_pack_snapshot_refs WHERE user_id=?",
            (user_id,),
        )
    return await _delete_where(
        db,
        "memory_pack_snapshots",
        "user_id=?",
        (user_id,),
    )


async def _scrub_memory_audit(
    db: aiosqlite.Connection,
    *,
    user_id: str,
) -> int:
    if not await _table_exists(db, "user_audit_log"):
        return 0
    columns = await _column_names(db, "user_audit_log")
    event_column = "event_type" if "event_type" in columns else "action"
    payload_column = "payload_json" if "payload_json" in columns else "details_json"
    if event_column not in columns or payload_column not in columns:
        return 0
    cursor = await db.execute(
        f"""
        UPDATE user_audit_log
        SET {payload_column}='{{"redacted":true,"outcome":"permanent_delete"}}'
        WHERE user_id=? AND {event_column} LIKE 'memory.%'
        """,
        (user_id,),
    )
    return cursor.rowcount or 0


async def _resolve_memory_notifications(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    now: str,
) -> None:
    if not await _table_exists(db, "notification_items"):
        return
    columns = await _column_names(db, "notification_items")
    if "type" not in columns:
        return
    recipient_join = ""
    where = "1=0"
    params: tuple[Any, ...] = ()
    if "owner_user_id" in columns and "owner_scope" in columns and ("status" in columns or "state" in columns):
        where = "owner_scope='account' AND owner_user_id=? AND type LIKE 'memory.%'"
        params = (user_id,)
    elif await _table_exists(db, "notification_recipients"):
        recipient_join = " AND id IN (SELECT notification_id FROM notification_recipients WHERE user_id=?)"
        where = "type LIKE 'memory.%'"
        params = (user_id,)
    assignments = []
    if "state" in columns:
        assignments.append("state='resolved'")
    if "status" in columns:
        assignments.append("status='resolved'")
    if "resolved_at" in columns:
        assignments.append("resolved_at=?")
        params = (now, *params)
    if "resolved_reason" in columns:
        assignments.append("resolved_reason='memory_permanent_delete'")
    if "body" in columns:
        assignments.append("body='Memory suggestions removed'")
    if "metadata_json" in columns:
        assignments.append("metadata_json='{}'")
    if "updated_at" in columns:
        assignments.append("updated_at=?")
        params = (now, *params)
    if assignments:
        await db.execute(
            f"""
            UPDATE notification_items
            SET {", ".join(assignments)}
            WHERE {where}{recipient_join}
            """,
            params,
        )


async def _delete_where_if_exists(
    db: aiosqlite.Connection,
    table: str,
    where: str,
    params: tuple[Any, ...],
) -> int:
    if not await _table_exists(db, table):
        return 0
    return await _delete_where(db, table, where, params)


async def _delete_where(
    db: aiosqlite.Connection,
    table: str,
    where: str,
    params: tuple[Any, ...],
) -> int:
    cursor = await db.execute(
        f"DELETE FROM {table} WHERE {where}",
        params,
    )
    return cursor.rowcount or 0


async def _count_rows(
    db: aiosqlite.Connection,
    table: str,
    where: str,
    params: tuple[Any, ...],
) -> int:
    if not await _table_exists(db, table):
        return 0
    async with db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {where}",
        params,
    ) as cursor:
        row = await cursor.fetchone()
    return int(row[0] or 0) if row else 0


async def _table_exists(
    db: aiosqlite.Connection,
    table: str,
) -> bool:
    async with db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ) as cursor:
        return await cursor.fetchone() is not None


async def _column_names(
    db: aiosqlite.Connection,
    table: str,
) -> set[str]:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        return {str(row[1]) for row in await cursor.fetchall()}


def _operation_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    result = json.loads(row["result_json"] or "{}")
    return {
        "operation_id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "action": row["operation_type"],
        "target_id": row["target_id"],
        "status": row["status"],
        "phase": row["phase"],
        "cutoff_at": row["cutoff_at"],
        "privacy_epoch": row["privacy_epoch"],
        "progress_current": row["progress_current"],
        "progress_total": row["progress_total"],
        "retryable": row["status"] in {"failed", "partially_failed"},
        "last_error_code": row["last_error_code"],
        "completed_at": row["completed_at"],
        "manifest_digest": result.get("manifest_digest"),
        **{key: value for key, value in result.items() if key != "manifest_digest"},
    }


def _manifest_digest(manifest: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _receipt_id(operation_id: str, target: str) -> str:
    digest = hashlib.sha256(
        f"{operation_id}:{target}".encode(),
    ).hexdigest()
    return f"pdr_{digest}"


def _validate_idempotency_key(value: str) -> None:
    if len(value) > 36:
        raise ValueError("Idempotency-Key must be a canonical UUID")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("Idempotency-Key must be a canonical UUID")


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="microseconds")


__all__ = [
    "MemoryDestructiveOperationConflictError",
    "MemoryDestructiveOperationStore",
    "MemoryDestructiveTargetNotFoundError",
    "MemoryIdempotencyConflictError",
    "MemoryMutationContext",
    "MemoryWriteBlockedError",
    "PrivacyDeletionLedger",
    "PrivacyDeletionNotReadyError",
    "current_memory_mutation_context",
    "ensure_memory_write_allowed",
    "is_memory_read_hidden",
    "reset_memory_mutation_context",
    "start_memory_mutation_context",
]
