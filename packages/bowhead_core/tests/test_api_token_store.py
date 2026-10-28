"""SP-AUTH-7 (概念术语审计 P1-2) — api_token 存储层行为 pin.

api_token 目前只有签发/撤销面 (网关鉴权尚未消费 lookup_api_token)。本文件在
接线前先钉死存储层语义: 过期与撤销的令牌 lookup 必须返回 None — 否则将来接线
即继承「过期令牌永久有效」漏洞。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.user_store import UserStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> UserStore:
    s = UserStore(tmp_path / "tok.db")
    await s.initialize()
    return s


async def _user(store: UserStore) -> str:
    team = await store.create_team(name="T", max_seats=5)
    u = await store.create_user(team_id=team.id, email="u@t.example", password="Passw0rd!")
    return u.id


@pytest.mark.asyncio
async def test_lookup_valid_token(store: UserStore) -> None:
    uid = await _user(store)
    raw, tid = await store.issue_api_token(user_id=uid, name="t1", role_snapshot="member")
    row = await store.lookup_api_token(raw)
    assert row is not None and row["id"] == tid and row["role_snapshot"] == "member"


@pytest.mark.asyncio
async def test_lookup_expired_token_returns_none(store: UserStore) -> None:
    uid = await _user(store)
    raw, _ = await store.issue_api_token(
        user_id=uid,
        name="t2",
        role_snapshot="member",
        expires_at="2020-01-01T00:00:00+00:00",
    )
    assert await store.lookup_api_token(raw) is None


@pytest.mark.asyncio
async def test_lookup_revoked_token_returns_none(store: UserStore) -> None:
    uid = await _user(store)
    raw, tid = await store.issue_api_token(user_id=uid, name="t3", role_snapshot="member")
    assert await store.revoke_api_token(tid, uid) is True
    assert await store.lookup_api_token(raw) is None
