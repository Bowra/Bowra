"""Native admission repair and the actual ordinary Lead terminal publisher."""

import asyncio
import json

import httpx2
import pytest
import respx
from bowhead_core.documents.executor import PROFILE
from test_document_preflight_question_http import prepare_document_http
from test_document_runner import IMAGE
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import rows, settled
from test_reference_http_start import start_case as start_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.mark.parametrize("ending", ["prose", "protocol", "repair", "provider", "accepted"])
async def test_known_admission_rejection_cannot_become_successful_prose(http_start, monkeypatch, ending):
    case = http_start
    _, worker, attachment = await prepare_document_http(case, monkeypatch)
    original = "Use the attached PPTX as the template. Update its title for the next quarter, then prepare a draft and PDF."
    calls, domain = [], []

    def respond(request):
        body = json.loads(request.content)
        if not body.get("tools"):
            try:
                payload = json.loads(body["messages"][-1]["content"])
            except (ValueError, TypeError):
                payload = None
            if not isinstance(payload, dict) or "instruction" not in payload:
                return httpx2.Response(
                    200,
                    json={
                        "choices": [{"message": {"role": "assistant", "content": "Document admission fixture"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                    },
                )
            domain.append(body)
            message = {
                "role": "assistant",
                "content": json.dumps({"action": "ask", "reason": "comparison", "slots": ["s0"], "question": "Which comparison?"}),
            }
            finish = "stop"
        else:
            calls.append(body)
            index = len(calls) - 1
            if ending == "provider" and index:
                return httpx2.Response(503, json={"error": {"message": "Controlled provider outage"}})
            if index == 0 or ending == "repair" and index == 1:
                arguments = {
                    "action": "generate",
                    "evidence": {"text": original if ending == "accepted" or index else "An invented quotation absent from the request"},
                    "inputs": [{"artifact_id": attachment.id, "role": "template", "evidence": {"text": "Use the attached PPTX as the template."}}],
                    "requested_outputs": ["source", "pdf"],
                }
                message = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"admission-{index}",
                            "type": "function",
                            "function": {"name": "template_document_submit", "arguments": json.dumps(arguments)},
                        }
                    ],
                }
                finish = "tool_calls"
            else:
                assert index == 1 or ending == "protocol" and index <= 3, (
                    "Only the existing bounded final-answer correction may follow rejected admission"
                )
                message = {
                    "role": "assistant",
                    "content": '<clarification_ask>{"questions": [{"question": "Continue?"}]}</clarification_ask>'
                    if ending == "protocol"
                    else "The requested document work is complete.",
                }
                finish = "stop"
        usage = {"prompt_tokens": 13, "completion_tokens": 7}
        if not body.get("stream"):
            return httpx2.Response(200, json={"choices": [{"message": message, "finish_reason": finish}], "usage": usage})
        chunks = [{"choices": [{"delta": message, "finish_reason": None}]}, {"choices": [{"delta": {}, "finish_reason": finish}], "usage": usage}]
        return httpx2.Response(
            200,
            text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://document-executor/document-runs/capabilities").respond(
            200, json={"profile": PROFILE, "image_id": IMAGE, "planner_profiles": ["pptx-grounded-plan-v5"]}
        )
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        response = await case.client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": case.fixture.scope.workspace_id,
                "message": original,
                "attachments": [attachment.id],
                "attachment_refs": [
                    {
                        "fileId": attachment.id,
                        "filename": attachment.filename,
                        "mime": attachment.mime,
                        "size": attachment.size,
                        "referenceKind": "upload",
                    }
                ],
            },
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200, response.text
        turn_id = response.json()["turn_id"]
        await settled(turn_id)
        turn = await case.fixture.case.store.get_turn(turn_id)
        terminals = [json.loads(row["payload_json"]) for row in rows(case, "turn_events") if row["event_type"] == "done"]
        assert len(terminals) == 1
        terminal = terminals[0]
        if ending in {"repair", "accepted"}:
            assert turn.status == "clarification_pending"
            assert len(domain) == 1 and len(rows(case, "document_jobs")) == len(rows(case, "document_dispatches")) == 1
            assert worker.runs == ["inventory", "apply_verify"]
            assert len(calls) == (2 if ending == "repair" else 1)
            return
        assert not domain and not worker.runs
        assert rows(case, "document_jobs") == rows(case, "document_dispatches") == []
        assert turn.status == "failed"
        if ending == "provider":
            assert terminal.get("reason_code") != "document_request_rejected"
            assert turn.error_kind == terminal["error_kind"] != "document_request_rejected"
            return
        assert len(calls) == (4 if ending == "protocol" else 2)
        assert turn.error_kind == terminal["reason_code"] == terminal["error_kind"] == "document_request_rejected"
        assert terminal["narrative_superseded"] is True and terminal["diagnostics"]["retryable"] is False
        assert turn.final_answer != "The requested document work is complete."
        replay = await asyncio.wait_for(case.client.get(f"/api/v1/chat/stream/{turn_id}"), timeout=10)
        assert replay.status_code == 200, replay.text
        frames = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
        assert any(frame.get("diagnostics") == terminal["diagnostics"] for frame in frames)
