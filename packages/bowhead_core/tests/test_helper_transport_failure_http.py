"""A helper whose model call fails in transport returns its failure to Lead.

116 dogfood `t_041e52fa751b`: a helper's model call failed in transport on a
run resumed after a clarification answer. The physical receipt became unknown,
which only a fresh run converts to an uncertain helper result, so the whole Turn
failed as ``lead_internal_error``. The failure is now settled as not applied and
retried; once retries are exhausted the typed failure returns to Lead as an
ordinary peer failure in fresh and resumed runs alike.
"""

import json
from dataclasses import replace

import httpx2
import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.subagent import PROFILES
from bowhead_gateway.routes import clarifications
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import events
from bowhead_router import AgentBinding

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.mark.parametrize("resumed", [False, True])
async def test_helper_transport_failure_returns_to_lead(http_start, monkeypatch, resumed):
    case = http_start
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask")))
    orchestrator = case.app.state.orchestrator
    register_clarification_tool(orchestrator.tool_registry)
    orchestrator.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    # Exhaust at once: the retry path itself is covered by the Router and Lead tests.
    orchestrator.router.max_retries = 0
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", orchestrator.event_registry)
    artifacts = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "helper-transport-uploads"))
    await artifacts.initialize()
    case.app.state.artifact_store = orchestrator.artifact_store = artifacts
    profile = case.fixture.case.args["reference_start"].profile_name
    question = {"question": "Which audience?", "options": [], "type": "free_text"}
    failing_call = "helper-after-answer" if resumed else "helper-first"
    lead_calls, helper_calls = [], []

    def spawn(call_id):
        return call(
            call_id, "subagent_spawn", {"profile": profile, "task": "Inspect this request.", "delegation_role": "helper", "required_source_urls": []}
        )

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            step = len(lead_calls)
            if resumed and step == 1:
                return response(body, spawn("helper-first"), "tool_calls")
            if resumed and step == 2:
                return response(body, call("question-native", "clarification_ask", {"questions": [question]}), "tool_calls")
            if (resumed and step == 3) or (not resumed and step == 1):
                return response(body, spawn(failing_call), "tool_calls")
            return response(body, {"role": "assistant", "content": "The helper could not finish; please try again."}, "stop")
        if "subagent_return" in names:
            helper_calls.append(body)
            if resumed and len(helper_calls) == 1:
                payload = {"delivery_surface": "chat", "exec_summary": "The request needs an audience first."}
                return response(body, call("helper-return", "subagent_return", {"payload": payload}), "tool_calls")
            raise httpx2.ReadError("connection reset by peer")
        return response(body, {"role": "assistant", "content": "Helper request"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send", json={"workspace_id": case.fixture.scope.workspace_id, "message": "Inspect the request, then answer."}
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        if resumed:
            cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
            assert len(cards) == 1
            answered = await case.client.post(
                f"/api/v1/clarifications/{cards[0]['request_id']}/resolve",
                json={"answers": [{"question": question["question"], "source": "custom", "answer": "Board members"}]},
            )
            assert answered.status_code == 200, answered.text
            await settled(turn_id)

    turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
    assert turn["status"] == "completed", turn["error"]
    assert len(helper_calls) == (2 if resumed else 1)
    # Lead received the helper's typed model failure and wrote the final answer itself.
    returned = [message for message in lead_calls[-1]["messages"] if message.get("tool_call_id") == failing_call]
    assert len(returned) == 1
    result = json.loads(returned[0]["content"])
    assert result["ok"] is False
    assert result["llm_reason_code"] == "llm_provider_unavailable"
    assert result.get("execution_outcome") != "unknown"
    persisted = [(row["event_type"], json.loads(row["payload_json"])) for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
    failed = [payload for kind, payload in persisted if kind == "peer_dispatch_failed"]
    assert [payload["call_id"] for payload in failed] == [failing_call]
    assert not [payload for kind, payload in persisted if kind == "turn_failed"]
