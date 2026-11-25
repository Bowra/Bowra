"""A Turn the user stops while a helper runs keeps its transcript and its usage, through the real HTTP path.

The Stop request commits the cancelled status and cancels the worker. On 116 (1.7.61,
synthetic-interrupted-task) the worker's cancellation then left the Turn runner inside an exception
group raised by a Tool-connection task group, so the runner's cancelled finalizer never
ran: nothing was published, and the Turn recorded no tokens and no credit charge although
twelve model calls had completed. ``wrapped`` reproduces that teardown.

A Turn stopped while the Lead writes its answer also keeps what it had shown of that answer
(a reproduced task: the next Turn started the article again).
"""

import asyncio
import contextlib
import json
from dataclasses import replace

import anyio
import httpx2
import pytest
import respx
from bowhead_core.context_assembly import incomplete_turn_marker
from bowhead_core.subagent import PROFILES
from bowhead_gateway.routes.turns import explicit_stop_request_id
from test_ordinary_lead_question_http import call, response, review_round
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import events, session_store, turn_runner
from bowhead_router import AgentBinding

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.mark.parametrize("wrapped", [False, True], ids=["plain", "exception-group"])
async def test_a_turn_stopped_while_a_helper_runs_keeps_its_transcript_and_usage(http_start, monkeypatch, wrapped):
    case, scope = http_start, http_start.fixture.scope
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn",)))
    orch = case.app.state.orchestrator
    orch.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    if wrapped:
        # A task group around the run turns the cancellation it sees into an exception group.
        original = turn_runner.TurnRunner._run_inner

        async def in_task_group(self, request, cancel_event):
            async with anyio.create_task_group():
                return await original(self, request, cancel_event)

        monkeypatch.setattr(turn_runner.TurnRunner, "_run_inner", in_task_group)
    helper = case.fixture.case.args["reference_start"].profile_name
    task = "Inspect the synthetic profit of every region and report each one. " * 8
    helper_running = asyncio.Event()
    lead_calls, stopped_helper_calls = [], []

    async def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" not in names:
            if "subagent_return" in names and len(lead_calls) == 2:
                # The helper of the Turn that will be stopped finishes one model call, then keeps running.
                stopped_helper_calls.append(body)
                if len(stopped_helper_calls) == 1:
                    return response(body, call("probe", "no_such_tool", {}), "tool_calls")
                helper_running.set()
                await asyncio.sleep(3600)
            return response(body, {"role": "assistant", "content": "The synthetic profit is 42."}, "stop")
        if review_round(body):
            return response(body, {"role": "assistant", "content": body["messages"][-2]["content"]}, "stop")
        lead_calls.append(body)
        if len(lead_calls) == 2:
            arguments = {"profile": helper, "task": task, "delegation_role": "helper", "required_source_urls": []}
            return response(body, call("helper-stopped", "subagent_spawn", arguments), "tool_calls")
        return response(body, {"role": "assistant", "content": "Noted."}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        first = await case.client.post("/api/v1/chat/send", json={"workspace_id": scope.workspace_id, "message": "Remember this session."})
        assert first.status_code == 200, first.text
        await settled(first.json()["turn_id"])
        session_id = first.json()["session_id"]
        second = await case.client.post(
            "/api/v1/chat/send", json={"session_id": session_id, "workspace_id": scope.workspace_id, "message": "Inspect every region."}
        )
        assert second.status_code == 200, second.text
        turn_id = second.json()["turn_id"]
        await asyncio.wait_for(helper_running.wait(), 20)
        turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
        stop = await case.client.post(
            f"/api/v1/turns/{turn_id}/cancel",
            json={
                "cancel_request_id": str(
                    explicit_stop_request_id(
                        team_id=scope.team_id, user_id=scope.user_id, turn_id=turn_id, execution_generation=turn["execution_generation"]
                    )
                ),
                "source": "explicit_stop",
                "expected_execution_generation": turn["execution_generation"],
            },
        )
        assert stop.status_code == 202, stop.text
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await settled(turn_id)
        stopped = next(row for row in rows(case, "turns") if row["id"] == turn_id)
        assert stopped["status"] == "cancelled"
        # The Turn is charged for the calls that completed before the Stop: the Lead's and the helper's.
        completed = [
            row
            for row in rows(case, "llm_usage_events")
            if row["turn_id"] == turn_id and row["reason"] == "turn" and row["receipt_state"] == "completed"
        ]
        used = (sum(row["tokens_input"] for row in completed), sum(row["tokens_output"] for row in completed))
        assert len(completed) == 2 and used == (34, 18)
        assert (stopped["tokens_input"], stopped["tokens_output"]) == used and stopped["cost_usd"] > 0
        charges = [row for row in rows(case, "credit_ledger") if row["turn_id"] == turn_id]
        assert [(row["reason"], row["tokens_input"], row["tokens_output"]) for row in charges] == [("turn", *used)]
        assert charges[0]["delta"] < 0
        # The stopped Turn's transcript is the published conversation head.
        head = next(row for row in rows(case, "model_context_conversation_heads") if row["session_id"] == session_id)
        assert head["published_turn_id"] == turn_id
        third = await case.client.post(
            "/api/v1/chat/send", json={"session_id": session_id, "workspace_id": scope.workspace_id, "message": "What had you done?"}
        )
        assert third.status_code == 200, third.text
        await settled(third.json()["turn_id"])
        assert next(row for row in rows(case, "turns") if row["id"] == third.json()["turn_id"])["status"] == "completed"
    # The next Turn reads the Lead's own call with its full task, the helper's state and the marker.
    messages = lead_calls[-1]["messages"]
    dispatch = next(message for message in messages if any(item["id"] == "helper-stopped" for item in message.get("tool_calls") or []))
    assert json.loads(dispatch["tool_calls"][0]["function"]["arguments"])["task"] == task
    result = next(message for message in messages if message.get("role") == "tool" and message.get("tool_call_id") == "helper-stopped")
    assert result["content"].startswith("cancelled｜子工具 ") and "没有回执" in result["content"]
    assert any(message.get("role") == "assistant" and message.get("content") == incomplete_turn_marker("cancelled") for message in messages)
    head = next(row for row in rows(case, "model_context_conversation_heads") if row["session_id"] == session_id)
    assert head["published_turn_id"] == third.json()["turn_id"]


async def stop(case, scope, turn_id):
    turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
    request_id = explicit_stop_request_id(
        team_id=scope.team_id, user_id=scope.user_id, turn_id=turn_id, execution_generation=turn["execution_generation"]
    )
    stopped = await case.client.post(
        f"/api/v1/turns/{turn_id}/cancel",
        json={"cancel_request_id": str(request_id), "source": "explicit_stop", "expected_execution_generation": turn["execution_generation"]},
    )
    assert stopped.status_code == 202, stopped.text
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await settled(turn_id)
    assert next(row for row in rows(case, "turns") if row["id"] == turn_id)["status"] == "cancelled"


@pytest.mark.parametrize("published", [True, False], ids=["published", "rebuilt-from-events"])
async def test_a_turn_stopped_while_its_answer_streams_keeps_what_it_had_written(http_start, monkeypatch, published):
    case, scope = http_start, http_start.fixture.scope
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn",)))
    orch = case.app.state.orchestrator
    orch.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    if not published:

        async def unavailable(self, **kwargs):
            return False

        monkeypatch.setattr(session_store.SessionStore, "publish_stopped_turn_context", unavailable)
    # A line break and a space arrive as chunks of their own, as DeepSeek streams them.
    written = ["Region A earned 7.", "\n\n", "Region B earned", " ", "9, and"]
    # The session's earlier answer holds the same text: asked to continue, a model often rewrites what it had shown.
    earlier = "".join(written) + " Region C earned 4."
    answer_streaming = asyncio.Event()
    lead_calls = []

    async def answer_then_hang():
        markers = "<headline>Regional profit</headline>\n\n<response_kind>chat</response_kind>\n\n"
        for delta in (markers, *written):
            yield ("data: " + json.dumps({"choices": [{"delta": {"role": "assistant", "content": delta}, "finish_reason": None}]}) + "\n\n").encode()
        answer_streaming.set()
        await asyncio.sleep(3600)

    async def respond(request):
        body = json.loads(request.content)
        if "subagent_spawn" not in {item["function"]["name"] for item in body.get("tools", [])}:
            return response(body, {"role": "assistant", "content": "Regions"}, "stop")
        if review_round(body):
            return response(body, {"role": "assistant", "content": body["messages"][-2]["content"]}, "stop")
        lead_calls.append(body)
        asked = json.dumps(body["messages"])
        if "Write that report again." in asked and "Continue from there." not in asked:
            assert body.get("stream")
            return httpx2.Response(200, content=answer_then_hang(), headers={"content-type": "text/event-stream"})
        return response(body, {"role": "assistant", "content": earlier}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        first = await case.client.post("/api/v1/chat/send", json={"workspace_id": scope.workspace_id, "message": "Report every region."})
        assert first.status_code == 200, first.text
        await settled(first.json()["turn_id"])
        session_id = first.json()["session_id"]
        second = await case.client.post(
            "/api/v1/chat/send", json={"session_id": session_id, "workspace_id": scope.workspace_id, "message": "Write that report again."}
        )
        assert second.status_code == 200, second.text
        turn_id = second.json()["turn_id"]
        await asyncio.wait_for(answer_streaming.wait(), 20)
        await stop(case, scope, turn_id)
        head = next(row for row in rows(case, "model_context_conversation_heads") if row["session_id"] == session_id)
        assert (head["published_turn_id"] == turn_id) is published
        third = await case.client.post(
            "/api/v1/chat/send", json={"session_id": session_id, "workspace_id": scope.workspace_id, "message": "Continue from there."}
        )
        assert third.status_code == 200, third.text
        await settled(third.json()["turn_id"])
        assert next(row for row in rows(case, "turns") if row["id"] == third.json()["turn_id"])["status"] == "completed"
    # The next Turn reads what the user had seen of the stopped answer, then the marker, without the protocol markers.
    shown = "".join(
        json.loads(row["payload_json"])["delta"]
        for row in rows(case, "turn_events")
        if row["turn_id"] == turn_id and row["event_type"] == "narrative.delta"
    )
    assert shown == "".join(written)
    closing = [message["content"] for message in lead_calls[-1]["messages"] if message.get("role") == "assistant"]
    assert f"{shown}\n\n{incomplete_turn_marker('cancelled')}" in closing
    assert incomplete_turn_marker("cancelled") not in closing
