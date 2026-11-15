"""Lead cannot spin on Tool calls the runtime refuses before execution.

the original regression (1.7.18): after a revision-scope refusal and a delivery-review
bounce, Lead requested one refused call per model decision for 50-66 decisions until
the user cancelled. Pre-execution refusals never reached the bounded Tool-error policy
and runtime-control Lead runs have no step stop, so only the Turn deadline remained.
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import respx
from bowhead_core.subagent import PROFILES
from bowhead_core.tool_registry import ToolExecutionContext, ToolSpec
from bowhead_router.runtime_notes import RUNTIME_CORRECTION_PREFIX
from bowhead_router.types import ToolSchema
from test_ordinary_lead_question_http import call, response
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

ANSWER = "I could not run that operation. The chart is unchanged; tell me how you want to continue."
# The fake provider answers in prose after this many decisions, so an unfixed runtime
# shows the spin as a call count instead of hanging the test.
PROVIDER_CAP = 12


def _install(case, monkeypatch) -> list[dict]:
    enable_lead_writer(case, monkeypatch)
    registry = case.app.state.orchestrator.tool_registry
    executed: list[dict] = []

    async def recolor_chart(args: dict, ctx: ToolExecutionContext) -> dict:
        executed.append(args)
        return {"ok": True}

    registry.register(
        ToolSpec(
            schema=ToolSchema(name="recolor_chart", description="Recolor a synthetic chart", input_schema={"type": "object"}), handler=recolor_chart
        )
    )
    # Only recolor_chart is offered; delete_chart is outside Lead's whitelist, so every
    # request for it is refused before execution with no step, receipt or effect.
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("recolor_chart",)))
    return executed


async def _send(case, respond) -> SimpleNamespace:
    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={"workspace_id": case.fixture.scope.workspace_id, "message": "Delete the old chart."},
            headers={"Content-Type": "application/json"},
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
    return SimpleNamespace(turn_id=turn_id, turn=await case.fixture.case.store.get_turn(turn_id))


async def test_lead_answers_without_tools_after_repeated_refusals(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    case = http_start
    executed = _install(case, monkeypatch)
    calls: list[dict] = []

    def respond(request):
        body = json.loads(request.content)
        if not body.get("stream"):
            return response(body, {"role": "assistant", "content": "Chart"}, "stop")
        calls.append(body)
        if body.get("tool_choice") == "none" or len(calls) >= PROVIDER_CAP:
            return response(body, {"role": "assistant", "content": ANSWER}, "stop")
        return response(body, call(f"delete-{len(calls)}", "delete_chart", {"attempt": len(calls)}), "tool_calls")

    sent = await _send(case, respond)
    assert len(calls) == 4, f"Lead spun for {len(calls)} decisions"
    assert all(body.get("tool_choice") != "none" for body in calls[:3])
    final_request = calls[3]
    assert final_request.get("tool_choice") == "none"
    # Providers may merge adjacent user-role turns; the note keeps its runtime frame.
    notes = [m["content"] for m in final_request["messages"] if m["role"] == "user" and "Do not call any Tool" in (m["content"] or "")]
    assert len(notes) == 1 and RUNTIME_CORRECTION_PREFIX in notes[0], final_request["messages"][-3:]
    assert executed == []
    assert sent.turn.status == "completed", sent.turn.error
    assert sent.turn.final_answer.startswith(ANSWER)
    events = [row for row in rows(case, "turn_events") if row["turn_id"] == sent.turn_id]
    assert not [row for row in events if row["event_type"] == "step_started" and "delete_chart" in row["payload_json"]]


async def test_lead_stops_when_it_still_requests_tools_after_the_answer_note(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Some providers ignore tool_choice=none; the run then ends instead of spinning."""
    case = http_start
    executed = _install(case, monkeypatch)
    calls: list[dict] = []

    def respond(request):
        body = json.loads(request.content)
        if not body.get("stream"):
            return response(body, {"role": "assistant", "content": "Chart"}, "stop")
        calls.append(body)
        if len(calls) >= PROVIDER_CAP:
            return response(body, {"role": "assistant", "content": ANSWER}, "stop")
        return response(body, call(f"delete-{len(calls)}", "delete_chart", {"attempt": len(calls)}), "tool_calls")

    sent = await _send(case, respond)
    assert len(calls) == 4, f"Lead spun for {len(calls)} decisions"
    assert executed == []
    assert sent.turn.status == "failed"
    events = [row for row in rows(case, "turn_events") if row["turn_id"] == sent.turn_id]
    done = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "done"]
    assert len(done) == 1 and done[0]["error_kind"] == "tool_call_not_executed", done


async def test_one_executed_call_resets_the_refusal_count(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only consecutive fully refused decisions count; a real call in between is progress."""
    case = http_start
    executed = _install(case, monkeypatch)
    calls: list[dict] = []
    script = ["delete", "delete", "recolor", "delete", "delete"]

    def respond(request):
        body = json.loads(request.content)
        if not body.get("stream"):
            return response(body, {"role": "assistant", "content": "Chart"}, "stop")
        calls.append(body)
        step = len(calls)
        if step > len(script):
            return response(body, {"role": "assistant", "content": ANSWER}, "stop")
        if script[step - 1] == "recolor":
            return response(body, call(f"recolor-{step}", "recolor_chart", {"color": "green"}), "tool_calls")
        return response(body, call(f"delete-{step}", "delete_chart", {"attempt": step}), "tool_calls")

    sent = await _send(case, respond)
    assert len(calls) == len(script) + 1
    assert all(body.get("tool_choice") != "none" for body in calls)
    assert executed == [{"color": "green"}]
    assert sent.turn.status == "completed", sent.turn.error
