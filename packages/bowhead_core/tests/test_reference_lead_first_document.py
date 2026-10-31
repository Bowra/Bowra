"""Lead, primary and native questions share original HTTP authority and history.

Provider responses and rendering are controlled by the original native fixture;
this verifies lifecycle integration, not model quality or native visual fidelity.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import respx
import test_reference_http_start as reference_http
from bowhead_core._db import open_shared_ctx
from test_document_authoring_turn import authored as authored
from test_document_authoring_turn import call, edits
from test_reference_assigned_read_question import http_start as http_start
from test_reference_assigned_read_question import model_reply
from test_reference_assigned_read_question import original_receiver_case as original_receiver_case
from test_reference_assigned_read_question import receiver_case as receiver_case
from test_reference_assigned_read_question import start_case as start_case
from test_reference_document_answers import native as native
from test_reference_document_answers import submitted as submitted

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("original_receiver_case", ["admission"], indirect=True),
    pytest.mark.parametrize("baseline", ["document_plugin"]),
    pytest.mark.parametrize("read_kind", ["skill", "mcp"]),
]


def answer(question, value):
    return {"answers": [{"question": question, "source": "custom", "answer": value, "skipped": False, "option_index": None}]}


async def test_lead_primary_and_native_answers_complete_one_original_document(http_start, submitted, authored, monkeypatch, read_kind):
    from bowhead_core.agents.clarification_tool import register_clarification_tool
    from bowhead_core.subagent import PROFILES
    from bowhead_gateway.routes import clarifications, documents

    from bowhead_core import events

    case = http_start
    reference_http.enable_lead_writer(case, monkeypatch)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("clarification_ask", "subagent_spawn")))
    register_clarification_tool(case.app.state.orchestrator.tool_registry)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    case.app.include_router(documents.router, prefix="/api/v1")
    monkeypatch.setattr(events, "_default_registry", case.app.state.orchestrator.event_registry)
    authored.responses = [
        call("document_read", {"source": "data-0"}, 0),
        call("clarification_ask", {"questions": [{"question": "Which reporting currency?"}]}, 1),
    ]
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return model_reply(request, name="clarification_ask", arguments={"questions": [{"question": "Which quarter?", "type": "free_text"}]})
        if len(requests) == 2:
            return model_reply(
                request,
                name="subagent_spawn",
                arguments={
                    "profile": case.fixture.case.args["reference_start"].profile_name,
                    "task": "Read the assigned document method, ask for the comparison period, then use the native document Tool.",
                    "delegation_role": "primary",
                },
            )
        if len(requests) == 3:
            return (
                model_reply(request, name=case.document_mcp.read.id, arguments={"year": 2025})
                if case.document_mcp is not None
                else model_reply(request, name="skill_read", arguments={"name": "pptx-authoring"})
            )
        if len(requests) == 4:
            return model_reply(
                request,
                name="subagent_return",
                arguments={
                    "payload": {
                        "error": {
                            "kind": "needs_user_input",
                            "message": "The comparison period is required.",
                            "clarification_payload": {"questions": [{"question": "Which comparison period?", "type": "free_text"}]},
                        }
                    }
                },
            )
        assert len(requests) == 5, "Continue the same primary without another read or delegation"
        return model_reply(request, name="template_document_submit", arguments=authored.arguments)

    def cards(turn_id):
        return [row for row in reference_http.rows(case, "clarification_requests") if row["turn_id"] == turn_id]

    async def resolve(turn_id, question, value):
        pending = [row for row in cards(turn_id) if row["status"] == "pending"]
        assert len(pending) == 1
        response = await case.client.post(f"/api/v1/clarifications/{pending[0]['request_id']}/resolve", json=answer(question, value))
        assert response.status_code == 200, response.text
        await reference_http.settled(turn_id)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=submitted)
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await reference_http.settled(turn_id)
        await resolve(turn_id, "Which quarter?", "2029 Q1")
        turn = await authored.domain.sessions.get_turn(turn_id)
        assert (turn.status, turn.execution_generation, len(requests)) == ("clarification_pending", 2, 4), turn.error_kind
        await resolve(turn_id, "Which comparison period?", "2028 Q4")
        turn = await authored.domain.sessions.get_turn(turn_id)
        assert (turn.status, turn.execution_generation, len(requests)) == ("clarification_pending", 3, 5), turn.error_kind
        assert all(value in json.dumps(authored.calls) for value in ("2029 Q1", "2028 Q4"))
        authored.responses = edits(authored.domain)
        await resolve(turn_id, "Which reporting currency?", "USD")
        turn = await authored.domain.sessions.get_turn(turn_id)
        assert (turn.status, turn.execution_generation) == ("completed", 4), turn.error_kind
        assert len(requests) == 5
        assert all(value in json.dumps(authored.calls) for value in ("2029 Q1", "2028 Q4", "USD"))
        jobs = [row for row in reference_http.rows(case, "document_jobs") if row["origin_turn_id"] == turn_id]
        assert len(jobs) == 1
        job_id = jobs[0]["id"]
        draft = await authored.service.delivery.get(scope=authored.scope, job_id=job_id)
        assert draft["state"] == "review"
        executions = [row for row in reference_http.rows(case, "turn_agent_execution") if row["turn_id"] == turn_id]
        assert len(executions) == 1 and executions[0]["status"] == "completed"
        records = [row for row in reference_http.rows(case, "turn_events") if row["turn_id"] == turn_id]
        assert sum(row["event_type"] == "peer_dispatched" for row in records) == 1
        results = [json.loads(row["payload_json"]) for row in records if row["event_type"] == "tool_result"]
        assert sum(row.get("tool_name") == "skill_read" for row in results) == (1 if read_kind == "skill" else 0)
        if case.document_mcp is not None:
            assert len(case.document_mcp.calls) == 1
        assert [json.loads(row["payload_json"])["status"] for row in records if row["event_type"] == "done"] == [
            "clarification_pending",
            "clarification_pending",
            "clarification_pending",
            "completed",
        ]
        from bowhead_core.chat_reference_runtime_store import is_reference_runtime_in_connection

        async with open_shared_ctx(case.path) as db:
            await db.execute("BEGIN")
            assert not await is_reference_runtime_in_connection(db, turn_id=turn_id), "Native generation four is not a generic Reference resume"
            await db.rollback()
        fields = {"command_id": "confirm-lead-first-document", "draft_digest": draft["draft_digest"]}
        confirmed = await case.client.post(f"/api/v1/documents/{job_id}/confirm", json=fields)
        assert confirmed.status_code == 200 and confirmed.json()["state"] == "published", confirmed.text
        assert (await case.client.post(f"/api/v1/documents/{job_id}/confirm", json=fields)).json() == confirmed.json()
        assert len([row for row in reference_http.rows(case, "document_confirmations") if row["job_id"] == job_id]) == 1
        session = next(row for row in reference_http.rows(case, "sessions") if row["id"] == submitted["session_id"])
        assert session["active_turn_id"] is None and json.loads(session["steer_buffer_json"]) == []
