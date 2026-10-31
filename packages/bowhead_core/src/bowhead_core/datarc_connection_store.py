"""workspace-datarc-decouple P2a (R3.1) — team-scoped datarc connection registry.

Stores per-team datarc connections {base_url, auth_key (Fernet at rest), account_uid} so ③
multi-tenant can attach different datarc instances/keys to different teams. The auth_key is
encrypted via ``llm_config_crypto`` with the SAME F14 guard as CCA ``team_tool_source``
(refuse to persist plaintext when ``BOWHEAD_FERNET_KEY`` is unset — user_store.py:691). The
``DatarcConnection`` DTO returned to callers NEVER carries the secret — only a ``key_hint``
(last 4); decryption is use-time + team-bound (``get_connection_auth``). The global env
connections are created only through the team settings API; no deployment
environment connection is seeded at startup.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiosqlite

from bowhead_core.llm_config_crypto import decrypt_llm_config, encrypt_llm_config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS datarc_connection (
    id            TEXT PRIMARY KEY,
    team_id        TEXT NOT NULL,
    name          TEXT NOT NULL,
    base_url      TEXT NOT NULL,
    account_uid   TEXT NOT NULL DEFAULT '',
    auth_ref      TEXT,                          -- Fernet ciphertext of {"auth_key": ...}; never plaintext
    key_hint      TEXT NOT NULL DEFAULT '',      -- last 4 chars of the key, for admin display
    status        TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_datarc_connection_team ON datarc_connection(team_id, status);
"""

# DB-level backstop for the one-active-per-team invariant: a partial-unique index so no code path
# can ever leave an team with two active connections. Created AFTER the legacy-duplicate heal in
# initialize() — on violating data the CREATE itself would fail (boot crash), so the heal runs first.
_ACTIVE_UNIQUE_INDEX = "CREATE UNIQUE INDEX IF NOT EXISTS ux_datarc_connection_one_active ON datarc_connection(team_id) WHERE status='active'"


def _dc_id() -> str:
    return f"dc_{uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _key_hint(auth_key: str) -> str:
    if not auth_key:
        return ""
    return auth_key[-4:] if len(auth_key) >= 4 else "****"


def mask_secret(value: str) -> str:
    """`abcd***wxyz` (first 4 + *** + last 4); short secrets fully masked."""
    if not value:
        return ""
    return (value[:4] + "***" + value[-4:]) if len(value) > 8 else "***"


def redact_datarc_config(config: dict) -> dict:
    """Mask the secret in a datarc-connection-shaped dict for display / logging (R7.5).

    Parallel to ``llm_config_crypto.redact_for_display``, which only walks
    ``providers[].api_key`` and is therefore a no-op on datarc connection payloads. Masks the
    top-level ``auth_key`` (admin create/update body) and drops the at-rest ``auth_ref``
    ciphertext. Non-mutating (returns a shallow copy)."""
    out = dict(config)
    if "auth_key" in out:
        out["auth_key"] = mask_secret(str(out.get("auth_key") or ""))
    if "auth_ref" in out:
        out["auth_ref"] = "[encrypted]" if out.get("auth_ref") else None
    return out


def _encrypt_auth_or_raise(auth_key: str) -> str:
    """Encrypt the auth_key (Fernet). F14 guard: refuse plaintext when no FERNET_KEY (mirrors
    UserStore.update_team_tool_source_auth)."""
    token = encrypt_llm_config({"auth_key": auth_key})
    if token.startswith("PLAIN:"):
        raise RuntimeError("BOWHEAD_FERNET_KEY required to store a datarc connection auth_key (refusing to persist plaintext)")
    return token


@dataclass(frozen=True)
class DatarcConnection:
    """Connection metadata returned to callers — NEVER includes the secret auth_key."""

    id: str
    team_id: str
    name: str
    base_url: str
    account_uid: str
    key_hint: str
    status: str
    created_at: str
    updated_at: str


