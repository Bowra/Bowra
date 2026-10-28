"""Turns that ended after a cursor, for the completion popup (agent run limits, phase 3)."""

import json
import sqlite3

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.session_store import SessionStore

pytestmark = pytest.mark.unit


async def _store(tmp_path) -> SessionStore:
    assert upgrade(f"sqlite+aiosqlite:///{tmp_path / 'requested.db'}", target="core") == ("core",)
    store = SessionStore(str(tmp_path / "bowhead.db"))
    await store.initialize()
    return store


async def _turn(store: SessionStore, *, session_id: str, turn_id: str, user_id: str, status: str, ended_at: str | None) -> None:
    await store.ensure_session(session_id=session_id, workspace_id="ws_1", user_id=user_id)
    await store.record_turn(turn_id=turn_id, session_id=session_id, user_text="analyse")
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE turns SET status=?, ended_at=? WHERE id=?", (status, ended_at, turn_id))
        db.execute("UPDATE sessions SET title=? WHERE id=?", (f"title {session_id}", session_id))


@pytest.mark.asyncio
async def test_ended_turns_after_the_cursor_with_their_outcome(tmp_path) -> None:
    store = await _store(tmp_path)
    await _turn(store, session_id="s_old", turn_id="t_old", user_id="u1", status="completed", ended_at="2026-10-21T08:00:00+00:00")
    await _turn(store, session_id="s_done", turn_id="t_done", user_id="u1", status="completed", ended_at="2026-10-21T09:00:00+00:00")
    await _turn(store, session_id="s_part", turn_id="t_part", user_id="u1", status="completed", ended_at="2026-10-21T09:01:00+00:00")
    await _turn(store, session_id="s_fail", turn_id="t_fail", user_id="u1", status="failed", ended_at="2026-10-21T09:02:00+00:00")
    await _turn(store, session_id="s_run", turn_id="t_run", user_id="u1", status="running", ended_at=None)
    await _turn(store, session_id="s_other", turn_id="t_other", user_id="u2", status="completed", ended_at="2026-10-21T09:03:00+00:00")
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO turn_events(turn_id,seq,event_type,payload_json,occurred_at) VALUES(?,?,?,?,?)",
            ("t_part", 5, "lead.subagent_summary", json.dumps({"status": "partial", "stop_reason": "time_limit"}), "2026-10-21T09:01:00+00:00"),
        )

    items = await store.list_turn_completions(user_id="u1", after="2026-10-21T08:30:00+00:00")
    assert [(i["turn_id"], i["outcome"]) for i in items] == [("t_done", "completed"), ("t_part", "partial"), ("t_fail", "failed")]
    assert items[0]["title"] == "title s_done"
