"""Short-lived, encrypted, single-use Bowra authorization and import envelopes."""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from bowhead_core._db import open_shared_ctx
from bowhead_core.llm_config_crypto import decrypt_llm_config, encrypt_llm_config


class BowraConnectStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def put(self, *, team_id: str, user_id: str, kind: str, payload: dict[str, Any]) -> str:
        sealed = encrypt_llm_config(payload)
        if not sealed.startswith("FERNET:"):
            raise ValueError("bowra_encryption_required")
        token = secrets.token_urlsafe(32)
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("DELETE FROM bowra_connect_pending WHERE expires_at<=?", (time.time(),))
            await db.execute(
                "INSERT INTO bowra_connect_pending VALUES(?,?,?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), team_id, user_id, kind, sealed, time.time() + 600),
            )
            await db.commit()
        return token

    async def take(self, token: str, *, team_id: str, user_id: str, kind: str) -> dict[str, Any] | None:
        async with open_shared_ctx(self.db_path) as db:
            async with db.execute(
                "DELETE FROM bowra_connect_pending WHERE token_hash=? AND team_id=? AND user_id=? AND kind=? RETURNING payload_cipher,expires_at",
                (hashlib.sha256(token.encode()).hexdigest(), team_id, user_id, kind),
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
        if row is None or row[1] <= time.time():
            return None
        return decrypt_llm_config(row[0])
