"""BI discovery workspace gates resume through actual HTTP and native workers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import aiosqlite
import httpx2
import pytest
import respx
from bowhead_gateway.datarc_context import DatarcTurnContext
from test_reference_http_start import http_start as _http_start
from test_reference_http_start import payload, rows, settled
from test_reference_http_start import receiver_case as _receiver_case
from test_reference_http_start import start_case as _start_case
from test_reference_prompt_http import _prompt_upload_body
from test_skill_extension_runtime import tool_response
from test_skill_model_invocation import success

http_start, receiver_case, start_case = _http_start, _receiver_case, _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_bi_metadata_unused_sop"], indirect=True),
]


@pytest.mark.parametrize(
    "tool,read_first,with_file,change",
    [
        ("bi_schema", False, False, None),
        ("bi_schema", True, True, None),
        ("bi_list_measures", True, False, None),
        ("bi_list_dimensions", False, True, None),
        ("bi_schema", False, False, "revoked"),
        ("bi_query", False, False, "revoked"),
        ("bi_schema", False, False, "dismissed"),
        ("bi_schema", False, False, "dismissed_deferred"),
        ("bi_schema", False, False, "dismissed_new_turn"),
        ("bi_schema", False, False, "expired"),
        ("bi_schema", False, False, "decision_race"),
    ],
)
async def test_discovery_workspace_selection_resumes_exact_original_call(http_start, monkeypatch, tool, read_first, with_file, change):
    from bowhead_gateway.routes import chat, clarifications

    from bowhead_core import events
    from bowhead_gateway import datarc_context

    case = http_start
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", case.app.state.orchestrator.event_registry)

    requests = []

    class DatarcClient:
        async def list_measures(self, project):
            requests.append(("measures", project))
            return []

        async def list_dimensions(self, project):
            requests.append(("dimensions", project))
            return []

    async def connector(*args, **kwargs):
        assert kwargs["team_id"] == case.fixture.scope.team_id
        return DatarcTurnContext(
            datarc_client=DatarcClient(),
            accessible_datarc_workspaces=({"id": "project_a", "name": "A"}, {"id": "project_b", "name": "B"}),
            active_datarc_project=kwargs.get("pinned_project"),
        )

    monkeypatch.setattr(chat, "resolve_datarc_turn_context", connector)
    monkeypatch.setattr(datarc_context, "resolve_datarc_turn_context", connector)
    body = payload(case)
    if with_file:
        body = await _prompt_upload_body(SimpleNamespace(case=case, body=body))
    bodies = []

    def respond(request):
        sent = json.loads(request.content)
        bodies.append(sent)
        if read_first and len(bodies) == 1:
            return tool_response("skill_read", {"name": "unused", "ref": "guide"}, stream=sent.get("stream", False))
        if len(bodies) == 1 + int(read_first):
            return tool_response(tool, {"query": "profit"} if tool == "bi_query" else {}, stream=sent.get("stream", False))
        if len(bodies) == 2 + int(read_first):
            # Datarc data reaches the model only through tool calls: nothing is prefetched before this request.
            assert requests == []
            response = tool_response(tool, {}, stream=sent.get("stream", False))
            return httpx2.Response(200, headers=response.headers, content=response.content.replace(f"call-{tool}".encode(), f"run-{tool}".encode()))
        return success("openai", "stream" if sent.get("stream") else "invoke")

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        response = await case.client.post("/api/v1/chat/send", json=body)
        assert response.status_code == 200, response.text
        turn_id = response.json()["turn_id"]
        await settled(turn_id)
        turn = await case.fixture.case.store.get_turn(turn_id)
        assert turn.status == "clarification_pending", (turn.status, turn.error_kind)
        original_usage = (turn.tokens_input, turn.tokens_output, turn.cost_usd)
        original_invocations = rows(case, "chat_reference_model_invocations")
        (pause,) = rows(case, "chat_reference_bi_continuations")
        assert pause["checkpoint_version"] == (1 if tool == "bi_query" else 4) and pause["state"] == "ready"
        assert pause["captured_iterations"] == 1 + int(read_first)
        execution = rows(case, "turn_agent_execution")[0]["id"]
        before = rows(case, "chat_reference_resolutions")
        assert requests == []
        question = pause["clarification_id"]
        # Maintenance must preserve a healthy pause even when no task is queued.
        assert await case.fixture.case.store.recover_expired_chat_spawn_dispatches() == []
        assert (await case.fixture.case.store.get_turn(turn_id)).status == "clarification_pending"
        if change == "revoked":
            with sqlite3.connect(case.path) as db:
                db.execute("UPDATE agent_assignment SET personal_state='disabled'")
        # Original file contents and method materials must survive their live sources changing.
        for path in case.fixture.root.rglob("*.md"):
            path.unlink()
        if with_file:
            for path in (case.path.parent / "prompt-uploads").rglob("*"):
                if path.is_file():
                    path.write_bytes(b"REPLACED_FILE_MATERIAL")
        if change == "decision_race":
            selected_race, dismissed_race = await asyncio.gather(
                case.client.post(f"/api/v1/clarifications/{question}/resolve", json={"chosen_value": "project_b"}),
                case.client.post(f"/api/v1/clarifications/{question}/dismiss"),
            )
            winner = await case.app.state.clarification_store.get(question)
            if winner.status == "dismissed":
                assert dismissed_race.status_code == 200 and selected_race.status_code == 409, (dismissed_race.text, selected_race.text)
                change = "dismissed"
            else:
                assert winner.status == "resolved" and selected_race.status_code == 200
                assert dismissed_race.status_code in {200, 409}, dismissed_race.text
                if dismissed_race.status_code == 200:
                    assert not dismissed_race.json()["dismissed"]
        if change in {"dismissed", "dismissed_deferred", "dismissed_new_turn", "expired"}:
            if change == "expired":
                await case.app.state.clarification_store.invalidate_one(
                    question, target_status="expired", reason="clarification_ttl_expired", actor="system"
                )
            else:
                original_execute = aiosqlite.Connection.execute

                def unavailable(db, sql, *args, **kwargs):
                    if change in {"dismissed_deferred", "dismissed_new_turn"} and "d.state='terminal' AND t.status='clarification_pending'" in sql:
                        raise sqlite3.OperationalError("transient maintenance database failure")
                    return original_execute(db, sql, *args, **kwargs)

                with monkeypatch.context() as outage:
                    outage.setattr(aiosqlite.Connection, "execute", unavailable)
                    dismissed = await case.client.post(f"/api/v1/clarifications/{question}/dismiss")
                assert dismissed.status_code == 200 and dismissed.json()["dismissed"], dismissed.text
            if change != "dismissed":
                assert (await case.fixture.case.store.get_turn(turn_id)).status == "clarification_pending"
                if change == "dismissed_new_turn":
                    await case.fixture.case.store.record_turn(turn_id="t_after_dismiss", session_id=turn.session_id, user_text="Replacement task")
                assert await case.fixture.case.store.recover_expired_chat_spawn_dispatches() == [turn_id]
                if change == "dismissed_new_turn":
                    assert (await case.fixture.case.store.get_turn("t_after_dismiss")).status == "running"
                    assert (await case.fixture.case.store.get_session(turn.session_id)).active_turn_id == "t_after_dismiss"
            expected_status = "failed" if change == "expired" else "cancelled"
            turn = await case.fixture.case.store.get_turn(turn_id)
            assert turn.status == expected_status, (turn.status, turn.error_kind)
            assert (turn.tokens_input, turn.tokens_output, turn.cost_usd) == original_usage
            assert rows(case, "chat_reference_model_invocations") == original_invocations
            assert rows(case, "turn_agent_execution")[0]["status"] == expected_status
            assert rows(case, "chat_reference_bi_continuations")[0]["state"] == "closed"
            terminal = [json.loads(event["payload_json"]) for event in rows(case, "turn_events") if event["event_type"] == "done"]
            assert terminal[-1]["status"] == expected_status
            assert route.call_count == 1
            before_events = rows(case, "turn_events")
            if change != "expired":
                assert (await case.client.post(f"/api/v1/clarifications/{question}/dismiss")).json() == dismissed.json()
            assert (await case.client.post(f"/api/v1/clarifications/{question}/resume_turn")).status_code == 409
            assert await case.fixture.case.store.recover_expired_chat_spawn_dispatches() == []
            assert rows(case, "turn_events") == before_events
            return
        selected = await case.client.post(f"/api/v1/clarifications/{question}/resolve", json={"chosen_value": "project_b", "remember": False})
        if change == "revoked":
            assert selected.status_code == 409, selected.text
            assert (await case.app.state.clarification_store.get(question)).status == "resolved"
            assert (await case.client.post(f"/api/v1/clarifications/{question}/resume_turn")).status_code == 409
            assert route.call_count == 1 + int(read_first)
            assert (await case.fixture.case.store.get_turn(turn_id)).status == "clarification_pending"
            assert rows(case, "chat_reference_bi_continuations")[0]["state"] == "ready"
            return
        assert selected.status_code == 200, selected.text
        assert selected.json()["chosen_value"] == "project_b"
        assert selected.json()["action"] == "turn_resume"
        resolution_events = [event for event in rows(case, "turn_events") if event["event_type"] == "clarification.resolved"]
        assert len(resolution_events) == 1
        repeated = await case.client.post(f"/api/v1/clarifications/{question}/resolve", json={"chosen_value": "project_a"})
        assert repeated.status_code == 200 and repeated.json()["chosen_value"] == "project_b"
        assert [event for event in rows(case, "turn_events") if event["event_type"] == "clarification.resolved"] == resolution_events
        resumed = await case.client.post(f"/api/v1/clarifications/{question}/resume_turn")
        assert resumed.status_code == 200, resumed.text
        await settled(turn_id)
        assert (await case.fixture.case.store.get_turn(turn_id)).status == "completed"
        assert route.call_count == 3 + int(read_first)
        assert (await case.client.post(f"/api/v1/clarifications/{question}/resume_turn")).json() == resumed.json()
        assert route.call_count == 3 + int(read_first)
        expected = {"measures", "dimensions"} if tool == "bi_schema" else {"measures" if tool == "bi_list_measures" else "dimensions"}
        assert sorted(requests) == sorted((name, "project_b") for name in expected)
        messages = bodies[-1]["messages"]
        original = [call for message in messages for call in message.get("tool_calls", []) if call["id"] == f"call-{tool}"]
        assert len(original) == 1 and original[0]["function"]["name"] == tool
        assert json.loads(original[0]["function"]["arguments"]) == {}
        assert any(message.get("tool_call_id") == f"call-{tool}" and "project_b" in message["content"] for message in messages)
        assert rows(case, "chat_reference_resolutions") == before
        assert rows(case, "turn_agent_execution")[0]["id"] == execution
        assert rows(case, "chat_reference_bi_continuations")[0]["state"] == "closed"
        if read_first:
            assert any(message.get("tool_call_id") == "call-skill_read" and "BASELINE_GUIDE_unused" in message["content"] for message in messages)
        if with_file:
            assert "period,profit\\nFY2026,42" in json.dumps(messages)
            assert "REPLACED_FILE_MATERIAL" not in json.dumps(messages)
        evidence = await case.client.get(f"/api/v1/turns/{turn_id}/reference-evidence")
        assert evidence.status_code == 200
        assert {item["generation"] for item in evidence.json()["model_invocations"]} == {1, 2}
