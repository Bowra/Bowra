"""Owner-only email send confirmation endpoints (send_email pre-delivery confirmation)."""

from __future__ import annotations

import asyncio
import time

import aiosqlite
import httpx2
import jwt as pyjwt
import pytest
from bowhead_core.user_store import UserStore, _now, hash_password
from fastapi import FastAPI

from bowhead_core import email_send_confirmation as queue_mod

SECRET = "test-secret-long-enough-for-hs256-32-bytes"


@pytest.fixture
async def store(tmp_path):
    s = UserStore(tmp_path / "esc.db")
    await s.initialize()
    async with aiosqlite.connect(s.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id, name, max_seats, default_user_tier, team_credit_balance, team_credit_quota, "
            "tier_credits_json, status, created_at) VALUES ('team_esc', 'O', 10, 'ultra', 0, 0, '{}', 'active', ?)",
            (_now(),),
        )
        for user_id in ("usr_owner", "usr_other"):
            await db.execute(
                "INSERT INTO users(id, team_id, email, email_verified, password_hash, tier, credit_balance, credit_quota, status, created_at) "
                "VALUES (?, 'team_esc', ?, 1, ?, 'ultra', 0, 0, 'active', ?)",
                (user_id, f"{user_id}@x.com", hash_password("pwd123"), _now()),
            )
        await db.commit()
    return s


@pytest.fixture
def app(store, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", SECRET)
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "prod")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setattr(queue_mod, "_default", queue_mod.EmailSendConfirmations(timeout_seconds=5))
    from bowhead_gateway.routes import email_send_confirmations as routes

    a = FastAPI()
    a.state.user_store = store

    class _S:
        dev_default_user = "dev_user"
        auth_mode = "prod"

    a.state.settings = _S()
    a.include_router(routes.router, prefix="/api/v1/email-send-confirmations")
    return a


def _cookie(user_id: str) -> dict[str, str]:
    now = int(time.time())
    return {"bowhead_jwt": pyjwt.encode({"sub": user_id, "iat": now, "exp": now + 3600, "pwd_v": 0}, SECRET, algorithm="HS256")}


def _client(app: FastAPI, user_id: str) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test", cookies=_cookie(user_id))


@pytest.mark.asyncio
async def test_owner_sees_full_email_and_approval_releases_the_waiting_tool(app):
    waiting = asyncio.create_task(
        queue_mod.default_email_send_confirmations().request(
            owner_user_id="usr_owner",
            turn_id="t_1",
            session_id="s_1",
            to="a@example.com",
            subject="月报",
            body="护肤美妆 9.54 亿元",
            attachments=[
                {
                    "artifact_id": "art_deck",
                    "filename": "销售报告.pptx",
                    "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    "size": 2048,
                }
            ],
        )
    )
    async with _client(app, "usr_owner") as owner, _client(app, "usr_other") as other:
        for _ in range(100):
            listed = (await owner.get("/api/v1/email-send-confirmations")).json()["confirmations"]
            if listed:
                break
            await asyncio.sleep(0.01)
        assert [(c["to"], c["subject"], c["body"]) for c in listed] == [("a@example.com", "月报", "护肤美妆 9.54 亿元")]
        assert listed[0]["attachments"] == [
            {
                "artifact_id": "art_deck",
                "filename": "销售报告.pptx",
                "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "size": 2048,
            }
        ]
        assert (await other.get("/api/v1/email-send-confirmations")).json()["confirmations"] == []

        foreign = await other.post(f"/api/v1/email-send-confirmations/{listed[0]['id']}/decision", json={"decision": "approve"})
        assert foreign.status_code == 409
        approved = await owner.post(f"/api/v1/email-send-confirmations/{listed[0]['id']}/decision", json={"decision": "approve"})
        assert approved.status_code == 200

    assert await waiting == "approved"


@pytest.mark.asyncio
async def test_owner_rejection_is_final(app):
    waiting = asyncio.create_task(
        queue_mod.default_email_send_confirmations().request(
            owner_user_id="usr_owner", turn_id="t_1", session_id="s_1", to="a@example.com", subject="s", body="b"
        )
    )
    async with _client(app, "usr_owner") as owner:
        while not (listed := (await owner.get("/api/v1/email-send-confirmations")).json()["confirmations"]):
            await asyncio.sleep(0.01)
        path = f"/api/v1/email-send-confirmations/{listed[0]['id']}/decision"
        assert (await owner.post(path, json={"decision": "reject"})).status_code == 200
        assert (await owner.post(path, json={"decision": "approve"})).status_code == 409

    assert await waiting == "rejected"


@pytest.mark.asyncio
async def test_anonymous_caller_is_rejected(app):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as anon:
        assert (await anon.get("/api/v1/email-send-confirmations")).status_code == 401
