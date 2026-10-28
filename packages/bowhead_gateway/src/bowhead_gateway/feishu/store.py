"""SQLite persistence for the Feishu IM adapter."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
from bowhead_core._db import open_shared_ctx

SCHEMA = """
CREATE TABLE IF NOT EXISTS feishu_user_bindings (
    tenant_key       TEXT NOT NULL,
    open_id          TEXT NOT NULL,
    union_id         TEXT,
    email            TEXT,
    bowhead_user_id  TEXT NOT NULL,
    binding_method   TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (tenant_key, open_id)
);
CREATE INDEX IF NOT EXISTS idx_feishu_bindings_user
    ON feishu_user_bindings(bowhead_user_id);

CREATE TABLE IF NOT EXISTS feishu_message_dedup (
    dedup_key    TEXT PRIMARY KEY,
    event_id     TEXT,
    message_id   TEXT,
    received_at  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'received'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_feishu_message_dedup_event
    ON feishu_message_dedup(event_id)
    WHERE event_id IS NOT NULL AND event_id != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_feishu_message_dedup_message
    ON feishu_message_dedup(message_id)
    WHERE message_id IS NOT NULL AND message_id != '';
CREATE INDEX IF NOT EXISTS idx_feishu_message_dedup_received_at
    ON feishu_message_dedup(received_at);

CREATE TABLE IF NOT EXISTS feishu_turn_links (
    turn_id             TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL,
    tenant_key          TEXT NOT NULL,
    chat_id             TEXT NOT NULL,
    sender_open_id      TEXT NOT NULL,
    source_message_id   TEXT NOT NULL,
    card_message_id     TEXT,
    answer_message_id   TEXT,
    meta_json           TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feishu_turn_links_session
    ON feishu_turn_links(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feishu_turn_links_updated_at
    ON feishu_turn_links(updated_at);

CREATE TABLE IF NOT EXISTS feishu_workspace_bindings (
    tenant_key       TEXT NOT NULL,
    chat_id          TEXT NOT NULL,
    open_id          TEXT NOT NULL,
    bowhead_user_id  TEXT NOT NULL,
    workspace_id     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (tenant_key, chat_id, open_id)
);
CREATE INDEX IF NOT EXISTS idx_feishu_workspace_bindings_user
    ON feishu_workspace_bindings(bowhead_user_id, workspace_id);

CREATE TABLE IF NOT EXISTS feishu_setup_sessions (
    id                  TEXT PRIMARY KEY,
    team_id              TEXT NOT NULL,
    workspace_id        TEXT,
    created_by_user_id  TEXT NOT NULL,
    state               TEXT NOT NULL,
    nonce               TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    tenant_key          TEXT,
    app_type            TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feishu_setup_sessions_team
    ON feishu_setup_sessions(team_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feishu_setup_sessions_state_expiry
    ON feishu_setup_sessions(state, expires_at);

CREATE TABLE IF NOT EXISTS feishu_binding_sessions (
    id                  TEXT PRIMARY KEY,
    tenant_key          TEXT NOT NULL,
    open_id             TEXT NOT NULL,
    union_id            TEXT,
    email               TEXT,
    chat_id             TEXT NOT NULL,
    source_message_id   TEXT NOT NULL,
    state               TEXT NOT NULL,
    bound_user_id       TEXT,
    expires_at          TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    completed_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_feishu_binding_sessions_identity
    ON feishu_binding_sessions(tenant_key, open_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feishu_binding_sessions_state_expiry
    ON feishu_binding_sessions(state, expires_at);
CREATE INDEX IF NOT EXISTS idx_feishu_binding_sessions_updated_at
    ON feishu_binding_sessions(updated_at);

CREATE TABLE IF NOT EXISTS feishu_app_configs (
    id                                  TEXT PRIMARY KEY,
    team_id                              TEXT NOT NULL,
    tenant_key                          TEXT NOT NULL DEFAULT '',
    app_type                            TEXT NOT NULL DEFAULT 'internal',
    enabled                             INTEGER NOT NULL DEFAULT 0,
    event_ingress                       TEXT NOT NULL DEFAULT 'webhook',
    app_id                              TEXT NOT NULL DEFAULT '',
    app_secret_ref                      TEXT,
    verification_token_ref              TEXT,
    encrypt_key_ref                     TEXT,
    bot_open_id                         TEXT NOT NULL DEFAULT '',
    group_public_answers_enabled        INTEGER NOT NULL DEFAULT 0,
    created_by_user_id                  TEXT NOT NULL,
    updated_by_user_id                  TEXT NOT NULL,
    created_at                          TEXT NOT NULL,
    updated_at                          TEXT NOT NULL,
    UNIQUE(tenant_key, app_type)
);
CREATE INDEX IF NOT EXISTS idx_feishu_app_configs_team
    ON feishu_app_configs(team_id, updated_at DESC);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@dataclass
class FeishuBinding:
    tenant_key: str
    open_id: str
    union_id: str | None
    email: str | None
    bowhead_user_id: str
    binding_method: str


@dataclass
class FeishuTurnLink:
    turn_id: str
    session_id: str
    tenant_key: str
    chat_id: str
    sender_open_id: str
    source_message_id: str
    card_message_id: str | None
    answer_message_id: str | None
    meta: dict[str, Any]


@dataclass
class FeishuWorkspaceBinding:
    tenant_key: str
    chat_id: str
    open_id: str
    bowhead_user_id: str
    workspace_id: str


@dataclass
class FeishuSetupSession:
    id: str
    team_id: str
    workspace_id: str | None
    created_by_user_id: str
    state: str
    nonce: str
    expires_at: str
    tenant_key: str | None
    app_type: str
    created_at: str
    updated_at: str


@dataclass
class FeishuBindingSession:
    id: str
    tenant_key: str
    open_id: str
    union_id: str | None
    email: str | None
    chat_id: str
    source_message_id: str
    state: str
    bound_user_id: str | None
    expires_at: str
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass
class FeishuStoredAppConfig:
    id: str
    team_id: str
    tenant_key: str
    app_type: str
    enabled: bool
    event_ingress: str
    app_id: str
    app_secret_ref: str | None
    verification_token_ref: str | None
    encrypt_key_ref: str | None
    bot_open_id: str
    group_public_answers_enabled: bool
    created_by_user_id: str
    updated_by_user_id: str
    created_at: str
    updated_at: str


@dataclass
class FeishuSweepStats:
    expired_setup_sessions: int = 0
    pruned_setup_sessions: int = 0
    expired_binding_sessions: int = 0
    pruned_binding_sessions: int = 0
    pruned_message_dedup: int = 0
    pruned_turn_links: int = 0

    @property
    def total_mutations(self) -> int:
        return (
            self.expired_setup_sessions
            + self.pruned_setup_sessions
            + self.expired_binding_sessions
            + self.pruned_binding_sessions
            + self.pruned_message_dedup
            + self.pruned_turn_links
        )

    def to_log_fields(self) -> dict[str, int]:
        return {
            "expired_setup_sessions": self.expired_setup_sessions,
            "pruned_setup_sessions": self.pruned_setup_sessions,
            "expired_binding_sessions": self.expired_binding_sessions,
            "pruned_binding_sessions": self.pruned_binding_sessions,
            "pruned_message_dedup": self.pruned_message_dedup,
            "pruned_turn_links": self.pruned_turn_links,
            "total_mutations": self.total_mutations,
        }


class FeishuEventStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with open_shared_ctx(self.db_path) as db:
            for table in ("feishu_setup_sessions", "feishu_app_configs"):
                columns = {row[1] for row in await db.execute_fetchall(f"PRAGMA table_info({table})")}
                if "org_id" in columns and "team_id" not in columns:
                    await db.execute(f"ALTER TABLE {table} RENAME COLUMN org_id TO team_id")
            await db.executescript(SCHEMA)
            await db.commit()

    async def claim_message(self, *, event_id: str | None, message_id: str | None) -> bool:
        dedup_key = (message_id or event_id or "").strip()
        if not dedup_key:
            return True
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT dedup_key, status FROM feishu_message_dedup "
                "WHERE dedup_key=? OR message_id=? OR event_id=? "
                "ORDER BY received_at DESC LIMIT 1",
                (dedup_key, message_id, event_id),
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                if row["status"] == "failed":
                    async with db.execute(
                        "SELECT 1 FROM feishu_turn_links WHERE source_message_id=? LIMIT 1",
                        (message_id or row["dedup_key"],),
                    ) as cur:
                        linked_turn = await cur.fetchone()
                    if linked_turn is not None:
                        return False
                    await db.execute(
                        "UPDATE feishu_message_dedup SET status='received', received_at=? WHERE dedup_key=?",
                        (_now(), row["dedup_key"]),
                    )
                    await db.commit()
                    return True
                return False
            try:
                cur = await db.execute(
                    "INSERT OR IGNORE INTO feishu_message_dedup"
                    "(dedup_key, event_id, message_id, received_at, status) "
                    "VALUES (?, ?, ?, ?, 'received')",
                    (dedup_key, event_id, message_id, _now()),
                )
                await db.commit()
                return cur.rowcount > 0
            except aiosqlite.IntegrityError:
                return False

    async def mark_message_status(self, *, message_id: str | None, status: str) -> None:
        if not message_id:
            return
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "UPDATE feishu_message_dedup SET status=? WHERE message_id=? OR dedup_key=?",
                (status, message_id, message_id),
            )
            await db.commit()

    async def get_message_status(self, message_id: str) -> str | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT status FROM feishu_message_dedup WHERE message_id=? OR dedup_key=?",
                (message_id, message_id),
            ) as cur:
                row = await cur.fetchone()
        return str(row["status"]) if row else None

    async def get_binding(self, *, tenant_key: str, open_id: str) -> FeishuBinding | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM feishu_user_bindings WHERE tenant_key=? AND open_id=?",
                (tenant_key, open_id),
            ) as cur:
                row = await cur.fetchone()
        return _binding_from_row(row) if row else None

    async def upsert_binding(
        self,
        *,
        tenant_key: str,
        open_id: str,
        union_id: str | None,
        email: str | None,
        bowhead_user_id: str,
        binding_method: str,
    ) -> FeishuBinding:
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "INSERT INTO feishu_user_bindings"
                "(tenant_key, open_id, union_id, email, bowhead_user_id, "
                "binding_method, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_key, open_id) DO UPDATE SET "
                "  union_id=excluded.union_id, email=excluded.email, "
                "  bowhead_user_id=excluded.bowhead_user_id, "
                "  binding_method=excluded.binding_method, updated_at=excluded.updated_at",
                (
                    tenant_key,
                    open_id,
                    union_id,
                    email,
                    bowhead_user_id,
                    binding_method,
                    now,
                    now,
                ),
            )
            await db.commit()
        return FeishuBinding(
            tenant_key=tenant_key,
            open_id=open_id,
            union_id=union_id,
            email=email,
            bowhead_user_id=bowhead_user_id,
            binding_method=binding_method,
        )

    async def upsert_turn_link(
        self,
        *,
        turn_id: str,
        session_id: str,
        tenant_key: str,
        chat_id: str,
        sender_open_id: str,
        source_message_id: str,
        card_message_id: str | None = None,
        answer_message_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "INSERT INTO feishu_turn_links"
                "(turn_id, session_id, tenant_key, chat_id, sender_open_id, "
                "source_message_id, card_message_id, answer_message_id, "
                "meta_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(turn_id) DO UPDATE SET "
                "  card_message_id=COALESCE(excluded.card_message_id, card_message_id), "
                "  answer_message_id=COALESCE(excluded.answer_message_id, answer_message_id), "
                "  meta_json=excluded.meta_json, updated_at=excluded.updated_at",
                (
                    turn_id,
                    session_id,
                    tenant_key,
                    chat_id,
                    sender_open_id,
                    source_message_id,
                    card_message_id,
                    answer_message_id,
                    json.dumps(meta or {}, ensure_ascii=False, default=str),
                    now,
                    now,
                ),
            )
            await db.commit()

    async def set_card_message_id(self, *, turn_id: str, card_message_id: str) -> None:
        await self._set_turn_link_field(
            turn_id=turn_id,
            field="card_message_id",
            value=card_message_id,
        )

    async def set_answer_message_id(self, *, turn_id: str, answer_message_id: str) -> None:
        await self._set_turn_link_field(
            turn_id=turn_id,
            field="answer_message_id",
            value=answer_message_id,
        )

    async def get_turn_link(self, turn_id: str) -> FeishuTurnLink | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM feishu_turn_links WHERE turn_id=?",
                (turn_id,),
            ) as cur:
                row = await cur.fetchone()
        return _turn_link_from_row(row) if row else None

    async def get_workspace_binding(
        self,
        *,
        tenant_key: str,
        chat_id: str,
        open_id: str,
    ) -> FeishuWorkspaceBinding | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM feishu_workspace_bindings WHERE tenant_key=? AND chat_id=? AND open_id=?",
                (tenant_key, chat_id, open_id),
            ) as cur:
                row = await cur.fetchone()
        return _workspace_binding_from_row(row) if row else None

    async def upsert_workspace_binding(
        self,
        *,
        tenant_key: str,
        chat_id: str,
        open_id: str,
        bowhead_user_id: str,
        workspace_id: str,
    ) -> FeishuWorkspaceBinding:
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "INSERT INTO feishu_workspace_bindings"
                "(tenant_key, chat_id, open_id, bowhead_user_id, workspace_id, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_key, chat_id, open_id) DO UPDATE SET "
                "  bowhead_user_id=excluded.bowhead_user_id, "
                "  workspace_id=excluded.workspace_id, "
                "  updated_at=excluded.updated_at",
                (
                    tenant_key,
                    chat_id,
                    open_id,
                    bowhead_user_id,
                    workspace_id,
                    now,
                    now,
                ),
            )
            await db.commit()
        return FeishuWorkspaceBinding(
            tenant_key=tenant_key,
            chat_id=chat_id,
            open_id=open_id,
            bowhead_user_id=bowhead_user_id,
            workspace_id=workspace_id,
        )

    async def create_setup_session(
        self,
        *,
        team_id: str,
        workspace_id: str | None,
        created_by_user_id: str,
        tenant_key: str | None = None,
        app_type: str = "internal_guided",
        expires_in_seconds: int = 1800,
    ) -> FeishuSetupSession:
        now_dt = datetime.now(tz=UTC)
        now = now_dt.isoformat(timespec="seconds")
        expires_at = (now_dt + timedelta(seconds=expires_in_seconds)).isoformat(timespec="seconds")
        session_id = f"fss_{secrets.token_urlsafe(24)}"
        nonce = secrets.token_urlsafe(18)
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "INSERT INTO feishu_setup_sessions"
                "(id, team_id, workspace_id, created_by_user_id, state, nonce, "
                "expires_at, tenant_key, app_type, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'created', ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    team_id,
                    workspace_id,
                    created_by_user_id,
                    nonce,
                    expires_at,
                    tenant_key,
                    app_type,
                    now,
                    now,
                ),
            )
            await db.commit()
        return FeishuSetupSession(
            id=session_id,
            team_id=team_id,
            workspace_id=workspace_id,
            created_by_user_id=created_by_user_id,
            state="created",
            nonce=nonce,
            expires_at=expires_at,
            tenant_key=tenant_key,
            app_type=app_type,
            created_at=now,
            updated_at=now,
        )

    async def get_setup_session(self, session_id: str) -> FeishuSetupSession | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM feishu_setup_sessions WHERE id=?",
                (session_id,),
            ) as cur:
                row = await cur.fetchone()
        return _setup_session_from_row(row) if row else None

    async def create_binding_session(
        self,
        *,
        tenant_key: str,
        open_id: str,
        union_id: str | None,
        email: str | None,
        chat_id: str,
        source_message_id: str,
        ttl_minutes: int = 30,
    ) -> FeishuBindingSession:
        now = _now()
        expires_at = (datetime.now(tz=UTC) + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")
        session_id = "fbs_" + secrets.token_urlsafe(18)
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "INSERT INTO feishu_binding_sessions"
                "(id, tenant_key, open_id, union_id, email, chat_id, source_message_id, "
                "state, bound_user_id, expires_at, created_at, updated_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?, NULL)",
                (
                    session_id,
                    tenant_key,
                    open_id,
                    union_id,
                    email,
                    chat_id,
                    source_message_id,
                    expires_at,
                    now,
                    now,
                ),
            )
            await db.commit()
        return FeishuBindingSession(
            id=session_id,
            tenant_key=tenant_key,
            open_id=open_id,
            union_id=union_id,
            email=email,
            chat_id=chat_id,
            source_message_id=source_message_id,
            state="pending",
            bound_user_id=None,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )

    async def get_binding_session(self, session_id: str) -> FeishuBindingSession | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM feishu_binding_sessions WHERE id=?",
                (session_id,),
            ) as cur:
                row = await cur.fetchone()
        return _binding_session_from_row(row) if row else None

    async def complete_binding_session(
        self,
        *,
        session_id: str,
        bowhead_user_id: str,
    ) -> None:
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "UPDATE feishu_binding_sessions SET state='bound', bound_user_id=?, updated_at=?, completed_at=? WHERE id=?",
                (bowhead_user_id, now, now, session_id),
            )
            await db.commit()

    async def get_app_config(
        self,
        *,
        tenant_key: str = "",
        app_type: str = "internal",
    ) -> FeishuStoredAppConfig | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM feishu_app_configs WHERE tenant_key=? AND app_type=?",
                (tenant_key, app_type),
            ) as cur:
                row = await cur.fetchone()
        return _stored_app_config_from_row(row) if row else None

    async def upsert_app_config(
        self,
        *,
        team_id: str,
        tenant_key: str = "",
        app_type: str = "internal",
        enabled: bool,
        event_ingress: str,
        app_id: str,
        app_secret_ref: str | None,
        verification_token_ref: str | None,
        encrypt_key_ref: str | None,
        bot_open_id: str,
        group_public_answers_enabled: bool,
        actor_user_id: str,
    ) -> FeishuStoredAppConfig:
        now = _now()
        config_id = f"fsc_{tenant_key or 'default'}_{app_type}"
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "INSERT INTO feishu_app_configs"
                "(id, team_id, tenant_key, app_type, enabled, event_ingress, app_id, "
                "app_secret_ref, verification_token_ref, encrypt_key_ref, bot_open_id, "
                "group_public_answers_enabled, created_by_user_id, updated_by_user_id, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_key, app_type) DO UPDATE SET "
                "  team_id=excluded.team_id, enabled=excluded.enabled, "
                "  event_ingress=excluded.event_ingress, app_id=excluded.app_id, "
                "  app_secret_ref=excluded.app_secret_ref, "
                "  verification_token_ref=excluded.verification_token_ref, "
                "  encrypt_key_ref=excluded.encrypt_key_ref, "
                "  bot_open_id=excluded.bot_open_id, "
                "  group_public_answers_enabled=excluded.group_public_answers_enabled, "
                "  updated_by_user_id=excluded.updated_by_user_id, "
                "  updated_at=excluded.updated_at",
                (
                    config_id,
                    team_id,
                    tenant_key,
                    app_type,
                    int(enabled),
                    event_ingress,
                    app_id,
                    app_secret_ref,
                    verification_token_ref,
                    encrypt_key_ref,
                    bot_open_id,
                    int(group_public_answers_enabled),
                    actor_user_id,
                    actor_user_id,
                    now,
                    now,
                ),
            )
            await db.commit()
        stored = await self.get_app_config(tenant_key=tenant_key, app_type=app_type)
        if stored is None:
            raise RuntimeError("failed to persist Feishu app config")
        return stored

    async def sweep_hygiene(
        self,
        *,
        now: datetime | None = None,
        dedup_retention_days: float = 14.0,
        turn_link_retention_days: float = 30.0,
        session_retention_days: float = 30.0,
    ) -> FeishuSweepStats:
        now_dt = now or datetime.now(tz=UTC)
        now_iso = now_dt.isoformat(timespec="seconds")
        dedup_cutoff = (now_dt - timedelta(days=dedup_retention_days)).isoformat(timespec="seconds")
        turn_link_cutoff = (now_dt - timedelta(days=turn_link_retention_days)).isoformat(timespec="seconds")
        session_cutoff = (now_dt - timedelta(days=session_retention_days)).isoformat(timespec="seconds")
        stats = FeishuSweepStats()
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(
                "UPDATE feishu_setup_sessions SET state='expired', updated_at=? WHERE state='created' AND expires_at < ?",
                (now_iso, now_iso),
            )
            stats.expired_setup_sessions = cur.rowcount

            cur = await db.execute(
                "DELETE FROM feishu_setup_sessions WHERE expires_at < ?",
                (session_cutoff,),
            )
            stats.pruned_setup_sessions = cur.rowcount

            cur = await db.execute(
                "UPDATE feishu_binding_sessions SET state='expired', updated_at=? WHERE state='pending' AND expires_at < ?",
                (now_iso, now_iso),
            )
            stats.expired_binding_sessions = cur.rowcount

            cur = await db.execute(
                "DELETE FROM feishu_binding_sessions WHERE expires_at < ?",
                (session_cutoff,),
            )
            stats.pruned_binding_sessions = cur.rowcount

            cur = await db.execute(
                "DELETE FROM feishu_message_dedup WHERE received_at < ?",
                (dedup_cutoff,),
            )
            stats.pruned_message_dedup = cur.rowcount

            cur = await db.execute(
                "DELETE FROM feishu_turn_links WHERE updated_at < ?",
                (turn_link_cutoff,),
            )
            stats.pruned_turn_links = cur.rowcount

            await db.commit()
        return stats

    async def _set_turn_link_field(self, *, turn_id: str, field: str, value: str) -> None:
        if field not in {"card_message_id", "answer_message_id"}:
            raise ValueError(f"invalid feishu turn link field: {field}")
        sql = (
            "UPDATE feishu_turn_links SET card_message_id=?, updated_at=? WHERE turn_id=?"
            if field == "card_message_id"
            else "UPDATE feishu_turn_links SET answer_message_id=?, updated_at=? WHERE turn_id=?"
        )
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                sql,
                (value, _now(), turn_id),
            )
            await db.commit()


