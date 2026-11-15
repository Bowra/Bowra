"""An unresolved revision scope refuses that dispatch, not the rest of the Turn (real spawn handler).

Until 2026-11-10 a scope miss locked the Turn: every later dispatch was refused, including a
corrected name or a delivered file (the original regression). Now Lead may correct the call in a
later step; the admitted dispatch states what it did instead (`scope_after_miss`), and the
failed-call circuit breaker bounds repeated misses.
See docs/sprints/revision-scope-retry-2026-11-10/tech-design.md.
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.subagent import PROFILES
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

QUESTION = {"question": "Which part should I change: Monthly trend or Regions?", "type": "single_select", "options": ["Monthly trend", "Regions"]}


async def _send(case, text: str, respond, *, session_id: str | None = None) -> SimpleNamespace:
    payload = {"workspace_id": case.fixture.scope.workspace_id, "message": text, **({"session_id": session_id} if session_id else {})}
    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=payload, headers={"Content-Type": "application/json"})
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
    events = [row for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
    return SimpleNamespace(turn_id=turn_id, turn=await case.fixture.case.store.get_turn(turn_id), events=events)


def _tool_names(body: dict) -> set[str]:
    return {item["function"]["name"] for item in body.get("tools", [])}


def _last_tool_result(body: dict) -> str:
    return next((str(message.get("content") or "") for message in reversed(body["messages"]) if message.get("role") == "tool"), "")


def _revise(call_id: str, scope: list[str]) -> dict:
    arguments = {
        "profile": "compute",
        "task": "Fix the charts the user named.",
        "required_source_urls": [],
        "canvas_mode": "revise",
        "revise_scope": scope,
    }
    return call(call_id, "subagent_spawn", arguments)


async def _session_with_report(case, monkeypatch: pytest.MonkeyPatch) -> str:
    """Turn 1 leaves a two-part Canvas report in the session."""
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    enable_lead_writer(case, monkeypatch)
    register_clarification_tool(case.app.state.orchestrator.tool_registry)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask")))
    store = CanvasStore(str(case.path))
    await store.initialize()
    monkeypatch.setattr(case.app.state.orchestrator, "canvas_store", store)
    case.app.state.canvas_store = store

    def first_turn(request):
        body = json.loads(request.content)
        return response(body, {"role": "assistant", "content": "The RBU report is on the canvas."}, "stop")

    first = await _send(case, "Build the RBU report.", first_turn)
    assert first.turn.status == "completed", first.turn.error
    session_id = next(row["session_id"] for row in rows(case, "turns") if row["id"] == first.turn_id)
    canvas = await store.ensure_canvas(session_id=session_id, workspace_id=case.fixture.scope.workspace_id, user_id=case.fixture.scope.user_id)
    await store.append_section(canvas_id=canvas.id, kind="h1", payload={"text": "RBU report"}, by_turn_id=first.turn_id)
    for title in ("Monthly trend", "Regions"):
        await store.append_section(
            canvas_id=canvas.id, kind="narrative", payload={"markdown": f"## {title}\n\nRevenue fell."}, by_turn_id=first.turn_id, owner="sub_w1"
        )
    return session_id


async def test_corrected_scope_runs_in_the_same_turn(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    case = http_start
    session_id = await _session_with_report(case, monkeypatch)
    lead_calls: list[dict] = []
    helper_calls: list[dict] = []

    def respond(request):
        body = json.loads(request.content)
        if not _tool_names(body):
            return response(body, {"role": "assistant", "content": "Chart fix"}, "stop")
        if "subagent_spawn" not in _tool_names(body):
            helper_calls.append(body)
            return response(body, {"role": "assistant", "content": "Revised the Regions charts."}, "stop")
        lead_calls.append(body)
        if len(lead_calls) == 1:
            return response(body, _revise("scope-miss", ["later charts"]), "tool_calls")
        if len(lead_calls) == 2:
            return response(body, _revise("scope-fixed", ["Regions"]), "tool_calls")
        return response(body, {"role": "assistant", "content": "I couldn't find later charts; I revised Regions."}, "stop")

    sent = await _send(case, "The Regions charts did not render; fix them.", respond, session_id=session_id)
    assert sent.turn.status == "completed", sent.turn.error
    assert len(helper_calls) == 1, "The miss ran no helper; the corrected retry ran exactly one"
    refusal = _last_tool_result(lead_calls[1])
    assert "revise_scope_unresolved" in refusal and "Monthly trend; Regions" in refusal
    facts = _last_tool_result(lead_calls[2])
    assert '"scope_after_miss"' in facts and '"not_found": ["later charts"]' in facts and '"scope_parts": ["Regions"]' in facts


async def test_question_after_an_admitted_retry_does_not_fail_the_turn(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review P1-4: with the lock gone, a question can follow a helper that ran after a miss."""
    case = http_start
    session_id = await _session_with_report(case, monkeypatch)
    lead_calls: list[dict] = []

    def respond(request):
        body = json.loads(request.content)
        if not _tool_names(body):
            return response(body, {"role": "assistant", "content": "Chart fix"}, "stop")
        if "subagent_spawn" not in _tool_names(body):
            return response(body, {"role": "assistant", "content": "Revised the Regions charts."}, "stop")
        lead_calls.append(body)
        if len(lead_calls) == 1:
            return response(body, _revise("scope-miss", ["later charts"]), "tool_calls")
        if len(lead_calls) == 2:
            return response(body, _revise("scope-fixed", ["Regions"]), "tool_calls")
        if len(lead_calls) == 3:
            return response(body, call("ask-1", "clarification_ask", {"questions": [QUESTION]}), "tool_calls")
        return response(body, {"role": "assistant", "content": "I revised Regions. Should Monthly trend change too?"}, "stop")

    sent = await _send(case, "The later charts did not render; fix them.", respond, session_id=session_id)
    assert sent.turn.status != "failed", sent.turn.error
    cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == sent.turn_id]
    if cards:
        assert len(lead_calls) == 3 and QUESTION["question"] in json.dumps(cards[0], ensure_ascii=False)
    else:
        # Today the durable pause cannot retain a refused spawn next to an admitted helper (the checkpoint's
        # helper_materials count); the question is refused without failing the Turn (2f0e574f2) and Lead asks in prose.
        assert sent.turn.status == "completed", sent.turn.error
        assert "clarification_capture_unavailable" in _last_tool_result(lead_calls[3])


