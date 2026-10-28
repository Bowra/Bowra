"""Persistence for the Teams P0 personal-chat adapter."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite


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
        existing = await self.get_connection_for_team(team_id)
        connection_id = existing.id if existing else f"tmc_{secrets.token_hex(12)}"
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
        if existing:
            fields.update({name: getattr(existing, name) for name in fields})
        fields.update(values)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO teams_connections(
                id,team_id,cloud,entra_tenant_id,microsoft_app_id,app_type,app_secret_ref,
                managed_identity_client_id,sso_auth_app_id,sso_audience,sso_connection_name,
                teams_manifest_id,status,secret_expires_at,created_by_user_id,updated_by_user_id,created_at,updated_at)
                VALUES(?,?, 'global',?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?)
                ON CONFLICT(team_id) DO UPDATE SET entra_tenant_id=excluded.entra_tenant_id,
                microsoft_app_id=excluded.microsoft_app_id,app_type=excluded.app_type,
                app_secret_ref=excluded.app_secret_ref,managed_identity_client_id=excluded.managed_identity_client_id,
                sso_auth_app_id=excluded.sso_auth_app_id,sso_audience=excluded.sso_audience,
                sso_connection_name=excluded.sso_connection_name,teams_manifest_id=excluded.teams_manifest_id,
                status=excluded.status,secret_expires_at=excluded.secret_expires_at,
                updated_by_user_id=excluded.updated_by_user_id,updated_at=excluded.updated_at""",
                (connection_id, team_id, *fields.values(), actor_id, actor_id, now, now),
            )
            await db.commit()
        result = await self.get_connection_for_team(team_id)
        assert result is not None
        return result

    async def get_identity(self, tenant_id: str, oid: str) -> tuple[str, str] | None:
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT bowhead_user_id,team_id FROM teams_identity_bindings WHERE entra_tenant_id=? AND entra_oid=?", (tenant_id, oid)
            ) as cur,
        ):
            row = await cur.fetchone()
        return (str(row[0]), str(row[1])) if row else None

    async def bind_identity(self, *, tenant_id: str, oid: str, user_id: str, team_id: str) -> None:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO teams_identity_bindings(entra_tenant_id,entra_oid,bowhead_user_id,team_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(entra_tenant_id,entra_oid) DO UPDATE SET bowhead_user_id=excluded.bowhead_user_id,team_id=excluded.team_id,updated_at=excluded.updated_at""",
                (tenant_id, oid, user_id, team_id, now, now),
            )
            await db.commit()

    async def get_workspace(self, tenant_id: str, conversation_id: str, oid: str) -> str | None:
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT workspace_id FROM teams_workspace_bindings WHERE entra_tenant_id=? AND conversation_id=? AND entra_oid=?",
                (tenant_id, conversation_id, oid),
            ) as cur,
        ):
            row = await cur.fetchone()
        return str(row[0]) if row else None

    async def set_workspace(self, tenant_id: str, conversation_id: str, oid: str, workspace_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO teams_workspace_bindings(entra_tenant_id,conversation_id,entra_oid,workspace_id,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(entra_tenant_id,conversation_id,entra_oid) DO UPDATE SET workspace_id=excluded.workspace_id,updated_at=excluded.updated_at""",
                (tenant_id, conversation_id, oid, workspace_id, _now()),
            )
            await db.commit()

    async def claim_activity(self, activity_id: str) -> bool:
        if not activity_id:
            return True
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute("INSERT INTO teams_activity_dedup(activity_id,received_at) VALUES(?,?)", (activity_id, _now()))
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def _one(self, query: str, params: tuple[str, ...]) -> TeamsConnection | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cur:
                row = await cur.fetchone()
        return _connection(row) if row else None


def _connection(row: aiosqlite.Row) -> TeamsConnection:
    return TeamsConnection(**{key: row[key] for key in TeamsConnection.__dataclass_fields__})
