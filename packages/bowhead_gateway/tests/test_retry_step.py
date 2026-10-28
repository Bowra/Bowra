"""chat-redesign p0-p1-followup B1 — POST /api/v1/turns/{turn_id}/retry_step/{step_id}
+ /skip_step/{step_id} endpoint tests.

Audit doc: docs/chat-redesign-prd-gap-audit-2026-05-23.md §2 P0-1
Spec: option-(b) stub pattern matches W7.1/W7.5 — validate + ack event + 202.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_gateway.main import create_app
from bowhead_gateway.routes.turns import _retry_step_dedup, _skip_step_dedup
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
    _retry_step_dedup.clear()
    _skip_step_dedup.clear()
    return app


# ─────────────────────────────────────────────────────────────
# retry_step — happy path + error paths
# ─────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_retry_step_happy_path(app_ctx) -> None:
    """Validate + ack: turn exists, step_id matches, turn not done.

    Returns 202 with {success, new_step_id, eta_seconds, deduped=False}.
    """
    store: TurnLogStore = app_ctx.state.turn_log_store
    await store.record(
        turn_id="t1",
        event_type="turn_started",
        payload={"user_text": "x"},
    )
    await store.record(
        turn_id="t1",
        event_type="step_started",
        payload={"step_id": "step_foo", "step_type": "tool_run"},
    )
    await store.record(
        turn_id="t1",
        event_type="step_done",
        payload={"step_id": "step_foo", "state": "failure"},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/turns/t1/retry_step/step_foo",
            json={"reason": "transient failure"},
        )
        assert r.status_code == 202
        body = r.json()
        assert body["success"] is True
        assert body["new_step_id"].startswith("step_retry_pending_")
        assert body["eta_seconds"] == 5
        assert body["deduped"] is False


@pytest.mark.integration
async def test_retry_step_404_unknown_step(app_ctx) -> None:
    """step_id not in turn events → 404."""
    store: TurnLogStore = app_ctx.state.turn_log_store
    await store.record(
        turn_id="t1",
        event_type="turn_started",
        payload={"user_text": "x"},
    )
    await store.record(
        turn_id="t1",
        event_type="step_started",
        payload={"step_id": "step_real", "step_type": "tool_run"},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/api/v1/turns/t1/retry_step/step_phantom")
        assert r.status_code == 404


@pytest.mark.integration
async def test_retry_step_404_unknown_turn(app_ctx) -> None:
    """Turn doesn't exist → 404."""
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/api/v1/turns/t_nope/retry_step/step_foo")
        assert r.status_code == 404


@pytest.mark.integration
async def test_retry_step_409_turn_done(app_ctx) -> None:
    """Turn already done → 409 (no retry on closed turn)."""
    store: TurnLogStore = app_ctx.state.turn_log_store
    await store.record(
        turn_id="t1",
        event_type="turn_started",
        payload={"user_text": "x"},
    )
    await store.record(
        turn_id="t1",
        event_type="step_started",
        payload={"step_id": "step_foo", "step_type": "tool_run"},
    )
    await store.record(
        turn_id="t1",
        event_type="step_done",
        payload={"step_id": "step_foo", "state": "failure"},
    )
    # Terminal event closes the turn.
    await store.record(turn_id="t1", event_type="done", payload={"status": "done"})

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/api/v1/turns/t1/retry_step/step_foo")
        assert r.status_code == 409


