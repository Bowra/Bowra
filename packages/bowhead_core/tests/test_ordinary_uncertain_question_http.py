"""Real original-card continuation preserves failed and unknown helper outcomes."""

import asyncio
import json
from dataclasses import replace

import httpx2
import pytest
import respx
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.ordinary_lead_continuation import read_ordinary_card_in_tx, reopen_ordinary_checkpoint
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


@pytest.mark.parametrize("agent_source", ["team", "system"])
@pytest.mark.parametrize(
    "boundary",
    [
        "unknown",
        "refused",
        "skip",
        "conflicting_answers",
        "model_retry",
        "live_retry",
        "live_retry_question",
        "terminal_write_failed",
        "authority_changed",
    ],
)
async def test_helper_failure_continues_original_lead_without_replaying(http_start, monkeypatch, agent_source, boundary):
    case = http_start
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "1")
    monkeypatch.setenv("BOWHEAD_PI_K_ENABLED", "0")
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask"), max_iterations=8))
    orch = case.app.state.orchestrator
    orch.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    register_clarification_tool(orch.tool_registry)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    case.client.headers["Content-Type"] = "application/json"
    profile = "compute" if agent_source == "system" else case.fixture.case.args["reference_start"].profile_name
    lead_calls, helper_calls = [], []
    question = {"question": "Should I explain the retained facts?", "type": "free_text", "options": []}

    if boundary == "terminal_write_failed":

        async def lose_terminal_write(self, execution_id, **kwargs):
            raise OSError("Synthetic terminal write failure")

        monkeypatch.setattr(AgentExecutionBindingStore, "mark_terminal", lose_terminal_write)

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "clarification_ask" in names:
            lead_calls.append(body)
            index = len(lead_calls)
            if index == 1:
                message = call(
                    "original-helper", "subagent_spawn", {"profile": profile, "task": "Inspect synthetic facts.", "delegation_role": "helper"}
                )
            elif index == 2 and boundary in {"live_retry", "live_retry_question"}:
                message = call("next-helper", "subagent_spawn", {"profile": profile, "task": "Inspect again.", "delegation_role": "helper"})
            elif index == 3 and boundary == "live_retry_question":
                message = call("later-question", "clarification_ask", {"questions": [question]})
            elif index == 2:
                message = call("original-question", "clarification_ask", {"questions": [question]})
            elif index == 3 and boundary == "model_retry":
                message = call(
                    "new-id-retry",
                    "subagent_spawn",
                    {"profile": profile, "task": "Repeat the earlier inspection with different wording.", "delegation_role": "helper"},
                )
            else:
                return response(
                    body,
                    {
                        "role": "assistant",
                        "content": "The next inspection completed."
                        if boundary in {"model_retry", "live_retry", "live_retry_question"}
                        else "The earlier helper did not return a confirmed result. No operation was repeated.",
                    },
                    "stop",
                )
            return response(body, message, "tool_calls")
        if "subagent_return" in names:
            helper_calls.append(body)
            if len(helper_calls) == 2 and boundary in {"model_retry", "live_retry", "live_retry_question"}:
                return response(body, {"role": "assistant", "content": "The new inspection completed with synthetic facts."}, "stop")
            return httpx2.Response(401 if boundary == "refused" else 503, json={"error": {"message": "Synthetic provider refusal"}})
        return response(body, {"role": "assistant", "content": "Synthetic title"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send", json={"workspace_id": case.fixture.scope.workspace_id, "message": "Inspect, then ask before explaining."}
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        if boundary in {"live_retry", "live_retry_question"}:
            turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
            assert turn["status"] == "completed" and "next inspection completed" in turn["final_answer"]
            assert len(helper_calls) == 2
            executions = [
                row
                for row in rows(case, "turn_agent_execution")
                if row["turn_id"] == turn_id and row["dispatch_key"] in {"call:original-helper", "call:next-helper"}
            ]
            assert sorted(row["status"] for row in executions) == ["completed", "failed"]
            assert cards == []
            if boundary == "live_retry_question":
                assert len(lead_calls) == 4
                refusal = next(message for message in lead_calls[-1]["messages"] if message.get("tool_call_id") == "later-question")
                assert "clarification_capture_unavailable" in refusal["content"]
                earlier = next(message for message in lead_calls[-1]["messages"] if message.get("tool_call_id") == "original-helper")
                assert '"execution_outcome": "unknown"' in earlier["content"]
            return
        if boundary == "terminal_write_failed":
            assert cards == []
            assert len(helper_calls) == 1
            assert next(row for row in rows(case, "turns") if row["id"] == turn_id)["status"] == "failed"
            return
        assert len(cards) == 1, [(row["status"], row["error"]) for row in rows(case, "turns") if row["id"] == turn_id]
        card = cards[0]
        executions = [
            row for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id and row["dispatch_key"] != "collaboration-lead:" + turn_id
        ]
        assert len(executions) == 1 and executions[0]["status"] == "failed"
        assert len(helper_calls) == 1

        def helper_receipts():
            return [
                row
                for row in rows(case, "llm_usage_events")
                if row["receipt_json"] and json.loads(row["receipt_json"]).get("execution_id") == executions[0]["id"]
            ]

        receipts = helper_receipts()
        assert len(receipts) == 1
        assert receipts[0]["receipt_state"] == ("not_applied" if boundary == "refused" else "unknown")
        async with open_shared_ctx(case.path) as db:
            await db.execute("BEGIN")
            checkpoint = reopen_ordinary_checkpoint(await read_ordinary_card_in_tx(db, card_id=card["request_id"]))
        assert checkpoint.contract == "ordinary-lead-loop.v9"
        outcome = checkpoint.helper_materials()[0]["outcome"]
        assert outcome["reason"] == ("model_failure" if boundary == "refused" else "model_unknown")
        assert checkpoint.invocations()[0].result["ok"] is False
        if boundary != "refused":
            assert checkpoint.invocations()[0].result["execution_outcome"] == "unknown"
            assert "error_code" not in checkpoint.invocations()[0].result
            assert "subagent_execution_uncertain" not in json.dumps(lead_calls[1])
            assert "subagent_failed" not in json.dumps(lead_calls[1])
            assert outcome["failure_code"] == "peer_execution_uncertain"
            persisted = await case.app.state.turn_log_store.list_events(turn_id, event_types=("peer_dispatch_failed", "step_done"))
            failed = [event for event in persisted if event.event_type == "peer_dispatch_failed"]
            assert len(failed) == 1
            assert failed[0].payload["reason_code"] == "peer_execution_uncertain"
            assert failed[0].payload["diagnostics"]["retryable"] is False
            steps = [event for event in persisted if event.event_type == "step_done" and event.payload.get("error")]
            assert any(event.payload["error"].get("code") == "peer_execution_uncertain" for event in steps)
        history = await case.client.get(f"/api/v1/clarifications/turn/{turn_id}")
        assert history.status_code == 200
        assert history.json()["resume_failure"] is None
        if boundary == "authority_changed":
            if agent_source == "system":
                monkeypatch.delitem(PROFILES, "compute")
            else:
                async with open_shared_ctx(case.path) as db:
                    await db.execute("DELETE FROM agent_assignment WHERE id=?", (case.fixture.case.args["reference_start"].expected.assignment_id,))
                    await db.commit()
            reply = await case.client.post(
                f"/api/v1/clarifications/{card['request_id']}/resolve",
                json={"answers": [{"question": question["question"], "source": "custom", "answer": "Explain"}]},
            )
            assert reply.status_code == 409
            saved = next(row for row in rows(case, "clarification_requests") if row["request_id"] == card["request_id"])
            assert saved["injected_at"] is None
            assert len(helper_calls) == 1 and len(lead_calls) == 2
            assert helper_receipts() == receipts
            return
        # Disabling new captures must not disable the original card reader.
        monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "0")
        answer = {"skip": True} if boundary == "skip" else {"answers": [{"question": question["question"], "source": "custom", "answer": "Explain"}]}
        attempts = [answer]
        if boundary == "conflicting_answers":
            attempts.append({"answers": [{"question": question["question"], "source": "custom", "answer": "Summarize"}]})
        url = f"/api/v1/clarifications/{card['request_id']}/resolve"
        replies = await asyncio.gather(*(case.client.post(url, json=item) for item in attempts))
        assert all(reply.status_code in {200, 409} for reply in replies)
        assert any(reply.status_code == 200 for reply in replies), [reply.text for reply in replies]
        await settled(turn_id)
        saved = next(row for row in rows(case, "clarification_requests") if row["request_id"] == card["request_id"])
        assert saved["continuation_generation"] == 2 and saved["injected_at"] is not None
        assert saved["resume_context_json"] == card["resume_context_json"]
        assert len(helper_calls) == (2 if boundary == "model_retry" else 1)
        assert helper_receipts() == receipts
        assert [
            row for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id and row["dispatch_key"] == "call:original-helper"
        ] == executions
        turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
        assert turn["status"] == "completed"
        assert ("next inspection completed" if boundary == "model_retry" else "No operation was repeated") in turn["final_answer"]
        if boundary == "model_retry":
            retried = next(
                row for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id and row["dispatch_key"] == "call:new-id-retry"
            )
            assert retried["status"] == "completed"
        replay = await case.client.post(url, json=answer)
        assert replay.status_code == 200 and replay.json()["answers"] == json.loads(saved["answers_json"])
        assert len(helper_calls) == (2 if boundary == "model_retry" else 1)
