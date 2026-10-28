"""Resume checkpoints: capture, claim, supersede and purge (agent run limits, phase 2 §6)."""

import base64
import os
import sqlite3

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.session_store import SessionStore
from bowhead_router.types import Message, ToolCall

from bowhead_core import subagent_resume as sr

pytestmark = pytest.mark.unit

TRANSCRIPT = [
    Message(role="user", content="analyse sales by region"),
    Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="bi_query", arguments={"q": "sales"})]),
    Message(role="tool", content='{"rows": 3}', name="bi_query", tool_call_id="c1"),
]


@pytest.fixture(autouse=True)
def _fernet(monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())


async def _db(tmp_path) -> str:
    assert upgrade(f"sqlite+aiosqlite:///{tmp_path / 'requested.db'}", target="core") == ("core",)
    store = SessionStore(str(tmp_path / "bowhead.db"))
    await store.initialize()
    for session_id, turns in {"s_1": ("t_1", "t_2", "t_3"), "s_2": ("t_9",)}.items():
        await store.ensure_session(session_id=session_id, workspace_id="ws_1", user_id="u_1")
        await store.record_turn(turn_id=turns[0], session_id=session_id, user_text="go")
        with sqlite3.connect(store.db_path) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(turns)")]
            # Copies of the first turn stand in for later turns that are still running.
            for seq, turn_id in enumerate(turns[1:], start=2):
                values = ",".join("?" if c in ("id", "seq") else c for c in columns)
                params = [turn_id if c == "id" else seq for c in columns if c in ("id", "seq")]
                db.execute(f"INSERT INTO turns({','.join(columns)}) SELECT {values} FROM turns WHERE id=?", (*params, turns[0]))
    return store.db_path


async def _capture(db_path: str, **overrides) -> str:
    values = {
        "team_id": "team_1",
        "user_id": "u_1",
        "workspace_id": "ws_1",
        "session_id": "s_1",
        "turn_id": "t_1",
        "run_subagent_id": "sub_a",
        "profile": "compute",
        "agent_id": None,
        "agent_revision_id": "rev_1",
        "tool_names": ["bi_query", "subagent_return"],
        "task": "analyse sales by region",
        "completion": "partial",
        "stop_reason": "step_limit",
        "partial_done": "north and south",
        "partial_remaining": "east and west",
        "messages": TRANSCRIPT,
        "reference_inventory": [{"kind": "chart", "id": "ch_1", "meta": {}}],
    }
    return await sr.capture(db_path, **{**values, **overrides})


def _end(db_path: str, turn_id: str) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE turns SET status='completed', ended_at='2026-10-21T09:00:00+00:00' WHERE id=?", (turn_id,))


@pytest.mark.asyncio
async def test_a_partial_run_round_trips_and_ends_with_where_it_stopped(tmp_path):
    db_path = await _db(tmp_path)
    checkpoint_id = await _capture(db_path)

    [listed] = await sr.list_resumable(db_path, session_id="s_1", user_id="u_1", exclude_turn_id="t_2")
    assert (listed.id, listed.completion, listed.partial_remaining) == (checkpoint_id, "partial", "east and west")
    claimed = await sr.claim(db_path, checkpoint_id=checkpoint_id, session_id="s_1", user_id="u_1", turn_id="t_2")
    material = sr.open_material(claimed)
    assert [m.role for m in material.messages] == ["user", "assistant", "tool", "assistant"]
    assert "Remaining: east and west" in material.messages[-1].content
    assert material.reference_inventory == [{"kind": "chart", "id": "ch_1", "meta": {}}]
    with sqlite3.connect(db_path) as db:
        stored = db.execute("SELECT ciphertext FROM subagent_resume_checkpoints").fetchone()[0]
    assert "east and west" not in stored and "sales" not in stored


@pytest.mark.asyncio
async def test_claims_refuse_other_users_the_capturing_turn_and_a_second_turn(tmp_path):
    db_path = await _db(tmp_path)
    checkpoint_id = await _capture(db_path)

    for kwargs, code in (
        ({"user_id": "u_2", "turn_id": "t_2"}, "not_found"),
        ({"user_id": "u_1", "turn_id": "t_1"}, "same_turn"),
    ):
        with pytest.raises(sr.ResumeRefused) as refused:
            await sr.claim(db_path, checkpoint_id=checkpoint_id, session_id="s_1", **kwargs)
        assert refused.value.code == code
    await sr.claim(db_path, checkpoint_id=checkpoint_id, session_id="s_1", user_id="u_1", turn_id="t_2")
    # The same turn may claim again (recovery); a second turn may not while t_2 runs.
    await sr.claim(db_path, checkpoint_id=checkpoint_id, session_id="s_1", user_id="u_1", turn_id="t_2")
    with pytest.raises(sr.ResumeRefused) as refused:
        await sr.claim(db_path, checkpoint_id=checkpoint_id, session_id="s_1", user_id="u_1", turn_id="t_3")
    assert refused.value.code == "in_use"
    assert await sr.list_resumable(db_path, session_id="s_1", user_id="u_1") == []

    # Once the claiming turn ended without a newer checkpoint, the run is resumable again.
    _end(db_path, "t_2")
    await sr.claim(db_path, checkpoint_id=checkpoint_id, session_id="s_1", user_id="u_1", turn_id="t_3")


