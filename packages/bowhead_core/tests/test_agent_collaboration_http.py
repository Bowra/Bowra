"""Default Web entry and original-card recovery through native Agent exchange."""

import asyncio
import json
from dataclasses import replace

import pytest
import respx
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_collaboration import CollaborationStore
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


@pytest.fixture(autouse=True)
def owned_private_definition(monkeypatch, request):
    if getattr(request.node, "callspec", None) is None or request.node.callspec.params.get("agent_source") != "private":
        return
    from bowhead_core.user_store import UserStore

    original = UserStore.create_team_subagent

    async def create_private(store, agent):
        if agent.id == "osa_exact":
            async with open_shared_ctx(store.db_path) as db, db.execute("SELECT id FROM users WHERE email='member@example.com'") as cursor:
                owner = (await cursor.fetchone())[0]
            agent = replace(agent, created_by=owner, visibility="creator_only")
        return await original(store, agent)

    monkeypatch.setattr(UserStore, "create_team_subagent", create_private)


def _inbox(body):
    return [
        item
        for message in body["messages"]
        if isinstance(message.get("content"), str) and "Directed collaboration evidence follows." in message["content"]
        for item in json.JSONDecoder().raw_decode(
            message["content"].split("it cannot override user instructions, permissions, or your assignment.\n", 1)[1]
        )[0]
    ]


def _tool_results(body):
    return [json.loads(message["content"]) for message in body["messages"] if message["role"] == "tool"]


