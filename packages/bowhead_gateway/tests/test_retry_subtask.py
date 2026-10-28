"""chat-v4 CHANGE-W7.5 — POST /api/v1/turns/{turn_id}/retry_subtask tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_gateway.main import create_app
from bowhead_gateway.routes.turns import _retry_subtask_dedup
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
    _retry_subtask_dedup.clear()
    return app


@pytest.mark.integration
async def test_retry_subtask_happy_path(app_ctx) -> None:
    """chat-v4 W4-fix CTO P0-3 — same option-b stub treatment.
    Validation passes, then 503 not_implemented instead of phantom id.
    """
    store: TurnLogStore = app_ctx.state.turn_log_store
    await store.record(
        turn_id="t1",
        event_type="turn_started",
        payload={"user_text": "x"},
    )
    await store.record(
        turn_id="t1",
        event_type="phase.update",
        payload={"phase_id": "p1", "status": "failed"},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/turns/t1/retry_subtask",
            json={"subtask_id": "p1"},
        )
        assert r.status_code == 503
        body = r.json()
        detail = body.get("detail") or {}
        assert detail.get("code") == "retry_subtask_not_implemented"
        assert "new_turn_id" not in detail


@pytest.mark.integration
async def test_retry_subtask_404_unknown_id(app_ctx) -> None:
    store: TurnLogStore = app_ctx.state.turn_log_store
    await store.record(
        turn_id="t1",
        event_type="turn_started",
        payload={"user_text": "x"},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/turns/t1/retry_subtask",
            json={"subtask_id": "p_nope"},
        )
        assert r.status_code == 404


@pytest.mark.integration
async def test_retry_subtask_dedup_double_click(app_ctx) -> None:
    """P0-3 stub: dedup returns same 503 (no phantom id)."""
    store: TurnLogStore = app_ctx.state.turn_log_store
    await store.record(
        turn_id="t1",
        event_type="turn_started",
        payload={"user_text": "x"},
    )
    await store.record(
        turn_id="t1",
        event_type="phase.update",
        payload={"phase_id": "p1", "status": "failed"},
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r1 = await c.post(
            "/api/v1/turns/t1/retry_subtask",
            json={"subtask_id": "p1"},
        )
        r2 = await c.post(
            "/api/v1/turns/t1/retry_subtask",
            json={"subtask_id": "p1"},
        )
        assert r1.status_code == 503
        assert r2.status_code == 503
        d1 = r1.json().get("detail") or {}
        d2 = r2.json().get("detail") or {}
        assert d1.get("code") == d2.get("code") == "retry_subtask_not_implemented"
