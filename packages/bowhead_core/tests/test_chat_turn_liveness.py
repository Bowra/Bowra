"""A running chat turn is alive while its worker renews the dispatch lease (agent run limits, phase 3)."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.session_store import SessionStore

pytestmark = pytest.mark.unit


async def _store(tmp_path) -> SessionStore:
    assert upgrade(f"sqlite+aiosqlite:///{tmp_path / 'requested.db'}", target="core") == ("core",)
    store = SessionStore(str(tmp_path / "bowhead.db"))
    await store.initialize()
    await store.ensure_session(session_id="s1", workspace_id="w", user_id="u1")
    await store.record_turn(turn_id="t1", session_id="s1", user_text="analyse")
    return store


def _dispatch(store: SessionStore, *, state: str, expires_in_s: float) -> None:
    now = datetime.now(UTC).isoformat()
    expires = (datetime.now(UTC) + timedelta(seconds=expires_in_s)).isoformat()
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO chat_turn_spawn_dispatch(turn_id,session_id,dispatch_kind,state,lease_expires_at,created_at,updated_at) "
            "VALUES('t1','s1','start',?,?,?,?)",
            (state, expires, now, now),
        )


@pytest.mark.asyncio
async def test_a_renewed_lease_is_alive_and_an_expired_one_is_not(tmp_path) -> None:
    store = await _store(tmp_path)
    _dispatch(store, state="claimed", expires_in_s=30)
    assert await store.chat_turn_execution_alive(turn_id="t1") is True
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at=?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
    assert await store.chat_turn_execution_alive(turn_id="t1") is False


@pytest.mark.asyncio
async def test_a_settled_dispatch_is_not_alive_and_an_untracked_turn_cannot_be_proven_dead(tmp_path) -> None:
    store = await _store(tmp_path)
    assert await store.chat_turn_execution_alive(turn_id="t1") is True
    _dispatch(store, state="terminal", expires_in_s=30)
    assert await store.chat_turn_execution_alive(turn_id="t1") is False
