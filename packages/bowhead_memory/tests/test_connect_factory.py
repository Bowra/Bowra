"""Deployment connections can be injected without coupling memory to core."""

import sqlite3

import aiosqlite
import pytest

from bowhead_memory import DreamingEngine, MemoryCandidateStore


@pytest.mark.asyncio
async def test_memory_candidate_and_dreaming_use_injected_connection_factory(tmp_path):
    path = str(tmp_path / "memory.db")
    calls = []

    def connect(database):
        calls.append(database)
        return aiosqlite.connect(database)

    store = MemoryCandidateStore(path, connect_factory=connect)
    await store.initialize()
    await store.enqueue(user_id="u_1", kind="preference", key="language", value="en", reason=None, source_turn_id=None)
    engine = DreamingEngine(path, connect_factory=connect)
    assert (await engine.rem_digest()).total_turns == 0
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE turn_events(turn_id TEXT, occurred_at TEXT, event_type TEXT, payload_json TEXT)")
    assert (await engine.deep_digest()).total_turns == 0
    assert calls == [path, path, path, path]
