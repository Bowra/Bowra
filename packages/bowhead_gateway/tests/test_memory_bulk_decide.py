"""V3 PRD-B Day 6-7 · POST /api/v1/memory/candidates/bulk-decide tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_core.metrics_log import MetricsLog
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings
from bowhead_memory.candidates import PauseStateCache

from bowhead_core import UserMemoryStore
from bowhead_memory import MemoryCandidateStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def app_ctx(tmp_path: Path):
    db = tmp_path / "t.db"
    mem = UserMemoryStore(db)
    await mem.initialize()
    candidates = MemoryCandidateStore(db_path=str(db))
    await candidates.initialize()
    settings_store = UserSettingsStore(db)
    await settings_store.init()
    metrics = MetricsLog(db)
    await metrics.initialize()
    user_store = UserStore(str(db))
    await user_store.initialize()
    pause_cache = PauseStateCache(settings_store=settings_store, ttl_seconds=60.0)

    app = create_app()
    app.state.settings = Settings()
    app.state.user_memory_store = mem
    app.state.memory_candidate_store = candidates
    app.state.user_settings_store = settings_store
    app.state.metrics_log = metrics
    app.state.user_store = user_store
    app.state.pause_state_cache = pause_cache
    app.state.orchestrator = None
    app.state.datarc = None
    return app


def _client(app):
    transport = httpx2.ASGITransport(app=app)
    return httpx2.AsyncClient(transport=transport, base_url="http://gw")


@pytest.mark.integration
async def test_bulk_approve_writes_all_to_user_memory(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    a = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="a",
        value="1",
        reason=None,
        source_turn_id=None,
    )
    b = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="b",
        value="2",
        reason=None,
        source_turn_id=None,
    )

    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/candidates/bulk-decide",
            json={"cids": [a.id, b.id], "action": "approve"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert body["succeeded"] == 2
        assert body["failed"] == 0

    mem: UserMemoryStore = app_ctx.state.user_memory_store
    keys = {e.key for e in await mem.list_for_user(uid)}
    assert keys == {"a", "b"}


@pytest.mark.integration
async def test_bulk_reject_marks_all_rejected(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    a = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="a",
        value="1",
        reason=None,
        source_turn_id=None,
    )
    b = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="b",
        value="2",
        reason=None,
        source_turn_id=None,
    )
    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/candidates/bulk-decide",
            json={"cids": [a.id, b.id], "action": "reject"},
        )
        assert r.status_code == 200
        assert r.json()["succeeded"] == 2

    assert (await cs.get(a.id)).status == "rejected"
    assert (await cs.get(b.id)).status == "rejected"


@pytest.mark.integration
async def test_bulk_approve_skips_cross_user(app_ctx) -> None:
    """Cross-user cids return per-item ok=False with error='not_found'."""
    uid = app_ctx.state.settings.dev_default_user
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    mine = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="m",
        value="x",
        reason=None,
        source_turn_id=None,
    )
    foreign = await cs.enqueue(
        user_id="u_bob",
        kind="preference",
        key="b",
        value="y",
        reason=None,
        source_turn_id=None,
    )
    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/candidates/bulk-decide",
            json={"cids": [mine.id, foreign.id], "action": "approve"},
        )
        body = r.json()
        assert body["succeeded"] == 1
        assert body["failed"] == 1
        per = {it["cid"]: it for it in body["items"]}
        assert per[mine.id]["ok"] is True
        assert per[foreign.id]["ok"] is False
        assert per[foreign.id]["error"] == "not_found"

    # Bob's candidate untouched
    assert (await cs.get(foreign.id)).status == "pending"


@pytest.mark.integration
async def test_bulk_decide_dedupes_cids(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    a = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="a",
        value="1",
        reason=None,
        source_turn_id=None,
    )
    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/candidates/bulk-decide",
            json={"cids": [a.id, a.id, a.id], "action": "approve"},
        )
        body = r.json()
        # Deduped to 1 unique cid
        assert body["total"] == 1
        assert body["succeeded"] == 1


@pytest.mark.integration
async def test_bulk_decide_respects_max_limit(app_ctx) -> None:
    cids = [f"cand_{i}" for i in range(201)]
    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/candidates/bulk-decide",
            json={"cids": cids, "action": "approve"},
        )
        assert r.status_code == 400


@pytest.mark.integration
async def test_bulk_decide_empty_cids_400(app_ctx) -> None:
    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/candidates/bulk-decide",
            json={"cids": [], "action": "approve"},
        )
        assert r.status_code == 400


@pytest.mark.integration
async def test_bulk_decide_invalid_action_400(app_ctx) -> None:
    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/candidates/bulk-decide",
            json={"cids": ["x"], "action": "delete"},
        )
        assert r.status_code == 400