@pytest.mark.integration
async def test_retry_step_dedup_double_click(app_ctx) -> None:
    """Double-click yields same new_step_id, second call returns deduped=True."""
    store: TurnLogStore = app_ctx.state.turn_log_store
    await store.record(
        turn_id="t1",
        event_type="turn_started",
        payload={"user_text": "x"},
    )
    await store.record(
        turn_id="t1",
        event_type="step_started",
        payload={"step_id": "step_foo", "step_type": "tool_run"},
    )
    await store.record(
        turn_id="t1",
        event_type="step_done",
        payload={"step_id": "step_foo", "state": "failure"},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r1 = await c.post("/api/v1/turns/t1/retry_step/step_foo")
        r2 = await c.post("/api/v1/turns/t1/retry_step/step_foo")
        assert r1.status_code == 202
        assert r2.status_code == 202
        b1, b2 = r1.json(), r2.json()
        assert b1["new_step_id"] == b2["new_step_id"]
        assert b1["deduped"] is False
        assert b2["deduped"] is True


@pytest.mark.integration
async def test_retry_step_emits_ack_event(app_ctx) -> None:
    """retry_step emits a `turn.step_retry_requested` event on the turn bus.

    Verifies via the get_event_registry singleton (same bus the API touches).
    """
    from bowhead_core.events import get_event_registry

    store: TurnLogStore = app_ctx.state.turn_log_store
    await store.record(
        turn_id="t_emit",
        event_type="turn_started",
        payload={"user_text": "x"},
    )
    await store.record(
        turn_id="t_emit",
        event_type="step_started",
        payload={"step_id": "step_foo", "step_type": "tool_run"},
    )

    bus = await get_event_registry().get_or_create("t_emit")
    captured: list = []

    async def consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)
            if ev.event_type == "turn.step_retry_requested":
                return

    import asyncio

    consumer = asyncio.create_task(consume())

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/api/v1/turns/t_emit/retry_step/step_foo")
        assert r.status_code == 202

    try:
        await asyncio.wait_for(consumer, timeout=2.0)
    except TimeoutError:
        consumer.cancel()
        pytest.fail("did not see turn.step_retry_requested event within 2s")

    ack_events = [e for e in captured if e.event_type == "turn.step_retry_requested"]
    assert len(ack_events) == 1
    ack = ack_events[0]
    assert ack.payload["step_id"] == "step_foo"
    assert ack.payload["new_step_id"].startswith("step_retry_pending_")
    assert ack.payload["eta_seconds"] == 5


# ─────────────────────────────────────────────────────────────
# skip_step — happy path + error paths
# ─────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_skip_step_happy_path(app_ctx) -> None:
    """skip_step on a known step returns 202 with skip_marked=True."""
    store: TurnLogStore = app_ctx.state.turn_log_store
    await store.record(
        turn_id="t1",
        event_type="turn_started",
        payload={"user_text": "x"},
    )
    await store.record(
        turn_id="t1",
        event_type="step_started",
        payload={"step_id": "step_bar", "step_type": "tool_run"},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/turns/t1/skip_step/step_bar",
            json={"reason": "not relevant"},
        )
        assert r.status_code == 202
        body = r.json()
        assert body["success"] is True
        assert body["skip_marked"] is True
        assert body["deduped"] is False


@pytest.mark.integration
async def test_skip_step_404_unknown_step(app_ctx) -> None:
    """step_id not in turn events → 404."""
    store: TurnLogStore = app_ctx.state.turn_log_store
    await store.record(
        turn_id="t1",
        event_type="turn_started",
        payload={"user_text": "x"},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/api/v1/turns/t1/skip_step/step_phantom")
        assert r.status_code == 404


@pytest.mark.integration
async def test_skip_step_409_turn_done(app_ctx) -> None:
    """Skip on a closed turn → 409."""
    store: TurnLogStore = app_ctx.state.turn_log_store
    await store.record(
        turn_id="t1",
        event_type="turn_started",
        payload={"user_text": "x"},
    )
    await store.record(
        turn_id="t1",
        event_type="step_started",
        payload={"step_id": "step_bar", "step_type": "tool_run"},
    )
    await store.record(
        turn_id="t1",
        event_type="turn_failed",
        payload={"reason": "x"},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post("/api/v1/turns/t1/skip_step/step_bar")
        assert r.status_code == 409


@pytest.mark.integration
async def test_skip_step_dedup_double_click(app_ctx) -> None:
    """Double-click skip → 2nd call returns deduped=True, same skip_marked."""
    store: TurnLogStore = app_ctx.state.turn_log_store
    await store.record(
        turn_id="t1",
        event_type="turn_started",
        payload={"user_text": "x"},
    )
    await store.record(
        turn_id="t1",
        event_type="step_started",
        payload={"step_id": "step_bar", "step_type": "tool_run"},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r1 = await c.post("/api/v1/turns/t1/skip_step/step_bar")
        r2 = await c.post("/api/v1/turns/t1/skip_step/step_bar")
        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["deduped"] is False
        assert r2.json()["deduped"] is True
