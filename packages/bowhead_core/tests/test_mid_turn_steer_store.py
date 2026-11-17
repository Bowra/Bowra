"""Mid-run steering: the steer buffer head a running Turn may take, and the inbox that takes it."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.events import OrchestratorEvent
from bowhead_core.session_store import SessionStore
from bowhead_core.steer_inbox import SteerInbox, attach_steer_inbox
from bowhead_core.turn_log_store import TurnLogStore
from test_agent_revision_store import _db

pytestmark = pytest.mark.asyncio

SESSION = "s_mid_turn_steer"
TURN = "t_mid_turn_steer"


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(_db(tmp_path))


async def running_turn(db_path: str, buffer: list[dict]) -> SessionStore:
    """A running Turn that owns a live dispatch, with ``buffer`` queued behind it."""
    store = SessionStore(db_path)
    await store.ensure_session(session_id=SESSION, workspace_id="ws_1", user_id="u_1")
    await store.record_turn(turn_id=TURN, session_id=SESSION, user_text="Draw the chart.")
    assert await store.claim_chat_spawn_dispatch_for_worker(turn_id=TURN, owner_id="worker-1") == "claimed"
    write(db_path, "UPDATE sessions SET steer_buffer_json=? WHERE id=?", (json.dumps(buffer, ensure_ascii=False), SESSION))
    return store


def write(db_path: str, statement: str, parameters: tuple = ()) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(statement, parameters)
        connection.commit()


def stored_buffer(db_path: str) -> list[dict]:
    with sqlite3.connect(db_path) as connection:
        return json.loads(connection.execute("SELECT steer_buffer_json FROM sessions WHERE id=?", (SESSION,)).fetchone()[0])


PLAIN_A = {"text": "Use green bars.", "ts": "2026-11-15T12:00:00+00:00"}
PLAIN_B = {"text": "Add a title.", "attachment_ids": [], "attachment_refs": [], "submission_id": "sub-b", "ts": "2026-11-15T12:00:05+00:00"}
WITH_FILE = {
    "text": "Use this file.",
    "attachment_ids": ["att_1"],
    "attachment_refs": [],
    "submission_id": "sub-c",
    "ts": "2026-11-15T12:00:09+00:00",
}
REFERENCE = {"text": "@Analyst check it", "submission_id": "sub-d", "reference_submission_id": "sub-d", "ts": "2026-11-15T12:00:12+00:00"}


@pytest.mark.parametrize("boundary", [WITH_FILE, REFERENCE], ids=["attachment", "reference"])
async def test_head_is_the_plain_text_items_before_an_attachment_or_reference(db_path: str, boundary: dict) -> None:
    store = await running_turn(db_path, [PLAIN_A, PLAIN_B, boundary, PLAIN_A])

    head = await store.read_steer_head(session_id=SESSION, turn_id=TURN)

    assert head == [PLAIN_A, PLAIN_B], "nothing queued behind an attachment or a reference is taken, so the queue keeps its order"
    assert await store.claim_steer_head(session_id=SESSION, turn_id=TURN, items=head) is True
    assert stored_buffer(db_path) == [boundary, PLAIN_A], "the items behind the head stay queued for the follow-up path"
    assert await store.get_active_turn(SESSION) == TURN, "taking an instruction never releases the running Turn"


async def test_an_item_that_starts_with_an_attachment_blocks_the_head(db_path: str) -> None:
    store = await running_turn(db_path, [WITH_FILE, PLAIN_A])

    assert await store.read_steer_head(session_id=SESSION, turn_id=TURN) == []


async def test_an_instruction_appended_after_the_read_stays_queued(db_path: str) -> None:
    store = await running_turn(db_path, [PLAIN_A])
    head = await store.read_steer_head(session_id=SESSION, turn_id=TURN)
    assert await store.append_steer_buffer(session_id=SESSION, text="One more thing.", expected_turn_id=TURN)

    assert await store.claim_steer_head(session_id=SESSION, turn_id=TURN, items=head) is True

    assert [item["text"] for item in stored_buffer(db_path)] == ["One more thing."]


@pytest.mark.parametrize(
    "fence",
    ["no_dispatch", "expired_lease", "other_generation", "another_active_turn", "stopped", "head_changed"],
)
async def test_nothing_is_read_or_claimed_once_the_turn_no_longer_owns_the_buffer(db_path: str, fence: str) -> None:
    store = await running_turn(db_path, [PLAIN_A, PLAIN_B])
    head = await store.read_steer_head(session_id=SESSION, turn_id=TURN)
    assert head == [PLAIN_A, PLAIN_B]
    if fence == "no_dispatch":
        write(db_path, "DELETE FROM chat_turn_spawn_dispatch WHERE turn_id=?", (TURN,))
    elif fence == "expired_lease":
        expired = (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat()
        write(db_path, "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?", (expired, TURN))
    elif fence == "other_generation":
        write(db_path, "UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=2 WHERE turn_id=?", (TURN,))
    elif fence == "another_active_turn":
        write(db_path, "UPDATE sessions SET active_turn_id='t_other' WHERE id=?", (SESSION,))
    elif fence == "stopped":
        write(db_path, "UPDATE turns SET status='cancelled' WHERE id=?", (TURN,))
    else:
        write(db_path, "UPDATE sessions SET steer_buffer_json=? WHERE id=?", (json.dumps([PLAIN_B]), SESSION))
    before = stored_buffer(db_path)

    claimed = await store.claim_steer_head(session_id=SESSION, turn_id=TURN, items=head)

    assert claimed is False
    assert stored_buffer(db_path) == before, "a refused claim leaves every queued instruction for finalization or recovery"
    if fence != "head_changed":
        assert await store.read_steer_head(session_id=SESSION, turn_id=TURN) == []


async def test_a_malformed_buffer_is_left_for_the_existing_isolation_path(db_path: str) -> None:
    store = await running_turn(db_path, [PLAIN_A])
    write(db_path, 'UPDATE sessions SET steer_buffer_json=\'{"not": "a list"}\' WHERE id=?', (SESSION,))

    assert await store.read_steer_head(session_id=SESSION, turn_id=TURN) == []
    assert await store.claim_steer_head(session_id=SESSION, turn_id=TURN, items=[PLAIN_A]) is False


class RecordingBus:
    """Writes each event row before it reports delivery, like the legacy chat bus."""

    def __init__(self, db_path: str, *, accepting: bool = True) -> None:
        self.log = TurnLogStore(db_path)
        self.accepting = accepting
        self.events: list[OrchestratorEvent] = []

    async def emit(self, event: OrchestratorEvent) -> bool:
        if not self.accepting:
            return False
        await self.log.initialize()
        assert await self.log.record(turn_id=event.turn_id, event_type=event.event_type, payload=event.payload) is not None
        self.events.append(event)
        return True


def inbox_for(store: SessionStore, bus: RecordingBus) -> tuple[SteerInbox, SimpleNamespace]:
    ctx = SimpleNamespace(user_text="Draw the chart.")
    return SteerInbox(store=store, bus=bus, session_id=SESSION, turn_id=TURN, request_text="Draw the chart."), ctx


async def test_take_records_the_instructions_then_removes_them_and_extends_the_request(db_path: str) -> None:
    store = await running_turn(db_path, [PLAIN_A, PLAIN_B])
    bus = RecordingBus(db_path)
    inbox, ctx = inbox_for(store, bus)

    texts = await inbox.take(ctx, iteration=2)

    assert texts == ["Use green bars.", "Add a title."]
    assert stored_buffer(db_path) == []
    assert ctx.user_text == "Draw the chart.\n\nUse green bars.\n\nAdd a title.", "helpers started later check against everything the user asked"
    payload = bus.events[0].payload
    assert bus.events[0].event_type == "steer.applied" and bus.events[0].persist_before_delivery is True
    assert payload["items"] == [
        {"text": "Use green bars.", "queued_at": PLAIN_A["ts"]},
        {"text": "Add a title.", "queued_at": PLAIN_B["ts"], "submission_id": "sub-b"},
    ]
    assert payload["position"] == 0 and payload["iteration"] == 2 and payload["while_waiting"] is False and payload["waited_ms"] > 0
    assert await store.list_turn_applied_instructions([TURN, "t_unknown"]) == {TURN: ["Use green bars.", "Add a title."]}
    assert await inbox.take(ctx, iteration=3) == [], "an empty buffer takes nothing and records nothing"
    assert len(bus.events) == 1
    assert await store.append_steer_buffer(session_id=SESSION, text="Sort descending.", expected_turn_id=TURN)
    assert await inbox.take(ctx, iteration=4) == ["Sort descending."]
    assert bus.events[1].payload["position"] == 2, "each instruction keeps its ordinal in the Turn across events"


async def test_an_instruction_stays_queued_when_its_record_cannot_be_written(db_path: str) -> None:
    store = await running_turn(db_path, [PLAIN_A])
    inbox, ctx = inbox_for(store, RecordingBus(db_path, accepting=False))

    assert await inbox.take(ctx, iteration=1) == []

    assert stored_buffer(db_path) == [PLAIN_A]
    assert ctx.user_text == "Draw the chart."
    assert inbox.closed is False, "a later step may still take it if the bus accepts again"


async def test_a_failed_read_takes_nothing_and_the_next_step_reads_again(db_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    store = await running_turn(db_path, [PLAIN_A])
    bus = RecordingBus(db_path)
    inbox, ctx = inbox_for(store, bus)
    original = store.read_steer_head

    async def unavailable(**kwargs):
        raise OSError("database is locked")

    monkeypatch.setattr(store, "read_steer_head", unavailable)
    assert await inbox.take(ctx, iteration=1) == []
    assert bus.events == [] and inbox.closed is False and stored_buffer(db_path) == [PLAIN_A]

    monkeypatch.setattr(store, "read_steer_head", original)
    assert await inbox.take(ctx, iteration=2) == ["Use green bars."]


async def test_a_refused_claim_returns_nothing_and_closes_the_inbox(db_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    store = await running_turn(db_path, [PLAIN_A])
    bus = RecordingBus(db_path)
    inbox, ctx = inbox_for(store, bus)
    original = store.read_steer_head

    async def read_then_lose_the_dispatch(**kwargs):
        head = await original(**kwargs)
        write(db_path, "UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=2 WHERE turn_id=?", (TURN,))
        return head

    monkeypatch.setattr(store, "read_steer_head", read_then_lose_the_dispatch)

    assert await inbox.take(ctx, iteration=1) == []

    assert stored_buffer(db_path) == [PLAIN_A], "the instruction stays queued for the existing recovery path"
    assert inbox.closed is True and ctx.user_text == "Draw the chart."
    write(db_path, "UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=1 WHERE turn_id=?", (TURN,))
    assert await inbox.take(ctx, iteration=2) == [], "a closed inbox never records the same text twice"
    assert len(bus.events) == 1


async def test_only_an_ordinary_lead_context_gets_an_inbox(db_path: str) -> None:
    store = await running_turn(db_path, [])
    bus = RecordingBus(db_path)

    def context(**bound) -> SimpleNamespace:
        return SimpleNamespace(session_store=store, session_id=SESSION, user_text="Draw the chart.", steer_inbox=None, **bound)

    for key in (
        "_reference_runtime",
        "_agent_binding",
        "plan_execution_id",
        "configuration_settings_target",
        "coach_canvas_target",
        "document_revision_target",
        "_bound_workflow",
    ):
        bound = context(**{key: object()})
        await attach_steer_inbox(bound, bus=bus, turn_id=TURN)
        assert bound.steer_inbox is None, f"a Turn bound by {key} keeps today's follow-up behavior"

    ordinary = context()
    await attach_steer_inbox(ordinary, bus=bus, turn_id=TURN)
    assert isinstance(ordinary.steer_inbox, SteerInbox) and ordinary.steer_inbox.request_text == "Draw the chart."


async def test_a_resumed_turn_restores_the_instructions_it_already_took(db_path: str) -> None:
    store = await running_turn(db_path, [PLAIN_A])
    bus = RecordingBus(db_path)
    first, first_ctx = inbox_for(store, bus)
    assert await first.take(first_ctx, iteration=1) == ["Use green bars."]
    resumed = SimpleNamespace(session_store=store, session_id=SESSION, user_text="Draw the chart.", steer_inbox=None)

    await attach_steer_inbox(resumed, bus=bus, turn_id=TURN)

    assert resumed.user_text == "Draw the chart.\n\nUse green bars."
    assert resumed.steer_inbox.taken == ["Use green bars."]