def _row_to_conn(row: aiosqlite.Row) -> DatarcConnection:
    return DatarcConnection(
        id=row["id"],
        team_id=row["team_id"],
        name=row["name"],
        base_url=row["base_url"],
        account_uid=row["account_uid"],
        key_hint=row["key_hint"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class DatarcConnectionStore:
    """Team-scoped datarc connection registry. Reuses the bowhead.db sqlite file."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def initialize(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            now = _now()
            await db.execute(
                "UPDATE datarc_connection AS dc SET status='archived', updated_at=? "
                "WHERE dc.status='active' AND EXISTS ("
                "  SELECT 1 FROM datarc_connection newer "
                "  WHERE newer.team_id=dc.team_id AND newer.status='active' "
                "  AND (newer.created_at > dc.created_at "
                "       OR (newer.created_at = dc.created_at AND newer.id > dc.id))"
                ")",
                (now,),
            )
            await db.execute(_ACTIVE_UNIQUE_INDEX)
            await db.commit()

    async def create_connection(
        self,
        *,
        team_id: str,
        name: str,
        base_url: str,
        auth_key: str,
        account_uid: str = "",
        status: str = "active",
    ) -> DatarcConnection:
        if status not in ("active", "archived"):
            raise ValueError(f"invalid status: {status!r}")
        auth_ref = _encrypt_auth_or_raise(auth_key) if auth_key else None
        dc_id = _dc_id()
        now = _now()
        async with aiosqlite.connect(self._db_path) as db:
            if status == "active":
                await db.execute(
                    "UPDATE datarc_connection SET status='archived', updated_at=? WHERE team_id=? AND status='active'",
                    (now, team_id),
                )
            await db.execute(
                "INSERT INTO datarc_connection(id, team_id, name, base_url, account_uid, "
                "auth_ref, key_hint, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dc_id,
                    team_id,
                    name,
                    base_url,
                    account_uid,
                    auth_ref,
                    _key_hint(auth_key),
                    status,
                    now,
                    now,
                ),
            )
            await db.commit()
        return DatarcConnection(
            id=dc_id,
            team_id=team_id,
            name=name,
            base_url=base_url,
            account_uid=account_uid,
            key_hint=_key_hint(auth_key),
            status=status,
            created_at=now,
            updated_at=now,
        )

    async def list_connections(self, team_id: str, *, include_archived: bool = False) -> list[DatarcConnection]:
        sql = "SELECT * FROM datarc_connection WHERE team_id=?"
        if not include_archived:
            sql += " AND status='active'"
        sql += " ORDER BY created_at"
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (team_id,)) as cur:
                rows = await cur.fetchall()
        return [_row_to_conn(r) for r in rows]

    async def get_connection(self, dc_id: str, team_id: str) -> DatarcConnection | None:
        """team-bound read (fail-closed cross-team: wrong team_id → None)."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM datarc_connection WHERE id=? AND team_id=?", (dc_id, team_id)) as cur:
                row = await cur.fetchone()
        return _row_to_conn(row) if row else None

    async def get_connection_auth(self, dc_id: str, team_id: str) -> str | None:
        """Decrypt the auth_key — use-time + team-bound only. None if no connection / no auth."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT auth_ref FROM datarc_connection WHERE id=? AND team_id=?", (dc_id, team_id)) as cur:
                row = await cur.fetchone()
        if not row or not row["auth_ref"]:
            return None
        return decrypt_llm_config(row["auth_ref"]).get("auth_key")

    async def resolve_active_binding(self, team_id: str) -> tuple[DatarcConnection, str, str] | None:
        """Read active metadata and credentials together; no secret enters the DTO.

        The randomized Fernet reference distinguishes credential rotations even
        when the registry timestamp has not advanced within the same second.
        Its digest identifies a revision; it is not an authorization token.
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM datarc_connection WHERE team_id=? AND status='active'", (team_id,)) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        reference = row["auth_ref"] or ""
        auth_key = decrypt_llm_config(reference).get("auth_key") if reference else ""
        revision = row["updated_at"] + ":" + hashlib.sha256(reference.encode()).hexdigest()
        return _row_to_conn(row), auth_key or "", revision

    async def update_connection_auth(self, dc_id: str, team_id: str, *, auth_key: str) -> bool:
        auth_ref = _encrypt_auth_or_raise(auth_key) if auth_key else None
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE datarc_connection SET auth_ref=?, key_hint=?, updated_at=? WHERE id=? AND team_id=?",
                (auth_ref, _key_hint(auth_key), _now(), dc_id, team_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def set_status(self, dc_id: str, team_id: str, status: str) -> bool:
        if status not in ("active", "archived"):
            raise ValueError(f"invalid status: {status!r}")
        now = _now()
        async with aiosqlite.connect(self._db_path) as db:
            if status == "active":
                await db.execute(
                    "UPDATE datarc_connection SET status='archived', updated_at=? WHERE team_id=? AND id<>? AND status='active'",
                    (now, team_id, dc_id),
                )
            cur = await db.execute(
                "UPDATE datarc_connection SET status=?, updated_at=? WHERE id=? AND team_id=?",
                (status, now, dc_id, team_id),
            )
            await db.commit()
            return cur.rowcount > 0