@pytest.mark.asyncio
async def test_a_resumed_run_supersedes_its_lineage(tmp_path):
    db_path = await _db(tmp_path)
    first = await _capture(db_path)
    await sr.claim(db_path, checkpoint_id=first, session_id="s_1", user_id="u_1", turn_id="t_2")
    second = await _capture(db_path, turn_id="t_2", completion="complete", stop_reason=None, partial_done=None, partial_remaining=None)

    assert [c.id for c in await sr.list_resumable(db_path, session_id="s_1", user_id="u_1")] == [second]
    # The older run is never restarted: the newer one holds the latest work.
    with pytest.raises(sr.ResumeRefused) as refused:
        await sr.claim(db_path, checkpoint_id=first, session_id="s_1", user_id="u_1", turn_id="t_3")
    assert (refused.value.code, refused.value.checkpoint) == ("superseded", None)
    with sqlite3.connect(db_path) as db:
        states = dict(db.execute("SELECT id, state FROM subagent_resume_checkpoints"))
    assert states == {first: "superseded", second: "live"}


@pytest.mark.asyncio
async def test_release_returns_a_claim(tmp_path):
    db_path = await _db(tmp_path)
    checkpoint_id = await _capture(db_path)
    await sr.claim(db_path, checkpoint_id=checkpoint_id, session_id="s_1", user_id="u_1", turn_id="t_2")
    await sr.release(db_path, checkpoint_id=checkpoint_id, turn_id="t_2")
    await sr.claim(db_path, checkpoint_id=checkpoint_id, session_id="s_1", user_id="u_1", turn_id="t_3")


@pytest.mark.asyncio
async def test_expiry_and_session_deletion_drop_the_transcript(tmp_path):
    db_path = await _db(tmp_path)
    expired = await _capture(db_path)
    kept = await _capture(db_path, session_id="s_2", turn_id="t_9", run_subagent_id="sub_b")
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE subagent_resume_checkpoints SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (expired,))
    assert await sr.purge_expired(db_path) == 1
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE sessions SET deleted_at='2026-10-21T09:00:00+00:00' WHERE id='s_2'")
        rows = dict(db.execute("SELECT id, state FROM subagent_resume_checkpoints WHERE ciphertext IS NULL"))
    assert rows == {expired: "purged", kept: "purged"}


@pytest.mark.asyncio
async def test_requests_keep_their_order_and_limit(tmp_path):
    db_path = await _db(tmp_path)
    await sr.record_requests(db_path, turn_id="t_2", checkpoint_ids=["rsc_b", "rsc_a", "rsc_b", "rsc_c", "rsc_d", "rsc_e"])
    await sr.record_requests(db_path, turn_id="t_2", checkpoint_ids=["rsc_b", "rsc_a"])
    assert await sr.requested_checkpoint_ids(db_path, turn_id="t_2") == ["rsc_b", "rsc_a", "rsc_c", "rsc_d"]


def test_a_large_transcript_clears_the_oldest_tool_results_to_fit():
    messages = [Message(role="user", content="task")]
    for index in range(40):
        messages.append(Message(role="assistant", content="", tool_calls=[ToolCall(id=f"c{index}", name="bi_query", arguments={})]))
        messages.append(Message(role="tool", content=base64.b64encode(os.urandom(60_000)).decode() + str(index), tool_call_id=f"c{index}"))
    packed, _ = sr.encode_material(messages, [], max_sealed=600_000)
    restored = sr.decode_material(packed).messages
    assert restored[2].content.startswith("[Removed when this run was saved")
    assert restored[-1].content.endswith("39")


def test_an_unpaired_transcript_is_not_resumable():
    assert not sr.transcript_is_resumable(TRANSCRIPT[:2])
    assert sr.transcript_is_resumable(TRANSCRIPT)


def test_agent_or_tool_changes_refuse_the_transcript():
    checkpoint = sr.ResumeCheckpoint(
        *("rsc_1", "team", "u", "ws", "s", "t", "sub", "compute", None, "rev_1"),
        sr.tool_names_hash(["a", "b"]),
        *("task", "partial", "step_limit", None, None, "claimed", "t2", "now", "later"),
    )
    assert sr.resume_refusal_for(checkpoint, profile="compute", agent_revision_id="rev_1", tool_names=["b", "a"]) is None
    assert sr.resume_refusal_for(checkpoint, profile="compute", agent_revision_id="rev_2", tool_names=["a", "b"]) == "agent_changed"
    assert sr.resume_refusal_for(checkpoint, profile="compute", agent_revision_id="rev_1", tool_names=["a"]) == "tools_changed"


@pytest.mark.asyncio
async def test_a_deleted_session_lists_and_saves_nothing(tmp_path):
    db_path = await _db(tmp_path)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE sessions SET deleted_at='2026-10-21T09:00:00+00:00' WHERE id='s_1'")
    await _capture(db_path)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM subagent_resume_checkpoints").fetchone()[0] == 0
    assert await sr.list_resumable(db_path, session_id="s_1", user_id="u_1") == []
