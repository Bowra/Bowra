"""Signed, content-free account retirement intents outside business backups."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import aiosqlite

from bowhead_core.account_deletion import AccountDeletion, AccountDeletionError, retire_account_conn
from bowhead_core.user_store import UserStore


class AccountDeletionLedger:
    def __init__(self, path: Path, *, secret: str) -> None:
        if not secret:
            raise AccountDeletionError("account_erasure_key_unavailable")
        self.path = path
        self._key = hmac.digest(secret.encode(), b"bowhead-account-deletion-v1", "sha256")

    def subject(self, user_id: str) -> str:
        return hmac.new(self._key, user_id.encode(), hashlib.sha256).hexdigest()

    def _signature(self, subject: str, requested_at: str, purge_after: str) -> str:
        return hmac.new(self._key, json.dumps([subject, requested_at, purge_after]).encode(), hashlib.sha256).hexdigest()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS account_retirements (subject TEXT PRIMARY KEY,requested_at TEXT NOT NULL,purge_after TEXT NOT NULL,signature TEXT NOT NULL)"
            )
            await db.commit()

    async def record(self, deletion: AccountDeletion) -> None:
        subject = self.subject(deletion.user_id)
        signature = self._signature(subject, deletion.requested_at, deletion.purge_after)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO account_retirements VALUES (?,?,?,?)", (subject, deletion.requested_at, deletion.purge_after, signature)
            )
            async with db.execute("SELECT requested_at,purge_after,signature FROM account_retirements WHERE subject=?", (subject,)) as cur:
                row = await cur.fetchone()
            if row != (deletion.requested_at, deletion.purge_after, signature):
                raise AccountDeletionError("account_deletion_ledger_mismatch")
            await db.commit()

    async def replay(self, store: UserStore) -> int:
        """Must finish before restored business data is served or jobs are started."""
        from datetime import datetime

        async with (
            aiosqlite.connect(self.path) as ledger,
            ledger.execute("SELECT subject,requested_at,purge_after,signature FROM account_retirements") as cur,
        ):
            receipts = {row[0]: row for row in await cur.fetchall()}
        for subject, requested_at, purge_after, signature in receipts.values():
            if not hmac.compare_digest(signature, self._signature(subject, requested_at, purge_after)):
                raise AccountDeletionError("account_deletion_ledger_signature_invalid")
        restored = 0
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute("SELECT user_id,requested_at,purge_after FROM account_deletions") as cur:
                    enrolled = await cur.fetchall()
                for user_id, requested_at, purge_after in enrolled:
                    receipt = receipts.get(self.subject(user_id))
                    if receipt is None or (receipt[1], receipt[2]) != (requested_at, purge_after):
                        raise AccountDeletionError("account_deletion_ledger_incomplete")
                async with db.execute("SELECT id FROM users") as cur:
                    ids = [row[0] for row in await cur.fetchall()]
                for user_id in ids:
                    receipt = receipts.get(self.subject(user_id))
                    if receipt is None:
                        continue
                    result = await retire_account_conn(db, store, user_id, requested_at=datetime.fromisoformat(receipt[1]), time_source="audit")
                    if (result.requested_at, result.purge_after) != (receipt[1], receipt[2]):
                        raise AccountDeletionError("account_deletion_ledger_mismatch")
                    restored += 1
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return restored
