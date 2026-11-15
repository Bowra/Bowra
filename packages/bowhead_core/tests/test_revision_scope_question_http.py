"""A dispatch refused for an unmatched revision scope lets Lead ask the user.

The refusal tells Lead to ask with clarification_ask. the original regression (1.7.18) had
that question refused (`clarification_capture_unavailable`) because the refused spawn
had no helper binding; Lead then spun on refused dispatches. A spawn refused before any
helper ran is now recorded like other pre-work refusals, so the question pauses the Turn.
Asking in prose instead is the expected ending, not undelivered work.
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.subagent import PROFILES
from bowhead_core.tool_registry import ToolExecutionContext
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

SCOPE_QUESTION = "I couldn't find 「later charts」 in the report. Which part should I change? The report has: Monthly trend; Regions."
PROSE_QUESTION = "I couldn't find the later charts in the report. Which part should I change: Monthly trend or Regions?"


def _install(case, monkeypatch) -> list[dict]:
    enable_lead_writer(case, monkeypatch)
    registry = case.app.state.orchestrator.tool_registry
    register_clarification_tool(registry)
    dispatched: list[dict] = []

    async def refuse_scope(args: dict, ctx: ToolExecutionContext) -> dict:
        # The native handler's refusal shape when a named part matches no report section.
        dispatched.append(args)
        return {
            "error": "The part the user named was not found in the report: later charts",
            "error_kind": "invalid_args",
            "reason_code": "revise_scope_unresolved",
            "path": "revise_scope",
            "hint": "Ask the user which part to change with clarification_ask, using the suggested question.",
            "suggested_question": {"question": SCOPE_QUESTION, "type": "free_text", "options": []},
        }

    monkeypatch.setattr(registry.get("subagent_spawn"), "handler", refuse_scope)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask")))
    return dispatched


def _spawn():
    return call(
        "revise-1",
        "subagent_spawn",
        {"profile": "compute", "task": "Fix the later charts.", "revise_scope": ["later charts"], "required_source_urls": []},
    )


async def _send(case, respond) -> SimpleNamespace:
    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={"workspace_id": case.fixture.scope.workspace_id, "message": "The later charts did not render; fix them."},
            headers={"Content-Type": "application/json"},
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
    events = [row for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
    return SimpleNamespace(turn_id=turn_id, turn=await case.fixture.case.store.get_turn(turn_id), events=events)


async def test_question_after_scope_refusal_creates_a_card(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    case = http_start
    dispatched = _install(case, monkeypatch)
    calls: list[dict] = []

    def respond(request):
        body = json.loads(request.content)
        if not body.get("stream"):
            return response(body, {"role": "assistant", "content": "Chart fix"}, "stop")
        calls.append(body)
        if len(calls) == 1:
            return response(body, _spawn(), "tool_calls")
        if len(calls) == 2:
            question = {"question": SCOPE_QUESTION, "type": "free_text", "options": []}
            return response(body, call("ask-1", "clarification_ask", {"questions": [question]}), "tool_calls")
        return response(body, {"role": "assistant", "content": PROSE_QUESTION}, "stop")

    sent = await _send(case, respond)
    assert len(dispatched) == 1
    assert len(calls) == 2, "The question pauses the Turn; Lead makes no further decision"
    assert "clarification_capture_unavailable" not in json.dumps([row["payload_json"] for row in sent.events])
    cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == sent.turn_id]
    assert len(cards) == 1, sent.turn.status
    assert SCOPE_QUESTION in json.dumps(cards[0], ensure_ascii=False)


async def test_prose_question_after_scope_refusal_ends_the_turn(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    case = http_start
    _install(case, monkeypatch)
    calls: list[dict] = []

    def respond(request):
        body = json.loads(request.content)
        if not body.get("stream"):
            return response(body, {"role": "assistant", "content": "Chart fix"}, "stop")
        calls.append(body)
        if len(calls) == 1:
            return response(body, _spawn(), "tool_calls")
        return response(body, {"role": "assistant", "content": PROSE_QUESTION}, "stop")

    sent = await _send(case, respond)
    assert len(calls) == 2, "No delivery-review correction follows a scope question"
    assert sent.turn.status == "completed", sent.turn.error
    assert sent.turn.final_answer.strip() == PROSE_QUESTION
