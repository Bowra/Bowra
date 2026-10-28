"""V3 PRD-B · MemoryCandidateStore.list_for_user / auto_reject_stale +
PauseStateCache + DreamingLight pause integration."""

from __future__ import annotations

import pytest
from bowhead_memory.candidates import (
    SETTINGS_KEY_DREAMING_PAUSED,
    DreamingLight,
    DreamObservation,
    MemoryCandidateStore,
    PauseStateCache,
)


@pytest.fixture
async def store(tmp_path):
    s = MemoryCandidateStore(db_path=str(tmp_path / "cand.db"))
    await s.initialize()
    return s


# ─── list_for_user (status filter) ──────────────────


@pytest.mark.asyncio
async def test_list_for_user_pending_only_default(store):
    a = await store.enqueue(
        user_id="u1",
        kind="preference",
        key="a",
        value="x",
        reason="r",
        source_turn_id=None,
    )
    b = await store.enqueue(
        user_id="u1",
        kind="preference",
        key="b",
        value="y",
        reason="r",
        source_turn_id=None,
    )
    await store.set_decision(cid=b.id, status="approved", decided_by="u1")

    rows = await store.list_for_user(user_id="u1")
    assert len(rows) == 1
    assert rows[0].id == a.id
    assert rows[0].status == "pending"


@pytest.mark.asyncio
async def test_list_for_user_status_all(store):
    await store.enqueue(
        user_id="u1",
        kind="preference",
        key="a",
        value="x",
        reason="r",
        source_turn_id=None,
    )
    b = await store.enqueue(
        user_id="u1",
        kind="preference",
        key="b",
        value="y",
        reason="r",
        source_turn_id=None,
    )
    await store.set_decision(cid=b.id, status="rejected", decided_by="u1")

    rows = await store.list_for_user(user_id="u1", status="all")
    assert len(rows) == 2
    statuses = {r.status for r in rows}
    assert statuses == {"pending", "rejected"}


@pytest.mark.asyncio
async def test_list_for_user_status_approved_only(store):
    a = await store.enqueue(
        user_id="u1",
        kind="preference",
        key="a",
        value="x",
        reason="r",
        source_turn_id=None,
    )
    await store.set_decision(cid=a.id, status="approved", decided_by="u1")
    await store.enqueue(
        user_id="u1",
        kind="preference",
        key="b",
        value="y",
        reason="r",
        source_turn_id=None,
    )

    rows = await store.list_for_user(user_id="u1", status="approved")
    assert len(rows) == 1
    assert rows[0].id == a.id


@pytest.mark.asyncio
async def test_list_for_user_invalid_status(store):
    with pytest.raises(ValueError):
        await store.list_for_user(user_id="u1", status="bogus")


@pytest.mark.asyncio
async def test_list_for_user_isolates_users(store):
    await store.enqueue(
        user_id="u1",
        kind="preference",
        key="a",
        value="x",
        reason=None,
        source_turn_id=None,
    )
    await store.enqueue(
        user_id="u2",
        kind="preference",
        key="b",
        value="y",
        reason=None,
        source_turn_id=None,
    )
    u1_rows = await store.list_for_user(user_id="u1")
    assert len(u1_rows) == 1
    assert u1_rows[0].user_id == "u1"


# ─── auto_reject_stale ──────────────────────────────


@pytest.mark.asyncio
async def test_auto_reject_stale_rejects_old_pending(store, tmp_path):
    fresh = await store.enqueue(
        user_id="u1",
        kind="preference",
        key="fresh",
        value="x",
        reason=None,
        source_turn_id=None,
    )
    stale = await store.enqueue(
        user_id="u1",
        kind="preference",
        key="stale",
        value="y",
        reason=None,
        source_turn_id=None,
    )

    # Mock the old candidate created_at to 60 days ago.
    import aiosqlite

    async with aiosqlite.connect(str(tmp_path / "cand.db")) as db:
        await db.execute(
            "UPDATE memory_candidates SET created_at=datetime('now', '-60 days') WHERE id=?",
            (stale.id,),
        )
        await db.commit()

    n = await store.auto_reject_stale(max_age_days=30)
    assert n == 1

    refreshed_stale = await store.get(stale.id)
    assert refreshed_stale.status == "rejected"
    assert refreshed_stale.decided_by == "system_auto_reject"
    assert refreshed_stale.decision_kind == "system_expired"
    assert refreshed_stale.key == ""
    assert refreshed_stale.value is None
    assert refreshed_stale.reason is None
    assert refreshed_stale.source_turn_id is None

    refreshed_fresh = await store.get(fresh.id)
    assert refreshed_fresh.status == "pending"


