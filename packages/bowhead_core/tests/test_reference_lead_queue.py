"""Accepted follow-ups retain their orchestration across queue and rollout changes."""

from __future__ import annotations

import asyncio
import json

import pytest
import respx
from bowhead_gateway.routes import chat, clarifications
from test_reference_http_start import answer_response, enable_lead_writer, lead_has_dispatched, payload, rows, settled, skill_only_payload
from test_reference_http_start import http_start as http_start
from test_reference_http_start import prohibit_unmocked_network as prohibit_unmocked_network
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case
from test_reference_lead_system import system_body
from test_skill_extension_runtime import tool_response

from bowhead_core import events

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("lead_queue,ask", [(False, False), (True, False), (True, True)], ids=["legacy", "lead", "lead-question"])
@pytest.mark.parametrize("receiver", ["team", "automatic", "system"])
async def test_reference_follow_up_keeps_the_accepted_orchestration(http_start, monkeypatch, lead_queue, ask, receiver):
    case = http_start
    if receiver == "system":
        queued_body, _ = await system_body(case, monkeypatch)
    else:
        queued_body = skill_only_payload(case) if receiver == "automatic" else payload(case)
    enable_lead_writer(case, monkeypatch)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", case.app.state.orchestrator.event_registry)
    primary_started = asyncio.Event()
    release_primary = asyncio.Event()
    lead_calls = 0
    primary_calls = 0

    def follow_up(body):
        # The queued child's Lead request carries the parent Turn's answered history.
        return any(message.get("role") == "assistant" and not message.get("tool_calls") for message in body.get("messages", []))

    async def respond(request):
        nonlocal lead_calls, primary_calls
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls += 1
            if not lead_has_dispatched(body):
                return tool_response(
                    "subagent_spawn",
                    {
                        "profile": "compute" if receiver == "system" and follow_up(body) else case.fixture.case.args["reference_start"].profile_name,
                        "task": "Analyze the provided figures.",
                        "delegation_role": "primary",
                    },
                    stream=body.get("stream", False),
                )
        elif "skill_read" in names:
            primary_calls += 1
            if primary_calls == 1:
                primary_started.set()
                await asyncio.wait_for(release_primary.wait(), 120)
            elif primary_calls == 2 and ask:
                return tool_response(
                    "subagent_return",
                    {
                        "payload": {
                            "error": {
                                "kind": "needs_user_input",
                                "message": "Equity is required.",
                                "clarification_payload": {"questions": [{"question": "Average equity?", "type": "free_text", "options": []}]},
                            }
                        }
                    },
                    stream=body.get("stream", False),
                )
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        parent = await case.client.post("/api/v1/chat/send", json=payload(case, skills=False))
        assert parent.status_code == 200, parent.text
        parent_id, session_id = parent.json()["turn_id"], parent.json()["session_id"]
        started = asyncio.create_task(primary_started.wait())
        try:
            parent_tasks = [task for task in chat._inflight_tasks if task.get_name().endswith(":" + parent_id)]
            assert parent_tasks
            await asyncio.wait([started, *parent_tasks], timeout=120, return_when=asyncio.FIRST_COMPLETED)
            assert primary_started.is_set(), await case.fixture.case.store.get_turn(parent_id)
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "1" if lead_queue else "0")
            queued_body.update(session_id=session_id, submission_kind="steer", expected_parent_turn_id=parent_id, expected_execution_generation=1)
            queued = await case.client.post("/api/v1/chat/send", json=queued_body)
            assert queued.status_code == 200 and queued.json()["status"] == "buffered", queued.text
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "0" if lead_queue else "1")
            replay = await case.client.post("/api/v1/chat/send", json=queued_body)
            assert replay.status_code == 200 and replay.json() == queued.json()
        finally:
            release_primary.set()
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)
        await settled(parent_id)
        relation = await case.fixture.case.store.get_chat_follow_up(parent_turn_id=parent_id)
        assert relation is not None
        child_id = relation[0]
        child_tasks = [task for task in chat._inflight_tasks if task.get_name().endswith(":" + child_id)]
        if child_tasks:
            await asyncio.wait_for(asyncio.gather(*child_tasks), 120)
        child = await case.fixture.case.store.get_turn(child_id)
        if ask:
            assert child.status == "clarification_pending", child
            execution_ids = [row["id"] for row in rows(case, "turn_agent_execution") if row["turn_id"] == child_id]
            card = next(row for row in rows(case, "clarification_requests") if row["turn_id"] == child_id)
            answered = await case.client.post(
                f"/api/v1/clarifications/{card['request_id']}/resolve",
                json={"chosen_values": ["50"], "answers": [{"question": "Average equity?", "source": "custom", "answer": "50"}]},
            )
            assert answered.status_code == 200 and answered.json()["action"] == "turn_resume", answered.text
            await settled(child_id)
            child = await case.fixture.case.store.get_turn(child_id)
            assert [row["id"] for row in rows(case, "turn_agent_execution") if row["turn_id"] == child_id] == execution_ids
        assert child.status == "completed", child
        plan = next(row for row in rows(case, "chat_reference_plans") if row["turn_id"] == child_id)
        assert json.loads(plan["plan_json"]).get("orchestration_contract") == ("lead-delegation.v1" if lead_queue else None)
        # The parent's selected primary answer is delivered directly (5006e9348), so its Lead runs once.
        # A queued Lead child spawns once; only the system Agent's plain answer still needs Lead
        # finalization, and a resumed question delivers the primary's answer without another Lead call.
        assert lead_calls == ((2 if ask or receiver != "system" else 3) if lead_queue else 1)
        assert primary_calls == (3 if ask else 2)
        executions = [row for row in rows(case, "turn_agent_execution") if row["turn_id"] == child_id]
        assert len(executions) == 1 and executions[0]["agent_id"] == ("system:compute" if receiver == "system" else "osa_exact")
        assert executions[0]["status"] == "completed"
        saved = next(row for row in rows(case, "chat_reference_queued_inputs") if row["client_submission_id"] == queued_body["client_submission_id"])
        assert saved["materialized_turn_id"] == child_id and saved["state"] == "materialized"
