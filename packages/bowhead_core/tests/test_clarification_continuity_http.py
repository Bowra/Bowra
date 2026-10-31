"""Real HTTP questions retain every answer and the next Turn's topic."""

import json
from dataclasses import replace

import anyio
import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.subagent import PROFILES
from bowhead_gateway.routes import clarifications, sessions
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import events

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]

QUESTIONS = [
    {"question": "Which city?", "options": [], "type": "free_text"},
    {"question": "Which sections?", "options": ["Forecast", "Hourly", "Travel", "Week"], "type": "multi_select"},
]
ANSWERS = [
    {"question": "Which city?", "source": "custom", "answer": "Singapore", "skipped": False},
    *[
        {"question": "Which sections?", "source": "candidate", "answer": option, "option_index": index, "skipped": False}
        for index, option in reversed(list(enumerate(QUESTIONS[1]["options"])))
    ],
]


@pytest.mark.parametrize("mode", ["resume", "pending_followup", "answered_followup", "invalid_then_corrected", "stray_options"])
async def test_question_card_continuity(http_start, monkeypatch, mode):
    case = http_start
    case.client.headers["Content-Type"] = "application/json"
    scope = case.fixture.scope
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setattr(events, "_default_registry", case.app.state.orchestrator.event_registry)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("clarification_ask",)))
    register_clarification_tool(case.app.state.orchestrator.tool_registry)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    case.app.include_router(sessions.router, prefix="/api/v1/sessions")
    calls = []
    live_events = []
    original_emit = events.TurnEventBus.emit

    async def observe_emit(bus, event):
        live_events.append(event)
        return await original_emit(bus, event)

    monkeypatch.setattr(events.TurnEventBus, "emit", observe_emit)

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        index = len(calls)
        if index == 1 and mode == "invalid_then_corrected":
            questions = [{**QUESTIONS[0], "question": ""}, QUESTIONS[1]]
        elif index == 1 and mode == "stray_options":
            questions = [{**QUESTIONS[0], "options": ["Beijing", "Shanghai"]}, QUESTIONS[1]]
        elif index == 1 or (index == 2 and mode == "invalid_then_corrected"):
            if index == 2:
                wire = json.dumps(body)
                assert "schema_invalid" in wire and "questions[0].question" in wire
                assert not rows(case, "clarification_requests")
            questions = QUESTIONS
        else:
            return response(body, {"role": "assistant", "content": "The weather request remains understood."}, "stop")
        return response(body, call(f"ask-{index}", "clarification_ask", {"questions": questions}), "tool_calls")

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        first = await case.client.post(
            "/api/v1/chat/send", json={"workspace_id": scope.workspace_id, "message": "Make a presentation about tomorrow's weather."}
        )
        assert first.status_code == 200, first.text
        turn_id = first.json()["turn_id"]
        await settled(turn_id)
        turn = await case.app.state.session_store.get_turn(turn_id)
        assert turn.status == "clarification_pending"
        card = next(row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id)
        context = json.loads(card["resume_context_json"])
        assert context.get("card_questions", context.get("questions")) == QUESTIONS
        listed = await case.client.get(f"/api/v1/clarifications/turn/{turn_id}")
        assert listed.status_code == 200 and listed.json()["items"][0]["questions"] == QUESTIONS
        question_event = next(
            json.loads(row["payload_json"])
            for row in rows(case, "turn_events")
            if row["turn_id"] == turn_id and row["event_type"] == "clarification_required"
        )
        assert question_event["questions"] == QUESTIONS
        assert next(event for event in live_events if event.event_type == "clarification_required").payload["questions"] == QUESTIONS
        with anyio.fail_after(5):
            replay = await case.client.get(first.json()["stream_url"])
        assert replay.status_code == 200
        replayed = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
        assert next(item for item in replayed if item.get("clarification_id") == card["request_id"] and "questions" in item)["questions"] == QUESTIONS
        if mode == "invalid_then_corrected":
            assert len(calls) == 2
        if mode != "pending_followup":
            if mode == "answered_followup":

                async def unavailable(*_args, **_kwargs):
                    raise ValueError("simulated_preworker_unavailable")

                with monkeypatch.context() as failure:
                    failure.setattr("bowhead_gateway.turn_service.spawn_resume_turn", unavailable)
                    resolved = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/resolve", json={"answers": ANSWERS})
                assert resolved.status_code == 409
            else:
                resolved = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/resolve", json={"answers": ANSWERS})
                assert resolved.status_code == 200, resolved.text
                await settled(turn_id)
                assert (await case.app.state.session_store.get_turn(turn_id)).status == "completed"
                wire = json.dumps(calls[-1])
                assert all(answer["answer"] in wire for answer in ANSWERS)
                count = len(calls)
                retry = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/resolve", json={"answers": ANSWERS})
                assert retry.status_code == 200 and len(calls) == count
                receipt = next(
                    json.loads(row["payload_json"])
                    for row in rows(case, "turn_events")
                    if row["turn_id"] == turn_id and row["event_type"] == "clarification.resolved"
                )
                assert receipt["answers"] == resolved.json()["answers"]
                return
        saved_card = next(row for row in rows(case, "clarification_requests") if row["request_id"] == card["request_id"])
        before = await case.client.get(f"/api/v1/sessions/{turn.session_id}")
        assert before.status_code == 200
        second = await case.client.post(
            "/api/v1/chat/send",
            json={"workspace_id": scope.workspace_id, "session_id": turn.session_id, "message": "Make it a report instead."},
        )
        assert second.status_code == 200, second.text
        await settled(second.json()["turn_id"])
        wire = json.dumps(calls[-1])
        assert "tomorrow's weather" in wire
        assert "Which city?" in wire and "Which sections?" in wire
        assert "clarification_pending" in wire
        assert "does not resume" in wire
        if mode == "answered_followup":
            assert all(answer["answer"] in wire for answer in ANSWERS)
            assert "resolved" in wire
        assert next(row for row in rows(case, "clarification_requests") if row["request_id"] == card["request_id"]) == saved_card
        assert (await case.app.state.session_store.get_turn(turn_id)).status == "clarification_pending"
