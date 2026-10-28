"""Local-team OpenAPI credentials and content-free call audit log."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime, timedelta

import aiosqlite

SCOPES = frozenset(
    {
        "users:read",
        "users:write",
        "spaces:read",
        "spaces:write",
        "space_members:read",
        "space_members:write",
        "agent_versions:read",
        "agent_versions:write",
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(token: str) -> str:
    # Instance-stable key; never persist the bearer token itself.
    key = os.environ.get("BOWHEAD_FERNET_KEY", "").encode()
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


def _new_token() -> str:
    return "bhk_" + secrets.token_urlsafe(32)


class OpenApiStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def create_key(self, *, team_id: str, name: str, scopes: list[str], expires_at: str | None) -> tuple[dict, str]:
        if not set(scopes).issubset(SCOPES) or not scopes:
            raise ValueError("invalid scopes")
        token = _new_token()
        key_id = "oak_" + secrets.token_urlsafe(12)
        row = (key_id, team_id, name.strip(), _hash(token), token[:12], json.dumps(sorted(set(scopes))), expires_at, _now())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO team_openapi_keys(id,team_id,name,token_hash,token_prefix,scopes_json,expires_at,status,created_at) VALUES(?,?,?,?,?,?,?,'active',?)",
                row,
            )
            await db.commit()
        return await self.get_key(team_id, key_id), token

    async def get_key(self, team_id: str, key_id: str) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,name,token_prefix,scopes_json,expires_at,status,created_at,last_used_at FROM team_openapi_keys WHERE team_id=? AND id=?",
                (team_id, key_id),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            raise KeyError(key_id)
        value = dict(row)
        value["scopes"] = json.loads(value.pop("scopes_json"))
        return value

    async def list_keys(self, team_id: str) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,name,token_prefix,scopes_json,expires_at,status,created_at,last_used_at FROM team_openapi_keys WHERE team_id=? AND status<>'deleted' ORDER BY created_at DESC",
                (team_id,),
            ) as cur:
                rows = await cur.fetchall()
        values: list[dict] = []
        for row in rows:
            value = dict(row)
            value["scopes"] = json.loads(value.pop("scopes_json"))
            values.append(value)
        return values

    async def set_status(self, team_id: str, key_id: str, status: str) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            result = await db.execute("UPDATE team_openapi_keys SET status=? WHERE team_id=? AND id=?", (status, team_id, key_id))
            await db.commit()
        if result.rowcount != 1:
            raise KeyError(key_id)
        return await self.get_key(team_id, key_id)

    async def update_key(self, *, team_id: str, key_id: str, scopes: list[str], expires_at: str | None) -> dict:
        if not set(scopes).issubset(SCOPES) or not scopes:
            raise ValueError("invalid scopes")
        async with aiosqlite.connect(self.db_path) as db:
            result = await db.execute(
                "UPDATE team_openapi_keys SET scopes_json=?,expires_at=? WHERE team_id=? AND id=? AND status<>'deleted'",
                (json.dumps(sorted(set(scopes))), expires_at, team_id, key_id),
            )
            await db.commit()
        if result.rowcount != 1:
            raise KeyError(key_id)
        return await self.get_key(team_id, key_id)

    async def rotate(self, team_id: str, key_id: str) -> tuple[dict, str]:
        token = _new_token()
        async with aiosqlite.connect(self.db_path) as db:
            result = await db.execute(
                "UPDATE team_openapi_keys SET token_hash=?,token_prefix=?,status='active',last_used_at=NULL WHERE team_id=? AND id=?",
                (_hash(token), token[:12], team_id, key_id),
            )
            await db.commit()
        if result.rowcount != 1:
            raise KeyError(key_id)
        return await self.get_key(team_id, key_id), token

    async def authenticate(self, token: str) -> dict | None:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,team_id,name,token_prefix,scopes_json,expires_at FROM team_openapi_keys WHERE token_hash=? AND status='active' AND (expires_at IS NULL OR expires_at>?)",
                (_hash(token), now),
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                await db.execute("UPDATE team_openapi_keys SET last_used_at=? WHERE id=?", (now, row["id"]))
                await db.commit()
        return None if row is None else {**dict(row), "scopes": json.loads(row["scopes_json"])}

    async def log(
        self,
        *,
        credential: dict | None,
        method: str,
        path: str,
        status_code: int,
        request_summary: str | None,
        response_summary: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO team_openapi_call_logs(id,team_id,key_id,key_name,key_prefix,http_method,path,status_code,request_summary,response_summary,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "oal_" + secrets.token_urlsafe(12),
                    credential["team_id"] if credential else None,
                    credential["id"] if credential else None,
                    credential["name"] if credential else None,
                    credential["token_prefix"] if credential else None,
                    method,
                    path,
                    status_code,
                    request_summary,
                    response_summary,
                    _now(),
                ),
            )
            await db.commit()

    async def list_logs(self, team_id: str, limit: int = 100) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT key_id,key_name,key_prefix,http_method,path,status_code,request_summary,response_summary,created_at FROM team_openapi_call_logs WHERE team_id=? ORDER BY created_at DESC LIMIT ?",
                (team_id, limit),
            ) as cur:
                return [dict(row) for row in await cur.fetchall()]

    async def prune_logs(self, retention_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            result = await db.execute("DELETE FROM team_openapi_call_logs WHERE created_at<?", (cutoff,))
            await db.commit()
            return result.rowcount
