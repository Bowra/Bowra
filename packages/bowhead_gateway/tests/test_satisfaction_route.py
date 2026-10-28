"""M158-13 · POST /turns/{tid}/satisfaction route tests."""

from __future__ import annotations

import httpx2
import pytest
from bowhead_core.user_memory import UserMemoryStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import turns
from fastapi import FastAPI


@pytest.fixture
async def app(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """最小 app: 仅注入 user_memory_store + settings, 接 turns router."""
    from types import SimpleNamespace

    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    a = FastAPI()
    a.include_router(turns.router, prefix="/api/v1/turns")
    db = tmp_path / "bowhead.db"
    users = UserStore(db)
    await users.initialize()
    team = await users.create_team(name="Acme", max_seats=10)
    default = await users.create_user(team_id=team.id, email="default@example.com", password="Passw0rd!")
    alice = await users.create_user(team_id=team.id, email="alice@example.com", password="Passw0rd!")
    user_memory = UserMemoryStore(db)
    await user_memory.initialize()
    a.state.user_memory_store = user_memory
    a.state.user_store = users
    a.state.settings = SimpleNamespace(auth_mode="dev", dev_default_user=default.id)
    a.state.default_user_id = default.id
    a.state.alice_user_id = alice.id
    return a


@pytest.mark.asyncio
async def test_satisfaction_thumbs_up(app):
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/turns/t_test/satisfaction",
            json={"score": 1},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["score"] == 1
        assert body["turn_id"] == "t_test"

    # Verify user_memory persistence.
    items = await app.state.user_memory_store.list_for_user(app.state.default_user_id)
    sat = [e for e in items if e.kind == "satisfaction"]
    assert len(sat) == 1
    assert sat[0].key == "t_test"
    assert sat[0].parsed_value()["score"] == 1


@pytest.mark.asyncio
async def test_satisfaction_thumbs_down_with_comment(app):
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/turns/t_test/satisfaction",
            json={"score": -1, "comment": "数字不准"},
        )
        assert r.status_code == 200
    items = await app.state.user_memory_store.list_for_user(app.state.default_user_id)
    sat = [e for e in items if e.kind == "satisfaction"]
    parsed = sat[0].parsed_value()
    assert parsed["score"] == -1
    assert parsed["comment"] == "数字不准"


@pytest.mark.asyncio
async def test_satisfaction_idempotent_overwrite(app):
    """同 turn_id 二次提交覆盖之前 score."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        await c.post(
            "/api/v1/turns/t_test/satisfaction",
            json={"score": 1},
        )
        await c.post(
            "/api/v1/turns/t_test/satisfaction",
            json={"score": -1},
        )
    items = await app.state.user_memory_store.list_for_user(app.state.default_user_id)
    sat = [e for e in items if e.kind == "satisfaction"]
    assert len(sat) == 1  # No duplicate rows.
    assert sat[0].parsed_value()["score"] == -1


@pytest.mark.asyncio
async def test_satisfaction_score_validation(app):
    """score 必须 ∈ [-1, 1] 整数."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/turns/t_test/satisfaction",
            json={"score": 5},
        )
        assert r.status_code == 422  # pydantic ge/le validation
        r = await c.post(
            "/api/v1/turns/t_test/satisfaction",
            json={"score": -2},
        )
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_satisfaction_user_id_from_header(app):
    """X-Bowhead-User-Id header 优先 dev_default_user."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/turns/t_test/satisfaction",
            json={"score": 1},
            headers={"X-Bowhead-User-Id": app.state.alice_user_id},
        )
        assert r.status_code == 200
    items_alice = await app.state.user_memory_store.list_for_user(app.state.alice_user_id)
    items_default = await app.state.user_memory_store.list_for_user(app.state.default_user_id)
    assert len([e for e in items_alice if e.kind == "satisfaction"]) == 1
    assert len([e for e in items_default if e.kind == "satisfaction"]) == 0