def _binding_from_row(row: aiosqlite.Row) -> FeishuBinding:
    return FeishuBinding(
        tenant_key=row["tenant_key"],
        open_id=row["open_id"],
        union_id=row["union_id"],
        email=row["email"],
        bowhead_user_id=row["bowhead_user_id"],
        binding_method=row["binding_method"],
    )


def _turn_link_from_row(row: aiosqlite.Row) -> FeishuTurnLink:
    meta: dict[str, Any] = {}
    try:
        meta = json.loads(row["meta_json"] or "{}")
    except Exception:
        meta = {}
    return FeishuTurnLink(
        turn_id=row["turn_id"],
        session_id=row["session_id"],
        tenant_key=row["tenant_key"],
        chat_id=row["chat_id"],
        sender_open_id=row["sender_open_id"],
        source_message_id=row["source_message_id"],
        card_message_id=row["card_message_id"],
        answer_message_id=row["answer_message_id"],
        meta=meta,
    )


def _workspace_binding_from_row(row: aiosqlite.Row) -> FeishuWorkspaceBinding:
    return FeishuWorkspaceBinding(
        tenant_key=row["tenant_key"],
        chat_id=row["chat_id"],
        open_id=row["open_id"],
        bowhead_user_id=row["bowhead_user_id"],
        workspace_id=row["workspace_id"],
    )