@pytest.mark.asyncio
async def test_auto_reject_stale_skips_already_decided(store, tmp_path):
    a = await store.enqueue(
        user_id="u1",
        kind="preference",
        key="a",
        value="x",
        reason=None,
        source_turn_id=None,
    )
    await store.set_decision(cid=a.id, status="approved", decided_by="u1")

    import aiosqlite

    async with aiosqlite.connect(str(tmp_path / "cand.db")) as db:
        await db.execute(
            "UPDATE memory_candidates SET created_at=datetime('now', '-60 days') WHERE id=?",
            (a.id,),
        )
        await db.commit()

    n = await store.auto_reject_stale(max_age_days=30)
    assert n == 0  # already approved, not pending — untouched
    refreshed = await store.get(a.id)
    assert refreshed.status == "approved"


@pytest.mark.asyncio
async def test_auto_reject_stale_redacts_expired_user_rejection(
    store,
    tmp_path,
):
    candidate = await store.enqueue(
        user_id="u1",
        kind="preference",
        key="grain",
        value="monthly",
        reason="requested before",
        source_turn_id="t_1",
    )
    await store.set_decision(
        cid=candidate.id,
        status="rejected",
        decided_by="u1",
    )

    import aiosqlite

    async with aiosqlite.connect(str(tmp_path / "cand.db")) as db:
        await db.execute(
            """
            UPDATE memory_candidates
            SET decided_at=datetime('now', '-31 days')
            WHERE id=?
            """,
            (candidate.id,),
        )
        await db.execute(
            """
            UPDATE memory_candidate_suppressions
            SET expires_at=datetime('now', '-1 day')
            WHERE user_id='u1'
            """,
        )
        await db.commit()

    assert await store.auto_reject_stale(max_age_days=30) == 0

    redacted = await store.get(candidate.id)
    assert redacted is not None
    assert redacted.status == "rejected"
    assert redacted.key == ""
    assert redacted.value is None
    assert redacted.reason is None
    async with aiosqlite.connect(str(tmp_path / "cand.db")) as db:
        [remaining] = await db.execute_fetchall(
            """
            SELECT COUNT(*)
            FROM memory_candidate_suppressions
            WHERE user_id='u1'
            """,
        )
    assert remaining[0] == 0


# ─── PauseStateCache ────────────────────────────────


class _StubSettings:
    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.calls = 0

    async def get(self, user_id: str, key: str) -> str | None:
        self.calls += 1
        if key == SETTINGS_KEY_DREAMING_PAUSED:
            return self.value
        return None


@pytest.mark.asyncio
async def test_pause_cache_default_unpaused():
    cache = PauseStateCache(settings_store=_StubSettings(None))
    assert await cache.is_paused("u1") is False


@pytest.mark.asyncio
async def test_pause_cache_reads_settings_value():
    cache = PauseStateCache(settings_store=_StubSettings("true"))
    assert await cache.is_paused("u1") is True


@pytest.mark.asyncio
async def test_pause_cache_caches_within_ttl():
    settings = _StubSettings("true")
    cache = PauseStateCache(settings_store=settings, ttl_seconds=60.0)
    await cache.is_paused("u1")
    await cache.is_paused("u1")
    assert settings.calls == 1  # 2nd hit served from cache


@pytest.mark.asyncio
async def test_pause_cache_invalidate_forces_refresh():
    settings = _StubSettings("true")
    cache = PauseStateCache(settings_store=settings, ttl_seconds=60.0)
    await cache.is_paused("u1")
    cache.invalidate("u1")
    settings.value = "false"  # user un-paused
    assert await cache.is_paused("u1") is False
    assert settings.calls == 2


@pytest.mark.asyncio
async def test_pause_cache_no_settings_store():
    cache = PauseStateCache(settings_store=None)
    assert await cache.is_paused("u1") is False


@pytest.mark.asyncio
async def test_pause_cache_empty_user_id_safe():
    cache = PauseStateCache(settings_store=_StubSettings("true"))
    assert await cache.is_paused("") is False  # no DB hit on empty


# ─── DreamingLight pause integration ────────────────


class _StubRouter:
    def __init__(self) -> None:
        self.invoked = False

    async def invoke(self, agent_type, req):
        self.invoked = True
        raise RuntimeError("should not be called when paused")


@pytest.mark.asyncio
async def test_dreaming_skipped_when_paused(store):
    settings = _StubSettings("true")
    cache = PauseStateCache(settings_store=settings, ttl_seconds=60.0)
    router = _StubRouter()
    light = DreamingLight(
        router=router,
        candidate_store=store,
        pause_cache=cache,
        min_interval_seconds=0,
    )
    obs = DreamObservation(
        turn_id="t1",
        user_id="u1",
        user_text="hi",
        final_answer="ok",
        intent="exploration",
        tool_chain=["bi"],
    )
    out = await light.observe(obs)
    assert out == []
    assert router.invoked is False
