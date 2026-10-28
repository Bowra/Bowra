"""chat-v4 CHANGE-W7.1 — POST /api/v1/turns/{turn_id}/retry_dispatch tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_gateway.main import create_app
from bowhead_gateway.routes.turns import _retry_dispatch_dedup
from bowhead_gateway.settings import Settings

from bowhead_core import TurnLogStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def app_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    store = TurnLogStore(db_path=str(tmp_path / "log.db"))
    await store.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.turn_log_store = store
    app.state.orchestrator = None
    app.state.datarc = None
    # Clear dedup between tests.
    _retry_dispatch_dedup.clear()
    return app


async def _seed_turn_with_spawn(
    store: TurnLogStore,
    turn_id: str,
    spawn_id: str,
    *,
    returned: bool = True,
) -> None:
    await store.record(
        turn_id=turn_id,
        event_type="turn_started",
        payload={"user_text": "x"},
    )
    await store.record(
        turn_id=turn_id,
        event_type="peer_dispatched",
        payload={"spawn_id": spawn_id, "profile": "compute"},
    )
    if returned:
        await store.record(
            turn_id=turn_id,
            event_type="agent_spawned",
            payload={"subagent_id": spawn_id, "status": "completed"},
        )


@pytest.mark.integration
async def test_retry_dispatch_happy_path(app_ctx) -> None:
    """chat-v4 W4-fix CTO P0-3 — retry_dispatch returns 503 (option-b stub).

    Previously returned 202 + synthetic ``t_retry_{turn_id}_{spawn_id}``
    that referenced no real turn → FE GET /events would 404 forever.
    Now: validation logic (turn lookup / spawn match / peer-returned) all
    runs and returns 503 with structured error body when stub is reached.
    """
    store: TurnLogStore = app_ctx.state.turn_log_store
    await _seed_turn_with_spawn(store, "t1", "sub_abc")

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/turns/t1/retry_dispatch",
            json={"spawn_id": "sub_abc"},
        )
        # P0-3: stub returns 503 (not 202+phantom_id). Verify clean error.
        assert r.status_code == 503
        body = r.json()
        # FastAPI HTTPException(detail=dict) wraps under "detail"
        detail = body.get("detail") or {}
        assert detail.get("code") == "retry_dispatch_not_implemented"
        assert "turn_id" in detail
        assert "spawn_id" in detail
        # NO phantom new_turn_id returned to caller.
        assert "new_turn_id" not in detail


@pytest.mark.integration
async def test_retry_dispatch_503_no_phantom_id(app_ctx) -> None:
    """P0-3 explicit contract: stub MUST NOT return 202 with phantom id."""
    store: TurnLogStore = app_ctx.state.turn_log_store
    await _seed_turn_with_spawn(store, "t1", "sub_abc")

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/turns/t1/retry_dispatch",
            json={"spawn_id": "sub_abc"},
        )
        assert r.status_code != 202, "503 stub must not look like real queued turn"
        assert r.status_code == 503


@pytest.mark.integration
async def test_retry_dispatch_404_turn(app_ctx) -> None:
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/turns/unknown/retry_dispatch",
            json={"spawn_id": "sub_abc"},
        )
        assert r.status_code == 404


@pytest.mark.integration
async def test_retry_dispatch_404_spawn(app_ctx) -> None:
    store: TurnLogStore = app_ctx.state.turn_log_store
    await _seed_turn_with_spawn(store, "t1", "sub_xyz")

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/turns/t1/retry_dispatch",
            json={"spawn_id": "sub_NOT_THERE"},
        )
        assert r.status_code == 404


@pytest.mark.integration
async def test_retry_dispatch_400_not_returned(app_ctx) -> None:
    """Spawn exists but peer never returned (no agent_spawned status=completed)
    → 400 with `did not return successfully` message."""
    store: TurnLogStore = app_ctx.state.turn_log_store
    await _seed_turn_with_spawn(store, "t1", "sub_abc", returned=False)

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/turns/t1/retry_dispatch",
            json={"spawn_id": "sub_abc"},
        )
        assert r.status_code == 400


@pytest.mark.integration
async def test_retry_dispatch_dedup_double_click(app_ctx) -> None:
    """SRE P0: double-click within 60s. Under P0-3 option-b stub, both
    return 503 with the same structured error (no phantom id leak).
    """
    store: TurnLogStore = app_ctx.state.turn_log_store
    await _seed_turn_with_spawn(store, "t1", "sub_abc")

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r1 = await c.post(
            "/api/v1/turns/t1/retry_dispatch",
            json={"spawn_id": "sub_abc"},
        )
        r2 = await c.post(
            "/api/v1/turns/t1/retry_dispatch",
            json={"spawn_id": "sub_abc"},
        )
        # Both 503 (no phantom 202). Structured error parity.
        assert r1.status_code == 503
        assert r2.status_code == 503
        d1 = r1.json().get("detail") or {}
        d2 = r2.json().get("detail") or {}
        assert d1.get("code") == d2.get("code") == "retry_dispatch_not_implemented"
