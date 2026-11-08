"""Requester-only team MCP call confirmation endpoints (FEATURE_TEAM_TOOL_CONFIRMATION)."""

from __future__ import annotations

import asyncio
import time

import aiosqlite
import httpx2
import jwt as pyjwt
import pytest
from bowhead_core.user_store import UserStore, _now, hash_password
from fastapi import FastAPI

from bowhead_core import team_tool_confirmation as queue_mod

SECRET = "test-secret-long-enough-for-hs256-32-bytes"


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "ttc.db")
    await s.initialize()
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, max_seats, default_user_tier, team_credit_balance, team_credit_quota, "
            "tier_credits_json, status, created_at) VALUES ('team_ttc', 'O', 10, 'ultra', 0, 0, '{}', 'active', ?)",
            (_now(),),
        )
        for user_id in ("usr_owner", "usr_other"):
            await db.execute(
                "INSERT INTO users(id, team_id, email, email_verified, password_hash, tier, credit_balance, credit_quota, status, created_at) "
                "VALUES (?, 'team_ttc', ?, 1, ?, 'ultra', 0, 0, 'active', ?)",
                (user_id, f"{user_id}@x.com", hash_password("pwd123"), _now()),
            )
        await db.commit()
    return s


@pytest.fixture
def app(store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setattr(queue_mod, "_default", queue_mod.TeamToolConfirmations(timeout_seconds=5))
    from bowhead_gateway.routes import team_tool_confirmations as routes

    a = FastAPI()
    a.state.user_store = store

    class _S:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    a.state.settings = _S()
    a.include_router(routes.router, prefix="/api/v1/team-tool-confirmations")
    return a


def _cookie(user_id: str) -> dict[str, str]:
    now = int(time.time())
    return {"bowhead_jwt": pyjwt.encode({"sub": user_id, "iat": now, "exp": now + 3600, "pwd_v": 0}, SECRET, algorithm="HS256")}


def _client(app: FastAPI, user_id: str) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test", cookies=_cookie(user_id))


_BASE = "/api/v1/team-tool-confirmations"


def _call(**overrides):
    values = {
        "owner_user_id": "usr_owner",
        "turn_id": "t_1",
        "session_id": "s_1",
        "team_id": "team_ttc",
        "source_name": "CRM",
        "tool_ref": "teammcp.ots_crm.create_note",
        "tool_name": "create_note",
        "description": "Create a note",
        "schema_digest": "digest-1",
        "arguments": {"text": "hello"},
    }
    values.update(overrides)
    return queue_mod.default_team_tool_confirmations().request(**values)


async def _listed(client):
    for _ in range(100):
        listed = (await client.get(_BASE)).json()["confirmations"]
        if listed:
            return listed
        await asyncio.sleep(0.01)
    raise AssertionError("no pending confirmation")


@pytest.mark.asyncio
async def test_only_the_requester_sees_and_decides_the_exact_call(app):
    waiting = asyncio.create_task(_call())
    async with _client(app, "usr_owner") as owner, _client(app, "usr_other") as other:
        listed = await _listed(owner)
        assert [(c["source_name"], c["tool_name"], c["arguments"]) for c in listed] == [("CRM", "create_note", {"text": "hello"})]
        assert "schema_digest" not in listed[0]
        assert (await other.get(_BASE)).json()["confirmations"] == []
        foreign = await other.post(f"{_BASE}/{listed[0]['id']}/decision", json={"decision": "approve"})
        assert foreign.status_code == 409
        assert foreign.json()["detail"] == {"code": "team_tool_confirmation_unavailable"}
        assert (await owner.post(f"{_BASE}/{listed[0]['id']}/decision", json={"decision": "approve"})).status_code == 200
    assert await waiting == "approved"


@pytest.mark.asyncio
async def test_session_approval_covers_later_calls_and_rejection_is_final(app):
    waiting = asyncio.create_task(_call())
    async with _client(app, "usr_owner") as owner:
        listed = await _listed(owner)
        assert (await owner.post(f"{_BASE}/{listed[0]['id']}/decision", json={"decision": "approve_session"})).status_code == 200
        assert await waiting == "approved"
        assert await _call() == "approved"

        changed = asyncio.create_task(_call(schema_digest="digest-2"))
        listed = await _listed(owner)
        path = f"{_BASE}/{listed[0]['id']}/decision"
        assert (await owner.post(path, json={"decision": "reject"})).status_code == 200
        assert (await owner.post(path, json={"decision": "approve"})).status_code == 409
    assert await changed == "rejected"


@pytest.mark.asyncio
async def test_anonymous_caller_is_rejected(app):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as anon:
        assert (await anon.get(_BASE)).status_code == 401
