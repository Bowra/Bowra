"""Persistence for the Teams P0 personal-chat adapter."""

from __future__ import annotations

import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from bowhead_core.sqlite_domain_runtime import connect as diagnostic_connect


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class TeamsConnection:
    id: str
    team_id: str
    entra_tenant_id: str
    microsoft_app_id: str
    app_type: str
    app_secret_ref: str | None
    managed_identity_client_id: str | None
    sso_auth_app_id: str
    sso_audience: str
    sso_connection_name: str
    teams_manifest_id: str
    status: str
    secret_expires_at: str | None
    updated_at: str


class ActivityInProgress(Exception):
    pass


class InvalidTeamsConfig(ValueError):
    def __init__(self, fields: list[str]):
        self.fields = fields
        super().__init__("Missing fields in the enabled Teams connection")


class TeamsStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def get_connection_for_team(self, team_id: str) -> TeamsConnection | None:
        return await self._one("SELECT * FROM teams_connections WHERE team_id=?", (team_id,))

    async def get_enabled_connection(self, app_id: str, tenant_id: str) -> TeamsConnection | None:
        return await self._one(
            "SELECT * FROM teams_connections WHERE microsoft_app_id=? AND entra_tenant_id=? AND status='enabled'",
            (app_id, tenant_id),
        )

    async def upsert_connection(self, *, team_id: str, actor_id: str, values: dict[str, object]) -> TeamsConnection:
        now = _now()
        fields = {
            "entra_tenant_id": "",
            "microsoft_app_id": "",
            "app_type": "SingleTenant",
            "app_secret_ref": None,
            "managed_identity_client_id": None,
            "sso_auth_app_id": "",
            "sso_audience": "",
            "sso_connection_name": "",
            "teams_manifest_id": "",
            "status": "draft",
            "secret_expires_at": None,
        }
        async with diagnostic_connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT * FROM teams_connections WHERE team_id=?", (team_id,)) as cursor:
                existing = await cursor.fetchone()
            connection_id = existing["id"] if existing else f"tmc_{secrets.token_hex(12)}"
            if existing:
                fields.update({name: existing[name] for name in fields})
            defaults = {"status": "draft", "app_type": "SingleTenant"}
            nullable = {"app_secret_ref", "managed_identity_client_id", "secret_expires_at"}
            for name, value in values.items():
                if name not in nullable and value is None:
                    value = defaults.get(name, "")
                fields[name] = value.strip() if isinstance(value, str) else value
            required = ("entra_tenant_id", "microsoft_app_id", "app_secret_ref", "sso_audience", "sso_connection_name", "teams_manifest_id")
            if fields["status"] == "enabled" and (missing := [name for name in required if not fields[name]]):
                raise InvalidTeamsConfig(["app_secret" if name == "app_secret_ref" else name for name in missing])
            await db.execute(
                """INSERT INTO teams_connections(
                id,team_id,cloud,entra_tenant_id,microsoft_app_id,app_type,app_secret_ref,
                managed_identity_client_id,sso_auth_app_id,sso_audience,sso_connection_name,
                teams_manifest_id,status,secret_expires_at,created_by_user_id,updated_by_user_id,created_at,updated_at)
                VALUES(:id,:team_id,'global',:entra_tenant_id,:microsoft_app_id,:app_type,:app_secret_ref,
                :managed_identity_client_id,:sso_auth_app_id,:sso_audience,:sso_connection_name,:teams_manifest_id,
                :status,:secret_expires_at,:actor,:actor,:now,:now)
                ON CONFLICT(team_id) DO UPDATE SET entra_tenant_id=excluded.entra_tenant_id,
                microsoft_app_id=excluded.microsoft_app_id,app_type=excluded.app_type,
                app_secret_ref=excluded.app_secret_ref,managed_identity_client_id=excluded.managed_identity_client_id,
                sso_auth_app_id=excluded.sso_auth_app_id,sso_audience=excluded.sso_audience,
                sso_connection_name=excluded.sso_connection_name,teams_manifest_id=excluded.teams_manifest_id,
                status=excluded.status,secret_expires_at=excluded.secret_expires_at,
                updated_by_user_id=excluded.updated_by_user_id,updated_at=excluded.updated_at""",
                {**fields, "id": connection_id, "team_id": team_id, "actor": actor_id, "now": now},
            )
            async with db.execute("SELECT * FROM teams_connections WHERE team_id=?", (team_id,)) as cursor:
                saved = await cursor.fetchone()
            await db.commit()
            return _connection(saved)

    async def get_identity(self, tenant_id: str, oid: str, team_id: str) -> tuple[str, str] | None:
        async with (
            diagnostic_connect(self.db_path) as db,
            db.execute(
                "SELECT bowhead_user_id,team_id FROM teams_identity_bindings WHERE entra_tenant_id=? AND entra_oid=? AND team_id=? AND confirmed_at IS NOT NULL",
                (tenant_id, oid, team_id),
            ) as cur,
        ):
            row = await cur.fetchone()
        return (str(row[0]), str(row[1])) if row else None

    async def bind_identity(self, *, tenant_id: str, oid: str, user_id: str, team_id: str) -> None:
        now = _now()
        async with diagnostic_connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO teams_identity_bindings(entra_tenant_id,entra_oid,bowhead_user_id,team_id,created_at,updated_at,confirmed_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(entra_tenant_id,entra_oid,team_id) DO UPDATE SET
                confirmed_at=excluded.confirmed_at,updated_at=excluded.updated_at WHERE bowhead_user_id=excluded.bowhead_user_id""",
                (tenant_id, oid, user_id, team_id, now, now, now),
            )
            async with db.execute(
                "SELECT bowhead_user_id FROM teams_identity_bindings WHERE entra_tenant_id=? AND entra_oid=? AND team_id=?", (tenant_id, oid, team_id)
            ) as cursor:
                row = await cursor.fetchone()
                if row is None or row[0] != user_id:
                    raise aiosqlite.IntegrityError("Teams identity already bound")
            await db.commit()

    async def get_workspace(self, tenant_id: str, conversation_id: str, oid: str, team_id: str) -> str | None:
        async with (
            diagnostic_connect(self.db_path) as db,
            db.execute(
                "SELECT workspace_id FROM teams_workspace_bindings WHERE entra_tenant_id=? AND conversation_id=? AND entra_oid=? AND team_id=?",
                (tenant_id, conversation_id, oid, team_id),
            ) as cur,
        ):
            row = await cur.fetchone()
        return str(row[0]) if row else None

    async def set_workspace(self, tenant_id: str, conversation_id: str, oid: str, workspace_id: str, team_id: str) -> None:
        async with diagnostic_connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO teams_workspace_bindings(entra_tenant_id,conversation_id,entra_oid,workspace_id,updated_at,team_id)
            VALUES(?,?,?,?,?,?) ON CONFLICT(entra_tenant_id,conversation_id,entra_oid,team_id) DO UPDATE SET workspace_id=excluded.workspace_id,updated_at=excluded.updated_at""",
                (tenant_id, conversation_id, oid, workspace_id, _now(), team_id),
            )
            await db.commit()

    @asynccontextmanager
    async def process_activity(self, key: str, legacy_id: str):
        owner = secrets.token_hex(16)
        now = int(time.time())
        async with diagnostic_connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT 1 FROM teams_activity_dedup WHERE activity_id=?", (legacy_id,)) as cur:
                historical = await cur.fetchone()
            claimed = False
            if not historical:
                cur = await db.execute(
                    """INSERT INTO teams_activity_dedup(activity_id,received_at,claim_owner,claim_until) VALUES(?,?,?,?)
                    ON CONFLICT(activity_id) DO UPDATE SET claim_owner=excluded.claim_owner,claim_until=excluded.claim_until
                    WHERE teams_activity_dedup.claim_until<=?""",
                    (key, _now(), owner, now + 120, now),
                )
                claimed = cur.rowcount == 1
                if not claimed:
                    async with db.execute("SELECT claim_owner FROM teams_activity_dedup WHERE activity_id=?", (key,)) as cur:
                        if (await cur.fetchone())[0]:
                            raise ActivityInProgress
            await db.commit()
        try:
            yield claimed
        except BaseException:
            if claimed:
                async with diagnostic_connect(self.db_path) as db:
                    await db.execute("UPDATE teams_activity_dedup SET claim_until=0 WHERE activity_id=? AND claim_owner=?", (key, owner))
                    await db.commit()
            raise
        else:
            if claimed:
                async with diagnostic_connect(self.db_path) as db:
                    await db.execute(
                        "UPDATE teams_activity_dedup SET claim_owner=NULL,claim_until=NULL WHERE activity_id=? AND claim_owner=?", (key, owner)
                    )
                    await db.commit()

    async def track_delivery(self, key: str, reference: str, user_id: str) -> str:
        async with diagnostic_connect(self.db_path) as db:
            await db.execute(
                """UPDATE teams_activity_dedup SET delivery=?,delivery_user_id=?,delivery_due=unixepoch()
                WHERE activity_id=? AND delivery IS NULL AND delivery_seq=-1""",
                (reference, user_id, key),
            )
            async with db.execute("SELECT delivery FROM teams_activity_dedup WHERE activity_id=?", (key,)) as cursor:
                row = await cursor.fetchone()
            if row is None or not row[0]:
                raise ValueError("Teams delivery is no longer available")
            await db.commit()
            return row[0]

    async def take_delivery(self, owner: str):
        async with diagnostic_connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT activity_id FROM teams_activity_dedup WHERE delivery_due<=unixepoch() ORDER BY delivery_due LIMIT 1"
            ) as cursor:
                candidate = await cursor.fetchone()
            if candidate is None:
                return None
            async with db.execute(
                """UPDATE teams_activity_dedup SET delivery_owner=?,delivery_due=unixepoch()+120
                WHERE activity_id=? AND delivery_due<=unixepoch() RETURNING activity_id,delivery,delivery_seq,received_at""",
                (owner, candidate[0]),
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
            return dict(row) if row else None

    async def advance_delivery(self, key: str, owner: str, seq: int, delay: int | None):
        async with diagnostic_connect(self.db_path) as db:
            await db.execute(
                """UPDATE teams_activity_dedup SET delivery_seq=?,delivery_due=unixepoch()+?,delivery_owner=NULL,
                delivery=CASE WHEN ? IS NULL THEN NULL ELSE delivery END,delivery_user_id=CASE WHEN ? IS NULL THEN NULL ELSE delivery_user_id END
                WHERE activity_id=? AND delivery_owner=?""",
                (seq, delay, delay, delay, key, owner),
            )
            await db.commit()

    async def _one(self, query: str, params: tuple[str, ...]) -> TeamsConnection | None:
        async with diagnostic_connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cur:
                row = await cur.fetchone()
        return _connection(row) if row else None


def _connection(row: aiosqlite.Row) -> TeamsConnection:
    return TeamsConnection(**{key: row[key] for key in TeamsConnection.__dataclass_fields__})
