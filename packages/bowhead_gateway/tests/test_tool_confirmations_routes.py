"""One owner-only view of every waiting tool call (FEATURE_INLINE_TOOL_CONFIRMATION)."""

from __future__ import annotations

import asyncio
import time

import aiosqlite
import httpx2
import jwt as pyjwt
import pytest
from bowhead_core.user_store import UserStore, _now, hash_password
from fastapi import FastAPI

from bowhead_core import email_send_confirmation as email_mod
from bowhead_core import team_tool_confirmation as team_mod

SECRET = "test-secret-long-enough-for-hs256-32-bytes"
_BASE = "/api/v1/tool-confirmations"


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "tc.db")
    await s.initialize()
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, max_seats, default_user_tier, team_credit_balance, team_credit_quota, "
            "tier_credits_json, status, created_at) VALUES ('team_tc', 'O', 10, 'ultra', 0, 0, '{}', 'active', ?)",
            (_now(),),
        )
        for user_id in ("usr_owner", "usr_other"):
            await db.execute(
                "INSERT INTO users(id, team_id, email, email_verified, password_hash, tier, credit_balance, credit_quota, status, created_at) "
                "VALUES (?, 'team_tc', ?, 1, ?, 'ultra', 0, 0, 'active', ?)",
                (user_id, f"{user_id}@x.com", hash_password("pwd123"), _now()),
            )
        await db.commit()
    return s


class _NoMcpLicense:
    async def is_feature_licensed(self, team_id: str, feature: str) -> bool:
        return False


@pytest.fixture
def app(store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setattr(team_mod, "_default", team_mod.TeamToolConfirmations(timeout_seconds=5))
    monkeypatch.setattr(email_mod, "_default", email_mod.EmailSendConfirmations(timeout_seconds=5))
    from bowhead_gateway.routes import tool_confirmations as routes

    a = FastAPI()
    a.state.user_store = store
    a.state.entitlements = _NoMcpLicense()

    class _S:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    a.state.settings = _S()
    a.include_router(routes.router, prefix=_BASE)
    return a


def _client(app: FastAPI, user_id: str) -> httpx2.AsyncClient:
    now = int(time.time())
    token = pyjwt.encode({"sub": user_id, "iat": now, "exp": now + 3600, "pwd_v": 0}, SECRET, algorithm="HS256")
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test", cookies={"bowhead_jwt": token})


def _team_call(**overrides):
    values = {
        "owner_user_id": "usr_owner",
        "turn_id": "t_1",
        "session_id": "s_1",
        "team_id": "team_tc",
        "source_name": "CRM",
        "tool_ref": "teammcp.ots_crm.create_note",
        "tool_name": "create_note",
        "description": "Create a note",
        "schema_digest": "digest-1",
        "arguments": {"text": "hello"},
        "category": "external_write",
    }
    values.update(overrides)
    return team_mod.default_team_tool_confirmations().request(**values)


def _email_call():
    return email_mod.default_email_send_confirmations().request(
        owner_user_id="usr_owner", turn_id="t_2", session_id="s_2", to="a@example.com", subject="Weekly update", body="private body"
    )


async def _listed(client, count: int) -> list[dict]:
    for _ in range(200):
        items = (await client.get(_BASE)).json()["items"]
        if len(items) >= count:
            return items
        await asyncio.sleep(0.01)
    raise AssertionError("pending confirmations did not appear")


@pytest.mark.asyncio
async def test_owner_lists_every_kind_as_labels_and_reads_one_preview(app):
    team = asyncio.create_task(_team_call())
    email = asyncio.create_task(_email_call())
    async with _client(app, "usr_owner") as owner, _client(app, "usr_other") as other:
        items = {item["kind"]: item for item in await _listed(owner, 2)}
        assert set(items) == {"team_mcp", "email_send"}
        assert (items["team_mcp"]["title"], items["team_mcp"]["source_label"]) == ("create_note", "CRM")
        assert (items["email_send"]["title"], items["email_send"]["source_label"]) == ("Weekly update", "a@example.com")
        assert all(0 < item["remaining_seconds"] <= 5 for item in items.values())
        # Summaries carry labels only; previews need the owner's detail read.
        assert "hello" not in repr(items) and "private body" not in repr(items)
        assert (await other.get(_BASE)).json() == {"items": []}

        team_id = items["team_mcp"]["id"]
        detail = (await owner.get(f"{_BASE}/{team_id}")).json()
        assert detail["item"]["arguments"] == {"text": "hello"}
        assert detail["category"] == "external_write"
        assert detail["choices"] == ["reject", "approve", "approve_session"]
        assert (await other.get(f"{_BASE}/{team_id}")).status_code == 404
        assert (await owner.get(f"{_BASE}/xyz_unknown")).json()["detail"] == {"code": "tool_confirmation_not_found"}

        email_id = items["email_send"]["id"]
        assert (await owner.get(f"{_BASE}/{email_id}")).json()["item"]["body"] == "private body"
        refused = await owner.post(f"{_BASE}/{email_id}/decision", json={"decision": "approve_session"})
        assert refused.status_code == 422
        foreign = await other.post(f"{_BASE}/{email_id}/decision", json={"decision": "approve"})
        assert foreign.status_code == 404
        assert (await owner.post(f"{_BASE}/{email_id}/decision", json={"decision": "reject"})).status_code == 200
        assert (await owner.post(f"{_BASE}/{team_id}/decision", json={"decision": "approve_session"})).status_code == 200
        # A decided item is no longer available.
        assert (await owner.post(f"{_BASE}/{team_id}/decision", json={"decision": "approve"})).status_code == 404
    assert await email == "rejected"
    assert await team == "approved"
    # The session approval covers the same tool afterwards.
    assert await _team_call() == "approved"


@pytest.mark.asyncio
async def test_unlicensed_personal_mcp_has_no_items(app):
    async with _client(app, "usr_owner") as owner:
        assert (await owner.get(_BASE)).json() == {"items": []}
        assert (await owner.get(f"{_BASE}/pmi_missing")).status_code == 404
        assert (await owner.post(f"{_BASE}/pmi_missing/decision", json={"decision": "approve"})).status_code == 404


@pytest.mark.asyncio
async def test_anonymous_caller_is_rejected(app):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as anon:
        assert (await anon.get(_BASE)).status_code == 401
