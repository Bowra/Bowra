"""Current request authority through native admission, Lead repair and replay."""

import hashlib
import json

import pytest
import respx
from bowhead_core.chat_references import InputDocument
from test_reference_http_start import answer_response, enable_lead_writer, payload, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import prohibit_unmocked_network as prohibit_unmocked_network
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case
from test_skill_extension_runtime import tool_response

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


async def test_followup_correction_and_primary_preserve_current_request(http_start, monkeypatch):
    case = http_start
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setenv("BOWHEAD_CTX_TURN_HISTORY", "1")
    store = case.fixture.case.store
    session_id = "s_followup_history"
    previous_request = "Read north.txt and south.txt and list their revenue and profit."
    current_request = "Use only profit 18, revenue 120, assets 80 and equity 40 to calculate ROE. Do not read old files or create a report."
    await store.ensure_session(session_id=session_id, workspace_id=case.fixture.scope.workspace_id, user_id=case.fixture.scope.user_id)
    await store.record_turn(turn_id="t_previous_files", session_id=session_id, user_text=previous_request)
    await store.mark_completed(
        turn_id="t_previous_files", intent=None, final_answer="File reading complete.", tokens_input=0, tokens_output=0, cost_usd=0
    )
    await store.finalize_turn(session_id=session_id, current_turn_id="t_previous_files", next_turn_id=None)
    body = payload(case)
    body["session_id"] = session_id
    body["input_document"]["nodes"] = [node for node in body["input_document"]["nodes"] if node["type"] == "reference"] + [
        {"type": "text", "text": current_request}
    ]
    body["message"] = InputDocument.model_validate(body["input_document"]).display_text()
    agent_id = next(node["resource"]["id"] for node in body["input_document"]["nodes"] if node.get("resource", {}).get("kind") == "agent")
    lead_calls, primary_calls = [], []

    def respond(request):
        sent = json.loads(request.content)
        names = {tool["function"]["name"] for tool in sent.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(sent)
            if len(lead_calls) == 1:
                texts = [message["content"] for message in sent["messages"] if isinstance(message.get("content"), str)]
                assert any(previous_request in text for text in texts)
                assert any(current_request in text for text in texts)
                return answer_response(request)  # Prose without the required primary dispatch.
            if len(lead_calls) == 2:
                repair = next(message["content"] for message in sent["messages"] if "The selected primary Agent" in str(message.get("content", "")))
                assert current_request in repair
                assert previous_request not in repair
                # Even a stale model paraphrase must not replace the admitted request.
                return tool_response("subagent_spawn", {"agent_id": agent_id, "task": previous_request}, stream=sent.get("stream", False))
            return answer_response(request)
        if "skill_read" in names:
            primary_calls.append(sent)
            task = next(message["content"] for message in sent["messages"] if "Execute the current user request" in str(message.get("content", "")))
            material, _ = json.JSONDecoder().raw_decode(task[task.index('{"current_user_request"') :])
            assert material == {"current_user_request": current_request, "lead_delegation": previous_request}
            assert "takes precedence" in task
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=body)
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        assert (await store.get_turn(turn_id)).status == "completed"
        assert len(primary_calls) == 1
        replay = await case.client.post("/api/v1/chat/send", json=body)
        assert replay.status_code == 200 and replay.json()["turn_id"] == turn_id
        assert len(primary_calls) == 1
    evidence = await case.client.get(f"/api/v1/turns/{turn_id}/reference-evidence?include_content=true")
    assert evidence.status_code == 200, evidence.text
    projected = evidence.json()["delegated_payload"]
    task = projected["content"]
    assert any(task in str(message.get("content", "")) for message in primary_calls[0]["messages"])
    assert projected["content_hash"] == "sha256:" + hashlib.sha256(task.encode()).hexdigest()
    source = await store.get_chat_turn_user_source(turn_id=turn_id, user_id=case.fixture.scope.user_id, workspace_id=case.fixture.scope.workspace_id)
    assert source.input_document.text_content() == current_request