async def test_repeated_misses_trip_the_spawn_circuit_breaker(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review P1-1: three failed dispatches in a Turn, not REFUSED_DECISION_LIMIT, bound repeated misses."""
    case = http_start
    session_id = await _session_with_report(case, monkeypatch)
    tool = case.app.state.orchestrator.tool_registry.get("subagent_spawn")
    handled: list[dict] = []
    original = tool.handler

    async def counting(args, ctx):
        handled.append(args)
        return await original(args, ctx)

    monkeypatch.setattr(tool, "handler", counting)
    lead_calls: list[dict] = []

    def respond(request):
        body = json.loads(request.content)
        if not _tool_names(body):
            return response(body, {"role": "assistant", "content": "Chart fix"}, "stop")
        lead_calls.append(body)
        if len(lead_calls) <= 4:
            return response(body, _revise(f"scope-miss-{len(lead_calls)}", [f"part {len(lead_calls)}"]), "tool_calls")
        return response(body, {"role": "assistant", "content": "Which part should I change: Monthly trend or Regions?"}, "stop")

    sent = await _send(case, "Fix the charts.", respond, session_id=session_id)
    assert sent.turn.status == "completed", sent.turn.error
    assert len(handled) == 3, "The fourth dispatch is refused before the handler runs"
    assert "circuit_broken: tool 'subagent_spawn'" in _last_tool_result(lead_calls[4])


async def test_dispatch_rejects_unchanged_arguments_and_runs_the_correction(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    case = http_start
    session_id = await _session_with_report(case, monkeypatch)
    lead_calls: list[dict] = []
    helper_calls: list[dict] = []
    arguments = {"profile": "compute", "delivery": "chat", "required_source_urls": []}

    def respond(request):
        body = json.loads(request.content)
        if not _tool_names(body):
            return response(body, {"role": "assistant", "content": "Monthly sales lookup"}, "stop")
        if "subagent_spawn" not in _tool_names(body):
            helper_calls.append(body)
            return response(body, {"role": "assistant", "content": "Verified 42 monthly sales."}, "stop")
        lead_calls.append(body)
        count = len(lead_calls)
        if count <= 3:
            task = {"query": "sales"} if count <= 2 else "Verify monthly sales."
            return response(body, call(f"dispatch-{count}", "subagent_spawn", arguments | {"task": task}), "tool_calls")
        return response(body, {"role": "assistant", "content": "Verified 42 monthly sales."}, "stop")

    sent = await _send(case, "Find monthly sales.", respond, session_id=session_id)
    assert sent.turn.status == "completed", sent.turn.error
    assert len(helper_calls) == 1
    assert '"path": "task"' in _last_tool_result(lead_calls[1])
    assert "Correct the reported argument" in _last_tool_result(lead_calls[2])
    failed = [
        json.loads(row["payload_json"])
        for row in sent.events
        if row["event_type"] == "step_done"
        and json.loads(row["payload_json"]).get("tool_name") == "subagent_spawn"
        and json.loads(row["payload_json"]).get("ok") is False
    ]
    assert len(failed) == 1
    assert failed[0]["error"]["diagnostics"]["safe_detail"] == "field=task"
