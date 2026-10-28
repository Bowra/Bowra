"""Lease-based account cleanup with durable storage acknowledgements."""

from __future__ import annotations

import sqlite3
import traceback
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import aiosqlite
import anyio
import structlog

from bowhead_core.account_artifact_lock import account_artifact_lock
from bowhead_core.account_deletion import (
    AccountDeletion,
    AccountDeletionError,
    adopt_deleted_accounts,
    request_account_deletion,
)
from bowhead_core.account_deletion_ledger import AccountDeletionLedger
from bowhead_core.account_erasure import erase_rows, freeze_rows, key_sql, protocol_receipts, reviewed_schema
from bowhead_core.session_lifecycle import SessionLifecycleError

if TYPE_CHECKING:
    from bowhead_core.artifact_store import ArtifactStore
    from bowhead_core.session_lifecycle import SessionLifecycleCoordinator
    from bowhead_core.user_store import UserStore

logger = structlog.get_logger(__name__)


class AccountDeletionService:
    def __init__(
        self,
        users: UserStore,
        *,
        lifecycle: SessionLifecycleCoordinator,
        artifacts: ArtifactStore,
        ledger: AccountDeletionLedger,
        secret: str,
        external_cleanup: Callable[[str, str], Awaitable[None]],
        external_quarantine: Callable[[str, str], Awaitable[None]] | None = None,
        erasure_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.users = users
        self.lifecycle = lifecycle
        self.artifacts = artifacts
        self.ledger = ledger
        self.secret = secret
        self.external_cleanup = external_cleanup
        self.external_quarantine = external_quarantine
        self.erasure_guard = erasure_guard
        self.clock = clock
        self.owner = uuid4().hex
        self._wake = anyio.Event()
        self.users.account_deletion_recorder = ledger.record

    def wake(self) -> None:
        self._wake.set()

    async def request(self, user_id: str, *, expected_pwd_version: int | None = None) -> AccountDeletion:
        async with aiosqlite.connect(self.users.db_path) as db:
            await reviewed_schema(db)
        if getattr(self.artifacts.backend, "erase", None) is None:
            raise AccountDeletionError("account_erasure_backend_unavailable")
        check_ready = getattr(self.external_cleanup, "check_ready", None)
        if check_ready is not None:
            await check_ready()
        result = await request_account_deletion(
            self.users, user_id, now=self.clock(), record_intent=self.ledger.record, expected_pwd_version=expected_pwd_version
        )
        self.wake()
        return result

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("account_deletion.sweep_failed")
            with anyio.move_on_after(60):
                await self._wake.wait()
            self._wake = anyio.Event()

    async def run_once(self, *, limit: int = 20) -> dict[str, int]:
        await adopt_deleted_accounts(self.users, now=self.clock())
        now = self.clock().isoformat()
        async with (
            aiosqlite.connect(self.users.db_path) as db,
            db.execute(
                "SELECT user_id,operation_id,requested_at,purge_after FROM account_deletions "
                "WHERE state!='completed' AND (next_attempt_at IS NULL OR julianday(next_attempt_at)<=julianday(?)) "
                "AND (quarantined_at IS NULL OR julianday(purge_after)<=julianday(?)) "
                "AND (lease_expires_at IS NULL OR julianday(lease_expires_at)<=julianday(?)) "
                "ORDER BY attempt_count,purge_after,user_id LIMIT ?",
                (now, now, now, limit),
            ) as cur,
        ):
            rows = await cur.fetchall()
        completed = failed = 0
        for row in rows:
            deletion = AccountDeletion(*row)
            epoch = await self._claim(deletion)
            if epoch is None:
                continue
            try:
                failure = None
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(self._heartbeat, deletion, epoch)
                    try:
                        await self.ledger.record(deletion)
                        await self._quarantine(deletion)
                        if self.clock() >= datetime.fromisoformat(deletion.purge_after):
                            await self._erase(deletion, epoch)
                            completed += 1
                    except Exception as exc:
                        failure = exc
                    finally:
                        tasks.cancel_scope.cancel()
                if failure is not None:
                    raise failure
                await self._release(deletion, epoch, None)
            except Exception as exc:
                failed += 1
                code = (
                    exc.code
                    if isinstance(exc, (AccountDeletionError, SessionLifecycleError))
                    else (
                        "account_erasure_dependency_blocked"
                        if isinstance(exc, aiosqlite.IntegrityError)
                        else "account_storage_checkpoint_pending"
                        if getattr(exc, "sqlite_errorcode", None) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                        else "account_storage_delete_failed"
                        if isinstance(exc, OSError)
                        else "account_deletion_cleanup_failed"
                    )
                )
                # Never log third-party exception text: it may contain tokens.
                frames = [(frame.name, frame.lineno) for frame in traceback.extract_tb(exc.__traceback__)[-3:]]
                logger.warning(
                    "account_deletion.pending", operation_id=deletion.operation_id, code=code, error_type=type(exc).__name__, frames=frames
                )
                await self._release(deletion, epoch, code)
        if len(rows) == limit:
            self.wake()
        return {"completed": completed, "failed": failed}

    async def _claim(self, deletion: AccountDeletion) -> int | None:
        now = self.clock()
        async with aiosqlite.connect(self.users.db_path) as db:
            cur = await db.execute(
                "UPDATE account_deletions SET lease_owner=?,lease_epoch=lease_epoch+1,lease_expires_at=?,attempt_count=attempt_count+1 "
                "WHERE operation_id=? AND state!='completed' AND (lease_expires_at IS NULL OR julianday(lease_expires_at)<=julianday(?)) RETURNING lease_epoch",
                (self.owner, (now + timedelta(minutes=5)).isoformat(), deletion.operation_id, now.isoformat()),
            )
            row = await cur.fetchone()
            await cur.close()
            await db.commit()
        return int(row[0]) if row else None

    async def _release(self, deletion: AccountDeletion, epoch: int, code: str | None) -> None:
        retry_at = (self.clock() + timedelta(minutes=5)).isoformat() if code else None
        async with aiosqlite.connect(self.users.db_path) as db:
            await db.execute(
                "UPDATE account_deletions SET lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=?,last_code=? "
                "WHERE operation_id=? AND lease_owner=? AND lease_epoch=?",
                (retry_at, code, deletion.operation_id, self.owner, epoch),
            )
            await db.commit()

    async def _assert_lease(self, db: aiosqlite.Connection, deletion: AccountDeletion, epoch: int) -> None:
        cur = await db.execute(
            "UPDATE account_deletions SET lease_expires_at=? WHERE operation_id=? AND lease_owner=? AND lease_epoch=? AND julianday(lease_expires_at)>julianday(?)",
            ((self.clock() + timedelta(minutes=5)).isoformat(), deletion.operation_id, self.owner, epoch, self.clock().isoformat()),
        )
        if cur.rowcount != 1:
            raise AccountDeletionError("account_deletion_lease_lost")

    async def _heartbeat(self, deletion: AccountDeletion, epoch: int) -> None:
        while True:
            await anyio.sleep(30)
            async with aiosqlite.connect(self.users.db_path) as db:
                try:
                    await self._assert_lease(db, deletion, epoch)
                    await db.commit()
                except aiosqlite.OperationalError as exc:
                    if getattr(exc, "sqlite_errorcode", None) not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                        raise

    async def _quarantine(self, deletion: AccountDeletion) -> None:
        if self.external_quarantine is not None:
            await self.external_quarantine(deletion.user_id, deletion.operation_id)
        async with (
            aiosqlite.connect(self.users.db_path) as db,
            db.execute("SELECT id FROM sessions WHERE user_id=? ORDER BY id", (deletion.user_id,)) as cur,
        ):
            sessions = [row[0] for row in await cur.fetchall()]
        for session_id in sessions:
            await self.lifecycle.soft_delete(
                session_id=session_id,
                actor_audit_ref=deletion.user_id,
                request_id=f"account-retirement:{deletion.operation_id}:{session_id}",
                expected_user_id=deletion.user_id,
            )
        async with aiosqlite.connect(self.users.db_path) as db:
            await db.execute(
                "UPDATE account_deletions SET quarantined_at=COALESCE(quarantined_at,?),state=CASE WHEN state='retained' THEN 'quarantined' ELSE state END WHERE operation_id=?",
                (self.clock().isoformat(), deletion.operation_id),
            )
            await db.commit()

    async def _erase(self, deletion: AccountDeletion, epoch: int) -> None:
        async with (
            account_artifact_lock(self.users.db_path, deletion.user_id, erasing=True),
            self.erasure_guard() if self.erasure_guard is not None else nullcontext(),
        ):
            await self._erase_guarded(deletion, epoch)

    async def _erase_guarded(self, deletion: AccountDeletion, epoch: int) -> None:
        async with (
            aiosqlite.connect(self.users.db_path) as db,
            db.execute("SELECT state FROM account_deletions WHERE operation_id=?", (deletion.operation_id,)) as cur,
        ):
            operation = await cur.fetchone()
        if operation and operation[0] == "verifying":
            await self._finish_verification(deletion, epoch)
            return
        async with aiosqlite.connect(self.users.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_lease(db, deletion, epoch)
                # A future protocol must supply its own erasure adapter; never
                # interpret unknown execution semantics as a legacy Turn.
                async with db.execute(
                    "SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id WHERE s.user_id=? AND t.terminal_protocol_version NOT IN (1) LIMIT 1",
                    (deletion.user_id,),
                ) as cur:
                    if await cur.fetchone():
                        raise AccountDeletionError("terminal_protocol_future_version")
                primary = await freeze_rows(db, deletion.operation_id, deletion.user_id)
                # External evidence requires an authenticated protocol resolver.
                async with db.execute(
                    "SELECT e.audit_object_ref FROM turn_effect_evidence_links e WHERE e.audit_object_ref IS NOT NULL AND "
                    "json_array(e.evidence_link_id) IN (SELECT row_key FROM account_deletion_rows WHERE operation_id=? AND table_name='turn_effect_evidence_links')",
                    (deletion.operation_id,),
                ) as cur:
                    evidence = await cur.fetchall()
                for (reference,) in evidence:
                    # These are locators for local ledgers, not provider response
                    # objects. Both source rows are included in this transaction's
                    # ownership closure. Arbitrary external locators need a resolver.
                    prefix, _, identity = reference.partition(":")
                    if prefix == "invocation" and len(identity) == 64 and all(c in "0123456789abcdef" for c in identity):
                        continue
                    if prefix == "credit-ledger":
                        async with db.execute(
                            "SELECT 1 FROM credit_ledger WHERE id=? AND user_id=?",
                            (identity, deletion.user_id),
                        ) as cur:
                            if await cur.fetchone():
                                continue
                    raise AccountDeletionError("account_protocol_evidence_purge_required")
                object_columns = (
                    ("artifacts", "path"),
                    ("document_draft_files", "path"),
                    ("document_input_copies", "path"),
                    ("document_revision_inputs", "path"),
                    ("account_owned_artifact_writes", "path"),
                    ("artifact_purge_claims", "path"),
                    ("artifact_upload_confirmations", "quarantine_path"),
                    ("artifact_upload_confirmations", "candidate_path"),
                )
                for table, column in object_columns:
                    await db.execute(
                        f"INSERT OR IGNORE INTO account_deletion_objects(operation_id,storage_kind,object_key) "
                        f"SELECT ?,'artifact',{column} FROM {table} WHERE {column} IS NOT NULL AND {column}!='' AND {key_sql(primary[table])} IN "
                        "(SELECT row_key FROM account_deletion_rows WHERE operation_id=? AND table_name=?) "
                        f"AND NOT EXISTS(SELECT 1 FROM artifacts other WHERE other.path={table}.{column} AND json_array(other.id) NOT IN "
                        "(SELECT row_key FROM account_deletion_rows WHERE operation_id=? AND table_name='artifacts'))",
                        (deletion.operation_id, deletion.operation_id, table, deletion.operation_id),
                    )
                await db.execute("UPDATE account_deletions SET state='purging' WHERE operation_id=?", (deletion.operation_id,))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        # Cross-database/index cleanup owns its own durable barriers. A failure
        # here retains the source graph and exact object inventory for retry.
        await self.external_cleanup(deletion.user_id, deletion.operation_id)
        async with (
            aiosqlite.connect(self.users.db_path) as db,
            db.execute(
                "SELECT storage_kind,object_key FROM account_deletion_objects WHERE operation_id=? AND completed_at IS NULL", (deletion.operation_id,)
            ) as cur,
        ):
            objects = await cur.fetchall()
        for storage_kind, key in objects:
            if storage_kind != "artifact":
                raise AccountDeletionError("account_erasure_storage_unclassified")
            erase = getattr(self.artifacts.backend, "erase", None)
            if erase is None:
                raise AccountDeletionError("account_erasure_backend_unavailable")
            await erase(key)
            async with aiosqlite.connect(self.users.db_path) as db:
                await self._assert_lease(db, deletion, epoch)
                await db.execute(
                    "UPDATE account_deletion_objects SET completed_at=? WHERE operation_id=? AND storage_kind=? AND object_key=?",
                    (self.clock().isoformat(), deletion.operation_id, storage_kind, key),
                )
                await db.commit()
        # Preserve only the opaque retired principal needed by shared-resource
        # creator FKs. All personal attributes, credentials and settings vanish.
        async with aiosqlite.connect(self.users.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA secure_delete=ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_lease(db, deletion, epoch)
                await reviewed_schema(db)
                await protocol_receipts(db, deletion.user_id, secret=self.secret, erased_at=self.clock().isoformat())
                await erase_rows(db, deletion.operation_id, primary)
                await db.execute("INSERT INTO canvas_fts(canvas_fts) VALUES ('rebuild')")
                await db.execute(
                    "UPDATE users SET display_name=NULL,avatar_url=NULL,password_hash=NULL,oauth_providers_json='[]',preferences_json='{}',"
                    "email_verified=0,credit_balance=0,credit_quota=0,verify_token_jti=NULL,datarc_member_uid=NULL,active_team_id=NULL,"
                    "last_active_at=NULL,locale='auto',timezone='UTC' WHERE id=? AND status='deleted'",
                    (deletion.user_id,),
                )
                await db.execute(
                    "UPDATE admin_audit_log SET actor_user_id=CASE WHEN actor_user_id=? THEN 'retired' ELSE actor_user_id END,"
                    "target_id=CASE WHEN target_kind='user' AND target_id=? THEN NULL ELSE target_id END,payload_json='{}',ip=NULL,user_agent=NULL "
                    "WHERE actor_user_id=? OR (target_kind='user' AND target_id=?)",
                    (deletion.user_id, deletion.user_id, deletion.user_id, deletion.user_id),
                )
                await db.execute("DELETE FROM account_deletion_rows WHERE operation_id=?", (deletion.operation_id,))
                await db.execute("DELETE FROM account_deletion_objects WHERE operation_id=?", (deletion.operation_id,))
                await db.execute(
                    "UPDATE account_deletions SET state='verifying',last_code=NULL WHERE operation_id=?",
                    (deletion.operation_id,),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        await self._finish_verification(deletion, epoch)

    async def _finish_verification(self, deletion: AccountDeletion, epoch: int) -> None:
        async with aiosqlite.connect(self.users.db_path) as db:
            await self._assert_lease(db, deletion, epoch)
            await db.commit()
            await db.execute("VACUUM")
            async with db.execute("PRAGMA wal_checkpoint(TRUNCATE)") as cur:
                checkpoint = await cur.fetchone()
            if checkpoint is None or checkpoint[0] != 0:
                raise AccountDeletionError("account_storage_checkpoint_pending")
            await self._assert_lease(db, deletion, epoch)
            await db.execute(
                "UPDATE account_deletions SET state='completed',completed_at=?,last_code=NULL WHERE operation_id=? AND state='verifying'",
                (self.clock().isoformat(), deletion.operation_id),
            )
            await db.commit()
