"""Delegated inspection must return to Lead before the whole request can finish."""

import json
from types import SimpleNamespace

import httpx2
import pytest
import respx
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]

INSPECTION = "The source has no subtitle."
COMPLETE = "The comparison is complete; the source has no subtitle."


@pytest.mark.parametrize("role", [None, "helper", "primary"], ids=["unspecified", "helper", "whole-request"])
async def test_ordinary_delegation_preserves_remaining_work(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, role: str | None) -> None:
    case = http_start
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setenv("BOWHEAD_LEAD_POST_PEER_BREAK", "1")
    profile = case.fixture.case.args["reference_start"].profile_name
    calls = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        index = len(calls)
        calls.append(body)
        message = {"role": "assistant", "content": COMPLETE}
        if index in (0, 2):
            task = "Inspect the source, then complete the comparison." if role == "primary" else "Inspect the source."
            args = {"profile": profile, "task": task if index == 0 else "Finish the comparison using the inspection."}
            if role is not None or index == 2:
                args["delegation_role"] = role if index == 0 else "primary"
            name = "subagent_spawn"
        elif index in (1, 3):
            args = {"payload": {"delivery_surface": "chat", "exec_summary": COMPLETE if index == 3 or role == "primary" else INSPECTION}}
            name = "subagent_return"
        else:
            name = None
        if name:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"index": 0, "id": f"call_{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}],
            }
        reason = "tool_calls" if name else "stop"
        usage = {"prompt_tokens": 11, "completion_tokens": 7}
        if body.get("stream"):
            frames = [{"choices": [{"delta": message, "finish_reason": None}]}, {"choices": [{"delta": {}, "finish_reason": reason}], "usage": usage}]
            return httpx2.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n")
        return httpx2.Response(200, json={"choices": [{"message": message, "finish_reason": reason}], "usage": usage})

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            headers={"Content-Type": "application/json"},
            json={"workspace_id": case.fixture.scope.workspace_id, "message": "Inspect the source, then complete the comparison."},
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)

    turn = await case.fixture.case.store.get_turn(turn_id)
    assert turn.status == "completed", turn
    assert turn.final_answer == COMPLETE
    # Includes the existing final session-title call, separate from Lead orchestration.
    assert len(calls) == (3 if role == "primary" else 6)
    if role != "primary":
        results = [json.loads(m["content"]) for m in calls[2]["messages"] if m["role"] == "tool" and m.get("name") == "subagent_spawn"]
        assert results and results[0]["final_answer"] == INSPECTION
        assert "_chat_direct_delivery" not in results[0]
        assert "remaining" in results[0]["directive"]
    events = [row for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
    answers = [json.loads(row["payload_json"])["text"] for row in events if row["event_type"] == "final_answer"]
    assert answers == [turn.final_answer]
    done = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "done"]
    assert len(done) == 1 and done[0]["status"] == turn.status
    summaries = [row for row in events if row["event_type"] == "lead.subagent_summary"]
    assert len(summaries) == (1 if role == "primary" else 2)
