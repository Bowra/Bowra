"""Mid-run steering through real `/chat/send`: the running Lead takes a queued instruction.

Real Lead loop, stores and event log; only the provider is scripted. With the switch
off the same instruction runs as a follow-up Turn, as before.
"""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import respx
from bowhead_core.agent_collaboration import CollaborationStore
from bowhead_core.agent_collaboration_runtime import USER_REVISION_UNREAD_NOTE
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.subagent import PROFILES
from bowhead_core.tool_registry import ToolExecutionContext, ToolSpec
from bowhead_router.types import ToolSchema
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import agent_collaboration_runtime
from bowhead_router import AgentBinding

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]

GREEN = "Use green bars instead."
FORWARD = "The user added an instruction: draw the bars in green."
REVISION_FRAME = "revised your assignment on the user's behalf (server data):"
TITLE = "Add the title Primes."
JSON_HEADERS = {"Content-Type": "application/json"}


def user_text_after_last_tool_result(body: dict) -> str:
    """What the user side says after the last Tool result. The provider adapter joins adjacent user messages."""
    messages = body["messages"]
    last_tool = max(index for index, message in enumerate(messages) if message["role"] == "tool")
    return "\n\n".join(
        message["content"] for message in messages[last_tool + 1 :] if message["role"] == "user" and isinstance(message.get("content"), str)
    )


async def send(case: SimpleNamespace, message: str, session_id: str | None = None):
    body = {"workspace_id": case.fixture.scope.workspace_id, "message": message}
    if session_id is not None:
        body["session_id"] = session_id
    return await case.client.post("/api/v1/chat/send", json=body, headers=JSON_HEADERS)


async def settle_session(case: SimpleNamespace, turn_id: str) -> tuple[str, list[dict]]:
    await settled(turn_id)
    session_id = next(row["session_id"] for row in rows(case, "turns") if row["id"] == turn_id)
    for row in rows(case, "turns"):
        if row["session_id"] == session_id and row["id"] != turn_id:
            await settled(row["id"])
    return session_id, [row for row in rows(case, "turns") if row["session_id"] == session_id]


def events_of(case: SimpleNamespace, turn_id: str) -> list[tuple[str, dict]]:
    return [(row["event_type"], json.loads(row["payload_json"])) for row in rows(case, "turn_events") if row["turn_id"] == turn_id]


def register_chart_tool(case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, instructions: list[str]) -> list:
    """A Lead Tool during which the user adds ``instructions``, one request each."""
    replies = []

    async def draw_chart(args: dict, ctx: ToolExecutionContext) -> dict:
        for instruction in instructions:
            replies.append(await send(case, instruction, ctx.session_id))
        return {"ok": True, "chart": "art_chart_1", "bar_color": "blue"}

    schema = ToolSchema(name="draw_chart", description="Draw a synthetic chart", input_schema={"type": "object"})
    case.app.state.orchestrator.tool_registry.register(ToolSpec(schema=schema, handler=draw_chart))
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("draw_chart",)))
    return replies


