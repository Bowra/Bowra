"""Per-member OAuth grants on team built-in tool connections (provider-agnostic).

A team administrator registers the customer's OAuth client on a ``team_builtin_tool``
row; each member then authorizes once and the refresh token is stored here, sealed
with a key derived from ``BOWHEAD_FERNET_KEY`` for a fixed purpose plus
``user_id``/``tool_id``. Nobody but the member's own runs can unseal it.

State rows are single use: :meth:`BuiltinToolGrantStore.consume_state` deletes the
row it returns, and only the SHA-256 of the opaque state is stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

from bowhead_core._db import open_shared_ctx

_KEY_CONTEXT = "builtin-tool-grant.v1"
STATE_TTL_SECONDS = 600


class BuiltinToolGrantError(Exception):
    """Stable, secret-free failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BuiltinToolGrant:
    id: str
    tool_id: str
    team_id: str
    user_id: str
    provider_subject: str
    provider_email: str
    granted_scopes: tuple[str, ...]
    status: str
    created_at: str
    updated_at: str
    last_used_at: str | None


@dataclass(frozen=True)
class PendingAuthorization:
    tool_id: str
    team_id: str
    user_id: str
    requested_scopes: tuple[str, ...]
    redirect_uri: str
    code_verifier: str


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _hash_state(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _grant(row: Any) -> BuiltinToolGrant:
    return BuiltinToolGrant(
        id=row["id"],
        tool_id=row["tool_id"],
        team_id=row["team_id"],
        user_id=row["user_id"],
        provider_subject=row["provider_subject"],
        provider_email=row["provider_email"],
        granted_scopes=tuple(json.loads(row["granted_scopes"] or "[]")),
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_used_at=row["last_used_at"],
    )


class BuiltinToolGrantStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def _cipher(self, user_id: str, tool_id: str, purpose: str) -> Fernet:
        root = os.environ.get("BOWHEAD_FERNET_KEY", "")
        try:
            raw = base64.urlsafe_b64decode(root.encode())
        except Exception:
            raise BuiltinToolGrantError("builtin_tool_grant_key_unavailable") from None
        if len(raw) != 32:
            raise BuiltinToolGrantError("builtin_tool_grant_key_unavailable")
        context = json.dumps([_KEY_CONTEXT, purpose, user_id, tool_id], separators=(",", ":"))
        return Fernet(base64.urlsafe_b64encode(hmac.digest(raw, context.encode(), "sha256")))

    def _seal(self, user_id: str, tool_id: str, purpose: str, value: str) -> str:
        return self._cipher(user_id, tool_id, purpose).encrypt(value.encode()).decode()

    def _unseal(self, user_id: str, tool_id: str, purpose: str, value: str) -> str:
        try:
            return self._cipher(user_id, tool_id, purpose).decrypt(value.encode()).decode()
        except InvalidToken:
            raise BuiltinToolGrantError("builtin_tool_grant_unreadable") from None

    async def create_state(
        self,
        *,
        tool_id: str,
        team_id: str,
        user_id: str,
        requested_scopes: tuple[str, ...],
        redirect_uri: str,
        code_verifier: str,
    ) -> str:
        state = secrets.token_urlsafe(32)
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("DELETE FROM builtin_tool_oauth_state WHERE expires_at<?", (_iso(now),))
            await db.execute(
                """INSERT INTO builtin_tool_oauth_state(
                     state_hash,tool_id,team_id,user_id,requested_scopes,redirect_uri,verifier_cipher,expires_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    _hash_state(state),
                    tool_id,
                    team_id,
                    user_id,
                    json.dumps(list(requested_scopes)),
                    redirect_uri,
                    self._seal(user_id, tool_id, "pkce", code_verifier),
                    _iso(now + timedelta(seconds=STATE_TTL_SECONDS)),
                ),
            )
            await db.commit()
        return state

    async def consume_state(self, state: str) -> PendingAuthorization | None:
        """Delete and return the pending authorization; expired or unknown → None."""
        if not state:
            return None
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("DELETE FROM builtin_tool_oauth_state WHERE state_hash=? RETURNING *", (_hash_state(state),))).fetchone()
            await db.commit()
        if row is None or row["expires_at"] < _iso(_now()):
            return None
        try:
            verifier = self._unseal(row["user_id"], row["tool_id"], "pkce", row["verifier_cipher"])
        except BuiltinToolGrantError:
            return None
        return PendingAuthorization(
            tool_id=row["tool_id"],
            team_id=row["team_id"],
            user_id=row["user_id"],
            requested_scopes=tuple(json.loads(row["requested_scopes"])),
            redirect_uri=row["redirect_uri"],
            code_verifier=verifier,
        )

    async def get(self, tool_id: str, user_id: str) -> BuiltinToolGrant | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM builtin_tool_user_grant WHERE tool_id=? AND user_id=?", (tool_id, user_id))).fetchone()
        return _grant(row) if row else None

    async def list_for_user(self, user_id: str, team_id: str) -> list[BuiltinToolGrant]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute("SELECT * FROM builtin_tool_user_grant WHERE user_id=? AND team_id=? ORDER BY created_at", (user_id, team_id))
            ).fetchall()
        return [_grant(row) for row in rows]

    async def count_active(self, tool_id: str) -> int:
        async with open_shared_ctx(self.db_path) as db:
            row = await (await db.execute("SELECT COUNT(*) FROM builtin_tool_user_grant WHERE tool_id=? AND status='active'", (tool_id,))).fetchone()
        return int(row[0]) if row else 0

    async def save(
        self,
        *,
        tool_id: str,
        team_id: str,
        user_id: str,
        provider_subject: str,
        provider_email: str,
        granted_scopes: tuple[str, ...],
        refresh_token: str | None,
    ) -> tuple[BuiltinToolGrant, str | None]:
        """Upsert the member's grant; return it plus a superseded refresh token to revoke.

        Incremental consent may return no refresh token — the existing one is kept.
        Without a new token and without an existing grant the save fails closed.
        """
        now = _iso(_now())
        scopes = json.dumps(sorted(set(granted_scopes)))
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            existing = await (await db.execute("SELECT * FROM builtin_tool_user_grant WHERE tool_id=? AND user_id=?", (tool_id, user_id))).fetchone()
            superseded: str | None = None
            if existing is None:
                if not refresh_token:
                    raise BuiltinToolGrantError("builtin_tool_grant_refresh_token_missing")
                await db.execute(
                    """INSERT INTO builtin_tool_user_grant(
                         id,tool_id,team_id,user_id,provider_subject,provider_email,granted_scopes,
                         refresh_cipher,status,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,'active',?,?)""",
                    (
                        f"btg_{uuid4().hex[:16]}",
                        tool_id,
                        team_id,
                        user_id,
                        provider_subject,
                        provider_email,
                        scopes,
                        self._seal(user_id, tool_id, "refresh", refresh_token),
                        now,
                        now,
                    ),
                )
            else:
                if existing["provider_subject"] != provider_subject and not refresh_token:
                    raise BuiltinToolGrantError("builtin_tool_grant_refresh_token_missing")
                cipher = existing["refresh_cipher"]
                if refresh_token:
                    try:
                        superseded = self._unseal(user_id, tool_id, "refresh", cipher)
                    except BuiltinToolGrantError:
                        superseded = None
                    if superseded == refresh_token:
                        superseded = None
                    cipher = self._seal(user_id, tool_id, "refresh", refresh_token)
                await db.execute(
                    """UPDATE builtin_tool_user_grant
                       SET provider_subject=?,provider_email=?,granted_scopes=?,refresh_cipher=?,status='active',updated_at=?
                       WHERE tool_id=? AND user_id=?""",
                    (provider_subject, provider_email, scopes, cipher, now, tool_id, user_id),
                )
            await db.commit()
            row = await (await db.execute("SELECT * FROM builtin_tool_user_grant WHERE tool_id=? AND user_id=?", (tool_id, user_id))).fetchone()
        return _grant(row), superseded

    async def refresh_token(self, grant: BuiltinToolGrant) -> str:
        return (await self.refresh_token_versioned(grant))[0]

    async def refresh_token_versioned(self, grant: BuiltinToolGrant) -> tuple[str, str]:
        """(refresh token, stored ciphertext); the ciphertext identifies this token version."""
        async with open_shared_ctx(self.db_path) as db:
            row = await (
                await db.execute("SELECT refresh_cipher FROM builtin_tool_user_grant WHERE id=? AND user_id=?", (grant.id, grant.user_id))
            ).fetchone()
        if row is None:
            raise BuiltinToolGrantError("builtin_tool_grant_missing")
        return self._unseal(grant.user_id, grant.tool_id, "refresh", row[0]), str(row[0])

    async def mark_needs_reauth(self, tool_id: str, user_id: str, *, expected_cipher: str | None = None) -> None:
        """Mark the grant stale. With ``expected_cipher``, only if it still holds that token version:
        a refresh that failed with an old token must not break a grant the member just renewed."""
        sql = "UPDATE builtin_tool_user_grant SET status='needs_reauth',updated_at=? WHERE tool_id=? AND user_id=?"
        params: tuple[str, ...] = (_iso(_now()), tool_id, user_id)
        if expected_cipher is not None:
            sql += " AND refresh_cipher=?"
            params = (*params, expected_cipher)
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(sql, params)
            await db.commit()

    async def touch(self, tool_id: str, user_id: str) -> None:
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("UPDATE builtin_tool_user_grant SET last_used_at=? WHERE tool_id=? AND user_id=?", (_iso(_now()), tool_id, user_id))
            await db.commit()

    async def delete(self, tool_id: str, user_id: str) -> str | None:
        """Remove the member's grant; return its refresh token (for best-effort revoke)."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("DELETE FROM builtin_tool_user_grant WHERE tool_id=? AND user_id=? RETURNING refresh_cipher", (tool_id, user_id))
            ).fetchone()
            await db.commit()
        if row is None:
            return None
        try:
            return self._unseal(user_id, tool_id, "refresh", row["refresh_cipher"])
        except BuiltinToolGrantError:
            return None

    async def refresh_tokens_for_tool(self, tool_id: str) -> list[str]:
        """Every readable refresh token on a connection (for revoke before deleting it)."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT user_id,refresh_cipher FROM builtin_tool_user_grant WHERE tool_id=?", (tool_id,))).fetchall()
        tokens: list[str] = []
        for row in rows:
            try:
                tokens.append(self._unseal(row["user_id"], tool_id, "refresh", row["refresh_cipher"]))
            except BuiltinToolGrantError:
                continue
        return tokens

    async def delete_for_tool(self, tool_id: str) -> None:
        """Explicit cleanup before a connection row is removed.

        ``UserStore`` deletes connections on plain connections without
        ``foreign_keys=ON``, so the schema-level CASCADE cannot be relied on.
        """
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("DELETE FROM builtin_tool_oauth_state WHERE tool_id=?", (tool_id,))
            await db.execute("DELETE FROM builtin_tool_user_grant WHERE tool_id=?", (tool_id,))
            await db.commit()
