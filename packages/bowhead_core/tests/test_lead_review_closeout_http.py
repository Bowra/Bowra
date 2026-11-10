"""A delivery-review correction never turns Lead's question into unapproved work.

116 t_7edbc155f8d8: a chart edit failed, Lead asked the user how to continue, delivery
review bounced that reply with a user-role message, and Lead read it as the answer: it
started an alternative the user had not approved while the streamed question stayed
visible. The correction is now a runtime note, new work waits for the user, and the
bounced narrative is superseded identically live, persisted and on replay.
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import anyio
import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.subagent import PROFILES
from bowhead_core.tool_registry import ToolExecutionContext, ToolSpec
from bowhead_router.runtime_notes import RUNTIME_CORRECTION_NOTE_NAME, RUNTIME_CORRECTION_PREFIX
from bowhead_router.types import ToolSchema
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import events as event_bus
from bowhead_router import runtime_notes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]

QUESTION = "The chart could not be recolored. Should I add a new green copy instead? Reply with your choice."


async def test_review_correction_keeps_the_question_for_the_user(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    case = http_start
    live_events = []
    original_emit = event_bus.TurnEventBus.emit

    async def observe_emit(bus: event_bus.TurnEventBus, event: event_bus.OrchestratorEvent) -> bool:
        live_events.append(event)
        return await original_emit(bus, event)

    monkeypatch.setattr(event_bus.TurnEventBus, "emit", observe_emit)
    projected_notes = []
    original_projection = runtime_notes.model_messages

    def observe_projection(messages):
        projected_notes.extend(message for message in messages if message.role == "user" and message.name == RUNTIME_CORRECTION_NOTE_NAME)
        return original_projection(messages)

    monkeypatch.setattr(runtime_notes, "model_messages", observe_projection)
    enable_lead_writer(case, monkeypatch)
    registry = case.app.state.orchestrator.tool_registry
    register_clarification_tool(registry)
    edits = []

    async def recolor_chart(args: dict, ctx: ToolExecutionContext) -> dict:
        edits.append(args)
        return {"error": "chart_edit_incomplete", "hint": "The chart edit did not complete."}

    registry.register(
        ToolSpec(
            schema=ToolSchema(name="recolor_chart", description="Recolor a synthetic chart", input_schema={"type": "object"}), handler=recolor_chart
        )
    )
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("recolor_chart", "clarification_ask")))
    calls = []
    refusals = []

    def respond(request):
        body = json.loads(request.content)
        if not body.get("stream"):
            return response(body, {"role": "assistant", "content": "Chart color"}, "stop")
        calls.append(body)
        if len(calls) == 1:
            return response(body, call("edit-1", "recolor_chart", {"color": "green"}), "tool_calls")
        if len(calls) == 2:
            return response(body, {"role": "assistant", "content": QUESTION}, "stop")
        if len(calls) == 3:
            after_question = body["messages"][[m.get("content") for m in body["messages"]].index(QUESTION) + 1 :]
            # Providers may merge adjacent user-role turns (collaboration cards); the note keeps its frame.
            guidance = [m["content"] for m in after_question if m["role"] == "user" and "review_context (JSON data): " in m["content"]]
            assert len(guidance) == 1, after_question
            note = guidance[0][guidance[0].index("Fix requirement scope") - len(RUNTIME_CORRECTION_PREFIX) :]
            assert note.startswith(RUNTIME_CORRECTION_PREFIX), guidance[0]
            assert "do not dispatch, retry or start other work" in note
            # The unapproved alternative the model chose in 116.
            return response(body, call("edit-2", "recolor_chart", {"color": "green", "as_image": True}), "tool_calls")
        if len(calls) == 4:
            refusals.extend(m["content"] for m in body["messages"] if m["role"] == "tool" and m.get("tool_call_id") == "edit-2")
            guidance = next(m["content"] for m in body["messages"] if m["role"] == "user" and RUNTIME_CORRECTION_PREFIX in m["content"])
            failed = json.loads(guidance.split("review_context (JSON data): ", 1)[1])["failed_calls"][0]["call_id"]
            review = {
                "requirements": [{"requirement": "Recolor the chart green", "status": "unfulfilled", "evidence_call_ids": []}],
                "issues": [{"call_id": failed, "resolution": "unfulfilled", "evidence_call_ids": [], "explanation": "The edit did not complete."}],
            }
            return response(body, call("review", "review_delivery", {"payload": {"delivery_review": review}}), "tool_calls")
        return response(body, {"role": "assistant", "content": QUESTION}, "stop")

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={"workspace_id": case.fixture.scope.workspace_id, "message": "Make the bar chart green."},
            headers={"Content-Type": "application/json"},
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
    turn = await case.fixture.case.store.get_turn(turn_id)
    assert turn.status == "completed", turn.error
    assert len(calls) == 5
    assert edits == [{"color": "green"}], "The correction must not run work the user has not approved"
    assert refusals and refusals[0].startswith("Not executed."), refusals
    assert projected_notes and all(note.role == "user" for note in projected_notes), "The note keeps its private runtime identity"
    assert turn.final_answer.startswith(QUESTION), "The question stays the user's; partial delivery is disclosed after it"
    events = [row for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
    assert [json.loads(row["payload_json"])["tool_name"] for row in events if row["event_type"] == "step_started"].count("recolor_chart") == 1
    done = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "done"]
    assert len(done) == 1 and done[0]["status"] == "completed"
    assert done[0]["narrative_superseded"] is True, "The bounced streamed question is replaced by the done answer"
    assert [event.payload for event in live_events if event.event_type == "done"] == done
    with anyio.fail_after(5):
        replay = await case.client.get(accepted.json()["stream_url"])
    assert replay.status_code == 200
    replayed = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
    assert [value.get("narrative_superseded") for value in replayed if value.get("status") == "completed" and "turn_id" in value] == [True]