def _setup_session_from_row(row: aiosqlite.Row) -> FeishuSetupSession:
    return FeishuSetupSession(
        id=row["id"],
        team_id=row["team_id"],
        workspace_id=row["workspace_id"],
        created_by_user_id=row["created_by_user_id"],
        state=row["state"],
        nonce=row["nonce"],
        expires_at=row["expires_at"],
        tenant_key=row["tenant_key"],
        app_type=row["app_type"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _binding_session_from_row(row: aiosqlite.Row) -> FeishuBindingSession:
    return FeishuBindingSession(
        id=row["id"],
        tenant_key=row["tenant_key"],
        open_id=row["open_id"],
        union_id=row["union_id"],
        email=row["email"],
        chat_id=row["chat_id"],
        source_message_id=row["source_message_id"],
        state=row["state"],
        bound_user_id=row["bound_user_id"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


def _stored_app_config_from_row(row: aiosqlite.Row) -> FeishuStoredAppConfig:
    return FeishuStoredAppConfig(
        id=row["id"],
        team_id=row["team_id"],
        tenant_key=row["tenant_key"],
        app_type=row["app_type"],
        enabled=bool(row["enabled"]),
        event_ingress=row["event_ingress"],
        app_id=row["app_id"],
        app_secret_ref=row["app_secret_ref"],
        verification_token_ref=row["verification_token_ref"],
        encrypt_key_ref=row["encrypt_key_ref"],
        bot_open_id=row["bot_open_id"],
        group_public_answers_enabled=bool(row["group_public_answers_enabled"]),
        created_by_user_id=row["created_by_user_id"],
        updated_by_user_id=row["updated_by_user_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