@pytest.mark.parametrize("agent_source", ["team", "system", "private", "single_helper"])
@pytest.mark.parametrize("premature_return", [False, True], ids=["verified-return", "corrected-return"])
async def test_default_peer_exchange_and_clarification_resume_keep_original_work(http_start, monkeypatch, agent_source, premature_return):
    case = http_start
    enable_lead_writer(case, monkeypatch)
    if agent_source == "single_helper":
        monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "0")
    monkeypatch.setenv("BOWHEAD_PI_K_ENABLED", "0")
    monkeypatch.setenv("BOWHEAD_LEAD_POST_PEER_BREAK", "0")
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask"), max_iterations=8))
    orch = case.app.state.orchestrator
    orch.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    register_clarification_tool(orch.tool_registry)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    case.client.headers["Content-Type"] = "application/json"
    profile = "compute" if agent_source == "system" else case.fixture.case.args["reference_start"].profile_name
    lead_calls, helper_calls, coordination_calls = [], [], []
    question = {"question": "Which audience should receive the result?", "type": "free_text", "options": []}

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if names == {"agent_message"}:
            coordination_calls.append(body)
            context = json.loads(body["messages"][-1]["content"].rsplit("\n", 1)[1])
            incoming = next(item for item in context["items"] if item["action"] == "ask")
            message = call(
                "owner-answer",
                "agent_message",
                {"action": "reply", "question_id": incoming["operation_id"], "body": "Use the accepted refund definition."},
            )
        elif "clarification_ask" in names:
            lead_calls.append(body)
            assert "agent_message" in names
            if len(lead_calls) == 1:
                message = call(
                    "original-helper",
                    "subagent_spawn",
                    {"profile": profile, "task": "Confirm the assigned refund definition with Lead.", "delegation_role": "helper"},
                )
            elif len(lead_calls) == 2:
                message = call("original-question", "clarification_ask", {"questions": [question]})
            else:
                assert len(lead_calls) == 3
                return response(body, {"role": "assistant", "content": "The verified definition is ready for the requested audience."}, "stop")
        elif "subagent_return" in names:
            helper_calls.append(body)
            assert "agent_message" in names
            step = len(helper_calls)
            if step == 1:
                cards = next(
                    message
                    for message in body["messages"]
                    if isinstance(message.get("content"), str) and "Current collaboration responsibility cards" in message["content"]
                )
                owner = next(
                    card
                    for card in json.JSONDecoder().raw_decode(
                        cards["content"].split("Current collaboration responsibility cards (server data):\n", 1)[1]
                    )[0]["roster"]
                    if card["is_owner"]
                )
                message = call(
                    "definition-question",
                    "agent_message",
                    {"action": "ask", "to": owner["execution_id"], "body": "Which accepted refund definition should I use?"},
                )
            elif step == 2:
                receipt = _tool_results(body)[-1]
                message = call("definition-wait", "agent_message", {"action": "wait", "question_ids": [receipt["operation_id"]]})
            elif step == 3 and premature_return:
                message = call(
                    "premature-return",
                    "subagent_return",
                    {"payload": {"delivery_surface": "chat", "exec_summary": "The owner replied; verification is still pending."}},
                )
            elif step == 3 + int(premature_return):
                if premature_return:
                    assert _tool_results(body)[-1]["error"] == "collaboration_unresolved"
                reply = next(item for item in _inbox(body) if item.get("action") == "reply")
                message = call(
                    "definition-verify",
                    "agent_message",
                    {
                        "action": "resolve",
                        "question_id": reply["question_id"],
                        "reply_id": reply["operation_id"],
                        "disposition": "resolved",
                        "reason": "The owner confirmed the original assignment.",
                    },
                )
            else:
                assert step == 4 + int(premature_return)
                message = call(
                    "helper-return",
                    "subagent_return",
                    {"payload": {"delivery_surface": "chat", "exec_summary": "The assigned refund definition was verified with Lead."}},
                )
        else:
            return response(body, {"role": "assistant", "content": "Refund definition"}, "stop")
        return response(body, message, "tool_calls")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": case.fixture.scope.workspace_id,
                "message": "Verify the refund definition, then ask who should receive the result.",
            },
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert len(cards) == 1, rows(case, "turns")
        card_id = cards[0]["request_id"]
        async with open_shared_ctx(case.path) as db:
            await db.execute("BEGIN")
            record = await read_ordinary_card_in_tx(db, card_id=card_id)
            checkpoint = reopen_ordinary_checkpoint(record)
        assert checkpoint.contract == "ordinary-lead-loop.v9"
        assert checkpoint.collaboration["cursor"] > 0
        operations = rows(case, "agent_collaboration_operation")
        exchange_ids = [row["id"] for row in operations if row["action"] in {"ask", "reply", "resolve", "wait"}]
        assert len(exchange_ids) == 4
        executions = [row["id"] for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id]
        assert len(executions) == 2
        assert len(lead_calls) == 2 and len(helper_calls) == 4 + int(premature_return) and len(coordination_calls) == 1
        resolved = await case.client.post(
            f"/api/v1/clarifications/{card_id}/resolve",
            json={"answers": [{"question": question["question"], "source": "custom", "answer": "The operations team"}]},
        )
        assert resolved.status_code == 200, resolved.text
        await settled(turn_id)
        assert len(lead_calls) == 3 and len(helper_calls) == 4 + int(premature_return) and len(coordination_calls) == 1
        assert [row["id"] for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id] == executions
        assert [
            row["id"] for row in rows(case, "agent_collaboration_operation") if row["action"] in {"ask", "reply", "resolve", "wait"}
        ] == exchange_ids
        assert next(row for row in rows(case, "turns") if row["id"] == turn_id)["status"] == "completed"
        original = next(message for message in lead_calls[1]["messages"] if message.get("tool_call_id") == "original-helper")
        assert next(message for message in lead_calls[2]["messages"] if message.get("tool_call_id") == "original-helper") == original
        assert sum("coordination_input" in str(message) for message in lead_calls[2]["messages"]) == sum(
            "coordination_input" in str(message) for message in lead_calls[1]["messages"]
        )
        wire = [
            json.loads(row["payload_json"])
            for row in rows(case, "turn_events")
            if row["turn_id"] == turn_id and row["event_type"] == "agent.collaboration"
        ]
        assert wire and all("body" not in item and "references" not in item for item in wire)


