"""V3 PRD-B · MetricsLog unit tests."""

from __future__ import annotations

import pytest
from bowhead_core.metrics_log import MetricsLog


@pytest.fixture
async def log(tmp_path):
    m = MetricsLog(tmp_path / "metrics.db")
    await m.initialize()
    return m


@pytest.mark.asyncio
async def test_track_inserts_event(log):
    await log.track(
        "memory.candidate.approve",
        user_id="u1",
        attrs={"cid": "cand_abc", "kind": "preference"},
    )
    rows = await log.query_recent(name="memory.candidate.approve")
    assert len(rows) == 1
    assert rows[0]["user_id"] == "u1"
    assert rows[0]["attrs"]["cid"] == "cand_abc"


@pytest.mark.asyncio
async def test_query_recent_filters_by_name(log):
    await log.track("memory.entry.create", user_id="u1")
    await log.track("memory.entry.delete", user_id="u1")
    rows = await log.query_recent(name="memory.entry.create")
    assert len(rows) == 1
    assert rows[0]["name"] == "memory.entry.create"


@pytest.mark.asyncio
async def test_query_recent_filters_by_user(log):
    await log.track("memory.export.requested", user_id="alice")
    await log.track("memory.export.requested", user_id="bob")
    rows = await log.query_recent(user_id="alice")
    assert len(rows) == 1
    assert rows[0]["user_id"] == "alice"


@pytest.mark.asyncio
async def test_track_handles_missing_attrs(log):
    await log.track("memory.dreaming.paused", user_id="u1")
    rows = await log.query_recent(name="memory.dreaming.paused")
    assert rows[0]["attrs"] == {}


@pytest.mark.asyncio
async def test_track_swallows_errors(tmp_path):
    """track() best-effort: failures must not raise (PRD-B spec)."""
    bad = MetricsLog(tmp_path / "nonexistent_dir/nope.db")
    # initialize would create it, so skip init to force a failure path
    # ensure track returns None instead of raising
    result = await bad.track("oops", user_id="u1")
    assert result is None


@pytest.mark.asyncio
async def test_hard_delete_old_trims(log, tmp_path):
    await log.track("old", user_id="u1")
    await log.track("fresh", user_id="u1")

    import aiosqlite

    async with aiosqlite.connect(str(tmp_path / "metrics.db")) as db:
        await db.execute(
            "UPDATE metrics_event_log SET created_at=datetime('now', '-200 days') WHERE name='old'",
        )
        await db.commit()

    n = await log.hard_delete_old(max_age_days=90)
    assert n == 1
    remaining = await log.query_recent(hours=24 * 365)
    assert len(remaining) == 1
    assert remaining[0]["name"] == "fresh"
