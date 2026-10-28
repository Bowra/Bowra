"""Native Plan collaboration preserves routing, membership and owner lifetime."""

import json
import re
from dataclasses import replace

import httpx2
import pytest
import respx
from bowhead_core.agents.plan_propose_tool import register_plan_propose_tool
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.agents.report_tools import register_report_tools
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.plan_store import PlanStore
from bowhead_core.subagent import PROFILES
from test_agent_collaboration_http import _inbox, _tool_results
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.mark.parametrize("trigger", ["spawn", "plan_propose", "approved"])
@pytest.mark.parametrize("coordination_fails", [False, True], ids=["verified", "coordination-denied"])
async def test_plan_exchanges_keep_owner_running_until_verified_completion(http_start, monkeypatch, trigger, coordination_fails):
    case = http_start
    enable_lead_writer(case, monkeypatch)
    from bowhead_core.agents import lead_orchestrator

    # The synthetic assignments use supplied numbers, not the fixture's absent BI connection.
    # Keep admission, Plan routing/execution and collaboration membership unmodified.
    monkeypatch.setattr(lead_orchestrator, "_peer_handles_bi_plan_steps", lambda peer: False)
    for key, value in {
        "BOWHEAD_PLANNING_MODE": "1",
        "BOWHEAD_PLANNING_V2": "1",
        "BOWHEAD_PLANNING_VERIFY": "1",
        "BOWHEAD_WRITE_REPORT": "0",
        "BOWHEAD_PI_K_ENABLED": "0",
        "BOWHEAD_SSE_FANOUT": "1",
    }.items():
        monkeypatch.setenv(key, value)
    orch = case.app.state.orchestrator
    orch.plan_store = PlanStore(str(case.path))
    case.app.state.plan_store = orch.plan_store
    orch.canvas_store = CanvasStore(str(case.path))
    await orch.canvas_store.initialize()
    orch.multi_agent_registry = AgentRegistry.bootstrap()
    register_plan_propose_tool(orch.tool_registry)
    register_report_tools(orch.tool_registry)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "plan_propose")))
    case.client.headers["Content-Type"] = "application/json"
    helper = case.fixture.case.args["reference_start"].profile_name
    monkeypatch.setitem(PROFILES, "compute", replace(PROFILES["compute"], tool_whitelist=("subagent_return", "write_report")))
    observed = []
    calls = {"compute": 0, helper: 0}
    coordinator_calls = []
    lead_calls = 0

    async def respond(request):
        nonlocal lead_calls
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        system = "\n".join(str(message.get("content")) for message in body["messages"] if message["role"] == "system")
        kind = "auxiliary"
        if "把用户的复合项目拆成" in system:
            kind = "plan_author"
            content = json.dumps(
                {
                    "deliverable": "answer",
                    "steps": [
                        {
                            "id": "calculate",
                            "agent_name": "compute",
                            "objective": "Calculate 1200 minus 120, then ask the policy expert to verify the refund definition.",
                            "deliverable": "insight",
                            "needs_corpus": False,
                            "needs_web": False,
                            "depends_on": [],
                        },
                        {
                            "id": "policy",
                            "agent_name": helper,
                            "objective": "Confirm refund-v7 with Lead and answer compute about the 2026-Q3 ledger period.",
                            "needs_corpus": False,
                            "needs_web": False,
                            "depends_on": [],
                        },
                    ],
                }
            )
            result = {"role": "assistant", "content": content}
        elif "你是分析项目的验收官" in system:
            kind = "criteria"
            ids = re.findall(r"id=(ps_\w+)", json.dumps(body))
            result = {
                "role": "assistant",
                "content": json.dumps({"criteria": {step_id: ["Includes amount 1080 USD or policy refund-v7"] for step_id in ids}}),
            }
        elif "你是分析步骤的核验员" in system:
            kind = "verify"
            result = {
                "role": "assistant",
                "content": json.dumps({"items": [{"status": "met"}, {"status": "met"}]}),
            }
        elif "plan_propose" in names:
            kind = "lead"
            lead_calls += 1
            if trigger == "plan_propose":
                result = call("plan", "plan_propose", {"reason": "Two specialists must cross-check the refund calculation."})
            else:
                first = call(
                    "calculate",
                    "subagent_spawn",
                    {"profile": "compute", "task": "Calculate and verify with the policy expert.", "delegation_role": "helper"},
                )
                second = call(
                    "policy", "subagent_spawn", {"profile": helper, "task": "Check the definition with compute.", "delegation_role": "helper"}
                )
                second["tool_calls"][0]["index"] = 1
                result = {"role": "assistant", "content": "", "tool_calls": first["tool_calls"] + second["tool_calls"]}
        elif names == {"agent_message"}:
            kind = "coordination"
            coordinator_calls.append(body)
            if coordination_fails:
                return httpx2.Response(401, json={"error": {"message": "Provider access revoked."}})
            context = json.loads(body["messages"][-1]["content"].rsplit("\n", 1)[1])
            question = next(item for item in context["items"] if item["action"] == "ask")
            result = call(
                "owner-answer",
                "agent_message",
                {"action": "reply", "question_id": question["operation_id"], "body": "Use refund-v7 and the 2026-Q3 ledger period."},
            )
        elif "subagent_return" in names:
            kind = "specialist"
            assert "agent_message" in names
            cards = next(message for message in body["messages"] if "Current collaboration responsibility cards" in str(message.get("content")))
            roster = json.JSONDecoder().raw_decode(cards["content"].split("Current collaboration responsibility cards (server data):\n", 1)[1])[0][
                "roster"
            ]
            assert {card["profile"] for card in roster} == {"lead", "compute", helper}
            profile = helper if "bi_query" in names else "compute"
            calls[profile] += 1
            step = calls[profile]
            if step == 1:
                recipient = next(card for card in roster if card["profile"] == ("lead" if profile == helper else helper))
                result = call(
                    "question", "agent_message", {"action": "ask", "to": recipient["execution_id"], "body": "Confirm refund-v7 for 2026-Q3."}
                )
            elif step == 2:
                question = _tool_results(body)[-1]
                result = call("wait", "agent_message", {"action": "wait", "question_ids": [question["operation_id"]]})
            elif step == 3:
                reply = next(item for item in _inbox(body) if item["action"] == "reply")
                result = call(
                    "verify",
                    "agent_message",
                    {
                        "action": "resolve",
                        "question_id": reply["question_id"],
                        "reply_id": reply["operation_id"],
                        "disposition": "resolved",
                        "reason": "The definition and period match the original assignment.",
                    },
                )
            elif step == 4 and profile == helper:
                question = next(item for item in _inbox(body) if item["action"] == "ask")
                result = call(
                    "policy-answer",
                    "agent_message",
                    {"action": "reply", "question_id": question["operation_id"], "body": "Deduct 120 USD under refund-v7 for 2026-Q3."},
                )
            elif step == 4 and profile == "compute":
                result = call("unexpected-report", "write_report", {"content": "## Refund calculation\nNet revenue is 1080 USD."})
            else:
                assert step == 5
                if profile == "compute":
                    assert _tool_results(body)[-1].get("error") == "plan_answer_only"
                result = call(
                    "result",
                    "subagent_return",
                    {
                        "payload": {
                            "delivery_surface": "chat",
                            "exec_summary": "Net revenue is 1080 USD under refund-v7 for 2026-Q3, verified with the assigned expert.",
                        }
                    },
                )
        else:
            result = {"role": "assistant", "content": "Net revenue is 1080 USD, verified with the assigned expert."}
        observed.append(
            {
                "kind": kind,
                "tools": sorted(names),
                "owner_statuses": [row["status"] for row in rows(case, "turn_agent_execution") if row["runtime_profile_name"] == "lead"],
            }
        )
        return response(body, result, "tool_calls" if result.get("tool_calls") else "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={
                "planning_mode": "planning" if trigger == "approved" else "auto",
                "workspace_id": case.fixture.scope.workspace_id,
                "message": "Compute net revenue from 1200 USD gross and 120 USD product refunds. Have compute ask the policy specialist about refund-v7 and 2026-Q3, and have the policy specialist confirm the accepted scope with Lead. Return the answer in chat.",
            },
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        if trigger == "approved":
            assert not calls["compute"] and not calls[helper]
            plan = rows(case, "plans")[0]
            assert plan["status"] == "proposed"
            approved = await case.client.post(
                "/api/v1/chat/send",
                json={
                    "session_id": accepted.json()["session_id"],
                    "workspace_id": case.fixture.scope.workspace_id,
                    "message": "Run the approved plan.",
                    "plan_execution_id": plan["id"],
                    "expected_plan_revision": plan["current_rev"],
                },
            )
            assert approved.status_code == 200, approved.text
            turn_id = approved.json()["turn_id"]
            await settled(turn_id)
    tables = {
        name: rows(case, name)
        for name in ("turn_agent_execution", "agent_collaboration_operation", "agent_collaboration_participant", "plans", "plan_steps")
    }
    tables["turns"] = [row for row in rows(case, "turns") if row["id"] == turn_id]
    assert tables["plans"], observed
    if coordination_fails:
        assert tables["turns"][0]["status"] == "failed", tables
        assert tables["plans"][0]["status"] == "failed"
        assert len(tables["turn_agent_execution"]) == 3
        assert len(coordinator_calls) == 1
        assert lead_calls == (0 if trigger == "approved" else 1)
        assert calls["compute"] <= 2 and calls[helper] <= 2
        return
    assert tables["turns"][0]["status"] == "completed", tables
    assert len(tables["plan_steps"]) == 2
    assert all(json.loads(step["verify_json"])["verdict"] == "pass" for step in tables["plan_steps"])
    specialists = [item for item in observed if item["kind"] in {"specialist", "coordination"}]
    assert specialists and all(item["owner_statuses"] == ["running"] for item in specialists)
    assert calls == {"compute": 5, helper: 5}
    assert len(coordinator_calls) == 1
    questions = [row for row in tables["agent_collaboration_operation"] if row["action"] == "ask"]
    assert len(questions) == 2 and all(row["dependency_status"] == "resolved" for row in questions)
    assert len(tables["agent_collaboration_participant"]) == 3
    executions = tables["turn_agent_execution"]
    assert len(executions) == 3 and all(row["status"] == "completed" for row in executions)
    assert lead_calls == (0 if trigger == "approved" else 1)
    assert tables["turns"][0]["tokens_input"] >= sum(step["tokens_input"] for step in tables["plan_steps"]) + 17
    events = [row for row in rows(case, "turn_events") if row["turn_id"] == turn_id]
    owner_events = [(row, json.loads(row["payload_json"])) for row in events if row["event_type"] == "agent_spawned"]
    owner_events = [(row, payload) for row, payload in owner_events if payload.get("profile") == "lead"]
    assert len({payload["subagent_id"] for _, payload in owner_events}) == 1
    assert [payload.get("status") for _, payload in owner_events][-1] == "completed"
    assert owner_events[-1][0]["seq"] < next(row["seq"] for row in events if row["event_type"] == "done")
    assert await orch.canvas_store.get_canvas_by_session(accepted.json()["session_id"]) is None
    assert not any(row["event_type"] == "canvas.section_added" for row in events)
    done = next(json.loads(row["payload_json"]) for row in events if row["event_type"] == "done")
    assert done["response_kind"] == "chat"
    answers = [json.loads(row["payload_json"])["text"] for row in events if row["event_type"] == "final_answer"]
    assert answers[-1] == tables["turns"][0]["final_answer"]
    assert "1080" in answers[-1]