async def test_instruction_sent_during_a_tool_reaches_the_same_turn(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    case = http_start
    enable_lead_writer(case, monkeypatch)
    replies = register_chart_tool(case, monkeypatch, [GREEN])
    calls = []

    def respond(request):
        body = json.loads(request.content)
        if not body.get("stream"):
            return response(body, {"role": "assistant", "content": "Chart"}, "stop")
        calls.append(body)
        if len(calls) == 1:
            return response(body, call("draw-1", "draw_chart", {}), "tool_calls")
        return response(body, {"role": "assistant", "content": f"Answer {len(calls)}."}, "stop")

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await send(case, "Draw the primes bar chart.")
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        session_id, session_turns = await settle_session(case, turn_id)
        calls_before_later_question = len(calls)
        later = await send(case, "Which color did I ask for?", session_id)
        assert later.status_code == 200, later.text
        await settled(later.json()["turn_id"])

    assert [reply.status_code for reply in replies] == [200] and replies[0].json()["status"] == "buffered", replies[0].text
    turn = await case.fixture.case.store.get_turn(turn_id)
    assert turn.status == "completed", turn.error
    types = [kind for kind, _ in events_of(case, turn_id)]
    session = next(row for row in rows(case, "sessions") if row["id"] == session_id)
    assert user_text_after_last_tool_result(calls[1]).startswith(GREEN), (
        "the next model call carries the user's words, unframed, after the Tool result"
    )
    assert calls_before_later_question == 2 and len(session_turns) == 1, "one Turn and one answer; no follow-up Turn for the instruction"
    assert "turn.follow_up" not in types
    applied = [payload for kind, payload in events_of(case, turn_id) if kind == "steer.applied"]
    assert len(applied) == 1 and [item["text"] for item in applied[0]["items"]] == [GREEN]
    assert applied[0]["iteration"] == 1 and applied[0]["while_waiting"] is False
    assert types.index("steer.applied") < types.index("final_answer"), "replay shows the instruction before the answer it changed"
    assert session["steer_buffer_json"] == "[]" and session["active_turn_id"] is None
    assert turn.user_text == "Draw the primes bar chart.", "the stored request stays the original text"
    assert GREEN in json.dumps(calls[-1]["messages"]), "a later Turn's history still contains the instruction"


async def test_several_instructions_are_taken_together_in_order(http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    case = http_start
    enable_lead_writer(case, monkeypatch)
    replies = register_chart_tool(case, monkeypatch, [GREEN, TITLE])
    calls = []

    def respond(request):
        body = json.loads(request.content)
        if not body.get("stream"):
            return response(body, {"role": "assistant", "content": "Chart"}, "stop")
        calls.append(body)
        if len(calls) == 1:
            return response(body, call("draw-1", "draw_chart", {}), "tool_calls")
        return response(body, {"role": "assistant", "content": "Done."}, "stop")

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await send(case, "Draw the primes bar chart.")
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        _, session_turns = await settle_session(case, turn_id)

    assert [reply.json()["status"] for reply in replies] == ["buffered", "buffered"]
    assert user_text_after_last_tool_result(calls[1]).startswith(f"{GREEN}\n\n{TITLE}"), "the instructions arrive in the order sent"
    applied = [payload for kind, payload in events_of(case, turn_id) if kind == "steer.applied"]
    assert len(applied) == 1 and [item["text"] for item in applied[0]["items"]] == [GREEN, TITLE]
    assert len(session_turns) == 1 and len(calls) == 2


async def test_a_helper_never_takes_the_instruction_and_the_lead_takes_it_after_the_helper(
    http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = http_start
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn",)))
    case.app.state.orchestrator.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    profile = case.fixture.case.args["reference_start"].profile_name
    lead_calls, helper_calls = [], []
    lead_deciding, instruction_queued = asyncio.Event(), asyncio.Event()

    async def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            if len(lead_calls) == 1:
                # The instruction is queued before the helper starts, so the helper's first
                # step meets it in the buffer.
                lead_deciding.set()
                await instruction_queued.wait()
                arguments = {"profile": profile, "task": "Draw the primes bar chart.", "delegation_role": "helper", "required_source_urls": []}
                return response(body, call("helper-1", "subagent_spawn", arguments), "tool_calls")
            return response(body, {"role": "assistant", "content": "Here is the green chart."}, "stop")
        if "subagent_return" in names:
            helper_calls.append(body)
            payload = {"delivery_surface": "chat", "exec_summary": "The chart is drawn with blue bars."}
            return response(body, call("helper-return", "subagent_return", {"payload": payload}), "tool_calls")
        return response(body, {"role": "assistant", "content": "Chart"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await send(case, "Draw the primes bar chart.")
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await asyncio.wait_for(lead_deciding.wait(), timeout=30)
        queued = await send(case, GREEN, next(row["session_id"] for row in rows(case, "turns") if row["id"] == turn_id))
        instruction_queued.set()
        _, session_turns = await settle_session(case, turn_id)

    assert queued.status_code == 200 and queued.json()["status"] == "buffered", queued.text
    turn = await case.fixture.case.store.get_turn(turn_id)
    assert turn.status == "completed", turn.error
    assert helper_calls, "the helper ran"
    assert all(GREEN not in json.dumps(body["messages"]) for body in helper_calls), "a helper never receives the user's instruction directly"
    assert user_text_after_last_tool_result(lead_calls[1]).startswith(GREEN), "the Lead takes it at its next step, after the helper's result"
    assert len(session_turns) == 1


async def test_an_instruction_sent_while_a_helper_runs_reaches_it_through_the_lead(
    http_start: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = http_start
    monkeypatch.setenv("BOWHEAD_PI_K_ENABLED", "0")
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setitem(PROFILES, "compute", replace(PROFILES["compute"], tool_whitelist=("subagent_return",)))
    artifacts = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "mid-turn-steer-uploads"))
    await artifacts.initialize()
    case.app.state.artifact_store = case.app.state.orchestrator.artifact_store = artifacts
    # Only the gateway's wake-up can start the round within this test.
    monkeypatch.setattr(agent_collaboration_runtime, "USER_INPUT_POLL_SECONDS", 600)
    helper_working, forwarded = asyncio.Event(), asyncio.Event()
    original_update = CollaborationStore.update

    async def observe_update(store, *args, **kwargs):
        receipt = await original_update(store, *args, **kwargs)
        forwarded.set()
        return receipt

    monkeypatch.setattr(CollaborationStore, "update", observe_update)
    lead_calls, rounds, helper_calls = [], [], []

    def returns(body: dict, call_id: str, summary: str):
        message = call(call_id, "subagent_return", {"payload": {"delivery_surface": "chat", "exec_summary": summary}})
        return response(body, message, "tool_calls")

    async def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            if len(lead_calls) == 1:
                arguments = {"required_source_urls": [], "profile": "compute", "task": "Draw the primes bar chart.", "delegation_role": "helper"}
                return response(body, call("chart", "subagent_spawn", arguments), "tool_calls")
            return response(body, {"role": "assistant", "content": "Here is the chart with green bars."}, "stop")
        if names == {"agent_message"}:
            # The waiting Lead's bounded round: it decides to pass the instruction on.
            rounds.append(body)
            state = next(m["content"] for m in reversed(body["messages"]) if "Participants working now:" in str(m.get("content")))
            working = json.JSONDecoder().raw_decode(state.split("Participants working now:\n", 1)[1])[0]["participants"]
            return response(
                body, call("forward", "agent_message", {"action": "update", "to": working[0]["execution_id"], "body": FORWARD}), "tool_calls"
            )
        if "subagent_return" in names:
            helper_calls.append(body)
            if len(helper_calls) == 1:
                # The helper is writing its result when the user's instruction arrives and the Lead forwards it.
                helper_working.set()
                await asyncio.wait_for(forwarded.wait(), timeout=30)
                return returns(body, "return-1", "The chart is drawn with blue bars.")
            green = REVISION_FRAME in json.dumps(body["messages"])
            return returns(
                body, f"return-{len(helper_calls)}", "The chart is drawn with green bars." if green else "The chart is drawn with blue bars."
            )
        return response(body, {"role": "assistant", "content": "Chart"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await send(case, "Draw the primes bar chart.")
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await asyncio.wait_for(helper_working.wait(), timeout=30)
        queued = await send(case, GREEN, next(row["session_id"] for row in rows(case, "turns") if row["id"] == turn_id))
        _, session_turns = await settle_session(case, turn_id)

    assert queued.status_code == 200 and queued.json()["status"] == "buffered", queued.text
    turn = await case.fixture.case.store.get_turn(turn_id)
    assert turn.status == "completed", turn.error
    assert len(rounds) == 1 and len(session_turns) == 1, "one bounded round, one Turn"
    round_text = "\n".join(m["content"] for m in rounds[0]["messages"] if m["role"] == "user" and isinstance(m.get("content"), str))
    assert GREEN in round_text and "your delegation call is still running" in round_text
    applied = [payload for kind, payload in events_of(case, turn_id) if kind == "steer.applied"]
    assert len(applied) == 1 and applied[0]["while_waiting"] is True and [item["text"] for item in applied[0]["items"]] == [GREEN]
    assert "turn.follow_up" not in [kind for kind, _ in events_of(case, turn_id)]
    forwards = [
        json.loads(row["receipt_json"])
        for row in rows(case, "agent_collaboration_operation")
        if row["turn_id"] == turn_id and row["action"] == "update"
    ]
    assert len(forwards) == 1 and forwards[0]["user_revision"] is True
    # The helper's return was refused once; its next step read the Lead's forward, never the user's text.
    assert len(helper_calls) >= 2
    refused = json.loads(next(m["content"] for m in reversed(helper_calls[1]["messages"]) if m["role"] == "tool"))
    assert (refused["error"], refused["guidance"]) == ("collaboration_unresolved", USER_REVISION_UNREAD_NOTE)
    assert REVISION_FRAME in json.dumps(helper_calls[1]["messages"]) and FORWARD in json.dumps(helper_calls[1]["messages"], ensure_ascii=False)
    assert all(GREEN not in json.dumps(body["messages"]) for body in helper_calls), "a helper never receives the user's instruction directly"
    returned = json.loads(next(m["content"] for m in lead_calls[1]["messages"] if m.get("tool_call_id") == "chart"))
    assert "green bars" in json.dumps(returned), "the helper's result reflects the instruction"
    assert user_text_after_last_tool_result(lead_calls[1]).startswith(GREEN), "the Lead keeps the instruction after the helper's result"
