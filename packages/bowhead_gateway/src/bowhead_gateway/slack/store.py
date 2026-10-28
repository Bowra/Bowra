"""Persistent state for one customer-managed Slack app per Bowhead team."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class SlackConnection:
    id: str
    team_id: str
    slack_team_id: str
    slack_app_id: str
    client_id: str
    client_secret_ref: str | None
    signing_secret_ref: str | None
    bot_token_ref: str | None
    bot_user_id: str
    channel_answers_enabled: int
    status: str
    updated_by_user_id: str
    updated_at: str


@dataclass(frozen=True)
class SlackOAuthState:
    connection_id: str
    redirect_uri: str | None = None


@dataclass(frozen=True)
class PersonalSlackOAuthState:
    connection_id: str
    owner_user_id: str
    team_id: str
    redirect_uri: str | None = None


class SlackStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def by_team(self, team_id: str) -> SlackConnection | None:
        return await self._one("SELECT * FROM slack_connections WHERE team_id=?", (team_id,))

    async def enabled(self, connection_id: str) -> SlackConnection | None:
        return await self._one("SELECT * FROM slack_connections WHERE id=? AND status='enabled'", (connection_id,))

    async def by_id(self, connection_id: str) -> SlackConnection | None:
        return await self._one("SELECT * FROM slack_connections WHERE id=?", (connection_id,))

    async def upsert(self, *, team_id: str, actor_id: str, values: dict[str, object]) -> SlackConnection:
        existing = await self.by_team(team_id)
        now = _now()
        fields: dict[str, object] = {
            "slack_team_id": "",
            "slack_app_id": "",
            "client_id": "",
            "client_secret_ref": None,
            "signing_secret_ref": None,
            "bot_token_ref": None,
            "bot_user_id": "",
            "channel_answers_enabled": 0,
            "status": "draft",
        }
        if existing:
            fields.update({key: getattr(existing, key) for key in fields})
        fields.update(values)
        connection_id = existing.id if existing else f"slc_{secrets.token_hex(12)}"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO slack_connections(id,team_id,slack_team_id,slack_app_id,client_id,client_secret_ref,signing_secret_ref,bot_token_ref,bot_user_id,channel_answers_enabled,status,created_by_user_id,updated_by_user_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(team_id) DO UPDATE SET slack_team_id=excluded.slack_team_id,slack_app_id=excluded.slack_app_id,client_id=excluded.client_id,client_secret_ref=excluded.client_secret_ref,signing_secret_ref=excluded.signing_secret_ref,bot_token_ref=excluded.bot_token_ref,bot_user_id=excluded.bot_user_id,channel_answers_enabled=excluded.channel_answers_enabled,status=excluded.status,updated_by_user_id=excluded.updated_by_user_id,updated_at=excluded.updated_at""",
                (connection_id, team_id, *fields.values(), actor_id, actor_id, now, now),
            )
            await db.commit()
        result = await self.by_team(team_id)
        assert result is not None
        return result

    async def create_oauth_state(self, connection_id: str, *, redirect_uri: str | None = None) -> str:
        state = secrets.token_urlsafe(32)
        expiry = (datetime.now(tz=UTC) + timedelta(minutes=10)).isoformat(timespec="seconds")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO slack_oauth_states(state,connection_id,expires_at,created_at,redirect_uri) VALUES(?,?,?,?,?)",
                (state, connection_id, expiry, _now(), redirect_uri),
            )
            await db.commit()
        return state

    async def consume_oauth_state(self, state: str) -> SlackOAuthState | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("DELETE FROM slack_oauth_states WHERE state=? RETURNING connection_id,expires_at,redirect_uri", (state,)) as cur:
                row = await cur.fetchone()
            await db.commit()
        return SlackOAuthState(connection_id=str(row[0]), redirect_uri=row[2]) if row and row[1] > _now() else None

    async def create_personal_oauth_state(
        self,
        connection_id: str,
        owner_user_id: str,
        team_id: str,
        *,
        redirect_uri: str | None = None,
    ) -> str:
        state = secrets.token_urlsafe(32)
        expiry = (datetime.now(tz=UTC) + timedelta(minutes=10)).isoformat(timespec="seconds")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO personal_slack_oauth_states(state,connection_id,owner_user_id,team_id,expires_at,created_at,redirect_uri) VALUES(?,?,?,?,?,?,?)",
                (state, connection_id, owner_user_id, team_id, expiry, _now(), redirect_uri),
            )
            await db.commit()
        return state

    async def consume_personal_oauth_state(self, state: str) -> PersonalSlackOAuthState | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "DELETE FROM personal_slack_oauth_states WHERE state=? RETURNING connection_id,owner_user_id,team_id,expires_at,redirect_uri",
                (state,),
            ) as cur:
                row = await cur.fetchone()
            await db.commit()
        if row is None or row[3] <= _now():
            return None
        return PersonalSlackOAuthState(connection_id=str(row[0]), owner_user_id=str(row[1]), team_id=str(row[2]), redirect_uri=row[4])

    async def get_identity(self, connection_id: str, slack_user_id: str) -> str | None:
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT bowhead_user_id FROM slack_identity_bindings WHERE connection_id=? AND slack_user_id=?",
                (connection_id, slack_user_id),
            ) as cur,
        ):
            row = await cur.fetchone()
        return str(row[0]) if row else None

    async def bind_identity(self, connection_id: str, slack_user_id: str, user_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO slack_identity_bindings(connection_id,slack_user_id,bowhead_user_id,created_at,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(connection_id,slack_user_id) DO UPDATE SET bowhead_user_id=excluded.bowhead_user_id,updated_at=excluded.updated_at""",
                (connection_id, slack_user_id, user_id, _now(), _now()),
            )
            await db.commit()

    async def workspace(self, connection_id: str, conversation_id: str, slack_user_id: str) -> str | None:
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                "SELECT workspace_id FROM slack_workspace_bindings WHERE connection_id=? AND conversation_id=? AND slack_user_id=?",
                (connection_id, conversation_id, slack_user_id),
            ) as cur,
        ):
            row = await cur.fetchone()
        return str(row[0]) if row else None

    async def set_workspace(self, connection_id: str, conversation_id: str, slack_user_id: str, workspace_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO slack_workspace_bindings(connection_id,conversation_id,slack_user_id,workspace_id,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(connection_id,conversation_id,slack_user_id) DO UPDATE SET workspace_id=excluded.workspace_id,updated_at=excluded.updated_at""",
                (connection_id, conversation_id, slack_user_id, workspace_id, _now()),
            )
            await db.commit()

    async def claim_event(self, connection_id: str, event_id: str) -> bool:
        if not event_id:
            return True
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute("INSERT INTO slack_event_dedup(connection_id,event_id,received_at) VALUES(?,?,?)", (connection_id, event_id, _now()))
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def _one(self, sql: str, params: tuple[str, ...]) -> SlackConnection | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
        return SlackConnection(**{name: row[name] for name in SlackConnection.__dataclass_fields__}) if row else None