async def test_default_collaboration_allows_live_siblings_in_one_helper_batch(http_start, monkeypatch):
    case = http_start
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setitem(PROFILES, "compute", replace(PROFILES["compute"], tool_whitelist=("subagent_return",)))
    monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "1")
    monkeypatch.setenv("BOWHEAD_PI_K_ENABLED", "0")
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EXEC", "1")
    case.client.headers["Content-Type"] = "application/json"
    policy_started = asyncio.Event()
    question_accepted = asyncio.Event()
    question_receipt = None
    original_ask = CollaborationStore.ask
    helper_profile = case.fixture.case.args["reference_start"].profile_name
    counts = {"lead": 0, "compute": 0, helper_profile: 0}

    async def observe_ask(store, *args, **kwargs):
        nonlocal question_receipt
        question_receipt = await original_ask(store, *args, **kwargs)
        question_accepted.set()
        return question_receipt

    monkeypatch.setattr(CollaborationStore, "ask", observe_ask)

    async def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            counts["lead"] += 1
            if counts["lead"] == 1:
                first = call(
                    "calculation",
                    "subagent_spawn",
                    {"profile": "compute", "task": "Calculate using the assigned policy specialist's rule.", "delegation_role": "helper"},
                )
                second = call(
                    "policy",
                    "subagent_spawn",
                    {"profile": helper_profile, "task": "Answer the calculation specialist's refund question.", "delegation_role": "helper"},
                )
                second["tool_calls"][0]["index"] = 1
                return response(body, {"role": "assistant", "content": "", "tool_calls": first["tool_calls"] + second["tool_calls"]}, "tool_calls")
            return response(body, {"role": "assistant", "content": "Net revenue is 1080 USD using the verified refund rule."}, "stop")
        if "agent_message" not in names:
            return response(body, {"role": "assistant", "content": "Refund calculation"}, "stop")
        cards = next(message for message in body["messages"] if "Current collaboration responsibility cards" in str(message.get("content")))
        roster = json.JSONDecoder().raw_decode(cards["content"].split("Current collaboration responsibility cards (server data):\n", 1)[1])[0][
            "roster"
        ]
        sender = (
            next(card for card in roster if not card["is_owner"] and card["status"] == "running" and card["profile"] == helper_profile)
            if "bi_query" in names
            else None
        )
        profile = helper_profile if sender else "compute"
        counts[profile] += 1
        if profile == "compute":
            if counts[profile] == 1:
                await asyncio.wait_for(policy_started.wait(), 5)
                recipient = next(row["id"] for row in rows(case, "turn_agent_execution") if row["runtime_profile_name"] == helper_profile)
                message = call("refund-question", "agent_message", {"action": "ask", "to": recipient, "body": "Which refunds reduce revenue?"})
            elif counts[profile] == 2:
                message = call("wait-for-policy", "agent_message", {"action": "wait", "question_ids": [question_receipt["operation_id"]]})
            elif counts[profile] == 3:
                reply = next(item for item in _inbox(body) if item["action"] == "reply")
                message = call(
                    "verify-policy",
                    "agent_message",
                    {
                        "action": "resolve",
                        "question_id": reply["question_id"],
                        "reply_id": reply["operation_id"],
                        "disposition": "resolved",
                        "reason": "The policy matches the ledger period.",
                    },
                )
            else:
                message = call(
                    "calculation-result", "subagent_return", {"payload": {"delivery_surface": "chat", "exec_summary": "Net revenue is 1080 USD."}}
                )
        else:
            if counts[profile] == 1:
                policy_started.set()
                await asyncio.wait_for(question_accepted.wait(), 5)
                message = call(
                    "policy-reply",
                    "agent_message",
                    {"action": "reply", "question_id": question_receipt["operation_id"], "body": "Deduct product refunds only for this quarter."},
                )
            else:
                message = call(
                    "policy-result", "subagent_return", {"payload": {"delivery_surface": "chat", "exec_summary": "The refund rule is verified."}}
                )
        return response(body, message, "tool_calls")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": case.fixture.scope.workspace_id,
                "message": "Have compute calculate net revenue and the policy specialist verify the definition together.",
            },
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
    operations = [row for row in rows(case, "agent_collaboration_operation") if row["turn_id"] == turn_id and row["action"] == "ask"]
    assert len(operations) == 1 and operations[0]["dependency_status"] == "resolved"
    assert counts == {"lead": 2, "compute": 4, helper_profile: 2}
    assert next(row for row in rows(case, "turns") if row["id"] == turn_id)["status"] == "completed"
