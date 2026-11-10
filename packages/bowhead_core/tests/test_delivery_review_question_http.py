"""A native delivery correction survives the original question and answer flow."""

import json
from dataclasses import replace

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


@pytest.mark.parametrize("with_rejection", [False, True])
async def test_native_review_correction_preserves_helper_through_question_answer_and_replay(http_start, monkeypatch, with_rejection):
    case = http_start
    case.client.headers["Content-Type"] = "application/json"
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "1")
    monkeypatch.setenv("BOWHEAD_PAYLOAD_SOFT_GATE", "1")
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask", "skill_list")))
    orch = case.app.state.orchestrator
    register_clarification_tool(orch.tool_registry)
    orch.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    artifacts = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "review-question-uploads"))
    await artifacts.initialize()
    case.app.state.artifact_store = orch.artifact_store = artifacts
    question = {"question": "Which audience?", "options": [], "type": "free_text"}
    lead_calls, helper_calls, live = [], [], []
    emit = events.TurnEventBus.emit

    async def observe(bus, event):
        live.append(event)
        return await emit(bus, event)

    monkeypatch.setattr(events.TurnEventBus, "emit", observe)

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            index = len(lead_calls)
            if index == 1:
                message = call(
                    "helper-native",
                    "subagent_spawn",
                    {
                        "profile": case.fixture.case.args["reference_start"].profile_name,
                        "task": "Inspect the request.",
                        "delegation_role": "helper",
                        "required_source_urls": [],
                    },
                )
            elif index == 2:
                message = call("question-native", "clarification_ask", {"questions": [question]})
            elif index == 3:
                message = call("list-native", "skill_list", {})
            elif index == 4:
                message = call(
                    "scope-review",
                    "review_delivery",
                    {
                        "payload": {
                            "delivery_review": {
                                "requirements": [
                                    {
                                        "requirement": "List Skills for the selected audience after clarification",
                                        "status": "fulfilled",
                                        "evidence_call_ids": ["list-native"],
                                    }
                                ],
                                "issues": [],
                            }
                        }
                    },
                )
            else:
                assert index == 5
                return response(body, {"role": "assistant", "content": "The available Skills have been listed for Board members."}, "stop")
            return response(body, message, "tool_calls")
        if "subagent_return" in names:
            helper_calls.append(body)
            assert len(helper_calls) <= (2 if with_rejection else 1)
            payload = {"delivery_surface": "chat", "exec_summary": "The request needs an audience before its Skill list."}
            if with_rejection and len(helper_calls) == 1:
                payload["delivery_review"] = {}
            return response(
                body,
                call("review-correction" if with_rejection and len(helper_calls) == 1 else "helper-return", "subagent_return", {"payload": payload}),
                "tool_calls",
            )
        return response(body, {"role": "assistant", "content": "Skill selection"}, "stop")

    with respx.mock(assert_all_called=False) as transport:
        transport.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": case.fixture.scope.workspace_id,
                "message": "Inspect the request, ask which audience to address, then list the available Skills.",
            },
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert len(cards) == 1, "A known native correction must not invalidate the later question capture"
        original_helper = next(message for message in lead_calls[1]["messages"] if message.get("tool_call_id") == "helper-native")
        card_id = cards[0]["request_id"]
        visible = await case.client.get(f"/api/v1/clarifications/{card_id}")
        assert visible.status_code == 200 and visible.json()["questions"] == [question]
        answered = await case.client.post(
            f"/api/v1/clarifications/{card_id}/resolve",
            json={"answers": [{"question": question["question"], "source": "custom", "answer": "Board members"}]},
        )
        assert answered.status_code == 200, answered.text
        assert answered.json()["resume_turn_id"] == turn_id
        await settled(turn_id)
        turn = await case.fixture.case.store.get_turn(turn_id)
        assert turn.status == "completed", turn.error
        assert len(helper_calls) == (2 if with_rejection else 1), "Answering must not rerun the retained helper"
        review_message = next(message for message in lead_calls[4]["messages"] if message.get("tool_call_id") == "scope-review")
        assert json.loads(review_message["content"])["ok"] is True
        resumed_helper = next(message for message in lead_calls[2]["messages"] if message.get("tool_call_id") == "helper-native")
        assert resumed_helper == original_helper
        saved = [
            json.loads(row["payload_json"])
            for row in rows(case, "turn_events")
            if row["turn_id"] == turn_id
            and row["event_type"] == "tool_result"
            and json.loads(row["payload_json"]).get("call_id") == "review-correction"
        ]
        actual_live = [
            event.payload
            for event in live
            if event.turn_id == turn_id and event.event_type == "tool_result" and event.payload.get("call_id") == "review-correction"
        ]
        assert len(saved) == int(with_rejection) and saved == actual_live
        replay = await case.client.get(accepted.json()["stream_url"])
        replayed = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
        assert any(payload.get("call_id") == "review-correction" for payload in replayed) == with_rejection
        assert "clarification_resume_unavailable" not in replay.text
